//! Hedge & arbitrage bot (bots.rs): Polymarket x Kalshi, paper or real, its own wallet.
//!
//! For each game both list and each outcome: buy Yes on one venue and No on the other (No = 1 -
//! Yes bid). One of the two always pays $1 per contract pair, so a pair costing under $1 after BOTH
//! venues' taker fees is a locked profit: Kalshi 0.07 x p(1-p) a contract (rounded up to the cent),
//! Polymarket the market's own `feeSchedule` (sports: 0.05 x p(1-p); unknown = sports, never free).
//! This covers plain gaps and Kalshi-lag "locks" alike (Polymarket followed the bookmakers, Kalshi
//! has not yet).
//!
//! When: on every book change on either venue (venue.rs wakes it), at most every 150 ms, else
//! every 2 s: real gaps last a median of ~9 s and 69% open right after a bookmaker move.
//! Prices: the full order books both sockets keep. The order walks both books together and takes
//! each further level while the pair still returns `min_pair`; the whole position must return
//! `min_live` (game playing) or `min_pre` (pre-match: money locked ~5x longer per % and the
//! called-off risk). Size: as deep as the books pay, within the bot's cash, its per-trade and open
//! caps, and the per-game cap shared with the snipe bot. Right before taking it, both books are
//! read again `ORDER_MS` later and the position is priced on THAT walk.
//! Checks: both sockets alive; books agree with the latest recorded quotes; not a both-directions
//! (mapping) gap; settlement (settle.rs): same scheduled game, same result rule, and pre-match the
//! called-off case may not lose more than `settle::VOID_MAX_LOSS`. One open position per game.
//!
//! Real money: the Polymarket leg first (its price is the one that disappears: median ~10 s vs ~3
//! min for Kalshi's stale price), fill-and-kill at the walked limit; then the Kalshi leg for what
//! filled, fill-or-kill. If Kalshi does not fill, the Polymarket shares are sold back at once.
//!
//! Auto-unwind: an open position whose two legs can be SOLD now (walking both books' bids, fees
//! paid) for at least `unwind_at` of its locked profit is sold, instead of waiting hours for the
//! payout (76% of gaps reach their full profit within a median ~10 min): the money is free again.

use crate::bots::{self, ARB};
use crate::model::*;
use crate::settle;
use crate::snipe;
use crate::venue::{self, Fee, Ladder, V};
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde::Serialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

/// Longest wait between scans when no book changes.
const SCAN_MS: u64 = 2_000;
/// Shortest gap between scans.
const MIN_GAP_MS: u64 = 150;
/// Both venues' sockets must have delivered a message this recently.
const ALIVE_MS: Ms = 15_000;
const SEEN_MS: Ms = 5 * 60_000;
/// Order travel: the books are read again this much later and the position priced on them.
const ORDER_MS: u64 = 250;
/// Only games that pay out within this long: kick-off plus a typical game length must fall inside.
const PAYOUT_MS: Ms = 6 * 3_600_000;
const GAME_LEN_MS: Ms = 3 * 3_600_000;
/// Stakes the depth curve reports profit at.
const CURVE: [f64; 6] = [100.0, 250.0, 500.0, 1_000.0, 2_500.0, 5_000.0];
const KALSHI_FEE: Fee = Fee { rate: 0.07, exponent: 1.0 };

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
    /// Average prices paid per contract (before fees).
    pub yes: f64,
    pub no: f64,
    /// Worst price paid on each leg, and how many book levels each leg took.
    pub yes_worst: f64,
    pub no_worst: f64,
    pub levels: (usize, usize),
    pub contracts: f64,
    pub cost: f64,
    pub fees: f64,
    pub pm_fee: f64,
    pub kalshi_fee: f64,
    pub profit: f64,
    /// Return of the first and of the last pair bought (the book's gradient).
    pub first_return: f64,
    pub last_return: f64,
    /// (stake, contracts, profit) at other stakes, walking the same books.
    pub depth_curve: Vec<(f64, f64, f64)>,
    /// (contracts, profit) the scan saw before the order-time re-check.
    pub scan_walk: (f64, f64),
    /// How long since each book (Polymarket, Kalshi) last changed when the position was taken.
    pub book_age_ms: (Ms, Ms),
    pub settle: settle::Check,
    pub live: bool,
    /// Expected payout: kick-off plus a typical game length.
    pub payout_ms: Ms,
    /// Sent by hand from the Arbitrage Scout (Order button).
    pub manual: bool,
    /// Real money (false = paper).
    pub real: bool,
    /// What was bought where: the Polymarket token (the outcome's, or its complement for No), the
    /// Kalshi market and side.
    pub pm_token: String,
    pub kalshi_ticker: String,
    pub kalshi_yes: bool,
    pub leg_order: &'static str,
    /// Paper: the Kalshi leg did not fill after the Polymarket leg had; closed at once (sold back).
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub leg_failed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub closed_ms: Option<Ms>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pnl: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub close_reason: Option<&'static str>,
}

#[derive(Default)]
struct State {
    scans: u64,
    last_scan_ms: Ms,
    watching: usize,
    raw_gaps: u64,
    unwound: u64,
    rejected: HashMap<&'static str, u64>,
    /// Latest reason per game a gap was not taken, with what was seen: (when, reason, detail).
    last: HashMap<i64, (Ms, &'static str, String)>,
    games: Vec<(i64, String, String, String, bool, Ms)>,
    games_at: Ms,
    kev: HashMap<i64, (Ms, Option<(String, bool)>)>,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(30)).await;
        loop {
            // A book changed on either venue, or 2 s passed.
            let _ = tokio::time::timeout(Duration::from_millis(SCAN_MS), venue::CHANGED.notified()).await;
            let a = app.clone();
            let _ = tokio::task::spawn_blocking(move || {
                if let Err(e) = unwind_pass(&a) {
                    tracing::warn!("predarb unwind: {e:#}");
                }
                if let Err(e) = scan(&a) {
                    tracing::warn!("predarb: {e:#}");
                }
            })
            .await;
            tokio::time::sleep(Duration::from_millis(MIN_GAP_MS)).await;
        }
    });
}

/// The match's Kalshi event (cached a minute).
fn kev(c: &Connection, mid: i64, now: Ms) -> Option<(String, bool)> {
    if let Some((t, v)) = ST.lock().kev.get(&mid) {
        if now - t < 60_000 {
            return v.clone();
        }
    }
    let v = snipe::kalshi_event(c, mid);
    ST.lock().kev.insert(mid, (now, v.clone()));
    v
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

/// One leg of the pair: the price levels it buys from (best first) and its fee.
struct Leg {
    v: V,
    levels: Vec<(f64, f64)>,
    fee: Fee,
}

impl Leg {
    /// Buying Yes: the venue's Yes asks.
    fn yes(v: V, l: &Ladder, fee: Fee) -> Leg {
        Leg { v, levels: l.asks.clone(), fee }
    }
    /// Buying No: 1 - the venue's Yes bids (its best bid is the cheapest No).
    fn no(v: V, l: &Ladder, fee: Fee) -> Leg {
        Leg { v, levels: l.bids.iter().map(|(p, q)| (1.0 - p, *q)).collect(), fee }
    }
    /// Fee for the fills (price, contracts) this leg made: Kalshi rounds the order up to the cent,
    /// Polymarket each fill to 5 decimals.
    fn fees(&self, fills: &[(f64, f64)]) -> f64 {
        match self.v {
            V::Kalshi => {
                let raw: f64 = fills.iter().map(|(p, n)| self.fee.raw(*n, *p)).sum();
                (raw * 100.0 - 1e-9).ceil().max(0.0) / 100.0
            }
            V::Pm => fills.iter().map(|(p, n)| venue::pm_fee(self.fee, *n, *p)).sum(),
        }
    }
    fn unit(&self, p: f64) -> f64 {
        p + self.fee.raw(1.0, p)
    }
}

#[derive(Clone, Debug, Default)]
struct Walk {
    contracts: f64,
    yes_fills: Vec<(f64, f64)>,
    no_fills: Vec<(f64, f64)>,
    cost: f64,
    fee_yes: f64,
    fee_no: f64,
    profit: f64,
    first_return: f64,
    last_return: f64,
}

/// Walk both books together: at each step buy the next pair at the two cheapest remaining levels
/// while that pair still returns at least `min_ret` after fees, up to `budget` dollars. Whole
/// contracts only.
fn walk(yes: &Leg, no: &Leg, budget: f64, min_ret: f64) -> Option<Walk> {
    let (mut i, mut j) = (0usize, 0usize);
    let (mut ry, mut rn) = (yes.levels.first()?.1, no.levels.first()?.1);
    let (mut yf, mut nf): (Vec<(f64, f64)>, Vec<(f64, f64)>) = (Vec::new(), Vec::new());
    let (mut spent, mut total) = (0.0, 0.0);
    let (mut first, mut last) = (None, 0.0);
    loop {
        let (py, pn) = (yes.levels[i].0, no.levels[j].0);
        let unit = yes.unit(py) + no.unit(pn);
        let ret = 1.0 / unit - 1.0;
        if ret < min_ret || !(0.0..1.0).contains(&py) || !(0.0..1.0).contains(&pn) {
            break;
        }
        first.get_or_insert(ret);
        let room = (budget - spent) / unit;
        let q = ry.min(rn).min(room);
        if q <= 1e-9 {
            break;
        }
        yf.push((py, q));
        nf.push((pn, q));
        spent += q * unit;
        total += q;
        last = ret;
        ry -= q;
        rn -= q;
        if room - q <= 1e-9 {
            break;
        }
        if ry <= 1e-9 {
            i += 1;
            let Some(l) = yes.levels.get(i) else { break };
            ry = l.1;
        }
        if rn <= 1e-9 {
            j += 1;
            let Some(l) = no.levels.get(j) else { break };
            rn = l.1;
        }
    }
    // Whole contracts: trim the last fills.
    let mut cut = total - total.floor();
    while cut > 1e-9 {
        let (Some(a), Some(b)) = (yf.last_mut(), nf.last_mut()) else { break };
        let take = a.1.min(cut);
        a.1 -= take;
        b.1 -= take;
        cut -= take;
        if a.1 <= 1e-9 {
            yf.pop();
            nf.pop();
        }
    }
    let contracts = total.floor();
    if contracts < 1.0 {
        return None;
    }
    let (fee_yes, fee_no) = (yes.fees(&yf), no.fees(&nf));
    let cost = yf.iter().chain(nf.iter()).map(|(p, n)| p * n).sum::<f64>() + fee_yes + fee_no;
    Some(Walk { contracts, yes_fills: yf, no_fills: nf, cost, fee_yes, fee_no, profit: contracts - cost, first_return: first.unwrap_or(0.0), last_return: last })
}

fn avg(f: &[(f64, f64)]) -> f64 {
    let n: f64 = f.iter().map(|x| x.1).sum();
    if n > 0.0 { f.iter().map(|x| x.0 * x.1).sum::<f64>() / n } else { 0.0 }
}

fn distinct_levels(f: &[(f64, f64)]) -> usize {
    let mut p: Vec<i64> = f.iter().map(|x| (x.0 * 10_000.0).round() as i64).collect();
    p.dedup();
    p.len()
}

fn scan(app: &App) -> anyhow::Result<()> {
    scan_with(app, None).map(|_| ())
}

/// Dollars this bot may put into a new position now (before the per-game cap).
fn budget(c: &Connection, real: bool) -> f64 {
    let s = bots::settings();
    let (_, open, _, _) = bots::arb_ledger(c, real);
    let room = (s.arb.c.max_open - open).max(0.0).min(s.arb.c.max_trade);
    if real {
        room.min(bots::real_free(c, ARB))
    } else {
        room.min(bots::paper_cash(c, ARB))
    }
}

/// One pass. `only` = a single game sent by hand (Order button): any locked profit above 0 counts,
/// and the 6-hour payout window does not apply; every other check does.
fn scan_with(app: &App, only: Option<i64>) -> anyhow::Result<(Vec<Pos>, HashMap<&'static str, u64>)> {
    let now = now_ms();
    let manual = only.is_some();
    let set = bots::settings();
    let mode = bots::mode(ARB);
    let c = crate::store::open_ro(&app.db_path)?;
    let mut gs = {
        let cached = {
            let st = ST.lock();
            (now - st.games_at < 5_000).then(|| st.games.clone())
        };
        match cached {
            Some(g) if !manual => g,
            _ => {
                let g = games(&c, now)?;
                let mut st = ST.lock();
                st.games = g.clone();
                st.games_at = now;
                g
            }
        }
    };
    let mut rej: HashMap<&'static str, u64> = HashMap::new();
    let Some(real) = mode else {
        if manual {
            rej.insert("bot_stopped", 1);
            return Ok((Vec::new(), rej));
        }
        let mut st = ST.lock();
        st.scans += 1;
        st.last_scan_ms = now;
        st.watching = gs.len();
        return Ok((Vec::new(), rej));
    };
    let held: std::collections::HashSet<i64> = {
        let mut st = c.prepare_cached("SELECT DISTINCT match_id FROM pred_arbs WHERE json_extract(data, '$.payout_ms') > ?1")?;
        let it = st.query_map([now], |r| r.get::<_, i64>(0))?;
        it.filter_map(|x| x.ok()).collect()
    };
    if let Some(id) = only {
        gs.retain(|g| g.0 == id);
        if held.contains(&id) {
            rej.insert("already_bought", 1);
            return Ok((Vec::new(), rej));
        }
    }
    let mut pq = snipe::pm_quotes(&c);
    let mut kq = snipe::kalshi_quotes(&c);
    let mut cash = budget(&c, real);
    let mut new = Vec::new();
    let mut raw = 0u64;
    let mut notes: Vec<(i64, &'static str, String)> = Vec::new();
    let alive = now - venue::alive_ms(V::Pm) <= ALIVE_MS && now - venue::alive_ms(V::Kalshi) <= ALIVE_MS;
    for (mid, sport, home, away, live, start) in &gs {
        // Pays out within 6 hours: kick-off known, the game ends (kick-off + 3 h) inside the window.
        let payout_ms = start + GAME_LEN_MS;
        let pays_soon = *start > 0 && payout_ms <= now + PAYOUT_MS && payout_ms > now;
        let two_way = !crate::books::three_way(sport);
        let outs: &[Outcome] = if two_way { &[Outcome::Home, Outcome::Away] } else { &[Outcome::Home, Outcome::Away, Outcome::Draw] };
        let Some((kev, swap)) = kev(&c, *mid, now) else { continue };
        let min_p = if manual { 1e-6 } else if *live { set.arb.min_live } else { set.arb.min_pre };
        let pair = if manual { 1e-6 } else { set.arb.min_pair };
        // The game's best arbitrage (one position per game).
        let mut best: Option<Pos> = None;
        for &o in outs {
            let mut why = |r: &'static str| *rej.entry(r).or_default() += 1;
            // Both live books for this outcome (Kalshi in its own team order).
            let ko = venue::outcome_of(snipe::kalshi_side(o.as_str(), swap)).unwrap_or(o);
            let (Some((pm_tok, pl, pm_meta)), Some((k_tok, kl, k_meta))) = (venue::pm_contract(*mid, o), venue::kalshi_contract(&kev, ko)) else { continue };
            let top = |l: &[(f64, f64)]| l.first().map(|x| x.0);
            let (pb, pa, kb, ka) = (top(&pl.bids), top(&pl.asks), top(&kl.bids), top(&kl.asks));
            // Screen on the books' best prices with both venues' fees.
            let pm_unit = |x: f64| x + Fee::PM_SPORTS.raw(1.0, x);
            let k_unit = |x: f64| x + KALSHI_FEE.raw(1.0, x);
            // (Yes venue, Yes price, No price = 1 - the other venue's Yes bid)
            let dirs = [(V::Pm, pa, kb.map(|b| 1.0 - b)), (V::Kalshi, ka, pb.map(|b| 1.0 - b))];
            let gaps: Vec<V> = dirs
                .iter()
                .filter_map(|(v, y, n)| {
                    let (y, n) = ((*y)?, (*n)?);
                    let c = if *v == V::Pm { pm_unit(y) + k_unit(n) } else { k_unit(y) + pm_unit(n) };
                    ((0.01..0.99).contains(&y) && (0.01..0.99).contains(&n) && 1.0 / c - 1.0 >= min_p).then_some(*v)
                })
                .collect();
            if gaps.is_empty() {
                continue;
            }
            raw += 1;
            let fmt = |l: &Ladder| format!("{:?}/{:?}", top(&l.bids), top(&l.asks));
            if gaps.len() == 2 {
                why("both_directions");
                notes.push((*mid, "both_directions", format!("{}: polymarket book {} | kalshi book {}", o.as_str(), fmt(&pl), fmt(&kl))));
                continue;
            }
            // Cross-check against the latest recorded quotes (a book that missed an update, or a
            // team-order mix-up, disagrees with them).
            let (Some(p), Some(k)) = (pq.at(*mid, o.as_str(), now), kq.at(*mid, o.as_str(), now)) else {
                why("no_recorded_quote");
                continue;
            };
            if p.3 == "assumed" || k.3 == "assumed" {
                why("estimated_quote");
                continue;
            }
            let near = |a: Option<f64>, b: f64| a.is_some_and(|a| (a - b).abs() < 0.0101);
            if !(near(pb, p.0) && near(pa, p.1) && near(kb, k.0) && near(ka, k.1)) {
                why("book_mismatch");
                notes.push((*mid, "book_mismatch", format!("{}: polymarket quote {:.3}/{:.3} book {} | kalshi quote {:.3}/{:.3} book {}", o.as_str(), p.0, p.1, fmt(&pl), k.0, k.1, fmt(&kl))));
                continue;
            }
            if !alive {
                why("socket_quiet");
                continue;
            }
            if !manual && !pays_soon {
                why("pays_out_later");
                continue;
            }
            if !*live && set.arb.live_only && !manual {
                why("pre_match_off");
                continue;
            }
            if !manual && held.contains(mid) {
                why("already_holding");
                continue;
            }
            let money = cash.min(bots::game_room(&c, *mid));
            if money < snipe::MIN_FILL {
                why("no_cash");
                continue;
            }
            let pm_yes = gaps[0] == V::Pm;
            let Some(pm_leg_token) = (if pm_yes { Some(pm_tok.clone()) } else { pm_meta.complement.clone() }) else {
                why("no_complement_token");
                continue;
            };
            let pm_fee = pm_meta.fee.unwrap_or(Fee::PM_SPORTS);
            let legs = |pl: &Ladder, kl: &Ladder| if pm_yes { (Leg::yes(V::Pm, pl, pm_fee), Leg::no(V::Kalshi, kl, KALSHI_FEE)) } else { (Leg::yes(V::Kalshi, kl, KALSHI_FEE), Leg::no(V::Pm, pl, pm_fee)) };
            let (yes, no) = legs(&pl, &kl);
            let Some(w) = walk(&yes, &no, money, pair) else {
                why("gap_gone_after_fees");
                continue;
            };
            if w.cost < snipe::MIN_FILL {
                why("too_thin");
                continue;
            }
            if w.profit / w.cost < min_p {
                why("too_little_profit");
                continue;
            }
            // Settlement.
            if pm_meta.rules.is_empty() || k_meta.rules.is_empty() {
                why("settle_rules_missing");
                continue;
            }
            let (ps, ks) = (settle::schedule(&pm_meta.rules), settle::schedule(&k_meta.rules));
            if !settle::same_schedule(&ps, &ks) {
                why("settle_different_game");
                continue;
            }
            let rule = match settle::result_rule(sport, &pm_meta.title, &pm_meta.rules, &k_meta.rules) {
                Ok(r) => r,
                Err(r) => {
                    why(r);
                    continue;
                }
            };
            let mut chk = settle::Check {
                result_rule: rule,
                postpone_days: (settle::postpone_days(V::Pm, &pm_meta.rules, &ps), settle::postpone_days(V::Kalshi, &k_meta.rules, &ks)),
                pm_schedule: ps,
                kalshi_schedule: ks,
                ..Default::default()
            };
            let first_walk = (w.contracts, w.profit);
            let (w, pl, kl) = if real {
                // Real: read both books again after the orders' travel; the real orders follow.
                std::thread::sleep(Duration::from_millis(ORDER_MS));
                let (Some((_, pl, _)), Some((_, kl, _))) = (venue::pm_contract(*mid, o), venue::kalshi_contract(&kev, ko)) else {
                    why("gone_at_order");
                    continue;
                };
                let (yes, no) = legs(&pl, &kl);
                let Some(w) = walk(&yes, &no, money, pair).filter(|w| w.cost >= snipe::MIN_FILL && w.profit / w.cost >= min_p) else {
                    why("gone_at_order");
                    notes.push((*mid, "gone_at_order", format!("{}: {:.0} contracts +${:.2} at the scan, gone {} ms later", o.as_str(), first_walk.0, first_walk.1, ORDER_MS)));
                    continue;
                };
                (w, pl, kl)
            } else {
                // Paper: the two legs as real orders would fill them (measured timings).
                match paper_legs(*mid, o, &kev, ko, pm_yes, pm_fee, &w, *live) {
                    PaperLegs::Filled(w2, pl, kl) => (w2, pl, kl),
                    PaperLegs::Gone(msg) => {
                        why("gone_at_order");
                        notes.push((*mid, "gone_at_order", format!("{}: {msg}", o.as_str())));
                        continue;
                    }
                    PaperLegs::Failed { n, cost, value } => {
                        why("leg_failed");
                        notes.push((*mid, "leg_failed", format!("{}: Kalshi gone after the Polymarket leg; {n:.0} sold back, {:+.2}", o.as_str(), value - cost)));
                        let now2 = now_ms();
                        new.push(Pos {
                            ts: now2, match_id: *mid, sport: sport.clone(), home: home.clone(), away: away.clone(), outcome: o.as_str().into(),
                            yes_on: if pm_yes { "polymarket" } else { "kalshi" }, yes: 0.0, no: 0.0, yes_worst: 0.0, no_worst: 0.0, levels: (0, 0),
                            contracts: n, cost, fees: 0.0, pm_fee: 0.0, kalshi_fee: 0.0, profit: value - cost, first_return: w.first_return, last_return: w.last_return,
                            depth_curve: vec![], scan_walk: first_walk, book_age_ms: (0, 0), settle: chk.clone(), live: *live, payout_ms: now2, manual, real: false,
                            pm_token: pm_leg_token.clone(), kalshi_ticker: k_tok.clone(), kalshi_yes: !pm_yes, leg_order: "polymarket first",
                            leg_failed: true, closed_ms: Some(now2), pnl: Some(value - cost), close_reason: Some("leg failed: Kalshi price gone, Polymarket sold back"),
                        });
                        continue;
                    }
                }
            };
            if !*live {
                let kmid = kl.bids.first().zip(kl.asks.first()).map(|(b, a)| (b.0 + a.0) / 2.0);
                let pay = settle::void_pay(V::Pm, &pm_meta.rules, two_way, pm_yes, None).zip(settle::void_pay(V::Kalshi, &k_meta.rules, two_way, !pm_yes, kmid));
                let Some((pp, kp)) = pay else {
                    why("settle_void_unknown");
                    continue;
                };
                let value = w.contracts * (pp + kp);
                chk.void_value = Some(value);
                chk.void_pnl = Some(value - w.cost);
                if value - w.cost < -settle::VOID_MAX_LOSS * w.cost {
                    why("settle_void_loss");
                    continue;
                }
            }
            let curve = CURVE.iter().filter_map(|s| walk(&yes, &no, *s, pair).map(|x| (*s, x.contracts, x.profit))).collect();
            let (pm_fee_paid, k_fee_paid) = if pm_yes { (w.fee_yes, w.fee_no) } else { (w.fee_no, w.fee_yes) };
            let pos = Pos {
                ts: now,
                match_id: *mid,
                sport: sport.clone(),
                home: home.clone(),
                away: away.clone(),
                outcome: o.as_str().into(),
                yes_on: if pm_yes { "polymarket" } else { "kalshi" },
                yes: avg(&w.yes_fills),
                no: avg(&w.no_fills),
                yes_worst: w.yes_fills.last().map_or(0.0, |x| x.0),
                no_worst: w.no_fills.last().map_or(0.0, |x| x.0),
                levels: (distinct_levels(&w.yes_fills), distinct_levels(&w.no_fills)),
                contracts: w.contracts,
                cost: w.cost,
                fees: w.fee_yes + w.fee_no,
                pm_fee: pm_fee_paid,
                kalshi_fee: k_fee_paid,
                profit: w.profit,
                first_return: w.first_return,
                last_return: w.last_return,
                depth_curve: curve,
                scan_walk: first_walk,
                book_age_ms: (now - pl.ts, now - kl.ts),
                settle: chk,
                live: *live,
                payout_ms,
                manual,
                real,
                pm_token: pm_leg_token,
                kalshi_ticker: k_tok.clone(),
                kalshi_yes: !pm_yes,
                leg_order: "polymarket first",
                leg_failed: false,
                closed_ms: None,
                pnl: None,
                close_reason: None,
            };
            if best.as_ref().map_or(true, |b| pos.profit > b.profit) {
                best = Some(pos);
            }
        }
        if let Some(b) = best {
            let b = if real {
                match execute_real(app, b) {
                    Ok(x) => x,
                    Err(e) => {
                        bots::event(format!("arb: real arbitrage not taken: {e:#}"));
                        *rej.entry("real_failed").or_default() += 1;
                        continue;
                    }
                }
            } else {
                b
            };
            cash -= b.cost;
            new.push(b);
        }
    }
    if !new.is_empty() {
        let w = Connection::open(&app.db_path)?;
        w.busy_timeout(Duration::from_secs(5))?;
        for p in &new {
            w.execute("INSERT INTO pred_arbs(ts_ms, match_id, data) VALUES (?1, ?2, ?3)", params![p.ts, p.match_id, serde_json::to_string(p)?])?;
            tracing::info!("predarb: {} arb {} v {} {} yes on {} {:.1}% ({} + {} levels, fees ${:.2})", if p.real { "REAL" } else { "paper" }, p.home, p.away, p.outcome, p.yes_on, 100.0 * p.profit / p.cost, p.levels.0, p.levels.1, p.fees);
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
    for (m, r, d) in notes {
        st.last.insert(m, (now, r, d));
    }
    st.last.retain(|_, v| now - v.0 < 30 * 60_000);
    Ok((new, HashMap::new()))
}

enum PaperLegs {
    Filled(Walk, Ladder, Ladder),
    Gone(String),
    /// The Polymarket leg filled, Kalshi did not: n shares bought for `cost`, sold back for `value`.
    Failed { n: f64, cost: f64, value: f64 },
}

/// Take up to `n` from `levels` (best first) at prices no worse than `limit` (buying: <= limit).
fn take_upto(levels: &[(f64, f64)], limit: f64, n: f64) -> Vec<(f64, f64)> {
    let (mut left, mut out) = (n, Vec::new());
    for (p, q) in levels {
        if left <= 1e-9 || *p > limit + 1e-9 {
            break;
        }
        let k = q.min(left);
        out.push((*p, k));
        left -= k;
    }
    out
}

/// Paper legs as real orders fill them, with the measured timings (timing.rs): the Polymarket leg
/// (limit = the walk's worst Polymarket price) matches after its reply time plus, on a game
/// playing, the live-sports delay, against the book THEN; the Kalshi leg follows for what filled,
/// all or none at its walk's worst price, against the Kalshi book after its order time. If Kalshi
/// no longer fills, the Polymarket shares are sold back once settled (2-5c under the bid), which
/// is the loss a real leg failure costs.
fn paper_legs(mid: i64, o: Outcome, kev: &str, ko: Outcome, pm_yes: bool, pm_fee: Fee, w: &Walk, live: bool) -> PaperLegs {
    let t = crate::timing::get();
    let (pm_limit, k_limit) = if pm_yes {
        (w.yes_fills.last().map_or(0.0, |x| x.0), w.no_fills.last().map_or(0.0, |x| x.0))
    } else {
        (w.no_fills.last().map_or(0.0, |x| x.0), w.yes_fills.last().map_or(0.0, |x| x.0))
    };
    std::thread::sleep(Duration::from_millis(t.pm_fill_ms(live).max(0) as u64));
    let Some((_, pl, _)) = venue::pm_contract(mid, o) else { return PaperLegs::Gone("Polymarket book gone".into()) };
    let pm_levels = if pm_yes { Leg::yes(V::Pm, &pl, pm_fee).levels } else { Leg::no(V::Pm, &pl, pm_fee).levels };
    let pm_fills = take_upto(&pm_levels, pm_limit, w.contracts);
    let pm_n: f64 = pm_fills.iter().map(|x| x.1).sum::<f64>().floor();
    if pm_n < 1.0 {
        return PaperLegs::Gone(format!("Polymarket moved past {:.1}c by the match ({} ms)", pm_limit * 100.0, t.pm_fill_ms(live)));
    }
    let pm_fills = take_upto(&pm_levels, pm_limit, pm_n);
    std::thread::sleep(Duration::from_millis(t.kalshi_order_ms.max(0) as u64));
    let k_now = venue::kalshi_contract(kev, ko);
    let k_fills = k_now.as_ref().map(|(_, kl, _)| take_upto(&if pm_yes { Leg::no(V::Kalshi, kl, KALSHI_FEE).levels } else { Leg::yes(V::Kalshi, kl, KALSHI_FEE).levels }, k_limit, pm_n)).unwrap_or_default();
    let k_n: f64 = k_fills.iter().map(|x| x.1).sum();
    let pm_leg = Leg { v: V::Pm, levels: vec![], fee: pm_fee };
    let k_leg = Leg { v: V::Kalshi, levels: vec![], fee: KALSHI_FEE };
    let pm_cost = pm_fills.iter().map(|(p, q)| p * q).sum::<f64>() + pm_leg.fees(&pm_fills);
    if k_n + 1e-9 < pm_n {
        // Leg failure: sell the Polymarket shares back once they settle and the sale matches.
        std::thread::sleep(Duration::from_millis((t.pm_settle_ms + t.pm_fill_ms(live)).max(0) as u64));
        let bids = venue::pm_contract(mid, o).map(|(_, l, _)| if pm_yes { l.bids.clone() } else { l.asks.iter().map(|(p, q)| (1.0 - p, *q)).collect() }).unwrap_or_default();
        let floor = bids.first().map_or(0.0, |x| x.0) - 0.05;
        let (mut left, mut got, mut sold) = (pm_n, 0.0, Vec::new());
        for (p, q) in &bids {
            if left <= 1e-9 || *p < floor {
                break;
            }
            let k = q.min(left);
            got += k * p;
            left -= k;
            sold.push((*p, k));
        }
        let value = got - pm_leg.fees(&sold);
        return PaperLegs::Failed { n: pm_n, cost: pm_cost, value };
    }
    let (fee_pm, fee_k) = (pm_leg.fees(&pm_fills), k_leg.fees(&k_fills));
    let cost = pm_fills.iter().chain(k_fills.iter()).map(|(p, q)| p * q).sum::<f64>() + fee_pm + fee_k;
    let (yes_fills, no_fills, fee_yes, fee_no) = if pm_yes { (pm_fills, k_fills, fee_pm, fee_k) } else { (k_fills, pm_fills, fee_k, fee_pm) };
    let w2 = Walk { contracts: pm_n, yes_fills, no_fills, cost, fee_yes, fee_no, profit: pm_n - cost, first_return: w.first_return, last_return: w.last_return };
    let kl = k_now.map(|x| x.1).unwrap_or_default();
    PaperLegs::Filled(w2, pl, kl)
}

/// Place a real arbitrage: Polymarket leg first (fill-and-kill at the walked worst price), then the
/// Kalshi leg for what filled (fill-or-kill at its walked worst price). If Kalshi does not fill,
/// the Polymarket shares are sold straight back. Returns the position priced on the real fills.
fn execute_real(app: &App, mut p: Pos) -> anyhow::Result<Pos> {
    let pm = crate::pmacct::load(&app.db_path);
    let kc = crate::kalacct::load(&app.db_path);
    let pm_yes = p.yes_on == "polymarket";
    let (pm_px, k_px) = if pm_yes { (p.yes_worst, p.no_worst) } else { (p.no_worst, p.yes_worst) };
    let fee = venue::pm_contract(p.match_id, venue::outcome_of(&p.outcome).unwrap_or(Outcome::Home)).and_then(|x| x.2.fee).unwrap_or(Fee::PM_SPORTS);
    let rt = tokio::runtime::Handle::current();
    rt.block_on(async {
        let http = crate::books::http_client();
        // Same accounts as the snipe bot: claim both venues (always Polymarket, then Kalshi) so the
        // two bots never spend the same dollars; held until both legs are sent.
        let mut pw = crate::exec::claim(V::Pm, &app.db_path, &http, p.contracts * pm_px * 1.02).await?;
        let mut kw = crate::exec::claim(V::Kalshi, &app.db_path, &http, p.contracts * (k_px + 0.02)).await?;
        let f1 = crate::exec::pm_order(&http, &pm, &p.pm_token, true, pm_px, p.contracts, false).await;
        if let Ok(x) = &f1 {
            pw.spent += x.filled * x.avg * 1.02;
        }
        let f1 = match f1 {
            Ok(x) => {
                bots::log_order(app, ARB, "polymarket", x.filled >= 1.0, &json!({"side": "buy", "token": p.pm_token, "limit": pm_px, "shares": p.contracts, "fill": x, "error": if x.filled < 1.0 { "nothing filled" } else { "" }}));
                x
            }
            Err(e) => {
                bots::log_order(app, ARB, "polymarket", false, &json!({"side": "buy", "token": p.pm_token, "limit": pm_px, "shares": p.contracts, "error": format!("{e:#}")}));
                anyhow::bail!("Polymarket leg: {e:#}");
            }
        };
        let got = f1.filled.floor();
        anyhow::ensure!(got >= 1.0, "Polymarket leg did not fill");
        let f2 = crate::exec::kalshi_order(&http, &kc, &p.kalshi_ticker, true, p.kalshi_yes, k_px, got, true).await;
        let ok2 = matches!(&f2, Ok(x) if x.filled >= got);
        if let Ok(x) = &f2 {
            kw.spent += x.filled * (x.avg + 0.02);
        }
        drop(kw);
        match &f2 {
            Ok(x) => bots::log_order(app, ARB, "kalshi", ok2, &json!({"side": "buy", "ticker": p.kalshi_ticker, "yes": p.kalshi_yes, "limit": k_px, "count": got, "fill": x, "error": if ok2 { "" } else { "not filled" }})),
            Err(e) => bots::log_order(app, ARB, "kalshi", false, &json!({"side": "buy", "ticker": p.kalshi_ticker, "yes": p.kalshi_yes, "limit": k_px, "count": got, "error": format!("{e:#}")})),
        }
        if !ok2 {
            // Unhedged: sell the Polymarket shares back now (up to 5c under what was paid).
            let back = crate::exec::pm_order(&http, &pm, &p.pm_token, false, (f1.avg - 0.05).max(0.01), got, false).await;
            let msg = match &back {
                Ok(x) => format!("sold back {} of {got} at {:.3}", x.filled, x.avg),
                Err(e) => format!("SELL-BACK FAILED, {got} Polymarket shares are unhedged: {e:#}"),
            };
            bots::log_order(app, ARB, "polymarket", back.as_ref().is_ok_and(|x| x.filled >= got), &json!({"side": "sell (leg failed)", "token": p.pm_token, "shares": got, "result": msg}));
            anyhow::bail!("Kalshi leg did not fill; {msg}");
        }
        let f2 = f2.unwrap();
        let (pm_avg, k_avg) = (f1.avg, f2.avg);
        let pm_f = venue::pm_fee(fee, got, pm_avg);
        let k_f = snipe::kalshi_fee(got, k_avg);
        p.contracts = got;
        if pm_yes {
            p.yes = pm_avg;
            p.no = k_avg;
        } else {
            p.yes = k_avg;
            p.no = pm_avg;
        }
        p.pm_fee = pm_f;
        p.kalshi_fee = k_f;
        p.fees = pm_f + k_f;
        p.cost = got * (pm_avg + k_avg) + p.fees;
        p.profit = got - p.cost;
        Ok(p)
    })
}

/// Walk `levels` (best first) selling `n`: (sold, proceeds before fees, fills).
fn sell_walk(levels: &[(f64, f64)], n: f64) -> (f64, f64, Vec<(f64, f64)>) {
    let (mut left, mut got, mut fills) = (n, 0.0, Vec::new());
    for (p, q) in levels {
        if left <= 1e-9 {
            break;
        }
        let k = q.min(left);
        got += k * p;
        left -= k;
        fills.push((*p, k));
    }
    (n - left, got, fills)
}

/// What an open position sells for right now on both books, fees paid: (value, yes avg, no avg,
/// pm sale price floor, kalshi sale price floor). None if either book is missing or too thin.
fn sale_value(c: &Connection, v: &Value, now: Ms) -> Option<(f64, f64, f64, f64, f64)> {
    let mid = v["match_id"].as_i64()?;
    let o = venue::outcome_of(v["outcome"].as_str()?)?;
    let n = v["contracts"].as_f64()?;
    let (kev, swap) = kev(c, mid, now)?;
    let ko = venue::outcome_of(snipe::kalshi_side(o.as_str(), swap)).unwrap_or(o);
    let (_, pl, pm_meta) = venue::pm_contract(mid, o)?;
    let (_, kl, _) = venue::kalshi_contract(&kev, ko)?;
    let pm_fee = pm_meta.fee.unwrap_or(Fee::PM_SPORTS);
    // Selling Yes hits that venue's Yes bids; selling No hits its No bids = 1 - Yes asks.
    let no_bids = |l: &Ladder| l.asks.iter().map(|(p, q)| (1.0 - p, *q)).collect::<Vec<_>>();
    let pm_yes = v["yes_on"].as_str() == Some("polymarket");
    let (pm_levels, k_levels) = if pm_yes { (pl.bids.clone(), no_bids(&kl)) } else { (no_bids(&pl), kl.bids.clone()) };
    let (s1, g1, f1) = sell_walk(&pm_levels, n);
    let (s2, g2, f2) = sell_walk(&k_levels, n);
    if s1 < n - 1e-9 || s2 < n - 1e-9 {
        return None;
    }
    let fees = f1.iter().map(|(p, q)| venue::pm_fee(pm_fee, *q, *p)).sum::<f64>() + snipe::kalshi_fee(n, g2 / n);
    let (pm_avg, k_avg) = (g1 / n, g2 / n);
    let (ya, na) = if pm_yes { (pm_avg, k_avg) } else { (k_avg, pm_avg) };
    Some((g1 + g2 - fees, ya, na, f1.last().map_or(pm_avg, |x| x.0), f2.last().map_or(k_avg, |x| x.0)))
}

/// Close one position at a sale value (paper: as priced; real: the two sell orders).
fn close_pos(app: &App, ts: Ms, match_id: i64, mut v: Value, why: &str) -> anyhow::Result<Value> {
    let now = now_ms();
    let c = crate::store::open_ro(&app.db_path)?;
    let (value, ys, ns, pm_floor, k_floor) = sale_value(&c, &v, now).ok_or_else(|| anyhow::anyhow!("no live book deep enough to sell both legs now"))?;
    let real = v["real"].as_bool().unwrap_or(false);
    let mut value = value;
    let (mut ys, mut ns) = (ys, ns);
    if !real {
        // Paper: the sale matches after Polymarket's reply time (+ the live-sports delay on a game
        // playing), all or none at the Polymarket floor seen now, as the real sale would.
        let live = crate::timing::live(&c, match_id, now);
        std::thread::sleep(Duration::from_millis(crate::timing::get().pm_fill_ms(live).max(0) as u64));
        let (v2, ys2, ns2, _, _) = sale_value(&c, &v, now_ms()).ok_or_else(|| anyhow::anyhow!("the books thinned out before the sale matched; position kept"))?;
        let pm_yes = v["yes_on"].as_str() == Some("polymarket");
        let pm_avg = if pm_yes { ys2 } else { ns2 };
        anyhow::ensure!(pm_avg + 1e-9 >= pm_floor, "Polymarket moved below the sale limit before it matched; position kept");
        value = v2;
        ys = ys2;
        ns = ns2;
    }
    if real {
        let pm = crate::pmacct::load(&app.db_path);
        let kc = crate::kalacct::load(&app.db_path);
        let n = v["contracts"].as_f64().unwrap_or(0.0);
        let tok = v["pm_token"].as_str().unwrap_or("").to_string();
        let tk = v["kalshi_ticker"].as_str().unwrap_or("").to_string();
        let kyes = v["kalshi_yes"].as_bool().unwrap_or(false);
        let rt = tokio::runtime::Handle::current();
        value = rt.block_on(async {
            let http = crate::books::http_client();
            // Both legs or neither first: Polymarket all-or-none, then Kalshi.
            let a = crate::exec::pm_order(&http, &pm, &tok, false, pm_floor, n, true).await;
            let a = match a {
                Ok(x) if x.filled >= n => {
                    bots::log_order(app, ARB, "polymarket", true, &json!({"side": "sell", "token": tok, "limit": pm_floor, "shares": n, "fill": x}));
                    x
                }
                other => {
                    bots::log_order(app, ARB, "polymarket", false, &json!({"side": "sell", "token": tok, "limit": pm_floor, "shares": n, "error": format!("{:?}", other.err())}));
                    anyhow::bail!("Polymarket sale did not fill; position kept");
                }
            };
            // Kalshi: up to three tries, each a little lower, so the position is not left one-legged.
            let mut left = n;
            let mut got_k = 0.0;
            for cut in [0.0, 0.02, 0.05] {
                if left < 1.0 {
                    break;
                }
                match crate::exec::kalshi_order(&http, &kc, &tk, false, kyes, (k_floor - cut).max(0.01), left, false).await {
                    Ok(x) => {
                        bots::log_order(app, ARB, "kalshi", x.filled > 0.0, &json!({"side": "sell", "ticker": tk, "count": left, "fill": x}));
                        got_k += x.filled * x.avg;
                        left -= x.filled;
                    }
                    Err(e) => bots::log_order(app, ARB, "kalshi", false, &json!({"side": "sell", "ticker": tk, "count": left, "error": format!("{e:#}")})),
                }
            }
            if left >= 1.0 {
                bots::event(format!("arb: {left} Kalshi contracts of match {match_id} could not be sold after the Polymarket leg was: UNHEDGED, sell them by hand"));
            }
            let sold_k = n - left;
            let fees = venue::pm_fee(Fee::PM_SPORTS, n, a.avg) + snipe::kalshi_fee(sold_k, if sold_k > 0.0 { got_k / sold_k } else { 0.0 });
            Ok::<f64, anyhow::Error>(a.filled * a.avg + got_k - fees)
        })?;
    }
    let pnl = value - v["cost"].as_f64().unwrap_or(0.0);
    v["closed_ms"] = json!(now);
    v["close_value"] = json!(value);
    v["pnl"] = json!(pnl);
    v["sold_yes"] = json!(ys);
    v["sold_no"] = json!(ns);
    v["payout_ms"] = json!(now);
    v["close_reason"] = json!(why);
    let w = Connection::open(&app.db_path)?;
    w.busy_timeout(Duration::from_secs(5))?;
    w.execute("UPDATE pred_arbs SET data = ?3 WHERE ts_ms = ?1 AND match_id = ?2", params![ts, match_id, serde_json::to_string(&v)?])?;
    tracing::info!("predarb: {why} {} v {} for {:.2} ({:+.2})", v["home"], v["away"], value, pnl);
    if real {
        bots::check_daily_loss(app, ARB);
    }
    Ok(v)
}

/// Sell open positions whose two legs fetch at least `unwind_at` of the locked profit now.
fn unwind_pass(app: &App) -> anyhow::Result<()> {
    let share = bots::settings().arb.unwind_at;
    if share <= 0.0 {
        return Ok(());
    }
    let now = now_ms();
    let c = crate::store::open_ro(&app.db_path)?;
    let rows: Vec<(Ms, i64, String)> = {
        let mut st = c.prepare_cached("SELECT ts_ms, match_id, data FROM pred_arbs WHERE json_extract(data, '$.payout_ms') > ?1 AND json_extract(data, '$.closed_ms') IS NULL")?;
        let it = st.query_map([now], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
        it.filter_map(|x| x.ok()).collect()
    };
    for (ts, mid, d) in rows {
        let Ok(v) = serde_json::from_str::<Value>(&d) else { continue };
        let Some((value, ..)) = sale_value(&c, &v, now) else { continue };
        let cost = v["cost"].as_f64().unwrap_or(0.0);
        let locked = v["contracts"].as_f64().unwrap_or(0.0) - cost;
        if value > cost && value - cost >= share * locked - 1e-9 {
            match close_pos(app, ts, mid, v, "auto-unwind") {
                Ok(_) => ST.lock().unwound += 1,
                Err(e) => tracing::warn!("predarb unwind {mid}: {e:#}"),
            }
        }
    }
    Ok(())
}

/// Order button: re-price one game from the latest Polymarket and Kalshi books and take the
/// arbitrage if it is still there (paper or real, as the bot is set).
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
    let paper: Vec<&Value> = rows.iter().filter(|r| !r["real"].as_bool().unwrap_or(false)).collect();
    let (n, cost, profit) = paper.iter().fold((0, 0.0, 0.0), |a, r| (a.0 + 1, a.1 + r["cost"].as_f64().unwrap_or(0.0), a.2 + r["pnl"].as_f64().or(r["profit"].as_f64()).unwrap_or(0.0)));
    let st = ST.lock();
    let now = now_ms();
    let open = rows.iter().filter(|r| r["payout_ms"].as_i64().unwrap_or(0) > now).count();
    let s = bots::settings();
    json!({"open": open, "cash": crate::store::open_ro(&app.db_path).map(|c| bots::paper_cash(&c, ARB)).unwrap_or(0.0), "min_pair": s.arb.min_pair, "min_live": s.arb.min_live, "min_pre": s.arb.min_pre,
        "trades": n, "staked": cost, "locked_profit": profit, "scans": st.scans, "last_scan_ms": st.last_scan_ms, "unwound": st.unwound,
        "watching": st.watching, "raw_gaps": st.raw_gaps, "rejected": st.rejected,
        "last_rejects": st.last.iter().map(|(m, (t, r, d))| json!({"match_id": m, "ts": t, "reason": r, "detail": d})).collect::<Vec<_>>(), "recent": rows})
}

/// Sell an open arbitrage now (Sell button): both legs into the live books' bids, both venues'
/// fees; paper or real as the position was taken.
pub fn sell(app: &App, ts: Ms, match_id: i64) -> Value {
    let r = (|| -> anyhow::Result<Value> {
        let now = now_ms();
        let c = crate::store::open_ro(&app.db_path)?;
        let data: String = c.query_row("SELECT data FROM pred_arbs WHERE ts_ms = ?1 AND match_id = ?2", params![ts, match_id], |r| r.get(0))?;
        let v: Value = serde_json::from_str(&data)?;
        anyhow::ensure!(v["payout_ms"].as_i64().unwrap_or(0) > now && v["closed_ms"].is_null(), "this position is already closed");
        Ok(json!({"ok": true, "position": close_pos(app, ts, match_id, v, "sold by hand")?}))
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

#[cfg(test)]
mod tests {
    use super::*;

    fn ladder(bids: &[(f64, f64)], asks: &[(f64, f64)]) -> Ladder {
        Ladder { bids: bids.to_vec(), asks: asks.to_vec(), ts: 0 }
    }

    #[test]
    fn walks_both_books_until_the_next_pair_stops_paying() {
        // Polymarket Yes asks 45c x100, 56c x100; Kalshi Yes bids 53c x100 (No 47c), 47c x100 (No 53c).
        let pm = ladder(&[], &[(0.45, 100.0), (0.56, 100.0)]);
        let k = ladder(&[(0.53, 100.0), (0.47, 100.0)], &[]);
        let (y, n) = (Leg::yes(V::Pm, &pm, Fee::PM_SPORTS), Leg::no(V::Kalshi, &k, KALSHI_FEE));
        let w = walk(&y, &n, 10_000.0, 0.01).unwrap();
        // Pair 1: 45 + 47 + fees (1.24c + 1.74c) = 94.98c -> +5.3%. Pair 2: 56 + 53 > $1: not taken.
        assert_eq!(w.contracts, 100.0);
        assert!((w.fee_yes - 1.2375).abs() < 1e-9, "pm fee {}", w.fee_yes);
        assert!((w.fee_no - 1.75).abs() < 1e-9, "kalshi fee {}", w.fee_no);
        assert!((w.cost - (92.0 + 1.2375 + 1.75)).abs() < 1e-9);
        assert!(w.profit > 4.9 && w.profit < 5.1);
        // A smaller budget stops at whole contracts.
        let s = walk(&y, &n, 50.0, 0.01).unwrap();
        assert_eq!(s.contracts, 52.0);
        // No gap at all: nothing.
        let flat = ladder(&[(0.40, 100.0)], &[]);
        assert!(walk(&y, &Leg::no(V::Kalshi, &flat, KALSHI_FEE), 1_000.0, 0.01).is_none());
    }

    #[test]
    fn takes_deeper_levels_while_they_still_pay() {
        let pm = ladder(&[], &[(0.40, 50.0), (0.42, 50.0), (0.53, 500.0)]);
        let k = ladder(&[(0.55, 500.0)], &[]);
        let w = walk(&Leg::yes(V::Pm, &pm, Fee::PM_SPORTS), &Leg::no(V::Kalshi, &k, KALSHI_FEE), 10_000.0, 0.01).unwrap();
        // 40c and 42c pay (+12% / +10%); 53c + 45c + fees costs over $1.
        assert_eq!(w.contracts, 100.0);
        assert!(w.first_return > w.last_return && w.last_return >= 0.01);
        assert_eq!(distinct_levels(&w.yes_fills), 2);
    }
}
