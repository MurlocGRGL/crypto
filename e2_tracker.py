"""
E2 Paper Trading Tracker
Runs every 15 minutes, evaluates E2 signal for BTC/ETH/SOL/HYPE.
Logs new LONG/SHORT signals and monitors open positions for SL/TP hits.

Output: paper_trading_log.csv
Usage:  python e2_tracker.py
Stop:   Ctrl+C

CSV columns:
  timestamp, symbol, signal, entry_price, sl, tp1, tp2, tp3,
  e2_conditions, result, close_time, close_price

result values: SL_HIT | TP1_HIT | TP2_HIT | TP3_HIT  (empty = still open)
e2_conditions: pipe-separated list of met E2 conditions
"""

import csv
import math
import os
import sys
import time
from datetime import datetime

# Allow importing project modules from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from data_fetcher import DataFetcher
from indicators import analyze_timeframe, correlation_with_btc, time_based_levels
from report_generator import build_symbol_analysis, _trend_from_ichimoku_text

CSV_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trading_log.csv")
CHECK_INTERVAL = 900   # 15 minutes

CSV_FIELDS = [
    "timestamp", "symbol", "signal", "entry_price",
    "sl", "tp1", "tp2", "tp3", "e2_conditions",
    "result", "close_time", "close_price",
]

_fetcher = DataFetcher()


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(v) -> str:
    """Format a numeric value for CSV; empty string for None/0/invalid."""
    try:
        f = float(v)
        return str(round(f, 8)) if f else ""
    except (TypeError, ValueError):
        return ""


# ── CSV I/O ────────────────────────────────────────────────────────────────────

def _init_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()
        print(f"[{_ts()}] Created {CSV_PATH}")


def _read_csv() -> list:
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(rows: list):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _open_map(rows: list) -> dict:
    """Returns {symbol: row} for the most recent open (no result) signal per symbol."""
    result = {}
    for row in rows:
        sym = row.get("symbol", "")
        if not row.get("result") and sym not in result:
            result[sym] = row
    return result


# ── Data fetch ─────────────────────────────────────────────────────────────────

def _fetch_all() -> tuple:
    """Returns (analyses: list, live_prices: dict). Raises on network failure."""
    raw = _fetcher.fetch_all(config.SYMBOLS, config.TIMEFRAMES, limit=config.CANDLE_LIMIT)

    analyzed = {}
    tl_map   = {}
    for sym, tf_dict in raw.items():
        analyzed[sym] = {tf: analyze_timeframe(df) for tf, df in tf_dict.items()}
        df_1h = tf_dict.get("1h")
        tl_map[sym] = time_based_levels(df_1h) if df_1h is not None else {}

    btc_trend = None
    if "BTC/USDT" in analyzed:
        btc_tf = analyzed["BTC/USDT"].get("4h") or analyzed["BTC/USDT"].get("1d")
        if btc_tf:
            btc_trend = _trend_from_ichimoku_text(btc_tf["ichimoku_text"])

    btc_1h     = raw.get("BTC/USDT", {}).get("1h")
    fear_greed = _fetcher.fetch_fear_greed()

    analyses = []
    for sym in config.SYMBOLS:
        tf_dict = raw.get(sym, {})
        corr    = (correlation_with_btc(tf_dict.get("1h"), btc_1h)
                   if sym != "BTC/USDT" and btc_1h is not None else None)

        fr, oi        = _fetcher.fetch_funding_and_oi(sym)
        ls_l, ls_s    = _fetcher.fetch_long_short_ratio(sym)
        oi_hist       = _fetcher.fetch_oi_history(sym)
        basis         = _fetcher.fetch_futures_basis(sym)
        cvd           = _fetcher.fetch_cvd(sym)
        options       = _fetcher.fetch_options_data(sym)

        try:
            a = build_symbol_analysis(
                sym, analyzed.get(sym, {}),
                btc_trend=btc_trend,    correlation_btc=corr,
                funding_rate=fr,        open_interest=oi,
                ls_long=ls_l,           ls_short=ls_s,
                oi_history=oi_hist,     fear_greed=fear_greed,
                basis=basis,            cvd=cvd,
                options_data=options,   time_levels=tl_map.get(sym, {}),
            )
            analyses.append(a)
        except Exception as exc:
            print(f"[{_ts()}] ERROR  {sym}: {exc}")

    live_prices = _fetcher.fetch_live_prices(config.SYMBOLS)
    return analyses, live_prices


# ── SL / TP check ──────────────────────────────────────────────────────────────

def _check_tp_sl(rows: list, prices: dict) -> int:
    """
    Scans open signals against current prices.
    Mutates matching rows in-place with result/close_time/close_price.
    Returns number of newly closed signals.
    """
    closed = 0
    for row in rows:
        if row.get("result"):
            continue

        sym   = row.get("symbol", "")
        price = prices.get(sym, {}).get("price")
        if not price:
            continue

        sig  = row.get("signal", "")
        sl   = float(row["sl"])  if row.get("sl")  else None
        tp1  = float(row["tp1"]) if row.get("tp1") else None
        tp2  = float(row["tp2"]) if row.get("tp2") else None
        tp3  = float(row["tp3"]) if row.get("tp3") else None

        hit = None
        if sig == "LONG":
            if   sl  and price <= sl:   hit = "SL_HIT"
            elif tp3 and price >= tp3:  hit = "TP3_HIT"
            elif tp2 and price >= tp2:  hit = "TP2_HIT"
            elif tp1 and price >= tp1:  hit = "TP1_HIT"
        elif sig == "SHORT":
            if   sl  and price >= sl:   hit = "SL_HIT"
            elif tp3 and price <= tp3:  hit = "TP3_HIT"
            elif tp2 and price <= tp2:  hit = "TP2_HIT"
            elif tp1 and price <= tp1:  hit = "TP1_HIT"

        if hit:
            row["result"]      = hit
            row["close_time"]  = _ts()
            row["close_price"] = str(price)
            closed += 1
            print(f"[{_ts()}] CLOSED  {sym:12} {sig:5} → {hit:8}  @ {price}")

    return closed


# ── New signal logging ─────────────────────────────────────────────────────────

def _log_new(rows: list, analyses: list, om: dict) -> int:
    """
    Appends new E2 LONG/SHORT signals for symbols without an open position.
    Returns number of rows added.
    """
    added = 0
    for a in analyses:
        if a.get("error"):
            continue
        sym = a["symbol"]
        sig = a.get("e2_signal", "WAIT")
        if sig not in ("LONG", "SHORT") or sym in om:
            continue

        cl    = a.get("e2_checklist") or {}
        conds = "|".join(k for k, v in cl.items() if v)
        side  = a["long"] if sig == "LONG" else a["short"]
        entry = _fmt(side.get("entry") or a.get("last_price"))

        rows.append({
            "timestamp":     _ts(),
            "symbol":        sym,
            "signal":        sig,
            "entry_price":   entry,
            "sl":            _fmt(side.get("sl")),
            "tp1":           _fmt(side.get("tp1")),
            "tp2":           _fmt(side.get("tp2")),
            "tp3":           _fmt(side.get("tp3")),
            "e2_conditions": conds,
            "result":        "",
            "close_time":    "",
            "close_price":   "",
        })
        added += 1
        print(f"[{_ts()}] NEW     {sym:12} {sig:5}  entry={entry}")

    return added


# ── Cycle ──────────────────────────────────────────────────────────────────────

def run_cycle():
    rows = _read_csv()
    om   = _open_map(rows)

    try:
        analyses, prices = _fetch_all()
    except Exception as exc:
        print(f"[{_ts()}] ERROR  fetch failed: {exc}")
        return

    closed = _check_tp_sl(rows, prices)
    if closed:
        om = _open_map(rows)   # refresh after closures so freed slots can get new signals

    added = _log_new(rows, analyses, om)

    if closed or added:
        _write_csv(rows)
        total_open   = sum(1 for r in rows if not r.get("result"))
        total_closed = sum(1 for r in rows if r.get("result"))
        print(f"[{_ts()}] saved  +{added} new  -{closed} closed  "
              f"| open={total_open}  closed={total_closed}  total={len(rows)}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    _init_csv()
    print(f"[{_ts()}] E2 Tracker running  |  interval={CHECK_INTERVAL}s")
    print(f"[{_ts()}] CSV: {CSV_PATH}")
    print("  Ctrl+C to stop\n")

    # Align first run to the next 15-minute candle boundary
    now      = time.time()
    next_run = math.ceil(now / CHECK_INTERVAL) * CHECK_INTERVAL
    secs     = next_run - now
    print(f"[{_ts()}] First run in {int(secs)}s (next 15m close)…")

    while True:
        secs = max(0.0, next_run - time.time())
        time.sleep(secs)
        try:
            run_cycle()
        except Exception as exc:
            print(f"[{_ts()}] ERROR  cycle: {exc}")
        next_run += CHECK_INTERVAL


if __name__ == "__main__":
    try:
        if "--once" in sys.argv:
            # Single-cycle mode — used by GitHub Actions
            _init_csv()
            run_cycle()
        else:
            main()   # infinite loop for local use
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] Stopped.")
