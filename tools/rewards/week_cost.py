"""The reward markets the rule picks RIGHT NOW: what would a week of real trades have cost the minimum pair there?
(24 Nov, PC.) Reads recorder 2's files in this folder (rec2.py running), then for each market the rule picks at the
newest pass: Polymarket's 1-minute price history of the last 7 days and every real trade (reward_sim.history, cached in
hist/). Our pair = a buy of Yes and a buy of No, each D cents from the price of the minute before the last re-set
(never past the reward limit), the minimum size, re-set every RESET minutes. A side fills when a real trade printed at
or through it (also shown: plus every time the price series itself crossed it); a filled side stays off until the next
re-set, at most 2 orders deep a side. A fill is valued 1 h later, 24 h later and at the end of the history.
The pot share uses the others' score of the newest pass for the whole week: NOT known for the past (the weak point; the
recorder will show whether these markets stay empty). The pot itself is re-set by Polymarket every day.
usage: python week_cost.py [min $ a day, default 3] [D cents, default 3]"""
import bisect
import collections
import json
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ARGV = sys.argv[1:]
sys.argv = [sys.argv[0]]
import paperbot as PB  # noqa: E402
import reward_sim as RS  # noqa: E402

MIN = float(ARGV[0]) if ARGV else 3.0
D = float(ARGV[1]) if len(ARGV) > 1 else 3.0


def sim(g, v, n, R, oth, reset, cross):
    H = g['hist']
    if len(H) < 120:
        return None
    T = [h[0] for h in H]
    by_min = collections.defaultdict(list)
    for ts, is_yes, p, sz, side in g['trades']:
        b = 1 if (side or '').upper() == 'BUY' else -1
        by_min[ts // 60].append((p if is_yes else 1 - p, b if is_yes else -b))
    rew, fills, mins, pos = 0.0, [], 0, 0.0
    bid = ask = None
    on_b = on_a = False
    last_set = -10 ** 9
    for k in range(1, len(H)):
        t, mid_prev, mid = H[k][0], H[k - 1][1], H[k][1]
        if H[k][0] - H[k - 1][0] > 600:
            bid = ask = None
            last_set = -10 ** 9
            continue
        if t - last_set >= reset * 60:
            last_set = t
            if 0.05 <= mid_prev <= 0.95:
                b_, a_ = PB.quotes(np.array(mid_prev), v, D)
                bid, ask = float(b_), float(a_)
                on_b, on_a = pos < 2 * n and bid >= 0.01, pos > -2 * n and ask <= 0.99
            else:
                bid = ask = None
        mins += 1
        if bid is None:
            continue
        q = float(PB.pair_score(np.array(mid), np.array(bid), np.array(ask), v, n, np.array(on_b), np.array(on_a)))
        if q > 0:
            rew += R / 1440.0 * q / (q + oth)
        tb = on_b and any(b < 0 and p <= bid + 1e-9 for p, b in by_min.get(t // 60, ()))
        ta = on_a and any(b > 0 and p >= ask - 1e-9 for p, b in by_min.get(t // 60, ()))
        xb = on_b and not tb and cross and mid <= bid - 1e-9
        xa = on_a and not ta and cross and mid >= ask + 1e-9
        if tb or xb:
            fills.append((k, 1, bid))
            pos += n
            on_b = False
        if ta or xa:
            fills.append((k, -1, ask))
            pos -= n
            on_a = False
    last = H[-1][1]
    final = 1.0 if last >= 0.97 else 0.0 if last <= 0.03 else last

    def at(k, m_on):
        j = bisect.bisect_left(T, T[k] + m_on * 60)
        return H[j][1] if j < len(H) else final
    pf = [(sg * (at(k, 60) - p) * n, sg * (at(k, 1440) - p) * n, sg * (final - p) * n) for k, sg, p in fills]
    return dict(days=mins / 1440.0, rew=rew, nf=len(fills), c1=sum(a for a, _, _ in pf), c24=sum(b for _, b, _ in pf), cend=sum(c for _, _, c in pf), worst=min([c for _, _, c in pf], default=0.0), ntr=len(g['trades']))


def main():
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    M, cond, end, created, T, B, C = PB.load()
    k = len(T) - 1
    rate, bid, ask, n = PB.expected(k, T, B, C, end, D, 300, 3)
    mid, qb, qa = (B[k, :, j].astype(np.float64) for j in (0, 3, 4))
    oth = PB.others(mid, qb, qa)
    picks = [int(i) for i in np.argsort(-np.nan_to_num(rate, nan=-1)) if np.isfinite(rate[i]) and rate[i] >= MIN]
    say(f'{len(picks)} markets pass the rule at {time.strftime("%d %b %H:%M", time.localtime(int(T[k])))} (expected pot share ${MIN:g}+ a day, pair {D:g}c from the price); pots ${np.nansum(C[k, picks, 0]):,.0f} a day in all, '
        f'expected share ${np.nansum(rate[picks]):,.0f} a day; {np.mean([(T[k] - created[i]) / 3600 < 24 for i in picks]) * 100:.0f}% of them were created in the last 24 h, {np.mean([oth[i] < 0.5 for i in picks]) * 100:.0f}% have nobody else qualifying')
    G, t0 = {}, time.time()
    for j, i in enumerate(picks):
        c = cond[i]
        G[i] = RS.history({'condition_id': c, 'tokens': [{'token_id': M[c]['yes']}, {'token_id': M[c]['no']}]})
        if (j + 1) % 20 == 0:
            print(f'  {j + 1} of {len(picks)} histories, {time.time() - t0:.0f} s', flush=True)
    rng = random.Random(3)
    for cross in (False, True):
        say('')
        say('fills = REAL TRADES at or through our price only' if not cross else 'fills = real trades PLUS every time the price series crossed our price (upper bound on fills)')
        say(f'  {"re-set every":<13}{"mkts":>5} {"mkt-days":>8} | fills a day | a fill is worth after 1 h / 24 h / at the end | worst fill | pot share a day (others as NOW) | NET a day a market [90% by market] | NET a day with HALF / A TENTH of that pot share')
        for reset in (2, 60, 1440):
            rs = []
            for i in picks:
                r = sim(G[i], float(C[k, i, 2]), float(n[i]), float(C[k, i, 0]), float(oth[i]), reset, cross)
                if r and r['days'] > 0.5:
                    rs.append((i, r))
            if not rs:
                continue
            Dd, NF = sum(r['days'] for _, r in rs), sum(r['nf'] for _, r in rs)
            nets = [(r['rew'] + r['cend'], r['days']) for _, r in rs]
            bs = sorted((lambda S: sum(a for a, _ in S) / sum(b for _, b in S))([nets[rng.randrange(len(nets))] for _ in nets]) for _ in range(500))
            rw, ce = sum(r['rew'] for _, r in rs) / Dd, sum(r['cend'] for _, r in rs) / Dd
            say(f'  {({2: "2 min", 60: "1 hour", 1440: "24 hours"}[reset]):<13}{len(rs):>5} {Dd:>8.0f} | {NF / Dd:>6.2f}      | ${sum(r["c1"] for _, r in rs) / max(NF, 1):+5.2f} / ${sum(r["c24"] for _, r in rs) / max(NF, 1):+5.2f} / ${sum(r["cend"] for _, r in rs) / max(NF, 1):+5.2f}'
                f'                   | ${min(r["worst"] for _, r in rs):+6.2f}   | ${rw:6.2f}                        | ${rw + ce:+6.2f} [{bs[25]:+.2f}..{bs[475]:+.2f}]          | ${rw / 2 + ce:+6.2f} / ${rw / 10 + ce:+6.2f}')
            if reset == 2 and not cross:
                keep = rs
    say('')
    say('by market (re-set every 2 min, real-trade fills): pot a day | others now | age | real trades in the week | fills, their value at the end | pot share a day (others as now) | net a day')
    for i, r in sorted(keep, key=lambda z: -(z[1]['rew'] + z[1]['cend']) / z[1]['days']):
        ag = (T[k] - created[i]) / 3600
        say(f"   ${C[k, i, 0]:3.0f} | others {oth[i]:5.1f} | {(f'{ag:.0f} h' if ag < 48 else f'{ag / 24:.0f} d'):>6} | {r['ntr']:5d} tr | {r['nf']:3d} fills ${r['cend']:+7.2f} | ${r['rew'] / r['days']:6.2f} | ${(r['rew'] + r['cend']) / r['days']:+7.2f} | {M[cond[i]]['q'][:60]}")
    say(f'markets without a usable week of price history: {len(picks) - len(keep)} of {len(picks)}')
    open(os.path.join(HERE, 'week_cost.txt'), 'w', encoding='utf-8').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
