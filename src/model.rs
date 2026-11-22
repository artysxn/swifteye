//! Normalised types every bookmaker adapter produces, and the change records
//! the engine derives from them.

use serde::Serialize;

pub type Ms = i64;

/// NTP-corrected wall time (see clock.rs).
pub fn now_ms() -> Ms {
    crate::clock::now_ms()
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Serialize, PartialOrd, Ord)]
pub enum Market {
    /// Full-time 1X2 (three-way result).
    #[serde(rename = "1x2")]
    X12,
    /// Full-time total goals over/under; `line` is the goal line.
    #[serde(rename = "total")]
    Total,
    /// Full-time handicap; `line` is the HOME team's handicap.
    #[serde(rename = "handicap")]
    Handicap,
    /// Both teams to score.
    #[serde(rename = "btts")]
    Btts,
}

impl Market {
    pub fn as_str(self) -> &'static str {
        match self {
            Market::X12 => "1x2",
            Market::Total => "total",
            Market::Handicap => "handicap",
            Market::Btts => "btts",
        }
    }
    pub fn parse(s: &str) -> Option<Market> {
        Some(match s {
            "1x2" => Market::X12,
            "total" => Market::Total,
            "handicap" => Market::Handicap,
            "btts" => Market::Btts,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug, Serialize, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    Home,
    Draw,
    Away,
    Over,
    Under,
    Yes,
    No,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Home => "home",
            Outcome::Draw => "draw",
            Outcome::Away => "away",
            Outcome::Over => "over",
            Outcome::Under => "under",
            Outcome::Yes => "yes",
            Outcome::No => "no",
        }
    }
}

/// Lines are keyed in hundredths so they can be hashed exactly (2.25 -> 225).
pub fn line_key(line: Option<f64>) -> i32 {
    match line {
        Some(l) => (l * 100.0).round() as i32,
        None => i32::MIN,
    }
}

pub fn line_from_key(k: i32) -> Option<f64> {
    if k == i32::MIN {
        None
    } else {
        Some(k as f64 / 100.0)
    }
}

/// One price as an adapter saw it.
#[derive(Clone, Debug)]
pub struct QuoteIn {
    pub market: Market,
    pub line: Option<f64>,
    pub outcome: Outcome,
    /// `None` = listed but suspended / not bettable right now.
    pub price: Option<f64>,
    /// The bookmaker's own change timestamp, when the feed carries one.
    pub book_ts: Option<Ms>,
}

impl QuoteIn {
    pub fn new(market: Market, line: Option<f64>, outcome: Outcome, price: Option<f64>) -> Self {
        // Treat 0 / 1.0 / garbage as suspended: no book offers evens-or-worse as a live price.
        let price = price.filter(|p| p.is_finite() && *p > 1.0);
        QuoteIn { market, line, outcome, price, book_ts: None }
    }
    pub fn ts(mut self, ts: Option<Ms>) -> Self {
        self.book_ts = ts;
        self
    }
}

/// One live event as an adapter saw it.
#[derive(Clone, Debug, Default)]
pub struct EventIn {
    pub id: String,
    pub sport: String,
    pub league: String,
    pub home: String,
    pub away: String,
    pub start_ms: Option<Ms>,
    /// Sportradar match id when the book exposes it; used for exact matching.
    pub sr_id: Option<String>,
    /// The id is probably Sportradar but not guaranteed (Polymarket gameId): use it
    /// for an exact link when it hits, but never let it block a name match.
    pub sr_weak: bool,
    /// In play (true) or not started yet (false).
    pub live: bool,
    pub score: Option<String>,
    pub clock: Option<String>,
    pub quotes: Vec<QuoteIn>,
}

/// The full live list one poll of a book returned.
/// A bookmaker's price for a PART of a game (map 1, first set, first half, a quarter, a period).
/// Recorded for research only: it never enters the engine, so it is not matched, not shown, not
/// broadcast and no bot can see it. `event` = the book's id of the whole game.
#[derive(Clone, Debug)]
pub struct PartQuote {
    pub event: String,
    pub kind: &'static str,
    pub line: f64,
    pub outcome: Outcome,
    /// Decimal odds; None = not offered right now.
    pub price: Option<f64>,
}

pub struct Snapshot {
    pub events: Vec<EventIn>,
    /// When the response body finished arriving (ms since epoch).
    pub recv_ms: Ms,
}

#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ChangeKind {
    /// First time this price was seen.
    Open,
    /// Price moved.
    Move,
    /// Price went from a number to suspended.
    Suspend,
    /// Price came back from suspended.
    Resume,
    /// Line or event disappeared from the feed.
    Close,
}

impl ChangeKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ChangeKind::Open => "open",
            ChangeKind::Move => "move",
            ChangeKind::Suspend => "suspend",
            ChangeKind::Resume => "resume",
            ChangeKind::Close => "close",
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Change {
    pub ts_ms: Ms,
    pub book: &'static str,
    pub book_event_id: String,
    pub match_id: i64,
    pub market: Market,
    pub line: Option<f64>,
    pub outcome: Outcome,
    pub kind: ChangeKind,
    pub old_price: Option<f64>,
    pub new_price: Option<f64>,
    pub book_ts_ms: Option<Ms>,
}
