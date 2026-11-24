"""What would resting the minimum order in a reward market have earned and cost over the last days? (24 Nov night)

For each chosen market: Polymarket's public 1-minute price history and every trade (Data API), the reward settings, and
the other makers' current score from the book snapshot (snap_first.jsonl; assumed to hold through the period: the weak
point). Policy, re-decided every minute from the PREVIOUS minute's price (a slow bot):
  a bid s cents under the mid and an offer s cents over it (the offer = a bid on the other token), n shares each;
  a side is switched off while our position on it is already 2 orders deep.
A side fills (once a minute) when a real trade in that minute printed at or through its price. Rewards per minute by the
published formula. At the end the position is valued at the last price (the result when the market has resolved).
Prints per market and in total: reward, trading result, net, per day, and the money tied up.
usage: python reward_sim.py N_MARKETS [s cents]
"""
import json, os, sys, time, urllib.request, random, collections
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
def category(q):
    if 'temperature' in q: return 'weather'
    if any(w in q for w in ('vs.', ' vs ', 'qualify', 'Major', ' Cup', 'League', 'NBA', 'NFL', 'Stage', 'Grand Prix', 'hampionship', 'Spread:', 'O/U', 'Map ', 'win the 20')): return 'sports'
    return 'other'
def history(r, days=7):
    cdir = os.path.join(HERE, 'hist'); os.makedirs(cdir, exist_ok=True)
    path = os.path.join(cdir, r['condition_id'] + '.json')
    if os.path.exists(path): return json.load(open(path))
    now = int(time.time())
    yes = r['tokens'][0]['token_id']
    h = get(f'https://clob.polymarket.com/prices-history?market={yes}&startTs={now - days * 86400}&endTs={now}&fidelity=1')
    time.sleep(0.12)
    tr, cur = [], None
    for _ in range(15):
        x = get(f'https://data-api.polymarket.com/v2/trades?condition={r["condition_id"]}&limit=1000' + (f'&cursor={cur}' if cur else ''))
        time.sleep(0.12)
        d = x.get('data') or []
        tr += [(int(z['timestamp']), 1 if z['token_id'] == yes else 0, float(z['price']), float(z['size']), z.get('side')) for z in d]
        pg = x.get('pagination') or {}
        cur = pg.get('next_cursor') or pg.get('cursor')
        if not d or not cur or tr[-1][0] < now - days * 86400: break
    g = {'hist': [(int(p['t']), float(p['p'])) for p in (h.get('history') or [])], 'trades': tr, 'fetched': now}
    json.dump(g, open(path, 'w'))
    return g
def simulate(r, g, snap, s_c, n=None):
    v, n0, R = float(r['rewards_max_spread']), float(r['rewards_min_size']), rate(r)
    n = n or max(n0, 20.0)
    H = g['hist']
    if len(H) < 120 or s_c >= v: return None
    q_oth = min(snap[3], snap[4]) if snap else 0.0
    # trades by minute, on the YES token's terms: (price, +1 taker bought YES / -1 sold)
    byMin = collections.defaultdict(list)
    for ts, is_yes, p, sz, side in g['trades']:
        b = 1 if (side or '').upper() == 'BUY' else -1
        byMin[ts // 60].append((p if is_yes else 1 - p, b if is_yes else -b))
    pos = 0.0; cash = 0.0; rew = 0.0; fills = 0; mins = 0; two = 0
    s = s_c / 100.0
    for k in range(1, len(H)):
        t, mid_prev, mid = H[k][0], H[k - 1][1], H[k][1]
        if H[k][0] - H[k - 1][0] > 600 or not (0.03 < mid_prev < 0.97): continue      # a gap in the history, or a price at the edge: no quotes
        bid = int((mid_prev - s) * 100 + 1e-9) / 100.0
        ask = -int(-(mid_prev + s) * 100 + 1e-9) / 100.0
        on_b, on_a = pos < 2 * n, pos > -2 * n
        mins += 1
        # the reward for this minute, at the distance our quotes really are from the current mid
        qb = ((v - (mid - bid) * 100) / v) ** 2 * n if on_b and 0 <= (mid - bid) * 100 < v else 0.0
        qa = ((v - (ask - mid) * 100) / v) ** 2 * n if on_a and 0 <= (ask - mid) * 100 < v else 0.0
        q = max(min(qb, qa), max(qb, qa) / 3.0) if 0.10 <= mid <= 0.90 else min(qb, qa)
        if q > 0:
            rew += R / 1440.0 * q / (q + q_oth)
            two += 1 if min(qb, qa) > 0 else 0
        hit_b = hit_a = False
        for p, b in byMin.get(t // 60, ()):
            if b < 0 and on_b and p <= bid + 1e-9: hit_b = True
            if b > 0 and on_a and p >= ask - 1e-9: hit_a = True
        if hit_b: pos += n; cash -= n * bid; fills += 1
        if hit_a: pos -= n; cash += n * ask; fills += 1
    last = H[-1][1]
    final = 1.0 if last >= 0.97 else 0.0 if last <= 0.03 else last
    trade_pnl = cash + pos * final
    days = max(mins / 1440.0, 1e-9)
    return {'days': days, 'reward': rew, 'trade': trade_pnl, 'net': rew + trade_pnl, 'fills': fills, 'capital': n * (1 - 2 * s) + 2 * n * 0.5, 'q_oth': q_oth, 'n': n, 'two': two / max(mins, 1), 'last': last, 'R': R}
if __name__ == '__main__':
    N = int(sys.argv[1]); s_list = [float(x) for x in sys.argv[2:]] or [1.0, 2.0, 3.0]
    rows = json.load(open(os.path.join(HERE, 'reward_markets.json')))
    snaps = {}
    for line in open(os.path.join(HERE, 'snap_first.jsonl')):
        d = json.loads(line); snaps[d['c']] = d['sc']
    uni = [r for r in rows if rate(r) >= 5 and 0 < float(r.get('rewards_min_size') or 0) <= 50 and r['condition_id'] in snaps]
    random.seed(7)
    by = collections.defaultdict(list)
    for r in uni: by[category(r.get('question') or '')].append(r)
    pick = []
    for c, v_ in by.items(): pick += random.sample(v_, min(len(v_), N // len(by)))
    out = []
    t0 = time.time()
    for i, r in enumerate(pick):
        g = history(r)
        for s_c in s_list:
            res = simulate(r, g, snaps.get(r['condition_id']), s_c)
            if res: out.append((category(r.get('question') or ''), s_c, r['condition_id'], res, (r.get('question') or '')[:60]))
        if (i + 1) % 50 == 0: print(f'  {i + 1} of {len(pick)} markets, {time.time() - t0:.0f} s', flush=True)
    json.dump([(c, s, cid, res, q) for c, s, cid, res, q in out], open(os.path.join(HERE, 'reward_sim.json'), 'w'))
    print(f'{len(pick)} markets; per market-day, {"/".join(str(s) for s in s_list)} cents from the mid, the minimum size a side')
    for s_c in s_list:
        print(f'--- quotes {s_c:.0f}c from the mid')
        for c in sorted(by) + ['ALL']:
            xs = [res for cc, ss, cid, res, q in out if ss == s_c and (cc == c or c == 'ALL') and res['days'] > 0.2]
            if not xs: continue
            d = sum(x['days'] for x in xs)
            print(f'   {c:<8} {len(xs):>4} markets {d:7.0f} market-days | reward ${sum(x["reward"] for x in xs) / d:6.2f} a day, trading ${sum(x["trade"] for x in xs) / d:+7.2f}, NET ${sum(x["net"] for x in xs) / d:+7.2f} a market-day | '
                  f'fills {sum(x["fills"] for x in xs) / d:5.1f} a day | markets with net > 0: {sum(1 for x in xs if x["net"] > 0) / len(xs) * 100:.0f}% | money tied up ~${sum(x["capital"] for x in xs) / len(xs):.0f} a market')
