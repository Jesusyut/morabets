# nfl_game_enrichment.py
"""
NFL Game Context Enrichment Module
Mirrors the MLB enrichment shape enough for UI parity.
- Classifies game environment from totals odds.
- Identifies favored team from moneylines.
- Assigns confidence/hit_probability heuristics by prop type.
"""

from typing import Dict, List, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

# --- Small helpers ---
def american_to_prob(american: Optional[int]) -> Optional[float]:
    if american is None: return None
    try:
        a = int(american)
    except Exception:
        return None
    if a < 0:  # favorite
        return (-a) / ((-a) + 100)
    return 100 / (a + 100)

def classify_game_environment(total_points: Optional[float],
                              over_american: Optional[int],
                              under_american: Optional[int]) -> str:
    """Simple, MLB-like bucket: High / Low / Neutral."""
    if total_points is None:
        return "Neutral"
    # relaxed demo thresholds to ensure labels show up
    if total_points >= 47.5 or (over_american is not None and over_american <= -115):
        return "High Scoring"
    if total_points <= 40.5 or (under_american is not None and under_american <= -115):
        return "Low Scoring"
    return "Neutral"

def favored_team_from_moneylines(home_ml: Optional[int], away_ml: Optional[int]) -> Optional[str]:
    """Return 'home' or 'away' if we can infer a favorite."""
    ph = american_to_prob(home_ml)
    pa = american_to_prob(away_ml)
    if ph is None or pa is None:
        return None
    return "home" if ph > pa else "away"

# --- Public API used by app.py ---
def build_nfl_environment_map(events: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """
    Build a per-matchup environment map from the same `events` object
    used to make props (no extra API calls).
    Keys match your backend rows: 'AWY @ HOME' using team names.
    """
    env = {}
    for ev in events or []:
        home = (ev.get("home_team") or "").strip()
        away = (ev.get("away_team") or "").strip()
        if not home or not away:
            continue
        # search books for game total and moneyline markets
        total_points = None
        over_am = None
        under_am = None
        home_ml = None
        away_ml = None

        for bk in (ev.get("bookmakers") or []):
            for mk in (bk.get("markets") or []):
                key = (mk.get("key") or "").lower()
                if key in ("totals", "game_total", "total_points"):
                    # expect two outcomes: Over/Under
                    for oc in (mk.get("outcomes") or []):
                        name = (oc.get("name") or "").lower()
                        if "over" in name:
                            over_am = oc.get("price")
                            total_points = oc.get("point", total_points)
                        elif "under" in name:
                            under_am = oc.get("price")
                            total_points = oc.get("point", total_points)
                elif key in ("h2h", "moneyline"):
                    for oc in (mk.get("outcomes") or []):
                        team_name = (oc.get("name") or "").strip()
                        if team_name == home:
                            home_ml = oc.get("price")
                        elif team_name == away:
                            away_ml = oc.get("price")

        env_key = f"{away} @ {home}"
        env_class = classify_game_environment(total_points, over_am, under_am)
        favored_side = favored_team_from_moneylines(home_ml, away_ml)
        env[env_key] = {
            "total_points": total_points,
            "over_american": over_am,
            "under_american": under_am,
            "favored_side": favored_side,  # 'home' / 'away' / None
            "environment": env_class
        }
    return env

def enrich_nfl_props_with_context(props: List[Dict], env_map: Dict[str, Dict[str, Any]]) -> List[Dict]:
    """
    Attach context/confidence/hit_probability using the env_map.
    Heuristics (keep simple & fast):
      - High Scoring: boost OVER-ish props (pass_yds, pass_tds, rec_yds, receptions, rush_yds).
      - Low Scoring: boost UNDER-ish props for the same markets.
      - Neutral: keep Medium ~0.50.
    """
    out = []
    for p in props or []:
        matchup = p.get("matchup")  # "AWAY @ HOME" using full team names
        env = env_map.get(matchup, {})
        bucket = env.get("environment", "Neutral")
        stat = (p.get("stat") or p.get("market") or "").lower()

        # Decide direction tendency by stat type (no side selection, just confidence)
        is_offense_yardage = any(s in stat for s in [
            "pass_yds", "rec_yds", "reception_yds", "receptions", "rush_yds", "pass_tds", "rush_tds", "reception_tds"
        ])

        # Default
        confidence = "Medium"
        hit_prob = 0.50

        if bucket == "High Scoring" and is_offense_yardage:
            confidence = "High"
            hit_prob = 0.65
        elif bucket == "Low Scoring" and is_offense_yardage:
            confidence = "Low"
            hit_prob = 0.40
        else:
            confidence = "Medium"
            hit_prob = 0.50

        p["context"] = {
            "environment": bucket,
            "favored_side": env.get("favored_side"),  # can be used later for FAV/DOG badge
            "total_points": env.get("total_points"),
        }
        p["confidence"] = confidence
        p["hit_probability"] = hit_prob
        out.append(p)
    return out
