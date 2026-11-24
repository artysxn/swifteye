"""Recorder 2 for the reward markets (24 Nov morning): every 2 minutes the books of every watched reward market in a
few batch calls (POST /books, read-only public data), scored with the published reward formula; the reward list itself
is read again every 30 minutes so NEW markets enter the watch set as they appear.

Watch set: pot $5+ a day, minimum size 50 shares or less, 24-hour volume under $2,000, 2+ days to the end (once in,
a market stays watched while it is on the list).
Files (in this folder):
  rec2_markets.json   static facts per market: idx, condition, question, tokens, created_at, end_date, first_seen
  rec2_list.jsonl     one line per refresh: {t, m: [[idx, pot a day, min size, max spread, 24h volume, Polymarket competitiveness], ...]}
  rec2_books.jsonl    one line per pass: {t, key (1 = every market written), rows: [[idx, mid, best bid, best ask, Q bids, Q asks, raw best bid, raw best ask, last trade], ...], miss: [idx...]}
                      a market is written only when its row changed since it was last written (mid None = no two-sided book of the minimum size)
usage: python rec2.py [hours, default 48]"""
import json, os, sys, time, urllib.request, urllib.parse, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 48.0
sys.argv = [sys.argv[0]]
import snap as SN
UA = {'User-Agent': 'research-script/1.0', 'Accept': 'application/json', 'Content-Type': 'application/json'}
def post(url, body, tries=3):
    err = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA, method='POST')
            with urllib.request.urlopen(req, timeout=40) as r: return json.loads(r.read().decode())
        except Exception as e:
            err = e; time.sleep(2 * (a + 1))
    return {'error': str(err)}
def parse(e):
    e = (e or '').replace(' ', 'T').replace('Z', '+00:00')
    if e.endswith('+00'): e += ':00'
    try:
        d = dt.datetime.fromisoformat(e)
        return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
    except Exception: return None
MP = os.path.join(HERE, 'rec2_markets.json')
M = json.load(open(MP)) if os.path.exists(MP) else {}          # condition -> static facts
cfg = {}                                                        # condition -> (rate, min size, max spread)
def refresh():
    rows, cur = [], None
    for page in range(120):
        q = {'order_by': 'rate_per_day', 'position': 'DESC', 'page_size': '100'}
        if cur: q['next_cursor'] = cur
        d = SN.get('https://clob.polymarket.com/rewards/markets/multi?' + urllib.parse.urlencode(q))
        if 'error' in d: return None
        data = d.get('data') or []
        rows += data
        cur = d.get('next_cursor')
        if not data or not cur or cur == 'LTE=' or SN.rate(data[-1]) < 5: break
        time.sleep(0.15)
    now = time.time(); line = []; live = set()
    for r in rows:
        c = r['condition_id']; rate, ms, v = SN.rate(r), float(r.get('rewards_min_size') or 0), float(r.get('rewards_max_spread') or 0)
        vol = float(r.get('volume_24hr') or 0); end = parse(r.get('end_date'))
        ok = rate >= 5 and 0 < ms <= 50 and v > 0 and len(r.get('tokens') or []) == 2
        if c not in M:
            if not (ok and vol < 2000 and (end is None or end - now >= 2 * 86400)): continue
            M[c] = {'idx': len(M), 'q': (r.get('question') or '')[:90], 'yes': r['tokens'][0]['token_id'], 'no': r['tokens'][1]['token_id'], 'created': r.get('created_at'), 'end': r.get('end_date'), 'first_seen': int(now), 'slug': r.get('market_slug')}
        if not ok: continue
        live.add(c); cfg[c] = (rate, ms, v)
        line.append([M[c]['idx'], rate, ms, v, round(vol), round(float(r.get('market_competitiveness') or 0), 3)])
    for c in list(cfg):
        if c not in live: del cfg[c]
    json.dump(M, open(MP + '.tmp', 'w')); os.replace(MP + '.tmp', MP)
    with open(os.path.join(HERE, 'rec2_list.jsonl'), 'a') as fh: fh.write(json.dumps({'t': int(now), 'm': line}) + '\n')
    return len(rows)
end_at = time.time() + HOURS * 3600
last = {}; t_ref = 0; key = True
r6 = lambda x: None if x is None else round(x, 4)
while time.time() < end_at:
    t0 = time.time()
    if t0 - t_ref >= 1800:
        n = refresh()
        if n is not None: t_ref = t0; key = True
        print(time.strftime('%H:%M:%S'), 'list:', n, 'rows read, watching', len(cfg), 'of', len(M), 'known,', round(time.time() - t0), 's', flush=True)
    by_tok = {M[c]['yes']: c for c in cfg}
    toks = list(by_tok); rows = []; seen = set(); bad = 0
    for i in range(0, len(toks), 200):
        d = post('https://clob.polymarket.com/books', [{'token_id': t} for t in toks[i:i + 200]])
        time.sleep(0.15)
        if not isinstance(d, list): bad += 1; continue
        for b in d:
            c = by_tok.get(b.get('asset_id'))
            if c is None: continue
            seen.add(c)
            rate, ms, v = cfg[c]
            sc = SN.score(b, v, ms)
            rb = max((float(o['price']) for o in b.get('bids', [])), default=None); ra = min((float(o['price']) for o in b.get('asks', [])), default=None)
            try: ltp = float(b.get('last_trade_price')) if b.get('last_trade_price') not in (None, '') else None
            except Exception: ltp = None
            row = (M[c]['idx'],) + ((r6(sc[0]), r6(sc[1]), r6(sc[2]), round(sc[3], 2), round(sc[4], 2)) if sc else (None, None, None, None, None)) + (rb, ra, ltp)
            if key or last.get(c) != row: rows.append(row); last[c] = row
    miss = [M[c]['idx'] for c in cfg if c not in seen] if not bad else []
    with open(os.path.join(HERE, 'rec2_books.jsonl'), 'a') as fh: fh.write(json.dumps({'t': int(t0), 'key': 1 if key else 0, 'bad': bad, 'rows': rows, 'miss': miss}) + '\n')
    if key or int(t0) % 1800 < 125: print(time.strftime('%H:%M:%S'), 'pass:', len(seen), 'books,', len(rows), 'written,', len(miss), 'missing, bad calls', bad, round(time.time() - t0, 1), 's', flush=True)
    if not bad: key = False
    time.sleep(max(5, 120 - (time.time() - t0)))
