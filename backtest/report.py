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


def render_comparison_report(
    variants: list[dict],          # [{"label": str, "results": {symbol: data}}, ...]
    buy_hold: dict,                # {symbol: {"return_pct": float, ...}}
) -> str:
    """
    Srovnávací report: všechny varianty prahu side-by-side pro každý symbol.
    variants = [{"label": "Baseline (≥45)", "results": {...}}, ...]
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    symbols = list(variants[0]["results"].keys()) if variants else []
    v_labels = [v["label"] for v in variants]

    # ── Metodika ──────────────────────────────────────────────────────────────
    header = (
        f"# Backtest — Srovnání D vs E (confluence varianty)\n"
        f"Vygenerováno: {now}\n\n"
        f"## Testované varianty\n\n"
        f"| Varianta | Podmínky |\n"
        f"|---|---|\n"
        f"| D | HTF=trend, STF=trend, RSI v pásmu, close > VWAP, close > POC (5 podmínek) |\n"
        f"| E | vše z D + Volatility Regime=TRENDING + poslední BOS souhlasí + RSI divergence neblokuje (8 podmínek) |\n\n"
        f"Každá varianta testována s pákou **1×, 3×, 5×** (margin model: 1 % účtu per obchod).\n\n"
        f"**SL/TP:** {ATR_SL_MULT}× ATR (1:1 R:R)  |  "
        f"**Entry:** open příšti svíčky (no look-ahead)  |  "
        f"**Fees sloupec:** 0.04 % taker round-trip\n\n"
        f"---\n\n"
    )

    # ── Per-symbol tabulky ────────────────────────────────────────────────────
    sections = []
    for symbol in symbols:
        sym = symbol.replace("/USDT", "")
        bh_ret = buy_hold.get(symbol, {}).get("return_pct")
        bh_str = _pct(bh_ret) if bh_ret is not None else "N/A"

        # Zjisti periodu z první varianty
        first_data = variants[0]["results"].get(symbol, {})
        period = (
            f"{first_data.get('period_start','?')[:10]} → "
            f"{first_data.get('period_end','?')[:10]}"
        )

        # Sestavíme tabulku řádků × variant
        col_w = 14
        sep_col = "|" + "|".join(["-"*(col_w+2)] * (len(v_labels)+1)) + "|\n"

        def hdr_row(label):
            cells = [f" {label:<22} "] + [f" {lbl:^{col_w}} " for lbl in v_labels]
            return "|" + "|".join(cells) + "|\n"

        def data_row(label, values):
            cells = [f" {label:<22} "] + [f" {v:^{col_w}} " for v in values]
            return "|" + "|".join(cells) + "|\n"

        tbl  = hdr_row("Metrika")
        tbl += sep_col

        def get(v_idx, key, subkey=None):
            d = variants[v_idx]["results"].get(symbol, {})
            if "error" in d:
                return "ERR"
            if subkey:
                return d.get(key, {}).get(subkey, "?")
            return d.get(key, "?")

        def s0(v_idx, key):
            d = variants[v_idx]["results"].get(symbol, {})
            if "error" in d:
                return "ERR"
            return d.get("stats_no_fees", {}).get(key, "?")

        def sf(v_idx, key):
            d = variants[v_idx]["results"].get(symbol, {})
            if "error" in d:
                return "ERR"
            return d.get("stats_with_fees", {}).get(key, "?")

        tbl += data_row("Počet obchodů",  [str(s0(i, "n_trades"))   for i in range(len(variants))])
        tbl += data_row("Win rate",       [f"{s0(i,'win_rate')} %"  for i in range(len(variants))])
        tbl += data_row("Expectancy",     [_r(s0(i,"expectancy_r")) for i in range(len(variants))])
        tbl += data_row("Výnos fees=0",   [_pct(s0(i,"total_return_pct")) for i in range(len(variants))])
        tbl += data_row("Výnos fees=0.04%", [_pct(sf(i,"total_return_pct")) for i in range(len(variants))])
        tbl += data_row("Max drawdown",   [_pct(s0(i,"max_dd_pct")) for i in range(len(variants))])
        tbl += data_row("Avg R:R",        [str(s0(i,"avg_rr"))      for i in range(len(variants))])
        tbl += data_row(f"Buy&Hold ({bh_str})",
                        [bh_str] + ["(stejné)" for _ in range(len(variants)-1)])

        # HYPE small-sample flag
        small_warns = []
        for i, v in enumerate(variants):
            d = v["results"].get(symbol, {})
            if d.get("is_small_sample"):
                small_warns.append(f"{v['label']}: {d.get('stats_no_fees',{}).get('n_trades','?')} obchodů")

        small_note = ""
        if small_warns:
            small_note = f"\n> ⚠️  **MALÝ VZOREK** ({sym}) — {'; '.join(small_warns)}\n"

        sections.append(f"### {sym} — {period}\n{small_note}\n{tbl}")

    # ── Souhrnná tabulka napříč symboly (výnos s fees=0.04%) ─────────────────
    summary_rows = []
    for symbol in symbols:
        sym = symbol.replace("/USDT", "")
        row_vals = []
        for i in range(len(variants)):
            d = variants[i]["results"].get(symbol, {})
            if "error" in d:
                row_vals.append("ERR")
            else:
                val = d.get("stats_with_fees", {}).get("total_return_pct")
                row_vals.append(_pct(val))
        bh_ret = buy_hold.get(symbol, {}).get("return_pct")
        row_vals.append(_pct(bh_ret))
        summary_rows.append((sym, row_vals))

    sum_hdr = "| Symbol | " + " | ".join(v_labels) + " | Buy&Hold |\n"
    sum_sep = "|---|" + "---|" * (len(v_labels) + 1) + "\n"
    sum_body = ""
    for sym, vals in summary_rows:
        sum_body += f"| {sym} | " + " | ".join(vals) + " |\n"

    summary = (
        f"## Souhrnná tabulka — výnos s fees=0.04 % (1 % risk/trade)\n\n"
        f"{sum_hdr}{sum_sep}{sum_body}\n"
    )

    return header + summary + "---\n\n## Detail po symbolech\n\n" + "\n\n---\n\n".join(sections)


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
