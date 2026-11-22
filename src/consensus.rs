//! The bookmakers' consensus for one outcome (each price feed counted once) and how Polymarket
//! relates to it in this game: does it follow the bookmakers late (the snipe edge), move with
//! them, or lead them?
//!
//! A snipe is only worth taking when
//!  - the consensus itself moved (not one feed wobbling back from a dip),
//!  - the gap between the consensus and Polymarket widened beyond its usual size in this game
//!    (Polymarket often sits at a steady offset from the bookmakers; that offset is no edge),
//!  - and earlier in this game Polymarket followed the bookmakers' jumps late instead of moving
//!    with them or before them.

use crate::model::Ms;
use rusqlite::{params, Connection};
use std::collections::BTreeMap;

/// A price older than this does not count toward the consensus.
const MAX_AGE_MS: Ms = 20 * 60_000;
/// Sampling step for the history measures.
const STEP_MS: Ms = 5_000;
/// A consensus jump earlier in the game: this much within 10 s.
const JUMP: f64 = 0.03;
/// Sentinel for "not measured" (kept finite so the replay cache stays valid JSON).
pub const UNKNOWN: f64 = -9.0;
/// The earlier-jumps history ('followed' / 'lead') reads Polymarket by its OWN clock, as if every price
/// reached us this much late (22 Nov 2026, owner: "Update the bot to have the 5s slack"). Read by arrival
/// time, 'followed' swung with our feed's delay: the late feed of 17-18 Nov let more moves through and
/// they paid; on time (from 21 Nov) it let few through. On Polymarket's clock + 5 s, the replay of 17-21
/// Nov: 41 trades, 76% won; today's rules with an on-time feed: 31, 61% (tools/lag_shift.py, wallet_best.py).
pub const FOLLOW_SLACK_MS: Ms = 5_000;

/// Every bookmaker's and Polymarket's price for one (match, outcome) over a time span.
pub struct Series {
    /// Per book: (family, [(ts, implied probability)] sorted by time).
    books: Vec<(String, Vec<(Ms, f64)>)>,
    pm: Vec<(Ms, f64)>,
    /// Polymarket's middle by its own clock (pm_quotes; empty for Kalshi or a market with no quotes):
    /// the earlier-jumps history reads this, shifted by FOLLOW_SLACK_MS.
    pm_own: Vec<(Ms, f64)>,
}

/// What the move looks like against the whole market and this game's history.
#[derive(Clone, Copy, Debug, Default, serde::Serialize, serde::Deserialize)]
pub struct Ctx {
    /// Feed families priced now, and how many of them moved 2+ points our way.
    pub fams: usize,
    pub fam_moved: usize,
    /// Consensus change over the move window, in our direction.
    pub cons_move: f64,
    /// The consensus at the decision (for the "reason gone" exit).
    pub cons_now: f64,
    /// (consensus - Polymarket) now minus its median over the 10 min before, in our direction:
    /// how much the gap opened beyond its usual size. The catch-up target.
    pub gap_shock: f64,
    /// Earlier consensus jumps in this game (last 30 min) and Polymarket's response: median
    /// share of each jump it made in the 60 s after (follow) and in the 40 s up to it (lead).
    pub jumps: usize,
    pub follow: f64,
    pub lead: f64,
    /// The move in units of the consensus's usual 10 s change over the 10 min before.
    pub z: f64,
}

fn at(v: &[(Ms, f64)], t: Ms) -> Option<(Ms, f64)> {
    let i = v.partition_point(|x| x.0 <= t);
    (i > 0).then(|| v[i - 1])
}

fn median(mut v: Vec<f64>) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    v.sort_by(|a, b| a.total_cmp(b));
    Some(v[v.len() / 2])
}

impl Series {
    /// Load prices for [from, to], plus each book's last price before `from`.
    pub fn load(c: &Connection, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Series {
        Self::load_for(c, "polymarket", match_id, outcome, from, to)
    }

    /// The same for either prediction market as the venue traded (`base`). For Kalshi, Polymarket
    /// counts as one more feed (it usually moves first).
    pub fn load_for(c: &Connection, base: &str, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Series {
        let mut books: BTreeMap<String, Vec<(Ms, f64)>> = BTreeMap::new();
        let mut pm: Vec<(Ms, f64)> = Vec::new();
        let mut push = |b: String, ts: Ms, p: f64| {
            if b == base {
                pm.push((ts, p));
            } else if b != "kalshi" {
                books.entry(b).or_default().push((ts, p));
            }
        };
        if let Ok(mut st) = c.prepare_cached(
            "SELECT book, MAX(ts_ms), new_price FROM changes WHERE match_id = ?1 AND market = '1x2' AND outcome = ?2 AND ts_ms < ?3 AND ts_ms >= ?4 AND new_price IS NOT NULL GROUP BY book",
        ) {
            if let Ok(it) = st.query_map(params![match_id, outcome, from, from - MAX_AGE_MS], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Ms>(1)?, r.get::<_, f64>(2)?))) {
                for (b, ts, p) in it.flatten() {
                    push(b, ts, 1.0 / p.max(1.0001));
                }
            }
        }
        if let Ok(mut st) = c.prepare_cached("SELECT book, ts_ms, new_price FROM changes WHERE match_id = ?1 AND market = '1x2' AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4 AND new_price IS NOT NULL ORDER BY ts_ms") {
            if let Ok(it) = st.query_map(params![match_id, outcome, from, to], |r| Ok((r.get::<_, String>(0)?, r.get::<_, Ms>(1)?, r.get::<_, f64>(2)?))) {
                for (b, ts, p) in it.flatten() {
                    push(b, ts, 1.0 / p.max(1.0001));
                }
            }
        }
        pm.sort_by_key(|x| x.0);
        // Polymarket by its own clock, for the earlier-jumps history (Polymarket only).
        let mut pm_own: Vec<(Ms, f64)> = Vec::new();
        if base == "polymarket" {
            if let Ok(mut st) = c.prepare_cached(
                "SELECT CASE WHEN server_ts_ms > 0 THEN server_ts_ms ELSE ts_ms END, (bid + ask) / 2.0 FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND bid > 0 AND ask > 0 AND ts_ms >= ?3 AND ts_ms <= ?4",
            ) {
                if let Ok(it) = st.query_map(params![match_id, outcome, from - MAX_AGE_MS, to], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, f64>(1)?))) {
                    pm_own.extend(it.flatten());
                }
            }
            pm_own.sort_by_key(|x| x.0);
        }
        Series {
            books: books
                .into_iter()
                .map(|(b, mut v)| {
                    v.sort_by_key(|x| x.0);
                    (crate::snipe::platform(&b).to_string(), v)
                })
                .collect(),
            pm,
            pm_own,
        }
    }

    /// Each family's price at `t` (mean of its books with a fresh price).
    fn families(&self, t: Ms) -> BTreeMap<&str, f64> {
        let mut acc: BTreeMap<&str, (f64, usize)> = BTreeMap::new();
        for (fam, v) in &self.books {
            if let Some((ts, p)) = at(v, t) {
                if t - ts <= MAX_AGE_MS {
                    let e = acc.entry(fam.as_str()).or_default();
                    e.0 += p;
                    e.1 += 1;
                }
            }
        }
        acc.into_iter().map(|(k, (s, n))| (k, s / n as f64)).collect()
    }

    /// The consensus at `t`: median of the families' prices.
    pub fn cons_at(&self, t: Ms) -> Option<f64> {
        median(self.families(t).into_values().collect())
    }

    pub fn pm_at(&self, t: Ms) -> Option<f64> {
        at(&self.pm, t).filter(|x| t - x.0 <= MAX_AGE_MS).map(|x| x.1)
    }

    /// Polymarket for the earlier-jumps history: its own clock, seen FOLLOW_SLACK_MS late (arrival time
    /// when there is no own-clock series, e.g. Kalshi).
    fn pm_hist(&self, t: Ms) -> Option<f64> {
        if self.pm_own.is_empty() {
            return self.pm_at(t);
        }
        let ts = t - FOLLOW_SLACK_MS;
        at(&self.pm_own, ts).filter(|x| ts - x.0 <= MAX_AGE_MS).map(|x| x.1)
    }

    /// The move from `pre` to `now` measured against the whole market and this game's history.
    pub fn ctx(&self, dir: f64, pre: Ms, now: Ms) -> Option<Ctx> {
        let (c_pre, c_now) = (self.cons_at(pre)?, self.cons_at(now)?);
        let (f_pre, f_now) = (self.families(pre), self.families(now));
        let fam_moved = f_now.iter().filter(|(k, p)| f_pre.get(*k).is_some_and(|a| (*p - a) * dir >= 0.02)).count();
        // Usual gap and usual 10 s change over the 10 min before.
        let (mut gaps, mut d2) = (Vec::new(), Vec::new());
        let mut t = pre - 600_000;
        while t <= pre {
            if let (Some(cn), Some(p)) = (self.cons_at(t), self.pm_at(t)) {
                gaps.push(cn - p);
            }
            if let (Some(a), Some(b)) = (self.cons_at(t - 10_000), self.cons_at(t)) {
                d2.push((b - a) * (b - a));
            }
            t += STEP_MS;
        }
        let basis = median(gaps).or_else(|| Some(c_pre - self.pm_at(pre)?));
        let gap_now = self.pm_at(now).map(|p| c_now - p);
        let gap_shock = basis.zip(gap_now).map(|(b, g)| (g - b) * dir).unwrap_or(UNKNOWN);
        let sd = if d2.is_empty() { 0.0 } else { (d2.iter().sum::<f64>() / d2.len() as f64).sqrt() };
        let cons_move = (c_now - c_pre) * dir;
        // Earlier jumps and Polymarket's response (all of it known before the move).
        let (mut fol, mut led) = (Vec::new(), Vec::new());
        let mut t = now - 1_800_000;
        let mut last: Ms = t - 60_000;
        while t + 60_000 <= pre {
            if t - last >= 60_000 {
                if let (Some(a), Some(b)) = (self.cons_at(t - 10_000), self.cons_at(t)) {
                    let j = b - a;
                    if j.abs() >= JUMP {
                        if let (Some(p40), Some(p0), Some(p60)) = (self.pm_hist(t - 40_000), self.pm_hist(t), self.pm_hist(t + 60_000)) {
                            led.push((p0 - p40) / j);
                            fol.push((p60 - p0) / j);
                            last = t;
                        }
                    }
                }
            }
            t += STEP_MS;
        }
        Some(Ctx {
            fams: f_now.len(),
            fam_moved,
            cons_move,
            cons_now: c_now,
            gap_shock,
            jumps: fol.len(),
            follow: median(fol).unwrap_or(UNKNOWN),
            lead: median(led).unwrap_or(UNKNOWN),
            z: cons_move / sd.max(0.003),
        })
    }
}
