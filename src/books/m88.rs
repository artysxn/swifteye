//! M88 (Mansion88), an Asian book: the JSON its mobile site posts to
//! `msports.m88.com/api/v1/M88S/data/main` with the guest bearer ("Bearer GUEST").
//!
//! * Live: `method = LoadOddsDataLive, display_type 11`; pre-match (today/early):
//!   `LoadOddsData, display_type 12`. One call per sport.
//! * `odds_type 3` = decimal odds. Each odds field is "price|price".
//! * Markets come as slots a..i, each with `game_type_x`: 1 = 1X2 full time (odds_1 home,
//!   odds_2 draw, odds_3 away), 12 = two-way winner (odds_1 home, odds_3 away).
//!   0/5 = handicap / over-under (not used yet). `status_x` "0|..." = open.
//! * `match_date` "YYYYMMDDHHMM" in UTC+8. Sport ids: 10 football, 11 tennis, 12 basketball,
//!   52 e-sports (title in front of the league: "Dota 2 - 1win Essence").

use super::*;

const URL: &str = "https://msports.m88.com/api/v1/M88S/data/main";
pub const SPORT_IDS: &[(&str, &str)] = &[("10", "football"), ("11", "tennis"), ("12", "basketball"), ("52", "esports")];

pub struct M88 {
    pub sports: Vec<(&'static str, &'static str)>,
    pub pre: Pre,
}

impl M88 {
    pub fn new(follows: impl Fn(&str) -> bool, pre: Pre) -> Self {
        M88 { sports: SPORT_IDS.iter().copied().filter(|(_, s)| follows(s)).collect(), pre }
    }
}

fn body(sport: &str, live: bool) -> Value {
    serde_json::json!({
        "lang_id": "en", "view_type": "0", "leagues": "999", "odds_type": "3", "odds_group": "3",
        "view_date": "0", "time_filter": "0", "category": "0", "branch_code": "888", "currency": "MYR",
        "is_full": "true", "prev_params": "", "version": "", "is_stream": "false", "is_live_center": "false",
        "sport_id": sport, "display_type": if live { "11" } else { "12" }, "top_x": 0,
        "currentMenu": "spesificSport", "currentTimeSlot": if live { "1" } else { "2" }, "prevMenu": "spesificSport",
        "method": if live { "LoadOddsDataLive" } else { "LoadOddsData" },
    })
}

async fn call(http: &reqwest::Client, sport: &'static str, key: &'static str, live: bool) -> Result<(Vec<EventIn>, Ms)> {
    let req = http
        .post(URL)
        .header("authorization", "Bearer GUEST")
        .header("Origin", "https://msports.m88.com")
        .header("Referer", "https://msports.m88.com/app/v2/")
        .json(&body(sport, live));
    let (v, t) = fetch_json(req).await?;
    // status 1 = data; 204 = nothing for this sport right now.
    if v["status"].as_i64() == Some(204) {
        return Ok((Vec::new(), t));
    }
    if v["status"].as_i64() != Some(1) {
        bail!("status {}: {}", v["status"], v["data"]);
    }
    let mut out = Vec::new();
    for block in v["data"].as_array().into_iter().flatten() {
        for d in block["data"].as_array().into_iter().flatten() {
            if let Some(e) = parse(d, key, live) {
                out.push(e);
            }
        }
    }
    Ok((out, t))
}

fn price(d: &Value, k: &str) -> Option<f64> {
    d[k].as_str()?.split('|').next()?.trim().parse().ok()
}

fn parse(d: &Value, key: &'static str, live: bool) -> Option<EventIn> {
    let league = text(&d["event_name"]).split('|').next().unwrap_or("").to_string();
    let sport: &str = if key == "esports" { esport_key(league.split(" - ").next().unwrap_or(""))? } else { key };
    let home = text(&d["club_home"]);
    let away = text(&d["club_away"]);
    if home.is_empty() || away.is_empty() {
        return None;
    }
    let md = text(&d["match_date"]);
    let start = (md.len() == 12)
        .then(|| naive_ms(&format!("{}-{}-{} {}:{}:00", &md[0..4], &md[4..6], &md[6..8], &md[8..10], &md[10..12]), 8))
        .flatten();
    let is_live = live || text(&d["is_live"]) == "1";
    let mut ev = EventIn {
        // The same game keeps `no_partai` from pre-match into play.
        id: text(&d["no_partai"]),
        sport: sport.into(),
        league: league.clone(),
        home,
        away,
        start_ms: start,
        sr_id: None,
        sr_weak: false,
        live: is_live,
        score: if is_live { Some(format!("{}-{}", text(&d["home_score"]).split('_').next().unwrap_or("0"), text(&d["away_score"]))) } else { None },
        clock: None,
        quotes: Vec::new(),
    };
    let want = if three_way(sport) { "1" } else { "12" };
    for slot in ["a", "b", "c", "d", "e", "f", "g", "h", "i"] {
        if text(&d[format!("game_type_{slot}").as_str()]) != want {
            continue;
        }
        let open = text(&d[format!("status_{slot}").as_str()]).starts_with('0');
        let mut sides = vec![("odds_1_", Outcome::Home), ("odds_3_", Outcome::Away)];
        if want == "1" {
            sides.insert(1, ("odds_2_", Outcome::Draw));
        }
        for (k, o) in sides {
            let p = if open { price(d, &format!("{k}{slot}")) } else { None };
            ev.quotes.push(QuoteIn::new(Market::X12, None, o, p));
        }
        break;
    }
    ev.quotes = winner_quotes(sport, ev.quotes);
    Some(ev)
}

impl Book for M88 {
    fn name(&self) -> &'static str {
        "m88"
    }
    fn source(&self) -> String {
        let s: Vec<&str> = self.sports.iter().map(|x| x.1).collect();
        format!("msports.m88.com guest odds API (decimal), {}", s.join(", "))
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let sports = self.sports.clone();
        refresh_pre(&mut self.pre, "m88", || async {
            let res = futures::future::join_all(sports.iter().map(|(id, k)| call(http, id, k, false))).await;
            let mut out = Vec::new();
            let mut err = None;
            for r in res {
                match r {
                    Ok((e, _)) => out.extend(e),
                    Err(e) => err = Some(e),
                }
            }
            match (out.is_empty(), err) {
                (true, Some(e)) => Err(e),
                _ => Ok(out),
            }
        })
        .await;
        let res = futures::future::join_all(sports.iter().map(|(id, k)| call(http, id, k, true))).await;
        let mut events = Vec::new();
        let mut recv_ms = 0;
        let mut errs = Vec::new();
        for r in res {
            match r {
                Ok((e, t)) => {
                    events.extend(e);
                    recv_ms = recv_ms.max(t);
                }
                Err(e) => errs.push(format!("{e:#}")),
            }
        }
        if !sports.is_empty() && errs.len() == sports.len() {
            bail!("{}", errs.join("; "));
        }
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms: if recv_ms > 0 { recv_ms } else { now_ms() } })
    }
}
