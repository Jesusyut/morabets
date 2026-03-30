import os, sys, requests, json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, Tuple

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "icehockey_nhl"

PRIMARY_BOOKS = ["draftkings", "fanduel", "betmgm"]

DEFAULT_MARKETS = [
    "player_points",
    "player_power_play_points",
    "player_shots_on_goal",
    "player_blocked_shots",
    "player_assists",
    "player_goals",
]

def _debug_log(tag: str, url: str, params: dict):
    try:
        print(f"[NHL][{tag}] GET {url} params={json.dumps(params, sort_keys=True)}")
    except Exception:
        pass

def _get(url: str, params: Dict[str, Any], timeout: int = 20) -> Tuple[Any, Dict[str, str]]:
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY is not set")
    q = {**params, "apiKey": ODDS_API_KEY}
    r = requests.get(url, params=q, timeout=timeout)
    hdrs = {k.lower(): v for k, v in r.headers.items()}
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Odds API error {r.status_code} at {url}: {detail}")
    try:
        return r.json(), hdrs
    except Exception as e:
        raise RuntimeError(f"Invalid JSON at {url}: {e}")

def _log_headers(tag: str, hdrs: Dict[str, str]):
    rem = hdrs.get("x-requests-remaining")
    used = hdrs.get("x-requests-used")
    lim = hdrs.get("x-requests-limit")
    if rem or used or lim:
        print(f"[NHL][{tag}] usage remaining={rem} used={used} limit={lim}", file=sys.stderr)

def _list_events(hours_ahead: int = 48) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours_ahead)
    ev, hdrs = _get(
        f"{BASE}/sports/{SPORT_KEY}/events",
        {
            "commenceTimeFrom": now.replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            "commenceTimeTo": end.replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            "regions": "us",
            "oddsFormat": "american",
        },
    )
    _log_headers("events", hdrs)
    return ev if isinstance(ev, list) else []

def _event_odds(event_id: str, markets: List[str]) -> Dict[str, Any]:
    url = f"{BASE}/sports/{SPORT_KEY}/events/{event_id}/odds"
    params = {
        "regions": "us",
        "oddsFormat": "american",
        "markets": ",".join(markets),
    }
    _debug_log("event-odds", url, params)
    data, hdrs = _get(url, params)
    _log_headers(f"event-{event_id}", hdrs)
    payloads = data if isinstance(data, list) else [data]
    for p in payloads:
        if isinstance(p, dict) and p.get("bookmakers"):
            return p
    return payloads[0] if payloads else {}

def fetch_nhl_props(
    markets: Optional[List[str]] = None,
    hours_ahead: int = 48,
) -> List[Dict[str, Any]]:
    """
    Fetch NHL player props from the Odds API. Returns a flat list of raw prop dicts
    with over_price, under_price, player, stat, line, bookmaker fields.
    """
    mkts = markets or DEFAULT_MARKETS
    events = _list_events(hours_ahead)
    props: List[Dict[str, Any]] = []

    for ev in events:
        ev_id = ev.get("id")
        if not ev_id:
            continue
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        try:
            event_data = _event_odds(ev_id, mkts)
        except RuntimeError as e:
            print(f"[NHL] Event {ev_id} ({away} @ {home}) failed: {e}")
            continue

        for book in event_data.get("bookmakers", []):
            book_title = book.get("title", "Unknown")
            if book_title not in {"DraftKings", "FanDuel", "BetMGM"}:
                continue
            for market in book.get("markets", []):
                stat = market.get("key")
                player_outcomes: Dict[str, Dict] = {}
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
                            "odds": over_price,
                            "bookmaker": book_title,
                            "home_team": home,
                            "away_team": away,
                            "sport": "nhl",
                        })

    print(f"[NHL] fetch_nhl_props: {len(props)} raw props from {len(events)} events")
    return props

def get_nhl_game_environment_map(hours_ahead: int = 48) -> Dict[str, Dict[str, Any]]:
    """Return environment map keyed by 'AWAY @ HOME'"""
    try:
        from team_abbreviations import TEAM_ABBREVIATIONS
    except ImportError:
        TEAM_ABBREVIATIONS = {}

    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours_ahead)
    try:
        data, hdrs = _get(
            f"{BASE}/sports/{SPORT_KEY}/odds",
            {
                "regions": "us",
                "oddsFormat": "american",
                "dateFormat": "iso",
                "markets": "h2h,totals",
                "commenceTimeFrom": now.replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
                "commenceTimeTo": end.replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            },
        )
        _log_headers("env", hdrs)
    except Exception as e:
        print(f"[NHL] get_nhl_game_environment_map error: {e}")
        return {}

    env_map: Dict[str, Dict[str, Any]] = {}
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

        # NHL totals: high scoring = >6.5, low = <5.5
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

if __name__ == "__main__":
    try:
        props = fetch_nhl_props(hours_ahead=48)
        print("nhl raw props:", len(props))
    except Exception as e:
        print(f"Error: {e}")
