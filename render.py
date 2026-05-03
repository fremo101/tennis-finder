"""Build the HTML output from fetched results."""

from collections import defaultdict, OrderedDict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _all_half_hours() -> list[str]:
    """Return all 30-min slot starts from 07:00 to 21:30."""
    slots = []
    for h in range(7, 22):
        slots.append(f"{h:02d}:00")
        slots.append(f"{h:02d}:30")
    return slots


def _slot_label(slot: str) -> str:
    h, m = int(slot.split(":")[0]), int(slot.split(":")[1])
    display_h = h if h <= 12 else h - 12
    suffix = "am" if h < 12 else "pm"
    return f"{display_h}:{m:02d}{suffix}"


TIME_COLS = [{"slot": s, "label": _slot_label(s)} for s in _all_half_hours()]


def _venue_name(court: str) -> str:
    return court.split(" – ")[0].strip() if " – " in court else court


def _court_label(court: str) -> str:
    parts = court.split(" – ")
    return parts[1].strip() if len(parts) > 1 else "Main"


def build_html(results: list[dict], date_str: str, venues: list[dict]) -> str:
    """
    results: flat list of {"court": str, "slots": [...], "error": None|str, "platform": str, "surface": str}
    """
    rows = []
    total_slots = 0
    errors = 0
    venue_dict: dict = OrderedDict()

    for r in results:
        slot_map = {}
        for s in r.get("slots", []):
            t = s["time"]  # "HH:MM"
            h = int(t.split(":")[0])
            if 7 <= h <= 21:
                slot_map[t] = s["url"]
        total_slots += len(slot_map)
        if r.get("error"):
            errors += 1
        rows.append({
            "court": r["court"],
            "platform": r.get("platform", ""),
            "surface": r.get("surface", ""),
            "slot_map": slot_map,
            "error": r.get("error"),
        })

        vname = _venue_name(r["court"])
        clabel = _court_label(r["court"])
        if vname not in venue_dict:
            venue_dict[vname] = {
                "name": vname,
                "slots": defaultdict(list),
                "error": None,
                "platform": r.get("platform", ""),
                "surface": r.get("surface", ""),
            }
        if r.get("error") and not venue_dict[vname]["error"]:
            venue_dict[vname]["error"] = r["error"]
        for t, url in slot_map.items():
            venue_dict[vname]["slots"][t].append({"label": clabel, "url": url})

    venue_rows = [
        {
            "name": info["name"],
            "slots": dict(info["slots"]),
            "error": info["error"],
            "platform": info["platform"],
            "surface": info["surface"],
        }
        for info in venue_dict.values()
    ]

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_display = dt.strftime("%A %-d %B %Y")
    generated_at = datetime.now().strftime("%H:%M")
    generated_iso = datetime.now().isoformat()

    env = Environment(loader=FileSystemLoader(Path(__file__).parent))
    tmpl = env.get_template("template.html")

    return tmpl.render(
        rows=rows,
        venue_rows=venue_rows,
        time_cols=TIME_COLS,
        date_str=date_str,
        date_display=date_display,
        generated_at=generated_at,
        generated_iso=generated_iso,
        total_slots=total_slots,
        venue_count=len(venues),
        errors=errors,
    )
