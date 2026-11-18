//! Live tracking of pre-match arbitrage. A scan every 5 s records when each opportunity
//! (match + market + line, whichever books make it) appears and disappears, in `arb_history`.
//! From the finished ones: how long an arbitrage usually lasts, and for an open one, roughly
//! how long it has left (median lifetime of past ones that lived at least as long, minus its age).
//!
//! Durations are only as fine as the scan (±5 s). Not counted in the averages: ones that ended
//! because the match kicked off (they did not close on their own), ones cut by a restart, and
//! ones already open in the first scan after a start (their real start is unknown).

use crate::arb::Arb;
use crate::model::*;
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const SCAN_MS: u64 = 5_000;
/// Near misses down to this are kept for the page; only profit > 0 is tracked.
const KEEP_MIN: f64 = -0.03;
/// Durations of finished ones from this far back feed the averages.
const STATS_SPAN_MS: Ms = 14 * 86_400_000;

pub fn opp_key(a: &Arb) -> String {
    format!("{}|{}|{}", a.match_id, a.market, a.line.map(|l| l.to_string()).unwrap_or_default())
}

struct Open {
    id: i64,
    first_ms: Ms,
    last_ms: Ms,
    peak: f64,
    start_ms: Option<Ms>,
    censored: bool,
}

#[derive(Default, Clone, Serialize)]
pub struct Stats {
    /// Finished arbitrages that count (closed on their own, start seen).
    pub n: usize,
    pub median_ms: Option<i64>,
    pub mean_ms: Option<i64>,
    pub p25_ms: Option<i64>,
    pub p75_ms: Option<i64>,
    pub p90_ms: Option<i64>,
    /// Ended because the match started (not in the averages).
    pub kickoff: usize,
    /// (market, count, median ms)
    pub by_market: Vec<(String, usize, i64)>,
    /// (bucket label, count, median ms) by peak return.
    pub by_return: Vec<(String, usize, i64)>,
    #[serde(skip)]
    durs: Vec<(String, i64)>,
}

#[derive(Default)]
struct State {
    open: HashMap<String, Open>,
    current: Vec<Arb>,
    scanned_ms: Ms,
    scans: u64,
    stats: Stats,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);

fn conn(app: &App) -> rusqlite::Result<Connection> {
    let c = Connection::open(&app.db_path)?;
    c.busy_timeout(Duration::from_secs(10))?;
    Ok(c)
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        let a2 = app.clone();
        let _ = tokio::task::spawn_blocking(move || {
            if let Ok(c) = conn(&a2) {
                // Whatever was open when we stopped has an unknown end.
                let _ = c.execute("UPDATE arb_history SET end_ms = last_ms, end_reason = 'restart' WHERE end_ms IS NULL", []);
                refresh_stats(&c);
            }
        })
        .await;
        let mut tick = 0u64;
        loop {
            tokio::time::sleep(Duration::from_millis(SCAN_MS)).await;
            tick += 1;
            let a2 = app.clone();
            let _ = tokio::task::spawn_blocking(move || step(&a2, tick % 12 == 0)).await;
        }
    });
}

fn quant(v: &[i64], q: f64) -> Option<i64> {
    (!v.is_empty()).then(|| v[((v.len() - 1) as f64 * q).round() as usize])
}

fn refresh_stats(c: &Connection) {
    let since = now_ms() - STATS_SPAN_MS;
    let mut durs: Vec<(String, i64, f64)> = Vec::new();
    if let Ok(mut st) = c.prepare(
        "SELECT market, (last_ms + end_ms) / 2 - first_ms, peak_profit FROM arb_history
         WHERE end_reason = 'gone' AND censored = 0 AND first_ms > ?1",
    ) {
        if let Ok(rows) = st.query_map([since], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, Option<f64>>(2)?.unwrap_or(0.0)))) {
            durs = rows.filter_map(|x| x.ok()).collect();
        }
    }
    let kickoff: usize = c
        .query_row("SELECT COUNT(*) FROM arb_history WHERE end_reason = 'kickoff' AND first_ms > ?1", [since], |r| r.get::<_, i64>(0))
        .unwrap_or(0) as usize;
    let mut all: Vec<i64> = durs.iter().map(|d| d.1).collect();
    all.sort();
    let group = |key: &dyn Fn(&(String, i64, f64)) -> String| -> Vec<(String, usize, i64)> {
        let mut g: HashMap<String, Vec<i64>> = HashMap::new();
        for d in &durs {
            g.entry(key(d)).or_default().push(d.1);
        }
        let mut out: Vec<(String, usize, i64)> = g
            .into_iter()
            .map(|(k, mut v)| {
                v.sort();
                let m = quant(&v, 0.5).unwrap_or(0);
                (k, v.len(), m)
            })
            .collect();
        out.sort_by(|a, b| a.0.cmp(&b.0));
        out
    };
    let by_market = group(&|d| d.0.clone());
    let by_return = group(&|d| (if d.2 < 0.005 { "0-0.5%" } else if d.2 < 0.01 { "0.5-1%" } else if d.2 < 0.02 { "1-2%" } else { "2%+" }).to_string());
    let stats = Stats {
        n: all.len(),
        median_ms: quant(&all, 0.5),
        mean_ms: (!all.is_empty()).then(|| all.iter().sum::<i64>() / all.len() as i64),
        p25_ms: quant(&all, 0.25),
        p75_ms: quant(&all, 0.75),
        p90_ms: quant(&all, 0.9),
        kickoff,
        by_market,
        by_return,
        durs: durs.into_iter().map(|d| (d.0, d.1)).collect(),
    };
    ST.lock().stats = stats;
}

fn step(app: &App, stats: bool) {
    let arbs = crate::arb::scan(app, KEEP_MIN, 100.0);
    let now = now_ms();
    let mut best: HashMap<String, &Arb> = HashMap::new();
    for a in arbs.iter().filter(|a| a.profit > 0.0) {
        let k = opp_key(a);
        if best.get(&k).is_none_or(|b| a.profit > b.profit) {
            best.insert(k, a);
        }
    }
    let Ok(c) = conn(app) else { return };
    {
        let mut st = ST.lock();
        let first_scan = st.scans == 0;
        let gone: Vec<String> = st.open.keys().filter(|k| !best.contains_key(*k)).cloned().collect();
        for k in gone {
            let Some(o) = st.open.remove(&k) else { continue };
            let reason = if o.start_ms.is_some_and(|s| s <= now) { "kickoff" } else { "gone" };
            let _ = c.execute(
                "UPDATE arb_history SET end_ms = ?1, end_reason = ?2, last_ms = ?3, peak_profit = ?4 WHERE id = ?5",
                params![now, reason, o.last_ms, o.peak, o.id],
            );
        }
        for (k, a) in &best {
            if let Some(o) = st.open.get_mut(k) {
                o.last_ms = now;
                o.peak = o.peak.max(a.profit);
                continue;
            }
            let books = a.legs.iter().map(|l| l.book.as_str()).collect::<Vec<_>>().join(",");
            let legs = serde_json::to_string(&a.legs).unwrap_or_default();
            let ok = c.execute(
                "INSERT INTO arb_history(key, match_id, sport, home, away, league, market, line, books, legs, first_ms, last_ms, open_profit, peak_profit, censored)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?11, ?12, ?12, ?13)",
                params![k, a.match_id, a.sport, a.home, a.away, a.league, a.market, a.line, books, legs, now, a.profit, first_scan as i64],
            );
            if ok.is_ok() {
                st.open.insert(k.clone(), Open { id: c.last_insert_rowid(), first_ms: now, last_ms: now, peak: a.profit, start_ms: a.start_ms, censored: first_scan });
            }
        }
        st.current = arbs.clone();
        st.scanned_ms = now;
        st.scans += 1;
    }
    if stats {
        refresh_stats(&c);
    }
}

/// (age ms, estimated ms left, how many past ones the estimate rests on, age is a lower bound)
pub fn timing(key: &str, market: &str) -> Option<(i64, Option<i64>, usize, bool)> {
    let st = ST.lock();
    let o = st.open.get(key)?;
    let age = st.scanned_ms.max(o.last_ms) - o.first_ms;
    let same: Vec<i64> = st.stats.durs.iter().filter(|d| d.0 == market).map(|d| d.1).collect();
    let pool: Vec<i64> = if same.len() >= 10 { same } else { st.stats.durs.iter().map(|d| d.1).collect() };
    let mut alive: Vec<i64> = pool.into_iter().filter(|d| *d > age).collect();
    alive.sort();
    let left = (alive.len() >= 5).then(|| quant(&alive, 0.5).unwrap_or(0) - age);
    Some((age, left, alive.len(), o.censored))
}

fn with_timing(a: &Arb, stake: f64) -> Value {
    let mut v = serde_json::to_value(a).unwrap_or(Value::Null);
    if stake != 100.0 {
        if let Some(legs) = v["legs"].as_array_mut() {
            for l in legs {
                if let Some(s) = l["stake"].as_f64() {
                    l["stake"] = json!(s * stake / 100.0);
                }
            }
        }
    }
    if a.profit > 0.0 {
        if let Some((age, left, n, partial)) = timing(&opp_key(a), a.market) {
            v["age_ms"] = json!(age);
            v["left_ms"] = json!(left);
            v["left_n"] = json!(n);
            v["age_partial"] = json!(partial);
        }
    }
    v
}

/// The latest scan (at most 5 s old), with age and time left on the open ones.
pub fn current(min_profit: f64, stake: f64) -> Value {
    let (arbs, at, stats) = {
        let st = ST.lock();
        (st.current.clone(), st.scanned_ms, st.stats.clone())
    };
    let rows: Vec<Value> = arbs.iter().filter(|a| a.profit >= min_profit).map(|a| with_timing(a, stake)).collect();
    json!({"now_ms": now_ms(), "scanned_ms": at, "scan_every_ms": SCAN_MS, "min_profit": min_profit, "stake": stake, "lifetime": stats, "arbs": rows})
}

/// Short "lasts ~2m, ~40s left" for a message, when known.
pub fn timing_text(a: &Arb) -> Option<String> {
    let (age, left, _, _) = timing(&opp_key(a), a.market)?;
    let f = |ms: i64| if ms >= 60_000 { format!("{}m {:02}s", ms / 60_000, (ms % 60_000) / 1000) } else { format!("{}s", ms / 1000) };
    let typical = ST.lock().stats.median_ms;
    Some(match (left, typical) {
        (Some(l), _) => format!("open {}, about {} left", f(age), f(l.max(0))),
        (None, Some(t)) if age > t => format!("open {}, already longer than most ({} typical)", f(age), f(t)),
        _ => format!("open {}", f(age)),
    })
}

/// Recently finished ones plus the lifetime numbers.
pub fn history(app: &App, limit: i64) -> Value {
    let rows: Vec<Value> = conn(app)
        .ok()
        .and_then(|c| {
            let mut st = c
                .prepare(
                    "SELECT match_id, sport, home, away, league, market, line, books, first_ms, last_ms, end_ms, end_reason, open_profit, peak_profit, censored
                     FROM arb_history WHERE end_ms IS NOT NULL ORDER BY end_ms DESC LIMIT ?1",
                )
                .ok()?;
            let it = st
                .query_map([limit], |r| {
                    let (first, last, end): (i64, i64, i64) = (r.get(8)?, r.get(9)?, r.get(10)?);
                    Ok(json!({
                        "match_id": r.get::<_, i64>(0)?, "sport": r.get::<_, Option<String>>(1)?, "home": r.get::<_, Option<String>>(2)?,
                        "away": r.get::<_, Option<String>>(3)?, "league": r.get::<_, Option<String>>(4)?, "market": r.get::<_, String>(5)?,
                        "line": r.get::<_, Option<f64>>(6)?, "books": r.get::<_, Option<String>>(7)?, "first_ms": first, "end_ms": end,
                        "lasted_ms": (last + end) / 2 - first, "end_reason": r.get::<_, Option<String>>(11)?,
                        "open_profit": r.get::<_, Option<f64>>(12)?, "peak_profit": r.get::<_, Option<f64>>(13)?, "censored": r.get::<_, i64>(14)? != 0,
                    }))
                })
                .ok()?;
            Some(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    let st = ST.lock();
    json!({"lifetime": st.stats, "open": st.open.len(), "recent": rows})
}
