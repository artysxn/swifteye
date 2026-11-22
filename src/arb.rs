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
//! Two checks keep out what is not an arbitrage (20 Nov 2026: of 162 finds of 5%+ in 72 hours
//! most were a wrong link, a side the wrong way round or a price with one contract behind it):
//! * a price 12+ points better than three or more other sources say is left out (`FAR`);
//! * a prediction-market leg carries the dollars resting at its price, and the set the largest
//!   total stake those legs can take (`takeable`).
//! A set no other source could confirm is marked `few_sources`. Bookmaker bet limits and each
//! book's settlement rules are still not checked.

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
/// A price this much (in probability) better than what the other sources say is a wrong link, a
/// side the wrong way round or a feed error: left out when 3+ other sources price the outcome.
const FAR: f64 = 0.12;
/// Fewer other sources than this for a leg: the set is marked as not cross-checked.
const MIN_REFS: usize = 2;
/// A set whose prediction-market legs can take less than this in total is not worth showing.
pub const MIN_TAKE: f64 = 10.0;

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
    /// Dollars resting at this price (prediction markets; None = not known, as for bookmakers).
    pub avail: Option<f64>,
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
    /// The largest total stake the prediction-market legs can take at these prices (None when
    /// no leg's size is known: bookmakers only).
    pub takeable: Option<f64>,
    /// "ok" = every leg's price agrees with at least two other sources; "few_sources" = it could
    /// not be cross-checked.
    pub check: &'static str,
}

impl Arb {
    /// Cross-checked, and the prediction-market legs (if any) can take a stake worth placing.
    pub fn sound(&self) -> bool {
        self.check == "ok" && self.takeable.is_none_or(|t| t >= MIN_TAKE)
    }
}

#[derive(Clone)]
struct Cand {
    book: String,
    side: &'static str,
    against: Option<&'static str>,
    odds: f64,
    updated_ms: Ms,
    avail: Option<f64>,
}

/// Implied probabilities with the bookmaker's margin removed (each to the same power, summing to 1).
fn power(pi: &[f64]) -> Vec<f64> {
    let s: f64 = pi.iter().sum();
    if s <= 1.0 {
        return pi.iter().map(|p| p / s).collect();
    }
    let (mut lo, mut hi) = (1.0f64, 6.0f64);
    for _ in 0..40 {
        let k = (lo + hi) / 2.0;
        if pi.iter().map(|p| p.powf(k)).sum::<f64>() > 1.0 {
            lo = k;
        } else {
            hi = k;
        }
    }
    pi.iter().map(|p| p.powf((lo + hi) / 2.0)).collect()
}

/// source -> its fair probability for each of `outs`, from the bookmakers that price them all.
fn fair_books(g: &Game, market: Market, line: i32, outs: &[Outcome]) -> HashMap<String, Vec<f64>> {
    let mut by: HashMap<&str, Vec<Option<f64>>> = HashMap::new();
    for (i, o) in outs.iter().enumerate() {
        for c in g.prices.get(&(market, line, *o)).into_iter().flatten() {
            by.entry(c.book.as_str()).or_insert_with(|| vec![None; outs.len()])[i] = Some(1.0 / c.odds);
        }
    }
    by.into_iter()
        .filter_map(|(b, v)| {
            let pi: Vec<f64> = v.into_iter().collect::<Option<Vec<f64>>>()?;
            let s: f64 = pi.iter().sum();
            (1.0..=1.30).contains(&s).then(|| (b.to_string(), power(&pi)))
        })
        .collect()
}

/// (how many other sources price it, how far their middle is above `p`) for a bet from `book`
/// that pays with probability `pay(source's fair values)`. Sources of the same platform as `book`
/// do not count: they share one price.
fn others_say(fair: &HashMap<String, Vec<f64>>, book: &str, p: f64, pay: impl Fn(&[f64]) -> f64) -> (usize, f64) {
    let me = crate::snipe::platform(book);
    let mut v: Vec<f64> = fair.iter().filter(|(b, _)| crate::snipe::platform(b) != me).map(|(_, f)| pay(f)).collect();
    if v.is_empty() {
        return (0, 0.0);
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    (v.len(), v[v.len() / 2] - p)
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

/// Latest (bid, ask, ts, contracts at the bid, contracts at the ask) per outcome name from a
/// prediction-market quote table.
#[allow(clippy::type_complexity)]
fn pred_quotes(c: &Connection, sql: &str, key: &dyn rusqlite::ToSql, outcome: &str, since: Ms) -> Option<(Option<f64>, Option<f64>, Ms, Option<f64>, Option<f64>)> {
    let mut st = c.prepare_cached(sql).ok()?;
    st.query_row(params![key, outcome, since], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))).optional().ok()?
}

fn legs_market(m: &str) -> Market {
    match m {
        "total" => Market::Total,
        "handicap" => Market::Handicap,
        "btts" => Market::Btts,
        _ => Market::X12,
    }
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
                                g.prices.entry(*k).or_default().push(Cand { book: book.to_string(), side: "back", against: None, odds: p, updated_ms: q.updated_ms, avail: None });
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
        // Prediction-market Yes/No per 1X2 outcome: (book, outcome, yes cost, no cost, ts,
        // dollars resting at the Yes price, dollars resting at the No price).
        #[allow(clippy::type_complexity)]
        let mut pred: Vec<(&str, Outcome, Option<f64>, Option<f64>, Ms, Option<f64>, Option<f64>)> = Vec::new();
        // The 1X2 market as each source sees it, margin removed: what a price is checked against.
        let mut fair = fair_books(&g, Market::X12, line_key(None), wins);
        if let Some(c) = &c {
            let mut mids: HashMap<&str, Vec<Option<f64>>> = HashMap::new();
            for (i, &o) in wins.iter().enumerate() {
                let ok = |x: Option<f64>| x.filter(|x| *x > 0.0 && *x < 1.0);
                let mut add = |book: &'static str, b: Option<f64>, a: Option<f64>, ts: Ms, bs: Option<f64>, asz: Option<f64>, cost: fn(f64) -> f64| {
                    let (b, a) = (ok(b), ok(a));
                    if let (Some(b), Some(a)) = (b, a) {
                        mids.entry(book).or_insert_with(|| vec![None; wins.len()])[i] = Some((b + a) / 2.0);
                    }
                    // Buying Yes takes the asks; buying No takes the Yes bids, at 1 - bid each.
                    pred.push((book, o, a.map(cost), b.map(|b| cost(1.0 - b)), ts, a.zip(asz).map(|(a, n)| a * n), b.zip(bs).map(|(b, n)| (1.0 - b) * n)));
                };
                if g.pm {
                    let sql = "SELECT bid, ask, ts_ms, bid_size, ask_size FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 ORDER BY ts_ms DESC LIMIT 1";
                    if let Some((b, a, ts, bs, asz)) = pred_quotes(c, sql, &mid, o.as_str(), since) {
                        add(PM, b, a, ts, bs, asz, pm_cost);
                    }
                }
                if let Some((ev, swap)) = &g.kalshi {
                    let sql = "SELECT bid, ask, ts_ms, bid_size, ask_size FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND est = 0 AND ts_ms >= ?3 ORDER BY ts_ms DESC LIMIT 1";
                    if let Some((b, a, ts, bs, asz)) = pred_quotes(c, sql, ev, crate::snipe::kalshi_side(o.as_str(), *swap), since) {
                        add(KALSHI, b, a, ts, bs, asz, kalshi_cost);
                    }
                }
            }
            for (book, v) in mids {
                if let Some(m) = v.into_iter().collect::<Option<Vec<f64>>>() {
                    let s: f64 = m.iter().sum();
                    if (0.85..=1.20).contains(&s) {
                        fair.insert(book.to_string(), m.iter().map(|x| x / s).collect());
                    }
                }
            }
        }
        let idx = |o: Outcome| wins.iter().position(|w| *w == o).unwrap_or(0);
        // A bet that pays when `o` wins (`on`), or when it does not (a No in a three-way market).
        let far = |book: &str, p: f64, o: Outcome, on: bool| {
            let i = idx(o);
            let (n, d) = others_say(&fair, book, p, |f| if on { f[i] } else { 1.0 - f[i] });
            n >= 3 && d >= FAR
        };

        // ---- 1X2 / winner
        let mut best: HashMap<Outcome, Option<Cand>> = wins.iter().map(|o| (*o, None)).collect();
        let mut best_no: HashMap<Outcome, Option<Cand>> = HashMap::new();
        for &o in wins {
            for cnd in g.prices.get(&(Market::X12, line_key(None), o)).into_iter().flatten() {
                if far(&cnd.book, 1.0 / cnd.odds, o, true) {
                    continue;
                }
                better(best.get_mut(&o).unwrap(), cnd.clone());
            }
        }
        for (book, o, yes, no, ts, yes_avail, no_avail) in &pred {
            if let Some(y) = yes.filter(|y| !far(book, *y, *o, true)) {
                better(best.get_mut(o).unwrap(), Cand { book: book.to_string(), side: "yes", against: None, odds: 1.0 / y, updated_ms: *ts, avail: *yes_avail });
            }
            if let Some(n) = no.filter(|n| !far(book, *n, *o, false)) {
                let cand = Cand { book: book.to_string(), side: "no", against: Some(o.as_str()), odds: 1.0 / n, updated_ms: *ts, avail: *no_avail };
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
            let fair_line = fair_books(&g, *m, *l, &pair);
            for (i, o) in pair.into_iter().enumerate() {
                let mut b = None;
                for cnd in g.prices.get(&(*m, *l, o)).into_iter().flatten() {
                    let (n, d) = others_say(&fair_line, &cnd.book, 1.0 / cnd.odds, |f| f[i]);
                    if n >= 3 && d >= FAR {
                        continue;
                    }
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
            // Could every leg's price be confirmed by other sources?
            let fair_m = if market == "1x2" { None } else { Some(fair_books(&g, legs_market(market), line_key(line), &legs.iter().map(|(o, _)| *o).collect::<Vec<_>>())) };
            let confirmed = legs.iter().enumerate().all(|(i, (o, c))| {
                let n = match &fair_m {
                    Some(f) => others_say(f, &c.book, 1.0 / c.odds, |v| v[i]).0,
                    None => {
                        let j = idx(c.against.and_then(|a| wins.iter().copied().find(|w| w.as_str() == a)).unwrap_or(*o));
                        others_say(&fair, &c.book, 1.0 / c.odds, |v| v[j]).0
                    }
                };
                n >= MIN_REFS
            });
            if !confirmed {
                warn.push("Too few other sources price this to cross-check it");
            }
            // Each leg's share of the total stake is 1 / (odds x cost): the leg with the least
            // resting at its price sets how much the whole set can take.
            let takeable = legs.iter().filter_map(|(_, c)| c.avail.map(|a| a * c.odds * cost)).fold(None, |m: Option<f64>, x| Some(m.map_or(x, |y| y.min(x))));
            if takeable.is_some_and(|t| t < MIN_TAKE) {
                warn.push("Almost nothing is on offer at the prediction-market price");
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
                    .map(|(o, c)| Leg { outcome: o.as_str(), book: c.book, side: c.side, against: c.against, odds: c.odds, stake: stake / (c.odds * cost), updated_ms: c.updated_ms, avail: c.avail })
                    .collect(),
                cost,
                profit,
                sources: g.sources,
                warn,
                takeable,
                check: if confirmed { "ok" } else { "few_sources" },
            });
        }
    }
    out.sort_by(|a, b| b.profit.partial_cmp(&a.profit).unwrap_or(std::cmp::Ordering::Equal));
    out.truncate(500);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn margin_is_removed_so_the_probabilities_sum_to_one() {
        // 1.30 / 3.40: implied 76.9% + 29.4% = 106.3%.
        let f = power(&[1.0 / 1.30, 1.0 / 3.40]);
        assert!((f[0] + f[1] - 1.0).abs() < 1e-6, "{f:?}");
        // The power method takes more of the margin off the underdog than a plain rescale would.
        assert!(f[1] < (1.0 / 3.40) / (1.0 / 1.30 + 1.0 / 3.40), "{f:?}");
        assert!(f[0] > 0.73 && f[0] < 0.76, "{f:?}");
    }

    fn fair(rows: &[(&str, f64)]) -> HashMap<String, Vec<f64>> {
        rows.iter().map(|(b, home)| (b.to_string(), vec![*home, 1.0 - *home])).collect()
    }

    #[test]
    fn a_price_far_from_every_other_source_is_caught() {
        // Six sources put the home side near 70%; leovegas offers it as if it were 36%.
        let f = fair(&[("pinnacle", 0.70), ("marathon", 0.71), ("leon", 0.69), ("duel", 0.70), ("m88", 0.72), ("olimp", 0.70), ("leovegas", 0.33)]);
        let (n, d) = others_say(&f, "leovegas", 0.36, |v| v[0]);
        assert_eq!(n, 6);
        assert!(d >= FAR, "{d}");
        // A price 3 points better than the rest is an arbitrage candidate, not an error.
        let (_, d) = others_say(&f, "marathon", 0.67, |v| v[0]);
        assert!(d < FAR && d > 0.0, "{d}");
    }

    #[test]
    fn brands_of_one_platform_do_not_confirm_each_other() {
        // Unibet, LeoVegas and Paf share one price feed: for a Unibet price only pinnacle counts.
        let f = fair(&[("unibet", 0.40), ("leovegas", 0.40), ("paf", 0.40), ("pinnacle", 0.70)]);
        let (n, _) = others_say(&f, "unibet", 0.40, |v| v[0]);
        assert_eq!(n, 1);
        assert!(n < MIN_REFS);
    }

    #[test]
    fn a_set_is_sound_only_when_checked_and_worth_placing() {
        let mut a = Arb {
            key: String::new(), match_id: 1, sport: "tennis".into(), home: "A".into(), away: "B".into(), league: String::new(), start_ms: None, market: "1x2", line: None,
            legs: Vec::new(), cost: 0.95, profit: 0.05, sources: 5, warn: Vec::new(), takeable: Some(4.3), check: "ok",
        };
        assert!(!a.sound(), "$4 on offer is not worth showing");
        a.takeable = Some(250.0);
        assert!(a.sound());
        a.takeable = None; // bookmakers only: limits unknown, still shown
        assert!(a.sound());
        a.check = "few_sources";
        assert!(!a.sound());
    }
}

