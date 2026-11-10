"""
Min & Max Salary Range Dashboard — Flask backend, exact-UI clone
Serves the original Index.html (adapted load() only) + a /api/data
endpoint matching the Google Apps Script getData() JSON shape,
sourced live from Zoho Sheet via OAuth refresh-token API.
"""
import os
import requests
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, Response, request, session, redirect, send_from_directory, jsonify
from dotenv import load_dotenv

load_dotenv()

ZOHO_CLIENT_ID = os.environ["ZOHO_CLIENT_ID"]
ZOHO_CLIENT_SECRET = os.environ["ZOHO_CLIENT_SECRET"]
ZOHO_REFRESH_TOKEN = os.environ["ZOHO_REFRESH_TOKEN"]
ZOHO_ACCOUNTS_DOMAIN = os.environ.get("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.in")
ZOHO_SHEET_API_DOMAIN = os.environ.get("ZOHO_SHEET_API_DOMAIN", "https://sheet.zoho.in")
ZOHO_WORKSHEET_ID = os.environ["ZOHO_WORKSHEET_ID"]
ZOHO_SHEET_NAME = os.environ.get("ZOHO_SHEET_NAME", "Active")

ADMIN_USER = os.environ.get("ADMIN_USER", "Hradmin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
RESTRICTED_USER = os.environ.get("RESTRICTED_USER", "Hruser")
RESTRICTED_PASS = os.environ.get("RESTRICTED_PASS", "")
RESTRICTED_MAX_SUBBAND = int(os.environ.get("RESTRICTED_MAX_SUBBAND", "7"))
SECRET_KEY = os.environ.get("FLASK_SECRET", "minmax-dashboard-secret-change-me")

# Location-restricted logins — same username, different password per city.
# Each maps to a (role, location-match-substring) pair.
LOCATION_LOGINS = {
    os.environ.get("CHENNAI_PASS", "hrchn"): "Chennai",
    os.environ.get("BENGALURU_PASS", "hrban"): "Bengaluru",
    os.environ.get("HYDERABAD_PASS", "hrhyd"): "Hyderabad",
}
LOCATION_USER = os.environ.get("LOCATION_USER", "Hruser")
# Match substrings for each city against the sheet's actual Location text (handles
# "Bangalore"/"Bengaluru" spelling variance etc.)
LOCATION_MATCH = {
    "Chennai": ["chennai"],
    "Bengaluru": ["bengaluru", "bangalore", "banglore"],
    "Hyderabad": ["hyderabad"],
}
# Location logins are also capped at Sub Band 11A and below (excludes 11B onward).
LOCATION_MAX_SUBBAND_RAW = os.environ.get("LOCATION_MAX_SUBBAND", "11B")

# Team login — Sub Band 8A and below (excludes 8B onward), excluding the HR Costing Head.
TEAM_USER = os.environ.get("TEAM_USER", "Hrteam")
TEAM_PASS = os.environ.get("TEAM_PASS", "Urban@2026")
TEAM_MAX_SUBBAND_RAW = os.environ.get("TEAM_MAX_SUBBAND", "8B")
TEAM_EXCLUDE_COSTHEAD = "hr"

app = Flask(__name__)
app.secret_key = SECRET_KEY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Only these columns get sent to the browser (mirrors Code.gs VISIBLE_COLUMNS)
# Name and Emp ID are intentionally excluded — kept confidential, never sent to the browser.
VISIBLE_COLUMNS = {
    "band", "sub band", "designation",
    "department", "role", "salary heads", "costing heads", "doj", "date of joining", "location",
    "gross monthly", "other allowances monthly", "annual varaible pay", "annual variable pay",
    "annual ctc", "exp", "experience", "total exp", "total experience", "exp tenure",
    "experience (years)", "total experience (years)", "total experience (in years)",
}


def norm(s):
    return " ".join(str(s).lower().split())


def sub_band_value(v):
    """Extract the leading numeric part of a Sub Band value like '7A' -> 7, '11B' -> 11."""
    import re
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def sub_band_ordinal(v):
    """Parse a Sub Band like '11A', '11B', '12' into a (number, salary_rank) tuple
    that sorts by actual salary level: the band number dominates (higher number =
    higher pay), and within the same number, earlier letters pay MORE — 'A' is the
    highest-paid sub-tier, 'B' next, 'C' lowest, etc. A plain number with no letter
    suffix has no sub-tier split. Returns None if unparsable."""
    import re
    m = re.match(r"\s*(\d+)\s*([A-Za-z]?)", str(v or ""))
    if not m:
        return None
    number = int(m.group(1))
    letter = m.group(2).upper()
    # 'A' -> 0 (highest), 'B' -> -1, 'C' -> -2 ... descending as the letter advances.
    salary_rank = 0 if not letter else -(ord(letter) - ord("A"))
    return (number, salary_rank)


def parse_subband_cap(raw, default):
    """Parse a cap value that may be a plain number ('11') or number+letter ('11B')
    into a (number, salary_rank) ordinal tuple for comparison — the cap is a ceiling,
    so any row at or below this salary level (per sub_band_ordinal) is shown."""
    ordinal = sub_band_ordinal(raw)
    return ordinal if ordinal is not None else sub_band_ordinal(str(default))


LOCATION_MAX_SUBBAND = parse_subband_cap(LOCATION_MAX_SUBBAND_RAW, "11B")
TEAM_MAX_SUBBAND = parse_subband_cap(TEAM_MAX_SUBBAND_RAW, "8B")


_token_cache = {"token": None, "expires": 0}


def get_access_token():
    import time
    if _token_cache["token"] and time.time() < _token_cache["expires"]:
        return _token_cache["token"]
    r = requests.post(
        f"{ZOHO_ACCOUNTS_DOMAIN}/oauth/v2/token",
        data={
            "refresh_token": ZOHO_REFRESH_TOKEN,
            "client_id": ZOHO_CLIENT_ID,
            "client_secret": ZOHO_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + data.get("expires_in", 3600) - 60
    return _token_cache["token"]


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("authed"):
            return redirect("/login")
        return f(*a, **kw)
    return wrapper


LOGIN_HTML = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Min & Max — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; background:#fbf1f6; display:flex; align-items:center;
    justify-content:center; height:100vh; margin:0; }}
  .card {{ background:#fff; padding:32px; border-radius:14px; box-shadow:0 1px 3px rgba(120,30,70,.15);
    width:320px; }}
  h1 {{ font-size:20px; margin:0 0 20px; color:#2a1720; }}
  input {{ width:100%; padding:10px; margin-bottom:12px; border:1px solid #ecc9da; border-radius:8px;
    box-sizing:border-box; font-size:14px; }}
  button {{ width:100%; padding:10px; background:#b0306f; color:#fff; border:0; border-radius:8px;
    font-size:14px; cursor:pointer; font-weight:600; }}
  .err {{ color:#c2410c; font-size:13px; margin-bottom:12px; }}
</style></head><body>
<div class="card">
  <h1>🔒 Min &amp; Max Dashboard</h1>
  {error}
  <form method="post">
    <input name="username" placeholder="Username" autofocus required>
    <input name="password" type="password" placeholder="Password" required>
    <button type="submit">Sign in</button>
  </form>
</div></body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["authed"] = True
            session["role"] = "admin"
            return redirect("/")
        if u == RESTRICTED_USER and p == RESTRICTED_PASS:
            session["authed"] = True
            session["role"] = "restricted"
            return redirect("/")
        if u == LOCATION_USER and p in LOCATION_LOGINS:
            session["authed"] = True
            session["role"] = "location"
            session["location"] = LOCATION_LOGINS[p]
            return redirect("/")
        if u == TEAM_USER and p == TEAM_PASS:
            session["authed"] = True
            session["role"] = "team"
            return redirect("/")
        error = '<div class="err">Invalid username or password.</div>'
    return LOGIN_HTML.format(error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    return send_from_directory(BASE_DIR, "static_index.html")


def format_subband_cap(ordinal):
    """Turn a (number, salary_rank) tuple back into a display label like '8B'."""
    if ordinal is None:
        return None
    number, salary_rank = ordinal
    letter = chr(ord("A") - salary_rank) if salary_rank != 0 else ""
    return f"{number}{letter}"


@app.route("/api/role")
@login_required
def api_role():
    role = session.get("role", "admin")
    return jsonify({
        "role": role,
        "maxSubBand": RESTRICTED_MAX_SUBBAND if role == "restricted" else (format_subband_cap(TEAM_MAX_SUBBAND) if role == "team" else None),
        "location": session.get("location") if role == "location" else None,
    })


def _fetch_sheet_rows(sheet_name=None):
    """Fetch the header row and every data row from one worksheet in the Zoho
    workbook. Returns (col_to_header, row_details) — the raw building blocks
    shared by /api/data and /api/lookup-emp."""
    sheet_name = sheet_name or ZOHO_SHEET_NAME
    token = get_access_token()

    # Step 1: read the header row to map column index -> header name.
    # (worksheet.records.fetch, the "Tabular Data" API, silently truncates at
    # the first blank header cell — this sheet has one between Gender and
    # Education, which was dropping every column after it. worksheet.content.get
    # reads raw cells by row/column range instead and has no such limitation.)
    hdr_resp = requests.get(
        f"{ZOHO_SHEET_API_DOMAIN}/api/v2/{ZOHO_WORKSHEET_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        params={
            "method": "worksheet.content.get",
            "worksheet_name": sheet_name,
            "start_row": 1, "end_row": 1,
            "start_column": 1, "end_column": 60,
        },
        timeout=30,
    )
    hdr_resp.raise_for_status()
    hdr_data = hdr_resp.json()
    used_row = hdr_data.get("used_row", 1)
    used_column = hdr_data.get("used_column", 60)
    header_cells = hdr_data["range_details"][0]["row_details"] if hdr_data.get("range_details") else []
    col_to_header = {c["column_index"]: str(c.get("content", "")).strip() for c in header_cells}

    # Step 2: read every data row (2..used_row) across all used columns.
    body_resp = requests.get(
        f"{ZOHO_SHEET_API_DOMAIN}/api/v2/{ZOHO_WORKSHEET_ID}",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        params={
            "method": "worksheet.content.get",
            "worksheet_name": sheet_name,
            "start_row": 2, "end_row": used_row,
            "start_column": 1, "end_column": used_column,
        },
        timeout=60,
    )
    body_resp.raise_for_status()
    body_data = body_resp.json()
    row_details = body_data.get("range_details", [])
    return col_to_header, row_details


# EMP ID lookups for the "Replacement" flow search these 3 worksheets, in
# order — a departing employee being replaced often sits in Attrition rather
# than Active. "Summary" is a pivot/dashboard sheet, not raw employee rows.
LOOKUP_SHEETS = [ZOHO_SHEET_NAME, "Attrition", "Entire Attrition"]


def _col_idx(col_to_header, *names):
    """Find a column index whose normalized header matches any of `names`."""
    wanted = {norm(n) for n in names}
    return next((i for i, h in col_to_header.items() if norm(h) in wanted), None)


def _row_visible(cells, sub_band_col_idx, band_col_idx, location_col_idx, costhead_col_idx):
    """Apply the session's role-based restrictions to one row's cells.
    Returns True if the current session is allowed to see this row."""
    role = session.get("role")
    if role == "restricted":
        raw = cells.get(sub_band_col_idx) if sub_band_col_idx else cells.get(band_col_idx)
        sb = sub_band_value(raw)
        if sb is None or sb > RESTRICTED_MAX_SUBBAND:
            return False
    elif role == "location":
        allowed_substrings = LOCATION_MATCH.get(session.get("location", ""), [])
        loc_val = norm(cells.get(location_col_idx, "")) if location_col_idx else ""
        if not any(sub in loc_val for sub in allowed_substrings):
            return False
        raw = cells.get(sub_band_col_idx) if sub_band_col_idx else cells.get(band_col_idx)
        sb = sub_band_ordinal(raw)
        if sb is None or sb > LOCATION_MAX_SUBBAND:
            return False
    elif role == "team":
        raw = cells.get(sub_band_col_idx) if sub_band_col_idx else cells.get(band_col_idx)
        sb = sub_band_ordinal(raw)
        if sb is None or sb > TEAM_MAX_SUBBAND:
            return False
        costhead_val = norm(cells.get(costhead_col_idx, "")) if costhead_col_idx else ""
        if costhead_val == TEAM_EXCLUDE_COSTHEAD:
            return False
    return True


def _parse_number(v):
    if v is None:
        return None
    s = str(v).replace("₹", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# EMP IDs to always exclude from the dashboard (not from the Zoho source
# sheet itself) — e.g. duplicate/test records flagged by HR for removal.
EXCLUDED_EMP_IDS = {"1501", "1502"}


def _is_excluded(cells, empid_col_idx):
    if empid_col_idx is None:
        return False
    raw = str(cells.get(empid_col_idx, "") or "").strip()
    if raw.upper().startswith("NMR"):
        return True
    # Match on the trailing digits so "1501", "EIN 1501", "EIN1501" all match.
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits in EXCLUDED_EMP_IDS


@app.route("/api/data")
@login_required
def api_data():
    col_to_header, row_details = _fetch_sheet_rows()

    if not row_details:
        return jsonify({"headers": [], "rows": [], "fetchedAt": datetime.now(timezone.utc).isoformat()})

    all_headers = [col_to_header[i] for i in sorted(col_to_header) if col_to_header[i]]
    keep_headers = [h for h in all_headers if norm(h) in VISIBLE_COLUMNS]

    sub_band_col_idx = _col_idx(col_to_header, "sub band")
    band_col_idx = _col_idx(col_to_header, "band")
    location_col_idx = _col_idx(col_to_header, "location")
    costhead_col_idx = _col_idx(col_to_header, "costing heads")
    empid_col_idx = _col_idx(col_to_header, "emp id")
    keep_col_idx = [i for i in sorted(col_to_header) if col_to_header[i] in keep_headers]

    rows = []
    for rd in row_details:
        cells = {c["column_index"]: c.get("content", "") for c in rd.get("row_details", [])}

        if not _row_visible(cells, sub_band_col_idx, band_col_idx, location_col_idx, costhead_col_idx):
            continue
        if _is_excluded(cells, empid_col_idx):
            continue

        row = [cells.get(i, "") for i in keep_col_idx]
        if any(v != "" and v is not None for v in row):
            rows.append(row)

    return jsonify({
        "headers": keep_headers,
        "rows": rows,
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/lookup-emp")
@login_required
def api_lookup_emp():
    """Look up ONE employee by EMP ID for the Recruiter "Replacement" flow.
    Searches Active, then Attrition, then Entire Attrition — the person being
    replaced has often already left, so Active alone isn't enough. Returns
    only salary/band/designation fields needed for comparison — never the
    employee's name. Subject to the same role-based visibility rules as
    /api/data (checked against whichever sheet the match came from), so a
    restricted/location/team login can't use this to peek at an employee
    outside their normal scope."""
    emp_id = (request.args.get("id") or "").strip()
    if not emp_id:
        return jsonify({"found": False, "error": "missing id"}), 400

    wanted = norm(emp_id)

    for sheet_name in LOOKUP_SHEETS:
        col_to_header, row_details = _fetch_sheet_rows(sheet_name)
        if not row_details:
            continue

        empid_col_idx = _col_idx(col_to_header, "emp id")
        if empid_col_idx is None:
            continue

        sub_band_col_idx = _col_idx(col_to_header, "sub band")
        band_col_idx = _col_idx(col_to_header, "band")
        location_col_idx = _col_idx(col_to_header, "location")
        costhead_col_idx = _col_idx(col_to_header, "costing heads")
        desig_col_idx = _col_idx(col_to_header, "designation")
        dept_col_idx = _col_idx(col_to_header, "department")
        # Active uses "Gross Monthly" / "Annual Varaible Pay"; the Attrition
        # sheets use shorter names ("Gross" / "Variable Pay") for the same data.
        gross_col_idx = _col_idx(col_to_header, "gross monthly", "gross")
        ctc_col_idx = _col_idx(col_to_header, "annual ctc")
        vp_col_idx = _col_idx(col_to_header, "annual varaible pay", "annual variable pay", "variable pay")

        for rd in row_details:
            cells = {c["column_index"]: c.get("content", "") for c in rd.get("row_details", [])}
            if norm(cells.get(empid_col_idx, "")) != wanted:
                continue

            if not _row_visible(cells, sub_band_col_idx, band_col_idx, location_col_idx, costhead_col_idx):
                return jsonify({"found": False})
            if _is_excluded(cells, empid_col_idx):
                return jsonify({"found": False})

            return jsonify({
                "found": True,
                "sheet": sheet_name,
                "designation": cells.get(desig_col_idx, ""),
                "department": cells.get(dept_col_idx, ""),
                "band": cells.get(band_col_idx, ""),
                "subBand": cells.get(sub_band_col_idx, ""),
                "location": cells.get(location_col_idx, ""),
                "gross": _parse_number(cells.get(gross_col_idx)),
                "ctc": _parse_number(cells.get(ctc_col_idx)),
                "vp": _parse_number(cells.get(vp_col_idx)),
            })

    return jsonify({"found": False})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8505)
