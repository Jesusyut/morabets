"""
Backend-only Supabase helpers for Mora Bets.

This module uses Supabase REST with the service-role key. It is intentionally
not wired into any route yet, so importing it does not change app behavior.
"""

import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Optional

import requests


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

DEFAULT_TIMEOUT = 10


class SupabaseConfigError(RuntimeError):
    """Raised when Supabase environment variables are not configured."""


def normalize_email(email: str) -> str:
    """Return the canonical email key used by the Flask backend."""
    return (email or "").strip().lower()


OWNER_EMAILS = {
    normalize_email(email)
    for email in os.environ.get("OWNER_EMAILS", "").split(",")
    if normalize_email(email)
}


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _headers(prefer: Optional[str] = None) -> dict[str, str]:
    if not is_configured():
        raise SupabaseConfigError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"
        )

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path.lstrip('/')}"


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, str]] = None,
    json: Any = None,
    prefer: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    response = requests.request(
        method,
        _url(path),
        headers=_headers(prefer=prefer),
        params=params,
        json=json,
        timeout=timeout,
    )
    response.raise_for_status()
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def _single_row(rows: Any) -> Optional[dict[str, Any]]:
    if isinstance(rows, list) and rows:
        return rows[0]
    if isinstance(rows, dict):
        return rows
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_future(value: Optional[str]) -> bool:
    if not value:
        return True
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed >= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def hash_cache_part(value: str) -> str:
    """Small utility for callers that need stable cache keys or hash parts."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_subscription_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    normalized = status.strip().lower()
    return {
        "trial": "trialing",
        "day_pass": "active",
        "canceled": "cancelled",
    }.get(normalized, normalized)


def normalize_daily_report_status(status: Optional[str]) -> str:
    if status is None:
        return "ready"
    normalized = status.strip().lower()
    if normalized not in {"pending", "ready", "failed", "stale"}:
        raise ValueError("Invalid daily report status")
    return normalized


def normalize_context_edge_run_window(run_window: str) -> str:
    normalized = (run_window or "").strip().lower()
    if normalized not in {"morning", "afternoon"}:
        raise ValueError("Invalid Context Edge run window")
    return normalized


def normalize_context_edge_output_key(output_key: str) -> str:
    normalized = (output_key or "").strip().lower()
    allowed = {
        "mlb_value",
        "soccer_value",
        "plus_money",
        "nfl_value",
    }
    if normalized not in allowed:
        raise ValueError("Invalid Context Edge output key")
    return normalized


def _iso_date(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def upsert_profile_by_email(email: str, **fields: Any) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    if not normalized_email or "@" not in normalized_email:
        raise ValueError("A valid email is required")

    payload = {
        "email": normalized_email,
        "updated_at": _utc_now_iso(),
    }
    for key in ("full_name", "stripe_customer_id"):
        if fields.get(key) is not None:
            payload[key] = fields[key]

    rows = _request(
        "POST",
        "profiles",
        params={"on_conflict": "email"},
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    profile = _single_row(rows)
    if not profile:
        raise RuntimeError("Supabase profile upsert returned no row")
    return profile


def _get_latest_subscription_for_profile(
    profile_id: str,
    plan: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    params = {
        "select": "*",
        "profile_id": f"eq.{profile_id}",
        "order": "created_at.desc",
        "limit": "1",
    }
    if plan:
        params["plan"] = f"eq.{plan}"
    rows = _request("GET", "subscriptions", params=params)
    return _single_row(rows)


def upsert_subscription(
    *,
    profile: Optional[dict[str, Any]] = None,
    email: Optional[str] = None,
    **fields: Any,
) -> dict[str, Any]:
    if profile is None:
        if not email:
            raise ValueError("profile or email is required")
        profile = upsert_profile_by_email(
            email,
            full_name=fields.get("full_name") or fields.get("name"),
            stripe_customer_id=fields.get("stripe_customer_id"),
        )
    if not profile.get("id"):
        raise ValueError("profile must include an id")

    payload = {
        "profile_id": profile["id"],
        "updated_at": _utc_now_iso(),
    }
    allowed_fields = (
        "stripe_subscription_id",
        "stripe_price_id",
        "plan",
        "status",
        "current_period_start",
        "current_period_end",
        "trial_ends_at",
        "cancelled_at",
    )
    for key in allowed_fields:
        if fields.get(key) is not None:
            if key == "status":
                payload[key] = normalize_subscription_status(fields[key])
            else:
                payload[key] = fields[key]

    subscription_id = payload.get("stripe_subscription_id")
    if subscription_id:
        rows = _request(
            "POST",
            "subscriptions",
            params={"on_conflict": "stripe_subscription_id"},
            json=payload,
            prefer="resolution=merge-duplicates,return=representation",
        )
        subscription = _single_row(rows)
        if subscription:
            return subscription

    existing = _get_latest_subscription_for_profile(
        profile["id"],
        plan=payload.get("plan"),
    )
    if existing:
        rows = _request(
            "PATCH",
            "subscriptions",
            params={"id": f"eq.{existing['id']}"},
            json=payload,
            prefer="return=representation",
        )
    else:
        rows = _request(
            "POST",
            "subscriptions",
            json=payload,
            prefer="return=representation",
        )

    subscription = _single_row(rows)
    if not subscription:
        raise RuntimeError("Supabase subscription upsert returned no row")
    return subscription


def upsert_subscription_by_email(email: str, **fields: Any) -> dict[str, Any]:
    return upsert_subscription(email=email, **fields)


def read_active_subscription_status_by_email(email: str) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    if not normalized_email or "@" not in normalized_email:
        return {"active": False, "email": normalized_email, "subscription": None}

    profiles = _request(
        "GET",
        "profiles",
        params={
            "select": "id,email",
            "email": f"eq.{normalized_email}",
            "limit": "1",
        },
    )
    profile = _single_row(profiles)
    if not profile:
        return {"active": False, "email": normalized_email, "subscription": None}

    rows = _request(
        "GET",
        "subscriptions",
        params={
            "select": "*",
            "profile_id": f"eq.{profile['id']}",
            "status": "in.(active,trialing)",
            "order": "created_at.desc",
        },
    )

    for subscription in rows or []:
        period_ok = _is_future(subscription.get("current_period_end"))
        trial_ok = _is_future(subscription.get("trial_ends_at"))
        if period_ok and trial_ok:
            return {
                "active": True,
                "email": normalized_email,
                "profile": profile,
                "subscription": subscription,
            }

    return {
        "active": False,
        "email": normalized_email,
        "profile": profile,
        "subscription": _single_row(rows),
    }


def user_has_context_edge_access(email: str) -> bool:
    normalized_email = normalize_email(email)
    if not normalized_email or "@" not in normalized_email:
        return False
    if normalized_email in OWNER_EMAILS:
        return True

    try:
        status = read_active_subscription_status_by_email(normalized_email)
    except SupabaseConfigError:
        return False
    except Exception:
        return False
    return bool(status.get("active"))


def read_context_edge_cache(cache_key: str) -> Optional[dict[str, Any]]:
    if not cache_key:
        return None

    rows = _request(
        "GET",
        "context_edge_cache",
        params={
            "select": "*",
            "cache_key": f"eq.{cache_key}",
            "expires_at": f"gt.{_utc_now_iso()}",
            "limit": "1",
        },
    )
    return _single_row(rows)


def write_context_edge_cache(
    cache_key: str,
    *,
    prompt_hash: str,
    board_hash: str,
    user_message: str,
    response: str,
    expires_at: str,
    model: Optional[str] = None,
    sport: Optional[str] = None,
) -> dict[str, Any]:
    if not cache_key:
        raise ValueError("cache_key is required")

    payload = {
        "cache_key": cache_key,
        "prompt_hash": prompt_hash,
        "board_hash": board_hash,
        "user_message": user_message,
        "response": response,
        "expires_at": expires_at,
        "updated_at": _utc_now_iso(),
    }
    if model is not None:
        payload["model"] = model
    if sport is not None:
        payload["sport"] = sport

    rows = _request(
        "POST",
        "context_edge_cache",
        params={"on_conflict": "cache_key"},
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    cached = _single_row(rows)
    if not cached:
        raise RuntimeError("Supabase context cache upsert returned no row")
    return cached


def upsert_context_edge_daily_report(
    *,
    report_date: Any,
    report_json: dict[str, Any],
    board_hash: str,
    sport_scope: str = "all",
    status: str = "ready",
    model: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    if not report_date:
        raise ValueError("report_date is required")
    if not isinstance(report_json, dict):
        raise ValueError("report_json must be a dict")
    if not board_hash:
        raise ValueError("board_hash is required")

    payload = {
        "report_date": _iso_date(report_date),
        "sport_scope": sport_scope or "all",
        "status": normalize_daily_report_status(status),
        "report_json": report_json,
        "board_hash": board_hash,
        "updated_at": _utc_now_iso(),
    }
    if model is not None:
        payload["model"] = model
    if generated_at is not None:
        payload["generated_at"] = generated_at

    rows = _request(
        "POST",
        "context_edge_daily_reports",
        params={"on_conflict": "report_date,sport_scope"},
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    report = _single_row(rows)
    if not report:
        raise RuntimeError("Supabase daily report upsert returned no row")
    return report


def read_context_edge_daily_report(
    report_date: Optional[Any] = None,
    sport_scope: str = "all",
) -> Optional[dict[str, Any]]:
    params = {
        "select": "*",
        "sport_scope": f"eq.{sport_scope or 'all'}",
        "order": "report_date.desc,generated_at.desc",
        "limit": "1",
    }
    if report_date is not None:
        params["report_date"] = f"eq.{_iso_date(report_date)}"

    rows = _request(
        "GET",
        "context_edge_daily_reports",
        params=params,
    )
    return _single_row(rows)


def upsert_context_edge_button_output(
    *,
    report_date: Any,
    run_window: str,
    output_key: str,
    report_json: dict[str, Any],
    board_hash: str,
    status: str = "pending",
    model: Optional[str] = None,
    error_message: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    if not report_date:
        raise ValueError("report_date is required")
    if not isinstance(report_json, dict):
        raise ValueError("report_json must be a dict")
    if not board_hash:
        raise ValueError("board_hash is required")

    payload = {
        "report_date": _iso_date(report_date),
        "run_window": normalize_context_edge_run_window(run_window),
        "output_key": normalize_context_edge_output_key(output_key),
        "status": normalize_daily_report_status(status),
        "report_json": report_json,
        "board_hash": board_hash,
        "updated_at": _utc_now_iso(),
    }
    if model is not None:
        payload["model"] = model
    if error_message is not None:
        payload["error_message"] = error_message
    if generated_at is not None:
        payload["generated_at"] = generated_at

    rows = _request(
        "POST",
        "context_edge_button_outputs",
        params={"on_conflict": "report_date,run_window,output_key"},
        json=payload,
        prefer="resolution=merge-duplicates,return=representation",
    )
    output = _single_row(rows)
    if not output:
        raise RuntimeError("Supabase button output upsert returned no row")
    return output


def read_latest_ready_context_edge_button_output(
    output_key: str,
) -> Optional[dict[str, Any]]:
    rows = _request(
        "GET",
        "context_edge_button_outputs",
        params={
            "select": "*",
            "output_key": f"eq.{normalize_context_edge_output_key(output_key)}",
            "status": "eq.ready",
            "order": "generated_at.desc",
            "limit": "1",
        },
    )
    return _single_row(rows)


def read_latest_context_edge_button_output(
    output_key: str,
) -> Optional[dict[str, Any]]:
    rows = _request(
        "GET",
        "context_edge_button_outputs",
        params={
            "select": "*",
            "output_key": f"eq.{normalize_context_edge_output_key(output_key)}",
            "order": "generated_at.desc",
            "limit": "1",
        },
    )
    return _single_row(rows)


def read_context_edge_button_output(
    report_date: Any,
    run_window: str,
    output_key: str,
) -> Optional[dict[str, Any]]:
    if not report_date:
        raise ValueError("report_date is required")

    rows = _request(
        "GET",
        "context_edge_button_outputs",
        params={
            "select": "*",
            "report_date": f"eq.{_iso_date(report_date)}",
            "run_window": f"eq.{normalize_context_edge_run_window(run_window)}",
            "output_key": f"eq.{normalize_context_edge_output_key(output_key)}",
            "limit": "1",
        },
    )
    return _single_row(rows)
