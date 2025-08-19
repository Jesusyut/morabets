# Mora Bets Render Migration Guide

## Overview

This migration separates the Mora Bets application into a web-only service and a dedicated worker process for better scalability on Render.

## Architecture Changes

### Before (Monolithic)
- Single Flask app with embedded scheduler
- File-based caching
- Mixed web and background tasks

### After (Distributed)
- **Web Service**: Flask app serving cached data only
- **Worker Process**: Dedicated async worker for data refresh
- **Redis Cache**: Distributed caching with locks
- **Health Checks**: Proper readiness and health endpoints

## New File Structure

```
morabets/
├── app.py              # Web-only Flask app
├── worker.py           # Async worker for data refresh
├── redis_client.py     # Redis connection helper
├── http_client.py      # Shared HTTP client with pooling
├── Procfile           # Render process definitions
├── env.example        # Environment configuration
├── test_worker.py     # Unit tests
└── MIGRATION.md       # This file
```

## Key Components

### 1. Web Service (app.py)
- **Health Endpoint**: `GET /healthz` → `{"ok": true}`
- **Readiness Endpoint**: `GET /readyz` → Checks Redis data freshness
- **Props Endpoint**: `GET /props` → Serves cached props from Redis
- **Magazine Endpoint**: `GET /magazine` → Serves cached magazine from Redis
- **Legacy Support**: Backward-compatible endpoints

### 2. Worker Process (worker.py)
- **Distributed Locks**: Redis-based job locking (TTL: 900s)
- **Connection Pooling**: Shared HTTP client with limits
- **Structured Logging**: Job duration, counts, vendor status
- **Error Handling**: Graceful failures with retry logic

### 3. Redis Integration
- **Key Patterns**:
  - `mb:props:YYYYMMDD` - Daily props data
  - `mb:magazine:latest` - Latest magazine
  - `mb:odds:latest` - Latest odds data
  - `mb:meta:last_refresh` - Job refresh timestamps
  - `lock:mjob:<name>` - Distributed locks

## Render Setup Instructions

### 1. Create Redis Database
1. Go to Render Dashboard
2. Create new **Redis** service
3. Note the connection URL (starts with `rediss://`)

### 2. Create Web Service
1. Create new **Web Service**
2. Connect to your GitHub repository
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `gunicorn app:app -k uvicorn.workers.UvicornWorker -w 3 --timeout 60 --keep-alive 15`

### 3. Create Worker Service
1. Create new **Background Worker**
2. Connect to same GitHub repository
3. **Build Command**: `pip install -r requirements.txt`
4. **Start Command**: `python worker.py --task=all && python worker.py --loop 600`

### 4. Environment Variables
Set these in both services:

```bash
REDIS_URL=rediss://username:password@host:port
ODDS_API_KEY=your_odds_api_key
MLB_API_KEY=your_mlb_api_key
SPORT_TZ=America/New_York
FLASK_ENV=production
```

### 5. Alternative: Cron Job
Instead of background worker, you can use Render Cron Jobs:

```bash
# Every 10 minutes
python worker.py --task=all
```

## API Endpoints

### Health & Readiness
```bash
# Health check
curl https://your-app.onrender.com/healthz
# {"ok": true}

# Readiness check
curl https://your-app.onrender.com/readyz
# {"ready": true, "last_refresh": "2024-01-15T10:30:00Z", "age_minutes": 5.2}
```

### Data Endpoints
```bash
# Get props data
curl https://your-app.onrender.com/props
# {"data": [...], "timestamp": "...", "count": 1322}

# Get magazine
curl https://your-app.onrender.com/magazine
# {"date": "20240115", "picks": [...], "analysis": "..."}
```

### Legacy Endpoints (Backward Compatible)
```bash
# Legacy player props
curl https://your-app.onrender.com/player_props?league=mlb&tz=ET

# Legacy status
curl https://your-app.onrender.com/api/status
```

## Worker Commands

### Manual Execution
```bash
# Run all tasks once
python worker.py --task=all

# Run specific task
python worker.py --task=odds
python worker.py --task=props
python worker.py --task=enrich
python worker.py --task=magazine

# Run in loop (every 10 minutes)
python worker.py --loop 600
```

### Task Flow
1. **refresh_odds** → Fetches game odds from vendor
2. **refresh_props** → Fetches player props from vendor
3. **enrich_mlb** → Processes and enriches props data
4. **publish_magazine** → Generates daily picks magazine

## Monitoring & Debugging

### Logs
- **Web Service**: Request logs, Redis connection status
- **Worker**: Job execution, vendor API responses, lock status

### Health Checks
- **/healthz**: Basic service health
- **/readyz**: Data freshness check (< 15 minutes)

### Redis Monitoring
```bash
# Check last refresh times
redis-cli HGETALL mb:meta:last_refresh

# Check data freshness
redis-cli TTL mb:magazine:latest
redis-cli TTL mb:props:20240115
```

## Testing

### Run Unit Tests
```bash
pip install pytest pytest-asyncio
python test_worker.py
```

### Manual Testing
```bash
# Test worker locally
REDIS_URL=redis://localhost:6379 python worker.py --task=all

# Test web endpoints
curl http://localhost:5000/healthz
curl http://localhost:5000/readyz
```

## Migration Checklist

- [ ] Set up Redis database on Render
- [ ] Create web service with new app.py
- [ ] Create worker service or cron job
- [ ] Configure environment variables
- [ ] Test health and readiness endpoints
- [ ] Verify data flow: Worker → Redis → Web
- [ ] Update frontend URLs if needed
- [ ] Monitor logs for errors
- [ ] Set up alerts for worker failures

## Benefits

1. **Scalability**: Web and worker scale independently
2. **Reliability**: Distributed locks prevent duplicate work
3. **Performance**: Connection pooling and caching
4. **Monitoring**: Proper health checks and structured logs
5. **Maintainability**: Clear separation of concerns

## Troubleshooting

### Common Issues

1. **Worker not running**: Check logs for Redis connection or API key issues
2. **Stale data**: Verify worker is running and check lock status
3. **Redis connection**: Ensure REDIS_URL is correct and accessible
4. **API limits**: Monitor vendor API usage and rate limits

### Debug Commands
```bash
# Check worker status
python worker.py --task=all

# Check Redis data
redis-cli KEYS mb:*

# Check locks
redis-cli KEYS lock:mjob:*
``` 