"""
Konfigurace BTC/ETH/SOL/HYPE analyzátoru.
Uprav si tu cokoliv potřebuješ - symboly, timeframy, interval běhu atd.
"""

# Mince, které se sledují
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "HYPE/USDT"]

# Timeframy, které se stahují pro každou minci
TIMEFRAMES = ["15m", "1h", "4h", "1d"]

# Pořadí burz, které se zkusí (ccxt id). Pokud pár na burze chybí, jde se na další.
EXCHANGES_PRIORITY = ["binance", "bybit", "okx"]

# Kolik svíček se stahuje na jeden timeframe (musí stačit na Ichimoku - potřebuje 52+26)
CANDLE_LIMIT = 300

# Jak často (v sekundách) se má celý cyklus opakovat, když skript běží "furt"
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
