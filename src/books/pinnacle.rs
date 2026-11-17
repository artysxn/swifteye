//! Pinnacle: the public "guest" Arcadia API the pinnacle.com site loads (no login).
//! Per sport: `/sports/{id}/matchups[/live]` (games) + `/sports/{id}/markets[/live]/straight` (prices).
//!
//! * Only `type = matchup` with units "Regular" is the game itself; "special" and child
//!   matchups (maps, kills, corners) are side markets and are skipped.
//! * A live matchup can carry `parentId` = the pre-match matchup; that id is the event id so a
//!   game keeps one id from pre-match into play.
//! * Prices are American odds. Moneyline period 0 = full game (two-way incl. overtime, or
//!   home/draw/away for football). Football also gets the main total and spread.
//! * Pinnacle is the sharpest book, so it is the reference the others are timed against.

use super::*;
use std::collections::HashMap;

const API: &str = "https://guest.api.arcadia.pinnacle.com/0.1";
/// The key pinnacle.com itself sends for guests.
const KEY: &str = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R";

/// (Pinnacle sport id, our sport key). "esports" is refined per league from its title.
pub const SPORT_IDS: &[(i64, &str)] = &[
    (29, "football"),
    (4, "basketball"),
    (33, "tennis"),
    (19, "hockey"),
    (3, "baseball"),
    (34, "volleyball"),
    (15, "amfootball"),
    (22, "mma"),
    (32, "tabletennis"),
    (18, "handball"),
    (12, "esports"),
];

pub struct Pinnacle {
    /// Sports to ask for (only the followed ones, to keep the request count down).
    pub sports: Vec<(i64, &'static str)>,
    pub pre: Pre,
    /// A sport with nothing in play is re-checked every 30 s instead of every poll.
    idle_until: HashMap<i64, Ms>,
}

impl Pinnacle {
    pub fn new(follows: impl Fn(&str) -> bool, pre: Pre) -> Self {
        let sports = SPORT_IDS.iter().copied().filter(|(_, s)| follows(s)).collect();
        Pinnacle { sports, pre, idle_until: HashMap::new() }
    }
}

async fn get(http: &reqwest::Client, path: String) -> Result<(Value, Ms)> {
    fetch_json(http.get(format!("{API}{path}")).header("x-api-key", KEY).header("Referer", "https://www.pinnacle.com/")).await
}

/// Matchups + their markets for one sport.
async fn sport_list(http: &reqwest::Client, id: i64, sport: &'static str, live: bool) -> Result<(Vec<EventIn>, Ms)> {
    let (mu, mk) = if live {
        (format!("/sports/{id}/matchups/live"), format!("/sports/{id}/markets/live/straight"))
    } else {
        (format!("/sports/{id}/matchups"), format!("/sports/{id}/markets/straight?primaryOnly=true"))
    };
    let (a, b) = futures::join!(get(http, mu), get(http, mk));
    let ((mu, t1), (mk, t2)) = (a?, b?);
    Ok((parse(&mu, &mk, sport, live), t1.max(t2)))
}

impl Book for Pinnacle {
    fn name(&self) -> &'static str {
        "pinnacle"
    }
    fn source(&self) -> String {
        let s: Vec<&str> = self.sports.iter().map(|x| x.1).collect();
        format!("Pinnacle guest Arcadia API (matchups + straight markets): {}", s.join(", "))
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let sports = self.sports.clone();
        refresh_pre(&mut self.pre, "pinnacle", || async {
            let res = futures::future::join_all(sports.iter().map(|(id, s)| sport_list(http, *id, s, false))).await;
            let mut out = Vec::new();
            let mut ok = false;
            for r in res {
                match r {
                    Ok((evs, _)) => {
                        ok = true;
                        out.extend(evs.into_iter().filter(|e| !e.live));
                    }
                    Err(e) => tracing::warn!("pinnacle pre-match: {e:#}"),
                }
            }
            if !ok {
                bail!("every sport failed");
            }
            Ok(out)
        })
        .await;

        let now = now_ms();
        let due: Vec<(i64, &'static str)> = sports.iter().copied().filter(|(id, _)| self.idle_until.get(id).map_or(true, |t| now >= *t)).collect();
        let res = futures::future::join_all(due.iter().map(|(id, s)| sport_list(http, *id, s, true))).await;
        let mut events = Vec::new();
        let mut recv_ms = 0;
        let mut errors = Vec::new();
        for ((id, _), r) in due.iter().zip(res) {
            match r {
                Ok((evs, t)) => {
                    recv_ms = recv_ms.max(t);
                    if evs.is_empty() {
                        self.idle_until.insert(*id, now + 30_000);
                    } else {
                        self.idle_until.remove(id);
                    }
                    events.extend(evs);
                }
                Err(e) => errors.push(format!("{id}: {e:#}")),
            }
        }
        if !due.is_empty() && errors.len() == due.len() {
            bail!("{}", errors.join("; "));
        }
        for e in &errors {
            tracing::warn!("pinnacle live {e}");
        }
        // Sports skipped this poll (idle) keep their pre-match games through the merge below;
        // a sport that errored would otherwise look closed for one poll, which the engine
        // tolerates (an event must be missing 3 polls and 15 s before it closes).
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms: if recv_ms > 0 { recv_ms } else { now_ms() } })
    }
}

/// American odds to decimal.
fn decimal(american: f64) -> Option<f64> {
    if american >= 100.0 {
        Some(1.0 + american / 100.0)
    } else if american <= -100.0 {
        Some(1.0 + 100.0 / -american)
    } else {
        None
    }
}

fn parse(mu: &Value, mk: &Value, sport: &'static str, live: bool) -> Vec<EventIn> {
    let mut markets: HashMap<i64, Vec<&Value>> = HashMap::new();
    for m in mk.as_array().into_iter().flatten() {
        if m["period"].as_i64() != Some(0) || m["isAlternate"].as_bool() == Some(true) {
            continue;
        }
        if let Some(id) = m["matchupId"].as_i64() {
            markets.entry(id).or_default().push(m);
        }
    }
    let mut events = Vec::new();
    for m in mu.as_array().into_iter().flatten() {
        if m["type"].as_str() != Some("matchup") || m["units"].as_str().is_some_and(|u| u != "Regular") {
            continue;
        }
        let Some(mid) = m["id"].as_i64() else { continue };
        let league = text(&m["league"]["name"]);
        let sport: &str = if sport == "esports" { esport_key(&league).unwrap_or("esports") } else { sport };
        let parts: Vec<&Value> = m["participants"].as_array().into_iter().flatten().collect();
        if parts.len() != 2 {
            continue;
        }
        let side = |al: &str, ord: usize| parts.iter().find(|p| p["alignment"].as_str() == Some(al)).copied().or(parts.get(ord).copied());
        let (Some(h), Some(a)) = (side("home", 0), side("away", 1)) else { continue };
        let pid: HashMap<i64, Outcome> = [(h, Outcome::Home), (a, Outcome::Away)].into_iter().filter_map(|(p, o)| Some((p["id"].as_i64()?, o))).collect();
        let st = &m["state"];
        let mut ev = EventIn {
            id: m["parentId"].as_i64().unwrap_or(mid).to_string(),
            sport: sport.into(),
            league,
            home: text(&h["name"]),
            away: text(&a["name"]),
            start_ms: m["startTime"].as_str().and_then(rfc3339_ms),
            sr_id: None,
            sr_weak: false,
            live: live || m["isLive"].as_bool() == Some(true),
            score: None,
            clock: if live { st["minutes"].as_i64().map(|x| format!("{x}'")) } else { None },
            quotes: Vec::new(),
        };
        for mkt in markets.get(&mid).into_iter().flatten() {
            let typ = mkt["type"].as_str().unwrap_or("");
            let market = match typ {
                "moneyline" => Market::X12,
                "total" if sport == "football" => Market::Total,
                "spread" if sport == "football" => Market::Handicap,
                _ => continue,
            };
            let open = mkt["status"].as_str().map_or(true, |s| s == "open");
            let mut home_points = None;
            let mut qs = Vec::new();
            for p in mkt["prices"].as_array().into_iter().flatten() {
                let outcome = match p["designation"].as_str() {
                    Some("home") => Outcome::Home,
                    Some("away") => Outcome::Away,
                    Some("draw") => Outcome::Draw,
                    Some("over") => Outcome::Over,
                    Some("under") => Outcome::Under,
                    _ => match p["participantId"].as_i64().and_then(|i| pid.get(&i)) {
                        Some(o) => *o,
                        None => continue,
                    },
                };
                let points = num(&p["points"]);
                if market == Market::Handicap && outcome == Outcome::Home {
                    home_points = points;
                }
                let price = if open { num(&p["price"]).and_then(decimal) } else { None };
                qs.push((outcome, points, price));
            }
            for (outcome, points, price) in qs {
                let line = match market {
                    Market::Total => points,
                    Market::Handicap => home_points.or(points.map(|x| -x)),
                    _ => None,
                };
                if market != Market::X12 && line.is_none() {
                    continue;
                }
                ev.quotes.push(QuoteIn::new(market, line, outcome, price));
            }
        }
        ev.quotes = winner_quotes(sport, ev.quotes);
        events.push(ev);
    }
    events
}
