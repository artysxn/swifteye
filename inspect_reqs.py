r"""Log the exact method/headers/body of each book's odds API request.

    .venv\Scripts\python.exe inspect_reqs.py
"""

import json
import time
from pathlib import Path

from camoufox.sync_api import Camoufox

SITES = [
    ("m88", "https://www.m88.com/sports/M%20Sports%20Seamless?Domain=https:%2F%2Fwww.m88.com&category=0&sportsId=52&showHeader=true", "M88S/data/main"),
    ("betway", "https://betway.com/g/en/sports", "getEventsWithMultipleMarkets"),
    ("thunderpick", "https://thunderpick.io/sports?list=Live", "/api/matches"),
    ("novibet", "https://www.novibet.com/live-betting", "marketviews/location"),
    ("favbet", "https://www.favbet.hr/en/live/all/", "frontend_api"),
]

OUT = Path("extract_out/requests")
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    with Camoufox(headless=True) as browser:
        for slug, url, marker in SITES:
            context = browser.new_context()
            page = context.new_page()
            hits = []

            def on_request(req):
                if marker in req.url and req.resource_type in ("xhr", "fetch"):
                    hits.append({
                        "url": req.url,
                        "method": req.method,
                        "headers": req.headers,
                        "post_data": req.post_data,
                    })

            page.on("request", on_request)
            page.goto(url, timeout=60_000, wait_until="domcontentloaded")
            time.sleep(15)
            page.close()
            context.close()

            (OUT / f"{slug}.json").write_text(json.dumps(hits, indent=2, ensure_ascii=False), encoding="utf-8")
            for h in hits[:3]:
                print(f"[{slug}] {h['method']} {h['url'][:160]}")
                if h["post_data"]:
                    print(f"    body: {h['post_data'][:200]}")
            if not hits:
                print(f"[{slug}] no matching request captured")


if __name__ == "__main__":
    main()
