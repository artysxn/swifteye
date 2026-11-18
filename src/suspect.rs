//! Suspicious-match detection, two independent signals.
//!
//! ## A. Heavily bet lower-tier CS2 games (Polymarket)
//! From the earlier study of the user's Fixed / Clean marks: the best single factor was how
//! heavily a game was bet, not any kind of account or bet. Per game, from Polymarket's public
//! trade history (all markets of the event: match, maps, totals), fills merged into bets
//! (same wallet, market, outcome and side within 60 s):
//! * total money traded, number of big underdog bets (>= `big_bet_usd` bought below
//!   `underdog_below`), number of big favourite bets (>= `big_bet_usd` bought at 50%+), the share
//!   of the money in big bets, and the match price drift from the first trade to the start.
//! Each is turned into a percentile against "normal" games: finished lower-tier CS2 games of the
//! last `baseline_days`. Score = average percentile (drift counts half; it was the weakest).
//! Tier 1 is skipped: a VRS top-`vrs_top` team, or both teams in the top `vrs_both`.
//! Caveats from that study: partly circular (marks were made on activity charts), and the money
//! in Fixed games did not know the result. "Heavily bet lower-tier game" is what this flags.
//!
//! ## B. Pre-match line moves (bookmakers, every sport)
//! For games that have not started, each independent bookmaker platform's first price vs its
//! current price, margin removed. Flagged when the median platform moved an outcome by
//! `line_move_pts` points or more and at least `line_min_platforms` moved it the same way by
//! half that. 1.85 / 1.85 -> 2.50 / 1.66 is a 10-point move.

use crate::books::{fetch_json, int, num, text};
use crate::model::*;
use crate::App;
use anyhow::Result;
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const GAMMA: &str = "https://gamma-api.polymarket.com";
const DATA: &str = "https://data-api.polymarket.com/v2";
const CS2_TAG: &str = "100780";
const VRS_REPO: &str = "https://api.github.com/repos/ValveSoftware/counter-strike_regional_standings/contents/live";

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct Settings {
    pub big_bet_usd: f64,
    pub underdog_below: f64,
    /// Alert (and highlight) at this score or more (0-100).
    pub alert_score: f64,
    pub vrs_top: i64,
    pub vrs_both: i64,
    pub baseline_days: i64,
    pub line_move_pts: f64,
    pub line_min_platforms: usize,
    /// Only games starting within this many hours.
    pub line_window_h: f64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { big_bet_usd: 200.0, underdog_below: 0.40, alert_score: 80.0, vrs_top: 20, vrs_both: 30, baseline_days: 14, line_move_pts: 8.0, line_min_platforms: 2, line_window_h: 24.0 }
    }
}

#[derive(Default)]
struct State {
    settings: Settings,
    path: PathBuf,
    /// Normalised team name -> global VRS rank.
    vrs: HashMap<String, i64>,
    vrs_date: String,
    /// Event id -> markets (condition id, is the match market).
    markets: HashMap<String, Vec<(String, bool)>>,
    scans: u64,
    last_error: Option<String>,
    /// Alerts already sent: key -> value at the time (score or move).
    alerted: HashMap<String, f64>,
}

static ST: LazyLock<Mutex<State>> = LazyLock::new(Default::default);

pub fn settings() -> Settings {
    ST.lock().settings.clone()
}

pub fn save_settings(edit: &Value) -> Result<Settings> {
    let mut st = ST.lock();
    let mut v = serde_json::to_value(&st.settings)?;
    if let (Some(d), Some(e)) = (v.as_object_mut(), edit.as_object()) {
        for (k, x) in e {
            d.insert(k.clone(), x.clone());
        }
    }
    let s: Settings = serde_json::from_value(v)?;
    std::fs::write(&st.path, serde_json::to_string_pretty(&s)?)?;
    st.settings = s.clone();
    Ok(s)
}

fn conn(app: &App) -> rusqlite::Result<Connection> {
    let c = Connection::open(&app.db_path)?;
    c.busy_timeout(Duration::from_secs(10))?;
    Ok(c)
}

/// Team name as a VRS lookup key: lowercase letters and digits, filler words dropped.
fn team_key(s: &str) -> String {
    let s = deunicode::deunicode(s).to_lowercase();
    let s: String = s.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { ' ' }).collect();
    s.split_whitespace().filter(|w| !matches!(*w, "team" | "esports" | "esport" | "gaming" | "gg" | "club" | "the")).collect::<Vec<_>>().join("")
}

/// Short names Polymarket uses for teams Valve lists in full.
const ALIASES: &[(&str, &str)] = &[
    ("nip", "ninjasinpyjamas"),
    ("navi", "natusvincere"),
    ("vp", "virtuspro"),
    ("col", "complexity"),
    ("eg", "evilgeniuses"),
    ("tl", "liquid"),
    ("mibr", "madeinbrazil"),
    ("ef", "eternalfire"),
    ("gl", "gamerlegion"),
    ("bb", "betboom"),
    ("ihc", "ihcesports"),
];

fn rank(name: &str) -> i64 {
    let st = ST.lock();
    let k = team_key(name);
    if let Some(r) = st.vrs.get(&k) {
        return *r;
    }
    if let Some((_, full)) = ALIASES.iter().find(|(a, _)| *a == k) {
        if let Some(r) = st.vrs.get(*full) {
            return *r;
        }
    }
    // "Inner Circle" vs "Inner Circle Esports" style differences: a unique prefix match.
    if k.len() >= 5 {
        let hits: Vec<i64> = st.vrs.iter().filter(|(v, _)| v.starts_with(&k) || k.starts_with(v.as_str()) && v.len() >= 5).map(|(_, r)| *r).collect();
        if hits.len() == 1 {
            return hits[0];
        }
    }
    0
}

/// Tier 1 = a VRS top-`vrs_top` team, or both in the top `vrs_both` (0 = unranked).
fn is_tier1(a: i64, b: i64, s: &Settings) -> bool {
    (a > 0 && a <= s.vrs_top) || (b > 0 && b <= s.vrs_top) || (a > 0 && b > 0 && a <= s.vrs_both && b <= s.vrs_both)
}

pub fn spawn(app: Arc<App>) {
    {
        let mut st = ST.lock();
        st.path = app.db_path.parent().map(|d| d.join("suspicious.json")).unwrap_or_else(|| PathBuf::from("suspicious.json"));
        st.settings = std::fs::read_to_string(&st.path).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    }
    let a = app.clone();
    tokio::spawn(async move {
        let http = crate::books::http_client();
        let mut tick: u64 = 0;
        loop {
            if tick % 240 == 0 {
                if let Err(e) = sync_vrs(&http).await {
                    tracing::warn!("suspicious: VRS sync: {e:#}");
                }
            }
            if let Err(e) = cs2_round(&a, &http, tick == 0).await {
                tracing::warn!("suspicious: CS2 activity: {e:#}");
                ST.lock().last_error = Some(format!("{e:#}"));
            }
            ST.lock().scans += 1;
            tick += 1;
            tokio::time::sleep(Duration::from_secs(180)).await;
        }
    });
    // Kalshi's CS2 game list (for game pages' Kalshi history), built off the request path.
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_secs(5)).await;
        let http = crate::books::http_client();
        loop {
            kalshi_cs2_events(&http).await;
            tokio::time::sleep(Duration::from_secs(25 * 60)).await;
        }
    });
    // Line moves on their own clock: the CS2 backfill can take minutes.
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(30)).await;
        loop {
            let a2 = app.clone();
            let _ = tokio::task::spawn_blocking(move || line_round(&a2)).await;
            tokio::time::sleep(Duration::from_secs(60)).await;
        }
    });
}

// ---------------------------------------------------------------- VRS

async fn sync_vrs(http: &reqwest::Client) -> Result<()> {
    let year = time::OffsetDateTime::now_utc().year();
    let mut files: Vec<(String, String)> = Vec::new();
    for y in [year, year - 1] {
        let (v, _) = fetch_json(http.get(format!("{VRS_REPO}/{y}")).header("Accept", "application/vnd.github+json")).await?;
        for f in v.as_array().into_iter().flatten() {
            let name = text(&f["name"]);
            if name.starts_with("standings_global_") && name.ends_with(".md") {
                files.push((name, text(&f["download_url"])));
            }
        }
        if !files.is_empty() {
            break;
        }
    }
    files.sort();
    let (name, url) = files.pop().ok_or_else(|| anyhow::anyhow!("no global standings file"))?;
    let md = http.get(url).send().await?.text().await?;
    let mut map = HashMap::new();
    for line in md.lines() {
        let cols: Vec<&str> = line.split('|').map(|c| c.trim()).collect();
        if cols.len() < 4 {
            continue;
        }
        let Ok(r) = cols[1].parse::<i64>() else { continue };
        let k = team_key(cols[3]);
        if !k.is_empty() {
            map.entry(k).or_insert(r);
        }
    }
    if map.len() < 50 {
        anyhow::bail!("{name}: only {} teams parsed", map.len());
    }
    let date = name.trim_start_matches("standings_global_").trim_end_matches(".md").replace('_', "-");
    tracing::info!("suspicious: VRS {date}, {} teams", map.len());
    let mut st = ST.lock();
    st.vrs = map;
    st.vrs_date = date;
    Ok(())
}

// ---------------------------------------------------------------- A. CS2 activity

/// "Counter-Strike: A vs B (BO3) - League" -> (A, B, League)
fn parse_title(t: &str) -> Option<(String, String, String)> {
    let t = t.trim_start_matches("Counter-Strike:").trim();
    let (teams, league) = t.split_once(" - ").unwrap_or((t, ""));
    let teams = teams.split(" (BO").next().unwrap_or(teams);
    let (a, b) = teams.split_once(" vs. ").or_else(|| teams.split_once(" vs "))?;
    Some((a.trim().to_string(), b.trim().to_string(), league.trim().to_string()))
}

fn rfc_ms(s: &str) -> Option<Ms> {
    time::OffsetDateTime::parse(s, &time::format_description::well_known::Rfc3339).ok().map(|t| (t.unix_timestamp_nanos() / 1_000_000) as Ms)
}

struct Ev {
    id: String,
    slug: String,
    title: String,
    home: String,
    away: String,
    league: String,
    start_ms: Ms,
    markets: Vec<(String, bool)>,
}

fn parse_event(e: &Value) -> Option<Ev> {
    let title = text(&e["title"]);
    if !title.starts_with("Counter-Strike") {
        return None;
    }
    let (home, away, league) = parse_title(&title)?;
    let start_ms = rfc_ms(&text(&e["startTime"])).or_else(|| rfc_ms(&text(&e["startDate"])))?;
    let markets = e["markets"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|m| {
            let cid = text(&m["conditionId"]);
            (!cid.is_empty()).then(|| (cid, m["sportsMarketType"].as_str() == Some("moneyline")))
        })
        .collect();
    Some(Ev { id: text(&e["id"]), slug: text(&e["slug"]), title, home, away, league, start_ms, markets })
}

/// Every fill of one market (newest first from the API; returned oldest first).
async fn trades(http: &reqwest::Client, cid: &str) -> Result<Vec<Value>> {
    let mut out = Vec::new();
    let mut cursor: Option<String> = None;
    for _ in 0..40 {
        let url = match &cursor {
            Some(c) => format!("{DATA}/trades?condition={cid}&limit=1000&cursor={c}"),
            None => format!("{DATA}/trades?condition={cid}&limit=1000"),
        };
        let (v, _) = fetch_json(http.get(url)).await?;
        out.extend(v["data"].as_array().cloned().unwrap_or_default());
        match v["pagination"]["next_cursor"].as_str() {
            Some(c) if v["pagination"]["has_more"].as_bool() == Some(true) && !c.is_empty() => cursor = Some(c.to_string()),
            _ => break,
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    out.reverse();
    Ok(out)
}

#[derive(Default, Debug, Clone, Copy)]
struct Metrics {
    volume: f64,
    trades: i64,
    bets: i64,
    big_dog: i64,
    big_fav: i64,
    big_share: f64,
    drift: Option<f64>,
}

/// Fills -> bets -> the activity measures.
fn metrics(fills: &[(bool, Value)], start_ms: Ms, s: &Settings) -> Metrics {
    let mut m = Metrics::default();
    // (wallet, condition, outcome, side) -> open bet (cash, shares, last ts)
    let mut open: HashMap<(String, String, i64, String), (f64, f64, i64)> = HashMap::new();
    let mut bets: Vec<(f64, f64, bool)> = Vec::new(); // (cash, avg price, buy)
    let mut first0: Option<f64> = None;
    let mut last0: Option<f64> = None;
    let flush = |b: (f64, f64, i64), side: &str, bets: &mut Vec<(f64, f64, bool)>| {
        if b.1 > 0.0 {
            bets.push((b.0, b.0 / b.1, side == "BUY"));
        }
    };
    for (is_match, f) in fills {
        let (Some(size), Some(price)) = (num(&f["size"]), num(&f["price"])) else { continue };
        let ts = int(&f["timestamp"]).unwrap_or(0) * 1000;
        let oi = int(&f["outcome_index"]).unwrap_or(0);
        let side = text(&f["side"]);
        let cash = size * price;
        m.volume += cash;
        m.trades += 1;
        if *is_match && ts <= start_ms {
            let p0 = if oi == 0 { price } else { 1.0 - price };
            first0.get_or_insert(p0);
            last0 = Some(p0);
        }
        let key = (text(&f["proxy_wallet"]), text(&f["condition_id"]), oi, side.clone());
        match open.get_mut(&key) {
            Some(b) if ts - b.2 <= 60_000 => {
                b.0 += cash;
                b.1 += size;
                b.2 = ts;
            }
            _ => {
                if let Some(old) = open.insert(key, (cash, size, ts)) {
                    flush(old, &side, &mut bets);
                }
            }
        }
    }
    for ((_, _, _, side), b) in open {
        flush(b, &side, &mut bets);
    }
    m.bets = bets.len() as i64;
    let big: Vec<&(f64, f64, bool)> = bets.iter().filter(|b| b.2 && b.0 >= s.big_bet_usd).collect();
    m.big_dog = big.iter().filter(|b| b.1 < s.underdog_below).count() as i64;
    m.big_fav = big.iter().filter(|b| b.1 >= 0.5).count() as i64;
    m.big_share = if m.volume > 0.0 { big.iter().map(|b| b.0).sum::<f64>() / m.volume } else { 0.0 };
    m.drift = first0.zip(last0).map(|(a, b)| (b - a).abs());
    m
}

async fn cs2_round(app: &Arc<App>, http: &reqwest::Client, first: bool) -> Result<()> {
    let s = settings();
    let now = now_ms();
    let c = conn(app)?;
    // Games to (re)score: open ones from 12 h ahead to 6 h ago, plus (first round of a start)
    // finished ones back to `baseline_days` not stored as final yet.
    let mut evs: Vec<Ev> = Vec::new();
    for closed in [false, true] {
        if closed && !first && ST.lock().scans % 10 != 0 {
            continue; // finished games: on start and every ~30 min
        }
        for page in 0..20 {
            let url = format!("{GAMMA}/events?tag_id={CS2_TAG}&closed={closed}&limit=100&offset={}&order=startTime&ascending=false", page * 100);
            let (v, _) = fetch_json(http.get(url).header("Accept-Encoding", "gzip")).await?;
            let list = v.as_array().cloned().unwrap_or_default();
            let mut older = false;
            for e in &list {
                let Some(ev) = parse_event(e) else { continue };
                if ev.start_ms < now - s.baseline_days * 86_400_000 {
                    older = true;
                    continue;
                }
                // Open games from 12 h ahead to 6 h ago. Polymarket also marks some future games
                // "closed" (cancelled or settled early): never score a game that has not started.
                if ev.start_ms > now + 12 * 3_600_000 || (closed && ev.start_ms > now) || (!closed && ev.start_ms < now - 6 * 3_600_000) {
                    continue;
                }
                evs.push(ev);
            }
            if older || list.len() < 100 {
                break;
            }
        }
    }
    let finals: HashSet<String> = {
        let mut st = c.prepare("SELECT event_id FROM cs2_activity WHERE final = 1")?;
        let rows = st.query_map([], |r| r.get::<_, String>(0))?;
        rows.filter_map(|x| x.ok()).collect()
    };
    retier(&c, &s)?;
    // Score with what is stored before the (possibly long) refresh, and every 25 games in it.
    rescore(&c, &s)?;
    let mut done_n = 0usize;
    for ev in evs {
        if finals.contains(&ev.id) {
            continue;
        }
        done_n += 1;
        if done_n % 25 == 0 {
            rescore(&c, &s)?;
        }
        let (rh, ra) = (rank(&ev.home), rank(&ev.away));
        let tier1 = is_tier1(rh, ra, &s);
        let done = ev.start_ms < now - 4 * 3_600_000;
        ST.lock().markets.insert(ev.id.clone(), ev.markets.clone());
        let mut m = Metrics::default();
        if !tier1 {
            let mut fills = Vec::new();
            for (cid, is_match) in &ev.markets {
                match trades(http, cid).await {
                    Ok(t) => fills.extend(t.into_iter().map(|f| (*is_match, f))),
                    Err(e) => tracing::debug!("suspicious: trades {cid}: {e:#}"),
                }
                tokio::time::sleep(Duration::from_millis(300)).await;
            }
            fills.sort_by_key(|f| int(&f.1["timestamp"]).unwrap_or(0));
            m = metrics(&fills, ev.start_ms, &s);
        }
        c.execute(
            "INSERT INTO cs2_activity(event_id, slug, title, home, away, league, start_ms, vrs_home, vrs_away, tier1, volume, trades, bets, big_dog, big_fav, big_share, drift, final, updated_ms)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19)
             ON CONFLICT(event_id) DO UPDATE SET slug=excluded.slug, title=excluded.title, home=excluded.home, away=excluded.away, league=excluded.league,
               start_ms=excluded.start_ms, vrs_home=excluded.vrs_home, vrs_away=excluded.vrs_away, tier1=excluded.tier1, volume=excluded.volume,
               trades=excluded.trades, bets=excluded.bets, big_dog=excluded.big_dog, big_fav=excluded.big_fav, big_share=excluded.big_share,
               drift=excluded.drift, final=excluded.final, updated_ms=excluded.updated_ms",
            params![ev.id, ev.slug, ev.title, ev.home, ev.away, ev.league, ev.start_ms, rh, ra, tier1 as i64, m.volume, m.trades, m.bets, m.big_dog, m.big_fav, m.big_share, m.drift, done as i64, now],
        )?;
    }
    rescore(&c, &s)?;
    // Alerts for open games that crossed the line.
    let rows: Vec<Value> = {
    let mut st = c.prepare(
        "SELECT event_id, title, home, away, league, start_ms, score, volume, big_dog, big_fav, big_share, drift, vrs_home, vrs_away FROM cs2_activity
         WHERE tier1 = 0 AND score >= ?1 AND start_ms > ?2",
    )?;
    let rows: Vec<Value> = st
        .query_map(params![s.alert_score, now - 6 * 3_600_000], |r| {
            Ok(json!({"id": r.get::<_, String>(0)?, "title": r.get::<_, String>(1)?, "home": r.get::<_, String>(2)?, "away": r.get::<_, String>(3)?,
                "league": r.get::<_, String>(4)?, "start_ms": r.get::<_, i64>(5)?, "score": r.get::<_, f64>(6)?, "volume": r.get::<_, f64>(7)?,
                "big_dog": r.get::<_, i64>(8)?, "big_fav": r.get::<_, i64>(9)?, "big_share": r.get::<_, f64>(10)?, "drift": r.get::<_, Option<f64>>(11)?,
                "vrs": [r.get::<_, i64>(12)?, r.get::<_, i64>(13)?]}))
        })?
        .filter_map(|x| x.ok())
        .collect();
    rows
    };
    drop(c);
    for r in rows {
        let key = format!("cs2|{}", r["id"].as_str().unwrap_or(""));
        let score = r["score"].as_f64().unwrap_or(0.0);
        let again = ST.lock().alerted.get(&key).is_none_or(|prev| score >= prev + 10.0);
        if !again {
            continue;
        }
        ST.lock().alerted.insert(key, score);
        let vr = |x: &Value| if x.as_i64().unwrap_or(0) > 0 { format!("#{}", x) } else { "unranked".into() };
        let text = format!(
            "<b>Heavily bet CS2 game · score {:.0}</b>\n{} v {} ({} / {} VRS)\n{}\nTraded ${:.0} · {} big underdog bets · {} big favourite bets · big bets {:.0}% of money{}",
            score,
            crate::telegram::esc(r["home"].as_str().unwrap_or("")),
            crate::telegram::esc(r["away"].as_str().unwrap_or("")),
            vr(&r["vrs"][0]),
            vr(&r["vrs"][1]),
            crate::telegram::esc(r["league"].as_str().unwrap_or("")),
            r["volume"].as_f64().unwrap_or(0.0),
            r["big_dog"],
            r["big_fav"],
            r["big_share"].as_f64().unwrap_or(0.0) * 100.0,
            r["drift"].as_f64().map(|d| format!(" · price drift {:.0} pts", d * 100.0)).unwrap_or_default()
        );
        crate::telegram::alert_suspicious("activity", &text).await;
    }
    Ok(())
}

/// Re-rank every stored game against the current VRS list and tier settings (names that
/// failed to match before, a new standings week, or changed tier limits).
fn retier(c: &Connection, s: &Settings) -> Result<()> {
    if ST.lock().vrs.is_empty() {
        return Ok(());
    }
    let rows: Vec<(String, String, String, i64, i64, i64)> = {
        let mut st = c.prepare("SELECT event_id, COALESCE(home, ''), COALESCE(away, ''), COALESCE(vrs_home, 0), COALESCE(vrs_away, 0), tier1 FROM cs2_activity")?;
        let it = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?;
        it.filter_map(|x| x.ok()).collect()
    };
    for (id, h, a, rh0, ra0, t0) in rows {
        let (rh, ra) = (rank(&h), rank(&a));
        let t = is_tier1(rh, ra, s) as i64;
        if (rh, ra, t) != (rh0, ra0, t0) {
            c.execute("UPDATE cs2_activity SET vrs_home = ?1, vrs_away = ?2, tier1 = ?3 WHERE event_id = ?4", params![rh, ra, t, id])?;
        }
    }
    Ok(())
}

/// Percentile scores against the baseline: finished, lower-tier games of the last `baseline_days`.
fn rescore(c: &Connection, s: &Settings) -> Result<()> {
    let since = now_ms() - s.baseline_days * 86_400_000;
    let base: Vec<[f64; 5]> = {
        let mut st = c.prepare("SELECT volume, big_dog, big_fav, big_share, COALESCE(drift, 0) FROM cs2_activity WHERE final = 1 AND tier1 = 0 AND start_ms > ?1 AND trades > 0")?;
        let rows = st.query_map([since], |r| Ok([r.get::<_, f64>(0)?, r.get::<_, i64>(1)? as f64, r.get::<_, i64>(2)? as f64, r.get::<_, f64>(3)?, r.get::<_, f64>(4)?]))?;
        rows.filter_map(|x| x.ok()).collect()
    };
    if base.len() < 10 {
        return Ok(()); // not enough normal games to compare against yet
    }
    let mut cols: Vec<Vec<f64>> = (0..5).map(|i| base.iter().map(|b| b[i]).collect()).collect();
    for c in &mut cols {
        c.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    }
    // Share of normal games at or below x (ties count half).
    let pct = |col: &Vec<f64>, x: f64| {
        let below = col.partition_point(|v| *v < x) as f64;
        let eq = col.partition_point(|v| *v <= x) as f64 - below;
        (below + eq / 2.0) / col.len() as f64
    };
    let rows: Vec<(String, [f64; 5])> = {
        let mut st = c.prepare("SELECT event_id, volume, big_dog, big_fav, big_share, COALESCE(drift, 0) FROM cs2_activity WHERE tier1 = 0 AND trades > 0")?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, [r.get::<_, f64>(1)?, r.get::<_, i64>(2)? as f64, r.get::<_, i64>(3)? as f64, r.get::<_, f64>(4)?, r.get::<_, f64>(5)?])))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let w = [1.0, 1.0, 1.0, 1.0, 0.5];
    for (id, v) in rows {
        let score = (0..5).map(|i| w[i] * pct(&cols[i], v[i])).sum::<f64>() / w.iter().sum::<f64>() * 100.0;
        c.execute("UPDATE cs2_activity SET score = ?1 WHERE event_id = ?2", params![score, id])?;
    }
    Ok(())
}

// ---------------------------------------------------------------- B. Pre-match line moves

fn line_round(app: &App) {
    let s = settings();
    let now = now_ms();
    // Upcoming games in the window and the platforms pricing them now (1X2 / winner).
    struct G {
        sport: String,
        home: String,
        away: String,
        league: String,
        start: Ms,
        /// platform -> (book, outcome -> current odds)
        now: BTreeMap<String, (String, BTreeMap<String, f64>)>,
    }
    let mut games: HashMap<i64, G> = HashMap::new();
    {
        let eng = app.engine.lock();
        for (book, bs) in &eng.books {
            if *book == "polymarket" || *book == "kalshi" {
                continue;
            }
            for es in bs.events.values() {
                let m = eng.matcher.matches.get(&es.match_id);
                let Some(start) = m.and_then(|m| m.start_ms).or(es.meta.start_ms) else { continue };
                if es.meta.live || start <= now || start > now + (s.line_window_h * 3_600_000.0) as i64 {
                    continue;
                }
                let prices: BTreeMap<String, f64> = es
                    .quotes
                    .iter()
                    .filter(|(k, _)| k.0 == Market::X12)
                    .filter_map(|(k, q)| q.price.filter(|p| *p > 1.0).map(|p| (k.2.as_str().to_string(), p)))
                    .collect();
                if prices.len() < 2 {
                    continue;
                }
                let g = games.entry(es.match_id).or_insert_with(|| G {
                    sport: m.map(|m| m.sport.clone()).unwrap_or_default(),
                    home: m.map(|m| m.home.clone()).unwrap_or_default(),
                    away: m.map(|m| m.away.clone()).unwrap_or_default(),
                    league: es.meta.league.clone(),
                    start,
                    now: BTreeMap::new(),
                });
                g.now.entry(crate::snipe::platform(book).to_string()).or_insert((book.to_string(), prices));
            }
        }
    }
    let Ok(c) = conn(app) else { return };
    let norm = |p: &BTreeMap<String, f64>| -> BTreeMap<String, f64> {
        let tot: f64 = p.values().map(|o| 1.0 / o).sum();
        p.iter().map(|(k, o)| (k.clone(), (1.0 / o) / tot)).collect()
    };
    let thr = s.line_move_pts / 100.0;
    let mut flagged: Vec<Value> = Vec::new();
    for (mid, g) in games.into_iter().filter(|(_, g)| g.now.len() >= s.line_min_platforms) {
        // Each platform's book: its price history for this game.
        let Ok(mut st) = c.prepare_cached("SELECT book, outcome, new_price, ts_ms FROM changes WHERE match_id = ?1 AND market = '1x2' AND new_price IS NOT NULL ORDER BY ts_ms") else { continue };
        let books: HashSet<&str> = g.now.values().map(|(b, _)| b.as_str()).collect();
        let Ok(rows) = st.query_map([mid], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, f64>(2)?, r.get::<_, Ms>(3)?))) else { continue };
        let mut hist: HashMap<String, Vec<(String, f64, Ms)>> = HashMap::new();
        for (b, o, p, t) in rows.filter_map(|x| x.ok()) {
            if books.contains(b.as_str()) {
                hist.entry(b).or_default().push((o, p, t));
            }
        }
        // Per platform: opening (first full set) vs now, normalised; and when it moved.
        let mut per: Vec<(BTreeMap<String, f64>, BTreeMap<String, f64>, Vec<(String, Ms)>)> = Vec::new();
        for (book, cur) in g.now.values() {
            let Some(h) = hist.get(book) else { continue };
            let mut open: BTreeMap<String, f64> = BTreeMap::new();
            for (o, p, _) in h {
                if cur.contains_key(o) && !open.contains_key(o) {
                    open.insert(o.clone(), *p);
                }
                if open.len() == cur.len() {
                    break;
                }
            }
            if open.len() != cur.len() {
                continue;
            }
            let (on, cn) = (norm(&open), norm(cur));
            // When each outcome first crossed halfway from open to now.
            let mut when = Vec::new();
            for (o, p_now) in &cn {
                let half = (on[o] + p_now) / 2.0;
                let up = *p_now > on[o];
                let mut run = open.clone();
                for (oo, p, t) in h {
                    run.insert(oo.clone(), *p);
                    if oo == o && run.len() == cur.len() {
                        let v = norm(&run)[o];
                        if (up && v >= half) || (!up && v <= half) {
                            when.push((o.clone(), *t));
                            break;
                        }
                    }
                }
            }
            per.push((on, cn, when));
        }
        if per.len() < s.line_min_platforms {
            continue;
        }
        let outcomes: Vec<String> = per[0].1.keys().cloned().collect();
        let mut best: Option<Value> = None;
        for o in &outcomes {
            let mut d: Vec<f64> = per.iter().filter_map(|(on, cn, _)| Some(cn.get(o)? - on.get(o)?)).collect();
            if d.len() < s.line_min_platforms {
                continue;
            }
            d.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let med = d[d.len() / 2];
            let agree = d.iter().filter(|x| x.signum() == med.signum() && x.abs() >= thr / 2.0).count();
            if med.abs() < thr || agree < s.line_min_platforms || med <= 0.0 {
                continue; // report the side that shortened; the other side mirrors it
            }
            let mut opens: Vec<f64> = per.iter().filter_map(|(on, _, _)| on.get(o).copied()).collect();
            let mut nows: Vec<f64> = per.iter().filter_map(|(_, cn, _)| cn.get(o).copied()).collect();
            let mut whens: Vec<Ms> = per.iter().flat_map(|(_, _, w)| w.iter().filter(|x| &x.0 == o).map(|x| x.1)).collect();
            for v in [&mut opens, &mut nows] {
                v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            }
            whens.sort();
            let r = json!({"match_id": mid, "outcome": o, "sport": g.sport, "home": g.home, "away": g.away, "league": g.league, "start_ms": g.start,
                "open_p": opens[opens.len() / 2], "now_p": nows[nows.len() / 2], "move": med, "platforms": d.len(), "agree": agree,
                "moved_ms": whens.get(whens.len() / 2)});
            if best.as_ref().is_none_or(|b| med > b["move"].as_f64().unwrap_or(0.0)) {
                best = Some(r);
            }
        }
        if let Some(r) = best {
            flagged.push(r);
        }
    }
    for r in &flagged {
        let _ = c.execute(
            "INSERT INTO line_moves(match_id, outcome, sport, home, away, league, start_ms, open_p, now_p, move, platforms, agree, moved_ms, first_ms, updated_ms)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?14)
             ON CONFLICT(match_id, outcome) DO UPDATE SET open_p=excluded.open_p, now_p=excluded.now_p, move=excluded.move, platforms=excluded.platforms,
               agree=excluded.agree, moved_ms=excluded.moved_ms, start_ms=excluded.start_ms, updated_ms=excluded.updated_ms",
            params![r["match_id"].as_i64(), r["outcome"].as_str(), r["sport"].as_str(), r["home"].as_str(), r["away"].as_str(), r["league"].as_str(),
                r["start_ms"].as_i64(), r["open_p"].as_f64(), r["now_p"].as_f64(), r["move"].as_f64(), r["platforms"].as_i64(), r["agree"].as_i64(), r["moved_ms"].as_i64(), now],
        );
    }
    // Telegram: once per game + outcome, again if the move grows 5+ points.
    let rt = tokio::runtime::Handle::try_current();
    for r in flagged {
        let key = format!("line|{}|{}", r["match_id"], r["outcome"].as_str().unwrap_or(""));
        let mv = r["move"].as_f64().unwrap_or(0.0);
        let again = ST.lock().alerted.get(&key).is_none_or(|prev| mv >= prev + 0.05);
        if !again {
            continue;
        }
        ST.lock().alerted.insert(key, mv);
        let odds = |p: f64| if p > 0.0 { format!("{:.2}", 1.0 / p) } else { "-".into() };
        let name = match r["outcome"].as_str().unwrap_or("") {
            "home" => r["home"].as_str().unwrap_or("home").to_string(),
            "away" => r["away"].as_str().unwrap_or("away").to_string(),
            o => o.to_string(),
        };
        let left = (r["start_ms"].as_i64().unwrap_or(now) - now) / 60_000;
        let text = format!(
            "<b>Pre-match line move +{:.0} pts</b>\n{} v {} ({})\n{} shortened {} → {} (fair odds, {} of {} bookmaker platforms agree) · kick-off in {}h {:02}m",
            mv * 100.0,
            crate::telegram::esc(r["home"].as_str().unwrap_or("")),
            crate::telegram::esc(r["away"].as_str().unwrap_or("")),
            crate::telegram::esc(r["league"].as_str().unwrap_or("")),
            crate::telegram::esc(&name),
            odds(r["open_p"].as_f64().unwrap_or(0.0)),
            odds(r["now_p"].as_f64().unwrap_or(0.0)),
            r["agree"],
            r["platforms"],
            left / 60,
            left % 60
        );
        if let Ok(h) = &rt {
            h.spawn(async move { crate::telegram::alert_suspicious("lines", &text).await });
        }
    }
}

// ---------------------------------------------------------------- API

pub fn view(app: &App, days: i64) -> Value {
    let s = settings();
    let now = now_ms();
    let since = now - days.clamp(1, 60) * 86_400_000;
    let (vrs_n, vrs_date, err, markets) = {
        let st = ST.lock();
        (st.vrs.len(), st.vrs_date.clone(), st.last_error.clone(), st.markets.len())
    };
    let c = match conn(app) {
        Ok(c) => c,
        Err(e) => return json!({"error": e.to_string()}),
    };
    let base_n: i64 = c
        .query_row("SELECT COUNT(*) FROM cs2_activity WHERE final = 1 AND tier1 = 0 AND trades > 0 AND start_ms > ?1", [now - s.baseline_days * 86_400_000], |r| r.get(0))
        .unwrap_or(0);
    let activity: Vec<Value> = c
        .prepare(
            "SELECT event_id, slug, home, away, league, start_ms, vrs_home, vrs_away, tier1, volume, trades, bets, big_dog, big_fav, big_share, drift, score, final, updated_ms
             FROM cs2_activity WHERE start_ms > ?1 AND start_ms < ?2 ORDER BY start_ms DESC LIMIT 2000",
        )
        .and_then(|mut st| {
            let it = st.query_map(params![since, now + 12 * 3_600_000], |r| {
                Ok(json!({"event_id": r.get::<_, String>(0)?, "slug": r.get::<_, Option<String>>(1)?, "home": r.get::<_, Option<String>>(2)?, "away": r.get::<_, Option<String>>(3)?,
                    "league": r.get::<_, Option<String>>(4)?, "start_ms": r.get::<_, Option<i64>>(5)?, "vrs": [r.get::<_, Option<i64>>(6)?, r.get::<_, Option<i64>>(7)?],
                    "tier1": r.get::<_, i64>(8)? != 0, "volume": r.get::<_, Option<f64>>(9)?, "trades": r.get::<_, Option<i64>>(10)?, "bets": r.get::<_, Option<i64>>(11)?,
                    "big_dog": r.get::<_, Option<i64>>(12)?, "big_fav": r.get::<_, Option<i64>>(13)?, "big_share": r.get::<_, Option<f64>>(14)?, "drift": r.get::<_, Option<f64>>(15)?,
                    "score": r.get::<_, Option<f64>>(16)?, "final": r.get::<_, i64>(17)? != 0, "updated_ms": r.get::<_, Option<i64>>(18)?}))
            })?;
            Ok(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    // Link each Polymarket game to its Swifteye match page when the matcher linked it.
    let mut activity = activity;
    {
        let eng = app.engine.lock();
        for a in &mut activity {
            let id = a["event_id"].as_str().unwrap_or("").to_string();
            if let Some(m) = eng.known.get(&("polymarket".to_string(), id)) {
                a["match_id"] = json!(m);
            }
        }
    }
    let lines: Vec<Value> = c
        .prepare("SELECT match_id, outcome, sport, home, away, league, start_ms, open_p, now_p, move, platforms, agree, moved_ms, first_ms, updated_ms FROM line_moves WHERE start_ms > ?1 AND move >= ?2 ORDER BY first_ms DESC LIMIT 500")
        .and_then(|mut st| {
            let it = st.query_map(params![since, s.line_move_pts / 100.0 - 1e-9], |r| {
                Ok(json!({"match_id": r.get::<_, i64>(0)?, "outcome": r.get::<_, String>(1)?, "sport": r.get::<_, Option<String>>(2)?, "home": r.get::<_, Option<String>>(3)?,
                    "away": r.get::<_, Option<String>>(4)?, "league": r.get::<_, Option<String>>(5)?, "start_ms": r.get::<_, Option<i64>>(6)?, "open_p": r.get::<_, Option<f64>>(7)?,
                    "now_p": r.get::<_, Option<f64>>(8)?, "move": r.get::<_, Option<f64>>(9)?, "platforms": r.get::<_, Option<i64>>(10)?, "agree": r.get::<_, Option<i64>>(11)?,
                    "moved_ms": r.get::<_, Option<i64>>(12)?, "first_ms": r.get::<_, Option<i64>>(13)?, "updated_ms": r.get::<_, Option<i64>>(14)?}))
            })?;
            Ok(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    json!({"now_ms": now, "settings": s, "vrs": {"teams": vrs_n, "date": vrs_date}, "baseline_games": base_n, "tracked_events": markets, "error": err, "activity": activity, "lines": lines})
}

/// One game in detail (fetched live from Polymarket): the match price over time, every big bet,
/// and money bought per side.
pub async fn event_detail(app: &App, id: &str) -> Result<Value> {
    let s = settings();
    let http = crate::books::http_client();
    let (v, _) = fetch_json(http.get(format!("{GAMMA}/events/{id}"))).await?;
    let ev = parse_event(&v).ok_or_else(|| anyhow::anyhow!("not a CS2 game"))?;
    let names: HashMap<String, String> = v["markets"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|m| {
            let t = text(&m["groupItemTitle"]);
            (text(&m["conditionId"]), if t.is_empty() { text(&m["question"]) } else { t })
        })
        .collect();
    let mut price = Vec::new();
    let mut bets: Vec<Value> = Vec::new();
    let mut by_side: BTreeMap<String, f64> = BTreeMap::new();
    for (cid, is_match) in &ev.markets {
        let fills = trades(&http, cid).await.unwrap_or_default();
        // (wallet, outcome, side) -> (cash, shares, first ts, last ts, outcome name)
        let mut open: HashMap<(String, i64, String), (f64, f64, i64, i64, String)> = HashMap::new();
        let mut done: Vec<(String, (f64, f64, i64, i64, String))> = Vec::new();
        for f in &fills {
            let (Some(size), Some(p)) = (num(&f["size"]), num(&f["price"])) else { continue };
            let ts = int(&f["timestamp"]).unwrap_or(0) * 1000;
            let oi = int(&f["outcome_index"]).unwrap_or(0);
            let side = text(&f["side"]);
            if *is_match {
                price.push(json!([ts, if oi == 0 { p } else { 1.0 - p }]));
                if side == "BUY" {
                    *by_side.entry(text(&f["outcome"])).or_default() += size * p;
                }
            }
            let key = (text(&f["proxy_wallet"]), oi, side.clone());
            match open.get_mut(&key) {
                Some(b) if ts - b.3 <= 60_000 => {
                    b.0 += size * p;
                    b.1 += size;
                    b.3 = ts;
                }
                _ => {
                    if let Some(old) = open.insert(key, (size * p, size, ts, ts, text(&f["outcome"]))) {
                        done.push((side.clone(), old));
                    }
                }
            }
        }
        done.extend(open.into_iter().map(|(k, b)| (k.2, b)));
        for (side, b) in done {
            if b.0 >= s.big_bet_usd && b.1 > 0.0 {
                bets.push(json!({"ts": b.2, "side": side, "outcome": b.4, "cash": b.0, "price": b.0 / b.1,
                    "market": names.get(cid).cloned().unwrap_or_default(), "match_market": is_match}));
            }
        }
    }
    bets.sort_by_key(|b| b["ts"].as_i64());
    // Swifteye's match for this game (for every bookmaker's line), and whether it lists the
    // teams the other way round from Polymarket.
    let linked: Option<(i64, String, String)> = {
        let eng = app.engine.lock();
        eng.known.get(&("polymarket".to_string(), ev.id.clone())).and_then(|m| eng.matcher.matches.get(m).map(|x| (*m, x.home.clone(), x.away.clone())))
    };
    let linked = match linked {
        Some(x) => Some(x),
        None => conn(app).ok().and_then(|c| {
            c.query_row(
                "SELECT m.id, m.home, m.away FROM book_events b JOIN matches m ON m.id = b.match_id WHERE b.book = 'polymarket' AND b.book_event_id = ?1",
                [&ev.id],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, String>(2)?)),
            )
            .ok()
        }),
    };
    let swapped = linked.as_ref().is_some_and(|(_, mh, ma)| crate::matcher::names_swapped("cs2", mh, ma, &ev.home, &ev.away));
    let stored: Value = conn(app)
        .ok()
        .and_then(|c| {
            c.query_row(
                "SELECT score, volume, big_dog, big_fav, big_share, drift, tier1 FROM cs2_activity WHERE event_id = ?1",
                [&ev.id],
                |r| {
                    Ok(json!({"score": r.get::<_, Option<f64>>(0)?, "volume": r.get::<_, Option<f64>>(1)?, "big_dog": r.get::<_, Option<i64>>(2)?,
                        "big_fav": r.get::<_, Option<i64>>(3)?, "big_share": r.get::<_, Option<f64>>(4)?, "drift": r.get::<_, Option<f64>>(5)?, "tier1": r.get::<_, i64>(6)? != 0}))
                },
            )
            .ok()
        })
        .unwrap_or(Value::Null);
    let kalshi = kalshi_history(&http, &ev.home, &ev.away, ev.start_ms).await;
    // Each "Map N Winner" market closes the moment map N is over; the match market when the game is.
    let pm_ms = |s: &str| -> Option<Ms> {
        // "2026-11-17 19:04:19.882197+00" (or RFC 3339)
        let t = s.trim().replacen(' ', "T", 1);
        let t = if t.ends_with("+00") { format!("{t}:00") } else { t };
        rfc_ms(&t)
    };
    let mut maps: Vec<Value> = Vec::new();
    let mut match_end: Option<Ms> = None;
    for m in v["markets"].as_array().into_iter().flatten() {
        let Some(end) = m["closedTime"].as_str().and_then(pm_ms) else { continue };
        let kind = m["sportsMarketType"].as_str().unwrap_or("");
        let title = text(&m["groupItemTitle"]);
        if kind == "moneyline" {
            match_end = Some(end);
        } else if kind == "child_moneyline" {
            if let Some(n) = title.strip_prefix("Map ").and_then(|r| r.split_whitespace().next()).and_then(|x| x.parse::<i64>().ok()) {
                maps.push(json!({"map": n, "end_ms": end}));
            }
        }
    }
    maps.sort_by_key(|m| m["map"].as_i64());
    Ok(json!({"id": ev.id, "slug": ev.slug, "title": ev.title, "home": ev.home, "away": ev.away, "league": ev.league,
        "start_ms": ev.start_ms, "vrs": [rank(&ev.home), rank(&ev.away)], "price": price, "bets": bets, "by_side": by_side, "settings": s,
        "match_id": linked.as_ref().map(|x| x.0), "match_home": linked.as_ref().map(|x| x.1.clone()), "match_away": linked.as_ref().map(|x| x.2.clone()),
        "pm_swapped": swapped, "stored": stored,
        "kalshi": kalshi.map(|(t, p)| json!({"event": t, "price": p, "start_ms": kalshi_ticker_ms(&t)})), "maps": maps, "match_end_ms": match_end}))
}

// ---------------------------------------------------------------- Kalshi history for a game

const KAPI: &str = "https://api.elections.kalshi.com/trade-api/v2";
const KCS2_SERIES: &str = "KXCS2GAME";

/// Kalshi's CS2 games (open and settled), refreshed every 30 minutes: (event ticker, title).
static KCS2: LazyLock<Mutex<(Ms, Vec<(String, String)>)>> = LazyLock::new(Default::default);

/// GET with up to three tries: Kalshi answers 429 when the collector's own polling is busy.
async fn kget(http: &reqwest::Client, url: &str) -> Option<Value> {
    for attempt in 1..=3u64 {
        match fetch_json(http.get(url)).await {
            Ok((v, _)) => return Some(v),
            Err(_) => tokio::time::sleep(Duration::from_millis(1200 * attempt)).await,
        }
    }
    None
}

/// One build of the list at a time; a caller arriving mid-build waits for it instead of
/// starting a second one (Kalshi would rate-limit both).
static KCS2_BUILD: LazyLock<tokio::sync::Mutex<()>> = LazyLock::new(|| tokio::sync::Mutex::new(()));

async fn kalshi_cs2_events(http: &reqwest::Client) -> Vec<(String, String)> {
    let fresh = || {
        let c = KCS2.lock();
        (now_ms() - c.0 < 30 * 60_000 && !c.1.is_empty()).then(|| c.1.clone())
    };
    if let Some(v) = fresh() {
        return v;
    }
    let _g = KCS2_BUILD.lock().await;
    if let Some(v) = fresh() {
        return v;
    }
    let mut out = Vec::new();
    let mut complete = true;
    for status in ["open", "closed", "settled"] {
        let mut cursor = String::new();
        // Settled games, newest first: 15 pages of 200 covers several weeks.
        for _ in 0..15 {
            let url = format!("{KAPI}/events?series_ticker={KCS2_SERIES}&status={status}&limit=200{}", if cursor.is_empty() { String::new() } else { format!("&cursor={cursor}") });
            let Some(v) = kget(http, &url).await else {
                complete = false;
                break;
            };
            for e in v["events"].as_array().into_iter().flatten() {
                out.push((text(&e["event_ticker"]), text(&e["title"])));
            }
            cursor = text(&v["cursor"]);
            if cursor.is_empty() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    }
    // An incomplete list (Kalshi kept refusing) is only kept for 2 minutes.
    *KCS2.lock() = (if complete { now_ms() } else { now_ms() - 28 * 60_000 }, out.clone());
    out
}

/// Kalshi ticker date "26NOV171845" -> ms (Kalshi writes US Eastern time; good to a few hours).
fn kalshi_ticker_ms(ticker: &str) -> Option<Ms> {
    let part = ticker.split('-').nth(1)?;
    if part.len() < 7 {
        return None;
    }
    let (yy, mon, dd) = (&part[0..2], &part[2..5], &part[5..7]);
    let m = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"].iter().position(|x| *x == mon)? as u8 + 1;
    let d = time::Date::from_calendar_date(2000 + yy.parse::<i32>().ok()?, time::Month::try_from(m).ok()?, dd.parse().ok()?).ok()?;
    let (h, mi) = if part.len() >= 11 { (part[7..9].parse().unwrap_or(12), part[9..11].parse().unwrap_or(0)) } else { (12, 0) };
    let t = d.with_hms(h, mi, 0).ok()?.assume_utc();
    // US Eastern: UTC-4 in summer time (2nd Sunday of March to 1st Sunday of November), else UTC-5.
    let year = d.year();
    let nth_sunday = |month: time::Month, n: u8| -> Option<time::Date> {
        let first = time::Date::from_calendar_date(year, month, 1).ok()?;
        let back = first.weekday().number_days_from_sunday();
        let first_sun = first + time::Duration::days(((7 - back) % 7) as i64);
        Some(first_sun + time::Duration::weeks(n as i64 - 1))
    };
    let summer = match (nth_sunday(time::Month::March, 2), nth_sunday(time::Month::November, 1)) {
        (Some(a), Some(b)) => d >= a && d < b,
        _ => false,
    };
    Some((t.unix_timestamp() + if summer { 4 } else { 5 } * 3600) as Ms * 1000)
}

/// The same game on Kalshi (team names, date within 36 h) and its minute-by-minute price for
/// `home` (Polymarket's home team): (event ticker, [[ms, chance]]).
async fn kalshi_history(http: &reqwest::Client, home: &str, away: &str, start_ms: Ms) -> Option<(String, Vec<Value>)> {
    let (h, a) = (crate::matcher::Names::new(home), crate::matcher::Names::new(away));
    let events = kalshi_cs2_events(http).await;
    // Same two teams, nearest in time (within 36 h); a rematch on another day must not win.
    let mut cands: Vec<(i64, String)> = events
        .iter()
        .filter_map(|(t, title)| {
            let ms = kalshi_ticker_ms(t)?;
            let diff = (ms - start_ms).abs();
            let tt = title.split(':').next_back().unwrap_or(title);
            let (x, y) = tt.split_once(" vs. ").or_else(|| tt.split_once(" vs "))?;
            let (x, y) = (crate::matcher::Names::new(x.trim()), crate::matcher::Names::new(y.trim()));
            let same = h.sim(&x).min(a.sim(&y)) >= 0.75 || h.sim(&y).min(a.sim(&x)) >= 0.75;
            (same && diff <= 36 * 3_600_000).then(|| (diff, t.clone()))
        })
        .collect();
    cands.sort();
    cands.dedup_by(|x, y| x.1 == y.1);
    for (_, ticker) in cands.into_iter().take(3) {
        if let Some(pts) = kalshi_candles(http, &ticker, &h, start_ms).await {
            return Some((ticker, pts));
        }
    }
    None
}

/// Minute-by-minute chance of `h` winning from one Kalshi game's candles.
async fn kalshi_candles(http: &reqwest::Client, ticker: &str, h: &crate::matcher::Names, start_ms: Ms) -> Option<Vec<Value>> {
    let v = kget(http, &format!("{KAPI}/events/{ticker}?with_nested_markets=true")).await?;
    let markets = if v["event"]["markets"].is_array() { v["event"]["markets"].clone() } else { v["markets"].clone() };
    // The market whose Yes is `h` winning.
    let market = markets.as_array()?.iter().max_by(|m1, m2| {
        let s = |m: &Value| h.sim(&crate::matcher::Names::new(&text(&m["yes_sub_title"])));
        s(m1).partial_cmp(&s(m2)).unwrap_or(std::cmp::Ordering::Equal)
    })?;
    if h.sim(&crate::matcher::Names::new(&text(&market["yes_sub_title"]))) < 0.5 {
        return None;
    }
    let mt = text(&market["ticker"]);
    let (from, to) = ((start_ms - 24 * 3_600_000) / 1000, (start_ms + 6 * 3_600_000).min(now_ms()) / 1000);
    let url = format!("{KAPI}/series/{KCS2_SERIES}/markets/{mt}/candlesticks?start_ts={from}&end_ts={to}&period_interval=1");
    let cv = kget(http, &url).await?;
    let px = |k: &Value, side: &str| num(&k[side]["close_dollars"]).or_else(|| num(&k[side]["close"]).map(|c| c / 100.0)).filter(|x| *x > 0.0 && *x < 1.0);
    let pts: Vec<Value> = cv["candlesticks"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|k| {
            let ts = int(&k["end_period_ts"])? * 1000;
            // Mid of the closing bid and ask; the last trade when the book was one-sided.
            let p = match (px(k, "yes_bid"), px(k, "yes_ask")) {
                (Some(b), Some(a)) if a > b && a - b <= 0.25 => (a + b) / 2.0,
                _ => px(k, "price")?,
            };
            Some(json!([ts, p]))
        })
        .collect();
    (!pts.is_empty()).then_some(pts)
}
