import io
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import pandas as pd
import plotly.express as px
import qrcode
import streamlit as st
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as ReportLabImage
from reportlab.lib.styles import getSampleStyleSheet

# ============================================================
# APAAR TRUSTGRAPH — COMPLETE DEMO APP (PHASES 1–5 STEP 2)
# Synthetic/demo data only. Not production authentication.
# ============================================================

st.set_page_config(page_title="APAAR TrustGraph", page_icon="🎓", layout="wide")

BASE_DIR = Path(__file__).parent
INSTITUTIONS_FILE = BASE_DIR / "institutions_fixed.csv"
DEMO_FILE = BASE_DIR / "student_enrolments_fixed.csv"
DB_FILE = BASE_DIR / "apaar_trustgraph.db"

REQUIRED_COLUMNS = [
    "record_id", "student_id", "apaar_id", "institution_id",
    "programme", "academic_year", "nri_category", "oci_status",
    "foreign_national_status"
]
STATUSES = ["Pending", "Under Review", "Resolved", "Rejected"]
SEVERITY_MAP = {
    "Missing APAAR ID": "High", "Duplicate APAAR ID": "Medium",
    "Cross-Institution Conflict": "High", "Unknown institution": "High",
    "Programme/year mismatch": "Medium", "Capacity exceeded": "High",
    "Approved intake exceeded": "Medium"
}
PURPOSES = [
    "Admission verification", "Academic record verification",
    "Scholarship eligibility", "Employment or internship verification"
]

# ---------------- DEMO LOGIN ----------------
DEMO_USERS = {
    "AICTE Admin": {"password": "Admin@123", "institution_id": None},
    "Institution": {"password": "Inst@123", "institution_id": "DEMO-I001"},
    "Verifier": {"password": "Verify@123", "institution_id": None}
}

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("🎓 APAAR TrustGraph")
    st.subheader("Login to continue")
    with st.form("login_form"):
        selected_role = st.selectbox("Select your role", list(DEMO_USERS))
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
    if submitted:
        account = DEMO_USERS[selected_role]
        if password == account["password"]:
            st.session_state.logged_in = True
            st.session_state.user_role = selected_role
            st.session_state.institution_id = account["institution_id"]
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    st.info("Demo credentials only. Do not use real student data or real passwords.")
    st.stop()

role = st.session_state.get("user_role", "")
if role not in DEMO_USERS:
    st.session_state.clear()
    st.rerun()
    
st.sidebar.success(f"Logged in as: {role}")
if role == "Institution":
    assigned_id = st.session_state.get("institution_id")
    if assigned_id:
        st.sidebar.info(f"Assigned Institution ID: {assigned_id}")
    else:
        st.sidebar.error("No institution ID is assigned. Log out and sign in again.")
if st.sidebar.button("Logout"):
    st.session_state.logged_in = False
    st.session_state.pop("user_role", None)
    st.session_state.pop("institution_id", None)
    st.rerun()

# ---------------- DATABASE ----------------
def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn

def initialize_database():
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS issue_reviews (
            issue_key TEXT PRIMARY KEY, record_id TEXT NOT NULL,
            issue TEXT NOT NULL, details TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'Medium',
            status TEXT NOT NULL DEFAULT 'Pending',
            reviewer_notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL, actor_role TEXT NOT NULL,
            action TEXT NOT NULL, record_id TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL, details TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS student_consent (
            record_id TEXT NOT NULL, purpose TEXT NOT NULL,
            consent_status TEXT NOT NULL DEFAULT 'Not Granted',
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            notes TEXT DEFAULT '', PRIMARY KEY(record_id, purpose)
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL, requesting_institution_id TEXT NOT NULL,
            record_id TEXT NOT NULL, purpose TEXT NOT NULL,
            request_status TEXT NOT NULL DEFAULT 'Pending',
            reviewed_at TEXT DEFAULT '', reviewed_by TEXT DEFAULT '',
            review_notes TEXT DEFAULT ''
        )""")
        conn.commit()

initialize_database()

def log_event(action, record_id, result, details=""):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO verification_audit
        (timestamp_utc, actor_role, action, record_id, result, details)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), role, str(action),
         str(record_id)[:200], str(result)[:100], str(details)[:500]))
        conn.commit()
        
def make_issue_key(record_id, issue, details):
    return hashlib.sha256(f"{record_id}||{issue}||{details}".encode("utf-8")).hexdigest()

def load_review(key, record_id, issue, details):
    with get_connection() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO issue_reviews
        (issue_key, record_id, issue, details, severity, status, reviewer_notes, updated_at)
        VALUES (?, ?, ?, ?, ?, 'Pending', '', ?)""",
        (key, str(record_id), str(issue), str(details),
         SEVERITY_MAP.get(issue, "Low"), datetime.now(timezone.utc).isoformat()))
        row = conn.execute("""
        SELECT severity, status, reviewer_notes, updated_at
        FROM issue_reviews WHERE issue_key = ?""", (key,)).fetchone()
        conn.commit()
    return row

def save_review(row):
    with get_connection() as conn:
        conn.execute("""
        INSERT INTO issue_reviews
        (issue_key, record_id, issue, details, severity, status, reviewer_notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issue_key) DO UPDATE SET
          severity=excluded.severity, status=excluded.status,
          reviewer_notes=excluded.reviewer_notes, updated_at=excluded.updated_at
        """, (
            row["issue_key"], str(row["record_id"]), str(row["issue"]),
            str(row["details"]), str(row["Severity"]), str(row["Status"]),
            str(row["Reviewer notes"]), datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()

@st.cache_data
def load_institutions():
    df = pd.read_csv(INSTITUTIONS_FILE, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    df = df[df["institution_id"].astype(str).str.strip().str.lower() != "institution_id"].copy()
    for col in ["institution_id", "programme", "academic_year"]:
        df[col] = df[col].fillna("").astype(str).str.strip()
    for col in ["approved_intake", "supernumerary_seats"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df

def clean_text(df):
    df = df.copy()
    for col in df.columns:
        df[col] = df[col].fillna("").astype(str).str.strip()
    return df

# ---------------- VALIDATION ----------------
def validate_students(uploaded, institutions):
    df = uploaded.copy()
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return df, pd.DataFrame(columns=["record_id", "issue", "details"]), missing
    df = clean_text(df[REQUIRED_COLUMNS])
    issues = []
    ids = df["apaar_id"].str.upper().str.strip()
    valid = ids.ne("")
    for idx in df.index[~valid]:
        issues.append({"record_id": df.at[idx, "record_id"], "issue": "Missing APAAR ID", "details": "APAAR ID is blank."})
    dup = valid & ids.duplicated(keep=False)
    for idx in df.index[dup]:
        issues.append({"record_id": df.at[idx, "record_id"], "issue": "Duplicate APAAR ID", "details": f"Repeated demo ID: {df.at[idx, 'apaar_id']}"})
    vf = df.loc[valid].copy()
    vf["_normalized_id"] = ids[valid]
    for _, group in vf.groupby("_normalized_id"):
        if group["institution_id"].str.upper().nunique() > 1:
            found = ", ".join(sorted(group["institution_id"].unique()))
            for idx in group.index:
                issues.append({"record_id": df.at[idx, "record_id"], "issue": "Cross-Institution Conflict", "details": f"Same demo APAAR ID is associated with multiple institutions: {found}. Review required."})
    known = set(institutions["institution_id"].str.upper())
    for idx in df.index:
        inst = df.at[idx, "institution_id"].upper()
        if inst not in known:
            issues.append({"record_id": df.at[idx, "record_id"], "issue": "Unknown institution", "details": f"Institution ID {inst} is not in the institution master."})
        matches = institutions[
            institutions["institution_id"].str.upper().eq(inst)
        ]
        if not matches.empty:
            matched = matches[
                matches["programme"].str.casefold().eq(df.at[idx, "programme"].casefold())
                & matches["academic_year"].str.casefold().eq(df.at[idx, "academic_year"].casefold())
            ]
            if matched.empty:
                issues.append({"record_id": df.at[idx, "record_id"], "issue": "Programme/year mismatch", "details": "Programme or academic year does not match the institution master."})
    counts = df.groupby("institution_id").size()
    for inst, count in counts.items():
        inst_rows = institutions[institutions["institution_id"].str.upper() == str(inst).upper()]
        if inst_rows.empty:
            continue
        approved = float(inst_rows.iloc[0]["approved_intake"])
        supernumerary = float(inst_rows.iloc[0]["supernumerary_seats"])
        capacity = approved + supernumerary
        if count > capacity:
            issues.append({"record_id": "", "issue": "Capacity exceeded", "details": f"Institution {inst} has {count} records against capacity {capacity:g}."})
        if count > approved:
            issues.append({"record_id": "", "issue": "Approved intake exceeded", "details": f"Institution {inst} has {count} records against approved intake {approved:g}."})
    return df, pd.DataFrame(issues, columns=["record_id", "issue", "details"]), []
# ---------------- LOAD DATA ----------------
st.title("🎓 APAAR TrustGraph")
st.caption("Student data validation, conflict detection and review")

try:
    institutions = load_institutions()
except Exception as exc:
    st.error(f"Could not load institutions_fixed.csv: {exc}")
    st.stop()

if role == "AICTE Admin":
    st.sidebar.header("Data source")
    uploaded_file = st.sidebar.file_uploader("Upload student CSV", type=["csv"])
else:
    st.sidebar.info("Student CSV upload is restricted to AICTE Admin.")
    uploaded_file = None

try:
    if uploaded_file is not None:
        students_input = pd.read_csv(uploaded_file, dtype=str).fillna("")
        source_name = uploaded_file.name
    else:
        students_input = pd.read_csv(DEMO_FILE, dtype=str).fillna("")
        source_name = "student_enrolments_fixed.csv (demo)"
except Exception as exc:
    st.error(f"Could not load student data: {exc}")
    st.stop()

st.caption(f"Current data source: {source_name}")
students, issues, missing_columns = validate_students(students_input, institutions)

if missing_columns:
    st.error("Missing required columns: " + ", ".join(missing_columns))
    st.stop()

# PHASE 5: Limit Institution role to its assigned institution.
if role == "Institution":
    assigned_id = str(st.session_state.get("institution_id") or "").strip()
    configured_id = str(DEMO_USERS["Institution"]["institution_id"])

    if not assigned_id or assigned_id != configured_id:
        st.error("Institution assignment is invalid. Log out and sign in again.")
        st.stop()

    students = students[
        students["institution_id"].astype(str).str.strip().str.casefold()
        == assigned_id.casefold()
    ].copy()

    if students.empty:
        st.warning(f"No student records belong to {assigned_id} in the demo dataset.")

    valid_record_ids = set(students["record_id"].astype(str))
    issues = issues[issues["record_id"].astype(str).isin(valid_record_ids)].copy()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Institutions", len(institutions) if role != "Institution" else 1)
c2.metric("Student records", len(students))
c3.metric("Validation issues", len(issues))
c4.metric(
    "Missing APAAR IDs",
    int(students["apaar_id"].astype(str).str.strip().eq("").sum())
)

st.divider()
st.subheader("📊 Institution Intake Overview")

counts = students.groupby("institution_id").size().reset_index(name="Records in dataset")
chart_data = institutions.merge(counts, on="institution_id", how="left")
chart_data["Records in dataset"] = chart_data["Records in dataset"].fillna(0)

if role == "Institution":
    chart_data = chart_data[
        chart_data["institution_id"].astype(str).str.casefold()
        == str(st.session_state["institution_id"]).casefold()
    ]

chart_long = chart_data.melt(
    id_vars=["institution_id"],
    value_vars=["Records in dataset", "approved_intake"],
    var_name="Measure",
    value_name="Count"
)

fig = px.bar(
    chart_long,
    x="institution_id",
    y="Count",
    color="Measure",
    barmode="group",
    title="Uploaded records vs approved intake"
)
st.plotly_chart(fig, use_container_width=True)
st.caption("Uploaded record counts do not necessarily represent complete enrolment.")

# ---------------- VALIDATION RESULTS ----------------
st.subheader("🔎 Validation Results")

if issues.empty:
    st.success("No validation issues found.")
else:
    st.warning(f"{len(issues)} issue entries detected.")
    selected_type = st.selectbox(
        "Filter validation issues",
        ["All"] + sorted(issues["issue"].unique().tolist())
    )
    shown = issues if selected_type == "All" else issues[issues["issue"] == selected_type]
    st.dataframe(shown, use_container_width=True)

    if role == "AICTE Admin":
        st.download_button(
            "⬇️ Download validation report",
            issues.to_csv(index=False).encode("utf-8-sig"),
            "validation_report.csv",
            "text/csv"
        )
    else:
        st.info("Validation report downloads are restricted to AICTE Admin.")

st.subheader("🔗 Cross-Institution Conflict Review")
cross = issues[issues["issue"] == "Cross-Institution Conflict"]

if cross.empty:
    st.info("No cross-institution conflicts found in this upload.")
else:
    st.warning(f"{cross['record_id'].nunique()} record(s) flagged for review.")
    st.dataframe(cross, use_container_width=True)

    if role == "AICTE Admin":
        st.download_button(
            "⬇️ Download conflict report",
            cross.to_csv(index=False).encode("utf-8-sig"),
            "cross_institution_conflicts.csv",
            "text/csv"
        )

# ---------------- ISSUE REVIEW ----------------
st.divider()
st.header("📝 Issue Review Workflow")

if issues.empty:
    st.info("There are no issues to review.")
else:
    review_df = issues.copy().reset_index(drop=True)
    review_df["issue_key"] = review_df.apply(
        lambda r: make_issue_key(r["record_id"], r["issue"], r["details"]),
        axis=1
    )

    saved_rows = []
    for _, r in review_df.iterrows():
        saved = load_review(
            r["issue_key"], r["record_id"], r["issue"], r["details"]
        )
        saved_rows.append({
            "issue_key": r["issue_key"],
            "record_id": r["record_id"],
            "issue": r["issue"],
            "details": r["details"],
            "Severity": saved[0],
            "Status": saved[1],
            "Reviewer notes": saved[2],
            "Last updated (UTC)": saved[3]
        })

    review_table = pd.DataFrame(saved_rows)
    a, b, c, d = st.columns(4)
    a.metric("Issues", len(review_table))
    b.metric("Pending", int((review_table["Status"] == "Pending").sum()))
    c.metric("Under Review", int((review_table["Status"] == "Under Review").sum()))
    d.metric("Resolved", int((review_table["Status"] == "Resolved").sum()))

    editable = review_table[
        ["issue_key", "record_id", "issue", "details", "Severity", "Status", "Reviewer notes"]
    ].copy()

    if role in ["AICTE Admin", "Verifier"]:
        edited = st.data_editor(
            editable,
            hide_index=True,
            use_container_width=True,
            num_rows="fixed",
            disabled=["issue_key", "record_id", "issue", "details"],
            column_config={
                "issue_key": None,
                "Severity": st.column_config.SelectboxColumn(
                    "Severity", options=["High", "Medium", "Low"], required=True
                ),
                "Status": st.column_config.SelectboxColumn(
                    "Review status", options=STATUSES, required=True
                ),
                "Reviewer notes": st.column_config.TextColumn("Reviewer notes")
            },
            key="review_editor"
        )

        for _, r in edited.iterrows():
            save_review(r)
    else:
        st.info("Read-only access: Institutions cannot edit issue reviews.")
        st.dataframe(
            editable.drop(columns=["issue_key"]),
            hide_index=True,
            use_container_width=True
        )

    with get_connection() as conn:
        report = pd.read_sql_query(
            """SELECT record_id, issue, details, severity, status,
                      reviewer_notes, updated_at AS last_updated_utc
               FROM issue_reviews ORDER BY record_id, issue""",
            conn
        )

    if role == "AICTE Admin":
        st.download_button(
            "⬇️ Download permanent review report",
            report.to_csv(index=False).encode("utf-8-sig"),
            "apaar_review_report.csv",
            "text/csv"
        )

# ---------------- STUDENT RECORDS ----------------
st.divider()
st.subheader("👩‍🎓 Student Records")

if role == "AICTE Admin":
    search = st.text_input("Search student ID, APAAR ID, or institution ID")
    filtered = students.copy()

    if search:
        mask = filtered.astype(str).apply(
            lambda col: col.str.contains(
                search, case=False, na=False, regex=False
            )
        ).any(axis=1)
        filtered = filtered[mask]

    st.dataframe(filtered, use_container_width=True)
    st.download_button(
        "⬇️ Download student data",
        filtered.to_csv(index=False).encode("utf-8-sig"),
        "student_records_export.csv",
        "text/csv"
    )
elif role == "Institution":
    st.info(
        f"Student records are filtered to your assigned institution: "
        f"{st.session_state['institution_id']}."
    )
    st.dataframe(students, use_container_width=True)
else:
    st.info("Verifier role: student-level records and downloads are disabled.")

if role == "AICTE Admin":
    with st.expander("View institution reference data"):
        st.dataframe(institutions, use_container_width=True)

# ---------------- PHASE 2: QR CERTIFICATE ----------------
def mask_apaar_id(value):
    value = str(value).strip()
    return "Not available" if not value else ("*" * max(0, len(value) - 4) + value[-4:])

def make_record_hash(record):
    fields = [
        "record_id", "student_id", "apaar_id",
        "institution_id", "programme", "academic_year"
    ]
    raw = "|".join(str(record[field]).strip() for field in fields)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

st.divider()
st.header("Student Verification & QR Certificate")
st.warning("DEMO ONLY: not connected to official APAAR or AICTE systems.")

required = [
    "record_id", "student_id", "apaar_id",
    "institution_id", "programme", "academic_year"
]

if all(c in students.columns for c in required):
    verification_data = students.copy().fillna("")
    for col in required:
        verification_data[col] = verification_data[col].astype(str).str.strip()

    st.subheader("1. Search and Verify Student")
    query_id = st.text_input(
        "Enter Student Record ID", key="phase2_search_record_id"
    ).strip()

    if st.button("Verify Student Record", key="phase2_verify_button"):
        found = (
            verification_data[
                verification_data["record_id"].str.casefold() == query_id.casefold()
            ] if query_id else pd.DataFrame()
        )

        if found.empty:
            st.error("Enter a record ID that exists in the current dataset.")
        elif len(found) != 1:
            st.error("Duplicate record IDs found. Resolve duplicates first.")
        else:
            r = found.iloc[0]
            st.success("Demo record found.")
            st.write("Record ID:", r["record_id"], "| Student ID:", r["student_id"])
            st.write("Masked APAAR ID:", mask_apaar_id(r["apaar_id"]))
            st.write(
                "Institution:", r["institution_id"],
                "| Programme:", r["programme"],
                "| Year:", r["academic_year"]
            )
            st.write("Record SHA-256:")
            st.code(make_record_hash(r))

    st.subheader("2. Generate Demo QR Certificate")
    if role != "AICTE Admin":
        st.info("Only AICTE Admin can generate demo certificates.")
    elif not verification_data.empty:
        options = verification_data.loc[
            verification_data["record_id"].ne(""), "record_id"
        ].drop_duplicates().tolist()
        selected = st.selectbox(
            "Select Student Record", options, key="phase2_certificate_select"
        )
        match = verification_data[verification_data["record_id"] == selected]

        if len(match) == 1:
            r = match.iloc[0]
            consent = st.checkbox(
                "I confirm I am authorized to generate a demo certificate for this synthetic record.",
                key="phase2_certificate_consent"
            )

            if st.button("Generate Demo Certificate", key="phase2_generate_button"):
                if not consent:
                    st.error("Confirm authorization first.")
                else:
                    digest = make_record_hash(r)
                    payload = (
                        "APAAR TRUSTGRAPH DEMO\n"
                        f"Record ID: {r['record_id']}\n"
                        f"SHA256: {digest}\n"
                        "STATUS: DEMO ONLY - NOT OFFICIAL"
                    )
                    qr = qrcode.QRCode(
                        version=1,
                        error_correction=qrcode.constants.ERROR_CORRECT_M,
                        box_size=6,
                        border=4
                    )
                    qr.add_data(payload)
                    qr.make(fit=True)
                    qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
                    qr_buffer = io.BytesIO()
                    qr_image.save(qr_buffer, format="PNG")
                    qr_buffer.seek(0)

                    pdf_buffer = io.BytesIO()
                    styles = getSampleStyleSheet()
                    doc = SimpleDocTemplate(pdf_buffer, pagesize=A4)
                    story = [
                        Paragraph("APAAR TrustGraph — Demo Verification Certificate", styles["Title"]),
                        Spacer(1, 16),
                        Paragraph("DEMO ONLY — NOT AN OFFICIAL APAAR OR AICTE CERTIFICATE", styles["Heading2"]),
                        Spacer(1, 12),
                        Paragraph(f"Record ID: {escape(str(r['record_id']))}", styles["BodyText"]),
                        Paragraph(f"Student ID: {escape(str(r['student_id']))}", styles["BodyText"]),
                        Paragraph(f"Masked APAAR ID: {escape(mask_apaar_id(r['apaar_id']))}", styles["BodyText"]),
                        Paragraph(f"Institution: {escape(str(r['institution_id']))}", styles["BodyText"]),
                        Paragraph(f"Programme: {escape(str(r['programme']))}", styles["BodyText"]),
                        Paragraph(f"Academic Year: {escape(str(r['academic_year']))}", styles["BodyText"]),
                        Paragraph(f"SHA-256: {digest}", styles["BodyText"]),
                        Spacer(1, 16),
                        ReportLabImage(qr_buffer, width=150, height=150)
                    ]
                    doc.build(story)
                    pdf_buffer.seek(0)
                    log_event("Demo certificate generated", r["record_id"], "Success", digest)
                    st.download_button(
                        "Download Demo Certificate PDF",
                        data=pdf_buffer.getvalue(),
                        file_name=f"demo_certificate_{r['record_id']}.pdf",
                        mime="application/pdf"
                    )
else:
    st.error("Required student record columns are missing; verification is unavailable.")

# ---------------- PHASE 3: QR TOKEN VERIFICATION AND AUDIT ----------------
st.divider()
st.header("🛡️ QR Token Verification & Audit Log")

qr_text = st.text_area("Paste decoded QR token text", height=130, key="phase3_qr_token")

if st.button("Verify QR Token", key="phase3_verify_token"):
   
    
    lines = [line.strip() for line in qr_text.splitlines() if line.strip()]

    token_id = ""
    token_hash = ""
    demo_marker_found = False

    for line in lines:
        label, separator, value = line.partition(":")
        if not separator:
            if line.casefold() == "apaar trustgraph demo":
                demo_marker_found = True
            continue

        label = label.strip().casefold()
        value = value.strip()

        if label == "record id":
            token_id = value
        elif label in ("sha256", "sha256 hash", "sha256 checksum"):
            token_hash = value
        elif label == "type" and "demo" in value.casefold():
            demo_marker_found = True
        elif label == "status" and "demo only" in value.casefold():
            demo_marker_found = True

    if not demo_marker_found or not token_id or not token_hash:
        st.error("Invalid token format. Check the record ID, demo type, and SHA256 checksum.")
        log_event(
            "QR token verification",
            token_id,
            "INVALID TOKEN",
            "Missing required fields or incorrect format."
        )

    elif len(token_hash) != 64 or any(
        ch not in "0123456789abcdefABCDEF" for ch in token_hash
    ):
        st.error("Invalid SHA-256 checksum format.")
        log_event(
            "QR token verification",
            token_id,
            "INVALID HASH FORMAT"
        )

    else:
        # Continue with your existing record lookup and hash comparison below.

        data = students.copy().fillna("")
        for col in required:
            data[col] = data[col].astype(str).str.strip()

        found = data[
            data["record_id"].str.casefold() == token_id.casefold()
        ]

        if len(found) != 1:
            st.error("NOT VERIFIED: record is missing or record ID is duplicated.")
            log_event(
                "QR token verification", token_id, "NOT VERIFIED",
                "Missing or duplicate record."
            )
        elif make_record_hash(found.iloc[0]).casefold() == token_hash.casefold():
            st.success("DEMO TOKEN MATCH: hash matches the currently loaded record.")
            st.write(
                "Record ID:", token_id,
                "| Masked APAAR ID:", mask_apaar_id(found.iloc[0]["apaar_id"])
            )
            log_event(
                "QR token verification", token_id, "DEMO MATCH",
                "Hash matched current record."
            )
        else:
            st.error("NOT VERIFIED: hash mismatch.")
            log_event(
                "QR token verification", token_id, "HASH MISMATCH",
                "Supplied hash differs from current record."
            )


st.subheader("Persistent Verification Audit Log")

if role == "AICTE Admin":
    with get_connection() as conn:
        audit = pd.read_sql_query(
            """SELECT audit_id, timestamp_utc, actor_role, action,
                      record_id, result, details
               FROM verification_audit
               ORDER BY audit_id DESC LIMIT 500""",
            conn
        )

    st.dataframe(audit, use_container_width=True)

    if not audit.empty:
        st.download_button(
            "Download Verification Audit CSV",
            audit.to_csv(index=False).encode("utf-8-sig"),
            "apaar_verification_audit.csv",
            "text/csv",
            key="phase3_audit_download"
        )
else:
    st.info("The audit log is visible only to the AICTE Admin demo role.")



# ---------------- PHASE 4: CONSENT AND REQUESTS ----------------

st.divider()
st.header("Phase 4: Student Consent & Verification Requests")

st.warning(
    "Demo only: consent and institution identities are not authenticated. "
    "Use synthetic data only."
)


def get_consent(record_id, purpose):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT consent_status
            FROM student_consent
            WHERE TRIM(record_id)=TRIM(?)
              AND TRIM(purpose)=TRIM(?)
            """,
            (str(record_id), str(purpose))
        ).fetchone()

    return row[0] if row else "Not Granted"


def get_requests(institution_id=None):
    with get_connection() as conn:
        if institution_id is None:
            return pd.read_sql_query(
                """
                SELECT * FROM verification_requests
                ORDER BY request_id DESC
                """,
                conn
            )

        return pd.read_sql_query(
            """
            SELECT * FROM verification_requests
            WHERE requesting_institution_id=?
            ORDER BY request_id DESC
            """,
            conn,
            params=(str(institution_id),)
        )


records = (
    sorted(students["record_id"].dropna().astype(str).unique().tolist())
    if "record_id" in students.columns else []
)

if not records:
    st.info("No student records are available for Phase 4.")

elif role == "AICTE Admin":

    # -------- A. Manage student consent --------
    st.subheader("A. Demo Consent Register")

    with st.form("phase4_consent_form"):
        consent_record = st.selectbox(
            "Select student record",
            records,
            key="phase4_consent_record"
        )

        consent_purpose = st.selectbox(
            "Purpose of data access",
            PURPOSES,
            key="phase4_consent_purpose"
        )

        consent_status = st.selectbox(
            "Consent status",
            ["Granted", "Withdrawn", "Not Granted"],
            key="phase4_consent_status"
        )

        consent_notes = st.text_area(
            "Demo notes (no sensitive information)",
            key="phase4_consent_notes"
        )

        save_consent = st.form_submit_button("Save Consent Status")

    if save_consent:
        now = datetime.now(timezone.utc).isoformat()

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO student_consent
                (record_id, purpose, consent_status, updated_at,
                 updated_by, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id, purpose) DO UPDATE SET
                    consent_status=excluded.consent_status,
                    updated_at=excluded.updated_at,
                    updated_by=excluded.updated_by,
                    notes=excluded.notes
                """,
                (
                    consent_record,
                    consent_purpose,
                    consent_status,
                    now,
                    role,
                    consent_notes
                )
            )
            conn.commit()

        log_event(
            "Consent status updated",
            consent_record,
            consent_status,
            f"Purpose: {consent_purpose}"
        )

        st.success("Demo consent status saved.")
        st.rerun()

    with get_connection() as conn:
        consent_df = pd.read_sql_query(
            """
            SELECT record_id, purpose, consent_status,
                   updated_at, updated_by, notes
            FROM student_consent
            ORDER BY updated_at DESC
            """,
            conn
        )

    st.dataframe(
        consent_df,
        use_container_width=True,
        hide_index=True
    )

    # -------- B. Review requests (Admin only) --------
    st.subheader("B. Review Verification Requests")

    admin_requests = get_requests()

    if admin_requests.empty:
        st.info("There are no verification requests yet.")

    else:
        st.dataframe(
            admin_requests,
            use_container_width=True,
            hide_index=True
        )

        pending_admin = admin_requests[
            admin_requests["request_status"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.casefold()
            .eq("pending")
        ]

        if not pending_admin.empty:
            request_options = (
                pd.to_numeric(
                    pending_admin["request_id"],
                    errors="coerce"
                )
                .dropna()
                .astype(int)
                .tolist()
            )

            with st.form("phase4_review_form"):
                req_id = st.selectbox(
                    "Select pending request ID",
                    request_options
                )

                decision = st.selectbox(
                    "Review decision",
                    ["Approve", "Reject"]
                )

                review_notes = st.text_area("Review notes")

                save_decision = st.form_submit_button(
                    "Save Review Decision"
                )

            if save_decision:
                blocked = False
                updated = 0
                current = None
                rec_id = ""
                purpose = ""
                new_status = ""

                with get_connection() as conn:
                    current = conn.execute(
                        """
                        SELECT record_id, purpose, request_status
                        FROM verification_requests
                        WHERE request_id=?
                        """,
                        (int(req_id),)
                    ).fetchone()

                    if current:
                        rec_id, purpose, current_status = current

                        if (
                            str(current_status).strip().casefold()
                            != "pending"
                        ):
                            st.warning(
                                "This request is no longer Pending. "
                                "Refresh and check its status."
                            )

                        else:
                            consent_row = conn.execute(
                                """
                                SELECT consent_status
                                FROM student_consent
                                WHERE TRIM(record_id)=TRIM(?)
                                  AND TRIM(purpose)=TRIM(?)
                                """,
                                (str(rec_id), str(purpose))
                            ).fetchone()

                            consent = (
                                str(consent_row[0]).strip().casefold()
                                if consent_row
                                else "not granted"
                            )

                            blocked = (
                                decision == "Approve"
                                and consent != "granted"
                            )

                            if not blocked:
                                new_status = (
                                    "Approved"
                                    if decision == "Approve"
                                    else "Rejected"
                                )

                                cursor = conn.execute(
                                    """
                                    UPDATE verification_requests
                                    SET request_status=?,
                                        reviewed_at=?,
                                        reviewed_by=?,
                                        review_notes=?
                                    WHERE request_id=?
                                      AND LOWER(TRIM(request_status))
                                          = 'pending'
                                    """,
                                    (
                                        new_status,
                                        datetime.now(timezone.utc).isoformat(),
                                        role,
                                        review_notes,
                                        int(req_id)
                                    )
                                )

                                conn.commit()
                                updated = cursor.rowcount

                if not current:
                    st.error("Request not found.")

                elif blocked:
                    log_event(
                        "Verification approval blocked",
                        rec_id,
                        "Blocked",
                        f"Request ID: {req_id}; Purpose: {purpose}; "
                        f"Consent: {consent}"
                    )

                    st.error(
                        f"APPROVAL BLOCKED: consent is '{consent}', "
                        "not Granted, for this record and purpose. "
                        "The request remains Pending."
                    )

                elif updated == 1:
                    log_event(
                        "Verification request reviewed",
                        rec_id,
                        new_status,
                        f"Request ID: {req_id}; Purpose: {purpose}; "
                        f"Notes: {review_notes}"
                    )

                    st.success(
                        f"Request #{req_id} was marked {new_status}."
                    )
                    st.rerun()

                elif current:
                    st.warning(
                        "The request was not updated. "
                        "Refresh and check its status."
                    )


elif role == "Institution":

    # -------- A. Validate institution assignment --------
    assigned_id = str(
        st.session_state.get("institution_id") or ""
    ).strip()

    expected_id = str(
        DEMO_USERS.get("Institution", {}).get("institution_id") or ""
    ).strip()

    if not assigned_id or assigned_id != expected_id:
        st.error(
            "Invalid institution assignment. "
            "Please log out and sign in again."
        )
        st.stop()

    st.info(
        f"Assigned Institution ID: {assigned_id}. "
        "Requests are submitted for this institution automatically."
    )

    # -------- B. Submit verification request --------
    st.subheader("B. Submit a Verification Request")

    with st.form("phase4_request_form"):
        requested_record = st.selectbox(
            "Student record to verify",
            records,
            key="phase4_requested_record"
        )

        requested_purpose = st.selectbox(
            "Reason for verification",
            PURPOSES,
            key="phase4_request_purpose"
        )

        submit_request = st.form_submit_button(
            "Submit Verification Request"
        )

    # IMPORTANT: This block must remain inside the Institution branch.
                # IMPORTANT: This block must remain inside the Institution branch.
        if submit_request:
            session_role = st.session_state.get("user_role", "")

            session_inst = str(
                st.session_state.get("institution_id") or ""
            ).strip()

            expected_inst = str(
                DEMO_USERS.get(session_role, {}).get("institution_id") or ""
            ).strip()

            if (
                session_role != "Institution"
                or not session_inst
                or session_inst != expected_inst
                or session_inst != assigned_id
            ):
                st.error(
                    "Request blocked: invalid role or institution assignment."
                )

                log_event(
                    "Verification request blocked",
                    requested_record,
                    "Blocked",
                    "Invalid institution session assignment."
                )

            else:
                now = datetime.now(timezone.utc).isoformat()

                with get_connection() as conn:
                    cursor = conn.execute(
                        """
                        INSERT INTO verification_requests
                        (
                            created_at,
                            requesting_institution_id,
                            record_id,
                            purpose,
                            request_status
                        )
                        VALUES (?, ?, ?, ?, 'Pending')
                        """,
                        (
                            now,
                            session_inst,
                            requested_record,
                            requested_purpose
                        )
                    )

                    request_id = cursor.lastrowid
                    conn.commit()

                log_event(
                    "Verification request submitted",
                    requested_record,
                    "Pending",
                    f"Request ID: {request_id}; "
                    f"Institution: {session_inst}; "
                    f"Purpose: {requested_purpose}"
                )

                st.success(
                    f"Request #{request_id} submitted for admin review."
                )

                st.rerun()

    # -------- C. Institution sees its own requests --------
    st.subheader("C. Your Institution's Requests")

    own = get_requests(assigned_id)

    if own.empty:
        st.info("No requests found for your assigned demo institution.")
    else:
        st.dataframe(
            own,
            use_container_width=True,
            hide_index=True
        )


elif role == "Verifier":

    # -------- Verifier sees metadata only --------
    st.subheader("Verification Request Overview")

    requests = get_requests()

    if requests.empty:
        st.info("There are no verification requests yet.")

    else:
        metadata_columns = [
            column
            for column in [
                "request_id",
                "created_at",
                "requesting_institution_id",
                "record_id",
                "purpose",
                "request_status"
            ]
            if column in requests.columns
        ]

        st.dataframe(
            requests[metadata_columns],
            use_container_width=True,
            hide_index=True
        )

    st.caption(
        "The Verifier can view request metadata only and cannot "
        "approve or reject requests."
    )
