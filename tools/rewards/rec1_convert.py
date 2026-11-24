"""The first recorder's file (snap_loop.jsonl, 10-minute passes, fixed candidates) in recorder 2's format, for paperbot.py --prefix rec1"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = [sys.argv[0]]
import snap as SN
rows = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
S = [json.loads(l) for l in open(os.path.join(HERE, 'snap_loop.jsonl'))]
t0 = min(d['t'] for d in S)
conds = sorted({d['c'] for d in S})
M = {c: {'idx': i, 'q': (rows[c].get('question') or '')[:90], 'yes': rows[c]['tokens'][0]['token_id'], 'no': rows[c]['tokens'][1]['token_id'], 'created': rows[c].get('created_at'), 'end': rows[c].get('end_date'), 'first_seen': t0} for i, c in enumerate(conds)}
json.dump(M, open(os.path.join(HERE, 'rec1_markets.json'), 'w'))
with open(os.path.join(HERE, 'rec1_list.jsonl'), 'w') as fh:
    fh.write(json.dumps({'t': t0 - 100, 'm': [[M[c]['idx'], SN.rate(rows[c]), float(rows[c]['rewards_min_size']), float(rows[c]['rewards_max_spread']), round(float(rows[c].get('volume_24hr') or 0)), 0] for c in conds]}) + '\n')
passes, cur = [], []
for d in S:
    if cur and d['t'] - cur[-1]['t'] > 200: passes.append(cur); cur = []
    cur.append(d)
if cur: passes.append(cur)
with open(os.path.join(HERE, 'rec1_books.jsonl'), 'w') as fh:
    for p in passes:
        rws = [[M[d['c']]['idx']] + ([round(d['sc'][0], 4), d['sc'][1], d['sc'][2], round(d['sc'][3], 2), round(d['sc'][4], 2)] if d['sc'] else [None] * 5) + [d.get('bb'), d.get('ba'), None] for d in p]
        fh.write(json.dumps({'t': min(d['t'] for d in p), 'key': 0, 'bad': 0, 'rows': rws, 'miss': []}) + '\n')
print(len(passes), 'passes', len(conds), 'markets')
