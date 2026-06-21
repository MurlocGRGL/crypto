"""
Live web dashboard.

Spuštění: python dashboard.py
Otevři prohlížeč na http://localhost:5000

Data se přepočítávají na pozadí každých LOOP_INTERVAL_SECONDS (config.py).
Stránka se sama přepočítá každých 15 sekund přes /api/data.
"""

import time
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template

import config
from data_fetcher import DataFetcher
from indicators import analyze_timeframe, correlation_with_btc
from report_generator import build_symbol_analysis, _trend_from_ichimoku_text

app = Flask(__name__)

_lock = threading.Lock()
_state = {
    "analyses": [],
    "timestamp": None,
    "next_update": None,
    "status": "starting",
    "error_msg": None,
}
_fetcher = DataFetcher()


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


def _run_cycle():
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
        )
        analyses.append(a)

    return analyses


def background_loop():
    while True:
        try:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] Stahuji data...")
            analyses = _run_cycle()
            now = datetime.now()
            next_ts = time.time() + config.LOOP_INTERVAL_SECONDS

            with _lock:
                _state["analyses"] = _to_python(analyses)
                _state["timestamp"] = now.strftime("%Y-%m-%d %H:%M:%S")
                _state["next_update"] = next_ts
                _state["status"] = "ok"
                _state["error_msg"] = None

            print(f"Hotovo. Příští aktualizace za {config.LOOP_INTERVAL_SECONDS}s.")
        except Exception:
            err = traceback.format_exc()
            print(f"[CHYBA]\n{err}")
            with _lock:
                _state["status"] = "error"
                _state["error_msg"] = err.splitlines()[-1]
                if not _state.get("next_update"):
                    _state["next_update"] = time.time() + 60

        time.sleep(config.LOOP_INTERVAL_SECONDS)


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
    print("  Dashboard spuštěn na http://localhost:5000")
    print(f"  Data se načítají každých {config.LOOP_INTERVAL_SECONDS}s (config.py).")
    print("  Ctrl+C pro ukončení.")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
