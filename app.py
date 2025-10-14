import os
import json
import gspread
import csv
from flask import Flask, render_template, request, redirect, session, url_for
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ---------- TIME & STRING HELPERS ----------
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

# Quarter/date helpers (end is EXCLUSIVE)
DT_FMT = "%Y-%m-%d %H:%M:%S"

# 👉 Update these dates to match your school calendar.
# Example below assumes the 2025–2026 school year with Q2 starting Oct 20, 2025.
# 👉 Quarter dates for 2025–2026 (END is EXCLUSIVE)
QUARTERS = [
    {"name": "Q1", "start": "2025-08-06", "end": "2025-10-10"},  # adjust start if needed
    {"name": "Q2", "start": "2025-10-13", "end": "2025-12-20"},  # covers Oct 20–Dec 19
    {"name": "Q3", "start": "2026-01-06", "end": "2026-03-07"},  # covers Jan 6–Mar 6
    {"name": "Q4", "start": "2026-03-09", "end": "2026-05-23"},  # covers Mar 9–May 22
]

def signout_checks(first_name: str, last_name: str):
    """
    Return a list of (code, message) explaining why signout should be blocked.
    If list is empty, signout is allowed.
    """
    issues = []

    # Normalize names once
    first = safe_str(first_name)
    last  = safe_str(last_name)

    # Quarter state
    qname, start_dt, end_dt = _active_quarter_dt()

    # If you want to block during breaks, keep this; otherwise delete this block.
    if qname == "Unknown" or start_dt is None or end_dt is None:
        issues.append((
            "no_quarter",
            "No active quarter (e.g., break day). Passes resume next school day."
        ))

    # Capacity check
    passes = read_passes()
    currently_out = [p for p in passes if not safe_str(p.get('Time In'))]
    if len(currently_out) >= HALL_LIMIT:
        issues.append((
            "capacity",
            "The maximum number of students are already out. Please wait until someone returns."
        ))

    # Quarter limit (only if there is an active quarter)
    if qname != "Unknown":
        used = passes_this_quarter(first, last)
        if used >= MAX_QUARTER_PASSES:
            issues.append((
                "limit_reached",
                f"You have used all {MAX_QUARTER_PASSES} passes for this quarter ({qname})."
            ))

    return issues

def _to_local_midnight(date_str: str) -> datetime:
    y, m, d = [int(x) for x in date_str.split("-")]
    return datetime(y, m, d, 0, 0, 0, tzinfo=LOCAL_TZ)

def _active_quarter_dt(now: datetime | None = None):
    """Return (name, start_dt, end_dt) where end_dt is exclusive."""
    now = now or datetime.now(LOCAL_TZ)
    for q in QUARTERS:
        start = _to_local_midnight(q["start"])
        end   = _to_local_midnight(q["end"])  # exclusive
        if start <= now < end:
            return q["name"], start, end
    # If after last end date, treat last quarter as open-ended (optional)
    if QUARTERS and now >= _to_local_midnight(QUARTERS[-1]["end"]):
        q = QUARTERS[-1]
        return q["name"], _to_local_midnight(q["start"]), datetime.max.replace(tzinfo=LOCAL_TZ)
    # If before first start, treat first as current (optional)
    if QUARTERS and now < _to_local_midnight(QUARTERS[0]["start"]):
        q = QUARTERS[0]
        return q["name"], _to_local_midnight(q["start"]), _to_local_midnight(q["end"])
    return "Unknown", None, None

def _within_period(ts_str: str, start_dt: datetime, end_dt: datetime) -> bool:
    """ts_str is 'YYYY-MM-DD HH:MM:SS' in local time."""
    try:
        ts = datetime.strptime((ts_str or "").strip(), DT_FMT)
        # your stored timestamps are naïve; interpret as LOCAL_TZ
        ts = ts.replace(tzinfo=LOCAL_TZ)
        return start_dt is not None and end_dt is not None and (start_dt <= ts < end_dt)
    except Exception:
        return False

def now_str():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

def safe_str(v):
    """Coerce any value (None, float, etc.) to a safe trimmed string."""
    try:
        return str(v or "").strip()
    except Exception:
        return ""

def normalize_pin(v) -> str:
    """
    Normalize any PIN / Student ID-like value to a 4-digit string:
    - keep only digits,
    - use the last 4 digits,
    - zero-pad on the left to length 4.
    This makes '0123', '123', '123.0', '  00123 ' all compare equal.
    """
    s = safe_str(v)
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    return digits[-4:].zfill(4)

app = Flask(__name__)

# ---------- SECRETS & CONFIG (env-first) ----------
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-override")  # dev fallback
PASSWORD = os.environ.get("TEACHER_DASHBOARD_PASSWORD", "dev-password")  # dev fallback

HALL_LIMIT = int(os.environ.get("HALL_LIMIT", 10))
MAX_QUARTER_PASSES = int(os.environ.get("MAX_QUARTER_PASSES", 18))

# ---------- PIN FEATURE FLAG & ROSTER SOURCES ----------
# 0 = off (default, does nothing), 1 = on
ENABLE_STUDENT_PIN = int(os.environ.get("ENABLE_STUDENT_PIN", "0"))

# For local testing: a CSV in your project (columns: First Name, Last Name, Student ID)
ROSTER_CSV_PATH = os.environ.get("ROSTER_CSV_PATH", "roster.csv")

# For production: a tab in the same Google Sheet (columns: First Name, Last Name, PIN (or Student ID), Active)
ROSTER_SHEET_NAME = os.environ.get("ROSTER_SHEET_NAME", "Roster")

# Make the flag available inside templates
@app.context_processor
def inject_flags():
    return {"ENABLE_STUDENT_PIN": ENABLE_STUDENT_PIN}

# ---------- GOOGLE SHEETS (env-first creds, modern gspread auth) ----------
google_creds_json = os.environ.get("GOOGLE_CREDS_JSON")
if google_creds_json:
    try:
        google_creds = json.loads(google_creds_json)
    except json.JSONDecodeError as e:
        raise RuntimeError("GOOGLE_CREDS_JSON is set but contains invalid JSON.") from e
else:
    # Local development - load from file (keep out of git via .gitignore)
    try:
        with open("service_account.json") as f:
            google_creds = json.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(
            "No GOOGLE_CREDS_JSON env var and service_account.json not found. "
            "Set GOOGLE_CREDS_JSON in Render (paste full JSON) or add service_account.json locally."
        ) from e

# Modern auth (no oauth2client needed)
# Modern auth (no oauth2client needed)
client = gspread.service_account_from_dict(google_creds)

SHEET_NAME = os.environ.get("SHEET_NAME", "HallPassTracker")
PASS_LOG_SHEET_NAME = os.environ.get("PASS_LOG_SHEET_NAME", "PassLog").strip()

# The exact columns your routes expect
PASS_HEADERS = ["First Name", "Last Name", "Period", "Teacher", "Reason", "Time Out", "Time In"]

def _get_or_create_pass_sheet():
    """Open the pass log worksheet by name; create and seed headers if missing."""
    ss = client.open(SHEET_NAME)
    try:
        ws = ss.worksheet(PASS_LOG_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=PASS_LOG_SHEET_NAME, rows=1000, cols=len(PASS_HEADERS))
        ws.append_row(PASS_HEADERS)
        return ws

    # Ensure headers are present/consistent (first row)
    current = ws.row_values(1)
    if [c.strip() for c in current] != PASS_HEADERS:
        if not current:
            ws.append_row(PASS_HEADERS)  # empty sheet
        else:
            ws.update('1:1', [PASS_HEADERS])  # overwrite header row
    return ws

# Global handle used everywhere else in your code
sheet = _get_or_create_pass_sheet()

# ---------- ROSTER LOADING ----------
def load_roster_from_csv(path: str):
    """CSV columns: First Name, Last Name, Student ID (used as PIN)."""
    roster = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                fn = safe_str(r.get("First Name")).lower()
                ln = safe_str(r.get("Last Name")).lower()
                # (2) normalize when loading
                pin = normalize_pin(r.get("Student ID"))
                if fn and ln and pin:
                    roster[(fn, ln)] = {"pin": pin, "active": True}
    except FileNotFoundError:
        pass
    return roster

def load_roster_from_sheet():
    """Read from a Google Sheet worksheet named ROSTER_SHEET_NAME."""
    try:
        ws = client.open(SHEET_NAME).worksheet(ROSTER_SHEET_NAME)
    except Exception:
        return {}
    rows = ws.get_all_records(head=1, default_blank="")
    roster = {}
    for r in rows:
        fn = safe_str(r.get("First Name")).lower()
        ln = safe_str(r.get("Last Name")).lower()
        # (2) normalize when loading
        pin = normalize_pin(r.get("PIN") or r.get("Student ID"))
        active = safe_str(r.get("Active") or "Y").upper() != "N"
        if fn and ln and pin:
            roster[(fn, ln)] = {"pin": pin, "active": active}
    return roster

def get_roster():
    """Always load roster for suggestions. CSV first (local), else Sheet tab."""
    roster = load_roster_from_csv(ROSTER_CSV_PATH)
    if roster:
        return roster
    return load_roster_from_sheet()

def get_roster_name_lists():
    """
    Returns three sorted lists derived from the roster:
    - first_names: unique first names, Title-cased
    - last_names: unique last names, Title-cased
    - full_names: 'First Last' combined, Title-cased (optional use)
    """
    roster = get_roster()  # {(fn, ln): {...}}
    first_names = sorted({fn.title() for (fn, ln) in roster.keys()})
    last_names  = sorted({ln.title() for (fn, ln) in roster.keys()})
    full_names  = sorted({f"{fn.title()} {ln.title()}" for (fn, ln) in roster.keys()})
    return first_names, last_names, full_names

def check_student_pin(first: str, last: str, pin: str) -> bool:
    """True iff roster contains the student, marked active, and the PIN matches."""
    if not ENABLE_STUDENT_PIN:
        return True  # feature disabled
    roster = get_roster()
    rec = roster.get((safe_str(first).lower(), safe_str(last).lower()))
    # (3) normalize the entered pin before comparing
    entered = normalize_pin(pin)
    return bool(rec and rec["active"] and entered and entered == rec["pin"])

# ---------- CHOICES ----------
TEACHERS = [
    "R. Ahlrich", "B. Ames", "D. Andrews", "B. Barron", "J. Bird", "B. Brennan", "T. Brennan", "B. Breyette",
    "C. Caine", "H. Carbaugh-Keefe", "B. Carroll", "L. Carroll", "C. Carver", "M. Chavez", "J. Clark", "L. Day", "A. De Lucenay",
    "D. Derifield", "J. Dreibelbis", "B. Garrity", "K. Garrity", "S. Garrity", "N. Hart", "R. Heeren", "S. Houston",
    "C. Hughes", "J. Jimenez", "J. Kallenberg", "B. Langowski", "G. Miller", "A. Schmeltz",
    "P. Skirvin", "A. Smith", "B. Stiles", "G. Stout", "J. Taylor", "S. Taylor", "S. Vanlue"
]
REASONS = ["Restroom", "Water", "Office", "Locker", "Nurse", "Other"]
PERIODS = ["Advisory/STORM", "Period 2", "Period 3", "Period 4", "Period 5", "Period 6", "Period 7"]

# ---------- HELPERS ----------
def student_has_open_pass(first: str, last: str) -> bool:
    """True if this student already has a row with Time Out set and empty Time In."""
    first_l, last_l = safe_str(first).lower(), safe_str(last).lower()
    for row in read_passes():
        if safe_str(row.get('First Name')).lower() == first_l and safe_str(row.get('Last Name')).lower() == last_l:
            if safe_str(row.get('Time Out')) and not safe_str(row.get('Time In')):
                return True
    return False

def recent_signout_exists(first: str, last: str, window_seconds: int = 20) -> bool:
    """True if a sign-out for this student was recorded within the last N seconds."""
    first_l, last_l = safe_str(first).lower(), safe_str(last).lower()
    now_local = datetime.now(LOCAL_TZ)
    for row in read_passes():
        if safe_str(row.get('First Name')).lower() == first_l and safe_str(row.get('Last Name')).lower() == last_l:
            ts = safe_str(row.get('Time Out'))
            if not ts:
                continue
            try:
                t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)
            except ValueError:
                continue
            if (now_local - t).total_seconds() <= window_seconds:
                return True
    return False

def read_passes():
    records = sheet.get_all_records()
    return [row for row in records if any(row.values())]

def write_pass(entry):
    sheet.append_row([
        entry['First Name'], entry['Last Name'], entry['Period'], entry['Teacher'],
        entry['Reason'], entry['Time Out'], entry['Time In']
    ])

def get_current_quarter():
    name, _, _ = _active_quarter_dt()
    return name

def passes_this_quarter(first, last):
    passes = read_passes()
    qname, start_dt, end_dt = _active_quarter_dt()
    if start_dt is None or end_dt is None:
        return 0

    first_l = safe_str(first).lower()
    last_l  = safe_str(last).lower()
    count = 0

    for row in passes:
        fn = safe_str(row.get('First Name')).lower()
        ln = safe_str(row.get('Last Name')).lower()
        if fn == first_l and ln == last_l:
            time_out_str = safe_str(row.get('Time Out'))
            if _within_period(time_out_str, start_dt, end_dt):
                count += 1
    return count

def auto_close_stale_passes(max_minutes: int = 30) -> int:
    """
    Auto-sign students back in if they've been out longer than max_minutes.
    Returns how many rows were auto-closed.
    """
    try:
        rows = sheet.get_all_values()
        if not rows:
            return 0

        headers = rows[0]
        # Find the columns we need
        try:
            timeout_idx = headers.index("Time Out")
            timein_idx  = headers.index("Time In")
        except ValueError:
            # Headers missing; nothing to do
            return 0

        closed = 0
        # Start at row 2 (sheet row numbers are 1-based; row 1 = headers)
        for r, row in enumerate(rows[1:], start=2):
            time_out = (row[timeout_idx] or "").strip()
            time_in  = (row[timein_idx]  or "").strip()

            # Only consider rows that have Time Out and empty Time In
            if time_out and time_in == "":
                # Parse "YYYY-MM-DD HH:MM:SS" written by now_str()
                try:
                    out_dt = datetime.strptime(time_out, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    # Skip rows with unexpected formats
                    continue

                # Compare using local time (you already have LOCAL_TZ and now_str())
                now_naive = datetime.now(LOCAL_TZ).replace(tzinfo=None)
                if now_naive - out_dt > timedelta(minutes=max_minutes):
                    sheet.update_cell(r, timein_idx + 1, now_str())
                    closed += 1

        return closed
    except Exception as e:
        # Don't crash the request if Sheets is flaky—just log and continue
        print(f"auto_close_stale_passes error: {e}")
        return 0

# ---------- ROUTES ----------
@app.route('/')
def home():
    auto_close_stale_passes()  # keep this

    # Build name options for datalist suggestions
    first_names, last_names, _ = get_roster_name_lists()

    name = request.args.get('name')
    used_passes = None
    if name:
        try:
            first, last = name.strip().split(' ', 1)
            used_passes = passes_this_quarter(first, last)
            name = f"{first} {last}"
        except ValueError:
            name = None

    return render_template(
        'index.html',
        name=name,
        used_passes=used_passes,
        teachers=TEACHERS,
        reasons=REASONS,
        periods=PERIODS,
        first_name_options=first_names,
        last_name_options=last_names
    )

@app.route('/signout', methods=['POST'])
def signout():
    first_name   = request.form.get('first_name', '').strip()
    last_name    = request.form.get('last_name', '').strip()
    pin          = request.form.get('pin', '').strip()
    period       = request.form.get('period', '').strip()
    teacher      = request.form.get('teacher', '').strip()
    reason       = request.form.get('reason', '').strip()
    other_reason = request.form.get('other_reason', '').strip()
    final_reason = other_reason if (reason == "Other" and other_reason) else reason
    time_out     = now_str()

    # Preload options for any early return
    first_names, last_names, _ = get_roster_name_lists()

    # Basic validation
    if not first_name or not last_name:
        return render_template(
            "index.html",
            error="First and last name are required.",
            error_code="name_required",
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        ), 400

    # If PIN feature is enabled, validate client + server side
    if ENABLE_STUDENT_PIN:
        if not pin:
            return render_template(
                "index.html",
                error="Enter the last 4 digits of your Student ID (numbers only).",
                error_code="pin_required",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 400
        if not check_student_pin(first_name, last_name, pin):
            return render_template(
                "index.html",
                error="Name and last 4 of Student ID didn’t match our roster.",
                error_code="pin_mismatch",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 403

    # --- Guard rails: capacity / (optional) break / quarter limit ---
    # If you added signout_checks() earlier, keep using it:
    if 'signout_checks' in globals():
        problems = signout_checks(first_name, last_name, block_on_no_quarter=False)
        if problems:
            code, msg = problems[0]
            status_map = {"capacity": 429, "limit_reached": 403, "no_quarter": 409}
            status = status_map.get(code, 400)
            return render_template(
                "index.html",
                error=msg,
                error_code=code,
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), status
    else:
        # (Fallback) Do the two core checks inline if you didn't add signout_checks()
        passes_all = read_passes()
        currently_out = [p for p in passes_all if not safe_str(p.get('Time In'))]
        if len(currently_out) >= HALL_LIMIT:
            return render_template(
                "index.html",
                error="The maximum number of students are already out. Please wait.",
                error_code="capacity",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 429
        if passes_this_quarter(first_name, last_name) >= MAX_QUARTER_PASSES:
            return render_template(
                "index.html",
                error=f"You have used all {MAX_QUARTER_PASSES} passes for this quarter.",
                error_code="limit_reached",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 403

    # --- Duplicate protection (idempotency) ---
    if student_has_open_pass(first_name, last_name):
        return render_template(
            "index.html",
            error="You’re already signed out. Please sign back in before starting a new pass.",
            error_code="already_out",
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        ), 409  # Conflict

    if recent_signout_exists(first_name, last_name, window_seconds=20):
        return render_template(
            "index.html",
            error="We already received your sign-out. Please wait a few seconds.",
            error_code="duplicate_click",
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        ), 202  # Accepted (we got it)

  # Write entry (do NOT store the PIN)
entry = {
    'First Name': first_name,
    'Last Name': last_name,
    'Period': period,
    'Teacher': teacher,
    'Reason': final_reason,
    'Time Out': time_out,
    'Time In': ''
}

# --- Safe write with detailed logging ---
import traceback
try:
    write_pass(entry)  # this does sheet.append_row([...])
    return redirect(url_for('home', name=f"{first_name} {last_name}"))
except Exception as e:
    # Log a detailed traceback to Render logs so we can see the root cause
    print("write_pass error:", repr(e))
    print("TRACEBACK:\n", traceback.format_exc())
    return render_template(
        "index.html",
        error="Couldn’t save your pass to the Google Sheet. Please try again in a moment.",
        error_code="write_failed",
        teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
        first_name_options=first_names, last_name_options=last_names
    ), 502

@app.route("/signin", methods=["POST"])
def signin():
    # Accept either "full_name" or "name" from the form
    full_name = (request.form.get("full_name") or request.form.get("name") or "").strip()
    full_name = " ".join(full_name.split())  # normalize extra spaces
    if not full_name or " " not in full_name:
        return "First and last name are required", 400

    parts = full_name.split(" ")
    first_name = parts[0]
    last_name  = " ".join(parts[1:])

    # Pull rows and header row from Google Sheets
    records = sheet.get_all_records()
    headers = sheet.row_values(1)
    try:
        time_in_col = headers.index("Time In") + 1
    except ValueError as e:
        return f"Missing expected column: {e}", 500

    target_first = first_name.strip().lower()
    target_last  = last_name.strip().lower()

    for idx, row in enumerate(records, start=2):
        row_first   = str(row.get("First Name", "")).strip().lower()
        row_last    = str(row.get("Last Name", "")).strip().lower()
        time_in_val = str(row.get("Time In", "")).strip()
        is_time_in_empty = (time_in_val == "" or time_in_val.lower() in ("none", "nan"))

        if row_first == target_first and row_last == target_last and is_time_in_empty:
            sheet.update_cell(idx, time_in_col, now_str())
            used_passes = passes_this_quarter(first_name, last_name)
            return render_template("signin_success.html",
                                   first_name=first_name,
                                   last_name=last_name,
                                   used_passes=used_passes)

    return "Student not found or already signed in", 404

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        return render_template('login.html', error='Incorrect password')
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    auto_close_stale_passes()
    passes = read_passes()
    counts = get_pass_counts()
    currently_out = [p for p in passes if not str(p.get('Time In', '')).strip()]
    return render_template('dashboard.html', passes=currently_out, counts=counts)

@app.route('/student_list')
def student_list():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    auto_close_stale_passes()
    passes = read_passes()
    counts = get_pass_counts()
    current_quarter = get_current_quarter()
    return render_template('student_list.html', counts=counts, current_quarter=current_quarter)

@app.route('/logout')
def logout():
    session['logged_in'] = False
    return redirect(url_for('home'))

def get_pass_counts():
    passes = read_passes()
    qname, start_dt, end_dt = _active_quarter_dt()
    counts = {}
    if start_dt is None or end_dt is None:
        return counts

    for entry in passes:
        first = safe_str(entry.get("First Name"))
        last  = safe_str(entry.get("Last Name"))
        name = f"{first} {last}".strip()
        if not name:
            continue
        tout = safe_str(entry.get("Time Out"))
        if _within_period(tout, start_dt, end_dt):
            counts[name] = counts.get(name, 0) + 1
    return counts

@app.route("/diag_sheets")
def diag_sheets():
    try:
        ss = client.open(SHEET_NAME)
        worksheets = [ws.title for ws in ss.worksheets()]
        # Try to open the desired pass log sheet; create if missing.
        try:
            ws = ss.worksheet(PASS_LOG_SHEET_NAME)
            status = f"Found worksheet '{PASS_LOG_SHEET_NAME}'."
        except gspread.exceptions.WorksheetNotFound:
            ws = ss.add_worksheet(title=PASS_LOG_SHEET_NAME, rows=1000, cols=len(PASS_HEADERS))
            ws.append_row(PASS_HEADERS)
            status = f"Created worksheet '{PASS_LOG_SHEET_NAME}' and wrote headers."

        # Ensure headers on row 1 are correct
        current_headers = ws.row_values(1)
        if [c.strip() for c in current_headers] != PASS_HEADERS:
            if not current_headers:
                ws.append_row(PASS_HEADERS)
                status += " Added headers to empty sheet."
            else:
                ws.update('1:1', [PASS_HEADERS])
                status += " Updated header row to expected columns."

        return (
            f"Opened spreadsheet: {ss.title}<br>"
            f"Existing tabs: {worksheets}<br>"
            f"PASS_LOG_SHEET_NAME: {PASS_LOG_SHEET_NAME}<br>"
            f"Status: {status}<br>"
            f"Headers now: {ws.row_values(1)}",
            200
        )
    except Exception as e:
        # This will surface permission issues like PERMISSION_DENIED
        return f"diag_sheets error: {type(e).__name__}: {e}", 500

# ---- Optional health/diagnostic endpoints (handy on Render) ----
@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/diag")
def diag():
    try:
        title = client.open(SHEET_NAME).title
        return f"Sheets OK. Opened: {title}", 200
    except Exception as e:
        return f"Sheets error: {type(e).__name__}: {e}", 500

# ---------- RUN ----------
if __name__ == '__main__':
    app.run(debug=True)
