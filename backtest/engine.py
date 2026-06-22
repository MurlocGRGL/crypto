"""
Backtest engine — simuluje live běh analyzátoru na historických datech.

ARCHITEKTURA (no-look-ahead záruky):
  1. Indicator series se předpočítají přes celý DataFrame JEDNOU (O(n)).
     series.iloc[i] = kauzální rolling operace → žádné budoucí data.
  2. Ichimoku senkou_a/b: shift(+26) ověřen jako bezpečný (chikou s shift(-26) nikde
     nečteme — viz ichimoku_signal() v indicators.py).
  3. Pro 4H/1D Ichimoku: pointer j4h/j1d se posunuje jen dopředu, čte pouze 4H/1D bary
     jejichž close_time ≤ uzavření aktuálního 1H baru.
  4. Entry = open[i+1] (next bar) — nikdy close nebo intrabar cena signálního baru.
  5. Exit evaluace: high/low baru j pro j > entry_bar.
     Intrabar konflikt (SL i TP zasaženy ve stejné svíčce) → SL vítězí (pesimisticky).
  6. Timeout: 48 1H barů (2 dny) bez exitu → uzavřeno na close.

ZJEDNODUŠENÍ oproti live systému:
  SL/TP: 1.5× ATR (live systém používá Volume Profile).
  To NEOVLIVNÍ výběr směru (LONG/SHORT) — logika je identická.
  Funding rate: ignorováno (historická data nejsou dostupná bez API).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

# ── Konstanty ─────────────────────────────────────────────────────────────────

# Min. barů potřebných pro Ichimoku: SENKOU_B (52) + KIJUN shift (26) + rezerva
MIN_BARS = config.ICHIMOKU_SENKOU_B + config.ICHIMOKU_KIJUN + 5   # 83

ATR_PERIOD    = 14
ATR_SL_MULT   = 1.5   # SL vzdálenost = ATR_SL_MULT × ATR
ATR_TP_MULT   = 1.5   # TP1 vzdálenost = ATR_TP_MULT × ATR (1:1 R:R)
SWING_WINDOW  = 20

TRADE_TIMEOUT_BARS       = 48   # 2 dny na 1H
HYPE_SMALL_SAMPLE_THRESH = 30   # pod tímto počtem obchodů = varování


# ── Precompute indicator series ───────────────────────────────────────────────

POC_LOOKBACK = 300   # barů pro rolling Volume Profile (shoduje se s CANDLE_LIMIT v live)
POC_BINS     = config.VOLUME_PROFILE_BINS


def _rolling_poc(df: pd.DataFrame) -> pd.Series:
    """
    Rolling Point of Control: cenová hladina s nejvyšším objemem za posledních
    POC_LOOKBACK barů. Používá numpy histogram na midpointu (H+L)/2 barů.
    O(n) numpy volání, každé na POC_LOOKBACK prvcích → kauzální, no look-ahead.
    """
    n     = len(df)
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    vol   = df["volume"].to_numpy(dtype=float)
    mid   = (high + low) / 2.0
    poc   = np.full(n, np.nan)

    for i in range(POC_LOOKBACK - 1, n):
        start   = i - POC_LOOKBACK + 1
        sl_mid  = mid[start : i + 1]
        sl_vol  = vol[start : i + 1]
        sl_high = high[start : i + 1]
        sl_low  = low[start : i + 1]
        p_min   = sl_low.min()
        p_max   = sl_high.max()
        if p_max <= p_min:
            poc[i] = mid[i]
            continue
        counts, edges = np.histogram(sl_mid, bins=POC_BINS,
                                     range=(p_min, p_max), weights=sl_vol)
        b       = int(np.argmax(counts))
        poc[i]  = (edges[b] + edges[b + 1]) / 2.0

    return pd.Series(poc, index=df.index)


def _precompute(df: pd.DataFrame) -> dict:
    """
    Předpočítá všechny indicator série pro celý DataFrame (O(n)).
    Všechny operace jsou kauzální (rolling window zpět v čase) → no look-ahead.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    # Ichimoku — shift(+26) ověřen: posune series dopředu v indexu,
    # tzn. series.iloc[i] pochází z dat baru i-26 (minulost, bezpečné)
    tenkan   = (high.rolling(config.ICHIMOKU_TENKAN).max()   +
                low.rolling(config.ICHIMOKU_TENKAN).min())   / 2
    kijun    = (high.rolling(config.ICHIMOKU_KIJUN).max()    +
                low.rolling(config.ICHIMOKU_KIJUN).min())    / 2
    senkou_a = ((tenkan + kijun) / 2).shift(config.ICHIMOKU_KIJUN)
    senkou_b = ((high.rolling(config.ICHIMOKU_SENKOU_B).max() +
                 low.rolling(config.ICHIMOKU_SENKOU_B).min()) / 2
               ).shift(config.ICHIMOKU_KIJUN)

    # RSI (EMA smoothing, stejně jako live indicators.py)
    delta  = close.diff()
    gain   = delta.clip(lower=0).ewm(com=config.RSI_PERIOD - 1, adjust=False).mean()
    loss   = (-delta.clip(upper=0)).ewm(com=config.RSI_PERIOD - 1, adjust=False).mean()
    rsi    = 100.0 - 100.0 / (1.0 + gain / loss.replace(0.0, np.nan))

    # ATR (EMA)
    prev_c = close.shift(1)
    tr     = pd.concat([
        high - low,
        (high - prev_c).abs(),
        (low  - prev_c).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # Swing high/low (rolling max/min)
    swing_high = high.rolling(SWING_WINDOW).max()
    swing_low  = low.rolling(SWING_WINDOW).min()

    # Divergence (bullish: close nové dno ale RSI ne, bearish: opak)
    divergence = (
        ((close <= close.rolling(5).min()) & (rsi > rsi.rolling(5).min().shift(1))) |
        ((close >= close.rolling(5).max()) & (rsi < rsi.rolling(5).max().shift(1)))
    )

    # VWAP s denním resetem (UTC půlnoc) — stejná logika jako live systém
    typical = (high + low + close) / 3.0
    tp_vol  = typical * df["volume"]
    if "timestamp" in df.columns:
        day_key    = pd.to_datetime(df["timestamp"]).dt.normalize()
        cum_tp_vol = tp_vol.groupby(day_key).cumsum()
        cum_vol    = df["volume"].groupby(day_key).cumsum()
        vwap       = (cum_tp_vol / cum_vol.replace(0.0, np.nan)).fillna(typical)
    else:
        vwap = (tp_vol.cumsum() / df["volume"].cumsum().replace(0.0, np.nan)).fillna(typical)

    # Rolling Point of Control (Volume Profile POC)
    poc = _rolling_poc(df)

    return {
        "tenkan": tenkan, "kijun": kijun,
        "senkou_a": senkou_a, "senkou_b": senkou_b,
        "rsi": rsi, "atr": atr,
        "swing_high": swing_high, "swing_low": swing_low,
        "divergence": divergence,
        "vwap": vwap,
        "poc":  poc,
    }


def _ichimoku_trend(close_val: float, sa: float, sb: float) -> str:
    if pd.isna(sa) or pd.isna(sb):
        return "N/A"
    cloud_top = max(sa, sb)
    cloud_bot = min(sa, sb)
    if close_val > cloud_top:
        return "BULLISH"
    if close_val < cloud_bot:
        return "BEARISH"
    return "NEUTRÁLNÍ"


def _signal(
    htf_trend: str,
    stf_trend: str,
    rsi_val: float,
    divergence: bool,
    threshold: int = 45,
    require_htf_confirm: bool = False,
) -> tuple[str, float]:
    """
    Identická logika jako build_symbol_analysis v report_generator.py.
    Funding rate vynecháno (historicky nedostupné).

    threshold           — min. long_pct/short_pct pro LONG/SHORT signál (default 45 = live chování)
    require_htf_confirm — Varianta C: LONG jen při BULLISH HTF, SHORT jen při BEARISH HTF

    Vrátí (direction, trader_score).
    """
    score_map = {"BULLISH": 1, "BEARISH": -1, "NEUTRÁLNÍ": 0, "N/A": 0}
    score     = score_map.get(htf_trend, 0) * 2 + score_map.get(stf_trend, 0)
    rsi_bias  = (rsi_val - 50.0) / 50.0
    score    += rsi_bias * 2.0

    long_pct  = max(5, min(80, round(50 + score * 12)))
    short_pct = max(5, min(80, round(50 - score * 12)))
    wait_pct  = max(5, 100 - long_pct - short_pct)
    total     = long_pct + short_pct + wait_pct
    long_pct, short_pct, wait_pct = (
        round(x * 100 / total) for x in (long_pct, short_pct, wait_pct)
    )

    if long_pct >= short_pct and long_pct >= wait_pct and long_pct >= threshold:
        direction = "LONG"
    elif short_pct >= long_pct and short_pct >= wait_pct and short_pct >= threshold:
        direction = "SHORT"
    else:
        direction = "WAIT"

    # Varianta C: vyfiltruj counter-trend obchody
    if require_htf_confirm and direction != "WAIT":
        if direction == "LONG"  and htf_trend != "BULLISH":
            direction = "WAIT"
        elif direction == "SHORT" and htf_trend != "BEARISH":
            direction = "WAIT"

    trader_score = max(0, min(100, round(50 + score * 10 + (10 if divergence else 0))))
    return direction, float(trader_score)


def _signal_confluence(
    htf_trend: str,
    stf_trend: str,
    rsi_val: float,
    close: float,
    vwap: float,
    poc: float,
) -> str:
    """
    Varianta D — LONG/SHORT pouze při konfluenci všech 5 podmínek.

    LONG:  HTF=BULLISH, STF=BULLISH, RSI ∈ [45,65], close > VWAP, close > POC
    SHORT: HTF=BEARISH, STF=BEARISH, RSI ∈ [35,55], close < VWAP, close < POC

    RSI pásma záměrně symetrická kolem 50:
      LONG  45–65 = momentum ale ne přeextendovaný (bullish bias, ne extrém)
      SHORT 35–55 = momentum ale ne přeextendovaný (bearish bias, ne extrém)
    """
    if pd.isna(vwap) or pd.isna(poc):
        return "WAIT"

    if (htf_trend == "BULLISH" and stf_trend == "BULLISH"
            and 45.0 <= rsi_val <= 65.0
            and close > vwap and close > poc):
        return "LONG"

    if (htf_trend == "BEARISH" and stf_trend == "BEARISH"
            and 35.0 <= rsi_val <= 55.0
            and close < vwap and close < poc):
        return "SHORT"

    return "WAIT"


# ── Hlavní backtest smyčka ────────────────────────────────────────────────────

def run_symbol_backtest(
    symbol: str,
    dfs: dict,               # {"1h": df, "4h": df | None, "1d": df | None}
    fees_pct: float = 0.0,
    threshold: int = 45,     # min. long_pct/short_pct pro otevření obchodu (A/B/C)
    require_htf_confirm: bool = False,  # Varianta C: HTF musí souhlasit se směrem
    signal_mode: str = "score",         # "score" (A/B/C) | "confluence" (D)
) -> dict:
    """
    Spustí backtest pro jeden symbol a vrátí:
      symbol, trades, equity, n_bars, n_signals,
      period_start, period_end, is_small_sample
    """
    df_1h = dfs.get("1h")
    df_4h = dfs.get("4h")
    df_1d = dfs.get("1d")

    if df_1h is None or len(df_1h) < MIN_BARS + 2:
        return {"symbol": symbol, "error": "nedostatek 1H dat"}

    # Normalizuj timestamp na UTC
    for df in [df_1h, df_4h, df_1d]:
        if df is not None:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    # Předpočítej série
    pre_1h = _precompute(df_1h)
    pre_4h = _precompute(df_4h) if (df_4h is not None and len(df_4h) >= MIN_BARS) else None
    pre_1d = _precompute(df_1d) if (df_1d is not None and len(df_1d) >= MIN_BARS) else None

    # Pointery do 4H / 1D (posouváme se jen dopředu → O(n) celkem)
    # Pointer j = index posledního uzavřeného baru daného TF v čase signal_close_ts
    j4h = -1
    j1d = -1

    n          = len(df_1h)
    trades: list[dict] = []
    equity     = [1.0]
    n_signals  = 0
    open_trade: dict | None = None

    for i in range(MIN_BARS, n - 1):
        bar          = df_1h.iloc[i]
        bar_ts       = bar["timestamp"]
        # Čas uzavření tohoto 1H baru (= kdy smíme číst jeho data)
        signal_ts    = bar_ts + pd.Timedelta(hours=1)

        # ── Posuň pointery do 4H / 1D ────────────────────────────────────
        if df_4h is not None:
            # 4H bar s open_time T zavírá v T+4h; zahrneme ho pokud T+4h ≤ signal_ts
            while (j4h + 1 < len(df_4h) and
                   df_4h.iloc[j4h + 1]["timestamp"] + pd.Timedelta(hours=4) <= signal_ts):
                j4h += 1

        if df_1d is not None:
            while (j1d + 1 < len(df_1d) and
                   df_1d.iloc[j1d + 1]["timestamp"] + pd.Timedelta(hours=24) <= signal_ts):
                j1d += 1

        # ── Vyhodnoť otevřený obchod ──────────────────────────────────────
        if open_trade is not None:
            high_i = float(bar["high"])
            low_i  = float(bar["low"])
            ot     = open_trade
            sl, tp1, side, entry = ot["sl"], ot["tp1"], ot["side"], ot["entry"]

            if side == "LONG":
                sl_hit  = low_i  <= sl
                tp1_hit = high_i >= tp1
            else:
                sl_hit  = high_i >= sl
                tp1_hit = low_i  <= tp1

            # Pesimisticky: při konfliktu SL vítězí
            if sl_hit:
                hit, hit_price = "SL", sl
            elif tp1_hit:
                hit, hit_price = "TP1", tp1
            else:
                hit = hit_price = None

            bars_held = i - ot["entry_bar"]
            if hit is None and bars_held >= TRADE_TIMEOUT_BARS:
                hit, hit_price = "TIMEOUT", float(bar["close"])

            if hit is not None:
                sl_dist_pct = abs(entry - sl) / entry
                fees_r = (2.0 * fees_pct / 100.0) / sl_dist_pct if sl_dist_pct > 1e-10 else 0.0
                raw_r  = (
                    (hit_price - entry) / abs(entry - sl) if side == "LONG"
                    else (entry - hit_price) / abs(sl - entry)
                )
                pnl_r = raw_r - fees_r

                equity.append(equity[-1] * (1.0 + pnl_r * 0.01))   # 1 % risk/trade
                ot.update({
                    "exit_bar":    i,
                    "exit_price":  round(hit_price, 8),
                    "exit_reason": hit,
                    "pnl_r":       round(pnl_r, 4),
                    "bars_held":   bars_held,
                    "win":         hit.startswith("TP"),
                })
                trades.append(ot)
                open_trade = None

        # ── Generuj signál (jen když nejsme v obchodu) ────────────────────
        if open_trade is not None:
            continue

        # HTF trend (1D preferováno, fallback 4H, fallback 1H)
        htf_trend = "N/A"
        if pre_1d is not None and j1d >= MIN_BARS - 1:
            htf_trend = _ichimoku_trend(
                float(df_1d.iloc[j1d]["close"]),
                float(pre_1d["senkou_a"].iloc[j1d]),
                float(pre_1d["senkou_b"].iloc[j1d]),
            )
        elif pre_4h is not None and j4h >= MIN_BARS - 1:
            htf_trend = _ichimoku_trend(
                float(df_4h.iloc[j4h]["close"]),
                float(pre_4h["senkou_a"].iloc[j4h]),
                float(pre_4h["senkou_b"].iloc[j4h]),
            )

        # STF trend (4H preferováno, fallback 1H)
        if pre_4h is not None and j4h >= MIN_BARS - 1:
            stf_trend = _ichimoku_trend(
                float(df_4h.iloc[j4h]["close"]),
                float(pre_4h["senkou_a"].iloc[j4h]),
                float(pre_4h["senkou_b"].iloc[j4h]),
            )
        else:
            stf_trend = _ichimoku_trend(
                float(bar["close"]),
                float(pre_1h["senkou_a"].iloc[i]),
                float(pre_1h["senkou_b"].iloc[i]),
            )

        rsi_val = float(pre_1h["rsi"].iloc[i])
        atr_val = float(pre_1h["atr"].iloc[i])
        div_val = bool(pre_1h["divergence"].iloc[i])

        if pd.isna(rsi_val) or pd.isna(atr_val) or atr_val <= 0:
            continue

        if signal_mode == "confluence":
            vwap_i = float(pre_1h["vwap"].iloc[i])
            poc_i  = float(pre_1h["poc"].iloc[i])
            direction   = _signal_confluence(
                htf_trend, stf_trend, rsi_val,
                float(bar["close"]), vwap_i, poc_i,
            )
            trader_score = 80.0  # fixní skóre: všech 5 podmínek splněno = silný setup
        else:
            direction, trader_score = _signal(
                htf_trend, stf_trend, rsi_val, div_val,
                threshold=threshold,
                require_htf_confirm=require_htf_confirm,
            )

        if direction == "WAIT":
            continue

        n_signals += 1

        # Entry = open příšti svíčky (NO LOOK-AHEAD: close baru i ještě neznáme)
        next_bar    = df_1h.iloc[i + 1]
        entry_price = float(next_bar["open"])

        # SL/TP: ATR-based (zjednodušení, live systém používá Volume Profile)
        risk = ATR_SL_MULT * atr_val
        if direction == "LONG":
            sl  = entry_price - risk
            tp1 = entry_price + ATR_TP_MULT * atr_val
        else:
            sl  = entry_price + risk
            tp1 = entry_price - ATR_TP_MULT * atr_val

        # Sanity check
        if direction == "LONG"  and sl >= entry_price:
            continue
        if direction == "SHORT" and sl <= entry_price:
            continue

        open_trade = {
            "symbol":       symbol,
            "side":         direction,
            "entry_bar":    i + 1,
            "entry_ts":     str(next_bar["timestamp"]),
            "entry":        entry_price,
            "sl":           round(sl, 8),
            "tp1":          round(tp1, 8),
            "htf_trend":    htf_trend,
            "stf_trend":    stf_trend,
            "trader_score": trader_score,
        }

    # Otevřený obchod na konci dat → uzavři za close posledního baru
    if open_trade is not None:
        last       = df_1h.iloc[-1]
        entry      = open_trade["entry"]
        sl         = open_trade["sl"]
        hit_price  = float(last["close"])
        sl_dist_pct = abs(entry - sl) / entry
        fees_r     = (2.0 * fees_pct / 100.0) / sl_dist_pct if sl_dist_pct > 1e-10 else 0.0
        raw_r      = (
            (hit_price - entry) / abs(entry - sl) if open_trade["side"] == "LONG"
            else (entry - hit_price) / abs(sl - entry)
        )
        pnl_r = raw_r - fees_r
        equity.append(equity[-1] * (1.0 + pnl_r * 0.01))
        open_trade.update({
            "exit_bar": n - 1,
            "exit_price": round(hit_price, 8),
            "exit_reason": "EOD",
            "pnl_r": round(pnl_r, 4),
            "bars_held": n - 1 - open_trade["entry_bar"],
            "win": False,
        })
        trades.append(open_trade)

    return {
        "symbol":               symbol,
        "trades":               trades,
        "equity":               equity,
        "n_bars":               n,
        "n_signals":            n_signals,
        "period_start":         str(df_1h.iloc[MIN_BARS]["timestamp"]),
        "period_end":           str(df_1h.iloc[-1]["timestamp"]),
        "is_small_sample":      len(trades) < HYPE_SMALL_SAMPLE_THRESH,
        "threshold":            threshold,
        "require_htf_confirm":  require_htf_confirm,
        "signal_mode":          signal_mode,
    }
