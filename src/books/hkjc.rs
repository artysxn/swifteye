//! HKJC (Hong Kong Jockey Club, bet.hkjc.com): the GraphQL GET its football pages make to
//! `info.cld.hkjc.com/graphql/base-get/` (Apollo, `useGETForQueries`).
//!
//! * The server takes only the query text the site sends (`QUERY`, copied from the page) and
//!   rejects a GET without `Content-Type: application/json` with HTTP 400
//!   "Internal server error - BAD_REQUEST" (Apollo's CSRF guard). Edge-cached replies can hide
//!   that, so a missing header looks like random 400s.
//! * One market family per request: `fbOddsTypes` HAD+EHA (1X2), HDC+EDC (handicap) or
//!   HIL+EHL (total). Mixing them answers "Invalid queration arguments". EHA/EDC/EHL are the
//!   extra-time pools; only HAD/HDC/HIL are stored.
//! * `inplayOnly: true` lists every game with in-play pools, kicked off or not; `status` tells
//!   them apart (PREEVENT, FIRSTHALF, FIRSTHALFCOMPLETED, SECONDHALF, ...).
//! * `condition` is the line: HDC is the HOME handicap ("-0.5/-1.0" = quarter line -0.75),
//!   HIL the goal line ("2.5/3.0" = 2.75). Combination `str`: H/D/A for HAD, H/A for HDC,
//!   H (high = over) / L (low = under) for HIL. Odds are decimal strings.
//! * A pool's `updateAt` did not change when its prices did (checked 2026-11-22), so there is no
//!   source stamp. Guests' own page refreshes every 12 s (logged-in users get MQTT push).
//! * Kick-off times carry +08:00. No Sportradar id.

use super::*;

const URL: &str = "https://info.cld.hkjc.com/graphql/base-get/";
const PAGE: usize = 60;

const QUERY: &str = "query matchList($startIndex:Int$endIndex:Int$startDate:String$endDate:String$matchIds:[String]$tournIds:[String]$fbOddsTypes:[FBOddsType]!$fbOddsTypesM:[FBOddsType]!$inplayOnly:Boolean$featuredMatchesOnly:Boolean$frontEndIds:[String]$earlySettlementOnly:Boolean$showAllMatch:Boolean){matches(startIndex:$startIndex endIndex:$endIndex startDate:$startDate endDate:$endDate matchIds:$matchIds tournIds:$tournIds fbOddsTypes:$fbOddsTypesM inplayOnly:$inplayOnly featuredMatchesOnly:$featuredMatchesOnly frontEndIds:$frontEndIds earlySettlementOnly:$earlySettlementOnly showAllMatch:$showAllMatch){id frontEndId matchDate kickOffTime status updateAt sequence esIndicatorEnabled homeTeam{id name_en name_ch}awayTeam{id name_en name_ch}tournament{id frontEndId nameProfileId isInteractiveServiceAvailable code name_en name_ch}isInteractiveServiceAvailable inplayDelay venue{code name_en name_ch}tvChannels{code name_en name_ch}liveEvents{id code}featureStartTime featureMatchSequence poolInfo{normalPools inplayPools sellingPools ntsInfo entInfo definedPools ngsInfo{str name_en name_ch instNo}agsInfo{str name_en name_ch}}runningResult{homeScore awayScore corner homeCorner awayCorner}runningResultExtra{homeScore awayScore corner homeCorner awayCorner}adminOperation{remark{typ}}foPools(fbOddsTypes:$fbOddsTypes){id status oddsType instNo inplay name_ch name_en updateAt expectedSuspendDateTime lines{lineId status condition main combinations{combId str status offerEarlySettlement currentOdds selections{selId str name_ch name_en}}}}}}";

pub struct Hkjc {
    pub pre: Pre,
}

/// One page of `matches` for a market family.
async fn page(http: &reqwest::Client, types: [&str; 2], inplay: bool, start: usize) -> Result<(Vec<Value>, Ms)> {
    let vars = serde_json::json!({
        "fbOddsTypes": types,
        "fbOddsTypesM": if inplay { vec!["ALL"] } else { types.to_vec() },
        "inplayOnly": inplay,
        "featuredMatchesOnly": false,
        "startIndex": start,
        "endIndex": start + PAGE - 1,
        "earlySettlementOnly": false,
        "showAllMatch": false,
    });
    let req = http
        .get(URL)
        .query(&[("query", QUERY), ("variables", &vars.to_string())])
        .header("Content-Type", "application/json")
        .header("Referer", "https://bet.hkjc.com/")
        .header("Origin", "https://bet.hkjc.com");
    let (v, t) = fetch_json(req).await?;
    if let Some(e) = v["errors"].as_array().and_then(|a| a.first()) {
        bail!("graphql: {}", e["message"]);
    }
    match v["data"]["matches"].as_array() {
        Some(a) => Ok((a.clone(), t)),
        None => bail!("no data.matches: {}", &v.to_string()[..v.to_string().len().min(200)]),
    }
}

/// Every page of a list (stops at a short page; at most `max_pages`).
async fn all(http: &reqwest::Client, types: [&str; 2], inplay: bool, max_pages: usize) -> Result<(Vec<Value>, Ms)> {
    let mut out = Vec::new();
    let mut recv = 0;
    for p in 0..max_pages {
        let (ms, t) = page(http, types, inplay, 1 + p * PAGE).await?;
        recv = recv.max(t);
        let n = ms.len();
        out.extend(ms);
        if n < PAGE {
            break;
        }
    }
    Ok((out, recv))
}

/// "-0.5/-1.0" -> -0.75, "+1.0" -> 1.0, "2.5" -> 2.5.
fn line(cond: &str) -> Option<f64> {
    let parts: Vec<f64> = cond.split('/').map(|s| s.trim().trim_start_matches('+').parse::<f64>()).collect::<Result<_, _>>().ok()?;
    (!parts.is_empty()).then(|| parts.iter().sum::<f64>() / parts.len() as f64)
}

fn is_live(m: &Value) -> bool {
    let s = m["status"].as_str().unwrap_or("");
    s != "PREEVENT" && !m["runningResult"].is_null()
}

fn event(m: &Value) -> EventIn {
    let rr = &m["runningResult"];
    let live = is_live(m);
    EventIn {
        id: text(&m["id"]),
        sport: "football".into(),
        league: text(&m["tournament"]["name_en"]),
        home: text(&m["homeTeam"]["name_en"]),
        away: text(&m["awayTeam"]["name_en"]),
        start_ms: m["kickOffTime"].as_str().and_then(rfc3339_ms),
        sr_id: None,
        sr_weak: false,
        live,
        score: if live { Some(format!("{}-{}", text(&rr["homeScore"]), text(&rr["awayScore"]))) } else { None },
        clock: None,
        quotes: Vec::new(),
    }
}

/// Quotes of the HAD / HDC / HIL pools on one match.
fn quotes(m: &Value) -> Vec<QuoteIn> {
    let mut out = Vec::new();
    for p in m["foPools"].as_array().into_iter().flatten() {
        let kind = p["oddsType"].as_str().unwrap_or("");
        if !matches!(kind, "HAD" | "HDC" | "HIL") {
            continue;
        }
        let pool_open = p["status"].as_str() == Some("SELLINGSTARTED");
        for l in p["lines"].as_array().into_iter().flatten() {
            let (market, ln) = match kind {
                "HAD" => (Market::X12, None),
                "HDC" => match line(l["condition"].as_str().unwrap_or("")) {
                    Some(x) => (Market::Handicap, Some(x)),
                    None => continue,
                },
                _ => match line(l["condition"].as_str().unwrap_or("")) {
                    Some(x) => (Market::Total, Some(x)),
                    None => continue,
                },
            };
            let line_open = pool_open && l["status"].as_str() == Some("AVAILABLE");
            for c in l["combinations"].as_array().into_iter().flatten() {
                let outcome = match (kind, c["str"].as_str().unwrap_or("")) {
                    ("HAD", "H") | ("HDC", "H") => Outcome::Home,
                    ("HAD", "D") => Outcome::Draw,
                    ("HAD", "A") | ("HDC", "A") => Outcome::Away,
                    ("HIL", "H") => Outcome::Over,
                    ("HIL", "L") => Outcome::Under,
                    _ => continue,
                };
                let open = line_open && c["status"].as_str() == Some("AVAILABLE");
                out.push(QuoteIn::new(market, ln, outcome, if open { num(&c["currentOdds"]) } else { None }));
            }
        }
    }
    out
}

impl Book for Hkjc {
    fn name(&self) -> &'static str {
        "hkjc"
    }
    fn source(&self) -> String {
        "info.cld.hkjc.com GraphQL (bet.hkjc.com): in-play 1X2 + handicap + total, pre-match 1X2".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        refresh_pre(&mut self.pre, "hkjc", || async {
            let (ms, _) = all(http, ["HAD", "EHA"], false, 5).await?;
            Ok(ms
                .iter()
                .filter(|m| m["status"].as_str() == Some("PREEVENT"))
                .map(|m| {
                    let mut e = event(m);
                    e.quotes = quotes(m);
                    e
                })
                .collect())
        })
        .await;

        // The 1X2 list decides which games are live; handicap and total add their lines.
        let (had, hdc, hil) = tokio::join!(
            all(http, ["HAD", "EHA"], true, 3),
            all(http, ["HDC", "EDC"], true, 3),
            all(http, ["HIL", "EHL"], true, 3),
        );
        let (had, recv_ms) = had?;
        let mut events: Vec<EventIn> = Vec::new();
        let mut index = std::collections::HashMap::new();
        for m in had.iter().filter(|m| is_live(m)) {
            let mut e = event(m);
            e.quotes = quotes(m);
            index.insert(e.id.clone(), events.len());
            events.push(e);
        }
        for extra in [hdc, hil] {
            match extra {
                Ok((ms, _)) => {
                    for m in &ms {
                        if let Some(&i) = index.get(&text(&m["id"])) {
                            events[i].quotes.extend(quotes(m));
                        }
                    }
                }
                Err(e) => tracing::debug!("hkjc handicap/total: {e:#}"),
            }
        }
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quarter_lines_average_their_two_halves() {
        assert_eq!(line("-0.5/-1.0"), Some(-0.75));
        assert_eq!(line("0.0/+0.5"), Some(0.25));
        assert_eq!(line("+1.0"), Some(1.0));
        assert_eq!(line("2.5/3.0"), Some(2.75));
        assert_eq!(line(""), None);
    }

    #[test]
    fn a_suspended_line_keeps_its_quotes_without_a_price() {
        let m = serde_json::json!({"foPools": [{"oddsType": "HIL", "status": "SELLINGSTARTED", "lines": [
            {"condition": "2.5", "status": "AVAILABLE", "combinations": [{"str": "H", "status": "AVAILABLE", "currentOdds": "1.95"}, {"str": "L", "status": "AVAILABLE", "currentOdds": "1.75"}]},
            {"condition": "3.5", "status": "SUSPENDED", "combinations": [{"str": "H", "status": "AVAILABLE", "currentOdds": "4.75"}, {"str": "L", "status": "AVAILABLE", "currentOdds": "1.13"}]}
        ]}]});
        let q = quotes(&m);
        assert_eq!(q.len(), 4);
        assert_eq!((q[0].outcome, q[0].line, q[0].price), (Outcome::Over, Some(2.5), Some(1.95)));
        assert_eq!((q[3].outcome, q[3].line, q[3].price), (Outcome::Under, Some(3.5), None));
    }
}
