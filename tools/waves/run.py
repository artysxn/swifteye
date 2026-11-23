"""Waves over every recorded game: per-source delay and correlation against Polymarket, and a check against the
owner's painted marks (Trade review). Read-only on the database.

usage: python tools/waves/run.py [--db PATH] [--days N] [--sport S] [--games N] [--clock receive|source] [--boot B]
  writes <db dir>/waves/{scores.json, waves.jsonl, report.txt}

Columns of the report (one row per source, on the chosen clock):
  waves      waves it took part in            first   share of those it was the first of ALL sources to move
  <PM       share of the waves where both moved in which it moved first
  noPM      share of its waves in which Polymarket made no wave of its own (it may still have drifted)
  lead       median of (Polymarket's onset - its onset) when both moved, s, with a 90% range from resampling games
  >2.2s      share where it led Polymarket by at least our buy latency
  precision  share of its moves made before Polymarket after which Polymarket made half of the move within 60 s
  tradable   share of all its moves that were both: >= 2.2 s before Polymarket AND followed (the useful ones)
  catch-up   share where Polymarket had ALREADY made half the wave when it moved (it was following)
  pass       Polymarket's move per unit of its move (least squares through 0), its leading moves only
  recall     share of Polymarket's waves (in games it priced) that it also showed within 2 minutes
  delay      relative delay from the two-way model over all sources, s (negative = earlier than Polymarket)
  HY lag/ρ   Hayashi-Yoshida lead-lag on log-odds increments: lag of the strongest co-movement (s, + = it leads)
             and the correlation there; ρ0 = at zero lag
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waves as W  # noqa: E402


def pct(x):
    return '-' if x is None else f'{100 * x:.0f}%'


def secs(x):
    return '-' if x is None else f'{x / 1000:+.1f}'


def rng90(vals, fn):
    v = [x for x in vals if x is not None]
    return None if not v else (fn(np.percentile(v, 5)), fn(np.percentile(v, 95)))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--days', type=float)
    ap.add_argument('--sport')
    ap.add_argument('--games', type=int)
    ap.add_argument('--clock', default='receive', choices=['receive', 'source'])
    ap.add_argument('--boot', type=int, default=300)
    ap.add_argument('--out')
    a = ap.parse_args()
    path = a.db or W.default_db()
    out = a.out or os.path.join(os.path.dirname(path), 'waves')
    os.makedirs(out, exist_ok=True)
    db = W.connect(path)
    since = int((time.time() - a.days * 86400) * 1000) if a.days else None
    gl = W.games(db, since, a.sport)
    if a.games:
        gl = gl[-a.games:]
    print(f'{len(gl)} games, clock: {a.clock}', flush=True)

    rows, waves = [], []
    present = defaultdict(set)            # (game, outcome) -> sources with a line
    hy = defaultdict(dict)                # source -> game -> (U, vb, vp)
    t0 = time.time()
    for k, (m, sport, home, away, start) in enumerate(gl):
        pm_rows, bk_rows = W.load(db, m)
        outs = ['home', 'away'] if sport in W.THREE_WAY else ['home']
        for o in outs:
            g = W.game_waves(pm_rows, bk_rows, o, a.clock)
            if not g['pm']:
                continue
            present[(m, o)] = set(g['books']) | {W.PM} | ({W.SUPER} if g['superline'] else set())
            for r in g['rows']:
                r.update(game=m, outcome=o, sport=sport)
                rows.append(r)
            for w in g['waves']:
                w.update(game=m, outcome=o, sport=sport, home=home, away=away)
                waves.append(w)
            for b, line in list(g['books'].items()) + ([(W.SUPER, g['superline'])] if g['superline'] else []):
                h = W.hy_contrast(line, g['pm'])
                if h:
                    U0, vb0, vp0 = hy[b].get(m, (0, 0.0, 0.0))
                    hy[b][m] = (U0 + h[0], vb0 + h[1], vp0 + h[2])
        if (k + 1) % 100 == 0:
            print(f'  {k + 1} games, {len(waves)} waves, {time.time() - t0:.0f} s', flush=True)

    games_all = sorted({r['game'] for r in rows})
    by_src = defaultdict(list)
    for r in rows:
        by_src[r['src']].append(r)
    rng = np.random.default_rng(11)
    boots = [rng.choice(games_all, len(games_all)) for _ in range(a.boot)] if games_all else []

    def stats(rs):
        lead = [r['lead'] for r in rs if r['lead'] is not None]
        led = [r for r in rs if r['led_pm'] and r['followed'] is not None]
        cu = [r for r in rs if r['pm_done_before'] is not None]
        pp = [r for r in rs if r['led_pm'] and r['pm_gain'] is not None]
        return {
            'waves': len(rs), 'first': np.mean([r['first'] for r in rs]) if rs else None,
            'before_pm': np.mean([x > 0 for x in lead]) if lead else None,
            'pm_none': np.mean([r['lead'] is None for r in rs]) if rs else None,
            'lead_ms': float(np.median(lead)) if lead else None,
            'ahead_exec': np.mean([x >= W.EXEC_MS for x in lead]) if lead else None,
            'precision': np.mean([r['followed'] for r in led]) if led else None,
            'tradable': np.mean([(r['lead'] is None or r['lead'] >= W.EXEC_MS) and r['led_pm'] and bool(r['followed']) for r in rs]) if rs else None,
            'catch_up': np.mean([r['pm_done_before'] >= 0.5 for r in cu]) if cu else None,
            'pass': (sum(r['pm_gain'] * r['amp'] for r in pp) / sum(r['amp'] ** 2 for r in pp)) if pp else None,
        }

    delta = W.two_way_delays(waves)
    # Resampled whole games (with replacement): 90% ranges. Rows become arrays once; a resample is a
    # concatenation of each picked game's row indices.
    wave_games = defaultdict(list)
    for w in waves:
        wave_games[w['game']].append(w)
    boot_delta = [W.two_way_delays([w for gm in b for w in wave_games.get(gm, [])], iters=12) for b in boots[: max(1, a.boot // 10)]]

    scores = {}
    for s, rs in by_src.items():
        st = stats(rs)
        lead = np.array([np.nan if r['lead'] is None else r['lead'] for r in rs], dtype=float)
        fol = np.array([np.nan if r['followed'] is None else float(r['followed']) for r in rs], dtype=float)
        ledpm = np.array([r['led_pm'] for r in rs], dtype=bool)
        trad = np.array([(r['lead'] is None or r['lead'] >= W.EXEC_MS) and r['led_pm'] and bool(r['followed']) for r in rs], dtype=float)
        gidx = defaultdict(list)
        for i, r in enumerate(rs):
            gidx[r['game']].append(i)
        gidx = {g: np.array(v) for g, v in gidx.items()}
        bl, bp, bt = [], [], []
        for b in boots:
            parts = [gidx[g] for g in b if g in gidx]
            if not parts:
                continue
            idx = np.concatenate(parts)
            L = lead[idx]
            bl.append(float(np.nanmedian(L)) if np.isfinite(L).any() else None)
            F = fol[idx][ledpm[idx]]
            bp.append(float(np.nanmean(F)) if np.isfinite(F).any() else None)
            bt.append(float(trad[idx].mean()))
        # Recall: Polymarket's waves in games this source priced, that it also showed.
        pmw = [w for w in waves if w['pm_on'] is not None and s in present[(w['game'], w['outcome'])]]
        st['recall'] = np.mean([s in w['on'] for w in pmw]) if pmw else None
        st['delay_ms'] = delta.get(s)
        st['delay_range'] = rng90([d.get(s) for d in boot_delta], float)
        st['lead_range'] = rng90(bl, float)
        st['precision_range'] = rng90(bp, float)
        st['tradable_range'] = rng90(bt, float)
        H = hy.get(s, {})
        if H:
            U = sum(v[0] for v in H.values())
            vb = sum(v[1] for v in H.values())
            vp = sum(v[2] for v in H.values())
            i = int(np.argmax(np.abs(U)))
            z = int(np.argmin(np.abs(W.THETAS)))
            den = np.sqrt(vb * vp) if vb > 0 and vp > 0 else None
            st['hy_lag_ms'] = float(W.THETAS[i])
            gk = list(H)
            lags = []
            for _ in range(200):
                pick = rng.choice(len(gk), len(gk))
                Ub = sum(H[gk[j]][0] for j in pick)
                lags.append(float(W.THETAS[int(np.argmax(np.abs(Ub)))]))
            st['hy_lag_range'] = rng90(lags, float)
            st['hy_rho'] = float(U[i] / den) if den else None
            st['hy_rho0'] = float(U[z] / den) if den else None
        st['family'] = W.family(s)
        scores[s] = {k: (float(v) if isinstance(v, (np.floating, np.bool_)) else v) for k, v in st.items()}

    # ---- the owner's painted marks: did the detector find the same waves and the same delay?
    marks = check_marks(db, a.clock)

    # ---- report
    lines = []
    n_led_pm = sum(1 for w in waves if w['leader'] == W.PM)
    with_both = [w for w in waves if w['pm_on'] is not None and len(w['on']) > 1]
    lines.append(f'{len(games_all)} games, {len(waves)} waves ({a.clock} clock). Polymarket moved first in '
                 f'{pct(n_led_pm / len(waves) if waves else None)} of all waves; in {pct(np.mean([w["leader"] == W.PM for w in with_both]) if with_both else None)} '
                 f'of the {len(with_both)} waves where it and a bookmaker both moved.')
    lines.append('')
    head = f'{"source":<13}{"fam":<9}{"waves":>6}{"first":>7}{"<PM":>6}{"noPM":>6}{"lead s":>8}{"(90%)":>14}{">2.2s":>7}{"prec":>6}{"(90%)":>11}{"trad":>6}{"catchup":>8}{"pass":>6}{"recall":>7}{"delay s":>9}{"HYlag":>7}{"(90%)":>12}{"ρ":>6}{"ρ0":>6}'
    lines.append(head)
    num = lambda x: '-' if x is None else f'{x:.2f}'
    hy_rng = lambda r: '-' if not r else f'{r[0] / 1000:+.1f}..{r[1] / 1000:+.1f}'
    for s, st in sorted(scores.items(), key=lambda x: -x[1]['waves']):
        if st['waves'] < 5:
            continue
        lr, pr = st['lead_range'], st['precision_range']
        lead_rng = f'{lr[0] / 1000:+.0f}..{lr[1] / 1000:+.0f}' if lr else '-'
        prec_rng = f'{100 * pr[0]:.0f}-{100 * pr[1]:.0f}%' if pr else '-'
        lines.append(f'{s:<13}{st["family"]:<9}{st["waves"]:>6}{pct(st["first"]):>7}{pct(st["before_pm"]):>6}{pct(st["pm_none"]):>6}{secs(st["lead_ms"]):>8}'
                     f'{lead_rng:>14}{pct(st["ahead_exec"]):>7}{pct(st["precision"]):>6}{prec_rng:>11}{pct(st["tradable"]):>6}'
                     f'{pct(st["catch_up"]):>8}{num(st["pass"]):>6}{pct(st["recall"]):>7}{secs(st["delay_ms"]):>9}'
                     f'{secs(st.get("hy_lag_ms")):>7}{hy_rng(st.get("hy_lag_range")):>12}{num(st.get("hy_rho")):>6}{num(st.get("hy_rho0")):>6}')
    lines.append('')
    lines.append('Painted marks (Trade review): the detector against what was painted')
    for mk in marks:
        lines.append('  ' + mk['text'])
    errs = [abs(mk['found_ms'] - mk['painted_start_ms']) for mk in marks if mk['found_ms'] is not None]
    if errs:
        signs = sum((mk['found_ms'] > 0) == (mk['painted_start_ms'] > 0) for mk in marks if mk['found_ms'] is not None)
        lines.append(f'  {len(errs)} of {len(marks)} painted pairs found; median gap to the painted start-to-start delay {np.median(errs) / 1000:.1f} s; '
                     f'same order (who first) in {signs} of {len(errs)}')
    report = '\n'.join(lines)
    print('\n' + report)
    with open(os.path.join(out, 'report.txt'), 'w', encoding='utf-8') as f:
        f.write(report + '\n')
    with open(os.path.join(out, 'scores.json'), 'w', encoding='utf-8') as f:
        json.dump({'clock': a.clock, 'games': len(games_all), 'waves': len(waves), 'params': {k: getattr(W, k) for k in ('MAX_SPREAD', 'DEADBAND_PM', 'DEADBAND_BOOK', 'JOIN_MS', 'MIN_MOVE', 'MIN_LOGIT', 'MAX_LEN', 'HOLD', 'WINDOW', 'FOLLOW', 'EXEC_MS')},
                   'sources': scores, 'two_way_delay_ms': delta, 'marks': marks}, f, indent=1, default=float)
    with open(os.path.join(out, 'waves.jsonl'), 'w', encoding='utf-8') as f:
        for w in waves:
            f.write(json.dumps({k: v for k, v in w.items()}, default=float) + '\n')
    print(f'\nwritten to {out}')


def check_marks(db, clock):
    """Each painted pair: the detector's waves for that game, the bookmaker's and Polymarket's onsets nearest
    the painted lines, and the delay found against the painted one."""
    out = []
    try:
        labels = db.execute('SELECT id, match_id, outcome, data FROM trade_labels').fetchall()
    except Exception:
        return out
    for lid, m, o, data in labels:
        d = json.loads(data)
        ps, bs = d.get('pm_seg'), d.get('book_seg')
        if not ps or not bs or not bs.get('book'):
            continue
        book = bs['book']
        pm_rows, bk_rows = W.load(db, m)
        g = W.game_waves(pm_rows, bk_rows, o, clock)
        pad = 15_000
        b_hits = [w for w in g['waves'] if book in w['on'] and bs['t0'] - pad <= w['on'][book] <= bs['t1'] + pad]
        p_on = [w['on'][W.PM] for w in g['waves'] if W.PM in w['on'] and ps['t0'] - pad <= w['on'][W.PM] <= ps['t1'] + pad]
        painted = ((ps['t0'] + ps['t1']) / 2) - ((bs['t0'] + bs['t1']) / 2)
        painted_start = ps['t0'] - bs['t0']
        found_b = b_hits[0]['on'][book] if b_hits else None
        found_p = p_on[0] if p_on else None
        found = None if found_b is None or found_p is None else found_p - found_b
        same = bool(b_hits) and found_p is not None and any(W.PM in w['on'] and w['on'][W.PM] == found_p for w in b_hits)
        text = (f'#{lid} game {m} {o} {book}: painted Polymarket {painted_start / 1000:+.1f} s after the bookmaker (start to start; {painted / 1000:+.1f} middle to middle); '
                f'found bookmaker wave {"yes" if found_b else "NO"}, Polymarket wave {"yes" if found_p else "NO"}'
                + (f', delay {found / 1000:+.1f} s' if found is not None else '') + (', same wave' if same else ''))
        out.append({'id': lid, 'game': m, 'book': book, 'painted_ms': painted, 'painted_start_ms': painted_start, 'found_ms': found, 'book_found': found_b is not None, 'pm_found': found_p is not None, 'same_wave': same, 'text': text})
    return out


if __name__ == '__main__':
    main()
