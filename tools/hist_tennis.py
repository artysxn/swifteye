"""More matches for the pre-match favourite bet: tennis from BEFORE Swifteye's own recording (owner, 21 Nov 2026:
"Need more data for matches. That isn't enough.").

  bookmakers   betexplorer.com: each day's results page (every match with its start time) and each singles
               tournament's results page (the AVERAGE closing odds of the bookmakers for both players)
  Polymarket   Gamma API: closed tennis match markets with their result; CLOB price history around the start
Joined by the two players' names and the day. Everything is cached under ~/swifteye/research/hist/, so a second run
only fetches what is missing. Paced (about 150 KB/s) and it waits while the bot's own feed is late.
usage: hist_tennis.py fetch [from YYYY-MM-DD] [to YYYY-MM-DD]   |   hist_tennis.py join
"""
import sys, os, re, json, time, html, subprocess, unicodedata, datetime as dt
from zoneinfo import ZoneInfo
H = os.path.expanduser('~/swifteye/research/hist/')
os.makedirs(H, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PRG = ZoneInfo('Europe/Prague')      # the site's clock (its "now" stamp matched Central European time)
MODE = sys.argv[1] if len(sys.argv) > 1 else 'fetch'
FROM = sys.argv[2] if len(sys.argv) > 2 else '2026-10-05'
TO = sys.argv[3] if len(sys.argv) > 3 else '2026-11-16'
TENNIS_TAG = '864'
# FAST=1: no waiting for the bot's feed and short pauses (owner, 21 Nov 2026: "You can slow the live bot, just get it done").
FAST = os.environ.get('FAST') == '1'
PAUSE = (lambda s: time.sleep(min(s, 0.4))) if FAST else time.sleep


def curl(u, t=40):
    for k in range(3):
        r = subprocess.run(['curl', '-s', '-m', str(t), '-L', '-A', UA, '-H', 'Accept-Language: en', u], capture_output=True).stdout
        if r: return r.decode('utf-8', 'ignore')
        time.sleep(3)
    return ''


def feed_wait():
    if FAST: return
    while True:
        try:
            d = json.loads(subprocess.run(['curl', '-s', '-m', '8', 'http://127.0.0.1:8787/bots'], capture_output=True, text=True).stdout)
            if (d['snipe']['status'].get('pm_lag_ms') or 0) < 400: return
        except Exception:
            return
        print('   bot feed late: waiting 20 s', flush=True); time.sleep(20)


def days(a, b):
    d = dt.date.fromisoformat(a)
    while d <= dt.date.fromisoformat(b):
        yield d; d += dt.timedelta(days=1)


def fetch_days():
    """Each day's results page -> matches {id: (start ts, tournament path)} and the singles tournaments played."""
    for d in days(FROM, TO):
        f = H + f'day_{d}.json'
        if os.path.exists(f): continue
        feed_wait()
        s = curl(f'https://www.betexplorer.com/tennis/results/?year={d.year}&month={d.month}&day={d.day}')
        PAUSE(5)
        out = {}; tour = None
        for row in re.findall(r'<tr[^>]*>.*?</tr>', s, flags=re.S):
            m = re.search(r'<a href="(/tennis/[^"]+/)" class="table-main__tournament', row)
            if m: tour = m.group(1); continue
            m = re.search(r'data-dt="(\d+),(\d+),(\d+),(\d+),(\d+)"', row); a = re.search(r'href="(/tennis/[^"]+/([A-Za-z0-9]{8})/)"', row)
            if not (m and a and tour): continue
            dd, mm, yy, hh, mi = map(int, m.groups())
            ts = int(dt.datetime(yy, mm, dd, hh, mi, tzinfo=PRG).timestamp())
            out[a.group(2)] = (ts, tour)
        if len(s) < 50_000: print(f'  {d}: short page ({len(s)} bytes), not saved', flush=True); continue
        json.dump(out, open(f, 'w'))
        print(f'  {d}: {len(out)} matches, {len({v[1] for v in out.values() if "singles" in v[1]})} singles tournaments', flush=True)


def fetch_tournaments():
    tours = set()
    for d in days(FROM, TO):
        f = H + f'day_{d}.json'
        if os.path.exists(f): tours |= {v[1] for v in json.load(open(f)).values() if 'singles' in v[1]}
    todo = [t for t in sorted(tours) if not os.path.exists(H + 'tour_' + t.strip('/').replace('/', '__') + '.json')]
    print(f'{len(tours)} singles tournaments, {len(todo)} to fetch', flush=True)
    for k, t in enumerate(todo):
        feed_wait()
        s = curl('https://www.betexplorer.com' + t + 'results/'); PAUSE(2.5)
        rows = []
        for row in re.findall(r'<tr[^>]*>.*?</tr>', s, flags=re.S):
            od = re.findall(r'data-odd="([0-9.]+)"', row); a = re.search(r'href="/tennis/[^"]+/([A-Za-z0-9]{8})/" class="in-match">(.*?)</a>', row, flags=re.S)
            if len(od) < 2 or not a: continue
            names = [html.unescape(re.sub(r'<[^>]+>', '', x)).strip() for x in re.split(r'</span>\s*-\s*<span>', a.group(2))]
            if len(names) != 2: continue
            res = re.search(r'<td class="h-text-center"><a[^>]*>(.*?)</a>', row, flags=re.S)
            rows.append(dict(id=a.group(1), home=names[0], away=names[1], oh=float(od[0]), oa=float(od[1]), res=html.unescape(re.sub(r'<[^>]+>', '', res.group(1))).strip() if res else ''))
        if len(s) > 20_000: json.dump(rows, open(H + 'tour_' + t.strip('/').replace('/', '__') + '.json', 'w'))
        if k % 20 == 19: print(f'  {k + 1}/{len(todo)} tournaments', flush=True)


def fetch_gamma():
    """Closed tennis match markets, listed one day at a time (the API stops paging after about 2,000 rows), $300+ traded."""
    for d in days(FROM, (dt.date.fromisoformat(TO) + dt.timedelta(days=2)).isoformat()):
        f = H + f'gammaday_{d}.json'
        if os.path.exists(f): continue
        mk = []; off = 0; ok = True
        while True:
            feed_wait()
            r = curl(f'https://gamma-api.polymarket.com/markets?tag_id={TENNIS_TAG}&closed=true&sports_market_types=moneyline&end_date_min={d}T00:00:00Z&end_date_max={d + dt.timedelta(days=1)}T00:00:00Z&volume_num_min=300&limit=100&offset={off}&order=endDate&ascending=true')
            PAUSE(3)
            try: j = json.loads(r)
            except Exception: j = None
            if not isinstance(j, list): ok = False; break
            for m in j:
                try: outs = json.loads(m['outcomes']); toks = json.loads(m['clobTokenIds']); px = [float(x) for x in json.loads(m['outcomePrices'])]
                except Exception: continue
                if len(outs) != 2 or not m.get('gameStartTime'): continue
                try: st = int(dt.datetime.fromisoformat(m['gameStartTime'].replace(' ', 'T').replace('+00', '+00:00')).timestamp())
                except Exception: continue
                mk.append(dict(q=m.get('question'), outs=outs, toks=toks, res=px, start=st, vol=round(m.get('volumeNum') or 0), cond=m.get('conditionId')))
            if len(j) < 100: break
            off += 100
            if off > 1900: break
        if ok: json.dump(mk, open(f, 'w'))
        print(f'  polymarket {d}: {len(mk)} markets', flush=True)


def fold(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r"[^a-z. ]", ' ', s.replace('-', ' ')).split()


def be_key(name):
    """'De Minaur A.' -> (('de', 'minaur'), 'a')"""
    t = fold(name); sur = tuple(x for x in t if not x.endswith('.')); ini = [x[0] for x in t if x.endswith('.')]
    return sur, (ini[0] if ini else '')


def same(be, pm):
    sur, ini = be_key(be); p = [x.strip('.') for x in fold(pm)]
    if not sur or not p: return False
    return all(x in p for x in sur) and (not ini or p[0].startswith(ini) or any(x.startswith(ini) for x in p if x not in sur))


def join():
    gm = {}
    for d in days(FROM, (dt.date.fromisoformat(TO) + dt.timedelta(days=2)).isoformat()):
        f = H + f'gammaday_{d}.json'
        if os.path.exists(f):
            for m in json.load(open(f)): gm[m['cond']] = m
    gm = sorted(gm.values(), key=lambda m: m['start'])
    # the results site's clock is UTC+1 all year (its October times ran exactly 1 h ahead of Polymarket's when read as Prague time)
    import calendar
    fix = lambda ts: calendar.timegm(dt.datetime.fromtimestamp(ts, PRG).replace(tzinfo=None).timetuple()) - 3600
    start = {};
    for d in days(FROM, TO):
        f = H + f'day_{d}.json'
        if os.path.exists(f):
            for k, v in json.load(open(f)).items(): start[k] = fix(v[0])
    be = []
    for f in os.listdir(H):
        if f.startswith('tour_'):
            for r in json.load(open(H + f)):
                if r['id'] in start: r['ts'] = start[r['id']]; r['tour'] = f[5:-5]; be.append(r)
    be.sort(key=lambda r: r['ts'])
    print(f'bookmaker side: {len(be)} singles matches with odds and a start time; polymarket side: {len(gm)} markets', flush=True)
    import bisect
    T = [r['ts'] for r in be]
    out = []; fetched = 0
    pf = H + 'prices.json'; P = json.load(open(pf)) if os.path.exists(pf) else {}
    def match_of(m):
        lo, hi = bisect.bisect_left(T, m['start'] - 30 * 3600), bisect.bisect_right(T, m['start'] + 30 * 3600)
        cand = [r for r in be[lo:hi] if (same(r['home'], m['outs'][0]) and same(r['away'], m['outs'][1])) or (same(r['home'], m['outs'][1]) and same(r['away'], m['outs'][0]))]
        return cand[0] if len(cand) == 1 else None
    if FAST:
        from concurrent.futures import ThreadPoolExecutor
        need = [(m['toks'][0], r['ts']) for m in gm for r in [match_of(m)] if r and m['toks'][0] not in P]
        print(f'  price histories to fetch: {len(need)} (8 at a time)', flush=True)
        def one(x):
            try: j = json.loads(curl(f"https://clob.polymarket.com/prices-history?market={x[0]}&startTs={x[1] - 7 * 3600}&endTs={x[1] + 600}&fidelity=1", 20))
            except Exception: j = None
            return x[0], [(y['t'], y['p']) for y in (j or {}).get('history', [])]
        with ThreadPoolExecutor(8) as ex:
            for k, (tok, h) in enumerate(ex.map(one, need)):
                P[tok] = h
                if k % 500 == 499: json.dump(P, open(pf, 'w')); print(f'  {k + 1}/{len(need)} price histories', flush=True)
        json.dump(P, open(pf, 'w'))
    for i, m in enumerate(gm):
        lo, hi = bisect.bisect_left(T, m['start'] - 30 * 3600), bisect.bisect_right(T, m['start'] + 30 * 3600)
        cand = [r for r in be[lo:hi] if (same(r['home'], m['outs'][0]) and same(r['away'], m['outs'][1])) or (same(r['home'], m['outs'][1]) and same(r['away'], m['outs'][0]))]
        if len(cand) != 1: continue
        r = cand[0]; flip = not same(r['home'], m['outs'][0])
        tok = m['toks'][0]
        if tok not in P:
            feed_wait() if fetched % 40 == 0 else None
            j = None
            try: j = json.loads(curl(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={r['ts'] - 7 * 3600}&endTs={r['ts'] + 600}&fidelity=1", 20))
            except Exception: pass
            P[tok] = [(x['t'], x['p']) for x in (j or {}).get('history', [])]
            fetched += 1; time.sleep(0.25)
            if fetched % 200 == 0: json.dump(P, open(pf, 'w')); print(f'  price histories: {fetched} fetched, {len(out)} matches joined', flush=True)
        out.append(dict(q=m['q'], outs=m['outs'], res=m['res'], vol=m['vol'], pm_start=m['start'], start=r['ts'], tour=r['tour'], odds=[r['oa'], r['oh']] if flip else [r['oh'], r['oa']], be_res=r['res'], hist=P[tok]))
    json.dump(P, open(pf, 'w'))
    json.dump(out, open(H + f'joined_{FROM}_{TO}.json', 'w'))
    print(f'joined {len(out)} matches -> {H}joined_{FROM}_{TO}.json', flush=True)


if __name__ == '__main__':
    t0 = time.time()
    if MODE == 'fetch':
        fetch_days(); fetch_tournaments(); fetch_gamma(); join()
    elif MODE == 'join': join()
    print(f'done in {time.time() - t0:.0f} s', flush=True)
