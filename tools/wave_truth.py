"""The owner's hand-marked waves as the reference (Wave Marker page, 21 Nov 2026).

Marks: ~/swifteye/research/wave_marks/t<trade>.json (from the artifact's database, collection `marks`).
Lines: Documents/dev/swifteye-waves/marker-data.json (exactly what the owner saw: change points per line).

usage: wave_truth.py clean       how far each marked endpoint is from the real turning point (fat finger), after snapping
       wave_truth.py pairs       the owner's blue waves paired with the owner's orange waves: who starts first, sizes
       wave_truth.py detector    old detector and new candidates scored against the marks
       wave_truth.py providers   per chart, every bookmaker line scored against the owner's Polymarket waves
"""
import sys, os, json, glob, statistics as st, collections

DATA = os.path.expanduser('~/Documents/dev/swifteye-waves/marker-data.json')
MARKS = os.path.expanduser('~/swifteye/research/wave_marks')
SNAP_S = 15          # a finger is this many seconds off at most
MIN_C = 2.0          # waves smaller than this are not paired


def load():
    D = json.load(open(DATA)); T = {t['id']: t for t in D['trades']}
    M = {}
    for f in glob.glob(MARKS + '/t*.json'):
        m = json.load(open(f))
        if m.get('waves'): M[m['trade']] = m
    return T, M


_arr = {}
def arr(t, k):
    key = (t['id'], k)
    if key not in _arr:
        a = [None] * t['n']; v = None; j = 0; pts = t['series'][k]['pts']
        for i in range(t['n']):
            while j < len(pts) and pts[j][0] <= i: v = pts[j][1]; j += 1
            a[i] = v
        _arr[key] = a
    return _arr[key]


def snap(x, i, want, lo, hi):
    """The real turning point near second i: the extreme (`want` = 'min' or 'max') of the line within [lo, hi],
    at the moment closest to i."""
    lo, hi = max(0, lo), min(len(x) - 1, hi)
    c = [(x[k], k) for k in range(lo, hi + 1) if x[k] is not None]
    if not c: return i, None
    best = min(v for v, _ in c) if want == 'min' else max(v for v, _ in c)
    ks = [k for v, k in c if abs(v - best) < 1e-9]
    k = min(ks, key=lambda k: abs(k - i))
    return k, best


def cleaned(t, k, waves):
    """The owner's waves on one line with each endpoint moved to the real turning point within SNAP_S seconds."""
    x = arr(t, k); out = []
    for w in sorted(waves, key=lambda w: (w['a'], w['b'])):
        a, b = w['a'], w['b']; mid = (a + b) // 2
        if w['type'] == 'flat' or b - a < 2:
            va = x[min(a, len(x) - 1)]; vb = x[min(b, len(x) - 1)]
            out.append(dict(w, a2=a, b2=b, va2=va if va is not None else w['va'], vb2=vb if vb is not None else w['vb'])); continue
        up = w['type'] == 'up'
        a2, va2 = snap(x, a, 'min' if up else 'max', a - SNAP_S, min(a + SNAP_S, mid))
        b2, vb2 = snap(x, b, 'max' if up else 'min', max(b - SNAP_S, mid + 1), b + SNAP_S)
        if va2 is None or vb2 is None or b2 <= a2: a2, b2, va2, vb2 = a, b, w['va'], w['vb']
        out.append(dict(w, a2=a2, b2=b2, va2=va2, vb2=vb2))
    return out


def by_line(m):
    d = collections.defaultdict(list)
    for w in m['waves']: d[w['k']].append(w)
    return d


def cmd_clean(T, M):
    ds, dc, n = [], [], 0
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            for w in cleaned(t, k, ws):
                if w['type'] == 'flat': continue
                n += 1; ds += [abs(w['a2'] - w['a']), abs(w['b2'] - w['b'])]; dc += [abs(w['va2'] - w['va']), abs(w['vb2'] - w['vb'])]
    print('waves cleaned: %d | endpoint moved: median %d s, 90%% within %d s | price changed: median %.1fc, 90%% within %.1fc; endpoints moved 3c+: %d of %d' % (
        n, st.median(ds), sorted(ds)[int(len(ds) * 0.9)], st.median(dc), sorted(dc)[int(len(dc) * 0.9)], sum(1 for x in dc if x >= 3), len(dc)))


def pair(P, B, slack=45):
    """One-to-one: each wave of P with the wave of B the same way that overlaps it most in time (ends stretched by slack)."""
    cand = []
    for i, p in enumerate(P):
        for j, b in enumerate(B):
            if p['dir'] != b['dir']: continue
            ov = min(p['b'] + slack, b['b'] + slack) - max(p['a'] - slack, b['a'] - slack)
            if ov <= 0: continue
            cand.append((ov / (min(p['b'] - p['a'], b['b'] - b['a']) + 2 * slack), i, j))
    usedp, usedb, out = set(), set(), []
    for s, i, j in sorted(cand, reverse=True):
        if i in usedp or j in usedb: continue
        usedp.add(i); usedb.add(j); out.append((P[i], B[j]))
    return out


def simple(ws, key='2'):
    out = []
    for w in ws:
        a, b, va, vb = (w['a2'], w['b2'], w['va2'], w['vb2']) if key == '2' else (w['a'], w['b'], w['va'], w['vb'])
        d = vb - va
        if w.get('type') == 'flat' or abs(d) < MIN_C: continue
        out.append(dict(a=a, b=b, va=va, vb=vb, size=d, dir=1 if d > 0 else -1))
    return out


def cmd_pairs(T, M):
    allp = []; np = nb = 0
    print('%-5s %-10s %-22s %5s %5s %6s  %s' % ('trade', 'sport', 'orange line', 'blue', 'orng', 'paired', 'median: blue starts before orange (s) / blue ends after orange (s) / blue size against orange'))
    for tid, m in sorted(M.items()):
        t = T[tid]; L = by_line(m)
        if 'pm' not in L: continue
        others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k]))
        P = simple(cleaned(t, 'pm', L['pm'])); B = simple(cleaned(t, k, L[k]))
        # only where both lines are marked
        lo = max(min(w['a'] for w in L['pm']), min(w['a'] for w in L[k])); hi = min(max(w['b'] for w in L['pm']), max(w['b'] for w in L[k]))
        P = [p for p in P if p['b'] > lo and p['a'] < hi]; B = [b for b in B if b['b'] > lo and b['a'] < hi]
        pr = pair(P, B); np += len(P); nb += len(B)
        for p, b in pr: allp.append((tid, t['sport'], p, b))
        if pr:
            print('#%-4d %-10s %-22s %5d %5d %6d  %+5d / %+5d / %.2f' % (tid, t['sport'], t['series'][k]['name'][:22], len(P), len(B), len(pr), st.median([b['a'] - p['a'] for p, b in pr]), st.median([p['b'] - b['b'] for p, b in pr]), st.median([abs(p['size'] / b['size']) for p, b in pr])))
        else:
            print('#%-4d %-10s %-22s %5d %5d %6d' % (tid, t['sport'], t['series'][k]['name'][:22], len(P), len(B), 0))
    n = len(allp)
    print('\nyour blue waves of %.0fc+: %d | your orange waves: %d | paired the same way at the same time: %d (%.0f%% of blue, %.0f%% of orange)' % (MIN_C, np, nb, n, 100.0 * n / max(np, 1), 100.0 * n / max(nb, 1)))
    lead = sorted(b['a'] - p['a'] for _, _, p, b in allp); end = sorted(p['b'] - b['b'] for _, _, p, b in allp); rat = sorted(abs(p['size'] / b['size']) for _, _, p, b in allp)
    q = lambda v, f: v[min(len(v) - 1, int(len(v) * f))]
    print('START: Polymarket first in %d, same second (within 3 s) %d, bookmaker first in %d | median %+d s (quarter %+d, three quarters %+d)' % (sum(1 for x in lead if x > 3), sum(1 for x in lead if abs(x) <= 3), sum(1 for x in lead if x < -3), q(lead, .5), q(lead, .25), q(lead, .75)))
    print('END:   Polymarket ends later in %d, together %d, bookmaker ends later in %d | median %+d s (quarter %+d, three quarters %+d)' % (sum(1 for x in end if x > 3), sum(1 for x in end if abs(x) <= 3), sum(1 for x in end if x < -3), q(end, .5), q(end, .25), q(end, .75)))
    print('SIZE:  Polymarket wave against the bookmaker wave: quarter %.2f, median %.2f, three quarters %.2f' % (q(rat, .25), q(rat, .5), q(rat, .75)))
    bs = collections.defaultdict(list)
    for tid, sp, p, b in allp: bs[sp].append((b['a'] - p['a'], abs(p['size'] / b['size'])))
    for sp, v in sorted(bs.items(), key=lambda x: -len(x[1])):
        print('   %-10s pairs %2d | Polymarket starts first in %2d, bookmaker first in %2d | median %+d s | size %.2f' % (sp, len(v), sum(1 for x in v if x[0] > 3), sum(1 for x in v if x[0] < -3), st.median(x[0] for x in v), st.median(x[1] for x in v)))
    return allp


# ---------------------------------------------------------------- detectors
def zigzag(x, th):
    idx = [i for i, v in enumerate(x) if v is not None]
    if len(idx) < 3: return []
    piv = [(idx[0], x[idx[0]])]; d = 0; ei, ev = idx[0], x[idx[0]]
    for i in idx[1:]:
        v = x[i]
        if d == 0:
            if v - piv[0][1] >= th: d = 1; ei, ev = i, v
            elif piv[0][1] - v >= th: d = -1; ei, ev = i, v
            continue
        if d > 0:
            if v > ev: ei, ev = i, v
            elif ev - v >= th: piv.append((ei, ev)); d = -1; ei, ev = i, v
        else:
            if v < ev: ei, ev = i, v
            elif v - ev >= th: piv.append((ei, ev)); d = 1; ei, ev = i, v
    if d != 0: piv.append((ei, ev))
    return [dict(a=a, va=va, b=b, vb=vb) for (a, va), (b, vb) in zip(piv, piv[1:]) if b > a]


def strokes(x, eps, flat=1.0, rel=0.0):
    """Straight strokes, the way a person draws them: keep splitting a stretch at the point where the line is
    furthest (in cents) from the straight stroke across it, until nothing is further than `eps` (or `rel` x the
    stroke's own size, if larger). Gaps with no price end a stroke."""
    out = []
    def rec(i0, i1):
        v0, v1 = x[i0], x[i1]
        if i1 - i0 < 2: out.append((i0, i1)); return
        worst, wi = 0.0, None
        for i in range(i0 + 1, i1):
            dev = abs(x[i] - (v0 + (v1 - v0) * (i - i0) / (i1 - i0)))
            if dev > worst + 1e-9: worst, wi = dev, i
        if wi is not None and worst > max(eps, rel * abs(v1 - v0)):
            # for a step line, the corner is where the value last held before the jump or first arrived after it
            rec(i0, wi); rec(wi, i1)
        else: out.append((i0, i1))
    i = 0; n = len(x)
    while i < n:
        if x[i] is None: i += 1; continue
        j = i
        while j + 1 < n and x[j + 1] is not None: j += 1
        if j > i: rec(i, j)
        i = j + 1
    segs = [dict(a=a, va=x[a], b=b, vb=x[b]) for a, b in out if b > a]
    # join neighbours that go the same way at about the same speed, and neighbouring flats
    res = []
    for s in segs:
        s['type'] = 'flat' if abs(s['vb'] - s['va']) < flat else ('up' if s['vb'] > s['va'] else 'down')
        if res and res[-1]['b'] == s['a'] and res[-1]['type'] == s['type'] == 'flat':
            res[-1]['b'], res[-1]['vb'] = s['b'], s['vb']
        else: res.append(s)
    return res


def label(ws, n, key=''):
    lab = [None] * n
    for w in ws:
        a, b = w['a' + key], w['b' + key]; va, vb = w['va' + key], w['vb' + key]
        ty = w.get('type') or ('flat' if abs(vb - va) < 1.0 else ('up' if vb > va else 'down'))
        for i in range(max(0, a), min(n, b + 1)): lab[i] = ty
    return lab


def bounds(ws, key=''):
    b = set()
    for w in ws: b.add(w['a' + key]); b.add(w['b' + key])
    return sorted(b)


def score(t, k, user, det, tol=20):
    """Agreement of a detector's waves with the owner's on one line: share of seconds with the same direction, and
    the owner's wave ends found within `tol` s (recall) / detector's wave ends that are the owner's (precision)."""
    n = t['n']; U = label(user, n, '2'); Dl = label(det, n)
    span = [i for i in range(n) if U[i] is not None]
    same = sum(1 for i in span if U[i] == Dl[i])
    ub = bounds(user, '2'); lo, hi = (min(span), max(span)) if span else (0, 0)
    db = [x for x in bounds(det) if lo - tol <= x <= hi + tol]
    hit = sum(1 for u in ub if any(abs(u - d) <= tol for d in db)); ok = sum(1 for d in db if any(abs(u - d) <= tol for u in ub))
    return same, len(span), hit, len(ub), ok, len(db), len([w for w in user if w['type'] != 'flat']), len([w for w in det if w['b'] > lo and w['a'] < hi])


def cmd_detector(T, M):
    cands = collections.OrderedDict()
    cands['OLD: turn after 3c (Polymarket) / 4c (bookmakers)'] = lambda x, pm: zigzag(x, 3.0 if pm else 4.0)
    for th in (2.0, 5.0, 7.0): cands['turn after %.0fc' % th] = (lambda th: lambda x, pm: zigzag(x, th))(th)
    for eps in (1.5, 2.0, 2.5, 3.0, 4.0, 5.0): cands['straight strokes, nothing over %.1fc off' % eps] = (lambda e: lambda x, pm: strokes(x, e))(eps)
    for eps, rel in ((2.0, 0.15), (2.0, 0.25), (3.0, 0.15), (3.0, 0.25), (2.5, 0.2)): cands['straight strokes, %.1fc or %d%% of the stroke' % (eps, rel * 100)] = (lambda e, r: lambda x, pm: strokes(x, e, rel=r))(eps, rel)
    lines = []
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            if len(ws) >= 3: lines.append((t, k, cleaned(t, k, ws)))
    print('lines with 3+ waves marked: %d (%d Polymarket, %d bookmaker)' % (len(lines), sum(1 for l in lines if l[1] == 'pm'), sum(1 for l in lines if l[1] != 'pm')))
    print('%-52s %9s %9s %9s %8s %8s   %s' % ('detector', 'same dir', 'ends hit', 'ends true', 'F1', 'waves', 'worst line (same dir)'))
    best = None
    for name, fn in cands.items():
        tot = [0] * 8; worst = (2, '')
        for t, k, user in lines:
            det = fn(arr(t, k), k == 'pm')
            for w in det: w.setdefault('type', 'flat' if abs(w['vb'] - w['va']) < 1.0 else ('up' if w['vb'] > w['va'] else 'down'))
            s = score(t, k, user, det)
            tot = [a + b for a, b in zip(tot, s)]
            r = s[0] / max(s[1], 1)
            if r < worst[0]: worst = (r, '#%d %s' % (t['id'], k))
        rec, prec = tot[2] / max(tot[3], 1), tot[4] / max(tot[5], 1); f1 = 2 * rec * prec / max(rec + prec, 1e-9)
        print('%-52s %8.0f%% %8.0f%% %8.0f%% %8.2f %4d/%-4d  %s %.0f%%' % (name, 100.0 * tot[0] / tot[1], 100 * rec, 100 * prec, f1, tot[7], tot[6], worst[1], 100 * worst[0]))
        if best is None or f1 + tot[0] / tot[1] > best[0]: best = (f1 + tot[0] / tot[1], name)
    print('\nclosest to your marks:', best[1])


def cmd_providers(T, M, eps=2.5, rel=0.2):
    print('per chart: each bookmaker line against YOUR Polymarket waves (new detector on the bookmaker line)')
    for tid, m in sorted(M.items()):
        t = T[tid]; L = by_line(m)
        if 'pm' not in L: continue
        P = simple(cleaned(t, 'pm', L['pm']))
        rows = []
        for k, s_ in t['series'].items():
            if s_['kind'] not in ('book', 'all'): continue
            x = arr(t, k); live = sum(1 for v in x if v is not None) / len(x)
            B = [dict(a=w['a'], b=w['b'], va=w['va'], vb=w['vb'], size=w['vb'] - w['va'], dir=1 if w['vb'] > w['va'] else -1) for w in swings(x, max(1.5, 0.125 * chart_range(t, k))) if w['type'] != 'flat' and abs(w['vb'] - w['va']) >= MIN_C]
            pr = pair(P, B)
            rows.append((len(pr), k, s_['name'], live, len(B), st.median([b['a'] - p['a'] for p, b in pr]) if pr else None))
        rows.sort(reverse=True)
        marked = [k for k in L if k != 'pm']
        print('#%d %s %s v %s: your %d Polymarket waves' % (tid, t['sport'], t['home'], t['away'], len(P)))
        for n, k, name, live, nb, lag in rows[:6]:
            print('     %-26s priced %3.0f%% of the time | its waves %2d | match yours %2d of %d%s%s' % (name[:26], 100 * live, nb, n, len(P), (' | it starts %+d s after Polymarket (median)' % lag) if lag is not None else '', '   <- the line you marked' if k in marked else ''))


def swings(x, th, quiet_s=45, quiet_c=1.0):
    """Waves the way the owner marks them: big turns only (a turn counts after the line comes back `th` cents),
    and a long still stretch before a wave is its own flat piece instead of part of the wave."""
    out = []
    for l in zigzag(x, th):
        a, b, va, vb = l['a'], l['b'], l['va'], l['vb']; sgn = 1 if vb > va else -1
        s = a
        for i in range(a, b + 1):
            if x[i] is None: continue
            if (x[i] - va) * sgn <= quiet_c: s = i
            elif (x[i] - va) * sgn >= 0.5 * abs(vb - va): break
        if s - a >= quiet_s:
            out.append(dict(a=a, va=va, b=s, vb=x[s] if x[s] is not None else va, type='flat'))
            out.append(dict(a=s, va=x[s] if x[s] is not None else va, b=b, vb=vb, type='up' if sgn > 0 else 'down'))
        else:
            out.append(dict(a=a, va=va, b=b, vb=vb, type='up' if sgn > 0 else 'down'))
    return out


def wave_f1(user, det, lo, hi, min_c=3.0, slack=25):
    """Wave by wave: the owner's waves of min_c+ that the detector also has (same way, overlapping in time, size
    within a factor of two), and the detector's waves of min_c+ that the owner also has."""
    U = [dict(a=w['a2'], b=w['b2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in user if w['type'] != 'flat' and abs(w['vb2'] - w['va2']) >= min_c]
    Dw = [dict(a=w['a'], b=w['b'], size=w['vb'] - w['va'], dir=1 if w['vb'] > w['va'] else -1) for w in det if w.get('type') != 'flat' and abs(w['vb'] - w['va']) >= min_c and w['b'] > lo and w['a'] < hi]
    pr = [(u, d) for u, d in pair(U, Dw, slack) if 0.5 <= abs(u['size'] / d['size']) <= 2.0]
    return len(pr), len(U), len(Dw), [abs(u['a'] - d['a']) for u, d in pr], [abs(u['b'] - d['b']) for u, d in pr]


def cmd_detector2(T, M):
    lines = []
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            if len(ws) >= 3:
                c = cleaned(t, k, ws); lines.append((t, k, c, min(w['a'] for w in ws), max(w['b'] for w in ws)))
    cands = collections.OrderedDict()
    cands['OLD turn after 3c / 4c'] = lambda x, pm: zigzag(x, 3.0 if pm else 4.0)
    for tp in (2.0, 3.0, 4.0, 5.0, 6.0):
        for tb in (3.0, 4.0, 5.0, 6.0, 8.0):
            cands['turn after %.0fc Polymarket / %.0fc bookmakers, still stretches cut' % (tp, tb)] = (lambda tp, tb: lambda x, pm: swings(x, tp if pm else tb))(tp, tb)
    for eps in (3.0, 5.0): cands['straight strokes %.0fc' % eps] = (lambda e: lambda x, pm: strokes(x, e))(eps)
    rows = []
    for name, fn in cands.items():
        res = {'pm': [0, 0, 0, [], []], 'bk': [0, 0, 0, [], []]}
        for t, k, user, lo, hi in lines:
            det = fn(arr(t, k), k == 'pm'); r = wave_f1(user, det, lo, hi); g = res['pm' if k == 'pm' else 'bk']
            g[0] += r[0]; g[1] += r[1]; g[2] += r[2]; g[3] += r[3]; g[4] += r[4]
        def f(g):
            rec, prec = g[0] / max(g[1], 1), g[0] / max(g[2], 1); return rec, prec, 2 * rec * prec / max(rec + prec, 1e-9)
        rows.append((name, f(res['pm']), f(res['bk']), res))
    print('%-66s | %-27s | %-27s' % ('detector', 'Polymarket: found/extra/F1', 'bookmakers: found/extra/F1'))
    for name, a, b, res in rows:
        print('%-66s | %3.0f%% of yours, %3.0f%% true %.2f | %3.0f%% of yours, %3.0f%% true %.2f' % (name, 100 * a[0], 100 * a[1], a[2], 100 * b[0], 100 * b[1], b[2]))
    bp = max(rows[1:26], key=lambda r: r[1][2]); bb = max(rows[1:26], key=lambda r: r[2][2])
    print('\nbest for Polymarket:', bp[0].split(',')[0], '(F1 %.2f)' % bp[1][2], '| best for bookmakers:', bb[0], '(F1 %.2f)' % bb[2][2])
    for nm, r in (('Polymarket', bp), ('bookmakers', bb)):
        g = r[3]['pm' if nm == 'Polymarket' else 'bk']
        if g[3]: print('  %s: matched waves start within a median %d s of yours and end within %d s' % (nm, st.median(g[3]), st.median(g[4])))


def chart_range(t, k):
    """What the eye has for scale: the height of the chart the owner saw (Polymarket, all bookmakers and the line)."""
    v = [x for kk in ('pm', 'pm_buy', 'all', k) for x in arr(t, kk) if x is not None]
    return (max(v) - min(v)) if v else 1.0


def line_range(t, k):
    v = [x for x in arr(t, k) if x is not None]
    return (max(v) - min(v)) if v else 1.0


def cmd_detector3(T, M):
    lines = []
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            if len(ws) >= 3:
                c = cleaned(t, k, ws); lines.append((t, k, c, min(w['a'] for w in ws), max(w['b'] for w in ws)))
    cands = collections.OrderedDict()
    cands['OLD: turn after 3c / 4c'] = lambda t, k: zigzag(arr(t, k), 3.0 if k == 'pm' else 4.0)
    cands['fixed: 5c Polymarket / 4c bookmakers'] = lambda t, k: swings(arr(t, k), 5.0 if k == 'pm' else 4.0)
    for r in (0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30):
        cands['turn after %4.1f%% of the chart height' % (r * 100)] = (lambda r: lambda t, k: swings(arr(t, k), max(1.5, r * chart_range(t, k))))(r)
        cands['turn after %4.1f%% of the line\'s own range' % (r * 100)] = (lambda r: lambda t, k: swings(arr(t, k), max(1.5, r * line_range(t, k))))(r)
    out = []
    for name, fn in cands.items():
        g = [0, 0, 0, [], []]; per = {}
        for t, k, user, lo, hi in lines:
            for mc in (3.0,):
                r = wave_f1(user, fn(t, k), lo, hi, min_c=max(2.0, 0.08 * chart_range(t, k)))
            g[0] += r[0]; g[1] += r[1]; g[2] += r[2]; g[3] += r[3]; g[4] += r[4]
            per[(t['id'], k)] = (r[0], r[1], r[2])
        rec, prec = g[0] / max(g[1], 1), g[0] / max(g[2], 1); f1 = 2 * rec * prec / max(rec + prec, 1e-9)
        out.append((f1, name, rec, prec, g, per))
    print('%-46s %14s %14s %6s' % ('detector', 'your waves found', 'its waves yours', 'F1'))
    for f1, name, rec, prec, g, per in out: print('%-46s %13.0f%% %13.0f%% %7.2f   (%d of your %d; it made %d)' % (name, 100 * rec, 100 * prec, f1, g[0], g[1], g[2]))
    best = max(out[2:], key=lambda r: r[0])
    print('\nclosest to your marks: %s | its matched waves start a median %d s from yours and end %d s from yours' % (best[1], st.median(best[4][3]), st.median(best[4][4])))
    # leave one chart out: pick the setting on the other charts, score it on the one left out
    ids = sorted(set(t['id'] for t, *_ in lines)); tot = [0, 0, 0]
    for hold in ids:
        def f1_on(per, keep):
            a = sum(v[0] for kk, v in per.items() if keep(kk[0])); b = sum(v[1] for kk, v in per.items() if keep(kk[0])); c = sum(v[2] for kk, v in per.items() if keep(kk[0]))
            rec, prec = a / max(b, 1), a / max(c, 1); return 2 * rec * prec / max(rec + prec, 1e-9), a, b, c
        pick = max(out[2:], key=lambda r: f1_on(r[5], lambda i: i != hold)[0])
        _, a, b, c = f1_on(pick[5], lambda i: i == hold); tot[0] += a; tot[1] += b; tot[2] += c
    rec, prec = tot[0] / max(tot[1], 1), tot[0] / max(tot[2], 1)
    print('checked on charts it was not tuned on (one left out at a time): %d%% of your waves found, %d%% of its waves are yours, F1 %.2f' % (100 * rec, 100 * prec, 2 * rec * prec / max(rec + prec, 1e-9)))
    print('by chart, closest setting (yours found / yours / it made):', ' '.join('#%d:%s %d/%d/%d' % (kk[0], 'pm' if kk[1] == 'pm' else 'bk', v[0], v[1], v[2]) for kk, v in sorted(best[5].items())))


def waves2(x, th, knee=0.25, flat_s=40, min_part=0.2):
    """Detector v2. Big turns first (a turn counts after the line comes back `th`), then each swing is cut where its
    speed changes: at the point furthest from the straight stroke across it, if that is more than `knee` of the
    swing. Still stretches of flat_s+ seconds become flat pieces; slivers under min_part of the swing rejoin."""
    out = []
    for l in zigzag(x, th):
        a, b = l['a'], l['b']; size = abs(l['vb'] - l['va']); sgn = 1 if l['vb'] > l['va'] else -1
        cuts = [a, b]
        def rec(i0, i1, depth):
            if i1 - i0 < 6 or depth > 3: return
            v0, v1 = x[i0], x[i1]; worst, wi = 0.0, None
            for i in range(i0 + 1, i1):
                if x[i] is None: continue
                dev = abs(x[i] - (v0 + (v1 - v0) * (i - i0) / (i1 - i0)))
                if dev > worst + 1e-9: worst, wi = dev, i
            if wi is not None and worst >= max(1.5, knee * size):
                cuts.append(wi); rec(i0, wi, depth + 1); rec(wi, i1, depth + 1)
        rec(a, b, 0)
        cuts = sorted(set(cuts)); parts = []
        for i0, i1 in zip(cuts, cuts[1:]):
            v0, v1 = x[i0], x[i1]
            if v0 is None or v1 is None: continue
            d = (v1 - v0) * sgn
            ty = 'flat' if (abs(v1 - v0) < 1.0 or (d < min_part * size and i1 - i0 >= flat_s)) else ('up' if v1 > v0 else 'down')
            parts.append(dict(a=i0, va=v0, b=i1, vb=v1, type=ty))
        # short slivers and same-kind neighbours rejoin
        merged = []
        for p_ in parts:
            if merged and (merged[-1]['type'] == p_['type'] == 'flat' or (p_['type'] != 'flat' and merged[-1]['type'] == p_['type'] and (abs(p_['vb'] - p_['va']) < min_part * size or abs(merged[-1]['vb'] - merged[-1]['va']) < min_part * size))):
                merged[-1]['b'], merged[-1]['vb'] = p_['b'], p_['vb']
            elif merged and p_['type'] == 'flat' and p_['b'] - p_['a'] < flat_s:
                merged[-1]['b'], merged[-1]['vb'] = p_['b'], p_['vb']
            else: merged.append(dict(p_))
        out += merged
    return out


def cmd_detector4(T, M):
    lines = []
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            if len(ws) >= 3:
                c = cleaned(t, k, ws); lines.append((t, k, c, min(w['a'] for w in ws), max(w['b'] for w in ws)))
    cands = collections.OrderedDict()
    cands['OLD: turn after 3c / 4c'] = lambda t, k: zigzag(arr(t, k), 3.0 if k == 'pm' else 4.0)
    cands['turns only, 12.5% of the chart height'] = lambda t, k: swings(arr(t, k), max(1.5, 0.125 * chart_range(t, k)))
    for r in (0.10, 0.125, 0.15, 0.20):
        for knee in (0.15, 0.25, 0.35):
            cands['v2: turns %4.1f%% of chart, speed cut at %d%% of the swing' % (r * 100, knee * 100)] = (lambda r, kn: lambda t, k: waves2(arr(t, k), max(1.5, r * chart_range(t, k)), kn))(r, knee)
    out = []
    for name, fn in cands.items():
        g = [0, 0, 0, [], []]; per = {}
        for t, k, user, lo, hi in lines:
            r = wave_f1(user, fn(t, k), lo, hi, min_c=max(2.0, 0.08 * chart_range(t, k)))
            g[0] += r[0]; g[1] += r[1]; g[2] += r[2]; g[3] += r[3]; g[4] += r[4]; per[(t['id'], k)] = (r[0], r[1], r[2])
        rec, prec = g[0] / max(g[1], 1), g[0] / max(g[2], 1); out.append((2 * rec * prec / max(rec + prec, 1e-9), name, rec, prec, g, per))
    print('%-58s %14s %14s %6s' % ('detector', 'your waves found', 'its waves yours', 'F1'))
    for f1, name, rec, prec, g, per in out: print('%-58s %13.0f%% %13.0f%% %7.2f   (%d of your %d; it made %d)' % (name, 100 * rec, 100 * prec, f1, g[0], g[1], g[2]))
    best = max(out[2:], key=lambda r: r[0]); ids = sorted(set(t['id'] for t, *_ in lines)); tot = [0, 0, 0]
    for hold in ids:
        def sc(per, keep):
            a = sum(v[0] for kk, v in per.items() if keep(kk[0])); b = sum(v[1] for kk, v in per.items() if keep(kk[0])); c = sum(v[2] for kk, v in per.items() if keep(kk[0]))
            rec, prec = a / max(b, 1), a / max(c, 1); return 2 * rec * prec / max(rec + prec, 1e-9), a, b, c
        pick = max(out[2:], key=lambda r: sc(r[5], lambda i: i != hold)[0]); _, a, b, c = sc(pick[5], lambda i: i == hold); tot[0] += a; tot[1] += b; tot[2] += c
    rec, prec = tot[0] / max(tot[1], 1), tot[0] / max(tot[2], 1)
    print('\nclosest: %s | starts a median %d s from yours, ends %d s from yours' % (best[1], st.median(best[4][3]), st.median(best[4][4])))
    print('on charts it was not tuned on (one left out at a time): %d%% of your waves found, %d%% of its waves are yours, F1 %.2f' % (100 * rec, 100 * prec, 2 * rec * prec / max(rec + prec, 1e-9)))
    print('by chart (yours found / yours / it made):', ' '.join('#%d:%s %d/%d/%d' % (kk[0], 'pm' if kk[1] == 'pm' else 'bk', v[0], v[1], v[2]) for kk, v in sorted(best[5].items())))


# ---------------------------------------------------------------- detector v3: the best set of straight strokes
def polyline(x, lam, grid=3, span=900):
    """The straight strokes a person would draw: of ALL ways to cover the line with strokes that start and end on the
    line itself, the one with the least total miss (squared cents, summed over the line) plus `lam` for every stroke
    used. A bigger `lam` means fewer, longer strokes. Gaps with no price end a stroke. Returns strokes in seconds."""
    out = []; n = len(x); i = 0
    while i < n:
        if x[i] is None: i += 1; continue
        j = i
        while j + 1 < n and x[j + 1] is not None: j += 1
        ks = list(range(i, j + 1, grid))
        if ks[-1] != j: ks.append(j)
        ys = [x[k] for k in ks]; m = len(ys)
        if m >= 2:
            P1 = [0.0] * (m + 1); P2 = [0.0] * (m + 1); P3 = [0.0] * (m + 1)
            for k in range(m): P1[k + 1] = P1[k] + ys[k]; P2[k + 1] = P2[k] + ys[k] * ys[k]; P3[k + 1] = P3[k] + k * ys[k]
            best = [0.0] + [1e18] * (m - 1); prev = [0] * m; W_ = max(2, span // grid)
            for b in range(1, m):
                lo = max(0, b - W_); yb = ys[b]; bb = 1e18; bi = lo
                for a in range(lo, b):
                    d = b - a; ya = ys[a]; sl = (yb - ya) / d
                    s1 = P1[b + 1] - P1[a]; s2 = P2[b + 1] - P2[a]; sk = (P3[b + 1] - P3[a]) - a * s1
                    sse = s2 - 2 * ya * s1 + (d + 1) * ya * ya - 2 * sl * sk + 2 * sl * ya * d * (d + 1) / 2 + sl * sl * d * (d + 1) * (2 * d + 1) / 6
                    c = best[a] + sse + lam
                    if c < bb: bb = c; bi = a
                best[b] = bb; prev[b] = bi
            kn = [m - 1]
            while kn[-1] > 0: kn.append(prev[kn[-1]])
            kn.reverse()
            for a, b in zip(kn, kn[1:]): out.append(dict(a=ks[a], va=ys[a], b=ks[b], vb=ys[b]))
        i = j + 1
    return out


def waves3(x, height, L=0.03, flat=0.05, grid=3):
    """Detector v3: the best straight strokes, named up / down / flat (flat = under `flat` of the chart's height),
    neighbours of the same name going at nearly the same speed joined."""
    st_ = polyline(x, L * height * height, grid); fl = max(1.0, flat * height); out = []
    for w in st_:
        d = w['vb'] - w['va']; w['type'] = 'flat' if abs(d) < fl else ('up' if d > 0 else 'down')
        if out and out[-1]['b'] == w['a'] and out[-1]['type'] == w['type']:
            p_ = out[-1]; s1 = (p_['vb'] - p_['va']) / max(p_['b'] - p_['a'], 1); s2 = d / max(w['b'] - w['a'], 1)
            if w['type'] == 'flat' or (min(abs(s1), abs(s2)) / max(abs(s1), abs(s2), 1e-9) > 0.5):
                p_['b'], p_['vb'] = w['b'], w['vb']; continue
        out.append(w)
    return out


def cmd_detector5(T, M):
    lines = []
    for tid, m in sorted(M.items()):
        t = T[tid]
        for k, ws in by_line(m).items():
            if len(ws) >= 3:
                c = cleaned(t, k, ws); lines.append((t, k, c, min(w['a'] for w in ws), max(w['b'] for w in ws)))
    cands = collections.OrderedDict()
    cands['OLD: turn after 3c / 4c'] = lambda t, k: zigzag(arr(t, k), 3.0 if k == 'pm' else 4.0)
    cands['v2: turn at 12.5% of the chart height'] = lambda t, k: swings(arr(t, k), max(1.5, 0.125 * chart_range(t, k)))
    for L in (0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2):
        for fl in (0.04, 0.08):
            cands['v3: best strokes, cost %.2f, flat under %d%%' % (L, fl * 100)] = (lambda L, fl: lambda t, k: waves3(arr(t, k), chart_range(t, k), L, fl))(L, fl)
    out = []
    for name, fn in cands.items():
        g = [0, 0, 0, [], []]; per = {}; same = tot = hit = nu = ok = nd = 0
        for t, k, user, lo, hi in lines:
            det = fn(t, k)
            for w in det: w.setdefault('type', 'flat' if abs(w['vb'] - w['va']) < 1.0 else ('up' if w['vb'] > w['va'] else 'down'))
            r = wave_f1(user, det, lo, hi, min_c=max(2.0, 0.08 * chart_range(t, k)))
            g[0] += r[0]; g[1] += r[1]; g[2] += r[2]; g[3] += r[3]; g[4] += r[4]; per[(t['id'], k)] = (r[0], r[1], r[2])
            sc = score(t, k, user, det); same += sc[0]; tot += sc[1]; hit += sc[2]; nu += sc[3]; ok += sc[4]; nd += sc[5]
        rec, prec = g[0] / max(g[1], 1), g[0] / max(g[2], 1); f1 = 2 * rec * prec / max(rec + prec, 1e-9)
        out.append((f1, name, rec, prec, g, per, same / max(tot, 1), hit / max(nu, 1), ok / max(nd, 1)))
    print('%-46s | %-34s | %-22s | %s' % ('detector', 'wave by wave: yours found / its own true / F1', 'same direction, second by second', 'wave ends: yours hit / its own true'))
    for f1, name, rec, prec, g, per, sd, bh, bp in out:
        print('%-46s | %3.0f%% / %3.0f%% / %.2f  (%3d of %3d; made %3d) | %3.0f%%                  | %3.0f%% / %3.0f%%' % (name, 100 * rec, 100 * prec, f1, g[0], g[1], g[2], 100 * sd, 100 * bh, 100 * bp))
    best = max(out[2:], key=lambda r: r[0] + r[6]); ids = sorted(set(t['id'] for t, *_ in lines)); tot = [0, 0, 0]
    for hold in ids:
        def sc(per, keep):
            a = sum(v[0] for kk, v in per.items() if keep(kk[0])); b = sum(v[1] for kk, v in per.items() if keep(kk[0])); c = sum(v[2] for kk, v in per.items() if keep(kk[0]))
            rec, prec = a / max(b, 1), a / max(c, 1); return 2 * rec * prec / max(rec + prec, 1e-9), a, b, c
        pick = max(out[2:], key=lambda r: sc(r[5], lambda i: i != hold)[0]); _, a, b, c = sc(pick[5], lambda i: i == hold); tot[0] += a; tot[1] += b; tot[2] += c
    rec, prec = tot[0] / max(tot[1], 1), tot[0] / max(tot[2], 1)
    print('\nclosest to your marks: %s | matched waves start a median %d s and end %d s from yours' % (best[1], st.median(best[4][3]), st.median(best[4][4])))
    print('on charts it was not tuned on (one left out at a time): %d%% of your waves found, %d%% of its waves yours, F1 %.2f' % (100 * rec, 100 * prec, 2 * rec * prec / max(rec + prec, 1e-9)))


# ---------------------------------------------------------------- delay and correlation from the v3 waves
V3 = dict(L=0.08, flat=0.04)
def halfway(x, w):
    sgn = 1 if w['vb'] > w['va'] else -1; need = 0.5 * abs(w['vb'] - w['va'])
    for i in range(max(0, w['a']), min(len(x) - 1, w['b']) + 1):
        if x[i] is not None and (x[i] - w['va']) * sgn >= need - 1e-9: return i
    return (w['a'] + w['b']) // 2


def movers(ws, min_c):
    return [dict(a=w['a'], b=w['b'], va=w['va'], vb=w['vb'], size=w['vb'] - w['va'], dir=1 if w['vb'] > w['va'] else -1) for w in ws if w.get('type') != 'flat' and abs(w['vb'] - w['va']) >= min_c]


def pair3(xp, xb, height, upto=None, slack=45):
    """Waves on both lines (detector v3), paired; each pair's delay = Polymarket's wave halfway minus the bookmaker's
    wave halfway, in seconds (+ = the bookmaker was first). `upto`: only what has ended by that second."""
    n = len(xp) if upto is None else upto
    mc = max(2.0, 0.08 * height)
    P = movers(waves3(xp[:n], height, **V3), mc); B = movers(waves3(xb[:n], height, **V3), mc)
    if upto is not None and upto < len(xp):         # the stroke still being drawn is not a finished wave
        P = [w for w in P if w['b'] < n - 5]; B = [w for w in B if w['b'] < n - 5]
    pr = []
    for p_, b_ in pair(P, B, slack):
        pr.append(dict(p=p_, b=b_, hp=halfway(xp, p_), hb=halfway(xb, b_)))
        pr[-1]['delay'] = pr[-1]['hp'] - pr[-1]['hb']
    return P, B, pr


def scores3(P, B, pr, now=None, half=600.0):
    """CORRELATION = of all the up and down movement on both lines (cents), the share that the other line made too
    (a paired wave counts with the smaller of the two sizes, on both sides). DELAY = the pairs' delays, the middle
    one by size, recent pairs counting more (half every `half` s when `now` is given)."""
    w = lambda t: 1.0 if now is None else 0.5 ** (max(now - t, 0) / half)
    tot = sum(abs(x['size']) * w(x['b']) for x in P) + sum(abs(x['size']) * w(x['b']) for x in B)
    mt = sum(2 * min(abs(q['p']['size']), abs(q['b']['size'])) * w(max(q['p']['b'], q['b']['b'])) for q in pr)
    if tot <= 0: return None
    if pr:
        srt = sorted(pr, key=lambda q: q['delay']); ww = [min(abs(q['p']['size']), abs(q['b']['size'])) * w(max(q['p']['b'], q['b']['b'])) for q in srt]; acc = 0.0; d = srt[-1]['delay']
        for q, x in zip(srt, ww):
            acc += x
            if acc >= sum(ww) / 2: d = q['delay']; break
    else: d = None
    rem = [q['rem'] for q in pr if 'rem' in q]
    return dict(corr=100.0 * mt / tot, delay=d, pairs=len(pr), first=sum(1 for q in pr if q['delay'] > 3), behind=sum(1 for q in pr if q['delay'] < -3), np=len(P), nb=len(B), rem=(st.median(rem) if rem else None))


def cmd_scores3(T, M):
    rows = []
    print('delay: + = the bookmaker line first, - = Polymarket first (seconds, halfway to halfway)')
    print('%-5s %-10s %-14s | %-44s | %-52s' % ('chart', 'sport', 'orange line', 'YOUR MARKS', 'NEW: detector v3 waves, paired'))
    for tid, m in sorted(M.items()):
        t = T[tid]; L = by_line(m); others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k])); xp, xb = arr(t, 'pm'), arr(t, k); H = chart_range(t, k)
        # the owner's own pairs
        mc = max(2.0, 0.08 * H)
        UP = [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in cleaned(t, 'pm', L['pm']) if w['type'] != 'flat' and abs(w['vb2'] - w['va2']) >= MIN_C]
        UB = [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in cleaned(t, k, L[k]) if w['type'] != 'flat' and abs(w['vb2'] - w['va2']) >= MIN_C]
        upr = [dict(p=p_, b=b_, delay=halfway(xp, p_) - halfway(xb, b_)) for p_, b_ in pair(UP, UB)]
        us = scores3(UP, UB, upr)
        P, B, pr = pair3(xp, xb, H); ns = scores3(P, B, pr)
        rows.append((tid, us, ns))
        f = lambda r: '-' if not r else ('%2d pairs, corr %3.0f, delay %s (bk first %d, PM first %d)' % (r['pairs'], r['corr'], ('%+4.0f' % r['delay']) if r['delay'] is not None else '   -', r['first'], r['behind']))
        print('#%-4d %-10s %-14s | %-44s | %-52s' % (tid, t['sport'], t['series'][k]['name'][:14], f(us), f(ns)))
    ok = [(a, b) for _, a, b in rows if a and b and a['pairs'] >= 4 and b['pairs'] >= 2 and a['delay'] is not None and b['delay'] is not None]
    lead = sum(1 for a, b in ok if (a['delay'] > 3 and b['delay'] > 3) or (a['delay'] < -3 and b['delay'] < -3) or (abs(a['delay']) <= 3 and abs(b['delay']) <= 3))
    maj = sum(1 for a, b in ok if (a['first'] > a['behind']) == (b['first'] > b['behind']) or (a['first'] == a['behind']) or (b['first'] == b['behind']))
    print('\ncharts with 4+ of your pairs: %d | new delay names the same leader on %d | same side has more firsts on %d | delay off by a median %.0f s | correlation off from yours by a median %.0f points' % (
        len(ok), lead, maj, st.median([abs(a['delay'] - b['delay']) for a, b in ok]), st.median([abs(a['corr'] - b['corr']) for a, b in ok])))


def burst(x, w):
    """WHEN a wave really happens: the middle of the shortest stretch in which the line covers half of the wave.
    For a bookmaker's step that is the step itself; for a slow drift with one fast part, the fast part."""
    a, b = max(0, w['a']), min(len(x) - 1, w['b']); sgn = 1 if w['vb'] > w['va'] else -1; need = 0.5 * abs(w['vb'] - w['va'])
    v = [(x[i] if x[i] is not None else None) for i in range(a, b + 1)]
    best = None
    for i in range(len(v)):
        if v[i] is None: continue
        for j in range(i, len(v)):
            if v[j] is None: continue
            if (v[j] - v[i]) * sgn >= need - 1e-9:
                if best is None or j - i < best[0]: best = (j - i, (i + j) / 2.0)
                break
        if best is not None and best[0] == 0: break
    return a + (best[1] if best else (b - a) / 2.0)


def pair4(xp, xb, height, upto=None, slack=45):
    """As pair3, with each pair's delay measured between the two waves' fast parts (see burst)."""
    P, B, pr = pair3(xp, xb, height, upto, slack); keep = []
    for q in pr:
        q['tp'], q['tb'] = burst(xp, q['p']), burst(xb, q['b']); q['delay'] = q['tp'] - q['tb']
        if abs(q['delay']) > 90: continue                      # fast parts over 90 s apart: not the same move
        # how much of Polymarket's wave was still to come when the bookmaker made its move
        i = int(min(max(q['tb'], 0), len(xp) - 1)); v = xp[i]; size = q['p']['vb'] - q['p']['va']
        if q['tb'] <= q['p']['a']: q['rem'] = 1.0
        elif q['tb'] >= q['p']['b'] or v is None: q['rem'] = 0.0
        else: q['rem'] = min(max((q['p']['vb'] - v) / size, 0.0), 1.0)
        keep.append(q)
    return P, B, keep


def cmd_scores4(T, M):
    rows = []
    print('delay between the FAST PARTS of paired waves: + = the bookmaker line first, - = Polymarket first (seconds)')
    print('%-5s %-10s %-14s | %-52s | %-52s' % ('chart', 'sport', 'orange line', 'YOUR waves', 'DETECTOR v3 waves'))
    alld = []
    for tid, m in sorted(M.items()):
        t = T[tid]; L = by_line(m); others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k])); xp, xb = arr(t, 'pm'), arr(t, k); H = chart_range(t, k)
        UP = [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in cleaned(t, 'pm', L['pm']) if w['type'] != 'flat' and abs(w['vb2'] - w['va2']) >= MIN_C]
        UB = [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in cleaned(t, k, L[k]) if w['type'] != 'flat' and abs(w['vb2'] - w['va2']) >= MIN_C]
        upr = [dict(p=p_, b=b_, delay=burst(xp, p_) - burst(xb, b_)) for p_, b_ in pair(UP, UB)]
        us = scores3(UP, UB, upr); P, B, pr = pair4(xp, xb, H); ns = scores3(P, B, pr); rows.append((tid, us, ns)); alld += [q['delay'] for q in upr]
        f = lambda r: '-' if not r else ('%2d pairs, corr %3.0f, delay %s (bk first %d, PM first %d)' % (r['pairs'], r['corr'], ('%+4.0f' % r['delay']) if r['delay'] is not None else '   -', r['first'], r['behind']))
        print('#%-4d %-10s %-14s | %-52s | %-52s' % (tid, t['sport'], t['series'][k]['name'][:14], f(us), f(ns)))
    ok = [(a, b) for _, a, b in rows if a and b and a['pairs'] >= 4 and b['pairs'] >= 2 and a['delay'] is not None and b['delay'] is not None]
    lead = sum(1 for a, b in ok if (a['delay'] > 3 and b['delay'] > 3) or (a['delay'] < -3 and b['delay'] < -3) or (abs(a['delay']) <= 3 and abs(b['delay']) <= 3))
    print('\ncharts with 4+ of your pairs: %d | the detector names the same leader as your waves on %d | delay off by a median %.0f s | correlation off by a median %.0f points' % (len(ok), lead, st.median([abs(a['delay'] - b['delay']) for a, b in ok]), st.median([abs(a['corr'] - b['corr']) for a, b in ok])))
    alld.sort(); n = len(alld)
    print('your %d pairs by fast part: Polymarket first %d, bookmaker first %d, within 3 s %d | median %+.0f s (quarter %+.0f, three quarters %+.0f)' % (n, sum(1 for x in alld if x < -3), sum(1 for x in alld if x > 3), sum(1 for x in alld if abs(x) <= 3), alld[n // 2], alld[n // 4], alld[3 * n // 4]))


if __name__ == '__main__':
    T, M = load(); a = sys.argv[1:] or ['pairs']
    {'clean': cmd_clean, 'pairs': cmd_pairs, 'detector': cmd_detector, 'detector2': cmd_detector2, 'detector3': cmd_detector3, 'detector4': cmd_detector4, 'detector5': cmd_detector5, 'scores3': cmd_scores3, 'scores4': cmd_scores4, 'providers': cmd_providers}[a[0]](T, M)
