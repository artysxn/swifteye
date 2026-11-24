//! Resting orders for the steered-maker bot (24 Nov 2026). The bot (tools/maker/paperbot.py)
//! decides which orders it wants resting; this module keeps exactly those on Polymarket, reads
//! the fills back and guards the money. AGENTS.md 8n.
//!
//! How it is driven. About once a second the bot posts its WHOLE wanted set to `/maker/sync`: one
//! order per key ("<game>:<side>"), each a token, buy or sell, a price in whole cents and a size.
//! This module compares that set with what it has resting and sends only the difference: an
//! order that is no longer wanted, or whose price changed, is cancelled; a new one is placed
//! post-only (the venue refuses it if it would trade at once, so it never pays the taker fee).
//! The reply carries the orders now resting, every fill the bot has not seen yet, what this
//! module holds, and what was refused and why.
//!
//! DRY and REAL. Until the owner switches Real on (`/maker/real`, typed confirmation, the keys in
//! Settings) nothing here talks to Polymarket about orders: a sync only keeps a pretend order
//! table ("dry-N"), so the whole path can be rehearsed. Real is OFF after every start of the
//! collector. Agents do not arm it (AGENTS.md 8i).
//!
//! What guards the money while Real is on (all of it here, none of it in the bot):
//! * limits per order, per token, on orders resting at once and on dollars at risk at once
//!   (`maker.json`, `/maker/settings`); a sale never exceeds what THIS module bought;
//! * the bot goes silent for `sync_timeout_ms` (3 s): every order is pulled;
//! * the collector dies or loses its line: Polymarket's own heartbeat (sent every 5 s) stops,
//!   and after 10 s the venue cancels every open order of the API key. NOTE: that is every order
//!   of the key, also ones this module did not place, and switching Real off has the same effect;
//! * the account (cash + positions at Polymarket's marks) is worth `max_loss_usd` less than when
//!   Real was switched on, on two readings 30 s apart: Real switches itself off;
//! * the bots' kill switch: Real off, everything pulled.
//! Every real placement, cancel and fill is written to `bot_orders` (bot = "maker").
//!
//! Fills are read once a second from the key's open orders (`size_matched`); an order that has
//! left the list is read once more by id for its last state. A fill counts at the order's own
//! price (a resting order trades at its limit).
//!
//! NOT exercised against the live venue while this was written. The first real call is the
//! owner's test (`/maker/test`, type TEST): one post-only buy of the smallest size 10c under the
//! best bid, read back, cancelled, read back again. It costs nothing unless the price falls 10c
//! in that second.

use crate::exec;
use crate::model::{now_ms, Ms};
use crate::App;
use anyhow::{ensure, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

pub const BOT: &str = "maker";
/// Shares below this are rounding, not a fill.
const EPS: f64 = 0.004;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Limits {
    /// Largest single order, shares.
    pub max_order_shares: f64,
    /// Most orders resting at once. Not the money guard (`max_open_usd` is): the bot rests an order or
    /// two in every game that qualifies, 10-15 at a time on a busy evening, and a low number here
    /// changes what it trades (the first rehearsal with 12 refused an order or two on most passes).
    pub max_live_orders: usize,
    /// Most shares of one token: held plus resting buys.
    pub max_position_shares: f64,
    /// Most dollars at risk at once: what the shares held cost, plus the dollars in resting buys.
    pub max_open_usd: f64,
    /// Real switches itself off when the account is worth this much less than at arming.
    pub max_loss_usd: f64,
    /// No sync from the bot for this long: every order is pulled.
    pub sync_timeout_ms: i64,
    /// Send Polymarket's heartbeat while Real is on (the venue then cancels the key's orders when it stops).
    pub venue_heartbeat: bool,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { max_order_shares: 20.0, max_live_orders: 30, max_position_shares: 40.0, max_open_usd: 175.0, max_loss_usd: 30.0, sync_timeout_ms: 3_000, venue_heartbeat: true }
    }
}

/// One order the bot wants resting.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
#[serde(default)]
pub struct Want {
    pub key: String,
    pub token: String,
    pub buy: bool,
    pub price: f64,
    pub size: f64,
    pub game: i64,
    pub kind: String,
    pub title: String,
}

/// An order this module has resting (real) or pretends to (dry).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Live {
    pub id: String,
    pub want: Want,
    pub placed_ms: Ms,
    /// Shares filled so far.
    pub filled: f64,
    pub real: bool,
    /// A cancel went out and the venue did not confirm it: its last state is still to be read.
    pub closing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FillEv {
    pub seq: u64,
    pub ts_ms: Ms,
    pub key: String,
    pub token: String,
    pub buy: bool,
    pub price: f64,
    pub shares: f64,
    pub order_id: String,
    pub game: i64,
    pub kind: String,
}

/// What this module holds of one token and what it paid for it.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Pos {
    pub shares: f64,
    pub cost: f64,
}

#[derive(Default)]
struct State {
    real: bool,
    armed_ms: Ms,
    equity0: Option<f64>,
    equity: Option<f64>,
    equity_ms: Ms,
    loss_strikes: u32,
    /// By key.
    live: HashMap<String, Live>,
    /// Real orders we cancelled: read once more for a fill that came just before the cancel.
    verify: Vec<Live>,
    pos: HashMap<String, Pos>,
    fills: VecDeque<FillEv>,
    seq: u64,
    realized: f64,
    last_sync_ms: Ms,
    pulled_silent: bool,
    hb_id: String,
    hb_ms: Ms,
    hb_ok_ms: Ms,
    hb_fail: u32,
    dry_n: u64,
    last_token: String,
    counts: BTreeMap<String, u64>,
    last_error: String,
    last_error_ms: Ms,
    place_ms: VecDeque<i64>,
    /// A placement the venue refused: the same wish is not sent again before this time.
    retry: HashMap<String, (Ms, Want)>,
}

impl State {
    fn count(&mut self, k: &str) {
        *self.counts.entry(k.to_string()).or_default() += 1;
    }
    fn err(&mut self, m: String) {
        tracing::warn!("maker: {m}");
        self.last_error = m;
        self.last_error_ms = now_ms();
    }
}

/// What survives a restart of the collector: holdings, the fill log, and the real orders that
/// were resting (they are cancelled at the next start).
#[derive(Default, Serialize, Deserialize)]
#[serde(default)]
struct Saved {
    pos: HashMap<String, Pos>,
    fills: Vec<FillEv>,
    seq: u64,
    realized: f64,
    live_real: Vec<Live>,
}

static ST: LazyLock<tokio::sync::Mutex<State>> = LazyLock::new(Default::default);
static LIM: LazyLock<parking_lot::RwLock<Limits>> = LazyLock::new(Default::default);
static HTTP: LazyLock<reqwest::Client> = LazyLock::new(|| crate::books::http_builder().timeout(Duration::from_secs(6)).connect_timeout(Duration::from_secs(4)).build().expect("http client"));

pub fn limits() -> Limits {
    LIM.read().clone()
}

fn file(app: &App, name: &str) -> PathBuf {
    app.db_path.parent().map(|d| d.join(name)).unwrap_or_else(|| PathBuf::from(name))
}

fn save_state(app: &App, st: &State) {
    let s = Saved { pos: st.pos.clone(), fills: st.fills.iter().cloned().collect(), seq: st.seq, realized: st.realized, live_real: st.live.values().filter(|l| l.real).cloned().chain(st.verify.iter().cloned()).collect() };
    if let Ok(t) = serde_json::to_string(&s) {
        let p = file(app, "maker_state.json");
        let tmp = file(app, "maker_state.json.tmp");
        if std::fs::write(&tmp, t).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

fn log(app: &Arc<App>, ok: bool, data: Value) {
    let app = app.clone();
    tokio::task::spawn_blocking(move || crate::bots::log_order(&app, BOT, "polymarket", ok, &data));
}

// ---------------------------------------------------------------- the rules (no network: unit-tested)

/// The bot's wanted set from a sync body. A bad entry refuses the whole sync: half a set must
/// never be acted on.
fn parse_wants(body: &Value) -> Result<Vec<Want>, String> {
    let mut out: Vec<Want> = Vec::new();
    let mut keys = HashSet::new();
    for o in body["orders"].as_array().ok_or("no \"orders\" list")? {
        let w = Want {
            key: o["key"].as_str().unwrap_or("").trim().to_string(),
            token: o["token"].as_str().unwrap_or("").trim().to_string(),
            buy: match (o["buy"].as_bool(), o["side"].as_str().map(str::to_uppercase).as_deref()) {
                (Some(b), _) => b,
                (None, Some("BUY")) => true,
                (None, Some("SELL")) => false,
                _ => return Err("an order has neither \"buy\" nor a BUY / SELL \"side\"".into()),
            },
            price: o["price"].as_f64().unwrap_or(f64::NAN),
            size: o["size"].as_f64().unwrap_or(f64::NAN),
            game: o["game"].as_i64().unwrap_or(0),
            kind: o["kind"].as_str().unwrap_or("").to_string(),
            title: o["title"].as_str().unwrap_or("").chars().take(80).collect(),
        };
        if w.key.is_empty() || w.key.len() > 60 {
            return Err("an order has no key".into());
        }
        if w.token.is_empty() || w.token.len() > 78 || !w.token.chars().all(|c| c.is_ascii_digit()) {
            return Err(format!("order {}: the token id is not a decimal number", w.key));
        }
        if !w.price.is_finite() || !w.size.is_finite() || w.size <= 0.0 {
            return Err(format!("order {}: price or size is not a number", w.key));
        }
        if ((w.price * 100.0).round() / 100.0 - w.price).abs() > 1e-6 {
            return Err(format!("order {}: the price must be whole cents", w.key));
        }
        if !keys.insert(w.key.clone()) {
            return Err(format!("two orders with the key {}", w.key));
        }
        out.push(w);
    }
    Ok(out)
}

/// Is this resting order still the one the bot wants? Same token, side and price, and the size it
/// was placed with or what is left of it (the bot may not know of a part fill yet: the rest stays).
fn same_order(l: &Live, w: &Want) -> bool {
    l.want.token == w.token && l.want.buy == w.buy && (l.want.price - w.price).abs() < 1e-9 && ((l.want.size - w.size).abs() < 0.005 || (l.want.size - l.filled - w.size).abs() < 0.005)
}

/// (keys whose order must go, orders to place). An order whose cancel is unconfirmed is left
/// alone and blocks its key until its last state is known.
fn plan(wants: &[Want], live: &HashMap<String, Live>) -> (Vec<String>, Vec<Want>) {
    let mut cancel: Vec<String> = live.iter().filter(|(k, l)| !l.closing && !wants.iter().any(|w| &w.key == *k && same_order(l, w))).map(|(k, _)| k.clone()).collect();
    cancel.sort();
    let place = wants.iter().filter(|w| !live.get(&w.key).is_some_and(|l| l.closing || same_order(l, w))).cloned().collect();
    (cancel, place)
}

/// Dollars at risk now: what the shares held cost, plus what the resting buys would cost.
fn open_usd(live: &HashMap<String, Live>, pos: &HashMap<String, Pos>) -> f64 {
    pos.values().map(|p| p.cost.max(0.0)).sum::<f64>() + live.values().filter(|l| l.want.buy).map(|l| (l.want.size - l.filled).max(0.0) * l.want.price).sum::<f64>()
}

/// May this order be placed? `live` = what rests after this round's cancels. `holdings` off (dry
/// runs only): a sale is not checked against what this module holds, because a paper bot's
/// shares exist nowhere.
fn admit(w: &Want, live: &HashMap<String, Live>, pos: &HashMap<String, Pos>, lim: &Limits, holdings: bool) -> Result<(), String> {
    if w.size > lim.max_order_shares + 1e-9 {
        return Err(format!("{} shares is over the limit of {} an order", w.size, lim.max_order_shares));
    }
    if !(0.02..=0.98).contains(&w.price) {
        return Err(format!("price {:.2} is outside 2-98c", w.price));
    }
    if live.len() >= lim.max_live_orders {
        return Err(format!("{} orders are resting already (limit {})", live.len(), lim.max_live_orders));
    }
    let held = pos.get(&w.token).map_or(0.0, |p| p.shares);
    let resting = |buy: bool| live.values().filter(|l| l.want.token == w.token && l.want.buy == buy).map(|l| (l.want.size - l.filled).max(0.0)).sum::<f64>();
    if w.buy {
        if held + resting(true) + w.size > lim.max_position_shares + 1e-9 {
            return Err(format!("{:.2} held + {:.2} resting + {:.2} is over {} shares of one token", held, resting(true), w.size, lim.max_position_shares));
        }
        let at_risk = open_usd(live, pos);
        if at_risk + w.size * w.price > lim.max_open_usd + 1e-9 {
            return Err(format!("${:.2} at risk + ${:.2} is over the limit of ${:.0}", at_risk, w.size * w.price, lim.max_open_usd));
        }
    } else if holdings && resting(false) + w.size > held + 0.005 {
        return Err(format!("would sell {:.2} with {:.2} held (this module sells only what it bought)", resting(false) + w.size, held));
    }
    Ok(())
}

/// Book a fill into the holdings. Returns the dollars a sale made over what the shares cost.
fn apply_fill(pos: &mut HashMap<String, Pos>, token: &str, buy: bool, price: f64, n: f64) -> f64 {
    let p = pos.entry(token.to_string()).or_default();
    let mut made = 0.0;
    if buy {
        p.shares += n;
        p.cost += n * price;
    } else {
        let m = n.min(p.shares.max(0.0));
        let avg = if p.shares > 1e-9 { p.cost / p.shares } else { price };
        p.shares -= n;
        p.cost -= avg * m;
        made = (price - avg) * m;
    }
    if p.shares < 0.005 {
        pos.remove(token);
    }
    made
}

// ---------------------------------------------------------------- fills

/// The venue says `matched` shares of this order are filled: book what is new.
fn note_fill(app: &Arc<App>, st: &mut State, l: &mut Live, matched: f64) {
    let d = ((matched - l.filled) * 100.0).round() / 100.0;
    if d <= EPS {
        return;
    }
    l.filled = matched;
    st.seq += 1;
    let ev = FillEv { seq: st.seq, ts_ms: now_ms(), key: l.want.key.clone(), token: l.want.token.clone(), buy: l.want.buy, price: l.want.price, shares: d, order_id: l.id.clone(), game: l.want.game, kind: l.want.kind.clone() };
    st.realized += apply_fill(&mut st.pos, &ev.token, ev.buy, ev.price, ev.shares);
    st.count("fills");
    crate::bots::event(format!("maker: FILL {} {} @ {:.2} {} ({})", if ev.buy { "bought" } else { "sold" }, ev.shares, ev.price, l.want.title, ev.kind));
    log(app, true, json!({"what": "fill", "order_id": ev.order_id, "key": ev.key, "token": ev.token, "side": if ev.buy { "buy" } else { "sell" }, "price": ev.price, "shares": ev.shares, "game": ev.game, "kind": ev.kind}));
    st.fills.push_back(ev);
    while st.fills.len() > 500 {
        st.fills.pop_front();
    }
}

fn terminal(status: &str) -> bool {
    matches!(status, "MATCHED" | "CANCELED" | "CANCELLED" | "CANCELED_MARKET_RESOLVED" | "INVALID")
}

/// Read the fills of every real order, and the last state of the ones that left the book.
async fn poll(app: &Arc<App>, st: &mut State) {
    if !st.live.values().any(|l| l.real) && st.verify.is_empty() {
        return;
    }
    let c = crate::pmacct::load(&app.db_path);
    let before = st.seq;
    let mut changed = false;
    if st.live.values().any(|l| l.real) {
        let open = match exec::pm_open_orders(&HTTP, &c).await {
            Ok(o) => o,
            Err(e) => {
                st.count("reading open orders failed");
                st.err(format!("reading open orders: {e:#}"));
                return;
            }
        };
        let keys: Vec<String> = st.live.iter().filter(|(_, l)| l.real).map(|(k, _)| k.clone()).collect();
        for k in keys {
            let Some(mut l) = st.live.remove(&k) else { continue };
            let (matched, status) = match open.get(&l.id) {
                Some(x) => x.clone(),
                // Not in the open list: filled, cancelled, or placed a moment ago and not listed yet.
                None => match exec::pm_order_state(&HTTP, &c, &l.id).await {
                    Ok(x) => x,
                    Err(e) => {
                        st.err(format!("reading order {}: {e:#}", l.id));
                        st.live.insert(k, l);
                        continue;
                    }
                },
            };
            note_fill(app, st, &mut l, matched);
            if terminal(&status) || l.want.size - l.filled < EPS {
                changed = true;
            } else {
                l.closing = false;
                st.live.insert(k, l);
            }
        }
    }
    for mut l in std::mem::take(&mut st.verify) {
        match exec::pm_order_state(&HTTP, &c, &l.id).await {
            Ok((m, status)) => {
                note_fill(app, st, &mut l, m);
                changed = true;
                if !terminal(&status) && l.want.size - l.filled >= EPS {
                    // A cancel that did not take: it is still resting. Back into the table, to be pulled again.
                    l.closing = false;
                    st.count("cancels that did not take");
                    let k = if st.live.contains_key(&l.want.key) { format!("{}#{}", l.want.key, l.id) } else { l.want.key.clone() };
                    st.live.insert(k, l);
                }
            }
            Err(_) if now_ms() - l.placed_ms < 3_600_000 => st.verify.push(l),
            Err(_) => changed = true,
        }
    }
    if changed || st.seq != before {
        save_state(app, st);
    }
}

// ---------------------------------------------------------------- pulling orders, the Real switch

/// Cancel every real order of ours. Orders the venue does not confirm stay known and are read again.
async fn pull_all(app: &Arc<App>, st: &mut State, why: &str) {
    st.live.retain(|_, l| l.real);
    if st.live.is_empty() {
        return;
    }
    let c = crate::pmacct::load(&app.db_path);
    let ids: Vec<String> = st.live.values().map(|l| l.id.clone()).collect();
    match exec::pm_cancel(&HTTP, &c, &ids).await {
        Ok((done, not)) => {
            for (_, mut l) in st.live.drain() {
                l.closing = !done.contains(&l.id);
                st.verify.push(l);
            }
            crate::bots::event(format!("maker: pulled {} orders ({why}){}", done.len(), if not.is_empty() { format!("; {} not confirmed, being read", not.len()) } else { String::new() }));
            log(app, true, json!({"what": "cancel all", "why": why, "ids": ids, "canceled": done.len(), "not_canceled": not.iter().map(|x| json!([x.0, x.1])).collect::<Vec<_>>()}));
        }
        Err(e) => {
            // They stay in the table; the next tick tries again (and the venue's heartbeat is the backstop).
            st.count("pulling orders failed");
            st.err(format!("PULLING {} ORDERS FAILED ({why}): {e:#}", ids.len()));
            log(app, false, json!({"what": "cancel all", "why": why, "ids": ids, "error": format!("{e:#}")}));
        }
    }
    save_state(app, st);
}

async fn disarm(app: &Arc<App>, st: &mut State, why: &str) {
    if st.real {
        st.real = false;
        crate::bots::event(format!("maker: real money switched OFF ({why})"));
    }
    pull_all(app, st, why).await;
}

/// The Real switch. On needs the typed confirmation, the keys and the kill switch off.
pub async fn set_real(app: &Arc<App>, real: bool, confirm: &str) -> Result<Value> {
    let mut g = ST.lock().await;
    let st = &mut *g;
    if real {
        ensure!(confirm.trim() == "REAL MONEY", "type REAL MONEY to confirm");
        ensure!(!crate::bots::settings().kill, "the kill switch is on");
        let c = crate::pmacct::load(&app.db_path);
        ensure!(exec::signer_address(&c.private_key).is_ok(), "missing: Polymarket trading key (Settings)");
        ensure!(!c.api_key.trim().is_empty() && crate::pmacct::secret_ok(&c.api_secret) && !c.passphrase.trim().is_empty(), "missing: Polymarket API key, secret and passphrase (Settings)");
        if !st.real {
            st.live.clear(); // the pretend orders of the dry run
            st.real = true;
            st.armed_ms = now_ms();
            st.equity0 = None;
            st.equity = None;
            st.equity_ms = 0;
            st.loss_strikes = 0;
            st.hb_id.clear();
            st.hb_ms = 0;
            st.hb_fail = 0;
            st.last_sync_ms = now_ms();
            st.pulled_silent = false;
            crate::bots::event("maker: switched to REAL MONEY");
        }
    } else {
        disarm(app, st, "switched off by hand").await;
    }
    Ok(view_of(st))
}

/// Limits edit: only the fields given, each kept inside a sane range.
pub fn update(app: &App, edit: &Value) -> Result<Value> {
    let mut l = limits();
    let num = |k: &str, lo: f64, hi: f64| edit.get(k).and_then(|x| x.as_f64()).map(|x| x.clamp(lo, hi));
    if let Some(x) = num("max_order_shares", 5.0, 1_000.0) {
        l.max_order_shares = x;
    }
    if let Some(x) = num("max_live_orders", 1.0, 200.0) {
        l.max_live_orders = x as usize;
    }
    if let Some(x) = num("max_position_shares", 5.0, 5_000.0) {
        l.max_position_shares = x;
    }
    if let Some(x) = num("max_open_usd", 1.0, 100_000.0) {
        l.max_open_usd = x;
    }
    if let Some(x) = num("max_loss_usd", 1.0, 100_000.0) {
        l.max_loss_usd = x;
    }
    if let Some(x) = num("sync_timeout_ms", 1_000.0, 10_000.0) {
        l.sync_timeout_ms = x as i64;
    }
    if let Some(x) = edit.get("venue_heartbeat").and_then(|x| x.as_bool()) {
        l.venue_heartbeat = x;
    }
    std::fs::write(file(app, "maker.json"), serde_json::to_string_pretty(&l)?)?;
    crate::bots::event(format!("maker: limits now {}", serde_json::to_string(&l)?));
    *LIM.write() = l.clone();
    Ok(json!({"limits": l}))
}

// ---------------------------------------------------------------- the bot's call

fn reply(st: &State, since: u64, refused: Vec<Value>) -> Value {
    json!({
        "ok": true, "real": st.real, "now_ms": now_ms(), "seq": st.seq,
        "orders": st.live.values().map(|l| json!({"key": l.want.key, "id": l.id, "token": l.want.token, "buy": l.want.buy, "price": l.want.price, "size": l.want.size,
            "filled": l.filled, "real": l.real, "closing": l.closing, "placed_ms": l.placed_ms})).collect::<Vec<_>>(),
        "fills": st.fills.iter().filter(|f| f.seq > since).collect::<Vec<_>>(),
        "positions": st.pos, "open_usd": open_usd(&st.live, &st.pos), "realized": st.realized, "refused": refused,
    })
}

/// The bot's wanted set: `{"mode": "paper" | "real", "orders": [{key, token, buy | side, price,
/// size, game?, kind?, title?}], "since": last fill seq seen, "settled": [tokens whose game is over]}`.
pub async fn sync(app: &Arc<App>, body: &Value) -> Value {
    let lim = limits();
    let since = body["since"].as_u64().unwrap_or(0);
    let mut g = ST.lock().await;
    let st = &mut *g;
    let wants = match parse_wants(body) {
        Ok(w) => w,
        Err(e) => {
            st.count("syncs refused");
            let mut r = reply(st, since, Vec::new());
            r["ok"] = json!(false);
            r["error"] = json!(e);
            return r;
        }
    };
    // Both sides must agree: a bot running on paper never drives real orders, and a bot that
    // thinks it is real is told at once when Real is off here. A sync in the wrong mode does
    // nothing and does not count as a sign of life (real orders are then pulled as for silence).
    if (body["mode"].as_str() == Some("real")) != st.real {
        st.count("syncs in the wrong mode");
        let mut r = reply(st, since, Vec::new());
        r["ok"] = json!(false);
        r["error"] = json!(if st.real { "Real is ON here but this sync says paper: nothing was done" } else { "Real is OFF here but this sync says real: nothing was done" });
        return r;
    }
    st.last_sync_ms = now_ms();
    st.pulled_silent = false;
    st.count("syncs");
    if let Some(w) = wants.first() {
        st.last_token = w.token.clone();
    }
    for t in body["settled"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).filter(|t| t.chars().all(|c| c.is_ascii_digit())) {
        if !wants.iter().any(|w| w.token == t) && !st.live.values().any(|l| l.want.token == t) {
            if let Some(p) = st.pos.remove(t) {
                crate::bots::event(format!("maker: the bot reports the game of token …{} over: {:.2} shares (cost ${:.2}) leave the books", &t[t.len().saturating_sub(6)..], p.shares, p.cost));
                save_state(app, st);
            }
        }
    }
    if st.real && crate::bots::settings().kill {
        disarm(app, st, "the kill switch is on").await;
    }
    let real = st.real;
    let c = real.then(|| crate::pmacct::load(&app.db_path));
    let (cancel, place) = plan(&wants, &st.live);
    let mut refused: Vec<Value> = Vec::new();
    let mut blocked: HashSet<String> = HashSet::new();
    let mut changed = false;

    // 1. What must go.
    let mut real_out: Vec<(String, String)> = Vec::new();
    for k in &cancel {
        match st.live.get(k) {
            Some(l) if l.real => real_out.push((k.clone(), l.id.clone())),
            Some(_) => {
                st.live.remove(k);
                st.count("dry cancels");
            }
            None => {}
        }
    }
    if let (false, Some(c)) = (real_out.is_empty(), c.as_ref()) {
        let ids: Vec<String> = real_out.iter().map(|x| x.1.clone()).collect();
        match exec::pm_cancel(&HTTP, c, &ids).await {
            Ok((done, not)) => {
                for (k, id) in &real_out {
                    if done.contains(id) {
                        if let Some(l) = st.live.remove(k) {
                            st.verify.push(l);
                        }
                        st.count("cancels");
                    } else {
                        // Usually "already matched": its fill is read on the next tick; nothing new on this key until then.
                        if let Some(l) = st.live.get_mut(k) {
                            l.closing = true;
                        }
                        blocked.insert(k.split('#').next().unwrap_or(k).to_string());
                        st.count("cancels the venue did not confirm");
                    }
                }
                changed = true;
                log(app, true, json!({"what": "cancel", "ids": ids, "canceled": done.len(), "not_canceled": not.iter().map(|x| json!([x.0, x.1])).collect::<Vec<_>>()}));
            }
            Err(e) => {
                blocked.extend(real_out.iter().map(|x| x.0.clone()));
                st.count("cancel calls failed");
                st.err(format!("cancel of {} orders failed: {e:#}", ids.len()));
                log(app, false, json!({"what": "cancel", "ids": ids, "error": format!("{e:#}")}));
            }
        }
    }

    // 2. What is new.
    for w in place {
        // A key whose old order is not certainly gone gets nothing new (and a real order is never
        // overwritten in the table by a pretend one).
        if blocked.contains(&w.key) || st.live.contains_key(&w.key) {
            continue;
        }
        // A wish the venue just refused is tried again after a pause, not twice a second.
        if st.retry.get(&w.key).is_some_and(|(t, old)| now_ms() < *t && *old == w) {
            st.count("placements waiting to be tried again");
            continue;
        }
        if let Err(why) = admit(&w, &st.live, &st.pos, &lim, real) {
            st.count("orders refused by a limit");
            refused.push(json!({"key": w.key, "why": why}));
            continue;
        }
        match c.as_ref() {
            Some(c) => match exec::pm_rest_place(&HTTP, c, &w.token, w.buy, w.price, w.size).await {
                Ok(a) => {
                    st.count("placements");
                    st.place_ms.push_back(a.latency_ms);
                    while st.place_ms.len() > 200 {
                        st.place_ms.pop_front();
                    }
                    log(app, true, json!({"what": "place", "order_id": a.order_id, "status": a.status, "key": w.key, "token": w.token, "side": if w.buy { "buy" } else { "sell" }, "price": a.price, "shares": a.shares,
                        "game": w.game, "kind": w.kind, "latency_ms": a.latency_ms, "prep_ms": a.prep_ms}));
                    if a.status != "live" {
                        st.count("placements the venue did not call live");
                        st.err(format!("order {} came back \"{}\", not \"live\"", a.order_id, a.status));
                    }
                    st.retry.remove(&w.key);
                    st.live.insert(w.key.clone(), Live { id: a.order_id, want: w, placed_ms: a.sent_ms, filled: 0.0, real: true, closing: false });
                    changed = true;
                }
                Err(e) => {
                    let m = format!("{e:#}");
                    // The price moved through ours between the bot's look and the venue's: the order would have
                    // traded at once, and post-only refused it. Expected now and then; not an error.
                    let crosses = m.contains("crosses");
                    st.retry.insert(w.key.clone(), (now_ms() + if crosses { 1_000 } else { 5_000 }, w.clone()));
                    if crosses {
                        st.count("placements refused: would have traded at once");
                    } else {
                        st.count("placements failed");
                        st.err(format!("placing {} {} @ {:.2} failed: {m}", if w.buy { "buy" } else { "sell" }, w.size, w.price));
                        log(app, false, json!({"what": "place", "key": w.key, "token": w.token, "side": if w.buy { "buy" } else { "sell" }, "price": w.price, "shares": w.size, "error": m}));
                    }
                    refused.push(json!({"key": w.key, "why": m}));
                }
            },
            None => {
                st.dry_n += 1;
                st.count("dry placements");
                st.live.insert(w.key.clone(), Live { id: format!("dry-{}", st.dry_n), want: w, placed_ms: now_ms(), filled: 0.0, real: false, closing: false });
            }
        }
    }
    if real && changed {
        save_state(app, st);
    }
    reply(st, since, refused)
}

// ---------------------------------------------------------------- the clock

async fn tick(app: &Arc<App>) {
    let lim = limits();
    let mut g = ST.lock().await;
    let st = &mut *g;
    let now = now_ms();
    if st.real {
        if crate::bots::settings().kill {
            disarm(app, st, "the kill switch is on").await;
        } else {
            // Every tick while any order is left (a cancel call that failed is tried again).
            if now - st.last_sync_ms > lim.sync_timeout_ms && st.live.values().any(|l| l.real) {
                if !st.pulled_silent {
                    st.pulled_silent = true;
                    st.count("pulled: the bot went silent");
                }
                pull_all(app, st, "the bot went silent").await;
            }
            let c = crate::pmacct::load(&app.db_path);
            if lim.venue_heartbeat && now - st.hb_ms >= 5_000 {
                st.hb_ms = now;
                match exec::pm_heartbeat(&HTTP, &c, &st.hb_id).await {
                    Ok(id) => {
                        st.hb_id = id;
                        st.hb_ok_ms = now_ms();
                        st.hb_fail = 0;
                    }
                    Err(e) => {
                        st.hb_fail += 1;
                        st.count("heartbeats failed");
                        if st.hb_fail == 1 || st.hb_fail % 12 == 0 {
                            st.err(format!("Polymarket heartbeat failed ({} in a row; the venue pulls every order of the key after 10 s without one): {e:#}", st.hb_fail));
                        }
                    }
                }
            }
            if now - st.equity_ms >= 30_000 {
                st.equity_ms = now;
                match exec::pm_equity(&HTTP, &c).await {
                    Ok(e) => {
                        st.equity = Some(e);
                        let e0 = *st.equity0.get_or_insert(e);
                        // Twice in a row: right after a buy the cash is gone before the shares are counted.
                        st.loss_strikes = if e < e0 - lim.max_loss_usd { st.loss_strikes + 1 } else { 0 };
                        if st.loss_strikes >= 2 {
                            disarm(app, st, &format!("the account is worth ${e:.2}, ${:.2} less than when Real was switched on (limit ${:.0})", e0 - e, lim.max_loss_usd)).await;
                        }
                    }
                    Err(e) => st.err(format!("reading the account's worth: {e:#}")),
                }
            }
        }
    } else if st.live.values().any(|l| l.real) {
        pull_all(app, st, "Real is off").await;
    } else if now - st.last_sync_ms > lim.sync_timeout_ms {
        st.live.clear(); // a dry run's orders die with a silent bot too
    }
    poll(app, st).await;
}

/// Read the limits and the saved state; cancel what an earlier run left resting; start the clock.
pub fn spawn(app: Arc<App>) {
    if let Some(l) = std::fs::read_to_string(file(&app, "maker.json")).ok().and_then(|t| serde_json::from_str::<Limits>(&t).ok()) {
        *LIM.write() = l;
    }
    tokio::spawn(async move {
        {
            let mut g = ST.lock().await;
            let st = &mut *g;
            if let Some(s) = std::fs::read_to_string(file(&app, "maker_state.json")).ok().and_then(|t| serde_json::from_str::<Saved>(&t).ok()) {
                st.pos = s.pos;
                st.fills = s.fills.into();
                st.seq = s.seq;
                st.realized = s.realized;
                // Orders that were resting when the collector stopped: back into the table as real
                // orders with Real off, so the first tick cancels them and reads their fills.
                for l in s.live_real {
                    let k = if st.live.contains_key(&l.want.key) { format!("{}#{}", l.want.key, l.id) } else { l.want.key.clone() };
                    st.live.insert(k, l);
                }
                if !st.live.is_empty() {
                    crate::bots::event(format!("maker: {} orders were resting when the collector stopped: cancelling them", st.live.len()));
                }
            }
        }
        let mut iv = tokio::time::interval(Duration::from_secs(1));
        iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            iv.tick().await;
            tick(&app).await;
        }
    });
}

// ---------------------------------------------------------------- what the owner sees

fn view_of(st: &State) -> Value {
    let now = now_ms();
    let mut lat: Vec<i64> = st.place_ms.iter().copied().collect();
    lat.sort();
    json!({
        "real": st.real, "armed_ms": st.armed_ms, "limits": limits(), "kill": crate::bots::settings().kill,
        "last_sync_ago_ms": if st.last_sync_ms > 0 { Some(now - st.last_sync_ms) } else { None },
        "orders": st.live.values().map(|l| json!({"key": l.want.key, "id": l.id, "title": l.want.title, "token": l.want.token, "side": if l.want.buy { "BUY" } else { "SELL" }, "price": l.want.price, "size": l.want.size,
            "filled": l.filled, "real": l.real, "closing": l.closing, "kind": l.want.kind, "age_ms": now - l.placed_ms})).collect::<Vec<_>>(),
        "being_read": st.verify.len(),
        "positions": st.pos, "open_usd": open_usd(&st.live, &st.pos), "realized": st.realized,
        "fills": st.fills.iter().rev().take(50).collect::<Vec<_>>(), "seq": st.seq,
        "account": {"worth_at_arming": st.equity0, "worth_now": st.equity},
        "heartbeat": {"on": limits().venue_heartbeat, "last_ok_ago_ms": if st.hb_ok_ms > 0 { Some(now - st.hb_ok_ms) } else { None }, "failed_in_a_row": st.hb_fail},
        "place_latency_ms": {"median": lat.get(lat.len() / 2), "slowest": lat.last(), "n": lat.len()},
        "counts": st.counts, "last_error": st.last_error, "last_error_ago_ms": if st.last_error_ms > 0 { Some(now - st.last_error_ms) } else { None },
    })
}

pub async fn view() -> Value {
    view_of(&*ST.lock().await)
}

/// The owner's test (type TEST): one real post-only buy of the smallest size 10c under the best
/// bid, read back, cancelled, read back again. Nothing fills unless the price falls 10c meanwhile.
pub async fn test(app: &Arc<App>, token: Option<String>) -> Result<Value> {
    ensure!(!crate::bots::settings().kill, "the kill switch is on");
    let c = crate::pmacct::load(&app.db_path);
    ensure!(exec::signer_address(&c.private_key).is_ok(), "missing: Polymarket trading key (Settings)");
    let token = match token.map(|t| t.trim().to_string()).filter(|t| !t.is_empty()) {
        Some(t) => t,
        None => Some(ST.lock().await.last_token.clone()).filter(|t| !t.is_empty()).context("no token: pass one, or let the bot sync once first")?,
    };
    let book: Value = HTTP.get(format!("https://clob.polymarket.com/book?token_id={token}")).send().await?.json().await?;
    let best_bid = book["bids"].as_array().into_iter().flatten().filter_map(|l| crate::books::num(&l["price"])).fold(f64::NAN, f64::max);
    ensure!(best_bid.is_finite(), "this market has no bids: pick another token");
    let facts = exec::token_facts(&HTTP, &token).await?;
    let price = ((best_bid - 0.10) * 100.0 + 1e-6).floor() / 100.0;
    ensure!(price >= 0.02, "the best bid is {best_bid:.2}: pick a market priced above 12c, so the test order can sit 10c under it");
    let size = facts.min_size.max(5.0).ceil();
    let mut out = json!({"token": token, "best_bid": best_bid, "price": price, "shares": size, "would_cost_if_filled": size * price, "started_ms": now_ms()});
    let a = exec::pm_rest_place(&HTTP, &c, &token, true, price, size).await.context("placing the test order")?;
    out["placed"] = json!({"order_id": a.order_id, "status": a.status, "latency_ms": a.latency_ms, "prep_ms": a.prep_ms});
    log(app, true, json!({"what": "test place", "order_id": a.order_id, "token": token, "price": price, "shares": size, "status": a.status}));
    tokio::time::sleep(Duration::from_millis(700)).await;
    let seen = exec::pm_order_state(&HTTP, &c, &a.order_id).await;
    let listed = exec::pm_open_orders(&HTTP, &c).await.map(|o| o.contains_key(&a.order_id));
    out["read_back"] = json!({"by_id": seen.as_ref().map(|x| json!({"filled": x.0, "status": x.1})).unwrap_or_else(|e| json!({"error": format!("{e:#}")})),
        "in_open_orders": listed.as_ref().map(|x| json!(x)).unwrap_or_else(|e| json!({"error": format!("{e:#}")}))});
    // Cancel, twice if the first call fails: an order must never be left behind by a test.
    let ids = [a.order_id.clone()];
    let t0 = now_ms();
    let mut cancel = exec::pm_cancel(&HTTP, &c, &ids).await;
    if cancel.is_err() {
        tokio::time::sleep(Duration::from_millis(500)).await;
        cancel = exec::pm_cancel(&HTTP, &c, &ids).await;
    }
    let cancel_ms = now_ms() - t0;
    let cancelled = cancel.as_ref().is_ok_and(|x| x.0.contains(&a.order_id));
    out["cancel"] = match &cancel {
        Ok((done, not)) => json!({"canceled": done, "not_canceled": not.iter().map(|x| json!([x.0, x.1])).collect::<Vec<_>>(), "latency_ms": cancel_ms}),
        Err(e) => json!({"error": format!("{e:#}"), "latency_ms": cancel_ms}),
    };
    tokio::time::sleep(Duration::from_millis(500)).await;
    let after = exec::pm_order_state(&HTTP, &c, &a.order_id).await;
    out["after_cancel"] = after.as_ref().map(|x| json!({"filled": x.0, "status": x.1})).unwrap_or_else(|e| json!({"error": format!("{e:#}")}));
    let filled = after.as_ref().map(|x| x.0).unwrap_or(0.0).max(seen.as_ref().map(|x| x.0).unwrap_or(0.0));
    let gone = after.as_ref().is_ok_and(|x| terminal(&x.1));
    out["ok"] = json!(a.status == "live" && seen.as_ref().is_ok_and(|x| x.1 == "LIVE") && cancelled && gone && filled < EPS);
    if !(cancelled && gone) {
        out["warning"] = json!(format!("THE TEST ORDER MAY STILL BE RESTING: look at the open orders on polymarket.com and cancel order {} there", a.order_id));
        crate::bots::event(format!("maker: TEST ORDER {} MAY STILL BE RESTING (cancel not confirmed)", a.order_id));
    }
    if filled >= EPS {
        out["warning"] = json!(format!("{filled} shares of the test order FILLED at {price:.2}: they are on the account and in nobody's books"));
    }
    log(app, out["ok"] == json!(true), json!({"what": "test", "result": out}));
    crate::bots::event(format!("maker: test order {}", if out["ok"] == json!(true) { "placed, read back and cancelled: OK" } else { "did NOT go as expected: read /maker/test's reply" }));
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn want(key: &str, token: &str, buy: bool, price: f64, size: f64) -> Want {
        Want { key: key.into(), token: token.into(), buy, price, size, ..Default::default() }
    }
    fn live(id: &str, w: Want, filled: f64, real: bool) -> Live {
        Live { id: id.into(), want: w, placed_ms: 0, filled, real, closing: false }
    }
    fn table(v: Vec<Live>) -> HashMap<String, Live> {
        v.into_iter().map(|l| (l.want.key.clone(), l)).collect()
    }

    #[test]
    fn a_sync_body_is_read_strictly() {
        let ok = json!({"orders": [{"key": "17:1", "token": "123", "side": "BUY", "price": 0.48, "size": 20, "game": 17, "kind": "entry"}, {"key": "17:-1", "token": "456", "buy": false, "price": 0.52, "size": 9.92}]});
        let w = parse_wants(&ok).unwrap();
        assert_eq!(w.len(), 2);
        assert!(w[0].buy && !w[1].buy && w[0].game == 17);
        assert!(parse_wants(&json!({})).is_err(), "no list");
        assert!(parse_wants(&json!({"orders": []})).unwrap().is_empty(), "an empty set is a valid wish: nothing resting");
        for bad in [
            json!({"orders": [{"key": "a", "token": "0xabc", "buy": true, "price": 0.5, "size": 5}]}),
            json!({"orders": [{"key": "a", "token": "12", "buy": true, "price": 0.505, "size": 5}]}),
            json!({"orders": [{"key": "a", "token": "12", "buy": true, "price": 0.5, "size": 0}]}),
            json!({"orders": [{"key": "a", "token": "12", "price": 0.5, "size": 5}]}),
            json!({"orders": [{"key": "a", "token": "12", "buy": true, "price": "x", "size": 5}]}),
            json!({"orders": [{"key": "a", "token": "12", "buy": true, "price": 0.5, "size": 5}, {"key": "a", "token": "13", "buy": true, "price": 0.5, "size": 5}]}),
        ] {
            assert!(parse_wants(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn only_the_difference_is_sent() {
        let a = want("1:1", "11", true, 0.48, 20.0);
        let b = want("2:-1", "22", true, 0.30, 10.0);
        let t = table(vec![live("x", a.clone(), 0.0, true), live("y", b.clone(), 0.0, true)]);
        // Nothing changed: nothing to do.
        assert_eq!(plan(&[a.clone(), b.clone()], &t), (vec![], vec![]));
        // One no longer wanted: it goes, nothing new.
        assert_eq!(plan(&[a.clone()], &t), (vec!["2:-1".to_string()], vec![]));
        // A price change: the old one goes and the new one is placed.
        let a2 = want("1:1", "11", true, 0.49, 20.0);
        assert_eq!(plan(&[a2.clone(), b.clone()], &t), (vec!["1:1".to_string()], vec![a2]));
        // A new key: placed, nothing cancelled.
        let n = want("3:1", "33", true, 0.60, 10.0);
        assert_eq!(plan(&[a.clone(), b.clone(), n.clone()], &t), (vec![], vec![n]));
        // The bot wants nothing: everything goes.
        assert_eq!(plan(&[], &t).0.len(), 2);
    }

    #[test]
    fn a_part_filled_order_stays_while_the_bot_still_asks_for_it() {
        let a = want("1:1", "11", true, 0.48, 20.0);
        let t = table(vec![live("x", a.clone(), 5.0, true)]);
        // The bot has not heard of the 5 yet and still asks for 20: the 15 left stay.
        assert_eq!(plan(&[a.clone()], &t), (vec![], vec![]));
        // It asks for exactly what is left: stays too.
        assert_eq!(plan(&[want("1:1", "11", true, 0.48, 15.0)], &t), (vec![], vec![]));
        // It asks for something else: replaced.
        let c = want("1:1", "11", true, 0.48, 10.0);
        assert_eq!(plan(&[c.clone()], &t), (vec!["1:1".to_string()], vec![c]));
    }

    #[test]
    fn an_unconfirmed_cancel_blocks_its_key() {
        let a = want("1:1", "11", true, 0.48, 20.0);
        let mut l = live("x", a.clone(), 0.0, true);
        l.closing = true;
        let t = table(vec![l]);
        // Not cancelled again, and nothing new on the key, whatever the bot asks.
        assert_eq!(plan(&[want("1:1", "11", true, 0.50, 20.0)], &t), (vec![], vec![]));
        assert_eq!(plan(&[], &t), (vec![], vec![]));
    }

    #[test]
    fn the_limits_hold() {
        let lim = Limits { max_live_orders: 12, ..Limits::default() };
        let none = HashMap::new();
        let nopos = HashMap::new();
        assert!(admit(&want("a", "1", true, 0.5, 20.0), &none, &nopos, &lim, true).is_ok());
        assert!(admit(&want("a", "1", true, 0.5, 21.0), &none, &nopos, &lim, true).is_err(), "order too big");
        assert!(admit(&want("a", "1", true, 0.99, 5.0), &none, &nopos, &lim, true).is_err(), "price out of range");
        // Shares of one token: 20 held + 15 resting + 10 > 40.
        let mut pos = HashMap::new();
        pos.insert("1".to_string(), Pos { shares: 20.0, cost: 10.0 });
        let t = table(vec![live("x", want("k", "1", true, 0.5, 15.0), 0.0, true)]);
        assert!(admit(&want("a", "1", true, 0.5, 10.0), &t, &pos, &lim, true).is_err());
        assert!(admit(&want("a", "1", true, 0.5, 5.0), &t, &pos, &lim, true).is_ok());
        // Dollars at risk: $170 in holdings + $10 would pass $175.
        let mut rich = HashMap::new();
        rich.insert("9".to_string(), Pos { shares: 200.0, cost: 170.0 });
        assert!(admit(&want("a", "1", true, 0.5, 20.0), &none, &rich, &lim, true).is_err());
        assert!(admit(&want("a", "1", true, 0.5, 10.0), &none, &rich, &lim, true).is_ok());
        // Orders at once.
        let many = table((0..12).map(|i| live(&format!("o{i}"), want(&format!("k{i}"), &format!("{}", 100 + i), true, 0.1, 5.0), 0.0, true)).collect());
        assert!(admit(&want("a", "1", true, 0.5, 5.0), &many, &nopos, &lim, true).is_err());
        // A sale: only what this module holds, counting sales already resting.
        assert!(admit(&want("a", "1", false, 0.6, 5.0), &none, &nopos, &lim, true).is_err(), "nothing held");
        assert!(admit(&want("a", "1", false, 0.6, 20.0), &none, &pos, &lim, true).is_ok());
        let selling = table(vec![live("s", want("k", "1", false, 0.6, 15.0), 0.0, true)]);
        assert!(admit(&want("a", "1", false, 0.6, 10.0), &selling, &pos, &lim, true).is_err(), "15 + 10 > 20 held");
        // A dry run has no holdings to check a sale against.
        assert!(admit(&want("a", "1", false, 0.6, 5.0), &none, &nopos, &lim, false).is_ok());
    }

    #[test]
    fn fills_are_booked_at_cost() {
        let mut pos = HashMap::new();
        assert_eq!(apply_fill(&mut pos, "1", true, 0.40, 10.0), 0.0);
        assert_eq!(apply_fill(&mut pos, "1", true, 0.50, 10.0), 0.0);
        assert_eq!(pos["1"], Pos { shares: 20.0, cost: 9.0 });
        // Sell 5 at 55c: bought at 45c on average, 50c made.
        let made = apply_fill(&mut pos, "1", false, 0.55, 5.0);
        assert!((made - 0.50).abs() < 1e-9);
        assert!((pos["1"].shares - 15.0).abs() < 1e-9 && (pos["1"].cost - 6.75).abs() < 1e-9);
        // Sell the rest: the token leaves the books.
        apply_fill(&mut pos, "1", false, 0.40, 15.0);
        assert!(pos.is_empty());
        assert!((open_usd(&HashMap::new(), &pos)).abs() < 1e-9);
    }

    #[test]
    fn dollars_at_risk_counts_holdings_and_resting_buys_only() {
        let mut pos = HashMap::new();
        pos.insert("1".to_string(), Pos { shares: 20.0, cost: 9.0 });
        let t = table(vec![live("b", want("k1", "2", true, 0.30, 10.0), 4.0, true), live("s", want("k2", "1", false, 0.60, 20.0), 0.0, true)]);
        // $9 held + 6 shares left to buy at 30c; the sale adds nothing.
        assert!((open_usd(&t, &pos) - 10.8).abs() < 1e-9);
    }

    #[test]
    fn last_states() {
        assert!(terminal("MATCHED") && terminal("CANCELED") && terminal("CANCELED_MARKET_RESOLVED") && terminal("INVALID"));
        assert!(!terminal("LIVE") && !terminal(""));
    }
}

/// The whole real path against a pretend exchange on this machine: nothing here reaches
/// Polymarket (exec.rs `TEST_CLOB`). The pretend exchange keeps orders, cancels and heartbeats the
/// way the documentation describes them (24 Nov 2026).
#[cfg(test)]
mod venue_tests {
    use super::*;
    use axum::extract::{Path, State as Ax};
    use axum::http::{HeaderMap, StatusCode};
    use axum::routing::{delete, get, post};
    use axum::{Json, Router};

    #[derive(Clone)]
    struct MockOrder {
        id: String,
        token: String,
        buy: bool,
        price: f64,
        size: f64,
        matched: f64,
        status: String,
    }

    #[derive(Default)]
    struct Mock {
        orders: Vec<MockOrder>,
        posts: u64,
        cancels: u64,
        unsigned: u64,
        cross_next: bool,
        hb: Vec<String>,
        hb_bad: u32,
    }

    type M = Arc<parking_lot::Mutex<Mock>>;

    fn signed(h: &HeaderMap) -> bool {
        ["poly_address", "poly_signature", "poly_timestamp", "poly_api_key", "poly_passphrase"].iter().all(|k| h.contains_key(*k))
    }

    async fn book() -> Json<Value> {
        Json(json!({"tick_size": "0.01", "min_order_size": "5", "neg_risk": false, "bids": [{"price": "0.40", "size": "100"}], "asks": [{"price": "0.60", "size": "100"}]}))
    }

    async fn fee() -> Json<Value> {
        Json(json!({"base_fee": 0}))
    }

    async fn neg() -> Json<Value> {
        Json(json!({"neg_risk": false}))
    }

    async fn place(Ax(m): Ax<M>, h: HeaderMap, Json(b): Json<Value>) -> (StatusCode, Json<Value>) {
        let mut m = m.lock();
        m.posts += 1;
        if !signed(&h) {
            m.unsigned += 1;
        }
        if b["orderType"] != "GTC" || b["postOnly"] != true {
            return (StatusCode::BAD_REQUEST, Json(json!({"error": "this pretend exchange takes post-only GTC orders only"})));
        }
        if m.cross_next {
            m.cross_next = false;
            return (StatusCode::BAD_REQUEST, Json(json!({"error": "invalid post-only order: order crosses book"})));
        }
        let o = &b["order"];
        let buy = o["side"] == "BUY";
        let amt = |k: &str| o[k].as_str().unwrap().parse::<f64>().unwrap() / 1e6;
        let (size, usd) = if buy { (amt("takerAmount"), amt("makerAmount")) } else { (amt("makerAmount"), amt("takerAmount")) };
        let id = format!("0xorder{}", m.orders.len() + 1);
        m.orders.push(MockOrder { id: id.clone(), token: o["tokenId"].as_str().unwrap().into(), buy, price: (usd / size * 100.0).round() / 100.0, size, matched: 0.0, status: "LIVE".into() });
        (StatusCode::OK, Json(json!({"success": true, "errorMsg": "", "orderID": id, "status": "live"})))
    }

    async fn cancel(Ax(m): Ax<M>, h: HeaderMap, Json(ids): Json<Vec<String>>) -> Json<Value> {
        let mut m = m.lock();
        m.cancels += 1;
        if !signed(&h) {
            m.unsigned += 1;
        }
        let (mut done, mut not) = (Vec::new(), serde_json::Map::new());
        for id in ids {
            match m.orders.iter_mut().find(|o| o.id == id) {
                Some(o) if o.status == "LIVE" => {
                    o.status = "CANCELED".into();
                    done.push(id);
                }
                Some(_) => {
                    not.insert(id, json!("order already matched"));
                }
                None => {
                    not.insert(id, json!("order not found"));
                }
            }
        }
        Json(json!({"canceled": done, "not_canceled": not}))
    }

    async fn open(Ax(m): Ax<M>) -> Json<Value> {
        let m = m.lock();
        Json(json!({"data": m.orders.iter().filter(|o| o.status == "LIVE").map(|o| json!({"id": o.id, "status": "LIVE", "size_matched": o.matched.to_string(), "original_size": o.size.to_string()})).collect::<Vec<_>>(), "next_cursor": "LTE="}))
    }

    async fn one(Ax(m): Ax<M>, Path(id): Path<String>) -> (StatusCode, Json<Value>) {
        match m.lock().orders.iter().find(|o| o.id == id) {
            Some(o) => (StatusCode::OK, Json(json!({"id": o.id, "status": o.status, "size_matched": o.matched.to_string()}))),
            None => (StatusCode::NOT_FOUND, Json(Value::Null)),
        }
    }

    async fn heartbeat(Ax(m): Ax<M>, Json(b): Json<Value>) -> (StatusCode, Json<Value>) {
        let mut m = m.lock();
        let expected = m.hb.last().cloned().unwrap_or_default();
        if b["heartbeat_id"].as_str() != Some(expected.as_str()) {
            m.hb_bad += 1;
            return (StatusCode::BAD_REQUEST, Json(json!({"error_msg": "Invalid Heartbeat ID", "heartbeat_id": expected})));
        }
        let id = format!("hb-{}", m.hb.len() + 1);
        m.hb.push(id.clone());
        (StatusCode::OK, Json(json!({"heartbeat_id": id})))
    }

    fn o(key: &str, buy: bool, price: f64, size: f64) -> Value {
        json!({"key": key, "token": "111", "buy": buy, "price": price, "size": size, "game": 1, "kind": if buy { "entry" } else { "exit" }, "title": "A v B"})
    }

    #[tokio::test]
    async fn the_real_path_against_a_pretend_exchange() {
        let mock: M = Default::default();
        let router = Router::new()
            .route("/book", get(book))
            .route("/fee-rate", get(fee))
            .route("/neg-risk", get(neg))
            .route("/order", post(place))
            .route("/orders", delete(cancel))
            .route("/data/orders", get(open))
            .route("/data/order/{id}", get(one))
            .route("/v1/heartbeats", post(heartbeat))
            .with_state(mock.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        *exec::TEST_CLOB.lock() = Some(format!("http://{}", listener.local_addr().unwrap()));
        tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });

        let dir = std::env::temp_dir().join(format!("maker-venue-{}-{:x}", std::process::id(), rand::random::<u32>()));
        std::fs::create_dir_all(&dir).unwrap();
        let app = crate::desk::pretend_app(&dir);
        let pk = format!("{:064x}", 7);
        let me = exec::signer_address(&pk).unwrap();
        crate::pmacct::save(&app.db_path, &crate::pmacct::PmCreds { wallet: me.clone(), signer: me, api_key: "key".into(), api_secret: "c2VjcmV0".into(), passphrase: "p".into(), private_key: pk, signature_type: 0 }).unwrap();
        let real = |orders: Vec<Value>, since: u64| json!({"mode": "real", "orders": orders, "since": since});
        let fills_read = |app: Arc<App>| async move {
            let mut g = ST.lock().await;
            poll(&app, &mut g).await;
        };
        let posts = || mock.lock().posts;

        // Real is off: a sync that says "real" does nothing, and the wrong words do not switch it on.
        let r = sync(&app, &real(vec![o("1:1", true, 0.45, 20.0)], 0)).await;
        assert_eq!(r["ok"], false, "{r}");
        assert!(set_real(&app, true, "yes please").await.is_err());
        assert!(!ST.lock().await.real);
        // A rehearsal: a pretend order, nothing sent.
        let r = sync(&app, &json!({"mode": "paper", "orders": [o("1:1", true, 0.45, 20.0)]})).await;
        assert_eq!((r["ok"].as_bool(), r["orders"][0]["real"].as_bool(), r["orders"][0]["id"].as_str()), (Some(true), Some(false), Some("dry-1")), "{r}");
        assert_eq!(posts(), 0);

        // Real on (the exchange is the pretend one). The pretend orders are gone.
        let v = set_real(&app, true, "REAL MONEY").await.unwrap();
        assert_eq!((v["real"].as_bool(), v["orders"].as_array().map(Vec::len)), (Some(true), Some(0)));
        ST.lock().await.equity_ms = now_ms(); // the account's worth is read from another service: not in this test
        // Now a sync that says "paper" does nothing.
        let r = sync(&app, &json!({"mode": "paper", "orders": [o("1:1", true, 0.45, 20.0)]})).await;
        assert_eq!(r["ok"], false, "{r}");
        assert_eq!(posts(), 0);

        // 1. An order is placed: post-only, signed, at the price and size asked.
        let r = sync(&app, &real(vec![o("1:1", true, 0.45, 20.0)], 0)).await;
        assert_eq!((r["ok"].as_bool(), r["orders"][0]["real"].as_bool(), r["orders"][0]["id"].as_str()), (Some(true), Some(true), Some("0xorder1")), "{r}");
        {
            let m = mock.lock();
            assert_eq!((m.posts, m.unsigned, m.orders.len()), (1, 0, 1));
            assert_eq!((m.orders[0].buy, m.orders[0].price, m.orders[0].size, m.orders[0].token.as_str()), (true, 0.45, 20.0, "111"));
        }
        // 2. The same wish again: nothing is sent.
        sync(&app, &real(vec![o("1:1", true, 0.45, 20.0)], 0)).await;
        assert_eq!((posts(), mock.lock().cancels), (1, 0));

        // 3. Five shares fill. The fill is read, booked at the order's price, and the 15 left stay
        //    while the bot still asks for its 20.
        mock.lock().orders[0].matched = 5.0;
        fills_read(app.clone()).await;
        let r = sync(&app, &real(vec![o("1:1", true, 0.45, 20.0)], 0)).await;
        assert_eq!(r["fills"].as_array().unwrap().len(), 1, "{r}");
        assert_eq!((r["fills"][0]["shares"].as_f64(), r["fills"][0]["price"].as_f64(), r["fills"][0]["buy"].as_bool(), r["fills"][0]["seq"].as_u64()), (Some(5.0), Some(0.45), Some(true), Some(1)));
        assert_eq!(r["positions"]["111"]["shares"], 5.0);
        assert_eq!(posts(), 1);
        // The bot has seen fill 1: it is not sent again.
        assert!(sync(&app, &real(vec![o("1:1", true, 0.45, 20.0)], 1)).await["fills"].as_array().unwrap().is_empty());

        // 4. The price changes: the old order is cancelled, a new one placed; reading the old one
        //    once more books nothing twice.
        let r = sync(&app, &real(vec![o("1:1", true, 0.46, 20.0)], 1)).await;
        assert_eq!(r["orders"][0]["id"], "0xorder2", "{r}");
        {
            let m = mock.lock();
            assert_eq!((m.posts, m.cancels, m.orders[0].status.as_str(), m.orders[1].status.as_str(), m.orders[1].price), (2, 1, "CANCELED", "LIVE", 0.46));
        }
        fills_read(app.clone()).await;
        assert_eq!(ST.lock().await.seq, 1);

        // 5. The race: the order fills completely just before the bot asks for another price. The
        //    cancel is not confirmed, so NOTHING new is placed on that key until the fill is read.
        {
            let mut m = mock.lock();
            m.orders[1].matched = 20.0;
            m.orders[1].status = "MATCHED".into();
        }
        let r = sync(&app, &real(vec![o("1:1", true, 0.47, 20.0)], 1)).await;
        assert_eq!((posts(), r["orders"][0]["closing"].as_bool()), (2, Some(true)), "{r}");
        fills_read(app.clone()).await;
        let r = sync(&app, &real(vec![], 1)).await;
        assert_eq!((r["fills"][0]["shares"].as_f64(), r["fills"][0]["price"].as_f64(), r["orders"].as_array().map(Vec::len)), (Some(20.0), Some(0.46), Some(0)), "{r}");
        assert_eq!(r["positions"]["111"]["shares"], 25.0);
        assert!((r["positions"]["111"]["cost"].as_f64().unwrap() - (5.0 * 0.45 + 20.0 * 0.46)).abs() < 1e-9);
        assert!((r["open_usd"].as_f64().unwrap() - 11.45).abs() < 1e-9);

        // 6. The limits are kept here, whatever the bot asks: 25 held + 20 more is over 40 a token.
        let r = sync(&app, &real(vec![o("1:1", true, 0.47, 20.0)], 2)).await;
        assert_eq!((posts(), r["refused"].as_array().map(Vec::len), r["orders"].as_array().map(Vec::len)), (2, Some(1), Some(0)), "{r}");
        // 7. Selling: 25 is over the limit an order; 20 is placed; 10 more on top would be more than is held.
        let r = sync(&app, &real(vec![o("1:-1", false, 0.52, 25.0)], 2)).await;
        assert_eq!((posts(), r["refused"].as_array().map(Vec::len)), (2, Some(1)), "{r}");
        let r = sync(&app, &real(vec![o("1:-1", false, 0.52, 20.0), o("9:-1", false, 0.53, 10.0)], 2)).await;
        assert_eq!((posts(), r["refused"].as_array().map(Vec::len), r["orders"].as_array().map(Vec::len)), (3, Some(1), Some(1)), "{r}");
        {
            let m = mock.lock();
            assert_eq!((m.orders[2].buy, m.orders[2].price, m.orders[2].size), (false, 0.52, 20.0));
        }

        // 8. A post-only order that would trade at once is refused by the exchange: nothing rests,
        //    and it is not an error.
        mock.lock().cross_next = true;
        let r = sync(&app, &real(vec![o("1:-1", false, 0.51, 20.0)], 2)).await;
        assert_eq!((posts(), r["orders"].as_array().map(Vec::len), mock.lock().orders[2].status.as_str()), (4, Some(0), "CANCELED"), "{r}");
        assert!(r["refused"][0]["why"].as_str().unwrap().contains("crosses"));
        // The same wish again at once is not sent again yet.
        sync(&app, &real(vec![o("1:-1", false, 0.51, 20.0)], 2)).await;
        assert_eq!(posts(), 4);

        // 9. The bot goes silent: the order is pulled by the clock, and the exchange's heartbeat is
        //    sent with each reply's id.
        sync(&app, &real(vec![o("1:-1", false, 0.52, 20.0)], 2)).await;
        assert_eq!((posts(), mock.lock().orders[3].status.as_str()), (5, "LIVE"));
        {
            let mut g = ST.lock().await;
            g.last_sync_ms = now_ms() - 10_000;
            g.equity_ms = now_ms();
        }
        tick(&app).await;
        {
            let m = mock.lock();
            assert_eq!((m.orders[3].status.as_str(), m.hb.len()), ("CANCELED", 1));
        }
        assert!(ST.lock().await.live.is_empty());
        {
            let mut g = ST.lock().await;
            g.hb_ms = 0;
            g.equity_ms = now_ms();
        }
        tick(&app).await;
        {
            let m = mock.lock();
            assert_eq!((m.hb.len(), m.hb_bad), (2, 0));
        }

        // 10. Real switched off: what rests is pulled, and a "real" sync does nothing again.
        sync(&app, &real(vec![o("1:-1", false, 0.52, 20.0)], 2)).await;
        assert_eq!((posts(), mock.lock().orders[4].status.as_str()), (6, "LIVE"));
        let v = set_real(&app, false, "").await.unwrap();
        assert_eq!((v["real"].as_bool(), mock.lock().orders[4].status.as_str()), (Some(false), "CANCELED"));
        let r = sync(&app, &real(vec![o("1:-1", false, 0.52, 20.0)], 2)).await;
        assert_eq!((r["ok"].as_bool(), posts()), (Some(false), 6));
        assert_eq!(mock.lock().unsigned, 0, "every order and cancel carried the signed headers");
        // The holdings and the fill log are on disk for the next start.
        let saved: Saved = serde_json::from_str(&std::fs::read_to_string(dir.join("maker_state.json")).unwrap()).unwrap();
        assert_eq!((saved.seq, saved.pos["111"].shares), (2, 25.0));
        *exec::TEST_CLOB.lock() = None;
        let _ = std::fs::remove_dir_all(&dir);
    }
}
