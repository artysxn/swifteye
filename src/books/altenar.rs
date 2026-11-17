//! Altenar platform (VINN and others): public widget API on biahosted.com.
//! Market typeIds: 1 = 1X2 (odds 1/2/3), 18 = total (sv = line, odds 12/13),
//! 16 = handicap (sv = home line, odds 1714/1715), 29 = BTTS (74/76).

use super::*;
use std::collections::HashMap;

pub struct Altenar {
    pub name: &'static str,
    pub integration: String,
    pub country: String,
    pub origin: String,
}

impl Book for Altenar {
    fn name(&self) -> &'static str {
        self.name
    }
    fn source(&self) -> String {
        format!("Altenar widget API, integration '{}'", self.integration)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = format!(
            "https://sb2frontend-altenar2.biahosted.com/api/widget/GetLiveEvents?culture=en-GB&timezoneOffset=0&integration={}&deviceType=1&numFormat=en-GB&countryCode={}&eventCount=0&sportId=66",
            self.integration, self.country
        );
        let req = http.get(url).header("Origin", &self.origin).header("Referer", format!("{}/", self.origin));
        let (v, recv_ms) = fetch_json(req).await?;

        let by_id = |key: &str| -> HashMap<i64, &Value> {
            v[key].as_array().into_iter().flatten().filter_map(|x| Some((x["id"].as_i64()?, x))).collect()
        };
        let competitors = by_id("competitors");
        let champs = by_id("champs");
        let cats = by_id("categories");
        let markets = by_id("markets");
        let odds = by_id("odds");

        let mut events = Vec::new();
        for e in v["events"].as_array().into_iter().flatten() {
            if e["sportId"].as_i64() != Some(66) {
                continue;
            }
            let comp: Vec<String> = e["competitorIds"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|c| c.as_i64())
                .filter_map(|c| competitors.get(&c).map(|x| text(&x["name"])))
                .collect();
            let (home, away) = if comp.len() == 2 {
                (comp[0].clone(), comp[1].clone())
            } else {
                let name = text(&e["name"]);
                match name.split_once(" vs. ") {
                    Some((h, a)) => (h.to_string(), a.to_string()),
                    None => continue,
                }
            };
            let name_of = |m: &HashMap<i64, &Value>, k: &str| -> String {
                e[k].as_i64().and_then(|id| m.get(&id)).map(|x| text(&x["name"])).unwrap_or_default()
            };
            let score = e["score"].as_array().filter(|s| s.len() == 2).map(|s| format!("{}-{}", s[0], s[1]));
            let mut ev = EventIn {
                id: id_text(&e["id"]),
                sport: "football".into(),
                league: join_league(&[&name_of(&cats, "catId"), &name_of(&champs, "champId")]),
                home,
                away,
                start_ms: e["startDate"].as_str().and_then(rfc3339_ms),
                sr_id: None,
                score,
                clock: e["liveTime"].as_str().map(str::to_string),
                quotes: Vec::new(),
                sr_weak: false,
            };
            for mid in e["marketIds"].as_array().into_iter().flatten().filter_map(|x| x.as_i64()) {
                let Some(m) = markets.get(&mid) else { continue };
                let typ = m["typeId"].as_i64().unwrap_or(0);
                let sv = m["sv"].as_str().and_then(|s| s.trim().parse::<f64>().ok());
                let (market, line) = match typ {
                    1 => (Market::X12, None),
                    18 => match sv {
                        Some(l) => (Market::Total, Some(l)),
                        None => continue,
                    },
                    16 => match sv {
                        Some(l) => (Market::Handicap, Some(l)),
                        None => continue,
                    },
                    29 => (Market::Btts, None),
                    _ => continue,
                };
                for oid in m["oddIds"].as_array().into_iter().flatten().filter_map(|x| x.as_i64()) {
                    let Some(o) = odds.get(&oid) else { continue };
                    let outcome = match (typ, o["typeId"].as_i64().unwrap_or(0)) {
                        (1, 1) => Outcome::Home,
                        (1, 2) => Outcome::Draw,
                        (1, 3) => Outcome::Away,
                        (18, 12) => Outcome::Over,
                        (18, 13) => Outcome::Under,
                        (16, 1714) => Outcome::Home,
                        (16, 1715) => Outcome::Away,
                        (29, 74) => Outcome::Yes,
                        (29, 76) => Outcome::No,
                        _ => continue,
                    };
                    let open = o["oddStatus"].as_i64() == Some(0);
                    let price = if open { num(&o["price"]) } else { None };
                    ev.quotes.push(QuoteIn::new(market, line, outcome, price));
                }
            }
            events.push(ev);
        }
        Ok(Snapshot { events, recv_ms })
    }
}
