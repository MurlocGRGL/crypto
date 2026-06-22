#!/usr/bin/env python3
"""
Faze 3: test individualnich pair streamu vs !forceOrder@arr global.
Taky zkousi Binance testnet a aktualni dokumentaci.
"""

import io
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import websocket
except ImportError:
    print("pip install websocket-client"); sys.exit(1)

LOG_FILE = "liquidation_debug.log"
RUN_SECONDS = 120

_lock = threading.Lock()
_counts: dict[str, int] = {}


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _log(msg: str):
    line = f"{_ts()} {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    with _lock:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _make_ws(name: str, url: str, on_open_extra=None):
    _counts[name] = 0

    def on_msg(ws, raw):
        _counts[name] += 1
        _log(f"[{name}] MSG #{_counts[name]}: {raw[:400]}")

    def on_open(ws):
        _log(f"[{name}] OPEN OK")
        if on_open_extra:
            try:
                on_open_extra(ws)
            except Exception as e:
                _log(f"[{name}] on_open FAIL: {e}")

    def on_err(ws, e):
        _log(f"[{name}] ERROR: {e}")

    def on_close(ws, code, msg):
        _log(f"[{name}] CLOSE code={code} msg={msg}")

    ws = websocket.WebSocketApp(url,
        on_message=on_msg, on_open=on_open,
        on_error=on_err, on_close=on_close)
    threading.Thread(
        target=lambda: ws.run_forever(ping_interval=20, ping_timeout=8),
        daemon=True, name=f"ws-{name}",
    ).start()
    return ws


open(LOG_FILE, "w", encoding="utf-8").close()
_log("=" * 60)
_log(f"  Faze 3: individualni streams + alternativy ({RUN_SECONDS}s)")
_log("=" * 60)

# Varianta 1: combined-stream s individualni pairy (ne !arr)
ws1 = _make_ws("BN-combined-pairs",
    "wss://fstream.binance.com/stream?streams=btcusdt@forceOrder/ethusdt@forceOrder/solusdt@forceOrder")

# Varianta 2: raw per-symbol (stary format)
ws2 = _make_ws("BN-raw-btc",
    "wss://fstream.binance.com/ws/btcusdt@forceOrder")

# Varianta 3: global !arr (control — ocekavame 0)
ws3 = _make_ws("BN-arr-control",
    "wss://fstream.binance.com/stream?streams=!forceOrder@arr")

# Varianta 4: markPriceUpdate stream — test ze Binance vubec posilá data na teto IP
# (markPrice se aktualizuje kazde 3s, tedy 40+ zprav za 2 minuty)
ws4 = _make_ws("BN-markprice-test",
    "wss://fstream.binance.com/stream?streams=btcusdt@markPrice@1s")

_log(f"\nCekam {RUN_SECONDS}s...\n")
try:
    time.sleep(RUN_SECONDS)
except KeyboardInterrupt:
    _log("Preruseno.")

for ws in (ws1, ws2, ws3, ws4):
    ws.close()
time.sleep(0.5)

_log("=" * 60)
_log("VYSLEDKY:")
for name, cnt in sorted(_counts.items()):
    flag = "<-- FUNGUJE" if cnt > 0 else ""
    _log(f"  {name:25}: {cnt:4} zprav {flag}")
_log("")
_log("INTERPRET:")
if _counts.get("BN-markprice-test", 0) == 0:
    _log("  [!] markPrice = 0 => Binance WS blokovan pro tuto IP (firewall/geo)")
elif _counts.get("BN-markprice-test", 0) > 0:
    _log("  [OK] markPrice funguje => Binance WS OK, jen forceOrder nema data")
    if _counts.get("BN-combined-pairs", 0) == 0 and _counts.get("BN-raw-btc", 0) == 0:
        _log("  [!!] forceOrder VSECHNY = 0 => stream je tichy NEBO endpoint deprecated")
    else:
        _log("  [OK] nektere forceOrder streamy fungou - viz vysledky")
_log("=" * 60)
