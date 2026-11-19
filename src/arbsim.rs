//! Whole-dataset simulation of the hedge & arbitrage bot with its Bots-page settings.
//!
//! Replays every recorded Polymarket and Kalshi quote (best bid / ask and the size resting there)
//! of every game both venues listed, in time order, and trades it the way predarb.rs does:
//! * a gap = Yes on one venue + No on the other costing under $1 after both fees (Polymarket sports
//!   0.05 p(1-p), Kalshi 0.07 p(1-p)), returning at least `min_live` (game playing) or `min_pre`;
//!   pre-match only when paying out within 6 hours; skipped pre-match with `live_only`;
//! * a gap showing both ways at once is a data error and skipped;
//! * order timing as measured by the test runs (timing.rs): the Polymarket leg matches after its
//!   reply time (+ the live-sports delay on a game playing) at no worse than the price seen; the
//!   Kalshi leg follows for what filled; if Kalshi has moved by then, the Polymarket shares are
//!   sold back once settled and the loss is booked (leg failure); size = what rests at the best
//!   prices then, within the bot's cash, its per-trade and open-money caps and the per-game cap;
//! * one open position per game; it closes when both legs sell (into the recorded bids, fees paid)
//!   for `unwind_at` of the locked profit, else at the payout (kick-off + 3 h) for $1 a pair.
//! Not replayable: the settlement-rule comparison (rule texts were not stored historically), book
//! levels behind the best price (not recorded), and Kalshi sizes before 18 Nov ~16:00 UTC (the
//! book bug; reported as a share of the trades). Quotes wider than 15c, crossed, or older than
//! 30 minutes are treated as no quote (stale or broken data).

use crate::bots::ArbCfg;
use crate::model::*;
use crate::snipe;
use rusqlite::params;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicUsize, Ordering};

const PM_RATE: f64 = 0.05;
const K_RATE: f64 = 0.07;
const MAX_SPREAD: f64 = 0.15;
const MAX_AGE_MS: Ms = 30 * 60_000;
const PAYOUT_MS: Ms = 6 * 3_600_000;
const GAME_LEN_MS: Ms = 3 * 3_600_000;
/// Kalshi recorded sizes are trustworthy from here on (book fix).
pub const KALSHI_FIX_MS: Ms = 1_795_017_600_000;

#[derive(Clone, Copy, Default)]
struct Side {
    ts: Ms,
    bid: f64,
    ask: f64,
    bsz: f64,
    asz: f64,
    ok: bool,
}

#[derive(Clone, Copy)]
struct State {
    ts: Ms,
    p: Side,
    k: Side,
}

fn side(ts: Ms, b: Option<f64>, a: Option<f64>, bs: Option<f64>, az: Option<f64>) -> Side {
    match (b, a) {
        (Some(b), Some(a)) if b > 0.0 && a < 1.0 && b < a && a - b <= MAX_SPREAD => Side { ts, bid: b, ask: a, bsz: bs.unwrap_or(-1.0), asz: az.unwrap_or(-1.0), ok: true },
        _ => Side { ts, ..Default::default() },
    }
}

fn unit_pm(p: f64) -> f64 {
    p + PM_RATE * p * (1.0 - p)
}
fn unit_k(p: f64) -> f64 {
    p + K_RATE * p * (1.0 - p)
}

/// The best gap at a state: (yes on PM?, cost per pair, max pairs (-1 = size unknown)).
fn gap(s: &State, min_p: f64) -> (Option<(bool, f64, f64)>, bool) {
    let fresh = |x: &Side| x.ok && s.ts - x.ts <= MAX_AGE_MS;
    if !fresh(&s.p) || !fresh(&s.k) {
        return (None, false);
    }
    let a = unit_pm(s.p.ask) + unit_k(1.0 - s.k.bid);
    let b = unit_k(s.k.ask) + unit_pm(1.0 - s.p.bid);
    let ok = |c: f64| 1.0 / c - 1.0 >= min_p;
    let size = |x: f64, y: f64| if x < 0.0 || y < 0.0 { -1.0 } else { x.min(y) };
    match (ok(a), ok(b)) {
        (true, true) => (None, true),
        (true, false) => (Some((true, a, size(s.p.asz, s.k.bsz))), false),
        (false, true) => (Some((false, b, size(s.k.asz, s.p.bsz))), false),
        _ => (None, false),
    }
}

/// Sale value of one pair now (fees paid) and the pairs the bids can take.
fn sale(s: &State, pm_yes: bool) -> Option<(f64, f64)> {
    if !s.p.ok || !s.k.ok {
        return None;
    }
    let net_pm = |x: f64| x - PM_RATE * x * (1.0 - x);
    let net_k = |x: f64| x - K_RATE * x * (1.0 - x);
    // Yes sells into that venue's Yes bid; No into the other's No bid = 1 - Yes ask.
    let (v, sz) = if pm_yes { (net_pm(s.p.bid) + net_k(1.0 - s.k.ask), s.p.bsz.min(s.k.asz)) } else { (net_k(s.k.bid) + net_pm(1.0 - s.p.ask), s.k.bsz.min(s.p.asz)) };
    Some((v, sz))
}

struct Cand {
    t: Ms,
    m: i64,
    sport: String,
    unit: f64,
    max_n: f64,
    exit_t: Ms,
    exit_value: f64,
    unwound: bool,
    live: bool,
    /// The Kalshi leg did not fill: Polymarket shares bought and sold back.
    failed: bool,
}

pub struct Progress {
    pub done: AtomicUsize,
    pub total: AtomicUsize,
}

pub fn run(db: &std::path::Path, cfg: &ArbCfg, game_cap: f64, prog: &Progress) -> anyhow::Result<Value> {
    run_since(db, cfg, game_cap, prog, 0, false)
}

/// The same, trading only gaps seen at or after `since` (e.g. after the Kalshi book fix).
/// `kalshi_first`: buy the Kalshi leg first (no matching delay there), then Polymarket; a failed
/// second leg sells the Kalshi contracts back.
pub fn run_since(db: &std::path::Path, cfg: &ArbCfg, game_cap: f64, prog: &Progress, since: Ms, kalshi_first: bool) -> anyhow::Result<Value> {
    let c = crate::store::open_ro(db)?;
    let now = now_ms();
    let games: Vec<(i64, String, Ms)> = c
        .prepare(
            "SELECT m.id, m.sport, COALESCE(m.start_ms, 0) FROM matches m
             WHERE EXISTS (SELECT 1 FROM book_events p WHERE p.match_id = m.id AND p.book = 'polymarket')
               AND EXISTS (SELECT 1 FROM book_events k WHERE k.match_id = m.id AND k.book = 'kalshi')",
        )?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .filter_map(|x| x.ok())
        .collect();
    prog.total.store(games.len(), Ordering::Relaxed);
    let (mut cands, mut unknown_size, mut both_ways, mut gone) = (Vec::new(), 0usize, 0usize, 0usize);
    let mut first_ts: Option<Ms> = None;
    for (gi, (m, sport, start)) in games.iter().enumerate() {
        prog.done.store(gi, Ordering::Relaxed);
        let Some((ev, swap)) = snipe::kalshi_event(&c, *m) else { continue };
        let outs: &[&str] = if crate::books::three_way(sport) { &["home", "away", "draw"] } else { &["home", "away"] };
        for o in outs {
            let ko = snipe::kalshi_side(o, swap);
            let pm: Vec<(Ms, u8, Side)> = c
                .prepare_cached("SELECT ts_ms, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 ORDER BY ts_ms")?
                .query_map(params![m, o], |r| Ok((r.get::<_, Ms>(0)?, 0u8, side(r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))))?
                .filter_map(|x| x.ok())
                .collect();
            let k: Vec<(Ms, u8, Side)> = c
                .prepare_cached("SELECT ts_ms, bid, ask, bid_size, ask_size FROM kalshi_quotes WHERE event = ?1 AND outcome = ?2 AND est = 0 ORDER BY ts_ms")?
                .query_map(params![ev, ko], |r| Ok((r.get::<_, Ms>(0)?, 1u8, side(r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))))?
                .filter_map(|x| x.ok())
                .collect();
            if pm.is_empty() || k.is_empty() {
                continue;
            }
            let mut evs = pm;
            evs.extend(k);
            evs.sort_by_key(|e| e.0);
            first_ts = Some(first_ts.map_or(evs[0].0, |f: Ms| f.min(evs[0].0)));
            // States after each update (both venues known).
            let mut states: Vec<State> = Vec::with_capacity(evs.len());
            let (mut p, mut kk) = (None, None);
            for (ts, w, s) in evs {
                if w == 0 {
                    p = Some(s);
                } else {
                    kk = Some(s);
                }
                if let (Some(p), Some(k)) = (p, kk) {
                    states.push(State { ts, p, k });
                }
            }
            let at = |t: Ms| -> Option<&State> {
                let i = states.partition_point(|s| s.ts <= t);
                if i == 0 { None } else { states.get(i - 1) }
            };
            let mut last_cand: Ms = 0;
            for (i, s) in states.iter().enumerate() {
                let live = *start > 0 && *start <= s.ts;
                let payout = start + GAME_LEN_MS;
                let pays_soon = *start > 0 && payout <= s.ts + PAYOUT_MS && payout > s.ts;
                if !pays_soon || (!live && cfg.live_only) || s.ts < since {
                    continue;
                }
                let min_p = if live { cfg.min_live } else { cfg.min_pre };
                let (g, both) = gap(s, min_p);
                if both {
                    both_ways += 1;
                    continue;
                }
                let Some((pm_yes, _, _)) = g else { continue };
                if s.ts - last_cand < 2_000 {
                    continue;
                }
                // The two legs as real orders fill (measured timings, timing.rs): the Polymarket
                // leg at its price now as the limit, matched after its reply time (+ the live-
                // sports delay on a game playing) against the quote THEN; the Kalshi leg for what
                // filled, at its price now as the limit, after its order time. If Kalshi is gone
                // by then, the Polymarket shares are sold back once settled (a real loss).
                let side_px = |st: &State, pm: bool| -> (f64, f64) {
                    // (price, size) of the leg we buy: PM Yes = PM ask, PM No = 1 - PM bid;
                    // Kalshi Yes = K ask, Kalshi No = 1 - K bid.
                    match (pm, pm_yes) {
                        (true, true) => (st.p.ask, st.p.asz),
                        (true, false) => (1.0 - st.p.bid, st.p.bsz),
                        (false, true) => (1.0 - st.k.bid, st.k.bsz),
                        (false, false) => (st.k.ask, st.k.asz),
                    }
                };
                let (pm_lim, pm_sz0) = side_px(s, true);
                let (k_lim, k_sz0) = side_px(s, false);
                if pm_sz0 < 0.0 || k_sz0 < 0.0 {
                    unknown_size += 1;
                    last_cand = s.ts;
                    continue;
                }
                let tm = crate::timing::get();
                let (t_k, max_n, unit);
                if kalshi_first {
                    let t_k1 = s.ts + tm.kalshi_order_ms;
                    let t_pm = t_k1 + tm.pm_fill_ms(live);
                    let (Some(s1), Some(s2)) = (at(t_k1), at(t_pm)) else { continue };
                    last_cand = s.ts;
                    let (k1, ksz1) = side_px(s1, false);
                    if !s1.k.ok || k1 > k_lim + 1e-9 || ksz1 < 1.0 {
                        gone += 1;
                        continue; // nothing bought
                    }
                    let k_n = ksz1.floor();
                    let (p2, sz2) = side_px(s2, true);
                    if !s2.p.ok || p2 > pm_lim + 1e-9 || sz2 < 1.0 {
                        // Leg failure: sell the Kalshi contracts back (Kalshi Yes when the
                        // Polymarket leg was No, and the other way round).
                        let t_back = t_pm + tm.kalshi_order_ms;
                        let back = at(t_back).filter(|x| x.k.ok).map(|x| if pm_yes { 1.0 - x.k.ask } else { x.k.bid }).unwrap_or(0.0);
                        let net = back - K_RATE * back * (1.0 - back);
                        cands.push(Cand { t: t_k1, m: *m, sport: sport.clone(), unit: unit_k(k1), max_n: k_n, exit_t: t_back, exit_value: net, unwound: false, live, failed: true });
                        continue;
                    }
                    t_k = t_pm;
                    max_n = k_n.min(sz2.floor());
                    unit = unit_pm(p2) + unit_k(k1);
                } else {
                    let t_pm = s.ts + tm.pm_fill_ms(live);
                    let t_k0 = t_pm + tm.kalshi_order_ms;
                    let (Some(s1), Some(s2)) = (at(t_pm), at(t_k0)) else { continue };
                    last_cand = s.ts;
                    let (p1, sz1) = side_px(s1, true);
                    if !s1.p.ok || p1 > pm_lim + 1e-9 || sz1 < 1.0 {
                        gone += 1;
                        continue; // nothing bought
                    }
                    let pm_n = sz1.floor();
                    let pm_unit = unit_pm(p1);
                    let (k2, ksz2) = side_px(s2, false);
                    if !s2.k.ok || k2 > k_lim + 1e-9 || ksz2 < 1.0 {
                        // Leg failure: sell the Polymarket shares back after they settle + the sale matches.
                        let t_back = t_k0 + tm.pm_settle_ms + tm.pm_fill_ms(live);
                        let back = at(t_back).filter(|x| x.p.ok).map(|x| if pm_yes { x.p.bid } else { 1.0 - x.p.ask }).unwrap_or(0.0);
                        let net = back - PM_RATE * back * (1.0 - back);
                        cands.push(Cand { t: t_pm, m: *m, sport: sport.clone(), unit: pm_unit, max_n: pm_n, exit_t: t_back, exit_value: net, unwound: false, live, failed: true });
                        continue;
                    }
                    t_k = t_k0;
                    max_n = pm_n.min(ksz2.floor());
                    unit = pm_unit + unit_k(k2);
                }
                // Exit: the first later state whose sale fetches `unwind_at` of the locked profit
                // for every pair, else the payout.
                let (mut exit_t, mut exit_value, mut unwound) = (payout, 1.0, false);
                if cfg.unwind_at > 0.0 {
                    for s3 in states[i + 1..].iter() {
                        if s3.ts >= payout {
                            break;
                        }
                        // A sale can go once the shares settled; it matches after the delay.
                        if s3.ts < t_k + tm.pm_settle_ms {
                            continue;
                        }
                        if let Some((v, sz)) = sale(s3, pm_yes) {
                            if sz >= max_n && v > unit && v - unit >= cfg.unwind_at * (1.0 - unit) - 1e-9 {
                                // Priced where the sale matches.
                                let fill_at = at(s3.ts + tm.pm_fill_ms(live)).and_then(|x| sale(x, pm_yes)).map(|x| x.0).unwrap_or(v);
                                if fill_at > unit {
                                    exit_t = s3.ts + tm.pm_fill_ms(live);
                                    exit_value = fill_at;
                                    unwound = true;
                                    break;
                                }
                            }
                        }
                    }
                }
                cands.push(Cand { t: t_k, m: *m, sport: sport.clone(), unit, max_n, exit_t, exit_value, unwound, live, failed: false });
            }
        }
    }
    prog.done.store(games.len(), Ordering::Relaxed);
    // The wallet, in time order.
    cands.sort_by_key(|c| c.t);
    let start_bal = cfg.c.start;
    let (mut cash, mut realized) = (start_bal, 0.0);
    let mut open: Vec<(Ms, i64, f64, f64)> = Vec::new(); // (exit, match, cost, value)
    let mut held: HashSet<i64> = HashSet::new();
    let mut taken: Vec<(Ms, Ms, f64, f64, String, bool, bool, bool)> = Vec::new();
    let mut per_game: HashMap<i64, f64> = HashMap::new();
    for cd in &cands {
        open.retain(|o| {
            if o.0 <= cd.t {
                cash += o.3;
                realized += o.3 - o.2;
                held.remove(&o.1);
                per_game.remove(&o.1);
                false
            } else {
                true
            }
        });
        if held.contains(&cd.m) || cd.exit_t > now {
            continue;
        }
        let open_cost: f64 = open.iter().map(|o| o.2).sum();
        let budget = cash.min(cfg.c.max_trade).min(cfg.c.max_open - open_cost).min(game_cap - per_game.get(&cd.m).copied().unwrap_or(0.0));
        let n = cd.max_n.min((budget / cd.unit).floor());
        if n < 1.0 || n * cd.unit < snipe::MIN_FILL {
            continue;
        }
        let cost = n * cd.unit;
        let value = n * cd.exit_value;
        cash -= cost;
        held.insert(cd.m);
        *per_game.entry(cd.m).or_default() += cost;
        open.push((cd.exit_t, cd.m, cost, value));
        taken.push((cd.t, cd.exit_t, value - cost, cost, cd.sport.clone(), cd.unwound, cd.live, cd.failed));
    }
    let _ = realized;
    let pre_fix = taken.iter().filter(|t| t.0 < KALSHI_FIX_MS).count();
    let unwound = taken.iter().filter(|t| t.5).count();
    let live = taken.iter().filter(|t| t.6).count();
    let hold_mins: Vec<f64> = taken.iter().map(|t| (t.1 - t.0) as f64 / 60_000.0).collect();
    let mut sum = crate::adaptive::sim_summary(start_bal, first_ts, now, taken.iter().map(|t| (t.0, t.1, t.2, t.3, t.4.as_str(), t.5)).collect());
    sum["unwound"] = json!(unwound);
    sum["live_trades"] = json!(live);
    sum["candidates"] = json!(cands.len());
    sum["size_unknown"] = json!(unknown_size);
    sum["both_ways_skipped"] = json!(both_ways);
    sum["pre_fix_trades"] = json!(pre_fix);
    sum["leg_failures"] = json!(taken.iter().filter(|t| t.7).count());
    sum["leg_failure_loss"] = json!(taken.iter().filter(|t| t.7).map(|t| t.2).sum::<f64>());
    sum["gone_before_fill"] = json!(gone);
    sum["timing"] = json!(crate::timing::get());
    sum["median_hold_min"] = json!(if hold_mins.is_empty() {
        0.0
    } else {
        let mut h = hold_mins.clone();
        h.sort_by(|a, b| a.partial_cmp(b).unwrap());
        h[h.len() / 2]
    });
    Ok(sum)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_the_cheap_direction_and_its_size() {
        let s = State { ts: 10, p: side(10, Some(0.44), Some(0.45), Some(50.0), Some(80.0)), k: side(10, Some(0.53), Some(0.55), Some(30.0), Some(40.0)) };
        // Yes PM 45 + No Kalshi 47 (+ fees) < $1; the other way 55 + 56 > $1.
        let (g, both) = gap(&s, 0.01);
        assert!(!both);
        let (pm_yes, unit, n) = g.unwrap();
        assert!(pm_yes && unit < 0.96 && n == 30.0);
        // Unknown sizes are reported as such.
        let s2 = State { k: side(10, Some(0.53), Some(0.55), None, Some(40.0)), ..s };
        assert_eq!(gap(&s2, 0.01).0.unwrap().2, -1.0);
        // Broken data (crossed / too wide) is no quote.
        assert!(!side(1, Some(0.6), Some(0.5), None, None).ok);
        assert!(!side(1, Some(0.02), Some(0.41), None, None).ok);
    }
}
