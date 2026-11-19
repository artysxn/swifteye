//! Live snipe bot (Polymarket), paper or real (bots.rs). The Adaptive page re-plays every move
//! after the fact to choose rules; this module trades those rules as the moves happen.
//!
//! 1. Every bookmaker 1X2 price change arrives on the live stream. A change of 4+ points from that
//!    book's last price (also across a suspension: a goal) starts a candidate move, at most one per
//!    game every 2 minutes (one move shows on both sides of a two-way game).
//! 2. At move + 1 s and + 1.5 s (as the simulator: reaction, confirmation) the move is measured with
//!    only what was recorded by then: the trigger book's move from 10 s before, how many independent
//!    bookmaker platforms have moved 2+ points the same way, how much of the move Polymarket has
//!    made, its spread, and the expected gain if it catches up after spread, slippage and the
//!    sports fee. The current rules (Adaptive's last pick for Polymarket) decide.
//! 3. Buy with a limit at the best price (+2c with book walking): a fill-and-kill order on real
//!    money; on paper the fill is what rests under that limit when a real order would match
//!    (Polymarket reply + live-sports matching delay, measured by the test runs, timing.rs), + 1c;
//!    not taken if the price ran more than 2c before sending. Stake = share of the
//!    bot's own balance (x clip(edge / 4c, 0.5, 3) with edge sizing), capped by the bot's caps,
//!    its cash and the per-game cap shared with the arbitrage bot.
//! 4. Every second: sell once Polymarket has caught up (`catch` of the move), else at the give-up
//!    time; never before the bought shares have settled. Paper sales fill like real ones: a limit
//!    2c under the bid, against the bids standing when it matches.
//! The shadow bot (`snipe-1book`) takes the same moves with 1 bookmaker instead of the picked
//! count, on paper, as the live A/B test of that rule.

use crate::bots::{self, SNIPE, SNIPE_SHADOW};
use crate::model::*;
use crate::snipe;
use crate::venue::{self, Fee};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const REACTION: Ms = 1_000;
const CONFIRM: Ms = 500;
const FILL: Ms = 250;
const SLIP: f64 = 0.01;
const CAP: f64 = 0.02;
const MIN_JUMP: f64 = 0.04;
const SAME_MOVE_MS: Ms = 120_000;
/// Time for the store to have written what the stream already delivered.
const STORE_LAG_MS: Ms = 400;

#[derive(Default)]
struct State {
    /// (match, outcome, book) -> (last real probability, when)
    last: HashMap<(i64, Outcome, &'static str), (f64, Ms)>,
    /// match -> when a candidate last started. Per GAME, not per outcome: in a two-way market one
    /// move shows on both sides (home up = away down) and both would buy the same token; the
    /// simulator also takes one move per game per 2 minutes.
    seen: HashMap<i64, Ms>,
    candidates: u64,
    skipped: BTreeMap<String, u64>,
    entered: BTreeMap<String, u64>,
    recent: Vec<Value>,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);

pub fn status() -> Value {
    let s = ST.lock();
    json!({"candidates": s.candidates, "skipped": s.skipped, "entered": s.entered, "recent": s.recent})
}

/// Where the app's database is (signals log).
static DB: LazyLock<Mutex<Option<std::path::PathBuf>>> = LazyLock::new(Default::default);

fn note(bot: &str, why: &str, detail: Value) {
    // Every decision of the main bot is kept (Bots page → test run chart).
    if bot == SNIPE {
        if let Some(db) = DB.lock().clone() {
            if let Ok(c) = Connection::open(db) {
                let _ = c.busy_timeout(Duration::from_secs(2));
                let _ = c.execute("INSERT INTO bot_signals(ts_ms, match_id, outcome, result, data) VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![now_ms(), detail["match_id"].as_i64(), detail["outcome"].as_str(), why, detail.to_string()]);
            }
        }
    }
    let mut s = ST.lock();
    if why == "entered" {
        *s.entered.entry(bot.into()).or_default() += 1;
    } else {
        *s.skipped.entry(format!("{bot}: {why}")).or_default() += 1;
    }
    s.recent.insert(0, json!({"ts": now_ms(), "bot": bot, "result": why, "detail": detail}));
    s.recent.truncate(40);
}

pub fn spawn(app: Arc<App>) {
    *DB.lock() = Some(app.db_path.clone());
    {
        let a = app.clone();
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(30)).await;
                let a2 = a.clone();
                if let Ok(Some(msg)) = tokio::task::spawn_blocking(move || bots::tick_run(&a2)).await {
                    let _ = crate::telegram::send("bot", &msg).await;
                }
            }
        });
    }
    // Open trades from before a restart keep being watched.
    if let Ok(c) = crate::store::open_ro(&app.db_path) {
        if let Ok(mut st) = c.prepare("SELECT id, data FROM bot_trades WHERE closed_ms IS NULL AND bot IN (?1, ?2)") {
            let rows: Vec<(i64, String)> = st.query_map(params![SNIPE, SNIPE_SHADOW], |r| Ok((r.get(0)?, r.get(1)?))).map(|it| it.filter_map(|x| x.ok()).collect()).unwrap_or_default();
            for (id, d) in rows {
                if let Ok(v) = serde_json::from_str::<Value>(&d) {
                    tokio::spawn(monitor(app.clone(), id, v));
                }
            }
        }
    }
    let mut rx = app.live.subscribe();
    tokio::spawn(async move {
        loop {
            match rx.recv().await {
                Ok(ch) => on_change(&app, &ch),
                Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => tracing::warn!("snipebot: live stream lagged by {n}"),
                Err(_) => break,
            }
        }
    });
}

fn on_change(app: &Arc<App>, ch: &Change) {
    if ch.market != Market::X12 || ch.book == "polymarket" || ch.book == "kalshi" || ch.match_id <= 0 {
        return;
    }
    if !matches!(ch.outcome, Outcome::Home | Outcome::Away | Outcome::Draw) {
        return;
    }
    let Some(new) = ch.new_price.filter(|p| *p > 1.0) else { return };
    let p = 1.0 / new;
    let key = (ch.match_id, ch.outcome, ch.book);
    let jump = {
        let mut s = ST.lock();
        let prev = s.last.insert(key, (p, ch.ts_ms));
        if s.last.len() > 200_000 {
            let cut = ch.ts_ms - 6 * 3_600_000;
            s.last.retain(|_, v| v.1 > cut);
        }
        match prev {
            Some((q, _)) => p - q,
            None => return,
        }
    };
    if jump.abs() < MIN_JUMP {
        return;
    }
    let set = bots::settings();
    let main = bots::mode(SNIPE);
    let shadow = set.snipe.shadow_one_book && set.snipe.c.enabled && !set.kill;
    if main.is_none() && !shadow {
        return;
    }
    {
        let mut s = ST.lock();
        let k = ch.match_id;
        if s.seen.get(&k).is_some_and(|t| ch.ts_ms - *t < SAME_MOVE_MS) {
            return;
        }
        s.seen.insert(k, ch.ts_ms);
        s.seen.retain(|_, t| ch.ts_ms - *t < SAME_MOVE_MS);
        s.candidates += 1;
    }
    let (app, m, o, dir, book, t0) = (app.clone(), ch.match_id, ch.outcome, if jump > 0.0 { 1.0 } else { -1.0 }, ch.book, ch.ts_ms);
    tokio::spawn(async move {
        let wake = t0 + REACTION + CONFIRM + STORE_LAG_MS - now_ms();
        if wake > 0 {
            tokio::time::sleep(Duration::from_millis(wake as u64)).await;
        }
        let a = app.clone();
        let t_meas = now_ms();
        let feat = tokio::task::spawn_blocking(move || measure(&a, m, o, dir, book, t0)).await.ok().flatten();
        let Some(mut feat) = feat else { return };
        feat.measure_ms = now_ms() - t_meas;
        let mut rules = crate::adaptive::active_rules();
        rules.max_followed = rules.max_followed.min(crate::adaptive::SAFE_MAX_FOLLOWED);
        let mut one = rules;
        one.min_books = 1;
        let fill_at = t0 + REACTION + CONFIRM + FILL;
        let wait = fill_at - now_ms();
        if wait > 0 {
            tokio::time::sleep(Duration::from_millis(wait as u64)).await;
        }
        if let Some(real) = main {
            let safe = crate::adaptive::Flt::live();
            match gate(&feat, &rules, &safe) {
                Ok(()) => {
                    // Real money always has a paper twin on the same move, with the paper
                    // wallet's own stakes: the pair shows what paper gets wrong. Started first
                    // and on its own task, so it never delays the real order.
                    if real {
                        let (a, f2) = (app.clone(), feat.clone());
                        tokio::spawn(async move { enter(&a, SNIPE, false, &f2, &rules, false).await });
                    }
                    enter(&app, SNIPE, real, &feat, &rules, false).await
                }
                Err(why) => {
                    // Not a safe-set trade: the wider set (smaller stake, its own daily stop)?
                    let pb = crate::adaptive::policy_b();
                    let set = bots::settings().snipe;
                    if !set.b_enabled {
                        note(SNIPE, why, feat.json());
                    } else if let Err(why_b) = gate(&feat, &pb, &crate::adaptive::Flt::wider()) {
                        note(SNIPE, why, json!({"f": feat.json(), "wider": why_b}));
                    } else {
                        // During a real test run the wider set trades on paper: real money only
                        // where the evidence is strongest.
                        let real_b = real && bots::settings().run.is_none();
                        if set.b_daily_loss > 0.0 && wider_today(&app, real_b) <= -set.b_daily_loss {
                            note(SNIPE, "wider_set_daily_stop", feat.json());
                        } else {
                            enter(&app, SNIPE, real_b, &feat, &pb, true).await;
                        }
                    }
                }
            }
        }
        if shadow {
            match gate(&feat, &one, &crate::adaptive::Flt::live()) {
                Ok(()) => enter(&app, SNIPE_SHADOW, false, &feat, &one, false).await,
                Err(why) => note(SNIPE_SHADOW, why, Value::Null),
            }
        }
    });
}

/// Everything known about a move at the two tests.
#[derive(Clone, Debug)]
struct Feat {
    m: i64,
    o: Outcome,
    dir: f64,
    trigger: &'static str,
    t0: Ms,
    pre_mid: f64,
    bm: [f64; 2],
    books: [usize; 2],
    fol: [f64; 2],
    sp: [f64; 2],
    dec: [f64; 2],
    pause: bool,
    gap: bool,
    /// [Polymarket 10-min trend, trigger book 5-min net move, its 10-min trend, its 10-min range].
    tr: [f64; 4],
    /// The move against the whole market and this game's history.
    cx: crate::consensus::Ctx,
    /// How long measuring took (ms).
    measure_ms: Ms,
    sport: String,
    home: String,
    away: String,
}

impl Feat {
    fn json(&self) -> Value {
        json!({"match_id": self.m, "outcome": self.o.as_str(), "dir": self.dir, "trigger": self.trigger, "books": self.books, "move": self.bm, "followed": self.fol, "spread": self.sp, "tr": self.tr, "dec": self.dec, "cx": self.cx, "sport": self.sport, "measure_ms": self.measure_ms})
    }
    /// Expected gain per contract if Polymarket catches up to `catch` of the move (test i).
    fn edge(&self, i: usize, catch: f64) -> f64 {
        // A share never pays more than $1 or less than 0: cap where the price can go.
        let target = (self.pre_mid + self.dir * self.bm[i] * catch).clamp(0.0, 1.0);
        let exp = if self.dir > 0.0 { target - self.sp[i] / 2.0 - SLIP } else { 1.0 - (target + self.sp[i] / 2.0) - SLIP };
        let d = self.dec[i];
        exp - d - Fee::PM_SPORTS.raw(1.0, d) - Fee::PM_SPORTS.raw(1.0, exp.clamp(0.0, 1.0))
    }
}

/// Independent bookmaker platforms whose price for (m, o) moved 2+ points in `dir` between
/// `from` and `t`.
fn platforms_moved(c: &Connection, m: i64, o: &str, dir: f64, from: Ms, t: Ms) -> usize {
    let books: Vec<String> = c
        .prepare_cached("SELECT DISTINCT book FROM changes WHERE match_id = ?1 AND market = '1x2' AND outcome = ?2 AND ts_ms > ?3 AND ts_ms <= ?4 AND book NOT IN ('polymarket', 'kalshi')")
        .and_then(|mut st| st.query_map(params![m, o, from, t], |r| r.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    let mut p: Vec<&str> = books
        .iter()
        .filter(|b| match (snipe::book_prob(c, b, m, o, from), snipe::book_prob(c, b, m, o, t)) {
            (Some(a), Some(z)) => (z - a) * dir >= MIN_JUMP / 2.0,
            _ => false,
        })
        .map(|b| snipe::platform(b))
        .collect();
    p.sort();
    p.dedup();
    p.len()
}

fn measure(app: &App, m: i64, o: Outcome, dir: f64, trigger: &'static str, t0: Ms) -> Option<Feat> {
    let c = crate::store::open_ro(&app.db_path).ok()?;
    let os = o.as_str();
    let (td, td2) = (t0 + REACTION, t0 + REACTION + CONFIRM);
    let pre = t0 - snipe::PRE_MS;
    let skip = |why: &str| {
        note(SNIPE, why, json!({"match_id": m, "outcome": os, "trigger": trigger}));
        None
    };
    let (Some(b0), Some(b1), Some(b2)) = (snipe::book_prob(&c, trigger, m, os, pre), snipe::book_prob(&c, trigger, m, os, td), snipe::book_prob(&c, trigger, m, os, td2)) else {
        return skip("no_book_price");
    };
    let bm = [(b1 - b0) * dir, (b2 - b0) * dir];
    if bm[0] < 0.01 || bm[1] < 0.01 {
        return skip("book_reverted");
    }
    let mut q = snipe::pm_quotes(&c);
    let (Some(q0), Some(q1), Some(q2)) = (q.at(m, os, pre), q.at(m, os, td), q.at(m, os, td2)) else {
        return skip("no_market_price");
    };
    let pre_mid = (q0.0 + q0.1) / 2.0;
    let fol = |x: &snipe::Q, b: f64| ((x.0 + x.1) / 2.0 - pre_mid) * dir / b;
    let dec = |x: &snipe::Q| if dir > 0.0 { x.1 + SLIP } else { 1.0 - x.0 + SLIP };
    let (a, b, cc, d) = crate::adaptive::trends(&c, q.as_mut(), trigger, m, os, dir, t0, pre_mid, b0, b2);
    let Some(cx) = crate::consensus::Series::load(&c, m, os, t0 - 31 * 60_000, td2).ctx(dir, pre, td2) else {
        return skip("no_consensus");
    };
    let (sport, home, away): (String, String, String) = c.query_row("SELECT sport, home, away FROM matches WHERE id = ?1", [m], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap_or_default();
    Some(Feat {
        m,
        o,
        dir,
        trigger,
        t0,
        pre_mid,
        bm,
        // Independent bookmakers = feed families that moved (as the replay counts them).
        books: [cx.fam_moved, cx.fam_moved],
        fol: [fol(&q1, bm[0]), fol(&q2, bm[1])],
        sp: [q1.1 - q1.0, q2.1 - q2.0],
        dec: [dec(&q1), dec(&q2)],
        pause: crate::adaptive::after_pause(&c, trigger, m, os, t0),
        gap: snipe::reopened(&c, trigger, m, os, pre, td2) || snipe::reopened(&c, "polymarket", m, os, pre, td2),
        tr: [a, b, cc, d],
        cx,
        measure_ms: 0,
        sport,
        home,
        away,
    })
}

/// Today's (UTC) result of the wider set's closed trades.
fn wider_today(app: &App, real: bool) -> f64 {
    let now = now_ms();
    let day = now - now.rem_euclid(86_400_000);
    crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            c.query_row(
                "SELECT COALESCE(SUM(json_extract(data, '$.pnl')), 0) FROM bot_trades WHERE bot = ?1 AND real = ?2 AND closed_ms >= ?3 AND json_extract(data, '$.set') = 'wider'",
                params![SNIPE, real as i64, day],
                |r| r.get::<_, f64>(0),
            )
            .ok()
        })
        .unwrap_or(0.0)
}

fn gate(f: &Feat, r: &crate::adaptive::Policy, flt: &crate::adaptive::Flt) -> Result<(), &'static str> {
    if f.gap {
        return Err("data_gap");
    }
    if f.bm[0] < r.min_move {
        return Err("move_too_small");
    }
    if f.books[0] < r.min_books || f.books[1] < r.min_books {
        return Err("too_few_books");
    }
    if f.fol[0] > r.max_followed || f.fol[1] > r.max_followed {
        return Err("already_moved");
    }
    if f.sp[0] > r.max_spread + 1e-9 || f.sp[1] > r.max_spread + 1e-9 {
        return Err("spread_too_wide");
    }
    if !f.dec.iter().all(|d| (0.02..=0.98).contains(d)) {
        return Err("price_out_of_range");
    }
    // Above 90c (no room to gain), trends against us, bounces and swingy games: see adaptive::Flt.
    if let Some(why) = flt.check(&f.sport, f.trigger, now_ms(), f.dec[0].max(f.dec[1]), f.bm, f.tr, &f.cx) {
        return Err(why);
    }
    if f.edge(0, r.catch) < r.min_edge || f.edge(1, r.catch) < r.min_edge {
        return Err("gap_too_small");
    }
    if r.skip_after_pause && f.pause {
        return Err("after_pause");
    }
    Ok(())
}

/// The token we buy and the price levels we buy from (best first), plus the market's fee.
/// Yes: the outcome's token, its asks. No: the complement token, priced 1 - the Yes bids.
fn our_book(m: i64, o: Outcome, dir: f64) -> Option<(String, Vec<(f64, f64)>, Vec<(f64, f64)>, Fee)> {
    let (tok, l, meta) = venue::pm_contract(m, o)?;
    let fee = meta.fee.unwrap_or(Fee::PM_SPORTS);
    if dir > 0.0 {
        Some((tok, l.asks.clone(), l.bids.clone(), fee))
    } else {
        let comp = meta.complement.clone()?;
        let asks = l.bids.iter().map(|(p, q)| (1.0 - p, *q)).collect();
        let bids = l.asks.iter().map(|(p, q)| (1.0 - p, *q)).collect();
        Some((comp, asks, bids, fee))
    }
}

/// Take `levels` (best first) up to `max_px`, for at most `dollars`: (contracts, average price).
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
    let n = n.floor();
    (n, if n > 0.0 { cost / n.max(1e-9) } else { 0.0 })
}

async fn enter(app: &Arc<App>, bot: &'static str, real: bool, f: &Feat, r: &crate::adaptive::Policy, wider: bool) {
    let s = bots::settings();
    // During a real test run the saved settings are the run's small real stakes; paper keeps the
    // wallet the settings had before it (and get back after it).
    let sn = if real { s.snipe.clone() } else { s.run.as_ref().map(|r| r.prev.clone()).unwrap_or_else(|| s.snipe.clone()) };
    let Some((token, asks, _, fee)) = our_book(f.m, f.o, f.dir) else {
        note(bot, "no_live_book", f.json());
        return;
    };
    let Some(best) = asks.first().map(|l| l.0) else {
        note(bot, "no_offer", f.json());
        return;
    };
    if best + SLIP > f.dec[1] + CAP {
        note(bot, "price_ran", f.json());
        return;
    }
    // Stake: share of this bot's own balance (x edge), within its caps, cash and the game cap.
    let (cash, balance, open, room) = {
        let Ok(c) = crate::store::open_ro(&app.db_path) else { return };
        let (closed, open, _, _, _) = bots::snipe_ledger(&c, bot, real);
        let bal = sn.c.start + closed;
        // Real: this bot's own bankroll (not the whole account); paper: its paper cash.
        (if real { bots::real_free(&c, SNIPE) } else { bal - open }, if real { sn.c.bankroll + closed } else { bal }, open, if bot == SNIPE { bots::snipe_game_room(&c, f.m, real) } else { f64::INFINITY })
    };
    let edge = f.edge(1, r.catch);
    let k = if sn.edge_sizing && !wider { (edge / 0.04).clamp(0.5, 3.0) } else { 1.0 };
    // The safe set: share of the balance x edge, up to the trade cap. The wider set: its own
    // small flat stake.
    // Real money has its own wallet and its own stakes: share of the real balance, its own most
    // per trade, and never more open than the wallet. Paper uses the paper wallet's.
    let (pct, max_trade, max_open) = if real { (sn.r_pct, sn.r_max_trade, sn.c.bankroll) } else { (sn.pct, sn.c.max_trade, sn.c.max_open) };
    let stake = if wider { (sn.b_pct * balance).min(if real { sn.r_max_trade } else { sn.b_max_trade }) } else { (pct * balance * k).min(max_trade) }.min(cash).min(max_open - open).min(room);
    let give_up_s = if !wider && sn.give_up_s > 0 { sn.give_up_s } else { r.give_up_s };
    if stake < snipe::MIN_FILL {
        note(bot, "no_cash", f.json());
        return;
    }
    // Past the best price (up to 2c) only as far as the expected gain still clears the rule's
    // minimum at that price: every level bought must be worth buying on its own.
    let room = edge - (best + SLIP - f.dec[1]) - r.min_edge;
    let max_px = if r.walk_book { best + ((room * 100.0 + 1e-6).floor() / 100.0).clamp(0.0, 0.02) } else { best };
    let (mut n, mut avg) = take(&asks, max_px, stake);
    if n < 1.0 || n * avg < snipe::MIN_FILL {
        note(bot, "too_thin", f.json());
        return;
    }
    if real {
        let pm = crate::pmacct::load(&app.db_path);
        let http = crate::books::http_client();
        // Same account as the arbitrage bot: one order at a time, only with free cash.
        let mut wallet = match crate::exec::claim(venue::V::Pm, &app.db_path, &http, n * max_px * 1.02).await {
            Ok(w) => w,
            Err(e) => {
                note(bot, "no_account_cash", json!({"error": format!("{e:#}")}));
                return;
            }
        };
        let placed = crate::exec::pm_order(&http, &pm, &token, true, max_px, n, false).await;
        if let Ok(x) = &placed {
            wallet.spent += x.filled * x.avg * 1.02;
        }
        drop(wallet);
        match placed {
            Ok(x) if x.filled >= 1.0 => {
                bots::log_order(app, bot, "polymarket", true, &json!({"side": "buy", "token": token, "limit": max_px, "shares": n, "fill": x}));
                // Everything received, to the hundredth of a share (a fill under the limit returns
                // a little more than ordered; the whole of it is sold later).
                n = (x.filled * 100.0 + 1e-6).floor() / 100.0;
                avg = x.avg;
            }
            Ok(x) => {
                bots::log_order(app, bot, "polymarket", false, &json!({"side": "buy", "token": token, "limit": max_px, "shares": n, "fill": x, "error": "nothing filled"}));
                note(bot, "real_not_filled", f.json());
                return;
            }
            Err(e) => {
                bots::log_order(app, bot, "polymarket", false, &json!({"side": "buy", "token": token, "limit": max_px, "shares": n, "error": format!("{e:#}")}));
                note(bot, "real_order_failed", f.json());
                return;
            }
        }
    } else {
        // Paper: as a real order would fill. The order reaches Polymarket after its reply time
        // and, on a game already playing, matches only after the live-sports delay (both measured
        // by the test runs): the fill takes what rests at or under the limit THEN.
        let live = crate::store::open_ro(&app.db_path).map(|c| crate::timing::live(&c, f.m, now_ms())).unwrap_or(true);
        // A real order leaves `pm_send_lag_ms` after the second test (cash claim, signing: this
        // paper path skips them), then the reply and the matching delay.
        let tmg = crate::timing::get();
        let wait = (f.t0 + REACTION + CONFIRM + tmg.pm_send_lag_ms - now_ms()).max(0) + tmg.pm_match_ms(live);
        tokio::time::sleep(Duration::from_millis(wait.max(0) as u64)).await;
        let Some((_, asks2, _, _)) = our_book(f.m, f.o, f.dir) else {
            note(bot, "gone_at_match", f.json());
            return;
        };
        let (n2, avg2) = take(&asks2, max_px, n * max_px + 1.0);
        let n2 = n2.min(n);
        if n2 < 1.0 || n2 * avg2 < snipe::MIN_FILL {
            note(bot, "gone_at_match", json!({"match_id": f.m, "outcome": f.o.as_str(), "waited_ms": wait, "limit": max_px}));
            return;
        }
        // No slippage on top: real buys paid exactly what rested at the match (19 Nov, 5 of 5).
        n = n2;
        avg = avg2;
    }
    let fee_in = venue::pm_fee(fee, n, avg);
    let v = json!({
        "bot": bot, "real": real, "ts": now_ms(), "match_id": f.m, "sport": f.sport, "home": f.home, "away": f.away,
        "outcome": f.o.as_str(), "dir": f.dir, "trigger": f.trigger, "move_ms": f.t0, "token": token,
        "pre_mid": f.pre_mid, "move": f.bm[0], "spread": f.sp[0], "books": f.books, "followed": f.fol, "edge": edge, "edge_x": k,
        "catch": r.catch, "give_up_s": give_up_s, "fee_rate": fee.rate, "set": if wider { "wider" } else { "safe" },
        "contracts": n, "entry": avg, "fee_in": fee_in, "cost": n * avg + fee_in, "stake": stake, "limit": max_px, "best": best,
        "tr": f.tr, "stop_px": crate::adaptive::Flt::live().stop_px(avg, f.bm[0]), "cx": f.cx, "revert": crate::adaptive::Flt::live().revert,
    });
    let id = {
        let Ok(w) = Connection::open(&app.db_path) else { return };
        let _ = w.busy_timeout(Duration::from_secs(5));
        if w.execute("INSERT INTO bot_trades(bot, real, ts_ms, match_id, data) VALUES (?1, ?2, ?3, ?4, ?5)", params![bot, real as i64, now_ms(), f.m, v.to_string()]).is_err() {
            return;
        }
        w.last_insert_rowid()
    };
    note(bot, "entered", json!({"match_id": f.m, "outcome": f.o.as_str(), "entry": avg, "contracts": n, "real": real}));
    tracing::info!("snipebot {bot}: bought {n} {} {} at {avg:.3} ({})", f.home, f.o.as_str(), if real { "REAL" } else { "paper" });
    tokio::spawn(monitor(app.clone(), id, v));
}

/// Watch one open trade every second; sell when caught up or at the give-up time.
async fn monitor(app: Arc<App>, id: i64, v: Value) {
    let bot: String = v["bot"].as_str().unwrap_or(SNIPE).into();
    let real = v["real"].as_bool().unwrap_or(false);
    let m = v["match_id"].as_i64().unwrap_or(0);
    let Some(o) = venue::outcome_of(v["outcome"].as_str().unwrap_or("")) else { return };
    let dir = v["dir"].as_f64().unwrap_or(1.0);
    let pre_mid = v["pre_mid"].as_f64().unwrap_or(0.5);
    let need = v["catch"].as_f64().unwrap_or(1.0) * v["move"].as_f64().unwrap_or(0.0);
    let s1 = v["spread"].as_f64().unwrap_or(0.0);
    let give_end = v["ts"].as_i64().unwrap_or(0) + v["give_up_s"].as_i64().unwrap_or(1800) * 1000;
    let mut left = v["contracts"].as_f64().unwrap_or(0.0);
    let (mut got, mut fees_out) = (0.0, 0.0);
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    loop {
        tick.tick().await;
        let now = now_ms();
        let Some((tok, l, meta)) = venue::pm_contract(m, o) else {
            // No live book (the market closed or the feed lost it): after the give-up time, settle
            // on the result once the recorded prices show one.
            if now > give_end {
                if let Some(won) = settled(&app, m, o) {
                    let pay = if (dir > 0.0) == won { 1.0 } else { 0.0 };
                    close(&app, id, &v, &bot, real, pay * left, 0.0, now, "settled");
                    return;
                }
            }
            continue;
        };
        let (bid, ask) = (l.bids.first().map(|x| x.0), l.asks.first().map(|x| x.0));
        let caught = match (bid, ask) {
            // Also cash in once the bought side can be sold at 99c+: the win is all but certain.
            (Some(b), Some(a)) => snipe::caught_up(&(b, a, now, "book"), dir, pre_mid, need, s1) || (if dir > 0.0 { b } else { 1.0 - a }) >= 0.99,
            _ => false,
        };
        // Stop-loss: the bought side can only be sold well under the buy price -> out now.
        let mut stopped = !caught && v["stop_px"].as_f64().zip(bid.zip(ask)).is_some_and(|(sp, (b, a))| (if dir > 0.0 { b } else { 1.0 - a }) <= sp);
        // "Reason gone": the bookmakers' consensus gave back half its move (checked every 2 s).
        if !caught && !stopped && v["revert"].as_bool() == Some(true) && now % 2000 < 1000 {
            let (cn0, mv) = (v["cx"]["cons_now"].as_f64().unwrap_or(0.0), v["cx"]["cons_move"].as_f64().unwrap_or(0.0));
            if let Ok(c) = crate::store::open_ro(&app.db_path) {
                if let Some(cn) = crate::consensus::Series::load(&c, m, o.as_str(), now - 60_000, now).cons_at(now) {
                    stopped = (cn - cn0) * dir <= -0.5 * mv.max(0.01);
                }
            }
        }
        if !caught && !stopped && now < give_end {
            continue;
        }
        // Bought shares can be sold only once settled (measured ~1.8 s after the buy).
        if now < v["ts"].as_i64().unwrap_or(0) + crate::timing::get().pm_settle_ms {
            continue;
        }
        // Sell: Yes into the Yes bids; No into the complement's bids (1 - Yes asks).
        let (token, bids): (String, Vec<(f64, f64)>) = if dir > 0.0 { (tok, l.bids.clone()) } else { (meta.complement.clone().unwrap_or_default(), l.asks.iter().map(|(p, q)| (1.0 - p, *q)).collect()) };
        let Some(top) = bids.first().map(|x| x.0) else { continue };
        let fee = meta.fee.unwrap_or(Fee::PM_SPORTS);
        if real {
            let pm = crate::pmacct::load(&app.db_path);
            let http = crate::books::http_client();
            let limit = (top - CAP).max(0.01);
            match crate::exec::pm_order(&http, &pm, &token, false, limit, left, false).await {
                Ok(x) if x.filled >= 1.0 => {
                    bots::log_order(&app, &bot, "polymarket", true, &json!({"side": "sell", "token": token, "limit": limit, "shares": left, "fill": x}));
                    got += x.filled * x.avg;
                    fees_out += venue::pm_fee(fee, x.filled, x.avg);
                    left -= x.filled;
                    if left >= 1.0 {
                        continue; // sell the rest next second
                    }
                }
                Ok(x) => {
                    bots::log_order(&app, &bot, "polymarket", false, &json!({"side": "sell", "token": token, "limit": limit, "shares": left, "fill": x, "error": "nothing filled"}));
                    continue;
                }
                Err(e) => {
                    bots::log_order(&app, &bot, "polymarket", false, &json!({"side": "sell", "token": token, "limit": limit, "shares": left, "error": format!("{e:#}")}));
                    // If a fractional size was the problem, the next try sells whole shares.
                    if left.fract() > 0.0 && left >= 1.0 {
                        left = left.floor();
                    }
                    tokio::time::sleep(Duration::from_secs(4)).await;
                    continue;
                }
            }
        } else {
            // Paper: the sale is a limit 2c under the bid now and fills against the bids standing
            // when it matches (after the live-sports delay on a game playing); what does not fit
            // is tried again next second, as the real bot does.
            let limit = (top - CAP).max(0.01);
            let live = crate::store::open_ro(&app.db_path).map(|c| crate::timing::live(&c, m, now)).unwrap_or(true);
            tokio::time::sleep(Duration::from_millis(crate::timing::get().pm_fill_ms(live).max(0) as u64)).await;
            let Some((tok2, l2, meta2)) = venue::pm_contract(m, o) else { continue };
            let bids2: Vec<(f64, f64)> = if dir > 0.0 { l2.bids.clone() } else { l2.asks.iter().map(|(p, q)| (1.0 - p, *q)).collect() };
            let _ = (tok2, meta2);
            let (mut sold, mut proceeds) = (0.0, 0.0);
            for (p, q) in &bids2 {
                if *p < limit - 1e-9 || sold >= left {
                    break;
                }
                let k = q.min(left - sold);
                sold += k;
                proceeds += k * (p - SLIP).max(0.0);
            }
            if sold < 1.0 {
                continue;
            }
            got += proceeds;
            fees_out += venue::pm_fee(fee, sold, proceeds / sold);
            left -= sold;
            if left >= 1.0 {
                continue;
            }
        }
        close(&app, id, &v, &bot, real, got, fees_out, now, if caught { "caught up" } else if stopped { "sold early (stop / reason gone)" } else { "gave up" });
        return;
    }
}

/// Did this outcome win? From the last recorded Polymarket price (97c+ / 3c-), else unknown.
fn settled(app: &App, m: i64, o: Outcome) -> Option<bool> {
    let c = crate::store::open_ro(&app.db_path).ok()?;
    let (b, a): (Option<f64>, Option<f64>) = c
        .query_row("SELECT bid, ask FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND (bid IS NOT NULL OR ask IS NOT NULL) ORDER BY ts_ms DESC LIMIT 1", params![m, o.as_str()], |r| Ok((r.get(0)?, r.get(1)?)))
        .ok()?;
    if b.is_some_and(|b| b >= 0.97) {
        Some(true)
    } else if a.is_some_and(|a| a <= 0.03) {
        Some(false)
    } else {
        None
    }
}

#[allow(clippy::too_many_arguments)]
fn close(app: &App, id: i64, v: &Value, bot: &str, real: bool, got: f64, fees_out: f64, now: Ms, reason: &str) {
    let n = v["contracts"].as_f64().unwrap_or(0.0);
    let exit = if n > 0.0 { got / n } else { 0.0 };
    let pnl = got - fees_out - v["cost"].as_f64().unwrap_or(0.0);
    let mut w2 = v.clone();
    w2["exit"] = json!(exit);
    w2["exit_ts"] = json!(now);
    w2["fee_out"] = json!(fees_out);
    w2["pnl"] = json!(pnl);
    w2["reason"] = json!(reason);
    if let Ok(w) = Connection::open(&app.db_path) {
        let _ = w.busy_timeout(Duration::from_secs(5));
        let _ = w.execute("UPDATE bot_trades SET closed_ms = ?2, data = ?3 WHERE id = ?1", params![id, now, w2.to_string()]);
    }
    tracing::info!("snipebot {bot}: sold {} {} at {exit:.3} ({reason}) {pnl:+.2}", v["home"].as_str().unwrap_or(""), v["outcome"].as_str().unwrap_or(""));
    if real {
        bots::check_daily_loss(app, bot);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn takes_levels_up_to_the_cap_in_whole_contracts() {
        let lv = [(0.50, 30.0), (0.51, 30.0), (0.55, 100.0)];
        let (n, avg) = take(&lv, 0.52, 1_000.0);
        assert_eq!(n, 60.0);
        assert!((avg - 0.505).abs() < 1e-9);
        let (n2, _) = take(&lv, 0.52, 20.0);
        assert_eq!(n2, 39.0); // $15 buys 30 at 50c, $5 buys 9.8 at 51c
    }
}
