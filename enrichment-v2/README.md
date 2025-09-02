
# Morabets Enrichment v2 (MLB)

Clean enrichment path for MLB batter props:
- Rank by fair probability first
- Enrich Top-K only within a time budget
- Use free MLB Stats API (no paid key needed)
- Cache logs in Redis (optional)
- Output: `prop.enrichment.mlb_context`

## Env

- `CTX_V2_TOPK` (default 200)
- `CTX_V2_MAX_PROPS` (default 120)
- `CTX_V2_BUDGET_SEC` (default 2.0)
- `CTX_V2_LAST_N` (default 10)
- `REDIS_URL` (optional)

## Integrate

```python
from enrichment_v2 import enrich_props_mlb_v2

# after you build `props` and (optionally) compute fair probs:
enriched_count = enrich_props_mlb_v2(props)
# continue to AI overlay...
```

