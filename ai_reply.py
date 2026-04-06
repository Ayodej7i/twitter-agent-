"""
AI Reply Generator
==================
Uses Groq (llama-3.3-70b) to write a fresh, context-aware reply to any tweet.

Returns the reply string on success, or None on failure so the caller
can fall back to the template system transparently.

Usage:
    from ai_reply import generate_ai_reply

    reply = generate_ai_reply(tweet_text)
    if reply is None:
        reply = template_fallback(...)   # caller's existing logic
"""

import json
import os
import re
import random
import time
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv
try:
    from ddgs import DDGS as _DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS_AVAILABLE = False

try:
    from utils.safe_io import read_json, write_json
    from utils.bot_logger import get_logger
    _alog = get_logger("ai_reply")
except Exception:
    # Fallback if utils not importable
    import logging as _logging
    _alog = _logging.getLogger("ai_reply")
    def read_json(p, default=None):
        try: return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else (default or {})
        except Exception: return default or {}
    def write_json(p, data, indent=2):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")

load_dotenv()

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_MODEL         = "gpt-4o-mini"    # primary — fast, smart, cost-effective
_MODEL_FALLBACK = "gpt-3.5-turbo" # fallback — cheaper, still solid

# ─────────────────────────────────────────────────────────────────────────────
# Web search — live facts fetched before AI generation
# ─────────────────────────────────────────────────────────────────────────────

_SEARCH_CACHE: dict = {}   # query → (timestamp, result_str)
_CACHE_TTL = 1800          # 30 minutes
_CACHE_MAX_SIZE = 200      # evict oldest entries when exceeded

# ─── Daily stats log ──────────────────────────────────────────────────────────
_LOG_FILE = Path(__file__).parent / "data" / "daily_log.json"

def update_daily_log(key: str, amount: int = 1) -> None:
    """Increment a daily stats counter. Shared across both bot engines."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = read_json(_LOG_FILE, {})
        day = data.setdefault(today, {})
        day[key] = day.get(key, 0) + amount
        write_json(_LOG_FILE, data)
    except Exception:
        pass

# ─── Non-United player guard ──────────────────────────────────────────────────
# Maps lowercase surname/name → current club (March 2026).
# Used to warn the AI not to link these players to United,
# AND to remind it not to spontaneously name other players from the same clubs.
_KNOWN_NON_UNITED_PLAYERS: dict[str, str] = {
    # ── Arsenal ──
    "saka": "Arsenal", "odegaard": "Arsenal", "martinelli": "Arsenal",
    "havertz": "Arsenal", "rice": "Arsenal", "white": "Arsenal",
    "gabriel": "Arsenal", "timber": "Arsenal", "calafiori": "Arsenal",
    "zinchenko": "Arsenal", "trossard": "Arsenal", "nketiah": "Arsenal",
    "jesus": "Arsenal", "merino": "Arsenal", "raya": "Arsenal",
    "eze": "Arsenal", "eberechi eze": "Arsenal",
    "hincapie": "Arsenal", "piero hincapie": "Arsenal",  # £45m deal from Leverkusen
    "declan rice": "Arsenal",  # explicit full-name guard
    "gyokeres": "Arsenal", "viktor gyokeres": "Arsenal",  # striker, started Carabao Cup final
    "madueke": "Arsenal", "noni madueke": "Arsenal",  # winger, sub in Carabao Cup final
    "zubimendi": "Arsenal", "martin zubimendi": "Arsenal",  # midfielder
    "kepa": "Arsenal", "kepa arrizabalaga": "Arsenal",  # GK, howler in Carabao Cup final
    "dowman": "Arsenal", "max dowman": "Arsenal",  # academy prospect, 'freak of nature'
    # ── Liverpool ──
    "salah": "Liverpool", "van dijk": "Liverpool", "nunez": "Liverpool",
    "szoboszlai": "Liverpool", "mac allister": "Liverpool", "gakpo": "Liverpool",
    "diaz": "Liverpool", "robertson": "Liverpool", "konate": "Liverpool",
    "alexander-arnold": "Liverpool", "trent": "Liverpool",
    # ── Man City ──
    "haaland": "Man City", "de bruyne": "Man City", "foden": "Man City",
    "wirtz": "Man City", "bernardo": "Man City", "dias": "Man City",
    "stones": "Man City", "gvardiol": "Man City", "doku": "Man City",
    "kovacic": "Man City", "ederson": "Man City",
    # ── Chelsea ──
    "palmer": "Chelsea", "reece james": "Chelsea", "garnacho": "Chelsea",
    "joao pedro": "Chelsea", "nkunku": "Chelsea", "cucurella": "Chelsea",
    "enzo fernandez": "Chelsea", "mudryk": "Chelsea", "gusto": "Chelsea",
    "colwill": "Chelsea",
    # ── Spurs ──
    "son": "Spurs", "maddison": "Spurs", "richarlison": "Spurs",
    "pedro porro": "Spurs", "kulusevski": "Spurs",
    # ── Newcastle ──
    "isak": "Newcastle", "gordon": "Newcastle", "trippier": "Newcastle",
    "guimaraes": "Newcastle", "bruno guimaraes": "Newcastle",
    # ── Real Madrid ──
    "mbappe": "Real Madrid", "vinicius": "Real Madrid", "bellingham": "Real Madrid",
    "modric": "Real Madrid", "valverde": "Real Madrid", "tchouameni": "Real Madrid",
    "rudiger": "Real Madrid", "courtois": "Real Madrid",
    # ── Barcelona ──
    "yamal": "Barcelona", "lewandowski": "Barcelona", "rashford": "Barcelona",
    "pedri": "Barcelona", "gavi": "Barcelona", "araujo": "Barcelona",
    "raphinha": "Barcelona", "ter stegen": "Barcelona",
    # ── PSG ──
    "barcola": "PSG", "kvaratskhelia": "PSG", "kvara": "PSG",
    "dembele": "PSG", "hakimi": "PSG", "donnarumma": "PSG", "marquinhos": "PSG",
    # ── Bayern Munich ──
    "kane": "Bayern Munich", "musiala": "Bayern Munich", "kimmich": "Bayern Munich",
    "muller": "Bayern Munich", "neuer": "Bayern Munich",
    "sane": "Bayern Munich", "leroy sane": "Bayern Munich",
    # ── Napoli ──
    "mctominay": "Napoli", "osimhen": "Napoli",
    # ── Aston Villa ──
    "sancho": "Aston Villa",
    # ── Marseille ──
    "greenwood": "Marseille",
    # ── Other ──
    "messi": "Inter Miami", "lookman": "Atalanta", "fati": "Brighton",
    "rodriguez": "Rayo Vallecano",  # James Rodriguez — wherever he is now
    "benzema": "Al-Ittihad", "neymar": "Santos",  # returned to Santos
}

def _inject_player_guard(tweet_text: str) -> str:
    """Return a prompt warning if non-United players are detected in the tweet."""
    found = []
    clubs_seen = set()
    t = tweet_text.lower()
    for name, club in _KNOWN_NON_UNITED_PLAYERS.items():
        if name in t:
            found.append(f"{name.title()} ({club})")
            clubs_seen.add(club)
    if not found:
        # Even with no named players detected, remind the AI of the global rule
        return (
            "⚠️ PLAYER NAMING RULE: Do NOT spontaneously name specific players at ANY club "
            "(Arsenal, Liverpool, City, Chelsea, Real Madrid, Barcelona, PSG, etc.) from your "
            "own memory. Rosters change constantly. Only name a player if (a) the tweet "
            "mentions them by name, or (b) LIVE WEB FACTS in this prompt confirm their current club.\n\n"
        )
    clubs_str = ", ".join(sorted(clubs_seen))
    return (
        f"PLAYER CLUB FACTS: {', '.join(found)} — confirmed current clubs as of March 2026.\n"
        f"Do NOT link these players to {FAVORITE_TEAM} unless the tweet explicitly mentions a transfer.\n"
        f"⚠️ CRITICAL: Do NOT spontaneously name OTHER players from {clubs_str} that are not in the tweet "
        f"— their rosters have changed and you WILL get it wrong. Attack the CLUB, not assumed players.\n\n"
    )

# ─── Reply style pools + engagement-weighted selection ────────────────────────
_STYLES_FOOTBALL = [
    "Use a HOT TAKE angle.",
    "Use an OUTSIDER CHALLENGE — as a neutral or United fan, question the claim.",
    "Use a REFRAME — flip the angle completely.",
    "Drop a strong OPINION — no fabricated stats, just a clear take.",
    "Be CONTRARIAN — take the opposite stance confidently.",
]
_STYLES_GENERAL = [
    "Use a QUESTION angle — end with one punchy specific question.",
    "Use the SAY LESS approach — 5 to 10 sharp words max.",
    "Use an OPEN LOOP — leave it unfinished so they have to respond.",
    "Use a RELATABLE angle — say what everyone is thinking.",
]
_STYLES_ALL = _STYLES_FOOTBALL + _STYLES_GENERAL

_STYLE_WEIGHTS_FILE = Path(__file__).parent / "data" / "style_stats.json"
_STYLE_PERF_FILE    = Path(__file__).parent / "data" / "style_performance.json"

# Tracks which style was used in the most recent generate_ai_reply call so
# the caller can associate it with the impression count after posting.
_last_style_used: str = ""

# ─────────────────────────────────────────────────────────────────────────────
# Live United news context — refreshed every 4 hours, injected into football prompts
# ─────────────────────────────────────────────────────────────────────────────
_UNITED_CTX_FILE = Path(__file__).parent / "data" / "united_context.json"
_UNITED_CTX_TTL  = 14_400   # 4 hours

_united_ctx_memo: tuple = (0.0, "")  # (timestamp, context_str)

def get_united_context() -> str:
    """Return cached live Man United context (results/transfers/lineup).
    Fetches fresh data every 4 hours via DuckDuckGo. Returns '' on failure."""
    global _united_ctx_memo
    # In-memory fast path — avoid file read on every call
    if _united_ctx_memo[0] and time.time() - _united_ctx_memo[0] < _UNITED_CTX_TTL:
        return _united_ctx_memo[1]
    cached = read_json(_UNITED_CTX_FILE, {})
    if cached and time.time() - cached.get("ts", 0) < _UNITED_CTX_TTL:
        _united_ctx_memo = (cached["ts"], cached.get("context", ""))
        return _united_ctx_memo[1]
    queries = [
        f"{FAVORITE_TEAM} latest result score 2026",
        f"{FAVORITE_TEAM} next fixture lineup 2026",
        f"{FAVORITE_TEAM} transfer news signing 2026",
    ]
    parts = []
    for q in queries:
        r = web_search(q, max_results=3)
        if r:
            parts.append(r)
        time.sleep(0.3)
    context = "\n---\n".join(parts)
    write_json(_UNITED_CTX_FILE, {"ts": time.time(), "context": context})
    _united_ctx_memo = (time.time(), context)
    if context:
        _alog.info("United context refreshed from web")
    return context


# ── Rival context auto-refresh — fetched on demand, cached 6 hours ───────────
_RIVAL_CTX_FILE = Path(__file__).parent / "data" / "rival_context.json"
_RIVAL_CTX_TTL = 21_600  # 6 hours
_rival_ctx_memo: dict = {}  # rival → (timestamp, context_str)

_RIVAL_SEARCH_QUERIES = {
    "liverpool": ["Liverpool FC latest result 2026", "Liverpool FC news transfers 2026"],
    "city": ["Manchester City latest result 2026", "Man City news Guardiola 2026"],
    "arsenal": ["Arsenal FC latest result 2026", "Arsenal FC news transfers 2026"],
    "chelsea": ["Chelsea FC latest result 2026", "Chelsea FC news manager 2026"],
}


def get_rival_context(rival: str) -> str:
    """Fetch fresh rival news via web search. Cached 6 hours per rival."""
    now = time.time()
    # In-memory fast path
    if rival in _rival_ctx_memo:
        ts, ctx = _rival_ctx_memo[rival]
        if now - ts < _RIVAL_CTX_TTL:
            return ctx
    # File cache
    all_cached = read_json(_RIVAL_CTX_FILE, {})
    if rival in all_cached and now - all_cached[rival].get("ts", 0) < _RIVAL_CTX_TTL:
        ctx = all_cached[rival].get("context", "")
        _rival_ctx_memo[rival] = (all_cached[rival]["ts"], ctx)
        return ctx
    # Fetch fresh
    queries = _RIVAL_SEARCH_QUERIES.get(rival, [])
    if not queries:
        return ""
    parts = []
    for q in queries:
        r = web_search(q, max_results=3)
        if r:
            parts.append(r)
        time.sleep(0.3)
    context = "\n---\n".join(parts)
    all_cached[rival] = {"ts": now, "context": context}
    write_json(_RIVAL_CTX_FILE, all_cached)
    _rival_ctx_memo[rival] = (now, context)
    if context:
        _alog.info(f"Rival context refreshed for {rival}")
    return context


def _load_style_weights() -> dict[str, int]:
    return read_json(_STYLE_WEIGHTS_FILE, {})

def _save_style_weight(style: str) -> None:
    weights = _load_style_weights()
    weights[style] = weights.get(style, 0) + 1
    write_json(_STYLE_WEIGHTS_FILE, weights)

def _load_style_performance() -> dict:
    return read_json(_STYLE_PERF_FILE, {})

def log_style_performance(impressions: int) -> None:
    """Call after posting a non-Japanese reply — records impressions for the
    style that was used, so future replies lean toward what works."""
    global _last_style_used
    if not _last_style_used or impressions <= 0:
        return
    perf = _load_style_performance()
    entry = perf.setdefault(_last_style_used, {"samples": [], "avg": 0})
    entry["samples"].append(impressions)
    entry["samples"] = entry["samples"][-50:]   # keep last 50
    entry["avg"]     = int(sum(entry["samples"]) / len(entry["samples"]))
    write_json(_STYLE_PERF_FILE, perf)

def _get_performance_prompt_hint() -> str:
    """Return a short performance note to inject into the non-Japanese prompt.
    Only fires when ≥5 data points exist for the best style."""
    perf = _load_style_performance()
    if not perf:
        return ""
    valid = {s: d for s, d in perf.items() if len(d.get("samples", [])) >= 5}
    if not valid:
        return ""
    best  = max(valid.items(), key=lambda x: x[1]["avg"])
    worst = min(valid.items(), key=lambda x: x[1]["avg"])
    if best[1]["avg"] <= worst[1]["avg"]:
        return ""
    ratio = best[1]["avg"] / max(worst[1]["avg"], 1)
    # Strip the 'Use a/an/the ...' wrapper for a readable label
    label = re.sub(r'^Use (a|an|the) ', '', best[0], flags=re.I)
    label = re.sub(r'\s*[—\-].*$', '', label).rstrip('. ').lower()
    return (
        f"PERFORMANCE NOTE: Your \"{ label }\" replies are averaging "
        f"{best[1]['avg']:,} impressions — {ratio:.1f}x higher than "
        f"your least effective style. Lean toward this angle when the tweet fits naturally."
    )

def _pick_style(style_category: str = "") -> str:
    """Pick a reply style, jointly weighted by performance (impressions) and
    variety (inverse recent usage) — so high-impression styles win but we
    don't repeat the exact same approach forever."""
    pool = (
        _STYLES_FOOTBALL if style_category == "football"
        else _STYLES_GENERAL if style_category == "general"
        else _STYLES_ALL
    )
    usage = _load_style_weights()
    perf  = _load_style_performance()
    recent = _load_recent_angles()
    def _score(s: str) -> float:
        u   = 1 + usage.get(s, 0)
        avg = perf.get(s, {}).get("avg", 0)
        base = (1 + avg / 10_000) / u
        # Heavy penalty for angles used in the last 5 replies globally (item 2)
        recency_hits = recent.count(s)
        return base / (1 + recency_hits * 5)
    weights = [_score(s) for s in pool]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    _push_recent_angle(chosen)
    return chosen


# ─── Global angle ring-buffer (item 2) ───────────────────────────────────────
# Tracks last 5 styles used across ALL accounts to prevent identical approaches
# firing back-to-back across different reply targets.
_RECENT_ANGLES_FILE = Path(__file__).parent / "data" / "recent_angles.json"

def _load_recent_angles() -> list:
    return read_json(_RECENT_ANGLES_FILE, [])

def _push_recent_angle(style: str):
    angles = _load_recent_angles()
    angles.append(style)
    angles = angles[-5:]   # keep last 5 globally
    write_json(_RECENT_ANGLES_FILE, angles, indent=0)


# ─── Self-review helper (item 4) ─────────────────────────────────────────────
def _review_reply(reply_text: str, tweet_text: str) -> int:
    """Ask GPT-4o-mini to rate the reply 1-10 for controversy/engagement. Returns 1-10 (default 7 on failure)."""
    if not _OPENAI_API_KEY:
        return 7
    try:
        resp = requests.post(
            _OPENAI_URL,
            headers={"Authorization": f"Bearer {_OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": "You rate Twitter replies by controversy and engagement potential. Be strict."},
                    {"role": "user", "content": (
                        f"Original tweet: \"{tweet_text[:200]}\"\n\n"
                        f"Reply: \"{reply_text}\"\n\n"
                        "Score this reply 1-10 for controversy and engagement potential on Twitter/X. "
                        "Reply with ONLY valid JSON: {\"score\": N} where N is 1-10. "
                        "Most replies are 5-7. Score 8-10 only for genuine hot takes. Score 1-4 for safe/bland/agreeable."
                    )},
                ],
                "max_tokens": 15,
                "temperature": 0.1,
            },
            timeout=8,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        return int(json.loads(content).get("score", 7))
    except Exception:
        return 7


# Keywords that suggest the tweet contains checkable football/sports facts
_FACTUAL_TRIGGERS = [
    "manager", "coach", "sacked", "hired", "appointed", "resign",
    "transfer", "signing", "deal", "loan", "bid", "sold", "bought", "fee",
    "goal", "scored", "hat-trick", "assist", "match", "game", "result",
    "won", "lost", "draw", "beat", "defeat", "score",
    "performance", "performed", "poor", "bad", "struggled", "rating",
    "injury", "injured", "return", "ban", "suspended", "contract",
    "acl", "anterior cruciate", "mcl", "hamstring", "surgery",
    "breaking", "report", "confirmed", "official", "sources say",
    "chelsea", "arsenal", "liverpool", "man city", "spurs", "tottenham",
    "newcastle", "real madrid", "barcelona",
    "psg", "juventus", "milan", "inter", "atletico", "dortmund", "bayern",
    "premier league", "la liga", "serie a", "bundesliga", "champions league",
]

_CLUB_MAP = {
    "chelsea": "Chelsea FC",
    "arsenal": "Arsenal FC",
    "liverpool": "Liverpool FC",
    "man city": "Manchester City",
    "spurs": "Tottenham Hotspur",
    "tottenham": "Tottenham Hotspur",
    "newcastle": "Newcastle United",
    # Add your team abbreviations below:
    # "man utd": "Manchester United",
    "real madrid": "Real Madrid",
    "barcelona": "FC Barcelona",
    "psg": "PSG",
    "juventus": "Juventus",
    "milan": "AC Milan",
    "inter": "Inter Milan",
    "atletico": "Atletico Madrid",
    "dortmund": "Borussia Dortmund",
    "bayern": "Bayern Munich",
}

_PLAYER_NAME_NORMALISATIONS = {
    "sane": "Leroy Sane",
    "leroy sane": "Leroy Sane",
}

_PLAYER_PERFORMANCE_MARKERS = [
    "didn't perform well", "did not perform well", "not perform well",
    "poor performance", "poor performances", "bad game", "bad match",
    "struggled", "underperformed", "underperform", "worst game",
    "worst match", "match rating", "match ratings",
]


def _normalise_player_search_subject(subject: str) -> str:
    """Convert a raw player mention into a search-friendly subject."""
    cleaned = re.sub(r"[^a-zA-Z\s\-']", " ", subject.lower()).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return ""
    if cleaned in _PLAYER_NAME_NORMALISATIONS:
        return _PLAYER_NAME_NORMALISATIONS[cleaned]
    return " ".join(part.capitalize() for part in cleaned.split())


def _extract_player_search_subject(tweet_text: str) -> str:
    """Extract a likely player name from a research-style prompt."""
    t = tweet_text.lower()
    patterns = [
        r"(?:games?|matches?)\s+(?:that\s+)?([a-z][a-z\s\-.']{1,50}?)\s+played\b",
        r"\bhow did\s+([a-z][a-z\s\-.']{1,50}?)\s+play\b",
        r"\b(?:about|on)\s+([a-z][a-z\s\-.']{1,50}?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, t)
        if match:
            subject = _normalise_player_search_subject(match.group(1))
            if subject:
                return subject
    for alias in sorted(_KNOWN_NON_UNITED_PLAYERS, key=len, reverse=True):
        if alias in t:
            subject = _normalise_player_search_subject(alias)
            if subject:
                return subject
    return ""


def _is_player_performance_request(tweet_text: str) -> bool:
    """Detect prompts asking for examples of a player's poor performances."""
    t = tweet_text.lower()
    if not any(word in t for word in ["game", "games", "match", "matches"]):
        return False
    return any(marker in t for marker in _PLAYER_PERFORMANCE_MARKERS)


def _find_clubs_in_text(tweet_text: str) -> list[str]:
    """Find club keywords without matching unrelated substrings like 'internet'."""
    t = tweet_text.lower()
    found = []
    for key, club in _CLUB_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", t):
            found.append(club)
    return found


def web_search(query: str, max_results: int = 4) -> str:
    """Search DuckDuckGo and return a compact summary. Returns '' on any failure."""
    if not _DDGS_AVAILABLE:
        return ""
    now = time.time()
    if query in _SEARCH_CACHE:
        ts, cached = _SEARCH_CACHE[query]
        if now - ts < _CACHE_TTL:
            return cached
    try:
        results = list(_DDGS().text(query, max_results=max_results, timelimit="m"))
        if not results:
            return ""
        snippets = [f"- {r.get('title', '')}: {r.get('body', '')[:160]}" for r in results]
        summary = "\n".join(snippets)
        _SEARCH_CACHE[query] = (now, summary)
        # Evict oldest entries if cache is too large
        if len(_SEARCH_CACHE) > _CACHE_MAX_SIZE:
            oldest_key = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
            _SEARCH_CACHE.pop(oldest_key, None)
        return summary
    except Exception:
        return ""


def _extract_headline_fact(results_str: str) -> str:
    """
    Pull the single most direct fact from web results.
    Prefers lines that mention 2025/2026 in the body content (not just update timestamps).
    Returns a short 'KEY FACT: ...' line or ''.
    """
    import re
    appointment_phrases = [
        "joined", "appointed", "is the manager", "is the head coach",
        "named as manager", "named as head coach", "confirmed as",
        "signs contract", "signed a contract", "new head coach", "new manager",
    ]
    recency_markers = ["2026", "2025"]

    for line in results_str.splitlines():
        lower = line.lower()
        if not any(phrase in lower for phrase in appointment_phrases):
            continue
        # Strip "- Title: " prefix to get just the body
        clean = re.sub(r'^-\s*[^:]+:\s*', '', line).strip()
        if not clean:
            continue
        # Recency must appear within the first 120 chars of the BODY (not just a timestamp)
        body_start = clean[:120].lower()
        if any(m in body_start for m in recency_markers):
            return f"KEY FACT: {clean[:200]}"

    return ""


def _needs_web_search(tweet_text: str) -> bool:
    t = tweet_text.lower()
    return any(kw in t for kw in _FACTUAL_TRIGGERS)


# Keywords that indicate a tweet is about your team — customise these
_UNITED_TOPIC_KEYWORDS = [
    # Add your team name variants and key player names here. Example:
    # "manchester united", "man utd", "man united", "mufc",
    # "old trafford", "bruno fernandes", "mainoo",
]

def _is_united_topic(tweet_text: str, parent_tweet: str = "", quoted_tweet: str = "") -> bool:
    """Return True only if the combined tweet text is about the configured team."""
    combined = f"{tweet_text} {parent_tweet} {quoted_tweet}".lower()
    return any(kw in combined for kw in _UNITED_TOPIC_KEYWORDS)


def _build_search_query(tweet_text: str) -> str:
    """Build a focused search query from a tweet."""
    t = tweet_text.lower()
    clubs_found = _find_clubs_in_text(tweet_text)
    if _is_player_performance_request(tweet_text):
        subject = _extract_player_search_subject(tweet_text)
        query_parts = []
        if subject:
            query_parts.append(subject)
        if clubs_found:
            query_parts.append(clubs_found[0])
        query_parts.extend(["poor performances", "match ratings", "2025", "2026"])
        query = " ".join(part for part in query_parts if part).strip()
        if query:
            return query
    # Use 'current manager' instead of 'coach' for cleaner results
    manager_kw = "current manager" if any(w in t for w in ["manager", "coach", "sacked", "hired", "appointed"]) else ""
    actions = ["current manager" if manager_kw else w
               for w in ["transfer", "signing", "injury", "result", "sacked"]
               if w in t and not manager_kw]
    if clubs_found:
        query = " ".join(clubs_found[:2])
        if manager_kw:
            query += " " + manager_kw
        elif actions:
            query += " " + " ".join(actions[:1])
        query += " 2026"
    else:
        query = tweet_text[:70].strip() + " 2026"
    return query


# ─────────────────────────────────────────────────────────────────────────────
# (Source 2 / DDGS live match detection removed — using @ChampionsLeague feed only)
# ─────────────────────────────────────────────────────────────────────────────

def _REMOVED_parse_match_from_search(results_str: str) -> dict | None:
    """
    Try to extract team names + score from ddgs search results.
    Returns dict(home, away, score, raw) or None.
    """
    # Pattern: "Team1 2-1 Team2" or "Team1 2 - 1 Team2"
    score_re = re.compile(
        r'([A-Z][a-zA-Z\s\u00C0-\u017E]{2,25}?)\s+(\d+)\s*[-–]\s*(\d+)\s+([A-Z][a-zA-Z\s\u00C0-\u017E]{2,25})',
        re.MULTILINE,
    )
    # Pattern: "Team1 vs Team2" with CL context nearby
    vs_re = re.compile(
        r'([A-Z][a-zA-Z\s\u00C0-\u017E]{2,20}?)\s+(?:vs?\.?|v\.?|versus)\s+([A-Z][a-zA-Z\s\u00C0-\u017E]{2,20})',
        re.IGNORECASE,
    )
    # Keywords that confirm a match is happening RIGHT NOW (not an old result/preview)
    live_now_signals = ['live', 'in progress', 'now', 'tonight', 'ongoing',
                        'kick off', 'kick-off', 'underway', 'first half', 'second half',
                        'half time', 'halftime', 'matchday', "'", ' min ']
    cl_keywords = ['champions league', 'ucl', 'quarter', 'semi', 'round of 16', 'knockout']

    full_lower = results_str.lower()
    has_live_signal = any(kw in full_lower for kw in live_now_signals)
    has_cl_signal = any(kw in full_lower for kw in cl_keywords)

    # Hard requirement: results must show both CL context AND a live-right-now signal
    if not (has_live_signal and has_cl_signal):
        return None

    for line in results_str.splitlines():
        m = score_re.search(line)
        if m:
            home = m.group(1).strip().rstrip(' -')
            away = m.group(4).strip().lstrip('- ')
            # Sanity check: team names shouldn't be single words like pronouns/articles
            if len(home) >= 3 and len(away) >= 3 and home.lower() not in ('the', 'and', 'for'):
                return {
                    'home': home,
                    'away': away,
                    'score': f"{m.group(2)}-{m.group(3)}",
                    'raw': line[:250],
                }

    # Only return a scoreless match if the line itself contains a live signal
    for line in results_str.splitlines():
        low = line.lower()
        has_line_live = any(kw in low for kw in live_now_signals)
        has_line_cl = any(kw in low for kw in cl_keywords)
        if has_line_live and has_line_cl:
            m = vs_re.search(line)
            if m:
                home = m.group(1).strip()
                away = m.group(2).strip()
                if len(home) >= 3 and len(away) >= 3:
                    return {'home': home, 'away': away, 'score': '', 'raw': line[:250]}

    return None





# ─────────────────────────────────────────────────────────────────────────────
# System prompt — persona + season knowledge + voice rules
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Persona config — all identity, prompts, and banter loaded from config/persona.py
# Edit config/persona.py to customise the bot's personality and team affiliation.
# ─────────────────────────────────────────────────────────────────────────────
from config.persona import (
    BOT_HANDLE,
    FAVORITE_TEAM,
    NATIONALITY,
    SYSTEM_PROMPT,
    POST_SYSTEM_PROMPT,
    RIVAL_BANTER_STANCES,
    RIVAL_NEWS_MARKERS as _RIVAL_NEWS_MARKERS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Rival news blocks — markers for conditional injection from persona prompts
# These map rival keywords to start/end markers in SYSTEM_PROMPT so the bot
# only injects relevant rival news into the prompt (saves tokens).
# Configure these in config/persona.py → RIVAL_NEWS_MARKERS
# ─────────────────────────────────────────────────────────────────────────────

# Start/end markers — customise these to match your SYSTEM_PROMPT content
_RIVAL_NEWS_START: dict[str, str] = {}  # e.g. {"liverpool": "  - Liverpool (Slot)"}
_RIVAL_NEWS_END: dict[str, str] = {}    # e.g. {"liverpool": "  - Man City (Pep)"}


_BANNED_OPENERS = re.compile(
    r"^(let'?s be real[,\s\-\u2013\u2014]*|let me be real[,\s\-\u2013\u2014]*|"
    r"let me be honest[,\s\-\u2013\u2014]*|to be honest[,\s\-\u2013\u2014]*|"
    r"honestly[,\s\-\u2013\u2014]+|i have to say[,\s\-\u2013\u2014]*|"
    r"i'?ve got to say[,\s\-\u2013\u2014]*|look[,\s]+i |"
    r"at the end of the day[,\s\-\u2013\u2014]*|the truth is[,\s\-\u2013\u2014]*|"
    r"real talk[,\s\-\u2013\u2014]*|real talk though[,\s\-\u2013\u2014]*)",
    re.IGNORECASE,
)

def _strip_banned_openers(reply: str) -> str:
    """Strip lazy/banned openers from the start of a reply, keeping the meat."""
    cleaned = _BANNED_OPENERS.sub("", reply).strip().lstrip(",\u2014\u2013-").strip()
    if cleaned and cleaned[0].islower():
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned if cleaned else reply


def _reply_has_identity_error(reply: str) -> bool:
    """
    Return True if the reply appears to speak as a fan of a non-United club.
    These phrases should never appear in our replies.
    """
    r = reply.lower()
    forbidden = [
        "our blues", "our reds", "our gunners", "our cityzens", "our citizens",
        "our toon", "our magpies", "our saints", "our hammers", "our spurs",
        "our lilywhites", "our foxes", "our villans", "our wolves",
        "our side won", "our side lost", "our side beat",
        # Injecting United into non-United topics
        f"could help {FAVORITE_TEAM.lower()}", f"would help {FAVORITE_TEAM.lower()}", "help united turn",
        "united could use", "united could do with", "united need someone like",
        "perfect for united", "fit perfectly at united", "would suit united",
        "ideal for united", "united should sign", "united must sign",
        "imagine him at united", "imagine her at united",
        "imagine him at old trafford", "imagine her at old trafford",
        "at old trafford",  # non-United player being placed at OT
    ]
    return any(phrase in r for phrase in forbidden)


# ─────────────────────────────────────────────────────────────────────────────
# Rival fan banter stances — injected into prompt when replying to a known
# rival fan account. Tells the AI to root against their club.
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Stats / result verification — catches AI hallucinating scores before posting
# ─────────────────────────────────────────────────────────────────────────────
_STAT_RE = re.compile(
    r'\b\d+\s*[-–]\s*\d+\b'       # scores: 2-1, 3-0
    r'|\b\d+\s+goals?\b'           # "3 goals"
    r'|\b\d+\s+assists?\b'         # "7 assists"
    r'|\bscored\s+\d+\b'           # "scored 9"
    r'|\b\d+\s+(?:points?|pts)\b', # "54 points"
    re.IGNORECASE,
)

def _has_stat_claim(text: str) -> bool:
    """Return True if text contains a specific numerical football claim."""
    return bool(_STAT_RE.search(text))


def verify_stat_reply(reply: str, facts_context: str = "") -> str:
    """Verify a reply containing stats against known facts via a quick GPT call.
    Returns the original reply (or a corrected version) — never blocks on failure."""
    if not _has_stat_claim(reply) or not facts_context or not _OPENAI_API_KEY:
        return reply
    prompt = (
        f"A bot is about to post this Twitter reply:\n\"{reply}\"\n\n"
        f"KNOWN VERIFIED FACTS:\n{facts_context[:1000]}\n\n"
        f"Does this reply contain specific football stats, scores, or factual claims?\n"
        f"- If YES and they match the facts (or can't be checked here): reply PASS\n"
        f"- If YES and they clearly contradict the facts: reply FIX: [rewrite same tone "
        f"but replace the wrong stat with a strong opinion instead, max 275 chars]\n"
        f"- If NO stats: reply PASS\n"
        f"Output ONLY 'PASS' or 'FIX: [text]'"
    )
    try:
        resp = requests.post(
            _OPENAI_URL,
            headers={"Authorization": f"Bearer {_OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 100,
                "temperature": 0,
            },
            timeout=12,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"].strip()
        if result.upper().startswith("PASS"):
            return reply
        if result.upper().startswith("FIX:"):
            fixed = result[4:].strip().strip('"\'')
            if 10 < len(fixed) <= 280:
                print(f"  ✅ Stat verified — reply corrected")
                return fixed
    except Exception:
        pass
    return reply
def generate_ai_reply(
    tweet_text: str,
    context: str = "",
    account: str = "",
    style_category: str = "",
    avoid_openers: list = None,
    avoid_phrases: list = None,
    parent_tweet: str = "",
    quoted_tweet: str = "",
    language: str = "",
    rival_fan: str = "",
    is_united_fan: bool = False,
    is_neutral_elite: bool = False,
    is_meme_banter: bool = False,
    image_urls: list = None,
    top_replies: str = "",
    account_profile: str = "",  # item 1 — per-account memory hint
) -> str | None:
    """
    Generate a fresh AI reply to tweet_text.

    Args:
        tweet_text:     The tweet being replied to.
        context:        Optional extra context (topic facts or our original tweet).
        account:        Optional handle of the account being replied to.
        style_category: 'football' | 'general' | '' (all styles).
        avoid_openers:  First words used recently — bot avoids them for variety.
        parent_tweet:   Parent tweet text when replying inside a thread.
        quoted_tweet:   Embedded quoted tweet text (for quote tweets).
        rival_fan:      Rival club the account supports (e.g. 'arsenal', 'liverpool').
                        When set, rival banter stance is injected into the prompt.

    Returns:
        A reply string under 275 chars, or None if AI is unavailable / fails.
    """
    if not tweet_text or not tweet_text.strip():
        return None

    # Japanese mode — use Japanese reply system prompt, skip player guard / style pool
    is_japanese = language.strip().lower() == "ja"

    # Pick style using impression-weighted + inverse-frequency scoring
    style_hint = _pick_style(style_category)
    # Store for caller to associate with impression count after posting
    global _last_style_used
    _last_style_used = style_hint if not is_japanese else ""

    # Live web search — fetch current facts before building the prompt
    live_ctx = ""
    if _needs_web_search(tweet_text):
        query = _build_search_query(tweet_text)
        live_ctx = web_search(query)
        if live_ctx:
            print(f"  🌐 Web context fetched for: {query[:60]}")

    # United live context — only inject when tweet is actually about United
    # (or the account is a confirmed United fan). Never inject into neutral conversations.
    united_ctx = ""
    if style_category == "football" and not is_japanese:
        if is_united_fan or _is_united_topic(tweet_text, parent_tweet, quoted_tweet):
            united_ctx = get_united_context()
            if united_ctx:
                print(f"  📰 United context attached")

    # Rival live context — fetch fresh news for rivals mentioned in the tweet
    rival_ctx = ""
    if style_category == "football" and not is_japanese:
        mentioned_rivals = _detect_rivals_in_text(tweet_text)
        for rival in mentioned_rivals:
            ctx = get_rival_context(rival)
            if ctx:
                rival_ctx += f"\n{rival.upper()} LATEST NEWS:\n{ctx}\n"
        if rival_ctx:
            print(f"  📰 Rival context attached for: {', '.join(mentioned_rivals)}")

    player_guard = "" if is_japanese else _inject_player_guard(tweet_text)

    prompt = tweet_text.strip()
    live_header = ""
    if live_ctx or united_ctx or rival_ctx:
        headline = _extract_headline_fact(live_ctx) if live_ctx else ""
        united_block = (
            f"\nUNITED LIVE NEWS (supplementary — do not contradict VERIFIED RESULTS in system prompt):\n"
            f"{united_ctx[:500]}\n"
        ) if united_ctx else ""
        rival_block = f"\nRIVAL LIVE NEWS (use to update your banter — more recent than system prompt):\n{rival_ctx[:800]}\n" if rival_ctx else ""
        live_header = (
            f"LIVE WEB FACTS — these are more accurate than your training data, use them:\n"
            f"{headline + chr(10) if headline else ''}"
            f"{live_ctx}\n"
            f"{united_block}"
            f"{rival_block}"
            f"INSTRUCTION: If the tweet's claim contradicts the above facts, correct it in your reply.\n\n"
        )
    if (live_ctx or united_ctx or rival_ctx) and context and context.startswith("Context:"):
        prompt = f"{live_header}{context}\n\nTweet to reply to: {tweet_text}\n\n{style_hint}"
    elif live_ctx or united_ctx or rival_ctx:
        prompt = f"{live_header}Tweet to reply to: {tweet_text}\n\n{style_hint}"
    elif context and context.startswith("Context:"):
        # Topic context facts injected from the fast replier's topic detection
        prompt = f"{context}\n\nTweet to reply to: {tweet_text}\n\n{style_hint}"
    elif context:
        # Our original tweet that they replied to
        prompt = f"[Context — our tweet they replied to: {context}]\n\nTheir tweet: {tweet_text}\n\n{style_hint}"
    elif account:
        prompt = f"[Posted by {account}]\n\n{tweet_text}\n\n{style_hint}"
    else:
        prompt = f"{tweet_text}\n\n{style_hint}"

    # Performance hint — inject what styles are working, for non-Japanese replies only
    if not is_japanese:
        perf_hint = _get_performance_prompt_hint()
        if perf_hint:
            prompt += f"\n\n{perf_hint}"
        if _is_player_performance_request(tweet_text):
            prompt += (
                "\n\nRESEARCH TASK: If they are asking for games where a player performed poorly, "
                "answer directly with only the specific matches you can support from the LIVE WEB FACTS. "
                "If the web facts are too thin, say you can't verify exact games rather than guessing."
            )

    # Thread awareness — inject parent tweet context for thread replies
    if parent_tweet:
        prompt = f"[Thread context — tweet being replied to: \"{parent_tweet[:200]}\"]\n\n" + prompt

    # Quote tweet awareness — inject the embedded quoted tweet so AI understands full context
    if quoted_tweet:
        prompt = f"[QUOTED TWEET — they are quoting this post: \"{quoted_tweet[:200]}\"]\n\n" + prompt

    # Player guard — warn AI not to link non-United players to United
    if player_guard:
        prompt = player_guard + prompt

    # Rival fan banter — inject stance when replying to a known rival fan account
    if rival_fan and rival_fan.lower() in RIVAL_BANTER_STANCES and not is_japanese:
        banter_note = RIVAL_BANTER_STANCES[rival_fan.lower()]
        prompt = banter_note + "\n\n" + prompt
        print(f"  ⚔️  Rival banter mode: {rival_fan}")

    # Teammate fan mode — agree & build on their take, never contradict
    if is_united_fan and not is_japanese:
        teammate_note = (
            f"🤝 TEAMMATE MODE — THIS IS A TEAMMATE ACCOUNT: They support {FAVORITE_TEAM} like you do.\n"
            "✅ AGREE with their take first, then BUILD ON it. Do NOT contradict or challenge their praise.\n"
            "✅ If they praise a player — CO-SIGN it, add hype, stack with them.\n"
            "✅ Exception: if debate is about a player's recent poor form, nuanced takes are OK — but stay broadly supportive.\n"
            "✅ If they criticize a rival — PILE ON. You're on the same side.\n"
            "❌ FORBIDDEN: Playing devil's advocate against a teammate's positive take.\n"
            "❌ FORBIDDEN: 'He's been good but let's not get carried away' — sound like a teammate, not a debate opponent.\n"
        )
        prompt = teammate_note + "\n\n" + prompt
        print(f"  🤝 Teammate mode: {account}")

    # Neutral elite mode — balanced analyst, elite & factual, no bias
    if is_neutral_elite and not is_japanese:
        neutral_note = (
            "📊 NEUTRAL ELITE MODE — THIS IS A NEUTRAL/ANALYST ACCOUNT: Not a United fan, not a rival fan.\n"
            "✅ Reply with ELITE, FACTUAL, ANALYTICAL takes. Sharp insight, not tribal bias.\n"
            "✅ Bring stats, context, tactical observations or sharp opinions backed by evidence.\n"
            "✅ You can still have a clear view — but it must be grounded in fact, not pure fandom.\n"
            "✅ Engage the debate on its merits. Add the angle nobody else in the replies is making.\n"
            "❌ FORBIDDEN: Blind United hype or cheerleading with no factual basis.\n"
            "❌ FORBIDDEN: Dismissing rival clubs purely out of bias — make the point analytically.\n"
            "❌ FORBIDDEN: Generic fan takes. Every reply must feel like a knowledgeable pundit speaking.\n"
        )
        prompt = neutral_note + "\n\n" + prompt
        print(f"  📊 Neutral elite mode: {account}")

    # Meme banter mode — match troll/meme account energy with jokes & banter
    if is_meme_banter and not is_japanese:
        banter_note = (
            "😂 MEME BANTER MODE — THIS IS A TROLL/MEME FOOTBALL ACCOUNT:\n"
            "✅ Reply with JOKES, MEMES, CLUB BANTER, TROLLING. Keep it fun and playful.\n"
            "✅ Match their energy — if they're clowning a club, pile on with a funnier take.\n"
            "✅ Short punchy replies that hit hard. One-liners, ratio material, quote-tweet energy.\n"
            "✅ Use football culture references, running jokes, 'finished club', 'banter era', etc.\n"
            "✅ You can use 😂💀🤣🔥 emojis — this is pure entertainment mode.\n"
            "✅ Troll rival clubs freely (Arsenal, Liverpool, Chelsea, City). Hype United wins.\n"
            "❌ FORBIDDEN: Serious analytical replies. This is not the place for stats or punditry.\n"
            "❌ FORBIDDEN: Long paragraphs. Keep it under 2 sentences max.\n"
            "❌ FORBIDDEN: Being boring or generic. Every reply must be funny or savage.\n"
        )
        prompt = banter_note + "\n\n" + prompt
        print(f"  😂 Meme banter mode: {account}")

    # Per-account memory (item 1) — personalise reply with inferred profile
    if account_profile and not is_japanese:
        prompt = account_profile + "\n\n" + prompt
        print(f"  🧠 Account profile injected")

    # Anti-repetition — avoid recently-used first words and phrases
    if avoid_openers:
        avoid_str = ", ".join(f'"{w}"' for w in avoid_openers[:10])
        prompt += f"\n\nDO NOT start your reply with any of these words (recently used): {avoid_str}"
    if avoid_phrases:
        phrase_str = " | ".join(f'"{p}"' for p in avoid_phrases[-10:])
        prompt += f"\nDO NOT use replies that start similarly to any of these recent replies: {phrase_str}"

    # Top existing replies context — helps AI write something that stands out
    if top_replies and not is_japanese:
        prompt += (
            f"\n\nEXISTING REPLIES already posted by others (do NOT copy or echo these — "
            f"be more specific, more insightful, or take a different angle):\n{top_replies}"
        )
        print(f"  💬 Top replies injected into prompt")

    if not _OPENAI_API_KEY:
        return None  # No API key configured — silent fallback

    is_united = is_united_fan or _is_united_topic(tweet_text, parent_tweet, quoted_tweet)
    active_system_prompt = SYSTEM_PROMPT_JA_REPLY if is_japanese else build_system_prompt(tweet_text, is_united_topic=is_united)

    # Vision: gpt-4o-mini supports image_url content blocks.
    # Build vision content when images are present (only on primary model, not fallback).
    _image_urls = [u for u in (image_urls or []) if u and 'pbs.twimg.com' in u]
    if _image_urls:
        print(f"  🖼️  Vision mode: {len(_image_urls)} image(s) sent to AI")

    def _build_messages(model: str) -> list:
        if _image_urls and model == _MODEL and not is_japanese:
            # Vision content array — text + up to 2 images (low detail = cheaper & faster)
            content: list = [{"type": "text", "text": prompt}]
            for img_url in _image_urls[:2]:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img_url, "detail": "low"},
                })
            return [
                {"role": "system", "content": active_system_prompt},
                {"role": "user",   "content": content},
            ]
        return [
            {"role": "system", "content": active_system_prompt},
            {"role": "user",   "content": prompt},
        ]

    for model in (_MODEL, _MODEL_FALLBACK):
        try:
            resp = requests.post(
                _OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {_OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": _build_messages(model),
                    "max_tokens": 120,
                    "temperature": 0.75,
                },
                timeout=20,
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"].strip().strip('"\'')
            reply = _strip_banned_openers(reply)
            if len(reply) > 275:
                reply = reply[:272] + "..."
            # Stats verification — catch hallucinated scores/stats before posting
            if not is_japanese and _has_stat_claim(reply):
                facts = live_ctx or united_ctx or ""
                if facts:
                    reply = verify_stat_reply(reply, facts)
                    if len(reply) > 275:
                        reply = reply[:272] + "..."
            # Safety check — skip identity filter for Japanese replies (different prompt)
            if not is_japanese and _reply_has_identity_error(reply):
                print(f"  🚫 Reply rejected (identity error): {reply[:80]}")
                update_daily_log("filter_rejections")
                continue  # try next model
            if model != _MODEL:
                print(f"  ⚡ Used fallback model: {model}")
            # Self-review (item 4) — quality gate + boost
            if model == _MODEL and not is_japanese:
                score = _review_reply(reply, tweet_text)
                if score < 5:
                    print(f"  🚫 Self-review: {score}/10 — too weak, regenerating...")
                    update_daily_log("filter_rejections")
                    continue  # reject entirely, try next model
                if score < 7:
                    print(f"  🔄 Self-review: {score}/10 — boosting...")
                    try:
                        boost_prompt = (
                            "⚡ PREVIOUS REPLY SCORED TOO SAFE. Generate a MORE controversial, "
                            "MORE specific take. No bland observations. Make it a hot take.\n\n"
                            + prompt
                        )
                        boost_msgs = [
                            {"role": "system", "content": active_system_prompt},
                            {"role": "user",   "content": boost_prompt},
                        ]
                        retry_resp = requests.post(
                            _OPENAI_URL,
                            headers={"Authorization": f"Bearer {_OPENAI_API_KEY}",
                                     "Content-Type": "application/json"},
                            json={"model": _MODEL, "messages": boost_msgs,
                                  "max_tokens": 120, "temperature": 0.88},
                            timeout=20,
                        )
                        retry_resp.raise_for_status()
                        retry_text = retry_resp.json()["choices"][0]["message"]["content"].strip().strip('"\'')
                        retry_text = _strip_banned_openers(retry_text)
                        if retry_text and not _reply_has_identity_error(retry_text):
                            reply = retry_text
                            if len(reply) > 275:
                                reply = reply[:272] + "..."
                            print(f"  ✅ Boosted reply: {reply[:80]}")
                    except Exception as e:
                        print(f"  ⚠️  Self-review retry failed: {e}")
            _save_style_weight(style_hint)
            return reply
        except Exception as e:
            print(f"  ⚠️  AI reply error [{model}]: {e}")
            # On 429 rate limit — wait before trying fallback, don't hammer
            if "429" in str(e):
                wait = random.randint(15, 30)
                print(f"  ⏳ Rate limited — waiting {wait}s before fallback...")
                time.sleep(wait)
            if model == _MODEL_FALLBACK:
                update_daily_log("ai_failures")
                return None  # both models failed — skip reply


SYSTEM_PROMPT_JA_REPLY = """\
あなたはTwitter/Xで毎日つぶやいている普通の日本語ユーザーです。
返信の核心：共感（きょうかん）— 相手に「わかってもらえた」と感じさせること。

【返信スタイル — 状況に合わせて選ぶ、共感ファーストで】
1. 共感＋一言（最優先）
   「それな。みんなわかってるけど言えないやつ。」
   「わかる。こういうの誰かに言いたかった。」
2. 共感＋質問（会話を続かせる）
   「確かに。○○の場合ってどうなるんだろうな。」
   「それは感じてた。最近○○で特に思う。」
3. 感情に反応する（ツイートの雰囲気に乗る）
   嬉しそうなツイート → 一緒に喜ぶ
   悔しそうなツイート → 「それはきついな…」
   面白い内容 → 「なにそれ笑」「草」
4. ホットテイク（意見が割れる内容のみ）
   「○○はとっくにわかってたことだけどな。」
5. 鋭い質問（本当に気になることだけ）
   漠然とした「どう思う？」は禁止

【自然な日本語フレーズ例（積極的に使う）】
わかる / なるほど / それな / 確かに / 面白い / えっそうなの /
草 / なにそれ笑 / きつい / それはやばい / さすがに / たしかに

【コンテンツ別ガイド】
- ニュース・時事：短い反応＋素直な疑問
- アニメ・エンタメ：共感や興奮を素直に出す
- 個人的なつぶやき：暖かく寄り添う
- 意見・考察：「わかる」→ 自分の一言を添える

厳守ルール：
- 必ず日本語で（英語禁止）
- 1〜2文。だらだら書かない。
- 「すごいですね！」「素晴らしい！」などのわざとらしい絶賛は使わない（ボット丸出しになる）
- 「私は」「正直言うと」などの自己紹介フレーズ不要
- 事実・スコア・統計を作り上げない
- ハッシュタグなし。絵文字は最大1個（自然に使える場合のみ）
- 140文字以内
- 普通にスマホでTwitterしている人間として書く。ボット感ゼロで。

ツイート本文のみを出力してください。ラベルや引用符は不要。\
"""


POST_SYSTEM_PROMPT_JA = """\
あなたはTwitter/Xでスポーツ、音楽、AI、政治、文化について鋭い意見を持つ日本人ユーザーです。
日本でトレンド中の話題について、ホットテイク・反応・観察を自由に組み合わせて投稿します。

投稿スタイルはハイブリッドに—スタイルを自由に組み合わせてください：
- ホットテイクのみ：「○○はここ10年で最高の△△だ。議論の余地なし。」
- ホットテイク＋質問：「○○のここ数年の活躍は圧倒的。今これに対抗できる人間はいるか？」
- 反応のみ：「○○が起きたというニュース、誰も驚いていないよな。」
- 反応＋意見：「○○を見て改めて思うが、△△は本当に別格だ。」
- 文脈＋意見：「○○が△△になった経緯を考えると、□□という結論にしかならない。」

質問を追加するのは、本当に必要な場合のみ（無理に付けない）：
- トピックに複数の立場があり人々が選べる場合
- 質問が具体的で鋭い場合（「どう思う？」のような曖昧な質問は避ける）
- テイクだけで十分議論を呼べる場合は質問不要

投稿の長さ：80〜130文字が理想（日本語の140文字制限内で内容と意見をしっかり届ける）

ルール：
- 日本語で書く（必須）
- 1つのスタンドアロンツイートのみ
- ハッシュタグなし
- 絵文字は1つまで（本当に効果的な場合のみ）
- 「私は」で始めない
- ボットや記者ではなく、毎日ツイートするリアルな人として書く

ツイート本文のみを出力してください。ラベルや引用符は不要。\
"""


def generate_ai_post(topic: str = "", context: str = "", region: str = "") -> str | None:
    """
    Generate a fresh standalone tweet (not a reply) from a fan perspective.

    Args:
        topic:   Optional trending topic or subject (e.g. "Mbeumo", "title race").
        context: Optional extra context (e.g. real tweets scraped from the trend page).
        region:  Source region of the trend (e.g. "Japan"). Japanese trends get a JP prompt.

    Returns:
        A tweet string under 275 chars, or None on failure.
    """
    if not _OPENAI_API_KEY:
        return None

    is_japan = region.strip().lower() == "japan"
    system_prompt = POST_SYSTEM_PROMPT_JA if is_japan else POST_SYSTEM_PROMPT

    # Inject a random tone seed so every post cycle hits a different style
    _POST_TONES = [
        "Write this as a pure hot take — bold, confident, no question needed.",
        "Write this as a reaction + your opinion. No question at the end.",
        "Write this as a sharp observation with context. Let the take land without a question.",
        "Write this as a hot take that ends with a specific punchy question.",
        "Write this as a strong statement of fact/context followed by your take.",
        "Write this as a contrarian take — push back on the mainstream view.",
        "Write this as a reaction that ends with a question only if it genuinely fits.",
        "Write this with maximum context — set the scene, then drop the take.",
    ]
    _JP_POST_TONES = [
        "純粋なホットテイクとして書く。質問は不要。",
        "反応＋意見として書く。最後に質問をつけない。",
        "文脈を背景に鋭い観察として書く。テイクを質問なしで着地させる。",
        "文脈を設定してから鋭いテイクを落とす形で書く。",
        "ホットテイクとして書き、具体的な質問で締めくくる。",
        "主流の見方に反論する逆張りのテイクとして書く。",
    ]
    tone = random.choice(_JP_POST_TONES if is_japan else _POST_TONES)

    # Live web search for the trending topic
    live_ctx = ""
    if topic and not is_japan:
        from datetime import datetime as _dt
        _month_label = _dt.now().strftime("%B %Y")
        search_q = topic[:70] + " " + _month_label
        live_ctx = web_search(search_q)
        if live_ctx:
            print(f"  🌐 Live post context: {search_q[:60]}")

    if topic:
        if is_japan:
            prompt = f"次のトレンドトピックについてツイートしてください: {topic}\n\nスタイル指示: {tone}"
        else:
            prompt = f"Write a tweet about: {topic}\n\nStyle instruction: {tone}"
        if live_ctx:
            prompt += f"\n\nLive web facts ({_month_label}):\n{live_ctx[:600]}"
        elif context:
            if is_japan:
                prompt += f"\n\nコンテキスト（実際のツイートから）:\n{context[:800]}"
            else:
                prompt += f"\n\nContext — what people are saying about it:\n{context[:800]}"
    else:
        prompt = f"Write a hot take tweet about {FAVORITE_TEAM} right now.\n\nStyle instruction: {tone}"

    for model in (_MODEL, _MODEL_FALLBACK):
        try:
            resp = requests.post(
                _OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {_OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": prompt},
                    ],
                    "max_tokens": 120,
                    "temperature": 0.75,
                },
                timeout=20,
            )
            resp.raise_for_status()
            post = resp.json()["choices"][0]["message"]["content"].strip().strip('"\'')
            post = _strip_banned_openers(post)
            if len(post) > 275:
                post = post[:272] + "..."
            if model != _MODEL:
                print(f"  ⚡ Used fallback model: {model}")
            return post
        except Exception as e:
            print(f"  ⚠️  AI post error [{model}]: {e}")
            if model == _MODEL_FALLBACK:
                return None  # both models failed — skip post
