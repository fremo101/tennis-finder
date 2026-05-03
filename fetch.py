"""
Fetchers for each platform. Each returns:
  {"court": str, "slots": [{"time": "HH:MM", "url": str}], "error": None|str}
"""

import re
import asyncio
from datetime import datetime, time, timedelta
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup


HOURS = list(range(7, 21))  # 7am–8pm (last bookable start = 8pm)


def _time_str(h, m=0):
    return f"{h:02d}:{m:02d}"


def _slot_times():
    """All 30-min slot starts 7:00–20:00."""
    slots = []
    for h in range(7, 21):
        slots.append(_time_str(h, 0))
        if h < 20:
            slots.append(_time_str(h, 30))
    return slots


def _merge_30min_to_60min(avail_30: set[str]) -> list[str]:
    """
    Return slot starts where the next 30-min slot is also available.
    Works for any start (HH:00 or HH:30).
    """
    result = []
    for t in sorted(avail_30):
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        if not (7 <= h <= 21):
            continue
        nm = m + 30
        nh = h + nm // 60
        nm = nm % 60
        next_t = _time_str(nh, nm)
        if next_t in avail_30:
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Platform: intrac_new (Lyne Park)
# ---------------------------------------------------------------------------

def fetch_intrac_new(venue: dict, date_str: str) -> list[dict]:
    """
    GET /api/getGrid?location_id=N&date=YYYY-MM-DD&mode=space
    Response: {"success": {"data": [{...location info with spaces and hours...}], "data_schedule": [...]}}
    Schedule entries have schedule_start/schedule_finish as "YYYY-MM-DD HH:MM:SS"
    """
    base = venue["intrac_base_url"].rstrip("/")
    location_id = venue["intrac_location_id"]
    booking_url = venue["booking_url"]

    results = []

    def dt_to_mins(dt_str: str) -> int:
        """'2026-04-27 14:00:00' -> minutes since midnight"""
        parts = dt_str.split(" ")
        t = parts[1] if len(parts) > 1 else parts[0]
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        return h * 60 + m

    def hms_to_mins(hms: str) -> int:
        """'06:00:00' or '06:00' -> minutes since midnight"""
        parts = hms.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            client.get(f"{base}/dashboard")  # establish session cookie

            resp = client.get(
                f"{base}/api/getGrid",
                params={"location_id": location_id, "date": date_str, "mode": "space"},
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                return [{"court": venue["name"], "slots": [], "error": str(data["error"])}]

            success = data.get("success", {})
            location_data = success.get("data", [{}])[0]
            schedule = success.get("data_schedule", [])
            spaces = location_data.get("spaces", [])

            hours = location_data.get("hours", {})
            hours_start = hours.get("hours_start", "07:00:00")
            hours_finish = hours.get("hours_finish", "20:00:00")

            start_mins = hms_to_mins(hours_start)
            finish_mins = hms_to_mins(hours_finish)

            # Cap to display range 7am–10pm
            start_mins = max(start_mins, 7 * 60)
            finish_mins = min(finish_mins, 22 * 60)

            # Build booked intervals per space_id
            booked_by_space: dict[int, list[tuple[int, int]]] = {}
            for entry in schedule:
                sid = entry.get("space_id")
                s = dt_to_mins(entry.get("schedule_start", ""))
                e = dt_to_mins(entry.get("schedule_finish", ""))
                booked_by_space.setdefault(sid, []).append((s, e))

            for space in spaces:
                sid = space.get("space_id")
                court_name = space.get("space_name", f"Court {sid}")

                booked = booked_by_space.get(sid, [])

                avail_30 = set()
                cur = start_mins
                while cur + 30 <= finish_mins:
                    slot_end = cur + 30
                    overlaps = any(b_s < slot_end and b_e > cur for b_s, b_e in booked)
                    if not overlaps:
                        avail_30.add(_time_str(cur // 60, cur % 60))
                    cur += 30

                hour_slots = _merge_30min_to_60min(avail_30)
                slots = [{"time": t, "url": booking_url} for t in hour_slots]
                results.append({"court": f"{venue['name']} – {court_name}", "slots": slots, "error": None})

    except Exception as e:
        results.append({"court": venue["name"], "slots": [], "error": str(e)})

    return results


# ---------------------------------------------------------------------------
# Platform: intrac_old (Wentworth, Parklands)
# ColdFusion era: scrape the timetable HTML page
# ---------------------------------------------------------------------------

def fetch_intrac_old(venue: dict, date_str: str) -> list[dict]:
    """
    Old ColdFusion intrac sites. Available slots are <a class="book"> links with
    href="javascript:pop('reserve.cfm?...')" or "javascript:pop('book.cfm?...')".
    Try schedule.cfm first, fall back to book.cfm.
    """
    base = venue["intrac_base_url"].rstrip("/")
    location_id = venue["intrac_location_id"]

    results = []

    def _parse_pop_href(href: str, base_url: str) -> tuple[str, str, str] | None:
        """Extract (court_id, start_hhmm, absolute_url) from javascript:pop('...') href."""
        m = re.search(r"pop\('([^']+)'\)", href)
        if not m:
            return None
        rel = m.group(1)  # e.g. "reserve.cfm?location=80&date=...&start=06:00&court=481"
        params = parse_qs(rel.split("?", 1)[1] if "?" in rel else "")
        court = params.get("court", [None])[0]
        start = params.get("start", [None])[0]
        if not court or not start:
            return None
        # Normalise start time HH:MM
        start = start.strip()
        if re.match(r"^\d:\d\d$", start):
            start = "0" + start
        # Make absolute URL from base (strip last path component)
        base_dir = base_url.rsplit("/", 1)[0]
        abs_url = f"{base_dir}/{rel}"
        return court, start, abs_url

    try:
        with httpx.Client(follow_redirects=True, timeout=20,
                          headers={"User-Agent": "Mozilla/5.0 (compatible)"}) as client:
            # Try schedule.cfm first, then book.cfm
            page_url = None
            resp = None
            for cfm in ("schedule.cfm", "book.cfm"):
                url = f"{base}/{cfm}"
                r = client.get(url, params={"location": location_id, "date": date_str})
                if r.status_code == 200:
                    soup_test = BeautifulSoup(r.text, "html.parser")
                    if soup_test.find("a", class_="book"):
                        page_url = str(r.url)
                        resp = r
                        break

            if resp is None:
                return [{"court": venue["name"], "slots": [], "error": "no timetable page found"}]

            soup = BeautifulSoup(resp.text, "html.parser")
            book_links = soup.find_all("a", class_="book")

            # court_id -> {start_time -> booking_url}
            court_data: dict[str, dict[str, str]] = {}

            for a in book_links:
                href = a.get("href", "")
                parsed = _parse_pop_href(href, page_url)
                if not parsed:
                    continue
                court_id, start, abs_url = parsed
                h, m = int(start.split(":")[0]), int(start.split(":")[1])
                if not (7 <= h <= 21):
                    continue
                court_data.setdefault(court_id, {})[start] = abs_url

            # Detect granularity: if >10% of available times are :30, assume 30-min booking increments
            all_times = [t for tm in court_data.values() for t in tm]
            half_count = sum(1 for t in all_times if t.endswith(":30"))
            uses_half_hours = (len(all_times) > 0 and half_count / len(all_times) > 0.1)

            for court_id, time_url_map in sorted(court_data.items(), key=lambda x: int(x[0])):
                if uses_half_hours:
                    # Need consecutive 30-min pair to form a 60-min slot
                    avail_30 = set(time_url_map.keys())
                    hour_starts = _merge_30min_to_60min(avail_30)
                    slots = [{"time": t, "url": time_url_map[t]} for t in hour_starts]
                else:
                    # 1-hour granularity: each available :00 slot is already a 60-min booking
                    hour_starts = sorted(
                        t for t in time_url_map
                        if t.endswith(":00") and 7 <= int(t.split(":")[0]) <= 21
                    )
                    slots = [{"time": t, "url": time_url_map[t]} for t in hour_starts]
                results.append({
                    "court": f"{venue['name']} – Court {court_id}",
                    "slots": slots,
                    "error": None,
                })

    except Exception as e:
        results.append({"court": venue["name"], "slots": [], "error": str(e)})

    return results


# ---------------------------------------------------------------------------
# Platform: tennisvenues (Snape Park, Bondi)
# ---------------------------------------------------------------------------

def fetch_tennisvenues(venue: dict, date_str: str) -> list[dict]:
    """
    TennisVenues AJAX endpoint:
      GET /booking/{slug}/fetch-booking-data
      ?client_id={slug}&venue_id=1&resource_id=&date=YYYYMMDD&view=v3
    Returns HTML fragment with <div class="v3-court-slots" id="v3_slots_C1"> per court.
    Available slots: <a class="v3-slot-btn" href="/booking/request?...&t=HHMM&...">
    Unavailable: <div class="v3-slot-btn v3-slot-unavailable">
    """
    slug = venue["tennisvenues_slug"]
    date_compact = date_str.replace("-", "")  # YYYYMMDD

    results = []
    BASE = "https://www.tennisvenues.com.au"

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/booking/{slug}/timeslot",
        }
        with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
            # Establish session
            client.get(f"{BASE}/booking/{slug}/timeslot", params={"date": date_compact})

            resp = client.get(
                f"{BASE}/booking/{slug}/fetch-booking-data",
                params={
                    "client_id": slug,
                    "venue_id": "1",
                    "resource_id": "",
                    "date": date_compact,
                    "view": "v3",
                },
            )
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "html.parser")

            court_sections = soup.find_all("div", class_="v3-court-slots")
            if not court_sections:
                return [{"court": venue["name"], "slots": [], "error": "no court sections in AJAX response"}]

            # Court name mapping from court buttons
            court_names: dict[str, str] = {}
            for btn in soup.find_all("button", class_="v3-court-btn"):
                btn_id = btn.get("id", "")  # e.g. "v3_court_btn_C1"
                court_id = btn_id.replace("v3_court_btn_", "") if btn_id.startswith("v3_court_btn_") else ""
                name = btn.get_text(strip=True)
                if court_id and name:
                    court_names[court_id] = name

            for section in court_sections:
                section_id = section.get("id", "")  # "v3_slots_C1"
                court_id = section_id.replace("v3_slots_", "") if section_id.startswith("v3_slots_") else section_id
                court_label = court_names.get(court_id, court_id or "Court")

                time_url_map: dict[str, str] = {}
                for a in section.find_all("a", class_="v3-slot-btn"):
                    if "v3-slot-unavailable" in a.get("class", []):
                        continue
                    href = a.get("href", "")
                    # Extract t=HHMM from URL
                    t_match = re.search(r"[?&]t=(\d{3,4})", href)
                    if not t_match:
                        continue
                    t_raw = t_match.group(1).zfill(4)  # "930" → "0930"
                    h, m = int(t_raw[:2]), int(t_raw[2:])
                    if not (7 <= h <= 21):
                        continue
                    slot_time = _time_str(h, m)
                    abs_url = BASE + href if href.startswith("/") else href
                    time_url_map[slot_time] = abs_url

                avail_30 = set(time_url_map.keys())
                hour_slots = _merge_30min_to_60min(avail_30)
                slots = [{"time": t, "url": time_url_map[t]} for t in hour_slots]
                results.append({
                    "court": f"{venue['name']} – {court_label}",
                    "slots": slots,
                    "error": None,
                })

    except Exception as e:
        results.append({"court": venue["name"], "slots": [], "error": str(e)})

    return results


# ---------------------------------------------------------------------------
# Platform: sportsguru (White City) — requires Playwright
# ---------------------------------------------------------------------------

def _find_60min_from_15min(avail_times: set[str]) -> list[str]:
    """Return starts where the next 3 x 15-min slots are also available (= 60 min free)."""
    result = []
    for t in sorted(avail_times):
        h, m = int(t.split(":")[0]), int(t.split(":")[1])
        if not (7 <= h < 22):
            continue
        needed = []
        for offset in (15, 30, 45):
            tot = h * 60 + m + offset
            needed.append(_time_str(tot // 60, tot % 60))
        if all(n in avail_times for n in needed):
            result.append(t)
    return result


def fetch_sportsguru(venue: dict, date_str: str) -> list[dict]:
    """
    White City Tennis (SportsGuru/InTennis platform).
    Fetches the calendar-widget AJAX endpoint directly (no browser required).
    Available slots: <button class="btn btn-success" onclick="checkResourceAvailability('HH:MM:SS','C1','/...book?...',false)">
    Uses 15-min granularity; require 4 consecutive available slots for a 60-min block.
    """
    venue_url = venue["sportsguru_url"]
    BASE = "https://whitecity.intennis.com.au"
    date_compact = date_str.replace("-", "")  # YYYYMMDD
    results = []

    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        if (target - today).days < 0:
            return [{"court": venue["name"], "slots": [], "error": "date is in the past"}]

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": venue_url,
        }
        with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
            client.get(venue_url)  # establish session cookie
            resp = client.get(
                f"{BASE}/secure/customer/booking/v2/public/calendar-widget",
                params={"date": date_compact},
            )
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")

        court_data: dict[str, dict[str, str]] = {}

        for btn in soup.find_all("button", class_="btn-success"):
            onclick = btn.get("onclick", "")
            m = re.match(
                r"checkResourceAvailability\('(\d{1,2}:\d{2}:\d{2})','([^']+)','([^']+)'",
                onclick,
            )
            if not m:
                continue
            time_hms, court_id, rel_url = m.group(1), m.group(2), m.group(3)
            h, min_ = int(time_hms.split(":")[0]), int(time_hms.split(":")[1])
            if not (7 <= h <= 21):
                continue
            slot_time = _time_str(h, min_)
            abs_url = BASE + rel_url if rel_url.startswith("/") else rel_url
            court_data.setdefault(court_id, {})[slot_time] = abs_url

        if not court_data:
            return [{"court": venue["name"], "slots": [], "error": None}]

        for court_id, time_url_map in sorted(court_data.items()):
            avail_15 = set(time_url_map.keys())
            hour_starts = _find_60min_from_15min(avail_15)
            slots = [{"time": t, "url": time_url_map[t]} for t in hour_starts]
            results.append({
                "court": f"{venue['name']} – {court_id}",
                "slots": slots,
                "error": None,
            })

    except Exception as e:
        results.append({"court": venue["name"], "slots": [], "error": str(e)})

    return results


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

FETCHERS = {
    "intrac_new": fetch_intrac_new,
    "intrac_old": fetch_intrac_old,
    "tennisvenues": fetch_tennisvenues,
    "sportsguru": fetch_sportsguru,
}


def fetch_venue(venue: dict, date_str: str) -> list[dict]:
    platform = venue["platform"]
    fn = FETCHERS.get(platform)
    if fn is None:
        return [{"court": venue["name"], "slots": [], "error": f"unknown platform: {platform}"}]
    return fn(venue, date_str)
