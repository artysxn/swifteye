"""Rolling walk-forward of the wave model against the old bots: python tools/signals/walkforward_bots.py --db X --data DIR

For each day D (UTC) from --first to the last bot trade: train the wave models only on games that kicked off before
D (train_waves.py --before D), then read the bot trades of games that kick off on day D with that model
(bot_vs_model.py --games-from D --games-to D+1). Every bot trade is judged by a model that never saw its game or
anything after it. The scored trades of all days are pooled: kept (model's predicted move beats the fees) against
skipped, per share net of fees, with a bootstrap range and a permutation test."""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--data', required=True)
    ap.add_argument('--first', default='2026-11-19')
    ap.add_argument('--last', default='2026-11-23')
    ap.add_argument('--epochs', type=int, default=10)
    a = ap.parse_args()
    day = dt.datetime.fromisoformat(a.first).replace(tzinfo=dt.timezone.utc)
    end = dt.datetime.fromisoformat(a.last).replace(tzinfo=dt.timezone.utc)
    rows = []
    while day <= end:
        d0 = int(day.timestamp() * 1000)
        d1 = int((day + dt.timedelta(days=1)).timestamp() * 1000)
        out = os.path.join(a.data, 'wf', day.strftime('%Y%m%d'))
        os.makedirs(out, exist_ok=True)
        dump = os.path.join(out, 'scored.json')
        if os.path.exists(dump):   # a day already done in this walk-forward (resumed run)
            day_rows = json.load(open(dump, encoding='utf-8'))
            print(f'== {day:%d %b}: done before, {len(day_rows)} bot trades', flush=True)
            rows += day_rows
            day += dt.timedelta(days=1)
            continue
        print(f'== {day:%d %b}: train on games before it, score that day\'s bot trades', flush=True)
        subprocess.run([sys.executable, os.path.join(HERE, 'train_waves.py'), '--data', a.data, '--before', str(d0), '--epochs', str(a.epochs), '--out', out, '--no-exit'],
                       check=True, stdout=subprocess.DEVNULL)
        dump = os.path.join(out, 'scored.json')
        subprocess.run([sys.executable, os.path.join(HERE, 'bot_vs_model.py'), '--db', a.db, '--signals', out, '--games-from', str(d0), '--games-to', str(d1), '--dump', dump],
                       check=True, stdout=subprocess.DEVNULL)
        day_rows = json.load(open(dump, encoding='utf-8'))
        print(f'   {len(day_rows)} bot trades scored', flush=True)
        rows += day_rows
        day += dt.timedelta(days=1)

    def line(name, rs):
        if not rs:
            return f'  {name:<52} no trades'
        p = np.array([r['per_share'] for r in rs])
        return f"  {name:<52}{len(rs):>4} trades  per share {p.mean():+6.2f}c  win {100 * np.mean(p > 0):3.0f}%  total ${sum(r['usd'] for r in rs):+8.2f}"

    lines = [f'Rolling walk-forward, {a.first} .. {a.last}: every bot trade judged by a model trained only on games that kicked off before its day.']
    for group, keep in (('snipe bots (snipe, snipe-1book, snipe-trial)', lambda r: r['bot'] != 'favs'), ('favs', lambda r: r['bot'] == 'favs'), ('all bots', lambda r: True),
                        ('snipe bots, real money', lambda r: r['bot'] != 'favs' and r['real'])):
        rs = [r for r in rows if keep(r)]
        if not rs:
            continue
        lines.append(f'\n{group}:')
        lines.append(line('as the bots traded', rs))
        lines.append(line('kept: the model sees a move bigger than the fees', [r for r in rs if r['edge'] > 0]))
        lines.append(line('skipped', [r for r in rs if r['edge'] <= 0]))
        med = float(np.median([r['edge'] for r in rs]))
        lines.append(line(f'top half by the model ({med:+.2f}c and up)', [r for r in rs if r['edge'] > med]))
        lines.append(line('bottom half', [r for r in rs if r['edge'] <= med]))
        p = np.array([r['per_share'] for r in rs])
        k = np.array([r['edge'] > 0 for r in rs])
        if k.sum() >= 3 and (~k).sum() >= 3:
            d0_ = p[k].mean() - p[~k].mean()
            rng = np.random.default_rng(1)
            perm = np.array([(lambda kk: p[kk].mean() - p[~kk].mean())(rng.permutation(k)) for _ in range(20000)])
            boot = []
            for _ in range(5000):
                i = rng.integers(0, len(p), len(p))
                if k[i].any() and (~k[i]).any():
                    boot.append(p[i][k[i]].mean() - p[i][~k[i]].mean())
            lines.append(f'  kept minus skipped {d0_:+.2f}c a share (90% range {np.quantile(boot, 0.05):+.2f}..{np.quantile(boot, 0.95):+.2f}); '
                         f'a random split does as well with p = {np.mean(perm >= d0_):.3f}')
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(a.data, 'wf', 'walkforward_bots.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
