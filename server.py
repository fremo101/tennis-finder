#!/usr/bin/env python3
"""Flask web server for interactive court availability browsing."""

import json
import os
import time
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Clear Python cache BEFORE importing local modules to prevent __pycache__ corruption
for p in Path(__file__).parent.glob("**/__pycache__"):
    shutil.rmtree(p, ignore_errors=True)

from flask import Flask, request

from fetch import fetch_venue
from render import build_html

app = Flask(__name__)
_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = 600


def load_config() -> list[dict]:
    p = Path(__file__).parent / "config.json"
    with open(p) as f:
        return json.load(f)


PLATFORM_LABELS = {
    "intrac_new": "Intrac",
    "intrac_old": "Intrac",
    "tennisvenues": "TennisVenues",
    "sportsguru": "SportsGuru",
}


@app.route("/")
def index():
    date_str = request.args.get("date")
    if not date_str:
        landing = Path(__file__).parent / "landing.html"
        return landing.read_text()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD.", 400

    # Check cache
    if date_str in _cache:
        ts, html = _cache[date_str]
        if time.time() - ts < CACHE_TTL:
            print(f"[{datetime.now().isoformat()}] Serving {date_str} from cache", flush=True)
            return html

    print(f"[{datetime.now().isoformat()}] Fetching {date_str}...", flush=True)
    venues = load_config()
    all_results = []

    def fetch_one(venue):
        try:
            print(f"  → {venue['name']} ({venue['platform']})...", flush=True)
            rows = fetch_venue(venue, date_str)
            for r in rows:
                r["platform"] = PLATFORM_LABELS.get(venue["platform"], venue["platform"])
                r["surface"] = venue.get("surface", "")
            print(f"     {venue['name']}: OK ({len(rows)} courts)", flush=True)
            return rows
        except Exception as e:
            print(f"     {venue['name']}: ERROR — {e}", flush=True)
            return [
                {
                    "court": venue["name"],
                    "slots": [],
                    "error": str(e),
                    "platform": PLATFORM_LABELS.get(venue["platform"], venue["platform"]),
                    "surface": venue.get("surface", ""),
                }
            ]

    # Parallel fetch: all non-sportsguru venues
    parallel_venues = [v for v in venues if v["platform"] != "sportsguru"]
    playwright_venues = [v for v in venues if v["platform"] == "sportsguru"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_one, v): v for v in parallel_venues}
        for fut in as_completed(futures):
            all_results.extend(fut.result())

    # Sequential fetch: sportsguru (Playwright)
    for v in playwright_venues:
        all_results.extend(fetch_one(v))

    print(f"[{datetime.now().isoformat()}] Rendering HTML...", flush=True)
    html = build_html(all_results, date_str, venues)
    _cache[date_str] = (time.time(), html)
    print(f"[{datetime.now().isoformat()}] Complete", flush=True)
    return html


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, threaded=True, port=port)
