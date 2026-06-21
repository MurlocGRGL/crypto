"""
Portfolio & Risk Tracking + Trading Journal.
SQLite backend (portfolio.db), thread-safe.

Pozice se zadávají ručně přes formulář v dashboardu.
Deník loguje každý 1H vygenerovaný setup a umožňuje dopsat reálný výsledek.
"""

import math
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "portfolio.db"
_lock = threading.Lock()

_JOURNAL_DEDUP_SECS = 3600  # loguj max 1× za hodinu na symbol


def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _lock:
        con = _conn()
        con.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                side        TEXT    NOT NULL,
                entry_price REAL    NOT NULL,
                size_usdt   REAL    NOT NULL,
                sl_price    REAL,
                open_ts     INTEGER NOT NULL,
                closed_ts   INTEGER,
                close_price REAL,
                pnl_usdt    REAL,
                status      TEXT    NOT NULL DEFAULT 'OPEN'
            );
            CREATE TABLE IF NOT EXISTS risk_settings (
                key   TEXT PRIMARY KEY,
                value REAL
            );
            INSERT OR IGNORE INTO risk_settings VALUES ('daily_stop',   null);
            INSERT OR IGNORE INTO risk_settings VALUES ('weekly_stop',  null);
            INSERT OR IGNORE INTO risk_settings VALUES ('account_size', 1000);
            CREATE TABLE IF NOT EXISTS journal_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                ts           INTEGER NOT NULL,
                candle_ts    INTEGER NOT NULL,
                conclusion   TEXT,
                long_pct     INTEGER,
                short_pct    INTEGER,
                wait_pct     INTEGER,
                trader_score INTEGER,
                long_entry   REAL,
                long_sl      REAL,
                short_entry  REAL,
                short_sl     REAL,
                action       TEXT,
                result       TEXT,
                notes        TEXT,
                pnl_usdt     REAL,
                UNIQUE(symbol, candle_ts)
            );
        """)
        con.commit()
        con.close()


# ── Positions ─────────────────────────────────────────────────────────────────

def add_position(symbol: str, side: str, entry_price: float,
                 size_usdt: float, sl_price: float | None = None) -> int:
    now = int(time.time())
    with _lock:
        con = _conn()
        cur = con.execute(
            "INSERT INTO positions (symbol, side, entry_price, size_usdt, sl_price, open_ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, side.upper(), entry_price, size_usdt, sl_price, now),
        )
        pos_id = cur.lastrowid
        con.commit()
        con.close()
    return pos_id


def close_position(pos_id: int, close_price: float) -> dict | None:
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT * FROM positions WHERE id=? AND status='OPEN'", (pos_id,)
        ).fetchone()
        if not row:
            con.close()
            return None
        qty = row["size_usdt"] / row["entry_price"]
        direction = 1 if row["side"] == "LONG" else -1
        pnl = round(qty * (close_price - row["entry_price"]) * direction, 4)
        now = int(time.time())
        con.execute(
            "UPDATE positions SET status='CLOSED', closed_ts=?, close_price=?, pnl_usdt=? WHERE id=?",
            (now, close_price, pnl, pos_id),
        )
        con.commit()
        result = dict(con.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone())
        con.close()
    return result


def delete_position(pos_id: int) -> bool:
    with _lock:
        con = _conn()
        rows = con.execute("DELETE FROM positions WHERE id=?", (pos_id,)).rowcount
        con.commit()
        con.close()
    return rows > 0


def _live_pnl(row, live_prices: dict) -> float | None:
    px_info = live_prices.get(row["symbol"])
    if not px_info:
        return None
    price = px_info.get("price")
    if not price:
        return None
    qty = row["size_usdt"] / row["entry_price"]
    direction = 1 if row["side"] == "LONG" else -1
    return round(qty * (price - row["entry_price"]) * direction, 4)


def _day_boundaries():
    now_utc = datetime.now(timezone.utc)
    today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    week  = today - timedelta(days=today.weekday())  # Monday
    return int(today.timestamp()), int(week.timestamp())


def get_portfolio_summary(live_prices: dict, corr_matrix: dict | None = None) -> dict:
    today_start, week_start = _day_boundaries()

    with _lock:
        con = _conn()
        open_rows  = con.execute(
            "SELECT * FROM positions WHERE status='OPEN'  ORDER BY open_ts DESC"
        ).fetchall()
        closed_all = con.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_ts"
        ).fetchall()
        closed_recent = con.execute(
            "SELECT * FROM positions WHERE status='CLOSED' ORDER BY closed_ts DESC LIMIT 20"
        ).fetchall()
        settings = {r["key"]: r["value"] for r in con.execute("SELECT * FROM risk_settings")}
        con.close()

    # Open positions with live P&L
    open_positions = []
    total_open_pnl = 0.0
    total_exposure  = 0.0
    exposure_by_sym: dict[str, float] = {}
    sides_by_sym:   dict[str, list]   = {}

    for r in open_rows:
        pnl = _live_pnl(r, live_prices)
        pos = dict(r)
        pos["live_pnl"] = pnl
        px = live_prices.get(r["symbol"], {}).get("price")
        pos["live_price"] = px
        if px and r["sl_price"]:
            pos["sl_dist_pct"] = round(abs(px - r["sl_price"]) / px * 100, 2)
        open_positions.append(pos)
        if pnl is not None:
            total_open_pnl += pnl
        total_exposure += r["size_usdt"]
        sym = r["symbol"].replace("/USDT", "")
        exposure_by_sym[sym] = exposure_by_sym.get(sym, 0) + r["size_usdt"]
        sides_by_sym.setdefault(sym, []).append(r["side"])

    # Closed P&L — today / this week / all time
    today_pnl = sum(r["pnl_usdt"] or 0 for r in closed_all if (r["closed_ts"] or 0) >= today_start)
    week_pnl  = sum(r["pnl_usdt"] or 0 for r in closed_all if (r["closed_ts"] or 0) >= week_start)
    total_closed_pnl = sum(r["pnl_usdt"] or 0 for r in closed_all)

    # Max drawdown (peak-to-trough of cumulative closed P&L)
    cum = peak = max_dd = 0.0
    for r in closed_all:
        cum += r["pnl_usdt"] or 0
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    account = settings.get("account_size") or 1000
    leverage = round(total_exposure / account, 2) if account else None

    # Correlation risk warnings (same-direction positions on highly correlated coins)
    corr_warnings = []
    open_syms = list(exposure_by_sym.keys())
    if corr_matrix and len(open_syms) >= 2:
        syms   = corr_matrix.get("symbols", [])
        matrix = corr_matrix.get("matrix", [])
        checked = set()
        for i, a in enumerate(syms):
            for j, b in enumerate(syms):
                if j <= i or (a, b) in checked:
                    continue
                checked.add((a, b))
                if a not in open_syms or b not in open_syms:
                    continue
                corr_val = matrix[i][j] if matrix else 0
                if abs(corr_val) < 0.80:
                    continue
                # Warn if both have positions in the same direction
                common_sides = set(sides_by_sym.get(a, [])) & set(sides_by_sym.get(b, []))
                if common_sides:
                    corr_warnings.append({
                        "coins": [a, b],
                        "corr": corr_val,
                        "side": list(common_sides)[0],
                    })

    return {
        "open_positions": open_positions,
        "closed_recent": [dict(r) for r in closed_recent],
        "total_open_pnl": round(total_open_pnl, 4),
        "today_pnl":  round(today_pnl + total_open_pnl, 4),   # closed today + open unrealized
        "week_pnl":   round(week_pnl  + total_open_pnl, 4),
        "total_closed_pnl": round(total_closed_pnl, 4),
        "max_drawdown": round(-max_dd, 4),
        "total_exposure": round(total_exposure, 2),
        "exposure_by_sym": {k: round(v, 2) for k, v in exposure_by_sym.items()},
        "leverage": leverage,
        "corr_warnings": corr_warnings,
        "settings": settings,
    }


def update_settings(**kwargs) -> dict:
    allowed = {"daily_stop", "weekly_stop", "account_size"}
    with _lock:
        con = _conn()
        for key, val in kwargs.items():
            if key in allowed:
                con.execute(
                    "INSERT OR REPLACE INTO risk_settings (key, value) VALUES (?, ?)", (key, val)
                )
        con.commit()
        settings = {r["key"]: r["value"] for r in con.execute("SELECT * FROM risk_settings")}
        con.close()
    return settings


# ── Journal ───────────────────────────────────────────────────────────────────

def log_setups(analyses: list):
    """Auto-loguje setupy z analýzy. Deduplikuje na 1H okno na symbol."""
    candle_ts = math.floor(time.time() / _JOURNAL_DEDUP_SECS) * _JOURNAL_DEDUP_SECS
    now = int(time.time())
    with _lock:
        con = _conn()
        for a in analyses:
            if "error" in a:
                continue
            try:
                con.execute(
                    """INSERT OR IGNORE INTO journal_entries
                       (symbol, ts, candle_ts, conclusion, long_pct, short_pct, wait_pct,
                        trader_score, long_entry, long_sl, short_entry, short_sl)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (a["symbol"], now, candle_ts,
                     a.get("conclusion"), a.get("long_pct"), a.get("short_pct"), a.get("wait_pct"),
                     a.get("trader_score"),
                     a.get("long", {}).get("entry"), a.get("long", {}).get("sl"),
                     a.get("short", {}).get("entry"), a.get("short", {}).get("sl")),
                )
            except Exception:
                pass
        con.commit()
        con.close()


def get_journal(limit: int = 150) -> dict:
    with _lock:
        con = _conn()
        entries = [dict(r) for r in con.execute(
            "SELECT * FROM journal_entries ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()]
        scored = con.execute(
            "SELECT conclusion, result, trader_score FROM journal_entries "
            "WHERE action IS NOT NULL AND result IS NOT NULL"
        ).fetchall()
        con.close()

    total  = len(scored)
    wins   = sum(1 for r in scored if r["result"] == "WIN")
    win_scores  = [r["trader_score"] for r in scored if r["result"] == "WIN"  and r["trader_score"]]
    loss_scores = [r["trader_score"] for r in scored if r["result"] == "LOSS" and r["trader_score"]]
    by_conclusion: dict = {}
    for r in scored:
        c = r["conclusion"] or "?"
        if c not in by_conclusion:
            by_conclusion[c] = {"total": 0, "wins": 0}
        by_conclusion[c]["total"] += 1
        if r["result"] == "WIN":
            by_conclusion[c]["wins"] += 1

    return {
        "entries": entries,
        "stats": {
            "total_with_result": total,
            "wins": wins,
            "win_rate": round(wins / total * 100, 1) if total else None,
            "avg_score_win":  round(sum(win_scores)  / len(win_scores),  1) if win_scores  else None,
            "avg_score_loss": round(sum(loss_scores) / len(loss_scores), 1) if loss_scores else None,
            "by_conclusion": by_conclusion,
        },
    }


def update_journal_entry(entry_id: int, **kwargs) -> bool:
    allowed = {"action", "result", "notes", "pnl_usdt"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    set_clause = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        con = _conn()
        rows = con.execute(
            f"UPDATE journal_entries SET {set_clause} WHERE id=?",
            (*fields.values(), entry_id),
        ).rowcount
        con.commit()
        con.close()
    return rows > 0
