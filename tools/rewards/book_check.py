import json, time, urllib.request, sys
def get(url):
    err = None
    for a in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'research-script/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read().decode())
        except Exception as e:
            err = e; time.sleep(1.5 * (a + 1))
    return {'error': str(err)}
rows = json.load(open('reward_markets.json'))
rate = lambda r: sum(float(c.get('rate_per_day') or 0) for c in (r.get('rewards_config') or []))
def qscore(book, mid, v, minsz):
    """raw sum of S x size over resting orders within v cents of mid (orders under the min size do not count), bids and asks of this token"""
    qb = sum(((v - (mid - float(o['price'])) * 100) / v) ** 2 * float(o['size']) for o in book.get('bids', []) if 0 <= (mid - float(o['price'])) * 100 < v and float(o['size']) >= minsz)
    qa = sum(((v - (float(o['price']) - mid) * 100) / v) ** 2 * float(o['size']) for o in book.get('asks', []) if 0 <= (float(o['price']) - mid) * 100 < v and float(o['size']) >= minsz)
    return qb, qa
picks = [r for r in rows if rate(r) > 0]
import random
random.seed(5)
sample = sorted(picks, key=lambda r: -rate(r))[:12] + random.sample([r for r in picks if 20 <= rate(r) < 200], 14) + random.sample([r for r in picks if 5 <= rate(r) < 20], 8)
print(f'{"rate/day":>9} {"min":>5} {"v":>4} {"compet":>9} | {"mid":>5} {"bestbid":>7} {"bestask":>7} | {"Q bids":>9} {"Q asks":>9} {"min(Qb,Qa)":>10} | compet / min(Q) | question')
ratios = []
for r in sample:
    tok = (r.get('tokens') or [{}])[0]
    b = get('https://clob.polymarket.com/book?token_id=' + str(tok.get('token_id')))
    time.sleep(0.15)
    if 'error' in b or not b.get('bids') or not b.get('asks'):
        print(f'{rate(r):>9.1f} {float(r.get("rewards_min_size") or 0):>5.0f} {r.get("rewards_max_spread")!s:>4} {float(r.get("market_competitiveness") or 0):>9.2f} | one-sided or empty book | {(r.get("question") or "")[:50]}'); continue
    bb, ba = max(float(o['price']) for o in b['bids']), min(float(o['price']) for o in b['asks'])
    mid = (bb + ba) / 2
    v, ms = float(r.get('rewards_max_spread') or 0), float(r.get('rewards_min_size') or 0)
    qb, qa = qscore(b, mid, v, ms)
    comp = float(r.get('market_competitiveness') or 0)
    mq = min(qb, qa)
    if mq > 0 and comp > 0: ratios.append(comp / mq)
    print(f'{rate(r):>9.1f} {ms:>5.0f} {v:>4} {comp:>9.2f} | {mid * 100:>5.1f} {bb * 100:>7.1f} {ba * 100:>7.1f} | {qb:>9.0f} {qa:>9.0f} {mq:>10.0f} | {comp / mq if mq > 0 else float("nan"):>10.5f}    | {(r.get("question") or "")[:50]}')
import statistics
if ratios: print('compet / min(Q): median', statistics.median(ratios), 'quartiles', statistics.quantiles(ratios, n=4))
