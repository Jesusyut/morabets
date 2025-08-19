#!/usr/bin/env python3
"""
Mora Bets Worker - Handles scheduled tasks and data refresh
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import argparse

import httpx

from redis_client import redis_client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def now_ts():
    """Get current timestamp"""
    return int(datetime.now(timezone.utc).timestamp())

def today():
    """Get today's date in YYYYMMDD format in UTC"""
    return datetime.now(timezone.utc).strftime("%Y%m%d")

def acquire_lock(name, ttl=900):
    """Acquire a distributed lock"""
    try:
        r = redis_client()
        return r.set(f"lock:{name}", "1", nx=True, ex=ttl)
    except Exception as e:
        logger.error(f"Failed to acquire lock {name}: {e}")
        return False

def release_lock(name):
    """Release a distributed lock"""
    try:
        r = redis_client()
        r.delete(f"lock:{name}")
    except Exception as e:
        logger.error(f"Failed to release lock {name}: {e}")

class MoraBetsWorker:
    def __init__(self):
        try:
            self.r = redis_client()
            logger.info("✅ Redis client initialized")
        except Exception as e:
            logger.error(f"❌ Redis client failed: {e}")
            self.r = None
        
        self.odds_api_key = os.getenv("ODDS_API_KEY")
        self.mlb_api_key = os.getenv("MLB_API_KEY")
        
        if not self.odds_api_key:
            logger.error("ODDS_API_KEY not set")
            sys.exit(1)
    
    async def refresh_odds(self) -> Dict[str, Any]:
        """Refresh odds data from vendor API"""
        job_name = "refresh_odds"
        start_time = time.time()
        
        if not acquire_lock(job_name):
            logger.info(f"⏭️ Skipping {job_name} - lock exists")
            return {"status": "skipped", "reason": "lock_exists"}
        
        try:
            logger.info(f"🔄 Starting {job_name}")
            
            # Fetch odds data from vendor
            async with httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)) as client:
                url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
                params = {
                    "apiKey": self.odds_api_key,
                    "regions": "us",
                    "markets": "h2h,totals",
                    "oddsFormat": "american"
                }
                
                response = await client.get(url, params=params)
                odds_data = response.json()
            
            # Store in Redis
            if self.r:
                self.r.setex(
                    "mb:odds:latest",
                    3600,  # 1 hour TTL
                    json.dumps({
                        "data": odds_data,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "count": len(odds_data)
                    })
                )
                
                # Update metadata
                self.r.hset("mb:meta", "last_refresh_ts", now_ts())
            
            duration = time.time() - start_time
            logger.info(f"✅ {job_name} completed in {duration:.2f}s - {len(odds_data)} events")
            
            return {
                "status": "success",
                "duration": duration,
                "count": len(odds_data),
                "http_status": response.status_code
            }
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ {job_name} failed after {duration:.2f}s: {e}")
            return {
                "status": "error",
                "duration": duration,
                "error": str(e)
            }
        finally:
            release_lock(job_name)
    
    async def refresh_props(self) -> Dict[str, Any]:
        """Refresh player props from vendor API"""
        job_name = "refresh_props"
        start_time = time.time()
        
        if not acquire_lock(job_name):
            logger.info(f"⏭️ Skipping {job_name} - lock exists")
            return {"status": "skipped", "reason": "lock_exists"}
        
        try:
            logger.info(f"🔄 Starting {job_name}")
            
            # Fetch props data from vendor
            async with httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)) as client:
                url = f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
                params = {
                    "apiKey": self.odds_api_key,
                    "regions": "us",
                    "markets": "player_props",
                    "oddsFormat": "american"
                }
                
                response = await client.get(url, params=params)
                props_data = response.json()
            
            # Store in Redis with date-based key (exact format web expects)
            key = f"mb:props:mlb:{today()}"
            if self.r:
                self.r.setex(
                    key,
                    60*60,  # 1 hour TTL
                    json.dumps({
                        "data": props_data,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "count": len(props_data)
                    })
                )
                
                # Update metadata
                self.r.hset("mb:meta", "last_refresh_ts", now_ts())
                self.r.hset("mb:meta", "last_key", key)
            
            duration = time.time() - start_time
            logger.info(f"[REFRESH] wrote {key} size={len(json.dumps(props_data))}")
            logger.info(f"✅ {job_name} completed in {duration:.2f}s - {len(props_data)} props")
            
            return {
                "status": "success",
                "duration": duration,
                "count": len(props_data),
                "http_status": response.status_code
            }
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ {job_name} failed after {duration:.2f}s: {e}")
            return {
                "status": "error",
                "duration": duration,
                "error": str(e)
            }
        finally:
            release_lock(job_name)
    
    async def enrich_mlb(self) -> Dict[str, Any]:
        """Enrich MLB props with additional data"""
        job_name = "enrich_mlb"
        start_time = time.time()
        
        if not acquire_lock(job_name):
            logger.info(f"⏭️ Skipping {job_name} - lock exists")
            return {"status": "skipped", "reason": "lock_exists"}
        
        try:
            logger.info(f"🔄 Starting {job_name}")
            
            # Get props data from Redis
            props_key = f"mb:props:mlb:{today()}"
            
            if not self.r or not self.r.exists(props_key):
                logger.warning(f"No props data found for {today()}")
                return {"status": "skipped", "reason": "no_props_data"}
            
            props_data = json.loads(self.r.get(props_key))
            
            # TODO: Add enrichment logic here
            # This would include probability calculations, edge analysis, etc.
            enriched_data = props_data["data"]  # Placeholder
            
            # Store enriched data
            if self.r:
                self.r.setex(
                    f"mb:enriched:mlb:{today()}",
                    60*60,  # 1 hour TTL
                    json.dumps({
                        "data": enriched_data,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "count": len(enriched_data)
                    })
                )
                
                # Update metadata
                self.r.hset("mb:meta", "last_refresh_ts", now_ts())
            
            duration = time.time() - start_time
            logger.info(f"✅ {job_name} completed in {duration:.2f}s - {len(enriched_data)} enriched")
            
            return {
                "status": "success",
                "duration": duration,
                "count": len(enriched_data)
            }
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ {job_name} failed after {duration:.2f}s: {e}")
            return {
                "status": "error",
                "duration": duration,
                "error": str(e)
            }
        finally:
            release_lock(job_name)
    
    async def publish_magazine(self) -> Dict[str, Any]:
        """Publish the daily magazine with top picks"""
        job_name = "publish_magazine"
        start_time = time.time()
        
        if not acquire_lock(job_name):
            logger.info(f"⏭️ Skipping {job_name} - lock exists")
            return {"status": "skipped", "reason": "lock_exists"}
        
        try:
            logger.info(f"🔄 Starting {job_name}")
            
            # Get enriched data from Redis
            enriched_key = f"mb:enriched:mlb:{today()}"
            
            if not self.r or not self.r.exists(enriched_key):
                logger.warning(f"No enriched data found for {today()}")
                return {"status": "skipped", "reason": "no_enriched_data"}
            
            enriched_data = json.loads(self.r.get(enriched_key))
            
            # TODO: Add magazine generation logic here
            # This would include top picks selection, analysis, etc.
            magazine_data = {
                "date": today(),
                "picks": enriched_data["data"][:10],  # Top 10 picks
                "analysis": "Daily picks analysis...",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            # Store magazine
            if self.r:
                self.r.setex(
                    "mb:magazine:latest",
                    60*60,  # 1 hour TTL
                    json.dumps(magazine_data)
                )
                
                # Update metadata
                self.r.hset("mb:meta", "last_refresh_ts", now_ts())
            
            duration = time.time() - start_time
            logger.info(f"✅ {job_name} completed in {duration:.2f}s")
            
            return {
                "status": "success",
                "duration": duration,
                "picks_count": len(magazine_data["picks"])
            }
            
        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"❌ {job_name} failed after {duration:.2f}s: {e}")
            return {
                "status": "error",
                "duration": duration,
                "error": str(e)
            }
        finally:
            release_lock(job_name)
    
    async def run_all_tasks(self) -> Dict[str, Any]:
        """Run all tasks in sequence"""
        logger.info("🚀 Starting full refresh cycle")
        
        results = {}
        
        # Run tasks in order
        tasks = [
            ("refresh_odds", self.refresh_odds),
            ("refresh_props", self.refresh_props),
            ("enrich_mlb", self.enrich_mlb),
            ("publish_magazine", self.publish_magazine)
        ]
        
        for task_name, task_func in tasks:
            try:
                result = await task_func()
                results[task_name] = result
                
                if result["status"] == "error":
                    logger.error(f"Task {task_name} failed, stopping cycle")
                    break
                    
            except Exception as e:
                logger.error(f"Task {task_name} crashed: {e}")
                results[task_name] = {"status": "crashed", "error": str(e)}
                break
        
        logger.info("🏁 Refresh cycle completed")
        return results
    
    async def run_loop(self, interval: int = 600):
        """Run tasks in a loop with specified interval"""
        logger.info(f"🔄 Starting worker loop with {interval}s interval")
        
        while True:
            try:
                await self.run_all_tasks()
                logger.info(f"💤 Sleeping for {interval}s")
                await asyncio.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("🛑 Worker loop interrupted")
                break
            except Exception as e:
                logger.error(f"Worker loop error: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying

async def main():
    parser = argparse.ArgumentParser(description="Mora Bets Worker")
    parser.add_argument("--task", choices=["all", "odds", "props", "enrich", "magazine"], 
                       default="all", help="Task to run")
    parser.add_argument("--loop", type=int, help="Run in loop with specified interval (seconds)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    
    args = parser.parse_args()
    
    worker = MoraBetsWorker()
    
    try:
        if args.loop:
            await worker.run_loop(args.loop)
        elif args.once:
            await worker.run_all_tasks()
        else:
            if args.task == "all":
                results = await worker.run_all_tasks()
                print(json.dumps(results, indent=2))
            elif args.task == "odds":
                result = await worker.refresh_odds()
                print(json.dumps(result, indent=2))
            elif args.task == "props":
                result = await worker.refresh_props()
                print(json.dumps(result, indent=2))
            elif args.task == "enrich":
                result = await worker.enrich_mlb()
                print(json.dumps(result, indent=2))
            elif args.task == "magazine":
                result = await worker.publish_magazine()
                print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Worker failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main()) 