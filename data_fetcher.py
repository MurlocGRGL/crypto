"""
Stahování OHLCV dat z burz pomocí ccxt a derivátová data z veřejných endpointů.

Tier 0: OHLCV, funding rate, OI snapshot (ccxt)
Tier 1: L/S ratio, OI history, Fear & Greed (Binance + alternative.me)
Tier 2: Futures basis, CVD, Options (Binance + Deribit)
"""

import json
import time as _time
import urllib.request
from datetime import datetime

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

    # ── OHLCV ─────────────────────────────────────────────────────────────────

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

    # ── Tier 0 deriváty ───────────────────────────────────────────────────────

    def fetch_funding_and_oi(self, symbol: str):
        """Aktuální funding rate + open interest (best-effort přes ccxt)."""
        funding_rate, open_interest = None, None
        for exchange_id in ("binanceusdm", "bybit", "okx"):
            try:
                exchange = self._get_exchange(exchange_id)
                if hasattr(exchange, "fetch_funding_rate"):
                    fr = exchange.fetch_funding_rate(symbol)
                    funding_rate = fr.get("fundingRate")
                if hasattr(exchange, "fetch_open_interest"):
                    oi = exchange.fetch_open_interest(symbol)
                    open_interest = oi.get("openInterestAmount") or oi.get("openInterestValue")
                if funding_rate is not None or open_interest is not None:
                    break
            except Exception:
                continue
        return funding_rate, open_interest

    # ── Tier 1 ────────────────────────────────────────────────────────────────

    def fetch_long_short_ratio(self, symbol: str):
        """
        Top-trader long/short ratio z Binance Futures (veřejný endpoint, bez API klíče).
        Vrací (long_pct, short_pct) nebo (None, None).
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
                return (
                    round(float(data[0]["longAccount"]) * 100, 1),
                    round(float(data[0]["shortAccount"]) * 100, 1),
                )
        except Exception:
            pass
        return None, None

    def fetch_oi_history(self, symbol: str, period: str = "1h", limit: int = 24):
        """
        OI historie z Binance Futures – aktuální OI + 24h změna %.
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
                change = (oi_now - oi_ago) / oi_ago * 100 if oi_ago else 0.0
                return {"current_usd": oi_now, "change_24h_pct": round(change, 2)}
        except Exception:
            pass
        return None

    @staticmethod
    def fetch_fear_greed():
        """Crypto Fear & Greed Index z alternative.me (globální, 0–100)."""
        try:
            with urllib.request.urlopen("https://api.alternative.me/fng/?limit=1", timeout=5) as r:
                entry = json.loads(r.read())["data"][0]
            return {"value": int(entry["value"]), "label": entry["value_classification"]}
        except Exception:
            return None

    # ── Tier 2 ────────────────────────────────────────────────────────────────

    def fetch_futures_basis(self, symbol: str):
        """
        Binance perpetual mark price vs index (spot basket).
        Basis > 0 %: perpetuál dražší → longy platí prémii (bullish leverage bias).
        Basis < 0 %: perpetuál levnější → bearish bias.
        Vrací {"mark_price", "index_price", "basis_pct"} nebo None.
        """
        sym = symbol.replace("/", "")
        try:
            url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            mark = float(data["markPrice"])
            idx = float(data["indexPrice"])
            if idx > 0:
                return {
                    "mark_price": mark,
                    "index_price": idx,
                    "basis_pct": round((mark - idx) / idx * 100, 4),
                }
        except Exception:
            pass
        return None

    def fetch_cvd(self, symbol: str, lookback_seconds: int = 3600):
        """
        Cumulative Volume Delta z Binance Futures aggTrades za posledních ~1h.
        Taker buy (agresivní nákup) vs taker sell → delta = net buying pressure.
        Vrací {"buy_usd", "sell_usd", "delta_usd", "delta_pct"} nebo None.
        """
        sym = symbol.replace("/", "")
        try:
            start_ts = int((_time.time() - lookback_seconds) * 1000)
            url = (
                f"https://fapi.binance.com/fapi/v1/aggTrades"
                f"?symbol={sym}&startTime={start_ts}&limit=1000"
            )
            with urllib.request.urlopen(url, timeout=8) as r:
                trades = json.loads(r.read())
            if not trades:
                return None

            buy_usd = sell_usd = 0.0
            for t in trades:
                usd = float(t["q"]) * float(t["p"])
                if t["m"]:   # isBuyerMaker=True → seller was taker → aggressive sell
                    sell_usd += usd
                else:
                    buy_usd += usd

            total = buy_usd + sell_usd
            delta = buy_usd - sell_usd
            return {
                "buy_usd": round(buy_usd),
                "sell_usd": round(sell_usd),
                "delta_usd": round(delta),
                "delta_pct": round(delta / total * 100, 1) if total > 0 else 0.0,
            }
        except Exception:
            pass
        return None

    def fetch_options_data(self, symbol: str):
        """
        Options data z Deribit (jen BTC a ETH mají likvidní opční trh).
        Vrací put/call OI ratio, P/C volume ratio, přibližnou ATM IV.
        """
        currency = symbol.replace("/USDT", "")
        if currency not in ("BTC", "ETH"):
            return None

        import re

        def _parse_deribit_date(s):
            m = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$', s)
            if not m:
                return None
            d, mo, y = m.groups()
            try:
                return datetime.strptime(f"{d.zfill(2)}{mo}{y}", "%d%b%y")
            except ValueError:
                return None

        try:
            # 1) Aktuální index cena (pro výběr ATM strike)
            with urllib.request.urlopen(
                f"https://www.deribit.com/api/v2/public/get_index_price"
                f"?index_name={currency.lower()}_usd",
                timeout=5,
            ) as r:
                index_price = float(json.loads(r.read())["result"]["index_price"])

            # 2) Přehled všech aktivních opcí
            with urllib.request.urlopen(
                f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
                f"?currency={currency}&kind=option",
                timeout=10,
            ) as r:
                instruments = json.loads(r.read()).get("result", [])

            if not instruments:
                return None

            # Nejbližší budoucí expirace
            now = datetime.utcnow()
            dates = {}
            for inst in instruments:
                parts = inst["instrument_name"].split("-")
                if len(parts) == 4:
                    dt = _parse_deribit_date(parts[1])
                    if dt and dt > now:
                        dates[parts[1]] = dt
            near_exp = min(dates, key=lambda k: dates[k], default=None)

            call_oi = put_oi = call_vol = put_vol = 0.0
            atm_ivs = []

            for inst in instruments:
                parts = inst["instrument_name"].split("-")
                if len(parts) != 4:
                    continue
                exp_str, strike_str, opt_type = parts[1], parts[2], parts[3]
                try:
                    strike = float(strike_str)
                except ValueError:
                    continue

                oi = float(inst.get("open_interest", 0) or 0)
                vol = float(inst.get("volume", 0) or 0)
                iv = float(inst.get("mark_iv", 0) or 0)

                if opt_type == "C":
                    call_oi += oi
                    call_vol += vol
                elif opt_type == "P":
                    put_oi += oi
                    put_vol += vol

                # ATM IV: nejbližší expirace, strike do 10 % od indexu
                if exp_str == near_exp and iv > 1.0 and abs(strike - index_price) / index_price < 0.10:
                    atm_ivs.append(iv)

            return {
                "pc_ratio_oi": round(put_oi / call_oi, 2) if call_oi > 0 else None,
                "pc_ratio_vol": round(put_vol / call_vol, 2) if call_vol > 0 else None,
                "atm_iv": round(sum(atm_ivs) / len(atm_ivs), 1) if atm_ivs else None,
                "near_expiry": near_exp,
            }
        except Exception:
            pass
        return None
