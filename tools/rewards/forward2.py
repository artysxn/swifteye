"""Forward check, competition done properly (24 Nov morning).

forward.py took the other makers' score as min(Q bids, Q asks). That is wrong for a lopsided book: with the price in
10-90c a one-sided maker still scores a third of his side, so the others hold between max/3 (all one-sided) and
(2 min + max)/3 (paired up as far as the book allows). Outside 10-90c only two-sided makers score: between 0 and min.
This recomputes the pot share from the recorded books under the old assumption and both bounds; fills are those of
forward.json (they do not depend on the competition). The rule is applied at the FIRST pass only (no hindsight)."""
import json, os, sys, collections, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
rate = lambda r: sum(float(c.get('rate_per_day') or 0) for c in (r.get('rewards_config') or []))
rows = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
F = {x['c']: x for x in json.load(open(os.path.join(HERE, 'forward.json')))}
S = collections.defaultdict(list)
for line in open(os.path.join(HERE, 'snap_loop.jsonl')):
    d = json.loads(line); S[d['c']].append(d)
t_lo = min(d['t'] for v in S.values() for d in v)
now = dt.datetime.fromtimestamp(t_lo, dt.timezone.utc)
def dleft(r):
    e = (r.get('end_date') or '').replace(' ', 'T')
    if e.endswith('+00'): e += ':00'
    try: return (dt.datetime.fromisoformat(e) - now).total_seconds() / 86400
    except Exception: return None
def others(mid, qb, qa):
    lo, hi = min(qb, qa), max(qb, qa)
    if 0.10 <= mid <= 0.90: return lo, hi / 3, (2 * lo + hi) / 3          # old, low bound, high bound
    return lo, 0.0, lo
res = []
for c, v in S.items():
    r = rows[c]; f = F.get(c)
    if f is None or len(v) < 4: continue
    v.sort(key=lambda d: d['t'])
    vmax, n = float(r['rewards_max_spread']), max(float(r['rewards_min_size']), 20.0)
    q = (1.0 / vmax) ** 2 * n
    rew = [0.0, 0.0, 0.0]; hrs = 0.0; first = None; oth_path = []
    for k, d in enumerate(v):
        t_a, t_b = d['t'], (v[k + 1]['t'] if k + 1 < len(v) else d['t'] + 600)
        sc = d['sc']
        if sc is None: continue
        mid, bb, ba, qb, qa = sc
        if not (0.05 <= mid <= 0.95) or ba - bb > 0.20: continue
        o = others(mid, qb, qa)
        if first is None: first = (mid, o, k)
        oth_path.append(o[2])
        for j in range(3): rew[j] += rate(r) * (t_b - t_a) / 86400 * q / (q + o[j])
        hrs += (t_b - t_a) / 3600
    if hrs < 0.2 or first is None: continue
    res.append(dict(c=c, q=(r.get('question') or '')[:60], pot=rate(r), rew=rew, hrs=hrs, fills=f['fills'], mark=f['mark'], vol=float(r.get('volume_24hr') or 0), dl=dleft(r) or 0,
                    first_old=first[1][0], first_hi=first[1][2], first_lo=first[1][1], first_k=first[2], oth_max=max(oth_path), oth_med=sorted(oth_path)[len(oth_path) // 2], vmax=vmax, n=n, mid=first[0]))
json.dump(res, open(os.path.join(HERE, 'forward2.json'), 'w'))
def line(name, sel):
    if not sel: print(f'  {name:<58} none'); return
    H = sum(x['hrs'] for x in sel); Fl = sum(x['fills'] for x in sel)
    R = [sum(x['rew'][j] for x in sel) / H * 24 for j in range(3)]
    fd = Fl / H * 24
    print(f'  {name:<58}{len(sel):>4} mkts {H:>5.0f} mkt-h | pot share a market-day: old ${R[0]:5.2f}, true range ${R[2]:5.2f}..${R[1]:5.2f} | fills {fd:4.1f} a day | NET (worst competition) at $0.50 a fill ${R[2] - 0.5 * fd:+6.2f}, at $1.27 ${R[2] - 1.27 * fd:+6.2f}, at $3 ${R[2] - 3 * fd:+6.2f}')
print(f'{len(res)} quotable candidate markets, {(max(d["t"] for v in S.values() for d in v) - t_lo) / 3600:.1f} recorded hours; competition bounds: low = max/3, high = (2 min + max)/3 (price 10-90c)')
quiet = [x for x in res if x['vol'] < 300 and x['dl'] >= 3]
line('all quotable candidates', res)
line('QUIET (vol < $300) and 3+ days to the end', quiet)
line('  + old rule at the first pass: min(Qb,Qa) <= 20', [x for x in quiet if x['first_old'] <= 20])
for cap in (20, 5, 2, 1):
    line(f'  + TRUE rule at the first pass: others (high) <= {cap}', [x for x in quiet if x['first_hi'] <= cap])
line('  + old rule passed but true others > 20 (the mistake)', [x for x in quiet if x['first_old'] <= 20 and x['first_hi'] > 20])
line('busy (vol $300+), true others <= 20', [x for x in res if x['vol'] >= 300 and x['first_hi'] <= 20])
line('ends within a day, true others <= 20', [x for x in res if x['dl'] < 1 and x['first_hi'] <= 20])
sel = sorted([x for x in quiet if x['first_hi'] <= 20], key=lambda x: -x['rew'][2])
print('\nquiet + 3 days + true others <= 20 at the first pass, by market (pot, others at first pass / median / max later, pot share kept (worst), fills, hours):')
for x in sel[:40]:
    print(f"   ${x['pot']:4.0f} | others {x['first_hi']:5.1f} / {x['oth_med']:5.1f} / {x['oth_max']:6.1f} | kept ${x['rew'][2]:5.2f} of ${x['pot'] * x['hrs'] / 24:5.2f} | fills {x['fills']} | {x['hrs']:.1f} h | mid {x['mid'] * 100:4.1f}c v {x['vmax']} | {x['q']}")
