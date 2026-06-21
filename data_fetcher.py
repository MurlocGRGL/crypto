"""
Stahování OHLCV dat z burz pomocí ccxt, s automatickým fallbackem,
a nová endpoint volání pro L/S ratio, OI historii a Fear & Greed.
"""

import json
import urllib.request

import ccxt
import pandas as pd

import config


class DataFetcher:
    def __init__(self):
        self._exchanges = {}
        self._symbol_exchange_cache = {}

    def _get_exchange(self, exchange_id):
        if exchange_id not in self._exchanges:
            exchange_class = getattr(ccxt, exchange_id)
            self._exchanges[exchange_id] = exchange_class({"enableRateLimit": True})
        return self._exchanges[exchange_id]

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        candidates = config.EXCHANGES_PRIORITY
        cached = self._symbol_exchange_cache.get(symbol)
        if cached:
            candidates = [cached] + [e for e in candidates if e != cached]

        last_error = None
        for exchange_id in candidates:
            try:
                exchange = self._get_exchange(exchange_id)
                raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                if not raw:
                    continue
                df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                self._symbol_exchange_cache[symbol] = exchange_id
                df.attrs["exchange"] = exchange_id
                return df
            except Exception as e:
                last_error = e
                continue

        raise RuntimeError(
            f"Nepodařilo se stáhnout {symbol} {timeframe} z žádné burzy "
            f"({candidates}). Poslední chyba: {last_error}"
        )

    def fetch_all(self, symbols, timeframes, limit=300):
        data = {}
        for symbol in symbols:
            data[symbol] = {}
            for tf in timeframes:
                try:
                    data[symbol][tf] = self.fetch_ohlcv(symbol, tf, limit=limit)
                except Exception as e:
                    print(f"[WARN] {symbol} {tf}: {e}")
                    data[symbol][tf] = None
        return data

    def fetch_funding_and_oi(self, symbol: str):
        """Aktuální funding rate + open interest (best-effort)."""
        funding_rate, open_interest = None, None
        for exchange_id, sym in [("binanceusdm", symbol), ("bybit", symbol), ("okx", symbol)]:
            try:
                exchange = self._get_exchange(exchange_id)
                if hasattr(exchange, "fetch_funding_rate"):
                    fr = exchange.fetch_funding_rate(sym)
                    funding_rate = fr.get("fundingRate")
                if hasattr(exchange, "fetch_open_interest"):
                    oi = exchange.fetch_open_interest(sym)
                    open_interest = oi.get("openInterestAmount") or oi.get("openInterestValue")
                if funding_rate is not None or open_interest is not None:
                    break
            except Exception:
                continue
        return funding_rate, open_interest

    def fetch_long_short_ratio(self, symbol: str):
        """
        Top-trader long/short ratio z Binance Futures (veřejný endpoint, bez API klíče).
        Vrací (long_pct, short_pct) zaokrouhleně na 1 des., nebo (None, None).
        """
        sym = symbol.replace("/", "")
        try:
            url = (
                "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
                f"?symbol={sym}&period=1h&limit=1"
            )
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            if data:
                long_pct = round(float(data[0]["longAccount"]) * 100, 1)
                short_pct = round(float(data[0]["shortAccount"]) * 100, 1)
                return long_pct, short_pct
        except Exception:
            pass
        return None, None

    def fetch_oi_history(self, symbol: str, period: str = "1h", limit: int = 24):
        """
        OI historie z Binance Futures (posledních `limit` hodin).
        Vrací {"current_usd": float, "change_24h_pct": float} nebo None.
        """
        sym = symbol.replace("/", "")
        try:
            url = (
                "https://fapi.binance.com/futures/data/openInterestHist"
                f"?symbol={sym}&period={period}&limit={limit}"
            )
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            if data and len(data) >= 2:
                oi_now = float(data[-1]["sumOpenInterestValue"])
                oi_ago = float(data[0]["sumOpenInterestValue"])
                change_pct = (oi_now - oi_ago) / oi_ago * 100 if oi_ago else 0.0
                return {"current_usd": oi_now, "change_24h_pct": round(change_pct, 2)}
        except Exception:
            pass
        return None

    @staticmethod
    def fetch_fear_greed():
        """
        Crypto Fear & Greed Index z alternative.me (globální, není per-coin).
        Vrací {"value": int, "label": str} nebo None.
        """
        try:
            url = "https://api.alternative.me/fng/?limit=1"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            entry = data["data"][0]
            return {"value": int(entry["value"]), "label": entry["value_classification"]}
        except Exception:
            return None
