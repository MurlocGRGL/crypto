"""
Stahování OHLCV dat z burz pomocí ccxt, s automatickým fallbackem,
pokud pár na dané burze neexistuje (typicky se to může stát u HYPE).
"""

import ccxt
import pandas as pd
import config


class DataFetcher:
    def __init__(self):
        self._exchanges = {}
        # symbol -> exchange_id, který se osvědčil naposledy (cache, ať to nezkoušíme pořád dokola)
        self._symbol_exchange_cache = {}

    def _get_exchange(self, exchange_id):
        if exchange_id not in self._exchanges:
            exchange_class = getattr(ccxt, exchange_id)
            self._exchanges[exchange_id] = exchange_class({"enableRateLimit": True})
        return self._exchanges[exchange_id]

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        """
        Vrátí DataFrame se sloupci: timestamp, open, high, low, close, volume
        Zkusí burzy v pořadí podle config.EXCHANGES_PRIORITY, dokud jedna nezabere.
        """
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
                df = pd.DataFrame(
                    raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
                )
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
        """
        Vrátí dict: {symbol: {timeframe: DataFrame}}
        """
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
        """
        Best-effort: zkusí stáhnout funding rate a open interest z perpetual futures.
        Vrací (funding_rate, open_interest) - kterékoliv může být None, pokud se nepodaří.
        Nikdy nevyhazuje výjimku navenek, jen tiše vrátí None.
        """
        funding_rate, open_interest = None, None
        candidates = [
            ("binanceusdm", symbol),
            ("bybit", symbol),
            ("okx", symbol),
        ]
        for exchange_id, sym in candidates:
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

