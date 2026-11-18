#!/usr/bin/env python3
"""Locks (Kalshi lagging, hedged on Polymarket) and plain arbitrage: what can be tuned?

A. LOCK, on the Kalshi lagging moves of GET /adaptive/dump (same gate as hedge_research.py):
   - minimum locked return: how many locks and how good at 1/2/3/5%;
   - leg order: after a lock appears, which price goes first, Kalshi's stale one or Polymarket's
     hedge (worse by more than 0.5c);
   - fallback when the hedge does not come within H: snipe exit / hedge at whatever price at H /
     never buy without the hedge;
   - auto-unwind: sell both legs before the payout once the sale returns U of the locked profit.
B. ARBITRAGE, on the episodes saved by arb_census.py (JSON):
   - capture: polling every 2 s vs reacting to each book update;
   - return per hour of locked money (payout = kick-off + a typical game length);
   - minimum return thresholds;
   - auto-unwind, as for locks.

Fees: Kalshi 0.07 p(1-p), Polymarket 0.05 p(1-p) on every taker fill; 1c slippage on each
sale. Kalshi sizes recorded before 18 Nov ~16:00 UTC are unreliable (book bug), prices are not.

Usage: python3 -I tools/lock_arb_tuning.py ~/swifteye/oddsfeed.db dump.json episodes.json
"""
import json
import sqlite3
import statistics as st
import sys
from collections import Counter

SLIP, FILL = 0.01, 250
GATE = dict(min_books=1, max_followed=0.25, min_move=0.04, max_spread=0.04)
GAME_H = {"football": 2.0, "tennis": 2.0, "hockey": 2.75, "baseball": 3.0}
UNWIND_H = 4


def kfee(p):
    return 0.07 * p * (1 - p)


def pfee(p):
    return 0.05 * p * (1 - p)


def game_h(sport):
    return GAME_H.get(sport, 2.5)


def fmt(vals, pct=True):
    vals = [v for v in vals if v is not None]
    if not vals:
        return "n=0"
    m = st.mean(vals)
    return f"n={len(vals):>4} mean {100 * m:+.2f}%" if pct else f"n={len(vals):>4} median {st.median(vals):.1f}"


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.orient = {}

    def pm(self, m, o, t0, t1):
        return self.c.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms > ? AND ts_ms <= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (m, o, t0, t1)).fetchall()

    def pm_at(self, m, o, t):
        return self.c.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (m, o, t)).fetchone()

    def k(self, ev, ko, t0, t1):
        return self.c.execute("SELECT ts_ms, bid, ask FROM kalshi_quotes WHERE event = ? AND outcome = ? AND est = 0 AND ts_ms > ? AND ts_ms <= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (ev, ko, t0, t1)).fetchall()

    def k_at(self, ev, ko, t):
        return self.c.execute("SELECT ts_ms, bid, ask FROM kalshi_quotes WHERE event = ? AND outcome = ? AND est = 0 AND ts_ms <= ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (ev, ko, t)).fetchone()

    def kalshi_side(self, m, o, t):
        """(event, Kalshi outcome) for a match outcome: team order from prices (the closer mid)."""
        if m not in self.orient:
            r = self.c.execute("SELECT book_event_id, COALESCE(m.sport, '') FROM book_events b JOIN matches m ON m.id = b.match_id WHERE b.book = 'kalshi' AND b.match_id = ? ORDER BY b.last_seen_ms DESC LIMIT 1", (m,)).fetchone()
            if not r:
                self.orient[m] = None
            else:
                ev, sport = r
                sw = False
                if sport not in ("football", "handball"):
                    p = self.pm_at(m, "home", t)
                    kh, ka = self.k_at(ev, "home", t), self.k_at(ev, "away", t)
                    if p and kh and ka:
                        pm_mid = (p[1] + p[2]) / 2
                        sw = abs((ka[1] + ka[2]) / 2 - pm_mid) < abs((kh[1] + kh[2]) / 2 - pm_mid)
                self.orient[m] = (ev, sw)
        v = self.orient[m]
        if v is None:
            return None
        ev, sw = v
        return ev, ({"home": "away", "away": "home"}.get(o, o) if sw else o)

    def start(self, m):
        r = self.c.execute("SELECT COALESCE(start_ms, 0), sport FROM matches WHERE id = ?", (m,)).fetchone()
        return r if r else (0, "")


def unwind(paths, hold_value, cost, t0, u):
    """First time selling both legs returns at least u x the locked profit. paths = merged
    [(ts, value_now)]. Returns (minutes, profit per pair) or None."""
    target = cost + u * (hold_value - cost)
    for ts, v in paths:
        if v >= target and v > cost:
            return (ts - t0) / 60_000, v - cost
    return None


def merge_value(a, b, va, vb):
    """Carry-forward merge of two quote streams into (ts, va(qa) + vb(qb))."""
    ev = sorted([(q[0], 0, q) for q in a] + [(q[0], 1, q) for q in b], key=lambda x: x[0])
    qa = qb = None
    out = []
    for ts, w, q in ev:
        if w == 0:
            qa = q
        else:
            qb = q
        if qa and qb:
            out.append((ts, va(qa) + vb(qb)))
    return out


def part_a(db, dump_path):
    d = json.load(open(dump_path))
    rows = [r for r in d["rows"] if r["b"] == "kalshi"]
    ci, gi = d["catch"].index(0.5), d["give"].index(900)
    out = []
    for r in rows:
        if r["k"][1] < GATE["min_books"] or r["fol"][1] > GATE["max_followed"] or abs(r["bm"][1]) < GATE["min_move"] or r["sp"][1] > GATE["max_spread"]:
            continue
        snipe = r["x"][ci * len(d["give"]) + gi]
        if snipe is None:
            continue
        m, o, dr, t, px = r["m"], r["o"], r["d"], r["t"], r["px"]
        ks = db.kalshi_side(m, o, t)
        if not ks:
            continue
        ev, ko = ks
        hedge = lambda q: (1 - q[1] + SLIP) if dr > 0 else (q[2] + SLIP)  # buy PM No(o) / Yes(o)
        locked = lambda h: 1 / (px + h + kfee(px) + pfee(h)) - 1
        pmp = db.pm(m, o, t - 600_000, t + 300_000)
        before = [q for q in pmp if q[0] <= t + FILL]
        after = [q for q in pmp if q[0] > t]
        if not before:
            continue
        h0 = hedge(before[-1])
        l0 = locked(h0)
        # When does the lock first appear (decided on a quote, filled FILL later)?
        lock_t, lock_h = (t, h0) if l0 >= 0.01 else (None, None)
        if lock_t is None:
            for q in after:
                if locked(hedge(q)) >= 0.01:
                    qf = [x for x in pmp if x[0] <= q[0] + FILL][-1]
                    if locked(hedge(qf)) >= 0.01:
                        lock_t, lock_h = q[0], hedge(qf)
                    break
        res = dict(t=t, m=m, s=r["s"], dr=dr, px=px, snipe=snipe, l0=l0, lock_t=lock_t, lock_h=lock_h, ev=ev, ko=ko, o=o)
        # Fallback: hedge at whatever price at t + H.
        for H in (5, 30, 60, 300):
            q = [x for x in pmp if x[0] <= t + H * 1000]
            res[f"mkt{H}"] = locked(hedge(q[-1])) if q else None
        out.append(res)
    if not out:
        print("A: no Kalshi entries")
        return
    print(f"\n######## A. LOCK ({len(out)} Kalshi lagging moves, gate {GATE})")
    now_locks = [x for x in out if x["l0"] >= 0.01]
    print("  minimum locked return (lock right at the fill):")
    for m in (0.01, 0.02, 0.03, 0.05):
        xs = [x["l0"] for x in out if x["l0"] >= m]
        print(f"    >= {100 * m:.0f}%: {len(xs):>3} locks, mean {100 * st.mean(xs):.2f}%" if xs else f"    >= {100 * m:.0f}%: none")

    print("  fallback when the hedge is not there at the fill (per Kalshi buy; 'skip' = never buy unhedged):")
    for H in (5, 30, 60, 300):
        within = lambda x: x["lock_t"] is not None and x["lock_t"] - x["t"] <= H * 1000
        a = [locked_v for locked_v in [((1 / (x["px"] + x["lock_h"] + kfee(x["px"]) + pfee(x["lock_h"])) - 1) if within(x) else x["snipe"]) for x in out]]
        b = [((1 / (x["px"] + x["lock_h"] + kfee(x["px"]) + pfee(x["lock_h"])) - 1) if within(x) else x[f"mkt{H}"]) for x in out]
        print(f"    wait {H:>3}s: lock else snipe {fmt(a)} | lock else hedge at market {fmt(b)} | locks {sum(within(x) for x in out)}")
    print(f"    skip unless locked at the fill: {fmt([x['l0'] for x in now_locks])} on {len(now_locks)} of {len(out)} moves")

    # Leg order and unwind on the locks at the fill.
    first = Counter()
    k_life, p_life, unw = [], [], {0.5: [], 0.8: [], 1.0: []}
    hold_rates, unw_rates = [], {0.5: [], 0.8: []}
    for x in now_locks:
        t, dr, px = x["t"], x["dr"], x["px"]
        kp = db.k(x["ev"], x["ko"], t, t + UNWIND_H * 3_600_000)
        pp = db.pm(x["m"], x["o"], t, t + UNWIND_H * 3_600_000)
        k_entry = lambda q: (q[2] if dr > 0 else 1 - q[1]) + SLIP
        kt = next((q[0] for q in kp if k_entry(q) > px + 0.005), None)
        h0 = x["lock_h"]
        hedge = lambda q: (1 - q[1] + SLIP) if dr > 0 else (q[2] + SLIP)
        pt = next((q[0] for q in pp if hedge(q) > h0 + 0.005), None)
        if kt is not None:
            k_life.append((kt - t) / 1000)
        if pt is not None:
            p_life.append((pt - t) / 1000)
        first["kalshi price went first" if (kt or 9e18) < (pt or 9e18) else "polymarket hedge went first" if pt is not None else "neither within the window"] += 1
        cost = px + h0 + kfee(px) + pfee(h0)
        k_sell = lambda q: (q[1] if dr > 0 else 1 - q[2]) - SLIP
        p_sell = lambda q: (1 - q[2] if dr > 0 else q[1]) - SLIP  # we hold PM No (dr>0) or Yes
        vals = merge_value(kp, pp, lambda q: k_sell(q) - kfee(k_sell(q)), lambda q: p_sell(q) - pfee(p_sell(q)))
        start, sport = db.start(x["m"])
        hold_h = max(0.25, ((start + game_h(x["s"]) * 3_600_000) - t) / 3_600_000) if start else game_h(x["s"])
        hold_rates.append((1 - cost) / cost / hold_h)
        for u in unw:
            r = unwind(vals, 1.0, cost, t, u)
            unw[u].append(r)
            if r and u in unw_rates:
                unw_rates[u].append((r[1] / cost) / max(r[0] / 60, 1 / 60))
    print(f"  leg order on {len(now_locks)} locks: {dict(first)}; median life: Kalshi stale price {fmt(k_life, False)} s, Polymarket hedge {fmt(p_life, False)} s")
    print(f"  auto-unwind within {UNWIND_H} h (hold to payout = 100% of the locked profit):")
    for u, rs in unw.items():
        got = [r for r in rs if r]
        if got:
            print(f"    sell at {100 * u:.0f}% of the locked profit: {len(got)}/{len(rs)} unwound, median after {st.median(r[0] for r in got):.0f} min")
    if hold_rates:
        print(f"  return per hour of money locked: hold to payout median {100 * st.median(hold_rates):.2f}%/h" + "".join(f"; unwind {100 * u:.0f}% median {100 * st.median(v):.2f}%/h" for u, v in unw_rates.items() if v))


def part_b(db, ep_path):
    eps = json.load(open(ep_path))
    eps = [e for e in eps if e["fill0"] >= 10]
    print(f"\n######## B. ARBITRAGE ({len(eps)} gap episodes with $10+ at the best level, last census window)")
    dur = lambda e: (e["t1"] - e["t0"]) / 1000

    def capture(e, mode):
        d = dur(e)
        if mode == "poll2s":
            return max(0.0, min(1.0, (d - 0.25) / 2.0))
        return 1.0 if d >= 0.35 else 0.0

    prof = lambda e: e["ret0"] * e["fill0"] / (1 + e["ret0"])
    for mode in ("poll2s", "event"):
        exp_n = sum(capture(e, mode) for e in eps)
        exp_p = sum(capture(e, mode) * prof(e) for e in eps)
        print(f"  {mode:<7}: expected captures {exp_n:.0f}, locked at the best level ~${exp_p:.0f}")
    print("  thresholds (event-driven capture):")
    for lo, hi in ((0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)):
        xs = [e for e in eps if lo <= e["ret0"] < hi and capture(e, "event")]
        if xs:
            print(f"    {100 * lo:.0f}-{100 * hi:.0f}%: {len(xs):>4} episodes, median {st.median(dur(e) for e in xs):.1f} s, median ${st.median(e['fill0'] for e in xs):.0f}, ~${sum(prof(e) for e in xs):.0f}")
    rates = []
    for e in eps:
        if not e.get("start_ms"):
            continue
        h = max(0.25, (e["start_ms"] + game_h(e["s"]) * 3_600_000 - e["t0"]) / 3_600_000)
        rates.append((e["ret0"] / h, e))
    if rates:
        rates.sort(key=lambda r: r[0])
        print(f"  return per hour locked: median {100 * st.median(r for r, _ in rates):.2f}%/h; live games {100 * st.median([r for r, e in rates if e['t0'] >= e['start_ms']] or [0]):.2f}%/h, pre-match {100 * st.median([r for r, e in rates if e['t0'] < e['start_ms']] or [0]):.2f}%/h")
    # Unwind on episodes that lasted 2 s+ (takeable).
    ok = [e for e in eps if dur(e) >= 2]
    unw = {0.5: [], 0.8: [], 1.0: []}
    for e in ok:
        t, y, n = e["t0"], e["y"], e["n"]
        pp = db.pm(e["m"], e["o"], t, t + UNWIND_H * 3_600_000)
        kp = db.k(e["ev"], e["ko"], t, t + UNWIND_H * 3_600_000)
        if e["yes_on"] == "polymarket":  # hold PM Yes + Kalshi No
            va = lambda q: (q[1] - SLIP) - pfee(q[1] - SLIP)
            vb = lambda q: (1 - q[2] - SLIP) - kfee(1 - q[2] - SLIP)
        else:  # hold Kalshi Yes + PM No
            va = lambda q: (1 - q[2] - SLIP) - pfee(1 - q[2] - SLIP)
            vb = lambda q: (q[1] - SLIP) - kfee(q[1] - SLIP)
        vals = merge_value(pp, kp, va, vb)
        for u in unw:
            unw[u].append(unwind(vals, 1.0, e["cost"], t, u))
    print(f"  auto-unwind within {UNWIND_H} h on {len(ok)} takeable episodes:")
    for u, rs in unw.items():
        got = [r for r in rs if r]
        if got:
            print(f"    sell at {100 * u:.0f}% of the locked profit: {len(got)}/{len(rs)} unwound, median after {st.median(r[0] for r in got):.0f} min")
    print("  by sport (takeable): " + ", ".join(f"{k} {v}" for k, v in Counter(e["s"] for e in ok).most_common()))
    pre = [e for e in ok if e.get("start_ms") and e["t0"] < e["start_ms"]]
    print(f"  pre-match share (where the called-off rule applies): {len(pre)}/{len(ok)}")


if __name__ == "__main__":
    db = DB(sys.argv[1])
    part_a(db, sys.argv[2])
    part_b(db, sys.argv[3])
