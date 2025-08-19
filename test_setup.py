#!/usr/bin/env python3
"""
Quick setup test for Mora Bets Redis and API
"""
import os
import json
from datetime import datetime, timezone
from redis_client import redis_client

def test_redis_connection():
    """Test Redis connection and basic operations"""
    print("🔍 Testing Redis connection...")
    
    try:
        r = redis_client()
        print("✅ Redis connection successful")
        
        # Test basic operations
        r.set("test:key", "test_value", ex=60)
        value = r.get("test:key")
        print(f"✅ Basic operations work: {value}")
        
        # Clean up
        r.delete("test:key")
        print("✅ Cleanup successful")
        
        return True
        
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        return False

def test_key_patterns():
    """Test the expected key patterns"""
    print("\n🔍 Testing key patterns...")
    
    try:
        r = redis_client()
        
        # Test today's date
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        print(f"Today (UTC): {today}")
        
        # Test props key
        props_key = f"mb:props:mlb:{today}"
        print(f"Props key: {props_key}")
        
        # Test metadata key
        meta_key = "mb:meta"
        print(f"Meta key: {meta_key}")
        
        # Check if any data exists
        keys = r.keys("mb:*")
        print(f"Existing mb:* keys: {keys}")
        
        if keys:
            # Check metadata
            meta = r.hgetall("mb:meta")
            print(f"Metadata: {meta}")
            
            # Check sample data
            sample_key = keys[0]
            sample_data = r.get(sample_key)
            if sample_data:
                try:
                    parsed = json.loads(sample_data)
                    print(f"Sample data from {sample_key}: {type(parsed)}")
                    if isinstance(parsed, dict):
                        print(f"  Keys: {list(parsed.keys())}")
                        if "count" in parsed:
                            print(f"  Count: {parsed['count']}")
                except:
                    print(f"Sample data is not valid JSON")
        
        return True
        
    except Exception as e:
        print(f"❌ Key pattern test failed: {e}")
        return False

def test_api_endpoints():
    """Test API endpoints (if running locally)"""
    print("\n🔍 Testing API endpoints...")
    
    import requests
    
    base_url = "http://localhost:5000"
    
    endpoints = [
        "/healthz",
        "/readyz", 
        "/debug/state",
        "/props?league=mlb"
    ]
    
    for endpoint in endpoints:
        try:
            url = f"{base_url}{endpoint}"
            response = requests.get(url, timeout=5)
            print(f"✅ {endpoint}: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                print(f"   Response: {type(data)}")
        except requests.exceptions.ConnectionError:
            print(f"⚠️  {endpoint}: Connection refused (app not running)")
        except Exception as e:
            print(f"❌ {endpoint}: {e}")

def main():
    print("🚀 Mora Bets Setup Test")
    print("=" * 50)
    
    # Check environment
    print("🔍 Environment check:")
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        print(f"✅ REDIS_URL: {redis_url[:20]}...")
    else:
        print("❌ REDIS_URL not set")
        return
    
    odds_key = os.getenv("ODDS_API_KEY")
    if odds_key:
        print(f"✅ ODDS_API_KEY: {odds_key[:10]}...")
    else:
        print("❌ ODDS_API_KEY not set")
    
    # Test Redis
    if not test_redis_connection():
        return
    
    # Test key patterns
    if not test_key_patterns():
        return
    
    # Test API (if running)
    test_api_endpoints()
    
    print("\n✅ Setup test completed!")

if __name__ == "__main__":
    main() 