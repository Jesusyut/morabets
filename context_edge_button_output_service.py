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
            "Run a context-first Plus Money upside scan. "
            "Use the no-vig board only as a loose starting map for strong players, strong teams, favorable offensive environments, high-scoring games, and matchup clusters worth researching. "
            "Do not require the exact plus-money market to already exist on the no-vig board, and do not simply repeat safe 0.5 hit props or other heavy negative odds board plays. "
            "Research and infer higher-payout alternatives from context. For MLB hitters with a strong 0.5 hit no-vig signal, inspect whether matchup and environment support over 1.5 total bases, over 1.5 hits, HR 0.5, RBI, runs + RBI, hit + run + RBI, or available team/player alternate markets. "
            "For teams, if a spread/run line has a strong probability signal, ask whether the team can win outright at plus money or whether an alternate spread/handicap offers a better payout with real context support. "
            "Prioritize paths of least resistance: high-scoring game labels, weak pitcher or defense, bullpen vulnerability, hitter-friendly park/weather, player form, role and lineup position, injury/travel/rest advantages, and team motivation/context. "
            "Prefer plus-money or near-plus-money plays. Avoid heavy negative odds unless briefly explaining why they are not Plus Money. "
            "If the board lacks alt markets, still use web/context research and the cached board context to identify likely upside alternatives. "
            "Do not write a long board-structure essay. If no qualified plus-money edge exists, give a short PASS, but first show the closest researched candidates. "
            "Output in a betting-desk style with: Pick, Market, Target price range, Why context supports upside, Risk, Verdict. "
            "Do not force reckless longshots; the play still needs a credible no-vig/context signal."
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
