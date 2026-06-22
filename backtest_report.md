# Backtest — Srovnání D vs E (confluence varianty)
Vygenerováno: 2026-06-22 15:16

## Testované varianty

| Varianta | Podmínky |
|---|---|
| D | HTF=trend, STF=trend, RSI v pásmu, close > VWAP, close > POC (5 podmínek) |
| E | vše z D + Volatility Regime=TRENDING + poslední BOS souhlasí + RSI divergence neblokuje (8 podmínek) |

Každá varianta testována s pákou **1×, 3×, 5×** (margin model: 1 % účtu per obchod).

**SL/TP:** 1.5× ATR (1:1 R:R)  |  **Entry:** open příšti svíčky (no look-ahead)  |  **Fees sloupec:** 0.04 % taker round-trip

---

## Souhrnná tabulka — výnos s fees=0.04 % (1 % risk/trade)

| Symbol | E2 3x | E2 5x | H  3x | H  5x | Buy&Hold |
|---|---|---|---|---|---|
| BTC | -0.25 % | -0.42 % | -0.07 % | -0.12 % | +114.09 % |
| SOL | +3.32 % | +5.57 % | +0.38 % | +0.63 % | +338.28 % |

---

## Detail po symbolech

### BTC — 2023-06-25 → 2026-06-22

| Metrika                |     E2 3x      |     E2 5x      |     H  3x      |     H  5x      |
|----------------|----------------|----------------|----------------|----------------|
| Počet obchodů          |      378       |      378       |      110       |      110       |
| Win rate               |     52.9 %     |     52.9 %     |     49.1 %     |     49.1 %     |
| Expectancy             |    +0.077 R    |    +0.077 R    |    -0.018 R    |    -0.018 R    |
| Výnos fees=0           |    +0.66 %     |    +1.10 %     |    +0.20 %     |    +0.32 %     |
| Výnos fees=0.04%       |    -0.25 %     |    -0.42 %     |    -0.07 %     |    -0.12 %     |
| Max drawdown           |    -0.73 %     |    -1.22 %     |    -0.30 %     |    -0.50 %     |
| Avg R:R                |      1.04      |      1.04      |      1.0       |      1.0       |
| Buy&Hold (+114.09 %)   |   +114.09 %    |    (stejné)    |    (stejné)    |    (stejné)    |


---

### SOL — 2023-06-25 → 2026-06-22

| Metrika                |     E2 3x      |     E2 5x      |     H  3x      |     H  5x      |
|----------------|----------------|----------------|----------------|----------------|
| Počet obchodů          |      315       |      315       |      142       |      142       |
| Win rate               |     57.8 %     |     57.8 %     |     53.5 %     |     53.5 %     |
| Expectancy             |    +0.159 R    |    +0.159 R    |    +0.070 R    |    +0.070 R    |
| Výnos fees=0           |    +4.10 %     |    +6.91 %     |    +0.73 %     |    +1.21 %     |
| Výnos fees=0.04%       |    +3.32 %     |    +5.57 %     |    +0.38 %     |    +0.63 %     |
| Max drawdown           |    -0.55 %     |    -0.91 %     |    -1.11 %     |    -1.84 %     |
| Avg R:R                |      1.01      |      1.01      |      1.0       |      1.0       |
| Buy&Hold (+338.28 %)   |   +338.28 %    |    (stejné)    |    (stejné)    |    (stejné)    |
