# nfl_game_enrichment.py
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)

def novig_two_sided(p_over_raw: float | None, p_under_raw: float | None) -> float:
    """
    Convert raw implied probs for over/under into a single no-vig p(over).
    If only one side is present, fall back to the other’s complement.
    """
    if p_over_raw is None and p_under_raw is None:
        return 0.50
    if p_over_raw is None:
        return 1.0 - p_under_raw
    if p_under_raw is None:
        return p_over_raw

    # Remove vig by normalizing the two sides to sum to 1
    denom = p_over_raw + (1.0 - p_under_raw)
    if denom <= 1e-9:
        return 0.50
    return p_over_raw / denom

# ---------- odds helpers ----------
def american_to_prob(american: Optional[int]) -> Optional[float]:
    if american is None: return None
    try:
        a = int(american)
    except Exception:
        return None
    if a < 0:
        return (-a) / ((-a) + 100)
    return 100 / (a + 100)

def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))

# ---------- environment from totals + moneyline (no new API calls) ----------
def _classify_env(total_points: Optional[float], over_am: Optional[int], under_am: Optional[int]) -> str:
    if total_points is None:
        return "Neutral"
    # simple buckets
    if total_points >= 47.5 or (over_am is not None and over_am <= -115):
        return "High Scoring"
    if total_points <= 40.5 or (under_am is not None and under_am <= -115):
        return "Low Scoring"
    return "Neutral"

def _fav_side(home_ml: Optional[int], away_ml: Optional[int]) -> Optional[str]:
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    if ph is None or pa is None:
        return None
    return "home" if ph > pa else "away"

def build_nfl_environment_map(events: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """
    Keys: '<Away Name> @ <Home Name>'
    Values: {total_points, over_american, under_american, favored_side, environment}
    """
    env = {}
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

        for bk in (ev.get("bookmakers") or []):
            for mk in (bk.get("markets") or []):
                key = (mk.get("key") or "").lower()

                # totals market
                if key in ("totals", "game_total", "total_points"):
                    for oc in (mk.get("outcomes") or []):
                        nm = (oc.get("name") or "").lower()
                        if "over" in nm:
                            over_am = oc.get("price")
                            total_points = oc.get("point", total_points)
                        elif "under" in nm:
                            under_am = oc.get("price")
                            total_points = oc.get("point", total_points)

                # moneyline market (h2h)
                elif key in ("h2h", "moneyline"):
                    for oc in (mk.get("outcomes") or []):
                        t = (oc.get("name") or "").strip()
                        if t == home:
                            home_ml = oc.get("price")
                        elif t == away:
                            away_ml = oc.get("price")

        env[f"{away} @ {home}"] = {
            "total_points": total_points,
            "over_american": over_am,
            "under_american": under_am,
            "favored_side": _fav_side(home_ml, away_ml),   # 'home' | 'away' | None
            "environment": _classify_env(total_points, over_am, under_am),
        }

    return env

# ---------- per-prop confidence (lean, MLB-like) ----------
_OFFENSE_KEYS = (
    "pass_yds", "pass_tds", "rec_yds", "reception_yds", "receptions",
    "rush_yds", "rush_tds", "reception_tds"
)

def _env_adjust(stat_key: str, env_bucket: str) -> float:
    k = stat_key.lower()
    offensey = any(s in k for s in _OFFENSE_KEYS)

    # bias: high-scoring → OVER-friendly props more likely to hit in general
    if env_bucket == "High Scoring" and offensey:
        return 0.05   # +5% bump
    if env_bucket == "Low Scoring" and offensey:
        return -0.05  # -5% bump
    return 0.0

def enrich_nfl_props_with_context(props: List[Dict], env_map: Dict[str, Dict[str, Any]]) -> List[Dict]:
    """
    For each row, compute a hit_probability and confidence using:
      - no-vig-like base: implied probs from book odds (Over & Under)
      - environment bump from totals
    We keep it lean & deterministic. No new external calls.
    """
    out = []
    for p in props or []:
        stat = (p.get("stat") or p.get("market") or "").lower()
        matchup = p.get("matchup")
        env = env_map.get(matchup, {})
        bucket = env.get("environment", "Neutral")

        # base from books
        p_over = american_to_prob(p.get("over_odds"))
        p_under = american_to_prob(p.get("under_odds"))

        # if only one side present, treat missing as complement
        if p_over is None and p_under is None:
            base = 0.50
        elif p_over is None:
            base = 1.0 - (p_under or 0.5)
        elif p_under is None:
            base = (p_over or 0.5)
        else:
            # take the more likely side as "hit rate" for display
            base = max(p_over, 1.0 - p_under)

        # environment nudge
        adj = clamp(base + _env_adjust(stat, bucket), 0.30, 0.85)

        # confidence buckets similar to MLB UI cutoffs
        if adj >= 0.64:
            conf = "High"
        elif adj >= 0.56:
            conf = "Medium"
        else:
            conf = "Low"

        # attach
        p["context"] = {
            "environment": bucket,
            "favored_side": env.get("favored_side"),
            "total_points": env.get("total_points"),
        }
        p["hit_probability"] = round(adj, 3)
        p["confidence"] = conf

        out.append(p)
    return out

