"""Each 5%+ arbitrage against what every OTHER source said about the same game at that moment."""
import sqlite3, json, time, datetime as dt, statistics as st
from collections import Counter, defaultdict
from sx import power, fam
c = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
HM = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b %H:%M')
A = [a for a in json.load(open('arbs.json')) if a['why'] != 'restart']
win = {g[0]: g[4] for g in json.load(open('games.json'))}
raw = {r[0]: json.loads(r[1]) for r in c.execute("SELECT id, legs FROM arb_history WHERE id IN (%s)" % ','.join(str(a['id']) for a in A))}
res = []
for a in A:
    m, t = a['m'], a['first']
    rows = c.execute("SELECT ts_ms, book, outcome, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND ts_ms<=? AND ts_ms>=? ORDER BY ts_ms", (m, t, t - 12 * 3600_000)).fetchall(); time.sleep(0.004)
    state = {}
    for ts, b, o, k, p in rows:
        if k == 'close': state.pop((b, o), None)
        elif k == 'suspend': state.pop((b, o), None)
        elif p: state[(b, o)] = (p, ts)
    outs = sorted({o for (_, o) in state})
    per = defaultdict(dict)
    for (b, o), (p, ts) in state.items(): per[b][o] = p
    fair = {}
    for b, d in per.items():
        if set(d) == set(outs) and len(outs) >= 2 and 1.0 <= sum(1 / x for x in d.values()) <= 1.3:
            fair[b] = dict(zip(outs, power([d[o] for o in outs])))
    legs = []
    for l in raw[a['id']]:
        o = l['outcome']                                   # the outcome this leg pays on
        p = 1 / l['odds']
        others = [f[o] for b, f in fair.items() if fam(b) != fam(l['book']) and o in f]
        own = fair.get(l['book'], {}).get(o)
        legs.append(dict(book=l['book'], side=l.get('side'), o=o, p=p, ref=st.median(others) if len(others) >= 2 else None, n=len(others), own=own, age=(t - l['updated_ms']) / 1000 if l.get('updated_ms') else None))
    cls = 'cannot check (under 2 other sources)'
    if all(l['ref'] is not None for l in legs):
        odd = [l for l in legs if l['ref'] - l['p'] >= 0.08]
        if not odd: cls = 'every leg within 8 pt of the other sources'
        else:
            l = max(odd, key=lambda l: l['ref'] - l['p'])
            if l['book'] in ('kalshi', 'polymarket') and abs((1 - l['ref']) - l['p']) <= 0.07 and len(outs) == 2: cls = f"{l['book']}: the two sides are the wrong way round"
            elif l['own'] is not None and abs(l['own'] - l['p']) > 0.08 and l['book'] in ('kalshi', 'polymarket'): cls = f"{l['book']}: leg price does not match its own recorded price"
            elif l['age'] is not None and l['age'] > 1800: cls = f"old price from {l['book']} (over 30 min) far from the others"
            else: cls = f"{l['book'] if l['book'] in ('kalshi','polymarket') else 'a bookmaker'}: price far from every other source"
    w = win.get(m)
    paid = [l['o'] == w for l in legs] if w else None
    res.append(dict(a=a, legs=legs, cls=cls, paid=paid))
print(f"{len(res)} arbitrages of 5%+ (restart records left out), {len({r['a']['m'] for r in res})} games\n")
for k, n in Counter(r['cls'] for r in res).most_common():
    x = [r for r in res if r['cls'] == k]
    print(f"  {n:4d} records in {len({r['a']['m'] for r in x}):3d} games | average peak {st.mean(r['a']['peak'] for r in x)*100:4.0f}% | {k}")
s = [r for r in res if r['paid'] is not None]
print(f"\ngames with a known result: {len(s)} records. Exactly one leg paid (as a real arbitrage must): {sum(sum(r['paid']) == 1 for r in s)} | NO leg paid: {sum(sum(r['paid']) == 0 for r in s)} | both paid: {sum(sum(r['paid']) >= 2 for r in s)}")
print("\nexamples per kind:")
seen = set()
for r in sorted(res, key=lambda r: -r['a']['peak']):
    if (r['cls'], r['a']['m']) in seen or sum(1 for x in seen if x[0] == r['cls']) >= 3: continue
    seen.add((r['cls'], r['a']['m'])); a = r['a']
    print(f"  [{r['cls']}]\n     {HM(a['first'])} {a['sport']} {a['game'][:40]} peak {a['peak']*100:.0f}%: " + ' + '.join(f"{l['book']} pays on {l['o']} at {l['p']*100:.0f}% (others say {l['ref']*100:.0f}%, {l['n']} sources{', own recorded ' + format(l['own']*100, '.0f') + '%' if l['own'] is not None else ''}, {l['age']:.0f} s old)" if l['ref'] is not None else f"{l['book']} {l['o']} at {l['p']*100:.0f}%" for l in r['legs']))
json.dump([dict(id=r['a']['id'], cls=r['cls']) for r in res], open('arb_cls.json', 'w'))
