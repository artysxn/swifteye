//! Favourites bot (PAPER ONLY): a pre-match bet on a favourite, held to the result.
//!
//! Found 21 Nov 2026 on the games of 17-20 Nov (tools/value_bets.py, tools/value_test.py): a
//! favourite at 55-90c on Polymarket that the bookmakers rate above what the bet costs won more
//! often than its price says: 70 bets, 84% won against 75% needed, +12.9% a dollar, and the more
//! the bookmakers were above the price the better (2+ points above: 94% won). The same reading in
//! play, or on underdogs, loses. It was found after trying several cuts, one half of the games
//! showed no edge, and only 5 bets had been made on games never looked at: this bot is the test
//! on unseen games. No real order can come from it.
//!
//! The bet, checked every few minutes for each game that starts in the next 6.5 hours:
//! * bookmakers: every platform's current odds (the last 9 h of the recording), margin removed by
//!   the power method (which leaves the favourite more of its probability than an even split),
//!   the middle over the independent platforms; 3+ platforms, and none moved in the last 10 min;
//! * Polymarket: the favourite's ask at 55-90c, spread at most 4c;
//! * the bookmakers' middle is 1+ point above the ask plus the fee;
//! * not a CS2 game with a suspicion score at the alert level (suspect.rs): of the 17 such
//!   favourites on 17-21 Nov, 8 won at an average price of 63c (owner's rule, 21 Nov 2026);
//! * one bet a game, 4% of the wallet (owner, 21 Nov 2026: "10% of the wallet per bet is also too
//!   much for gambling. 4%"), and never more than 80% of the wallet tied up in open bets. On paper
//!   the bet is exactly that share, in hundredths of a contract. A REAL order needs 5 contracts
//!   ($2.75 to $4.50 at these prices), which is 4% only of a wallet of $70 to $115: each bet
//!   records whether a real order of its size could have been placed (`real_size_ok`).
//!
//! Settled from Polymarket's last quotes: our side at 99c+ (or 97c+ and no quote for 30 min) is a
//! win paying $1 a contract; another side there is a loss. No fee on the payout.

use crate::bots;
use crate::model::*;
use crate::venue::{self, Fee};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

/// The paper wallet's name in `bot_trades`.
pub const FAVS: &str = "favs";
const TICK: Duration = Duration::from_secs(60);
/// A scan every this many ticks.
const SCAN_TICKS: u64 = 3;
const AHEAD_MS: Ms = 6 * 3_600_000 + 1_800_000;
/// Not in the last minutes before the listed start (the game may already be on).
const NOT_WITHIN_MS: Ms = 3 * 60_000;
const LOOKBACK_MS: Ms = 9 * 3_600_000;
const QUIET_MS: Ms = 10 * 60_000;
const MIN_PX: f64 = 0.55;
const MAX_PX: f64 = 0.90;
const MAX_SPREAD: f64 = 0.04;
/// The bookmakers' middle must be this far above the ask plus the fee.
const MIN_EDGE: f64 = 0.01;
const MIN_GROUPS: usize = 3;
/// Share of the wallet on one bet.
pub const STAKE_SHARE: f64 = 0.04;
/// Open bets may hold at most this share of the wallet.
const MAX_TIED: f64 = 0.80;
/// Polymarket's smallest real order, in contracts.
const MIN_CONTRACTS: f64 = 5.0;
/// The smallest paper bet, in dollars.
const MIN_BET: f64 = 1.0;

#[derive(Default)]
struct Status {
    scans: u64,
    last_scan_ms: Ms,
    games: usize,
    /// Why games were passed over in the last scan.
    skipped: BTreeMap<&'static str, usize>,
    bought: u64,
}

static ST: LazyLock<Mutex<Status>> = LazyLock::new(|| Mutex::new(Status::default()));

/// Odds with the bookmaker's margin removed by the power method: each implied probability raised
/// to the one power that makes them sum to 1. A longshot gives up more of its probability than a
/// favourite, as bookmakers load their margin.
pub(crate) fn fair_power(odds: &[f64]) -> Vec<f64> {
    let pi: Vec<f64> = odds.iter().map(|o| 1.0 / o.max(1.000_1)).collect();
    let s: f64 = pi.iter().sum();
    if s <= 1.0 {
        return pi.iter().map(|p| p / s).collect();
    }
    let (mut lo, mut hi) = (1.0_f64, 6.0_f64);
    for _ in 0..40 {
        let k = (lo + hi) / 2.0;
        if pi.iter().map(|p| p.powf(k)).sum::<f64>() > 1.0 {
            lo = k;
        } else {
            hi = k;
        }
    }
    let k = (lo + hi) / 2.0;
    pi.iter().map(|p| p.powf(k)).collect()
}

/// (book, outcome) -> (odds, suspended) now, and the number of price moves in the last 10 min.
fn book_state(c: &Connection, m: i64, from: Ms, now: Ms) -> (HashMap<(String, String), (f64, bool)>, usize) {
    let mut st: HashMap<(String, String), (f64, bool)> = HashMap::new();
    let mut recent = 0;
    let Ok(mut q) = c.prepare_cached(
        "SELECT ts_ms, book, outcome, kind, new_price FROM changes WHERE match_id = ?1 AND market = '1x2' AND ts_ms >= ?2 AND ts_ms <= ?3
           AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms",
    ) else {
        return (st, 0);
    };
    let Ok(rows) = q.query_map(params![m, from, now], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?, r.get::<_, String>(3)?, r.get::<_, Option<f64>>(4)?))) else {
        return (st, 0);
    };
    for (t, b, o, kind, px) in rows.flatten() {
        let k = (b, o);
        match kind.as_str() {
            "close" => {
                st.remove(&k);
            }
            "suspend" => {
                if let Some(x) = st.get_mut(&k) {
                    x.1 = true;
                }
            }
            "resume" => match px.filter(|p| *p > 0.0) {
                Some(p) => {
                    st.insert(k, (p, false));
                }
                None => {
                    if let Some(x) = st.get_mut(&k) {
                        x.1 = false;
                    }
                }
            },
            _ => {
                if let Some(p) = px.filter(|p| *p > 0.0) {
                    st.insert(k, (p, false));
                }
                if kind == "move" && t > now - QUIET_MS {
                    recent += 1;
                }
            }
        }
    }
    (st, recent)
}

/// The bookmakers' middle fair probability per outcome, and how many independent platforms it is
/// the middle of. None with fewer than `MIN_GROUPS`.
fn middle(st: &HashMap<(String, String), (f64, bool)>) -> Option<(HashMap<String, f64>, usize)> {
    let mut per: HashMap<&str, HashMap<&str, f64>> = HashMap::new();
    for ((b, o), (od, sus)) in st {
        if *od > 1.0 && !*sus {
            per.entry(b.as_str()).or_default().insert(o.as_str(), *od);
        }
    }
    if per.is_empty() {
        return None;
    }
    // A three-way game when most bookmakers quote the draw.
    let three = per.values().filter(|d| d.contains_key("draw")).count() * 2 >= per.len();
    let outs: Vec<&str> = if three { vec!["home", "draw", "away"] } else { vec!["home", "away"] };
    let mut fam: HashMap<&str, Vec<Vec<f64>>> = HashMap::new();
    for (b, d) in &per {
        if d.len() != outs.len() || !outs.iter().all(|o| d.contains_key(o)) {
            continue;
        }
        let odds: Vec<f64> = outs.iter().map(|o| d[o]).collect();
        let over: f64 = odds.iter().map(|x| 1.0 / x).sum();
        if !(1.0..=1.25).contains(&over) {
            continue;
        }
        fam.entry(crate::snipe::platform(b)).or_default().push(fair_power(&odds));
    }
    if fam.len() < MIN_GROUPS {
        return None;
    }
    let mut out = HashMap::new();
    for (i, o) in outs.iter().enumerate() {
        let mut v: Vec<f64> = fam.values().map(|books| books.iter().map(|f| f[i]).sum::<f64>() / books.len() as f64).collect();
        v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = v.len();
        out.insert((*o).to_string(), if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 });
    }
    Some((out, fam.len()))
}

/// How far the bookmakers' middle is above what one contract costs (ask + fee), when the price
/// is a favourite's and the spread is tight. None = not a bet.
pub(crate) fn edge(mid: f64, ask: f64, bid: f64, fee: Fee) -> Option<f64> {
    if !(MIN_PX..MAX_PX).contains(&ask) || ask - bid > MAX_SPREAD + 1e-9 {
        return None;
    }
    let e = mid - ask - venue::pm_fee(fee, 1.0, ask);
    (e >= MIN_EDGE - 1e-9).then_some(e)
}

/// A CS2 game's suspicion score (suspect.rs), when it has one.
fn suspicion(c: &Connection, m: i64) -> Option<f64> {
    c.query_row(
        "SELECT MAX(a.score) FROM book_events e JOIN cs2_activity a ON a.event_id = e.book_event_id WHERE e.book = 'polymarket' AND e.match_id = ?1",
        [m],
        |r| r.get::<_, Option<f64>>(0),
    )
    .ok()
    .flatten()
}

/// (closed result, cost of the open bets, settled bets, won).
fn ledger(c: &Connection) -> (f64, f64, usize, usize) {
    let Ok(mut st) = c.prepare("SELECT closed_ms, json_extract(data, '$.pnl'), json_extract(data, '$.cost') FROM bot_trades WHERE bot = ?1") else { return (0.0, 0.0, 0, 0) };
    let Ok(rows) = st.query_map([FAVS], |r| Ok((r.get::<_, Option<Ms>>(0)?, r.get::<_, Option<f64>>(1)?, r.get::<_, Option<f64>>(2)?))) else { return (0.0, 0.0, 0, 0) };
    let (mut closed, mut open, mut n, mut w) = (0.0, 0.0, 0, 0);
    for (cl, pnl, cost) in rows.flatten() {
        match cl {
            Some(_) => {
                let p = pnl.unwrap_or(0.0);
                closed += p;
                n += 1;
                w += (p > 0.0) as usize;
            }
            None => open += cost.unwrap_or(0.0),
        }
    }
    (closed, open, n, w)
}

/// What the wallet holds: the snipe bot's real wallet amount (so "a tenth of the wallet" means
/// the same wallet the owner talks about) plus this bot's own settled results.
fn wallet(closed: f64) -> f64 {
    bots::settings().snipe.c.bankroll + closed
}

/// Take `levels` (best first) up to `max_px`, for at most `dollars`: (contracts in hundredths, cost).
fn take(levels: &[(f64, f64)], max_px: f64, dollars: f64) -> (f64, f64) {
    let (mut n, mut cost) = (0.0, 0.0);
    for (p, q) in levels {
        if *p > max_px + 1e-9 {
            break;
        }
        let k = q.min((dollars - cost) / p);
        if k <= 0.0 {
            break;
        }
        n += k;
        cost += k * p;
    }
    if n < 0.01 {
        return (0.0, 0.0);
    }
    let whole = (n * 100.0).floor() / 100.0;
    (whole, cost * whole / n)
}

fn skip(why: &'static str) {
    *ST.lock().skipped.entry(why).or_default() += 1;
}

/// One pass over the games that start soon. Blocking (database reads).
fn scan(app: &App) {
    let now = now_ms();
    {
        let mut s = ST.lock();
        s.scans += 1;
        s.last_scan_ms = now;
        s.skipped.clear();
        s.games = 0;
    }
    // Paper fills at the price the bot sees: not while the feed is late or silent.
    if venue::pm_lag_ms() > bots::LATE_FEED_MS || now - venue::alive_ms(venue::V::Pm) > 10_000 {
        skip("feed_late");
        return;
    }
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return };
    let games: Vec<(i64, String, String, String, Ms)> = c
        .prepare("SELECT id, sport, home, away, start_ms FROM matches WHERE start_ms > ?1 AND start_ms <= ?2 ORDER BY start_ms")
        .and_then(|mut st| st.query_map(params![now + NOT_WITHIN_MS, now + AHEAD_MS], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).map(|it| it.flatten().collect()))
        .unwrap_or_default();
    ST.lock().games = games.len();
    let alert = crate::suspect::settings().alert_score;
    for (m, sport, home, away, start) in games {
        // The favourite, if Polymarket prices one at 55-90c with a tight spread.
        let mut fav = None;
        for o in [Outcome::Home, Outcome::Away] {
            let Some((tok, l, meta)) = venue::pm_contract(m, o) else { continue };
            let (Some(&(ask, _)), Some(&(bid, _))) = (l.asks.first(), l.bids.first()) else { continue };
            if (MIN_PX..MAX_PX).contains(&ask) && ask - bid <= MAX_SPREAD + 1e-9 {
                fav = Some((o, tok, l.asks.clone(), ask, bid, meta.fee.unwrap_or(Fee::PM_SPORTS)));
            }
        }
        let Some((o, tok, asks, ask, bid, fee)) = fav else {
            skip("no_favourite_at_55_to_90c");
            continue;
        };
        let had: i64 = c.query_row("SELECT COUNT(*) FROM bot_trades WHERE bot = ?1 AND match_id = ?2", params![FAVS, m], |r| r.get(0)).unwrap_or(1);
        if had > 0 {
            skip("already_bet");
            continue;
        }
        let (st, recent) = book_state(&c, m, start - LOOKBACK_MS, now);
        if recent > 0 {
            skip("bookmakers_moving");
            continue;
        }
        let Some((mid, groups)) = middle(&st) else {
            skip("under_3_bookmaker_groups");
            continue;
        };
        let Some(fair) = mid.get(o.as_str()).copied() else {
            skip("under_3_bookmaker_groups");
            continue;
        };
        let Some(e) = edge(fair, ask, bid, fee) else {
            skip("bookmakers_not_above_the_price");
            continue;
        };
        let score = suspicion(&c, m);
        if score.is_some_and(|s| s >= alert) {
            skip("suspicion_score_high");
            continue;
        }
        let (closed, open, _, _) = ledger(&c);
        let w = wallet(closed);
        let stake = STAKE_SHARE * w;
        if w <= 0.0 || open + stake > MAX_TIED * w {
            skip("wallet_tied_up");
            continue;
        }
        let (n, cost) = take(&asks, ask + 0.01, stake);
        if cost < MIN_BET {
            skip("too_thin");
            continue;
        }
        let avg = cost / n;
        let fee_in = venue::pm_fee(fee, n, avg);
        if fair - avg - fee_in / n < MIN_EDGE - 1e-9 {
            skip("bookmakers_not_above_the_price");
            continue;
        }
        let v = json!({
            "bot": FAVS, "real": false, "ts": now, "match_id": m, "sport": sport, "home": home, "away": away, "outcome": o.as_str(), "dir": 1.0,
            "token": tok, "set": "favs", "contracts": n, "entry": avg, "fee_in": fee_in, "cost": n * avg + fee_in, "stake": stake, "best": ask, "bid": bid,
            "spread": ask - bid, "fair": fair, "groups": groups, "edge": e, "start_ms": start, "score": score, "wallet": w, "real_size_ok": n >= MIN_CONTRACTS,
        });
        let Ok(wr) = Connection::open(&app.db_path) else { continue };
        let _ = wr.busy_timeout(Duration::from_secs(5));
        if wr.execute("INSERT INTO bot_trades(bot, real, ts_ms, match_id, data) VALUES (?1, 0, ?2, ?3, ?4)", params![FAVS, now, m, v.to_string()]).is_ok() {
            ST.lock().bought += 1;
            tracing::info!("favs (paper): {n} of {} in {home} v {away} at {avg:.3}, bookmakers {fair:.3} ({groups} groups), starts in {} min", o.as_str(), (start - now) / 60_000);
        }
    }
}

/// The last recorded quote of one side: (bid, when).
fn last_bid(c: &Connection, m: i64, o: &str) -> Option<(f64, Ms)> {
    c.query_row("SELECT bid, ts_ms FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND bid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", params![m, o], |r| Ok((r.get::<_, f64>(0)?, r.get::<_, Ms>(1)?))).ok()
}

/// A side has won: it trades at 99c+, or at 97c+ and the market has gone quiet.
pub(crate) fn decided(bid: f64, quote_ms: Ms, now: Ms) -> bool {
    bid >= 0.99 - 1e-9 || (bid >= 0.97 - 1e-9 && now - quote_ms >= 30 * 60_000)
}

/// Close the bets whose game has a result. Blocking.
fn settle(app: &App) {
    let now = now_ms();
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return };
    let open: Vec<(i64, i64, String)> = c
        .prepare("SELECT id, match_id, data FROM bot_trades WHERE bot = ?1 AND closed_ms IS NULL")
        .and_then(|mut st| st.query_map([FAVS], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).map(|it| it.flatten().collect()))
        .unwrap_or_default();
    for (id, m, data) in open {
        let Ok(mut v) = serde_json::from_str::<Value>(&data) else { continue };
        let start = v["start_ms"].as_i64().unwrap_or(0);
        if now < start + 10 * 60_000 {
            continue;
        }
        let ours = v["outcome"].as_str().unwrap_or("").to_string();
        let won = last_bid(&c, m, &ours).is_some_and(|(b, t)| decided(b, t, now));
        let lost = ["home", "draw", "away"].iter().filter(|o| **o != ours).any(|o| last_bid(&c, m, o).is_some_and(|(b, t)| decided(b, t, now)));
        let (exit, reason) = match (won, lost) {
            (true, false) => (1.0, "won"),
            (false, true) => (0.0, "lost"),
            // Two days on and no result (called off, never resolved): out at the last price.
            _ if now > start + 48 * 3_600_000 => (last_bid(&c, m, &ours).map(|x| x.0).unwrap_or(0.5), "no result"),
            _ => continue,
        };
        let n = v["contracts"].as_f64().unwrap_or(0.0);
        let cost = v["cost"].as_f64().unwrap_or(0.0);
        v["exit"] = json!(exit);
        v["exit_ts"] = json!(now);
        v["pnl"] = json!(n * exit - cost);
        v["reason"] = json!(reason);
        let Ok(wr) = Connection::open(&app.db_path) else { continue };
        let _ = wr.busy_timeout(Duration::from_secs(5));
        if wr.execute("UPDATE bot_trades SET closed_ms = ?2, data = ?3 WHERE id = ?1", params![id, now, v.to_string()]).is_ok() {
            tracing::info!("favs (paper): {} v {} {reason}, {:+.2}", v["home"].as_str().unwrap_or(""), v["away"].as_str().unwrap_or(""), n * exit - cost);
        }
    }
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        // The feeds need a few minutes after a start before the bookmakers' prices are all in.
        tokio::time::sleep(Duration::from_secs(240)).await;
        let mut tick: u64 = 0;
        loop {
            let a = app.clone();
            let do_scan = tick % SCAN_TICKS == 0;
            let _ = tokio::task::spawn_blocking(move || {
                settle(&a);
                if do_scan {
                    scan(&a);
                }
            })
            .await;
            tick += 1;
            tokio::time::sleep(TICK).await;
        }
    });
}

fn rows(app: &App) -> Vec<(Option<Ms>, Value)> {
    crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT closed_ms, data FROM bot_trades WHERE bot = ?1 ORDER BY ts_ms").ok()?;
            let it = st.query_map([FAVS], |r| Ok((r.get::<_, Option<Ms>>(0)?, r.get::<_, String>(1)?))).ok()?;
            Some(it.flatten().filter_map(|(cl, d)| serde_json::from_str::<Value>(&d).ok().map(|v| (cl, v))).collect())
        })
        .unwrap_or_default()
}

/// For the Bots API (`/bots` -> `favs`).
pub fn panel(app: &App) -> Value {
    let r = rows(app);
    let closed: f64 = r.iter().filter(|x| x.0.is_some()).map(|x| x.1["pnl"].as_f64().unwrap_or(0.0)).sum();
    let open: f64 = r.iter().filter(|x| x.0.is_none()).map(|x| x.1["cost"].as_f64().unwrap_or(0.0)).sum();
    let n = r.iter().filter(|x| x.0.is_some()).count();
    let wins = r.iter().filter(|x| x.0.is_some() && x.1["pnl"].as_f64().unwrap_or(0.0) > 0.0).count();
    let s = ST.lock();
    json!({
        "paper_only": true, "stake_share": STAKE_SHARE, "start": bots::settings().snipe.c.bankroll, "wallet": wallet(closed), "closed_pnl": closed, "open_cost": open,
        "settled": n, "won": wins, "open": r.iter().filter(|x| x.0.is_none()).map(|x| x.1.clone()).collect::<Vec<_>>(),
        "trades": r.iter().rev().take(100).map(|(cl, v)| { let mut v = v.clone(); v["closed_ms"] = json!(cl); v }).collect::<Vec<_>>(),
        "status": {"scans": s.scans, "last_scan_ms": s.last_scan_ms, "games": s.games, "skipped": s.skipped, "bought": s.bought},
    })
}

fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// /favs in Telegram.
pub fn text(app: &App) -> String {
    let r = rows(app);
    let now = now_ms();
    let closed: Vec<&(Option<Ms>, Value)> = r.iter().filter(|x| x.0.is_some()).collect();
    let open: Vec<&(Option<Ms>, Value)> = r.iter().filter(|x| x.0.is_none()).collect();
    let pnl: f64 = closed.iter().map(|x| x.1["pnl"].as_f64().unwrap_or(0.0)).sum();
    let wins = closed.iter().filter(|x| x.1["pnl"].as_f64().unwrap_or(0.0) > 0.0).count();
    let staked: f64 = closed.iter().map(|x| x.1["cost"].as_f64().unwrap_or(0.0)).sum();
    let tied: f64 = open.iter().map(|x| x.1["cost"].as_f64().unwrap_or(0.0)).sum();
    let mut t = format!(
        "⭐ <b>Favourites bot · paper only</b>\nBefore a game starts it buys a favourite at 55-90¢ that the bookmakers rate 1+ point above Polymarket's price, and holds it to the result. It skips CS2 games with a high suspicion score. One bet a game, {:.0}% of the wallet.\n\nWallet <b>${:.2}</b> (started at ${:.2})",
        STAKE_SHARE * 100.0,
        wallet(pnl),
        bots::settings().snipe.c.bankroll
    );
    if closed.is_empty() {
        t.push_str("\nNo bet settled yet.");
    } else {
        t.push_str(&format!(
            "\n{} bets settled, {wins} won · <b>{}${:.2}</b>{}",
            closed.len(),
            if pnl < 0.0 { "-" } else { "+" },
            pnl.abs(),
            if staked > 0.0 { format!(" ({:+.1}% of the money staked)", 100.0 * pnl / staked) } else { String::new() }
        ));
    }
    if !open.is_empty() {
        t.push_str(&format!("\n\n<b>Open · {} bets, ${tied:.2} tied up</b>", open.len()));
        for (_, v) in open.iter().rev().take(10) {
            let start = v["start_ms"].as_i64().unwrap_or(0);
            let side = if v["outcome"].as_str() == Some("home") { v["home"].as_str() } else { v["away"].as_str() }.unwrap_or("");
            let when = if start > now { format!("starts in {} min", (start - now) / 60_000) } else { "playing".to_string() };
            t.push_str(&format!("\n• {} at {:.0}¢ (bookmakers {:.0}%) · {when}", esc(side), v["entry"].as_f64().unwrap_or(0.0) * 100.0, v["fair"].as_f64().unwrap_or(0.0) * 100.0));
        }
    }
    if !closed.is_empty() {
        t.push_str("\n\n<b>Latest</b>");
        for (_, v) in closed.iter().rev().take(6) {
            let side = if v["outcome"].as_str() == Some("home") { v["home"].as_str() } else { v["away"].as_str() }.unwrap_or("");
            let p = v["pnl"].as_f64().unwrap_or(0.0);
            t.push_str(&format!("\n• {} at {:.0}¢ · {} · {}${:.2}", esc(side), v["entry"].as_f64().unwrap_or(0.0) * 100.0, v["reason"].as_str().unwrap_or(""), if p < 0.0 { "-" } else { "+" }, p.abs()));
        }
    }
    let s = ST.lock();
    if s.scans > 0 {
        let why: Vec<String> = s.skipped.iter().map(|(k, n)| format!("{} {n}", k.replace('_', " "))).collect();
        t.push_str(&format!("\n\n<i>Last look {} min ago: {} games starting within 6.5 h{}</i>", (now - s.last_scan_ms) / 60_000, s.games, if why.is_empty() { String::new() } else { format!(" ({})", why.join(", ")) }));
    }
    t
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_power_method_leaves_the_favourite_more_than_an_even_split() {
        // 1.30 / 3.60: implied 76.9% + 27.8% = 104.7%.
        let f = fair_power(&[1.30, 3.60]);
        assert!((f[0] + f[1] - 1.0).abs() < 1e-6);
        let even = (1.0 / 1.30) / (1.0 / 1.30 + 1.0 / 3.60);
        assert!(f[0] > even, "{} v {even}", f[0]);
        // No margin: nothing to remove.
        let g = fair_power(&[2.0, 2.0]);
        assert!((g[0] - 0.5).abs() < 1e-9);
    }

    #[test]
    fn a_bet_needs_a_favourites_price_a_tight_spread_and_the_bookmakers_above_the_cost() {
        let fee = Fee::PM_SPORTS;
        // 75c ask: the fee is 5% x 0.75 x 0.25 = 0.94c, so the bet costs 75.94c.
        assert!(edge(0.78, 0.75, 0.74, fee).is_some());
        assert!(edge(0.765, 0.75, 0.74, fee).is_none()); // under a point above the cost
        assert!(edge(0.60, 0.50, 0.49, fee).is_none()); // not a favourite's price
        assert!(edge(0.99, 0.92, 0.91, fee).is_none()); // too dear: little left to win
        assert!(edge(0.80, 0.75, 0.70, fee).is_none()); // spread 5c
    }

    #[test]
    fn the_middle_needs_three_independent_platforms() {
        let mut st: HashMap<(String, String), (f64, bool)> = HashMap::new();
        let mut put = |b: &str, h: f64, a: f64| {
            st.insert((b.to_string(), "home".to_string()), (h, false));
            st.insert((b.to_string(), "away".to_string()), (a, false));
        };
        put("pinnacle", 1.30, 3.70);
        put("marathon", 1.28, 3.60);
        assert!(middle(&st).is_none());
        // Two clones of one platform are one voice.
        let mut st2 = st.clone();
        for b in ["22bet", "melbet"] {
            st2.insert((b.to_string(), "home".to_string()), (1.25, false));
            st2.insert((b.to_string(), "away".to_string()), (3.80, false));
        }
        let (mid, groups) = middle(&st2).unwrap();
        assert_eq!(groups, 3);
        assert!(mid["home"] > 0.74 && mid["home"] < 0.80, "{}", mid["home"]);
        assert!((mid["home"] + mid["away"] - 1.0).abs() < 0.02);
        // A suspended price does not count.
        st2.insert(("pinnacle".to_string(), "home".to_string()), (1.30, true));
        assert!(middle(&st2).is_none());
    }

    #[test]
    fn a_game_is_decided_at_99c_or_at_97c_once_the_market_is_quiet() {
        let now = 10_000_000_000;
        assert!(decided(0.99, now, now));
        assert!(!decided(0.97, now - 60_000, now)); // still trading: a favourite at 97c can lose
        assert!(decided(0.97, now - 31 * 60_000, now));
        assert!(!decided(0.90, now - 3_600_000, now));
    }
}
