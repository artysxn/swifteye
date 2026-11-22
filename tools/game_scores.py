"""Two running scores for a game, built only from what has already happened in it (owner, 21 Nov 2026: "tell
correlation throughout a game, tell the delay throughout a game ... a correlation score and a delay score").

  CORRELATION score, 0 to 100: how much of Polymarket's movement and the bookmakers' movement is the same movement,
                     once one line is slid in time to fit the other as well as it can.
  DELAY score, in seconds:     how far the lines have to be slid for that best fit.
                     +8 = the bookmakers are 8 s AHEAD of Polymarket (Polymarket trails: the snipe's case)
                     -8 = Polymarket is 8 s ahead of the bookmakers.

How: every second, each line's change over the last 10 s is compared with the other line's change now and up to two
minutes earlier or later; older seconds count for less (half as much every 10 minutes). Nothing from the future
is used: the scores at a moment come from the game up to that moment. Prices in cents, our side's terms.

usage: game_scores.py marks                 the scores on the owner's hand-marked charts against the marks themselves
       game_scores.py game <trade id>       the scores every few minutes through the hour before that trade
       game_scores.py trades                the scores at each buy the bot made, and how those buys ended
       game_scores.py replay build | test   the same on the replay's moves (Polymarket by its own clock)
"""
import sys, os, json, math, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waves as W

H = 10            # a change is measured over this many seconds
MAXLAG = 120      # lines are slid up to this far apart
STEP = 2
HALF = 600        # seconds for an old second to count half
MOVE_C = 2.0      # a bookmaker change of this size in 10 s is "activity"


class Est:
    """The running estimator. step(bookmaker cents, Polymarket cents) once a second; read() any time."""
    def __init__(self, half=HALF, h=H, maxlag=MAXLAG, step=STEP):
        self.h = h; self.lags = list(range(-maxlag, maxlag + 1, step)); self.lam = 1.0 if not half else 0.5 ** (1.0 / half)
        self.bk = collections.deque(maxlen=h + 1); self.pm = collections.deque(maxlen=h + 1)
        self.db = collections.deque(maxlen=maxlag + 1); self.dp = collections.deque(maxlen=maxlag + 1)
        self.S = [0.0] * len(self.lags); self.Vb = self.Vp = 0.0; self.act = 0.0; self.was = False; self.lb = self.lp = None

    def step(self, bk, pm):
        if bk is None: bk = self.lb          # a suspended bookmaker shows nothing new: its next price counts when it comes
        if pm is None: pm = self.lp
        if bk is None or pm is None: return
        self.lb, self.lp = bk, pm
        self.bk.append(bk); self.pm.append(pm)
        if len(self.bk) <= self.h: return
        dbk, dpm = bk - self.bk[0], pm - self.pm[0]
        self.db.append(dbk); self.dp.append(dpm)
        lam = self.lam; nb, n_p = len(self.db), len(self.dp)
        self.Vb = lam * self.Vb + dbk * dbk; self.Vp = lam * self.Vp + dpm * dpm
        S = self.S
        for j, L in enumerate(self.lags):
            if L >= 0: S[j] = lam * S[j] + (self.db[nb - 1 - L] * dpm if nb > L else 0.0)      # the bookmakers' change L s ago with Polymarket's now
            else: S[j] = lam * S[j] + (dbk * self.dp[n_p - 1 + L] if n_p > -L else 0.0)        # Polymarket's change |L| s ago with the bookmakers' now
        mv = abs(dbk) >= MOVE_C
        self.act = lam * self.act + (1.0 if mv and not self.was else 0.0); self.was = mv

    def read(self):
        if self.Vb <= 1e-9 or self.Vp <= 1e-9: return None
        den = math.sqrt(self.Vb * self.Vp); c = [s / den for s in self.S]
        j = max(range(len(c)), key=lambda i: c[i]); top = c[j]
        if top <= 0: return dict(corr=0.0, delay=0.0, at0=c[len(c) // 2], act=self.act, lean=0.0)
        # the middle of the peak: neighbouring slides that fit nearly as well
        lo = hi = j
        while lo > 0 and c[lo - 1] >= 0.9 * top: lo -= 1
        while hi < len(c) - 1 and c[hi + 1] >= 0.9 * top: hi += 1
        w = [c[i] for i in range(lo, hi + 1)]; delay = sum(self.lags[i] * c[i] for i in range(lo, hi + 1)) / sum(w)
        pos = sum(max(x, 0) for x, L in zip(c, self.lags) if L > 0); neg = sum(max(x, 0) for x, L in zip(c, self.lags) if L < 0)
        return dict(corr=100.0 * min(top, 1.0), delay=delay, at0=100.0 * c[len(c) // 2], act=self.act, lean=(pos - neg) / max(pos + neg, 1e-9))


def run(bk, pm, half=HALF, every=None):
    """Scores at the end of two per-second lines (cents); with `every`, also a reading each `every` seconds."""
    e = Est(half); out = []
    for i in range(len(bk)):
        e.step(bk[i], pm[i])
        if every and i and i % every == 0: out.append((i, e.read()))
    return (e.read(), out) if every else e.read()


def cents(a): return [None if v is None else v * 100.0 for v in a]
def show(r): return 'no movement yet' if not r else 'correlation %3.0f | delay %+5.0f s (%s) | bookmaker moves so far %.1f' % (r['corr'], r['delay'], 'bookmakers ahead' if r['delay'] > 3 else 'Polymarket ahead' if r['delay'] < -3 else 'together', r['act'])


# ---------------------------------------------------------------- against the owner's marks
def cmd_marks():
    import wave_truth as WT
    T, M = WT.load(); rows = []
    def half(x, w):
        sgn = 1 if w['size'] > 0 else -1
        for i in range(max(0, w['a']), min(len(x) - 1, w['b']) + 1):
            if x[i] is not None and (x[i] - w['va']) * sgn >= 0.5 * abs(w['size']) - 1e-9: return i
    def simple2(ws):
        return [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in ws if w.get('type') != 'flat' and abs(w['vb2'] - w['va2']) >= WT.MIN_C]
    print('%-5s %-10s %-14s | %-36s | %-40s | %s' % ('chart', 'sport', 'orange line', 'YOUR MARKS: waves paired, who is first', 'SCORES on the same two lines', 'scores against all bookmakers'))
    for tid, m in sorted(M.items()):
        t = T[tid]; L = WT.by_line(m); others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k]))
        P = simple2(WT.cleaned(t, 'pm', L['pm'])); B = simple2(WT.cleaned(t, k, L[k])); xs, xb = WT.arr(t, 'pm'), WT.arr(t, k)
        lags = []
        for p, b in WT.pair(P, B):
            hp, hb = half(xs, p), half(xb, b)
            if hp is not None and hb is not None: lags.append(hp - hb)         # + = the bookmaker got halfway first
        pr = len(lags) / max(len(B), 1)
        r = run(xb, xs, half=None); ra = run(WT.arr(t, 'all'), xs, half=None)
        rows.append((tid, t['sport'], st.median(lags) if lags else None, pr, r, ra, len(lags)))
        print('#%-4d %-10s %-14s | %2d pairs, %3.0f%% of orange, median %+4.0f s | %-40s | %s' % (tid, t['sport'], t['series'][k]['name'][:14], len(lags), 100 * pr, st.median(lags) if lags else 0, show(r)[:40] if r else '-', ('corr %3.0f delay %+4.0f s' % (ra['corr'], ra['delay'])) if ra else '-'))
    ok = [(x[2], x[4]['delay']) for x in rows if x[2] is not None and x[4] and x[6] >= 4]
    same = sum(1 for a, b in ok if (a > 3 and b > 3) or (a < -3 and b < -3) or (abs(a) <= 3 and abs(b) <= 3))
    side = sum(1 for a, b in ok if (a >= 0) == (b >= 0))
    print('\ncharts with 4+ of your pairs: %d | the delay score names the same leader as your marks on %d (same side of zero on %d) | typical gap between the two: %.0f s' % (len(ok), same, side, st.median([abs(a - b) for a, b in ok])))
    cr = [(x[3], x[4]['corr']) for x in rows if x[4]]
    print('your pairing rate against the correlation score, chart by chart:', ' '.join('%.0f%%/%.0f' % (100 * a, b) for a, b in cr))


class EstLevel:
    """Version 2: slide one whole line over the other. For every slide the two lines (their levels, not their
    changes) are compared over the game so far, older seconds counting for less; the score is how alike they are
    at the best slide, the delay is that slide."""
    def __init__(self, half=HALF, maxlag=MAXLAG, step=STEP):
        self.lags = list(range(-maxlag, maxlag + 1, step)); self.lam = 1.0 if not half else 0.5 ** (1.0 / half); self.maxlag = maxlag
        self.bk = collections.deque(maxlen=maxlag + 1); self.pm = collections.deque(maxlen=maxlag + 1)
        n = len(self.lags); self.w = [0.0] * n; self.sx = [0.0] * n; self.sy = [0.0] * n; self.sxx = [0.0] * n; self.syy = [0.0] * n; self.sxy = [0.0] * n
        self.lb = self.lp = None; self.n = 0; self.lo = self.hi = None

    def step(self, bk, pm):
        if bk is None: bk = self.lb
        if pm is None: pm = self.lp
        if bk is None or pm is None: return
        self.lb, self.lp = bk, pm; self.bk.append(bk); self.pm.append(pm); self.n += 1
        self.lo = bk if self.lo is None else min(self.lo, bk, pm); self.hi = bk if self.hi is None else max(self.hi, bk, pm)
        lam = self.lam; nb = len(self.bk)
        for j, L in enumerate(self.lags):
            if L >= 0:
                if nb <= L: continue
                x, y = self.bk[nb - 1 - L], pm            # the bookmakers L s ago against Polymarket now
            else:
                if nb <= -L: continue
                x, y = bk, self.pm[nb - 1 + L]            # Polymarket |L| s ago against the bookmakers now
            self.w[j] = lam * self.w[j] + 1; self.sx[j] = lam * self.sx[j] + x; self.sy[j] = lam * self.sy[j] + y
            self.sxx[j] = lam * self.sxx[j] + x * x; self.syy[j] = lam * self.syy[j] + y * y; self.sxy[j] = lam * self.sxy[j] + x * y

    def read(self):
        c = []
        for j in range(len(self.lags)):
            w = self.w[j]
            if w < 30: c.append(None); continue
            mx, my = self.sx[j] / w, self.sy[j] / w; vx, vy = self.sxx[j] / w - mx * mx, self.syy[j] / w - my * my
            c.append(None if vx < 0.25 or vy < 0.25 else (self.sxy[j] / w - mx * my) / math.sqrt(vx * vy))
        ok = [j for j in range(len(c)) if c[j] is not None]
        if not ok: return None
        j = max(ok, key=lambda i: c[i]); top = c[j]
        if top <= 0: return dict(corr=0.0, delay=0.0, at0=0.0, act=self.n / 60.0, lean=0.0)
        lo = hi = j
        while lo > 0 and c[lo - 1] is not None and c[lo - 1] >= top - 0.01: lo -= 1
        while hi < len(c) - 1 and c[hi + 1] is not None and c[hi + 1] >= top - 0.01: hi += 1
        mid = len(c) // 2
        return dict(corr=100.0 * top, delay=(self.lags[lo] + self.lags[hi]) / 2.0, at0=100.0 * (c[mid] or 0), act=(self.hi - self.lo) if self.hi is not None else 0.0, lean=0.0, wide=self.lags[hi] - self.lags[lo])


def run_level(bk, pm, half=HALF, every=None):
    e = EstLevel(half); out = []
    for i in range(len(bk)):
        e.step(bk[i], pm[i])
        if every and i and i % every == 0: out.append((i, e.read()))
    return (e.read(), out) if every else e.read()


def wave_scores(bk, pm, upto=None, last=None):
    """Version 3: waves. Both lines are cut into waves (a turn counts once the line comes back by an eighth of the
    range seen so far), each bookmaker wave is paired with Polymarket's wave the same way at the same time, and
    the delay is how much earlier the bookmaker's wave was halfway done (median over the pairs).
    Only waves that have ENDED by `upto` are used."""
    import wave_truth as WT
    n = len(bk) if upto is None else upto
    b, p = bk[:n], pm[:n]
    vals = [v for v in b + p if v is not None]
    if len(vals) < 60: return None
    th = max(1.5, 0.125 * (max(vals) - min(vals)))
    def ws(x):
        out = []
        for w in WT.swings(x, th):
            if w['type'] == 'flat' or abs(w['vb'] - w['va']) < 2.0: continue
            out.append(dict(a=w['a'], b=w['b'], va=w['va'], vb=w['vb'], size=w['vb'] - w['va'], dir=1 if w['vb'] > w['va'] else -1))
        return out[:-1] if out else out          # the last wave may still be running
    B, P = ws(b), ws(p)
    if last: B, P = [w for w in B if w['b'] >= n - last], [w for w in P if w['b'] >= n - last]
    def half(x, w):
        sgn = 1 if w['size'] > 0 else -1
        for i in range(w['a'], w['b'] + 1):
            if x[i] is not None and (x[i] - w['va']) * sgn >= 0.5 * abs(w['size']) - 1e-9: return i
    lags = []
    for pw, bw in WT.pair(P, B):
        hp, hb = half(p, pw), half(b, bw)
        if hp is not None and hb is not None: lags.append(hp - hb)
    if not B or not P: return dict(corr=0.0, delay=0.0, act=float(len(B)), pairs=0, nb=len(B), np=len(P))
    return dict(corr=100.0 * 2 * len(lags) / (len(B) + len(P)), delay=st.median(lags) if lags else 0.0, act=float(len(B)), pairs=len(lags), nb=len(B), np=len(P),
                first=sum(1 for x in lags if x > 3), behind=sum(1 for x in lags if x < -3))


def cmd_marks2():
    import wave_truth as WT
    T, M = WT.load(); rows = []
    def half(x, w):
        sgn = 1 if w['size'] > 0 else -1
        for i in range(max(0, w['a']), min(len(x) - 1, w['b']) + 1):
            if x[i] is not None and (x[i] - w['va']) * sgn >= 0.5 * abs(w['size']) - 1e-9: return i
    def simple2(ws):
        return [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in ws if w.get('type') != 'flat' and abs(w['vb2'] - w['va2']) >= WT.MIN_C]
    print('delay: + = the bookmaker line is ahead of Polymarket, - = Polymarket is ahead (seconds)')
    print('%-5s %-10s %-12s | %-24s | %-22s | %-22s | %-30s' % ('chart', 'sport', 'orange line', 'YOUR MARKS', 'v1 changes', 'v2 whole lines', 'v3 waves'))
    for tid, m in sorted(M.items()):
        t = T[tid]; L = WT.by_line(m); others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k]))
        P = simple2(WT.cleaned(t, 'pm', L['pm'])); B = simple2(WT.cleaned(t, k, L[k])); xs, xb = WT.arr(t, 'pm'), WT.arr(t, k)
        lags = []
        for p_, b_ in WT.pair(P, B):
            hp, hb = half(xs, p_), half(xb, b_)
            if hp is not None and hb is not None: lags.append(hp - hb)
        pr = 2 * len(lags) / max(len(B) + len(P), 1)
        r1 = run(xb, xs, half=None); r2 = run_level(xb, xs, half=None); r3 = wave_scores(xb, xs)
        rows.append((tid, st.median(lags) if lags else None, 100 * pr, r1, r2, r3, len(lags)))
        f = lambda r: ('corr %3.0f delay %+4.0f' % (r['corr'], r['delay'])) if r else '-'
        print('#%-4d %-10s %-12s | %2d pairs %3.0f%% delay %+4.0f | %-22s | %-22s | %s (%d pairs)' % (tid, t['sport'], t['series'][k]['name'][:12], len(lags), 100 * pr, st.median(lags) if lags else 0, f(r1), f(r2), f(r3), r3['pairs'] if r3 else 0))
    def agree(idx, name):
        ok = [(x[1], x[idx]['delay'], x[2], x[idx]['corr']) for x in rows if x[1] is not None and x[idx] and x[6] >= 4]
        lead = sum(1 for a, b, _, _ in ok if (a > 3 and b > 3) or (a < -3 and b < -3) or (abs(a) <= 3 and abs(b) <= 3))
        print('%-16s same leader as your marks on %d of %d charts | delay off by a median %2.0f s | correlation score off from your pairing rate by a median %2.0f points' % (name, lead, len(ok), st.median([abs(a - b) for a, b, _, _ in ok]), st.median([abs(c - d) for _, _, c, d in ok])))
    print(); agree(3, 'v1 changes:'); agree(4, 'v2 whole lines:'); agree(5, 'v3 waves:')


def bursts(bk, upto=None, win=10, keep=30):
    """The bookmaker line's jumps: a net change of `J`+ cents within `win` s that is still at least half there `keep` s
    later. J = the larger of 3c and a tenth of the range seen so far. Returns (second the jump was halfway, size)."""
    n = len(bk) if upto is None else upto; out = []; lo = hi = None; i = win; last = -999
    f = [None] * n; v = None
    for k in range(n):
        if bk[k] is not None: v = bk[k]
        f[k] = v
    while i < n - 1:
        if f[i] is None or f[i - win] is None: i += 1; continue
        lo = f[i] if lo is None else min(lo, f[i]); hi = f[i] if hi is None else max(hi, f[i])
        J = max(3.0, 0.10 * (hi - lo)); d = f[i] - f[i - win]
        if abs(d) >= J and i - last > win:
            # the jump: where inside the window the line crossed half of the change
            sgn = 1 if d > 0 else -1; th = next((k for k in range(i - win, i + 1) if (f[k] - f[i - win]) * sgn >= 0.5 * abs(d) - 1e-9), i)
            j2 = min(n - 1, th + keep)
            if j2 - th >= keep * 0.5 and (f[j2] - f[i - win]) * sgn >= 0.5 * abs(d):
                out.append((th, d)); last = i
        i += 1
    return out


def burst_scores(bk, pm, upto=None, W_=90, half=HALF, share_min=0.3):
    """Version 4. For every bookmaker jump so far: did Polymarket make the same move within W_ s either side, and
    when was Polymarket's own move halfway done? delay = Polymarket's halfway time minus the jump's (+ = the
    bookmakers were first), median over the jumps, recent ones counting more. correlation = share of the jumps'
    size that Polymarket also made."""
    n = len(bk) if upto is None else upto
    g = [None] * n; v = None
    for k in range(n):
        if pm[k] is not None: v = pm[k]
        g[k] = v
    rows = []
    for th, d in bursts(bk, n):
        a, b = th - W_, th + W_
        if a < 0 or b >= n or g[a] is None or g[b] is None: continue          # the jump's surroundings are not all known yet
        sgn = 1 if d > 0 else -1; dp = (g[b] - g[a]) * sgn; w = abs(d) * (0.5 ** ((n - th) / half) if half else 1.0)
        if dp < share_min * abs(d): rows.append((th, d, None, w, max(dp, 0) / abs(d))); continue
        tp = next((k for k in range(a, b + 1) if g[k] is not None and (g[k] - g[a]) * sgn >= 0.5 * dp - 1e-9), None)
        rows.append((th, d, None if tp is None else tp - th, w, min(dp / abs(d), 1.5)))
    if not rows: return None
    paired = [r for r in rows if r[2] is not None]; tw = sum(r[3] for r in rows)
    corr = 100.0 * sum(r[3] * min(r[4], 1.0) for r in rows) / tw
    if paired:
        srt = sorted(paired, key=lambda r: r[2]); acc = 0.0; tot = sum(r[3] for r in paired); delay = srt[-1][2]
        for r in srt:
            acc += r[3]
            if acc >= tot / 2: delay = r[2]; break
    else: delay = 0.0
    return dict(corr=corr, delay=float(delay), act=float(len(rows)), pairs=len(paired), first=sum(1 for r in paired if r[2] > 3), behind=sum(1 for r in paired if r[2] < -3), rows=rows)


def cmd_marks3():
    import wave_truth as WT
    T, M = WT.load(); rows = []
    def half(x, w):
        sgn = 1 if w['size'] > 0 else -1
        for i in range(max(0, w['a']), min(len(x) - 1, w['b']) + 1):
            if x[i] is not None and (x[i] - w['va']) * sgn >= 0.5 * abs(w['size']) - 1e-9: return i
    def simple2(ws):
        return [dict(a=w['a2'], b=w['b2'], va=w['va2'], vb=w['vb2'], size=w['vb2'] - w['va2'], dir=1 if w['vb2'] > w['va2'] else -1) for w in ws if w.get('type') != 'flat' and abs(w['vb2'] - w['va2']) >= WT.MIN_C]
    print('delay: + = the bookmaker line is ahead of Polymarket, - = Polymarket is ahead (seconds)')
    print('%-5s %-10s %-12s | %-38s | %-26s | %-44s' % ('chart', 'sport', 'orange line', 'YOUR MARKS', 'v2 whole lines', 'v4 bookmaker jumps'))
    for tid, m in sorted(M.items()):
        t = T[tid]; L = WT.by_line(m); others = [k for k in L if k != 'pm']
        if not others: continue
        k = max(others, key=lambda k: len(L[k]))
        P = simple2(WT.cleaned(t, 'pm', L['pm'])); B = simple2(WT.cleaned(t, k, L[k])); xs, xb = WT.arr(t, 'pm'), WT.arr(t, k)
        lags = []
        for p_, b_ in WT.pair(P, B):
            hp, hb = half(xs, p_), half(xb, b_)
            if hp is not None and hb is not None: lags.append(hp - hb)
        pr = 2 * len(lags) / max(len(B) + len(P), 1)
        r2 = run_level(xb, xs, half=None); r4 = burst_scores(xb, xs, half=None)
        rows.append((tid, st.median(lags) if lags else None, 100 * pr, r2, r4, len(lags), sum(1 for x in lags if x > 3), sum(1 for x in lags if x < -3)))
        print('#%-4d %-10s %-12s | %2d pairs %3.0f%% delay %+4.0f (bk first %d, PM first %d) | corr %3.0f delay %+4.0f        | %s' % (tid, t['sport'], t['series'][k]['name'][:12], len(lags), 100 * pr, st.median(lags) if lags else 0, rows[-1][6], rows[-1][7], r2['corr'] if r2 else 0, r2['delay'] if r2 else 0,
              ('corr %3.0f delay %+4.0f (%d jumps: bk first %d, PM first %d)' % (r4['corr'], r4['delay'], r4['pairs'], r4['first'], r4['behind'])) if r4 else 'no jump'))
    for idx, name in ((3, 'v2 whole lines:'), (4, 'v4 bookmaker jumps:')):
        ok = [(x[1], x[idx]['delay'], x[2], x[idx]['corr']) for x in rows if x[1] is not None and x[idx] and x[5] >= 4]
        lead = sum(1 for a, b, _, _ in ok if (a > 3 and b > 3) or (a < -3 and b < -3) or (abs(a) <= 3 and abs(b) <= 3))
        print('%-20s same leader as your marks on %d of %d charts | delay off by a median %2.0f s | correlation score off from your pairing rate by a median %2.0f points' % (name, lead, len(ok), st.median([abs(a - b) for a, b, _, _ in ok]), st.median([abs(c - d) for _, _, c, d in ok])))


# ---------------------------------------------------------------- through a game
def cmd_game(tid, minutes=75, every=300):
    db = W.conn(); tr = [t for t in W.trades(db) if t['id'] == tid]
    if not tr: print('no such trade'); return
    tr = tr[0]; d = tr['d']; dr = d.get('dir', 1.0)
    t1 = tr['ts']; t0 = t1 - minutes * 60000
    g = W.grid(db, tr['mid'], d['outcome'], dr, t0, t1, d.get('trigger'))
    pm, al, tg = cents(g['mid']), cents(g['cons']), cents(g['trig'])
    print('#%d %s %s v %s, the %d minutes before the buy at %s (trigger %s)' % (tid, d.get('sport'), d.get('home'), d.get('away'), minutes, time.strftime('%H:%M:%S', time.localtime(t1 / 1000)), d.get('trigger')))
    _, a = run(al, pm, every=every); _, b = run(tg, pm, every=every)
    for (i, ra), (_, rb) in zip(a, b):
        print('  %s (%2d min in) | all bookmakers: %-66s | %s: %s' % (time.strftime('%H:%M', time.localtime((t0 + i * 1000) / 1000)), i // 60, show(ra), d.get('trigger'), show(rb)))


# ---------------------------------------------------------------- at each buy
def at_buy(db, tr, minutes=60, clock='ours'):
    d = tr['d']; dr = d.get('dir', 1.0)
    t1 = d['move_ms'] - 15000; t0 = t1 - minutes * 60000           # stops 15 s before the move that set the trade off
    g = W.grid(db, tr['mid'], d['outcome'], dr, t0, t1, d.get('trigger'), clock=clock)
    return run(cents(g['cons']), cents(g['mid'])), run(cents(g['trig']), cents(g['mid']))


def bucket(rows, name, key, cuts, fmt='%5.0f'):
    print('\n' + name)
    for lo, hi in cuts:
        gset = [r for r in rows if r[key] is not None and lo <= r[key] < hi]
        if not gset: continue
        days = sorted(set(r['day'] for r in rows))
        print(('   ' + fmt + ' to ' + fmt + ': %2d trades | won %2d (%3.0f%%) | $%+7.2f | by day %s') % (lo, hi, len(gset), sum(1 for r in gset if r['pnl'] > 0), 100.0 * sum(1 for r in gset if r['pnl'] > 0) / len(gset), sum(r['pnl'] for r in gset), ' '.join('%s:%+.1f/%d' % (dy, sum(r['pnl'] for r in gset if r['day'] == dy), sum(1 for r in gset if r['day'] == dy)) for dy in days)))


def cmd_trades():
    db = W.conn(); rows = []
    for tr in W.trades(db):
        ra, rt = at_buy(db, tr); d = tr['d']
        rows.append(dict(id=tr['id'], day=time.strftime('%d', time.localtime(tr['ts'] / 1000)), sport=d.get('sport'), pnl=W.pnl12(d, d['exit']), real=tr['real'],
                         corr=ra['corr'] if ra else None, delay=ra['delay'] if ra else None, act=ra['act'] if ra else None, tcorr=rt['corr'] if rt else None, tdelay=rt['delay'] if rt else None,
                         old=(d.get('cx') or {}).get('follow', 0) - (d.get('cx') or {}).get('lead', 0) if d.get('cx') else None))
    print('%d buys, $%+.2f at $12; scores read 15 s before the move that set each trade off, from the hour before' % (len(rows), sum(r['pnl'] for r in rows)))
    for r in rows: print('  #%-4d %s %-10s %s | all bookmakers: corr %s delay %s | trigger: corr %s delay %s | old delay score %s | $%+.2f' % (r['id'], r['day'], r['sport'], 'REAL' if r['real'] else 'pap ', '%3.0f' % r['corr'] if r['corr'] is not None else '  -', '%+5.0f' % r['delay'] if r['delay'] is not None else '    -', '%3.0f' % r['tcorr'] if r['tcorr'] is not None else '  -', '%+5.0f' % r['tdelay'] if r['tdelay'] is not None else '    -', '%+.2f' % r['old'] if r['old'] is not None else '   -', r['pnl']))
    bucket(rows, 'DELAY score against all bookmakers (+ = bookmakers ahead)', 'delay', [(-999, -10), (-10, -3), (-3, 3), (3, 10), (10, 999)])
    bucket(rows, 'CORRELATION score against all bookmakers', 'corr', [(0, 30), (30, 50), (50, 70), (70, 101)])
    bucket(rows, 'DELAY score against the trigger bookmaker', 'tdelay', [(-999, -10), (-10, -3), (-3, 3), (3, 10), (10, 999)])
    good = [r for r in rows if r['delay'] is not None and r['corr'] is not None]
    for nm, f in (('bookmakers ahead (delay 3 s+) AND correlation 50+', lambda r: r['delay'] >= 3 and r['corr'] >= 50), ('bookmakers ahead (3 s+), correlation under 50', lambda r: r['delay'] >= 3 and r['corr'] < 50),
                  ('together (-3 to 3 s)', lambda r: -3 <= r['delay'] < 3), ('Polymarket ahead (3 s+)', lambda r: r['delay'] < -3)):
        g_ = [r for r in good if f(r)]
        if g_: print('   %-52s %2d trades | won %2d | $%+7.2f' % (nm, len(g_), sum(1 for r in g_ if r['pnl'] > 0), sum(r['pnl'] for r in g_)))
    bucket(rows, 'for comparison, the bot\'s OLD delay score (share followed after minus share led)', 'old', [(-9, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 9)], fmt='%5.2f')


def scores(bk, pm, half=HALF, step=STEP):
    """THE TWO SCORES at the end of two per-second lines (cents), from everything before:
    correlation = how alike the whole lines are at the best slide (version 2), delay = median over the bookmaker
    jumps so far of how long after the jump Polymarket's own move was halfway done (version 4; + = bookmakers first)."""
    e = EstLevel(half, step=step)
    for i in range(len(bk)): e.step(bk[i], pm[i])
    r2 = e.read(); r4 = burst_scores(bk, pm, half=half)
    if not r2: return None
    return dict(corr=r2['corr'], delay=r4['delay'] if r4 else None, jumps=r4['pairs'] if r4 else 0, first=r4['first'] if r4 else 0, behind=r4['behind'] if r4 else 0,
                follow=r4['corr'] if r4 else None, slide=r2['delay'])


def say(r):
    if not r: return 'not enough yet'
    if r['delay'] is None or r['jumps'] < 2: return 'correlation %3.0f | delay: too few bookmaker jumps yet (%d)' % (r['corr'], r['jumps'])
    who = 'BOOKMAKERS AHEAD' if r['delay'] > 3 else 'POLYMARKET AHEAD' if r['delay'] < -3 else 'together'
    return 'correlation %3.0f | delay %+4.0f s %s (of %d jumps: bookmakers first %d, Polymarket first %d)' % (r['corr'], r['delay'], who, r['jumps'], r['first'], r['behind'])


def cmd_game2(tid, minutes=90, every=300):
    db = W.conn(); tr = [t for t in W.trades(db) if t['id'] == tid]
    if not tr: print('no such trade'); return
    tr = tr[0]; d = tr['d']; dr = d.get('dir', 1.0); t1 = tr['ts']; t0 = t1 - minutes * 60000
    g = W.grid(db, tr['mid'], d['outcome'], dr, t0, t1, d.get('trigger'))
    pm, al = cents(g['mid']), cents(g['cons'])
    first = next((i for i in range(len(al)) if al[i] is not None and pm[i] is not None), 0)
    print('#%d %s %s v %s: the two scores through the %d minutes before the buy at %s (Polymarket against all bookmakers)' % (tid, d.get('sport'), d.get('home'), d.get('away'), minutes, time.strftime('%H:%M:%S', time.localtime(t1 / 1000))))
    for i in range(first + every, len(al) + 1, every):
        r = scores(al[first:i], pm[first:i])
        print('  %s, %3d min of prices | %s' % (time.strftime('%H:%M', time.localtime((t0 + i * 1000) / 1000)), (i - first) // 60, say(r)))
    r = scores(al[first:], pm[first:]); print('  at the buy             | ' + say(r), '| the bot\'s old delay score: %s' % ('%.2f' % ((d.get('cx') or {}).get('follow', 0) - (d.get('cx') or {}).get('lead', 0)) if d.get('cx') else '-'))


def at_buy2(db, tr, minutes=60, clock='ours'):
    d = tr['d']; dr = d.get('dir', 1.0); t1 = d['move_ms'] - 15000; t0 = t1 - minutes * 60000
    g = W.grid(db, tr['mid'], d['outcome'], dr, t0, t1, d.get('trigger'), clock=clock)
    return scores(cents(g['cons']), cents(g['mid']), step=4)


def cmd_trades2():
    db = W.conn(); rows = []
    for tr in W.trades(db):
        r = at_buy2(db, tr); d = tr['d']
        rows.append(dict(id=tr['id'], day=time.strftime('%d', time.localtime(tr['ts'] / 1000)), sport=d.get('sport'), pnl=W.pnl12(d, d['exit']), real=tr['real'],
                         corr=r['corr'] if r else None, delay=r['delay'] if r and r['jumps'] >= 2 else None, jumps=r['jumps'] if r else 0, first=r['first'] if r else 0, behind=r['behind'] if r else 0,
                         old=((d.get('cx') or {}).get('follow', 0) - (d.get('cx') or {}).get('lead', 0)) if d.get('cx') else None))
    print('%d buys, $%+.2f at $12; scores from the hour before, read 15 s before the move that set each trade off' % (len(rows), sum(r['pnl'] for r in rows)))
    for r in rows: print('  #%-4d %s %-10s %s | corr %s | delay %s (jumps %d: bk first %d, PM first %d) | old score %s | $%+.2f' % (r['id'], r['day'], r['sport'], 'REAL' if r['real'] else 'pap ', '%3.0f' % r['corr'] if r['corr'] is not None else '  -', '%+5.0f' % r['delay'] if r['delay'] is not None else '    -', r['jumps'], r['first'], r['behind'], '%+.2f' % r['old'] if r['old'] is not None else '   -', r['pnl']))
    bucket(rows, 'DELAY score (+ = bookmakers ahead of Polymarket in this game so far)', 'delay', [(-999, -20), (-20, -3), (-3, 3.01), (3.01, 20), (20, 999)])
    bucket(rows, 'CORRELATION score', 'corr', [(0, 60), (60, 80), (80, 90), (90, 101)])
    for r in rows: r['lead'] = (r['first'] - r['behind']) / r['jumps'] if r['jumps'] >= 2 else None
    bucket(rows, 'WHO WAS FIRST on the jumps so far (+1 = always the bookmakers, -1 = always Polymarket)', 'lead', [(-1.01, -0.34), (-0.34, 0.34), (0.34, 1.01)], fmt='%5.2f')
    good = [r for r in rows if r['delay'] is not None and r['corr'] is not None]
    print('\nTOGETHER:')
    for nm, f in (('bookmakers ahead 3 s+ AND correlation 80+', lambda r: r['delay'] > 3 and r['corr'] >= 80), ('bookmakers ahead 3 s+, correlation under 80', lambda r: r['delay'] > 3 and r['corr'] < 80),
                  ('together (-3 to 3 s)', lambda r: -3 <= r['delay'] <= 3), ('Polymarket ahead 3 s+', lambda r: r['delay'] < -3)):
        g_ = [r for r in good if f(r)]
        if g_: print('   %-46s %2d trades | won %2d | $%+7.2f | real only: %d trades $%+.2f' % (nm, len(g_), sum(1 for r in g_ if r['pnl'] > 0), sum(r['pnl'] for r in g_), sum(1 for r in g_ if r['real']), sum(r['pnl'] for r in g_ if r['real'])))
    none = [r for r in rows if r['delay'] is None]
    print('   %-46s %2d trades | won %2d | $%+7.2f' % ('too few jumps in the hour before to tell', len(none), sum(1 for r in none if r['pnl'] > 0), sum(r['pnl'] for r in none)))
    bucket(rows, 'for comparison, the bot\'s OLD delay score', 'old', [(-9, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 9)], fmt='%5.2f')


# ---------------------------------------------------------------- on the replay
OUT = os.path.expanduser('~/swifteye/research/game_scores_replay.json')
def cmd_replay(mode):
    sys.argv = ['sweep.py', 'none']
    import sweep as S
    ci = S.CATCH.index(1.0)
    LOOSE = dict(S.BASE, lag=None, weak_cap=None, min_move=0.07, max_followed=0.10, min_edge=0.03, dota_spread=None, bb='all', pause=False)
    key = lambda c: '%d|%s|%d' % (c['match_id'], c['outcome'], c['t0'])
    if mode == 'build':
        db = W.conn(); cand = [c for c in S.ROWS if S.gate(c, LOOSE, ci)]; prof = {}; cache = {}; t_start = time.time()
        print('moves to score:', len(cand), flush=True)
        for n, c in enumerate(cand):
            ck = (c['match_id'], c['outcome'], c['dir'], c['t0'] // 30000)
            if ck not in cache:
                t1 = c['t0'] - 15000; g = W.grid(db, c['match_id'], c['outcome'], float(c['dir']), t1 - 3600000, t1, clock='server')
                r = scores(cents(g['cons']), cents(g['mid']), step=4); cache[ck] = r and dict(corr=r['corr'], delay=r['delay'], jumps=r['jumps'], first=r['first'], behind=r['behind'], follow=r['follow'])
            prof[key(c)] = cache[ck]
            if n % 200 == 0: print(n, 'of', len(cand), '%.0f s' % (time.time() - t_start), flush=True)
        json.dump(prof, open(OUT, 'w')); print('written', OUT, len(prof)); return
    prof = json.load(open(OUT))
    for c in S.ROWS: c['_gs'] = prof.get(key(c))
    def tot(T): return sum(t['pnl'] for t in T)
    def line(name, T, T0=None):
        dp = collections.defaultdict(float); dn = collections.Counter()
        for t in T: dy = time.strftime('%d', time.localtime(t['c']['t0'] / 1000)); dp[dy] += t['pnl']; dn[dy] += 1
        print('  %-58s trades %3d | won %3d (%3.0f%%) | $%+8.2f%s | by day %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(len(T), 1), tot(T), (' (%+.2f)' % (tot(T) - tot(T0))) if T0 is not None else '', ' '.join('%s:%+.1f/%d' % (d, dp[d], dn[d]) for d in sorted(dn))))
    def flt(pred): return [c for c in S.ROWS if c['_gs'] and c['_gs']['jumps'] >= 2 and c['_gs']['delay'] is not None and pred(c['_gs'])]
    lead = lambda g: (g['first'] - g['behind']) / g['jumps']
    for title, P in (("TODAY'S RULES", dict(S.BASE)), ("TODAY'S RULES WITHOUT the bot's own 'lagged 50%' and weak-game rules", dict(S.BASE, lag=None, weak_cap=None)), ('LOOSER SET (7pt+, followed 10%, room 3c, every sport)', LOOSE)):
        print('\n== ' + title); T0 = S.sim(P); line('all moves', T0)
        line('games with 2+ bookmaker jumps in the hour before', S.sim(P, flt(lambda g: True)))
        print('  DELAY score')
        line('   bookmakers ahead 20 s+', S.sim(P, flt(lambda g: g['delay'] >= 20)))
        line('   bookmakers ahead 3 to 20 s', S.sim(P, flt(lambda g: 3 < g['delay'] < 20)))
        line('   together (-3 to 3 s)', S.sim(P, flt(lambda g: -3 <= g['delay'] <= 3)))
        line('   Polymarket ahead 3 to 20 s', S.sim(P, flt(lambda g: -20 < g['delay'] < -3)))
        line('   Polymarket ahead 20 s+', S.sim(P, flt(lambda g: g['delay'] <= -20)))
        print('  WHO WAS FIRST on the jumps so far')
        line('   mostly the bookmakers (a third more)', S.sim(P, flt(lambda g: lead(g) > 0.34)))
        line('   mixed', S.sim(P, flt(lambda g: -0.34 <= lead(g) <= 0.34)))
        line('   mostly Polymarket', S.sim(P, flt(lambda g: lead(g) < -0.34)))
        print('  CORRELATION score')
        line('   90+', S.sim(P, flt(lambda g: g['corr'] >= 90)))
        line('   80 to 90', S.sim(P, flt(lambda g: 80 <= g['corr'] < 90)))
        line('   under 80', S.sim(P, flt(lambda g: g['corr'] < 80)))
        print('  AS RULES')
        line('   skip games where Polymarket is ahead 3 s+', S.sim(P, [c for c in S.ROWS if not (c['_gs'] and c['_gs']['jumps'] >= 2 and c['_gs']['delay'] is not None and c['_gs']['delay'] < -3)]), T0)
        line('   skip games where Polymarket was mostly first', S.sim(P, [c for c in S.ROWS if not (c['_gs'] and c['_gs']['jumps'] >= 2 and c['_gs']['delay'] is not None and lead(c['_gs']) < -0.34)]), T0)
        line('   only correlation 90+', S.sim(P, flt(lambda g: g['corr'] >= 90)), T0)
        line('   only bookmakers ahead or together, correlation 80+', S.sim(P, flt(lambda g: g['delay'] >= -3 and g['corr'] >= 80)), T0)


# ---------------------------------------------------------------- the trigger bookmaker's own delay
OUT_T = os.path.expanduser('~/swifteye/research/game_scores_trigger.json')
def cmd_trig(mode):
    """Delay score of the bookmaker that set each move off: on ITS earlier jumps in this game, was Polymarket's own
    move halfway done before or after the jump?"""
    if mode == 'trades':
        db = W.conn(); rows = []
        for tr in W.trades(db):
            d = tr['d']; dr = d.get('dir', 1.0); t1 = d['move_ms'] - 15000
            g = W.grid(db, tr['mid'], d['outcome'], dr, t1 - 3600000, t1, d.get('trigger'))
            r = burst_scores(cents(g['trig']), cents(g['mid']))
            rows.append(dict(id=tr['id'], day=time.strftime('%d', time.localtime(tr['ts'] / 1000)), pnl=W.pnl12(d, d['exit']), real=tr['real'], sport=d.get('sport'),
                             delay=r['delay'] if r and r['pairs'] >= 2 else None, lead=((r['first'] - r['behind']) / r['pairs']) if r and r['pairs'] >= 2 else None, jumps=r['pairs'] if r else 0, follow=r['corr'] if r else None))
        print('%d buys; the TRIGGER bookmaker\'s own jumps in the hour before each buy' % len(rows))
        bucket(rows, 'DELAY score of the trigger bookmaker (+ = it was ahead of Polymarket)', 'delay', [(-999, -20), (-20, -3), (-3, 3.01), (3.01, 20), (20, 999)])
        bucket(rows, 'WHO WAS FIRST on the trigger\'s jumps (+1 = always the trigger, -1 = always Polymarket)', 'lead', [(-1.01, -0.34), (-0.34, 0.34), (0.34, 1.01)], fmt='%5.2f')
        bucket(rows, 'share of the trigger\'s earlier jumps that Polymarket also made (0-100)', 'follow', [(0, 40), (40, 70), (70, 101)])
        none = [r for r in rows if r['delay'] is None]
        print('   trigger had fewer than 2 jumps in the hour before: %d trades | won %d | $%+.2f' % (len(none), sum(1 for r in none if r['pnl'] > 0), sum(r['pnl'] for r in none)))
        return
    sys.argv = ['sweep.py', 'none']
    import sweep as S
    ci = S.CATCH.index(1.0)
    LOOSE = dict(S.BASE, lag=None, weak_cap=None, min_move=0.07, max_followed=0.10, min_edge=0.03, dota_spread=None, bb='all', pause=False)
    key = lambda c: '%d|%s|%d|%s' % (c['match_id'], c['outcome'], c['t0'], c['trigger'])
    if mode == 'build':
        db = W.conn(); cand = [c for c in S.ROWS if S.gate(c, LOOSE, ci)]; prof = {}; cache = {}; t_start = time.time()
        print('moves:', len(cand), flush=True)
        for n, c in enumerate(cand):
            ck = (c['match_id'], c['outcome'], c['dir'], c['t0'] // 30000, c['trigger'])
            if ck not in cache:
                t1 = c['t0'] - 15000; g = W.grid(db, c['match_id'], c['outcome'], float(c['dir']), t1 - 3600000, t1, c['trigger'], clock='server')
                r = burst_scores(cents(g['trig']), cents(g['mid']))
                cache[ck] = r and dict(delay=r['delay'], jumps=r['pairs'], first=r['first'], behind=r['behind'], follow=r['corr'], n=int(r['act']))
            prof[key(c)] = cache[ck]
            if n % 400 == 0: print(n, 'of', len(cand), '%.0f s' % (time.time() - t_start), flush=True)
        json.dump(prof, open(OUT_T, 'w')); print('written', OUT_T, len(prof)); return
    prof = json.load(open(OUT_T))
    for c in S.ROWS: c['_tg'] = prof.get(key(c))
    def tot(T): return sum(t['pnl'] for t in T)
    def line(name, T, T0=None):
        dp = collections.defaultdict(float); dn = collections.Counter()
        for t in T: dy = time.strftime('%d', time.localtime(t['c']['t0'] / 1000)); dp[dy] += t['pnl']; dn[dy] += 1
        print('  %-58s trades %3d | won %3d (%3.0f%%) | $%+8.2f%s | by day %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(len(T), 1), tot(T), (' (%+.2f)' % (tot(T) - tot(T0))) if T0 is not None else '', ' '.join('%s:%+.1f/%d' % (d, dp[d], dn[d]) for d in sorted(dn))))
    ok = lambda c: c['_tg'] and c['_tg']['jumps'] >= 2
    def flt(pred): return [c for c in S.ROWS if ok(c) and pred(c['_tg'])]
    lead = lambda g: (g['first'] - g['behind']) / g['jumps']
    for title, P in (("TODAY'S RULES", dict(S.BASE)), ("TODAY'S RULES WITHOUT the bot's own 'lagged 50%' and weak-game rules", dict(S.BASE, lag=None, weak_cap=None)), ('LOOSER SET (7pt+, followed 10%, room 3c, every sport)', LOOSE)):
        print('\n== ' + title + ' | by the TRIGGER bookmaker\'s own earlier jumps in the game'); T0 = S.sim(P); line('all moves', T0)
        line('trigger had 2+ earlier jumps', S.sim(P, flt(lambda g: True)))
        line('   trigger ahead 20 s+', S.sim(P, flt(lambda g: g['delay'] >= 20)))
        line('   trigger ahead 3 to 20 s', S.sim(P, flt(lambda g: 3 < g['delay'] < 20)))
        line('   together (-3 to 3 s)', S.sim(P, flt(lambda g: -3 <= g['delay'] <= 3)))
        line('   Polymarket ahead 3 to 20 s', S.sim(P, flt(lambda g: -20 < g['delay'] < -3)))
        line('   Polymarket ahead 20 s+', S.sim(P, flt(lambda g: g['delay'] <= -20)))
        line('   trigger mostly first', S.sim(P, flt(lambda g: lead(g) > 0.34)))
        line('   mixed', S.sim(P, flt(lambda g: -0.34 <= lead(g) <= 0.34)))
        line('   Polymarket mostly first', S.sim(P, flt(lambda g: lead(g) < -0.34)))
        line('   RULE: skip when Polymarket is ahead of the trigger 3 s+', S.sim(P, [c for c in S.ROWS if not (ok(c) and c['_tg']['delay'] < -3)]), T0)
        line('   RULE: only when the trigger is ahead or together', S.sim(P, flt(lambda g: g['delay'] >= -3)), T0)


if __name__ == '__main__':
    a = sys.argv[1:] or ['marks']
    if a[0] == 'marks': cmd_marks()
    elif a[0] == 'marks2': cmd_marks2()
    elif a[0] == 'marks3': cmd_marks3()
    elif a[0] == 'game': cmd_game2(int(a[1]))
    elif a[0] == 'trades2': cmd_trades2()
    elif a[0] == 'trades': cmd_trades()
    elif a[0] == 'replay': cmd_replay(a[1] if len(a) > 1 else 'test')
    elif a[0] == 'trig': cmd_trig(a[1] if len(a) > 1 else 'test')
