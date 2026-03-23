"""
Playwright Trend Poster
=======================
Every hour:
  1. Scrapes X/Twitter trending topics from the Explore page
  2. Picks the most relevant trend (football-first, then general)
  3. Generates a punchy, engagement-optimised original tweet about it
  4. Posts it automatically — no manual input

Coordination:
  - Uses a shared posting_lock.json so it never clashes with the
    Notification Responder or Fast Replier mid-post
  - Run this in its own terminal alongside the other scripts — it uses a
    SEPARATE browser profile so there is no Playwright session conflict

Usage:
  python playwright_trend_poster.py              # live, post every 30 minutes
  python playwright_trend_poster.py --dry-run    # print tweet but don't post
  python playwright_trend_poster.py --once       # one cycle then exit
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import requests
try:
    from ai_reply import generate_ai_post, generate_goal_reaction, generate_follow_for_follow_post, update_daily_log, web_search
    _AI_ENABLED = True
except ImportError:
    _AI_ENABLED = False
    def update_daily_log(*a, **kw): pass  # no-op fallback
    def web_search(*a, **kw): return ""  # no-op fallback
    def generate_follow_for_follow_post() -> str:
        return "Football fans — drop your @ below 👇 Mutual follows tonight! ⚽"

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

try:
    from utils.safe_io import read_json, write_json
    from utils.bot_logger import get_logger
    _tlog = get_logger("trend_poster")
except Exception:
    import logging as _logging
    _tlog = _logging.getLogger("trend_poster")
    def read_json(p, default=None):
        import json as _j
        try: return _j.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else (default or {})
        except Exception: return default or {}
    def write_json(p, data, indent=2):
        import json as _j
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(_j.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("❌ Playwright not installed. Run: pip install playwright && playwright install chromium")

try:
    from playwright_stealth import Stealth
except ImportError:
    Stealth = None
    print("⚠️ playwright-stealth not installed. Run: pip install playwright-stealth")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

USERNAME  = os.getenv("TWITTER_USERNAME", "")
PASSWORD  = os.getenv("TWITTER_PASSWORD", "")

DATA_DIR     = Path(__file__).parent / "data"
PROFILE_DIR  = str(DATA_DIR / "browser_profile_trend")   # Own profile — no conflict
STATE_FILE   = DATA_DIR / "trend_poster_state.json"
LOCK_FILE    = DATA_DIR / "posting_lock.json"

POST_INTERVAL_SECONDS  = 2400   # 40 minutes between normal trend posts (was 30)
GOAL_CHECK_INTERVAL    = 180    # 3 minutes — check @ChampionsLeague for fresh goals
MIN_POST_GAP           = 1200   # 20 minutes minimum between ANY two posts (was 15)
MAX_DAILY_POSTS        = 48     # cap at ~48 posts/day (was 96)
LOCK_HOLD_SECONDS      = 90     # how long we hold the lock while posting
LOCK_MAX_WAIT_SECONDS  = 120    # give up waiting for lock after this long
MIN_TREND_POSTS        = 500    # ignore trends with fewer posts (dead topics)
POSTING_HOURS_WAT      = (7, 24)  # only post trend tweets 7 am – midnight WAT (UTC+1)

# ── Trend blocklist — topics we never post about ──────────────────────────────
# Religious holidays, humanitarian crises, tragedies, etc. — off-brand for a
# football banter account and risk bad-taste posts or mismatched images.
BLOCKED_TRENDS = {
    "eid mubarak", "eid al fitr", "eid al adha", "ramadan", "ramadan mubarak",
    "ramadan kareem", "happy eid", "eid saeed", "blessed eid",
    "merry christmas", "happy easter", "happy diwali", "happy hanukkah",
    "rip", "rest in peace", "prayers for", "pray for",
    "genocide", "ceasefire", "rafah", "gaza", "palestine", "free palestine",
    "mass shooting", "school shooting", "bombing",
}

# Peak engagement hours WAT (UTC+1) — post every 15 min instead of 30
PEAK_HOURS_WAT         = list(range(12, 14)) + list(range(18, 23))  # 12-2pm, 6-11pm
PEAK_INTERVAL_SECONDS  = 1200  # 20 min during peak hours (was 15)

# Accounts to scrape for live goal updates on match days.
LIVE_SCORE_ACCOUNTS = ["@ChampionsLeague", "@brfootball"]

TREND_LOCATIONS   = ["Nigeria", "United States"]

CATEGORY_SCORES   = {
    "my_team": 100, "rivals": 80, "ai": 80,
    "entertainment": 75, "politics": 70, "european": 70,
    "football": 60, "sports": 55, "general": 50,
}


# ─────────────────────────────────────────────────────────────────────────────
# Posting lock — shared with playwright_notification_responder.py
# ─────────────────────────────────────────────────────────────────────────────

def _read_lock() -> dict:
    return read_json(LOCK_FILE, {"locked": False})


def acquire_lock(holder: str = "trend_poster") -> bool:
    """Grab the posting lock if it isn't currently held. Returns True if acquired."""
    data = _read_lock()
    if data.get("locked"):
        expires = datetime.fromisoformat(data.get("expires_at", "2000-01-01"))
        if datetime.now() < expires:
            return False            # Someone else is mid-post
    # Acquire
    write_json(LOCK_FILE, {
        "locked": True,
        "holder": holder,
        "acquired_at": datetime.now().isoformat(),
        "expires_at": (datetime.now() + timedelta(seconds=LOCK_HOLD_SECONDS)).isoformat(),
    })
    return True


def release_lock():
    write_json(LOCK_FILE, {"locked": False, "holder": None})


# WOEID (Where On Earth ID) — X uses these to serve regional trends directly via URL
_WOEID_MAP = {
    "Nigeria":        "23424908",
    "United States":  "23424977",
    "United Kingdom": "23424975",
    "Japan":          "23424856",
    "Ghana":          "23424824",
    "South Africa":   "23424942",
}


def change_trending_location(page, location: str) -> bool:
    """
    Switch trending region to `location` using the WOEID direct URL.
    X serves per-country trends at /explore/tabs/trending?woeid=XXXXX — no
    clicking or settings navigation needed, so this is fast and reliable.
    Falls back to /explore/tabs/trending (default region) if WOEID not mapped.
    """
    print(f"  🌍 Switching trending region → {location} …")
    woeid = _WOEID_MAP.get(location)
    if woeid:
        url = f"https://x.com/explore/tabs/trending?woeid={woeid}"
    else:
        url = "https://x.com/explore/tabs/trending"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
        human_delay(3, 5)
        page.evaluate("window.scrollBy(0, 400)")
        human_delay(1, 2)
        if woeid:
            print(f"  ✅ Trending region → {location} (WOEID {woeid})")
        else:
            print(f"  ⚠️  No WOEID for '{location}' — using default trending page")
        return True
    except Exception as e:
        print(f"  ⚠️  Location switch error: {e}")
        return False


def set_location_in_settings(page, location: str = "Nigeria") -> bool:
    """
    Set the account's trending location permanently via Settings → Explore.
    Called once at startup so /explore/tabs/trending always returns the correct
    region without needing ?woeid= in the URL.
    """
    try:
        print(f"  ⚙️  Setting account location to '{location}' via settings/explore …")
        page.goto("https://x.com/settings/explore", wait_until="networkidle", timeout=40_000)
        human_delay(4, 6)
        print(f"  📍 URL after load: {page.url}")

        # Dismiss any cookie consent dialog that may block the settings page
        for cookie_sel in [
            'button:has-text("Accept all cookies")',
            'button:has-text("Refuse non-essential cookies")',
            '[data-testid="ce-cookie-accept"]',
        ]:
            el = page.query_selector(cookie_sel)
            if el:
                el.click()
                print(f"  🍪  Cookie dialog dismissed")
                human_delay(3, 4)
                break

        # Click the location row to open the picker
        clicked = False
        for sel in [
            '[data-testid="woeid_location"]',
            'div[role="button"]:has-text("Worldwide")',
            'div[role="button"]:has-text("Nigeria")',
        ]:
            el = page.query_selector(sel)
            if el:
                el.click()
                human_delay(2, 3)
                clicked = True
                break

        if not clicked:
            # Broader scan — any clickable row mentioning a place or "location"
            for el in page.query_selector_all('[role="button"], [tabindex="0"]'):
                txt = (el.text_content() or "").strip().lower()
                if any(w in txt for w in ["worldwide", "nigeria", "location", "trend"]):
                    el.click()
                    human_delay(2, 3)
                    clicked = True
                    break

        if not clicked:
            print(f"  ⚠️  Location row not found on settings/explore — WOEID approach still active")
            return False

        # Find the search input that appears after clicking
        inp = None
        for input_sel in [
            'input[data-testid="typeaheadInput"]',
            'input[placeholder*="earch"]',
            'input[placeholder*="ocation"]',
            'input[type="text"]',
        ]:
            inp = page.query_selector(input_sel)
            if inp:
                break

        if not inp:
            print(f"  ⚠️  Search input not found after opening location picker")
            return False

        inp.fill("")
        human_delay(0.3, 0.5)
        inp.type(location, delay=80)
        human_delay(2, 3)

        for opt_sel in ['[role="option"]', '[data-testid="typeaheadResult"]', 'li[role="listitem"]']:
            opts = page.query_selector_all(opt_sel)
            if opts:
                opts[0].click()
                human_delay(2, 3)
                print(f"  ✅ Account trending location → '{location}'")
                return True

        print(f"  ⚠️  No autocomplete results for '{location}'")
        return False

    except Exception as e:
        print(f"  ⚠️  settings/explore error: {e}")
        return False


def wait_for_lock(holder: str = "trend_poster") -> bool:
    """Block until the lock is free, then acquire it. Returns False on timeout."""
    deadline = time.time() + LOCK_MAX_WAIT_SECONDS
    while time.time() < deadline:
        if acquire_lock(holder):
            return True
        print("  ⏳ Another script is posting — waiting 10 s …")
        time.sleep(10)
    print(f"  ⚠️  Lock wait timed out after {LOCK_MAX_WAIT_SECONDS} s — skipping post")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# State management
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return read_json(STATE_FILE, {"posts_today": 0, "last_post_date": None, "used_trends": []})


def save_state(state: dict):
    write_json(STATE_FILE, state)


def get_state_for_today(state: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_post_date") != today:
        state["posts_today"] = 0
        state["last_post_date"] = today
        state["used_trends"] = []
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Football & sports keyword sets
# ─────────────────────────────────────────────────────────────────────────────

FOOTBALL_TERMS = {
    "my_team": [
        # Add your team's keywords, player names, stadium, manager, etc.
        # Example:
        # "manchester united", "man utd", "mufc", "old trafford",
        # "bruno fernandes", "mainoo", "amad",

    ],
    "rivals": [
        "arsenal", "liverpool", "man city", "chelsea", "spurs", "tottenham",
        "newcastle", "aston villa", "west ham", "everton",
        # Ex-United players now at rival clubs — generate rival-angle posts
        "garnacho", "alejandro garnacho",  # sold to Chelsea
        "onana",                             # left United
    ],
    "european": [
        "real madrid", "barcelona", "psg", "juventus", "inter", "atletico",
        "napoli", "ac milan", "bayern", "dortmund",
    ],
    "general_football": [
        "premier league", "champions league", "europa league", "football",
        "transfer", "penalty", "red card", "offside", "var", "goal", "hat trick",
        "hat-trick", "clean sheet", "fifa", "world cup",
        "bundesliga", "la liga", "serie a", "ligue 1",
    ],
    "general_sports": [
        "nba", "nfl", "ufc", "boxing", "cricket", "tennis", "formula 1", "f1",
        "wimbledon", "olympics", "superbowl", "super bowl",
    ],
    "ai_tech": [
        # models & companies
        "chatgpt", "openai", "gpt", "gpt-4", "gpt-5", "o3", "o4",
        "claude", "anthropic",
        "gemini", "google ai", "google deepmind", "deepmind",
        "grok", "xai", "x.ai",
        "llama", "meta ai",
        "mistral", "perplexity",
        "copilot", "microsoft ai",
        "midjourney", "stable diffusion", "dall-e", "sora",
        "sam altman", "demis hassabis", "dario amodei",
        "nvidia", "jensen huang",
        # concepts
        "artificial intelligence", "machine learning", "deep learning",
        "large language model", "llm", "agi", "singularity",
        "ai art", "ai generated", "ai safety", "ai regulation",
        "ai coding", "vibe coding", "cursor ai",
        "prompt engineering", "fine tuning", "fine-tuning",
        "neural network", "transformer",
        # short forms used in trending
        " ai ", "#ai", "ai is", "ai will", "ai can", "ai jobs",
    ],
    "entertainment": [
        # Global music / pop culture
        "netflix", "spotify", "hbo", "disney", "apple tv", "amazon prime",
        "oscars", "grammy", "billboard", "vma", "brit awards",
        "drake", "beyonce", "taylor swift", "rihanna", "kanye", "ye",
        "eminem", "bad bunny", "the weeknd", "post malone", "sabrina carpenter",
        "celebrity", "movie", "film", "album new", "music video", "concert", "world tour",
        "streaming", "box office",
        # Afrobeats / Nigerian entertainment
        "burna boy", "wizkid", "davido", "afrobeats", "afrobeat",
        "tiwa savage", "asake", "simi", "ckay", "rema", "ayra starr",
        "fireboy", "olamide", "2baba", "2face", "don jazzy",
        "nollywood", "bbnaija", "big brother naija",
        # Nigerian music — expanded
        "zlatan", "naira marley", "portable", "seun kuti", "yemi alade",
        "pheelz", "victony", "oxlade", "tems", "omah lay", "ruger",
        "afropop", "highlife", "fuji", "juju music",
        # US entertainment
        "nicki minaj", "cardi b", "kendrick lamar", "21 savage", "travis scott",
        "lil baby", "gunna", "metro boomin", "future", "lil durk",
        "snl", "saturday night live", "mtv", "bet awards",
    ],
    "nigerian_life": [
        # Social / everyday Nigeria
        "nigeria", "lagos", "abuja", "ph", "kano", "ibadan",
        "nigerian", "naija", "9ja", "naijatwitter",
        "japa", "visa", "canada immigration", "uk visa",
        "nepa", "phcn", "electricity", "fuel", "generator",
        "dangote", "zenith bank", "gtbank", "access bank", "opay", "palmpay",
        "nba naija", "supersEagles", "super eagles", "super falcons",
        "seplat", "mtn nigeria", "airtel", "glo",
        "jollof", "amala", "suya", "egusi",
    ],
    "politics": [
        # Global
        "president", "election", "vote", "ballot", "congress", "parliament",
        "senate", "prime minister", "government", "policy", "constitution",
        "trump", "biden", "kamala", "maga",
        "democrat", "republican", "labour party", "conservative",
        "starmer", "keir starmer",
        "war", "ukraine", "russia", "israel", "gaza", "nato",
        "un", "g7", "g20", "imf", "world bank", "sanctions",
        "tariff", "economy", "inflation", "recession", "gdp",
        # Nigerian politics / economy
        "tinubu", "peter obi", "atiku",
        "naira", "cbn", "nnpc", "efcc", "national assembly", "inec",
        "fuel subsidy", "petrol price", "nigeria news",
    ],
}

ALL_FOOTBALL_TERMS = [t for sublist in FOOTBALL_TERMS.values() for t in sublist]


def classify_trend(trend_name: str) -> str:
    """Return category string used in CATEGORY_SCORES."""
    name_lower = trend_name.lower()
    for term in FOOTBALL_TERMS["my_team"]:
        if term in name_lower:
            return "my_team"
    for term in FOOTBALL_TERMS["rivals"]:
        if term in name_lower:
            return "rivals"
    for term in FOOTBALL_TERMS["european"]:
        if term in name_lower:
            return "european"
    for term in FOOTBALL_TERMS["general_football"]:
        if term in name_lower:
            return "football"
    for term in FOOTBALL_TERMS["general_sports"]:
        if term in name_lower:
            return "sports"
    for term in FOOTBALL_TERMS["ai_tech"]:
        if term in name_lower:
            return "ai"
    for term in FOOTBALL_TERMS["entertainment"]:
        if term in name_lower:
            return "entertainment"
    for term in FOOTBALL_TERMS["politics"]:
        if term in name_lower:
            return "politics"
    if hasattr(FOOTBALL_TERMS, '__contains__') or True:
        for term in FOOTBALL_TERMS.get("nigerian_life", []):
            if term in name_lower:
                return "nigerian_life"
    return "general"


CATEGORY_SCORES["nigerian_life"] = 65  # Nigerian everyday > pure general


# ─────────────────────────────────────────────────────────────────────────────
# Tweet generation per category
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Context detection — read what people are actually saying about a trend
# ─────────────────────────────────────────────────────────────────────────────

# Signals for post-type detection (same vocabulary as fast_replier)
HYPE_WORDS     = ["breaking", "just in", "official", "confirmed", "announced", "done deal",
                   "breaking:", "🚨", "⚡", "🔴", "won", "loses", "fired", "sacked", "signed",
                   "scores", "goal", "red card", "banned", "injured", "done", "leaked"]
QUESTION_WORDS = ["?", "who", "what", "why", "when", "how", "should", "would", "could",
                   "do you", "do we", "is this", "thoughts", "anyone else"]
OPINION_WORDS  = ["think", "believe", "feel", "opinion", "honest", "personally", "unpopular",
                   "hot take", "real talk", "genuinely", "actually", "ngl", "imo", "tbh"]
AGREE_WORDS    = ["agree", "exactly", "facts", "100%", "same", "absolutely", "spot on", "this",
                   "preach", "correct", "truth", "right", "finally"]
DISAGREE_WORDS = ["wrong", "disagree", "no", "cap", "nope", "rubbish", "nonsense", "false",
                   "stop", "lol no", "actually no", "counterpoint", "nah"]


def detect_post_type(text: str) -> str:
    """Detect the dominant angle in a body of text."""
    t = text.lower()
    if any(w in t for w in HYPE_WORDS):
        return "breaking_news"
    if any(w in t for w in QUESTION_WORDS):
        return "question"
    if any(w in t for w in OPINION_WORDS):
        return "opinion"
    if any(w in t for w in AGREE_WORDS + DISAGREE_WORDS):
        return "debate"
    return "statement"


def fetch_trend_context(page, trend_name: str) -> str:
    """
    Visit the trend's live search page, read the top tweets, and return
    the combined text.  This is what we use to detect *what's actually
    being discussed* so the generated tweet matches the real discourse.
    """
    try:
        query = urllib.parse.quote_plus(trend_name)
        # &f=live  = chronological; gives us the freshest conversation
        url = f"https://x.com/search?q={query}&f=live"
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        human_delay(3, 5)

        articles = page.query_selector_all('article[data-testid="tweet"]')
        snippets = []
        for article in articles[:12]:  # read more tweets for richer context
            text_el = article.query_selector('[data-testid="tweetText"]')
            if text_el:
                t = text_el.text_content().strip()
                if t and len(t) > 15:
                    snippets.append(t)

        combined = " ".join(snippets[:10])  # use up to 10 for AI
        if combined:
            print(f"  📖 Context fetched ({len(snippets)} tweets, {len(combined)} chars)")
            preview = combined[:200].replace("\n", " ")
            print(f'     Preview: "{preview}…"')
        else:
            print("  ⚠️  No context tweets found — will use trend name only")
        return combined

    except Exception as e:
        print(f"  ⚠️  Context fetch error: {e}")
        return ""


def research_trend(trend_name: str, region: str = "") -> str:
    """
    DuckDuckGo web search to find out WHY this topic is currently trending.
    Runs outside the browser — uses the DDGS API directly.
    Returns a compact summary string, or '' if nothing found.
    """
    try:
        # Add region hint only if it's not already in the trend name
        region_hint = f" {region}" if region and region.lower() not in trend_name.lower() else ""
        query = f"{trend_name}{region_hint} 2026"
        results = web_search(query, max_results=5)
        if results:
            print(f"  🔎 Web research found ({len(results)} chars): {query[:60]}")
            preview = results[:180].replace("\n", " ")
            print(f'     "{preview}…"')
        else:
            print(f"  🔎 No web research results for: {query[:60]}")
        return results
    except Exception as e:
        print(f"  ⚠️  Research error: {e}")
        return ""


def generate_tweet(trend_name: str, context_text: str = "", count_text: str = "", region: str = "") -> str:
    """Generate a fresh tweet about the trending topic via AI."""
    clean_name = trend_name.strip().lstrip("#").strip()
    # Pass the clean topic name only — no "trending in X" label so the AI
    # doesn't announce the trend. Region is passed separately for language routing.
    if _AI_ENABLED:
        ai = generate_ai_post(topic=clean_name, context=context_text, region=region)
        if ai:
            return ai
    # Fallback: give a hot-take flavour, not a trend announcement
    return f"Hot take on {clean_name}: the mainstream opinion is wrong. Full stop."


# Trending topic scraper
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trend:
    name: str
    category: str       # raw category text from X (e.g. "Sports · Trending")
    count: str          # e.g. "15.2K posts"
    score: int          # how relevant we think it is (higher = better)
    region: str = ""    # source region, e.g. "Japan", "Nigeria", "United States"


def _clean_text(t: str) -> str:
    return re.sub(r'\s+', ' ', t or "").strip()


def _parse_trend_item(el) -> Optional[Trend]:
    """Parse a single trend element into a Trend dataclass."""
    try:
        full_text = _clean_text(el.text_content())
        if not full_text or len(full_text) < 2:
            return None

        # Strategy: split by newlines then filter
        lines = [l.strip() for l in el.inner_text().split("\n") if l.strip()]
        if not lines:
            return None

        # Identify which line is the trend name (not a count, not just "Trending")
        category_line = ""
        name_line = ""
        count_line = ""

        for line in lines:
            ll = line.lower()
            if re.search(r'\d[\d,.]*\s*(k|m)?\s*posts?', ll):
                count_line = line
            elif "trending" in ll and len(line) < 60:
                category_line = line
            elif not name_line and len(line) >= 2:
                name_line = line

        # Re-do: look for the "name" as the longest non-count, non-category line
        content_lines = [
            l for l in lines
            if "trending" not in l.lower()
            and not re.search(r'\d[\d,.]*\s*(k|m)?\s*posts?', l.lower())
            and len(l) > 1
        ]
        if content_lines:
            # Prefer lines without a colon or slash (those tend to be context labels)
            name_candidates = [l for l in content_lines if "·" not in l and "/" not in l]
            name_line = name_candidates[0] if name_candidates else content_lines[0]

        if not name_line:
            return None

        # Skip items that are likely not topics (pure numbers, single punctuation, etc.)
        if re.fullmatch(r'[\d,. ]+', name_line):
            return None

        # Score based on relevance
        cat = classify_trend(name_line)
        score = CATEGORY_SCORES.get(cat, 10)

        return Trend(
            name=name_line,
            category=category_line or "Trending",
            count=count_line,
            score=score,
        )
    except Exception:
        return None


def _extract_from_search_links(page, label: str) -> list[Trend]:
    """Pull trends from /search?q= hrefs — works on both explore and home pages."""
    import urllib.parse
    trends = []
    links = page.query_selector_all('a[href*="/search?q="]')
    seen: set[str] = set()
    for link in links:
        try:
            href = link.get_attribute("href") or ""
            m = re.search(r'[?&]q=([^&]+)', href)
            if not m:
                continue
            raw_q = urllib.parse.unquote_plus(m.group(1)).strip()
            # Skip if it's a multi-word query with operators (advanced search links)
            if len(raw_q) > 80 or "filter:" in raw_q or "from:" in raw_q:
                continue
            key = raw_q.lower()
            if key in seen or len(key) < 2:
                continue
            seen.add(key)

            # Try to get a count from the link's surrounding text
            text = _clean_text(link.text_content())
            count_m = re.search(r'([\d,.]+\s*[KkMm]?\s*posts?)', text)
            count = count_m.group(1) if count_m else ""

            cat = classify_trend(raw_q)

            trends.append(Trend(
                name=raw_q,
                category=label,
                count=count,
                score=CATEGORY_SCORES.get(cat, 10),
            ))
        except Exception:
            continue
    return trends


def _extract_from_testid_trend(page) -> list[Trend]:
    """Extract trends from data-testid='trend' or 'trendItem' elements."""
    trends = []
    for testid in ("trend", "trendItem"):
        items = page.query_selector_all(f'[data-testid="{testid}"]')
        if not items:
            continue
        print(f"  Found {len(items)} items via data-testid='{testid}'")
        for item in items:
            t = _parse_trend_item(item)
            if t:
                trends.append(t)
        if trends:
            break
    return trends


def scrape_trending(page) -> list[Trend]:
    """Navigate to X trending and return parsed Trend list, using multiple strategies."""
    print("🔍 Scraping trending topics …")
    trends: list[Trend] = []

    # ── Strategy 1: Explore/Trending tab (direct URL) ─────────────────────
    try:
        print("  Strategy 1: explore/tabs/trending …")
        page.goto("https://x.com/explore/tabs/trending",
                  wait_until="domcontentloaded", timeout=25_000)
        # Wait generously for JS-rendered content
        human_delay(5, 8)
        page.evaluate("window.scrollBy(0, 600)")
        human_delay(2, 3)

        trends = _extract_from_testid_trend(page)

        # Also try search-link extraction on this page
        if not trends:
            trends = _extract_from_search_links(page, "Trending")
            if trends:
                print(f"  Found {len(trends)} trends via search-link extraction (explore)")

    except Exception as e:
        print(f"  Strategy 1 error: {e}")

    # ── Strategy 2: Explore base page + click Trending tab ────────────────
    if not trends:
        try:
            print("  Strategy 2: explore + click Trending tab …")
            page.goto("https://x.com/explore", wait_until="domcontentloaded", timeout=20_000)
            human_delay(3, 5)

            # Click "Trending" tab
            for sel in [
                'a[href*="trending"]',
                'span:has-text("Trending")',
                'div[role="tab"]:has-text("Trending")',
            ]:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    human_delay(3, 5)
                    break

            trends = _extract_from_testid_trend(page)
            if not trends:
                trends = _extract_from_search_links(page, "Trending")
                if trends:
                    print(f"  Found {len(trends)} trends via search-link extraction (explore tab)")

        except Exception as e:
            print(f"  Strategy 2 error: {e}")

    # ── Strategy 3: Home page sidebar ─────────────────────────────────────
    if not trends:
        try:
            print("  Strategy 3: home page sidebar …")
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
            human_delay(4, 6)

            sidebar = page.query_selector('[data-testid="sidebarColumn"]')
            if sidebar:
                # Try testid first on sidebar
                for testid in ("trend", "trendItem"):
                    items = sidebar.query_selector_all(f'[data-testid="{testid}"]')
                    if items:
                        print(f"    Sidebar: {len(items)} items via data-testid='{testid}'")
                        for item in items:
                            t = _parse_trend_item(item)
                            if t:
                                trends.append(t)
                        break

            # Also try search links anywhere on home page
            if not trends:
                trends = _extract_from_search_links(page, "Sidebar trending")
                if trends:
                    print(f"  Found {len(trends)} trends via search-link extraction (home)")

        except Exception as e:
            print(f"  Strategy 3 error: {e}")

    # ── Strategy 4: Parse raw page text looking for "Trending" sections ───
    if not trends:
        try:
            print("  Strategy 4: raw text mining …")
            page.goto("https://x.com/explore/tabs/trending",
                      wait_until="networkidle", timeout=30_000)
            human_delay(4, 6)
            body_text = page.inner_text("body")
            lines = [l.strip() for l in body_text.split("\n") if l.strip()]

            # Look for pairs: a line followed by "X posts" or "Trending"
            i = 0
            seen: set[str] = set()
            while i < len(lines) - 1:
                line = lines[i]
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                is_count = bool(re.match(r'[\d,.]+\s*[KkMm]?\s*posts?', next_line))
                is_trending_marker = "trending" in next_line.lower()

                if (is_count or is_trending_marker) and 2 <= len(line) <= 60:
                    # Likely a trend name
                    key = line.lower()
                    if key not in seen and not re.fullmatch(r'[\d,. ]+', line):
                        seen.add(key)
                        cat = classify_trend(line)
                        trends.append(Trend(
                            name=line,
                            category="Trending" if is_trending_marker else "",
                            count=next_line if is_count else "",
                            score=CATEGORY_SCORES.get(cat, 10),
                        ))
                i += 1

            if trends:
                print(f"  Found {len(trends)} trends via text mining")

        except Exception as e:
            print(f"  Strategy 4 error: {e}")

    if not trends:
        print("  ⚠️ No trends found with any strategy — will retry next cycle")
        return []

    # ── De-duplicate by name ──────────────────────────────────────────────
    seen_names: set[str] = set()
    unique: list[Trend] = []
    for t in trends:
        key = t.name.lower().strip()
        if key not in seen_names and len(key) > 1:
            seen_names.add(key)
            unique.append(t)

    print(f"  ✅ {len(unique)} unique trends after dedup")
    return unique


def scrape_multi_region(page, locations: list[str] = None) -> list[Trend]:
    """Scrape trending topics across Nigeria, USA, and Japan then merge."""
    if locations is None:
        locations = TREND_LOCATIONS
    all_trends: list[Trend] = []
    seen_names: set[str] = set()

    for location in locations:
        change_trending_location(page, location)
        regional = scrape_trending(page)
        new_count = 0
        for t in regional:
            key = t.name.lower().strip()
            if key not in seen_names and len(key) > 1:
                seen_names.add(key)
                t.region = location   # tag with source region
                all_trends.append(t)
                new_count += 1
        print(f"  🌍 {location}: +{new_count} unique trends (total so far: {len(all_trends)})")

    return all_trends


def _normalize_trend_name(name: str) -> str:
    """Normalize a trend name for dedup comparison."""
    return name.lower().strip().lstrip("#").strip()


def _parse_post_count(count_str: str) -> int:
    """Parse '15.2K posts' → 15200. Returns 0 if unparseable."""
    m = re.search(r'([\d,.]+)\s*([KkMm]?)', count_str.replace(',', ''))
    if not m:
        return 0
    try:
        num = float(m.group(1))
        suffix = m.group(2).lower()
        if suffix == 'k':
            num *= 1_000
        elif suffix == 'm':
            num *= 1_000_000
        return int(num)
    except Exception:
        return 0


def pick_trend(trends: list[Trend], used_today: list[str]) -> Optional[Trend]:
    """Pick the best unused trend — boosted by cross-region presence and post count."""
    used_normalized = {_normalize_trend_name(u) for u in used_today}

    # Filter already used (normalized match), dead topics, and blocked trends
    candidates = [
        t for t in trends
        if _normalize_trend_name(t.name) not in used_normalized
        and _parse_post_count(t.count) >= MIN_TREND_POSTS
        and not any(b in t.name.lower() for b in BLOCKED_TRENDS)
    ]
    if not candidates:
        # Relax the post-count filter if nothing passes (but still respect blocklist)
        candidates = [
            t for t in trends
            if _normalize_trend_name(t.name) not in used_normalized
            and not any(b in t.name.lower() for b in BLOCKED_TRENDS)
        ]
    if not candidates:
        candidates = trends  # full reset — we've used everything
    if not candidates:
        return None

    # Tally how many regions each trend appears in
    name_regions: dict[str, set] = {}
    for t in trends:
        key = _normalize_trend_name(t.name)
        name_regions.setdefault(key, set()).add(t.region)

    def trend_sort_key(t: Trend):
        key = _normalize_trend_name(t.name)
        cross_region_bonus = 30 if len(name_regions.get(key, set())) > 1 else 0
        post_count = _parse_post_count(t.count)
        # Normalise post count to 0-20 bonus range
        count_bonus = min(20, post_count // 5_000)
        return t.score + cross_region_bonus + count_bonus

    candidates.sort(key=trend_sort_key, reverse=True)
    top = candidates[:min(5, len(candidates))]
    chosen = random.choice(top)

    cross = len(name_regions.get(_normalize_trend_name(chosen.name), set())) > 1
    print(f"  {'🌍 Cross-region' if cross else '📍 Regional'} pick: '{chosen.name}' "
          f"(score {chosen.score}, posts: {chosen.count}, region: {chosen.region})")
    return chosen


# ─────────────────────────────────────────────────────────────────────────────
# Browser helpers
# ─────────────────────────────────────────────────────────────────────────────

def human_delay(lo: float = 0.8, hi: float = 2.2):
    time.sleep(random.uniform(lo, hi))


def is_logged_in(page) -> bool:
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
        human_delay(3, 5)
        page.wait_for_selector('[data-testid="SideNav_NewTweet_Button"]', timeout=15_000)
        print("✅ Session active")
        return True
    except PlaywrightTimeout:
        return False
    except Exception:
        return False


def login(page) -> bool:
    print("🔐 Logging in …")
    try:
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=20_000)
        human_delay(3, 5)

        for sel in ['input[autocomplete="username"]', 'input[name="text"]']:
            try:
                f = page.wait_for_selector(sel, timeout=7_000)
                if f:
                    f.fill(USERNAME)
                    break
            except PlaywrightTimeout:
                continue
        human_delay(1, 2)
        for sel in ['button:has-text("Next")', 'div[role="button"]:has-text("Next")']:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                break
        else:
            page.keyboard.press("Enter")
        human_delay(2, 4)

        try:
            vf = page.wait_for_selector('input[data-testid="ocfEnterTextTextInput"]', timeout=4_000)
            if vf:
                vf.fill(USERNAME)
                page.keyboard.press("Enter")
                human_delay(2, 3)
        except PlaywrightTimeout:
            pass

        for sel in ['input[name="password"]', 'input[type="password"]']:
            try:
                f = page.wait_for_selector(sel, timeout=7_000)
                if f:
                    f.fill(PASSWORD)
                    break
            except PlaywrightTimeout:
                continue
        human_delay(1, 2)
        for sel in ['button:has-text("Log in")', 'div[role="button"]:has-text("Log in")']:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                break
        else:
            page.keyboard.press("Enter")

        human_delay(5, 8)
        page.wait_for_selector('[data-testid="SideNav_NewTweet_Button"]', timeout=20_000)
        print("✅ Login successful")
        return True
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Image selection (optional media attachment)
# ─────────────────────────────────────────────────────────────────────────────

IMAGE_DIR   = DATA_DIR / "images"
_CACHE_DIR  = IMAGE_DIR / "_cache"   # auto-downloaded images per cycle
_IMG_EXTS   = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_MAX_CACHE  = 60   # prune oldest when cache exceeds this

# category → preferred image subfolder
_CAT_FOLDER: dict[str, str] = {
    "my_team":   "my_team",
    "rivals":    "rivals",
    "european":  "football",
    "football":  "football",
    "sports":    "football",
}
# post_type → preferred image subfolder (checked before category)
_TYPE_FOLDER: dict[str, str] = {
    "breaking_news": "breaking",
    "debate":        "debate",
}


def pick_image(post_type: str = "", category: str = "") -> Optional[str]:
    """Return path to a random image from data/images/<folder>, or None if empty.
    
    For non-football categories (politics, AI, entertainment, etc.) we skip
    the football/general fallback to avoid attaching mismatched images.
    """
    folders: list[str] = []
    if post_type in _TYPE_FOLDER:
        folders.append(_TYPE_FOLDER[post_type])
    if category in _CAT_FOLDER:
        folders.append(_CAT_FOLDER[category])

    # Only fall through to generic football/general images for football-related categories.
    # Non-football topics (politics, AI, entertainment, etc.) should go text-only
    # rather than risk attaching a random football image to a Gaza/music/AI post.
    _FOOTBALL_CATS = {"my_team", "rivals", "european", "football", "sports"}
    if category in _FOOTBALL_CATS or not category:
        folders += ["football", "general", ""]        # generic fallbacks

    for folder in folders:
        search_dir = IMAGE_DIR / folder if folder else IMAGE_DIR
        if not search_dir.exists():
            continue
        candidates = [f for f in search_dir.iterdir() if f.suffix.lower() in _IMG_EXTS]
        if candidates:
            return str(random.choice(candidates))
    return None   # no images available → text-only post


# ─────────────────────────────────────────────────────────────────────────────
# Auto image fetching — X/Twitter + web
# ─────────────────────────────────────────────────────────────────────────────

def _download_image(url: str, dest: Path) -> bool:
    """Download url → dest. Returns True if a valid image was saved."""
    try:
        r = requests.get(
            url, timeout=8, stream=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if r.status_code != 200:
            return False
        ct = r.headers.get("content-type", "")
        if "image" not in ct and not any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]):
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return dest.stat().st_size > 3000   # skip tiny/broken images
    except Exception:
        return False


def _cache_subfolder(category: str) -> str:
    return {"my_team": "my_team", "rivals": "rivals"}.get(category, "football")


def fetch_images_from_trend_page(page, category: str = "") -> list[str]:
    """
    Extract tweet images from the ALREADY LOADED X search page.
    Call immediately after fetch_trend_context() — reuses the same page, no extra navigation.
    Returns list of local file paths.
    
    For non-football categories (politics, entertainment, etc.) we skip image
    scraping entirely to avoid attaching unrelated or sensitive images.
    """
    if category not in _FOOTBALL_CATEGORIES:
        print(f"  ⏭️  Skipping X image scrape — category '{category}' is not football-related")
        return []
    saved: list[str] = []
    try:
        sub   = _cache_subfolder(category)
        imgs  = page.query_selector_all(
            'article[data-testid="tweet"] img[src*="pbs.twimg.com/media"]'
        )
        seen: set[str] = set()
        for img in imgs:
            if len(saved) >= 3:
                break
            src = img.get_attribute("src") or ""
            base = src.split("?")[0]
            if not base or base in seen:
                continue
            seen.add(base)
            url  = base + "?format=jpg&name=medium"
            dest = _CACHE_DIR / sub / f"x_{abs(hash(url)) % 10**9}.jpg"
            if dest.exists() or _download_image(url, dest):
                saved.append(str(dest))
                print(f"  📸 X image: {dest.name}")
    except Exception as e:
        print(f"  ⚠️  X image fetch error: {e}")
    return saved


# Categories where we can confidently attach images (sport/football related)
_FOOTBALL_CATEGORIES = {"my_team", "rivals", "european", "football", "sports"}


def fetch_images_from_web(trend_name: str, category: str = "") -> list[str]:
    """
    DuckDuckGo image search → download up to 2 images.
    Used as fallback when X scraping yields nothing.
    Only fetches for football-related categories to avoid mismatched images.
    """
    # Skip DDG image search for non-football topics — too high a risk of
    # attaching an unrelated image (e.g. a football photo on a politics post).
    if category not in _FOOTBALL_CATEGORIES:
        print(f"  ⏭️  Skipping DDG image search — category '{category}' is not football-related")
        return []
    saved: list[str] = []
    try:
        from duckduckgo_search import DDGS
        sub     = _cache_subfolder(category)
        query   = f"{trend_name} football"
        results = list(DDGS().images(query, max_results=8, type_image="photo", size="Medium"))
        for r in results:
            if len(saved) >= 2:
                break
            url = r.get("image", "")
            if not url:
                continue
            dest = _CACHE_DIR / sub / f"web_{abs(hash(url)) % 10**9}.jpg"
            if dest.exists() or _download_image(url, dest):
                saved.append(str(dest))
                print(f"  🌐 Web image: {dest.name}")
    except Exception as e:
        print(f"  ⚠️  Web image fetch error: {e}")
    return saved


def _prune_image_cache():
    """Keep the cache under _MAX_CACHE files by deleting the oldest."""
    try:
        files = [f for f in _CACHE_DIR.rglob("*") if f.is_file() and f.suffix.lower() in _IMG_EXTS]
        if len(files) > _MAX_CACHE:
            files.sort(key=lambda f: f.stat().st_mtime)
            for f in files[:len(files) - _MAX_CACHE]:
                f.unlink(missing_ok=True)
            print(f"  🗑️  Cache pruned → {_MAX_CACHE} images kept")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Post tweet
# ─────────────────────────────────────────────────────────────────────────────

def post_tweet(page, text: str, image_path: Optional[str] = None) -> bool:
    """Compose and post an original tweet — fully automated.
    
    Pass image_path to attach a local image file (jpg/png/gif/webp).
    If attachment fails the tweet is still posted as text-only.
    """
    print(f"\n📝 Posting ({len(text)} chars):")
    print(f'   "{text}"')

    try:
        # Go to home to use the compose button
        if "home" not in page.url:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15_000)
            human_delay(2, 3)

        # Open compose box
        compose_btn = page.wait_for_selector(
            '[data-testid="SideNav_NewTweet_Button"]', timeout=10_000
        )
        compose_btn.click()
        human_delay(1.5, 2.5)

        # Find textarea
        textarea = None
        for sel in [
            '[data-testid="tweetTextarea_0"]',
            'div[contenteditable="true"][data-testid="tweetTextarea_0"]',
            'div[role="textbox"]',
        ]:
            try:
                textarea = page.wait_for_selector(sel, timeout=8_000)
                if textarea:
                    break
            except PlaywrightTimeout:
                continue

        if not textarea:
            print("❌ Could not find tweet textarea")
            return False

        textarea.click()
        human_delay(0.5, 1)

        try:
            textarea.fill(text)
        except Exception:
            for char in text:
                page.keyboard.type(char, delay=random.randint(30, 80))

        human_delay(1, 2)

        # Attach image if one was provided
        if image_path and Path(image_path).exists():
            try:
                file_input = page.wait_for_selector(
                    'input[data-testid="fileInput"]',
                    state="attached", timeout=5_000,
                )
                file_input.set_input_files(image_path)
                human_delay(2, 3)
                # Wait for the thumbnail preview to confirm upload
                page.wait_for_selector(
                    '[data-testid="attachments"] img, [data-testid="previewInterstitial"],'
                    ' [data-testid="tweet-image-preview-media"]',
                    timeout=10_000,
                )
                print(f"🖼️  Image attached: {Path(image_path).name}")
            except Exception as img_err:
                print(f"⚠️  Image attach skipped ({img_err}) — posting text only")

        # Click Post button
        post_btn = None
        for sel in [
            '[data-testid="tweetButtonInline"]',
            '[data-testid="tweetButton"]',
            'button:has-text("Post")',
            'div[role="button"]:has-text("Post")',
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=5_000)
                if btn and btn.is_enabled():
                    post_btn = btn
                    break
            except PlaywrightTimeout:
                continue

        if not post_btn:
            print("❌ Could not find Post button")
            return False

        post_btn.click()
        human_delay(3, 5)

        # Rate limit detection
        try:
            body_text = page.inner_text("body", timeout=2_000) or ""
            if any(x in body_text.lower() for x in ["rate limit", "you're posting too much", "slow down"]):
                print("⚠️ Rate limit detected — backing off 5 minutes")
                time.sleep(300)
                return False
        except Exception:
            pass

        # Confirm posted
        try:
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', state="hidden", timeout=8_000)
            print("🎉 TWEET POSTED SUCCESSFULLY!")
        except PlaywrightTimeout:
            if "status" in page.url or "home" in page.url:
                print("🎉 TWEET POSTED (confirmed via URL)")
            else:
                print("⚠️  Could not confirm — check X manually")

        return True

    except Exception as e:
        print(f"❌ Post error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Live game posting
# ─────────────────────────────────────────────────────────────────────────────

_GOAL_KEYWORDS = [
    "goal", "goall", "scores", "scored", "penalty", "red card", "equaliz",
    "1-0", "2-0", "2-1", "3-0", "3-1", "3-2", "0-1", "0-2", "1-1", "1-2",
    "aggregate", "extra time", "aet", "shootout", "shoot-out",
]
# Tweet must contain at least one of these to confirm it's CL-related.
# This prevents @brfootball Premier League or other competition tweets slipping through.
_CL_KEYWORDS = [
    "champions league", "ucl", "#ucl", "#championsleague", "#uefachampionsleague",
    "quarter-final", "quarterfinal", "semi-final", "semifinal",
    "round of 16", "knockout", "cl ", " cl,", " cl.",
]
_GOAL_SEEN: dict = {}  # tweet_text_hash → timestamp, to avoid re-posting same goal


def fetch_goal_tweet(page) -> str | None:
    """
    Visit each LIVE_SCORE_ACCOUNT profile in order and return the text of the
    most recent tweet if it looks like a fresh goal update (< 6 min old).
    Returns None if nothing fresh found.
    """
    import hashlib
    now = time.time()

    for account in LIVE_SCORE_ACCOUNTS:
        handle = account.lstrip("@")
        url = f"https://x.com/{handle}"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            human_delay(2, 3)
            page.wait_for_selector('article[data-testid="tweet"]', timeout=10_000)

            articles = page.query_selector_all('article[data-testid="tweet"]')
            if not articles:
                continue

            for article in articles[:3]:
                try:
                    # Skip pinned tweets
                    social_ctx = article.query_selector('[data-testid="socialContext"]')
                    if social_ctx and "pinned" in (social_ctx.text_content() or "").lower():
                        continue

                    # Get tweet text
                    text_el = article.query_selector('[data-testid="tweetText"]')
                    if not text_el:
                        continue
                    text = text_el.text_content().strip()
                    if not text or len(text) < 10:
                        continue

                    # Must look like a goal/match update
                    t_lower = text.lower()
                    if not any(kw in t_lower for kw in _GOAL_KEYWORDS):
                        continue

                    # Must be CL-related — ignore Premier League, La Liga, etc.
                    if not any(kw in t_lower for kw in _CL_KEYWORDS):
                        print(f"  ⏭️  Skipping non-CL tweet from {account}: \"{text[:80]}\"")
                        continue

                    # Get timestamp — look for <time> element
                    time_el = article.query_selector("time")
                    if time_el:
                        dt_str = time_el.get_attribute("datetime") or ""
                        if dt_str:
                            from datetime import timezone
                            try:
                                import dateutil.parser
                                tweet_dt = dateutil.parser.parse(dt_str)
                                age_seconds = (datetime.now(timezone.utc) - tweet_dt).total_seconds()
                                if age_seconds > 360:  # older than 6 minutes — skip
                                    continue
                            except Exception:
                                pass  # if we can't parse time, allow it through

                    # Dedup by text hash
                    text_hash = hashlib.md5(text.encode()).hexdigest()
                    if text_hash in _GOAL_SEEN:
                        if now - _GOAL_SEEN[text_hash] < 3600:  # seen in last hour
                            continue
                    _GOAL_SEEN[text_hash] = now

                    print(f"  ⚽ Fresh goal tweet from {account}: \"{text[:100]}\"")
                    return text

                except Exception:
                    continue

        except Exception as e:
            print(f"  ⚠️  Goal scrape error ({account}): {e}")
            continue

    return None


def try_post_goal_reaction(page, state: dict, dry_run: bool = False) -> bool:
    """
    Scrape live score accounts for a fresh goal tweet and post a rephrased reaction.
    Respects MIN_POST_GAP to avoid posting too frequently.
    Returns True if a post was made.
    """
    if not _AI_ENABLED:
        return False
    if state["posts_today"] >= MAX_DAILY_POSTS:
        return False

    gap = _time_since_last_post(state)
    if gap < MIN_POST_GAP:
        remaining = int(MIN_POST_GAP - gap)
        print(f"  ⏳ Goal post: skipping — last post was {int(gap)}s ago (need {remaining}s more)")
        return False

    goal_tweet = fetch_goal_tweet(page)
    if not goal_tweet:
        return False

    tweet = generate_goal_reaction(goal_tweet)
    if not tweet:
        print("  ⚠️  Could not generate goal reaction")
        return False

    print(f'  🔴 Goal reaction ({len(tweet)} chars): "{tweet}"')

    if dry_run:
        print("  [DRY-RUN] Not posted.")
        return False

    if not wait_for_lock("trend_poster_goal"):
        return False

    try:
        success = post_tweet(page, tweet)
    finally:
        release_lock()

    if success:
        now = time.time()
        state["posts_today"] = state.get("posts_today", 0) + 1
        state["last_live_post"] = now
        state["last_post_at"] = now
        save_state(state)
        update_daily_log("goal_posts")

    return success


def _time_since_last_post(state: dict) -> float:
    """Seconds since the most recent post of any type."""
    return time.time() - max(
        state.get("last_post_at", 0),
        state.get("last_live_post", 0),
    )

def _is_in_posting_window() -> bool:
    """Return True if the current WAT (UTC+1) hour is within POSTING_HOURS_WAT."""
    from datetime import timezone, timedelta
    wat = timezone(timedelta(hours=1))
    hour = datetime.now(wat).hour
    return POSTING_HOURS_WAT[0] <= hour < POSTING_HOURS_WAT[1]




# ─────────────────────────────────────────────────────────────────────────────
# Main cycle
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle(page, dry_run: bool = False) -> bool:
    """One full cycle: scrape → pick → generate → post. Returns True if posted."""
    state = get_state_for_today(load_state())

    # Daily cap check
    if state["posts_today"] >= MAX_DAILY_POSTS:
        print(f"📊 Daily cap reached ({MAX_DAILY_POSTS}). Skipping until tomorrow.")
        return False

    # Safety gap — skip trend post if a live game post went out too recently
    gap = _time_since_last_post(state)
    if gap < MIN_POST_GAP:
        remaining = int(MIN_POST_GAP - gap)
        print(f"⏳ Trend post: skipping — last post was {int(gap)}s ago (need {remaining}s more)")
        return False

    if not _is_in_posting_window():
        from datetime import timezone, timedelta
        wat_hour = datetime.now(timezone(timedelta(hours=1))).strftime("%H:%M")
        print(f"⏰ Outside posting window ({wat_hour} WAT) — trend post skipped (resumes at {POSTING_HOURS_WAT[0]:02d}:00)")
        return False

    # Scrape trending across Nigeria and USA
    trends = scrape_multi_region(page)
    if not trends:
        print("⚠️  No trends scraped — will try again next cycle")
        return False

    print(f"\n📈 {len(trends)} trends found:")
    for i, t in enumerate(trends[:12], 1):
        marker = "⚽" if t.score >= 60 else ("🇳🇬" if t.region == "Nigeria" else "🇺🇸")
        posts = _parse_post_count(t.count)
        post_label = f"{posts:,} posts" if posts else t.count
        print(f"  {i:2}. {marker} [{t.region}] {t.name}  {post_label}")

    # Pick best trend
    trend = pick_trend(trends, state.get("used_trends", []))
    if not trend:
        print("⚠️  No unused trends available")
        return False

    cat = classify_trend(trend.name)
    print(f"\n🎯 Selected: '{trend.name}' (category: {cat}, score: {trend.score}, region: {trend.region})")

    # Fetch what people are actually saying about this trend on X
    print(f"  → Fetching context from live X search …")
    context_text = fetch_trend_context(page, trend.name)

    # Harvest images from the X search page we just loaded (no extra navigation)
    fetched_images = fetch_images_from_trend_page(page, cat)

    # Web research: understand WHY it's trending (news, events, announcements)
    research = research_trend(trend.name, region=trend.region)
    if research:
        # Prepend news context so the AI understands the real reason before seeing tweets
        context_text = (
            f"[WHY IT'S TRENDING — web research]\n{research}\n\n"
            f"[WHAT PEOPLE ARE SAYING ON X]\n{context_text}"
            if context_text else
            f"[WHY IT'S TRENDING — web research]\n{research}"
        )

    # Detect what angle the discussion is taking
    combined    = f"{trend.name} {context_text}"
    post_type   = detect_post_type(combined)
    print(f"  → Detected post_type: {post_type}")

    tweet = generate_tweet(trend.name, context_text, trend.count, region=trend.region)
    print(f'📝 Generated tweet:\n   "{tweet}"')

    # If X scraping got nothing, try DuckDuckGo image search as fallback
    if not fetched_images:
        fetched_images = fetch_images_from_web(trend.name, cat)
    _prune_image_cache()

    # Priority: freshly fetched images → static curated folder
    image_path = fetched_images[0] if fetched_images else pick_image(post_type, cat)
    if image_path:
        print(f"  → Image: {Path(image_path).name}")

    if dry_run:
        print("\n[DRY-RUN] No post made.")
        return False

    # Wait for / acquire the posting lock
    if not wait_for_lock("trend_poster"):
        return False

    try:
        success = post_tweet(page, tweet, image_path=image_path)
    finally:
        release_lock()

    if success:
        now = time.time()
        state["posts_today"] = state.get("posts_today", 0) + 1
        state["last_post_at"] = now
        used = state.get("used_trends", [])
        # Store normalized name to prevent near-duplicate usage
        used.append(_normalize_trend_name(trend.name))
        state["used_trends"] = used[-60:]   # keep last 60
        save_state(state)
        update_daily_log("posts_made")

    return success


def run_loop(page, dry_run: bool = False, once: bool = False):
    print("\n🚀 Trend Poster started — monitoring X trending every 30 minutes")
    print(f"   Daily cap : {MAX_DAILY_POSTS} posts")
    print(f"   Interval  : {POST_INTERVAL_SECONDS // 60} min (peak hours: {PEAK_INTERVAL_SECONDS // 60} min)")
    print(f"   Goal feed : checks @ChampionsLeague + @brfootball (CL only) every {GOAL_CHECK_INTERVAL // 60} min")
    print(f"   Dry-run   : {dry_run}")
    print("=" * 55)

    cycle_num = 0
    while True:
        cycle_num += 1
        print(f"\n{'=' * 55}")
        print(f"🔄 CYCLE {cycle_num}  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print('=' * 55)

        try:
            run_cycle(page, dry_run=dry_run)
        except Exception as e:
            _tlog.error(f"Cycle #{cycle_num} crashed: {e}", exc_info=True)
            print(f"\n  ❌ Cycle #{cycle_num} error: {e} — recovering...")
            try:
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15_000)
                time.sleep(3)
            except Exception:
                pass

        if once:
            print("\n✅ --once flag set. Exiting.")
            break

        # Use shorter interval during peak hours WAT
        from datetime import timezone, timedelta as _td
        _wat_now = datetime.now(timezone(_td(hours=1))).hour
        _base_interval = PEAK_INTERVAL_SECONDS if _wat_now in PEAK_HOURS_WAT else POST_INTERVAL_SECONDS
        interval = _base_interval + random.randint(-180, 300)  # ±3-5 min jitter
        interval = max(interval, MIN_POST_GAP)  # never below min gap
        if _wat_now in PEAK_HOURS_WAT:
            print(f"  ⚡ Peak hour ({_wat_now:02d}:xx WAT) — posting in ~{interval // 60} min")

        # During the wait, check for goals and live game updates
        wait_start = time.time()
        next_time = datetime.now() + timedelta(seconds=interval)
        print(f"\n⏰ Next main cycle at {next_time.strftime('%H:%M:%S')} "
              f"({interval // 60} min)")

        last_goal_check = 0.0

        while time.time() - wait_start < interval:
            time.sleep(60)
            elapsed = time.time() - wait_start
            if interval - elapsed <= 0:
                break

            now = time.time()
            state = get_state_for_today(load_state())

            # Goal feed check — every 3 minutes
            if now - last_goal_check >= GOAL_CHECK_INTERVAL:
                last_goal_check = now
                print(f"\n⚽ Goal feed check — {datetime.now().strftime('%H:%M:%S')}")
                posted = try_post_goal_reaction(page, state, dry_run=dry_run)
                if posted:
                    continue  # just posted, reload state next iteration

            # Follow-for-follow post — once per day during evening peak (20:00-21:00 WAT)
            from datetime import timezone, timedelta as _td2
            _wat_h = datetime.now(timezone(_td2(hours=1))).hour
            if _wat_h == 20 and not state.get("f4f_posted_today"):
                gap2 = _time_since_last_post(state)
                if gap2 >= MIN_POST_GAP:
                    if not _is_in_posting_window():
                        pass
                    else:
                        f4f_tweet = generate_follow_for_follow_post()
                        print(f"\n👥 F4F post — {datetime.now().strftime('%H:%M:%S')}")
                        print(f'   "{f4f_tweet}"')
                        if not dry_run:
                            if wait_for_lock("trend_poster"):
                                try:
                                    ok = post_tweet(page, f4f_tweet)
                                finally:
                                    release_lock()
                                if ok:
                                    state["f4f_posted_today"] = True
                                    state["last_post_at"] = time.time()
                                    state["posts_today"] = state.get("posts_today", 0) + 1
                                    save_state(state)
                                    update_daily_log("posts_made")
                                    print("  ✅ F4F posted")





# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Trend Poster — Every 30-minute automated tweet about X trending topics")
    parser.add_argument("--dry-run", action="store_true", help="Scrape + generate but don't post")
    parser.add_argument("--once",    action="store_true", help="Run one cycle then exit")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (no window)")
    args = parser.parse_args()

    if not USERNAME or not PASSWORD:
        print("❌ TWITTER_USERNAME / TWITTER_PASSWORD not set in .env")
        sys.exit(1)

    print("\n🤖 TREND POSTER")
    print("=" * 55)
    print(f"  Account  : {USERNAME}")
    print(f"  Profile  : {PROFILE_DIR}")
    print(f"  Dry-run  : {args.dry_run}")
    print("=" * 55)

    DATA_DIR.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        # Anti-detection: randomize viewport + disable automation flags
        _vw = random.choice([1280, 1366, 1440, 1536])
        _vh = random.choice([800, 864, 900, 960])
        context = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=args.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled",
            ],
            viewport={"width": _vw, "height": _vh},
        )
        if Stealth:
            Stealth().apply_stealth_sync(context)
            print("  Stealth  : enabled")

        page = context.new_page() if not context.pages else context.pages[0]
        page.set_default_timeout(20_000)

        # Login if needed
        if not is_logged_in(page):
            if not login(page):
                print("❌ Login failed — cannot continue")
                context.close()
                sys.exit(1)

        # NOTE: settings/explore times out on this profile; Nigeria location is
        # handled per-cycle via WOEID URL in change_trending_location().

        try:
            run_loop(page, dry_run=args.dry_run, once=args.once)
        except KeyboardInterrupt:
            print("\n\n⛔ Stopped by user")
        finally:
            release_lock()   # always release the lock on exit
            context.close()


if __name__ == "__main__":
    main()
