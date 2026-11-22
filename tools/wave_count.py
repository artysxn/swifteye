"""How many OPPOSING waves are there inside games? (owner, 22 Nov 2026: "Across all the games you just downloaded, how
many opposing waves exist? Meaning turning direction, from up to down, or down to up, up to down to up to down to end
would be 3 waves of opposing directions, not counting flat ones.")

A DIRECTION CHANGE = the price turning from up to down or from down to up; flat stretches are ignored (up, flat, up is
one up wave). Up, down, up, down = 3 direction changes. Counted two ways:
  size     a turn counts once the price has come back at least X cents from the last top / bottom (X = 2, 3, 5, 10)
  v3       the wave detector the owner agreed matches the eye (tools/wave_truth.py waves3, flat under 4% of the
           chart's height), run on the whole game's chart
Data: tools/game_paths.mjs (gzip), the ENTIRE Polymarket history of each market: every trade and Polymarket's own price
line (one point a minute). Main path = the minute line: on 140 games Swifteye recorded itself (17-21 Nov) it gives
the same count as the recorded order book sampled once a minute (tools/wave_res.py). Trades alone see fewer waves
(nobody trades for minutes at a time), so they are only shown beside it. The book seen every 10 s has about 2.2-2.9
times as many direction changes as once a minute; that ratio gives the 10-second estimate. In play = from the listed start until the price reaches 99c / 1c (decided) or
the last trade. Before the start = the market's whole life before the listed start.

usage: wave_count.py [paths dir]      -> prints the tables, writes <dir>/wave_counts.txt and <dir>/wave_counts.json
"""
import sys, os, json, glob, gzip, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wave_truth as WT

D = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/swifteye/research/paths')
SIZES = (2, 3, 5, 10)
MAIN = 3
STEP = 10          # seconds a step on the every-trade path


def steps(pts, a, b, step):
    """Price in cents every `step` seconds from a to b (unix s): the middle price of the points in each step, else held."""
    out = []; k = 0; last = None; n = len(pts)
    while k < n and pts[k][0] < a: last = pts[k][1] * 100; k += 1
    for u in range(a, b + 1, step):
        v = []
        while k < n and pts[k][0] < u + step: v.append(pts[k][1] * 100); k += 1
        if v: v.sort(); last = v[len(v) // 2]
        out.append(last)
    return out


def turns(x, th):
    """Waves by size: legs of at least th cents, each turn confirmed when the price comes back th from the extreme."""
    legs = []; d = 0; ext = None; ext_i = 0; start = None; start_i = 0
    for i, v in enumerate(x):
        if v is None: continue
        if start is None: start = ext = v; start_i = ext_i = i; continue
        if d == 0:
            if abs(v - start) >= th: d = 1 if v > start else -1; ext, ext_i = v, i
            continue
        if (v - ext) * d > 0: ext, ext_i = v, i
        elif abs(v - ext) >= th:
            legs.append((d, start, ext, start_i, ext_i)); start, start_i = ext, ext_i; d = -d; ext, ext_i = v, i
    if d != 0: legs.append((d, start, ext, start_i, ext_i))
    return legs


def v3turns(x, grid=1):
    v = [y for y in x if y is not None]
    if len(v) < 5: return []
    H = max(3.0, max(v) - min(v))
    ws = [w for w in WT.waves3(x, H, L=WT.V3['L'], flat=WT.V3['flat'], grid=grid) if w['type'] != 'flat']
    legs = []
    for w in ws:
        d = 1 if w['type'] == 'up' else -1
        if legs and legs[-1][0] == d: legs[-1] = (d, legs[-1][1], w['vb'], legs[-1][3], w['b'])
        else: legs.append((d, w['va'], w['vb'], w['a'], w['b']))
    return legs


def at_bid(x):
    """Did this trade happen at side 0's SELL price (its bid)? A taker selling side 0, or a taker buying side 1
    (side 1's ask = 1 - side 0's bid). The others happened at side 0's buy price (its ask)."""
    return (x[4] == 0 and x[3] < 0) or (x[4] == 1 and x[3] > 0)


def game(r):
    """Main path = Polymarket's own price line, one point a minute: on 140 games recorded 17-21 Nov it gives the same
    count as the recorded order book sampled once a minute (tools/wave_res.py), so it is the book, seen coarsely."""
    tr = r['trades']; line = r['line']; s0 = r['s']
    lp = [x for x in line if x[0] >= s0]
    if len(lp) < 20: return None
    end = lp[-1][0]
    x = steps(line, s0, end, 60)
    for i, v in enumerate(x):
        if v is not None and (v >= 99 or v <= 1): x = x[:i + 1]; end = s0 + i * 60; break
    if end - s0 < 20 * 60: return None                     # under 20 minutes of play: skip
    vals = [v for v in x if v is not None]
    if len(vals) < 20: return None
    g = dict(sport=r['tag'], q=r['q'], s=s0, vol=r['vol'], mins=(end - s0) / 60, trades=sum(1 for t in tr if s0 <= t[0] <= end), range=max(vals) - min(vals))
    for th in SIZES:
        L = turns(x, th); g['n%d' % th] = max(0, len(L) - 1); g['legs%d' % th] = [(d, round(abs(b - a), 1), e - s) for d, a, b, s, e in L]
    L = v3turns(x, grid=1); g['v3'] = max(0, len(L) - 1); g['legs_v3'] = [(d, round(abs(b - a), 1), e - s) for d, a, b, s, e in L]
    bid = [t for t in tr if at_bid(t)]
    xt = steps(bid, s0, end, STEP) if bid else []
    g['tr%d' % MAIN] = max(0, len(turns(xt, MAIN)) - 1) if xt else None
    pre = steps(line, line[0][0], s0, 60) if s0 - line[0][0] > 600 else []
    g['pre%d' % MAIN] = max(0, len(turns(pre, MAIN)) - 1) if pre else None
    g['pre_hours'] = (s0 - line[0][0]) / 3600
    return g


def pct(v, q): v = sorted(v); return v[min(len(v) - 1, int(q * len(v)))]


def table(G, out):
    def line(name, X):
        if len(X) < 20: out.append('  %-12s %5d games (too few)' % (name, len(X))); return
        hrs = sum(g['mins'] for g in X) / 60
        cells = []
        for th in SIZES:
            v = [g['n%d' % th] for g in X]
            cells.append('%2dc+: %5.1f a game (median %2d, %3.0f%% none) %4.1f an hour' % (th, st.mean(v), st.median(v), 100.0 * sum(1 for y in v if y == 0) / len(v), sum(v) / hrs))
        v = [g['v3'] for g in X]
        out.append('  %-12s %5d games, median %3d min played, %4d trades | ' % (name, len(X), st.median(g['mins'] for g in X), st.median(g['trades'] for g in X)) + ' | '.join(cells)
                   + ' | v3 detector: %4.1f a game (median %d, %2.0f%% none)' % (st.mean(v), st.median(v), 100.0 * sum(1 for y in v if y == 0) / len(v)))
    out.append('DIRECTION CHANGES IN PLAY (up->down or down->up; flat ignored), by the size a wave must have')
    for s in sorted({g['sport'] for g in G}): line(s, [g for g in G if g['sport'] == s])
    line('ALL', G)
    out.append('\nTOTAL direction changes across all %d games: ' % len(G) + ', '.join('%dc+ %d' % (th, sum(g['n%d' % th] for g in G)) for th in SIZES) + ', v3 detector %d' % sum(g['v3'] for g in G))
    out.append('\nHOW MANY A GAME (%dc+ waves): share of games with ...' % MAIN)
    for s in sorted({g['sport'] for g in G}) + ['ALL']:
        X = G if s == 'ALL' else [g for g in G if g['sport'] == s]
        if len(X) < 20: continue
        v = [g['n%d' % MAIN] for g in X]; b = [(0, 0), (1, 2), (3, 5), (6, 10), (11, 20), (21, 9999)]
        out.append('  %-12s ' % s + '  '.join('%s: %3.0f%%' % ('%d' % lo if lo == hi else ('%d+' % lo if hi > 999 else '%d-%d' % (lo, hi)), 100.0 * sum(1 for y in v if lo <= y <= hi) / len(v)) for lo, hi in b)
                   + '   | 10%% of games %d or fewer, 90%% %d or fewer' % (pct(v, 0.1), pct(v, 0.9)))
    out.append('\nTHE WAVES THEMSELVES (%dc+): size and length' % MAIN)
    for s in sorted({g['sport'] for g in G}) + ['ALL']:
        X = G if s == 'ALL' else [g for g in G if g['sport'] == s]
        if len(X) < 20: continue
        legs = [l for g in X for l in g['legs%d' % MAIN]]
        mid = [l for g in X for l in g['legs%d' % MAIN][1:-1]]           # waves with a turn at both ends
        out.append('  %-12s %6d waves | size median %4.1fc (middle half %4.1f-%4.1fc) | length median %3d min (middle half %d-%d) | a wave that turns is followed by one of at least half its size: %3.0f%%' % (
            s, len(legs), st.median(l[1] for l in legs), pct([l[1] for l in legs], .25), pct([l[1] for l in legs], .75), st.median(l[2] for l in legs), pct([l[2] for l in legs], .25), pct([l[2] for l in legs], .75),
            100.0 * sum(1 for g in X for a, b in zip(g['legs%d' % MAIN], g['legs%d' % MAIN][1:]) if b[1] >= 0.5 * a[1]) / max(1, sum(max(0, len(g['legs%d' % MAIN]) - 1) for g in X))))
    out.append('\nHOW LONG WAVES LAST in play (from a turn to the next top or bottom, Polymarket\'s minute line; the book every 10 s has more, shorter waves)')
    for s in sorted({g['sport'] for g in G}) + ['ALL']:
        X = G if s == 'ALL' else [g for g in G if g['sport'] == s]
        if len(X) < 20: continue
        cells = []
        for th in SIZES:
            m = sorted(l[2] for g in X for l in g['legs%d' % th])
            if len(m) >= 20: cells.append('%2dc+: %5.1f min on average, median %3d, middle half %d-%d' % (th, st.mean(m), m[len(m) // 2], m[len(m) // 4], m[3 * len(m) // 4]))
        out.append('  %-12s ' % s + ' | '.join(cells))
    out.append('\nBEFORE THE START (the market\'s whole life before the listed start, the minute line), %dc+ direction changes:' % MAIN)
    for s in sorted({g['sport'] for g in G}):
        X = [g for g in G if g['sport'] == s and g['pre%d' % MAIN] is not None]
        if X: out.append('  %-12s %5d games, open a median %5.1f h before the start: %5.1f a game (median %d)' % (s, len(X), st.median(g['pre_hours'] for g in X), st.mean(g['pre%d' % MAIN] for g in X), st.median(g['pre%d' % MAIN] for g in X)))
    out.append('\nWHAT THE BOOK WOULD SHOW AT 10-SECOND STEPS (estimate): the minute count times the 10-s / 1-min ratio measured on')
    out.append('140 games Swifteye recorded itself (tools/wave_res.py: 2c+ x2.92, 3c+ x2.76, 5c+ x2.44, 10c+ x2.22)')
    R = {2: 72.2 / 24.7, 3: 51.9 / 18.8, 5: 30.2 / 12.4, 10: 12.9 / 5.8}
    for s in sorted({g['sport'] for g in G}) + ['ALL']:
        X = G if s == 'ALL' else [g for g in G if g['sport'] == s]
        if len(X) >= 20: out.append('  %-12s ' % s + ' | '.join('%2dc+ about %5.1f a game' % (th, R[th] * st.mean(g['n%d' % th] for g in X)) for th in SIZES))
    out.append('\nBY HOW LONG THE GAME WAS PLAYED (%dc+ direction changes a game):' % MAIN)
    for lo, hi in ((20, 60), (60, 90), (90, 120), (120, 180), (180, 400)):
        X = [g for g in G if lo <= g['mins'] < hi]
        if len(X) >= 20: out.append('  %3d-%3d min  %5d games  %5.1f a game' % (lo, hi, len(X), st.mean(g['n%d' % MAIN] for g in X)))


if __name__ == '__main__':
    G = []
    for f in sorted(glob.glob(os.path.join(D, 'paths_*.ndjson.gz'))):
        for l in gzip.open(f, 'rt'):
            if l.strip():
                g = game(json.loads(l))
                if g: G.append(g)
        print('read', os.path.basename(f), len(G), 'games so far', flush=True)
    out = []; table(G, out); txt = '\n'.join(out); print(txt)
    open(os.path.join(D, 'wave_counts.txt'), 'w').write(txt + '\n')
    json.dump(G, open(os.path.join(D, 'wave_counts.json'), 'w'))
