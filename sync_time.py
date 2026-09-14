"""
Sync natrackovaneho casu z Clockify do Trello Custom Field.

Ako to funguje:
1. Zoberie karty z vybranych Trello listov (TRELLO_LIST_NAMES).
2. Pre kazdu kartu zisti Clockify task ID:
   - ak uz karta ma ulozene ID v custom field "Clockify Task ID", pouzije ho
     priamo (nezalezi na tom, ako sa karta odvtedy premenovala),
   - ak este ID ulozene nema, najde task podla NAZVU (jednorazovo, len pri
     prvom sparovani) a ID si ulozi na kartu pre buduce behy.
3. Ak sa nazov karty odvtedy zmenil, skript premenuje aj Clockify task,
   aby si oba nazvy zostali zosynchronizovane (cisto informativne, na
   parovanie sa uz nepouziva).
4. Sposcita vsetok cas natrackovany na danom tasku (cez vsetkych clenov workspace).
5. Zapise sucet (v hodinach) do Trello Custom Field na danej karte.

Beziaci na GitHub Actions podla harmonogramu (pozri .github/workflows/sync.yml).
Nepouziva Clockify Reports API (ta vyzaduje placeny plan) - iba zakladne
Time Entry API, ktore funguje aj na Free plane.
"""

import os
import re
import sys
import time
import requests

# --- Konfiguracia z prostredia (nastavuje sa v GitHub Secrets/Variables) ---
CLOCKIFY_API_KEY = os.environ["CLOCKIFY_API_KEY"]
CLOCKIFY_WORKSPACE_ID = os.environ["CLOCKIFY_WORKSPACE_ID"]
CLOCKIFY_PROJECT_ID = os.environ["CLOCKIFY_PROJECT_ID"]

TRELLO_KEY = os.environ["TRELLO_KEY"]
TRELLO_TOKEN = os.environ["TRELLO_TOKEN"]
TRELLO_BOARD_ID = os.environ["TRELLO_BOARD_ID"]
TRELLO_CUSTOM_FIELD_NAME = os.environ.get("TRELLO_CUSTOM_FIELD_NAME", "Natrackovaný čas (h)")
TRELLO_TASK_ID_FIELD_NAME = os.environ.get("TRELLO_TASK_ID_FIELD_NAME", "Clockify Task ID")
TRELLO_LIST_NAMES = [s.strip() for s in os.environ.get("TRELLO_LIST_NAMES", "").split(",") if s.strip()]

CLOCKIFY_BASE = "https://api.clockify.me/api/v1"
TRELLO_BASE = "https://api.trello.com/1"

CLOCKIFY_HEADERS = {"X-Api-Key": CLOCKIFY_API_KEY}


def clockify_get(path, params=None, retries=5):
    """GET na Clockify API s automatickym cakanim pri 429 (Too Many Requests)."""
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
    for required in (TRELLO_CUSTOM_FIELD_NAME, TRELLO_TASK_ID_FIELD_NAME):
        if required not in by_name:
            raise RuntimeError(
                f"Custom field '{required}' sa na boarde nenašiel. "
                f"Skontroluj presný názov (Menu boardu -> Custom Fields)."
            )
    return by_name[TRELLO_CUSTOM_FIELD_NAME], by_name[TRELLO_TASK_ID_FIELD_NAME]


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
    # customFieldItems=true nam v jednom volani vrati aj hodnoty custom fieldov
    # na kazdej karte, takze nemusime robit samostatny request pre kazdu kartu.
    all_cards = trello_request(
        "GET",
        f"/boards/{TRELLO_BOARD_ID}/cards",
        params={"fields": "name,idList", "customFieldItems": "true"},
    )
    return [c for c in all_cards if c["idList"] in list_ids]


def get_stored_task_id(card, task_id_field_id):
    for item in card.get("customFieldItems", []):
        if item.get("idCustomField") == task_id_field_id:
            return (item.get("value") or {}).get("text")
    return None


def find_clockify_task_by_name(card_name):
    tasks = clockify_get(
        f"/workspaces/{CLOCKIFY_WORKSPACE_ID}/projects/{CLOCKIFY_PROJECT_ID}/tasks",
        params={"name": card_name, "strict-name-search": "true"},
    )
    return tasks[0] if tasks else None


def get_clockify_task_by_id(task_id):
    """Vrati task, alebo None ak uz neexistuje (napr. bol v Clockify zmazany)."""
    try:
        return clockify_get(
            f"/workspaces/{CLOCKIFY_WORKSPACE_ID}/projects/{CLOCKIFY_PROJECT_ID}/tasks/{task_id}"
        )
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def rename_clockify_task(task_id, new_name):
    url = f"{CLOCKIFY_BASE}/workspaces/{CLOCKIFY_WORKSPACE_ID}/projects/{CLOCKIFY_PROJECT_ID}/tasks/{task_id}"
    r = requests.put(url, headers=CLOCKIFY_HEADERS, json={"name": new_name}, timeout=30)
    r.raise_for_status()


def store_task_id_on_card(card_id, task_id_field_id, task_id):
    trello_request(
        "PUT",
        f"/cards/{card_id}/customField/{task_id_field_id}/item",
        json_body={"value": {"text": task_id}},
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


def sum_task_seconds(task_id, users):
    """Sposcita cas na danom tasku naprieč všetkými členmi workspace."""
    total_seconds = 0
    for user in users:
        page = 1
        while True:
            entries = clockify_get(
                f"/workspaces/{CLOCKIFY_WORKSPACE_ID}/user/{user['id']}/time-entries",
                params={"task": task_id, "page": page, "page-size": 100},
            )
            if not entries:
                break
            for e in entries:
                interval = e.get("timeInterval") or {}
                total_seconds += parse_iso8601_duration(interval.get("duration"))
            if len(entries) < 100:
                break
            page += 1
    return total_seconds


def update_custom_field(card_id, field_id, hours):
    trello_request(
        "PUT",
        f"/cards/{card_id}/customField/{field_id}/item",
        json_body={"value": {"number": str(hours)}},
    )


def resolve_task_for_card(card, task_id_field_id):
    """Vrati Clockify task_id pre danu kartu, s riesenim premenovania.

    1. Ak karta uz ma ulozene ID, over ze task este existuje a ak sa
       nazov karty odvtedy zmenil, premenuje aj Clockify task.
    2. Ak ID ulozene nema (prve sparovanie, alebo ulozeny task uz
       neexistuje), najde task podla aktualneho nazvu karty a ID ulozi.
    """
    stored_id = get_stored_task_id(card, task_id_field_id)

    if stored_id:
        task = get_clockify_task_by_id(stored_id)
        if task:
            if task.get("name") != card["name"]:
                rename_clockify_task(stored_id, card["name"])
                print(f"    (Clockify task premenovaný na '{card['name']}')")
            return stored_id
        print(f"    Uložené ID {stored_id} už v Clockify neexistuje, hľadám podľa mena...")

    task = find_clockify_task_by_name(card["name"])
    if not task:
        return None
    store_task_id_on_card(card["id"], task_id_field_id, task["id"])
    print(f"    Prvé spárovanie -> uložené Clockify Task ID {task['id']} na kartu.")
    return task["id"]


def main():
    print("Spúšťam synchronizáciu Clockify -> Trello...")
    hours_field_id, task_id_field_id = get_custom_field_ids()
    list_ids = get_target_list_ids()
    if not list_ids:
        print("Žiadne cieľové listy sa nenašli, koniec.")
        return

    cards = get_cards(list_ids)
    print(f"Nájdených {len(cards)} kariet na synchronizáciu.")
    users = get_workspace_users()
    print(f"Vo workspace je {len(users)} členov.")

    ok, skipped = 0, 0
    for card in cards:
        task_id = resolve_task_for_card(card, task_id_field_id)
        if not task_id:
            print(f"  Preskakujem '{card['name']}' — nenašiel sa zhodný Clockify task.")
            skipped += 1
            continue
        seconds = sum_task_seconds(task_id, users)
        hours = round(seconds / 3600, 2)
        update_custom_field(card["id"], hours_field_id, hours)
        print(f"  '{card['name']}' -> {hours} h")
        ok += 1

    print(f"Hotovo. Aktualizovaných: {ok}, preskočených (bez zhody): {skipped}.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"CHYBA API volania: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"Chýba povinná premenná prostredia: {e}", file=sys.stderr)
        sys.exit(1)
