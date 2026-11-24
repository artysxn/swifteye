# The order desk

One way to place a bet at any bookmaker or on Polymarket, from one call. Code: `src/desk.rs`, endpoints under
`/desk`, driver template `tools/desk/driver.py`. Added 24 Nov 2026.

## How it fits together

```
you / a script / a bot ──POST /desk/tickets──▶  DESK (inside Swifteye)
                                                 │  one leg at a time, in the order given
                     ┌───────────────────────────┼─────────────────────────────┐
                     ▼                           ▼                             ▼
            polymarket leg                bookmaker leg                 bookmaker leg
        Swifteye places it itself     waits in that book's queue    waits in that book's queue
          (exec.rs, signed order)             ▲      │                     ▲      │
                                 GET /desk/next      POST /desk/result
                                              │      ▼
                                     YOUR DRIVER for that book
                             (runs where you are signed in to the site)
```

- A **ticket** is one or more **legs**: "back this outcome at this book for at least these odds with this stake".
- **Polymarket** legs are placed by Swifteye, with the same signed order the snipe bot sends.
- Every other book needs a **driver**: your own small program sitting in a browser where you are signed in to that
  bookmaker. It asks the desk for work, places the bet on the site, and reports what happened. Swifteye never holds a
  bookmaker login, never opens a bookmaker's betting pages, and does nothing about a site's bot checks: that side is
  yours.

## Three modes

| mode | what happens | needs |
|---|---|---|
| `paper` (default) | Nothing leaves Swifteye. A leg fills at the price Swifteye has recorded for that book right now (Polymarket: what rests in its order book), or is refused. | nothing |
| `dry` | Every leg goes to its driver at once, flagged `dry_run`. The driver puts the bet on the slip, reads the odds and the largest stake the site offers, and reports them **without confirming**. This is how to learn whether a recorded price can really be had. | a driver per book |
| `real` | Bets are placed, one leg at a time. | a driver per book, and every switch below |

## Real money: the switches

Real is off until **you** switch it on. All of these must hold, or a real ticket is refused before anything is sent:

1. the desk's own `real` is on;
2. that book's `real` is on;
3. the stake is at most that book's `max_stake`;
4. the book's `daily_cap` (real money sent to it in any 24 hours) is not passed;
5. the bots' kill switch is off.

Switching `real` on, or raising a limit, needs `"confirm": "REAL MONEY"` in the same request. Lowering a limit or
switching off needs nothing.

```bash
curl -s -X POST http://127.0.0.1:8787/desk/settings -H "X-Desk-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"real": true, "confirm": "REAL MONEY", "books": {"unibet": {"real": true, "max_stake": 25, "daily_cap": 100, "currency": "EUR"}}}'
```

## The key

Every call that changes the desk or hands out an order must send the desk key as the header `X-Desk-Key`. The key is
made on the first start and kept in `desk.json` next to the database (treat it like `telegram.json`: never commit it).
The API never shows it. Reason: without it, any web page open in your browser could queue a bet through
`127.0.0.1`. A plain in-page `fetch` from a bookmaker's page will not work either (the browser blocks it): use
`GM_xmlhttpRequest` in a userscript, an extension's background page, or a script outside the browser.

`"new_key": true` in `POST /desk/settings` makes a new one.

## Bookmaker logins

You type each bookmaker's email (or username) and password once, on the **Order desk** page of the app. They go to
`desk_accounts.json` next to the database (readable by your user only; treat it like `pinnacle.json`, never commit
it). The API never shows a password again, and the page shows the login cut short (`da…@gmail.com`).

Swifteye itself never signs in to a bookmaker with them. The only reader is that bookmaker's **driver**, when the
site has signed it out:

| call (needs `X-Desk-Key`) | what it does |
|---|---|
| `GET /desk/account?book=unibet` | The login and password, for the driver to type into the site's own sign-in form. |
| `POST /desk/account/state` `{"book": "unibet", "state": "...", "detail": "..."}` | The driver says how it went. |

| state | meaning | what the desk does |
|---|---|---|
| `signed_in` | The site shows a signed-in session. | Nothing; tickets flow. |
| `signed_out` | Signed out, and not (yet) signed in again. | Nothing; the driver may fetch the login. |
| `refused` | The site says the email or password is wrong. | The login is **not handed out again** until you save it again. You get a Telegram message. Dry and real tickets for that bookmaker are refused. |
| `needs_owner` | The site asked for a captcha, an emailed code, an identity check. | The same: held back, you are told, tickets refused, until you have signed in there by hand (the driver then reports `signed_in`) or saved the login again. |

Why the hold-back: repeated failed sign-ins lock accounts, and a captcha or a code is yours to answer, not a
program's. A login also goes to a driver at most 3 times without a `signed_in` in between.

`tools/desk/driver.py` does this for you: before each order it asks `Site.signed_in()`; if not, it fetches the login,
calls your `Site.sign_in(login, password)` and reports the result. Raise `Refused` for a wrong password and
`NeedsOwner("captcha")` for anything only you can do. It never prints or stores the password.

Saving a login is allowed from the app's own page on this machine, or with the desk key
(`POST /desk/accounts {"book", "login", "password", "note"}`; `{"book", "remove": true}` deletes it). An empty
password keeps the one on file. A bookmaker you always sign in to by hand needs no saved login: its driver can still
report `signed_in` / `needs_owner`.

Keep the driver's browser in its own profile, so the session survives restarts and the login is rarely needed.

## Handing in a ticket

```bash
curl -s -X POST http://127.0.0.1:8787/desk/tickets -H "X-Desk-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "mode": "paper",
  "note": "why this ticket exists",
  "legs": [
    {"book": "unibet",     "match_id": 30112, "market": "1x2", "outcome": "home", "min_odds": 2.05, "odds": 2.10, "stake": 25},
    {"book": "polymarket", "match_id": 30112, "outcome": "home", "side": "no", "min_odds": 1.90, "odds": 1.95, "stake": 26.92}
  ]
}'
```

| leg field | meaning |
|---|---|
| `book` | Swifteye's name for the book (`GET /books`), or `polymarket`. |
| `match_id` | Swifteye's match id. |
| `market`, `line` | `1x2` (default; also the winner of a two-way game), `total` (line = goals), `handicap` (line = the HOME side's handicap), `btts`. |
| `outcome` | `home`, `draw`, `away`, `over`, `under`, `yes`, `no`, in the **match's** orientation. |
| `side` | Bookmaker: `back` (default). Polymarket: `yes` = buy the outcome, `no` = buy against it. |
| `min_odds` | The lowest decimal odds to accept. For Polymarket: after its fee. |
| `odds` | The odds the ticket was built on (default: `min_odds`). |
| `stake` | Money to put on, in that account's currency. |

Ticket fields: `mode`, `note`, `ttl_s` (how long a leg may wait for its driver; default 60), `rebalance` (default
true: the legs are meant to pay the same, see below).

**Order of the legs matters.** They are placed in the order given, and a leg only goes out once the one before it is
in. Put the leg most likely to be refused first (a bookmaker with a low limit), the surest last (Polymarket).

`POST /desk/from_arb {"key": "<key of a row on GET /arbs>", "stake": 100, "mode": "dry", "slack": 0.01}` builds the
ticket from an arbitrage the tracker shows now: its legs and stakes, bookmakers first, Polymarket last; `slack` = how
much worse than the tracker's odds each leg may come in (a share of the odds, 0 to 0.05). Kalshi legs are not wired.

## What a ticket can end as

| ticket | meaning |
|---|---|
| `open` | A leg is waiting for, or with, a driver. |
| `done` | Every leg is in, and (with `rebalance`) they pay about the same. |
| `dead` | No leg is in. Nothing was bet. |
| `exposed` | Some legs are in and one is not, or they pay unevenly (over 3% apart): **a bet stands with nothing against it.** Real tickets send a Telegram message. |
| `check` | A driver took a leg and never answered. The bet **may** be in. Look at the site. |

| leg | meaning |
|---|---|
| `waiting` | A later leg, not handed out yet. |
| `queued` | Waiting for its book's driver. Expires after `ttl_s`. |
| `claimed` | A driver has it. It must answer within `claim_s` (default 90). |
| `placed` | Real: the bet is in. Dry: the slip would take it. Paper: filled at the recorded price. |
| `rejected` | The book said no (price gone, limit too low, closed). Nothing was bet. `odds` = what was on offer, `max_stake` = the slip's limit. |
| `failed` | The driver hit an error before confirming. Nothing was bet. |
| `unknown` | The driver never answered. **May be placed.** |
| `expired` | No driver took it in time. Nothing was bet. |
| `cancelled` | Cancelled, or an earlier leg did not go in. |

Rules the desk keeps:

- **An order is handed out once, ever.** A lost answer never causes a second bet; it causes an `unknown` leg that you
  settle by hand: look at the site, then `POST /desk/result` for that order id (`placed` with the odds and stake, or
  `failed`).
- **Re-sizing.** If the first leg comes in at other odds or a smaller stake than planned (the slip only took 25 of
  50), the later legs are re-sized by the same ratio so every leg still pays the same. Never above the book's
  `max_stake`.
- **Cancel** (`POST /desk/tickets/{id}/cancel`) takes back only what no driver holds. A claimed leg cannot be called
  back; if it goes in, the ticket is `exposed`.
- **Restart.** Legs no driver had taken are cancelled; legs a driver held become `unknown`.

## Writing a driver

A driver makes two calls, both with `X-Desk-Key`:

1. `GET /desk/next?book=<book>&driver=<name>&wait=25` waits up to 25 s and returns `{"order": null}` or
   ```json
   {"order": {"id": 41, "ticket": 40, "book": "unibet", "dry_run": false, "mode": "real",
     "match_id": 30112, "market": "1x2", "line": null, "outcome": "home",
     "min_odds": 2.05, "odds_seen": 2.10, "stake": 25.0, "answer_by_ms": 1795440000000,
     "info": {"known": true, "event_id": "1024671883", "sport": "football", "league": "...",
              "home": "Arsenal", "away": "Chelsea", "start_ms": 1795443600000, "live": false,
              "teams_swapped": false, "book_outcome": "home", "book_line": null, "pick": "Arsenal",
              "seen_odds": 2.10, "seen_changed_ms": 1795439990000, "page_read_ms": 1795439999000}}}
   ```
   `info` is in the **book's own terms**: its event id, its team names, and `book_outcome` / `book_line` / `pick` the
   way that book lists the teams. E-sports books often list the teams the other way round from the match
   (`teams_swapped`): then `outcome: "home"` is the book's away side, and `pick` names the right team either way.
   `known: false` = Swifteye does not read that book or it does not list the game; the driver must find it itself.
2. `POST /desk/result` with `{"id": 41, "status": "placed", "odds": 2.08, "stake": 25, "max_stake": 180, "bet_ref": "the site's bet id", "message": "..."}`.
   `status`: `placed`, `rejected` (also `price_changed`, `limit`, `closed`: same thing with the reason), `failed`.

What a driver must do, in this order:

1. Put the selection on the slip and read the **slip's** odds and limit.
2. Slip odds under `min_odds`: answer `price_changed` with those odds. Do not bet.
3. Limit under the stake: answer `limit` with `max_stake`. Do not bet (or stake the limit, if you choose that, and
   report the real stake: the desk re-sizes the other legs).
4. `dry_run`: answer `placed` with the slip's odds and limit, and clear the slip. Never confirm.
5. Otherwise confirm, wait for the site's answer, and report the odds and stake **the site accepted**.
6. If confirm was pressed and no clear answer came: **report nothing.** The desk marks the order `unknown` and you look.

`tools/desk/driver.py` does all of that around three small methods you write per book (`open_slip`, `confirm`,
`clear_slip`), and also refuses to touch an order id it has already handled. `--mock` turns it into a pretend
bookmaker.

A userscript does the same with two `GM_xmlhttpRequest` calls:

```js
// @grant GM_xmlhttpRequest   // @connect 127.0.0.1
const call = (method, path, body) => new Promise((ok, no) => GM_xmlhttpRequest({
  method, url: 'http://127.0.0.1:8787' + path, data: body && JSON.stringify(body), timeout: 40000,
  headers: { 'X-Desk-Key': KEY, 'Content-Type': 'application/json' },
  onload: (r) => ok(JSON.parse(r.responseText)), onerror: no, ontimeout: no }));
for (;;) {
  const { order } = await call('GET', '/desk/next?book=unibet&driver=userscript&wait=25');
  if (order) { const answer = await placeOnThisSite(order); if (answer) await call('POST', '/desk/result', { id: order.id, ...answer }); }
}
```

## Trying it without anything real: the sandbox

```bash
oddsfeed desk-sandbox 8799
```

The desk alone on `127.0.0.1:8799`: no feed, no bot, no database, no account, nothing sent anywhere. It holds one
pretend game (match 1, Natus Vincere v Team Spirit) at two pretend bookmakers, `alpha` and `beta` (`beta` lists the
teams the other way round), and a pretend Polymarket book. Its `desk.json` and journal go to a new folder in the temp
directory (or the folder given as the third argument), never next to the real database. A real Polymarket leg is
always refused there.

```bash
python tools/desk/driver.py --mock --book alpha --api http://127.0.0.1:8799 --desk-json <folder>/desk.json
```

Write your site driver against the sandbox first, then against the real desk in `dry` mode, then real with a small
`max_stake`.

## Looking at it

- `GET /desk?limit=50&state=exposed` : switches, each book's limits, what it got in 24 hours and when its driver last
  asked for work, ticket counts, the latest tickets.
- `GET /desk/tickets/{id}` : one ticket.
- `desk_tickets.jsonl` next to the database: every state of every ticket, one line each (the last line of a ticket is
  its current state). Real Polymarket orders are also in `bot_orders` under the bot name `desk`.

## What is not there yet

- The Order desk page shows logins, drivers, limits and the latest tickets. Handing in tickets and changing limits or
  the real-money switches is still API only.
- Kalshi legs (its outcome orientation in the feed is not reliable yet).
- Polymarket legs other than the winner market, and resting (maker) orders: a Polymarket leg fills now or not at all.
- The real Polymarket path was not exercised against the live venue when this was written (no real order was placed).
  It calls the same `exec::pm_order` as the snipe bot. Start with a small `max_stake`.
- Nothing here decides WHAT to bet. The arbitrage tracker's rows are not checked against a bet slip; run them in
  `dry` mode before trusting one.
