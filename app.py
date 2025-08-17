You are my debugging agent for the Mora Bets backend.  
Task:  
1. Open the function that serves props to the dashboard (where `group_props_by_matchup(enhanced_props)` is called).  
   - Confirm that there is a RELAXED fallback block:  
     * Checks `if not grouped and enhanced_props:`  
     * Builds schedule buckets via `build_game_contexts_for_today()`  
     * Normalizes team codes (`ARZ->ARI`, `WSH->WSN`, etc.)  
     * Assigns props under `AWAY @ HOME` keys with context (favorite/high-scoring preserved).  
   - If this block is missing, insert it exactly as specified.  
   - Ensure the endpoint still returns `grouped` with `context` fields (`favorite`, `total`, `classification`) and `props`.

2. Open `group_props_by_matchup`. Add diagnostics (no logic change):  
   ```python
   logger.debug("[GATE DIAG] matched=%s skipped=%s reasons=%s", matched, skipped, reasons)
   logger.debug("[GATE DIAG] teams_only_in_props=%s teams_only_in_ctx=%s", 
                sorted(teams_only_in_props), sorted(teams_only_in_ctx))
Place this right before the return.

matched, skipped, reasons, teams_only_in_props, teams_only_in_ctx should already exist or be easy to compute from the loop variables.

Confirm environment:

In Render settings, verify SPORT_TZ=America/New_York.

Start command: gunicorn app:app --workers=1 --timeout=120 --bind 0.0.0.0:$PORT.

After redeploy:

Check logs for [GATE DIAG] lines.

Report which reason dominates (key_miss, time_window, or team mismatch).

If teams_only_in_props shows ARZ while teams_only_in_ctx shows ARI, extend normalization map.

If time_window dominates, loosen tolerance from ±60 to ±120 min.

If key_miss dominates, compare the date formats between props and schedule.

Constraints:

Do not remove any UI-critical context (favorite highlighting, high scoring labels, team labels).

Preserve all working endpoints.

The fallback must only run when strict returns 0 matchup
