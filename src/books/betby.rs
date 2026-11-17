//! Betby platform (the sportsbook inside Duel and many crypto casinos): the brand's public feed on
//! `api-*.sptpub.com`, as its own sportsbook widget loads it.
//!
//! * `GET /api/v4/{live|prematch}/brand/{brand}/en/0` = pointers: `top_events_versions` and
//!   `rest_events_versions`. Each `GET .../en/{version}` returns a slice of the board with
//!   `sports`, `categories`, `tournaments` and `events` keyed by id. Together they are the full list.
//! * Event: `desc` (scheduled s, sport, tournament, competitors[2].name, type "match"),
//!   `markets` = { market id: { specifier: { outcome id: { k: price } } } } with Sportradar ids:
//!   1 = 1X2 (outcomes 1/2/3); two-way winners 186 (tennis, e-sports), 219 (basketball incl. OT),
//!   406 (hockey incl. OT and penalties), 251 (baseball incl. extra innings), 340 (cricket):
//!   outcomes 4 = home, 5 = away. `state.status` 1 = open, 3 = suspended.
//! * The brand id and API host are in the brand page's requests (see docs/BOOKMAKERS.md).

use super::*;
use std::collections::HashMap;

pub struct Betby {
    pub name: &'static str,
    /// e.g. "https://api-a-c7818b61-600.sptpub.com"
    pub api: String,
    pub brand: String,
    /// The brand's site, sent as Origin / Referer like its widget does.
    pub origin: String,
    pub pre: Pre,
}

const TWO_WAY: &[&str] = &["186", "219", "406", "251", "340"];

impl Betby {
    async fn board(&self, http: &reqwest::Client, kind: &str) -> Result<(Vec<EventIn>, Ms)> {
        let base = format!("{}/api/v4/{kind}/brand/{}/en", self.api, self.brand);
        let get = |u: String| http.get(u).header("Origin", &self.origin).header("Referer", format!("{}/", self.origin));
        let (p, _) = fetch_json(get(format!("{base}/0"))).await?;
        let versions: Vec<i64> = ["top_events_versions", "rest_events_versions"].iter().flat_map(|k| p[*k].as_array().into_iter().flatten().filter_map(|v| v.as_i64())).collect();
        let res = futures::future::join_all(versions.iter().map(|v| fetch_json(get(format!("{base}/{v}"))))).await;
        let mut recv = 0;
        let (mut sports, mut tours, mut events): (HashMap<String, Value>, HashMap<String, Value>, Vec<(String, Value)>) = Default::default();
        for r in res {
            let (v, t) = r?;
            recv = recv.max(t);
            for (k, x) in v["sports"].as_object().into_iter().flatten() {
                sports.insert(k.clone(), x.clone());
            }
            for (k, x) in v["tournaments"].as_object().into_iter().flatten() {
                tours.insert(k.clone(), x.clone());
            }
            for (k, x) in v["events"].as_object().into_iter().flatten() {
                events.push((k.clone(), x.clone()));
            }
        }
        let live = kind == "live";
        let mut out = Vec::new();
        for (id, e) in &events {
            let d = &e["desc"];
            if d["type"].as_str().is_some_and(|t| t != "match") {
                continue;
            }
            let comp: Vec<String> = d["competitors"].as_array().into_iter().flatten().map(|c| text(&c["name"])).collect();
            if comp.len() != 2 {
                continue;
            }
            let sport_name = sports.get(&text(&d["sport"])).map(|s| text(&s["name"])).unwrap_or_default();
            let league = tours.get(&text(&d["tournament"])).map(|t| text(&t["name"])).unwrap_or_default();
            let sport: &str = match sport_key(&sport_name).or_else(|| esport_key(&sport_name)) {
                Some(s) => s,
                None => continue,
            };
            let open = e["state"]["status"].as_i64().map_or(true, |s| s == 1 || (!live && s == 0));
            let mk = &e["markets"];
            let mut quotes = Vec::new();
            let pick = |mid: &str, sides: &[(&str, Outcome)], quotes: &mut Vec<QuoteIn>| {
                let Some(m) = mk[mid][""].as_object() else { return false };
                for (oid, o) in sides {
                    let p = if open { m.get(*oid).and_then(|x| num(&x["k"])) } else { None };
                    quotes.push(QuoteIn::new(Market::X12, None, *o, p));
                }
                true
            };
            if three_way(sport) {
                pick("1", &[("1", Outcome::Home), ("2", Outcome::Draw), ("3", Outcome::Away)], &mut quotes);
            } else {
                for mid in TWO_WAY {
                    if pick(mid, &[("4", Outcome::Home), ("5", Outcome::Away)], &mut quotes) {
                        break;
                    }
                }
            }
            let sc = &e["score"];
            out.push(EventIn {
                id: id.clone(),
                sport: sport.into(),
                league,
                home: comp[0].replace(", ", " "),
                away: comp[1].replace(", ", " "),
                start_ms: int(&d["scheduled"]).map(|s| s * 1000),
                sr_id: None,
                sr_weak: false,
                live,
                score: if live { Some(format!("{}-{}", text(&sc["home_score"]), text(&sc["away_score"]))) } else { None },
                clock: None,
                quotes,
            });
        }
        Ok((out, if recv > 0 { recv } else { now_ms() }))
    }
}

impl Book for Betby {
    fn name(&self) -> &'static str {
        self.name
    }
    fn source(&self) -> String {
        format!("Betby feed {} brand {} (live + pre-match)", self.api, self.brand)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        if self.pre.due() {
            match self.board(http, "prematch").await {
                Ok((evs, _)) => self.pre.store(evs),
                Err(e) => {
                    tracing::warn!("{} pre-match: {e:#}", self.name);
                    self.pre.failed();
                }
            }
        }
        let (mut events, recv_ms) = self.board(http, "live").await?;
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}
