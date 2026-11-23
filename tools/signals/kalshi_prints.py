"""Did real trades print on Kalshi at the prices our test assumed it could buy? (python -I tools/signals/kalshi_prints.py --signals DIR)

kalshi2.py buys the Kalshi contract at the order-book ask our recording shows 1 s after deciding. Open question
(AGENTS.md): a 10c gap to Polymarket that rests for minutes should be taken by arbitrage bots, so can those asks really
be hit? Kalshi's public trade history answers part of it: GET /markets/trades (no key) lists every print with its
time, price and which side took. For each test decision (decide 1 s after the Polymarket jump, order-book ask >= 5c
below Polymarket, Kalshi spread <= 2c, feed flowing) this fetches the contract's prints from 60 s before to 300 s
after our buy time and counts the prints where a TAKER BOUGHT OUR SIDE AT OUR PRICE OR CHEAPER (Yes buys: taker_side
yes, yes price <= ours; No buys: taker_side no, no price <= ours).
  prints at our price  -> the price was real and tradable then (and someone else was taking it);
  no print, price gone -> it was cancelled or repriced without trading: we cannot know if an order would have filled.
Read-only public data; responses cached in <signals>/kalshi_prints_cache.json."""
import argparse
import datetime as dt
import difflib
import json
import os
import pickle
import re
import sys
import time
import urllib.request

import numpy as np

API = 'https://api.elections.kalshi.com/trade-api/v2'


def get(path, cache):
    if path in cache:
        return cache[path]
    for attempt in range(4):
        try:
            req = urllib.request.Request(API + path, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
            cache[path] = d
            time.sleep(0.08)
            return d
        except Exception as ex:   # rate limit or a hiccup: wait and retry
            time.sleep(1.5 * (attempt + 1))
            err = ex
    cache[path] = {'error': str(err)}
    return cache[path]


def norm(s):
    return ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    cpath = os.path.join(a.signals, 'kalshi_prints_cache.json')
    cache = json.load(open(cpath, encoding='utf-8')) if os.path.exists(cpath) else {}
    out = ['Kalshi\'s own trade history around our test buys (decide 1 s after the Polymarket jump, order-book ask >= 5c below Polymarket, spread <= 2c, feed flowing)', '']
    for tag in ('mac', 'pc'):
        p = os.path.join(a.signals, f'kalshi2_{tag}.pkl')
        if not os.path.exists(p):
            continue
        R = [r for r in pickle.load(open(p, 'rb')) if r['lag'] == 1 and r['room'] >= 5 and r['kspread'] <= 2.01 and r['flow']]
        if not R or 'event' not in R[0]:
            out.append(f'[{tag}] records have no contract ids yet (re-run kalshi2.py)')
            continue
        rows = []
        for r in R:
            ev = get(f"/events/{r['event']}?with_nested_markets=true", cache)
            ms = (ev.get('event') or {}).get('markets') or ev.get('markets') or []
            ko = r['o'] if not r['sw'] else ('away' if r['o'] == 'home' else 'home')        # Kalshi's own side of this contract
            want = norm(r['eh'] if ko == 'home' else r['ea'])
            best = None
            for m in ms:
                sub = norm(m.get('yes_sub_title'))
                if sub in ('tie', 'draw'):
                    continue
                sc = difflib.SequenceMatcher(None, sub, want).ratio()
                if best is None or sc > best[0]:
                    best = (sc, m['ticker'])
            if best is None or best[0] < 0.4:
                continue
            t0, t1 = r['buy_t'] // 1000 - 60, r['buy_t'] // 1000 + 301
            tr, cursor = [], ''
            for _ in range(5):
                d = get(f'/markets/trades?ticker={best[1]}&min_ts={t0}&max_ts={t1}&limit=1000' + (f'&cursor={cursor}' if cursor else ''), cache)
                tr += d.get('trades') or []
                cursor = d.get('cursor') or ''
                if not cursor or not d.get('trades'):
                    break
            side = 'yes' if r['d'] > 0 else 'no'
            pk = 'yes_price_dollars' if r['d'] > 0 else 'no_price_dollars'
            at, any_after, any_before = [], 0, 0
            for x in tr:
                ts = dt.datetime.fromisoformat(x['created_time'].replace('Z', '+00:00')).timestamp() * 1000 - r['buy_t']
                if ts < 0:
                    any_before += 1
                    continue
                any_after += 1
                if x.get('taker_side') == side and float(x[pk]) <= r['kbuy'] + 1e-6:
                    at.append((ts / 1000, float(x['count_fp']), float(x[pk])))
            rows.append({'rec': r, 'at': at, 'any_after': any_after, 'any_before': any_before, 'n': len(tr)})
        with open(cpath, 'w', encoding='utf-8') as f:
            json.dump(cache, f)
        for rm_lo, rm_hi, lab in ((10, 1e9, 'ask >= 10c below Polymarket'), (5, 10, 'ask 5-10c below')):
            S = [x for x in rows if rm_lo <= x['rec']['room'] < rm_hi]
            if not S:
                continue
            n = len(S)
            share = lambda sec: np.mean([any(t <= sec for t, _, _ in x['at']) for x in S]) * 100
            hit = [x for x in S if x['at']]
            miss = [x for x in S if not x['at']]
            usd_at = [sum(c * p for _, c, p in x['at']) for x in hit]
            first = [min(t for t, _, _ in x['at']) for x in hit]
            out.append(f'[{tag}] {lab}: {n} test buys in {len({x["rec"]["game"] for x in S})} games')
            out.append(f'   a real taker bought our side at our price or cheaper within 5 s: {share(5):.0f}%, 30 s: {share(30):.0f}%, 60 s: {share(60):.0f}%, 5 min: {share(300):.0f}%')
            if hit:
                out.append(f'   where they did: first such print {np.median(first):.0f} s after our buy time (median), ${np.median(usd_at):,.0f} bought at those prices in 5 min (median; 25% under ${np.quantile(usd_at, 0.25):,.0f})')
            out.append(f'   any trade at all in that contract: in the minute before {np.mean([x["any_before"] > 0 for x in S]) * 100:.0f}%, in the 5 min after {np.mean([x["any_after"] > 0 for x in S]) * 100:.0f}% '
                       f'(median {np.median([x["any_after"] for x in S]):.0f} prints)')
            for name, grp in (('real buyers at our price', hit), ('nobody bought at our price', miss)):
                if grp:
                    v = np.array([x['rec']['tgt'] for x in grp])
                    out.append(f'   test result where {name}: {len(v)} buys, ${v.mean():+.2f} per $100, win {np.mean(v > 0) * 100:.0f}%')
            out.append('')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'kalshi_prints.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
