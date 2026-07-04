"""
Strategy engine — jediný zdroj pravdy pro E2 signál a obchodní úrovně.

Tenhle modul je ZÁMĚRNĚ čistý:
  - žádné I/O (fetch, síť, soubory)
  - žádné formátování textu (český report je jinde)
  - jen deterministická funkce: MarketState → Signal

Díky tomu ho volá dashboard, paper tracker i (časem) backtester a live bot
stejným způsobem — konec rozjezdů typu "dashboard počítá jinak než tracker".

Featury (indikátory) se počítají jinde (indicators.analyze_timeframe pro live,
backtest._precompute vektorizovaně) — sem vstupují už hotové jako MarketState.
Rozhodovací logika je tady na jednom místě.
"""

from dataclasses import dataclass, field


# ── Datové typy ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Levels:
    """Vstup + stop-loss + tři take-profity pro jednu stranu (long nebo short)."""
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float

    def as_dict(self) -> dict:
        return {"entry": self.entry, "sl": self.sl,
                "tp1": self.tp1, "tp2": self.tp2, "tp3": self.tp3}


@dataclass(frozen=True)
class MarketState:
    """
    Stav trhu k jednomu okamžiku — vstup do strategie.
    Hodnoty jsou už spočítané featury, ne raw svíčky.
    """
    last_price: float
    htf_trend: str                 # "BULLISH" | "BEARISH" | "NEUTRÁLNÍ" | "N/A"
    stf_trend: str
    rsi: float
    price_vs_vwap: str             # obsahuje "nad" / "pod"
    divergence: str                # text; hledá se "BULLISH" / "BEARISH"
    volatility_regime: dict | None # {"regime": "TRENDING"|...}
    market_structure: dict | None  # {"structure": "BULLISH"|"BEARISH"|...}
    volume_profile: dict           # {"poc", "val", "vah"}
    swing_high: float
    swing_low: float
    atr: float
    time_levels: dict = field(default_factory=dict)  # weekly_open, monday_low, ...


@dataclass(frozen=True)
class Signal:
    """
    Výsledek strategie. `side` je rozhodnutí; `long`/`short` úrovně se počítají
    vždy obě (dashboard zobrazuje oba scénáře, bot použije jen ten svůj).
    """
    side: str                      # "LONG" | "SHORT" | "WAIT"
    checklist: dict                # podmínka -> bool (pro vedoucí stranu)
    confidence: float              # podíl splněných podmínek 0..1
    reasons: list                  # názvy splněných podmínek
    long: Levels
    short: Levels


# ── E2 rozhodnutí (9 podmínek) ──────────────────────────────────────────────────

def evaluate_e2_conditions(
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

    Pozn.: Weekly Open podmínka je "wo is None or ..." — když time_levels chybí,
    podmínka NESELHÁVÁ (auto-pass). Proto je důležité dodat reálné time_levels,
    jinak je signál permisivnější, než dashboard ukazuje.
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


# ── Výpočet obchodních úrovní (Volume Profile + ATR + Time Levels) ──────────────

def compute_levels(
    last_price: float,
    volume_profile: dict,
    swing_high: float,
    swing_low: float,
    atr: float,
    time_levels: dict | None = None,
) -> tuple[Levels, Levels]:
    """
    Spočítá long i short úrovně (entry / SL / TP1-3) z Volume Profile, ATR
    a časových hladin. Vrací (long_levels, short_levels).

    Logika převzata 1:1 z původního report_generator.build_symbol_analysis,
    aby zůstalo zachováno chování dashboardu i trackeru.
    """
    vp  = volume_profile
    tl  = time_levels or {}
    atr_val = atr or last_price * 0.005

    risk = max(atr_val * 1.5, abs(last_price - swing_low) * 0.5)

    # Long vstupní zóna: nejbližší podpora pod cenou (Monday Low, Weekly Open,
    # Monthly Open) nebo VAL — whichever je nejblíž ceně (nejvyšší)
    long_supports = [vp["val"]]
    for name in ("monday_low", "weekly_open", "monthly_open"):
        lvl = tl.get(name)
        if lvl is not None and lvl < last_price:
            long_supports.append(lvl)
    long_entry = max(long_supports)
    long_entry = max(long_entry, last_price * 0.990)   # max 1 % pod cenou

    long_sl  = min(swing_low, long_entry - risk)
    long_tp1 = vp["poc"] if vp["poc"] > long_entry else long_entry + risk
    long_tp2 = max(vp["vah"], swing_high)
    long_tp3 = long_entry + (long_tp2 - long_entry) * 1.6

    # Short vstupní zóna: nejbližší odpor nad cenou (Monday High, Weekly High,
    # Prev Week High, Prev Month High) nebo VAH
    short_resistances = [vp["vah"]]
    for name in ("monday_high", "weekly_high", "prev_week_high", "prev_month_high"):
        lvl = tl.get(name)
        if lvl is not None and lvl > last_price:
            short_resistances.append(lvl)
    short_entry = min(short_resistances)
    short_entry = min(short_entry, last_price * 1.010)  # max 1 % nad cenou

    short_sl  = max(swing_high, short_entry + risk)
    short_tp1 = vp["poc"] if vp["poc"] < short_entry else short_entry - risk
    short_tp2 = min(vp["val"], swing_low)
    short_tp3 = short_entry - (short_entry - short_tp2) * 1.6

    long_levels  = Levels(long_entry,  long_sl,  long_tp1,  long_tp2,  long_tp3)
    short_levels = Levels(short_entry, short_sl, short_tp1, short_tp2, short_tp3)
    return long_levels, short_levels


# ── Hlavní vstupní bod ──────────────────────────────────────────────────────────

def evaluate(state: MarketState) -> Signal:
    """
    Vezme stav trhu a vrátí kompletní signál: směr, checklist, confidence
    a obchodní úrovně pro obě strany. Čistá deterministická funkce.
    """
    poc = (state.volume_profile or {}).get("poc")

    side, checklist = evaluate_e2_conditions(
        htf_trend=state.htf_trend,
        stf_trend=state.stf_trend,
        rsi_val=state.rsi,
        last_price=state.last_price,
        price_vs_vwap=state.price_vs_vwap,
        poc=poc,
        volatility_regime=state.volatility_regime,
        market_structure=state.market_structure,
        divergence=state.divergence,
        time_levels=state.time_levels,
    )

    long_levels, short_levels = compute_levels(
        last_price=state.last_price,
        volume_profile=state.volume_profile,
        swing_high=state.swing_high,
        swing_low=state.swing_low,
        atr=state.atr,
        time_levels=state.time_levels,
    )

    reasons    = [k for k, v in checklist.items() if v]
    confidence = round(len(reasons) / len(checklist), 3) if checklist else 0.0

    return Signal(
        side=side,
        checklist=checklist,
        confidence=confidence,
        reasons=reasons,
        long=long_levels,
        short=short_levels,
    )
