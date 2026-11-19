"""Backtest rule variants on the IN-APP REPLAY's own saved measurements
(~/swifteye/adaptive_cache/v16-*.json), following adaptive.rs run_policy + Flt + fill():
depth-aware fills, 1c off every sale, the 2-minute lock, one position a game.

Check first:  python3 replay_eval.py     -> must print 100 trades, 79 won, +186.11 for the base rules
(the number the Rust harness gave for 12-21 UTC, $12, give-up 5 min, sell at 100%).
"""
import json, glob, datetime as dt
from collections import Counter, defaultdict

CATCH = [0.50, 0.75, 1.00]; GIVE = [60, 120, 300, 900, 1800]; CAP = 0.02; MIN_FILL = 10.0
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
fee = lambda n, p: n * 0.05 * p * (1 - p)
KEEP = ('match_id', 'sport', 'home', 'outcome', 'dir', 'trigger', 'tf', 't0', 'books', 'bm', 'fol', 'fol_start', 'late_ms', 'sp', 'edge', 'px',
        'buy', 'pause', 'exits', 'cx', 'nofill', 'dec', 'pre_mid')

def load():
    out = []
    for f in sorted(glob.glob('/Users/danielvinichenko/swifteye/adaptive_cache/v16-*.json')):
        d = json.load(open(f))
        if isinstance(d, list) and d:
            out += [{k: c.get(k) for k in KEEP} for c in d[0]]
    out.sort(key=lambda c: c['tf'])
    return out

def platform(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b

BASE = dict(min_books=1, max_followed=0.05, min_edge=0.04, max_spread=0.04, min_move=0.10, catch=1.0, give=300, pause=True, walk=True,
            fams=2, cons=0.03, follow=0.3, gap=0.10, cap=0.90, hours=(12, 21))

def gap_before(c):
    return (c['cx']['cons_now'] - c['cx']['cons_move'] * c['dir'] - c['pre_mid']) * c['dir'] if c['pre_mid'] > 0 else None

def passes(c, p, extra=None):
    ci = CATCH.index(p['catch']); cx = c['cx']
    if p['hours'] and not (p['hours'][0] <= U(c['tf']).hour < p['hours'][1]): return False
    if min(c['books']) < p['min_books'] or c['bm'][0] < p['min_move']: return False
    if max(c['fol']) > p['max_followed'] or c['fol_start'] > p['max_followed']: return False
    if max(c['sp']) > p['max_spread'] + 1e-9: return False
    if c['edge'][0][ci] < p['min_edge'] or c['edge'][1][ci] < p['min_edge']: return False
    if p['pause'] and c['pause']: return False
    if c['sport'] == 'basketball' or cx['fam_moved'] < p['fams'] or cx['cons_move'] < p['cons']: return False
    if cx['jumps'] == 0 or cx['follow'] < p['follow']: return False
    g = gap_before(c)
    if p['gap'] is not None and g is not None and g >= p['gap']: return False
    px = max(c['dec']) if c['dec'][0] > 0 else c['px'] + 0.01
    if px > p['cap']: return False
    return extra(c) if extra else True

def fill(c, x, stake, walk):
    if c['buy']:
        best, near = c['buy']; near = near if walk else best
        a = min(stake / c['px'], max(best, 0.0)); b = max(min((stake - a * c['px']) / (c['px'] + 0.02), max(near - best, 0.0)), 0.0)
        n = a + b; entry = (a * c['px'] + b * (c['px'] + 0.02)) / n if n > 0 else c['px']
        if n <= 0 or n * entry < MIN_FILL: return None
    else:
        n, entry = stake / c['px'], c['px']
    if x['sell']:
        best, near = x['sell']; a = min(n, max(best, 0.0)); b = min(n - a, max(near - best, 0.0)); cc = n - a - b
        ex = max((a * x['px'] + b * (x['px'] - 0.02) + cc * (x['px'] - 0.05)) / n, 0.0)
    else:
        ex = x['px']
    return dict(pnl=n * (ex - entry) - fee(n, entry) - fee(n, max(ex, 0.0)), spent=n * entry, entry=entry, exit=ex)

def run(C, stake=12.0, extra=None, sizer=None, **kw):
    p = dict(BASE, **kw); ci = CATCH.index(p['catch']); gi = GIVE.index(p['give'])
    out, recent, busy = [], {}, {}
    for c in C:
        if not passes(c, p, extra): continue
        m = c['match_id']
        if m in recent and c['tf'] - recent[m] < 120_000: continue
        if c['nofill']: recent[m] = c['tf']; continue
        x = c['exits'][ci][gi]
        if not x: continue
        if busy.get(m, 0) > c['tf']: recent[m] = c['tf']; continue
        recent[m] = c['tf']; busy[m] = x['ts']
        s = sizer(c, p, ci) if sizer else stake
        f = fill(c, x, s, p['walk'] and c['edge'][1][ci] - CAP >= p['min_edge'] - 1e-9)
        if f: out.append(dict(c=c, x=x, stake=s, **f))
    return out

def line(name, T):
    n = len(T)
    if not n: return f"  {name:44s} n=0"
    w = sum(t['pnl'] > 0 for t in T); tot = sum(t['pnl'] for t in T); sp = sum(t['spent'] for t in T)
    bal = peak = dd = 0
    for t in sorted(T, key=lambda t: t['x']['ts']): bal += t['pnl']; peak = max(peak, bal); dd = max(dd, peak - bal)
    d = defaultdict(lambda: [0, 0.0])
    for t in T: k = U(t['c']['tf']).strftime('%d'); d[k][0] += 1; d[k][1] += t['pnl']
    return (f"  {name:44s} n={n:3d} won {w / n * 100:3.0f}%  total {tot:+8.2f}  per $ {tot / sp * 100:+5.1f}%  drop ${dd:5.1f}  lost 90%+: {sum(t['pnl'] <= -0.9 * t['spent'] for t in T):2d}"
            f"  | " + ' '.join(f"{k}: {d[k][0]:2d} {d[k][1]:+6.1f}" for k in ('17', '18', '19')))

if __name__ == '__main__':
    C = load()
    print(len(C), 'replay candidates')
    print(line('base rules (must be 100 / 79% / +186.11)', run(C)))
    print(line('give-up 15 min (must be 97 / +159.57)', run(C, give=900)))
