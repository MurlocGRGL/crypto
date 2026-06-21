# BTC / ETH / SOL / HYPE — Technický analyzátor

Python program, který nepřetržitě stahuje cenová data (BTC, ETH, SOL, HYPE)
z burzovních API (Binance → fallback Bybit → fallback OKX), počítá RSI, VWAP,
Ichimoku, Volume Profile a RSI divergence, a generuje strukturovaný report
ve formátu, který jsi definoval (Trend / Long / Short / Invalidation / BTC vliv /
Pravděpodobnost / Závěr + souhrnné sekce na konci).

## ⚠️ Důležité upozornění

- **Toto není finanční poradenství.** Trader Score, Momentum % a Pravděpodobnosti
  jsou heuristiky odvozené z pravidel v `report_generator.py` (kombinace trendu,
  RSI, divergence), **ne** statisticky validovaná predikce budoucího pohybu ceny.
- Vstupy/SL/TP jsou odvozené z volume profilu a swing high/low posledních
  ~300 svíček – je to mechanický výpočet, ne investiční doporučení. Vždy si to
  proveř proti grafu a rozhoduj se na vlastní zodpovědnost.
- "Spaceman Key Levels" je proprietární indikátor z konkrétního TradingView
  scriptu, jehož přesnou logiku neznám – nahradil jsem ho kombinací
  Volume Profile (POC/VAH/VAL) a swing high/low, což dělá koncepčně podobnou
  věc (klíčové cenové úrovně), ale nebude 1:1 stejné jako na tvém TV grafu.

## Instalace

```bash
pip install -r requirements.txt
```

(Potřebuješ Python 3.9+)

## Spuštění

**Jednorázově** (stáhne data, vygeneruje report, skončí – dobré pro test):
```bash
python main.py --once
```

**Nepřetržitě** (běží furt, defaultně obnovuje data každých 5 minut):
```bash
python main.py
```
Necháš ho běžet v terminálu / přes `screen`, `tmux`, nebo jako službu.
Report se po každém cyklu přepíše do `reports/latest_report.md` a navíc se
uloží kopie s časovou značkou do `reports/report_<timestamp>.md` (historie).

Zastavení: `Ctrl+C`.

## Konfigurace

Vše se nastavuje v `config.py`:
- `SYMBOLS` – které mince se sledují
- `TIMEFRAMES` – 15m / 1h / 4h / 1d
- `LOOP_INTERVAL_SECONDS` – jak často se obnovují data (default 300 s = 5 min)
- `RSI_PERIOD`, `ICHIMOKU_*`, `VOLUME_PROFILE_BINS` – parametry indikátorů

## Jak je program postavený

| Soubor | Co dělá |
|---|---|
| `data_fetcher.py` | Stahuje OHLCV svíčky přes `ccxt` (Binance → Bybit → OKX fallback) |
| `indicators.py` | RSI, VWAP, Ichimoku, Volume Profile (POC/VAH/VAL), RSI divergence |
| `report_generator.py` | Skládá výsledky indikátorů do tvého formátu, počítá Trader Score / Momentum / Pravděpodobnosti |
| `main.py` | Smyčka, která to celé spouští furt dokola a ukládá report |

## Co dál (nápady na rozšíření)

- Notifikace (Telegram/Discord bot), když se splní Watchlist trigger
- Napojení na TradingView alerty přes webhook (využiješ svůj Premium účet
  k přesným Pine Script signálům, tenhle program by je jen sbíral a skládal
  do reportu)
- Backtester, který by ověřil, jak by tahle pravidla fungovala historicky –
  doporučuju **udělat dřív, než cokoliv reálně obchoduješ** podle skóre.

## Síťové omezení (pro info)

Program byl vyvinutý a logicky otestovaný na syntetických datech v sandboxu,
který nemá přístup k burzovním API (whitelist domén). Na tvém vlastním
počítači s běžným internetem poběží bez problémů – `ccxt` jen volá veřejná
REST API burz, žádný API klíč není potřeba pro čtení cen.
