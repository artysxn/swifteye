"""Record-only live-odds ingest for 9 new book sources (owner rule 22 Nov 2026).

Path per book:
  bcgame, rollbit, rainbet, csgo500, csgoempire : sptpub JSON API (plain requests, no browser)
  cloudbet                                     : same-origin in-page fetch of the site's own feed
  shuffle                                      : no public odds JSON found -> scrape the rendered page
  csgoroll, gamdom                             : no sports section (user-confirmed 2026-11-23) -> not polled

Every poll POSTs {book, recv_ms, events} to oddsfeed's /ingest.
"""
import hashlib
import json
import re
import sys
import time

import requests

API = "http://127.0.0.1:8787"
POLL_S = 5
SPT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# book -> (host, brand id)
SPT = {
    "bcgame": ("https://api-k-c7818b61-623.sptpub.com", "2103509236163162112"),
    "rollbit": ("https://api-a-c7818b61-600.sptpub.com", "2103518166582956032"),
    "rainbet": ("https://api-a-c7818b61-600.sptpub.com", "2374656571012681728"),
    "csgo500": ("https://api-h-c7818b61-608.sptpub.com", "2195256717934206976"),
    "csgoempire": ("https://api-h-c7818b61-608.sptpub.com", "2432911154364948480"),
}
CLOUD_KEYS = ("soccer", "american_football", "esport_fifa", "esport_nba")
DOM_BOOKS = ("shuffle",)

def now_ms():
    return int(time.time() * 1000)

def ingest(book, events):
    """POST one poll of the book's live list to oddsfeed /ingest."""
    if not events:
        return True
    try:
        r = requests.post(API + "/ingest", json={"book": book, "recv_ms": now_ms(), "events": events}, timeout=10)
        r.raise_for_status()
        return True
    except Exception as ex:
        print(book, "ingest fail:", str(ex)[:120], flush=True)
        return False

def spt_quotes(mkts, sports_name):
    quotes = []
    if not isinstance(mkts, dict):
        return quotes
    for mid, groups in mkts.items():
        if not isinstance(groups, dict):
            continue
        for group, inner in groups.items():
            gk, _, gv = group.partition("=")
            if mid == "1" and group == "":
                # 1x2: bets 1/2/3 = home/draw/away
                outmap = {"1": "home", "2": "draw", "3": "away"}
                kind = ("1x2", None)
            elif mid == "18" and gk == "total":
                # market 18 = total goals: bets 13=over, 12=under
                try:
                    line = float(gv)
                except ValueError:
                    continue
                outmap = {"13": "over", "12": "under"}
                kind = ("total", line)
            else:
                continue
            if not isinstance(inner, dict):
                continue
            for bid, b in inner.items():
                odds = (b or {}).get("k")
                if not odds:
                    continue
                try:
                    price = float(odds)
                except ValueError:
                    continue
                if price < 1.01:
                    continue
                out = outmap.get(bid)
                if out:
                    quotes.append({"market": kind[0], "line": kind[1], "outcome": out, "price": price})
    return quotes

def poll_spt(book):
    host, brand = SPT[book]
    s = requests.Session()
    s.headers["User-Agent"] = SPT_UA
    r = s.get(host + "/api/v4/live/brand/" + brand + "/en/0", timeout=15)
    r.raise_for_status()
    version = str(r.json().get("version") or 0)
    r2 = s.get(host + "/api/v4/live/brand/" + brand + "/en/" + version, timeout=30)
    r2.raise_for_status()
    j = r2.json()
    if not isinstance(j.get("events"), dict):
        raise RuntimeError("no events in snapshot")
    name2id, foot_ids = {}, set()
    for sid, sp in (j.get("sports") or {}).items():
        nm = (sp or {}).get("name", "").lower()
        name2id[sid] = nm
        if "foot" in nm or "soccer" in nm:
            foot_ids.add(sid)
    events = []
    for eid, ev in j["events"].items():
        d = ev.get("desc") or {}
        if foot_ids and d.get("sport") not in foot_ids:
            continue
        if (ev.get("state") or {}).get("status") == 3:
            continue
        comps = d.get("competitors") or []
        if len(comps) < 2:
            continue
        q = spt_quotes(ev.get("markets"), name2id.get(d.get("sport"), ""))
        if not q:
            continue
        events.append({
            "id": str(eid),
            "sport": "football",
            "league": (j.get("tournaments") or {}).get(d.get("tournament"), {}).get("name", ""),
            "home": comps[0].get("name", "?"),
            "away": comps[1].get("name", "?"),
            "start_ms": int(d.get("scheduled") or 0) * 1000,
            "quotes": q,
        })
    return events

CLOUD_FETCH = """async (u) => {
    try { const r = await fetch(u, {credentials: 'include'});
          return {s: r.status, b: await r.text()}; }
    catch (e) { return {s: -1, b: String(e)}; }
}"""

def cloud_quote(market_key, outcome, params):
    """(market, line, outcome) or None. outcome/params come from the v6 selection."""
    ok = market_key.split(".")[-1]
    if ok == "match_odds":
        return ("1x2", 0.0, outcome) if outcome in ("home", "draw", "away") else None
    if ok == "total_goals":
        if outcome not in ("over", "under"):
            return None
        for p in (params or "").split("&"):
            if p.startswith("total="):
                try:
                    return ("total", float(p[6:]), outcome)
                except ValueError:
                    return None
        return None
    if ok == "asian_handicap":
        if outcome not in ("home", "away"):
            return None
        for p in (params or "").split("&"):
            if p.startswith("handicap="):
                try:
                    return ("handicap", float(p[9:]), outcome)
                except ValueError:
                    return None
        return None
    if ok == "btts":
        if outcome == "yes":
            return ("btts", 0.0, "over")
        if outcome == "no":
            return ("btts", 0.0, "under")
    return None

CLOUD_MARKETS = "&".join("markets=" + m for m in
                         ("soccer.match_odds", "soccer.total_goals", "soccer.asian_handicap", "soccer.btts"))

def cloud_events_for(page, comp_key, comp_name, ev_list):
    """Fetch v6 per-event odds for events from the live list."""
    events = []
    for ev in ev_list or []:
        eid = ev.get("id")
        if eid is None:
            continue
        r = page.evaluate(CLOUD_FETCH, "https://www.cloudbet.com/sports-api/c/v6/sports/events/"
                        + str(eid) + "?" + CLOUD_MARKETS)
        if r.get("s") != 200:
            continue
        try:
            mkts = json.loads(r["b"]).get("markets") or {}
        except ValueError:
            continue
        quotes = []
        for mkey, mkt in mkts.items():
            subs = (mkt or {}).get("submarkets") or {}
            for sub in subs.values():
                for sel in (sub or {}).get("selections") or []:
                    price = sel.get("price")
                    if not price or sel.get("status") != "SELECTION_ENABLED":
                        continue
                    q = cloud_quote(mkey, sel.get("outcome", ""), sel.get("params", ""))
                    if q:
                        quotes.append({"market": q[0], "line": q[1], "outcome": q[2], "price": price})
        if not quotes:
            continue
        parts = (ev.get("home") or {}).get("name", "?"), (ev.get("away") or {}).get("name", "?")
        events.append({
            "id": comp_key + ":" + str(eid),
            "sport": "football",
            "league": comp_name,
            "home": parts[0],
            "away": parts[1],
            "start_ms": 0,
            "quotes": quotes,
        })
    return events

def poll_cloudbet(page):
    r = page.evaluate(CLOUD_FETCH, "https://www.cloudbet.com/sports-api/c/v6/sports/events"
                        "?live=true&locale=en&sports=soccer")
    if r.get("s") != 200:
        return []
    j = json.loads(r["b"])
    events = []
    for sport in j.get("sports") or []:
        if (sport.get("key") or "").startswith("soccer"):
            for comp in sport.get("competitions") or []:
                if not (comp.get("events") or []):
                    continue
                events.extend(cloud_events_for(page, comp.get("key", ""), comp.get("name", ""), comp.get("events")))
    return events

def dom_events(book, page):
    """Shuffle card layout (innerText): league, '+N', Home, 'vs.', Away, date,
    then odds block: Home, <o1>, [Draw, <ox>], Away, <o2> - each on its own line."""
    try:
        text = page.evaluate("() => document.body.innerText") or ""
    except Exception as ex:
        print(book, "dom scrape fail:", str(ex)[:120], flush=True)
        return []
    lines = [l.strip() for l in text.replace("\r", "\n").split("\n") if l.strip()]
    events = []
    seen = set()
    for i, line in enumerate(lines):
        if not re.fullmatch(r"\s*vs\.", line):
            continue
        home = lines[i - 1] if i >= 1 else ""
        away = lines[i + 1] if i + 1 < len(lines) else ""
        if not home or not away or len(home) > 60 or len(away) > 60:
            continue
        odds, j = [], i + 1
        while j + 1 < len(lines) and len(odds) < 3 and (j - i) < 12:
            a, n = lines[j], lines[j + 1]
            if a == home and re.fullmatch(r"\d{1,2}\.\d{2}", n):
                odds.append(float(n)); j += 2
            elif a == "Draw" and re.fullmatch(r"\d{1,2}\.\d{2}", n):
                odds.append(float(n)); j += 2
            elif a == away and re.fullmatch(r"\d{1,2}\.\d{2}", n):
                odds.append(float(n)); break
            else:
                j += 1
        if len(odds) < 2 or (home, away) in seen:
            continue
        seen.add((home, away))
        league = re.sub(r"\+\d+$", "", lines[i - 3]).strip() if i >= 3 else ""
        quotes = [{"market": "1x2", "line": 0.0, "outcome": "home", "price": odds[0]}]
        if len(odds) == 3:
            quotes.append({"market": "1x2", "line": 0.0, "outcome": "draw", "price": odds[1]})
        quotes.append({"market": "1x2", "line": 0.0, "outcome": "away", "price": odds[-1]})
        hid = int(hashlib.sha1(("%s|%s" % (home, away)).encode("utf-8")).hexdigest(), 16) % (1 << 60)
        events.append({
            "id": "dom:%d" % hid,
            "sport": "football",
            "league": league,
            "home": home,
            "away": away,
            "start_ms": 0,
            "quotes": quotes,
        })
    return events

DOM_URLS = {
    "shuffle": "https://shuffle.com/sports?section=live",
}

def main():
    print("browser_books: sptpub=%s cloudbet+dom=%s not polled: csgoroll, gamdom (no sports section)"
          % (", ".join(SPT), ", ".join(("cloudbet",) + DOM_BOOKS)), flush=True)
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=True, i_know_what_im_doing=True) as b:
        cloud_page = b.new_page()
        cloud_page.goto("https://www.cloudbet.com/en", wait_until="domcontentloaded", timeout=90_000)
        dom_pages = {}
        for book in DOM_BOOKS:
            p = b.new_page()
            p.goto(DOM_URLS[book], wait_until="domcontentloaded", timeout=90_000)
            p.wait_for_timeout(6000)  # let lazy live lists render once
            dom_pages[book] = p
        dom_last_reload = {}
        while True:
            t0 = time.time()
            for book in SPT:
                try:
                    ingest(book, poll_spt(book))
                except Exception as ex:
                    print(book, "poll fail:", str(ex)[:120], flush=True)
            try:
                ingest("cloudbet", poll_cloudbet(cloud_page))
            except Exception as ex:
                print("cloudbet poll fail:", str(ex)[:120], flush=True)
            for book, p in dom_pages.items():
                # the page is not self-updating: scrape once per 60 s reload
                last = dom_last_reload.get(book, 0)
                if time.time() - last > 60:
                    try:
                        p.goto(DOM_URLS[book], wait_until="domcontentloaded", timeout=90_000)
                        p.wait_for_timeout(5000)
                        dom_last_reload[book] = time.time()
                        events = dom_events(book, p)
                        ingest(book, events)
                        print("%s dom: %d events" % (book, len(events)), flush=True)
                    except Exception as ex:
                        print(book, "reload fail:", str(ex)[:120], flush=True)
            elapsed = time.time() - t0
            if elapsed < POLL_S:
                time.sleep(POLL_S - elapsed)

if __name__ == "__main__":
    main()

