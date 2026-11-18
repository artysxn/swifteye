#!/usr/bin/env python3
"""How many Polymarket x Kalshi arbitrages really existed, after both fees and book size?

Replays the recorded best prices and best-level sizes of every game listed on both venues
(pm_quotes, kalshi_quotes with real quotes only) and finds the moments where buying Yes on one
venue and No on the other cost under $1 after Kalshi's fee (0.07 p(1-p)) and Polymarket's sports
fee (0.05 p(1-p)). Consecutive moments of one gap are one episode. For each episode: how long it
lasted, its best return, the dollars fillable at the best level of both books, whether it opened
because Polymarket moved (Kalshi stale) or Kalshi moved (Polymarket stale), and whether a
bookmaker had moved that outcome by 3+ points in the 60 s before.

Usage: python3 -I tools/arb_census.py ~/swifteye/oddsfeed.db [hours=24] [min_return=0.01]
"""
import json
import sqlite3
import statistics as st
import sys
import time
from collections import Counter, defaultdict

PM_RATE, K_RATE = 0.05, 0.07


def unit_pm(p):
    return p + PM_RATE * p * (1 - p)


def unit_k(p):
    return p + K_RATE * p * (1 - p)


def orientation(db, mid, ev, since):
    """True if Kalshi lists the teams the other way round: compare Polymarket's home mid with
    Kalshi's "home" and "away" mids over the same minutes; the closer one is the same team."""
    pm = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = 'home' AND ts_ms > ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (mid, since)).fetchall()
    if not pm:
        return None
    step = max(1, len(pm) // 30)
    diff = {"home": [], "away": []}
    for t, b, a in pm[::step]:
        for ko in ("home", "away"):
            r = db.execute("SELECT bid, ask FROM kalshi_quotes WHERE event = ? AND outcome = ? AND est = 0 AND ts_ms <= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (ev, ko, t)).fetchone()
            if r:
                diff[ko].append(abs((r[0] + r[1]) / 2 - (a + b) / 2))
    if not diff["home"] or not diff["away"]:
        return None
    return st.mean(diff["away"]) < st.mean(diff["home"])


def main(db_path, hours=24.0, min_ret=0.01, out_json=None):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    since = int((time.time() - hours * 3600) * 1000)
    games = db.execute(
        """SELECT m.id, m.sport, m.home, m.away, k.book_event_id, COALESCE(k.home, ''), COALESCE(k.away, '')
           FROM matches m JOIN book_events k ON k.match_id = m.id AND k.book = 'kalshi'
           WHERE k.last_seen_ms > ? AND EXISTS (SELECT 1 FROM book_events p WHERE p.match_id = m.id AND p.book = 'polymarket' AND p.last_seen_ms > ?)""",
        (since, since),
    ).fetchall()
    eps = []
    for mid, sport, mh, ma, ev, eh, ea in games:
        sw = False if sport in ("football", "handball") else orientation(db, mid, ev, since)
        if sw is None:
            continue
        outs = ["home", "away", "draw"] if sport in ("football", "handball") else ["home", "away"]
        for o in outs:
            ko = {"home": "away", "away": "home"}.get(o, o) if sw else o
            pm = db.execute("SELECT ts_ms, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms > ? ORDER BY ts_ms", (mid, o, since)).fetchall()
            kq = db.execute("SELECT ts_ms, bid, ask, bid_size, ask_size FROM kalshi_quotes WHERE event = ? AND outcome = ? AND est = 0 AND ts_ms > ? ORDER BY ts_ms", (ev, ko, since)).fetchall()
            if not pm or not kq:
                continue
            bm = db.execute(
                "SELECT ts_ms, old_price, new_price FROM changes WHERE match_id = ? AND market = '1x2' AND outcome = ? AND book NOT IN ('polymarket', 'kalshi') AND ts_ms > ? ORDER BY ts_ms",
                (mid, o, since - 60_000),
            ).fetchall()
            bm_moves = [t for t, a, b in bm if a and b and a > 1 and b > 1 and abs(1 / b - 1 / a) >= 0.03]
            # Merge both streams in time.
            ev_list = [(r[0], "pm", r) for r in pm] + [(r[0], "k", r) for r in kq]
            ev_list.sort(key=lambda x: x[0])
            P = K = None
            cur = None  # open episode
            for t, src, r in ev_list:
                if src == "pm":
                    P = r
                else:
                    K = r
                best = None
                if P and K and P[1] and P[2] and K[1] and K[2]:
                    # Yes on PM + No on Kalshi; Yes on Kalshi + No on PM.
                    for yes_on, y, n, ysz, nsz in (("polymarket", P[2], 1 - K[1], P[4], K[3]), ("kalshi", K[2], 1 - P[1], K[4], P[3])):
                        if not (0.01 < y < 0.99 and 0.01 < n < 0.99):
                            continue
                        c = (unit_pm(y) + unit_k(n)) if yes_on == "polymarket" else (unit_k(y) + unit_pm(n))
                        ret = 1 / c - 1
                        if ret >= min_ret and (best is None or ret > best[1]):
                            fill = min(ysz or 0, nsz or 0) * c
                            best = (yes_on, ret, fill, y, n, c)
                if best and cur is None:
                    recent_bm = any(t - 60_000 <= x <= t for x in bm_moves)
                    cur = dict(m=mid, s=sport, o=o, ko=ko, ev=ev, t0=t, t1=t, ret=best[1], fill=best[2], yes_on=best[0], opened_by=src, bm=recent_bm,
                               y=best[3], n=best[4], cost=best[5], ret0=best[1], fill0=best[2])
                elif best and cur is not None:
                    cur["t1"] = t
                    cur["ret"] = max(cur["ret"], best[1])
                    cur["fill"] = max(cur["fill"], best[2])
                elif not best and cur is not None:
                    cur["t1"] = t
                    eps.append(cur)
                    cur = None
            if cur is not None:
                eps.append(cur)
    if not eps:
        print("no arbitrage episodes")
        return
    if out_json:
        starts = dict(db.execute("SELECT id, COALESCE(start_ms, 0) FROM matches WHERE id IN (%s)" % ",".join(str(e["m"]) for e in eps)).fetchall())
        for e in eps:
            e["start_ms"] = starts.get(e["m"], 0)
        json.dump(eps, open(out_json, "w"))
    games_n = len(set(e["m"] for e in eps))
    dur = [(e["t1"] - e["t0"]) / 1000 for e in eps]
    print(f"Last {hours:g} h, {len(games)} games on both venues: {len(eps)} episodes >= {100 * min_ret:.1f}% after fees in {games_n} games")
    print(f"  duration s: median {st.median(dur):.0f}, p25 {sorted(dur)[len(dur) // 4]:.0f}, p75 {sorted(dur)[3 * len(dur) // 4]:.0f}")
    print(f"  best return: median {100 * st.median(e['ret'] for e in eps):.1f}%; fillable at the best level: median ${st.median(e['fill'] for e in eps):.0f}, sum of episode max ${sum(e['fill'] for e in eps):.0f}")
    ok = [e for e in eps if e["fill"] >= 10 and (e["t1"] - e["t0"]) >= 2000]
    print(f"  lasting 2 s+ with $10+ fillable: {len(ok)} episodes, {len(set(e['m'] for e in ok))} games, locked profit at best level ~${sum(e['ret'] * e['fill'] / (1 + e['ret']) for e in ok):.0f}")
    for label, key in (("opened by a move on", "opened_by"), ("Yes bought on", "yes_on"), ("bookmaker moved 3+ pts in the 60 s before", "bm"), ("sport", "s")):
        c = Counter(e[key] for e in ok)
        print(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in c.most_common()))
    by = defaultdict(list)
    for e in ok:
        by[(e["opened_by"], e["bm"])].append(e)
    print("  by (opened by, bookmaker moved first): n, median return, median fillable $, median duration s")
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        print(f"    {k}: {len(v)}, {100 * st.median(e['ret'] for e in v):.1f}%, ${st.median(e['fill'] for e in v):.0f}, {st.median((e['t1'] - e['t0']) / 1000 for e in v):.0f}")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(a[0], float(a[1]) if len(a) > 1 else 24.0, float(a[2]) if len(a) > 2 else 0.01, a[3] if len(a) > 3 else None)
