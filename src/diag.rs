//! Test runs and diagnostics (Bots page). One click = the smallest REAL round trip a bot can make,
//! timed at every step, so delays and slippage are measured on the real venues before a bot trades
//! real money on its own. Nothing here runs by itself.
//!
//! * Arbitrage test: the Polymarket x Kalshi pair closest to $1 right now (live games first), at
//!   the venues' smallest size (Polymarket's minimum order, at least $1; the same count on Kalshi).
//!   Buys the Polymarket leg, then the Kalshi leg (as the bot does), then sells both straight back.
//! * Snipe test: the tightest liquid Polymarket market right now; buys the smallest order at the
//!   best ask and sells it straight back.
//! Cost: spread + fees on a few dollars. Each test is stored in `bot_tests` with: book ages and the
//! socket's silence at the decision, each order's preparation time, request latency (send ->
//! venue reply), the gap between the two legs, expected vs filled price (slippage) and the net
//! result. Orders go through the same account claim as the bots (bankroll aside: tests are tiny).

use crate::bots;
use crate::exec;
use crate::model::*;
use crate::snipe;
use crate::venue::{self, Fee, Ladder, V};
use crate::App;
use anyhow::{bail, Result};
use rusqlite::{params, Connection};
use serde_json::{json, Value};
use std::sync::Arc;

const KFEE: Fee = Fee { rate: 0.07, exponent: 1.0 };

fn top(l: &[(f64, f64)]) -> Option<(f64, f64)> {
    l.first().copied()
}

fn leg_json(venue: &str, side: &str, id: &str, expected: f64, limit: f64, want: f64, r: &Result<exec::Fill>, t_decide: Ms) -> Value {
    match r {
        Ok(f) => json!({"venue": venue, "side": side, "id": id, "expected": expected, "limit": limit, "wanted": want,
            "filled": f.filled, "avg": f.avg, "slippage": if f.filled > 0.0 { if side.starts_with("buy") { f.avg - expected } else { expected - f.avg } } else { 0.0 },
            "prep_ms": f.prep_ms, "latency_ms": f.latency_ms, "match_delay_ms": f.match_delay_ms, "settle_wait_ms": f.settle_wait_ms, "venue_fee_bps": f.fee_bps, "sent_after_decision_ms": f.sent_ms - t_decide, "status": f.status, "order_id": f.order_id}),
        Err(e) => json!({"venue": venue, "side": side, "id": id, "expected": expected, "limit": limit, "wanted": want, "filled": 0.0, "error": format!("{e:#}")}),
    }
}

fn save(app: &App, bot: &str, v: &Value) {
    if let Ok(c) = Connection::open(&app.db_path) {
        let _ = c.busy_timeout(std::time::Duration::from_secs(5));
        let _ = c.execute("CREATE TABLE IF NOT EXISTS bot_tests(id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, bot TEXT NOT NULL, data TEXT NOT NULL)", []);
        let _ = c.execute("INSERT INTO bot_tests(ts_ms, bot, data) VALUES (?1, ?2, ?3)", params![now_ms(), bot, v.to_string()]);
    }
}

/// Run one test. `bot` = "arb" or "snipe".
pub async fn run(app: Arc<App>, bot: &str) -> Result<Value> {
    if bots::settings().kill {
        bail!("the kill switch is on");
    }
    let r = match bot {
        bots::ARB => arb_test(&app).await,
        bots::SNIPE => snipe_test(&app).await,
        _ => bail!("unknown bot"),
    };
    let v = match r {
        Ok(v) => v,
        Err(e) => json!({"ok": false, "error": format!("{e:#}"), "ts": now_ms()}),
    };
    save(&app, bot, &v);
    bots::event(format!("{bot}: test run {}", if v["ok"].as_bool() == Some(true) { format!("done, net {:+.2}", v["net"].as_f64().unwrap_or(0.0)) } else { format!("failed: {}", v["error"].as_str().unwrap_or("")) }));
    Ok(v)
}

/// Shares for the smallest order: the market minimum, and at least $1 at this price.
fn min_shares(min_size: f64, px: f64) -> f64 {
    min_size.max((1.0 / px.max(0.01)).ceil()).ceil()
}

async fn snipe_test(app: &Arc<App>) -> Result<Value> {
    let http = crate::books::http_client();
    let pm = crate::pmacct::load(&app.db_path);
    let now = now_ms();
    // The tightest liquid market: spread <= 2c, prices 15-85c, freshest book; live games first.
    let live: std::collections::HashSet<i64> = {
        let c = crate::store::open_ro(&app.db_path)?;
        let mut st = c.prepare("SELECT id FROM matches WHERE start_ms > 0 AND start_ms <= ?1 AND start_ms > ?1 - 4 * 3600000")?;
        let v = st.query_map([now], |r| r.get::<_, i64>(0))?.filter_map(|x| x.ok()).collect();
        v
    };
    let mut best: Option<(bool, f64, i64, i64, Outcome, String, Ladder)> = None;
    for (m, o, (tok, l, _)) in venue::pm_all() {
        let (Some(b), Some(a)) = (top(&l.bids), top(&l.asks)) else { continue };
        let spread = a.0 - b.0;
        if spread > 0.02 + 1e-9 || !(0.15..=0.85).contains(&a.0) || a.1 < 10.0 || b.1 < 10.0 {
            continue;
        }
        let key = (live.contains(&m), -spread, -(now - l.ts));
        if best.as_ref().map_or(true, |x| (x.0, -x.1, -x.2) < key) {
            best = Some((live.contains(&m), spread, now - l.ts, m, o, tok, l));
        }
    }
    let Some((is_live, spread, book_age, m, o, tok, l)) = best else { bail!("no liquid Polymarket market with a 2c spread right now") };
    let facts = exec::token_facts(&http, &tok).await?;
    let (ask, bid) = (top(&l.asks).unwrap().0, top(&l.bids).unwrap().0);
    let n = min_shares(facts.min_size, ask);
    let t_decide = now_ms();
    let socket_quiet = t_decide - venue::alive_ms(V::Pm);
    let mut w = exec::claim(V::Pm, &app.db_path, &http, n * ask * 1.03).await?;
    let t_claimed = now_ms();
    let buy = exec::pm_order(&http, &pm, &tok, true, ask, n, false).await;
    if let Ok(x) = &buy {
        w.spent += x.filled * x.avg;
    }
    drop(w);
    bots::log_order(app, "snipe-test", "polymarket", buy.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "buy", "token": tok, "limit": ask, "shares": n, "fill": buy.as_ref().ok(), "error": buy.as_ref().err().map(|e| format!("{e:#}"))}));
    let got = buy.as_ref().map(|x| x.filled.floor()).unwrap_or(0.0);
    let mut legs = vec![leg_json("polymarket", "buy", &tok, ask, ask, n, &buy, t_decide)];
    let (mut net, mut sell_px) = (0.0, None);
    let fee = venue::pm_contract(m, o).and_then(|x| x.2.fee).unwrap_or(Fee::PM_SPORTS);
    if got >= 1.0 {
        let paid = buy.as_ref().map(|x| x.avg).unwrap_or(ask);
        let bid_now = venue::pm_contract(m, o).and_then(|x| top(&x.1.bids)).map(|x| x.0).unwrap_or(bid);
        let limit = (bid_now - 0.02).max(0.01);
        let t_sell = now_ms();
        let sell = exec::pm_order(&http, &pm, &tok, false, limit, got, false).await;
        bots::log_order(app, "snipe-test", "polymarket", sell.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "sell", "token": tok, "limit": limit, "shares": got, "fill": sell.as_ref().ok(), "error": sell.as_ref().err().map(|e| format!("{e:#}"))}));
        legs.push(leg_json("polymarket", "sell", &tok, bid_now, limit, got, &sell, t_sell));
        if let Ok(x) = &sell {
            sell_px = Some(x.avg);
            net = x.filled * x.avg - venue::pm_fee(fee, x.filled, x.avg) - got * paid - venue::pm_fee(fee, got, paid);
        }
    }
    let sold = legs.iter().filter(|l| l["side"] == "sell").map(|l| l["filled"].as_f64().unwrap_or(0.0)).sum::<f64>();
    let complete = got >= 1.0 && sold + 0.5 >= got;
    Ok(json!({"ok": complete, "kind": "snipe", "ts": t_decide, "match_id": m, "outcome": o.as_str(), "live": is_live,
        "market": {"spread": spread, "book_age_ms": book_age, "socket_quiet_ms": socket_quiet, "min_size": facts.min_size, "fee_bps": facts.fee_bps},
        "claim_ms": t_claimed - t_decide, "legs": legs, "shares": got, "sold_at": sell_px, "net": net,
        "error": if got < 1.0 { Some("the buy did not fill") } else if !complete { Some("bought, but not all sold back: use Sell back") } else { None }}))
}

async fn arb_test(app: &Arc<App>) -> Result<Value> {
    let http = crate::books::http_client();
    let pm = crate::pmacct::load(&app.db_path);
    let kc = crate::kalacct::load(&app.db_path);
    let now = now_ms();
    let c = crate::store::open_ro(&app.db_path)?;
    let games: Vec<(i64, String, Ms)> = c
        .prepare(
            "SELECT m.id, m.sport, COALESCE(m.start_ms, 0) FROM matches m
             WHERE EXISTS (SELECT 1 FROM book_events p WHERE p.match_id = m.id AND p.book = 'polymarket' AND p.last_seen_ms > ?1)
               AND EXISTS (SELECT 1 FROM book_events k WHERE k.match_id = m.id AND k.book = 'kalshi' AND k.last_seen_ms > ?1)",
        )?
        .query_map([now - 300_000], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .filter_map(|x| x.ok())
        .collect();
    // The pair closest to $1 (both fees) with enough at both best prices; live games first.
    struct Pick {
        live: bool,
        unit: f64,
        m: i64,
        o: Outcome,
        pm_yes: bool,
        pm_tok: String,
        pm_px: f64,
        k_tok: String,
        k_px: f64,
        ages: (Ms, Ms),
    }
    let mut best: Option<Pick> = None;
    for (m, sport, start) in &games {
        let Some((ev, swap)) = snipe::kalshi_event(&c, *m) else { continue };
        let outs: &[Outcome] = if crate::books::three_way(sport) { &[Outcome::Home, Outcome::Away, Outcome::Draw] } else { &[Outcome::Home, Outcome::Away] };
        for &o in outs {
            let ko = venue::outcome_of(snipe::kalshi_side(o.as_str(), swap)).unwrap_or(o);
            let (Some((ptok, pl, pmeta)), Some((ktok, kl, _))) = (venue::pm_contract(*m, o), venue::kalshi_contract(&ev, ko)) else { continue };
            let (Some(pa), Some(pb), Some(ka), Some(kb)) = (top(&pl.asks), top(&pl.bids), top(&kl.asks), top(&kl.bids)) else { continue };
            if pb.0 >= pa.0 || kb.0 >= ka.0 {
                continue;
            }
            let pf = pmeta.fee.unwrap_or(Fee::PM_SPORTS);
            // Yes on PM + No on Kalshi, or Yes on Kalshi + No on PM (the complement token).
            let dirs = [(true, pa.0, pa.1, 1.0 - kb.0, kb.1), (false, 1.0 - pb.0, pb.1, ka.0, ka.1)];
            for (pm_yes, ppx, psz, kpx, ksz) in dirs {
                if !(0.05..=0.95).contains(&ppx) || !(0.05..=0.95).contains(&kpx) || psz < 10.0 || ksz < 10.0 {
                    continue;
                }
                let tok = if pm_yes { ptok.clone() } else { match pmeta.complement.clone() { Some(t) => t, None => continue } };
                let unit = ppx + pf.raw(1.0, ppx) + kpx + KFEE.raw(1.0, kpx);
                let live = *start > 0 && *start <= now;
                if best.as_ref().map_or(true, |b| (live, -unit) > (b.live, -b.unit)) {
                    best = Some(Pick { live, unit, m: *m, o, pm_yes, pm_tok: tok, pm_px: ppx, k_tok: ktok.clone(), k_px: kpx, ages: (now - pl.ts, now - kl.ts) });
                }
            }
        }
    }
    let Some(p) = best else { bail!("no game with both books and 10+ at the best prices right now") };
    let facts = exec::token_facts(&http, &p.pm_tok).await?;
    let n = min_shares(facts.min_size, p.pm_px);
    let t_decide = now_ms();
    let quiet = (t_decide - venue::alive_ms(V::Pm), t_decide - venue::alive_ms(V::Kalshi));
    let mut pw = exec::claim(V::Pm, &app.db_path, &http, n * p.pm_px * 1.03).await?;
    let mut kw = exec::claim(V::Kalshi, &app.db_path, &http, n * (p.k_px + 0.03)).await?;
    let t_claimed = now_ms();
    let b1 = exec::pm_order(&http, &pm, &p.pm_tok, true, p.pm_px + 0.01, n, false).await;
    if let Ok(x) = &b1 {
        pw.spent += x.filled * x.avg;
    }
    bots::log_order(app, "arb-test", "polymarket", b1.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "buy", "token": p.pm_tok, "limit": p.pm_px + 0.01, "shares": n, "fill": b1.as_ref().ok(), "error": b1.as_ref().err().map(|e| format!("{e:#}"))}));
    let got = b1.as_ref().map(|x| x.filled.floor()).unwrap_or(0.0);
    let mut legs = vec![leg_json("polymarket", "buy", &p.pm_tok, p.pm_px, p.pm_px + 0.01, n, &b1, t_decide)];
    let t_pm_done = now_ms();
    let mut k_got = 0.0;
    let mut gap_ms = None;
    if got >= 1.0 {
        let b2 = exec::kalshi_order(&http, &kc, &p.k_tok, true, !p.pm_yes, p.k_px + 0.01, got, true).await;
        if let Ok(x) = &b2 {
            kw.spent += x.filled * x.avg;
            gap_ms = Some(x.sent_ms - t_pm_done);
        }
        bots::log_order(app, "arb-test", "kalshi", b2.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "buy", "ticker": p.k_tok, "yes": !p.pm_yes, "limit": p.k_px + 0.01, "count": got, "fill": b2.as_ref().ok(), "error": b2.as_ref().err().map(|e| format!("{e:#}"))}));
        k_got = b2.as_ref().map(|x| x.filled).unwrap_or(0.0);
        legs.push(leg_json("kalshi", "buy", &p.k_tok, p.k_px, p.k_px + 0.01, got, &b2, t_decide));
    }
    drop(pw);
    drop(kw);
    // Sell straight back whatever was bought (into the current bids, 2c under).
    let kalshi_ko = |c: &Connection| snipe::kalshi_event(c, p.m).map(|(ev, sw)| (ev, venue::outcome_of(snipe::kalshi_side(p.o.as_str(), sw)).unwrap_or(p.o)));
    let (mut cash_in, mut cash_out) = (0.0, 0.0);
    for l in &legs {
        cash_out += l["filled"].as_f64().unwrap_or(0.0) * l["avg"].as_f64().unwrap_or(0.0);
    }
    let pf = venue::pm_contract(p.m, p.o).and_then(|x| x.2.fee).unwrap_or(Fee::PM_SPORTS);
    let mut fees = legs.iter().map(|l| {
        let (f, a) = (l["filled"].as_f64().unwrap_or(0.0), l["avg"].as_f64().unwrap_or(0.0));
        if l["venue"] == "polymarket" { venue::pm_fee(pf, f, a) } else { snipe::kalshi_fee(f, a) }
    }).sum::<f64>();
    if got >= 1.0 {
        // Polymarket: sell what we hold (Yes token's bids, or the complement's = 1 - Yes asks).
        let pm_bid = venue::pm_contract(p.m, p.o).and_then(|x| if p.pm_yes { top(&x.1.bids).map(|b| b.0) } else { top(&x.1.asks).map(|a| 1.0 - a.0) }).unwrap_or(p.pm_px - 0.02);
        let t_s = now_ms();
        let s1 = exec::pm_order(&http, &pm, &p.pm_tok, false, (pm_bid - 0.02).max(0.01), got, false).await;
        bots::log_order(app, "arb-test", "polymarket", s1.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "sell", "token": p.pm_tok, "shares": got, "fill": s1.as_ref().ok(), "error": s1.as_ref().err().map(|e| format!("{e:#}"))}));
        legs.push(leg_json("polymarket", "sell", &p.pm_tok, pm_bid, (pm_bid - 0.02).max(0.01), got, &s1, t_s));
        if let Ok(x) = &s1 {
            cash_in += x.filled * x.avg;
            fees += venue::pm_fee(pf, x.filled, x.avg);
        }
    }
    if k_got >= 1.0 {
        let k_bid = crate::store::open_ro(&app.db_path).ok().and_then(|c| kalshi_ko(&c)).and_then(|(ev, ko)| venue::kalshi_contract(&ev, ko)).and_then(|x| if p.pm_yes { top(&x.1.asks).map(|a| 1.0 - a.0) } else { top(&x.1.bids).map(|b| b.0) }).unwrap_or(p.k_px - 0.02);
        let t_s = now_ms();
        let s2 = exec::kalshi_order(&http, &kc, &p.k_tok, false, !p.pm_yes, (k_bid - 0.02).max(0.01), k_got, false).await;
        bots::log_order(app, "arb-test", "kalshi", s2.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "sell", "ticker": p.k_tok, "count": k_got, "fill": s2.as_ref().ok(), "error": s2.as_ref().err().map(|e| format!("{e:#}"))}));
        legs.push(leg_json("kalshi", "sell", &p.k_tok, k_bid, (k_bid - 0.02).max(0.01), k_got, &s2, t_s));
        if let Ok(x) = &s2 {
            cash_in += x.filled * x.avg;
            fees += snipe::kalshi_fee(x.filled, x.avg);
        }
    }
    let unhedged = legs.iter().any(|l| l["side"] == "sell" && l["filled"].as_f64().unwrap_or(0.0) + 0.5 < l["wanted"].as_f64().unwrap_or(0.0));
    if unhedged {
        bots::event("arb test: a sale back did not fully fill: check the accounts (Shared accounts panel)");
    }
    Ok(json!({"ok": got >= 1.0 && k_got >= got && !unhedged, "kind": "arb", "ts": t_decide, "match_id": p.m, "outcome": p.o.as_str(), "live": p.live,
        "market": {"pair_cost": p.unit, "pm_book_age_ms": p.ages.0, "kalshi_book_age_ms": p.ages.1, "pm_socket_quiet_ms": quiet.0, "kalshi_socket_quiet_ms": quiet.1, "min_size": facts.min_size, "fee_bps": facts.fee_bps},
        "claim_ms": t_claimed - t_decide, "leg_gap_ms": gap_ms, "legs": legs, "contracts": got, "net": cash_in - cash_out - fees, "fees": fees, "left_open": unhedged,
        "error": if got < 1.0 { Some("the Polymarket leg did not fill") } else if k_got < got { Some("the Kalshi leg did not fill (Polymarket sold back)") } else { None }}))
}

/// Sell back what an incomplete test left on the account (Sell back button): every Polymarket
/// leg bought and not sold, into the current bids (2c under).
pub async fn sell_back(app: Arc<App>, id: i64) -> Result<Value> {
    let (data,): (String,) = {
        let c = crate::store::open_ro(&app.db_path)?;
        c.query_row("SELECT data FROM bot_tests WHERE id = ?1", [id], |r| Ok((r.get(0)?,)))?
    };
    let mut v: Value = serde_json::from_str(&data)?;
    let legs = v["legs"].as_array().cloned().unwrap_or_default();
    let (mut bought, mut sold) = (std::collections::HashMap::<String, f64>::new(), std::collections::HashMap::<String, f64>::new());
    for l in &legs {
        if l["venue"] != "polymarket" {
            continue;
        }
        let id = l["id"].as_str().unwrap_or("").to_string();
        let f = l["filled"].as_f64().unwrap_or(0.0);
        if l["side"] == "buy" { *bought.entry(id).or_default() += f } else { *sold.entry(id).or_default() += f }
    }
    let http = crate::books::http_client();
    let pm = crate::pmacct::load(&app.db_path);
    let mut out = Vec::new();
    for (tok, n) in bought {
        let left = (n - sold.get(&tok).copied().unwrap_or(0.0)).floor();
        if left < 1.0 {
            continue;
        }
        let book: Value = http.get(format!("https://clob.polymarket.com/book?token_id={tok}")).send().await?.json().await?;
        let bid = book["bids"].as_array().into_iter().flatten().filter_map(|b| crate::books::num(&b["price"])).fold(0.0, f64::max);
        if bid <= 0.0 {
            bail!("no bid on this market right now");
        }
        let limit = (bid - 0.02).max(0.01);
        let t = now_ms();
        let r = exec::pm_order(&http, &pm, &tok, false, limit, left, false).await;
        bots::log_order(&app, "test-sellback", "polymarket", r.as_ref().is_ok_and(|x| x.filled > 0.0), &json!({"side": "sell", "token": tok, "limit": limit, "shares": left, "fill": r.as_ref().ok(), "error": r.as_ref().err().map(|e| format!("{e:#}"))}));
        out.push(leg_json("polymarket", "sell (sell back)", &tok, bid, limit, left, &r, t));
    }
    let mut all = legs;
    all.extend(out.clone());
    v["legs"] = json!(all);
    v["sold_back"] = json!(true);
    if let Ok(c) = Connection::open(&app.db_path) {
        let _ = c.busy_timeout(std::time::Duration::from_secs(5));
        let _ = c.execute("UPDATE bot_tests SET data = ?2 WHERE id = ?1", params![id, v.to_string()]);
    }
    Ok(json!({"ok": true, "sold": out}))
}

/// Past tests with a summary of the delays measured.
pub fn view(app: &App) -> Value {
    let rows: Vec<Value> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT id, data FROM bot_tests ORDER BY id DESC LIMIT 100").ok()?;
            let it = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))).ok()?;
            Some(it.filter_map(|x| x.ok()).filter_map(|(id, s)| serde_json::from_str::<Value>(&s).ok().map(|mut v| { v["id"] = json!(id); v })).collect())
        })
        .unwrap_or_default();
    let med = |mut v: Vec<f64>| -> Option<f64> {
        if v.is_empty() {
            return None;
        }
        v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        Some(v[v.len() / 2])
    };
    let lat = |venue: &str| med(rows.iter().flat_map(|r| r["legs"].as_array().cloned().unwrap_or_default()).filter(|l| l["venue"] == venue).filter_map(|l| l["latency_ms"].as_f64()).collect());
    let slip = |venue: &str| med(rows.iter().flat_map(|r| r["legs"].as_array().cloned().unwrap_or_default()).filter(|l| l["venue"] == venue && l["filled"].as_f64().unwrap_or(0.0) > 0.0).filter_map(|l| l["slippage"].as_f64()).collect());
    json!({
        "tests": rows,
        "summary": {
            "count": rows.len(), "ok": rows.iter().filter(|r| r["ok"].as_bool() == Some(true)).count(),
            "pm_latency_ms": lat("polymarket"), "kalshi_latency_ms": lat("kalshi"),
            "pm_slippage": slip("polymarket"), "kalshi_slippage": slip("kalshi"),
            "leg_gap_ms": med(rows.iter().filter_map(|r| r["leg_gap_ms"].as_f64()).collect()),
            "pm_settle_ms": med(rows.iter().flat_map(|r| r["legs"].as_array().cloned().unwrap_or_default()).filter(|l| l["venue"] == "polymarket").filter_map(|l| l["settle_wait_ms"].as_f64()).filter(|d| *d > 0.0).collect()),
            "pm_match_delay_ms": med(rows.iter().flat_map(|r| r["legs"].as_array().cloned().unwrap_or_default()).filter(|l| l["venue"] == "polymarket").filter_map(|l| l["match_delay_ms"].as_f64()).filter(|d| *d > 0.0).collect()),
            "claim_ms": med(rows.iter().filter_map(|r| r["claim_ms"].as_f64()).collect()),
            "net_total": rows.iter().filter_map(|r| r["net"].as_f64()).sum::<f64>(),
        }
    })
}
