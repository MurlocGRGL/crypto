#!/usr/bin/env python3
"""
CLI spouštěč backtestingu — testuje více variant signálového prahu najednou.

Varianty:
  Baseline  long_pct/short_pct >= 45  (stávající live chování)
  A         long_pct/short_pct >= 60
  B         long_pct/short_pct >= 65
  C         long_pct/short_pct >= 60 AND HTF trend souhlasí se směrem

Použití:
  python run_backtest.py
  python run_backtest.py --symbols BTC/USDT ETH/USDT
  python run_backtest.py --years 1
  python run_backtest.py --output result.md
  python run_backtest.py --no-cache
"""

import argparse
import glob
from pathlib import Path

# ── CLI ────────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Crypto Analyzer — Backtest (multi-variant)")
parser.add_argument("--symbols", nargs="+",
                    default=["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"])
parser.add_argument("--years",   type=int, default=3)
parser.add_argument("--output",  default="backtest_report.md")
parser.add_argument("--no-cache", action="store_true")
parser.add_argument("--fees",    type=float, default=0.04,
                    help="Taker fee %% pro 'with fees' sloupec (default: 0.04)")
args = parser.parse_args()

# ── Definice variant ──────────────────────────────────────────────────────────
VARIANTS = [
    {"label": "Baseline (≥45)", "threshold": 45, "require_htf_confirm": False, "signal_mode": "score"},
    {"label": "A (≥60)",        "threshold": 60, "require_htf_confirm": False, "signal_mode": "score"},
    {"label": "B (≥65)",        "threshold": 65, "require_htf_confirm": False, "signal_mode": "score"},
    {"label": "C (≥60+HTF)",    "threshold": 60, "require_htf_confirm": True,  "signal_mode": "score"},
    {"label": "D (konfluence)", "threshold": 45, "require_htf_confirm": False, "signal_mode": "confluence"},
]

print("=" * 64)
print("  Crypto Analyzer — Backtest (Baseline + Varianty A, B, C)")
print("=" * 64)

# ── Imports ────────────────────────────────────────────────────────────────────
from backtest.data   import load_all
from backtest.engine import run_symbol_backtest, MIN_BARS
from backtest.stats  import compute_stats, compute_random_baseline, compute_buy_hold
from backtest.report import render_comparison_report

# ── Cache ──────────────────────────────────────────────────────────────────────
if args.no_cache:
    removed = sum(1 for f in glob.glob("backtest_data/*.csv")
                  if not Path(f).unlink())
    print(f"Cache vymazána.\n")

# ── [1/3] Data ─────────────────────────────────────────────────────────────────
print("[1/3] Načítám historická data...")
all_data = load_all(args.symbols, ["1h", "4h", "1d"], years=args.years)
print()

# ── [2/3] Backtest všech variant ──────────────────────────────────────────────
print(f"[2/3] Spouštím backtest ({len(VARIANTS)} varianty × {len(args.symbols)} symboly)...")

# variant_results[i] = {"label": str, "results": {symbol: data_dict}}
variant_results: list[dict] = []
buy_hold: dict = {}

for v in VARIANTS:
    v_res: dict = {}
    print(f"\n  Varianta: {v['label']}")

    for symbol in args.symbols:
        dfs = all_data.get(symbol, {})

        # fees = 0
        res0 = run_symbol_backtest(
            symbol, dfs, fees_pct=0.0,
            threshold=v["threshold"],
            require_htf_confirm=v["require_htf_confirm"],
            signal_mode=v["signal_mode"],
        )
        # fees = args.fees
        resf = run_symbol_backtest(
            symbol, dfs, fees_pct=args.fees,
            threshold=v["threshold"],
            require_htf_confirm=v["require_htf_confirm"],
            signal_mode=v["signal_mode"],
        )

        if "error" in res0:
            v_res[symbol] = res0
            print(f"    {symbol}: CHYBA — {res0['error']}")
            continue

        stats0   = compute_stats(res0["trades"], res0["equity"], fees_pct=0.0)
        statsf   = compute_stats(resf["trades"], resf["equity"], fees_pct=args.fees)
        baseline = compute_random_baseline(res0["trades"])

        # Buy&Hold se počítá jen jednou (stejné pro všechny varianty)
        if symbol not in buy_hold:
            buy_hold[symbol] = compute_buy_hold(dfs.get("1h"), MIN_BARS)

        v_res[symbol] = {
            **res0,
            "stats_no_fees":   stats0,
            "stats_with_fees": statsf,
            "baseline":        baseline,
        }

        n  = stats0["n_trades"]
        wr = stats0.get("win_rate", "?")
        r0 = stats0.get("total_return_pct", "?")
        rf = statsf.get("total_return_pct", "?")
        small = " ⚠️ MALÝ VZOREK" if res0.get("is_small_sample") else ""
        print(f"    {symbol:12} {n:4} obchodů | WR {wr}% | {r0:+.2f}% (0%) / {rf:+.2f}% ({args.fees}%){small}"
              if isinstance(r0, float) and isinstance(rf, float)
              else f"    {symbol:12} {n} obchodů{small}")

    variant_results.append({"label": v["label"], "results": v_res})

# ── [3/3] Report ───────────────────────────────────────────────────────────────
print(f"\n[3/3] Generuji srovnávací report → {args.output}...")
report = render_comparison_report(variant_results, buy_hold)
Path(args.output).write_text(report, encoding="utf-8")
print(f"Hotovo! Report: {Path(args.output).resolve()}")
print()
