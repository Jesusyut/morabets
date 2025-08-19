#!/usr/bin/env python3
"""
Lightweight unit tests for Mora Bets Worker
"""
import json
import os
import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone

from worker import MoraBetsWorker
from redis_client import create_redis_client

class TestMoraBetsWorker:
    @pytest.fixture
    def worker(self):
        """Create a worker instance with mocked dependencies"""
        with patch.dict(os.environ, {"ODDS_API_KEY": "test_key"}):
            worker = MoraBetsWorker()
            worker.redis_client = Mock()
            return worker
    
    @pytest.mark.asyncio
    async def test_refresh_odds_success(self, worker):
        """Test successful odds refresh"""
        # Mock Redis lock
        worker.redis_client.set.return_value = True
        
        # Mock HTTP response
        mock_response = Mock()
        mock_response.json.return_value = [{"id": "test_game", "odds": "test_odds"}]
        mock_response.status_code = 200
        
        with patch('worker.get_http_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client
            
            result = await worker.refresh_odds()
        
        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["http_status"] == 200
        
        # Verify Redis writes
        worker.redis_client.setex.assert_called()
        worker.redis_client.hset.assert_called()
    
    @pytest.mark.asyncio
    async def test_refresh_odds_lock_exists(self, worker):
        """Test odds refresh when lock exists"""
        # Mock Redis lock failure
        worker.redis_client.set.return_value = False
        
        result = await worker.refresh_odds()
        
        assert result["status"] == "skipped"
        assert result["reason"] == "lock_exists"
    
    @pytest.mark.asyncio
    async def test_refresh_props_success(self, worker):
        """Test successful props refresh"""
        # Mock Redis lock
        worker.redis_client.set.return_value = True
        
        # Mock HTTP response
        mock_response = Mock()
        mock_response.json.return_value = [{"id": "test_prop", "market": "test_market"}]
        mock_response.status_code = 200
        
        with patch('worker.get_http_client') as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client
            
            result = await worker.refresh_props()
        
        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["http_status"] == 200
        
        # Verify Redis writes with date-based key
        worker.redis_client.setex.assert_called()
        call_args = worker.redis_client.setex.call_args[0]
        assert call_args[0].startswith("mb:props:")
    
    @pytest.mark.asyncio
    async def test_enrich_mlb_no_data(self, worker):
        """Test enrichment when no props data exists"""
        # Mock Redis lock
        worker.redis_client.set.return_value = True
        # Mock no props data
        worker.redis_client.exists.return_value = False
        
        result = await worker.enrich_mlb()
        
        assert result["status"] == "skipped"
        assert result["reason"] == "no_props_data"
    
    @pytest.mark.asyncio
    async def test_publish_magazine_success(self, worker):
        """Test successful magazine publishing"""
        # Mock Redis lock
        worker.redis_client.set.return_value = True
        
        # Mock enriched data
        enriched_data = {
            "data": [{"id": f"prop_{i}", "edge": 0.1} for i in range(15)],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        worker.redis_client.exists.return_value = True
        worker.redis_client.get.return_value = json.dumps(enriched_data)
        
        result = await worker.publish_magazine()
        
        assert result["status"] == "success"
        assert result["picks_count"] == 10  # Top 10 picks
        
        # Verify magazine was stored
        worker.redis_client.setex.assert_called()
        call_args = worker.redis_client.setex.call_args[0]
        assert call_args[0] == "mb:magazine:latest"

class TestRedisClient:
    def test_create_redis_client_no_url(self):
        """Test Redis client creation without URL"""
        with patch.dict(os.environ, {}, clear=True):
            client = create_redis_client()
            assert client is None
    
    def test_create_redis_client_with_url(self):
        """Test Redis client creation with URL"""
        with patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379"}):
            with patch('redis.from_url') as mock_from_url:
                mock_client = Mock()
                mock_client.ping.return_value = True
                mock_from_url.return_value = mock_client
                
                client = create_redis_client()
                
                assert client is not None
                mock_from_url.assert_called_once()

if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v"]) 