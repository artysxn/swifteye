"""Were the model's Polymarket entries and exits real prices? (python -I tools/signals/pm_prints.py --db X --signals DIR)

waves2.py's picks (waves2_picks.npz) buy at the ask our recording shows 1 s after a wave start and sell into the bid
30 min later. After the Kalshi lesson (our recording there ran minutes behind the real market) the same check on
Polymarket: its public trade history (Gamma /events/{id} -> the moneyline market's condition id and tokens; Data API
/v2/trades?condition=..., newest first, cursor pages) around each entry and each exit. For the 25 most profitable picks
and 60 random others (two-way sports only: one market, two tokens), every print within 30 s of our entry / exit is put
on OUR token's price (the other token's price p counts as 1 - p) and compared with the price the test used.
  prints near our price  -> the recorded book was the real market;
  prints far from it     -> our recording was stale there and the trade is not real.
Read-only public data, cached in <signals>/pm_prints_cache.json."""
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

GAMMA, DATA = 'https://gamma-api.polymarket.com', 'https://data-api.polymarket.com/v2'


def get(url, cache):
    if url in cache:
        return cache[url]
    err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read().decode())
            cache[url] = d
            time.sleep(0.1)
            return d
        except Exception as ex:
            err = ex
            time.sleep(2.0 * (attempt + 1))
    return {'error': str(err)}


norm = lambda s: ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))
sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    cpath = os.path.join(a.signals, 'pm_prints_cache.json')
    cache = json.load(open(cpath, encoding='utf-8')) if os.path.exists(cpath) else {}
    P = dict(np.load(os.path.join(a.signals, 'waves2_picks.npz')))
    db = sqlite3.connect('file:' + a.db.replace('\\', '/') + '?mode=ro', uri=True)
    two = np.flatnonzero(~np.isin(P['sport'], ['football', 'handball']))
    order = two[np.argsort(-P['usd'][two])]
    rng = np.random.default_rng(3)
    sel = list(order[:25]) + list(rng.choice(order[25:], min(60, max(len(order) - 25, 0)), replace=False))
    trades_of, rows = {}, []
    for rank, i in enumerate(sel):
        m, t, d = int(P['game'][i]), int(P['t'][i]), int(P['d'][i])
        r = db.execute("SELECT e.book_event_id, mm.home, mm.away FROM book_events e JOIN matches mm ON mm.id = e.match_id WHERE e.book = 'polymarket' AND e.match_id = ? LIMIT 1", (m,)).fetchone()
        if not r:
            continue
        ev, home, away = r
        e = get(f'{GAMMA}/events/{ev}', cache)
        mk = [x for x in e.get('markets', []) if x.get('sportsMarketType') == 'moneyline'] or e.get('markets', [])[:1]
        if not mk:
            continue
        mk = mk[0]
        outs, toks = json.loads(mk.get('outcomes') or '[]'), json.loads(mk.get('clobTokenIds') or '[]')
        if len(outs) != 2 or len(toks) != 2:
            continue
        hi = 0 if sim(outs[0], home) + sim(outs[1], away) >= sim(outs[1], home) + sim(outs[0], away) else 1
        ours = toks[hi] if d > 0 else toks[1 - hi]          # buying "against home" = the other side's token
        cid = mk['conditionId']
        te = t // 1000 + 1
        if cid not in trades_of:
            tr, cursor = [], None
            for _ in range(80):
                x = get(f'{DATA}/trades?condition={cid}&limit=1000' + (f'&cursor={cursor}' if cursor else ''), cache)
                data = x.get('data') or []
                tr += [(int(z['timestamp']), z['token_id'], float(z['price']), float(z['size']), z.get('side')) for z in data]
                pg = x.get('pagination') or {}
                cursor = pg.get('next_cursor') or pg.get('cursor')
                if not data or not cursor or (tr and tr[-1][0] < te - 4000):
                    break
            trades_of[cid] = sorted(tr)
        tr = trades_of[cid]
        q = db.execute('SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1', (m, 'home', (te + 1800) * 1000)).fetchone()
        sale = None if not q or q[0] is None or q[1] is None else (q[0] if d > 0 else 1 - q[1])
        px = lambda lo, hi_: [p if tok == ours else 1 - p for ts_, tok, p, sz, sd in tr if lo <= ts_ <= hi_]
        pin, pout = px(te - 30, te + 30), px(te + 1800 - 30, te + 1800 + 30)
        rows.append({'top': rank < 25, 'usd': float(P['usd'][i]), 'buy': float(P['buy'][i]), 'sale': sale, 'pin': pin, 'pout': pout, 'match': f'{home} v {away}', 'n': len(tr),
                     'covered': bool(tr) and tr[0][0] <= te - 30})
    with open(cpath, 'w', encoding='utf-8') as f:
        json.dump(cache, f)
    out = [f'Polymarket\'s own trade history around {len(rows)} of the model\'s picks (25 best + random others, two-way sports)']
    for name, grp in (('the 25 most profitable picks', [r for r in rows if r['top']]), ('random other picks', [r for r in rows if not r['top']])):
        cov = [r for r in grp if r['covered']]
        ein = [np.median(r['pin']) - r['buy'] for r in cov if r['pin']]
        eout = [np.median(r['pout']) - r['sale'] for r in cov if r['pout'] and r['sale'] is not None]
        f = lambda v: (f'median {np.median(v) * 100:+.1f}c, middle half {np.quantile(v, 0.25) * 100:+.1f}c..{np.quantile(v, 0.75) * 100:+.1f}c, within 3c of ours in {np.mean(np.abs(v) <= 0.03) * 100:.0f}% (n {len(v)})'
                       if len(v) else 'no prints')
        out.append(f'   {name}: {len(grp)} picks, trade history reaches back to the entry for {len(cov)}; real prints within 30 s of the ENTRY in {sum(1 for r in cov if r["pin"])}, of the EXIT in {sum(1 for r in cov if r["pout"])}')
        out.append(f'      real print price minus the price the test PAID: {f(ein)}')
        out.append(f'      real print price minus the price the test SOLD at: {f(eout)}')
    out.append('   the biggest winners:')
    for r in [x for x in rows if x['top']][:8]:
        out.append(f'      {r["match"][:44]:<44} ${r["usd"]:+6.0f}: paid {r["buy"] * 100:.1f}c, real prints then {np.median(r["pin"]) * 100:.1f}c ({len(r["pin"])})' if r['pin'] else
                   f'      {r["match"][:44]:<44} ${r["usd"]:+6.0f}: paid {r["buy"] * 100:.1f}c, no real print within 30 s ({r["n"]} prints in all, history {"covers" if r["covered"] else "does NOT reach"} the entry)')
        if r['sale'] is not None:
            out[-1] += f'; sold {r["sale"] * 100:.1f}c, real prints then ' + (f'{np.median(r["pout"]) * 100:.1f}c ({len(r["pout"])})' if r['pout'] else 'none within 30 s')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'pm_prints.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
