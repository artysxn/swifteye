//! Rankings: who moves first, across every source at once, one event at a time.
//!
//! An *event* starts at any source's jump (see lag.rs). Every other watched source
//! either makes the same move within the window (timed at its halfway crossing) or
//! misses it. Within an event sources are ordered by crossing time. From that:
//!   * per source: how often first, average rank, median time behind the first mover,
//!     and lead over Polymarket on shared events;
//!   * opportunity: when a bookmaker moved and Polymarket was watched, how often
//!     Polymarket had not yet reacted, and how much of its move was still to come.

use crate::lag::{follow, jumps, value_at, Params, Series, PM};
use crate::model::Ms;
use anyhow::Result;
use rusqlite::params;
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Clone, Debug, Serialize)]
pub struct RankEvent {
    pub match_id: i64,
    pub home: String,
    pub away: String,
    pub outcome: String,
    /// +1 = the selection became more likely, -1 = less likely.
    pub direction: i8,
    /// (source, halfway-crossing time), earliest first.
    pub order: Vec<(String, Ms)>,
    /// Watched sources that did not make the move inside the window.
    pub missed: Vec<String>,
    pub first_book: Option<String>,
    pub first_book_ms: Option<Ms>,
    pub pm_ms: Option<Ms>,
    /// Polymarket probability at the moment the first bookmaker crossed, and where it ended.
    pub pm_at_first_book: Option<f64>,
    pub pm_to: Option<f64>,
    /// pm_ms - first_book_ms (positive = Polymarket later).
    pub pm_lead_ms: Option<i64>,
    /// Probability points Polymarket still had to move when the first bookmaker crossed.
    pub capturable: Option<f64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SourceRank {
    pub source: String,
    pub watched: usize,
    pub moved: usize,
    pub first: usize,
    pub first_share: Option<f64>,
    pub avg_rank: Option<f64>,
    pub median_behind_first_ms: Option<i64>,
    pub p75_behind_first_ms: Option<i64>,
    /// Median of (Polymarket time - this source's time) on events both made.
    pub median_vs_pm_ms: Option<i64>,
    pub ahead_of_pm_share: Option<f64>,
    pub vs_pm_events: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct Opportunity {
    /// Bookmaker moves while Polymarket was being watched.
    pub events: usize,
    pub pm_followed: usize,
    pub pm_did_not_follow: usize,
    /// (ms, events where Polymarket crossed at least this long after the first bookmaker, share of `events`)
    pub lead_at_least: Vec<(i64, usize, f64)>,
    pub median_lead_ms: Option<i64>,
    pub median_capturable_pts: Option<f64>,
    /// (points, events with at least this much Polymarket move still to come, share of `events`)
    pub capturable_at_least: Vec<(f64, usize, f64)>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Rankings {
    pub sources: Vec<SourceRank>,
    pub opportunity: Opportunity,
    pub events: Vec<RankEvent>,
    pub matches: usize,
}

fn quant(v: &[i64], q: f64) -> Option<i64> {
    if v.is_empty() {
        return None;
    }
    let mut s = v.to_vec();
    s.sort();
    Some(s[((s.len() - 1) as f64 * q).round() as usize])
}

pub fn compute(db: &Path, p: &Params) -> Result<Rankings> {
    let c = crate::store::open_ro(db)?;
    let lo = p.since - p.window_ms - p.settle_ms;
    let hi = p.until + p.window_ms + p.settle_ms;
    let ids: Vec<i64> = {
        let mut st = c.prepare(
            "SELECT match_id FROM changes WHERE market = '1x2' AND ts_ms BETWEEN ?1 AND ?2
             GROUP BY match_id HAVING COUNT(DISTINCT book) >= 2",
        )?;
        let rows = st.query_map(params![lo, hi], |r| r.get(0))?;
        rows.collect::<Result<_, _>>()?
    };

    let mut events: Vec<RankEvent> = Vec::new();
    for mid in &ids {
        if p.match_id.is_some_and(|m| m != *mid) {
            continue;
        }
        let (home, away): (String, String) = c
            .query_row("SELECT home, away FROM matches WHERE id = ?1", params![mid], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap_or_default();
        let mut series: BTreeMap<(String, String), Series> = BTreeMap::new();
        let mut st = c.prepare_cached(
            "SELECT book, outcome, ts_ms, book_ts_ms, new_price FROM changes
             WHERE match_id = ?1 AND market = '1x2' AND ts_ms BETWEEN ?2 AND ?3 ORDER BY ts_ms, id",
        )?;
        let rows = st.query_map(params![mid, lo, hi], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?, r.get::<_, Option<i64>>(3)?, r.get::<_, Option<f64>>(4)?))
        })?;
        for row in rows {
            let (book, outcome, ts, bts, price) = row?;
            let Some(price) = price.filter(|x| *x > 1.0) else { continue };
            let t = if p.source_clock { bts.unwrap_or(ts) } else { ts };
            series.entry((book, outcome)).or_default().push((t, 1.0 / price));
        }
        for s in series.values_mut() {
            s.sort_by_key(|x| x.0);
        }

        for outcome in ["home", "draw", "away"] {
            let srcs: Vec<(&String, &Series)> = series.iter().filter(|((_, o), _)| o == outcome).map(|((b, _), s)| (b, s)).collect();
            if srcs.len() < 2 {
                continue;
            }
            let pm_series = series.get(&(PM.to_string(), outcome.to_string()));

            let mut anchors: Vec<(Ms, f64, f64, &String)> = Vec::new();
            for (b, s) in &srcs {
                for (t, from, to) in jumps(s, p.min_jump, p.settle_ms) {
                    anchors.push((t, from, to, *b));
                }
            }
            anchors.sort_by_key(|a| a.0);

            // Spans already explained by an earlier event, per direction.
            let mut covered: Vec<(Ms, Ms, i8)> = Vec::new();
            for (t, from, to, anchor_src) in anchors {
                if t < p.since || t >= p.until {
                    continue;
                }
                let dir: i8 = if to > from { 1 } else { -1 };
                if covered.iter().any(|&(a, b, d)| d == dir && t >= a - p.settle_ms && t <= b + p.settle_ms) {
                    continue;
                }
                let mut order: Vec<(String, Ms)> = Vec::new();
                let mut missed: Vec<String> = Vec::new();
                let mut pm_to: Option<f64> = None;
                for (b, s) in &srcs {
                    // Only sources already being recorded can be said to have missed it.
                    if s.first().map_or(true, |f| f.0 > t - p.settle_ms) {
                        continue;
                    }
                    let (tc, _ff, ft, ok) = if *b == anchor_src { (Some(t), Some(from), Some(to), true) } else { follow(s, t, from, to, p.window_ms) };
                    match (ok, tc) {
                        (true, Some(tc)) => {
                            if b.as_str() == PM {
                                pm_to = ft;
                            }
                            order.push(((*b).clone(), tc));
                        }
                        _ => missed.push((*b).clone()),
                    }
                }
                if order.is_empty() {
                    continue;
                }
                order.sort_by_key(|x| x.1);
                let start = order[0].1.min(t);
                let end = order.iter().map(|x| x.1).max().unwrap_or(t).max(t);
                covered.push((start, end, dir));

                let first_book = order.iter().find(|x| x.0 != PM).cloned();
                let pm_ms = order.iter().find(|x| x.0 == PM).map(|x| x.1);
                let pm_watched = pm_ms.is_some() || missed.iter().any(|m| m == PM);
                if order.len() < 2 && !(pm_watched && first_book.is_some()) {
                    continue; // a lone move with nobody else watched to compare against
                }
                let pm_at = match (&first_book, pm_series) {
                    (Some((_, fb)), Some(s)) if pm_watched => value_at(s, *fb),
                    _ => None,
                };
                let capturable = match (pm_at, pm_to) {
                    (Some(a), Some(b)) => Some(((b - a) * dir as f64).max(0.0)),
                    _ => None,
                };
                events.push(RankEvent {
                    match_id: *mid,
                    home: home.clone(),
                    away: away.clone(),
                    outcome: outcome.into(),
                    direction: dir,
                    missed,
                    first_book: first_book.as_ref().map(|x| x.0.clone()),
                    first_book_ms: first_book.as_ref().map(|x| x.1),
                    pm_ms,
                    pm_at_first_book: pm_at,
                    pm_to,
                    pm_lead_ms: match (pm_ms, &first_book) {
                        (Some(pm), Some((_, fb))) => Some(pm - fb),
                        _ => None,
                    },
                    capturable,
                    order,
                });
            }
        }
    }
    events.sort_by_key(|e| e.order.first().map(|x| x.1).unwrap_or(0));

    // ---- per source
    let mut names: Vec<String> = events.iter().flat_map(|e| e.order.iter().map(|x| x.0.clone()).chain(e.missed.iter().cloned())).collect();
    names.sort();
    names.dedup();
    let mut sources = Vec::new();
    for n in &names {
        let (mut watched, mut moved, mut first) = (0usize, 0usize, 0usize);
        let mut ranks: Vec<f64> = Vec::new();
        let mut behind: Vec<i64> = Vec::new();
        let mut vs_pm: Vec<i64> = Vec::new();
        for e in &events {
            let pos = e.order.iter().position(|x| &x.0 == n);
            if pos.is_none() && !e.missed.contains(n) {
                continue;
            }
            if e.order.len() < 2 && pos.is_some() {
                continue; // nothing to rank against
            }
            watched += 1;
            let Some(i) = pos else { continue };
            moved += 1;
            ranks.push((i + 1) as f64);
            let (t, t0) = (e.order[i].1, e.order[0].1);
            if t == t0 {
                first += 1;
            }
            behind.push(t - t0);
            if n != PM {
                if let Some(pm) = e.pm_ms {
                    vs_pm.push(pm - t);
                }
            }
        }
        sources.push(SourceRank {
            source: n.clone(),
            watched,
            moved,
            first,
            first_share: (moved > 0).then(|| first as f64 / moved as f64),
            avg_rank: (!ranks.is_empty()).then(|| ranks.iter().sum::<f64>() / ranks.len() as f64),
            median_behind_first_ms: quant(&behind, 0.5),
            p75_behind_first_ms: quant(&behind, 0.75),
            median_vs_pm_ms: quant(&vs_pm, 0.5),
            ahead_of_pm_share: (!vs_pm.is_empty()).then(|| vs_pm.iter().filter(|x| **x > 0).count() as f64 / vs_pm.len() as f64),
            vs_pm_events: vs_pm.len(),
        });
    }
    sources.sort_by(|a, b| {
        a.median_behind_first_ms.unwrap_or(i64::MAX).cmp(&b.median_behind_first_ms.unwrap_or(i64::MAX)).then(b.moved.cmp(&a.moved))
    });

    // ---- opportunity: bookmaker moves while Polymarket was watched
    let opp: Vec<&RankEvent> = events.iter().filter(|e| e.first_book.is_some() && (e.pm_ms.is_some() || e.missed.iter().any(|m| m == PM))).collect();
    let total = opp.len();
    let share = |n: usize| if total > 0 { n as f64 / total as f64 } else { 0.0 };
    let leads: Vec<i64> = opp.iter().filter_map(|e| e.pm_lead_ms).collect();
    let followed = opp.iter().filter(|e| e.pm_ms.is_some()).count();
    let mut caps: Vec<f64> = opp.iter().filter_map(|e| e.capturable).collect();
    caps.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let opportunity = Opportunity {
        events: total,
        pm_followed: followed,
        pm_did_not_follow: total - followed,
        lead_at_least: [0i64, 1000, 2000, 5000, 10000, 30000]
            .iter()
            .map(|&th| {
                let n = leads.iter().filter(|l| if th == 0 { **l > 0 } else { **l >= th }).count();
                (th, n, share(n))
            })
            .collect(),
        median_lead_ms: quant(&leads, 0.5),
        median_capturable_pts: if caps.is_empty() { None } else { Some(caps[caps.len() / 2]) },
        capturable_at_least: [0.01f64, 0.02, 0.03, 0.05]
            .iter()
            .map(|&th| {
                let n = caps.iter().filter(|c| **c >= th).count();
                (th, n, share(n))
            })
            .collect(),
    };
    let mut ms: Vec<i64> = events.iter().map(|e| e.match_id).collect();
    ms.sort();
    ms.dedup();
    Ok(Rankings { sources, opportunity, events, matches: ms.len() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quantile_picks_middle() {
        assert_eq!(quant(&[5, 1, 3], 0.5), Some(3));
        assert_eq!(quant(&[], 0.5), None);
    }
}
