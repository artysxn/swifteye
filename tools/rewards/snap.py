"""Snapshot the order books of Polymarket's reward markets and score them with the published reward formula.
usage: python snap.py once|loop [minutes between snapshots]   (read-only public endpoints, paced)"""
import json, os, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
def get(url, tries=3):
    err = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=25) as r: return json.loads(r.read().decode())
        except Exception as e:
            err = e; time.sleep(1.5 * (a + 1))
    return {'error': str(err)}
rate = lambda r: sum(float(c.get('rate_per_day') or 0) for c in (r.get('rewards_config') or []))
def score(book, v, minsz):
    """(adjusted mid, best bid, best ask, Q bids, Q asks, shares within v on each side) counting only orders of the minimum size"""
    bids = [(float(o['price']), float(o['size'])) for o in book.get('bids', []) if float(o['size']) >= minsz]
    asks = [(float(o['price']), float(o['size'])) for o in book.get('asks', []) if float(o['size']) >= minsz]
    if not bids or not asks: return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    qb = sum(((v - (mid - p) * 100) / v) ** 2 * s for p, s in bids if (mid - p) * 100 < v)
    qa = sum(((v - (p - mid) * 100) / v) ** 2 * s for p, s in asks if (p - mid) * 100 < v)
    return mid, bb, ba, qb, qa
def universe():
    rows = json.load(open(os.path.join(HERE, 'reward_markets.json')))
    return [r for r in rows if rate(r) >= 5 and 0 < float(r.get('rewards_min_size') or 0) <= 50 and float(r.get('rewards_max_spread') or 0) > 0]
def snapshot(mk, out):
    n = 0
    with open(out, 'a') as fh:
        for r in mk:
            tok = (r.get('tokens') or [{}])[0].get('token_id')
            b = get('https://clob.polymarket.com/book?token_id=' + str(tok), tries=2)
            time.sleep(0.12)
            if 'error' in b: continue
            v, ms = float(r['rewards_max_spread']), float(r['rewards_min_size'])
            sc = score(b, v, ms)
            raw_b = max((float(o['price']) for o in b.get('bids', [])), default=None)
            raw_a = min((float(o['price']) for o in b.get('asks', [])), default=None)
            fh.write(json.dumps({'t': int(time.time()), 'c': r['condition_id'], 'sc': sc, 'bb': raw_b, 'ba': raw_a, 'nb': len(b.get('bids', [])), 'na': len(b.get('asks', []))}) + '\n')
            n += 1
    return n
if __name__ == '__main__':
    mk = universe()
    if sys.argv[1] == 'once':
        t = time.time(); n = snapshot(mk, os.path.join(HERE, 'snap_first.jsonl')); print(len(mk), 'markets,', n, 'books,', round(time.time() - t), 's')
    else:
        every = float(sys.argv[2]) * 60 if len(sys.argv) > 2 else 600
        want = set(json.load(open(os.path.join(HERE, 'candidates.json')))) if os.path.exists(os.path.join(HERE, 'candidates.json')) else None
        top = [r for r in mk if r['condition_id'] in want] if want else sorted(mk, key=lambda r: -rate(r))[:500]
        end = time.time() + 12 * 3600
        while time.time() < end:
            t = time.time(); n = snapshot(top, os.path.join(HERE, 'snap_loop.jsonl'))
            print(time.strftime('%H:%M:%S'), n, 'books', round(time.time() - t), 's', flush=True)
            time.sleep(max(5, every - (time.time() - t)))
