
## Example app.py integration

```python
from enrichment_v2 import enrich_props_mlb_v2

# ensure fair prob exists for ranking (optional if you already do this)
for p in props:
    try:
        if p.get("fair",{}).get("prob",{}).get("over") is None and callable(_ensure_fair_prob):
            _ensure_fair_prob(p)
    except Exception:
        pass

enriched_count = enrich_props_mlb_v2(props)

# meta to inspect in DevTools (optional)
meta = {"enriched": enriched_count, "league": league, "date": date_str}
payload = {"props": props, "meta": meta, "groups": grouped}
return jsonify(payload)
```
