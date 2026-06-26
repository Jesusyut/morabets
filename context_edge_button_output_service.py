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
        "prompt": "best value bets today MLB — include moneylines, spreads, and player props",
    },
    "soccer_value": {
        "label": "⚽ Soccer value",
        "prompt": "best value bets today Soccer World Cup — include moneylines and totals",
    },
    "top_5_plays": {
        "label": "⚡ Top 5 plays",
        "prompt": "top 5 plays today across all sports — mix of player props, moneylines, and spreads",
    },
    "plus_money": {
        "label": "💰 Plus money",
        "prompt": "best plus money plays today — any sport, odds better than +100",
    },
    "mlb_lines": {
        "label": "📊 MLB lines",
        "prompt": "best MLB moneyline and run line bets today based on pitching matchups and park factors",
    },
    "world_cup": {
        "label": "🏆 World Cup",
        "prompt": "World Cup match result and total goals bets today — best context edges",
    },
    "game_totals": {
        "label": "📈 Game totals",
        "prompt": "best high scoring game total plays today — overs with park factor and pitching context",
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
