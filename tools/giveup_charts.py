"""Charts of the give-ups of today's rules (replay, tools/sweep.py S.BASE): what the price did after the 5-minute
give-up, and what giving it 30 minutes would have done. (owner, 22 Nov 2026: "show the graphs of these")

Per chart, the side the bot bought:
  blue        Polymarket's sell price (bid), by Polymarket's own clock
  light blue  Polymarket's buy price (ask)
  orange      the middle of the bookmakers' chance for that side (1 / price), bookmakers by the time it reached us
  green dash  the catch-up target: the sell price at which the bot sells (price before the move + the bookmakers' move
              - half the spread at the buy; snipe::caught_up)
  markers     green triangle = the buy, red diamond = the 5-minute give-up sale, black circle = the 30-minute outcome
usage: giveup_charts.py <out dir> [giveup|winner]
"""
import os, sys, sqlite3, subprocess, statistics as st, time, collections, bisect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
W_, H_ = 1200, 820
BLUE, LBLUE, ORANGE, GREEN, RED, INK, MUTE, GRID = '#2f6fdf', '#9dbcf0', '#e07b25', '#16a34a', '#dc2626', '#14171c', '#6b7280', '#e4e6ea'
hm = lambda ms: time.strftime('%H:%M', time.localtime(ms / 1000))


def chart(k, t, u, db, mode='giveup'):
    c = t['c']; m = c['match_id']; dir_ = c['dir']; o = c['outcome']
    t_buy = c['tf']; t5 = t['xts']; x30 = c['exits'][S.CATCH.index(1.0)][S.GIVE.index(1800)]; t30 = x30[0]
    a = t_buy - 180_000; b = (max(t5, t_buy + 300_000) + 120_000) if mode == 'winner' else (max(t30, t_buy + 300_000) + 180_000)
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 ORDER BY id", (m, o)).fetchall()
    q = [(ts, (bd if dir_ > 0 else 1 - ak), (ak if dir_ > 0 else 1 - bd)) for ts, bd, ak in q if a <= ts <= b]
    ch = db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (m, o)).fetchall()
    lines = collections.defaultdict(lambda: ([], []))
    for ts, bk, p in ch:
        if ts <= b and p and p > 1.0:
            v = 1.0 / p; lines[bk][0].append(ts); lines[bk][1].append(v if dir_ > 0 else 1 - v)
    def bk_mid(ts):
        vals = []
        for L in lines.values():
            i = bisect.bisect_right(L[0], ts) - 1
            if i >= 0 and ts - L[0][i] < 1_800_000: vals.append(L[1][i])
        return st.median(vals) if vals else None
    grid = list(range(a, b + 1, 2000)); bm = [bk_mid(ts) for ts in grid]
    target = (c['pre_mid'] if dir_ > 0 else 1 - c['pre_mid']) + c['bm'][1] - c['_sp'] / 2
    vals = [x[1] for x in q] + [x[2] for x in q] + [v for v in bm if v is not None] + [target, t['entry'], t['exit'], x30[1]]
    lo, hi = max(0.0, min(vals) - 0.03), min(1.0, max(vals) + 0.03)
    L, R, T0, T1 = 90, 30, 200, 720
    X = lambda ts: L + (ts - a) / (b - a) * (W_ - L - R); Y = lambda v: T0 + (hi - v) / (hi - lo) * (T1 - T0)
    o_ = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" font-family="Helvetica, Arial, sans-serif">' % (W_, H_), '<rect width="%d" height="%d" fill="#fff"/>' % (W_, H_)]
    side = c['away'] if (o == 'away') == (dir_ > 0) else c['home']
    o_.append('<text x="30" y="48" font-size="32" font-weight="700" fill="%s">%s %d · %s · %s v %s</text>' % (INK, 'Winner' if mode == 'winner' else 'Give-up', k, c['sport'], c['home'][:22], c['away'][:22]))
    o_.append('<text x="30" y="88" font-size="25" fill="%s">%s %s · bought %s at %.0fc after the bookmakers moved %.0f pt (trigger %s)</text>' % (INK, time.strftime('%d %b', time.localtime(t_buy / 1000)), hm(t_buy), side[:24], 100 * t['entry'], 100 * c['bm'][1], c['trigger']))
    if mode == 'winner':
        o_.append('<text x="30" y="124" font-size="25" fill="%s"><tspan fill="%s" font-weight="700">Caught up</tspan> after %.1f min, sold %.0fc  %+.2f $</text>' % (INK, GREEN, (t5 - t_buy) / 60000, 100 * t['exit'], t['pnl']))
    else:
        r30 = ('caught up after %d min, sold %.0fc  %+.2f $' % ((t30 - t_buy) / 60000, 100 * x30[1], u['pnl'])) if u and u['caught'] else ('never caught up, sold at 30 min %.0fc  %+.2f $' % (100 * x30[1], u['pnl'] if u else 0))
        o_.append('<text x="30" y="124" font-size="25" fill="%s"><tspan fill="%s" font-weight="700">5 min:</tspan> sold %.0fc  %+.2f $   ·   <tspan font-weight="700">30 min:</tspan> %s</text>' % (INK, RED, 100 * t['exit'], t['pnl'], r30))
    o_.append('<text x="30" y="160" font-size="22" fill="%s"><tspan fill="%s" font-weight="700">blue</tspan> Polymarket sell price · <tspan fill="%s">light blue</tspan> buy price · <tspan fill="%s" font-weight="700">orange</tspan> bookmakers (middle) · <tspan fill="%s" font-weight="700">green dashes</tspan> catch-up target</text>' % (MUTE, BLUE, LBLUE, ORANGE, GREEN))
    span = hi - lo; step = 0.10 if span > 0.5 else 0.05 if span > 0.22 else 0.02 if span > 0.09 else 0.01
    v = (int(lo / step) + 1) * step
    while v < hi:
        o_.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/><text x="%d" y="%.1f" font-size="22" text-anchor="end" fill="%s">%.0f¢</text>' % (L, W_ - R, Y(v), Y(v), GRID, L - 10, Y(v) + 7, MUTE, 100 * v)); v += step
    mark = (a // 300_000 + 1) * 300_000
    while mark < b:
        o_.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s"/><text x="%.1f" y="%d" font-size="22" text-anchor="middle" fill="%s">%s</text>' % (X(mark), X(mark), T0, T1, GRID, X(mark), T1 + 30, MUTE, hm(mark))); mark += 300_000
    for ts, lab in ((t_buy + 300_000, '+5 min'),) + (((t_buy + 1_800_000, '+30 min'),) if mode != 'winner' else ()):
        if ts <= b: o_.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-dasharray="4 6"/><text x="%.1f" y="%d" font-size="20" text-anchor="middle" fill="%s">%s</text>' % (X(ts), X(ts), T0, T1, MUTE, X(ts), T0 - 8, MUTE, lab))
    o_.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-width="2.5" stroke-dasharray="10 7"/>' % (L, W_ - R, Y(target), Y(target), GREEN))
    def step_line(pts, col, wd):
        if len(pts) < 2: return ''
        s = []; last = None
        for ts, v in pts:
            if last is not None: s.append('%.1f,%.1f' % (X(ts), Y(last)))
            s.append('%.1f,%.1f' % (X(ts), Y(v))); last = v
        s.append('%.1f,%.1f' % (X(b), Y(last)))
        return '<polyline fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" points="%s"/>' % (col, wd, ' '.join(s))
    o_.append(step_line([(g, v) for g, v in zip(grid, bm) if v is not None], ORANGE, 3))
    o_.append(step_line([(x[0], x[2]) for x in q], LBLUE, 2))
    o_.append(step_line([(x[0], x[1]) for x in q], BLUE, 3.2))
    bx, by = X(t_buy), Y(t['entry']); o_.append('<path d="M%.1f %.1f l13 22 l-26 0 z" fill="%s" stroke="#fff" stroke-width="2"/>' % (bx, by - 13, GREEN))
    if mode == 'winner':
        cx_, cy_ = X(t5), Y(t['exit']); o_.append('<circle cx="%.1f" cy="%.1f" r="12" fill="%s" stroke="#fff" stroke-width="2.5"/>' % (cx_, cy_, INK))
    else:
        sx, sy = X(t5), Y(t['exit']); o_.append('<path d="M%.1f %.1f l13 13 l-13 13 l-13 -13 z" fill="%s" stroke="#fff" stroke-width="2"/>' % (sx, sy - 13, RED))
        cx_, cy_ = X(t30), Y(x30[1]); o_.append('<circle cx="%.1f" cy="%.1f" r="12" fill="%s" stroke="#fff" stroke-width="2.5"/>' % (cx_, cy_, INK))
    o_.append('</svg>')
    return ''.join(o_)


if __name__ == '__main__':
    out = os.path.abspath(sys.argv[1]); os.makedirs(out, exist_ok=True)
    db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
    A = S.sim(S.BASE); B = {(t['c']['match_id'], t['c']['t0']): t for t in S.sim(dict(S.BASE, give=1800))}
    mode = sys.argv[2] if len(sys.argv) > 2 else 'giveup'
    gu = [t for t in A if (t['caught'] if mode == 'winner' else not t['caught'])]
    for k, t in enumerate(gu, 1):
        u = B.get((t['c']['match_id'], t['c']['t0']))
        f = os.path.join(out, '%s_%02d.svg' % (mode, k)); open(f, 'w').write(chart(k, t, u, db, mode))
        subprocess.run(['qlmanage', '-t', '-s', '1400', '-o', out, f], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(f + '.png'): os.replace(f + '.png', f[:-4] + '.png')
        print('give-up %d: %s %s, 5 min %+.2f, 30 min %s' % (k, t['c']['sport'], hm(t['c']['tf']), t['pnl'], ('%+.2f%s' % (u['pnl'], ' caught' if u['caught'] else '')) if u else '-'), flush=True)
