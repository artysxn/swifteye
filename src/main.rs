mod adaptive;
mod api;
mod arb;
mod arbtrack;
mod books;
mod clock;
mod data;
mod engine;
mod kalacct;
mod lag;
mod matcher;
mod model;
mod pmacct;
mod predmeta;
mod predarb;
mod prune;
mod rank;
mod snipe;
mod store;
mod suspect;
mod telegram;

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
    /// Follow real e-sports (CS2, LoL, Dota 2, Valorant, ...) alongside football.
    pub esports: bool,
    /// Traditional sports to follow (keys from `books::SPORTS`). E-sports are switched by `esports`.
    pub sports: Vec<String>,
    /// Only record and show matches that Polymarket or Kalshi and at least one bookmaker list.
    pub prediction_only: bool,
    /// How far ahead to follow games before kick-off (0 = live only).
    pub prematch_hours: f64,
    /// How often to refresh the bookmakers' pre-match lists.
    pub prematch_refresh_ms: i64,
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
            esports: true,
            sports: ["football", "basketball", "tennis", "hockey", "baseball", "volleyball", "amfootball", "mma"].map(String::from).to_vec(),
            prediction_only: true,
            prematch_hours: 8.0,
            prematch_refresh_ms: 15_000,
            books: HashMap::new(),
        }
    }
}

impl Config {
    /// Is this sport key followed?
    pub fn follows(&self, sport: &str) -> bool {
        if books::is_esports_sport(sport) {
            self.esports
        } else {
            self.sports.iter().any(|s| s == sport)
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
    for s in &cfg.sports {
        if !books::SPORTS.contains(&s.as_str()) {
            tracing::warn!("config: unknown sport '{s}' (known: {})", books::SPORTS.join(", "));
        }
    }
    let db_path = PathBuf::from(&cfg.db);

    // Correct the PC clock before anything gets timestamped.
    match clock::sync().await {
        Ok(off) => tracing::info!("clock: this PC is {} ms {} true time (corrected in all timestamps)", off.abs(), if off < 0 { "ahead of" } else { "behind" }),
        Err(e) => tracing::warn!("clock: NTP sync failed ({e}); timestamps use the raw PC clock"),
    }
    clock::spawn_resync();

    let mut engine = Engine::default();
    engine.prediction_only = cfg.prediction_only;
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
    snipe::spawn_warmer(app.clone());
    telegram::spawn(app.clone());
    arbtrack::spawn(app.clone());
    adaptive::spawn(app.clone());
    suspect::spawn(app.clone());
    prune::spawn(app.clone());
    predarb::spawn(app.clone());

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

    let (ph, pr) = (cfg.prematch_hours, cfg.prematch_refresh_ms.max(3_000));
    let pre = || Pre::new(ph, pr);

    let follows = |s: &str| cfg.follows(s);
    // Kambi brands share Kambi's trading desk; operators can still shade margins and timing.
    for (name, offering, market) in [("unibet", "ubse", "SE"), ("leovegas", "leose", "SE"), ("paf", "paf", "FI")] {
        start!(name, 1000, |p: Option<String>| kambi::Kambi {
            name,
            offering: p.unwrap_or_else(|| offering.into()),
            market: market.into(),
            lang: "en_GB".into(),
            sports: kambi::sport_slugs(follows),
            pre: pre(),
        });
    }
    start!("sportybet", 1000, |p: Option<String>| sportybet::SportyBet { country: p.unwrap_or_else(|| "ng".into()), pre: pre() });
    start!("betpawa", 1500, |p: Option<String>| {
        let host = p.unwrap_or_else(|| "www.betpawa.ng".into());
        betpawa::BetPawa { brand: if host.ends_with(".ng") { "betpawa-nigeria".into() } else { "betpawa".into() }, host, pre: pre() }
    });
    start!("betika", 1000, |_p: Option<String>| betika::Betika { pre: pre() });
    // 1xBet-platform brands: same API on each brand's own domain.
    for (name, host, extra) in [
        ("linebet", "linebet.com", "&country=137&partner=189"),
        ("melbet", "melbet.com", ""),
        ("betwinner", "betwinner.com", ""),
        ("megapari", "megapari.com", ""),
        // Newer brands: the feed refuses requests without the brand's own partner id (`ref` in
        // the requests its live page makes). Their domains are mirrors that move; override with param.
        ("paripesa", "paripesa.me", "&country=137&partner=188"),
        ("22bet", "22bet21.com", "&country=137&partner=151"),
        ("22bit", "22bit1.com", "&country=137&partner=407"),
        ("1xbit", "1xbit1.com", "&country=137&partner=65"),
    ] {
        start!(name, 1000, |p: Option<String>| linebet::Linebet {
            name,
            host: p.unwrap_or_else(|| host.into()),
            extra: extra.into(),
            sports: linebet::sport_ids(follows),
            pre: pre(),
        });
    }
    start!("m88", 1500, |_p: Option<String>| m88::M88::new(follows, pre()));
    start!("pinnacle", 2000, |_p: Option<String>| pinnacle::Pinnacle::new(follows, pre()));
    start!("fonbet", 1000, |p: Option<String>| fonbet::Fonbet::new("fonbet", p.unwrap_or_else(|| "line52w.bk6bba-resources.com".into()), 1600, ph));
    // PARI (pari.ru) runs on Fonbet's platform: same feed on its own line hosts, market scope 2300.
    start!("pari", 1000, |p: Option<String>| fonbet::Fonbet::new("pari", p.unwrap_or_else(|| "line-vk02-w.pb06e2-resources.ru".into()), 2300, ph));
    start!("vinn", 1000, |p: Option<String>| altenar::Altenar {
        name: "vinn",
        integration: p.unwrap_or_else(|| "vinn".into()),
        country: "NO".into(),
        origin: "https://www.vinn.com".into(),
        pre: pre(),
    });
    // BetKing Ukraine runs on Altenar too (integration "betking").
    start!("betking", 1000, |p: Option<String>| altenar::Altenar {
        name: "betking",
        integration: p.unwrap_or_else(|| "betking".into()),
        country: "UA".into(),
        origin: "https://betking.com.ua".into(),
        pre: pre(),
    });
    start!("olimp", 1000, |_p: Option<String>| olimp::Olimp);
    start!("betcity", 1000, |_p: Option<String>| betcity::Betcity);
    // Marathonbet: param = its app version for the X-PAN-VERSION header.
    start!("marathon", 2000, |p: Option<String>| marathon::Marathon {
        version: p.unwrap_or_else(|| "2.8.2".into()),
        follows: books::SPORTS.iter().copied().filter(|s| cfg.follows(s)).collect(),
    });
    // Leon: param = the ctag (locale + build tag) its live page uses.
    start!("leon", 1500, |p: Option<String>| leon::Leon { ctag: p.unwrap_or_else(|| "en-US-7da6p".into()) });
    // Mostbet moves between mirror domains; param = the current one.
    start!("mostbet", 2000, |p: Option<String>| mostbet::Mostbet { host: p.unwrap_or_else(|| "0djawb3.com".into()), pre: Pre::new(ph, 60_000) });
    // Betby brands: (name, API host, brand id, site). Pre-match is ~600 KB, refreshed every minute.
    for (name, api, brand, origin) in [("duel", "https://api-a-c7818b61-600.sptpub.com", "2482975601191952386", "https://duel.com")] {
        start!(name, 1500, |_p: Option<String>| betby::Betby { name, api: api.into(), brand: brand.into(), origin: origin.into(), pre: Pre::new(ph, 60_000) });
    }
    start!("thunderpick", 1500, |_p: Option<String>| thunderpick::Thunderpick { pre_hours: ph, esports: cfg.esports });
    start!("novibet", 1000, |_p: Option<String>| novibet::Novibet);
    // Kalshi: second prediction-market baseline, polled in batches.
    // With an API key Kalshi pushes over its WebSocket and a poll is only a memory read, so it
    // runs every 250 ms; without one each poll is a batched REST call. Key edits apply on restart.
    let kcreds = kalacct::load(&app.db_path);
    let kpoll = if kalacct::has_key(&kcreds) { 250 } else { 1000 };
    start!("kalshi", kpoll, |_p: Option<String>| kalshi::Kalshi::new(ph, follows, kcreds.clone(), app.tx.clone()));

    // Polymarket is push (WebSocket), not polled. param = minutes before kick-off to start watching.
    let pc = get("polymarket");
    if pc.enabled.unwrap_or(true) {
        let prematch: i64 = pc.param.as_deref().and_then(|s| s.parse().ok()).unwrap_or((ph * 60.0) as i64).max(30);
        app.engine.lock().register(polymarket::NAME, polymarket::source(), 0);
        tracing::info!("polymarket: WebSocket, games up to {prematch} min before kick-off");
        tokio::spawn(polymarket::run(app.clone(), prematch));
    }
}
