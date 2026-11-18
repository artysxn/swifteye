#!/usr/bin/env python3
"""Kalshi lag: sell on catch-up, hedge on Polymarket, or hold to the result?

Bookmakers move first, Polymarket follows within seconds, Kalshi often much later (or never within
the give-up time). The Snipe bot buys the stale Kalshi side and sells on Kalshi when it catches up.
This compares, on the SAME entries, what else could be done with them:

  snipe   sell on Kalshi at catch-up / give-up (the bot's own exit; from /adaptive/dump)
  lock0   at the fill, also buy the other side on Polymarket (its ask + 1c): a locked pair if
          Kalshi price + Polymarket price + both fees < $1
  lockH   the first moment within H seconds after the fill where that pair locks at least
          MARGIN (decided on prices known then); if it never does, the snipe exit instead
  hold    keep the Kalshi position to the result (from final prices)

Inputs: the JSON of GET /adaptive/dump (every bookmaker move, both markets, with the Kalshi
entry price and the sale results) and the database (pm_quotes for the hedge, final prices).
Usage: python3 -I tools/hedge_research.py dump.json ~/swifteye/oddsfeed.db
"""
import json
import sqlite3
import statistics as st
import sys
from collections import defaultdict

SLIP = 0.01
PM_RATE = 0.05  # Polymarket sports taker fee: 0.05 x p(1-p) a contract
K_RATE = 0.07
H_S = [5, 30, 60, 300]
MARGIN = 0.01
FILL_MS = 250
STAKE = 100.0
# Entry gate (Kalshi): broad on purpose, to have a sample. Moves where Kalshi has made at most this
# share of the bookmakers' move, at least this many independent books, move of 4+ points.
GATE = dict(min_books=1, max_followed=0.25, min_move=0.04, max_spread=0.04)


def kfee(p):
    return K_RATE * p * (1 - p)


def pfee(p):
    return PM_RATE * p * (1 - p)


def main(dump_path, db_path):
    d = json.load(open(dump_path))
    catch, give = d["catch"], d["give"]
    # The Kalshi bot's default exit: catch 0.5, give-up 900 s.
    ci, gi = catch.index(0.5) if 0.5 in catch else 0, give.index(900) if 900 in give else 0
    rows = [r for r in d["rows"] if r["b"] == "kalshi"]
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q_path = "SELECT ts_ms, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1"
    q_after = "SELECT ts_ms, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms"
    q_final = "SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND (bid IS NOT NULL OR ask IS NOT NULL) ORDER BY ts_ms DESC LIMIT 1"

    final = {}

    def result(m, o):
        """1 = this outcome won, 0 = lost, None = not settled in the data."""
        if (m, o) not in final:
            r = db.execute(q_final, (m, o)).fetchone()
            v = None
            if r:
                b, a = r
                if b is not None and b >= 0.97:
                    v = 1
                elif a is not None and a <= 0.03:
                    v = 0
            final[(m, o)] = v
        return final[(m, o)]

    out = []
    for r in rows:
        k, fol, sp, bm = r["k"], r["fol"], r["sp"], r["bm"]
        if k[1] < GATE["min_books"] or fol[1] > GATE["max_followed"] or abs(bm[1]) < GATE["min_move"] or sp[1] > GATE["max_spread"]:
            continue
        snipe = r["x"][ci * len(give) + gi]
        if snipe is None:
            continue
        m, o, dr, t, px = r["m"], r["o"], r["d"], r["t"], r["px"]
        # The hedge: bought Kalshi Yes(o) -> buy Polymarket No(o) = 1 - bid; bought Kalshi No(o) -> Polymarket Yes(o) = ask.
        def hedge_px(q):
            _, b, a, bs, asz = q
            if dr > 0:
                return (1 - b + SLIP) if b else None
            return (a + SLIP) if a else None

        def locked(h):
            if h is None or not (0.01 < h < 0.99):
                return None
            cost = px + h + kfee(px) + pfee(h)
            return 1 / cost - 1

        # Hedge fills FILL_MS after the decision at the quote standing then, +1c; the order needs
        # this many contracts (a $STAKE Kalshi buy) at the best level.
        need = STAKE / px
        before = db.execute(q_path, (m, o, t)).fetchone()
        path = db.execute(q_after, (m, o, t, t + max(H_S) * 1000 + FILL_MS)).fetchall()
        quotes = ([before] if before and t - before[0] < 600_000 else []) + path

        def at(ts):
            q = None
            for x in quotes:
                if x[0] <= ts:
                    q = x
                else:
                    break
            return q

        def size_ok(q):
            sz = (q[3] if dr > 0 else q[4])  # No on PM takes Yes bids; Yes takes asks
            return sz is None or sz >= need  # None = depth not recorded then

        def lock_at(decide_ts):
            qd = at(decide_ts)
            if qd is None or (locked(hedge_px(qd)) or -1) < MARGIN:
                return None
            qf = at(decide_ts + FILL_MS)
            v = locked(hedge_px(qf)) if qf else None
            return (v, size_ok(qf)) if v is not None else None

        l0x = lock_at(t)
        l0 = l0x[0] if l0x else None
        lockh, sizes = {}, []
        for h in H_S:
            got = l0x
            if got is None:
                for q in path:
                    if q[0] > t + h * 1000:
                        break
                    got = lock_at(q[0])
                    if got is not None:
                        break
            lockh[h] = got[0] if got else None
            if h == 60 and got:
                sizes.append(got[1])
        res = result(m, o)
        hold = None
        if res is not None:
            pay = res if dr > 0 else 1 - res
            hold = (pay - px - kfee(px)) / (px + kfee(px))
        out.append(dict(t=t, m=m, s=r["s"], snipe=snipe, l0=l0, lockh=lockh, hold=hold, dep=r["dep"], size_ok=sizes[0] if sizes else None))

    if not out:
        print("no Kalshi entries pass the gate")
        return
    out.sort(key=lambda x: x["t"])
    half = out[len(out) // 2]["t"]

    def summ(name, vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return f"{name:<34} n=0"
        se = st.pstdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else 0
        return f"{name:<34} n={len(vals):>4}  mean {100 * st.mean(vals):+6.2f}%  (±{200 * se:.1f})  win {100 * sum(v > 0 for v in vals) / len(vals):.0f}%"

    print(f"Kalshi entries passing the gate: {len(out)} ({len(set(x['m'] for x in out))} games); gate {GATE}")
    for label, part in [("ALL", out), ("first half", [x for x in out if x["t"] < half]), ("second half", [x for x in out if x["t"] >= half])]:
        print(f"\n== {label}")
        print(summ("snipe (sell on Kalshi)", [x["snipe"] for x in part]))
        print(summ("lock now: pair return if it locks", [x["l0"] for x in part]))
        print(f"{'  lock now >= 1%: share of entries':<34} {100 * sum(1 for x in part if x['l0'] is not None and x['l0'] >= MARGIN) / len(part):.0f}%")
        for h in H_S:
            comb = [x["lockh"][h] if x["lockh"][h] is not None else x["snipe"] for x in part]
            share = sum(1 for x in part if x["lockh"][h] is not None) / len(part)
            print(summ(f"lock within {h}s else snipe ({100 * share:.0f}% lock)", comb))
        print(summ("hold to the result", [x["hold"] for x in part]))
        locks = [x for x in part if x["size_ok"] is not None]
        if locks:
            print(f"{'  60s locks with enough PM size for $' + str(int(STAKE)):<34} {100 * sum(x['size_ok'] for x in locks) / len(locks):.0f}% of {len(locks)}")
    by = defaultdict(list)
    for x in out:
        by[x["s"]].append(x)
    print("\n== by sport: snipe / lock-within-60s-else-snipe / hold")
    for s, xs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        c60 = [x["lockh"][60] if x["lockh"][60] is not None else x["snipe"] for x in xs]
        hold = [x["hold"] for x in xs if x["hold"] is not None]
        print(f"  {s:<12} n={len(xs):>4}  snipe {100 * st.mean(x['snipe'] for x in xs):+6.2f}%  lock60 {100 * st.mean(c60):+6.2f}%  hold {100 * st.mean(hold) if hold else float('nan'):+6.2f}% ({len(hold)})")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
