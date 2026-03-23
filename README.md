# Twitter/X AI Agent

An autonomous Twitter/X bot that monitors accounts, generates AI-powered replies, and posts original tweets about trending topics. Built with Playwright for browser automation and OpenAI for content generation.

The bot runs two engines simultaneously:
- **Reply Engine** — monitors configured accounts, detects new tweets, generates contextual replies
- **Posting Engine** — tracks trending topics, generates original tweets with images

All identity, team affiliation, and personality are configured in a single file (`config/persona.py`), making it easy to create any type of fan bot — football, basketball, music, tech, etc.

---

## Features

- **AI-Powered Replies** — context-aware responses using GPT-4o-mini (with fallback to GPT-3.5-turbo)
- **Trend Detection** — scrapes Twitter/X trending topics and generates hot takes
- **Image Support** — auto-attaches relevant images from web search or local library
- **Anti-Detection** — playwright-stealth integration, human-like delays, randomized behavior
- **Rival Banter** — configurable stances for rival fan accounts
- **Teammate Mode** — detects friendly accounts and builds on their takes
- **Japanese Support** — separate prompts for Japanese-language replies and posts
- **Safety System** — controversy scoring, banned phrase detection, identity verification
- **Auto-Restart** — watchdog process monitors both engines and relaunches on crash
- **Scheduled Downtime** — configurable quiet hours to mimic human sleep patterns

---

## Project Structure

```
├── watchdog.py                  # Entry point — launches & monitors both engines
├── playwright_fast_replier.py   # Reply engine — monitors accounts, generates replies
├── playwright_trend_poster.py   # Posting engine — trends, hot takes, images
├── ai_reply.py                  # AI generation — prompts, web search, stat verification
├── config/
│   ├── persona.py               # ★ MAIN CONFIG — bot identity, prompts, banter
│   ├── settings.py              # Environment-based settings (from .env)
│   ├── targets.py               # Per-account response configuration
│   └── fan_profile.py           # Fan profile questionnaire system
├── core/
│   ├── analyzer.py              # Tweet analysis and scoring
│   ├── generator.py             # Response generation pipeline
│   ├── monitor.py               # Account monitoring logic
│   ├── improved_monitor.py      # Enhanced monitoring with deduplication
│   ├── researcher.py            # Web research for context
│   └── safety.py                # Safety checks and content filtering
├── utils/
│   ├── bot_logger.py            # Logging with rotation
│   ├── safe_io.py               # Thread-safe file I/O
│   ├── state_db.py              # SQLite state persistence
│   └── scheduler.py             # Task scheduling utilities
├── setup.py                     # First-time setup wizard
├── setup_sessions.py            # Browser session setup
├── setup_fan_profile.py         # Interactive fan profile builder
├── manual_login_helper.py       # Manual Twitter login helper
├── launch.py                    # Alternative launcher
├── main.py                      # Alternative entry point
├── migrate_state.py             # State migration utility
├── .env.example                 # Environment variable template
├── requirements.txt             # Python dependencies
└── data/
    ├── images/                  # Image library (organized by subfolder)
    └── logs/                    # Runtime logs
```

---

## Prerequisites

- **Python 3.11+** (tested on 3.14)
- **OpenAI API key** — primary AI provider ([get one here](https://platform.openai.com/api-keys))
- **Groq API key** (optional) — free fallback AI ([get one here](https://console.groq.com))
- **Twitter/X account** — the bot logs in via browser automation
- **Twitter API v2 keys** (optional) — for enhanced features

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd twitter-agent
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Required
TWITTER_USERNAME=your_twitter_handle
TWITTER_PASSWORD=your_twitter_password
OPENAI_API_KEY=sk-proj-...

# Optional
GROQ_API_KEY=gsk_...
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...
TWITTER_BEARER_TOKEN=...
```

### 3. Configure your bot's persona

Edit `config/persona.py` — this is the most important file. It controls:

| Section | What it does |
|---------|-------------|
| **Bot Identity** | Handle, team, nationality, personality, location |
| **Monitored Accounts** | Twitter handles the reply engine watches |
| **Rival Teams** | Teams treated as rivals in banter |
| **Protected Players** | Players the bot will always defend |
| **Criticisable Players** | Players the bot can roast |
| **Rival Banter Stances** | Custom response style per rival fan account |
| **Slang** | Allowed/banned slang words |
| **Fixed Opinions** | Strong opinions the bot always holds |
| **SYSTEM_PROMPT** | The full reply engine personality prompt |
| **POST_SYSTEM_PROMPT** | The full posting engine personality prompt |
| **Rival News Markers** | Keywords that trigger rival team news detection |

Example — making a Liverpool fan bot:

```python
BOT_HANDLE = "@LFCTakesBot"
FAVORITE_TEAM = "Liverpool"
NATIONALITY = "British"
PERSONALITY = "PASSIONATE, WITTY"
LOCATION = "Liverpool"

RIVAL_TEAMS = ["Manchester United", "Everton", "Manchester City", "Chelsea"]

PROTECTED_PLAYERS = [
    "Mo Salah — the Egyptian King, untouchable",
    "Virgil van Dijk — best defender in the world",
]

MONITORED_ACCOUNTS = [
    "@LFC", "@AnfieldWatch", "@JamesPearceLFC",
]
```

Then edit `SYSTEM_PROMPT` and `POST_SYSTEM_PROMPT` at the bottom of the file to match the personality you want. These are detailed instruction prompts sent to the AI model — they control tone, style, and behavior.

### 4. Configure trend detection keywords

Edit the `FOOTBALL_TERMS` dictionary in `playwright_trend_poster.py`:

```python
FOOTBALL_TERMS = {
    "my_team": [
        # Your team's names, players, stadium, manager
        "liverpool", "lfc", "anfield", "salah", "van dijk",
    ],
    "rivals": [
        # Rival clubs
        "manchester united", "everton", "man city",
    ],
    # ... other categories (european, ai_tech, etc.) are pre-filled
}
```

### 5. Configure account classifications

In `playwright_fast_replier.py`, fill in the account classification lists:

- `VIP_ACCOUNTS` — accounts that get priority replies
- `ACCOUNT_TONE_MAP` — custom tone per account (e.g. `"@someuser": "banter"`)
- `RIVAL_FAN_ACCOUNTS` — known rival fans (triggers banter mode)
- `UNITED_FAN_ACCOUNTS` — teammate accounts (triggers agreement mode)
- `NEUTRAL_ELITE_ACCOUNTS` — analyst accounts (triggers balanced mode)

### 6. Set up browser session

Run the login helper to create a persistent browser session:

```bash
python manual_login_helper.py
```

This opens a real browser window. Log in to Twitter/X manually, complete any 2FA, then close the window. The session is saved for future runs.

### 7. (Optional) Add images

Place images in `data/images/` organized by subfolder:

```
data/images/
├── my_team/       # Your team images
├── rivals/        # Rival team images
├── football/      # General football images
└── general/       # Generic images
```

The posting engine auto-selects relevant images based on tweet category.

---

## Running the Bot

### Recommended: Use the watchdog

```bash
python watchdog.py
```

This launches both engines and automatically restarts either if it crashes. It also handles scheduled downtime (configurable quiet hours in the script).

### Run engines separately

```bash
# Reply engine only
python playwright_fast_replier.py

# Posting engine only
python playwright_trend_poster.py
```

### Stop the bot

Press `Ctrl+C` in the terminal. The watchdog gracefully shuts down both engines.

---

## How It Works

### Reply Engine (`playwright_fast_replier.py`)

1. Opens a Playwright browser with stealth mode
2. Navigates to each monitored account's profile
3. Detects new tweets (deduplicates via SQLite state DB)
4. Classifies the tweet (rival banter, teammate, neutral, etc.)
5. Sends tweet + context to OpenAI with the persona's SYSTEM_PROMPT
6. Applies safety checks (controversy score, banned phrases, identity errors)
7. Posts the reply with human-like typing delays
8. Waits a randomized interval before the next cycle

### Posting Engine (`playwright_trend_poster.py`)

1. Opens a Playwright browser with stealth mode
2. Scrapes Twitter/X trending topics
3. Scores trends by category (your team > rivals > football > AI > entertainment > general)
4. Fetches context from the trend page (real tweets about the topic)
5. Optionally runs a web search for live facts
6. Sends topic + context to OpenAI with POST_SYSTEM_PROMPT
7. Selects or downloads a relevant image
8. Posts the tweet with the image
9. Cycles through trends with randomized timing

### Safety System

- **Controversy scoring** — rates generated content 1-10, rejects anything above threshold
- **Identity verification** — ensures the bot doesn't claim to be a different team's fan
- **Banned phrase detection** — filters out phrases that sound robotic or AI-generated
- **Stat verification** — cross-checks any statistics mentioned against web sources
- **Rate limiting** — configurable max replies per hour and per user per day

---

## Configuration Reference

### Environment Variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `TWITTER_USERNAME` | Yes | Twitter/X login username |
| `TWITTER_PASSWORD` | Yes | Twitter/X login password |
| `OPENAI_API_KEY` | Yes | OpenAI API key for GPT models |
| `GROQ_API_KEY` | No | Groq API key (free fallback) |
| `TWITTER_API_KEY` | No | Twitter API v2 key |
| `TWITTER_API_SECRET` | No | Twitter API v2 secret |
| `TWITTER_ACCESS_TOKEN` | No | Twitter API v2 access token |
| `TWITTER_ACCESS_TOKEN_SECRET` | No | Twitter API v2 access token secret |
| `TWITTER_BEARER_TOKEN` | No | Twitter API v2 bearer token |
| `SAFE_MODE` | No | Enable extra safety checks (default: false) |
| `MAX_CONTROVERSY_LEVEL` | No | Max controversy score 1-10 (default: 8) |
| `REQUIRE_MANUAL_REVIEW` | No | Require approval before posting (default: true) |
| `RESPONSE_DELAY_MIN` | No | Min seconds between replies (default: 300) |
| `RESPONSE_DELAY_MAX` | No | Max seconds between replies (default: 1800) |
| `MAX_RESPONSES_PER_HOUR` | No | Max replies per hour (default: 3) |

### Key Files to Customize

| File | What to edit |
|------|-------------|
| `config/persona.py` | Bot identity, personality prompts, banter stances |
| `playwright_trend_poster.py` | `FOOTBALL_TERMS` dict — trend detection keywords |
| `playwright_fast_replier.py` | Account classification lists (VIP, rivals, teammates) |
| `config/targets.py` | Per-account response rules |
| `watchdog.py` | Quiet hours (`STOP_HOUR_WAT`, `RESTART_HOUR_WAT`) |

---

## Troubleshooting

**Bot can't log in to Twitter**
- Run `python manual_login_helper.py` to create a fresh session
- Make sure your `.env` credentials are correct
- Check if Twitter is requiring email/phone verification

**Replies seem robotic**
- Edit `SYSTEM_PROMPT` in `config/persona.py` — add more natural speech examples
- Add banned phrases to `BANNED_SLANG` to filter out robotic patterns
- Reduce `MAX_CONTROVERSY_LEVEL` if takes are too wild

**Bot is replying too fast / getting rate limited**
- Increase `RESPONSE_DELAY_MIN` and `RESPONSE_DELAY_MAX` in `.env`
- Reduce `MAX_RESPONSES_PER_HOUR`

**Browser detected as bot**
- The bot uses `playwright-stealth` v2.0.0+ for anti-detection
- Ensure you have the latest Chromium: `playwright install chromium`
- Avoid running multiple browser instances simultaneously

**Images not attaching**
- Place images in `data/images/` subfolders
- Supported formats: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`
- Images under 3KB are automatically skipped

---

## Notes

- The bot uses browser automation (Playwright), not the Twitter API, for core operations. This means it interacts with Twitter exactly like a human user.
- AI content is generated via OpenAI's API. You're responsible for API costs.
- The watchdog includes configurable quiet hours to mimic human sleep. Adjust the `STOP_HOUR_WAT` / `RESTART_HOUR_WAT` constants in `watchdog.py` to your timezone.
- State is persisted in a local SQLite database (`data/` folder) to avoid replying to the same tweet twice.
- The Japanese language support is built in — add accounts to `JAPANESE_ACCOUNTS` in `config/persona.py` to reply in Japanese.
