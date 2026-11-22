//! SBOBET: the public "euro" live-betting pages (no login), one page per sport:
//! `https://www.sbobet.com/euro/live-betting/{tennis|basketball|football|...}`.
//!
//! Each page carries its whole live board in `$P.onUpdate('od', [version, 1, [[section, [event, ...]], ...]])`,
//! a JavaScript array literal (single-quoted strings, holes `[1,,2]`, bare number keys). It is read as
//! data (`js_to_json`), never run. The pages are sent with `no-store` and are re-built for every load.
//! * event = [sport id, league id, [event id, home, away, code, ?, 'MM/DD/YYYY HH:MM' (GMT+8), ...],
//!   [live state], [[n, n, [...]], market, market, ...], ?]
//! * market = [market id, [type, 0, type, ?, max stake, line, ?], [prices]] with decimal prices.
//!   Type 11 = money line (home, away), 5 = 1X2 (home, draw, away), 1 = handicap, 3 = totals.
//!   A game whose winner market is missing is suspended there.
//! * The pages carry no league names and no score.
//!
//! RECORD ONLY (`books::RECORD_ONLY`, owner 22 Nov 2026: "try all of these"): its prices are saved to
//! `changes_research`, never to `changes`, and are never sent to the bots, until a test of its timing
//! against Polymarket says it may be used.

use super::*;

const BASE: &str = "https://www.sbobet.com/euro/live-betting/";
/// (page, our sport key): asked for every poll.
const MAIN: &[(&str, &str)] = &[("tennis", "tennis"), ("basketball", "basketball"), ("football", "football"), ("e-sports", "esports")];
/// Asked for one at a time, in turn (fewer live games, fewer requests).
const REST: &[(&str, &str)] = &[("ice-hockey", "hockey"), ("volleyball", "volleyball"), ("american-football", "amfootball"), ("baseball", "baseball")];

pub struct Sbobet {
    pub follows: Vec<String>,
    pub turn: usize,
}

impl Sbobet {
    pub fn new(follows: impl Fn(&str) -> bool) -> Self {
        let all: Vec<String> = MAIN.iter().chain(REST).map(|x| x.1).filter(|s| follows(s)).map(String::from).collect();
        Sbobet { follows: all, turn: 0 }
    }
    fn wanted(&self, sport: &str) -> bool {
        self.follows.iter().any(|s| s == sport)
    }
}

impl Book for Sbobet {
    fn name(&self) -> &'static str {
        "sbobet"
    }
    fn source(&self) -> String {
        "sbobet.com public euro live-betting pages (record only)".into()
    }
    /// Blocked or refused: wait long before asking again, so the address is not banned.
    fn cool_down(&self, msg: &str, streak: u32) -> Option<Duration> {
        let refused = msg.contains("HTTP 403") || msg.contains("HTTP 429") || msg.contains("no odds payload");
        refused.then(|| Duration::from_secs(if streak >= 3 { 1800 } else { 600 }))
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let mut pages: Vec<(&str, &str)> = MAIN.iter().copied().filter(|x| self.wanted(x.1)).collect();
        let rest: Vec<(&str, &str)> = REST.iter().copied().filter(|x| self.wanted(x.1)).collect();
        if !rest.is_empty() {
            pages.push(rest[self.turn % rest.len()]);
            self.turn += 1;
        }
        let res = futures::future::join_all(pages.iter().map(|(p, _)| async move {
            let resp = http.get(format!("{BASE}{p}")).header("Accept", "text/html").send().await?;
            let status = resp.status();
            let body = resp.text().await?;
            if !status.is_success() {
                bail!("HTTP {status}: {}", body.chars().take(120).collect::<String>());
            }
            Ok::<_, anyhow::Error>((body, now_ms()))
        }))
        .await;
        let mut events = Vec::new();
        let mut recv_ms = 0;
        let mut last_err = None;
        for ((_, sport), r) in pages.iter().zip(res) {
            match r.and_then(|(body, t)| parse(&body, sport).map(|e| (e, t))) {
                Ok((evs, t)) => {
                    events.extend(evs);
                    recv_ms = recv_ms.max(t);
                }
                Err(e) => last_err = Some(e),
            }
        }
        if recv_ms == 0 {
            return Err(last_err.unwrap_or_else(|| anyhow::anyhow!("no page")));
        }
        Ok(Snapshot { events, recv_ms })
    }
}

/// The `od` payload's array literal in a page, as written.
fn od_payload(page: &str) -> Option<&str> {
    let key = "$P.onUpdate('od',";
    let start = page.find(key)? + key.len();
    let s = &page[start..];
    let open = s.find('[')?;
    let mut depth = 0i32;
    let mut quote: Option<char> = None;
    let mut esc = false;
    for (i, c) in s[open..].char_indices() {
        if let Some(q) = quote {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == q {
                quote = None;
            }
            continue;
        }
        match c {
            '\'' | '"' => quote = Some(c),
            '[' | '{' => depth += 1,
            ']' | '}' => {
                depth -= 1;
                if depth == 0 {
                    return Some(&s[open..open + i + 1]);
                }
            }
            _ => {}
        }
    }
    None
}

/// A JavaScript array / object literal as JSON: single-quoted strings, holes (`[1,,2]` -> `[1,null,2]`)
/// and bare object keys (`{12:[0,0]}` -> `{"12":[0,0]}`). Data only; nothing is evaluated.
pub fn js_to_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + s.len() / 8);
    let mut stack: Vec<char> = Vec::new();
    let mut prev = ' '; // the last significant character outside strings
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c.is_whitespace() {
            continue;
        }
        if (c == ',' || c == ']') && stack.last() == Some(&'[') && (prev == '[' || prev == ',') {
            out.push_str("null");
        }
        match c {
            '\'' | '"' => {
                out.push('"');
                while let Some(d) = it.next() {
                    match d {
                        '\\' => match it.next() {
                            Some('\'') => out.push('\''),
                            Some(e) => {
                                out.push('\\');
                                out.push(e);
                            }
                            None => {}
                        },
                        '"' if c == '\'' => out.push_str("\\\""),
                        d if d == c => break,
                        d => out.push(d),
                    }
                }
                out.push('"');
                prev = '"';
                continue;
            }
            '[' | '{' => stack.push(c),
            ']' | '}' => {
                stack.pop();
            }
            _ => {}
        }
        if stack.last() == Some(&'{') && (prev == '{' || prev == ',') && (c.is_ascii_alphanumeric() || c == '_' || c == '-') && c != '{' {
            // a bare key: up to the ':'
            let mut key = String::from(c);
            while let Some(&d) = it.peek() {
                if d == ':' || d.is_whitespace() {
                    break;
                }
                key.push(d);
                it.next();
            }
            out.push('"');
            out.push_str(&key);
            out.push('"');
            prev = 'k';
            continue;
        }
        out.push(c);
        prev = c;
    }
    out
}

/// "11/22/2026 12:15" in GMT+8 -> ms.
fn start_ms(s: &str) -> Option<Ms> {
    let (date, hm) = s.trim().split_once(' ')?;
    let mut d = date.split('/');
    let (mo, da, yr) = (d.next()?, d.next()?, d.next()?);
    naive_ms(&format!("{yr}-{mo:0>2}-{da:0>2} {hm}:00"), 8)
}

/// One sport page's live games with their winner prices.
fn parse(page: &str, sport: &str) -> Result<Vec<EventIn>> {
    let raw = od_payload(page).ok_or_else(|| anyhow::anyhow!("no odds payload (blocked or changed page)"))?;
    let v: Value = serde_json::from_str(&js_to_json(raw)).context("odds payload is not readable")?;
    let mut out = Vec::new();
    for sec in v[2].as_array().into_iter().flatten() {
        for e in sec[1].as_array().into_iter().flatten() {
            let info = &e[2];
            let (Some(id), Some(home), Some(away)) = (info[0].as_i64(), info[1].as_str(), info[2].as_str()) else { continue };
            let three = three_way(sport);
            let (want, outs): (i64, &[Outcome]) = if three { (5, &[Outcome::Home, Outcome::Draw, Outcome::Away]) } else { (11, &[Outcome::Home, Outcome::Away]) };
            let prices: Option<Vec<f64>> = e[4]
                .as_array()
                .into_iter()
                .flatten()
                .skip(1)
                .find(|m| m[1][0].as_i64() == Some(want))
                .and_then(|m| m[2].as_array().map(|p| p.iter().filter_map(Value::as_f64).collect::<Vec<f64>>()))
                .filter(|p| p.len() == outs.len());
            let quotes = outs.iter().enumerate().map(|(i, o)| QuoteIn::new(Market::X12, None, *o, prices.as_ref().map(|p| p[i]))).collect();
            out.push(EventIn {
                id: id.to_string(),
                sport: sport.into(),
                league: String::new(),
                home: home.to_string(),
                away: away.to_string(),
                start_ms: info[5].as_str().and_then(start_ms),
                sr_id: None,
                sr_weak: false,
                live: true,
                score: None,
                clock: None,
                quotes,
            });
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const PAGE: &str = r#"<script>function initiateOddsDisplay(){ $M('tb').onDT('202611221345');$P.onUpdate('od',[17406,1,[[1,[
[8,48570,[10936989,'Billy Suarez','Stefan O\'Vujic','8.0427-2611221030-C03799',8,'11/22/2026 12:05','',0,0,1,0,{323993:[0,0],965345:[0,0]}],[1,0,66738345,45,0,0,{},,{1:45,2:45},0],[[18,18,[[1,0,1]]],[793026271,[11,0,11,1,1000,0.0,157450611],[3.5,1.23]],[793026272,[1,0,1,1,1000,1.5,157450611],[1.9,1.9]]],3],
[8,37231,[10936329,'Liam Broady','Yosuke Watanuki','8.0415-2611221200-C75477',8,'11/22/2026 12:15','',0,0,1,0,{}],[1,0,66738345,45,0,0,{},,{1:45},0],[[11,11,[[1,0,1]]]],3]
]]]]);$M('x').y();}</script>"#;

    #[test]
    fn a_page_s_games_and_winner_prices_are_read_as_data() {
        let evs = parse(PAGE, "tennis").unwrap();
        assert_eq!(evs.len(), 2);
        assert_eq!((evs[0].id.as_str(), evs[0].home.as_str(), evs[0].away.as_str()), ("10936989", "Billy Suarez", "Stefan O'Vujic"));
        // 12:05 GMT+8 = 04:05 UTC
        assert_eq!(evs[0].start_ms, Some(1795320300000));
        let p: Vec<Option<f64>> = evs[0].quotes.iter().map(|q| q.price).collect();
        assert_eq!(p, vec![Some(3.5), Some(1.23)]);
        // no money line listed: suspended
        assert!(evs[1].quotes.iter().all(|q| q.price.is_none()) && evs[1].quotes.len() == 2);
    }

    #[test]
    fn football_reads_the_three_way_market() {
        let page = r#"$P.onUpdate('od',[1,1,[[1,[[1,111215,[10736579,'Melbourne Victory (w)','Perth Glory (w)','x',10,'11/22/2026 12:00','',0,0,1,,{}],[1,2,46],[[3,3,[[1,0,1]]],[1,[1,0,1,1,3000,0.0,1],[1.78,2.02]],[2,[5,0,5,1,500,0.0,1],[8.488,1.105,9.256]]],4]]]]]);"#;
        let evs = parse(page, "football").unwrap();
        let p: Vec<(Outcome, Option<f64>)> = evs[0].quotes.iter().map(|q| (q.outcome, q.price)).collect();
        assert_eq!(p, vec![(Outcome::Home, Some(8.488)), (Outcome::Draw, Some(1.105)), (Outcome::Away, Some(9.256))]);
    }

    #[test]
    fn js_literals_become_json() {
        assert_eq!(js_to_json("[1,,'a\\'b',{12:[0,0],x:'\"'},[,]]"), r#"[1,null,"a'b",{"12":[0,0],"x":"\""},[null,null]]"#);
    }

    #[test]
    fn a_page_without_the_payload_is_an_error_so_the_feed_backs_off() {
        let e = parse("<html>captcha</html>", "tennis").unwrap_err().to_string();
        assert!(e.contains("no odds payload"));
        let s = Sbobet::new(|_| true);
        assert_eq!(s.cool_down(&e, 1), Some(Duration::from_secs(600)));
        assert_eq!(s.cool_down("timed out", 1), None);
    }
}
