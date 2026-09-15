"""
Sync natrackovaneho casu z Clockify do Trello Custom Field.

Ako to funguje:
1. Zoberie karty z vybranych Trello listov (TRELLO_LIST_NAMES).
2. Pre kazdeho clena workspace stiahne VSETKY time entries v danom
   Clockify projekte (CLOCKIFY_PROJECT_ID) a sposcita ich podla POPISU
   (description) - presne do tohto pola Clockify Trello extension uklada
   nazov karty pri trackovani.
3. Rieseni premenovania karty: kazda karta si v custom field
   TRELLO_ALIAS_FIELD_NAME (typ Text) drzi ZOZNAM VSETKYCH nazvov, ake
   kedy mala. Ak sa aktualny nazov karty odlisuje od poslednej ulozenej
   verzie, prida sa do zoznamu ako dalsi alias - stare aj nove time
   entries (podla starych aj noveho nazvu) sa naďalej pripocitaju.
4. Sposcita cas zo VSETKYCH aliasov danej karty a zapise sucet (v hodinach)
   do Trello Custom Field TRELLO_CUSTOM_FIELD_NAME.

Beziaci na GitHub Actions podla harmonogramu (pozri .github/workflows/sync.yml).
Nepouziva Clockify Reports API (ta vyzaduje placeny plan) - iba zakladne
Time Entry API, ktore funguje aj na Free plane.
"""

import os
import re
import sys
import time
import requests
from collections import defaultdict

CLOCKIFY_API_KEY = os.environ["CLOCKIFY_API_KEY"]
CLOCKIFY_WORKSPACE_ID = os.environ["CLOCKIFY_WORKSPACE_ID"]
CLOCKIFY_PROJECT_ID = os.environ["CLOCKIFY_PROJECT_ID"]

TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]
TRELLO_CUSTOM_FIELD_NAME = os.environ.get("TRELLO_CUSTOM_FIELD_NAME", "Natrackovaný čas (h)")
# Znovu pouzivame pole, ktore uz mas vytvorene ako "Clockify Task ID" -
# teraz sluzi ako ulozisko historie nazvov karty (aliasov), nie ID tasku.
TRELLO_ALIAS_FIELD_NAME = os.environ.get("TRELLO_ALIAS_FIELD_NAME", "Clockify Task ID")
TRELLO_LIST_NAMES = [s.strip() for s in os.environ.get("TRELLO_LIST_NAMES", "").split(",") if s.strip()]

ALIAS_SEPARATOR = "\n---\n"

CLOCKIFY_BASE = "https://api.clockify.me/api/v1"
TRELLO_BASE = "https://api.trello.com/1"
CLOCKIFY_HEADERS = {"X-Api-Key": CLOCKIFY_API_KEY}


def clockify_get(path, params=None, retries=5):
    url = f"{CLOCKIFY_BASE}{path}"
    for attempt in range(retries):
        r = requests.get(url, headers=CLOCKIFY_HEADERS, params=params, timeout=30)
        if r.status_code == 429:
            wait = 2 ** attempt
            print(f"  Clockify vrátil 429 (limit), čakám {wait}s a skúšam znova...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def trello_request(method, path, params=None, json_body=None):
    params = dict(params or {})
    params["key"] = TRELLO_KEY
    params["token"] = TRELLO_TOKEN
    r = requests.request(method, f"{TRELLO_BASE}{path}", params=params, json=json_body, timeout=30)
    if not r.ok:
        print(f"  Trello chyba {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json() if r.text else None


def get_custom_field_ids():
    fields = trello_request("GET", f"/boards/{TRELLO_BOARD_ID}/customFields")
    by_name = {f["name"]: f["id"] for f in fields}
    for required in (TRELLO_CUSTOM_FIELD_NAME, TRELLO_ALIAS_FIELD_NAME):
        if required not in by_name:
            raise RuntimeError(
                f"Custom field '{required}' sa na boarde nenašiel. "
                f"Skontroluj presný názov (Menu boardu -> Custom Fields)."
            )
    return by_name[TRELLO_CUSTOM_FIELD_NAME], by_name[TRELLO_ALIAS_FIELD_NAME]


def get_target_list_ids():
    lists = trello_request("GET", f"/boards/{TRELLO_BOARD_ID}/lists")
    if not TRELLO_LIST_NAMES:
        print("TRELLO_LIST_NAMES nie je nastavené -> synchronizujem VŠETKY listy na boarde.")
        return [l["id"] for l in lists]
    ids = [l["id"] for l in lists if l["name"] in TRELLO_LIST_NAMES]
    missing = set(TRELLO_LIST_NAMES) - {l["name"] for l in lists if l["id"] in ids}
    if missing:
        print(f"  Pozor: tieto listy sa na boarde nenašli: {missing}")
    return ids


def get_cards(list_ids):
    # customFieldItems=true vrati v jednom volani aj hodnoty custom fieldov
    # na kazdej karte (vratane historie aliasov).
    all_cards = trello_request(
        "GET",
        f"/boards/{TRELLO_BOARD_ID}/cards",
        params={"fields": "name,idList", "customFieldItems": "true"},
    )
    return [c for c in all_cards if c["idList"] in list_ids]


def get_stored_aliases(card, alias_field_id):
    for item in card.get("customFieldItems", []):
        if item.get("idCustomField") == alias_field_id:
            text = (item.get("value") or {}).get("text") or ""
            return [a for a in text.split(ALIAS_SEPARATOR) if a]
    return []


def store_aliases(card_id, alias_field_id, aliases):
    trello_request(
        "PUT",
        f"/cards/{card_id}/customField/{alias_field_id}/item",
        json_body={"value": {"text": ALIAS_SEPARATOR.join(aliases)}},
    )


def get_workspace_users():
    return clockify_get(f"/workspaces/{CLOCKIFY_WORKSPACE_ID}/users", params={"page-size": 200})


def parse_iso8601_duration(s):
    """Prevedie Clockify duration format napr. 'PT1H30M15S' na sekundy."""
    if not s:
        return 0
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def collect_seconds_by_description(users):
    """Stiahne vsetky time entries v danom Clockify projekte (pre vsetkych
    clenov workspace) a sposcita sekundy podla presneho znenia popisu."""
    totals = defaultdict(int)
    for user in users:
        page = 1
        while True:
            entries = clockify_get(
                f"/workspaces/{CLOCKIFY_WORKSPACE_ID}/user/{user['id']}/time-entries",
                params={"project": CLOCKIFY_PROJECT_ID, "page": page, "page-size": 100},
            )
            if not entries:
                break
            for e in entries:
                desc = (e.get("description") or "").strip()
                interval = e.get("timeInterval") or {}
                totals[desc] += parse_iso8601_duration(interval.get("duration"))
            if len(entries) < 100:
                break
            page += 1
    return totals


def update_custom_field(card_id, field_id, hours):
    trello_request(
        "PUT",
        f"/cards/{card_id}/customField/{field_id}/item",
        json_body={"value": {"number": str(hours)}},
    )


def main():
    print("Spúšťam synchronizáciu Clockify -> Trello...")
    hours_field_id, alias_field_id = get_custom_field_ids()
    list_ids = get_target_list_ids()
    if not list_ids:
        print("Žiadne cieľové listy sa nenašli, koniec.")
        return

    cards = get_cards(list_ids)
    print(f"Nájdených {len(cards)} kariet na synchronizáciu.")
    users = get_workspace_users()
    print(f"Vo workspace je {len(users)} členov.")

    totals = collect_seconds_by_description(users)
    print(f"V Clockify projekte nájdených {len(totals)} unikátnych popisov time entries.")

    for card in cards:
        current_name = card["name"].strip()
        aliases = get_stored_aliases(card, alias_field_id)

        if current_name not in aliases:
            aliases.append(current_name)
            store_aliases(card["id"], alias_field_id, aliases)
            print(f"  (Nový názov zaznamenaný: '{current_name}', history má teraz {len(aliases)} záznam(ov))")

        seconds = sum(totals.get(a, 0) for a in aliases)
        hours = round(seconds / 3600, 2)
        update_custom_field(card["id"], hours_field_id, hours)
        print(f"  '{current_name}' -> {hours} h  (aliasy: {aliases})")

    print("Hotovo.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"CHYBA API volania: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"Chýba povinná premenná prostredia: {e}", file=sys.stderr)
        sys.exit(1)
