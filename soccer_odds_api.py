"""
Soccer player props fetcher (MLS + FIFA World Cup).
Mirrors nhl_odds_api.py pipeline exactly:
  - fetch_player_props() returns raw prop list
  - group_props_by_player() (in odds_api.py) handles grouping + EV
  - Two market batches per event to stay within quota limits
  - Cache write happens in app.py wrapper, NOT here
"""

import requests
from datetime import datetime, timedelta
import os
import logging
import time

logger = logging.getLogger(__name__)

BASE_URL     = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

SPORT_KEYS = [
    ("soccer_usa_mls",        "MLS"),
    ("soccer_fifa_world_cup", "FIFA World Cup"),
]

ODDS_MIN = -350
ODDS_MAX =  200

_cache_store = {}


def _cache_get(key, ttl_seconds=300):
    entry = _cache_store.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_seconds:
        return entry["data"]
    return None


def _cache_set(key, data):
    _cache_store[key] = {"data": data, "ts": time.time()}


SOCCER_STAT_LABELS = {
    "player_shots_on_target": "Shots on Target",
    "player_shots":           "Total Shots",
    "player_goals":           "Goals",
    "player_assists":         "Assists",
    "player_cards":           "Cards",
}


def fetch_soccer_events(sport_key):
    """
    Step 1: Get soccer event list for the next 24 hours for one sport key.
    FREE endpoint — zero quota cost.
    """
    cache_key = f"soccer_events_{sport_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"[SOCCER:{sport_key}] {len(cached)} events from cache")
        return cached

    if not ODDS_API_KEY:
        logger.error(f"[SOCCER:{sport_key}] No ODDS_API_KEY")
        return []

    now    = datetime.utcnow()
    future = now + timedelta(hours=24)

    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{sport_key}/events",
            params={
                "apiKey":           ODDS_API_KEY,
                "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commenceTimeTo":   future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dateFormat":       "iso",
            },
            timeout=20,
        )

        # Out-of-season or unknown sport key → silent empty
        if resp.status_code in (404, 422):
            logger.info(f"[SOCCER:{sport_key}] No events (status {resp.status_code}) — out of season")
            _cache_set(cache_key, [])
            return []

        resp.raise_for_status()
        events = resp.json()
        logger.info(f"[SOCCER:{sport_key}] Fetched {len(events)} events (free)")
        _cache_set(cache_key, events)
        return events
    except Exception as e:
        logger.error(f"[SOCCER:{sport_key}] fetch_soccer_events error: {e}")
        return []


def fetch_player_props():
    """
    Step 2: Fetch soccer player props for all today's events across MLS and
    FIFA World Cup. Mirrors nhl_odds_api.fetch_player_props() exactly.

    Two market batches per event:
      Batch 0: player_shots_on_target, player_shots, player_goals
      Batch 1: player_assists, player_cards

    Filter: keep only rows where ODDS_MIN <= over_price <= ODDS_MAX.
    """
    if not ODDS_API_KEY:
        logger.error("[SOCCER] No ODDS_API_KEY")
        return []

    MARKET_BATCHES = [
        ["player_shots_on_target", "player_shots", "player_goals"],
        ["player_assists",         "player_cards"],
    ]
    BOOKS = "draftkings,fanduel,betmgm,betrivers"

    all_props = []
    total_events = 0

    for sport_key, league_label in SPORT_KEYS:
        events = fetch_soccer_events(sport_key)
        if not events:
            logger.info(f"[SOCCER:{sport_key}] No events today — skipping league")
            continue

        total_events += len(events)
        logger.info(f"[SOCCER:{sport_key}] Fetching props for {len(events)} events ({league_label})")

        league_prop_count_start = len(all_props)

        for event in events:
            eid       = event.get("id")
            home      = event.get("home_team", "")
            away      = event.get("away_team", "")
            game_time = event.get("commence_time", "")
            matchup   = f"{away} @ {home}"

            if not eid:
                continue

            for batch_idx, markets in enumerate(MARKET_BATCHES):
                # 0.5s sleep between batches per event
                if batch_idx > 0:
                    time.sleep(0.5)

                try:
                    resp = requests.get(
                        f"{BASE_URL}/sports/{sport_key}/events/{eid}/odds",
                        params={
                            "apiKey":     ODDS_API_KEY,
                            "regions":    "us",
                            "markets":    ",".join(markets),
                            "oddsFormat": "american",
                            "bookmakers": BOOKS,
                        },
                        timeout=20,
                    )

                    if resp.status_code == 422:
                        logger.info(f"[SOCCER:{sport_key}] Not posted: {matchup} batch {batch_idx}")
                        continue

                    if resp.status_code == 429:
                        logger.warning(f"[SOCCER:{sport_key}] Rate limited — sleeping 5s")
                        time.sleep(5)
                        continue

                    resp.raise_for_status()

                    quota = resp.headers.get("x-requests-remaining", "?")
                    logger.info(
                        f"[SOCCER:{sport_key}] {matchup} batch {batch_idx}: quota={quota}"
                    )

                    data = resp.json()

                    for book in data.get("bookmakers", []):
                        book_title = book.get("title", "")
                        if not book_title:
                            continue

                        for market in book.get("markets", []):
                            market_key = market.get("key", "")
                            outcomes   = market.get("outcomes", [])

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

                            for pname, sides in player_map.items():
                                over_price = sides["over_price"]
                                if over_price is None:
                                    continue

                                # Apply odds filter (chalk/long-shot trim)
                                if over_price < ODDS_MIN or over_price > ODDS_MAX:
                                    continue

                                all_props.append({
                                    "player":      pname,
                                    "stat":        market_key,
                                    "stat_label":  SOCCER_STAT_LABELS.get(
                                        market_key,
                                        market_key.replace("_", " ").title(),
                                    ),
                                    "line":        sides["line"],
                                    "over_price":  over_price,
                                    "under_price": sides["under_price"],
                                    "odds":        over_price,
                                    "bookmaker":   book_title,
                                    "home_team":   home,
                                    "away_team":   away,
                                    "matchup":     matchup,
                                    "game_time":   game_time,
                                    "sport":       "Soccer",
                                    "league":      league_label,
                                })

                except Exception as e:
                    logger.error(
                        f"[SOCCER:{sport_key}] Error {matchup} batch {batch_idx}: {e}"
                    )
                    continue

        league_added = len(all_props) - league_prop_count_start
        logger.info(f"[SOCCER:{sport_key}] {league_added} props added from {league_label}")

    stat_counts = {}
    for p in all_props:
        k = p.get("stat", "?")
        stat_counts[k] = stat_counts.get(k, 0) + 1

    logger.info(
        f"[SOCCER] {len(all_props)} raw props "
        f"from {total_events} total events: {stat_counts}"
    )
    return all_props


def fetch_soccer_props():
    """
    Public alias matching the spec's expected name.
    Returns raw player props for MLS + FIFA World Cup.
    Caching is handled by the app.py wrapper.
    """
    return fetch_player_props()
