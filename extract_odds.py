r"""Capture the JSON APIs a bookmaker page loads, to find its live-odds source.

    .venv\Scripts\python.exe extract_odds.py          # all candidates
    .venv\Scripts\python.exe extract_odds.py 1 3      # selected indices

For each site: opens the page with Camoufox, records every XHR/fetch response
that looks like JSON, waits for the SPA to render, then writes
extract_out\{slug}.json (endpoints + samples) and prints a summary.
"""

import json
import re
import sys
import time
from pathlib import Path

from camoufox.sync_api import Camoufox

SITES = [
    ("winline", "https://winline.ru/live"),
    ("ggbet", "https://gg.bet/no/live"),
    ("bet365", "https://statshub.sportradar.com/bet365/en/sport/1"),
    ("betway", "https://betway.com/g/en/sports"),
    ("20bet", "https://20betz0ne.com/no/live?top=1"),
    ("m88", "https://www.m88.com/sports/M%20Sports%20Seamless?Domain=https:%2F%2Fwww.m88.com&category=0&sportsId=52&showHeader=true"),
    ("favbet", "https://www.favbet.hr/en/live/all/"),
    ("novibet", "https://www.novibet.com/live-betting"),
    ("sts", "https://www.sts.pl/live/pilka-nozna"),
    ("thunderpick", "https://thunderpick.io/sports?list=Live"),
]

OUT_DIR = Path(__file__).parent / "extract_out"
RENDER_WAIT = 25  # seconds to let the SPA load its data


def looks_like_odds(body: str) -> bool:
    lowered = body.lower()
    return any(k in lowered for k in ('"odds"', '"coef', '"price', '"decimal', '1x2', 'over/under', '"markets"'))


def capture(browser, index: int, slug: str, url: str) -> None:
    context = browser.new_context()
    page = context.new_page()
    endpoints = []

    def on_response(resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            if "json" not in ctype and not resp.url.endswith((".json", ".json5")):
                return
            body = resp.text()
            if not body or body.startswith("<"):
                return
            entry = {
                "url": resp.url,
                "status": resp.status,
                "size": len(body),
                "odds_like": looks_like_odds(body[:200_000]),
                "sample": body[:600],
            }
            endpoints.append(entry)
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(url, timeout=45_000, wait_until="domcontentloaded")
    except Exception as exc:
        print(f"  goto error: {type(exc).__name__}: {str(exc)[:150]}")
    # let the SPA fetch its data
    for _ in range(RENDER_WAIT):
        time.sleep(1)
        if page.is_closed():
            break
    title = page.title() if not page.is_closed() else ""
    text = ""
    if not page.is_closed():
        try:
            text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
        except Exception:
            pass
    context.close()

    odds_endpoints = [e for e in endpoints if e["odds_like"]]
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"{slug}.json").write_text(
        json.dumps({"url": url, "title": title, "page_text": re.sub(r"\s+", " ", text),
                    "odds_endpoints": odds_endpoints,
                    "all_json_endpoints": [e for e in endpoints if not e["odds_like"]][:40]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")
    print(f"  title={title[:60]!r} | json endpoints: {len(endpoints)} | odds-like: {len(odds_endpoints)}")
    for e in odds_endpoints[:6]:
        print(f"    {e['status']} {e['size']:>8} B  {e['url'][:120]}")


def main() -> None:
    picks = [int(a) for a in sys.argv[1:]] or list(range(len(SITES)))
    with Camoufox(headless=True, i_know_what_im_doing=True) as browser:
        for index in picks:
            slug, url = SITES[index]
            print(f"[{index}] {slug}", flush=True)
            capture(browser, index, slug, url)
    print("Done. Details in extract_out\\")


if __name__ == "__main__":
    main()
