import requests
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY = "icehockey_nhl"

# NHL prop markets available in The Odds API
NHL_PROP_MARKETS = [
    "player_goal_scorer",
    "player_anytime_goalscorer",
    "player_points",
    "player_shots_on_goal",
    "player_power_play_points"
]

NHL_STAT_LABELS = {
    "player_goal_scorer": "Goal Scorer (First)",
    "player_anytime_goalscorer": "Anytime Goal Scorer",
    "player_points": "Over/Under Points",
    "player_shots_on_goal": "Shots on Goal",
    "player_power_play_points": "Power Play Points"
}

def get_nhl_stat_label(market_key):
    return NHL_STAT_LABELS.get(market_key, market_key.replace("_", " ").title())

# Simple in-process cache
_nhl_cache = {}

def _cache_get(key, ttl_seconds=3600):
    entry = _nhl_cache.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_seconds:
        return entry["data"]
    return None

def _cache_set(key, data):
    _nhl_cache[key] = {"data": data, "ts": time.time()}


def fetch_nhl_events():
    """
    Step 1: Fetch list of upcoming NHL events to get event IDs.
    Returns list of event dicts with id, home_team, away_team,
    commence_time.
    """
    if not ODDS_API_KEY:
        logger.error("[NHL] ODDS_API_KEY not set")
        return []

    cached = _cache_get("nhl_events", ttl_seconds=300)
    if cached:
        logger.info(f"[NHL] Returning {len(cached)} cached events")
        return cached

    try:
        response = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/events",
            params={
                "apiKey": ODDS_API_KEY,
                "dateFormat": "iso"
            },
            timeout=15
        )
        response.raise_for_status()
        events = response.json()

        remaining = response.headers.get("x-requests-remaining")
        logger.info(f"[NHL] Fetched {len(events)} events. "
                    f"API calls remaining: {remaining}")

        _cache_set("nhl_events", events)
        return events

    except Exception as e:
        logger.error(f"[NHL] Failed to fetch events: {e}")
        return []


def fetch_props_for_event(event):
    """
    Step 2: Fetch player props for a single NHL event.
    Called per-event in a thread pool.
    """
    event_id = event.get("id")
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    game_time = event.get("commence_time", "")

    if not event_id:
        return []

    cache_key = f"nhl_props_{event_id}"
    cached = _cache_get(cache_key, ttl_seconds=300)
    if cached:
        return cached

    markets = ",".join(NHL_PROP_MARKETS)
    props = []

    try:
        response = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": markets,
                "oddsFormat": "american",
                "bookmakers": "draftkings,fanduel,betmgm"
            },
            timeout=15
        )

        if response.status_code == 422:
            logger.info(f"[NHL] No props available for "
                        f"{away_team} @ {home_team}")
            return []

        response.raise_for_status()
        data = response.json()

        remaining = response.headers.get("x-requests-remaining")
        if remaining:
            logger.info(f"[NHL] Props fetched for "
                        f"{away_team} @ {home_team}. "
                        f"Calls remaining: {remaining}")

        bookmakers = data.get("bookmakers", [])

        for bookmaker in bookmakers:
            book_title = bookmaker.get("title", "")
            markets_data = bookmaker.get("markets", [])

            for market in markets_data:
                market_key = market.get("key", "")
                outcomes = market.get("outcomes", [])

                player_outcomes = {}
                for outcome in outcomes:
                    player_name = outcome.get("description", "")
                    side = outcome.get("name", "")
                    price = outcome.get("price")
                    point = outcome.get("point")

                    if not player_name:
                        continue

                    if player_name not in player_outcomes:
                        player_outcomes[player_name] = {
                            "over_price": None,
                            "under_price": None,
                            "line": point
                        }

                    if side == "Over":
                        player_outcomes[player_name]["over_price"] = price
                        player_outcomes[player_name]["line"] = point
                    elif side == "Under":
                        player_outcomes[player_name]["under_price"] = price

                for player_name, sides in player_outcomes.items():
                    if (sides["over_price"] is not None and
                            sides["under_price"] is not None):
                        props.append({
                            "player": player_name,
                            "stat": market_key,
                            "stat_label": get_nhl_stat_label(market_key),
                            "line": sides["line"],
                            "over_price": sides["over_price"],
                            "under_price": sides["under_price"],
                            "odds": sides["over_price"],
                            "bookmaker": book_title,
                            "home_team": home_team,
                            "away_team": away_team,
                            "game_time": game_time,
                            "sport": "NHL"
                        })

        _cache_set(cache_key, props)
        return props

    except Exception as e:
        logger.error(f"[NHL] Failed to fetch props for "
                     f"event {event_id}: {e}")
        return []


def fetch_nhl_props():
    """
    Main entry point: fetch all NHL player props for today's games.
    Returns raw props list (before grouping/no-vig calculation).
    """
    events = fetch_nhl_events()

    if not events:
        logger.warning("[NHL] No events found")
        return []

    logger.info(f"[NHL] Fetching props for {len(events)} events")

    all_props = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(fetch_props_for_event, events))

    for result in results:
        all_props.extend(result)

    logger.info(f"[NHL] Total raw props collected: {len(all_props)}")
    return all_props


def fetch_nhl_game_odds():
    """
    Fetch NHL moneylines, spreads, and totals (game-level odds).
    Separate from player props.
    """
    if not ODDS_API_KEY:
        return []

    try:
        response = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
                "bookmakers": "draftkings,fanduel,betmgm"
            },
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        remaining = response.headers.get("x-requests-remaining")
        logger.info(f"[NHL] Game odds fetched for {len(data)} games. "
                    f"Calls remaining: {remaining}")
        return data
    except Exception as e:
        logger.error(f"[NHL] Failed to fetch game odds: {e}")
        return []


def get_nhl_game_environment_map():
    """Return environment map keyed by 'AWAY @ HOME' using game-level odds."""
    try:
        from team_abbreviations import TEAM_ABBREVIATIONS
    except ImportError:
        TEAM_ABBREVIATIONS = {}

    data = fetch_nhl_game_odds()
    env_map = {}

    for event in data:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        if not home or not away:
            continue
        H = TEAM_ABBREVIATIONS.get(home, home)
        A = TEAM_ABBREVIATIONS.get(away, away)
        matchup_key = f"{A} @ {H}"

        total_point = over_odds = under_odds = home_ml = away_ml = None
        for bm in event.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") == "totals":
                    for o in market.get("outcomes", []):
                        if o.get("name") == "Over":
                            total_point = o.get("point")
                            over_odds = o.get("price")
                        elif o.get("name") == "Under":
                            under_odds = o.get("price")
                elif market.get("key") == "h2h":
                    for o in market.get("outcomes", []):
                        if o.get("name") == home:
                            home_ml = o.get("price")
                        elif o.get("name") == away:
                            away_ml = o.get("price")

        favored = None
        if home_ml is not None and away_ml is not None:
            favored = H if home_ml < away_ml else A

        environment = "Neutral"
        if total_point is not None:
            try:
                t = float(total_point)
                if t >= 6.5:
                    environment = "High Scoring"
                elif t <= 5.0:
                    environment = "Low Scoring"
            except Exception:
                pass

        env_map[matchup_key] = {
            "environment": environment,
            "total": total_point,
            "over_odds": over_odds,
            "under_odds": under_odds,
            "favored_team": favored,
            "home_team": H,
            "away_team": A,
        }
    return env_map
