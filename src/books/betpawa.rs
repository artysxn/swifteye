//! betPawa: sportsbook v4 "lists/by-queries" for live football, 1X2 only.

use super::*;

pub struct BetPawa {
    /// Site host, e.g. "www.betpawa.ng".
    pub host: String,
    /// Brand header value, e.g. "betpawa-nigeria".
    pub brand: String,
    pub pre: Pre,
}

const TAKE: usize = 100;

impl Book for BetPawa {
    fn name(&self) -> &'static str {
        "betpawa"
    }
    fn source(&self) -> String {
        format!("{}/api/sportsbook/v4/events/lists/by-queries (LIVE, football, 1X2)", self.host)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let (host, brand, hours) = (self.host.clone(), self.brand.clone(), self.pre.hours);
        refresh_pre(&mut self.pre, "betpawa", || async move {
            let until = now_ms() + (hours * 3_600_000.0) as i64;
            Ok(list(http, &host, &brand, "UPCOMING", Some(until)).await?.0)
        })
        .await;
        let (mut events, recv_ms) = list(http, &self.host, &self.brand, "LIVE", None).await?;
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}

/// One betPawa list, paged. UPCOMING is sorted by kick-off and stops past `until`.
async fn list(http: &reqwest::Client, host: &str, brand: &str, kind: &str, until: Option<Ms>) -> Result<(Vec<EventIn>, Ms)> {
    let mut events = Vec::new();
    let mut recv_ms = 0;
    let sort = if kind == "LIVE" { serde_json::json!({"competitionPriority":"DESC"}) } else { serde_json::json!({"startTime":"ASC"}) };
    for page in 0..10 {
        let q = serde_json::json!({"queries":[{
            "query":{"eventType": kind,"categories":["2"],"zones":{}},
            "view":{"marketTypes":["3743"]},
            "skip": page * TAKE, "take": TAKE,
            "sort": sort
        }]});
        let req = http
            .get(format!("https://{host}/api/sportsbook/v4/events/lists/by-queries"))
            .query(&[("q", q.to_string())])
            .header("x-pawa-brand", brand)
            .header("x-pawa-language", "en");
        let (v, t) = fetch_json(req).await?;
        recv_ms = recv_ms.max(t);
        let list = v["responses"][0]["responses"].as_array().cloned().unwrap_or_default();
        let n = list.len();
        let mut past = false;
        for e in &list {
            let mut ev = parse(e);
            ev.live = kind == "LIVE";
            if let (Some(u), Some(st)) = (until, ev.start_ms) {
                if st > u {
                    past = true;
                    continue;
                }
            }
            events.push(ev);
        }
        if n < TAKE || past {
            break;
        }
    }
    Ok((events, recv_ms))
}

fn parse(e: &Value) -> EventIn {
    let mut home = String::new();
    let mut away = String::new();
    for p in e["participants"].as_array().into_iter().flatten() {
        match p["position"].as_i64() {
            Some(1) => home = text(&p["name"]),
            Some(2) => away = text(&p["name"]),
            _ => {}
        }
    }
    let sr_id = e["widgets"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|w| w["type"].as_str() == Some("SPORTRADAR"))
        .map(|w| text(&w["id"]));
    let display = &e["results"]["display"];
    // Full-time score per participant lives in participantPeriodResults.
    let side_score = |side: &str| -> Option<String> {
        e["results"]["participantPeriodResults"].as_array()?.iter().find(|p| p["participant"]["type"].as_str() == Some(side)).and_then(|p| {
            p["periodResults"].as_array()?.iter().find(|r| r["period"]["slug"].as_str() == Some("FULL_TIME_EXCLUDING_OVERTIME")).map(|r| text(&r["result"]))
        })
    };
    let score = match (side_score("HOME"), side_score("AWAY")) {
        (Some(h), Some(a)) => Some(format!("{h}-{a}")),
        _ => None,
    };
    let mut ev = EventIn {
        id: text(&e["id"]),
        sport: "football".into(),
        league: join_league(&[e["region"]["name"].as_str().unwrap_or(""), e["competition"]["name"].as_str().unwrap_or("")]),
        home,
        away,
        start_ms: e["startTime"].as_str().and_then(rfc3339_ms),
        sr_id,
        score,
        clock: display["minute"].as_str().filter(|s| !s.is_empty()).map(|s| format!("{s}'")),
        quotes: Vec::new(),
        sr_weak: false,
                live: true,
    };
    for m in e["markets"].as_array().into_iter().flatten() {
        if text(&m["marketType"]["id"]) != "3743" {
            continue;
        }
        for row in m["row"].as_array().into_iter().flatten() {
            for p in row["prices"].as_array().into_iter().flatten() {
                let outcome = match p["name"].as_str() {
                    Some("1") => Outcome::Home,
                    Some("X") => Outcome::Draw,
                    Some("2") => Outcome::Away,
                    _ => continue,
                };
                let price = if p["suspended"].as_bool() == Some(true) { None } else { num(&p["odds"]) };
                ev.quotes.push(QuoteIn::new(Market::X12, None, outcome, price));
            }
        }
    }
    ev
}
