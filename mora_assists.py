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
    As new sports are added they will be picked up automatically.
    """
    board = {"props": [], "lines": [], "sports_found": []}

    mlb_props = load_props_from_file("mlb_props_cache.json")
    if mlb_props:
        board["props"].extend(mlb_props)
        board["sports_found"].append("MLB")

    nhl_props = load_props_from_file("nhl_props_cache.json")
    if nhl_props:
        board["props"].extend(nhl_props)
        if "NHL" not in board["sports_found"]:
            board["sports_found"].append("NHL")

    try:
        nfl_props = load_props_from_file("nfl_props_cache.json")
        if nfl_props:
            board["props"].extend(nfl_props)
            board["sports_found"].append("NFL")
    except Exception:
        pass

    for cache_file in ["mlb_lines_cache.json", "nhl_lines_cache.json", "nfl_lines_cache.json"]:
        try:
            lines = load_props_from_file(cache_file)
            if lines:
                board["lines"].extend(lines)
        except Exception:
            pass

    board["props"] = [p for p in board["props"] if p.get("no_vig_prob", 0) >= 55]
    board["lines"] = [l for l in board["lines"] if l.get("no_vig_prob", 0) >= 60]

    logger.info(
        f"[ASSISTS] Board loaded: {len(board['props'])} props, "
        f"{len(board['lines'])} lines, sports: {board['sports_found']}"
    )
    return board


# ══════════════════════════════════════════
# STEP 2: LLM PICK SELECTION
# ══════════════════════════════════════════

SELECTION_PROMPT = """
You are the Mora Assists pick selector.

Every morning you receive the full Mora Bets board — every sport, every market, every prop.
Analyze everything. Do not limit to one sport.

YOUR OUTPUT IS EXACTLY 5 PICKS:
- 2 player props (context-driven)
- 3 anchor lines (no-vig straight plays)

═══════════════════════════════════════
PROP RULES (Picks 1 and 2)
═══════════════════════════════════════

Minimum no_vig_prob: 55

For every game check environment label:

HIGH SCORING:
→ Offensive props only
→ Hits over, total bases over, RBIs over, shots on goal over, receiving yards over
→ Prefer favored team offensive players

LOW SCORING:
→ Pitching or defensive props only
→ Pitcher strikeouts over
→ Avoid offensive over totals

NO environment label:
→ Skip this game for props

Select 2 props from 2 DIFFERENT games.
Never 2 props from the same game.
Prefer highest no_vig_prob across all sports.

═══════════════════════════════════════
ANCHOR RULES (Picks 3, 4, and 5)
═══════════════════════════════════════

Minimum no_vig_prob: 60
Hard ceiling: -220 juice maximum ever

Juice efficiency — rank by less juice for same probability always:

TIER 1 BEST — positive odds above 55%
  +105 at 61% = instant yes

TIER 2 STRONG: -110 to -160 at 60-64% = yes
TIER 3 YES:    -160 to -200 at 63-67% = yes
TIER 4 BORDERLINE: -200 at 60% = last resort only
TIER 5 NEVER: Above -220 regardless of probability

3 anchors from 3 different games.
No game used in prop picks 1 or 2.
Spread across sports when possible.

═══════════════════════════════════════
OUTPUT — PURE JSON ONLY
No explanation. No markdown. Just JSON.
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
  "generated_at": "",
  "total_props_scanned": 0,
  "total_lines_scanned": 0,
  "sports_covered": []
}

WHY FIELD — plain English, simple:
Props: mention environment and matchup — "High scoring game. Best hitter on the favored team. Clear play."
Anchors: mention probability and juice — "62% true edge at -160. Market strongly favors this team."

If fewer than 2 props qualify: replace missing prop with anchor pick.
If fewer than 3 anchors qualify: send only what passes the rules. Never force a bad pick.
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
    """Load all active Mora Assists subscribers (status: active or trial)."""
    import csv
    subscribers = []
    try:
        if os.path.exists("mora_assists_subscribers.csv"):
            with open("mora_assists_subscribers.csv") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("status") in ["active", "trial"]:
                        subscribers.append(row)
    except Exception as e:
        logger.error(f"[ASSISTS] Load subscribers: {e}")
    return subscribers


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

    picks_data = select_picks_with_llm(board)

    if not picks_data:
        logger.error("[ASSISTS] LLM returned no picks")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    with open(f"assists_picks_{today_str}.json", "w") as f:
        json.dump(picks_data, f, indent=2)

    logger.info(f"[ASSISTS] Picks saved to assists_picks_{today_str}.json")

    subject, html = format_picks_email(picks_data)
    if not subject or not html:
        logger.error("[ASSISTS] Email format failed")
        return

    subscribers = load_active_subscribers()
    logger.info(f"[ASSISTS] Sending to {len(subscribers)} subscribers")

    if not subscribers:
        logger.warning("[ASSISTS] No active subscribers yet")
        return

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
