"""Money backtest of rule + veto with confidence stakes (python tools/signals/backtest_sized.py --db X --signals DIR).

Trades: improve.py's baseline picks (improve_trades.json 'base': the rule + veto trades of 19-23 Nov, every choice made
on earlier days, each with the stake weight the veto gave it: predicted net / its validation mean, 0.25x..3x, on the
days validation said sizing helps, else 1x). Valued with the owner's exit as booklead.py values it (collapsed tokens
sold at the first real bid within 10 min, else 0; fees 5% p(1-p) both fills).

Account: $10,000 start, base stake 1% of the balance (start + closed P&L, as the paper bot), times the weight, capped
by free cash. Shares = stake / (price + fee). Two fills: FULL (every share at the recorded best price) and BOOK (below). Same trades at
1x every time for comparison.
BOOK fill (checked 23 Nov: the 0.9c No token on Garland v Broadus bought 10,674 shares where ~1,500 could be sold
near the exit price): shares are capped by the depth within 2c on BOTH sides (entry book at the buy, exit book at the
planned sale); shares beyond the best level cost 1c more and sell 1c lower; unknown depth (NULL) is not capped. Writes backtest_sized.txt / .csv / .png in --signals."""
import argparse
import csv
import datetime as dt
import json
import os
import sys

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import L, fee  # noqa: E402

START, PCT = 10_000.0, 0.01
BLUE, ORANGE = '#2a78d6', '#eb6834'
INK, INK2, GRID, SURF = '#0b0b0b', '#52514e', '#e4e3df', '#fcfcfb'


def simulate(T, weights, fill):
    """T: trades sorted by entry. Returns per-trade rows and the balance after each closing event."""
    bal_closed, open_pos, rows, curve = START, [], [], [(T[0]['t'] - 60_000, START)]
    for tr, w in zip(T, weights):
        for p in sorted([p for p in open_pos if p['end'] <= tr['t']], key=lambda p: p['end']):
            bal_closed += p['pnl']
            curve.append((p['end'], bal_closed))
            open_pos.remove(p)
        free = bal_closed - sum(p['stake'] for p in open_pos)
        want = min(PCT * bal_closed * w, max(free, 0))
        unit = tr['buy'] + fee(tr['buy'])
        shares = want / unit
        if fill == 'full':
            stake, pnl = shares * unit, shares * tr['net'] / 100
        else:
            n = min(shares, tr['d_in'], tr['d_out'])
            n1, m1 = min(n, tr['s_in']), min(n, tr['s_out'])
            cost = n1 * tr['buy'] + (n - n1) * (tr['buy'] + 0.01) + n * fee(tr['buy'])
            proceeds = m1 * tr['sell'] + (n - m1) * max(tr['sell'] - 0.01, 0.0) - n * fee(tr['sell'])
            stake, pnl, shares = cost, proceeds - cost, n
        p = {'end': tr['end'], 'stake': stake, 'pnl': pnl}
        open_pos.append(p)
        rows.append({**tr, 'weight': w, 'wanted': want, 'stake': stake, 'shares': shares, 'pnl': pnl})
    for p in sorted(open_pos, key=lambda p: p['end']):
        bal_closed += p['pnl']
        curve.append((p['end'], bal_closed))
    return rows, curve


def stats(name, rows, curve):
    pnl = np.array([r['pnl'] for r in rows])
    stake = np.array([r['stake'] for r in rows])
    bal = np.array([b for _, b in curve])
    peak = np.maximum.accumulate(bal)
    dd = (bal - peak)
    k = int(np.argmin(dd))
    wanted = np.array([r['wanted'] for r in rows])
    part = np.mean(stake < wanted - 0.01) * 100
    return (f'  {name:<34} end ${bal[-1]:,.0f} ({(bal[-1] / START - 1) * 100:+.2f}%)  P&L ${pnl.sum():+,.0f}  staked ${stake.sum():,.0f} '
            f'(return on stakes {pnl.sum() / stake.sum() * 100:+.2f}%)  avg stake ${stake.mean():,.0f}  win {np.mean(pnl > 0) * 100:.0f}%  '
            f'max drawdown ${dd[k]:,.0f} ({dd[k] / peak[k] * 100:.2f}%)  partly unfilled {part:.0f}%')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    I = dict(np.load(os.path.join(a.signals, 'improve.npz'), allow_pickle=False))
    for k, v in I.items():
        if not k.startswith('heads'):
            D[k] = v
    picks = json.load(open(os.path.join(a.signals, 'improve_trades.json'), encoding='utf-8'))['base']
    db = W.connect(a.db)
    names = {m: (h, w_) for m, h, w_ in db.execute('SELECT id, home, away FROM matches')}
    oname = ['home', 'away']
    T = []
    for i, ek, w, day in picks:
        m, o, t, d = int(D['game'][i]), oname[D['outcome'][i]], int(D['t'][i]), int(D['dir'][i])
        sql = ('SELECT bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND outcome = ? AND ts_ms <= ? '
               'ORDER BY ts_ms DESC LIMIT 1')
        bid, ask, bsz, asz, bdp, adp = db.execute(sql, (m, o, t + L * 1000)).fetchone()
        buy = ask if d > 0 else 1 - bid
        num = lambda x: float('inf') if x is None else float(x)
        s_in, d_in = (num(asz), num(adp)) if d > 0 else (num(bsz), num(bdp))      # who sells us the token
        end = t + 1000 * int(D[f'hold_{ek}'][i])
        q = db.execute(sql, (m, o, end)).fetchone()
        s_out, d_out = ((num(q[2]), num(q[4])) if d > 0 else (num(q[3]), num(q[5]))) if q else (float('inf'), float('inf'))   # who buys it back
        x = D[ek][i] / 100 + buy + fee(buy)       # the sale price behind the trade's net (sell - fee(sell) = x)
        sell = x
        for _ in range(6):
            sell = x + fee(sell)
        depth = d_in
        h, aw = names.get(m, ('?', '?'))
        T.append({'s_in': s_in, 'd_in': max(d_in, s_in), 's_out': s_out, 'd_out': max(d_out, s_out), 'sell': float(sell),
                  't': t, 'end': end, 'day': day, 'game': m, 'match': f'{h} v {aw}', 'sport': str(D['sport'][i]),
                  'token': f'{h if o == "home" else aw} {"Yes" if d > 0 else "No"}', 'buy': float(buy), 'depth': float('nan') if depth is None else float(depth),
                  'net': float(D[ek][i]), 'exit': ek, 'gap': float(D['room'][i]), 'w': float(w)})
    order = np.argsort([x['t'] for x in T], kind='stable')
    T = [T[k] for k in order]
    W_ = [x['w'] for x in T]
    runs = {}
    for fill in ('full', 'book'):
        runs[('sized', fill)] = simulate(T, W_, fill)
        runs[('equal', fill)] = simulate(T, [1.0] * len(T), fill)
    out = [f'Rule + veto, {len(T)} trades {dt.datetime.fromtimestamp(T[0]["t"] / 1000, dt.timezone.utc):%d %b %H:%M} -> '
           f'{dt.datetime.fromtimestamp(T[-1]["t"] / 1000, dt.timezone.utc):%d %b %H:%M} UTC; start ${START:,.0f}, base stake {PCT * 100:.0f}% of the balance', '',
           'Every share filled at the recorded best price:',
           stats('confidence stakes (0.25x..3x)', *runs[('sized', 'full')]), stats('equal stakes (1x)', *runs[('equal', 'full')]), '',
           'Book fill: shares capped by the depth within 2c at the buy AND at the sale, 1c worse beyond the best level:',
           stats('confidence stakes (0.25x..3x)', *runs[('sized', 'book')]), stats('equal stakes (1x)', *runs[('equal', 'book')]), '',
           'By day (book fill): confidence stakes / equal stakes']
    rs, re_ = runs[('sized', 'book')][0], runs[('equal', 'book')][0]
    for day in sorted({r['day'] for r in rs}):
        a_ = [r for r in rs if r['day'] == day]
        b_ = [r for r in re_ if r['day'] == day]
        out.append(f'  {day}: {len(a_):>3} trades  ${sum(r["pnl"] for r in a_):+8.2f}  /  ${sum(r["pnl"] for r in b_):+8.2f}   '
                   f'weights {min(r["weight"] for r in a_):.2f}x..{max(r["weight"] for r in a_):.2f}x')
    w = np.array(W_)
    nets = np.array([r['net'] for r in rs])
    out.append('')
    for lo, hi, lab in ((0, 0.75, 'small (< 0.75x)'), (0.75, 1.25, 'about 1x'), (1.25, 9, 'big (> 1.25x)')):
        m = (w >= lo) & (w < hi)
        if m.any():
            out.append(f'  stakes {lab:<16} {m.sum():>3} trades, {nets[m].mean():+.2f}c a share, win {np.mean(nets[m] > 0) * 100:.0f}%')
    out.append('')
    out.append('By sport (book fill, confidence stakes): ' + ', '.join(
        f'{s} {sum(1 for r in rs if r["sport"] == s)} ${sum(r["pnl"] for r in rs if r["sport"] == s):+.0f}' for s in sorted({r['sport'] for r in rs})))
    best = sorted(rs, key=lambda r: r['pnl'])
    out.append('Worst: ' + '; '.join(f'{r["match"]} {r["token"]} ${r["pnl"]:+.0f}' for r in best[:3]))
    out.append('Best:  ' + '; '.join(f'{r["match"]} {r["token"]} ${r["pnl"]:+.0f}' for r in best[::-1][:3]))
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'backtest_sized.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    rb = {id(x): None for x in rs}
    bal = START
    with open(os.path.join(a.signals, 'backtest_sized.csv'), 'w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(['bought_utc', 'sold_utc', 'match', 'sport', 'token', 'gap_pt', 'buy_price', 'stake_weight', 'stake_usd', 'shares',
                     'net_cents_per_share', 'pnl_usd', 'pnl_equal_stake_usd'])
        for r, e in zip(rs, re_):
            wr.writerow([dt.datetime.fromtimestamp(r['t'] / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                         dt.datetime.fromtimestamp(r['end'] / 1000, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), r['match'], r['sport'], r['token'],
                         f'{r["gap"]:.1f}', f'{r["buy"]:.3f}', f'{r["weight"]:.2f}', f'{r["stake"]:.2f}', f'{r["shares"]:.1f}', f'{r["net"]:.2f}',
                         f'{r["pnl"]:.2f}', f'{e["pnl"]:.2f}'])
    del rb, bal
    chart(runs, T, os.path.join(a.signals, 'backtest_sized.png'))


def chart(runs, T, path):
    plt.rcParams.update({'font.family': 'Segoe UI', 'font.size': 10, 'axes.edgecolor': GRID, 'axes.labelcolor': INK2,
                         'xtick.color': INK2, 'ytick.color': INK2, 'axes.titlecolor': INK, 'figure.facecolor': SURF, 'axes.facecolor': SURF})
    fig, ax = plt.subplots(3, 1, figsize=(11, 9.5), gridspec_kw={'height_ratios': [3, 1.4, 1.6]})
    fig.subplots_adjust(left=0.08, right=0.86, top=0.9, bottom=0.07, hspace=0.55)
    t0 = T[0]['t']
    hrs = lambda ms: (np.asarray(ms) - t0) / 3_600_000
    series = (('sized', 'Confidence stakes', BLUE), ('equal', 'Equal stakes', ORANGE))
    for key, lab, col in series:
        _, cv = runs[(key, 'book')]
        x, y = hrs([c[0] for c in cv]), np.array([c[1] for c in cv])
        ax[0].step(x, y, where='post', color=col, lw=2, label=lab)
        ax[0].annotate(f'{lab}\n${y[-1]:,.0f}', (x[-1], y[-1]), xytext=(8, 16 if key == 'sized' else -16), textcoords='offset points', va='center', color=INK, fontsize=9.5)
        peak = np.maximum.accumulate(y)
        ax[1].step(x, y - peak, where='post', color=col, lw=2, label=lab)
    _, cvb = runs[('sized', 'full')]
    ax[0].step(hrs([c[0] for c in cvb]), [c[1] for c in cvb], where='post', color=BLUE, lw=1.2, ls=(0, (3, 2)), label='Confidence stakes if every share filled at the best price')
    ax[0].axhline(START, color=INK2, lw=0.8, ls=':', zorder=0)
    ax[0].set_ylabel('Balance ($)')
    ax[0].set_title('Rule + veto with confidence stakes: paper backtest, 19-23 Nov (book-depth fills; every choice made on earlier days)',
                    loc='left', fontsize=11.5, pad=26)
    ax[0].legend(loc='upper left', frameon=False, ncol=3, bbox_to_anchor=(0, 1.13), fontsize=9)
    ax[1].set_ylabel('Drawdown ($)')
    ax[1].set_title('Below the previous peak', loc='left', fontsize=10)
    # day boundaries
    days = sorted({r['day'] for r in T})
    for dd in days:
        ms = int(dt.datetime.fromisoformat(dd).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
        for k in (0, 1):
            ax[k].axvline(hrs(ms), color=GRID, lw=0.8, zorder=0)
        ax[0].text(hrs(ms) + 0.3, ax[0].get_ylim()[1], dt.datetime.fromisoformat(dd).strftime('%d %b'), va='top', fontsize=8.5, color=INK2)
    for k in (0, 1):
        ax[k].set_xlabel('Hours since the first trade')
        ax[k].grid(axis='y', color=GRID, lw=0.6)
        for s in ('top', 'right'):
            ax[k].spines[s].set_visible(False)
    # daily P&L, grouped bars
    xs = np.arange(len(days))
    for j, (key, lab, col) in enumerate(series):
        rows, _ = runs[(key, 'book')]
        v = [sum(r['pnl'] for r in rows if r['day'] == dd) for dd in days]
        ax[2].bar(xs + (j - 0.5) * 0.36, v, width=0.34, color=col, label=lab, edgecolor=SURF, linewidth=2)
        for xx, vv in zip(xs + (j - 0.5) * 0.36, v):
            ax[2].text(xx, vv + (2 if vv >= 0 else -2), f'{vv:+.0f}', ha='center', va='bottom' if vv >= 0 else 'top', fontsize=8, color=INK2)
    n_day = [sum(1 for r in T if r['day'] == dd) for dd in days]
    ax[2].set_xticks(xs, [f'{dt.datetime.fromisoformat(dd):%d %b}\n{n} trades' for dd, n in zip(days, n_day)])
    ax[2].axhline(0, color=INK2, lw=0.8)
    ax[2].set_ylabel('P&L ($)')
    ax[2].set_title('P&L by day', loc='left', fontsize=10)
    ax[2].legend(loc='upper left', frameon=False, ncol=2, fontsize=9)
    ax[2].grid(axis='y', color=GRID, lw=0.6)
    lo, hi = ax[2].get_ylim()
    ax[2].set_ylim(lo - 8, hi + 8)
    for s in ('top', 'right'):
        ax[2].spines[s].set_visible(False)
    fig.savefig(path, dpi=130)


if __name__ == '__main__':
    main()
