"""
NHL player props and game odds fetcher.
Mirrors odds_api.py (MLB) pipeline exactly:
  - fetch_player_props() returns raw prop list
  - group_props_by_player() (in odds_api.py) handles grouping + EV
  - Two market batches per event to stay within quota limits
"""

import requests
from datetime import datetime, timedelta
import os
import logging
import time

logger = logging.getLogger(__name__)

BASE_URL     = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY    = "icehockey_nhl"

# Mirror exact cache pattern from odds_api.py
_cache_store = {}


def _cache_get(key, ttl_seconds=300):
    entry = _cache_store.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_seconds:
        return entry["data"]
    return None


def _cache_set(key, data):
    _cache_store[key] = {"data": data, "ts": time.time()}


NHL_STAT_LABELS = {
    "player_shots_on_goal":      "Shots on Goal",
    "player_points":             "Points",
    "player_goal_scorer":        "First Goal Scorer",
    "player_anytime_goalscorer": "Anytime Goalscorer",
}


def fetch_nhl_events():
    """
    Step 1: Get NHL event list for the next 24 hours.
    FREE endpoint — zero quota cost.
    GET /v4/sports/icehockey_nhl/events
    """
    cached = _cache_get("nhl_events")
    if cached is not None:
        logger.info(f"[NHL] {len(cached)} events from cache")
        return cached

    if not ODDS_API_KEY:
        logger.error("[NHL] No ODDS_API_KEY")
        return []

    now    = datetime.utcnow()
    future = now + timedelta(hours=24)

    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/events",
            params={
                "apiKey":             ODDS_API_KEY,
                "commenceTimeFrom":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commenceTimeTo":     future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dateFormat":         "iso",
            },
            timeout=20,
        )
        resp.raise_for_status()
        events = resp.json()
        logger.info(f"[NHL] Fetched {len(events)} events (free)")
        _cache_set("nhl_events", events)
        return events
    except Exception as e:
        logger.error(f"[NHL] fetch_nhl_events error: {e}")
        return []


def fetch_player_props():
    """
    Step 2: Fetch NHL player props for all today's events.
    Mirrors fetch_player_props() in odds_api.py exactly.

    Per official Odds API NHL docs:
      - sport key:   icehockey_nhl
      - player name: outcome["description"]
      - side:        outcome["name"]  →  "Over" or "Under"
      - endpoint:    GET /events/{eventId}/odds

    Two market batches per event to control quota cost and
    avoid hitting per-request market limits:
      Batch 0: player_shots_on_goal, player_points
      Batch 1: player_goal_scorer, player_anytime_goalscorer
    """
    if not ODDS_API_KEY:
        logger.error("[NHL] No ODDS_API_KEY")
        return []

    events = fetch_nhl_events()
    if not events:
        logger.warning("[NHL] No events today")
        return []

    logger.info(f"[NHL] Fetching props for {len(events)} events")

    MARKET_BATCHES = [
        ["player_shots_on_goal", "player_points"],
        ["player_goal_scorer",   "player_anytime_goalscorer"],
    ]
    BOOKS = "draftkings,fanduel,betmgm,caesars,betrivers"

    all_props = []

    for event in events:
        eid       = event.get("id")
        home      = event.get("home_team", "")
        away      = event.get("away_team", "")
        game_time = event.get("commence_time", "")
        matchup   = f"{away} @ {home}"

        if not eid:
            continue

        for batch_idx, markets in enumerate(MARKET_BATCHES):
            # Mirror MLB delay between batches
            if batch_idx > 0:
                time.sleep(0.5)

            try:
                resp = requests.get(
                    f"{BASE_URL}/sports/{SPORT_KEY}/events/{eid}/odds",
                    params={
                        "apiKey":     ODDS_API_KEY,
                        "regions":    "us",
                        "markets":    ",".join(markets),
                        "oddsFormat": "american",
                        "bookmakers": BOOKS,
                    },
                    timeout=20,
                )

                # 422 = props not posted yet — normal, handle silently
                if resp.status_code == 422:
                    logger.info(f"[NHL] Not posted: {matchup} batch {batch_idx}")
                    continue

                # 429 = rate limited — back off and skip batch
                if resp.status_code == 429:
                    logger.warning("[NHL] Rate limited — sleeping 5s")
                    time.sleep(5)
                    continue

                resp.raise_for_status()

                quota = resp.headers.get("x-requests-remaining", "?")
                logger.info(
                    f"[NHL] {matchup} batch {batch_idx}: quota={quota}"
                )

                data = resp.json()

                for book in data.get("bookmakers", []):
                    book_title = book.get("title", "")
                    if not book_title:
                        continue

                    for market in book.get("markets", []):
                        market_key = market.get("key", "")
                        outcomes   = market.get("outcomes", [])

                        # ── CRITICAL PAIRING LOGIC — mirrors MLB exactly ──
                        # outcome["description"] = player name
                        # outcome["name"]        = "Over" or "Under"
                        # Group Over/Under by player within each market+book
                        player_map = {}

                        for outcome in outcomes:
                            player_name = outcome.get("description", "")
                            side        = outcome.get("name", "")
                            price       = outcome.get("price")
                            point       = outcome.get("point")

                            if not player_name or price is None:
                                continue

                            if player_name not in player_map:
                                player_map[player_name] = {
                                    "over_price":  None,
                                    "under_price": None,
                                    "line":        point,
                                }

                            if side == "Over":
                                player_map[player_name]["over_price"] = price
                                player_map[player_name]["line"]        = point
                            elif side == "Under":
                                player_map[player_name]["under_price"] = price

                        # Build one row per player — require over price at minimum
                        for pname, sides in player_map.items():
                            if sides["over_price"] is None:
                                continue

                            all_props.append({
                                "player":      pname,
                                "stat":        market_key,
                                "stat_label":  NHL_STAT_LABELS.get(
                                    market_key,
                                    market_key.replace("_", " ").title(),
                                ),
                                "line":        sides["line"],
                                "over_price":  sides["over_price"],
                                "under_price": sides["under_price"],
                                "odds":        sides["over_price"],
                                "bookmaker":   book_title,
                                "home_team":   home,
                                "away_team":   away,
                                "matchup":     matchup,
                                "game_time":   game_time,
                                "sport":       "NHL",
                            })

            except Exception as e:
                logger.error(
                    f"[NHL] Error {matchup} batch {batch_idx}: {e}"
                )
                continue

    stat_counts = {}
    for p in all_props:
        k = p.get("stat", "?")
        stat_counts[k] = stat_counts.get(k, 0) + 1

    logger.info(
        f"[NHL] {len(all_props)} raw props "
        f"from {len(events)} events: {stat_counts}"
    )
    return all_props


def fetch_nhl_game_odds():
    """
    Game-level odds only (moneyline, spread, total).
    Not player props. Uses /odds endpoint directly.
    """
    if not ODDS_API_KEY:
        return []

    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/odds",
            params={
                "apiKey":     ODDS_API_KEY,
                "regions":    "us",
                "markets":    "h2h,spreads,totals",
                "oddsFormat": "american",
                "bookmakers": "draftkings,fanduel,betmgm",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"[NHL] Game odds: {len(data)} games")
        return data
    except Exception as e:
        logger.error(f"[NHL] fetch_nhl_game_odds error: {e}")
        return []


def get_nhl_game_environment_map():
    """
    Returns an empty environment map for NHL.
    NHL has no ballpark factors — routes won't 500.
    """
    return {}
