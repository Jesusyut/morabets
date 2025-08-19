#!/usr/bin/env python3
"""
Simple test for the production worker
"""
import os
import asyncio
from worker import refresh

async def test_worker():
    """Test the worker refresh function"""
    print("🧪 Testing worker refresh...")
    
    # Check environment
    if not os.getenv("REDIS_URL"):
        print("❌ REDIS_URL not set")
        return False
    
    if not os.getenv("ODDS_API_KEY"):
        print("❌ ODDS_API_KEY not set")
        return False
    
    try:
        await refresh()
        print("✅ Worker refresh completed successfully")
        return True
    except Exception as e:
        print(f"❌ Worker refresh failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_worker())
    exit(0 if success else 1) 