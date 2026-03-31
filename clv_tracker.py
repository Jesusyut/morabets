import json
import os
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
CLV_LOG_FILE = "clv_log.json"


def log_bet_entry(
    event_id: str,
    player_or_team: str,
    market_type: str,
    side: str,
    line,
    book: str,
    offered_odds: int,
    fair_probability: float,
    fair_odds: int,
    ev_pct: float,
    edge_pct: float,
    game_time: str,
    minutes_to_game: int = None
) -> dict:
    entry = {
        "id":               str(event_id),
        "logged_at":        int(time.time()),
        "logged_at_iso":    datetime.utcnow().isoformat(),
        "player_or_team":   player_or_team,
        "market_type":      market_type,
        "side":             side,
        "line":             line,
        "book":             book,
        "offered_odds":     offered_odds,
        "fair_probability": fair_probability,
        "fair_odds":        fair_odds,
        "ev_pct":           ev_pct,
        "edge_pct":         edge_pct,
        "game_time":        game_time,
        "minutes_to_game":  minutes_to_game,
        "closing_odds":     None,
        "clv":              None,
        "result":           None,
        "status":           "open"
    }

    log = _load_log()
    log.append(entry)
    _save_log(log)

    logger.info(f"[CLV] Logged entry: {player_or_team} {market_type} @ {offered_odds}")
    return entry


def update_closing_line(
    event_id: str,
    market_type: str,
    side: str,
    closing_odds: int
):
    log = _load_log()

    for entry in log:
        if (entry["id"] == str(event_id) and
                entry["market_type"] == market_type and
                entry["side"] == side and
                entry["status"] == "open"):

            from ev_engine import american_to_implied

            entry_implied = american_to_implied(entry["offered_odds"])
            close_implied = american_to_implied(closing_odds)

            clv = round((close_implied - entry_implied) * 100, 2)

            entry["closing_odds"] = closing_odds
            entry["clv"] = clv

            logger.info(
                f"[CLV] Updated close for {entry['player_or_team']}: "
                f"entry={entry['offered_odds']}, close={closing_odds}, CLV={clv:+.2f}%"
            )

    _save_log(log)


def record_result(
    event_id: str,
    market_type: str,
    side: str,
    result: str
):
    log = _load_log()

    for entry in log:
        if (entry["id"] == str(event_id) and
                entry["market_type"] == market_type and
                entry["side"] == side):
            entry["result"] = result
            entry["status"] = "settled"

    _save_log(log)


def get_performance_report() -> dict:
    log = _load_log()
    settled = [e for e in log if e["status"] == "settled"]

    if not settled:
        return {"message": "No settled bets yet"}

    total = len(settled)
    wins = len([e for e in settled if e["result"] == "W"])
    losses = len([e for e in settled if e["result"] == "L"])

    clv_entries = [e for e in settled if e["clv"] is not None]
    avg_clv = (
        sum(e["clv"] for e in clv_entries) / len(clv_entries)
        if clv_entries else None
    )

    roi_points = 0
    for e in settled:
        if e["result"] == "W":
            odds = e["offered_odds"]
            if odds > 0:
                roi_points += odds / 100
            else:
                roi_points += 100 / abs(odds)
        elif e["result"] == "L":
            roi_points -= 1.0

    roi_pct = round((roi_points / total) * 100, 2)

    by_market = {}
    market_types = set(e["market_type"] for e in settled)
    for mt in market_types:
        mt_entries = [e for e in settled if e["market_type"] == mt]
        mt_wins = len([e for e in mt_entries if e["result"] == "W"])
        by_market[mt] = {
            "count":    len(mt_entries),
            "wins":     mt_wins,
            "win_rate": round(mt_wins / len(mt_entries) * 100, 1)
        }

    return {
        "total_bets":      total,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        round(wins / total * 100, 1),
        "roi_pct":         roi_pct,
        "avg_clv":         avg_clv,
        "avg_ev_at_entry": round(
            sum(e["ev_pct"] for e in settled if e["ev_pct"]) / total, 2
        ),
        "by_market":       by_market,
        "clv_positive":    len([e for e in clv_entries if e["clv"] > 0]),
        "clv_negative":    len([e for e in clv_entries if e["clv"] < 0])
    }


def _load_log() -> list:
    try:
        with open(CLV_LOG_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _save_log(log: list):
    try:
        with open(CLV_LOG_FILE, 'w') as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        logger.error(f"[CLV] Save failed: {e}")
