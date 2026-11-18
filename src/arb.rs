//! Arbitrage Scout: PRE-MATCH prices across every source, looking for one bet on each outcome
//! of a market whose combined cost is below the payout (sum of 1/odds < 1).
//!
//! * Bookmakers: the live decimal price in memory (event seen in the last 5 minutes).
//! * Polymarket / Kalshi: the engine keeps only the mid, which nobody can trade at, so these
//!   legs use the recorded best ASK (`pm_quotes` / `kalshi_quotes`, last 60 minutes). Yes on an
//!   outcome costs its ask; No costs 1 - Yes bid. Kalshi's taker fee (7% x p x (1-p)) and
//!   Polymarket's sports taker fee (5% x p x (1-p)) are added to the cost.
//! * Markets: 1X2 (home/draw/away, or home/away for two-way sports), total over/under and
//!   handicap home/away at the same line, both-teams-to-score yes/no. In a three-way market a
//!   prediction market's No on one outcome also covers the other two (a two-leg set).
//! Nothing checks order-book depth, bet limits or each book's settlement rules.

use crate::model::*;
use crate::App;
use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};

const PM: &str = "polymarket";
const KALSHI: &str = "kalshi";
/// A bookmaker event not seen for this long is not trusted.
const FRESH_MS: Ms = 5 * 60_000;
/// Prediction-market quotes older than this are not used.
const PRED_FRESH_MS: Ms = 60 * 60_000;

#[derive(Serialize, Clone, Debug)]
pub struct Leg {
    pub outcome: &'static str,
    pub book: String,
    /// "back" (bookmaker), "yes" or "no" (prediction market; "no" = against `covers`).
    pub side: &'static str,
    /// For a "no" leg: the outcome bet against.
    pub against: Option<&'static str>,
    pub odds: f64,
    pub stake: f64,
    pub updated_ms: Ms,
}

#[derive(Serialize, Clone, Debug)]
pub struct Arb {
    pub key: String,
    pub match_id: i64,
    pub sport: String,
    pub home: String,
    pub away: String,
    pub league: String,
    pub start_ms: Option<Ms>,
    pub market: &'static str,
    pub line: Option<f64>,
    pub legs: Vec<Leg>,
    /// Sum of 1/odds over the legs (< 1 = arbitrage).
    pub cost: f64,
    /// Guaranteed return on the total stake: 1/cost - 1.
    pub profit: f64,
    /// Sources pricing this match.
    pub sources: usize,
    pub warn: Vec<&'static str>,
}

#[derive(Clone)]
struct Cand {
    book: String,
    side: &'static str,
    against: Option<&'static str>,
    odds: f64,
    updated_ms: Ms,
}

fn better(slot: &mut Option<Cand>, c: Cand) {
    if slot.as_ref().map_or(true, |s| c.odds > s.odds) {
        *slot = Some(c);
    }
}

fn kalshi_cost(p: f64) -> f64 {
    p + 0.07 * p * (1.0 - p)
}

fn pm_cost(p: f64) -> f64 {
    p + crate::venue::Fee::PM_SPORTS.raw(1.0, p)
}

struct Game {
    sport: String,
    home: String,
    away: String,
    league: String,
    start_ms: Option<Ms>,
    /// (market, line key, outcome) -> book prices
    prices: BTreeMap<(Market, i32, Outcome), Vec<Cand>>,
    pm: bool,
    kalshi: Option<(String, bool)>,
    sources: usize,
}

/// Latest (bid, ask, ts) per outcome name from a prediction-market quote table.
fn pred_quotes(c: &Connection, sql: &str, key: &dyn rusqlite::ToSql, outcome: &str, since: Ms) -> Option<(Option<f64>, Option<f64>, Ms)> {
    let mut st = c.prepare_cached(sql).ok()?;
    st.query_row(params![key, outcome, since], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).optional().ok()?
}

pub fn scan(app: &App, min_profit: f64, stake: f64) -> Vec<Arb> {
    let now = now_ms();
    let mut games: HashMap<i64, Game> = HashMap::new();
    {
        let eng = app.engine.lock();
        for (book, bs) in &eng.books {
            for es in bs.events.values() {
                let m = eng.matcher.matches.get(&es.match_id);
                let start = m.and_then(|m| m.start_ms).or(es.meta.start_ms);
                // Pre-match only: not live anywhere we can see, kick-off still ahead.
                if es.meta.live || start.map_or(true, |s| s <= now) || now - es.last_seen_ms > FRESH_MS {
                    continue;
                }
                let g = games.entry(es.match_id).or_insert_with(|| Game {
                    sport: m.map(|m| m.sport.clone()).unwrap_or_else(|| es.meta.sport.clone()),
                    home: m.map(|m| m.home.clone()).unwrap_or_else(|| es.meta.home.clone()),
                    away: m.map(|m| m.away.clone()).unwrap_or_else(|| es.meta.away.clone()),
                    league: es.meta.league.clone(),
                    start_ms: start,
                    prices: BTreeMap::new(),
                    pm: false,
                    kalshi: None,
                    sources: 0,
                });
                g.sources += 1;
                match *book {
                    PM => g.pm = true,
                    KALSHI => g.kalshi = Some((es.meta.id.clone(), es.swap)),
                    _ => {
                        for (k, q) in &es.quotes {
                            if let Some(p) = q.price.filter(|p| *p > 1.0) {
                                g.prices.entry(*k).or_default().push(Cand { book: book.to_string(), side: "back", against: None, odds: p, updated_ms: q.updated_ms });
                            }
                        }
                    }
                }
            }
        }
    }
    let c = crate::store::open_ro(&app.db_path).ok();
    let since = now - PRED_FRESH_MS;
    let mut out = Vec::new();
    for (mid, g) in games {
        let three = crate::books::three_way(&g.sport);
        let wins: &[Outcome] = if three { &[Outcome::Home, Outcome::Draw, Outcome::Away] } else { &[Outcome::Home, Outcome::Away] };
        // Prediction-market Yes/No costs per 1X2 outcome: (book, yes cost, no cost, ts).
        let mut pred: Vec<(&str, Outcome, Option<f64>, Option<f64>, Ms)> = Vec::new();
        if let Some(c) = &c {
            for &o in wins {
                if g.pm {
                    let sql = "SELECT bid, ask, ts_ms FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 ORDER BY ts_ms DESC LIMIT 1";
                    if let Some((b, a, ts)) = pred_quotes(c, sql, &mid, o.as_str(), since) {
                        pred.push((PM, o, a.filter(|a| *a > 0.0 && *a < 1.0).map(pm_cost), b.filter(|b| *b > 0.0 && *b < 1.0).map(|b| pm_cost(1.0 - b)), ts));
                    }
                }
                if let Some((ev, swap)) = &g.kalshi {
                    let sql = "SELECT bid, ask, ts_ms FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND est = 0 AND ts_ms >= ?3 ORDER BY ts_ms DESC LIMIT 1";
                    if let Some((b, a, ts)) = pred_quotes(c, sql, ev, crate::snipe::kalshi_side(o.as_str(), *swap), since) {
                        let yes = a.filter(|a| *a > 0.0 && *a < 1.0).map(kalshi_cost);
                        let no = b.filter(|b| *b > 0.0 && *b < 1.0).map(|b| kalshi_cost(1.0 - b));
                        pred.push((KALSHI, o, yes, no, ts));
                    }
                }
            }
        }

        // ---- 1X2 / winner
        let mut best: HashMap<Outcome, Option<Cand>> = wins.iter().map(|o| (*o, None)).collect();
        let mut best_no: HashMap<Outcome, Option<Cand>> = HashMap::new();
        for &o in wins {
            for cnd in g.prices.get(&(Market::X12, line_key(None), o)).into_iter().flatten() {
                better(best.get_mut(&o).unwrap(), cnd.clone());
            }
        }
        for (book, o, yes, no, ts) in &pred {
            if let Some(y) = yes {
                better(best.get_mut(o).unwrap(), Cand { book: book.to_string(), side: "yes", against: None, odds: 1.0 / y, updated_ms: *ts });
            }
            if let Some(n) = no {
                let cand = Cand { book: book.to_string(), side: "no", against: Some(o.as_str()), odds: 1.0 / n, updated_ms: *ts };
                if three {
                    better(best_no.entry(*o).or_insert(None), cand);
                } else {
                    // Two-way: No on one side is the other side.
                    let other = if *o == Outcome::Home { Outcome::Away } else { Outcome::Home };
                    better(best.get_mut(&other).unwrap(), cand);
                }
            }
        }
        let mut sets: Vec<(&'static str, Option<f64>, Vec<(Outcome, Cand)>)> = Vec::new();
        if wins.iter().all(|o| best[o].is_some()) {
            sets.push(("1x2", None, wins.iter().map(|o| (*o, best[o].clone().unwrap())).collect()));
        }
        if three {
            for &o in wins {
                if let (Some(Some(yes)), Some(Some(no))) = (best.get(&o), best_no.get(&o)) {
                    sets.push(("1x2", None, vec![(o, yes.clone()), (o, no.clone())]));
                }
            }
        }
        // ---- totals, handicaps, both teams to score (bookmakers only)
        let mut lines: BTreeMap<(Market, i32), ()> = BTreeMap::new();
        for (m, l, _) in g.prices.keys() {
            if *m != Market::X12 {
                lines.insert((*m, *l), ());
            }
        }
        for (m, l) in lines.keys() {
            let pair = match m {
                Market::Total => [Outcome::Over, Outcome::Under],
                Market::Handicap => [Outcome::Home, Outcome::Away],
                Market::Btts => [Outcome::Yes, Outcome::No],
                Market::X12 => continue,
            };
            let mut legs = Vec::new();
            for o in pair {
                let mut b = None;
                for cnd in g.prices.get(&(*m, *l, o)).into_iter().flatten() {
                    better(&mut b, cnd.clone());
                }
                if let Some(b) = b {
                    legs.push((o, b));
                }
            }
            if legs.len() == 2 {
                sets.push((m.as_str(), line_from_key(*l), legs));
            }
        }

        for (market, line, legs) in sets {
            let cost: f64 = legs.iter().map(|(_, c)| 1.0 / c.odds).sum();
            let profit = 1.0 / cost - 1.0;
            if profit < min_profit || legs.iter().all(|(_, c)| c.book == legs[0].1.book) {
                continue;
            }
            let mut warn = Vec::new();
            if profit > 0.10 {
                warn.push("Unusually large: check that every source lists the same game");
            }
            if legs.iter().any(|(_, c)| c.side != "back") {
                warn.push("Prediction-market leg at the best ask: check the order book depth");
            }
            if market == "handicap" || market == "total" {
                warn.push("Settlement rules (pushes, quarter lines) can differ between books");
            }
            if market == "1x2" && !three {
                warn.push("Winner market: check whether each book includes overtime / retirements");
            }
            let key = format!("{mid}|{market}|{}|{}", line.map(|l| l.to_string()).unwrap_or_default(), legs.iter().map(|(o, c)| format!("{}:{}:{}", o.as_str(), c.book, c.side)).collect::<Vec<_>>().join(","));
            out.push(Arb {
                key,
                match_id: mid,
                sport: g.sport.clone(),
                home: g.home.clone(),
                away: g.away.clone(),
                league: g.league.clone(),
                start_ms: g.start_ms,
                market,
                line,
                legs: legs
                    .into_iter()
                    .map(|(o, c)| Leg { outcome: o.as_str(), book: c.book, side: c.side, against: c.against, odds: c.odds, stake: stake / (c.odds * cost), updated_ms: c.updated_ms })
                    .collect(),
                cost,
                profit,
                sources: g.sources,
                warn,
            });
        }
    }
    out.sort_by(|a, b| b.profit.partial_cmp(&a.profit).unwrap_or(std::cmp::Ordering::Equal));
    out.truncate(500);
    out
}
