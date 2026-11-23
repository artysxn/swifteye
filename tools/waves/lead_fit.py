"""Fit and score the lead state and trend waves (lead.py) on every recorded game.

  python tools/waves/lead_fit.py [--db PATH] [--days N] [--workers 16]

Writes <db dir>/waves/lead_report.txt and lead_fit.json:
* Polymarket's response to the gap, per state: OLS (through 0) of x(t+h) - x(t) on e(t), beta_h, for h = 5..120 s,
  and the fit beta_h = pass * (1 - exp(-kappa h)) for superline-led gaps (the numbers lead.py and waves.js use);
  the superline's own response (does it move back toward Polymarket instead?).
* How often a superline-led gap was right: Polymarket moved half the gap its way within 60 s of the gap opening.
* Trend waves: how many per hour on each line, how many superline waves Polymarket followed, the lead
  (Polymarket's onset - the superline's) and the detection lead (alarm - alarm)."""
import argparse
import json
import math
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead as L  # noqa: E402
import waves as W  # noqa: E402

H = (5, 10, 20, 30, 60, 120)
GROUPS = ('ahead', 'pm_ahead', 'level', 'stale')


def game_series(pm_rows, bk_rows, outcome):
    """(ts, pm, sl) on the 1 s grid over the span both exist, or None."""
    pm = W.pm_line(pm_rows, outcome)
    books = W.book_lines(bk_rows, outcome)
    if not pm or not books:
        return None
    sl = [(t, p) for t, p, _ in W.superline(books)]
    t0 = max(pm[0][0], sl[0][0])
    t1 = min(pm[-1][0], sl[-1][0])
    if t1 - t0 < 600_000:
        return None
    ts, gp = L.grid(pm, t0, t1)
    _, gs = L.grid(sl, t0, t1)
    return ts, gp, gs


def one(args):
    db_path, m, sport = args
    db = W.connect(db_path)
    pm_rows, bk_rows = W.load(db, m)
    outs = ['home', 'away'] if sport in W.THREE_WAY else ['home']
    acc = {g: {'n': 0, 'ee': 0.0, **{f'xe{h}': 0.0 for h in H}, **{f'ye{h}': 0.0 for h in H}} for g in GROUPS}
    opened = {'n': 0, 'right': 0, 'wrong': 0}
    tr = {'sl': 0, 'pm': 0, 'sl_followed': 0, 'leads': [], 'alarm_leads': [], 'pm_preceded': 0, 'hours': 0.0}
    secs = {g: 0 for g in GROUPS}
    for o in outs:
        g = game_series(pm_rows, bk_rows, o)
        if g is None:
            continue
        ts, gp, gs = g
        r = L.lead_state(gp, gs)
        x, y = L.logit(gp), L.logit(gs)
        st, e = r['state'], r['e']
        n = len(ts)
        tr['hours'] += n / 3600
        grp = np.full(n, -1)
        grp[(st == L.AHEAD_UP) | (st == L.AHEAD_DOWN)] = 0
        grp[st == L.PM_AHEAD] = 1
        grp[st == L.LEVEL] = 2
        grp[st == L.STALE] = 3
        for gi, name in enumerate(GROUPS):
            idx = np.where(grp == gi)[0]
            secs[name] += len(idx)
            idx = idx[idx < n - max(H)]
            idx = idx[np.isfinite(e[idx])]
            if not len(idx):
                continue
            a = acc[name]
            a['n'] += len(idx)
            a['ee'] += float((e[idx] ** 2).sum())
            for h in H:
                dx = x[idx + h] - x[idx]
                dy = y[idx + h] - y[idx]
                ok = np.isfinite(dx) & np.isfinite(dy)
                a[f'xe{h}'] += float((dx[ok] * e[idx][ok]).sum())
                a[f'ye{h}'] += float((dy[ok] * e[idx][ok]).sum())
        # Each superline-led gap as it opened: did Polymarket move half of it its way within 60 s?
        ahead = (st == L.AHEAD_UP) | (st == L.AHEAD_DOWN)
        starts = np.where(ahead[1:] & ~ahead[:-1])[0] + 1
        for i in starts:
            if i + 60 >= n or not np.isfinite(e[i]):
                continue
            opened['n'] += 1
            mv = (x[i + 1:i + 61] - x[i]) * np.sign(e[i])
            mv = mv[np.isfinite(mv)]
            if len(mv) and mv.max() >= 0.5 * abs(e[i]):
                opened['right'] += 1
            elif len(mv) and mv.min() <= -0.5 * abs(e[i]):
                opened['wrong'] += 1
        se, _, _ = L.trends(gs)
        pe, _, _ = L.trends(gp)
        tr['sl'] += len(se)
        tr['pm'] += len(pe)
        for pr in L.pair_trends(se, pe):
            if pr['pm'] is not None:
                tr['sl_followed'] += 1
                tr['leads'].append(pr['lead'])
                tr['alarm_leads'].append(pr['alarm_lead'])
        for q in pe:
            if any(s['dir'] == q['dir'] and 0 <= q['on'] - s['on'] <= 180 for s in se):
                tr['pm_preceded'] += 1
    return acc, opened, tr, secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--days', type=float)
    ap.add_argument('--workers', type=int, default=16)
    a = ap.parse_args()
    path = a.db or W.default_db()
    db = W.connect(path)
    since = int((time.time() - a.days * 86400) * 1000) if a.days else None
    gl = W.games(db, since)
    print(f'{len(gl)} games', flush=True)
    t0 = time.time()
    tot = {g: {} for g in GROUPS}
    opened = {'n': 0, 'right': 0, 'wrong': 0}
    tr = {'sl': 0, 'pm': 0, 'sl_followed': 0, 'leads': [], 'alarm_leads': [], 'pm_preceded': 0, 'hours': 0.0}
    secs = {g: 0 for g in GROUPS}
    with Pool(a.workers) as pool:
        for k, (acc, op, t, sc) in enumerate(pool.imap_unordered(one, [(path, m, s) for m, s, *_ in gl], chunksize=4)):
            for g in GROUPS:
                for key, v in acc[g].items():
                    tot[g][key] = tot[g].get(key, 0) + v
                secs[g] += sc[g]
            for key in opened:
                opened[key] += op[key]
            for key in ('sl', 'pm', 'sl_followed', 'pm_preceded', 'hours'):
                tr[key] += t[key]
            tr['leads'] += t['leads']
            tr['alarm_leads'] += t['alarm_leads']
            if (k + 1) % 100 == 0:
                print(f'  {k + 1} games, {time.time() - t0:.0f} s', flush=True)

    beta = {g: {h: (tot[g][f'xe{h}'] / tot[g]['ee'] if tot[g].get('ee') else None) for h in H} for g in GROUPS}
    beta_sl = {g: {h: (tot[g][f'ye{h}'] / tot[g]['ee'] if tot[g].get('ee') else None) for h in H} for g in GROUPS}
    # beta_h = pass * (1 - exp(-kappa h)) for superline-led gaps: least squares over a kappa grid.
    best = None
    bh = np.array([beta['ahead'][h] or 0 for h in H])
    for kappa in np.exp(np.linspace(math.log(0.002), math.log(1.0), 400)):
        f = 1 - np.exp(-kappa * np.array(H))
        pas = float((f * bh).sum() / (f * f).sum())
        err = float(((bh - pas * f) ** 2).sum())
        if best is None or err < best[0]:
            best = (err, float(kappa), pas)
    _, kappa, pas = best
    total_s = sum(secs.values()) or 1
    lead = np.array(tr['leads'], dtype=float)
    al = np.array(tr['alarm_leads'], dtype=float)
    out = {
        'games': len(gl), 'hours': tr['hours'], 'seconds_share': {g: secs[g] / total_s for g in GROUPS},
        'beta_pm': beta, 'beta_superline': beta_sl, 'fit': {'kappa': kappa, 'pass': pas, 'half_s': math.log(2) / kappa},
        'ahead_opened': opened,
        'trends': {'sl_per_hour': tr['sl'] / max(tr['hours'], 1e-9), 'pm_per_hour': tr['pm'] / max(tr['hours'], 1e-9),
                   'sl_followed': tr['sl_followed'] / max(tr['sl'], 1), 'pm_preceded': tr['pm_preceded'] / max(tr['pm'], 1),
                   'lead_median_s': float(np.median(lead)) if len(lead) else None, 'lead_first_share': float((lead > 0).mean()) if len(lead) else None,
                   'alarm_lead_median_s': float(np.median(al)) if len(al) else None, 'alarm_first_share': float((al > 0).mean()) if len(al) else None},
        'params': {'lead': L.LEAD, 'trend': L.TREND},
    }
    od = os.path.join(os.path.dirname(path), 'waves')
    os.makedirs(od, exist_ok=True)
    with open(os.path.join(od, 'lead_fit.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1)
    pct = lambda v: '-' if v is None else f'{100 * v:.0f}%'
    lines = [f"{len(gl)} games, {tr['hours']:.0f} outcome-hours on a 1 s grid. Time in each state: " + ', '.join(f'{g} {pct(secs[g] / total_s)}' for g in GROUPS), '',
             "Polymarket's later move per unit of gap (OLS of x(t+h)-x(t) on e(t)), by state at t; then the superline's:",
             '  h (s)        ' + ''.join(f'{h:>8}' for h in H)]
    for g in GROUPS:
        lines.append(f'  PM  {g:<9}' + ''.join(f"{(beta[g][h] or 0):>8.3f}" for h in H))
    for g in GROUPS:
        lines.append(f'  SL  {g:<9}' + ''.join(f"{(beta_sl[g][h] or 0):>8.3f}" for h in H))
    lines += ['', f'Fit for superline-led gaps: Polymarket closes pass = {pas:.2f} of the gap, at kappa = {kappa:.4f}/s (half of that in {math.log(2) / kappa:.0f} s).',
              f"Superline-led gaps opened: {opened['n']}; Polymarket moved half the gap its way within 60 s in {pct(opened['right'] / max(opened['n'], 1))}, "
              f"half the gap the other way first in {pct(opened['wrong'] / max(opened['n'], 1))}.", '',
              f"Trend waves: superline {out['trends']['sl_per_hour']:.1f}/h, Polymarket {out['trends']['pm_per_hour']:.1f}/h. "
              f"Polymarket followed {pct(out['trends']['sl_followed'])} of superline waves (same direction, onset -60..+180 s); "
              f"{pct(out['trends']['pm_preceded'])} of Polymarket's waves had a superline wave start 0..180 s before.",
              f"Paired waves: median onset lead {out['trends']['lead_median_s']} s (superline first in {pct(out['trends']['lead_first_share'])}); "
              f"median detection lead {out['trends']['alarm_lead_median_s']} s (superline detected first in {pct(out['trends']['alarm_first_share'])})."]
    with open(os.path.join(od, 'lead_report.txt'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    sys.stdout.reconfigure(encoding='utf-8')
    print('\n'.join(lines))
    print(f'written to {od}')


if __name__ == '__main__':
    main()
