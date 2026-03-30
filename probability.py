import logging
from functools import reduce

logger = logging.getLogger(__name__)

def implied_probability(odds):
    """Calculate implied probability from American odds"""
    try:
        if odds > 0:
            return round(100 / (odds + 100), 4)
        else:
            return round(abs(odds) / (abs(odds) + 100), 4)
    except (ValueError, ZeroDivisionError) as e:
        logger.error(f"Error calculating implied probability for odds {odds}: {e}")
        return 0.0

def calculate_edge(true_prob, book_odds):
    """Calculate betting edge"""
    try:
        if not (0 <= true_prob <= 1):
            raise ValueError("True probability must be between 0 and 1")
        
        imp_prob = implied_probability(book_odds)
        edge = round((true_prob - imp_prob) * 100, 2)
        
        return {
            "true_prob": true_prob,
            "book_odds": book_odds,
            "implied_prob": imp_prob,
            "edge": edge
        }
    except Exception as e:
        logger.error(f"Error calculating edge: {e}")
        return {"error": str(e)}

def kelly_bet_size(prob, odds, bankroll):
    """Calculate Kelly criterion bet size"""
    try:
        if not (0 <= prob <= 1):
            raise ValueError("Probability must be between 0 and 1")
        if bankroll <= 0:
            raise ValueError("Bankroll must be positive")
        
        # Convert American odds to decimal odds
        if odds > 0:
            decimal_odds = (odds / 100) + 1
        else:
            decimal_odds = (100 / abs(odds)) + 1
        
        # Kelly formula: f = (bp - q) / b
        # where b = decimal_odds - 1, p = prob, q = 1 - prob
        b = decimal_odds - 1
        edge = prob * b - (1 - prob)
        kelly_fraction = edge / b if b > 0 else 0
        
        # Apply fractional Kelly (typically 25% of full Kelly for risk management)
        fractional_kelly = kelly_fraction * 0.25
        
        # Ensure bet size is not negative
        bet_size = max(0, round(bankroll * fractional_kelly, 2))
        
        return bet_size
    except Exception as e:
        logger.error(f"Error calculating Kelly bet size: {e}")
        return 0.0

def no_vig_probability(over_price, under_price):
    """Strip vig and return true probability for the OVER side. Both inputs are American odds."""
    def to_implied(american):
        if american > 0:
            return 100 / (american + 100)
        else:
            return abs(american) / (abs(american) + 100)
    try:
        raw_over = to_implied(over_price)
        raw_under = to_implied(under_price)
        total = raw_over + raw_under
        if total == 0:
            return 50.0
        return round((raw_over / total) * 100, 1)
    except Exception:
        return 50.0

def get_confidence_tier(no_vig_prob):
    """Return LOCK, FIRE, or LOW based on no-vig probability."""
    if no_vig_prob >= 70:
        return "LOCK"
    elif no_vig_prob >= 50:
        return "FIRE"
    else:
        return "LOW"

TIER_SORT_ORDER = {"LOCK": 0, "FIRE": 1, "LOW": 2}

def sort_props_by_tier(props):
    """Sort props: LOCK first, then FIRE, then LOW. Within tier, highest prob first."""
    return sorted(
        props,
        key=lambda p: (TIER_SORT_ORDER.get(p.get("confidence_tier", "LOW"), 2),
                       -p.get("no_vig_prob", 0))
    )

def calculate_parlay_edge(probabilities, book_odds):
    """Calculate parlay edge"""
    try:
        if not probabilities or not isinstance(probabilities, list):
            raise ValueError("Probabilities must be a non-empty list")
        
        for prob in probabilities:
            if not (0 <= prob <= 1):
                raise ValueError("All probabilities must be between 0 and 1")
        
        # Calculate combined probability
        combined_prob = reduce(lambda x, y: x * y, probabilities)
        
        # Calculate true fair odds
        true_odds = (1 / combined_prob) if combined_prob > 0 else float('inf')
        
        # Convert book odds to decimal format for comparison
        if book_odds > 0:
            decimal_book_odds = (book_odds / 100) + 1
        else:
            decimal_book_odds = (100 / abs(book_odds)) + 1
        
        # Calculate edge
        edge = round(true_odds - decimal_book_odds, 2)
        
        return {
            "combined_prob": round(combined_prob * 100, 2),
            "true_odds": round(true_odds, 2),
            "book_odds": book_odds,
            "decimal_book_odds": round(decimal_book_odds, 2),
            "edge": edge
        }
    except Exception as e:
        logger.error(f"Error calculating parlay edge: {e}")
        return {"error": str(e)}