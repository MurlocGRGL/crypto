"""
Sběr veřejných likvidací z Binance Futures WebSocket (bez API klíče).

Stream !forceOrder@arr broadcastuje všechny tržní likvidace v reálném čase.
Filtrujeme BTC/ETH/SOL/HYPE a ukládáme do SQLite liquidations.db.

Combined-stream endpoint (starý wss://.../ws/!forceOrder@arr deprecated 2026-04-23):
  wss://fstream.binance.com/stream?streams=!forceOrder@arr

Debug log: pokud proměnná prostředí LIQ_DEBUG=1, loguje každou raw WS zprávu
           do liquidation_debug.log. Vypnout pro produkci.

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
from datetime import datetime, timezone

try:
    import websocket as _ws_lib
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False

# combined-stream endpoint (post-2026-04-23)
_WS_URLS = [
    "wss://fstream.binance.com/stream?streams=!forceOrder@arr",
    "wss://fstream.binance.com/ws/!forceOrder@arr",   # fallback (deprecated)
]

_TRACKED    = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"}
_KEEP_DAYS  = 7
DB_PATH     = os.path.join(os.path.dirname(__file__), "liquidations.db")
_DEBUG_LOG  = os.path.join(os.path.dirname(__file__), "liquidation_debug.log")
_DEBUG_MODE = os.environ.get("LIQ_DEBUG", "0") == "1"

# Canary stream: markPrice@1s posílá zprávu každou sekundu.
# Pokud za _CANARY_TIMEOUT_SECS nepřijde nic, Binance WS je blokovaný.
_CANARY_URL          = "wss://fstream.binance.com/stream?streams=btcusdt@markPrice@1s"
_CANARY_TIMEOUT_SECS = 90    # po 90s bez zprávy = blok
_HEALTH_WARN_SECS    = 1800  # varování v logu po 30min bez forceOrder zprávy

_started        = False
_started_lock   = threading.Lock()
_total_inserted = 0    # záznamy vložené do DB
_total_received = 0    # raw WS zprávy přijaté z liquidation streamu
_last_msg_ts    = 0.0  # Unix čas poslední přijaté forceOrder zprávy
_ws_blocked     = None # None=neznámo, True=blokováno, False=OK
_lock_stats     = threading.Lock()


# ── Debug logging ─────────────────────────────────────────────────────────────

def _debug_log(msg: str):
    if not _DEBUG_MODE:
        return
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]} {msg}\n"
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ── SQLite ────────────────────────────────────────────────────────────────────

def _init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS liq (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      INTEGER NOT NULL,
                symbol  TEXT    NOT NULL,
                side    TEXT    NOT NULL,
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
        with _lock_stats:
            _total_inserted += 1
    except Exception as e:
        _debug_log(f"[DB] INSERT chyba: {e}")


# ── Veřejné query funkce ──────────────────────────────────────────────────────

def get_stats(symbol_clean: str, hours: float) -> dict:
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
    sym = symbol.replace("/", "")
    try:
        with sqlite3.connect(DB_PATH) as con:
            total = con.execute("SELECT COUNT(*) FROM liq WHERE symbol=?", (sym,)).fetchone()[0]
    except Exception:
        total = 0

    with _lock_stats:
        inserted  = _total_inserted
        received  = _total_received
        last_msg  = _last_msg_ts
        blocked   = _ws_blocked

    secs_since = time.time() - last_msg if last_msg > 0 else None

    return {
        "1h":  get_stats(sym, 1),
        "4h":  get_stats(sym, 4),
        "24h": get_stats(sym, 24),
        "total":        total,
        "total_all":    inserted,
        "ws_received":  received,
        "secs_since_last_msg": round(secs_since) if secs_since else None,
        "collecting":   _started,
        "ws_blocked":   blocked,   # None=zjišťuji, True=blokováno, False=OK
    }


# ── WebSocket zpracování ──────────────────────────────────────────────────────

def _process_event(event: dict):
    """Zpracuje jeden forceOrder event — loguje vše včetně neznámých formátů."""
    o = event.get("o", {})
    if not o:
        _debug_log(f"[PARSE] event bez 'o' klice: {event}")
        return

    symbol = o.get("s", "")
    if symbol not in _TRACKED:
        return   # jiný symbol — OK, nelogujeme (je jich stovky)

    order_side = o.get("S", "")
    liq_side   = "LONG" if order_side == "SELL" else "SHORT"

    try:
        price = float(o.get("ap") or o.get("p") or 0)
        qty   = float(o.get("q") or 0)
        usd   = price * qty
        ts    = int(o.get("T") or time.time() * 1000)
    except (TypeError, ValueError) as e:
        _debug_log(f"[PARSE] parse chyba u {symbol}: {e} | o={o}")
        return

    if usd > 0:
        _debug_log(f"[INSERT] {symbol} {liq_side} ${usd:.0f} @ {price}")
        _insert(ts, symbol, liq_side, price, qty, usd)
    else:
        _debug_log(f"[SKIP] {symbol} usd=0 | o={o}")


def _on_message(ws, raw):
    global _total_received, _last_msg_ts

    with _lock_stats:
        _total_received += 1
        _last_msg_ts = time.time()

    _debug_log(f"[RAW] {raw[:500]}")

    # Parsování — chyby se logují, neswallowujeme tiše
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _debug_log(f"[JSON] decode chyba: {e} | raw={raw[:200]}")
        return

    # Combined-stream wrapper: {"stream":"!forceOrder@arr","data":{...}}
    if isinstance(data, dict) and "data" in data:
        data = data["data"]

    if isinstance(data, list):
        for evt in data:
            if isinstance(evt, dict):
                _process_event(evt)
            else:
                _debug_log(f"[PARSE] neocekavany typ v listu: {type(evt)} | {evt}")
    elif isinstance(data, dict):
        _process_event(data)
    else:
        _debug_log(f"[PARSE] neocekavany format: {type(data)} | {str(data)[:200]}")


def _on_open(ws, url: str):
    print(f"[LIQ] Pripojeno: {url}")
    _debug_log(f"[WS] OPEN: {url}")


def _on_error(ws, error):
    msg = str(error)
    print(f"[LIQ] WS chyba: {msg}")
    _debug_log(f"[WS] ERROR: {msg}")


def _on_close(ws, code, msg):
    print(f"[LIQ] WS odpojeno (kod={code})")
    _debug_log(f"[WS] CLOSE code={code} msg={msg}")


def _run_ws(url: str):
    ws = _ws_lib.WebSocketApp(
        url,
        on_message=_on_message,
        on_open=lambda ws: _on_open(ws, url),
        on_error=_on_error,
        on_close=_on_close,
    )
    ws.run_forever(ping_interval=20, ping_timeout=8)


# ── Health check vlákno ───────────────────────────────────────────────────────

def _health_loop():
    """Každých 5 minut zkontroluje, zda stream posílá data. Varuje pokud ne."""
    time.sleep(300)   # první check po 5 minutách
    while True:
        with _lock_stats:
            received = _total_received
            last_ts  = _last_msg_ts

        secs = time.time() - last_ts if last_ts > 0 else float("inf")
        if secs > _HEALTH_WARN_SECS:
            mins = int(secs // 60)
            print(f"[LIQ] VAROVANI: {mins} min bez WS zprav. "
                  f"Celkem prijato: {received}. Stream mozna nefunguje.")
            _debug_log(f"[HEALTH] {mins} min bez zprav, received={received}")
        else:
            _debug_log(f"[HEALTH] OK, posledni zprava pred {int(secs)}s, received={received}")

        time.sleep(300)


# ── Canary: detekce bloku Binance WS ─────────────────────────────────────────

def _canary_loop():
    """
    Připojí se k markPrice@1s streamu (1 zpráva/s).
    Po _CANARY_TIMEOUT_SECS bez zprávy nastaví _ws_blocked = True.
    Jakmile přijde první zpráva, _ws_blocked = False.
    """
    global _ws_blocked
    _canary_received = [0]
    _canary_started  = [time.time()]

    def on_msg(ws, raw):
        global _ws_blocked
        _canary_received[0] += 1
        if _canary_received[0] == 1:
            with _lock_stats:
                _ws_blocked = False
            print("[LIQ] Canary: Binance WS OK (markPrice prijat).")
            _debug_log("[CANARY] OK - markPrice zprava prijata, WS neni blokovan")

    def on_open(ws):
        _canary_started[0] = time.time()
        _debug_log("[CANARY] OPEN")

    def on_err(ws, e):
        _debug_log(f"[CANARY] ERROR: {e}")

    def on_close(ws, code, msg):
        _debug_log(f"[CANARY] CLOSE code={code}")

    ws = _ws_lib.WebSocketApp(_CANARY_URL,
        on_message=on_msg, on_open=on_open,
        on_error=on_err, on_close=on_close)

    t = threading.Thread(target=lambda: ws.run_forever(ping_interval=20, ping_timeout=8),
                         daemon=True, name="liq-canary-ws")
    t.start()

    # Počkej _CANARY_TIMEOUT_SECS — pokud do té doby nepřijde zpráva, blok detekován
    deadline = time.time() + _CANARY_TIMEOUT_SECS
    while time.time() < deadline:
        time.sleep(1)
        if _canary_received[0] > 0:
            return   # OK, hotovo

    ws.close()
    if _canary_received[0] == 0:
        with _lock_stats:
            _ws_blocked = True
        print(
            "[LIQ] VAROVANI: Binance Futures WebSocket nereaguje po "
            f"{_CANARY_TIMEOUT_SECS}s. Mozne geoblokovani nebo IP ban.\n"
            "      Zkus VPN nebo proxy. Liquidation data nebudou k dispozici."
        )
        _debug_log("[CANARY] TIMEOUT - WS blokovan")


# ── Kolektor smyčka ───────────────────────────────────────────────────────────

def _collector_loop():
    url_idx = 0
    backoff = 3.0
    while True:
        url = _WS_URLS[url_idx % len(_WS_URLS)]
        try:
            _run_ws(url)
        except Exception as e:
            print(f"[LIQ] Vyjimka: {e}")
            _debug_log(f"[WS] Vyjimka: {e}")
        url_idx += 1
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 60)


# ── Veřejné API ───────────────────────────────────────────────────────────────

def start_collector():
    """Inicializuje DB a spustí WS listener. Idempotentní."""
    global _started
    with _started_lock:
        if _started:
            return
        if not _WS_AVAILABLE:
            print("[LIQ] websocket-client neni nainstalovan. Kolektor preskocen.")
            return
        _started = True

    if _DEBUG_MODE:
        # Vymaž starý debug log při startu
        try:
            open(_DEBUG_LOG, "w", encoding="utf-8").close()
        except Exception:
            pass
        print(f"[LIQ] DEBUG mod zapnut — raw zpravy logovany do {_DEBUG_LOG}")

    _init_db()

    threading.Thread(target=_collector_loop,  daemon=True, name="liq-collector").start()
    threading.Thread(target=_health_loop,     daemon=True, name="liq-health").start()
    threading.Thread(target=_canary_loop,     daemon=True, name="liq-canary").start()

    print("[LIQ] Kolektor likvidaci spusten (stream: !forceOrder@arr).")
    print("[LIQ]   Pro debug: LIQ_DEBUG=1 python dashboard.py")
