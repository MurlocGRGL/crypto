"""
Hlavní vstupní bod.

Spuštění:
    python main.py            -> běží furt dokola (interval viz config.py)
    python main.py --once     -> stáhne data, vygeneruje report jednou a skončí

Report se vždy uloží do reports/latest_report.md (přepisuje se)
a navíc do reports/report_<timestamp>.md (historie).
"""

import argparse
import os
import time
import traceback
from datetime import datetime

import config
from data_fetcher import DataFetcher
from indicators import analyze_timeframe, correlation_with_btc
from report_generator import build_symbol_analysis, render_full_report, _trend_from_ichimoku_text


def run_cycle(fetcher: DataFetcher):
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] Stahuji data...")
    raw_data = fetcher.fetch_all(config.SYMBOLS, config.TIMEFRAMES, limit=config.CANDLE_LIMIT)

    analyzed = {}
    for symbol, tf_dict in raw_data.items():
        analyzed[symbol] = {tf: analyze_timeframe(df) for tf, df in tf_dict.items()}

    # BTC trend a 1H data pro korelaci - zjistíme první, ostatní mince se vůči tomu poměřují
    btc_trend = None
    btc_df_1h = raw_data.get("BTC/USDT", {}).get("1h")
    if "BTC/USDT" in analyzed:
        btc_tf = analyzed["BTC/USDT"].get("4h") or analyzed["BTC/USDT"].get("1d")
        if btc_tf:
            btc_trend = _trend_from_ichimoku_text(btc_tf["ichimoku_text"])

    fear_greed = fetcher.fetch_fear_greed()

    analyses = []
    for symbol in config.SYMBOLS:
        corr = None
        if symbol != "BTC/USDT" and btc_df_1h is not None:
            corr = correlation_with_btc(raw_data.get(symbol, {}).get("1h"), btc_df_1h)

        funding_rate, open_interest = fetcher.fetch_funding_and_oi(symbol)
        ls_long, ls_short = fetcher.fetch_long_short_ratio(symbol)
        oi_history = fetcher.fetch_oi_history(symbol)

        a = build_symbol_analysis(
            symbol,
            analyzed.get(symbol, {}),
            btc_trend=btc_trend,
            correlation_btc=corr,
            funding_rate=funding_rate,
            open_interest=open_interest,
            ls_long=ls_long,
            ls_short=ls_short,
            oi_history=oi_history,
            fear_greed=fear_greed,
        )
        analyses.append(a)

    report = render_full_report(analyses)

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    latest_path = os.path.join(config.OUTPUT_DIR, config.LATEST_REPORT_FILENAME)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(report)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    history_path = os.path.join(config.OUTPUT_DIR, f"report_{ts}.md")
    with open(history_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"Hotovo. Report uložen do: {latest_path}")
    return report


def main():
    parser = argparse.ArgumentParser(description="BTC/ETH/SOL/HYPE technický analyzátor")
    parser.add_argument("--once", action="store_true", help="Spustit jen jeden cyklus a skončit")
    args = parser.parse_args()

    fetcher = DataFetcher()

    if args.once:
        report = run_cycle(fetcher)
        print("\n" + "=" * 60)
        print(report)
        return

    print(f"Spouštím nepřetržité sledování (interval: {config.LOOP_INTERVAL_SECONDS}s). Ctrl+C pro ukončení.")
    while True:
        try:
            run_cycle(fetcher)
        except Exception:
            print("[CHYBA] Cyklus selhal, zkusím to znovu za chvíli:")
            traceback.print_exc()
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
