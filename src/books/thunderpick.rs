//! Thunderpick: POST /api/matches. One call returns live and upcoming games with their main
//! market (1X2 when a draw is offered, otherwise a two-way "Match Winner").
//!
//! Football (gameId 10) cannot share a request with the e-sports ids, so there are two calls
//! per poll. E-sports ids seen so far: 1 Overwatch, 3 League of Legends, 4 Dota 2, 6 Counter-Strike 2.
//! Other ids are asked for as well and show up as the generic "esports" sport, which the
//! matcher lets pair with any title.

use super::*;

pub struct Thunderpick {
    pub pre_hours: f64,
    pub esports: bool,
}

const ESPORT_IDS: &[i64] = &[1, 2, 3, 4, 5, 6, 7, 8, 9];

fn game_sport(id: i64) -> &'static str {
    match id {
        10 => "football",
        1 => "ow",
        3 => "lol",
        4 => "dota2",
        6 => "cs2",
        _ => "esports",
    }
}

impl Thunderpick {
    async fn call(&self, http: &reqwest::Client, ids: &[i64]) -> Result<(Value, Ms)> {
        let body = serde_json::json!({"gameIds":ids,"competitionId":null,"country":null,"parentTournamentUrlAcronym":null,"matchesView":"normal"});
        let req = http.post("https://thunderpick.io/api/matches").header("Referer", "https://thunderpick.io/sports").json(&body);
        fetch_json(req).await
    }
}

impl Book for Thunderpick {
    fn name(&self) -> &'static str {
        "thunderpick"
    }
    fn source(&self) -> String {
        if self.esports {
            "thunderpick.io/api/matches (football 1X2 + e-sports match winner)".into()
        } else {
            "thunderpick.io/api/matches (football, 1X2)".into()
        }
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let until = now_ms() + (self.pre_hours * 3_600_000.0) as i64;
        let mut replies = vec![self.call(http, &[10]).await?];
        if self.esports {
            // The e-sports list is optional: a hiccup there must not blank the football list.
            match self.call(http, ESPORT_IDS).await {
                Ok(r) => replies.push(r),
                Err(e) => tracing::warn!("thunderpick e-sports: {e:#}"),
            }
        }
        let recv_ms = replies.iter().map(|r| r.1).max().unwrap_or_else(now_ms);
        let mut events = Vec::new();
        for (v, _) in &replies {
            for (list, live) in [("live", true), ("upcoming", false)] {
                for m in v["data"][list].as_array().into_iter().flatten() {
                    let Some(gid) = m["gameId"].as_i64() else { continue };
                    let sport = game_sport(gid);
                    if sport == "football" && gid != 10 {
                        continue;
                    }
                    let start = m["startTime"].as_str().and_then(rfc3339_ms);
                    if !live && start.map_or(true, |s| s > until) {
                        continue;
                    }
                    let mk = &m["market"];
                    let mut quotes = Vec::new();
                    // A two-way market (no draw) is only meaningful for e-sports; a football
                    // game without a draw price is some other market and is skipped.
                    let has_draw = mk["draw"].is_object();
                    if has_draw || sport != "football" {
                        let open = mk["status"].as_i64() == Some(1);
                        let sides: &[(&str, Outcome)] = if has_draw {
                            &[("home", Outcome::Home), ("draw", Outcome::Draw), ("away", Outcome::Away)]
                        } else {
                            &[("home", Outcome::Home), ("away", Outcome::Away)]
                        };
                        for (k, o) in sides {
                            let sel = &mk[*k];
                            let price = if open && sel["status"].as_i64() == Some(1) { num(&sel["odds"]) } else { None };
                            quotes.push(QuoteIn::new(Market::X12, None, *o, price));
                        }
                    }
                    let sc = &m["score"]["score"];
                    events.push(EventIn {
                        id: id_text(&m["id"]),
                        sport: sport.into(),
                        league: text(&m["competition"]["name"]).if_empty(|| text(&m["tournament"]["name"])),
                        home: text(&m["teams"]["home"]["name"]),
                        away: text(&m["teams"]["away"]["name"]),
                        start_ms: start,
                        sr_id: None,
                        sr_weak: false,
                        live: live || m["isLive"].as_bool() == Some(true),
                        score: if live { Some(format!("{}-{}", text(&sc["homeScore"]), text(&sc["awayScore"]))) } else { None },
                        clock: None,
                        quotes,
                    });
                }
            }
        }
        Ok(Snapshot { events, recv_ms })
    }
}

trait IfEmpty {
    fn if_empty(self, f: impl FnOnce() -> String) -> String;
}

impl IfEmpty for String {
    fn if_empty(self, f: impl FnOnce() -> String) -> String {
        if self.is_empty() {
            f()
        } else {
            self
        }
    }
}
