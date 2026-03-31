import requests
from datetime import datetime, timedelta
import os
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
# from contextual import get_contextual_hit_rate  # removed — no longer in active code path
# from fantasy import get_fantasy_hit_rate  # removed — no longer in active code path

logger = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# Simple in-process TTL caches to avoid re-hitting rate-limited endpoints
_cache_store = {}

def _cache_get(key, ttl_seconds=3600):
    entry = _cache_store.get(key)
    if entry and (time.time() - entry["ts"]) < ttl_seconds:
        return entry["data"]
    return None

def _cache_set(key, data):
    _cache_store[key] = {"data": data, "ts": time.time()}

# API quota tracking
_last_quota = {"remaining": None, "used": None, "updated_at": None}

def update_quota_from_headers(response):
    try:
        remaining = response.headers.get("x-requests-remaining")
        used = response.headers.get("x-requests-used")
        if remaining is not None:
            _last_quota["remaining"] = int(remaining)
            _last_quota["used"] = int(used) if used else None
            _last_quota["updated_at"] = time.time()
    except Exception:
        pass

def get_quota():
    return dict(_last_quota)

# Preferred sportsbooks for filtering
PREFERRED_SPORTSBOOKS = [
    "draftkings", "fanduel", "betmgm",
    "caesars", "pointsbetus", "betrivers",
    "bovada", "betonlineag", "fanatics"
]
VALID_BOOKS = {"DraftKings", "FanDuel", "BetMGM"}

def get_favored_team(game):
    """
    Determine the favored team based on moneyline odds
    Lower odds = favored team (e.g., -140 is favored over +120)
    """
    home_odds = game.get("home_odds")
    away_odds = game.get("away_odds")
    
    if home_odds is None or away_odds is None:
        return None  # Can't calculate favored team
        
    # Convert odds to numerical values for comparison
    # Negative odds are favorites, positive odds are underdogs
    home_team = game.get("home_team")
    away_team = game.get("away_team")
    
    # Lower odds value = favorite
    if home_odds < away_odds:
        return home_team
    else:
        return away_team

def parse_game_data():
    """Fetch moneylines with preferred sportsbooks first, fallback to all if needed"""
    now = datetime.utcnow()
    future = now + timedelta(hours=48)
    start_time = now.replace(microsecond=0).isoformat() + "Z"
    end_time = future.replace(microsecond=0).isoformat() + "Z"

    if not ODDS_API_KEY:
        print("[ERROR] ODDS_API_KEY is not set")
        return []

    # Try preferred sportsbooks first
    try:
        print(f"[DEBUG] Fetching moneylines from preferred sportsbooks: {PREFERRED_SPORTSBOOKS}")
        response = requests.get(
            f"{BASE_URL}/sports/baseball_mlb/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
                "commenceTimeFrom": start_time,
                "commenceTimeTo": end_time,
                "bookmakers": ",".join(PREFERRED_SPORTSBOOKS)
            },
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        print(f"[INFO] Retrieved {len(data)} moneyline matchups from preferred sportsbooks")
        
        # If we got good data, return it
        if data and len(data) > 0:
            return data
        else:
            print("[WARNING] No moneylines from preferred sportsbooks, falling back to all sportsbooks")
            
    except Exception as e:
        print(f"[ERROR] Failed to fetch odds from preferred sportsbooks: {e}, falling back to all sportsbooks")

    # Fallback to all sportsbooks
    try:
        print("[DEBUG] Fetching moneylines from all sportsbooks")
        response = requests.get(
            f"{BASE_URL}/sports/baseball_mlb/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
                "commenceTimeFrom": start_time,
                "commenceTimeTo": end_time
            },
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        print(f"[INFO] Retrieved {len(data)} moneyline matchups from all sportsbooks")
        return data
    except Exception as e:
        print(f"[ERROR] Failed to fetch odds from all sportsbooks: {e}")
        return []

def get_matchup_map():
    """Get today's games with accurate team matchups from Odds API"""
    from team_abbreviations import TEAM_ABBREVIATIONS
    
    now = datetime.utcnow()
    future = now + timedelta(hours=48)
    start_time = now.replace(microsecond=0).isoformat() + "Z"
    end_time = future.replace(microsecond=0).isoformat() + "Z"

    if not ODDS_API_KEY:
        print("[ERROR] ODDS_API_KEY is not set")
        return {}

    try:
        response = requests.get(
            f"{BASE_URL}/sports/baseball_mlb/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
                "commenceTimeFrom": start_time,
                "commenceTimeTo": end_time,
                "bookmakers": ",".join(PREFERRED_SPORTSBOOKS)
            },
            timeout=20
        )
        response.raise_for_status()
        games = response.json()
        
        matchup_map = {}
        for game in games:
            home_team = game.get("home_team", "")
            away_team = game.get("away_team", "")
            game_id = game.get("id", "")
            
            # Convert team names to abbreviations
            home_abbr = TEAM_ABBREVIATIONS.get(home_team, home_team)
            away_abbr = TEAM_ABBREVIATIONS.get(away_team, away_team)
            
            matchup_str = f"{away_abbr} @ {home_abbr}"
            matchup_map[matchup_str] = {
                "teams": [home_abbr, away_abbr],
                "game_id": game_id,
                "home_team": home_team,
                "away_team": away_team
            }
        
        print(f"[INFO] Built matchup map with {len(matchup_map)} games: {list(matchup_map.keys())}")
        return matchup_map
        
    except Exception as e:
        print(f"[ERROR] Failed to build matchup map: {e}")
        return {}

def get_mlb_totals_odds():
    """Fetch over/under totals odds for MLB games"""
    now = datetime.utcnow()
    future = now + timedelta(hours=48)
    start_time = now.replace(microsecond=0).isoformat() + "Z"
    end_time = future.replace(microsecond=0).isoformat() + "Z"

    if not ODDS_API_KEY:
        print("[ERROR] ODDS_API_KEY is not set")
        return []

    try:
        print("[DEBUG] Fetching MLB totals odds")
        response = requests.get(
            f"{BASE_URL}/sports/baseball_mlb/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "totals",
                "oddsFormat": "american",
                "commenceTimeFrom": start_time,
                "commenceTimeTo": end_time,
                "bookmakers": ",".join(PREFERRED_SPORTSBOOKS)
            },
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        print(f"[INFO] Retrieved totals odds for {len(data)} MLB games")
        return data
        
    except Exception as e:
        print(f"[ERROR] Failed to fetch totals odds: {e}")
        return []

def fetch_mlb_events():
    """
    Fetch just the MLB event list (home/away team names).
    This endpoint is free-tier and works even when the odds endpoints are at quota.
    Returns a list of event dicts with 'home_team' and 'away_team'.
    Cached for 30 minutes to avoid repeated hits when rate-limited.
    """
    cached = _cache_get("mlb_events", ttl_seconds=1800)
    if cached is not None:
        print(f"[EVENTS] Returning {len(cached)} MLB events from cache")
        return cached

    now = datetime.utcnow()
    future = now + timedelta(hours=48)
    resp = requests.get(
        f"{BASE_URL}/sports/baseball_mlb/events",
        params={
            "apiKey": ODDS_API_KEY,
            "commenceTimeFrom": now.strftime('%Y-%m-%dT%H:%M:%SZ'),
            "commenceTimeTo": future.strftime('%Y-%m-%dT%H:%M:%SZ'),
        },
        timeout=15
    )
    resp.raise_for_status()
    events = resp.json()
    print(f"[EVENTS] Fetched {len(events)} MLB events")
    _cache_set("mlb_events", events)
    return events


def get_mlb_stats_environment_map():
    """
    Fallback environment map using free data sources:
    - Odds API events endpoint (free tier) for home/away team names
    - MLB Stats API (completely free, no key) for win% and runs-per-game

    Favorite = team with higher win percentage (home gets +3% edge).
    Scoring label = estimated total derived from team offensive/defensive stats.
    Returns the same dict format as get_mlb_game_environment_map().
    """
    cached = _cache_get("mlb_stats_env_map", ttl_seconds=3600)
    if cached is not None:
        print(f"[STATS ENV] Returning {len(cached)} environments from cache")
        return cached

    from team_abbreviations import TEAM_ABBREVIATIONS

    # Additional name mappings for teams that changed names/locations
    EXTRA_ABBR = {
        "Athletics": "ATH",        # Moved from Oakland; events API drops city name
        "Oakland Athletics": "OAK",
    }

    def team_to_abbr(full_name):
        if full_name in EXTRA_ABBR:
            return EXTRA_ABBR[full_name]
        if full_name in TEAM_ABBREVIATIONS:
            return TEAM_ABBREVIATIONS[full_name]
        return full_name[:3].upper()

    # --- Step 1: Get today's matchups from events endpoint ---
    try:
        events = fetch_mlb_events()
    except Exception as e:
        print(f"[STATS ENV] Events fetch failed: {e}")
        return {}

    # --- Step 2: MLB Stats API standings (free, no auth required) ---
    team_stats = {}
    try:
        r = requests.get(
            "https://statsapi.mlb.com/api/v1/standings",
            params={"leagueId": "103,104", "season": "2025", "standingsTypes": "regularSeason"},
            timeout=10
        )
        r.raise_for_status()
        for division in r.json().get("records", []):
            for tr in division.get("teamRecords", []):
                name = tr.get("team", {}).get("name", "")
                games = max(tr.get("gamesPlayed", 1) or 1, 1)
                win_pct = float(tr.get("winningPercentage", "0.500") or "0.500")
                rs = tr.get("runsScored", 0) or 0
                ra = tr.get("runsAllowed", 0) or 0
                if name:
                    team_stats[name] = {
                        "win_pct": win_pct,
                        "rs_pg": rs / games,
                        "ra_pg": ra / games,
                    }
        print(f"[STATS ENV] Loaded stats for {len(team_stats)} teams from MLB Stats API")
    except Exception as e:
        print(f"[STATS ENV] MLB Stats API failed: {e}")

    def find_stats(event_name):
        """Match an events-API team name to stats-API team name."""
        if event_name in team_stats:
            return team_stats[event_name]
        for stat_name, s in team_stats.items():
            if event_name in stat_name or stat_name in event_name:
                return s
            # Last-word match (e.g. "Blue Jays" vs "Blue Jays")
            if (event_name.split()[-1] == stat_name.split()[-1]
                    and len(event_name.split()[-1]) > 4):
                return s
        return None

    env_map = {}
    for event in events:
        home_name = event.get("home_team", "")
        away_name = event.get("away_team", "")
        if not home_name or not away_name:
            continue

        home_abbr = team_to_abbr(home_name)
        away_abbr = team_to_abbr(away_name)
        matchup_key = f"{away_abbr} @ {home_abbr}"

        home_s = find_stats(home_name)
        away_s = find_stats(away_name)

        # Favorite: higher win% wins; home team gets +3% advantage
        favored_team = None
        if home_s and away_s:
            if (home_s["win_pct"] + 0.03) >= away_s["win_pct"]:
                favored_team = home_abbr
            else:
                favored_team = away_abbr

        # Scoring estimate: expected runs each side, summed
        environment_label = "Neutral"
        estimated_total = None
        if home_s and away_s:
            away_exp = (away_s["rs_pg"] + home_s["ra_pg"]) / 2
            home_exp = (home_s["rs_pg"] + away_s["ra_pg"]) / 2
            estimated_total = round(away_exp + home_exp, 1)
            if estimated_total >= 9.5:
                environment_label = "High Scoring"
            elif estimated_total <= 8.0:
                environment_label = "Low Scoring"

        underdog_team = None
        if favored_team == home_abbr:
            underdog_team = away_abbr
        elif favored_team == away_abbr:
            underdog_team = home_abbr

        fav_str = f" (Fav: {favored_team})" if favored_team else ""
        total_str = f" Total~{estimated_total}" if estimated_total else ""
        print(f"[ENV] {matchup_key}: {environment_label}{total_str}{fav_str} [stats-based]")

        env_map[matchup_key] = {
            "environment": environment_label,
            "total": estimated_total,
            "favored_team": favored_team,
            "home_team": home_abbr,
            "away_team": away_abbr,
            "underdog_team": underdog_team,
            "source": "mlb_stats_api",
        }

    print(f"[STATS ENV] Built environment map for {len(env_map)} games")
    if env_map:
        _cache_set("mlb_stats_env_map", env_map)
    return env_map


def get_mlb_game_environment_map():
    """Get environment classification and favored team for each MLB game"""
    cached = _cache_get("mlb_env_map", ttl_seconds=3600)
    if cached is not None:
        print(f"[ENV] Returning {len(cached)} game environments from cache")
        return cached

    from mlb_game_enrichment import classify_game_environment
    from team_abbreviations import TEAM_ABBREVIATIONS

    totals_data = get_mlb_totals_odds()
    moneyline_data = parse_game_data()  # Get moneylines for favored team calculation
    env_map = {}
    
    # Create a lookup for moneyline odds by team matchup
    moneyline_lookup = {}
    for game in moneyline_data:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        
        if home_team and away_team:
            home_abbr = TEAM_ABBREVIATIONS.get(home_team, home_team)
            away_abbr = TEAM_ABBREVIATIONS.get(away_team, away_team)
            matchup_key = f"{away_abbr} @ {home_abbr}"
            
            # Extract moneyline odds
            for bookmaker in game.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "h2h":  # head-to-head (moneyline)
                        outcomes = market.get("outcomes", [])
                        
                        home_odds = None
                        away_odds = None
                        
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                home_odds = outcome.get("price")
                            elif outcome.get("name") == away_team:
                                away_odds = outcome.get("price")
                        
                        if home_odds and away_odds:
                            # Determine favored team
                            favored_team = home_abbr if home_odds < away_odds else away_abbr
                            
                            moneyline_lookup[matchup_key] = {
                                "home_odds": home_odds,
                                "away_odds": away_odds,
                                "favored_team": favored_team
                            }
                            break
                if matchup_key in moneyline_lookup:
                    break

    for game in totals_data:
        try:
            home_team = game.get("home_team", "")
            away_team = game.get("away_team", "")
            
            if not home_team or not away_team:
                continue
                
            # Convert to abbreviations
            home_abbr = TEAM_ABBREVIATIONS.get(home_team, home_team)
            away_abbr = TEAM_ABBREVIATIONS.get(away_team, away_team)
            matchup_key = f"{away_abbr} @ {home_abbr}"
                
            # Find totals market in bookmakers
            for bookmaker in game.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "totals":
                        outcomes = market.get("outcomes", [])
                        
                        total_point = None
                        over_odds = None
                        under_odds = None
                        
                        for outcome in outcomes:
                            if outcome.get("name") == "Over":
                                total_point = outcome.get("point")
                                over_odds = outcome.get("price")
                            elif outcome.get("name") == "Under":
                                under_odds = outcome.get("price")
                        
                        if total_point and over_odds and under_odds:
                            label = classify_game_environment(total_point, over_odds, under_odds)
                            
                            # Get favored team from moneyline lookup
                            moneyline_info = moneyline_lookup.get(matchup_key, {})
                            favored_team = moneyline_info.get("favored_team")
                            
                            env_map[matchup_key] = {
                                "environment": label,
                                "total": total_point,
                                "over_odds": over_odds,
                                "under_odds": under_odds,
                                "favored_team": favored_team,
                                "home_team": home_abbr,
                                "away_team": away_abbr
                            }
                            
                            fav_indicator = f" (Fav: {favored_team})" if favored_team else ""
                            print(f"[ENV] {matchup_key}: {label} (Total: {total_point}){fav_indicator}")
                            break
                if matchup_key in env_map:
                    break
                    
        except Exception as e:
            logger.debug(f"Error processing game environment for {game}: {e}")
            continue

    print(f"[INFO] Classified {len(env_map)} game environments with favored teams")

    # If Odds API returned no data (quota hit, 401, etc.), fall back to stats-based approach
    if not env_map:
        print("[INFO] Odds API returned no environment data — using stats-based fallback")
        env_map = get_mlb_stats_environment_map()

    if env_map:
        _cache_set("mlb_env_map", env_map)

    return env_map

def fetch_player_props():
    """Fetch player props with preferred sportsbooks first, fallback to all if needed"""
    now = datetime.utcnow()
    future = now + timedelta(hours=48)
    start_time = now.replace(microsecond=0).isoformat() + "Z"
    end_time = future.replace(microsecond=0).isoformat() + "Z"

    if not ODDS_API_KEY:
        print("[ERROR] ODDS_API_KEY is not set")
        return []

    try:
        event_resp = requests.get(
            f"{BASE_URL}/sports/baseball_mlb/events",
            params={
                "apiKey": ODDS_API_KEY,
                "commenceTimeFrom": start_time,
                "commenceTimeTo": end_time
            },
            timeout=20
        )
        event_resp.raise_for_status()
        events = event_resp.json()
        print(f"[INFO] Found {len(events)} events")
    except Exception as e:
        print(f"[ERROR] Failed to fetch MLB events: {e}")
        return []

    props = []
    print(f"[DEBUG] Starting prop collection for {len(events)} events")
    
    # Define available markets only (7 markets total) - confirmed working with API
    markets_batch_1 = ["batter_hits", "batter_home_runs", "batter_total_bases"]
    markets_batch_2 = ["pitcher_strikeouts", "pitcher_earned_runs", "pitcher_outs", "pitcher_hits_allowed"]
    
    print(f"[DEBUG] Using verified markets: {markets_batch_1 + markets_batch_2}")
    
    all_markets = [markets_batch_1, markets_batch_2]

    for event in events:
        eid = event.get("id")
        if not eid:
            continue

        # Process each market batch to avoid rate limiting
        for batch_idx, markets in enumerate(all_markets):
            try:
                # Add delay between batches to respect rate limits
                if batch_idx > 0:
                    time.sleep(1)
                
                odds_resp = requests.get(
                    f"{BASE_URL}/sports/baseball_mlb/events/{eid}/odds",
                    params={
                        "apiKey": ODDS_API_KEY,
                        "regions": "us",
                        "markets": ",".join(markets),
                        "oddsFormat": "american",
                        "bookmakers": ",".join(PREFERRED_SPORTSBOOKS)
                    },
                    timeout=20
                )
                odds_resp.raise_for_status()
                update_quota_from_headers(odds_resp)
                data = odds_resp.json()
                
                # Log successful market response
                if data.get("bookmakers"):
                    successful_markets = [m.get('key') for m in data.get('bookmakers', [])[0].get('markets', [])]
                    print(f"[DEBUG] Event {eid} batch {batch_idx} fetched props for markets: {successful_markets}")
                
                for book in data.get("bookmakers", []):
                    book_title = book.get("title", "Unknown")
                    
                    # Filter to only valid sportsbooks
                    if book_title not in VALID_BOOKS:
                        continue
                    
                    for market in book.get("markets", []):
                        stat = market.get("key")
                        # Group over/under outcomes by player for no-vig math
                        player_outcomes = {}
                        for outcome in market.get("outcomes", []):
                            player = outcome.get("description")
                            side = outcome.get("name", "").strip()
                            price = outcome.get("price")
                            point = outcome.get("point")
                            if player and price is not None:
                                if player not in player_outcomes:
                                    player_outcomes[player] = {"point": point}
                                if side == "Over":
                                    player_outcomes[player]["over_price"] = price
                                elif side == "Under":
                                    player_outcomes[player]["under_price"] = price
                        for player, data_p in player_outcomes.items():
                            over_price = data_p.get("over_price")
                            under_price = data_p.get("under_price")
                            if over_price is not None and under_price is not None:
                                props.append({
                                    "player": player,
                                    "stat": stat,
                                    "line": data_p.get("point"),
                                    "over_price": over_price,
                                    "under_price": under_price,
                                    "odds": over_price,  # backward compat
                                    "bookmaker": book_title
                                })
                                
            except Exception as e:
                print(f"[ERROR] Failed to fetch props for event {eid} batch {batch_idx}: {e}")
                continue
        
        print(f"[DEBUG] Event {eid}: Collected {len(props)} props so far")

    print(f"[INFO] Final count of player props: {len(props)}")
    print(f"[DEBUG] Final props fetched: {len(props)}")
    print(f"🔍 DEBUG: Fetched {len(props)} raw props from API")
    
    # Debug: Show stat type breakdown
    stat_counts = {}
    for prop in props:
        stat = prop.get('stat', 'unknown')
        stat_counts[stat] = stat_counts.get(stat, 0) + 1
    
    print(f"[DEBUG] Props by stat type: {stat_counts}")
    return props

def group_props_by_player(props):
    """
    Group props by player+stat+line. For each group, collect all books,
    run full EV evaluation, and return one enriched prop dict per unique
    player+stat+line. Only props passing the EV threshold are surfaced.
    """
    from ev_engine import evaluate_pick

    groups = {}
    for p in props:
        key = f"{p['player']}_{p['stat']}_{p['line']}"
        if key not in groups:
            groups[key] = {
                "player": p["player"],
                "stat": p["stat"],
                "line": p["line"],
                "all_books": []
            }
        if p.get("over_price") is not None and p.get("under_price") is not None:
            groups[key]["all_books"].append({
                "book": p["bookmaker"],
                "over_price": p["over_price"],
                "under_price": p["under_price"]
            })

    result = []
    for key, group in groups.items():
        books = group["all_books"]
        if not books:
            continue

        try:
            evaluation = evaluate_pick(
                player_or_team=group["player"],
                market_type=group["stat"],
                side="over",
                line=group["line"],
                book_prices=books,
                game_time=group.get("game_time")
            )

            if not evaluation["passes_threshold"]:
                logger.debug(
                    f"[EV] Filtered: {group['player']} — "
                    f"{evaluation.get('rejection_reason')}"
                )
                evaluation["surfaced"] = False
            else:
                evaluation["surfaced"] = True

            # Line shop savings (backward compat)
            if books:
                best_price = max(b["over_price"] for b in books if b.get("over_price") is not None)
                worst_price = min(b["over_price"] for b in books if b.get("over_price") is not None)
                def to_imp(o):
                    return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)
                savings_pct = round((to_imp(worst_price) - to_imp(best_price)) * 100, 1)
            else:
                savings_pct = 0

            fp = evaluation.get("fair_probability") or 0.5
            result.append({
                **group,
                **evaluation,
                "no_vig_prob":         round(fp * 100, 1),
                "confidence_tier":     evaluation["confidence_tier"],
                "best_book":           evaluation["best_book"],
                "best_over_price":     evaluation["best_offered_odds"],
                "line_shop_savings_pct": savings_pct,
                "odds":                evaluation["best_offered_odds"],
                "bookmaker":           evaluation["best_book"],
                "enriched":            True
            })

        except Exception as e:
            logger.warning(f"[EV] evaluate_pick failed for {group['player']}: {e} — using fallback")
            from probability import no_vig_probability, get_confidence_tier
            best = max(books, key=lambda b: b["over_price"])
            sharpest = min(books, key=lambda b: (
                (100/(b["over_price"]+100) if b["over_price"]>0 else abs(b["over_price"])/(abs(b["over_price"])+100)) +
                (100/(b["under_price"]+100) if b["under_price"]>0 else abs(b["under_price"])/(abs(b["under_price"])+100))
            ))
            no_vig_prob = no_vig_probability(sharpest["over_price"], sharpest["under_price"])
            tier = get_confidence_tier(no_vig_prob)
            result.append({
                **group,
                "no_vig_prob":         no_vig_prob,
                "confidence_tier":     tier,
                "best_book":           best["book"],
                "best_over_price":     best["over_price"],
                "odds":                best["over_price"],
                "bookmaker":           best["book"],
                "surfaced":            True,
                "passes_threshold":    True,
                "enriched":            True
            })

    print(f"[INFO] Grouped {len(props)} raw props into {len(result)} unique player props")
    return result

def enrich_prop(prop):
    """Enrich a single prop with contextual and fantasy hit rates - with robust error handling"""
    try:
        # Get contextual hit rate with fallback
        contextual = None
        try:
            contextual = get_contextual_hit_rate(
                prop["player"], 
                stat_type=prop["stat"], 
                threshold=prop["line"]
            )
        except Exception as e:
            print(f"[WARN] Contextual hit rate error for {prop['player']}: {e}")
            contextual = {
                "player": prop["player"],
                "stat": prop["stat"],
                "threshold": prop["line"],
                "hit_rate": None,
                "confidence": "Unknown",
                "error": f"Contextual calculation failed: {str(e)}"
            }
        
        # Ensure we always have a contextual object
        if not contextual or contextual.get("error"):
            contextual = {
                "player": prop["player"],
                "stat": prop["stat"],
                "threshold": prop["line"],
                "hit_rate": 0.30,  # Default fallback
                "confidence": "Low",
                "note": "Using fallback hit rate"
            }
        
        # Enhanced Enrichment: Apply pro-level betting context multipliers
        try:
            from enrichment import (apply_park_factor, get_recent_form_multiplier, 
                                  get_bullpen_fatigue_multiplier, get_lineup_position_multiplier,
                                  get_player_id)
            
            base_hit_rate = contextual.get("hit_rate", 0.30)
            enhanced_multiplier = 1.0
            enhancement_factors = []
            
            # Park Factor Analysis
            stadium = prop.get("venue", "")
            if stadium:
                park_multiplier = apply_park_factor(prop, stadium)
                if park_multiplier != 1.0:
                    enhanced_multiplier *= park_multiplier
                    enhancement_factors.append(f"Park: {park_multiplier:.2f}")
            
            # Recent Form Analysis
            player_id = get_player_id(prop["player"])
            if player_id:
                form_multiplier = get_recent_form_multiplier(player_id, prop["stat"])
                if form_multiplier != 1.0:
                    enhanced_multiplier *= form_multiplier
                    enhancement_factors.append(f"Form: {form_multiplier:.2f}")
            
            # Bullpen Fatigue Context
            opponent_team = prop.get("opponent_team", "")
            if opponent_team:
                bullpen_multiplier = get_bullpen_fatigue_multiplier(opponent_team)
                if bullpen_multiplier != 1.0:
                    enhanced_multiplier *= bullpen_multiplier
                    enhancement_factors.append(f"Bullpen: {bullpen_multiplier:.2f}")
            
            # Lineup Position Influence
            lineup_multiplier = get_lineup_position_multiplier(prop["player"])
            if lineup_multiplier != 1.0:
                enhanced_multiplier *= lineup_multiplier
                enhancement_factors.append(f"Lineup: {lineup_multiplier:.2f}")
            
            # Apply enhanced multiplier to hit rate (cap between 0.05 and 0.95)
            if isinstance(base_hit_rate, (int, float)) and base_hit_rate > 0:
                enhanced_hit_rate = min(0.95, max(0.05, base_hit_rate * enhanced_multiplier))
                
                # Update contextual data with enhanced analysis
                contextual["enhanced_hit_rate"] = round(enhanced_hit_rate, 3)
                contextual["enhancement_multiplier"] = round(enhanced_multiplier, 3)
                contextual["enhancement_factors"] = enhancement_factors
                contextual["original_hit_rate"] = base_hit_rate
                
                if enhancement_factors:
                    print(f"[ENHANCED] {prop['player']}: {base_hit_rate:.2f} -> {enhanced_hit_rate:.2f} ({', '.join(enhancement_factors)})")
            
        except Exception as enhancement_error:
            print(f"[DEBUG] Enhanced enrichment failed for {prop['player']}: {enhancement_error}")
            # Continue with basic contextual data if enhancement fails
        
        # Get fantasy hit rate with fallback
        fantasy = None
        try:
            fantasy = get_fantasy_hit_rate(prop["player"], threshold=prop["line"])
        except Exception as e:
            print(f"[WARN] Fantasy hit rate error for {prop['player']}: {e}")
            fantasy = {
                "player": prop["player"],
                "threshold": prop["line"],
                "hit_rate": 0.35,  # Default fallback
                "confidence": "Low",
                "note": "Using fallback fantasy rate"
            }
        
        # Ensure we always have a fantasy object
        if not fantasy:
            fantasy = {
                "player": prop["player"],
                "threshold": prop["line"],
                "hit_rate": 0.35,  # Default fallback
                "confidence": "Low",
                "note": "Using fallback fantasy rate"
            }
        
        # Return enriched prop
        return {
            **prop,
            "contextual_hit_rate": contextual,
            "fantasy_hit_rate": fantasy,
            "enriched": True
        }
    except Exception as e:
        print(f"[ERROR] Failed to enrich prop for {prop.get('player', 'Unknown')}: {e}")
        # Return original prop with error indication
        return {
            **prop,
            "contextual_hit_rate": {
                "hit_rate": 0.30,
                "confidence": "Low",
                "error": "Enrichment failed"
            },
            "fantasy_hit_rate": {
                "hit_rate": 0.35,
                "confidence": "Low",
                "error": "Enrichment failed"
            },
            "enriched": False,
            "error": str(e)
        }

def enrich_player_props(props):
    """Enrich player props with contextual and fantasy hit rates using parallel processing"""
    if not props:
        return []
    
    print(f"[INFO] Starting enrichment for {len(props)} props")
    
    # Use ThreadPoolExecutor for parallel processing
    with ThreadPoolExecutor(max_workers=10) as executor:
        enriched_props = list(executor.map(enrich_prop, props))
    
    # Count successful enrichments
    successful_enrichments = sum(1 for prop in enriched_props if prop.get("enriched", False))
    print(f"[INFO] Enrichment complete: {successful_enrichments}/{len(props)} props successfully enriched")
    
    return enriched_props