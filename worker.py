#!/usr/bin/env python3
"""
Mora Bets Worker - Production-ready background worker
"""
import os
import json
import asyncio
import logging
from datetime import datetime, timezone
import httpx
import redis

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Redis connection
try:
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    logger.info("✅ Redis client initialized")
except Exception as e:
    logger.error(f"❌ Redis client failed: {e}")
    r = None

def cache_key(league="mlb", dt=None):
    """Generate cache key for props data"""
    d = (dt or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"mb:props:{league}:{d}"

def now_ts():
    """Get current timestamp"""
    return int(datetime.now(timezone.utc).timestamp())
    
# ---- INSERT YOUR EXISTING FETCH/ENRICH CODE HERE ----
async def fetch_odds(client):
    """Fetch odds data from The Odds API"""
    try:
        url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
        params = {
            "regions": "us",
            "markets": "h2h,totals,player_props",
            "oddsFormat": "american",
            "apiKey": os.environ["ODDS_API_KEY"]
        }
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        logger.info(f"✅ Fetched {len(resp.json())} odds events")
        return resp.json()
    except Exception as e:
        logger.error(f"❌ Failed to fetch odds: {e}")
        raise

async def fetch_mlb(client):
    """Fetch MLB schedule and context data"""
    try:
        url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"✅ Fetched MLB schedule with {len(data.get('dates', []))} dates")
        return data
    except Exception as e:
        logger.error(f"❌ Failed to fetch MLB data: {e}")
        raise

def enrich(odds_data, mlb_data):
    """Enrich odds data with MLB context"""
    try:
        # TODO: Replace with your actual enrichment logic
        # This is a placeholder that combines the data
        enriched_props = []
        
        for event in odds_data:
            if "bookmakers" in event:
                for bookmaker in event["bookmakers"]:
                    if "markets" in bookmaker:
                        for market in bookmaker["markets"]:
                            if market["key"] == "player_props":
                                for outcome in market["outcomes"]:
                                    prop = {
                                        "player": outcome.get("description", ""),
                                        "market": market["key"],
                                        "line": outcome.get("point"),
                                        "odds": outcome.get("price"),
                                        "team": event.get("home_team"),
                                        "event_id": event.get("id"),
                                        "commence_time": event.get("commence_time")
                                    }
                                    enriched_props.append(prop)
        
        # Add MLB context
        mlb_context = mlb_data.get("dates", [])
        
        result = {
            "props": enriched_props,
            "mlb_context": mlb_context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": len(enriched_props)
        }
        
        logger.info(f"✅ Enriched {len(enriched_props)} props")
        return result
        
    except Exception as e:
        logger.error(f"❌ Failed to enrich data: {e}")
        raise

# ------------------------------------------------------

async def refresh():
    """Main refresh function - fetches, enriches, and caches data"""
    if not r:
        logger.error("❌ Redis not available")
        return
    
    try:
        logger.info("🔄 Starting data refresh...")
        
        async with httpx.AsyncClient(timeout=20) as client:
            odds_data = await fetch_odds(client)
            mlb_data = await fetch_mlb(client)
            data = enrich(odds_data, mlb_data)

        key = cache_key("mlb")
        r.set(key, json.dumps(data), ex=3600)  # 1 hour TTL
        r.hset("mb:meta", mapping={
            "last_refresh_ts": now_ts(),
            "last_key": key
        })
        
        logger.info(f"[REFRESH] wrote {key}, size={len(json.dumps(data))}")
        logger.info(f"✅ Refresh completed successfully")
        
    except Exception as e:
        logger.error(f"❌ Refresh failed: {e}")
        raise

async def loop(interval=600):
    """Run refresh in a loop with specified interval"""
    logger.info(f"🔄 Starting worker loop with {interval}s interval")
    
    while True:
        try:
            await refresh()
            logger.info(f"💤 Sleeping for {interval}s")
            await asyncio.sleep(interval)
            
        except KeyboardInterrupt:
            logger.info("🛑 Worker loop interrupted")
            break
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
            await asyncio.sleep(60)  # Wait 1 minute before retrying

if __name__ == "__main__":
    asyncio.run(loop()) 