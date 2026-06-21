"""
Technické indikátory: RSI, VWAP, Ichimoku, Volume Profile, RSI divergence.
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
    rsi_series = rsi_series.fillna(50)
    return rsi_series


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range - měřítko volatility, použité pro adaptivní SL."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP počítané přes celé stažené okno (anchored na první svíčku v datasetu)."""
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

    return {
        "tenkan": tenkan,
        "kijun": kijun,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou": chikou,
    }


def ichimoku_signal(df: pd.DataFrame, ichi: dict) -> str:
    """Zjednodušené čtení mraku: cena nad/pod/v mraku + tenkan/kijun cross."""
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
    """
    Rozdělí cenové rozpětí okna do binů, sečte objem v každém binu,
    najde POC (Point of Control) a Value Area (VAH/VAL).
    """
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

    # Value area - rozšiřujeme od POC, dokud nemáme VALUE_AREA_PCT objemu
    total_volume = bin_volumes.sum()
    target = total_volume * config.VALUE_AREA_PCT
    included = {poc_idx}
    acc = bin_volumes[poc_idx]
    lo, hi = poc_idx, poc_idx
    while acc < target and (lo > 0 or hi < bins - 1):
        left_vol = bin_volumes[lo - 1] if lo > 0 else -1
        right_vol = bin_volumes[hi + 1] if hi < bins - 1 else -1
        if right_vol >= left_vol:
            hi += 1
            acc += bin_volumes[hi]
            included.add(hi)
        else:
            lo -= 1
            acc += bin_volumes[lo]
            included.add(lo)

    vah = bin_edges[hi + 1]
    val = bin_edges[lo]

    return {"poc": poc_price, "vah": vah, "val": val}


def _local_extrema(series: pd.Series, order: int = 3, min_distance: int = 5, min_amplitude_pct: float = 0.4):
    """
    Najde indexy lokálních maxim a minim (swing detektor).
    Filtruje šum: po sobě jdoucí piloty musí být aspoň `min_distance` svíček
    od sebe a lišit se o aspoň `min_amplitude_pct` % ceny, jinak se ignorují.
    """
    raw_highs, raw_lows = [], []
    vals = series.values
    for i in range(order, len(vals) - order):
        window = vals[i - order : i + order + 1]
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
    """
    Zjednodušená detekce RSI divergence na posledních `lookback` svíčkách.
    Porovná poslední dva swing highy / lowy v ceně vs. RSI.
    """
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


def correlation_with_btc(df_symbol: pd.DataFrame, df_btc: pd.DataFrame, window: int = 50) -> float:
    """
    Rolling Pearson korelace výnosů (returns) altcoinu vůči BTC za posledních `window` svíček.
    Vrací hodnotu -1..1 (None pokud nelze spočítat).
    """
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


def analyze_timeframe(df: pd.DataFrame) -> dict:
    """Spočítá všechny indikátory pro jeden timeframe a vrátí shrnutí."""
    if df is None or len(df) < config.ICHIMOKU_SENKOU_B + config.ICHIMOKU_KIJUN:
        return None

    rsi_series = rsi(df)
    vwap_series = vwap(df)
    atr_series = atr(df)
    ichi = ichimoku(df)
    vp = volume_profile(df)
    divergence = rsi_divergence(df, rsi_series)

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
    }
