"""PILOT: direction from wave timing (owner, 23 Nov night: "if we can predict how long a wave lasts, say 30 s, then we
can tell which direction: at 30 s into an up wave, the next one goes down more often than up").

Data: lab.py build (lab_sec.npz = in-play decision seconds, lab_evt.npz = wave starts). A row is IN A WAVE when
Polymarket's CUSUM wave alarm has fired and the wave has not ended (X['pm_wave'] = its direction, known live);
rem_t = seconds left to its peak (hindsight; missing once the peak has passed).
FADE = buy against the wave now; FOLLOW = buy with it. Dollars per $100 with fees and depth (mine.labels): sold after
1 min, after 5 min, or +5c / 5 min.

1. Do waves alternate? Next Polymarket wave of the same game: opposite or same direction (lab_evt).
2. THE CEILING: fading exactly AT THE PEAK (rem_t <= 2 s, which nobody knows live). If this loses, no wave-length
   model can make the fade pay; if it wins, the question is how close a model gets.
3. The literal rule: fade at a fixed wave age (10-20 s, 25-35 s, 60 s+).
4. LIGHT MODELS (trees, one split: trained on the days before 22 Nov, last fifth of those for the thresholds, scored on
   22-23 Nov; a 400k-row sample, <= 80 trees, about a minute each):
   A "has it peaked" (the wave-length idea as a yes / no): fade its top 5% of calls;
   B "fading beats following over 5 min" (direction itself): fade its top 5%, follow its bottom 5%.
   Each next to no model, with the hit rate it needs (lab.breakeven).

usage: python tools/signals/pilot_wavedir.py --signals ~/swifteye/research/lab
"""
import argparse
import datetime as dt
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import lab as LB  # noqa: E402
import mine as MN  # noqa: E402

C = MN.C
EX = (('1 min', 0), ('5 min', 1), ('+5c or 5 min', 2))


def line(name, pay, games, right=None):
    st = MN.gstats(pay, games)
    if st is None:
        return f'   {name:<44} no rows'
    s = f'   {name:<44}{st["n"]:>8,} rows {st["games"]:>5,} games  ${st["mean"]:+6.2f} per $100 (90% of the total {st["lo"] / st["n"]:+.2f}..{st["hi"] / st["n"]:+.2f})  win {st["win"]:.0f}%'
    if right is not None:
        s += f'  right way {np.mean(right) * 100:.0f}%'
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    import torch
    torch.manual_seed(0)
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S = np.load(os.path.join(a.signals, 'lab_sec.npz'))
    T, G, SP, Y, rem = S['t'], S['game'].astype(np.int64), S['sport'], S['Y'], S['rem_t']
    X = S['X']
    wv, age = X[:, C['pm_wave']], np.expm1(X[:, C['pm_wave_age']])
    inw = (wv != 0) & np.isfinite(Y[:, 0, 1]) & np.isfinite(Y[:, 1, 1])
    I = np.flatnonzero(inw)
    d = wv[I]
    jo, js = np.where(d > 0, 1, 0), np.where(d > 0, 0, 1)                      # Y[:, 0] = buy up, Y[:, 1] = buy down
    fade = Y[I, jo, :3].astype(float)
    foll = Y[I, js, :3].astype(float)
    right = fade[:, 1] > foll[:, 1]                                            # over 5 min the price went against the wave
    remI, ageI, GI, TI, SPI = rem[I], age[I], G[I], T[I], SP[I]
    say(f'{len(T):,} in-play decision rows, {len(I):,} of them inside a Polymarket wave ({len(np.unique(GI)):,} games) ({time.time() - t0:.0f} s)')

    # ---------------- 1: do waves alternate
    E = np.load(os.path.join(a.signals, 'lab_evt.npz'))
    pm = E['W'][:, 9] == 1
    o = np.lexsort((E['t'][pm], E['outcome'][pm], E['game'][pm]))
    g_, k_, t_, d_, sp_ = (E[k][pm][o] for k in ('game', 'outcome', 't', 'd', 'sport'))
    amp_ = E['TG'][pm][o][:, 3]
    same_game = (g_[1:] == g_[:-1]) & (k_[1:] == k_[:-1])
    opp = (d_[1:] != d_[:-1])[same_game]
    gap = ((t_[1:] - t_[:-1]) / 1000.0)[same_game]
    spn, ampn = sp_[:-1][same_game], amp_[:-1][same_game]
    say()
    say('1. DO WAVES ALTERNATE? The next Polymarket wave of the same game goes the OTHER way in:')
    say(f'   all {opp.mean() * 100:.1f}% of {len(opp):,} pairs (50% = no memory)   |   ' + ', '.join(f'{s} {opp[spn == s].mean() * 100:.0f}%' for s in ('tennis', 'football', 'basketball', 'cs2', 'hockey') if (spn == s).sum() > 200))
    say('   by time to the next wave: ' + ', '.join(f'{n} {opp[m].mean() * 100:.0f}% ({m.sum():,})' for n, m in (('under 1 min', gap < 60), ('1-5 min', (gap >= 60) & (gap < 300)), ('5 min+', gap >= 300)) if m.sum() > 100))
    q = np.nanquantile(ampn, [1 / 3, 2 / 3])
    say('   by this wave\'s size: ' + ', '.join(f'{n} {opp[m].mean() * 100:.0f}%' for n, m in (('small', ampn <= q[0]), ('medium', (ampn > q[0]) & (ampn <= q[1])), ('big', ampn > q[1]))))

    # ---------------- 2: the ceiling
    say()
    say('2. THE CEILING: buying against the wave exactly at its peak (hindsight), $ per $100')
    pk = np.isfinite(remI) & (remI <= 2)
    for name, e in EX:
        say(line(f'at the peak, fade, sold {name}', fade[pk, e], GI[pk], right[pk]))
    say(line('at the peak, FOLLOW, sold 5 min', foll[pk, 1], GI[pk]))
    for s in ('tennis', 'football', 'basketball', 'cs2'):
        m = pk & (SPI == s)
        if m.sum() > 300:
            say(line(f'   {s}: at the peak, fade, sold 5 min', fade[m, 1], GI[m], right[m]))
    for name, m in (('10 s+ before the peak, fade (wrong moment)', np.isfinite(remI) & (remI >= 10)), ('after the peak, wave not over yet, fade', ~np.isfinite(remI))):
        say(line(name + ', 5 min', fade[m, 1], GI[m], right[m]))
    say(line('every in-wave row, fade, 5 min (no model)', fade[:, 1], GI, right))
    say(line('every in-wave row, follow, 5 min (no model)', foll[:, 1], GI))

    # ---------------- 3: the literal rule
    say()
    say('3. THE LITERAL RULE: buy against a wave at a fixed age since its alarm, sold after 5 min')
    for name, m in (('age 10-20 s', (ageI >= 10) & (ageI < 20)), ('age 25-35 s', (ageI >= 25) & (ageI < 35)), ('age 60 s+', ageI >= 60)):
        say(line(name + ', fade', fade[m, 1], GI[m], right[m]))

    # ---------------- 4: light models
    say()
    say('4. LIGHT MODELS (trained before 22 Nov, scored on 22-23 Nov; picks one position a game per 5 min)')
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    cut = d_ms('2026-11-22')
    FS = np.concatenate([X[I], S['H'][I], S['Q'][I]], 1)
    del X
    tr_all = np.flatnonzero(TI + 600_000 < cut)
    te = np.flatnonzero(TI >= cut)
    tr_all = tr_all[np.argsort(TI[tr_all], kind='stable')]
    c80 = int(0.8 * len(tr_all))
    va = tr_all[c80:]
    tr = tr_all[:c80]
    tr = tr[TI[tr] + 600_000 < TI[va].min()]
    rng = np.random.default_rng(0)
    if len(tr) > 400_000:
        tr = np.sort(rng.choice(tr, 400_000, replace=False))
    Xb = torch.from_numpy(LB.binned(FS, tr))
    tg = lambda v: torch.from_numpy(np.asarray(v))
    say(f'   rows: train {len(tr):,}, thresholds {len(va):,}, scored {len(te):,} ({len(np.unique(GI[te])):,} games)')
    peaked = ((~np.isfinite(remI)) | (remI <= 3)).astype(np.float32)
    targets = (('A "has it peaked"', peaked, 'logistic'), ('B "fading beats following"', right.astype(np.float32), 'logistic'))
    for name, yv, loss in targets:
        t1 = time.time()
        m = LB.GB(loss=loss, n_trees=80, patience=20, min_leaf=300).fit(Xb[tg(tr)], tg(yv)[tg(tr)], Xb[tg(va)], tg(yv)[tg(va)])
        pv, pt = m.predict(Xb[tg(va)]), m.predict(Xb[tg(te)])
        say(f'   MODEL {name}: {len(m.trees)} trees in {time.time() - t1:.0f} s; AUC on 22-23 Nov {LB.auc(pt, yv[te] > 0.5):.3f} (0.50 = no model); it is true in {yv[te].mean() * 100:.0f}% of rows, in {yv[te][pt >= np.quantile(pv, 0.95)].mean() * 100:.0f}% of its top 5%')
        for label, sel, pay_all in (('fade its top 5%', pt >= np.quantile(pv, 0.95), fade), ('FOLLOW its bottom 5%', pt <= np.quantile(pv, 0.05), foll)):
            pk_ = MN.place(TI, GI, te[sel])
            if len(pk_) < 30:
                say(f'      {label}: too few picks')
                continue
            ok_dir = right[pk_] if pay_all is fade else ~right[pk_]
            say('   ' + line(f'{label}, sold 5 min', pay_all[pk_, 1], GI[pk_], ok_dir))
            be = LB.breakeven(pay_all[pk_, 1], ok_dir)
            if be:
                say(f'         right way -> ${be[0]:+.2f}, wrong -> ${-be[1]:+.2f}: needs {be[2] * 100:.0f}% right, has {be[3] * 100:.0f}%' if np.isfinite(be[2]) else f'         loses even when right (${be[0]:+.2f})')
        pk0 = MN.place(TI, GI, te[rng.random(len(te)) < 0.05])
        say('   ' + line('no model: fade a random 5%', fade[pk0, 1], GI[pk0], right[pk0]))
    with open(os.path.join(a.signals, 'pilot_wavedir.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
