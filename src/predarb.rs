//! Paper arbitrage between Polymarket and Kalshi, live and pre-match.
//!
//! Every 2 seconds, for each game both list (both seen in the last 5 minutes) and each outcome:
//! buy Yes on one venue and No on the other (No = 1 - Yes bid). One of the two always pays $1 per
//! contract pair, so cost < $1 after Kalshi's taker fee is a locked profit. Polymarket is taken as
//! fee-free. Kalshi quotes are read in the match's team order (`snipe::kalshi_quotes` flips them).
//!
//! Checks, because most raw gaps are data, not money: both quotes at most 60 s old and not
//! estimated; order-book size recorded on both legs and the trade capped to it (best price only);
//! at least $10 fillable; a gap showing in both directions at once is a mapping error and skipped;
//! profit at least `MIN_PROFIT`. One paper position per game and outcome per 10 minutes.
//! Stake 12.5% of the paper starting balance (Paper bot tab). Only games paying out within 6 hours
//! (kick-off + 3 h); at most 4 arbitrages waiting for their payout at once.
//!
//! Not checked: that both venues settle the market the same way (overtime, walkovers, retirements),
//! and that the order actually fills: the book can move in the time it takes to send two orders.

use crate::model::*;
use crate::snipe;
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const SCAN_MS: u64 = 2_000;
const MIN_PROFIT: f64 = 0.01;
const FRESH_MS: Ms = 60_000;
const SEEN_MS: Ms = 5 * 60_000;
const REPEAT_MS: Ms = 10 * 60_000;
/// Share of the paper starting balance per arbitrage.
const STAKE_SHARE: f64 = 0.125;
/// Only games that pay out within this long: kick-off plus a typical game length must fall inside.
const PAYOUT_MS: Ms = 6 * 3_600_000;
const GAME_LEN_MS: Ms = 3 * 3_600_000;
/// Arbitrages open (not yet paid out) at once.
const MAX_OPEN: usize = 4;

#[derive(Serialize, Clone, Debug)]
pub struct Pos {
    pub ts: Ms,
    pub match_id: i64,
    pub sport: String,
    pub home: String,
    pub away: String,
    pub outcome: String,
    /// "polymarket" or "kalshi": where Yes was bought (No on the other).
    pub yes_on: &'static str,
    pub yes: f64,
    pub no: f64,
    pub contracts: f64,
    pub cost: f64,
    pub fees: f64,
    pub profit: f64,
    pub live: bool,
    /// Expected payout: kick-off plus a typical game length.
    #[serde(default)]
    pub payout_ms: Ms,
    /// Sent by hand from the Arbitrage Scout (Order button).
    #[serde(default)]
    pub manual: bool,
}

#[derive(Default)]
struct State {
    seen: HashMap<(i64, String), Ms>,
    scans: u64,
    last_scan_ms: Ms,
    watching: usize,
    raw_gaps: u64,
    rejected: HashMap<&'static str, u64>,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);

fn kfee(n: f64, p: f64) -> f64 {
    snipe::kalshi_fee(n, p)
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(30)).await;
        loop {
            let a = app.clone();
            let _ = tokio::task::spawn_blocking(move || {
                if let Err(e) = scan(&a) {
                    tracing::warn!("predarb: {e:#}");
                }
            })
            .await;
            tokio::time::sleep(Duration::from_millis(SCAN_MS)).await;
        }
    });
}

fn games(c: &Connection, now: Ms) -> rusqlite::Result<Vec<(i64, String, String, String, bool, Ms)>> {
    let mut st = c.prepare_cached(
        "SELECT m.id, m.sport, m.home, m.away, COALESCE(m.start_ms, 0) FROM matches m
         WHERE EXISTS (SELECT 1 FROM book_events p WHERE p.match_id = m.id AND p.book = 'polymarket' AND p.last_seen_ms > ?1)
           AND EXISTS (SELECT 1 FROM book_events k WHERE k.match_id = m.id AND k.book = 'kalshi' AND k.last_seen_ms > ?1)",
    )?;
    let it = st.query_map(params![now - SEEN_MS], |r| {
        let start: i64 = r.get(4)?;
        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, start > 0 && start <= now, start))
    })?;
    Ok(it.filter_map(|x| x.ok()).collect())
}

fn scan(app: &App) -> anyhow::Result<()> {
    scan_with(app, None).map(|_| ())
}

/// One pass. `only` = a single game sent by hand (Order button): any locked profit above 0 counts,
/// and the 6-hour payout window and the 4-open limit do not apply; every other check does.
fn scan_with(app: &App, only: Option<i64>) -> anyhow::Result<(Vec<Pos>, HashMap<&'static str, u64>)> {
    let now = now_ms();
    let manual = only.is_some();
    let min_p = if manual { 1e-6 } else { MIN_PROFIT };
    let c = crate::store::open_ro(&app.db_path)?;
    let mut gs = games(&c, now)?;
    if let Some(id) = only {
        gs.retain(|g| g.0 == id);
        let held: i64 = c
            .query_row("SELECT COUNT(*) FROM pred_arbs WHERE match_id = ?1 AND json_extract(data, '$.payout_ms') > ?2", params![id, now], |r| r.get(0))
            .unwrap_or(0);
        if held > 0 {
            let mut r = HashMap::new();
            r.insert("already_bought", 1);
            return Ok((Vec::new(), r));
        }
    }
    let mut pq = snipe::pm_quotes(&c);
    let mut kq = snipe::kalshi_quotes(&c);
    let bank = crate::adaptive::bank();
    let stake = (bank.start * STAKE_SHARE).max(snipe::MIN_FILL);
    // Arbitrages still waiting for their payout.
    let mut open: usize = c
        .query_row("SELECT COUNT(*) FROM pred_arbs WHERE json_extract(data, '$.payout_ms') > ?1", [now], |r| r.get::<_, i64>(0))
        .unwrap_or(0) as usize;
    let mut new = Vec::new();
    let mut rej: HashMap<&'static str, u64> = HashMap::new();
    let mut raw = 0u64;
    for (mid, sport, home, away, live, start) in &gs {
        // Pays out within 6 hours: kick-off known, the game ends (kick-off + 3 h) inside the window.
        let payout_ms = start + GAME_LEN_MS;
        let pays_soon = *start > 0 && payout_ms <= now + PAYOUT_MS && payout_ms > now;
        let outs: &[&str] = if crate::books::three_way(sport) { &["home", "away", "draw"] } else { &["home", "away"] };
        for &o in outs {
            let (Some(p), Some(k)) = (pq.at(*mid, o, now), kq.at(*mid, o, now)) else { continue };
            // (yes venue, yes ask, no price = 1 - other's Yes bid)
            let dirs = [("polymarket", p.1, 1.0 - k.0), ("kalshi", k.1, 1.0 - p.0)];
            let cost1 = |yes_on: &str, y: f64, n: f64| y + n + if yes_on == "kalshi" { 0.07 * y * (1.0 - y) } else { 0.07 * n * (1.0 - n) };
            let gaps: Vec<_> = dirs.iter().filter(|(v, y, n)| (0.01..0.99).contains(y) && (0.01..0.99).contains(n) && 1.0 / cost1(v, *y, *n) - 1.0 >= min_p).collect();
            if gaps.is_empty() {
                continue;
            }
            raw += 1;
            let mut why = |r: &'static str| *rej.entry(r).or_default() += 1;
            if gaps.len() == 2 {
                why("both_directions");
                continue;
            }
            if now - p.2 > FRESH_MS || now - k.2 > FRESH_MS {
                why("stale_quote");
                continue;
            }
            if p.3 == "assumed" || k.3 == "assumed" {
                why("estimated_quote");
                continue;
            }
            if !manual && !pays_soon {
                why("pays_out_later");
                continue;
            }
            if !manual && open >= MAX_OPEN {
                why("four_open");
                continue;
            }
            let (yes_on, y, n) = *gaps[0];
            // Size at the best price on each leg: Yes takes the ask; No takes the other's Yes bid.
            let (pd, kd) = (pq.depth(*mid, o, now), kq.depth(*mid, o, now));
            let (ysz, nsz) = if yes_on == "polymarket" {
                (pd.and_then(|d| d.ask_size), kd.and_then(|d| d.bid_size))
            } else {
                (kd.and_then(|d| d.ask_size), pd.and_then(|d| d.bid_size))
            };
            let (Some(ysz), Some(nsz)) = (ysz, nsz) else {
                why("no_depth");
                continue;
            };
            let per = cost1(yes_on, y, n);
            let contracts = (stake / per).min(ysz).min(nsz).floor();
            if contracts * per < snipe::MIN_FILL {
                why("too_thin");
                continue;
            }
            let key = (*mid, o.to_string());
            if ST.lock().seen.get(&key).is_some_and(|t| now - *t < REPEAT_MS) {
                why("already_holding");
                continue;
            }
            let fees = if yes_on == "kalshi" { kfee(contracts, y) } else { kfee(contracts, n) };
            let cost = contracts * (y + n) + fees;
            let profit = contracts - cost;
            if profit / cost < min_p {
                why("fee_rounding");
                continue;
            }
            ST.lock().seen.insert(key, now);
            new.push(Pos { ts: now, match_id: *mid, sport: sport.clone(), home: home.clone(), away: away.clone(), outcome: o.into(), yes_on, yes: y, no: n, contracts, cost, fees, profit, live: *live, payout_ms, manual });
            open += 1;
        }
    }
    if manual && new.len() > 1 {
        // One Order = one arbitrage: the best return (a two-way game shows the same gap mirrored).
        new.sort_by(|a, b| (b.profit / b.cost).partial_cmp(&(a.profit / a.cost)).unwrap_or(std::cmp::Ordering::Equal));
        new.truncate(1);
    }
    if !new.is_empty() {
        let w = Connection::open(&app.db_path)?;
        w.busy_timeout(Duration::from_secs(5))?;
        for p in &new {
            w.execute("INSERT INTO pred_arbs(ts_ms, match_id, data) VALUES (?1, ?2, ?3)", params![p.ts, p.match_id, serde_json::to_string(p)?])?;
            tracing::info!("predarb: paper arb {} v {} {} yes on {} {:.1}%", p.home, p.away, p.outcome, p.yes_on, 100.0 * p.profit / p.cost);
        }
    }
    if manual {
        return Ok((new, rej));
    }
    let mut st = ST.lock();
    st.scans += 1;
    st.last_scan_ms = now;
    st.watching = gs.len();
    st.raw_gaps += raw;
    for (k, v) in rej {
        *st.rejected.entry(k).or_default() += v;
    }
    st.seen.retain(|_, t| now - *t < REPEAT_MS);
    Ok((new, HashMap::new()))
}

/// Order button: re-price one game from the latest Polymarket and Kalshi quotes and take the
/// paper arbitrage if it is still there. (Later: the same call places the real orders.)
pub fn order(app: &App, match_id: i64) -> Value {
    match scan_with(app, Some(match_id)) {
        Ok((taken, _)) if !taken.is_empty() => json!({"ok": true, "taken": taken}),
        Ok((_, rej)) => {
            let why = rej.keys().map(|k| k.replace('_', " ")).collect::<Vec<_>>().join(", ");
            json!({"ok": false, "error": if why.is_empty() { "no arbitrage between Polymarket and Kalshi at the latest prices".to_string() } else { format!("not taken: {why}") }})
        }
        Err(e) => json!({"ok": false, "error": format!("{e:#}")}),
    }
}

pub fn view(app: &App) -> Value {
    let rows: Vec<Value> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT data FROM pred_arbs ORDER BY ts_ms DESC LIMIT 500").ok()?;
            let it = st.query_map([], |r| r.get::<_, String>(0)).ok()?;
            Some(it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect())
        })
        .unwrap_or_default();
    let (n, cost, profit) = rows.iter().fold((0, 0.0, 0.0), |a, r| (a.0 + 1, a.1 + r["cost"].as_f64().unwrap_or(0.0), a.2 + r["profit"].as_f64().unwrap_or(0.0)));
    let st = ST.lock();
    let now = now_ms();
    let open = rows.iter().filter(|r| r["payout_ms"].as_i64().unwrap_or(0) > now).count();
    json!({"open": open, "max_open": MAX_OPEN, "stake_share": STAKE_SHARE, "trades": n, "staked": cost, "locked_profit": profit, "min_profit": MIN_PROFIT, "scans": st.scans, "last_scan_ms": st.last_scan_ms,
        "watching": st.watching, "raw_gaps": st.raw_gaps, "rejected": st.rejected, "recent": rows})
}

/// Sell an open paper arbitrage now: both legs sold at the latest bids (Yes at its venue's Yes
/// bid; No at 1 - the other venue's Yes ask), Kalshi's fee on its sale. The position closes with
/// that result instead of waiting for the payout.
pub fn sell(app: &App, ts: Ms, match_id: i64) -> Value {
    let r = (|| -> anyhow::Result<Value> {
        let now = now_ms();
        let c = crate::store::open_ro(&app.db_path)?;
        let data: String = c.query_row("SELECT data FROM pred_arbs WHERE ts_ms = ?1 AND match_id = ?2", params![ts, match_id], |r| r.get(0))?;
        let mut v: Value = serde_json::from_str(&data)?;
        anyhow::ensure!(v["payout_ms"].as_i64().unwrap_or(0) > now && v["closed_ms"].is_null(), "this position is already closed");
        let o = v["outcome"].as_str().unwrap_or("").to_string();
        let yes_on = v["yes_on"].as_str().unwrap_or("").to_string();
        let n = v["contracts"].as_f64().unwrap_or(0.0);
        let cost = v["cost"].as_f64().unwrap_or(0.0);
        let mut pq = snipe::pm_quotes(&c);
        let mut kq = snipe::kalshi_quotes(&c);
        let (p, k) = (pq.at(match_id, &o, now), kq.at(match_id, &o, now));
        let (Some(p), Some(k)) = (p, k) else { anyhow::bail!("no current Polymarket or Kalshi price for this game") };
        anyhow::ensure!(now - p.2 <= 5 * 60_000 && now - k.2 <= 5 * 60_000, "prices are more than 5 minutes old; try again when the market is active");
        // (Yes sale price, No sale price)
        let (ys, ns) = if yes_on == "polymarket" { (p.0, 1.0 - k.1) } else { (k.0, 1.0 - p.1) };
        let kfee_sale = if yes_on == "kalshi" { kfee(n, ys) } else { kfee(n, ns) };
        let value = n * (ys + ns) - kfee_sale;
        let pnl = value - cost;
        v["closed_ms"] = json!(now);
        v["close_value"] = json!(value);
        v["pnl"] = json!(pnl);
        v["sold_yes"] = json!(ys);
        v["sold_no"] = json!(ns);
        v["payout_ms"] = json!(now);
        let w = Connection::open(&app.db_path)?;
        w.busy_timeout(Duration::from_secs(5))?;
        w.execute("UPDATE pred_arbs SET data = ?3 WHERE ts_ms = ?1 AND match_id = ?2", params![ts, match_id, serde_json::to_string(&v)?])?;
        tracing::info!("predarb: sold {} v {} for {:.2} ({:+.2})", v["home"], v["away"], value, pnl);
        Ok(json!({"ok": true, "position": v}))
    })();
    r.unwrap_or_else(|e| json!({"ok": false, "error": format!("{e:#}")}))
}

/// Arbitrage activity after `since` for Telegram: bought, sold early, paid out. Returns the lines
/// and the newest event time.
pub fn events_since(app: &App, since: Ms) -> (Vec<String>, Ms) {
    let now = now_ms();
    let mut ev: Vec<(Ms, String)> = Vec::new();
    let rows: Vec<Value> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT data FROM pred_arbs WHERE ts_ms > ?1 - 86400000").ok()?;
            let it = st.query_map([since], |r| r.get::<_, String>(0)).ok()?;
            Some(it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect())
        })
        .unwrap_or_default();
    let name = |v: &Value| format!("{} v {}", v["home"].as_str().unwrap_or(""), v["away"].as_str().unwrap_or(""));
    for v in &rows {
        let ts = v["ts"].as_i64().unwrap_or(0);
        let cost = v["cost"].as_f64().unwrap_or(0.0);
        if ts > since {
            ev.push((ts, format!("🟢 Bought arbitrage · {} · {} · ${:.2} staked, +${:.2} locked", name(v), v["outcome"].as_str().unwrap_or(""), cost, v["profit"].as_f64().unwrap_or(0.0))));
        }
        if let Some(cl) = v["closed_ms"].as_i64() {
            if cl > since {
                let p = v["pnl"].as_f64().unwrap_or(0.0);
                ev.push((cl, format!("🔴 Sold arbitrage early · {} · {}${:.2}", name(v), if p >= 0.0 { "+" } else { "-" }, p.abs())));
            }
        } else if let Some(po) = v["payout_ms"].as_i64() {
            if po > since && po <= now {
                ev.push((po, format!("💰 Arbitrage paid out · {} · +${:.2}", name(v), v["profit"].as_f64().unwrap_or(0.0))));
            }
        }
    }
    ev.sort_by_key(|e| e.0);
    let upto = ev.last().map(|e| e.0).unwrap_or(since);
    (ev.into_iter().map(|e| e.1).collect(), upto)
}
