"""
Konfigurace BTC/ETH/SOL/HYPE analyzátoru.
Citlivé hodnoty (API klíče, tokeny) se čtou z .env – nikdy je nepište přímo sem.
Uprav si tu cokoliv potřebuješ - symboly, timeframy, interval běhu atd.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Mince, které se sledují (stahují se a zobrazují v dashboardu všechny)
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"]

# Mince, pro které se LOGUJÍ paper-trade signály do e2_trackeru.
# Podmnožina SYMBOLS — jen ty s prokázaným edge z walk-forward analýzy
# (walk_forward.py, 3 roky / 6 půlroků). Ostatní se pořád stahují a zobrazují
# v dashboardu, ale netvoří paper-trade záznamy (nesbírá se šum bez edge).
#   BTC  — robustní směrový edge: kladná expectancy 5/6 půlroků
#   SOL  — robustní edge: kladná expectancy 5/6 půlroků, přežívá i fees
#   ETH  — smíšený (3/6), zatím neprokázaný → jen monitoring (snadno zapneš níž)
#   HYPE — nespolehlivý + malý vzorek (~1 rok), záporný celkově → jen monitoring
PAPER_TRADE_SYMBOLS = ["BTC/USDT", "SOL/USDT"]

# Timeframy, které se stahují pro každou minci
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

# Pořadí burz, které se zkusí (ccxt id). Pokud pár na burze chybí, jde se na další.
EXCHANGES_PRIORITY = ["binance", "bybit", "okx"]

# Kolik svíček se stahuje na jeden timeframe (musí stačit na Ichimoku - potřebuje 52+26)
CANDLE_LIMIT = 300

# Jak často se obnoví jen CENA (ticker) — rychlý fetch bez přepočtu indikátorů
PRICE_REFRESH_SECONDS = 90   # 1,5 minuty

# Kolik sekund po uzavření svíčky se počká, než se stáhnou data (burza potřebuje chvíli)
CANDLE_SETTLE_SECS = 10

# Jak často (v sekundách) se má celý cyklus opakovat, když skript běží "furt" (CLI main.py)
LOOP_INTERVAL_SECONDS = 300  # 5 minut

# Indikátory - nastavení period
RSI_PERIOD = 14
ICHIMOKU_TENKAN = 9
ICHIMOKU_KIJUN = 26
ICHIMOKU_SENKOU_B = 52

# Volume profile - počet cenových binů
VOLUME_PROFILE_BINS = 24
VALUE_AREA_PCT = 0.70  # 70% objemu = value area (VAH/VAL)

# Kam se ukládají reporty
OUTPUT_DIR = "reports"
LATEST_REPORT_FILENAME = "latest_report.md"
