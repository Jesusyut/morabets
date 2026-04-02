import requests
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY = "icehockey_nhl"

NHL_PROP_MARKETS = [
    "player_goal_scorer",
    "player_anytime_goalscorer",
    "player_points",
    "player_shots_on_goal",
    "player_power_play_points"
]

NHL_STAT_LABELS = {
    "player_goal_scorer":        "First Goal Scorer",
    "player_anytime_goalscorer": "Anytime Goal Scorer",
    "player_points":             "Points (Over/Under)",
    "player_shots_on_goal":      "Shots on Goal",
    "player_power_play_points":  "Power Play Points"
}

_cache = {}

def _cache_get(key, ttl=300):
    e = _cache.get(key)
    if e and (time.time() - e["ts"]) < ttl:
        return e["data"]
    return None

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": time.time()}

def _log_quota(response):
    remaining = response.headers.get("x-requests-remaining")
    used = response.headers.get("x-requests-used")
    cost = response.headers.get("x-requests-last")
    logger.info(
        f"[NHL][QUOTA] remaining={remaining} "
        f"used={used} last_cost={cost}"
    )


def fetch_nhl_events():
    """
    Step 1 — Get list of today's NHL events.

    Endpoint: GET /v4/sports/icehockey_nhl/events
    Cost: FREE — does not count against quota.
    Returns: list of dicts with id, home_team,
             away_team, commence_time.
    """
    if not ODDS_API_KEY:
        logger.error("[NHL] ODDS_API_KEY not set")
        return []

    cached = _cache_get("nhl_events")
    if cached is not None:
        logger.info(f"[NHL] {len(cached)} events from cache")
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
        logger.info(f"[NHL] Fetched {len(events)} events (free)")
        _cache_set("nhl_events", events)
        return events

    except Exception as e:
        logger.error(f"[NHL] fetch_nhl_events error: {e}")
        return []


def fetch_props_for_event(event):
    """
    Step 2 — Fetch player props for one NHL event.

    Endpoint:
    GET /v4/sports/icehockey_nhl/events/{eventId}/odds

    Cost: 1 credit per market per region.
    5 markets x 1 region = 5 credits per event.

    CRITICAL — official docs confirm prop outcome shape:
    {
      "name": "Over",               <- Over or Under ONLY
      "description": "Player Name", <- actual player name
      "price": -148,
      "point": 1.5
    }
    Player name MUST be read from outcome["description"].
    """
    event_id = event.get("id")
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    game_time = event.get("commence_time", "")
    matchup = f"{away_team} @ {home_team}"

    if not event_id:
        return []

    cache_key = f"nhl_props_{event_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    props = []

    try:
        response = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}"
            f"/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": ",".join(NHL_PROP_MARKETS),
                "oddsFormat": "american",
                "bookmakers": "draftkings,fanduel,betmgm,caesars,pointsbetus,betrivers,bovada,betonlineag,fanatics"
            },
            timeout=15
        )

        # 422 = props not posted yet — normal, not an error
        if response.status_code == 422:
            logger.info(
                f"[NHL] Props not posted yet: {matchup}"
            )
            _cache_set(cache_key, [])
            return []

        # 429 = rate limited
        if response.status_code == 429:
            logger.warning("[NHL] Rate limited — slow down")
            return []

        response.raise_for_status()
        _log_quota(response)
        data = response.json()

        bookmakers = data.get("bookmakers", [])

        for bookmaker in bookmakers:
            book_title = bookmaker.get("title", "")
            markets_list = bookmaker.get("markets", [])

            for market in markets_list:
                market_key = market.get("key", "")
                outcomes = market.get("outcomes", [])

                # Group outcomes by player name
                # Player name = outcome["description"] per docs
                # Side (Over/Under) = outcome["name"] per docs
                player_map = {}

                for outcome in outcomes:
                    # CORRECT field for player name
                    player_name = outcome.get("description", "")
                    side = outcome.get("name", "")
                    price = outcome.get("price")
                    point = outcome.get("point")

                    if not player_name or price is None:
                        continue

                    if player_name not in player_map:
                        player_map[player_name] = {
                            "over_price": None,
                            "under_price": None,
                            "line": point
                        }

                    if side == "Over":
                        player_map[player_name]["over_price"] = price
                        player_map[player_name]["line"] = point
                    elif side == "Under":
                        player_map[player_name]["under_price"] = price

                # Build props — require at least an over price; under can be None
                for player_name, sides in player_map.items():
                    over = sides.get("over_price")
                    under = sides.get("under_price")
                    line = sides.get("line")

                    if over is None:
                        continue  # Need at least the over price

                    props.append({
                        "player":      player_name,
                        "stat":        market_key,
                        "stat_label":  NHL_STAT_LABELS.get(
                                           market_key,
                                           market_key
                                           .replace("_", " ")
                                           .title()
                                       ),
                        "line":        line,
                        "over_price":  over,
                        "under_price": under,
                        "odds":        over,
                        "bookmaker":   book_title,
                        "home_team":   home_team,
                        "away_team":   away_team,
                        "matchup":     matchup,
                        "game_time":   game_time,
                        "sport":       "NHL"
                    })

        logger.info(
            f"[NHL] {matchup}: {len(props)} props parsed"
        )
        _cache_set(cache_key, props)
        return props

    except requests.exceptions.HTTPError as e:
        logger.error(
            f"[NHL] HTTP {e.response.status_code} "
            f"for {matchup}: {e.response.text[:200]}"
        )
        return []
    except Exception as e:
        logger.error(
            f"[NHL] fetch_props_for_event error "
            f"for {matchup}: {e}"
        )
        return []


def fetch_nhl_props():
    """
    Main entry point — fetches all NHL props for today.

    Step 1: /events (free) -> get event IDs
    Step 2: /events/{id}/odds (5 credits per event) -> props

    Returns raw props list for group_props_by_player()
    and sort_props_by_tier() to process downstream.
    """
    if not ODDS_API_KEY:
        logger.error("[NHL] ODDS_API_KEY not set")
        return []

    events = fetch_nhl_events()
    if not events:
        logger.warning("[NHL] No NHL events today")
        return []

    logger.info(
        f"[NHL] Fetching props for {len(events)} events"
    )

    # max_workers=3 to avoid 429 rate limiting
    all_props = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(fetch_props_for_event, events)
        )

    for result in results:
        if result:
            all_props.extend(result)

    logger.info(
        f"[NHL] Total raw props: {len(all_props)}"
    )
    return all_props


def fetch_nhl_game_odds():
    """
    Game-level odds only (moneyline, spread, total).
    Not props. Uses /odds endpoint directly.
    Cost: 3 credits (3 markets x 1 region)
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
        _log_quota(response)
        data = response.json()
        logger.info(
            f"[NHL] Game odds: {len(data)} games"
        )
        return data
    except Exception as e:
        logger.error(f"[NHL] fetch_nhl_game_odds error: {e}")
        return []


def get_nhl_game_environment_map():
    """
    Returns a minimal environment map for NHL games.
    NHL does not have ballpark factors like MLB,
    so returns an empty dict — routes won't 500.
    """
    return {}
