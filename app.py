import os
import json
import gspread
import csv  # <-- add this
from flask import Flask, render_template, request, redirect, session, url_for
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ▶️ Add these three lines:
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")
def now_str():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

def safe_str(v):
    """Coerce any value (None, float, etc.) to a safe trimmed string."""
    try:
        return str(v or "").strip()
    except Exception:
        return ""

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

def load_roster_from_csv(path: str):
    """CSV columns: First Name, Last Name, Student ID (used as PIN)."""
    roster = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                fn = safe_str(r.get("First Name")).lower()
                ln = safe_str(r.get("Last Name")).lower()
                pin = safe_str(r.get("Student ID"))
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
        pin = safe_str(r.get("PIN") or r.get("Student ID"))
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
    return bool(rec and rec["active"] and safe_str(pin) == rec["pin"])

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
client = gspread.service_account_from_dict(google_creds)
SHEET_NAME = os.environ.get("SHEET_NAME", "HallPassTracker")
sheet = client.open(SHEET_NAME).sheet1

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
def read_passes():
    records = sheet.get_all_records()
    return [row for row in records if any(row.values())]

def write_pass(entry):
    sheet.append_row([
        entry['First Name'], entry['Last Name'], entry['Period'], entry['Teacher'],
        entry['Reason'], entry['Time Out'], entry['Time In']
    ])

def get_current_quarter():
    today = date.today()
    year = today.year
    quarters = {
        "Q1": (date(year, 7, 1), date(year, 10, 31)),
        "Q2": (date(year, 11, 1), date(year + 1, 1, 15)),
        "Q3": (date(year, 1, 16), date(year, 3, 31)),
        "Q4": (date(year, 4, 1), date(year, 6, 30)),
    }
    for q, (start, end) in quarters.items():
        if start <= today <= end or (start.month > end.month and (today >= start or today <= end)):
            return q
    return "Unknown"

def passes_this_quarter(first, last):
    passes = read_passes()
    quarter = get_current_quarter()
    count = 0

    today = date.today()
    year = today.year
    quarters = {
        "Q1": (date(year, 7, 1), date(year, 10, 31)),
        "Q2": (date(year, 11, 1), date(year + 1, 1, 15)),
        "Q3": (date(year, 1, 16), date(year, 3, 31)),
        "Q4": (date(year, 4, 1), date(year, 6, 30)),
    }
    start, end = quarters.get(quarter, (None, None))

    for row in passes:
        fn = safe_str(row.get('First Name')).lower()
        ln = safe_str(row.get('Last Name')).lower()
        if fn == safe_str(first).lower() and ln == safe_str(last).lower():
            time_out_str = safe_str(row.get('Time Out'))
            if not time_out_str:
                continue
            try:
                time_out = datetime.strptime(time_out_str, "%Y-%m-%d %H:%M:%S").date()
            except ValueError:
                continue

            if start and end and (
                (start <= end and start <= time_out <= end) or
                (start > end and (time_out >= start or time_out <= end))
            ):
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
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        ), 400

    # If PIN feature is enabled, validate client + server side
    if ENABLE_STUDENT_PIN:
        if len(pin) != 4 or not pin.isdigit():
            return render_template(
                "index.html",
                error="Enter the last 4 digits of your Student ID (numbers only).",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 400
        if not check_student_pin(first_name, last_name, pin):
            return render_template(
                "index.html",
                error="Name and last 4 of Student ID didn’t match our roster.",
                teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
                first_name_options=first_names, last_name_options=last_names
            ), 403

    # Capacity checks
    passes = read_passes()
    currently_out = [p for p in passes if not safe_str(p.get('Time In'))]
    if len(currently_out) >= HALL_LIMIT:
        return render_template(
            "index.html",
            error="The maximum number of students are already out. Please wait.",
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        )

    if passes_this_quarter(first_name, last_name) >= MAX_QUARTER_PASSES:
        return render_template(
            "index.html",
            error=f"You have used all {MAX_QUARTER_PASSES} passes for this quarter.",
            teachers=TEACHERS, reasons=REASONS, periods=PERIODS,
            first_name_options=first_names, last_name_options=last_names
        )

    # Write entry (do NOT store the PIN; if you ever log it, mask it)
    entry = {
        'First Name': first_name,
        'Last Name': last_name,
        'Period': period,
        'Teacher': teacher,
        'Reason': final_reason,
        'Time Out': time_out,
        'Time In': ''
    }
    write_pass(entry)

    return redirect(url_for('home', name=f"{first_name} {last_name}"))


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
    counts = {}
    for entry in passes:
        first = safe_str(entry.get("First Name"))
        last  = safe_str(entry.get("Last Name"))
        name = f"{first} {last}".strip()
        if not name:
            continue  # skip rows without names
        counts[name] = counts.get(name, 0) + 1
    return counts


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
