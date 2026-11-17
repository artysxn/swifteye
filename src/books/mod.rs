//! Bookmaker adapters. Each one polls the same public JSON the bookmaker's
//! own live page loads, and turns it into a normalised `Snapshot`.

pub mod altenar;
pub mod betika;
pub mod betpawa;
pub mod fonbet;
pub mod kambi;
pub mod linebet;
pub mod polymarket;
pub mod sportybet;

use crate::model::*;
use crate::App;
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

pub trait Book: Send + 'static {
    fn name(&self) -> &'static str;
    /// Human-readable description of where the data comes from.
    fn source(&self) -> String;
    fn fetch(&mut self, http: &reqwest::Client) -> impl Future<Output = Result<Snapshot>> + Send;
}

pub fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent(UA)
        .gzip(true)
        .brotli(true)
        .deflate(true)
        .timeout(Duration::from_secs(15))
        .connect_timeout(Duration::from_secs(8))
        .pool_idle_timeout(Duration::from_secs(90))
        .tcp_nodelay(true)
        .build()
        .expect("http client")
}

/// Send a request, read the whole body, and stamp the moment it finished arriving.
pub async fn fetch_json(req: reqwest::RequestBuilder) -> Result<(Value, Ms)> {
    let resp = req.send().await?;
    let status = resp.status();
    let body = resp.bytes().await?;
    let recv = now_ms();
    if !status.is_success() {
        let snippet = String::from_utf8_lossy(&body[..body.len().min(200)]).into_owned();
        bail!("HTTP {status}: {snippet}");
    }
    let v: Value = serde_json::from_slice(&body).with_context(|| {
        format!("bad JSON: {}", String::from_utf8_lossy(&body[..body.len().min(200)]))
    })?;
    Ok((v, recv))
}

/// Poll one book forever.
pub async fn run<B: Book>(mut book: B, app: Arc<App>, poll_ms: u64) {
    let name = book.name();
    let http = http_client();
    let mut iv = tokio::time::interval(Duration::from_millis(poll_ms));
    iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut backoff = Duration::ZERO;
    loop {
        iv.tick().await;
        if !backoff.is_zero() {
            tokio::time::sleep(backoff).await;
        }
        let t0 = Instant::now();
        let res = tokio::time::timeout(Duration::from_secs(20), book.fetch(&http)).await;
        let res = match res {
            Ok(r) => r,
            Err(_) => Err(anyhow::anyhow!("timed out")),
        };
        match res {
            Ok(mut snap) => {
                let req_ms = t0.elapsed().as_millis() as i64;
                if !app.cfg.include_esoccer {
                    snap.events.retain(|e| !is_esoccer(e));
                }
                let writes = app.engine.lock().apply(name, snap, req_ms);
                app.emit(writes).await;
                if !backoff.is_zero() {
                    tracing::info!("{name}: recovered");
                }
                backoff = Duration::ZERO;
            }
            Err(e) => {
                let msg = format!("{e:#}");
                app.engine.lock().poll_error(name, msg.clone());
                backoff = (backoff * 2).clamp(Duration::from_secs(2), Duration::from_secs(60));
                tracing::warn!("{name}: {msg} (retry in {}s)", backoff.as_secs());
            }
        }
    }
}

/// Simulated / e-sports football (FIFA "2x4 min" leagues, Volta, cyber) — not real matches.
pub fn is_esoccer(e: &EventIn) -> bool {
    let l = e.league.to_lowercase();
    let teams = format!("{} {}", e.home, e.away).to_lowercase();
    l.contains("esport")
        || l.contains("e-sport")
        || l.contains("cyber")
        || l.contains("efootball")
        || l.contains("e-football")
        || l.contains("esoccer")
        || l.contains("e-soccer")
        || l.contains("volta")
        || l.contains("fifa")
        || l.contains("ea sports")
        || l.contains(" min.")
        || l.contains("x4 min")
        || l.contains("x3 min")
        || l.contains("x5 min")
        || l.contains("x6 min")
        || (l.starts_with("fc 2") && l.as_bytes().get(5).is_some_and(|b| b.is_ascii_digit()))
        || l.contains("short football")
        || l.trim_end().ends_with('+')
        || e.home.trim_end().ends_with(" +")
        // "Division 4x4", "LFL 5x5", "Short Football 3x3": small-sided simulated formats
        || l.split(|c: char| !c.is_ascii_alphanumeric()).any(|t| {
            let b = t.as_bytes();
            b.len() == 3 && b[0].is_ascii_digit() && b[1] == b'x' && b[2].is_ascii_digit()
        })
        || teams.contains("esports")
        || teams.contains("(e)")
}

// ---- small JSON helpers -------------------------------------------------

pub fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

pub fn int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

pub fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.trim().to_string(),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

pub fn id_text(v: &Value) -> String {
    text(v)
}

pub fn rfc3339_ms(s: &str) -> Option<Ms> {
    let t = time::OffsetDateTime::parse(s, &time::format_description::well_known::Rfc3339).ok()?;
    Some((t.unix_timestamp_nanos() / 1_000_000) as i64)
}

/// "YYYY-MM-DD HH:MM:SS" in a fixed UTC offset (hours).
pub fn naive_ms(s: &str, offset_h: i64) -> Option<Ms> {
    let fmt = time::macros::format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let t = time::PrimitiveDateTime::parse(s.trim(), &fmt).ok()?;
    Some(t.assume_utc().unix_timestamp() * 1000 - offset_h * 3600 * 1000)
}

pub fn join_league(parts: &[&str]) -> String {
    parts.iter().filter(|p| !p.is_empty()).copied().collect::<Vec<_>>().join(" / ")
}
