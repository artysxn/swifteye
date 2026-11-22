"""Check the Telegram chart of one snipe trade (owner, 21 Nov 2026: 'Monitor every buy / sell to make sure its chart
looks correct'). Builds the same Chart.js config the bot sends to Telegram (through the bot's own test program, so
nothing is re-implemented and nothing leaves this Mac), once as sent with the BUY and once as sent with the SALE,
checks the numbers on it against the recorded trade, and writes a short snippet per chart that draws it for a look by eye
(run the snippet in the app's browser pane on http://127.0.0.1:8787).
usage: chart_check.py <trade id> [<trade id> ...]     ('last' = the last closed snipe trade)"""
import sys, os, json, glob, sqlite3, subprocess, datetime as dt, html
DB = os.path.expanduser('~/swifteye/oddsfeed.db')
OUT = os.path.expanduser('~/swifteye/research/charts'); os.makedirs(OUT, exist_ok=True)
NO = dt.timezone(dt.timedelta(hours=1))
hm = lambda ms: dt.datetime.fromtimestamp(ms / 1000, NO).strftime('%H:%M')
hms = lambda ms: dt.datetime.fromtimestamp(ms / 1000, NO).strftime('%H:%M:%S')
BIN = max(glob.glob(os.path.expanduser('~/swifteye-src/target/release/build/oddsfeed/*/out/oddsfeed-*')), key=os.path.getmtime)


def dump(tid, open_, now):
    out = os.path.join(OUT, f"trade_{tid}_{'buy' if open_ else 'sale'}.json")
    env = dict(os.environ, SWIFTEYE_DB=DB, TRADE_ID=str(tid), OUT=out, NOW=str(now))
    if open_: env['OPEN'] = '1'
    r = subprocess.run([BIN, '--ignored', '--exact', 'telegram::bot_text_tests::dump_trade_chart'], env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out): return None, (r.stdout + r.stderr)[-400:]
    return json.load(open(out)), None


def series(ch, label=None, starts=None):
    for d in ch['data']['datasets']:
        if (label and d['label'] == label) or (starts and d['label'].startswith(starts)): return d['data']
    return None


def near(line, i, span=2):
    """The line's value at grid point i, or the nearest drawn point within `span` points."""
    for k in sorted(range(max(0, i - span), min(len(line), i + span + 1)), key=lambda k: abs(k - i)):
        if line[k] is not None: return line[k]
    return None


def check(tid):
    c = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=10)
    row = c.execute("SELECT bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE id = ?", (tid,)).fetchone()
    if not row: print(f"trade #{tid}: not found"); return False
    bot, real, ts, cl, data = row; v = json.loads(data)
    entry, exit_, n = v.get('entry'), v.get('exit'), v.get('contracts') or 0
    print(f"\ntrade #{tid} {bot} {'REAL' if real else 'paper'}: {v.get('sport')} {v.get('home')} v {v.get('away')} | bought {hms(ts)} {n:g} at {100 * entry:.1f}c (${v.get('cost', 0):.2f}, stake x{v.get('stake_x', 1)})"
          + (f" | sold {hms(cl)} at {100 * exit_:.1f}c, {v.get('reason')}, {v.get('pnl', 0):+.2f}" if cl and exit_ is not None else ' | still open'))
    bad, warn, pages = [], [], []
    for which, open_, now in (('BUY', True, ts + 3000),) + ((('SALE', False, cl + 3000),) if cl else ()):
        ch, err = dump(tid, open_, now)
        if ch is None: bad.append(f"{which} chart: none was built ({(err or '').strip().splitlines()[-1] if err else 'no output'})"); continue
        L = ch['data']['labels']; lab = [x.replace('​', '') for x in L]
        buy, sell, sig = series(ch, 'Buy'), series(ch, 'Sell'), series(ch, 'Bookmaker move')
        bid, ask, mid = series(ch, 'Polymarket sell'), series(ch, 'Polymarket buy'), series(ch, 'All bookmakers')
        trig = series(ch, v.get('trigger')) if v.get('trigger') else None
        target = series(ch, starts='Plan: cash out'); be = series(ch, starts='Plan: break-even')
        pts = lambda s: [(i, x) for i, x in enumerate(s or []) if x is not None]
        B = pts(buy)
        if len(B) != 1: bad.append(f"{which}: {len(B)} buy markers (expected 1)"); continue
        ib, pb = B[0]
        if abs(pb - 100 * entry) > 0.06: bad.append(f"{which}: buy marker at {pb}c, the trade says {100 * entry:.1f}c")
        if lab[ib] not in (hm(ts), hm(ts + 60_000)): bad.append(f"{which}: buy marker at {lab[ib]}, the trade says {hms(ts)}")
        a = near(ask, ib)
        if a is None: warn.append(f"{which}: no offer line near the buy")
        elif abs(a - pb) > 3.0: warn.append(f"{which}: buy marker {pb}c sits {abs(a - pb):.1f}c from the offer line ({a}c)")
        M = pts(sig)
        if len(M) != 1: warn.append(f"{which}: {len(M)} bookmaker-move markers (expected 1)")
        elif M[0][0] > ib: bad.append(f"{which}: the bookmaker move is drawn after the buy")
        if trig is None or near(trig, ib, 6) is None: warn.append(f"{which}: no line for the trigger bookmaker ({v.get('trigger')}) near the buy")
        if near(mid, ib, 6) is None: warn.append(f"{which}: no 'All bookmakers' line near the buy")
        if target and target[0] is not None and not (pb < target[0] <= 100): bad.append(f"{which}: cash-out plan {target[0]}c is not above the buy {pb}c")
        if be and be[0] is not None and not (pb <= be[0] <= 100): bad.append(f"{which}: break-even {be[0]}c is under the buy {pb}c")
        vals = [x for s in (bid, ask, mid, trig or []) for x in s if x is not None]
        if vals and not (0 <= min(vals) and max(vals) <= 100): bad.append(f"{which}: a line leaves 0-100c ({min(vals)} to {max(vals)})")
        notes = ch['options'].get('annotation', {}).get('annotations', [])
        if not any('gives up' in str(x.get('label', {}).get('content', '')) for x in notes): warn.append(f"{which}: no give-up line")
        title = ch['options']['title']['text']; title = title if isinstance(title, str) else ' '.join(title)
        if str(v.get('home')) not in title or str(v.get('away')) not in title: bad.append(f"{which}: the title does not name the game")
        if open_:
            if pts(sell): bad.append("BUY: a sale marker on the chart of an open trade")
            drawn_after = [i for i, x in pts(bid) if i > ib + 1]
            if drawn_after: bad.append("BUY: the price line runs on after 'now' (it shows the future)")
            if not any(x.get('label', {}).get('content') == 'Now' for x in notes): warn.append("BUY: no 'Now' line")
        else:
            Sx = pts(sell)
            if len(Sx) != 1: bad.append(f"SALE: {len(Sx)} sale markers (expected 1)")
            else:
                i_s, ps = Sx[0]
                if abs(ps - 100 * exit_) > 0.06: bad.append(f"SALE: sale marker at {ps}c, the trade says {100 * exit_:.1f}c")
                if lab[i_s] not in (hm(cl), hm(cl + 60_000)): bad.append(f"SALE: sale marker at {lab[i_s]}, the trade says {hms(cl)}")
                if i_s < ib: bad.append("SALE: the sale is drawn before the buy")
                b = near(bid, i_s)
                if b is None: warn.append("SALE: no price line near the sale")
                elif abs(b - ps) > 3.0: warn.append(f"SALE: sale marker {ps}c sits {abs(b - ps):.1f}c from the price line ({b}c)")
                hole = [lab[i] for i in range(ib, i_s + 1) if bid[i] is None]
                if len(hole) > max(1, (i_s - ib) // 4): warn.append(f"SALE: the price line is broken for {len(hole)} of {i_s - ib + 1} points while the trade was open")
            # the money: result = shares x (sale - buy) - both fees
            if exit_ is not None and v.get('pnl') is not None:
                rate = v.get('fee_rate', 0.05); fees = n * rate * (entry * (1 - entry) + exit_ * (1 - exit_))
                want = n * (exit_ - entry) - fees
                if abs(want - v['pnl']) > max(0.05, 0.02 * abs(v.get('cost', 0))): warn.append(f"SALE: result {v['pnl']:+.2f} but shares x (sale - buy) - fees = {want:+.2f}")
        pages.append((which, ch))
    # Each chart as a SHORT snippet for the app's browser pane: run it on the bot's own page (127.0.0.1:8787) to
    # draw it with Chart.js 2.9.4, as QuickChart does for Telegram. Packed (repeated labels, empty points and flat
    # lines are folded) so it is a few KB; a served page would need a macOS permission the owner has to give.
    def pack(ch):
        ch = json.loads(json.dumps(ch)); L = ch['data']['labels']; runs = []
        for x in L:
            k = x.replace('\u200b', '')
            if runs and runs[-1][0] == k: runs[-1][1] += 1
            else: runs.append([k, 1])
        ch['data']['labels'] = runs
        for a in ch['options'].get('annotation', {}).get('annotations', []):
            for k in ('value', 'xMin', 'xMax'):
                if isinstance(a.get(k), str) and '\u200b' in a[k]: a[k] = a[k].replace('\u200b', '') + '~' + str(a[k].count('\u200b'))
        for d in ch['data']['datasets']:
            v = d['data']; some = [(i, x) for i, x in enumerate(v) if x is not None]
            if some and len(some) == len(v) and len({x for _, x in some}) == 1: d['data'] = {'k': some[0][1]}
            elif len(some) <= 3: d['data'] = {'s': some}
            else:
                while v and v[-1] is None: v = v[:-1]
                d['data'] = v
        return ch
    snips = []
    for w, ch in pages:
        snip = os.path.join(OUT, f"trade_{tid}_{w.lower()}.js"); snips.append(snip)
        open(snip, 'w').write("(async()=>{const L=u=>new Promise((y,n)=>{if([...document.scripts].some(s=>s.src===u))return y();const s=document.createElement('script');s.src=u;s.onload=y;s.onerror=()=>n(new Error('blocked '+u));document.head.appendChild(s)});"
                              "await L('https://cdnjs.cloudflare.com/ajax/libs/Chart.js/2.9.4/Chart.min.js');await L('https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-annotation/0.5.7/chartjs-plugin-annotation.min.js');"
                              "const c=" + json.dumps(pack(ch), separators=(',', ':'), ensure_ascii=False).replace('\u200b', '') + ";"
                              "const lb=[];for(const [k,r] of c.data.labels)for(let i=0;i<r;i++)lb.push(k+'\\u200b'.repeat(i));c.data.labels=lb;const N=lb.length;"
                              "for(const d of c.data.datasets){const v=d.data;if(!Array.isArray(v)){const a=new Array(N).fill(null);if('k' in v)a.fill(v.k);else for(const [i,x] of v.s)a[i]=x;d.data=a}else while(v.length<N)v.push(null)}"
                              "const fix=x=>{if(typeof x!=='string')return x;const m=x.match(/^(\\d\\d:\\d\\d)~(\\d+)$/);return m?m[1]+'\\u200b'.repeat(+m[2]):x};"
                              "for(const a of (c.options.annotation||{}).annotations||[])for(const k of ['value','xMin','xMax'])if(k in a)a[k]=fix(a[k]);"
                              "document.getElementById('cc-view')?.remove();const b=document.createElement('div');b.id='cc-view';b.style.cssText='position:fixed;inset:0;z-index:99999;background:#fff;overflow:auto;padding:6px';document.body.appendChild(b);"
                              "const v=document.createElement('canvas');v.width=900;v.height=600;v.style.cssText='width:900px;height:600px;display:block;background:#fff';b.appendChild(v);"
                              "Chart.defaults.global.animation=false;c.options.responsive=false;c.options.animation=false;new Chart(v,c);return 'drawn " + f"#{tid} {w}" + ", '+N+' points'})()")
    for x in bad: print('   WRONG  ' + x)
    for x in warn: print('   look   ' + x)
    print(f"   {'CHART WRONG' if bad else 'numbers on the chart agree with the trade' + (' (see the look lines)' if warn else '')}; to look at it: run {' / '.join(os.path.basename(x) for x in snips)} (in {OUT}) in the browser pane on http://127.0.0.1:8787")
    return not bad


if __name__ == '__main__':
    ids = sys.argv[1:] or ['last']
    if ids == ['last']:
        c = sqlite3.connect('file:' + DB + '?mode=ro', uri=True, timeout=10)
        ids = [c.execute("SELECT id FROM bot_trades WHERE bot LIKE 'snipe%' AND closed_ms IS NOT NULL ORDER BY closed_ms DESC LIMIT 1").fetchone()[0]]
    ok = all([check(int(i)) for i in ids])
    sys.exit(0 if ok else 1)
