# nfl_game_enrichment.py
from typing import Dict, List, Any, Optional, Tuple
import os, json, time, logging, urllib.request, urllib.parse

logger = logging.getLogger("nfl_enrich")
logger.setLevel(logging.INFO)

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
    """Return no-vig p(over) from raw implied probs."""
    if p_over_raw is None and p_under_raw is None:
        return 0.50
    if p_over_raw is None:
        return 1.0 - (p_under_raw or 0.5)
    if p_under_raw is None:
        return p_over_raw
    denom = p_over_raw + (1.0 - p_under_raw)
    if denom <= 1e-9:
        return 0.50
    return p_over_raw / denom

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

# -------------------- Environment from totals / moneyline / spread --------------------
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
    """Keys: '<Away> @ <Home>'"""
    env: Dict[str, Dict[str, Any]] = {}
    for ev in (events or []):
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

        fav_side = _fav_side_from_ml(home_ml, away_ml)
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
            "favored_side": fav_side,         # 'home' | 'away' | None
            "environment": _classify_env(total_points, over_am, under_am),
        }
    return env

# -------------------- API-Sports (Players + Statistics) --------------------
# Use American-Football base; switch to v1.nfl.api-sports.io if your plan requires it.
_API_PLAYERS = "https://v1.american-football.api-sports.io/players"
_API_STATS   = "https://v1.american-football.api-sports.io/players/statistics"

def _api_key() -> Optional[str]:
    k = (
        os.getenv("APISPORTS_KEY")
        or os.getenv("APISPORTS_KEY")
        or os.getenv("APISPORTS_KEY")
        or ""
    ).strip()   # <- removes stray newline/space that caused "Invalid header value ...\n"
    return k or None

def _api_headers() -> Optional[Dict[str, str]]:
    k = _api_key()
    return {"x-apisports-key": k} if k else None

# small in-process cache: {cache_key: (timestamp, bump)}
_FORM_CACHE: Dict[str, Tuple[float, float]] = {}

def _http_get_json(url: str, headers: Dict[str, str], timeout: int = 6) -> Optional[Dict[str, Any]]:
    logger.info("[NFL] API-Sports GET %s", url)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        logger.error("[NFL] API-Sports GET failed %s :: %s", url, e)
        return None

def _find_player_id(player_name: str) -> Optional[int]:
    try:
        hdrs = _api_headers()
        if not hdrs or not player_name:
            return None
        q = urllib.parse.urlencode({"search": player_name})
        data = _http_get_json(f"{_API_PLAYERS}?{q}", hdrs, timeout=6)
        resp = data.get("response") if data else None
        if not resp:
            return None
        pid = (resp[0].get("player") or {}).get("id")
        return int(pid) if pid is not None else None
    except Exception as e:
        logger.debug("_find_player_id error for %s: %s", player_name, e)
        return None

def _fetch_last5_bump(player_name: str, stat_key: str, season: Optional[int]) -> float:
    """Tiny ±0.04 bump from last-5 form using Players Statistics."""
    if os.getenv("DISABLE_NFL_FORM") == "1":
        return 0.0
    try:
        hdrs = _api_headers()
        if not hdrs:
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
        logger.info("[NFL] fetch last5 for id=%s stat=%s season=%s", pid, stat_key, season)
        data = _http_get_json(f"{_API_STATS}?{q}", hdrs, timeout=8)
        resp = data.get("response") if data else None
        if not resp:
            _FORM_CACHE[cache_key] = (now, 0.0)
            return 0.0

        games: List[Dict[str, Any]] = []
        for block in resp:
            st = block.get("statistics") or {}
            games.append({
                "receptions":  (st.get("receptions") or {}).get("receptions"),
                "targets":     (st.get("receptions") or {}).get("targets"),
                "rec_yards":   (st.get("receiving")  or {}).get("yards"),
                "rush_att":    (st.get("rushing")    or {}).get("attempts"),
                "rush_yards":  (st.get("rushing")    or {}).get("yards"),
                "pass_att":    (st.get("passing")    or {}).get("att"),
                "pass_yards":  (st.get("passing")    or {}).get("yards"),
                "pass_tds":    (st.get("passing")    or {}).get("td"),
            })

        last5 = games[-5:] if len(games) > 5 else games

        def avg(k: str) -> Optional[float]:
            vals = [float(g[k]) for g in last5 if g.get(k) is not None]
            return sum(vals) / len(vals) if vals else None

        bump = 0.0
        k = (stat_key or "").lower()
        if "receptions" in k:
            tpg = avg("targets") or 0.0
            rpg = avg("receptions") or 0.0
            bump = +0.03 if (tpg >= 8 or rpg >= 6) else (-0.02 if (tpg <= 4 or rpg <= 3) else 0.0)
        elif "reception_yds" in k or "rec_yds" in k:
            ryg = avg("rec_yards") or 0.0
            bump = +0.03 if ryg >= 70 else (-0.02 if ryg <= 35 else 0.0)
        elif "rush_yds" in k:
            rapg = avg("rush_att") or 0.0
            ryg  = avg("rush_yards") or 0.0
            bump = +0.03 if (rapg >= 16 or ryg >= 70) else (-0.02 if (rapg <= 8 or ryg <= 35) else 0.0)
        elif "pass_yds" in k:
            pay = avg("pass_yards") or 0.0
            paa = avg("pass_att") or 0.0
            bump = +0.02 if (pay >= 275 or paa >= 36) else (-0.02 if (pay <= 210 or paa <= 28) else 0.0)
        elif "pass_tds" in k:
            ptd = avg("pass_tds") or 0.0
            bump = +0.02 if ptd >= 2.2 else (-0.02 if ptd <= 1.0 else 0.0)

        bump = clamp(bump, -0.04, 0.04)
        _FORM_CACHE[cache_key] = (now, bump)
        return bump

    except Exception as e:
        logger.debug("_fetch_last5_bump error for %s: %s", player_name, e)
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
        adj += 0.01  # tiny bias for favored game script
    return adj

def enrich_nfl_props_with_context(props: List[Dict], env_map: Dict[str, Dict[str, Any]]) -> List[Dict]:
    season_env = os.getenv("NFL_SEASON") or os.getenv("APISPORTS_NFL_SEASON")
    try:
        season = int(season_env) if season_env else None
    except Exception:
        season = None

    out: List[Dict] = []
    key_map = {
        "player_receptions": "receptions",
        "player_reception_yds": "rec_yds",
        "player_receiving_yds": "rec_yds",
        "player_rush_yds": "rush_yds",
        "player_pass_yds": "pass_yds",
        "player_pass_tds": "pass_tds",
    }

    for p in (props or []):
        raw_key = (p.get("stat") or p.get("market") or "").lower()
        stat = key_map.get(raw_key, raw_key)

        matchup = p.get("matchup", "")
        env = env_map.get(matchup, {}) or {}
        bucket = env.get("environment", "Neutral")
        fav_side = env.get("favored_side")

        # base from no-vig Over/Under
        p_over_raw = american_to_prob(p.get("over_odds"))
        p_under_raw = american_to_prob(p.get("under_odds"))
        base_over = novig_two_sided(p_over_raw, p_under_raw)
        base = max(base_over, 1.0 - base_over)

        # bumps
        env_bump  = _env_adjust(stat, bucket, fav_side)
        form_bump = _fetch_last5_bump(p.get("player") or p.get("player_name") or "", stat, season)

        adj = clamp(base + env_bump + form_bump, 0.30, 0.90)

        # confidence
        conf = "High" if adj >= 0.66 else ("Medium" if adj >= 0.56 else "Low")

        fav_abbr = p.get("home_abbr") if fav_side == "home" else (p.get("away_abbr") if fav_side == "away" else None)

        p["context"] = {
            "environment": bucket,
            "favored_side": fav_side,
            "total_points": env.get("total_points"),
            "fav_team_abbr": fav_abbr,
            # Optional: expose components for debugging
            # "components": {"base": round(base,3), "env": round(env_bump,3), "form": round(form_bump,3)}
        }
        p["hit_probability"] = round(adj, 3)
        p["confidence"] = conf
        out.append(p)

    return out
