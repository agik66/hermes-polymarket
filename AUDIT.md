# AUDIT — hermes-polymarket (2026-07-15)

Triezvá technická analýza. Nič v kóde nebolo zmenené.

## 1. Stav bota

**Čo to je:** copy-trading *research* bot, čisto paper trading (žiadne kľúče, žiadne ordery — vynútené testom v `test_bot.py`). Stdlib-only Python, sqlite (`hermes.db`), launchd cyklus každých 15 min (`run_cycle.sh`), dashboard na GitHub Pages (`docs/`). Live stránka sedí s kódom — `data.json` generovaný 20:04, rules v21, dáta aktuálne.

**Stratégia:** copy-trading z leaderboardu. `scan` stiahne 500 peňaženiek (30d okno), profiluje top 50 (pozície + REDEEM aktivita + trady), skóruje ROI/konzistenciu/kopírovateľnosť s one-hit-wonder penaltou, trackuje top ≤15. `monitor` polluje ich trady, každý BUY skóruje (drift, spread, likvidita, čas do rezolúcie, conviction) → `paper_copy`/`watchlist`/`skip`. SELL peňaženky zavrie našu pozíciu. `review` po ≥1h súdi rozhodnutia podľa driftu ceny a **automaticky si prepisuje pravidlá**.

**Reálne výsledky (z `hermes.db`, 9.–15.7.):**

| Metrika | Hodnota |
|---|---|
| Paper copies | 35 (12 313 skipov) |
| Realized PnL | **−112.65 $** (bankroll 100 $ + manuálny „vklad" 100 $) |
| Win rate na uzavretých | 11/30 (37 %) |
| Blind benchmark (10 $ na každý pozorovaný BUY) | +404 $ na 65 510 $ staked = **+0.62 %**, na midpointe, bez spreadu |
| Rule changes | 20, z toho **19× posun `min_copy_score`** (vrátane 1 manuálnej korekcie) |

Kľúčový fakt: **filtrovaná stratégia prehrala výrazne viac ako neﬁltrované slepé kopírovanie.** Filter je momentálne anti-prediktívny alebo (pravdepodobnejšie pri n=35) je to šum — ale v žiadnom prípade nie je preukázaný edge. Blind +0.62 % na midpointe by po prekročení spreadu bol ~0.

**Čo bot NEMÁ:** stop-loss, max drawdown limit, modelovanie spreadu/slippage pri fill-e (fill na midpointe = optimistický), backtest na historických dátach, štatistickú validáciu (n=35 je nič), arbitrážny modul.

## 2. Top zlepšenia podľa dopadu

### P0 — Vypnúť/opraviť samo-učiacu slučku (aktívne škodí)

`rule_changes` ukazuje ratchet: pravidlo *„missing winners just under the gate"* znížilo `min_copy_score` 60→44 (9.7.), po manuálnej oprave 12.7. znova 60→45 do 14.7. Príčina v `step_review` (bot.py:467): „missed winner" = cena stúpla o >0.03 **za 1 hodinu** — pri 12k skipoch sa 3 také nájdu takmer každý cyklus, takže brána klesá o 2 body/15 min až na podlahu 45. Protiváha („copies losing, tighten") vyžaduje 5 kópií, ktoré prichádzajú oveľa pomalšie. Výsledok: filter je trvalo pripnutý na najvoľnejšom nastavení.

Hlbší problém: **hodnotiť rozhodnutia podľa 1h driftu je šum, nie signál** (1845 missed_winner vs 1855 avoided_loser — dokonale symetrické, presne ako pri náhode). Náprava:
- súdiť podľa **finálnej rezolúcie**, nie 1h driftu;
- auto-tuning buď úplne vypnúť do n≥300 rozhodnutí, alebo: max 1 zmena/kľúč/deň, hysteréza, a zmena len ak rozdiel prejde aspoň hrubým binomickým testom.

### P1 — Modelovať skutočné náklady fill-u

Paper fill na midpointe systematicky nadhodnocuje výsledok. Minimálne: fill na `mid + spread/2` (kúpa na asku), plus 1–2 centy slippage penaltu pri likvidite < ~20k. Polymarket historicky neúčtuje trading fees a gas je abstrahovaný cez proxy wallet, takže dominantný náklad je **spread + drift z latencie** — a práve ten teraz v PnL chýba. Bez tohto je každé „paper profitable" ilúzia.

### P2 — Opraviť win_rate profiling (garbage in)

Trackované peňaženky majú win_rate 0.97–1.00 — to je zjavne artefakt: výhry sa počítajú z REDEEM feedu (cap 500 riadkov), prehry len z aktuálnych pozícií s `curPrice ≤ 0.02`. Staré prehraté pozície zmiznú, výhry sa kumulujú → whale s tisíckami tradov vyzerá ako 99 % winner. `score_wallet` potom skóruje šum. Náprava: počítať win_rate len z tradov v spoločnom časovom okne (párovať BUY→rezolúcia cez trade history), alebo priznať, že win_rate nemáme, a vážiť len ROI + penaltu + aktivitu.

### P3 — Latencia a adverse selection (jadro toho, prečo copy-trading zlyháva)

Cyklus 15 min znamená detekciu trade-u až o 0–15 min neskôr; `max_drift` 0.05 síce odfiltruje najhoršie, ale zvyšok kupuje po pohybe. Navyše `entry_timing` skóruje **záporný drift ako dobrý vstup** (bot.py:154) — cena, ktorá klesla po nákupe whale-a, je často informácia, že whale sa mýlil, nie zľava. Straty sú koncentrované v športových moneyline marketoch (~50/50 coin flipy: MLB, tenis, esporty) — tam žiadny kopírovateľný edge nie je, whale tam berie varianciu, ktorú si môže dovoliť. Náprava:
- **kategóriový filter**: kopírovať peňaženku len v kategórii, kde má preukázané výsledky (stĺpec `best_category` už existuje, nepoužíva sa v rozhodovaní);
- skrátiť polling na 1–2 min pre trackované peňaženky (25 API callov, lacné);
- záporný drift za hranicou ~1–2 centov penalizovať, nie odmeňovať.

### P4 — Risk management

- 5–15 % equity na trade je pri korelovaných coin-flipoch (viac MLB zápasov v ten istý deň) recept na ruin — presne to sa stalo (bankroll −112 % ). Kelly pri neistom edgi ⇒ **1–2 % equity max**, plus limit celkovej expozície na kategóriu/deň.
- Chýba denný stop (napr. −5 % equity/deň = žiadne nové kópie) a max drawdown circuit breaker.
- Jediný exit je „whale predal" alebo rezolúcia. Pridať time-stop a mark-to-market stop-loss (napr. −50 % pozície).

### P5 — Validácia edge-u predtým, než sa čokoľvek ladí

- **Backtest**: data-api poskytuje históriu tradov peňaženiek — dá sa simulovať „čo by kopírovanie tejto peňaženky urobilo za posledné 3 mesiace" offline, s penalizáciou driftu. To je najlacnejší spôsob, ako zistiť, či existuje kopírovateľná peňaženka, bez čakania týždňov na live paper dáta.
- Štatistika: pri 35 tradoch so smerodajnou odchýlkou ~15 $ nie je odlíšiteľný edge ±10 % od nuly. Cieľ: stovky paper tradov + confidence interval na dashboarde namiesto bodového PnL.
- Overfitting: 19 z 20 rule changes bol jeden parameter tam a späť — to je učebnicový overfitting na šum. Každé auto-ladenie prahu na dátach, ktoré samo generuje, je in-sample optimalizácia.

### P6 — Drobnosti / hygiena

- `beat_blind_copy` v `step_report` (bot.py:514): výraz `min(1, len(pts)*10/max(1,len(pts)*10))` je vždy 1, porovnáva sa nenormalizovaný PnL (200 $ bankroll vs 65k staked benchmarku) — pole je bezvýznamné. Dashboard graf to normalizuje správne (per 100 $), report nie.
- `hermes.db` (11 MB) a `bot.log` v gite — rastú donekonečna, patria do `.gitignore`.
- Commit histórie „data update" každých 15 min zahlcuje repo; stačí commitovať len `docs/data.json`.

## 3. Arbitráž — verdikt

**Hľadá ju bot?** Nie. Nikde v kóde nie je porovnávanie komplementárnych cien ani cross-market logika. Je to čistý copy-trading.

**Dá sa na Polymarkete arbitrážiť?** Teoreticky áno, tri formy:

1. **Binárny komplement (YES + NO < 1):** prakticky mŕtve. YES a NO sú jeden zrkadlový orderbook v CLOB-e — kúpa NO za 0.4 je to isté ako predaj YES za 0.6. Diera vzniká len na milisekundy a berú ju market-making boty priamo na CLOB-e.
2. **Multi-outcome eventy (súčet YES cien ≠ 1, „negative risk"):** reálne existuje (napr. súčet kandidátov 1.02–1.05), ale Polymarket má natívny NegRisk konverzný mechanizmus a sedia na tom špecializované boty s WebSocket feedom a okamžitou exekúciou. Typická diera je 1–3 centy a zavrie sa v sekundách; po prejdení spreadu na každej nohe zostáva pri malej likvidite pár centov až dolárov na príležitosť.
3. **Cross-venue (Polymarket vs Kalshi):** rozdiely v cene reálne bývajú aj 2–5 ¢, ale: Kalshi má poplatky, KYC, USD rails (pomalé presuny kapitálu), a hlavne **iné znenie/rezolučné pravidlá** — „arbitráž" sa vie zmeniť na dve nezávislé stávky. Toto je skôr obchodovanie bázy než arbitráž.

**Prekážky pre tento bot konkrétne:** cyklus 15 min + HTTP polling public API = latencia 10⁴–10⁵× horšia než konkurencia; paper mód aj tak nevie fill garantovať; kapitál na oboch nohách; tenké knihy znamenajú, že zobrazená diera často nefillne ani 50 $.

**Odporúčanie:** exekučnú arbitráž nestavať — pri tejto latencii je očakávaný realizovateľný zisk ~0. Čo sa **oplatí** je lacný *detekčný/merací* modul (~50 riadkov): pri každom cykle stiahnuť multi-outcome eventy cez gamma API, spočítať `sum(YES)` a logovať odchýlky > spread do tabuľky `arb_observations` s hĺbkou kníh. Po 2–4 týždňoch dát budete *vedieť*, nie tušiť, či je tam niečo chytateľné pri vašej latencii. Očakávanie: nebude — ale meranie je skoro zadarmo a je to poctivá odpoveď.

## 4. Live stránka

Sedí s kódom: tabs (Wallets/Signals/Paper Trades/Journal/Rules/Reports), PnL graf bot vs blind benchmark (správne normalizovaný per 100 $ staked), „PAPER ONLY" badge, rule-change história. Dáta čerstvé (commit + data.json v ten istý 15-min cyklus). Jediná výhrada: dashboard ukazuje bodový PnL bez neistoty — pri n=35 by mal ukazovať aj interval/od kedy, inak zvádza k záverom zo šumu.

## 5. Čo by som spravil ako prvé (poradie)

1. **Zmraziť auto-tuning pravidiel** (jeden if v `step_review`) — momentálne aktívne kazí filter. Reset `min_copy_score` na 60.
2. **Fill na asku + spread penalta** v paper PnL — nech čísla znamenajú to, čo tvrdia.
3. **Review podľa rezolúcie namiesto 1h driftu** — bez správnej label-y je každé učenie aj vyhodnotenie bezcenné.
4. **Zníženie sizingu na 1–2 % equity + denný stop** — nech paper experiment prežije dosť dlho na štatistiku.
5. **Offline backtest kopírovania top peňaženiek** z ich trade history — najrýchlejšia cesta k odpovedi „existuje vôbec kopírovateľný edge?" bez týždňov čakania.
6. Kategóriový filter (nekopírovať šport-moneyline, resp. len best_category peňaženky).
7. Arb *detekčný* modul len na meranie (bod 3 vyššie).

## Triezvy záver

Bot je remeselne slušne napísaný (čistý stdlib, testy, versioning pravidiel, poctivé SAFETY.md — autor sám vymenoval hlavné riziká copy-tradingu a mal pravdu vo všetkých). Ale dáta hovoria jasne: **žiadny preukázaný edge**. Filter prehráva proti vlastnému blind benchmarku, benchmark sám je po nákladoch ~0, učiaca slučka sa učí zo šumu a rozbíja si vlastné brány. Copy-trading na verejnom API s minútovou+ latenciou je štrukturálne nevýhodný — ste vždy posledný v rade za rovnakou informáciou. Realistická hodnota projektu je *výskumný nástroj na vyvrátenie/potvrdenie hypotéz lacno na papieri* — a v tom je dobrý. Cesta k reálnemu zarábaniu vedie cez body 1–5 + backtest; ak ani potom filter nebije blind benchmark po nákladoch, edge tam nie je a nasadzovať peniaze by bola chyba.

---

## Addendum (2026-07-17) — opravy, učenie v2 a backtest

Opravy P0–P4 + P6 implementované (rules v22): review podľa finálnej rezolúcie,
fill na asku/bide (+1c tenké knihy), sizing 1–2 % equity, denný −5 % kill-switch,
cap 2 pozície/kategóriu, win_rate artefakt odstránený z profilovania aj
dashboardu, `market_info` opravený (gamma skrýva closed trhy bez `closed=true`).

**Učenie v2 (`learn_rules`):** samo-učiaca slučka NEbola zrušená, ale prerobená —
učí sa výhradne z **realizovaného PnL rozriešených kópií po nákladoch** (fill už
zaplatil spread + slippage), nikdy z cenového driftu. Ladí copy gate, sizing
(risk_max), spread a likviditné brány. Guardraily: ≥20 vzoriek na pravidlo,
per-key evidence recency (staré dáta nikdy nere-triggerujú tú istú zmenu),
split-half sign agreement (obe časové polovice dôkazov musia súhlasiť —
konzistenčný check, nie formálny test významnosti), ohraničený krok, tvrdé
hranice parametrov, 7-dňový cooldown na kľúč. Downgrade peňaženiek len podľa
realizovaných výsledkov (≥5 rozriešených, záporný priemer). Testy pokrývajú
smer zmien, cooldown, recency aj šumovú odolnosť; QA bez CRITICAL/HIGH nálezov.

**Backtest (`backtest.py`, out-of-sample):** 50 leaderboard peňaženiek, 90 d
história, selekcia len na in-sample, OOS okno [−28 d, −7 d], náklady +0/1/2 ¢,
dedup korelovaných stávok. Pri +2 ¢: vybraná top-5 skupina **−8 % na trade**
(n=97, t=−0.92), blind všetkých 50 −6 % (n=479), IS→OOS rank korelácia −0.21,
pričom in-sample referencia tých istých peňaženiek +14 % (t=2.3) — overfit gap.
Všetky známe biasy výsledok nadhodnocujú.

**Porovnanie učiacich signálov** (simulácia na OOS, wallet-selection páka):
žiadne učenie −7.9 $ celkovo (n=97) · učenie z rezolúcií −13.0 $ (n=45) ·
emulácia starej šumovej slučky (coin-flip labely, 20 seedov) −11.2 $ priemer.
Poučenie: na dátach **bez edge** žiadne učenie nepomôže — adaptívne pravidlá na
šume len pridávajú churn. Opravené učenie je bezpečnejšie (nemôže sa rozbehnúť
na šume), ale edge nevyrobí.

**Verdikt: žiadny preukázateľný edge po nákladoch; reálne peniaze nenasadzovať.**
