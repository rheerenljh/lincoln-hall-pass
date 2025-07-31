import gspread
from oauth2client.service_account import ServiceAccountCredentials
from flask import Flask, render_template, request, redirect, session, url_for
import csv
from datetime import datetime, date
import os

app = Flask(__name__)
app.secret_key = 'lincoln20252026'

# ---------- CONFIG ----------
CSV_FILE = 'hall_passes.csv'
HALL_LIMIT = 5
MAX_QUARTER_PASSES = 18
PASSWORD = 'lincoln20252026'

# ---------- GOOGLE SHEETS ----------
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
import json
import os

google_creds = json.loads(os.environ["GOOGLE_CREDS_JSON"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(google_creds, scope)
client = gspread.authorize(creds)
SHEET_NAME = "HallPassTracker"
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

PERIODS = [
    "Advisory/STORM", "Period 2", "Period 3", "Period 4", "Period 5", "Period 6", "Period 7"
]

# ---------- HELPERS ----------
def read_passes():
    if not os.path.exists(CSV_FILE):
        return []
    with open(CSV_FILE, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        return [row for row in reader if any(row.values())]  # Skip blank rows

def write_pass(entry):
    fieldnames = ['First Name', 'Last Name', 'Period', 'Teacher', 'Reason', 'Time Out', 'Time In']
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, 'a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(entry)

    # Also write to Google Sheets
    sheet.append_row([
        entry['First Name'], entry['Last Name'], entry['Period'], entry['Teacher'],
        entry['Reason'], entry['Time Out'], entry['Time In']
    ])

def get_current_quarter():
    today = date.today()
    year = today.year

    quarters = {
        "Q1": (date(year, 7, 1), date(year, 10, 31)),
        "Q2": (date(year, 11, 1), date(year + 1, 1, 15)),  # ✅ Jan 15 is next year
        "Q3": (date(year, 1, 16), date(year, 3, 31)),
        "Q4": (date(year, 4, 1), date(year, 6, 30)),
    }

    for q, (start, end) in quarters.items():
        # 🧠 Handle year wrap for Q2 (Nov–Jan)
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
    print(f"📅 Current quarter: {quarter} ({start} to {end})")

    for row in passes:
        fn = row.get('First Name', '').strip().lower()
        ln = row.get('Last Name', '').strip().lower()

        if fn == first.strip().lower() and ln == last.strip().lower():
            time_out_str = row.get('Time Out', '').strip()
            if not time_out_str:
                print(f"⏳ Skipping row with empty Time Out: {row}")
                continue
            try:
                time_out = datetime.strptime(time_out_str, "%Y-%m-%d %H:%M:%S").date()
                if start <= time_out <= end or (start > end and (time_out >= start or time_out <= end)):
                    count += 1
                    print(f"✅ Counted pass for {first} {last} on {time_out}")
                else:
                    print(f"🚫 Outside quarter: {time_out}")
            except Exception as e:
                print(f"⚠️ Error parsing time for row: {row} — {e}")
    print(f"🔢 Total passes for {first} {last} this quarter: {count}")
    return count

# ---------- ROUTES ----------
@app.route('/')
def home():
    name = request.args.get('name')
    used_passes = None

    if name:
        try:
            first, last = name.strip().split(' ', 1)
            used_passes = passes_this_quarter(first, last)
            name = f"{first} {last}"  # Ensure name is clean and consistent
        except ValueError:
            name = None

    return render_template(
        'index.html',
        name=name,
        used_passes=used_passes,
        teachers=TEACHERS,
        reasons=REASONS,
        periods=PERIODS
    )

@app.route('/signout', methods=['POST'])
def signout():
    first_name = request.form['first_name']
    last_name = request.form['last_name']
    period = request.form['period']
    teacher = request.form['teacher']
    reason = request.form['reason']
    other_reason = request.form.get('other_reason', '').strip()
    final_reason = other_reason if reason == "Other" and other_reason else reason
    time_out = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Enforce hall limit
    passes = read_passes()
    currently_out = [p for p in passes if not p['Time In'].strip()]
    if len(currently_out) >= HALL_LIMIT:
        return render_template("error.html", message="The maximum number of students are already out. Please wait.")

    # Enforce per-quarter pass limit
    if passes_this_quarter(first_name, last_name) >= MAX_QUARTER_PASSES:
        return render_template("error.html", message="You have used all 18 passes for this quarter.")

    # Save
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

@app.route('/signin', methods=['POST'])
def signin():
    full_name = request.form['name'].strip()
    try:
        first, last = full_name.split(' ', 1)
    except ValueError:
        return render_template("error.html", message="Please enter your full name as 'First Last'.")

    passes = read_passes()
    updated = False

    for row in reversed(passes):
        if (row.get('First Name', '').strip().lower() == first.lower() and
            row.get('Last Name', '').strip().lower() == last.lower() and
            row.get('Time In', '').strip() == ''):
            row['Time In'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            updated = True
            break

    if updated:
        # Clean passes list before writing back
        cleaned_passes = []
        for row in passes:
            if None in row:
                continue  # skip malformed rows
            cleaned_row = {k: v for k, v in row.items() if k is not None}
            cleaned_passes.append(cleaned_row)

        # Write to CSV
        with open(CSV_FILE, 'w', newline='') as csvfile:
            fieldnames = ['First Name', 'Last Name', 'Period', 'Teacher', 'Reason', 'Time Out', 'Time In']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(cleaned_passes)

        # Write to Google Sheet
        sheet.clear()
        sheet.append_row(fieldnames)
        for row in cleaned_passes:
            sheet.append_row([row.get(f, '') for f in fieldnames])

        return redirect(url_for('home', name=f"{first} {last}"))
    else:
        return render_template("error.html", message="No active pass found. Did you sign out first?")

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
    passes = read_passes()
    counts = get_pass_counts()
    currently_out = [p for p in passes if not p['Time In'].strip()]
    return render_template('dashboard.html', passes=currently_out, counts=counts)

@app.route('/student_list')
def student_list():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
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
        name = f"{entry['First Name']} {entry['Last Name']}".strip()
        counts[name] = counts.get(name, 0) + 1
    return counts

# ---------- RUN ----------
if __name__ == '__main__':
    app.run(debug=True)
