"""At rest: what a direction call is worth, by how sure the timing is (no model; all 7 days, live rows).

  python tools/signals/wavedir_rest.py <signals dir>/wavedir.npz

Reads the file of wavedir.py build. For seconds where Polymarket is at rest and where it is already moving: how often
a wave / a big wave follows within 30 s, what a right and a wrong call make by wave size and exit, the result at a
given direction accuracy, and the two numbers a call needs (how often right, how often a big wave must follow)."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import mine as MN  # noqa: E402

S = np.load(sys.argv[1])
C = MN.C
X = S['X']
live = (S['age'] <= 15) & (S['late'] <= 1000)
tn, amp, Y, G, SP = S['tn'], S['amp'], S['Y'], S['game'].astype(np.int64), S['sport']
okY = np.isfinite(Y[:, :, :6]).all((1, 2))
rest = (X[:, C['pm_U']] == 0) & (X[:, C['pm_D']] == 0) & (X[:, C['pm_r10']] == 0)
up, dn = tn[:, 0] <= 30, tn[:, 1] <= 30
one, none = up ^ dn, ~(up | dn)
ampw = np.where(up, amp[:, 0], amp[:, 1])
Yr = np.where(up[:, None], Y[:, 0], Y[:, 1])
Yw = np.where(up[:, None], Y[:, 1], Y[:, 0])
BIG = 0.45
base = live & okY
print(f'live rows {live.sum():,} of {len(live):,}; at rest {np.mean(rest[live]) * 100:.1f}% of them; games {len(np.unique(G[live]))}')
for name, m in (('AT REST', base & rest), ('MOVING', base & ~rest)):
    w, b = m & one, m & one & (ampw >= BIG)
    print(f'{name}: {m.sum():,} seconds; a wave within 30 s after {np.mean(one[m]) * 100:.1f}% (both ways {np.mean((up & dn)[m]) * 100:.1f}%), a BIG one after {np.mean((one & (ampw >= BIG))[m]) * 100:.2f}%; of waves, big {np.mean(ampw[w] >= BIG) * 100:.0f}%')
    for j, ex in ((1, '1 min'), (2, '2 min'), (3, '5 min')):
        Wb, Lb = np.nanmean(Yr[b, j]), np.nanmean(Yw[b, j])
        Ws, Ls = np.nanmean(Yr[w & ~b, j]), np.nanmean(Yw[w & ~b, j])
        Ln = np.nanmean(Y[m & none][:, :, j])
        pb, ps, pn = np.mean((one & (ampw >= BIG))[m]), np.mean((one & (ampw < BIG))[m]), np.mean(~one[m])
        print(f'   sold after {ex}: big wave right ${Wb:+.2f} / wrong ${Lb:+.2f}; small wave right ${Ws:+.2f} / wrong ${Ls:+.2f}; no wave ${Ln:+.2f}')
        for a in (0.5, 0.6, 0.75, 1.0):
            ev_all = pb * (a * Wb + (1 - a) * Lb) + ps * (a * Ws + (1 - a) * Ls) + pn * Ln
            ev_big = a * Wb + (1 - a) * Lb
            print(f'      direction right {a * 100:.0f}%: every such second ${ev_all:+.2f} per $100 | if a BIG wave were certain ${ev_big:+.2f}')
        need = -Lb / (Wb - Lb)
        # how often a big wave must follow a call for a given direction accuracy on big waves, when the rest is "no wave"
        for a in (0.55, 0.6, 0.75, 1.0):
            g = a * Wb + (1 - a) * Lb
            print(f'      big-wave direction right {a * 100:.0f}%: a call needs a big wave after {(-Ln / (g - Ln) * 100) if g > 0 else float("nan"):.0f}% of calls (the rest no wave)', end='')
            print()
        print(f'      if a big wave is certain, direction needs {need * 100:.0f}%')
    for sp in ('football', 'tennis', 'cs2', 'basketball'):
        mm = w & (SP == sp)
        if mm.sum() > 300:
            print(f'   {sp}: waves {mm.sum():,} seconds, big {np.mean(ampw[mm] >= BIG) * 100:.0f}%; right way 2 min ${np.nanmean(Yr[mm, 2]):+.2f}, big right ${np.nanmean(Yr[mm & (ampw >= BIG), 2]):+.2f}')
