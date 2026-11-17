//! Fonbet: the public line CDN (`*.bk6bba-resources.com`) behind fonbet's apps and site.
//! First call takes the full line (version=0); after that only deltas since the
//! last packetVersion. A full refresh every 30 s drops anything a delta missed.
//!
//! Factor ids: 921/922/923 = 1X2, 930/931 = main total over/under,
//! 927/928 = main handicap team1/team2 (pt = that team's handicap).

use super::*;
use std::collections::HashMap;

pub struct Fonbet {
    pub host: String,
    /// Look-ahead for pre-match games (the line feed already includes them).
    pub pre_hours: f64,
    version: u64,
    last_full_ms: Ms,
    /// segment id -> (parent sport id, name)
    segments: HashMap<i64, (i64, String)>,
    events: HashMap<i64, Value>,
    factors: HashMap<i64, HashMap<i64, (f64, Option<String>)>>,
    blocked: HashMap<i64, bool>,
    misc: HashMap<i64, Value>,
    timers: HashMap<i64, String>,
}

impl Fonbet {
    pub fn new(host: String, pre_hours: f64) -> Self {
        Fonbet {
            host,
            pre_hours,
            version: 0,
            last_full_ms: 0,
            segments: HashMap::new(),
            events: HashMap::new(),
            factors: HashMap::new(),
            blocked: HashMap::new(),
            misc: HashMap::new(),
            timers: HashMap::new(),
        }
    }

    fn merge(&mut self, v: &Value) {
        for s in v["sports"].as_array().into_iter().flatten() {
            if let Some(id) = s["id"].as_i64() {
                let parent = s["parentId"].as_i64().unwrap_or(id);
                self.segments.insert(id, (parent, text(&s["name"])));
            }
        }
        for e in v["events"].as_array().into_iter().flatten() {
            if let Some(id) = e["id"].as_i64() {
                self.events.insert(id, e.clone());
            }
        }
        for b in v["eventBlocks"].as_array().into_iter().flatten() {
            if let Some(id) = b["eventId"].as_i64() {
                self.blocked.insert(id, b["state"].as_str() == Some("blocked"));
            }
        }
        for m in v["eventMiscs"].as_array().into_iter().flatten() {
            if let Some(id) = m["id"].as_i64() {
                self.misc.insert(id, m.clone());
            }
        }
        for li in v["liveEventInfos"].as_array().into_iter().flatten() {
            if let (Some(id), Some(t)) = (li["eventId"].as_i64(), li["timer"].as_str()) {
                self.timers.insert(id, t.to_string());
            }
        }
        for c in v["customFactors"].as_array().into_iter().flatten() {
            let Some(e) = c["e"].as_i64() else { continue };
            let entry = self.factors.entry(e).or_default();
            for f in c["factors"].as_array().into_iter().flatten() {
                if let (Some(fid), Some(val)) = (f["f"].as_i64(), f["v"].as_f64()) {
                    entry.insert(fid, (val, f["pt"].as_str().map(str::to_string)));
                }
            }
        }
    }

    fn root_sport(&self, seg: i64) -> i64 {
        let mut cur = seg;
        for _ in 0..6 {
            match self.segments.get(&cur) {
                Some(&(parent, _)) if parent != cur => cur = parent,
                _ => break,
            }
        }
        cur
    }
}

impl Book for Fonbet {
    fn name(&self) -> &'static str {
        "fonbet"
    }
    fn source(&self) -> String {
        format!("{}/events/list (incremental by packetVersion)", self.host)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let full = self.version == 0 || now_ms() - self.last_full_ms > 30_000;
        let ver = if full { 0 } else { self.version };
        let url = format!("https://{}/events/list?lang=en&version={ver}&scopeMarket=1600", self.host);
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        if full {
            self.events.clear();
            self.factors.clear();
            self.blocked.clear();
            self.misc.clear();
            self.timers.clear();
            self.last_full_ms = recv_ms;
        }
        self.merge(&v);
        if let Some(pv) = v["packetVersion"].as_u64() {
            self.version = pv;
        }

        let mut out = Vec::new();
        for (&id, e) in &self.events {
            // In-play games, plus pre-match games kicking off inside the look-ahead window.
            let live = e["place"].as_str() == Some("live");
            if e["level"].as_i64() != Some(1) {
                continue;
            }
            if !live {
                let start = e["startTime"].as_i64().map(|t| t * 1000);
                let ok = e["place"].as_str() == Some("line")
                    && self.pre_hours > 0.0
                    && start.is_some_and(|t| t >= now_ms() - 15 * 60_000 && t <= now_ms() + (self.pre_hours * 3_600_000.0) as i64);
                if !ok {
                    continue;
                }
            }
            let seg = e["sportId"].as_i64().unwrap_or(0);
            if self.root_sport(seg) != 1 {
                continue;
            }
            let league = self.segments.get(&seg).map(|s| s.1.clone()).unwrap_or_default();
            let misc = self.misc.get(&id);
            let score = misc.and_then(|m| Some(format!("{}-{}", m["score1"].as_i64()?, m["score2"].as_i64()?)));
            let clock = self.timers.get(&id).cloned();
            let blocked = self.blocked.get(&id).copied().unwrap_or(false);
            let mut ev = EventIn {
                id: id.to_string(),
                sport: "football".into(),
                league: league.replace(". ", " / "),
                home: text(&e["team1"]),
                away: text(&e["team2"]),
                start_ms: e["startTime"].as_i64().map(|s| s * 1000),
                sr_id: None,
                score,
                clock,
                quotes: Vec::new(),
                sr_weak: false,
                live,
            };
            if let Some(fs) = self.factors.get(&id) {
                let price = |fid: i64| if blocked { None } else { fs.get(&fid).map(|x| x.0) };
                let pt = |fid: i64| fs.get(&fid).and_then(|x| x.1.as_deref()).and_then(|s| s.trim_start_matches('+').parse::<f64>().ok());
                for (fid, o) in [(921, Outcome::Home), (922, Outcome::Draw), (923, Outcome::Away)] {
                    if fs.contains_key(&fid) {
                        ev.quotes.push(QuoteIn::new(Market::X12, None, o, price(fid)));
                    }
                }
                if let Some(l) = pt(930).or(pt(931)) {
                    for (fid, o) in [(930, Outcome::Over), (931, Outcome::Under)] {
                        if fs.contains_key(&fid) {
                            ev.quotes.push(QuoteIn::new(Market::Total, Some(l), o, price(fid)));
                        }
                    }
                }
                if let Some(l) = pt(927).or(pt(928).map(|x| -x)) {
                    for (fid, o) in [(927, Outcome::Home), (928, Outcome::Away)] {
                        if fs.contains_key(&fid) {
                            ev.quotes.push(QuoteIn::new(Market::Handicap, Some(l), o, price(fid)));
                        }
                    }
                }
            }
            out.push(ev);
        }
        Ok(Snapshot { events: out, recv_ms })
    }
}
