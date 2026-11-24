"""What does a resting minimum-size order really cost? The reward bots' own fills, marked to the price afterwards.
For each resting-side fill of a bot (public activity; resting = not in its taker listing): the token's price one
minute before (how far from the price the bot was quoting) and 5 min / 1 h / 6 h later and at the end of the
recorded history (CLOB prices-history, 1-minute). Cents a share against the fill price; + = the bot is ahead.
usage: python markouts.py N_MARKETS WALLET [WALLET ...]"""
import json, os, sys, time, urllib.request, collections, statistics, random, bisect
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
def taker_set(w, since):
    path = os.path.join(HERE, f'tak_{w[:10]}.json')
    if os.path.exists(path): return set(tuple(x) for x in json.load(open(path)))
    s, cur = set(), None
    for _ in range(60):
        x = get(f'{B}/trades?user={w}&limit=1000&takerOnly=true' + (f'&cursor={cur}' if cur else '')); time.sleep(0.12)
        d = x.get('data') or []
        for z in d: s.add((z['transaction_hash'], z['token_id']))
        cur = (x.get('pagination') or {}).get('next_cursor')
        if not d or not cur or d[-1]['timestamp'] < since: break
    if s: json.dump(sorted(s), open(path, 'w'))
    return s
def hist(token):
    cdir = os.path.join(HERE, 'tokhist'); os.makedirs(cdir, exist_ok=True)
    path = os.path.join(cdir, token[-24:] + '.json')
    if os.path.exists(path): return json.load(open(path))
    h = get(f'https://clob.polymarket.com/prices-history?market={token}&interval=max&fidelity=1'); time.sleep(0.1)
    out = [(int(p['t']), float(p['p'])) for p in (h.get('history') or [])]
    json.dump(out, open(path, 'w'))
    return out
def kind(q):
    return 'weather' if 'temperature' in q else 'sports' if any(k in q for k in (' vs', 'Spread', 'O/U', 'qualify', 'Major', ' Cup', 'win the 20')) else 'crypto' if any(k in q for k in ('Bitcoin', 'Ethereum', 'Solana', 'XRP', 'Up or Down')) else 'other'
if __name__ == '__main__':
    N = int(sys.argv[1]); wallets = sys.argv[2:]
    rew = {r['condition_id']: r for r in json.load(open(os.path.join(HERE, 'reward_markets.json')))}
    F = []
    random.seed(4)
    for w in wallets:
        rows = json.load(open(os.path.join(HERE, f'act_{w[:10]}.json')))
        tak = taker_set(w, rows[-1]['timestamp'])
        if not tak:
            print(w[:10], 'taker listing empty: skipped', flush=True); continue
        rest = [z for z in rows if z['type'] == 'TRADE' and (z['transaction_hash'], z['token_id']) not in tak]
        toks = sorted({z['token_id'] for z in rest})
        pick = set(random.sample(toks, min(N, len(toks))))
        days = (rows[0]['timestamp'] - rows[-1]['timestamp']) / 86400
        print(f'{w[:10]}: {len(rest)} resting fills in {len({z["condition_id"] for z in rest})} markets over {days:.1f} days; sampling {len(pick)} tokens', flush=True)
        for z in rest:
            if z['token_id'] not in pick: continue
            h = hist(z['token_id'])
            if len(h) < 30: continue
            ts = [x[0] for x in h]
            t, p, sgn = z['timestamp'], float(z['price'] or 0), (1 if z['side'] == 'BUY' else -1)
            def at(tt):
                i = bisect.bisect_right(ts, tt) - 1
                return h[i][1] if i >= 0 else None
            before = at(t - 60)
            if before is None or t > ts[-1] + 600: continue
            marks = [at(t + s) if t + s <= ts[-1] else None for s in (300, 3600, 21600)]
            last = h[-1][1]
            end = 1.0 if last >= 0.97 else 0.0 if last <= 0.03 else last
            F.append({'w': w[:10], 'kind': kind(z['title'] or ''), 'p': p, 'sgn': sgn, 'size': float(z['size'] or 0), 'dist': sgn * (before - p) * 100, 'm5': None if marks[0] is None else sgn * (marks[0] - p) * 100,
                      'm60': None if marks[1] is None else sgn * (marks[1] - p) * 100, 'm360': None if marks[2] is None else sgn * (marks[2] - p) * 100, 'end': sgn * (end - p) * 100, 'left_h': (ts[-1] - t) / 3600, 'pot': None if z['condition_id'] not in rew else sum(float(c.get('rate_per_day') or 0) for c in rew[z['condition_id']]['rewards_config']), 'c': z['condition_id'], 't': t})
    json.dump(F, open(os.path.join(HERE, 'markouts.json'), 'w'))
    mean = lambda v: sum(v) / len(v) if v else float('nan')
    def row(name, fs):
        if len(fs) < 25: return
        g = lambda k: [f[k] for f in fs if f[k] is not None]
        print(f'  {name:<40}{len(fs):>6} fills | quoted {statistics.median(g("dist")):+5.1f}c from the price (median) | kept after 5 min {mean(g("m5")):+6.2f}c  1 h {mean(g("m60")):+6.2f}c  6 h {mean(g("m360")):+6.2f}c  at the end {mean(g("end")):+6.2f}c | per fill at the end ${mean([f["end"] * f["size"] / 100 for f in fs]):+.2f}')
    print(f'\n{len(F)} resting fills marked; cents a share against the fill price (+ = the resting order is ahead)')
    row('all', F)
    for w in sorted({f['w'] for f in F}): row(w, [f for f in F if f['w'] == w])
    for k in ('weather', 'sports', 'crypto', 'other'): row(k, [f for f in F if f['kind'] == k])
    print('by how far from the price the order was resting:')
    for lo, hi in ((-99, 0.5), (0.5, 1.5), (1.5, 2.5), (2.5, 4.5), (4.5, 99)): row(f'  {lo if lo > -99 else "under 0.5"}{"-" + str(hi) if lo > -99 and hi < 99 else "+" if hi == 99 else ""}c', [f for f in F if lo <= f['dist'] < hi])
    print('by the price of the token bought or sold:')
    for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 0.9), (0.9, 1.01)): row(f'  {lo * 100:.0f}-{hi * 100:.0f}c', [f for f in F if lo <= f['p'] < hi])
    print('by hours of price history left after the fill (close to the end = close to the result):')
    for lo, hi in ((0, 2), (2, 12), (12, 48), (48, 1e9)): row(f'  {lo}-{hi if hi < 1e8 else "+"} h', [f for f in F if lo <= f['left_h'] < hi])
    print('by size of the fill:')
    row('  the minimum or less (<= 20)', [f for f in F if f['size'] <= 20.01]); row('  over 20 shares', [f for f in F if f['size'] > 20.01])
