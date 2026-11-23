"""How the trained network (tools/signals/train.py) reads the owner's Trade review buys.

  python tools/signals/marks_eval.py [--db PATH] [--api http://127.0.0.1:8787]

For every mark with a buy (your own buys, and your version of a bot's trade), on any game the page can open
(recorded here or imported): the network's chance of an up and a down win at that second, where that stands
among all the decision seconds of the same game (percentile), and whether the trade in the mark's direction
wins by the dataset's rule (the take-profit / stop / horizon it was built with, entering 1 s later)."""
import argparse
import json
import os
import sys
import urllib.request

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import train as T  # noqa: E402
import waves as W  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--api', default='http://127.0.0.1:8787')
    a = ap.parse_args()
    od = os.path.join(os.path.dirname(a.db or W.default_db()), 'signals')
    ck = torch.load(os.path.join(od, 'model.pt'), weights_only=False)
    # Score the marks with the same take-profit / stop / horizon the model was trained on.
    lab = np.load(os.path.join(od, 'dataset.npz'))['label']
    F.TP, F.STOP, F.HOR = float(lab[0]), float(lab[1]), int(lab[2])
    model = T.Net(len(ck['names']), ck['hidden'], 'mlp')
    model.load_state_dict(ck['state'])
    model.eval()
    mu, sd = ck['mu'], ck['sd']
    get = lambda u: json.load(urllib.request.urlopen(a.api + u))
    marks = [l for l in get('/review/labels')['labels'] if l.get('buy_ms') and l['kind'] in ('buy', 'adjust')]
    games = {}
    out = []
    for l in sorted(marks, key=lambda l: (l['match_id'], l['buy_ms'])):
        gid, o = l['match_id'], l['outcome']
        if gid not in games:
            games[gid] = get(f'/review/game/{gid}')
        G = games[gid]
        sport = G['match'].get('sport') or ''
        pm, bk = F.g_rows(G, o)
        r = F.build(pm, bk, o, sport, G['match'].get('start_ms'), sport in W.THREE_WAY)
        if r is None:
            out.append(f"#{l['id']:<4} game {gid}: not enough prices to read")
            continue
        ts, X, y, net, evt, hold = r
        i = int(np.searchsorted(ts, l['buy_ms'], side='right') - 1)
        if i < 0 or l['buy_ms'] - ts[i] > 5000:
            out.append(f"#{l['id']:<4} game {gid}: Polymarket not tradable at the buy")
            continue
        Z = np.clip((X - mu) / sd, -10, 10).astype(np.float32)
        with torch.no_grad():
            p = torch.sigmoid(model(torch.from_numpy(Z))).numpy()
        k = 0 if (l.get('side') or 'yes') == 'yes' else 1
        rows = np.flatnonzero(evt)
        pct = float((p[rows, k] < p[i, k]).mean()) if len(rows) else float('nan')
        tau = (ck['tau'].get('mlp') or {}).get('up' if k == 0 else 'down')
        fired = tau is not None and p[i, k] >= tau
        out.append(f"#{l['id']:<4} game {gid:<6} {G['match']['home'][:18]:<18} {W.__name__ and ''}{'Up  ' if k == 0 else 'Down'} at {l['buy_ms']}: "
                   f"network up {p[i, 0]:.2f} / down {p[i, 1]:.2f} (your direction at the {100 * pct:.0f}th percentile of the game; "
                   f"{'over' if fired else 'under'} its trading threshold); by the rule this {'WINS' if y[i, k] else 'does not win'} "
                   f"(trade net {100 * float(net[i, 2 * k + 1]):+.1f}c)")
    text = f'{len(marks)} marks with a buy\n' + '\n'.join(out)
    print(text)
    with open(os.path.join(od, 'marks_eval.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
