//! Linebet (1xBet platform): LiveFeed/Get1x2_VZip filtered to football.
//! Event types: 1/2/3 = 1X2, 7/8 = handicap (P is that team's handicap),
//! 9/10 = total over/under (P = line), 180/181 = both teams to score.

use super::*;

pub struct Linebet {
    pub host: String,
}

impl Book for Linebet {
    fn name(&self) -> &'static str {
        "linebet"
    }
    fn source(&self) -> String {
        format!("{}/service-api/LiveFeed/Get1x2_VZip (football)", self.host)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = format!(
            "https://{}/service-api/LiveFeed/Get1x2_VZip?sports=1&count=500&lng=en&mode=4&country=137&partner=189&getEmpty=true&noFilterBlockEvent=true",
            self.host
        );
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        if v["Success"].as_bool() == Some(false) {
            bail!("feed error: {}", v["Error"]);
        }
        let mut events = Vec::new();
        for g in v["Value"].as_array().into_iter().flatten() {
            if g["SI"].as_i64() != Some(1) {
                continue;
            }
            let sc = &g["SC"];
            let s1 = sc["FS"]["S1"].as_i64().unwrap_or(0);
            let s2 = sc["FS"]["S2"].as_i64().unwrap_or(0);
            let mut ev = EventIn {
                id: id_text(&g["I"]),
                sport: "football".into(),
                league: text(&g["LE"]).chars().take(200).collect::<String>().replace(". ", " / "),
                home: text(if g["O1E"].is_string() { &g["O1E"] } else { &g["O1"] }),
                away: text(if g["O2E"].is_string() { &g["O2E"] } else { &g["O2"] }),
                start_ms: int(&g["S"]).map(|s| s * 1000),
                sr_id: None,
                score: Some(format!("{s1}-{s2}")),
                clock: int(&sc["TS"]).map(|s| format!("{}'", s / 60)),
                quotes: Vec::new(),
                sr_weak: false,
            };
            if ev.league.is_empty() {
                ev.league = text(&g["L"]);
            }
            let game_blocked = g["B"].as_bool() == Some(true);
            // Main events plus the extra lines in AE; dedupe happens in the engine.
            let mains = g["E"].as_array().into_iter().flatten();
            let extra = g["AE"].as_array().into_iter().flatten().flat_map(|ae| ae["ME"].as_array().into_iter().flatten());
            for o in mains.chain(extra) {
                let t = o["T"].as_i64().unwrap_or(0);
                let p = num(&o["P"]);
                let (market, line, outcome) = match t {
                    1 => (Market::X12, None, Outcome::Home),
                    2 => (Market::X12, None, Outcome::Draw),
                    3 => (Market::X12, None, Outcome::Away),
                    7 => (Market::Handicap, Some(p.unwrap_or(0.0)), Outcome::Home),
                    8 => (Market::Handicap, Some(-p.unwrap_or(0.0)), Outcome::Away),
                    9 => match p {
                        Some(l) => (Market::Total, Some(l), Outcome::Over),
                        None => continue,
                    },
                    10 => match p {
                        Some(l) => (Market::Total, Some(l), Outcome::Under),
                        None => continue,
                    },
                    180 => (Market::Btts, None, Outcome::Yes),
                    181 => (Market::Btts, None, Outcome::No),
                    _ => continue,
                };
                // Only full-time markets live in groups 1, 2, 17, 19.
                if !matches!(o["G"].as_i64(), Some(1) | Some(2) | Some(17) | Some(19)) {
                    continue;
                }
                let blocked = game_blocked || o["B"].as_bool() == Some(true);
                let price = if blocked { None } else { num(&o["C"]) };
                ev.quotes.push(QuoteIn::new(market, line, outcome, price));
            }
            events.push(ev);
        }
        Ok(Snapshot { events, recv_ms })
    }
}
