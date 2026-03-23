"""
One-time session setup — run this once to log in to both browser profiles.
After this, playwright_fast_replier.py and playwright_trend_poster.py will
start without asking you to log in again.

Usage:
    python setup_sessions.py
"""

import time
from pathlib import Path
from playwright.sync_api import sync_playwright

DATA_DIR = Path(__file__).parent / "data"

PROFILES = [
    ("fast_replier", str(DATA_DIR / "browser_profile_pw")),
    ("trend_poster",  str(DATA_DIR / "browser_profile_trend")),
]


def setup_profile(name: str, profile_dir: str):
    print(f"\n{'='*55}")
    print(f"  Setting up session: {name}")
    print(f"  Profile: {profile_dir}")
    print(f"{'='*55}")
    print("  A browser window will open. Log in to X/Twitter.")
    print("  Once you see your home feed, come back here and")
    print("  press ENTER to save the session and continue.")
    input("  Press ENTER to open the browser...")

    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30_000)

        print(f"\n  Browser is open. Log in if needed, then come back here.")
        input("  Press ENTER once you're logged in and can see your home feed...")

        # Verify login
        try:
            page.wait_for_selector('[data-testid="SideNav_NewTweet_Button"]', timeout=10_000)
            print(f"  ✅ Session saved for {name}!")
        except Exception:
            print(f"  ⚠️  Could not confirm login for {name}. Session may not be saved.")

        # Small pause so Chromium flushes cookies/storage to disk
        time.sleep(2)
        ctx.close()


def main():
    print("\n🔐 Twitter Agent — One-Time Session Setup")
    print("This will open a browser for each profile so you can log in once.")
    print("After this, the scripts will start automatically without login prompts.\n")

    for name, profile_dir in PROFILES:
        setup_profile(name, profile_dir)

    print("\n✅ All sessions configured!")
    print("You can now run:")
    print("  python playwright_fast_replier.py")
    print("  python playwright_trend_poster.py")
    print("without logging in again.")


if __name__ == "__main__":
    main()
