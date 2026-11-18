#!/usr/bin/env python3
"""Polymarket snipe: which exit, how fast, which prices, which bookmakers, what size?

Takes the bot's own entries (every bookmaker move from GET /adaptive/dump, filtered by the
Adaptive bot's rules) and replays what happens after the fill on the recorded Polymarket quotes
(pm_quotes) and bookmaker prices (changes). Same entries, different decisions:

  exits     sell at the bid when caught up (taker, the bot today) with give-up times from 10 s
            to 30 min; a bookmaker-reversal stop; a maker sale (resting order at the catch-up
            price, no fee, filled only when the bid trades 1c THROUGH it, else taker at give-up)
  speed     the same with dumps taken at 0.5 s / 0.25 s reaction (pass several --dump)
  prices    return by entry-price band (the fee peaks at 50c)
  books     return by the bookmaker that moved first; walk-forward: books picked on the first
            half, judged on the second
  sizing    stake by expected edge vs flat, walk-forward

Every number is per $ staked, at a flat $100 at the best price (the entry price from the dump
includes 1c slippage; every taker sale is 1c worse; Polymarket fee 0.05 p(1-p) on every taker
fill). In-sample warning: the rules come from the bot's walk-forward picks on this same data.

Usage: python3 -I tools/snipe_tuning.py ~/swifteye/oddsfeed.db dump.json:1000:500 [dump2.json:500:250 ...]
"""
import json
import sqlite3
import statistics as st
import sys
from collections import defaultdict

SLIP, FILL, RATE, STAKE = 0.01, 250, 0.05, 100.0
PRE_MS, SAME_MOVE_MS = 10_000, 120_000
# The bot's rule sets (Adaptive, Polymarket): picked now, and the defaults.
RULES = {
    "current": dict(min_books=2, max_followed=0.5, min_edge=0.04, max_spread=0.06, min_move=0.07, catch=1.0, give=1800),
    "defaults": dict(min_books=2, max_followed=0.5, min_edge=0.04, max_spread=0.02, min_move=0.07, catch=1.0, give=900),
}
CATCH, GIVE = [0.5, 0.75, 1.0], [60, 120, 300, 900, 1800]


def fee(n, p):
    return RATE * n * p * (1 - p)


def entries(rows, r):
    ci = CATCH.index(r["catch"])
    out, recent = [], []
    for x in sorted(rows, key=lambda x: x["t"]):
        k, bm, fol, sp, e = x["k"], x["bm"], x["fol"], x["sp"], x["e"]
        if min(k) < r["min_books"] or bm[0] < r["min_move"] or max(fol) > r["max_followed"] or max(sp) > r["max_spread"] + 1e-9 or e[ci] < r["min_edge"]:
            continue
        recent = [q for q in recent if x["t"] - q[1] < SAME_MOVE_MS]
        if any(q[0] == x["m"] for q in recent):
            continue
        recent.append((x["m"], x["t"]))
        out.append(x)
    return out


class Replay:
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def quotes(self, m, o, t0, t1):
        return self.db.execute("SELECT ts_ms, bid, ask, last FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms", (m, o, t0, t1)).fetchall()

    def at(self, m, o, t):
        r = self.db.execute("SELECT ts_ms, bid, ask, last FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1", (m, o, t)).fetchone()
        return r

    def book(self, m, b, o, t0, t1):
        """Trigger bookmaker's implied probability: (prob at t0, [(ts, prob)] after)."""
        q = "SELECT ts_ms, new_price FROM changes WHERE match_id = ? AND book = ? AND market = '1x2' AND outcome = ? AND new_price IS NOT NULL AND ts_ms {} ORDER BY ts_ms {}"
        a = self.db.execute(q.format("<= ? ", "DESC LIMIT 1"), (m, b, o, t0)).fetchone()
        path = self.db.execute(q.format("> ? AND ts_ms <= ?", ""), (m, b, o, t0, t1)).fetchall()
        p = lambda d: 1 / max(d, 1.0001)
        return (p(a[1]) if a else None), [(t, p(d)) for t, d in path]

    def prep(self, x, delay):
        """Everything after the fill for one entry, cached."""
        key = (x["m"], x["o"], x["t"])
        if key in self.cache:
            return self.cache[key]
        m, o, t, d = x["m"], x["o"], x["t"], x["d"]
        t0 = t - delay - FILL
        q0 = self.at(m, o, t0 - PRE_MS)
        if not q0 or q0[1] is None or q0[2] is None:
            self.cache[key] = None
            return None
        pre_mid = (q0[1] + q0[2]) / 2
        path = [q for q in self.quotes(m, o, t, t + max(GIVE) * 1000 + 2 * FILL) if q[1] is not None and q[2] is not None]
        b_post, bpath = self.book(m, x["tr"], o, t, t + max(GIVE) * 1000)
        v = dict(pre_mid=pre_mid, path=path, b_post=b_post, bpath=bpath)
        self.cache[key] = v
        return v


def our_bid(q, d):
    return q[1] if d > 0 else 1 - q[2]


def sell_at(path, ts, d, fallback):
    """Taker sale FILL after `ts`: the standing quote then, 1c worse."""
    q = None
    for r in path:
        if r[0] <= ts + FILL:
            q = r
        else:
            break
    q = q or fallback
    return (our_bid(q, d) - SLIP) if q else None


def exit_return(x, pv, catch, give, stop=None, maker=None):
    """Return per $ for one entry. stop = reversal share of the bookmaker move; maker = offset of
    the resting sale from the catch-up price (None = taker)."""
    d, px, bm1, s1 = x["d"], x["px"], x["bm"][0], x["sp"][0]
    path, pre = pv["path"], pv["pre_mid"]
    n = STAKE / px
    need = catch * bm1
    end = x["t"] + give * 1000
    our_pre = pre if d > 0 else 1 - pre
    level = max(our_pre + need - s1 / 2 + (maker or 0.0), px + 0.01) if maker is not None else None
    # Reversal: the trigger book gives back `stop` of its move.
    rev_t = None
    if stop is not None and pv["b_post"] is not None:
        for t, p in pv["bpath"]:
            if t > end:
                break
            if (p - pv["b_post"]) * d <= -stop * bm1:
                rev_t = t
                break
    for q in path:
        if q[0] > end:
            break
        if rev_t is not None and q[0] >= rev_t:
            break
        if maker is not None:
            last_ours = (q[3] if d > 0 else 1 - q[3]) if q[3] is not None else None
            if our_bid(q, d) >= level + 0.01 or (last_ours is not None and last_ours >= level + 0.01):
                return (n * (level - px) - fee(n, px)) / STAKE, "maker"
            continue
        side_px = q[1] if d > 0 else q[2]
        if (side_px - pre) * d >= need - s1 / 2:
            s = sell_at(path, q[0], d, q)
            return (n * (s - px) - fee(n, px) - fee(n, s)) / STAKE, "caught"
    t_out = min(end, rev_t) if rev_t is not None else end
    if path and path[-1][0] < t_out and t_out > path[-1][0] + 3_600_000:
        return None, "open"
    s = sell_at(path, t_out, d, path[0] if path else None)
    if s is None:
        return None, "no quote"
    return (n * (s - px) - fee(n, px) - fee(n, s)) / STAKE, ("reversed" if rev_t is not None and rev_t <= end else "gave up")


def fmt(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n=0"
    se = (st.pstdev(vals) / len(vals) ** 0.5) if len(vals) > 1 else 0
    return f"n={len(vals):>3}  {100 * st.mean(vals):+6.2f}% ±{200 * se:4.1f}  win {100 * sum(v > 0 for v in vals) / len(vals):3.0f}%"


def halves(ents):
    ents = sorted(ents, key=lambda x: x["t"])
    h = ents[len(ents) // 2]["t"] if ents else 0
    return [("all", ents), ("1st half", [x for x in ents if x["t"] < h]), ("2nd half", [x for x in ents if x["t"] >= h])]


def main(db_path, dumps):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rp = Replay(db)
    base = None
    for spec in dumps:
        path, reaction, confirm = spec.split(":")
        delay = int(reaction) + int(confirm)
        d = json.load(open(path))
        rows = [x for x in d["rows"] if x["b"] == "polymarket"]
        rp.cache = {}
        print(f"\n######## reaction {reaction} ms + confirm {confirm} ms ({len(rows)} Polymarket moves)")
        for name, r in RULES.items():
            ents = entries(rows, r)
            ci, gi = CATCH.index(r["catch"]), GIVE.index(r["give"])
            print(f"  rules {name}: {len(ents)} trades; bot's own exit (dump): {fmt([x['x'][ci * len(GIVE) + gi] for x in ents])}")
        if base is not None:
            continue  # the exit study runs once, on the first dump
        base = (rows, delay)
        ents = [x for x in entries(rows, RULES["current"]) if rp.prep(x, delay)]
        print(f"\n== EXITS on the current rules' {len(ents)} trades (replayed; first row should match the dump)")
        variants = [("taker catch 1.0, give 1800 s (bot)", dict(catch=1.0, give=1800))]
        variants += [(f"taker catch 1.0, give {g} s", dict(catch=1.0, give=g)) for g in (10, 20, 30, 60, 120, 300, 900)]
        variants += [(f"taker catch 0.75, give {g} s", dict(catch=0.75, give=g)) for g in (30, 60, 300)]
        variants += [(f"reversal stop {s:.2f}, give {g} s", dict(catch=1.0, give=g, stop=s)) for s in (0.33, 0.5) for g in (60, 300, 1800)]
        variants += [(f"maker at catch-up{'+1c' if m else ''}, give {g} s", dict(catch=1.0, give=g, maker=m)) for m in (0.0, 0.01) for g in (60, 300, 1800)]
        variants += [(f"maker + reversal 0.5, give {g} s", dict(catch=1.0, give=g, maker=0.0, stop=0.5)) for g in (60, 300)]
        results = {}
        for label, kw in variants:
            res = [(x, *exit_return(x, rp.prep(x, delay), **kw)) for x in ents]
            results[label] = res
            line = "  ".join(f"{h}: {fmt([r for x, r, _ in res if x in part])}" for h, part in halves(ents))
            how = defaultdict(int)
            for _, r, k in res:
                how[k] += 1
            print(f"  {label:<36} {line}   [{', '.join(f'{k} {v}' for k, v in sorted(how.items()))}]")
        bot = {id(x): r for x, r, _ in results["taker catch 1.0, give 1800 s (bot)"]}

        print("\n== ENTRY PRICE bands (bot's exit)")
        bands = [(0.0, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 1.0)]
        for lo, hi in bands:
            part = [x for x in ents if lo <= x["px"] < hi]
            print(f"  {lo:.2f}-{hi:.2f}: " + "  ".join(f"{h}: {fmt([bot[id(x)] for x in p])}" for h, p in halves(part)) if part else f"  {lo:.2f}-{hi:.2f}: none")

        print("\n== BOOKMAKER that moved first (bot's exit)")
        by = defaultdict(list)
        for x in ents:
            by[x["tr"]].append(x)
        for b, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print(f"  {b:<12} {fmt([bot[id(x)] for x in xs])}")
        (_, a), (_, h1), (_, h2) = halves(ents)
        good = {b for b in by if len([x for x in h1 if x["tr"] == b]) >= 3 and st.mean(bot[id(x)] for x in h1 if x["tr"] == b) > 0}
        print(f"  walk-forward: books positive on the 1st half (3+ trades): {sorted(good)}")
        print(f"    2nd half, those books only: {fmt([bot[id(x)] for x in h2 if x['tr'] in good])}   all books: {fmt([bot[id(x)] for x in h2])}")

        print("\n== SIZING by expected edge (bot's exit), stake = $100 x clip(edge / 4c, 0.5, 3)")
        ci = CATCH.index(1.0)
        for h, part in halves(ents):
            w = [min(3.0, max(0.5, x["e"][ci] / 0.04)) for x in part]
            r = [bot[id(x)] for x in part]
            ok = [(wi, ri) for wi, ri in zip(w, r) if ri is not None]
            if not ok:
                continue
            flat = sum(ri for _, ri in ok) / len(ok)
            sized = sum(wi * ri for wi, ri in ok) / sum(wi for wi, _ in ok)
            print(f"  {h}: flat {100 * flat:+.2f}% a $, edge-sized {100 * sized:+.2f}% a $ ({sum(wi for wi, _ in ok) / len(ok):.2f}x the money on average)")
        edges = sorted((x["e"][ci], bot[id(x)]) for x in ents if bot[id(x)] is not None)
        q = len(edges) // 3
        if q:
            for name, part in (("low edge third", edges[:q]), ("mid", edges[q:2 * q]), ("high edge third", edges[2 * q:])):
                print(f"  {name:<16} edge {100 * part[0][0]:.1f}-{100 * part[-1][0]:.1f}c: {fmt([r for _, r in part])}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
