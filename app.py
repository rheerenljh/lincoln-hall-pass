import os
import json
import gspread
import csv
from flask import Flask, render_template, request, redirect, session, url_for, render_template_string
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

SKILL_SHEET_NAME = "SkillTracker"
SKILL_FORM_TAB = "Form Responses 1"
SKILL_REQUIREMENTS_TAB = "SkillRequirements"

# ---------- TIME & STRING HELPERS ----------
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")

SCHOOL_YEAR = "2026-2027"

# Quarter/date helpers (end is EXCLUSIVE)
DT_FMT = "%Y-%m-%d %H:%M:%S"

# 👉 Update these dates to match your school calendar.
# Example below assumes the 2026-2027 school year with Q2 starting Oct 19, 2026.
# 👉 Quarter dates for 2026–2027 (END is EXCLUSIVE)
QUARTERS = [
    {"name": "Q1", "start": "2026-08-03", "end": "2026-10-10"},  # adjust start if needed
    {"name": "Q2", "start": "2026-10-19", "end": "2026-12-19"},  # covers Oct 19–Dec 20
    {"name": "Q3", "start": "2027-01-05", "end": "2027-03-13"},  # covers Jan 5–Mar 12
    {"name": "Q4", "start": "2027-03-15", "end": "2027-05-28"},  # covers Mar 15–May 27
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
STAFF_PASSWORD = os.environ.get(
    "LINCOLN_STAFF_PASSWORD",
    "dev-password"
)

HALL_LIMIT = int(os.environ.get("HALL_LIMIT", 10))
MAX_QUARTER_PASSES = int(os.environ.get("MAX_QUARTER_PASSES", 18))

# ---------- PIN FEATURE FLAG & ROSTER SOURCES ----------
# 0 = off (default, does nothing), 1 = on
ENABLE_STUDENT_PIN = int(os.environ.get("ENABLE_STUDENT_PIN", "0"))

# For local testing: a CSV in your project (columns: First Name, Last Name, Student ID)
ROSTER_CSV_PATH = os.environ.get("ROSTER_CSV_PATH", "roster.csv")

# For production: a tab in the same Google Sheet (columns: First Name, Last Name, PIN (or Student ID), Active)
ROSTER_SHEET_NAME = os.environ.get("ROSTER_SHEET_NAME", "Roster")

# ---------- TEMPLATE GLOBALS ----------
@app.context_processor
def inject_globals():
    try:
        current_q = get_current_quarter()
    except Exception:
        current_q = "Unknown"

    return {
        "ENABLE_STUDENT_PIN": ENABLE_STUDENT_PIN,
        "CURRENT_QUARTER": current_q,
        "MAX_QUARTER_PASSES": MAX_QUARTER_PASSES,
    }

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

SHEET_NAME = os.environ.get(
    "SHEET_NAME",
    "HallPassTracker"
)

# The exact columns used by every quarterly pass-log sheet.
PASS_HEADERS = [
    "First Name",
    "Last Name",
    "Period",
    "Teacher",
    "Reason",
    "Time Out",
    "Time In"
]


def _get_or_create_pass_sheet():
    """
    Open the worksheet for the current school-year quarter.

    Examples:
    2026-2027 Q1
    2026-2027 Q2
    """
    worksheet_name = get_quarter_pass_sheet_name()
    spreadsheet = client.open(SHEET_NAME)

    try:
        worksheet = spreadsheet.worksheet(
            worksheet_name
        )

    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=1000,
            cols=len(PASS_HEADERS)
        )

        worksheet.append_row(PASS_HEADERS)

        return worksheet

    current_headers = worksheet.row_values(1)

    normalized_headers = [
        str(header).strip()
        for header in current_headers
    ]

    if normalized_headers != PASS_HEADERS:
        if not current_headers:
            worksheet.append_row(PASS_HEADERS)
        else:
            worksheet.update(
                "1:1",
                [PASS_HEADERS]
            )

    return worksheet

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
    """
    Load the current Google Sheets roster first.
    Use the local CSV only as a backup.
    """
    roster = load_roster_from_sheet()

    if roster:
        return roster

    return load_roster_from_csv(ROSTER_CSV_PATH)

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
    "R. Ahlrich", "B. Ames", "D. Andrews", "M. Berg", "J. Bird", "J. Breden", "B. Brennan", "T. Brennan",
    "C. Caine", "H. Carbaugh-Keefe", "L. Carroll", "C. Carver", "A. De Lucenay",
    "D. Derifield", "J. Dreibelbis", "B. Garrity", "S. Garrity", "N. Hart", "R. Heeren", "S. Henneberger", "S. Houston",
    "S. Hovermale", "A. Howell", "C. Hughes", "J. Hyden", "J. Jimenez", "J. Kallenberg", "B. Langowski", "B. Marquardt", "A. Oliver", "A. Schmeltz",
    "P. Skirvin", "A. Smith", "B. Stiles", "G. Stout", "S. Taylor", "S. Vanlue", "M. Vinson"
]
REASONS = ["Restroom", "Water", "Office", "Locker", "Nurse", "Other"]
PERIODS = ["Advisory/STORM", "Period 2", "Period 3", "Period 4", "Period 5", "Period 6", "Period 7"]

# ---------- HELPERS ----------
def student_has_open_pass(first: str, last: str) -> bool:
    """True if the student has a row with Time Out set and Time In empty."""
    try:
        first_l, last_l = safe_str(first).lower(), safe_str(last).lower()
        for row in (read_passes() or []):
            fn = safe_str(row.get('First Name')).lower()
            ln = safe_str(row.get('Last Name')).lower()
            tout = safe_str(row.get('Time Out'))
            tin  = safe_str(row.get('Time In'))
            if fn == first_l and ln == last_l and tout and not tin:
                return True
        return False
    except Exception as e:
        import traceback
        print("student_has_open_pass error:", repr(e))
        print("TRACEBACK:\n", traceback.format_exc())
        # Fail safe (assume not open so we don't block sign-out due to an error)
        return False

def recent_signout_exists(first: str, last: str, window_seconds: int = 20) -> bool:
    """True if a sign-out for this student was written within the last N seconds."""
    try:
        first_l, last_l = safe_str(first).lower(), safe_str(last).lower()
        now_local = datetime.now(LOCAL_TZ)
        for row in (read_passes() or []):
            fn = safe_str(row.get('First Name')).lower()
            ln = safe_str(row.get('Last Name')).lower()
            if fn != first_l or ln != last_l:
                continue
            ts = safe_str(row.get('Time Out'))
            if not ts:
                continue
            try:
                t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                # Skip unexpected formats
                continue
            # interpret stored naive time as local
            t = t.replace(tzinfo=LOCAL_TZ)
            if (now_local - t).total_seconds() <= window_seconds:
                return True
        return False
    except Exception as e:
        import traceback
        print("recent_signout_exists error:", repr(e))
        print("TRACEBACK:\n", traceback.format_exc())
        # Fail safe (assume not recent to avoid blocking)
        return False

def read_passes():
    current_sheet = _get_or_create_pass_sheet()

    records = current_sheet.get_all_records()

    return [
        row
        for row in records
        if any(row.values())
    ]


def write_pass(entry):
    current_sheet = _get_or_create_pass_sheet()

    current_sheet.append_row([
        entry["First Name"],
        entry["Last Name"],
        entry["Period"],
        entry["Teacher"],
        entry["Reason"],
        entry["Time Out"],
        entry["Time In"]
    ])

def get_current_quarter():
    name, _, _ = _active_quarter_dt()
    return name


def get_quarter_pass_sheet_name():
    quarter = get_current_quarter()

    if quarter == "Unknown":
        quarter = "Outside Quarter"

    return f"{SCHOOL_YEAR} {quarter}"

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
        current_sheet = _get_or_create_pass_sheet()

        rows = current_sheet.get_all_values()

        if not rows:
            return 0

        headers = rows[0]

        try:
            timeout_idx = headers.index("Time Out")
            timein_idx = headers.index("Time In")
        except ValueError:
            return 0

        closed = 0

        for row_number, row in enumerate(rows[1:], start=2):
            time_out = (row[timeout_idx] or "").strip()
            time_in = (row[timein_idx] or "").strip()

            if time_out and time_in == "":
                try:
                    out_dt = datetime.strptime(
                        time_out,
                        "%Y-%m-%d %H:%M:%S"
                    )
                except ValueError:
                    continue

                now_naive = datetime.now(
                    LOCAL_TZ
                ).replace(tzinfo=None)

                if (
                    now_naive - out_dt
                    > timedelta(minutes=max_minutes)
                ):
                    current_sheet.update_cell(
                        row_number,
                        timein_idx + 1,
                        now_str()
                    )

                    closed += 1

        return closed

    except Exception as error:
        print(
            f"auto_close_stale_passes error: {error}"
        )

        return 0

def render_index_error(error_msg: str, error_code: str, status: int = 400, error_detail: str | None = None):
    """Re-render the index with a big in-page error banner (no 500s)."""
    # Build the options the form needs
    try:
        first_names, last_names, _ = get_roster_name_lists()
    except Exception:
        first_names, last_names = [], []

    # Optional: keep the pass counter visible after an error
    name = None
    used_passes = None
    fn = safe_str(request.form.get("first_name"))
    ln = safe_str(request.form.get("last_name"))
    if fn and ln:
        name = f"{fn} {ln}"
        try:
            used_passes = passes_this_quarter(fn, ln)
        except Exception:
            used_passes = None

    return (
        render_template(
            "index.html",
            error=error_msg,                 # headline
            error_detail=error_detail,       # NEW: one clear helper line
            error_code=error_code,
            teachers=TEACHERS,
            reasons=REASONS,
            periods=PERIODS,
            first_name_options=first_names,
            last_name_options=last_names,
            name=name,
            used_passes=used_passes,
        ),
        status,
    )

# ---------- TEMPLATE GLOBALS ----------
@app.context_processor
def inject_globals():
    # Keep all template-level globals in one place
    try:
        current_q = get_current_quarter()
    except Exception:
        current_q = "Unknown"

    return {
        "ENABLE_STUDENT_PIN": ENABLE_STUDENT_PIN,
        "CURRENT_QUARTER": current_q,
        "MAX_QUARTER_PASSES": MAX_QUARTER_PASSES,
    }

# ---------- ROUTES ----------
@app.route('/')
def home():
    import traceback
    try:
        auto_close_stale_passes()

        # Build name options for datalist suggestions
        first_names, last_names, _ = get_roster_name_lists()

        # Work out the current quarter name
        qname, _, _ = _active_quarter_dt()

        # Optional “hello, NAME” + usage count if a name param is present
        name = (request.args.get('name') or '').strip() or None
        used_passes = None
        if name and ' ' in name:
            first, last = name.split(' ', 1)
            used_passes = passes_this_quarter(first, last)
            name = f"{first} {last}"

        # Always provide every key the template might use
        return render_template(
            'index.html',
            name=name,
            used_passes=used_passes,
            teachers=TEACHERS,
            reasons=REASONS,
            periods=PERIODS,
            first_name_options=first_names,
            last_name_options=last_names,
            CURRENT_QUARTER=qname,
            MAX_QUARTER_PASSES=MAX_QUARTER_PASSES,
            ENABLE_STUDENT_PIN=ENABLE_STUDENT_PIN,
            error=None,
            error_code=None
        )
    except Exception as e:
        # Never 500 the home page—log and render with safe defaults
        print("home() error:", repr(e))
        print("TRACEBACK:\n", traceback.format_exc())
        return render_template(
            'index.html',
            name=None,
            used_passes=None,
            teachers=TEACHERS,
            reasons=REASONS,
            periods=PERIODS,
            first_name_options=[],
            last_name_options=[],
            CURRENT_QUARTER="Unknown",
            MAX_QUARTER_PASSES=MAX_QUARTER_PASSES,
            ENABLE_STUDENT_PIN=ENABLE_STUDENT_PIN,
            error="We couldn’t load everything. You can still sign out below.",
            error_code="home_render_partial"
        ), 200

@app.route('/signout', methods=['POST'])
def signout():
    import traceback
    try:
        first_name   = request.form.get('first_name', '').strip()
        last_name    = request.form.get('last_name', '').strip()
        pin          = request.form.get('pin', '').strip()
        period       = request.form.get('period', '').strip()
        teacher      = request.form.get('teacher', '').strip()
        reason       = request.form.get('reason', '').strip()
        other_reason = request.form.get('other_reason', '').strip()
        final_reason = other_reason if (reason == "Other" and other_reason) else reason
        time_out     = now_str()

        # Basic validation
        if not first_name or not last_name:
            return render_index_error("First and last name are required.", "name_required", 200)

        # PIN validation (if enabled)
        if ENABLE_STUDENT_PIN:
            if not pin:
                return render_index_error(
                    "Enter the last 4 digits of your Student ID (numbers only).",
                    "pin_required", 200
                )
            if not check_student_pin(first_name, last_name, pin):
                return render_index_error(
                    "Name and last 4 of Student ID didn’t match our roster.",
                    "pin_mismatch", 200
                )

        # Require text if "Other"
        if reason == "Other" and not other_reason:
            return render_index_error(
                "Please type your reason when selecting ‘Other’.",
                "other_required", 200
            )

        # --- Capacity guard (robust, logs current/limit) ---
        try:
            passes_all = read_passes() or []
            currently_out = [p for p in passes_all if not safe_str(p.get('Time In'))]
            hall_limit = int(HALL_LIMIT)
            print(f"[capacity] out={len(currently_out)} limit={hall_limit}")
           
            if len(currently_out) >= hall_limit:
                return render_index_error(
                    f"The hall is at capacity ({hall_limit} students out).",  # short headline
                    "capacity", 200,
                    "All spots are currently taken. Please try again when someone returns."  # detail
                )
            
        except Exception as cap_e:
            print("capacity-guard error:", repr(cap_e))
            print("TRACEBACK:\n", traceback.format_exc())
            return render_index_error(
                "We couldn’t check hall capacity. Please try again in a moment.",
                "capacity_check_failed", 500
            )

        # --- Quarter limit (robust) ---
        try:
            if passes_this_quarter(first_name, last_name) >= MAX_QUARTER_PASSES:
                return render_index_error(
                    f"You have used all {MAX_QUARTER_PASSES} passes for this quarter.",
                    "limit_reached", 200
                )
        except Exception as limit_e:
            print("limit-check error:", repr(limit_e))
            print("TRACEBACK:\n", traceback.format_exc())
            return render_index_error(
                "We couldn’t verify your quarter pass count. Please try again in a moment.",
                "limit_check_failed", 500
            )

        # --- Duplicate protection (robust; logs results) ---
        try:
            open_now = student_has_open_pass(first_name, last_name)
            print(f"[dup] student_has_open_pass('{first_name} {last_name}') -> {open_now}")
            if open_now:
                return render_index_error(
                    "You’re already signed out.",        # short headline
                    "already_out", 200,
                    "Please sign back in before starting a new pass."  # detail
                )

            just_sent = recent_signout_exists(first_name, last_name, window_seconds=20)
            print(f"[dup] recent_signout_exists('{first_name} {last_name}') -> {just_sent}")
            if just_sent:
                return render_index_error(
                    "We already received your sign-out.",  # short headline
                    "duplicate_click", 200,
                    "Please wait a few seconds."           # detail
                )
            
        except Exception as dup_e:
            print("duplicate-guard error:", repr(dup_e))
            print("TRACEBACK:\n", traceback.format_exc())
            return render_index_error(
                "Something went wrong checking your current pass. Please try again, or ask a teacher.",
                "dup_check_failed", 500
            )

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

        try:
            write_pass(entry)
            return redirect(url_for('home', name=f"{first_name} {last_name}"))
        except Exception as e:
            print("write_pass error:", repr(e))
            print("TRACEBACK:\n", traceback.format_exc())
            return render_index_error(
                "Couldn’t save your pass to the Google Sheet. Please try again in a moment.",
                "write_failed", 502
            )

    except Exception as e:
        # FINAL CATCH-ALL: no more generic 500s
        print("signout unhandled error:", repr(e))
        print("TRACEBACK:\n", traceback.format_exc())
        return render_index_error(
            "Something went wrong processing your pass. Please try again, or ask a teacher.",
            "unhandled", 500
        )

@app.route("/signin", methods=["POST"])
def signin():
    # Accept either "full_name" or "name" from the form.
    full_name = (
        request.form.get("full_name")
        or request.form.get("name")
        or ""
    ).strip()

    full_name = " ".join(full_name.split())

    if not full_name or " " not in full_name:
        return "First and last name are required", 400

    parts = full_name.split(" ")
    first_name = parts[0]
    last_name = " ".join(parts[1:])

    current_sheet = _get_or_create_pass_sheet()

    records = current_sheet.get_all_records()
    headers = current_sheet.row_values(1)

    try:
        time_in_col = headers.index("Time In") + 1
    except ValueError as error:
        return f"Missing expected column: {error}", 500

    target_first = first_name.strip().lower()
    target_last = last_name.strip().lower()

    for row_number, row in enumerate(records, start=2):
        row_first = safe_str(
            row.get("First Name")
        ).lower()

        row_last = safe_str(
            row.get("Last Name")
        ).lower()

        time_in_value = safe_str(
            row.get("Time In")
        )

        is_time_in_empty = (
            time_in_value == ""
            or time_in_value.lower() in ("none", "nan")
        )

        if (
            row_first == target_first
            and row_last == target_last
            and is_time_in_empty
        ):
            current_sheet.update_cell(
                row_number,
                time_in_col,
                now_str()
            )

            used_passes = passes_this_quarter(
                first_name,
                last_name
            )

            return render_template(
                "signin_success.html",
                first_name=first_name,
                last_name=last_name,
                used_passes=used_passes
            )

    return "Student not found or already signed in", 404

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form['password'] == STAFF_PASSWORD:
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
@app.errorhandler(500)
def handle_500(e):
    import traceback
    print("Unhandled 500:", repr(e))
    print("TRACEBACK:\n", traceback.format_exc())
    return render_index_error(
        "Something went wrong saving your pass. Please try again, or ask a teacher.",
        "write_failed", 500
    )

@app.route("/promise")
def promise():
    student_id = normalize_pin(
        request.args.get("id", "")
    )

    award_error = request.args.get("award_error")
    award_success = request.args.get("award_success")

    if not student_id:
        return render_template_string("""
        <html>
        <head>
          <style>
            body {
              font-family: Arial;
              text-align: center;
              padding: 40px;
              background: #f4f4f4;
            }

          .card {
              background: white;
              padding: 30px;
              border-radius: 12px;
              max-width: 400px;
              margin: auto;
              box-shadow: 0 4px 10px rgba(0,0,0,0.1);
            }

             input {
              padding: 12px;
              font-size: 18px;
              width: 200px;
              margin-bottom: 15px;
            }

            button {
              padding: 10px 20px;
              font-size: 18px;
              background: #c1121f;
              color: white;
              border: none;
              border-radius: 6px;
              cursor: pointer;
            }
          </style>
        </head>

        <body>
          <div class="card">
            <h2>Enter Student ID</h2>

            <form action="/lookup" method="post">
              <input type="text" name="student_id" maxlength="4" required>
              <br>
              <button type="submit">View Status</button>
            </form>
          </div>
        </body>
        </html>
        """)

    ss = client.open_by_key("1JP4mscjAyY73ZEFlrStxaTJFzU7yz4fRtsiN1s4YO-0")

    master_sheet = ss.worksheet("Master")
    updates_sheet = ss.worksheet("All Updates")
    lincoln_sheet = ss.worksheet("Lincoln Awards")

    data = master_sheet.get_all_records()
    updates = updates_sheet.get_all_records()

    student = None

    for row in data:
        row_student_id = normalize_pin(
            row.get("Student ID", "")
        )

        if row_student_id == student_id:
            student = row
            break

    if not student:
        return "Student not found"

    # Use the Overall status already calculated on the Master sheet.
    overall_status = str(student.get("Overall", "")).strip()

    if overall_status not in ("Eligible", "Ineligible"):
        overall_status = "Eligible"

    student["Overall"] = overall_status
    
    def get_comments(class_name):
        comments = []

        elective_classes = [
            "Band",
            "Choir",
            "FACS",
            "Art",
            "Wellness",
            "Orchestra",
            "Ag",
            "Strength",
            "Drama"
        ]

        for row in updates:
            row_id = normalize_pin(
                row.get("Student ID", "")
            )
            row_class = str(row.get("Class", "")).strip()
            row_status = str(row.get("Status", "")).strip()
            row_comment = str(row.get("Comment", "")).strip()
            row_teacher = str(row.get("Teacher", "")).strip()

            if row_id != student_id:
                continue

            if not row_comment:
                continue

            if row_status not in ["Warning", "Lost"]:
                continue

            comment_text = (
                f"{row_teacher}: {row_comment}"
                if row_teacher else row_comment
            )

            if row_class == class_name:
                comments.append(comment_text)

            elif class_name == "Electives" and row_class in elective_classes:
                comments.append(f"{row_class}: {comment_text}")

        return comments


    student["Advisory Comments"] = get_comments("Advisory")
    student["Math Comments"] = get_comments("Math")
    student["ELA Comments"] = get_comments("ELA")
    student["Science Comments"] = get_comments("Science")
    student["Social Studies Comments"] = get_comments("Social Studies")
    student["Electives Comments"] = get_comments("Electives")
    student["Interventions Comments"] = get_comments("Interventions")
    student["Administration Comments"] = get_comments("Administration")
    student["Counselors Comments"] = get_comments("Counselors")
    student["Aides Comments"] = get_comments("Aides")
    
        # Count this student's Lincolns
    lincoln_rows = lincoln_sheet.get_all_records()

    lincoln_total = sum(
        1
        for row in lincoln_rows
        if normalize_pin(row.get("Student ID", ""))
        == student_id
    )

    # Calculate this student's Lincoln totals by month.
    student_lincoln_rows = [
        row
        for row in lincoln_rows
        if normalize_pin(row.get("Student ID", ""))
        == student_id
    ]

    now_local = datetime.now(LOCAL_TZ)
    current_month_key = now_local.strftime("%Y-%m")

    monthly_counts = {}

    for row in student_lincoln_rows:
        timestamp_text = str(row.get("Timestamp", "")).strip()

        if not timestamp_text:
            continue

        try:
            award_time = datetime.strptime(
                timestamp_text,
                "%m/%d/%Y %I:%M %p"
            )
        except ValueError:
            # Skip any row with an unexpected timestamp format.
            continue

        month_key = award_time.strftime("%Y-%m")
        monthly_counts[month_key] = (
            monthly_counts.get(month_key, 0) + 1
        )

    current_month_total = monthly_counts.get(
        current_month_key,
        0
    )

    # Months displayed for the 2026–2027 school year.
    school_year_months = [
        ("2026-08", "August"),
        ("2026-09", "September"),
        ("2026-10", "October"),
        ("2026-11", "November"),
        ("2026-12", "December"),
        ("2027-01", "January"),
        ("2027-02", "February"),
        ("2027-03", "March"),
        ("2027-04", "April"),
        ("2027-05", "May")
    ]

    monthly_totals = [
        {
            "name": month_name,
            "total": monthly_counts.get(month_key, 0),
            "is_current": month_key == current_month_key
        }
        for month_key, month_name in school_year_months
    ]

    current_date = datetime.now(LOCAL_TZ).strftime("%B %d, %Y")

    # Get this student's 5 most recent Lincoln Awards
    student_awards = student_lincoln_rows.copy()

    # Show newest first
    student_awards.reverse()

    recent_awards = student_awards[:5]

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    for award in recent_awards:

        award_time = datetime.strptime(
            award["Timestamp"],
            "%m/%d/%Y %I:%M %p"
        )

        if award_time.date() == today:
            award["DisplayDate"] = "Today • " + award_time.strftime("%I:%M %p").lstrip("0")

        elif award_time.date() == yesterday:
            award["DisplayDate"] = "Yesterday • " + award_time.strftime("%I:%M %p").lstrip("0")

        else:
            award["DisplayDate"] = (
                award_time.strftime("%B ")
                + str(award_time.day)
                + " • "
                + award_time.strftime("%I:%M %p").lstrip("0")
            )

    html = """
    
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: Arial, sans-serif; background: #f4f4f4; padding: 16px; }
    .card { max-width: 760px; margin: 20px auto; background: white; border-radius: 18px; padding: 28px; box-shadow: 0 4px 14px rgba(0,0,0,.15); }
    .logo { display: block; width: 65%; max-width: 300px; margin: 0 auto 10px; }
    .title { text-align: center; font-size: 32px; font-weight: bold; margin-bottom: 16px; }
    h1 { text-align: center; margin: 0; font-size: 48px; font-weight: 800; line-height: 1.05; }
    .id {
        text-align: center;
        color: #555;
        font-size: 24px;
        font-weight: 600;
        margin-top: 8px;
    }

    .date {
        text-align: center;
        color: #777;
        font-size: 20px;
        margin-top: 4px;
        margin-bottom: 14px;
    }

    .overall { text-align: center; font-size: 28px; font-weight: bold; padding: 18px; border-radius: 14px; margin: 20px 0 10px; }
    .Eligible { background: #d4edda; color: #155724; }
    .NotEligible { background: #f8d7da; color: #721c24; }
    .overall-note { text-align: center; color: #666; margin-bottom: 22px; }
    .section-title { font-size: 18px; color: #666; font-weight: bold; margin: 18px 0 6px; }

    .class-row {
      display: grid;
      grid-template-columns: 70px 170px 1fr 130px;
      align-items: center;
      gap: 14px;
      margin: 12px 0;
      padding: 16px;
      border-radius: 14px;
      border: 1px solid #ddd;
    }

    .class-row.Good { background: #f2fbf3; border-color: #cfe8d2; }
    .class-row.Warning { background: #fff8e1; border-color: #f3d98b; }
    .class-row.Lost { background: #fdeaea; border-color: #f3b8b8; }

    .class-icon {
      width: 54px;
      height: 54px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
    }

    .class-row.Good .class-icon { background: #dff3e2; }
    .class-row.Warning .class-icon { background: #fff0b8; }
    .class-row.Lost .class-icon { background: #f8cccc; }

    .class-name { font-size: 24px; font-weight: 800; }
    .comment-box { border-left: 3px solid #ddd; padding-left: 12px; font-size: 15px; color: #333; }
    .comment-teacher { font-weight: 700; margin-bottom: 3px; }

    .status {
      display: inline-block;
      width: 120px;
      text-align: center;
      padding: 10px 8px;
      border-radius: 10px;
      color: white;
      font-weight: 900;
      font-size: 16px;
    }

    .status.Good { background: #2f9e44; }
    .status.Warning { background: #d99a00; }
    .status.Lost { background: #c1121f; }

.lincoln-section {
    margin-top: 35px;
    padding: 30px;
    background: linear-gradient(135deg, #fffdf4, #fff6d8);
    border: 2px solid #d4af37;
    border-radius: 18px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, .10);
}

.lincoln-top {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    align-items: center;
    gap: 30px;
}

.lincoln-left {
    display: flex;
    justify-content: center;
    align-items: center;
}

.lincoln-trophy {
    display: block;
    width: 220px;
    height: auto;
    margin: auto;
}

.lincoln-right {
    min-width: 0;
    padding-left: 30px;
    text-align: center;
    border-left: 2px solid #d8c27a;
}

.lincoln-title {
    color: #6b5200;
    font-size: 46px;
    font-weight: 900;
    margin-bottom: 22px;
}

.lincoln-summary-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 22px;
    margin-top: 26px;
}

.lincoln-summary-box {
    padding: 28px 18px;
    text-align: center;
    background: white;
    border: 2px solid #d4af37;
    border-radius: 14px;
}

.lincoln-summary-box.current-month {
    background: #fff3c4;
    border-color: #c99a00;
}

.lincoln-summary-box.grand-total {
    background: #c1121f;
    border-color: #c1121f;
}

.lincoln-summary-label {
    color: #6b5200;
    font-size: 24px;
    font-weight: 800;
    margin-bottom: 8px;
}

.lincoln-summary-number {
    color: #9a7100;
    font-size: 82px;
    line-height: 1;
    font-weight: 900;
}

.lincoln-summary-box.grand-total .lincoln-summary-label,
.lincoln-summary-box.grand-total .lincoln-summary-number {
    color: white;
}

.lincoln-monthly-section {
    margin-top: 26px;
    padding-top: 22px;
    border-top: 2px solid #d8c27a;
}

.monthly-totals-title {
    margin: 0 0 12px;
    color: #6b5200;
    text-align: center;
    font-size: 20px;
    font-weight: 900;
}

.monthly-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    overflow: hidden;
    background: rgba(255, 255, 255, .78);
    border: 1px solid #d8c27a;
    border-radius: 12px;
}

.monthly-list-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 11px 16px;
    border-bottom: 1px solid #eadfb8;
}

.monthly-list-row:nth-child(odd) {
    border-right: 1px solid #eadfb8;
}

.monthly-list-row:nth-last-child(-n + 2) {
    border-bottom: none;
}

.monthly-list-row.current {
    background: #fff3c4;
    font-weight: 800;
}

.monthly-list-name {
    color: #5f521f;
    font-size: 15px;
}

.monthly-list-number {
    color: #8b6700;
    font-size: 20px;
    font-weight: 900;
}

/* AWARD FORM */

.lincoln-button {
    padding: 14px 28px;
    color: white;
    background: #c1121f;
    border: none;
    border-radius: 10px;
    font-size: 18px;
    font-weight: 800;
    cursor: pointer;
}

.lincoln-button:hover {
    background: #9f1019;
}

.lincoln-form-section {
    margin-top: 16px;
    padding: 22px;
    background: white;
    border: 1px solid #ddd;
    border-radius: 14px;
    box-shadow: 0 3px 10px rgba(0, 0, 0, .08);
}

.lincoln-form-title {
    margin-bottom: 16px;
    color: #555;
    text-align: center;
    font-size: 22px;
    font-weight: 800;
}

.lincoln-form-row {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr auto;
    align-items: center;
    gap: 14px;
}

.lincoln-input {
    box-sizing: border-box;
    width: 100%;
    height: 52px;
    padding: 10px 14px;
    background: white;
    border: 1px solid #999;
    border-radius: 9px;
    font-size: 16px;
}

.lincoln-form-row .lincoln-button {
    height: 52px;
    padding: 10px 24px;
    white-space: nowrap;
}

.award-message {
    margin-bottom: 14px;
    padding: 10px 14px;
    border-radius: 8px;
    text-align: center;
    font-weight: bold;
}

.award-error {
    color: #b00020;
    background: #fdeaea;
    border: 1px solid #efb2b7;
}

.award-success {
    color: #1b5e20;
    background: #e7f7ea;
    border: 1px solid #b8dfbf;
}

/* RECENT AWARDS */

.lincoln-history-section {
    margin-top: 20px;
    padding: 22px;
    background: white;
    border-radius: 14px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, .08);
}

.lincoln-history-title {
    margin-bottom: 16px;
    color: #333;
    text-align: center;
    font-size: 20px;
    font-weight: bold;
}

.lincoln-history-item {
    padding: 12px 4px;
    border-bottom: 1px solid #e5e5e5;
}

.lincoln-history-item:last-child {
    border-bottom: none;
}

.lincoln-history-date {
    margin-bottom: 4px;
    color: #777;
    font-size: 13px;
}

.lincoln-history-reason {
    color: #333;
    font-size: 16px;
    font-weight: bold;
}

.lincoln-history-empty {
    padding: 12px;
    color: #777;
    text-align: center;
}

/* MOBILE */

@media (max-width:700px) {

    .lincoln-top {
        grid-template-columns: 1fr;
        gap: 14px;
    }

    .lincoln-left {
        display: flex;
        justify-content: center;
    }

    .lincoln-trophy {
        width: 170px;
    }

    .lincoln-right {
        width: 100%;
        padding-top: 18px;
        padding-left: 0;
        border-top: 2px solid #d8c27a;
        border-left: none;
    }

    .lincoln-monthly-section {
        margin-top: 20px;
        padding-top: 18px;
    }

    .monthly-list {
        grid-template-columns: repeat(2, 1fr);
    }

    .lincoln-form-row {
        grid-template-columns: 1fr;
    }

    .lincoln-form-row .lincoln-button {
        width: 100%;
    }
}

.footer {
    margin-top: 20px;
    text-align: center;
    font-size: 13px;
    color: #777;
}
  </style>
</head>

<body>
  <div class="card">
    <img src="{{ url_for('static', filename='lincoln_logo.png') }}" alt="Lincoln Junior High Logo" class="logo">

    <div class="title">Promise Card Status</div>

    <h1>{{ student['First Name'] }} {{ student['Last Name'] }}</h1>

    <div class="id">
        Grade {{ student['Grade'] }}
    </div>

    <div class="date">{{ current_date }}</div>

    <div class="overall {{ 'Eligible' if student['Overall'] == 'Eligible' else 'NotEligible' }}">
      {{ '✅ ELIGIBLE' if student['Overall'] == 'Eligible' else '❌ NOT ELIGIBLE' }}
    </div>

    <div class="overall-note">
      {{ "You’re good to go! Keep up the great work." if student['Overall'] == 'Eligible' else "See class details below." }}
    </div>

    <div class="section-title">Class Breakdown</div>

    {% set icons = {
      'Advisory': '👥',
      'Math': '🧮',
      'ELA': '📖',
      'Science': '⚗️',
      'Social Studies': '🌎',
      'Electives': '🎨',
      'Administration': '🏫',
      'Counselors': '💬',
      'Interventions': '📘',
      'Aides': '🤝'
    } %}

    <!-- CORE CLASSES ALWAYS SHOW -->

    <!-- CORE CLASSES ALWAYS SHOW -->

{% for class_name, status, comments in [
  ('Advisory', student['Advisory'], student['Advisory Comments']),
  ('Math', student['Math'], student['Math Comments']),
  ('ELA', student['ELA'], student['ELA Comments']),
  ('Science', student['Science'], student['Science Comments']),
  ('Social Studies', student['Social Studies'], student['Social Studies Comments']),
  ('Electives', student['Electives'], student['Electives Comments'])
] %}

  <div class="class-row {{ status if status else 'Good' }}">

    <div class="class-icon">
      {{ icons[class_name] }}
    </div>

    <div class="class-name">
      {{ class_name }}
    </div>

    <div class="comment-box">

      {% if comments %}

        {% for comment in comments %}

          {% if ':' in comment %}
            <div class="comment-teacher">
              {{ comment.split(':')[0] }}
            </div>

            <div>
              {{ comment.split(':', 1)[1] }}
            </div>

          {% else %}
            <div>{{ comment }}</div>
          {% endif %}

        {% endfor %}

      {% else %}

        {% if status == "Good" or not status %}
          <div>No concerns reported.</div>
        {% else %}
          <div style="color:#777; font-style:italic;">
            No teacher comment provided.
          </div>
        {% endif %}

      {% endif %}

    </div>

    <div>
      <span class="status {{ status if status else 'Good' }}">
        {{ (status if status else 'Good')|upper }}
      </span>
    </div>

  </div>

{% endfor %}

<!-- SUPPORT / ADMIN ONLY SHOW WHEN LOST -->

{% set admin_rows = [
  ('Administration', student['Administration'], student['Administration Comments']),
  ('Counselors', student['Counselors'], student['Counselors Comments']),
  ('Interventions', student['Interventions'], student['Interventions Comments']),
  ('Aides', student['Aides'], student['Aides Comments'])
] %}

{% set has_admin_issues = false %}

{% for class_name, status, comments in admin_rows %}
  {% if status == "Lost" %}
    {% set has_admin_issues = true %}
  {% endif %}
{% endfor %}

{% if has_admin_issues %}
  <div class="section-title">Additional Concerns</div>
{% endif %}

{% for class_name, status, comments in admin_rows %}

  {% if (class_name == "Interventions" and status)
        or (class_name != "Interventions" and status == "Lost") %}

    <div class="class-row {{ status }}">

      <div class="class-icon">
        {{ icons[class_name] }}
      </div>

      <div class="class-name">
        {{ class_name }}
      </div>

      <div class="comment-box">

        {% if comments %}

          {% for comment in comments %}

            {% if ':' in comment %}
              <div class="comment-teacher">
                {{ comment.split(':')[0] }}
              </div>

              <div>
                {{ comment.split(':', 1)[1] }}
              </div>

            {% else %}
              <div>{{ comment }}</div>
            {% endif %}

          {% endfor %}

        {% else %}

          {% if status == "Good" or not status %}
            <div>No concerns reported.</div>
          {% else %}
            <div style="color:#777; font-style:italic;">
              No teacher comment provided.
            </div>
          {% endif %}

        {% endif %}

      </div>

      <div>
        <span class="status {{ status }}">
          {{ status|upper }}
        </span>
      </div>

    </div>

  {% endif %}

{% endfor %}

<div class="lincoln-section">

    <div class="lincoln-top">

        <div class="lincoln-left">
            <img
                src="{{ url_for('static', filename='lincoln_trophy.png') }}"
                class="lincoln-trophy"
                alt="Lincoln Trophy">
        </div>

        <div class="lincoln-right">

            <div class="lincoln-title">
                Lincoln Awards
            </div>

            <div class="lincoln-summary-grid">

                <div class="lincoln-summary-box current-month">
                    <div class="lincoln-summary-label">
                        This Month
                    </div>

                    <div class="lincoln-summary-number">
                        {{ current_month_total }}
                    </div>
                </div>

                <div class="lincoln-summary-box grand-total">
                    <div class="lincoln-summary-label">
                        Grand Total
                    </div>

                    <div class="lincoln-summary-number">
                        {{ lincoln_total }}
                    </div>
                </div>

            </div>

        </div>

    </div>

    <div class="lincoln-monthly-section">

        <div class="monthly-totals-title">
            Monthly Breakdown
        </div>

        <div class="monthly-list">

            {% for month in monthly_totals %}

                <div class="monthly-list-row {{ 'current' if month.is_current else '' }}">

                    <span class="monthly-list-name">
                        {{ month.name }}
                    </span>

                    <span class="monthly-list-number">
                        {{ month.total }}
                    </span>

                </div>

            {% endfor %}

        </div>

    </div>

</div>

<div class="lincoln-form-section">

    <div class="lincoln-form-title">
        Award a Lincoln
    </div>
    {% if award_error %}
    <div class="award-message award-error">
        {{ award_error }}
    </div>
    {% endif %}

    {% if award_success %}
    <div class="award-message award-success">
        {{ award_success }}
    </div>
    {% endif %}

    <form action="/award_lincoln" method="POST">

        <input
            type="hidden"
            name="student_id"
            value="{{ student_id }}">

        <div class="lincoln-form-row">

            <select
            name="reason"
            required
            class="lincoln-input">

            <option value="">Select a reason...</option>
            <option>Leadership</option>
            <option>Kindness</option>
            <option>Citizenship</option>
            <option>Respect</option>
            <option>Responsibility</option>
            <option>Perseverance</option>
            <option>Academic Excellence</option>
            <option>Helping Others</option>
            <option>Positive Attitude</option>
            <option>Other</option>

        </select>

        <input
            type="text"
            name="awarding_staff"
            placeholder="Awarded By"
            required
            class="lincoln-input">

        <input
            type="password"
            name="password"
            placeholder="Staff Password"
            required
            class="lincoln-input">

        <button
            class="lincoln-button"
            type="submit">
            🏆 Award Lincoln
        </button>

    </div>

    </form>

</div>

<div class="lincoln-history-section">

    <div class="lincoln-history-title">
        🏆 Recent Lincoln Awards
    </div>

    {% if recent_awards %}

        {% for award in recent_awards %}

            <div class="lincoln-history-item">

                <div class="lincoln-history-date">
                    {{ award["DisplayDate"] }}
                </div>

                <div class="lincoln-history-reason">
                    {{ award["Reason"] }}
                </div>

            </div>

        {% endfor %}

    {% else %}

        <div class="lincoln-history-empty">
            No Lincoln Awards yet.
        </div>

    {% endif %}

</div>

<div class="footer">
    Promise Card status updates live from the school tracker.
</div>

</div>

</body>
</html>
"""

    return render_template_string(
        html,
        student=student,
        current_date=current_date,
        lincoln_total=lincoln_total,
        current_month_total=current_month_total,
        monthly_totals=monthly_totals,
        student_id=student_id,
        award_error=award_error,
        award_success=award_success,
        recent_awards=recent_awards
    )


def get_lincoln_total(student_id):
    try:
        rows = lincoln_sheet.get_all_records()
        target_id = str(student_id).strip()

        return sum(
            1
            for row in rows
            if str(row.get("Student ID", "")).strip() == target_id
        )

    except Exception as e:
        print("Lincoln total error:", repr(e))
        return 0

@app.route("/skills")
def skills():
    student_id = request.args.get("id", "").strip()

    if not student_id:
        return "Missing student ID. Add ?id=1234 to the URL."

    sheet = client.open(SKILL_SHEET_NAME)

    evidence_ws = sheet.worksheet(SKILL_FORM_TAB)
    requirements_ws = sheet.worksheet(SKILL_REQUIREMENTS_TAB)

    evidence_data = evidence_ws.get_all_records()
    requirements = requirements_ws.get_all_records()

    student_rows = [
        row for row in evidence_data
        if str(row.get("Student ID", "")).strip() == student_id
    ]

    if not student_rows:
        return "No skill evidence found for this student."

    first_name = student_rows[0].get("First Name", "")
    last_name = student_rows[0].get("Last Name", "")
    student_name = f"{first_name} {last_name}".strip()

    html = f"""
    <html>
    <head>
        <title>Skill Tracker</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #f4f4f4;
                padding: 30px;
            }}

            .container {{
                max-width: 900px;
                margin: auto;
            }}

            .header {{
                background: #b00020;
                color: white;
                padding: 20px;
                border-radius: 12px;
                margin-bottom: 20px;
                text-align: center;
            }}

            .header h1 {{
                font-size: 42px;
                margin-bottom: 10px;
            }}

            .header h2 {{
                font-size: 54px;
                margin-top: 0;
            }}

            .card {{
                background: white;
                padding: 20px;
                border-radius: 12px;
                margin-bottom: 20px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.15);
            }}

            .evidence {{
                background: #fafafa;
                padding: 12px;
                margin-top: 12px;
                border-left: 5px solid #999;
                border-radius: 6px;
            }}

            .rating {{
                font-weight: bold;
                padding: 4px 8px;
                border-radius: 6px;
                display: inline-block;
                margin-bottom: 8px;
            }}

            .rating {{
                font-weight: bold;
                padding: 4px 8px;
                border-radius: 6px;
                display: inline-block;
                margin-bottom: 8px;
            }}

            .not-reviewed {{ background: #fff3cd; }}
            .not-yet {{ background: #f8d7da; }}
            .meets-criteria {{ background: #d4edda; }}
            .developing {{ background: #fff0b3; }}
            .mastered {{ background: #c8f7c5; }}
            .advanced {{ background: #cce5ff; }}

            .skill-summary {{
                padding: 18px;
                border-radius: 12px;
                margin-bottom: 16px;
                color: #111;
            }}

            .summary-red {{
                background: #f8d7da;
                border-left: 8px solid #c1121f;
            }}

            .summary-yellow {{
                background: #fff3cd;
                border-left: 8px solid #d99a00;
            }}

            .summary-green {{
                background: #d4edda;
                border-left: 8px solid #2f9e44;
            }}

            .progress {{
                font-size: 18px;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
    <div class="container">

        <div class="header">
            <h1>Skill Tracker</h1>
            <h2>{student_name}</h2>
        </div>
    """

    for req in requirements:
        skill_name = req.get("Skill", "")
        needed = int(req.get("Evidence Needed", 3))

        matching_entries = [
            row for row in student_rows
            if row.get("Skill", "") == skill_name
        ]

        mastered_count = sum(
            1 for row in matching_entries
            if str(row.get("Rating", "")).strip() == "Meets Criteria"
        )

        if mastered_count >= 3:
            status = "Mastery"
        elif mastered_count == 2:
            status = "Approaching"
        elif mastered_count == 1:
            status = "Developing"
        else:
            status = "Not Yet"

        if mastered_count >= 3:
            summary_class = "summary-green"
        elif mastered_count >= 1:
            summary_class = "summary-yellow"
        else:
            summary_class = "summary-red"

        html += f"""
        <div class="card">

            <div class="skill-summary {summary_class}">
                <h2>{skill_name}</h2>
                <p class="progress">Progress: {mastered_count} / {needed}</p>
                <p><strong>Status:</strong> {status}</p>
             </div>
        """

        if not matching_entries:
            html += "<p>No evidence submitted yet.</p>"

        for entry in matching_entries:
            rating = entry.get("Rating", "") or "Not Reviewed"
            comment = entry.get("Teacher Comment", "")
            evidence = entry.get("Evidence", "")
            evidence_info = entry.get("Evidence Information", "")
            timestamp = entry.get("Timestamp", "")
            reviewing_teacher = entry.get("Reviewing Teacher", "")

            rating_class = "not-reviewed"
            if rating == "Not Yet":
                rating_class = "not-yet"
            elif rating == "Meets Criteria":
                rating_class = "meets-criteria"

            html += f"""
            <div class="evidence">

                <span class="rating {rating_class}">{rating}</span><br>

                <strong>Date:</strong> {timestamp}<br><br>

                <strong>Evidence:</strong><br>
                {evidence}<br><br>

                <strong>Evidence Information:</strong><br>
                {evidence_info}<br><br>

                <strong>Reviewing Teacher:</strong><br>
                {reviewing_teacher}<br><br>

                <strong>Teacher Comment:</strong><br>
                {comment}
            </div>
            """

        html += "</div>"

    html += """
    </div>
    </body>
    </html>
    """

    return html

@app.route("/lookup", methods=["POST"])
def lookup():
    student_id = request.form.get("student_id")

    return redirect(f"/promise?id={student_id}")
    
@app.route("/skills-review")
def skills_review():
    teacher_filter = request.args.get("teacher", "").strip()

    sheet = client.open(SKILL_SHEET_NAME)
    evidence_ws = sheet.worksheet(SKILL_FORM_TAB)

    rows = evidence_ws.get_all_records()

    requirements_ws = sheet.worksheet(SKILL_REQUIREMENTS_TAB)
    requirements = requirements_ws.get_all_records()

    requirements_lookup = {}

    for req in requirements:
        skill = str(req.get("Skill", "")).strip()
        needed = int(req.get("Evidence Needed", 3))
        requirements_lookup[skill] = needed

    unreviewed = []
    for i, row in enumerate(rows, start=2):
        rating_blank = not str(row.get("Rating", "")).strip()
        reviewing_teacher = str(row.get("Reviewing Teacher", "")).strip()

        teacher_matches = True
        if teacher_filter:
            teacher_matches = reviewing_teacher == teacher_filter

        if rating_blank and teacher_matches:
            row["Sheet Row"] = i
            unreviewed.append(row)

    html = """
    <html>
    <head>
        <title>Skill Review</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f4f4f4;
                padding: 30px;
            }
            .container {
                max-width: 900px;
                margin: auto;
            }
            .card {
                background: white;
                padding: 20px;
                border-radius: 12px;
                margin-bottom: 20px;
                box-shadow: 0 2px 6px rgba(0,0,0,0.15);
            }
            textarea {
                width: 100%;
                height: 70px;
                margin-top: 8px;
            }
            select, input, button {
                padding: 8px;
                font-size: 16px;
                margin-top: 8px;
            }
            button {
                background: #b00020;
                color: white;
                border: none;
                border-radius: 6px;
                cursor: pointer;
            }
            .evidence {
                background: #fafafa;
                padding: 12px;
                border-left: 5px solid #999;
                margin-top: 10px;
            }
        </style>
    </head>
    <body>
    <div class="container">
        <h1>Skill Evidence Review</h1>
    """

    if teacher_filter:
        html += f"<h2>Reviewing Teacher: {teacher_filter}</h2>"

    if not unreviewed:
        html += "<p>No unreviewed submissions.</p>"

    for row in unreviewed:
        student_id = str(row.get("Student ID", "")).strip()
        skill_name = str(row.get("Skill", "")).strip()

        matching_history = [
            r for r in rows
            if str(r.get("Student ID", "")).strip() == student_id
            and str(r.get("Skill", "")).strip() == skill_name
        ]

        approved_count = sum(
            1 for r in matching_history
            if str(r.get("Rating", "")).strip() == "Meets Criteria"
        )

        total_submissions = len(matching_history)

        needed = requirements_lookup.get(skill_name, 3)

        if approved_count >= 3:
            current_status = "Mastery"
        elif approved_count == 2:
            current_status = "Approaching"
        elif approved_count == 1:
            current_status = "Developing"
        else:
            current_status = "Not Yet"
        html += f"""
        <div class="card">
            <h2>{row.get("First Name", "")} {row.get("Last Name", "")}</h2>

            <p><strong>Student ID:</strong> {row.get("Student ID", "")}</p>
            <p><strong>Skill:</strong> {row.get("Skill", "")}</p>
            <p><strong>Reviewing Teacher:</strong> {row.get("Reviewing Teacher", "")}</p>
            <p><strong>Approved Evidence:</strong> {approved_count} / {needed}</p>
            <p><strong>Total Submissions:</strong> {total_submissions}</p>
            <p><strong>Current Status:</strong> {current_status}</p>

            <div class="evidence">
                <strong>Evidence:</strong><br>
                {row.get("Evidence", "")}<br><br>

                <strong>Evidence Information:</strong><br>
                {row.get("Evidence Information", "")}
            </div>

            <form action="/rate-evidence" method="post">
                <input type="hidden" name="sheet_row" value="{row.get("Sheet Row")}">

                <label><strong>Rating:</strong></label><br>
                <select name="rating" required>
                    <option value="">Choose rating</option>
                    <option value="Not Yet">Not Yet</option>
                    <option value="Meets Criteria">Meets Criteria</option>
                </select>

                <br><br>

                <label><strong>Teacher Comment:</strong></label><br>
                <textarea name="teacher_comment"></textarea>

                <br>

                <label><strong>Reviewed By:</strong></label><br>
                <input type="text" name="reviewed_by" placeholder="Teacher name" required>

                <br><br>

                <button type="submit">Save Rating</button>
            </form>
        </div>
        """

    html += """
    </div>
    </body>
    </html>
    """

    return html

@app.route("/rate-evidence", methods=["POST"])
def rate_evidence():
    sheet_row = int(request.form.get("sheet_row"))
    rating = request.form.get("rating", "")
    teacher_comment = request.form.get("teacher_comment", "")
    reviewed_by = request.form.get("reviewed_by", "")
    reviewed_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet = client.open(SKILL_SHEET_NAME)
    evidence_ws = sheet.worksheet(SKILL_FORM_TAB)

    headers = evidence_ws.row_values(1)

    rating_col = headers.index("Rating") + 1
    comment_col = headers.index("Teacher Comment") + 1
    reviewed_by_col = headers.index("Reviewed By") + 1
    reviewed_date_col = headers.index("Reviewed Date") + 1

    evidence_ws.update_cell(sheet_row, rating_col, rating)
    evidence_ws.update_cell(sheet_row, comment_col, teacher_comment)
    evidence_ws.update_cell(sheet_row, reviewed_by_col, reviewed_by)
    evidence_ws.update_cell(sheet_row, reviewed_date_col, reviewed_date)

    return redirect("/skills-review")
@app.route("/award_lincoln", methods=["POST"])
def award_lincoln():

    student_id = normalize_pin(
        request.form.get("student_id", "")
    )
    reason = request.form.get("reason", "").strip()
    awarding_staff = request.form.get(
        "awarding_staff",
        ""
    ).strip()
    password = request.form.get("password", "")

    if password != STAFF_PASSWORD:
        return redirect(
            url_for(
                "promise",
                id=student_id,
                award_error="Incorrect teacher password."
            )
        )

    if not student_id:
        return "Missing student ID.", 400

    if not reason:
        return redirect(
            url_for(
                "promise",
                id=student_id,
                award_error="Please select a reason."
            )
        )

    if not awarding_staff:
        return redirect(
            url_for(
                "promise",
                id=student_id,
                award_error="Please enter your name."
            )
        )

    ss = client.open_by_key("1JP4mscjAyY73ZEFlrStxaTJFzU7yz4fRtsiN1s4YO-0")
    lincoln_sheet = ss.worksheet("Lincoln Awards")

    master_sheet = ss.worksheet("Master")
    student_rows = master_sheet.get_all_records()

    student = next(
        (
            row
            for row in student_rows
            if normalize_pin(row.get("Student ID", ""))
            == student_id
        ),
        None
    )

    if not student:
        return redirect(
            url_for(
                "promise",
                id=student_id,
                award_error="Student could not be found."
            )
        )

    timestamp = datetime.now().strftime("%m/%d/%Y %I:%M %p")

    lincoln_sheet.append_row([
        timestamp,
        student_id,
        student.get("First Name", ""),
        student.get("Last Name", ""),
        awarding_staff,
        reason,
        1
    ])

    return redirect(
        url_for(
            "promise",
            id=student_id,
            award_success="Lincoln awarded successfully!"
        )
    )
if __name__ == '__main__':
    app.run(debug=True)
