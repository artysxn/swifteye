"""Every version side by side, on one footing (python tools/signals/compare_versions.py --db X --signals DIR).

Cents per share, net of fees, no settlement luck (old-bot trades valued as their plan meant them to end:
no_settlement.py), on the same window: trades bought 19 Nov 00:00 UTC to the end of the recording. Each row says
whether its choices were made only on earlier days (clean) or with the whole period in view (exploratory)."""
import argparse
import datetime as dt
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402

START = int(dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc).timestamp() * 1000)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    db = W.connect(a.db)
    buy = {tid: json.loads(d)['ts'] for tid, d in db.execute('SELECT id, data FROM bot_trades')}
    end_ms = db.execute('SELECT MAX(ts_ms) FROM pm_quotes').fetchone()[0]
    days = (end_ms - START) / 86_400_000
    ns = [r for r in json.load(open(os.path.join(a.signals, 'no_settlement.json'), encoding='utf-8')) if buy.get(r['id'], 0) >= START]
    v2 = {r['id']: r for r in json.load(open(os.path.join(a.signals, 'exit_v2.json'), encoding='utf-8'))}
    rng = np.random.default_rng(7)

    def row(name, vals, kind, note=''):
        v = np.array([x for x in vals if x is not None and np.isfinite(x)], dtype=float)
        if not len(v):
            return (name, 0, 0, None, None, None, None, None, kind, note)
        boot = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(5000)]
        return (name, len(v), len(v) / days, v.mean(), np.quantile(boot, 0.05), np.quantile(boot, 0.95), 100 * np.mean(v > 0), v.sum(), kind, note)

    rows = [
        row('Old snipe bots (snipe, 1book, trial), as planned', [r['planned'] for r in ns], 'baseline', 'its own buys and exits'),
        row('Old bots + model veto (keep if move > fees)', [r['planned'] for r in ns if r['edge'] is not None and r['edge'] > 0], 'clean', 'day-by-day models'),
        row('   the trades the veto removed', [r['planned'] for r in ns if r['edge'] is not None and not r['edge'] > 0], 'clean', ''),
        row('Old bots + instant exit v1', [r['model_exit'] for r in ns if r['model_exit'] is not None], 'clean', 'sold after ~16 s'),
        row('Old bots + instant exit v2', [v2[r['id']]['model'] for r in ns if r['id'] in v2], 'clean', 'patient, ~97 s'),
        row('Old bots + veto + exit v2', [v2[r['id']]['model'] for r in ns if r['id'] in v2 and r['edge'] is not None and r['edge'] > 0], 'clean', 'both'),
    ]
    # Bookmaker-leads (owner's second drawing): pooled numbers from booklead.py test (same window, day by day).
    bl = {}
    txt = open(os.path.join(a.signals, 'booklead_test.txt'), encoding='utf-8').read()
    for line in txt.splitlines():
        s = line.strip()
        for key in ('every instance', 'the rule', 'the dedicated model'):
            if s.startswith(key):
                parts = s.split()
                n = int(parts[parts.index('trades') - 1])
                mean = float(s.split('mean ')[1].split('c')[0])
                lo, hi = [float(x.rstrip('c)')) for x in s.split('(90% ')[1].split(')')[0].split('..')]
                win = float(s.split('win ')[1].split('%')[0])
                tot = float(s.split('total ')[1].rstrip('c'))
                bl[key] = (n, mean, lo, hi, win, tot)
    for key, name, note in (('every instance', 'Bookmaker leads: every signal (no filter)', 'your 2nd drawing'),
                            ('the rule', 'Bookmaker leads: rule (gap 8+, model OK, your exit)', 'chosen on earlier days'),
                            ('the dedicated model', 'Bookmaker leads: dedicated model', 'trained on earlier days')):
        if key in bl:
            n, mean, lo, hi, win, tot = bl[key]
            rows.append((name, n, n / days, mean, lo, hi, win, tot, 'clean', note))
    # Exploratory, whole-period numbers (pattern_catchup.py, train_waves.py) for context.
    def pat(fname, sel):
        p = os.path.join(a.signals, fname)
        if not os.path.exists(p):
            return []
        return [r['base'] for r in json.load(open(p, encoding='utf-8')) if r['t'] >= START and sel(r)]
    rows.append(row('Your 1st drawing (PM first, bookmaker past it)', pat('pattern_drawn.json', lambda r: True) or pat('pattern_catchup.json', lambda r: True), 'exploratory', 'plain exit, no filter'))
    rows.append(row('   ... when the model takes it', pat('pattern_drawn.json', lambda r: r['th'] is not None and r['edge'] >= r['th']) or pat('pattern_catchup.json', lambda r: r['th'] is not None and r['edge'] >= r['th']), 'clean', 'day-by-day models, plain exit'))

    print(f'Window: trades bought from 19 Nov 00:00 UTC to the end of the Mac recording ({days:.1f} days). Cents a share, fees paid, no settlement luck.\n')
    print(f"{'version':<52}{'trades':>7}{'/day':>6}{'mean':>8}{'90% range':>17}{'win':>6}{'total':>8}   {'test':<11} note")
    for name, n, pd_, mean, lo, hi, win, tot, kind, note in rows:
        if not n:
            print(f'{name:<52}{0:>7}   no trades')
            continue
        print(f"{name:<52}{n:>7}{pd_:>6.0f}{mean:>+7.2f}c{f'{lo:+.2f}..{hi:+.2f}':>17}{win:>5.0f}%{tot:>+8.0f}   {kind:<11} {note}")
    out = {'days': days, 'rows': [dict(zip(('name', 'trades', 'per_day', 'mean', 'lo', 'hi', 'win', 'total', 'test', 'note'), r)) for r in rows]}
    with open(os.path.join(a.signals, 'compare_versions.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, default=float, indent=1)


if __name__ == '__main__':
    main()
