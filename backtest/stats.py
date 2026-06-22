"""
Statistiky backtesting výsledků.

Random baseline:
  Protože SL a TP1 jsou symetrické (obě = ATR_SL_MULT × ATR), platí:
  flip směru LONG↔SHORT → pnl_r = -pnl_r (přesně, bez aproximace).
  Monte Carlo (1 000 sim.): každý obchod nezávisle flipujeme s p=0.5.
"""

import numpy as np

RISK_PER_TRADE = 0.01   # 1 % kapitálu na obchod (fixní)


def compute_stats(trades: list, equity: list, fees_pct: float) -> dict:
    empty = {
        "fees_pct": fees_pct, "n_trades": 0, "n_long": 0, "n_short": 0,
        "win_rate": None, "avg_win_r": None, "avg_loss_r": None,
        "avg_rr": None, "expectancy_r": None,
        "total_return_pct": 0.0, "max_dd_pct": 0.0,
        "avg_bars_held": None, "exit_counts": {},
    }
    if not trades:
        return empty

    n      = len(trades)
    wins   = [t for t in trades if t.get("win")]
    losses = [t for t in trades if not t.get("win")]

    avg_win_r  = float(np.mean([t["pnl_r"] for t in wins]))   if wins   else 0.0
    avg_loss_r = float(np.mean([t["pnl_r"] for t in losses])) if losses else 0.0
    avg_rr     = (abs(avg_win_r / avg_loss_r)
                  if losses and avg_loss_r != 0 else None)

    eq   = np.array(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    max_dd_pct = round(float(((eq - peak) / peak).min() * 100), 2)

    pnl_rs     = [t["pnl_r"] for t in trades]
    expectancy = float(np.mean(pnl_rs))

    exit_counts: dict = {}
    for t in trades:
        k = t.get("exit_reason", "?")
        exit_counts[k] = exit_counts.get(k, 0) + 1

    return {
        "fees_pct":         fees_pct,
        "n_trades":         n,
        "n_long":           sum(1 for t in trades if t.get("side") == "LONG"),
        "n_short":          sum(1 for t in trades if t.get("side") == "SHORT"),
        "win_rate":         round(len(wins) / n * 100, 1),
        "avg_win_r":        round(avg_win_r,  3),
        "avg_loss_r":       round(avg_loss_r, 3),
        "avg_rr":           round(float(avg_rr), 2) if avg_rr is not None else None,
        "expectancy_r":     round(expectancy, 4),
        "total_return_pct": round(float((eq[-1] - 1.0) * 100), 2),
        "max_dd_pct":       max_dd_pct,
        "avg_bars_held":    round(float(np.mean([t.get("bars_held", 0) for t in trades])), 1),
        "exit_counts":      exit_counts,
    }


def compute_random_baseline(trades: list, n_sim: int = 1000, seed: int = 42) -> dict:
    """
    Monte Carlo random baseline: flip každého obchodu s pravděpodobností 0.5.

    Používá pnl_pct (% účtu, leverage-aware) pokud je k dispozici;
    jinak fallback na pnl_r × RISK_PER_TRADE pro zpětnou kompatibilitu.
    SL/TP jsou symetrické (ATR-based) → flip pnl je přesný, ne aproximace.
    Vrátí medián výnosu a 5.–95. percentil distribuce.
    """
    if not trades:
        return {"n_trades": 0}

    # pnl_vals v zlomku účtu (0.01 = 1 %)
    if "pnl_pct" in trades[0]:
        pnl_vals = np.array([t["pnl_pct"] / 100.0 for t in trades], dtype=float)
    else:
        pnl_vals = np.array([t["pnl_r"] for t in trades], dtype=float) * RISK_PER_TRADE

    rng = np.random.default_rng(seed)

    sim_returns   = np.empty(n_sim)
    sim_win_rates = np.empty(n_sim)

    for k in range(n_sim):
        signs     = rng.choice(np.array([-1.0, 1.0]), size=len(pnl_vals))
        flipped   = pnl_vals * signs
        eq        = float(np.prod(1.0 + flipped))
        sim_returns[k]   = (eq - 1.0) * 100.0
        sim_win_rates[k] = float(np.mean(flipped > 0) * 100.0)

    return {
        "n_trades":        len(trades),
        "n_sim":           n_sim,
        "return_median":   round(float(np.median(sim_returns)), 2),
        "return_p5":       round(float(np.percentile(sim_returns, 5)), 2),
        "return_p95":      round(float(np.percentile(sim_returns, 95)), 2),
        "win_rate_median": round(float(np.median(sim_win_rates)), 1),
    }


def compute_buy_hold(df_1h, start_bar: int) -> dict:
    """Buy-and-hold výnos za stejné testované období jako backtest."""
    if df_1h is None or start_bar >= len(df_1h) - 1:
        return {}
    entry  = float(df_1h.iloc[start_bar]["open"])
    exit_p = float(df_1h.iloc[-1]["close"])
    return {
        "entry_price": round(entry,  4),
        "exit_price":  round(exit_p, 4),
        "return_pct":  round((exit_p - entry) / entry * 100, 2),
    }
