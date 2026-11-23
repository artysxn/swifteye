"""Head to head: the wave model at the moments the old bots bought (python tools/signals/bot_vs_model.py).

For every bot trade the Trade review page can open (the replay paper bot on this PC's games, and imported trades
from another machine), the wave model (wave_model.pt, model A) is read at the buy second: its predicted median
capturable move in the bot's direction minus fees (edge) and its directional wave-start score. Then the bots'
real results (per share, net of fees) are split by what the model said: if the model's "yes" trades did better
than its "no" trades, the model used as a filter on the old bot beats the old bot alone.

  python tools/signals/bot_vs_model.py                       trades the Trade review page can open (this PC)
  python tools/signals/bot_vs_model.py --db X --signals DIR  every bot_trades row of database X (another
                                                             machine's), with the model and split in DIR"""
import argparse
import json
import os
import sys
import urllib.request

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import train_waves as TW  # noqa: E402
import waves as W  # noqa: E402

API = 'http://127.0.0.1:8787'


def db_games(path):
    """(game dicts in the page's shape, one per game with bot trades) from a database's bot_trades."""
    db = W.connect(path)
    for (m,) in db.execute('SELECT DISTINCT match_id FROM bot_trades').fetchall():
        mt = db.execute('SELECT id, sport, home, away, start_ms FROM matches WHERE id = ?', (m,)).fetchone()
        if not mt:
            continue
        trades = [{'bot': b, 'real': bool(r), 'ts_ms': ts, 'data': json.loads(d)} for b, r, ts, d in
                  db.execute('SELECT bot, real, ts_ms, data FROM bot_trades WHERE match_id = ?', (m,)).fetchall()]
        pm, bk = W.load(db, m)
        try:
            book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
        except Exception:
            book = None
        yield {'match': {'id': m, 'sport': mt[1], 'home': mt[2], 'away': mt[3], 'start_ms': mt[4]}, 'trades': trades, 'rows': (pm, bk, book)}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--signals')
    ap.add_argument('--games-from', type=int, help='only bot trades of games that kicked off from this (ms) ...')
    ap.add_argument('--games-to', type=int, help='... to before this (ms): games the model never trained on')
    ap.add_argument('--dump', help='write the scored trades as JSON here')
    a = ap.parse_args()
    od = a.signals or os.path.join(os.path.dirname(W.default_db()), 'signals')
    ck = torch.load(os.path.join(od, 'wave_model.pt'), weights_only=False)
    mA = TW.Body(len(ck['names']), ck['hidden'], ck['A']['net.6.weight'].shape[0])
    mA.load_state_dict(ck['A'])
    mA.eval()
    mu, sd = ck['mu'], ck['sd']
    get = lambda u: json.load(urllib.request.urlopen(API + u))
    # The model's training data ends where train_waves.py's split puts it (first 60% of games by kick-off);
    # trades bought 6 h after the last training kick-off cannot have been seen (no game runs that long here).
    if a.games_from:   # an explicit window of games the model never trained on (walkforward_bots.py)
        cutoff = 0
    else:
        A = np.load(os.path.join(od, 'waves_A.npz'))
        gs = dict(zip(A['game'].tolist(), A['start'].tolist()))
        order = sorted(gs, key=lambda g: gs[g])
        cutoff = max(gs[g] for g in order[:int(0.6 * len(order))]) + 6 * 3600_000
        print(f'model trained on games up to kick-off {cutoff - 6 * 3600_000}; unseen = bought after {cutoff}')
    if a.db:
        source = list(db_games(a.db))
    else:
        source = [get(f"/review/game/{g['id']}") for g in get('/review/games?kind=bought&limit=1000')['games']]
    games = source
    rows = []
    for G in source:
        sport = G['match'].get('sport') or ''
        ko = G['match'].get('start_ms') or 0
        if (a.games_from and ko < a.games_from) or (a.games_to and ko >= a.games_to):
            continue
        cache = {}
        for t in G['trades']:
            d = t['data'] or {}
            o, buy = d.get('outcome'), d.get('ts') or t.get('ts_ms')
            if not o or not buy or d.get('pnl') is None:
                continue
            shares = d.get('contracts') or ((d.get('spent') or d.get('cost')) / d['entry'] if d.get('entry') else None)
            if not shares:
                continue
            if o not in cache:
                if 'rows' in G:
                    pm, bk, book = G['rows']
                else:
                    (pm, bk), book = F.g_rows(G, o), None
                cache[o] = F.build(pm, bk, o, sport, G['match'].get('start_ms'), sport in W.THREE_WAY, book, full=True)
            fg = cache[o]
            if fg is None:
                continue
            i = int(np.searchsorted(fg['ts'], buy, side='right') - 1)
            if i < 0 or buy - fg['ts'][i] > 5000:
                continue
            z = np.clip((fg['X'][i:i + 1] - mu) / sd, -10, 10).astype(np.float32)
            with torch.no_grad():
                out = mA(torch.from_numpy(z)).numpy()[0]
            h_up, h_dn = 1 / (1 + np.exp(-out[0])), 1 / (1 + np.exp(-out[1]))
            q_up, q_dn = np.sort(out[2:5]) * 3, np.sort(out[5:8]) * 3
            mid = fg['mid'][i] if np.isfinite(fg['mid'][i]) else 0.5
            fee_rt = 2 * 0.05 * mid * (1 - mid) * 100
            up = (d.get('dir') or 1) > 0
            rows.append({'bot': t['bot'], 'real': bool(t.get('real')), 'sport': sport, 'up': up, 'unseen': bool(a.games_from) or buy > cutoff, 'kickoff': ko, 'buy': int(buy),
                         'edge': (q_up[1] if up else q_dn[1]) - fee_rt, 'wave': (h_up - h_dn) if up else (h_dn - h_up),
                         'per_share': 100 * d['pnl'] / shares, 'usd': d['pnl']})
    print(f'{len(rows)} old-bot trades read by the model ({len(games)} games)')
    if a.dump:
        with open(a.dump, 'w', encoding='utf-8') as f:
            json.dump(rows, f, default=float)
    # Is the model's filter better than chance? Permutation test on the unseen snipe trades: how often does a random
    # subset of the same size do as well as the trades the model keeps (edge > fees)?
    rs = [r for r in rows if r['bot'] != 'favs' and r['unseen']]
    if rs:
        p = np.array([r['per_share'] for r in rs])
        keep = np.array([r['edge'] > 0 for r in rs])
        d0 = p[keep].mean() - p[~keep].mean()
        rng = np.random.default_rng(1)
        perm = np.array([(lambda k: p[k].mean() - p[~k].mean())(rng.permutation(keep)) for _ in range(20000)])
        boot = [(lambda i: p[i][keep[i]].mean() - p[i][~keep[i]].mean())(rng.integers(0, len(p), len(p))) for _ in range(5000)]
        print(f'unseen snipe trades: kept by the model {keep.sum()}, per share {p[keep].mean():+.2f}c vs skipped {(~keep).sum()} at {p[~keep].mean():+.2f}c; '
              f'difference {d0:+.2f}c, 90% range {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f}c, chance of a random split doing as well p = {np.mean(perm >= d0):.3f}')

    def line(name, rs):
        if not rs:
            return f'  {name:<46} no trades'
        p = np.array([r['per_share'] for r in rs])
        return f"  {name:<46}{len(rs):>4} trades  per share {p.mean():+6.2f}c  win {100 * np.mean(p > 0):3.0f}%  total ${sum(r['usd'] for r in rs):+8.2f}"

    for group, keep in (('all bots', lambda r: True), ('snipe bots (snipe, snipe-1book, snipe-trial, replay)', lambda r: r['bot'] != 'favs'), ('favs', lambda r: r['bot'] == 'favs'),
                        ('snipe bots, ONLY trades after the training data', lambda r: r['bot'] != 'favs' and r['unseen']),
                        ('snipe bots, real money, after the training data', lambda r: r['bot'] != 'favs' and r['unseen'] and r['real'])):
        rs = [r for r in rows if keep(r)]
        if not rs:
            continue
        print(f'\n{group}:')
        print(line('as the bots traded', rs))
        for key, label in (('edge', 'predicted move in its direction, minus fees'), ('wave', 'directional wave-start score')):
            med = float(np.median([r[key] for r in rs]))
            print(line(f'model {label} above its median ({med:+.2f})', [r for r in rs if r[key] > med]))
            print(line(f'model {label} at or below', [r for r in rs if r[key] <= med]))
        print(line('model sees a positive edge (move > fees)', [r for r in rs if r['edge'] > 0]))


if __name__ == '__main__':
    main()
