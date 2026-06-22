#!/usr/bin/env python3
"""
CLI spouštěč backtestingu — srovnání E2 vs G.

Varianty:
  E2  Konfluence (8 binarnich podminek), RSI LONG [40-70] / SHORT [30-60]
      Vstup: open nasledujici svicky po signalu
  H   E2 + 9. podminka (Weekly Open bias) + vstup na time-based level:
        - 9. podminka: close > Weekly Open povoli LONG, close < Weekly Open povoli SHORT
        - Vstup: limit order na nearest support (VAL/Wkly Open/Mon Low/Monthly Open)
                 nebo nearest resistance (VAH/Wkly High/Mon High/Prev Wk High)
        - Timeout: limit expiruje po 24 barech, pokud cena nedosahne urovne
        - SL/TP: standardni 1.5xATR od entry

Kazda varianta testovana s pakou 3x a 5x.

Pouziti:
  python run_backtest.py
  python run_backtest.py --symbols BTC/USDT SOL/USDT ETH/USDT HYPE/USDT
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
                    default=["BTC/USDT", "SOL/USDT"])
parser.add_argument("--years",   type=int, default=3)
parser.add_argument("--output",  default="backtest_report.md")
parser.add_argument("--no-cache", action="store_true")
parser.add_argument("--fees",    type=float, default=0.04,
                    help="Taker fee %% pro 'with fees' sloupec (default: 0.04)")
args = parser.parse_args()

# ── Definice variant ──────────────────────────────────────────────────────────
_E2 = {"signal_mode": "confluence_e", "rsi_lo": 40.0, "rsi_hi": 70.0,
       "threshold": 45, "require_htf_confirm": False, "score_threshold": 65.0}
_H  = {"signal_mode": "variant_h",    "rsi_lo": 40.0, "rsi_hi": 70.0,
       "threshold": 45, "require_htf_confirm": False, "score_threshold": 65.0}

VARIANTS = [
    # E2 (baseline): 8 podminek, vstup na dalsi open
    {**_E2, "label": "E2 3x", "leverage": 3.0},
    {**_E2, "label": "E2 5x", "leverage": 5.0},
    # H: E2 + Weekly Open jako 9. podminka + limit vstup na time-based level
    {**_H,  "label": "H  3x", "leverage": 3.0},
    {**_H,  "label": "H  5x", "leverage": 5.0},
]

print("=" * 64)
print("  Crypto Analyzer -- Backtest E2 vs H (Weekly Open + time levels)")
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
print(f"[2/3] Spouštím backtest ({len(VARIANTS)} variant × {len(args.symbols)} symboly)...")

# variant_results[i] = {"label": str, "results": {symbol: data_dict}}
variant_results: list[dict] = []
buy_hold: dict = {}

for v in VARIANTS:
    v_res: dict = {}
    print(f"\n  Varianta: {v['label']}")

    for symbol in args.symbols:
        dfs = all_data.get(symbol, {})

        rsi_lo  = v.get("rsi_lo", 45.0)
        rsi_hi  = v.get("rsi_hi", 65.0)
        s_thr   = v.get("score_threshold", 65.0)
        # fees = 0
        res0 = run_symbol_backtest(
            symbol, dfs, fees_pct=0.0,
            threshold=v["threshold"],
            require_htf_confirm=v["require_htf_confirm"],
            signal_mode=v["signal_mode"],
            leverage=v["leverage"],
            rsi_lo=rsi_lo, rsi_hi=rsi_hi,
            score_threshold=s_thr,
        )
        # fees = args.fees
        resf = run_symbol_backtest(
            symbol, dfs, fees_pct=args.fees,
            threshold=v["threshold"],
            require_htf_confirm=v["require_htf_confirm"],
            signal_mode=v["signal_mode"],
            leverage=v["leverage"],
            rsi_lo=rsi_lo, rsi_hi=rsi_hi,
            score_threshold=s_thr,
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
print(f"\n[3/3] Generuji srovnavaci report -> {args.output}...")
report = render_comparison_report(variant_results, buy_hold)
Path(args.output).write_text(report, encoding="utf-8")
print(f"Hotovo! Report: {Path(args.output).resolve()}")
print()
