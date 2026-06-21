"""
Sběr veřejných likvidací z Binance Futures WebSocket (bez API klíče).

Stream !forceOrder@arr broadcastuje všechny tržní likvidace v reálném čase.
Filtrujeme BTC/ETH/SOL/HYPE a ukládáme do SQLite liquidations.db.
Databáze se postupně plní od prvního spuštění appky.

Nový combined-stream endpoint (starý wss://.../ws/!forceOrder@arr skončil 2026-04-23):
  wss://fstream.binance.com/stream?streams=!forceOrder@arr

Použití:
    from liquidation_collector import start_collector, get_liq_summary
    start_collector()
    stats = get_liq_summary('BTC/USDT')
"""

import json
import os
import sqlite3
import threading
import time

try:
    import websocket as _ws_lib
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# nový combined-stream endpoint (post-2026-04-23), fallback na starý formát
_WS_URLS = [
    "wss://fstream.binance.com/stream?streams=!forceOrder@arr",
    "wss://fstream.binance.com/ws/!forceOrder@arr",
]

_TRACKED = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"}
_KEEP_DAYS = 7          # starší záznamy mažeme při startu
DB_PATH = os.path.join(os.path.dirname(__file__), "liquidations.db")

_started = False
_started_lock = threading.Lock()
_total_inserted = 0     # čítač pro status v dashboardu


# ── SQLite ──────────────────────────────────────────────────────────────────

def _init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS liq (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                symbol  TEXT    NOT NULL,
                side    TEXT    NOT NULL,   -- LONG nebo SHORT (pozice která byla likvidována)
                price   REAL    NOT NULL,
                qty     REAL    NOT NULL,
                usd     REAL    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_sym_ts ON liq(symbol, ts)")
    _cleanup_old()


def _cleanup_old():
    cutoff = int((time.time() - _KEEP_DAYS * 86400) * 1000)
    try:
        with sqlite3.connect(DB_PATH) as con:
            deleted = con.execute("DELETE FROM liq WHERE ts < ?", (cutoff,)).rowcount
        if deleted:
            print(f"[LIQ] Smazáno {deleted} starých záznamů (>{_KEEP_DAYS}d).")
    except Exception:
        pass


def _insert(ts, symbol, side, price, qty, usd):
    global _total_inserted
    try:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO liq(ts, symbol, side, price, qty, usd) VALUES (?,?,?,?,?,?)",
                (ts, symbol, side, price, qty, usd),
            )
        _total_inserted += 1
    except Exception:
        pass


# ── Veřejné query funkce ─────────────────────────────────────────────────────

def get_stats(symbol_clean: str, hours: float) -> dict:
    """
    symbol_clean: 'BTCUSDT'
    Vrací {"long_usd", "short_usd", "long_count", "short_count"} za posledních `hours` h.
    """
    since = int((time.time() - hours * 3600) * 1000)
    try:
        with sqlite3.connect(DB_PATH) as con:
            rows = con.execute(
                "SELECT side, SUM(usd), COUNT(*) FROM liq WHERE symbol=? AND ts>=? GROUP BY side",
                (symbol_clean, since),
            ).fetchall()
    except Exception:
        return {"long_usd": 0, "short_usd": 0, "long_count": 0, "short_count": 0}

    r = {"long_usd": 0, "short_usd": 0, "long_count": 0, "short_count": 0}
    for side, usd, cnt in rows:
        if side == "LONG":
            r["long_usd"] = round(usd or 0)
            r["long_count"] = cnt or 0
        elif side == "SHORT":
            r["short_usd"] = round(usd or 0)
            r["short_count"] = cnt or 0
    return r


def get_liq_summary(symbol: str) -> dict:
    """
    symbol: 'BTC/USDT'
    Vrací dict s 1H / 4H / 24H stats a celkovým počtem v DB.
    """
    sym = symbol.replace("/", "")   # BTC/USDT → BTCUSDT
    try:
        with sqlite3.connect(DB_PATH) as con:
            total = con.execute("SELECT COUNT(*) FROM liq WHERE symbol=?", (sym,)).fetchone()[0]
    except Exception:
        total = 0

    return {
        "1h":  get_stats(sym, 1),
        "4h":  get_stats(sym, 4),
        "24h": get_stats(sym, 24),
        "total": total,
        "total_all": _total_inserted,
        "collecting": _started,
    }


# ── WebSocket zpracování ─────────────────────────────────────────────────────

def _process_event(event: dict):
    o = event.get("o", {})
    symbol = o.get("s", "")
    if symbol not in _TRACKED:
        return

    # S: SELL = long pozice likvidována (broker prodal long)
    #    BUY  = short pozice likvidována (broker koupil short)
    order_side = o.get("S", "")
    liq_side = "LONG" if order_side == "SELL" else "SHORT"

    try:
        price = float(o.get("ap") or o.get("p") or 0)
        qty   = float(o.get("q") or 0)
        usd   = price * qty
        ts    = int(o.get("T") or time.time() * 1000)
    except (TypeError, ValueError):
        return

    if usd > 0:
        _insert(ts, symbol, liq_side, price, qty, usd)


def _on_message(ws, raw):
    try:
        data = json.loads(raw)
        # Combined-stream wrapper: {"stream":"!forceOrder@arr","data":{...}}
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        # data může být list (arr) nebo single dict
        if isinstance(data, list):
            for evt in data:
                _process_event(evt)
        elif isinstance(data, dict):
            _process_event(data)
    except Exception:
        pass


def _run_ws(url: str):
    ws = _ws_lib.WebSocketApp(
        url,
        on_message=_on_message,
        on_open=lambda ws: print(f"[LIQ] Připojen: {url}"),
        on_error=lambda ws, e: print(f"[LIQ] Chyba: {e}"),
        on_close=lambda ws, c, m: print(f"[LIQ] Odpojeno (kód {c})"),
    )
    ws.run_forever(ping_interval=20, ping_timeout=8)


def _collector_loop():
    url_idx = 0
    backoff = 3.0
    while True:
        url = _WS_URLS[url_idx % len(_WS_URLS)]
        try:
            _run_ws(url)
        except Exception as e:
            print(f"[LIQ] Výjimka: {e}")
        # Při chybě zkusíme druhý URL
        url_idx += 1
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 60)


# ── Veřejné API ──────────────────────────────────────────────────────────────

def start_collector():
    """
    Inicializuje SQLite DB a spustí WebSocket listener na pozadí.
    Idempotentní — opakované volání je bezpečné.
    """
    global _started
    with _started_lock:
        if _started:
            return
        if not _WS_AVAILABLE:
            print("[LIQ] websocket-client není nainstalován (pip install websocket-client). Kolektor přeskočen.")
            return
        _started = True

    _init_db()
    t = threading.Thread(target=_collector_loop, daemon=True, name="liq-collector")
    t.start()
    print("[LIQ] Kolektor likvidací spuštěn (stream: !forceOrder@arr).")
