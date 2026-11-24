"""Polymarket's own public trade history for a sample of recorded games, with SIZES (24 Nov night).

pm_quotes only shows a trade when the last-trade price changed, and never its size. The Data API has every trade:
Gamma /events/{id} -> the moneyline market (condition id, the two tokens), Data API /v2/trades?condition=... (newest
first, cursor pages of 1,000). Read-only public data, paced (0.15 s between calls), cached per game in
<out>/pm_trades/<match>.json. Two-way games only (one market, two tokens).

Output <out>/pm_trades.npz, one row per trade, on the HOME token's terms:
  match, ts (s, Polymarket's stamp), buy (+1 the taker bought the home token / -1 sold it; a buy of the other token at p
  is a sale of home at 1 - p), price (home token), size (shares), raw_home (1 = the trade was in the home token).

usage: python tools/signals/prints_fetch.py --db ~/swifteye/oddsfeed.db --out ~/swifteye/research/lab [--games 400] [--pages 40]
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as PT  # noqa: E402

GAMMA, DATA = 'https://gamma-api.polymarket.com', 'https://data-api.polymarket.com/v2'
norm = lambda s: ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))
sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def get(url):
    err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read().decode())
            time.sleep(0.15)
            return d
        except Exception as ex:
            err = ex
            time.sleep(2.0 * (attempt + 1))
    return {'error': str(err)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--games', type=int, default=400)
    ap.add_argument('--pages', type=int, default=40)
    ap.add_argument('--sports', default='tennis,basketball,cs2,hockey,valorant,dota2,amfootball')
    a = ap.parse_args()
    t0 = time.time()
    cdir = os.path.join(a.out, 'pm_trades')
    os.makedirs(cdir, exist_ok=True)
    M, _ = PT.load(a.out)
    db = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True)
    rng = np.random.default_rng(11)
    want = set(a.sports.split(','))
    cand = [i for i in range(len(M['key'])) if M['k'][i] == 0 and str(M['sport'][i]) in want and M['won'][i] >= 0 and M['start'][i] > 0]
    # the same number from each sport, at random
    by = {}
    for i in cand:
        by.setdefault(str(M['sport'][i]), []).append(i)
    per = max(1, a.games // len(by))
    pick = []
    for s, v in sorted(by.items()):
        pick += list(rng.choice(v, min(per, len(v)), replace=False))
    rows, done, short = [], 0, 0
    for i in pick:
        m = int(M['match'][i])
        path = os.path.join(cdir, f'{m}.json')
        if os.path.exists(path):
            g = json.load(open(path))
        else:
            r = db.execute("SELECT e.book_event_id, mm.home, mm.away FROM book_events e JOIN matches mm ON mm.id = e.match_id WHERE e.book = 'polymarket' AND e.match_id = ? LIMIT 1", (m,)).fetchone()
            if not r:
                continue
            ev, home, away = r
            e = get(f'{GAMMA}/events/{ev}')
            mk = [x for x in e.get('markets', []) if x.get('sportsMarketType') == 'moneyline'] or e.get('markets', [])[:1]
            if not mk:
                continue
            mk = mk[0]
            outs, toks = json.loads(mk.get('outcomes') or '[]'), json.loads(mk.get('clobTokenIds') or '[]')
            if len(outs) != 2 or len(toks) != 2:
                continue
            hi = 0 if sim(outs[0], home) + sim(outs[1], away) >= sim(outs[1], home) + sim(outs[0], away) else 1
            t_first = int(M['t0'][i]) // 1000
            tr, cursor, complete = [], None, False
            for _ in range(a.pages):
                x = get(f'{DATA}/trades?condition={mk["conditionId"]}&limit=1000' + (f'&cursor={cursor}' if cursor else ''))
                data = x.get('data') or []
                tr += [(int(z['timestamp']), 1 if z['token_id'] == toks[hi] else 0, float(z['price']), float(z['size']), z.get('side')) for z in data]
                pg = x.get('pagination') or {}
                cursor = pg.get('next_cursor') or pg.get('cursor')
                if not data or not cursor or (tr and tr[-1][0] < t_first):
                    complete = True
                    break
            g = {'match': m, 'condition': mk['conditionId'], 'home_token': toks[hi], 'complete': complete, 'secondsDelay': mk.get('secondsDelay'), 'trades': tr}
            json.dump(g, open(path, 'w'))
        done += 1
        short += 0 if g['complete'] else 1
        for ts, is_home, p, sz, side in g['trades']:
            buy = 1 if (side or '').upper() == 'BUY' else -1
            rows.append((g['match'], ts, buy if is_home else -buy, p if is_home else 1 - p, sz, is_home))
        if done % 25 == 0:
            print(f'  {done} of {len(pick)} games, {len(rows):,} trades, {time.time() - t0:.0f} s', flush=True)
    R = np.array(rows, float).reshape(-1, 6)
    np.savez(os.path.join(a.out, 'pm_trades.npz'), match=R[:, 0].astype(np.int64), ts=R[:, 1].astype(np.int64), buy=R[:, 2].astype(np.int8), price=R[:, 3], size=R[:, 4], raw_home=R[:, 5].astype(np.int8))
    print(f'{done} games ({short} cut short at {a.pages} pages), {len(R):,} trades; {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
