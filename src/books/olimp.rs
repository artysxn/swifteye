//! Olimpbet (olimp.bet, Russia; same platform as olimpbet.kz/.uz): the JSON its own live page loads,
//! `GET /api/v4/0/live/sports-with-competitions-with-events?vids[]=`.
//!
//! * Response: one item per sport, `payload.competitionsWithEvents[].events[]`.
//! * Names come in Russian (`name`) and English (`names["2"]`): the English ones are used.
//!   Event name "Home - Away"; sport names "Soccer", "Basketball", "Ice Hockey", ...
//! * Prices: `outcomes[]`; `shortName` П1 / Х / П2 = home / draw / away; the decimal price is in
//!   the field called `probability`. Market 1 = match result (three-way); two-way sports have their
//!   winner in another market (tennis 2, basketball 4366).
//! * Live only (the line endpoints list only top events).

use super::*;

const URL: &str = "https://www.olimp.bet/api/v4/0/live/sports-with-competitions-with-events?vids%5B%5D=";

pub struct Olimp;

impl Book for Olimp {
    fn name(&self) -> &'static str {
        "olimp"
    }
    fn source(&self) -> String {
        "olimp.bet /api/v4/0/live/sports-with-competitions-with-events (all live sports, match result)".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let req = http.get(URL).header("Referer", "https://www.olimp.bet/live");
        let (v, recv_ms) = fetch_json(req).await?;
        Ok(Snapshot { events: parse(&v), recv_ms })
    }
}

fn en(v: &Value) -> String {
    let s = text(&v["names"]["2"]);
    if s.is_empty() {
        text(&v["name"])
    } else {
        s
    }
}

fn parse(v: &Value) -> Vec<EventIn> {
    let mut out = Vec::new();
    for item in v.as_array().into_iter().flatten() {
        let p = &item["payload"];
        let sport_name = text(&p["sport"]["names"]["2"]);
        for cw in p["competitionsWithEvents"].as_array().into_iter().flatten() {
            let league = en(&cw["competition"]);
            let sport: &str = match sport_key(&sport_name) {
                Some(s) => s,
                // "Cybersport" and similar: the title is in the competition name.
                None => match esport_key(&league).or_else(|| esport_key(&sport_name)) {
                    Some(s) => s,
                    None => continue,
                },
            };
            for e in cw["events"].as_array().into_iter().flatten() {
                let name = en(e);
                let Some((home, away)) = name.split_once(" - ") else { continue };
                let open = e["state"].as_str() == Some("OPEN");
                let outs: Vec<&Value> = e["outcomes"].as_array().into_iter().flatten().collect();
                // Three-way sports: market 1 (regular time). Two-way sports: the first market that
                // has П1/П2 and no Х (tennis uses 2, basketball 4366 = winner incl. overtime).
                let market_id = if three_way(sport) {
                    Some(1)
                } else {
                    outs.iter()
                        .filter(|o| o["shortName"].as_str() == Some("П1"))
                        .filter_map(|o| o["marketId"].as_i64())
                        .find(|m| !outs.iter().any(|x| x["marketId"].as_i64() == Some(*m) && x["shortName"].as_str() == Some("Х")))
                };
                let mut quotes = Vec::new();
                for o in &outs {
                    if o["marketId"].as_i64() != market_id {
                        continue;
                    }
                    let outcome = match o["shortName"].as_str().unwrap_or("") {
                        "П1" => Outcome::Home,
                        "Х" => Outcome::Draw,
                        "П2" => Outcome::Away,
                        _ => continue,
                    };
                    if quotes.iter().any(|q: &QuoteIn| q.outcome == outcome) {
                        continue;
                    }
                    quotes.push(QuoteIn::new(Market::X12, None, outcome, if open { num(&o["probability"]) } else { None }));
                }
                out.push(EventIn {
                    id: text(&e["id"]),
                    sport: sport.into(),
                    league: league.clone(),
                    home: home.trim().to_string(),
                    away: away.trim().to_string(),
                    start_ms: int(&e["startDateTime"]).map(|s| if s < 10_000_000_000 { s * 1000 } else { s }),
                    sr_id: None,
                    sr_weak: false,
                    live: true,
                    score: e["score"].as_str().map(|s| s.replace(':', "-")),
                    clock: None,
                    quotes: winner_quotes(sport, quotes),
                });
            }
        }
    }
    out
}
