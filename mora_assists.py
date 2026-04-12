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
        ('/var/data/mlb_props_cache.json', 'MLB'),
        ('/var/data/nhl_props_cache.json', 'NHL'),
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
You are a sharp sports betting analyst.
Your job is to find 5 player props
with genuine contextual edge today.

You have access to today's full props board
including no-vig probability, odds, matchup,
sport, and game environment labels.

The board contains MLB and NHL props.
You must treat both sports equally.
Never default to baseball only.
Always scan all sports on the board
before selecting any picks.

You are selecting picks for subscribers
who want to know WHICH prop to bet and WHY
in plain simple language.

═══════════════════════════════════════
HOW TO FIND CONTEXTUAL EDGE
═══════════════════════════════════════

You build edge from what the data tells you.
No external sources needed.
The lines themselves contain the signal.

READ THE ENVIRONMENT LABEL FIRST:

HIGH SCORING game →
  Offensive props have natural tailwind

  MLB: hits over, total bases over,
  RBIs over, home runs, runs scored over

  NHL: goals over, points over,
  shots on goal over,
  power play points over

  Avoid in both sports:
  under props for offensive players
  in high scoring environments

LOW SCORING game →
  Pitching and defensive props win

  MLB: strikeouts over,
  hits allowed under,
  earned runs under

  NHL: shots on goal under,
  goalie save props,
  low total props

  Avoid: offensive over props when
  environment suppresses scoring

No label →
  Look at the odds movement signal.
  A prop sitting at -150 or heavier
  means the market has already moved
  toward that outcome. Respect it.

READ THE MATCHUP:

MLB — Home team batter vs road pitcher:
  Home advantage is real.
  Batter props on home favorites
  in high scoring environments
  are your highest percentage plays.

MLB — Road team starter in a dome:
  No weather factor. Pure stuff.
  If the park suppresses offense
  and the pitcher has a favorable
  matchup, strikeout props are clean.

NHL — Home team in a playoff race:
  Home ice matters more late in
  the season. Star player points
  and shots props carry more value
  when the team is desperate to win.

NHL — Back to back games:
  Teams on back to backs fatigue.
  Under props and goalie props
  gain value when a team played
  last night.

READ THE ODDS THEMSELVES:

A -130 prop that strips to 61% no-vig
means the book and the market both
agree this hits more than half the time
and you are getting paid fairly.
That is a solid play.

A -220 prop that strips to 64% no-vig
means you need to win 69% just to break
even on the juice. Not worth it.
Skip even if the context is good.

A +100 or better prop above 57% no-vig
is your best find on any slate.
Plus money with majority probability
is rare. Take it immediately.

═══════════════════════════════════════
SELECTION RULES — ALL HARD LIMITS
═══════════════════════════════════════

Books: DraftKings or FanDuel ONLY

Juice limit: -220 maximum
  Worse than -220 = never touch it

No-vig probability: 57% minimum
  Below 57% = skip regardless of context
  Above 70% = juice is -300 or worse,
  skip regardless of probability

Player name: Must be a real human name
  Never select "TeamName Batter_Hits"
  Never select "TeamName Player_Points"
  Never select team-level aggregates
  If the player field contains a team
  name or an underscore category —
  skip it entirely

Valid MLB prop types:
  Player hits, player total bases,
  player home runs, player RBIs,
  player strikeouts (pitchers only),
  player runs scored

Valid NHL prop types:
  Player goals, player points,
  player shots on goal,
  player power play points,
  player assists

Diversity:
  5 picks from at least 3 different games
  Never more than 2 picks from same game
  When both MLB and NHL are on the board
  include at least 1 NHL prop
  and at least 1 MLB prop
  Never all 5 from one sport

Plus money priority:
  +100 or better at 57%+ = take it first
  This is rare and always the best pick
  on the board when it exists

═══════════════════════════════════════
THE ONE_LINER — THIS IS THE PRODUCT
═══════════════════════════════════════

The one_liner is what the subscriber
actually pays for.

It must answer one question:
WHY does this prop hit TODAY specifically.

Not: "62% true probability at -150"
That is data. Not a reason.

MLB examples:
"Freeman bats cleanup in a high
scoring dome game against a starter
giving up 1.4 hits per inning on
the road this season — this line
is cheap for what it is."

"Burnes gets a lineup ranked bottom 5
in contact rate. Low scoring park,
his strikeout prop has been under-
priced three starts running."

NHL examples:
"McDavid at home in a must-win game —
he has recorded a point in 9 of his
last 11 home starts and this line
opened 20 cents cheaper yesterday."

"Eichel gets a back-to-back opponent
on short rest — Vegas at home and
the matchup is as clean as it gets
on tonight's slate."

The context comes from:
- Sport (MLB vs NHL — always specify)
- Environment label (HIGH/LOW SCORING)
- Home vs away in matchup field
- The odds themselves — heavy juice
  means the market agrees with you
- Game importance if visible in data

One sentence. Plain English.
Reads like a sharp friend texting you.
Never sounds like a robot.
Never restates the odds or probability.
Always references the sport context.

═══════════════════════════════════════
OUTPUT — PURE JSON ONLY
No markdown. No text before or after.
═══════════════════════════════════════

{
  "picks": [
    {
      "pick_number": 1,
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "one_liner": ""
    },
    {
      "pick_number": 2,
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "one_liner": ""
    },
    {
      "pick_number": 3,
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "one_liner": ""
    },
    {
      "pick_number": 4,
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "one_liner": ""
    },
    {
      "pick_number": 5,
      "player": "",
      "team": "",
      "stat": "",
      "line": 0,
      "direction": "over",
      "book": "",
      "odds": 0,
      "no_vig_prob": 0.0,
      "sport": "",
      "matchup": "",
      "environment": "",
      "one_liner": ""
    }
  ],
  "board_summary": "",
  "generated_at": "",
  "sports_covered": [],
  "total_props_scanned": 0
}

board_summary:
  2 sentences max.
  What sports and environments are
  on the board today.
  Always mention both MLB and NHL
  when both are active.
  Example: "14 MLB games and 6 NHL
  games on tonight's slate.
  High scoring environments across
  both sports — offensive props
  are the primary angle today."
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

    picks = picks_data.get("picks", [])
    today = datetime.now().strftime("%A, %B %d")

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
  .one-liner {{ font-size: 13px; color: #1a3d0a; font-style: italic; padding: 10px 12px; background: #f5faf2; border-left: 3px solid #4cbb17; border-radius: 0 8px 8px 0; }}
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

    html += '<div class="section-label">⚡ &nbsp; Today\'s Prop Plays</div>\n'

    for p in picks:
        env = p.get("environment", "")
        env_badge = ""
        if "HIGH" in env.upper():
            env_badge = '<span class="env-badge high">High Scoring</span>'
        elif "LOW" in env.upper():
            env_badge = '<span class="env-badge low">Low Scoring</span>'

        html += f"""  <div class="pick-card">
    <div class="pick-number">Pick {p['pick_number']} · {p.get('sport','')} · {p.get('matchup','')}{env_badge}</div>
    <div class="pick-title">{p.get('player','')} — {p.get('stat','')} OVER {p.get('line','')}</div>
    <div class="pick-detail">Best line at {p.get('book','').title()}</div>
    <div class="pick-stats">
      <div class="stat-box"><div class="stat-label">Odds</div><div class="stat-value">{fmt_odds(p.get('odds', 0))}</div></div>
      <div class="stat-box"><div class="stat-label">True Prob</div><div class="stat-value green">{p.get('no_vig_prob', 0)}%</div></div>
      <div class="stat-box"><div class="stat-label">Sport</div><div class="stat-value">{p.get('sport','')}</div></div>
    </div>
    <div class="one-liner">{p.get('one_liner','')}</div>
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
