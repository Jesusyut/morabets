import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Book Classification ─────────────────────────────────
SHARP_BOOKS = {
    "pinnacle",
    "betcris",
    "circa",
    "bookmaker"
}

STANDARD_BOOKS = {
    "draftkings",
    "fanduel",
    "betmgm",
    "caesars",
    "pointsbet",
    "betrivers",
    "fanatics"
}

SOFT_BOOKS = {
    "bovada",
    "mybookie",
    "betonlineag",
    "heritage"
}

BOOK_WEIGHTS = {
    "pinnacle":    3.0,
    "betcris":     2.5,
    "circa":       2.5,
    "bookmaker":   2.0,
    "draftkings":  1.5,
    "fanduel":     1.5,
    "betmgm":      1.2,
    "caesars":     1.2,
    "pointsbet":   1.0,
    "pointsbetus": 1.0,
    "betrivers":   1.0,
    "fanatics":    1.0,
    "bovada":      0.8,
    "mybookie":    0.7,
    "betonlineag": 0.8,
    "heritage":    0.7
}

EV_THRESHOLDS = {
    "h2h":                  2.0,
    "spreads":              2.5,
    "totals":               2.0,
    "pitcher_strikeouts":   4.0,
    "batter_hits":          4.0,
    "batter_total_bases":   4.0,
    "batter_home_runs":     5.0,
    "player_shots_on_goal": 4.0,
    "player_points":        4.0,
    "default":              3.0
}

MIN_BOOKS_REQUIRED = {
    "h2h":     3,
    "spreads":  3,
    "totals":   3,
    "default":  2
}


def american_to_implied(american_odds: int) -> float:
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def implied_to_american(prob: float) -> int:
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return round(-(prob / (1 - prob)) * 100)
    else:
        return round(((1 - prob) / prob) * 100)


def remove_vig_two_sided(over_price: int, under_price: int) -> tuple:
    raw_over = american_to_implied(over_price)
    raw_under = american_to_implied(under_price)
    total = raw_over + raw_under
    if total == 0:
        return 0.5, 0.5
    return raw_over / total, raw_under / total


def calculate_weighted_fair_probability(
    book_prices: list,
    side: str = "over"
) -> tuple:
    weighted_prob_sum = 0.0
    total_weight = 0.0
    book_count = 0

    for entry in book_prices:
        book_name = entry.get('book', '').lower()
        over_p = entry.get('over_price')
        under_p = entry.get('under_price')

        if over_p is None or under_p is None:
            continue

        no_vig_over, no_vig_under = remove_vig_two_sided(over_p, under_p)
        prob = no_vig_over if side == "over" else no_vig_under
        weight = BOOK_WEIGHTS.get(book_name, 1.0)

        weighted_prob_sum += prob * weight
        total_weight += weight
        book_count += 1

    if book_count == 0 or total_weight == 0:
        return None, 0, 0.0

    fair_prob = weighted_prob_sum / total_weight
    return round(fair_prob, 4), book_count, total_weight


def calculate_ev(fair_probability: float, offered_odds: int) -> float:
    if offered_odds > 0:
        profit_per_unit = offered_odds / 100
    else:
        profit_per_unit = 100 / abs(offered_odds)

    lose_prob = 1 - fair_probability
    ev = (fair_probability * profit_per_unit) - (lose_prob * 1.0)
    return round(ev * 100, 2)


def calculate_edge_in_cents(fair_probability: float, offered_odds: int) -> float:
    fair_american = implied_to_american(fair_probability)
    fair_implied = american_to_implied(fair_american)
    offered_implied = american_to_implied(offered_odds)
    edge = fair_implied - offered_implied
    return round(edge * 100, 2)


def get_best_offered_line(book_prices: list, side: str = "over") -> tuple:
    best_price = None
    best_book = None

    for entry in book_prices:
        price_key = 'over_price' if side == 'over' else 'under_price'
        price = entry.get(price_key)
        book = entry.get('book', '')

        if price is None:
            continue

        if best_price is None or price > best_price:
            best_price = price
            best_book = book

    return best_price, best_book


def evaluate_pick(
    player_or_team: str,
    market_type: str,
    side: str,
    line,
    book_prices: list,
    game_time=None,
    minutes_to_game=None
) -> dict:
    result = {
        "player":            player_or_team,
        "market_type":       market_type,
        "side":              side,
        "line":              line,
        "book_count":        0,
        "fair_probability":  None,
        "fair_odds":         None,
        "best_offered_odds": None,
        "best_book":         None,
        "break_even_prob":   None,
        "ev_pct":            None,
        "edge_pct":          None,
        "passes_threshold":  False,
        "rejection_reason":  None,
        "confidence_tier":   "LOW",
        "game_time":         game_time,
        "minutes_to_game":   minutes_to_game,
        "evaluated_at":      int(time.time()),
        "all_books":         book_prices
    }

    min_books = MIN_BOOKS_REQUIRED.get(market_type, MIN_BOOKS_REQUIRED["default"])
    valid_books = [
        b for b in book_prices
        if b.get('over_price') is not None and b.get('under_price') is not None
    ]
    result["book_count"] = len(valid_books)

    if len(valid_books) < min_books:
        result["rejection_reason"] = (
            f"Insufficient books: {len(valid_books)} available, {min_books} required"
        )
        return result

    fair_prob, book_count, weight = calculate_weighted_fair_probability(valid_books, side)

    if fair_prob is None:
        result["rejection_reason"] = "Could not calculate fair probability"
        return result

    result["fair_probability"] = fair_prob
    result["fair_odds"] = implied_to_american(fair_prob)

    best_odds, best_book = get_best_offered_line(valid_books, side)

    if best_odds is None:
        result["rejection_reason"] = "No offered line found"
        return result

    result["best_offered_odds"] = best_odds
    result["best_book"] = best_book
    result["break_even_prob"] = round(american_to_implied(best_odds), 4)

    ev = calculate_ev(fair_prob, best_odds)
    edge = calculate_edge_in_cents(fair_prob, best_odds)

    result["ev_pct"] = ev
    result["edge_pct"] = edge

    threshold = EV_THRESHOLDS.get(market_type, EV_THRESHOLDS["default"])

    if ev < threshold:
        result["rejection_reason"] = (
            f"EV {ev:.1f}% below threshold {threshold:.1f}% for {market_type}"
        )
        return result

    result["passes_threshold"] = True

    if ev >= 6.0:
        result["confidence_tier"] = "LOCK"
    elif ev >= 3.0:
        result["confidence_tier"] = "FIRE"
    else:
        result["confidence_tier"] = "EDGE"

    logger.info(
        f"[EV] ✅ {player_or_team} {market_type} {side} | "
        f"Fair: {result['fair_odds']} | Best: {best_odds}@{best_book} | "
        f"EV: {ev:.1f}% | Edge: {edge:.1f}%"
    )

    return result
