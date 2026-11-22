"""Step 1 of the suspicious-CS2 study: the full Polymarket history of the tracked wallets (the 81 accounts of the
"CS2 Polymarket Team Report" sheet), and the facts of every CS2 market they touched.

  wallets/<addr>.json   every activity row, compact: [ts, kind(B/S/R/X), condition, outcome index, price, shares, usdc, event slug]
  markets.json          condition -> {title, type (moneyline / child_moneyline / ...), event slug, start, end, closed, result index, outcomes, volume}

Read-only, public APIs, cached (a wallet or a market already on disk is not fetched again unless --refresh).
usage: fetch_wallets.py <Traders tab .csv> [--refresh]
"""
import sys, os, csv, json, time, urllib.request, urllib.parse, concurrent.futures as cf, threading

OUT = os.path.expanduser('~/swifteye/research/cs2sus')
os.makedirs(OUT + '/wallets', exist_ok=True)
REFRESH = '--refresh' in sys.argv
_lock = threading.Lock(); _next = [0.0]
import ssl
CTX = ssl.create_default_context(cafile='/etc/ssl/cert.pem')   # this Python has no certificate bundle of its own
ERR = [0]


def get(url, gap=0.08):
    for k in range(7):
        with _lock:
            now = time.time(); at = max(now, _next[0]); _next[0] = at + gap
        if at > now: time.sleep(at - now)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/8', 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    import gzip; raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            time.sleep(2 * (k + 1))
        except Exception as e:
            ERR[0] += 1
            if ERR[0] <= 3: print('request failed:', repr(e)[:160], flush=True)
            time.sleep(1 + k)
    return None


def wallet(addr):
    p = f'{OUT}/wallets/{addr}.json'
    if os.path.exists(p) and not REFRESH: return json.load(open(p))
    st = get(f'https://data-api.polymarket.com/v2/user-stats?user={addr}') or {}
    rows, cursor, more = [], '', False
    for page in range(400):
        j = get(f'https://data-api.polymarket.com/v2/activity?user={addr}&limit=1000' + (f'&cursor={cursor}' if cursor else ''))
        if not j: break
        for r in j.get('data') or []:
            kind = 'R' if r.get('type') == 'REDEEM' else 'B' if r.get('side') == 'BUY' else 'S' if r.get('side') == 'SELL' else 'X'
            rows.append([int(r['timestamp']), kind, r.get('condition_id'), int(r.get('outcome_index') or 0), float(r.get('price') or 0), float(r.get('size') or 0), float(r.get('usdc_size') or 0), r.get('event_slug') or r.get('slug') or ''])
        pg = j.get('pagination') or {}
        cursor = pg.get('next_cursor') if pg.get('has_more') else ''
        if not cursor: break
        if page == 399: more = True
    d = dict(addr=addr, join=(st.get('data') or {}).get('join_date'), pnl=((st.get('data') or {}).get('all_time_pnl') or {}).get('realized_market_pnl'), more=more, rows=rows)
    json.dump(d, open(p, 'w'))
    return d


def main():
    traders = [r for r in csv.reader(open(sys.argv[1], encoding='utf-8'))][1:]
    addrs = [(r[0], r[8].strip().lower()) for r in traders if len(r) > 8 and len(r[8].strip()) == 42]
    print(len(addrs), 'wallets'); t0 = time.time()
    with cf.ThreadPoolExecutor(6) as ex: W = list(ex.map(lambda a: wallet(a[1]), addrs))
    json.dump({a: n for n, a in addrs}, open(OUT + '/names.json', 'w'))
    tot = sum(len(w['rows']) for w in W)
    print('failed requests:', ERR[0])
    print(f"{tot} activity rows, {sum(1 for w in W if w['more'])} wallets cut at the page limit, {time.time() - t0:.0f} s")
    cids = sorted({r[2] for w in W for r in w['rows'] if r[7].startswith('cs2-') and r[2]})
    mp = OUT + '/markets.json'; M = json.load(open(mp)) if os.path.exists(mp) else {}
    todo = [c for c in cids if c not in M or (REFRESH and not M[c].get('closed'))]
    print(len(cids), 'CS2 markets,', len(todo), 'to look up')

    def look(batch):
        q = '&'.join('condition_ids=' + c for c in batch)
        out = {}
        for closed in ('true', 'false'):
            j = get(f'https://gamma-api.polymarket.com/markets?{q}&closed={closed}&limit=100') or []
            for m in j:
                try: prices = [float(x) for x in json.loads(m.get('outcomePrices') or '[]')]
                except Exception: prices = []
                try: outs = json.loads(m.get('outcomes') or '[]')
                except Exception: outs = []
                ev = (m.get('events') or [{}])[0]
                res = None
                if m.get('closed') and len(prices) == 2 and max(prices) >= 0.99: res = prices.index(max(prices))
                out[m['conditionId']] = dict(title=m.get('question'), type=m.get('sportsMarketType'), event=ev.get('slug'), eid=ev.get('id'), start=m.get('gameStartTime') or ev.get('startTime'),
                                             end=m.get('closedTime') or m.get('endDate'), closed=bool(m.get('closed')), res=res, outs=outs, vol=float(m.get('volumeNum') or 0), item=m.get('groupItemTitle'))
        return out
    batches = [todo[i:i + 20] for i in range(0, len(todo), 20)]
    with cf.ThreadPoolExecutor(6) as ex:
        for o in ex.map(look, batches): M.update(o)
    json.dump(M, open(mp, 'w'))
    print(f"{len(M)} markets on file, {sum(1 for c in cids if c in M and M[c]['res'] is not None)} of these wallets' CS2 markets have a result; {time.time() - t0:.0f} s")


if __name__ == '__main__': main()
