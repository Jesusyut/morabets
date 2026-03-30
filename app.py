import os
import json
import logging
import time
import random
import string
import smtplib
import requests
import stripe
import uuid
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# IMPORTANT: In Stripe Dashboard → Payment Links → Edit your $24.99 link
# → After payment section → Confirmation page → select "Don't show confirmation page"
# → Redirect URL: https://morabets.com/success?session_id={CHECKOUT_SESSION_ID}
#
# Also register webhook at: https://morabets.com/webhook
# Events: checkout.session.completed, customer.subscription.deleted, invoice.payment_failed

# Stripe configuration
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
LICENSE_DB = 'license_keys.json'
TOKENS_FILE = 'access_tokens.json'


# ── Token system ───────────────────────────────────────────────────────────────

def load_tokens():
    """Load all access tokens from persistent file."""
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_tokens(tokens):
    """Save all access tokens and sync active ones to LICENSE_DB for dashboard compatibility."""
    try:
        with open(TOKENS_FILE, 'w') as f:
            json.dump(tokens, f, indent=2)
    except Exception as e:
        logger.error(f'[TOKEN] Could not save tokens: {e}')
    # Sync to license_keys.json so existing dashboard validation works
    try:
        try:
            with open(LICENSE_DB, 'r') as f:
                keys = json.load(f)
        except Exception:
            keys = {}
        for token, data in tokens.items():
            keys[token.lower()] = {'email': data.get('email', ''), 'plan': 'monthly', 'active': data.get('active', True)}
        with open(LICENSE_DB, 'w') as f:
            json.dump(keys, f, indent=2)
    except Exception as e:
        logger.error(f'[TOKEN] Could not sync to LICENSE_DB: {e}')


def generate_access_token(customer_name, customer_email):
    """Generate unique access token in MB-XX-XXXX-XXXX format. One token per email."""
    tokens = load_tokens()
    for t, data in tokens.items():
        if data.get('email') == customer_email and data.get('active'):
            logger.info(f'[TOKEN] Existing token found for {customer_email}')
            return t
    if customer_name and customer_name.strip():
        parts = customer_name.strip().split()
        initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else parts[0][:2].upper()
    else:
        initials = customer_email.split('@')[0][:2].upper()
    chars = string.ascii_uppercase + string.digits
    while True:
        seg1 = ''.join(random.choices(chars, k=4))
        seg2 = ''.join(random.choices(chars, k=4))
        token = f'MB-{initials}-{seg1}-{seg2}'
        if token not in tokens:
            break
    logger.info(f'[TOKEN] Generated {token} for {customer_email}')
    return token


def get_token_by_email(email):
    """Look up active token for a given email."""
    tokens = load_tokens()
    for t, data in tokens.items():
        if data.get('email') == email and data.get('active'):
            return t
    return None


def deactivate_token_by_subscription(subscription_id):
    """Deactivate token when subscription is cancelled."""
    tokens = load_tokens()
    for t, data in tokens.items():
        if data.get('subscription_id') == subscription_id:
            tokens[t]['active'] = False
            tokens[t]['deactivated_at'] = datetime.utcnow().isoformat()
            save_tokens(tokens)
            logger.info(f'[TOKEN] Deactivated {t} (subscription {subscription_id} cancelled)')
            return t
    return None


def validate_token(token):
    """Check if a token is valid and active."""
    tokens = load_tokens()
    if token in tokens:
        return tokens[token].get('active', False)
    return False


# ── Email delivery ─────────────────────────────────────────────────────────────

def send_access_key_email(to_email, customer_name, access_token):
    """Send access key to customer immediately after payment."""
    from_email = os.environ.get('EMAIL_FROM')
    password = os.environ.get('EMAIL_PASSWORD')
    if not from_email or not password:
        logger.info(f'[EMAIL] Credentials not set — skipping email to {to_email}. Add EMAIL_FROM and EMAIL_PASSWORD.')
        return False
    first_name = customer_name.split()[0] if customer_name else 'there'
    subject = f'Your Mora Bets Access Key — {access_token}'
    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Inter,-apple-system,sans-serif;">
<div style="max-width:520px;margin:40px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
  <div style="background:#1B6B3A;padding:28px 32px;text-align:center;">
    <div style="font-size:20px;font-weight:800;color:#fff;letter-spacing:1px;">MORA BETS</div>
    <div style="font-size:12px;color:rgba(255,255,255,0.7);margin-top:4px;letter-spacing:0.5px;">DAILY EDGE · LIVE PROPS · TRUE PROBABILITY</div>
  </div>
  <div style="padding:32px;">
    <h1 style="font-size:22px;font-weight:700;color:#1a1a1a;margin:0 0 8px;">You're in, {first_name}.</h1>
    <p style="font-size:14px;color:#555;line-height:1.7;margin:0 0 28px;">Your Mora Bets subscription is active and your access key is ready below. Save it — this is your key into the dashboard every single day.</p>
    <div style="background:#F5FAF7;border:2px solid #C8E6D4;border-radius:12px;padding:20px 24px;text-align:center;margin-bottom:20px;">
      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#1B6B3A;margin-bottom:10px;">Your Access Key</div>
      <div style="font-size:26px;font-weight:800;color:#1a1a1a;letter-spacing:3px;font-family:monospace;">{access_token}</div>
    </div>
    <div style="background:#FFF8F0;border-left:3px solid #E8A020;padding:12px 16px;border-radius:0 8px 8px 0;margin-bottom:24px;">
      <p style="font-size:12px;color:#8B5500;margin:0;line-height:1.5;"><strong>Important:</strong> This key is issued once per account. Do not share it. Accounts found sharing access keys will be permanently banned without refund.</p>
    </div>
    <a href="https://morabets.com/dashboard?key={access_token}" style="display:block;background:#1B6B3A;color:#fff;text-align:center;padding:15px;border-radius:10px;text-decoration:none;font-size:15px;font-weight:700;margin-bottom:28px;">Go to Your Dashboard →</a>
    <div style="border-top:1px solid #ECEAE5;padding-top:24px;">
      <p style="font-size:13px;font-weight:700;color:#1a1a1a;margin:0 0 14px;">How to use your key</p>
      <p style="font-size:13px;color:#555;margin:0 0 8px;line-height:1.5;">1. Go to morabets.com — enter your key to access the dashboard</p>
      <p style="font-size:13px;color:#555;margin:0 0 8px;line-height:1.5;">2. Check your dashboard every morning — props update fresh daily before first pitch</p>
      <p style="font-size:13px;color:#555;margin:0 0 0;line-height:1.5;">3. Follow the 7 Golden Rules — let the math compound over the season</p>
    </div>
  </div>
  <div style="background:#F7F6F3;padding:20px 32px;text-align:center;">
    <p style="font-size:11px;color:#999;margin:0;line-height:1.7;">Questions? Reply to this email anytime.<br>
    <a href="https://morabets.com/cancel" style="color:#1B6B3A;">Manage subscription</a> · <a href="https://morabets.com" style="color:#1B6B3A;">morabets.com</a><br><br>
    © 2026 Mora Bets. For informational and entertainment purposes only.<br>Please bet responsibly. ncpgambling.org</p>
  </div>
</div>
</body>
</html>"""
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f'Mora Bets <{from_email}>'
    msg['To'] = to_email
    msg.attach(MIMEText(html_body, 'html'))
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(from_email, password)
            server.sendmail(from_email, to_email, msg.as_string())
        logger.info(f'[EMAIL] Sent access key to {to_email}')
        return True
    except Exception as e:
        logger.error(f'[EMAIL ERROR] Failed for {to_email}: {e}')
        return False


# Updated Stripe configuration for monthly/yearly pricing
PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
PRICE_MONTHLY = os.environ.get("STRIPE_PRICE_ID_MONTHLY", "price_1RtyVnIzLEeC8QTzhOrtq2CO")
PRICE_YEARLY = os.environ.get("STRIPE_PRICE_ID_YEARLY", "price_1RtyYYIzLEeC8QTzw8JsGH39")
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "3"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")

# Legacy price lookup for backward compatibility
PRICE_LOOKUP = {
    'prod_SjjH7D6kkxRbJf': 'price_1RoFpPIzLEeC8QTz5kdeiLyf',  # Calculator Tool - $9.99/month
    'prod_Sjkk8GQGPBvuOP': 'price_1RoHFOIzLEeC8QTziT9k1t45'   # Mora Assist - $28.99
}



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
    """Redirect to how-it-works landing page"""
    return redirect(url_for("how_it_works"))

@app.route("/how-it-works")
def how_it_works():
    """Landing page explaining how Mora Bets works"""
    return render_template("how_it_works.html")

@app.route("/paywall")
def paywall():
    """Pricing page with Stripe checkout options"""
    return render_template("index.html")

@app.route("/config", methods=["GET"])
def paywall_config():
    """Return paywall configuration for frontend"""
    return jsonify({
        "publicKey": PUBLISHABLE_KEY,
        "priceMonthly": PRICE_MONTHLY,
        "priceYearly": PRICE_YEARLY,
        "trialDays": TRIAL_DAYS
    })

@app.route("/tool")
def tool():
    """Tool access page - requires valid license"""
    # Check if user has valid license in session
    if session.get("licensed"):
        return redirect(url_for("dashboard"))
    else:
        return redirect(url_for("paywall") + "?message=You need a valid license key to access the tool.")

@app.route("/create-checkout-session", methods=['POST'])
def create_checkout_session():
    """Create Stripe checkout session - supports both legacy and new pricing"""
    try:
        # Handle legacy form-based product ID
        product_id = request.form.get('product_id')
        
        # Handle new JSON-based price ID for monthly/yearly toggle
        data = None
        if not product_id:
            try:
                data = request.get_json(force=True)
                price_id = data.get("price_id") if data else None
            except:
                return jsonify({"error": "Missing product or price ID"}), 400
        else:
            price_id = PRICE_LOOKUP.get(product_id)
        
        if not price_id:
            return jsonify({"error": "Invalid product"}), 400
        
        # Validate that price_id is one of our accepted prices
        if price_id not in [PRICE_MONTHLY, PRICE_YEARLY] + list(PRICE_LOOKUP.values()):
            return jsonify({"error": "Invalid price"}), 400
            
        # Configure session
        subscription_data = {}
        
        # Add trial only for monthly subscription
        if price_id == PRICE_MONTHLY and TRIAL_DAYS > 0:
            subscription_data["trial_period_days"] = TRIAL_DAYS
        
        # Legacy trial for old price
        if price_id == 'price_1RoFpPIzLEeC8QTz5kdeiLyf':
            subscription_data["trial_period_days"] = 3
            
        session_config = {
            'line_items': [{'price': price_id, 'quantity': 1}],
            'mode': 'subscription',
            'allow_promotion_codes': True,
            'success_url': f'{APP_BASE_URL}/verify?session_id={{CHECKOUT_SESSION_ID}}',
            'cancel_url': f'{APP_BASE_URL}/paywall?canceled=true',
        }
        
        if subscription_data:
            session_config['subscription_data'] = subscription_data
        
        # Enable phone collection and disclaimer for legacy Mora Assist
        if product_id == 'prod_Sjkk8GQGPBvuOP':
            session_config['phone_number_collection'] = {'enabled': True}
            session_config['custom_fields'] = [
                {
                    'key': 'disclaimer',
                    'label': {
                        'type': 'custom',
                        'custom': 'Risk Acknowledgment (18+)'
                    },
                    'type': 'dropdown',
                    'dropdown': {
                        'options': [
                            {'label': 'I agree (not financial advice)', 'value': 'agree'}
                        ]
                    },
                    'optional': False
                }
            ]
        
        session = stripe.checkout.Session.create(**session_config)
        
        # Return JSON response for new API or redirect for legacy
        if data:
            return jsonify({"id": session.id, "url": session.url})
        else:
            return redirect(session.url or request.url_root, code=303)
            
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        logger.error(f"Full traceback: {e}", exc_info=True)
        if data:
            return jsonify({"error": str(e)}), 400
        else:
            return f"Checkout failed: {str(e)}", 500


@app.route("/webhook", methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    webhook_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')

    try:
        if webhook_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        else:
            event = json.loads(payload)
            logger.warning('[WEBHOOK] No STRIPE_WEBHOOK_SECRET set — skipping signature verification')
    except stripe.error.SignatureVerificationError as e:
        logger.error(f'[WEBHOOK] Signature failed: {e}')
        return jsonify({'error': 'Invalid signature'}), 400
    except Exception as e:
        logger.error(f'[WEBHOOK] Error: {e}')
        return jsonify({'error': str(e)}), 400

    event_type = event.get('type') if isinstance(event, dict) else event['type']
    logger.info(f'[WEBHOOK] Event received: {event_type}')

    if event_type == 'checkout.session.completed':
        session_obj = event['data']['object']
        customer_details = session_obj.get('customer_details') or {}
        customer_email = customer_details.get('email', '')
        customer_name = customer_details.get('name', '') or ''
        subscription_id = session_obj.get('subscription', '')
        customer_id = session_obj.get('customer', '')

        if customer_email:
            tokens = load_tokens()
            existing = get_token_by_email(customer_email)
            if existing:
                logger.info(f'[WEBHOOK] Token already exists for {customer_email}: {existing}')
                if subscription_id and not tokens[existing].get('subscription_id'):
                    tokens[existing]['subscription_id'] = subscription_id
                    save_tokens(tokens)
            else:
                token = generate_access_token(customer_name, customer_email)
                tokens[token] = {
                    'email': customer_email,
                    'name': customer_name,
                    'stripe_customer_id': customer_id,
                    'subscription_id': subscription_id,
                    'created_at': datetime.utcnow().isoformat(),
                    'active': True
                }
                save_tokens(tokens)
                send_access_key_email(customer_email, customer_name, token)
                logger.info(f'[WEBHOOK] New subscriber processed: {customer_email}')

    elif event_type == 'customer.subscription.deleted':
        subscription = event['data']['object']
        sub_id = subscription.get('id')
        deactivated = deactivate_token_by_subscription(sub_id)
        if deactivated:
            logger.info(f'[WEBHOOK] Token deactivated for subscription {sub_id}')

    elif event_type == 'invoice.payment_failed':
        invoice = event['data']['object']
        customer_email = invoice.get('customer_email', 'unknown')
        logger.warning(f'[WEBHOOK] Payment failed for {customer_email}')

    return jsonify({'status': 'ok'}), 200


@app.route('/success')
def success():
    """Post-payment success page — displays access key to new subscriber."""
    session_id = request.args.get('session_id')
    if not session_id:
        return render_template('success.html', access_token=None, customer_name=None, customer_email=None, pending=True)
    try:
        stripe_session = stripe.checkout.Session.retrieve(session_id)
        customer_email = stripe_session.customer_details.email
        customer_name = stripe_session.customer_details.name or ''
        tokens = load_tokens()
        token = None
        for t, data in tokens.items():
            if data.get('email') == customer_email and data.get('active'):
                token = t
                break
        if not token:
            token = generate_access_token(customer_name, customer_email)
            tokens[token] = {
                'email': customer_email,
                'name': customer_name,
                'stripe_customer_id': stripe_session.get('customer'),
                'subscription_id': stripe_session.get('subscription'),
                'created_at': datetime.utcnow().isoformat(),
                'active': True,
                'session_id': session_id
            }
            save_tokens(tokens)
            send_access_key_email(customer_email, customer_name, token)
        return render_template('success.html', access_token=token, customer_name=customer_name, customer_email=customer_email, pending=False)
    except Exception as e:
        logger.error(f'[SUCCESS] Error: {e}')
        return render_template('success.html', access_token=None, customer_name=None, customer_email=None, pending=True)


@app.route('/admin/health')
def admin_health():
    """Admin health check — shows token count and system status."""
    admin_key = request.args.get('key')
    if admin_key != os.environ.get('ADMIN_KEY', 'mora-admin-2026'):
        return jsonify({'error': 'unauthorized'}), 401
    tokens = load_tokens()
    active = [t for t, d in tokens.items() if d.get('active')]
    inactive = [t for t, d in tokens.items() if not d.get('active')]
    return jsonify({
        'status': 'running',
        'total_tokens': len(tokens),
        'active_subscribers': len(active),
        'cancelled': len(inactive),
        'stripe_key_set': bool(os.environ.get('STRIPE_SECRET_KEY')),
        'webhook_secret_set': bool(os.environ.get('STRIPE_WEBHOOK_SECRET')),
        'email_configured': bool(os.environ.get('EMAIL_FROM')),
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route("/dashboard")
def dashboard():
    """Main Mora Bets dashboard - protected route"""
    # Check for key parameter
    user_key = request.args.get('key', '').strip()
    
    if user_key:
        # Validate key
        try:
            with open(LICENSE_DB, 'r') as f:
                keys = json.load(f)
        except Exception as e:
            logger.error(f"Error loading license keys: {e}")
            return redirect(url_for('index') + '?message=System+error.+Please+try+again.')
        
        # Check if key exists and is valid (case-insensitive)
        is_valid = False
        for key in keys:
            if key.upper() == user_key.upper() and keys[key]:
                is_valid = True
                break
        
        if not is_valid:
            logger.info(f"Invalid key attempt: {user_key}")
            return redirect(url_for('index') + '?message=Invalid+key.+Please+try+again.')
        
        # Key is valid, set session and render dashboard
        session["licensed"] = True
        session["license_key"] = user_key
        logger.info(f"✅ Dashboard access granted for key: {user_key}")
    
    try:
        hits = cache_incr("hits")
        return render_template("dashboard.html", hits=hits)
    except Exception as e:
        logger.error(f"Error in dashboard route: {e}")
        return f'''
        <!DOCTYPE html>
        <html>
        <head><title>Mora Bets</title></head>
        <body>
        <h1>Mora Bets - Sports Betting Analytics</h1>
        <p>System Status: Running</p>
        <p>Error: {str(e)}</p>
        <p><a href="/health">Health Check</a></p>
        <p><a href="/api/status">API Status</a></p>
        </body>
        </html>
        '''

@app.route("/verify")
def verify():
    """Handle Stripe success and generate license key"""
    session_id = request.args.get('session_id')
    key = request.args.get('key')  # For direct key display
    
    if key:
        return render_template('verify.html', key=key)
    
    if not session_id:
        return render_template('verify.html', error='Missing session ID.')

    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=['customer'])
        if not session.customer_details:
            return render_template('verify.html', error="No customer details found")
        customer_email = session.customer_details.email or "unknown@example.com"
        customer_name = session.customer_details.name or 'user'
        last = customer_name.split()[-1].lower()
        suffix = str(uuid.uuid4().int)[-4:]
        key = f'{last}{suffix}'

        # Load existing keys
        try:
            with open(LICENSE_DB, 'r') as f:
                keys = json.load(f)
        except:
            keys = {}

        # Check if this is Mora Assist (no license key needed)
        line_items = session.get('line_items', {}).get('data', [])
        is_mora_assist = False
        if line_items:
            price_id = line_items[0].get('price', {}).get('id', '')
            is_mora_assist = price_id == 'price_1RoHFOIzLEeC8QTziT9k1t45'
        
        if is_mora_assist:
            # Mora Assist - no license key, just confirmation
            phone_number = getattr(session.customer_details, 'phone', 'Not provided')
            logger.info(f"✅ Mora Assist purchase confirmed: {customer_email}, Phone: {phone_number}")
            return render_template('verify.html', mora_assist=True, email=customer_email, phone=phone_number)
        else:
            # Calculator Tool - generate license key
            keys[key] = {'email': customer_email, 'plan': session.mode}
            with open(LICENSE_DB, 'w') as f:
                json.dump(keys, f)

            logger.info(f"✅ Generated license key for {customer_email}: {key}")
            return render_template('verify.html', key=key)
        
    except Exception as e:
        logger.error(f"❌ Stripe verification error: {e}")
        return render_template('verify.html', error='Verification failed. Please contact support.')

@app.route("/verify-key")
def verify_key():
    """Verify license key for dashboard access"""
    user_key = request.args.get('key', '').strip()
    
    # Load keys from JSON file
    try:
        with open(LICENSE_DB, 'r') as f:
            keys = json.load(f)
    except Exception as e:
        logger.error(f"Error loading license keys: {e}")
        return jsonify({'valid': False})
    
    # Check if key exists and is valid (case-insensitive)
    is_valid = False
    for key in keys:
        if key.upper() == user_key.upper() and keys[key]:
            is_valid = True
            break
    
    logger.info(f"Key verification for '{user_key}': {'Valid' if is_valid else 'Invalid'}")
    
    return jsonify({'valid': is_valid})

@app.route("/validate-key", methods=['POST'])
def validate_key():
    """Validate license key and grant access"""
    user_key = request.form.get('key', '').strip().lower()
    
    # Check master key first
    if user_key == 'mora-king':
        session["licensed"] = True
        session["license_key"] = user_key
        session["access_level"] = "creator"
        logger.info("✅ Master key access granted")
        return jsonify({'valid': True, 'redirect': url_for('dashboard')})
    
    # Check license database
    try:
        with open(LICENSE_DB, 'r') as f:
            keys = json.load(f)
    except:
        return jsonify({'valid': False})
    
    if user_key in keys:
        session["licensed"] = True
        session["license_key"] = user_key
        session["access_level"] = "premium"
        logger.info(f"✅ License key validated: {user_key}")
        return jsonify({'valid': True, 'redirect': url_for('dashboard')})
    
    return jsonify({'valid': False})

@app.before_request
def require_license():
    """Protect dashboard routes except public pages and API endpoints"""
    # Allow access to public pages, verification, health checks, API endpoints, and static files
    public_endpoints = [
        "home", "how_it_works", "paywall", "paywall_config", "tool", "verify", "verify_key", "validate_key", "create_checkout_session",
        "stripe_webhook", "billing_portal", "health", "ping", "static", "api_status", "get_props", "filtered_moneylines",
        "logout", "dashboard", "analytics", "success", "admin_health"
    ]
    
    # Also allow access to any route starting with /api/
    if request.endpoint in public_endpoints or request.path.startswith("/static") or request.path.startswith("/api/"):
        return
    
    # Check if user has valid license in session for protected routes
    if not session.get("licensed"):
        return redirect(url_for("paywall"))

@app.route("/health")
def health():
    """Health check endpoint - instant response"""
    return jsonify({"health": "live"}), 200

@app.route("/status")
def status():
    """Simple status endpoint for health checks"""
    return jsonify({"status": "OK"}), 200

@app.route("/billing-portal")
def billing_portal():
    """Create Stripe billing portal session for subscription management"""
    try:
        # This is a placeholder - in production, you'd retrieve the customer ID from your session/database
        # For now, return to paywall with message about contacting support
        return redirect(url_for("paywall") + "?message=To manage your subscription, please contact support with your license key.")
        
        # Future implementation when customer IDs are stored:
        # customer_id = session.get('stripe_customer_id')
        # if not customer_id:
        #     return redirect(url_for("paywall") + "?message=No active subscription found.")
        # 
        # portal_session = stripe.billing_portal.Session.create(
        #     customer=customer_id,
        #     return_url=f'{request.url_root}dashboard'
        # )
        # return redirect(portal_session.url)
    except Exception as e:
        logger.error(f"Billing portal error: {e}")
        return redirect(url_for("paywall") + "?message=Unable to access billing portal. Please contact support.")

@app.route("/logout")
def logout():
    """Clear license session for testing"""
    session.clear()
    return redirect(url_for("how_it_works"))

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
    """Return NHL game-level odds (moneyline, spreads, totals)."""
    try:
        from nhl_odds_api import fetch_nhl_game_odds
        odds = fetch_nhl_game_odds()
        return jsonify({"odds": odds, "sport": "NHL"})
    except Exception as e:
        logger.error(f"[NHL] /api/nhl/odds error: {e}")
        return jsonify({"error": "Failed to load NHL odds"}), 500

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
