//! Kambi platform (Unibet and others): public offering API on kambicdn.com.
//! Live: the in-play list. Pre-match: the matches list (games not started yet).

use super::*;

pub struct Kambi {
    pub name: &'static str,
    /// Kambi offering code, e.g. "ubse" for unibet.se.
    pub offering: String,
    pub market: String,
    pub lang: String,
    /// Kambi sport slugs asked for in the pre-match lists (the live list covers every sport).
    pub sports: Vec<&'static str>,
    pub pre: Pre,
}

/// (Kambi list slug, our sport key).
pub const SPORT_SLUGS: &[(&str, &str)] = &[
    ("football", "football"),
    ("basketball", "basketball"),
    ("tennis", "tennis"),
    ("ice_hockey", "hockey"),
    ("baseball", "baseball"),
    ("volleyball", "volleyball"),
    ("american_football", "amfootball"),
    ("ufc_mma", "mma"),
    ("handball", "handball"),
    ("table_tennis", "tabletennis"),
    ("esports", "esports"),
];

pub fn sport_slugs(follows: impl Fn(&str) -> bool) -> Vec<&'static str> {
    SPORT_SLUGS.iter().filter(|(_, s)| follows(s)).map(|x| x.0).collect()
}

impl Kambi {
    fn url(&self, sport: &str, list: &str) -> String {
        format!(
            "https://eu-offering-api.kambicdn.com/offering/v2018/{}/listView/{sport}/all/all/all/{list}.json?lang={}&market={}&useCombined=true&ncid={}",
            self.offering,
            self.lang,
            self.market,
            now_ms()
        )
    }
}

impl Book for Kambi {
    fn name(&self) -> &'static str {
        self.name
    }
    fn source(&self) -> String {
        format!("Kambi offering '{}': in-play list + upcoming matches", self.offering)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let pre_urls: Vec<String> = self.sports.iter().map(|s| self.url(s, "matches")).collect();
        refresh_pre(&mut self.pre, self.name, || async {
            let res = futures::future::join_all(pre_urls.into_iter().map(|u| fetch_json(http.get(u)))).await;
            let mut out = Vec::new();
            let mut last_err = None;
            for r in res {
                match r {
                    Ok((v, _)) => out.extend(parse(&v, Some("NOT_STARTED"))),
                    Err(e) => last_err = Some(e),
                }
            }
            match (out.is_empty(), last_err) {
                (true, Some(e)) => Err(e),
                _ => Ok(out),
            }
        })
        .await;
        let (v, recv_ms) = fetch_json(http.get(self.url("all", "in-play"))).await?;
        let mut events = parse(&v, None);
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}

fn parse(v: &Value, state: Option<&str>) -> Vec<EventIn> {
    let mut events = Vec::new();
    for item in v["events"].as_array().into_iter().flatten() {
        let e = &item["event"];
        let path_names: Vec<&str> = e["path"].as_array().into_iter().flatten().filter_map(|p| p["englishName"].as_str()).collect();
        let raw_sport = e["sport"].as_str().unwrap_or("");
        let sport: &str = match sport_key(raw_sport) {
            Some(s) => s,
            // E-sports come as COUNTER_STRIKE, DOTA, ... under the "Esports" path.
            None if path_names.first() == Some(&"Esports") => match path_names.get(1).and_then(|t| esport_key(t)) {
                Some(s) => s,
                None => continue,
            },
            None => continue,
        };
        if let Some(st) = state {
            if e["state"].as_str() != Some(st) {
                continue;
            }
        }
        let path: Vec<&str> = e["path"].as_array().into_iter().flatten().skip(1).filter_map(|p| p["englishName"].as_str()).collect();
        let live = &item["liveData"];
        let score = match (live["score"]["home"].as_str(), live["score"]["away"].as_str()) {
            (Some(h), Some(a)) => Some(format!("{h}-{a}")),
            _ => None,
        };
        let mut ev = EventIn {
            id: id_text(&e["id"]),
            sport: sport.into(),
            league: join_league(&path),
            home: text(&e["homeName"]),
            away: text(&e["awayName"]),
            start_ms: e["start"].as_str().and_then(rfc3339_ms),
            sr_id: None,
            score,
            clock: live["matchClock"]["minute"].as_i64().map(|m| format!("{m}'")),
            quotes: Vec::new(),
            sr_weak: false,
            live: e["state"].as_str() == Some("STARTED"),
        };
        for bo in item["betOffers"].as_array().into_iter().flatten() {
            let typ = bo["betOfferType"]["id"].as_i64().unwrap_or(0);
            let crit = bo["criterion"]["englishLabel"].as_str().unwrap_or("");
            let lifetime = bo["criterion"]["lifetime"].as_str().unwrap_or("FULL_TIME");
            let three = three_way(sport);
            if !(lifetime == "FULL_TIME" || (!three && lifetime == "FULL_TIME_OVERTIME")) {
                continue;
            }
            let market = match (typ, crit) {
                (2, "Full Time") if sport == "football" => Market::X12,
                (2, _) if sport == "handball" && crit.starts_with("Full Time") => Market::X12,
                // Two-way winner: "Match Odds", "Moneyline - Including Overtime", "Bout Odds".
                (2, _) if !three && bo["outcomes"].as_array().is_some_and(|o| o.len() == 2) && (crit.starts_with("Match Odds") && !crit.contains("Regular Time") || crit.starts_with("Moneyline") || crit == "Match" || crit == "Bout Odds") => Market::X12,
                (6, "Total Goals") if sport == "football" => Market::Total,
                (7, _) if sport == "football" && crit.contains("Asian Handicap") => Market::Handicap,
                (18, "Both Teams To Score") if sport == "football" => Market::Btts,
                _ => continue,
            };
            let offer_suspended = bo["suspended"].as_bool().unwrap_or(false);
            for o in bo["outcomes"].as_array().into_iter().flatten() {
                let outcome = match o["type"].as_str().unwrap_or("") {
                    "OT_ONE" => Outcome::Home,
                    "OT_CROSS" => Outcome::Draw,
                    "OT_TWO" => Outcome::Away,
                    "OT_OVER" => Outcome::Over,
                    "OT_UNDER" => Outcome::Under,
                    "OT_YES" => Outcome::Yes,
                    "OT_NO" => Outcome::No,
                    _ => continue,
                };
                let raw_line = o["line"].as_f64().map(|l| l / 1000.0);
                let line = match market {
                    Market::Total => raw_line,
                    // Kambi gives each side its own handicap; store the home side's.
                    Market::Handicap => match outcome {
                        Outcome::Away => raw_line.map(|l| -l),
                        _ => raw_line,
                    },
                    _ => None,
                };
                if matches!(market, Market::Total | Market::Handicap) && line.is_none() {
                    continue;
                }
                let open = !offer_suspended && o["status"].as_str() == Some("OPEN");
                let price = if open { o["odds"].as_f64().map(|x| x / 1000.0) } else { None };
                ev.quotes.push(QuoteIn::new(market, line, outcome, price).ts(o["changedDate"].as_str().and_then(rfc3339_ms)));
            }
        }
        ev.quotes = winner_quotes(sport, ev.quotes);
        events.push(ev);
    }
    events
}
