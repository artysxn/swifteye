"""Can a wave be seen COMING, or its END? (owner, 22 Nov 2026: "is there a way to confidently identify when a wave
begins, or is about to begin, or is about to end?")

Patterns first: every 2 s of 400 games Swifteye recorded (the same games as tools/wave_fade.py, in play), what was known
then, and what Polymarket's price did next. Nothing is traded here.

Known at the moment (all from the past only):
  bk30      the bookmakers' move in the last 30 s: the middle of every bookmaker's change of the home side's chance
            (1 / its price), points; bookmakers by the time their price reached us
  bkn       how many bookmakers moved 3 pt+ for home in the last 30 s, minus how many moved 3 pt+ against
  gap60     the bookmakers' move in the last 60 s minus Polymarket's (the middle of its book), points: + = bookmakers
            ahead for home
  imb       the best price's size: (size on the buy side - size on the sell side) / both, for home (-1..1)
  dimb      the same with the whole book's depth
  spread    Polymarket's spread now, cents; wide = the spread over 1.5 x this game's usual
  act       book updates in the last 30 s, against this game's usual
  mom10/30  Polymarket's own move in the last 10 / 30 s, cents
What happened next:
  a WAVE STARTS = the middle moves 5c+ one way within the next 60 s (and further than it goes the other way)
  the TOP = inside a wave of 5c+ that is still going, the wave's top (before a 3c turn) comes within the next 10 s

usage: wave_predict.py build      -> ~/swifteye/research/paths/predict_400.pickle  (reads the db read-only, by match_id)
       wave_predict.py test       -> tables, ~/swifteye/research/paths/wave_predict.txt
"""
import sys, os, sqlite3, pickle, bisect, statistics as st, collections
from array import array
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
OUT = os.path.expanduser('~/swifteye/research/paths/predict_400.pickle')
BOOKS = os.path.expanduser('~/swifteye/research/paths/book_400.pickle')
STEP = 2_000
NAN = float('nan')
isn = lambda x: x != x


def build():
    G0 = pickle.load(open(BOOKS, 'rb'))
    db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
    start_of = {r[0]: r[1] for r in db.execute("SELECT id, start_ms FROM matches WHERE id IN (%s)" % ','.join(str(g['id']) for g in G0))}
    out = []
    for n, g0 in enumerate(G0):
        mid_id = g0['id']; start = start_of[mid_id]; nstep = len(g0['bid']); end = start + (nstep - 1) * STEP
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id=? AND outcome='home' AND bid > 0 AND ask > 0 AND ask > bid ORDER BY id", (mid_id,)).fetchall()
        q = [x for x in q if start - 120_000 <= x[0] <= end]
        qt = [x[0] for x in q]
        ch = db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome='home' AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid_id,)).fetchall()
        lines = collections.defaultdict(lambda: ([], []))
        for t, b, o, p in ch:
            if start - 600_000 <= t <= end:
                L = lines[b]; L[0].append(t); L[1].append((100.0 / p) if p and p > 1.0 else None)
        # every bookmaker's value on the 2-s grid, from 60 s before the start (index + 30)
        grids = []
        for L in lines.values():
            gr = []; j = 0; last_v = None
            for s_ in range(-30, nstep):
                u = start + s_ * STEP
                while j < len(L[0]) and L[0][j] <= u: last_v = L[1][j]; j += 1
                gr.append(last_v)
            grids.append(gr)
        F = {k: array('f') for k in ('mid', 'spread', 'imb', 'dimb', 'act', 'bk30', 'bkn', 'gap60')}
        k = 0; last = None
        for s in range(nstep):
            u = start + s * STEP
            while k < len(q) and q[k][0] <= u: last = q[k]; k += 1
            if last is None:
                for a in F.values(): a.append(float('nan'))
                continue
            _, b, a, bs, as_, bd, ad = last
            F['mid'].append((a + b) * 50); F['spread'].append((a - b) * 100)
            F['imb'].append(((bs or 0) - (as_ or 0)) / ((bs or 0) + (as_ or 0)) if (bs or 0) + (as_ or 0) > 0 else 0.0)
            F['dimb'].append(((bd or 0) - (ad or 0)) / ((bd or 0) + (ad or 0)) if (bd or 0) + (ad or 0) > 0 else 0.0)
            F['act'].append(bisect.bisect_right(qt, u) - bisect.bisect_right(qt, u - 30_000))
            mv = []; up = dn = 0; mv60 = []
            for gr in grids:
                v1, v0, v00 = gr[s + 30], gr[s + 15], gr[s]
                if v1 is not None and v0 is not None:
                    mv.append(v1 - v0); up += (v1 - v0 >= 3); dn += (v1 - v0 <= -3)
                if v1 is not None and v00 is not None: mv60.append(v1 - v00)
            F['bk30'].append(st.median(mv) if mv else float('nan')); F['bkn'].append(up - dn)
            F['gap60'].append(float('nan') if not mv60 or s < 30 or isn(F['mid'][s - 30]) else st.median(mv60) - (F['mid'][s] - F['mid'][s - 30]))
        out.append(dict(id=mid_id, sport=g0['sport'], res=g0['res'], books=len(lines), **{k: v for k, v in F.items()}))
        if n % 50 == 0: print(n, 'games', flush=True)
    pickle.dump(out, open(OUT, 'wb'), protocol=4)
    print('written', len(out), 'games ->', OUT)


NAN = float('nan')
isn = lambda x: x != x


def labels(g):
    """For every step: fut = (largest rise, largest fall) of the middle within the next 60 s, cents;
    top10 = inside a still-going 5c+ wave, is the wave's top within the next 10 s (None = not inside such a wave)."""
    m = g['mid']; n = len(m); W = 30   # 60 s
    up = array('f', [NAN] * n); dn = array('f', [NAN] * n)
    for i in range(n):
        if isn(m[i]): continue
        hi = lo = m[i]
        for j in range(i + 1, min(n, i + W + 1)):
            v = m[j]
            if isn(v): continue
            if v > hi: hi = v
            if v < lo: lo = v
        up[i] = hi - m[i]; dn[i] = m[i] - lo
    # waves (zigzag, 3c turn) for the top labels
    top = [None] * n; age = [NAN] * n; size = [NAN] * n
    d = 0; base = None; base_i = 0; ext = None; ext_i = 0
    legs = []
    for i in range(n):
        v = m[i]
        if isn(v): continue
        if base is None: base = ext = v; base_i = ext_i = i; continue
        if d == 0:
            if abs(v - base) >= 3: d = 1 if v > base else -1; ext, ext_i = v, i
            continue
        if (v - ext) * d > 0: ext, ext_i = v, i
        elif abs(v - ext) >= 3:
            legs.append((base_i, ext_i, d)); base, base_i = ext, ext_i; d = -d; ext, ext_i = v, i
    for b_i, e_i, d in legs:
        hi = m[b_i] * d
        for i in range(b_i + 1, e_i + 1):
            if isn(m[i]): continue
            if m[i] * d > hi: hi = m[i] * d
            if abs(m[i] - m[b_i]) < 5: continue
            if m[i] * d < hi - 1: continue          # still going: at (or within 1c of) its high so far
            top[i] = (e_i - i) <= 5; age[i] = (i - b_i) * 2; size[i] = abs(m[i] - m[b_i])
    return up, dn, top, age, size


def test():
    G = pickle.load(open(OUT, 'rb'))
    out = []; P = lambda *a: out.append(' '.join(str(x) for x in a))
    rows = []   # (sport, feature dict, up, dn, dir of the 60s move)
    tops = []
    for g in G:
        up, dn, top, age, size = labels(g)
        m = g['mid']; sp = [x for x in g['spread'] if not isn(x)]; usual = st.median(sp) if sp else 2.0
        ac = [x for x in g['act'] if not isn(x)]; uact = max(1.0, st.median(ac)) if ac else 1.0
        for i in range(30, len(m)):
            if isn(m[i]) or isn(up[i]): continue
            f = dict(bk30=g['bk30'][i], bkn=g['bkn'][i], gap60=g['gap60'][i], imb=g['imb'][i], dimb=g['dimb'][i], spread=g['spread'][i], wide=g['spread'][i] > 1.5 * usual + 0.5,
                     act=g['act'][i] / uact, mom10=m[i] - m[i - 5] if not isn(m[i - 5]) else NAN, mom30=m[i] - m[i - 15] if not isn(m[i - 15]) else NAN)
            rows.append((g['sport'], f, up[i], dn[i]))
            if top[i] is not None:
                mo10 = m[i] - m[i - 5] if not isn(m[i - 5]) else NAN
                d = 1 if m[i] - m[max(0, i - int(age[i] / 2))] > 0 else -1
                tops.append(dict(top=top[i], age=age[i], size=size[i], speed10=abs(mo10) if not isn(mo10) else NAN, gap=g['gap60'][i] * d, imb=g['imb'][i] * d, dimb=g['dimb'][i] * d, wide=g['spread'][i] > 1.5 * usual + 0.5, sport=g['sport']))
    N = len(rows)
    base_up = sum(1 for r in rows if r[2] >= 5 and r[2] > r[3]) / N; base_any = sum(1 for r in rows if max(r[2], r[3]) >= 5) / N
    P('WAVE STARTS: %d moments (every 2 s of %d recorded games, in play). A 5c+ wave starts within the next 60 s at %.1f%% of moments (either way); a 5c+ rise %.1f%%.' % (N, len(G), 100 * base_any, 100 * base_up))
    P('For each signal, the moments sorted by it (signed so + points to home rising): how often a 5c+ wave starts THAT WAY within 60 s, how often the other way, and the middle move 60 s later.\n')
    def table(name, key, cuts):
        P('  %s' % name)
        for lo, hi, lab in cuts:
            X = [r for r in rows if not isn(key(r)) and lo <= key(r) < hi]
            if len(X) < 300: P('    %-26s %8d moments (too few)' % (lab, len(X))); continue
            way = sum(1 for r in X if r[2] >= 5 and r[2] > r[3]) / len(X); other = sum(1 for r in X if r[3] >= 5 and r[3] > r[2]) / len(X)
            P('    %-26s %8d moments (%5.1f%%) | 5c+ up next %5.1f%% | 5c+ down next %5.1f%% | any 5c+ %5.1f%% (x%.1f the usual)' % (lab, len(X), 100.0 * len(X) / N, 100 * way, 100 * other, 100 * (way + other), (way + other) / base_any))
    table('BOOKMAKERS MOVED (middle of their 30-s moves for home, points)', lambda r: r[1]['bk30'], [(-99, -5, 'down 5+ pt'), (-5, -2, 'down 2-5 pt'), (-2, 2, 'about still'), (2, 5, 'up 2-5 pt'), (5, 99, 'up 5+ pt')])
    table('BOOKMAKERS THAT MOVED 3 pt+ in 30 s (for home minus against)', lambda r: r[1]['bkn'], [(-99, -3, '3+ more against'), (-3, -1, '1-2 more against'), (-1, 1, 'none / even'), (1, 3, '1-2 more for'), (3, 99, '3+ more for')])
    table('BOOKMAKERS AHEAD of Polymarket (60 s, points; + = for home)', lambda r: r[1]['gap60'], [(-99, -6, 'behind by 6+ (down)'), (-6, -3, '3-6 down'), (-3, 3, 'level'), (3, 6, '3-6 up'), (6, 99, 'ahead by 6+ (up)')])
    table('ORDER BOOK, best price size (+ = more waiting to buy home)', lambda r: r[1]['imb'], [(-1.01, -0.8, '-1 to -0.8'), (-0.8, -0.4, '-0.8 to -0.4'), (-0.4, 0.4, 'even'), (0.4, 0.8, '0.4 to 0.8'), (0.8, 1.01, '0.8 to 1')])
    table('ORDER BOOK, whole depth (+ = more waiting to buy home)', lambda r: r[1]['dimb'], [(-1.01, -0.6, '-1 to -0.6'), (-0.6, -0.2, '-0.6 to -0.2'), (-0.2, 0.2, 'even'), (0.2, 0.6, '0.2 to 0.6'), (0.6, 1.01, '0.6 to 1')])
    table('SPREAD now (cents)', lambda r: r[1]['spread'], [(0, 1.5, '1c'), (1.5, 2.5, '2c'), (2.5, 4.5, '3-4c'), (4.5, 8.5, '5-8c'), (8.5, 999, '9c+')])
    table('BOOK ACTIVITY (updates in 30 s / this game\'s usual)', lambda r: r[1]['act'], [(0, 0.5, 'quiet (under half)'), (0.5, 1.5, 'usual'), (1.5, 3, '1.5-3 x'), (3, 999, '3 x+')])
    table('POLYMARKET\'S OWN LAST 10 s (cents)', lambda r: r[1]['mom10'], [(-99, -3, 'fell 3c+'), (-3, -1, 'fell 1-3c'), (-1, 1, 'still'), (1, 3, 'rose 1-3c'), (3, 99, 'rose 3c+')])
    # combinations: the strongest the bot could know
    P('\n  COMBINED: bookmakers ahead for home by 6+ AND 3+ bookmakers moved for home in 30 s')
    for lab, fn in (('ahead 6+ & 3+ books for', lambda f: f['gap60'] >= 6 and f['bkn'] >= 3), ('behind 6+ & 3+ books against', lambda f: f['gap60'] <= -6 and f['bkn'] <= -3),
                    ('ahead 3+ & book buy-heavy (imb 0.4+)', lambda f: f['gap60'] >= 3 and f['imb'] >= 0.4), ('book buy-heavy 0.8+ & depth 0.6+', lambda f: f['imb'] >= 0.8 and f['dimb'] >= 0.6),
                    ('spread wide & bookmakers ahead 6+ either way', lambda f: f['wide'] and abs(f['gap60']) >= 6)):
        X = [r for r in rows if fn(r[1])]
        if len(X) < 50: P('    %-44s %6d moments (too few)' % (lab, len(X))); continue
        way = sum(1 for r in X if r[2] >= 5 and r[2] > r[3]) / len(X); other = sum(1 for r in X if r[3] >= 5 and r[3] > r[2]) / len(X)
        P('    %-44s %6d moments | 5c+ up next %5.1f%% | 5c+ down next %5.1f%% | any %5.1f%% (x%.1f)' % (lab, len(X), 100 * way, 100 * other, 100 * (way + other), (way + other) / base_any))
    # warning: of the 5c+ waves, how many had a signal in the 30 s before
    P('\nWARNING BEFORE A WAVE: of all 5c+ wave starts (60-s windows), the share that had each signal pointing the right way at that moment')
    starts = [r for r in rows if max(r[2], r[3]) >= 5]
    for lab, fn in (('bookmakers ahead 3+ pt the wave\'s way', lambda r, d: r[1]['gap60'] * d >= 3), ('bookmakers ahead 6+ pt', lambda r, d: r[1]['gap60'] * d >= 6),
                    ('2+ bookmakers moved 3 pt the wave\'s way (30 s)', lambda r, d: r[1]['bkn'] * d >= 2), ('book imbalance 0.4+ the wave\'s way', lambda r, d: r[1]['imb'] * d >= 0.4),
                    ('spread wide', lambda r, d: r[1]['wide'])):
        hit = sum(1 for r in starts if fn(r, 1 if r[2] > r[3] else -1))
        P('    %-48s %5.1f%% of %d' % (lab, 100.0 * hit / len(starts), len(starts)))
    # tops
    P('\nWAVE TOPS: %d moments inside a still-going 5c+ wave; the top comes within 10 s at %.1f%% of them.' % (len(tops), 100.0 * sum(1 for t in tops if t['top']) / max(1, len(tops))))
    bt = sum(1 for t in tops if t['top']) / max(1, len(tops))
    def ttable(name, key, cuts):
        P('  %s' % name)
        for lo, hi, lab in cuts:
            X = [t for t in tops if not isn(key(t)) and lo <= key(t) < hi]
            if len(X) < 200: P('    %-30s %7d (too few)' % (lab, len(X))); continue
            r = sum(1 for t in X if t['top']) / len(X)
            P('    %-30s %7d moments | top within 10 s %5.1f%% (x%.2f the usual)' % (lab, len(X), 100 * r, r / bt))
    ttable('HOW LONG THE WAVE HAS RUN (s)', lambda t: t['age'], [(0, 10, 'under 10 s'), (10, 30, '10-30 s'), (30, 60, '30-60 s'), (60, 180, '1-3 min'), (180, 1e9, '3 min+')])
    ttable('HOW BIG IT IS SO FAR (c)', lambda t: t['size'], [(5, 8, '5-8c'), (8, 12, '8-12c'), (12, 20, '12-20c'), (20, 999, '20c+')])
    ttable('SPEED IN THE LAST 10 s (c)', lambda t: t['speed10'], [(0, 0.5, 'stalled'), (0.5, 2, 'slow 0.5-2c'), (2, 5, '2-5c'), (5, 999, 'fast 5c+')])
    ttable('BOOKMAKERS vs POLYMARKET (+ = bookmakers further the wave\'s way, still to catch up)', lambda t: t['gap'], [(-99, -3, 'Polymarket past them 3+'), (-3, 3, 'level'), (3, 6, 'bookmakers 3-6 ahead'), (6, 99, 'bookmakers 6+ ahead')])
    ttable('ORDER BOOK (+ = more waiting the wave\'s way)', lambda t: t['imb'], [(-1.01, -0.4, 'against -1 to -0.4'), (-0.4, 0.4, 'even'), (0.4, 1.01, 'with 0.4 to 1')])
    ttable('SPREAD', lambda t: 1 if t['wide'] else 0, [(0, 0.5, 'normal'), (0.5, 1.5, 'wide')])
    txt = '\n'.join(out); print(txt)
    open(os.path.expanduser('~/swifteye/research/paths/wave_predict.txt'), 'w').write(txt + '\n')


if __name__ == '__main__':
    {'build': build, 'test': test}[sys.argv[1] if len(sys.argv) > 1 else 'test']()
