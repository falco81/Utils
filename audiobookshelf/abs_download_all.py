#!/usr/bin/env python3
"""
AudioBookshelf - stažení všech epizod všech podcastů
=====================================================
Přesná replika UI toku:

  [1] "Look for new episodes after this date"  ->  PATCH lastEpisodeCheck=0
  [2] "Limit = 0"                               ->  GET checknew?limit=0
  [3] "Check & Download New Episodes"           ->  (volá se v kroku [2])

Podle oficiální ABS API dokumentace:
  - PATCH /api/items/{id}/media  body: { "lastEpisodeCheck": 0 }
       -> resetuje "co už jsme viděli" na epoch zero
  - GET /api/podcasts/{id}/checknew?limit=0
       -> limit=0 znamená "all episodes will be downloaded"
       -> endpoint vrátí seznam epizod co BUDOU staženy (server už pustil stahování)

Použití:
  pip3 install requests --break-system-packages
  python3 abs_download_all.py
"""

import sys
import requests

# ============ KONFIGURACE ============
ABS_URL    = "http://localhost:13378"
API_TOKEN  = "SEM_VLOZ_SVUJ_API_TOKEN"   # Settings -> Users -> klik na user -> API Token
VERIFY_SSL = True
# =====================================

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}


def api(method, path, **kwargs):
    r = requests.request(
        method, f"{ABS_URL}{path}",
        headers=HEADERS, verify=VERIFY_SSL, timeout=60,
        **kwargs,
    )
    r.raise_for_status()
    if not r.text:
        return None
    try:
        return r.json()
    except ValueError:
        return r.text


def main():
    if API_TOKEN == "SEM_VLOZ_SVUJ_API_TOKEN":
        print("CHYBA: vyplň API_TOKEN v skriptu", file=sys.stderr)
        sys.exit(1)

    me = api("GET", "/api/me")
    print(f"Připojeno jako: {me.get('username', '?')} ({me.get('type', '?')})\n")

    libs_data = api("GET", "/api/libraries")
    libs = libs_data.get("libraries", libs_data) if isinstance(libs_data, dict) else libs_data
    podcast_libs = [l for l in libs if l.get("mediaType") == "podcast"]

    if not podcast_libs:
        print("Žádné podcastové knihovny nenalezeny.")
        return

    print(f"Podcastových knihoven: {len(podcast_libs)}\n")

    total_queued = 0
    skipped = 0
    errors = 0

    for lib in podcast_libs:
        items_data = api("GET", f"/api/libraries/{lib['id']}/items", params={"limit": 10000})
        items = items_data.get("results", []) if isinstance(items_data, dict) else []
        print(f"=== Knihovna '{lib.get('name', '?')}' ({len(items)} podcastů) ===")

        for p in items:
            title = p.get("media", {}).get("metadata", {}).get("title", "(bez názvu)")
            item_id = p.get("id")
            rss_url = p.get("media", {}).get("metadata", {}).get("feedUrl")

            if not rss_url:
                print(f"  SKIP {title}: nemá RSS feed URL")
                skipped += 1
                continue

            try:
                # [1] RESET: "Look for new episodes after this date" = epoch 0
                #     Nastaví podcast tak, že VŠECHNY epizody ve feedu budou "nové"
                api("PATCH", f"/api/items/{item_id}/media",
                    json={"lastEpisodeCheck": 0})

                # [2] CHECK & DOWNLOAD: limit=0 = všechny (ne default 3)
                #     Endpoint na pozadí spustí stahování a vrátí seznam frontovaných
                result = api("GET", f"/api/podcasts/{item_id}/checknew",
                             params={"limit": 0})
                episodes = result.get("episodes", []) if isinstance(result, dict) else []
                count = len(episodes)

                print(f"  OK   {title}: zařazeno {count} epizod")
                total_queued += count

            except requests.exceptions.HTTPError as e:
                code = e.response.status_code if e.response is not None else "?"
                msg = e.response.text[:200] if e.response is not None else str(e)
                print(f"  ERR  {title}: HTTP {code} - {msg}", file=sys.stderr)
                errors += 1
            except Exception as e:
                print(f"  ERR  {title}: {e}", file=sys.stderr)
                errors += 1

        print()

    print(f"Hotovo. Celkem zařazeno: {total_queued}, přeskočeno: {skipped}, chyb: {errors}")
    print("ABS si stahuje na pozadí. Sleduj web UI -> Queue.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except requests.exceptions.HTTPError as e:
        resp = e.response.text[:500] if e.response is not None else "?"
        print(f"FATAL HTTP: {e}\n{resp}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"FATAL: nelze se připojit na {ABS_URL}: {e}", file=sys.stderr)
        sys.exit(1)
