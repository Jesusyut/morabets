"""Generate and store cached Context Edge outputs for quick buttons."""

import json
import os
from datetime import datetime
from typing import Any

from context_edge_button_output_service import (
    build_board_hash,
    get_output_config,
)
from context_edge_prompt_service import (
    CONTEXT_EDGE_MODEL,
    build_context_edge_system_prompt,
    call_context_edge_ai,
)
from supabase_backend import upsert_context_edge_button_output


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


def _report_date() -> str:
    return datetime.utcnow().date().isoformat()


def _load_context_edge_board() -> list[dict[str, Any]]:
    from app import load_board_for_context

    return load_board_for_context()


def _format_context_edge_board(board: list[dict[str, Any]]) -> str:
    if not board:
        return "No qualifying props on board today."

    guidance = (
        "Board selection guidance:\n"
        "- Prioritize paths of least resistance first.\n"
        "- If games or props are labeled High Scoring, inspect edge candidates there first.\n"
        "- Especially for Plus Money, look for plus-money plays where environment, matchup, and no-vig math already support the bet.\n"
        "- Do not force longshots.\n"
        "- Use contextual layering to strengthen an existing no-vig edge.\n"
        "- Best edge = no-vig math points one way, and context confirms or improves it.\n\n"
    )

    return guidance + "Complete cached no-vig board JSON:\n" + json.dumps(
        board,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        default=str,
    )


def _has_sport_board(board: list[dict[str, Any]], sport: str) -> bool:
    target = (sport or "").strip().lower()
    return any((prop.get("sport") or "").strip().lower() == target for prop in board or [])


def generate_context_edge_button_output(output_key: str, run_window: str) -> dict[str, Any]:
    """
    Generate one cached Context Edge quick-button output.

    This uses the same board loader, system prompt, model, and quick-button
    prompt text as the existing live Context Edge path. The cached generator
    passes the full loaded board JSON into the existing board prompt slot so
    the model can see all available prop, odds, and enrichment fields.
    """
    config = get_output_config(output_key)
    normalized_output_key = (output_key or "").strip().lower()
    board = _load_context_edge_board()
    board_hash = build_board_hash(board)
    report_date = _report_date()
    generated_at = _utc_now_iso()

    try:
        upsert_context_edge_button_output(
            report_date=report_date,
            run_window=run_window,
            output_key=normalized_output_key,
            status="pending",
            report_json={},
            board_hash=board_hash,
            model=CONTEXT_EDGE_MODEL,
            generated_at=generated_at,
        )

        if normalized_output_key == "nfl_value" and not _has_sport_board(board, "NFL"):
            response_text = "NFL scan not active yet. Check back when NFL markets are live on the board."
        elif not board:
            response_text = "No props on the board right now. Check back after 7 AM PHX when the daily fetch runs."
        else:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")

            today_str = datetime.now().strftime('%A %B %d %Y')
            board_text = _format_context_edge_board(board)
            system_prompt = build_context_edge_system_prompt(today_str, board_text)
            response_text = call_context_edge_ai(
                api_key,
                system_prompt,
                config["prompt"],
            )

        report_json = {
            "output_key": normalized_output_key,
            "label": config["label"],
            "prompt": config["prompt"],
            "response": response_text,
        }
        return upsert_context_edge_button_output(
            report_date=report_date,
            run_window=run_window,
            output_key=normalized_output_key,
            status="ready",
            report_json=report_json,
            board_hash=board_hash,
            model=CONTEXT_EDGE_MODEL,
            generated_at=_utc_now_iso(),
        )

    except Exception as e:
        return upsert_context_edge_button_output(
            report_date=report_date,
            run_window=run_window,
            output_key=normalized_output_key,
            status="failed",
            report_json={
                "output_key": normalized_output_key,
                "label": config["label"],
                "prompt": config["prompt"],
            },
            board_hash=board_hash,
            model=CONTEXT_EDGE_MODEL,
            error_message=str(e),
            generated_at=_utc_now_iso(),
        )
