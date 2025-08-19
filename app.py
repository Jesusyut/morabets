import os
import json
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from redis_client import create_redis_client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app)

# Initialize Redis client
redis_client = create_redis_client()

@app.route("/healthz")
def healthz():
    """Health check endpoint"""
    return jsonify({"ok": True})

@app.route("/readyz")
def readyz():
    """Readiness check endpoint - ready only if Redis has fresh magazine data"""
    try:
        if not redis_client:
            return jsonify({"ready": False, "reason": "redis_unavailable"}), 503
        
        # Check if magazine data exists and is fresh (< 15 minutes old)
        magazine_data = redis_client.get("mb:magazine:latest")
        if not magazine_data:
            return jsonify({"ready": False, "reason": "no_magazine_data"}), 503
        
        magazine = json.loads(magazine_data)
        magazine_time = datetime.fromisoformat(magazine["timestamp"].replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - magazine_time).total_seconds() / 60
        
        if age_minutes > 15:
            return jsonify({
                "ready": False, 
                "reason": "stale_data", 
                "age_minutes": round(age_minutes, 1)
            }), 503
        
        return jsonify({
            "ready": True,
            "last_refresh": magazine["timestamp"],
            "age_minutes": round(age_minutes, 1)
        })
        
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        return jsonify({"ready": False, "reason": "error", "error": str(e)}), 503

@app.route("/props")
def get_props():
    """Get props data from Redis cache"""
    try:
        # Get today's date
        today = datetime.now().strftime("%Y%m%d")
        props_key = f"mb:props:{today}"
        
        if not redis_client:
            return jsonify({"error": "Redis unavailable"}), 503
        
        props_data = redis_client.get(props_key)
        if not props_data:
            return jsonify({"error": "No props data available"}), 404
        
        props = json.loads(props_data)
        return jsonify(props)
        
    except Exception as e:
        logger.error(f"Props endpoint error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/magazine")
def get_magazine():
    """Get magazine data from Redis cache"""
    try:
        if not redis_client:
            return jsonify({"error": "Redis unavailable"}), 503
        
        magazine_data = redis_client.get("mb:magazine:latest")
        if not magazine_data:
            return jsonify({"error": "No magazine data available"}), 404
        
        magazine = json.loads(magazine_data)
        return jsonify(magazine)
        
    except Exception as e:
        logger.error(f"Magazine endpoint error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/dashboard")
def dashboard():
    """Dashboard page"""
    try:
        # Get basic stats for dashboard
        stats = {}
        if redis_client:
            # Get last refresh times
            refresh_meta = redis_client.hgetall("mb:meta:last_refresh")
            stats["last_refresh"] = refresh_meta
            
            # Get today's props count
            today = datetime.now().strftime("%Y%m%d")
            props_key = f"mb:props:{today}"
            props_data = redis_client.get(props_key)
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
        "redis_connected": redis_client is not None,
        "cache_type": "redis" if redis_client else "none"
    })

@app.route("/ping")
def ping():
    """Ping endpoint"""
    redis_status = "OK" if redis_client else "FAIL"
    return jsonify({"status": "running", "redis": redis_status})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

