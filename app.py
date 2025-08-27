import os
import json
import gspread
from flask import Flask, render_template, request, redirect, session, url_for
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ---- Time helpers ----
LOCAL_TZ = ZoneInfo("America/Indiana/Indianapolis")
def now_str():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")

app = Flask(__name__)

# ---- Secrets & config ----
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-override")
PASSWORD = os.environ.get("TEACHER_DASHBOARD_PASSWORD", "dev-password")
HALL_LIMIT = int(os.environ.get("HALL_LIMIT", 10))
MAX_QUARTER_PASSES = int(os.environ.get("MAX_QUARTER_PASSES", 18))

# ---- Google Sheets auth (env-first) ----
google_creds_json = os.environ.get("GOOGLE_CREDS_JSON")
if google_creds_json:
    google_creds = json.loads(google_creds_json)
else:
    with open("service_account.json") as f:
        google_creds = json.load(f)

client = gspread.service_account_from_dict(google_creds)
SHEET_NAME = os.environ.get("SHEET_NAME", "HallPassTracker")
sheet = client.open(SHEET_NAME).sheet1

# ---- Choices ----
TEACHERS = [
    "R. Ahlrich","B. Ames","D. Andrews","B. Barron","J. Bird","B. Brennan","T. Brennan","B. Breyette",
    "C. Caine","H. Carbaugh-Keefe","B. Carroll","L. Carroll","C. Carver","M. Chavez","J. Clark","L. Day","A. De Lucenay",
    "D. Derifield","J. Dreibelbis","B. Garrity","K. Garrity","S. Garrity","N. Hart","R. Heeren","S. Houston",
    "C. Hughes","J. Jimenez","J. Kallenberg","B. Langowski","G. Miller","A. Schmeltz",
    "P. Skirvin","A. Smith","B. Stiles","G. Stout","J. Taylor","S. Taylor","S. Vanlue"
]
REASONS = ["Restroom","Water","Office","Locker","Nurse","Other"]
PERIODS = ["Advisory/STORM","Period 2","Period 3","Period 4","Period 5","Period 6","Period 7"]

# ---- Header hardening ----
EXPECTED_HEADERS = ["First Name","Last Name","Period","Teacher","Reason","Time Out","Time In"]
def read_passes():
    """
    Read rows using a fixed header schema and coerce any odd rows into dicts.
    This survives duplicate/blank headers and short rows.
    """
    try:
        # Preferred: dicts with our expected schema regardless of the real header row
        recs = sheet.get_all_records(
            expected_headers=EXPECTED_HEADERS,
            head=1,
            default_blank=""
        )
        # Safety: ensure each item is a dict (coerce lists if Google returns values)
        fixed = []
        for r in recs:
            if isinstance(r, dict):
                fixed.append(r)
            elif isinstance(r, list):
                fixed.append({
                    h: (r[i] if i < len(r) else "")
                    for i, h in enumerate(EXPECTED_HEADERS)
                })
            else:
                # unknown type -> skip
                continue
        return fixed
    except Exception:
        # Absolute fallback: work from raw values
        rows = sheet.get_all_values()
        if not rows:
            return []
        fixed = []
        for row in rows[1:]:
            fixed.append({
                h: (row[i] if i < len(row) else "")
                for i, h in enumerate(EXPECTED_HEADERS)
            })
        return fixed

def _header_index(col_name: str) -> int:
    try:
        row1 = sheet.row_values(1)
    except Exception:
        row1 = []
    norm = [(h or "").strip().lower() for h in row1]
    key = col_name.strip().lower()
    if key in norm:
        return norm.index(key) + 1
    # fall back to our expected schema
    return EXPECTED_HEADERS.index(col_name) + 1

def write_pass(entry):
    sheet.append_row([
        entry['First Name'], entry['Last Name'], entry['Period'], entry['Teacher'],
        entry['Reason'], entry['Time Out'], entry['Time In']
    ])

def get_current_quarter():
    today = date.today()
    year = today.year
    quarters = {
        "Q1": (date(year, 7, 1), date(year,10,31)),
        "Q2": (date(year,11, 1), date(year+1,1,15)),
        "Q3": (date(year, 1,16), date(year, 3,31)),
        "Q4": (date(year, 4, 1), date(year, 6,30)),
    }
    for _, (start, end) in quarters.items():
        if start <= today <= end or (start.month > end.month and (today >= start or today <= end)):
            return _
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

    first_lc = first.strip().lower()
    last_lc  = last.strip().lower()

    for row in passes:
        # Coerce any non-dict rows just in case
        if isinstance(row, list):
            row = {EXPECTED_HEADERS[i]: (row[i] if i < len(row) else "")
                   for i in range(len(EXPECTED_HEADERS))}
        if not isinstance(row, dict):
            continue

        fn = str(row.get('First Name', '')).strip().lower()
        ln = str(row.get('Last Name', '')).strip().lower()
        if fn != first_lc or ln != last_lc:
            continue

        time_out_str = str(row.get('Time Out', '')).strip()
        if not time_out_str:
            continue
        try:
            time_out = datetime.strptime(time_out_str, "%Y-%m-%d %H:%M:%S").date()
        except ValueError:
            # Skip rows with unexpected date formats
            continue

        # Handle Q2 spanning New Year
        if start and end and (start <= end and start <= time_out <= end or
                              start and end and start > end and (time_out >= start or time_out <= end)):
            count += 1

    return count

def auto_close_stale_passes(max_minutes: int = 30) -> int:
    try:
        rows = sheet.get_all_values()
        if not rows:
            return 0
        timeout_idx = _header_index("Time Out") - 1
        timein_idx  = _header_index("Time In")  - 1
        closed = 0
        for r, row in enumerate(rows[1:], start=2):
            if len(row) <= max(timeout_idx, timein_idx):
                continue
            time_out = (row[timeout_idx] or "").strip()
            time_in  = (row[timein_idx]  or "").strip()
            if time_out and time_in == "":
                try:
                    out_dt = datetime.strptime(time_out, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                now_naive = datetime.now(LOCAL_TZ).replace(tzinfo=None)
                if now_naive - out_dt > timedelta(minutes=max_minutes):
                    sheet.update_cell(r, timein_idx + 1, now_str())
                    closed += 1
        return closed
    except Exception as e:
        print(f"auto_close_stale_passes error: {e}")
        return 0

# ---- ROUTES ----
@app.route("/", methods=["GET"])
def home():
    auto_close_stale_passes()  # keep this if you want auto-close behavior on page load
    name = (request.args.get("name") or "").strip()
    used_passes = None
    if name and " " in name:
        first, last = name.split(" ", 1)
        used_passes = passes_this_quarter(first, last)
        name = f"{first} {last}"
    else:
        name = None
    return render_template(
        "index.html",
        name=name,
        used_passes=used_passes,
        teachers=TEACHERS,
        reasons=REASONS,
        periods=PERIODS,
    )

@app.route('/signout', methods=['POST'])
def signout():
    first_name = request.form['first_name']
    last_name  = request.form['last_name']
    period     = request.form['period']
    teacher    = request.form['teacher']
    reason     = request.form['reason']
    other_reason = request.form.get('other_reason', '').strip()
    final_reason = other_reason if reason == "Other" and other_reason else reason
    time_out = now_str()

    passes = read_passes()
    currently_out = [p for p in passes if not p.get('Time In','').strip()]
    if len(currently_out) >= HALL_LIMIT:
        return render_template("error.html", message="The maximum number of students are already out. Please wait.")
    if passes_this_quarter(first_name, last_name) >= MAX_QUARTER_PASSES:
        return render_template("error.html", message="You have used all 18 passes for this quarter.")

    entry = {
        'First Name': first_name, 'Last Name': last_name, 'Period': period,
        'Teacher': teacher, 'Reason': final_reason,
        'Time Out': time_out, 'Time In': ''
    }
    write_pass(entry)
    return redirect(url_for('home', name=f"{first_name} {last_name}"))

@app.route("/signin", methods=["POST"])
def signin():
    full_name = (request.form.get("full_name") or request.form.get("name") or "").strip()
    full_name = " ".join(full_name.split())
    if not full_name or " " not in full_name:
        return "First and last name are required", 400
    parts = full_name.split(" ")
    first_name, last_name = parts[0], " ".join(parts[1:])

    records = read_passes()
    time_in_col = _header_index("Time In")

    target_first = first_name.strip().lower()
    target_last  = last_name.strip().lower()

    for idx, row in enumerate(records, start=2):
        row_first = str(row.get("First Name", "")).strip().lower()
        row_last  = str(row.get("Last Name", "")).strip().lower()
        time_in_val = str(row.get("Time In", "")).strip()
        if row_first == target_first and row_last == target_last and (time_in_val == "" or time_in_val.lower() in ("none","nan")):
            sheet.update_cell(idx, time_in_col, now_str())
            used_passes = passes_this_quarter(first_name, last_name)
            return render_template("signin_success.html",
                                   first_name=first_name, last_name=last_name, used_passes=used_passes)
    return "Student not found or already signed in", 404

@app.route('/login', methods=['GET','POST'])
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
    currently_out = [p for p in passes if not p.get('Time In','').strip()]
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
        name = f"{entry.get('First Name','').strip()} {entry.get('Last Name','').strip()}".strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts

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

if __name__ == "__main__":
    app.run(debug=True)
