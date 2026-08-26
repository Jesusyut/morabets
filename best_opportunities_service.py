from datetime import datetime, timezone


MIN_NO_VIG_PROB = 55.0
MIN_EDGE_PCT = 0.0
MIN_ACCEPTABLE_ODDS = -220
MAX_OPPORTUNITIES = 60
ACTION_BOOKS = ("draftkings", "fanduel")


def _to_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value):
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_book_name(value):
    return str(value or "").strip().lower().replace(" ", "")


def _book_title(value):
    name = str(value or "").strip()
    if _normalize_book_name(name) == "draftkings":
        return "DraftKings"
    if _normalize_book_name(name) == "fanduel":
        return "FanDuel"
    return name


def _is_action_book(value):
    normalized = _normalize_book_name(value)
    return any(book in normalized for book in ACTION_BOOKS)


def _extract_book_rows(item):
    rows = []
    for book in item.get("all_books") or []:
        if not isinstance(book, dict):
            continue
        book_name = book.get("book") or book.get("title") or book.get("bookmaker") or book.get("sportsbook")
        price = _to_int(
            book.get("over_price")
            if book.get("over_price") is not None
            else book.get("price")
            if book.get("price") is not None
            else book.get("odds")
        )
        if book_name and price is not None:
            rows.append({"book": _book_title(book_name), "price": price})

    best_book = item.get("best_book") or item.get("book")
    best_price = _to_int(
        item.get("best_over_price")
        if item.get("best_over_price") is not None
        else item.get("odds")
        if item.get("odds") is not None
        else item.get("price")
    )
    if best_book and best_price is not None:
        rows.append({"book": _book_title(best_book), "price": best_price})

    deduped = {}
    for row in rows:
        key = _normalize_book_name(row["book"])
        current = deduped.get(key)
        if current is None or row["price"] > current["price"]:
            deduped[key] = row
    return list(deduped.values())


def _best_actionable_book(item):
    candidates = [
        row for row in _extract_book_rows(item)
        if _is_action_book(row["book"]) and row["price"] >= MIN_ACCEPTABLE_ODDS
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: row["price"], reverse=True)[0]


def _context_notes(item):
    text_parts = [
        item.get("matchup"),
        item.get("environment"),
        item.get("game_environment"),
        item.get("team_status"),
        item.get("weather_note"),
        item.get("context_note"),
    ]
    joined = " ".join(str(part) for part in text_parts if part).lower()
    notes = []
    if "high scoring" in joined or "hitter" in joined or "offense" in joined:
        notes.append("High-scoring context")
    if "rest" in joined or "travel" in joined or "injur" in joined:
        notes.append("Situational context")
    if item.get("contextual_hit_rate") or item.get("context_probability"):
        notes.append("Context layer available")
    if len(_extract_book_rows(item)) >= 2:
        notes.append("Line-shop confirmed")
    if not notes:
        notes.append("No-vig board signal")
    return notes[:3]


def _market_label(item):
    label = item.get("stat_label") or item.get("market") or item.get("stat") or item.get("market_type")
    if label:
        return str(label)
    return "Market"


def _title(item):
    return str(item.get("player") or item.get("team") or item.get("name") or item.get("selection") or "Unknown")


def _score(no_vig_prob, edge_pct, price, notes):
    edge_component = min(max(edge_pct or 0, 0), 20) * 1.2
    odds_component = 4 if price and price > 0 else 2 if price and price >= -140 else 0
    context_component = len([note for note in notes if note != "No-vig board signal"]) * 2
    return round(no_vig_prob + edge_component + odds_component + context_component, 2)


def _normalize_item(item, sport, source_type):
    if not isinstance(item, dict):
        return None

    no_vig_prob = _to_float(
        item.get("no_vig_prob")
        if item.get("no_vig_prob") is not None
        else item.get("prob")
        if item.get("prob") is not None
        else item.get("probability")
    )
    if no_vig_prob is None or no_vig_prob < MIN_NO_VIG_PROB:
        return None

    edge_pct = _to_float(item.get("ev_pct") if item.get("ev_pct") is not None else item.get("edge_pct"))
    if edge_pct is not None and edge_pct <= MIN_EDGE_PCT and source_type == "line":
        return None

    action_book = _best_actionable_book(item)
    if not action_book:
        return None

    notes = _context_notes(item)
    price = action_book["price"]
    return {
        "sport": sport,
        "source_type": source_type,
        "title": _title(item),
        "market": _market_label(item),
        "line": item.get("line"),
        "matchup": item.get("matchup"),
        "game_time": item.get("game_time"),
        "no_vig_prob": round(no_vig_prob, 1),
        "edge_pct": round(edge_pct, 1) if edge_pct is not None else None,
        "book": action_book["book"],
        "odds": price,
        "available_books": sorted({_book_title(row["book"]) for row in _extract_book_rows(item) if _is_action_book(row["book"])}),
        "context_notes": notes,
        "score": _score(no_vig_prob, edge_pct, price, notes),
    }


def _unique_key(item):
    return "|".join(str(item.get(key) or "") for key in ("sport", "title", "market", "line", "matchup", "book", "odds"))


def _line_items_from_payload(payload):
    if not isinstance(payload, dict):
        return []
    seen = set()
    items = []
    for bucket in ("edge_picks", "no_vig_picks", "picks"):
        for item in payload.get(bucket) or []:
            key = "|".join(str(item.get(field) or "") for field in ("player", "stat_label", "line", "game_time", "best_book"))
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    return items


def _prop_items_from_payload(payload):
    if isinstance(payload, dict):
        return payload.get("props") or payload.get("picks") or []
    if isinstance(payload, list):
        return payload
    return []


def build_best_opportunities_report(line_payloads=None, prop_payloads=None, generated_at=None):
    opportunities = []
    source_counts = {}

    for sport, payload in (line_payloads or {}).items():
        items = _line_items_from_payload(payload)
        source_counts[f"{sport.lower()}_lines"] = len(items)
        for item in items:
            normalized = _normalize_item(item, sport, "line")
            if normalized:
                opportunities.append(normalized)

    for sport, payload in (prop_payloads or {}).items():
        items = _prop_items_from_payload(payload)
        source_counts[f"{sport.lower()}_props"] = len(items)
        for item in items:
            normalized = _normalize_item(item, sport, "prop")
            if normalized:
                opportunities.append(normalized)

    deduped = {}
    for item in opportunities:
        key = _unique_key(item)
        current = deduped.get(key)
        if current is None or item["score"] > current["score"]:
            deduped[key] = item

    ranked = sorted(deduped.values(), key=lambda item: (item["score"], item["no_vig_prob"], item["odds"]), reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index

    return {
        "status": "ready" if ranked else "empty",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_no_vig_prob": MIN_NO_VIG_PROB,
            "required_books": ["DraftKings", "FanDuel"],
            "min_acceptable_odds": MIN_ACCEPTABLE_ODDS,
            "edge_required_for_lines": True,
            "uses_cached_board_only": True,
        },
        "source_counts": source_counts,
        "count": len(ranked[:MAX_OPPORTUNITIES]),
        "opportunities": ranked[:MAX_OPPORTUNITIES],
    }
