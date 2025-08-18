from collections import defaultdict

PREFERRED_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbetus"]

def american_to_prob(american: int) -> float:
    """Convert American odds to implied probability (with vig)."""
    if american is None:
        return 0.0
    if int(american) >= 0:
        return 100.0 / (int(american) + 100.0)
    return (-int(american)) / ((-int(american)) + 100.0)

def no_vig_two_way(p_a: float, p_b: float) -> tuple[float, float]:
    """De-vig a two-way market: returns normalized probs that sum to 1."""
    if p_a is None or p_b is None:
        return 0.0, 0.0
    s = p_a + p_b
    if s <= 0:
        return 0.0, 0.0
    return p_a / s, p_b / s 