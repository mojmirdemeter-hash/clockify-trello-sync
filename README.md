# Clockify → Trello: synchronizácia natrackovaného času

Táto automatizácia raz za 30 minút (dá sa zmeniť) prejde vybrané Trello listy,
pre každú kartu nájde zhodný Clockify task podľa **rovnakého názvu**, sčíta
všetok natrackovaný čas a zapíše ho do Trello Custom Field.

## Čo potrebuješ pripraviť (5 vecí, žiadny kód)

### 1. V Trello vytvor DVA Custom Fieldy
Menu boardu → Custom Fields → Nový:
- typ **Number**, názov napr. `Natrackovaný čas (h)` — sem sa zapisuje výsledný súčet.
- typ **Text**, názov napr. `Clockify Task ID` — toto je "pamäťové" pole,
  do ktorého si skript sám uloží ID Clockify tasku pri prvom spárovaní.
  Vďaka nemu môžeš kartu kľudne premenovať — párovanie sa už nerobí podľa
  mena, ale podľa tohto ID. Toto pole môžeš na karte pokojne skryť
  (Trello to umožňuje), nie je určené na ručné úpravy.

### 2. Zisti si tieto hodnoty

| Čo | Kde to nájdeš |
|---|---|
| Trello API key + token | https://trello.com/power-ups/admin → vytvor si Power-Up alebo použi https://trello.com/app-key, tam je aj tlačidlo na vygenerovanie tokenu |
| Trello Board ID | Otvor board, za URL pridaj `.json` (napr. `trello.com/b/XXXX/nazov.json`) a nájdi pole `"id"` úplne na začiatku |
| Clockify API key | Clockify → Profil (vpravo hore) → Profile settings → API → Generate |
| Clockify Workspace ID | Clockify → nastavenia workspace, alebo v URL adrese po prihlásení |
| Clockify Project ID | Otvor projekt v Clockify, ID je v URL adrese |

### 3. Nahraj tento kód do GitHub repozitára
Vytvor nový (alebo použi existujúci) repozitár a nahraj doň presne túto
priečinkovú štruktúru (súbory `sync_time.py` a `.github/workflows/sync.yml`).

### 4. V GitHub repozitári nastav Secrets a Variables
`Settings → Secrets and variables → Actions`

**Secrets** (tajné, nikto ich neuvidí, ani ja):
- `CLOCKIFY_API_KEY`
- `TRELLO_KEY`
- `TRELLO_TOKEN`

**Variables** (verejné v rámci repa, netreba tajiť):
- `CLOCKIFY_WORKSPACE_ID`
- `CLOCKIFY_PROJECT_ID`
- `TRELLO_BOARD_ID`
- `TRELLO_CUSTOM_FIELD_NAME` — presný názov Number poľa z kroku 1
- `TRELLO_TASK_ID_FIELD_NAME` — presný názov Text poľa ("pamäť") z kroku 1
- `TRELLO_LIST_NAMES` — názvy listov oddelené čiarkou, napr.:
  `Priradiť Claudovi,Claude pracuje,Ensa review`
  (necháš prázdne = synchronizujú sa všetky listy na boarde)

### 5. Otestuj
V GitHub repozitári → záložka **Actions** → vyber workflow "Sync Clockify
time to Trello" → tlačidlo **Run workflow** → spusti ručne. V logu uvidíš
presne, ktoré karty sa spárovali a s akým výsledkom. Ak niečo zlyhá, chybová
hláška v logu ti povie presne prečo (napr. zlý názov custom fieldu, alebo
karta bez zodpovedajúceho Clockify tasku).

## Ako funguje párovanie a premenovanie

- **Prvýkrát** sa karta s Clockify taskom spáruje podľa **rovnakého názvu**
  (musia sa zhodovať presne). Ak sa nenájde zhoda, karta sa preskočí a
  napíše sa to do logu — nič sa nepokazí.
- Hneď po prvom spárovaní sa Clockify Task ID uloží do poľa
  `Clockify Task ID` na karte. **Od tej chvíle už na názve karty nezáleží**
  — môžeš ju premenovať, ako chceš, párovanie zostáva podľa uloženého ID.
- Ak sa názov karty zmení, skript pri najbližšom behu automaticky
  premenuje aj zodpovedajúci Clockify task, aby si oba názvy zostali
  zrozumiteľne zosynchronizované (toto je len kozmetické, na párovanie to
  už nemá vplyv).
- Ak niekto Clockify task medzičasom zmaže, skript si to všimne a skúsi
  nájsť nový task podľa aktuálneho názvu karty (rovnako ako pri prvom
  spárovaní).

## Ak narazíš na chybu

Skopíruj mi text z logu (Actions → tá zlyhaná beh → rozbaľ krok "Run sync")
a pošli mi ho — z chybovej hlášky viem presne povedať, čo treba opraviť.
