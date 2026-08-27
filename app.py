import os
import json
import logging
import time
import random
import requests
import sys
import csv
import threading
import hashlib
from dotenv import load_dotenv

load_dotenv()

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

try:
    os.makedirs('/var/data', exist_ok=True)
except OSError:
    pass

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
CONTEXT_EDGE_SESSION_EMAIL_KEY = "context_edge_verified_email"
CONTEXT_EDGE_SESSION_VERIFIED_AT_KEY = "context_edge_verified_at"


def _normalize_context_edge_email(email):
    return (email or "").strip().lower()


def _context_edge_access_allowed(email):
    normalized_email = _normalize_context_edge_email(email)
    if not normalized_email or "@" not in normalized_email:
        return False

    try:
        from supabase_backend import user_has_context_edge_access

        return bool(user_has_context_edge_access(normalized_email))
    except Exception as e:
        logger.error(f"[CONTEXT EDGE] Access check failed for {normalized_email}: {e}")
        return False


def _set_context_edge_session(email):
    normalized_email = _normalize_context_edge_email(email)
    session[CONTEXT_EDGE_SESSION_EMAIL_KEY] = normalized_email
    session[CONTEXT_EDGE_SESSION_VERIFIED_AT_KEY] = datetime.utcnow().isoformat()
    session.permanent = True


def _clear_context_edge_session():
    session.pop(CONTEXT_EDGE_SESSION_EMAIL_KEY, None)
    session.pop(CONTEXT_EDGE_SESSION_VERIFIED_AT_KEY, None)


def _session_has_context_edge_access():
    email = _normalize_context_edge_email(session.get(CONTEXT_EDGE_SESSION_EMAIL_KEY))
    if not email:
        return False

    if _context_edge_access_allowed(email):
        return True

    _clear_context_edge_session()
    return False


def _session_verified_email():
    return _normalize_context_edge_email(session.get(CONTEXT_EDGE_SESSION_EMAIL_KEY))


def _session_has_affiliate_access():
    email = _session_verified_email()
    if not email:
        return False

    try:
        from supabase_backend import user_has_affiliate_access

        return bool(user_has_affiliate_access(email))
    except Exception as e:
        logger.error(f"[AFFILIATE] Access check failed for {email}: {e}")
        return False


def save_subscriber(email, name="", phone="", source="daily_dashboard_trial"):
    """Append or update a subscriber record in the local JSON backup."""
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE, 'r') as f:
                subs = json.load(f)
        else:
            subs = []

        normalized_email = (email or "").strip().lower()
        now = datetime.utcnow().isoformat()

        # Preserve compatibility with older backups that stored plain email strings.
        existing = None
        for record in subs:
            if isinstance(record, str) and record.strip().lower() == normalized_email:
                existing = record
                break
            if isinstance(record, dict) and record.get('email', '').strip().lower() == normalized_email:
                existing = record
                break

        subscriber_record = {
            'email': normalized_email,
            'name': (name or '').strip(),
            'phone': (phone or '').strip(),
            'source': (source or 'daily_dashboard_trial').strip(),
            'signed_up_at': now,
        }

        if isinstance(existing, dict):
            existing.update({k: v for k, v in subscriber_record.items() if v})
        elif existing is not None:
            subs[subs.index(existing)] = subscriber_record
        else:
            subs.append(subscriber_record)

        with open(SUBSCRIBERS_FILE, 'w') as f:
            json.dump(subs, f, indent=2)
        logger.info(f'[SUBSCRIBE] Added/updated {normalized_email} source={subscriber_record["source"]}')
    except Exception as e:
        logger.error(f'[SUBSCRIBE] Error saving subscriber {email}: {e}')


def _lead_event_id(email, source="daily_dashboard_trial"):
    """Stable daily Meta event id so repeat submits do not inflate lead counts."""
    normalized_email = (email or "").strip().lower()
    normalized_source = (source or "daily_dashboard_trial").strip().lower()
    digest = hashlib.sha256(f"{normalized_source}:{normalized_email}".encode("utf-8")).hexdigest()[:20]
    return f"lead-{datetime.utcnow().strftime('%Y%m%d')}-{digest}"


def _sync_to_brevo_contact(email, name="", phone="", source="daily_dashboard_trial"):
    """Create/update a Brevo contact if BREVO_API_KEY is configured."""
    brevo_key = os.environ.get('BREVO_API_KEY', '').strip()
    if not brevo_key:
        logger.warning('[LEAD] BREVO_API_KEY not set - skipping Brevo sync')
        return False

    normalized_email = (email or '').strip().lower()
    full_name = (name or '').strip()
    first_name = full_name.split(' ', 1)[0] if full_name else ''
    last_name = full_name.split(' ', 1)[1] if ' ' in full_name else ''
    attributes = {
        'SOURCE': (source or 'daily_dashboard_trial').strip(),
    }
    if first_name:
        attributes['FIRSTNAME'] = first_name
    if last_name:
        attributes['LASTNAME'] = last_name
    cleaned_phone = (phone or '').strip()
    if cleaned_phone:
        phone_digits = ''.join(ch for ch in cleaned_phone if ch.isdigit())
        normalized_sms = cleaned_phone
        if len(phone_digits) == 10:
            normalized_sms = f'+1{phone_digits}'
        elif len(phone_digits) == 11 and phone_digits.startswith('1'):
            normalized_sms = f'+{phone_digits}'
        elif cleaned_phone.startswith('+'):
            normalized_sms = '+' + phone_digits if phone_digits else cleaned_phone

        attributes['PHONE'] = normalized_sms
        attributes['SMS'] = normalized_sms

    payload = {
        'email': normalized_email,
        'attributes': attributes,
        'updateEnabled': True,
    }
    brevo_list_id = os.environ.get('BREVO_LIST_ID', '').strip()
    if brevo_list_id.isdigit():
        payload['listIds'] = [int(brevo_list_id)]

    try:
        resp = requests.post(
            'https://api.brevo.com/v3/contacts',
            headers={
                'accept': 'application/json',
                'api-key': brevo_key,
                'content-type': 'application/json',
            },
            json=payload,
            timeout=8,
        )
        if resp.status_code in (200, 201, 204):
            logger.info(f'[LEAD] Brevo contact synced for {normalized_email}')
            return True
        logger.warning(f'[LEAD] Brevo sync failed status={resp.status_code} body={resp.text[:200]}')
    except Exception as e:
        logger.error(f'[LEAD] Brevo sync error for {normalized_email}: {e}')
    return False


# Redis configuration with robust stability features
redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis = None
memory_cache = {}  # In-memory fallback cache
redis_healthy = False
redis_last_check = 0

# ---- NFL prop filtering (FanDuel/DraftKings + odds gate) ----
VALID_BOOK_TITLES = {"fanduel", "draftkings"}   # case-insensitive match on bookmaker.title
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
    """Public Fade the Books landing page."""
    return render_template("index.html")

@app.route('/mora-assists')
def mora_assists_page():
    """Mora Assists sales and information page."""
    return render_template('mora_assists_sales.html')


@app.route('/mora-assists-welcome')
def mora_assists_welcome():
    """Thank you page — shown after Stripe trial signup. Fires StartTrial pixel."""
    return render_template('mora_assists_welcome.html')


@app.route('/success')
def dashboard_trial_success():
    """Dashboard trial success page. Verifies Stripe session before tracking StartTrial."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return render_template(
            "dashboard_success.html",
            verified=False,
            session_id="",
            email="",
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="We could not verify your checkout yet. Please contact support if this continues.",
        ), 400

    stripe_secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_secret_key:
        logger.error("[STRIPE] STRIPE_SECRET_KEY is not configured for dashboard success page")
        return render_template(
            "dashboard_success.html",
            verified=False,
            session_id="",
            email="",
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="We could not verify your checkout yet. Please contact support if this continues.",
        ), 500

    try:
        stripe_lib.api_key = stripe_secret_key
        checkout_session = stripe_lib.checkout.Session.retrieve(session_id)
        status = _stripe_object_get(checkout_session, "status", "")
        verified = status == "complete"
        email = _stripe_checkout_email(checkout_session)

        if verified:
            try:
                _sync_dashboard_subscription_from_checkout_session(checkout_session)
            except Exception as sync_err:
                logger.warning(f"[STRIPE] Dashboard success subscription sync failed: {sync_err}")
            if email:
                _set_context_edge_session(email)

        return render_template(
            "dashboard_success.html",
            verified=verified,
            session_id=session_id if verified else "",
            email=email,
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="" if verified else "We could not verify your checkout yet. Please contact support if this continues.",
        )
    except Exception as e:
        logger.error(f"[STRIPE] Dashboard success verification error: {e}")
        return render_template(
            "dashboard_success.html",
            verified=False,
            session_id="",
            email="",
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="We could not verify your checkout yet. Please contact support if this continues.",
        ), 400


@app.route('/trial-success')
def trial_downsell_success():
    """Newsletter down-sell success page with the 3-day trial handoff."""
    return render_template(
        "trial_success.html",
        trial_link="https://buy.stripe.com/aFa7sK6eh7MF4YZ1Oa0gw00",
        meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
    )


@app.route('/context-edge/success')
def context_edge_success():
    """Context Edge checkout success page. Verifies Stripe session before tracking Purchase."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return redirect(url_for("home", checkout="missing_session"))

    stripe_secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_secret_key:
        logger.error("[CONTEXT EDGE STRIPE] STRIPE_SECRET_KEY is not configured for success page")
        return render_template(
            "context_edge_success.html",
            verified=False,
            session_id="",
            email="",
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="We could not verify your checkout yet. Please contact support if this continues.",
        ), 500

    try:
        stripe_lib.api_key = stripe_secret_key
        checkout_session = stripe_lib.checkout.Session.retrieve(session_id)
        product = _stripe_object_get(_stripe_object_get(checkout_session, "metadata", {}), "product", "")
        status = _stripe_object_get(checkout_session, "status", "")
        verified = product == "context_edge" and status == "complete"
        email = (
            _stripe_object_get(checkout_session, "customer_email", "")
            or _stripe_object_get(_stripe_object_get(checkout_session, "customer_details", {}), "email", "")
        )
        if verified and email:
            _set_context_edge_session(email)

        return render_template(
            "context_edge_success.html",
            verified=verified,
            session_id=session_id if verified else "",
            email=(email or "").strip().lower(),
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="" if verified else "We could not verify your checkout yet. Please contact support if this continues.",
        )
    except Exception as e:
        logger.error(f"[CONTEXT EDGE STRIPE] Success verification error: {e}")
        return render_template(
            "context_edge_success.html",
            verified=False,
            session_id="",
            email="",
            meta_pixel_id=os.environ.get("META_PIXEL_ID", "1688269505510635"),
            error="We could not verify your checkout yet. Please contact support if this continues.",
        ), 400


@app.route('/mora-assists-setup')
def mora_assists_setup():
    """Onboarding instructions page — linked from welcome page. No pixel fires here."""
    return render_template('mora_assists_setup.html')


@app.route('/no-vig-calculator')
def no_vig_calculator():
    return render_template('seo_no_vig_calculator.html')


@app.route('/what-is-vig-sports-betting')
def what_is_vig():
    return render_template('seo_what_is_vig.html')


@app.route('/mlb-player-props-explained')
def mlb_props_explained():
    return render_template('seo_mlb_props.html')


@app.route('/how-to-find-value-bets-mlb')
def value_bets():
    return render_template('seo_value_bets.html')


@app.route('/casino')
def casino_page():
    return render_template('casino.html')


@app.route('/crypto-guide')
def crypto_guide_page():
    return render_template('crypto_guide.html')


@app.route('/downloads/cheatsheet')
def download_cheatsheet():
    from flask import send_file
    import os
    pdf_path = os.path.join(os.path.dirname(__file__), 'static', 'downloads', 'logical_gambler_cheatsheet.pdf')
    return send_file(
        pdf_path,
        as_attachment=True,
        download_name='LogicalGambler_CheatSheet.pdf'
    )


@app.route('/manifest.json')
def pwa_manifest():
    """PWA web app manifest for home screen install."""
    return jsonify({
        "name": "Fade the Books",
        "short_name": "Fade Books",
        "description": "No-vig board and Context Edge scans",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#4CBB17",
        "icons": [
            {
                "src": "/static/fade-the-books-home-192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/fade-the-books-home-512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    })


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


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/sitemap.xml")
def sitemap():
    from flask import Response as _Response
    today = datetime.utcnow().strftime("%Y-%m-%d")
    sitemap_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://fadethebooks.com/dashboard</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/how-it-works</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/mora-assists-welcome</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.5</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/mora-assists</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/no-vig-calculator</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/what-is-vig-sports-betting</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/mlb-player-props-explained</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/how-to-find-value-bets-mlb</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/casino</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.7</priority>
  </url>
  <url>
    <loc>https://fadethebooks.com/crypto-guide</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.6</priority>
  </url>
</urlset>"""
    return _Response(sitemap_xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots_txt():
    from flask import Response as _Response
    content = """User-agent: *
Allow: /
Allow: /how-it-works

Disallow: /api/
Disallow: /dashboard
Disallow: /api/debug/
Disallow: /admin/

Sitemap: https://fadethebooks.com/sitemap.xml"""
    return _Response(content, mimetype="text/plain")


@app.after_request
def add_cache_headers(response):
    """Cache-Control headers for SEO and performance."""
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000"
    elif request.path in ["/sitemap.xml", "/robots.txt"]:
        response.headers["Cache-Control"] = "public, max-age=86400"
    elif request.path == "/dashboard":
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/dashboard")
def dashboard():
    """Main Fade the Books dashboard for verified members."""
    if not _session_has_context_edge_access():
        return redirect(url_for("home", access="required"))

    try:
        hits = cache_incr("hits")
        return render_template("dashboard.html", hits=hits)
    except Exception as e:
        logger.error(f"Error in dashboard route: {e}")
        return f'<h1>Fade the Books</h1><p>Error: {str(e)}</p><p><a href="/health">Health Check</a></p>'


@app.route("/affiliate")
def affiliate_portal():
    """Protected placeholder portal for approved FadeTheBooks partners."""
    email = _session_verified_email()
    if not email:
        return redirect(url_for("home", affiliate="required"))
    if not _session_has_affiliate_access():
        return render_template("affiliate.html", email=email, denied=True), 403
    return render_template("affiliate.html", email=email, denied=False)


@app.before_request
def before_request():
    """Request hook reserved for lightweight app-wide concerns."""
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

PROP_CACHE_MAX_AGE_SECONDS = 43200  # twice-daily refresh window
LINE_CACHE_MAX_AGE_SECONDS = 43200
CACHE_FALLBACK_DIR = os.environ.get("CACHE_FALLBACK_DIR", "/tmp/fadethebooks-cache")

def _cache_fallback_path(cache_file):
    return os.path.join(CACHE_FALLBACK_DIR, os.path.basename(cache_file))

def _cache_candidates(cache_file):
    fallback = _cache_fallback_path(cache_file)
    return [cache_file] if fallback == cache_file else [cache_file, fallback]

def _existing_cache_path(cache_file):
    return next((candidate for candidate in _cache_candidates(cache_file) if os.path.exists(candidate)), None)

def _load_json_cache_file(cache_file):
    for candidate in _cache_candidates(cache_file):
        try:
            from enrichment import load_props_from_file
            if os.path.exists(candidate):
                return load_props_from_file(candidate)
        except Exception as e:
            logger.warning(f"[CACHE] Could not read {candidate}: {e}")
    return []

def _json_cache_is_fresh(cache_file, max_age_seconds):
    try:
        existing = _existing_cache_path(cache_file)
        return bool(existing) and (time.time() - os.path.getmtime(existing)) < max_age_seconds
    except OSError:
        return False

def _write_json_cache_file(cache_file, payload):
    for candidate in _cache_candidates(cache_file):
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            with open(candidate, 'w') as f:
                json.dump(payload, f)
            if candidate != cache_file:
                logger.info(f"[CACHE] Wrote fallback cache {candidate}")
            return True
        except Exception as e:
            logger.warning(f"[CACHE] Could not write {candidate}: {e}")
    return False

def _cache_age_seconds(cache_file):
    try:
        existing = _existing_cache_path(cache_file)
        if existing:
            return round(time.time() - os.path.getmtime(existing))
    except OSError:
        pass
    return None

_cache_warmups = {}
_cache_job_status = {}

def _record_cache_job(cache_key, status, message=None):
    _cache_job_status[cache_key] = {
        "status": status,
        "message": message,
        "updated_at": datetime.utcnow().isoformat(),
    }

def _queue_cache_warmup(cache_key, refresh_func, min_interval_seconds=43200):
    """Kick off one background cache refresh per window when Render has no file cache."""
    now_ts = time.time()
    last_ts = _cache_warmups.get(cache_key)
    if last_ts and (now_ts - last_ts) < min_interval_seconds:
        logger.info(f"[CACHE] Warmup already queued recently for {cache_key}")
        return False

    _cache_warmups[cache_key] = now_ts

    def _run():
        try:
            logger.info(f"[CACHE] Background warmup started for {cache_key}")
            _record_cache_job(cache_key, "started")
            with app.app_context():
                result = refresh_func()
            result_count = len(result) if isinstance(result, list) else None
            _record_cache_job(cache_key, "completed", f"result_count={result_count}")
            logger.info(f"[CACHE] Background warmup completed for {cache_key}")
        except Exception as e:
            _record_cache_job(cache_key, "failed", str(e))
            logger.warning(f"[CACHE] Background warmup failed for {cache_key}: {e}", exc_info=True)

    from threading import Thread
    Thread(target=_run, daemon=True).start()
    return True

@app.route("/api/mlb/props")
def mlb_props():
    try:
        from enrichment import load_props_from_file

        cache_file = "/var/data/mlb_props_cache.json"
        props = _load_json_cache_file(cache_file)
        if props:
            fresh = _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)
            logger.info(f"[MLB PROPS] Serving {len(props)} from {'fresh' if fresh else 'stale'} cache")
            return jsonify({
                "props":  props,
                "count":  len(props),
                "sport":  "MLB",
                "cached": True,
                "stale": not fresh,
                "cache_age_seconds": _cache_age_seconds(cache_file),
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW":  len([p for p in props if p.get("confidence_tier") == "LOW"])
                }
            })

        logger.info("[MLB PROPS] No cache available — queueing guarded background warmup")
        _queue_cache_warmup("mlb_props", _fetch_and_process_mlb_props, min_interval_seconds=300)
        return jsonify({
            "props": [],
            "count": 0,
            "sport": "MLB",
            "cached": False,
            "status": "not_available",
            "message": "MLB props refresh twice daily — check back after the next board update."
        })

    except Exception as e:
        logger.error(f"[MLB PROPS] Route error: {e}")
        return jsonify({"error": str(e), "props": [], "count": 0}), 500

@app.route("/player_props")
def get_props():
    """Get enriched props grouped by matchup with optional filtering (Underdog Fantasy style)"""
    try:
        from enrichment import load_props_from_file
        
        # Load props from file cache (no Redis dependency)
        props_data = load_props_from_file("/var/data/mlb_props_cache.json")
        
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
    """Capture landing-page leads and sync to Brevo with local backup."""
    try:
        data = request.get_json(force=True) or {}
        email = (data.get('email') or '').strip().lower()
        name = (data.get('name') or '').strip()
        phone = (data.get('phone') or '').strip()
        source = (data.get('source') or 'daily_dashboard_trial').strip()
        if not email or '@' not in email:
            return jsonify({'error': 'Invalid email'}), 400
        if source in {'daily_board_preview', 'daily_dashboard_trial', 'trial_downsell_newsletter'} and not name:
            return jsonify({'error': 'Name is required'}), 400
        if source in {'daily_dashboard_trial', 'trial_downsell_newsletter'} and not phone:
            return jsonify({'error': 'Phone is required'}), 400

        brevo_synced = _sync_to_brevo_contact(email, name=name, phone=phone, source=source)
        save_subscriber(email, name=name, phone=phone, source=source)
        if not brevo_synced:
            logger.warning(f'[SUBSCRIBE] Brevo did not confirm lead sync for {email}; skipping Meta Lead')
            return jsonify({
                'error': 'Could not save contact. Try again.',
                'external': {'brevo': False},
            }), 502

        event_id = None
        try:
            from meta_pixel import track_lead
            event_id = _lead_event_id(email, source)
            track_lead(request, customer_email=email, event_id=event_id)
            logger.info(f'[META] Dashboard trial Lead fired for {email}')
        except Exception as meta_err:
            logger.warning(f'[META] Dashboard trial Lead failed: {meta_err}')

        return jsonify({
            'status': 'ok',
            'message': 'Subscribed',
            'event_id': event_id,
            'external': {'brevo': brevo_synced},
        }), 200
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
            "message": "Welcome to Fade the Books API!",
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
def mlb_odds(force_refresh=False):
    """Curated MLB picks — h2h, spreads, totals — with no-vig probability."""
    try:
        from ev_engine import evaluate_pick

        cache_file = "/var/data/mlb_odds_cache.json"
        cached_payload = _load_json_cache_file(cache_file)
        if cached_payload and (not force_refresh or _json_cache_is_fresh(cache_file, LINE_CACHE_MAX_AGE_SECONDS)):
            cached_payload["cached"] = True
            cached_payload["cache_age_seconds"] = _cache_age_seconds(cache_file)
            return jsonify(cached_payload)

        if not force_refresh:
            logger.info("[MLB ODDS] No cache available — queueing guarded background warmup")
            _queue_cache_warmup("game_lines", _fetch_and_cache_game_lines, min_interval_seconds=300)
            return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "MLB", "cached": False, "status": "not_available", "message": "MLB lines are warming up from cache. Refresh shortly."})

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

            # ── Evaluate h2h — only show the favored side (≥52% no-vig) ──
            for team, prices in [(home, h2h_side1), (away, h2h_side2)]:
                if not prices:
                    continue
                ev = evaluate_pick(
                    player_or_team=team, market_type="h2h", side="over",
                    line=None, book_prices=prices, game_time=game_time
                )
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                # Hard filter — only show favored side with ≥52% no-vig
                if nv < 52.0:
                    continue
                picks.append({
                    "player":          team,
                    "stat":            "h2h",
                    "stat_label":      "Moneyline Win",
                    "line":            None,
                    "no_vig_prob":     nv,
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
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                if nv < 50.0:
                    continue
                label = f"Run Line {'+' if point > 0 else ''}{point}"
                picks.append({
                    "player":          team,
                    "stat":            "spreads",
                    "stat_label":      label,
                    "line":            point,
                    "no_vig_prob":     nv,
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
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                if nv < 50.0:
                    continue
                picks.append({
                    "player":          matchup,
                    "stat":            "totals",
                    "stat_label":      side_label,
                    "line":            total_point,
                    "no_vig_prob":     nv,
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
        # Edge: positive EV AND 55%+ true probability
        edge_picks = [
            p for p in picks
            if (
                p.get("ev_pct") is not None
                and p.get("ev_pct") > 0
                and p.get("no_vig_prob", 0) >= 55.0
            )
        ]
        edge_picks.sort(key=lambda p: -(p.get("ev_pct") or 0))

        # No-vig board: everything 52%+ (edge picks also appear here)
        no_vig_picks = [p for p in picks if (p.get("no_vig_prob") or 0) >= 52.0]
        no_vig_picks.sort(key=lambda p: -(p.get("no_vig_prob") or 0))

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

        payload = {
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
        }
        _write_json_cache_file(cache_file, payload)
        return jsonify(payload)

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
        cache_file = "/var/data/nfl_environment_cache.json"
        env_map = _load_json_cache_file(cache_file)
        if isinstance(env_map, dict) and env_map:
            logger.info("[NFL ENV] Serving %d matchups from cache", len(env_map))
        else:
            logger.info("[NFL ENV] No cache available - queueing guarded background warmup")
            _queue_cache_warmup("nfl_props", _fetch_and_process_nfl_props, min_interval_seconds=300)
            env_map = {}
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

@app.route("/api/best-opportunities")
def api_best_opportunities():
    """Member dashboard assist board built from cached odds/props only."""
    try:
        from best_opportunities_service import build_best_opportunities_report

        line_payloads = {
            "MLB": _load_json_cache_file("/var/data/mlb_odds_cache.json"),
            "NBA": _load_json_cache_file("/var/data/nba_odds_cache.json"),
            "NHL": _load_json_cache_file("/var/data/nhl_odds_cache.json"),
        }
        prop_payloads = {
            "MLB": _load_json_cache_file("/var/data/mlb_props_cache.json"),
            "NBA": _load_json_cache_file("/var/data/nba_props_cache.json"),
            "NHL": _load_json_cache_file("/var/data/nhl_props_cache.json"),
            "Soccer": _load_json_cache_file("/var/data/soccer_props_cache.json"),
            "NFL": _load_json_cache_file("/var/data/nfl_props_cache.json"),
        }
        report = build_best_opportunities_report(line_payloads=line_payloads, prop_payloads=prop_payloads)
        return jsonify(report)
    except Exception as e:
        logger.error(f"[BEST OPPORTUNITIES] Route error: {e}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": "Best opportunities are temporarily unavailable.",
            "opportunities": [],
            "count": 0,
        }), 500

@app.route("/api/nhl/props")
def api_nhl_props():
    """
    NHL player props — serves from file cache only.
    Never does a blocking live fetch (that takes 15-30s with sequential calls).
    The background scheduler populates the cache at 10:15 AM ET daily.
    """
    try:
        from enrichment import load_props_from_file

        cache_file = "/var/data/nhl_props_cache.json"
        props = _load_json_cache_file(cache_file)
        if props:
            fresh = _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)
            logger.info(f"[NHL PROPS] Serving {len(props)} from {'fresh' if fresh else 'stale'} cache")
            return jsonify({
                "props":  props,
                "count":  len(props),
                "sport":  "NHL",
                "cached": True,
                "stale": not fresh,
                "cache_age_seconds": _cache_age_seconds(cache_file),
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW":  len([p for p in props if p.get("confidence_tier") == "LOW"])
                }
            })

        logger.info("[NHL PROPS] No cache available — queueing guarded background warmup")
        _queue_cache_warmup("nhl_props", _fetch_and_process_nhl_props, min_interval_seconds=300)
        return jsonify({
            "props":  [],
            "count":  0,
            "sport":  "NHL",
            "cached": False,
            "status": "not_available",
            "message": "NHL props update at 10 AM ET · Check back before first puck drop"
        })

    except Exception as e:
        logger.error(f"[NHL] /api/nhl/props error: {e}")
        return jsonify({"props": [], "count": 0, "sport": "NHL", "error": str(e)}), 500

@app.route("/api/soccer/props")
def api_soccer_props():
    """
    Soccer player props (MLS + FIFA World Cup) — serves from file cache only.
    Mirrors /api/nhl/props exactly. Background scheduler populates the cache
    at 7:45 AM PHX daily.
    """
    try:
        from enrichment import load_props_from_file

        cache_file = "/var/data/soccer_props_cache.json"
        props = _load_json_cache_file(cache_file)
        if props:
            fresh = _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)
            logger.info(f"[SOCCER PROPS] Serving {len(props)} from {'fresh' if fresh else 'stale'} cache")
            return jsonify({
                "props":  props,
                "count":  len(props),
                "sport":  "Soccer",
                "cached": True,
                "stale": not fresh,
                "cache_age_seconds": _cache_age_seconds(cache_file),
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW":  len([p for p in props if p.get("confidence_tier") == "LOW"])
                }
            })

        logger.info("[SOCCER PROPS] No cache available — queueing guarded background warmup")
        _queue_cache_warmup("soccer_props", _fetch_and_process_soccer_props, min_interval_seconds=300)
        return jsonify({
            "props":  [],
            "count":  0,
            "sport":  "Soccer",
            "cached": False,
            "status": "not_available",
            "message": "Soccer props refresh daily — check back later or when MLS/FIFA games are scheduled"
        })

    except Exception as e:
        logger.error(f"[SOCCER] /api/soccer/props error: {e}")
        return jsonify({"props": [], "count": 0, "sport": "Soccer", "error": str(e)}), 500


@app.route("/api/soccer/gamelines")
def api_soccer_gamelines(force_refresh=False):
    """
    Soccer game-level lines (MLS + FIFA World Cup): 3-way h2h moneyline + totals.
    Mirrors the MLB Best Lines pattern (/api/mlb/odds): serves fresh file cache
    when available, otherwise does a cheap live fetch (one /odds call per league).

    Returns the raw game lines plus an `environments` map keyed by
    "{away_team} @ {home_team}" so the dashboard can attach the HIGH/LOW SCORING
    tag and favored-team win-probability badge to each soccer game card.
    """
    try:
        cache_file = "/var/data/soccer_gamelines_cache.json"
        games = None

        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    games = json.load(f)
                fresh = _json_cache_is_fresh(cache_file, LINE_CACHE_MAX_AGE_SECONDS)
                logger.info(f"[SOCCER LINES] Serving {len(games)} from {'fresh' if fresh else 'stale'} cache")
            except Exception:
                games = None

        if games is None:
            if not force_refresh:
                logger.info("[SOCCER LINES] No cache available — queueing guarded background warmup")
                _queue_cache_warmup("game_lines", _fetch_and_cache_game_lines, min_interval_seconds=300)
                return jsonify({"games": [], "environments": {}, "count": 0, "sport": "Soccer", "cached": False, "status": "not_available", "message": "Soccer lines are warming up from cache. Refresh shortly."})
            from soccer_odds_api import fetch_soccer_game_lines
            games = fetch_soccer_game_lines()
            _write_json_cache_file(cache_file, games or [])

        environments = {}
        for g in (games or []):
            key = f"{g.get('away_team','')} @ {g.get('home_team','')}"
            environments[key] = {
                "favored_team": g.get("favored_team"),
                "favored_prob": g.get("favored_prob"),
                "environment":  g.get("environment", ""),
                "no_vig_home":  g.get("no_vig_home"),
                "no_vig_draw":  g.get("no_vig_draw"),
                "no_vig_away":  g.get("no_vig_away"),
                "home_team":    g.get("home_team"),
                "away_team":    g.get("away_team"),
            }

        return jsonify({
            "games":        games or [],
            "environments": environments,
            "count":        len(games or []),
            "sport":        "Soccer",
        })

    except Exception as e:
        logger.error(f"[SOCCER] /api/soccer/gamelines error: {e}")
        return jsonify({"games": [], "environments": {}, "count": 0, "sport": "Soccer", "error": str(e)}), 500




def _build_game_line_payload(raw_games, sport_label, spread_label="Spread", total_label="Game Total"):
    """Normalize h2h/spreads/totals Odds API games into dashboard no-vig picks."""
    from ev_engine import evaluate_pick

    picks = []
    for game in raw_games or []:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        game_time = game.get("commence_time", "")
        matchup = f"{away} @ {home}"

        h2h_home = []
        h2h_away = []
        spread_sides = {}
        total_over_prices = []
        total_under_prices = []
        total_point = None

        for bk in game.get("bookmakers", []):
            bk_name = bk.get("title", "").lower()
            for market in bk.get("markets", []):
                mk = market.get("key", "")
                outcomes = market.get("outcomes", [])

                if mk == "h2h" and len(outcomes) >= 2:
                    for outcome in outcomes:
                        other = next((x for x in outcomes if x != outcome), None)
                        if not other:
                            continue
                        price = outcome.get("price")
                        opp_price = other.get("price")
                        if price is None or opp_price is None:
                            continue
                        if outcome.get("name") == home:
                            h2h_home.append({"book": bk_name, "over_price": price, "under_price": opp_price})
                        elif outcome.get("name") == away:
                            h2h_away.append({"book": bk_name, "over_price": price, "under_price": opp_price})

                elif mk == "spreads" and len(outcomes) >= 2:
                    for outcome in outcomes:
                        other = next((x for x in outcomes if x != outcome), None)
                        if not other:
                            continue
                        team = outcome.get("name", "")
                        price = outcome.get("price")
                        point = outcome.get("point")
                        opp_price = other.get("price")
                        if price is None or opp_price is None or point is None:
                            continue
                        spread_sides.setdefault((team, point), []).append({
                            "book": bk_name,
                            "over_price": price,
                            "under_price": opp_price,
                        })

                elif mk == "totals" and len(outcomes) >= 2:
                    over_o = next((o for o in outcomes if o.get("name") == "Over"), None)
                    under_o = next((o for o in outcomes if o.get("name") == "Under"), None)
                    if over_o and under_o:
                        over_price = over_o.get("price")
                        under_price = under_o.get("price")
                        point = over_o.get("point")
                        if over_price is not None and under_price is not None and point is not None:
                            total_point = point
                            total_over_prices.append({"book": bk_name, "over_price": over_price, "under_price": under_price})
                            total_under_prices.append({"book": bk_name, "over_price": under_price, "under_price": over_price})

        for team, prices in [(home, h2h_home), (away, h2h_away)]:
            if not prices:
                continue
            ev = evaluate_pick(team, "h2h", "over", None, prices, game_time)
            no_vig_prob = round((ev["fair_probability"] or 0) * 100, 1)
            if no_vig_prob < 52.0:
                continue
            picks.append({
                "player": team,
                "stat": "h2h",
                "stat_label": "Moneyline Win",
                "line": None,
                "no_vig_prob": no_vig_prob,
                "fair_odds": ev["fair_odds"],
                "ev_pct": ev["ev_pct"],
                "edge_pct": ev["edge_pct"],
                "confidence_tier": ev["confidence_tier"],
                "best_over_price": ev["best_offered_odds"],
                "best_book": ev["best_book"],
                "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                "book_count": ev["book_count"],
                "matchup": matchup,
                "game_time": game_time,
                "sport": sport_label,
                "market_type": "moneyline",
                "all_books": prices,
                "passes_threshold": ev["passes_threshold"],
                "surfaced": ev["passes_threshold"],
            })

        for (team, point), prices in spread_sides.items():
            ev = evaluate_pick(team, "spreads", "over", point, prices, game_time)
            no_vig_prob = round((ev["fair_probability"] or 0) * 100, 1)
            if no_vig_prob < 50.0:
                continue
            picks.append({
                "player": team,
                "stat": "spreads",
                "stat_label": f"{spread_label} {'+' if point > 0 else ''}{point}",
                "line": point,
                "no_vig_prob": no_vig_prob,
                "fair_odds": ev["fair_odds"],
                "ev_pct": ev["ev_pct"],
                "edge_pct": ev["edge_pct"],
                "confidence_tier": ev["confidence_tier"],
                "best_over_price": ev["best_offered_odds"],
                "best_book": ev["best_book"],
                "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                "book_count": ev["book_count"],
                "matchup": matchup,
                "game_time": game_time,
                "sport": sport_label,
                "market_type": "spread",
                "all_books": prices,
                "passes_threshold": ev["passes_threshold"],
                "surfaced": ev["passes_threshold"],
            })

        for side_label, prices, side in [
            (f"{total_label} Over {total_point}", total_over_prices, "over"),
            (f"{total_label} Under {total_point}", total_under_prices, "under"),
        ]:
            if not prices or total_point is None:
                continue
            ev = evaluate_pick(matchup, "totals", side, total_point, prices, game_time)
            no_vig_prob = round((ev["fair_probability"] or 0) * 100, 1)
            if no_vig_prob < 50.0:
                continue
            picks.append({
                "player": matchup,
                "stat": "totals",
                "stat_label": side_label,
                "line": total_point,
                "no_vig_prob": no_vig_prob,
                "fair_odds": ev["fair_odds"],
                "ev_pct": ev["ev_pct"],
                "edge_pct": ev["edge_pct"],
                "confidence_tier": ev["confidence_tier"],
                "best_over_price": ev["best_offered_odds"],
                "best_book": ev["best_book"],
                "break_even_prob": round((ev["break_even_prob"] or 0) * 100, 1),
                "book_count": ev["book_count"],
                "matchup": matchup,
                "game_time": game_time,
                "sport": sport_label,
                "market_type": "total",
                "all_books": prices,
                "passes_threshold": ev["passes_threshold"],
                "surfaced": ev["passes_threshold"],
            })

    edge_picks = [
        p for p in picks
        if p.get("ev_pct") is not None and p.get("ev_pct") > 0 and p.get("no_vig_prob", 0) >= 55.0
    ]
    edge_picks.sort(key=lambda p: -(p.get("ev_pct") or 0))
    no_vig_picks = [p for p in picks if (p.get("no_vig_prob") or 0) >= 52.0]
    no_vig_picks.sort(key=lambda p: -(p.get("no_vig_prob") or 0))
    avg_ev = round(sum(p.get("ev_pct") or 0 for p in edge_picks) / len(edge_picks), 1) if edge_picks else 0

    return {
        "no_vig_picks": no_vig_picks[:30],
        "edge_picks": edge_picks,
        "picks": no_vig_picks[:30],
        "counts": {
            "no_vig": len(no_vig_picks),
            "edge": len(edge_picks),
            "implied": len([p for p in no_vig_picks if p.get("implied_only")]),
        },
        "count": len(no_vig_picks),
        "sport": sport_label,
        "avg_ev": avg_ev,
        "tiers": {
            "LOCK": len([p for p in edge_picks if p.get("confidence_tier") == "LOCK"]),
            "FIRE": len([p for p in edge_picks if p.get("confidence_tier") == "FIRE"]),
            "EDGE": len([p for p in edge_picks if p.get("confidence_tier") == "EDGE"]),
        },
    }


@app.route("/api/nba/props")
def api_nba_props():
    """NBA player props — serves from file cache, fetches on cache miss."""
    try:
        from enrichment import load_props_from_file

        cache_file = "/var/data/nba_props_cache.json"
        props = _load_json_cache_file(cache_file)
        if props:
            fresh = _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)
            logger.info(f"[NBA PROPS] Serving {len(props)} from {'fresh' if fresh else 'stale'} cache")
            return jsonify({
                "props": props,
                "count": len(props),
                "sport": "NBA",
                "cached": True,
                "stale": not fresh,
                "cache_age_seconds": _cache_age_seconds(cache_file),
                "tiers": {
                    "LOCK": len([p for p in props if p.get("confidence_tier") == "LOCK"]),
                    "FIRE": len([p for p in props if p.get("confidence_tier") == "FIRE"]),
                    "LOW": len([p for p in props if p.get("confidence_tier") == "LOW"]),
                },
            })

        logger.info("[NBA PROPS] No cache available - queueing guarded background warmup")
        _queue_cache_warmup("nba_props", _fetch_and_process_nba_props, min_interval_seconds=300)
        return jsonify({
            "props": [],
            "count": 0,
            "sport": "NBA",
            "cached": False,
            "status": "not_available",
            "message": "NBA props refresh twice daily - check back when games are scheduled",
        })
    except Exception as e:
        logger.error(f"[NBA] /api/nba/props error: {e}")
        return jsonify({"props": [], "count": 0, "sport": "NBA", "error": str(e)}), 500


@app.route("/api/nba/odds")
def api_nba_odds(force_refresh=False):
    """Curated NBA picks — h2h, spreads, totals — evaluated with EV engine."""
    try:
        from nba_odds_api import fetch_nba_game_odds
        cache_file = "/var/data/nba_odds_cache.json"
        cached_payload = _load_json_cache_file(cache_file)
        if cached_payload and (not force_refresh or _json_cache_is_fresh(cache_file, LINE_CACHE_MAX_AGE_SECONDS)):
            cached_payload["cached"] = True
            cached_payload["cache_age_seconds"] = _cache_age_seconds(cache_file)
            return jsonify(cached_payload)

        if not force_refresh:
            logger.info("[NBA ODDS] No cache available - queueing guarded background warmup")
            _queue_cache_warmup("game_lines", _fetch_and_cache_game_lines, min_interval_seconds=300)
            return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "NBA", "cached": False, "status": "not_available", "message": "NBA lines are warming up from cache. Refresh shortly."})

        raw_games = fetch_nba_game_odds()
        payload = _build_game_line_payload(raw_games, "NBA", spread_label="Spread", total_label="Game Total")
        _write_json_cache_file(cache_file, payload)
        return jsonify(payload)
    except Exception as e:
        logger.error(f"[NBA] /api/nba/odds error: {e}")
        return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "NBA", "error": str(e)}), 500


@app.route("/api/nba/environment")
def api_nba_environment():
    try:
        from nba_odds_api import get_nba_game_environment_map
        env_map = get_nba_game_environment_map()
        return jsonify({"environments": env_map, "count": len(env_map), "sport": "NBA"})
    except Exception as e:
        logger.error(f"[NBA ENV] error: {e}")
        return jsonify({"environments": {}, "count": 0, "sport": "NBA", "error": str(e)}), 500

@app.route("/api/nhl/odds")
def api_nhl_odds(force_refresh=False):
    """Curated NHL picks — h2h, spreads, totals — evaluated with EV engine."""
    try:
        from nhl_odds_api import fetch_nhl_game_odds
        from ev_engine import evaluate_pick

        cache_file = "/var/data/nhl_odds_cache.json"
        cached_payload = _load_json_cache_file(cache_file)
        if cached_payload and (not force_refresh or _json_cache_is_fresh(cache_file, LINE_CACHE_MAX_AGE_SECONDS)):
            cached_payload["cached"] = True
            cached_payload["cache_age_seconds"] = _cache_age_seconds(cache_file)
            return jsonify(cached_payload)

        if not force_refresh:
            logger.info("[NHL ODDS] No cache available - queueing guarded background warmup")
            _queue_cache_warmup("game_lines", _fetch_and_cache_game_lines, min_interval_seconds=300)
            return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "NHL", "cached": False, "status": "not_available", "message": "NHL lines are warming up from cache. Refresh shortly."})

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

            # ── Evaluate h2h — only show the favored side (≥52% no-vig) ──
            for team, prices in [(home, h2h_home), (away, h2h_away)]:
                if not prices:
                    continue
                ev = evaluate_pick(
                    player_or_team=team, market_type="h2h", side="over",
                    line=None, book_prices=prices, game_time=game_time
                )
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                if nv < 52.0:
                    continue
                picks.append({
                    "player":          team,
                    "stat":            "h2h",
                    "stat_label":      "Moneyline Win",
                    "line":            None,
                    "no_vig_prob":     nv,
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
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                if nv < 50.0:
                    continue
                label = f"Puck Line {'+' if point > 0 else ''}{point}"
                picks.append({
                    "player":          team,
                    "stat":            "spreads",
                    "stat_label":      label,
                    "line":            point,
                    "no_vig_prob":     nv,
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
                nv = round((ev["fair_probability"] or 0) * 100, 1)
                if nv < 50.0:
                    continue
                picks.append({
                    "player":          matchup,
                    "stat":            "totals",
                    "stat_label":      side_label,
                    "line":            total_point,
                    "no_vig_prob":     nv,
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
        # Edge: positive EV AND 55%+ true probability
        edge_picks = [
            p for p in picks
            if (
                p.get("ev_pct") is not None
                and p.get("ev_pct") > 0
                and p.get("no_vig_prob", 0) >= 55.0
            )
        ]
        edge_picks.sort(key=lambda p: -(p.get("ev_pct") or 0))

        # No-vig board: everything 52%+ (edge picks also appear here)
        no_vig_picks = [p for p in picks if (p.get("no_vig_prob") or 0) >= 52.0]
        no_vig_picks.sort(key=lambda p: -(p.get("no_vig_prob") or 0))

        avg_ev = round(sum(p.get("ev_pct") or 0 for p in edge_picks) / len(edge_picks), 1) if edge_picks else 0

        payload = {
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
        }
        _write_json_cache_file(cache_file, payload)
        return jsonify(payload)

    except Exception as e:
        logger.error(f"[NHL] /api/nhl/odds error: {e}")
        return jsonify({"picks": [], "no_vig_picks": [], "edge_picks": [], "count": 0, "sport": "NHL", "error": str(e)}), 500


def _fetch_and_cache_game_lines():
    """Refresh dashboard game-line caches on the scheduled cadence only."""
    jobs = [
        ("MLB", lambda: mlb_odds(force_refresh=True)),
        ("Soccer", lambda: api_soccer_gamelines(force_refresh=True)),
        ("NBA", lambda: api_nba_odds(force_refresh=True)),
        ("NHL", lambda: api_nhl_odds(force_refresh=True)),
    ]
    with app.app_context():
        for sport, job in jobs:
            try:
                job()
                logger.info(f"[{sport} ODDS] Scheduled line cache refresh complete")
            except Exception as e:
                logger.warning(f"[{sport} ODDS] Scheduled line cache refresh failed: {e}")



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

    mlb_props = load_props_from_file("/var/data/mlb_props_cache.json")
    nhl_props = load_props_from_file("/var/data/nhl_props_cache.json")
    nba_props = load_props_from_file("/var/data/nba_props_cache.json")
    nfl_props = load_props_from_file("/var/data/nfl_props_cache.json")
    soccer_props = load_props_from_file("/var/data/soccer_props_cache.json")

    def file_age_minutes(filename):
        try:
            mtime = os.path.getmtime(filename)
            return round((time.time() - mtime) / 60, 1)
        except Exception:
            return None

    return jsonify({
        "mlb": {
            "props_count": len(mlb_props),
            "cache_age_minutes": file_age_minutes("/var/data/mlb_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in mlb_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in mlb_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in mlb_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "nhl": {
            "props_count": len(nhl_props),
            "cache_age_minutes": file_age_minutes("/var/data/nhl_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in nhl_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in nhl_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in nhl_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "nba": {
            "props_count": len(nba_props),
            "cache_age_minutes": file_age_minutes("/var/data/nba_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in nba_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in nba_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in nba_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "nfl": {
            "props_count": len(nfl_props),
            "cache_age_minutes": file_age_minutes("/var/data/nfl_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in nfl_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in nfl_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in nfl_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "soccer": {
            "props_count": len(soccer_props),
            "cache_age_minutes": file_age_minutes("/var/data/soccer_props_cache.json"),
            "tiers": {
                "LOCK": len([p for p in soccer_props if p.get("confidence_tier") == "LOCK"]),
                "FIRE": len([p for p in soccer_props if p.get("confidence_tier") == "FIRE"]),
                "LOW": len([p for p in soccer_props if p.get("confidence_tier") == "LOW"])
            }
        },
        "next_refresh": "Twice daily at 7:00 AM and 3:00 PM Phoenix time, staggered by sport",
        "strategy": "Scheduled cache refreshes keep the dashboard fast and protect Odds API quota."
    })

@app.route("/api/mlb/props/enhanced")
def get_enhanced_mlb_props():
    """Get MLB props with deep game context analysis"""
    try:
        from enrichment import load_props_from_file
        
        # Load props from file cache
        props_data = load_props_from_file("/var/data/mlb_props_cache.json")
        
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
    - Serves cached props for dashboard stability and API quota safety
    - Queues guarded background warmup on cache miss
    - Live fetch only happens from scheduler/manual/debug paths
    """
    try:
        logger.info("[NFL] /api/nfl/props called")

        cache_file = "/var/data/nfl_props_cache.json"
        props = _load_json_cache_file(cache_file)
        existing_cache = _existing_cache_path(cache_file)
        if props or (existing_cache and _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)):
            fresh = _json_cache_is_fresh(cache_file, PROP_CACHE_MAX_AGE_SECONDS)
            logger.info("[NFL PROPS] Serving %d from %s cache", len(props), "fresh" if fresh else "stale")
            return jsonify(props)

        logger.info("[NFL PROPS] No cache available - queueing guarded background warmup")
        _queue_cache_warmup("nfl_props", _fetch_and_process_nfl_props, min_interval_seconds=300)
        return jsonify([])

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

def _fetch_and_process_mlb_props():
    """
    Fetch, group, and cache MLB props.
    Returns the processed props list.
    """
    try:
        from odds_api import (
            fetch_player_props as _fetch_player_props,
            group_props_by_player as _group_props_by_player,
        )
        from enrichment import cache_props_to_file

        logger.info("[MLB PROPS] Starting fetch...")

        raw = _fetch_player_props()

        if not raw:
            logger.warning("[MLB PROPS] No raw props returned")
            return []

        logger.info(f"[MLB PROPS] {len(raw)} raw props — grouping now")

        props = _group_props_by_player(raw)

        logger.info(f"[MLB PROPS] {len(props)} props after grouping")

        if not props:
            logger.warning("[MLB PROPS] 0 props after grouping")
            return []

        cache_props_to_file(props, "/var/data/mlb_props_cache.json")
        logger.info(f"[MLB PROPS] Cached {len(props)} props ✅")

        return props

    except Exception as e:
        logger.error(f"[MLB PROPS] _fetch_and_process failed: {e}", exc_info=True)
        return []


def _fetch_and_process_nhl_props():
    """
    Fetch, group, and cache NHL props.
    Falls back to stale cache when all events return 422.
    """
    try:
        from nhl_odds_api import fetch_player_props
        from odds_api import group_props_by_player as _group_props_by_player
        from enrichment import cache_props_to_file, load_props_from_file

        logger.info("[NHL PROPS] Starting fetch...")

        raw = fetch_player_props()

        if not raw:
            logger.warning("[NHL PROPS] No raw props — checking yesterday's cache")
            stale = load_props_from_file("/var/data/nhl_props_cache.json")
            if stale:
                logger.info(
                    f"[NHL PROPS] Serving {len(stale)} stale props "
                    f"(no fresh data available)"
                )
            return stale or []

        props = _group_props_by_player(raw)

        if not props:
            logger.warning("[NHL PROPS] 0 after grouping")
            return []

        cache_props_to_file(props, "/var/data/nhl_props_cache.json")
        logger.info(f"[NHL PROPS] Cached {len(props)} ✅")

        return props

    except Exception as e:
        logger.error(f"[NHL PROPS] failed: {e}", exc_info=True)
        return []


def _fetch_and_process_soccer_props():
    """
    Fetch, group, and cache Soccer props (MLS + FIFA World Cup).
    Mirrors _fetch_and_process_nhl_props exactly.
    Empty result is valid off-season — falls back to stale cache.
    """
    try:
        from soccer_odds_api import fetch_player_props
        from odds_api import group_props_by_player as _group_props_by_player
        from enrichment import cache_props_to_file, load_props_from_file

        logger.info("[SOCCER PROPS] Starting fetch...")

        try:
            os.makedirs('/var/data', exist_ok=True)
        except OSError:
            # Read-only filesystem (e.g. local Replit) — cache write will
            # silently no-op downstream, mirroring MLB/NHL behavior.
            pass

        raw = fetch_player_props()

        if not raw:
            logger.info("[SOCCER PROPS] No raw props — checking yesterday's cache")
            stale = load_props_from_file("/var/data/soccer_props_cache.json")
            if stale:
                logger.info(
                    f"[SOCCER PROPS] Serving {len(stale)} stale props "
                    f"(no fresh data available)"
                )
            return stale or []

        props = _group_props_by_player(raw)

        if not props:
            logger.warning("[SOCCER PROPS] 0 after grouping")
            return []

        cache_props_to_file(props, "/var/data/soccer_props_cache.json")
        logger.info(f"[SOCCER PROPS] Cached {len(props)} ✅")

        return props

    except Exception as e:
        logger.error(f"[SOCCER PROPS] failed: {e}", exc_info=True)
        return []



def _fetch_and_process_nba_props():
    """Fetch, group, and cache NBA props."""
    try:
        from nba_odds_api import fetch_player_props
        from odds_api import group_props_by_player as _group_props_by_player
        from enrichment import cache_props_to_file, load_props_from_file

        logger.info("[NBA PROPS] Starting fetch...")
        try:
            os.makedirs('/var/data', exist_ok=True)
        except OSError:
            pass

        raw = fetch_player_props()
        if not raw:
            logger.info("[NBA PROPS] No raw props - checking stale cache")
            stale = load_props_from_file("/var/data/nba_props_cache.json")
            return stale or []

        props = _group_props_by_player(raw)
        if not props:
            logger.warning("[NBA PROPS] 0 after grouping")
            return []

        cache_props_to_file(props, "/var/data/nba_props_cache.json")
        logger.info(f"[NBA PROPS] Cached {len(props)}")
        return props
    except Exception as e:
        logger.error(f"[NBA PROPS] failed: {e}", exc_info=True)
        return []

def _normalize_nfl_events_to_props(events):
    """Normalize Odds API NFL event payloads into dashboard-ready prop rows."""
    enhanced_props: list[dict] = []

    for event in events or []:
        home_team = (event.get("home_team") or "").strip()
        away_team = (event.get("away_team") or "").strip()

        for bookmaker in (event.get("bookmakers") or []):
            title = (bookmaker.get("title") or "").strip()
            if title.lower() not in VALID_BOOK_TITLES:
                continue

            for market in (bookmaker.get("markets") or []):
                market_key = (market.get("key") or "").strip()

                pairs: dict[tuple, dict] = {}
                for oc in (market.get("outcomes") or []):
                    price = oc.get("price")
                    if not _valid_price(price):
                        continue

                    player_name = (oc.get("description") or "").strip()
                    if not player_name:
                        continue

                    point = oc.get("point", None)
                    side = (oc.get("name") or "").strip().lower()
                    key = (player_name, point, market_key)
                    entry = pairs.setdefault(key, {"over_odds": None, "under_odds": None})

                    if "over" in side:
                        entry["over_odds"] = price
                    elif "under" in side:
                        entry["under_odds"] = price

                for (player_name, point, mk), ou in pairs.items():
                    if ou.get("over_odds") is None and ou.get("under_odds") is None:
                        continue

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
                        "confidence": "Medium",
                        "team": "",
                        "team_abbr": "",
                        "team_status": "",
                        "hit_probability": 0.5,
                    })

    return enhanced_props

def _fetch_and_process_nfl_props():
    """Fetch, normalize, enrich, and cache NFL props plus environment context."""
    try:
        from enrichment import cache_props_to_file, load_props_from_file
        from nfl_odds_api import fetch_nfl_props, get_nfl_game_environment_map
        from nfl_game_enrichment import build_nfl_environment_map, enrich_nfl_props_with_context

        logger.info("[NFL PROPS] Starting fetch...")
        try:
            os.makedirs('/var/data', exist_ok=True)
        except OSError:
            pass

        try:
            events = fetch_nfl_props() or []
        except RuntimeError as e:
            msg = str(e)
            if "422" in msg or "INVALID_MARKET" in msg:
                logger.info("[NFL PROPS] Off-season/no prop markets: %s", msg)
                stale = load_props_from_file("/var/data/nfl_props_cache.json")
                return stale or []
            raise

        try:
            env_map = get_nfl_game_environment_map()
        except Exception as e:
            logger.warning("[NFL PROPS] Live environment fetch failed, deriving from prop events: %s", e)
            env_map = build_nfl_environment_map(events)
        _write_json_cache_file("/var/data/nfl_environment_cache.json", env_map)
        logger.info("[NFL PROPS] Cached environment for %d matchups", len(env_map))

        if not events:
            logger.info("[NFL PROPS] No events with props - checking stale cache")
            stale = load_props_from_file("/var/data/nfl_props_cache.json")
            if stale:
                return stale
            cache_props_to_file([], "/var/data/nfl_props_cache.json")
            return []

        props = _normalize_nfl_events_to_props(events)
        logger.info("[NFL PROPS] Normalized %d props", len(props))

        if not props:
            logger.info("[NFL PROPS] 0 props after filters - checking stale cache")
            stale = load_props_from_file("/var/data/nfl_props_cache.json")
            if stale:
                return stale
            cache_props_to_file([], "/var/data/nfl_props_cache.json")
            return []

        enriched = enrich_nfl_props_with_context(props, env_map)
        cache_props_to_file(enriched, "/var/data/nfl_props_cache.json")
        logger.info("[NFL PROPS] Cached %d props", len(enriched))
        return enriched

    except Exception as e:
        logger.error(f"[NFL PROPS] failed: {e}", exc_info=True)
        return []

def _fetch_and_cache_soccer_game_lines():
    """Fetch soccer game lines (h2h 3-way + totals) and cache to a SEPARATE
    file from player props so neither overwrites the other."""
    try:
        from soccer_odds_api import fetch_soccer_game_lines
        try:
            os.makedirs('/var/data', exist_ok=True)
        except OSError:
            pass
        lines = fetch_soccer_game_lines()
        if lines:
            try:
                with open("/var/data/soccer_gamelines_cache.json", "w") as f:
                    json.dump(lines, f)
            except OSError:
                # Read-only filesystem (e.g. local Replit dev) — write no-ops,
                # mirroring MLB/NHL cache behavior. Live fetch still serves data.
                pass
        return lines or []
    except Exception as e:
        logger.error(f"[SOCCER LINES] fetch/cache failed: {e}", exc_info=True)
        return []


def fetch_soccer_props_safe():
    """Safe wrapper for the soccer scheduler job — props AND game lines."""
    try:
        props = _fetch_and_process_soccer_props()
        gamelines = _fetch_and_cache_soccer_game_lines()
        logger.info(
            f"Soccer: {len(props)} props, {len(gamelines)} game lines cached"
        )
    except Exception as e:
        logger.error(f"Soccer fetch failed: {e}")


# Keep old names as aliases for backward compat with existing scheduler jobs
def update_player_props():
    return _fetch_and_process_mlb_props()

update_mlb_props = update_player_props

def update_nhl_props():
    return _fetch_and_process_nhl_props()

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

# MLB props — twice daily PHX (sharp consensus windows)
scheduler.add_job(
    func=lambda: _fetch_and_process_mlb_props(),
    trigger="cron",
    hour="7,15",
    minute=0,
    timezone="America/Phoenix",
    id="mlb_props_twice_daily",
    name="MLB Props 7AM + 3PM PHX",
    replace_existing=True
)

# NHL props — twice daily PHX (15 min offset to stagger API calls)
scheduler.add_job(
    func=lambda: _fetch_and_process_nhl_props(),
    trigger="cron",
    hour="7,15",
    minute=15,
    timezone="America/Phoenix",
    id="nhl_props_twice_daily",
    name="NHL Props 7:15AM + 3:15PM PHX",
    replace_existing=True
)


# NBA props — twice daily PHX (staggered after NHL)
scheduler.add_job(
    func=lambda: _fetch_and_process_nba_props(),
    trigger="cron",
    hour="7,15",
    minute=30,
    timezone="America/Phoenix",
    id="nba_props_twice_daily",
    name="NBA Props 7:30AM + 3:30PM PHX",
    replace_existing=True
)

# NFL props — twice daily PHX (staggered after NBA)
scheduler.add_job(
    func=lambda: _fetch_and_process_nfl_props(),
    trigger="cron",
    hour="7,15",
    minute=40,
    timezone="America/Phoenix",
    id="nfl_props_twice_daily",
    name="NFL Props 7:40AM + 3:40PM PHX",
    replace_existing=True
)

# Soccer props — twice daily PHX (after MLB/NHL/NBA/NFL)
scheduler.add_job(
    func=fetch_soccer_props_safe,
    trigger="cron",
    hour="7,15",
    minute=45,
    timezone="America/Phoenix",
    id="soccer_props_twice_daily",
    name="Soccer Props 7:45AM + 3:45PM PHX",
    replace_existing=True
)

# Game lines refresh twice daily
scheduler.add_job(
    func=_fetch_and_cache_game_lines,
    trigger="cron",
    hour="7,15",
    minute=30,
    timezone="America/Phoenix",
    id="game_lines_twice_daily",
    name="All Game Lines 7:30AM + 3:30PM PHX",
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

# Mora Assists daily picks email removed per request.



def send_board_email():
    """
    Runs every morning at 8:30 AM CST.
    Exports today's qualifying props
    and emails them to Jesus for
    manual pick selection.
    """
    import json, os
    from datetime import datetime

    logger.info('[BOARD EMAIL] Starting...')

    try:
        def load_json(path):
            if os.path.exists(path):
                with open(path) as f:
                    return json.load(f)
            return []

        mlb = load_json('/var/data/mlb_props_cache.json')
        nhl = load_json('/var/data/nhl_props_cache.json')
        soccer = load_json('/var/data/soccer_props_cache.json')
        all_props = mlb + nhl + soccer

        quality = [
            p for p in all_props
            if p.get('no_vig_prob', 0) >= 55
            and p.get('best_over_price', 0) != 0
            and p.get('best_over_price', 0) > -235
            and p.get('player', '')
            and not any(
                x in p.get('player', '')
                for x in ['Batter', 'Pitcher', '_']
            )
        ]

        quality.sort(
            key=lambda x: x.get('no_vig_prob', 0),
            reverse=True
        )

        today = datetime.utcnow().strftime('%A %B %d')

        lines = []
        lines.append(f'MORA BETS BOARD — {today}')
        lines.append(f'Qualifying props: {len(quality)}')
        lines.append('')

        for p in quality[:50]:
            over = p.get('best_over_price', 0)
            under = next((
                b.get('under_price')
                for b in p.get('all_books', [])
                if b.get('under_price')
            ), None)
            books = [b.get('book', '') for b in p.get('all_books', [])]
            stat = p.get('stat_label', p.get('stat', '')).replace('player_', '').replace('_', ' ').title()
            env = p.get('environment', 'no label')

            lines.append(
                f"{p.get('player', '')} | "
                f"{stat} OVER {p.get('line', '')} | "
                f"Over: {over} | "
                f"Under: {under} | "
                f"No-vig: {p.get('no_vig_prob', '')}% | "
                f"{p.get('away_team', '')} @ {p.get('home_team', '')} | "
                f"{env} | "
                f"{p.get('sport', '')} | "
                f"Books: {', '.join(books[:2])}"
            )

        board_text = '\n'.join(lines)

        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(
            api_key=os.environ.get('SENDGRID_API_KEY')
        )

        message = Mail(
            from_email=os.environ.get('EMAIL_FROM', 'picks@morabets.com'),
            to_emails='acdc5671@gmail.com',
            subject=f'⚡ Mora Bets Board — {today}',
            plain_text_content=board_text
        )

        sg.send(message)

        logger.info(f'[BOARD EMAIL] Sent. {len(quality)} props.')

    except Exception as e:
        logger.error(f'[BOARD EMAIL] Failed: {e}')


# Global flag to track initialization
app_initialized = False

CONTEXT_EDGE_OUTPUT_KEYS = [
    "mlb_value",
    "soccer_value",
    "plus_money",
    "nfl_value",
]


def _context_edge_scheduler_enabled():
    return os.environ.get("CONTEXT_EDGE_SCHEDULER_ENABLED", "").lower() == "true"


def _run_context_edge_button_output_job(output_key, run_window):
    logger.info(f"[CONTEXT EDGE] Started {run_window} generation for {output_key}")
    try:
        from context_edge_button_generator_service import generate_context_edge_button_output

        result = generate_context_edge_button_output(output_key, run_window)
        status = result.get("status") if isinstance(result, dict) else "unknown"
        if status == "failed":
            logger.error(f"[CONTEXT EDGE] Failed {run_window} generation for {output_key}")
        else:
            logger.info(
                f"[CONTEXT EDGE] Completed {run_window} generation for {output_key} "
                f"with status={status}"
            )
        return result
    except Exception as e:
        logger.error(f"[CONTEXT EDGE] Failed {run_window} generation for {output_key}: {e}")
        raise


def _register_context_edge_output_jobs():
    if not _context_edge_scheduler_enabled():
        logger.info("[CONTEXT EDGE] Scheduler disabled; set CONTEXT_EDGE_SCHEDULER_ENABLED=true to enable")
        return

    windows = [
        ("morning", 9),
        ("afternoon", 15),
    ]
    for run_window, hour in windows:
        for index, output_key in enumerate(CONTEXT_EDGE_OUTPUT_KEYS):
            minute = index * 3
            job_id = f"context_edge_{run_window}_{output_key}"
            scheduler.add_job(
                func=_run_context_edge_button_output_job,
                trigger="cron",
                hour=hour,
                minute=minute,
                timezone="America/Phoenix",
                id=job_id,
                name=f"Context Edge {run_window} {output_key}",
                args=[output_key, run_window],
                replace_existing=True,
                misfire_grace_time=1800,
                coalesce=True,
                max_instances=1,
            )
            logger.info(
                f"[CONTEXT EDGE] Scheduled {run_window} generation for {output_key} "
                f"at {hour:02d}:{minute:02d} America/Phoenix"
            )

def background_initializer():
    """
    Registers all scheduled jobs and primes caches on startup.
    Runs in a background thread so it does not block server startup.
    Re-registers every job on every startup — APScheduler does not
    persist jobs across process restarts on Render.
    """
    global app_initialized
    import time
    time.sleep(5)  # Wait for server to fully boot

    try:
        logger.info("🚀 Starting background init...")

        # ── START SCHEDULER ──────────────────────────────────────
        if not scheduler.running:
            scheduler.start()
            logger.info("✅ Scheduler started")
        else:
            logger.info("⚠️ Scheduler already running")

        # Mora Assists daily picks email removed per request.

        # ── REGISTER MLB PROPS JOB ───────────────────────────────
        try:
            scheduler.add_job(
                func=_fetch_and_process_mlb_props,
                trigger='cron',
                hour='7,15',
                minute=0,
                timezone='America/Phoenix',
                id='mlb_props_twice_daily',
                name='MLB Props 7AM + 3PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ MLB props job registered")
        except Exception as e:
            logger.warning(f"MLB props job error: {e}")

        # ── REGISTER NHL PROPS JOB ───────────────────────────────
        try:
            scheduler.add_job(
                func=_fetch_and_process_nhl_props,
                trigger='cron',
                hour='7,15',
                minute=15,
                timezone='America/Phoenix',
                id='nhl_props_twice_daily',
                name='NHL Props 7:15AM + 3:15PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ NHL props job registered")
        except Exception as e:
            logger.warning(f"NHL props job error: {e}")

        # ── REGISTER NBA PROPS JOB ───────────────────────────────
        try:
            scheduler.add_job(
                func=_fetch_and_process_nba_props,
                trigger='cron',
                hour='7,15',
                minute=30,
                timezone='America/Phoenix',
                id='nba_props_twice_daily',
                name='NBA Props 7:30AM + 3:30PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ NBA props job registered")
        except Exception as e:
            logger.warning(f"NBA props job error: {e}")

        # ── REGISTER NFL PROPS JOB ───────────────────────────────
        try:
            scheduler.add_job(
                func=_fetch_and_process_nfl_props,
                trigger='cron',
                hour='7,15',
                minute=40,
                timezone='America/Phoenix',
                id='nfl_props_twice_daily',
                name='NFL Props 7:40AM + 3:40PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ NFL props job registered")
        except Exception as e:
            logger.warning(f"NFL props job error: {e}")

        # ── REGISTER SOCCER PROPS JOB ────────────────────────────
        try:
            scheduler.add_job(
                func=fetch_soccer_props_safe,
                trigger='cron',
                hour='7,15',
                minute=45,
                timezone='America/Phoenix',
                id='soccer_props_twice_daily',
                name='Soccer Props 7:45AM + 3:45PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ Soccer props job registered")
        except Exception as e:
            logger.warning(f"Soccer props job error: {e}")

        # ── REGISTER BOARD EMAIL JOB ──────────────────────────────
        try:
            scheduler.add_job(
                func=send_board_email,
                trigger='cron',
                hour=7,
                minute=25,
                timezone='America/Phoenix',
                id='board_email_daily',
                name='Board Email 7:25AM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info('✅ Board email job registered')
        except Exception as e:
            logger.warning(f"Board email job error: {e}")

        # ── REGISTER GAME ODDS REFRESH ───────────────────────────
        try:
            scheduler.add_job(
                func=_fetch_and_cache_game_lines,
                trigger='cron',
                hour='7,15',
                minute=30,
                timezone='America/Phoenix',
                id='game_lines_twice_daily',
                name='All Game Lines 7:30AM + 3:30PM PHX',
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True
            )
            logger.info("✅ Game odds job registered")
        except Exception as e:
            logger.warning(f"Odds job error: {e}")

        # ── REGISTER CONTEXT EDGE CACHED OUTPUTS ───────────────────
        try:
            _register_context_edge_output_jobs()
        except Exception as e:
            logger.warning(f"Context Edge scheduler job error: {e}")

        # ── LOG ALL REGISTERED JOBS ──────────────────────────────
        all_jobs = scheduler.get_jobs()
        logger.info(f"📋 Total scheduled jobs: {len(all_jobs)}")
        for j in all_jobs:
            logger.info(f"  {j.id} → {j.next_run_time}")

        # ── PRIME CACHES (cache-age-aware) ───────────────────────
        import os as _os
        from datetime import datetime as _dt

        def _cache_is_fresh(filepath, max_hours=10):
            if not _os.path.exists(filepath):
                return False
            age = (_dt.utcnow().timestamp() - _os.path.getmtime(filepath)) / 3600
            return age < max_hours

        logger.info("🔄 Priming caches (age-aware)...")

        # MLB props — skip if fetched within 10 hours
        mlb_cache = '/var/data/mlb_props_cache.json'
        if _cache_is_fresh(mlb_cache, max_hours=10):
            logger.info("[CACHE] MLB props fresh — skipping fetch")
        else:
            try:
                _fetch_and_process_mlb_props()
                logger.info("✅ MLB props primed")
            except Exception as e:
                logger.warning(f"MLB props failed: {e}")

        # NHL props — skip if fetched within 10 hours
        nhl_cache = '/var/data/nhl_props_cache.json'
        if _cache_is_fresh(nhl_cache, max_hours=10):
            logger.info("[CACHE] NHL props fresh — skipping fetch")
        else:
            try:
                _fetch_and_process_nhl_props()
                logger.info("✅ NHL props primed")
            except Exception as e:
                logger.warning(f"NHL props failed: {e}")

        # NFL props — skip if fetched within 10 hours
        nfl_cache = '/var/data/nfl_props_cache.json'
        if _cache_is_fresh(nfl_cache, max_hours=10):
            logger.info("[CACHE] NFL props fresh — skipping fetch")
        else:
            try:
                _fetch_and_process_nfl_props()
                logger.info("✅ NFL props primed")
            except Exception as e:
                logger.warning(f"NFL props failed: {e}")

        # Soccer props — skip if fetched within 10 hours
        soccer_cache = '/var/data/soccer_props_cache.json'
        if _cache_is_fresh(soccer_cache, max_hours=10):
            logger.info("[CACHE] Soccer props fresh — skipping fetch")
        else:
            try:
                _fetch_and_process_soccer_props()
                logger.info("✅ Soccer props primed")
            except Exception as e:
                logger.warning(f"Soccer props failed: {e}")

        # Odds — skip if fetched within 6 hours
        odds_flag = '/var/data/odds_last_fetch.txt'
        odds_fresh = False
        if _os.path.exists(odds_flag):
            try:
                with open(odds_flag) as _f:
                    _last = float(_f.read().strip())
                if (_dt.utcnow().timestamp() - _last) < 21600:
                    odds_fresh = True
                    logger.info("[CACHE] Odds fresh — skipping")
            except Exception:
                pass
        if not odds_fresh:
            try:
                _fetch_and_cache_game_lines()
                _os.makedirs('/var/data', exist_ok=True)
                with open(odds_flag, 'w') as _f:
                    _f.write(str(_dt.utcnow().timestamp()))
                logger.info("✅ Odds primed")
            except Exception as e:
                logger.warning(f"Odds failed: {e}")

        # Mora Assists misfire recovery removed per request.

        app_initialized = True
        logger.info("🎉 Background init complete")

    except Exception as e:
        logger.error(f"Background init failed: {e}", exc_info=True)
        app_initialized = True



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

@app.route("/api/refresh-props", methods=["POST"])
def refresh_props():
    """
    Manual board refresh — called by the dashboard refresh button.
    Rate-limited to twice daily after a usable cache exists to protect API quota.
    Runs the fetch in a background thread so the response is instant.
    """
    import time as _time

    cache_files = [
        "/var/data/mlb_props_cache.json",
        "/var/data/nhl_props_cache.json",
        "/var/data/nba_props_cache.json",
        "/var/data/nfl_props_cache.json",
        "/var/data/soccer_props_cache.json",
        "/var/data/mlb_odds_cache.json",
        "/var/data/nhl_odds_cache.json",
        "/var/data/nba_odds_cache.json",
        "/var/data/soccer_lines_cache.json",
    ]
    has_any_cache = any(_existing_cache_path(filename) for filename in cache_files)
    last = memory_cache.get("last_manual_refresh_ts")
    if has_any_cache and last and (_time.time() - last) < 43200:
        return jsonify({
            "status": "rate_limited",
            "message": "Board already refreshed in this 12-hour window. Using cached data."
        }), 429

    try:
        from threading import Thread as _Thread

        def _refresh_all():
            jobs = [
                ("game lines", _fetch_and_cache_game_lines),
                ("MLB props", _fetch_and_process_mlb_props),
                ("NHL props", _fetch_and_process_nhl_props),
                ("NBA props", _fetch_and_process_nba_props),
                ("NFL props", _fetch_and_process_nfl_props),
                ("Soccer props", _fetch_and_process_soccer_props),
            ]
            for label, job in jobs:
                cache_key = f"manual_{label.replace(' ', '_')}"
                try:
                    logger.info(f"[REFRESH] Starting {label}")
                    _record_cache_job(cache_key, "started")
                    result = job()
                    result_count = len(result) if isinstance(result, list) else None
                    _record_cache_job(cache_key, "completed", f"result_count={result_count}")
                    logger.info(f"[REFRESH] Completed {label}")
                except Exception as e:
                    _record_cache_job(cache_key, "failed", str(e))
                    logger.warning(f"[REFRESH] {label} error: {e}", exc_info=True)
            memory_cache["last_manual_refresh_ts"] = _time.time()

        _Thread(target=_refresh_all, daemon=True).start()

        return jsonify({
            "status": "refreshing",
            "message": "Board cache updating in background. Reload in 30 seconds."
        })

    except Exception as e:
        logger.error(f"[REFRESH] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/cache-check")
def cache_check():
    """Quick check of the file caches that power the dashboard."""

    def file_info(filename):
        existing = _existing_cache_path(filename)
        if not existing:
            return {"exists": False, "count": 0, "age_minutes": None, "sample": None}
        age = (time.time() - os.path.getmtime(existing)) / 60
        try:
            with open(existing, "r") as f:
                payload = json.load(f)
        except Exception as e:
            return {
                "exists": True,
                "count": 0,
                "age_minutes": round(age, 1),
                "sample": None,
                "error": str(e),
            }

        if isinstance(payload, dict):
            rows = payload.get("props") or payload.get("picks") or payload.get("no_vig_picks") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []

        return {
            "exists": True,
            "count": len(rows),
            "age_minutes": round(age, 1),
            "sample": rows[0] if rows else None,
            "path": existing,
        }

    return jsonify({
        "props": {
            "mlb": file_info("/var/data/mlb_props_cache.json"),
            "nhl": file_info("/var/data/nhl_props_cache.json"),
            "nba": file_info("/var/data/nba_props_cache.json"),
            "nfl": file_info("/var/data/nfl_props_cache.json"),
            "soccer": file_info("/var/data/soccer_props_cache.json"),
        },
        "lines": {
            "mlb": file_info("/var/data/mlb_odds_cache.json"),
            "nhl": file_info("/var/data/nhl_odds_cache.json"),
            "nba": file_info("/var/data/nba_odds_cache.json"),
            "soccer": file_info("/var/data/soccer_lines_cache.json"),
        },
        "warmups": _cache_job_status,
        "timestamp": datetime.utcnow().isoformat(),
    })


@app.route("/api/debug/props-test")
def debug_props_test():
    """
    One-shot diagnostic endpoint for MLB player props.
    Costs 1 API credit per event batch.
    Remove after confirming props pipeline is working.
    """
    try:
        from odds_api import fetch_player_props, group_props_by_player

        raw = fetch_player_props()

        stat_counts = {}
        book_counts = {}
        player_samples = []
        for p in raw:
            stat_counts[p.get("stat", "?")] = stat_counts.get(p.get("stat", "?"), 0) + 1
            book_counts[p.get("bookmaker", "?")] = book_counts.get(p.get("bookmaker", "?"), 0) + 1
            if len(player_samples) < 3:
                player_samples.append({
                    k: p[k] for k in
                    ["player", "stat", "line", "over_price", "under_price",
                     "bookmaker", "matchup", "game_time"]
                    if k in p
                })

        grouped = group_props_by_player(raw)

        tier_counts = {}
        for g in grouped:
            t = g.get("confidence_tier", "?")
            tier_counts[t] = tier_counts.get(t, 0) + 1

        return jsonify({
            "raw_count":   len(raw),
            "grouped_count": len(grouped),
            "stat_counts": stat_counts,
            "book_counts": book_counts,
            "tier_counts": tier_counts,
            "player_samples": player_samples,
            "grouped_samples": grouped[:3]
        })
    except Exception as e:
        logger.exception("[DEBUG] /api/debug/props-test error")
        return jsonify({"error": str(e)}), 500


# Start background initialization in a separate thread
from threading import Thread
init_thread = Thread(target=background_initializer, daemon=True)
init_thread.start()

# ── Email Gate ──────────────────────────────────────────────────────────────

EMAIL_LIST_FILE = '/var/data/email_subscribers.csv'


def _load_emails():
    """Load all emails from CSV file. Returns a set of lowercase emails."""
    emails = set()
    try:
        if os.path.exists(EMAIL_LIST_FILE):
            with open(EMAIL_LIST_FILE, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('email'):
                        emails.add(row['email'].lower())
    except Exception as e:
        logger.error(f"[GATE] Load emails error: {e}")
    return emails


def _save_email(email: str, name: str = '', source: str = 'mora_bets_gate') -> bool:
    """Append a new email row to the CSV file. Returns True on success."""
    try:
        os.makedirs(os.path.dirname(EMAIL_LIST_FILE), exist_ok=True)
        file_exists = os.path.exists(EMAIL_LIST_FILE)

        fieldnames = ['email', 'name', 'source', 'signed_up_at']
        if file_exists:
            try:
                with open(EMAIL_LIST_FILE, 'r', newline='') as existing_file:
                    reader = csv.DictReader(existing_file)
                    existing_rows = list(reader)
                    existing_fields = reader.fieldnames or []

                if existing_fields and 'source' not in existing_fields:
                    with open(EMAIL_LIST_FILE, 'w', newline='') as rewrite_file:
                        writer = csv.DictWriter(rewrite_file, fieldnames=fieldnames)
                        writer.writeheader()
                        for row in existing_rows:
                            writer.writerow({
                                'email':        row.get('email', ''),
                                'name':         row.get('name', ''),
                                'source':       row.get('source', 'unknown'),
                                'signed_up_at': row.get('signed_up_at', '')
                            })
            except Exception as migrate_err:
                logger.warning(f"[GATE] CSV source-column migration failed: {migrate_err}")

        with open(EMAIL_LIST_FILE, 'a', newline='') as f:
            writer = csv.DictWriter(
                f, fieldnames=fieldnames
            )
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                'email':        email,
                'name':         name,
                'source':       source,
                'signed_up_at': datetime.utcnow().isoformat()
            })

        # ── BACKUP WRITE ──────────────────────────────────────────
        backup = '/var/data/emails_backup.txt'
        try:
            with open(backup, 'a') as b:
                b.write(
                    f"{datetime.utcnow().isoformat()}|{email}|{name}|{source}\n"
                )
        except Exception as backup_err:
            logger.warning(f"[GATE] Backup write failed: {backup_err}")
        # ── END BACKUP ────────────────────────────────────────────

        logger.info(f"[GATE] Saved: {email} → {EMAIL_LIST_FILE}")
        return True

    except Exception as e:
        logger.error(f"[GATE] Save failed: {e}")
        logger.error(f"[GATE] LOST EMAIL: {email}")
        return False


# REQUIRED ENVIRONMENT VARIABLES:
# ZAPIER_WEBHOOK_URL — from zapier.com
#   Webhooks by Zapier → Catch Hook
#   Maps to: Google Sheets + Mailchimp
#   Every signup fires this immediately
#   This is the PRIMARY email storage
#   CSV file is backup only

@app.route('/api/gate-signup', methods=['POST'])
def gate_signup():
    """
    Save email from gate form. Fires Zapier webhook first (primary storage),
    then saves to local CSV (backup). Always returns success — never blocks user.
    """
    try:
        import requests as http_requests
        from meta_pixel import track_lead, get_event_id

        data  = request.json or {}
        email = data.get('email', '').strip().lower()
        name  = data.get('name',  '').strip()
        source = str(data.get('source', 'mora_bets_gate')).strip() or 'mora_bets_gate'

        if not email or '@' not in email:
            return jsonify({"success": False, "error": "Invalid email"}), 400

        # ── ZAPIER WEBHOOK ────────────────────────────────────────────────────
        # Fires FIRST — before duplicate check and before CSV write.
        # Email is safe in external storage immediately, even if CSV fails.
        zapier_url = os.environ.get('ZAPIER_WEBHOOK_URL')
        if zapier_url:
            try:
                hook_resp = http_requests.post(
                    zapier_url,
                    json={
                        "email":     email,
                        "name":      name,
                        "source":    source,
                        "signed_up": datetime.utcnow().isoformat(),
                        "url":       "fadethebooks.com"
                    },
                    timeout=5
                )
                logger.info(
                    f"[ZAPIER] ✅ Fired for {email} — status {hook_resp.status_code}"
                )
            except Exception as ze:
                logger.error(f"[ZAPIER] ❌ Failed for {email}: {ze}")
        else:
            logger.warning("[ZAPIER] No webhook URL set — skipping")
        # ── END ZAPIER ────────────────────────────────────────────────────────

        existing = _load_emails()
        if email in existing:
            logger.info(f"[GATE] Returning user: {email}")
            return jsonify({"success": True, "existing": True})

        saved = _save_email(email, name, source)
        count = len(_load_emails())
        if saved:
            logger.info(f"[GATE] ✅ Email saved: {email} to {EMAIL_LIST_FILE} (total: {count})")
        else:
            logger.error(f"[GATE] ❌ Write failed for: {email} — check disk mount at /var/data")

        # Generate shared event_id for browser + server deduplication
        event_id = get_event_id()

        # Fire server-side Lead event to Meta Conversions API
        # Fire-and-forget — never block signup on pixel failure
        try:
            track_lead(request, customer_email=email, event_id=event_id)
            logger.info(f"[META] Lead fired for {email}")
        except Exception as meta_err:
            logger.warning(f"[META] Lead event failed: {meta_err}")

        return jsonify({"success": True, "count": count, "event_id": event_id})

    except Exception as e:
        logger.error(f"[GATE] Signup error: {e}")
        return jsonify({"success": True})


@app.route('/api/subscriber-count')
def subscriber_count():
    """Return current subscriber count."""
    try:
        count = len(_load_emails())
        return jsonify({"count": count, "spots_remaining": max(0, 10000 - count)})
    except Exception:
        return jsonify({"count": 0})


@app.route('/api/subscribers/export')
def export_subscribers():
    """Export the full email list as a CSV download."""
    try:
        from flask import send_file
        if not os.path.exists(EMAIL_LIST_FILE):
            return jsonify({"error": "No list yet"})
        return send_file(
            EMAIL_LIST_FILE,
            mimetype='text/csv',
            as_attachment=True,
            download_name='mora_bets_emails.csv'
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/emails')
def admin_emails():
    """Quick admin view of email signups."""
    try:
        rows = []
        if os.path.exists(EMAIL_LIST_FILE):
            with open(EMAIL_LIST_FILE, 'r') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        total  = len(rows)
        recent = rows[-10:][::-1]

        row_html = ''
        for r in recent:
            row_html += (
                '<tr>'
                '<td>' + r.get('email', '') + '</td>'
                '<td>' + r.get('name', '—') + '</td>'
                '<td>' + r.get('signed_up_at', '')[:10] + '</td>'
                '</tr>'
            )

        html = (
            '<html><head><title>Fade the Books Emails</title><style>'
            'body{font-family:Inter,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;background:#f5faf2;}'
            'h1{color:#0f2406;}'
            '.count{font-size:48px;font-weight:900;color:#4cbb17;}'
            'table{width:100%;border-collapse:collapse;background:white;border-radius:12px;overflow:hidden;margin-top:20px;}'
            'th{background:#0f2406;color:white;padding:12px;text-align:left;font-size:12px;text-transform:uppercase;}'
            'td{padding:10px 12px;border-bottom:1px solid #e8f5e1;font-size:13px;}'
            '.export{display:inline-block;background:#4cbb17;color:white;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:bold;margin-top:16px;}'
            '</style></head><body>'
            '<h1>Fade the Books Email List</h1>'
            '<div class="count">' + str(total) + '</div>'
            '<p>total signups &middot; ' + str(10000 - total) + ' spots remaining</p>'
            '<a href="/api/subscribers/export" class="export">Export CSV</a>'
            '<table><tr><th>Email</th><th>Name</th><th>Signed Up</th></tr>'
            + row_html +
            '</table>'
            '<p style="color:#6b9e5a;font-size:12px;margin-top:16px;">Showing 10 most recent signups</p>'
            '</body></html>'
        )
        return html

    except Exception as e:
        return f"Error: {e}", 500


# Flask app startup
# SEO MANUAL STEPS AFTER DEPLOY:
#
# 1. Google Search Console
#    Go to: search.google.com/search-console
#    Add property: morabets.com
#    Verify via HTML file OR DNS TXT record
#    Submit sitemap: https://morabets.com/sitemap.xml
#
# 2. Google will crawl the site within
#    1-4 weeks of sitemap submission
#
# 3. Bing Webmaster Tools (second largest engine)
#    Go to: bing.com/webmasters
#    Import from Google Search Console
#    One click import, catches Bing + DuckDuckGo
#
# 4. Monitor rankings after 4-6 weeks at:
#    search.google.com/search-console
#    Look for: Impressions, Clicks,
#    Average Position by query
#
# 5. First keywords likely to rank:
#    "mora bets" (brand — fast)
#    "no vig betting tool" (medium — 4-8 weeks)
#    "MLB picks today free" (competitive — 3-6 months)

# ══════════════════════════════════════════════════════════════
# MORA ASSISTS — Stripe webhook, subscriber management, email
# ══════════════════════════════════════════════════════════════

import stripe as stripe_lib
from mora_assists import run_daily_assists


def _context_edge_stripe_required_env():
    required = {
        "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY", ""),
        "STRIPE_CONTEXT_EDGE_INTRO_PRICE_ID": os.environ.get("STRIPE_CONTEXT_EDGE_INTRO_PRICE_ID", ""),
        "STRIPE_CONTEXT_EDGE_PRICE_ID": os.environ.get("STRIPE_CONTEXT_EDGE_PRICE_ID", ""),
        "CONTEXT_EDGE_CANCEL_URL": os.environ.get("CONTEXT_EDGE_CANCEL_URL", ""),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required Stripe env vars: {', '.join(missing)}")
    return required


def _stripe_ts_to_iso(value):
    if not value:
        return None
    try:
        return datetime.utcfromtimestamp(int(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _stripe_object_get(obj, key, default=None):
    if not obj:
        return default
    try:
        return obj.get(key, default)
    except AttributeError:
        return getattr(obj, key, default)


def _context_edge_get_customer(customer_id):
    if not customer_id:
        return None
    try:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        return stripe_lib.Customer.retrieve(customer_id)
    except Exception as e:
        logger.warning(f"[CONTEXT EDGE STRIPE] Could not retrieve customer {customer_id}: {e}")
        return None


def _context_edge_customer_email(customer_id, fallback_email=""):
    email = (fallback_email or "").strip().lower()
    if email:
        return email

    customer = _context_edge_get_customer(customer_id)
    return (_stripe_object_get(customer, "email", "") or "").strip().lower()


def _context_edge_subscription_price_id(subscription):
    items = _stripe_object_get(subscription, "items", {}) or {}
    item_data = _stripe_object_get(items, "data", []) or []
    if not item_data:
        return os.environ.get("STRIPE_CONTEXT_EDGE_PRICE_ID", "")

    price = _stripe_object_get(item_data[0], "price", {}) or {}
    return _stripe_object_get(price, "id", "") or os.environ.get("STRIPE_CONTEXT_EDGE_PRICE_ID", "")


def _stripe_checkout_email(checkout_session):
    return (
        _stripe_object_get(checkout_session, "customer_email", "")
        or _stripe_object_get(_stripe_object_get(checkout_session, "customer_details", {}), "email", "")
        or _stripe_object_get(_stripe_object_get(checkout_session, "metadata", {}), "email", "")
        or ""
    ).strip().lower()


DASHBOARD_STRIPE_PRODUCT_KEYS = {"dashboard", "daily_dashboard", "fade_the_books_dashboard"}


def _stripe_metadata_product(obj):
    return (_stripe_object_get(_stripe_object_get(obj, "metadata", {}), "product", "") or "").strip().lower()


def _is_dashboard_checkout_session(checkout_session):
    product = _stripe_metadata_product(checkout_session)
    if product in DASHBOARD_STRIPE_PRODUCT_KEYS:
        return True

    configured_payment_link = os.environ.get("STRIPE_DASHBOARD_PAYMENT_LINK_ID", "").strip()
    payment_link = (_stripe_object_get(checkout_session, "payment_link", "") or "").strip()
    return bool(configured_payment_link and payment_link == configured_payment_link)


def _is_dashboard_subscription(subscription):
    return _stripe_metadata_product(subscription) in DASHBOARD_STRIPE_PRODUCT_KEYS


def _sync_dashboard_subscription_from_stripe_subscription(subscription, fallback_email=""):
    from supabase_backend import upsert_profile_by_email, upsert_subscription

    customer_id = _stripe_object_get(subscription, "customer", "")
    email = _context_edge_customer_email(customer_id, fallback_email)
    if not email or "@" not in email:
        raise ValueError("Stripe customer email is required for dashboard subscription sync")

    profile = upsert_profile_by_email(email, stripe_customer_id=customer_id)
    return upsert_subscription(
        profile=profile,
        stripe_subscription_id=_stripe_object_get(subscription, "id", ""),
        stripe_price_id=_context_edge_subscription_price_id(subscription),
        stripe_customer_id=customer_id,
        plan="dashboard",
        status=_stripe_object_get(subscription, "status", "trialing"),
        current_period_start=_stripe_ts_to_iso(_stripe_object_get(subscription, "current_period_start")),
        current_period_end=_stripe_ts_to_iso(_stripe_object_get(subscription, "current_period_end")),
        trial_ends_at=_stripe_ts_to_iso(_stripe_object_get(subscription, "trial_end")),
        cancelled_at=_stripe_ts_to_iso(
            _stripe_object_get(subscription, "canceled_at")
            or _stripe_object_get(subscription, "ended_at")
        ),
    )


def _sync_dashboard_subscription_from_checkout_session(checkout_session):
    subscription_id = _stripe_object_get(checkout_session, "subscription", "")
    fallback_email = _stripe_checkout_email(checkout_session)
    if subscription_id:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        subscription = stripe_lib.Subscription.retrieve(subscription_id)
        return _sync_dashboard_subscription_from_stripe_subscription(
            subscription,
            fallback_email=fallback_email,
        )

    from supabase_backend import upsert_subscription_by_email

    return upsert_subscription_by_email(
        fallback_email,
        stripe_customer_id=_stripe_object_get(checkout_session, "customer", ""),
        plan="dashboard",
        status="active",
    )


def _handle_dashboard_checkout_completed(checkout_session):
    result = _sync_dashboard_subscription_from_checkout_session(checkout_session)
    session_id = _stripe_object_get(checkout_session, "id", "")
    email = _stripe_checkout_email(checkout_session)

    try:
        from meta_pixel import track_start_trial

        track_start_trial(
            customer_email=email,
            event_id=session_id,
            value=0,
            currency="USD",
            content_name="Fade the Books 3-Day Trial",
        )
    except Exception as e:
        logger.warning(f"[META CAPI] Dashboard StartTrial tracking failed: {e}")

    logger.info(f"[STRIPE] Dashboard checkout completed: {session_id}")
    return result


def _is_dashboard_invoice(invoice):
    if _stripe_metadata_product(invoice) in DASHBOARD_STRIPE_PRODUCT_KEYS:
        return True

    subscription_id = _stripe_object_get(invoice, "subscription", "")
    if not subscription_id:
        return False

    try:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        subscription = stripe_lib.Subscription.retrieve(subscription_id)
        return _is_dashboard_subscription(subscription)
    except Exception as e:
        logger.warning(f"[STRIPE] Could not verify dashboard invoice subscription {subscription_id}: {e}")
        return False


def _handle_dashboard_paid_invoice(invoice):
    amount_paid = _stripe_object_get(invoice, "amount_paid", 0) or 0
    if amount_paid <= 0:
        logger.info(f"[STRIPE] Dashboard invoice has no paid amount: {_stripe_object_get(invoice, 'id', '')}")
        return None

    email = (
        _stripe_object_get(invoice, "customer_email", "")
        or _context_edge_customer_email(_stripe_object_get(invoice, "customer", ""))
    )

    try:
        from meta_pixel import track_purchase

        track_purchase(
            customer_email=email,
            event_id=_stripe_object_get(invoice, "id", ""),
            value=round(amount_paid / 100, 2),
            currency=(_stripe_object_get(invoice, "currency", "usd") or "usd").upper(),
            content_name="Fade the Books Monthly Subscription",
        )
    except Exception as e:
        logger.warning(f"[META CAPI] Dashboard paid Purchase tracking failed: {e}")

    logger.info(f"[STRIPE] Dashboard paid invoice tracked: {_stripe_object_get(invoice, 'id', '')}")
    return None


def _upsert_context_edge_subscription_from_stripe(subscription, fallback_email=""):
    from supabase_backend import upsert_profile_by_email, upsert_subscription

    customer_id = _stripe_object_get(subscription, "customer", "")
    email = _context_edge_customer_email(customer_id, fallback_email)
    if not email or "@" not in email:
        raise ValueError("Stripe customer email is required for Context Edge subscription sync")

    profile = upsert_profile_by_email(
        email,
        stripe_customer_id=customer_id,
    )

    status = _stripe_object_get(subscription, "status", "inactive")
    cancelled_at = _stripe_ts_to_iso(
        _stripe_object_get(subscription, "canceled_at")
        or _stripe_object_get(subscription, "ended_at")
    )

    return upsert_subscription(
        profile=profile,
        stripe_subscription_id=_stripe_object_get(subscription, "id", ""),
        stripe_price_id=_context_edge_subscription_price_id(subscription),
        stripe_customer_id=customer_id,
        plan="monthly",
        status=status,
        current_period_start=_stripe_ts_to_iso(_stripe_object_get(subscription, "current_period_start")),
        current_period_end=_stripe_ts_to_iso(_stripe_object_get(subscription, "current_period_end")),
        trial_ends_at=_stripe_ts_to_iso(_stripe_object_get(subscription, "trial_end")),
        cancelled_at=cancelled_at,
    )


def _handle_context_edge_stripe_event(event):
    event_type = event.get("type")
    data_obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        if _is_dashboard_checkout_session(data_obj):
            return _handle_dashboard_checkout_completed(data_obj)

        customer_id = _stripe_object_get(data_obj, "customer", "")
        fallback_email = (
            _stripe_object_get(data_obj, "customer_email", "")
            or _stripe_object_get(_stripe_object_get(data_obj, "customer_details", {}), "email", "")
        )
        subscription_id = _stripe_object_get(data_obj, "subscription", "")

        if subscription_id:
            stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            subscription = stripe_lib.Subscription.retrieve(subscription_id)
            result = _upsert_context_edge_subscription_from_stripe(
                subscription,
                fallback_email=fallback_email,
            )
        else:
            from supabase_backend import upsert_profile_by_email, upsert_subscription

            email = _context_edge_customer_email(customer_id, fallback_email)
            profile = upsert_profile_by_email(email, stripe_customer_id=customer_id)
            result = upsert_subscription(
                profile=profile,
                stripe_customer_id=customer_id,
                stripe_price_id=os.environ.get("STRIPE_CONTEXT_EDGE_PRICE_ID", ""),
                plan="monthly",
                status="active",
            )

        try:
            from meta_pixel import track_purchase

            session_id = _stripe_object_get(data_obj, "id", "")
            purchase_email = (
                fallback_email
                or _context_edge_customer_email(customer_id)
            )
            track_purchase(
                customer_email=purchase_email,
                event_id=session_id,
                value=7,
                currency="USD",
                content_name="Context Edge $7 Trial",
            )
        except Exception as e:
            logger.warning(f"[META CAPI] Context Edge Purchase tracking failed: {e}")

        logger.info(f"[CONTEXT EDGE STRIPE] Checkout completed: {subscription_id}")
        return result

    if event_type == "invoice.payment_succeeded":
        if _is_dashboard_invoice(data_obj):
            return _handle_dashboard_paid_invoice(data_obj)
        logger.info(f"[STRIPE] Ignored non-dashboard paid invoice: {_stripe_object_get(data_obj, 'id', '')}")
        return None

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        if _is_dashboard_subscription(data_obj):
            result = _sync_dashboard_subscription_from_stripe_subscription(data_obj)
            logger.info(
                "[STRIPE] Dashboard subscription synced: "
                f"{_stripe_object_get(data_obj, 'id', '')} ({_stripe_object_get(data_obj, 'status', '')})"
            )
            return result

        result = _upsert_context_edge_subscription_from_stripe(data_obj)
        logger.info(
            "[CONTEXT EDGE STRIPE] Subscription synced: "
            f"{_stripe_object_get(data_obj, 'id', '')} ({_stripe_object_get(data_obj, 'status', '')})"
        )
        return result

    logger.info(f"[CONTEXT EDGE STRIPE] Ignored event: {event_type}")
    return None


@app.route('/api/context-edge/create-checkout-session', methods=['POST'])
def create_context_edge_checkout_session():
    try:
        env = _context_edge_stripe_required_env()
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        if email and "@" not in email:
            return jsonify({"error": "A valid email is required"}), 400

        stripe_lib.api_key = env["STRIPE_SECRET_KEY"]
        session_args = {
            "mode": "subscription",
            "line_items": [
                {
                    "price": env["STRIPE_CONTEXT_EDGE_INTRO_PRICE_ID"],
                    "quantity": 1,
                },
                {
                    "price": env["STRIPE_CONTEXT_EDGE_PRICE_ID"],
                    "quantity": 1,
                }
            ],
            "success_url": url_for("context_edge_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": env["CONTEXT_EDGE_CANCEL_URL"],
            "allow_promotion_codes": True,
            "metadata": {
                "product": "context_edge",
            },
            "subscription_data": {
                "trial_period_days": 7,
                "metadata": {
                    "product": "context_edge",
                }
            },
        }
        if email:
            session_args["customer_email"] = email
            session_args["client_reference_id"] = email
            session_args["metadata"]["email"] = email
            session_args["subscription_data"]["metadata"]["email"] = email

        session_obj = stripe_lib.checkout.Session.create(**session_args)
        return jsonify({
            "checkout_url": session_obj.url,
            "session_id": session_obj.id,
        })
    except Exception as e:
        logger.error(f"[CONTEXT EDGE STRIPE] Checkout session error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/stripe/context-edge/webhook', methods=['POST'])
def context_edge_stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        logger.error("[CONTEXT EDGE STRIPE] STRIPE_WEBHOOK_SECRET is not configured")
        return jsonify({"error": "Webhook secret is not configured"}), 500

    try:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        event = stripe_lib.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe_lib.error.SignatureVerificationError as e:
        logger.warning(f"[CONTEXT EDGE STRIPE] Signature verification failed: {e}")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        logger.error(f"[CONTEXT EDGE STRIPE] Webhook parse error: {e}")
        return jsonify({"error": str(e)}), 400

    try:
        _handle_context_edge_stripe_event(event)
    except Exception as e:
        logger.error(f"[CONTEXT EDGE STRIPE] Event handler error: {e}")
        return jsonify({"error": str(e)}), 400

    return jsonify({"status": "ok"}), 200


def _save_subscriber(data):
    """Append or update a subscriber row in mora_assists_subscribers.csv."""
    FILE = "/var/data/mora_assists_subscribers.csv"
    fieldnames = [
        "email", "name", "stripe_customer_id",
        "stripe_subscription_id", "status",
        "subscribed_at", "trial_ends_at", "cancelled_at"
    ]
    rows = []
    updated = False

    if os.path.exists(FILE):
        with open(FILE, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("email", "").lower() == data.get("email", "").lower():
                    row.update({k: v for k, v in data.items() if v})
                    updated = True
                rows.append(row)

    if not updated:
        rows.append(data)

    with open(FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _update_subscriber_status(subscription_id, status):
    """Update the status field for a given stripe_subscription_id."""
    FILE = "/var/data/mora_assists_subscribers.csv"
    if not os.path.exists(FILE):
        return

    fieldnames = [
        "email", "name", "stripe_customer_id",
        "stripe_subscription_id", "status",
        "subscribed_at", "trial_ends_at", "cancelled_at"
    ]
    rows = []
    with open(FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("stripe_subscription_id") == subscription_id:
                row["status"] = status
                if status == "cancelled":
                    row["cancelled_at"] = datetime.utcnow().isoformat()
            rows.append(row)

    with open(FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _send_welcome_email(email, name=""):
    """Send welcome email to a new Mora Assists subscriber via SendGrid."""
    SENDGRID_KEY = os.environ.get("SENDGRID_API_KEY")
    FROM_EMAIL   = os.environ.get("EMAIL_FROM", "picks@morabets.com")

    if not SENDGRID_KEY:
        logger.warning("[EMAIL] No SENDGRID_API_KEY — skipping welcome email")
        return False

    first_name = name.split()[0] if name else "there"
    subject    = "⚡ Welcome to Mora Assists — your first picks arrive tomorrow"

    html = f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{font-family:Inter,Arial,sans-serif;background:#f5faf2;margin:0;padding:20px;color:#0f2406;}}
  .container {{max-width:560px;margin:0 auto;background:white;border-radius:16px;overflow:hidden;border:2px solid #4cbb17;}}
  .header {{background:#0f2406;padding:32px 24px;text-align:center;}}
  .logo {{color:white;font-size:32px;font-weight:900;letter-spacing:4px;}}
  .logo span {{color:#4cbb17;}}
  .tagline {{color:#6b9e5a;font-size:13px;margin-top:6px;}}
  .body {{padding:36px 32px;}}
  h1 {{font-size:22px;font-weight:900;color:#0f2406;margin:0 0 8px;}}
  p {{font-size:14px;color:#6b9e5a;line-height:1.7;margin:0 0 16px;}}
  .highlight {{color:#0f2406;font-weight:700;}}
  .what-to-expect {{background:#f5faf2;border:1px solid #e8f5e1;border-radius:12px;padding:20px;margin:20px 0;}}
  .what-to-expect h3 {{font-size:13px;font-weight:700;color:#0f2406;text-transform:uppercase;letter-spacing:1px;margin:0 0 12px;}}
  .step {{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px;}}
  .step-num {{background:#4cbb17;color:white;width:22px;height:22px;border-radius:50%;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;}}
  .step-text {{font-size:13px;color:#1a3d0a;line-height:1.5;margin:0;}}
  .trial-notice {{background:#e8f5e1;border:1px solid #4cbb17;border-radius:10px;padding:14px 18px;margin:20px 0;text-align:center;}}
  .trial-notice p {{margin:0;font-size:13px;color:#2d6e0f;font-weight:600;}}
  .cta-btn {{display:block;background:#4cbb17;color:white;text-align:center;padding:14px 28px;border-radius:50px;text-decoration:none;font-weight:700;font-size:15px;margin:24px 0 8px;}}
  .footer {{background:#f5faf2;padding:20px 32px;text-align:center;border-top:1px solid #e8f5e1;}}
  .footer p {{font-size:11px;color:#a0bf96;margin:0 0 6px;}}
  .footer a {{color:#6b9e5a;text-decoration:underline;font-size:11px;}}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="logo">MORA <span>ASSISTS</span></div>
    <div class="tagline">Daily picks. Delivered before first pitch.</div>
  </div>
  <div class="body">
    <h1>You're in, {first_name}. ⚡</h1>
    <p>Your 3-day free trial starts today. Your <span class="highlight">first picks arrive tomorrow morning</span> — 5 plays in your inbox by 9:00 AM CST.</p>
    <div style="background:#fff8e1;border:1px solid #f59e0b;border-radius:10px;padding:14px 18px;margin:16px 0;">
      <p style="margin:0 0 8px;font-size:13px;font-weight:700;color:#92400e;">⚠️ Important — do this right now:</p>
      <p style="margin:0 0 6px;font-size:13px;color:#92400e;">1. Check your spam or junk folder for this email if you don't see it in your inbox.</p>
      <p style="margin:0;font-size:13px;color:#92400e;">2. Save this email address as a contact — <strong>picks@morabets.com</strong> — so your daily picks never land in spam.</p>
    </div>
    <div class="what-to-expect">
      <h3>Here's what happens next</h3>
      <div class="step"><div class="step-num">1</div><p class="step-text"><strong>Every morning at 10 AM</strong> — our system scans every MLB and NHL line on the board</p></div>
      <div class="step"><div class="step-num">2</div><p class="step-text"><strong>AI selects 5 picks</strong> — 2 player props based on game environment, 3 anchor lines with real mathematical edge</p></div>
      <div class="step"><div class="step-num">3</div><p class="step-text"><strong>Email lands by 9:00 AM CST</strong> — open it, place the bets, done before lunch</p></div>
      <div class="step"><div class="step-num">4</div><p class="step-text"><strong>Same unit every play</strong> — flat stakes, no chasing. The math compounds across the season.</p></div>
    </div>
    <div class="trial-notice"><p>🔒 &nbsp; 3-day free trial — cancel anytime before day 3 and you pay nothing. $28.99/month after your trial ends.</p></div>
    <p>While you wait for tomorrow's picks — the full board is live right now.</p>
    <a href="https://morabets.com/dashboard" class="cta-btn">See Today's Full Board →</a>
  </div>
  <div class="footer">
    <p>You're receiving this because you subscribed to Mora Assists.</p>
    <p>Mora Bets · Free sports analytics tool</p>
    <p style="text-align:center;margin-top:16px;font-size:13px;color:#6B9E5A;">
      <a href="https://billing.stripe.com/p/login/aFa7sK6eh7MF4YZ1Oa0gw00"
         style="color:#4CBB17;text-decoration:none;font-weight:600;">
        Manage your subscription →
      </a>
    </p>
    <p style="margin-top:8px;">
      <a href="https://morabets.com/unsubscribe/assists?email={email}">Cancel subscription &amp; unsubscribe</a>
      &nbsp;·&nbsp;
      <a href="https://morabets.com/dashboard">View dashboard</a>
    </p>
    <p style="margin-top:8px;">© 2026 Mora Bets. For informational purposes only. Please bet responsibly.</p>
  </div>
</div>
</body>
</html>"""

    try:
        import sendgrid as sg_lib
        from sendgrid.helpers.mail import Mail as SgMail
        sg      = sg_lib.SendGridAPIClient(api_key=SENDGRID_KEY)
        message = SgMail(from_email=FROM_EMAIL, to_emails=email, subject=subject, html_content=html)
        resp    = sg.send(message)
        return resp.status_code in [200, 201, 202]
    except Exception as e:
        logger.error(f"[EMAIL] Welcome send failed to {email}: {e}")
        return False


def _handle_stripe_event(event):
    """Process a verified Stripe webhook event."""
    event_type = event.get("type")
    data_obj   = event.get("data", {}).get("object", {})

    if event_type == "customer.subscription.created":
        customer_id   = data_obj.get("customer")
        subscription_id = data_obj.get("id")
        status        = data_obj.get("status", "trialing")
        trial_end_ts  = data_obj.get("trial_end")
        trial_ends_at = (
            datetime.utcfromtimestamp(trial_end_ts).isoformat()
            if trial_end_ts else ""
        )

        email = ""
        name  = ""
        try:
            stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            customer   = stripe_lib.Customer.retrieve(customer_id)
            email      = customer.get("email", "")
            name       = customer.get("name", "")
        except Exception as e:
            logger.warning(f"[STRIPE] Could not retrieve customer {customer_id}: {e}")

        csv_status = "trial" if "trial" in status else "active"
        _save_subscriber({
            "email":                  email,
            "name":                   name,
            "stripe_customer_id":     customer_id,
            "stripe_subscription_id": subscription_id,
            "status":                 csv_status,
            "subscribed_at":          datetime.utcnow().isoformat(),
            "trial_ends_at":          trial_ends_at,
            "cancelled_at":           "",
        })
        logger.info(f"[STRIPE] New trial subscriber: {email}")

        try:
            _send_welcome_email(email, name)
            logger.info(f"[STRIPE] Welcome email sent to {email}")
        except Exception as e:
            logger.warning(f"[STRIPE] Welcome email failed for {email}: {e}")

    elif event_type == "customer.subscription.updated":
        subscription_id = data_obj.get("id")
        status = data_obj.get("status", "")
        csv_status = "active" if status == "active" else (
            "cancelled" if status in ["canceled", "cancelled"] else status
        )
        _update_subscriber_status(subscription_id, csv_status)
        logger.info(f"[STRIPE] Subscription updated: {subscription_id} → {csv_status}")

    elif event_type in ("customer.subscription.deleted", "customer.subscription.canceled"):
        subscription_id = data_obj.get("id")
        _update_subscriber_status(subscription_id, "cancelled")
        logger.info(f"[STRIPE] Subscription cancelled: {subscription_id}")

    elif event_type == "invoice.payment_failed":
        subscription_id = data_obj.get("subscription")
        if subscription_id:
            _update_subscriber_status(subscription_id, "past_due")
        logger.warning(f"[STRIPE] Payment failed for subscription: {subscription_id}")


def _sync_to_brevo(email, name=""):
    """Stub — extend if Brevo/Sendinblue integration is added later."""
    pass


@app.route('/zapier/new-assists-subscriber', methods=['POST'])
def zapier_new_assists_subscriber():
    """
    Called by Zapier when a new Mora Assists subscription is created in Stripe.
    Saves the subscriber and sends the welcome email via SendGrid.
    Supports plan=day_pass (one-time, expires end of day ET) and plan=trial (monthly).
    """
    from zoneinfo import ZoneInfo as _ZI

    def _get_day_pass_expiry():
        _et  = _ZI('America/New_York')
        _now = datetime.now(_et)
        _eod = _now.replace(hour=23, minute=59, second=59, microsecond=0)
        return _eod.isoformat()

    try:
        data = request.json or {}
        logger.info(f"[ZAPIER] New subscriber payload received: {data}")

        email = (
            data.get('customer_email') or
            data.get('email') or
            data.get('Customer Email') or ''
        ).strip().lower()

        name = (
            data.get('customer_name') or
            data.get('name') or
            data.get('Customer Name') or ''
        ).strip()

        stripe_customer_id = (
            data.get('customer') or
            data.get('customer_id') or
            data.get('Customer') or ''
        )

        stripe_subscription_id = (
            data.get('subscription') or
            data.get('id') or
            data.get('Subscription ID') or ''
        )

        plan = data.get('plan', 'trial').lower().strip()

        if not email or '@' not in email:
            logger.error(f"[ZAPIER] Invalid email in payload: {data}")
            return jsonify({'success': False, 'error': 'No valid email in payload'}), 400

        FILE = '/var/data/mora_assists_subscribers.csv'

        if plan == 'day_pass':
            # Day pass — always allowed, no duplicate check
            logger.info(f"[ZAPIER] Day pass purchase: {email}")
            sub_status   = 'day_pass'
            trial_ends   = _get_day_pass_expiry()
        else:
            # Monthly trial — block if already active or on trial
            if os.path.exists(FILE):
                with open(FILE) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('email', '').lower() == email:
                            if row.get('status', '') in ('active', 'trial'):
                                logger.info(f"[ZAPIER] Already active/trial: {email}")
                                return jsonify({'success': True, 'existing': True, 'email': email})
            sub_status = 'trial'
            trial_ends = (datetime.utcnow() + timedelta(days=3)).isoformat()

        # Save to subscriber CSV
        saved = False
        try:
            os.makedirs('/var/data', exist_ok=True)
            _save_subscriber({
                'email':                  email,
                'name':                   name,
                'stripe_customer_id':     stripe_customer_id,
                'stripe_subscription_id': stripe_subscription_id,
                'status':                 sub_status,
                'subscribed_at':          datetime.utcnow().isoformat(),
                'trial_ends_at':          trial_ends,
                'cancelled_at':           ''
            })
            saved = True
            logger.info(f"[ZAPIER] ✅ Subscriber saved: {email} ({sub_status})")
        except Exception as se:
            logger.error(f"[ZAPIER] ❌ CSV write failed for {email}: {se}")

        # Send welcome email — always attempt regardless of CSV result
        try:
            _send_welcome_email(email, name)
            logger.info(f"[ZAPIER] ✅ Welcome email sent: {email}")
        except Exception as we:
            logger.error(f"[ZAPIER] Welcome email failed for {email}: {we}")

        # Sync to marketing list (no-op until Brevo is wired)
        try:
            _sync_to_brevo(email, name)
        except Exception:
            pass

        return jsonify({
            'success': True,
            'email':   email,
            'name':    name,
            'plan':    plan,
            'status':  sub_status,
            'saved':   saved,
            'message': 'Subscriber processed'
        })

    except Exception as e:
        logger.error(f"[ZAPIER] Subscriber route error: {e}", exc_info=True)
        # Return 200 so Zapier does not retry endlessly
        return jsonify({'success': True, 'warning': str(e)})


@app.route('/zapier/cancel-subscriber', methods=['POST'])
def zapier_cancel_subscriber():
    """
    Called by Zapier when a Mora Assists subscription is cancelled in Stripe.
    Marks the subscriber as cancelled in the CSV.
    """
    import csv as _csv
    data   = request.json or {}
    email  = data.get('email', '').lower().strip()
    reason = data.get('reason', 'cancelled')

    if not email:
        return jsonify({'error': 'no email'}), 400

    FILE = '/var/data/mora_assists_subscribers.csv'

    if not os.path.exists(FILE):
        return jsonify({'error': 'no file'}), 500

    rows = []
    with open(FILE, 'r') as f:
        reader = _csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    updated = False
    for row in rows:
        if row.get('email', '').lower() == email:
            row['status']       = 'cancelled'
            row['cancelled_at'] = datetime.utcnow().isoformat()
            updated = True
            logger.info(f'[CANCEL] {email} cancelled — reason: {reason}')

    if updated:
        with open(FILE, 'w', newline='') as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    return jsonify({
        'success': True,
        'email':   email,
        'status':  'cancelled',
        'updated': updated
    })


@app.route('/api/scheduler-status')
def scheduler_status():
    """
    Check scheduler health and registered jobs.
    Visit: morabets.com/api/scheduler-status
    """
    try:
        from datetime import datetime as _dt
        jobs      = scheduler.get_jobs()
        mora_job  = scheduler.get_job('mora_assists_daily')
        return jsonify({
            'scheduler_running':   scheduler.running,
            'total_jobs':          len(jobs),
            'mora_assists_daily': {
                'registered':    mora_job is not None,
                'next_run':      str(mora_job.next_run_time) if mora_job else 'NOT REGISTERED',
                'misfire_grace': mora_job.misfire_grace_time if mora_job else None,
            },
            'all_jobs': [
                {'id': j.id, 'name': j.name, 'next_run': str(j.next_run_time)}
                for j in jobs
            ],
            'server_time_utc': str(_dt.utcnow()),
            'status': '✅ healthy' if mora_job else '❌ mora_assists_daily missing'
        })
    except Exception as e:
        return jsonify({'error': str(e), 'status': '❌ error'}), 500


@app.route('/api/send-picks-now', methods=['POST'])
def send_picks_now():
    """
    Manually trigger Mora Assists picks send immediately.
    Use when scheduler misses a day.
    POST to: morabets.com/api/send-picks-now
    """
    try:
        from mora_assists import run_daily_assists as _run_assists
        import threading

        def _run_in_background():
            logger.info("[MANUAL] Picks send triggered via /api/send-picks-now")
            result = _run_assists()
            logger.info(f"[MANUAL] Result: {result}")

        t = threading.Thread(target=_run_in_background, daemon=True)
        t.start()

        return jsonify({
            'status':      'triggered',
            'message':     'Picks send started in background. Check Render logs for results.',
            'check_logs':  True
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/subscribers-check')
def admin_subscribers_check():
    """Quick check of who is in the Mora Assists picks list. Verify Zapier writes."""
    try:
        FILE = '/var/data/mora_assists_subscribers.csv'
        rows = []
        if os.path.exists(FILE):
            with open(FILE) as f:
                rows = list(csv.DictReader(f))

        active = [r for r in rows if r.get('status') in ['active', 'trial']]
        return jsonify({'total': len(rows), 'active': len(active), 'subscribers': active})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Receive and verify Stripe webhook events."""
    payload       = request.get_data()
    sig_header    = request.headers.get("Stripe-Signature", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not webhook_secret:
        logger.warning("[STRIPE] No STRIPE_WEBHOOK_SECRET — processing event without verification")
        try:
            event = request.get_json()
            _handle_stripe_event(event)
            return jsonify({"status": "ok"}), 200
        except Exception as e:
            logger.error(f"[STRIPE] Webhook processing error: {e}")
            return jsonify({"error": str(e)}), 400

    try:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        event = stripe_lib.Webhook.construct_event(payload, sig_header, webhook_secret)
    except stripe_lib.error.SignatureVerificationError as e:
        logger.warning(f"[STRIPE] Signature verification failed: {e}")
        return jsonify({"error": "Invalid signature"}), 400
    except Exception as e:
        logger.error(f"[STRIPE] Webhook parse error: {e}")
        return jsonify({"error": str(e)}), 400

    try:
        _handle_stripe_event(event)
    except Exception as e:
        logger.error(f"[STRIPE] Event handler error: {e}")

    return jsonify({"status": "ok"}), 200


@app.route('/api/assists/test-send', methods=['POST'])
def test_assists_send():
    try:
        from mora_assists import (
            load_full_board,
            select_picks_with_llm,
            format_picks_email,
            send_picks_email
        )
        data  = request.json or {}
        email = data.get('email', '')

        if not email:
            return jsonify(
                {'error': 'email required'}
            ), 400

        board      = load_full_board()
        picks_data = select_picks_with_llm(board)
        subject, html = format_picks_email(picks_data)
        success = send_picks_email(
            email, subject, html
        )
        return jsonify({
            'success':     success,
            'picks_count': len(
                picks_data.get('picks', [])
                if picks_data else []
            ),
            'sports':      board.get(
                'sports_found', []
            )
        })
    except Exception as e:
        logger.error(f"[TEST] {e}")
        return jsonify({'error': str(e)}), 500


@app.route("/unsubscribe/assists")
def unsubscribe_assists():
    """One-click unsubscribe from email footer. Works without JavaScript."""
    email = request.args.get("email", "").strip()

    if not email:
        return """<html><body style="font-family:Inter;text-align:center;padding:60px;">
        <h2>Link invalid.</h2><p>Please contact us at picks@morabets.com</p>
        </body></html>""", 400

    try:
        stripe_lib.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

        sub_id = None
        if os.path.exists("/var/data/mora_assists_subscribers.csv"):
            with open("/var/data/mora_assists_subscribers.csv", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("email", "").lower() == email.lower():
                        sub_id = row.get("stripe_subscription_id", "")
                        break

        if sub_id and stripe_lib.api_key:
            try:
                stripe_lib.Subscription.cancel(sub_id)
                logger.info(f"[UNSUB] Cancelled Stripe subscription: {sub_id}")
            except Exception as e:
                logger.warning(f"[UNSUB] Stripe cancel error: {e}")

        _update_subscriber_status(sub_id, "cancelled")
        logger.info(f"[UNSUB] Unsubscribed: {email}")

        return """<html>
<head><style>
  body{font-family:Inter,sans-serif;background:#f5faf2;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
  .card{background:white;border:2px solid #4cbb17;border-radius:16px;padding:48px;text-align:center;max-width:400px;}
  h2{color:#0f2406;font-size:24px;margin-bottom:12px;}
  p{color:#6b9e5a;font-size:14px;line-height:1.6;}
  a{display:inline-block;background:#4cbb17;color:white;padding:12px 24px;border-radius:50px;text-decoration:none;font-weight:700;margin-top:20px;font-size:14px;}
</style></head>
<body><div class="card">
  <div style="font-size:48px;margin-bottom:16px;">✓</div>
  <h2>You're unsubscribed.</h2>
  <p>Your Mora Assists subscription has been cancelled. No further charges will be made.</p>
  <p style="margin-top:12px;">The free board at morabets.com is still available to you anytime.</p>
  <a href="https://morabets.com/dashboard">Back to Dashboard</a>
</div></body></html>"""

    except Exception as e:
        logger.error(f"[UNSUB] Error: {e}")
        return f"""<html><body style="font-family:Inter;text-align:center;padding:60px;">
        <h2>Something went wrong.</h2><p>Email us: picks@morabets.com</p>
        </body></html>""", 500


# In-memory per-IP throttle for the (public) Context Edge endpoint. Each request
# triggers a paid AI call, so cap bursts to limit cost-exhaustion abuse.
_CE_RATE_BUCKETS = {}
_CE_RATE_MAX = 10        # requests
_CE_RATE_WINDOW = 3600   # seconds (per hour)


def load_board_for_context():
    """Load the current no-vig board from all available sport caches.
    READ ONLY — never writes to cache files. Returns a fresh list every call."""
    caches = {
        'MLB': '/var/data/mlb_props_cache.json',
        'Soccer': '/var/data/soccer_props_cache.json',
        'NHL': '/var/data/nhl_props_cache.json',
        'NFL': '/var/data/nfl_props_cache.json',
    }
    board = []
    for sport, path in caches.items():
        if os.path.exists(path):
            try:
                with open(path) as f:
                    props = json.load(f)
                qualifying = [
                    p for p in props
                    if p.get('no_vig_prob', 0) >= 57
                    and p.get('best_over_price', 0) != 0
                    and p.get('best_over_price', 0) > -235
                    and p.get('player', '')
                    and not any(x in p.get('player', '') for x in ['Batter', 'Pitcher', '_'])
                ]
                qualifying.sort(key=lambda x: x.get('no_vig_prob', 0), reverse=True)
                board.extend(qualifying[:15])
            except Exception as e:
                logger.warning(f'Cache load error {sport}: {e}')
    return board


def format_board_for_context_ai(board):
    """Render the board as a compact one-line-per-prop string for the prompt."""
    if not board:
        return "No qualifying props on board today."
    lines = []
    for p in board:
        over = p.get('best_over_price', 0)
        nv = p.get('no_vig_prob', 0)
        books = [b.get('book', '') for b in p.get('all_books', [])]
        stat = p.get('stat_label', p.get('stat', '')).replace('player_', '').replace('_', ' ').title()
        env = p.get('environment', '')
        lines.append(
            f"[{p.get('sport','MLB')}] "
            f"{p.get('player','')} | "
            f"{stat} OVER {p.get('line','')} | "
            f"Over: {over} | "
            f"No-vig: {nv}% | "
            f"{p.get('matchup','')} | "
            f"ENV: {env or 'standard'} | "
            f"Books: {', '.join(books[:2])}"
        )
    return '\n'.join(lines)


def _context_edge_admin_authorized():
    expected = os.environ.get("ADMIN_CONTEXT_EDGE_TOKEN", "")
    provided = request.headers.get("X-Admin-Token", "")
    return bool(expected and provided and provided == expected)


def _queue_context_edge_generation(output_key, run_window, source="admin"):
    normalized_output_key = (output_key or "").strip().lower()

    def _run_generation():
        try:
            from context_edge_button_generator_service import generate_context_edge_button_output

            logger.info(
                f"[CONTEXT EDGE] {source} generation started for "
                f"{normalized_output_key} ({run_window})"
            )
            result = generate_context_edge_button_output(normalized_output_key, run_window)
            status = result.get("status") if isinstance(result, dict) else "unknown"
            logger.info(
                f"[CONTEXT EDGE] {source} generation completed for "
                f"{normalized_output_key} ({run_window}) status={status}"
            )
        except Exception as e:
            logger.error(
                f"[CONTEXT EDGE] {source} generation failed for "
                f"{normalized_output_key} ({run_window}): {e}",
                exc_info=True,
            )

    thread = threading.Thread(
        target=_run_generation,
        name=f"context-edge-{source}-{normalized_output_key}-{run_window}",
        daemon=True,
    )
    thread.start()
    logger.info(
        f"[CONTEXT EDGE] {source} generation queued for "
        f"{normalized_output_key} ({run_window})"
    )


@app.route('/admin/context-edge/run/<output_key>', methods=['POST'])
def admin_context_edge_run(output_key):
    if not _context_edge_admin_authorized():
        return jsonify({'error': 'forbidden'}), 403

    try:
        from context_edge_button_output_service import (
            get_output_config,
            get_phoenix_run_window,
        )
        from supabase_backend import normalize_context_edge_run_window

        try:
            get_output_config(output_key)
        except KeyError:
            return jsonify({'error': 'Invalid output key'}), 400

        data = request.get_json(silent=True) or {}
        try:
            run_window = normalize_context_edge_run_window(
                data.get('run_window') or get_phoenix_run_window()
            )
        except ValueError:
            return jsonify({'error': 'Invalid run window'}), 400

        normalized_output_key = output_key.strip().lower()
        _queue_context_edge_generation(normalized_output_key, run_window, source="manual-admin")
        return jsonify({
            'status': 'queued',
            'output_key': normalized_output_key,
            'run_window': run_window,
        }), 200

    except Exception as e:
        logger.error(f"[CONTEXT EDGE] Manual admin queue failed for {output_key}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/context-edge/run-all/<run_window>', methods=['POST'])
def admin_context_edge_run_all(run_window):
    if not _context_edge_admin_authorized():
        return jsonify({'error': 'forbidden'}), 403

    try:
        from supabase_backend import normalize_context_edge_run_window

        try:
            normalized_run_window = normalize_context_edge_run_window(run_window)
        except ValueError:
            return jsonify({'error': 'Invalid run window'}), 400

        for output_key in CONTEXT_EDGE_OUTPUT_KEYS:
            _queue_context_edge_generation(
                output_key,
                normalized_run_window,
                source="manual-admin-run-all",
            )

        return jsonify({
            'status': 'queued',
            'run_window': normalized_run_window,
            'count': len(CONTEXT_EDGE_OUTPUT_KEYS),
            'output_keys': CONTEXT_EDGE_OUTPUT_KEYS,
        }), 200

    except Exception as e:
        logger.error(f"[CONTEXT EDGE] Manual admin run-all queue failed for {run_window}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/context-edge/output/<output_key>', methods=['GET'])
def context_edge_button_output(output_key):
    """Return the latest cached Context Edge output for a quick button."""
    try:
        from context_edge_button_output_service import get_output_config
        from supabase_backend import (
            SupabaseConfigError,
            read_latest_context_edge_button_output,
        )

        try:
            config = get_output_config(output_key)
        except KeyError:
            return jsonify({'error': 'Invalid output key'}), 404
        normalized_output_key = output_key.strip().lower()

        try:
            cached = read_latest_context_edge_button_output(normalized_output_key)
        except SupabaseConfigError:
            logger.warning('[CONTEXT EDGE] Supabase is not configured')
            cached = None
        except Exception as e:
            logger.error(f'[CONTEXT EDGE] Cached output read failed: {e}')
            cached = None

        if cached and cached.get('status') == 'ready':
            return jsonify({
                'status':       'ready',
                'output_key':   normalized_output_key,
                'label':        config['label'],
                'generated_at': cached.get('generated_at'),
                'run_window':   cached.get('run_window'),
                'report':       cached.get('report_json'),
            }), 200

        if cached and cached.get('status') == 'failed':
            return jsonify({
                'status':       'failed',
                'output_key':   normalized_output_key,
                'label':        config['label'],
                'generated_at': cached.get('generated_at'),
                'run_window':   cached.get('run_window'),
                'message':      'Context Edge scan failed. Please try again later.',
            }), 200

        return jsonify({
            'status':     'pending',
            'output_key': normalized_output_key,
            'label':      config['label'],
            'message':    (
                "NFL scan not active yet."
                if normalized_output_key == "nfl_value"
                else "Context Edge is preparing today's scan."
            ),
        }), 200

    except Exception as e:
        logger.error(f'[CONTEXT EDGE] Output endpoint error: {e}')
        return jsonify({'error': 'Context Edge output unavailable'}), 500


@app.route('/api/context-edge/check-access', methods=['POST'])
def context_edge_check_access():
    data = request.get_json(silent=True) or {}
    email = _normalize_context_edge_email(data.get('email'))

    if not email or '@' not in email:
        return jsonify({'access': False, 'error': 'A valid email is required'}), 400

    access = _context_edge_access_allowed(email)
    if access:
        _set_context_edge_session(email)
    elif session.get(CONTEXT_EDGE_SESSION_EMAIL_KEY) == email:
        _clear_context_edge_session()

    return jsonify({'access': access}), 200


@app.route('/api/context-edge', methods=['POST'])
def context_edge_analysis():
    """
    Receives a betting line from the frontend and returns Context Edge's
    Context Edge analysis as JSON. Additive feature — does not affect
    any existing route or data pipeline.
    """
    raw = None
    try:
        from openai import APITimeoutError
        from datetime import datetime as _dt
        today_str = _dt.now().strftime('%A %B %d %Y')
        # Lightweight per-IP rate limit
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown')
              .split(',')[0].strip())
        now = time.time()
        bucket = [t for t in _CE_RATE_BUCKETS.get(ip, []) if now - t < _CE_RATE_WINDOW]
        if len(bucket) >= _CE_RATE_MAX:
            return jsonify({'response': 'Too many requests. Wait a few minutes and try again.'}), 200
        bucket.append(now)
        _CE_RATE_BUCKETS[ip] = bucket

        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400

        # Passcode gate remains available; paid/owner email access can also unlock.
        expected_passcode = os.environ.get('CONTEXT_EDGE_PASSCODE', '4774')
        passcode_valid = str(data.get('passcode', '')).strip() == expected_passcode
        email_access_valid = False
        if not passcode_valid:
            email_access_valid = _context_edge_access_allowed(data.get('email', ''))

        if not passcode_valid and not email_access_valid:
            return jsonify({'error': 'Invalid passcode.'}), 403

        # validate_only: passcode check passed above, so just confirm access. No AI call.
        if data.get('validate_only'):
            return jsonify({'valid': True}), 200

        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            return jsonify({'error': 'Context Edge is not configured.'}), 503

        # Load today's live no-vig board fresh on every call (READ ONLY — never writes).
        board = load_board_for_context()
        if not board:
            return jsonify({'response': "No props on the board right now. Check back after 7 AM PHX when the daily fetch runs."})

        # New chat flow: the user types free text; the board is supplied to the model below.
        chat_message = (data.get('user_message') or '').strip()
        if not chat_message:
            return jsonify({'error': 'No input'}), 400

        # Pre-build the board string BEFORE the f-string so the f-string
        # only references simple variables (no inline function calls).
        board_text = format_board_for_context_ai(board)
        from context_edge_prompt_service import (
            build_context_edge_system_prompt,
            call_context_edge_ai,
        )

        SYSTEM_PROMPT = build_context_edge_system_prompt(today_str, board_text)
        try:
            raw = call_context_edge_ai(api_key, SYSTEM_PROMPT, chat_message)
            return jsonify({'response': raw})

        except APITimeoutError:
            logger.warning('Context edge timeout')
            return jsonify({
                'response': 'Research is taking longer than usual. Try asking about a specific team or game instead of the full slate.'
            }), 200

        except Exception as ai_err:
            logger.error(f'Context Edge AI call failed: {ai_err}')
            return jsonify({
                'response': 'Analysis failed. Try again.'
            }), 500

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f'Context edge error: {tb}')
        return jsonify({'error': 'Analysis failed. Try again.'}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
