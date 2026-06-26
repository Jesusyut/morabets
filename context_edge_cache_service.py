"""
Context Edge cache lookup service.

This module does not call any AI provider and is not wired into the live
Context Edge route yet. It only creates deterministic cache keys and checks
the Supabase cache table through the backend helper.
"""

import hashlib
import json
import re
from typing import Any, Callable, Optional

from supabase_backend import read_context_edge_cache


CACHE_KEY_VERSION = "context-edge:v1"


def normalize_user_prompt(user_prompt: str) -> str:
    """Normalize prompt text so casing and extra whitespace do not split cache."""
    return re.sub(r"\s+", " ", (user_prompt or "").strip()).lower()


def canonical_board_json(board: Any) -> str:
    """Serialize today's board into stable JSON for hashing."""
    return json.dumps(
        board or [],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_context_edge_cache_parts(
    board: Any,
    user_prompt: str,
) -> dict[str, str]:
    normalized_prompt = normalize_user_prompt(user_prompt)
    board_json = canonical_board_json(board)
    prompt_hash = sha256_hex(normalized_prompt)
    board_hash = sha256_hex(board_json)
    cache_key = sha256_hex(
        f"{CACHE_KEY_VERSION}:{prompt_hash}:{board_hash}"
    )

    return {
        "cache_key": cache_key,
        "prompt_hash": prompt_hash,
        "board_hash": board_hash,
        "normalized_prompt": normalized_prompt,
    }


def lookup_context_edge_cache(
    board: Any,
    user_prompt: str,
    *,
    cache_reader: Callable[[str], Optional[dict[str, Any]]] = read_context_edge_cache,
) -> dict[str, Any]:
    """
    Check Supabase for a cached Context Edge response.

    Returns a small result object:
    - hit=True with response/cache_row when a cached response exists
    - hit=False when Supabase has no matching cache entry
    """
    parts = build_context_edge_cache_parts(board, user_prompt)
    cache_row = cache_reader(parts["cache_key"])

    if cache_row and cache_row.get("response"):
        return {
            "hit": True,
            "cache_key": parts["cache_key"],
            "prompt_hash": parts["prompt_hash"],
            "board_hash": parts["board_hash"],
            "response": cache_row["response"],
            "cache_row": cache_row,
        }

    return {
        "hit": False,
        "cache_key": parts["cache_key"],
        "prompt_hash": parts["prompt_hash"],
        "board_hash": parts["board_hash"],
        "response": None,
        "cache_row": cache_row,
    }
