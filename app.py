import sqlite3
import csv
import io
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, Response

app = Flask(__name__)
DB_PATH = "tracker.db"

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
        last_fed=last_fed,
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

        conn = get_db()
        conn.execute(
            "INSERT INTO journal_entries (entry_date, event, notes, created_at) VALUES (?, ?, ?, ?)",
            (entry_date, event, notes, datetime.now().isoformat()),
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
    for r in rows:
        entries_by_date.setdefault(r["entry_date"], []).append(r)

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
        days=days,
        anchor=anchor_str,
        prev_week_anchor=prev_week_anchor,
        next_week_anchor=next_week_anchor,
        is_current_week=is_current_week,
        today=today,
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
