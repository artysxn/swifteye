"""One game in full: every chance the model saw, what the old bot did and skipped, and how each would have ended.

  python tools/signals/game_review.py --db X --signals DIR --trade ID [--outcome away]

The day's model (walkforward_bots.py: trained only on games that kicked off before the game's day) is read at every
decision second. Its entries (predicted median move minus fees >= the threshold its validation picked, one position
at a time each way) are run with the plain exit (sell at +3c net, else after 180 s: never held to settlement), as
are the old bot's own trades and every signal it skipped (bot_signals, with its reason). Chart + text in DIR/charts."""
import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import lead as L  # noqa: E402
import train_waves as TW  # noqa: E402
import wave_targets as WT  # noqa: E402
import waves as W  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--trade', type=int, required=True)
    ap.add_argument('--outcome')
    a = ap.parse_args()
    db = W.connect(a.db)
    tid, mid, data = db.execute('SELECT id, match_id, data FROM bot_trades WHERE id = ?', (a.trade,)).fetchone()
    tr = json.loads(data)
    sport, home, away, start = db.execute('SELECT sport, home, away, start_ms FROM matches WHERE id = ?', (mid,)).fetchone()
    o = a.outcome or tr['outcome']
    day = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).strftime('%Y%m%d')
    ck = torch.load(os.path.join(a.signals, 'wf', day, 'wave_model.pt'), weights_only=False)
    net = TW.Body(len(ck['names']), ck['hidden'], ck['A']['net.6.weight'].shape[0])
    net.load_state_dict(ck['A'])
    net.eval()
    pm_rows, bk_rows = W.load(db, mid)
    book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (mid,)).fetchall()
    g = F.build(pm_rows, bk_rows, o, sport, start, sport in W.THREE_WAY, book, full=True)
    T = WT.wave_targets(g)
    ts, n = g['ts'], len(g['ts'])
    Z = np.clip((g['X'] - ck['mu']) / ck['sd'], -10, 10).astype(np.float32)
    with torch.no_grad():
        P = net(torch.from_numpy(Z)).numpy()
    edge_up = np.sort(P[:, 2:5], axis=1)[:, 1] * 3 - T['fee_rt']
    edge_dn = np.sort(P[:, 5:8], axis=1)[:, 1] * 3 - T['fee_rt']
    th_up, th_dn = ck['theta'].get('up'), ck['theta'].get('down')
    dec = g['evt'] & g['ok'] & np.isfinite(T['base_up'])
    books = W.book_lines(bk_rows, o)
    sl = F._held([(t, p) for t, p, _ in W.superline(books)], ts[0], n) if books else np.full(n, np.nan)
    gp = g['mid']

    def run_entries(score, th, key):
        taken, busy = [], -1
        if th is None:
            return taken
        for i in np.flatnonzero(dec & (score >= th)):
            if i < busy:
                continue
            busy = i + int(T[f'hold_{key}'][i])
            taken.append((i, float(T[f'base_{key}'][i])))
        return taken
    m_up, m_dn = run_entries(edge_up, th_up, 'up'), run_entries(edge_dn, th_dn, 'dn')
    loose_up, loose_dn = run_entries(edge_up, 0.0, 'up'), run_entries(edge_dn, 0.0, 'dn')
    # The bot's trades and skipped signals, valued with the same plain exit from the second they happened.
    idx_at = lambda t: int(np.clip(np.searchsorted(ts, t, side='right') - 1, 0, n - 1))
    bt = db.execute('SELECT id, bot, real, data FROM bot_trades WHERE match_id = ?', (mid,)).fetchall()
    sig = db.execute('SELECT ts_ms, outcome, result, data FROM bot_signals WHERE match_id = ? ORDER BY ts_ms', (mid,)).fetchall()
    # Lead statistics for the game.
    ld = L.lead_state(gp, sl)
    se, _, _ = L.trends(sl)
    pe, _, _ = L.trends(gp)
    pairs = L.pair_trends(se, pe)
    followed = [p for p in pairs if p['pm'] is not None]
    leads = [p['lead'] for p in followed]
    lines = [f'{home} v {away} ({sport}), outcome {o}; model of {day} (trained only on earlier games); {int(dec.sum())} decision seconds over {(ts[-1] - ts[0]) / 60000:.0f} min.',
             f"Lead in this game: superline ahead {100 * np.mean((ld['state'] == 1) | (ld['state'] == 2)):.0f}% of the time, Polymarket ahead {100 * np.mean(ld['state'] == 3):.0f}%; "
             f'superline waves {len(se)}, Polymarket followed {len(followed)} of them, median onset lead {np.median(leads) if leads else float("nan"):+.0f} s (+ = superline first).',
             f"Old bot: {len(bt)} trades; {len(sig)} signals: " + ', '.join(f'{k} {v}' for k, v in Counter(s[2] for s in sig).most_common()),
             f"Model at its validation thresholds (up >= {th_up}, down >= {th_dn}): up {len(m_up)} trades {sum(v for _, v in m_up):+.1f}c, down {len(m_dn)} trades {sum(v for _, v in m_dn):+.1f}c (plain exit, fees paid).",
             f"Model taking every positive edge: up {len(loose_up)} trades {sum(v for _, v in loose_up):+.1f}c ({100 * np.mean([v > 0 for _, v in loose_up]) if loose_up else 0:.0f}% win), "
             f"down {len(loose_dn)} trades {sum(v for _, v in loose_dn):+.1f}c ({100 * np.mean([v > 0 for _, v in loose_dn]) if loose_dn else 0:.0f}% win)."]
    sk = []
    for s_ts, so, res, sdata in sig:
        if res == 'entered':
            continue
        i = idx_at(s_ts)
        sd = json.loads(sdata or '{}')
        # The direction of the skipped move: the triggering bookmaker's 5 s move at that second, else unknown.
        key = None
        if so == o:
            up5, dn5 = g['X'][i, F.NAMES.index('fam_up5')], g['X'][i, F.NAMES.index('fam_dn5')]
            key = 'up' if up5 > dn5 else ('dn' if dn5 > up5 else None)
        val = float(T[f'base_{key}'][i]) if key and np.isfinite(T[f'base_{key}'][i]) else None
        sk.append((s_ts, res, key, val, edge_up[i] if key == 'up' else edge_dn[i] if key == 'dn' else None))
    vals = [v for *_, v, _ in sk if v is not None]
    lines.append(f"Skipped signals on this outcome with a direction ({len(vals)}): had they been bought with the plain exit, {sum(vals):+.1f}c in total, "
                 f"{100 * np.mean([v > 0 for v in vals]) if vals else 0:.0f}% wins; the model saw a positive edge at {sum(1 for *_, v, e in sk if v is not None and e is not None and e > 0)} of them.")
    for i, v in m_up + m_dn:
        pass
    text = '\n'.join(lines)
    print(text)
    # ---- chart: Yes price of the outcome (cents), superline, bot trades, skipped signals, model entries.
    # The live part: from 5 minutes before Polymarket first moved 5c from where it started.
    mv = np.flatnonzero(np.abs(np.nan_to_num(gp, nan=gp[np.isfinite(gp)][0]) - gp[np.isfinite(gp)][0]) > 0.05)
    w0 = max(0, (mv[0] if len(mv) else 0) - 300)
    t0 = ts[w0]
    x = (ts - t0) / 60000
    fig = plt.figure(figsize=(18, 8), dpi=100)
    gsp = fig.add_gridspec(2, 1, height_ratios=[4, 1], hspace=0.06, left=0.05, right=0.80)
    ax = fig.add_subplot(gsp[0])
    sx = fig.add_subplot(gsp[1], sharex=ax)
    ax.plot(x, g['bid'] * 100, color='#2f6fd6', lw=1.6, drawstyle='steps-post', label=f'Polymarket sell price (Yes {o})')
    ax.plot(x, g['ask'] * 100, color='#9bb8ef', lw=0.8, drawstyle='steps-post', label='Polymarket buy price')
    ax.plot(x, sl * 100, color='#13a9b8', lw=1.6, drawstyle='steps-post', label='Superline')
    for i, v in m_up:
        ax.scatter([x[i]], [g['ask'][i + 1] * 100], marker='^', s=110, color='#2ca24f' if v > 0 else 'white', edgecolor='#2ca24f', lw=1.8, zorder=6)
        ax.annotate(f'{v:+.0f}', (x[i], g['ask'][i + 1] * 100), textcoords='offset points', xytext=(0, 9), ha='center', fontsize=7, color='#2ca24f')
    for i, v in m_dn:
        ax.scatter([x[i]], [g['bid'][i + 1] * 100], marker='v', s=110, color='#d9443a' if v > 0 else 'white', edgecolor='#d9443a', lw=1.8, zorder=6)
        ax.annotate(f'{v:+.0f}', (x[i], g['bid'][i + 1] * 100), textcoords='offset points', xytext=(0, -14), ha='center', fontsize=7, color='#d9443a')
    ax.scatter([], [], marker='^', s=90, color='#2ca24f', edgecolor='#2ca24f', label='Model buys up (filled = won, number = cents)')
    ax.scatter([], [], marker='v', s=90, color='#d9443a', edgecolor='#d9443a', label='Model buys down (hollow = lost)')
    for k_, (b_id, b_bot, b_real, b_data) in enumerate(bt):
        bd = json.loads(b_data)
        i = idx_at(bd['ts'])
        ax.scatter([x[i]], [g['mid'][i] * 100], marker='*', s=320, color='#e0a400', edgecolor='k', zorder=7, label='Old bot traded' if k_ == 0 else None)
    for s_ts, res, key, val, e in sk:
        i = idx_at(s_ts)
        sx.plot([x[i], x[i]], [0.6, 1.4], color='#888' if res == 'book_reverted' else '#c07', lw=1.2)
    for b_id, b_bot, b_real, b_data in bt:
        i = idx_at(json.loads(b_data)['ts'])
        sx.plot([x[i], x[i]], [0.6, 1.4], color='#e0a400', lw=3)
    # model edges (predicted move minus fees), up and down
    sx2 = sx.twinx()
    sx2.plot(x, np.where(dec, edge_up, np.nan), color='#2ca24f', lw=0.6, alpha=0.7)
    sx2.plot(x, np.where(dec, edge_dn, np.nan), color='#d9443a', lw=0.6, alpha=0.7)
    if th_up is not None:
        sx2.axhline(th_up, color='#2ca24f', ls=':', lw=1)
    if th_dn is not None:
        sx2.axhline(th_dn, color='#d9443a', ls=':', lw=1)
    sx2.set_ylabel('model edge, c', fontsize=8)
    sx.set_yticks([1])
    sx.set_yticklabels(['bot signals\n(grey = skipped,\ngold = traded)'], fontsize=7.5)
    sx.set_ylim(0, 2)
    sx.set_xlabel('minutes since the game went live (5 min before Polymarket first moved 5c)', fontsize=9)
    sx.set_xlim(0, x[-1])
    ax.set_ylabel(f'cents (Yes {o})')
    ax.grid(alpha=0.25)
    ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1.0), fontsize=8)
    ax.set_title(f'{home} v {away} ({sport}): every chance in the game', loc='left', fontsize=11)
    plt.setp(ax.get_xticklabels(), visible=False)
    fig.text(0.81, 0.32, '\n'.join(lines[1:]).replace('; ', ';\n').replace(': ', ':\n', 1), fontsize=7.6, va='top', ha='left', wrap=True)
    out = os.path.join(a.signals, 'charts', f'game_{mid}_{o}.png')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    with open(out.replace('.png', '.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    print('wrote', out)


if __name__ == '__main__':
    main()
