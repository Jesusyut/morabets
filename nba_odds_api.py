"""
NBA player props and game odds fetcher.
Mirrors nhl_odds_api.py / soccer_odds_api.py pipeline:
  - fetch_player_props() returns raw prop rows
  - odds_api.group_props_by_player() handles grouping + no-vig math
  - fetch_nba_game_odds() returns game-level h2h/spreads/totals
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
SPORT_KEY = "basketball_nba"

_cache_store: Dict[str, Dict[str, Any]] = {}


def _cache_get(key, ttl_seconds=300):
    entry = _cache_store.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_seconds:
        return entry["data"]
    return None


def _cache_set(key, data):
    _cache_store[key] = {"data": data, "ts": time.time()}


NBA_STAT_LABELS = {
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "Made Threes",
    "player_blocks": "Blocks",
    "player_steals": "Steals",
    "player_blocks_steals": "Blocks + Steals",
    "player_turnovers": "Turnovers",
    "player_points_rebounds_assists": "Pts + Reb + Ast",
    "player_points_rebounds": "Pts + Reb",
    "player_points_assists": "Pts + Ast",
    "player_rebounds_assists": "Reb + Ast",
}

ODDS_MIN = -350
ODDS_MAX = 250
BOOKS = "draftkings,fanduel,betmgm,caesars,betrivers,fanatics"


def fetch_nba_events(hours_ahead=72):
    """Fetch NBA events for the next window. The Odds API events endpoint is free-tier."""
    cache_key = f"nba_events_{hours_ahead}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"[NBA] {len(cached)} events from cache")
        return cached

    if not ODDS_API_KEY:
        logger.error("[NBA] No ODDS_API_KEY")
        return []

    now = datetime.utcnow()
    future = now + timedelta(hours=hours_ahead)

    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/events",
            params={
                "apiKey": ODDS_API_KEY,
                "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commenceTimeTo": future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "dateFormat": "iso",
            },
            timeout=20,
        )
        if resp.status_code in (404, 422):
            logger.info(f"[NBA] No events available status={resp.status_code}")
            _cache_set(cache_key, [])
            return []
        resp.raise_for_status()
        events = resp.json()
        logger.info(f"[NBA] Fetched {len(events)} events")
        _cache_set(cache_key, events)
        return events
    except Exception as e:
        logger.error(f"[NBA] fetch_nba_events error: {e}")
        return []


def fetch_player_props():
    """Fetch NBA player prop markets and normalize into MLB/NHL-style raw rows."""
    if not ODDS_API_KEY:
        logger.error("[NBA] No ODDS_API_KEY")
        return []

    events = fetch_nba_events()
    if not events:
        logger.info("[NBA] No events in window")
        return []

    market_batches = [
        ["player_points", "player_rebounds", "player_assists", "player_threes"],
        ["player_points_rebounds_assists", "player_points_rebounds", "player_points_assists", "player_rebounds_assists"],
        ["player_blocks", "player_steals", "player_blocks_steals", "player_turnovers"],
    ]
    all_props: List[Dict[str, Any]] = []

    for event in events:
        eid = event.get("id")
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        game_time = event.get("commence_time", "")
        matchup = f"{away} @ {home}"
        if not eid:
            continue

        for batch_idx, markets in enumerate(market_batches):
            if batch_idx > 0:
                time.sleep(0.5)
            try:
                resp = requests.get(
                    f"{BASE_URL}/sports/{SPORT_KEY}/events/{eid}/odds",
                    params={
                        "apiKey": ODDS_API_KEY,
                        "regions": "us",
                        "markets": ",".join(markets),
                        "oddsFormat": "american",
                        "bookmakers": BOOKS,
                    },
                    timeout=20,
                )
                if resp.status_code == 422:
                    logger.info(f"[NBA] Props not posted: {matchup} batch {batch_idx}")
                    continue
                if resp.status_code == 429:
                    logger.warning("[NBA] Rate limited - sleeping 5s")
                    time.sleep(5)
                    continue
                resp.raise_for_status()
                logger.info(f"[NBA] {matchup} batch {batch_idx}: quota={resp.headers.get('x-requests-remaining', '?')}")
                data = resp.json()

                for book in data.get("bookmakers", []):
                    book_title = book.get("title", "")
                    if not book_title:
                        continue
                    for market in book.get("markets", []):
                        market_key = market.get("key", "")
                        player_map: Dict[str, Dict[str, Any]] = {}
                        for outcome in market.get("outcomes", []):
                            player_name = outcome.get("description", "")
                            side = outcome.get("name", "")
                            price = outcome.get("price")
                            point = outcome.get("point")
                            if not player_name or price is None:
                                continue
                            if player_name not in player_map:
                                player_map[player_name] = {"over_price": None, "under_price": None, "line": point}
                            if side == "Over":
                                player_map[player_name]["over_price"] = price
                                player_map[player_name]["line"] = point
                            elif side == "Under":
                                player_map[player_name]["under_price"] = price

                        for player_name, sides in player_map.items():
                            over_price = sides["over_price"]
                            if over_price is None:
                                continue
                            if over_price < ODDS_MIN or over_price > ODDS_MAX:
                                continue
                            all_props.append({
                                "player": player_name,
                                "stat": market_key,
                                "stat_label": NBA_STAT_LABELS.get(market_key, market_key.replace("_", " ").title()),
                                "line": sides["line"],
                                "over_price": over_price,
                                "under_price": sides["under_price"],
                                "odds": over_price,
                                "bookmaker": book_title,
                                "home_team": home,
                                "away_team": away,
                                "matchup": matchup,
                                "game_time": game_time,
                                "sport": "NBA",
                            })
            except Exception as e:
                logger.error(f"[NBA] Error {matchup} batch {batch_idx}: {e}")
                continue

    stat_counts: Dict[str, int] = {}
    for p in all_props:
        stat_counts[p.get("stat", "?")] = stat_counts.get(p.get("stat", "?"), 0) + 1
    logger.info(f"[NBA] {len(all_props)} raw props from {len(events)} events: {stat_counts}")
    return all_props


def fetch_nba_game_odds(hours_ahead=72):
    """Fetch NBA game-level moneyline, spread, and total odds."""
    if not ODDS_API_KEY:
        return []
    now = datetime.utcnow()
    future = now + timedelta(hours=hours_ahead)
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{SPORT_KEY}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
                "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "commenceTimeTo": future.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bookmakers": BOOKS,
            },
            timeout=20,
        )
        if resp.status_code in (404, 422):
            logger.info(f"[NBA] No game odds status={resp.status_code}")
            return []
        resp.raise_for_status()
        logger.info(f"[NBA] Game odds quota={resp.headers.get('x-requests-remaining', '?')}")
        return resp.json()
    except Exception as e:
        logger.error(f"[NBA] fetch_nba_game_odds error: {e}")
        return []


def _classify_nba_environment(total):
    try:
        t = float(total)
    except Exception:
        return "Neutral"
    if t >= 235:
        return "High"
    if t <= 215:
        return "Low"
    return "Neutral"


def get_nba_game_environment_map(hours_ahead=72):
    games = fetch_nba_game_odds(hours_ahead=hours_ahead)
    env_map: Dict[str, Dict[str, Any]] = {}
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        if not home or not away:
            continue
        matchup = f"{away} @ {home}"
        home_ml = away_ml = total_point = over_odds = under_odds = None
        for book in game.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") == "h2h":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == home:
                            home_ml = outcome.get("price")
                        elif outcome.get("name") == away:
                            away_ml = outcome.get("price")
                elif market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            total_point = outcome.get("point")
                            over_odds = outcome.get("price")
                        elif outcome.get("name") == "Under":
                            under_odds = outcome.get("price")
            if total_point is not None and home_ml is not None and away_ml is not None:
                break
        favored = None
        if home_ml is not None and away_ml is not None:
            favored = home if home_ml < away_ml else away
        env_map[matchup] = {
            "environment": _classify_nba_environment(total_point),
            "total": total_point,
            "over_odds": over_odds,
            "under_odds": under_odds,
            "favored_team": favored,
            "home_team": home,
            "away_team": away,
        }
    return env_map


if __name__ == "__main__":
    print("events", len(fetch_nba_events()))
    print("game_odds", len(fetch_nba_game_odds()))
