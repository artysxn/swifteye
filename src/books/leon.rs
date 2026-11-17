//! Leon (leon.bet): the JSON its own live page loads,
//! `GET /api-2/betline/events/inplayupcomingall?ctag=<ctag>&hideClosed=true&flags=reg,urlv2,orn2,cn,mm2,rrc,cmg`.
//!
//! * `ctag` is the locale plus a build tag ("en-US-7da6p" on 17 Nov 2026); plain "en-US" is refused
//!   with INVALID_CODE. Override with `param` if Leon changes it. Without the `nodup` flag every event
//!   carries its full league and sport (with it, repeats are replaced by `{id}` only).
//! * Event: `competitors[].homeAway` HOME/AWAY, `kickoff` ms, `betline` inplay / prematch,
//!   `league.sport.name` (Football, Tennis, Ice Hockey, ..., Esports) and for e-sports the game in
//!   `league.region.name` (CS2, Dota 2, ...).
//! * Prices: the `primary` market ("Winner"), `runners[]` tagged HOME / DRAW / AWAY with `price`.

use super::*;

pub struct Leon {
    pub ctag: String,
}

impl Book for Leon {
    fn name(&self) -> &'static str {
        "leon"
    }
    fn source(&self) -> String {
        format!("leon.bet /api-2/betline/events/inplayupcomingall (ctag {}), in play + upcoming", self.ctag)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = format!("https://leon.bet/api-2/betline/events/inplayupcomingall?ctag={}&hideClosed=true&flags=reg,urlv2,orn2,cn,mm2,rrc,cmg", self.ctag);
        let (v, recv_ms) = fetch_json(http.get(url).header("Referer", "https://leon.bet/live")).await?;
        if v["events"].is_null() {
            bail!("unexpected reply: {}", v.to_string().chars().take(160).collect::<String>());
        }
        Ok(Snapshot { events: parse(&v), recv_ms })
    }
}

fn parse(v: &Value) -> Vec<EventIn> {
    let mut out = Vec::new();
    for e in v["events"].as_array().into_iter().flatten() {
        let lg = &e["league"];
        let sport_name = text(&lg["sport"]["name"]);
        let sport: &str = match sport_key(&sport_name) {
            Some(s) => s,
            None if sport_name == "Esports" => match esport_key(&text(&lg["region"]["name"])).or_else(|| esport_key(&text(&lg["name"]))) {
                Some(s) => s,
                None => continue,
            },
            None => continue,
        };
        let side = |t: &str| e["competitors"].as_array().into_iter().flatten().find(|c| c["homeAway"].as_str() == Some(t)).map(|c| text(&c["name"]));
        let (Some(home), Some(away)) = (side("HOME"), side("AWAY")) else { continue };
        let open = e["open"].as_bool() != Some(false);
        let mut quotes = Vec::new();
        if let Some(m) = e["markets"].as_array().into_iter().flatten().find(|m| m["primary"].as_bool() == Some(true) && m["name"].as_str() == Some("Winner") || m["primary"].as_bool() == Some(true) && m["name"].as_str().is_some_and(|n| n.starts_with("Winner ("))) {
            let mopen = open && m["open"].as_bool() != Some(false);
            for r in m["runners"].as_array().into_iter().flatten() {
                let tags: Vec<&str> = r["tags"].as_array().into_iter().flatten().filter_map(|t| t.as_str()).collect();
                let outcome = if tags.contains(&"HOME") {
                    Outcome::Home
                } else if tags.contains(&"DRAW") {
                    Outcome::Draw
                } else if tags.contains(&"AWAY") {
                    Outcome::Away
                } else {
                    continue;
                };
                let p = if mopen && r["open"].as_bool() != Some(false) { num(&r["price"]) } else { None };
                quotes.push(QuoteIn::new(Market::X12, None, outcome, p));
            }
        }
        let live = e["betline"].as_str() == Some("inplay");
        out.push(EventIn {
            id: id_text(&e["id"]),
            sport: sport.into(),
            league: join_league(&[&text(&lg["region"]["name"]), &text(&lg["name"])]),
            home,
            away,
            start_ms: int(&e["kickoff"]),
            sr_id: None,
            sr_weak: false,
            live,
            score: if live { e["liveStatus"]["score"].as_str().map(|s| s.replace(':', "-")) } else { None },
            clock: if live { e["liveStatus"]["progress"].as_str().map(str::to_string) } else { None },
            quotes: winner_quotes(sport, quotes),
        });
    }
    out
}
