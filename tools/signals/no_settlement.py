"""The veto and instant-exit comparisons WITHOUT settlement luck (owner, 23 Nov: who wins a match cannot be told).

  python tools/signals/no_settlement.py --db X --signals DIR

Every snipe-bot trade is valued as its plan meant it to end: its own exit when it sold within its give-up time
(caught up, or gave up on time); otherwise (held past the give-up: settled, sold by hand hours later) at the
Polymarket sell price of its token at the give-up moment, fees paid. favs (hold-to-result by design) is left out.
Then: the model's veto (walkforward_bots.py scores) and the instant exit (exit_vs_hold.py) against those values."""
import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402


def fee(p):
    return 0.05 * p * (1 - p)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    db = W.connect(a.db)
    scored = {}
    for f in glob.glob(os.path.join(a.signals, 'wf', '*', 'scored.json')):
        for r in json.load(open(f, encoding='utf-8')):
            scored[(r['bot'], r['buy'])] = r
    exits = {r['id']: r for r in json.load(open(os.path.join(a.signals, 'exit_vs_hold.json'), encoding='utf-8'))}
    rows = []
    for tid, bot, real, mid, data in db.execute("SELECT id, bot, real, match_id, data FROM bot_trades WHERE bot != 'favs'").fetchall():
        d = json.loads(data)
        if d.get('pnl') is None or not d.get('contracts') or d.get('entry') is None:
            continue
        buy, gu = d['ts'], (d.get('give_up_s') or 1800) * 1000
        actual = d['pnl'] / d['contracts'] * 100
        late = d.get('exit_ts') is None or d['exit_ts'] > buy + gu + 60_000
        planned = actual
        if late:   # what the plan would have got: the token's sell price at the give-up moment, fees paid
            o, up = d['outcome'], (d.get('dir') or 1) > 0
            q = db.execute('SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? ORDER BY ts_ms DESC LIMIT 1', (mid, o, buy + gu)).fetchone()
            px = None
            if q:
                px = q[0] if up else (None if q[1] is None else 1 - q[1])
            if px is None:   # nobody bids (a collapsed token): the first real bid within 10 min, else 0 (was: dropped)
                for b_, a_ in db.execute('SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms',
                                         (mid, o, buy + gu, buy + gu + 600_000)):
                    px = b_ if up else (None if a_ is None else 1 - a_)
                    if px is not None:
                        break
                px = 0.0 if px is None else px
            e = d['entry']
            planned = (px - fee(px) - (e + fee(e))) * 100
        s = scored.get((bot, buy))
        x = exits.get(tid)
        model_exit = None
        if x is not None:
            model_exit = x['model_net'] if (x.get('sold_at_s') is not None and x['sold_at_s'] * 1000 < gu) else planned
        rows.append({'id': tid, 'bot': bot, 'real': bool(real), 'late': late, 'actual': actual, 'planned': planned,
                     'edge': None if s is None else s['edge'], 'model_exit': model_exit, 'sold_early': x is not None and x.get('sold_at_s') is not None and x['sold_at_s'] * 1000 < gu})
    late = [r for r in rows if r['late']]
    print(f"{len(rows)} snipe-bot trades; {len(late)} held past their give-up (settled / sold by hand): as they really ended "
          f"{np.mean([r['actual'] for r in late]):+.1f}c a share, as planned {np.mean([r['planned'] for r in late]):+.1f}c")

    def line(name, rs, key='planned'):
        if not rs:
            return f'  {name:<50} no trades'
        p = np.array([r[key] for r in rs])
        return f"  {name:<50}{len(rs):>4} trades  per share {p.mean():+6.2f}c  win {100 * np.mean(p > 0):3.0f}%  total {p.sum():+7.0f}c"

    sc = [r for r in rows if r['edge'] is not None]
    print('\nThe veto, every trade valued as planned (no settlement), each day\'s model trained only on earlier games:')
    print(line('as the bots traded', sc))
    kept, skipped = [r for r in sc if r['edge'] > 0], [r for r in sc if r['edge'] <= 0]
    print(line('kept (model sees a move bigger than the fees)', kept))
    print(line('vetoed', skipped))
    if len(kept) >= 3 and len(skipped) >= 3:
        p = np.array([r['planned'] for r in sc])
        k = np.array([r['edge'] > 0 for r in sc])
        d0 = p[k].mean() - p[~k].mean()
        rng = np.random.default_rng(1)
        perm = np.array([(lambda kk: p[kk].mean() - p[~kk].mean())(rng.permutation(k)) for _ in range(20000)])
        boot = []
        for _ in range(5000):
            i = rng.integers(0, len(p), len(p))
            if k[i].any() and (~k[i]).any():
                boot.append(p[i][k[i]].mean() - p[i][~k[i]].mean())
        print(f'  kept minus vetoed {d0:+.2f}c a share (90% range {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f}); a random split does as well with p = {np.mean(perm >= d0):.3f}')
    print(line('real money only, as traded', [r for r in sc if r['real']]))
    print(line('real money only, kept', [r for r in sc if r['real'] and r['edge'] > 0]))

    ex = [r for r in rows if r['model_exit'] is not None]
    print('\nThe instant exit, against the bot\'s exits as planned (no settlement):')
    print(line('the bot (sell when caught up, else at its give-up)', ex))
    print(line('model sells at once when it calls a failure', ex, 'model_exit'))
    early = [r for r in ex if r['sold_early']]
    print(line('   of which the model sold early: the bot', early))
    print(line('   of which the model sold early: the model', early, 'model_exit'))
    with open(os.path.join(a.signals, 'no_settlement.json'), 'w', encoding='utf-8') as f:
        json.dump(rows, f, default=float)


if __name__ == '__main__':
    main()
