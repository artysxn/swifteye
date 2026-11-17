//! Mostbet: the JSON its own SPA loads, `GET /api/v3/user/line/list?t=2&l=100&of=N` (t=2 live,
//! t=1 pre-match; `l` at most 100, page with `of`). Mostbet serves from rotating mirror domains
//! (mostbet.com sends the browser to one, e.g. 0djawb3.com); set the current one with `param`.
//!
//! * `lines_hierarchy[].line_category_dto_collection[]` (sport: `code` soccer / tennis / ice-hockey ...)
//!   → `line_supercategory_dto_collection[]` (country) → `line_subcategory_dto_collection[]`
//!   (tournament) → `line_dto_collection[]` (one match: `match.team1/team2.title`, `begin_at` s).
//! * `outcomes[]`: `alias` "1" / "x" / "2" = home / draw / away (the main result market), price in
//!   `odd` (string), `active` false = suspended.

use super::*;

pub struct Mostbet {
    pub host: String,
    pub pre: Pre,
}

impl Mostbet {
    async fn list(&self, http: &reqwest::Client, t: u8, pages: usize) -> Result<(Vec<EventIn>, Ms)> {
        let mut out = Vec::new();
        let mut recv = 0;
        for page in 0..pages {
            let url = format!("https://{}/api/v3/user/line/list?t={t}&l=100&of={}", self.host, page * 100);
            let (v, r) = fetch_json(http.get(url).header("Referer", format!("https://{}/live", self.host))).await?;
            recv = recv.max(r);
            let n = parse(&v, t == 2, &mut out);
            if n < 100 {
                break;
            }
        }
        Ok((out, if recv > 0 { recv } else { now_ms() }))
    }
}

/// Appends events; returns how many lines the page had.
fn parse(v: &Value, live: bool, out: &mut Vec<EventIn>) -> usize {
    let mut n = 0;
    for lh in v["lines_hierarchy"].as_array().into_iter().flatten() {
        for cat in lh["line_category_dto_collection"].as_array().into_iter().flatten() {
            let code = text(&cat["code"]).replace('-', " ");
            for sc in cat["line_supercategory_dto_collection"].as_array().into_iter().flatten() {
                for sub in sc["line_subcategory_dto_collection"].as_array().into_iter().flatten() {
                    let league = join_league(&[&text(&sc["title"]), &text(&sub["title"])]);
                    for ln in sub["line_dto_collection"].as_array().into_iter().flatten() {
                        n += 1;
                        if ln["is_outright"].as_bool() == Some(true) {
                            continue;
                        }
                        let sport: &str = match sport_key(&code).or_else(|| esport_key(&code)).or_else(|| esport_key(&league)) {
                            Some(s) => s,
                            None => continue,
                        };
                        let m = &ln["match"];
                        let (home, away) = (text(&m["team1"]["title"]), text(&m["team2"]["title"]));
                        if home.is_empty() || away.is_empty() {
                            continue;
                        }
                        let mut quotes = Vec::new();
                        for o in ln["outcomes"].as_array().into_iter().flatten() {
                            if o["group_alias"].as_str() != Some("x") {
                                continue;
                            }
                            let outcome = match o["alias"].as_str() {
                                Some("1") => Outcome::Home,
                                Some("x") => Outcome::Draw,
                                Some("2") => Outcome::Away,
                                _ => continue,
                            };
                            let open = o["active"].as_bool() == Some(true) && o["closed"].as_bool() != Some(true);
                            quotes.push(QuoteIn::new(Market::X12, None, outcome, if open { num(&o["odd"]) } else { None }));
                        }
                        out.push(EventIn {
                            id: id_text(&ln["id"]),
                            sport: sport.into(),
                            league: league.clone(),
                            home,
                            away,
                            start_ms: int(&m["begin_at"]).map(|s| s * 1000),
                            sr_id: None,
                            sr_weak: false,
                            live,
                            score: if live { m["score"].as_str().map(|s| s.replace(':', "-")) } else { None },
                            clock: if live { int(&m["match_time"]).map(|x| format!("{x}'")) } else { None },
                            quotes: winner_quotes(sport, quotes),
                        });
                    }
                }
            }
        }
    }
    n
}

impl Book for Mostbet {
    fn name(&self) -> &'static str {
        "mostbet"
    }
    fn source(&self) -> String {
        format!("{} /api/v3/user/line/list (live, 100 per page) + first 500 pre-match", self.host)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        if self.pre.due() {
            match self.list(http, 1, 5).await {
                Ok((evs, _)) => self.pre.store(evs),
                Err(e) => {
                    tracing::warn!("mostbet pre-match: {e:#}");
                    self.pre.failed();
                }
            }
        }
        let (mut events, recv_ms) = self.list(http, 2, 6).await?;
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}
