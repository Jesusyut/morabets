# Mora Bets - Sports Betting Analytics Platform

## Overview
Mora Bets is a free sports betting analytics platform for MLB, NHL, and NFL. It provides real-time odds comparison, EV (expected value) analysis, and player prop evaluation with no paywall, no login, and no subscription required. The platform is fully open — any visitor can access the dashboard and all tools.

### Recent Updates (June 2026) — Context Edge Analyst (additive)
- **Context Edge button**: A subtle "🔍 Context Edge" button on every prop/game-line card opens an inline overlay with an AI second-opinion on the bet
- **Endpoint**: New `POST /api/context-edge` calls Anthropic Claude (`claude-sonnet-4-5-20250929`) and returns a structured JSON verdict (PLAY/SMALL PLAY/PASS, confidence, context/price scores, probabilities, supporting/against context, missing data, unit size, "what would make this a pass")
- **Re-analyze with context**: Users can add their own notes (injury/weather/lineup) and re-run the analysis
- **Safety**: public endpoint is per-IP rate-limited (15/min) with a 30s timeout; untrusted model output is rendered text-only (no innerHTML); errors are generic
- Fully additive — no existing route, data pipeline, scheduler, or UI behavior was changed

### Recent Updates (April 2026) — Premium Rebuild & Paywall Removal
- **Paywall fully removed**: All Stripe/token/license-key code deleted from `app.py`. Dashboard is open to all visitors
- **Email capture**: `/api/subscribe` + `save_subscriber()` saves emails to `email_subscribers.json`; slide-in modal in dashboard (45s delay / 60% scroll, once per session, skips <375px)
- **New dashboard design**: Dark navy `#091f35`, Tailwind CDN + Google Fonts (Barlow Condensed, Space Grotesk, Inter), gold `#facc15` accents, sticky bottom bar with rotating affiliate messages
- **Best Lines board**: Two sections — ⚡ Edge Picks (gold-bordered, EV+ only) + 📊 Today's Lines (all no-vig cards)
- **Affiliate config**: `static/affiliate_config.js` — single source of truth for FanDuel, DraftKings, BetMGM, Caesars URLs
- **How It Works page**: Updated to free-tool messaging, dark nav matching dashboard, all `/paywall` links replaced with `/dashboard`

### Recent Updates (March 2026) — EV Engine Overhaul
- **EV Engine**: `ev_engine.py` with book weights (sharp/standard/soft), weighted fair probability, `evaluate_pick()`, EV%, edge%, break-even threshold
- **CLV Tracker**: `clv_tracker.py` — logs every surfaced pick (entry odds, fair probability, EV%), supports `update_closing_line`, `record_result`, `get_performance_report`
- **Player Props**: `group_props_by_player` uses EV engine; only `passes_threshold=True` picks surfaced; `sort_props_by_tier` sorts by EV%
- **Best Lines (MLB/NHL)**: `/api/mlb/odds` and `/api/nhl/odds` collect all books per game/market first, then call `evaluate_pick()` once per side; CLV logging on every surfaced pick
- **Bookmakers expanded**: 9 books (draftkings, fanduel, betmgm, caesars, pointsbetus, betrivers, bovada, betonlineag, fanatics) across all APIs
- **Tier labels**: LOCK (≥6% EV), FIRE (≥3% EV), EDGE (marginal positive EV) — LOW replaced
- **Dashboard UI**: EV%, fair odds, break-even% shown on every card; board summary is EV-based with info tooltip
- **Performance API**: `/api/performance` returns CLV/ROI report; `/api/performance/log` returns raw log

### Recent Updates (August 2025)
- **NFL UI Integration**: Added complete MLB/NFL sport switching tabs in dashboard with proper navigation flow
- **MLB Game Context Enrichment**: Implemented advanced game-level context analysis for MLB props, identifying favorable environments for OVER props based on team trends, pitcher matchups, and opponent weaknesses
- **Enhanced Props API**: New `/api/mlb/props/enhanced` endpoint provides deep context analysis with edge calculation
- **NFL Environment Classification**: Fully operational NFL environment API with 272+ games classified as High Scoring (≥50), Low Scoring (≤42), or Neutral
- **Favored Team Highlighting**: Complete integration for both MLB and NFL with bright green (#00FF95) highlighting and sport-specific environment endpoints
- **Production-Ready NFL API**: Updated NFL odds API with proper market normalization, error handling, and The Odds API v4 compliance
- **NFL Off-Season Handling**: Implemented graceful off-season error handling with friendly user messages and proper API response formatting

## User Preferences
Preferred communication style: Simple, everyday language.

## System Architecture
### Backend
- **Framework**: Flask (Python 3.x)
- **Architecture Pattern**: Monolithic web application with API endpoints.
- **Deployment**: Configured for Replit with Gunicorn WSGI server.
- **Data Processing**: Modules for odds API integration, probability calculations (implied probability, edge, Kelly criterion), contextual player statistics, and fantasy hit rates.
- **Key Features**: Odds analysis, player prop analysis, probability calculations, contextual player performance analysis, and fantasy integration for both MLB and NFL.
- **Multi-Sport Support**: Complete NFL support with production-ready API integration, environment classification, and favored team identification. NFL-specific thresholds and market validation ensure accurate data processing.
- **Caching**: File-based caching (`mlb_props_cache.json`) for persistent data storage, replacing Redis.
- **Background Processing**: APScheduler for automated data refresh (hourly for player props, twice daily for core updates).
- **AI Integration**: OpenAI (GPT-4o-mini) for "Today's Picks" feature, generating parlay recommendations with contextual reasoning.
- **Matchup Validation**: Strict player-team validation ensures accurate prop grouping by MLB matchups.
- **MLB Game Context Engine**: Advanced enrichment layer analyzing team form, pitcher matchups, offensive splits, and bullpen context to identify favorable betting environments with confidence scoring.
- **NFL Environment Classification**: Real-time game environment analysis with NFL-specific scoring thresholds, favored team identification via moneyline analysis, and comprehensive 272+ game coverage.

### Frontend
- **Template Engine**: Jinja2 (Flask's default templating).
- **UI Framework**: Bootstrap 5 with a dark theme.
- **JavaScript**: Vanilla JavaScript for dynamic interactions.
- **Styling**: Custom CSS complements Bootstrap utilities.
- **UI/UX Decisions**: PrizePicks-style interface for player props, professional tabbed navigation (Moneylines, Player Props, Today's Picks, How to Profit), color-coded confidence indicators, mobile responsiveness, user key sign-in functionality, and sport-specific favored team highlighting with bright green (#00FF95) glow effects.
- **Key Features**: Comprehensive search and filtering for player props (player name, stat type, confidence level, sportsbook), educational "How to Profit" section, conversion-optimized landing page with single $9.99 pricing tier, and streamlined user experience focused on the core betting tool.

### System Design
- **Data Flow**: Data ingested from external APIs, processed through analytics modules, cached, and served via Flask endpoints.
- **Robustness**: Implemented robust error handling, graceful degradation (e.g., when AI services are unavailable), and persistent file-based caching for high availability.
- **Security**: Access control via license key verification, integrated with Stripe for subscription management.
- **Scalability**: Optimized API calls through smart filtering, batch processing, and scheduled updates to manage load.
- **SEO**: Comprehensive SEO metadata, Open Graph tags, Twitter Cards, and JSON-LD schema implemented.

## External Dependencies
- **The Odds API**: Primary source for MLB betting odds and lines.
- **MLB Stats API**: Official MLB statistics for player performance data.
- **OpenAI API**: Used for AI-powered "Today's Picks" feature.
- **Stripe**: Payment gateway for subscription management and license key generation.
- **Bootstrap CDN**: Frontend styling and components.
- **Font Awesome**: Icons for the user interface.
- **Meta Pixel**: For analytics and conversion tracking.
- **Python Libraries**: Flask, APScheduler, Requests, Flask-CORS, and other standard libraries for web development and data processing.