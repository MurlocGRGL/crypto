"""
Crypto Analyzer — desktopová aplikace.

Spuštění: pythonw app.pyw   (nebo zástupce na ploše)

Architektura:
  - Flask server běží v daemon threadu (HTTP API na 127.0.0.1:5000)
  - background_loop stahuje data každých LOOP_INTERVAL_SECONDS
  - pywebview zobrazí localhost:5000 jako nativní okno (bez lišty prohlížeče)
  - pystray přidá ikonku do system tray s menu Otevřít / Ukončit
  - Zavření okna → skrytí do traye (ne ukončení)
  - "Ukončit" v traye → skutečné ukončení aplikace
"""

import socket
import sys
import threading
import time

import pystray
import webview
from PIL import Image, ImageDraw

from dashboard import app as flask_app, background_loop

_window = None
_tray_icon = None
_quitting = False       # True = destroy() byl zavolán záměrně, ne X-buttonem


# ── Flask readiness check ──────────────────────────────────────────────────────
def _wait_for_flask(host="127.0.0.1", port=5000, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ── Tray icon (candlestick chart, 64×64) ──────────────────────────────────────
def _make_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0

    d.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=int(10 * s),
        fill=(22, 27, 34, 255),
    )

    candles = [
        # (color,          cx,  body_top, body_bot, wick_top, wick_bot)
        ((63, 185, 80, 255),  15,   22,      44,       12,      52),
        ((248, 81, 73, 255),  32,   18,      48,        8,      56),
        ((88, 166, 255, 255), 50,   24,      42,       14,      54),
    ]
    for color, cx, bt, bb, wt, wb in candles:
        x = int(cx * s)
        w = max(1, int(s))
        d.line([(x, int(wt * s)), (x, int(wb * s))], fill=color, width=w)
        d.rectangle(
            [x - int(5 * s), int(bt * s), x + int(5 * s), int(bb * s)],
            fill=color,
        )
    return img


# ── Window actions (thread-safe: pywebview marshals to main thread) ────────────
def _show_window():
    if _window:
        _window.show()


def _quit_app(icon=None, item=None):
    global _quitting
    _quitting = True
    if _tray_icon:
        _tray_icon.stop()
    if _window:
        _window.destroy()


# ── Close event: skrýt do traye místo ukončení ────────────────────────────────
def _on_closing():
    if _quitting:
        return True     # záměrné destroy() → nechej projít
    if _window:
        _window.hide()
    return False        # zruší skutečné zavření okna


# ── Tray ──────────────────────────────────────────────────────────────────────
def _start_tray():
    global _tray_icon
    menu = pystray.Menu(
        pystray.MenuItem(
            "Otevřít dashboard",
            lambda icon, item: _show_window(),
            default=True,
        ),
        pystray.MenuItem("Ukončit", _quit_app),
    )
    _tray_icon = pystray.Icon(
        name="crypto_analyzer",
        icon=_make_icon(64),
        title="Crypto Analyzer",
        menu=menu,
    )
    _tray_icon.run()    # blokuje vlákno traye


# ── Webview init callback (voláno z pywebview threadu po startu GUI) ───────────
def _on_webview_started(window):
    global _window
    _window = window
    window.events.closing += _on_closing
    threading.Thread(target=_start_tray, daemon=True).start()


# ── Entrypoint ─────────────────────────────────────────────────────────────────
def main():
    # Flask HTTP server
    threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1", port=5000, debug=False, use_reloader=False
        ),
        daemon=True,
    ).start()

    # Smyčka stahování dat
    threading.Thread(target=background_loop, daemon=True).start()

    # Počkáme, až Flask naběhne (max 15 s)
    if not _wait_for_flask():
        sys.exit("Flask server nenaběhl do 15 sekund.")

    window = webview.create_window(
        title="Crypto Analyzer",
        url="http://127.0.0.1:5000",
        width=1200,
        height=780,
        min_size=(800, 600),
        resizable=True,
        text_select=False,
        confirm_close=False,
    )

    # webview.start() blokuje hlavní vlákno (nutné na Windows)
    webview.start(func=_on_webview_started, args=window, debug=False)


if __name__ == "__main__":
    main()
