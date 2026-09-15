"""
Sync natrackovaneho casu z Clockify do Trello Custom Field.

Ako to funguje:
1. Zoberie karty z vybranych Trello listov (TRELLO_LIST_NAMES).
2. Pre kazdeho clena workspace stiahne VSETKY time entries v danom
   Clockify projekte (CLOCKIFY_PROJECT_ID) a sposcita ich podla POPISU
   (description) - presne do tohto pola Clockify Trello extension uklada
   nazov karty pri trackovani ("Clockify will pick up Trello's card name").
   Task field sa NEPOUZIVA na parovanie, lebo extension ho pri trackovani
   priamo z Trello karty nenastavuje spolahlivo.
3. Pre kazdu kartu najde sucet podla PRESNEJ zhody: nazov karty == popis
   time entry, a zapise ho (v hodinach) do Trello Custom Field.

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
TRELLO_LIST_NAMES = [s.strip() for s in os.environ.get("TRELLO_LIST_NAMES", "").split(",") if s.strip()]

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


def get_custom_field_id():
    fields = trello_request("GET", f"/boards/{TRELLO_BOARD_ID}/customFields")
    for f in fields:
        if f["name"] == TRELLO_CUSTOM_FIELD_NAME:
            return f["id"]
    raise RuntimeError(
        f"Custom field '{TRELLO_CUSTOM_FIELD_NAME}' sa na boarde nenašiel. "
        f"Skontroluj presný názov (Menu boardu -> Custom Fields)."
    )


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
    all_cards = trello_request("GET", f"/boards/{TRELLO_BOARD_ID}/cards", params={"fields": "name,idList"})
    return [c for c in all_cards if c["idList"] in list_ids]


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
    clenov workspace) a sposcita sekundy podla presneho znenia popisu
    (description) - to je pole, kam extension uklada nazov Trello karty."""
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
    field_id = get_custom_field_id()
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
        seconds = totals.get(card["name"].strip(), 0)
        hours = round(seconds / 3600, 2)
        update_custom_field(card["id"], field_id, hours)
        print(f"  '{card['name']}' -> {hours} h")

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
