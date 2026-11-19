//! Snipe window: a result cache for the heavy move analysis, and a paper-trading simulation
//! of "a bookmaker moved, buy the prediction market before it reacts".
//!
//! ## Cache
//! `rank::compute` reads every 1X2 change in the window (≈2 M rows a day), seconds of CPU.
//! Results are cached by their parameters; a "last N hours" request is keyed by its span, not
//! its exact start, so every page asking for "24 h" shares one entry. Fresh (< 20 s) entries
//! are served as they are; older ones (< 30 min) are served at once and refreshed in the
//! background. Only one analysis runs at a time (the Mac also runs other jobs).
//!
//! ## Simulation (paper money, nothing is traded)
//! The model ONLY buys when it can see, at that moment, that the prediction market is delayed:
//! 1. Trigger: a bookmaker crosses halfway through a 1X2 move (the first bookmaker on each move,
//!    or one chosen bookmaker). We act `reaction_ms` later.
//! 2. Gate, using only prices known then: the bookmaker's move so far (vs 10 s before) is still
//!    there, the market has made less than `max_followed` of that move (it is delayed), and the
//!    expected gain if it catches up to `catch` of the move, after spread and fees, is at least
//!    `min_edge`. Otherwise no trade.
//! 3. Buy the side the bookmaker moved to (Yes if likelier, No otherwise) at the ask.
//! 4. Sell at the bid at the first quote where the market has made `catch` of the bookmaker's
//!    move ("caught up"), or at the give-up time ("gave up"), whichever comes first.
//! * Polymarket: real best bid/ask from `pm_quotes`. Fee: Polymarket's sports taker fee
//!   (5% x p x (1-p) per contract, each side), or `pm_fee_bps` of notional when set.
//! * Kalshi: only the mid is recorded, so bid/ask = mid ∓ `k_spread`/2. Fee: Kalshi's taker
//!   formula, 7% × contracts × p × (1-p), rounded up to the cent, on entry and exit.
//! * Not modelled: order-book depth (every fill is assumed at the top of book), queue, or the
//!   price moving during our own reaction beyond what the recorded quotes show.

use crate::api::LagQ;
use crate::model::Ms;
use crate::rank::{RankEvent, Rankings, KALSHI};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, LazyLock};

const FRESH_MS: Ms = 20_000;
const STALE_OK_MS: Ms = 30 * 60_000;
const PM: &str = "polymarket";

/// Cached results are either `Rankings` or a simulation `Value`; stored type-erased.
type Any = Arc<dyn std::any::Any + Send + Sync>;
type Entry = (Ms, Any);
static CACHE: LazyLock<Mutex<HashMap<String, Entry>>> = LazyLock::new(|| Mutex::new(HashMap::new()));
static REFRESHING: LazyLock<Mutex<HashSet<String>>> = LazyLock::new(|| Mutex::new(HashSet::new()));
/// One heavy computation at a time.
static HEAVY: LazyLock<tokio::sync::Mutex<()>> = LazyLock::new(|| tokio::sync::Mutex::new(()));

fn now() -> Ms {
    crate::model::now_ms()
}

/// Cache key for the analysis parameters. "Last N hours" is keyed by N, so it stays shared.
fn rank_key(q: &LagQ) -> String {
    let p = q.params();
    let range = match (q.since, q.until) {
        (Some(s), None) if s <= 0 => "all".to_string(),
        (_, None) => format!("span{}", ((now() - p.since) as f64 / 60_000.0).round() as i64),
        (_, Some(u)) => format!("abs{}-{u}", p.since),
    };
    format!(
        "{range}|{}|{}|{}|{}|{:?}|{:?}|{:?}|{:?}",
        p.min_jump, p.window_ms, p.settle_ms, p.source_clock, p.match_id, p.book, p.sport, p.fastest
    )
}

/// The same query moved to "now" (a span keeps its length).
fn rebased(q: &LagQ) -> LagQ {
    let mut q = q.clone();
    if q.until.is_none() {
        if let Some(s) = q.since.filter(|s| *s > 0) {
            let span = (((now() - s) as f64 / 60_000.0).round() as i64) * 60_000;
            q.since = Some(now() - span);
        }
    }
    q
}

type Job = Arc<dyn Fn() -> Result<Any, String> + Send + Sync>;

/// Serve from the cache (fresh, or stale while a background refresh runs), else compute now.
async fn cached(key: String, job: Job) -> Result<(Any, Ms), String> {
    let hit = CACHE.lock().get(&key).cloned();
    if let Some((at, v)) = hit {
        let age = now() - at;
        if age < FRESH_MS {
            return Ok((v, age));
        }
        if age < STALE_OK_MS {
            if REFRESHING.lock().insert(key.clone()) {
                let (k, j) = (key.clone(), job.clone());
                tokio::spawn(async move {
                    let _ = run(k.clone(), j).await;
                    REFRESHING.lock().remove(&k);
                });
            }
            return Ok((v, age));
        }
    }
    run(key, job).await.map(|v| (v, 0))
}

async fn run(key: String, job: Job) -> Result<Any, String> {
    let _g = HEAVY.lock().await;
    // Someone else may have filled it while we waited for the lock.
    if let Some((at, v)) = CACHE.lock().get(&key).cloned() {
        if now() - at < FRESH_MS {
            return Ok(v);
        }
    }
    let v = tokio::task::spawn_blocking(move || job()).await.map_err(|e| e.to_string())??;
    let mut c = CACHE.lock();
    c.insert(key, (now(), v.clone()));
    // Keep the cache small: drop the oldest entries beyond 40.
    if c.len() > 40 {
        let mut ks: Vec<(String, Ms)> = c.iter().map(|(k, (t, _))| (k.clone(), *t)).collect();
        ks.sort_by_key(|x| x.1);
        for (k, _) in ks.into_iter().take(c.len() - 40) {
            c.remove(&k);
        }
    }
    Ok(v)
}

/// Rankings as JSON-able struct, cached. Returns (rankings, age of the cached result in ms).
pub async fn rankings_cached(app: &Arc<App>, q: &LagQ) -> Result<(Arc<Rankings>, Ms), String> {
    let (v, age) = cached(format!("rank|{}", rank_key(q)), rank_job(app, q)).await?;
    let r = v.downcast::<Rankings>().map_err(|_| "cache type mismatch".to_string())?;
    Ok((r, age))
}

fn rank_job(app: &Arc<App>, q: &LagQ) -> Job {
    let (path, q) = (app.db_path.clone(), q.clone());
    Arc::new(move || {
        let p = rebased(&q).params();
        let r = crate::rank::compute(&path, &p).map_err(|e| format!("{e:#}"))?;
        Ok(Arc::new(r) as Any)
    })
}

/// Keep the default Rankings view (last 24 h, default thresholds) warm, so pages open instantly.
pub fn spawn_warmer(app: Arc<App>) {
    // Real Kalshi spreads for the hours before real quotes were recorded (once per start).
    let a2 = app.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        if let Err(e) = kalshi_backfill(&a2, 3).await {
            tracing::warn!("kalshi backfill: {e}");
        }
    });
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(20)).await;
        loop {
            let mut q = LagQ::default();
            q.since = Some(now() - 24 * 3_600_000);
            if let Err(e) = rankings_cached(&app, &q).await {
                tracing::warn!("rankings warm-up: {e}");
            }
            // The Snipe window page's default view too.
            if let Err(e) = simulate_cached(&app, &q, &SimQ::default()).await {
                tracing::warn!("snipe warm-up: {e}");
            }
            tokio::time::sleep(std::time::Duration::from_secs(25)).await;
        }
    });
}

// ---------------------------------------------------------------- simulation

#[derive(Deserialize, Default, Clone, Debug)]
#[serde(default)]
pub struct SimQ {
    /// ms from the bookmaker's move to our decision (detection). Default 1000.
    pub reaction_ms: Option<i64>,
    /// ms from the decision to the fill (order travel). The fill uses the quote at that moment. Default 250.
    pub fill_ms: Option<i64>,
    /// Price paid worse than the quote on every buy and sale, in cents. Default 1.
    pub slip_c: Option<u32>,
    /// Paper stake per trade in dollars (as cents x100 to keep this hashable). Default 100.
    pub stake: Option<u64>,
    /// Polymarket fee in basis points of notional, each side. Default (None): Polymarket's sports
    /// taker fee, 5% x p x (1-p) per contract.
    pub pm_fee_bps: Option<u32>,
    /// Assumed Kalshi spread when neither a real quote nor a candle is known (cents). Default 4.
    pub k_spread_c: Option<u32>,
    /// "first" (default) = trigger on the first bookmaker of each move; "fastest"; or a bookmaker name.
    pub trigger: Option<String>,
    /// Sell once the market has made this share (%) of the bookmaker's move. Default 75.
    pub catch_pct: Option<u32>,
    /// Entry gate per market (pm_ = Polymarket, k_ = Kalshi); see `Gate`.
    pub pm_min_edge_c: Option<u32>,
    pub pm_max_followed_pct: Option<u32>,
    pub pm_min_books: Option<u32>,
    pub pm_max_spread_c: Option<u32>,
    pub pm_max_age_s: Option<u32>,
    pub k_min_edge_c: Option<u32>,
    pub k_max_followed_pct: Option<u32>,
    pub k_min_books: Option<u32>,
    pub k_max_spread_c: Option<u32>,
    pub k_max_age_s: Option<u32>,
    pub pm_min_entry_c: Option<u32>,
    pub k_min_entry_c: Option<u32>,
    /// false = also trade on an assumed spread when Kalshi's real price is unknown (research only).
    pub k_need_real: Option<bool>,
    /// ms between the two delay tests; both must pass before buying. Default 500.
    pub confirm_ms: Option<i64>,
    /// Return every trade (for research), not just the latest 60.
    pub all: Option<bool>,
}

/// When the model is allowed to buy, using only what is known at that moment.
#[derive(Clone, Copy, Debug, Serialize)]
pub struct Gate {
    /// Expected gain if the market catches up, after spread, slippage and fees (probability).
    pub min_edge: f64,
    /// "Delayed" = the market has made less than this share of the bookmaker's move.
    pub max_followed: f64,
    /// At least this many bookmakers have already made the move.
    pub min_books: usize,
    /// The market's spread is at most this.
    pub max_spread: f64,
    /// The market's last quote change is at most this old (a dead market does not catch up).
    pub max_age_ms: Ms,
    /// Buy price at least this (cheap long shots rarely catch up in time).
    pub min_entry: f64,
    /// Only trade on a real, known price (Kalshi hours with neither a recorded quote nor a
    /// minute candle are skipped instead of trading on an assumed spread).
    pub need_real: bool,
    /// Give up (sell at the bid) after this long if the price has not caught up.
    pub give_up_s: i64,
}

/// Defaults per market, chosen on the first 60% of the data and checked on the last 40%
/// (tools/snipe_research.mjs; 17 Nov 2026, real spreads, 1c slippage, 250 ms fills):
/// * Polymarket rarely follows ONE bookmaker: buys on a single book's move lost ~7% a trade.
///   With 2+ books moved, price >= 40c, market <= 10% of the way, spread <= 2c:
///   +$2.2 a trade before the split (33), +$2.7 after (27), give-up 2 min.
/// * Kalshi follows slowly (median ~18 s behind): only a 5-min give-up paid. Real prices only,
///   spread <= 4c, expected gain >= 2c: +$13.7 a trade before (18), +$10.0 after (24).
/// Small samples (one day); re-run the research as data grows.
/// Sweep 19 Nov 2026 (48 h, ~5,500 Polymarket rule combinations x confirm gap 0/0.5/1 s x give-up,
/// best on the first 60% by profit, checked on the last 40%): 2+ independent books, spread <= 2c,
/// expected gain >= 1c, no price floor, give-up 5 min: +$17.3 a trade (10) then +$22.5 (4), against
/// +$6.9 / +$15.6 for the previous rule. 2,412 of 2,448 train-profitable combos also made money on
/// test: the Polymarket delay edge is robust. Kalshi was not (168 of 1,028; its test "profit" was two
/// 4c buys on one 86-point swing, -$542 without the top 3 trades), so its gate stays strict.
pub const PM_GATE: Gate = Gate { min_edge: 0.01, max_followed: 0.10, min_books: 2, max_spread: 0.02, max_age_ms: 86_400_000, min_entry: 0.0, need_real: true, give_up_s: 300 };
pub const K_GATE: Gate = Gate { min_edge: 0.02, max_followed: 0.25, min_books: 2, max_spread: 0.04, max_age_ms: 86_400_000, min_entry: 0.0, need_real: true, give_up_s: 300 };

impl SimQ {
    fn gate(&self, base: &str) -> Gate {
        let (d, e, f, b, sp, age, me, nr) = if base == PM {
            (PM_GATE, self.pm_min_edge_c, self.pm_max_followed_pct, self.pm_min_books, self.pm_max_spread_c, self.pm_max_age_s, self.pm_min_entry_c, None)
        } else {
            (K_GATE, self.k_min_edge_c, self.k_max_followed_pct, self.k_min_books, self.k_max_spread_c, self.k_max_age_s, self.k_min_entry_c, self.k_need_real)
        };
        Gate {
            min_edge: e.map_or(d.min_edge, |x| x as f64 / 100.0),
            max_followed: f.map_or(d.max_followed, |x| x as f64 / 100.0),
            min_books: b.map_or(d.min_books, |x| x as usize),
            max_spread: sp.map_or(d.max_spread, |x| x as f64 / 100.0),
            max_age_ms: age.map_or(d.max_age_ms, |x| x as Ms * 1000),
            min_entry: me.map_or(d.min_entry, |x| x as f64 / 100.0),
            need_real: nr.unwrap_or(d.need_real),
            give_up_s: d.give_up_s,
        }
    }
}

#[derive(Serialize, Clone, Debug)]
struct Trade {
    ts: Ms,
    exit_ts: Ms,
    match_id: i64,
    sport: String,
    home: String,
    away: String,
    outcome: String,
    dir: i8,
    trigger: String,
    /// Bookmaker move in probability points (signed toward `dir`, so positive).
    book_move: f64,
    /// Share of that move the market had already made when we bought.
    followed: f64,
    entry: f64,
    exit: f64,
    /// "caught up" or "gave up".
    reason: &'static str,
    pnl: f64,
    /// Hindsight label: when the market crossed halfway, minus the trigger (positive = later).
    lead_ms: Option<i64>,
    // ---- what was known at the moment of buying (used to choose the entry rules)
    /// Bookmakers that had already made the move.
    books: usize,
    /// Market spread at the decision.
    spread: f64,
    /// Age of the market's last quote change at the decision.
    quote_age_ms: Ms,
    /// Expected gain per $1 contract at the decision, after spread and fees.
    edge: f64,
    /// Where the quote came from: "book" (real top of book), "candle" (Kalshi minute candle spread),
    /// "assumed" (mid ± the assumed spread).
    src: &'static str,
    /// Share of the stake the recorded order book could fill; null = book depth not recorded then.
    filled: Option<f64>,
}

#[derive(Serialize, Default, Clone, Debug)]
struct Group {
    trades: usize,
    wins: usize,
    pnl: f64,
}

/// One top-of-book reading: (bid, ask, time of the quote, source).
pub(crate) type Q = (f64, f64, Ms, &'static str);

/// Top of book for one prediction market.
pub(crate) trait Quotes {
    fn at(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<Q>;
    /// Order-book size behind the latest quote at or before t (None = not recorded).
    fn depth(&mut self, _match_id: i64, _outcome: &str, _t: Ms) -> Option<crate::engine::Depth> {
        None
    }
    /// Every quote change in (from, to], oldest first.
    fn path(&mut self, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Vec<Q>;
    /// The last recorded row at or before t as it is: (bid, ask, when); either side may be missing.
    fn raw(&mut self, _match_id: i64, _outcome: &str, _t: Ms) -> Option<(Option<f64>, Option<f64>, Ms)> {
        None
    }
}

fn ok_quote(b: Option<f64>, a: Option<f64>) -> Option<(f64, f64)> {
    Some((b?, a?)).filter(|(b, a)| *b > 0.0 && *a < 1.0 && a > b)
}

/// A quote older than this says nothing about now.
const STALE_MS: Ms = 30 * 60_000;

struct PmQuotes<'a> {
    c: &'a Connection,
}

fn depth_row(r: &rusqlite::Row) -> rusqlite::Result<crate::engine::Depth> {
    Ok(crate::engine::Depth { bid_size: r.get(0)?, ask_size: r.get(1)?, bid_depth: r.get(2)?, ask_depth: r.get(3)? })
}

impl Quotes for PmQuotes<'_> {
    fn raw(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<(Option<f64>, Option<f64>, Ms)> {
        let mut st = self
            .c
            .prepare_cached("SELECT bid, ask, ts_ms FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1")
            .ok()?;
        st.query_row(params![match_id, outcome, t], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional().ok()?
    }
    fn depth(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<crate::engine::Depth> {
        let mut st = self
            .c
            .prepare_cached("SELECT bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1")
            .ok()?;
        st.query_row(params![match_id, outcome, t], depth_row).optional().ok()?
    }
    fn at(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<Q> {
        let mut st = self
            .c
            .prepare_cached("SELECT bid, ask, ts_ms FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1")
            .ok()?;
        let r: Option<(Option<f64>, Option<f64>, Ms)> = st.query_row(params![match_id, outcome, t], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional().ok()?;
        let (b, a, ts) = r?;
        if t - ts > STALE_MS {
            return None;
        }
        ok_quote(b, a).map(|(b, a)| (b, a, ts, "book"))
    }
    fn path(&mut self, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Vec<Q> {
        let Ok(mut st) = self.c.prepare_cached("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms > ?3 AND ts_ms <= ?4 ORDER BY ts_ms") else {
            return Vec::new();
        };
        let rows = st.query_map(params![match_id, outcome, from, to], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, Option<f64>>(1)?, r.get::<_, Option<f64>>(2)?)));
        rows.map(|it| it.filter_map(|x| x.ok()).filter_map(|(t, b, a)| ok_quote(b, a).map(|(b, a)| (b, a, t, "book"))).collect()).unwrap_or_default()
    }
}

/// Kalshi: real top of book from `kalshi_quotes` when recorded; for earlier hours the mid we
/// logged (second by second) with the spread of Kalshi's own 1-minute candle; else mid ± assumed.
struct KalshiQuotes<'a> {
    c: &'a Connection,
    half: f64,
    /// match id -> (Kalshi event ticker, listed the other way round from the match?)
    events: HashMap<i64, Option<(String, bool)>>,
}

impl KalshiQuotes<'_> {
    /// `kalshi_quotes` rows keep Kalshi's own home/away (the event title's order); `changes`
    /// and every other table use the match's. The flag says whether to turn one into the other,
    /// decided by the same name rule the matcher used when linking the event.
    fn event(&mut self, match_id: i64) -> Option<(String, bool)> {
        let c = self.c;
        self.events.entry(match_id).or_insert_with(|| kalshi_event(c, match_id)).clone()
    }
    fn mid(&self, match_id: i64, outcome: &str, t: Ms) -> Option<(f64, Ms)> {
        let mut st = self
            .c
            .prepare_cached("SELECT new_price, ts_ms FROM changes WHERE match_id = ?1 AND book = 'kalshi' AND market = '1x2' AND outcome = ?2 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1")
            .ok()?;
        let (p, ts): (Option<f64>, Ms) = st.query_row(params![match_id, outcome, t], |r| Ok((r.get(0)?, r.get(1)?))).optional().ok()??;
        if t - ts > STALE_MS {
            return None;
        }
        Some((1.0 / p.filter(|p| *p > 1.0)?, ts))
    }
    /// Latest kalshi_quotes row at or before t: (bid, ask, ts, est).
    fn row(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<(Option<f64>, Option<f64>, Ms, bool)> {
        let (ev, swap) = self.event(match_id)?;
        let outcome = kalshi_side(outcome, swap);
        let mut st = self.c.prepare_cached("SELECT bid, ask, ts_ms, est FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1").ok()?;
        st.query_row(params![ev, outcome, t], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get::<_, i64>(3)? != 0))).optional().ok()?
    }
}

impl Quotes for KalshiQuotes<'_> {
    fn depth(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<crate::engine::Depth> {
        let (ev, swap) = self.event(match_id)?;
        let mut st = self
            .c
            .prepare_cached("SELECT bid_size, ask_size, bid_depth, ask_depth FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND est = 0 AND ts_ms <= ?3 ORDER BY ts_ms DESC LIMIT 1")
            .ok()?;
        st.query_row(params![ev, kalshi_side(outcome, swap), t], depth_row).optional().ok()?
    }
    fn at(&mut self, match_id: i64, outcome: &str, t: Ms) -> Option<Q> {
        let row = self.row(match_id, outcome, t).filter(|r| t - r.2 <= STALE_MS);
        if let Some((b, a, ts, false)) = row {
            // A real quote; one-sided or empty = nothing to trade.
            return ok_quote(b, a).map(|(b, a)| (b, a, ts, "book"));
        }
        let (mid, mts) = self.mid(match_id, outcome, t)?;
        let (half, src) = match row {
            Some((Some(b), Some(a), _, true)) if a > b => ((a - b) / 2.0, "candle"),
            _ => (self.half, "assumed"),
        };
        Some(((mid - half).max(0.01), (mid + half).min(0.99), mts, src))
    }
    fn path(&mut self, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Vec<Q> {
        // Every moment either the logged mid or a recorded quote changed.
        let mut ts: Vec<Ms> = Vec::new();
        if let Ok(mut st) = self.c.prepare_cached("SELECT ts_ms FROM changes WHERE match_id = ?1 AND book = 'kalshi' AND market = '1x2' AND outcome = ?2 AND ts_ms > ?3 AND ts_ms <= ?4") {
            if let Ok(it) = st.query_map(params![match_id, outcome, from, to], |r| r.get::<_, Ms>(0)) {
                ts.extend(it.filter_map(|x| x.ok()));
            }
        }
        if let Some((ev, swap)) = self.event(match_id) {
            if let Ok(mut st) = self.c.prepare_cached("SELECT ts_ms FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND ts_ms > ?3 AND ts_ms <= ?4") {
                if let Ok(it) = st.query_map(params![ev, kalshi_side(outcome, swap), from, to], |r| r.get::<_, Ms>(0)) {
                    ts.extend(it.filter_map(|x| x.ok()));
                }
            }
        }
        ts.sort();
        ts.dedup();
        ts.into_iter().filter_map(|t| self.at(match_id, outcome, t).map(|q| (q.0, q.1, t, q.3))).collect()
    }
}

/// The match's outcome name as Kalshi's own listing calls it (home and away trade places
/// when the event is listed the other way round; the draw never moves).
/// A match's Kalshi event ticker, and whether Kalshi lists the teams the other way round.
pub(crate) fn kalshi_event(c: &Connection, match_id: i64) -> Option<(String, bool)> {
    c.query_row(
        "SELECT b.book_event_id, COALESCE(b.sport, m.sport), m.home, m.away, COALESCE(b.home, ''), COALESCE(b.away, '')
         FROM book_events b JOIN matches m ON m.id = b.match_id
         WHERE b.book = 'kalshi' AND b.match_id = ?1 ORDER BY b.last_seen_ms DESC LIMIT 1",
        params![match_id],
        |r| {
            let (ev, sport, mh, ma, eh, ea): (String, String, String, String, String, String) =
                (r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?);
            let swap = !eh.is_empty() && crate::matcher::names_swapped(&sport, &mh, &ma, &eh, &ea);
            Ok((ev, swap))
        },
    )
    .optional()
    .ok()
    .flatten()
}

pub(crate) fn kalshi_side(outcome: &str, swap: bool) -> &str {
    match (swap, outcome) {
        (true, "home") => "away",
        (true, "away") => "home",
        (_, o) => o,
    }
}

/// A source stopped being seen for this outcome (collector restart, event dropped from the
/// feed) and came back inside (from, to]: an `open` row with earlier rows before it. A
/// "move" measured across that gap is whatever happened while nobody was watching.
pub(crate) fn reopened(c: &Connection, book: &str, match_id: i64, outcome: &str, from: Ms, to: Ms) -> bool {
    let Ok(mut st) = c.prepare_cached(
        "SELECT 1 FROM changes o WHERE o.match_id = ?1 AND o.book = ?2 AND o.market = '1x2' AND o.outcome = ?3
           AND o.kind = 'open' AND o.ts_ms > ?4 AND o.ts_ms <= ?5
           AND EXISTS (SELECT 1 FROM changes p WHERE p.match_id = ?1 AND p.book = ?2 AND p.market = '1x2'
                         AND p.outcome = ?3 AND p.ts_ms < o.ts_ms)
         LIMIT 1",
    ) else {
        return false;
    };
    st.exists(params![match_id, book, outcome, from, to]).unwrap_or(false)
}

/// Quote readers for the adaptive paper bot (adaptive.rs).
pub(crate) fn pm_quotes(c: &Connection) -> Box<dyn Quotes + '_> {
    Box::new(PmQuotes { c })
}

pub(crate) fn kalshi_quotes(c: &Connection) -> Box<dyn Quotes + '_> {
    Box::new(KalshiQuotes { c, half: 0.02, events: HashMap::new() })
}

/// Brands that share one odds feed count as ONE confirmation (same trading desk, same prices).
pub(crate) fn platform(book: &str) -> &str {
    match book {
        "linebet" | "melbet" | "betwinner" | "megapari" | "paripesa" | "22bet" | "22bit" | "1xbit" => "1xbet",
        "unibet" | "leovegas" | "paf" => "kambi",
        // mostbet repeats fonbet's prices (84% of ticks identical within 3 s).
        "fonbet" | "pari" | "mostbet" => "fonbet",
        "vinn" | "betking" => "altenar",
        b => b,
    }
}

/// Independent bookmakers (distinct platforms) that had made the move by time `t`.
pub(crate) fn confirmations(e: &RankEvent, t: Ms) -> usize {
    let mut p: Vec<&str> = e.order.iter().filter(|x| x.0 != PM && x.0 != KALSHI && x.1 <= t).map(|x| platform(&x.0)).collect();
    p.sort();
    p.dedup();
    p.len()
}

/// A bookmaker's implied probability for (match, outcome) at `t` (last change at or before).
/// The bookmaker's last real price at or before `t`. A suspension (a row with no price) does
/// not count: across a goal the book suspends, then reopens at the new price, and the move is
/// measured from the price before the suspension (as the move detector in rank.rs does).
/// Gaps in recording are handled separately (`reopened`).
pub(crate) fn book_prob(c: &Connection, book: &str, match_id: i64, outcome: &str, t: Ms) -> Option<f64> {
    let mut st = c
        .prepare_cached("SELECT new_price FROM changes WHERE match_id = ?1 AND book = ?2 AND market = '1x2' AND outcome = ?3 AND ts_ms <= ?4 AND new_price IS NOT NULL ORDER BY ts_ms DESC LIMIT 1")
        .ok()?;
    let p: Option<Option<f64>> = st.query_row(params![match_id, book, outcome, t], |r| r.get(0)).optional().ok()?;
    Some(1.0 / p??.max(1.0001))
}

pub(crate) fn kalshi_fee(contracts: f64, p: f64) -> f64 {
    // Round up to the cent, ignoring float noise (175.00000000000003 cents is 1.75, not 1.76).
    (0.07 * contracts * p * (1.0 - p) * 100.0 - 1e-9).ceil() / 100.0
}

/// Give-up times compared on the page (seconds).
const HOLDS_S: [i64; 7] = [30, 60, 120, 300, 600, 1200, 1800];
/// How long before the trigger the "before the move" levels are read.
pub(crate) const PRE_MS: Ms = 10_000;

pub async fn simulate_cached(app: &Arc<App>, q: &LagQ, s: &SimQ) -> Result<Arc<Value>, String> {
    let (rank, _) = rankings_cached(app, q).await?;
    // Keyed on the whole query with defaults filled in, so the page and the warmer share entries.
    let mut n = s.clone();
    n.reaction_ms.get_or_insert(1000);
    n.fill_ms.get_or_insert(250);
    n.slip_c.get_or_insert(1);
    n.stake.get_or_insert(100);
    n.k_spread_c.get_or_insert(4);
    n.catch_pct.get_or_insert(75);
    n.confirm_ms.get_or_insert(500);
    if n.trigger.as_deref().map_or(true, str::is_empty) {
        n.trigger = Some("first".into());
    }
    let key = format!("sim4|{}|{:?}", rank_key(q), n);
    let (path, s2) = (app.db_path.clone(), s.clone());
    let job: Job = Arc::new(move || simulate(&path, &rank, &s2).map(|v| Arc::new(v) as Any));
    let (v, _) = cached(key, job).await?;
    v.downcast::<Value>().map_err(|_| "cache type mismatch".to_string())
}

/// A buy the model made, before choosing when to sell.
struct Buy<'a> {
    e: &'a RankEvent,
    trigger: String,
    /// Fill time.
    tf: Ms,
    pre_mid: f64,
    book_move: f64,
    followed: f64,
    entry: f64,
    books: usize,
    spread: f64,
    quote_age_ms: Ms,
    edge: f64,
    src: &'static str,
    /// Contracts bought (all of the stake unless the recorded book was thinner).
    contracts: f64,
    depth_known: bool,
}

/// Entries on one match closer than this are one move.
pub(crate) const SAME_MOVE_MS: Ms = 120_000;
/// With a known book, a fill under this many dollars is not a trade.
pub(crate) const MIN_FILL: f64 = 10.0;

/// Buy for `stake` at `price` with `best` contracts resting there and `near` within 2c
/// (including `best`); the rest of `near` fills 2c worse, anything beyond is not filled.
/// Returns (contracts, average price).
pub(crate) fn fill_buy(stake: f64, price: f64, best: f64, near: f64) -> (f64, f64) {
    let a = (stake / price).min(best.max(0.0));
    let b = ((stake - a * price) / (price + 0.02)).min((near - best).max(0.0)).max(0.0);
    let n = a + b;
    (n, if n > 0.0 { (a * price + b * (price + 0.02)) / n } else { price })
}

/// Has the market caught up, judged on the price we would actually sell at? Selling Yes hits
/// the bid, selling No hits the Yes ask. The move counts from the mid before the trigger, with
/// half the entry spread allowed, so a normal quote passes as the old mid test did but a spread
/// that blows out (mid on target, bid collapsed) does not.
pub(crate) fn caught_up(q: &Q, dir: f64, pre_mid: f64, need: f64, entry_spread: f64) -> bool {
    let px = if dir > 0.0 { q.0 } else { q.1 };
    (px - pre_mid) * dir >= need - entry_spread / 2.0
}

/// Sell `n` contracts at `price` with `best` resting there and `near` within 2c; the rest of
/// `near` 2c lower, anything beyond 5c lower. Returns the average price.
pub(crate) fn fill_sell(n: f64, price: f64, best: f64, near: f64) -> f64 {
    if n <= 0.0 {
        return price;
    }
    let a = n.min(best.max(0.0));
    let b = (n - a).min((near - best).max(0.0));
    let c = n - a - b;
    ((a * price + b * (price - 0.02) + c * (price - 0.05)) / n).max(0.0)
}

fn simulate(path: &std::path::Path, r: &Rankings, s: &SimQ) -> Result<Value, String> {
    let c = crate::store::open_ro(path).map_err(|e| format!("{e:#}"))?;
    let reaction = s.reaction_ms.unwrap_or(1000).clamp(0, 60_000);
    let fill_ms = s.fill_ms.unwrap_or(250).clamp(0, 10_000);
    let slip = s.slip_c.unwrap_or(1).min(20) as f64 / 100.0;
    let stake = s.stake.unwrap_or(100).clamp(1, 1_000_000) as f64;
    let pm_fee_bps = s.pm_fee_bps.map(|b| b.min(1000) as f64 / 10_000.0);
    let k_half = s.k_spread_c.unwrap_or(4).min(50) as f64 / 200.0;
    let catch = s.catch_pct.unwrap_or(75).clamp(5, 200) as f64 / 100.0;
    let confirm_ms = s.confirm_ms.unwrap_or(500).clamp(0, 10_000);
    // A fill more than this worse than the decided price is not taken (limit order cap).
    let cap = 0.02;
    let trigger = s.trigger.clone().filter(|t| !t.is_empty()).unwrap_or_else(|| "first".into());
    let trigger = if trigger == "fastest" { r.fastest_book.clone().unwrap_or_else(|| "first".into()) } else { trigger };
    let t_now = now();

    let mut out = Vec::new();
    for base in [PM, KALSHI] {
        let g = s.gate(base);
        let mut quotes: Box<dyn Quotes> =
            if base == PM { Box::new(PmQuotes { c: &c }) } else { Box::new(KalshiQuotes { c: &c, half: k_half, events: HashMap::new() }) };
        // Fee for n contracts at price p, one side.
        let fee = |n: f64, p: f64| match (base == PM, pm_fee_bps) {
            (true, Some(f)) => f * n * p,
            (true, None) => crate::venue::pm_fee(crate::venue::Fee::PM_SPORTS, n, p),
            _ => kalshi_fee(n, p),
        };
        let moves: Vec<(&RankEvent, String, Ms)> = r
            .events
            .iter()
            .filter(|e| e.order.iter().any(|x| x.0 == base) || e.missed.iter().any(|m| m == base))
            .filter_map(|e| {
                if trigger == "first" {
                    Some((e, e.first_book.clone()?, e.first_book_ms?))
                } else {
                    e.order.iter().find(|x| x.0 == trigger).map(|x| (e, x.0.clone(), x.1))
                }
            })
            .collect();

        // ---- entry gate: only what is known at the moment of deciding.
        let mut why: BTreeMap<&str, usize> = BTreeMap::new();
        let mut entries: Vec<Buy> = Vec::new();
        for (e, tb, t0) in &moves {
            let mut skip = |k: &'static str| *why.entry(k).or_default() += 1;
            let td = t0 + reaction;
            let dir = e.direction as f64;
            let (Some(b_pre), Some(b_now)) = (book_prob(&c, tb, e.match_id, &e.outcome, t0 - PRE_MS), book_prob(&c, tb, e.match_id, &e.outcome, td)) else {
                skip("no_book_price");
                continue;
            };
            let (Some(q0), Some(q)) = (quotes.at(e.match_id, &e.outcome, t0 - PRE_MS), quotes.at(e.match_id, &e.outcome, td)) else {
                skip("no_market_price");
                continue;
            };
            // Either side came back from a gap between "before" and the fill: the "move" and the
            // market's "delay" are both just what changed while it was not being recorded.
            let t_end = td + confirm_ms + fill_ms;
            if reopened(&c, tb, e.match_id, &e.outcome, t0 - PRE_MS, t_end) || reopened(&c, base, e.match_id, &e.outcome, t0 - PRE_MS, t_end) {
                skip("data_gap");
                continue;
            }
            // The bookmaker's move as it stands when we act (it may have partly reverted).
            let bm = (b_now - b_pre) * dir;
            if bm < 0.01 {
                skip("book_reverted");
                continue;
            }
            let (pre_mid, mid) = ((q0.0 + q0.1) / 2.0, (q.0 + q.1) / 2.0);
            let followed = (mid - pre_mid) * dir / bm;
            if followed > g.max_followed {
                skip("already_moved");
                continue;
            }
            // Independent bookmakers (distinct platforms) that had made the move by now.
            let books = confirmations(e, td);
            if books < g.min_books {
                skip("too_few_books");
                continue;
            }
            if g.need_real && q.3 == "assumed" {
                skip("price_unknown");
                continue;
            }
            let spread = q.1 - q.0;
            if spread > g.max_spread + 1e-9 {
                skip("spread_too_wide");
                continue;
            }
            let age = td - q.2;
            if age > g.max_age_ms {
                skip("market_quiet");
                continue;
            }
            // Expected sale: the market reaches `catch` of the move, sold at the bid then, minus slippage.
            let target_mid = pre_mid + dir * bm * catch;
            let (dec_entry, exp_exit) = if dir > 0.0 { (q.1 + slip, target_mid - spread / 2.0 - slip) } else { (1.0 - q.0 + slip, 1.0 - (target_mid + spread / 2.0) - slip) };
            if !(0.02..=0.98).contains(&dec_entry) {
                skip("price_extreme");
                continue;
            }
            if dec_entry < g.min_entry {
                skip("price_too_low");
                continue;
            }
            let n = stake / dec_entry;
            let edge = (n * (exp_exit - dec_entry) - fee(n, dec_entry) - fee(n, exp_exit.clamp(0.0, 1.0))) / n;
            if edge < g.min_edge {
                skip("gap_too_small");
                continue;
            }
            // Test 2: `confirm_ms` later every condition is checked again on fresh prices — the
            // bookmakers still hold the move, still from 2+ independent platforms, the market is
            // still delayed, and the trade still pays. Only when both tests pass does it buy.
            let td2 = td + confirm_ms;
            let (Some(b2), Some(q2)) = (book_prob(&c, tb, e.match_id, &e.outcome, td2), quotes.at(e.match_id, &e.outcome, td2)) else {
                skip("second_test_failed");
                continue;
            };
            let bm2 = (b2 - b_pre) * dir;
            let spread2 = q2.1 - q2.0;
            let followed2 = if bm2 > 0.0 { ((q2.0 + q2.1) / 2.0 - pre_mid) * dir / bm2 } else { 1.0 };
            let target2 = pre_mid + dir * bm2 * catch;
            let (dec2, exp2) = if dir > 0.0 { (q2.1 + slip, target2 - spread2 / 2.0 - slip) } else { (1.0 - q2.0 + slip, 1.0 - (target2 + spread2 / 2.0) - slip) };
            let n2 = stake / dec2.max(0.01);
            let edge2 = (n2 * (exp2 - dec2) - fee(n2, dec2) - fee(n2, exp2.clamp(0.0, 1.0))) / n2;
            let pass2 = bm2 >= 0.01
                && followed2 <= g.max_followed
                && confirmations(e, td2) >= g.min_books
                && spread2 <= g.max_spread + 1e-9
                && !(g.need_real && q2.3 == "assumed")
                && (0.02..=0.98).contains(&dec2)
                && dec2 >= g.min_entry
                && edge2 >= g.min_edge;
            if !pass2 {
                skip("second_test_failed");
                continue;
            }
            let dec_entry = dec2;
            // The order lands fill_ms after the second test at that moment's price (+ slippage), capped.
            let tf = td2 + fill_ms;
            let Some(qf) = quotes.at(e.match_id, &e.outcome, tf) else {
                skip("no_market_price");
                continue;
            };
            let entry = if dir > 0.0 { qf.1 + slip } else { 1.0 - qf.0 + slip };
            if entry > dec_entry + cap {
                skip("price_ran");
                continue;
            }
            // What rested on our side when the order landed: buying Yes takes asks, buying No
            // takes Yes bids. Unknown for hours recorded before depth was kept.
            let side = quotes.depth(e.match_id, &e.outcome, tf).and_then(|d| if dir > 0.0 { d.ask_size.zip(d.ask_depth) } else { d.bid_size.zip(d.bid_depth) });
            let (contracts, entry) = match side {
                Some((best, near)) => fill_buy(stake, entry, best, near),
                None => (stake / entry, entry),
            };
            if side.is_some() && contracts * entry < MIN_FILL {
                skip("book_thin");
                continue;
            }
            entries.push(Buy { e, trigger: tb.clone(), tf, pre_mid, book_move: bm, followed: followed.max(0.0), entry, contracts, depth_known: side.is_some(), books, spread, quote_age_ms: age, edge, src: q.3 });
        }
        // One position per move: entries on the same match within 2 minutes are the same event
        // (home, draw and away all reprice on one goal; two book triggers can name one move).
        // Keep the one with the best expected gain.
        entries.sort_by_key(|en| en.tf);
        let mut kept: Vec<Buy> = Vec::new();
        for en in entries {
            match kept.iter_mut().rev().find(|k| k.e.match_id == en.e.match_id && en.tf - k.tf < SAME_MOVE_MS) {
                Some(k) => {
                    *why.entry("same_move").or_default() += 1;
                    if en.edge > k.edge {
                        *k = en;
                    }
                }
                None => kept.push(en),
            }
        }
        let entries = kept;

        // ---- exit: first quote where the market has made `catch` of the move, else give up;
        // the sale lands fill_ms later at that moment's bid, minus slippage.
        let mut per_hold = Vec::new();
        for hold in HOLDS_S {
            let mut trades: Vec<Trade> = Vec::new();
            for en in &entries {
                let e = en.e;
                let dir = e.direction as f64;
                let give_up = en.tf + hold * 1000;
                if give_up + fill_ms > t_now {
                    continue;
                }
                let path = quotes.path(e.match_id, &e.outcome, en.tf, give_up);
                let hit = path.iter().find(|q| caught_up(q, dir, en.pre_mid, catch * en.book_move, en.spread)).map(|q| q.2);
                let (t_sell, reason) = match hit {
                    Some(t) => (t, "caught up"),
                    None => (give_up, "gave up"),
                };
                let Some(qs) = quotes.at(e.match_id, &e.outcome, t_sell + fill_ms) else { continue };
                let exit = (if dir > 0.0 { qs.0 } else { 1.0 - qs.1 }) - slip;
                let n = en.contracts;
                // Selling Yes hits bids; selling No hits Yes asks. Known depth: what does not fit
                // in the book within 2c is sold 5c lower.
                let exit = match quotes.depth(e.match_id, &e.outcome, t_sell + fill_ms).and_then(|d| if dir > 0.0 { d.bid_size.zip(d.bid_depth) } else { d.ask_size.zip(d.ask_depth) }) {
                    Some((best, near)) if en.depth_known => fill_sell(n, exit, best, near),
                    _ => exit,
                };
                let pnl = n * (exit - en.entry) - fee(n, en.entry) - fee(n, exit.max(0.0));
                trades.push(Trade {
                    ts: en.tf,
                    exit_ts: t_sell + fill_ms,
                    match_id: e.match_id,
                    sport: e.sport.clone(),
                    home: e.home.clone(),
                    away: e.away.clone(),
                    outcome: e.outcome.clone(),
                    dir: e.direction,
                    trigger: en.trigger.clone(),
                    book_move: en.book_move,
                    followed: en.followed,
                    entry: en.entry,
                    exit,
                    reason,
                    pnl,
                    lead_ms: e.order.iter().find(|x| x.0 == base).map(|x| x.1 - (en.tf - fill_ms - reaction)),
                    books: en.books,
                    spread: en.spread,
                    quote_age_ms: en.quote_age_ms,
                    edge: en.edge,
                    src: en.src,
                    filled: en.depth_known.then(|| (n * en.entry / stake).min(1.0)),
                });
            }
            trades.sort_by_key(|t| t.ts);
            per_hold.push(summarise(hold, stake, &trades, s.all.unwrap_or(false)));
        }
        let opp = if base == PM { &r.opportunity } else { &r.opportunity_kalshi };
        let mut src: BTreeMap<&str, usize> = BTreeMap::new();
        for en in &entries {
            *src.entry(en.src).or_default() += 1;
        }
        out.push(json!({
            "baseline": base,
            "gate": g,
            "moves": moves.len(),
            "funnel": { "triggers": moves.len(), "skipped": why, "entered": entries.len() },
            "quote_source": src,
            "opportunity": opp,
            "holds": per_hold,
        }));
    }
    Ok(json!({
        "fastest_book": r.fastest_book,
        "trigger": trigger,
        "settings": {
            "reaction_ms": reaction, "fill_ms": fill_ms, "slip": slip, "stake": stake, "pm_fee_bps": pm_fee_bps.map(|f| f * 10_000.0),
            "k_spread": k_half * 2.0, "catch": catch, "cap": cap, "confirm_ms": confirm_ms,
        },
        "snipe": r.snipe,
        "sources": r.sources,
        "baselines": out,
    }))
}

fn summarise(hold: i64, stake: f64, trades: &[Trade], all: bool) -> Value {
    let n = trades.len();
    let wins = trades.iter().filter(|t| t.pnl > 0.0).count();
    let caught = trades.iter().filter(|t| t.reason == "caught up").count();
    let pnl: f64 = trades.iter().map(|t| t.pnl).sum();
    let mut held: Vec<i64> = trades.iter().map(|t| t.exit_ts - t.ts).collect();
    held.sort();
    // Equity curve (by sale time) and max drawdown.
    let mut by_exit: Vec<&Trade> = trades.iter().collect();
    by_exit.sort_by_key(|t| t.exit_ts);
    let (mut cum, mut peak, mut dd) = (0.0f64, 0.0f64, 0.0f64);
    let step = (n / 1500).max(1);
    let mut curve = Vec::new();
    for (i, t) in by_exit.iter().enumerate() {
        cum += t.pnl;
        peak = peak.max(cum);
        dd = dd.max(peak - cum);
        if i % step == 0 || i + 1 == n {
            curve.push((t.exit_ts, (cum * 100.0).round() / 100.0));
        }
    }
    let mut by_sport: BTreeMap<String, Group> = BTreeMap::new();
    let mut by_trigger: BTreeMap<String, Group> = BTreeMap::new();
    let mut by_lead: BTreeMap<String, Group> = BTreeMap::new();
    let mut by_reason: BTreeMap<String, Group> = BTreeMap::new();
    let mut by_depth: BTreeMap<String, Group> = BTreeMap::new();
    for t in trades {
        // How far behind the market really was, known only afterwards.
        let bucket = match t.lead_ms {
            None => "did not follow",
            Some(l) if l <= 0 => "already moved",
            Some(l) if l < 2000 => "0-2 s behind",
            Some(l) if l < 10_000 => "2-10 s behind",
            Some(_) => "10 s+ behind",
        };
        for (m, k) in [(&mut by_sport, t.sport.clone()), (&mut by_trigger, t.trigger.clone()), (&mut by_lead, bucket.to_string()), (&mut by_reason, t.reason.to_string()), (&mut by_depth, (if t.filled.is_some() { "book depth known" } else { "depth not recorded" }).to_string())] {
            let g = m.entry(k).or_default();
            g.trades += 1;
            g.wins += (t.pnl > 0.0) as usize;
            g.pnl += t.pnl;
        }
    }
    let groups = |m: BTreeMap<String, Group>| -> Vec<Value> {
        let mut v: Vec<(String, Group)> = m.into_iter().collect();
        v.sort_by(|a, b| b.1.trades.cmp(&a.1.trades));
        v.into_iter().map(|(k, g)| json!({"key": k, "trades": g.trades, "win_rate": g.wins as f64 / g.trades.max(1) as f64, "pnl": g.pnl})).collect()
    };
    // Dollars actually spent per trade (the recorded order book can fill only part of the stake).
    let spent = |t: &Trade| stake * t.filled.unwrap_or(1.0).max(1e-9);
    let staked: f64 = trades.iter().map(spent).sum();
    let mut rets: Vec<f64> = trades.iter().map(|t| t.pnl / spent(t)).collect();
    rets.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    json!({
        "hold_s": hold,
        "trades": n,
        "wins": wins,
        "caught_up": caught,
        "catch_rate": if n > 0 { Some(caught as f64 / n as f64) } else { None },
        "median_held_ms": held.get(n / 2),
        "win_rate": if n > 0 { Some(wins as f64 / n as f64) } else { None },
        "pnl": pnl,
        "roi": if n > 0 { Some(pnl / staked) } else { None },
        "staked": staked,
        "median_return": rets.get(n / 2),
        "max_drawdown": dd,
        "curve": curve,
        "by_sport": groups(by_sport),
        "by_trigger": groups(by_trigger),
        "by_lead": groups(by_lead),
        "by_reason": groups(by_reason),
        "by_depth": groups(by_depth),
        "recent": trades.iter().rev().take(if all { usize::MAX } else { 60 }).collect::<Vec<_>>(),
    })
}

// ---------------------------------------------------------------- Kalshi quote backfill

/// For hours before real Kalshi quotes were recorded: Kalshi's own 1-minute candles give the
/// real bid/ask close of every minute with activity. Stored as `kalshi_quotes` rows with
/// est = 1 (the simulation takes the spread from them around the mid we logged each second).
/// Idempotent: events that already have candle rows are skipped.
pub async fn kalshi_backfill(app: &Arc<App>, days: i64) -> Result<Value, String> {
    let path = app.db_path.clone();
    let since = now() - days * 86_400_000;
    let events: Vec<(String, String, Ms, Ms, Option<Ms>)> = tokio::task::spawn_blocking(move || -> Result<_, String> {
        let c = crate::store::open_ro(&path).map_err(|e| format!("{e:#}"))?;
        let mut st = c
            .prepare(
                "SELECT b.book_event_id, b.sport, MIN(b.first_seen_ms), MAX(b.last_seen_ms),
                        (SELECT MIN(ts_ms) FROM kalshi_quotes q WHERE q.event = b.book_event_id AND q.est = 0)
                 FROM book_events b
                 WHERE b.book = 'kalshi' AND b.last_seen_ms > ?1
                   AND NOT EXISTS (SELECT 1 FROM kalshi_quotes q WHERE q.event = b.book_event_id AND q.est = 1)
                 GROUP BY b.book_event_id",
            )
            .map_err(|e| e.to_string())?;
        let rows = st.query_map(params![since], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).map_err(|e| e.to_string())?;
        Ok(rows.filter_map(|x| x.ok()).collect())
    })
    .await
    .map_err(|e| e.to_string())??;
    let http = crate::books::http_client();
    const API: &str = "https://api.elections.kalshi.com/trade-api/v2";
    let (mut done, mut rows, mut failed) = (0usize, 0usize, 0usize);
    for (ev, sport, first, last, first_real) in events {
        let Some(sport) = crate::books::SPORTS.iter().chain(crate::books::ESPORTS.iter()).find(|x| **x == sport) else { continue };
        let pause = || tokio::time::sleep(std::time::Duration::from_millis(150));
        let Ok((v, _)) = crate::books::fetch_json(http.get(format!("{API}/events/{ev}?with_nested_markets=true"))).await else {
            failed += 1;
            pause().await;
            continue;
        };
        let mut e = v["event"].clone();
        if e["markets"].as_array().map_or(true, |m| m.is_empty()) {
            e["markets"] = v["markets"].clone();
        }
        let series = e["series_ticker"].as_str().unwrap_or_else(|| ev.split('-').next().unwrap_or("")).to_string();
        let end = first_real.unwrap_or(last).min(now());
        for (outcome, ticker) in crate::books::kalshi::event_contracts(&e, sport) {
            pause().await;
            let url = format!("{API}/series/{series}/markets/{ticker}/candlesticks?start_ts={}&end_ts={}&period_interval=1", (first - 3_600_000) / 1000, end / 1000);
            let Ok((cv, _)) = crate::books::fetch_json(http.get(url)).await else {
                failed += 1;
                continue;
            };
            for k in cv["candlesticks"].as_array().into_iter().flatten() {
                let px = |side: &str| crate::books::num(&k[side]["close_dollars"]).or_else(|| crate::books::num(&k[side]["close"]).map(|c| c / 100.0)).filter(|x| *x > 0.0 && *x < 1.0);
                let Some(ts) = k["end_period_ts"].as_i64() else { continue };
                let w = crate::engine::Write::KQuote { ts_ms: ts * 1000, server_ts_ms: None, event: ev.clone(), outcome, bid: px("yes_bid"), ask: px("yes_ask"), est: true, depth: Default::default() };
                if app.tx.send(w).await.is_ok() {
                    rows += 1;
                }
            }
        }
        done += 1;
    }
    tracing::info!("kalshi backfill: {done} events, {rows} candle rows, {failed} failed requests");
    Ok(json!({"events": done, "rows": rows, "failed": failed}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fills_stop_at_the_book() {
        // $100 at 50c with 100 at the best and 150 within 2c: 100 @ 0.50, then $50 @ 0.52 = 96.15.
        let (n, avg) = fill_buy(100.0, 0.50, 100.0, 150.0);
        assert!((n - 150.0).abs() < 1e-9, "only 150 rest within 2c, got {n}");
        assert!((avg - (50.0 + 50.0 * 0.52) / 150.0).abs() < 1e-9);
        // Plenty of book: the whole stake at the best price.
        let (n, avg) = fill_buy(100.0, 0.50, 1000.0, 1000.0);
        assert!((n - 200.0).abs() < 1e-9 && (avg - 0.50).abs() < 1e-9);
        // Selling 200 into 50 at the best and 100 within 2c: 50 @ .60, 50 @ .58, 100 @ .55.
        let p = fill_sell(200.0, 0.60, 50.0, 100.0);
        assert!((p - (50.0 * 0.60 + 50.0 * 0.58 + 100.0 * 0.55) / 200.0).abs() < 1e-9);
    }

    #[test]
    fn catch_up_is_judged_on_the_sell_price() {
        // Bought Yes after a 10-pt book move from a 0.50 mid, entry spread 2c; need 7.5 pts.
        // Normal quote 0.57/0.59: bid moved 7 pts, within half a spread of 7.5 -> caught up.
        assert!(caught_up(&(0.57, 0.59, 0, "book"), 1.0, 0.50, 0.075, 0.02));
        // Spread blew out 0.33/0.95: mid 0.64 is on target but the bid fell -> not caught up.
        assert!(!caught_up(&(0.33, 0.95, 0, "book"), 1.0, 0.50, 0.075, 0.02));
        // Bought No (Yes falling): judged on the Yes ask.
        assert!(caught_up(&(0.41, 0.43, 0, "book"), -1.0, 0.50, 0.075, 0.02));
        assert!(!caught_up(&(0.05, 0.60, 0, "book"), -1.0, 0.50, 0.075, 0.02));
    }

    #[test]
    fn depth_from_levels() {
        let d = crate::engine::Depth::from_levels(&[(0.40, 10.0), (0.39, 5.0), (0.30, 99.0)], &[(0.42, 7.0), (0.44, 3.0), (0.50, 50.0)]);
        assert_eq!((d.bid_size, d.bid_depth, d.ask_size, d.ask_depth), (Some(10.0), Some(15.0), Some(7.0), Some(10.0)));
    }

    #[test]
    fn kalshi_fee_rounds_up_to_the_cent() {
        // 100 contracts at 50c: 0.07 * 100 * 0.25 = 1.75
        assert!((kalshi_fee(100.0, 0.5) - 1.75).abs() < 1e-9);
        assert!((kalshi_fee(1.0, 0.5) - 0.02).abs() < 1e-9);
    }
}
