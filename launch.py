"""
Twitter Agent Launcher
======================
Starts all three scripts in separate console windows with one command.

Usage:
  python launch.py            # start everything live
  python launch.py --dry-run  # trend_poster in dry-run mode, others live
  python launch.py --stop     # kill all running agent windows
"""

import argparse
import subprocess
import sys
import os
from pathlib import Path

HERE = Path(__file__).parent

SCRIPTS = [
    {
        "name": "Trend Poster",
        "script": "playwright_trend_poster.py",
        "title": "MUFC Agent — Trend Poster",
        "dry_run_flag": True,   # supports --dry-run
    },
    {
        "name": "Fast Replier",
        "script": "playwright_fast_replier.py",
        "title": "MUFC Agent — Fast Replier",
        "dry_run_flag": False,
    },
    {
        "name": "Notification Responder",
        "script": "playwright_notification_responder.py",
        "title": "MUFC Agent — Notification Responder",
        "dry_run_flag": False,
    },
]

WINDOW_TITLES = [s["title"] for s in SCRIPTS]


def start_all(dry_run: bool = False):
    python = sys.executable
    processes = []

    print("\n🤖 MUFC Twitter Agent — Launching all scripts\n")

    for s in SCRIPTS:
        script_path = HERE / s["script"]
        if not script_path.exists():
            print(f"  ⚠️  {s['script']} not found — skipping")
            continue

        cmd = [python, str(script_path)]
        if dry_run and s["dry_run_flag"]:
            cmd.append("--dry-run")

        # Windows: open each in a new titled console window
        full_cmd = [
            "cmd", "/c", "start",
            f'"{s["title"]}"',          # window title
            "cmd", "/k",                # keep window open after exit
        ] + cmd

        proc = subprocess.Popen(
            " ".join(full_cmd),
            shell=True,
            cwd=str(HERE),
        )
        processes.append(proc)
        status = " [DRY-RUN]" if (dry_run and s["dry_run_flag"]) else ""
        print(f"  ✅ {s['name']}{status} — started in new window")

    print(f"\n🚀 {len(processes)} script(s) running.")
    print("   Close each window individually to stop a script.")
    print("   Or run:  python launch.py --stop\n")


def stop_all():
    """Kill all python processes running our agent scripts."""
    script_names = [s["script"] for s in SCRIPTS]
    killed = 0

    print("\n🛑 Stopping all agent scripts …\n")

    for script in script_names:
        # Use taskkill to find and kill python processes with this script in their commandline
        result = subprocess.run(
            f'wmic process where "name=\'python.exe\' and commandline like \'%{script}%\'" '
            f'call terminate',
            shell=True,
            capture_output=True,
            text=True,
        )
        if "successful" in result.stdout.lower() or "ReturnValue = 0" in result.stdout:
            print(f"  ✅ Stopped: {script}")
            killed += 1
        else:
            print(f"  ℹ️  Not running: {script}")

    if killed:
        print(f"\n  {killed} script(s) stopped.")
    else:
        print("\n  Nothing was running.")


def main():
    parser = argparse.ArgumentParser(description="MUFC Twitter Agent Launcher")
    parser.add_argument("--dry-run", action="store_true",
                        help="Start trend_poster in dry-run mode (no real posts)")
    parser.add_argument("--stop", action="store_true",
                        help="Stop all running agent scripts")
    args = parser.parse_args()

    # Check .env exists
    env_file = HERE / ".env"
    if not env_file.exists() and not args.stop:
        print("❌ .env file not found. Copy .env.example and fill in your credentials.")
        sys.exit(1)

    if args.stop:
        stop_all()
    else:
        start_all(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
