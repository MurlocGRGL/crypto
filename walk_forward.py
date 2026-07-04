#!/usr/bin/env python3
"""
Walk-forward / temporal-stability analýza LIVE E2 strategie.

PROČ TAKHLE:
  LIVE varianta nemá laděné parametry (9 podmínek + fixní 1.5×ATR SL/TP).
  Engine je no-look-ahead → KAŽDÝ obchod už je out-of-sample, žádný fitting.
  Otázka tedy není "train/test split parametrů", ale:
    Je edge STABILNÍ napříč obdobími, nebo ho táhne jedno šťastné okno?

JAK:
  1. Spustí plný backtest (indikátory vždy s plnou historií = jako live).
  2. Rozdělí obchody podle data vstupu do kalendářních půlroků (H1/H2).
  3. Per půlrok: počet obchodů, win rate, expectancy R, výnos období.
  Konzistentně kladná expectancy napříč půlroky = edge není artefakt období.

Použití:
  python walk_forward.py
  python walk_forward.py --symbols SOL/USDT BTC/USDT --years 3 --leverage 3 --fees 0.04
"""

import argparse
from collections import defaultdict

from backtest.data   import load_all
from backtest.engine import run_symbol_backtest
from backtest.stats  import compute_stats

# ── CLI ─────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Walk-forward temporal stability — LIVE E2")
parser.add_argument("--symbols", nargs="+",
                    default=["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"])
parser.add_argument("--years",    type=int,   default=3)
parser.add_argument("--leverage", type=float, default=3.0)
parser.add_argument("--fees",     type=float, default=0.04)
args = parser.parse_args()


def _half_key(entry_ts: str) -> str:
    """'2024-03-15 12:00:00+00:00' -> '2024-H1'."""
    date = entry_ts[:10]
    year, month = date[:4], int(date[5:7])
    return f"{year}-H{1 if month <= 6 else 2}"


def _fold_stats(trades: list) -> dict:
    """Statistiky jednoho půlroku z jeho obchodů (výnos = compounding pnl_pct)."""
    if not trades:
        return {"n": 0}
    wins = sum(1 for t in trades if t.get("win"))
    exp  = sum(t["pnl_r"] for t in trades) / len(trades)
    eq   = 1.0
    for t in trades:
        eq *= (1.0 + t["pnl_pct"] / 100.0)
    return {
        "n":      len(trades),
        "wr":     round(wins / len(trades) * 100, 1),
        "exp":    round(exp, 3),
        "ret":    round((eq - 1.0) * 100, 2),
    }


print("=" * 78)
print(f"  Walk-forward (temporal stability) — LIVE E2  |  páka {args.leverage}×  fees {args.fees}%")
print("=" * 78)

all_data = load_all(args.symbols, ["1h", "4h", "1d"], years=args.years)
print()

# všechny kalendářní půlroky napříč symboly (pro zarovnané sloupce)
all_halves: set = set()
symbol_folds: dict = {}
symbol_overall: dict = {}

for symbol in args.symbols:
    dfs = all_data.get(symbol, {})
    res = run_symbol_backtest(
        symbol, dfs, fees_pct=args.fees,
        threshold=45, signal_mode="live_e2",
        leverage=args.leverage, rsi_lo=40.0, rsi_hi=70.0,
    )
    if "error" in res:
        print(f"{symbol}: CHYBA — {res['error']}")
        continue

    buckets: dict = defaultdict(list)
    for t in res["trades"]:
        buckets[_half_key(t["entry_ts"])].append(t)

    folds = {h: _fold_stats(ts) for h, ts in buckets.items()}
    symbol_folds[symbol]   = folds
    symbol_overall[symbol] = compute_stats(res["trades"], res["equity"], fees_pct=args.fees)
    all_halves.update(folds.keys())

halves = sorted(all_halves)

# ── Tabulka výnosů po půlrocích ─────────────────────────────────────────────────
print("VÝNOS PO PŮLROCÍCH (%)  —  kladné okno = edge přítomný v tom období\n")
hdr = f"{'Symbol':10}" + "".join(f"{h:>10}" for h in halves) + f"{'  | celk.':>12}{'  poz/celk':>10}"
print(hdr)
print("-" * len(hdr))
for symbol in args.symbols:
    folds = symbol_folds.get(symbol)
    if not folds:
        continue
    row = f"{symbol.replace('/USDT',''):10}"
    pos = tot = 0
    for h in halves:
        f = folds.get(h)
        if f and f["n"] > 0:
            row += f"{f['ret']:>+10.2f}"
            tot += 1
            pos += 1 if f["ret"] > 0 else 0
        else:
            row += f"{'—':>10}"
    ov = symbol_overall[symbol]["total_return_pct"]
    row += f"{ov:>+12.2f}{f'{pos}/{tot}':>10}"
    print(row)

# ── Tabulka expectancy po půlrocích ─────────────────────────────────────────────
print("\nEXPECTANCY PO PŮLROCÍCH (R/obchod)  —  konzistentně kladná = robustní edge\n")
print(hdr.replace("  | celk.", " |  celk.R"))
print("-" * len(hdr))
for symbol in args.symbols:
    folds = symbol_folds.get(symbol)
    if not folds:
        continue
    row = f"{symbol.replace('/USDT',''):10}"
    pos = tot = 0
    for h in halves:
        f = folds.get(h)
        if f and f["n"] > 0:
            row += f"{f['exp']:>+10.3f}"
            tot += 1
            pos += 1 if f["exp"] > 0 else 0
        else:
            row += f"{'—':>10}"
    ov = symbol_overall[symbol]["expectancy_r"]
    row += f"{ov:>+12.3f}{f'{pos}/{tot}':>10}"
    print(row)

# ── Verdikt ─────────────────────────────────────────────────────────────────────
print("\n" + "=" * 78)
print("  VERDIKT")
print("=" * 78)
for symbol in args.symbols:
    folds = symbol_folds.get(symbol)
    if not folds:
        continue
    active = [f for f in folds.values() if f["n"] > 0]
    pos_exp = sum(1 for f in active if f["exp"] > 0)
    ov = symbol_overall[symbol]
    n_half = len(active)
    sym = symbol.replace("/USDT", "")
    if n_half == 0:
        continue
    frac = pos_exp / n_half
    if frac >= 0.75 and ov["expectancy_r"] > 0:
        tag = "ROBUSTNI - edge kladny ve vetsine obdobi"
    elif frac >= 0.5 and ov["expectancy_r"] > 0:
        tag = "SMISENY - edge nekonzistentni, opatrne"
    else:
        tag = "NESPOLEHLIVY - edge nedrzi mimo stastna okna"
    print(f"  {sym:6} exp {ov['expectancy_r']:+.3f}R celkove | "
          f"kladna expectancy {pos_exp}/{n_half} pulroku -> {tag}")
print()
