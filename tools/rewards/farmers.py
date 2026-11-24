"""Do the wallets that REST orders in Polymarket's small reward markets make money? Their public record (24 Nov night).

No simulation: Polymarket publishes, per wallet, a daily series of trading result, reward income and rebates
(Data API /v2/user-pnl). To avoid looking only at today's survivors, the wallets are picked by what they did in the
FIRST week of the window (25 Oct - 1 Nov 2026): at least 5 resting-side fills in long-lived reward markets with a small
minimum size. Then what each made from 1 Nov to 23 Nov is read off, whether or not it kept going.
  resting-side fill = a trade row that is in the full listing (takerOnly=false) but not in the taker listing.
usage: python farmers.py [N markets]
"""
import json, os, sys, time, urllib.request, collections, random, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
B = 'https://data-api.polymarket.com/v2'
T0, T1 = 1792886400, 1793491200          # 25 Oct 2026 00:00 UTC, 1 Nov 2026 00:00 UTC
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
def trades(cid, taker_only, max_pages=25):
    out, cur = [], None
    for _ in range(max_pages):
        x = get(f'{B}/trades?condition={cid}&limit=1000&takerOnly={"true" if taker_only else "false"}' + (f'&cursor={cur}' if cur else ''))
        time.sleep(0.1)
        d = x.get('data') or []
        out += [(z['transaction_hash'], z['proxy_wallet'], z['token_id'], z['price'], z['size'], z['side'], int(z['timestamp'])) for z in d]
        cur = (x.get('pagination') or {}).get('next_cursor')
        if not d or not cur or out[-1][6] < T0: break
    return out
if __name__ == '__main__':
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    rows = json.load(open(os.path.join(HERE, 'reward_markets.json')))
    old = [r for r in rows if rate(r) > 0 and 0 < float(r.get('rewards_min_size') or 0) <= 50 and (r.get('created_at') or '9') < '2026-10-20' and float(r.get('volume_24hr') or 0) > 200]
    random.seed(3)
    pick = random.sample(old, min(N, len(old)))
    print(len(old), 'long-lived small reward markets with trading; sampling', len(pick), flush=True)
    cache = os.path.join(HERE, 'farmers_fills.json')
    if os.path.exists(cache):
        fills = collections.Counter(json.load(open(cache)))
    else:
        fills = collections.Counter()
        t0 = time.time()
        for i, r in enumerate(pick):
            allr, tak = trades(r['condition_id'], False), trades(r['condition_id'], True)
            ts = collections.Counter(x[:6] for x in tak)
            for x in allr:
                if ts[x[:6]] > 0: ts[x[:6]] -= 1; continue          # the taker's own row
                if T0 <= x[6] < T1: fills[x[1]] += 1
            if (i + 1) % 25 == 0: print(f'  {i + 1} markets, {len(fills)} wallets with a resting fill in the first week, {time.time() - t0:.0f} s', flush=True)
        json.dump(dict(fills), open(cache, 'w'))
    ws = [w for w, n in fills.items() if n >= 5]
    print(len(fills), 'wallets with a resting-side fill 25 Oct - 1 Nov;', len(ws), 'with 5 or more', flush=True)
    out = []
    for i, w in enumerate(ws):
        pn = (get(f'{B}/user-pnl?user={w}&interval=1m&fidelity=1d').get('data') or {}).get('points') or []
        time.sleep(0.1)
        val = (get(f'{B}/value?user={w}').get('data') or {}).get('value') or 0
        time.sleep(0.1)
        pts = [p for p in pn if p['timestamp'] >= T1 - 86400]
        if len(pts) < 5: continue
        g = lambda p, k: float(p.get(k) or 0)
        a, b = pts[0], pts[-1]
        days = (b['timestamp'] - a['timestamp']) / 86400
        eco = [g(p, 'economic_pnl') for p in pts]
        inc = [eco[j + 1] - eco[j] for j in range(len(eco) - 1)]
        out.append({'w': w, 'fills_wk1': fills[w], 'value': val, 'days': days, 'total': eco[-1] - eco[0], 'reward': g(b, 'reward_income') - g(a, 'reward_income'), 'rebate': g(b, 'maker_rebate') - g(a, 'maker_rebate'),
                    'trading': (g(b, 'realized_market_pnl') + g(b, 'unrealized_pnl')) - (g(a, 'realized_market_pnl') + g(a, 'unrealized_pnl')), 'vol': g(b, 'volume_usdc') - g(a, 'volume_usdc'), 'pos_days': sum(1 for v in inc if v > 0),
                    'act_days': sum(1 for v in inc if abs(v) > 1e-9), 'worst': min(inc), 'trades': g(b, 'trade_count') - g(a, 'trade_count')})
        if (i + 1) % 50 == 0: print(f'  {i + 1} of {len(ws)} wallets', flush=True)
    json.dump(out, open(os.path.join(HERE, 'farmers_result.json'), 'w'))
    med = lambda v: statistics.median(v) if v else float('nan')
    print(f'\n{len(out)} wallets that rested orders in late October: what they made 1-23 Nov (about 23 days)')
    for name, f in (('all', lambda o: True), ('still active in November (traded)', lambda o: o['trades'] > 0), ('volume under $20k', lambda o: 0 < o['vol'] < 20_000), ('volume $20k-200k', lambda o: 20_000 <= o['vol'] < 200_000),
                    ('volume $200k-2M', lambda o: 200_000 <= o['vol'] < 2_000_000), ('volume over $2M', lambda o: o['vol'] >= 2_000_000), ('earned rewards ($20+)', lambda o: o['reward'] >= 20)):
        g_ = [o for o in out if f(o)]
        if not g_: continue
        print(f'  {name:<36} {len(g_):>4} wallets | in profit {sum(1 for o in g_ if o["total"] > 0) / len(g_) * 100:3.0f}% | total: median ${med([o["total"] for o in g_]):+9,.0f}, all together ${sum(o["total"] for o in g_):+12,.0f} | '
              f'of which rewards ${sum(o["reward"] for o in g_):>10,.0f}, rebates ${sum(o["rebate"] for o in g_):>10,.0f}, trading ${sum(o["trading"] for o in g_):+12,.0f} | median share of days in profit {med([o["pos_days"] / max(o["act_days"], 1) for o in g_ if o["act_days"] >= 5]) * 100:.0f}%')
    print('  the 12 with the most volume under $200k:')
    for o in sorted([o for o in out if 0 < o['vol'] < 200_000], key=lambda o: -o['vol'])[:12]:
        print(f'    {o["w"][:10]} vol ${o["vol"]:>9,.0f} value ${o["value"]:>8,.0f}: total ${o["total"]:+9,.0f} (rewards ${o["reward"]:,.0f}, rebates ${o["rebate"]:,.0f}, trading ${o["trading"]:+,.0f}), days in profit {o["pos_days"]}/{o["act_days"]}, worst day ${o["worst"]:+,.0f}')
