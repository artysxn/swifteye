"""Synthetic cases with known answers for waves.py.  usage: python tools/waves/test_waves.py"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waves as W  # noqa: E402

S = 1_000_000_000_000  # a start time in ms
passed = 0


def ok(name, cond, detail=''):
    global passed
    assert cond, f'{name}: {detail}'
    passed += 1
    print('ok -', name)


def pm_rows(points, outcome='home'):
    """Polymarket rows from (t, mid): bid/ask 1c either side."""
    return [(t, t, outcome, round(m - 0.01, 4), round(m + 0.01, 4)) for t, m in points]


def book_rows(book, points):
    """Bookmaker rows for a two-way market from (t, p_home or None): fair odds with no margin."""
    out = []
    for t, p in points:
        out.append((t, book, 'home', None if p is None else 1 / p))
        out.append((t, book, 'away', None if p is None else 1 / (1 - p)))
    return out


# 1. A bookmaker steps 40% -> 55% at 100 s; Polymarket makes the same move at 108 s.
pm = pm_rows([(S + i * 1000, 0.40) for i in range(0, 108)] + [(S + i * 1000, 0.55) for i in range(108, 200)])
bk = book_rows('unibet', [(S, 0.40), (S + 100_000, 0.55)])
g = W.game_waves(pm, bk, 'home')
ok('one wave across both sources', len(g['waves']) == 1, g['waves'])
w = g['waves'][0]
ok('the bookmaker led', w['leader'] == 'unibet' and w['on'][W.PM] - w['on']['unibet'] == 8_000, w['on'])
r = g['rows'][0]
ok('Polymarket followed the bookmaker', r['followed'] and r['led_pm'] and r['lead'] == 8_000, r)
ok('Polymarket completed the wave by 10 s', w['pm_resp'][10_000] > 0.95 and w['pm_resp'][5_000] < 0.05, w['pm_resp'])

# 2. Polymarket flickers a tick up and down every second around a real 10-point move: one wave, no flicker waves.
flick = [(S + i * 1000, 0.50 + (0.01 if i % 2 else 0)) for i in range(0, 60)] + [(S + i * 1000, 0.60 + (0.01 if i % 2 else 0)) for i in range(60, 120)]
g = W.game_waves(pm_rows(flick), book_rows('unibet', [(S, 0.5), (S + 59_000, 0.6)]), 'home')
ok('flicker is not a wave', len([x for x in g['waves'] if W.PM in x['on']]) == 1, [x['on'] for x in g['waves']])

# 3. Polymarket moves first (100 s), the bookmaker catches up 60 s later: Polymarket led, the bookmaker caught up.
pm = pm_rows([(S + i * 1000, 0.40) for i in range(0, 100)] + [(S + i * 1000, 0.55) for i in range(100, 250)])
g = W.game_waves(pm, book_rows('linebet', [(S, 0.40), (S + 160_000, 0.55)]), 'home')
w = g['waves'][0]
r = g['rows'][0]
ok('Polymarket led the wave', w['leader'] == W.PM and not r['led_pm'] and r['lead'] == -60_000, (w, r))
ok('the bookmaker was catching up', r['pm_done_before'] > 0.95, r)

# 4. A suspension across a goal: the bookmaker goes dark at 100 s and reopens at 106 s at a new price.
g = W.game_waves(pm_rows([(S + i * 1000, 0.40) for i in range(0, 300)]), book_rows('fonbet', [(S, 0.40), (S + 100_000, None), (S + 106_000, 0.70)]), 'home')
c = [x for x in g['rows'] if x['src'] == 'fonbet']
ok('a reopen at a new price is a wave from the price before', len(c) == 1 and c[0]['reopen'] and abs(c[0]['amp'] - 0.30) < 1e-9 and c[0]['t'] == S + 106_000, c)
ok('Polymarket did not follow it', c[0]['followed'] is False, c)

# 5. Lead-lag: a random walk seen by a bookmaker (polled every 2 s) and by Polymarket 6 s later (every 0.5 s).
rng = np.random.default_rng(7)
steps = rng.normal(0, 0.01, 4000)
latent = 0.5 + np.cumsum(steps) * 0.2
latent = np.clip(latent, 0.05, 0.95)
lat = lambda t: latent[min(max(int(t / 500), 0), len(latent) - 1)]
book = [(S + t, float(lat(t))) for t in range(0, 2_000_000, 2000)]
pmln = [(S + t, float(lat(t - 6000))) for t in range(0, 2_000_000, 500)]
U, vb, vp = W.hy_contrast(book, pmln)
th = W.THETAS[int(np.argmax(np.abs(U)))]
ok('Hayashi-Yoshida finds the 6 s lead', abs(th - 6000) <= 1000, th)
ok('and a high correlation there', np.max(np.abs(U)) / np.sqrt(vb * vp) > 0.8, np.max(np.abs(U)) / np.sqrt(vb * vp))

# 6. Two-way model: three sources at fixed delays over many waves (noise), Polymarket in the middle.
rng = np.random.default_rng(3)
waves = []
for k in range(200):
    T = k * 1_000_000
    waves.append({'on': {'fast': T + int(rng.normal(0, 800)), W.PM: T + 4000 + int(rng.normal(0, 800)), 'slow': T + 30_000 + int(rng.normal(0, 800))}})
d = W.two_way_delays(waves)
ok('two-way delays recovered', abs(d['fast'] + 4000) < 500 and abs(d['slow'] - 26_000) < 500 and d[W.PM] == 0, d)

# 7. Superline: one family jumps, the others confirm -> the line goes all the way; a lone jump the others
#    contradict -> the line comes back; a sister brand repeating is not a confirmation.
calm = lambda n: [(S + i * 2000, 0.50) for i in range(n)]
L = {'unibet': calm(50) + [(S + 100_000, 0.65)], 'fonbet': calm(50) + [(S + 103_000, 0.65)], 'leon': calm(50) + [(S + 105_000, 0.65)]}
sl = W.superline(L)
at = lambda line, t: [p for tt, p, _ in line if tt <= t][-1]
ok('superline reacts to the first family at once', 0.55 < at(sl, S + 100_000) < 0.65, at(sl, S + 100_000))
ok('and goes all the way once others confirm', abs(at(sl, S + 106_000) - 0.65) < 0.01, at(sl, S + 106_000))
L2 = {'unibet': calm(50) + [(S + 100_000, 0.65)], 'fonbet': calm(50) + [(S + 102_000, 0.501)], 'leon': calm(50) + [(S + 104_000, 0.499)]}
sl2 = W.superline(L2)
ok('a lone jump the others contradict comes back', abs(at(sl2, S + 105_000) - 0.50) < 0.01, at(sl2, S + 105_000))
L3 = {'unibet': calm(50) + [(S + 100_000, 0.65)], 'leovegas': calm(50) + [(S + 101_000, 0.65)], 'fonbet': calm(50) + [(S + 102_000, 0.50)]}
sl3 = W.superline(L3)
ok('a sister brand repeating is not a confirmation', at(sl3, S + 103_000) < 0.53, at(sl3, S + 103_000))

# 8. Lead state (lead.py): a steady offset is not a lead; the superline stepping up while Polymarket waits is
#    "superline ahead, up" until Polymarket catches up; Polymarket moving first is "Polymarket ahead".
import lead as LD  # noqa: E402
n = 1200
pm = np.full(n, 0.50)
sl = np.full(n, 0.53)                       # the bookmakers a bit more optimistic all along
r = LD.lead_state(pm, sl)
ok('a steady offset is level', (r['state'][60:] == LD.LEVEL).all(), np.unique(r['state']))
sl2 = sl.copy()
sl2[600:] = 0.63                            # superline +10 points at 600 s
pm2 = pm.copy()
pm2[615:] = 0.60                            # Polymarket follows 15 s later
r = LD.lead_state(pm2, sl2)
ok('superline ahead (up) while Polymarket waits', (r['state'][600:615] == LD.AHEAD_UP).all(), r['state'][598:618])
ok('level again once Polymarket caught up', r['state'][620] == LD.LEVEL, r['state'][615:625])
ok('expected catch-up is upward', r['exp'][605] > 0, r['exp'][605])
pm3 = pm.copy()
pm3[600:] = 0.40                            # Polymarket drops; the superline stays
r = LD.lead_state(pm3, sl)
ok('Polymarket moving first is Polymarket ahead', r['state'][605] == LD.PM_AHEAD, r['state'][600:610])
ok('a gap left open is stale after 3 minutes', r['state'][600 + 200] == LD.STALE, r['state'][790:800])

# 9. Trend waves (CUSUM): a jump is found at once, a slow climb is found, flicker is not.
p = np.full(600, 0.5)
p[300:] = 0.58
eps, _, _ = LD.trends(p)
ok('a jump is one up wave, detected in the same second', len(eps) == 1 and eps[0]['dir'] == 1 and eps[0]['alarm'] == 300 and eps[0]['on'] == 299, eps)
x = np.concatenate([np.zeros(200), np.arange(1, 121) * 0.005, np.full(280, 0.6)])
eps, _, _ = LD.trends(1 / (1 + np.exp(-x)))
ok('a climb of 0.005 log-odds a second is an up wave', len(eps) == 1 and eps[0]['dir'] == 1 and 199 <= eps[0]['on'] <= 201 and eps[0]['alarm'] < 260, eps)
fl = 0.5 + 0.01 * (np.arange(600) % 2)
eps, _, _ = LD.trends(fl)
ok('one-point flicker is no wave', len(eps) == 0, eps)
p = np.full(900, 0.5)
p[300:] = 0.58
p[600:] = 0.47
eps, _, _ = LD.trends(p)
ok('up then down: two waves in order', [e['dir'] for e in eps] == [1, -1] and eps[1]['alarm'] == 600, eps)

print(f'\n{passed} passed')
