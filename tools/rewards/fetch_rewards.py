import json, time, urllib.request, urllib.parse, collections, statistics, sys
def get(url):
    err = None
    for a in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as r: return json.loads(r.read().decode())
        except Exception as e:
            err = e; time.sleep(2 * (a + 1))
    return {'error': str(err)}
rows, cur = [], None
for page in range(120):
    q = {'order_by': 'rate_per_day', 'position': 'DESC', 'page_size': '100'}
    if cur: q['next_cursor'] = cur
    d = get('https://clob.polymarket.com/rewards/markets/multi?' + urllib.parse.urlencode(q))
    if 'error' in d: print('error', d['error']); break
    data = d.get('data') or []
    rows += data
    cur = d.get('next_cursor')
    if not data or not cur or cur == 'LTE=': break
    time.sleep(0.2)
json.dump(rows, open('reward_markets.json', 'w'))
print(len(rows), 'reward markets,', page + 1, 'pages')
rate = lambda r: sum(float(c.get('rate_per_day') or 0) for c in (r.get('rewards_config') or []))
act = [r for r in rows if rate(r) > 0]
print('with a reward rate > 0:', len(act), ' total $/day:', round(sum(rate(r) for r in act)))
b = collections.Counter()
for r in act:
    x = rate(r); b['<5' if x < 5 else '5-20' if x < 20 else '20-50' if x < 50 else '50-200' if x < 200 else '200+'] += 1
print('by rate/day:', dict(b))
comp = [float(r.get('market_competitiveness') or 0) for r in act]
if comp: print('competitiveness deciles:', [round(x, 2) for x in statistics.quantiles(comp, n=10)])
low = [r for r in act if float(r.get('market_competitiveness') or 0) < 5 and rate(r) >= 10]
print('rate >= $10/day and competitiveness < 5:', len(low), ' $/day in them:', round(sum(rate(r) for r in low)))
for r in sorted(low, key=lambda r: -rate(r))[:40]:
    print(f"  ${rate(r):6.1f}/day  min {float(r.get('rewards_min_size') or 0):>5.0f} sh  max spread {r.get('rewards_max_spread')}c  compet {float(r.get('market_competitiveness') or 0):7.2f}  spread {float(r.get('spread') or 0)*100:5.1f}c  vol24h ${float(r.get('volume_24hr') or 0):>8.0f}  ends {(r.get('end_date') or '')[:10]}  {(r.get('question') or '')[:62]}")
