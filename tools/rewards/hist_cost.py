"""What do fills really cost in the markets the rule picks, and how often must the orders be re-set? (24 Nov morning)

Markets: forward2.json rows that pass the rule at the first recorded pass (quiet, 3+ days, true others <= cap).
For each: Polymarket's 1-minute price history of the last 7 days and every real trade. Our two minimum orders sit
v - 1 cents from the price of the minute before the last RE-SET and stay there until the next re-set (every 1 min,
10 min, 1 h, 6 h, 24 h: a bot ... a person). A side fills when a real trade printed at or through it, or the price
itself crossed it; a filled side stays off until the next re-set (at most 2 orders deep a side).
Each fill is valued 1 h later, 24 h later and at the end of the history (97c+ = won, 3c- = lost).
Rewards by the published formula against the others seen in the 5 recorded hours (first pass / median / max).
usage: python hist_cost.py [cap, default 20]"""
import json, os, sys, time, collections, random, bisect
HERE = os.path.dirname(os.path.abspath(__file__))
cap = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
sys.argv = [sys.argv[0]]
import reward_sim as RS
rows = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
F2 = json.load(open(os.path.join(HERE, 'forward2.json')))
sel = [x for x in F2 if x['vol'] < 300 and x['dl'] >= 3 and x['first_hi'] <= cap]
print(f'{len(sel)} markets pass the rule (others <= {cap:g}); fetching a week of history for each', flush=True)
RESETS = (1, 10, 60, 360, 1440)
CROSS = True
def sim(r, g, x, reset):
    v, n, R = float(r['rewards_max_spread']), x['n'], RS.rate(r)
    s = (v - 1.0) / 100
    H = g['hist']
    if len(H) < 120: return None
    T = [h[0] for h in H]
    byMin = collections.defaultdict(list)
    for ts, is_yes, p, sz, side in g['trades']:
        b = 1 if (side or '').upper() == 'BUY' else -1
        byMin[ts // 60].append((p if is_yes else 1 - p, b if is_yes else -b))
    oth = (x['first_hi'], x['oth_med'], x['oth_max'])
    rew = [0.0, 0.0, 0.0]; fills = []; mins = 0; pos = 0.0
    bid = ask = None; on_b = on_a = False; last_set = -10 ** 9
    for k in range(1, len(H)):
        t, mid_prev, mid = H[k][0], H[k - 1][1], H[k][1]
        if H[k][0] - H[k - 1][0] > 600: bid = ask = None; last_set = -10 ** 9; continue
        if t - last_set >= reset * 60:
            last_set = t
            if 0.05 <= mid_prev <= 0.95:
                bid, ask = int((mid_prev - s) * 100 + 1e-9) / 100.0, -int(-(mid_prev + s) * 100 + 1e-9) / 100.0
                on_b, on_a = pos < 2 * n and bid >= 0.01, pos > -2 * n and ask <= 0.99
            else: bid = ask = None
        mins += 1
        if bid is None: continue
        qb = ((v - (mid - bid) * 100) / v) ** 2 * n if on_b and 0 <= (mid - bid) * 100 < v else 0.0
        qa = ((v - (ask - mid) * 100) / v) ** 2 * n if on_a and 0 <= (ask - mid) * 100 < v else 0.0
        q = max(min(qb, qa), max(qb, qa) / 3.0) if 0.10 <= mid <= 0.90 else min(qb, qa)
        if q > 0:
            for j in range(3): rew[j] += R / 1440.0 * q / (q + oth[j])
        tb = on_b and any(b < 0 and p <= bid + 1e-9 for p, b in byMin.get(t // 60, ()))
        ta = on_a and any(b > 0 and p >= ask - 1e-9 for p, b in byMin.get(t // 60, ()))
        xb = on_b and not tb and CROSS and mid <= bid - 1e-9
        xa = on_a and not ta and CROSS and mid >= ask + 1e-9
        if tb or xb: fills.append((k, 1, bid, 't' if tb else 'x')); pos += n; on_b = False
        if ta or xa: fills.append((k, -1, ask, 't' if ta else 'x')); pos -= n; on_a = False
    last = H[-1][1]
    final = 1.0 if last >= 0.97 else 0.0 if last <= 0.03 else last
    def at(k, mins_on):
        j = bisect.bisect_left(T, T[k] + mins_on * 60)
        return H[j][1] if j < len(H) else final
    pf = [(sg * (at(k, 60) - p) * n, sg * (at(k, 1440) - p) * n, sg * (final - p) * n) for k, sg, p, kd in fills]
    nt = sum(1 for f in fills if f[3] == 't')
    return dict(days=mins / 1440.0, rew=rew, n_f=len(fills), n_t=nt, c1h=sum(a for a, _, _ in pf), c24=sum(b for _, b, _ in pf), cend=sum(c for _, _, c in pf), worst=min([c for _, _, c in pf], default=0.0),
                t_end=sum(c for (_, _, c), f in zip(pf, fills) if f[3] == 't'), t_24=sum(b for (_, b, _), f in zip(pf, fills) if f[3] == 't'), ntrades=len(g['trades']), npts=len(H))
G = {}
t0 = time.time()
for i, x in enumerate(sel):
    G[x['c']] = RS.history(rows[x['c']])
    if (i + 1) % 25 == 0: print(f'  {i + 1} of {len(sel)} histories, {time.time() - t0:.0f} s', flush=True)
out = {}
for CROSS in (False, True):
    print(f'\n{"fills = REAL TRADES at or through our price only" if not CROSS else "fills = real trades PLUS every time the price series crossed our price (upper bound)"}; per market-day, + = we gained')
    print(f'  {"re-set every":<10}{"mkts":>5} {"mkt-days":>8} | fills a day (real-trade) | a fill is worth after 24 h / at the end | worst fill | pot share a day, others first/median/max | NET a day: median others [90% by market] / max others | markets up')
    for reset in RESETS:
        rs = []
        for x in sel:
            r = sim(rows[x['c']], G[x['c']], x, reset)
            if r and r['days'] > 0.5: rs.append((x, r))
        D = sum(r['days'] for _, r in rs); NF = sum(r['n_f'] for _, r in rs)
        rw = [sum(r['rew'][j] for _, r in rs) / D for j in range(3)]
        nets = [(r['rew'][1] + r['cend'], r['days']) for _, r in rs]
        rng = random.Random(3)
        bs = sorted((lambda S: sum(a for a, _ in S) / sum(b for _, b in S))([nets[rng.randrange(len(nets))] for _ in nets]) for _ in range(500))
        lab = {1: '1 min', 10: '10 min', 60: '1 hour', 360: '6 hours', 1440: '24 hours'}[reset]
        print(f'  {lab:<10}{len(rs):>5} {D:>8.0f} | {NF / D:>6.2f} ({sum(r["n_t"] for _, r in rs) / D:5.2f})          | ${sum(r["c24"] for _, r in rs) / max(NF, 1):+5.2f} / ${sum(r["cend"] for _, r in rs) / max(NF, 1):+5.2f}'
              f'                    | ${min(r["worst"] for _, r in rs):+6.2f}   | ${rw[0]:5.2f} / ${rw[1]:5.2f} / ${rw[2]:5.2f}              | ${sum(a for a, _ in nets) / D:+5.2f} [{bs[25]:+.2f}..{bs[475]:+.2f}] / ${(sum(r["rew"][2] + r["cend"] for _, r in rs)) / D:+5.2f} | {sum(1 for a, _ in nets if a > 0) / len(nets) * 100:.0f}%')
        out[(CROSS, reset)] = [(x['c'], x['q'], r) for x, r in rs]
print('\nby market, re-set every hour, real-trade fills only: pot | days of history | real trades in the week | fills | fills value at the end | pot share (median others) | net a day')
for c, q, r in sorted(out[(False, 60)], key=lambda z: -(z[2]['rew'][1] + z[2]['cend']) / z[2]['days']):
    x = next(z for z in sel if z['c'] == c)
    print(f"   ${x['pot']:3.0f} | {r['days']:4.1f} d | {r['ntrades']:5d} tr | {r['n_f']:3d} fills ${r['cend']:+7.2f} | share ${r['rew'][1]:6.2f} | net ${(r['rew'][1] + r['cend']) / r['days']:+6.2f} a day | {q[:58]}")
print(f"markets without a usable week of history: {len(sel) - len(out[(False, 60)])} of {len(sel)}")
json.dump({f'{a}_{b}': v for (a, b), v in out.items()}, open(os.path.join(HERE, f'hist_cost_{cap:g}.json'), 'w'))
