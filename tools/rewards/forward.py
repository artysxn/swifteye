"""Forward check on the recorded hours: in each candidate market, the minimum order resting on both sides (re-set only
at each recorder pass, so up to 10 minutes stale), the pot share it holds by the published formula against the others
really there at each pass, and how often REAL trades of those hours would have hit it.
usage: python forward.py [distance from the price in cents, default: max spread - 1]"""
import json, os, sys, time, urllib.request, collections, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
def get(url):
    err = None
    for a in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read().decode())
        except Exception as e:
            err = e; time.sleep(1.5 * (a + 1))
    return {'error': str(err)}
rate = lambda r: sum(float(c.get('rate_per_day') or 0) for c in (r.get('rewards_config') or []))
rows = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
S = collections.defaultdict(list)
for line in open(os.path.join(HERE, 'snap_loop.jsonl')):
    d = json.loads(line); S[d['c']].append(d)
t_lo = min(d['t'] for v in S.values() for d in v); t_hi = max(d['t'] for v in S.values() for d in v)
hours = (t_hi - t_lo) / 3600
res = []
t0 = time.time()
for i, (c, v) in enumerate(S.items()):
    r = rows[c]
    if len(v) < 4: continue
    vmax, n = float(r['rewards_max_spread']), max(float(r['rewards_min_size']), 20.0)
    s = (float(sys.argv[1]) if len(sys.argv) > 1 else vmax - 1.0) / 100
    yes = r['tokens'][0]['token_id']
    x = get(f'https://data-api.polymarket.com/v2/trades?condition={c}&limit=1000'); time.sleep(0.1)
    tr = [(int(z['timestamp']), (float(z['price']) if z['token_id'] == yes else 1 - float(z['price'])), (1 if (z['side'] == 'BUY') == (z['token_id'] == yes) else -1), float(z['size'])) for z in (x.get('data') or []) if int(z['timestamp']) >= t_lo]
    v.sort(key=lambda d: d['t'])
    rew = 0.0; fills = []; quoted = 0.0
    for k, d in enumerate(v):
        t_a, t_b = d['t'], (v[k + 1]['t'] if k + 1 < len(v) else d['t'] + 600)
        sc = d['sc']
        if sc is None: continue                           # no two-sided book of real size: no price to quote around
        mid, bb, ba, qb, qa = sc
        if not (0.05 <= mid <= 0.95) or ba - bb > 0.20: continue      # nothing to anchor on: we stay out
        bid, ask = int((mid - s) * 100 + 1e-9) / 100, -int(-(mid + s) * 100 + 1e-9) / 100
        q = ((vmax - s * 100) / vmax) ** 2 * n
        rew += rate(r) * (t_b - t_a) / 86400 * q / (q + min(qb, qa))
        quoted += (t_b - t_a) / 3600
        hb = [t for t in tr if t_a <= t[0] < t_b and t[2] < 0 and t[1] <= bid + 1e-9]
        ha = [t for t in tr if t_a <= t[0] < t_b and t[2] > 0 and t[1] >= ask - 1e-9]
        if hb: fills.append((1, bid, mid))
        if ha: fills.append((-1, ask, mid))
    last_mid = next((d['sc'][0] for d in reversed(v) if d['sc']), None)
    mark = sum(sg * (last_mid - p) * n for sg, p, m in fills) if last_mid is not None else 0.0
    res.append({'c': c, 'pot': rate(r), 'rew': rew, 'fills': len(fills), 'quoted_h': quoted, 'mark': mark, 'trades': len(tr), 'q': (r.get('question') or '')[:50]})
    if (i + 1) % 150 == 0: print(f'  {i + 1} markets, {time.time() - t0:.0f} s', flush=True)
json.dump(res, open(os.path.join(HERE, 'forward.json'), 'w'))
act = [x for x in res if x['quoted_h'] > 0.2]
H = sum(x['quoted_h'] for x in act)
print(f'{len(res)} candidate markets over {hours:.1f} recorded hours; quotable (a real two-sided book within 20c) in {len(act)}, {H:.0f} market-hours')
R, F, MK = sum(x['rew'] for x in act), sum(x['fills'] for x in act), sum(x['mark'] for x in act)
print(f'pot share earned: ${R:.2f} = ${R / H * 24:.2f} a market-day | fills: {F} = {F / H * 24:.2f} a market-day | fills marked at the last recorded price: ${MK:+.2f}')
for cost in (0.5, 1.27, 3.0):
    print(f'   if a fill ends up costing ${cost:.2f} (the bots\' own: $0.50 at the minimum size, $1.27 in these markets): NET ${(R - cost * F) / H * 24:+.2f} a market-day')
z = sorted(act, key=lambda x: -x['fills'])
print('most-hit markets:', [(x['q'][:36], x['fills'], round(x['rew'], 2)) for x in z[:6]])
print(f'markets with no fill at all: {sum(1 for x in act if x["fills"] == 0) / len(act) * 100:.0f}%; real trades in the window in {sum(1 for x in act if x["trades"] > 0) / len(act) * 100:.0f}% of them')
