# Handoff: BTC/ETH/SOL/HYPE technický analyzátor → pokračování v Claude Code

Vlož tenhle soubor (nebo jeho obsah) jako první zprávu v Claude Code ve složce
projektu. Dává Claude Code kompletní kontext, ať nemusíš nic opakovat.

---

## Kontext projektu

Stavíme nástroj pro technickou analýzu kryptoměn (BTC, ETH, SOL, HYPE), který
nahrazuje ruční práci s TradingView grafy + ChatGPT analýzami. Uživatel je
zkušený trader (obchoduje X let), **nechce finanční poradenství ani
"magické" predikce** – chce přesný, transparentní, mechanický nástroj
postavený na skutečných indikátorech, který si bude dál sám ladit a případně
rozšiřovat směrem k vlastnímu tradingbotu.

## Co už existuje (aktuální stav kódu)

```
crypto_analyzer/
├── config.py              # symboly, timeframy, parametry indikátorů
├── data_fetcher.py        # ccxt wrapper: OHLCV (Binance→Bybit→OKX fallback),
│                           # + funding rate / open interest (best-effort)
├── indicators.py          # RSI, VWAP, ATR, Ichimoku, Volume Profile (POC/VAH/VAL),
│                           # RSI divergence (s filtrem šumu), BTC korelace
├── report_generator.py    # skládá indikátory do Trend/Long/Short/Invalidation/
│                           # BTC vliv/Pravděpodobnost/Závěr + souhrnné sekce
│                           # (Síla mincí, Trader Score, Momentum, Watchlist...)
├── main.py                # CLI: `python main.py --once` nebo nepřetržitá smyčka
└── requirements.txt
```

**Funguje:** ověřeno na syntetických datech (sandbox, kde jsem to psal, nemá
přístup k burzovním API), i na reálných datech na uživatelově PC – `python
main.py --once` produkuje validní report.

**Důležité rozhodnutí, které padlo v konverzaci:** Trader Score, Momentum %
a Pravděpodobnosti (Long/Short/Wait %) jsou **ruční heuristika** (lineární
kombinace trend/RSI/divergence/funding), ne nic statisticky kalibrovaného.
Uživatel o tom ví a chce to časem nahradit něčím podloženějším (viz roadmapa).

## Co bylo právě přidáno (poslední iterace, ale NEOTESTOVÁNO na živých datech)

- ATR-based stop-loss (1.5×ATR místo čistě swing-based SL) – adaptivní na volatilitu
- Vylepšená RSI divergence detekce (min. vzdálenost + min. amplituda mezi pivoty, ať nechytá šum)
- BTC korelace (rolling Pearson na 1H returns, posledních 50 svíček)
- Funding rate jako kontextový signál (extrémní funding = kontrariánský tlak)
- **Spustit `python main.py --once` jako první krok a zkontrolovat, že to s reálnými daty dává smysl** (zejména SL/TP úrovně a funding rate hodnoty)

## Co NENÍ hotové

- **Live dashboard** (web UI, ne markdown soubor) – bylo rozjednáno (Flask
  + auto-refresh každých pár sekund, 4 karty s coiny), ale není dokončené.
  Uživatel chce: "pustím program a vidím tam 4 mince, každých 5 min se to
  přepočítá" – tedy okno/dashboard, ne otevírání .md souboru pokaždé znovu.
- Žádný backtesting
- Žádné notifikace (Telegram/Discord)
- Žádná perzistentní historie (jen poslední report se přepisuje)

## Roadmapa – jak využít výkonný počítač uživatele

Seřazeno podle doporučené priority:

### 1. Live dashboard (UX priorita #1)
Flask (nebo FastAPI) server + background thread, co loopuje `run_cycle()`
každých `LOOP_INTERVAL_SECONDS`, ukládá výsledek do paměti (thread-safe),
a servíruje HTML stránku s auto-refreshem (JS `fetch()` na `/api/data` každých
~15-30s). 4 karty (BTC/ETH/SOL/HYPE) + souhrnné panely (Síla mincí, Watchlist
atd.). Tohle je nejvyšší priorita, protože je to to, co si uživatel výslovně
přál ("program, ne poznámkový blok").

### 2. Lokální cache historických dat
SQLite nebo Parquet soubory pro uložení stažených svíček. Důvod: každý
backtest jinak začíná zbytečným re-fetchem stejných dat z burzy a naráží na
rate limity. Výkonný disk/SSD uživatele se tu reálně využije.

### 3. Backtesting engine (klíčové pro důvěryhodnost skóre)
Tohle je místo, kde se "silný počítač" reálně využije:
- Stáhnout roky historie (2020+) pro všechny timeframy a symboly
- Vektorizovaný backtest pravidel z `report_generator.py` (pandas/numpy,
  případně `vectorbt` pro rychlost)
- Walk-forward validace (ladit parametry na jednom období, ověřit na jiném –
  jinak hrozí overfitting na historická data)
- Výstup: skutečná win-rate, R:R, max drawdown pro různé kombinace pravidel
  → tím se Trader Score/Pravděpodobnosti změní z "vycucané z prstu" na
  "podložené reálným testem"

### 4. Rozšíření univerza / souběžnost
Až bude backtester fungovat, dává smysl škálovat z 4 mincí na desítky
(async ccxt, `asyncio` + `aiohttp`), aby šlo skenovat širší trh, ne jen
4 předem vybrané coiny. Výkonný CPU s více jádry se využije na paralelní
zpracování indikátorů přes symboly.

### 5. (Volitelné, až po backtesteru) ML skórování
Až bude existovat backtester a tedy způsob, jak objektivně poznat "lepší" od
"horší" model: natrénovat gradient boosting (XGBoost/LightGBM – běží skvěle
na CPU, GPU tu není potřeba) na engineered features (RSI, ATR, vzdálenost od
POC/VAH/VAL, tloušťka Ichimoku mraku, funding rate, změna OI, BTC korelace)
místo ručně vážené lineární kombinace. **Pozor:** GPU/deep learning tady
pravděpodobně nepřinese nic navíc – pro tabulková technická data je gradient
boosting standardně silnější a mnohem méně náchylný na overfit než neuronka.
Powerful PC se tu hodí spíš na rychlé cross-validation runy než na GPU compute.

### 6. (Volitelné) Notifikace
Telegram/Discord bot, který pingne, když se splní Watchlist trigger (cena
zavře nad/pod definovanou úrovní) – netřeba mít dashboard pořád otevřený.

## Co NEDĚLAT

- Neslibovat uživateli, že to "predikuje trh" – je to nástroj na zpracování
  indikátorů, ne věštírna. Uživatel tohle sám chápe a explicitně řekl, že
  nehledá finanční poradenství.
- Nepřeskakovat backtester rovnou k ML – bez objektivního měřítka ("je tenhle
  model lepší než ten předchozí?") je ladění jen hádání.
- Nezapomenout, že "Spaceman Key Levels" z původního TradingView promptu je
  proprietární indikátor, jehož přesnou logiku neznáme – aktuálně nahrazen
  Volume Profile (POC/VAH/VAL) + swing high/low jako koncepčně podobnou věcí.
