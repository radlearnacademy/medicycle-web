import calendar
import datetime
import uuid
import pandas as pd
import streamlit as st
from fpdf import FPDF
from supabase import Client, create_client

st.set_page_config(
    page_title="MediCycle | Reproductive Health & HSG Platform",
    page_icon="🏥",
    layout="wide",
)

CLINICAL_REF_VERSION = "v2026.1-RadiologyInfo-Days7-10"
DISCLAIMER = (
    "CLINICAL DECISION-SUPPORT DISCLAIMER: MediCycle provides calendar-based menstrual "
    "estimates and structured safety screening checklists. Automated calculations NEVER "
    "confirm ovulation, NEVER exclude pregnancy, NEVER diagnose uterine tumours or malignancy, "
    "and NEVER replace verified clinician clearance or approved radiology booking."
)
TISSUE_OPTIONS = [
    "Unknown tissue status",
    "Normal tissue",
    "Suspected abnormal tissue",
    "Confirmed tumour",
]


def get_supabase_creds():
    if "connections" in st.secrets and "supabase" in st.secrets["connections"]:
        cfg = st.secrets["connections"]["supabase"]
        return cfg["SUPABASE_URL"], cfg["SUPABASE_PUBLISHABLE_KEY"]
    return st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_PUBLISHABLE_KEY"]


def get_supabase() -> Client:
    url, key = get_supabase_creds()
    client = create_client(url, key)
    if st.session_state.get("sb_session"):
        client.auth.set_session(
            st.session_state.sb_session.access_token,
            st.session_state.sb_session.refresh_token,
        )
    return client


def log_audit(sb: Client, profile: dict, action: str, target_id: str = "", details: dict = None):
    try:
        sb.table("audit_logs").insert(
            {
                "actor_email": profile.get("email"),
                "actor_role": profile.get("role"),
                "action": action,
                "target_id": target_id,
                "details": details or {},
            }
        ).execute()
    except Exception:
        pass


def calculate_cycle_metrics(lmp: datetime.date, cycle_len: int, ref_date: datetime.date):
    cycle_day = (ref_date - lmp).days + 1
    next_period = lmp + datetime.timedelta(days=cycle_len)
    ovulation = next_period - datetime.timedelta(days=14)
    fertile_start = ovulation - datetime.timedelta(days=5)
    fertile_end = ovulation
    hsg_start = lmp + datetime.timedelta(days=6)  # Cycle Day 7
    hsg_end = lmp + datetime.timedelta(days=9)    # Cycle Day 10
    if cycle_day < 7:
        win_status = "Upcoming"
    elif cycle_day <= 10:
        win_status = "Current (Within Days 7-10 Window)"
    else:
        win_status = "Past"
    return {
        "cycle_day": cycle_day,
        "next_period": next_period,
        "ovulation": ovulation,
        "fertile_start": fertile_start,
        "fertile_end": fertile_end,
        "hsg_start": hsg_start,
        "hsg_end": hsg_end,
        "window_status": win_status,
        "ovulation_cycle_day": cycle_len - 13,
    }


def evaluate_safety(lmp, cycle_len, ref_date, regularity, bleeding_stopped, preg_poss, preg_test, infection, extra):
    metrics = calculate_cycle_metrics(lmp, cycle_len, ref_date)
    warnings = []
    critical = 0

    if preg_poss in ("Yes", "Unsure") or preg_test == "Positive":
        critical += 1
        warnings.append({
            "severity": "critical",
            "title": "Pregnancy Exclusion Required",
            "msg": "Calendar ovulation estimates cannot exclude pregnancy. Formal pregnancy testing and clinician clearance are mandatory before HSG.",
        })
    else:
        warnings.append({
            "severity": "info",
            "title": "Pregnancy Protocol Reminder",
            "msg": "Reporting 'No' pregnancy possibility does not independently exclude pregnancy; department protocol remains mandatory.",
        })

    if infection in ("Diagnosed active pelvic infection", "Suspected pelvic infection"):
        critical += 1
        warnings.append({
            "severity": "critical",
            "title": "Active / Suspected Pelvic Infection",
            "msg": f"{infection} reported. HSG must NOT proceed; clinical assessment and resolution are required.",
        })
    elif infection == "Uncertain infection status":
        warnings.append({
            "severity": "warning",
            "title": "Uncertain Infection Status",
            "msg": "Infection status remains uncertain/unknown rather than assumed normal. Clinician review required.",
        })

    if bleeding_stopped in ("No", "Unsure"):
        warnings.append({
            "severity": "warning",
            "title": "Ongoing / Uncertain Menstrual Bleeding",
            "msg": "Preferred post-menstrual scheduling conditions may not yet be met.",
        })

    if regularity in ("Irregular", "Unsure"):
        warnings.append({
            "severity": "warning",
            "title": "Irregular Cycle Reliability Limitation",
            "msg": f"Cycle regularity is '{regularity}'. Ovulation and fertile-window dates may be unreliable; individualized review required.",
        })

    if cycle_len <= 24:
        warnings.append({
            "severity": "warning",
            "title": "Short Cycle Early-Ovulation Risk",
            "msg": f"Cycle length ({cycle_len}d) <= 24 days: estimated ovulation (~Day {metrics['ovulation_cycle_day']}) may overlap Days 7-10.",
        })

    if metrics["cycle_day"] > cycle_len:
        warnings.append({
            "severity": "warning",
            "title": "Cycle Length Exceeded",
            "msg": f"Current Cycle Day ({metrics['cycle_day']}) exceeds average length ({cycle_len}d). Re-evaluate LMP & exclude pregnancy.",
        })

    flagged_extra = [k for k, v in extra.items() if v == "Yes"]
    if flagged_extra:
        if extra.get("Fever or abnormal discharge") == "Yes" or extra.get("Previous contrast reaction") == "Yes":
            critical += 1
        warnings.append({
            "severity": "warning",
            "title": "Additional Clinical Considerations",
            "msg": "Reported factors requiring review: " + ", ".join(flagged_extra),
        })

    preg_pending = not (preg_poss == "No" and preg_test == "Negative")
    inf_req = infection != "No known pelvic infection" or extra.get("Fever or abnormal discharge") == "Yes" or extra.get("Unexplained pelvic pain") == "Yes"

    if infection in ("Diagnosed active pelvic infection", "Suspected pelvic infection"):
        auto_status = "Infection assessment required"
    elif preg_pending:
        auto_status = "Pregnancy exclusion pending"
    elif warnings:
        auto_status = "Clinical review required"
    else:
        auto_status = "Clinician clearance pending"

    return metrics, warnings, auto_status, critical


def safe_pdf_text(val) -> str:
    txt = str(val or "").replace("—", "-").replace("–", "-")
    return txt.encode("latin-1", "replace").decode("latin-1")


def build_pdf_bytes(assessment: dict, patient: dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, safe_pdf_text("MediCycle - Clinical HSG & Cycle Assessment Report"), ln=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, safe_pdf_text(f"Reference: {assessment['id']} | Date: {assessment['assessment_date']} | Guidance: {CLINICAL_REF_VERSION}"), ln=True)
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, safe_pdf_text("1. Patient Identification"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pt_name = patient.get("full_name", assessment["patient_id"]) if patient else assessment["patient_id"]
    pt_dob = patient.get("date_of_birth", "N/A") if patient else "N/A"
    pt_tissue = patient.get("tissue_status", "Unknown tissue status") if patient else "Unknown"
    pdf.multi_cell(0, 6, safe_pdf_text(
        f"Patient ID: {assessment['patient_id']} | Name: {pt_name} | DOB: {pt_dob}\n"
        f"Documented Tissue Status (Non-HSG): {pt_tissue}"
    ))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, safe_pdf_text("2. Automated Menstrual & HSG Reference Estimates (Non-Diagnostic)"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_pdf_text(
        f"LMP (Day 1): {assessment['lmp_date']} | Cycle Length: {assessment['avg_cycle_length']} days | Regularity: {assessment['cycle_regularity']}\n"
        f"Current Cycle Day: Day {assessment['calculated_cycle_day']} | Bleeding Stopped: {assessment['bleeding_stopped']}\n"
        f"Est. Next Period: {assessment['est_next_period']} | Approx. Ovulation (Calendar): {assessment['approx_ovulation_date']}\n"
        f"Est. Fertile Window: {assessment['fertile_window_start']} to {assessment['fertile_window_end']}\n"
        f"HSG Reference Window (Days 7-10): {assessment['hsg_ref_window_start']} to {assessment['hsg_ref_window_end']}"
    ))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, safe_pdf_text("3. Clinical Safety Screening"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_pdf_text(
        f"Pregnancy Possibility: {assessment['pregnancy_possibility']} | Pregnancy Test: {assessment['pregnancy_test_status']} ({assessment.get('pregnancy_test_date') or 'N/A'})\n"
        f"Pelvic Infection Status: {assessment['pelvic_infection_status']}"
    ))
    for w in assessment.get("safety_warnings") or []:
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, safe_pdf_text(f"- {w.get('title')}: {w.get('msg')}"))
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, safe_pdf_text("4. Verified Clinical Review & Clinician Decision"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, safe_pdf_text(
        f"Review Status: {assessment['review_status']}\n"
        f"Clinician Decision: {assessment.get('clinician_decision') or 'Pending'}\n"
        f"Reviewing Professional: {assessment.get('reviewed_by') or 'Pending'}\n"
        f"Notes: {assessment.get('clinician_notes') or 'None recorded.'}"
    ))
    pdf.ln(3)

    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4.5, safe_pdf_text(DISCLAIMER))
    return bytes(pdf.output())


# ---------- Session & Authentication ----------
sb = get_supabase()

if "user" not in st.session_state:
    st.session_state.user = None
    st.session_state.sb_session = None
    st.session_state.profile = None

if not st.session_state.user:
    st.title("MediCycle")
    st.caption("Reproductive Health & HSG Scheduling Clinical Decision-Support Platform")
    with st.form("login"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign In"):
            try:
                res = sb.auth.sign_in_with_password({"email": email.strip(), "password": password})
                st.session_state.user = res.user
                st.session_state.sb_session = res.session
                sb = get_supabase()
                prof_res = sb.table("app_users").select("*").eq("auth_user_id", res.user.id).execute()
                if prof_res.data:
                    st.session_state.profile = prof_res.data[0]
                    log_audit(sb, st.session_state.profile, "USER_LOGIN", res.user.id)
                    st.rerun()
                else:
                    st.error("Signed in, but no role profile found in public.app_users. Please add this user to app_users in Supabase.")
            except Exception as exc:
                st.error(f"Sign-in failed: {exc}")
    st.stop()

profile = st.session_state.profile
role = profile["role"]

# Fetch Authorized Data (Filtered Automatically by PostgreSQL RLS)
patients = (sb.table("patients").select("*").order("created_at", desc=True).execute().data) or []
assessments = (sb.table("hsg_assessments").select("*").order("created_at", desc=True).execute().data) or []
appointments = (sb.table("hsg_appointments").select("*").order("appointment_date", desc=False).execute().data) or []
patient_by_id = {p["id"]: p for p in patients}

# ---------- Sidebar Navigation ----------
with st.sidebar:
    st.markdown("### MediCycle")
    st.caption(f"{profile['full_name']} ({role})")
    pages = [
        "Dashboard",
        "Patient Management",
        "Register New Patient",
        "HSG Cycle Calculator",
        "Safety Screening & Clinician Review",
        "HSG Scheduling Overview",
        "Assessment History & PDF Reports",
        "Analytics & Statistics",
        "Settings & Audit Logs",
    ]
    if role == "Patient":
        pages = [
            "Dashboard",
            "HSG Cycle Calculator",
            "HSG Scheduling Overview",
            "Assessment History & PDF Reports",
        ]
    elif role == "Radiology Staff":
        pages = [
            "Dashboard",
            "Patient Management",
            "HSG Scheduling Overview",
            "Assessment History & PDF Reports",
            "Analytics & Statistics",
        ]
    page = st.radio("Navigation", pages)
    st.divider()
    if st.button("Sign Out"):
        sb.auth.sign_out()
        st.session_state.user = None
        st.session_state.sb_session = None
        st.session_state.profile = None
        st.rerun()

# ---------- Pages ----------
if page == "Dashboard":
    st.title("MediCycle Control Centre")
    st.caption(f"Welcome, {profile['full_name']} | Date: {datetime.date.today().isoformat()} | {CLINICAL_REF_VERSION}")

    pending_rev = [a for a in assessments if a["review_status"] != "Assessment completed"]
    completed_rev = [a for a in assessments if a["review_status"] == "Assessment completed"]
    safety_alerts = [a for a in assessments if a["review_status"] in ("Pregnancy exclusion pending", "Infection assessment required")]

    c1, c2, c3 = st.columns(3)
    c1.metric("Total Registered Patients", len(patients))
    c2.metric("Total HSG Assessments", len(assessments))
    c3.metric("Pending Clinical Reviews", len(pending_rev))

    c4, c5, c6 = st.columns(3)
    c4.metric("Completed Reports", len(completed_rev))
    c5.metric("Safety Verification Needed", len(safety_alerts))
    c6.metric("Approved Appointments", len(appointments))

    st.subheader("Recent Assessment Activity")
    if not assessments:
        st.info("No assessments stored yet. Register a patient and run the HSG Cycle Calculator.")
    else:
        rows = [
            {
                "Ref": a["id"],
                "Patient": patient_by_id.get(a["patient_id"], {}).get("full_name", a["patient_id"]),
                "Date": a["assessment_date"],
                "Cycle Day": f"Day {a['calculated_cycle_day']}",
                "HSG Ref Window": f"{a['hsg_ref_window_start']} to {a['hsg_ref_window_end']}",
                "Status": a["review_status"],
                "Reviewer": a.get("reviewed_by") or "Pending",
            }
            for a in assessments[:10]
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True)

elif page == "Patient Management":
    st.title("Patient Management")
    q = st.text_input("Search by Patient ID or Name")
    filtered = [p for p in patients if not q or q.lower() in p["full_name"].lower() or q.lower() in p["id"].lower()]
    if not filtered:
        st.info("No matching patients found.")
    else:
        st.dataframe(pd.DataFrame(filtered)[["id", "full_name", "date_of_birth", "referring_professional", "tissue_status", "registered_at"]], hide_index=True)

        if role in ("Administrator", "Healthcare Professional"):
            st.subheader("Update Documented Tissue / Imaging Status")
            st.caption("HSG is not a definitive tumour-characterization study. Tissue status is never inferred from menstrual data.")
            sel_pid = st.selectbox("Select Patient", [p["id"] for p in filtered], format_func=lambda x: f"{patient_by_id[x]['full_name']} ({x})")
            cur_t = patient_by_id[sel_pid]["tissue_status"]
            new_t = st.selectbox("Tissue Status", TISSUE_OPTIONS, index=TISSUE_OPTIONS.index(cur_t) if cur_t in TISSUE_OPTIONS else 0)
            if st.button("Save Tissue Status"):
                sb.table("patients").update({"tissue_status": new_t}).eq("id", sel_pid).execute()
                log_audit(sb, profile, "TISSUE_STATUS_UPDATED", sel_pid, {"tissue_status": new_t})
                st.success("Tissue status updated.")
                st.rerun()

elif page == "Register New Patient":
    st.title("Register New Patient")
    next_id = f"MC-PT-{len(patients) + 1:04d}"
    with st.form("reg_patient"):
        pid = st.text_input("Patient ID *", value=next_id)
        name = st.text_input("Full Name *")
        dob = st.date_input("Date of Birth *", value=datetime.date(1994, 1, 1), max_value=datetime.date.today())
        phone = st.text_input("Contact Phone")
        ref_doc = st.text_input("Referring Healthcare Professional *", value=profile["full_name"])
        history = st.text_area("Relevant Reproductive-Health History")
        consent = st.checkbox("NDPA 2023 Sensitive Health Data Consent Obtained *")
        if st.form_submit_button("Register Patient"):
            if not name.strip() or not consent:
                st.error("Full name and NDPA 2023 consent are required.")
            else:
                try:
                    sb.table("patients").insert({
                        "id": pid.strip(),
                        "full_name": name.strip(),
                        "date_of_birth": dob.isoformat(),
                        "contact_phone": phone.strip(),
                        "reproductive_history": history.strip(),
                        "referring_professional": ref_doc.strip(),
                        "ndpa_consent_granted": True,
                        "created_by": st.session_state.user.id,
                    }).execute()
                    log_audit(sb, profile, "PATIENT_REGISTERED", pid.strip())
                    st.success(f"Patient {name} ({pid}) registered!")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save patient: {exc}")

elif page == "HSG Cycle Calculator":
    st.title("HSG Menstrual Cycle & Scheduling Calculator")
    if not patients:
        st.warning("Register a patient first before creating an assessment.")
        st.stop()

    with st.form("calc_form"):
        st.subheader("Section A: Menstrual Information")
        sel_pid = st.selectbox("Patient *", [p["id"] for p in patients], format_func=lambda x: f"{patient_by_id[x]['full_name']} ({x})")
        c1, c2, c3 = st.columns(3)
        lmp = c1.date_input("First Day of LMP (Day 1) *", value=datetime.date.today() - datetime.timedelta(days=5), max_value=datetime.date.today())
        cycle_len = c2.number_input("Average Cycle Length (days) *", min_value=18, max_value=60, value=28)
        ref_date = c3.date_input("Assessment Date *", value=datetime.date.today())
        regularity = st.selectbox("Menstrual-Cycle Regularity *", ["Regular", "Irregular", "Unsure"])
        bleeding = st.radio("Has menstrual bleeding completely stopped? *", ["Yes", "No", "Unsure"], horizontal=True)

        st.subheader("Clinical Safety Screening")
        preg_poss = st.radio("Is there any possibility that you could currently be pregnant? *", ["No", "Yes", "Unsure"], horizontal=True)
        sc1, sc2, sc3 = st.columns(3)
        infection = sc1.selectbox("Pelvic Infection Status *", [
            "No known pelvic infection",
            "Diagnosed active pelvic infection",
            "Suspected pelvic infection",
            "Uncertain infection status",
        ])
        preg_test = sc2.selectbox("Pregnancy Test Status", ["Not Done", "Negative", "Positive", "Pending/Unknown"])
        preg_test_date = sc3.date_input("Pregnancy Test Date (if done)", value=None)

        st.caption("Additional Clinical Considerations (Unknown remains labelled Unknown)")
        e1, e2, e3 = st.columns(3)
        extra = {
            "Previous PID": e1.selectbox("Previous PID", ["Unknown", "No", "Yes"]),
            "History of STI": e2.selectbox("History of STI", ["Unknown", "No", "Yes"]),
            "Previous contrast reaction": e3.selectbox("Contrast-media reaction", ["Unknown", "No", "Yes"]),
            "Unexplained pelvic pain": e1.selectbox("Unexplained pelvic pain", ["Unknown", "No", "Yes"]),
            "Fever or abnormal discharge": e2.selectbox("Fever / abnormal discharge", ["Unknown", "No", "Yes"]),
            "Recent miscarriage/procedure": e3.selectbox("Recent pregnancy procedure", ["Unknown", "No", "Yes"]),
        }
        notes = st.text_area("Assessment Notes")
        submitted = st.form_submit_button("Calculate & Save Assessment")

    if submitted:
        if ref_date < lmp:
            st.error("LMP date cannot be later than the assessment date.")
        else:
            metrics, warnings, auto_status, critical = evaluate_safety(
                lmp, int(cycle_len), ref_date, regularity, bleeding, preg_poss, preg_test, infection, extra
            )
            asm_id = f"MC-ASM-{uuid.uuid4().hex[:6].upper()}"
            payload = {
                "id": asm_id,
                "patient_id": sel_pid,
                "assessment_date": ref_date.isoformat(),
                "lmp_date": lmp.isoformat(),
                "avg_cycle_length": int(cycle_len),
                "cycle_regularity": regularity,
                "bleeding_stopped": bleeding,
                "calculated_cycle_day": metrics["cycle_day"],
                "est_next_period": metrics["next_period"].isoformat(),
                "approx_ovulation_date": metrics["ovulation"].isoformat(),
                "fertile_window_start": metrics["fertile_start"].isoformat(),
                "fertile_window_end": metrics["fertile_end"].isoformat(),
                "hsg_ref_window_start": metrics["hsg_start"].isoformat(),
                "hsg_ref_window_end": metrics["hsg_end"].isoformat(),
                "pregnancy_possibility": preg_poss,
                "pregnancy_test_status": preg_test,
                "pregnancy_test_date": preg_test_date.isoformat() if preg_test_date else None,
                "pelvic_infection_status": infection,
                "additional_screening": extra,
                "safety_warnings": warnings,
                "review_status": auto_status,
                "clinician_notes": notes.strip(),
                "created_by": st.session_state.user.id,
            }
            sb.table("hsg_assessments").insert(payload).execute()
            log_audit(sb, profile, "ASSESSMENT_CREATED", asm_id, {"patient_id": sel_pid, "status": auto_status})

            st.success(f"Assessment {asm_id} saved! Status: {auto_status}")
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Current Cycle Day", f"Day {metrics['cycle_day']}")
            r2.metric("Est. Next Period", metrics["next_period"].isoformat())
            r3.metric("Approx. Ovulation (Cal.)", metrics["ovulation"].isoformat())
            r4.metric("HSG Ref Days 7-10", f"{metrics['hsg_start']} to {metrics['hsg_end']}")
            st.info(f"Window Position: {metrics['window_status']} | Reminder: Reference Days 7-10 is NOT an approved appointment or eligibility declaration.")
            for w in warnings:
                if w["severity"] == "critical":
                    st.error(f"**{w['title']}**: {w['msg']}")
                elif w["severity"] == "warning":
                    st.warning(f"**{w['title']}**: {w['msg']}")
                else:
                    st.info(f"**{w['title']}**: {w['msg']}")

elif page == "Safety Screening & Clinician Review":
    st.title("Clinical Safety Screening & Verified Clinician Review")
    if not assessments:
        st.info("No assessments available for review.")
        st.stop()
    sel_aid = st.selectbox("Select Assessment", [a["id"] for a in assessments], format_func=lambda x: f"{x} - {next((a['review_status'] for a in assessments if a['id'] == x), '')}")
    asm = next(a for a in assessments if a["id"] == sel_aid)

    st.write(f"**Patient:** {patient_by_id.get(asm['patient_id'], {}).get('full_name', asm['patient_id'])} | **Cycle Day:** {asm['calculated_cycle_day']} | **HSG Ref:** {asm['hsg_ref_window_start']} to {asm['hsg_ref_window_end']}")
    for w in asm.get("safety_warnings") or []:
        if w["severity"] == "critical":
            st.error(f"**{w['title']}**: {w['msg']}")
        elif w["severity"] == "warning":
            st.warning(f"**{w['title']}**: {w['msg']}")
        else:
            st.info(f"**{w['title']}**: {w['msg']}")

    if role in ("Administrator", "Healthcare Professional"):
        with st.form("review_form"):
            st.subheader("Record Verified Clinician Decision")
            new_status = st.selectbox("Clinical Review Status", [
                "Assessment completed",
                "Clinical review required",
                "Pregnancy exclusion pending",
                "Infection assessment required",
                "Clinician clearance pending",
            ])
            decision = st.selectbox("Clinician Decision", [
                "Cleared for HSG per Protocol",
                "Deferred - Pregnancy Exclusion Required",
                "Deferred - Infection Assessment Required",
                "Individualized Clinical Review Needed",
            ])
            cnotes = st.text_area("Verified Clinical Findings", value=asm.get("clinician_notes") or "")
            if st.form_submit_button("Save Clinician Clearance / Review"):
                sb.table("hsg_assessments").update({
                    "review_status": new_status,
                    "clinician_decision": decision,
                    "reviewed_by": profile["full_name"],
                    "reviewed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "clinician_notes": cnotes.strip(),
                }).eq("id", sel_aid).execute()
                log_audit(sb, profile, "CLINICIAN_REVIEW_SAVED", sel_aid, {"status": new_status, "decision": decision})
                st.success("Verified clinician review saved.")
                st.rerun()

elif page == "HSG Scheduling Overview":
    st.title("HSG Scheduling Overview")
    st.caption("Distinguishes calculated Days 7-10 reference windows from clinician-approved booked appointments.")

    today = datetime.date.today()
    month_dates = calendar.Calendar(firstweekday=6).monthdatescalendar(today.year, today.month)
    st.subheader(f"Reference & Booking Calendar - {today.strftime('%B %Y')}")

    cal_rows = []
    for week in month_dates:
        row = {}
        for dt in week:
            day_label = dt.strftime("%a")
            tags = [str(dt.day)]
            iso = dt.isoformat()
            for a in assessments:
                if a["hsg_ref_window_start"] <= iso <= a["hsg_ref_window_end"]:
                    tags.append(f"Ref({a['patient_id']})")
                if a["approx_ovulation_date"] == iso:
                    tags.append(f"Ovul({a['patient_id']})")
            for ap in appointments:
                if ap["appointment_date"] == iso:
                    tags.append(f"BOOKED:{ap['patient_id']}@{ap['time_slot']}")
            row[day_label] = " | ".join(tags)
        cal_rows.append(row)
    st.dataframe(pd.DataFrame(cal_rows), hide_index=True)

    if role in ("Administrator", "Healthcare Professional", "Radiology Staff") and assessments:
        with st.form("book_apt"):
            st.subheader("Record Clinician-Approved Appointment")
            aid = st.selectbox("Assessment Reference", [a["id"] for a in assessments])
            adate = st.date_input("Approved Appointment Date", value=today)
            slot = st.text_input("Time Slot", value="09:30 WAT")
            if st.form_submit_button("Record Booked Appointment"):
                asm_obj = next(a for a in assessments if a["id"] == aid)
                apt_id = f"MC-APT-{uuid.uuid4().hex[:5].upper()}"
                sb.table("hsg_appointments").insert({
                    "id": apt_id,
                    "patient_id": asm_obj["patient_id"],
                    "assessment_id": aid,
                    "appointment_date": adate.isoformat(),
                    "time_slot": slot.strip(),
                    "approved_by_clinician": asm_obj.get("reviewed_by") or profile["full_name"],
                    "scheduled_by": profile["full_name"],
                }).execute()
                log_audit(sb, profile, "APPOINTMENT_BOOKED", apt_id, {"assessment_id": aid})
                st.success("Appointment recorded.")
                st.rerun()

elif page == "Assessment History & PDF Reports":
    st.title("Assessment History & Printable Medical Reports")
    if not assessments:
        st.info("No assessments recorded yet.")
    else:
        st.dataframe(pd.DataFrame(assessments)[["id", "patient_id", "assessment_date", "lmp_date", "calculated_cycle_day", "hsg_ref_window_start", "hsg_ref_window_end", "review_status", "reviewed_by"]], hide_index=True)
        rep_id = st.selectbox("Select Assessment for PDF Medical Report", [a["id"] for a in assessments])
        rep_asm = next(a for a in assessments if a["id"] == rep_id)
        pdf_bytes = build_pdf_bytes(rep_asm, patient_by_id.get(rep_asm["patient_id"]))
        st.download_button(
            label=f"Download Medical Report PDF ({rep_id})",
            data=pdf_bytes,
            file_name=f"MediCycle_Report_{rep_id}.pdf",
            mime="application/pdf",
        )

elif page == "Analytics & Statistics":
    st.title("Aggregated Clinical Analytics")
    st.caption("De-identified workflow statistics reflecting actual stored records.")
    if not assessments:
        st.info("No assessment data available yet.")
    else:
        df = pd.DataFrame(assessments)
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Review Status Distribution")
            st.bar_chart(df["review_status"].value_counts())
        with c2:
            st.subheader("Cycle Regularity Distribution")
            st.bar_chart(df["cycle_regularity"].value_counts())

elif page == "Settings & Audit Logs":
    st.title("Settings, Reference & Audit Logs")
    st.info(f"Clinical Reference Version: {CLINICAL_REF_VERSION} | {DISCLAIMER}")
    if role == "Administrator":
        st.subheader("Registered Clinical Users")
        users = (sb.table("app_users").select("full_name,email,role,department,is_active").execute().data) or []
        st.dataframe(pd.DataFrame(users), hide_index=True)

        st.subheader("Security & Clinical Audit Trail")
        logs = (sb.table("audit_logs").select("*").order("occurred_at", desc=True).limit(100).execute().data) or []
        if logs:
            st.dataframe(pd.DataFrame(logs)[["occurred_at", "actor_email", "actor_role", "action", "target_id"]], hide_index=True)
    else:
        st.warning("User management and audit logs are restricted to Administrators.")
