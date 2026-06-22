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

import base64
import math
import os
import time
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template, request

import config
from data_fetcher import DataFetcher
import pandas as pd
from indicators import analyze_timeframe, correlation_with_btc, time_based_levels
from report_generator import build_symbol_analysis, _trend_from_ichimoku_text
import portfolio as pf

app = Flask(__name__)
pf.init_db()        # portfolio.db — pozice + deník

_lock = threading.Lock()
_state = {
    "analyses": [],
    "live_prices": {},          # {symbol: {price, change_24h_pct}} — fast ticker
    "correlation_matrix": None, # {symbols: [...], matrix: [[...]]} — 4×4 tabulka
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


def _compute_correlation_matrix(raw_data: dict) -> dict | None:
    """Vypočítá párové Pearson korelace zavíracích cen (1h, 60 svíček) pro všechny symboly."""
    closes = {}
    for sym in config.SYMBOLS:
        df = raw_data.get(sym, {}).get("1h")
        if df is not None and len(df) >= 60:
            closes[sym] = df["close"].iloc[-60:].reset_index(drop=True)
    if len(closes) < 2:
        return None
    syms = list(closes.keys())
    df_all = pd.DataFrame(closes)
    corr = df_all.corr()
    labels = [s.replace("/USDT", "") for s in syms]
    matrix = [[round(float(corr.loc[a, b]), 2) for b in syms] for a in syms]
    return {"symbols": labels, "matrix": matrix}


def _run_full_analysis() -> list:
    """Stáhne OHLCV, spočítá indikátory a derivátová data pro všechny symboly."""
    raw_data = _fetcher.fetch_all(config.SYMBOLS, config.TIMEFRAMES, limit=config.CANDLE_LIMIT)

    analyzed = {}
    time_levels_map = {}
    for symbol, tf_dict in raw_data.items():
        analyzed[symbol] = {tf: analyze_timeframe(df) for tf, df in tf_dict.items()}
        df_1h_sym = tf_dict.get("1h")
        time_levels_map[symbol] = time_based_levels(df_1h_sym) if df_1h_sym is not None else {}

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
            time_levels=time_levels_map.get(symbol, {}),
        )
        analyses.append(a)

    corr_matrix = _compute_correlation_matrix(raw_data)
    with _lock:
        _state["correlation_matrix"] = _to_python(corr_matrix)

    pf.log_setups(analyses)   # auto-log do trading deníku (1× za hodinu na symbol)

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


# ── Portfolio API ─────────────────────────────────────────────────────────────

@app.route("/api/portfolio")
def api_portfolio():
    with _lock:
        lp = dict(_state["live_prices"])
        cm = _state.get("correlation_matrix")
    return jsonify(pf.get_portfolio_summary(lp, cm))


@app.route("/api/portfolio/position", methods=["POST"])
def api_add_position():
    d = request.json or {}
    try:
        pos_id = pf.add_position(
            symbol=d["symbol"],
            side=d["side"],
            entry_price=float(d["entry_price"]),
            size_usdt=float(d["size_usdt"]),
            sl_price=float(d["sl_price"]) if d.get("sl_price") else None,
        )
        return jsonify({"ok": True, "id": pos_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/portfolio/position/<int:pos_id>/close", methods=["POST"])
def api_close_position(pos_id):
    d = request.json or {}
    try:
        updated = pf.close_position(pos_id, float(d["close_price"]))
        if updated:
            return jsonify({"ok": True, "position": updated})
        return jsonify({"ok": False, "error": "Pozice nenalezena"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/portfolio/position/<int:pos_id>", methods=["DELETE"])
def api_delete_position(pos_id):
    return jsonify({"ok": pf.delete_position(pos_id)})


@app.route("/api/portfolio/settings", methods=["POST"])
def api_portfolio_settings():
    d = request.json or {}
    kwargs = {}
    for key in ("daily_stop", "weekly_stop", "account_size"):
        if key in d:
            kwargs[key] = float(d[key]) if d[key] not in (None, "") else None
    settings = pf.update_settings(**kwargs)
    return jsonify({"ok": True, "settings": settings})


# ── Journal API ───────────────────────────────────────────────────────────────

@app.route("/api/journal")
def api_journal():
    return jsonify(pf.get_journal())


@app.route("/api/journal/<int:entry_id>", methods=["POST"])
def api_update_journal(entry_id):
    d = request.json or {}
    ok = pf.update_journal_entry(entry_id, **{
        k: v for k, v in d.items()
        if k in ("action", "result", "notes", "pnl_usdt")
    })
    return jsonify({"ok": ok})


@app.route("/api/screenshot", methods=["POST"])
def api_screenshot():
    d = request.json or {}
    img_data = d.get("image", "")
    filename  = d.get("filename", "screenshot.png")
    filename  = "".join(c for c in filename if c.isalnum() or c in "._-")
    if not filename.endswith(".png"):
        filename += ".png"
    try:
        _, b64 = img_data.split(",", 1)
        img_bytes = base64.b64decode(b64)
        folder = os.path.join(os.path.dirname(__file__), "screenshots")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, filename), "wb") as f:
            f.write(img_bytes)
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


if __name__ == "__main__":
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    print("=" * 55)
    print("  Dashboard spusten na http://localhost:5000")
    print(f"  Cena: kazdy {config.PRICE_REFRESH_SECONDS}s | Analyza: po uzavreni 15m svicky")
    print("  Ctrl+C pro ukonceni.")
    print("=" * 55)
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
