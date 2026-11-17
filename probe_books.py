r"""Probe bookmaker live-odds pages with Camoufox.

Checks whether each site's Cloudflare challenge (if any) is passed and what
content loads. Usage:

    .venv\Scripts\python.exe probe_books.py            # all sites
    .venv\Scripts\python.exe probe_books.py 0 3 7      # selected indices

Results are written to probe_results.json (one entry per site), screenshots to
probe_shots\{index}_{slug}.png.
"""

import json
import re
import sys
import time
from pathlib import Path

from camoufox.sync_api import Camoufox

SITES = [
    ("betboom", "https://betboom.ru/sport/live/football"),
    ("winline", "https://winline.ru/live"),
    ("favbet", "https://www.favbet.hr/en/live/all/"),
    ("ggbet", "https://gg.bet/no/live"),
    ("merkur", "https://www.merkurbets.de/en/sports/live"),
    ("bet365", "https://statshub.sportradar.com/bet365/en/sport/1"),
    ("pinnacle", "https://www.pinnacle.com/en/live/overview/"),
    ("betway", "https://betway.com/g/en/sports"),
    ("m88", "https://www.m88.com/sports/M%20Sports%20Seamless?Domain=https:%2F%2Fwww.m88.com&category=0&sportsId=52&showHeader=true"),
    ("20bet", "https://20betz0ne.com/no/live?top=1"),
    ("1win", "https://1win.com/betting/live"),
    ("novibet", "https://www.novibet.com/live-betting"),
    ("sts", "https://www.sts.pl/live/pilka-nozna"),
    ("thunderpick", "https://thunderpick.io/sports?list=Live"),
    ("norsk_tipping", "https://www.norsk-tipping.no/"),
]

RESULTS_FILE = Path(__file__).parent / "probe_results.json"
SHOTS_DIR = Path(__file__).parent / "probe_shots"

CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser", "one more step")


def probe(browser, index: int, slug: str, url: str) -> dict:
    result = {"index": index, "slug": slug, "url": url}
    context = browser.new_context()
    page = context.new_page()
    try:
        try:
            page.goto(url, timeout=45_000, wait_until="domcontentloaded")
        except Exception as exc:
            result["error"] = f"goto: {type(exc).__name__}: {str(exc)[:200]}"
            return result
        result["status"] = page.evaluate("() => { try { return performance.getEntriesByType('navigation')[0]?.responseStatus } catch { return null } }")

        saw_challenge = False
        for _ in range(6):
            title = page.title()
            body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 3000) : ''")
            if any(t in title.lower() for t in CHALLENGE_TITLES):
                saw_challenge = True
                time.sleep(5)
                continue
            if "challenge-platform" in (page.evaluate("() => document.documentElement.outerHTML.slice(0, 200000)") or ""):
                saw_challenge = True
                time.sleep(5)
                continue
            break
        else:
            title = page.title()
            body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 3000) : ''")

        result["title"] = title
        result["challenge_seen"] = saw_challenge
        result["challenge_passed"] = saw_challenge and not any(t in title.lower() for t in CHALLENGE_TITLES)
        text = re.sub(r"\s+", " ", body)
        result["text_len"] = len(text)
        result["snippet"] = text[:400]
        SHOTS_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOTS_DIR / f"{index:02d}_{slug}.png"))
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return result
    finally:
        context.close()


def main() -> None:
    picks = [int(a) for a in sys.argv[1:]] or list(range(len(SITES)))
    results = {}
    if RESULTS_FILE.exists():
        raw = RESULTS_FILE.read_text(encoding="utf-8").strip()
        if raw:
            results = {str(r["index"]): r for r in json.loads(raw)}

    with Camoufox(headless=True) as browser:
        for index in picks:
            slug, url = SITES[index]
            print(f"[{index}] {slug}: {url}", flush=True)
            result = probe(browser, index, slug, url)
            results[str(index)] = result
            RESULTS_FILE.write_text(json.dumps(list(results.values()), indent=2, ensure_ascii=False), encoding="utf-8")
            flag = "PASS" if result.get("challenge_passed") else ("CHALLENGE-STUCK" if result.get("challenge_seen") else "OK/NO-CF" if "error" not in result else "ERROR")
            print(f"    -> {flag} | status={result.get('status')} | title={result.get('title', '')[:60]!r} | {result.get('error', '')}", flush=True)
    print("Done.")


if __name__ == "__main__":
    main()
