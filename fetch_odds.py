r"""Fetch live-odds JSON from each book through Camoufox.

Opens the book's live page (clears any Cloudflare challenge, sets cookies),
then replays the odds API call from inside the page (same-origin fetch with
the browser's cookies/headers), and saves the raw payload.

    .venv\Scripts\python.exe fetch_odds.py            # all books
    .venv\Scripts\python.exe fetch_odds.py 20bet m88  # selected

Output: extract_out/full/{slug}_{i}.json
"""

import json
import sys
import time
import urllib.parse
from pathlib import Path

from camoufox.sync_api import Camoufox

OUT = Path(__file__).parent / "extract_out" / "full"
OUT.mkdir(parents=True, exist_ok=True)

# slug -> (live page url, [(method, api url, body-or-None), ...])
BOOKS = [
    ("20bet", "https://20betz0ne.com/no/live?top=1", [
        ("GET", "https://platform.20betz0ne.com/api/event/list?lang=no&relations=odds&relations=withMarketsCount&relations=result&relations=league&relations=competitors&relations=sportCategories&relations=tips&relations=additionalInfo&relations=broadcasts&relations=statistics&oddsExists_eq=1&main=1&limit=30&status_in=2&status_in=1&isTopLive_eq=1&oddsBooster=0&isFavorite=0&isLive=true", None),
    ]),
    ("m88", "https://www.m88.com/sports/M%20Sports%20Seamless?Domain=https:%2F%2Fwww.m88.com&category=0&sportsId=52&showHeader=true", [
        ("POST", "https://msports.m88.com/api/v1/M88S/data/main",
         {"lang_id": "en", "view_type": "0", "leagues": "999", "odds_type": "2", "odds_group": "3",
          "view_date": "0", "time_filter": "0", "category": "0", "branch_code": "888", "currency": "MYR",
          "is_full": "true", "prev_params": "", "version": "", "is_stream": "false", "is_live_center": "false",
          "sport_id": "52", "display_type": "11", "top_x": 0, "currentMenu": "spesificSport",
          "currentTimeSlot": "1", "prevMenu": "spesificSport", "method": "LoadOddsDataLive"}),
    ]),
    ("thunderpick", "https://thunderpick.io/sports?list=Live", [
        ("POST", "https://thunderpick.io/api/matches",
         {"gameIds": [10, 11, 12, 13, 14, 15, 16, 17, 18, 24, 25, 26, 27, 28, 29, 30, 31, 43, 44, 45, 46, 47, 48],
          "competitionId": None, "country": None, "parentTournamentUrlAcronym": None, "matchesView": "normal"}),
    ]),
    ("novibet", "https://www.novibet.com/live-betting", [
        ("GET", "https://www.novibet.com/spt/feed/marketviews/location/v2/4324/4390/?lang=en-US&timeZ=Romance%20Standard%20Time&oddsR=1&usrGrp=null&timestamp={ts}", None),
    ]),
    ("favbet", "https://www.favbet.hr/en/live/all/", [
        ("POST", "https://www.favbet.hr/service/lineout/frontend_api2/",
         {"jsonrpc": "2.0", "id": 1, "method": "frontend/event/get",
          "params": {"by": {"lang": "en", "sport_id": 1, "head_markets": True, "service_id": 1}}}),
        ("POST", "https://www.favbet.hr/service/lineout/frontend_api/",
         {"lang": "en", "tags": ["top_live"]}),
    ]),
]

INDEX = {slug: i for i, (slug, _, _) in enumerate(BOOKS)}


def api_call(page, method, url, body):
    return page.evaluate(
        """async ([method, url, body]) => {
            const opts = {method, headers: {}};
            if (body) {
                opts.headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }
            try {
                const r = await fetch(url, opts);
                const text = await r.text();
                return {status: r.status, body: text.slice(0, 6000000)};
            } catch (e) {
                return {status: -1, body: String(e)};
            }
        }""",
        [method, url, body],
    )


def main() -> int:
    slugs = sys.argv[1:] or [slug for slug, _, _ in BOOKS]
    picks = [BOOKS[INDEX[s]] for s in slugs]

    with Camoufox(headless=True) as browser:
        for slug, url, calls in picks:
            page = browser.new_page()
            print(f"=== {slug} ===", flush=True)
            try:
                page.goto(url, timeout=60_000, wait_until="domcontentloaded")
                time.sleep(12)
            except Exception as e:
                print(f"  page load failed: {e}")
            for i, (method, c_url, body) in enumerate(calls):
                if "{ts}" in c_url:
                    c_url = c_url.replace("{ts}", str(int(time.time() * 10_000)))
                if urllib.parse.urlsplit(c_url).netloc != urllib.parse.urlsplit(url).netloc:
                    # cross-origin (e.g. msports.m88.com): in-page fetch would hit
                    # CORS, so use the context request instead
                    if method == "POST":
                        r = page.request.post(c_url, data=json.dumps(body), headers={
                            "Content-Type": "application/json",
                            "Referer": "https://msports.m88.com/app/v2/",
                            "Origin": "https://msports.m88.com",
                            "Authorization": "Bearer GUEST",
                        })
                    else:
                        r = page.request.get(c_url)
                    res = {"status": r.status, "body": r.text()}
                else:
                    res = api_call(page, method, c_url, body)
                out = OUT / f"{slug}_{i}.json"
                out.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"  [{i}] {method} {c_url[:100]} -> {res['status']} {len(res['body'])} B")
            page.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
