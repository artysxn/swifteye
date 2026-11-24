"""Forward check, third pass (24 Nov morning): exact order prices and distances, proper competition bounds, several
distances from the price, every fill valued later. Trades of the recorded hours are fetched once (fwd_trades.json).

Each recorder pass (10 min): our two minimum orders are re-set from that pass's book: the bid is the cent price
closest to the mid that is still at least d cents under it (d = 'edge' means the furthest cent that still earns),
the offer the mirror. Score by the published formula at the REAL distance. Others: low = max/3, high = (2 min + max)/3
for a price of 10-90c (see forward2.py). A side fills when a real trade in the next 10 minutes printed at or through it.
The rule (quiet, 3+ days, others (high) <= cap) is applied at the first pass only.
usage: python forward3.py"""
import json, os, sys, time, collections, math, datetime as dt, random
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0]]
import snap as SN
rows = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
S = collections.defaultdict(list)
for line in open(os.path.join(HERE, 'snap_loop.jsonl')):
    d = json.loads(line); S[d['c']].append(d)
t_lo = min(d['t'] for v in S.values() for d in v); t_hi = max(d['t'] for v in S.values() for d in v)
now0 = dt.datetime.fromtimestamp(t_lo, dt.timezone.utc)
def dleft(r):
    e = (r.get('end_date') or '').replace(' ', 'T')
    if e.endswith('+00'): e += ':00'
    try: return (dt.datetime.fromisoformat(e) - now0).total_seconds() / 86400
    except Exception: return 0
tp = os.path.join(HERE, 'fwd_trades.json')
TR = json.load(open(tp)) if os.path.exists(tp) and '--fresh' not in sys.argv else {}
if TR.get('_t_hi', 0) < t_hi - 1800:
    TR = {'_t_hi': t_hi}; t0 = time.time()
    for i, (c, v) in enumerate(S.items()):
        r = rows[c]; yes = r['tokens'][0]['token_id']
        x = SN.get(f'https://data-api.polymarket.com/v2/trades?condition={c}&limit=1000'); time.sleep(0.1)
        TR[c] = [(int(z['timestamp']), (float(z['price']) if z['token_id'] == yes else 1 - float(z['price'])), (1 if (z['side'] == 'BUY') == (z['token_id'] == yes) else -1), float(z['size'])) for z in (x.get('data') or []) if int(z['timestamp']) >= t_lo]
        if (i + 1) % 200 == 0: print(f'  trades of {i + 1} markets, {time.time() - t0:.0f} s', flush=True)
    json.dump(TR, open(tp, 'w'))
def prices(mid, v, d):
    """cent prices: the bid closest to the mid with distance >= d (d None: the furthest that still scores), and the mirror offer"""
    if d is None:
        bid = math.ceil((mid - v / 100) * 100 + 1e-7) / 100          # smallest cent strictly inside the limit
        if (mid - bid) * 100 >= v - 1e-7: bid += 0.01
        ask = math.floor((mid + v / 100) * 100 - 1e-7) / 100
        if (ask - mid) * 100 >= v - 1e-7: ask -= 0.01
    else:
        bid = math.floor((mid - d / 100) * 100 + 1e-7) / 100
        ask = math.ceil((mid + d / 100) * 100 - 1e-7) / 100
    return round(bid, 2), round(ask, 2)
def run(c, d):
    r, v = rows[c], sorted(S[c], key=lambda z: z['t'])
    vmax, n, R = float(r['rewards_max_spread']), max(float(r['rewards_min_size']), 20.0), SN.rate(r)
    tr = TR.get(c, [])
    rew = [0.0, 0.0]; hrs = 0.0; fills = []; first = None; mids = []
    for k, z in enumerate(v):
        t_a, t_b = z['t'], (v[k + 1]['t'] if k + 1 < len(v) else z['t'] + 600)
        if z['sc'] is None: continue
        mid, bb, ba, qb, qa = z['sc']
        mids.append((t_a, mid))
        if not (0.05 <= mid <= 0.95) or ba - bb > 0.20: continue
        lo, hi = min(qb, qa), max(qb, qa)
        o_lo, o_hi = (hi / 3, (2 * lo + hi) / 3) if 0.10 <= mid <= 0.90 else (0.0, lo)
        if first is None: first = o_hi
        bid, ask = prices(mid, vmax, d)
        if bid < 0.01 or ask > 0.99: continue
        db, da = (mid - bid) * 100, (ask - mid) * 100
        sb = ((vmax - db) / vmax) ** 2 * n if 0 <= db < vmax else 0.0
        sa = ((vmax - da) / vmax) ** 2 * n if 0 <= da < vmax else 0.0
        q = max(min(sb, sa), max(sb, sa) / 3) if 0.10 <= mid <= 0.90 else min(sb, sa)
        if q > 0:
            rew[0] += R * (t_b - t_a) / 86400 * q / (q + o_hi); rew[1] += R * (t_b - t_a) / 86400 * q / (q + o_lo)
        hrs += (t_b - t_a) / 3600
        if any(t_a <= t[0] < t_b and t[2] < 0 and t[1] <= bid + 1e-9 for t in tr): fills.append((t_a, 1, bid, bid >= bb + 0.005))
        if any(t_a <= t[0] < t_b and t[2] > 0 and t[1] >= ask - 1e-9 for t in tr): fills.append((t_a, -1, ask, ask <= ba - 0.005))
    if hrs < 0.2 or first is None: return None
    last = mids[-1][1]
    def later(t, h):
        m = [x for x in mids if x[0] >= t + h * 3600]
        return m[0][1] if m else last
    return dict(c=c, pot=R, rew=rew, hrs=hrs, nf=len(fills), v1=sum(sg * (later(t, 1) - p) * n for t, sg, p, _ in fills), vl=sum(sg * (last - p) * n for t, sg, p, _ in fills),
                inside=sum(1 for f in fills if f[3]), first=first, vol=float(r.get('volume_24hr') or 0), dl=dleft(r), n=n, tied=n * (prices(mids[0][1], vmax, d)[0] + 1 - prices(mids[0][1], vmax, d)[1]), q=(r.get('question') or '')[:60])
print(f'{len(S)} recorded markets, {(t_hi - t_lo) / 3600:.1f} hours')
print('QUIET (vol < $300), 3+ days to the end, others (high bound) at the FIRST pass <= cap; per market-day; + = we gained')
print(f'  {"cap":>4} {"distance":<10}{"mkts":>5} {"mkt-h":>6} | pot share a day worst..best | fills a day (as the best price) | a fill after 1 h / at the last price | NET a day at $0.50 / $1.27 / $3 a fill (worst pot share) | break-even $ a fill | money tied up')
keep = {}
for cap in (20, 5):
    for d, lab in ((None, 'edge'), (3.0, '3c'), (2.0, '2c'), (1.0, '1c')):
        rs = [x for x in (run(c, d) for c in S) if x and x['vol'] < 300 and x['dl'] >= 3 and x['first'] <= cap]
        H = sum(x['hrs'] for x in rs); NF = sum(x['nf'] for x in rs)
        a, b, fd = sum(x['rew'][0] for x in rs) / H * 24, sum(x['rew'][1] for x in rs) / H * 24, NF / H * 24
        print(f'  {cap:>4} {lab:<10}{len(rs):>5} {H:>6.0f} | ${a:5.2f}..${b:5.2f}             | {fd:5.2f} ({sum(x["inside"] for x in rs) / H * 24:4.2f})                 | ${sum(x["v1"] for x in rs) / max(NF, 1):+5.2f} / ${sum(x["vl"] for x in rs) / max(NF, 1):+5.2f}                   '
              f'| ${a - 0.5 * fd:+5.2f} / ${a - 1.27 * fd:+5.2f} / ${a - 3 * fd:+5.2f}                         | ${a / max(fd, 1e-9):5.2f}              | ${sum(x["tied"] for x in rs) / len(rs):5.2f}')
        keep[(cap, lab)] = rs
json.dump({f'{a}_{b}': v for (a, b), v in keep.items()}, open(os.path.join(HERE, 'forward3.json'), 'w'))
rs = keep[(20, 'edge')]
old = [x for x in rs if x['c'] in {y['c'] for y in json.load(open(os.path.join(HERE, 'hist_cost_20.json')))['False_60'] } ] if os.path.exists(os.path.join(HERE, 'hist_cost_20.json')) else []
if old:
    try:
        oc = {y[0] for y in json.load(open(os.path.join(HERE, 'hist_cost_20.json')))['False_60']}
        for name, sel in (('markets with a week of history', [x for x in rs if x['c'] in oc]), ('newer markets', [x for x in rs if x['c'] not in oc])):
            H = sum(x['hrs'] for x in sel); NF = sum(x['nf'] for x in sel)
            print(f'  edge, cap 20, {name:<32}{len(sel):>4} mkts | pot share ${sum(x["rew"][0] for x in sel) / H * 24:5.2f} a day | fills {NF / H * 24:4.2f} a day')
    except Exception as e: print('split failed', e)
# by hour of the window (is it one lucky hour?)
