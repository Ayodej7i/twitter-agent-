"""
Playwright Fast Replier
- Continuously monitors a list of big creator accounts
- The moment they post something new, generates a context-aware reply and posts it
- Being one of the first to reply = maximum visibility on their post
- No manual input required
"""

import time
import random
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

try:
    from utils.safe_io import read_json, write_json
    from utils.bot_logger import get_logger
    _flog = get_logger("fast_replier")
except Exception:
    import logging as _logging
    _flog = _logging.getLogger("fast_replier")
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

try:
    from ai_reply import generate_ai_reply, update_daily_log, log_style_performance
    _AI_ENABLED = True
except ImportError:
    _AI_ENABLED = False
    def update_daily_log(*a, **kw): pass       # no-op fallback
    def log_style_performance(*a, **kw): pass  # no-op fallback

try:
    from config.persona import MONITORED_ACCOUNTS as _PERSONA_ACCOUNTS
    from config.persona import JAPANESE_ACCOUNTS as _PERSONA_JA
except ImportError:
    _PERSONA_ACCOUNTS = None
    _PERSONA_JA = None

# ─────────────────────────────────────────────────────────────────────────────
# Anti-detection — rotate user-agents to avoid fingerprinting
# ─────────────────────────────────────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

def _pick_user_agent() -> str:
    """Pick a random user-agent for this session."""
    return random.choice(_USER_AGENTS)

# ─────────────────────────────────────────────────────────────────────────────
# Config — edit these to match your notification accounts
# ─────────────────────────────────────────────────────────────────────────────

USERNAME    = os.getenv("TWITTER_USERNAME", "")
PASSWORD    = os.getenv("TWITTER_PASSWORD", "")
PROFILE_DIR = str(Path(__file__).parent / "data" / "browser_profile_pw")
STATE_FILE          = str(Path(__file__).parent / "data" / "fast_reply_state.json")
PERFORMANCE_FILE    = str(Path(__file__).parent / "data" / "reply_performance.json")
ACCOUNT_MEMORY_FILE = str(Path(__file__).parent / "data" / "account_memory.json")
HOURLY_PERF_FILE    = str(Path(__file__).parent / "data" / "hourly_performance.json")
SEEN_MENTIONS_FILE  = str(Path(__file__).parent / "data" / "seen_mentions.json")

# Accounts to monitor — loaded from config/persona.py (edit there to customise)
MONITORED_ACCOUNTS = _PERSONA_ACCOUNTS if _PERSONA_ACCOUNTS else [
    # Fallback — add handles here or configure in config/persona.py
]

# Accounts that receive Japanese-language replies only
JAPANESE_ACCOUNTS = _PERSONA_JA if _PERSONA_JA else set()

# How often to check each account (seconds). Lower = faster but riskier.
CHECK_INTERVAL_BASE = 75   # base seconds between cycles (was 60)
CHECK_JITTER        = 30   # +/- random jitter added each cycle
# Max age of a tweet to still reply to (don't reply to old posts)
MAX_TWEET_AGE_MINUTES = 30
# Max replies per account per HOUR (safety cap) — reduced from 4 to avoid detection
MAX_REPLIES_PER_ACCOUNT_PER_HOUR = 2
# Max gain train replies per hour GLOBALLY (prevents flooding the TL with F4F noise)
MAX_GAIN_TRAINS_PER_HOUR = 5

def _jittered_interval() -> int:
    """Return CHECK_INTERVAL with random jitter so timing is never predictable."""
    return CHECK_INTERVAL_BASE + random.randint(-CHECK_JITTER, CHECK_JITTER)

# ── VIP double-reply config ──────────────────────────────────────────────────
# Accounts eligible for a second, more aggressive reply on high-impression posts
VIP_ACCOUNTS = [
    # Add accounts eligible for double-reply on high-impression posts
    # "@SomeBigAccount",
]
# Minimum impressions before we fire the second reply
IMPRESSION_THRESHOLD = 3000

# Reply style category per account: 'football' | 'general' | '' (all styles)
ACCOUNT_TONE_MAP: dict[str, str] = {
    # Map each monitored account to a tone. Example:
    # "@SomeFootballAccount": "football",
    # "@SomeGeneralAccount": "general",
}

# ─── Rival fan accounts — add any account you discover supports a rival club ─
# The bot will use rival banter mode (root for their opponents) when replying.
# Supported clubs: "arsenal" | "liverpool" | "man_city" | "chelsea" |
#                  "aston_villa" | "tottenham" | "newcastle" | "everton"
RIVAL_FAN_ACCOUNTS: dict[str, str] = {
    # Map accounts to the rival club they support. Example:
    # "@SomeAccount": "chelsea",
    # "@AnotherAccount": "arsenal",
}

# Clubs competing with your team for top spots — when they play, root against them
TOP4_RIVALS: frozenset = frozenset({
    # "liverpool", "arsenal", "chelsea", "tottenham",
})

# Fan accounts that support your team — agree & build on their takes.
UNITED_FAN_ACCOUNTS: frozenset = frozenset({
    # "@TeammateFanAccount1",
    # "@TeammateFanAccount2",
})

# Neutral accounts — reply with elite, factual, analytical takes.
NEUTRAL_ELITE_ACCOUNTS: frozenset = frozenset({
    # "@JournalistAccount",
    # "@TransferNewsAccount",
})

# Meme / troll accounts — reply with banter, memes, club jokes.
MEME_BANTER_ACCOUNTS: frozenset = frozenset({
    # "@TrollFootball",
})


# ─────────────────────────────────────────────────────────────────────────────
# Topic + sentiment detection (same heuristics as notification responder)
# ─────────────────────────────────────────────────────────────────────────────

# Posts we never reply to (promotional / spam / private conversations)
SKIP_PATTERNS = [
    r'click the link',
    r'join my',
    r'group chat',
    r'subscribe',
    r'promo code',
    r'use code',
    r'affiliate',
    r'dm (me|us) for',
    r'follow (me|us) for',
    r'giveaway',
    r'win a',
    r'\blink in bio\b',
    r'\bonly fans\b',
    r'@grok\b',           # Grok AI queries
    r'@chatgpt\b',        # ChatGPT queries
    r'hey @',             # Directed "hey @someone" posts
    r'^@\w+,',            # Starts with "@handle,"
]

# ─────────────────────────────────────────────────────────────────────────────
# Sensitive topic filter — skip tweets about death, tragedy, illness, heavy politics
# Engaging with these risks PR disaster or looks tone-deaf
# ─────────────────────────────────────────────────────────────────────────────
_SENSITIVE_PATTERNS = [
    # Death / RIP
    r'\brip\b', r'\brest in peace\b', r'\bpassed away\b', r'\bhas died\b', r'\bhas passed\b',
    r'\bdeath\b', r'\bdied\b', r'\bfuneral\b', r'\bgrieve\b', r'\bcondolences\b',
    r'\bin memory of\b', r'\bdeceased\b', r'\bgone too soon\b', r'\brest easy\b',
    # Serious illness / health
    r'\bcancer\b', r'\btumour\b', r'\btumor\b', r'\bdiagnosed with\b', r'\bin hospital\b',
    r'\bintensive care\b', r'\bicu\b', r'\blife support\b', r'\bstroke\b', r'\bheart attack\b',
    # Heavy politics (Nigeria-sensitive)
    r'\belection rigging\b', r'\bcoup\b', r'\bgovernment crackdown\b', r'\barrest(ed)? by\b',
    r'\bprotester(s)? (shot|killed|dead)\b', r'\bmassacre\b', r'\bgenocide\b',
    # Sexual / explicit
    r'\bsex tape\b', r'\bnude\b', r'\bporn\b', r'\bonlyfans\b',
    # Suicide / self-harm
    r'\bsuicid\b', r'\bself.harm\b', r'\btook (his|her|their) (own )?life\b',
    r"\bcan'?t go on\b", r'\bwant to (die|end it)\b',
    # Grief / personal bereavement
    r'\bgrieving\b', r'\bin mourning\b',
    r'\blost (my|his|her|their) (dad|mum|mom|mother|father|brother|sister|son|daughter|wife|husband|grandm|grandp)\b',
    r'\bmy (dad|mum|mom|mother|father|brother|sister|son|daughter|wife|husband).{0,30}(passed|died|gone|left us)\b',
    # Mental health crisis
    r'\bmental breakdown\b', r'\bhaving a breakdown\b', r'\bbreaking down\b',
    # Religious holidays / greetings — off-brand for a football banter account
    r'\beid mubarak\b', r'\beid al.?(fitr|adha)\b', r'\bramadan\s*(mubarak|kareem)\b',
    r'\bhappy eid\b', r'\bblessed eid\b', r'\bhappy ramadan\b',
    r'\bmerry christmas\b', r'\bhappy easter\b', r'\bhappy diwali\b', r'\bhappy hanukkah\b',
    # War / humanitarian crises — too sensitive for banter replies
    r'\bceasefire\b', r'\brafah\b', r'\bhumanitarian crisis\b', r'\bbombing\b',
    r'\bmass shooting\b', r'\bschool shooting\b',
]

def is_sensitive(text: str) -> bool:
    """Return True if tweet touches death, serious illness, suicide, or heavy politics."""
    t = text.lower()
    return any(re.search(pat, t) for pat in _SENSITIVE_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Tweet relevance scoring — prioritise tweets the bot can reply to well
# ─────────────────────────────────────────────────────────────────────────────

_FOOTBALL_KW = re.compile(
    r'\b(united|mufc|utd|glazer|ineos|ten hag|tenhag|amorim|rashford|bruno|hojlund|h[øo]jlund|'
    r'mount|mainoo|garnacho|casemiro|dalot|onana|martinez|diallo|eriksen|antony|'
    r'old trafford|premier league|epl|champions league|ucl|europa|fa cup|carabao|'
    r'transfer|signing|bid|deal|loan|contract|release clause|'
    r'football|soccer|offside|penalty|var|goal|assist|clean sheet|'
    r'arsenal|liverpool|city|chelsea|spurs|tottenham|newcastle|villa|everton|'
    r'manager|sacked|appointed|formation|tactics|lineup|starting xi|'
    r'injury|injured|fitness|setback|comeback|'
    r'matchday|kick.off|half.time|full.time|ft|ht|'
    r'man (of the match|utd)|motm|potm|poty)\b', re.IGNORECASE
)

_QUESTION_RE = re.compile(r'\?\s*$|who should|what do you think|your thoughts|rate this|agree or disagree', re.IGNORECASE)


def score_tweet_relevance(text: str, account: str, like_count: int, age_minutes: float) -> int:
    """Score a tweet 0-100 for reply priority. Higher = reply first."""
    score = 50  # baseline
    t = text.lower()

    # Football content — our bread and butter
    football_hits = len(_FOOTBALL_KW.findall(t))
    score += min(football_hits * 8, 30)  # up to +30

    # Questions invite engagement
    if _QUESTION_RE.search(text):
        score += 12

    # Hot takes / opinion markers
    if any(w in t for w in ["unpopular opinion", "hot take", "controversial", "debate", "overrated", "underrated"]):
        score += 10

    # High-engagement tweets (likes as proxy)
    if like_count >= 1000:
        score += 15
    elif like_count >= 200:
        score += 10
    elif like_count >= 50:
        score += 5

    # Fresher is better
    if age_minutes < 5:
        score += 10
    elif age_minutes < 10:
        score += 5

    # Longer tweets usually have more to engage with
    word_count = len(text.split())
    if word_count >= 20:
        score += 5
    elif word_count < 5:
        score -= 10  # very terse, hard to reply meaningfully

    # VIP account bonus
    if account in VIP_ACCOUNTS:
        score += 8

    return max(0, min(100, score))


def should_skip(text: str, account: str) -> Optional[str]:
    """Return a reason string if this post should not be replied to, else None."""
    t = text.lower().strip()
    # Directed at another specific account (starts with @handle that isn't us)
    if t.startswith('@') and not t.lower().startswith(f'@{USERNAME.lower()}'):
        return "directed at another account"
    # Sensitive topics — death, illness, suicide, heavy politics
    if is_sensitive(text):
        return "sensitive topic"
    # Promotional / spam
    for pat in SKIP_PATTERNS:
        if re.search(pat, t):
            return f"promo/spam pattern: {pat}"
    # Too short to be meaningful
    if len(t) < 15:
        return "too short"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Gain train detection — follow trains, mutual follows, RT-for-follow posts
# These are growth opportunities: jump in with a short participatory reply
# ─────────────────────────────────────────────────────────────────────────────

GAIN_TRAIN_PATTERNS = [
    r'follow\s*everyone\s*(who|that)',
    r'follow(ing)?\s*(all|everyone|everybody)\s*(back|who|that)',
    r'(follow|rt|retweet)\s*(train|spree)',
    r'gain\s*train',
    r'mutual\s*follow',
    r"let.s\s*(all\s*)?follow\s*(each other|everyone)",
    r'follow\s*(4|for)\s*follow',
    r'\bf4f\b',
    r'\bfff\b',
    r'follow\s*back\s*(everyone|all)',
    r'(like|rt|retweet)\s*(and|&|,)\s*follow\s*everyone',
    r"reply\s*(and|to)\s*(i.ll|i will|we.ll)\s*follow",
    # Engagement / drop-your-@ posts
    r'drop\s*(your|ur)\s*@',
    r'drop\s*(your|ur)\s*(handle|username|user)',
    r'comment\s*(your|ur)\s*@',
    r'comment\s*(your|ur)\s*(handle|username|user)',
    r'post\s*(your|ur)\s*@',
    r'(leave|drop|comment)\s*(your|ur)\s*(@ ?below|link below)',
    r'small\s*account.*follow',
    r'follow.*small\s*account',
    r'small\s*account.*connect',
    r'small\s*account.*together',
    r'(let.s|lets)\s*(connect|network|grow).*follow',
    r'follow.*let.s\s*(connect|grow|network)',
    r'(grow|build).*together.*follow',
    r'support\s*(small|each other).*follow',
    r'reply\s*(with|and drop)\s*your\s*@',
    r'i.ll\s*follow\s*(back|everyone)',
    r'everyone\s*(who\s*)?(retweet|rt|replies|comment)',
    # "Say hello / let's connect" posts
    r"(say|drop|just say)\s*(hello|hi|hey|holla)",
    r"let.s\s*connect",
    r"connect\s*(with\s*(me|each other|you|us)|together)",
    r"never stop growing",
    r"growing your account",
    r"bro to bro",
    r"(hello|hi|hey).{0,30}(follow|connect|grow)",
    r"(follow|connect|grow).{0,30}(hello|hi|hey)",
    r"introduce\s*yourself",
    r"(who|what).{0,20}(follow|account).{0,20}(here|tonight|today)",
    r"drop\s*(a\s*)?(follow|comment|reply).*connect",
]

GAIN_TRAIN_REPLIES = [
    f"Hello! 👋 @{USERNAME} here — daily football takes ⚽ Follow me and I'll follow back everyone in this thread!",
    f"Hello from @{USERNAME}! 🙌 I post hot takes daily — follow me, I'm following back all!",
    f"In! @{USERNAME} — fan posting 🔥 takes every day. Drop a follow, following back everyone here 🔄",
    f"Hello! 👋 I'm @{USERNAME}, posting content daily. Follow me and I follow back — let's grow together!",
    f"Hey! @{USERNAME} here ⚽ Follow me for hot takes — I'm following back everyone who replies 🤝",
    f"Hello! @{USERNAME} — I post daily content ⚽⚽ Follow me, I follow back all!",
    f"In the building! @{USERNAME} here — hot takes, transfer news, all the drama ⚽ Following back everyone 🔄",
    f"Hello! 👋 @{USERNAME} — fan account. Follow for follow, I'm following back all in this thread!",
    f"Hey! I'm @{USERNAME} ⚽ Posting takes that'll get you going. Follow me — following back everyone here!",
    f"Hello from @{USERNAME}! I post content daily, no filter ⚽🔥 Follow me, I follow back — let's connect!",
]


def is_gain_train(text: str) -> bool:
    """Return True if this is a follow/gain train post worth participating in."""
    t = text.lower()
    return any(re.search(pat, t) for pat in GAIN_TRAIN_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Topic detection — enriches AI prompt with relevant facts
# Each entry: list of keywords → context string fed to the AI
# ─────────────────────────────────────────────────────────────────────────────

TOPIC_CONTEXT: list[tuple[list[str], str]] = [
    # Ownership / board
    (["glazer", "glazers", "ineos", "ratcliffe", "sir jim", "ownership", "shareholders", "dividends"],
     "Context: Club ownership context — customise for your team. "
     "Fans have protested the Glazers for years over debt loading and lack of investment."),

    # Current manager
    (["carrick", "michael carrick"],
     "Context: Current manager context — customise for your team. "
     "Carrick previously managed Middlesbrough. United are 3rd in the PL (54 pts), Arsenal lead by 16 pts."),

    # Amorim (previous manager, still discussed)
    (["amorim", "ruben amorim"],
     "Context: Former manager context — customise for your team. "
     "He came from Sporting CP with a 3-4-3 system that never clicked at United."),

    # Rashford (on loan)
    (["rashford", "marcus rashford"],
     "Context: Marcus Rashford is on loan at Barcelona since summer 2025. "
     "He fell out with Amorim at United and hasn't played for the club since. His future at United is uncertain."),

    # Garnacho (sold)
    (["garnacho", "alejandro garnacho"],
     "Context: Alejandro Garnacho was sold to Chelsea in summer 2025 for around £50m. "
     "Many United fans were upset about the sale given his age and potential."),

    # Top scorers / key attackers
    (["mbeumo", "bryan mbeumo"],
     "Context: Bryan Mbeumo joined United in 2025 and is one of their top scorers this season. "
     "He has been bright in an otherwise difficult campaign."),

    (["sesko", "šeško", "benjamin sesko"],
     "Context: Benjamin Šeško joined United in 2025 from RB Leipzig. "
     "He is joint top scorer alongside Mbeumo and has shown promise as a clinical finisher."),

    (["casemiro"],
     "Context: Casemiro is 34 and still at United. Remarkably he has scored 7 league goals this season. "
     "His contract situation and future beyond this season is uncertain."),

    (["mainoo", "kobbie mainoo"],
     "Context: Kobbie Mainoo is a key United midfielder, homegrown from the academy. "
     "He is one of the brightest spots in the current squad."),

    (["fernandes", "bruno fernandes"],
     "Context: Bruno Fernandes is still United's captain and creative hub. "
     "He has been consistent despite the team's struggles."),

    (["dorgu", "patrick dorgu"],
     "Context: Patrick Dorgu joined United in January 2025 from Lecce. "
     "He plays at left back and has impressed with his athleticism."),

    # Defenders
    (["de ligt", "matthijs de ligt"],
     "Context: Matthijs de Ligt is a United centre-back signed from Bayern Munich in 2024. "
     "He has been inconsistent and faces competition from Leny Yoro."),

    (["yoro", "leny yoro"],
     "Context: Leny Yoro is a highly-rated young French centre-back signed from LOSC Lille in 2024. "
     "He suffered a serious injury early on but is now back and seen as a key future asset."),

    # Results / league position
    (["title race", "top four", "top 4", "third place", "54 pts", "league position", "table"],
     "Context: Your team's current league position — update regularly. "
     "Arsenal lead the league by 16 points. United are not in the title race. "
     "They have no European football this season."),

    # No Europe
    (["ucl", "champions league", "europa league", "european", "europe"],
     "Context: European competition status — customise for your team. "
     "They finished outside the top 4 last season. Speak as a fan — 'we'."),

    # ACL / injury disambiguation — ACL = knee injury, NOT Champions League
    (["acl", "anterior cruciate", "tear acl", "acl tear", "acl injury", "ruptured acl"],
     "Context: ACL stands for Anterior Cruciate Ligament — a serious knee injury that typically "
     "sidelines a player for 9-12 months. This is an INJURY topic, NOT related to the Champions League. "
     "UCL/CL = Champions League. ACL = knee ligament injury. React with empathy or a take on the injury impact."),

    # Naija music — strong opinions ready
    (["burna boy", "burnaboy", "burna"],
     "Context: Burna Boy is a Nigerian Afrobeats superstar, Grammy winner, known as the African Giant. "
     "He's from Port Harcourt. Hot take territory — passionate Nigerian fans have strong views."),
    (["wizkid", "wiz kid", "starboy"],
     "Context: Wizkid is a Nigerian Afrobeats legend, collaborated with Drake, Beyonce. "
     "Massive global following. Huge debates between Wizkid vs Davido fans online."),
    (["davido", "david adeleke", "001"],
     "Context: Davido is a Nigerian Afrobeats star, known as OBO (Omo Baba Olowo). "
     "Massive Nigerian fan base. The Wizkid vs Davido rivalry is one of the biggest debates in Nigerian Twitter."),
    (["rema", "calm down", "benin city"],
     "Context: Rema is a young Nigerian Afrobeats artist from Benin City, known for 'Calm Down'. "
     "Huge global hit. Rising star in the Afrobeats scene."),
    (["asake", "amapiano", "afrobeats", "afropop"],
     "Context: Nigerian Afrobeats is dominating global music in 2025-26. "
     "Strong Nigerian Twitter culture around music — take strong opinions."),

    # DSTV / SuperSport / Nigerian football context
    (["dstv", "supersport", "super sport"],
     "Context: DStv/SuperSport is the main way Nigerians watch football — it's a big deal when games "
     "aren't showing or when subscription prices go up. Nigerians have strong feelings about this."),
    (["npfl", "nigerian premier league", "enyimba", "rangers", "sunshine stars"],
     "Context: Nigerian Premier League (NPFL) is the top Nigerian football league. "
     "Mention as an aside — most fans follow European football more closely."),
    (["super eagles", "nigeria national team", "eagles"],
     "Context: The Super Eagles are Nigeria's national football team. "
     "They qualify for AFCON regularly. Nigerian fans are very passionate — especially about AFCON performances."),
    (["afcon", "africa cup", "cup of nations"],
     "Context: AFCON (Africa Cup of Nations) is the biggest African football tournament. "
     "Nigeria's Super Eagles are always involved. Very high-emotion topic for Nigerian football fans."),
    # History / legacy — customise for your team
    # (["ferguson", "sir alex", "treble", "1999", "class of 92", "glory days", "golden era"],
    #  "Context: Sir Alex Ferguson's era (1986-2013) brought United 13 league titles."),

    # Your team / fan perspective — add your team's keywords
    # (["your team", "your abbreviation", "your stadium"],
    #  "Context: You are replying as a fan. Use WE/US/OUR — never 'they' or 'them' for your team."),
]


def detect_topic_context(tweet_text: str) -> str:
    """
    Match the tweet against TOPIC_CONTEXT keywords.
    Returns a context string to enrich the AI prompt, or '' if no match.
    """
    t = tweet_text.lower()
    for keywords, context in TOPIC_CONTEXT:
        if any(kw in t for kw in keywords):
            return context
    return ""


def generate_reply(tweet_text: str, account: str, style_category: str = "",
                   avoid_openers: list = None, avoid_phrases: list = None,
                   parent_tweet: str = "", quoted_tweet: str = "",
                   language: str = "", rival_fan: str = "",
                   is_united_fan: bool = False,
                   is_neutral_elite: bool = False,
                   is_meme_banter: bool = False,
                   image_urls: list = None, top_replies: str = "",
                   account_profile: str = "") -> str | None:
    """Read the tweet, generate a fresh contextual reply via AI."""
    # Gain train posts get a short participatory reply — no AI needed
    if is_gain_train(tweet_text):
        return random.choice(GAIN_TRAIN_REPLIES)
    if _AI_ENABLED:
        # Enrich the AI prompt with relevant topic facts when the tweet is about United
        topic_ctx = detect_topic_context(tweet_text)

        ai = generate_ai_reply(
            tweet_text,
            context=topic_ctx,
            account=account,
            style_category=style_category,
            avoid_openers=avoid_openers or [],
            avoid_phrases=avoid_phrases or [],
            parent_tweet=parent_tweet,
            quoted_tweet=quoted_tweet,
            language=language,
            rival_fan=rival_fan,
            is_united_fan=is_united_fan,
            is_neutral_elite=is_neutral_elite,
            is_meme_banter=is_meme_banter,
            image_urls=image_urls or [],
            top_replies=top_replies,
            account_profile=account_profile,
        )
        if ai:
            return ai
    return None  # both AI models failed — skip reply rather than post garbage


# State persistence — tracks last seen tweet per account + daily reply counts
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return read_json(STATE_FILE, {})


def save_state(state: dict):
    write_json(STATE_FILE, state)


# ─────────────────────────────────────────────────────────────────────────────
# Reply performance tracking
# Logs the impression count of tweets we replied to, keyed by account + type.
# Used to re-order MONITORED_ACCOUNTS so the most valuable accounts are
# checked first in each cycle.
# ─────────────────────────────────────────────────────────────────────────────

def load_performance() -> dict:
    return read_json(PERFORMANCE_FILE, {})


def log_reply_performance(account: str, impressions: int, post_type: str):
    """Record impressions of the original tweet we replied to (proxy for our reply's exposure)."""
    if impressions <= 0:
        return
    perf = load_performance()
    if account not in perf:
        perf[account] = {"impressions": [], "post_types": [], "avg": 0}
    entry = perf[account]
    entry["impressions"].append(impressions)
    entry["post_types"].append(post_type)
    entry["impressions"] = entry["impressions"][-50:]   # keep last 50
    entry["post_types"]  = entry["post_types"][-50:]
    entry["avg"] = int(sum(entry["impressions"]) / len(entry["impressions"]))
    write_json(PERFORMANCE_FILE, perf)


def log_engagement(account: str):
    """Record that a monitored account engaged with us (mentioned or replied back). Item 7."""
    perf = load_performance()
    if account not in perf:
        perf[account] = {"impressions": [], "post_types": [], "avg": 0}
    entry = perf[account]
    entry["engagements"] = entry.get("engagements", 0) + 1
    entry["last_engagement"] = datetime.now().isoformat()[:16]
    write_json(PERFORMANCE_FILE, perf)


def log_hourly_performance(impressions: int):
    """Log impressions by hour-of-day to find peak engagement windows. Item 8."""
    if impressions <= 0:
        return
    hour = str(datetime.now().hour)
    try:
        data = read_json(HOURLY_PERF_FILE, {})
        entry = data.setdefault(hour, {"count": 0, "total_imp": 0, "avg": 0})
        entry["count"] += 1
        entry["total_imp"] += impressions
        entry["avg"] = entry["total_imp"] // entry["count"]
        write_json(HOURLY_PERF_FILE, data)
    except Exception:
        pass


# ─── Per-account memory (item 1) ─────────────────────────────────────────────
# Infer club + topic interests from tweets to personalise future replies
_CLUB_KEYWORDS: dict[str, list] = {
    # "your team": ["abbreviation", "nickname", "stadium", "key players"]  # Add your team,
    "real madrid":       ["real madrid", "bernabeu", "ancelotti", "vinicius", "bellingham", "mbappe"],
    "barcelona":         ["barcelona", "barca", "yamal", "lewandowski", "nou camp"],
    "arsenal":           ["arsenal", "gunners", "saka", "arteta", "odegaard"],
    "liverpool":         ["liverpool", "anfield", "salah", "slot", "van dijk"],
    "man city":          ["man city", "etihad", "haaland", "guardiola", "foden"],
    "chelsea":           ["chelsea", "stamford", "palmer", "boehly"],
    "psg":               ["psg", "paris saint-germain", "parc des princes"],
    "juventus":          ["juventus", "juve", "allianz stadium"],
}
_TOPIC_KEYWORDS: list[str] = [
    "transfer", "signing", "manager", "coach", "tactics", "formation",
    "champions league", "premier league", "la liga", "serie a", "bundesliga",
    "injury", "comeback", "hattrick", "assist",
    "ballon d'or", "goat", "messi", "ronaldo", "history",
    "stats", "analysis", "highlight", "match",
]

def load_account_memory() -> dict:
    return read_json(ACCOUNT_MEMORY_FILE, {})

def save_account_memory(memory: dict):
    write_json(ACCOUNT_MEMORY_FILE, memory)

def update_account_memory(account: str, tweet_text: str):
    """Infer club + topic interests from the tweet and persist to account_memory.json."""
    memory = load_account_memory()
    entry = memory.setdefault(account, {
        "interactions": 0,
        "inferred_club": "",
        "topics": [],
        "last_updated": "",
    })
    entry["interactions"] = entry.get("interactions", 0) + 1
    t = tweet_text.lower()
    # Update club inference — first club whose keywords appear wins
    for club, kws in _CLUB_KEYWORDS.items():
        if any(kw in t for kw in kws):
            entry["inferred_club"] = club
            break
    # Accumulate topic tags (cap at 15 unique)
    existing = set(entry.get("topics", []))
    for topic in _TOPIC_KEYWORDS:
        if topic in t:
            existing.add(topic)
    entry["topics"] = list(existing)[:15]
    entry["last_updated"] = datetime.now().isoformat()[:16]
    save_account_memory(memory)

def get_account_profile_hint(account: str) -> str:
    """Return a short profile string to inject into the AI prompt. Empty if <3 interactions."""
    memory = load_account_memory()
    entry = memory.get(account)
    if not entry or entry.get("interactions", 0) < 3:
        return ""
    n = entry.get("interactions", 0)
    parts = [f"{n} prior interactions"]
    if entry.get("inferred_club"):
        parts.append(f"likely {entry['inferred_club'].title()} fan")
    if entry.get("topics"):
        parts.append(f"usually talks about: {', '.join(entry['topics'][:4])}")
    hint = f"ACCOUNT PROFILE ({account} — {', '.join(parts)})."
    # Add continuity guidance based on interaction count
    if n >= 10:
        hint += " You've engaged with this account many times — feel free to escalate tone, call back a running argument, or go more aggressive. Don't restart from zero."
    elif n >= 5:
        hint += " You've talked before — skip pleasantries, get straight to the take."
    return hint


# ─── Conversation thread tracking — reply to mentions (item 3) ───────────────
def check_mentions(page, state: dict, dry_run: bool = False):
    """Check @mentions in notifications and follow up on new replies to the bot."""
    try:
        seen = read_json(SEEN_MENTIONS_FILE, {})

        page.goto("https://x.com/notifications/mentions", wait_until="domcontentloaded", timeout=20_000)
        human_delay(2, 3)
        articles = page.query_selector_all('article[data-testid="tweet"]')
        if not articles:
            return

        replied_count = 0
        for article in articles[:10]:
            try:
                # Get tweet ID from the status link
                link_el = article.query_selector('a[href*="/status/"]')
                if not link_el:
                    continue
                href = link_el.get_attribute("href") or ""
                m = re.search(r'/status/(\d+)', href)
                if not m:
                    continue
                tweet_id = m.group(1)

                if tweet_id in seen:
                    continue  # already handled

                # Get timestamp
                time_el = article.query_selector('time')
                age_minutes = 999.0
                if time_el:
                    dt_attr = time_el.get_attribute("datetime") or ""
                    try:
                        tweet_dt = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                        now_utc = datetime.now(tweet_dt.tzinfo)
                        age_minutes = (now_utc - tweet_dt).total_seconds() / 60
                    except Exception:
                        pass
                if age_minutes > 60:       # only follow up on fresh mentions (< 1h)
                    seen[tweet_id] = "old"
                    continue

                # Get tweet text
                text_el = article.query_selector('[data-testid="tweetText"]')
                mention_text = text_el.text_content().strip() if text_el else ""
                if not mention_text or len(mention_text) < 5:
                    seen[tweet_id] = "empty"
                    continue

                # Skip sensitive/spam
                if is_sensitive(mention_text):
                    seen[tweet_id] = "sensitive"
                    continue

                # Infer which monitored account this might be from, for personality context
                author_handle = ""
                user_el = article.query_selector('[data-testid="User-Name"] a[href^="/"]')
                if user_el:
                    author_handle = "@" + (user_el.get_attribute("href") or "").lstrip("/").split("/")[0]

                full_url = f"https://x.com{href}" if href.startswith("/") else href
                account_in_monitor = author_handle if author_handle in MONITORED_ACCOUNTS else ""
                style_cat = ACCOUNT_TONE_MAP.get(account_in_monitor, "football")
                language  = "ja" if account_in_monitor in JAPANESE_ACCOUNTS else ""
                is_united_fan = account_in_monitor in UNITED_FAN_ACCOUNTS
                is_neutral_elite = account_in_monitor in NEUTRAL_ELITE_ACCOUNTS
                is_meme_banter = account_in_monitor in MEME_BANTER_ACCOUNTS
                account_profile = get_account_profile_hint(account_in_monitor) if account_in_monitor else ""

                thread_context = f"[They replied to us on Twitter. Continue the conversation naturally.]"
                reply = generate_reply(
                    mention_text,
                    account_in_monitor or "mention",
                    style_category=style_cat,
                    parent_tweet=thread_context,
                    language=language,
                    is_united_fan=is_united_fan,
                    is_neutral_elite=is_neutral_elite,
                    is_meme_banter=is_meme_banter,
                    account_profile=account_profile,
                )
                if not reply:
                    seen[tweet_id] = "no_reply"
                    continue

                print(f"\n  💬 MENTION from {author_handle}: {mention_text[:80]}")
                print(f"     Our follow-up: {reply}")

                if dry_run:
                    seen[tweet_id] = "dry_run"
                    continue

                td = TweetData(tweet_id=tweet_id, text=mention_text, url=full_url, age_minutes=age_minutes)
                success, _ = post_reply(page, td, reply)
                if success:
                    seen[tweet_id] = datetime.now().isoformat()[:16]
                    replied_count += 1
                    update_daily_log("mention_replies")
                    if account_in_monitor:
                        update_account_memory(account_in_monitor, mention_text)
                else:
                    seen[tweet_id] = "failed"

                if replied_count >= 3:   # cap at 3 mention follow-ups per cycle
                    break

            except Exception as e:
                pass

        # Prune old seen entries (keep last 500)
        if len(seen) > 500:
            items = sorted(seen.items(), key=lambda x: x[1] if isinstance(x[1], str) else "")
            seen = dict(items[-500:])

        write_json(SEEN_MENTIONS_FILE, seen)

        if replied_count:
            print(f"  💬 Mention follow-ups posted: {replied_count}")

    except Exception as e:
        print(f"  ⚠️  check_mentions error: {e}")


def sorted_accounts_by_performance() -> list:
    """Return MONITORED_ACCOUNTS sorted by avg impressions (highest first).
    Accounts with no history keep their original relative order at the end."""
    perf     = load_performance()
    tracked  = [(a, perf[a]["avg"]) for a in MONITORED_ACCOUNTS if a in perf]
    untracked = [a for a in MONITORED_ACCOUNTS if a not in perf]
    tracked.sort(key=lambda x: x[1], reverse=True)
    return [a for a, _ in tracked] + untracked


def classify_post_type(tweet_text: str, style_category: str, language: str, is_gain_train: bool) -> str:
    """Classify the tweet we replied to — used for performance breakdown reports."""
    if is_gain_train:       return "gain_train"
    if language == "ja":    return "japanese"
    t = tweet_text.lower()
    if "?" in tweet_text:
        return "football_question" if style_category == "football" else "general_question"
    if any(kw in t for kw in ["transfer", "signing", "bid", "deal", "loan", "sold", "buy"]):
        return "transfer_news"
    if style_category == "football":
        return "football_opinion"
    return "general"


def _print_performance_summary():
    """Print top accounts + best post types + engagers + peak hours."""
    perf = load_performance()
    if not perf:
        print("  📊 No performance data yet.")
        return
    print("\n  📊 PERFORMANCE — avg impressions of posts we replied to:")
    ranked = sorted(perf.items(), key=lambda x: x[1]["avg"], reverse=True)
    for i, (acc, data) in enumerate(ranked[:8], 1):
        n = len(data["impressions"])
        eng = f"  💬×{data['engagements']}" if data.get("engagements") else ""
        print(f"     {i}. {acc:<22} {data['avg']:>8,} avg  ({n} replies){eng}")
    # Aggregate by post type across all accounts
    type_totals: dict = {}
    for data in perf.values():
        for imp, pt in zip(data["impressions"], data["post_types"]):
            type_totals.setdefault(pt, []).append(imp)
    if type_totals:
        print("     ─── by post type ───")
        for pt, imps in sorted(type_totals.items(), key=lambda x: sum(x[1]) / len(x[1]), reverse=True):
            print(f"     {pt:<22} {int(sum(imps)/len(imps)):>8,} avg  ({len(imps)} samples)")
    # Engagers — accounts that replied/engaged back (item 7)
    engagers = [(acc, data.get("engagements", 0), data.get("last_engagement", ""))
                for acc, data in perf.items() if data.get("engagements", 0) > 0]
    if engagers:
        engagers.sort(key=lambda x: x[1], reverse=True)
        print("     ─── accounts that engaged back ───")
        for acc, cnt, last in engagers[:6]:
            print(f"     {acc:<22} {cnt} engagement(s)  (last: {last[:10]})")
    # Peak hours — best time-of-day to post (item 8)
    try:
        hourly = read_json(HOURLY_PERF_FILE, {})
        if len(hourly) >= 3:
            best = max(hourly.items(), key=lambda x: x[1]["avg"])
            print(f"     ─── peak hour: {best[0]}:00 → {best[1]['avg']:,} avg imp ({best[1]['count']} samples) ───")
    except Exception:
        pass
    print()


def get_account_state(state: dict, account: str) -> dict:
    if account not in state:
        state[account] = {
            "last_tweet_id": None,
            "replies_this_hour": 0,
            "gain_trains_this_hour": 0,
            "last_reply_hour": None,
            "second_replies": {},
            "pending_vip_tweets": {},
        }
    acc = state[account]
    acc.setdefault("second_replies", {})
    acc.setdefault("pending_vip_tweets", {})
    # Migrate old per-day keys if present
    if "replies_today" in acc:
        acc["replies_this_hour"] = acc.pop("replies_today")
        acc["last_reply_hour"] = acc.pop("last_reply_date", None)
    # Ensure gain_trains counter exists
    acc.setdefault("gain_trains_this_hour", 0)
    # Reset hourly counts when the clock rolls into a new hour
    current_hour = datetime.now().strftime("%Y-%m-%d-%H")
    if (acc.get("last_reply_hour") or "")[:13] != current_hour:
        acc["replies_this_hour"] = 0
        acc["gain_trains_this_hour"] = 0
        acc["last_reply_hour"] = current_hour
    return acc


# ─────────────────────────────────────────────────────────────────────────────
# Browser helpers
# ─────────────────────────────────────────────────────────────────────────────

def human_delay(lo=0.8, hi=2.0):
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
    print("🔐 Logging in...")
    try:
        page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=20_000)
        human_delay(3, 5)

        for sel in ['input[autocomplete="username"]', 'input[name="text"]']:
            try:
                f = page.wait_for_selector(sel, timeout=7_000)
                if f:
                    f.fill(USERNAME); break
            except PlaywrightTimeout:
                continue
        human_delay(1, 2)
        for sel in ['button:has-text("Next")', 'div[role="button"]:has-text("Next")']:
            btn = page.query_selector(sel)
            if btn: btn.click(); break
        else:
            page.keyboard.press("Enter")
        human_delay(2, 4)

        try:
            vf = page.wait_for_selector('input[data-testid="ocfEnterTextTextInput"]', timeout=4_000)
            if vf: vf.fill(USERNAME); page.keyboard.press("Enter"); human_delay(2, 3)
        except PlaywrightTimeout:
            pass

        for sel in ['input[name="password"]', 'input[type="password"]']:
            try:
                f = page.wait_for_selector(sel, timeout=7_000)
                if f:
                    f.fill(PASSWORD); break
            except PlaywrightTimeout:
                continue
        human_delay(1, 2)
        for sel in ['button:has-text("Log in")', 'div[role="button"]:has-text("Log in")']:
            btn = page.query_selector(sel)
            if btn: btn.click(); break
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
# Profile scraper — get the first (newest) non-pinned tweet
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TweetData:
    tweet_id: str
    text: str
    url: str
    age_minutes: float
    is_reply: bool = False    # True when this tweet is itself a reply in a thread
    parent_text: str = ""     # parent tweet text (fetched on demand)
    quoted_text: str = ""     # embedded quoted tweet text (quote tweets only)
    image_urls: list = field(default_factory=list)  # image URLs found in the tweet
    like_count: int = 0           # scraped like count for viral detection


def get_tweet_impressions(page, tweet_url: str) -> int:
    """
    Open a tweet thread page and read the view/impression count.
    Returns 0 if it can't be read.

    Strategy:
    1. aria-label on views element (most reliable on tweet detail pages)
    2. Scan tweet article inner text for "X Views" pattern
    3. data-testid="app-text-transition-container" broad fallback
    """
    try:
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=20_000)
        human_delay(2, 3)

        def _parse_num(raw: str) -> int:
            """Parse '12.5K', '1M', '123,456' etc. into an int."""
            raw = raw.strip().replace(",", "")
            m = re.match(r'^([\d]+(?:\.[\d]+)?)([KkMm]?)$', raw)
            if not m:
                return 0
            num = float(m.group(1))
            suffix = m.group(2).lower()
            if suffix == 'k':
                num *= 1_000
            elif suffix == 'm':
                num *= 1_000_000
            return int(num)

        # Strategy 1: aria-label on the views link/span (e.g. "25,430 Views")
        for sel in [
            '[aria-label*=" Views"]',
            '[aria-label*=" views"]',
        ]:
            for el in page.query_selector_all(sel):
                label = el.get_attribute("aria-label") or ""
                m = re.search(r'([\d,]+)\s*[Vv]iews', label)
                if m:
                    n = int(m.group(1).replace(",", ""))
                    if n > 0:
                        print(f"     📊 Views (aria-label): {n:,}")
                        return n

        # Strategy 2: scan the tweet article text for "X,XXX Views"
        try:
            article_text = page.inner_text('article[data-testid="tweet"]', timeout=4_000)
        except Exception:
            article_text = ""
        if article_text:
            m = re.search(r'([\d,\.]+[KkMm]?)\s+[Vv]iews', article_text)
            if m:
                n = _parse_num(m.group(1))
                if n > 0:
                    print(f"     📊 Views (article text): {n:,}")
                    return n

        # Strategy 3: broad span scan — filter low numbers (likes/retweets are small)
        for el in page.query_selector_all('[data-testid="app-text-transition-container"]'):
            raw = (el.text_content() or "").strip()
            n = _parse_num(raw)
            if n >= 500:  # views will always be much larger than rt/like counts
                print(f"     📊 Views (transition container): {n:,}")
                return n

    except Exception as e:
        print(f"     ⚠️ Impression read error: {e}")
    print("     📊 Views: could not read (returns 0)")
    return 0


def generate_aggressive_reply(tweet_text: str, account: str) -> str:
    """Generate a high-engagement second reply designed to drive replies, likes, reposts."""
    if _AI_ENABLED:
        aggressive_context = (
            "This post is going viral — your SECOND reply must drive MAXIMUM engagement. "
            "Pick ONE of these attack angles: "
            "(A) DIVISIVE QUESTION — ask the most contentious question this post raises, "
            "one that forces every reader to pick a side and reply. "
            "(B) CONTRARIAN FLIP — drop the hot take that contradicts the post's premise, "
            "bold enough that fans will repost it to argue with their own followers. "
            "(C) MISSING TRUTH — name the specific thing nobody else in the replies has "
            "said yet; make readers feel they MUST share your reply because it's what "
            "everyone was thinking but didn't say. "
            "Rules: Be direct and confident — no 'I think' or 'in my opinion'. "
            "1-2 sentences MAXIMUM. No hashtags. One emoji at most. "
            "Your reply should make people tap Like AND reply to you specifically."
        )
        ai = generate_ai_reply(tweet_text, context=aggressive_context, account=account)
        if ai:
            return ai
    # Fallback punchy lines
    fallbacks = [
        "This is the conversation everyone's been dodging — where do you actually stand?",
        "The silence from the other side says everything tbh.",
        "Everyone's sharing this but nobody's said the real part out loud yet.",
        "Hot take: the actual story here isn't what most people are talking about.",
        "Drop your honest take below — I want to see how this room is really split.",
    ]
    return random.choice(fallbacks)


def _parse_count_str(s: str) -> int:
    """Parse Twitter-style count strings: '1.2K' → 1200, '5M' → 5000000, '823' → 823."""
    s = (s or "").strip().replace(",", "")
    try:
        if s.upper().endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.upper().endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except (ValueError, AttributeError):
        return 0


def get_latest_tweet(page, account: str) -> Optional["TweetData"]:
    """Navigate to account profile and return their newest tweet."""
    handle = account.lstrip("@")
    url = f"https://x.com/{handle}"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        human_delay(2, 4)

        # Wait for at least one tweet article
        page.wait_for_selector('article[data-testid="tweet"]', timeout=12_000)
        human_delay(1, 1.5)

        articles = page.query_selector_all('article[data-testid="tweet"]')
        if not articles:
            return None

        for article in articles[:5]:   # check first few in case first is pinned
            try:
                # Skip pinned tweets — they contain a "Pinned" label
                social_context = article.query_selector('[data-testid="socialContext"]')
                if social_context and "pinned" in (social_context.text_content() or "").lower():
                    continue

                # Tweet text
                text_el = article.query_selector('[data-testid="tweetText"]')
                text = text_el.text_content().strip() if text_el else ""

                # Skip retweets; detect replies
                retweet_indicator = article.query_selector('[data-testid="socialContext"]')
                is_reply_tweet = False
                if retweet_indicator:
                    ctx_text = (retweet_indicator.text_content() or "").lower()
                    if "retweeted" in ctx_text:
                        continue
                    if "replying to" in ctx_text:
                        is_reply_tweet = True
                if text.startswith("@"):
                    is_reply_tweet = True

                # Get tweet URL (contains tweet ID)
                link_el = article.query_selector('a[href*="/status/"]')
                if not link_el:
                    continue
                href = link_el.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://x.com" + href

                # Extract tweet ID from URL
                m = re.search(r'/status/(\d+)', href)
                if not m:
                    continue
                tweet_id = m.group(1)

                # Estimate age from tweet timestamp
                time_el = article.query_selector('time')
                age_minutes = 999.0
                if time_el:
                    dt_attr = time_el.get_attribute("datetime") or ""
                    try:
                        tweet_dt = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                        now_utc = datetime.now(tweet_dt.tzinfo)
                        age_minutes = (now_utc - tweet_dt).total_seconds() / 60
                    except Exception:
                        pass

                # Detect quote tweet — extract embedded quoted content
                quoted_text = ""
                try:
                    all_text_els = article.query_selector_all('[data-testid="tweetText"]')
                    if len(all_text_els) >= 2:
                        quoted_text = all_text_els[1].text_content().strip()
                        if quoted_text:
                            print(f'     🔁 Quote tweet detected: "{quoted_text[:60]}..."')
                except Exception:
                    pass

                # Extract image URLs from tweet photos (for vision-aware AI reply)
                image_urls = []
                try:
                    img_els = article.query_selector_all('[data-testid="tweetPhoto"] img')
                    for img in img_els[:3]:
                        src = img.get_attribute('src') or ''
                        if src and 'pbs.twimg.com' in src:
                            # Upgrade to medium quality for better AI recognition
                            src = re.sub(r'name=\w+', 'name=medium', src) if 'name=' in src else src
                            image_urls.append(src)
                except Exception:
                    pass
                if image_urls:
                    print(f'     🖼️  {len(image_urls)} image(s) detected in tweet')

                # Scrape like count for viral detection (item 6)
                like_count = 0
                try:
                    like_btn = article.query_selector('[data-testid="like"]')
                    if like_btn:
                        span = like_btn.query_selector('[data-testid="app-text-transition-container"] span span')
                        if span:
                            like_count = _parse_count_str(span.text_content() or "0")
                except Exception:
                    pass

                return TweetData(
                    tweet_id=tweet_id,
                    text=text,
                    url=href,
                    age_minutes=age_minutes,
                    is_reply=is_reply_tweet,
                    quoted_text=quoted_text,
                    image_urls=image_urls,
                    like_count=like_count,
                )

            except Exception:
                continue

    except PlaywrightTimeout:
        print(f"  ⏳ Timeout loading {account}")
    except Exception as e:
        print(f"  ⚠️  Error loading {account}: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Reply poster
# ─────────────────────────────────────────────────────────────────────────────

def post_reply(page, tweet: TweetData, reply_text: str) -> tuple[bool, int]:
    """Open the tweet thread and post the reply. Returns (success, impressions)."""
    view_count = 0
    try:
        page.goto(tweet.url, wait_until="domcontentloaded", timeout=20_000)
        human_delay(2, 4)

        # Wait for the main tweet article to be present before trying to interact
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=10_000)
            human_delay(0.5, 1)
        except PlaywrightTimeout:
            pass

        # Capture impressions from the loaded page — no extra navigation needed.
        # This tells us how many eyes are on this tweet (= exposure value for our reply).
        try:
            for sel in ['[aria-label*=" Views"]', '[aria-label*=" views"]']:
                for el in page.query_selector_all(sel):
                    lbl = el.get_attribute("aria-label") or ""
                    m = re.search(r'([\d,]+)\s*[Vv]iews', lbl)
                    if m:
                        view_count = int(m.group(1).replace(",", ""))
                        break
                if view_count:
                    break
            if not view_count:
                art_text = page.inner_text('article[data-testid="tweet"]', timeout=3_000) or ""
                m = re.search(r'([\d,\.]+[KkMm]?)\s+[Vv]iews', art_text)
                if m:
                    raw = m.group(1).strip().replace(",", "")
                    nm = re.match(r'^([\d]+(?:\.[\d]+)?)([KkMm]?)$', raw)
                    if nm:
                        n, s = float(nm.group(1)), nm.group(2).lower()
                        view_count = int(n * (1_000 if s == 'k' else 1_000_000 if s == 'm' else 1))
        except Exception:
            pass

        # Click the reply button scoped to the FIRST article (the original tweet).
        # Using page-level query_selector_all picks up reply buttons on every reply
        # in the thread — when a tweet has many replies (e.g. 2nd reply scenario)
        # btns[0] ends up being a button on a child reply, not the original post.
        try:
            articles = page.query_selector_all('article[data-testid="tweet"]')
            if articles:
                reply_btn = articles[0].query_selector('[data-testid="reply"]')
                if reply_btn:
                    reply_btn.scroll_into_view_if_needed()
                    reply_btn.click()
                    human_delay(2, 4)  # compose modal needs time to open
        except Exception:
            pass

        # Find textarea
        textarea = None
        for sel in [
            '[data-testid="tweetTextarea_0"]',
            'div[contenteditable="true"]',
            'div[role="textbox"]',
        ]:
            try:
                textarea = page.wait_for_selector(sel, timeout=8_000)
                if textarea:
                    break
            except PlaywrightTimeout:
                continue

        if not textarea:
            print("  ❌ Textarea not found")
            return False, view_count

        textarea.click()
        human_delay(0.4, 0.8)

        try:
            textarea.fill(reply_text)
        except Exception:
            for char in reply_text:
                page.keyboard.type(char, delay=random.randint(20, 60))

        human_delay(0.8, 1.5)

        # Post button
        post_btn = None
        for sel in [
            '[data-testid="tweetButtonInline"]',
            '[data-testid="tweetButton"]',
            'button:has-text("Reply")',
            'div[role="button"]:has-text("Reply")',
            'button:has-text("Post")',
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=5_000)
                if btn and btn.is_enabled():
                    post_btn = btn
                    break
            except PlaywrightTimeout:
                continue

        if not post_btn:
            print("  ❌ Post button not found")
            return False, view_count

        post_btn.click()
        print("  ✅ Post click sent")
        human_delay(3, 5)

        # Rate limit detection
        try:
            body_text = page.inner_text("body", timeout=2_000) or ""
            if any(x in body_text.lower() for x in ["rate limit", "you're posting too much", "slow down"]):
                backoff = random.randint(420, 720)  # 7-12 min randomized (was fixed 5 min)
                print(f"  ⚠️  Rate limit detected — backing off {backoff // 60}m {backoff % 60}s")
                update_daily_log("rate_limit_hits")
                time.sleep(backoff)
                return False, 0
        except Exception:
            pass

        # Confirm: textarea empties or disappears
        posted = False
        try:
            page.wait_for_selector('[data-testid="tweetTextarea_0"]', state="hidden", timeout=5_000)
            posted = True
        except PlaywrightTimeout:
            try:
                ta = page.query_selector('[data-testid="tweetTextarea_0"]')
                if not ta or not (ta.text_content() or "").strip():
                    posted = True
            except Exception:
                pass

        if posted:
            print("  🎉 REPLY POSTED!")
        else:
            print("  ✅ Reply submitted (click confirmed)")

        return True, view_count

    except Exception as e:
        print(f"  ❌ Reply error: {e}")
        return False, 0


# ─── Quote tweet ──────────────────────────────────────────────────────────────
# Football accounts: 15% chance to quote instead of reply — more visibility
# VIP accounts: 35% chance — plus a dedicated question prompt to maximise
# engagement (quote + open question = their followers pile into the thread)

_QUOTE_ELIGIBLE_CATEGORIES = {"football"}   # only quote football-category accounts

def should_quote_tweet(account: str, style_category: str, tweet_age_minutes: float) -> bool:
    """Return True if we should quote-tweet this post instead of replying."""
    if style_category not in _QUOTE_ELIGIBLE_CATEGORIES:
        return False
    if tweet_age_minutes > 10:  # only quote fresh posts for max exposure
        return False
    rate = 0.35 if account in VIP_ACCOUNTS else 0.15
    return random.random() < rate


def generate_vip_quote_question(tweet_text: str, account: str) -> str | None:
    """
    Generate a short, punchy QUESTION to go with a quote tweet of a VIP account.
    Goal: make their followers pile into the quote thread by forcing them to pick
    a side or answer something they genuinely want to weigh in on.
    """
    if not _AI_ENABLED:
        return None
    ctx = (
        "You are quote-tweeting a big football creator's post. "
        "Your job: write ONE short question (max 15 words) that opens a debate "
        "their followers can't ignore — something that forces them to pick a side "
        "or share a hot take in reply. "
        "Rules: Must end with a question mark. No 'I think' or 'in my opinion'. "
        "No hashtags. No emojis unless it adds real punch. "
        "Output the question only — no preamble."
    )
    return generate_ai_reply(tweet_text, context=ctx, account=account)


def post_quote_tweet(page, tweet: TweetData, quote_text: str) -> bool:
    """Navigate to tweet URL and post a quote tweet with quote_text."""
    try:
        page.goto(tweet.url, wait_until="domcontentloaded", timeout=20_000)
        human_delay(2, 3)

        # Click the share / retweet icon to get the quote option
        for sel in ['[data-testid="retweet"]']:
            try:
                btns = page.query_selector_all(sel)
                if btns:
                    btns[0].click()
                    human_delay(0.8, 1.5)
                    break
            except Exception:
                continue

        # Click "Quote" from the dropdown
        quote_btn = None
        for sel in [
            '[data-testid="quoteTweet"]',
            'span:has-text("Quote")',
            'div[role="menuitem"]:has-text("Quote")',
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=5_000)
                if btn:
                    quote_btn = btn
                    break
            except PlaywrightTimeout:
                continue

        if not quote_btn:
            print("  ❌ Quote button not found — falling back to reply")
            return False

        quote_btn.click()
        human_delay(1, 2)

        # Find the tweet compose box
        textarea = None
        for sel in ['[data-testid="tweetTextarea_0"]', 'div[contenteditable="true"]']:
            try:
                textarea = page.wait_for_selector(sel, timeout=7_000)
                if textarea:
                    break
            except PlaywrightTimeout:
                continue

        if not textarea:
            print("  ❌ Quote compose box not found")
            return False

        textarea.click()
        human_delay(0.4, 0.8)
        try:
            textarea.fill(quote_text)
        except Exception:
            for char in quote_text:
                page.keyboard.type(char, delay=random.randint(20, 60))

        human_delay(0.8, 1.5)

        # Post button
        post_btn = None
        for sel in [
            '[data-testid="tweetButtonInline"]',
            '[data-testid="tweetButton"]',
            'button:has-text("Post")',
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=5_000)
                if btn and btn.is_enabled():
                    post_btn = btn
                    break
            except PlaywrightTimeout:
                continue

        if not post_btn:
            print("  ❌ Quote post button not found")
            return False

        post_btn.click()
        print("  ✅ Quote tweet click sent")
        human_delay(3, 5)
        print("  🔁 QUOTE TWEET POSTED!")
        return True

    except Exception as e:
        print(f"  ❌ Quote tweet error: {e}")
        return False


def get_thread_context(page, tweet: "TweetData") -> tuple[str, str]:
    """
    Navigate to the tweet thread page and extract:
    - parent tweet text (when this tweet is a reply in a thread)
    - top 2-3 existing replies from other users (for AI context)
    Returns: (parent_text, top_replies_text)
    """
    parent_text = ""
    top_replies = ""
    try:
        page.goto(tweet.url, wait_until="domcontentloaded", timeout=15_000)
        human_delay(1.5, 2.5)
        articles = page.query_selector_all('article[data-testid="tweet"]')

        # Parent tweet — only useful when this tweet is a reply
        if tweet.is_reply and len(articles) >= 2:
            parent_el = articles[0].query_selector('[data-testid="tweetText"]')
            if parent_el:
                parent_txt = parent_el.text_content().strip()
                if parent_txt and parent_txt != tweet.text:
                    parent_text = parent_txt
                    print(f'     📝 Thread parent: "{parent_text[:80]}..."')

        # Top existing replies — articles after the original tweet (and parent if threaded)
        # These tell the AI what direction the conversation is going
        reply_start = 2 if tweet.is_reply else 1
        reply_els = []
        for art in articles[reply_start:reply_start + 3]:
            try:
                el = art.query_selector('[data-testid="tweetText"]')
                if el:
                    rt = el.text_content().strip()
                    if rt and rt != tweet.text and len(rt) > 10:
                        reply_els.append(rt[:150])
            except Exception:
                pass
        if reply_els:
            top_replies = "\n".join(f'• "{r}"' for r in reply_els)
            print(f'     💬 {len(reply_els)} existing repl(ies) captured')
    except Exception as e:
        print(f"     ⚠️  Thread context error: {e}")
    return parent_text, top_replies


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run_loop(page, dry_run: bool = False):
    """Continuously cycle through monitored accounts and reply to new posts."""
    state = load_state()
    cycle = 0
    global_gain_trains = 0
    global_gain_train_hour = datetime.now().strftime("%Y-%m-%d-%H")

    print(f"\n👀 Monitoring {len(MONITORED_ACCOUNTS)} accounts")
    print(f"   Checking every ~{CHECK_INTERVAL_BASE}s (±{CHECK_JITTER}s jitter) | Max age: {MAX_TWEET_AGE_MINUTES}min | Cap: {MAX_REPLIES_PER_ACCOUNT_PER_HOUR}/hour per account | Gain train cap: {MAX_GAIN_TRAINS_PER_HOUR}/hour global")
    print(f"   Dry run: {dry_run}\n")

    while True:
        cycle += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'─'*55}")
        print(f"🔄 Cycle #{cycle}  [{now_str}]")
        print(f"{'─'*55}")

        try:
            replied_this_cycle = 0

            # Re-order accounts by avg impressions every cycle — highest-exposure accounts first
            ordered_accounts = sorted_accounts_by_performance()

            # Reset global gain train counter each new hour
            current_hour = datetime.now().strftime("%Y-%m-%d-%H")
            if current_hour != global_gain_train_hour:
                global_gain_trains = 0
                global_gain_train_hour = current_hour

            for account in ordered_accounts:
                acc_state = get_account_state(state, account)

                # Hourly cap — gain train posts bypass per-account cap but have their own global cap
                at_hourly_cap = acc_state["replies_this_hour"] >= MAX_REPLIES_PER_ACCOUNT_PER_HOUR
                at_gain_train_cap = global_gain_trains >= MAX_GAIN_TRAINS_PER_HOUR

                print(f"\n  📡 Checking {account}...")
                tweet = get_latest_tweet(page, account)

                if not tweet:
                    print(f"     No tweet found")
                    continue

                # Already replied to this tweet?
                if tweet.tweet_id == acc_state.get("last_tweet_id"):
                    print(f"     No new post (last: {tweet.tweet_id})")
                    continue

                # Engagement detection (item 7) — did they mention us?
                if f'@{USERNAME.lower()}' in tweet.text.lower():
                    log_engagement(account)
                    print(f"     💬 {account} mentioned @{USERNAME} — engagement logged!")

                # Tweet too old?
                if tweet.age_minutes > MAX_TWEET_AGE_MINUTES:
                    print(f"     Tweet is {tweet.age_minutes:.0f}min old — too old, skipping")
                    acc_state["last_tweet_id"] = tweet.tweet_id
                    save_state(state)
                    continue

                # Determine if this is a gain train (gain trains always get a reply)
                gain_train = is_gain_train(tweet.text)

                # Relevance scoring — prioritise tweets the bot can reply well to
                relevance = score_tweet_relevance(tweet.text, account, tweet.like_count, tweet.age_minutes)
                if not gain_train and replied_this_cycle >= 2 and relevance < 35:
                    print(f"     📉 Low relevance ({relevance}/100) & already replied to {replied_this_cycle} this cycle — deferring")
                    continue

                # Viral tweet detection (item 6) — bypass hourly cap for high-traction fresh posts
                is_viral = tweet.like_count >= 500 and tweet.age_minutes < 15
                if is_viral:
                    print(f"     🚀 Viral tweet! ({tweet.like_count:,} likes, {tweet.age_minutes:.0f}min old) — hourly cap bypassed")
                elif tweet.like_count > 0:
                    print(f"     ❤️  {tweet.like_count:,} likes")

                # If gain train but global gain train cap hit — skip
                if gain_train and at_gain_train_cap:
                    print(f"  {account}: global gain train cap reached ({MAX_GAIN_TRAINS_PER_HOUR}/hr) — skipping")
                    continue

                # If hourly cap hit AND this isn't a gain train or viral — skip until next hour
                if at_hourly_cap and not gain_train and not is_viral:
                    print(f"  {account}: hourly cap reached ({MAX_REPLIES_PER_ACCOUNT_PER_HOUR}) — skipping until next hour")
                    continue

                # Skip check (gain trains bypass this too)
                if gain_train:
                    print(f"     Gain train detected — joining")
                else:
                    skip_reason = should_skip(tweet.text, account)
                    if skip_reason:
                        print(f"     Skipping: {skip_reason}")
                        acc_state["last_tweet_id"] = tweet.tweet_id
                        save_state(state)
                        continue

                # 🎯 NEW post found!
                style_cat      = ACCOUNT_TONE_MAP.get(account, "")
                avoid_openers  = acc_state.get("recent_openers", [])
                avoid_phrases  = acc_state.get("recent_phrases", [])
                language       = "ja" if account in JAPANESE_ACCOUNTS else ""
                rival_fan      = RIVAL_FAN_ACCOUNTS.get(account, "")
                is_united_fan  = account in UNITED_FAN_ACCOUNTS
                is_neutral_elite = account in NEUTRAL_ELITE_ACCOUNTS
                is_meme_banter  = account in MEME_BANTER_ACCOUNTS
                account_profile = get_account_profile_hint(account)  # item 1
                # Thread context: fetch parent tweet if this is a reply
                # Reply context: fetch top existing replies for VIP accounts or short ambiguous tweets
                parent_tweet  = ""
                top_replies   = ""
                needs_context = (
                    (tweet.is_reply and len(tweet.text.strip()) < 120)
                    or account in VIP_ACCOUNTS
                )
                if needs_context:
                    parent_tweet, top_replies = get_thread_context(page, tweet)
                elif tweet.is_reply:
                    parent_tweet, _ = get_thread_context(page, tweet)
                reply = generate_reply(tweet.text, account,
                                       style_category=style_cat,
                                       avoid_openers=avoid_openers,
                                       avoid_phrases=avoid_phrases,
                                       parent_tweet=parent_tweet,
                                       quoted_tweet=tweet.quoted_text,
                                       language=language,
                                       rival_fan=rival_fan,
                                       is_united_fan=is_united_fan,
                                       is_neutral_elite=is_neutral_elite,
                                       is_meme_banter=is_meme_banter,
                                       image_urls=tweet.image_urls,
                                       top_replies=top_replies,
                                       account_profile=account_profile)

                print(f"  🔥 NEW POST from {account} ({tweet.age_minutes:.0f}min ago, relevance: {relevance}/100)")
                print(f"     Their post: {tweet.text[:100]}{'...' if len(tweet.text)>100 else ''}")
                print(f"     Our reply : {reply}")

                if not reply:
                    acc_state["last_tweet_id"] = tweet.tweet_id
                    save_state(state)
                    continue

                if dry_run:
                    print(f"     [DRY RUN - not posting]")
                    acc_state["last_tweet_id"] = tweet.tweet_id
                    save_state(state)
                    continue

                # Decide: quote tweet (35% VIP / 15% other football, fresh posts) or reply
                use_quote = (not gain_train and
                             should_quote_tweet(account, style_cat, tweet.age_minutes))
                if use_quote:
                    # VIP quote tweets use a dedicated question to maximise engagement
                    if account in VIP_ACCOUNTS:
                        quote_text = generate_vip_quote_question(tweet.text, account) or reply
                        print(f"     [VIP QUOTE + QUESTION mode]")
                    else:
                        quote_text = reply
                        print(f"     [QUOTE TWEET mode]")
                    print(f"     Quote text: {quote_text}")
                    success = post_quote_tweet(page, tweet, quote_text)
                    imp = 0
                    if not success:
                        # Fallback to regular reply if quote fails
                        success, imp = post_reply(page, tweet, reply)
                else:
                    success, imp = post_reply(page, tweet, reply)

                if success:
                    if not gain_train:
                        log_reply_performance(account, imp,
                            classify_post_type(tweet.text, style_cat, language, gain_train))
                        # Log impression against the reply style used — ai_reply.py
                        # uses this to self-tune which angles drive the most exposure
                        # (Japanese replies excluded: they use a separate prompt)
                        if language != "ja":
                            log_style_performance(imp)
                        log_hourly_performance(imp)               # item 8 — peak hour tracking
                    update_account_memory(account, tweet.text)    # item 1 — per-account memory
                    acc_state["last_tweet_id"] = tweet.tweet_id
                    acc_state["last_tweet_text"] = tweet.text   # stored for potential 2nd reply
                    if gain_train:
                        acc_state["gain_trains_this_hour"] = acc_state.get("gain_trains_this_hour", 0) + 1
                        global_gain_trains += 1
                    else:
                        acc_state["replies_this_hour"] = acc_state.get("replies_this_hour", 0) + 1
                    # Anti-repetition: log first word AND first 6 words of reply for variety
                    if reply and not gain_train and reply.split():
                        first_word = reply.split()[0].lower().rstrip(".,!?:\"'")
                        if first_word:
                            recent = acc_state.get("recent_openers", [])
                            recent.append(first_word)
                            acc_state["recent_openers"] = recent[-20:]
                        # Also store full phrase fingerprints (first 6 words) for deeper dedup
                        phrase = " ".join(reply.split()[:6]).lower().rstrip(".,!?")
                        recent_phrases = acc_state.get("recent_phrases", [])
                        recent_phrases.append(phrase)
                        acc_state["recent_phrases"] = recent_phrases[-20:]
                    update_daily_log("replies_posted")
                    # Track for VIP second reply — watchlist for all replied VIP tweets
                    if account in VIP_ACCOUNTS:
                        acc_state["pending_vip_tweets"][tweet.tweet_id] = {
                            "text": tweet.text,
                            "url": tweet.url,
                            "replied_at": datetime.now().isoformat(),
                        }
                    save_state(state)
                    replied_this_cycle += 1

                    # Small pause after each reply to appear natural — varied to avoid bot patterns
                    if replied_this_cycle > 1:
                        # Longer, more varied gaps to look human (was 30-180, now 60-300)
                        r = random.random()
                        if r < 0.5:
                            gap = random.randint(60, 150)
                        elif r < 0.8:
                            gap = random.randint(150, 240)
                        else:
                            gap = random.randint(240, 360)
                        print(f"\n  ⏳ Cooling down {gap}s...")
                        time.sleep(gap)
                else:
                    # Still update last_tweet_id to avoid retrying broken posts
                    acc_state["last_tweet_id"] = tweet.tweet_id
                    acc_state["last_tweet_text"] = tweet.text
                    save_state(state)

                # Brief pause between account checks
                human_delay(1.5, 3)

            # ── VIP double-reply check ────────────────────────────────────────────
            # Check ALL pending VIP tweets (not just the latest), fire 2nd reply
            # when impressions cross the threshold. Expires entries after 12 hours.
            for account in VIP_ACCOUNTS:
                acc_state = get_account_state(state, account)
                pending = acc_state.get("pending_vip_tweets", {})
                if not pending:
                    continue

                expired_ids = []
                for tweet_id, meta in list(pending.items()):
                    # Expire entries older than 12 hours
                    try:
                        replied_at = datetime.fromisoformat(meta.get("replied_at", ""))
                        age_hours = (datetime.now() - replied_at).total_seconds() / 3600
                        if age_hours > 12:
                            print(f"     ⏰ {account} tweet {tweet_id} expired (12h) — removing from watchlist")
                            expired_ids.append(tweet_id)
                            continue
                    except Exception:
                        pass

                    tweet_url = meta.get("url") or f"https://x.com/{account.lstrip('@')}/status/{tweet_id}"
                    original_text = meta.get("text", "")

                    print(f"\n  🔍 VIP check {account} — impressions on {tweet_id}...")
                    impressions = get_tweet_impressions(page, tweet_url)
                    print(f"     Impressions: {impressions:,}  |  Age: {age_hours:.1f}h")

                    # Fire if over threshold OR if impressions unreadable and tweet is ≥45 min old
                    # (scraping impression counts on X is fragile — time-based fallback ensures
                    #  viral VIP posts always get the second reply)
                    fire_by_impressions = impressions >= IMPRESSION_THRESHOLD
                    fire_by_time = (impressions == 0 and age_hours >= 0.75)  # 45 min

                    if not fire_by_impressions and not fire_by_time:
                        print(f"     Below threshold and too fresh — watching")
                        continue

                    reason = f"{impressions:,} impressions" if fire_by_impressions else f"time-based ({age_hours:.1f}h old, impressions unreadable)"
                    print(f"  🚀 Firing aggressive 2nd reply! ({reason})")
                    reply2 = generate_aggressive_reply(original_text, account)
                    print(f"     2nd reply: {reply2}")

                    if dry_run:
                        print(f"     [DRY RUN - not posting]")
                        expired_ids.append(tweet_id)
                        save_state(state)
                        continue

                    td = TweetData(tweet_id=tweet_id, text=original_text, url=tweet_url, age_minutes=0)
                    success, _ = post_reply(page, td, reply2)
                    if success:
                        print(f"  🎉 2nd REPLY POSTED on {account}!")
                        expired_ids.append(tweet_id)  # done — remove from watchlist
                        save_state(state)
                        gap = random.randint(45, 90)
                        print(f"  ⏳ Cooling down {gap}s...")
                        time.sleep(gap)

                # Clean up expired / completed entries
                for tid in expired_ids:
                    pending.pop(tid, None)
                if expired_ids:
                    save_state(state)

                print(f"\n  ✅ Cycle complete — replied to {replied_this_cycle} post(s)")
                # Check mentions every 3 cycles (item 3) — follow up on direct replies to us
                if cycle % 3 == 0:
                    check_mentions(page, state, dry_run)
                if cycle % 5 == 0:
                    _print_performance_summary()
        except Exception as e:
            _flog.error(f"Cycle #{cycle} crashed: {e}", exc_info=True)
            print(f"\n  ❌ Cycle #{cycle} error: {e} — recovering...")
            try:
                page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15_000)
                human_delay(2, 3)
            except Exception:
                pass
        _sleep = _jittered_interval()
        print(f"  ⏳ Next check in {_sleep}s...")
        time.sleep(_sleep)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not USERNAME or not PASSWORD:
        print("❌ TWITTER_USERNAME / TWITTER_PASSWORD not set in .env")
        sys.exit(1)

    import argparse
    parser = argparse.ArgumentParser(description="Fast Replier — reply to creator posts the moment they drop")
    parser.add_argument("--dry-run", action="store_true", help="Detect new posts but don't post replies")
    parser.add_argument("--once", action="store_true", help="Run only one cycle then exit")
    args = parser.parse_args()

    print("\n⚡ PLAYWRIGHT FAST REPLIER")
    print("=" * 55)
    print(f"  Account  : {USERNAME}")
    print(f"  Targets  : {', '.join(MONITORED_ACCOUNTS)}")
    print(f"  Interval : ~{CHECK_INTERVAL_BASE}s (±{CHECK_JITTER}s jitter)")
    print(f"  Max age  : {MAX_TWEET_AGE_MINUTES}min")
    print(f"  Dry run  : {args.dry_run}")
    print("=" * 55)

    os.makedirs(PROFILE_DIR, exist_ok=True)

    with sync_playwright() as pw:
        _ua = _pick_user_agent()
        print(f"  UA       : {_ua[:50]}...")
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": random.choice([1280, 1366, 1440, 1536]), "height": random.choice([800, 864, 900, 960])},
            user_agent=_ua,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled",
            ],
        )
        if Stealth:
            _stealth = Stealth(navigator_user_agent_override=_ua)
            _stealth.apply_stealth_sync(ctx)
            print("  Stealth  : enabled")
        page = ctx.new_page()

        try:
            if not is_logged_in(page):
                if not login(page):
                    print("❌ Login failed")
                    ctx.close()
                    return

            if args.once:
                # Single cycle
                state = load_state()
                for account in MONITORED_ACCOUNTS:
                    acc_state = get_account_state(state, account)
                    at_hourly_cap     = acc_state["replies_this_hour"]     >= MAX_REPLIES_PER_ACCOUNT_PER_HOUR
                    at_gain_train_cap = acc_state["gain_trains_this_hour"] >= MAX_GAIN_TRAINS_PER_HOUR
                    print(f"\n  📡 Checking {account}...")
                    tweet = get_latest_tweet(page, account)
                    if not tweet:
                        print(f"     No tweet found"); continue
                    if tweet.tweet_id == acc_state.get("last_tweet_id"):
                        print(f"     No new post"); continue
                    if tweet.age_minutes > MAX_TWEET_AGE_MINUTES:
                        print(f"     Too old ({tweet.age_minutes:.0f}min)"); acc_state["last_tweet_id"] = tweet.tweet_id; save_state(state); continue
                    gain_train = is_gain_train(tweet.text)
                    if gain_train and at_gain_train_cap:
                        print(f"     Gain train cap reached — skipping"); continue
                    if not gain_train and at_hourly_cap:
                        print(f"     Hourly cap reached — skipping"); continue
                    if not gain_train:
                        skip_reason = should_skip(tweet.text, account)
                        if skip_reason:
                            print(f"     Skipping: {skip_reason}"); acc_state["last_tweet_id"] = tweet.tweet_id; save_state(state); continue
                    reply = generate_reply(tweet.text, account)
                    print(f"  🔥 NEW: {account} | {tweet.text[:80]}...")
                    print(f"     → {reply}")
                    if not args.dry_run:
                        if post_reply(page, tweet, reply):
                            acc_state["last_tweet_id"] = tweet.tweet_id
                            if gain_train:
                                acc_state["gain_trains_this_hour"] = acc_state.get("gain_trains_this_hour", 0) + 1
                            else:
                                acc_state["replies_this_hour"] = acc_state.get("replies_this_hour", 0) + 1
                            save_state(state)
                    human_delay(2, 4)
            else:
                run_loop(page, dry_run=args.dry_run)

        except KeyboardInterrupt:
            print("\n⛔ Stopped by user")
        finally:
            ctx.close()
            print("🔒 Browser closed")


if __name__ == "__main__":
    main()
