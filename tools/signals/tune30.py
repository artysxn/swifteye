"""Tuning the long-hold wave picks on the CORRECTED data, and a $40 wallet (python tools/signals/tune30.py --signals DIR).

Owner, 24 Nov: filters on the price paid (70 / 80 / 90c winning, 10-30c losing, maybe 50c), a trained veto, settings to
fine-tune, and a $40 wallet at $10 / $5 / $2 a bet or a fixed share of the wallet. NOTE: the "+$7.18 after 30 min" he
quotes was survivorship bias (AGENTS.md); everything here runs on waves2.npz as rebuilt with games that end inside
the hold valued at the token's final price.
  picks    boosted trees per day (trained on earlier days, 45-min gap) for dollars after 10 / 20 / 30 min; a pick =
           a wave start whose prediction is in the top q of the earlier days' predictions; one position a game.
  filters  fixed price bands on the 30-min model's top 3%.
  tuned    per day, the model's horizon, q, the lowest price allowed and the highest are chosen on the last 20% of the
           earlier days (total minus one standard error over games, 30+ trades, must be > 0), then applied to the day:
           this is the veto and the fine-tuning done without looking at the day.
  wallet   the trades in time order from $40: fixed $10 / $5 / $2, or 5 / 12.5 / 25% of the wallet; a bet needs free
           cash and at least 5 shares (Polymarket's minimum); P&L scales from the $100 order. Also the same trades in
           1,000 random orders."""
import argparse
import datetime as dt
import itertools
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mine as MN  # noqa: E402
import waves2 as W2  # noqa: E402

DAY = 86_400_000
HOLD = {2: 601_000, 7: 1_201_000, 8: 1_801_000}
HN = {2: '10 min', 7: '20 min', 8: '30 min'}
BANDS = ((0, .10), (.10, .30), (.30, .50), (.50, .70), (.70, .80), (.80, .90), (.90, 1.01))


def wallet(trades, mode, val, start=40.0):
    cash, open_, low, taken, skipped = start, [], start, 0, 0
    for t, hold, u, price in trades:
        for p in [p for p in open_ if p[0] <= t]:
            cash += p[2]
            open_.remove(p)
        low = min(low, cash)
        stake = val if mode == 'fixed' else val * cash
        if stake < 1 or stake > cash - sum(p[1] for p in open_) + 1e-9 or stake / price < 5:
            skipped += 1
            continue
        open_.append((t + hold, stake, u / 100.0 * stake))
        taken += 1
    cash += sum(p[2] for p in open_)
    return cash, min(low, cash), taken, skipped


def main():
    import torch
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    dev = 'cuda'
    D = np.load(os.path.join(a.signals, 'waves2.npz'))
    X, H, Wf, Y, TG, T, G, SP, d = D['X'], D['H'], D['W'], D['Y'], D['TG'], D['t'], D['game'].astype(np.int64), D['sport'], D['d']
    Xd = X.copy()
    Xd[d < 0] = MN.mirror(X[d < 0])
    FE = np.concatenate([Xd, H, Wf], 1)
    buy = TG[:, 4]
    okY = np.isfinite(Y).all(1) & np.isfinite(buy)
    d_ms = lambda s: int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    days = [x for x in sorted({dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in T[::200]}) if x >= '2026-11-19']
    rng = np.random.default_rng(0)
    b0 = np.flatnonzero(T < d_ms(days[0]))
    samp = rng.choice(b0, min(200_000, len(b0)), replace=False)
    Xb = np.zeros(FE.shape, np.uint8)
    for j in range(FE.shape[1]):
        e = np.unique(np.quantile(FE[samp, j], np.linspace(0, 1, 65)[1:-1]))
        Xb[:, j] = np.searchsorted(e, FE[:, j], side='right')
    Xg = torch.from_numpy(Xb).to(dev)
    Yc = torch.from_numpy(np.clip(np.nan_to_num(Y), -40, 40).astype(np.float32)).to(dev)
    tg = lambda v: torch.from_numpy(v).to(dev)
    out = []
    say = lambda s: (out.append(s), print(s, flush=True))

    def place(idx, hold):
        return MN.place(T, G, idx, hold=hold)
    ref, fixed, tuned, lines = [], {}, [], []
    FIX = (('all prices', 0, 1.01), ('50c and over', .5, 1.01), ('70c and over', .7, 1.01), ('80c and over', .8, 1.01), ('90c and over', .9, 1.01),
           ('70-90c', .7, .9), ('under 50c', 0, .5), ('everything except 10-30c', -1, -1))
    for dday in days:
        d0 = d_ms(dday)
        tr_all = np.flatnonzero(okY & (T + 2_700_000 < d0))
        today = np.flatnonzero(okY & (T >= d0) & (T < d0 + DAY))
        if len(tr_all) < 10_000 or len(today) < 500:
            continue
        idx = tr_all[np.argsort(T[tr_all], kind='stable')]
        cut = int(0.8 * len(idx))
        va, tr = idx[cut:], idx[:cut]
        tr = tr[T[tr] + 2_700_000 < T[va].min()]
        P = {}
        for j in (2, 7, 8):
            m = MN.Trees(depth=5, n_trees=300, min_leaf=100, lam=50.0, row_frac=0.5).fit(Xg[tg(tr)], Yc[tg(tr), j], Xg[tg(va)], Yc[tg(va), j])
            P[j] = (m.predict(Xg[tg(va)]).cpu().numpy(), m.predict(Xg[tg(today)]).cpu().numpy())
        pv, pt = P[8]
        top = pt >= np.quantile(pv, 0.97)
        ref += list(place(today[top], HOLD[8]))
        for name, lo, hi in FIX:
            mm = top & ((buy[today] >= lo) & (buy[today] < hi) if lo >= 0 else ~((buy[today] >= .1) & (buy[today] < .3)))
            fixed.setdefault(name, []).extend(place(today[mm], HOLD[8]))
        best = None
        for j, q, lo, hi in itertools.product((2, 7, 8), (0.90, 0.95, 0.97, 0.99), (0, .3, .5, .7, .8, .9), (1.01, .97, .9)):
            if hi <= lo:
                continue
            pv_j = P[j][0]
            tk = place(va[(pv_j >= np.quantile(pv_j, q)) & (buy[va] >= lo) & (buy[va] < hi)], HOLD[j])
            if len(tk) >= 30:
                s = MN.gstats(Y[tk, j], G[tk])
                if s['cautious'] > 0 and (best is None or s['cautious'] > best[0]):
                    best = (s['cautious'], j, q, lo, hi, len(tk), s['mean'])
        if best is None:
            lines.append(f'{dday[8:]} Nov: no setting made money on the earlier days: no trades')
            continue
        _, j, q, lo, hi, nv, mv = best
        pt_j = P[j][1]
        tk = place(today[(pt_j >= np.quantile(P[j][0], q)) & (buy[today] >= lo) & (buy[today] < hi)], HOLD[j])
        tuned += [(int(T[i]), HOLD[j], float(Y[i, j]), float(buy[i]), int(G[i])) for i in tk]
        lines.append(f'{dday[8:]} Nov: hold {HN[j]}, top {round((1 - q) * 100)}%, price {int(lo * 100)}-{min(int(hi * 100), 100)}c (earlier days: {nv} trades ${mv:+.2f} per $100) -> '
                     f'{len(tk)} trades ${Y[tk, j].sum():+,.0f}, ${Y[tk, j].mean() if len(tk) else 0:+.2f} per $100')
    ref = np.array(ref, np.int64)
    say('CORRECTED DATA (games that end inside the hold are counted). The 30-minute model\'s top 3% picks, one position a game, by the price paid:')
    for lo, hi in BANDS:
        mm = (buy[ref] >= lo) & (buy[ref] < hi)
        if mm.sum() >= 5:
            say(f'   {int(lo * 100):>2}-{min(int(hi * 100), 100):<3}c {mm.sum():>5} trades  ${Y[ref[mm], 8].mean():+6.2f} per $100  win {np.mean(Y[ref[mm], 8] > 0) * 100:.0f}%')
    say('FIXED PRICE FILTERS on those picks (each filter re-places its own one-a-game trades):')
    for name, _, _ in FIX:
        pk = np.array(fixed.get(name, []), np.int64)
        say(MN.fmt(name, MN.gstats(Y[pk, 8], G[pk]) if len(pk) else None))
    say('TUNED ON EARLIER DAYS ONLY (hold, threshold, price range = the veto and the fine-tuning, chosen without the day):')
    for ln in lines:
        say('   ' + ln)
    say(MN.fmt('pooled, tuned', MN.gstats([x[2] for x in tuned], [x[4] for x in tuned]) if tuned else None))
    sets = {'the tuned trades': sorted((x[0], x[1], x[2], x[3]) for x in tuned),
            'top 3%, 80c and over, 30 min': sorted((int(T[i]), HOLD[8], float(Y[i, 8]), float(buy[i])) for i in fixed.get('80c and over', []))}
    for sname, tr_ in sets.items():
        if not tr_:
            continue
        say(f'$40 WALLET on {sname} ({len(tr_)} signals over {len(days)} days):')
        for lab, mode, val in (('$10 a bet', 'fixed', 10.0), ('$5 a bet', 'fixed', 5.0), ('$2 a bet', 'fixed', 2.0), ('5% of the wallet', 'pct', 0.05), ('12.5% of the wallet', 'pct', 0.125), ('25% of the wallet', 'pct', 0.25)):
            end, low, taken, skipped = wallet(tr_, mode, val)
            r2 = np.random.default_rng(5)
            ends = []
            for _ in range(1000):
                perm = r2.permutation(len(tr_))
                ends.append(wallet([(tr_[k][0], tr_[k][1], tr_[perm[k]][2], tr_[perm[k]][3]) for k in range(len(tr_))], mode, val)[0])
            ends = np.array(ends)
            say(f'   {lab:<20} ends at ${end:6.2f} (lowest point ${low:6.2f}); bets placed {taken}, not placed {skipped} (no free cash or under 5 shares); '
                f'in 1,000 random orders: ${np.quantile(ends, 0.05):.2f}..${np.quantile(ends, 0.95):.2f}, ends above $40 in {np.mean(ends > 40) * 100:.0f}%')
    with open(os.path.join(a.signals, 'tune30.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
