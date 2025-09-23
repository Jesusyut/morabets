# nfl_game_enrichment.py
from typing import Dict, List, Any, Optional, Tuple
import logging, os, json, urllib.request

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

# ----------------------- Optional: API-Sports form -----------------------

_API_SPORTS_URL = "https://v1.american-football.api-sports.io/players"

def _api_sports_headers() -> Optional[Dict[str, str]]:
    key = os.getenv("API_SPORTS_KEY") or os.getenv("API_SPORTS_NFL_KEY")
    if not key:
        return None
    return {"x-apisports-key": key}

def _fetch_api_sports_form(player_name: str) -> Optional[Dict[str, Any]]:
    """
    Very lean: last 5 stats for a player if key is present; otherwise None.
    We do not couple to IDs—just a name query best-effort.
    """
    hdrs = _api_sports_headers()
    if not hdrs:
        return None
    try:
        q = urllib.parse.urlencode({"search": player_name})
        req = urllib.request.Request(f"{_API_SPORTS_URL}?{q}", headers=hdrs)
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        # You can refine how you aggregate; here we just return the raw
        return data
    except Exception as e:
        logger.debug(f"API-Sports form fetch failed for {player_name}: {e}")
        return None

def _form_adjust(player_name: str, stat_key: str) -> float:
    """
    Translate API-Sports last-5 into a tiny +/- bump.
    Keep it very conservative to avoid overfitting.
    """
    data = _fetch_api_sports_form(player_name)
    if not data:
        return 0.0
    # TODO: parse last-5 for specific stat types.
    # For now, keep neutral until you wire exact fields.
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

    # totals-driven bias
    if env_bucket == "High Scoring" and offensey:
        adj += 0.05
    elif env_bucket == "Low Scoring" and offensey:
        adj -= 0.05

    # small global nudge for favored team (matchup-level; per-player side unknown)
    # this keeps it lean—later you can infer player team to make it per-card.
    if fav_side is not None:
        adj += 0.01  # tiny bias toward favored context

    return adj

def enrich_nfl_props_with_context(props: List[Dict], env_map: Dict[str, Dict[str, Any]]) -> List[Dict]:
    """
    Compute:
      - hit_probability: no-vig base from book + environment + (optional) form
      - confidence: High / Medium / Low using thresholds suited for no-vig
      - context: environment, favored_side, total_points, fav_team_abbr
    """
    out: List[Dict] = []
    for p in props or []:
        stat = (p.get("stat") or p.get("market") or "").lower()
        matchup = p.get("matchup", "")
        env = env_map.get(matchup, {}) or {}
        bucket = env.get("environment", "Neutral")
        fav_side = env.get("favored_side")

        # base from no-vig p(over)
        p_over_raw = american_to_prob(p.get("over_odds"))
        p_under_raw = american_to_prob(p.get("under_odds"))
        p_over_novig = novig_two_sided(p_over_raw, p_under_raw)

        # Display as probability that the more likely side hits
        base = max(p_over_novig, 1.0 - p_over_novig)

        # Environment + (optional) player-form nudges
        env_bump  = _env_adjust(stat, bucket, fav_side)
        form_bump = _form_adjust(p.get("player") or p.get("player_name") or "", stat)
        adj = clamp(base + env_bump + form_bump, 0.30, 0.90)

        # Confidence cutoffs (tuned for no-vig base)
        if   adj >= 0.66:
            conf = "High"
        elif adj >= 0.56:
            conf = "Medium"
        else:
            conf = "Low"

        # matchup-level favored badge (uses team abbreviations already on the row)
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

