
from __future__ import annotations
import os, time, json, unicodedata, logging, datetime as _dt
from typing import List, Dict, Any, Optional, Tuple

try:
    import requests
except Exception as _e:  # pragma: no cover
    requests = None

LOG = logging.getLogger("enrichment_v2")

# Optional Redis cache
_r = None
try:
    import redis  # type: ignore
    _r = redis.from_url(os.getenv("REDIS_URL",""), decode_responses=True) if os.getenv("REDIS_URL") else None
except Exception:
    _r = None

def _rget(k: str):
    if not _r: return None
    v = _r.get(k)
    try:
        return json.loads(v) if v else None
    except Exception:
        return None

def _rset(k: str, v: Any, ttl: int = 1800):
    if not _r: return
    try:
        _r.setex(k, ttl, json.dumps(v))
    except Exception:
        pass

STAT_TO_FIELD = {
    "batter_hits": "h", "hits": "h", "h": "h",
    "batter_total_bases": "tb", "total_bases": "tb", "tb": "tb",
    "batter_home_runs": "hr", "home_runs": "hr", "hr": "hr",
    "batter_walks": "bb", "walks": "bb", "bb": "bb",
    "batter_stolen_bases": "sb", "stolen_bases": "sb", "sb": "sb",
    "batter_runs_batted_in": "rbi", "rbi": "rbi",
    "batter_runs": "r", "runs": "r",
}

def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii","ignore").decode("ascii")
    s = s.replace(".", "").replace(",", "")
    for suf in (" Jr", " Jr.", " III", " II"):
        s = s.replace(suf, "")
    return " ".join(s.split())

def _hit_rate_from_series(series: List[float], line: float) -> Dict[str, Any]:
    n = len(series)
    hits = sum(1 for v in series if float(v) >= float(line))
    raw = (hits / n) if n else 0.0
    alpha = 8
    league = 0.50
    smooth = (hits + alpha*league) / (n + alpha) if n else league
    return {
        "hit_rate": round(smooth, 6),
        "hit_rate_raw": round(raw, 6),
        "sample_size": n,
        "successes": hits,
        "confidence": "high" if n >= 12 else "medium" if n >= 6 else "low",
        "threshold": float(line),
    }

UA = {"User-Agent":"morabets/1.0"}
TIMEOUT = (2, 5)

def _http_json(url: str) -> dict:
    if requests is None:
        raise RuntimeError("requests not available")
    t0 = time.time()
    r = requests.get(url, timeout=TIMEOUT, headers=UA)
    dur = time.time() - t0
    ok = r.status_code == 200
    if not ok:
        LOG.warning("mlbapi status=%s dur=%.2fs url=%s", r.status_code, dur, url)
        raise RuntimeError(f"mlbapi status {r.status_code}")
    LOG.info("mlbapi ok dur=%.2fs url=%s bytes=%d", dur, url, len(r.content))
    return r.json()

def _mlb_search_person_id(name: str) -> Optional[int]:
    name = _norm_name(name)
    ck = f"mlb:person_id:{name}"
    v = _rget(ck)
    if isinstance(v, int): return v
    url = f"https://statsapi.mlb.com/api/v1/people/search?names={name.replace(' ','%20')}"
    j = _http_json(url)
    people = j.get("people") or j.get("searchPeople") or []
    if isinstance(people, dict):
        people = people.get("people", [])
    person_id = None
    if isinstance(people, list) and people:
        p = people[0]
        person_id = p.get("id") or p.get("personId") or p.get("peopleId")
    if isinstance(person_id, int):
        _rset(ck, person_id, 86400)
        return person_id
    return None

def _mlb_game_logs(person_id: int, group: str = "hitting", limit: int = 15) -> List[Dict[str, Any]]:
    today = _dt.date.today()
    seasons = [today.year, today.year-1]
    rows: List[Dict[str, Any]] = []
    for season in seasons:
        url = f"https://statsapi.mlb.com/api/v1/people/{person_id}/stats?stats=gameLog&group={group}&season={season}"
        j = _http_json(url)
        try:
            splits = (((j.get("stats") or [{}])[0]).get("splits")) or []
        except Exception:
            splits = []
        for s in splits:
            stat = s.get("stat") or {}
            rows.append({
                "date": s.get("date") or s.get("gameDate"),
                "h": stat.get("hits", 0),
                "tb": stat.get("totalBases", 0),
                "hr": stat.get("homeRuns", 0),
                "bb": stat.get("baseOnBalls", 0),
                "sb": stat.get("stolenBases", 0),
                "rbi": stat.get("rbi", 0),
                "r": stat.get("runs", 0),
            })
    def _key(d):
        try: return _dt.datetime.fromisoformat((d.get("date") or "1970-01-01"))
        except Exception: return _dt.datetime(1970,1,1)
    rows = [r for r in rows if r.get("date")]
    rows.sort(key=_key, reverse=True)
    return rows[:limit]

def _fetch_logs_free(name: str, last_n: int = 10) -> Optional[List[Dict[str, Any]]]:
    pid = _mlb_search_person_id(name)
    if not pid: 
        return None
    ck = f"mlb:logs:{pid}:{last_n}"
    cached = _rget(ck)
    if isinstance(cached, list) and cached:
        return cached[:last_n]
    logs = _mlb_game_logs(pid, "hitting", max(last_n, 15))
    if logs:
        _rset(ck, logs, 1800)
        return logs[:last_n]
    return None

def _ensure_fair_over(p: Dict[str,Any]) -> Optional[float]:
    try:
        v = p.get("fair",{}).get("prob",{}).get("over")
        if isinstance(v,(int,float)): return float(v)
    except Exception: pass
    for k in ("no_vig_prob_over","fair_prob_over","novig_over_prob","market_prob_over"):
        v = p.get(k)
        if isinstance(v,(int,float)): return float(v)
    prices = p.get("prices")
    if isinstance(prices, list):
        over = under = None
        for q in prices:
            o = q.get("over") or q.get("o") or q.get("home") or q.get("over_odds") or q.get("overPrice")
            u = q.get("under") or q.get("u") or q.get("away") or q.get("under_odds") or q.get("underPrice")
            try: over = over if over is not None else (float(o) if o is not None else None)
            except: pass
            try: under = under if under is not None else (float(u) if u is not None else None)
            except: pass
            if over is not None and under is not None: break
        if over is not None and under is not None:
            am = lambda x: 100/(x+100) if x>=0 else (-x)/((-x)+100)
            po, pu = am(over), am(under)
            d = po+pu
            if d>0: return po/d
    return None

def enrich_props_mlb_v2(props: List[Dict[str,Any]]) -> int:
    if not props: return 0
    budget_sec = float(os.getenv("CTX_V2_BUDGET_SEC","2.0"))
    topk       = int(os.getenv("CTX_V2_TOPK","200"))
    max_props  = int(os.getenv("CTX_V2_MAX_PROPS","120"))
    last_n     = int(os.getenv("CTX_V2_LAST_N","10"))

    # ensure fair exists for prioritization
    for p in props:
        fo = _ensure_fair_over(p)
        if fo is not None:
            p.setdefault("fair",{}).setdefault("prob",{})["over"] = round(fo,6)
            p["fair"]["prob"]["under"] = round(1.0-fo,6)

    ranked = sorted(
        props,
        key=lambda x: (x.get("fair",{}).get("prob",{}).get("over") or 0.0),
        reverse=True
    )[:topk]

    t_deadline = time.time() + budget_sec
    memo: Dict[Tuple[str,str,float], Optional[Dict[str,Any]]] = {}
    enriched = 0

    for p in ranked:
        if enriched >= max_props or time.time() > t_deadline:
            break
        if str(p.get("league","")).lower() != "mlb":
            continue
        stat = (p.get("stat") or "").lower()
        if stat not in STAT_TO_FIELD and "batter" not in stat:
            continue

        player = _norm_name(p.get("player",""))
        line   = float(p.get("line",0) or 0.0)

        key = (player, stat, line)
        if key in memo:
            ctx = memo[key]
        else:
            logs = _fetch_logs_free(player, last_n=last_n)
            series = []
            if logs:
                field = STAT_TO_FIELD.get(stat, None)
                if not field and "batter" in stat:
                    field = STAT_TO_FIELD.get(stat.replace("batter_",""), None)
                if field:
                    for g in logs:
                        try:
                            series.append(float(g.get(field, 0) or 0))
                        except Exception:
                            series.append(0.0)
            ctx = _hit_rate_from_series(series, line) if series else None
            memo[key] = ctx

        if ctx:
            p.setdefault("enrichment",{})["mlb_context"] = ctx
            enriched += 1

    return enriched
