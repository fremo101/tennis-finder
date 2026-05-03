#!/usr/bin/env python3
"""
Usage: python run.py --date 2026-04-27
Outputs: tennis_YYYY-MM-DD.html (opened in browser automatically)
"""

import argparse
import json
import shutil
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Clear Python cache BEFORE importing local modules to prevent __pycache__ corruption
for p in Path(__file__).parent.glob("**/__pycache__"):
    shutil.rmtree(p, ignore_errors=True)

from fetch import fetch_venue
from render import build_html


def _check_dependencies():
    """Fail fast if dependencies are corrupted."""
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError as e:
        print(f"❌ Dependency corruption detected: {e}")
        print("\nFix this by running:")
        print("  source /Users/deanfremder/Documents/Coding/tennis-finder/venv/bin/activate")
        print("  pip install -r /Users/deanfremder/Documents/Coding/tennis-finder/requirements.lock")
        sys.exit(1)


def load_config() -> list[dict]:
    p = Path(__file__).parent / "config.json"
    with open(p) as f:
        return json.load(f)


def platform_label(venue: dict) -> str:
    return {
        "intrac_new": "Intrac",
        "intrac_old": "Intrac",
        "tennisvenues": "TennisVenues",
        "sportsguru": "SportsGuru",
    }.get(venue["platform"], venue["platform"])


def main():
    _check_dependencies()
    parser = argparse.ArgumentParser(description="Sydney Eastern Suburbs Tennis Court Finder")
    parser.add_argument("--date", required=True, help="Date to check (YYYY-MM-DD)")
    parser.add_argument("--output", help="Output HTML path (default: tennis_YYYY-MM-DD.html)")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        print(f"Error: invalid date '{args.date}', expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    venues = load_config()
    print(f"Checking {len(venues)} venues for {args.date}...")

    all_results = []

    def fetch_one(venue):
        print(f"  → {venue['name']} ({venue['platform']})...", flush=True)
        try:
            rows = fetch_venue(venue, args.date)
            for r in rows:
                r["platform"] = platform_label(venue)
                r["surface"] = venue.get("surface", "")
            ok = sum(1 for r in rows if not r.get("error"))
            total = sum(len(r.get("slots", [])) for r in rows)
            print(f"     {venue['name']}: {ok} courts, {total} available hour(s)", flush=True)
            return rows
        except Exception as e:
            print(f"     {venue['name']}: ERROR — {e}", flush=True)
            return [{"court": venue["name"], "slots": [], "error": str(e),
                     "platform": platform_label(venue), "surface": venue.get("surface", "")}]

    # Run intrac_new/old and tennisvenues in parallel; sportsguru (Playwright) separately
    parallel_venues = [v for v in venues if v["platform"] != "sportsguru"]
    playwright_venues = [v for v in venues if v["platform"] == "sportsguru"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_one, v): v for v in parallel_venues}
        for fut in as_completed(futures):
            all_results.extend(fut.result())

    # Playwright venues run sequentially (browser is heavy)
    for v in playwright_venues:
        all_results.extend(fetch_one(v))

    html = build_html(all_results, args.date, venues)

    out_path = args.output or f"tennis_{args.date}.html"
    out_file = Path(out_path)
    out_file.write_text(html, encoding="utf-8")
    print(f"\nOutput: {out_file.resolve()}")

    if not args.no_open:
        webbrowser.open(out_file.resolve().as_uri())


if __name__ == "__main__":
    main()
