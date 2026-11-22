"""Export every closed snipe buy with its price lines (Polymarket sell and buy price, all bookmakers, and each
bookmaker feed on its own) for the wave-marking page. Our side's terms, cents. Lines are stored as change points
[second from the window's start, cents]; null = no price. Also the detector's legs per line, for comparison.

usage: wave_marker_export.py <out.json>"""
import sys, os, json, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import waves as W

def steps(arr):
    out, last = [], object()
    for i, v in enumerate(arr):
        v = None if v is None else round(v * 100, 1)
        if v != last: out.append([i, v]); last = v
    return out

def export(db, tr):
    d = tr['d']; dr = d.get('dir', 1.0); oc = d['outcome']; mid = tr['mid']
    t0 = tr['ts'] - W.PRE_S * 1000; t1 = min(tr['closed'], tr['ts'] + 900000) + W.POST_S * 1000
    n = int((t1 - t0) // 1000) + 1
    g = W.grid(db, mid, oc, dr, t0, t1, d.get('trigger'))
    ch = db.execute("SELECT ts_ms, book, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid, oc, t0 - 1800000, t1)).fetchall()
    books = collections.OrderedDict()
    state, j = {}, 0
    for i in range(n):
        t = t0 + i * 1000
        while j < len(ch) and ch[j][0] <= t:
            _, b, p = ch[j]
            state[b] = ((1.0 / p) if dr > 0 else (1 - 1.0 / p)) if p and p > 1.0 else None
            if b not in books: books[b] = [None] * n
            j += 1
        for b, v in state.items(): books[b][i] = v
    series = collections.OrderedDict()
    series['pm'] = dict(name='Polymarket (sell price)', kind='pm', pts=steps(g['bid']))
    series['pm_buy'] = dict(name='Polymarket (buy price)', kind='pmbuy', pts=steps(g['ask']))
    series['all'] = dict(name='All bookmakers', kind='all', pts=steps(g['cons']))
    order = sorted(books, key=lambda b: (b != d.get('trigger'), b))
    for b in order:
        if sum(1 for v in books[b] if v is not None) < 30: continue
        series['b_' + b] = dict(name=b + (' (the bot\'s trigger)' if b == d.get('trigger') else ''), kind='book', pts=steps(books[b]))
    # The detector's waves, version 2 (fitted to the owner's hand marks, 21 Nov 2026): a turn counts once the line
    # comes back by an eighth of the chart's height; a still stretch of 45 s+ before a wave is its own flat piece.
    import wave_truth as WT
    det = {}
    cents = lambda arr: [None if v is None else round(v * 100, 1) for v in arr]
    base = [v for arr in (g['bid'], g['ask'], g['cons']) for v in cents(arr) if v is not None]
    for k, arr in [('pm', g['bid']), ('all', g['cons'])] + [('b_' + b, books[b]) for b in order if 'b_' + b in series]:
        c = cents(arr); vals = base + [v for v in c if v is not None]
        th = max(1.5, 0.125 * (max(vals) - min(vals))) if vals else 3.0
        det[k] = [[w['a'], w['va'], w['b'], w['vb']] for w in WT.waves3(c, (max(vals) - min(vals)) if vals else 30.0, **WT.V3)]     # detector v3: the best straight strokes
    # The two running scores (tools/game_scores.py) from the hour before the buy: every 5 minutes, and 15 s before
    # the bookmaker move that set the trade off. Polymarket against all bookmakers; nothing after that moment is used.
    import game_scores as GS
    t_end = d['move_ms'] - 15000; gh = W.grid(db, mid, oc, dr, t_end - 3600000, t_end, d.get('trigger'))
    hb, hp = GS.cents(gh['cons']), GS.cents(gh['mid'])
    first = next((i for i in range(len(hb)) if hb[i] is not None and hp[i] is not None), None)
    gs = None
    if first is not None:
        def pack(r, i): return None if not r else [round((len(hb) - i) / 60.0), round(r['corr']), None if r['delay'] is None or r['jumps'] < 2 else round(r['delay']), r['jumps'], r['first'], r['behind']]
        rows_ = [pack(GS.scores(hb[first:i], hp[first:i], step=4), i) for i in range(first + 300, len(hb), 300)]
        gs = dict(at=pack(GS.scores(hb[first:], hp[first:], step=4), len(hb)), rows=[r for r in rows_ if r], minutes=round((len(hb) - first) / 60.0))
    pre = d.get('pre_mid'); pre_our = pre if dr > 0 else (1 - pre if pre is not None else None)
    target = (pre_our + d.get('move', 0) * d.get('catch', 1.0) - d.get('spread', 0.0) / 2) if pre_our is not None else None
    pnl = d.get('pnl')
    return dict(id=tr['id'], t0=t0 // 1000, n=n, sport=d.get('sport'), home=d.get('home'), away=d.get('away'), real=bool(tr['real']), bot=tr['bot'],
                side=('Yes ' if dr > 0 else 'No ') + ('1' if oc == 'home' else '2' if oc == 'away' else 'X'), trigger=d.get('trigger'),
                buy=int((tr['ts'] - t0) // 1000), sell=min(n - 1, int((tr['closed'] - t0) // 1000)), move=int((d['move_ms'] - t0) // 1000),
                entry=round(d['entry'] * 100, 1), exit=round(d['exit'] * 100, 1), target=None if target is None else round(target * 100, 1),
                reason=d.get('reason'), pnl=None if pnl is None else round(pnl, 2), series=series, det=det, gs=gs)

if __name__ == '__main__':
    db = W.conn(); out = [export(db, t) for t in W.trades(db)]
    s = json.dumps(dict(made=int(time.time()), trades=out), separators=(',', ':'))
    open(sys.argv[1], 'w').write(s)
    print('trades', len(out), '|', len(s) // 1024, 'KB | lines per trade: median', int(st.median(len(t['series']) for t in out)), 'max', max(len(t['series']) for t in out))
