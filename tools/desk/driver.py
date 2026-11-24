"""A bookmaker driver for Swifteye's order desk (docs/DESK.md): the part that sits where you are signed in.

  python tools/desk/driver.py --book unibet --desk-json ~/swifteye/desk.json            (your site code, see Site below)
  python tools/desk/driver.py --book testbook --desk-json ~/swifteye/desk.json --mock   (a pretend bookmaker, for trying the desk)

What this file already does, so the site code cannot get it wrong:
  * asks the desk for the book's next order and waits for one (GET /desk/next, the key from desk.json as X-Desk-Key)
  * never handles the same order id twice (ids are kept in driver_<book>_<desk>_done.json beside this file)
  * reads the bet slip FIRST and refuses when its odds are under the order's min_odds or its limit is under the stake
  * a dry run (order["dry_run"]) stops after reading the slip: nothing is confirmed
  * answers the desk (POST /desk/result): placed / rejected / failed, with the odds, the stake, the slip's limit and the bet id
  * when it cannot tell whether a bet went in (Unsure), it answers NOTHING: the desk marks the order "unknown" and you look

  * SIGNING IN: before an order it asks the site "am I signed in?" (Site.signed_in). If not, it fetches the login you
    saved on Swifteye's Order desk page (GET /desk/account, needs the desk key) and hands it to Site.sign_in. The
    password is never printed or written anywhere by this file. It tells the desk how that went:
      signed_in    it worked
      refused      the site says the login is wrong -> the desk will NOT hand it out again until you save it again
      needs_owner  the site asked for a captcha, an emailed code or an identity check (raise NeedsOwner): the driver
                   stops trying, you get a message, you sign in by hand in that browser, and it carries on
    Nothing here solves a captcha or works around a check: those are yours.

What you write: the methods of Site, for one bookmaker's pages, in a browser profile of your own.

An order looks like this:
  {"id": 41, "ticket": 40, "book": "unibet", "dry_run": false, "mode": "real",
   "match_id": 30112, "market": "1x2", "line": null, "outcome": "home",       <- the match's orientation
   "min_odds": 2.05, "odds_seen": 2.10, "stake": 25.0, "answer_by_ms": 1795440000000,
   "info": {"known": true, "event_id": "1024671883",                           <- the BOOK's own event id
            "home": "Arsenal", "away": "Chelsea", "league": "...", "sport": "football", "start_ms": ..., "live": false,
            "teams_swapped": false, "book_outcome": "home", "book_line": null,  <- as THIS book lists the teams
            "pick": "Arsenal",                                                  <- the selection's name on the site
            "seen_odds": 2.10, "seen_changed_ms": ..., "page_read_ms": ...}}
Use info.event_id / info.pick / info.book_outcome / info.book_line to find the selection: the book may list the teams
the other way round from the match (teams_swapped), and then "home" in the order is the book's away side.
"""
import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


class Refused(Exception):
    """The site will not take the bet as ordered (price moved, limit too low, market closed). NOTHING was bet."""

    def __init__(self, why, odds=None, max_stake=None, status='rejected'):
        super().__init__(why)
        self.why, self.odds, self.max_stake, self.status = why, odds, max_stake, status


class Unsure(Exception):
    """Confirm was pressed (or may have been) and the site's answer was not seen. The bet MAY be in."""


class NeedsOwner(Exception):
    """Signing in needs the owner: a captcha, a code sent by email or phone, an identity check. Never worked around."""


class Site:
    """One bookmaker's pages. Fill these in for your book; keep them small and literal."""

    def signed_in(self):
        """Is the site showing a signed-in session right now? (Look for the balance or the account menu.)"""
        return True

    def sign_in(self, login, password):
        """Type the login into the site's own sign-in form and submit it. Return when signed in.
        Raise Refused when the site says the email or password is wrong.
        Raise NeedsOwner (with what it asked for) when it shows a captcha, asks for a code or wants a document."""
        raise NotImplementedError('sign_in: not wired for this book yet')

    def open_slip(self, order):
        """Put the order's selection on the bet slip and return (odds, max_stake) as the slip shows them
        (max_stake None when the site does not say). Raise Refused when the market is closed or not found."""
        raise NotImplementedError('open_slip: not wired for this book yet')

    def confirm(self, order, stake):
        """Type the stake, press the site's confirm button and wait for its answer.
        Return (odds, stake, bet_ref) of the ACCEPTED bet. Raise Refused when the site says no and nothing was bet.
        Raise Unsure when confirm was pressed and no clear answer came (time-out, page gone)."""
        raise NotImplementedError('confirm: not wired for this book yet')

    def clear_slip(self):
        """Empty the bet slip (after a dry run or a refusal). Must never confirm anything."""


class MockSite(Site):
    """A pretend bookmaker: offers the order's odds_seen moved by a random amount, with a stake limit."""

    def __init__(self, limit=200.0, wobble=0.03, lost=0.0, signed_out=False):
        self.limit, self.wobble, self.lost = limit, wobble, lost
        self.n = 0
        self.session = not signed_out

    def signed_in(self):
        return self.session

    def sign_in(self, login, password):
        # A pretend site: a login that starts with "wrong" is refused, one that starts with "captcha" wants the owner.
        if login.startswith('wrong'):
            raise Refused('wrong email or password')
        if login.startswith('captcha'):
            raise NeedsOwner('captcha at sign-in')
        self.session = bool(login and password)

    def open_slip(self, order):
        self.odds = round(order['odds_seen'] * (1 + random.uniform(-self.wobble, self.wobble)), 3)
        return self.odds, self.limit

    def confirm(self, order, stake):
        if random.random() < self.lost:
            raise Unsure('mock: the page hung after confirm')
        self.n += 1
        return self.odds, stake, f'MOCK-{order["id"]}-{self.n}'


def call(api, key, method, path, body=None, timeout=40):
    req = urllib.request.Request(api + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'X-Desk-Key': key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'{method} {path}: HTTP {e.code}: {e.read().decode()[:300]}') from None


def ensure_signed_in(site, api, key, book):
    """True when the site is signed in (signing in with the saved login if it was not). Tells the desk either way."""
    tell = lambda state, detail='': call(api, key, 'POST', '/desk/account/state', {'book': book, 'state': state, 'detail': detail})
    if site.signed_in():
        return True
    try:
        acc = call(api, key, 'GET', f'/desk/account?book={book}')
    except Exception as e:  # nothing saved, or the desk is holding the login back: say why, do not guess
        print(f'signed out, and the desk gives no login: {str(e)[:220]}', flush=True)
        try:
            tell('signed_out', 'signed out; no login to use')
        except Exception:
            pass
        return False
    try:
        site.sign_in(acc['login'], acc['password'])
    except Refused as e:
        tell('refused', e.why)
        print('the site refused the saved login: fix it on the Order desk page', flush=True)
        return False
    except NeedsOwner as e:
        tell('needs_owner', str(e)[:200])
        print(f'signing in needs you: {e}', flush=True)
        return False
    except NotImplementedError as e:
        tell('signed_out', str(e))
        return False
    except Exception as e:  # never the password: only the kind of error
        tell('signed_out', f'sign-in failed: {type(e).__name__}')
        return False
    finally:
        acc = None
    if site.signed_in():
        tell('signed_in')
        return True
    tell('signed_out', 'the sign-in form went through but the site still shows signed out')
    return False


def handle(site, order, partial=False):
    """One order -> the answer for the desk, or None when nothing may be said (Unsure)."""
    ans = {'id': order['id']}
    try:
        odds, limit = site.open_slip(order)
        ans.update(odds=odds, max_stake=limit)
        if odds < order['min_odds'] - 1e-9:
            raise Refused(f'the slip shows {odds}, under the minimum of {order["min_odds"]}', odds, limit, 'price_changed')
        stake = order['stake']
        if limit is not None and limit < stake - 1e-9:
            if not partial or order['dry_run'] or limit <= 0:
                raise Refused(f'the slip takes at most {limit}, the order is for {stake}', odds, limit, 'limit')
            stake = limit
        if order['dry_run']:
            site.clear_slip()
            return dict(ans, status='placed', stake=min(stake, limit) if limit is not None else stake, message='dry run: slip read, nothing confirmed')
        got_odds, got_stake, ref = site.confirm(order, stake)
        return dict(ans, status='placed', odds=got_odds, stake=got_stake, bet_ref=str(ref or ''))
    except Refused as e:
        try:
            site.clear_slip()
        except Exception:
            pass
        return dict(ans, status=e.status, odds=e.odds if e.odds is not None else ans.get('odds'), max_stake=e.max_stake if e.max_stake is not None else ans.get('max_stake'), message=e.why)
    except Unsure as e:
        print(f'!! order {order["id"]}: NOT SURE whether the bet went in ({e}). Nothing reported: check the site, then report it by hand.', flush=True)
        return None
    except NotImplementedError as e:
        return dict(ans, status='failed', message=str(e))
    except Exception as e:  # anything before confirm was pressed: nothing was bet
        return dict(ans, status='failed', message=f'{type(e).__name__}: {e}'[:300])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--desk-json', required=True, help='desk.json next to the Swifteye database (holds the key)')
    ap.add_argument('--api', default='http://127.0.0.1:8787')
    ap.add_argument('--name', default=None)
    ap.add_argument('--mock', action='store_true', help='a pretend bookmaker instead of a site')
    ap.add_argument('--mock-limit', type=float, default=200.0)
    ap.add_argument('--mock-wobble', type=float, default=0.03)
    ap.add_argument('--mock-signed-out', action='store_true', help='the pretend bookmaker starts signed out, so the saved login is used')
    ap.add_argument('--partial', action='store_true', help='stake what the slip allows when its limit is under the order (default: refuse)')
    ap.add_argument('--once', action='store_true', help='handle one order (or none within the wait) and stop')
    a = ap.parse_args()
    key = json.load(open(os.path.expanduser(a.desk_json)))['key']
    site = MockSite(a.mock_limit, a.mock_wobble, signed_out=a.mock_signed_out) if a.mock else Site()
    name = a.name or ('mock' if a.mock else 'driver') + '-' + a.book
    # one list per desk (a sandbox numbers its orders from 1 again): named after a fingerprint of that desk's key
    done_path = os.path.join(HERE, f'driver_{a.book}_{hashlib.sha256(key.encode()).hexdigest()[:8]}_done.json')
    done = set(json.load(open(done_path))) if os.path.exists(done_path) else set()
    print(f'{name}: asking {a.api} for {a.book} orders', flush=True)
    while True:
        try:
            order = call(a.api, key, 'GET', f'/desk/next?book={a.book}&driver={name}&wait=25')['order']
        except Exception as e:
            print('desk not reachable:', str(e)[:200], flush=True)
            if a.once:
                sys.exit(1)
            time.sleep(5)
            continue
        if order is None:
            if a.once:
                return
            continue
        if order['id'] in done:  # the desk never hands an order out twice; this is the second lock on the same door
            print(f'order {order["id"]} was already handled here: skipped', flush=True)
            continue
        done.add(order['id'])
        json.dump(sorted(done)[-5000:], open(done_path, 'w'))
        print(f'order {order["id"]}: {"DRY " if order["dry_run"] else ""}{order["info"].get("pick", order["outcome"])} at {order["min_odds"]}+ for {order["stake"]}', flush=True)
        if ensure_signed_in(site, a.api, key, a.book):
            ans = handle(site, order, a.partial)
        else:  # nothing was bet: the order is answered at once so its ticket does not hang
            ans = {'id': order['id'], 'status': 'failed', 'message': 'the driver is not signed in to the site'}
        if ans is not None:
            ans['driver'] = name
            try:
                t = call(a.api, key, 'POST', '/desk/result', ans)
                print(f'   -> {ans["status"]} {ans.get("odds") or ""} {ans.get("message") or ""} | ticket {t.get("id")} is {t.get("state")}', flush=True)
            except Exception as e:
                print(f'!! order {order["id"]}: the answer did not reach the desk ({e}). It was: {json.dumps(ans)}', flush=True)
        if a.once:
            return


if __name__ == '__main__':
    main()
