"""Small reward farmers: what do the ones in profit do differently from the ones at a loss? (public activity)"""
import json, os, sys, time, urllib.request, collections, statistics
sys.argv = [sys.argv[0]]
import bots as BT, markouts as MO
HERE = os.path.dirname(os.path.abspath(__file__))
out = json.load(open(os.path.join(HERE, 'farmers_result.json')))
rew = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
q = [o for o in out if o['reward'] >= 150 and o['trades'] < 1000 and abs(o['trading']) < 2000]
q.sort(key=lambda o: o['total'])
grp = {'at a loss': q[:10], 'in profit': q[-10:]}
res = {}
for name, ws in grp.items():
    S = collections.defaultdict(list)
    for o in ws:
        w = o['w']
        rows = BT.activity(w, 6)
        tr = [z for z in rows if z['type'] == 'TRADE']
        if len(tr) < 30: continue
        tak = MO.taker_set(w, rows[-1]['timestamp'])
        days = max((rows[0]['timestamp'] - rows[-1]['timestamp']) / 86400, 0.5)
        rest = [z for z in tr if (z['transaction_hash'], z['token_id']) not in tak]
        S['resting share of fills'].append(len(rest) / len(tr))
        S['fills a day'].append(len(tr) / days)
        S['markets traded a day'].append(len({z['condition_id'] for z in tr}) / days)
        S['median shares a fill'].append(statistics.median(float(z['size'] or 0) for z in tr))
        S['median price of token'].append(statistics.median(float(z['price'] or 0) for z in tr))
        S['share of fills in weather'].append(sum(1 for z in tr if 'temperature' in (z['title'] or '')) / len(tr))
        S['share of fills in sports'].append(sum(1 for z in tr if any(k in (z['title'] or '') for k in (' vs', 'Spread', 'O/U', 'qualify', 'Major', ' Cup'))) / len(tr))
        S['share of fills under 10c or over 90c'].append(sum(1 for z in tr if float(z['price'] or 0) < 0.10 or float(z['price'] or 0) > 0.90) / len(tr))
        S['merges per 100 fills'].append(100 * sum(1 for z in rows if z['type'] == 'MERGE') / len(tr))
        S['redeems per 100 fills'].append(100 * sum(1 for z in rows if z['type'] == 'REDEEM') / len(tr))
        pots = [BT.rate(rew[c]) for c in {z['condition_id'] for z in tr} if c in rew]
        if pots: S['median pot of its markets ($/day)'].append(statistics.median(pots))
        S['rewards per fill ($)'].append(o['reward'] / max(o['trades'], 1))
        S['trading per fill ($)'].append(o['trading'] / max(o['trades'], 1))
    res[name] = S
keys = list(res['in profit'].keys())
print(f'{"(median over ~10 wallets each)":<42}{"in profit":>12}{"at a loss":>12}')
for k in keys:
    a, b = res['in profit'].get(k, []), res['at a loss'].get(k, [])
    if a and b: print(f'{k:<42}{statistics.median(a):>12.2f}{statistics.median(b):>12.2f}')
for name, ws in grp.items():
    print(name + ':', ', '.join(f'{o["w"][:8]} {o["total"]:+.0f}' for o in ws))
