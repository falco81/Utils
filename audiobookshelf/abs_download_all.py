#!/usr/bin/env python3
"""
AudioBookshelf - download all episodes of all podcasts
=======================================================
Exact replica of the UI flow:

  [1] "Look for new episodes after this date"  ->  PATCH lastEpisodeCheck=0
  [2] "Limit = 0"                               ->  GET checknew?limit=0
  [3] "Check & Download New Episodes"           ->  (triggered by step [2])

Per the official ABS API documentation:
  - PATCH /api/items/{id}/media  body: { "lastEpisodeCheck": 0 }
       -> resets "what we have already seen" to epoch zero
  - GET /api/podcasts/{id}/checknew?limit=0
       -> limit=0 means "all episodes will be downloaded"
       -> returns the list of episodes that WILL be downloaded
          (the server has already started downloading in the background)

Usage:
  pip3 install requests --break-system-packages
  python3 abs_download_all.py
"""

import sys
import requests

# ============ CONFIGURATION ============
ABS_URL    = "http://localhost:13378"
API_TOKEN  = "PUT_YOUR_API_TOKEN_HERE"   # Settings -> Users -> click on user -> API Token
VERIFY_SSL = True
# =======================================

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
    if API_TOKEN == "PUT_YOUR_API_TOKEN_HERE":
        print("ERROR: fill in API_TOKEN in the script", file=sys.stderr)
        sys.exit(1)

    me = api("GET", "/api/me")
    print(f"Connected as: {me.get('username', '?')} ({me.get('type', '?')})\n")

    libs_data = api("GET", "/api/libraries")
    libs = libs_data.get("libraries", libs_data) if isinstance(libs_data, dict) else libs_data
    podcast_libs = [l for l in libs if l.get("mediaType") == "podcast"]

    if not podcast_libs:
        print("No podcast libraries found.")
        return

    print(f"Podcast libraries: {len(podcast_libs)}\n")

    total_queued = 0
    skipped = 0
    errors = 0

    for lib in podcast_libs:
        items_data = api("GET", f"/api/libraries/{lib['id']}/items", params={"limit": 10000})
        items = items_data.get("results", []) if isinstance(items_data, dict) else []
        print(f"=== Library '{lib.get('name', '?')}' ({len(items)} podcasts) ===")

        for p in items:
            title = p.get("media", {}).get("metadata", {}).get("title", "(no title)")
            item_id = p.get("id")
            rss_url = p.get("media", {}).get("metadata", {}).get("feedUrl")

            if not rss_url:
                print(f"  SKIP {title}: no RSS feed URL")
                skipped += 1
                continue

            try:
                # [1] RESET: "Look for new episodes after this date" = epoch 0
                #     Makes the podcast treat ALL feed episodes as "new"
                api("PATCH", f"/api/items/{item_id}/media",
                    json={"lastEpisodeCheck": 0})

                # [2] CHECK & DOWNLOAD: limit=0 = all (not the default 3)
                #     Endpoint kicks off background download and returns the queued list
                result = api("GET", f"/api/podcasts/{item_id}/checknew",
                             params={"limit": 0})
                episodes = result.get("episodes", []) if isinstance(result, dict) else []
                count = len(episodes)

                print(f"  OK   {title}: queued {count} episodes")
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

    print(f"Done. Total queued: {total_queued}, skipped: {skipped}, errors: {errors}")
    print("ABS is downloading in the background. Watch the web UI -> Queue.")


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
        print(f"FATAL: cannot connect to {ABS_URL}: {e}", file=sys.stderr)
        sys.exit(1)
