mod api;
mod books;
mod clock;
mod engine;
mod lag;
mod matcher;
mod model;
mod store;

use engine::{Engine, Write};
use model::*;
use parking_lot::Mutex;
use serde::Deserialize;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use tokio::sync::{broadcast, mpsc};

#[derive(Deserialize, Clone)]
#[serde(default)]
pub struct Config {
    /// Address the API listens on. Keep it on localhost unless you put auth in front.
    pub bind: String,
    /// SQLite file holding every change.
    pub db: String,
    /// Keep e-sports / simulated football (FIFA 2x4 min leagues etc.).
    pub include_esoccer: bool,
    pub books: HashMap<String, BookCfg>,
}

#[derive(Deserialize, Clone, Default)]
#[serde(default)]
pub struct BookCfg {
    pub enabled: Option<bool>,
    pub poll_ms: Option<u64>,
    /// Adapter-specific override (host, offering, country...).
    pub param: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            bind: "127.0.0.1:8787".into(),
            db: "oddsfeed.db".into(),
            include_esoccer: false,
            books: HashMap::new(),
        }
    }
}

pub struct App {
    pub cfg: Config,
    pub db_path: PathBuf,
    pub engine: Mutex<Engine>,
    pub tx: mpsc::Sender<Write>,
    pub live: broadcast::Sender<Arc<Change>>,
    pub started_ms: Ms,
}

impl App {
    pub async fn emit(&self, writes: Vec<Write>) {
        for w in writes {
            if let Write::Change(c) = &w {
                let _ = self.live.send(Arc::new(c.clone()));
            }
            if self.tx.send(w).await.is_err() {
                tracing::error!("db writer is gone");
            }
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let cfg_path = std::env::args().nth(1).unwrap_or_else(|| "oddsfeed.toml".into());
    let cfg: Config = match std::fs::read_to_string(&cfg_path) {
        Ok(s) => toml::from_str(&s)?,
        Err(_) => {
            tracing::info!("no {cfg_path}, using defaults");
            Config::default()
        }
    };
    let db_path = PathBuf::from(&cfg.db);

    // Correct the PC clock before anything gets timestamped.
    match clock::sync().await {
        Ok(off) => tracing::info!("clock: this PC is {} ms {} true time (corrected in all timestamps)", off.abs(), if off < 0 { "ahead of" } else { "behind" }),
        Err(e) => tracing::warn!("clock: NTP sync failed ({e}); timestamps use the raw PC clock"),
    }
    clock::spawn_resync();

    let mut engine = Engine::default();
    store::open(&db_path)?;
    store::restore(&db_path, &mut engine)?;
    tracing::info!("db {} ({} recent matches restored)", db_path.display(), engine.matcher.matches.len());

    let (tx, rx) = mpsc::channel::<Write>(200_000);
    let writer = store::spawn_writer(db_path.clone(), rx);
    let (live, _) = broadcast::channel(16_384);

    let app = Arc::new(App {
        cfg: cfg.clone(),
        db_path,
        engine: Mutex::new(engine),
        tx,
        live,
        started_ms: now_ms(),
    });

    start_books(&app);

    let listener = tokio::net::TcpListener::bind(&cfg.bind).await?;
    tracing::info!("API on http://{}", cfg.bind);
    axum::serve(listener, api::router(app.clone()))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;

    // The writer commits every ~200 ms; give it one more cycle. WAL keeps the file consistent regardless.
    tracing::info!("shutting down");
    tokio::time::sleep(std::time::Duration::from_millis(400)).await;
    drop(writer);
    Ok(())
}

fn start_books(app: &Arc<App>) {
    use books::*;
    let cfg = &app.cfg;
    let get = |name: &str| cfg.books.get(name).cloned().unwrap_or_default();

    macro_rules! start {
        ($name:expr, $default_ms:expr, $make:expr) => {{
            let bc = get($name);
            if bc.enabled.unwrap_or(true) {
                let poll = bc.poll_ms.unwrap_or($default_ms).max(250);
                let param = bc.param.clone();
                let book = $make(param);
                app.engine.lock().register(book.name(), book.source(), poll);
                tracing::info!("{}: polling every {} ms — {}", book.name(), poll, book.source());
                tokio::spawn(books::run(book, app.clone(), poll));
            }
        }};
    }

    start!("unibet", 1000, |p: Option<String>| kambi::Kambi {
        name: "unibet",
        offering: p.unwrap_or_else(|| "ubse".into()),
        market: "SE".into(),
        lang: "en_GB".into(),
    });
    start!("sportybet", 1000, |p: Option<String>| sportybet::SportyBet { country: p.unwrap_or_else(|| "ng".into()) });
    start!("betpawa", 1500, |p: Option<String>| {
        let host = p.unwrap_or_else(|| "www.betpawa.ng".into());
        betpawa::BetPawa { brand: if host.ends_with(".ng") { "betpawa-nigeria".into() } else { "betpawa".into() }, host }
    });
    start!("betika", 1000, |_p: Option<String>| betika::Betika);
    start!("linebet", 1000, |p: Option<String>| linebet::Linebet { host: p.unwrap_or_else(|| "linebet.com".into()) });
    start!("fonbet", 1000, |p: Option<String>| fonbet::Fonbet::new(p.unwrap_or_else(|| "line52w.bk6bba-resources.com".into())));
    start!("vinn", 1000, |p: Option<String>| altenar::Altenar {
        name: "vinn",
        integration: p.unwrap_or_else(|| "vinn".into()),
        country: "NO".into(),
        origin: "https://www.vinn.com".into(),
    });

    // Polymarket is push (WebSocket), not polled. param = minutes before kick-off to start watching.
    let pc = get("polymarket");
    if pc.enabled.unwrap_or(true) {
        let prematch: i64 = pc.param.as_deref().and_then(|s| s.parse().ok()).unwrap_or(30);
        app.engine.lock().register(polymarket::NAME, polymarket::source(), 0);
        tracing::info!("polymarket: WebSocket — {}", polymarket::source());
        tokio::spawn(polymarket::run(app.clone(), prematch));
    }
}
