"""Inside the 30%: bookmaker jumps Polymarket had NOT followed yet. Which are worth trading?
(python tools/signals/faster_slices.py --signals DIR; needs faster_events.npz from faster.py)

For the decision moments of faster.py (0 = an ideal feed at the bookmaker's publish time + 0.25 s, 2 = today's bot at
our receive time + 1 s), over the ELIGIBLE jumps (Polymarket had not moved the jump's way yet):
  1. how many Polymarket then followed (made >= half the bookmaker's jump within 1 min), and what those made vs the
     rest (a perfect picker's ceiling: it uses the future, so it is NOT a strategy);
  2. dollars per $100 order by what is known at the decision: other bookmaker families already moved the same way
     (confirmations), spread, token price, jump size, sport, bookmaker;
  3. CLEAN: per day 19-23 Nov, a filter (min jump, min confirmations, max spread, price band, sell into the bid or a
     limit sale) chosen on the days before by total minus one standard error (>= 30 trades, > 0), applied to the day;
     and a ridge model on the same inputs with its threshold chosen on the last 20% before the day. One position per
     game, 5 min each."""
import argparse
import datetime as dt
import itertools
import os
import sys

import numpy as np

DAY = 86_400_000


def cautious(v):
    v = np.asarray(v, float)
    return v.sum() - v.std() * np.sqrt(len(v)) if len(v) > 1 else -np.inf


def place(t, g, idx):
    busy, out = {}, []
    for i in idx[np.argsort(t[idx], kind='stable')]:
        if t[i] < busy.get(int(g[i]), -1):
            continue
        busy[int(g[i])] = int(t[i]) + 300_000
        out.append(i)
    return np.array(out, np.int64)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    E = dict(np.load(os.path.join(a.signals, 'faster_events.npz'), allow_pickle=False))
    t, g = E['t'], E['game']
    sports = sorted(set(E['sport'].tolist()))
    books = sorted(set(E['book'].tolist()))
    out = []
    for k, lab in ((0, 'IDEAL FEED (bookmaker publish time + 0.25 s)'), (2, 'TODAY (our receive time + 1 s)')):
        el = E[f'ok{k}'] & (E[f'eligible{k}'] == 1)
        uT, uM = E[f'usd_T{k}'], E[f'usd_M{k}']
        fol = E[f'f60{k}'] >= 0.5
        out.append(f'== {lab}: {el.sum()} eligible jumps (Polymarket had not followed yet) of {E[f"ok{k}"].sum()}')
        out.append(f'   Polymarket then made >= half the bookmaker\'s jump within 1 min: {np.mean(fol[el]) * 100:.0f}%')
        out.append(f'   all eligible: ${uT[el].mean():+.2f} per $100 (sell into the bid), ${uM[el].mean():+.2f} (limit sale)')
        out.append(f'   the ones PM followed: ${uT[el & fol].mean():+.2f} / ${uM[el & fol].mean():+.2f}; the rest ${uT[el & ~fol].mean():+.2f} '
                   f'(knowing in advance which PM follows would make ${uT[el & fol].sum():+,.0f} over {np.sum(el & fol)} trades: the ceiling, not a strategy)')
        out.append('   by what is known at the decision ($ per $100 sell into the bid / limit sale, n, PM followed %):')
        conf, sp, pr, jp = E[f'confirm{k}'], E[f'spread{k}'], E[f'price{k}'], E['jump']
        groups = [('confirmations', [(f'{c}', conf == c) for c in (0, 1, 2)] + [('3+', conf >= 3)]),
                  ('spread', [('<= 1c', sp <= 1.01), ('1-2c', (sp > 1.01) & (sp <= 2.01)), ('> 2c', sp > 2.01)]),
                  ('token price', [('< 20c', pr < 0.2), ('20-80c', (pr >= 0.2) & (pr <= 0.8)), ('> 80c', pr > 0.8)]),
                  ('jump', [('0.12-0.3', jp < 0.3), ('0.3-0.7', (jp >= 0.3) & (jp < 0.7)), ('0.7+', jp >= 0.7)]),
                  ('sport', [(s, E['sport'] == s) for s in sports]), ('bookmaker', [(b, E['book'] == b) for b in books])]
        for name, cells in groups:
            parts = []
            for cl, m in cells:
                mm = el & m
                if mm.sum() >= 100:
                    parts.append(f'{cl} ${uT[mm].mean():+.2f}/${uM[mm].mean():+.2f} ({mm.sum()}, {np.mean(fol[mm]) * 100:.0f}%)')
            out.append(f'     {name:<14} ' + '; '.join(parts))
        # best combined cells (exploratory, in-sample)
        combos = []
        for (cn, cm), (sn, sm), (pn, pm_), (jn, jm) in itertools.product(
                [('conf>=0', conf >= 0), ('conf>=1', conf >= 1), ('conf>=2', conf >= 2), ('conf>=3', conf >= 3)],
                [('spread<=1', sp <= 1.01), ('spread<=2', sp <= 2.01), ('any spread', sp >= 0)],
                [('20-80c', (pr >= 0.2) & (pr <= 0.8)), ('any price', pr >= 0)],
                [('jump>=0.12', jp >= 0.12), ('jump>=0.3', jp >= 0.3), ('jump>=0.7', jp >= 0.7)]):
            mm = el & cm & sm & pm_ & jm
            if mm.sum() >= 200:
                combos.append((max(uT[mm].mean(), uM[mm].mean()), f'{cn}, {sn}, {pn}, {jn}', mm.sum(), uT[mm].mean(), uM[mm].mean()))
        combos.sort(reverse=True)
        out.append('   best combined slices with >= 200 jumps (chosen looking at all days: exploratory):')
        for _, name, n, a_, b_ in combos[:6]:
            out.append(f'     {name:<48} {n:>6}  ${a_:+.2f} / ${b_:+.2f}')
        # clean day by day: filter and ridge
        rows_f, rows_r, lines = [], [], []
        feats = lambda idx: np.stack([jp[idx], E['size'][idx] / 10, np.minimum(conf[idx], 5), sp[idx], pr[idx], pr[idx] * (1 - pr[idx]), E['delay'][idx] / 1000] +
                                     [(E['sport'][idx] == s).astype(float) for s in sports] + [(E['book'][idx] == b).astype(float) for b in books], 1)
        for day in range(19, 24):
            d0 = int(dt.datetime(2026, 11, day, tzinfo=dt.timezone.utc).timestamp() * 1000)
            d1 = d0 + DAY
            before = np.flatnonzero(el & (t + 900_000 < d0))
            today = np.flatnonzero(el & (t >= d0) & (t < d1))
            best = None
            for mj, mc, ms, (plo, phi), ex in itertools.product((0.12, 0.3, 0.7), (0, 1, 2, 3), (1.01, 2.01, 99), ((0.0, 1.0), (0.2, 0.8)), ('T', 'M')):
                f = lambda idx: (jp[idx] >= mj) & (conf[idx] >= mc) & (sp[idx] <= ms) & (pr[idx] >= plo) & (pr[idx] <= phi)
                tk = place(t, g, before[f(before)])
                if len(tk) < 30:
                    continue
                v = E[f'usd_{ex}{k}'][tk]
                sc = cautious(v)
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, mj, mc, ms, plo, phi, ex, f)
            if best is None:
                lines.append(f'     {day} Nov: filter: nothing made money before (cautious), no trades')
            else:
                _, mj, mc, ms, plo, phi, ex, f = best
                tk = place(t, g, today[f(today)])
                rows_f += [(int(g[i]), float(E[f'usd_{ex}{k}'][i])) for i in tk]
                lines.append(f'     {day} Nov: filter jump>={mj} conf>={mc} spread<={ms:.0f}c price {plo:.1f}-{phi:.1f} exit {ex}: {len(tk)} trades ${np.sum(E[f"usd_{ex}{k}"][tk]):+.0f}')
            # ridge on the same inputs
            order = before[np.argsort(t[before])]
            cut = int(0.8 * len(order))
            tr, va = order[:cut], order[cut:]
            if len(tr) > 500:
                X = feats(tr)
                mu, sd = X.mean(0), X.std(0)
                sd[sd < 1e-9] = 1
                Z = lambda idx: np.concatenate([(feats(idx) - mu) / sd, np.ones((len(idx), 1))], 1)
                y = np.clip(uT[tr], -60, 60)
                A = Z(tr)
                w = np.linalg.solve(A.T @ A + 10 * np.eye(A.shape[1]), A.T @ y)
                pv, ptd = Z(va) @ w, Z(today) @ w if len(today) else np.zeros(0)
                bt = None
                for tau in (-4, -2, 0, 1, 2, 4, 6, 10):
                    tk = place(t, g, va[pv >= tau])
                    if len(tk) >= 30 and cautious(uT[tk]) > 0 and (bt is None or cautious(uT[tk]) > bt[0]):
                        bt = (cautious(uT[tk]), tau)
                if bt is not None and len(today):
                    tk = place(t, g, today[ptd >= bt[1]])
                    rows_r += [(int(g[i]), float(uT[i])) for i in tk]
                    lines[-1] += f'; ridge >= ${bt[1]:+.0f}: {len(tk)} trades ${np.sum(uT[tk]):+.0f}'
                else:
                    lines[-1] += '; ridge: nothing passed on validation'
        out.append('   CLEAN, day by day (everything chosen on earlier days):')
        out += lines
        for name, rows in (('filter', rows_f), ('ridge', rows_r)):
            if rows:
                v = np.array([r[1] for r in rows])
                gg = np.array([r[0] for r in rows])
                ug, inv = np.unique(gg, return_inverse=True)
                gs = np.bincount(inv, v)
                rng = np.random.default_rng(5)
                boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(4000)]
                out.append(f'     pooled {name}: {len(v)} trades, ${v.sum():+,.0f} (90% {np.quantile(boot, 0.05):+,.0f}..{np.quantile(boot, 0.95):+,.0f}), ${v.mean():+.2f} a trade, win {np.mean(v > 0) * 100:.0f}%')
            else:
                out.append(f'     pooled {name}: no trades')
        out.append('')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'faster_slices.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
