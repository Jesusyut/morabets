# nfl_game_enrichment.py
from typing import Dict, List, Any, Optional, Tuple
import logging, os, json, urllib.request
import urllib.request
import urllib.parse   # <-- this line is critical

logger = logging.getLogger(__name__)

# ------------------------ Odds helpers ------------------------

def american_to_prob(american: Optional[int]) -> Optional[float]:
    if american is None:
        return None
    try:
        a = int(american)
    except Exception:
        return None
    if a < 0:
        return (-a) / ((-a) + 100)
    return 100 / (a + 100)

def novig_two_sided(p_over_raw: Optional[float], p_under_raw: Optional[float]) -> float:
    """
    Convert raw implied probs for over/under into a single no-vig p(over).
    If only one side is present, fall back to the other’s complement.
    """
    if p_over_raw is None and p_under_raw is None:
        return 0.50
    if p_over_raw is None:
        return 1.0 - (p_under_raw or 0.5)
    if p_under_raw is None:
        return p_over_raw

    # Remove vig by normalizing the two sides to sum to 1
    # p_over_raw + (1 - p_under_raw) is the book-implied sum for the two outcomes
    denom = p_over_raw + (1.0 - p_under_raw)
    if denom <= 1e-9:
        return 0.50
    return p_over_raw / denom

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

# -------------------- Env from totals / spread / ML --------------------

def _classify_env(total_points: Optional[float], over_am: Optional[int], under_am: Optional[int]) -> str:
    if total_points is None:
        return "Neutral"
    if total_points >= 47.5 or (over_am is not None and over_am <= -115):
        return "High Scoring"
    if total_points <= 40.5 or (under_am is not None and under_am <= -115):
        return "Low Scoring"
    return "Neutral"

def _fav_side_from_ml(home_ml: Optional[int], away_ml: Optional[int]) -> Optional[str]:
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    if ph is None or pa is None:
        return None
    return "home" if ph > pa else "away"

def build_nfl_environment_map(events: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """
    Keys: '<Away Name> @ <Home Name>'
    Values include: total_points, over/under prices, spreads, favored_side, environment.
    """
    env: Dict[str, Dict[str, Any]] = {}
    for ev in events or []:
        home = (ev.get("home_team") or "").strip()
        away = (ev.get("away_team") or "").strip()
        if not home or not away:
            continue

        total_points = None
        over_am = None
        under_am = None
        home_ml = None
        away_ml = None
        home_spread = None
        away_spread = None

        for bk in (ev.get("bookmakers") or []):
            for mk in (bk.get("markets") or []):
                key = (mk.get("key") or "").lower()

                if key in ("totals", "game_total", "total_points"):
                    for oc in (mk.get("outcomes") or []):
                        nm = (oc.get("name") or "").lower()
                        if "over" in nm:
                            over_am = oc.get("price")
                            total_points = oc.get("point", total_points)
                        elif "under" in nm:
                            under_am = oc.get("price")
                            total_points = oc.get("point", total_points)

                elif key in ("h2h", "moneyline"):
                    for oc in (mk.get("outcomes") or []):
                        t = (oc.get("name") or "").strip()
                        if t == home:
                            home_ml = oc.get("price")
                        elif t == away:
                            away_ml = oc.get("price")

                elif key in ("spreads", "spread"):
                    for oc in (mk.get("outcomes") or []):
                        t = (oc.get("name") or "").strip()
                        pt = oc.get("point")
                        if t == home:
                            home_spread = pt
                        elif t == away:
                            away_spread = pt

        # start with ML; if spreads exist, prefer the negative spread
        fav_side = _fav_side_from_ml(home_ml, away_ml)
        if home_spread is not None and away_spread is not None:
            if isinstance(home_spread, (int, float)) and isinstance(away_spread, (int, float)):
                if home_spread < 0:
                    fav_side = "home"
                elif away_spread < 0:
                    fav_side = "away"

        env[f"{away} @ {home}"] = {
            "total_points": total_points,
            "over_american": over_am,
            "under_american": under_am,
            "home_spread": home_spread,
            "away_spread": away_spread,
            "favored_side": fav_side,   # 'home' | 'away' | None
            "environment": _classify_env(total_points, over_am, under_am),
        }

    return env


_API_PLAYERS = "https://v1.american-football.api-sports.io/players"
_API_STATS   = "https://v1.american-football.api-sports.io/players/statistics"

def _api_key() -> Optional[str]:
    return os.getenv("API_SPORTS_KEY") or os.getenv("API_SPORTS_NFL_KEY") or os.getenv("APISPORTS_KEY")

def _api_headers() -> Optional[Dict[str, str]]:
    k = _api_key()
    return {"x-apisports-key": k} if k else None

# simple in-process cache to avoid hammering
_FORM_CACHE: Dict[str, Tuple[float, float]] = {}  # {cache_key: (ts, bump)}

def _http_get_json(url: str, headers: Dict[str, str], timeout: int = 5) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"API-Sports GET failed: {url} -> {e}")
        return None

def _find_player_id(player_name: str) -> Optional[int]:
    try:
        hdrs = _api_headers()
        if not hdrs or not player_name:
            return None
        q = urllib.parse.urlencode({"search": player_name})
        data = _http_get_json(f"{_API_PLAYERS}?{q}", hdrs, timeout=5)
        if not data or "response" not in data:
            return None
        resp = data["response"]
        if not resp:
            return None
        item = resp[0]
        pid = item.get("player", {}).get("id")
        return int(pid) if pid is not None else None
    except Exception as e:
        logger.debug(f"_find_player_id error for {player_name}: {e}")
        return None

def _fetch_last5_bump(player_name: str, stat_key: str, season: Optional[int]) -> float:
    # short-circuit if disabled
    if os.getenv("DISABLE_NFL_FORM") == "1":
        return 0.0
    try:
        if not _api_headers():
            return 0.0
        cache_key = f"{player_name}|{stat_key}|{season}"
        now = time.time()
        cached = _FORM_CACHE.get(cache_key)
        if cached and (now - cached[0]) < 3600:
            return cached[1]

        pid = _find_player_id(player_name)
        if not pid:
            _FORM_CACHE[cache_key] = (now, 0.0)
            return 0.0

        params = {"player": pid}
        if season:
            params["season"] = season
        q = urllib.parse.urlencode(params)
        data = _http_get_json(f"{_API_STATS}?{q}", _api_headers(), timeout=6)
        if not data or "response" not in data:
            _FORM_CACHE[cache_key] = (now, 0.0)
            return 0.0

    # The schema groups stats by team/league/games. We aggregate last 5 appearances.
    try:
        games = []
        for block in data["response"]:
            # block likely has "games" and position-specific "statistics"
            g = block.get("games") or {}
            st = block.get("statistics") or {}
            # Most endpoints split by positions; we try to gather common fields
            record = {
                "receptions":   st.get("receptions", {}).get("receptions"),
                "targets":      st.get("receptions", {}).get("targets"),
                "rec_yards":    st.get("receiving", {}).get("yards"),
                "rush_att":     st.get("rushing", {}).get("attempts"),
                "rush_yards":   st.get("rushing", {}).get("yards"),
                "pass_att":     st.get("passing", {}).get("att"),
                "pass_yards":   st.get("passing", {}).get("yards"),
                "pass_tds":     st.get("passing", {}).get("td"),
            }
            # guard against None → keep numeric or None
            games.append(record)

        # take last 5 records (end is latest on most APIs; if not, still fine)
        last5 = games[-5:] if len(games) > 5 else games

        def avg(key: str) -> Optional[float]:
            vals = [float(x[key]) for x in last5 if x.get(key) is not None]
            return sum(vals) / len(vals) if vals else None

        # derive a tiny bump based on relevant metric vs typical line scale
        bump = 0.0
        k = stat_key.lower()

        if "receptions" in k:
            # targets per game & receptions per game
            tpg = avg("targets") or 0.0
            rpg = avg("receptions") or 0.0
            # scale: 7+ targets ~= bullish; under 4 ~= bearish
            if tpg >= 8 or rpg >= 6:
                bump = +0.03
            elif tpg <= 4 or rpg <= 3:
                bump = -0.02

        elif "reception_yds" in k or "rec_yds" in k:
            ryg = avg("rec_yards") or 0.0
            if ryg >= 70:
                bump = +0.03
            elif ryg <= 35:
                bump = -0.02

        elif "rush_yds" in k:
            rapg = avg("rush_att") or 0.0
            ryg = avg("rush_yards") or 0.0
            if rapg >= 16 or ryg >= 70:
                bump = +0.03
            elif rapg <= 8 or ryg <= 35:
                bump = -0.02

        elif "pass_yds" in k:
            pay = avg("pass_yards") or 0.0
            paa = avg("pass_att") or 0.0
            if pay >= 275 or paa >= 36:
                bump = +0.02
            elif pay <= 210 or paa <= 28:
                bump = -0.02

        elif "pass_tds" in k:
            ptd = avg("pass_tds") or 0.0
            if ptd >= 2.2:
                bump = +0.02
            elif ptd <= 1.0:
                bump = -0.02

        bump = clamp(bump, -0.04, 0.04)
        _FORM_CACHE[cache_key] = (now, bump)
        return bump

    except Exception as e:
        logger.debug(f"API-Sports parse error for {player_name}: {e}")
        _FORM_CACHE[cache_key] = (now, 0.0)
        return 0.0
# -------------------- Per-prop confidence & hit rate --------------------

_OFFENSE_KEYS = (
    "pass_yds", "pass_tds", "rec_yds", "reception_yds", "receptions",
    "rush_yds", "rush_tds", "reception_tds"
)

def _env_adjust(stat_key: str, env_bucket: str, fav_side: Optional[str]) -> float:
    k = stat_key.lower()
    offensey = any(s in k for s in _OFFENSE_KEYS)
    adj = 0.0
    if env_bucket == "High Scoring" and offensey:
        adj += 0.05
    elif env_bucket == "Low Scoring" and offensey:
        adj -= 0.05
    if fav_side is not None:
        adj += 0.01  # small bias toward favorite game script
    return adj

def enrich_nfl_props_with_context(props: List[Dict], env_map: Dict[str, Dict[str, Any]]) -> List[Dict]:
    season_env = os.getenv("NFL_SEASON") or os.getenv("APISPORTS_NFL_SEASON")
    try:
        season = int(season_env) if season_env else None
    except Exception:
        season = None

    out: List[Dict] = []
    for p in props or []:
        stat = (p.get("stat") or p.get("market") or "").lower()
        matchup = p.get("matchup", "")
        env = env_map.get(matchup, {}) or {}
        bucket = env.get("environment", "Neutral")
        fav_side = env.get("favored_side")

        # base from no-vig Over/Under
        p_over_raw = american_to_prob(p.get("over_odds"))
        p_under_raw = american_to_prob(p.get("under_odds"))
        p_over_novig = novig_two_sided(p_over_raw, p_under_raw)
        base = max(p_over_novig, 1.0 - p_over_novig)

        # bumps
        env_bump  = _env_adjust(stat, bucket, fav_side)
        form_bump = _fetch_last5_bump(p.get("player") or p.get("player_name") or "", stat, season)

        adj = clamp(base + env_bump + form_bump, 0.30, 0.90)

        # confidence cutoffs (tuned for no-vig + small bumps)
        if   adj >= 0.66:
            conf = "High"
        elif adj >= 0.56:
            conf = "Medium"
        else:
            conf = "Low"

        fav_abbr = p.get("home_abbr") if fav_side == "home" else (p.get("away_abbr") if fav_side == "away" else None)

        p["context"] = {
            "environment": bucket,
            "favored_side": fav_side,
            "total_points": env.get("total_points"),
            "fav_team_abbr": fav_abbr,
        }
        p["hit_probability"] = round(adj, 3)
        p["confidence"] = conf
        out.append(p)

    return out

