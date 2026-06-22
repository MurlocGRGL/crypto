#!/usr/bin/env python3
"""
CLI spouštěč backtestingu.

Použití:
  python run_backtest.py                      # všechny symboly, 3 roky
  python run_backtest.py --symbols BTC/USDT ETH/USDT
  python run_backtest.py --years 1
  python run_backtest.py --output muj_report.md
  python run_backtest.py --no-cache           # smaž cache a stáhni znovu
"""

import argparse
import glob
import sys
from pathlib import Path

# ── CLI argumenty ──────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(
    description="Crypto Analyzer — Backtest Engine",
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
parser.add_argument(
    "--symbols", nargs="+",
    default=["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"],
    help="Symboly k testování (default: BTC ETH SOL HYPE)",
)
parser.add_argument(
    "--years", type=int, default=3,
    help="Délka history v letech (default: 3)",
)
parser.add_argument(
    "--output", default="backtest_report.md",
    help="Výstupní soubor (default: backtest_report.md)",
)
parser.add_argument(
    "--no-cache", action="store_true",
    help="Smaž cache a stáhni data znovu",
)
args = parser.parse_args()

print("=" * 62)
print("  Crypto Analyzer — Backtest Engine")
print("=" * 62)

# ── Imports ────────────────────────────────────────────────────────────────────
from backtest.data   import load_all
from backtest.engine import run_symbol_backtest, MIN_BARS
from backtest.stats  import compute_stats, compute_random_baseline, compute_buy_hold
from backtest.report import render_report

# ── Volitelné smazání cache ────────────────────────────────────────────────────
if args.no_cache:
    removed = 0
    for f in glob.glob("backtest_data/*.csv"):
        Path(f).unlink()
        removed += 1
    print(f"Cache vymazána ({removed} souborů).\n")

# ── [1/3] Data ─────────────────────────────────────────────────────────────────
print("[1/3] Načítám historická data...")
all_data = load_all(args.symbols, ["1h", "4h", "1d"], years=args.years)
print()

# ── [2/3] Backtest ─────────────────────────────────────────────────────────────
print("[2/3] Spouštím backtest (může trvat minutu)...")
all_results: dict = {}

for symbol in args.symbols:
    dfs = all_data.get(symbol, {})
    print(f"\n  ► {symbol}")

    # fees = 0 %
    res0 = run_symbol_backtest(symbol, dfs, fees_pct=0.0)
    if "error" in res0:
        print(f"     CHYBA: {res0['error']}")
        all_results[symbol] = res0
        continue

    # fees = 0.04 % taker (identický běh se stejnými trades, jiné pnl_r)
    resf = run_symbol_backtest(symbol, dfs, fees_pct=0.04)

    stats0 = compute_stats(res0["trades"], res0["equity"], fees_pct=0.0)
    statsf = compute_stats(resf["trades"], resf["equity"], fees_pct=0.04)
    baseline = compute_random_baseline(res0["trades"])
    bh = compute_buy_hold(dfs.get("1h"), MIN_BARS)

    n  = stats0["n_trades"]
    wr = stats0.get("win_rate")
    r0 = stats0.get("total_return_pct")
    rf = statsf.get("total_return_pct")
    bh_r = bh.get("return_pct", "N/A")

    print(f"     Obchodů: {n}  |  Win rate: {wr} %")
    print(f"     Výnos (fees=0): {r0:+.2f} %  |  Výnos (fees=0.04%): {rf:+.2f} %" if isinstance(r0, float) and isinstance(rf, float) else "")
    print(f"     Buy & Hold stejné období: {bh_r:+.2f} %" if isinstance(bh_r, float) else "")
    if res0.get("is_small_sample"):
        print(f"     ⚠️  MALÝ VZOREK ({n} obchodů) — výsledky nejsou statisticky spolehlivé")

    all_results[symbol] = {
        **res0,
        "stats_no_fees":   stats0,
        "stats_with_fees": statsf,
        "baseline":        baseline,
        "buy_hold":        bh,
    }

# ── [3/3] Report ───────────────────────────────────────────────────────────────
print(f"\n[3/3] Generuji report → {args.output}...")
report = render_report(all_results)
Path(args.output).write_text(report, encoding="utf-8")
print(f"Hotovo! Report: {Path(args.output).resolve()}")
print()
