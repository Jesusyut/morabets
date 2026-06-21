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


def classify_soccer_environment(total_point):
    """
    Classify a soccer game's scoring environment from the over/under total goals
    line. Soccer totals cluster around 2.5 goals, so the bands are tighter than
    MLB/NHL run/goal totals.
      >= 3.0  -> HIGH SCORING
      <= 2.25 -> LOW SCORING
      else    -> "" (neutral, e.g. the common 2.5 line)
    """
    try:
        if total_point is None:
            return ""
        pt = float(total_point)
        if pt >= 3.0:
            return "HIGH SCORING"
        if pt <= 2.25:
            return "LOW SCORING"
        return ""
    except (TypeError, ValueError):
        return ""


def fetch_soccer_game_lines():
    """
    Fetch game-level lines for MLS and FIFA World Cup: h2h (3-way moneyline:
    home / draw / away) and totals (over/under total goals). Separate from
    player props.

    Unlike player props, this is ONE simple /odds call per sport_key (not an
    event-by-event loop) — the /odds endpoint returns every game with its lines
    in a single response, mirroring how MLB/NHL game lines are pulled.

    Returns a list of game dicts, each with:
      home_team, away_team, matchup, game_time, league,
      no_vig_home / no_vig_draw / no_vig_away (true probabilities, 0-100),
      favored_team (home or away — whichever has the higher no-vig win prob),
      favored_prob, environment ("HIGH SCORING" / "LOW SCORING" / ""),
      h2h (best price + all_books per outcome), totals (point + best prices).
    """
    from probability import no_vig_three_way

    if not ODDS_API_KEY:
        logger.error("[SOCCER LINES] No ODDS_API_KEY")
        return []

    cache_key = "soccer_game_lines_all"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.info(f"[SOCCER LINES] {len(cached)} game lines from cache")
        return cached

    BOOKS = "draftkings,fanduel,betmgm,betrivers"
    games = []

    for sport_key, league_label in SPORT_KEYS:
        try:
            resp = requests.get(
                f"{BASE_URL}/sports/{sport_key}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "regions":    "us",
                    "markets":    "h2h,totals",
                    "oddsFormat": "american",
                    "bookmakers": BOOKS,
                },
                timeout=20,
            )

            if resp.status_code in (404, 422):
                logger.info(f"[SOCCER LINES:{sport_key}] No game lines (status {resp.status_code}) — out of season")
                continue
            if resp.status_code == 429:
                logger.warning(f"[SOCCER LINES:{sport_key}] Rate limited — skipping")
                continue

            resp.raise_for_status()

            quota = resp.headers.get("x-requests-remaining", "?")
            data = resp.json()
            logger.info(f"[SOCCER LINES:{sport_key}] {len(data)} games ({league_label}) quota={quota}")

            for game in data:
                home      = game.get("home_team", "")
                away      = game.get("away_team", "")
                game_time = game.get("commence_time", "")
                if not home or not away:
                    continue
                matchup = f"{away} @ {home}"

                # Per-book prices for each outcome
                home_books = []   # [{book, price}]
                draw_books = []
                away_books = []
                # Per-book triples (only books that posted all three outcomes)
                triples = []      # [{home, draw, away}]
                total_point = None
                over_books  = []  # [{book, over_price, under_price}]
                under_books = []

                for bk in game.get("bookmakers", []):
                    bk_name = bk.get("title", "")
                    b_home = b_draw = b_away = None
                    for market in bk.get("markets", []):
                        mk       = market.get("key", "")
                        outcomes = market.get("outcomes", [])

                        if mk == "h2h":
                            for o in outcomes:
                                name  = o.get("name", "")
                                price = o.get("price")
                                if price is None:
                                    continue
                                if name == home:
                                    b_home = price
                                    home_books.append({"book": bk_name, "price": price})
                                elif name == away:
                                    b_away = price
                                    away_books.append({"book": bk_name, "price": price})
                                elif name == "Draw":
                                    b_draw = price
                                    draw_books.append({"book": bk_name, "price": price})

                        elif mk == "totals":
                            over_o  = next((o for o in outcomes if o.get("name") == "Over"),  None)
                            under_o = next((o for o in outcomes if o.get("name") == "Under"), None)
                            if over_o and under_o:
                                op = over_o.get("price")
                                up = under_o.get("price")
                                pt = over_o.get("point")
                                if op is not None and up is not None and pt is not None:
                                    total_point = pt
                                    over_books.append({"book": bk_name, "over_price": op,  "under_price": up})
                                    under_books.append({"book": bk_name, "over_price": up, "under_price": op})

                    if b_home is not None and b_draw is not None and b_away is not None:
                        triples.append({"home": b_home, "draw": b_draw, "away": b_away})

                # ── 3-way no-vig: average the per-book stripped probabilities ──
                nv_home_vals, nv_draw_vals, nv_away_vals = [], [], []
                for t in triples:
                    nv = no_vig_three_way(t["home"], t["draw"], t["away"])
                    if nv["no_vig_home"] is not None:
                        nv_home_vals.append(nv["no_vig_home"])
                    if nv["no_vig_draw"] is not None:
                        nv_draw_vals.append(nv["no_vig_draw"])
                    if nv["no_vig_away"] is not None:
                        nv_away_vals.append(nv["no_vig_away"])

                def _avg(vals):
                    return round(sum(vals) / len(vals), 1) if vals else None

                no_vig_home = _avg(nv_home_vals)
                no_vig_draw = _avg(nv_draw_vals)
                no_vig_away = _avg(nv_away_vals)

                # Best (most favorable) American price per outcome across books
                def _best(books):
                    prices = [b["price"] for b in books if b.get("price") is not None]
                    return max(prices) if prices else None

                favored_team = None
                favored_prob = None
                if no_vig_home is not None and no_vig_away is not None:
                    if no_vig_home >= no_vig_away:
                        favored_team, favored_prob = home, no_vig_home
                    else:
                        favored_team, favored_prob = away, no_vig_away

                games.append({
                    "home_team":   home,
                    "away_team":   away,
                    "matchup":     matchup,
                    "game_time":   game_time,
                    "league":      league_label,
                    "sport":       "Soccer",
                    "no_vig_home": no_vig_home,
                    "no_vig_draw": no_vig_draw,
                    "no_vig_away": no_vig_away,
                    "favored_team": favored_team,
                    "favored_prob": favored_prob,
                    "environment": classify_soccer_environment(total_point),
                    "h2h": {
                        "home": {"best_price": _best(home_books), "all_books": home_books},
                        "draw": {"best_price": _best(draw_books), "all_books": draw_books},
                        "away": {"best_price": _best(away_books), "all_books": away_books},
                    },
                    "totals": {
                        "point":       total_point,
                        "over_books":  over_books,
                        "under_books": under_books,
                    },
                })

        except Exception as e:
            logger.error(f"[SOCCER LINES:{sport_key}] error: {e}")
            continue

    logger.info(f"[SOCCER LINES] {len(games)} total game lines built")
    _cache_set(cache_key, games)
    return games
