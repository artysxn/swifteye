# What could the bot have known AT THE BUY about the wave? Per trade, from data before the buy only.
import sys, os, time, statistics as st, collections
sys.path.insert(0, os.path.expanduser('~/swifteye-src/tools'))
import waves as W
db = W.conn(); T = W.trades(db)
rows = []
for t in T:
    r = W.analyse(db, t); g = r['g']; d = t['d']; im, ib = r['im'], r['ib']
    mv = d.get('move', 0) or 1e-9
    mid = g['mid']; trg = g['trig']; cons = g['cons']
    pm_now = mid[max(im - 1, 0)]
    w = [v for v in mid[max(0, im - 120):im] if v is not None]
    pm_rise = (pm_now - min(w)) / mv if (w and pm_now is not None) else None           # how much of the move Polymarket had ALREADY risen from its 2-min low
    pm_fall = (max(w) - pm_now) / mv if (w and pm_now is not None) else None           # ... or had fallen from its 2-min high
    wb = [v for v in (cons[max(0, im - 120):im]) if v is not None]
    bk_before = cons[max(im - 2, 0)]
    bounce = (max(wb) - bk_before) / mv if (wb and bk_before is not None) else None     # the bookmakers had just DROPPED this much: the move is a bounce back
    gap = (bk_before - pm_now) if (bk_before is not None and pm_now is not None) else None   # bookmakers above Polymarket before the move
    hg, pr = W.history(db, t, 60)
    m = [(B, P) for B, P in pr if P]
    h_first = (sum(1 for B, P in m if P['b0'] < B['b0']) / len(m)) if len(m) >= 4 else None
    h_ratio = st.median([abs(P['size'] / B['size']) for B, P in m]) if len(m) >= 4 else None
    h_lag = st.median([P['b0'] - B['b0'] for B, P in m]) if len(m) >= 4 else None
    real_share = ((r['peak'] - r['pre_our']) / mv) if r['pre_our'] is not None else None   # how much of the move the wave's top covered
    rows.append(dict(id=t['id'], day=time.strftime('%d', time.localtime(t['ts'] / 1000)), sport=d.get('sport'), base=r['base'], cls=r['cls'], pm_rise=pm_rise, pm_fall=pm_fall, bounce=bounce, gap=gap, h_first=h_first, h_ratio=h_ratio, h_lag=h_lag, share=real_share, top=W.pnl12(d, r['peak']), n_pairs=len(m)))
def split(name, key, cuts):
    print('\n' + name)
    v = [x for x in rows if x[key] is not None]
    for lo, hi in cuts:
        gset = [x for x in v if lo <= x[key] < hi]
        if not gset: continue
        print('   %5.2f to %5.2f: %2d trades | won %2d | bot $%+7.2f | wave reached target %2d | top of the wave covered %.0f%% of the move (median) | by day %s' % (lo, hi, len(gset), sum(1 for x in gset if x['base'] > 0), sum(x['base'] for x in gset), sum(1 for x in gset if x['cls'] == 'wave reached the target'), 100 * st.median([x['share'] for x in gset if x['share'] is not None] or [0]), ' '.join('%s:%+.1f' % (dy, sum(x['base'] for x in gset if x['day'] == dy)) for dy in ('18', '19', '20', '21'))))
print('%d buys, bot result $%+.2f at $12' % (len(rows), sum(x['base'] for x in rows)))
split('A. Polymarket had ALREADY risen in the 2 min before the bookmaker move (share of the move)', 'pm_rise', [(-9, 0.10), (0.10, 0.30), (0.30, 9)])
split('B. Polymarket had been FALLING in the 2 min before (share of the move)', 'pm_fall', [(-9, 0.10), (0.10, 0.50), (0.50, 9)])
split('C. the bookmakers had just dropped: the move is a bounce back (share of the move)', 'bounce', [(-9, 0.25), (0.25, 0.75), (0.75, 9)])
split('D. bookmakers above Polymarket BEFORE the move (cents/100)', 'gap', [(-9, -0.02), (-0.02, 0.03), (0.03, 0.06), (0.06, 9)])
split('E. in this game\'s last hour, share of legs Polymarket started first', 'h_first', [(0, 0.40), (0.40, 0.60), (0.60, 1.01)])
split('F. in this game\'s last hour, median start of Polymarket\'s leg after the bookmakers\' (s)', 'h_lag', [(-999, -3), (-3, 5), (5, 999)])
split('G. in this game\'s last hour, size of Polymarket\'s legs against the bookmakers\'', 'h_ratio', [(0, 0.8), (0.8, 1.1), (1.1, 99)])
sh = sorted(x['share'] for x in rows if x['share'] is not None)
print('\ntop of the wave as a share of the bookmaker move, all trades: quarter %.2f, median %.2f, three quarters %.2f; reached 100%%: %d of %d; reached 50%%: %d' % (sh[len(sh)//4], sh[len(sh)//2], sh[3*len(sh)//4], sum(1 for x in sh if x >= 1.0), len(sh), sum(1 for x in sh if x >= 0.5)))
