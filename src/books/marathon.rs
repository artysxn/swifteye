//! Marathonbet (marathonbet.com): the "event-line" API its React site calls,
//! `https://www.marathonbet.com/eag/event-line/api/v1/...`. Needs the site's own app headers
//! (`X-PAN-SOURCE: REDESIGN_WEB`, `X-PAN-TARGET: BROWSER`, `X-PAN-VERSION: <app version>`); no login.
//!
//! * `GET /sports/menu` → sports with `treeId` and `liveEventCount`.
//! * `GET /sports/{treeId}/all-tournaments/live` → `itemMap[tournament].liveEvents[]` with
//!   `homeTeam/awayTeam.members[].name`, `displayTime` ms and `markets{id: {electedType, state,
//!   selections{selId: {name, coeff.price {n, d}}}}}` (fractional: decimal = 1 + n/d). The 1/X/2
//!   labels of each selection are in `marketGroups[].rows[].cells[] {selId, cellLabel}`.
//! * Main market: `Match_Result` (1X2; two selections in tennis / e-sports), `Match_Winner_OT`
//!   (winner incl. overtime: basketball, hockey).

use super::*;
use std::collections::HashMap;

const API: &str = "https://www.marathonbet.com/eag/event-line/api/v1";

pub struct Marathon {
    /// The site's app version for X-PAN-VERSION (from its asset path /app/<version>/).
    pub version: String,
    pub follows: Vec<&'static str>,
}

fn sport_of(tree_id: i64) -> Option<&'static str> {
    Some(match tree_id {
        11 => "football",
        2398 => "tennis",
        6 => "basketball",
        537 => "hockey",
        382549 => "tabletennis",
        22712 => "volleyball",
        5 => "baseball",
        4 => "amfootball",
        439050 => "mma",
        52914 => "handball",
        1895085 => "esports",
        _ => return None,
    })
}

impl Marathon {
    fn get(&self, http: &reqwest::Client, path: &str) -> reqwest::RequestBuilder {
        http.get(format!("{API}{path}"))
            .header("X-PAN-SOURCE", "REDESIGN_WEB")
            .header("X-PAN-TARGET", "BROWSER")
            .header("X-PAN-VERSION", &self.version)
            .header("Accept-Language", "en")
            .header("Referer", "https://www.marathonbet.com/en/live")
    }
}

impl Book for Marathon {
    fn name(&self) -> &'static str {
        "marathon"
    }
    fn source(&self) -> String {
        "marathonbet.com /eag/event-line/api/v1 (sports menu + all live tournaments per sport)".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let (menu, _) = fetch_json(self.get(http, "/sports/menu")).await?;
        let ids: Vec<(i64, &'static str)> = menu["items"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|s| s["liveEventCount"].as_i64().unwrap_or(0) > 0)
            .filter_map(|s| {
                let id = s["treeId"].as_i64()?;
                let sp = sport_of(id)?;
                (sp == "esports" || self.follows.contains(&sp)).then_some((id, sp))
            })
            .collect();
        let res = futures::future::join_all(ids.iter().map(|(id, _)| fetch_json(self.get(http, &format!("/sports/{id}/all-tournaments/live"))))).await;
        let mut events = Vec::new();
        let mut recv = 0;
        for ((_, sp), r) in ids.iter().zip(res) {
            let (v, t) = r?;
            recv = recv.max(t);
            parse(&v, sp, &mut events);
        }
        Ok(Snapshot { events, recv_ms: if recv > 0 { recv } else { now_ms() } })
    }
}

fn team(t: &Value) -> String {
    t["members"].as_array().into_iter().flatten().map(|m| text(&m["name"])).collect::<Vec<_>>().join("/")
}

fn parse(v: &Value, group: &'static str, out: &mut Vec<EventIn>) {
    for tour in v["itemMap"].as_object().into_iter().flatten().map(|(_, t)| t) {
        let league = text(&tour["name"]);
        let sport: &str = if group == "esports" {
            match esport_key(&league) {
                Some(s) => s,
                None => continue,
            }
        } else {
            group
        };
        for e in tour["liveEvents"].as_array().into_iter().flatten() {
            let (home, away) = (team(&e["homeTeam"]), team(&e["awayTeam"]));
            if home.is_empty() || away.is_empty() {
                continue;
            }
            // 1 / X / 2 label per selection id, from the display rows.
            let mut label: HashMap<i64, String> = HashMap::new();
            for g in e["marketGroups"].as_array().into_iter().flatten() {
                for row in g["rows"].as_array().into_iter().flatten() {
                    for c in row["cells"].as_array().into_iter().flatten() {
                        if let Some(id) = c["selId"].as_i64() {
                            label.insert(id, text(&c["cellLabel"]));
                        }
                    }
                }
            }
            let markets: Vec<&Value> = e["markets"].as_object().into_iter().flatten().map(|(_, m)| m).collect();
            let pick = |kind: &str| markets.iter().find(|m| m["electedType"].as_str() == Some(kind)).copied();
            let m = if three_way(sport) { pick("Match_Result") } else { pick("Match_Winner_OT").or_else(|| pick("Match_Result")) };
            let mut quotes = Vec::new();
            if let Some(m) = m {
                let open = m["state"].as_str() == Some("ACTIVE");
                for (_, s) in m["selections"].as_object().into_iter().flatten() {
                    let id = s["selId"].as_i64().unwrap_or(0);
                    let name = text(&s["name"]);
                    let outcome = match label.get(&id).map(String::as_str) {
                        Some("1") => Outcome::Home,
                        Some("X") => Outcome::Draw,
                        Some("2") => Outcome::Away,
                        _ if name == "Draw" => Outcome::Draw,
                        _ if name.starts_with(&home) => Outcome::Home,
                        _ if name.starts_with(&away) => Outcome::Away,
                        _ => continue,
                    };
                    let p = &s["coeff"]["price"];
                    let price = match (num(&p["n"]), num(&p["d"])) {
                        (Some(n), Some(d)) if d > 0.0 && open => Some(1.0 + n / d),
                        _ => None,
                    };
                    quotes.push(QuoteIn::new(Market::X12, None, outcome, price));
                }
            }
            let sc = &e["matchScore"]["main"];
            out.push(EventIn {
                id: id_text(&e["eventId"]),
                sport: sport.into(),
                league: league.clone(),
                home,
                away,
                start_ms: int(&e["displayTime"]),
                sr_id: None,
                sr_weak: false,
                live: true,
                score: sc["home"].as_str().map(|h| format!("{h}-{}", text(&sc["away"]))),
                clock: e["matchTime"]["timeString"].as_str().map(str::to_string),
                quotes: winner_quotes(sport, quotes),
            });
        }
    }
}
