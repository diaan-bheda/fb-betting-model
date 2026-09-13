"""
Reads data/matches.json (written by fetch_matches.py), pulls each team's
recent match history from football-data.org, fits a simple Poisson
expected-goals model per league, and writes a `model_prob` (H/D/A, in %)
onto every upcoming fixture it can find enough history for.

This is a standard, well-known approach (attack/defense strength ratios
feeding a Poisson goal model) — not a guarantee of accuracy, just an
independent statistical estimate to compare against the bookmaker's odds.

Run manually:  FOOTBALL_DATA_API_KEY=xxxx python build_model.py
"""

import json
import math
import os
import time
from difflib import SequenceMatcher

import requests

API_KEY = os.environ["FOOTBALL_DATA_API_KEY"]
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

# Maps the-odds-api league keys (used in fetch_matches.py) to football-data.org
# competition codes. Add/remove to match the LEAGUES list in fetch_matches.py.
LEAGUE_MAP = {
    "soccer_epl": "PL",
    "soccer_spain_la_liga": "PD",
    "soccer_germany_bundesliga": "BL1",
    "soccer_italy_serie_a": "SA",
    "soccer_france_ligue_one": "FL1",
    "soccer_uefa_champs_league": "CL",
}

MAX_GOALS = 6           # grid size for the Poisson calculation
MIN_MATCHES = 3         # minimum home/away matches needed before we trust a team's numbers
LOOKBACK_DAYS = 200      # how far back to pull finished matches from


def normalize(name):
    junk = [" fc", " cf", " afc", " ac", " sc", " cfc", " calcio", " club"]
    n = name.lower()
    for j in junk:
        n = n.replace(j, "")
    return n.strip()


def fuzzy_match(name, candidates):
    """Best-effort match between an odds-api team name and a football-data team name."""
    norm_name = normalize(name)
    best, best_score = None, 0.0
    for c in candidates:
        score = SequenceMatcher(None, norm_name, normalize(c)).ratio()
        if score > best_score:
            best, best_score = c, score
    return best if best_score >= 0.6 else None


def fetch_competition_matches(code):
    url = f"{BASE_URL}/competitions/{code}/matches"
    params = {"status": "FINISHED"}
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    if r.status_code != 200:
        print(f"[warn] {code}: HTTP {r.status_code} — {r.text[:150]}")
        return []
    return r.json().get("matches", [])


def build_team_stats(matches):
    """Returns per-team home/away goals-for and goals-against averages, plus league averages."""
    home_goals_sum, away_goals_sum, n_matches = 0, 0, 0
    home_scored, home_conceded = {}, {}
    away_scored, away_conceded = {}, {}

    for m in matches:
        score = m.get("score", {}).get("fullTime", {})
        hg, ag = score.get("home"), score.get("away")
        if hg is None or ag is None:
            continue
        home_team = m["homeTeam"]["name"]
        away_team = m["awayTeam"]["name"]

        home_scored.setdefault(home_team, []).append(hg)
        home_conceded.setdefault(home_team, []).append(ag)
        away_scored.setdefault(away_team, []).append(ag)
        away_conceded.setdefault(away_team, []).append(hg)

        home_goals_sum += hg
        away_goals_sum += ag
        n_matches += 1

    if n_matches == 0:
        return None

    league_home_avg = home_goals_sum / n_matches
    league_away_avg = away_goals_sum / n_matches

    def avg(lst):
        return sum(lst) / len(lst) if lst else None

    teams = set(list(home_scored.keys()) + list(away_scored.keys()))
    stats = {}
    for t in teams:
        hs, hc = home_scored.get(t, []), home_conceded.get(t, [])
        aws, awc = away_scored.get(t, []), away_conceded.get(t, [])
        if len(hs) < MIN_MATCHES or len(aws) < MIN_MATCHES:
            continue
        stats[t] = {
            "home_attack": avg(hs) / league_home_avg,
            "home_defense": avg(hc) / league_away_avg,
            "away_attack": avg(aws) / league_away_avg,
            "away_defense": avg(awc) / league_home_avg,
        }

    return {
        "teams": stats,
        "league_home_avg": league_home_avg,
        "league_away_avg": league_away_avg,
        "team_names": list(teams),
    }


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def match_probs(home_xg, away_xg, max_goals=MAX_GOALS):
    probs = {"H": 0.0, "D": 0.0, "A": 0.0}
    for hg in range(max_goals + 1):
        for ag in range(max_goals + 1):
            p = poisson_pmf(hg, home_xg) * poisson_pmf(ag, away_xg)
            if hg > ag:
                probs["H"] += p
            elif hg == ag:
                probs["D"] += p
            else:
                probs["A"] += p
    total = sum(probs.values())
    return {k: round(v / total * 100, 1) for k, v in probs.items()}


def main():
    with open("data/matches.json") as f:
        data = json.load(f)

    fixtures = data.get("matches", [])
    leagues_needed = {f["league"] for f in fixtures if f.get("league") in LEAGUE_MAP}

    league_models = {}
    for league_key in leagues_needed:
        code = LEAGUE_MAP[league_key]
        print(f"Building model for {league_key} ({code})...")
        raw_matches = fetch_competition_matches(code)
        model = build_team_stats(raw_matches)
        league_models[league_key] = model
        time.sleep(6)  # stay comfortably under football-data.org's free-tier rate limit

    updated = 0
    for fx in fixtures:
        model = league_models.get(fx.get("league"))
        if not model:
            continue
        home_match = fuzzy_match(fx["home"], model["team_names"])
        away_match = fuzzy_match(fx["away"], model["team_names"])
        if not home_match or not away_match:
            continue
        home_stats = model["teams"].get(home_match)
        away_stats = model["teams"].get(away_match)
        if not home_stats or not away_stats:
            continue

        home_xg = home_stats["home_attack"] * away_stats["away_defense"] * model["league_home_avg"]
        away_xg = away_stats["away_attack"] * home_stats["home_defense"] * model["league_away_avg"]
        fx["model_prob"] = match_probs(home_xg, away_xg)
        fx["model_xg"] = {"home": round(home_xg, 2), "away": round(away_xg, 2)}
        updated += 1

    data["model_updated_at"] = data.get("updated_at")
    with open("data/matches.json", "w") as f:
        json.dump(data, f, indent=2)

    print(f"Added model probabilities to {updated} of {len(fixtures)} fixtures.")


if __name__ == "__main__":
    main()
