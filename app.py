import sqlite3
import csv
import io
import os
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, Response, send_file
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
DB_PATH = "tracker.db"
PHOTO_UPLOAD_DIR = os.path.join("static", "uploads", "photo_year")

# Background themes for the Photo Year A3 print - each just changes the accent colors
PHOTO_YEAR_THEMES = {
    "soft_blue": {"label": "Soft Blue", "bg": (234, 244, 255), "accent": (74, 144, 217), "text": (44, 95, 138)},
    "warm_peach": {"label": "Warm Peach", "bg": (255, 241, 230), "accent": (224, 139, 92), "text": (150, 82, 40)},
    "mint_pastel": {"label": "Mint Pastel", "bg": (232, 248, 240), "accent": (85, 172, 132), "text": (40, 110, 78)},
}

# Event types and what unit each one is measured in
EVENT_TYPES = {
    "breast": {"label": "Breast Feed", "unit": "minutes"},
    "express": {"label": "Express Milk", "unit": "ml"},
    "formula": {"label": "Formula", "unit": "ml"},
    "pump": {"label": "Pump", "unit": "ml"},
    "poo": {"label": "Poo", "unit": None},
    "pee": {"label": "Pee", "unit": None},
}

# Types that count as an actual feed, for the "last fed" banner
FEED_TYPES = ("breast", "express", "formula")

# Accepted date formats for CSV import, tried in order
DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d"]

# Ethan's birth date - used to calculate current age for the guidance page
BABY_BIRTH_DATE = date(2026, 7, 3)

# General age-based guidance, roughly following common CDC/AAP-style newborn guidance.
# Each entry applies from its "from_days" onward, in ascending order.
# This is general reference info, not medical advice - always defer to a pediatrician.
AGE_GUIDANCE = [
    {
        "from_days": 0,
        "label": "0–2 weeks",
        "nappy_size": "Newborn (size 1), up to ~5kg",
        "feed_volume": "~30–90ml per feed, every 2–3 hours (8–12 feeds/day)",
        "sleep": "14–17 hours total per day, in short stretches",
        "milestones": "Lifts head briefly, focuses on faces ~20–30cm away, strong reflexes (grasp, startle)",
    },
    {
        "from_days": 14,
        "label": "2–6 weeks",
        "nappy_size": "Size 1–2, ~4–8kg depending on brand",
        "feed_volume": "~60–120ml per feed, every 2–4 hours",
        "sleep": "14–17 hours total, starting slightly longer night stretches",
        "milestones": "Starts to smile socially, follows objects with eyes, more alert periods",
    },
    {
        "from_days": 42,
        "label": "6–12 weeks",
        "nappy_size": "Size 2, ~4–8kg",
        "feed_volume": "~90–150ml per feed, every 3–4 hours",
        "sleep": "14–16 hours total, night stretches often 4–6 hours",
        "milestones": "Holds head up during tummy time, coos, tracks moving objects well",
    },
    {
        "from_days": 84,
        "label": "3–4 months",
        "nappy_size": "Size 2–3, ~5–9kg",
        "feed_volume": "~120–180ml per feed, every 3–4 hours",
        "sleep": "12–16 hours total, more consolidated night sleep",
        "milestones": "Pushes up on arms, rolls front-to-back, laughs out loud, reaches for objects",
    },
    {
        "from_days": 120,
        "label": "4–6 months",
        "nappy_size": "Size 3, ~5–9kg",
        "feed_volume": "~180–240ml per feed, 4–6 feeds/day; solids may begin around 6 months",
        "sleep": "12–15 hours total, typically 2–3 daytime naps",
        "milestones": "Rolls both ways, sits with support, babbles, brings objects to mouth",
    },
    {
        "from_days": 180,
        "label": "6–9 months",
        "nappy_size": "Size 3–4, ~7–12kg",
        "feed_volume": "Milk/formula plus 2–3 solid meals/day",
        "sleep": "12–15 hours total, usually 2 naps",
        "milestones": "Sits unassisted, may start crawling, responds to name, picks up small objects",
    },
]


def get_age_guidance(age_days):
    """Return the most relevant guidance entry for the given age in days."""
    match = AGE_GUIDANCE[0]
    for entry in AGE_GUIDANCE:
        if age_days >= entry["from_days"]:
            match = entry
        else:
            break
    return match


def parse_flexible_date(raw_date):
    """Try several common date formats and normalize to YYYY-MM-DD. Returns None if none match."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_tags(raw_tags):
    """Turn a comma-separated tag string into a clean list: trimmed, deduped, empty entries dropped."""
    if not raw_tags:
        return []
    seen = []
    for t in raw_tags.split(","):
        t = t.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def format_tags(tag_list):
    """Turn a list of tags back into the comma-separated string stored in the database."""
    return ", ".join(tag_list) if tag_list else None

# Quick-log presets shown as one-tap buttons on the home page
QUICK_LOG_PRESETS = [
    {"label": "Formula 90ml", "type": "formula", "value": 90},
    {"label": "Formula 120ml", "type": "formula", "value": 120},
    {"label": "Express 100ml", "type": "express", "value": 100},
    {"label": "Poo", "type": "poo", "value": 1},
    {"label": "Pee", "type": "pee", "value": 1},
]


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            type TEXT NOT NULL,
            value REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS journal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            event TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weight_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            value REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_photos (
            month_number INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_photos (
            week_number INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # One-time migration: move any weight entries logged the old way (inside the
    # generic 'entries' table) into the new dedicated weight_entries table.
    # Safe to run every startup - only migrates rows that haven't been moved yet.
    old_weight_rows = conn.execute(
        "SELECT * FROM entries WHERE type = 'weight' ORDER BY entry_date, entry_time"
    ).fetchall()
    if old_weight_rows:
        for row in old_weight_rows:
            already_migrated = conn.execute(
                "SELECT 1 FROM weight_entries WHERE entry_date = ? AND value = ?",
                (row["entry_date"], row["value"]),
            ).fetchone()
            if not already_migrated:
                conn.execute(
                    "INSERT INTO weight_entries (entry_date, value, created_at) VALUES (?, ?, ?)",
                    (row["entry_date"], row["value"], row["created_at"]),
                )
        conn.execute("DELETE FROM entries WHERE type = 'weight'")
        conn.commit()

    # One-time migration: add a 'tags' column to journal_entries if it doesn't
    # already exist (needed since the table was created before tags existed).
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(journal_entries)").fetchall()]
    if "tags" not in existing_columns:
        conn.execute("ALTER TABLE journal_entries ADD COLUMN tags TEXT")
        conn.commit()

    conn.close()


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        entry_date = request.form.get("entry_date") or date.today().isoformat()
        entry_time = request.form.get("entry_time") or datetime.now().strftime("%H:%M")
        etype = request.form.get("type")
        value = request.form.get("value")

        # poo/pee have no numeric value - just log 1 as a marker
        if etype in ("poo", "pee"):
            value = 1
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None

        conn = get_db()
        conn.execute(
            "INSERT INTO entries (entry_date, entry_time, type, value, created_at) VALUES (?, ?, ?, ?, ?)",
            (entry_date, entry_time, etype, value, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("day_view", entry_date=entry_date))

    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M")
    last_fed = get_last_fed()
    journal_date_prefill = request.args.get("journal_date") or today
    scroll_to_journal = bool(request.args.get("journal_date"))
    weight_date_prefill = request.args.get("weight_date") or today
    scroll_to_weight = bool(request.args.get("weight_date"))
    daily_date_prefill = request.args.get("daily_date") or today
    scroll_to_daily = bool(request.args.get("daily_date"))
    return render_template(
        "index.html",
        event_types=EVENT_TYPES,
        today=today,
        now=now,
        quick_presets=QUICK_LOG_PRESETS,
        last_fed=last_fed,
        journal_date_prefill=journal_date_prefill,
        scroll_to_journal=scroll_to_journal,
        weight_date_prefill=weight_date_prefill,
        scroll_to_weight=scroll_to_weight,
        daily_date_prefill=daily_date_prefill,
        scroll_to_daily=scroll_to_daily,
        active="home",
    )


@app.route("/quick-log", methods=["POST"])
def quick_log():
    """One-tap logging using a preset - always logs at the current date/time."""
    etype = request.form.get("type")
    value = request.form.get("value")
    entry_date = date.today().isoformat()
    entry_time = datetime.now().strftime("%H:%M")

    try:
        value = float(value)
    except (TypeError, ValueError):
        value = None

    conn = get_db()
    conn.execute(
        "INSERT INTO entries (entry_date, entry_time, type, value, created_at) VALUES (?, ?, ?, ?, ?)",
        (entry_date, entry_time, etype, value, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("day_view", entry_date=entry_date))


def compute_totals(rows):
    totals = {
        "breast_minutes": 0,
        "pump_ml": 0,
        "pump_count": 0,
        "express_ml": 0,
        "formula_ml": 0,
        "poo_count": 0,
        "pee_count": 0,
    }
    for r in rows:
        v = r["value"] or 0
        if r["type"] == "breast":
            totals["breast_minutes"] += v
        elif r["type"] == "pump":
            totals["pump_ml"] += v
            totals["pump_count"] += 1
        elif r["type"] == "express":
            totals["express_ml"] += v
        elif r["type"] == "formula":
            totals["formula_ml"] += v
        elif r["type"] == "poo":
            totals["poo_count"] += 1
        elif r["type"] == "pee":
            totals["pee_count"] += 1

    totals["overall_milk_ml"] = totals["express_ml"] + totals["formula_ml"]
    hours, mins = divmod(int(totals["breast_minutes"]), 60)
    totals["breast_time_display"] = f"{hours}h {mins}m" if hours else f"{mins}m"
    return totals


def get_last_fed():
    """Find the most recent feed entry (breast/express/formula) across all history."""
    placeholders = ",".join("?" * len(FEED_TYPES))
    conn = get_db()
    row = conn.execute(
        f"SELECT * FROM entries WHERE type IN ({placeholders}) "
        "ORDER BY entry_date DESC, entry_time DESC LIMIT 1",
        FEED_TYPES,
    ).fetchone()
    conn.close()

    if row is None:
        return None

    fed_at = datetime.strptime(f"{row['entry_date']} {row['entry_time']}", "%Y-%m-%d %H:%M")
    elapsed = datetime.now() - fed_at
    total_minutes = int(elapsed.total_seconds() // 60)
    hours, mins = divmod(max(total_minutes, 0), 60)
    if hours > 0:
        ago = f"{hours}h {mins}m ago"
    else:
        ago = f"{mins}m ago"

    total_hours = total_minutes / 60
    if total_hours >= 4:
        status = "danger"
    elif total_hours >= 3:
        status = "warning"
    else:
        status = "normal"

    return {
        "type_label": EVENT_TYPES[row["type"]]["label"],
        "time": row["entry_time"],
        "ago": ago,
        "status": status,
    }


@app.route("/day")
def day_view():
    entry_date = request.args.get("entry_date") or date.today().isoformat()

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE entry_date = ? ORDER BY entry_time",
        (entry_date,),
    ).fetchall()
    conn.close()

    totals = compute_totals(rows)
    last_fed = get_last_fed()

    d = datetime.strptime(entry_date, "%Y-%m-%d").date()
    prev_date = (d - timedelta(days=1)).isoformat()
    next_date = (d + timedelta(days=1)).isoformat()
    is_today = (d == date.today())

    return render_template(
        "day.html",
        entry_date=entry_date,
        rows=rows,
        totals=totals,
        event_types=EVENT_TYPES,
        last_fed=last_fed,
        prev_date=prev_date,
        next_date=next_date,
        is_today=is_today,
        active="day",
    )


@app.route("/week")
def week_view():
    # anchor date - defaults to today, week runs Fri-Thu containing that date
    # (Ethan was born on a Friday, so weeks start there)
    anchor_str = request.args.get("entry_date") or date.today().isoformat()
    anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    days_since_friday = (anchor.weekday() - 4) % 7  # Friday = weekday() 4
    week_start = anchor - timedelta(days=days_since_friday)
    week_end = week_start + timedelta(days=6)  # Thursday

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date, entry_time",
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    conn.close()

    # group rows by date
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        d_str = d.isoformat()
        day_rows = [r for r in rows if r["entry_date"] == d_str]
        day_totals = compute_totals(day_rows)
        days.append({"date": d_str, "label": d.strftime("%a %d %b"), "totals": day_totals, "entry_count": len(day_rows)})

    week_totals = compute_totals(rows)

    prev_week_anchor = (week_start - timedelta(days=7)).isoformat()
    next_week_anchor = (week_start + timedelta(days=7)).isoformat()
    is_current_week = (week_start <= date.today() <= week_end)

    # Ethan's birth date is itself a Friday, so it's the start of his "Week 0"
    week_number = (week_start - BABY_BIRTH_DATE).days // 7
    is_birth_week = (week_number <= 0)
    last_fed = get_last_fed()

    return render_template(
        "week.html",
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        days=days,
        week_totals=week_totals,
        anchor=anchor_str,
        prev_week_anchor=prev_week_anchor,
        next_week_anchor=next_week_anchor,
        is_current_week=is_current_week,
        week_number=week_number,
        is_birth_week=is_birth_week,
        birth_week_anchor=BABY_BIRTH_DATE.isoformat(),
        last_fed=last_fed,
        today=date.today().isoformat(),
        active="week",
    )


@app.route("/weight")
def weight_view():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM weight_entries ORDER BY entry_date"
    ).fetchall()
    conn.close()

    points = [{"id": r["id"], "date": r["entry_date"], "value": r["value"]} for r in rows]

    chart_svg = None
    if len(points) >= 1:
        chart_svg = build_weight_chart_svg(points)

    latest = points[-1] if points else None
    change = None
    if len(points) >= 2:
        diff = points[-1]["value"] - points[-2]["value"]
        change = f"+{diff:.2f} kg" if diff >= 0 else f"{diff:.2f} kg"

    return render_template(
        "weight.html",
        points=list(reversed(points)),  # newest first for the table
        chart_svg=chart_svg,
        latest=latest,
        change=change,
        today=date.today().isoformat(),
        active="weight",
    )


@app.route("/weight/report")
def weight_report():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM weight_entries ORDER BY entry_date"
    ).fetchall()
    conn.close()

    points = [{"id": r["id"], "date": r["entry_date"], "value": r["value"]} for r in rows]

    chart_svg = None
    if len(points) >= 1:
        chart_svg = build_weight_chart_svg(points)

    latest = points[-1] if points else None
    change = None
    if len(points) >= 2:
        diff = points[-1]["value"] - points[-2]["value"]
        change = f"+{diff:.2f} kg" if diff >= 0 else f"{diff:.2f} kg"

    return render_template(
        "weight_report.html",
        points=list(reversed(points)),  # newest first for the table
        chart_svg=chart_svg,
        latest=latest,
        change=change,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/weight/add", methods=["POST"])
def weight_add():
    entry_date = request.form.get("entry_date") or date.today().isoformat()
    raw_value = request.form.get("value")

    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return redirect(url_for("weight_view"))

    conn = get_db()
    conn.execute(
        "INSERT INTO weight_entries (entry_date, value, created_at) VALUES (?, ?, ?)",
        (entry_date, value, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("weight_view"))


@app.route("/weight/edit/<int:entry_id>", methods=["GET", "POST"])
def weight_edit(entry_id):
    conn = get_db()

    if request.method == "POST":
        entry_date = request.form.get("entry_date")
        try:
            value = float(request.form.get("value"))
        except (TypeError, ValueError):
            conn.close()
            return redirect(url_for("weight_view"))

        conn.execute(
            "UPDATE weight_entries SET entry_date = ?, value = ? WHERE id = ?",
            (entry_date, value, entry_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("weight_view"))

    row = conn.execute("SELECT * FROM weight_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    if row is None:
        return redirect(url_for("weight_view"))

    return render_template("weight_edit.html", row=row, active="weight")


@app.route("/weight/delete/<int:entry_id>", methods=["POST"])
def weight_delete(entry_id):
    conn = get_db()
    conn.execute("DELETE FROM weight_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("weight_view"))


    return render_template(
        "import.html",
        journal_message=message, journal_error=error, journal_row_errors=row_errors,
        entries_message=None, entries_error=None, entries_row_errors=[],
        weight_message=None, weight_error=None, weight_row_errors=[],
        event_types=EVENT_TYPES, active="import",
    )


@app.route("/weight/import", methods=["POST"])
def weight_import():
    message = None
    error = None
    row_errors = []

    file = request.files.get("csv_file")
    if not file or file.filename == "":
        error = "No file selected."
    else:
        try:
            content = file.stream.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(content))

            valid_rows = []
            for line_num, row in enumerate(reader, start=2):
                raw_date = (row.get("Date") or "").strip()
                raw_value = (row.get("Weight") or row.get("Value") or "").strip()

                normalized_date = parse_flexible_date(raw_date)
                if not normalized_date:
                    row_errors.append(f"Row {line_num}: date '{raw_date}' isn't in a recognized format (try YYYY-MM-DD or DD/MM/YYYY)")
                    continue

                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    row_errors.append(f"Row {line_num}: weight '{raw_value}' isn't a valid number")
                    continue

                valid_rows.append((normalized_date, value))

            if row_errors:
                error = (
                    f"Import stopped — {len(row_errors)} row(s) had problems, and nothing was written "
                    f"to the database. Fix these and re-upload:"
                )
            elif not valid_rows:
                error = "No valid rows found in that file."
            else:
                conn = get_db()
                try:
                    now_iso = datetime.now().isoformat()
                    conn.executemany(
                        "INSERT INTO weight_entries (entry_date, value, created_at) VALUES (?, ?, ?)",
                        [(d, v, now_iso) for d, v in valid_rows],
                    )
                    conn.commit()
                    message = f"Imported {len(valid_rows)} weight entries successfully."
                except Exception as e:
                    conn.rollback()
                    error = f"Import failed partway through and was rolled back — nothing was saved. Error: {e}"
                finally:
                    conn.close()

        except Exception as e:
            error = f"Could not read that file: {e}"

    return render_template(
        "import.html",
        weight_message=message, weight_error=error, weight_row_errors=row_errors,
        entries_message=None, entries_error=None, entries_row_errors=[],
        journal_message=None, journal_error=None, journal_row_errors=[],
        event_types=EVENT_TYPES, active="import",
    )


def build_weight_chart_svg(points):
    """Hand-rolled SVG line chart - no external JS/CDN dependency needed."""
    width, height = 600, 260
    pad_left, pad_right, pad_top, pad_bottom = 50, 20, 20, 40

    values = [p["value"] for p in points]
    min_v, max_v = min(values), max(values)
    if min_v == max_v:
        min_v -= 0.5
        max_v += 0.5
    else:
        span = max_v - min_v
        min_v -= span * 0.1
        max_v += span * 0.1

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(points)

    # x position is proportional to actual elapsed days since the first reading,
    # not just the point's position in the list - so a 3-day gap and a 10-day gap
    # between readings look visually different, matching reality.
    dates = [datetime.strptime(p["date"], "%Y-%m-%d") for p in points]
    first_date = dates[0]
    total_span_days = (dates[-1] - first_date).days or 1  # avoid divide-by-zero if all same day

    def x_for(i):
        if n == 1:
            return pad_left + plot_w / 2
        elapsed = (dates[i] - first_date).days
        return pad_left + (elapsed / total_span_days) * plot_w

    def y_for(v):
        return pad_top + plot_h - ((v - min_v) / (max_v - min_v)) * plot_h

    coords = [(x_for(i), y_for(p["value"])) for i, p in enumerate(points)]
    path_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in coords)

    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#4a90d9" />'
        for x, y in coords
    )
    labels = "".join(
        f'<text x="{x:.1f}" y="{height - pad_bottom + 18}" font-size="10" '
        f'text-anchor="middle" fill="#888">{p["date"][5:]}</text>'
        for (x, y), p in zip(coords, points)
    )
    value_labels = "".join(
        f'<text x="{x:.1f}" y="{y - 10:.1f}" font-size="11" font-weight="600" '
        f'text-anchor="middle" fill="#4a90d9">{p["value"]}</text>'
        for (x, y), p in zip(coords, points)
    )

    grid_y1 = y_for(min_v + (max_v - min_v) * 0.5)
    grid_line = f'<line x1="{pad_left}" y1="{grid_y1:.1f}" x2="{width-pad_right}" y2="{grid_y1:.1f}" stroke="#eee" stroke-width="1" />'

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">
        {grid_line}
        <path d="{path_d}" fill="none" stroke="#4a90d9" stroke-width="2.5" />
        {circles}
        {value_labels}
        {labels}
    </svg>'''


@app.route("/journal", methods=["GET", "POST"])
def journal_view():
    if request.method == "POST":
        entry_date = request.form.get("entry_date") or date.today().isoformat()
        event = (request.form.get("event") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        tags = parse_tags(request.form.get("tags"))

        conn = get_db()
        conn.execute(
            "INSERT INTO journal_entries (entry_date, event, notes, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            (entry_date, event, notes, format_tags(tags), datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("journal_view", entry_date=entry_date))

    anchor_str = request.args.get("entry_date") or date.today().isoformat()
    anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    days_since_friday = (anchor.weekday() - 4) % 7
    week_start = anchor - timedelta(days=days_since_friday)
    week_end = week_start + timedelta(days=6)
    week_number = (week_start - BABY_BIRTH_DATE).days // 7
    is_birth_week = (week_number <= 0)

    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM journal_entries WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date, id",
        (week_start.isoformat(), week_end.isoformat()),
    ).fetchall()
    conn.close()

    entries_by_date = {}
    all_tags_this_week = set()
    for r in rows:
        tag_list = parse_tags(r["tags"])
        all_tags_this_week.update(tag_list)
        entry_dict = {
            "id": r["id"], "entry_date": r["entry_date"], "event": r["event"],
            "notes": r["notes"], "tags": tag_list,
        }
        entries_by_date.setdefault(r["entry_date"], []).append(entry_dict)

    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        d_str = d.isoformat()
        day_number = (d - BABY_BIRTH_DATE).days
        days.append({
            "date": d_str,
            "label": d.strftime("%a %d %b"),
            "day_number": day_number,
            "entries": entries_by_date.get(d_str, []),
        })

    prev_week_anchor = (week_start - timedelta(days=7)).isoformat()
    next_week_anchor = (week_start + timedelta(days=7)).isoformat()
    is_current_week = (week_start <= date.today() <= week_end)
    today = date.today().isoformat()

    return render_template(
        "journal.html",
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        week_number=week_number,
        is_birth_week=is_birth_week,
        birth_week_anchor=BABY_BIRTH_DATE.isoformat(),
        days=days,
        anchor=anchor_str,
        prev_week_anchor=prev_week_anchor,
        next_week_anchor=next_week_anchor,
        is_current_week=is_current_week,
        today=today,
        all_tags_this_week=sorted(all_tags_this_week),
        active="journal",
    )


@app.route("/journal/edit/<int:entry_id>", methods=["GET", "POST"])
def journal_edit(entry_id):
    conn = get_db()

    if request.method == "POST":
        entry_date = request.form.get("entry_date")
        event = (request.form.get("event") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        conn.execute(
            "UPDATE journal_entries SET entry_date = ?, event = ?, notes = ? WHERE id = ?",
            (entry_date, event, notes, entry_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("journal_view", entry_date=entry_date))

    row = conn.execute("SELECT * FROM journal_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()
    if row is None:
        return redirect(url_for("journal_view"))

    return render_template("journal_edit.html", row=row, active="journal")


@app.route("/journal/delete/<int:entry_id>", methods=["POST"])
def journal_delete(entry_id):
    entry_date = request.form.get("entry_date") or date.today().isoformat()
    conn = get_db()
    conn.execute("DELETE FROM journal_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("journal_view", entry_date=entry_date))


@app.route("/journal/import", methods=["GET", "POST"])
def journal_import():
    message = None
    error = None
    row_errors = []

    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            error = "No file selected."
        else:
            try:
                content = file.stream.read().decode("utf-8")
                reader = csv.DictReader(io.StringIO(content))

                valid_rows = []
                for line_num, row in enumerate(reader, start=2):
                    raw_date = (row.get("Date") or "").strip()
                    event = (row.get("Type") or row.get("Event") or "").strip()
                    notes = (row.get("Notes") or row.get("Details") or "").strip()

                    normalized_date = parse_flexible_date(raw_date)
                    if not normalized_date:
                        row_errors.append(f"Row {line_num}: date '{raw_date}' isn't in a recognized format (try YYYY-MM-DD or DD/MM/YYYY)")
                        continue

                    if not event and not notes:
                        row_errors.append(f"Row {line_num}: needs at least a Type or Notes value")
                        continue

                    valid_rows.append((normalized_date, event, notes))

                if row_errors:
                    error = (
                        f"Import stopped — {len(row_errors)} row(s) had problems, and nothing was written "
                        f"to the database. Fix these and re-upload:"
                    )
                elif not valid_rows:
                    error = "No valid rows found in that file."
                else:
                    conn = get_db()
                    try:
                        now_iso = datetime.now().isoformat()
                        conn.executemany(
                            "INSERT INTO journal_entries (entry_date, event, notes, created_at) VALUES (?, ?, ?, ?)",
                            [(d, e, n, now_iso) for d, e, n in valid_rows],
                        )
                        conn.commit()
                        message = f"Imported {len(valid_rows)} journal entries successfully."
                    except Exception as e:
                        conn.rollback()
                        error = f"Import failed partway through and was rolled back — nothing was saved. Error: {e}"
                    finally:
                        conn.close()

            except Exception as e:
                error = f"Could not read that file: {e}"

    return render_template(
        "import.html",
        journal_message=message, journal_error=error, journal_row_errors=row_errors,
        entries_message=None, entries_error=None, entries_row_errors=[],
        weight_message=None, weight_error=None, weight_row_errors=[],
        event_types=EVENT_TYPES, active="import",
    )


@app.route("/journal/report")
def journal_report():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM journal_entries ORDER BY entry_date, id"
    ).fetchall()
    conn.close()

    by_date = {}
    for r in rows:
        by_date.setdefault(r["entry_date"], []).append(r)

    def week_start_for(d_str):
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        days_since_friday = (d.weekday() - 4) % 7
        return d - timedelta(days=days_since_friday)

    weeks = {}
    for d_str in by_date:
        ws = week_start_for(d_str).isoformat()
        weeks.setdefault(ws, []).append(d_str)

    week_summaries = []
    for ws in sorted(weeks.keys(), reverse=True):
        week_start_date = datetime.strptime(ws, "%Y-%m-%d").date()
        week_end_date = week_start_date + timedelta(days=6)
        week_number = (week_start_date - BABY_BIRTH_DATE).days // 7

        day_list = []
        for d_str in sorted(weeks[ws], reverse=True):
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
            day_number = (d - BABY_BIRTH_DATE).days
            day_list.append({"date": d_str, "day_number": day_number, "entries": by_date[d_str]})

        week_summaries.append({
            "week_number": week_number,
            "week_start": ws,
            "week_end": week_end_date.isoformat(),
            "days": day_list,
        })

    return render_template(
        "journal_report.html",
        week_summaries=week_summaries,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/growth")
def growth_view():
    today = date.today()
    age_days = (today - BABY_BIRTH_DATE).days
    weeks, days = divmod(age_days, 7)
    months = round(age_days / 30.44, 1)

    guidance = get_age_guidance(age_days)
    current_index = AGE_GUIDANCE.index(guidance)

    return render_template(
        "growth.html",
        age_days=age_days,
        age_weeks=weeks,
        age_extra_days=days,
        age_months=months,
        guidance=guidance,
        all_stages=AGE_GUIDANCE,
        current_index=current_index,
        birth_date=BABY_BIRTH_DATE.isoformat(),
        active="growth",
    )


@app.route("/photo-year")
def photo_year_view():
    conn = get_db()
    rows = conn.execute("SELECT * FROM monthly_photos").fetchall()
    conn.close()

    photos_by_month = {r["month_number"]: r["filename"] for r in rows}
    months = [{"number": i, "filename": photos_by_month.get(i)} for i in range(1, 13)]
    filled_count = len(photos_by_month)

    return render_template(
        "photo_year.html",
        months=months,
        filled_count=filled_count,
        themes=PHOTO_YEAR_THEMES,
        active="growth",
    )


@app.route("/photo-year/upload", methods=["POST"])
def photo_year_upload():
    try:
        month_number = int(request.form.get("month_number"))
    except (TypeError, ValueError):
        return redirect(url_for("photo_year_view"))

    if month_number < 1 or month_number > 12:
        return redirect(url_for("photo_year_view"))

    file = request.files.get("photo")
    if not file or file.filename == "":
        return redirect(url_for("photo_year_view"))

    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_extensions:
        return redirect(url_for("photo_year_view"))

    os.makedirs(PHOTO_UPLOAD_DIR, exist_ok=True)

    # Remove any previous photo for this month before saving the new one
    conn = get_db()
    existing = conn.execute(
        "SELECT filename FROM monthly_photos WHERE month_number = ?", (month_number,)
    ).fetchone()
    if existing:
        old_path = os.path.join(PHOTO_UPLOAD_DIR, existing["filename"])
        if os.path.exists(old_path):
            os.remove(old_path)

    safe_filename = f"month_{month_number:02d}{ext}"
    file.save(os.path.join(PHOTO_UPLOAD_DIR, safe_filename))

    conn.execute(
        """
        INSERT INTO monthly_photos (month_number, filename, uploaded_at) VALUES (?, ?, ?)
        ON CONFLICT(month_number) DO UPDATE SET filename = excluded.filename, uploaded_at = excluded.uploaded_at
        """,
        (month_number, safe_filename, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("photo_year_view"))


@app.route("/photo-year/delete/<int:month_number>", methods=["POST"])
def photo_year_delete(month_number):
    conn = get_db()
    row = conn.execute(
        "SELECT filename FROM monthly_photos WHERE month_number = ?", (month_number,)
    ).fetchone()
    if row:
        path = os.path.join(PHOTO_UPLOAD_DIR, row["filename"])
        if os.path.exists(path):
            os.remove(path)
        conn.execute("DELETE FROM monthly_photos WHERE month_number = ?", (month_number,))
        conn.commit()
    conn.close()
    return redirect(url_for("photo_year_view"))


def get_photo_year_font(size, bold=False):
    """Load a nice font if available (Linux paths for the Pi, Windows paths for local
    testing), otherwise fall back to PIL's built-in font at the requested size so this
    never crashes AND still scales correctly even without any font files installed."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeuib.ttf" if bold else "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    # Nothing found (rare) - PIL's built-in default font DOES accept a size argument
    # in modern Pillow, so this is a genuine fallback, not a silent "always tiny" trap
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def build_photo_collage_pdf(items_data, theme_key, title_text, label_prefix, upload_dir):
    """Compose a print-ready A3 (300 DPI) collage of 12 photos using Pillow.
    Shared by both the 'Ethan's First Year' (months) and 'Ethan's First 12 Weeks' pages."""
    theme = PHOTO_YEAR_THEMES.get(theme_key, PHOTO_YEAR_THEMES["soft_blue"])

    dpi = 300
    page_w = int(297 / 25.4 * dpi)   # A3 width in px at 300 DPI (portrait)
    page_h = int(420 / 25.4 * dpi)   # A3 height in px at 300 DPI

    canvas = Image.new("RGB", (page_w, page_h), theme["bg"])
    draw = ImageDraw.Draw(canvas)

    # Title - auto-shrinks to fit the page width so it can never overflow,
    # regardless of how long the title text is
    title_font = get_photo_year_font(280, bold=True)
    max_title_w = page_w - 300  # keep clear of both edges
    while True:
        bbox = draw.textbbox((0, 0), title_text, font=title_font)
        title_w = bbox[2] - bbox[0]
        if title_w <= max_title_w or title_font.size <= 60:
            break
        title_font = get_photo_year_font(title_font.size - 10, bold=True)
    draw.text(((page_w - title_w) / 2, 90), title_text, font=title_font, fill=theme["text"])

    # Grid: 3 columns x 4 rows
    cols, rows = 3, 4
    margin = 150
    top_offset = 480
    label_height = 110
    grid_w = page_w - margin * 2
    grid_h = page_h - top_offset - margin
    cell_w = grid_w // cols
    cell_h = (grid_h - label_height * rows) // rows

    label_font = get_photo_year_font(130, bold=True)

    for i, item in enumerate(items_data):
        col = i % cols
        row = i // cols
        cell_x = margin + col * cell_w
        cell_y = top_offset + row * (cell_h + label_height)

        photo_area_w = cell_w - 40
        photo_area_h = cell_h - 40
        photo_x = cell_x + 20
        photo_y = cell_y + 20

        # Draw a soft card background behind each photo slot
        draw.rectangle(
            [cell_x + 10, cell_y + 10, cell_x + cell_w - 10, cell_y + cell_h - 10],
            fill=(255, 255, 255), outline=theme["accent"], width=4,
        )

        if item["filename"]:
            photo_path = os.path.join(upload_dir, item["filename"])
            if os.path.exists(photo_path):
                img = Image.open(photo_path).convert("RGB")
                # Center-crop ("cover" style) so the photo fills the cell without stretching
                img_ratio = img.width / img.height
                target_ratio = photo_area_w / photo_area_h
                if img_ratio > target_ratio:
                    new_height = img.height
                    new_width = int(new_height * target_ratio)
                    left = (img.width - new_width) // 2
                    img = img.crop((left, 0, left + new_width, new_height))
                else:
                    new_width = img.width
                    new_height = int(new_width / target_ratio)
                    top = (img.height - new_height) // 2
                    img = img.crop((0, top, new_width, top + new_height))
                img = img.resize((photo_area_w, photo_area_h), Image.LANCZOS)
                canvas.paste(img, (photo_x, photo_y))
        else:
            # Placeholder for an item with no photo uploaded yet
            draw.rectangle(
                [photo_x, photo_y, photo_x + photo_area_w, photo_y + photo_area_h],
                fill=(245, 245, 245),
            )
            placeholder_font = get_photo_year_font(70)
            ph_text = "No photo yet"
            pbbox = draw.textbbox((0, 0), ph_text, font=placeholder_font)
            pw = pbbox[2] - pbbox[0]
            draw.text(
                (photo_x + (photo_area_w - pw) / 2, photo_y + photo_area_h / 2 - 25),
                ph_text, font=placeholder_font, fill=(180, 180, 180),
            )

        # Label below the photo cell (e.g. "Month 3" or "Week 7")
        label_text = f"{label_prefix} {item['number']}"
        lbbox = draw.textbbox((0, 0), label_text, font=label_font)
        lw = lbbox[2] - lbbox[0]
        draw.text(
            (cell_x + (cell_w - lw) / 2, cell_y + cell_h - 15),
            label_text, font=label_font, fill=theme["text"],
        )

    buffer = io.BytesIO()
    canvas.save(buffer, "PDF", resolution=float(dpi))
    buffer.seek(0)
    return buffer


@app.route("/photo-year/generate")
def photo_year_generate():
    theme_key = request.args.get("theme", "soft_blue")

    conn = get_db()
    rows = conn.execute("SELECT * FROM monthly_photos").fetchall()
    conn.close()

    photos_by_month = {r["month_number"]: r["filename"] for r in rows}
    months_data = [{"number": i, "filename": photos_by_month.get(i)} for i in range(1, 13)]

    pdf_buffer = build_photo_collage_pdf(months_data, theme_key, "Ethan's First Year", "Month", PHOTO_UPLOAD_DIR)

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="ethans-first-year.pdf",
    )


@app.route("/photo-weeks")
def photo_weeks_view():
    conn = get_db()
    rows = conn.execute("SELECT * FROM weekly_photos").fetchall()
    conn.close()

    photos_by_week = {r["week_number"]: r["filename"] for r in rows}
    weeks = [{"number": i, "filename": photos_by_week.get(i)} for i in range(1, 13)]
    filled_count = len(photos_by_week)

    return render_template(
        "photo_weeks.html",
        weeks=weeks,
        filled_count=filled_count,
        themes=PHOTO_YEAR_THEMES,
        active="growth",
    )


@app.route("/photo-weeks/upload", methods=["POST"])
def photo_weeks_upload():
    try:
        week_number = int(request.form.get("week_number"))
    except (TypeError, ValueError):
        return redirect(url_for("photo_weeks_view"))

    if week_number < 1 or week_number > 12:
        return redirect(url_for("photo_weeks_view"))

    file = request.files.get("photo")
    if not file or file.filename == "":
        return redirect(url_for("photo_weeks_view"))

    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_extensions:
        return redirect(url_for("photo_weeks_view"))

    weeks_upload_dir = os.path.join("static", "uploads", "photo_weeks")
    os.makedirs(weeks_upload_dir, exist_ok=True)

    conn = get_db()
    existing = conn.execute(
        "SELECT filename FROM weekly_photos WHERE week_number = ?", (week_number,)
    ).fetchone()
    if existing:
        old_path = os.path.join(weeks_upload_dir, existing["filename"])
        if os.path.exists(old_path):
            os.remove(old_path)

    safe_filename = f"week_{week_number:02d}{ext}"
    file.save(os.path.join(weeks_upload_dir, safe_filename))

    conn.execute(
        """
        INSERT INTO weekly_photos (week_number, filename, uploaded_at) VALUES (?, ?, ?)
        ON CONFLICT(week_number) DO UPDATE SET filename = excluded.filename, uploaded_at = excluded.uploaded_at
        """,
        (week_number, safe_filename, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

    return redirect(url_for("photo_weeks_view"))


@app.route("/photo-weeks/delete/<int:week_number>", methods=["POST"])
def photo_weeks_delete(week_number):
    weeks_upload_dir = os.path.join("static", "uploads", "photo_weeks")
    conn = get_db()
    row = conn.execute(
        "SELECT filename FROM weekly_photos WHERE week_number = ?", (week_number,)
    ).fetchone()
    if row:
        path = os.path.join(weeks_upload_dir, row["filename"])
        if os.path.exists(path):
            os.remove(path)
        conn.execute("DELETE FROM weekly_photos WHERE week_number = ?", (week_number,))
        conn.commit()
    conn.close()
    return redirect(url_for("photo_weeks_view"))


@app.route("/photo-weeks/generate")
def photo_weeks_generate():
    theme_key = request.args.get("theme", "soft_blue")

    conn = get_db()
    rows = conn.execute("SELECT * FROM weekly_photos").fetchall()
    conn.close()

    photos_by_week = {r["week_number"]: r["filename"] for r in rows}
    weeks_data = [{"number": i, "filename": photos_by_week.get(i)} for i in range(1, 13)]

    weeks_upload_dir = os.path.join("static", "uploads", "photo_weeks")
    pdf_buffer = build_photo_collage_pdf(weeks_data, theme_key, "Ethan's First 12 Weeks", "Week", weeks_upload_dir)

    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="ethans-first-12-weeks.pdf",
    )


@app.route("/report")
def report_view():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries ORDER BY entry_date, entry_time"
    ).fetchall()
    conn.close()

    if not rows:
        return render_template(
            "report.html",
            week_summaries=[],
            event_types=EVENT_TYPES,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    # group entries by date first
    by_date = {}
    for r in rows:
        by_date.setdefault(r["entry_date"], []).append(r)

    # then group dates into Fri-Thu weeks, matching the Weekly View's week boundaries
    def week_start_for(d_str):
        d = datetime.strptime(d_str, "%Y-%m-%d").date()
        days_since_friday = (d.weekday() - 4) % 7
        return d - timedelta(days=days_since_friday)

    weeks = {}  # week_start_iso -> list of dates
    for d_str in by_date:
        ws = week_start_for(d_str).isoformat()
        weeks.setdefault(ws, []).append(d_str)

    week_summaries = []
    for ws in sorted(weeks.keys(), reverse=True):  # latest week first
        week_start_date = datetime.strptime(ws, "%Y-%m-%d").date()
        week_end_date = week_start_date + timedelta(days=6)
        week_number = (week_start_date - BABY_BIRTH_DATE).days // 7

        day_summaries = []
        for d_str in sorted(weeks[ws], reverse=True):  # latest day first within the week
            day_rows = by_date[d_str]
            day_summaries.append({"date": d_str, "totals": compute_totals(day_rows)})

        week_summaries.append({
            "week_number": week_number,
            "week_start": ws,
            "week_end": week_end_date.isoformat(),
            "days": day_summaries,
        })

    return render_template(
        "report.html",
        week_summaries=week_summaries,
        event_types=EVENT_TYPES,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    message = None
    error = None
    row_errors = []

    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            error = "No file selected."
        else:
            try:
                content = file.stream.read().decode("utf-8")
                reader = csv.DictReader(io.StringIO(content))
                label_to_key = {meta["label"]: key for key, meta in EVENT_TYPES.items()}

                # --- Phase 1: validate every row, write nothing yet ---
                valid_rows = []
                for line_num, row in enumerate(reader, start=2):  # start=2: row 1 is the header
                    raw_date = (row.get("Date") or "").strip()
                    raw_time = (row.get("Time") or "").strip()
                    raw_type = (row.get("Type") or "").strip()
                    raw_value = (row.get("Value") or "").strip()

                    # Date: try several common formats, normalize to YYYY-MM-DD
                    normalized_date = parse_flexible_date(raw_date)
                    if not normalized_date:
                        row_errors.append(f"Row {line_num}: date '{raw_date}' isn't in a recognized format (try YYYY-MM-DD or DD/MM/YYYY)")
                        continue
                    raw_date = normalized_date

                    # Time must be HH:MM (24-hour) - single-digit hours like '5:00' are fine
                    try:
                        parsed_time = datetime.strptime(raw_time, "%H:%M")
                        raw_time = parsed_time.strftime("%H:%M")
                    except ValueError:
                        row_errors.append(f"Row {line_num}: time '{raw_time}' isn't in HH:MM format")
                        continue

                    etype = label_to_key.get(raw_type) or (raw_type.lower() if raw_type.lower() in EVENT_TYPES else None)
                    if not etype:
                        row_errors.append(f"Row {line_num}: type '{raw_type}' doesn't match any known entry type")
                        continue

                    if etype in ("poo", "pee"):
                        # Value here is treated as a COUNT of events at this date/time,
                        # e.g. "Poo,3" logs three separate poo entries, not one entry valued at 3
                        try:
                            count = max(int(float(raw_value)), 1) if raw_value else 1
                        except (TypeError, ValueError):
                            row_errors.append(f"Row {line_num}: poo/pee count '{raw_value}' isn't a valid number")
                            continue
                        for _ in range(count):
                            valid_rows.append((raw_date, raw_time, etype, 1))
                        continue
                    else:
                        try:
                            value = float(raw_value)
                        except (TypeError, ValueError):
                            row_errors.append(f"Row {line_num}: value '{raw_value}' isn't a valid number")
                            continue

                    valid_rows.append((raw_date, raw_time, etype, value))

                # --- Phase 2: only write to the database if the whole file was clean ---
                if row_errors:
                    error = (
                        f"Import stopped — {len(row_errors)} row(s) had problems, and nothing was written "
                        f"to the database. Fix these and re-upload:"
                    )
                elif not valid_rows:
                    error = "No valid rows found in that file."
                else:
                    # Safety net: snapshot the database before writing, just in case
                    import shutil, os
                    if os.path.exists(DB_PATH):
                        os.makedirs("backups", exist_ok=True)
                        snapshot_name = f"backups/tracker_pre_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                        shutil.copy2(DB_PATH, snapshot_name)

                    conn = get_db()
                    try:
                        now_iso = datetime.now().isoformat()
                        conn.executemany(
                            "INSERT INTO entries (entry_date, entry_time, type, value, created_at) VALUES (?, ?, ?, ?, ?)",
                            [(d, t, ty, v, now_iso) for d, t, ty, v in valid_rows],
                        )
                        conn.commit()
                        message = f"Imported {len(valid_rows)} entries successfully."
                    except Exception as e:
                        conn.rollback()
                        error = f"Import failed partway through and was rolled back — nothing was saved. Error: {e}"
                    finally:
                        conn.close()

            except Exception as e:
                error = f"Could not read that file: {e}"

    return render_template(
        "import.html",
        entries_message=message, entries_error=error, entries_row_errors=row_errors,
        weight_message=None, weight_error=None, weight_row_errors=[],
        journal_message=None, journal_error=None, journal_row_errors=[],
        event_types=EVENT_TYPES, active="import",
    )


@app.route("/edit/<int:entry_id>", methods=["GET", "POST"])
def edit_entry(entry_id):
    conn = get_db()

    if request.method == "POST":
        entry_date = request.form.get("entry_date")
        entry_time = request.form.get("entry_time")
        etype = request.form.get("type")
        value = request.form.get("value")

        if etype in ("poo", "pee"):
            value = 1
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None

        conn.execute(
            "UPDATE entries SET entry_date = ?, entry_time = ?, type = ?, value = ? WHERE id = ?",
            (entry_date, entry_time, etype, value, entry_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("day_view", entry_date=entry_date))

    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()

    if row is None:
        return redirect(url_for("day_view"))

    return render_template("edit.html", row=row, event_types=EVENT_TYPES)


@app.route("/delete/<int:entry_id>", methods=["POST"])
def delete_entry(entry_id):
    entry_date = request.form.get("entry_date") or date.today().isoformat()

    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("day_view", entry_date=entry_date))


@app.route("/export.csv")
def export_csv():
    """Export all entries (or a single date if provided) as a downloadable CSV."""
    entry_date = request.args.get("entry_date")

    conn = get_db()
    if entry_date:
        rows = conn.execute(
            "SELECT * FROM entries WHERE entry_date = ? ORDER BY entry_date, entry_time",
            (entry_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entries ORDER BY entry_date, entry_time"
        ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Time", "Type", "Value"])
    for r in rows:
        writer.writerow([r["entry_date"], r["entry_time"], EVENT_TYPES[r["type"]]["label"], r["value"]])

    filename = f"baby-tracker-{entry_date or 'all'}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/journal/export.csv")
def journal_export_csv():
    """Export all journal entries as a downloadable CSV."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM journal_entries ORDER BY entry_date, id"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Type", "Notes"])
    for r in rows:
        writer.writerow([r["entry_date"], r["event"] or "", r["notes"] or ""])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=baby-ethan-journal.csv"},
    )


if __name__ == "__main__":
    init_db()
    # host="0.0.0.0" makes it reachable from other devices on the home network
    app.run(host="0.0.0.0", port=5000, debug=True)
