"""Charts for the owner to judge the upgraded detectors (21 Nov 2026: "upgrade the correlation, up/down trend wave
detection and delay then output some charts to me to see if I agree").

One picture per trade chart:
  top     Polymarket's sell price (blue) and one bookmaker line (orange) with the detector's waves drawn over them
          (green = up, red = down, yellow = flat), each pair of matching waves numbered and joined by a line that says
          who made the move first and by how many seconds; the owner's own marks, where there are any, as thin dashes
  middle  the correlation score as it stood at each moment (0-100)
  bottom  the delay score as it stood at each moment (seconds; up = the bookmaker first, down = Polymarket first)

usage: wave_charts.py <out dir> <trade id>[:line] ...      (line = all or a bookmaker name; default the marked line or the trigger)
"""
import os, sys, json, subprocess, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave_truth as WT

W_, H_ = 1200, 1330
BLUE, ORANGE, UP, DOWN, FLAT, INK, MUTE, GRID = '#2f6fdf', '#e07b25', '#16a34a', '#dc2626', '#d4a106', '#14171c', '#6b7280', '#e4e6ea'


def hm(t, s):
    import time
    return time.strftime('%H:%M', time.localtime(t['t0'] + s))


def chart(t, k, marks=None):
    xp, xb = WT.arr(t, 'pm'), WT.arr(t, k); n = t['n']; H = WT.chart_range(t, k)
    Pw = WT.waves3(xp, H, **WT.V3); Bw = WT.waves3(xb, H, **WT.V3)
    P, B, pr = WT.pair4(xp, xb, H); sc = WT.scores3(P, B, pr)
    # the scores as they stood through the chart (only waves finished by then, recent ones counting more)
    run = []
    for u in range(180, n + 1, 30):
        P2, B2, pr2 = WT.pair4(xp, xb, H, upto=u); r = WT.scores3(P2, B2, pr2, now=u)
        run.append((u, r))
    vals = [v for v in xp + xb if v is not None] + [t['entry'], t['exit']]
    lo, hi = min(vals) - 1.5, max(vals) + 1.5
    L, R, T0, T1 = 86, 26, 185, 820            # price panel
    X = lambda s: L + s / (n - 1) * (W_ - L - R); Y = lambda v: T0 + (hi - v) / (hi - lo) * (T1 - T0)
    o = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" font-family="Helvetica, Arial, sans-serif">' % (W_, H_), '<rect width="%d" height="%d" fill="#ffffff"/>' % (W_, H_)]
    name = 'all bookmakers' if k == 'all' else k[2:]
    o.append('<text x="%d" y="46" font-size="34" font-weight="700" fill="%s">#%d · %s · %s v %s</text>' % (L - 60, INK, t['id'], t['sport'], t['home'][:20], t['away'][:20]))
    who = 'no pairs' if not sc or sc['delay'] is None else ('the two move together' if abs(sc['delay']) <= 3 else ('%s first by %d s' % ('bookmaker' if sc['delay'] > 0 else 'Polymarket', abs(round(sc['delay'])))))
    if sc:
        o.append('<text x="%d" y="86" font-size="27" fill="%s">Correlation %d of 100 · %s (%d pairs: bookmaker first %d, Polymarket first %d)</text>' % (L - 60, INK, round(sc['corr']), who, sc['pairs'], sc['first'], sc['behind']))
        if sc.get('rem') is not None: o.append('<text x="%d" y="120" font-size="27" fill="%s">When the bookmaker moves, %d%% of Polymarket\'s wave is still to come (middle pair)</text>' % (L - 60, INK, round(100 * sc['rem'])))
    o.append('<text x="%d" y="156" font-size="24" fill="%s"><tspan fill="%s" font-weight="700">blue</tspan> Polymarket sell price · <tspan fill="%s" font-weight="700">orange</tspan> %s · thick strokes = detected waves%s</text>' % (L - 60, MUTE, BLUE, ORANGE, name, ' · thin dashes = your marks' if marks else ''))
    span = hi - lo; step = 10 if span > 50 else 5 if span > 22 else 2 if span > 9 else 1; v = (int(lo / step) + 1) * step
    while v < hi:
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/><text x="%d" y="%.1f" font-size="24" text-anchor="end" fill="%s">%d¢</text>' % (L, W_ - R, Y(v), Y(v), GRID, L - 10, Y(v) + 8, MUTE, v)); v += step
    for s in range(n):
        if (t['t0'] + s) % 120 == 0:
            o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s"/><text x="%.1f" y="%d" font-size="24" text-anchor="middle" fill="%s">%s</text>' % (X(s), X(s), T0, T1, GRID, X(s), T1 + 30, MUTE, hm(t, s)))
    def stroke(ws, x):
        out = ''
        for w in ws:
            c = UP if w['type'] == 'up' else DOWN if w['type'] == 'down' else FLAT
            out += '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="13" stroke-linecap="round" opacity="0.45"/>' % (X(w['a']), Y(w['va']), X(w['b']), Y(w['vb']), c)
        return out
    def step_line(x, col, wd):
        pts = []; seg = []; last = None
        for i in range(n):
            vv = x[i]
            if vv is None:
                if len(seg) > 1: pts.append(seg)
                seg = []; last = None; continue
            if last is not None and vv != last: seg.append('%.1f,%.1f' % (X(i), Y(last)))
            if last is None or vv != last or i == n - 1: seg.append('%.1f,%.1f' % (X(i), Y(vv)))
            last = vv
        if len(seg) > 1: pts.append(seg)
        return ''.join('<polyline fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" points="%s"/>' % (col, wd, ' '.join(p)) for p in pts)
    o.append(stroke(Bw, xb)); o.append(stroke(Pw, xp))
    o.append(step_line(xb, ORANGE, 2.6)); o.append(step_line(xp, BLUE, 3.2))
    if marks:
        for w in marks:
            if w['k'] not in ('pm', k): continue
            o.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2.2" stroke-dasharray="7 6"/>' % (X(w['a']), Y(w['va']), X(w['b']), Y(w['vb']), INK))
    # buy and sale
    bx, by = X(t['buy']), Y(t['entry']); o.append('<path d="M%.1f %.1f l12 20 l-24 0 z" fill="%s" stroke="#fff" stroke-width="2"/>' % (bx, by - 12, UP))
    sx, sy = X(t['sell']), Y(t['exit']); o.append('<path d="M%.1f %.1f l12 12 l-12 12 l-12 -12 z" fill="%s" stroke="#fff" stroke-width="2"/>' % (sx, sy - 12, DOWN))
    # the pairs: a line from the bookmaker's fast part to Polymarket's, numbered, with who was first
    for i, q in enumerate(sorted(pr, key=lambda q: q['tp']), 1):
        vb_ = xb[int(q['tb'])] if xb[int(q['tb'])] is not None else q['b']['va']; vp_ = xp[int(q['tp'])] if xp[int(q['tp'])] is not None else q['p']['va']
        x1, y1, x2, y2 = X(q['tb']), Y(vb_), X(q['tp']), Y(vp_)
        d = q['delay']; col = MUTE if abs(d) <= 3 else (ORANGE if d > 0 else BLUE); lab = 'same time' if abs(d) <= 3 else ('%s %d s' % ('bookmaker' if d > 0 else 'Polymarket', abs(round(d))))
        o.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2.5"/>' % (x1, y1, x2, y2, INK))
        for (cx, cy) in ((x1, y1), (x2, y2)): o.append('<circle cx="%.1f" cy="%.1f" r="15" fill="#fff" stroke="%s" stroke-width="2.5"/><text x="%.1f" y="%.1f" font-size="20" font-weight="700" text-anchor="middle" fill="%s">%d</text>' % (cx, cy, INK, cx, cy + 7, INK, i))
        col2 = '#6b7280' if abs(d) <= 3 else (ORANGE if d > 0 else BLUE)
        cx_ = L - 60 + ((i - 1) % 3) * 385; cy_ = T1 + 70 + ((i - 1) // 3) * 30
        o.append('<text x="%d" y="%d" font-size="22" fill="%s"><tspan font-weight="700" fill="%s">%d</tspan> %s, %d%% to come</text>' % (cx_, cy_, col2, INK, i, lab, round(100 * q.get('rem', 0))))
    # the two strips
    def strip(y0, y1, title, lo_, hi_, key, zero=None, fmt='%d'):
        Ys = lambda v: y0 + (hi_ - v) / (hi_ - lo_) * (y1 - y0)
        s_ = '<rect x="%d" y="%d" width="%d" height="%d" fill="#f7f8fa"/>' % (L, y0, W_ - L - R, y1 - y0)
        s_ += '<text x="%d" y="%d" font-size="24" font-weight="700" fill="%s">%s</text>' % (L - 60, y0 - 12, INK, title)
        for g_ in (lo_, (lo_ + hi_) / 2, hi_): s_ += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/><text x="%d" y="%.1f" font-size="22" text-anchor="end" fill="%s">%s</text>' % (L, W_ - R, Ys(g_), Ys(g_), GRID, L - 10, Ys(g_) + 8, MUTE, fmt % g_)
        pts = [(X(u), Ys(max(lo_, min(hi_, key(r))))) for u, r in run if r and key(r) is not None]
        if len(pts) > 1: s_ += '<polyline fill="none" stroke="%s" stroke-width="4" points="%s"/>' % (INK, ' '.join('%.1f,%.1f' % p for p in pts))
        for p in pts: s_ += '<circle cx="%.1f" cy="%.1f" r="4" fill="%s"/>' % (p[0], p[1], INK)
        return s_
    o.append(strip(1030, 1130, 'Correlation score as the chart went on (0 to 100)', 0, 100, lambda r: r['corr']))
    o.append(strip(1200, 1300, 'Delay score as the chart went on (seconds: above 0 = bookmaker first, below = Polymarket first)', -60, 60, lambda r: r['delay'], fmt='%+d'))
    o.append('</svg>')
    return ''.join(o), sc, len(Pw), len(Bw)


if __name__ == '__main__':
    out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
    T, M = WT.load()
    for a in sys.argv[2:]:
        tid, _, ln = a.partition(':'); tid = int(tid); t = T[tid]; m = M.get(tid)
        if ln: k = 'all' if ln == 'all' else 'b_' + ln
        elif m:
            L = WT.by_line(m); others = [x for x in L if x != 'pm']; k = max(others, key=lambda x: len(L[x])) if others else 'b_' + t['trigger']
        else: k = 'b_' + t['trigger']
        if k not in t['series']: k = 'all'
        svg, sc, npw, nbw = chart(t, k, m['waves'] if m else None)
        f = os.path.join(out, 'waves_%d_%s.svg' % (tid, k.replace('b_', '')))
        open(f, 'w').write(svg)
        subprocess.run(['qlmanage', '-t', '-s', '1400', '-o', out, f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        png = f + '.png'; final = f[:-4] + '.png'
        if os.path.exists(png): os.replace(png, final)
        print('#%d %s: waves Polymarket %d, bookmaker %d | %s -> %s' % (tid, k, npw, nbw, ('corr %.0f, delay %s, pairs %d' % (sc['corr'], ('%+.0f' % sc['delay']) if sc['delay'] is not None else '-', sc['pairs'])) if sc else 'no waves', os.path.basename(final)))
