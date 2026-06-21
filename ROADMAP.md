# Roadmap – Trading velitelské centrum (BTC/ETH/SOL/HYPE)

Tohle je prioritizovaný seznam vylepšení směřující k jednomu místu, kde máš
všechno důležité pro trading 24/7, místo přepínání mezi TradingView,
CoinGlassem, Twitterem a zprávami. Claude Code mezitím kód posunul dál
(dashboard.py, pywebview, system tray) – tenhle dokument NEMUSÍ přesně
zrcadlit aktuální stav souborů, ber to jako mapu nápadů a pořadí priorit.

**Pravidlo:** dokonči vždy aktuální tier a ověř, že to reálně používáš a dává
ti to smysl na živých datech, než skočíš na další. Lákavé je rozjet deset
věcí najednou – nedělej to. Velitelské centrum se staví panel po panelu.

---

## Tier 0 – Hotovo (stav k 21.6.2026)

- OHLCV data (Binance → Bybit → OKX fallback) přes ccxt
- RSI, VWAP, ATR, Ichimoku, Volume Profile (POC/VAH/VAL)
- RSI divergence detekce (filtrovaná na šum)
- BTC korelace (rolling Pearson)
- Funding rate jako kontextový signál
- ATR-based stop-loss (adaptivní na volatilitu)
- Live dashboard (Flask), desktopové okno (pywebview), system tray ikonka

---

## Tier 1 – Rychlá vylepšení indikátorů (cenová data, co už stahuješ)

- [ ] **MACD** – potvrzení momentum, signální linie crossover
- [ ] **Bollinger Bands** – je trh "natažený", nebo se stahuje do konsolidace
- [ ] **Long/Short ratio top traderů** (Binance endpoint) – kontrariánský signál
- [ ] **Historie Open Interest** (trend, ne jen snímek) – roste OI s cenou (nový kapitál) vs. roste cena a OI klesá (krytí shortů)
- [ ] **Fear & Greed Index** (alternative.me, free API)
- [ ] **Order book imbalance** (`ccxt.fetchOrderBook`) – nákupní vs. prodejní tlak těsně u ceny

## Tier 2 – Derivátová data do hloubky

- [ ] **Liquidation heatmapa** – kde se kumulují pozice, co se vynulují při pohybu ceny (magnet pro cenu)
- [ ] **Futures basis** – prémie futures nad spotem = míra leverage v systému
- [ ] **Options data (Deribit, hlavně BTC/ETH)** – put/call ratio, implied volatility; velké expirace umí "přišpendlit" cenu k hladině
- [ ] **CVD (Cumulative Volume Delta)** – kdo reálně tlačí cenu (agresivní nákupy vs. prodeje)

## Tier 3 – Market structure & systémové nástroje

- [ ] **Market structure (BOS/CHoCH)** – automatická detekce break/change of structure, navazuje na swing detekci v `indicators.py`
- [ ] **Volatility regime filtr** – trendující vs. rangující trh (ATR + šířka Bollingerů) → filtr "tenhle setup teď nedává smysl"
- [ ] **Multi-coin korelační matice** – BTC/ETH/SOL/HYPE navzájem, ne jen vůči BTC
- [ ] **Portfolio & risk tracking** *(dříve chybělo, klíčová věc)*:
  - otevřené pozice, nerealizovaný P&L
  - celková expozice/leverage napříč coiny
  - korelace tvých vlastních otevřených pozic (nemít 4x to samé bez vědomí)
  - max drawdown sledování, denní/týdenní stop (circuit breaker, než si ublížíš)
- [ ] **Trading deník** – log každého vygenerovaného setupu + co se reálně stalo. Objektivní zpětná vazba na edge bez nutnosti plného backtestu.
- [ ] **Telegram notifikace** – watchlist triggery, silné setupy, risk limity
- [ ] **Position sizing kalkulačka** – účet + % riziko → velikost pozice podle SL
- [ ] **Backtesting engine** – walk-forward validace pravidel na historických datech

## Tier 4 – News & Sentiment

- [ ] **CryptoPanic API** – agregace novinek, bullish/bearish hlasování, free tier
- [ ] **Sentiment scoring přes Claude API** – headlines → skóre 1-10 + shrnutí
- [ ] **Whale Alert** – velké transfery na/z burz jako předzvěst tlaku

## Tier 5 – Makro kontext (hýbe hlavně BTC, přes BTC vším ostatním)

- [ ] **DXY** (dolarový index) – často inverzní korelace s cryptem
- [ ] **US 10Y výnos dluhopisů**
- [ ] **S&P 500 / Nasdaq** – crypto se dnes chová hodně jako "risk-on" tech proxy
- [ ] **VIX** – strach na tradičních trzích
- [ ] Zdroj dat: většinou potřeba placené/omezené API (Alpha Vantage, FRED pro yields je zdarma) – řešit až tahle vrstva přijde na řadu

## Tier 6 – On-chain tok peněz

- [ ] **Netflow na/z burz** – přítok = prodejní tlak, odtok = akumulace
- [ ] **Změny supply stablecoinů** (USDT/USDC) – nový mint = potenciální nákupní síla čekající na vstup
- [ ] **HYPE buyback tracking** – 90 % poplatků jde na zpětný odkup, fundamentální datový bod specifický pro HYPE (BTC/ETH ho nemají)
- Realisticky vyžaduje placené API (Glassnode/CryptoQuant) – nejdražší tier, řešit až na konci

## Tier 7 – Kalendář událostí (sjednocuje rozházené položky z ostatních tierů)

- [ ] **Token unlock kalendář** (token.unlocks.app) – hlavně HYPE
- [ ] **Options expirace** (Deribit – měsíční/kvartální, dělají volatilitu)
- [ ] **Makro kalendář** (FOMC, CPI, NFP)
- Jeden panel, který ukazuje "co se blíží v příštích 7 dnech" napříč všemi kategoriemi

---

## Architektura velitelského centra

Různé kategorie dat se musí obnovovat různě často – tohle by se nemělo cpát
do jednoho monolitického reportu, ale do samostatných panelů s vlastním
refresh intervalem:

| Kategorie | Refresh interval |
|---|---|
| Ceny, indikátory (Tier 0-2) | každých 5 min |
| Portfolio/risk (Tier 3) | real-time / při změně pozice |
| News/sentiment (Tier 4) | každé 2-3 hodiny |
| Makro (Tier 5) | jednou denně (mimo dny FOMC/CPI) |
| On-chain (Tier 6) | jednou denně |
| Kalendář (Tier 7) | jednou denně |

## Infrastruktura, na kterou nezapomenout (napříč všemi tiery)

- [ ] **Secrets management** – API klíče (Telegram, CryptoPanic, případně Glassnode) do `.env`, `.env` do `.gitignore`. Nikdy necommitovat klíče do GitHubu.
- [ ] **Centralizovaná vrstva pro fetch/cache** – s každým tierem přibývá API volání. Jedno místo na rate limity a cache, než to začne bolet.
- [ ] **Panel-based UI** – dashboard rozdělený na nezávislé widgety/panely (viz tabulka výš), ne jeden velký report, co se musí celý přepočítat najednou.

---

## (Volné nápady mimo trading – pro pozdější chvíli, ne teď)

- Osobní dashboard / tracker na cokoliv jiného, co tě napadne
- Automatizace opakující se práce mimo trading
