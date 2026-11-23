"""Charts of real bot trades with the model's view at the moment of buying (python tools/signals/example_charts.py).

  python tools/signals/example_charts.py --db X --signals DIR [--out DIR/charts]

Each chart: the token's Polymarket sell and buy price, the superline and the bookmaker that triggered the trade
(cents of the token bought); the bot's buy and its actual sale; what the day's model (trained only on games
before that day, walkforward_bots.py) said at the buy: its chance that a wave starts within 30 s each way, the
10-50-90% range of the move it expected a trade could capture and when it expected the best price, whether it kept
or vetoed the trade; the cash-out model's instant sale if it would have sold early (exit_vs_hold.py); and what
really happened (the best sell price within 3 minutes, the bot's result). Under each chart, the lead strip and the
up / down waves (CUSUM) of the superline and Polymarket, as on the Trade review page.

Six examples, chosen by rule so they are not cherry-picked: the snipe trade the model kept that made the most and
the one it kept that lost the most, the vetoed one that lost the most and the vetoed one that made the most, and the
early sale that helped most and the one that hurt most."""
import argparse
import datetime as dt
import glob
import json
import os
import sys

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
import waves as W  # noqa: E402

C = {'sell': '#2f6fd6', 'buy': '#9bb8ef', 'super': '#13a9b8', 'trig': '#e8822a', 'up': '#2ca24f', 'down': '#d9443a', 'pmlead': '#4a8cff', 'stale': '#9a9aa5',
     'band': '#2ca24f', 'best': '#e0a400', 'exit': '#8a3fd1'}


def fee(p):
    return 0.05 * p * (1 - p)


def pick(trades):
    out, seen = [], set()

    def take(label, rows, key, reverse):
        rows = [r for r in rows if r['id'] not in seen]
        if rows:
            r = sorted(rows, key=key, reverse=reverse)[0]
            seen.add(r['id'])
            out.append((label, r))

    sn = [t for t in trades if t['bot'] != 'favs' and t.get('edge') is not None]
    take('Model KEPT it, and it won', [t for t in sn if t['edge'] > 0 and t['per_share'] > 0], lambda t: t['per_share'], True)
    take('Model KEPT it, and it lost', [t for t in sn if t['edge'] > 0 and t['per_share'] < 0], lambda t: t['per_share'], False)
    take('Model VETOED it, and it lost (veto right)', [t for t in sn if t['edge'] <= 0 and t['per_share'] < 0], lambda t: t['per_share'], False)
    take('Model VETOED it, but it won (veto wrong)', [t for t in sn if t['edge'] <= 0 and t['per_share'] > 0], lambda t: t['per_share'], True)
    ex = [t for t in sn if t.get('sold_at_s') is not None]
    take('Instant sale HELPED', ex, lambda t: t['model_net'] - t['bot_net'], True)
    take('Instant sale HURT', ex, lambda t: t['model_net'] - t['bot_net'], False)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--out')
    a = ap.parse_args()
    out_dir = a.out or os.path.join(a.signals, 'charts')
    os.makedirs(out_dir, exist_ok=True)
    db = W.connect(a.db)
    scored = {}
    for f in glob.glob(os.path.join(a.signals, 'wf', '*', 'scored.json')):
        for r in json.load(open(f, encoding='utf-8')):
            scored[(r['bot'], r['buy'])] = (r, os.path.dirname(f))
    exits = {r['id']: r for r in json.load(open(os.path.join(a.signals, 'exit_vs_hold.json'), encoding='utf-8'))}
    trades = []
    for tid, bot, real, mid, data in db.execute('SELECT id, bot, real, match_id, data FROM bot_trades').fetchall():
        d = json.loads(data)
        s = scored.get((bot, d.get('ts')))
        if not s:
            continue
        r, day_dir = s
        e = exits.get(tid, {})
        trades.append({'id': tid, 'bot': bot, 'real': bool(real), 'match': mid, 'd': d, 'edge': r['edge'], 'per_share': r['per_share'], 'day_dir': day_dir,
                       'sold_at_s': e.get('sold_at_s'), 'bot_net': e.get('bot_net'), 'model_net': e.get('model_net')})
    picks = pick(trades)
    files = []
    for n, (label, t) in enumerate(picks, 1):
        files.append(chart(db, t, label, n, out_dir))
        print('wrote', files[-1], flush=True)
    print(json.dumps(files))


def held_rows(rows, times, stop_after=None):
    """Sample-and-hold of sorted [(t, v)] at times; nan before the first row, where v is None, and (stop_after)
    more than that many ms after the last row (a line that stopped is not drawn on)."""
    if not rows:
        return np.full(len(times), np.nan)
    lt = np.array([x[0] for x in rows], dtype=np.int64)
    lv = np.array([np.nan if x[1] is None else x[1] for x in rows], dtype=float)
    k = np.searchsorted(lt, times, side='right') - 1
    out = np.where(k >= 0, lv[np.clip(k, 0, None)], np.nan)
    if stop_after is not None:
        out[times > lt[-1] + stop_after] = np.nan
    return out


def chart(db, t, label, n, out_dir):
    d = t['d']
    sport, home, away, start = db.execute('SELECT sport, home, away, start_ms FROM matches WHERE id = ?', (t['match'],)).fetchone()
    o, buy_ms = d['outcome'], d['ts']
    up = (d.get('dir') or 1) > 0
    pm_rows, bk_rows = W.load(db, t['match'])
    try:
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (t['match'],)).fetchall()
    except Exception:
        book = None
    g = F.build(pm_rows, bk_rows, o, sport, start, sport in W.THREE_WAY, book, full=True)
    ts, n_ = g['ts'], len(g['ts'])
    i = int(np.searchsorted(ts, buy_ms, side='right') - 1)
    books = W.book_lines(bk_rows, o)
    sl_line = [(tt, p) for tt, p, _ in W.superline(books)] if books else []
    tok = (lambda p: p * 100) if up else (lambda p: (1 - p) * 100)
    # The day's model at the buy second.
    ck = torch.load(os.path.join(t['day_dir'], 'wave_model.pt'), weights_only=False)
    net = TW.Body(len(ck['names']), ck['hidden'], ck['A']['net.6.weight'].shape[0])
    net.load_state_dict(ck['A'])
    net.eval()
    z = np.clip((g['X'][i:i + 1] - ck['mu']) / ck['sd'], -10, 10).astype(np.float32)
    with torch.no_grad():
        out = net(torch.from_numpy(z)).numpy()[0]
    h_up, h_dn = 1 / (1 + np.exp(-out[0])), 1 / (1 + np.exp(-out[1]))
    q = np.sort(out[2:5] if up else out[5:8]) * 3
    t_best = (out[8] if up else out[9]) * 60
    pm_wave_now = g['X'][i, F.NAMES.index('pm_wave')]
    rem_t, rem_a = out[10] * 60, np.sort(out[11:14]) * 3
    # Polymarket on its own timeline, to the bot's real exit (the feature grid stops where the bookmakers stop).
    rb = sorted(((x[0], x[3]) for x in pm_rows if x[2] == o), key=lambda r: r[0])
    ra = sorted(((x[0], x[4]) for x in pm_rows if x[2] == o), key=lambda r: r[0])
    exit_ms = d.get('exit_ts') or rb[-1][0]
    t_lo = buy_ms - 180_000
    t_hi = min(max(buy_ms + 240_000, exit_ms + 60_000), buy_ms + 600_000, rb[-1][0] + 60_000)   # 10 min after the buy at most
    tt = np.arange(t_lo, t_hi + 1, 1000, dtype=np.int64)
    x = (tt - buy_ms) / 60000
    bid, ask = held_rows(rb, tt), held_rows(ra, tt)
    sell = (bid if up else 1 - ask) * 100
    buyp = (ask if up else 1 - bid) * 100
    sl = held_rows(sl_line, tt, 60_000)
    entry = d.get('entry')
    j0 = int((buy_ms - t_lo) // 1000)
    entry_c = entry * 100 if entry is not None else buyp[j0]
    fees = 2 * fee(entry_c / 100) * 100

    fig = plt.figure(figsize=(17.5, 7.4), dpi=100)
    gsp = fig.add_gridspec(2, 1, height_ratios=[4.2, 1], hspace=0.08, left=0.055, right=0.655)
    ax = fig.add_subplot(gsp[0])
    sx = fig.add_subplot(gsp[1], sharex=ax)
    ax.plot(x, buyp, color=C['buy'], lw=1, label='Polymarket buy price', drawstyle='steps-post')
    ax.plot(x, sell, color=C['sell'], lw=2.2, label='Polymarket sell price', drawstyle='steps-post')
    ax.plot(x, tok(sl), color=C['super'], lw=1.8, label='Superline', drawstyle='steps-post')
    trig = d.get('trigger')
    marks = [entry_c, entry_c + q[0], entry_c + q[2]]
    if trig and trig in books:
        ax.plot(x, tok(held_rows(books[trig], tt, 60_000)), color=C['trig'], lw=1.2, alpha=0.9, label=f'{trig} (triggered the bot)', drawstyle='steps-post')
    # The model at the buy: 10-90% of the price it could sell at (entry + capturable move) up to when it expected the
    # best price, and the median.
    tb_min = max(t_best, 5) / 60
    ax.fill_between([0, tb_min], entry_c + q[0], entry_c + q[2], color=C['band'], alpha=0.16, label='Model at the buy: 10-90% range of the best sell price')
    ax.plot([0, tb_min], [entry_c + q[1]] * 2, color=C['band'], lw=2.2, ls='--', label=f'Model median: {q[1]:+.1f}c, best price after {t_best:.0f} s')
    ax.axhline(entry_c + fees, color='#888', lw=1, ls=':', label=f'Break-even after fees ({entry_c + fees:.1f}c)')
    ax.axvline(0, color='#555', lw=0.8, alpha=0.6)
    w = sell[j0 + 1:j0 + 181]
    if np.isfinite(w).any():
        k = int(np.nanargmax(w))
        ax.scatter([x[j0 + 1 + k]], [w[k]], s=90, color=C['best'], zorder=6, edgecolor='k', label=f'Actual best sell within 3 min: {w[k] - entry_c:+.1f}c vs price paid')
        marks.append(w[k])
    ax.scatter([0], [entry_c], marker='^', s=170, color=C['up'], edgecolor='k', zorder=7, label=f'Bot bought at {entry_c:.1f}c')
    if d.get('exit') is not None and d.get('exit_ts'):
        xe = (d['exit_ts'] - buy_ms) / 60000
        lbl = f"Bot sold at {d['exit'] * 100:.1f}c after {xe:.1f} min ({d.get('reason') or 'exit'}): {t['per_share']:+.1f}c a share"
        if d['exit_ts'] <= t_hi:
            ax.scatter([xe], [d['exit'] * 100], marker='D', s=110, color=C['down'], edgecolor='k', zorder=7, label=lbl)
            marks.append(d['exit'] * 100)
        else:
            ax.plot([], [], ' ', label=lbl + ' (off the chart)')
    if t.get('sold_at_s'):
        k = j0 + int(t['sold_at_s'])
        if k < len(tt):
            ax.scatter([x[k]], [sell[k]], marker='X', s=170, color=C['exit'], edgecolor='k', zorder=8,
                       label=f"Cash-out model would sell after {t['sold_at_s']} s: {t['model_net']:+.1f}c (bot {t['bot_net']:+.1f}c)")
            marks.append(sell[k])
    vals = np.concatenate([sell, buyp, tok(sl), np.array(marks, dtype=float)])
    vals = vals[np.isfinite(vals)]
    if len(vals):
        lo_, hi_ = np.percentile(vals, 0.5), np.percentile(vals, 99.5)
        lo_, hi_ = min(lo_, min(marks)), max(hi_, max(marks))
        ax.set_ylim(max(0, lo_ - 3), min(100, hi_ + 3))
    ax.set_ylabel(f"cents of the token bought ({'Yes' if up else 'No'} {o})")
    ax.grid(alpha=0.25)
    ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1.0), fontsize=8, framealpha=0.95, borderaxespad=0)
    verdict = 'KEPT' if t['edge'] > 0 else 'VETOED'
    ax.set_title(f"{n}. {label}\n{home} v {away} ({sport}) · {t['bot']}{' REAL' if t['real'] else ''} · {'Up' if up else 'Down'} {o} · "
                 f"model {verdict} it (predicted move minus fees {t['edge']:+.1f}c) · bot result {t['per_share']:+.1f}c a share", fontsize=10.5, loc='left')
    info = ['What the model said at the buy:',
            f"  wave starting within 30 s: up {100 * h_up:.0f}%, down {100 * h_dn:.0f}%",
            f"  best sell price, 10 / 50 / 90%: {q[0]:+.1f} / {q[1]:+.1f} / {q[2]:+.1f}c vs price paid",
            f"  expected after about {t_best:.0f} s"]
    if pm_wave_now:
        info.append(f"  a Polymarket {'up' if pm_wave_now > 0 else 'down'}-wave was already running:")
        info.append(f"  about {rem_t:.0f} s and {rem_a[1]:+.1f} pt left to its peak")
    fig.text(0.665, 0.27, '\n'.join(info), fontsize=8.6, va='top', ha='left', bbox=dict(boxstyle='round', fc='#f7f7f7', ec='#bbb'))
    # Strip: lead state and both lines' up / down waves (from the feature grid, so they stop with the bookmakers).
    gp, gs = g['mid'], F._held(sl_line, ts[0], n_)
    gx = (ts - buy_ms) / 60000
    inwin = (ts >= t_lo) & (ts <= t_hi)
    ld = L.lead_state(gp, gs)
    flip = 1 if up else -1
    rows = {'Lead': 2, 'Superline': 1, 'Polymarket': 0}
    lead_col = {L.AHEAD_UP: C['up'] if up else C['down'], L.AHEAD_DOWN: C['down'] if up else C['up'], L.PM_AHEAD: C['pmlead'], L.STALE: C['stale']}
    st = ld['state']
    idx = np.flatnonzero(inwin)
    if len(idx):
        k, kend = idx[0], idx[-1]
        while k <= kend:
            v = st[k]
            k2 = k
            while k2 + 1 <= kend and st[k2 + 1] == v:
                k2 += 1
            if v in lead_col:
                sx.barh(2, (k2 + 1 - k) / 60, left=gx[k], height=0.7, color=lead_col[v], alpha=0.85)
            k = k2 + 1
    for name, series in (('Superline', gs), ('Polymarket', gp)):
        eps, _, _ = L.trends(series)
        for e in eps:
            e0, e1 = ts[e['on']], ts[min(e['end'], n_ - 1)]
            if e1 < t_lo or e0 > t_hi:
                continue
            cc = C['up'] if e['dir'] * flip > 0 else C['down']
            x0, xp, xe, xd = [gx[min(max(v, 0), n_ - 1)] for v in (e['on'], e['peak'], e['end'], e['alarm'])]
            sx.barh(rows[name], xp - x0, left=x0, height=0.7, color=cc, alpha=0.9)
            sx.barh(rows[name], xe - xp, left=xp, height=0.7, color=cc, alpha=0.25)
            sx.plot([xd, xd], [rows[name] - 0.35, rows[name] + 0.35], color='k', lw=1.4)
    sx.set_yticks([0, 1, 2])
    sx.set_yticklabels(['Polymarket\nwaves', 'Superline\nwaves', 'Lead'], fontsize=8)
    sx.set_ylim(-0.6, 2.6)
    sx.axvline(0, color='#555', lw=0.8, alpha=0.6)
    sx.set_xlabel('minutes from the buy', fontsize=9)
    sx.grid(alpha=0.2, axis='x')
    sx.set_xlim(x[0], x[-1])
    plt.setp(ax.get_xticklabels(), visible=False)
    fig.text(0.665, 0.10, 'Strip: green = up for this token, red = down;\nsolid to the peak, faint until it fades;\nblack tick = when the wave was detected.\nLead: green / red = superline ahead,\nblue = Polymarket ahead, grey = stale gap.',
             fontsize=7.8, va='top', ha='left', color='#444')
    path = os.path.join(out_dir, f"example_{n}_{t['id']}.png")
    fig.savefig(path)
    plt.close(fig)
    return path


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
