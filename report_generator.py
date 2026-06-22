"""
Sestaví kompletní report přesně ve formátu, který si nadefinoval:
- pro každou minci: Trend / Impuls / Long / Short / Invalidation / BTC vliv / Pravděpodobnost / Závěr
- na konci: Síla mincí, Trader Score, Momentum, Ideální vstupy, Nejlepší setup dne, Watchlist

DŮLEŽITÉ: Trader Score, Momentum % a Pravděpodobnosti jsou heuristiky odvozené
z indikátorů (pravidly níže), NE statisticky podložená predikce budoucího vývoje.
Slouží jako strukturovaný přehled, ne jako finanční doporučení.
"""

from datetime import datetime
import config
import indicators as ind


def _fmt(price, decimals=None):
    if price is None:
        return "N/A"
    if decimals is None:
        decimals = 2 if price >= 10 else 4
    return f"{price:,.{decimals}f}"


def _compute_e2_signal(
    htf_trend: str,
    stf_trend: str,
    rsi_val: float,
    last_price: float,
    price_vs_vwap: str,
    poc,
    volatility_regime: dict | None,
    market_structure: dict | None,
    divergence: str,
    time_levels: dict | None = None,
) -> tuple[str, dict]:
    """
    Vypočítá E2 signál (9 podmínek, RSI LONG 40–70 / SHORT 30–60).
    9. podmínka: cena nad/pod Weekly Open = bullish/bearish bias.
    Vrací (signal, checklist) kde signal = "LONG" / "SHORT" / "WAIT".
    """
    vol_str  = (volatility_regime or {}).get("regime", "")
    ms_str   = (market_structure or {}).get("structure", "")
    div_str  = divergence or ""
    vwap_str = (price_vs_vwap or "").lower()
    tl       = time_levels or {}
    wo       = tl.get("weekly_open")   # None = data nedostupná → podmínka neselhává

    long_cond = {
        "HTF BULLISH":       htf_trend == "BULLISH",
        "STF BULLISH":       stf_trend == "BULLISH",
        "RSI 40–70":         rsi_val is not None and 40.0 <= rsi_val <= 70.0,
        "Cena nad VWAP":     "nad" in vwap_str,
        "Cena nad POC":      poc is not None and last_price > poc,
        "Vol. TRENDING":     vol_str == "TRENDING",
        "BOS BULLISH":       ms_str == "BULLISH",
        "Bez bear. div.":    "BEARISH" not in div_str,
        "Nad Weekly Open":   wo is None or last_price > wo,
    }
    short_cond = {
        "HTF BEARISH":       htf_trend == "BEARISH",
        "STF BEARISH":       stf_trend == "BEARISH",
        "RSI 30–60":         rsi_val is not None and 30.0 <= rsi_val <= 60.0,
        "Cena pod VWAP":     "pod" in vwap_str,
        "Cena pod POC":      poc is not None and last_price < poc,
        "Vol. TRENDING":     vol_str == "TRENDING",
        "BOS BEARISH":       ms_str == "BEARISH",
        "Bez bull. div.":    "BULLISH" not in div_str,
        "Pod Weekly Open":   wo is None or last_price < wo,
    }

    if all(long_cond.values()):
        return "LONG", long_cond
    if all(short_cond.values()):
        return "SHORT", short_cond

    long_score  = sum(long_cond.values())
    short_score = sum(short_cond.values())
    checklist   = long_cond if long_score >= short_score else short_cond
    return "WAIT", checklist


def _trend_from_ichimoku_text(text: str) -> str:
    if "nad mrakem" in text:
        return "BULLISH"
    if "pod mrakem" in text:
        return "BEARISH"
    return "NEUTRÁLNÍ"


def build_symbol_analysis(
    symbol: str,
    tf_results: dict,
    btc_trend: str = None,
    correlation_btc: float = None,
    funding_rate: float = None,
    open_interest: float = None,
    ls_long: float = None,
    ls_short: float = None,
    oi_history: dict = None,
    fear_greed: dict = None,
    # Tier 2
    basis: dict = None,
    cvd: dict = None,
    options_data: dict = None,
    # Time-based price levels
    time_levels: dict = None,
) -> dict:
    """tf_results: {timeframe: analyze_timeframe() output or None}"""
    tf_1d = tf_results.get("1d")
    tf_4h = tf_results.get("4h")
    tf_1h = tf_results.get("1h")
    tf_15m = tf_results.get("15m")

    available = {tf: r for tf, r in tf_results.items() if r is not None}
    if not available:
        return {"symbol": symbol, "error": "Nedostatek dat pro analýzu"}

    # Higher timeframe trend (1D, fallback 4H)
    htf = tf_1d or tf_4h
    htf_trend = _trend_from_ichimoku_text(htf["ichimoku_text"]) if htf else "N/A"

    # Short-term trend (4H, fallback 1H)
    stf = tf_4h or tf_1h
    stf_trend = _trend_from_ichimoku_text(stf["ichimoku_text"]) if stf else "N/A"

    # Momentum reference (1H, fallback nejmenší dostupný TF)
    momentum_tf = tf_1h or tf_15m or list(available.values())[0]
    rsi_val = momentum_tf["rsi"]
    macd_data = momentum_tf.get("macd")
    bb_data = momentum_tf.get("bb")

    last_price = (tf_15m or tf_1h or tf_4h or tf_1d)["last_close"]

    # --- Trend / Impuls textově ---
    if htf_trend == stf_trend and htf_trend != "N/A":
        trend_text = f"{htf_trend} (shoda HTF i krátkodobého trendu)"
    else:
        trend_text = f"HTF: {htf_trend} / krátkodobě: {stf_trend} (rozpor mezi timeframy)"

    impuls_text = f"RSI({momentum_tf['rsi']:.0f}) na momentum TF, cena je {momentum_tf['price_vs_vwap']}, {momentum_tf['divergence']}"

    # --- Long / Short scénáře: kombinace Volume Profile + ATR + Time Levels ---
    entry_tf = tf_1h or tf_4h or htf
    vp = entry_tf["volume_profile"]
    swing_high = entry_tf["swing_high"]
    swing_low = entry_tf["swing_low"]
    atr_val = entry_tf.get("atr") or last_price * 0.005
    tl = time_levels or {}

    risk = max(atr_val * 1.5, abs(last_price - swing_low) * 0.5)

    # Long vstupní zóna: nejbližší podpora pod cenou z time levels (Monday Low, Weekly Open)
    # nebo VAL — whichever is higher (closest to current price)
    _long_supports = [vp["val"]]
    for _name in ("monday_low", "weekly_open", "monthly_open"):
        _lvl = tl.get(_name)
        if _lvl is not None and _lvl < last_price:
            _long_supports.append(_lvl)
    long_entry = max(_long_supports)
    long_entry = max(long_entry, last_price * 0.990)   # max 1 % pod cenou

    long_sl = min(swing_low, long_entry - risk)
    long_tp1 = vp["poc"] if vp["poc"] > long_entry else long_entry + risk
    long_tp2 = max(vp["vah"], swing_high)
    long_tp3 = long_entry + (long_tp2 - long_entry) * 1.6

    # Short vstupní zóna: nejbližší odpor nad cenou (Monday High, Weekly High) nebo VAH
    _short_resistances = [vp["vah"]]
    for _name in ("monday_high", "weekly_high", "prev_week_high", "prev_month_high"):
        _lvl = tl.get(_name)
        if _lvl is not None and _lvl > last_price:
            _short_resistances.append(_lvl)
    short_entry = min(_short_resistances)
    short_entry = min(short_entry, last_price * 1.010)  # max 1 % nad cenou

    short_sl = max(swing_high, short_entry + risk)
    short_tp1 = vp["poc"] if vp["poc"] < short_entry else short_entry - risk
    short_tp2 = min(vp["val"], swing_low)
    short_tp3 = short_entry - (short_entry - short_tp2) * 1.6

    invalidation = (
        f"Long scénář padá při uzavření svíčky pod {_fmt(long_sl)}. "
        f"Short scénář padá při uzavření svíčky nad {_fmt(short_sl)}."
    )

    # --- BTC vliv (teď i s číselnou korelací, ne jen textem) ---
    if symbol == "BTC/USDT":
        btc_influence = "Toto JE BTC analýza – ostatní altcoiny se odvíjí od něj."
    else:
        corr_text = f"korelace {correlation_btc:+.2f}" if correlation_btc is not None else "korelace neznámá"
        if btc_trend:
            btc_influence = f"BTC trend: {btc_trend} ({corr_text}). Pokud BTC neguje altcoin scénář, preferuj WAIT."
        else:
            btc_influence = f"BTC trend neznámý ({corr_text}) – ber jako neznámou proměnnou, sniž confidence."

    # --- Pravděpodobnosti (heuristika) ---
    trend_score = {"BULLISH": 1, "BEARISH": -1, "NEUTRÁLNÍ": 0, "N/A": 0}
    score = trend_score.get(htf_trend, 0) * 2 + trend_score.get(stf_trend, 0)
    rsi_bias = (rsi_val - 50) / 50  # -1..1
    score += rsi_bias * 2

    # Funding rate jako extra kontextový signál: extrémně kladný funding = přeplněný long
    # (kontrariánský tlak dolů), extrémně záporný = přeplněný short (tlak nahoru)
    funding_note = None
    if funding_rate is not None:
        funding_pct = funding_rate * 100
        if funding_pct > 0.05:
            score -= 0.5
            funding_note = f"Funding rate {funding_pct:+.3f}% (zvýšený - longy platí shortům, opatrně na chase longů)"
        elif funding_pct < -0.05:
            score += 0.5
            funding_note = f"Funding rate {funding_pct:+.3f}% (záporný - shorty platí longům, opatrně na chase shortů)"
        else:
            funding_note = f"Funding rate {funding_pct:+.3f}% (neutrální)"

    long_pct = max(5, min(80, round(50 + score * 12)))
    short_pct = max(5, min(80, round(50 - score * 12)))
    wait_pct = max(5, 100 - long_pct - short_pct)
    # normalizace na 100
    total = long_pct + short_pct + wait_pct
    long_pct, short_pct, wait_pct = (round(x * 100 / total) for x in (long_pct, short_pct, wait_pct))

    if long_pct >= short_pct and long_pct >= wait_pct and long_pct >= 45:
        conclusion = "🟢 LONG"
    elif short_pct >= long_pct and short_pct >= wait_pct and short_pct >= 45:
        conclusion = "🔴 SHORT"
    else:
        conclusion = "🟡 WAIT"

    # --- Trader Score & Momentum (pro souhrn) ---
    trader_score = round(50 + score * 10 + (10 if "žádná" not in entry_tf["divergence"] else 0))
    trader_score = max(0, min(100, trader_score))
    momentum_pct = round(rsi_bias * 100)

    # Market structure + volatility regime — per-timeframe, prefer 1h/4h for structure
    ms_tf = tf_1h or tf_4h or htf
    ms = ms_tf.get("market_structure") if ms_tf else None
    vr = ms_tf.get("volatility_regime") if ms_tf else None
    # Collect per-timeframe ms/vr for the detailed tab
    tf_ms_vr = {}
    for tf_name, tf_res in tf_results.items():
        if tf_res:
            tf_ms_vr[tf_name] = {
                "market_structure": tf_res.get("market_structure"),
                "volatility_regime": tf_res.get("volatility_regime"),
            }

    # E2 paper trading signal (9 podmínek — přidán Weekly Open bias)
    e2_signal, e2_checklist = _compute_e2_signal(
        htf_trend=htf_trend,
        stf_trend=stf_trend,
        rsi_val=rsi_val,
        last_price=last_price,
        price_vs_vwap=momentum_tf.get("price_vs_vwap", ""),
        poc=(vp.get("poc") if vp else None),
        volatility_regime=vr,
        market_structure=ms,
        divergence=entry_tf.get("divergence", ""),
        time_levels=tl,
    )

    return {
        "symbol": symbol,
        "last_price": last_price,
        "trend_text": trend_text,
        "htf_trend": htf_trend,
        "stf_trend": stf_trend,
        "impuls_text": impuls_text,
        "long": {"entry": long_entry, "sl": long_sl, "tp1": long_tp1, "tp2": long_tp2, "tp3": long_tp3},
        "short": {"entry": short_entry, "sl": short_sl, "tp1": short_tp1, "tp2": short_tp2, "tp3": short_tp3},
        "invalidation": invalidation,
        "btc_influence": btc_influence,
        "correlation_btc": correlation_btc,
        "funding_rate": funding_rate,
        "funding_note": funding_note,
        "open_interest": open_interest,
        "atr": atr_val,
        "long_pct": long_pct,
        "short_pct": short_pct,
        "wait_pct": wait_pct,
        "conclusion": conclusion,
        "trader_score": trader_score,
        "momentum_pct": momentum_pct,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "vp": vp,
        "rsi": rsi_val,
        "divergence": entry_tf["divergence"],
        "price_vs_vwap": momentum_tf.get("price_vs_vwap", "N/A"),
        "macd": macd_data,
        "bb": bb_data,
        "ls_long": ls_long,
        "ls_short": ls_short,
        "oi_history": oi_history,
        "fear_greed": fear_greed,
        "basis": basis,
        "cvd": cvd,
        "options_data": options_data,
        "market_structure": ms,
        "volatility_regime": vr,
        "tf_ms_vr": tf_ms_vr,
        "e2_signal":    e2_signal,
        "e2_checklist": e2_checklist,
        "time_levels":  tl,
    }


def render_symbol_section(a: dict) -> str:
    if "error" in a:
        return f"## {a['symbol']}\n⚠️ {a['error']}\n"

    s = a["symbol"].replace("/USDT", "")
    L, S = a["long"], a["short"]
    return f"""## {s} — {_fmt(a['last_price'])} USDT

**Trend:** {a['trend_text']}
**Impuls:** {a['impuls_text']}

**🟢 Long scénář**
- Vstup: {_fmt(L['entry'])}
- SL: {_fmt(L['sl'])}
- TP1: {_fmt(L['tp1'])}
- TP2: {_fmt(L['tp2'])}
- TP3: {_fmt(L['tp3'])}

**🔴 Short scénář**
- Vstup: {_fmt(S['entry'])}
- SL: {_fmt(S['sl'])}
- TP1: {_fmt(S['tp1'])}
- TP2: {_fmt(S['tp2'])}
- TP3: {_fmt(S['tp3'])}

**Invalidation:** {a['invalidation']}
**BTC vliv:** {a['btc_influence']}
**Funding:** {a.get('funding_note') or 'N/A (perpetual data nedostupná)'}

**Pravděpodobnost:** Long {a['long_pct']}% | Short {a['short_pct']}% | Wait {a['wait_pct']}%

**Závěr:** {a['conclusion']}
"""


def render_full_report(analyses: list) -> str:
    valid = [a for a in analyses if "error" not in a]
    now = datetime.now().strftime("%Y-%m-%d %H:%M") + " (tvůj lokální čas)"

    sections = [render_symbol_section(a) for a in analyses]

    # 🏆 Síla mincí - seřazeno podle trader_score
    ranked = sorted(valid, key=lambda a: a["trader_score"], reverse=True)
    strength_lines = [
        f"{i+1}. {a['symbol'].replace('/USDT','')} — score {a['trader_score']}/100"
        for i, a in enumerate(ranked)
    ]

    score_lines = [f"- {a['symbol'].replace('/USDT','')}: {a['trader_score']}/100" for a in valid]
    momentum_lines = [
        f"- {a['symbol'].replace('/USDT','')}: {a['momentum_pct']:+d}%" for a in valid
    ]
    entry_lines = [
        f"- {a['symbol'].replace('/USDT','')}: Long @ {_fmt(a['long']['entry'])} | Short @ {_fmt(a['short']['entry'])}"
        for a in valid
    ]

    best = max(valid, key=lambda a: abs(a["momentum_pct"]) + a["trader_score"]) if valid else None
    if best:
        direction = "LONG" if best["conclusion"] == "🟢 LONG" else ("SHORT" if best["conclusion"] == "🔴 SHORT" else "WAIT")
        side = best["long"] if direction == "LONG" else best["short"]
        prob = best["long_pct"] if direction == "LONG" else best["short_pct"]
        best_block = f"""- Coin: {best['symbol'].replace('/USDT','')}
- Směr: {direction}
- Vstup: {_fmt(side['entry'])}
- SL: {_fmt(side['sl'])}
- TP1: {_fmt(side['tp1'])}
- TP2: {_fmt(side['tp2'])}
- TP3: {_fmt(side['tp3'])}
- Pravděpodobnost: {prob}%"""
    else:
        best_block = "Nedostatek dat."

    watchlist_lines = []
    for a in valid:
        s = a["symbol"].replace("/USDT", "")
        watchlist_lines.append(
            f"**{s}**\n- Long trigger: uzavření nad {_fmt(a['swing_high'])}\n"
            f"- Short trigger: uzavření pod {_fmt(a['swing_low'])}\n"
        )

    report = f"""# 📊 BTC / ETH / SOL / HYPE — Analýza ({now})

> ⚠️ Toto je automaticky generovaná technická analýza (RSI, VWAP, Ichimoku, Volume Profile).
> Trader Score, Momentum a Pravděpodobnosti jsou heuristiky odvozené z indikátorů,
> NE statisticky podložená predikce ani finanční doporučení. Rozhoduj se na vlastní zodpovědnost.

---

{"".join(sections)}
---

## 🏆 Síla mincí
{chr(10).join(strength_lines)}

## 📊 Trader Score (0–100)
{chr(10).join(score_lines)}

## ⚡ Momentum (%)
{chr(10).join(momentum_lines)}

## 🎯 Ideální vstupy
{chr(10).join(entry_lines)}

## 🔥 Nejlepší setup dne
{best_block}

## 👀 Watchlist
{"".join(watchlist_lines)}
"""
    return report
