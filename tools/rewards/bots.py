"""How do the best reward bots operate? Reconstructed from their public activity (24 Nov night).
usage: python bots.py WALLET [pages of 500]"""
import json, os, sys, time, urllib.request, collections, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
B = 'https://data-api.polymarket.com/v2'
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
def activity(w, pages):
    path = os.path.join(HERE, f'act_{w[:10]}.json')
    if os.path.exists(path): return json.load(open(path))
    rows, cur = [], None
    for _ in range(pages):
        x = get(f'{B}/activity?user={w}&limit=500' + (f'&cursor={cur}' if cur else '')); time.sleep(0.15)
        d = x.get('data') or []
        rows += [{k: z.get(k) for k in ('type', 'timestamp', 'condition_id', 'token_id', 'side', 'price', 'size', 'usdc_size', 'outcome_index', 'title', 'transaction_hash')} for z in d]
        cur = (x.get('pagination') or {}).get('next_cursor')
        if not d or not cur: break
    json.dump(rows, open(path, 'w'))
    return rows
if __name__ == '__main__':
    w = sys.argv[1]; pages = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    rows = activity(w, pages)
    rew = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
    span = (rows[0]['timestamp'] - rows[-1]['timestamp']) / 86400
    tr = [z for z in rows if z['type'] == 'TRADE']
    print(f'{w[:10]}: {len(rows)} rows over {span:.2f} days; {collections.Counter(z["type"] for z in rows).most_common()}')
    # which of its fills were taker fills
    tak = set()
    cur = None
    for _ in range(pages):
        x = get(f'{B}/trades?user={w}&limit=1000&takerOnly=true' + (f'&cursor={cur}' if cur else '')); time.sleep(0.15)
        d = x.get('data') or []
        for z in d: tak.add((z['transaction_hash'], z['token_id']))
        cur = (x.get('pagination') or {}).get('next_cursor')
        if not d or not cur or d[-1]['timestamp'] < rows[-1]['timestamp']: break
    is_tak = [(z['transaction_hash'], z['token_id']) in tak for z in tr]
    print(f'trades {len(tr)} ({len(tr) / max(span, 1e-9):,.0f} a day): resting fills {len(tr) - sum(is_tak)} ({(1 - sum(is_tak) / max(len(tr), 1)) * 100:.0f}%), taker fills {sum(is_tak)}; in {len({z["condition_id"] for z in tr})} markets')
    sz = [float(z['size'] or 0) for z in tr]
    print(f'shares a fill: median {statistics.median(sz):.0f}, mean {statistics.mean(sz):.0f}; share of fills of exactly the market minimum or less: {sum(1 for z in tr if z["condition_id"] in rew and float(z["size"] or 0) <= float(rew[z["condition_id"]]["rewards_min_size"]) + 1e-6) / max(1, sum(1 for z in tr if z["condition_id"] in rew)) * 100:.0f}%')
    inrew = [z for z in tr if z['condition_id'] in rew and rate(rew[z['condition_id']]) > 0]
    print(f'fills in markets that carry a reward today: {len(inrew) / max(len(tr), 1) * 100:.0f}% (the rest resolved since or pay none)')
    pots = [rate(rew[c]) for c in {z['condition_id'] for z in inrew}]
    if pots: print(f'  pots of those markets: median ${statistics.median(pots):.0f} a day, 10th-90th pct ${sorted(pots)[len(pots) // 10]:.0f}-${sorted(pots)[len(pots) * 9 // 10]:.0f}; min size {collections.Counter(float(rew[c]["rewards_min_size"]) for c in {z["condition_id"] for z in inrew}).most_common(4)}; max spread {collections.Counter(float(rew[c]["rewards_max_spread"]) for c in {z["condition_id"] for z in inrew}).most_common(4)}')
    # its quoted width: in each market, buys of Yes at p and buys of No at q within 10 minutes of each other -> width = 1 - p - q
    by = collections.defaultdict(list)
    for z, t_ in zip(tr, is_tak):
        if t_ or z['side'] != 'BUY': continue
        by[z['condition_id']].append((z['timestamp'], z['outcome_index'], float(z['price'] or 0)))
    widths = []
    for c, v in by.items():
        v.sort()
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                if v[j][0] - v[i][0] > 600: break
                if v[j][1] != v[i][1]:
                    widths.append((1 - v[i][2] - v[j][2]) * 100); break
    if widths:
        ws = sorted(widths)
        print(f'its quoted width (a resting buy of Yes and of No in one market within 10 min; cents between its bid and its offer): median {statistics.median(ws):.1f}c, quartiles {ws[len(ws) // 4]:.1f}-{ws[len(ws) * 3 // 4]:.1f}c, 10th-90th pct {ws[len(ws) // 10]:.1f}-{ws[len(ws) * 9 // 10]:.1f}c ({len(ws)} pairs); pairs that locked a profit (width > 0): {sum(1 for x in ws if x > 0) / len(ws) * 100:.0f}%')
    # cash result per market over the window (buys - / sells + / merges + / redeems +), open leftovers ignored
    cash = collections.defaultdict(float); n_f = collections.Counter()
    for z in rows:
        u = float(z['usdc_size'] or 0)
        if z['type'] == 'TRADE': cash[z['condition_id']] += u if z['side'] == 'SELL' else -u; n_f[z['condition_id']] += 1
        elif z['type'] in ('MERGE', 'REDEEM'): cash[z['condition_id']] += u
        elif z['type'] in ('SPLIT',): cash[z['condition_id']] -= u
    tot = sum(cash.values())
    kind = lambda c: (lambda q: 'weather' if 'temperature' in q else 'sports' if any(k in q for k in (' vs', 'Spread', 'O/U', 'qualify', 'Major', ' Cup')) else 'other')(next((z['title'] or '' for z in rows if z['condition_id'] == c), ''))
    kc = collections.defaultdict(float); kn = collections.Counter()
    for c, v in cash.items(): kc[kind(c)] += v; kn[kind(c)] += 1
    print(f'cash in minus cash out over the window (trades, merges, redemptions; open shares not valued): ${tot:+,.0f} = ${tot / max(span, 1e-9):+,.0f} a day; by kind: ' + ', '.join(f'{k} ${kc[k]:+,.0f} ({kn[k]} mkts)' for k in kc))
    rw = [float(z['usdc_size'] or z['size'] or 0) for z in rows if z['type'] == 'REWARD']
    print(f'reward payouts in the window: {len(rw)} rows, ${sum(rw):,.0f}')
    hours = collections.Counter(time.gmtime(z['timestamp']).tm_hour for z in tr)
    print('fills by UTC hour:', [hours.get(h, 0) for h in range(24)])
