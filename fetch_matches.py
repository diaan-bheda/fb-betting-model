"""
Fetches upcoming soccer fixtures + 1X2 odds from The Odds API (free tier)
and writes them to data/matches.json in a format the Matchday Slip Builder
web app can read directly.

Run manually:  ODDS_API_KEY=xxxx python fetch_matches.py
Run weekly via the included GitHub Actions workflow.
"""

import json
import os
from datetime import datetime, timezone

import requests

API_KEY = os.environ["ODDS_API_KEY"]

# Edit this list to the competitions you actually bet on.
# Full list of valid keys: https://the-odds-api.com/sports-odds-data/soccer-odds.html
LEAGUES = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
]

BASE_URL = "https://api.the-odds-api.com/v4/sports/{league}/odds"


def fetch_league(league):
    url = BASE_URL.format(league=league)
    params = {
        "apiKey": API_KEY,
        "regions": "uk,eu",
        "markets": "h2h",       # 1X2 market
        "oddsFormat": "decimal",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def average_odds(event):
    """Average the 1X2 price across every bookmaker the API returned for this event,
    and separately pull out Pinnacle's line if it's in the response — Pinnacle is
    the market's usual sharp reference point, so it's a useful second number to
    compare against the broad average (and against whatever a specific book like
    Stake is offering)."""
    home, away = event["home_team"], event["away_team"]
    buckets = {"H": [], "D": [], "A": []}
    pinnacle = {"H": None, "D": None, "A": None}

    for bookmaker in event.get("bookmakers", []):
        is_pinnacle = bookmaker.get("key") == "pinnacle"
        for market in bookmaker.get("markets", []):
            if market["key"] != "h2h":
                continue
            for outcome in market["outcomes"]:
                name, price = outcome["name"], outcome["price"]
                key = "H" if name == home else "A" if name == away else "D" if name.lower() == "draw" else None
                if key is None:
                    continue
                buckets[key].append(price)
                if is_pinnacle:
                    pinnacle[key] = price

    def avg(values):
        return round(sum(values) / len(values), 2) if values else None

    avg_odds = {"H": avg(buckets["H"]), "D": avg(buckets["D"]), "A": avg(buckets["A"])}
    has_pinnacle = all(pinnacle.values())
    return avg_odds, (pinnacle if has_pinnacle else None)


def main():
    all_matches = []

    for league in LEAGUES:
        try:
            events = fetch_league(league)
        except requests.RequestException as e:
            print(f"[warn] failed to fetch {league}: {e}")
            continue

        for event in events:
            odds, odds_pinnacle = average_odds(event)
            if not all(odds.values()):
                continue  # skip events without full 1X2 coverage yet

            match = {
                "id": event["id"],
                "league": league,
                "home": event["home_team"],
                "away": event["away_team"],
                "commence_time": event["commence_time"],
                "odds": odds,
            }
            if odds_pinnacle:
                match["odds_pinnacle"] = odds_pinnacle
            all_matches.append(match)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "matches": all_matches,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/matches.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(all_matches)} matches across {len(LEAGUES)} leagues.")


if __name__ == "__main__":
    main()
