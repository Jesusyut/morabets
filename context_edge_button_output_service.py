"""
Backend service helpers for cached Context Edge quick-button outputs.

This preserves the current dashboard quick-button labels and prompt intent,
but does not call AI or wire anything into the frontend yet.
"""

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo


PHOENIX_TZ = ZoneInfo("America/Phoenix")

OUTPUT_CONFIGS = {
    "mlb_value": {
        "label": "⚾ MLB value",
        "prompt": "Find the best MLB value edges across props, moneylines, spreads, and totals. Prioritize the highest context edge gap.",
    },
    "soccer_value": {
        "label": "⚽ Soccer value",
        "prompt": "Find the best soccer value edges across moneylines, spreads/handicaps, totals, and props if available. Prioritize the highest context edge gap.",
    },
    "plus_money": {
        "label": "💰 Plus money",
        "prompt": (
            "Find true Plus Money edges, not normal safe no-vig board plays. "
            "Use the no-vig board as the starting signal, then look for upside markets where matchup, environment, and context support a higher payout. "
            "For player props, if a player is strongly supported by no-vig plus context for an offensive day, do not simply repeat a heavy negative odds play like over 0.5 hits at -200. "
            "Ask whether today's context supports upgrading to a higher-payout market such as over 1.5 hits, over 1.5 total bases, runs + RBIs, 0.5 home runs, or available alternate bases/hits/RBI markets. "
            "Across sports, prioritize underdog moneylines with real context support, alternate spreads or handicaps with better payout, and player/team upside props where no-vig plus matchup context points toward a ceiling outcome. "
            "Avoid reckless longshots. Only suggest plus-money or near-plus-money plays when no-vig math gives a strong starting point, the game environment or matchup supports upside, and the contextual layer improves or confirms the edge. "
            "Do not label heavy negative odds as Plus Money. If no true plus-money edge exists, say PASS / no qualified plus-money edge today. "
            "Use language like: No-vig signal identified the player/team. Context supports upgrading to the higher-payout market."
        ),
    },
    "nfl_value": {
        "label": "🏈 NFL value",
        "prompt": "If NFL board exists, find the best NFL value edges across spreads, moneylines, totals, and props. If no NFL board exists, return \"NFL scan not active yet\" cleanly.",
    },
}


def get_output_configs() -> dict[str, dict[str, str]]:
    """Return a defensive copy of the configured quick-button outputs."""
    return deepcopy(OUTPUT_CONFIGS)


def get_output_config(output_key: str) -> dict[str, str]:
    key = (output_key or "").strip().lower()
    if key not in OUTPUT_CONFIGS:
        raise KeyError(f"Unknown Context Edge output key: {output_key}")
    return deepcopy(OUTPUT_CONFIGS[key])


def build_board_hash(board: Any) -> str:
    board_json = json.dumps(
        board or [],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(board_json.encode("utf-8")).hexdigest()


def get_phoenix_run_window(now: Optional[datetime] = None) -> str:
    """
    Return the active generation window for Phoenix time.

    The morning window covers the day before 3:00 PM Phoenix time; the
    afternoon window starts at 3:00 PM Phoenix time.
    """
    if now is None:
        phoenix_now = datetime.now(PHOENIX_TZ)
    elif now.tzinfo is None:
        phoenix_now = now.replace(tzinfo=PHOENIX_TZ)
    else:
        phoenix_now = now.astimezone(PHOENIX_TZ)

    if phoenix_now.hour >= 15:
        return "afternoon"
    return "morning"


def build_button_output_payload(output_key: str, board: Any) -> dict[str, Any]:
    config = get_output_config(output_key)
    return {
        "output_key": (output_key or "").strip().lower(),
        "label": config["label"],
        "prompt": config["prompt"],
        "board_hash": build_board_hash(board),
        "board": board or [],
    }
