"""
Markdown report z výsledků backtestingu.
"""

from datetime import datetime

from backtest.engine import ATR_SL_MULT, ATR_TP_MULT, TRADE_TIMEOUT_BARS


def _pct(val) -> str:
    if val is None:
        return "N/A"
    sign = "+" if float(val) >= 0 else ""
    return f"{sign}{float(val):.2f} %"


def _r(val) -> str:
    if val is None:
        return "N/A"
    sign = "+" if float(val) >= 0 else ""
    return f"{sign}{float(val):.3f} R"


def _row(label: str, v0: str, vf: str) -> str:
    return f"| {label:<32} | {v0:>16} | {vf:>16} |\n"


def _symbol_section(symbol: str, data: dict) -> str:
    if "error" in data:
        return f"\n### {symbol.replace('/USDT','')}\n⚠️  {data['error']}\n"

    s0       = data.get("stats_no_fees",   {})
    sf       = data.get("stats_with_fees", {})
    baseline = data.get("baseline",        {})
    bh       = data.get("buy_hold",        {})
    sym      = symbol.replace("/USDT", "")
    n_trades = s0.get("n_trades", 0)
    n_signals = data.get("n_signals", "?")
    start    = data.get("period_start", "?")[:10]
    end      = data.get("period_end",   "?")[:10]

    # ── HYPE small-sample warning ────────────────────────────────────────────
    small_warn = ""
    if data.get("is_small_sample"):
        small_warn = (
            f"\n> ⚠️  **MALÝ VZOREK — {n_trades} obchodů** "
            f"({sym} má krátkou historii na Binance/Bybit; HYPE kotace vznikla koncem 2024). "
            f"Výsledky nejsou statisticky spolehlivé. Interpretuj s maximální opatrností.\n"
        )

    # ── Statistická tabulka fees = 0 vs fees = 0.04 % ───────────────────────
    tbl  = (
        f"\n| {'Metrika':<32} | {'fees = 0 %':>16} | {'fees = 0.04 %':>16} |\n"
        f"|{'-'*34}|{'-'*18}|{'-'*18}|\n"
    )
    tbl += _row("Počet obchodů",         str(n_trades),                     str(sf.get("n_trades","?")))
    tbl += _row("  z toho LONG",         str(s0.get("n_long","?")),         str(sf.get("n_long","?")))
    tbl += _row("  z toho SHORT",        str(s0.get("n_short","?")),        str(sf.get("n_short","?")))
    tbl += _row("Win rate",              f"{s0.get('win_rate','?')} %",     f"{sf.get('win_rate','?')} %")
    tbl += _row("Avg vítězný obchod",    _r(s0.get("avg_win_r")),           _r(sf.get("avg_win_r")))
    tbl += _row("Avg ztrátový obchod",   _r(s0.get("avg_loss_r")),          _r(sf.get("avg_loss_r")))
    tbl += _row("Avg R:R",               str(s0.get("avg_rr","?")),         str(sf.get("avg_rr","?")))
    tbl += _row("Expectancy",            _r(s0.get("expectancy_r")),        _r(sf.get("expectancy_r")))
    tbl += _row("Celkový výnos (1% risk)",_pct(s0.get("total_return_pct")), _pct(sf.get("total_return_pct")))
    tbl += _row("Max drawdown",          _pct(s0.get("max_dd_pct")),        _pct(sf.get("max_dd_pct")))
    tbl += _row("Průměrná délka (1H)",   f"{s0.get('avg_bars_held','?')} barů", "—")

    # ── Exit důvody ─────────────────────────────────────────────────────────
    exit_str = "  ".join(
        f"**{k}**: {v}" for k, v in s0.get("exit_counts", {}).items()
    ) or "žádné obchody"

    # ── Random baseline ──────────────────────────────────────────────────────
    base_block = ""
    if baseline and baseline.get("n_trades", 0) > 0:
        sys_ret  = s0.get("total_return_pct", 0.0) or 0.0
        p95      = baseline.get("return_p95", 9999)
        edge_str = (
            "✅ **EDGE nad 95. percentilem baseline** (systém překonává náhodné zadávání)"
            if sys_ret > p95 else
            "⚠️  **žádný jasný edge nad baseline** (výnos leží v náhodném pásmu)"
        )
        base_block = (
            f"\n**Random baseline** "
            f"(Monte Carlo {baseline.get('n_sim',1000)} simulací, "
            f"symetrický flip směru LONG↔SHORT):\n"
            f"- Výnos: medián **{_pct(baseline.get('return_median'))}**,  "
            f"rozsah 5–95 %: {_pct(baseline.get('return_p5'))} … {_pct(baseline.get('return_p95'))}\n"
            f"- Win rate: {baseline.get('win_rate_median','?')} %\n"
            f"- Hodnocení: {edge_str}\n"
        )

    # ── Buy & Hold ───────────────────────────────────────────────────────────
    bh_block = ""
    if bh:
        bh_block = (
            f"\n**Buy & Hold** ({start} → {end}):\n"
            f"- Vstup: {bh.get('entry_price')} → Výstup: {bh.get('exit_price')} "
            f"= **{_pct(bh.get('return_pct'))}**\n"
        )

    return (
        f"\n### {sym}  —  {start} → {end}\n"
        f"{small_warn}"
        f"Signálů celkem: **{n_signals}**  |  Obchodů otevřeno: **{n_trades}**  "
        f"(1 pozice najednou, timeout {TRADE_TIMEOUT_BARS}H)\n"
        f"{tbl}\n"
        f"Exit důvody: {exit_str}\n"
        f"{base_block}"
        f"{bh_block}"
    )


def render_report(all_results: dict) -> str:
    now      = datetime.now().strftime("%Y-%m-%d %H:%M")
    sections = [_symbol_section(sym, data) for sym, data in all_results.items()]

    return (
        f"# Backtest Report — BTC / ETH / SOL / HYPE\n"
        f"Vygenerováno: {now}\n\n"
        f"## Metodika\n\n"
        f"| Parametr | Hodnota |\n"
        f"|---|---|\n"
        f"| Trigger timeframe | 1H (signál po uzavření každé 1H svíčky) |\n"
        f"| Vstupní cena | open příští 1H svíčky (NO LOOK-AHEAD) |\n"
        f"| SL | {ATR_SL_MULT}× ATR (live systém: Volume Profile) |\n"
        f"| TP1 | {ATR_TP_MULT}× ATR — R:R = 1:1 |\n"
        f"| Intrabar konflikt (SL + TP ve stejné svíčce) | SL vítězí (pesimisticky) |\n"
        f"| Timeout | {TRADE_TIMEOUT_BARS} barů (2 dny) → close na close svíčky |\n"
        f"| Risk per trade | 1 % kapitálu (fixní, pro equity curve) |\n"
        f"| Fees | vždy side-by-side: 0 % vs. 0.04 % taker (round-trip 2× 0.04 %) |\n"
        f"| Ichimoku shift | shift(+26) ověřen — žádný look-ahead bias ✓ |\n"
        f"| Signálová logika | Identická s live systémem (bez funding rate) |\n"
        f"| Random baseline | Monte Carlo 1 000 sim., symetrický flip LONG↔SHORT |\n\n"
        f"> ⚠️  **Upozornění:** Backtest nezahrnuje slippage, partial fills, gaps v datech,\n"
        f"> a SL/TP jsou zjednodušeny na ATR místo Volume Profile. Minulá výkonnost\n"
        f"> NEZARUČUJE výsledky do budoucnosti. Nepředstavuje finanční poradenství.\n\n"
        f"---\n"
        + "\n---\n".join(sections)
    )
