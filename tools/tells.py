"""Winners against losers: which signals, known at the moment of the buy, tell a winning snipe from a losing one?
(owner's request, 21 Nov 2026: "signals, tells and statistics that help us distinguish winners from losers")

Data: the backtest set of tools/sweep.py (every bookmaker move of 7+ points, 17-20 Nov, Polymarket by its own clock,
$12 a trade, sold when Polymarket has caught up or after 5 minutes), plus the signals of systems_backtest.py and
tells_feats.py. One trade per game per 2 minutes, as the bot's lock works, in three sets of rules:
  ANY     no rule at all: every move whose buy was really there
  LOOSE   loose rules (move 7+, spread <= 6c, followed <= 15%, room 2c+, price <= 95c)
  BOT     today's rules
A signal counts only if it splits the trades the same way on separate days and on both halves of the games.

usage: tells.py rank | cats | losers | model | intrade | all
"""
import sys, os, json, math, random, hashlib, statistics as st
from collections import defaultdict, Counter
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
MODE = sys.argv[1] if len(sys.argv) > 1 else 'all'
_ARGV = list(sys.argv)
sys.argv = [sys.argv[0], 'lib']
import sweep as S
R = os.path.expanduser('~/swifteye/research/')
F = json.load(open(R + 'systems_feats.json'))
G = json.load(open(R + 'tells_feats.json')) if os.path.exists(R + 'tells_feats.json') else {}
key = lambda c: f"{c['match_id']}|{c['outcome']}|{c['t0']}"
HOLE = json.load(open(R + 'hole_gap.json')) if os.path.exists(R + 'hole_gap.json') else {}
LAGS = json.load(open(R + 'feed_lag.json')) if os.path.exists(R + 'feed_lag.json') else {}
CLEAN_ONLY = '--clean' in _ARGV


def clean(c):
    """True = Polymarket quotes were being recorded at the buy; False = the buy sits in a hole; None = not measured."""
    k = key(c); h = HOLE.get(k)
    if h: return h['gap'] <= 2.0
    if k in HOLE or k in LAGS:
        l = LAGS.get(k)
        return (l < 300) if l is not None else None
    return None


ci, gi = 2, 2
P = dict(S.BASE, legacy=False, clean=False)      # this tool splits clean and gap trades itself (--clean)
LOOSE = dict(S.BASE, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False, bb='all', clean=False)
ANY = dict(LOOSE, max_followed=9, min_edge=-9, max_spread=9, cap=9, pause=False)
half = lambda m: int(hashlib.md5(str(m).encode()).hexdigest()[:6], 16) % 2
random.seed(7)

# activity in the same game before each move (from every recorded move, bought or not)
_by = defaultdict(list)
for c in S.ROWS: _by[c['match_id']].append(c)
for rows in _by.values():
    rows.sort(key=lambda c: c['t0'])
    for i, c in enumerate(rows):
        side = lambda x: x['dir'] * (1 if x['outcome'] == c['outcome'] else -1)   # +1 = same side as this move
        prev = [x for x in rows[max(0, i - 400):i] if c['t0'] - x['t0'] <= 600_000 and c['t0'] - x['t0'] > 3000]
        c['_n2'] = sum(1 for x in prev if c['t0'] - x['t0'] <= 120_000)
        c['_n10'] = len(prev)
        c['_same10'] = (sum(1 for x in prev if side(x) > 0) / len(prev)) if prev else None
        c['_flip'] = (side(prev[-1]) < 0) if prev else None


def at_buy(c, entry=None):
    cx = c['cx']; f = F.get(key(c)) or {}; g = G.get(key(c)) or {}
    entry = c['px'] if entry is None else entry
    r = dict(
        c=c, m=c['match_id'], sport=c['sport'], day=c['_day'],
        # --- known at the buy
        move=c['_mv'], rise=c['dir'] > 0, price=entry, off50=abs(entry - 0.5), followed=c['_fol'], spread=c['_sp'], room=c['_emin'][ci], gap=c['_gap'],
        books=c['_books'], fams=cx['fams'], fam_moved=cx['fam_moved'], fam_share=cx['fam_moved'] / max(cx['fams'], 1), cons_move=cx['cons_move'],
        cons_share=cx['cons_move'] / c['_mv'], gap_shock=cx['gap_shock'], jumps=cx['jumps'], follow=cx['follow'] if cx['jumps'] else None,
        lead=cx['lead'] if cx['jumps'] else None, score=(cx['follow'] - cx['lead']) if cx['jumps'] else None, z=cx['z'],
        pm_trend10=c['pm_tr'], book_tr5=c['b_tr5'], book_tr10=c['b_tr10'], book_range=c['b_rng'], move_vs_range=c['_mv'] / c['b_rng'] if c['b_rng'] > 0 else None,
        late_s=c['late_ms'] / 1000, order_s=(c['tf'] - c['t0']) / 1000, mins_in=(c['tf'] - c['start_ms']) / 60000 if c.get('start_ms') else None, hour=c['_hour'],
        weak_feed=c['_weak'], trig=S.platform(c['trigger']), depth_best=(c['buy'][0] * c['px']) if c['buy'] else None,
        n2=c['_n2'], n10=c['_n10'], same10=c['_same10'], flip=c['_flip'],
        toward=f.get('toward'), verdict=f.get('v'), ev=f.get('ev'),
        q_age=g.get('q_age'), q_n60=g.get('q_n60'), q_upd=g.get('q_upd'), sz_tf=g.get('sz_tf'), sz_ratio=g.get('sz_ratio'), imb=g.get('imb'),
        drift10=g.get('drift10'), drift60=g.get('drift60'), k_gap=g.get('k_gap'), k_mv=g.get('k_mv'), oth_buy=g.get('oth_buy'), n_fam60=g.get('n_fam60'), g=g, f=f)
    return r


def rec(c, t):
    pnl, spent, entry, ex = t
    r = at_buy(c, entry)
    r.update(pnl=pnl, spent=spent, ret=pnl / spent, win=pnl > 0, caught=bool(c['exits'][ci][gi][4]), entry=entry, dur=(c['exits'][ci][gi][0] - c['tf']) / 1000)
    return r


def universe(p, lock=120_000, extra=None):
    out, recent = [], {}
    for c in S.ROWS:
        if not S.gate(c, p, ci): continue
        if extra and not extra(c): continue
        m = c['match_id']
        if m in recent and c['tf'] - recent[m] < lock: continue
        recent[m] = c['tf']
        if c['nofill'] or not c['exits'][ci][gi]: continue
        t = S.trade(c, c['exits'][ci][gi], p, ci)
        if t: out.append(rec(c, t))
    if CLEAN_ONLY: out = [r for r in out if clean(r['c']) is True]
    return out


NUM = ['move', 'price', 'off50', 'followed', 'spread', 'room', 'gap', 'books', 'fams', 'fam_moved', 'fam_share', 'cons_move', 'cons_share', 'gap_shock', 'jumps', 'follow',
       'lead', 'score', 'z', 'pm_trend10', 'book_tr5', 'book_tr10', 'book_range', 'move_vs_range', 'late_s', 'order_s', 'mins_in', 'hour', 'depth_best', 'n2', 'n10', 'same10',
       'toward', 'ev', 'q_age', 'q_n60', 'q_upd', 'sz_tf', 'sz_ratio', 'imb', 'drift10', 'drift60', 'k_gap', 'k_mv', 'oth_buy', 'n_fam60']
CAT = ['sport', 'rise', 'weak_feed', 'trig', 'verdict', 'flip', 'day']
ret = lambda x: (sum(r['pnl'] for r in x) / sum(r['spent'] for r in x)) if x else float('nan')


def edges(vals, k):
    v = sorted(vals); e = []
    for i in range(1, k):
        q = v[min(len(v) - 1, int(len(v) * i / k))]
        if not e or q > e[-1]: e.append(q)
    return e


def binof(x, e):
    b = 0
    for q in e:
        if x >= q: b += 1
    return b


def split(U, feat, k):
    """rows with the signal, grouped low -> high. Returns (groups, edges)."""
    have = [r for r in U if r[feat] is not None]
    if len(have) < 60: return None, None
    e = edges([r[feat] for r in have], k)
    if not e: return None, None
    gr = [[] for _ in range(len(e) + 1)]
    for r in have: gr[binof(r[feat], e)].append(r)
    gr = [g for g in gr if g]
    return (gr, e) if len(gr) >= 2 else (None, None)


def boot_diff(a, b, n=400):
    """top minus bottom return per $, resampling whole games; returns (diff, sd, share of resamples with the same sign)."""
    by = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in a: by[r['m']][0] += r['pnl']; by[r['m']][1] += r['spent']
    for r in b: by[r['m']][2] += r['pnl']; by[r['m']][3] += r['spent']
    ks = list(by); d0 = ret(a) - ret(b); v = []
    for _ in range(n):
        s = [by[random.choice(ks)] for _ in ks]
        pa, sa, pb, sb = (sum(x[i] for x in s) for i in range(4))
        if sa > 0 and sb > 0: v.append(pa / sa - pb / sb)
    if len(v) < 50: return d0, float('nan'), float('nan')
    return d0, st.pstdev(v), sum(1 for x in v if (x > 0) == (d0 > 0)) / len(v)


def consistency(a, b):
    """in how many of the days, and of the two halves of the games, top beats bottom (same sign as overall)."""
    d0 = ret(a) - ret(b); ok = tot = 0
    for dy in ('17', '18', '19', '20'):
        x, y = [r for r in a if r['day'] == dy], [r for r in b if r['day'] == dy]
        if len(x) >= 8 and len(y) >= 8: tot += 1; ok += ((ret(x) - ret(y)) > 0) == (d0 > 0)
    hv = 0
    for h in (0, 1):
        x, y = [r for r in a if half(r['m']) == h], [r for r in b if half(r['m']) == h]
        if x and y: hv += ((ret(x) - ret(y)) > 0) == (d0 > 0)
    return ok, tot, hv


def line(g):
    return f"{len(g):5d} trades {len({r['m'] for r in g}):4d} games | caught up {100 * st.mean(r['caught'] for r in g):3.0f}% | won {100 * st.mean(r['win'] for r in g):3.0f}% | {100 * ret(g):+6.1f}% a $"


def fmt(feat, x):
    if feat in ('move', 'price', 'off50', 'followed', 'spread', 'room', 'gap', 'cons_move', 'gap_shock', 'pm_trend10', 'book_tr5', 'book_tr10', 'book_range', 'drift10', 'drift60', 'k_gap', 'k_mv'):
        return f"{100 * x:.1f}c" if feat not in ('followed',) else f"{100 * x:.0f}%"
    return f"{x:.2f}" if abs(x) < 10 else f"{x:.0f}"


def rank(U, name, k):
    print(f"\n==== {name}: {line(U)}")
    print(f"     every numeric signal split into {k} equal groups, lowest to highest; ranked by how far apart the top and bottom groups' returns are, in units of chance (z)")
    res = []
    for feat in NUM:
        gr, e = split(U, feat, k)
        if not gr: continue
        d, sd, same = boot_diff(gr[-1], gr[0])
        ok, tot, hv = consistency(gr[-1], gr[0])
        rets = [ret(g) for g in gr]
        mono = sum(1 for i in range(len(rets) - 1) if (rets[i + 1] - rets[i]) * d > 0) / (len(rets) - 1)
        res.append((abs(d / sd) if sd and sd == sd else 0, feat, gr, e, d, sd, ok, tot, hv, mono))
    res.sort(key=lambda x: -x[0])
    print(f"  {'signal':14s} {'with it':>7s} | {'groups low -> high: return per $ (won %)':70s} | top-bottom |   z  | days same way | halves | steady rise")
    for z, feat, gr, e, d, sd, ok, tot, hv, mono in res:
        cells = '  '.join(f"{100 * ret(g):+5.1f}({100 * st.mean(r['win'] for r in g):2.0f})" for g in gr)
        print(f"  {feat:14s} {sum(len(g) for g in gr):7d} | {cells:70s} | {100 * d:+8.1f}% | {z:4.1f} | {ok}/{tot:<11d} | {hv}/2    | {100 * mono:3.0f}%")
    return res


def detail(U, feat, k):
    gr, e = split(U, feat, k)
    if not gr: return
    print(f"  -- {feat}")
    lo = None
    for i, g in enumerate(gr):
        a = min(r[feat] for r in g); b = max(r[feat] for r in g)
        days = ' '.join(f"{dy}:{100 * ret([r for r in g if r['day'] == dy]):+5.0f}%({sum(1 for r in g if r['day'] == dy)})" for dy in ('17', '18', '19', '20'))
        print(f"     {fmt(feat, a):>7s} to {fmt(feat, b):>7s}: {line(g)} | big losses {100 * st.mean(r['ret'] <= -0.25 for r in g):3.0f}% | by day {days}")


def cats(U, name):
    print(f"\n==== {name}: groups by kind")
    for feat in CAT:
        vals = Counter(r[feat] for r in U)
        print(f"  -- {feat}")
        for v, n in vals.most_common(9):
            g = [r for r in U if r[feat] == v]
            if len(g) < 15: continue
            days = ' '.join(f"{dy}:{100 * ret([r for r in g if r['day'] == dy]):+5.0f}%({sum(1 for r in g if r['day'] == dy)})" for dy in ('17', '18', '19', '20'))
            print(f"     {str(v):12s} {line(g)} | by day {days}")


if __name__ == '__main__':
    UA, UL, UB = universe(ANY), universe(LOOSE), universe(dict(P, one_per_game=False))
    print(f"signals from the database: {len(G)} moves (tells_feats), {len(F)} (systems_feats)")
    if MODE in ('rank', 'all'):
        ra = rank(UA, 'ANY (no rules)', 5)
        rl = rank(UL, 'LOOSE rules', 4)
        rb = rank(UB, "BOT's rules", 2)
        print("\n==== detail of the strongest signals under LOOSE rules")
        for z, feat, *_ in rl[:14]: detail(UL, feat, 4)
    if MODE in ('cats', 'all'):
        cats(UL, 'LOOSE rules'); cats(UB, "BOT's rules")


# ------------------------------------------------------------------ which of the bot's rules a move fails
def fails(c, p=P):
    cx = c['cx']; f = []
    if c['_mv'] < p['min_move']: f.append('move under 10 pt')
    if c['_fol'] > p['max_followed'] or (c['_tfol'] is not None and c['_tfol'] > p['max_followed']): f.append('Polymarket already followed 5%+')
    if cx['fam_moved'] < p['fams']: f.append('under 2 bookmaker groups')
    if cx['cons_move'] < p['cons']: f.append("bookmakers' average moved under 3 pt")
    if c['_sp'] > p['max_spread'] + 1e-9: f.append('spread over 4c')
    if c['_emin'][ci] < p['min_edge']: f.append('room under 4c')
    if c['sport'] == 'basketball': f.append('basketball')
    if c['pause']: f.append('paused game')
    if c['_px'] > p['cap']: f.append('price over 90c')
    if cx['jumps'] == 0: f.append('no earlier jump in the game')
    elif cx['follow'] < p['lag']: f.append('Polymarket did not lag earlier (follow under 30%)')
    if c['_gap'] is not None and c['_gap'] >= p['gap']: f.append('gap 10 pt+ already')
    return f


def near():
    """Moves that fail exactly ONE of the bot's rules: is the rule keeping money out, or keeping losses out?"""
    print("\n==== NEAR MISSES: moves that fail exactly one of today's rules (one trade per game per 2 minutes within each group)")
    grp = defaultdict(list)
    for c in S.ROWS:
        f = fails(c)
        if len(f) == 1: grp[f[0]].append(c)
    for why, rows in sorted(grp.items(), key=lambda x: -len(x[1])):
        ids = {id(c) for c in rows}
        U = universe(ANY, extra=lambda c: id(c) in ids)
        if len(U) < 10: print(f"  {why:52s} {len(U)} trades: too few"); continue
        by = defaultdict(float)
        for r in U: by[r['m']] += r['pnl']
        ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(600))
        days = ' '.join(f"{dy}:{sum(r['pnl'] for r in U if r['day'] == dy):+6.1f}" for dy in ('17', '18', '19', '20'))
        print(f"  {why:52s} {line(U)} | ${sum(r['pnl'] for r in U):+7.2f} (90% range {v[30]:+.0f} to {v[570]:+.0f}) | by day {days}")
        if why in ('move under 10 pt', 'basketball', 'Polymarket already followed 5%+', 'spread over 4c', 'room under 4c', 'price over 90c'):
            for sp, n in Counter(r['sport'] for r in U).most_common(5):
                g = [r for r in U if r['sport'] == sp]
                if len(g) >= 8: print(f"       {sp:11s} {line(g)} | ${sum(r['pnl'] for r in g):+7.2f}")


def show(r):
    c = r['c']; g = r['g']
    return (f"{r['day']} Nov {c['_hour']:02d}h {r['sport']:10s} {(c['home'] + ' v ' + c['away'])[:34]:34s} paid {100 * r['entry']:4.1f}c pnl {r['pnl']:+6.2f} {'caught' if r['caught'] else 'gave up'} {r['dur']:4.0f}s | move {100 * r['move']:4.1f} "
            f"score {r['score'] if r['score'] is None else round(r['score'], 2)} follow {None if r['follow'] is None else round(r['follow'], 2)} lead {None if r['lead'] is None else round(r['lead'], 2)} jumps {r['jumps']} spread {100 * r['spread']:.1f}c "
            f"toward {None if r['toward'] is None else round(r['toward'], 2)} v {r['verdict']} fams {r['fam_moved']}/{r['fams']} trig {r['trig']} late {r['late_s']:.1f}s mins {None if r['mins_in'] is None else round(r['mins_in'])} "
            f"oth_buy {None if g.get('oth_buy') is None else round(g['oth_buy'], 2)} q_age {g.get('q_age')} k_gap {None if g.get('k_gap') is None else round(100 * g['k_gap'], 1)}")


def losers(U, name):
    print(f"\n==== {name}: the worst and the best trades")
    U = sorted(U, key=lambda r: r['pnl'])
    for r in U[:14]: print('  ' + show(r))
    print('  ...')
    for r in U[-8:]: print('  ' + show(r))
    big = [r for r in U if r['ret'] <= -0.25]; rest = [r for r in U if r['ret'] > -0.25]
    print(f"  big losses (a quarter of the stake or more): {len(big)} of {len(U)} trades, ${sum(r['pnl'] for r in big):+.2f}; all other trades ${sum(r['pnl'] for r in rest):+.2f}")
    print(f"  {'signal':14s} {'big losers (middle)':>20s} {'other trades (middle)':>22s}")
    for feat in NUM:
        a = [r[feat] for r in big if r[feat] is not None]; b = [r[feat] for r in rest if r[feat] is not None]
        if len(a) >= 5 and len(b) >= 5: print(f"  {feat:14s} {st.median(a):20.3f} {st.median(b):22.3f}")


if __name__ == '__main__':
    if MODE in ('near', 'all'): near()
    if MODE in ('losers', 'all'): losers(UB, "BOT's rules")
    if MODE in ('cond', 'all'):
        UG = universe(dict(LOOSE, lag=0.3))
        rg = rank(UG, "LOOSE rules + 'Polymarket lagged earlier' (follow 30%+)", 3)
        for z, feat, *_ in rg[:10]: detail(UG, feat, 3)
        cats(UG, "LOOSE + lagged earlier")


# ------------------------------------------------------------------ a combined score, learned on some days and tested on the others
MODEL_F = [f for f in NUM if f not in ('late_s', 'order_s', 'hour', 'books', 'fam_moved', 'k_gap', 'k_mv', 'sz_ratio', 'imb')]


def learn(train, k=3, zmin=2.0, feats=MODEL_F):
    """Tells found on the training trades only: a signal counts when its top and bottom thirds differ by zmin units of chance
    and point the same way on both halves of the training games. +1 point in the good third, -1 in the bad third."""
    rules = []
    for feat in feats:
        gr, e = split(train, feat, k)
        if not gr or len(e) < 1: continue
        d, sd, same = boot_diff(gr[-1], gr[0], n=150)
        if not sd or sd != sd or abs(d / sd) < zmin: continue
        if consistency(gr[-1], gr[0])[2] < 2: continue
        rules.append((feat, 1 if d > 0 else -1, e[0], e[-1], abs(d / sd)))
    return rules


def points(r, rules):
    s = 0
    for feat, sign, lo, hi, z in rules:
        v = r[feat]
        if v is None: continue
        s += sign * ((v >= hi) - (v < lo))
    return s


def logit(train, feats=MODEL_F, lam=2.0, iters=350, lr=0.15):
    med = {}; mu = {}; sd = {}
    for f in feats:
        v = [float(r[f]) for r in train if r[f] is not None]
        med[f] = st.median(v) if v else 0.0
        x = [float(r[f]) if r[f] is not None else med[f] for r in train]
        lo, hi = sorted(x)[int(len(x) * .02)], sorted(x)[int(len(x) * .98)]
        x = [min(max(a, lo), hi) for a in x]
        mu[f] = st.mean(x); sd[f] = st.pstdev(x) or 1.0; med[f] = (med[f], lo, hi)
    def vec(r):
        return [((min(max(float(r[f]) if r[f] is not None else med[f][0], med[f][1]), med[f][2])) - mu[f]) / sd[f] for f in feats]
    X = [vec(r) for r in train]; y = [1.0 if r['win'] else 0.0 for r in train]; n = len(X); w = [0.0] * len(feats); b = 0.0
    for _ in range(iters):
        gw = [0.0] * len(feats); gb = 0.0
        for xi, yi in zip(X, y):
            p = 1 / (1 + math.exp(-max(min(b + sum(a * c for a, c in zip(w, xi)), 30), -30))); e = p - yi; gb += e
            for j, a in enumerate(xi): gw[j] += e * a
        w = [wj - lr * (g / n + lam * wj / n) for wj, g in zip(w, gw)]; b -= lr * gb / n
    return (lambda r: b + sum(a * c for a, c in zip(w, vec(r)))), sorted(zip(w, feats), key=lambda x: -abs(x[0]))


def oos(U, name):
    print(f"\n==== COMBINED SCORE, tested on trades it never saw: {name} ({line(U)})")
    for scheme, folds in (('each day predicted from the other three', [('day ' + d, lambda r, d=d: r['day'] == d) for d in ('17', '18', '19', '20')]),
                          ('each half of the games predicted from the other half', [('half ' + str(h), lambda r, h=h: half(r['m']) == h) for h in (0, 1)])):
        pool = {'points': [], 'model': []}
        print(f"  -- {scheme}")
        for nm, test in folds:
            tr, te = [r for r in U if not test(r)], [r for r in U if test(r)]
            if len(te) < 20 or len(tr) < 100: continue
            rules = learn(tr); fn, w = logit(tr)
            print(f"     {nm}: tells found without it: " + (', '.join(f"{f}{'+' if s > 0 else '-'}" for f, s, *_ in rules) or 'none'))
            for kind, sc in (('points', lambda r: points(r, rules)), ('model', fn)):
                x = sorted(te, key=sc)
                for i, r in enumerate(x): pool[kind].append((i / len(x), r))
        for kind in ('points', 'model'):
            x = pool[kind]
            for lab, lo, hi in (('best quarter', 0.75, 1.01), ('second quarter', 0.5, 0.75), ('third quarter', 0.25, 0.5), ('worst quarter', 0.0, 0.25)):
                g = [r for q, r in x if lo <= q < hi]
                if not g: continue
                d, sd, same = boot_diff(g, [r for q, r in x if not lo <= q < hi])
                print(f"     {kind:6s} {lab:14s} {line(g)} | ${sum(r['pnl'] for r in g):+7.2f} | big losses {100 * st.mean(r['ret'] <= -0.25 for r in g):3.0f}% | against the rest {100 * d:+5.1f}% (z {d / sd if sd else 0:+.1f})")


if __name__ == '__main__':
    if MODE in ('model', 'all'):
        UM = universe(dict(LOOSE, bb='none'))
        oos(UM, 'LOOSE rules, no basketball')
        rules = learn(UM); fn, w = logit(UM)
        print("\n  tells found on ALL trades (for the record; this is hindsight): " + ', '.join(f"{f}{'+' if s > 0 else '-'} (z {z:.1f})" for f, s, lo, hi, z in rules))
        print("  model weights, largest first: " + ', '.join(f"{f} {x:+.2f}" for x, f in w[:14]))


# ------------------------------------------------------------------ tells AFTER the buy: when is it right to let a trade go early?
CK = (10, 30, 60, 120, 180)


def sold_at(r, T):
    b = r['g'].get(f'bid{T}')
    if b is None: return None
    n = r['spent'] / r['entry']; ex = max(b - 0.01, 0.0)
    return n * (ex - r['entry']) - n * (S.fee(r['entry']) + (S.fee(ex) if 0 < ex < 1 else 0))


def intrade(U, name):
    U = [r for r in U if r['g'].get('bid10') is not None]
    print(f"\n==== AFTER THE BUY: {name} ({line(U)}; total ${sum(r['pnl'] for r in U):+.2f})")
    print("  trades still open T seconds after the buy: what the state then says about the rest of the trade ('kept' = sold as today, 'sold now' = sold at that moment)")
    for T in CK:
        O = [r for r in U if r['dur'] > T and sold_at(r, T) is not None]
        if len(O) < 12: continue
        print(f"  -- {T} s after the buy: {len(O)} still open, {100 * st.mean(r['caught'] for r in O):.0f}% later caught up, kept ${sum(r['pnl'] for r in O):+.2f}, sold now ${sum(sold_at(r, T) for r in O):+.2f}")
        tests = [('our price has fallen 3c+', lambda r: r['g'][f'bid{T}'] - r['entry'] <= -0.03), ('our price has fallen 1-3c', lambda r: -0.03 < r['g'][f'bid{T}'] - r['entry'] <= -0.01),
                 ('our price is level or up', lambda r: r['g'][f'bid{T}'] - r['entry'] > -0.01),
                 ('trigger gave back half+', lambda r: r['g'].get(f'trg{T}') is not None and r['g'][f'trg{T}'] < 0.5), ('trigger still holds', lambda r: r['g'].get(f'trg{T}') is not None and r['g'][f'trg{T}'] >= 0.5),
                 ('others have NOT followed (<10%)', lambda r: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
                 ('others followed 10-50%', lambda r: r['g'].get(f'oth{T}') is not None and 0.1 <= r['g'][f'oth{T}'] < 0.5),
                 ('others followed 50%+', lambda r: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] >= 0.5),
                 ('others went the OTHER way', lambda r: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < -0.1)]
        for nm, fn in tests:
            g = [r for r in O if fn(r)]
            if len(g) < 6: continue
            keep, sell = sum(r['pnl'] for r in g), sum(sold_at(r, T) for r in g)
            print(f"       {nm:32s} {len(g):4d} trades | later caught up {100 * st.mean(r['caught'] for r in g):3.0f}% | kept ${keep:+8.2f} | sold now ${sell:+8.2f} | selling now is {'BETTER' if sell > keep else 'worse'} by ${abs(sell - keep):.2f}")
    print("  as rules: sell at the first check where the condition holds, else as today")
    rules = [('others have not followed 10% by 30 s', (30,), lambda r, T: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
             ('others have not followed 10% by 60 s', (60,), lambda r, T: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
             ('others have not followed 10% by 120 s', (120,), lambda r, T: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
             ('others went the other way (10, 30, 60, 120 s)', (10, 30, 60, 120), lambda r, T: r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < -0.1),
             ('trigger gave back half (10, 30, 60, 120 s)', (10, 30, 60, 120), lambda r, T: r['g'].get(f'trg{T}') is not None and r['g'][f'trg{T}'] < 0.5),
             ('trigger gave back half AND others under 10%', (10, 30, 60, 120), lambda r, T: r['g'].get(f'trg{T}') is not None and r['g'][f'trg{T}'] < 0.5 and r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
             ('our price fell 3c+ (30, 60, 120 s)', (30, 60, 120), lambda r, T: r['g'][f'bid{T}'] - r['entry'] <= -0.03),
             ('our price fell 3c+ AND others under 10%', (30, 60, 120), lambda r, T: r['g'][f'bid{T}'] - r['entry'] <= -0.03 and r['g'].get(f'oth{T}') is not None and r['g'][f'oth{T}'] < 0.1),
             ('no catch-up by 120 s at all', (120,), lambda r, T: True), ('no catch-up by 180 s at all', (180,), lambda r, T: True)]
    base = sum(r['pnl'] for r in U)
    for nm, Ts, fn in rules:
        new = {}; fired = 0
        for r in U:
            v = r['pnl']
            for T in Ts:
                if r['dur'] > T and sold_at(r, T) is not None and fn(r, T): v = sold_at(r, T); fired += 1; break
            new[id(r)] = v
        by = defaultdict(float)
        for r in U: by[r['m']] += new[id(r)] - r['pnl']
        ks = list(by); v = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(800))
        tot = sum(new.values())
        print(f"     {nm:48s} fires on {fired:3d} | total ${tot:+8.2f} ({tot - base:+7.2f}, 90% range {v[40]:+.0f} to {v[760]:+.0f}) | worst trade ${min(new.values()):+.2f} (today ${min(r['pnl'] for r in U):+.2f}) | by day " +
              ' '.join(f"{dy}:{sum(new[id(r)] - r['pnl'] for r in U if r['day'] == dy):+6.1f}" for dy in ('17', '18', '19', '20')))


MID = dict(P, min_move=0.07, max_followed=0.10, min_edge=0.03, one_per_game=False)
if __name__ == '__main__':
    if MODE in ('intrade', 'all'):
        intrade(UB, "BOT's rules"); intrade(universe(MID), "bot's rules a little looser (move 7+, followed 10%, room 3c)")
    if MODE in ('mid', 'all'):
        UMID = universe(MID)
        rm = rank(UMID, "bot's rules a little looser (move 7+, followed <=10%, room 3c+)", 3)
        for z, feat, *_ in rm[:12]: detail(UMID, feat, 3)
        cats(UMID, 'a little looser'); oos(UMID, "bot's rules a little looser")
