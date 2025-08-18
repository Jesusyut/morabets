# Tiered filtering: GREEN (strong), BLUE (neutral-but-good), hide RED (weak)
from typing import List, Dict, Any

# === Thresholds (tune safely without touching engine) ===
WIN_TRUE_STRONG       = 0.58  # Team favored
TOTAL_OVER_STRONG     = 0.58  # Game leans Over

WIN_TRUE_NEUTRAL_LO   = 0.47  # Neutral band for win prob
WIN_TRUE_NEUTRAL_HI   = 0.53
TOTAL_NEUTRAL_LO      = 0.48  # Neutral band for totals Over prob
TOTAL_NEUTRAL_HI      = 0.54

WIN_TRUE_WEAK         = 0.42  # Big dog threshold
TOTAL_UNDER_STRONG    = 0.58  # Heavy Under probability

ENGINE_OK             = 0.58  # Show neutral if engine >= this
ENGINE_STRONG         = 0.64  # Rescue weak contexts only if engine >= this

# Only show markets that reflect "position to do well"
MIN_PLAYER_MARKETS = {
    "player_hits","player_total_bases","player_runs","player_rbis",
    "player_home_runs","player_singles","player_doubles","player_triples",
    "player_walks","player_stolen_bases",
    "player_strikeouts"  # pitchers optional but included
}

def _tier_from_context(team_true_win, true_prob_over, engine_prob):
    # GREEN: strong context (favored or over-lean)
    if (team_true_win is not None and team_true_win >= WIN_TRUE_STRONG) or \
       (true_prob_over is not None and true_prob_over >= TOTAL_OVER_STRONG):
        return "GREEN", "Favored/Over-lean"

    # RED: weak context (big dog + heavy Under), unless rescued by very strong engine
    if (team_true_win is not None and team_true_win <= WIN_TRUE_WEAK) and \
       (true_prob_over is not None and (1 - true_prob_over) >= TOTAL_UNDER_STRONG):
        if engine_prob is not None and engine_prob >= ENGINE_STRONG:
            return "BLUE", "Weak game but strong player signal"
        return "RED", "Big dog & likely Under"

    # BLUE: neutral lanes require decent engine
    in_win_neutral = (team_true_win is not None and WIN_TRUE_NEUTRAL_LO <= team_true_win <= WIN_TRUE_NEUTRAL_HI)
    in_total_neutral = (true_prob_over is not None and TOTAL_NEUTRAL_LO <= true_prob_over <= TOTAL_NEUTRAL_HI)
    if in_win_neutral or in_total_neutral:
        if engine_prob is not None and engine_prob >= ENGINE_OK:
            return "BLUE", "Neutral game, player-driven edge"
        return "RED", "Neutral game, weak player signal"

    # If uncertain/missing, hide
    return "RED", "Insufficient context"

def _build_position_score(team_true_win, true_prob_over, prop_implied, engine_prob):
    # Transparent weighted score (engine matters more for neutral)
    tw = team_true_win or 0.5
    ov = true_prob_over or 0.5
    ip = prop_implied or 0.0
    eg = engine_prob or 0.0
    score = 0.35*tw + 0.35*ov + 0.10*ip + 0.20*eg
    if eg and ip and eg > ip:
        score += min(0.06, eg - ip)  # reward genuine edge
    return round(score, 4)

def _map_team_true_win(team: str, ctx: Dict[str, Any]):
    if not team or not ctx:
        return None
    # Exact match first
    if team == ctx.get("home_team"):
        return ctx.get("home_true_win")
    if team == ctx.get("away_team"):
        return ctx.get("away_true_win")
    # Fallback contains match (abbr in name, etc.)
    if team in (ctx.get("home_team") or ""):
        return ctx.get("home_true_win")
    if team in (ctx.get("away_team") or ""):
        return ctx.get("away_true_win")
    return None

def filter_positioned_props(all_props: List[Dict[str, Any]], event_context_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for prop in all_props or []:
        if prop.get("market") not in MIN_PLAYER_MARKETS:
            continue

        ctx = event_context_by_id.get(prop.get("event_id"))
        if not ctx:
            continue

        team_true_win = _map_team_true_win(prop.get("team"), ctx)
        true_prob_over = ctx.get("true_prob_over")
        prop_implied   = prop.get("implied_prob_from_odds")  # may be None
        engine_prob    = prop.get("engine_prob")             # your existing engine value

        tier, reason = _tier_from_context(team_true_win, true_prob_over, engine_prob)
        if tier == "RED":
            continue  # hide weak/unknown unless rescued in _tier_from_context

        score = _build_position_score(team_true_win, true_prob_over, prop_implied, engine_prob)

        out.append({
            **prop,
            "position_tier": tier,            # "GREEN" or "BLUE"
            "position_reason": reason,
            "position_context": {
                "favored_team": ctx.get("favored_team"),
                "team_true_win": team_true_win,
                "total_point": ctx.get("total_point"),
                "true_prob_over": true_prob_over,
                "engine_prob": engine_prob,
                "prop_implied": prop_implied
            },
            "position_score": score,
            "badges": list(filter(None, [
                "Favored Team" if (team_true_win or 0) >= WIN_TRUE_STRONG else None,
                (f"Over {ctx.get('total_point')}" if (true_prob_over or 0) >= TOTAL_OVER_STRONG and ctx.get("total_point") else None),
                ("Neutral—Player Edge" if tier == "BLUE" else None),
            ]))
        })

    # Sort: GREEN first, then BLUE; within each, by (engine − implied) then position_score desc
    def sort_key(p):
        tier_rank = 0 if p["position_tier"] == "GREEN" else 1
        eg = p["position_context"].get("engine_prob") or 0.0
        ip = p["position_context"].get("prop_implied") or 0.0
        edge = eg - ip
        return (tier_rank, -edge, -p["position_score"])

    out.sort(key=sort_key)
    return out 