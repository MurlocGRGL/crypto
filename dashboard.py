"""
Live web dashboard.

Spuštění: python dashboard.py
Otevři prohlížeč na http://localhost:5000

Architektura aktualizací:
  - Cena (live_prices): každých PRICE_REFRESH_SECONDS sekund — pouze ticker, žádné indikátory
  - Indikátory + závěr (analyses): po uzavření 15m svíčky + CANDLE_SETTLE_SECS buffer
    (nikdy se nepřepočítává na rozpracované svíčce)
  - Stránka si data tahá každých 15s přes /api/data
"""

import math
import time
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template

import config
from data_fetcher import DataFetcher
from indicators import analyze_timeframe, correlation_with_btc
from liquidation_collector import start_collector, get_liq_summary
from report_generator import build_symbol_analysis, _trend_from_ichimoku_text

app = Flask(__name__)
start_collector()   # WebSocket stream !forceOrder@arr, ukládá do liquidations.db

_lock = threading.Lock()
_state = {
    "analyses": [],
    "live_prices": {},          # {symbol: {price, change_24h_pct}} — fast ticker
    "timestamp": None,          # čas poslední analýzy
    "next_price_ts": None,      # Unix ts příští aktualizace ceny
    "next_analysis_ts": None,   # Unix ts příšího triggeru analýzy (nejbližší 15m close)
    "status": "starting",
    "error_msg": None,
}
_fetcher = DataFetcher()

# Délky timeframe v sekundách (svíčky jsou zarovnané na Unix epochu)
_TF_SECS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _to_python(obj):
    """Rekurzivně převede numpy/pandas typy na plain Python pro JSON."""
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {k: _to_python(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_python(x) for x in obj]
    return obj


def _run_full_analysis() -> list:
    """Stáhne OHLCV, spočítá indikátory a derivátová data pro všechny symboly."""
    raw_data = _fetcher.fetch_all(config.SYMBOLS, config.TIMEFRAMES, limit=config.CANDLE_LIMIT)

    analyzed = {}
    for symbol, tf_dict in raw_data.items():
        analyzed[symbol] = {tf: analyze_timeframe(df) for tf, df in tf_dict.items()}

    btc_trend = None
    btc_df_1h = raw_data.get("BTC/USDT", {}).get("1h")
    if "BTC/USDT" in analyzed:
        btc_tf = analyzed["BTC/USDT"].get("4h") or analyzed["BTC/USDT"].get("1d")
        if btc_tf:
            btc_trend = _trend_from_ichimoku_text(btc_tf["ichimoku_text"])

    fear_greed = _fetcher.fetch_fear_greed()

    analyses = []
    for symbol in config.SYMBOLS:
        corr = None
        if symbol != "BTC/USDT" and btc_df_1h is not None:
            corr = correlation_with_btc(raw_data.get(symbol, {}).get("1h"), btc_df_1h)

        funding_rate, open_interest = _fetcher.fetch_funding_and_oi(symbol)
        ls_long, ls_short = _fetcher.fetch_long_short_ratio(symbol)
        oi_history = _fetcher.fetch_oi_history(symbol)
        basis = _fetcher.fetch_futures_basis(symbol)
        cvd = _fetcher.fetch_cvd(symbol)
        options_data = _fetcher.fetch_options_data(symbol)
        liquidations = get_liq_summary(symbol)

        a = build_symbol_analysis(
            symbol,
            analyzed.get(symbol, {}),
            btc_trend=btc_trend,
            correlation_btc=corr,
            funding_rate=funding_rate,
            open_interest=open_interest,
            ls_long=ls_long,
            ls_short=ls_short,
            oi_history=oi_history,
            fear_greed=fear_greed,
            basis=basis,
            cvd=cvd,
            options_data=options_data,
            liquidations=liquidations,
        )
        analyses.append(a)

    return analyses


def _next_15m_close(now: float) -> float:
    """Unix ts nejbližšího příštího uzavření 15m svíčky."""
    return math.ceil(now / 900) * 900


def background_loop():
    """
    Smyčka řízená událostmi:
      - Spí přesně do příštího relevantního eventu (cena nebo uzavření svíčky).
      - Analýza se spustí jen po uzavření 15m svíčky + CANDLE_SETTLE_SECS buffer.
      - Cena se aktualizuje každých PRICE_REFRESH_SECONDS (rychlý ticker, bez OHLCV).
    """
    # Inicializace: zaznamenáme aktuální uzavřené svíčky jako "již zpracované",
    # takže se hned nespustí analýza — necháme proběhnout první explicitní cyklus níže.
    now = time.time()
    _last_analyzed_close = {tf: math.floor(now / secs) * secs for tf, secs in _TF_SECS.items()}
    _last_price_ts = 0.0   # 0 = hned při startu aktualizuj cenu
    first_run = True

    while True:
        now = time.time()

        # ── Zjisti, co je potřeba udělat ─────────────────────────────────────
        price_needed = (now - _last_price_ts) >= config.PRICE_REFRESH_SECONDS

        # Analýza: hledáme TF, jehož svíčka se uzavřela a ještě nebyla zpracována.
        # Čekáme CANDLE_SETTLE_SECS, aby burza stihla data finalizovat.
        newly_closed = {}
        for tf, secs in _TF_SECS.items():
            last_close = math.floor(now / secs) * secs
            settle_ok = now >= last_close + config.CANDLE_SETTLE_SECS
            if last_close > _last_analyzed_close[tf] and settle_ok:
                newly_closed[tf] = last_close

        analysis_needed = first_run or bool(newly_closed)

        # ── Provádíme akce ───────────────────────────────────────────────────
        if analysis_needed:
            closed_str = ", ".join(newly_closed.keys()) if newly_closed else "init"
            print(f"[{datetime.now().isoformat(timespec='seconds')}] "
                  f"Analyza (trigger: {closed_str})...")
            try:
                analyses = _run_full_analysis()
                prices = _fetcher.fetch_live_prices(config.SYMBOLS)
                now_ts = datetime.now()
                for tf, close_ts in newly_closed.items():
                    _last_analyzed_close[tf] = close_ts
                if first_run:
                    for tf, secs in _TF_SECS.items():
                        _last_analyzed_close[tf] = math.floor(time.time() / secs) * secs
                with _lock:
                    _state["analyses"] = _to_python(analyses)
                    _state["live_prices"] = prices
                    _state["timestamp"] = now_ts.strftime("%Y-%m-%d %H:%M:%S")
                    _state["status"] = "ok"
                    _state["error_msg"] = None
                _last_price_ts = time.time()
                first_run = False
                print("Hotovo.")
            except Exception:
                err = traceback.format_exc()
                print(f"[CHYBA]\n{err}")
                with _lock:
                    _state["status"] = "error"
                    _state["error_msg"] = err.splitlines()[-1]
                first_run = False

        elif price_needed:
            try:
                prices = _fetcher.fetch_live_prices(config.SYMBOLS)
                with _lock:
                    _state["live_prices"] = prices
            except Exception:
                pass
            _last_price_ts = time.time()

        # ── Spočítej, kdy nastat příštímu eventu ─────────────────────────────
        now = time.time()
        next_price = _last_price_ts + config.PRICE_REFRESH_SECONDS
        next_15m   = _next_15m_close(now) + config.CANDLE_SETTLE_SECS

        with _lock:
            _state["next_price_ts"]    = next_price
            _state["next_analysis_ts"] = next_15m

        sleep_time = max(5.0, min(next_price, next_15m) - now)
        time.sleep(sleep_time)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(dict(_state))


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    print("=" * 55)
    print("  Dashboard spusten na http://localhost:5000")
    print(f"  Cena: kazdy {config.PRICE_REFRESH_SECONDS}s | Analyza: po uzavreni 15m svicky")
    print("  Ctrl+C pro ukonceni.")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
