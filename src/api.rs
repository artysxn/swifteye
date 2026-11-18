//! HTTP API. Live state comes from memory; history comes from SQLite.

use crate::engine::{EventState, QKey};
use crate::model::*;
use crate::{store, App};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures::StreamExt;
use rusqlite::types::Value as SqlVal;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::convert::Infallible;
use std::sync::Arc;
use tokio_stream::wrappers::BroadcastStream;

type S = State<Arc<App>>;

pub fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/", get(|| async { asset("index.html", "text/html; charset=utf-8", include_str!("web/index.html")) }))
        .route("/assets/app.css", get(|| async { asset("app.css", "text/css; charset=utf-8", include_str!("web/app.css")) }))
        .route("/assets/app.js", get(|| async { asset("app.js", "text/javascript; charset=utf-8", include_str!("web/app.js")) }))
        .route("/assets/pages.js", get(|| async { asset("pages.js", "text/javascript; charset=utf-8", include_str!("web/pages.js")) }))
        .route("/assets/pages2.js", get(|| async { asset("pages2.js", "text/javascript; charset=utf-8", include_str!("web/pages2.js")) }))
        .route("/assets/pages3.js", get(|| async { asset("pages3.js", "text/javascript; charset=utf-8", include_str!("web/pages3.js")) }))
        .route("/assets/uplot.js", get(|| async { asset("vendor/uPlot.iife.min.js", "text/javascript; charset=utf-8", include_str!("web/vendor/uPlot.iife.min.js")) }))
        .route("/assets/uplot.css", get(|| async { asset("vendor/uPlot.min.css", "text/css; charset=utf-8", include_str!("web/vendor/uPlot.min.css")) }))
        .route("/assets/img/{name}", get(img))
        .route("/assets/pages4.js", get(|| async { asset("pages4.js", "text/javascript; charset=utf-8", include_str!("web/pages4.js")) }))
        .route("/telegram", get(tg_get).post(tg_post))
        .route("/telegram/test", post(tg_test))
        .route("/telegram/watch", get(|| async { Json(json!({"watch": crate::telegram::watched()})) }))
        .route("/telegram/watch/{id}", post(tg_watch))
        .route("/arbs", get(arbs))
        .route("/arbs/history", get(arbs_history))
        .route("/assets/pages5.js", get(|| async { asset("pages5.js", "text/javascript; charset=utf-8", include_str!("web/pages5.js")) }))
        .route("/suspicious", get(suspicious))
        .route("/suspicious/settings", post(suspicious_settings))
        .route("/suspicious/event/{id}", get(suspicious_event))
        .route("/search", get(search))
        .route("/data", get(data_stats))
        .route("/data/scan", get(data_scan))
        .route("/data/folders", post(data_folders))
        .route("/data/job", get(|| async { Json(crate::data::job()) }))
        .route("/data/export", post(data_export))
        .route("/data/merge", post(data_merge))
        .route("/pred/meta", get(pred_meta))
        .route("/rankings", get(rankings))
        .route("/snipe", get(snipe))
        .route("/adaptive", get(adaptive))
        .route("/adaptive/settings", post(adaptive_settings))
        .route("/adaptive/dump", get(adaptive_dump))
        .route("/predarb", get(predarb))
        .route("/bots", get(bots_get))
        .route("/bots/settings", post(bots_settings))
        .route("/bots/real/{bot}", post(bots_real))
        .route("/bots/sim", get(bots_sim))
        .route("/bots/accounts", get(bots_accounts))
        .route("/bots/tests", get(bots_tests))
        .route("/bots/runs", get(bots_runs))
        .route("/bots/run/start", post(bots_run_start))
        .route("/bots/run/stop", post(bots_run_stop))
        .route("/bots/run/chart", get(bots_run_chart))
        .route("/pm/api-from-key", post(pm_api_from_key))
        .route("/bots/test/{bot}", post(bots_test))
        .route("/bots/test-sellback/{id}", post(bots_test_sellback))
        .route("/assets/pages6.js", get(|| async { asset("pages6.js", "text/javascript; charset=utf-8", include_str!("web/pages6.js")) }))
        .route("/predarb/order/{id}", post(predarb_order))
        .route("/predarb/sell/{ts}/{id}", post(predarb_sell))
        .route("/api", get(index))
        .route("/ui", get(ui))
        .route("/quotes", get(quotes))
        .route("/info", get(info))
        .route("/control/books/{name}", post(control_book))
        .route("/pm/settings", get(pm_settings_get).post(pm_settings_post))
        .route("/pm/account", get(pm_account))
        .route("/kalshi/settings", get(kalshi_settings_get).post(kalshi_settings_post))
        .route("/kalshi/account", get(kalshi_account))
        .route("/control/scope", post(control_scope))
        .route("/books", get(books))
        .route("/matches", get(matches))
        .route("/matches/{id}", get(match_detail))
        .route("/matches/{id}/changes", get(match_changes))
        .route("/matches/{into}/merge/{from}", post(merge))
        .route("/events/{book}/{event_id}/unlink", post(unlink))
        .route("/board", get(board))
        .route("/changes", get(changes))
        .route("/stream", get(stream))
        .route("/query", get(query))
        .route("/stats", get(stats))
        .route("/lag", get(lag_summary))
        .route("/lag/events", get(lag_events))
        .route("/lag/books", get(lag_books_summary))
        .route("/lag/books/events", get(lag_books_events))
        .route("/clock", get(|| async { Json(crate::clock::status()) }))
        .layer(axum::middleware::from_fn(local_cors))
        .with_state(app)
}

/// Let ui.html opened from disk (Origin "null") or from another localhost port read
/// the API. Other websites still can't read your odds data.
async fn local_cors(req: axum::extract::Request, next: axum::middleware::Next) -> Response {
    let origin = req.headers().get(axum::http::header::ORIGIN).cloned();
    let mut resp = next.run(req).await;
    if let Some(o) = origin {
        let s = o.to_str().unwrap_or("");
        let local = s == "null"
            || s.starts_with("file:")
            || s.starts_with("http://127.0.0.1")
            || s.starts_with("http://localhost")
            || s.starts_with("https://localhost");
        if local {
            resp.headers_mut().insert(axum::http::header::ACCESS_CONTROL_ALLOW_ORIGIN, o);
        }
    }
    resp
}

fn err(code: StatusCode, msg: impl Into<String>) -> Response {
    (code, Json(json!({"error": msg.into()}))).into_response()
}

async fn index() -> Json<Value> {
    Json(json!({
        "service": "oddsfeed",
        "endpoints": {
            "GET /ui": "live odds table in the browser",
            "GET /books": "per-book status: polls, errors, live events, last request time",
            "GET /matches?all=0&q=&book=": "matches (cross-book) currently live; best 1X2 across books",
            "GET /matches/{id}": "every current price for one match, per book",
            "GET /matches/{id}/changes?since=&market=&book=&limit=": "price history for one match (oldest first)",
            "GET /board?market=1x2|total|handicap|btts": "the live table: one row per match, prices per book",
            "GET /changes?since=&until=&book=&match=&market=&outcome=&kind=&limit=&order=asc|desc": "the change log",
            "GET /stream?match=&book=&market=": "server-sent events, one per price change as it happens",
            "GET /lag/books?since=&until=&min_jump=&window_s=&settle_s=&clock=&match=&book=": "book-to-book delay: per (trigger, follower) pair, how long after one book jumps the others follow",
            "GET /lag/books/events?trigger_book=&follower_book=&limit=": "each book-to-book jump behind the summary",
            "GET /query?sql=SELECT...&limit=": "read-only SQL over tables matches, book_events, changes",
            "GET /stats": "row counts and changes per book in the last 5 minutes",
            "POST /matches/{into}/merge/{from}": "fix a missed cross-book match",
            "POST /events/{book}/{event_id}/unlink": "fix a wrong cross-book match"
        },
        "notes": {
            "ts_ms": "ms since epoch when the response carrying the change finished arriving",
            "book_ts_ms": "the bookmaker's own change time, when its feed has one (kambi s-precision, sportybet/betika ms)",
            "line": "total: goal line; handicap: home team's handicap",
            "price null": "suspended or removed"
        }
    }))
}

async fn books(State(app): S) -> Json<Value> {
    let eng = app.engine.lock();
    let mut v: Vec<_> = eng.books.values().map(|b| b.status.clone()).collect();
    v.sort_by_key(|s| s.name);
    Json(json!({"started_ms": app.started_ms, "now_ms": now_ms(), "books": v}))
}

#[derive(Deserialize, Default)]
struct MatchesQ {
    all: Option<u8>,
    q: Option<String>,
    book: Option<String>,
    min_books: Option<usize>,
}

fn quote_json(k: &QKey, q: &crate::engine::QuoteState) -> Value {
    json!({
        "market": k.0, "line": line_from_key(k.1), "outcome": k.2,
        "price": q.price, "updated_ms": q.updated_ms, "book_ts_ms": q.book_ts
    })
}

fn live_events_of(app: &App, match_id: i64) -> Vec<(&'static str, EventSnapshot)> {
    let eng = app.engine.lock();
    let mut v = Vec::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            if es.match_id == match_id {
                v.push((*book, EventSnapshot::from(es)));
            }
        }
    }
    v.sort_by_key(|x| x.0);
    v
}

struct EventSnapshot {
    meta: crate::engine::EventMeta,
    quotes: Vec<(QKey, crate::engine::QuoteState)>,
    last_seen_ms: Ms,
}
impl From<&EventState> for EventSnapshot {
    fn from(es: &EventState) -> Self {
        EventSnapshot {
            meta: es.meta.clone(),
            quotes: es.quotes.iter().map(|(k, q)| (*k, q.clone())).collect(),
            last_seen_ms: es.last_seen_ms,
        }
    }
}

async fn matches(State(app): S, Query(p): Query<MatchesQ>) -> Json<Value> {
    let eng = app.engine.lock();
    let needle = p.q.as_deref().map(|s| s.to_lowercase());
    // match id -> (book, &EventState)
    let mut by_match: BTreeMap<i64, Vec<(&'static str, &EventState)>> = BTreeMap::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            by_match.entry(es.match_id).or_default().push((*book, es));
        }
    }
    let mut rows = Vec::new();
    let ids: Vec<i64> = if p.all == Some(1) {
        eng.matcher.matches.keys().copied().collect()
    } else {
        by_match.keys().copied().collect()
    };
    for id in ids {
        let Some(m) = eng.matcher.matches.get(&id) else { continue };
        if !eng.visible(id) && p.all != Some(1) {
            continue;
        }
        let evs = by_match.get(&id).cloned().unwrap_or_default();
        if let Some(b) = &p.book {
            if !evs.iter().any(|(bk, _)| bk == b) {
                continue;
            }
        }
        if evs.len() < p.min_books.unwrap_or(0) {
            continue;
        }
        if let Some(n) = &needle {
            let hay = format!("{} {} {}", m.home, m.away, m.league).to_lowercase();
            let hay2: String = evs.iter().map(|(_, e)| format!(" {} {}", e.meta.home, e.meta.away)).collect::<String>().to_lowercase();
            if !hay.contains(n.as_str()) && !hay2.contains(n.as_str()) {
                continue;
            }
        }
        let mut best: BTreeMap<&str, (f64, &str)> = BTreeMap::new();
        for (book, es) in &evs {
            for (k, q) in &es.quotes {
                if k.0 != Market::X12 {
                    continue;
                }
                if let Some(pr) = q.price {
                    let e = best.entry(k.2.as_str()).or_insert((0.0, book));
                    if pr > e.0 {
                        *e = (pr, book);
                    }
                }
            }
        }
        let best_json: BTreeMap<&str, Value> = best.iter().map(|(o, (pr, b))| (*o, json!({"price": pr, "book": b}))).collect();
        let overround = if best.len() == 3 { Some(best.values().map(|(p, _)| 1.0 / p).sum::<f64>()) } else { None };
        let score = evs.iter().find_map(|(_, e)| e.meta.score.clone());
        let clock = evs.iter().find_map(|(_, e)| e.meta.clock.clone());
        rows.push(json!({
            "id": m.id, "sport": m.sport, "home": m.home, "away": m.away, "league": m.league,
            "start_ms": m.start_ms, "sr_id": m.sr_id, "score": score, "clock": clock,
            "live": evs.iter().any(|(_, e)| e.meta.live),
            "books": evs.iter().map(|(b, e)| json!({"book": b, "event_id": e.meta.id, "home": e.meta.home, "away": e.meta.away, "league": e.meta.league})).collect::<Vec<_>>(),
            "best_1x2": best_json,
            "best_1x2_book_sum": overround,
        }));
    }
    rows.sort_by(|a, b| {
        let na = a["books"].as_array().map_or(0, |x| x.len());
        let nb = b["books"].as_array().map_or(0, |x| x.len());
        nb.cmp(&na).then(a["start_ms"].as_i64().cmp(&b["start_ms"].as_i64()))
    });
    Json(json!({"now_ms": now_ms(), "count": rows.len(), "matches": rows}))
}

async fn match_detail(State(app): S, Path(id): Path<i64>) -> Response {
    let info = app.engine.lock().matcher.matches.get(&id).cloned();
    let evs = live_events_of(&app, id);
    if info.is_none() && evs.is_empty() {
        return err(StatusCode::NOT_FOUND, "match not in memory; use /changes?match= for history");
    }
    let books: Vec<Value> = evs
        .iter()
        .map(|(b, e)| {
            json!({
                "book": b, "event": e.meta, "last_seen_ms": e.last_seen_ms,
                "quotes": e.quotes.iter().map(|(k, q)| quote_json(k, q)).collect::<Vec<_>>()
            })
        })
        .collect();
    Json(json!({"now_ms": now_ms(), "match": info, "books": books})).into_response()
}

#[derive(Deserialize, Default)]
struct ChangesQ {
    since: Option<i64>,
    until: Option<i64>,
    book: Option<String>,
    #[serde(rename = "match")]
    match_id: Option<i64>,
    market: Option<String>,
    outcome: Option<String>,
    kind: Option<String>,
    line: Option<f64>,
    limit: Option<usize>,
    order: Option<String>,
}

async fn run_changes(app: Arc<App>, p: ChangesQ) -> Response {
    let mut sql = String::from(
        "SELECT c.id, c.ts_ms, c.book, c.book_event_id, c.match_id, c.market, c.line, c.outcome, c.kind, c.old_price, c.new_price, c.book_ts_ms FROM changes c WHERE 1=1",
    );
    let mut args: Vec<SqlVal> = Vec::new();
    let mut add = |cond: &str, v: SqlVal| {
        args.push(v);
        sql.push_str(&format!(" AND {cond} ?{}", args.len()));
    };
    if let Some(x) = p.since {
        add("c.ts_ms >=", SqlVal::Integer(x));
    }
    if let Some(x) = p.until {
        add("c.ts_ms <", SqlVal::Integer(x));
    }
    if let Some(x) = p.book {
        add("c.book =", SqlVal::Text(x));
    }
    if let Some(x) = p.match_id {
        add("c.match_id =", SqlVal::Integer(x));
    }
    if let Some(x) = p.market {
        add("c.market =", SqlVal::Text(x));
    }
    if let Some(x) = p.outcome {
        add("c.outcome =", SqlVal::Text(x));
    }
    if let Some(x) = p.kind {
        add("c.kind =", SqlVal::Text(x));
    }
    if let Some(x) = p.line {
        add("c.line =", SqlVal::Real(x));
    }
    let asc = match p.order.as_deref() {
        Some("asc") => true,
        Some("desc") => false,
        _ => p.since.is_some(),
    };
    let limit = p.limit.unwrap_or(500).min(100_000);
    sql.push_str(if asc { " ORDER BY c.id ASC" } else { " ORDER BY c.id DESC" });
    sql.push_str(&format!(" LIMIT {limit}"));
    let path = app.db_path.clone();
    match tokio::task::spawn_blocking(move || store::query_json(&path, &sql, &args, limit)).await {
        Ok(Ok(rows)) => Json(json!({"count": rows.len(), "changes": rows})).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn changes(State(app): S, Query(p): Query<ChangesQ>) -> Response {
    run_changes(app, p).await
}

async fn match_changes(State(app): S, Path(id): Path<i64>, Query(mut p): Query<ChangesQ>) -> Response {
    p.match_id = Some(id);
    if p.order.is_none() {
        p.order = Some("asc".into());
    }
    if p.limit.is_none() {
        p.limit = Some(10_000);
    }
    run_changes(app, p).await
}

#[derive(Deserialize, Default)]
struct BoardQ {
    market: Option<String>,
    min_books: Option<usize>,
}

/// The live table: one row per match, each book's current prices for one market.
async fn board(State(app): S, Query(p): Query<BoardQ>) -> Response {
    let market = match Market::parse(p.market.as_deref().unwrap_or("1x2")) {
        Some(m) => m,
        None => return err(StatusCode::BAD_REQUEST, "market must be 1x2, total, handicap or btts"),
    };
    let eng = app.engine.lock();
    let mut by_match: BTreeMap<i64, Vec<(&'static str, &EventState)>> = BTreeMap::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            by_match.entry(es.match_id).or_default().push((*book, es));
        }
    }
    let mut rows = Vec::new();
    for (id, mut evs) in by_match {
        if evs.len() < p.min_books.unwrap_or(1) {
            continue;
        }
        if !eng.visible(id) {
            continue;
        }
        evs.sort_by_key(|x| x.0);
        let Some(m) = eng.matcher.matches.get(&id) else { continue };
        let mut per_book = serde_json::Map::new();
        let mut last_change: Ms = 0;
        for (book, es) in &evs {
            // line -> outcome -> price
            let mut lines: BTreeMap<String, serde_json::Map<String, Value>> = BTreeMap::new();
            for (k, q) in &es.quotes {
                if k.0 != market {
                    continue;
                }
                last_change = last_change.max(q.updated_ms);
                let lk = match line_from_key(k.1) {
                    Some(l) => format!("{l}"),
                    None => "-".into(),
                };
                lines.entry(lk).or_default().insert(k.2.as_str().into(), json!(q.price));
            }
            let v = if market == Market::X12 || market == Market::Btts {
                lines.remove("-").map(Value::Object).unwrap_or(Value::Null)
            } else {
                Value::Object(lines.into_iter().map(|(k, v)| (k, Value::Object(v))).collect())
            };
            per_book.insert(book.to_string(), v);
        }
        rows.push(json!({
            "id": id, "sport": m.sport, "home": m.home, "away": m.away, "league": m.league, "start_ms": m.start_ms,
            "score": evs.iter().find_map(|(_, e)| e.meta.score.clone()),
            "clock": evs.iter().find_map(|(_, e)| e.meta.clock.clone()),
            "last_change_ms": last_change,
            "live": evs.iter().any(|(_, e)| e.meta.live),
            "books": per_book,
        }));
    }
    rows.sort_by(|a, b| {
        let na = a["books"].as_object().map_or(0, |x| x.len());
        let nb = b["books"].as_object().map_or(0, |x| x.len());
        nb.cmp(&na).then(a["home"].as_str().cmp(&b["home"].as_str()))
    });
    let books: Vec<&str> = eng.books.keys().copied().collect();
    Json(json!({"now_ms": now_ms(), "market": market, "books": books, "rows": rows})).into_response()
}

#[derive(Deserialize, Default)]
struct StreamQ {
    #[serde(rename = "match")]
    match_id: Option<i64>,
    book: Option<String>,
    market: Option<String>,
}

async fn stream(State(app): S, Query(p): Query<StreamQ>) -> Sse<impl futures::Stream<Item = Result<Event, Infallible>>> {
    let rx = app.live.subscribe();
    let s = BroadcastStream::new(rx).filter_map(move |r| {
        let out = match r {
            Ok(c) => {
                let keep = p.match_id.map_or(true, |m| m == c.match_id)
                    && p.book.as_deref().map_or(true, |b| b == c.book)
                    && p.market.as_deref().map_or(true, |m| m == c.market.as_str());
                if keep {
                    Some(Ok(Event::default().event("change").data(serde_json::to_string(&*c).unwrap_or_default())))
                } else {
                    None
                }
            }
            // Slow client fell behind; tell it how many it missed instead of dropping silently.
            Err(tokio_stream::wrappers::errors::BroadcastStreamRecvError::Lagged(n)) => {
                Some(Ok(Event::default().event("lagged").data(n.to_string())))
            }
        };
        futures::future::ready(out)
    });
    Sse::new(s).keep_alive(KeepAlive::default())
}

#[derive(Deserialize)]
struct SqlQ {
    sql: String,
    limit: Option<usize>,
}

async fn query(State(app): S, Query(p): Query<SqlQ>) -> Response {
    let limit = p.limit.unwrap_or(1000).min(100_000);
    let path = app.db_path.clone();
    match tokio::task::spawn_blocking(move || store::query_json(&path, &p.sql, &[], limit)).await {
        Ok(Ok(rows)) => Json(json!({"count": rows.len(), "rows": rows})).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn stats(State(app): S) -> Response {
    let path = app.db_path.clone();
    let since = now_ms() - 5 * 60 * 1000;
    let res = tokio::task::spawn_blocking(move || -> anyhow::Result<Value> {
        let totals = store::query_json(
            &path,
            "SELECT (SELECT COUNT(*) FROM changes) AS changes, (SELECT COUNT(*) FROM matches) AS matches, (SELECT COUNT(*) FROM book_events) AS book_events",
            &[],
            1,
        )?;
        let per_book = store::query_json(
            &path,
            "SELECT book, COUNT(*) AS changes_5m, SUM(kind='move') AS moves_5m FROM changes WHERE ts_ms >= ?1 GROUP BY book ORDER BY 2 DESC",
            &[SqlVal::Integer(since)],
            100,
        )?;
        Ok(json!({"totals": totals.first(), "last_5_minutes": per_book}))
    })
    .await;
    match res {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn merge(State(app): S, Path((into, from)): Path<(i64, i64)>) -> Response {
    let res = app.engine.lock().merge(into, from);
    match res {
        Ok(writes) => {
            let n = writes.len();
            app.emit(writes).await;
            Json(json!({"ok": true, "moved_events": n})).into_response()
        }
        Err(e) => err(StatusCode::BAD_REQUEST, e),
    }
}

async fn unlink(State(app): S, Path((book, event_id)): Path<(String, String)>) -> Response {
    let res = app.engine.lock().unlink(&book, &event_id);
    match res {
        Ok((id, writes)) => {
            app.emit(writes).await;
            Json(json!({"ok": true, "new_match_id": id})).into_response()
        }
        Err(e) => err(StatusCode::BAD_REQUEST, e),
    }
}

#[derive(Deserialize, Default, Clone)]
pub struct LagQ {
    /// ms since epoch; default 24 h ago
    pub since: Option<i64>,
    pub until: Option<i64>,
    /// minimum implied-probability move that counts as a jump (default 0.04)
    min_jump: Option<f64>,
    /// how far either side of a jump to look for the other side's reaction (default 90)
    window_s: Option<i64>,
    /// how long a move may take to complete (default 10)
    settle_s: Option<i64>,
    /// "receive" (default: when we saw it) or "source" (each feed's own timestamp where it has one)
    clock: Option<String>,
    #[serde(rename = "match")]
    match_id: Option<i64>,
    book: Option<String>,
    trigger: Option<String>,
    trigger_book: Option<String>,
    follower_book: Option<String>,
    limit: Option<usize>,
    /// Rankings: time the prediction markets against this bookmaker instead of the window's
    /// own fastest (the match page passes the all-games fastest book).
    fastest: Option<String>,
    /// Rankings: one sport key or "esports".
    sport: Option<String>,
}

impl LagQ {
    pub fn params(&self) -> crate::lag::Params {
        let now = now_ms();
        crate::lag::Params {
            since: self.since.unwrap_or(now - 24 * 3600 * 1000),
            until: self.until.unwrap_or(now + 1),
            min_jump: self.min_jump.unwrap_or(0.04).max(0.005),
            window_ms: self.window_s.unwrap_or(90).clamp(5, 900) * 1000,
            settle_ms: self.settle_s.unwrap_or(10).clamp(1, 120) * 1000,
            source_clock: self.clock.as_deref() == Some("source"),
            match_id: self.match_id,
            book: self.book.clone(),
            fastest: self.fastest.clone().filter(|f| !f.is_empty()),
            sport: self.sport.clone().filter(|f| !f.is_empty() && f != "all"),
        }
    }
}

async fn lag_run(app: &Arc<App>, q: &LagQ) -> Result<Vec<crate::lag::LagEvent>, Response> {
    let path = app.db_path.clone();
    let p = q.params();
    match tokio::task::spawn_blocking(move || crate::lag::compute(&path, &p)).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}"))),
        Err(e) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

/// Per book: how long after a bookmaker jump Polymarket follows, and vice versa.
async fn lag_summary(State(app): S, Query(q): Query<LagQ>) -> Response {
    let evs = match lag_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let p = q.params();
    let matches: std::collections::BTreeSet<i64> = evs.iter().map(|e| e.match_id).collect();
    Json(json!({
        "since": p.since, "until": p.until, "min_jump": p.min_jump,
        "window_ms": p.window_ms, "settle_ms": p.settle_ms,
        "clock": if p.source_clock { "source" } else { "receive" },
        "matches": matches.len(),
        "sign": "delay_ms = polymarket time - book time; positive means Polymarket was later",
        "summary": crate::lag::summarize(&evs),
    }))
    .into_response()
}

async fn lag_events(State(app): S, Query(q): Query<LagQ>) -> Response {
    let mut evs = match lag_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(t) = &q.trigger {
        evs.retain(|e| e.trigger == t.as_str());
    }
    let n = evs.len();
    let limit = q.limit.unwrap_or(1000);
    if evs.len() > limit {
        evs.drain(..evs.len() - limit);
    }
    Json(json!({"count": n, "events": evs})).into_response()
}

async fn lag_books_run(app: &Arc<App>, q: &LagQ) -> Result<Vec<crate::lag::BookLagEvent>, Response> {
    let path = app.db_path.clone();
    let p = q.params();
    match tokio::task::spawn_blocking(move || crate::lag::compute_books(&path, &p)).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}"))),
        Err(e) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

/// Per (trigger, follower) book pair: how long after one book jumps the other follows.
async fn lag_books_summary(State(app): S, Query(q): Query<LagQ>) -> Response {
    let evs = match lag_books_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let p = q.params();
    let matches: std::collections::BTreeSet<i64> = evs.iter().map(|e| e.match_id).collect();
    Json(json!({
        "since": p.since, "until": p.until, "min_jump": p.min_jump,
        "window_ms": p.window_ms, "settle_ms": p.settle_ms,
        "clock": if p.source_clock { "source" } else { "receive" },
        "matches": matches.len(),
        "sign": "delay_ms = follower time - trigger time; positive means the follower was later",
        "summary": crate::lag::summarize_books(&evs),
    }))
    .into_response()
}

async fn lag_books_events(State(app): S, Query(q): Query<LagQ>) -> Response {
    let mut evs = match lag_books_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(t) = &q.trigger_book {
        evs.retain(|e| e.trigger_book == *t);
    }
    if let Some(f) = &q.follower_book {
        evs.retain(|e| e.follower_book == *f);
    }
    let n = evs.len();
    let limit = q.limit.unwrap_or(1000);
    if evs.len() > limit {
        evs.drain(..evs.len() - limit);
    }
    Json(json!({"count": n, "events": evs})).into_response()
}

/// Web files are compiled in. Set ODDSFEED_WEB_DIR=<repo>/src/web to serve them from disk while editing.
fn asset(name: &'static str, ct: &'static str, body: &'static str) -> Response {
    let headers = [(axum::http::header::CONTENT_TYPE, ct), (axum::http::header::CACHE_CONTROL, "no-cache")];
    if let Ok(dir) = std::env::var("ODDSFEED_WEB_DIR") {
        if let Ok(text) = std::fs::read_to_string(std::path::Path::new(&dir).join(name)) {
            return (headers, text).into_response();
        }
    }
    (headers, body).into_response()
}

/// Every current quote as flat rows: the spreadsheet view.
async fn quotes(State(app): S) -> Json<Value> {
    let eng = app.engine.lock();
    let mut rows = Vec::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            if !eng.visible(es.match_id) {
                continue;
            }
            let m = eng.matcher.matches.get(&es.match_id);
            for (k, q) in &es.quotes {
                rows.push(json!([
                    es.match_id,
                    m.map(|m| m.sport.as_str()).unwrap_or(&es.meta.sport),
                    m.map(|m| m.home.as_str()).unwrap_or(&es.meta.home),
                    m.map(|m| m.away.as_str()).unwrap_or(&es.meta.away),
                    es.meta.league,
                    es.meta.score,
                    es.meta.clock,
                    book,
                    k.0,
                    line_from_key(k.1),
                    k.2,
                    q.price,
                    q.prev,
                    q.updated_ms,
                    q.book_ts,
                    es.meta.live,
                    m.and_then(|m| m.start_ms).or(es.meta.start_ms),
                ]));
            }
        }
    }
    Json(json!({
        "now_ms": now_ms(),
        "columns": ["match_id","sport","home","away","league","score","clock","book","market","line","outcome","price","prev","updated_ms","book_ts_ms","live","start_ms"],
        "rows": rows,
    }))
}

/// Service facts for Settings / Dashboard: uptime, database size, clock, config.
async fn info(State(app): S) -> Json<Value> {
    let db = &app.db_path;
    let size: u64 = ["", "-wal", "-shm"]
        .iter()
        .filter_map(|s| std::fs::metadata(format!("{}{}", db.display(), s)).ok())
        .map(|m| m.len())
        .sum();
    Json(json!({
        "version": env!("CARGO_PKG_VERSION"),
        "started_ms": app.started_ms,
        "now_ms": now_ms(),
        "db_path": std::fs::canonicalize(db).unwrap_or(db.clone()).display().to_string().trim_start_matches(r"\\?\").to_string(),
        "db_bytes": size,
        "bind": app.cfg.bind,
        "include_esoccer": app.cfg.include_esoccer,
        "prediction_only": app.engine.lock().prediction_only,
        "prematch_hours": app.cfg.prematch_hours,
        "clock": crate::clock::status(),
    }))
}

#[derive(Deserialize)]
struct ControlQ {
    paused: Option<bool>,
    poll_ms: Option<u64>,
}

async fn control_book(State(app): S, Path(name): Path<String>, Json(b): Json<ControlQ>) -> Response {
    match app.engine.lock().control(&name, b.paused, b.poll_ms) {
        Some(st) => Json(json!({"ok": true, "status": st})).into_response(),
        None => err(StatusCode::NOT_FOUND, "unknown source"),
    }
}

async fn pm_settings_get(State(app): S) -> Json<Value> {
    Json(crate::pmacct::public_view(&crate::pmacct::load(&app.db_path)))
}

async fn pm_settings_post(State(app): S, Json(edit): Json<Value>) -> Response {
    let c = crate::pmacct::merge(crate::pmacct::load(&app.db_path), &edit);
    match crate::pmacct::save(&app.db_path, &c) {
        Ok(()) => Json(crate::pmacct::public_view(&c)).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
    }
}

async fn pm_account(State(app): S) -> Json<Value> {
    let c = crate::pmacct::load(&app.db_path);
    let http = crate::books::http_client();
    Json(crate::pmacct::snapshot(&http, &c).await)
}

#[derive(Deserialize)]
struct ScopeQ {
    prediction_only: bool,
}

/// Switch between "only matches on Polymarket/Kalshi plus a bookmaker" and "everything".
async fn control_scope(State(app): S, Json(b): Json<ScopeQ>) -> Json<Value> {
    let mut eng = app.engine.lock();
    eng.prediction_only = b.prediction_only;
    eng.qualified.clear();
    Json(json!({"ok": true, "prediction_only": eng.prediction_only}))
}

async fn kalshi_settings_get(State(app): S) -> Json<Value> {
    Json(crate::kalacct::public_view(&crate::kalacct::load(&app.db_path)))
}

async fn kalshi_settings_post(State(app): S, Json(edit): Json<Value>) -> Response {
    let c = crate::kalacct::merge(crate::kalacct::load(&app.db_path), &edit);
    match crate::kalacct::save(&app.db_path, &c) {
        Ok(()) => Json(crate::kalacct::public_view(&c)).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
    }
}

async fn kalshi_account(State(app): S) -> Json<Value> {
    let c = crate::kalacct::load(&app.db_path);
    let http = crate::books::http_client();
    Json(crate::kalacct::snapshot(&http, &c).await)
}

// ---------------------------------------------------------------- Telegram, arbitrage, market size

async fn tg_get(State(app): S) -> Json<Value> {
    Json(crate::telegram::view(&app))
}

async fn tg_post(State(app): S, Json(edit): Json<Value>) -> Response {
    match crate::telegram::edit(&edit) {
        Ok(()) => Json(crate::telegram::view(&app)).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

async fn tg_test(State(app): S) -> Response {
    match crate::telegram::test(&app).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

#[derive(Deserialize)]
struct WatchB {
    on: bool,
}

async fn tg_watch(Path(id): Path<i64>, Json(b): Json<WatchB>) -> Json<Value> {
    Json(json!({"id": id, "on": crate::telegram::set_watch(id, b.on), "watch": crate::telegram::watched()}))
}

#[derive(Deserialize)]
struct ArbQ {
    /// Smallest return to list, as a fraction (default 0; negative shows near-misses).
    min_profit: Option<f64>,
    stake: Option<f64>,
}

/// Pre-match arbitrage across every source (see arb.rs).
async fn arbs(State(app): S, Query(q): Query<ArbQ>) -> Response {
    // The live tracker scans every 5 s (near misses down to -3%); serve its latest scan.
    let _ = app;
    let (min, stake) = (q.min_profit.unwrap_or(0.0).clamp(-0.03, 1.0), q.stake.unwrap_or(100.0).clamp(1.0, 1e7));
    Json(crate::arbtrack::current(min, stake)).into_response()
}

#[derive(Deserialize)]
struct ArbHistQ {
    limit: Option<i64>,
}

/// Finished arbitrages and how long they lasted.
async fn arbs_history(State(app): S, Query(q): Query<ArbHistQ>) -> Response {
    let limit = q.limit.unwrap_or(100).clamp(1, 2000);
    match tokio::task::spawn_blocking(move || crate::arbtrack::history(&app, limit)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Per match, the size and activity of its Polymarket and Kalshi listings: liquidity and
/// volume (from the sources), price changes in the last hour, and the cheapest outcome's price.
async fn pred_meta(State(app): S) -> Response {
    static CACHE: std::sync::LazyLock<parking_lot::Mutex<Option<(Ms, Value)>>> = std::sync::LazyLock::new(Default::default);
    if let Some((t, v)) = CACHE.lock().clone() {
        if now_ms() - t < 20_000 {
            return Json(v).into_response();
        }
    }
    // (match, book) -> (event id, cheapest outcome probability)
    let mut listed: Vec<(i64, &'static str, String, Option<f64>)> = Vec::new();
    {
        let eng = app.engine.lock();
        for book in ["polymarket", "kalshi"] {
            let Some(bs) = eng.books.get(book) else { continue };
            for es in bs.events.values() {
                let min_p = es.quotes.iter().filter(|(k, _)| k.0 == Market::X12).filter_map(|(_, q)| q.price.filter(|p| *p > 1.0).map(|p| 1.0 / p)).fold(None, |m: Option<f64>, p| Some(m.map_or(p, |m| m.min(p))));
                listed.push((es.match_id, book, es.meta.id.clone(), min_p));
            }
        }
    }
    let path = app.db_path.clone();
    let since = now_ms() - 3_600_000;
    let counts: std::collections::HashMap<(i64, String), i64> = tokio::task::spawn_blocking(move || -> std::collections::HashMap<(i64, String), i64> {
        let Ok(c) = crate::store::open_ro(&path) else { return Default::default() };
        let Ok(mut st) = c.prepare("SELECT match_id, book, COUNT(*) FROM changes WHERE book IN ('polymarket', 'kalshi') AND ts_ms > ?1 AND kind = 'move' GROUP BY match_id, book") else { return Default::default() };
        st.query_map([since], |r| Ok(((r.get::<_, i64>(0)?, r.get::<_, String>(1)?), r.get::<_, i64>(2)?)))
            .map(|it| it.filter_map(|x| x.ok()).collect())
            .unwrap_or_default()
    })
    .await
    .unwrap_or_default();
    let mut by_match: serde_json::Map<String, Value> = serde_json::Map::new();
    for (mid, book, ev, min_p) in listed {
        let size = crate::predmeta::get(book, &ev).unwrap_or_default();
        let entry = by_match.entry(mid.to_string()).or_insert_with(|| json!({}));
        entry[book] = json!({
            "liquidity": size.liquidity,
            "volume": size.volume,
            "volume_24h": size.volume_24h,
            "moves_1h": counts.get(&(mid, book.to_string())).copied().unwrap_or(0),
            "min_price": min_p,
        });
    }
    let v = json!({"now_ms": now_ms(), "matches": by_match});
    *CACHE.lock() = Some((now_ms(), v.clone()));
    Json(v).into_response()
}

#[derive(Deserialize)]
struct SuspQ {
    days: Option<i64>,
}

/// Heavily bet lower-tier CS2 games and pre-match line moves (see suspect.rs).
async fn suspicious(State(app): S, Query(q): Query<SuspQ>) -> Response {
    let days = q.days.unwrap_or(7);
    match tokio::task::spawn_blocking(move || crate::suspect::view(&app, days)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn suspicious_event(State(app): S, Path(id): Path<String>) -> Response {
    match crate::suspect::event_detail(&app, &id).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::BAD_GATEWAY, format!("{e:#}")),
    }
}

async fn suspicious_settings(Json(edit): Json<Value>) -> Response {
    match crate::suspect::save_settings(&edit) {
        Ok(s) => Json(json!({"settings": s})).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

#[derive(Deserialize)]
struct SearchQ {
    q: String,
}

/// Site search: matches (live, upcoming and recent) and leagues by name.
async fn search(State(app): S, Query(q): Query<SearchQ>) -> Response {
    let term = q.q.trim().to_lowercase().replace(['%', '_'], "");
    if term.chars().count() < 2 {
        return Json(json!({"matches": [], "leagues": []})).into_response();
    }
    let (path, like, now) = (app.db_path.clone(), format!("%{term}%"), now_ms());
    let res = tokio::task::spawn_blocking(move || -> anyhow::Result<(Vec<Value>, Vec<Value>)> {
        let c = crate::store::open_ro(&path)?;
        let mut st = c.prepare(
            "SELECT id, sport, home, away, league, start_ms FROM matches
             WHERE lower(home) LIKE ?1 OR lower(away) LIKE ?1 OR lower(league) LIKE ?1
             ORDER BY abs(COALESCE(start_ms, created_ms) - ?2) LIMIT 30",
        )?;
        let m = st
            .query_map(rusqlite::params![like, now], |r| {
                Ok(json!({"id": r.get::<_, i64>(0)?, "sport": r.get::<_, String>(1)?, "home": r.get::<_, String>(2)?, "away": r.get::<_, String>(3)?,
                    "league": r.get::<_, Option<String>>(4)?, "start_ms": r.get::<_, Option<i64>>(5)?}))
            })?
            .filter_map(|x| x.ok())
            .collect();
        let mut st = c.prepare("SELECT league, sport, COUNT(*) n FROM matches WHERE league IS NOT NULL AND lower(league) LIKE ?1 GROUP BY league ORDER BY n DESC LIMIT 8")?;
        let l = st
            .query_map(rusqlite::params![like], |r| Ok(json!({"league": r.get::<_, String>(0)?, "sport": r.get::<_, String>(1)?, "matches": r.get::<_, i64>(2)?})))?
            .filter_map(|x| x.ok())
            .collect();
        Ok((m, l))
    })
    .await;
    match res {
        Ok(Ok((mut m, l))) => {
            let eng = app.engine.lock();
            let live: std::collections::HashSet<i64> = eng.books.values().flat_map(|b| b.events.values().filter(|e| e.meta.live).map(|e| e.match_id)).collect();
            let open: std::collections::HashSet<i64> = eng.books.values().flat_map(|b| b.events.values().map(|e| e.match_id)).collect();
            for x in &mut m {
                let id = x["id"].as_i64().unwrap_or(0);
                x["live"] = json!(live.contains(&id));
                x["listed"] = json!(open.contains(&id));
            }
            Json(json!({"matches": m, "leagues": l})).into_response()
        }
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

#[derive(Deserialize)]
struct DataQ {
    fresh: Option<bool>,
}

/// Tables, rows and bytes on disk (cached 10 min; `fresh=true` recomputes).
async fn data_stats(State(app): S, Query(q): Query<DataQ>) -> Response {
    match tokio::task::spawn_blocking(move || crate::data::stats(&app, q.fresh.unwrap_or(false))).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn data_scan(State(app): S) -> Response {
    let a2 = app.clone();
    match tokio::task::spawn_blocking(move || crate::data::scan(&a2)).await {
        Ok(v) => Json(json!({"folders": crate::data::cfg(&app).folders, "files": v})).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn data_folders(State(app): S, Json(c): Json<crate::data::DataCfg>) -> Response {
    match crate::data::save_cfg(&app, &c) {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

async fn data_export(State(app): S) -> Response {
    match crate::data::export(app) {
        Ok(p) => Json(json!({"ok": true, "path": p})).into_response(),
        Err(e) => err(StatusCode::CONFLICT, format!("{e:#}")),
    }
}

#[derive(Deserialize)]
struct MergeB {
    path: String,
}

async fn data_merge(State(app): S, Json(b): Json<MergeB>) -> Response {
    match crate::data::merge(app, b.path) {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

/// Logo and source icons, compiled in.
async fn img(Path(name): Path<String>) -> Response {
    let bytes: Option<&'static [u8]> = match name.as_str() {
        "logo.png" => Some(include_bytes!("web/img/logo.png")),
        "favicon.png" => Some(include_bytes!("web/img/favicon.png")),
        "polymarket.png" => Some(include_bytes!("web/img/polymarket.png")),
        "unibet.png" => Some(include_bytes!("web/img/unibet.png")),
        "sportybet.png" => Some(include_bytes!("web/img/sportybet.png")),
        "betpawa.png" => Some(include_bytes!("web/img/betpawa.png")),
        "betika.png" => Some(include_bytes!("web/img/betika.png")),
        "linebet.png" => Some(include_bytes!("web/img/linebet.png")),
        "fonbet.png" => Some(include_bytes!("web/img/fonbet.png")),
        "vinn.png" => Some(include_bytes!("web/img/vinn.png")),
        "kalshi.png" => Some(include_bytes!("web/img/kalshi.png")),
        "pinnacle.png" => Some(include_bytes!("web/img/pinnacle.png")),
        "m88.png" => Some(include_bytes!("web/img/m88.png")),
        "melbet.png" => Some(include_bytes!("web/img/melbet.png")),
        "betwinner.png" => Some(include_bytes!("web/img/betwinner.png")),
        "megapari.png" => Some(include_bytes!("web/img/megapari.png")),
        "leovegas.png" => Some(include_bytes!("web/img/leovegas.png")),
        "paf.png" => Some(include_bytes!("web/img/paf.png")),
        "thunderpick.png" => Some(include_bytes!("web/img/thunderpick.png")),
        "novibet.png" => Some(include_bytes!("web/img/novibet.png")),
        "paripesa.png" => Some(include_bytes!("web/img/paripesa.png")),
        "22bet.png" => Some(include_bytes!("web/img/22bet.png")),
        "22bit.png" => Some(include_bytes!("web/img/22bit.png")),
        "1xbit.png" => Some(include_bytes!("web/img/1xbit.png")),
        "betking.png" => Some(include_bytes!("web/img/betking.png")),
        "olimp.png" => Some(include_bytes!("web/img/olimp.png")),
        "duel.png" => Some(include_bytes!("web/img/duel.png")),
        "mostbet.png" => Some(include_bytes!("web/img/mostbet.png")),
        "leon.png" => Some(include_bytes!("web/img/leon.png")),
        "marathon.png" => Some(include_bytes!("web/img/marathon.png")),
        "betcity.png" => Some(include_bytes!("web/img/betcity.png")),
        "pari.png" => Some(include_bytes!("web/img/pari.png")),
        "game-cs2.png" => Some(include_bytes!("web/img/game-cs2.png")),
        "game-dota2.png" => Some(include_bytes!("web/img/game-dota2.png")),
        "game-lol.png" => Some(include_bytes!("web/img/game-lol.png")),
        "game-valorant.png" => Some(include_bytes!("web/img/game-valorant.png")),
        "game-ow.png" => Some(include_bytes!("web/img/game-ow.png")),
        "game-rl.png" => Some(include_bytes!("web/img/game-rl.png")),
        "game-r6.png" => Some(include_bytes!("web/img/game-r6.png")),
        "game-cod.png" => Some(include_bytes!("web/img/game-cod.png")),
        "game-sc2.png" => Some(include_bytes!("web/img/game-sc2.png")),
        "game-mlbb.png" => Some(include_bytes!("web/img/game-mlbb.png")),
        "game-hok.png" => Some(include_bytes!("web/img/game-hok.png")),
        "game-pubg.png" => Some(include_bytes!("web/img/game-pubg.png")),
        _ => None,
    };
    match bytes {
        Some(b) => ([(axum::http::header::CONTENT_TYPE, "image/png"), (axum::http::header::CACHE_CONTROL, "max-age=86400")], b).into_response(),
        None => err(StatusCode::NOT_FOUND, "no such image"),
    }
}

/// Who moves first across all sources, plus how often Polymarket can still be bought before it reacts.
/// Served from a cache (see snipe.rs): instant after the first load, refreshed in the background.
async fn rankings(State(app): S, Query(q): Query<LagQ>) -> Response {
    let limit = q.limit.unwrap_or(400);
    match crate::snipe::rankings_cached(&app, &q).await {
        Ok((r, age_ms)) => {
            let n = r.events.len();
            let mut v = serde_json::to_value(&*r).unwrap_or(Value::Null);
            if let Some(ev) = v["events"].as_array_mut() {
                if n > limit {
                    ev.drain(..n - limit);
                }
            }
            Json(json!({"since": q.params().since, "event_count": n, "age_ms": age_ms, "rankings": v})).into_response()
        }
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e),
    }
}

/// The adaptive paper bot: current rules, the 4-hourly re-choices and the walk-forward record.
async fn adaptive(State(app): S) -> Response {
    match tokio::task::spawn_blocking(move || crate::adaptive::view(&app)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

#[derive(Deserialize)]
struct DumpQ {
    reaction: Option<i64>,
    confirm: Option<i64>,
}

/// Research: every move re-measured with another reaction / confirm delay.
async fn adaptive_dump(State(app): S, Query(q): Query<DumpQ>) -> Response {
    let (r, c) = (q.reaction.unwrap_or(1000), q.confirm.unwrap_or(500));
    match tokio::task::spawn_blocking(move || crate::adaptive::dump(&app, r, c)).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Order button on the Arbitrage Scout: paper-take the Polymarket x Kalshi arbitrage of one game.
async fn predarb_order(State(app): S, Path(id): Path<i64>) -> Response {
    match tokio::task::spawn_blocking(move || crate::predarb::order(&app, id)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Sell an open paper arbitrage at the latest prices.
async fn predarb_sell(State(app): S, Path((ts, id)): Path<(i64, i64)>) -> Response {
    match tokio::task::spawn_blocking(move || crate::predarb::sell(&app, ts, id)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Bots page: both bots, their wallets, the shared caps, the Real switches.
async fn bots_get(State(app): S) -> Response {
    match tokio::task::spawn_blocking(move || crate::bots::view(&app)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn bots_settings(State(app): S, Json(edit): Json<Value>) -> Response {
    match tokio::task::spawn_blocking(move || crate::bots::update(&app, &edit)).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

#[derive(Deserialize)]
struct SimQ2 {
    kind: String,
}

/// Fetch (or create) the Polymarket API key, secret and passphrase that belong to the saved
/// trading key, and save them. Pressed by the user on the Bots page.
async fn pm_api_from_key(State(app): S) -> Response {
    let mut c = crate::pmacct::load(&app.db_path);
    if crate::exec::signer_address(&c.private_key).is_err() {
        return err(StatusCode::BAD_REQUEST, "save the trading key first");
    }
    match crate::exec::pm_api_from_key(&crate::books::http_client(), &c.private_key).await {
        Ok((k, s, p)) => {
            c.api_key = k;
            c.api_secret = s;
            c.passphrase = p;
            if c.signer.trim().is_empty() {
                c.signer = crate::exec::signer_address(&c.private_key).unwrap_or_default();
            }
            match crate::pmacct::save(&app.db_path, &c) {
                Ok(()) => Json(crate::pmacct::public_view(&c)).into_response(),
                Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
            }
        }
        Err(e) => err(StatusCode::BAD_GATEWAY, format!("{e:#}")),
    }
}

async fn bots_runs(State(app): S) -> Response {
    Json(tokio::task::spawn_blocking(move || crate::bots::runs(&app)).await.unwrap_or_default()).into_response()
}

/// Start a timed REAL test run of the snipe bot: {"hours": 4|8|16|24, "confirm": "REAL MONEY"}.
async fn bots_run_start(State(app): S, Json(b): Json<Value>) -> Response {
    let (h, conf) = (b["hours"].as_i64().unwrap_or(0), b["confirm"].as_str().unwrap_or("").to_string());
    match tokio::task::spawn_blocking(move || crate::bots::start_run(&app, h, &conf)).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn bots_run_stop(State(app): S) -> Response {
    match tokio::task::spawn_blocking(move || crate::bots::stop_run(&app, "stopped by hand")).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

#[derive(Deserialize)]
struct ChartQ {
    m: i64,
    o: String,
    from: i64,
    to: i64,
}

async fn bots_run_chart(State(app): S, Query(q): Query<ChartQ>) -> Response {
    Json(tokio::task::spawn_blocking(move || crate::bots::chart(&app, q.m, &q.o, q.from, q.to)).await.unwrap_or_default()).into_response()
}

/// Past test runs and the delays they measured.
async fn bots_tests(State(app): S) -> Response {
    match tokio::task::spawn_blocking(move || crate::diag::view(&app)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Sell back what an incomplete test left on the account.
async fn bots_test_sellback(State(app): S, Path(id): Path<i64>) -> Response {
    match crate::diag::sell_back(app, id).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

/// One REAL test round trip (smallest stakes): {"confirm": "TEST"}.
async fn bots_test(State(app): S, Path(bot): Path<String>, Json(b): Json<Value>) -> Response {
    if b["confirm"].as_str().map(str::trim) != Some("TEST") {
        return err(StatusCode::BAD_REQUEST, "type TEST to confirm a real test order");
    }
    match crate::diag::run(app, &bot).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

/// The shared real accounts: cash and positions vs what both bots believe they hold.
async fn bots_accounts(State(app): S) -> Response {
    Json(crate::bots::accounts(app).await).into_response()
}

/// Whole-dataset simulation of a bot with its current settings (cached; poll until done).
async fn bots_sim(State(app): S, Query(q): Query<SimQ2>) -> Response {
    Json(crate::bots::sim(&app, &q.kind)).into_response()
}

/// Real switch: {"real": true, "confirm": "REAL MONEY"} or {"real": false}.
async fn bots_real(State(app): S, Path(bot): Path<String>, Json(b): Json<Value>) -> Response {
    let real = b["real"].as_bool().unwrap_or(false);
    let confirm = b["confirm"].as_str().unwrap_or("").to_string();
    match tokio::task::spawn_blocking(move || crate::bots::set_real(&app, &bot, real, &confirm)).await {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Paper Polymarket x Kalshi arbitrage.
async fn predarb(State(app): S) -> Response {
    match tokio::task::spawn_blocking(move || crate::predarb::view(&app)).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

/// Paper bot balance settings: {start, pct}. A change recomputes the bot's history.
async fn adaptive_settings(State(app): S, Json(edit): Json<Value>) -> Response {
    match crate::adaptive::save_settings(&app, &edit) {
        Ok(v) => Json(v).into_response(),
        Err(e) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
    }
}

/// Paper trading on bookmaker-led moves against Polymarket and Kalshi (see snipe.rs).
async fn snipe(State(app): S, Query(q): Query<LagQ>, Query(s): Query<crate::snipe::SimQ>) -> Response {
    match crate::snipe::simulate_cached(&app, &q, &s).await {
        Ok(v) => Json((*v).clone()).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e),
    }
}

async fn ui() -> Html<&'static str> {
    Html(include_str!("ui.html"))
}
