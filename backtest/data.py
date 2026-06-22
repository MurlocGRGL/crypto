"""
Stahování a cachování historických OHLCV dat pro backtest.
Cache: backtest_data/{SYMBOL}_{tf}.csv  (aktualizuje se přírůstkově)
"""

import time
from pathlib import Path

import ccxt
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "backtest_data"
DATA_DIR.mkdir(exist_ok=True)

# Alternativní ticker pro Binance (HYPE nemusí být dostupný všude)
_EXCHANGE_FALLBACK = {
    "HYPE/USDT": "bybit",
}


def _cache_path(symbol: str, tf: str) -> Path:
    return DATA_DIR / f"{symbol.replace('/', '_')}_{tf}.csv"


def _make_exchange(exchange_id: str = "binance") -> ccxt.Exchange:
    cls = getattr(ccxt, exchange_id)
    return cls({"enableRateLimit": True})


def _fetch_full(exchange: ccxt.Exchange, symbol: str, tf: str, since_ms: int) -> pd.DataFrame:
    all_candles: list = []
    current = since_ms
    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, tf, since=current, limit=1000)
        except Exception as exc:
            print(f"\n  WARN fetch {symbol} {tf}: {exc}")
            break
        if not candles:
            break
        all_candles.extend(candles)
        if len(candles) < 1000:
            break
        current = candles[-1][0] + 1
        time.sleep(0.25)
    if not all_candles:
        return pd.DataFrame()
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def load_or_fetch(symbol: str, tf: str, years: int = 3) -> pd.DataFrame:
    """Načte data z CSV cache nebo stáhne z burzy a uloží."""
    cache = _cache_path(symbol, tf)
    exc_id = _EXCHANGE_FALLBACK.get(symbol, "binance")
    exchange = _make_exchange(exc_id)

    since_dt = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=years)
    since_ms = int(since_dt.timestamp() * 1000)

    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["timestamp"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        last_ts = df["timestamp"].iloc[-1]
        fetch_from = int(last_ts.timestamp() * 1000) + 1
        print(f"  Cache: {symbol} {tf} ({len(df)} svíček, aktualizace od {last_ts.strftime('%Y-%m-%d')})")
        new_df = _fetch_full(exchange, symbol, tf, fetch_from)
        if not new_df.empty:
            df = pd.concat([df, new_df], ignore_index=True)
            df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    else:
        print(f"  Stahování: {symbol} {tf} (od {since_dt.strftime('%Y-%m-%d')})...", end="", flush=True)
        df = _fetch_full(exchange, symbol, tf, since_ms)
        print(f" {len(df)} svíček")

    if not df.empty:
        df.to_csv(cache, index=False)
    return df


def load_all(symbols: list[str], timeframes: list[str], years: int = 3) -> dict:
    """Vrátí {symbol: {tf: DataFrame}} pro všechny symboly a timeframy."""
    result: dict = {}
    for symbol in symbols:
        result[symbol] = {}
        for tf in timeframes:
            df = load_or_fetch(symbol, tf, years=years)
            result[symbol][tf] = df if not df.empty else None
            if df is None or df.empty:
                print(f"  WARN: žádná data pro {symbol} {tf}")
    return result
