//! Cross-book event matching.
//!
//! 1. Exact: books that expose a Sportradar match id link on it directly.
//! 2. Fuzzy: same sport, kick-off within a window, both team names similar.
//!    A match never takes two events from the same book.

use crate::model::{EventIn, Ms};
use serde::Serialize;
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug, Serialize)]
pub struct MatchInfo {
    pub id: i64,
    pub sport: String,
    pub home: String,
    pub away: String,
    pub league: String,
    pub start_ms: Option<Ms>,
    pub sr_id: Option<String>,
    pub created_ms: Ms,
    /// Books with an event currently linked to this match.
    pub books: HashSet<&'static str>,
    #[serde(skip)]
    pub last_seen_ms: Ms,
    #[serde(skip)]
    home_tok: Names,
    #[serde(skip)]
    away_tok: Names,
}

#[derive(Default)]
pub struct Matcher {
    pub matches: HashMap<i64, MatchInfo>,
    by_sr: HashMap<String, i64>,
    next_id: i64,
}

/// Kick-off times differ between books (scheduled vs actual, rounding), so allow some slack.
const WINDOW_MS: i64 = 45 * 60 * 1000;
/// Very confident name matches may sit further apart (books that report the
/// actual kick-off after a delay, or a wrong timezone on one side).
const WIDE_WINDOW_MS: i64 = 4 * 60 * 60 * 1000;

impl Matcher {
    pub fn set_next_id(&mut self, id: i64) {
        self.next_id = self.next_id.max(id);
    }

    pub fn alloc_id(&mut self) -> i64 {
        self.next_id += 1;
        self.next_id
    }

    pub fn restore(&mut self, m: MatchInfo) {
        if let Some(sr) = &m.sr_id {
            self.by_sr.insert(sr.clone(), m.id);
        }
        self.next_id = self.next_id.max(m.id);
        self.matches.insert(m.id, m);
    }

    pub fn new_match_info(id: i64, ev: &EventIn, now: Ms) -> MatchInfo {
        MatchInfo {
            id,
            sport: ev.sport.clone(),
            home: ev.home.clone(),
            away: ev.away.clone(),
            league: ev.league.clone(),
            start_ms: ev.start_ms,
            sr_id: ev.sr_id.clone(),
            created_ms: now,
            books: HashSet::new(),
            last_seen_ms: now,
            home_tok: Names::new(&ev.home),
            away_tok: Names::new(&ev.away),
        }
    }

    /// Returns (match id, created a new match?).
    pub fn assign(&mut self, book: &'static str, ev: &EventIn, now: Ms) -> (i64, bool) {
        if let Some(sr) = &ev.sr_id {
            if let Some(&id) = self.by_sr.get(sr) {
                if let Some(m) = self.matches.get_mut(&id) {
                    if !m.books.contains(book) {
                        m.books.insert(book);
                        m.last_seen_ms = now;
                        return (id, false);
                    }
                }
            }
        }

        let h = Names::new(&ev.home);
        let a = Names::new(&ev.away);
        let mut best: Option<(f64, i64)> = None;
        for m in self.matches.values() {
            if m.sport != ev.sport || m.books.contains(book) {
                continue;
            }
            // Two different Sportradar ids are two different matches (unless ours is only a guess).
            if let (Some(x), Some(y)) = (&m.sr_id, &ev.sr_id) {
                if x != y && !ev.sr_weak {
                    continue;
                }
            }
            if now - m.last_seen_ms > 6 * 3600 * 1000 {
                continue;
            }
            let dt = match (m.start_ms, ev.start_ms) {
                (Some(x), Some(y)) => (x - y).abs(),
                _ => 0,
            };
            if dt > WIDE_WINDOW_MS {
                continue;
            }
            let sh = m.home_tok.sim(&h);
            let sa = m.away_tok.sim(&a);
            let lo = sh.min(sa);
            let avg = (sh + sa) / 2.0;
            let ok = if dt <= WINDOW_MS {
                lo >= 0.5 && avg >= 0.7
            } else {
                lo >= 0.85
            };
            if ok && best.map_or(true, |(s, _)| avg > s) {
                best = Some((avg, m.id));
            }
        }
        if let Some((_, id)) = best {
            let m = self.matches.get_mut(&id).unwrap();
            m.books.insert(book);
            m.last_seen_ms = now;
            if m.sr_id.is_none() && !ev.sr_weak {
                if let Some(sr) = &ev.sr_id {
                    m.sr_id = Some(sr.clone());
                    self.by_sr.insert(sr.clone(), id);
                }
            }
            return (id, false);
        }

        self.next_id += 1;
        let id = self.next_id;
        let mut m = Self::new_match_info(id, ev, now);
        m.books.insert(book);
        if let Some(sr) = &ev.sr_id {
            // A guessed id still lets a later exact id find this match, but is not
            // stored as the match's own id (so it cannot veto a name match).
            self.by_sr.insert(sr.clone(), id);
            if ev.sr_weak {
                m.sr_id = None;
            }
        }
        self.matches.insert(id, m);
        (id, true)
    }

    pub fn touch(&mut self, id: i64, now: Ms) {
        if let Some(m) = self.matches.get_mut(&id) {
            m.last_seen_ms = now;
        }
    }

    pub fn attach(&mut self, id: i64, book: &'static str, now: Ms) {
        if let Some(m) = self.matches.get_mut(&id) {
            m.books.insert(book);
            m.last_seen_ms = now;
        }
    }

    pub fn detach(&mut self, id: i64, book: &'static str) {
        if let Some(m) = self.matches.get_mut(&id) {
            m.books.remove(book);
        }
    }

    /// Forget matches nobody has looked at for a day (keeps memory flat; the DB keeps history).
    pub fn prune(&mut self, now: Ms) {
        let stale: Vec<i64> = self
            .matches
            .values()
            .filter(|m| m.books.is_empty() && now - m.last_seen_ms > 24 * 3600 * 1000)
            .map(|m| m.id)
            .collect();
        for id in stale {
            if let Some(m) = self.matches.remove(&id) {
                if let Some(sr) = m.sr_id {
                    self.by_sr.remove(&sr);
                }
            }
        }
    }
}

/// A team name broken into comparable tokens plus "tags" that must agree
/// exactly (women, U21, reserves) so a youth side never matches the first team.
#[derive(Clone, Debug, Default)]
pub struct Names {
    tokens: Vec<String>,
    tags: Vec<&'static str>,
}

const STOP: &[&str] = &[
    "fc", "cf", "sc", "ac", "afc", "cd", "ud", "sd", "club", "de", "the", "fk", "sk", "if", "bk", "ff", "ik",
    "cs", "as", "sv", "ca", "cr", "ec", "se", "sa", "fbc", "jk", "nk", "hk", "ks", "kv", "rc", "rcd", "us",
    "calcio", "football", "futbol", "clube", "team",
];

impl Names {
    pub fn new(raw: &str) -> Names {
        let s = deunicode::deunicode(raw).to_lowercase();
        let s: String = s
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { ' ' })
            .collect();
        let mut tokens = Vec::new();
        let mut tags = Vec::new();
        for t in s.split_whitespace() {
            let tag = match t {
                "w" | "women" | "womens" | "ladies" | "fem" | "feminino" | "femenino" | "frauen" | "dames" | "zeny" => Some("women"),
                "res" | "reserves" | "reserve" | "ii" | "b" => Some("reserves"),
                "u17" | "u18" | "u19" | "u20" | "u21" | "u22" | "u23" => Some(match t {
                    "u17" => "u17",
                    "u18" => "u18",
                    "u19" => "u19",
                    "u20" => "u20",
                    "u21" => "u21",
                    "u22" => "u22",
                    _ => "u23",
                }),
                _ => None,
            };
            if let Some(tag) = tag {
                if !tags.contains(&tag) {
                    tags.push(tag);
                }
                continue;
            }
            if STOP.contains(&t) {
                continue;
            }
            let t = match t {
                "utd" => "united",
                "st" => "saint",
                "intl" => "international",
                "dep" => "deportivo",
                "atl" => "atletico",
                other => other,
            };
            tokens.push(t.to_string());
        }
        if tokens.is_empty() {
            // A name made only of stop words ("AC") — keep it rather than match anything.
            tokens.push(s.split_whitespace().collect::<Vec<_>>().join(""));
        }
        tags.sort();
        Names { tokens, tags }
    }

    pub fn sim(&self, other: &Names) -> f64 {
        if self.tags != other.tags {
            return 0.0;
        }
        let joined_a: String = self.tokens.concat();
        let joined_b: String = other.tokens.concat();
        if joined_a == joined_b {
            return 1.0;
        }
        let (short, long) = if self.tokens.len() <= other.tokens.len() {
            (&self.tokens, &other.tokens)
        } else {
            (&other.tokens, &self.tokens)
        };
        let mut total = 0.0;
        for t in short {
            let mut best: f64 = 0.0;
            for u in long {
                best = best.max(token_sim(t, u));
            }
            total += best;
        }
        let by_short = total / short.len() as f64;
        let by_long = total / long.len() as f64;
        let tok = by_short * 0.6 + by_long * 0.4;
        tok.max(trigram(&joined_a, &joined_b))
    }
}

fn token_sim(a: &str, b: &str) -> f64 {
    if a == b {
        return 1.0;
    }
    let (s, l) = if a.len() <= b.len() { (a, b) } else { (b, a) };
    if s.len() >= 3 && l.starts_with(s) {
        return 0.9;
    }
    let t = trigram(a, b);
    if t >= 0.5 {
        t
    } else {
        0.0
    }
}

fn trigram(a: &str, b: &str) -> f64 {
    fn grams(s: &str) -> HashSet<[u8; 3]> {
        let p = format!("  {s} ");
        let v = p.as_bytes();
        (0..v.len().saturating_sub(2)).map(|i| [v[i], v[i + 1], v[i + 2]]).collect()
    }
    let ga = grams(a);
    let gb = grams(b);
    if ga.is_empty() || gb.is_empty() {
        return 0.0;
    }
    let inter = ga.intersection(&gb).count() as f64;
    inter / (ga.len() + gb.len()) as f64 * 2.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(a: &str, b: &str) -> f64 {
        Names::new(a).sim(&Names::new(b))
    }

    #[test]
    fn names() {
        assert!(s("Manchester United", "Man Utd") >= 0.85, "{}", s("Manchester United", "Man Utd"));
        assert!(s("Sacramento Republic", "Sacramento Republic FC") >= 0.99);
        assert!(s("Bayern München", "Bayern Munchen") >= 0.99);
        assert!(s("Linfield Swifts (res)", "Linfield") < 0.01);
        assert!(s("Arsenal W", "Arsenal") < 0.01);
        assert!(s("Arsenal (W)", "Arsenal Women") >= 0.99);
        assert!(s("Real Madrid", "Real Betis") < 0.7);
        assert!(s("Irapuato", "Celaya") < 0.3);
        assert!(s("New Mexico United", "New Mexico Utd") >= 0.99);
    }
}
