"""
Bot Watchdog
============
Launches both bot engines and automatically restarts either one if it crashes.

Usage:
    python watchdog.py

Instead of running the two engines separately, run only this script.
It starts both, monitors them every 60 seconds, and relaunches any that have died.
Press Ctrl+C to stop everything.
"""

import os
import sys
import time
import subprocess
from datetime import datetime
from pathlib import Path

try:
    from utils.bot_logger import get_logger
    _log = get_logger("watchdog")
except Exception:
    import logging
    _log = logging.getLogger("watchdog")

# Ensure stdout/stderr handle emoji on Windows without PYTHONUTF8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CWD            = Path(__file__).parent
SCRIPTS        = ["playwright_fast_replier.py", "playwright_trend_poster.py"]
CHECK_INTERVAL = 60   # seconds between health checks
RESTART_DELAY_BASE = 10   # initial seconds before relaunch
RESTART_DELAY_MAX  = 300  # max backoff (5 minutes)
RESTART_DELAY_RESET = 1800  # reset backoff after 30 min stable
STOP_HOUR_WAT    = 1   # shut down both engines at this WAT hour
STOP_MINUTE_WAT  = 30  # ... and at this minute (1:30am WAT)
RESTART_HOUR_WAT = 3   # relaunch engines at this WAT hour
RESTART_MINUTE_WAT = 50  # ... and at this minute (3:50am WAT)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)
    _log.info(msg)


def _wat_now():
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=1)))


def _should_stop() -> bool:
    """Return True if WAT time has reached or passed the stop time."""
    now = _wat_now()
    return (now.hour, now.minute) >= (STOP_HOUR_WAT, STOP_MINUTE_WAT)


def _should_restart() -> bool:
    """Return True if WAT time has reached or passed the restart time."""
    now = _wat_now()
    return (now.hour, now.minute) >= (RESTART_HOUR_WAT, RESTART_MINUTE_WAT)


def launch(script: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, script],
        cwd=CWD,
        env=env,
    )
    log(f"▶  Launched {script} (PID {proc.pid})")
    return proc


def main() -> None:
    log("🐕 Watchdog starting — monitoring both bot engines")
    log(f"   Scripts : {', '.join(SCRIPTS)}")
    log(f"   Check   : every {CHECK_INTERVAL}s")
    log("=" * 55)

    procs: dict[str, subprocess.Popen] = {}
    # Per-script backoff tracking: {script: current_delay}
    backoff: dict[str, int] = {s: RESTART_DELAY_BASE for s in SCRIPTS}
    # Timestamp of last successful restart (for resetting backoff)
    last_restart_ts: dict[str, float] = {}

    for script in SCRIPTS:
        procs[script] = launch(script)
        time.sleep(3)  # stagger launches so both don't hit X simultaneously

    # Track the calendar date of the last nightly stop so we only stop once per night
    last_stop_date: str = ""

    try:
        while True:
            time.sleep(CHECK_INTERVAL)
            # ── 1:30am WAT auto-shutdown (once per calendar day) ──────────
            today_str = _wat_now().strftime("%Y-%m-%d")
            if _should_stop() and last_stop_date != today_str:
                last_stop_date = today_str
                log(f"🌙 {STOP_HOUR_WAT:02d}:{STOP_MINUTE_WAT:02d} WAT — auto-shutdown triggered.")
                for script, proc in procs.items():
                    if proc and proc.poll() is None:
                        proc.terminate()
                        log(f"   Terminated {script}")
                procs.clear()

                # ── Sleep until 3:50am WAT then restart ───────────────────
                log(f"😴 Sleeping until {RESTART_HOUR_WAT:02d}:{RESTART_MINUTE_WAT:02d} WAT to restart engines...")
                while not _should_restart():
                    time.sleep(60)
                log(f"⏰ {RESTART_HOUR_WAT:02d}:{RESTART_MINUTE_WAT:02d} WAT — restarting engines!")
                for script in SCRIPTS:
                    procs[script] = launch(script)
                    time.sleep(3)
                continue
            # ──────────────────────────────────────────────────────────────
            for script in SCRIPTS:
                proc = procs.get(script)
                if proc is None or proc.poll() is not None:
                    exit_code = proc.returncode if proc else "N/A"
                    delay = backoff[script]
                    log(f"⚠️  {script} stopped (exit {exit_code}) — restarting in {delay}s...")
                    _log.warning(f"{script} crashed with exit code {exit_code}")
                    time.sleep(delay)
                    procs[script] = launch(script)
                    last_restart_ts[script] = time.time()
                    # Exponential backoff: double delay up to max
                    backoff[script] = min(delay * 2, RESTART_DELAY_MAX)
                else:
                    # Reset backoff if process has been stable for RESTART_DELAY_RESET
                    if script in last_restart_ts:
                        if time.time() - last_restart_ts[script] > RESTART_DELAY_RESET:
                            backoff[script] = RESTART_DELAY_BASE
                            del last_restart_ts[script]
                            log(f"🔄 {script} stable — backoff reset")
                    log(f"✅ {script} alive (PID {proc.pid})")
    except KeyboardInterrupt:
        log("\n⛔ Watchdog stopped by user — terminating engines...")
    finally:
        for script, proc in procs.items():
            if proc and proc.poll() is None:
                proc.terminate()
                log(f"   Terminated {script}")


if __name__ == "__main__":
    main()
