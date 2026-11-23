//! Books collected by `browser_books.py` (repo root) instead of a Rust poll loop: BC.Game, Rollbit,
//! Rainbet, CSGO500 and CSGOEmpire through their Betby (`sptpub.com`) brand feeds, Cloudbet through
//! its own sports API fetched inside a headless browser page, Shuffle by reading its rendered live
//! page. The script is used as written; oddsfeed starts it, keeps it running, and takes what it
//! POSTs to `/ingest` through the same filters and engine as every other book.
//!
//! What the script sends, and what is fixed on the way in (see `normalize`):
//! * `recv_ms` is the raw PC clock (this PC runs ~2.6 s fast): corrected with the NTP offset.
//! * No `live` flag: every list it reads is a live list, so events are live unless they say otherwise.
//! * 1X2 / both-teams-to-score with `line: 0.0`, BTTS as over/under: stored like the other books
//!   (no line, yes/no), or they would never line up with them.
//! * Shuffle's live page mixes sports and the script tags every game "football": a "football" game
//!   priced home/away with no draw and no other market is a two-way sport, and is dropped.
//! * `start_ms: 0` = unknown.
//!
//! The script polls every 5 s (Shuffle: a page reload every 60 s) and posts only when it found games.
//! Its own error lines ("bcgame poll fail: ...") become the book's poll errors on the Feeds page.

use crate::model::*;
use crate::App;
use anyhow::{anyhow, Result};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// (name, what the script does for it, its poll interval in ms).
pub const BOOKS: &[(&str, &str, u64)] = &[
    ("bcgame", "Betby feed api-k-c7818b61-623.sptpub.com brand 2103509236163162112", 5_000),
    ("rollbit", "Betby feed api-a-c7818b61-600.sptpub.com brand 2103518166582956032", 5_000),
    ("rainbet", "Betby feed api-a-c7818b61-600.sptpub.com brand 2374656571012681728", 5_000),
    ("csgo500", "Betby feed api-h-c7818b61-608.sptpub.com brand 2195256717934206976", 5_000),
    ("csgoempire", "Betby feed api-h-c7818b61-608.sptpub.com brand 2432911154364948480", 5_000),
    ("cloudbet", "Cloudbet sports-api v6, fetched inside a headless browser page", 5_000),
    ("shuffle", "Shuffle live page, rendered in a headless browser and read every 60 s", 60_000),
];

pub fn book(name: &str) -> Option<&'static str> {
    BOOKS.iter().find(|b| b.0 == name).map(|b| b.0)
}

/// One posted snapshot made ready for the engine. Returns the events kept and how many were dropped
/// as two-way sports tagged football.
pub fn normalize(mut events: Vec<EventIn>) -> (Vec<EventIn>, usize) {
    let before = events.len();
    for e in &mut events {
        e.start_ms = e.start_ms.filter(|s| *s > 0);
        let mut seen: HashMap<(Market, i32, Outcome), usize> = HashMap::new();
        let mut out: Vec<QuoteIn> = Vec::with_capacity(e.quotes.len());
        for q in e.quotes.drain(..) {
            let (line, outcome) = match q.market {
                Market::X12 => (None, q.outcome),
                Market::Btts => (
                    None,
                    match q.outcome {
                        Outcome::Over => Outcome::Yes,
                        Outcome::Under => Outcome::No,
                        o => o,
                    },
                ),
                _ => (q.line, q.outcome),
            };
            let q = QuoteIn::new(q.market, line, outcome, q.price).ts(q.book_ts);
            let key = (q.market, line_key(q.line), q.outcome);
            match seen.get(&key) {
                Some(&i) => out[i] = q,
                None => {
                    seen.insert(key, out.len());
                    out.push(q);
                }
            }
        }
        e.quotes = out;
    }
    events.retain(|e| !(e.sport == "football" && two_way_only(&e.quotes)));
    let dropped = before - events.len();
    (events, dropped)
}

/// Priced home/away and nothing else: not football (every football 1X2 has a draw).
fn two_way_only(qs: &[QuoteIn]) -> bool {
    let winner = qs.iter().any(|q| q.market == Market::X12 && matches!(q.outcome, Outcome::Home | Outcome::Away));
    let draw = qs.iter().any(|q| q.market == Market::X12 && q.outcome == Outcome::Draw);
    let other = qs.iter().any(|q| q.market != Market::X12);
    winner && !draw && !other
}

/// The script's raw-PC-clock stamp on the corrected clock. A missing or implausible stamp (over 30 s
/// off) is replaced by the time it arrived.
pub fn corrected_recv(raw: Option<i64>, now: i64, offset: i64) -> i64 {
    match raw {
        Some(r) if r > 0 && (r + offset - now).abs() <= 30_000 => r + offset,
        _ => now,
    }
}

/// `POST /ingest`: `{book, recv_ms, events}` from browser_books.py.
pub async fn ingest(app: &Arc<App>, mut body: Value) -> Result<Value> {
    let name = body["book"].as_str().unwrap_or_default().to_string();
    let book = book(&name).ok_or_else(|| anyhow!("unknown book '{name}' (known: {})", BOOKS.iter().map(|b| b.0).collect::<Vec<_>>().join(", ")))?;
    if !app.engine.lock().books.contains_key(book) {
        // Switched off in the config (or the helper is not run by this oddsfeed): not recorded.
        return Ok(json!({"ok": true, "ignored": "book not enabled"}));
    }
    if app.engine.lock().schedule(book).0 {
        return Ok(json!({"ok": true, "ignored": "paused"}));
    }
    let now = now_ms();
    let recv_ms = corrected_recv(body["recv_ms"].as_i64(), now, now - crate::clock::local_ms());
    let mut events = Vec::new();
    let mut bad = 0;
    for mut e in body["events"].as_array_mut().map(std::mem::take).unwrap_or_default() {
        if let Some(o) = e.as_object_mut() {
            o.entry("live").or_insert(Value::Bool(true));
        }
        match serde_json::from_value::<EventIn>(e) {
            Ok(ev) => events.push(ev),
            Err(_) => bad += 1,
        }
    }
    let (mut events, two_way) = normalize(events);
    if !app.cfg.include_esoccer {
        events.retain(|e| e.sport != "football" || !super::is_esoccer(e));
    }
    events.retain(|e| app.cfg.follows(&e.sport));
    let kept = events.len();
    let writes = {
        let mut eng = app.engine.lock();
        let w = eng.apply(book, Snapshot { events, recv_ms }, 0);
        // The script does not time its requests; leave the column empty rather than show 0 ms.
        if let Some(b) = eng.books.get_mut(book) {
            b.status.last_request_ms = None;
        }
        w
    };
    let changes = writes.iter().filter(|w| matches!(w, crate::engine::Write::Change(_))).count();
    app.emit(writes).await;
    Ok(json!({"ok": true, "events": kept, "dropped_two_way": two_way, "unreadable": bad, "writes": changes}))
}

/// Where browser_books.py and its Python are. Config first, then the repo the exe was built in
/// (target/release/oddsfeed.exe), then the working directory and its parent.
fn locate(cfg: &crate::BrowserCfg) -> Option<(PathBuf, PathBuf)> {
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Some(d) = &cfg.dir {
        dirs.push(PathBuf::from(d));
    }
    if let Ok(exe) = std::env::current_exe() {
        dirs.extend(exe.ancestors().skip(1).take(3).map(Path::to_path_buf));
    }
    if let Ok(cwd) = std::env::current_dir() {
        dirs.extend(cwd.ancestors().take(2).map(Path::to_path_buf));
    }
    let dir = dirs.into_iter().find(|d| d.join("browser_books.py").is_file())?;
    let python = match &cfg.python {
        Some(p) => PathBuf::from(p),
        None => [dir.join(".venv").join("Scripts").join("python.exe"), dir.join(".venv").join("bin").join("python")].into_iter().find(|p| p.is_file())?,
    };
    Some((dir, python))
}

/// Started by oddsfeed with oddsfeed's process id: a thread waits for that process to end and then
/// stops the script (and the browser it drives) even when oddsfeed was killed. The API address is set
/// from `bind`; the script itself is not changed.
/// (Not stdin EOF: on Windows a thread blocked reading a pipe stalls numpy's DLL load in the main
/// thread, which camoufox imports, and the script hung before opening a page.)
const BOOTSTRAP: &str = r#"
import os, sys, threading, time
def _watch(pid):
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)
        if not h:
            return
        k.WaitForSingleObject(h, 0xFFFFFFFF)
    else:
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(2)
    os._exit(0)
threading.Thread(target=_watch, args=(int(os.environ["SWIFTEYE_PARENT_PID"]),), daemon=True).start()
sys.path.insert(0, os.environ["SWIFTEYE_BB_DIR"])
import browser_books as bb
bb.API = os.environ["SWIFTEYE_BB_API"]
bb.main()
"#;

/// Register the books and keep browser_books.py running. Off when `[browser_books] enabled = false`,
/// or when the script or its Python venv is not found (the Mac until it has one).
pub fn start(app: &Arc<App>) {
    let cfg = app.cfg.browser_books.clone();
    if !cfg.enabled {
        tracing::info!("browser books: off (config)");
        return;
    }
    let Some((dir, python)) = locate(&cfg) else {
        tracing::warn!("browser books: browser_books.py or its .venv Python not found; BC.Game, Rollbit, Rainbet, CSGO500, CSGOEmpire, Cloudbet and Shuffle are off");
        return;
    };
    let mut on = Vec::new();
    for (name, source, poll) in BOOKS {
        let bc = app.cfg.books.get(*name).cloned().unwrap_or_default();
        if !bc.enabled.unwrap_or(true) {
            continue;
        }
        let mut eng = app.engine.lock();
        eng.register(name, format!("browser_books.py: {source} (record-only)"), *poll);
        if bc.paused.unwrap_or(false) {
            eng.control(name, Some(true), None);
        }
        on.push(*name);
    }
    if on.is_empty() {
        return;
    }
    let host = app.cfg.bind.replace("0.0.0.0", "127.0.0.1");
    tracing::info!("browser books: {} via {} ({})", on.join(", "), dir.join("browser_books.py").display(), python.display());
    tokio::spawn(supervise(app.clone(), dir, python, format!("http://{host}")));
}

async fn supervise(app: Arc<App>, dir: PathBuf, python: PathBuf, api: String) {
    let mut backoff = Duration::from_secs(10);
    loop {
        let t0 = Instant::now();
        match run_once(&app, &dir, &python, &api).await {
            Ok(code) => tracing::warn!("browser books: browser_books.py exited ({code})"),
            Err(e) => tracing::warn!("browser books: could not run browser_books.py: {e:#}"),
        }
        if t0.elapsed() > Duration::from_secs(600) {
            backoff = Duration::from_secs(10);
        }
        for (name, ..) in BOOKS {
            app.engine.lock().poll_error(name, format!("browser_books.py stopped; restarting in {} s", backoff.as_secs()));
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(300));
    }
}

async fn run_once(app: &Arc<App>, dir: &Path, python: &Path, api: &str) -> Result<String> {
    use tokio::io::{AsyncBufReadExt, BufReader};
    let mut child = tokio::process::Command::new(python)
        .arg("-u")
        .arg("-c")
        .arg(BOOTSTRAP)
        .current_dir(dir)
        .env("SWIFTEYE_BB_DIR", dir)
        .env("SWIFTEYE_BB_API", api)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUNBUFFERED", "1")
        .env("SWIFTEYE_PARENT_PID", std::process::id().to_string())
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let mut out = BufReader::new(child.stdout.take().ok_or_else(|| anyhow!("no stdout"))?).lines();
    let mut err = BufReader::new(child.stderr.take().ok_or_else(|| anyhow!("no stderr"))?).lines();
    let mut quiet = Quiet::default();
    let (mut out_open, mut err_open) = (true, true);
    while out_open || err_open {
        tokio::select! {
            l = out.next_line(), if out_open => match l {
                Ok(Some(l)) => line(app, &l, &mut quiet),
                _ => out_open = false,
            },
            l = err.next_line(), if err_open => match l {
                Ok(Some(l)) => quiet.log("stderr", &l),
                _ => err_open = false,
            },
        }
    }
    let status = child.wait().await?;
    Ok(status.to_string())
}

/// One line the script printed: "<book> poll fail: ..." and the like become that book's poll error.
fn line(app: &Arc<App>, l: &str, quiet: &mut Quiet) {
    let first = l.split_whitespace().next().unwrap_or_default();
    if let (Some(b), true) = (book(first), l.contains(" fail")) {
        // "ingest fail" is this side refusing or not answering, not the bookmaker.
        if !l.contains("ingest fail") {
            app.engine.lock().poll_error(b, l[first.len()..].trim().to_string());
        }
        quiet.log(b, l);
    } else {
        quiet.log("", l);
    }
}

/// Logs each distinct line, then the same line again at most once a minute: a failing book prints
/// every 5 s and the script has no back-off of its own.
#[derive(Default)]
struct Quiet {
    last: HashMap<String, (String, Instant)>,
}

impl Quiet {
    fn log(&mut self, key: &str, l: &str) {
        let shape: String = l.chars().filter(|c| !c.is_ascii_digit()).collect();
        let k = if key.is_empty() { shape.split_whitespace().take(2).collect::<Vec<_>>().join(" ") } else { key.to_string() };
        if let Some((s, t)) = self.last.get(&k) {
            if *s == shape && t.elapsed() < Duration::from_secs(60) {
                return;
            }
        }
        self.last.insert(k, (shape, Instant::now()));
        if l.contains("fail") || l.contains("Traceback") || l.contains("Error") {
            tracing::warn!("browser_books.py: {l}");
        } else {
            tracing::info!("browser_books.py: {l}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(m: Market, line: Option<f64>, o: Outcome, p: f64) -> QuoteIn {
        QuoteIn { market: m, line, outcome: o, price: Some(p), book_ts: None }
    }
    fn ev(league: &str, quotes: Vec<QuoteIn>) -> EventIn {
        EventIn { id: "1".into(), sport: "football".into(), league: league.into(), home: "A".into(), away: "B".into(), start_ms: Some(0), live: true, quotes, ..Default::default() }
    }

    #[test]
    fn lines_and_btts_are_stored_like_the_other_books() {
        let (evs, dropped) = normalize(vec![ev(
            "Super Lig",
            vec![
                q(Market::X12, Some(0.0), Outcome::Home, 1.96),
                q(Market::X12, Some(0.0), Outcome::Draw, 3.75),
                q(Market::X12, Some(0.0), Outcome::Away, 3.9),
                q(Market::Btts, Some(0.0), Outcome::Over, 1.8),
                q(Market::Btts, Some(0.0), Outcome::Under, 1.95),
                q(Market::Total, Some(2.5), Outcome::Over, 2.0),
                q(Market::Total, Some(2.5), Outcome::Under, 1.0), // not a price: suspended
            ],
        )]);
        assert_eq!(dropped, 0);
        let e = &evs[0];
        assert_eq!(e.start_ms, None, "0 = unknown kick-off");
        assert!(e.quotes.iter().filter(|q| q.market == Market::X12).all(|q| q.line.is_none()));
        let btts: Vec<_> = e.quotes.iter().filter(|q| q.market == Market::Btts).map(|q| (q.line, q.outcome)).collect();
        assert_eq!(btts, vec![(None, Outcome::Yes), (None, Outcome::No)]);
        let under = e.quotes.iter().find(|q| q.market == Market::Total && q.outcome == Outcome::Under).unwrap();
        assert_eq!((under.line, under.price), (Some(2.5), None));
    }

    #[test]
    fn a_two_way_game_tagged_football_is_dropped() {
        // Shuffle's live page: NCAA basketball next to Super Lig, all sent as "football".
        let basket = ev("NCAA, Regular Season", vec![q(Market::X12, Some(0.0), Outcome::Home, 2.8), q(Market::X12, Some(0.0), Outcome::Away, 1.4)]);
        let soccer = ev("Super Lig", vec![q(Market::X12, None, Outcome::Home, 1.96), q(Market::X12, None, Outcome::Draw, 3.75), q(Market::X12, None, Outcome::Away, 3.9)]);
        // Draw suspended, but totals listed: still football.
        let no_draw = ev("Serie A", vec![q(Market::X12, None, Outcome::Home, 1.5), q(Market::X12, None, Outcome::Away, 6.0), q(Market::Total, Some(2.5), Outcome::Over, 1.9)]);
        let (evs, dropped) = normalize(vec![basket, soccer, no_draw]);
        assert_eq!(dropped, 1);
        assert_eq!(evs.iter().map(|e| e.league.as_str()).collect::<Vec<_>>(), vec!["Super Lig", "Serie A"]);
    }

    #[test]
    fn the_scripts_pc_clock_stamp_is_corrected() {
        let now = 1_795_417_900_000;
        // PC 2.6 s fast: offset -2600, the script stamped 2.6 s "ahead".
        assert_eq!(corrected_recv(Some(now + 2_600 - 40), now, -2_600), now - 40);
        assert_eq!(corrected_recv(Some(0), now, -2_600), now);
        assert_eq!(corrected_recv(None, now, -2_600), now);
        assert_eq!(corrected_recv(Some(now - 120_000), now, 0), now, "a stale stamp is not trusted");
    }

    #[test]
    fn the_known_books_are_the_seven_the_script_polls() {
        for n in ["bcgame", "rollbit", "rainbet", "csgo500", "csgoempire", "cloudbet", "shuffle"] {
            assert_eq!(book(n), Some(n));
            assert!(super::super::record_only(n), "{n} is record-only until its timing is tested");
        }
        assert_eq!(book("gamdom"), None);
    }
}
