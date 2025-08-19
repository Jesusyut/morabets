import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from redis_client import redis_client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# CORS configuration
origins = os.getenv("CORS_ORIGINS", "https://morabets.com,https://www.morabets.com").split(",")
CORS(app, origins=[o.strip() for o in origins])

# Initialize Redis client
try:
    r = redis_client()
    logger.info("✅ Redis client initialized")
except Exception as e:
    logger.error(f"❌ Redis client failed: {e}")
    r = None

def today_yyyymmdd():
    """Get today's date in YYYYMMDD format in UTC"""
    return datetime.now(timezone.utc).strftime("%Y%m%d")

@app.route("/healthz")
def healthz():
    """Health check endpoint"""
    return jsonify({"ok": True})

@app.route("/readyz")
def readyz():
    """Readiness check endpoint - ready only if Redis has fresh data"""
    try:
        if not r:
            return jsonify({"ready": False, "reason": "redis_unavailable"}), 503
        
        # Check last refresh timestamp
        ts = r.hget("mb:meta", "last_refresh_ts")
        if not ts:
            return jsonify({"ready": False, "reason": "no_refresh_data"}), 503
        
        # Check if data is fresh (< 15 minutes old)
        current_ts = int(datetime.now(timezone.utc).timestamp())
        age_seconds = current_ts - int(ts)
        age_minutes = age_seconds / 60
        
        ready = age_seconds < 15 * 60  # 15 minutes
        
        return jsonify({
            "ready": ready,
            "last_refresh_ts": ts,
            "age_minutes": round(age_minutes, 1),
            "reason": "stale_data" if not ready else None
        })
        
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return jsonify({"ready": False, "reason": "error", "error": str(e)}), 503

@app.route("/props")
def get_props():
    """Get props data from Redis cache"""
    try:
        league = request.args.get("league", "mlb")
        date = request.args.get("date")
        d = date or today_yyyymmdd()
        key = f"mb:props:{league}:{d}"
        
        if not r:
            return jsonify({"error": "Redis unavailable"}), 503
        
        raw = r.get(key)
        if not raw:
            if os.getenv("ALLOW_LIVE_FALLBACK") == "true":
                # OPTIONAL: call a minimal fetch() here to keep UI alive during cold starts
                logger.warning(f"Cache miss for {key}, fallback disabled")
                return jsonify({"error": "cache_miss_fallback_disabled"}), 503
            return jsonify({"error": f"cache_miss:{key}"}), 503
        
        return jsonify(json.loads(raw))
        
    except Exception as e:
        logger.error(f"Props endpoint error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/magazine")
def get_magazine():
    """Get magazine data from Redis cache"""
    try:
        if not r:
            return jsonify({"error": "Redis unavailable"}), 503
        
        magazine_data = r.get("mb:magazine:latest")
        if not magazine_data:
            return jsonify({"error": "No magazine data available"}), 404
        
        magazine = json.loads(magazine_data)
        return jsonify(magazine)
        
    except Exception as e:
        logger.error(f"Magazine endpoint error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/debug/state")
def debug_state():
    """Debug endpoint to check Redis state"""
    try:
        if not r:
            return jsonify({"error": "Redis unavailable"}), 503
        
        keys = r.keys("mb:*")
        meta = r.hgetall("mb:meta")
        
        # Get sample data from first props key
        sample_data = None
        props_keys = [k for k in keys if k.startswith("mb:props:")]
        if props_keys:
            sample_key = props_keys[0]
            sample_raw = r.get(sample_key)
            if sample_raw:
                try:
                    sample_data = json.loads(sample_raw)
                    # Truncate for readability
                    if isinstance(sample_data, dict) and "data" in sample_data:
                        sample_data["data"] = sample_data["data"][:2] if isinstance(sample_data["data"], list) else sample_data["data"]
                except:
                    sample_data = {"error": "invalid_json"}
        
        return jsonify({
            "keys": keys,
            "meta": meta,
            "sample_key": props_keys[0] if props_keys else None,
            "sample_data": sample_data,
            "redis_connected": True
        })
        
    except Exception as e:
        logger.error(f"Debug state error: {e}")
        return jsonify({"error": str(e), "redis_connected": False}), 500

@app.route("/dashboard")
def dashboard():
    """Dashboard page"""
    try:
        # Get basic stats for dashboard
        stats = {}
        if r:
            # Get last refresh times
            refresh_meta = r.hgetall("mb:meta")
            stats["last_refresh"] = refresh_meta
            
            # Get today's props count
            today = today_yyyymmdd()
            props_key = f"mb:props:mlb:{today}"
            props_data = r.get(props_key)
            if props_data:
                props = json.loads(props_data)
                stats["props_count"] = props.get("count", 0)
        
        return render_template("dashboard.html", stats=stats)
        
    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return render_template("dashboard.html", stats={})

@app.route("/")
def index():
    """Landing page"""
    return render_template("index.html")

# Legacy endpoints for backward compatibility
@app.route("/player_props", methods=["GET"])
def player_props():
    """Legacy player props endpoint - redirects to /props"""
    return get_props()

@app.route("/api/status")
def api_status():
    """Legacy status endpoint"""
    return jsonify({
        "status": "ok",
        "message": "Mora Bets API - Web Only Mode",
        "redis_connected": r is not None,
        "cache_type": "redis" if r else "none"
    })

@app.route("/ping")
def ping():
    """Ping endpoint"""
    redis_status = "OK" if r else "FAIL"
    return jsonify({"status": "running", "redis": redis_status})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

