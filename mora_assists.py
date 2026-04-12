"""
Mora Assists — LLM pick selection, email formatting, and delivery.
Runs daily at 10:30 AM ET via scheduler in app.py.
"""

import os
import json
import logging
import anthropic
import sendgrid
from sendgrid.helpers.mail import Mail
from datetime import datetime
from enrichment import load_props_from_file

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SENDGRID_API_KEY  = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL        = os.getenv("EMAIL_FROM", "picks@morabets.com")


# ══════════════════════════════════════════
# STEP 1: LOAD THE FULL BOARD
# ══════════════════════════════════════════

def load_full_board():
    """
    Load all cached props and game lines across every sport.
    Props come from local JSON cache files.
    Lines come from the in-memory app cache (mlb_odds / nhl_odds).
    """
    board = {"props": [], "lines": [], "sports_found": []}

    # ── PROPS ── check both /var/data/ and local ./
    prop_files = [
        ('mlb_props_cache.json',      'MLB'),
        ('/var/data/mlb_props_cache.json', 'MLB'),
        ('nhl_props_cache.json',      'NHL'),
        ('/var/data/nhl_props_cache.json', 'NHL'),
        ('nfl_props_cache.json',      'NFL'),
        ('/var/data/nfl_props_cache.json', 'NFL'),
    ]
    seen_files = set()
    for filepath, sport in prop_files:
        if filepath in seen_files:
            continue
        try:
            props = load_props_from_file(filepath)
            if props:
                board["props"].extend(props)
                seen_files.add(filepath)
                if sport not in board["sports_found"]:
                    board["sports_found"].append(sport)
                logger.info(f"[BOARD] {len(props)} props from {filepath}")
        except Exception:
            pass

    # ── LINES ── read from in-memory app cache (populated by update_odds())
    try:
        from app import cache_get
        import json as _json

        line_keys = [
            ('mlb_odds', 'MLB'),
            ('nhl_odds', 'NHL'),
            ('mlb_curated_picks', 'MLB'),
            ('nhl_curated_picks', 'NHL'),
        ]
        for key, sport in line_keys:
            try:
                val = cache_get(key)
                if val:
                    data = _json.loads(val) if isinstance(val, str) else val
                    if isinstance(data, list) and data:
                        board["lines"].extend(data)
                        if sport not in board["sports_found"]:
                            board["sports_found"].append(sport)
                        logger.info(f"[BOARD] {len(data)} lines from cache key: {key}")
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"[BOARD] Lines cache error: {e}")

    if not board["lines"]:
        logger.warning(
            "[BOARD] No lines cache found. "
            "LLM will select anchors from high-probability props."
        )

    # Filter to quality minimums
    board["props"] = [p for p in board["props"] if p.get("no_vig_prob", 0) >= 52]
    board["lines"] = [l for l in board["lines"] if l.get("no_vig_prob", 0) >= 60]

    logger.info(
        f"[BOARD] Final: {len(board['props'])} props, "
        f"{len(board['lines'])} lines, sports: {board['sports_found']}"
    )
    return board


# ══════════════════════════════════════════
# STEP 2: LLM PICK SELECTION
# ══════════════════════════════════════════

SELECTION_PROMPT = """
You are a professional data analyst —
the best in the market for oddsmakers.
You know the right proportions.
You are the Mora Assists pick selector.

Every morning you receive the full board.
Analyze everything across all sports.

YOUR OUTPUT IS EXACTLY 7 PICKS:
- 2 player props (picks 1 and 2)
- 3 anchor plays (picks 3, 4, and 5)
- 2 casual picks (picks 6 and 7)

═══════════════════════════════════════
BOOKS — DRAFTKINGS AND FANDUEL ONLY
═══════════════════════════════════════

For ALL picks — props, anchors, casual —
ONLY recommend lines available at:
- DraftKings
- FanDuel

Never recommend BetMGM, Caesars,
BetRivers, or any other book.
If a pick is only available at other books
skip it and find one at DK or FD.

═══════════════════════════════════════
PROBABILITY RULES — STRICT
═══════════════════════════════════════

Core picks (props + anchors) 1-5:
  PREFERRED range: 62% to 68%
  MAXIMUM: 70% hard ceiling
  MINIMUM: 60%

  Above 70% almost always means juice
  of -400 or worse. The subscriber cannot
  profit long term at that juice.
  SKIP these entirely even if the
  probability looks strong.

  The sweet spot is 62-68% with juice
  between -150 and -210.
  This is where mathematical edge meets
  realistic returns.

Casual picks 6-7:
  Range: 55% to 65%
  Entertainment plays.
  Prefer plus money or light juice
  (-130 or better).

═══════════════════════════════════════
JUICE LIMITS — HARD RULES
═══════════════════════════════════════

NEVER recommend any pick with juice
worse than -250. Not even at 70%.

At -300 or worse the bettor needs to
win 75%+ just to break even.
That is not a bet — it is a donation.

JUICE EFFICIENCY RANKING:
  Tier 1 BEST: +100 or better at 55%+
  Tier 2 STRONG: -110 to -180 at 62-68%
  Tier 3 OK: -180 to -220 at 65-68%
  Tier 4 BORDERLINE: -220 to -250 at 67-68%
  Tier 5 NEVER: worse than -250 ever

A -185 line at 63% true probability
is the ideal Mora Assists pick.
A -450 line at 69% is a terrible pick.
Always choose the -185 over the -450.

═══════════════════════════════════════
PROP RULES (Picks 1 and 2)
═══════════════════════════════════════

Minimum no_vig_prob: 60%
Maximum juice: -250
Preferred: 62-68% at DraftKings or FanDuel

Check game environment label:
HIGH SCORING → offensive props
  (hits over, total bases, RBIs,
   shots on goal, points)
LOW SCORING → pitching or defensive props
  (strikeouts over, hits allowed)

Prefer props from favored team players.
Select 2 props from 2 DIFFERENT games.

═══════════════════════════════════════
ANCHOR RULES (Picks 3, 4, and 5)
═══════════════════════════════════════

Anchors = ANY bet that is NOT a player prop.

This includes:
- Moneyline (team to win outright)
- Run line / Puck line / Spread
- Game total (over/under)
- Any game-level market

Check the lines data in the board first.
If lines data is available use it.

If lines data is empty or sparse,
select anchors from high-probability
PROPS above 60% that were not used
in picks 1 or 2.

NEVER return an anchor with:
- odds: 0
- no_vig_prob: 0.0
- team: empty string

If you cannot find 3 qualifying anchors
with real data, use 3 bonus prop picks
instead. Empty picks destroy trust.

Minimum: 60% no-vig probability
Maximum juice: -250 hard limit
Preferred: 62-68% at DK or FD
3 picks from 3 DIFFERENT games.

═══════════════════════════════════════
CASUAL PICKS (Picks 6 and 7)
═══════════════════════════════════════

Entertainment angle. Fun plays.
Casual fans watching the game tonight.

Range: 55% to 65% no-vig probability
Juice: -130 or better preferred
       Never worse than -200 for casual
Books: DraftKings or FanDuel only

Prefer:
- Recognizable star players
  (Judge, Ohtani, McDavid, etc.)
- Primetime or marquee matchups
- Plus money or light juice lines
- Different game from picks 1-5

why_casual must sound like a friend
texting you a pick. One sentence.
Plain language. No math jargon.
Example: "Judge has been on fire at home
and this line is way too cheap tonight."

═══════════════════════════════════════
CONTEXTUAL EDGE REQUIREMENT
═══════════════════════════════════════

Every pick must have a contextual reason
beyond just the probability number.

Consider:
- Is this team at home or away?
- Is it a high or low scoring environment?
- Is the player on the favored team?
- Is there a matchup advantage?
- Is the line undervalued vs the market?

The "why" field must reflect this context.
Not just "62% true probability."
This is: "Yankees at home vs weak Tampa
rotation in a high scoring environment —
market undervaluing the home advantage."

═══════════════════════════════════════
OUTPUT — PURE JSON ONLY
No text. No markdown. Just the JSON.
═══════════════════════════════════════

{
  "picks": [
    {
      "pick_number": 1,
      "type": "prop",
      "player": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "favored_team": "",
      "why": ""
    },
    {
      "pick_number": 2,
      "type": "prop",
      "player": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "favored_team": "",
      "why": ""
    },
    {
      "pick_number": 3,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    },
    {
      "pick_number": 4,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    },
    {
      "pick_number": 5,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }
  ],
  "casual_picks": [
    {
      "pick_number": 6,
      "type": "casual",
      "label": "For The Fans",
      "player": "",
      "team": "",
      "stat": "",
      "line": "",
      "market": "",
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "why_casual": ""
    },
    {
      "pick_number": 7,
      "type": "casual",
      "label": "For The Fans",
      "player": "",
      "team": "",
      "stat": "",
      "line": "",
      "market": "",
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "why_casual": ""
    }
  ],
  "generated_at": "",
  "total_props_scanned": 0,
  "total_lines_scanned": 0,
  "sports_covered": []
}

WHY FIELD — plain English, simple:
Props: mention environment and matchup context.
Anchors: mention matchup and why the line is undervalued.
Casual (why_casual): write like a friend texting — one sentence, conversational, no analytics language.

If fewer than 2 props qualify: replace missing prop with anchor pick.
If fewer than 3 anchors qualify: use bonus prop picks above 60%. Never force empty zeros.
If fewer than 2 casual picks qualify: return only what qualifies. Never force below 55%.
"""


def select_picks_with_llm(board):
    """Send board data to Claude and get 5 curated picks back as JSON."""
    if not ANTHROPIC_API_KEY:
        logger.error("[ASSISTS] No ANTHROPIC_API_KEY")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    board_summary = {
        "props":            board["props"][:100],
        "lines":            board["lines"][:50],
        "sports_available": board["sports_found"],
        "total_props":      len(board["props"]),
        "total_lines":      len(board["lines"]),
        "generated_at":     datetime.utcnow().isoformat(),
    }

    user_message = f"Today's board data:\n{json.dumps(board_summary, indent=2)}"

    raw = ""
    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=SELECTION_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        picks_data = json.loads(raw)

        logger.info(f"[ASSISTS] LLM selected {len(picks_data.get('picks', []))} picks")
        return picks_data

    except json.JSONDecodeError as e:
        logger.error(f"[ASSISTS] JSON parse error: {e}\nRaw: {raw[:500]}")
        return None
    except Exception as e:
        logger.error(f"[ASSISTS] LLM error: {e}")
        return None


# ══════════════════════════════════════════
# STEP 2B: TWO-STAGE LLM PIPELINE
# ══════════════════════════════════════════

def analyze_board_with_llm(board):
    """
    Stage 1 — Claude reads the full board
    and builds a structured cheat sheet.
    Returns a clean analysis dict that
    Stage 2 uses to make picks.
    """
    client = anthropic.Anthropic(
        api_key=os.environ.get('ANTHROPIC_API_KEY')
    )

    props = board.get('props', [])
    lines = board.get('lines', [])

    dk_fd_props = [
        p for p in props
        if p.get('bookmaker', '').lower() in ['draftkings', 'fanduel']
        and p.get('no_vig_prob', 0) >= 55
        and p.get('odds', 0) > -260
    ]

    dk_fd_lines = [
        l for l in lines
        if l.get('bookmaker', '').lower() in ['draftkings', 'fanduel']
    ]

    ANALYST_PROMPT = f"""
You are a professional sports betting 
data analyst — the best in the market.

Your job RIGHT NOW is NOT to pick games.
Your job is to READ the board and build
a clean cheat sheet for the pick selector.

TODAY'S BOARD DATA:

PROPS (DraftKings + FanDuel only, 55%+):
{json.dumps(dk_fd_props[:80], indent=2)}

GAME LINES (DraftKings + FanDuel):
{json.dumps(dk_fd_lines[:40], indent=2)}

ANALYZE the board and return a JSON
cheat sheet with this exact structure:

{{
  "top_props": [
    {{
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "odds": 0,
      "no_vig_prob": 0.0,
      "book": "",
      "matchup": "",
      "sport": "",
      "environment": "",
      "edge_reason": ""
    }}
  ],
  "top_game_lines": [
    {{
      "team": "",
      "market": "",
      "line": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "book": "",
      "matchup": "",
      "sport": "",
      "environment": "",
      "edge_reason": ""
    }}
  ],
  "best_environments": [],
  "avoid_games": [],
  "board_summary": "",
  "total_props_analyzed": 0,
  "total_lines_analyzed": 0,
  "sports_on_board": []
}}

RULES FOR YOUR ANALYSIS:

top_props:
- Only DraftKings or FanDuel
- Only no_vig_prob between 60% and 70%
- Only odds better than -250
- Player name must be a real player name
  NOT a team name like "Twins Batter_Hits"
  If you see "TeamName Batter_Hits" that
  is a prop category NOT a player — SKIP IT
- List top 10 qualifying props ranked
  by probability descending

top_game_lines:
- ONLY moneylines, spreads, or totals
- NOT player props in this section
- Only DraftKings or FanDuel
- Only no_vig_prob between 60% and 70%
- Only odds better than -250
- If no qualifying game lines exist
  return empty array — do not fake them

best_environments:
- List game matchups labeled HIGH SCORING
  These are best for offensive props

avoid_games:
- Any game with fewer than 3 props
  or no qualifying lines

board_summary:
- 2-3 sentence plain English summary
  of what the board looks like today

Return ONLY valid JSON. No markdown.
No explanation. Pure JSON object.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": ANALYST_PROMPT}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()

        analysis = json.loads(raw)

        logger.info(
            f"[ANALYST] Board analyzed: "
            f"{len(analysis.get('top_props', []))} top props, "
            f"{len(analysis.get('top_game_lines', []))} top lines"
        )
        logger.info(f"[ANALYST] Summary: {analysis.get('board_summary', '')}")

        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"[ANALYST] JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"[ANALYST] Analysis error: {e}")
        return None


def select_picks_from_analysis(analysis):
    """
    Stage 2 — Claude reads its own cheat
    sheet from Stage 1 and picks the 7
    best plays with full context.
    """
    if not analysis:
        logger.error("[SELECTOR] No analysis to work from")
        return None

    client = anthropic.Anthropic(
        api_key=os.environ.get('ANTHROPIC_API_KEY')
    )

    SELECTOR_PROMPT = f"""
You are a professional data analyst —
the best in the market for oddsmakers.
You know the right proportions.
You are the Mora Assists pick selector.

A senior analyst has already reviewed 
today's full board and built this 
cheat sheet for you:

CHEAT SHEET:
{json.dumps(analysis, indent=2)}

YOUR JOB: Select exactly 7 picks from
the cheat sheet above.

PICK STRUCTURE:
- Picks 1-2: Player props from top_props
- Picks 3-5: Anchor plays
  USE top_game_lines first.
  If top_game_lines is empty use
  additional props from top_props
  that were NOT used in picks 1-2.
  NEVER use "TeamName Batter_Hits" 
  as an anchor — that is a prop category
  not a real game line.
- Picks 6-7: Casual picks 55-65%
  lighter juice preferred

HARD RULES:
- DraftKings or FanDuel ONLY
- No odds worse than -250 on any pick
- No probability above 70%
- No probability below 60% for picks 1-5
- No probability below 55% for picks 6-7
- All 7 picks must have real data
  odds cannot be 0
  probability cannot be 0.0
  player or team cannot be empty
- Picks 1 and 2 must be PLAYER props
  with a real human player name
- Picks 3-5 prefer game lines
  (moneyline/spread/total) over props
- If no game lines available picks 3-5
  can be bonus props — label them
  type: "prop" not type: "anchor"
- Each pick needs a contextual why
  that references the cheat sheet data

BOARD SUMMARY FROM ANALYST:
{analysis.get('board_summary', '')}

Return ONLY valid JSON matching this
exact structure. No markdown. No text.

{{
  "picks": [
    {{
      "pick_number": 1,
      "type": "prop",
      "player": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }},
    {{
      "pick_number": 2,
      "type": "prop",
      "player": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }},
    {{
      "pick_number": 3,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }},
    {{
      "pick_number": 4,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }},
    {{
      "pick_number": 5,
      "type": "anchor",
      "team": "",
      "market": "",
      "line": "",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "why": ""
    }}
  ],
  "casual_picks": [
    {{
      "pick_number": 6,
      "type": "casual",
      "label": "For The Fans",
      "player": "",
      "team": "",
      "stat": "",
      "line": "",
      "market": "",
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "why_casual": ""
    }},
    {{
      "pick_number": 7,
      "type": "casual",
      "label": "For The Fans",
      "player": "",
      "team": "",
      "stat": "",
      "line": "",
      "market": "",
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "why_casual": ""
    }}
  ],
  "generated_at": "",
  "board_summary": "",
  "sports_covered": []
}}
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            messages=[{"role": "user", "content": SELECTOR_PROMPT}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()

        picks = json.loads(raw)

        logger.info(
            f"[SELECTOR] Picks selected: "
            f"{len(picks.get('picks', []))} core, "
            f"{len(picks.get('casual_picks', []))} casual"
        )

        return picks

    except json.JSONDecodeError as e:
        logger.error(f"[SELECTOR] JSON error: {e}")
        return None
    except Exception as e:
        logger.error(f"[SELECTOR] Error: {e}")
        return None


def run_two_stage_selection(board):
    """
    Convenience function for testing.
    Runs both stages and returns (picks, analysis).
    """
    analysis = analyze_board_with_llm(board)
    if not analysis:
        return None, None
    picks = select_picks_from_analysis(analysis)
    picks = validate_picks(picks)
    return picks, analysis


# ══════════════════════════════════════════
# STEP 3: FORMAT EMAIL
# ══════════════════════════════════════════

def format_picks_email(picks_data):
    """Format LLM picks into clean HTML email."""
    if not picks_data or not picks_data.get("picks"):
        return None, None

    picks   = picks_data["picks"]
    today   = datetime.now().strftime("%A, %B %d")
    props   = [p for p in picks if p["type"] == "prop"]
    anchors = [p for p in picks if p["type"] == "anchor"]

    def fmt_odds(odds):
        if odds > 0:
            return f"+{odds}"
        return str(odds)

    subject = f"⚡ Mora Assists — {today} · {len(picks)} Picks Ready"

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: Inter, Arial, sans-serif; background: #f5faf2; margin: 0; padding: 20px; color: #0f2406; }}
  .container {{ max-width: 600px; margin: 0 auto; background: white; border-radius: 16px; overflow: hidden; border: 2px solid #4cbb17; }}
  .header {{ background: #0f2406; padding: 24px; text-align: center; }}
  .logo {{ color: white; font-size: 28px; font-weight: 900; letter-spacing: 3px; }}
  .logo span {{ color: #4cbb17; }}
  .date {{ color: #6b9e5a; font-size: 13px; margin-top: 4px; }}
  .section-label {{ background: #f5faf2; padding: 12px 24px; font-size: 11px; font-weight: 700; letter-spacing: 2px; color: #6b9e5a; text-transform: uppercase; border-bottom: 1px solid #e8f5e1; }}
  .pick-card {{ padding: 20px 24px; border-bottom: 1px solid #e8f5e1; }}
  .pick-number {{ font-size: 11px; font-weight: 700; color: #6b9e5a; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }}
  .pick-title {{ font-size: 18px; font-weight: 900; color: #0f2406; margin-bottom: 4px; }}
  .pick-detail {{ font-size: 13px; color: #6b9e5a; margin-bottom: 8px; }}
  .pick-stats {{ display: flex; gap: 12px; margin-bottom: 10px; }}
  .stat-box {{ background: #f5faf2; border: 1px solid #e8f5e1; border-radius: 8px; padding: 8px 12px; text-align: center; }}
  .stat-label {{ font-size: 10px; color: #6b9e5a; text-transform: uppercase; letter-spacing: 1px; }}
  .stat-value {{ font-size: 16px; font-weight: 700; color: #0f2406; }}
  .stat-value.green {{ color: #4cbb17; }}
  .why {{ font-size: 13px; color: #1a3d0a; font-style: italic; padding: 10px 12px; background: #f5faf2; border-left: 3px solid #4cbb17; border-radius: 0 8px 8px 0; }}
  .instructions {{ padding: 24px; background: #f5faf2; text-align: center; }}
  .instructions h3 {{ color: #0f2406; font-size: 16px; margin-bottom: 8px; }}
  .instructions p {{ color: #6b9e5a; font-size: 13px; line-height: 1.6; margin: 4px 0; }}
  .cta-btn {{ display: inline-block; background: #4cbb17; color: white; padding: 12px 28px; border-radius: 50px; text-decoration: none; font-weight: 700; font-size: 14px; margin-top: 16px; }}
  .footer {{ padding: 16px 24px; text-align: center; font-size: 11px; color: #a0bf96; border-top: 1px solid #e8f5e1; }}
  .footer a {{ color: #6b9e5a; text-decoration: none; }}
  .env-badge {{ display: inline-block; font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 50px; text-transform: uppercase; letter-spacing: 1px; margin-left: 6px; }}
  .high {{ background: #fef3c7; color: #92400e; }}
  .low  {{ background: #e8f5e1; color: #2d6e0f; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">MORA <span>ASSISTS</span></div>
    <div class="date">{today} · Picks locked at 10:30 AM ET</div>
  </div>
"""

    if props:
        html += '<div class="section-label">🎯 &nbsp; Prop Plays</div>\n'
        for p in props:
            env = p.get("environment", "")
            env_badge = ""
            if "HIGH" in env.upper():
                env_badge = '<span class="env-badge high">High Scoring</span>'
            elif "LOW" in env.upper():
                env_badge = '<span class="env-badge low">Low Scoring</span>'

            html += f"""  <div class="pick-card">
    <div class="pick-number">Pick {p['pick_number']} — Prop · {p.get('sport','')} · {p.get('matchup','')}{env_badge}</div>
    <div class="pick-title">{p.get('player','')} — {p.get('stat','')} OVER {p.get('line','')}</div>
    <div class="pick-detail">Best line at {p.get('book','').title()}</div>
    <div class="pick-stats">
      <div class="stat-box"><div class="stat-label">Odds</div><div class="stat-value">{fmt_odds(p.get('odds', 0))}</div></div>
      <div class="stat-box"><div class="stat-label">True Prob</div><div class="stat-value green">{p.get('no_vig_prob', 0)}%</div></div>
      <div class="stat-box"><div class="stat-label">Sport</div><div class="stat-value">{p.get('sport','')}</div></div>
    </div>
    <div class="why">{p.get('why','')}</div>
  </div>
"""

    if anchors:
        html += '<div class="section-label">⚓ &nbsp; Anchor Plays</div>\n'
        for p in anchors:
            line_str = f" · {p.get('line','')}" if p.get("line") else ""
            html += f"""  <div class="pick-card">
    <div class="pick-number">Pick {p['pick_number']} — Anchor · {p.get('sport','')} · {p.get('matchup','')}</div>
    <div class="pick-title">{p.get('team','')} {p.get('market','').title()}</div>
    <div class="pick-detail">Best line at {p.get('book','').title()}{line_str}</div>
    <div class="pick-stats">
      <div class="stat-box"><div class="stat-label">Odds</div><div class="stat-value">{fmt_odds(p.get('odds', 0))}</div></div>
      <div class="stat-box"><div class="stat-label">True Prob</div><div class="stat-value green">{p.get('no_vig_prob', 0)}%</div></div>
    </div>
    <div class="why">{p.get('why','')}</div>
  </div>
"""

    casual = picks_data.get('casual_picks', [])
    if casual:
        html += """  <div class="section-label" style="background:#fff8e8;padding:12px 24px;font-size:11px;font-weight:700;letter-spacing:2px;color:#854f0b;text-transform:uppercase;border-bottom:1px solid #fac775;border-top:1px solid #fac775;">&#127942; &nbsp; For The Fans</div>\n"""
        for p in casual:
            odds_display = f'+{p.get("odds",0)}' if p.get('odds', 0) > 0 else str(p.get('odds', 0))
            if p.get('player'):
                pick_title = f'{p["player"]} — {p.get("stat","")} OVER {p.get("line","")}'
            else:
                pick_title = f'{p.get("team","")} {p.get("market","").title()}'
            html += f"""  <div style="padding:20px 24px;border-bottom:1px solid #e8f5e1;background:#ffffff;">
    <div style="font-size:11px;font-weight:700;color:#854f0b;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
      Pick {p['pick_number']} &nbsp;·&nbsp; For The Fans &nbsp;·&nbsp; {p.get('sport','')} &nbsp;·&nbsp; {p.get('matchup','')}
    </div>
    <div style="font-size:18px;font-weight:900;color:#0f2406;margin-bottom:4px;font-family:'Arial Black',Arial,sans-serif;">
      {pick_title}
    </div>
    <div style="font-size:13px;color:#6b9e5a;margin-bottom:10px;">Best line at {p.get('book','').title()}</div>
    <div style="display:flex;gap:10px;margin-bottom:10px;">
      <div style="background:#f5faf2;border:1px solid #e8f5e1;border-radius:8px;padding:8px 14px;text-align:center;min-width:70px;">
        <div style="font-size:10px;color:#6b9e5a;text-transform:uppercase;letter-spacing:1px;">Odds</div>
        <div style="font-size:16px;font-weight:700;color:#0f2406;">{odds_display}</div>
      </div>
      <div style="background:#f5faf2;border:1px solid #e8f5e1;border-radius:8px;padding:8px 14px;text-align:center;min-width:80px;">
        <div style="font-size:10px;color:#6b9e5a;text-transform:uppercase;letter-spacing:1px;">Lean</div>
        <div style="font-size:16px;font-weight:700;color:#4cbb17;">{p.get('no_vig_prob',0)}%</div>
      </div>
      <div style="background:#fff8e8;border:1px solid #fac775;border-radius:8px;padding:8px 14px;text-align:center;min-width:80px;">
        <div style="font-size:10px;color:#854f0b;text-transform:uppercase;letter-spacing:1px;">Vibe</div>
        <div style="font-size:13px;font-weight:700;color:#633806;">Casual &#127942;</div>
      </div>
    </div>
    <div style="font-style:italic;font-size:13px;color:#1a3d0a;padding:10px 14px;background:#fff8e8;border-left:3px solid #ef9f27;border-radius:0 8px 8px 0;">
      {p.get('why_casual','')}
    </div>
  </div>
"""
        html += """  <div style="padding:12px 24px;background:#fff8e8;text-align:center;border-bottom:1px solid #e8f5e1;">
    <p style="margin:0;font-size:11px;color:#854f0b;font-style:italic;">
      For The Fans picks are entertainment plays — solid leans, not sharp edges. Bet responsibly. One unit max.
    </p>
  </div>
"""

    html += """  <div class="instructions">
    <h3>How to use these picks</h3>
    <p>Same unit every play. Never chase.</p>
    <p>The math compounds over the season.</p>
    <p>$20 flat per play is all you need.</p>
    <a href="https://morabets.com/dashboard" class="cta-btn">See the Full Board →</a>
  </div>
  <div class="footer">
    Mora Assists · $28.99/month · 3-day free trial<br>
    <a href="#">Manage subscription</a> · <a href="#">Unsubscribe</a>
  </div>
</div>
</body>
</html>
"""
    return subject, html


# ══════════════════════════════════════════
# STEP 4: LOAD SUBSCRIBERS
# ══════════════════════════════════════════

def load_active_subscribers():
    """
    Load active Mora Assists subscribers.
    Auto-expires trials that have ended.
    Auto-removes cancelled subscribers from the send list.
    """
    import csv
    from datetime import datetime

    FILE = '/var/data/mora_assists_subscribers.csv'
    FIELDS = [
        'email', 'name',
        'stripe_customer_id',
        'stripe_subscription_id',
        'status', 'subscribed_at',
        'trial_ends_at', 'cancelled_at',
        'phone', 'sms_opt_in'
    ]

    active   = []
    updated  = False
    all_rows = []
    now      = datetime.utcnow()
    expired  = []
    skipped  = []

    try:
        if not os.path.exists(FILE):
            logger.warning('[ASSISTS] No subscriber file')
            return []

        with open(FILE, 'r') as f:
            rows = list(csv.DictReader(f))

        for row in rows:
            status = row.get('status', '')
            email  = row.get('email', '')

            # Skip cancelled entirely
            if status == 'cancelled':
                skipped.append(email)
                all_rows.append(row)
                continue

            # Check if trial has expired
            if status == 'trial':
                trial_end_str = row.get('trial_ends_at', '')
                if trial_end_str:
                    try:
                        trial_end = datetime.fromisoformat(trial_end_str[:19])
                        if now > trial_end:
                            row['status'] = 'expired'
                            expired.append(email)
                            updated = True
                            all_rows.append(row)
                            continue
                    except Exception:
                        pass

            # Active or valid trial
            if status in ['active', 'trial']:
                active.append(row)

            all_rows.append(row)

        # Write back only if statuses actually changed
        if updated:
            with open(FILE, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS)
                writer.writeheader()
                for row in all_rows:
                    clean = {k: row.get(k, '') for k in FIELDS}
                    writer.writerow(clean)

        if expired:
            logger.info(f'[ASSISTS] Expired trials: {expired}')
        if skipped:
            logger.info(f'[ASSISTS] Skipped cancelled: {skipped}')

        logger.info(f'[ASSISTS] Active subscribers for today: {len(active)}')
        return active

    except Exception as e:
        logger.error(f'[ASSISTS] load_active error: {e}')
        return []


# ══════════════════════════════════════════
# STEP 5: SEND EMAILS
# ══════════════════════════════════════════

def send_picks_email(to_email, subject, html_content):
    """Send one email via SendGrid."""
    try:
        sg      = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
        message = Mail(from_email=FROM_EMAIL, to_emails=to_email, subject=subject, html_content=html_content)
        response = sg.send(message)
        return response.status_code in [200, 201, 202]
    except Exception as e:
        logger.error(f"[ASSISTS] Send failed {to_email}: {e}")
        return False


def send_trial_expiry_warning(email, name):
    """
    Sends a retention email ~24 hours before trial ends.
    Called from run_daily_assists() when trial_ends_at is within 20-28 hours.
    """
    first = name.split()[0] if name else 'there'
    subject = "Your Mora Assists trial ends tomorrow."
    html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#f5faf2;font-family:Inter,Arial,sans-serif;">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:16px;border:2px solid #4cbb17;overflow:hidden;">
  <div style="background:#0f2406;padding:24px;text-align:center;">
    <div style="font-family:'Arial Black',Arial,sans-serif;font-size:22px;font-weight:900;color:#fff;letter-spacing:3px;">
      MORA <span style="color:#4cbb17;">ASSISTS</span>
    </div>
  </div>
  <div style="padding:32px;">
    <h1 style="font-size:20px;font-weight:900;color:#0f2406;margin:0 0 12px;">
      Hey {first} — your trial ends tomorrow.
    </h1>
    <p style="font-size:15px;color:#1a3d0a;line-height:1.7;margin:0 0 16px;">
      You've been getting 7 picks every morning — 2 props, 3 anchors, 2 casual plays.
    </p>
    <p style="font-size:15px;color:#1a3d0a;line-height:1.7;margin:0 0 24px;">
      Tomorrow at midnight your trial ends. If you want picks to keep landing
      in your inbox through baseball season — lock it in today.
    </p>
    <div style="text-align:center;margin:24px 0;">
      <a href="https://buy.stripe.com/fZucMY6GX2Hfat4bP74Vy05"
         style="display:inline-block;background:#4cbb17;color:#0f2406;font-family:'Arial Black',Arial,sans-serif;font-size:16px;font-weight:900;text-decoration:none;padding:16px 36px;border-radius:50px;">
        Keep My Picks — $28.99/mo →
      </a>
    </div>
    <p style="font-size:13px;color:#6b9e5a;text-align:center;margin:0;">
      Cancel anytime. No questions asked.
    </p>
  </div>
  <div style="background:#f5faf2;padding:16px;text-align:center;border-top:1px solid #e8f5e1;">
    <p style="margin:0;font-size:11px;color:#a0bf96;">
      Mora Assists · picks@morabets.com ·
      <a href="https://morabets.com/unsubscribe/assists?email={email}" style="color:#6b9e5a;">Unsubscribe</a>
    </p>
  </div>
</div>
</body>
</html>"""
    try:
        client = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
        msg = Mail(from_email=FROM_EMAIL, to_emails=email, subject=subject, html_content=html)
        client.send(msg)
        logger.info(f'[ASSISTS] Trial warning sent: {email}')
    except Exception as e:
        logger.error(f'[ASSISTS] Warning email failed: {e}')


# ══════════════════════════════════════════
# STEP 5.5: POST-SELECTION VALIDATION
# ══════════════════════════════════════════

def validate_picks(picks_data):
    """
    Remove any picks that violate hard rules before sending email.
    Prevents -500 juice picks or empty zero-data picks from going out.
    """
    if not picks_data:
        return picks_data

    clean_picks = []
    removed = 0

    for p in picks_data.get('picks', []):
        odds  = p.get('odds', 0)
        prob  = p.get('no_vig_prob', 0)
        ptype = p.get('type', '')

        # Hard reject: no data at all
        if odds == 0 and prob == 0.0:
            logger.warning(
                f"[VALIDATE] Removed empty pick {p.get('pick_number')} "
                f"(odds=0, prob=0)"
            )
            removed += 1
            continue

        # Hard reject: juice worse than -250
        if odds < -250:
            logger.warning(
                f"[VALIDATE] Removed pick {p.get('pick_number')} — "
                f"juice {odds} exceeds -250 limit"
            )
            removed += 1
            continue

        # Hard reject: probability above 70% for core picks
        if ptype != 'casual' and prob > 70.0:
            logger.warning(
                f"[VALIDATE] Removed pick {p.get('pick_number')} — "
                f"prob {prob}% above 70% ceiling"
            )
            removed += 1
            continue

        # Hard reject: probability below 60% for core picks
        if ptype != 'casual' and prob < 60.0:
            logger.warning(
                f"[VALIDATE] Removed pick {p.get('pick_number')} — "
                f"prob {prob}% below 60% floor"
            )
            removed += 1
            continue

        clean_picks.append(p)

    if removed > 0:
        logger.warning(
            f"[VALIDATE] Removed {removed} picks that failed quality check"
        )

    picks_data['picks'] = clean_picks
    return picks_data


# ══════════════════════════════════════════
# STEP 6: MAIN DAILY JOB
# ══════════════════════════════════════════

def run_daily_assists():
    """Main job called by scheduler at 10:30 AM ET. Loads board, selects picks, sends emails."""
    logger.info("[ASSISTS] Starting daily run...")

    board = load_full_board()

    if not board["props"] and not board["lines"]:
        logger.warning("[ASSISTS] Board empty — no picks to send today")
        return

    # Stage 1 — Analyst builds cheat sheet
    logger.info("[ASSISTS] Stage 1: Analyzing board...")
    analysis = analyze_board_with_llm(board)

    if not analysis:
        logger.error(
            "[ASSISTS] Stage 1 failed — "
            "no analysis returned"
        )
        return {"sent": 0, "failed": 0, "error": "analysis"}

    logger.info(
        f"[ASSISTS] Analyst summary: "
        f"{analysis.get('board_summary', '')}"
    )

    # Stage 2 — Selector picks from cheat sheet
    logger.info("[ASSISTS] Stage 2: Selecting picks...")
    picks_data = select_picks_from_analysis(analysis)

    # Validate picks
    picks_data = validate_picks(picks_data)

    if not picks_data:
        logger.error("[ASSISTS] LLM returned no picks")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    save_path = f"assists_picks_{today_str}.json"
    try:
        with open(save_path, "w") as f:
            json.dump(picks_data, f, indent=2)
        logger.info(f"[ASSISTS] Picks saved to {save_path}")
    except Exception as se:
        logger.warning(f"[ASSISTS] Could not save picks JSON: {se}")

    subject, html = format_picks_email(picks_data)
    if not subject or not html:
        logger.error("[ASSISTS] Email format failed")
        return

    subscribers = load_active_subscribers()
    logger.info(f"[ASSISTS] Sending to {len(subscribers)} subscribers")

    if not subscribers:
        logger.warning("[ASSISTS] No active subscribers yet")
        return

    # Send trial expiry warning to anyone whose trial ends in 20-28 hours
    for sub in subscribers:
        trial_end_str = sub.get('trial_ends_at', '')
        if not trial_end_str:
            continue
        try:
            trial_end  = datetime.fromisoformat(trial_end_str[:19])
            hours_left = (trial_end - datetime.utcnow()).total_seconds() / 3600
            if 20 <= hours_left <= 28:
                send_trial_expiry_warning(
                    sub.get('email', ''),
                    sub.get('name', '')
                )
                logger.info(
                    f'[ASSISTS] Trial warning: {sub.get("email")} '
                    f'({hours_left:.0f}hrs left)'
                )
        except Exception:
            continue

    sent   = 0
    failed = 0
    for sub in subscribers:
        email = sub.get("email")
        if not email:
            continue
        if send_picks_email(email, subject, html):
            sent += 1
        else:
            failed += 1

    logger.info(f"[ASSISTS] Sent: {sent} Failed: {failed}")
    return {"sent": sent, "failed": failed, "picks": len(picks_data.get("picks", []))}
