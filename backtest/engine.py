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
import warnings
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
G_CONFIRM_WINDOW         = 6    # max barů čekání na potvrzovací svíčku (Varianta G)
H_CONFIRM_WINDOW         = 24   # max barů čekání na dosažení limit úrovně (Varianta H)

# Margin alokovaný na každý obchod (zlomek účtu) — základ pro P&L s pákou
RISK_PER_TRADE = 0.01   # 1 % účtu jako margin per trade


# ── Precompute indicator series ───────────────────────────────────────────────

POC_LOOKBACK = 300   # barů pro rolling Volume Profile (shoduje se s CANDLE_LIMIT v live)
POC_BINS     = config.VOLUME_PROFILE_BINS


def _rolling_volume_profile(df: pd.DataFrame) -> tuple:
    """
    Rolling Volume Profile za posledních POC_LOOKBACK barů.
    Vrátí (poc, val, vah) — Point of Control, Volume Area Low/High (70 % objemu).
    Kauzální: series.iloc[i] závisí jen na barech start..i.
    """
    _VA_PCT = 0.70
    n     = len(df)
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    vol   = df["volume"].to_numpy(dtype=float)
    mid   = (high + low) / 2.0
    poc_arr = np.full(n, np.nan)
    val_arr = np.full(n, np.nan)
    vah_arr = np.full(n, np.nan)

    for i in range(POC_LOOKBACK - 1, n):
        start   = i - POC_LOOKBACK + 1
        sl_mid  = mid[start : i + 1]
        sl_vol  = vol[start : i + 1]
        sl_high = high[start : i + 1]
        sl_low  = low[start : i + 1]
        p_min   = sl_low.min()
        p_max   = sl_high.max()
        if p_max <= p_min:
            poc_arr[i] = val_arr[i] = vah_arr[i] = mid[i]
            continue
        counts, edges = np.histogram(sl_mid, bins=POC_BINS,
                                     range=(p_min, p_max), weights=sl_vol)
        poc_bin    = int(np.argmax(counts))
        poc_arr[i] = (edges[poc_bin] + edges[poc_bin + 1]) / 2.0

        total = counts.sum()
        if total <= 0:
            val_arr[i] = vah_arr[i] = poc_arr[i]
            continue

        lo = hi = poc_bin
        cumvol  = counts[poc_bin]
        target  = total * _VA_PCT
        while cumvol < target:
            can_lo = lo > 0
            can_hi = hi < len(counts) - 1
            if not can_lo and not can_hi:
                break
            if can_lo and can_hi:
                if counts[lo - 1] >= counts[hi + 1]:
                    lo -= 1; cumvol += counts[lo]
                else:
                    hi += 1; cumvol += counts[hi]
            elif can_lo:
                lo -= 1; cumvol += counts[lo]
            else:
                hi += 1; cumvol += counts[hi]

        val_arr[i] = edges[lo]
        vah_arr[i] = edges[hi + 1]

    return (pd.Series(poc_arr, index=df.index),
            pd.Series(val_arr, index=df.index),
            pd.Series(vah_arr, index=df.index))


def _rolling_time_levels(df: pd.DataFrame) -> dict:
    """
    Rolling time-based price levels pro každý bar — kauzální (bar i závisí jen na datech ≤ i).
    ISO týdny (Po–Ne), weekly/monthly open z prvního baru periody.

    Vrátí dict Series s prefixem "tl_":
      tl_weekly_open, tl_weekly_high, tl_weekly_low,
      tl_monday_high, tl_monday_low,
      tl_prev_week_high, tl_prev_week_low,
      tl_monthly_open
    """
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")

    # to_period() ztrácí tz info (korektní chování, data jsou UTC)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wk = ts.dt.to_period("W-SUN")   # ISO týden: Po–Ne
        ym = ts.dt.to_period("M")
    is_mon = ts.dt.dayofweek == 0        # True pro pondělní bary

    # Weekly Open: open prvního baru týdne (kauzální od prvního baru dál)
    weekly_open = df.groupby(wk)["open"].transform("first")

    # Weekly High/Low: kumulativní max/min uvnitř týdne (kauzální)
    weekly_high = df.groupby(wk)["high"].cummax()
    weekly_low  = df.groupby(wk)["low"].cummin()

    # Monday High/Low: kumuluje jen v pondělních barech, po pondělí nesení fix
    # NaN na ne-pondělních barech → cummax/cummin přenáší hodnotu pondělí dál v týdnu
    mon_h = df["high"].where(is_mon)
    mon_l = df["low"].where(is_mon)
    monday_high = mon_h.groupby(wk).cummax()
    monday_low  = mon_l.groupby(wk).cummin()

    # Prev Week High/Low: kompletní předchozí týden (shift(1) po přeskupení)
    wk_high_all = df.groupby(wk)["high"].max()
    wk_low_all  = df.groupby(wk)["low"].min()
    pwh_dict = wk_high_all.shift(1).to_dict()
    pwl_dict = wk_low_all.shift(1).to_dict()
    prev_week_high = pd.Series(
        [float(pwh_dict.get(w, np.nan)) for w in wk], index=df.index
    )
    prev_week_low = pd.Series(
        [float(pwl_dict.get(w, np.nan)) for w in wk], index=df.index
    )

    # Monthly Open: open prvního baru měsíce
    monthly_open = df.groupby(ym)["open"].transform("first")

    return {
        "tl_weekly_open":    weekly_open,
        "tl_weekly_high":    weekly_high,
        "tl_weekly_low":     weekly_low,
        "tl_monday_high":    monday_high,
        "tl_monday_low":     monday_low,
        "tl_prev_week_high": prev_week_high,
        "tl_prev_week_low":  prev_week_low,
        "tl_monthly_open":   monthly_open,
    }


def _rolling_volatility_regime(df: pd.DataFrame, avg_window: int = 40) -> pd.Series:
    """
    Rolling volatility regime: "TRENDING" | "RANGING" | "MIXED" | "UNKNOWN".
    Replikuje live volatility_regime() z indicators.py:
      score = (atr_ratio + bw_ratio) / 2
      > 1.20 → TRENDING, < 0.85 → RANGING, jinak MIXED.
    """
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    prev_c = close.shift(1)
    tr     = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    atr_s  = tr.ewm(span=14, adjust=False).mean()
    atr_ratio = atr_s / atr_s.rolling(avg_window).mean().replace(0.0, np.nan)

    sma      = close.rolling(20).mean()
    bw       = (close.rolling(20).std() * 4.0) / sma.replace(0.0, np.nan)
    bw_ratio = bw / bw.rolling(avg_window).mean().replace(0.0, np.nan)

    score  = (atr_ratio + bw_ratio) / 2.0
    regime = pd.Series("UNKNOWN", index=df.index, dtype=object)
    regime[(score >= 0.85) & (score <= 1.20)] = "MIXED"
    regime[score < 0.85]  = "RANGING"
    regime[score > 1.20]  = "TRENDING"
    return regime


def _rolling_last_bos(df: pd.DataFrame, swing_window: int = 20) -> pd.Series:
    """
    Rolling směr posledního BOS (Break of Structure): "BULLISH" | "BEARISH" | NaN.
    BOS BULLISH: close prorazí nad rolling swing high (max předchozích swing_window barů).
    BOS BEARISH: close prorazí pod rolling swing low.
    Forward-fill: drží poslední BOS směr až do nového BOS.
    """
    close      = df["close"]
    swing_high = df["high"].rolling(swing_window).max().shift(1)  # shift → no look-ahead
    swing_low  = df["low"].rolling(swing_window).min().shift(1)
    last_bos   = pd.Series(np.nan, index=df.index, dtype=object)
    last_bos[close > swing_high] = "BULLISH"
    last_bos[close < swing_low]  = "BEARISH"
    return last_bos.ffill()


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

    # Divergence — rozdělena na směrové série pro Variantu E
    # bullish_div: close na novém minimu ale RSI ne → blokuje SHORT
    # bearish_div: close na novém maximu ale RSI ne → blokuje LONG
    bullish_div = (close <= close.rolling(5).min()) & (rsi > rsi.rolling(5).min().shift(1))
    bearish_div = (close >= close.rolling(5).max()) & (rsi < rsi.rolling(5).max().shift(1))
    divergence  = bullish_div | bearish_div

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

    # Rolling Volume Profile: POC + VAL + VAH (70 % Volume Area)
    poc, val, vah = _rolling_volume_profile(df)

    # Varianta E: volatilitní režim + BOS směr
    vol_regime = _rolling_volatility_regime(df)
    last_bos   = _rolling_last_bos(df)

    # Varianta F: MACD histogram, Bollinger %B, CVD approximace
    ema12       = close.ewm(span=12, adjust=False).mean()
    ema26       = close.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    macd_hist   = macd_line - macd_line.ewm(span=9, adjust=False).mean()

    bb_sma   = close.rolling(20).mean()
    bb_std   = close.rolling(20).std()
    bb_up    = bb_sma + 2 * bb_std
    bb_dn    = bb_sma - 2 * bb_std
    bb_pct_b = (close - bb_dn) / (bb_up - bb_dn).replace(0.0, np.nan)

    # CVD approx: tick rule (close vs prior close → buy/sell volume), 20-bar rolling sum
    # Normalizováno na -1..+1 přes 100-bar rolling range
    tick_dir = close.diff()
    cvd_raw  = (df["volume"].where(tick_dir > 0, 0.0) -
                df["volume"].where(tick_dir < 0, 0.0)).rolling(20).sum()
    cvd_rmax = cvd_raw.rolling(100).max()
    cvd_rmin = cvd_raw.rolling(100).min()
    cvd_norm = (2.0 * (cvd_raw - cvd_rmin) / (cvd_rmax - cvd_rmin).replace(0.0, np.nan) - 1.0
               ).fillna(0.0).clip(-1.0, 1.0)

    # Varianta H: time-based levels (weekly/monthly open, Mon H/L, prev week H/L)
    tl = _rolling_time_levels(df)

    return {
        "tenkan": tenkan, "kijun": kijun,
        "senkou_a": senkou_a, "senkou_b": senkou_b,
        "rsi": rsi, "atr": atr,
        "swing_high": swing_high, "swing_low": swing_low,
        "divergence":  divergence,
        "bullish_div": bullish_div,
        "bearish_div": bearish_div,
        "vwap": vwap,
        "poc":  poc,
        "val":  val,
        "vah":  vah,
        "vol_regime": vol_regime,
        "last_bos":   last_bos,
        "macd_hist":  macd_hist,
        "bb_pct_b":   bb_pct_b,
        "cvd_norm":   cvd_norm,
        **tl,
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


def _signal_confluence_e(
    htf_trend:   str,
    stf_trend:   str,
    rsi_val:     float,
    close:       float,
    vwap:        float,
    poc:         float,
    vol_regime:  str,
    last_bos:    str,
    bullish_div: bool,
    bearish_div: bool,
    rsi_lo:      float = 45.0,
    rsi_hi:      float = 65.0,
) -> str:
    """
    Varianta E — všechny podmínky D + tři extra filtry:
    1. Volatility regime == TRENDING
    2. Poslední BOS musí souhlasit se směrem (BULLISH → LONG, BEARISH → SHORT)
    3. RSI divergence PROTI směru blokuje vstup:
       bearish_div (close nové max, RSI klesá) blokuje LONG
       bullish_div (close nové min, RSI stoupá) blokuje SHORT

    rsi_lo/rsi_hi: RSI pásmo pro LONG (default 45/65).
    SHORT pásmo = [rsi_lo-10, rsi_hi-10] — symetrické kolem 50.
    """
    if pd.isna(vwap) or pd.isna(poc):
        return "WAIT"

    if vol_regime != "TRENDING":
        return "WAIT"

    if (htf_trend == "BULLISH" and stf_trend == "BULLISH"
            and rsi_lo <= rsi_val <= rsi_hi
            and close > vwap and close > poc
            and last_bos == "BULLISH"
            and not bearish_div):
        return "LONG"

    if (htf_trend == "BEARISH" and stf_trend == "BEARISH"
            and (rsi_lo - 10.0) <= rsi_val <= (rsi_hi - 10.0)
            and close < vwap and close < poc
            and last_bos == "BEARISH"
            and not bullish_div):
        return "SHORT"

    return "WAIT"


def _score_f(
    htf_trend:   str,
    stf_trend:   str,
    last_bos:    str,
    rsi_val:     float,
    macd_hist:   float,
    bb_pct_b:    float,
    cvd_norm:    float,
    vol_regime:  str,
    bullish_div: bool,
    bearish_div: bool,
) -> float:
    """
    Varianta F — kontinuální skóre 0–100 z vážených kategorií.

    Strukturální trend (váha 3): HTF Ichimoku + STF Ichimoku + BOS
    Momentum         (váha 2):  RSI + MACD histogram + BB %B + CVD approx.
    Pozicování       (váha 1.5): funding/LS/OI — nedostupné v OHLCV → 0
    Divergence: binární modifikátor ±10 na výsledné skóre
    Volatility Regime: RANGING → skóre přitáhne ke 50 (×0.5 vzdálenost)

    LONG  pokud score >= threshold (default 65)
    SHORT pokud score <= (100 – threshold)
    """
    trend_map = {"BULLISH": 1.0, "BEARISH": -1.0, "NEUTRÁLNÍ": 0.0, "N/A": 0.0}
    htf_s = trend_map.get(htf_trend, 0.0)
    stf_s = trend_map.get(stf_trend, 0.0)
    bos_s = {"BULLISH": 1.0, "BEARISH": -1.0}.get(last_bos or "", 0.0)
    structural = (htf_s + stf_s + bos_s) / 3.0   # -1..+1

    rsi_n    = (rsi_val - 50.0) / 50.0
    macd_n   = 1.0 if macd_hist > 0 else (-1.0 if macd_hist < 0 else 0.0)
    bb_n     = max(-1.0, min(1.0, (bb_pct_b - 0.5) * 2.0))
    momentum = (rsi_n + macd_n + bb_n + max(-1.0, min(1.0, cvd_norm))) / 4.0  # -1..+1

    raw   = structural * 3.0 + momentum * 2.0   # positioning=0 → dropped
    score = (raw / 6.5) * 50.0 + 50.0           # normalize to 0–100

    if bullish_div:
        score += 10.0
    if bearish_div:
        score -= 10.0

    if vol_regime == "RANGING":
        score = 50.0 + (score - 50.0) * 0.5

    return float(max(0.0, min(100.0, score)))


# ── Hlavní backtest smyčka ────────────────────────────────────────────────────

def run_symbol_backtest(
    symbol: str,
    dfs: dict,                    # {"1h": df, "4h": df | None, "1d": df | None}
    fees_pct: float = 0.0,
    threshold: int = 45,          # min. long_pct/short_pct pro otevření obchodu (A/B/C)
    require_htf_confirm: bool = False,  # Varianta C: HTF musí souhlasit se směrem
    signal_mode: str = "score",   # "score" | "confluence" | "confluence_e" | "variant_f" | "variant_g"
    leverage: float = 1.0,        # pákový efekt — viz RISK_PER_TRADE pro model
    rsi_lo: float = 45.0,         # RSI spodní hranice LONG (Varianta E/G)
    rsi_hi: float = 65.0,         # RSI horní hranice LONG (Varianta E/G); SHORT = [lo-10, hi-10]
    score_threshold: float = 65.0, # práh pro LONG (Varianta F); SHORT < 100-threshold
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
    # Varianta G: čeká na potvrzovací svíčku po E2 signálu
    pending_g: dict | None = None   # {"direction", "expires", "htf_trend", "stf_trend"}
    # Varianta H: čeká na dosažení limit ceny po E2+Weekly_Open signálu
    pending_h: dict | None = None   # {"direction", "expires", "entry_target", "htf_trend", "stf_trend"}

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
                sl_dist_pct = abs(entry - sl) / entry if abs(entry - sl) > 1e-10 else 1e-6
                price_move  = (
                    (hit_price - entry) / entry if side == "LONG"
                    else (entry - hit_price) / entry
                )

                # pnl_r — R-jednotky pro směrové statistiky (win rate, R:R)
                pnl_r = price_move / sl_dist_pct

                # Leverage-aware P&L dle uživatelova modelu:
                #   pnl   = pohyb_ceny% × páka × margin (zlomek účtu)
                #   fees  = margin × páka × fee_rate × 2 (round-trip)
                pnl_pct  = price_move * leverage * RISK_PER_TRADE
                fees_acc = 2.0 * leverage * RISK_PER_TRADE * (fees_pct / 100.0)
                net_pct  = pnl_pct - fees_acc

                equity.append(equity[-1] * (1.0 + net_pct))
                ot.update({
                    "exit_bar":    i,
                    "exit_price":  round(hit_price, 8),
                    "exit_reason": hit,
                    "pnl_r":       round(pnl_r, 4),
                    "pnl_pct":     round(net_pct * 100.0, 4),  # % účtu (leverage-aware)
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

        # ── Varianta G: zkontroluj potvrzovací svíčku po čekajícím signálu ──────
        if signal_mode == "variant_g" and pending_g is not None:
            if i > pending_g["expires"]:
                pending_g = None
            else:
                val_j  = float(pre_1h["val"].iloc[i])
                vah_j  = float(pre_1h["vah"].iloc[i])
                vwap_j = float(pre_1h["vwap"].iloc[i])
                atr_j  = float(pre_1h["atr"].iloc[i])
                close_j = float(bar["close"])
                sh_j   = float(pre_1h["swing_high"].iloc[i])
                sl_j   = float(pre_1h["swing_low"].iloc[i])

                if not any(pd.isna(x) for x in (val_j, vah_j, vwap_j, atr_j)) and atr_j > 0:
                    pg_dir = pending_g["direction"]
                    if pg_dir == "LONG":
                        level     = max(val_j, vwap_j)
                        confirmed = close_j > level
                    else:
                        level     = min(vah_j, vwap_j)
                        confirmed = close_j < level

                    if confirmed and i + 1 < n:
                        ng = df_1h.iloc[i + 1]
                        ep = float(ng["open"])
                        if pg_dir == "LONG":
                            sl  = min(sl_j, ep - ATR_SL_MULT * atr_j)
                            tp1 = ep + ATR_TP_MULT * atr_j
                            valid = sl < ep
                        else:
                            sl  = max(sh_j, ep + ATR_SL_MULT * atr_j)
                            tp1 = ep - ATR_TP_MULT * atr_j
                            valid = sl > ep
                        if valid:
                            n_signals += 1
                            open_trade = {
                                "symbol":       symbol,
                                "side":         pg_dir,
                                "entry_bar":    i + 1,
                                "entry_ts":     str(ng["timestamp"]),
                                "entry":        ep,
                                "sl":           round(sl, 8),
                                "tp1":          round(tp1, 8),
                                "htf_trend":    pending_g["htf_trend"],
                                "stf_trend":    pending_g["stf_trend"],
                                "trader_score": 90.0,
                            }
                        pending_g = None
            continue  # nikdy negeneruj nový signál ve stejném baru jako pending check

        # ── Varianta H: zkontroluj limit vstup (cena dosáhla cílové úrovně) ────────
        if signal_mode == "variant_h" and pending_h is not None:
            if i > pending_h["expires"]:
                pending_h = None
            else:
                ph_dir = pending_h["direction"]
                et     = pending_h["entry_target"]
                atr_j  = float(pre_1h["atr"].iloc[i])

                if not pd.isna(atr_j) and atr_j > 0:
                    low_i  = float(bar["low"])
                    high_i = float(bar["high"])
                    filled = (ph_dir == "LONG" and low_i <= et) or \
                             (ph_dir == "SHORT" and high_i >= et)

                    if filled:
                        ep = et   # limit fill at target price
                        if ph_dir == "LONG":
                            sl  = ep - ATR_SL_MULT * atr_j
                            tp1 = ep + ATR_TP_MULT * atr_j
                            valid = sl < ep
                        else:
                            sl  = ep + ATR_SL_MULT * atr_j
                            tp1 = ep - ATR_TP_MULT * atr_j
                            valid = sl > ep

                        if valid:
                            n_signals += 1
                            open_trade = {
                                "symbol":       symbol,
                                "side":         ph_dir,
                                "entry_bar":    i,
                                "entry_ts":     str(bar["timestamp"]),
                                "entry":        ep,
                                "sl":           round(sl, 8),
                                "tp1":          round(tp1, 8),
                                "htf_trend":    pending_h["htf_trend"],
                                "stf_trend":    pending_h["stf_trend"],
                                "trader_score": 90.0,
                            }
                        pending_h = None
            continue

        if signal_mode == "confluence":
            vwap_i = float(pre_1h["vwap"].iloc[i])
            poc_i  = float(pre_1h["poc"].iloc[i])
            direction   = _signal_confluence(
                htf_trend, stf_trend, rsi_val,
                float(bar["close"]), vwap_i, poc_i,
            )
            trader_score = 80.0  # fixní skóre: všech 5 podmínek splněno = silný setup
        elif signal_mode == "confluence_e":
            vwap_i     = float(pre_1h["vwap"].iloc[i])
            poc_i      = float(pre_1h["poc"].iloc[i])
            regime_i   = pre_1h["vol_regime"].iloc[i]
            bos_i      = pre_1h["last_bos"].iloc[i]
            bull_div_i = bool(pre_1h["bullish_div"].iloc[i])
            bear_div_i = bool(pre_1h["bearish_div"].iloc[i])
            direction  = _signal_confluence_e(
                htf_trend, stf_trend, rsi_val,
                float(bar["close"]), vwap_i, poc_i,
                regime_i, bos_i, bull_div_i, bear_div_i,
                rsi_lo=rsi_lo, rsi_hi=rsi_hi,
            )
            trader_score = 90.0  # fixní skóre: 8 podmínek splněno = velmi selektivní setup
        elif signal_mode == "variant_g":
            # Stejná E2 detekce, ale vstup se odkládá — pending_g místo přímého otevření
            vwap_i     = float(pre_1h["vwap"].iloc[i])
            poc_i      = float(pre_1h["poc"].iloc[i])
            regime_i   = pre_1h["vol_regime"].iloc[i]
            bos_i      = pre_1h["last_bos"].iloc[i]
            bull_div_i = bool(pre_1h["bullish_div"].iloc[i])
            bear_div_i = bool(pre_1h["bearish_div"].iloc[i])
            direction  = _signal_confluence_e(
                htf_trend, stf_trend, rsi_val,
                float(bar["close"]), vwap_i, poc_i,
                regime_i, bos_i, bull_div_i, bear_div_i,
                rsi_lo=rsi_lo, rsi_hi=rsi_hi,
            )
            if direction != "WAIT":
                pending_g = {
                    "direction":  direction,
                    "expires":    i + G_CONFIRM_WINDOW,
                    "htf_trend":  htf_trend,
                    "stf_trend":  stf_trend,
                }
            continue  # nikdy neotvírej obchod okamžitě; vždy čekáme na potvrzení
        elif signal_mode == "variant_h":
            # E2 (8 podmínek) + 9. podmínka: cena nad/pod Weekly Open
            vwap_i     = float(pre_1h["vwap"].iloc[i])
            poc_i      = float(pre_1h["poc"].iloc[i])
            regime_i   = pre_1h["vol_regime"].iloc[i]
            bos_i      = pre_1h["last_bos"].iloc[i]
            bull_div_i = bool(pre_1h["bullish_div"].iloc[i])
            bear_div_i = bool(pre_1h["bearish_div"].iloc[i])
            direction  = _signal_confluence_e(
                htf_trend, stf_trend, rsi_val,
                float(bar["close"]), vwap_i, poc_i,
                regime_i, bos_i, bull_div_i, bear_div_i,
                rsi_lo=rsi_lo, rsi_hi=rsi_hi,
            )

            # 9. podmínka: Weekly Open jako bias filtr
            if direction != "WAIT":
                _wo = pre_1h["tl_weekly_open"].iloc[i]
                if not pd.isna(_wo):
                    _close_i = float(bar["close"])
                    if direction == "LONG"  and _close_i <= float(_wo):
                        direction = "WAIT"
                    elif direction == "SHORT" and _close_i >= float(_wo):
                        direction = "WAIT"

            if direction != "WAIT":
                _close_i = float(bar["close"])
                _val     = float(pre_1h["val"].iloc[i])
                _vah     = float(pre_1h["vah"].iloc[i])

                def _tl_val(key):
                    v = pre_1h[key].iloc[i]
                    return float(v) if not pd.isna(v) else None

                _wo_v    = _tl_val("tl_weekly_open")
                _ml_v    = _tl_val("tl_monday_low")
                _moo_v   = _tl_val("tl_monthly_open")
                _wh_v    = _tl_val("tl_weekly_high")
                _mh_v    = _tl_val("tl_monday_high")
                _pwh_v   = _tl_val("tl_prev_week_high")

                # Entry target: ближайший support/resistance из time levels (макс. 5 % от цены)
                # Если нет кандидата — near-market fallback 1 %
                if direction == "LONG":
                    cands = [_val]
                    for v in (_wo_v, _ml_v, _moo_v):
                        if v is not None and v < _close_i and (_close_i - v) / _close_i <= 0.05:
                            cands.append(v)
                    et = max(cands)
                    if (_close_i - et) / _close_i > 0.05:
                        et = _close_i * 0.99
                else:
                    cands = [_vah]
                    for v in (_wh_v, _mh_v, _pwh_v):
                        if v is not None and v > _close_i and (v - _close_i) / _close_i <= 0.05:
                            cands.append(v)
                    et = min(cands)
                    if (et - _close_i) / _close_i > 0.05:
                        et = _close_i * 1.01

                pending_h = {
                    "direction":    direction,
                    "expires":      i + H_CONFIRM_WINDOW,
                    "entry_target": et,
                    "htf_trend":    htf_trend,
                    "stf_trend":    stf_trend,
                }
            continue  # nikdy neotvírej obchod okamžitě
        elif signal_mode == "variant_f":
            bos_i      = pre_1h["last_bos"].iloc[i]
            regime_i   = pre_1h["vol_regime"].iloc[i]
            bull_div_i = bool(pre_1h["bullish_div"].iloc[i])
            bear_div_i = bool(pre_1h["bearish_div"].iloc[i])
            mh_i  = float(pre_1h["macd_hist"].iloc[i]) if not pd.isna(pre_1h["macd_hist"].iloc[i]) else 0.0
            bb_i  = float(pre_1h["bb_pct_b"].iloc[i])  if not pd.isna(pre_1h["bb_pct_b"].iloc[i])  else 0.5
            cvd_i = float(pre_1h["cvd_norm"].iloc[i])

            f_score  = _score_f(htf_trend, stf_trend, bos_i, rsi_val,
                                 mh_i, bb_i, cvd_i, regime_i, bull_div_i, bear_div_i)
            if f_score >= score_threshold:
                direction = "LONG"
            elif f_score <= (100.0 - score_threshold):
                direction = "SHORT"
            else:
                direction = "WAIT"
            trader_score = f_score
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
        last        = df_1h.iloc[-1]
        entry       = open_trade["entry"]
        sl          = open_trade["sl"]
        hit_price   = float(last["close"])
        sl_dist_pct = abs(entry - sl) / entry if abs(entry - sl) > 1e-10 else 1e-6
        price_move  = (
            (hit_price - entry) / entry if open_trade["side"] == "LONG"
            else (entry - hit_price) / entry
        )
        pnl_r    = price_move / sl_dist_pct
        pnl_pct  = price_move * leverage * RISK_PER_TRADE
        fees_acc = 2.0 * leverage * RISK_PER_TRADE * (fees_pct / 100.0)
        net_pct  = pnl_pct - fees_acc
        equity.append(equity[-1] * (1.0 + net_pct))
        open_trade.update({
            "exit_bar":    n - 1,
            "exit_price":  round(hit_price, 8),
            "exit_reason": "EOD",
            "pnl_r":       round(pnl_r, 4),
            "pnl_pct":     round(net_pct * 100.0, 4),
            "bars_held":   n - 1 - open_trade["entry_bar"],
            "win":         False,
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
        "leverage":             leverage,
    }
