import sqlite3
import csv
import glob
import io
import os
import secrets
from functools import wraps
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, Response, send_file, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# Needed to sign the login session cookie. Reads SECRET_KEY from the environment so the
# real value never lives in source code (and never ends up in git, which matters since
# this repo is public). Set SECRET_KEY on whatever host runs this app for real - e.g. in
# PythonAnywhere's web app config - and keep it the same across restarts, otherwise every
# restart invalidates everyone's login session.
# For everyday local testing on your own machine, no need to set anything: falls back to
# a freshly-generated random key each time you run the app, which is fine for local use
# (it just means logging back in again if you restart the server, nothing more).
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Personal tracking data (entries, journal, weight, settings) now lives in its own
# per-user file under DATA_DIR - never in one shared database. AUTH_DB_PATH holds only
# the users table (who can log in), completely separate from anyone's tracking data.
AUTH_DB_PATH = "auth.db"
DATA_DIR = "data"

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

# Baby's name and birth date are configurable via the /settings page (see get_settings()
# below) instead of being hardcoded here.

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
    {
        "from_days": 270,
        "label": "9–12 months",
        "nappy_size": "Size 4, ~9–14kg",
        "feed_volume": "3 meals + 1–2 snacks/day, plus ~500–600ml/day breast or formula milk",
        "sleep": "11–14 hours total, usually 1–2 naps",
        "milestones": "Pulls to stand, cruises along furniture, may take first steps, says first words, uses a pincer grasp",
    },
    {
        "from_days": 365,
        "label": "1–2 years",
        "nappy_size": "Size 4–5, ~10–16kg",
        "feed_volume": "3 meals + 2 snacks/day, transitioning to whole milk (~350–500ml/day)",
        "sleep": "11–14 hours total, usually dropping to 1 nap",
        "milestones": "Walks independently, builds a small vocabulary then short phrases, climbs, points to show interest",
    },
    {
        "from_days": 730,
        "label": "2–3 years",
        "nappy_size": "Size 5–6 or pull-ups - many start toilet training around now",
        "feed_volume": "3 meals + 1–2 snacks/day, mostly regular table food alongside the family",
        "sleep": "10–13 hours total, usually 1 nap or none by age 3",
        "milestones": "Runs and jumps, speaks in 2–3 word sentences, follows simple instructions, shows growing independence",
    },
    {
        "from_days": 1095,
        "label": "3–4 years",
        "nappy_size": "Most are toilet trained by now (day and/or night) - overnight pull-ups only if still needed",
        "feed_volume": "3 meals + 1–2 snacks/day, similar portions to family meals",
        "sleep": "10–13 hours total, daytime naps often dropped, may still need quiet rest time",
        "milestones": "Pedals a tricycle, speaks in full sentences, plays cooperatively, dresses with some help",
    },
    {
        "from_days": 1460,
        "label": "4–5 years",
        "nappy_size": "Typically toilet trained day and night",
        "feed_volume": "3 meals + 1–2 snacks/day, similar to family meals",
        "sleep": "10–13 hours total, usually no daytime nap",
        "milestones": "Hops and balances, tells stories, counts, draws recognizable shapes, increasingly independent",
    },
    {
        "from_days": 1825,
        "label": "5+ years",
        "nappy_size": "Not typically needed by this age",
        "feed_volume": "Regular family meals and snacks",
        "sleep": "9–11 hours total, no daytime nap",
        "milestones": "Growing independence and skills vary widely child to child - the Journal tab is probably more useful than this page from here on!",
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


def calendar_years_months(birth_date, today):
    """Whole calendar years/months between birth_date and today (e.g. someone born
    15 March who it is now 20 June two years later is "2 years, 3 months", not a
    days-based approximation) - the plain-language way people actually state a
    toddler's age, as opposed to weeks/days which stops being natural well before
    a child turns 2."""
    years = today.year - birth_date.year
    months = today.month - birth_date.month
    if today.day < birth_date.day:
        months -= 1
    if months < 0:
        years -= 1
        months += 12
    return years, months


def format_age_headline(age_days, birth_date, today):
    """The big "X old" headline at the top of the Growth page. Weeks/days reads
    naturally for infants but turns absurd well before a child turns 2 (nobody
    says "104 weeks old") - so past that point this switches to whole calendar
    years/months instead, e.g. "3 years, 0 months old"."""
    if age_days < 730:
        weeks, days = divmod(age_days, 7)
        week_word = "week" if weeks == 1 else "weeks"
        day_word = "day" if days == 1 else "days"
        return f"{weeks} {week_word}, {days} {day_word} old"
    years, months = calendar_years_months(birth_date, today)
    year_word = "year" if years == 1 else "years"
    month_word = "month" if months == 1 else "months"
    return f"{years} {year_word}, {months} {month_word} old"


def format_age_subline(age_days):
    """Secondary line under the headline - total days plus an approximate
    months/years figure, whichever reads more naturally at that age."""
    if age_days < 730:
        months = round(age_days / 30.44, 1)
        return f"{age_days} days total · ~{months} months"
    years = round(age_days / 365.25, 1)
    return f"{age_days} days total · ~{years} years"


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


def get_auth_db():
    """Connects to the shared auth database - this ever only holds the users table.
    Nobody's tracking data lives here."""
    conn = sqlite3.connect(AUTH_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    """Connects to the CURRENTLY LOGGED-IN user's household tracker database. Keyed by
    household_id rather than the user's own id, so two separate logins that share a
    household_id (e.g. both parents) see the exact same entries/journal/weight/settings -
    every family's own file still lives under DATA_DIR, never in one shared database.
    See ensure_user_db_ready() below, which creates this file's tables automatically
    before each request."""
    user = current_user()
    if user is None:
        raise RuntimeError("get_db() called with nobody logged in")
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(os.path.join(DATA_DIR, f"tracker_{user['household_id']}.db"))
    conn.row_factory = sqlite3.Row
    return conn


# Seed defaults - used only until someone saves real values on the /settings page,
# so a freshly-cloned copy of the app behaves sensibly out of the box.
DEFAULT_BABY_NAME = "Baby"

# Ceiling on how many logins can share one household's tracker data (see the
# add_household_login branch of settings_view) - a sanity limit, not shown to users.
MAX_HOUSEHOLD_MEMBERS = 5


def default_birth_date():
    """Today's date, computed fresh on every call (never a fixed date baked into the
    source) - used as the birth date shown/assumed until someone saves a real one on
    the /settings page. Being today means a brand new account's birth-date field opens
    on today rather than some arbitrary placeholder date, ready to edit."""
    return date.today()


def get_settings():
    """Current baby_name (str) and birth_date (date object), read from the logged-in
    user's own settings table. Falls back to the defaults above if nobody's logged
    in (e.g. rendering the login/signup pages) or no settings row has been saved yet."""
    if current_user() is None:
        return {"baby_name": DEFAULT_BABY_NAME, "birth_date": default_birth_date()}
    conn = get_db()
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()
    if row is None:
        return {"baby_name": DEFAULT_BABY_NAME, "birth_date": default_birth_date()}
    return {
        "baby_name": row["baby_name"],
        "birth_date": datetime.strptime(row["birth_date"], "%Y-%m-%d").date(),
    }


def save_settings(baby_name, birth_date_str):
    """birth_date_str should be an ISO 'YYYY-MM-DD' string, e.g. from an
    <input type="date"> form field."""
    conn = get_db()
    conn.execute(
        """
        INSERT INTO settings (id, baby_name, birth_date) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET baby_name = excluded.baby_name, birth_date = excluded.birth_date
        """,
        (baby_name, birth_date_str),
    )
    conn.commit()
    conn.close()


def get_birth_date():
    """Convenience accessor for the common case where only the date is needed."""
    return get_settings()["birth_date"]


def current_user():
    """The logged-in user's row (as a dict-like sqlite3.Row), or None if nobody's
    logged in / the session refers to a user that no longer exists. Always reads
    from the shared auth database, never from a per-user tracker file."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_auth_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def login_required(view):
    """Route decorator - redirects to /login if nobody's logged in, then sends them
    back to the page they wanted once they are. Applied to every route except
    signup/login/logout."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login_view", next=request.path))
        return view(*args, **kwargs)
    return wrapped_view


@app.context_processor
def inject_settings():
    """Makes baby_name, app_title, and current_user available in every template
    automatically, so templates don't each need them passed in explicitly via
    render_template().

    app_title is the "Baby X's Steps" header/title text, computed once here so
    every page shows the same thing without repeating this logic 18 times. Until
    a real name is saved on /settings, baby_name is still the DEFAULT_BABY_NAME
    placeholder ("Baby") - showing that as "Baby Baby's Steps" reads like a typo,
    so that specific case gets a friendly generic title instead. Once a name is
    saved (e.g. "Ethan"), it becomes "Baby Ethan's Steps" as before.
    """
    baby_name = get_settings()["baby_name"]
    if baby_name == DEFAULT_BABY_NAME:
        app_title = "Your Baby Steps"
    else:
        app_title = f"Baby {baby_name}'s Steps"
    return {"baby_name": baby_name, "app_title": app_title, "current_user": current_user()}


def init_user_tables(conn):
    """Creates every personal-tracking table in a per-user database connection, if
    they don't already exist yet. Runs automatically before each authenticated
    request (see ensure_user_db_ready below), so a brand new account's file is
    ready to use from its very first request - nothing extra to set up on signup."""
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
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            baby_name TEXT NOT NULL,
            birth_date TEXT NOT NULL
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


@app.before_request
def ensure_user_db_ready():
    """Makes sure the logged-in user's own tracker file (and its tables) exist
    before any route touches it. Runs on every request, but CREATE TABLE IF NOT
    EXISTS is cheap, so for a returning user this is effectively a no-op."""
    user = current_user()
    if user is not None:
        conn = get_db()
        init_user_tables(conn)
        conn.close()


def init_db():
    """Startup-only: creates the shared auth database (just the users table). Each
    household's own personal tracker database is created lazily on their first request
    instead - see ensure_user_db_ready() above."""
    conn = get_auth_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            household_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # One-time migration for databases created before household logins existed: add the
    # column if it's missing, then default every existing row to its OWN id - i.e. a
    # solo household of one, which matches their existing tracker_<id>.db file exactly,
    # so nobody's data moves or needs touching by hand. Safe to run on every startup.
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "household_id" not in existing_columns:
        conn.execute("ALTER TABLE users ADD COLUMN household_id INTEGER")
        conn.commit()
    conn.execute("UPDATE users SET household_id = id WHERE household_id IS NULL")
    conn.commit()
    conn.close()


@app.route("/sw.js")
def service_worker():
    """Served from the site ROOT (not /static/sw.js) on purpose - a service worker can
    only control pages under its own path, so serving it from /static/ would limit it to
    controlling other static files instead of the actual app pages. No login required:
    the browser fetches this before anyone's necessarily signed in."""
    response = send_from_directory("static", "sw.js")
    response.headers["Content-Type"] = "application/javascript"
    return response


@app.route("/", methods=["GET", "POST"])
@login_required
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
@login_required
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
@login_required
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
@login_required
def week_view():
    # anchor date - defaults to today. Each 7-day week starts on baby's birth weekday,
    # so "Week 0" always begins the day baby was born (whatever day of the week that was)
    anchor_str = request.args.get("entry_date") or date.today().isoformat()
    anchor = datetime.strptime(anchor_str, "%Y-%m-%d").date()
    days_since_anchor_weekday = (anchor.weekday() - get_birth_date().weekday()) % 7
    week_start = anchor - timedelta(days=days_since_anchor_weekday)
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

    # Baby's birth date is itself the start of "Week 0"
    week_number = (week_start - get_birth_date()).days // 7
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
        birth_week_anchor=get_birth_date().isoformat(),
        last_fed=last_fed,
        today=date.today().isoformat(),
        active="week",
    )


@app.route("/weight")
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def weight_delete(entry_id):
    conn = get_db()
    conn.execute("DELETE FROM weight_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("weight_view"))


@app.route("/weight/import", methods=["POST"])
@login_required
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
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#3c98a4" />'
        for x, y in coords
    )
    labels = "".join(
        f'<text x="{x:.1f}" y="{height - pad_bottom + 18}" font-size="10" '
        f'text-anchor="middle" fill="#888">{p["date"][5:]}</text>'
        for (x, y), p in zip(coords, points)
    )
    value_labels = "".join(
        f'<text x="{x:.1f}" y="{y - 10:.1f}" font-size="11" font-weight="600" '
        f'text-anchor="middle" fill="#3c98a4">{p["value"]}</text>'
        for (x, y), p in zip(coords, points)
    )

    grid_y1 = y_for(min_v + (max_v - min_v) * 0.5)
    grid_line = f'<line x1="{pad_left}" y1="{grid_y1:.1f}" x2="{width-pad_right}" y2="{grid_y1:.1f}" stroke="#eee" stroke-width="1" />'

    return f'''<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">
        {grid_line}
        <path d="{path_d}" fill="none" stroke="#3c98a4" stroke-width="2.5" />
        {circles}
        {value_labels}
        {labels}
    </svg>'''


@app.route("/journal", methods=["GET", "POST"])
@login_required
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
    days_since_anchor_weekday = (anchor.weekday() - get_birth_date().weekday()) % 7
    week_start = anchor - timedelta(days=days_since_anchor_weekday)
    week_end = week_start + timedelta(days=6)
    week_number = (week_start - get_birth_date()).days // 7
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
        day_number = (d - get_birth_date()).days
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
        birth_week_anchor=get_birth_date().isoformat(),
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
@login_required
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
@login_required
def journal_delete(entry_id):
    entry_date = request.form.get("entry_date") or date.today().isoformat()
    conn = get_db()
    conn.execute("DELETE FROM journal_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("journal_view", entry_date=entry_date))


@app.route("/journal/import", methods=["GET", "POST"])
@login_required
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
@login_required
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
        days_since_anchor_weekday = (d.weekday() - get_birth_date().weekday()) % 7
        return d - timedelta(days=days_since_anchor_weekday)

    weeks = {}
    for d_str in by_date:
        ws = week_start_for(d_str).isoformat()
        weeks.setdefault(ws, []).append(d_str)

    week_summaries = []
    for ws in sorted(weeks.keys(), reverse=True):
        week_start_date = datetime.strptime(ws, "%Y-%m-%d").date()
        week_end_date = week_start_date + timedelta(days=6)
        week_number = (week_start_date - get_birth_date()).days // 7

        day_list = []
        for d_str in sorted(weeks[ws], reverse=True):
            d = datetime.strptime(d_str, "%Y-%m-%d").date()
            day_number = (d - get_birth_date()).days
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
@login_required
def growth_view():
    today = date.today()
    birth_date = get_birth_date()
    age_days = (today - birth_date).days

    guidance = get_age_guidance(age_days)
    current_index = AGE_GUIDANCE.index(guidance)

    return render_template(
        "growth.html",
        age_days=age_days,
        age_headline=format_age_headline(age_days, birth_date, today),
        age_subline=format_age_subline(age_days),
        guidance=guidance,
        all_stages=AGE_GUIDANCE,
        current_index=current_index,
        birth_date=birth_date.isoformat(),
        active="growth",
    )


@app.route("/report")
@login_required
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
        days_since_anchor_weekday = (d.weekday() - get_birth_date().weekday()) % 7
        return d - timedelta(days=days_since_anchor_weekday)

    weeks = {}  # week_start_iso -> list of dates
    for d_str in by_date:
        ws = week_start_for(d_str).isoformat()
        weeks.setdefault(ws, []).append(d_str)

    week_summaries = []
    for ws in sorted(weeks.keys(), reverse=True):  # latest week first
        week_start_date = datetime.strptime(ws, "%Y-%m-%d").date()
        week_end_date = week_start_date + timedelta(days=6)
        week_number = (week_start_date - get_birth_date()).days // 7

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
@login_required
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
                    # Safety net: snapshot this household's database before writing, just in case
                    import shutil
                    household_id = current_user()["household_id"]
                    user_db_path = os.path.join(DATA_DIR, f"tracker_{household_id}.db")
                    if os.path.exists(user_db_path):
                        os.makedirs("backups", exist_ok=True)
                        snapshot_name = f"backups/tracker_{household_id}_pre_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
                        shutil.copy2(user_db_path, snapshot_name)

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
@login_required
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
@login_required
def delete_entry(entry_id):
    entry_date = request.form.get("entry_date") or date.today().isoformat()

    conn = get_db()
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("day_view", entry_date=entry_date))


@app.route("/export.csv")
@login_required
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
@login_required
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
        headers={"Content-Disposition": "attachment; filename=baby-tracker-journal.csv"},
    )


@app.route("/signup", methods=["GET", "POST"])
def signup_view():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("name") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not email or not password:
            error = "Email and password are both required."
        elif password != confirm_password:
            error = "Those two passwords don't match."
        elif len(password) < 8:
            error = "Password needs to be at least 8 characters."
        else:
            conn = get_auth_db()
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                conn.close()
                error = "An account with that email already exists - try logging in instead."
            else:
                conn.execute(
                    "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (email, name or email.split("@")[0], generate_password_hash(password), datetime.now().isoformat()),
                )
                conn.commit()
                new_user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
                # A brand new signup starts its own solo household - defaults to their own
                # id, same as the one-time migration in init_db() does for older accounts.
                conn.execute("UPDATE users SET household_id = ? WHERE id = ?", (new_user["id"], new_user["id"]))
                conn.commit()
                conn.close()
                session["user_id"] = new_user["id"]
                return redirect(request.values.get("next") or url_for("home"))

    return render_template("signup.html", error=error, next=request.values.get("next", ""))


@app.route("/login", methods=["GET", "POST"])
def login_view():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        conn = get_auth_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            return redirect(request.values.get("next") or url_for("home"))
        error = "Incorrect email or password."

    # Shown once, right after settings_view deletes an account and redirects here -
    # not stored anywhere, just a one-off query param on this one redirect.
    message = "Your account and all its data have been permanently deleted." if request.args.get("deleted") else None

    return render_template("login.html", error=error, message=message, next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout_view():
    session.pop("user_id", None)
    return redirect(url_for("login_view"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    saved = False
    error = None
    password_saved = False
    password_error = None
    delete_error = None
    household_saved = False
    household_error = None

    # Bounds for the birth date: can't be in the future, and (to catch fat-finger
    # typos like a birth year instead of a birth date) can't be more than 10 years
    # ago - comfortably past the oldest AGE_GUIDANCE stage ("5+ years") so a real
    # toddler/preschooler's birth date is never rejected by this sanity check.
    earliest_allowed = date.today() - timedelta(days=10 * 365)
    latest_allowed = date.today()

    if request.method == "POST" and request.form.get("form_name") == "change_password":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_new_password = request.form.get("confirm_new_password") or ""

        user = current_user()
        if not check_password_hash(user["password_hash"], current_password):
            password_error = "That's not your current password."
        elif new_password != confirm_new_password:
            password_error = "Those two new passwords don't match."
        elif len(new_password) < 8:
            password_error = "New password needs to be at least 8 characters."
        else:
            conn = get_auth_db()
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user["id"]),
            )
            conn.commit()
            conn.close()
            password_saved = True

    elif request.method == "POST" and request.form.get("form_name") == "add_household_login":
        new_email = (request.form.get("new_email") or "").strip().lower()
        new_name = (request.form.get("new_name") or "").strip()
        new_password = request.form.get("new_password") or ""
        new_confirm_password = request.form.get("new_confirm_password") or ""

        if not new_email or not new_password:
            household_error = "Email and password are both required."
        elif new_password != new_confirm_password:
            household_error = "Those two passwords don't match."
        elif len(new_password) < 8:
            household_error = "Password needs to be at least 8 characters."
        else:
            conn = get_auth_db()
            this_household_id = current_user()["household_id"]
            member_count = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE household_id = ?", (this_household_id,)
            ).fetchone()["n"]
            existing = conn.execute("SELECT id FROM users WHERE email = ?", (new_email,)).fetchone()
            if member_count >= MAX_HOUSEHOLD_MEMBERS:
                conn.close()
                household_error = "This household has reached its maximum number of logins."
            elif existing:
                conn.close()
                household_error = "An account with that email already exists."
            else:
                user = current_user()
                # Shares the CURRENT user's household_id rather than getting a fresh one of
                # their own - that's what makes this a second login onto the SAME tracker
                # data instead of a normal standalone /signup account.
                conn.execute(
                    "INSERT INTO users (email, name, password_hash, household_id, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        new_email,
                        new_name or new_email.split("@")[0],
                        generate_password_hash(new_password),
                        user["household_id"],
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
                conn.close()
                household_saved = True

    elif request.method == "POST" and request.form.get("form_name") == "delete_account":
        current_password = request.form.get("current_password") or ""

        user = current_user()
        if not check_password_hash(user["password_hash"], current_password):
            delete_error = "That's not your current password - nothing was deleted."
        else:
            user_id = user["id"]
            household_id = user["household_id"]

            conn = get_auth_db()
            other_household_logins = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE household_id = ? AND id != ?",
                (household_id, user_id),
            ).fetchone()["n"]

            # Only wipe the shared tracker data (entries/journal/weight/settings) plus its
            # pre-import safety-net snapshots if nobody else's login still shares this
            # household - if a partner's account points at the same household_id, their
            # data has to stay put even though this login is going away.
            if other_household_logins == 0:
                household_db_path = os.path.join(DATA_DIR, f"tracker_{household_id}.db")
                if os.path.exists(household_db_path):
                    os.remove(household_db_path)
                for backup_path in glob.glob(os.path.join("backups", f"tracker_{household_id}_pre_import_*.db")):
                    os.remove(backup_path)

            # The shared auth.db row is handled here regardless - deleting THIS login is
            # what actually removes the account and signs them out for good, whether or
            # not their household's tracker data survived them.
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            conn.close()

            session.pop("user_id", None)
            return redirect(url_for("login_view", deleted="1"))

    elif request.method == "POST":
        baby_name = (request.form.get("baby_name") or "").strip() or DEFAULT_BABY_NAME
        birth_date_str = request.form.get("birth_date") or date.today().isoformat()

        try:
            parsed_birth_date = datetime.strptime(birth_date_str, "%Y-%m-%d").date()
        except ValueError:
            error = "That doesn't look like a valid date."
            parsed_birth_date = None

        if parsed_birth_date is not None:
            if parsed_birth_date > latest_allowed:
                error = "Birth date can't be in the future."
            elif parsed_birth_date < earliest_allowed:
                error = "That birth date is more than 10 years ago - please double check it."
            else:
                save_settings(baby_name, birth_date_str)
                saved = True

    settings = get_settings()
    if error:
        # Re-show what they typed rather than silently reverting to the old saved values
        baby_name_value = request.form.get("baby_name") or settings["baby_name"]
        birth_date_value = request.form.get("birth_date") or settings["birth_date"].isoformat()
    else:
        baby_name_value = settings["baby_name"]
        birth_date_value = settings["birth_date"].isoformat()

    # Who else (if anyone) shares this household's tracker data via their own login -
    # shown so "add another login" visibly worked, not just taken on faith.
    this_user = current_user()
    conn = get_auth_db()
    household_members = conn.execute(
        "SELECT name, email FROM users WHERE household_id = ? ORDER BY id",
        (this_user["household_id"],),
    ).fetchall()
    conn.close()

    return render_template(
        "settings.html",
        baby_name_value=baby_name_value,
        birth_date_value=birth_date_value,
        min_birth_date=earliest_allowed.isoformat(),
        max_birth_date=latest_allowed.isoformat(),
        saved=saved,
        error=error,
        password_saved=password_saved,
        password_error=password_error,
        household_saved=household_saved,
        household_error=household_error,
        household_members=household_members,
        delete_error=delete_error,
        active="settings",
    )


# Runs at import time, not just when this file is executed directly - CREATE TABLE IF NOT
# EXISTS makes it a safe no-op on every later import/restart. This matters because a real
# deployment (e.g. a WSGI server on PythonAnywhere) imports this module instead of running
# it as __main__, so if init_db() only happened below, the users table would never get
# created there and the first signup attempt would fail outright.
init_db()

if __name__ == "__main__":
    # Debug mode (auto-reload + the interactive in-browser debugger) is OFF by default -
    # safe wherever this ends up running. Turn it on for your own local testing by setting
    # FLASK_DEBUG=1 before running, e.g. in PowerShell:
    #   $env:FLASK_DEBUG="1"; python app.py
    # Never set this on anything reachable by anyone but you - the debugger it enables
    # lets whoever's looking at an error page run code on the server.
    debug_mode = os.environ.get("FLASK_DEBUG") == "1"
    # host="0.0.0.0" makes it reachable from other devices on the home network
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
