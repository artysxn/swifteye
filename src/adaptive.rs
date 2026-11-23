//! Snipe paper bot with walk-forward ("adaptive") rules.
//!
//! The Snipe simulator buys a prediction market when a bookmaker moves and the market has not
//! followed yet. Which moves to take and when to sell are decision rules; this module re-chooses
//! them from the data every 4 hours (00, 04, 08, 12, 16 and 20 UTC), separately for Polymarket and
//! Kalshi, and paper-trades the current choice.
//!
//! * Facts are fixed, not tuned: reaction 1 s, confirming check 0.5 s later, order travel and the
//!   venue's matching delay as measured by the test runs (timing.rs: ~1.5 s on live Polymarket games),
//!   the buy at the price resting when the order matched (no slippage on top, as the real buys
//!   filled), 1c slippage on every sale, Kalshi taker fee, Polymarket sports taker fee
//!   (5% x p x (1-p) a contract, each side; version 6, before that 0), the real order book
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
//!   trades to qualify. Version 3 staked a fixed $100. Version 5 had no Polymarket fee. Version 6
//!   filled 250 ms after the decision; version 7 uses the measured order and matching times.)
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
use crate::rank::KALSHI;
use crate::snipe::{self, Quotes, Q};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const PM: &str = "polymarket";
const BASES: [&str; 2] = [PM, KALSHI];
/// Which markets the paper bot trades. Kalshi is OFF (2026-11-18): unhedged, its snipe lost
/// (-5% a trade over 851 lagging moves, -11% in the second half; walk-forward -4% after the
/// rules were frozen) because Kalshi often does not catch up and its spread eats the exit. Its
/// rules are still re-chosen and shown as research; no paper trades, no Telegram alerts.
const TRADED: [bool; 2] = [true, false];

// ---- facts (measured, never tuned)
const REACTION: Ms = 1_000;
const CONFIRM: Ms = 500;
/// Order travel and venue matching come from the diagnostic test runs (timing.rs): Polymarket's
/// reply plus, on games already playing, its matching delay; Kalshi's order time.
const SLIP: f64 = 0.01;
/// Slippage on a buy against the book recorded when the order matched. The real buys (19 Nov)
/// paid exactly the best price resting then (4 of 5; the fifth walked 6 @ 80c + the rest @ 82c,
/// as the depth model prices it), so nothing is added: the tests of a move keep their 1c margin,
/// only the money counted is exact. Sales keep 1c (real: +1, 0, 0, -1.5, -2c against the bid).
const SLIP_BUY: f64 = 0.0;
/// Stake used only to estimate the fee per contract in the expected-gain test.
const REF_STAKE: f64 = 100.0;
const CAP: f64 = 0.02;

// ---- schedule
pub const EVERY_MS: Ms = 4 * 3_600_000;
const FIRST_AFTER_MS: Ms = 8 * 3_600_000;
const EXIT_HORIZON_MS: Ms = 35 * 60_000;
const REBUILD_MS: Ms = 30 * 60_000;
const PAUSE_WINDOW_MS: Ms = 120_000;

// ---- choosing
/// Method version: stored with every run; a change recomputes the history.
const VERSION: u32 = 19;
/// Version of the candidate measurements on disk (bump only when `Cand` or its measuring changes;
/// rule or filter changes re-pick the rules but keep the measurements).
/// 17 (21 Nov 2026): a move measured where the Polymarket recording had a gap is marked `blind`.
/// 18 (21 Nov 2026): basketball moves carry `us_bb` (a US game: NBA or college).
/// 19 (22 Nov 2026): the earlier-jumps history ('followed' / 'lead') reads Polymarket by its own clock + 5 s
/// (consensus::FOLLOW_SLACK_MS), not by arrival time.
const CAND_VERSION: u32 = 19;
/// A gap in the recording: no Polymarket quote from ANY game for this long around the buy.
const HOLE_MS: Ms = 2_000;
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

/// The safe set never buys when Polymarket has already made more than this share of the move,
/// whatever the rule-picker chose for a window (it once allowed 25%: the replay's total loss).
pub const SAFE_MAX_FOLLOWED: f64 = 0.10;
/// The smallest bookmaker move the replay measures and the live bot takes. Every bookmaker update
/// is a trigger (hundreds a minute): smaller moves tested worse and measuring them all takes hours.
pub const LIVE_MIN_MOVE: f64 = 0.07;
/// A game's delay score: how much of earlier bookmaker jumps Polymarket made AFTER them, less how
/// much it had already made before them (`consensus::Ctx::follow - lead`). Under this, or with no
/// earlier jump to judge by, Polymarket has not been trailing the bookmakers in this game.
pub const WEAK_SCORE: f64 = 0.2;
/// The share of the usual stake put on a move in such a game. The move is still taken: skipping it
/// frees the game for a later, worse move (tools/blend.py, 17-20 Nov: the same 70 trades at half
/// stake in weak games lost $36 instead of $65 for $10 less profit; on 3,356 recorded moves
/// Polymarket caught up 8% of the move at the lowest scores and 28% at the highest).
pub const WEAK_STAKE: f64 = 0.5;

/// The delay score of a move's game (None = no earlier jump in the game).
pub fn delay_score(cx: &crate::consensus::Ctx) -> Option<f64> {
    (cx.jumps > 0).then(|| cx.follow - cx.lead)
}

/// The stake factor of a safe-set move: full in a game where Polymarket trails, `WEAK_STAKE` elsewhere.
pub fn stake_factor(cx: &crate::consensus::Ctx) -> f64 {
    if delay_score(cx).is_some_and(|s| s >= WEAK_SCORE) { 1.0 } else { WEAK_STAKE }
}
/// Bookmakers moving the same way within this long of the first one are one move.
pub const MOVE_MS: Ms = 90_000;
/// A bookmaker update this far from that bookmaker's previous price is a trigger (snipebot).
const TRIGGER_JUMP: f64 = 0.04;
/// How long after a sale with no book the result is looked for.
const AFTER_BOOK_MS: Ms = 10 * 60_000;

/// The wider set's rules of the move: 2+ feeds, 7-point move, Polymarket followed 10% at most,
/// 2c expected gain, 4c spread, sell on the full catch-up, give up after 15 minutes.
pub fn policy_b() -> Policy {
    Policy { min_books: 2, max_followed: 0.10, min_edge: 0.02, max_spread: 0.04, min_move: 0.07, catch: 1.0, give_up_s: 900, skip_after_pause: false, walk_book: true }
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
/// The rules to trade are known: pinned in the settings, or the picker's choice has been loaded
/// from disk (20 s after start; much longer after a method change, when it is recomputed).
pub fn rules_ready() -> bool {
    crate::bots::settings().snipe.pin_rules || ST.lock().runs.iter().any(|r| r.base == PM)
}

/// The rules tested on the live path on 17-19 Nov 2026 (tools/live_eval.py and the replay agree
/// on them trade for trade): Polymarket followed at most 5%, 4c+ expected gain, spread at most
/// 4c, sell at full catch-up, no buy after a pause. The give-up time comes from the settings.
/// The move must be 8+ points (10 until 21 Nov 2026). On the replay of 17-20 Nov with the moves
/// inside gaps of the recording left out, and no Dota 2: 10 points 29 trades +$32, 8 points 41
/// trades +$56, more on each of the four days; with `Flt::picked`'s 50% as well 34 trades, 76%
/// won, +$61, worst trade -$3.37 (tools/sweep.py on the flagged set; TRADING-TRIED.md).
pub fn pinned_rules() -> Policy {
    // give_up_s 900 since 22 Nov 2026 (owner: "15 min give up"; the live bot takes it from the settings, snipe.give_up_s).
    Policy { min_books: 1, max_followed: 0.05, min_edge: 0.04, max_spread: 0.04, min_move: 0.08, catch: 1.0, give_up_s: 900, skip_after_pause: true, walk_book: true }
}

/// The Polymarket rules the bot trades now: the last pick, else the defaults.
pub fn active_rules() -> Policy {
    if crate::bots::settings().snipe.pin_rules {
        return pinned_rules();
    }
    ST.lock().runs.iter().filter(|r| r.base == PM).last().map(|r| r.active).unwrap_or_else(|| policy(&default_idx(PM)))
}

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

// ---------------------------------------------------------------- trend filters

/// Extra trade filters and the stop-loss sale, outside the rule grid (tested by replay; the live
/// bot uses `Flt::live()`).
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Flt {
    /// Nothing bought above 90c.
    pub cap: bool,
    /// Skip when Polymarket's own price fell more than this (in our direction) over the 10 min before.
    pub pm_trend: Option<f64>,
    /// Skip when the trigger bookmaker fell more than this over the 10 min before its move.
    pub book_trend: Option<f64>,
    /// Skip when the trigger bookmaker's move over 5 min is under this share of its 10 s move
    /// (a bounce back from a dip, not new information).
    pub rev: Option<f64>,
    /// Skip when the 10 s move is under this multiple of the trigger book's range over the 10 min
    /// before (a swingy game where such jumps are routine).
    pub noise: Option<f64>,
    /// Sell early once the bought side can only be sold this share of the bookmakers' move below
    /// the buy price (at least 4c).
    pub stop: Option<f64>,
    /// Sell early once the bookmakers' consensus gave back half its move (the reason is gone).
    pub revert: bool,
    /// Consensus filters (see consensus.rs): least consensus move, least gap shock, least
    /// follow share (needs an earlier jump), most lead share, least z, least families moved.
    pub cons: Option<f64>,
    pub shock: Option<f64>,
    pub follow: Option<f64>,
    pub lead: Option<f64>,
    pub z: Option<f64>,
    pub fams: Option<usize>,
    /// Skip the paper-only sports (see `paper_only`): live basketball (swings every possession;
    /// the replay's big losers) and the sports with no record yet.
    pub no_basketball: bool,
    /// Nothing bought above this price (the wider set: 40c).
    pub max_px: Option<f64>,
    /// Skip moves triggered by the feeds that lost in the wider set (duel, the 1xbet group,
    /// olimp, betcity).
    pub skip_feeds: bool,
    /// Trade only between these UTC hours [from, to).
    pub hours: Option<[u8; 2]>,
    /// Skip when Polymarket already sat this far from the bookmakers' consensus before the move,
    /// on the side bought (see `gap_before`). The trade assumes the two agreed and the bookmakers
    /// moved first; a wide gap beforehand means Polymarket moved first and the books are the
    /// slow side (19 Nov 02:49 UTC, Azuma Visaya: 25 points apart, then -63%).
    #[serde(default)]
    pub gap: Option<f64>,
    /// Skip a WEAK game (delay score under `WEAK_SCORE`, or no earlier jump) when the price offered
    /// is above this. Owner, 21 Nov 2026, "apply the losing signal filter": the one group of
    /// trades under today's rules that lost money both in the clean replay (8 trades, 4 won,
    /// -$1.58; leaving them out +$5.56) and with real money (3 trades, 0 won, -$3.15). A dear
    /// price leaves little to gain, and in a weak game the catch-up is the less likely outcome.
    #[serde(default)]
    pub weak_cap: Option<f64>,
}

/// A Dota 2 game trades real money only with a spread up to this (owner, 21 Nov 2026, "apply the
/// two winning signals": 4 clean replay trades, 4 won, +$14.22; with a wider spread it stays on
/// paper). Thin evidence, and no more than a random group of 4 would give 1 time in 11.
pub const DOTA_MAX_SPREAD: f64 = 0.02;

/// A US basketball game (NBA or college), from the leagues the bookmakers list it under and the
/// teams' names. Those lost in the replay; basketball elsewhere made +$7.62 on 5 clean trades
/// (4 won) and +$0.36 on 3 paper trades against -$3.63 on 2 US ones.
pub fn us_basketball_text(leagues_and_teams: &str) -> bool {
    let s = leagues_and_teams.to_lowercase();
    (s.replace("nbb", "").contains("nba") && !s.contains("wnba")) || s.contains("ncaa") || s.contains("college") || s.contains("(stud)") || s.contains("cbb")
}

pub fn us_basketball(c: &Connection, match_id: i64, home: &str, away: &str) -> bool {
    let leagues: Vec<String> = c
        .prepare_cached("SELECT DISTINCT league FROM book_events WHERE match_id = ?1 AND league IS NOT NULL")
        .and_then(|mut st| st.query_map([match_id], |r| r.get::<_, String>(0)).map(|it| it.flatten().collect()))
        .unwrap_or_default();
    us_basketball_text(&format!("{} {home} {away}", leagues.join(" | ")))
}

/// Is THIS game paper-only? A paper-only sport, except basketball outside the US and Dota 2 with
/// a tight spread (owner, 21 Nov 2026).
pub fn paper_only_game(sport: &str, us_bb: bool, spread: f64) -> bool {
    match sport {
        "basketball" => us_bb,
        "dota2" => spread > DOTA_MAX_SPREAD + 1e-9,
        s => paper_only(s),
    }
}

/// Sports real money does not trade. Basketball lost in the replay (US college games above
/// all); American football and MMA were not recorded before 20 Nov 2026, so nothing is known
/// about them. Dota 2 (21 Nov 2026): of the replay trades of 17-20 Nov bought while quotes were
/// really being recorded, the 8 in Dota 2 won 2 and lost $20 (one lost the whole stake), and
/// every other sport together won 16 of 25 for +$26 (tools/tells_final.py). Eight trades is thin
/// evidence, so it is on paper, not dropped. The safe and wider sets skip these sports, live and
/// in the replay alike; the bot trades them on their own paper wallet (`bots::SNIPE_TRIAL`, rules
/// `Flt::trial`) until that record says otherwise.
pub const PAPER_ONLY_SPORTS: &[&str] = &["basketball", "amfootball", "mma", "dota2"];

pub fn paper_only(sport: &str) -> bool {
    PAPER_ONLY_SPORTS.contains(&sport)
}

/// How far the bookmakers' consensus stood above Polymarket's price BEFORE the move, in the
/// direction bought (positive = Polymarket was already under the books for the side we buy).
/// None when the Polymarket price before the move is not known.
pub fn gap_before(cx: &crate::consensus::Ctx, dir: f64, pre_mid: f64) -> Option<f64> {
    (pre_mid > 0.0).then(|| (cx.cons_now - cx.cons_move * dir - pre_mid) * dir)
}

impl Flt {
    /// The rules the replay of 17-19 Nov picked (no basketball, 2+ feed families, consensus and
    /// gap moved 3+ pts, Polymarket followed 30%+ of earlier jumps in the game, <= 90c): 53
    /// trades in 1.8 days, all won, +29%/$, median hold 58 s.
    pub fn picked() -> Flt {
        // follow 0.5 (0.3 until 21 Nov 2026): Polymarket must have made half of the earlier jumps in
        // this game after the bookmakers. Same profit on the clean replay with half the largest
        // drop ($4.59 against $8.81); see `pinned_rules`.
        Flt { cap: true, fams: Some(2), cons: Some(0.03), follow: Some(0.5), no_basketball: true, gap: Some(0.10), weak_cap: Some(0.60), ..Default::default() }
    }
    /// The safe set as the live bot runs it: `picked` inside the trading hours of the settings.
    pub fn live() -> Flt {
        Flt { hours: crate::bots::settings().snipe.hours(), ..Flt::picked() }
    }
    /// The wider set (more trades, smaller stakes): any consensus move, Polymarket followed 20%+
    /// of earlier jumps, bought at 40c or less, not triggered by the feeds that lost there.
    /// Rules of the move itself: `policy_b`.
    pub fn wider() -> Flt {
        Flt { cap: true, fams: Some(2), follow: Some(0.2), no_basketball: true, max_px: Some(0.40), skip_feeds: true, hours: crate::bots::settings().snipe.hours(), gap: Some(0.10), ..Default::default() }
    }
    /// The paper trial of the sports real money does not trade: the safe set's rules with those
    /// sports let in, at every hour (most of their games are played at night in Europe).
    pub fn trial() -> Flt {
        Flt { no_basketball: false, ..Flt::picked() }
    }
    /// These filters for one game: a paper-only sport's game that real money may trade (see
    /// `paper_only_game`) is not refused for its sport.
    pub fn for_game(&self, sport: &str, us_bb: bool, spread: f64) -> Flt {
        if self.no_basketball && paper_only(sport) && !paper_only_game(sport, us_bb, spread) {
            Flt { no_basketball: false, ..*self }
        } else {
            *self
        }
    }
    fn keep(&self, c: &Cand) -> bool {
        // The price the live bot tests: the offer plus its 1c margin.
        self.for_game(&c.sport, c.us_bb, c.sp[0].max(c.sp[1])).check(&c.sport, &c.trigger, c.tf, c.gate_px(), c.bm, [c.pm_tr, c.b_tr5, c.b_tr10, c.b_rng], &c.cx, gap_before(&c.cx, c.dir as f64, c.pre_mid)).is_none()
    }
    /// Why a move fails the filters (None = passes). `tr` = [pm_tr, b_tr5, b_tr10, b_rng].
    pub fn check(&self, sport: &str, trigger: &str, t: Ms, px: f64, bm: [f64; 2], tr: [f64; 4], cx: &crate::consensus::Ctx, gap: Option<f64>) -> Option<&'static str> {
        let [pm_tr, b_tr5, b_tr10, b_rng] = tr;
        if let Some([a, b]) = self.hours {
            let h = (t.rem_euclid(86_400_000) / 3_600_000) as u8;
            if a != b && !(if a < b { h >= a && h < b } else { h >= a || h < b }) {
                return Some("outside_hours");
            }
        }
        if self.max_px.is_some_and(|m| px > m) {
            return Some("price_above_set_limit");
        }
        if self.skip_feeds && matches!(snipe::platform(trigger), "duel" | "1xbet" | "olimp" | "betcity") {
            return Some("weak_feed");
        }
        if self.no_basketball && paper_only(sport) {
            return Some(if sport == "basketball" { "basketball" } else { "paper_only_sport" });
        }
        if self.fams.is_some_and(|k| cx.fam_moved < k) {
            return Some("too_few_feeds");
        }
        if self.cons.is_some_and(|k| cx.cons_move < k) {
            return Some("consensus_did_not_move");
        }
        if self.shock.is_some_and(|k| cx.gap_shock < k) {
            return Some("gap_usual_size");
        }
        if self.follow.is_some_and(|k| cx.jumps == 0 || cx.follow < k) {
            return Some("polymarket_does_not_lag");
        }
        if self.lead.is_some_and(|k| cx.jumps > 0 && cx.lead > k) {
            return Some("polymarket_leads");
        }
        if self.z.is_some_and(|k| cx.z < k) {
            return Some("swingy_consensus");
        }
        if self.gap.zip(gap).is_some_and(|(k, g)| g >= k) {
            return Some("polymarket_already_apart");
        }
        if self.cap && px > 0.90 {
            return Some("price_too_high");
        }
        // `px` is the offer plus the 1c the bot adds when it tests a price.
        if self.weak_cap.is_some_and(|cap| px - SLIP > cap + 1e-9 && stake_factor(cx) < 1.0) {
            return Some("weak_game_dear_price");
        }
        if self.pm_trend.is_some_and(|t| pm_tr < -t) {
            return Some("polymarket_trend_against");
        }
        if self.book_trend.is_some_and(|t| b_tr10 < -t) {
            return Some("book_trend_against");
        }
        if self.rev.is_some_and(|r| b_tr5 < r * bm[1]) {
            return Some("bounce_not_news");
        }
        if self.noise.is_some_and(|k| bm[0] < k * b_rng) {
            return Some("swingy_game");
        }
        None
    }
    /// The stop-loss sale price for a buy at `entry` on a bookmaker move `bm`.
    pub fn stop_px(&self, entry: f64, bm: f64) -> Option<f64> {
        self.stop.map(|k| entry - (k * bm).max(0.04))
    }
}

thread_local! {
    static FLT: std::cell::Cell<Flt> = std::cell::Cell::new(paper_flt());
}

/// The filters the paper record and its closed trades are built with: nothing bought above 90c, no
/// other filter (the per-thread starting value, which `view` and `closed_since` were written for).
fn paper_flt() -> Flt {
    Flt { cap: true, ..Default::default() }
}

/// Run `f` with the paper record's filters, then put back what the thread had. The filters are a
/// per-thread setting that `bot_sim` and the research replays leave behind on the blocking threads
/// they ran on; read as found, the Trade review's list came back empty (23 Nov 2026).
fn with_paper_flt<T>(f: impl FnOnce() -> T) -> T {
    let before = flt();
    FLT.with(|x| x.set(paper_flt()));
    let out = f();
    FLT.with(|x| x.set(before));
    out
}

fn flt() -> Flt {
    FLT.with(|f| f.get())
}

impl Cand {
    /// The price the live gate tests against its limits: the dearer of the two tests' offers
    /// (+1c). Blocks measured before those were kept: the fill price.
    fn gate_px(&self) -> f64 {
        if self.dec[0] > 0.0 { self.dec[0].max(self.dec[1]) } else { self.px - SLIP_BUY + SLIP }
    }
    fn exit(&self, ci: usize, gi: usize) -> Option<Exit> {
        let f = flt();
        if f.stop.is_none() && !f.revert {
            return self.exits[ci][gi];
        }
        let i = EXV.iter().position(|(s, r)| *r == f.revert && s.zip(f.stop).map_or(s.is_none() && f.stop.is_none(), |(a, b)| (a - b).abs() < 1e-9)).unwrap_or(1);
        self.stop_exits[i][ci][gi]
    }
}

/// Early-sale variants the replay prices: (stop-loss share of the move, "reason gone" exit).
pub const EXV: [(Option<f64>, bool); 5] = [(Some(0.25), false), (Some(0.5), false), (Some(1.0), false), (None, true), (Some(0.5), true)];

// ---------------------------------------------------------------- candidates

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
struct Exit {
    ts: Ms,
    /// Sale price per contract before the book (slippage included).
    px: f64,
    /// (contracts at the best price, contracts within 2c) on the side we sell into.
    sell: Option<(f64, f64)>,
    caught: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
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
    /// Price per contract at the best offer when the order matched (plus `SLIP_BUY`).
    px: f64,
    /// (contracts at the best price, contracts within 2c) on our side; None = depth not recorded.
    buy: Option<(f64, f64)>,
    pause: bool,
    /// [catch][give-up]; None = not known yet.
    exits: [[Option<Exit>; 5]; 3],
    /// The same with early sales, per `EXV`.
    stop_exits: [[[Option<Exit>; 5]; 3]; 5],
    /// The move against the whole market and this game's history.
    cx: crate::consensus::Ctx,
    /// Polymarket's mid change in our direction over the 10 min before the move.
    pm_tr: f64,
    /// The trigger bookmaker's change in our direction over 5 min (to the confirm) and over the 10 min before.
    b_tr5: f64,
    b_tr10: f64,
    /// The trigger book's price range over the 10 min before the move.
    b_rng: f64,
    /// The bookmaker update that triggered (the live bot's move time).
    #[serde(default)]
    t0: Ms,
    /// Share of the move Polymarket made since the FIRST bookmaker of this move moved (the live
    /// bot's `followed_since_start`): a slow book updating late is not a fresh move.
    #[serde(default)]
    fol_start: f64,
    /// How long after the first bookmaker of the move this one moved.
    #[serde(default)]
    late_ms: Ms,
    /// The rules can pass this move but the order cannot fill (the price ran before it landed,
    /// nothing rests within 2c, no book): no trade, but the live bot has used the move up and
    /// locked the game, so the replay must see it too.
    #[serde(default)]
    nofill: bool,
    /// The buy falls where the Polymarket recording had a gap (`Seen`): the prices around it are
    /// not what the market showed, so no replay takes this move or lets it lock its game.
    #[serde(default)]
    blind: bool,
    /// A US basketball game (`us_basketball`): those stay on paper.
    #[serde(default)]
    us_bb: bool,
    /// The price tested at each of the two tests (best offer + 1c), as the live gate sees it.
    #[serde(default)]
    dec: [f64; 2],
    /// Polymarket's mid price 10 s before the move (0 = not kept).
    #[serde(default)]
    pre_mid: f64,
}

/// What a candidate is measured for: one outcome of one game moving one way.
struct Mv<'a> {
    match_id: i64,
    sport: &'a str,
    home: &'a str,
    away: &'a str,
    outcome: &'a str,
    direction: i8,
}

fn fee(pm: bool, n: f64, p: f64) -> f64 {
    if pm { crate::venue::pm_fee(crate::venue::Fee::PM_SPORTS, n, p) } else { snipe::kalshi_fee(n, p) }
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

/// May this rule buy past the best price here? Only when the move's expected gain still clears
/// the rule's minimum after paying the 2c: the contracts bought past the best price must be worth
/// buying on their own, not carried by the cheap ones.
fn may_walk(c: &Cand, p: &Policy, ci: usize) -> bool {
    p.walk_book && c.edge[1][ci] - CAP >= p.min_edge - 1e-9
}

/// The trigger bookmaker suspended this price within 2 minutes before the move (a goal,
/// a red card, a break of serve: the book paused, then reopened somewhere else).
pub(crate) fn after_pause(c: &Connection, book: &str, match_id: i64, outcome: &str, t0: Ms) -> bool {
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
fn make_cand(c: &Connection, q: &mut dyn Quotes, e: &Mv, tb: &str, t0: Ms, base: &str, now: Ms, tm: (Ms, Ms)) -> Option<Cand> {
    let pm = base == PM;
    let (mid, o) = (e.match_id, e.outcome);
    let dir = e.direction as f64;
    let (td, td2) = (t0 + tm.0, t0 + tm.0 + tm.1);
    let tmg = crate::timing::get();
    let live = pm && crate::timing::live(c, mid, td2);
    // The buy: the real bot's order leaves `pm_send_lag_ms` after the second test (store lag,
    // measuring, cash claim, signing), then the reply and, on a game playing, the matching delay.
    let fd: Ms = if pm { tmg.pm_send_lag_ms + tmg.pm_match_ms(live) } else { tmg.kalshi_order_ms };
    // A sale: signing, the reply, the matching delay.
    let fs: Ms = if pm { tmg.pm_fill_ms(live) } else { tmg.kalshi_order_ms };
    // Bought shares can only be sold once they have settled (Polymarket).
    let settle: Ms = if pm { tmg.pm_settle_ms } else { 0 };
    let tf = td2 + fd;
    let b_pre = snipe::book_prob(c, tb, mid, o, t0 - snipe::PRE_MS)?;
    let (b1, b2) = (snipe::book_prob(c, tb, mid, o, td)?, snipe::book_prob(c, tb, mid, o, td2)?);
    let (q0, q1, q2) = (q.at(mid, o, t0 - snipe::PRE_MS)?, q.at(mid, o, td)?, q.at(mid, o, td2)?);
    let qf = q.at(mid, o, tf);
    // A gap in the recording up to the second test fails the rules (as live); one between the
    // test and the fill only means no fill is known.
    if snipe::reopened(c, tb, mid, o, t0 - snipe::PRE_MS, td2) || snipe::reopened(c, base, mid, o, t0 - snipe::PRE_MS, td2) {
        return None;
    }
    let gap_fill = snipe::reopened(c, tb, mid, o, td2, tf) || snipe::reopened(c, base, mid, o, td2, tf);
    if !pm && (qf.is_none() || gap_fill) {
        return None;
    }
    if !pm && (q1.3 == "assumed" || q2.3 == "assumed") {
        return None; // Kalshi price unknown at that moment
    }
    let pre_mid = (q0.0 + q0.1) / 2.0;
    let (bm1, bm2) = ((b1 - b_pre) * dir, (b2 - b_pre) * dir);
    let (pm_tr, b_tr5, b_tr10, b_rng) = trends(c, q, tb, mid, o, dir, t0, pre_mid, b_pre, b2);
    let series = crate::consensus::Series::load_for(c, base, mid, o, t0 - 31 * 60_000, tf + GIVE[GIVE.len() - 1] * 1000 + 60_000);
    let cx = series.ctx(dir, t0 - snipe::PRE_MS, td2)?;
    if bm1 < (if pm { LIVE_MIN_MOVE } else { MOVE[0] }) || bm2 < 0.01 {
        return None;
    }
    let fol = |x: &Q, bm: f64| ((x.0 + x.1) / 2.0 - pre_mid) * dir / bm;
    let (f1, f2) = (fol(&q1, bm1), fol(&q2, bm2));
    let (s1, s2) = (q1.1 - q1.0, q2.1 - q2.0);
    let loose_f = if pm { SAFE_MAX_FOLLOWED } else { *FOLLOWED.last().unwrap() };
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
        let target = (pre_mid + dir * bm * catch).clamp(0.0, 1.0);
        let exp = if dir > 0.0 { target - sp / 2.0 - SLIP } else { 1.0 - (target + sp / 2.0) - SLIP };
        let n = REF_STAKE / d;
        (n * (exp - d) - fee(pm, n, d) - fee(pm, n, exp.clamp(0.0, 1.0))) / n
    };
    let e1 = CATCH.map(|k| edge(bm1, s1, d1, k));
    let e2 = CATCH.map(|k| edge(bm2, s2, d2, k));
    if !(0..3).any(|k| e1[k] >= EDGE[0] && e2[k] >= EDGE[0]) {
        return None;
    }
    // What rested on our side when the order landed: buying Yes takes asks, buying No takes Yes
    // bids. Unknown for hours recorded before depth was kept.
    let side = q.depth(mid, o, tf).and_then(|d| if dir > 0.0 { d.ask_size.zip(d.ask_depth) } else { d.bid_size.zip(d.bid_depth) });
    // No fill: no book at the fill, the price ran before it, or not even $10 rests within 2c.
    let nofill = gap_fill || qf.is_none_or(|x| dec(&x) > d2 + CAP || side.is_some_and(|(_, near)| near * (dec(&x) - SLIP + SLIP_BUY) < snipe::MIN_FILL));
    if nofill {
        if !pm {
            return None;
        }
        return Some(Cand {
            match_id: mid,
            sport: e.sport.to_string(),
            home: e.home.to_string(),
            away: e.away.to_string(),
            outcome: e.outcome.to_string(),
            dir: e.direction,
            trigger: tb.to_string(),
            pm,
            tf,
            books: [cx.fam_moved, cx.fam_moved],
            bm: [bm1, bm2],
            fol: [f1, f2],
            sp: [s1, s2],
            edge: [e1, e2],
            px: d2 - SLIP + SLIP_BUY,
            buy: None,
            pause: after_pause(c, tb, mid, o, t0),
            exits: [[None; 5]; 3],
            stop_exits: [[[None; 5]; 3]; 5],
            cx,
            pm_tr,
            b_tr5,
            b_tr10,
            b_rng,
            t0,
            fol_start: f2,
            late_ms: 0,
            nofill: true,
            blind: false,
            us_bb: false,
            dec: [d1, d2],
            pre_mid,
        });
    }
    let qf = qf?;
    let entry0 = dec(&qf) - SLIP + SLIP_BUY;
    // Every sale: catch-up judged on the price we would sell at; else the give-up time.
    let path = q.path(mid, o, tf, tf + GIVE[GIVE.len() - 1] * 1000);
    let mut exits = [[None; 5]; 3];
    let mut stop_exits = [[[None; 5]; 3]; 5];
    // The consensus gave back half its move: the reason for the trade is gone.
    let gone = |t: Ms| series.cons_at(t).is_some_and(|cn| (cn - cx.cons_now) * dir <= -0.5 * cx.cons_move.max(0.01));
    for si in 0..=EXV.len() {
    // si == EXV.len(): no early sale.
    let stop_px = EXV.get(si).and_then(|v| v.0).map(|k| entry0 - (k * bm1).max(0.04));
    let revert = EXV.get(si).is_some_and(|v| v.1);
    for (ci, &k) in CATCH.iter().enumerate() {
        for (gi, &g) in GIVE.iter().enumerate() {
            let give = tf + g * 1000;
            let sellable = |x: &Q| if dir > 0.0 { x.0 } else { 1.0 - x.1 };
            // The bot looks at the book once a second from its buy and sells on what it sees
            // then (never before the shares have settled): a catch-up that is gone again by the
            // next look is missed, as it is live.
            let tick = |t: Ms| tf + (t - tf + 999).div_euclid(1000) * 1000;
            let state_at = |t: Ms| {
                let i = path.partition_point(|x| x.2 <= t);
                if i == 0 { qf } else { path[i - 1] }
            };
            let first = tick(tf + settle);
            let caught_at = |x: &Q| snipe::caught_up(x, dir, pre_mid, k * bm1, s1) || sellable(x) >= 0.99;
            let hit = std::iter::once(first)
                .chain(path.iter().filter(|x| x.2 > first).map(|x| tick(x.2)))
                .take_while(|t| *t <= give)
                .find_map(|t| {
                    let x = state_at(t);
                    let c = caught_at(&x);
                    (c || stop_px.is_some_and(|sp| sellable(&x) <= sp) || (revert && gone(t))).then_some((t, c))
                });
            let (t_sell, caught) = match hit {
                Some((t, c)) => (t, c),
                None if give + fs > now => continue, // still open
                None => (give, false),
            };
            if t_sell + fs > now {
                continue;
            }
            // The sale. With no two-sided book at that moment (the game is over, or one side is
            // empty) the live bot keeps looking, and books the result once the price shows one:
            // these are the trades that lose the whole stake, and they count.
            let (ts_x, px, booked) = match q.at(mid, o, t_sell + fs) {
                Some(qs) => (t_sell + fs, (if dir > 0.0 { qs.0 } else { 1.0 - qs.1 }) - SLIP, false),
                None => match after_book(q, mid, o, dir, t_sell + fs, now) {
                    Some(x) => x,
                    None => continue,
                },
            };
            // Selling Yes hits bids; selling No hits Yes asks.
            let sell = if side.is_some() && !booked {
                q.depth(mid, o, ts_x).and_then(|d| if dir > 0.0 { d.bid_size.zip(d.bid_depth) } else { d.ask_size.zip(d.ask_depth) })
            } else {
                None
            };
            let x = Some(Exit { ts: ts_x, px, sell, caught });
            if si < EXV.len() { stop_exits[si][ci][gi] = x } else { exits[ci][gi] = x }
        }
    }
    }
    Some(Cand {
        match_id: mid,
        sport: e.sport.to_string(),
        home: e.home.to_string(),
        away: e.away.to_string(),
        outcome: e.outcome.to_string(),
        dir: e.direction,
        trigger: tb.to_string(),
        pm,
        tf,
        // Independent bookmakers = feed families that moved (the live bot counts the same way).
        books: [cx.fam_moved, cx.fam_moved],
        bm: [bm1, bm2],
        fol: [f1, f2],
        sp: [s1, s2],
        edge: [e1, e2],
        px: entry0,
        buy: side,
        pause: after_pause(c, tb, mid, o, t0),
        exits,
        stop_exits,
        cx,
        pm_tr,
        b_tr5,
        b_tr10,
        b_rng,
        t0,
        fol_start: f2,
        late_ms: 0,
        nofill: false,
        blind: false,
        us_bb: false,
        dec: [d1, d2],
        pre_mid,
    })
}

/// A position whose book is gone or one-sided at `t`: (when, price per contract, booked?) of what
/// the live bot ends up with. The next two-sided book it can sell into; or what the one side
/// still there pays; or the result (1 or 0) once the last recorded price shows one (97c+ / 3c-,
/// as snipebot::settled). None = not known (yet).
fn after_book(q: &mut dyn Quotes, mid: i64, o: &str, dir: f64, t: Ms, now: Ms) -> Option<(Ms, f64, bool)> {
    let end = (t + AFTER_BOOK_MS).min(now);
    let mut t = t;
    while t <= end {
        if let Some(x) = q.at(mid, o, t) {
            return Some((t, (if dir > 0.0 { x.0 } else { 1.0 - x.1 }) - SLIP, false));
        }
        if let Some((b, a, ts)) = q.raw(mid, o, t) {
            if b.is_some_and(|b| b >= 0.97) {
                return Some((t, if dir > 0.0 { 1.0 } else { 0.0 }, true));
            }
            if a.is_some_and(|a| a <= 0.03) {
                return Some((t, if dir > 0.0 { 0.0 } else { 1.0 }, true));
            }
            // One side only: sell into it if it is ours to sell into.
            let s = if dir > 0.0 { b } else { a.map(|a| 1.0 - a) };
            if let Some(s) = s.filter(|s| *s > 0.0 && *s < 1.0 && t - ts <= 30 * 60_000) {
                return Some((t, (s - SLIP).max(0.0), true));
            }
        }
        t += 5_000;
    }
    None
}

/// Trend features shared with the live bot: (Polymarket's 10-min trend, the trigger book's
/// 5-min net move, the trigger book's 10-min trend before, its range over those 10 min), the
/// trends in our direction.
pub(crate) fn trends(c: &Connection, q: &mut dyn Quotes, tb: &str, mid: i64, o: &str, dir: f64, t0: Ms, pre_mid: f64, b_pre: f64, b_now: f64) -> (f64, f64, f64, f64) {
    let pm_tr = q.at(mid, o, t0 - 600_000).map(|x| (pre_mid - (x.0 + x.1) / 2.0) * dir).unwrap_or(0.0);
    // A bookmaker with no price at some moment has none at any earlier moment either, so the
    // first "no price" ends the looking back. The numbers are the same; a bookmaker that listed
    // the game minutes ago no longer costs twenty more searches through the game's history.
    let b5 = snipe::book_prob(c, tb, mid, o, t0 - 300_000);
    let b10 = if b5.is_some() { snipe::book_prob(c, tb, mid, o, t0 - 600_000) } else { None };
    let b_tr5 = b5.map(|b| (b_now - b) * dir).unwrap_or(b_now - b_pre);
    let b_tr10 = b10.map(|b| (b_pre - b) * dir).unwrap_or(0.0);
    // Range: the book's price every 30 s over the 10 min before the move window.
    let (mut lo, mut hi) = (b_pre, b_pre);
    for k in 1..=20 {
        let Some(b) = snipe::book_prob(c, tb, mid, o, t0 - snipe::PRE_MS - k * 30_000) else { break };
        lo = lo.min(b);
        hi = hi.max(b);
    }
    (pm_tr, b_tr5, b_tr10, hi - lo)
}

/// Every move in [start, end) for both markets.
/// When the Polymarket recording was receiving nothing. On 17-18 Nov 2026 the bot's own feed ran
/// 5-30 s late and whole stretches of quotes never arrived: by Polymarket's clock NO game has a
/// quote for 30-60 s. A move measured inside such a stretch "buys" the last price before it and
/// "sells" the first one after it. Of the 70 trades the bot's rules took in the replay of
/// 17-20 Nov, the 37 inside a stretch made +$103 and the 33 outside made +$5
/// (tools/hole_check2.py, tools/tells_final.py). Reading quotes by Polymarket's own clock does
/// not help: the quotes are not late, they are missing.
struct Seen {
    /// Polymarket's own time of every recorded quote of every game, sorted.
    own: Vec<Ms>,
}

impl Seen {
    fn load(c: &Connection) -> Seen {
        let mut own: Vec<Ms> = Vec::new();
        if let Ok(mut st) = c.prepare("SELECT CASE WHEN server_ts_ms > 0 THEN server_ts_ms ELSE ts_ms END FROM pm_quotes") {
            if let Ok(rows) = st.query_map([], |r| r.get::<_, Ms>(0)) {
                own.extend(rows.flatten());
            }
        }
        own.sort_unstable();
        Seen { own }
    }
    /// How long the whole feed was silent around `t`: from the last quote at or before it to the
    /// first one after it (to `t` itself when nothing came after yet). Nothing recorded before
    /// `t` at all = unknown, counted as silent.
    fn silent(&self, t: Ms) -> Ms {
        let i = self.own.partition_point(|x| *x <= t);
        match (i, self.own.get(i)) {
            (0, _) => Ms::MAX,
            (_, Some(next)) => next - self.own[i - 1],
            (_, None) => t - self.own[i - 1],
        }
    }
}

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
    // Everything this block reads, copied into memory in one sequential pass: the database is
    // bigger than this machine's free memory, so random reads on disk made the build crawl.
    let c = mem_copy(db, start - MEM_BEFORE_MS, end + MEM_AFTER_MS)?;
    let seen = Seen::load(&c);
    // The leagues a game is listed under live in the full database (the copy has Kalshi's only).
    let real = crate::store::open_ro(db).ok();
    let mut us_bb: HashMap<i64, bool> = HashMap::new();
    let mut out: [Vec<Cand>; 2] = [Vec::new(), Vec::new()];
    // Polymarket: the moves the LIVE bot triggers on (every bookmaker update that jumps), measured
    // by the same steps, so the replay and the bot look at the same things.
    {
        let mut q = snipe::pm_quotes(&c);
        let mut names: HashMap<i64, (String, String, String)> = HashMap::new();
        for mv in live_moves(&c, start, end)? {
            let (sport, home, away) = names
                .entry(mv.match_id)
                .or_insert_with(|| c.query_row("SELECT sport, home, away FROM matches WHERE id = ?1", [mv.match_id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap_or_default())
                .clone();
            let e = Mv { match_id: mv.match_id, sport: &sport, home: &home, away: &away, outcome: mv.outcome, direction: mv.dir };
            let Some(mut cd) = make_cand(&c, &mut *q, &e, &mv.book, mv.t0, PM, now, tm) else { continue };
            cd.late_ms = mv.t0 - mv.start;
            cd.blind = seen.silent(cd.tf) > HOLE_MS;
            if sport == "basketball" {
                cd.us_bb = *us_bb.entry(mv.match_id).or_insert_with(|| real.as_ref().is_some_and(|r| us_basketball(r, mv.match_id, &home, &away)));
            }
            if mv.start < mv.t0 {
                // As snipebot::measure: Polymarket's move since 10 s before the first bookmaker.
                if let (Some(qs), Some(q2)) = (q.at(mv.match_id, mv.outcome, mv.start - snipe::PRE_MS), q.at(mv.match_id, mv.outcome, mv.t0 + tm.0 + tm.1)) {
                    cd.fol_start = ((q2.0 + q2.1) / 2.0 - (qs.0 + qs.1) / 2.0) * mv.dir as f64 / cd.bm[1];
                }
            }
            out[0].push(cd);
        }
        out[0].sort_by_key(|x| x.tf);
    }
    // Kalshi is not traded; its candidates are measured for research only (one per move, at the
    // first bookmaker, as before).
    {
        let r = crate::rank::compute_with(&c, &p)?;
        let mut q = snipe::kalshi_quotes(&c);
        for e in &r.events {
            if !(e.order.iter().any(|x| x.0 == KALSHI) || e.missed.iter().any(|m| m == KALSHI)) {
                continue;
            }
            let (Some(tb), Some(t0)) = (e.first_book.clone(), e.first_book_ms) else { continue };
            let mv = Mv { match_id: e.match_id, sport: &e.sport, home: &e.home, away: &e.away, outcome: &e.outcome, direction: e.direction };
            if let Some(cd) = make_cand(&c, &mut *q, &mv, &tb, t0, KALSHI, now, tm) {
                out[1].push(cd);
            }
        }
        out[1].sort_by_key(|x| x.tf);
    }
    Ok(out)
}

/// One bookmaker update the live bot triggers on and measures.
struct LiveMove {
    match_id: i64,
    outcome: &'static str,
    dir: i8,
    book: String,
    t0: Ms,
    /// When the first bookmaker of this move moved (`t0` itself when this one is the first).
    start: Ms,
}

/// The live bot's triggers in [start, end), as snipebot::on_change + the first steps of
/// snipebot::measure find them: every bookmaker update 4+ points from that bookmaker's previous
/// price, still moved 1 s and 1.5 s later, with the start of its move (the first such update the
/// same way within MOVE_MS). Kept when the move is big enough for a rule and two feed families
/// moved (every rule set needs both); the rest is measured by `make_cand`.
fn live_moves(c: &Connection, start: Ms, end: Ms) -> anyhow::Result<Vec<LiveMove>> {
    const OUTS: [&str; 3] = ["home", "away", "draw"];
    const WARM_MS: Ms = 10 * 60_000;
    const MAX_AGE_MS: Ms = 20 * 60_000;
    let mut books: Vec<String> = Vec::new();
    let mut book_id: HashMap<String, u16> = HashMap::new();
    // (match, outcome, book) -> that book's real prices as probabilities, in time order.
    let mut ser: HashMap<(i64, u8, u16), Vec<(Ms, f64)>> = HashMap::new();
    let mut by_mo: HashMap<(i64, u8), Vec<u16>> = HashMap::new();
    let mut ev: Vec<(Ms, i64, u8, u16, f64)> = Vec::new();
    {
        let mut st = c.prepare("SELECT ts_ms, book, match_id, outcome, new_price FROM changes WHERE new_price IS NOT NULL AND match_id > 0 AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms, id")?;
        let mut rows = st.query([])?;
        while let Some(r) = rows.next()? {
            let (ts, book, m, o, p): (Ms, String, i64, String, f64) = (r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?);
            let Some(oi) = OUTS.iter().position(|x| *x == o) else { continue };
            let b = match book_id.get(&book) {
                Some(b) => *b,
                None => {
                    let b = books.len() as u16;
                    books.push(book.clone());
                    book_id.insert(book, b);
                    b
                }
            };
            let v = ser.entry((m, oi as u8, b)).or_insert_with(|| {
                by_mo.entry((m, oi as u8)).or_default().push(b);
                Vec::new()
            });
            v.push((ts, 1.0 / p.max(1.0001)));
            if p > 1.0 {
                ev.push((ts, m, oi as u8, b, 1.0 / p));
            }
        }
    }
    let at = |v: &[(Ms, f64)], t: Ms| -> Option<(Ms, f64)> {
        let i = v.partition_point(|x| x.0 <= t);
        (i > 0).then(|| v[i - 1])
    };
    // Each feed family's price at `t` (mean of its books with a fresh price), as consensus.rs.
    let fams = |m: i64, o: u8, t: Ms| -> BTreeMap<&str, f64> {
        let mut acc: BTreeMap<&str, (f64, usize)> = BTreeMap::new();
        for b in by_mo.get(&(m, o)).map(|v| v.as_slice()).unwrap_or(&[]) {
            if let Some((ts, p)) = at(&ser[&(m, o, *b)], t) {
                if t - ts <= MAX_AGE_MS {
                    let e = acc.entry(snipe::platform(&books[*b as usize])).or_default();
                    e.0 += p;
                    e.1 += 1;
                }
            }
        }
        acc.into_iter().map(|(k, (s, n))| (k, s / n as f64)).collect()
    };
    let mut last: HashMap<(i64, u8, u16), f64> = HashMap::new();
    let mut starts: HashMap<(i64, u8, bool), Ms> = HashMap::new();
    let mut out = Vec::new();
    for &(ts, m, o, b, p) in &ev {
        let Some(prev) = last.insert((m, o, b), p) else { continue };
        if (p - prev).abs() < TRIGGER_JUMP || ts < start - WARM_MS || ts >= end {
            continue;
        }
        let dir: f64 = if p > prev { 1.0 } else { -1.0 };
        let s = &ser[&(m, o, b)];
        let (pre, td, td2) = (ts - snipe::PRE_MS, ts + REACTION, ts + REACTION + CONFIRM);
        let (Some(b0), Some(b1), Some(b2)) = (at(s, pre), at(s, td), at(s, td2)) else { continue };
        let (bm1, bm2) = ((b1.1 - b0.1) * dir, (b2.1 - b0.1) * dir);
        if bm1 < 0.01 || bm2 < 0.01 {
            continue; // the book went back
        }
        let e = starts.entry((m, o, dir > 0.0)).or_insert(ts);
        if ts - *e > MOVE_MS || *e - ts > MOVE_MS || ts < *e {
            *e = ts;
        }
        let first = *e;
        if ts < start || bm1 < LIVE_MIN_MOVE {
            continue;
        }
        let (f_pre, f_now) = (fams(m, o, pre), fams(m, o, td2));
        if f_now.iter().filter(|(k, v)| f_pre.get(*k).is_some_and(|a| (*v - a) * dir >= 0.02)).count() < 2 {
            continue;
        }
        out.push(LiveMove { match_id: m, outcome: OUTS[o as usize], dir: dir as i8, book: books[b as usize].clone(), t0: ts, start: first });
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
    let mut busy: HashMap<i64, Ms> = HashMap::new();
    for (k, c) in cands[lo..].iter().enumerate() {
        let c: &'a Cand = *c;
        if c.tf >= to {
            break;
        }
        // Measured inside a gap in the recording: nothing about it is known.
        if c.blind {
            continue;
        }
        if c.books[0] < p.min_books
            || c.books[1] < p.min_books
            || c.bm[0] < p.min_move
            || c.fol[0] > p.max_followed
            || c.fol[1] > p.max_followed
            || c.fol_start > p.max_followed
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
        // The order cannot fill: no trade, but the live bot has taken the move and locked the game.
        if c.nofill {
            recent.push_back((c.match_id, c.tf));
            continue;
        }
        let Some(x) = c.exit(ci, gi) else { continue };
        if x.ts > known_by {
            continue;
        }
        // One open position a game, as the live bot; the move is used up either way.
        if busy.get(&c.match_id).is_some_and(|t| *t > c.tf) {
            recent.push_back((c.match_id, c.tf));
            continue;
        }
        if take(lo + k, c, &x) {
            recent.push_back((c.match_id, c.tf));
            busy.insert(c.match_id, x.ts);
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
                        if let Some(x) = &c.exit(ci, gi) {
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
    run_policy(cands, &p, from, to, known_by, |k, c, _| match pr[k][may_walk(c, &p, ci) as usize][ci][gi] {
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
            let stake = (bank.pct * (bank.start + closed) * stake_factor(&c.cx)).min(cash);
            if stake < snipe::MIN_FILL {
                return false; // broke, or all cash in open trades
            }
            let Some(f) = fill(c, x, stake, may_walk(c, p, sale_idx(p).0)) else { return false };
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

/// 4-hour blocks measured at once (each on its own in-memory copy, ~0.3-0.5 GB each).
const BUILD_THREADS: usize = 1;
/// The in-memory copy of a block: prices from this long before it (a book's "last price"
/// further back is not seen) to this long after it (exits, consensus after the fill).
const MEM_BEFORE_MS: Ms = 2 * 3_600_000;
const MEM_AFTER_MS: Ms = 45 * 60_000;

/// The first row id of `table` at or after `t` (ids grow with time; bisection on the key).
fn id_at(d: &Connection, table: &str, t: Ms) -> anyhow::Result<i64> {
    let max: i64 = d.query_row(&format!("SELECT COALESCE(MAX(id), 0) FROM {table}"), [], |r| r.get(0))?;
    let ts_of = |id: i64| -> Option<Ms> { d.query_row(&format!("SELECT ts_ms FROM {table} WHERE id >= ?1 ORDER BY id LIMIT 1"), [id], |r| r.get(0)).ok() };
    let (mut lo, mut hi) = (1i64, max + 1);
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        match ts_of(mid) {
            Some(ts) if ts < t => lo = mid + 1,
            _ => hi = mid,
        }
    }
    Ok(lo)
}

/// An in-memory database with the 1x2 price changes and Polymarket quotes of [lo, hi] (read by
/// row-id range, i.e. sequentially) and the matches table, indexed like the real one.
fn mem_copy(db: &std::path::Path, lo: Ms, hi: Ms) -> anyhow::Result<Connection> {
    let d = crate::store::open_ro(db)?;
    // A little slack: ids are only roughly in time order.
    let (c0, c1) = (id_at(&d, "changes", lo - 60_000)?, id_at(&d, "changes", hi + 60_000)?);
    let (q0, q1) = (id_at(&d, "pm_quotes", lo - 60_000)?, id_at(&d, "pm_quotes", hi + 60_000)?);
    drop(d);
    let m = Connection::open_with_flags(":memory:", rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_CREATE | rusqlite::OpenFlags::SQLITE_OPEN_URI)?;
    let uri = format!("file:{}?mode=ro", db.display());
    m.execute("ATTACH DATABASE ?1 AS d", [uri])?;
    m.execute_batch("PRAGMA d.mmap_size = 8589934592;")?;
    m.execute("CREATE TABLE changes AS SELECT id, ts_ms, book, match_id, market, line, outcome, kind, old_price, new_price, book_ts_ms FROM d.changes WHERE id >= ?1 AND id < ?2 AND market = '1x2' AND ts_ms BETWEEN ?3 AND ?4", params![c0, c1, lo, hi])?;
    m.execute("CREATE TABLE pm_quotes AS SELECT * FROM d.pm_quotes WHERE id >= ?1 AND id < ?2 AND ts_ms BETWEEN ?3 AND ?4", params![q0, q1, lo, hi])?;
    // Kalshi: its quotes (candle backfills are not in id order: filter on time) and event links.
    m.execute("CREATE TABLE kalshi_quotes AS SELECT * FROM d.kalshi_quotes WHERE ts_ms BETWEEN ?1 AND ?2", params![lo, hi])?;
    m.execute_batch(
        "CREATE TABLE book_events AS SELECT * FROM d.book_events WHERE book = 'kalshi';
         CREATE INDEX kalshi_quotes_ev ON kalshi_quotes(event, outcome, ts_ms);
         CREATE INDEX book_events_match ON book_events(match_id);
         CREATE TABLE matches AS SELECT * FROM d.matches;
         DETACH DATABASE d;
         CREATE INDEX changes_ts ON changes(ts_ms);
         CREATE INDEX changes_match ON changes(match_id, ts_ms);
         CREATE INDEX pm_quotes_match ON pm_quotes(match_id, ts_ms);
         CREATE INDEX matches_id ON matches(id);",
    )?;
    Ok(m)
}

fn cache_dir(app: &App) -> PathBuf {
    app.db_path.parent().map(|d| d.join("adaptive_cache")).unwrap_or_else(|| PathBuf::from("adaptive_cache"))
}

/// A finished block from the disk cache, if it was measured by this method version.
fn load_chunk(app: &App, start: Ms) -> Option<[Vec<Cand>; 2]> {
    let t = std::fs::read(cache_dir(app).join(format!("v{CAND_VERSION}-{start}.json"))).ok()?;
    serde_json::from_slice(&t).ok()
}

fn save_chunk(app: &App, start: Ms, b: &[Vec<Cand>; 2]) {
    let d = cache_dir(app);
    let _ = std::fs::create_dir_all(&d);
    // Blocks of older method versions are useless now.
    if let Ok(rd) = std::fs::read_dir(&d) {
        for e in rd.flatten() {
            if !e.file_name().to_string_lossy().starts_with(&format!("v{CAND_VERSION}-")) {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }
    if let Ok(t) = serde_json::to_vec(b) {
        let tmp = d.join(format!("v{CAND_VERSION}-{start}.tmp"));
        if std::fs::write(&tmp, t).is_ok() {
            let _ = std::fs::rename(tmp, d.join(format!("v{CAND_VERSION}-{start}.json")));
        }
    }
}

/// Candidate references for one market, sorted by fill time.
/// Live rule `cap`: nothing bought above 90c (no room to gain, whole stake at risk).
fn all_cands(chunks: &[[Arc<Vec<Cand>>; 2]], bi: usize) -> Vec<&Cand> {
    let f = flt();
    let mut v: Vec<&Cand> = chunks.iter().flat_map(|c| c[bi].iter()).filter(|c| f.keep(c)).collect();
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
    FLT.with(|x| x.set(Flt::live()));
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
    // ---- chunks: finished 4-hour blocks come from the disk cache (same method version); the
    // rest are measured, several blocks at a time.
    let mut todo: Vec<Ms> = Vec::new();
    let mut start = boundary(from);
    while start < now {
        let need = {
            let st = ST.lock();
            match st.chunks.get(&start) {
                None => true,
                Some(ch) => !ch.complete && now - ch.built_ms >= REBUILD_MS,
            }
        };
        if need {
            let complete = start + EVERY_MS + EXIT_HORIZON_MS <= now;
            match complete.then(|| load_chunk(app, start)).flatten() {
                Some(c) => {
                    ST.lock().chunks.insert(start, Chunk { built_ms: now, complete: true, c: c.map(Arc::new) });
                }
                None => todo.push(start),
            }
        }
        start += EVERY_MS;
    }
    // While real money is on, nothing is measured and no rules are picked: a block build keeps the
    // disk busy and the rule search the processor for minutes, and the bot's decisions then come
    // late and are dropped (19 Nov: 10% of them). The traded rules are pinned, so the replay can
    // wait; it catches up as soon as real money is off.
    if crate::bots::mode(crate::bots::SNIPE) == Some(true) {
        ST.lock().status = if todo.is_empty() { "paused while real money is on".to_string() } else { format!("paused while real money is on: {} blocks wait to be measured (moves inside gaps in the recording are now left out)", todo.len()) };
        return Ok(());
    }
    for batch in todo.chunks(BUILD_THREADS) {
        ST.lock().status = format!("measuring moves {} UTC ({} blocks left)", fmt_utc(batch[0]), todo.len());
        let built: Vec<(Ms, anyhow::Result<[Vec<Cand>; 2]>)> = std::thread::scope(|sc| {
            let hs: Vec<_> = batch.iter().map(|&st| (st, sc.spawn(move || build_chunk(&app.db_path, st, (st + EVERY_MS).min(now + 1), now)))).collect();
            hs.into_iter().map(|(st, h)| (st, h.join().unwrap_or_else(|_| Err(anyhow::anyhow!("build thread panicked"))))).collect()
        });
        for (st, r) in built {
            let b = r?;
            let complete = st + EVERY_MS + EXIT_HORIZON_MS <= now;
            if complete {
                save_chunk(app, st, &b);
            }
            ST.lock().chunks.insert(st, Chunk { built_ms: now, complete, c: b.map(Arc::new) });
        }
    }
    // ---- re-choices due: every boundary from 8 h after the data starts
    let first = boundary(from + FIRST_AFTER_MS) + EVERY_MS;
    let mut t = first;
    let chunks = snapshot();
    while t <= boundary(now) {
        for (bi, base) in BASES.iter().enumerate() {
            if !TRADED[bi] {
                continue; // research only: no rules chosen
            }
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
        "stake": t.stake, "filled": f.filled, "reason": if x.caught { "caught up" } else { "gave up" }, "after_pause": c.pause,
        "move": c.bm[0], "books": c.books[0], "fol": c.fol[0], "pm_tr": c.pm_tr, "b_tr5": c.b_tr5, "b_tr10": c.b_tr10, "b_rng": c.b_rng, "px": c.px,
        "t0": c.t0, "spread": c.sp[0]})
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
/// The paper bot's page (Snipe window), with the paper record's filters whatever the thread was left with.
pub fn view(app: &App) -> Value {
    with_paper_flt(|| view_inner(app))
}

fn view_inner(_app: &App) -> Value {
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
                "candidates": cands.len(), "depth_known": depth_known, "traded": TRADED[bi]})
        })
        .collect();
    let next = boundary(now) + EVERY_MS;
    json!({"now_ms": now, "status": status, "error": error, "data_from": from, "chunks": nchunks,
        "first_run_ms": from.map(|f| boundary(f + FIRST_AFTER_MS) + EVERY_MS), "next_run_ms": next,
        "every_ms": EVERY_MS, "min_trades": MIN_N, "version": VERSION, "grid": GRID, "bank": bank, "bases": bases})
}

/// Whole-dataset simulation of the live snipe bot with its Bots-page settings: every recorded
/// Polymarket move, the bot's own sizing (share of its balance, x edge when on, per-trade and
/// open-money caps, its own cash), fills on the recorded book. `walk_forward`: each 4-hour window
/// trades the rules picked before it (defaults before the first pick) = what the bot would have
/// done; else today's rules on all of it (in-sample). `one_book`: the 1-bookmaker shadow.
pub fn bot_sim(cfg: &crate::bots::SnipeCfg, walk_forward: bool, one_book: bool, f: Flt) -> Value {
    FLT.with(|x| x.set(f));
    let now = now_ms();
    let chunks = snapshot();
    let (runs, warm) = {
        let st = ST.lock();
        (st.runs.clone(), st.warm)
    };
    if !warm {
        return json!({"building": true});
    }
    let cands = all_cands(&chunks, 0);
    let pm_runs: Vec<&Run> = runs.iter().filter(|r| r.base == PM).collect();
    let def = policy(&default_idx(PM));
    let mut segs: Vec<(Policy, Ms, Ms)> = if walk_forward {
        let first = pm_runs.first().map(|r| r.ts).unwrap_or(now + 1);
        let mut v = vec![(def, 0, first)];
        v.extend(segments(&pm_runs, now + 1, |r| r.active));
        v
    } else {
        vec![(active_rules(), 0, now + 1)]
    };
    if one_book {
        for s in &mut segs {
            s.0.min_books = 1;
        }
    }
    for s in &mut segs {
        if cfg.give_up_s > 0 {
            s.0.give_up_s = cfg.give_up_s;
        }
        s.0.max_followed = s.0.max_followed.min(SAFE_MAX_FOLLOWED);
        s.0.min_move = s.0.min_move.max(LIVE_MIN_MOVE);
    }
    // Every trade either set would take, then one wallet in time order. The safe set first; the
    // wider set only where the safe set did not take the move (2 minutes per match).
    let mut picks: Vec<(&Cand, Exit, bool, f64, bool)> = Vec::new(); // (candidate, sale, wider set?, edge, walk)
    for (p, from, to) in &segs {
        let (ci, _) = sale_idx(p);
        run_policy(&cands, p, *from, *to, now, |_, c, x| {
            picks.push((c, *x, false, c.edge[1][ci], may_walk(c, p, ci)));
            true
        });
    }
    let n_safe = picks.len();
    let cands_b: Vec<&Cand> = if cfg.b_enabled && !one_book {
        FLT.with(|x| x.set(Flt { hours: f.hours, ..Flt::wider() }));
        let v = all_cands(&chunks, 0);
        FLT.with(|x| x.set(f));
        v
    } else {
        Vec::new()
    };
    if !cands_b.is_empty() {
        let pb = policy_b();
        let (ci, _) = sale_idx(&pb);
        let mut extra = Vec::new();
        run_policy(&cands_b, &pb, 0, now + 1, now, |_, c, x| {
            let taken = picks[..n_safe].iter().any(|s| s.0.match_id == c.match_id && (s.0.tf - c.tf).abs() < snipe::SAME_MOVE_MS);
            if !taken {
                extra.push((c, *x, true, c.edge[1][ci], may_walk(c, &pb, ci)));
            }
            !taken
        });
        picks.extend(extra);
    }
    picks.sort_by_key(|p| p.0.tf);
    let start = cfg.c.start;
    let (mut cash, mut closed) = (start, 0.0);
    let mut open: Vec<(Ms, f64, f64, i64)> = Vec::new();
    // (fill ts, exit ts, pnl, spent, sport, reason)
    let mut done: Vec<(Ms, Ms, f64, f64, String, bool)> = Vec::new();
    let mut rows: Vec<Value> = Vec::new();
    // The wider set's own daily loss stop: (UTC day, result so far that day).
    let mut b_day: (Ms, f64) = (0, 0.0);
    let mut b_open: Vec<(Ms, f64)> = Vec::new();
    for (c, x, wider, edge, walk) in picks {
        open.retain(|o| {
            if o.0 <= c.tf {
                cash += o.1 + o.2;
                closed += o.2;
                false
            } else {
                true
            }
        });
        let day = c.tf - c.tf.rem_euclid(86_400_000);
        if b_day.0 != day {
            b_day = (day, 0.0);
        }
        b_open.retain(|o| {
            if o.0 <= c.tf {
                if o.0 >= day {
                    b_day.1 += o.1;
                }
                false
            } else {
                true
            }
        });
        // One open position a game, whichever set bought it (as the live bot).
        if open.iter().any(|o| o.3 == c.match_id) {
            continue;
        }
        let open_cost: f64 = open.iter().map(|o| o.1).sum();
        let stake = if wider {
            if cfg.b_daily_loss > 0.0 && b_day.1 <= -cfg.b_daily_loss {
                continue; // the wider set is done for the day
            }
            (cfg.b_pct * (start + closed)).min(cfg.b_max_trade)
        } else {
            let k = if cfg.edge_sizing { (edge / 0.04).clamp(0.5, 3.0) } else { 1.0 };
            (cfg.pct * (start + closed) * k).min(cfg.c.max_trade) * stake_factor(&c.cx)
        }
        .min(cash)
        .min(cfg.c.max_open - open_cost);
        if stake < snipe::MIN_FILL {
            continue;
        }
        let Some(fl) = fill(c, &x, stake, walk) else { continue };
        cash -= fl.spent;
        open.push((x.ts, fl.spent, fl.pnl, c.match_id));
        if wider {
            b_open.push((x.ts, fl.pnl));
        }
        done.push((c.tf, x.ts, fl.pnl, fl.spent, c.sport.clone(), x.caught));
        rows.push(json!({"t": c.tf, "x": x.ts, "pnl": fl.pnl, "spent": fl.spent, "sport": c.sport, "tr": c.trigger, "caught": x.caught, "px": c.px, "entry": fl.entry, "stake": stake, "filled": fl.filled, "exit": fl.exit, "set": if wider { "wider" } else { "safe" },
            "bm": c.bm[0], "k": c.books[0], "fol": c.fol[0], "pm_tr": c.pm_tr, "b_tr5": c.b_tr5, "b_tr10": c.b_tr10, "b_rng": c.b_rng, "cx": c.cx, "m": c.match_id, "home": c.home, "o": c.outcome, "t0": c.t0, "fol_start": c.fol_start, "late_ms": c.late_ms}));
    }
    let mut out = sim_summary(start, cands.first().map(|c| c.tf), now, done.iter().map(|d| (d.0, d.1, d.2, d.3, d.4.as_str(), d.5)).collect());
    out["list"] = json!(rows);
    out
}

/// Why a rule and its filters pass over a measured move (None = they take it).
fn why_not(c: &Cand, p: &Policy, f: &Flt) -> Option<&'static str> {
    let (ci, _) = sale_idx(p);
    if c.bm[0] < p.min_move {
        return Some("move_too_small");
    }
    if c.books[0] < p.min_books || c.books[1] < p.min_books {
        return Some("too_few_books");
    }
    if c.fol[0] > p.max_followed || c.fol[1] > p.max_followed {
        return Some("already_moved");
    }
    if c.fol_start > p.max_followed {
        return Some("moved_with_first_book");
    }
    if c.sp[0] > p.max_spread + 1e-9 || c.sp[1] > p.max_spread + 1e-9 {
        return Some("spread_too_wide");
    }
    if let Some(why) = f.for_game(&c.sport, c.us_bb, c.sp[0].max(c.sp[1])).check(&c.sport, &c.trigger, c.tf, c.gate_px(), c.bm, [c.pm_tr, c.b_tr5, c.b_tr10, c.b_rng], &c.cx, gap_before(&c.cx, c.dir as f64, c.pre_mid)) {
        return Some(why);
    }
    if c.edge[0][ci] < p.min_edge || c.edge[1][ci] < p.min_edge {
        return Some("gap_too_small");
    }
    if p.skip_after_pause && c.pause {
        return Some("after_pause");
    }
    None
}

/// The live bot against the replay over the same hours: is the replay telling the truth about
/// what the bot does? Every live trade (paper or real) since `from` is paired with the replay's
/// trade on the same move (same game, within 30 s): buy price, sale price and return side by
/// side. Trades only one side took are listed with the reason: for a live-only trade, the rule
/// the replay failed it on (or that the replay never measured the move); for a replay-only
/// trade, what the live bot logged for that move (`bot_signals`).
pub fn parity(db: &std::path::Path, cfg: &crate::bots::SnipeCfg, real: bool, from: Ms) -> Value {
    // This thread's filter goes back to what it was when the comparison is done.
    let before = flt();
    let out = parity_inner(db, cfg, real, from);
    FLT.with(|x| x.set(before));
    out
}

fn parity_inner(db: &std::path::Path, cfg: &crate::bots::SnipeCfg, real: bool, from: Ms) -> Value {
    let sim = bot_sim(cfg, true, false, Flt::live());
    if sim["building"].as_bool() == Some(true) {
        return sim;
    }
    let now = now_ms();
    let Ok(c) = crate::store::open_ro(db) else { return json!({"error": "database"}) };
    let live: Vec<(i64, Ms, i64, Value)> = c
        .prepare("SELECT id, ts_ms, match_id, data FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND ts_ms >= ?2 ORDER BY ts_ms")
        .and_then(|mut st| {
            st.query_map(params![real as i64, from], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Ms>(1)?, r.get::<_, i64>(2)?, r.get::<_, String>(3)?)))
                .map(|it| it.filter_map(|x| x.ok()).filter_map(|(a, b, m, d)| Some((a, b, m, serde_json::from_str(&d).ok()?))).collect())
        })
        .unwrap_or_default();
    let rows: Vec<&Value> = sim["list"].as_array().map(|a| a.iter().filter(|r| r["t"].as_i64().unwrap_or(0) >= from).collect()).unwrap_or_default();
    // Every measured move, whatever the rules, to explain a live trade the replay did not take.
    let chunks = snapshot();
    FLT.with(|x| x.set(Flt::default()));
    let raw = all_cands(&chunks, 0);
    let mut safe = active_rules();
    safe.max_followed = safe.max_followed.min(SAFE_MAX_FOLLOWED);
    let near = |m: i64, t: Ms, rt: Ms| m > 0 && (t - rt).abs() <= 30_000;
    let mut used = vec![false; rows.len()];
    let (mut both, mut only_live) = (Vec::new(), Vec::new());
    let (mut d_entry, mut d_exit, mut d_ret) = (Vec::new(), Vec::new(), Vec::new());
    for (id, ts, m, v) in &live {
        let (entry, exit) = (v["entry"].as_f64(), v["exit"].as_f64());
        let ret = v["pnl"].as_f64().zip(v["cost"].as_f64()).map(|(p, c)| p / c.max(1e-9));
        let head = json!({"id": id, "ts": ts, "match_id": m, "home": v["home"], "outcome": v["outcome"], "set": v["set"], "entry": entry, "exit": exit, "return": ret, "reason": v["reason"], "cost": v["cost"]});
        let k = rows.iter().enumerate().filter(|(k, r)| !used[*k] && near(*m, *ts, r["t"].as_i64().unwrap_or(0)) && r["m"].as_i64() == Some(*m)).map(|(k, _)| k).next();
        match k {
            Some(k) => {
                used[k] = true;
                let r = rows[k];
                let r_ret = r["pnl"].as_f64().zip(r["spent"].as_f64()).map(|(p, c)| p / c.max(1e-9));
                if let (Some(a), Some(b)) = (entry, r["entry"].as_f64()) {
                    d_entry.push(a - b);
                }
                if let (Some(a), Some(b)) = (exit, r["exit"].as_f64()) {
                    d_exit.push(a - b);
                }
                if let (Some(a), Some(b)) = (ret, r_ret) {
                    d_ret.push(a - b);
                }
                both.push(json!({"live": head, "replay": {"t": r["t"], "set": r["set"], "entry": r["entry"], "exit": r["exit"], "return": r_ret, "caught": r["caught"], "filled": r["filled"]}}));
            }
            None => {
                let wider = v["set"] == "wider";
                let (p, f) = if wider { (policy_b(), Flt::wider()) } else { (safe, Flt::live()) };
                let why = match raw.iter().find(|c| c.match_id == *m && (c.tf - ts).abs() <= 30_000) {
                    None => "the replay measured no such move",
                    Some(c) => why_not(c, &p, &f).unwrap_or("taken by the rules, lost to the wallet or another trade on the game"),
                };
                let mut h = head;
                h["replay_says"] = json!(why);
                only_live.push(h);
            }
        }
    }
    let only_replay: Vec<Value> = rows
        .iter()
        .enumerate()
        .filter(|(k, _)| !used[*k])
        .map(|(_, r)| {
            let (t, m) = (r["t"].as_i64().unwrap_or(0), r["m"].as_i64().unwrap_or(0));
            let said: Vec<String> = c
                .prepare_cached("SELECT DISTINCT result FROM bot_signals WHERE match_id = ?1 AND ts_ms BETWEEN ?2 AND ?3")
                .and_then(|mut st| st.query_map(params![m, t - 20_000, t + 20_000], |x| x.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).collect()))
                .unwrap_or_default();
            json!({"t": t, "match_id": m, "home": r["home"], "outcome": r["o"], "set": r["set"], "entry": r["entry"], "exit": r["exit"],
                "return": r["pnl"].as_f64().zip(r["spent"].as_f64()).map(|(p, c)| p / c.max(1e-9)),
                "live_says": if said.is_empty() { vec!["no signal logged (bot off, or the move not seen live)".to_string()] } else { said }})
        })
        .collect();
    let mean = |v: &[f64]| if v.is_empty() { None } else { Some(v.iter().sum::<f64>() / v.len() as f64) };
    json!({"from": from, "to": now, "real": real,
        "summary": {"live_trades": live.len(), "replay_trades": rows.len(), "both": both.len(), "only_live": only_live.len(), "only_replay": only_replay.len(),
            "entry_diff": mean(&d_entry), "exit_diff": mean(&d_exit), "return_diff": mean(&d_ret)},
        "both": both, "only_live": only_live, "only_replay": only_replay})
}

/// Every Polymarket candidate (no rule filter) with its features and the result per $ of a
/// $25 trade at catch-up 100%, for each give-up and stop-loss (research).
pub fn research() -> Value {
    if !ST.lock().warm {
        return json!({"building": true});
    }
    let chunks = snapshot();
    FLT.with(|x| x.set(Flt::default()));
    let cands = all_cands(&chunks, 0);
    let r = |x: Option<Exit>, c: &Cand| x.and_then(|x| fill(c, &x, 25.0, true).map(|f| ((f.pnl / 25.0 * 1e4).round() / 1e4, (x.ts - c.tf) / 1000, x.caught)));
    let rows: Vec<Value> = cands
        .iter()
        .map(|c| {
            json!({"t": c.tf, "m": c.match_id, "s": c.sport, "tr": c.trigger, "k": c.books, "bm": c.bm, "fol": c.fol, "sp": c.sp, "e": c.edge[1][2], "px": c.px,
                "p": c.pause, "pm_tr": c.pm_tr, "b_tr5": c.b_tr5, "b_tr10": c.b_tr10, "b_rng": c.b_rng,
                "x": (0..GIVE.len()).map(|gi| r(c.exits[2][gi], c)).collect::<Vec<_>>(),
                "t0": c.t0, "fol_start": c.fol_start, "late_ms": c.late_ms, "gap_before": gap_before(&c.cx, c.dir as f64, c.pre_mid),
                "cx": c.cx, "xs": EXV.iter().enumerate().map(|(si, _)| (0..GIVE.len()).map(|gi| r(c.stop_exits[si][2][gi], c)).collect::<Vec<_>>()).collect::<Vec<_>>()})
        })
        .collect();
    json!({"give": GIVE, "exv": EXV, "rows": rows})
}

/// Shared summary for whole-dataset simulations: trades (fill ts, exit ts, pnl, dollars spent,
/// sport, won the normal way: caught up / unwound).
pub fn sim_summary(start: f64, from: Option<Ms>, now: Ms, mut t: Vec<(Ms, Ms, f64, f64, &str, bool)>) -> Value {
    t.sort_by_key(|x| x.1);
    let n = t.len();
    let pnl: f64 = t.iter().map(|x| x.2).sum();
    let staked: f64 = t.iter().map(|x| x.3).sum();
    let wins = t.iter().filter(|x| x.2 > 0.0).count();
    let (mut bal, mut peak, mut dd) = (start, start, 0.0f64);
    let mut curve = vec![json!([from.unwrap_or(now), start])];
    for x in &t {
        bal += x.2;
        peak = peak.max(bal);
        dd = dd.max(peak - bal);
        curve.push(json!([x.1, bal]));
    }
    let mut days: BTreeMap<i64, (usize, f64, f64)> = BTreeMap::new();
    for x in &t {
        let d = days.entry(x.1 - x.1.rem_euclid(86_400_000)).or_default();
        d.0 += 1;
        d.1 += x.2;
        d.2 += x.3;
    }
    let mut sports: BTreeMap<&str, (usize, f64, f64)> = BTreeMap::new();
    for x in &t {
        let s = sports.entry(x.4).or_default();
        s.0 += 1;
        s.1 += x.2;
        s.2 += x.3;
    }
    let span_d = from.map(|f| (now - f) as f64 / 86_400_000.0).unwrap_or(0.0);
    json!({
        "trades": n, "wins": wins, "pnl": pnl, "staked": staked, "roi": if staked > 0.0 { pnl / staked } else { 0.0 },
        "start": start, "end_balance": start + pnl, "max_drawdown": dd, "from": from, "to": now, "days": span_d,
        "per_day": if span_d > 0.0 { pnl / span_d } else { 0.0 },
        "curve": curve,
        "daily": days.iter().map(|(d, v)| json!({"day": d, "trades": v.0, "pnl": v.1, "staked": v.2})).collect::<Vec<_>>(),
        "sports": sports.iter().map(|(s, v)| json!({"sport": s, "trades": v.0, "pnl": v.1, "staked": v.2})).collect::<Vec<_>>(),
        "normal_exits": t.iter().filter(|x| x.5).count(),
    })
}

/// Newly closed paper trades of the current rules (for Telegram and the Trade review page), with the
/// paper record's filters whatever the thread was left with.
pub fn closed_since(since: Ms) -> Vec<Value> {
    with_paper_flt(|| closed_since_inner(since))
}

fn closed_since_inner(since: Ms) -> Vec<Value> {
    let now = now_ms();
    let chunks = snapshot();
    let (runs, bank) = {
        let st = ST.lock();
        (st.runs.clone(), st.bank)
    };
    let mut out = Vec::new();
    for (bi, base) in BASES.iter().enumerate() {
        if !TRADED[bi] {
            continue;
        }
        let cands = all_cands(&chunks, bi);
        let rules = runs.iter().filter(|r| r.base == *base).last().map(|r| r.active).unwrap_or_else(|| policy(&default_idx(base)));
        let (trades, _, _) = backtest(&cands, &rules, now, &bank, base);
        out.extend(trades.into_iter().filter(|t| t["exit_ts"].as_i64().unwrap_or(0) > since).map(|mut t| {
            // The rules it traded on: where it sells (share of the move caught up) and when it gives up.
            t["catch"] = json!(rules.catch);
            t["give_up_s"] = json!(rules.give_up_s);
            t
        }));
    }
    out.sort_by_key(|t| t["exit_ts"].as_i64());
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stake_is_halved_where_polymarket_does_not_trail() {
        let cx = |jumps, follow, lead| crate::consensus::Ctx { jumps, follow, lead, ..Default::default() };
        // Polymarket moves after the bookmakers in this game: the full stake.
        assert_eq!(stake_factor(&cx(5, 0.9, 0.2)), 1.0);
        assert_eq!(delay_score(&cx(5, 0.9, 0.2)).map(|s| (s * 100.0).round()), Some(70.0));
        // It moves first as much as after, or first: half.
        assert_eq!(stake_factor(&cx(5, 0.6, 0.5)), WEAK_STAKE);
        assert_eq!(stake_factor(&cx(5, 0.3, 1.2)), WEAK_STAKE);
        // No earlier jump to judge by: half.
        assert_eq!(stake_factor(&cx(0, 0.0, 0.0)), WEAK_STAKE);
        assert_eq!(delay_score(&cx(0, 0.0, 0.0)), None);
        // Half of the usual $12 is still above the smallest real order.
        assert!(12.0 * WEAK_STAKE >= crate::snipe::MIN_REAL_STAKE);
    }

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
            stop_exits: [[[None; 5]; 3]; 5],
            cx: Default::default(),
            pm_tr: 0.0,
            b_tr5: 0.0,
            b_tr10: 0.0,
            b_rng: 0.0,
            t0: 0,
            fol_start: 0.0,
            late_ms: 0,
            nofill: false,
            blind: false,
            us_bb: false,
            dec: [0.0, 0.0],
            pre_mid: 0.0,
        }
    }

    /// Not a unit test: builds the replay's candidates for a time span from the real database
    /// and writes them, with the trades the live rules take, as JSON, to compare with
    /// tools/live_emu.py (the checked copy of the live bot). SWIFTEYE_DB, FROM_MS, TO_MS, OUT.

    #[test]
    fn paper_only_sports_never_pass_the_sets_real_money_trades() {
        // A move that passes every other rule of the safe set.
        let cx = crate::consensus::Ctx { fams: 5, fam_moved: 3, cons_move: 0.05, cons_now: 0.6, gap_shock: 0.05, jumps: 3, follow: 0.6, ..Default::default() };
        let why = |f: &Flt, sport: &str, px: f64| f.check(sport, "pinnacle", 0, px, [0.12, 0.12], [0.0; 4], &cx, Some(0.0));
        let safe = Flt::picked();
        assert_eq!(why(&safe, "tennis", 0.60), None);
        assert_eq!(why(&safe, "basketball", 0.60), Some("basketball"));
        assert_eq!(why(&safe, "amfootball", 0.60), Some("paper_only_sport"));
        assert_eq!(why(&safe, "mma", 0.60), Some("paper_only_sport"));
        // The trial lets exactly those in, and is the safe set in everything else.
        let trial = Flt::trial();
        for s in PAPER_ONLY_SPORTS {
            assert!(paper_only(s));
            assert_eq!(why(&trial, s, 0.60), None, "{s}");
            assert_eq!(why(&trial, s, 0.95), Some("price_too_high"), "{s}");
        }
        assert!(!paper_only("tennis") && !paper_only("cs2") && !paper_only("football"));
        assert!(paper_only("dota2"));
    }
    #[test]
    fn basketball_outside_the_us_and_tight_dota_games_may_trade_real_money() {
        assert!(us_basketball_text("NCAA | Citadel Bulldogs South Carolina State"));
        assert!(us_basketball_text("USA. NBA | Boston Celtics Miami Heat"));
        assert!(us_basketball_text("Stephen F. Austin (stud) Indiana State"));
        assert!(!us_basketball_text("Euroleague | Barcelona Crvena Zvezda"));
        assert!(!us_basketball_text("Brazil. NBB | Flamengo Franca")); // NBB is not the NBA
        assert!(!us_basketball_text("USA. WNBA | Aces Liberty"));
        assert!(paper_only_game("basketball", true, 0.01));
        assert!(!paper_only_game("basketball", false, 0.04));
        assert!(!paper_only_game("dota2", false, 0.02));
        assert!(paper_only_game("dota2", false, 0.03));
        assert!(paper_only_game("mma", false, 0.01) && paper_only_game("amfootball", false, 0.01));
        assert!(!paper_only_game("cs2", false, 0.04) && !paper_only_game("tennis", false, 0.04));
        // The live filters refuse the sport, the game's own filters do not.
        let f = Flt::picked();
        assert!(f.no_basketball && !f.for_game("basketball", false, 0.03).no_basketball);
        assert!(f.for_game("basketball", true, 0.03).no_basketball && f.for_game("dota2", false, 0.03).no_basketball);
        assert!(!f.for_game("dota2", false, 0.02).no_basketball);
    }
    #[test]
    fn a_weak_game_is_not_bought_above_60c() {
        let f = Flt { weak_cap: Some(0.60), ..Default::default() };
        let cx = |follow: f64, lead: f64, jumps: usize| crate::consensus::Ctx { fams: 5, fam_moved: 3, cons_move: 0.05, cons_now: 0.5, gap_shock: 0.05, jumps, follow, lead, z: 3.0 };
        let why = |c: &crate::consensus::Ctx, px: f64| f.check("cs2", "pinnacle", 0, px, [0.12, 0.12], [0.0; 4], c, Some(0.0));
        let (weak, strong, fresh) = (cx(0.6, 0.6, 5), cx(0.9, 0.3, 5), cx(0.0, 0.0, 0));
        assert_eq!(why(&weak, 0.66), Some("weak_game_dear_price")); // offer 65c
        assert_eq!(why(&fresh, 0.66), Some("weak_game_dear_price")); // no earlier jump = weak
        assert_eq!(why(&weak, 0.61), None); // offer 60c: allowed
        assert_eq!(why(&strong, 0.85), None); // a strong game at any price
    }
    #[test]
    fn a_gap_in_the_recording_is_measured_around_the_buy() {
        // Quotes every 250 ms, then nothing for 40 s (the feed fell behind), then quotes again.
        let mut own: Vec<Ms> = (0..40).map(|i| 1_000_000 + i * 250).collect();
        own.extend((0..40).map(|i| 1_050_000 + i * 250));
        let s = Seen { own };
        assert_eq!(s.silent(1_005_100), 250); // quotes flowing
        assert!(s.silent(1_020_000) > HOLE_MS); // inside the gap
        assert_eq!(s.silent(1_020_000), 1_050_000 - 1_009_750);
        assert_eq!(s.silent(1_055_000), 250);
        assert_eq!(s.silent(999_000), Ms::MAX); // before anything was recorded: unknown
        assert_eq!(s.silent(1_060_000), 1_060_000 - 1_059_750); // after the last quote: silent so far
    }
    #[test]
    #[ignore]
    fn dump_live_path() {
        let db = std::path::PathBuf::from(std::env::var("SWIFTEYE_DB").expect("SWIFTEYE_DB"));
        let from: Ms = std::env::var("FROM_MS").expect("FROM_MS").parse().unwrap();
        let to: Ms = std::env::var("TO_MS").expect("TO_MS").parse().unwrap();
        let out = std::env::var("OUT").expect("OUT");
        let now = to + 3_600_000;
        let mut chunks: Vec<[Arc<Vec<Cand>>; 2]> = Vec::new();
        let mut start = boundary(from);
        while start < to {
            let t = std::time::Instant::now();
            let [a, b] = build_chunk(&db, start, (start + EVERY_MS).min(to), now).expect("build");
            eprintln!("block {start}: {} polymarket candidates, {} kalshi ({:.0} s)", a.len(), b.len(), t.elapsed().as_secs_f64());
            chunks.push([Arc::new(a), Arc::new(b)]);
            start += EVERY_MS;
        }
        FLT.with(|x| x.set(Flt::default()));
        let light: Vec<Value> = all_cands(&chunks, 0)
            .iter()
            .map(|c| {
                json!({"t0": c.t0, "tf": c.tf, "m": c.match_id, "o": c.outcome, "d": c.dir, "book": c.trigger, "bm": c.bm, "fol": c.fol, "fol_start": c.fol_start,
                    "late_ms": c.late_ms, "sp": c.sp, "edge": [c.edge[0][2], c.edge[1][2]], "px": c.px, "pause": c.pause, "sport": c.sport, "cx": c.cx, "nofill": c.nofill, "blind": c.blind, "us_bb": c.us_bb, "pre_mid": c.pre_mid, "gap_before": gap_before(&c.cx, c.dir as f64, c.pre_mid),
                    "x900": c.exits[2][3].map(|x| json!([x.ts, x.px, x.caught])), "x300": c.exits[2][2].map(|x| json!([x.ts, x.px, x.caught]))})
            })
            .collect();
        FLT.with(|x| x.set(Flt { hours: Some([12, 21]), ..Flt::picked() }));
        let cands = all_cands(&chunks, 0);
        let mut trades: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for give in [900, 300] {
            let p = Policy { min_books: 1, max_followed: 0.05, min_edge: 0.04, max_spread: 0.04, min_move: 0.10, catch: 1.0, give_up_s: give, skip_after_pause: true, walk_book: true };
            let (ci, _) = sale_idx(&p);
            let mut v = Vec::new();
            // As bot_sim: the rules took the move (the game is locked) whether or not $12 fills.
            run_policy(&cands, &p, 0, now + 1, now, |_, c, x| {
                if let Some(f) = fill(c, x, 12.0, may_walk(c, &p, ci)) {
                    v.push(json!({"t0": c.t0, "m": c.match_id, "o": c.outcome, "d": c.dir, "book": c.trigger, "entry": f.entry, "exit": f.exit, "pnl": f.pnl, "spent": f.spent, "caught": x.caught, "x": x.ts, "home": c.home, "sport": c.sport}));
                }
                true
            });
            trades.insert(format!("give{give}"), v);
        }
        std::fs::write(&out, serde_json::to_vec(&json!({"cands": light, "trades": trades})).unwrap()).unwrap();
        eprintln!("{} candidates written to {out}", light.len());
    }

    #[test]
    fn walks_only_when_the_gain_clears_the_minimum_after_2c() {
        let mut p = policy_b(); // min edge 2c, walks the book
        let mut c = cand(0.50, Some((200.0, 1000.0)));
        c.edge = [[0.05; 3]; 2];
        assert!(may_walk(&c, &p, 2)); // 5c gain - 2c = 3c >= 2c
        c.edge = [[0.03; 3]; 2];
        assert!(!may_walk(&c, &p, 2)); // 3c - 2c = 1c < 2c: best price only
        p.walk_book = false;
        c.edge = [[0.10; 3]; 2];
        assert!(!may_walk(&c, &p, 2));
    }

    #[test]
    fn order_size_rules() {
        let x = Exit { ts: 1, px: 0.60, sell: None, caught: true };
        // No recorded book: the whole stake fills at the price. 1,000 contracts, 50c -> 60c, less
        // Polymarket's sports fee on the buy ($12.50) and the sale ($12.00).
        let f = fill(&cand(0.50, None), &x, 500.0, false).unwrap();
        assert!((f.spent - 500.0).abs() < 1e-9 && (f.pnl - 75.5).abs() < 1e-9 && f.filled.is_none());
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
                out.push(json!({"b": BASES[bi], "m": c.match_id, "s": c.sport, "o": c.outcome, "d": c.dir, "tr": c.trigger, "t": c.tf, "cx": c.cx,
                    "k": c.books, "bm": c.bm, "fol": c.fol, "sp": c.sp, "e": c.edge[1], "px": c.px, "p": c.pause, "dep": c.buy.is_some(), "x": ex}));
            }
        }
        start += EVERY_MS;
    }
    Ok(json!({"reaction": reaction, "confirm": confirm, "catch": CATCH, "give": GIVE, "rows": out}))
}
