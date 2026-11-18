//! Lead/lag between bookmakers and Polymarket on the 1X2 market.
//!
//! Prices become implied probabilities (1 / decimal odds). A *jump* is a move of at
//! least `min_jump` (e.g. 0.04 = 4 points) within `settle_ms`. For each jump on the
//! trigger side we look at the other side over ±`window_ms`: if it moved the same way
//! by at least half as much, the moment it crossed the halfway level of its own move is
//! its reaction time. Both sides are timed at their halfway crossing.
//!
//! delay_ms = polymarket_time - book_time   (positive: Polymarket was later)
//!
//! trigger = "book":       a bookmaker jumped; how long until Polymarket followed.
//! trigger = "polymarket": Polymarket jumped; how long until each book followed
//!                         (plus "first book": the earliest of them).

use crate::model::Ms;
use anyhow::Result;
use rusqlite::params;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};
use std::path::Path;

pub const PM: &str = "polymarket";

#[derive(Clone, Debug)]
pub struct Params {
    pub since: Ms,
    pub until: Ms,
    pub min_jump: f64,
    pub window_ms: i64,
    pub settle_ms: i64,
    /// Use each source's own timestamp (book_ts_ms) where it has one, instead of our receive time.
    pub source_clock: bool,
    pub match_id: Option<i64>,
    pub book: Option<String>,
    /// Rankings only: reference bookmaker for the "vs fastest overall" delay (None = pick it).
    pub fastest: Option<String>,
    /// Rankings only: one sport key, or "esports" for every e-sports title (None = all).
    pub sport: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct LagEvent {
    pub match_id: i64,
    pub home: String,
    pub away: String,
    pub outcome: String,
    pub trigger: &'static str,
    pub book: String,
    pub book_ms: Option<Ms>,
    pub pm_ms: Option<Ms>,
    /// Polymarket time minus book time. Null when the other side did not follow.
    pub delay_ms: Option<i64>,
    pub followed: bool,
    pub book_from: Option<f64>,
    pub book_to: Option<f64>,
    pub pm_from: Option<f64>,
    pub pm_to: Option<f64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Summary {
    pub trigger: &'static str,
    pub book: String,
    pub jumps: usize,
    pub followed: usize,
    pub median_delay_ms: Option<i64>,
    pub p25_delay_ms: Option<i64>,
    pub p75_delay_ms: Option<i64>,
    pub mean_delay_ms: Option<i64>,
    /// Share of followed jumps where Polymarket crossed first (delay < 0).
    pub polymarket_first_share: Option<f64>,
}

pub(crate) type Series = Vec<(Ms, f64)>;

pub(crate) fn value_at(s: &Series, t: Ms) -> Option<f64> {
    let i = s.partition_point(|p| p.0 <= t);
    if i == 0 {
        None
    } else {
        Some(s[i - 1].1)
    }
}

/// First time in [from, to] the series is at/over `level` (up) or at/under it (down).
fn cross_time(s: &Series, from: Ms, to: Ms, level: f64, up: bool) -> Option<Ms> {
    let hit = |v: f64| if up { v >= level } else { v <= level };
    if let Some(v) = value_at(s, from) {
        if hit(v) {
            return Some(from);
        }
    }
    let start = s.partition_point(|p| p.0 <= from);
    s[start..].iter().take_while(|p| p.0 <= to).find(|p| hit(p.1)).map(|p| p.0)
}

/// Times a series came back after a gap in recording (an `open` row after earlier rows:
/// collector restart, or the event dropped out of the source's feed and returned).
pub(crate) type Breaks = BTreeMap<(String, String), Vec<Ms>>;

/// Note a gap when a source's (book, outcome) re-opens after it had already been seen.
pub(crate) fn note_break(breaks: &mut Breaks, series: &BTreeMap<(String, String), Series>, key: &(String, String), kind: &str, t: Ms) {
    if kind == "open" && series.get(key).is_some_and(|s| !s.is_empty()) {
        breaks.entry(key.clone()).or_default().push(t);
    }
}

/// (time of halfway crossing, level before, level after). A move whose settle window spans a
/// break is not a move: it is the difference between before and after a gap in recording.
pub(crate) fn jumps(s: &Series, breaks: &[Ms], min_jump: f64, settle_ms: i64) -> Vec<(Ms, f64, f64)> {
    let mut out = Vec::new();
    let mut skip_until = Ms::MIN;
    for &(t, p) in s {
        if t <= skip_until {
            continue;
        }
        if breaks.iter().any(|&b| b > t - settle_ms && b <= t + settle_ms) {
            continue;
        }
        let Some(before) = value_at(s, t - settle_ms).or_else(|| None) else { continue };
        if (p - before).abs() < min_jump {
            continue;
        }
        let after = value_at(s, t + settle_ms).unwrap_or(p);
        if (after - before).abs() < min_jump {
            continue; // spike that reverted
        }
        let half = before + (after - before) / 2.0;
        let tc = cross_time(s, t - settle_ms, t + settle_ms, half, after > before).unwrap_or(t);
        out.push((tc, before, after));
        skip_until = t + settle_ms;
    }
    out
}

/// One match's 1X2 history as implied-probability series per (source, outcome), with the
/// gaps in recording. `keep(book)` picks the sources.
pub(crate) fn load_series(
    c: &rusqlite::Connection,
    match_id: i64,
    lo: Ms,
    hi: Ms,
    source_clock: bool,
    keep: impl Fn(&str) -> bool,
) -> Result<(BTreeMap<(String, String), Series>, Breaks)> {
    let mut series: BTreeMap<(String, String), Series> = BTreeMap::new();
    let mut breaks = Breaks::new();
    let mut st = c.prepare_cached(
        "SELECT book, outcome, ts_ms, book_ts_ms, new_price, kind FROM changes
         WHERE match_id = ?1 AND market = '1x2' AND ts_ms BETWEEN ?2 AND ?3 ORDER BY ts_ms, id",
    )?;
    let rows = st.query_map(params![match_id, lo, hi], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, Option<i64>>(3)?,
            r.get::<_, Option<f64>>(4)?,
            r.get::<_, String>(5)?,
        ))
    })?;
    for row in rows {
        let (book, outcome, ts, bts, price, kind) = row?;
        if !keep(&book) {
            continue;
        }
        let t = if source_clock { bts.unwrap_or(ts) } else { ts };
        let key = (book, outcome);
        note_break(&mut breaks, &series, &key, &kind, t);
        let Some(price) = price.filter(|x| *x > 1.0) else { continue };
        series.entry(key).or_default().push((t, 1.0 / price));
    }
    for s in series.values_mut() {
        s.sort_by_key(|x| x.0);
    }
    Ok((series, breaks))
}

/// How a follower series reacted around time `t` to a move from `from` to `to`.
pub(crate) fn follow(f: &Series, t: Ms, from: f64, to: f64, window: i64) -> (Option<Ms>, Option<f64>, Option<f64>, bool) {
    // Level before: at the start of the window, or the first value seen inside it
    // (the series may start mid-window, e.g. just after we subscribed).
    let before = value_at(f, t - window).or_else(|| f.iter().find(|p| p.0 >= t - window && p.0 <= t).map(|p| p.1));
    let (Some(fb), Some(fa)) = (before, value_at(f, t + window)) else {
        return (None, None, None, false);
    };
    let d = to - from;
    let df = fa - fb;
    let ok = df.signum() == d.signum() && df.abs() >= 0.5 * d.abs();
    if !ok {
        return (None, Some(fb), Some(fa), false);
    }
    let tc = cross_time(f, t - window, t + window, fb + df / 2.0, df > 0.0);
    (tc, Some(fb), Some(fa), tc.is_some())
}

pub fn compute(db: &Path, p: &Params) -> Result<Vec<LagEvent>> {
    let c = crate::store::open_ro(db)?;
    let lo = p.since - p.window_ms - p.settle_ms;
    let hi = p.until + p.window_ms + p.settle_ms;

    // Matches that have Polymarket prices in range.
    let mut ids: Vec<i64> = {
        let mut st = c.prepare("SELECT DISTINCT match_id FROM changes WHERE book = ?1 AND market = '1x2' AND ts_ms BETWEEN ?2 AND ?3")?;
        let rows = st.query_map(params![PM, lo, hi], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };
    if let Some(m) = p.match_id {
        ids.retain(|x| *x == m);
    }

    let mut names: HashMap<i64, (String, String)> = HashMap::new();
    let mut out = Vec::new();
    for mid in ids {
        let mut st = c.prepare_cached("SELECT home, away FROM matches WHERE id = ?1")?;
        if let Ok(n) = st.query_row([mid], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))) {
            names.insert(mid, n);
        }
        let (series, breaks) = load_series(&c, mid, lo, hi, p.source_clock, |_| true)?;
        let none: Vec<Ms> = Vec::new();
        let brk = |b: &str, o: &str| breaks.get(&(b.to_string(), o.to_string())).unwrap_or(&none);
        let (home, away) = names.get(&mid).cloned().unwrap_or_default();
        for outcome in ["home", "draw", "away"] {
            let Some(pm) = series.get(&(PM.to_string(), outcome.to_string())) else { continue };
            let books: Vec<(&String, &Series)> = series
                .iter()
                .filter(|((b, o), _)| b != PM && o == outcome && p.book.as_ref().map_or(true, |x| x == b))
                .map(|((b, _), s)| (b, s))
                .collect();

            // Book jumps -> Polymarket reaction.
            for (book, bs) in &books {
                for (tb, from, to) in jumps(bs, brk(book, outcome), p.min_jump, p.settle_ms) {
                    if tb < p.since || tb >= p.until {
                        continue;
                    }
                    // Polymarket must already have been observed before the jump,
                    // otherwise "did not follow" just means "not recorded yet".
                    if pm.first().map_or(true, |f| f.0 > tb - p.settle_ms) {
                        continue;
                    }
                    let (tp, pf, pt, ok) = follow(pm, tb, from, to, p.window_ms);
                    out.push(LagEvent {
                        match_id: mid,
                        home: home.clone(),
                        away: away.clone(),
                        outcome: outcome.into(),
                        trigger: "book",
                        book: (*book).clone(),
                        book_ms: Some(tb),
                        pm_ms: tp,
                        delay_ms: tp.map(|x| x - tb),
                        followed: ok,
                        book_from: Some(from),
                        book_to: Some(to),
                        pm_from: pf,
                        pm_to: pt,
                    });
                }
            }

            // Polymarket jumps -> each book's reaction, and the first book to react.
            for (tp, from, to) in jumps(pm, brk(PM, outcome), p.min_jump, p.settle_ms) {
                if tp < p.since || tp >= p.until {
                    continue;
                }
                let mut first: Option<(Ms, &String)> = None;
                for (book, bs) in &books {
                    if bs.first().map_or(true, |f| f.0 > tp - p.settle_ms) {
                        continue;
                    }
                    let (tb, bf, bt, ok) = follow(bs, tp, from, to, p.window_ms);
                    if let Some(t) = tb {
                        if first.map_or(true, |(f, _)| t < f) {
                            first = Some((t, book));
                        }
                    }
                    out.push(LagEvent {
                        match_id: mid,
                        home: home.clone(),
                        away: away.clone(),
                        outcome: outcome.into(),
                        trigger: "polymarket",
                        book: (*book).clone(),
                        book_ms: tb,
                        pm_ms: Some(tp),
                        delay_ms: tb.map(|b| tp - b),
                        followed: ok,
                        book_from: bf,
                        book_to: bt,
                        pm_from: Some(from),
                        pm_to: Some(to),
                    });
                }
                if !books.is_empty() {
                    out.push(LagEvent {
                        match_id: mid,
                        home: home.clone(),
                        away: away.clone(),
                        outcome: outcome.into(),
                        trigger: "polymarket",
                        book: match first {
                            Some((_, b)) => format!("first book ({b})"),
                            None => "first book".into(),
                        },
                        book_ms: first.map(|f| f.0),
                        pm_ms: Some(tp),
                        delay_ms: first.map(|(b, _)| tp - b),
                        followed: first.is_some(),
                        book_from: None,
                        book_to: None,
                        pm_from: Some(from),
                        pm_to: Some(to),
                    });
                }
            }
        }
    }
    out.sort_by_key(|e| (e.pm_ms.or(e.book_ms).unwrap_or(0), e.match_id));
    Ok(out)
}

pub fn summarize(events: &[LagEvent]) -> Vec<Summary> {
    let mut groups: BTreeMap<(&'static str, String), Vec<&LagEvent>> = BTreeMap::new();
    for e in events {
        let book = if e.book.starts_with("first book") { "first book".to_string() } else { e.book.clone() };
        groups.entry((e.trigger, book)).or_default().push(e);
    }
    groups
        .into_iter()
        .map(|((trigger, book), evs)| {
            let mut d: Vec<i64> = evs.iter().filter_map(|e| e.delay_ms).collect();
            d.sort();
            let pct = |q: f64| -> Option<i64> {
                if d.is_empty() {
                    None
                } else {
                    Some(d[((d.len() - 1) as f64 * q).round() as usize])
                }
            };
            Summary {
                trigger,
                book,
                jumps: evs.len(),
                followed: d.len(),
                median_delay_ms: pct(0.5),
                p25_delay_ms: pct(0.25),
                p75_delay_ms: pct(0.75),
                mean_delay_ms: if d.is_empty() { None } else { Some(d.iter().sum::<i64>() / d.len() as i64) },
                polymarket_first_share: if d.is_empty() {
                    None
                } else {
                    Some(d.iter().filter(|x| **x < 0).count() as f64 / d.len() as f64)
                },
            }
        })
        .collect()
}

// ---------------------------------------------------------------- book <-> book

/// One (trigger book jump, follower book) pair.
#[derive(Clone, Debug, Serialize)]
pub struct BookLagEvent {
    pub match_id: i64,
    pub home: String,
    pub away: String,
    pub outcome: String,
    pub trigger_book: String,
    pub follower_book: String,
    /// When the trigger book's move was half done.
    pub trigger_ms: Ms,
    /// When the follower's move was half done. Null when it did not follow.
    pub follower_ms: Option<Ms>,
    /// Follower time minus trigger time; positive means the follower was later.
    pub delay_ms: Option<i64>,
    pub followed: bool,
    pub trigger_from: f64,
    pub trigger_to: f64,
    pub follower_from: Option<f64>,
    pub follower_to: Option<f64>,
}

/// Delay stats for one (trigger, follower) book pair.
#[derive(Clone, Debug, Serialize)]
pub struct BookPairSummary {
    pub trigger_book: String,
    pub follower_book: String,
    /// Trigger jumps that had a follower to compare against.
    pub jumps: usize,
    /// Of those, how many the follower actually followed.
    pub followed: usize,
    pub median_delay_ms: Option<i64>,
    pub p25_delay_ms: Option<i64>,
    pub p75_delay_ms: Option<i64>,
    pub mean_delay_ms: Option<i64>,
    /// Share of followed jumps where the follower crossed first (delay < 0).
    pub follower_first_share: Option<f64>,
}

/// For every bookmaker jump, how long until each other bookmaker followed.
/// Polymarket is excluded (covered by `compute`).
pub fn compute_books(db: &Path, p: &Params) -> Result<Vec<BookLagEvent>> {
    let c = crate::store::open_ro(db)?;
    let lo = p.since - p.window_ms - p.settle_ms;
    let hi = p.until + p.window_ms + p.settle_ms;

    // Matches with 1X2 changes from at least two bookmakers in range.
    let mut ids: Vec<i64> = {
        let mut st = c.prepare(
            "SELECT match_id FROM changes
             WHERE market = '1x2' AND book != ?1 AND ts_ms BETWEEN ?2 AND ?3
             GROUP BY match_id HAVING COUNT(DISTINCT book) >= 2",
        )?;
        let rows = st.query_map(params![PM, lo, hi], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };
    if let Some(m) = p.match_id {
        ids.retain(|x| *x == m);
    }

    let mut out = Vec::new();
    for mid in ids {
        let (home, away): (String, String) =
            c.query_row("SELECT home, away FROM matches WHERE id = ?1", params![mid], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;

        // (book, outcome) -> series of implied probability
        let (series, breaks) = load_series(&c, mid, lo, hi, p.source_clock, |b| b != PM && p.book.as_ref().map_or(true, |x| x == b))?;
        let none: Vec<Ms> = Vec::new();

        for outcome in ["home", "draw", "away"] {
            let books: Vec<(&String, &Series)> = series
                .iter()
                .filter(|((_, o), _)| o == outcome)
                .map(|((b, _), s)| (b, s))
                .collect();
            for (tb_book, tb_s) in &books {
                let tb_breaks = breaks.get(&((*tb_book).clone(), outcome.to_string())).unwrap_or(&none);
                for (tt, from, to) in jumps(tb_s, tb_breaks, p.min_jump, p.settle_ms) {
                    if tt < p.since || tt >= p.until {
                        continue;
                    }
                    for (fb_book, fb_s) in &books {
                        if fb_book == tb_book {
                            continue;
                        }
                        // Follower must already have been observed before the jump,
                        // otherwise "did not follow" just means "not recorded yet".
                        if fb_s.first().map_or(true, |f| f.0 > tt - p.settle_ms) {
                            continue;
                        }
                        let (tf, ff, ft, ok) = follow(fb_s, tt, from, to, p.window_ms);
                        out.push(BookLagEvent {
                            match_id: mid,
                            home: home.clone(),
                            away: away.clone(),
                            outcome: outcome.into(),
                            trigger_book: (*tb_book).clone(),
                            follower_book: (*fb_book).clone(),
                            trigger_ms: tt,
                            follower_ms: tf,
                            delay_ms: tf.map(|x| x - tt),
                            followed: ok,
                            trigger_from: from,
                            trigger_to: to,
                            follower_from: ff,
                            follower_to: ft,
                        });
                    }
                }
            }
        }
    }
    out.sort_by_key(|e| (e.trigger_ms, e.match_id));
    Ok(out)
}

/// Group book-to-book events by (trigger, follower) pair.
pub fn summarize_books(events: &[BookLagEvent]) -> Vec<BookPairSummary> {
    let mut groups: BTreeMap<(String, String), Vec<&BookLagEvent>> = BTreeMap::new();
    for e in events {
        groups
            .entry((e.trigger_book.clone(), e.follower_book.clone()))
            .or_default()
            .push(e);
    }
    groups
        .into_iter()
        .map(|((t, f), evs)| {
            let mut d: Vec<i64> = evs.iter().filter_map(|e| e.delay_ms).collect();
            d.sort();
            let pct = |q: f64| -> Option<i64> {
                if d.is_empty() {
                    None
                } else {
                    Some(d[((d.len() - 1) as f64 * q).round() as usize])
                }
            };
            BookPairSummary {
                trigger_book: t,
                follower_book: f,
                jumps: evs.len(),
                followed: d.len(),
                median_delay_ms: pct(0.5),
                p25_delay_ms: pct(0.25),
                p75_delay_ms: pct(0.75),
                mean_delay_ms: if d.is_empty() { None } else { Some(d.iter().sum::<i64>() / d.len() as i64) },
                follower_first_share: if d.is_empty() {
                    None
                } else {
                    Some(d.iter().filter(|x| **x < 0).count() as f64 / d.len() as f64)
                },
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn halfway_crossing() {
        // Book jumps 0.40 -> 0.60 at t=10_000; Polymarket drifts there 3 s later.
        let book: Series = vec![(0, 0.40), (10_000, 0.60), (30_000, 0.60)];
        let pm: Series = vec![(0, 0.42), (12_000, 0.47), (13_000, 0.53), (14_000, 0.61), (40_000, 0.61)];
        let j = jumps(&book, &[], 0.04, 10_000);
        assert_eq!(j.len(), 1);
        let (tb, from, to) = j[0];
        assert_eq!(tb, 10_000);
        let (tp, _, _, ok) = follow(&pm, tb, from, to, 60_000);
        assert!(ok);
        assert_eq!(tp, Some(13_000)); // 0.42 -> 0.61, halfway 0.515
    }

    #[test]
    fn a_reopen_after_a_gap_is_not_a_jump() {
        // Seen at 0.40 until t=10 s, collector down, re-opened at 0.80 at t=600 s; a real move
        // to 0.60 at t=700 s still counts.
        let s: Series = vec![(0, 0.40), (10_000, 0.40), (600_000, 0.80), (700_000, 0.60)];
        let j = jumps(&s, &[600_000], 0.04, 10_000);
        assert_eq!(j.len(), 1);
        assert_eq!(j[0].1, 0.80);
        assert_eq!(jumps(&s, &[], 0.04, 10_000).len(), 2);
    }

    #[test]
    fn opposite_move_is_not_a_follow() {
        let pm: Series = vec![(0, 0.50), (5_000, 0.40)];
        let (_, _, _, ok) = follow(&pm, 1_000, 0.40, 0.60, 30_000);
        assert!(!ok);
    }
}
