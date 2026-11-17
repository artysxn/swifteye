//! Kambi platform (Unibet and others): public offering API on kambicdn.com.

use super::*;

pub struct Kambi {
    pub name: &'static str,
    /// Kambi offering code, e.g. "ubse" for unibet.se.
    pub offering: String,
    pub market: String,
    pub lang: String,
}

impl Book for Kambi {
    fn name(&self) -> &'static str {
        self.name
    }
    fn source(&self) -> String {
        format!("Kambi offering '{}' in-play list (eu-offering-api.kambicdn.com)", self.offering)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = format!(
            "https://eu-offering-api.kambicdn.com/offering/v2018/{}/listView/football/all/all/all/in-play.json?lang={}&market={}&useCombined=true&ncid={}",
            self.offering,
            self.lang,
            self.market,
            now_ms()
        );
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        let mut events = Vec::new();
        for item in v["events"].as_array().into_iter().flatten() {
            let e = &item["event"];
            if e["sport"].as_str() != Some("FOOTBALL") {
                continue;
            }
            let path: Vec<&str> = e["path"]
                .as_array()
                .into_iter()
                .flatten()
                .skip(1)
                .filter_map(|p| p["englishName"].as_str())
                .collect();
            let live = &item["liveData"];
            let score = match (live["score"]["home"].as_str(), live["score"]["away"].as_str()) {
                (Some(h), Some(a)) => Some(format!("{h}-{a}")),
                _ => None,
            };
            let clock = live["matchClock"]["minute"].as_i64().map(|m| format!("{m}'"));
            let mut ev = EventIn {
                id: id_text(&e["id"]),
                sport: "football".into(),
                league: join_league(&path),
                home: text(&e["homeName"]),
                away: text(&e["awayName"]),
                start_ms: e["start"].as_str().and_then(rfc3339_ms),
                sr_id: None,
                score,
                clock,
                quotes: Vec::new(),
                sr_weak: false,
            };
            for bo in item["betOffers"].as_array().into_iter().flatten() {
                let typ = bo["betOfferType"]["id"].as_i64().unwrap_or(0);
                let crit = bo["criterion"]["englishLabel"].as_str().unwrap_or("");
                let lifetime = bo["criterion"]["lifetime"].as_str().unwrap_or("FULL_TIME");
                if lifetime != "FULL_TIME" {
                    continue;
                }
                let market = match (typ, crit) {
                    (2, "Full Time") => Market::X12,
                    (6, "Total Goals") => Market::Total,
                    (7, _) if crit.contains("Asian Handicap") => Market::Handicap,
                    (18, "Both Teams To Score") => Market::Btts,
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
                    let ts = o["changedDate"].as_str().and_then(rfc3339_ms);
                    ev.quotes.push(QuoteIn::new(market, line, outcome, price).ts(ts));
                }
            }
            events.push(ev);
        }
        Ok(Snapshot { events, recv_ms })
    }
}
