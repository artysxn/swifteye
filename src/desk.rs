//! Order desk: one way to place a bet at any bookmaker or prediction market (24 Nov 2026).
//!
//! Something (the owner, a script, later a bot) hands in a TICKET: one or more LEGS, each "back
//! this outcome at this book for at least these odds with this stake". The desk places the legs
//! one at a time, in the order given, and never starts a leg before the one before it is in.
//!
//! Who places a leg:
//! * polymarket: Swifteye itself, through exec.rs (the same signed order the snipe bot sends).
//! * every other book: a DRIVER, the owner's own program running where he is signed in to that
//!   bookmaker (a userscript, an extension, a Playwright script). The driver asks the desk for
//!   work (`GET /desk/next`), places the bet on the site and reports back (`POST /desk/result`).
//!   Swifteye never holds a bookmaker login and never talks to a bookmaker's betting pages itself.
//!
//! Modes of a ticket:
//! * paper (default): nothing leaves Swifteye. A leg "fills" at the price Swifteye has recorded
//!   for that book right now (Polymarket: what rests in its order book), or is refused.
//! * dry: every leg goes to its driver at once, flagged `dry_run`: the driver loads the bet slip,
//!   reads the odds and the largest stake the site offers, and reports them WITHOUT confirming.
//!   This is how to learn whether a recorded price can really be had.
//! * real: bets are placed. Off until the owner switches it on, for the desk and per book, each
//!   with its own largest stake and 24-hour cap (`POST /desk/settings`, "confirm": "REAL MONEY").
//!   The bots' kill switch stops real tickets too.
//!
//! What can go wrong, and what the desk does:
//! * a leg is refused, fails or nobody takes it in time: the legs after it are cancelled. With no
//!   leg in, the ticket is `dead` (nothing was bet). With an earlier leg in, it is `exposed`: a
//!   bet stands with nothing against it, and the owner is told (Telegram, real money only).
//! * a driver takes a leg and never answers: the leg is `unknown` (it MAY be placed) and the
//!   ticket `check`. An order is NEVER handed out twice: the owner looks at the site and reports
//!   the result by hand (`POST /desk/result` takes it late).
//! * a leg comes in at other odds or a smaller stake than planned: the legs after it are re-sized
//!   so every leg still pays the same (`rebalance`, on by default).
//!
//! Stored next to the database: `desk.json` (settings and the key every driver must send as
//! `X-Desk-Key`; private, like telegram.json) and `desk_tickets.jsonl` (every state of every
//! ticket, one line each; the last line of a ticket is its current state).
//!
//! BOOKMAKER LOGINS (`desk_accounts.json`, private like pinnacle.json): the owner types each
//! bookmaker's email / username and password once, on the Order desk page. Swifteye never signs
//! in anywhere with them and the API never shows a password again. The only reader is the
//! book's driver: `GET /desk/account?book=` with the desk key, when the site has signed it out.
//! The driver says how that went (`POST /desk/account/state`): `signed_in`, `signed_out`,
//! `refused` (the site says the login is wrong) or `needs_owner` (a captcha, an emailed code, an
//! identity check: things only the owner may do). After `refused` or `needs_owner` the login is
//! NOT handed out again until the owner has acted (saved it again, or signed in by hand and the
//! driver reported `signed_in`): repeated failed sign-ins lock accounts. It is also not handed
//! out more than `HAND_MAX` times without a `signed_in` in between. New dry and real tickets for
//! such a book are refused, and the owner gets a Telegram message.
//!
//! The Polymarket leg's real path was NOT exercised against the live venue while this was
//! written (no real order was placed); it calls the same `exec::pm_order` the snipe bot uses.

use crate::model::*;
use crate::App;
use anyhow::{bail, ensure, Context, Result};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

pub const PM: &str = "polymarket";
/// The name a leg carries when Swifteye itself handled it (paper fills, Polymarket).
const SELF: &str = "swifteye";
const MAX_LEGS: usize = 6;
/// Tickets kept in memory (all of them stay in the journal).
const KEEP: usize = 2_000;
/// Polymarket's smallest order, in shares.
const PM_MIN_SHARES: f64 = 5.0;
/// After a Polymarket order that reported no fill: wait this long, then look at the account.
const RECHECK_MS: u64 = 4_000;
/// Legs that should pay the same are "uneven" when one pays this much less than another.
const UNEVEN: f64 = 0.97;

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    #[default]
    Paper,
    Dry,
    Real,
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum St {
    /// A later leg: not handed out until the leg before it is in.
    #[default]
    Waiting,
    /// Waiting for its book's driver to take it.
    Queued,
    /// A driver has it; no result yet.
    Claimed,
    /// Real: the bet is in. Dry: the slip would take it. Paper: filled at the recorded price.
    Placed,
    /// The book said no (price gone, limit too low, market closed). Nothing was bet.
    Rejected,
    /// The driver hit an error and says nothing was bet.
    Failed,
    /// A driver took it and never answered. It MAY be placed: check the site.
    Unknown,
    /// No driver took it in time. Nothing was bet.
    Expired,
    /// Cancelled before a driver took it.
    Cancelled,
}

impl St {
    fn open(self) -> bool {
        matches!(self, St::Waiting | St::Queued | St::Claimed)
    }
}

#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TicketSt {
    #[default]
    Open,
    /// Every leg is in (and the legs pay about the same).
    Done,
    /// No leg is in: nothing was bet.
    Dead,
    /// Some legs are in and one is not, or they pay unevenly: a bet stands unhedged.
    Exposed,
    /// A leg's fate is unknown: look at the site, then report it.
    Check,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Leg {
    pub id: u64,
    pub ticket: u64,
    /// Position in the ticket, from 0.
    pub n: usize,
    pub mode: Mode,
    pub book: String,
    pub match_id: i64,
    /// 1x2 (also the winner of a two-way game), total, handicap, btts.
    pub market: String,
    /// Total: the goal line. Handicap: the HOME side's handicap, in the match's orientation.
    pub line: Option<f64>,
    /// home, draw, away, over, under, yes, no: in the MATCH's orientation (see `info.book_outcome`).
    pub outcome: String,
    /// back (a bookmaker bet on `outcome`), yes (Polymarket: buy the outcome), no (Polymarket:
    /// buy AGAINST the outcome).
    pub side: String,
    /// The lowest decimal odds to accept. For Polymarket: after its fee.
    pub min_odds: f64,
    /// The odds the ticket was built on (at least `min_odds`).
    pub odds_seen: f64,
    /// Money to put on, in the book's account currency.
    pub stake: f64,
    pub state: St,
    pub created_ms: Ms,
    pub released_ms: Ms,
    /// A queued leg nobody has taken by then expires.
    pub expires_ms: Ms,
    pub claimed_ms: Ms,
    pub done_ms: Ms,
    /// Who took it.
    pub driver: String,
    /// What the driver needs to find the bet on the site (`describe`).
    pub info: Value,
    /// Result: the odds the bet went in at (or, refused, the odds on offer).
    pub odds: Option<f64>,
    /// Result: money actually staked.
    pub filled: Option<f64>,
    /// Result: the largest stake the slip offered, when the driver read it.
    pub max_stake: Option<f64>,
    /// The book's own id for the bet.
    pub bet_ref: String,
    pub message: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Ticket {
    pub id: u64,
    pub created_ms: Ms,
    pub done_ms: Ms,
    pub mode: Mode,
    pub state: TicketSt,
    pub note: String,
    pub rebalance: bool,
    /// How long each leg may wait for its driver.
    pub ttl_s: i64,
    pub legs: Vec<Leg>,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct LegReq {
    pub book: String,
    pub match_id: i64,
    pub market: Option<String>,
    pub line: Option<f64>,
    pub outcome: String,
    pub side: Option<String>,
    pub min_odds: f64,
    pub odds: Option<f64>,
    pub stake: f64,
    /// The driver's directions (`describe`), filled in by Swifteye, never by the caller.
    #[serde(skip)]
    pub info: Value,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct TicketReq {
    pub mode: Option<Mode>,
    pub note: Option<String>,
    pub rebalance: Option<bool>,
    pub ttl_s: Option<i64>,
    pub legs: Vec<LegReq>,
}

/// What a driver says about an order it took.
#[derive(Clone, Debug, Default, Deserialize)]
pub struct Report {
    pub id: u64,
    /// placed | rejected (also price_changed, limit, closed) | failed (also error)
    pub status: String,
    pub odds: Option<f64>,
    pub stake: Option<f64>,
    pub max_stake: Option<f64>,
    pub bet_ref: Option<String>,
    pub message: Option<String>,
    pub driver: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct BookSet {
    /// Off = every order for this book is refused, paper too.
    pub enabled: bool,
    /// Real bets allowed at this book (the desk's own `real` must be on as well).
    pub real: bool,
    /// Largest real stake for one bet, in the account's currency.
    pub max_stake: f64,
    /// Most real money sent to this book in any 24 hours.
    pub daily_cap: f64,
    pub currency: String,
}

impl Default for BookSet {
    fn default() -> Self {
        BookSet { enabled: true, real: false, max_stake: 0.0, daily_cap: 0.0, currency: String::new() }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// Every driver and every change must send this as `X-Desk-Key`.
    pub key: String,
    pub real: bool,
    /// A queued leg nobody takes within this many seconds expires.
    pub ttl_s: i64,
    /// A driver that took a leg must answer within this many seconds, or the leg is `unknown`.
    pub claim_s: i64,
    /// A paper fill needs a price recorded within this many seconds.
    pub fresh_s: i64,
    pub books: BTreeMap<String, BookSet>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { key: String::new(), real: false, ttl_s: 60, claim_s: 90, fresh_s: 60, books: BTreeMap::new() }
    }
}

/// One bookmaker login, typed by the owner (never by Swifteye), kept in `desk_accounts.json`.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct Account {
    pub login: String,
    pub password: String,
    pub note: String,
    pub saved_ms: Ms,
    /// Goes up on every save.
    pub generation: u64,
    /// What the book's driver last said: "" (nothing yet), signed_in, signed_out, refused,
    /// needs_owner.
    pub state: String,
    pub detail: String,
    pub state_ms: Ms,
    /// Times this login went to a driver since the last `signed_in` (or the last save).
    pub handed: u64,
    pub handed_ms: Ms,
}

/// A login goes to a driver at most this many times without a `signed_in` in between.
const HAND_MAX: u64 = 3;

fn mask_login(l: &str) -> String {
    let (name, domain) = l.split_once('@').map_or((l, ""), |(a, b)| (a, b));
    let head: String = name.chars().take(2).collect();
    if l.is_empty() {
        String::new()
    } else if domain.is_empty() {
        format!("{head}…")
    } else {
        format!("{head}…@{domain}")
    }
}

/// The driver last seen asking for a book's orders.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Driver {
    pub name: String,
    pub last_ms: Ms,
    pub polls: u64,
    pub taken: u64,
}

#[derive(Default)]
pub struct Desk {
    pub set: Settings,
    pub tickets: Vec<Ticket>,
    pub drivers: BTreeMap<String, Driver>,
    pub accounts: BTreeMap<String, Account>,
    acc_dirty: bool,
    next_id: u64,
    /// Tickets changed since they were last written to the journal.
    dirty: Vec<u64>,
    set_dirty: bool,
    /// What the owner should hear about (real money only), oldest first.
    alerts: Vec<String>,
}

fn r2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

fn parse_outcome(s: &str) -> Option<Outcome> {
    Some(match s {
        "home" => Outcome::Home,
        "draw" => Outcome::Draw,
        "away" => Outcome::Away,
        "over" => Outcome::Over,
        "under" => Outcome::Under,
        "yes" => Outcome::Yes,
        "no" => Outcome::No,
        _ => return None,
    })
}

fn new_key() -> String {
    let b: [u8; 16] = rand::random();
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn cut(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

impl Desk {
    fn id(&mut self) -> u64 {
        self.next_id = self.next_id.max(1);
        let i = self.next_id;
        self.next_id += 1;
        i
    }

    fn idx(&self, ticket: u64) -> Option<usize> {
        self.tickets.iter().position(|t| t.id == ticket)
    }

    fn find_leg(&self, leg: u64) -> Option<(usize, usize)> {
        self.tickets.iter().enumerate().find_map(|(ti, t)| t.legs.iter().position(|l| l.id == leg).map(|li| (ti, li)))
    }

    fn touch(&mut self, ticket: u64) {
        if !self.dirty.contains(&ticket) {
            self.dirty.push(ticket);
        }
    }

    pub fn book(&self, name: &str) -> BookSet {
        self.set.books.get(name).cloned().unwrap_or_default()
    }

    pub fn ticket(&self, id: u64) -> Option<&Ticket> {
        self.idx(id).map(|i| &self.tickets[i])
    }

    /// Real money sent, or still on its way, to one book in the last 24 hours.
    pub fn real_24h(&self, book: &str, now: Ms) -> f64 {
        let mut sum = 0.0;
        for t in &self.tickets {
            if t.mode != Mode::Real || now - t.created_ms > 86_400_000 {
                continue;
            }
            for l in t.legs.iter().filter(|l| l.book == book) {
                sum += match l.state {
                    St::Placed => l.filled.unwrap_or(l.stake),
                    St::Waiting | St::Queued | St::Claimed | St::Unknown => l.stake,
                    _ => 0.0,
                };
            }
        }
        sum
    }

    /// Take in a ticket. `kill` = the bots' kill switch. Returns the ticket's id.
    pub fn create(&mut self, req: TicketReq, now: Ms, kill: bool) -> Result<u64> {
        ensure!(!req.legs.is_empty() && req.legs.len() <= MAX_LEGS, "a ticket has 1 to {MAX_LEGS} legs");
        let mode = req.mode.unwrap_or_default();
        if mode == Mode::Real {
            ensure!(self.set.real, "real bets are switched off on the desk");
            ensure!(!kill, "the kill switch is on");
        }
        let ttl_s = req.ttl_s.unwrap_or(self.set.ttl_s).clamp(5, 3_600);
        let mut legs = Vec::new();
        let mut adding: BTreeMap<String, f64> = BTreeMap::new();
        for (n, l) in req.legs.into_iter().enumerate() {
            let k = n + 1;
            let book = l.book.trim().to_ascii_lowercase();
            ensure!(!book.is_empty() && book.len() <= 40 && book.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'), "leg {k}: bad book name");
            let market = l.market.unwrap_or_else(|| "1x2".into());
            ensure!(Market::parse(&market).is_some(), "leg {k}: market must be 1x2, total, handicap or btts");
            ensure!(parse_outcome(&l.outcome).is_some(), "leg {k}: outcome must be home, draw, away, over, under, yes or no");
            let side = l.side.unwrap_or_else(|| if book == PM { "yes".into() } else { "back".into() });
            ensure!(matches!(side.as_str(), "back" | "yes" | "no"), "leg {k}: side must be back, yes or no");
            ensure!(book == PM || side == "back", "leg {k}: only Polymarket takes yes / no; a bookmaker leg is a back");
            ensure!(book != PM || side != "back", "leg {k}: a Polymarket leg is yes or no");
            ensure!(l.min_odds.is_finite() && l.min_odds > 1.0 && l.min_odds <= 1_000.0, "leg {k}: min_odds must be decimal odds over 1");
            let odds_seen = l.odds.unwrap_or(l.min_odds);
            ensure!(odds_seen.is_finite() && odds_seen >= l.min_odds - 1e-9 && odds_seen <= 1_000.0, "leg {k}: odds must be at least min_odds");
            ensure!(l.stake.is_finite() && l.stake > 0.0, "leg {k}: stake must be over 0");
            let bs = self.book(&book);
            ensure!(bs.enabled, "{book}: orders are switched off for this book");
            if mode != Mode::Paper {
                if let Some(a) = self.accounts.get(&book).filter(|a| matches!(a.state.as_str(), "refused" | "needs_owner")) {
                    bail!("{book}: its driver is not signed in ({}{}{}); see the Order desk page", a.state.replace('_', " "), if a.detail.is_empty() { "" } else { ": " }, a.detail);
                }
            }
            if mode == Mode::Real {
                ensure!(bs.real, "{book}: real bets are not switched on for this book");
                ensure!(l.stake <= bs.max_stake + 1e-9, "{book}: a stake of {} is over this book's limit of {}", l.stake, bs.max_stake);
                let used = self.real_24h(&book, now) + adding.get(&book).copied().unwrap_or(0.0);
                ensure!(used + l.stake <= bs.daily_cap + 1e-9, "{book}: {used:.2} already sent in 24 hours, {} more would pass the cap of {}", l.stake, bs.daily_cap);
                *adding.entry(book.clone()).or_default() += l.stake;
            }
            legs.push(Leg { n, mode, book, match_id: l.match_id, market, line: l.line, outcome: l.outcome, side, min_odds: l.min_odds, odds_seen, stake: r2(l.stake), created_ms: now, info: l.info, ..Default::default() });
        }
        let tid = self.id();
        for l in &mut legs {
            l.id = self.id();
            l.ticket = tid;
        }
        self.tickets.push(Ticket { id: tid, created_ms: now, done_ms: 0, mode, state: TicketSt::Open, note: cut(req.note.as_deref().unwrap_or(""), 300), rebalance: req.rebalance.unwrap_or(true), ttl_s, legs });
        let ti = self.tickets.len() - 1;
        if mode == Mode::Dry {
            // A dry run bets nothing, so the legs do not wait for each other: every slip is read
            // at the same moment, which is what shows whether the prices exist together.
            for li in 0..self.tickets[ti].legs.len() {
                self.release(ti, li, now);
            }
        }
        self.advance(ti, now);
        self.touch(tid);
        if self.tickets.len() > KEEP {
            // Finished tickets fall out of memory oldest first; open ones never do.
            let extra = self.tickets.len() - KEEP;
            let mut dropped = 0;
            self.tickets.retain(|t| {
                if dropped < extra && t.state != TicketSt::Open && t.state != TicketSt::Check {
                    dropped += 1;
                    false
                } else {
                    true
                }
            });
        }
        Ok(tid)
    }

    /// Hand a leg to its book's queue. A later leg of a real or paper ticket is first re-sized
    /// to what the first leg really came in at, so the legs still pay the same.
    fn release(&mut self, ti: usize, li: usize, now: Ms) {
        let cap = self.book(&self.tickets[ti].legs[li].book).max_stake;
        let t = &mut self.tickets[ti];
        let ttl = t.ttl_s;
        if li > 0 && t.rebalance && t.mode != Mode::Dry {
            let a = &t.legs[0];
            if let (Some(f), Some(o)) = (a.filled, a.odds) {
                let (planned, got) = (a.stake * a.odds_seen, f * o);
                if planned > 0.0 && (got / planned - 1.0).abs() > 0.002 {
                    let l = &mut t.legs[li];
                    let was = l.stake;
                    l.stake = r2(l.stake * got / planned).max(0.01);
                    l.message = format!("stake {was} -> {} (leg 1 came in at {o} for {f})", l.stake);
                }
            }
        }
        let l = &mut t.legs[li];
        if l.mode == Mode::Real && l.stake > cap + 1e-9 {
            l.message = format!("stake cut from {} to this book's limit of {cap}", l.stake);
            l.stake = r2(cap);
        }
        l.state = St::Queued;
        l.released_ms = now;
        l.expires_ms = now + ttl * 1000;
    }

    /// Bring a ticket's state up to date with its legs, and hand out the next leg when it is due.
    fn advance(&mut self, ti: usize, now: Ms) {
        let mode = self.tickets[ti].mode;
        let new = if mode == Mode::Dry {
            let t = &self.tickets[ti];
            if t.legs.iter().any(|l| l.state.open()) {
                TicketSt::Open
            } else if t.legs.iter().all(|l| l.state == St::Placed) {
                TicketSt::Done
            } else if t.legs.iter().any(|l| l.state == St::Unknown) {
                TicketSt::Check
            } else {
                TicketSt::Dead
            }
        } else {
            match self.tickets[ti].legs.iter().position(|l| l.state != St::Placed) {
                None => {
                    let pays: Vec<f64> = self.tickets[ti].legs.iter().map(|l| l.filled.unwrap_or(l.stake) * l.odds.unwrap_or(l.odds_seen)).collect();
                    let (lo, hi) = (pays.iter().cloned().fold(f64::INFINITY, f64::min), pays.iter().cloned().fold(0.0, f64::max));
                    if self.tickets[ti].rebalance && pays.len() > 1 && lo < hi * UNEVEN {
                        TicketSt::Exposed
                    } else {
                        TicketSt::Done
                    }
                }
                Some(i) => match self.tickets[ti].legs[i].state {
                    St::Waiting => {
                        self.release(ti, i, now);
                        TicketSt::Open
                    }
                    St::Queued | St::Claimed => TicketSt::Open,
                    st => {
                        for l in self.tickets[ti].legs.iter_mut().skip(i + 1) {
                            if matches!(l.state, St::Waiting | St::Queued) {
                                l.state = St::Cancelled;
                                l.done_ms = now;
                                l.message = "an earlier leg did not go in".into();
                            }
                        }
                        if self.tickets[ti].legs.iter().any(|l| l.state == St::Claimed) {
                            TicketSt::Open
                        } else if st == St::Unknown {
                            TicketSt::Check
                        } else if i > 0 {
                            TicketSt::Exposed
                        } else {
                            TicketSt::Dead
                        }
                    }
                },
            }
        };
        let t = &mut self.tickets[ti];
        if new != t.state {
            t.state = new;
            t.done_ms = if new == TicketSt::Open { 0 } else { now };
            if t.mode == Mode::Real && matches!(new, TicketSt::Exposed | TicketSt::Check) {
                let legs: Vec<String> = t.legs.iter().map(|l| format!("{} {} {:?}", l.book, l.outcome, l.state).to_lowercase()).collect();
                let what = if new == TicketSt::Check { "a leg's fate is UNKNOWN, check the site and report it" } else { "a bet stands with nothing against it" };
                self.alerts.push(format!("⚠️ Desk ticket {}: {what}. Legs: {}.{}", t.id, legs.join("; "), if t.note.is_empty() { String::new() } else { format!(" ({})", t.note) }));
            }
        }
    }

    fn claim(&mut self, now: Ms, driver: &str, want: impl Fn(&Leg) -> bool) -> Option<Leg> {
        let mut got = None;
        'find: for t in self.tickets.iter_mut() {
            if t.state != TicketSt::Open {
                continue;
            }
            for l in t.legs.iter_mut() {
                if l.state == St::Queued && now <= l.expires_ms && want(l) {
                    l.state = St::Claimed;
                    l.claimed_ms = now;
                    l.driver = cut(driver, 60);
                    got = Some(l.clone());
                    break 'find;
                }
            }
        }
        if let Some(l) = &got {
            self.touch(l.ticket);
        }
        got
    }

    /// A driver asks for its book's next order. An order is handed out once, ever.
    pub fn next(&mut self, book: &str, driver: &str, now: Ms) -> Option<Leg> {
        let d = self.drivers.entry(book.to_string()).or_default();
        d.name = cut(driver, 60);
        d.last_ms = now;
        d.polls += 1;
        if book == PM {
            return None; // Swifteye places these itself
        }
        let got = self.claim(now, driver, |l| l.book == book && l.mode != Mode::Paper);
        if got.is_some() {
            if let Some(d) = self.drivers.get_mut(book) {
                d.taken += 1;
            }
        }
        got
    }

    /// The next leg Swifteye handles itself: any paper leg, and Polymarket's.
    pub fn next_internal(&mut self, now: Ms) -> Option<Leg> {
        self.claim(now, SELF, |l| l.mode == Mode::Paper || l.book == PM)
    }

    /// A driver's answer for an order it took. Also taken late, for an `unknown` order (the
    /// owner looked at the site). Returns the ticket's id.
    pub fn report(&mut self, r: Report, now: Ms) -> Result<u64> {
        let (ti, li) = self.find_leg(r.id).with_context(|| format!("no order {}", r.id))?;
        let l = &mut self.tickets[ti].legs[li];
        ensure!(matches!(l.state, St::Claimed | St::Unknown), "order {} is {}: a result is only taken for an order a driver holds", r.id, format!("{:?}", l.state).to_lowercase());
        let mut msg = cut(r.message.as_deref().unwrap_or(""), 500);
        match r.status.as_str() {
            "placed" => {
                let odds = r.odds.context("a placed bet needs the odds it went in at")?;
                ensure!(odds.is_finite() && odds > 1.0 && odds <= 1_000.0, "odds must be decimal odds over 1");
                let stake = r.stake.unwrap_or(l.stake);
                ensure!(stake.is_finite() && stake > 0.0, "stake must be over 0");
                if odds < l.min_odds - 1e-9 {
                    msg = format!("went in at {odds}, UNDER the minimum of {}. {msg}", l.min_odds);
                }
                l.state = St::Placed;
                l.odds = Some(odds);
                l.filled = Some(stake);
            }
            "rejected" | "price_changed" | "limit" | "closed" => {
                if msg.is_empty() && r.status != "rejected" {
                    msg = r.status.replace('_', " ");
                }
                l.state = St::Rejected;
                l.odds = r.odds.filter(|o| o.is_finite() && *o > 1.0);
            }
            "failed" | "error" => l.state = St::Failed,
            s => bail!("status must be placed, rejected or failed (got {s:?})"),
        }
        l.max_stake = r.max_stake.filter(|x| x.is_finite() && *x >= 0.0);
        l.bet_ref = cut(r.bet_ref.as_deref().unwrap_or(""), 120);
        l.message = msg.trim().to_string();
        l.done_ms = now;
        if let Some(d) = r.driver {
            l.driver = cut(&d, 60);
        }
        let tid = l.ticket;
        self.advance(ti, now);
        self.touch(tid);
        Ok(tid)
    }

    /// Time passes: queued legs nobody took expire, taken legs nobody answered become unknown.
    pub fn tick(&mut self, now: Ms) {
        let claim_ms = self.set.claim_s.max(5) * 1000;
        let mut changed = Vec::new();
        for (ti, t) in self.tickets.iter_mut().enumerate() {
            if t.state != TicketSt::Open {
                continue;
            }
            for l in t.legs.iter_mut() {
                match l.state {
                    St::Queued if now > l.expires_ms => {
                        l.state = St::Expired;
                        l.done_ms = now;
                        l.message = "nobody took it in time".into();
                        changed.push(ti);
                    }
                    St::Claimed if now > l.claimed_ms + claim_ms => {
                        l.state = St::Unknown;
                        l.done_ms = now;
                        l.message = format!("{} took it and never answered: it MAY be placed, check the site", l.driver);
                        changed.push(ti);
                    }
                    _ => {}
                }
            }
        }
        changed.dedup();
        for ti in changed {
            self.advance(ti, now);
            let id = self.tickets[ti].id;
            self.touch(id);
        }
    }

    /// Cancel what no driver has taken yet. A leg a driver holds cannot be called back.
    pub fn cancel(&mut self, ticket: u64, now: Ms) -> Result<()> {
        let ti = self.idx(ticket).with_context(|| format!("no ticket {ticket}"))?;
        ensure!(self.tickets[ti].state == TicketSt::Open, "ticket {ticket} is already {}", format!("{:?}", self.tickets[ti].state).to_lowercase());
        for l in self.tickets[ti].legs.iter_mut() {
            if matches!(l.state, St::Waiting | St::Queued) {
                l.state = St::Cancelled;
                l.done_ms = now;
                l.message = "cancelled".into();
            }
        }
        self.advance(ti, now);
        self.touch(ticket);
        Ok(())
    }

    /// Change settings. Switching real bets on, or raising a real limit, needs
    /// `"confirm": "REAL MONEY"` in the same request.
    pub fn edit(&mut self, e: &Value) -> Result<()> {
        let confirmed = e["confirm"].as_str().map(str::trim) == Some("REAL MONEY");
        let need = "send \"confirm\": \"REAL MONEY\" with it";
        let mut s = self.set.clone();
        if let Some(x) = e["real"].as_bool() {
            ensure!(!x || s.real || confirmed, "switching real bets on: {need}");
            s.real = x;
        }
        if let Some(x) = e["ttl_s"].as_i64() {
            s.ttl_s = x.clamp(5, 3_600);
        }
        if let Some(x) = e["claim_s"].as_i64() {
            s.claim_s = x.clamp(10, 600);
        }
        if let Some(x) = e["fresh_s"].as_i64() {
            s.fresh_s = x.clamp(1, 600);
        }
        if let Some(bm) = e["books"].as_object() {
            for (name, b) in bm {
                let name = name.trim().to_ascii_lowercase();
                ensure!(!name.is_empty() && name.len() <= 40 && name.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'), "bad book name {name:?}");
                if b.is_null() {
                    s.books.remove(&name);
                    continue;
                }
                let mut bs = s.books.get(&name).cloned().unwrap_or_default();
                if let Some(x) = b["enabled"].as_bool() {
                    bs.enabled = x;
                }
                if let Some(x) = b["real"].as_bool() {
                    ensure!(!x || bs.real || confirmed, "{name}: switching real bets on: {need}");
                    bs.real = x;
                }
                for (field, slot) in [("max_stake", &mut bs.max_stake), ("daily_cap", &mut bs.daily_cap)] {
                    if let Some(x) = b[field].as_f64() {
                        ensure!(x.is_finite() && x >= 0.0, "{name}: {field} must be 0 or more");
                        ensure!(x <= *slot || confirmed, "{name}: raising {field}: {need}");
                        *slot = x;
                    }
                }
                if let Some(x) = b["currency"].as_str() {
                    bs.currency = cut(x.trim(), 8);
                }
                s.books.insert(name, bs);
            }
        }
        if e["new_key"].as_bool() == Some(true) {
            s.key = new_key();
        }
        self.set = s;
        self.set_dirty = true;
        Ok(())
    }

    /// The owner saves (or removes) a bookmaker login. An empty password keeps the one on file,
    /// so a note or the login name can be changed without typing it again.
    pub fn account_save(&mut self, e: &Value, now: Ms) -> Result<()> {
        let book = e["book"].as_str().unwrap_or("").trim().to_ascii_lowercase();
        ensure!(!book.is_empty() && book.len() <= 40 && book.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'), "which bookmaker? (its name as on the Feeds page, letters and digits)");
        ensure!(book != PM, "Polymarket is not signed in to with a password: its keys are in Settings");
        if e["remove"].as_bool() == Some(true) {
            ensure!(self.accounts.remove(&book).is_some(), "no login is saved for {book}");
            self.acc_dirty = true;
            return Ok(());
        }
        let old = self.accounts.get(&book).cloned().unwrap_or_default();
        let login = e["login"].as_str().map(str::trim).filter(|s| !s.is_empty()).map(String::from).unwrap_or(old.login.clone());
        let password = e["password"].as_str().filter(|s| !s.is_empty()).map(String::from).unwrap_or(old.password.clone());
        ensure!(!login.is_empty(), "the email or username is missing");
        ensure!(!password.is_empty(), "the password is missing");
        ensure!(login.len() <= 200 && password.len() <= 500, "too long");
        let note = e["note"].as_str().map(|s| cut(s.trim(), 200)).unwrap_or(old.note);
        // A new save is a fresh start: whatever the site refused before is tried again once.
        self.accounts.insert(book, Account { login, password, note, saved_ms: now, generation: old.generation + 1, ..Default::default() });
        self.acc_dirty = true;
        Ok(())
    }

    /// A driver asks for its book's login because the site has signed it out.
    pub fn account_for_driver(&mut self, book: &str, now: Ms) -> Result<Value> {
        let a = self.accounts.get_mut(book).filter(|a| !a.password.is_empty()).with_context(|| format!("no login is saved for {book}: type it on the Order desk page"))?;
        ensure!(a.state != "refused", "{book}: the site refused this login. It is not tried again until you save it again (repeated wrong sign-ins lock accounts)");
        ensure!(a.state != "needs_owner", "{book}: the site asked for something only you can do ({}). Sign in there yourself; the login is handed out again once the driver reports signed_in or you save it again", a.detail);
        ensure!(a.handed < HAND_MAX, "{book}: this login went to the driver {HAND_MAX} times without a sign-in that worked. Save it again once you have looked at the site");
        a.handed += 1;
        a.handed_ms = now;
        self.acc_dirty = true;
        Ok(json!({"book": book, "login": a.login, "password": a.password, "generation": a.generation}))
    }

    /// A driver says how its book's session stands.
    pub fn account_state(&mut self, book: &str, state: &str, detail: &str, now: Ms) -> Result<()> {
        ensure!(matches!(state, "signed_in" | "signed_out" | "refused" | "needs_owner"), "state must be signed_in, signed_out, refused or needs_owner");
        ensure!(!book.is_empty() && book.len() <= 40 && book.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') && book != PM, "bad book name");
        // A book the owner signed in to by hand has no saved login: its state is kept all the same.
        let a = self.accounts.entry(book.to_string()).or_default();
        let was = a.state.clone();
        a.state = state.to_string();
        a.detail = cut(detail.trim(), 200);
        a.state_ms = now;
        if state == "signed_in" {
            a.handed = 0;
        }
        if was != state && matches!(state, "refused" | "needs_owner") {
            let what = if state == "refused" { "the site REFUSED the saved login. Check it and save it again on the Order desk page" } else { "signing in needs YOU" };
            self.alerts.push(format!("🔑 Desk, {book}: {what}{}{}. No dry or real ticket goes to {book} until then.", if a.detail.is_empty() { "" } else { ": " }, a.detail));
        }
        self.acc_dirty = true;
        Ok(())
    }

    /// The saved logins as anyone reading the API may see them: never a password, the login cut short.
    pub fn accounts_public(&self, now: Ms) -> Value {
        let ago = |t: Ms| if t > 0 { Some((now - t) / 1000) } else { None };
        Value::Array(self.accounts.iter().map(|(b, a)| json!({
            "book": b, "login": mask_login(&a.login), "has_password": !a.password.is_empty(), "note": a.note,
            "saved_ago_s": ago(a.saved_ms), "state": a.state, "detail": a.detail, "state_ago_s": ago(a.state_ms),
            "handed": a.handed, "blocked": matches!(a.state.as_str(), "refused" | "needs_owner") || a.handed >= HAND_MAX,
        })).collect())
    }

    pub fn key_ok(&self, given: Option<&str>) -> bool {
        let (Some(g), k) = (given, self.set.key.as_bytes()) else { return false };
        let g = g.trim().as_bytes();
        // Same time whatever the first wrong character is.
        !k.is_empty() && g.len() == k.len() && g.iter().zip(k).fold(0u8, |a, (x, y)| a | (x ^ y)) == 0
    }

    /// Settings as shown to anyone who can read the API: never the key.
    pub fn public(&self, now: Ms) -> Value {
        let mut books = serde_json::Map::new();
        let mut names: Vec<String> = self.set.books.keys().cloned().collect();
        for d in self.drivers.keys() {
            if !names.contains(d) {
                names.push(d.clone());
            }
        }
        for n in names {
            let b = self.book(&n);
            books.insert(n.clone(), json!({"enabled": b.enabled, "real": b.real, "max_stake": b.max_stake, "daily_cap": b.daily_cap, "currency": b.currency,
                "real_24h": r2(self.real_24h(&n, now)), "placed_by": if n == PM { "swifteye" } else { "driver" },
                "driver": self.drivers.get(&n).map(|d| json!({"name": d.name, "last_ms": d.last_ms, "ago_s": (now - d.last_ms) / 1000, "polls": d.polls, "taken": d.taken}))}));
        }
        json!({"real": self.set.real, "ttl_s": self.set.ttl_s, "claim_s": self.set.claim_s, "fresh_s": self.set.fresh_s, "key": "in desk.json, next to the database", "books": books})
    }
}

// ---------------------------------------------------------------- files

pub fn settings_path(dir: &Path) -> PathBuf {
    dir.join("desk.json")
}

pub fn journal_path(dir: &Path) -> PathBuf {
    dir.join("desk_tickets.jsonl")
}

pub fn accounts_path(dir: &Path) -> PathBuf {
    dir.join("desk_accounts.json")
}

/// Read the settings and the journal. Whatever was on its way when Swifteye stopped is closed
/// off: a leg no driver had taken is cancelled, a leg a driver held is `unknown`.
pub fn load(dir: &Path, now: Ms) -> Desk {
    let mut d = Desk::default();
    d.set = std::fs::read_to_string(settings_path(dir)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    if d.set.key.len() < 16 {
        d.set.key = new_key();
        d.set_dirty = true;
    }
    d.accounts = std::fs::read_to_string(accounts_path(dir)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    let mut by_id: BTreeMap<u64, Ticket> = BTreeMap::new();
    if let Ok(text) = std::fs::read_to_string(journal_path(dir)) {
        for line in text.lines() {
            if let Ok(t) = serde_json::from_str::<Ticket>(line) {
                by_id.insert(t.id, t);
            }
        }
    }
    d.next_id = by_id.values().flat_map(|t| t.legs.iter().map(|l| l.id).chain([t.id])).max().unwrap_or(0) + 1;
    let skip = by_id.len().saturating_sub(KEEP);
    d.tickets = by_id.into_values().skip(skip).collect();
    for ti in 0..d.tickets.len() {
        let mut hit = false;
        for l in d.tickets[ti].legs.iter_mut() {
            match l.state {
                St::Waiting | St::Queued => {
                    l.state = St::Cancelled;
                    l.message = "Swifteye restarted before a driver took it".into();
                }
                St::Claimed => {
                    l.state = St::Unknown;
                    l.message = format!("Swifteye restarted while {} held it: it MAY be placed, check the site", l.driver);
                }
                _ => continue,
            }
            l.done_ms = now;
            hit = true;
        }
        if hit {
            d.advance(ti, now);
            let id = d.tickets[ti].id;
            d.touch(id);
        }
    }
    d
}

/// Write what changed: the tickets to the journal, the settings file, and hand back the alerts.
fn store(dir: &Path, d: &mut Desk) -> Vec<String> {
    if d.set_dirty {
        d.set_dirty = false;
        match serde_json::to_string_pretty(&d.set) {
            Ok(s) => {
                if let Err(e) = std::fs::write(settings_path(dir), s) {
                    tracing::warn!("desk: settings not saved: {e}");
                }
            }
            Err(e) => tracing::warn!("desk: settings not saved: {e}"),
        }
    }
    if d.acc_dirty {
        d.acc_dirty = false;
        let path = accounts_path(dir);
        match serde_json::to_string_pretty(&d.accounts) {
            Ok(s) => {
                if let Err(e) = std::fs::write(&path, s) {
                    tracing::warn!("desk: logins not saved: {e}");
                }
                // Only this user may read the file (where the system has such a thing).
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
                }
            }
            Err(e) => tracing::warn!("desk: logins not saved: {e}"),
        }
    }
    if !d.dirty.is_empty() {
        let ids = std::mem::take(&mut d.dirty);
        let mut out = String::new();
        for id in ids {
            if let Some(t) = d.ticket(id) {
                if let Ok(s) = serde_json::to_string(t) {
                    out.push_str(&s);
                    out.push('\n');
                }
            }
        }
        use std::io::Write as _;
        match std::fs::OpenOptions::new().create(true).append(true).open(journal_path(dir)) {
            Ok(mut f) => {
                if let Err(e) = f.write_all(out.as_bytes()) {
                    tracing::warn!("desk: journal not written: {e}");
                }
            }
            Err(e) => tracing::warn!("desk: journal not opened: {e}"),
        }
    }
    std::mem::take(&mut d.alerts)
}

// ---------------------------------------------------------------- the running desk

static DESK: LazyLock<Mutex<Desk>> = LazyLock::new(Default::default);
static DIR: LazyLock<Mutex<PathBuf>> = LazyLock::new(Default::default);
/// Woken when an order may be waiting for a driver.
static WAKE: LazyLock<tokio::sync::Notify> = LazyLock::new(tokio::sync::Notify::new);
/// Woken when the desk has something to do before its next one-second look.
static WORK: LazyLock<tokio::sync::Notify> = LazyLock::new(tokio::sync::Notify::new);

/// Set by `sandbox`: nothing may leave this machine.
static SANDBOX: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Load settings and tickets. Called once at startup.
pub fn init(db: &Path) {
    let dir = db.parent().map(Path::to_path_buf).unwrap_or_default();
    let mut d = load(&dir, now_ms());
    let _ = store(&dir, &mut d);
    *DESK.lock() = d;
    *DIR.lock() = dir;
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        loop {
            let _ = tokio::time::timeout(Duration::from_secs(1), WORK.notified()).await;
            DESK.lock().tick(now_ms());
            pump(&app).await;
        }
    });
}

/// Handle every leg Swifteye places itself, then write what changed and tell the owner.
pub async fn pump(app: &Arc<App>) {
    loop {
        let leg = DESK.lock().next_internal(now_ms());
        let Some(leg) = leg else { break };
        if leg.book == PM && leg.mode == Mode::Real {
            let a = app.clone();
            tokio::spawn(async move {
                // No answer = the order's fate is not known: the leg stays taken and turns
                // `unknown` on its own, which is the truth.
                if let Some(rep) = pm_real(&a, &leg).await {
                    if let Err(e) = DESK.lock().report(rep, now_ms()) {
                        tracing::warn!("desk: polymarket result not taken: {e:#}");
                    }
                }
                WORK.notify_one();
            });
            continue;
        }
        let rep = if leg.book == PM { pm_look(&leg) } else { paper(app, &leg) };
        if let Err(e) = DESK.lock().report(rep, now_ms()) {
            tracing::warn!("desk: own result not taken: {e:#}");
        }
    }
    let dir = DIR.lock().clone();
    let alerts = store(&dir, &mut DESK.lock());
    WAKE.notify_waiters();
    for a in alerts {
        crate::bots::event(a.clone());
        let _ = crate::telegram::send("bot", &a).await;
    }
}

fn rep(l: &Leg, status: &str, odds: Option<f64>, stake: Option<f64>, max_stake: Option<f64>, msg: String) -> Report {
    Report { id: l.id, status: status.into(), odds, stake, max_stake, bet_ref: None, message: Some(msg), driver: None }
}

/// The price Swifteye has recorded for a bookmaker leg: (price or None when suspended, when the
/// book's page for the game was last read).
fn book_quote(app: &App, l: &Leg) -> Option<(Option<f64>, Ms)> {
    let (mk, o) = (Market::parse(&l.market)?, parse_outcome(&l.outcome)?);
    let eng = app.engine.lock();
    let es = eng.books.get(l.book.as_str())?.events.values().find(|e| e.match_id == l.match_id)?;
    Some((es.quotes.get(&(mk, line_key(l.line), o)).and_then(|q| q.price), es.last_seen_ms))
}

/// Paper: a bookmaker leg fills at the recorded price when that price is fresh and good enough.
fn paper(app: &App, l: &Leg) -> Report {
    let fresh_ms = DESK.lock().set.fresh_s * 1000;
    let now = now_ms();
    match book_quote(app, l) {
        None => rep(l, "rejected", None, None, None, "paper: Swifteye has no price for this bet at this book".into()),
        Some((_, seen)) if now - seen > fresh_ms => rep(l, "rejected", None, None, None, format!("paper: the recorded price is {} s old", (now - seen) / 1000)),
        Some((None, _)) => rep(l, "rejected", None, None, None, "paper: the book shows no price for it now".into()),
        Some((Some(p), _)) if p < l.min_odds - 1e-9 => rep(l, "rejected", Some(p), None, None, format!("paper: the recorded price is {p}, under the minimum of {}", l.min_odds)),
        Some((Some(p), _)) => rep(l, "placed", Some(p), Some(l.stake), None, "paper: filled at the recorded price (no limit or bet-slip check)".into()),
    }
}

/// What a driver needs to find the bet on the site, in the BOOK's own terms: its event id, its
/// team names, and the outcome and handicap line the way the book lists the teams.
pub fn describe(app: &App, l: &mut LegReq) {
    let book = l.book.trim().to_ascii_lowercase();
    let (Some(mk), Some(o)) = (Market::parse(l.market.as_deref().unwrap_or("1x2")), parse_outcome(&l.outcome)) else { return };
    let eng = app.engine.lock();
    let Some(bs) = eng.books.get(book.as_str()) else {
        l.info = json!({"known": false, "why": "Swifteye does not read this book"});
        return;
    };
    let Some(es) = bs.events.values().find(|e| e.match_id == l.match_id) else {
        l.info = json!({"known": false, "why": "this book does not list the game right now"});
        return;
    };
    let (book_outcome, book_line) = if es.swap {
        let bo = match o {
            Outcome::Home => Outcome::Away,
            Outcome::Away => Outcome::Home,
            x => x,
        };
        (bo, if mk == Market::Handicap { l.line.map(|x| -x) } else { l.line })
    } else {
        (o, l.line)
    };
    let pick = match book_outcome {
        Outcome::Home => es.meta.home.clone(),
        Outcome::Away => es.meta.away.clone(),
        x => x.as_str().to_string(),
    };
    let q = es.quotes.get(&(mk, line_key(l.line), o));
    l.info = json!({
        "known": true, "event_id": es.meta.id, "sport": es.meta.sport, "league": es.meta.league,
        "home": es.meta.home, "away": es.meta.away, "start_ms": es.meta.start_ms, "live": es.meta.live,
        "teams_swapped": es.swap, "book_outcome": book_outcome.as_str(), "book_line": book_line, "pick": pick,
        "seen_odds": q.and_then(|q| q.price), "seen_changed_ms": q.map(|q| q.updated_ms), "page_read_ms": es.last_seen_ms,
    });
}

// ---------------------------------------------------------------- Polymarket, placed by Swifteye

/// The share price whose cost with the fee is `cost`: p + fee(p) = cost.
pub fn price_for_cost(cost: f64, fee: crate::venue::Fee) -> f64 {
    let (mut lo, mut hi) = (0.0f64, 1.0f64);
    for _ in 0..50 {
        let mid = (lo + hi) / 2.0;
        if mid + fee.raw(1.0, mid) > cost {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    lo
}

struct PmPlan {
    token: String,
    /// What can be bought, cheapest first: (price, shares).
    asks: Vec<(f64, f64)>,
    fee: crate::venue::Fee,
    /// The most to pay a share (cents), so that with the fee the odds are still `min_odds`.
    limit: f64,
    /// Shares wanted: what pays the planned amount.
    shares: f64,
}

fn pm_plan(l: &Leg) -> Result<PmPlan> {
    let o = crate::venue::outcome_of(&l.outcome).context("Polymarket legs are home, draw or away")?;
    ensure!(l.market == "1x2", "only the winner market is wired for Polymarket");
    let (id, book, meta) = crate::venue::pm_contract(l.match_id, o).context("Polymarket has no market linked to this game and outcome")?;
    let (token, asks): (String, Vec<(f64, f64)>) = if l.side == "no" {
        // Buying against the outcome = buying its other token; what that costs is what the
        // outcome's own buyers bid, seen from the other side.
        (meta.complement.clone().context("this market's other token is not known")?, book.bids.iter().map(|(p, q)| (((1.0 - p) * 1000.0).round() / 1000.0, *q)).collect())
    } else {
        (id, book.asks.clone())
    };
    let fee = meta.fee.unwrap_or(crate::venue::Fee::PM_SPORTS);
    let limit = ((price_for_cost(1.0 / l.min_odds, fee) * 100.0) + 1e-6).floor() / 100.0;
    ensure!(limit >= 0.01, "min_odds leaves no price to pay");
    Ok(PmPlan { token, asks, fee, limit, shares: (l.stake * l.odds_seen + 1e-6).floor() })
}

/// What `shares` would cost from `asks` at or under `limit`: (whole shares, average price,
/// dollars resting at or under the limit, fee included).
fn walk(p: &PmPlan) -> (f64, f64, f64) {
    let (mut n, mut cost, mut room) = (0.0, 0.0, 0.0);
    for (px, q) in &p.asks {
        if *px > p.limit + 1e-9 {
            break;
        }
        room += q * (px + p.fee.raw(1.0, *px));
        let k = q.min(p.shares - n);
        if k > 0.0 {
            n += k;
            cost += k * px;
        }
    }
    let n = n.floor();
    (n, if n > 0.0 { cost / n.max(1e-9) } else { 0.0 }, room)
}

/// Paper and dry for Polymarket: what its order book would give right now.
fn pm_look(l: &Leg) -> Report {
    let tag = if l.mode == Mode::Dry { "dry" } else { "paper" };
    let p = match pm_plan(l) {
        Ok(p) => p,
        Err(e) => return rep(l, "rejected", None, None, None, format!("{tag}: {e:#}")),
    };
    let silent = now_ms() - crate::venue::alive_ms(crate::venue::V::Pm);
    if silent > 15_000 {
        return rep(l, "rejected", None, None, None, format!("{tag}: Polymarket's price feed has been silent for {} s", silent / 1000));
    }
    let (n, avg, room) = walk(&p);
    let best = p.asks.first().map(|a| 1.0 / (a.0 + p.fee.raw(1.0, a.0)));
    if n < PM_MIN_SHARES {
        return rep(l, "rejected", best, None, Some(r2(room)), format!("{tag}: {n} shares rest at or under {:.2} (Polymarket's smallest order is {PM_MIN_SHARES})", p.limit));
    }
    let each = avg + p.fee.raw(1.0, avg);
    rep(l, "placed", Some(1.0 / each), Some(r2(n * each)), Some(r2(room)), format!("{tag}: {n} of {} shares at {avg:.3} from the order book", p.shares))
}

/// Real: buy on Polymarket. None = the order's fate could not be established.
async fn pm_real(app: &Arc<App>, l: &Leg) -> Option<Report> {
    // Checked again here: the switches may have changed since the ticket was taken in.
    let (desk_real, book_real) = {
        let d = DESK.lock();
        (d.set.real, d.book(PM).real)
    };
    if !desk_real || !book_real || crate::bots::settings().kill {
        return Some(rep(l, "rejected", None, None, None, "real bets were switched off before this leg went out".into()));
    }
    if SANDBOX.load(std::sync::atomic::Ordering::Relaxed) {
        return Some(rep(l, "rejected", None, None, None, "sandbox: a Polymarket order is never sent from here".into()));
    }
    let p = match pm_plan(l) {
        Ok(p) => p,
        Err(e) => return Some(rep(l, "rejected", None, None, None, format!("{e:#}"))),
    };
    let (can, _, room) = walk(&p);
    if p.shares < PM_MIN_SHARES || can < PM_MIN_SHARES {
        return Some(rep(l, "rejected", None, None, Some(r2(room)), format!("{can} shares rest at or under {:.2}, {} wanted (Polymarket's smallest order is {PM_MIN_SHARES})", p.limit, p.shares)));
    }
    let pm = crate::pmacct::load(&app.db_path);
    let http = crate::books::http_client();
    let before = {
        let (h, c, t) = (http.clone(), pm.clone(), p.token.clone());
        tokio::spawn(async move { crate::exec::pm_shares(&h, &c, &t).await.ok() })
    };
    let mut wallet = match crate::exec::claim(crate::venue::V::Pm, &app.db_path, &http, p.shares * p.limit * 1.02).await {
        Ok(w) => w,
        Err(e) => return Some(rep(l, "rejected", None, None, None, format!("{e:#}"))),
    };
    {
        let a = app.clone();
        let j = json!({"intent": true, "side": "buy-intent", "token": p.token, "limit": p.limit, "shares": p.shares, "match_id": l.match_id, "outcome": l.outcome, "desk_order": l.id, "desk_ticket": l.ticket});
        tokio::task::spawn_blocking(move || crate::bots::log_intent(&a, "desk", &j));
    }
    let placed = crate::exec::pm_order(&http, &pm, &p.token, true, p.limit, p.shares, false).await;
    if let Ok(x) = &placed {
        wallet.spent += x.filled * x.avg * 1.02;
    }
    drop(wallet);
    let done = |n: f64, avg: f64, order: &str, note: String| {
        let each = avg + p.fee.raw(1.0, avg);
        let mut r = rep(l, "placed", Some(1.0 / each), Some(r2(n * each)), Some(r2(room)), if n + 0.5 < p.shares { format!("PARTLY filled: {n} of {} shares. {note}", p.shares) } else { note });
        r.bet_ref = Some(order.to_string());
        r
    };
    match placed {
        Ok(x) if x.filled >= 1.0 => {
            crate::bots::log_order(app, "desk", PM, true, &json!({"side": "buy", "token": p.token, "limit": p.limit, "shares": p.shares, "fill": x, "desk_order": l.id}));
            Some(done((x.filled * 100.0 + 1e-6).floor() / 100.0, x.avg, &x.order_id, format!("{} shares at {:.3}", x.filled, x.avg)))
        }
        other => {
            let err = match &other {
                Ok(x) => {
                    crate::bots::log_order(app, "desk", PM, false, &json!({"side": "buy", "token": p.token, "limit": p.limit, "shares": p.shares, "fill": x, "error": "nothing filled", "desk_order": l.id}));
                    None
                }
                Err(e) => {
                    crate::bots::log_order(app, "desk", PM, false, &json!({"side": "buy", "token": p.token, "limit": p.limit, "shares": p.shares, "error": format!("{e:#}"), "desk_order": l.id}));
                    Some(format!("{e:#}"))
                }
            };
            // Did it fill after all? Shares on the account now, less what was there before.
            tokio::time::sleep(Duration::from_millis(RECHECK_MS)).await;
            let base = before.await.ok().flatten();
            let have = crate::exec::pm_shares(&http, &pm, &p.token).await.ok();
            match base.zip(have) {
                Some((b, h)) if h - b >= 1.0 => {
                    let n = ((h - b) * 100.0 + 1e-6).floor() / 100.0;
                    crate::bots::log_order(app, "desk", PM, true, &json!({"side": "buy", "token": p.token, "limit": p.limit, "shares": n, "recovered": true, "desk_order": l.id}));
                    Some(done(n, p.limit, "", "the order reported no fill but the account got the shares; price taken as the limit".into()))
                }
                Some(_) => Some(match err {
                    None => rep(l, "rejected", None, None, Some(r2(room)), format!("nothing filled at or under {:.2}", p.limit)),
                    Some(e) => rep(l, "failed", None, None, None, cut(&e, 300)),
                }),
                // The account could not be read: nothing is known. No answer.
                None => None,
            }
        }
    }
}

// ---------------------------------------------------------------- what the API calls

pub fn key_ok(given: Option<&str>) -> bool {
    DESK.lock().key_ok(given)
}

fn ticket_json(t: &Ticket) -> Value {
    serde_json::to_value(t).unwrap_or(Value::Null)
}

/// An order as a driver gets it.
fn order_json(l: &Leg) -> Value {
    json!({
        "id": l.id, "ticket": l.ticket, "book": l.book, "dry_run": l.mode == Mode::Dry, "mode": l.mode,
        "match_id": l.match_id, "market": l.market, "line": l.line, "outcome": l.outcome,
        "min_odds": l.min_odds, "odds_seen": l.odds_seen, "stake": l.stake,
        "answer_by_ms": l.claimed_ms + DESK.lock().set.claim_s.max(5) * 1000, "info": l.info,
    })
}

pub async fn create(app: &Arc<App>, mut req: TicketReq) -> Result<Value> {
    // The driver's directions are written before the desk takes the ticket, so no driver can
    // take a leg without them.
    for l in req.legs.iter_mut() {
        describe(app, l);
    }
    let kill = crate::bots::settings().kill;
    let id = DESK.lock().create(req, now_ms(), kill)?;
    pump(app).await;
    Ok(DESK.lock().ticket(id).map(ticket_json).unwrap_or(Value::Null))
}

/// A ticket from an arbitrage the tracker shows now (`GET /arbs`): its legs at the tracker's
/// stakes for `stake` in total, bookmakers first and Polymarket last, each leg accepted down to
/// `slack` (a share of its odds, 0 = not a tick worse).
pub async fn from_arb(app: &Arc<App>, key: &str, stake: f64, mode: Mode, slack: f64) -> Result<Value> {
    ensure!(stake.is_finite() && stake > 0.0, "stake must be over 0");
    ensure!((0.0..=0.05).contains(&slack), "slack is a share of the odds, 0 to 0.05");
    let cur = crate::arbtrack::current(-1.0, stake);
    let arb = cur["arbs"].as_array().and_then(|a| a.iter().find(|x| x["key"].as_str() == Some(key))).cloned().context("the tracker does not show that arbitrage now")?;
    let (match_id, market, line) = (arb["match_id"].as_i64().context("no match")?, arb["market"].as_str().unwrap_or("1x2").to_string(), arb["line"].as_f64());
    let mut legs = Vec::new();
    for l in arb["legs"].as_array().context("no legs")? {
        let (book, side) = (l["book"].as_str().unwrap_or("").to_string(), l["side"].as_str().unwrap_or("back").to_string());
        ensure!(book != "kalshi", "Kalshi legs are not wired on the desk");
        // A "no" leg covers its outcome by buying AGAINST another one: that one is what is bought.
        let outcome = if side == "no" { l["against"].as_str() } else { l["outcome"].as_str() }.context("leg without an outcome")?.to_string();
        let odds = l["odds"].as_f64().context("leg without odds")?;
        legs.push(LegReq { book, match_id, market: Some(market.clone()), line, outcome, side: Some(side), min_odds: (odds * (1.0 - slack)).max(1.0001), odds: Some(odds), stake: l["stake"].as_f64().context("leg without a stake")?, info: Value::Null });
    }
    legs.sort_by_key(|l| l.book == PM);
    let note = format!("arb {key}: {} v {}, {:+.2}% at the tracker", arb["home"].as_str().unwrap_or("?"), arb["away"].as_str().unwrap_or("?"), arb["profit"].as_f64().unwrap_or(0.0) * 100.0);
    create(app, TicketReq { mode: Some(mode), note: Some(note), rebalance: Some(true), ttl_s: None, legs }).await
}

/// A driver asks for work and waits up to `wait_s` for it. `{"order": null}` = nothing to do.
pub async fn next(book: &str, driver: &str, wait_s: u64) -> Value {
    let until = tokio::time::Instant::now() + Duration::from_secs(wait_s.min(30));
    loop {
        let woken = WAKE.notified();
        tokio::pin!(woken);
        woken.as_mut().enable();
        let got = DESK.lock().next(book, driver, now_ms());
        if let Some(l) = got {
            let dir = DIR.lock().clone();
            let _ = store(&dir, &mut DESK.lock());
            return json!({"order": order_json(&l)});
        }
        if tokio::time::timeout_at(until, woken).await.is_err() {
            return json!({"order": null});
        }
    }
}

pub async fn report(app: &Arc<App>, r: Report) -> Result<Value> {
    let id = DESK.lock().report(r, now_ms())?;
    pump(app).await;
    Ok(DESK.lock().ticket(id).map(ticket_json).unwrap_or(Value::Null))
}

pub async fn cancel(app: &Arc<App>, ticket: u64) -> Result<Value> {
    DESK.lock().cancel(ticket, now_ms())?;
    pump(app).await;
    Ok(DESK.lock().ticket(ticket).map(ticket_json).unwrap_or(Value::Null))
}

pub async fn edit(app: &Arc<App>, e: &Value) -> Result<Value> {
    DESK.lock().edit(e)?;
    pump(app).await;
    Ok(DESK.lock().public(now_ms()))
}

pub fn ticket(id: u64) -> Option<Value> {
    DESK.lock().ticket(id).map(ticket_json)
}

pub fn accounts() -> Value {
    DESK.lock().accounts_public(now_ms())
}

pub async fn account_save(app: &Arc<App>, e: &Value) -> Result<Value> {
    DESK.lock().account_save(e, now_ms())?;
    pump(app).await;
    Ok(accounts())
}

/// For a driver only (the caller checks the desk key): the login with its password.
pub async fn account_for_driver(app: &Arc<App>, book: &str) -> Result<Value> {
    let r = DESK.lock().account_for_driver(book, now_ms());
    pump(app).await;
    r
}

pub async fn account_state(app: &Arc<App>, book: &str, state: &str, detail: &str) -> Result<Value> {
    DESK.lock().account_state(book, state, detail, now_ms())?;
    pump(app).await;
    Ok(accounts())
}

/// The desk at a glance: switches, books and their drivers, open tickets, the latest ones.
pub fn view(limit: usize, state: Option<&str>) -> Value {
    let d = DESK.lock();
    let now = now_ms();
    let want = |t: &&Ticket| state.map_or(true, |s| format!("{:?}", t.state).to_lowercase() == s);
    let mut count: BTreeMap<String, usize> = BTreeMap::new();
    for t in &d.tickets {
        *count.entry(format!("{:?}", t.state).to_lowercase()).or_default() += 1;
    }
    let latest: Vec<Value> = d.tickets.iter().rev().filter(want).take(limit).map(ticket_json).collect();
    json!({"now_ms": now, "kill": crate::bots::settings().kill, "settings": d.public(now), "accounts": d.accounts_public(now), "counts": count, "tickets": latest})
}

// ---------------------------------------------------------------- sandbox

/// One pretend game as a pretend bookmaker lists it.
fn pretend_game(id: &str, home: &str, away: &str, h: f64, a: f64, start_ms: Ms) -> EventIn {
    EventIn {
        id: id.into(),
        sport: "cs2".into(),
        league: "Desk Sandbox League".into(),
        home: home.into(),
        away: away.into(),
        start_ms: Some(start_ms),
        quotes: vec![QuoteIn::new(Market::X12, None, Outcome::Home, Some(h)), QuoteIn::new(Market::X12, None, Outcome::Away, Some(a))],
        ..Default::default()
    }
}

/// An app with no feeds, no database and no accounts: only an engine with the pretend books.
pub(crate) fn pretend_app(dir: &Path) -> Arc<App> {
    let (tx, mut rx) = tokio::sync::mpsc::channel(1_000);
    tokio::spawn(async move { while rx.recv().await.is_some() {} });
    let (live, _) = tokio::sync::broadcast::channel(16);
    let mut engine = crate::engine::Engine::default();
    engine.prediction_only = false;
    engine.register("alpha", "pretend".into(), 1_000);
    engine.register("beta", "pretend".into(), 1_000);
    Arc::new(App { cfg: crate::Config::default(), db_path: dir.join("no-database.db"), engine: Mutex::new(engine), tx, live, started_ms: now_ms() })
}

/// The pretend prices, read "now": Natus Vincere v Team Spirit at alpha (`h` / `a`), the same game
/// at beta with the teams the other way round, and a Polymarket book for Natus Vincere (Yes 46c
/// bid / 48c ask). Returns the match id.
fn pretend_prices(app: &App, h: f64, a: f64, start_ms: Ms) -> i64 {
    use crate::venue::{self, Fee, Meta, V};
    let now = now_ms();
    let m = {
        let mut eng = app.engine.lock();
        eng.apply("alpha", Snapshot { events: vec![pretend_game("A-1", "Natus Vincere", "Team Spirit", h, a, start_ms)], recv_ms: now }, now);
        eng.apply("beta", Snapshot { events: vec![pretend_game("B-9", "Team Spirit", "Natus Vincere", r2(a + 0.05), r2(h - 0.05), start_ms)], recv_ms: now }, now);
        eng.books["alpha"].events["A-1"].match_id
    };
    venue::map_match(m, Outcome::Home, "SANDBOX-H");
    venue::set_meta(V::Pm, "SANDBOX-H", Meta { fee: Some(Fee::PM_SPORTS), complement: Some("SANDBOX-A".into()), title: "Natus Vincere v Team Spirit (pretend)".into(), ..Default::default() });
    venue::publish(V::Pm, "SANDBOX-H", vec![(0.46, 300.0), (0.45, 900.0)], vec![(0.48, 100.0), (0.49, 400.0)], now);
    venue::alive(V::Pm, now);
    m
}

/// `oddsfeed desk-sandbox [port] [folder]`: the desk alone, for writing and trying a driver.
/// No feed, no bot, no account, no database, nothing sent anywhere: one pretend game at two
/// pretend bookmakers ("alpha", and "beta" which lists the teams the other way round) and a
/// pretend Polymarket book, with bookmaker prices that move a little every 5 s. Its desk.json and
/// journal go to `folder` (default: a new folder in the temp directory), never next to the real
/// database. Only the /desk endpoints mean anything here.
pub async fn sandbox(port: u16, dir: Option<PathBuf>) -> Result<()> {
    SANDBOX.store(true, std::sync::atomic::Ordering::Relaxed);
    let dir = dir.unwrap_or_else(|| std::env::temp_dir().join(format!("desk-sandbox-{}", now_ms() / 1000)));
    std::fs::create_dir_all(&dir).with_context(|| format!("cannot make {}", dir.display()))?;
    ensure!(!dir.join("oddsfeed.db").exists() && !dir.join("polymarket.json").exists(), "{} holds a real Swifteye database or account file: give the sandbox a folder of its own", dir.display());
    let app = pretend_app(&dir);
    let start = now_ms() + 3_600_000;
    let m = pretend_prices(&app, 2.10, 1.75, start);
    init(&app.db_path);
    spawn(app.clone());
    {
        let app = app.clone();
        tokio::spawn(async move {
            let mut k = 0u64;
            loop {
                tokio::time::sleep(Duration::from_secs(5)).await;
                k += 1;
                let d = [0.0, 0.02, 0.04, 0.02, 0.0, -0.02, -0.04, -0.02][(k % 8) as usize];
                pretend_prices(&app, r2(2.10 + d), r2(1.75 - d), start);
            }
        });
    }
    let bind = format!("127.0.0.1:{port}");
    let listener = tokio::net::TcpListener::bind(&bind).await.with_context(|| format!("cannot listen on {bind}"))?;
    println!("desk sandbox on http://{bind}/desk   (nothing here is real: no feed, no account, nothing sent anywhere)");
    println!("folder: {}   (the key every call needs is in its desk.json)", dir.display());
    println!("pretend game: match_id {m}, Natus Vincere (home) v Team Spirit (away), cs2");
    println!("  alpha: home 2.10 / away 1.75    beta (teams the other way round): home 1.80 / away 2.05 in the MATCH's terms");
    println!("  polymarket: home Yes 46c bid / 48c ask (side yes or no)");
    println!("a pretend bookmaker to answer orders:  python tools/desk/driver.py --mock --book alpha --api http://{bind} --desk-json {}", settings_path(&dir).display());
    axum::serve(listener, crate::api::router(app))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leg(book: &str, outcome: &str, odds: f64, stake: f64) -> LegReq {
        LegReq { book: book.into(), match_id: 7, market: None, line: None, outcome: outcome.into(), side: None, min_odds: odds * 0.98, odds: Some(odds), stake, info: Value::Null }
    }

    fn tk(mode: Mode, legs: Vec<LegReq>) -> TicketReq {
        TicketReq { mode: Some(mode), note: None, rebalance: None, ttl_s: Some(60), legs }
    }

    fn placed(id: u64, odds: f64, stake: f64) -> Report {
        Report { id, status: "placed".into(), odds: Some(odds), stake: Some(stake), ..Default::default() }
    }

    fn real_desk() -> Desk {
        let mut d = Desk::default();
        d.edit(&json!({"real": true, "confirm": "REAL MONEY", "books": {"alpha": {"real": true, "max_stake": 100, "daily_cap": 150}, "beta": {"real": true, "max_stake": 100, "daily_cap": 500}}})).unwrap();
        d
    }

    #[test]
    fn legs_go_out_one_at_a_time_and_an_order_is_handed_out_once() {
        let mut d = real_desk();
        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.1, 47.62)]), 1_000, false).unwrap();
        // Only the first leg is out; the second book's driver finds nothing.
        assert!(d.next("beta", "b1", 1_100).is_none());
        let a = d.next("alpha", "a1", 1_100).expect("first leg");
        assert_eq!((a.book.as_str(), a.state), ("alpha", St::Claimed));
        assert!(d.next("alpha", "a2", 1_200).is_none(), "the same order is never handed out twice");
        d.report(placed(a.id, 2.0, 50.0), 2_000).unwrap();
        let b = d.next("beta", "b1", 2_100).expect("second leg after the first is in");
        assert_eq!(b.stake, 47.62);
        d.report(placed(b.id, 2.1, 47.62), 3_000).unwrap();
        let t = d.ticket(id).unwrap();
        assert_eq!(t.state, TicketSt::Done);
        assert!(d.report(placed(b.id, 2.1, 47.62), 3_100).is_err(), "a finished order takes no second result");
    }

    #[test]
    fn a_later_leg_is_resized_to_what_the_first_one_came_in_at() {
        let mut d = real_desk();
        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.0, 50.0)]), 0, false).unwrap();
        let a = d.next("alpha", "a", 10).unwrap();
        // The book only took 30 of the 50, at slightly lower odds: it pays 30 x 1.98 = 59.4, not 100.
        d.report(placed(a.id, 1.98, 30.0), 20).unwrap();
        let b = d.next("beta", "b", 30).unwrap();
        assert_eq!(b.stake, 29.7, "50 x 59.4 / 100");
        d.report(placed(b.id, 2.0, 29.7), 40).unwrap();
        assert_eq!(d.ticket(id).unwrap().state, TicketSt::Done);
    }

    #[test]
    fn a_refused_second_leg_leaves_the_ticket_exposed_and_tells_the_owner() {
        let mut d = real_desk();
        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "draw", 3.0, 33.0), leg("beta", "away", 6.0, 16.0)]), 0, false).unwrap();
        let a = d.next("alpha", "a", 10).unwrap();
        d.report(placed(a.id, 2.0, 50.0), 20).unwrap();
        let b = d.next("beta", "b", 30).unwrap();
        d.report(Report { id: b.id, status: "price_changed".into(), odds: Some(2.5), ..Default::default() }, 40).unwrap();
        let t = d.ticket(id).unwrap();
        assert_eq!(t.state, TicketSt::Exposed);
        assert_eq!(t.legs[1].state, St::Rejected);
        assert_eq!(t.legs[1].message, "price changed");
        assert_eq!(t.legs[2].state, St::Cancelled, "the third leg never went out");
        assert_eq!(d.alerts.len(), 1);
        assert!(d.next("beta", "b", 50).is_none());
    }

    #[test]
    fn a_refused_first_leg_kills_the_ticket_with_nothing_bet() {
        let mut d = real_desk();
        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.0, 50.0)]), 0, false).unwrap();
        let a = d.next("alpha", "a", 10).unwrap();
        d.report(Report { id: a.id, status: "rejected".into(), message: Some("limit is 5".into()), max_stake: Some(5.0), ..Default::default() }, 20).unwrap();
        let t = d.ticket(id).unwrap();
        assert_eq!((t.state, t.legs[1].state, t.legs[0].max_stake), (TicketSt::Dead, St::Cancelled, Some(5.0)));
        assert!(d.alerts.is_empty(), "nothing was bet: nothing to shout about");
        assert_eq!(d.real_24h("alpha", 30), 0.0);
    }

    #[test]
    fn nobody_taking_a_leg_expires_it_and_a_silent_driver_makes_it_unknown() {
        let mut d = real_desk();
        d.set.claim_s = 90;
        let dead = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 10.0)]), 0, false).unwrap();
        d.tick(60_000);
        assert_eq!(d.ticket(dead).unwrap().state, TicketSt::Open, "60 s is not over yet");
        d.tick(60_001);
        assert_eq!((d.ticket(dead).unwrap().state, d.ticket(dead).unwrap().legs[0].state), (TicketSt::Dead, St::Expired));

        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 10.0), leg("beta", "away", 2.0, 10.0)]), 100_000, false).unwrap();
        let a = d.next("alpha", "a", 100_500).unwrap();
        d.tick(100_500 + 90_000);
        assert_eq!(d.ticket(id).unwrap().legs[0].state, St::Claimed);
        d.tick(100_500 + 90_001);
        let t = d.ticket(id).unwrap();
        assert_eq!((t.state, t.legs[0].state, t.legs[1].state), (TicketSt::Check, St::Unknown, St::Cancelled));
        assert!(d.next("alpha", "a", 300_000).is_none(), "an unknown order is never handed out again");
        // It still counts against the cap: it may be on.
        assert_eq!(d.real_24h("alpha", 300_000), 10.0);
        // The owner looked at the site: it was placed. The hedge was cancelled, so it stands alone.
        d.report(placed(a.id, 2.0, 10.0), 400_000).unwrap();
        assert_eq!(d.ticket(id).unwrap().state, TicketSt::Exposed);
    }

    #[test]
    fn real_bets_need_every_switch_and_stay_under_the_limits() {
        let mut d = Desk::default();
        let one = |book: &str, stake: f64| tk(Mode::Real, vec![leg(book, "home", 2.0, stake)]);
        assert!(d.create(one("alpha", 10.0), 0, false).unwrap_err().to_string().contains("switched off on the desk"));
        assert!(d.edit(&json!({"real": true})).is_err(), "no confirmation, no real money");
        d.edit(&json!({"real": true, "confirm": "REAL MONEY"})).unwrap();
        assert!(d.create(one("alpha", 10.0), 0, false).unwrap_err().to_string().contains("not switched on for this book"));
        assert!(d.edit(&json!({"books": {"alpha": {"real": true, "max_stake": 20, "daily_cap": 30}}})).is_err());
        d.edit(&json!({"confirm": "REAL MONEY", "books": {"alpha": {"real": true, "max_stake": 20, "daily_cap": 30}}})).unwrap();
        assert!(d.create(one("alpha", 10.0), 0, true).unwrap_err().to_string().contains("kill switch"));
        assert!(d.create(one("alpha", 25.0), 0, false).unwrap_err().to_string().contains("over this book's limit"));
        d.create(one("alpha", 20.0), 0, false).unwrap();
        assert!(d.create(one("alpha", 15.0), 1_000, false).unwrap_err().to_string().contains("24 hours"));
        d.create(one("alpha", 10.0), 1_000, false).unwrap();
        // A day later the cap is free again.
        d.create(one("alpha", 20.0), 86_400_001 + 1_000, false).unwrap();
        // Lowering a limit or switching off needs no confirmation; raising does.
        d.edit(&json!({"books": {"alpha": {"max_stake": 5}}})).unwrap();
        assert!(d.edit(&json!({"books": {"alpha": {"max_stake": 50}}})).is_err());
        d.edit(&json!({"real": false})).unwrap();
        assert!(!d.set.real);
        // Paper needs none of it.
        d.create(tk(Mode::Paper, vec![leg("gamma", "home", 2.0, 1_000.0)]), 0, false).unwrap();
        d.edit(&json!({"books": {"gamma": {"enabled": false}}})).unwrap();
        assert!(d.create(tk(Mode::Paper, vec![leg("gamma", "home", 2.0, 1.0)]), 0, false).is_err());
    }

    #[test]
    fn a_dry_run_reads_every_slip_at_once_and_never_counts_as_exposed() {
        let mut d = Desk::default();
        let id = d.create(tk(Mode::Dry, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.0, 50.0)]), 0, false).unwrap();
        let (a, b) = (d.next("alpha", "a", 10).unwrap(), d.next("beta", "b", 10).unwrap());
        assert!(d.next("alpha", "paper", 10).is_none());
        d.report(Report { id: a.id, status: "placed".into(), odds: Some(2.02), max_stake: Some(40.0), ..Default::default() }, 20).unwrap();
        assert_eq!(d.ticket(id).unwrap().state, TicketSt::Open);
        d.report(Report { id: b.id, status: "rejected".into(), odds: Some(1.8), ..Default::default() }, 30).unwrap();
        let t = d.ticket(id).unwrap();
        assert_eq!(t.state, TicketSt::Dead);
        assert_eq!((t.legs[0].odds, t.legs[0].max_stake, t.legs[1].odds), (Some(2.02), Some(40.0), Some(1.8)));
        assert_eq!(d.real_24h("alpha", 40), 0.0);
    }

    #[test]
    fn paper_legs_are_swifteyes_own_and_polymarket_is_never_given_to_a_driver() {
        let mut d = Desk::default();
        let id = d.create(tk(Mode::Paper, vec![leg("alpha", "home", 2.0, 50.0), LegReq { side: Some("no".into()), ..leg("polymarket", "home", 2.0, 50.0) }]), 0, false).unwrap();
        assert!(d.next("alpha", "a", 10).is_none(), "a paper leg never reaches a driver");
        let a = d.next_internal(10).unwrap();
        d.report(placed(a.id, 2.0, 50.0), 20).unwrap();
        assert!(d.next("polymarket", "x", 30).is_none());
        let b = d.next_internal(30).unwrap();
        assert_eq!((b.book.as_str(), b.side.as_str(), b.driver.as_str()), ("polymarket", "no", "swifteye"));
        d.report(placed(b.id, 2.0, 50.0), 40).unwrap();
        assert_eq!(d.ticket(id).unwrap().state, TicketSt::Done);
        // Sides are checked: a bookmaker has no "no", Polymarket no "back".
        assert!(d.create(tk(Mode::Paper, vec![LegReq { side: Some("no".into()), ..leg("alpha", "home", 2.0, 1.0) }]), 0, false).is_err());
        assert!(d.create(tk(Mode::Paper, vec![LegReq { side: Some("back".into()), ..leg("polymarket", "home", 2.0, 1.0) }]), 0, false).is_err());
    }

    #[test]
    fn cancelling_takes_back_only_what_no_driver_holds() {
        let mut d = real_desk();
        let id = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.0, 50.0)]), 0, false).unwrap();
        let a = d.next("alpha", "a", 10).unwrap();
        d.cancel(id, 20).unwrap();
        let t = d.ticket(id).unwrap();
        assert_eq!((t.state, t.legs[0].state, t.legs[1].state), (TicketSt::Open, St::Claimed, St::Cancelled));
        d.report(placed(a.id, 2.0, 50.0), 30).unwrap();
        assert_eq!(d.ticket(id).unwrap().state, TicketSt::Exposed, "the bet went in and its hedge was cancelled");
        let other = d.create(tk(Mode::Real, vec![leg("beta", "home", 2.0, 10.0)]), 40, false).unwrap();
        d.cancel(other, 50).unwrap();
        assert_eq!(d.ticket(other).unwrap().state, TicketSt::Dead);
        assert!(d.cancel(other, 60).is_err());
    }

    #[test]
    fn the_journal_brings_tickets_back_and_closes_what_was_under_way() {
        let dir = std::env::temp_dir().join(format!("desk-test-{}-{:x}", std::process::id(), rand::random::<u32>()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut d = load(&dir, 0);
        assert_eq!(d.set.key.len(), 32);
        let key = d.set.key.clone();
        assert!(d.key_ok(Some(&key)) && !d.key_ok(Some("nope")) && !d.key_ok(None) && !d.key_ok(Some("")));
        d.edit(&json!({"real": true, "confirm": "REAL MONEY", "books": {"alpha": {"real": true, "max_stake": 100, "daily_cap": 500}, "beta": {"real": true, "max_stake": 100, "daily_cap": 500}}})).unwrap();
        let held = d.create(tk(Mode::Real, vec![leg("alpha", "home", 2.0, 50.0), leg("beta", "away", 2.0, 50.0)]), 1_000, false).unwrap();
        d.next("alpha", "a", 1_100).unwrap();
        let finished = d.create(tk(Mode::Real, vec![leg("beta", "home", 2.0, 20.0)]), 1_200, false).unwrap();
        let waiting = d.create(tk(Mode::Real, vec![leg("beta", "home", 2.0, 20.0)]), 1_300, false).unwrap();
        let b = d.next("beta", "b", 1_400).unwrap();
        // The older ticket's leg is handed out first.
        assert_eq!(b.ticket, finished);
        d.report(placed(b.id, 2.0, 20.0), 1_500).unwrap();
        let _ = store(&dir, &mut d);
        let last = d.next_id;

        let d2 = load(&dir, 9_000);
        assert_eq!(d2.set.key, key, "the key survives a restart");
        assert!(d2.set.real && d2.book("alpha").real);
        assert_eq!(d2.next_id, last, "ids go on where they stopped");
        let t = d2.ticket(held).unwrap();
        assert_eq!((t.state, t.legs[0].state, t.legs[1].state), (TicketSt::Check, St::Unknown, St::Cancelled));
        assert_eq!(d2.ticket(finished).unwrap().state, TicketSt::Done);
        let t = d2.ticket(waiting).unwrap();
        assert_eq!((t.state, t.legs[0].state), (TicketSt::Dead, St::Cancelled));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_login_is_typed_once_and_only_a_driver_reads_it_and_never_into_a_lock_out() {
        let dir = std::env::temp_dir().join(format!("desk-acc-{}-{:x}", std::process::id(), rand::random::<u32>()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut d = load(&dir, 0);
        let secret = format!("pw-{:x}", rand::random::<u64>());
        assert!(d.account_save(&json!({"book": "alpha", "login": "me@example.com"}), 1).is_err(), "no password, no login");
        assert!(d.account_save(&json!({"book": "polymarket", "login": "x", "password": "y"}), 1).is_err());
        d.account_save(&json!({"book": "Alpha", "login": " me@example.com ", "password": secret, "note": "main"}), 1_000).unwrap();
        // What the API shows: the login cut short, never the password.
        let shown = d.accounts_public(2_000).to_string();
        assert!(shown.contains("me…@example.com") && !shown.contains(&secret) && !shown.contains("me@example.com"), "{shown}");
        assert!(!d.public(2_000).to_string().contains(&secret));
        // The driver gets it whole.
        let got = d.account_for_driver("alpha", 3_000).unwrap();
        assert_eq!((got["login"].as_str(), got["password"].as_str(), got["generation"].as_u64()), (Some("me@example.com"), Some(secret.as_str()), Some(1)));
        assert!(d.account_for_driver("beta", 3_000).is_err(), "nothing saved for beta");
        d.account_state("alpha", "signed_in", "", 4_000).unwrap();
        assert!(d.alerts.is_empty());

        // On disk, for this user only; a restart brings it back.
        let _ = store(&dir, &mut d);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(accounts_path(&dir)).unwrap().permissions().mode() & 0o777, 0o600);
        }
        assert!(!std::fs::read_to_string(settings_path(&dir)).unwrap().contains(&secret), "the settings file holds no password");
        let mut d = load(&dir, 5_000);
        assert_eq!(d.account_for_driver("alpha", 5_100).unwrap()["password"].as_str(), Some(secret.as_str()));

        // The site wants a captcha: the owner is told once, the login is held back, tickets are refused.
        d.account_state("alpha", "needs_owner", "captcha at sign-in", 6_000).unwrap();
        d.account_state("alpha", "needs_owner", "captcha at sign-in", 6_500).unwrap();
        assert_eq!(d.alerts.len(), 1, "told once, not on every report");
        assert!(d.account_for_driver("alpha", 7_000).unwrap_err().to_string().contains("only you can do"));
        let dry = |d: &mut Desk| d.create(tk(Mode::Dry, vec![leg("alpha", "home", 2.0, 10.0)]), 8_000, false);
        assert!(dry(&mut d).unwrap_err().to_string().contains("not signed in"));
        d.create(tk(Mode::Paper, vec![leg("alpha", "home", 2.0, 10.0)]), 8_000, false).unwrap();
        // The owner signed in by hand; the driver sees it.
        d.account_state("alpha", "signed_in", "", 9_000).unwrap();
        dry(&mut d).unwrap();

        // A wrong password is never tried twice: only a new save frees it.
        d.account_state("alpha", "refused", "wrong email or password", 10_000).unwrap();
        assert!(d.account_for_driver("alpha", 10_100).unwrap_err().to_string().contains("refused this login"));
        d.account_save(&json!({"book": "alpha", "note": "fixed"}), 11_000).unwrap();
        let again = d.account_for_driver("alpha", 11_100).unwrap();
        assert_eq!((again["password"].as_str(), again["generation"].as_u64()), (Some(secret.as_str()), Some(2)), "an empty password field keeps the one on file");
        // A driver that keeps asking without ever getting in is stopped after three.
        d.account_for_driver("alpha", 11_200).unwrap();
        d.account_for_driver("alpha", 11_300).unwrap();
        assert!(d.account_for_driver("alpha", 11_400).unwrap_err().to_string().contains("3 times"));
        d.account_state("alpha", "signed_in", "", 11_500).unwrap();
        d.account_for_driver("alpha", 11_600).unwrap();
        // A book signed in to by hand has no login on file, and its state is kept all the same.
        d.account_state("gamma", "signed_in", "", 12_000).unwrap();
        assert!(d.account_for_driver("gamma", 12_100).is_err());
        d.account_save(&json!({"book": "alpha", "remove": true}), 13_000).unwrap();
        assert!(d.account_for_driver("alpha", 13_100).is_err());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn the_polymarket_limit_keeps_the_odds_after_the_fee() {
        let fee = crate::venue::Fee::PM_SPORTS;
        for cost in [0.12, 0.4, 0.5, 0.77, 0.95] {
            let p = price_for_cost(cost, fee);
            assert!((p + fee.raw(1.0, p) - cost).abs() < 1e-9, "{cost}");
        }
        // Odds of 2.0 after the fee = 50c all in = 48.75c before it; the order may pay 48c.
        let p = price_for_cost(0.5, fee);
        assert!((p - 0.48751).abs() < 1e-4, "{p}");
        // What the book gives at or under the limit, in whole shares.
        let plan = PmPlan { token: String::new(), asks: vec![(0.46, 30.0), (0.48, 50.5), (0.49, 500.0)], fee, limit: 0.48, shares: 100.0 };
        let (n, avg, room) = walk(&plan);
        assert_eq!(n, 80.0);
        assert!((avg - (30.0 * 0.46 + 50.5 * 0.48) / 80.0).abs() < 1e-9);
        assert!((room - (30.0 * (0.46 + fee.raw(1.0, 0.46)) + 50.5 * (0.48 + fee.raw(1.0, 0.48)))).abs() < 1e-9);
        let small = PmPlan { shares: 20.0, ..plan };
        assert_eq!(walk(&small).0, 20.0);
    }
}

/// The running desk end to end, in-process: a real engine holding one game at two pretend
/// bookmakers (one lists the teams the other way round) and a pretend Polymarket book. No network.
#[cfg(test)]
mod running_tests {
    use super::*;

    fn lr(book: &str, m: i64, outcome: &str, side: Option<&str>, min_odds: f64, odds: f64, stake: f64) -> LegReq {
        LegReq { book: book.into(), match_id: m, market: None, line: None, outcome: outcome.into(), side: side.map(String::from), min_odds, odds: Some(odds), stake, info: Value::Null }
    }

    fn req(mode: Mode, legs: Vec<LegReq>) -> TicketReq {
        TicketReq { mode: Some(mode), note: Some("test".into()), rebalance: None, ttl_s: Some(30), legs }
    }

    #[tokio::test]
    async fn the_running_desk_end_to_end() {
        let dir = std::env::temp_dir().join(format!("desk-run-{}-{:x}", std::process::id(), rand::random::<u32>()));
        std::fs::create_dir_all(&dir).unwrap();
        let app = pretend_app(&dir);
        let now = now_ms();
        // alpha: Natus Vincere 2.10 / Team Spirit 1.75. beta lists the same game the other way
        // round: Team Spirit 1.80 / Natus Vincere 2.05. Polymarket: Natus Vincere 46c / 48c.
        let m = pretend_prices(&app, 2.10, 1.75, now + 3_600_000);
        {
            let eng = app.engine.lock();
            assert_eq!(m, eng.books["beta"].events["B-9"].match_id, "both books' events are one match");
            assert!(eng.books["beta"].events["B-9"].swap, "beta lists the teams the other way round");
        }
        init(&app.db_path);
        let key = DESK.lock().set.key.clone();
        assert!(key_ok(Some(&key)) && !key_ok(Some("0123456789abcdef0123456789abcdef")));
        assert!(settings_path(&dir).exists(), "the key is on disk for the drivers");

        // PAPER: both legs fill at the prices Swifteye holds, the swapped book in its own terms.
        let t = create(&app, req(Mode::Paper, vec![lr("alpha", m, "home", None, 2.0, 2.10, 50.0), lr("beta", m, "away", None, 1.75, 1.80, 58.33)])).await.unwrap();
        assert_eq!(t["state"], "done", "{t}");
        assert_eq!((t["legs"][0]["odds"].as_f64(), t["legs"][1]["odds"].as_f64()), (Some(2.10), Some(1.80)));
        let info = &t["legs"][1]["info"];
        assert_eq!((info["event_id"].as_str(), info["teams_swapped"].as_bool(), info["book_outcome"].as_str(), info["pick"].as_str()), (Some("B-9"), Some(true), Some("home"), Some("Team Spirit")));
        assert_eq!(t["legs"][0]["info"]["pick"], "Natus Vincere");
        // A price under the minimum is refused, and a book Swifteye does not read has no price.
        let t = create(&app, req(Mode::Paper, vec![lr("alpha", m, "home", None, 2.5, 2.6, 10.0)])).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["state"].as_str(), t["legs"][0]["odds"].as_f64()), (Some("dead"), Some("rejected"), Some(2.10)));
        let t = create(&app, req(Mode::Paper, vec![lr("gamma", m, "home", None, 2.0, 2.1, 10.0)])).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["info"]["known"].as_bool()), (Some("dead"), Some(false)));

        // POLYMARKET (paper): the order book decides. Yes at 48c is 2.03 after the fee; buying
        // AGAINST the outcome costs what its buyers bid, seen from the other side (1 - 0.46).
        let t = create(&app, req(Mode::Paper, vec![lr("polymarket", m, "home", Some("yes"), 1.95, 2.0, 40.0), lr("polymarket", m, "home", Some("no"), 1.78, 1.81, 50.0)])).await.unwrap();
        let (yes, no) = (&t["legs"][0], &t["legs"][1]);
        assert_eq!((yes["state"].as_str(), no["state"].as_str()), (Some("placed"), Some("placed")), "{t}");
        assert!((yes["odds"].as_f64().unwrap() - 1.0 / (0.48 + 0.05 * 0.48 * 0.52)).abs() < 1e-9);
        assert!((no["odds"].as_f64().unwrap() - 1.0 / (0.54 + 0.05 * 0.54 * 0.46)).abs() < 1e-9);
        assert!((yes["filled"].as_f64().unwrap() - 80.0 * (0.48 + 0.05 * 0.48 * 0.52)).abs() < 0.01, "80 shares");
        // Odds that the book cannot give: refused, with the odds it does give.
        let t = create(&app, req(Mode::Paper, vec![lr("polymarket", m, "home", Some("yes"), 2.2, 2.3, 40.0)])).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["state"].as_str()), (Some("dead"), Some("rejected")), "{t}");
        assert!(t["legs"][0]["odds"].as_f64().unwrap() < 2.2);

        // DRY: both slips are read at once by the two drivers; a waiting driver is woken at once.
        let waiter = tokio::spawn(next_owned("alpha".into(), "drv-a".into(), 20));
        tokio::task::yield_now().await;
        let t = create(&app, req(Mode::Dry, vec![lr("alpha", m, "home", None, 2.0, 2.10, 50.0), lr("beta", m, "away", None, 1.75, 1.80, 58.33)])).await.unwrap();
        assert_eq!(t["state"], "open");
        let a = tokio::time::timeout(Duration::from_secs(3), waiter).await.expect("the waiting driver is woken by the new order").unwrap();
        let b = next("beta", "drv-b", 0).await;
        assert_eq!((a["order"]["dry_run"].as_bool(), a["order"]["info"]["event_id"].as_str(), b["order"]["info"]["pick"].as_str()), (Some(true), Some("A-1"), Some("Team Spirit")));
        assert!(next("alpha", "drv-a", 0).await["order"].is_null(), "nothing more for alpha");
        report(&app, Report { id: a["order"]["id"].as_u64().unwrap(), status: "placed".into(), odds: Some(2.08), max_stake: Some(35.0), ..Default::default() }).await.unwrap();
        let t = report(&app, Report { id: b["order"]["id"].as_u64().unwrap(), status: "placed".into(), odds: Some(1.80), max_stake: Some(500.0), ..Default::default() }).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["max_stake"].as_f64()), (Some("done"), Some(35.0)));

        // REAL on this scratch desk with pretend bookmakers: refused until switched on, then one
        // leg at a time, the second re-sized to what the first really got.
        let real = || req(Mode::Real, vec![lr("alpha", m, "home", None, 2.0, 2.10, 50.0), lr("beta", m, "away", None, 1.75, 1.80, 58.33)]);
        assert!(create(&app, real()).await.is_err());
        assert!(edit(&app, &json!({"real": true})).await.is_err());
        edit(&app, &json!({"real": true, "confirm": "REAL MONEY", "books": {"alpha": {"real": true, "max_stake": 60, "daily_cap": 100}, "beta": {"real": true, "max_stake": 60, "daily_cap": 100}}})).await.unwrap();
        let t = create(&app, real()).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["state"].as_str(), t["legs"][1]["state"].as_str()), (Some("open"), Some("queued"), Some("waiting")));
        assert!(next("beta", "drv-b", 0).await["order"].is_null(), "the second leg waits for the first");
        let a = next("alpha", "drv-a", 0).await;
        assert_eq!(a["order"]["dry_run"], false);
        // The slip only took 25 of the 50.
        report(&app, Report { id: a["order"]["id"].as_u64().unwrap(), status: "placed".into(), odds: Some(2.10), stake: Some(25.0), bet_ref: Some("A-777".into()), ..Default::default() }).await.unwrap();
        let b = next("beta", "drv-b", 0).await;
        assert_eq!(b["order"]["stake"].as_f64(), Some(29.17), "58.33 x 25 / 50");
        let t = report(&app, Report { id: b["order"]["id"].as_u64().unwrap(), status: "placed".into(), odds: Some(1.80), stake: Some(29.17), ..Default::default() }).await.unwrap();
        assert_eq!((t["state"].as_str(), t["legs"][0]["bet_ref"].as_str()), (Some("done"), Some("A-777")));
        // The 24-hour cap counts what really went in: 25 so far at alpha, so 60 more is refused.
        assert!(create(&app, req(Mode::Real, vec![lr("alpha", m, "home", None, 2.0, 2.10, 60.0)])).await.is_ok());
        assert!(create(&app, req(Mode::Real, vec![lr("alpha", m, "home", None, 2.0, 2.10, 20.0)])).await.unwrap_err().to_string().contains("24 hours"));

        // The view, and the journal on disk.
        let v = view(100, None);
        assert_eq!(v["settings"]["books"]["alpha"]["driver"]["name"], "drv-a");
        assert_eq!(v["settings"]["books"]["alpha"]["real_24h"].as_f64(), Some(85.0));
        assert!(v["settings"]["key"].as_str().unwrap() != key, "the key is never shown");
        assert_eq!(v["tickets"].as_array().unwrap().len(), 8);
        let lines = std::fs::read_to_string(journal_path(&dir)).unwrap();
        assert!(lines.lines().count() >= 8);
        std::fs::remove_dir_all(&dir).ok();
    }

    async fn next_owned(book: String, driver: String, wait_s: u64) -> Value {
        next(&book, &driver, wait_s).await
    }
}
