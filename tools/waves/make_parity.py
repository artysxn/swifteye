"""Build tools/waves/parity_fixture.json: inputs (synthetic and slices of recorded games) with the Python answers,
for tools/waves/test_parity.mjs to check src/web/waves.js against.  usage: python tools/waves/make_parity.py [--db PATH]"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lead as LD  # noqa: E402
import waves as W  # noqa: E402

S = 1_000_000_000_000


def answers(G, outcome):
    pm_rows = [(t, t, o, b, a) for o, rows in G['pm'].items() for t, b, a in rows]
    bk_rows = [(t, b, o, p) for b, outs in G['books'].items() for o, rows in outs.items() for t, p in rows]
    g = W.game_waves(pm_rows, bk_rows, outcome)
    corr = {}
    for b, line in list(g['books'].items()) + ([(W.SUPER, g['superline'])] if g['superline'] else []):
        h = W.hy_contrast(line, g['pm']) if g['pm'] else None
        if h:
            U, vb, vp = h
            i = int(abs(U).argmax())
            corr[b] = {'lag': float(W.THETAS[i]), 'rho': float(U[i] / (vb * vp) ** 0.5) if vb > 0 and vp > 0 else None}
    ld = LD.lead_all(g['pm'], g['superline'])
    eps = lambda es: [{k: e[k] for k in ('dir', 'on', 'alarm', 'peak', 'end')} for e in es]
    lead = None if ld is None else {'t0': ld['t0'], 'n': ld['n'], 'state': [int(v) for v in ld['state']],
                                    'e': [None if v != v else float(v) for v in ld['e']],
                                    'exp': [None if v != v else float(v) for v in ld['exp']],
                                    'sl_trends': eps(ld['sl_trends']), 'pm_trends': eps(ld['pm_trends'])}
    return {
        'lead': lead,
        'superline': [[t, p] for t, p in g['superline']],
        'waves': [{'T': w['T'], 'dir': w['dir'], 'leader': w['leader'], 'pm_on': w['pm_on'], 'on': w['on']} for w in g['waves']],
        'corr': corr,
    }


def synthetic():
    calm = lambda n: [[S + i * 2000, 2.0] for i in range(n)]
    pm = [[S + i * 500, 0.49, 0.51] for i in range(0, 220)] + [[S + i * 500, 0.59, 0.61] for i in range(220, 600)]
    books = {}
    for b, t_move in (('unibet', 100_000), ('fonbet', 103_000), ('leon', 104_000), ('linebet', 140_000)):
        home = [[S + i * 2000, 2.0] for i in range(0, t_move // 2000)] + [[S + t_move, 1 / 0.6]] + [[S + t_move + i * 3000, 1 / 0.6] for i in range(1, 40)]
        away = [[t, 1 / (1 - 1 / p)] for t, p in home]
        books[b] = {'home': home, 'away': away}
    return {'pm': {'home': pm}, 'books': books}


def recorded(db, m, span=20 * 60_000):
    pm_rows, bk_rows = W.load(db, m)
    t_mid = None
    for t, b, o, p in bk_rows:
        if p:
            t_mid = t
    pm, books = {}, {}
    for ts, srv, o, b, a in pm_rows:
        pm.setdefault(o, []).append([ts, b, a])
    for t, b, o, p in bk_rows:
        books.setdefault(b, {}).setdefault(o, []).append([t, p])
    # A 40-minute slice around the busiest stretch (keeps the fixture small).
    ts = sorted(r[0] for rows in pm.values() for r in rows)
    if not ts:
        return None
    c = ts[len(ts) // 2]
    keep = lambda rows: [r for r in rows if c - span <= r[0] <= c + span]
    return {'pm': {o: keep(r) for o, r in pm.items()}, 'books': {b: {o: keep(r) for o, r in outs.items()} for b, outs in books.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    a = ap.parse_args()
    cases = [{'name': 'synthetic: three desks move, Polymarket follows', 'outcome': 'home', 'G': synthetic()}]
    try:
        db = W.connect(a.db or W.default_db())
        for m in (7549, 270):
            G = recorded(db, m)
            if G:
                cases.append({'name': f'recorded game {m}', 'outcome': 'home', 'G': G})
    except SystemExit:
        pass
    for c in cases:
        c['python'] = answers(c['G'], c['outcome'])
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'parity_fixture.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'cases': cases}, f, separators=(',', ':'))
    print(f'{len(cases)} cases -> {out} ({os.path.getsize(out) // 1024} KB)')


if __name__ == '__main__':
    main()
