//! Snipe paper bot with walk-forward ("adaptive") rules.
//!
//! The Snipe simulator buys a prediction market when a bookmaker moves and the market has not
//! followed yet. Which moves to take and when to sell are decision rules; this module re-chooses
//! them from the data every 4 hours (00, 04, 08, 12, 16 and 20 UTC), separately for Polymarket and
//! Kalshi, and paper-trades the current choice.
//!
//! * Facts are fixed, not tuned: reaction 1 s, confirming check 0.5 s later, order travel 0.25 s,
//!   1c slippage on every buy and sale, Kalshi taker fee, Polymarket fee 0, the real order book
//!   (fills capped to what rested when depth was recorded; before that, a full fill).
//! * Money: a paper balance (settings: starting balance, share staked per trade). Each trade
//!   stakes that share of the balance (start plus closed P&L) when it is placed, at most the cash
//!   not tied up in open trades. Each market (Polymarket, Kalshi) has its own balance.
//! * Rules are tuned over a grid (20,736 per market): independent bookmakers that moved, how far
//!   the market may already have followed, minimum expected gain, maximum spread, minimum
//!   bookmaker move, the catch-up share to sell at, the give-up time, whether to skip moves right
//!   after a market pause (the bookmaker suspended, then reopened at the new price: goals), and
//!   the order size: buy only what rests at the best price, or also what rests up to 2c worse.
//! * Score: return per trade on the stake the bot would place now (so a rule that fills less of
//!   the order scores less), minus two standard errors (a conservative estimate), only for rules
//!   with 30+ finished trades. Ranked by the average over each rule and its one-step neighbours
//!   (a plateau, not a lucky spike). A candidate must then also pass validation: on the most
//!   recent 30% of the data before the pick it needs 5+ trades, a positive average, and at least
//!   the current rules' average there. It replaces the current rules only when its plateau score
//!   is 0.3 points per trade better, and never for rules whose conservative estimate (or plateau)
//!   is a loss: with nothing convincingly profitable it keeps the current rules.
//!   (Version 1 used one standard error, 20 trades and no validation: its first real pick chose
//!   rules that had looked good on 22 trades and lost $1,000 on Polymarket in the next 4 hours.
//!   Version 2 still switched to a negative-scoring candidate when the current rules had too few
//!   trades to qualify. Version 3 staked a fixed $100.)
//! * Versioned: runs store the method version and the balance settings; a change of either
//!   recomputes the whole walk-forward history from the first data, so records stay comparable.
//!   Rules are evaluated by their values (thresholds exactly; the sale at the nearest measured
//!   catch-up share and give-up time), so old picks still evaluate if the grid changes.
//! * Honest record: each pick trades the next 4 hours it was not chosen on (walk-forward); those
//!   results are compared with the fixed default rules on the same hours (own balance). If the
//!   last 24 hours of that record lost money over 8+ trades, the bot falls back to the defaults
//!   until the next pick.
//!
//! The candidate table: every bookmaker move (first bookmaker of each move in rank.rs), measured
//! at both tests with everything known at that moment, plus the sale for each catch-up share and
//! give-up time (price and the book on our side). Built in 4-hour chunks (a finished chunk once;
//! the current one every 5 minutes), so a re-choice is a filter over the table, not a new
//! simulation. A trade's dollars are priced from it at any stake.

use crate::model::*;
use crate::rank::{RankEvent, KALSHI};
use crate::snipe::{self, Quotes, Q};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const PM: &str = "polymarket";
const BASES: [&str; 2] = [PM, KALSHI];

// ---- facts (measured, never tuned)
const REACTION: Ms = 1_000;
const CONFIRM: Ms = 500;
const FILL: Ms = 250;
const SLIP: f64 = 0.01;
/// Stake used only to estimate the fee per contract in the expected-gain test.
const REF_STAKE: f64 = 100.0;
const CAP: f64 = 0.02;
const PM_FEE: f64 = 0.0;

// ---- schedule
pub const EVERY_MS: Ms = 4 * 3_600_000;
const FIRST_AFTER_MS: Ms = 8 * 3_600_000;
const EXIT_HORIZON_MS: Ms = 35 * 60_000;
const REBUILD_MS: Ms = 5 * 60_000;
const PAUSE_WINDOW_MS: Ms = 120_000;

// ---- choosing
/// Method version: stored with every run; a change recomputes the history.
const VERSION: u32 = 5;
const MIN_N: usize = 30;
/// Standard errors subtracted from the average return.
const SE_K: f64 = 2.0;
/// Share of the time before a pick held back to validate the candidate.
const VALIDATE_SHARE: f64 = 0.30;
const VALIDATE_MIN_N: usize = 5;
/// How many of the best-ranked candidates are tried against the validation slice.
const VALIDATE_TOP: usize = 60;
const SWITCH_MARGIN: f64 = 0.003;
const INELIGIBLE: f64 = -0.05;
const FALLBACK_SPAN_MS: Ms = 24 * 3_600_000;
const FALLBACK_MIN_TRADES: usize = 8;

// ---- the grid (each policy is one index per dimension)
const BOOKS: [usize; 3] = [1, 2, 3];
const FOLLOWED: [f64; 4] = [0.05, 0.10, 0.25, 0.50];
const EDGE: [f64; 4] = [0.0, 0.01, 0.02, 0.04];
const SPREAD: [f64; 3] = [0.02, 0.04, 0.06];
const MOVE: [f64; 3] = [0.04, 0.07, 0.10];
const CATCH: [f64; 3] = [0.50, 0.75, 1.00];
const GIVE: [i64; 5] = [60, 120, 300, 900, 1800];
const PAUSE: [bool; 2] = [false, true];
/// Order size: false = only what rests at the best price; true = also what rests up to 2c worse.
const WALK: [bool; 2] = [false, true];
const ND: usize = 9;
const DIMS: [usize; ND] = [3, 4, 4, 3, 3, 3, 5, 2, 2];
type Idx = [usize; ND];

fn yes() -> bool {
    true
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
pub struct Policy {
    pub min_books: usize,
    pub max_followed: f64,
    pub min_edge: f64,
    pub max_spread: f64,
    pub min_move: f64,
    pub catch: f64,
    pub give_up_s: i64,
    pub skip_after_pause: bool,
    /// Buy past the best price (up to 2c worse) to fill more of the stake.
    #[serde(default = "yes")]
    pub walk_book: bool,
}

fn policy(i: &Idx) -> Policy {
    Policy {
        min_books: BOOKS[i[0]],
        max_followed: FOLLOWED[i[1]],
        min_edge: EDGE[i[2]],
        max_spread: SPREAD[i[3]],
        min_move: MOVE[i[4]],
        catch: CATCH[i[5]],
        give_up_s: GIVE[i[6]],
        skip_after_pause: PAUSE[i[7]],
        walk_book: WALK[i[8]],
    }
}

fn idx_of(p: &Policy) -> Idx {
    let f = |v: f64, g: &[f64]| g.iter().position(|x| (x - v).abs() < 1e-9).unwrap_or(0);
    [
        BOOKS.iter().position(|x| *x == p.min_books).unwrap_or(1),
        f(p.max_followed, &FOLLOWED),
        f(p.min_edge, &EDGE),
        f(p.max_spread, &SPREAD),
        f(p.min_move, &MOVE),
        f(p.catch, &CATCH),
        GIVE.iter().position(|x| *x == p.give_up_s).unwrap_or(2),
        PAUSE.iter().position(|x| *x == p.skip_after_pause).unwrap_or(0),
        WALK.iter().position(|x| *x == p.walk_book).unwrap_or(1),
    ]
}

/// The fixed default rules (the Snipe window's gates).
fn default_idx(base: &str) -> Idx {
    // Chosen 18 Nov from ~19 h of merged PC + Mac data (stricter, patient): Polymarket 2+ books,
    // <=50% followed, >=4c gain, <=2c spread, >=7 pt move, sell fully caught up, give up 15 min;
    // Kalshi 1+ book, <=25% followed, >=4c gain, <=2c spread, sell at 50%, 15 min, skip pauses.
    if base == PM { [1, 3, 3, 0, 1, 2, 3, 0, 1] } else { [0, 2, 3, 0, 0, 0, 3, 1, 1] }
}

fn flat(i: &Idx) -> usize {
    i.iter().zip(DIMS.iter()).fold(0, |a, (v, d)| a * d + v)
}

fn unflat(mut f: usize) -> Idx {
    let mut i = [0; ND];
    for d in (0..ND).rev() {
        i[d] = f % DIMS[d];
        f /= DIMS[d];
    }
    i
}

const GRID: usize = 3 * 4 * 4 * 3 * 3 * 3 * 5 * 2 * 2;

// ---------------------------------------------------------------- money

/// Paper balance settings (`<db dir>/paperbot.json`).
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
pub struct Bank {
    /// Starting balance per market, dollars.
    pub start: f64,
    /// Share of the balance staked on each trade (0.01 = 1%).
    pub pct: f64,
}

impl Default for Bank {
    fn default() -> Self {
        Bank { start: 10_000.0, pct: 0.01 }
    }
}

/// The current paper balance settings.
pub fn bank() -> Bank {
    ST.lock().bank
}

fn bank_path(app: &App) -> PathBuf {
    app.db_path.parent().map(|d| d.join("paperbot.json")).unwrap_or_else(|| PathBuf::from("paperbot.json"))
}

fn load_bank(app: &App) -> Bank {
    std::fs::read_to_string(bank_path(app)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

/// Change the balance settings. A change recomputes the bot's history with the new money.
pub fn save_settings(app: &App, edit: &Value) -> anyhow::Result<Value> {
    let mut b = ST.lock().bank;
    if let Some(x) = edit["start"].as_f64() {
        anyhow::ensure!((100.0..=1e9).contains(&x), "the starting balance must be between $100 and $1,000,000,000");
        b.start = x;
    }
    if let Some(x) = edit["pct"].as_f64() {
        anyhow::ensure!(x > 0.0 && x <= 1.0, "the share per trade must be above 0% and at most 100%");
        b.pct = x;
    }
    std::fs::write(bank_path(app), serde_json::to_string_pretty(&b)?)?;
    let changed = {
        let mut st = ST.lock();
        let ch = st.bank != b;
        st.bank = b;
        if ch {
            st.runs.clear();
            st.status = "re-running the history with the new balance settings".into();
        }
        ch
    };
    if changed {
        if let Ok(c) = conn(app) {
            let _ = c.execute("DELETE FROM adaptive_runs", []);
        }
        KICK.notify_one();
    }
    Ok(json!({"bank": b, "recomputing": changed}))
}

// ---------------------------------------------------------------- candidates

#[derive(Clone, Copy, Debug)]
struct Exit {
    ts: Ms,
    /// Sale price per contract before the book (slippage included).
    px: f64,
    /// (contracts at the best price, contracts within 2c) on the side we sell into.
    sell: Option<(f64, f64)>,
    caught: bool,
}

#[derive(Clone, Debug)]
struct Cand {
    match_id: i64,
    sport: String,
    home: String,
    away: String,
    outcome: String,
    dir: i8,
    trigger: String,
    pm: bool,
    /// Fill time.
    tf: Ms,
    books: [usize; 2],
    bm: [f64; 2],
    fol: [f64; 2],
    sp: [f64; 2],
    /// Expected gain per contract at each test, for each catch-up share.
    edge: [[f64; 3]; 2],
    /// Price per contract before the book (slippage included).
    px: f64,
    /// (contracts at the best price, contracts within 2c) on our side; None = depth not recorded.
    buy: Option<(f64, f64)>,
    pause: bool,
    /// [catch][give-up]; None = not known yet.
    exits: [[Option<Exit>; 5]; 3],
}

fn fee(pm: bool, n: f64, p: f64) -> f64 {
    if pm { PM_FEE * n * p } else { snipe::kalshi_fee(n, p) }
}

#[derive(Clone, Copy, Debug)]
struct Fill {
    pnl: f64,
    spent: f64,
    entry: f64,
    exit: f64,
    filled: Option<f64>,
}

/// One trade at `stake` dollars. The order takes what rests at the best price, plus (walking the
/// book) what rests up to 2c worse; the sale walks the book on the way out. None when under $10
/// would fill.
fn fill(c: &Cand, x: &Exit, stake: f64, walk: bool) -> Option<Fill> {
    let (n, entry) = match c.buy {
        Some((best, near)) => snipe::fill_buy(stake, c.px, best, if walk { near } else { best }),
        None => (stake / c.px, c.px),
    };
    if n <= 0.0 || (c.buy.is_some() && n * entry < snipe::MIN_FILL) {
        return None;
    }
    let exit = match x.sell {
        Some((best, near)) => snipe::fill_sell(n, x.px, best, near),
        None => x.px,
    };
    let pnl = n * (exit - entry) - fee(c.pm, n, entry) - fee(c.pm, n, exit.max(0.0));
    Some(Fill { pnl, spent: n * entry, entry, exit, filled: c.buy.map(|_| (n * entry / stake).min(1.0)) })
}

/// The trigger bookmaker suspended this price within 2 minutes before the move (a goal,
/// a red card, a break of serve: the book paused, then reopened somewhere else).
fn after_pause(c: &Connection, book: &str, match_id: i64, outcome: &str, t0: Ms) -> bool {
    c.prepare_cached(
        "SELECT 1 FROM changes WHERE match_id = ?1 AND book = ?2 AND market = '1x2' AND outcome = ?3 AND kind = 'suspend' AND ts_ms BETWEEN ?4 AND ?5 LIMIT 1",
    )
    .and_then(|mut st| st.query_row(params![match_id, book, outcome, t0 - PAUSE_WINDOW_MS, t0], |_| Ok(())).optional())
    .ok()
    .flatten()
    .is_some()
}

/// One move measured exactly as the simulator does, with only what is known at each moment.
/// None when no rule in the grid could take it (or prices are missing).
fn make_cand(c: &Connection, q: &mut dyn Quotes, e: &RankEvent, tb: &str, t0: Ms, base: &str, now: Ms, tm: (Ms, Ms)) -> Option<Cand> {
    let pm = base == PM;
    let (mid, o) = (e.match_id, e.outcome.as_str());
    let dir = e.direction as f64;
    let (td, td2) = (t0 + tm.0, t0 + tm.0 + tm.1);
    let tf = td2 + FILL;
    let b_pre = snipe::book_prob(c, tb, mid, o, t0 - snipe::PRE_MS)?;
    let (b1, b2) = (snipe::book_prob(c, tb, mid, o, td)?, snipe::book_prob(c, tb, mid, o, td2)?);
    let (q0, q1, q2, qf) = (q.at(mid, o, t0 - snipe::PRE_MS)?, q.at(mid, o, td)?, q.at(mid, o, td2)?, q.at(mid, o, tf)?);
    if snipe::reopened(c, tb, mid, o, t0 - snipe::PRE_MS, tf) || snipe::reopened(c, base, mid, o, t0 - snipe::PRE_MS, tf) {
        return None;
    }
    if !pm && (q1.3 == "assumed" || q2.3 == "assumed") {
        return None; // Kalshi price unknown at that moment
    }
    let pre_mid = (q0.0 + q0.1) / 2.0;
    let (bm1, bm2) = ((b1 - b_pre) * dir, (b2 - b_pre) * dir);
    if bm1 < MOVE[0] || bm2 < 0.01 {
        return None;
    }
    let fol = |x: &Q, bm: f64| ((x.0 + x.1) / 2.0 - pre_mid) * dir / bm;
    let (f1, f2) = (fol(&q1, bm1), fol(&q2, bm2));
    let (s1, s2) = (q1.1 - q1.0, q2.1 - q2.0);
    let loose_f = *FOLLOWED.last().unwrap();
    let loose_s = *SPREAD.last().unwrap() + 1e-9;
    if f1 > loose_f || f2 > loose_f || s1 > loose_s || s2 > loose_s {
        return None;
    }
    let dec = |x: &Q| if dir > 0.0 { x.1 + SLIP } else { 1.0 - x.0 + SLIP };
    let (d1, d2) = (dec(&q1), dec(&q2));
    if !(0.02..=0.98).contains(&d1) || !(0.02..=0.98).contains(&d2) {
        return None;
    }
    let edge = |bm: f64, sp: f64, d: f64, catch: f64| {
        let target = pre_mid + dir * bm * catch;
        let exp = if dir > 0.0 { target - sp / 2.0 - SLIP } else { 1.0 - (target + sp / 2.0) - SLIP };
        let n = REF_STAKE / d;
        (n * (exp - d) - fee(pm, n, d) - fee(pm, n, exp.clamp(0.0, 1.0))) / n
    };
    let e1 = CATCH.map(|k| edge(bm1, s1, d1, k));
    let e2 = CATCH.map(|k| edge(bm2, s2, d2, k));
    if !(0..3).any(|k| e1[k] >= EDGE[0] && e2[k] >= EDGE[0]) {
        return None;
    }
    let entry0 = dec(&qf);
    if entry0 > d2 + CAP {
        return None; // price ran before the fill
    }
    // What rested on our side when the order landed: buying Yes takes asks, buying No takes Yes
    // bids. Unknown for hours recorded before depth was kept.
    let side = q.depth(mid, o, tf).and_then(|d| if dir > 0.0 { d.ask_size.zip(d.ask_depth) } else { d.bid_size.zip(d.bid_depth) });
    if side.is_some_and(|(_, near)| near * entry0 < snipe::MIN_FILL) {
        return None; // not even $10 within 2c: no order size fills
    }
    // Every sale: catch-up judged on the price we would sell at; else the give-up time.
    let path = q.path(mid, o, tf, tf + GIVE[GIVE.len() - 1] * 1000);
    let mut exits = [[None; 5]; 3];
    for (ci, &k) in CATCH.iter().enumerate() {
        for (gi, &g) in GIVE.iter().enumerate() {
            let give = tf + g * 1000;
            let hit = path.iter().take_while(|x| x.2 <= give).find(|x| snipe::caught_up(x, dir, pre_mid, k * bm1, s1)).map(|x| x.2);
            let (t_sell, caught) = match hit {
                Some(t) => (t, true),
                None if give + FILL > now => continue, // still open
                None => (give, false),
            };
            if t_sell + FILL > now {
                continue;
            }
            let Some(qs) = q.at(mid, o, t_sell + FILL) else { continue };
            let px = (if dir > 0.0 { qs.0 } else { 1.0 - qs.1 }) - SLIP;
            // Selling Yes hits bids; selling No hits Yes asks.
            let sell = if side.is_some() {
                q.depth(mid, o, t_sell + FILL).and_then(|d| if dir > 0.0 { d.bid_size.zip(d.bid_depth) } else { d.ask_size.zip(d.ask_depth) })
            } else {
                None
            };
            exits[ci][gi] = Some(Exit { ts: t_sell + FILL, px, sell, caught });
        }
    }
    Some(Cand {
        match_id: mid,
        sport: e.sport.clone(),
        home: e.home.clone(),
        away: e.away.clone(),
        outcome: e.outcome.clone(),
        dir: e.direction,
        trigger: tb.to_string(),
        pm,
        tf,
        books: [snipe::confirmations(e, td), snipe::confirmations(e, td2)],
        bm: [bm1, bm2],
        fol: [f1, f2],
        sp: [s1, s2],
        edge: [e1, e2],
        px: entry0,
        buy: side,
        pause: after_pause(c, tb, mid, o, t0),
        exits,
    })
}

/// Every move in [start, end) for both markets.
fn build_chunk(db: &std::path::Path, start: Ms, end: Ms, now: Ms) -> anyhow::Result<[Vec<Cand>; 2]> {
    build_chunk_with(db, start, end, now, (REACTION, CONFIRM))
}

fn build_chunk_with(db: &std::path::Path, start: Ms, end: Ms, now: Ms, tm: (Ms, Ms)) -> anyhow::Result<[Vec<Cand>; 2]> {
    let p = crate::lag::Params {
        since: start,
        until: end,
        min_jump: 0.04,
        window_ms: 90_000,
        settle_ms: 10_000,
        source_clock: false,
        match_id: None,
        book: None,
        fastest: None,
        sport: None,
    };
    let r = crate::rank::compute(db, &p)?;
    let c = crate::store::open_ro(db)?;
    let mut out: [Vec<Cand>; 2] = [Vec::new(), Vec::new()];
    for (bi, base) in BASES.iter().enumerate() {
        let mut q = if bi == 0 { snipe::pm_quotes(&c) } else { snipe::kalshi_quotes(&c) };
        for e in &r.events {
            if !(e.order.iter().any(|x| x.0 == *base) || e.missed.iter().any(|m| m == base)) {
                continue;
            }
            let (Some(tb), Some(t0)) = (e.first_book.clone(), e.first_book_ms) else { continue };
            if let Some(cd) = make_cand(&c, &mut *q, e, &tb, t0, base, now, tm) {
                out[bi].push(cd);
            }
        }
        out[bi].sort_by_key(|x| x.tf);
    }
    Ok(out)
}

// ---------------------------------------------------------------- evaluating rules

#[derive(Default, Clone, Copy, Debug, Serialize)]
struct Stats {
    n: usize,
    wins: usize,
    pnl: f64,
    spent: f64,
    sum_r: f64,
    sum_r2: f64,
}

impl Stats {
    /// One trade: return measured on the stake the bot meant to place, so a partly filled
    /// order counts for less.
    fn add(&mut self, pnl: f64, spent: f64, stake: f64) {
        let r = pnl / stake.max(1e-9);
        self.n += 1;
        self.wins += (pnl > 0.0) as usize;
        self.pnl += pnl;
        self.spent += spent;
        self.sum_r += r;
        self.sum_r2 += r * r;
    }
    fn mean(&self) -> f64 {
        if self.n == 0 { 0.0 } else { self.sum_r / self.n as f64 }
    }
    /// Average return minus SE_K standard errors.
    fn lower(&self) -> f64 {
        if self.n < 2 {
            return INELIGIBLE;
        }
        let m = self.mean();
        let var = ((self.sum_r2 - self.n as f64 * m * m) / (self.n as f64 - 1.0)).max(0.0);
        m - SE_K * (var / self.n as f64).sqrt()
    }
    fn json(&self) -> Value {
        json!({"trades": self.n, "wins": self.wins, "pnl": self.pnl, "staked": self.spent,
            "roi": if self.spent > 0.0 { Some(self.pnl / self.spent) } else { None },
            "mean_return": if self.n > 0 { Some(self.mean()) } else { None },
            "conservative": if self.n >= 2 { Some(self.lower()) } else { None }})
    }
}

/// The sale a rule uses: the nearest measured catch-up share and give-up time.
fn sale_idx(p: &Policy) -> (usize, usize) {
    let nearest = |v: f64, g: &[f64]| (0..g.len()).min_by(|a, b| (g[*a] - v).abs().partial_cmp(&(g[*b] - v).abs()).unwrap_or(std::cmp::Ordering::Equal)).unwrap_or(0);
    (nearest(p.catch, &CATCH), nearest(p.give_up_s as f64, &GIVE.map(|x| x as f64)))
}

/// Trades a rule takes among candidates (sorted by fill time) with fills in [from, to) and a
/// sale known by `known_by`, by rule values (so a pick stored under an older grid still
/// evaluates). One position per match at a time (2 minutes, the first move wins). `take` gets the
/// candidate's index and returns whether the trade was placed (an order that cannot fill is not).
fn run_policy<'a>(cands: &[&'a Cand], p: &Policy, from: Ms, to: Ms, known_by: Ms, mut take: impl FnMut(usize, &'a Cand, &Exit) -> bool) {
    let (ci, gi) = sale_idx(p);
    let lo = cands.partition_point(|c| c.tf < from);
    let mut recent: VecDeque<(i64, Ms)> = VecDeque::new();
    for (k, c) in cands[lo..].iter().enumerate() {
        let c: &'a Cand = *c;
        if c.tf >= to {
            break;
        }
        if c.books[0] < p.min_books
            || c.books[1] < p.min_books
            || c.bm[0] < p.min_move
            || c.fol[0] > p.max_followed
            || c.fol[1] > p.max_followed
            || c.sp[0] > p.max_spread + 1e-9
            || c.sp[1] > p.max_spread + 1e-9
            || c.edge[0][ci] < p.min_edge
            || c.edge[1][ci] < p.min_edge
            || (p.skip_after_pause && c.pause)
        {
            continue;
        }
        while recent.front().is_some_and(|r| c.tf - r.1 >= snipe::SAME_MOVE_MS) {
            recent.pop_front();
        }
        if recent.iter().any(|r| r.0 == c.match_id) {
            continue;
        }
        let Some(x) = c.exits[ci][gi] else { continue };
        if x.ts > known_by {
            continue;
        }
        if take(lo + k, c, &x) {
            recent.push_back((c.match_id, c.tf));
        }
    }
}

/// Every candidate's result at one stake: [order size][catch][give-up] = (P&L, dollars spent).
type Priced = [[[Option<(f64, f64)>; 5]; 3]; 2];

fn price_all(cands: &[&Cand], stake: f64) -> Vec<Priced> {
    cands
        .iter()
        .map(|c| {
            let mut p: Priced = [[[None; 5]; 3]; 2];
            for (w, &walk) in WALK.iter().enumerate() {
                for ci in 0..3 {
                    for gi in 0..GIVE.len() {
                        if let Some(x) = &c.exits[ci][gi] {
                            p[w][ci][gi] = fill(c, x, stake, walk).map(|f| (f.pnl, f.spent));
                        }
                    }
                }
            }
            p
        })
        .collect()
}

/// A grid rule at a fixed stake (choosing): `only` filters which taken trades count.
fn stats_where(cands: &[&Cand], pr: &[Priced], i: &Idx, from: Ms, to: Ms, known_by: Ms, stake: f64, only: impl Fn(&Cand) -> bool) -> Stats {
    let p = policy(i);
    let (ci, gi) = sale_idx(&p);
    let mut s = Stats::default();
    run_policy(cands, &p, from, to, known_by, |k, c, _| match pr[k][i[8]][ci][gi] {
        Some((pnl, spent)) => {
            if only(c) {
                s.add(pnl, spent, stake);
            }
            true
        }
        None => false,
    });
    s
}

fn stats(cands: &[&Cand], pr: &[Priced], i: &Idx, from: Ms, to: Ms, known_by: Ms, stake: f64) -> Stats {
    stats_where(cands, pr, i, from, to, known_by, stake, |_| true)
}

/// Choose at time `t` from everything finished before it, judging rules at `stake` dollars.
fn choose(cands: &[&Cand], pr: &[Priced], stake: f64, t: Ms, prev: Option<Idx>, base: &str) -> (Idx, bool, Stats, f64, Value) {
    let lower: Vec<(f64, usize)> = (0..GRID)
        .map(|f| {
            let s = stats(cands, pr, &unflat(f), 0, t, t, stake);
            (if s.n >= MIN_N { s.lower() } else { INELIGIBLE }, s.n)
        })
        .collect();
    // Plateau: the rule and its one-step neighbours.
    let plateau = |f: usize| {
        let i = unflat(f);
        let (mut sum, mut k) = (lower[f].0, 1.0);
        let ok = |g: usize| lower[g].1 >= MIN_N;
        for d in 0..ND {
            for step in [-1i64, 1] {
                let v = i[d] as i64 + step;
                if v >= 0 && (v as usize) < DIMS[d] {
                    let mut j = i;
                    j[d] = v as usize;
                    if ok(flat(&j)) {
                        sum += lower[flat(&j)].0;
                        k += 1.0;
                    }
                }
            }
        }
        sum / k
    };
    let cur = prev.unwrap_or_else(|| default_idx(base));
    let cur_score = plateau(flat(&cur));
    // Validation slice: the most recent 30% of the time with data before the pick.
    let first = cands.first().map(|c| c.tf).unwrap_or(t);
    let split = first + ((t - first) as f64 * (1.0 - VALIDATE_SHARE)) as Ms;
    let cur_val = stats(cands, pr, &cur, split, t, t, stake);
    let mut ranked: Vec<(f64, usize)> = (0..GRID).filter(|f| lower[*f].1 >= MIN_N).map(|f| (plateau(f), f)).collect();
    ranked.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    let mut tried = Vec::new();
    let mut pick = cur;
    for (score, f) in ranked.into_iter().take(VALIDATE_TOP) {
        if f == flat(&cur) {
            break; // the current rules rank above every remaining candidate
        }
        if score <= 0.0 || lower[f].0 <= 0.0 {
            break; // never adopt rules whose conservative estimate is a loss
        }
        if lower[flat(&cur)].1 >= MIN_N && score <= cur_score + SWITCH_MARGIN {
            break; // nothing clearly better
        }
        let v = stats(cands, pr, &unflat(f), split, t, t, stake);
        let ok = v.n >= VALIDATE_MIN_N && v.mean() > 0.0 && (cur_val.n == 0 || v.mean() >= cur_val.mean());
        if tried.len() < 5 {
            tried.push(json!({"rules": policy(&unflat(f)), "plateau": score, "validation": v.json(), "passed": ok}));
        }
        if ok {
            pick = unflat(f);
            break;
        }
    }
    let s = stats(cands, pr, &pick, 0, t, t, stake);
    let report = json!({"validation_from": split, "current_validation": cur_val.json(), "tried": tried, "eligible": lower.iter().filter(|x| x.1 >= MIN_N).count()});
    (pick, pick != cur, s, plateau(flat(&pick)), report)
}

// ---------------------------------------------------------------- paper money

struct Taken<'a> {
    /// Which segment (run) placed it.
    seg: usize,
    c: &'a Cand,
    x: Exit,
    f: Fill,
    /// What the bot meant to stake (share of the balance, capped by free cash).
    stake: f64,
}

/// Paper trading with a balance over consecutive segments (rules, from, to). Each trade stakes
/// `pct` of the balance (start plus closed P&L) when placed, at most the cash not tied up in open
/// trades. Every returned trade is closed by `known_by`, so the final balance is start + P&L.
fn simulate<'a>(cands: &[&'a Cand], segs: &[(Policy, Ms, Ms)], known_by: Ms, bank: &Bank) -> (Vec<Taken<'a>>, Stats) {
    let (mut cash, mut closed) = (bank.start, 0.0);
    let mut open: Vec<(Ms, f64, f64)> = Vec::new();
    let mut out = Vec::new();
    let mut s = Stats::default();
    for (si, (p, from, to)) in segs.iter().enumerate() {
        run_policy(cands, p, *from, *to, known_by, |_, c, x| {
            open.retain(|o| {
                if o.0 <= c.tf {
                    cash += o.1 + o.2;
                    closed += o.2;
                    false
                } else {
                    true
                }
            });
            let stake = (bank.pct * (bank.start + closed)).min(cash);
            if stake < snipe::MIN_FILL {
                return false; // broke, or all cash in open trades
            }
            let Some(f) = fill(c, x, stake, p.walk_book) else { return false };
            cash -= f.spent;
            open.push((x.ts, f.spent, f.pnl));
            s.add(f.pnl, f.spent, stake);
            out.push(Taken { seg: si, c, x: *x, f, stake });
            true
        });
    }
    (out, s)
}

// ---------------------------------------------------------------- state, schedule

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Run {
    pub ts: Ms,
    pub base: String,
    pub chosen: Policy,
    /// What the bot trades until the next run (the defaults when the safety switch fired).
    pub active: Policy,
    pub switched: bool,
    pub fallback: bool,
    pub insample: Value,
    pub pause_test: Value,
    pub candidates: usize,
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub validation: Value,
    /// The balance settings the run was made with.
    #[serde(default)]
    pub bank: Option<Bank>,
    /// The picked rules at this stake taking only the best price vs walking 2c.
    #[serde(default)]
    pub book_test: Value,
}

struct Chunk {
    built_ms: Ms,
    complete: bool,
    c: [Arc<Vec<Cand>>; 2],
}

#[derive(Default)]
struct State {
    chunks: BTreeMap<Ms, Chunk>,
    runs: Vec<Run>,
    status: String,
    error: Option<String>,
    data_from: Option<Ms>,
    bank: Bank,
    /// Every block measured once since start (until then the page shows progress, not a partial backtest).
    warm: bool,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);
/// Wakes the loop early (balance settings changed).
static KICK: LazyLock<tokio::sync::Notify> = LazyLock::new(tokio::sync::Notify::new);

fn boundary(t: Ms) -> Ms {
    t.div_euclid(EVERY_MS) * EVERY_MS
}

fn conn(app: &App) -> rusqlite::Result<Connection> {
    let c = Connection::open(&app.db_path)?;
    c.busy_timeout(Duration::from_secs(10))?;
    Ok(c)
}

fn load_runs(app: &App, bank: &Bank) -> Vec<Run> {
    let runs = load_runs_raw(app);
    if runs.iter().any(|r| r.version != VERSION || r.bank.as_ref() != Some(bank)) {
        // Picks made by another method version or other money: recompute the whole history.
        if let Ok(c) = conn(app) {
            let _ = c.execute("DELETE FROM adaptive_runs", []);
        }
        tracing::info!("adaptive: method version {VERSION} / balance settings changed; recomputing the pick history from the first data");
        return Vec::new();
    }
    runs
}

fn load_runs_raw(app: &App) -> Vec<Run> {
    conn(app)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT data FROM adaptive_runs ORDER BY ts_ms, base").ok()?;
            let it = st.query_map([], |r| r.get::<_, String>(0)).ok()?;
            Some(it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str::<Run>(&s).ok()).collect())
        })
        .unwrap_or_default()
}

fn save_run(app: &App, r: &Run) {
    if let Ok(c) = conn(app) {
        let _ = c.execute(
            "INSERT OR REPLACE INTO adaptive_runs(ts_ms, base, data) VALUES (?1, ?2, ?3)",
            params![r.ts, r.base, serde_json::to_string(r).unwrap_or_default()],
        );
    }
}

/// Candidate references for one market, sorted by fill time.
fn all_cands(chunks: &[[Arc<Vec<Cand>>; 2]], bi: usize) -> Vec<&Cand> {
    let mut v: Vec<&Cand> = chunks.iter().flat_map(|c| c[bi].iter()).collect();
    v.sort_by_key(|c| c.tf);
    v
}

fn snapshot() -> Vec<[Arc<Vec<Cand>>; 2]> {
    ST.lock().chunks.values().map(|c| c.c.clone()).collect()
}

/// Segments of a run list: each run's active rules until the next run (or `end`).
fn segments(rs: &[&Run], end: Ms, rules: impl Fn(&Run) -> Policy) -> Vec<(Policy, Ms, Ms)> {
    rs.iter().enumerate().map(|(k, r)| (rules(r), r.ts, rs.get(k + 1).map(|n| n.ts).unwrap_or(end))).collect()
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(20)).await;
        {
            let bank = load_bank(&app);
            let runs = load_runs(&app, &bank);
            let mut st = ST.lock();
            st.bank = bank;
            st.runs = runs;
        }
        loop {
            let a2 = app.clone();
            let r = tokio::task::spawn_blocking(move || step(&a2)).await;
            if let Ok(Err(e)) = r {
                tracing::warn!("adaptive: {e:#}");
                ST.lock().error = Some(format!("{e:#}"));
            }
            // Wake for the next boundary or the next live rebuild, whichever is first.
            let now = now_ms();
            let next = (boundary(now) + EVERY_MS + 60_000 - now).min(REBUILD_MS).max(30_000);
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(next as u64)) => {}
                _ = KICK.notified() => {}
            }
        }
    });
}

/// Bring the candidate table up to date, then make every re-choice that is due.
fn step(app: &App) -> anyhow::Result<()> {
    let now = now_ms();
    let from: Ms = {
        let c = crate::store::open_ro(&app.db_path)?;
        c.query_row("SELECT MIN(ts_ms) FROM changes", [], |r| r.get::<_, Option<Ms>>(0))?.unwrap_or(now)
    };
    let bank = {
        let mut st = ST.lock();
        st.data_from = Some(from);
        st.bank
    };
    // ---- chunks
    let mut start = boundary(from);
    while start < now {
        let end = start + EVERY_MS;
        let need = {
            let st = ST.lock();
            match st.chunks.get(&start) {
                None => true,
                Some(ch) => !ch.complete && now - ch.built_ms >= REBUILD_MS,
            }
        };
        if need {
            ST.lock().status = format!("measuring moves {} UTC", fmt_utc(start));
            let built = build_chunk(&app.db_path, start, end.min(now + 1), now)?;
            let complete = end + EXIT_HORIZON_MS <= now;
            ST.lock().chunks.insert(start, Chunk { built_ms: now, complete, c: built.map(Arc::new) });
        }
        start = end;
    }
    // ---- re-choices due: every boundary from 8 h after the data starts
    let first = boundary(from + FIRST_AFTER_MS) + EVERY_MS;
    let mut t = first;
    let chunks = snapshot();
    while t <= boundary(now) {
        for (bi, base) in BASES.iter().enumerate() {
            let have = ST.lock().runs.iter().any(|r| r.ts == t && r.base == *base);
            if have {
                continue;
            }
            ST.lock().status = format!("choosing rules for {} at {} UTC", base, fmt_utc(t));
            let cands = all_cands(&chunks, bi);
            let history: Vec<Run> = ST.lock().runs.iter().filter(|r| r.base == *base && r.ts < t).cloned().collect();
            let prev_chosen = history.last().map(|r| idx_of(&r.chosen));
            // The honest record so far sets the balance, and the balance the stake the rules are
            // judged at.
            let hr: Vec<&Run> = history.iter().collect();
            let (past, past_s) = simulate(&cands, &segments(&hr, t, |r| r.active), t, &bank);
            let balance = bank.start + past_s.pnl;
            let stake = (bank.pct * balance).max(snipe::MIN_FILL);
            let pr = price_all(&cands, stake);
            let (pick, switched, ins, plateau, validation) = choose(&cands, &pr, stake, t, prev_chosen, base);
            // Pause test on the picked rules: with and without moves right after a pause.
            let (mut with_pause, mut no_pause) = (pick, pick);
            with_pause[7] = 0;
            no_pause[7] = 1;
            let pause_test = json!({
                "with_pause_moves": stats(&cands, &pr, &with_pause, 0, t, t, stake).json(),
                "skipping_them": stats(&cands, &pr, &no_pause, 0, t, t, stake).json(),
                "pause_moves_alone": stats_where(&cands, &pr, &with_pause, 0, t, t, stake, |c| c.pause).json()});
            // Order-size test on the picked rules, on moves with a recorded order book.
            let (mut best_only, mut walk) = (pick, pick);
            best_only[8] = 0;
            walk[8] = 1;
            let book_test = json!({"stake": stake,
                "best_price_only": stats_where(&cands, &pr, &best_only, 0, t, t, stake, |c| c.buy.is_some()).json(),
                "walk_2c": stats_where(&cands, &pr, &walk, 0, t, t, stake, |c| c.buy.is_some()).json()});
            // Safety switch: the honest record of the last 24 hours.
            let mut recent = Stats::default();
            for x in past.iter().filter(|x| x.c.tf >= t - FALLBACK_SPAN_MS) {
                recent.add(x.f.pnl, x.f.spent, x.stake);
            }
            let fallback = recent.n >= FALLBACK_MIN_TRADES && recent.pnl < 0.0 && pick != default_idx(base);
            let run = Run {
                ts: t,
                base: base.to_string(),
                chosen: policy(&pick),
                active: if fallback { policy(&default_idx(base)) } else { policy(&pick) },
                switched,
                fallback,
                insample: json!({"stats": ins.json(), "plateau": plateau, "recent_24h_pnl": recent.pnl, "recent_24h_trades": recent.n, "balance": balance, "stake": stake}),
                pause_test,
                candidates: cands.iter().filter(|c| c.tf < t).count(),
                version: VERSION,
                validation,
                bank: Some(bank),
                book_test,
            };
            let mut st = ST.lock();
            if st.bank != bank {
                return Ok(()); // settings changed meanwhile: the next step recomputes
            }
            save_run(app, &run);
            st.runs.push(run);
            st.runs.sort_by(|a, b| (a.ts, &a.base).cmp(&(b.ts, &b.base)));
        }
        t += EVERY_MS;
    }
    let mut st = ST.lock();
    st.status = "ready".into();
    st.warm = true;
    Ok(())
}

fn fmt_utc(ms: Ms) -> String {
    time::OffsetDateTime::from_unix_timestamp(ms / 1000).map(|t| format!("{:02} {:?} {:02}:{:02}", t.day(), t.month(), t.hour(), t.minute())).unwrap_or_default()
}

// ---------------------------------------------------------------- the record and the API

fn trade_json(base: &str, t: &Taken) -> Value {
    let (c, x, f) = (t.c, &t.x, &t.f);
    json!({"base": base, "ts": c.tf, "exit_ts": x.ts, "match_id": c.match_id, "sport": c.sport, "home": c.home, "away": c.away,
        "outcome": c.outcome, "dir": c.dir, "trigger": c.trigger, "entry": f.entry, "exit": f.exit, "pnl": f.pnl, "spent": f.spent,
        "stake": t.stake, "filled": f.filled, "reason": if x.caught { "caught up" } else { "gave up" }, "after_pause": c.pause})
}

/// Balance after each closed trade: [[exit time, balance], ...], starting at `from`.
fn balance_curve(from: Ms, start: f64, trades: &[(Ms, f64)]) -> Vec<Value> {
    let mut v: Vec<(Ms, f64)> = trades.to_vec();
    v.sort_by_key(|x| x.0);
    let mut bal = start;
    std::iter::once(json!([from, start])).chain(v.into_iter().map(|(t, p)| { bal += p; json!([t, bal]) })).collect()
}

/// One set of rules replayed over every move recorded, with a fresh paper balance.
fn backtest(cands: &[&Cand], p: &Policy, now: Ms, bank: &Bank, base: &str) -> (Vec<Value>, Stats, Vec<Value>) {
    let (taken, s) = simulate(cands, &[(*p, 0, now + 1)], now, bank);
    let from = cands.first().map(|c| c.tf).unwrap_or(now);
    let curve = if taken.is_empty() { Vec::new() } else { balance_curve(from, bank.start, &taken.iter().map(|x| (x.x.ts, x.f.pnl)).collect::<Vec<_>>()) };
    let mut trades: Vec<Value> = taken.iter().map(|x| trade_json(base, x)).collect();
    trades.sort_by_key(|t| t["exit_ts"].as_i64());
    (trades, s, curve)
}

/// The paper bot as a backtest: the rules picked last (from every finished trade so far) replayed
/// over the WHOLE dataset; each re-pick re-runs it. The default rules replayed the same way for
/// comparison. Each run in the history also shows its rules over the whole dataset.
pub fn view(_app: &App) -> Value {
    let now = now_ms();
    let chunks = snapshot();
    let (runs, status, error, from, nchunks, bank, warm) = {
        let st = ST.lock();
        (st.runs.clone(), st.status.clone(), st.error.clone(), st.data_from, st.chunks.len(), st.bank, st.warm)
    };
    let blocks_total = from.map(|f| ((boundary(now) - boundary(f)) / EVERY_MS + 1) as usize).unwrap_or(0);
    if !warm {
        return json!({"now_ms": now, "status": status, "error": error, "building": true, "blocks_done": nchunks.min(blocks_total), "blocks_total": blocks_total, "bank": bank, "bases": []});
    }
    let bases: Vec<Value> = BASES
        .iter()
        .enumerate()
        .map(|(bi, base)| {
            let cands = all_cands(&chunks, bi);
            let current = runs.iter().filter(|r| r.base == *base).last().cloned();
            let rules = current.as_ref().map(|r| r.active).unwrap_or_else(|| policy(&default_idx(base)));
            let (trades, tot, curve) = backtest(&cands, &rules, now, &bank, base);
            let (_, tot_def, curve_defaults) = backtest(&cands, &policy(&default_idx(base)), now, &bank, base);
            // Every pick's rules over the whole dataset (same rules -> same numbers, computed once).
            let mut memo: Vec<(Policy, Value)> = Vec::new();
            let windows: Vec<Value> = runs
                .iter()
                .filter(|r| r.base == *base)
                .map(|r| {
                    let v = match memo.iter().find(|(p, _)| *p == r.active) {
                        Some((_, v)) => v.clone(),
                        None => {
                            let (_, s) = simulate(&cands, &[(r.active, 0, now + 1)], now, &bank);
                            let mut v = s.json();
                            v["balance"] = json!(bank.start + s.pnl);
                            memo.push((r.active, v.clone()));
                            v
                        }
                    };
                    json!({"run": r, "from": r.ts, "whole": v})
                })
                .collect();
            let depth_known = cands.iter().filter(|c| c.buy.is_some()).count();
            let recent: Vec<Value> = trades.iter().rev().take(80).cloned().collect();
            json!({"base": base, "current": current, "rules": rules, "default": policy(&default_idx(base)),
                "record": tot.json(), "defaults_record": tot_def.json(),
                "balance": bank.start + tot.pnl, "balance_defaults": bank.start + tot_def.pnl,
                "curve": curve, "curve_defaults": curve_defaults, "windows": windows, "recent": recent,
                "candidates": cands.len(), "depth_known": depth_known})
        })
        .collect();
    let next = boundary(now) + EVERY_MS;
    json!({"now_ms": now, "status": status, "error": error, "data_from": from, "chunks": nchunks,
        "first_run_ms": from.map(|f| boundary(f + FIRST_AFTER_MS) + EVERY_MS), "next_run_ms": next,
        "every_ms": EVERY_MS, "min_trades": MIN_N, "version": VERSION, "grid": GRID, "bank": bank, "bases": bases})
}

/// Newly closed paper trades of the current rules (for Telegram).
pub fn closed_since(since: Ms) -> Vec<Value> {
    let now = now_ms();
    let chunks = snapshot();
    let (runs, bank) = {
        let st = ST.lock();
        (st.runs.clone(), st.bank)
    };
    let mut out = Vec::new();
    for (bi, base) in BASES.iter().enumerate() {
        let cands = all_cands(&chunks, bi);
        let rules = runs.iter().filter(|r| r.base == *base).last().map(|r| r.active).unwrap_or_else(|| policy(&default_idx(base)));
        let (trades, _, _) = backtest(&cands, &rules, now, &bank, base);
        out.extend(trades.into_iter().filter(|t| t["exit_ts"].as_i64().unwrap_or(0) > since));
    }
    out.sort_by_key(|t| t["exit_ts"].as_i64());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cand(px: f64, buy: Option<(f64, f64)>) -> Cand {
        Cand {
            match_id: 1,
            sport: "football".into(),
            home: "A".into(),
            away: "B".into(),
            outcome: "home".into(),
            dir: 1,
            trigger: "x".into(),
            pm: true,
            tf: 0,
            books: [2, 2],
            bm: [0.1, 0.1],
            fol: [0.0, 0.0],
            sp: [0.01, 0.01],
            edge: [[0.05; 3]; 2],
            px,
            buy,
            pause: false,
            exits: [[None; 5]; 3],
        }
    }

    #[test]
    fn order_size_rules() {
        let x = Exit { ts: 1, px: 0.60, sell: None, caught: true };
        // No recorded book: the whole stake fills at the price.
        let f = fill(&cand(0.50, None), &x, 500.0, false).unwrap();
        assert!((f.spent - 500.0).abs() < 1e-9 && (f.pnl - 100.0).abs() < 1e-9 && f.filled.is_none());
        // 200 contracts at the best price, 1,000 within 2c: best price only buys $100.
        let c = cand(0.50, Some((200.0, 1000.0)));
        let b = fill(&c, &x, 500.0, false).unwrap();
        assert!((b.spent - 100.0).abs() < 1e-9 && (b.filled.unwrap() - 0.2).abs() < 1e-9);
        // Walking the book fills the rest 2c worse.
        let w = fill(&c, &x, 500.0, true).unwrap();
        assert!((w.spent - 500.0).abs() < 1e-6 && w.entry > 0.50 && w.pnl > b.pnl);
    }

    #[test]
    fn grid_round_trip() {
        for f in [0, 1, GRID / 2, GRID - 1] {
            assert_eq!(flat(&unflat(f)), f);
            assert_eq!(idx_of(&policy(&unflat(f))), unflat(f));
        }
    }
}

/// Research dump: every move re-measured with another reaction / confirm delay, flattened with
/// its features and the result of every sale at a $25 stake (walking the book).
pub fn dump(app: &App, reaction: Ms, confirm: Ms) -> anyhow::Result<Value> {
    let now = now_ms();
    let from: Ms = {
        let c = crate::store::open_ro(&app.db_path)?;
        c.query_row("SELECT MIN(ts_ms) FROM changes", [], |r| r.get::<_, Option<Ms>>(0))?.unwrap_or(now)
    };
    let mut out = Vec::new();
    let mut start = boundary(from);
    while start < now {
        let ch = build_chunk_with(&app.db_path, start, (start + EVERY_MS).min(now + 1), now, (reaction, confirm))?;
        for (bi, v) in ch.iter().enumerate() {
            for c in v {
                let mut ex = Vec::new();
                for ci in 0..3 {
                    for gi in 0..GIVE.len() {
                        ex.push(c.exits[ci][gi].and_then(|x| fill(c, &x, 25.0, true)).map(|f| (f.pnl / 25.0 * 10_000.0).round() / 10_000.0));
                    }
                }
                out.push(json!({"b": BASES[bi], "m": c.match_id, "s": c.sport, "o": c.outcome, "d": c.dir, "tr": c.trigger, "t": c.tf,
                    "k": c.books, "bm": c.bm, "fol": c.fol, "sp": c.sp, "e": c.edge[1], "px": c.px, "p": c.pause, "dep": c.buy.is_some(), "x": ex}));
            }
        }
        start += EVERY_MS;
    }
    Ok(json!({"reaction": reaction, "confirm": confirm, "catch": CATCH, "give": GIVE, "rows": out}))
}
