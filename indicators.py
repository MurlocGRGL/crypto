"""
Technické indikátory: RSI, VWAP, ATR, Ichimoku, Volume Profile,
RSI divergence, BTC korelace, MACD, Bollinger Bands.
Vstupem je vždy pandas DataFrame se sloupci open/high/low/close/volume.
"""

import numpy as np
import pandas as pd
import config


def rsi(df: pd.DataFrame, period: int = config.RSI_PERIOD) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_series = 100 - (100 / (1 + rs))
    return rsi_series.fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum()
    cum_vol_price = (typical_price * df["volume"]).cumsum()
    return cum_vol_price / cum_vol.replace(0, np.nan)


def ichimoku(df: pd.DataFrame):
    high, low, close = df["high"], df["low"], df["close"]
    tenkan = (high.rolling(config.ICHIMOKU_TENKAN).max() + low.rolling(config.ICHIMOKU_TENKAN).min()) / 2
    kijun = (high.rolling(config.ICHIMOKU_KIJUN).max() + low.rolling(config.ICHIMOKU_KIJUN).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(config.ICHIMOKU_KIJUN)
    senkou_b = (
        (high.rolling(config.ICHIMOKU_SENKOU_B).max() + low.rolling(config.ICHIMOKU_SENKOU_B).min()) / 2
    ).shift(config.ICHIMOKU_KIJUN)
    chikou = close.shift(-config.ICHIMOKU_KIJUN)
    return {"tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b, "chikou": chikou}


def ichimoku_signal(df: pd.DataFrame, ichi: dict) -> str:
    price = df["close"].iloc[-1]
    sa = ichi["senkou_a"].iloc[-1]
    sb = ichi["senkou_b"].iloc[-1]
    tenkan = ichi["tenkan"].iloc[-1]
    kijun = ichi["kijun"].iloc[-1]

    if pd.isna(sa) or pd.isna(sb):
        return "nedostatek dat"

    cloud_top, cloud_bottom = max(sa, sb), min(sa, sb)
    if price > cloud_top:
        position = "nad mrakem (bullish struktura)"
    elif price < cloud_bottom:
        position = "pod mrakem (bearish struktura)"
    else:
        position = "uvnitř mraku (nerozhodnuto / konsolidace)"

    if not pd.isna(tenkan) and not pd.isna(kijun):
        if tenkan > kijun:
            cross = "Tenkan > Kijun (bullish momentum)"
        elif tenkan < kijun:
            cross = "Tenkan < Kijun (bearish momentum)"
        else:
            cross = "Tenkan = Kijun"
    else:
        cross = "nedostatek dat"

    return f"{position}; {cross}"


def volume_profile(df: pd.DataFrame, bins: int = config.VOLUME_PROFILE_BINS):
    price_min = df["low"].min()
    price_max = df["high"].max()
    if price_max == price_min:
        return {"poc": price_max, "vah": price_max, "val": price_min}

    bin_edges = np.linspace(price_min, price_max, bins + 1)
    bin_volumes = np.zeros(bins)

    for _, row in df.iterrows():
        typical = (row["high"] + row["low"] + row["close"]) / 3
        idx = np.searchsorted(bin_edges, typical, side="right") - 1
        idx = min(max(idx, 0), bins - 1)
        bin_volumes[idx] += row["volume"]

    poc_idx = int(np.argmax(bin_volumes))
    poc_price = (bin_edges[poc_idx] + bin_edges[poc_idx + 1]) / 2

    total_volume = bin_volumes.sum()
    target = total_volume * config.VALUE_AREA_PCT
    acc = bin_volumes[poc_idx]
    lo, hi = poc_idx, poc_idx
    while acc < target and (lo > 0 or hi < bins - 1):
        left_vol = bin_volumes[lo - 1] if lo > 0 else -1
        right_vol = bin_volumes[hi + 1] if hi < bins - 1 else -1
        if right_vol >= left_vol:
            hi += 1
            acc += bin_volumes[hi]
        else:
            lo -= 1
            acc += bin_volumes[lo]

    return {"poc": poc_price, "vah": bin_edges[hi + 1], "val": bin_edges[lo]}


def _local_extrema(series: pd.Series, order: int = 3, min_distance: int = 5, min_amplitude_pct: float = 0.4):
    raw_highs, raw_lows = [], []
    vals = series.values
    for i in range(order, len(vals) - order):
        window = vals[i - order: i + order + 1]
        if vals[i] == window.max() and vals[i] != vals[i - 1]:
            raw_highs.append(i)
        if vals[i] == window.min() and vals[i] != vals[i - 1]:
            raw_lows.append(i)

    def _filter(indices):
        filtered = []
        for idx in indices:
            if not filtered:
                filtered.append(idx)
                continue
            last = filtered[-1]
            if idx - last < min_distance:
                continue
            pct_change = abs(vals[idx] - vals[last]) / max(abs(vals[last]), 1e-9) * 100
            if pct_change < min_amplitude_pct:
                continue
            filtered.append(idx)
        return filtered

    return _filter(raw_highs), _filter(raw_lows)


def rsi_divergence(df: pd.DataFrame, rsi_series: pd.Series, lookback: int = 60):
    window_close = df["close"].iloc[-lookback:].reset_index(drop=True)
    window_rsi = rsi_series.iloc[-lookback:].reset_index(drop=True)
    price_highs, price_lows = _local_extrema(window_close, order=3)
    result = "žádná zjevná divergence"

    if len(price_highs) >= 2:
        i1, i2 = price_highs[-2], price_highs[-1]
        if window_close[i2] > window_close[i1] and window_rsi[i2] < window_rsi[i1]:
            result = "BEARISH divergence (cena vyšší high, RSI nižší high)"

    if len(price_lows) >= 2:
        i1, i2 = price_lows[-2], price_lows[-1]
        if window_close[i2] < window_close[i1] and window_rsi[i2] > window_rsi[i1]:
            result = "BULLISH divergence (cena nižší low, RSI vyšší low)"

    return result


def correlation_with_btc(df_symbol: pd.DataFrame, df_btc: pd.DataFrame, window: int = 50):
    if df_symbol is None or df_btc is None:
        return None
    n = min(len(df_symbol), len(df_btc), window)
    if n < 10:
        return None
    ret_symbol = df_symbol["close"].pct_change().iloc[-n:]
    ret_btc = df_btc["close"].pct_change().iloc[-n:]
    corr = ret_symbol.reset_index(drop=True).corr(ret_btc.reset_index(drop=True))
    if pd.isna(corr):
        return None
    return float(corr)


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    """MACD – momentum indikátor (EMA12 - EMA26, signal EMA9, histogram)."""
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line

    m = float(macd_line.iloc[-1])
    s = float(signal_line.iloc[-1])
    h = float(histogram.iloc[-1])
    h_prev = float(histogram.iloc[-2]) if len(histogram) > 1 else h

    # Crossover = přechod histogramu přes nulu
    if h > 0 and h_prev <= 0:
        signal_text = "bullish crossover ▲"
    elif h < 0 and h_prev >= 0:
        signal_text = "bearish crossover ▼"
    elif m > s:
        signal_text = "nad signal (bullish)"
    else:
        signal_text = "pod signal (bearish)"

    return {
        "macd": m,
        "signal": s,
        "histogram": h,
        "histogram_growing": h > h_prev,
        "signal_text": signal_text,
    }


def bollinger_bands(df: pd.DataFrame, period: int = 20, std_mult: float = 2.0) -> dict:
    """Bollingerova pásma – SMA20 ± 2σ, pozice ceny v pásmu (%B)."""
    close = df["close"]
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std

    u = float(upper.iloc[-1])
    m = float(mid.iloc[-1])
    l = float(lower.iloc[-1])
    last = float(close.iloc[-1])

    if pd.isna(u) or pd.isna(l) or (u == l):
        return {"upper": None, "middle": None, "lower": None,
                "pct_b": None, "bandwidth": None, "signal_text": "N/A"}

    pct_b = (last - l) / (u - l)
    bandwidth = (u - l) / m if m else 0.0

    if pct_b > 1.0:
        signal_text = "nad horním pásmem"
    elif pct_b > 0.8:
        signal_text = "blízko horního pásma"
    elif pct_b < 0.0:
        signal_text = "pod dolním pásmem"
    elif pct_b < 0.2:
        signal_text = "blízko dolního pásma"
    else:
        signal_text = "uvnitř pásma"

    return {
        "upper": u,
        "middle": m,
        "lower": l,
        "pct_b": round(pct_b, 3),
        "bandwidth": round(bandwidth, 4),
        "signal_text": signal_text,
    }


def market_structure(df: pd.DataFrame, pivot_window: int = 5, lookback: int = 60,
                     event_lookback: int = 5) -> dict:
    """
    Detekuje market structure (BOS / CHoCH) z pivot high/low sekvence.

    BOS  (Break of Structure) = prolomení klíčové hladiny VE směru trendu → pokračování
    CHoCH (Change of Character) = prolomení PROTI trendu → potenciální otočení

    Vrací:
      structure: BULLISH | BEARISH | NEUTRAL
      event:     "BOS BULLISH" | "BOS BEARISH" | "CHoCH BULLISH" | "CHoCH BEARISH" | None
      event_candles_ago: int (0 = aktuální svíčka)
      swing_high: poslední pivot high
      swing_low:  poslední pivot low
    """
    n = len(df)
    pw = pivot_window
    lb = min(lookback, n - pw * 2 - 1)
    if lb < pw * 4:
        return None

    recent = df.iloc[-lb:].reset_index(drop=True)
    m = len(recent)

    # Pivot highs a lows (lokální extrémy s `pw` svíčkami potvrzení na obou stranách)
    highs, lows = [], []
    for i in range(pw, m - pw):
        h = recent["high"].iloc[i]
        l = recent["low"].iloc[i]
        if h == recent["high"].iloc[i - pw : i + pw + 1].max():
            highs.append((i, float(h)))
        if l == recent["low"].iloc[i - pw : i + pw + 1].min():
            lows.append((i, float(l)))

    if len(highs) < 2 or len(lows) < 2:
        return {"event": None, "structure": "NEUTRAL",
                "swing_high": None, "swing_low": None}

    # Poslední 2 pivoty každého typu
    (_, last_h), (_, prev_h) = highs[-1], highs[-2]
    (_, last_l), (_, prev_l) = lows[-1],  lows[-2]

    # Klasifikace struktury z posledního páru
    hh = last_h > prev_h
    hl = last_l > prev_l
    lh = last_h < prev_h
    ll = last_l < prev_l

    if hh and hl:
        structure = "BULLISH"
    elif lh and ll:
        structure = "BEARISH"
    elif hh or hl:
        structure = "BULLISH"
    elif lh or ll:
        structure = "BEARISH"
    else:
        structure = "NEUTRAL"

    # Hledáme nejčerstvější BOS/CHoCH v posledních `event_lookback` svíčkách
    event, key_level, candles_ago = None, None, 0
    for offset in range(event_lookback):
        idx = -(1 + offset)
        c = float(recent["close"].iloc[idx])

        if structure == "BULLISH":
            if c > last_h:
                event, key_level = "BOS BULLISH", last_h
            elif c < last_l:
                event, key_level = "CHoCH BEARISH", last_l
        elif structure == "BEARISH":
            if c < last_l:
                event, key_level = "BOS BEARISH", last_l
            elif c > last_h:
                event, key_level = "CHoCH BULLISH", last_h

        if event:
            candles_ago = offset
            break

    return {
        "event": event,
        "event_candles_ago": candles_ago,
        "key_level": round(key_level, 6) if key_level else None,
        "structure": structure,
        "swing_high": round(last_h, 6),
        "swing_low":  round(last_l, 6),
    }


def volatility_regime(df: pd.DataFrame, atr_period: int = 14,
                      bb_period: int = 20, bb_mult: float = 2.0,
                      avg_window: int = 40) -> dict:
    """
    Klasifikuje volatilitní režim: TRENDING | RANGING | MIXED.

    Porovnává aktuální ATR a šířku Bollingerových pásem vůči jejich klouzavému průměru.
    Výsledek se zobrazuje jako filtr "dává tento setup smysl v aktuálním režimu?".
    """
    n = len(df)
    if n < avg_window + bb_period:
        return None

    # ATR ratio
    atr_s = atr(df, period=atr_period)
    atr_avg = atr_s.rolling(avg_window).mean()
    if pd.isna(atr_s.iloc[-1]) or pd.isna(atr_avg.iloc[-1]) or atr_avg.iloc[-1] == 0:
        return None
    atr_ratio = float(atr_s.iloc[-1] / atr_avg.iloc[-1])

    # BB bandwidth ratio
    sma = df["close"].rolling(bb_period).mean()
    std = df["close"].rolling(bb_period).std()
    bw  = ((sma + bb_mult * std) - (sma - bb_mult * std)) / sma.replace(0, np.nan)
    bw_avg = bw.rolling(avg_window).mean()
    if pd.isna(bw.iloc[-1]) or pd.isna(bw_avg.iloc[-1]) or bw_avg.iloc[-1] == 0:
        return None
    bw_ratio = float(bw.iloc[-1] / bw_avg.iloc[-1])

    score = (atr_ratio + bw_ratio) / 2.0
    if score > 1.20:
        regime = "TRENDING"
    elif score < 0.85:
        regime = "RANGING"
    else:
        regime = "MIXED"

    return {
        "regime": regime,
        "atr_ratio": round(atr_ratio, 2),
        "bw_ratio":  round(bw_ratio, 2),
        "score":     round(score, 2),
    }


def time_based_levels(df: pd.DataFrame) -> dict:
    """
    Vypočítá klíčové časové cenové hladiny z 1H OHLCV dat.

    Vrací slovník:
      weekly_open/high/low      — aktuální týden (pondělí 00:00 UTC)
      monday_open/high/low      — pondělí aktuálního týdne
      prev_week_high/low        — předchozí týden
      monthly_open              — první bar aktuálního měsíce
      prev_month_high           — maximum předchozího měsíce

    Timestamps mohou být timezone-naive (UTC) nebo tz-aware — oba formáty zpracujeme.
    """
    if df is None or df.empty:
        return {}

    from datetime import timedelta

    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    else:
        ts = ts.dt.tz_convert("UTC")

    now = ts.iloc[-1]
    # Monday 00:00 UTC of current week
    days_back = now.dayofweek          # 0 = Monday
    week_start = (now - pd.Timedelta(days=days_back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prev_week_start = week_start - pd.Timedelta(days=7)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 1:
        prev_month_start = month_start.replace(year=month_start.year - 1, month=12)
    else:
        prev_month_start = month_start.replace(month=month_start.month - 1)

    def _mask(start, end):
        return (ts >= pd.Timestamp(start)) & (ts < pd.Timestamp(end))

    curr_week   = _mask(week_start,      now + pd.Timedelta(hours=1))
    monday_only = curr_week & (ts.dt.dayofweek == 0)
    prev_week   = _mask(prev_week_start, week_start)
    curr_month  = _mask(month_start,     now + pd.Timedelta(hours=1))
    prev_month  = _mask(prev_month_start, month_start)

    def _open(m):
        s = df.loc[m]
        return float(s["open"].iloc[0]) if not s.empty else None

    def _high(m):
        s = df.loc[m]
        return float(s["high"].max()) if not s.empty else None

    def _low(m):
        s = df.loc[m]
        return float(s["low"].min()) if not s.empty else None

    return {
        "weekly_open":     _open(curr_week),
        "weekly_high":     _high(curr_week),
        "weekly_low":      _low(curr_week),
        "monday_open":     _open(monday_only),
        "monday_high":     _high(monday_only),
        "monday_low":      _low(monday_only),
        "prev_week_high":  _high(prev_week),
        "prev_week_low":   _low(prev_week),
        "monthly_open":    _open(curr_month),
        "prev_month_high": _high(prev_month),
    }


def analyze_timeframe(df: pd.DataFrame, exclude_last: bool = True) -> dict:
    """
    Spočítá všechny indikátory pro jeden timeframe a vrátí shrnutí.

    exclude_last=True  (default, live): odřízne formující se svíčku (df.iloc[:-1]).
    exclude_last=False (backtest):      všechny předané svíčky jsou uzavřené — nic neodřezávej.
                                        Volající zajistí, že df neobsahuje budoucí data.
    """
    if df is None or len(df) < config.ICHIMOKU_SENKOU_B + config.ICHIMOKU_KIJUN + 1:
        return None
    if exclude_last:
        df = df.iloc[:-1].copy()
    else:
        df = df.copy()

    rsi_series = rsi(df)
    vwap_series = vwap(df)
    atr_series = atr(df)
    ichi = ichimoku(df)
    vp = volume_profile(df)
    divergence = rsi_divergence(df, rsi_series)
    macd_res = macd(df)
    bb_res = bollinger_bands(df)
    ms = market_structure(df)
    vr = volatility_regime(df)

    last_close = df["close"].iloc[-1]
    last_rsi = rsi_series.iloc[-1]
    last_vwap = vwap_series.iloc[-1]
    last_atr = atr_series.iloc[-1] if not pd.isna(atr_series.iloc[-1]) else (df["high"] - df["low"]).mean()

    return {
        "last_close": last_close,
        "rsi": last_rsi,
        "vwap": last_vwap,
        "atr": last_atr,
        "price_vs_vwap": "nad VWAP" if last_close > last_vwap else "pod VWAP",
        "ichimoku_text": ichimoku_signal(df, ichi),
        "ichimoku_raw": ichi,
        "volume_profile": vp,
        "divergence": divergence,
        "candle_count": len(df),
        "swing_high": df["high"].iloc[-20:].max(),
        "swing_low": df["low"].iloc[-20:].min(),
        "macd": macd_res,
        "bb": bb_res,
        "market_structure": ms,
        "volatility_regime": vr,
    }
