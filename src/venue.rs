//! Live state of the two prediction markets that the paper arbitrage needs and the database does
//! not keep: every contract's full order book, when each venue's socket last spoke, and each
//! market's fee schedule and settlement rules. The feeds (books/polymarket.rs, books/kalshi.rs)
//! publish; predarb.rs reads.
//!
//! Contracts are keyed by the venue's own id (Polymarket token id, Kalshi market ticker).
//! Polymarket tokens are also indexed by (match id, outcome in the match's orientation), set by the
//! feed once the engine has linked the game; Kalshi tickers by (event ticker, Kalshi's own
//! outcome), the same orientation `kalshi_quotes` keeps.

use crate::model::*;
use parking_lot::RwLock;
use std::collections::HashMap;
use std::sync::LazyLock;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum V {
    Pm,
    Kalshi,
}

/// One contract's Yes book: bids best (highest) first, asks best (lowest) first, (price, size).
#[derive(Clone, Debug, Default)]
pub struct Ladder {
    pub bids: Vec<(f64, f64)>,
    pub asks: Vec<(f64, f64)>,
    /// When this book last changed (our clock).
    pub ts: Ms,
}

/// Taker fee for `n` contracts at price `p`: n x rate x (p(1-p))^exponent. Polymarket publishes
/// this per market (`feeSchedule`); Kalshi's game series are 0.07 x p(1-p), rounded up to the cent.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fee {
    pub rate: f64,
    pub exponent: f64,
}

impl Fee {
    pub const NONE: Fee = Fee { rate: 0.0, exponent: 1.0 };
    /// Polymarket sports markets (feeSchedule rate 0.05, exponent 1, taker only) since July 2026.
    pub const PM_SPORTS: Fee = Fee { rate: 0.05, exponent: 1.0 };
    /// Fee in dollars for one fill (no rounding; see `pm_fee` / `snipe::kalshi_fee`).
    pub fn raw(&self, n: f64, p: f64) -> f64 {
        let q = (p * (1.0 - p)).max(0.0);
        n * self.rate * if self.exponent == 1.0 { q } else { q.powf(self.exponent) }
    }
}

/// Polymarket fee for one fill, rounded to 5 decimals as Polymarket does.
pub fn pm_fee(fee: Fee, n: f64, p: f64) -> f64 {
    (fee.raw(n, p) * 100_000.0).round() / 100_000.0
}

#[derive(Clone, Debug, Default)]
pub struct Meta {
    /// None = the market did not say (treated as unknown, never as free).
    pub fee: Option<Fee>,
    /// Settlement rules as the venue writes them (Polymarket description; Kalshi rules_primary +
    /// rules_secondary). Parsed in settle.rs.
    pub rules: String,
    /// The market's question / title, for the record.
    pub title: String,
    /// Polymarket: the token that pays when this one does not (two-way: the other side's token;
    /// football Yes/No markets: the No token). Buying "No" means buying this.
    pub complement: Option<String>,
}

#[derive(Default)]
struct Venue {
    books: HashMap<String, Ladder>,
    meta: HashMap<String, Meta>,
    /// Polymarket: (match id, match-oriented outcome) -> token. Kalshi: unused.
    by_match: HashMap<(i64, Outcome), String>,
    /// Kalshi: (event ticker, Kalshi outcome) -> market ticker. Polymarket: unused.
    by_event: HashMap<(String, Outcome), String>,
    /// Last message on the venue's price socket.
    alive_ms: Ms,
}

static PM: LazyLock<RwLock<Venue>> = LazyLock::new(Default::default);
/// Woken on every book change (the arbitrage bot scans on it).
pub static CHANGED: LazyLock<tokio::sync::Notify> = LazyLock::new(tokio::sync::Notify::new);
static KALSHI: LazyLock<RwLock<Venue>> = LazyLock::new(Default::default);

fn venue(v: V) -> &'static RwLock<Venue> {
    match v {
        V::Pm => &PM,
        V::Kalshi => &KALSHI,
    }
}

/// The venue's socket delivered a message now.
pub fn alive(v: V, now: Ms) {
    let mut g = venue(v).write();
    if now > g.alive_ms {
        g.alive_ms = now;
    }
}

pub fn alive_ms(v: V) -> Ms {
    venue(v).read().alive_ms
}

/// How late Polymarket's last 8 socket messages arrived (receive time minus Polymarket's own
/// timestamp), and how many have been seen.
static PM_LAGS: std::sync::LazyLock<parking_lot::Mutex<([Ms; 8], usize)>> = std::sync::LazyLock::new(|| parking_lot::Mutex::new(([0; 8], 0)));

/// One Polymarket message arrived `lag` ms after its own timestamp.
pub fn pm_lag(lag: Ms) {
    let mut g = PM_LAGS.lock();
    let i = g.1 % 8;
    g.0[i] = lag;
    g.1 += 1;
}

/// The backlog of Polymarket's price socket (ms): the SMALLEST delay among its last 8 messages.
/// When the reader falls behind, every message is late; one old timestamp among fresh ones (a
/// book sent again, a quiet market) says nothing. 0 until 8 messages have arrived.
pub fn pm_lag_ms() -> Ms {
    let g = PM_LAGS.lock();
    if g.1 < 8 {
        return 0;
    }
    g.0.iter().copied().min().unwrap_or(0)
}

/// A contract's book changed. Bids and asks in any order; stored best first.
pub fn publish(v: V, contract: &str, mut bids: Vec<(f64, f64)>, mut asks: Vec<(f64, f64)>, now: Ms) {
    bids.retain(|l| l.1 > 0.0 && l.0 > 0.0 && l.0 < 1.0);
    asks.retain(|l| l.1 > 0.0 && l.0 > 0.0 && l.0 < 1.0);
    bids.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
    asks.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    venue(v).write().books.insert(contract.to_string(), Ladder { bids, asks, ts: now });
    CHANGED.notify_one();
}

pub fn set_meta(v: V, contract: &str, m: Meta) {
    venue(v).write().meta.insert(contract.to_string(), m);
}

/// Polymarket: the token of a match outcome (match orientation).
pub fn map_match(match_id: i64, outcome: Outcome, token: &str) {
    let mut g = PM.write();
    if g.by_match.get(&(match_id, outcome)).map(|t| t.as_str()) != Some(token) {
        g.by_match.insert((match_id, outcome), token.to_string());
    }
}

/// Kalshi: (event, outcome) -> ticker for every contract discovery found.
pub fn map_events(list: impl IntoIterator<Item = (String, Outcome, String)>) {
    let mut g = KALSHI.write();
    for (ev, o, tk) in list {
        g.by_event.insert((ev, o), tk);
    }
}

/// Everything about one contract: (contract id, book, meta).
pub type Contract = (String, Ladder, Meta);

fn get(g: &Venue, id: &str) -> Option<Contract> {
    let book = g.books.get(id)?.clone();
    let meta = g.meta.get(id).cloned().unwrap_or_default();
    Some((id.to_string(), book, meta))
}

pub fn pm_contract(match_id: i64, outcome: Outcome) -> Option<Contract> {
    let g = PM.read();
    let id = g.by_match.get(&(match_id, outcome))?;
    get(&g, id)
}

/// Every Polymarket contract linked to a match: (match, outcome, contract).
pub fn pm_all() -> Vec<(i64, Outcome, Contract)> {
    let g = PM.read();
    g.by_match.iter().filter_map(|((m, o), id)| get(&g, id).map(|c| (*m, *o, c))).collect()
}

pub fn kalshi_contract(event: &str, outcome: Outcome) -> Option<Contract> {
    let g = KALSHI.read();
    let id = g.by_event.get(&(event.to_string(), outcome))?;
    get(&g, id)
}

pub fn outcome_of(s: &str) -> Option<Outcome> {
    match s {
        "home" => Some(Outcome::Home),
        "away" => Some(Outcome::Away),
        "draw" => Some(Outcome::Draw),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pm_sports_fee_matches_the_published_example() {
        // 100 shares at 50c, sports rate 0.05: $1.25.
        assert!((pm_fee(Fee::PM_SPORTS, 100.0, 0.5) - 1.25).abs() < 1e-9);
        assert!((pm_fee(Fee::PM_SPORTS, 100.0, 0.9) - 0.45).abs() < 1e-9);
    }
}

#[cfg(test)]
mod lag_tests {
    use super::*;

    #[test]
    fn the_backlog_is_the_smallest_recent_delay() {
        // Fewer than 8 messages: nothing is known yet.
        for _ in 0..7 {
            pm_lag(2_500);
        }
        assert_eq!(pm_lag_ms(), 0);
        // The reader is behind: every message is late.
        pm_lag(2_400);
        assert_eq!(pm_lag_ms(), 2_400);
        // One on-time message among late ones: no backlog (a backlog delays every message).
        pm_lag(-60);
        assert_eq!(pm_lag_ms(), -60);
        // Back on time, with one old timestamp (a book sent again): still no backlog.
        for _ in 0..7 {
            pm_lag(30);
        }
        pm_lag(40_000);
        assert_eq!(pm_lag_ms(), 30);
    }
}
