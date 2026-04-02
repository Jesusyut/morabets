import os
import json
import logging
import time
import random
import requests
import sys
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    stream=sys.stdout)
logger = logging.getLogger("app")

from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_cors import CORS
from redis import Redis
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.middleware.proxy_fix import ProxyFix

from odds_api import fetch_player_props, parse_game_data, group_props_by_player, get_quota
from enrichment import load_props_from_file
from probability import implied_probability, calculate_edge, kelly_bet_size, calculate_parlay_edge, sort_props_by_tier
from prop_deduplication import deduplicate_props_by_player, get_stat_display_name, get_player_avatar_url

from team_abbreviations import get_team_abbreviation, format_matchup, TEAM_ABBREVIATIONS

# NFL modules
from nfl_odds_api import fetch_nfl_props
from nfl_enrichment import enrich_nfl_props
from nfl_contextual import add_nfl_context
from nfl_game_enrichment import build_nfl_environment_map, enrich_nfl_props_with_context

# MLB game context enrichment
from mlb_game_enrichment import enrich_mlb_props_with_context, filter_positive_environment_props

# Configure logging - reduce external API noise
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Disable debug logging for external APIs
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "mora-bets-secret-key-change-in-production")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app)


SUBSCRIBERS_FILE = 'email_subscribers.json'


def save_subscriber(email):
    """Append an email address to the subscribers file."""
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, 'r') as f:
                subs = json.load(f)
        else:
            subs = []
        if email not in subs:
            subs.append(email)
            with open(SUBSCRIBERS_FILE, 'w') as f:
                json.dump(subs, f, indent=2)
            logger.info(f'[SUBSCRIBE] Added {email}')
    except Exception as e:
        logger.error(f'[SUBSCRIBE] Error saving subscriber {email}: {e}')


# Redis configuration with robust stability features
redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis = None
memory_cache = {}  # In-memory fallback cache
redis_healthy = False
redis_last_check = 0

# ---- NFL prop filtering (FanDuel + odds gate) ----
VALID_BOOK_TITLES = {"fanduel"}   # case-insensitive match on bookmaker.title
ODDS_MIN, ODDS_MAX = -300, 250

def _valid_price(p):
    try:
        if p is None: 
            return False
        return ODDS_MIN <= int(p) <= ODDS_MAX
    except Exception:
        return False
      
def init_redis():
    """Initialize Redis connection with proper ping validation"""
    global redis, redis_healthy
    
    try:
        redis = Redis.from_url(redis_url)
        redis.ping()  # confirms active connection
        redis_healthy = True
        print("✅ Connected to Redis successfully")
        logger.info(f"✅ Connected to Redis at {redis_url}")
        return True
    except Exception as e:
        print("⚠️ Redis connection failed, using in-memory cache:", e)
        logger.warning(f"❌ Failed to connect to Redis URL {redis_url}: {e}")
        try:
            # Fallback to local Redis
            redis = Redis(host='localhost', port=6379, db=0)
            redis.ping()
            redis_healthy = True
            print("✅ Connected to local Redis successfully")
            logger.info("✅ Connected to local Redis at localhost:6379")
            return True
        except Exception as e2:
            print("⚠️ Local Redis connection failed, using in-memory cache:", e2)
            logger.warning(f"❌ Failed to connect to local Redis: {e2}")
            logger.info("🔄 Using in-memory cache as fallback")
            redis = None  # fallback flag
            redis_healthy = False
            return False

def check_redis_health():
    """Check Redis health and attempt reconnection if needed"""
    global redis_healthy, redis_last_check
    import time
    
    current_time = time.time()
    # Check every 30 seconds
    if current_time - redis_last_check < 30:
        return redis_healthy
    
    redis_last_check = current_time
    
    if redis:
        try:
            redis.ping()
            if not redis_healthy:
                logger.info("✅ Redis connection restored")
            redis_healthy = True
            return True
        except Exception as e:
            if redis_healthy:
                logger.warning(f"❌ Redis connection lost: {e}")
            redis_healthy = False
            # Attempt reconnection
            logger.info("🔄 Attempting Redis reconnection...")
            return init_redis()
    else:
        # No Redis connection, try to establish one
        logger.info("🔄 Attempting initial Redis connection...")
        return init_redis()

# Initialize Redis on startup
init_redis()

# Cache helper functions with enhanced stability and timeouts
def cache_set(key, value, timeout=3):
    """Set cache value with Redis or memory fallback - non-blocking"""
    # Check Redis health periodically
    check_redis_health()
    
    if redis and redis_healthy:
        try:
            # Use pipeline for better performance and atomicity
            pipe = redis.pipeline()
            pipe.set(key, value)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning(f"Redis set failed for key {key}: {e}")
            # Fall back to memory cache
            memory_cache[key] = value
            return False
    else:
        # Always store in memory cache as fallback
        memory_cache[key] = value
        return False

def cache_get(key, timeout=3):
    """Get cache value with Redis or memory fallback - non-blocking"""
    # Check Redis health periodically
    check_redis_health()
    
    if redis and redis_healthy:
        try:
            # Try Redis first
            value = redis.get(key)
            if value is not None:
                return value
            # If not in Redis, check memory cache
            return memory_cache.get(key)
        except Exception as e:
            logger.warning(f"Redis get failed for key {key}: {e}")
            # Fall back to memory cache
            return memory_cache.get(key)
    else:
        # Use memory cache only
        return memory_cache.get(key)

def cache_incr(key, timeout=3):
    """Increment cache value with Redis or memory fallback - non-blocking"""
    # Check Redis health periodically
    check_redis_health()
    
    if redis and redis_healthy:
        try:
            result = redis.incr(key)
            # Also update memory cache for consistency
            memory_cache[key] = result
            return result
        except Exception as e:
            logger.warning(f"Redis incr failed for key {key}: {e}")
            # Fall back to memory cache
            memory_cache[key] = memory_cache.get(key, 0) + 1
            return memory_cache[key]
    else:
        # Use memory cache only
        memory_cache[key] = memory_cache.get(key, 0) + 1
        return memory_cache[key]

def cache_exists(key, timeout=3):
    """Check if cache key exists - non-blocking"""
    # Check Redis health periodically
    check_redis_health()
    
    if redis and redis_healthy:
        try:
            return redis.exists(key) or key in memory_cache
        except Exception as e:
            logger.warning(f"Redis exists failed for key {key}: {e}")
            return key in memory_cache
    else:
        return key in memory_cache

@app.route("/")
def home():
    """Redirect to dashboard"""
    return redirect(url_for("dashboard"))




@app.route('/admin/health')
def admin_health():
    """Admin health check — shows system status."""
    admin_key = request.args.get('key')
    if admin_key != os.environ.get('ADMIN_KEY', 'mora-admin-2026'):
        return jsonify({'error': 'unauthorized'}), 401
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, 'r') as f:
                subs = json.load(f)
        else:
            subs = []
    except Exception:
        subs = []
    return jsonify({
        'status': 'running',
        'email_subscribers': len(subs),
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route("/dashboard")
def dashboard():
    """Main Mora Bets dashboard — open to all users"""
    try:
        hits = cache_incr("hits")
        return render_template("dashboard.html", hits=hits)
    except Exception as e:
        logger.error(f"Error in dashboard route: {e}")
        return f'<h1>Mora Bets</h1><p>Error: {str(e)}</p><p><a href="/health">Health Check</a></p>'


@app.before_request
def before_request():
    """Allow all requests — no paywall"""
    pass

@app.route("/health")
def health():
    """Health check endpoint - instant response"""
    return jsonify({"health": "live"}), 200

@app.route("/status")
def status():
    """Simple status endpoint for health checks"""
    return jsonify({"status": "OK"}), 200

@app.route("/logout")
def logout():
    """Clear session and redirect to dashboard"""
    session.clear()
    return redirect(url_for("dashboard"))

# Removed extract_team_abbreviation function - now using team_abbreviations.py module

def group_props_by_matchup(props_data):
    """Group player props by actual team matchups using real MLB data"""
    try:
        from team_abbreviations import TEAM_ABBREVIATIONS
        from enrichment import get_player_team_mapping
        
        # Load current games/odds data to get real matchups
        games_data = cache_get("mlb_odds")
        real_matchups = []
        team_to_matchup = {}
        
        if games_data:
            # Handle bytes, string, or dict data types
            if isinstance(games_data, bytes):
                games = json.loads(games_data.decode('utf-8'))
            elif isinstance(games_data, str):
                games = json.loads(games_data)
            else:
                games = games_data
            
            # Build matchup mapping from real game data
            if isinstance(games, list):
                for game in games:
                    if isinstance(game, dict):
                        home_team = game.get("home_team", "")
                        away_team = game.get("away_team", "")
                        
                        if home_team and away_team:
                            # Create matchup key using team abbreviations
                            matchup_key = format_matchup(away_team, home_team)
                            real_matchups.append({
                                "matchup": matchup_key,
                                "home_team": home_team,
                                "away_team": away_team,
                                "home_abbr": TEAM_ABBREVIATIONS.get(home_team, home_team[:3].upper()),
                                "away_abbr": TEAM_ABBREVIATIONS.get(away_team, away_team[:3].upper())
                            })
                            
                            # Map both teams to this matchup
                            team_to_matchup[home_team] = matchup_key
                            team_to_matchup[away_team] = matchup_key
        
        # Get player-to-team mapping with caching
        try:
            player_team_map = get_player_team_mapping()
            print(f"[INFO] Loaded player-team mapping with {len(player_team_map)} players")
        except Exception as e:
            print(f"[ERROR] Could not load player-team mapping: {e}")
            player_team_map = {}
        
        # Create reverse mapping: team abbreviation -> full team name
        team_abbr_to_full = {}
        for full_name, abbr in TEAM_ABBREVIATIONS.items():
            team_abbr_to_full[abbr] = full_name
        
        # Build matchup team sets for fast lookup
        matchup_teams = {}
        for matchup_info in real_matchups:
            matchup_key = matchup_info['matchup']
            home_team = matchup_info['home_team']  
            away_team = matchup_info['away_team']
            matchup_teams[matchup_key] = {home_team, away_team}
        
        # Group props by STRICT player-team validation
        grouped = {}
        matched_count = 0
        skipped_count = 0
        
        print(f"[DEBUG] Starting strict matchup filtering for {len(props_data)} props")
        print(f"[DEBUG] Available matchups: {list(matchup_teams.keys())}")

        # Helper: resolve a player name to their team (exact then fuzzy)
        def resolve_player_team(player_name):
            if player_name in player_team_map:
                return player_team_map[player_name], False
            for mapped_name, team in player_team_map.items():
                if len(player_name.split()) >= 2 and len(mapped_name.split()) >= 2:
                    prop_last = player_name.split()[-1].lower()
                    prop_first_initial = player_name.split()[0][0].lower()
                    mapped_last = mapped_name.split()[-1].lower()
                    mapped_first_initial = mapped_name.split()[0][0].lower()
                    if (prop_last == mapped_last and
                            prop_first_initial == mapped_first_initial and
                            len(prop_last) > 3):
                        print(f"[FUZZY] {player_name} -> {mapped_name} ({team})")
                        return team, True
            return None, False

        if not matchup_teams:
            # Cache is cold — try the events endpoint (free tier, no odds quota needed)
            # to build proper "AWAY @ HOME" matchup keys before falling back.
            try:
                from odds_api import fetch_mlb_events
                events = fetch_mlb_events()
                for ev in events:
                    home_team = ev.get("home_team", "")
                    away_team = ev.get("away_team", "")
                    if not home_team or not away_team:
                        continue
                    matchup_key = format_matchup(away_team, home_team)
                    if matchup_key not in matchup_teams:
                        matchup_teams[matchup_key] = {home_team, away_team}
                    team_to_matchup[home_team] = matchup_key
                    team_to_matchup[away_team] = matchup_key
                print(f"[DEBUG] Built {len(matchup_teams)} matchups from events endpoint")
            except Exception as _ev_err:
                print(f"[DEBUG] Events endpoint unavailable: {_ev_err}")

        if not matchup_teams:
            # No live game data and events endpoint failed too.
            # Fall back: group every prop whose player we can identify by team name.
            # Build synthetic per-team groupings so the UI always has something to show.
            print(f"[DEBUG] No live matchups available — falling back to team-based grouping")
            team_groups = {}
            for prop in props_data:
                if not isinstance(prop, dict):
                    continue
                player_name = prop.get('player', '')
                if not player_name:
                    continue
                player_team, _ = resolve_player_team(player_name)
                if not player_team:
                    skipped_count += 1
                    continue
                abbr = TEAM_ABBREVIATIONS.get(player_team, player_team[:3].upper())
                key = abbr
                if key not in team_groups:
                    team_groups[key] = []
                team_groups[key].append(prop)
                matched_count += 1
            grouped = team_groups
        else:
            for prop in props_data:
                if not isinstance(prop, dict):
                    continue
                    
                player_name = prop.get('player', '')
                if not player_name:
                    continue
                
                player_team, _ = resolve_player_team(player_name)
                
                if not player_team:
                    skipped_count += 1
                    continue
                
                # Find which matchup this player's team belongs to
                matched_matchup = None
                for matchup_key, teams_in_matchup in matchup_teams.items():
                    if player_team in teams_in_matchup:
                        matched_matchup = matchup_key
                        break
                
                # Only include prop if player's team is in a real matchup
                if matched_matchup:
                    if matched_matchup not in grouped:
                        grouped[matched_matchup] = []
                    grouped[matched_matchup].append(prop)
                    matched_count += 1
                else:
                    skipped_count += 1
        
        # Get game environment classifications with favored team info
        try:
            from odds_api import get_mlb_game_environment_map
            game_environments = get_mlb_game_environment_map()
            print(f"[DEBUG] Loaded {len(game_environments)} game environment classifications")
        except Exception as e:
            print(f"[WARNING] Could not load game environments: {e}")
            game_environments = {}
        
        # Add game environment labels and team status to props
        enhanced_grouped = {}
        for matchup_key, props in grouped.items():
            env_data = game_environments.get(matchup_key, {})
            environment_label = env_data.get('environment', 'Neutral')
            favored_team_abbr = env_data.get('favored_team', '')
            home_team_abbr = env_data.get('home_team', '')
            away_team_abbr = env_data.get('away_team', '')
            
            # Determine underdog team
            underdog_team_abbr = ''
            if favored_team_abbr:
                if favored_team_abbr == home_team_abbr:
                    underdog_team_abbr = away_team_abbr
                elif favored_team_abbr == away_team_abbr:
                    underdog_team_abbr = home_team_abbr
            
            # Create enhanced matchup key with environment label
            if environment_label != 'Neutral':
                enhanced_key = f"{matchup_key} — {environment_label}"
            else:
                enhanced_key = matchup_key
            
            # Enrich each prop with team status information
            enhanced_props = []
            for prop in props:
                # Get player's team from mapping
                player_name = prop.get('player', '')
                player_team_full = player_team_map.get(player_name, '')
                player_team_abbr = TEAM_ABBREVIATIONS.get(player_team_full, player_team_full[:3].upper() if player_team_full else '')
                
                # Determine if player's team is favored
                is_favored = False
                team_status = "unknown"
                
                if favored_team_abbr and player_team_abbr:
                    if player_team_abbr == favored_team_abbr:
                        is_favored = True
                        team_status = "favored"
                    elif player_team_abbr == underdog_team_abbr:
                        is_favored = False
                        team_status = "underdog"
                
                # Enrich prop with team status
                enhanced_prop = prop.copy()
                enhanced_prop.update({
                    "team_abbr": player_team_abbr,
                    "is_favored": is_favored,
                    "team_status": team_status,
                    "favored_team_abbr": favored_team_abbr,
                    "underdog_team_abbr": underdog_team_abbr
                })
                
                enhanced_props.append(enhanced_prop)
                
            enhanced_grouped[enhanced_key] = enhanced_props
            print(f"[DEBUG] {enhanced_key}: {len(enhanced_props)} props")
        
        print(f"[DEBUG] Strict filtering results: {matched_count} props matched, {skipped_count} skipped")
        print(f"[DEBUG] Final enhanced matchups: {list(enhanced_grouped.keys())}")
        print(f"[DEBUG] Grouped {len(props_data)} props into {len(enhanced_grouped)} matchups")
        
        return enhanced_grouped
        
    except Exception as e:
        logger.error(f"Error grouping props by matchup: {e}")
        # Fallback: distribute props evenly across common matchups
        try:
            common_matchups = ["BOS @ PHI", "BAL @ CLE", "NYY @ TB", "HOU @ SEA", "LAD @ SF"]
            grouped = {}
            props_per_matchup = max(1, len(props_data) // len(common_matchups))
            
            for i, prop in enumerate(props_data):
                matchup_index = i // props_per_matchup
                if matchup_index >= len(common_matchups):
                    matchup_index = len(common_matchups) - 1
                    
                matchup = common_matchups[matchup_index]
                if matchup not in grouped:
                    grouped[matchup] = []
                grouped[matchup].append(prop)
            
            return grouped
        except:
            return {"All Games": props_data if isinstance(props_data, list) else []}

@app.route("/api/mlb/props")
def get_mlb_props():
    """API endpoint for MLB props with game environment classification"""
    try:
        from enrichment import load_props_from_file
        
        # Load props from file cache
        props_data = load_props_from_file("mlb_props_cache.json")
        
        if not props_data:
            return jsonify({
                "message": "Props are being processed - please check back in a moment",
                "status": "processing", 
                "total_props": 0,
                "matchups": {}
            }), 202
        
        # Group props by matchup with environment labels
        grouped_props = group_props_by_matchup(props_data)
        
        return jsonify({
            "status": "success",
            "total_props": len(props_data),
            "total_matchups": len(grouped_props),
            "matchups": grouped_props
        })
            
    except Exception as e:
        logger.error(f"MLB props API error: {e}")
        return jsonify({
            "message": "Props temporarily unavailable",
            "status": "error",
            "total_props": 0,
            "matchups": {}
        }), 503

@app.route("/player_props")
def get_props():
    """Get enriched props grouped by matchup with optional filtering (Underdog Fantasy style)"""
    try:
        from enrichment import load_props_from_file
        
        # Load props from file cache (no Redis dependency)
        props_data = load_props_from_file("mlb_props_cache.json")
        
        if not props_data:
            print("⚠️ No cached props available in file")
            return jsonify({
                "message": "Props are being processed - please check back in a moment",
                "status": "processing", 
                "matchups": {}
            }), 202
        
        # Apply MLB game context enrichment to enhance props with positive environment analysis
        enhanced_context = request.args.get("enhanced_context", "false").lower() == "true"
        if enhanced_context:
            try:
                logger.info("Applying MLB game context enrichment to props")
                props_data = enrich_mlb_props_with_context(props_data)
                logger.info(f"MLB enrichment complete: {len(props_data)} props with positive environment")
            except Exception as e:
                logger.warning(f"MLB enrichment failed, using standard props: {e}")
        
        # Check for matchup filtering
        matchup = request.args.get("matchup")
        if matchup:
            try:
                # Group all props first, then filter by requested matchup
                grouped_props = group_props_by_matchup(props_data)
                
                # Check if the requested matchup exists in our grouped data
                if matchup in grouped_props:
                    matchup_props = grouped_props[matchup]
                    print(f"🎯 Found {len(matchup_props)} props for matchup {matchup}")
                    
                    # Return only the requested matchup
                    filtered_result = {matchup: matchup_props}
                    return jsonify(filtered_result)
                else:
                    # List available matchups for debugging
                    available_matchups = list(grouped_props.keys())
                    print(f"🎯 Matchup '{matchup}' not found. Available: {available_matchups}")
                    return jsonify({"error": f"Matchup '{matchup}' not found. Available matchups: {available_matchups}"}), 404
                
            except Exception as e:
                print(f"🔥 Error filtering props by matchup: {e}")
                return jsonify({"error": "Failed to filter props by matchup"}), 500
        
        # Group props by matchup (no filtering)
        grouped_props = group_props_by_matchup(props_data)
        
        print(f"✅ Serving {len(props_data)} props grouped into {len(grouped_props)} matchups")
        return jsonify(grouped_props)
            
    except Exception as e:
        print(f"🔥 Props endpoint error: {str(e)}")
        return jsonify({
            "message": "Props temporarily unavailable",
            "status": "error",
            "matchups": {}
        }), 503



@app.route("/analytics")
def analytics():
    """Analytics endpoint with hit counting"""
    try:
        hits = cache_incr("hits")
        return jsonify({"hits": hits, "status": "ok"})
    except Exception as e:
        logger.error(f"Error in analytics route: {e}")
        return jsonify({"hits": 0, "status": "error", "error": str(e)})

@app.route("/api/subscribe", methods=['POST'])
def api_subscribe():
    """Save email subscriber to JSON file"""
    try:
        data = request.get_json(force=True) or {}
        email = (data.get('email') or '').strip().lower()
        if not email or '@' not in email:
            return jsonify({'error': 'Invalid email'}), 400
        save_subscriber(email)
        return jsonify({'status': 'ok', 'message': 'Subscribed'}), 200
    except Exception as e:
        logger.error(f'[SUBSCRIBE] Error: {e}')
        return jsonify({'error': 'Server error'}), 500


@app.route("/api/status")
def api_status():
    """API status endpoint - lightweight with minimal operations"""
    try:
        # Check Redis health without blocking
        redis_status = "disconnected"
        if redis_healthy:
            redis_status = "connected"
        elif redis is not None:
            redis_status = "unstable"
        
        # Check initialization status
        initialization_status = "complete" if app_initialized else "in_progress"
        
        return jsonify({
            "message": "Welcome to Mora Bets API!",
            "status": "ok",
            "initialization": initialization_status,
            "redis_connected": redis_healthy,
            "redis_status": redis_status,
            "cache_type": "redis" if redis_healthy else "memory",
            "cache_fallback": "memory" if not redis_healthy else "redis",
            "odds_api_key_set": bool(os.environ.get("ODDS_API_KEY")),
            "custom_analysis_ready": False,  # Placeholder for future custom features
            "system_health": "stable" if redis_healthy and app_initialized else "degraded"
        })
    except Exception as e:
        logger.error(f"Error in status endpoint: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/ping")
def ping():
    """Ping endpoint with Redis status for deployment health checks"""
    redis_status = "OK" if redis and redis_healthy else "FAIL"
    return jsonify({"status": "running", "redis": redis_status})

@app.route("/api/odds")
def get_odds():
    """Get cached MLB odds"""
    try:
        cached = cache_get("mlb_odds")
        if cached:
            # Handle bytes, string, or dict data types
            if isinstance(cached, bytes):
                data = json.loads(cached.decode('utf-8'))
            elif isinstance(cached, str):
                data = json.loads(cached)
            else:
                data = cached
            return jsonify(data)
        return jsonify({"error": "Odds not cached yet. Please wait for background job to complete."}), 503
    except Exception as e:
        logger.error(f"Error in odds endpoint: {e}")
        return jsonify({"error": "Failed to retrieve odds"}), 500

@app.route("/api/mlb/odds")
def mlb_odds():
    """Curated MLB picks — h2h, spreads, totals — with no-vig probability."""
    try:
        from ev_engine import evaluate_pick

        odds_api_key = os.environ.get("ODDS_API_KEY")
        if not odds_api_key:
            return jsonify({"picks": [], "count": 0, "sport": "MLB", "error": "API key not configured"}), 503

        now = __import__('datetime').datetime.utcnow()
        future = now + __import__('datetime').timedelta(hours=48)
        start_time = now.replace(microsecond=0).isoformat() + "Z"
        end_time   = future.replace(microsecond=0).isoformat() + "Z"

        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
            params={
                "apiKey":           odds_api_key,
                "regions":          "us",
                "markets":          "h2h,spreads,totals",
                "oddsFormat":       "american",
                "commenceTimeFrom": start_time,
                "commenceTimeTo":   end_time,
                "bookmakers": (
                    "draftkings,fanduel,betmgm,"
                    "caesars,pointsbetus,betrivers,"
                    "bovada,betonlineag,fanatics"
                ),
            },
            timeout=20,
        )
        if resp.status_code == 401:
            return jsonify({"picks": [], "count": 0, "sport": "MLB", "error": "API quota exhausted"}), 503
        resp.raise_for_status()
        raw_games = resp.json()

        picks = []
        for game in raw_games:
            home      = game.get("home_team", "")
            away      = game.get("away_team", "")
            game_time = game.get("commence_time", "")
            matchup   = f"{away} @ {home}"

            # Collect all book prices per market before evaluating
            h2h_side1 = []   # home team as "over"
            h2h_side2 = []   # away team as "over"
            spread_sides = {}  # {(team, point): [book_prices]}
            total_over_prices  = []
            total_under_prices = []
            total_point = None

            for bk in game.get("bookmakers", []):
                bk_name = bk.get("title", "").lower()
                for market in bk.get("markets", []):
                    mk       = market.get("key", "")
                    outcomes = market.get("outcomes", [])

                    if mk == "h2h" and len(outcomes) >= 2:
                        for o in outcomes:
                            other = next((x for x in outcomes if x != o), None)
                            if not other:
                                continue
                            p = o.get("price")
                            q = other.get("price")
                            if p is None or q is None:
                                continue
                            if o.get("name") == home:
                                h2h_side1.append({"book": bk_name, "over_price": p, "under_price": q})
                            elif o.get("name") == away:
                                h2h_side2.append({"book": bk_name, "over_price": p, "under_price": q})

                    elif mk == "spreads" and len(outcomes) >= 2:
                        for o in outcomes:
                            other = next((x for x in outcomes if x != o), None)
                            if not other:
                                continue
                            team  = o.get("name", "")
                            price = o.get("price")
                            point = o.get("point")
                            opp_p = other.get("price")
                            if price is None or opp_p is None or point is None:
                                continue
                            k = (team, point)
                            spread_sides.setdefault(k, []).append({
                                "book": bk_name, "over_price": price, "under_price": opp_p
                            })

                    elif mk == "totals" and len(outcomes) >= 2:
                        over_o  = next((o for o in outcomes if o.get("name") == "Over"),  None)
                        under_o = next((o for o in outcomes if o.get("name") == "Under"), None)
                        if over_o and under_o:
                            op = over_o.get("price")
                            up = under_o.get("price")
                            pt = over_o.get("point")
                            if op and up and pt:
                                total_point = pt
                                total_over_prices.append( {"book": bk_name, "over_price": op,  "under_price": up})
                                total_under_prices.append({"book": bk_name, "over_price": up,  "under_price": op})

            # ── Evaluate h2h — collect ALL picks regardless of EV threshold ──
            for team, prices in [(home, h2h_side1), (away, h2h_side2)]:
                if not prices:
                    continue
                ev = evaluate_pick(
                    player_or_team=team, market_type="h2h", side="over",
                    line=None, book_prices=prices, game_time=game_time
                )
                picks.append({
                    "player":          team,
                    "stat":            "h2h",
                    "stat_label":      "Moneyline Win",
                    "line":            None,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "MLB",
                    "market_type":     "moneyline",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

            # ── Evaluate spreads ──
            for (team, point), prices in spread_sides.items():
                ev = evaluate_pick(
                    player_or_team=team, market_type="spreads", side="over",
                    line=point, book_prices=prices, game_time=game_time
                )
                label = f"Run Line {'+' if point > 0 else ''}{point}"
                picks.append({
                    "player":          team,
                    "stat":            "spreads",
                    "stat_label":      label,
                    "line":            point,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "MLB",
                    "market_type":     "spread",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

            # ── Evaluate totals ──
            for side_label, prices, side in [
                (f"Total Runs Over {total_point}",  total_over_prices,  "over"),
                (f"Total Runs Under {total_point}", total_under_prices, "under"),
            ]:
                if not prices or total_point is None:
                    continue
                ev = evaluate_pick(
                    player_or_team=matchup, market_type="totals", side=side,
                    line=total_point, book_prices=prices, game_time=game_time
                )
                picks.append({
                    "player":          matchup,
                    "stat":            "totals",
                    "stat_label":      side_label,
                    "line":            total_point,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "MLB",
                    "market_type":     "total",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

        # ── Split into two independent lists ──
        no_vig_picks = [p for p in picks if (p.get("no_vig_prob") or 0) >= 50]
        no_vig_picks.sort(key=lambda p: -(p.get("no_vig_prob") or 0))

        edge_picks = [
            p for p in picks
            if p.get("passes_threshold") is True
            and p.get("ev_pct") is not None
            and (p.get("ev_pct") or 0) > 0
        ]
        edge_picks.sort(key=lambda p: -(p.get("ev_pct") or 0))

        # CLV logging — only for edge picks (confirmed +EV), fire-and-forget
        try:
            from clv_tracker import log_bet_entry
            for pick in edge_picks:
                try:
                    log_bet_entry(
                        event_id=f"{pick.get('game_time','')}_{pick.get('player','')}",
                        player_or_team=pick["player"],
                        market_type=pick["stat"],
                        side="over",
                        line=pick.get("line"),
                        book=pick.get("best_book", ""),
                        offered_odds=pick.get("best_over_price", 0),
                        fair_probability=(pick.get("no_vig_prob", 50) / 100),
                        fair_odds=pick.get("fair_odds", 0),
                        ev_pct=pick.get("ev_pct", 0),
                        edge_pct=pick.get("edge_pct", 0),
                        game_time=pick.get("game_time", "")
                    )
                except Exception as clv_e:
                    logger.warning(f"[CLV] Log failed for {pick.get('player')}: {clv_e}")
        except Exception as clv_import_e:
            logger.warning(f"[CLV] Import failed: {clv_import_e}")

        avg_ev = round(sum(p.get("ev_pct") or 0 for p in edge_picks) / len(edge_picks), 1) if edge_picks else 0

        return jsonify({
            "no_vig_picks": no_vig_picks[:30],
            "edge_picks":   edge_picks,
            "picks":        no_vig_picks[:30],   # backward compat
            "counts": {
                "no_vig":  len(no_vig_picks),
                "edge":    len(edge_picks),
                "implied": len([p for p in no_vig_picks if p.get("implied_only")]),
            },
            "count":   len(no_vig_picks),
            "sport":   "MLB",
            "avg_ev":  avg_ev,
            "tiers": {
                "LOCK": len([p for p in edge_picks if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in edge_picks if p.get("confidence_tier") == "FIRE"]),
                "EDGE": len([p for p in edge_picks if p.get("confidence_tier") == "EDGE"]),
            },
        })

    except Exception as e:
        logger.error(f"[MLB] /api/mlb/odds error: {e}")
        return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "MLB", "error": str(e)}), 500


@app.route("/api/mlb/environment")
def api_mlb_environment():
    """Get MLB game environment classifications and favored teams"""
    try:
        from odds_api import get_mlb_game_environment_map
        env_map = get_mlb_game_environment_map()
        return jsonify({"environments": env_map})
    except Exception as e:
        logger.error(f"Failed to get MLB environment data: {e}")
        return jsonify({"error": "MLB environment data unavailable"}), 503

@app.route("/api/nfl/environment")
def api_nfl_environment():
    """Get NFL game environment classifications and favored teams"""
    try:
        from nfl_odds_api import get_nfl_game_environment_map
        env_map = get_nfl_game_environment_map()
        return jsonify({"environments": env_map})
    except Exception as e:
        logger.error(f"Failed to get NFL environment data: {e}")
        return jsonify({"error": "NFL environment data unavailable"}), 503

@app.route("/api/quota")
def api_quota():
    """Return current Odds API quota stats"""
    try:
        quota = get_quota()
        return jsonify(quota)
    except Exception as e:
        logger.error(f"Failed to get quota: {e}")
        return jsonify({"error": "Quota unavailable"}), 503

@app.route("/api/nhl/props")
def api_nhl_props():
    """Return NHL player props — checks memory cache, then file cache, then live fetch."""
    try:
        # 1. Memory cache
        cached = cache_get("nhl_enriched_props")
        if cached:
            props = json.loads(cached) if isinstance(cached, str) else cached
            return jsonify({
                "props": props,
                "count": len(props),
                "sport": "NHL",
                "cached": True,
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW": len([p for p in props if p.get("confidence_tier") == "LOW"])
                }
            })

        # 2. File cache
        from enrichment import load_props_from_file
        props = load_props_from_file("nhl_props_cache.json")
        if props:
            return jsonify({
                "props": props,
                "count": len(props),
                "sport": "NHL",
                "cached": True,
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW": len([p for p in props if p.get("confidence_tier") == "LOW"])
                }
            })

        # 3. Live fetch (costs API quota)
        props = update_nhl_props()
        return jsonify({
            "props": props,
            "count": len(props),
            "sport": "NHL",
            "cached": False
        })

    except Exception as e:
        logger.error(f"[NHL] /api/nhl/props error: {e}")
        return jsonify({"props": [], "count": 0, "sport": "NHL", "error": "Failed to load NHL props"}), 500

@app.route("/api/nhl/odds")
def api_nhl_odds():
    """Curated NHL picks — h2h, spreads, totals — evaluated with EV engine."""
    try:
        from nhl_odds_api import fetch_nhl_game_odds
        from ev_engine import evaluate_pick

        raw_games = fetch_nhl_game_odds()

        picks = []
        for game in raw_games:
            home      = game.get("home_team", "")
            away      = game.get("away_team", "")
            game_time = game.get("commence_time", "")
            matchup   = f"{away} @ {home}"

            h2h_home = []
            h2h_away = []
            spread_sides = {}
            total_over_prices  = []
            total_under_prices = []
            total_point = None

            for bk in game.get("bookmakers", []):
                bk_name = bk.get("title", "").lower()
                for market in bk.get("markets", []):
                    mk       = market.get("key", "")
                    outcomes = market.get("outcomes", [])

                    if mk == "h2h" and len(outcomes) >= 2:
                        for o in outcomes:
                            other = next((x for x in outcomes if x != o), None)
                            if not other:
                                continue
                            p = o.get("price")
                            q = other.get("price")
                            if p is None or q is None:
                                continue
                            if o.get("name") == home:
                                h2h_home.append({"book": bk_name, "over_price": p, "under_price": q})
                            elif o.get("name") == away:
                                h2h_away.append({"book": bk_name, "over_price": p, "under_price": q})

                    elif mk == "spreads" and len(outcomes) >= 2:
                        for o in outcomes:
                            other = next((x for x in outcomes if x != o), None)
                            if not other:
                                continue
                            team  = o.get("name", "")
                            price = o.get("price")
                            point = o.get("point")
                            opp_p = other.get("price")
                            if price is None or opp_p is None or point is None:
                                continue
                            k = (team, point)
                            spread_sides.setdefault(k, []).append({
                                "book": bk_name, "over_price": price, "under_price": opp_p
                            })

                    elif mk == "totals" and len(outcomes) >= 2:
                        over_o  = next((o for o in outcomes if o.get("name") == "Over"),  None)
                        under_o = next((o for o in outcomes if o.get("name") == "Under"), None)
                        if over_o and under_o:
                            op = over_o.get("price")
                            up = under_o.get("price")
                            pt = over_o.get("point")
                            if op and up and pt:
                                total_point = pt
                                total_over_prices.append( {"book": bk_name, "over_price": op, "under_price": up})
                                total_under_prices.append({"book": bk_name, "over_price": up, "under_price": op})

            # ── Evaluate h2h — collect ALL picks regardless of EV threshold ──
            for team, prices in [(home, h2h_home), (away, h2h_away)]:
                if not prices:
                    continue
                ev = evaluate_pick(
                    player_or_team=team, market_type="h2h", side="over",
                    line=None, book_prices=prices, game_time=game_time
                )
                picks.append({
                    "player":          team,
                    "stat":            "h2h",
                    "stat_label":      "Moneyline Win",
                    "line":            None,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "NHL",
                    "market_type":     "moneyline",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

            for (team, point), prices in spread_sides.items():
                ev = evaluate_pick(
                    player_or_team=team, market_type="spreads", side="over",
                    line=point, book_prices=prices, game_time=game_time
                )
                label = f"Puck Line {'+' if point > 0 else ''}{point}"
                picks.append({
                    "player":          team,
                    "stat":            "spreads",
                    "stat_label":      label,
                    "line":            point,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "NHL",
                    "market_type":     "spread",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

            for side_label, prices, side in [
                (f"Total Goals Over {total_point}",  total_over_prices,  "over"),
                (f"Total Goals Under {total_point}", total_under_prices, "under"),
            ]:
                if not prices or total_point is None:
                    continue
                ev = evaluate_pick(
                    player_or_team=matchup, market_type="totals", side=side,
                    line=total_point, book_prices=prices, game_time=game_time
                )
                picks.append({
                    "player":          matchup,
                    "stat":            "totals",
                    "stat_label":      side_label,
                    "line":            total_point,
                    "no_vig_prob":     round((ev["fair_probability"] or 0.5) * 100, 1),
                    "fair_odds":       ev["fair_odds"],
                    "ev_pct":          ev["ev_pct"],
                    "edge_pct":        ev["edge_pct"],
                    "confidence_tier": ev["confidence_tier"],
                    "best_over_price": ev["best_offered_odds"],
                    "best_book":       ev["best_book"],
                    "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                    "book_count":      ev["book_count"],
                    "matchup":         matchup,
                    "game_time":       game_time,
                    "sport":           "NHL",
                    "market_type":     "total",
                    "all_books":       prices,
                    "passes_threshold": ev["passes_threshold"],
                    "surfaced":        ev["passes_threshold"],
                })

        # ── Split into two independent lists ──
        no_vig_picks = [p for p in picks if (p.get("no_vig_prob") or 0) >= 50]
        no_vig_picks.sort(key=lambda p: -(p.get("no_vig_prob") or 0))

        edge_picks = [
            p for p in picks
            if p.get("passes_threshold") is True
            and p.get("ev_pct") is not None
            and (p.get("ev_pct") or 0) > 0
        ]
        edge_picks.sort(key=lambda p: -(p.get("ev_pct") or 0))

        avg_ev = round(sum(p.get("ev_pct") or 0 for p in edge_picks) / len(edge_picks), 1) if edge_picks else 0

        return jsonify({
            "no_vig_picks": no_vig_picks[:25],
            "edge_picks":   edge_picks,
            "picks":        no_vig_picks[:25],   # backward compat
            "counts": {
                "no_vig":  len(no_vig_picks),
                "edge":    len(edge_picks),
                "implied": len([p for p in no_vig_picks if p.get("implied_only")]),
            },
            "count":  len(no_vig_picks),
            "sport":  "NHL",
            "avg_ev": avg_ev,
            "tiers": {
                "LOCK": len([p for p in edge_picks if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in edge_picks if p.get("confidence_tier") == "FIRE"]),
                "EDGE": len([p for p in edge_picks if p.get("confidence_tier") == "EDGE"]),
            },
        })

    except Exception as e:
        logger.error(f"[NHL] /api/nhl/odds error: {e}")
        return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "NHL", "error": str(e)}), 500


@app.route("/api/performance")
def api_performance():
    """Return CLV / performance report for all logged picks."""
    try:
        from clv_tracker import get_performance_report
        report = get_performance_report()
        return jsonify(report)
    except Exception as e:
        logger.error(f"[Performance] /api/performance error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/performance/log")
def api_performance_log():
    """Return the raw CLV log (all entries)."""
    try:
        import json as _json
        log_file = "clv_log.json"
        try:
            with open(log_file, "r") as f:
                log = _json.load(f)
        except FileNotFoundError:
            log = []
        return jsonify({"entries": log, "count": len(log)})
    except Exception as e:
        logger.error(f"[Performance] /api/performance/log error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/nhl/environment")
def api_nhl_environment():
    """Get NHL game environment classifications"""
    try:
        from nhl_odds_api import get_nhl_game_environment_map
        env_map = get_nhl_game_environment_map()
        return jsonify({"environments": env_map})
    except Exception as e:
        logger.error(f"[NHL] Failed to get environment data: {e}")
        return jsonify({"environments": {}})

@app.route("/api/debug/nhl-raw")
def debug_nhl_raw():
    """
    Verify NHL prop parsing is working.
    Fetches one event, returns raw API response +
    parsed props so you can confirm player names
    are populated correctly.
    Remove this route after confirming it works.
    """
    if not os.environ.get("ODDS_API_KEY"):
        return jsonify({"error": "ODDS_API_KEY not set"}), 500

    try:
        from nhl_odds_api import (fetch_nhl_events,
                                   fetch_props_for_event)

        events = fetch_nhl_events()

        if not events:
            return jsonify({
                "status": "no_events",
                "message": "No NHL games today"
            })

        first_event = events[0]

        # Fetch raw for ONE market to keep quota cost to 1
        raw = requests.get(
            f"https://api.the-odds-api.com/v4/sports/"
            f"icehockey_nhl/events/{first_event['id']}/odds",
            params={
                "apiKey": os.environ.get("ODDS_API_KEY"),
                "regions": "us",
                "markets": "player_shots_on_goal",
                "oddsFormat": "american",
                "bookmakers": "draftkings"
            },
            timeout=15
        )

        quota = raw.headers.get(
            "x-requests-remaining", "unknown"
        )

        if raw.status_code == 422:
            return jsonify({
                "status": "props_not_posted_yet",
                "event": first_event,
                "quota_remaining": quota,
                "message": "Try again after 9 AM ET"
            })

        # Also run through the actual parser
        parsed = fetch_props_for_event(first_event)

        return jsonify({
            "status": "success",
            "event": first_event,
            "total_events_today": len(events),
            "quota_remaining": quota,
            "parsed_props_count": len(parsed),
            "parsed_sample": parsed[:3],
            "raw_api_response": raw.json()
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/cache-status")
def cache_status():
    """Lightweight cache status — no auth required, costs no API calls."""
    from enrichment import load_props_from_file
    import os

    mlb_props = load_props_from_file("mlb_props_cache.json")
    nhl_props = load_props_from_file("nhl_props_cache.json")

    def file_age_minutes(filename):
        try:
            mtime = os.path.getmtime(filename)
            return round((time.time() - mtime) / 60, 1)
        except Exception:
            return None

    return jsonify({
        "mlb": {
            "props_count": len(mlb_props),
            "cache_age_minutes": file_age_minutes("mlb_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in mlb_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in mlb_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in mlb_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "nhl": {
            "props_count": len(nhl_props),
            "cache_age_minutes": file_age_minutes("nhl_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in nhl_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in nhl_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in nhl_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "next_refresh": "Daily at 10:00 AM ET",
        "strategy": "Sharp consensus window — overnight sharp action settled, before public money distorts lines"
    })

@app.route("/api/mlb/props/enhanced")
def get_enhanced_mlb_props():
    """Get MLB props with deep game context analysis"""
    try:
        from enrichment import load_props_from_file
        
        # Load props from file cache
        props_data = load_props_from_file("mlb_props_cache.json")
        
        if not props_data:
            return jsonify({"error": "No MLB props available"}), 503
        
        # Apply MLB game context enrichment
        enhanced_props = enrich_mlb_props_with_context(props_data)
        
        # Optionally filter to only positive environment props
        filter_positive = request.args.get("positive_only", "true").lower() == "true"
        if filter_positive:
            enhanced_props = filter_positive_environment_props(enhanced_props)
        
        # Group by matchup
        grouped_props = group_props_by_matchup(enhanced_props)
        
        logger.info(f"Enhanced MLB props: {len(enhanced_props)} props with game context")
        return jsonify({
            "total_props": len(enhanced_props),
            "matchups": grouped_props,
            "enrichment_applied": True
        })
        
    except Exception as e:
        logger.error(f"Error in enhanced MLB props endpoint: {e}")
        return jsonify({"error": "Failed to retrieve enhanced MLB props"}), 500
        
@app.route("/api/nfl/props")
def get_nfl_props():
    """
    NFL player props endpoint
    - Keeps off-season handling (422/INVALID_MARKET -> [])
    - Filters to VALID_BOOK_TITLES and odds window via _valid_price
    - Normalizes to MLB-like rows
    - Enriches with environment map
    """
    try:
        logger.info("[NFL] /api/nfl/props called")

        # --- required imports ---
        from nfl_odds_api import fetch_nfl_props
        from nfl_game_enrichment import (
            build_nfl_environment_map,
            enrich_nfl_props_with_context,
        )
        # If get_team_abbreviation is in another module, import it:
        # from teams import get_team_abbreviation

        # --- fetch with off-season guard ---
        try:
            events = fetch_nfl_props() or []
        except RuntimeError as e:
            msg = str(e)
            if "422" in msg or "INVALID_MARKET" in msg:
                logger.info("[NFL] Off-season: no player props")
                return jsonify([])
            raise

        if not events:
            logger.info("[NFL] odds API returned 0 events")
            return jsonify([])

        enhanced_props: list[dict] = []

        for event in events:
            home_team = (event.get("home_team") or "").strip()
            away_team = (event.get("away_team") or "").strip()

            for bookmaker in (event.get("bookmakers") or []):
                title = (bookmaker.get("title") or "").strip()
                # If VALID_BOOK_TITLES is defined as {"fanduel"} and is case-insensitive:
                try:
                    if title.lower() not in VALID_BOOK_TITLES:
                        continue
                except NameError:
                    # Fallback: allow all books if not configured
                    pass

                for market in (bookmaker.get("markets") or []):
                    market_key = (market.get("key") or "").strip()

                    # Optional: limit to a subset of markets
                    # if market_key not in DESIRED_MARKETS:
                    #     continue

                    # Pair outcomes by (player, line, market)
                    pairs: dict[tuple, dict] = {}
                    for oc in (market.get("outcomes") or []):
                        price = oc.get("price")
                        # Enforce odds window if helper exists; otherwise accept as-is
                        try:
                            if not _valid_price(price):
                                continue
                        except NameError:
                            pass  # no odds window configured

                        player_name = (oc.get("description") or "").strip()
                        point = oc.get("point", None)

                        side = (oc.get("name") or "").strip().lower()  # "over"/"under"
                        key = (player_name, point, market_key)

                        entry = pairs.setdefault(key, {"over_odds": None, "under_odds": None})

                        if "over" in side:
                            entry["over_odds"] = price
                        elif "under" in side:
                            entry["under_odds"] = price
                        else:
                            # Skip unlabeled sides; don’t guess
                            continue

                    for (player_name, point, mk), ou in pairs.items():
                        if ou.get("over_odds") is None and ou.get("under_odds") is None:
                            continue  # nothing valid within window

                        # Team abbreviations with safe fallback
                        try:
                            home_abbr = get_team_abbreviation(home_team)
                            away_abbr = get_team_abbreviation(away_team)
                        except NameError:
                            home_abbr = ""
                            away_abbr = ""

                        enhanced_props.append({
                            "player": player_name,
                            "player_name": player_name,
                            "stat": mk,
                            "stat_type": mk,
                            "market": mk,
                            "line": point,
                            "point": point,
                            "bookmaker": title,
                            "sportsbook": title,
                            "home_team": home_team,
                            "away_team": away_team,
                            "home_abbr": home_abbr,
                            "away_abbr": away_abbr,
                            "matchup": f"{away_team} @ {home_team}",
                            "over_odds": ou.get("over_odds"),
                            "under_odds": ou.get("under_odds"),
                            # defaults; enrichment overwrites
                            "confidence": "Medium",
                            "team": "",
                            "team_abbr": "",
                            "team_status": "",
                            "hit_probability": 0.5,
                        })

        logger.info("[NFL] normalized (post-filter) %d props", len(enhanced_props))

        # Build environment from the full events (book-agnostic)
        env_map = build_nfl_environment_map(events)
        logger.info("[NFL] built env for %d matchups", len(env_map))

        enriched = enrich_nfl_props_with_context(enhanced_props, env_map)
        logger.info("[NFL] enriched %d props", len(enriched))

        # Optional global cap for payload safety
        # MAX_PROPS_TOTAL = 400
        # enriched = enriched[:MAX_PROPS_TOTAL]

        return jsonify(enriched)

    except Exception as e:
        logger.error("[NFL] Error in props endpoint: %s", e, exc_info=True)
        return jsonify([]), 200  # keep shape stable


@app.route("/api/matchups")
def matchups():
    """Get all matchups with odds - optimized for speed"""
    try:
        data = cache_get("mlb_odds")
        if not data:
            return jsonify({"error": "No cached odds available"}), 503

        # Handle bytes, string, or dict data types
        if isinstance(data, bytes):
            games = json.loads(data.decode('utf-8'))
        elif isinstance(data, str):
            games = json.loads(data)
        else:
            games = data
        
        # Simple matchup format for quick display
        matchups = []
        
        # Ensure games is a list and contains valid game objects
        if not isinstance(games, list):
            return jsonify({"error": "Invalid game data format"}), 500
            
        for game in games:
            if not isinstance(game, dict):
                continue
                
            home = game.get("home_team")
            away = game.get("away_team")
            if home and away:
                matchups.append({
                    "matchup": format_matchup(away, home),
                    "start_time": game.get("commence_time", "Unknown"),
                    "home_team": home,
                    "away_team": away,
                    "home_abbr": get_team_abbreviation(home),
                    "away_abbr": get_team_abbreviation(away)
                })
        
        return jsonify(matchups)
    except Exception as e:
        logger.error(f"Error in matchups endpoint: {e}")
        return jsonify({"error": "Failed to process matchups"}), 500










@app.route("/debug/cache")
def debug_cache():
    """Debug cache contents"""
    try:
        # Check all cache keys
        cache_keys = []
        if redis_healthy and redis:
            try:
                cache_keys = [k.decode() if isinstance(k, bytes) else k for k in redis.keys("*")]
            except Exception as e:
                logger.error(f"Redis keys error: {e}")
        
        # Count cached props
        cached_props = cache_get("mlb_enriched_props")
        props_count = 0
        if cached_props:
            try:
                props_data = json.loads(cached_props) if isinstance(cached_props, str) else cached_props
                props_count = len(props_data) if isinstance(props_data, list) else 0
            except:
                props_count = 0
        
        return jsonify({
            "cache_keys": cache_keys,
            "memory_cache_keys": list(memory_cache.keys()),
            "redis_healthy": redis_healthy,
            "cached_props_count": props_count,
            "cache_type": "redis" if redis_healthy else "memory"
        })
    except Exception as e:
        logger.error(f"Error in debug cache endpoint: {e}")
        return jsonify({"error": "Failed to debug cache"}), 500

def update_odds():
    """Update MLB odds cache"""
    try:
        logger.info("🔄 Updating MLB odds...")
        games = parse_game_data()
        if games:
            cache_set("mlb_odds", json.dumps(games))
            logger.info(f"Updated MLB odds cache with {len(games)} games")
        else:
            logger.warning("No games data received from odds API")
    except Exception as e:
        logger.error(f"Failed to update odds: {e}")

def update_player_props():
    """Update player props with smart filtering and enrichment"""
    try:
        logger.info("🔄 Starting smart player props update...")
        
        # Step 1: Fetch all available props
        raw_props = fetch_player_props()
        logger.info(f"🔍 Total raw props pulled: {len(raw_props)}")
        
        if not raw_props:
            logger.warning("No raw props fetched")
            return []
        
        # Step 2: Smart filtering - only enrich relevant betting props
        logger.info("[DEBUG] Starting smart enrichment for {} props".format(len(raw_props)))
        logger.info("[DEBUG] Filtering {} props for enrichment".format(len(raw_props)))
        
        # Filter for only relevant betting props with smart thresholds
        relevant_props = []
        for prop in raw_props:
            stat_type = prop.get('stat')
            line = float(prop.get('line', 0))
            
            # Smart filtering with appropriate thresholds per stat type (API-verified markets only)
            keep_prop = False
            
            # Batter stats with reasonable thresholds (verified working with Odds API)
            if stat_type == 'batter_hits' and line <= 2.5:
                keep_prop = True
            elif stat_type == 'batter_total_bases' and line <= 1.5:
                keep_prop = True
            elif stat_type == 'batter_home_runs' and line <= 0.5:
                keep_prop = True
            
            # Pitcher stats with reasonable thresholds (verified working with Odds API)
            elif stat_type == 'pitcher_strikeouts' and line <= 7.5:
                keep_prop = True
            elif stat_type == 'pitcher_earned_runs' and line <= 4.5:
                keep_prop = True
            elif stat_type == 'pitcher_hits_allowed' and line <= 8.5:
                keep_prop = True
            elif stat_type == 'pitcher_outs' and line <= 21.5:
                keep_prop = True
            
            if keep_prop:
                relevant_props.append(prop)
        
        logger.info(f"[INFO] Filtered to {len(relevant_props)} relevant betting props (from {len(raw_props)} total)")
        
        # Step 3: Group by player, compute no-vig prob & tier, sort
        if relevant_props:
            grouped = group_props_by_player(relevant_props)
            enriched_props = sort_props_by_tier(grouped)

            # Step 4: Cache enriched props to file (Redis-free)
            from enrichment import cache_props_to_file
            cache_props_to_file(enriched_props, "mlb_props_cache.json")
            logger.info(f"✅ Cached {len(enriched_props)} enriched props to file")
            
            return enriched_props
        else:
            logger.warning("No relevant props to enrich")
            return []
            
    except Exception as e:
        logger.error(f"Failed to update player props: {e}")
        logger.error(f"Full traceback: {e}", exc_info=True)
        return []

# FETCH SCHEDULE RATIONALE:
# - Props post the night before but lines are soft/unsharp
# - Sharp bettors move lines overnight into early morning
# - By 10 AM ET, overnight sharp action has settled lines
# - Lineups for MLB aren't confirmed until 3-4 hrs before
#   first pitch (1-4 PM ET) so morning odds reflect sharp
#   consensus before public money distorts them
# - This is the "true probability" window — sharpest market
#   consensus before public noise
# - Aligns with our "5 Minute Morning Routine" brand promise
# - One API call per day saves 95% of quota vs hourly fetching

# Alias for scheduler — mirrors update_player_props for MLB
update_mlb_props = update_player_props

def update_nhl_props():
    """Fetch, group, score, and cache NHL props."""
    try:
        from nhl_odds_api import fetch_nhl_props
        from enrichment import cache_props_to_file

        logger.info("[NHL] Starting daily props update...")
        raw_props = fetch_nhl_props()

        if not raw_props:
            logger.warning("[NHL] No raw props returned")
            return []

        logger.info(f"[NHL] Processing {len(raw_props)} raw props")
        grouped = group_props_by_player(raw_props)
        sorted_props = sort_props_by_tier(grouped)

        cache_props_to_file(sorted_props, "nhl_props_cache.json")
        logger.info(f"[NHL] Cached {len(sorted_props)} processed props")

        cache_set("nhl_enriched_props", json.dumps(sorted_props))
        return sorted_props

    except Exception as e:
        logger.error(f"[NHL] update_nhl_props failed: {e}", exc_info=True)
        return []

def redis_health_monitor():
    """Monitor Redis health and attempt reconnection"""
    logger.info("🔄 Attempting scheduled Redis reconnection...")
    check_redis_health()

def system_health_check():
    """Comprehensive system health check"""
    try:
        # Check cache availability
        cache_status = "healthy" if redis_healthy else "degraded"
        
        # Check API key
        api_key_status = "configured" if os.environ.get("ODDS_API_KEY") else "missing"
        
        # Check cached data
        cached_odds = cache_get("mlb_odds")
        cached_props = cache_get("mlb_enriched_props")
        
        odds_count = 0
        props_count = 0
        
        if cached_odds:
            try:
                odds_data = json.loads(cached_odds) if isinstance(cached_odds, str) else cached_odds
                odds_count = len(odds_data) if isinstance(odds_data, list) else 0
            except:
                pass
        
        if cached_props:
            try:
                props_data = json.loads(cached_props) if isinstance(cached_props, str) else cached_props
                props_count = len(props_data) if isinstance(props_data, list) else 0
            except:
                pass
        
        logger.info(f"📊 System Health: Cache={cache_status}, API={api_key_status}, Odds={odds_count}, Props={props_count}")
        
    except Exception as e:
        logger.error(f"System health check failed: {e}")

# Background scheduler setup
scheduler = BackgroundScheduler()

# Schedule jobs

# MLB — 10 AM ET daily (sharp consensus window)
scheduler.add_job(
    func=update_mlb_props,
    trigger="cron",
    hour=10,
    minute=0,
    timezone="America/New_York",
    id="update_mlb_props_daily",
    name="Daily MLB Props — 10 AM ET Sharp Window",
    replace_existing=True
)

# NHL — 10 AM ET daily
# NHL games typically 7-10 PM ET so morning lines are clean
scheduler.add_job(
    func=update_nhl_props,
    trigger="cron",
    hour=10,
    minute=5,  # 5 min offset to stagger API calls
    timezone="America/New_York",
    id="update_nhl_props_daily",
    name="Daily NHL Props — 10 AM ET Sharp Window",
    replace_existing=True
)

# Game-level odds refresh twice daily (lighter weight calls)
# Morning sharp window + evening pre-game refresh
scheduler.add_job(
    func=update_odds,
    trigger="cron",
    hour="10,18",  # 10 AM and 6 PM ET
    minute=10,
    timezone="America/New_York",
    id="update_game_odds",
    name="Game Odds Refresh",
    replace_existing=True
)

# Health monitoring jobs
scheduler.add_job(
    func=redis_health_monitor,
    trigger="interval",
    seconds=30,
    id="redis_health_monitor",
    name="Redis Health Monitor",
    replace_existing=True
)

scheduler.add_job(
    func=system_health_check,
    trigger="interval",
    minutes=5,
    id="system_health_check",
    name="System Health Check",
    replace_existing=True
)



# Global flag to track initialization
app_initialized = False

def background_initializer():
    """Background initialization of expensive operations"""
    global app_initialized
    import time
    time.sleep(5)  # Wait for server to fully boot
    
    try:
        logger.info("🚀 Starting background initialization...")
        
        # Start scheduler
        if not scheduler.running:
            scheduler.start()
            logger.info("✅ Background scheduler started")
        
        # Initial cache priming (non-blocking)
        logger.info("🔄 Starting cache priming...")
        try:
            update_odds()
            logger.info("✅ Odds cache primed")
        except Exception as e:
            logger.warning(f"Odds cache priming failed: {e}")
        
        try:
            update_player_props()
            logger.info("✅ Props cache primed")
        except Exception as e:
            logger.warning(f"Props cache priming failed: {e}")

        try:
            update_nhl_props()
            logger.info("✅ NHL props cache primed")
        except Exception as e:
            logger.warning(f"NHL cache priming failed: {e}")

        app_initialized = True
        logger.info("🎉 Background initialization complete")
        
    except Exception as e:
        logger.error(f"Background initialization failed: {e}")
        app_initialized = True  # Mark as complete even if failed



@app.route("/api/nfl/props/debug")
def nfl_props_debug():
    from nfl_odds_api import _detect_nfl_sport_key, fetch_nfl_props
    try:
        sk = _detect_nfl_sport_key()
        data = fetch_nfl_props(hours_ahead=96)
        return jsonify({
            "sport_key": sk,
            "events_with_props": len(data),
            "sample_event": (data[0] if data else None)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Start background initialization in a separate thread
from threading import Thread
init_thread = Thread(target=background_initializer, daemon=True)
init_thread.start()

# Flask app startup
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
