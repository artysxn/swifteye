"""Is the in-app replay (Rust, adaptive.rs) the same thing as the checked copy of the live bot
(live_emu.py / live_cands.py)?

1. Build the replay's candidates:  SWIFTEYE_DB=~/swifteye/oddsfeed.db FROM_MS=.. TO_MS=.. OUT=rust.json \
       cargo test --release dump_live_path -- --ignored --nocapture
2. Build the test's candidates:    python3 live_cands.py FROM_MS TO_MS cands.pickle
3. python3 compare_replay.py rust.json cands.pickle

The replay is the stricter one by design: it fills by the recorded book depth and takes 1c off
every sale; the test fills at the best price and sells at the bid.
"""
import json, sys
from collections import Counter, defaultdict
import live_eval as L

R = json.load(open(sys.argv[1])); C = L.load(sys.argv[2])
key = lambda c: (c['t0'], c['m'], c['o'], int(c['d']), c['book'])
rc = {key(c): c for c in R['cands']}; pc = {key(c): c for c in C}
both = set(rc) & set(pc)
print(f"candidates: replay {len(rc)} ({sum(c['nofill'] for c in rc.values())} cannot fill), test {len(pc)}, in both {len(both)}, only in the test {len(set(pc) - set(rc))}")
def cmp(name, f, tol=1e-6):
    d = [abs(f(rc[k]) - f(pc[k])) for k in both]
    print(f"  {name:22s} identical on {sum(x <= tol for x in d) / len(d) * 100:6.2f}%   worst difference {max(d):.4f}")
cmp('bookmaker move', lambda c: c['bm'][0]); cmp('polymarket followed', lambda c: c['fol'][1]); cmp('spread', lambda c: c['sp'][1])
cmp('moved since 1st book', lambda c: c['fol_start'] if isinstance(c['fol_start'], float) else c['fol_start'][90000])
cmp('expected gain', lambda c: c['edge'][1], 1e-4); cmp('feed families moved', lambda c: c['cx']['fam_moved'])
cmp('consensus move', lambda c: c['cx']['cons_move']); cmp('polymarket lag', lambda c: c['cx']['follow']); cmp('after a pause', lambda c: float(c['pause']))
day = lambda t0: L.U(t0).strftime('%d')
for give in (900, 300):
    rt = {(t['t0'], t['m']): t for t in R['trades'][f'give{give}']}
    T = L.pick(C, lambda c: L.safe(c), give=give)
    pt = {(t['c']['t0'], t['c']['m']): t for t in T}
    b = set(rt) & set(pt)
    print(f"\nrules live now, give up after {give // 60} min, $12 a trade, 12-21 UTC")
    print(f"  replay: {len(rt):3d} trades, {sum(t['pnl'] > 0 for t in rt.values()):3d} won, {sum(t['pnl'] <= -10.8 for t in rt.values())} lost the whole stake, total {sum(t['pnl'] for t in rt.values()):+8.2f}")
    print(f"  test:   {len(pt):3d} trades, {sum(t['ret'] > 0 for t in pt.values()):3d} won, {sum(t['ret'] <= -0.9 for t in pt.values())} lost the whole stake, total {sum(12 * t['ret'] for t in pt.values()):+8.2f}")
    print(f"  the same trade in both: {len(b)}  (replay {sum(rt[k]['pnl'] for k in b):+.2f}, test {sum(12 * pt[k]['ret'] for k in b):+.2f})")
    byd = defaultdict(lambda: [0, 0.0, 0, 0.0])
    for k, t in rt.items(): byd[day(k[0])][0] += 1; byd[day(k[0])][1] += t['pnl']
    for k, t in pt.items(): byd[day(k[0])][2] += 1; byd[day(k[0])][3] += 12 * t['ret']
    print('  by day (replay trades, total | test trades, total):', {d: f"{v[0]}, {v[1]:+.1f} | {v[2]}, {v[3]:+.1f}" for d, v in sorted(byd.items())})
    only_r = sorted(set(rt) - set(pt)); only_p = sorted(set(pt) - set(rt))
    why = Counter()
    for k in only_p:
        r = [x for x in R['cands'] if x['t0'] == k[0] and x['m'] == k[1]]
        why['replay: cannot fill / too thin for $12' if r else 'replay never measured it'] += 1
    print(f"  only in the replay: {len(only_r)}; only in the test: {len(only_p)} {dict(why)}")
