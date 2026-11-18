//! How long real orders take, from the diagnostic test runs (Bots page → Test runs), so the
//! simulations, the move table behind the Adaptive bot and the paper bots fill where real orders
//! would. Medians of the tests' legs; until a quantity has been measured, a cautious default.
//!
//! Measured 18 Nov 2026 (3 Polymarket round trips, live tennis): order reply 65-114 ms, live-sports
//! matching delay 1.28-1.41 s, shares usable ~1.8 s after a buy, slippage 0c. Kalshi: not
//! measured yet.

use crate::model::*;
use parking_lot::Mutex;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::LazyLock;

#[derive(Clone, Copy, Debug, Serialize, PartialEq)]
pub struct Timing {
    /// Polymarket: order sent -> venue reply.
    pub pm_order_ms: Ms,
    /// Polymarket holds orders on games already playing this long before matching them.
    pub pm_live_delay_ms: Ms,
    /// Polymarket: bought shares can be sold this long after the buy matched.
    pub pm_settle_ms: Ms,
    /// Kalshi: order sent -> filled or killed.
    pub kalshi_order_ms: Ms,
    /// How many tests each figure comes from (0 = default).
    pub n_pm: usize,
    pub n_kalshi: usize,
}

impl Timing {
    /// Polymarket: from sending an order to its fill (game playing or not).
    pub fn pm_fill_ms(&self, live: bool) -> Ms {
        self.pm_order_ms + if live { self.pm_live_delay_ms } else { 0 }
    }
}

/// Before any test: the first measurements, and a cautious Kalshi guess.
pub const DEFAULT: Timing = Timing { pm_order_ms: 80, pm_live_delay_ms: 1_400, pm_settle_ms: 1_800, kalshi_order_ms: 300, n_pm: 0, n_kalshi: 0 };

struct Cache {
    db: Option<PathBuf>,
    at: Ms,
    t: Timing,
}

static C: LazyLock<Mutex<Cache>> = LazyLock::new(|| Mutex::new(Cache { db: None, at: 0, t: DEFAULT }));

pub fn init(db: &std::path::Path) {
    C.lock().db = Some(db.to_path_buf());
}

fn median(mut v: Vec<f64>) -> Option<f64> {
    if v.is_empty() {
        return None;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    Some(v[v.len() / 2])
}

/// The current figures (re-read from the tests every 10 minutes).
pub fn get() -> Timing {
    let now = now_ms();
    let db = {
        let c = C.lock();
        if now - c.at < 600_000 {
            return c.t;
        }
        c.db.clone()
    };
    let mut t = DEFAULT;
    if let Some(db) = db {
        if let Ok(c) = crate::store::open_ro(&db) {
            let legs: Vec<serde_json::Value> = c
                .prepare("SELECT data FROM bot_tests")
                .and_then(|mut st| st.query_map([], |r| r.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).collect::<Vec<_>>()))
                .unwrap_or_default()
                .iter()
                .filter_map(|s| serde_json::from_str::<serde_json::Value>(s).ok())
                .flat_map(|v| v["legs"].as_array().cloned().unwrap_or_default())
                .collect();
            let pick = |venue: &str, key: &str, pos: bool| -> Vec<f64> {
                legs.iter().filter(|l| l["venue"] == venue).filter_map(|l| l[key].as_f64()).filter(|x| !pos || *x > 0.0).collect()
            };
            let pm_lat = pick("polymarket", "latency_ms", false);
            t.n_pm = pm_lat.len();
            if let Some(x) = median(pm_lat) {
                t.pm_order_ms = x as Ms;
            }
            if let Some(x) = median(pick("polymarket", "match_delay_ms", true)) {
                t.pm_live_delay_ms = x as Ms;
            }
            if let Some(x) = median(pick("polymarket", "settle_wait_ms", true)) {
                t.pm_settle_ms = x as Ms;
            }
            let k = pick("kalshi", "latency_ms", false);
            t.n_kalshi = k.len();
            if let Some(x) = median(k) {
                t.kalshi_order_ms = x as Ms;
            }
        }
    }
    let mut c = C.lock();
    c.t = t;
    c.at = now;
    t
}

/// Is the game playing at `t`? (kick-off known and passed, within 6 hours)
pub fn live(c: &rusqlite::Connection, match_id: i64, t: Ms) -> bool {
    c.prepare_cached("SELECT COALESCE(start_ms, 0) FROM matches WHERE id = ?1")
        .and_then(|mut st| st.query_row([match_id], |r| r.get::<_, Ms>(0)))
        .map(|s| s > 0 && s <= t && t - s < 6 * 3_600_000)
        .unwrap_or(false)
}
