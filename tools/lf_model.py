"""PICK THE CATCH-UPS, trained further (owner, 22 Nov 2026: "17-22 nov is what we have data on. we can improve it further,
train it further").

Data: tools/lf_build.py v2 (every bookmaker jump of 4 pt+ that Polymarket had not yet made, with decision-time signals),
events as in lf_patterns.py, 8 pt+ jumps, spread 6c or less. Label and bet as tools/lf_funnel.py / lf_predict.py:
  CAUGHT UP   Polymarket's sell price reaches the decision sell price + 75% of the jump within 5 minutes
  the bet     bought at the buy price when the order lands, sold when the sell price reaches the decision sell price +
              the jump, else at 5 min; 1c off the sale, both fees, $12
Signals (only what is known at the decision, 2 s after the jump): the jump's size; bookmaker families that had jumped
the same way by then; Polymarket's move before (60 s, 10 s, last second); this bookmaker's record in the game (earlier
jumps, share followed, median lead); correlation; spread; price; sport; family; pause; hour; and (v2) the other
bookmakers' consensus against Polymarket (share of the jump), their move in the last 10 s, how many moved the same way /
did not move / have a price, Polymarket's depth and best size on both sides, its quote activity in the last minute,
minutes into the game, the trigger's own 10-minute range against the jump, seconds since the last jump on that side.
Models (pure Python, every signal cut into bands, one weight a band, L2 penalty, AdaGrad):
  P   logistic regression for CAUGHT UP
  $   ridge regression for the bet's $ (clipped to +-12)
Judged WALK-FORWARD: for each test day, fitted only on the days before it. Reported per test day and pooled.
usage: lf_model.py      -> ~/swifteye/research/lf_model.txt
"""
import os, sys, math, time, bisect, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP

fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
PS = 2


def walk(e):
    pb = e['pb']; size = e['size']; bd = e['bid_d']; a = e['ask_e']
    path = [(k * PS, pb[k] / 100.0) for k in range(len(pb)) if pb[k] != 255]
    if not path: return None
    c75 = next((s for s, v in path if v >= bd + 0.75 * size - 1e-9), None)
    hit = next(((s, v) for s, v in path if v >= bd + size - 1e-9), None)
    x = (hit[1] if hit else path[-1][1]) - 0.01
    n = 12.0 / a
    return (1 if c75 is not None else 0), n * (x - a) - n * (fee(a) + fee(max(x, 0.001)))


if __name__ == '__main__' or os.environ.get('LF_MODEL_RUN'):
    C = LP.load(sample=len(sys.argv) > 1 and sys.argv[1] == 'sample')
    by_side = collections.defaultdict(list)
    for c in C: by_side[(c['m'], c['o'])].append((c['t'], LP.FAM.get(c['book'], c['book'])))
    for k in by_side: by_side[k].sort()
    E = [LP.measure(e) for e in LP.events(C)]
    E = [e for e in E if e.get('best') is not None and e['spread'] <= 0.06 + 1e-9 and e['size'] >= 0.08 - 1e-9]
    rows = []
    for e in E:
        w = walk(e)
        if not w: continue
        L = by_side[(e['m'], e['o'])]; t = e['t']; sz = e['size']
        i0 = bisect.bisect_left(L, (t - 20_000, '')); i1 = bisect.bisect_right(L, (t + 2000, '~'))
        s = e['sb'] if e['sb'][0] >= 3 else e['sf']
        g = lambda k: e.get(k)
        f = dict(size=100 * sz, fams=len({ff for tt, ff in L[i0:i1]}), pre60=e['pre'], pre10=e['pre10'], now=((e['bid_d'] + e['ask_d']) / 2 - e['m1']) / sz,
                 jumps=e['sb'][0], follow=(s[6] if s[1] >= 2 else None), lead=(s[4] if s[1] >= 2 else None), corr=e['corr'], spread=100 * (e['ask_d'] - e['bid_d']),
                 price=100 * e['ask_e'], sport=e['sport'], fam=LP.FAM.get(e['book'], e['book']), pause=1 if e['pause'] else 0, hour=int(time.strftime('%H', time.localtime(t / 1000))) // 4,
                 cons_gap=(g('cons_gap') / sz) if g('cons_gap') is not None else None, cons_move=(g('cons_move10') / sz) if g('cons_move10') is not None else None,
                 n_others=g('n_others'), moved_same=g('moved_same'), share_moved=(g('moved_same') / g('n_others')) if g('n_others') else None, not_moved=g('not_moved'),
                 ask_usd=(g('ask_size') * e['ask_d']) if g('ask_size') is not None else None, bid_usd=(g('bid_size') * e['bid_d']) if g('bid_size') is not None else None,
                 ask_depth=g('ask_depth'), bid_depth=g('bid_depth'), depth_tilt=((g('bid_depth') - g('ask_depth')) / (g('bid_depth') + g('ask_depth'))) if g('bid_depth') and g('ask_depth') else None,
                 act=g('pm_act60'), mins_in=g('mins_in'), noise=(g('b_rng') / sz) if g('b_rng') is not None else None, since_prev=g('since_prev'))
        rows.append(dict(f=f, y=w[0], pnl=w[1], day=time.strftime('%d', time.localtime(t / 1000))))
    CUTS = dict(size=[10, 12, 15, 20, 30], fams=[2, 3, 4, 6], pre60=[-0.25, -0.05, 0.05, 0.25, 0.6], pre10=[-0.1, 0.0, 0.1, 0.3], now=[-0.1, 0.05, 0.2, 0.5],
                jumps=[1, 3, 8, 20], follow=[0.3, 0.5, 0.7, 0.9], lead=[-10, -3, 3, 10], corr=[50, 80, 92], spread=[1.5, 2.5, 4.5], price=[20, 35, 50, 65, 80],
                cons_gap=[-0.5, 0, 0.25, 0.5, 1.0], cons_move=[0, 0.25, 0.5, 1.0], n_others=[2, 4, 7, 10], moved_same=[1, 2, 4, 7], share_moved=[0.2, 0.4, 0.7],
                not_moved=[1, 3, 6], ask_usd=[10, 50, 200, 1000], bid_usd=[10, 50, 200, 1000], ask_depth=[100, 500, 2000, 8000], bid_depth=[100, 500, 2000, 8000],
                depth_tilt=[-0.5, -0.2, 0.2, 0.5], act=[5, 15, 40, 100], mins_in=[15, 45, 90, 150], noise=[0.5, 1, 2, 4], since_prev=[5, 30, 120, 600])
    CAT = ('sport', 'fam', 'pause', 'hour')
    KEYS = list(CUTS) + list(CAT)
    INDEX = {}


    def feats(r):
        out = []
        for k in KEYS:
            v = r['f'][k]
            b = 'none' if v is None else (str(v) if k in CAT else str(bisect.bisect_right(CUTS[k], v)))
            key = (k, b)
            if key not in INDEX: INDEX[key] = len(INDEX)
            out.append(INDEX[key])
        return out


    for r in rows: r['x'] = feats(r)
    NF = len(INDEX)


    def fit(R, kind, lam=0.02, iters=120, lr=0.3):
        w = [0.0] * NF; b = (math.log(max(1e-3, sum(r['y'] for r in R) / len(R)) / max(1e-3, 1 - sum(r['y'] for r in R) / len(R))) if kind == 'P' else sum(max(-12, min(12, r['pnl'])) for r in R) / len(R))
        G = [1e-8] * NF; Gb = 1e-8; N = len(R)
        for _ in range(iters):
            grad = [0.0] * NF; gb = 0.0
            for r in R:
                z = b + sum(w[j] for j in r['x'])
                if kind == 'P': err = 1 / (1 + math.exp(-max(-30, min(30, z)))) - r['y']
                else: err = (z - max(-12, min(12, r['pnl']))) / 4.0
                gb += err
                for j in r['x']: grad[j] += err
            for j in range(NF):
                gj = grad[j] / N + lam * w[j]; G[j] += gj * gj; w[j] -= lr * gj / math.sqrt(G[j])
            gb /= N; Gb += gb * gb; b -= lr * gb / math.sqrt(Gb)
        return w, b


    def pred(m, r):
        w, b = m; return b + sum(w[j] for j in r['x'])


    days = sorted({r['day'] for r in rows})
    out = []; P_ = out.append
    P_('PICK THE CATCH-UPS, trained further: %d events of 8 pt+ jumps, days %s Nov (%s)' % (len(rows), '/'.join(days), ', '.join('%s: %d, %.0f%% caught' % (d, sum(1 for r in rows if r['day'] == d), 100.0 * sum(r['y'] for r in rows if r['day'] == d) / max(1, sum(1 for r in rows if r['day'] == d))) for d in days)))
    P_('Walk-forward: each test day judged by models fitted only on the days before it. $12 a bet.\n')
    pooled = collections.defaultdict(list)
    for d in days[2:]:
        tr = [r for r in rows if r['day'] < d]; te = [r for r in rows if r['day'] == d]
        if len(te) < 50: continue
        mP = fit(tr, 'P'); mD = fit(tr, '$')
        for r in te: r['p'] = pred(mP, r); r['d'] = pred(mD, r)
        P_('TEST DAY %s Nov (trained on %d events of %s): %d events, %.0f%% caught up; all as bets %+.2f $' % (d, len(tr), '/'.join(sorted({r['day'] for r in tr})), len(te), 100.0 * sum(r['y'] for r in te) / len(te), sum(r['pnl'] for r in te)))
        for mname, key in (('chance model', 'p'), ('$ model', 'd')):
            X = sorted(te, key=lambda r: -r[key])
            cells = []
            for share in (0.02, 0.05, 0.10, 0.20):
                Y = X[:max(1, int(share * len(X)))]
                cells.append('top %2.0f%%: %3d bets, caught %2.0f%%, won %2.0f%%, %+7.2f $' % (100 * share, len(Y), 100.0 * sum(r['y'] for r in Y) / len(Y), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / len(Y), sum(r['pnl'] for r in Y)))
                pooled[(mname, share)] += Y
            P_('   %-13s %s' % (mname, ' | '.join(cells)))
        for th in (0.0, 0.5, 1.0):
            Y = [r for r in te if r['d'] > th]; pooled[('$ > %.1f' % th, 0)] += Y
            P_('   $ model expects more than %+.1f $: %4d bets | caught %3.0f%% | won %3.0f%% | %+8.2f $' % (th, len(Y), 100.0 * sum(r['y'] for r in Y) / max(1, len(Y)), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / max(1, len(Y)), sum(r['pnl'] for r in Y)))
        P_('')
    P_('POOLED OVER THE TEST DAYS (every bet judged on a day its model never saw):')
    for k, Y in pooled.items():
        if not Y: continue
        lab = ('%s, top %.0f%% each day' % (k[0], 100 * k[1])) if k[1] else ('$ model expects %s' % k[0][2:])
        P_('  %-36s %5d bets | caught %3.0f%% | won %3.0f%% | %+9.2f $ = %+5.2f a bet | by day %s' % (lab, len(Y), 100.0 * sum(r['y'] for r in Y) / len(Y), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / len(Y), sum(r['pnl'] for r in Y), sum(r['pnl'] for r in Y) / len(Y),
            ' '.join('%s:%+.0f' % (d, sum(r['pnl'] for r in Y if r['day'] == d)) for d in sorted({r['day'] for r in Y}))))
    # what the model leans on (fitted on every day), biggest band weights of the $ model
    mD = fit(rows, '$'); inv = {v: k for k, v in INDEX.items()}
    top = sorted(range(NF), key=lambda j: -abs(mD[0][j]))[:24]
    P_('\nWHAT THE $ MODEL LEANS ON (fitted on all days; the bands that move the expected $ most):')
    for j in top:
        k, b = inv[j]; n = sum(1 for r in rows if j in r['x'])
        lab = b if (k in CAT or b == 'none') else ('<%g' % CUTS[k][0] if b == '0' else ('%g+' % CUTS[k][-1] if int(b) == len(CUTS[k]) else '%g-%g' % (CUTS[k][int(b) - 1], CUTS[k][int(b)])))
        P_('  %-12s %-10s %+5.2f $ a bet   (%d events)' % (k, lab, mD[0][j], n))
    txt = '\n'.join(out); print(txt)
    open(os.path.expanduser('~/swifteye/research/lf_model.txt'), 'w').write(txt + '\n')
