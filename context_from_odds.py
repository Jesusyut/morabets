from collections import Counter
from odds_utils import american_to_prob, no_vig_two_way, PREFERRED_BOOKS

def _choose_books(bookmakers):
    preferred = [b for b in bookmakers if (b.get("key") in PREFERRED_BOOKS)]
    return preferred if preferred else (bookmakers or [])

def _consensus_total_line(bookmakers) -> float:
    points = []
    for b in _choose_books(bookmakers):
        for m in b.get("markets", []):
            if m.get("key") == "totals":
                for o in m.get("outcomes", []):
                    if "point" in o:
                        points.append(o["point"])
    if not points:
        return None
    return Counter(points).most_common(1)[0][0] if points else None

def compute_event_context(event_odds: dict) -> dict:
    """
    Build event-level true (de-vigged) win probabilities and totals Over/Under true probs
    at a consensus line.
    Returns keys:
      - event_id, home_team, away_team, favored_team
      - home_true_win, away_true_win
      - total_point, true_prob_over, true_prob_under
    """
    if not event_odds:
        return {}

    bookmakers = event_odds.get("bookmakers") or []
    if not bookmakers:
        return {}

    home_name = event_odds.get("home_team")
    away_name = event_odds.get("away_team")

    # Moneyline -> de-vig per book, then average
    home_p, away_p = [], []
    for b in _choose_books(bookmakers):
        for m in b.get("markets", []):
            if m.get("key") == "h2h":
                prices = {}
                for o in m.get("outcomes", []):
                    nm = o.get("name")
                    pr = o.get("price")
                    if nm is not None and pr is not None:
                        prices[nm] = american_to_prob(int(pr))
                if home_name in prices and away_name in prices:
                    p_h, p_a = no_vig_two_way(prices[home_name], prices[away_name])
                    if p_h and p_a:
                        home_p.append(p_h); away_p.append(p_a)

    home_true = round(sum(home_p)/len(home_p), 4) if home_p else None
    away_true = round(sum(away_p)/len(away_p), 4) if away_p else None

    favored_team = None
    if home_true is not None and away_true is not None:
        favored_team = home_name if home_true > away_true else away_name

    # Totals -> pick consensus line, then de-vig per book, then average
    total_point = _consensus_total_line(bookmakers)
    over_probs, under_probs = [], []
    if total_point is not None:
        for b in _choose_books(bookmakers):
            for m in b.get("markets", []):
                if m.get("key") == "totals":
                    over_p, under_p = None, None
                    for o in m.get("outcomes", []):
                        if o.get("point") == total_point:
                            nm = o.get("name")
                            pr = o.get("price")
                            if nm == "Over":
                                over_p = american_to_prob(int(pr))
                            elif nm == "Under":
                                under_p = american_to_prob(int(pr))
                    if (over_p is not None) and (under_p is not None):
                        p_o, p_u = no_vig_two_way(over_p, under_p)
                        if p_o and p_u:
                            over_probs.append(p_o); under_probs.append(p_u)

    true_over = round(sum(over_probs)/len(over_probs), 4) if over_probs else None
    true_under = round(sum(under_probs)/len(under_probs), 4) if under_probs else None

    return {
        "event_id": event_odds.get("id"),
        "home_team": home_name,
        "away_team": away_name,
        "favored_team": favored_team,
        "home_true_win": home_true,
        "away_true_win": away_true,
        "total_point": total_point,
        "true_prob_over": true_over,
        "true_prob_under": true_under,
    } 