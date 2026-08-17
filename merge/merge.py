"""
ConsultBae assignment — Task 1: Merge pipeline.

Ingests 3 messy CSVs (Naukri applicants, gig workers, CBNexus contacts),
normalizes every field, resolves the same real person across sources into
ONE record, and loads the result into a single SQLite database.

Design notes (read this before you touch matching logic):
- No single ID is common across all 3 files, so we build a match key out of
  normalized PHONE first (most reliable: digits-only, last 10 digits, since
  every source sometimes includes +91/91/0 prefixes) and fall back to
  normalized EMAIL (lowercased, trimmed) when phone is missing.
- If neither phone nor email lines up, we do NOT silently merge. We fall
  back to a fuzzy name+city similarity check (rapidfuzz) and only ever
  *flag* those as "possible_duplicate_needs_review" in issues_found.md —
  we never auto-merge on name alone, because two different people can
  share a name (this actually happens in this dataset — see Arjun Mehta
  and Deepak Nair in the issues report).
- Every cleaning decision that fires is logged to `issues found` list and
  dumped to merge/issues_found.md, which is what Task 4's report is built
  from (not written free-hand afterwards).

Run: python3 merge.py   (from the merge/ directory)
Produces: merge/people.db, merge/issues_found.md
"""

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH = Path(__file__).parent / "people.db"
ISSUES_PATH = Path(__file__).parent / "issues_found.md"

issues = []  # list of (category, detail) tuples, built as we go


def log_issue(category, detail):
    issues.append((category, detail))


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

CITY_ALIASES = {
    "gurgaon": "Gurugram",
    "gurugram": "Gurugram",
    "new delhi": "Delhi",
    "delhi ncr": "Delhi",
    "delhi": "Delhi",
    "bangalore": "Bengaluru",
    "bengaluru": "Bengaluru",
    "pune": "Pune",
    "noida": "Noida",
}


def norm_phone(raw):
    """Strip everything but digits, then take the last 10 (Indian mobile
    numbers are 10 digits; sources variously prefix +91, 91, or a leading 0)."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) < 10:
        return None
    return digits[-10:]


def norm_email(raw):
    if raw is None:
        return None
    e = str(raw).strip().lower()
    return e if "@" in e else None


def norm_city(raw):
    if raw is None:
        return None
    c = str(raw).strip().lower()
    c = re.sub(r"\s+", " ", c)
    canonical = CITY_ALIASES.get(c)
    if canonical:
        if canonical != c.title() and c.title() not in (canonical,):
            log_issue(
                "City name inconsistency",
                f"'{raw}' normalized to canonical '{canonical}' "
                f"(aliases like Gurgaon/Gurugram, Delhi/New Delhi/Delhi NCR, "
                f"Bangalore/Bengaluru all refer to the same city)",
            )
        return canonical
    return c.title()


def norm_name(raw):
    if raw is None:
        return None
    n = re.sub(r"\s+", " ", str(raw).strip())
    # Title-case unless it's an initial like "R." which we leave alone
    return n.title() if not re.match(r"^[A-Z]\.\s", n) else n


def norm_ctc(raw):
    """Source1 'Current CTC' mixes two units: plain annual rupees (e.g.
    417964) and lakhs-per-annum as a small decimal (e.g. 4.2 meaning
    4.2 LPA = 420000). We detect the small-decimal case and convert."""
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val < 1000:  # can't be a real annual rupee salary -> it's LPA
        log_issue(
            "CTC unit inconsistency",
            f"Value {raw} in 'Current CTC' is too small to be annual INR — "
            f"interpreted as lakhs-per-annum and converted to {val * 100000:.0f}",
        )
        return round(val * 100000)
    return round(val)


DATE_FORMATS = [
    "%d-%m-%Y",   # 24-07-2026
    "%Y-%m-%d",   # 2026-08-08
    "%d %b %Y",   # 7 Jul 2026
    "%m/%d/%Y",   # 07/13/2026  (month first, since day 13 disambiguates it)
]


def norm_date(raw):
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    log_issue("Unparseable date", f"'{raw}' in Applied Date did not match any known format")
    return None


def norm_skills(raw):
    """Skills/skill_tags come in with mixed case and mixed spacing across
    sources ('REST APIs' vs 'rest apis'). Canonicalize to a fixed casing
    per known skill so the same skill from different sources is the same
    string, then re-title for display."""
    if not raw:
        return set()
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    canon = {}
    for p in parts:
        key = p.lower()
        canon.setdefault(key, p)
    # Prefer a single canonical spelling per skill regardless of source casing
    KNOWN = {
        "n8n": "n8n", "langchain": "LangChain", "rest apis": "REST APIs",
        "mongodb": "MongoDB", "sql": "SQL", "docker": "Docker",
        "zapier": "Zapier", "javascript": "JavaScript", "react": "React",
        "mysql": "MySQL", "python": "Python", "selenium": "Selenium",
        "web scraping": "Web Scraping", "fastapi": "FastAPI",
        "pandas": "Pandas",
    }
    return {KNOWN.get(k, v.title()) for k, v in canon.items()}


def norm_rate(raw):
    """source2 'rate' mixes '<n>/hr' and '<n>k/month'. Normalize both to an
    estimated hourly rate (assuming ~160 working hours/month) so rates are
    comparable, and keep the original for audit."""
    if not raw:
        return None, None, None
    raw = str(raw).strip()
    m = re.match(r"^([\d.]+)/hr$", raw)
    if m:
        return float(m.group(1)), "hourly", raw
    m = re.match(r"^([\d.]+)k/month$", raw)
    if m:
        monthly = float(m.group(1)) * 1000
        hourly = round(monthly / 160, 2)
        log_issue(
            "Rate unit inconsistency",
            f"'{raw}' is a monthly rate, converted to hourly-equivalent {hourly}/hr "
            f"(assuming 160 working hrs/month) so it's comparable to hourly rows",
        )
        return hourly, "monthly_converted_to_hourly", raw
    log_issue("Unparseable rate", f"'{raw}' in gig_workers.rate did not match known patterns")
    return None, None, raw


def norm_bool_verified(raw):
    if raw is None:
        return None
    v = str(raw).strip().lower()
    if v in ("y", "yes", "true", "1"):
        return "Yes"
    if v in ("n", "no", "false", "0"):
        return "No"
    log_issue("Unrecognized 'Verified' value", f"'{raw}' in CBNexus did not map to Yes/No")
    return None


# ---------------------------------------------------------------------------
# Source loaders — each returns a list of clean dicts, one per real row
# ---------------------------------------------------------------------------

def load_source1():
    """Naukri applicants."""
    rows = []
    seen_by_key = {}  # (phone or email) -> row, to catch in-source duplicates
    with open(DATA_DIR / "source1_naukri_applicants.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            phone = norm_phone(r["Phone"])
            email = norm_email(r["Email"])
            key = phone or email
            rec = {
                "full_name": norm_name(r["Full Name"]),
                "email": email,
                "phone": phone,
                "city": norm_city(r["City"]),
                "experience_years": float(r["Experience (Years)"]) if r["Experience (Years)"] else None,
                "current_ctc_annual_inr": norm_ctc(r["Current CTC"]),
                "applied_date": norm_date(r["Applied Date"]),
                "skills": norm_skills(r["Skills"]),
                "source": "naukri",
            }
            if key in seen_by_key:
                prev = seen_by_key[key]
                log_issue(
                    "Duplicate row within source1 (same person, two applications)",
                    f"'{prev['full_name']}' <{prev['email']}> and '{rec['full_name']}' <{rec['email']}> "
                    f"share phone {phone} — kept one record, merged skills, kept fuller name",
                )
                # keep the longer/fuller-looking name, union the skills
                if len(rec["full_name"] or "") > len(prev["full_name"] or ""):
                    prev["full_name"] = rec["full_name"]
                prev["skills"] |= rec["skills"]
                prev["email"] = prev["email"] or rec["email"]
                continue
            seen_by_key[key] = rec
            rows.append(rec)
    return rows


def load_source2():
    """Gig workers — includes a blank row and a column-shifted row we must repair."""
    rows = []
    seen_by_key = {}
    with open(DATA_DIR / "source2_gig_workers.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for raw_row in reader:
            if all(v.strip() == "" for v in raw_row):
                log_issue("Blank row in source2", "Fully empty row found and dropped")
                continue
            r = dict(zip(header, raw_row))
            # Detect the column-shifted row: email_id field has no '@' but
            # does contain comma-joined skill text -> columns rotated by one.
            if r["email_id"] and "@" not in r["email_id"] and "," in r["email_id"]:
                log_issue(
                    "Column-shifted row in source2",
                    f"Row starting with '{r['email_id'][:30]}...' has values shifted left by one "
                    f"column (skill_tags value landed in email_id). Realigned columns before parsing.",
                )
                vals = list(r.values())
                # observed shift: [skill_tags, email_id, worker_name, rate, location, status]
                skill_tags, email_id, worker_name, rate, location, status = vals
                r = {
                    "email_id": email_id, "worker_name": worker_name, "rate": rate,
                    "location": location, "status": status, "skill_tags": skill_tags,
                }
            email = norm_email(r["email_id"])
            rate_amt, rate_unit, rate_raw = norm_rate(r["rate"])
            rec = {
                "full_name": norm_name(r["worker_name"]),
                "email": email,
                "phone": None,
                "city": norm_city(r["location"]),
                "gig_rate_amount": rate_amt,
                "gig_rate_unit": rate_unit,
                "gig_rate_raw": rate_raw,
                "gig_status": (r["status"] or "").strip().title() or None,
                "skills": norm_skills(r["skill_tags"]),
                "source": "gig",
            }
            key = email
            if key in seen_by_key:
                prev = seen_by_key[key]
                log_issue(
                    "Duplicate row within source2",
                    f"Email {email} appears twice (once as the column-shifted row) — merged into one record",
                )
                prev["skills"] |= rec["skills"]
                continue
            seen_by_key[key] = rec
            rows.append(rec)
    return rows


def load_source3():
    """CBNexus contacts — file has a second header row pasted in mid-file
    (looks like two exports concatenated) that must be dropped, not treated
    as data."""
    rows = []
    with open(DATA_DIR / "source3_cbnexus_contacts.csv", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        for raw_row in reader:
            if raw_row == header:
                log_issue(
                    "Repeated header row in source3",
                    "A second header row was pasted mid-file (two exports concatenated) — dropped, not ingested as a person",
                )
                continue
            r = dict(zip(header, raw_row))
            phone = norm_phone(r["Phone Number"])
            rec = {
                "full_name": norm_name(r["Name"]),
                "email": None,
                "phone": phone,
                "city": norm_city(r["City"]),
                "cbnexus_verified": norm_bool_verified(r["Verified"]),
                "cbnexus_projects_completed": int(r["Projects Completed"]) if r["Projects Completed"] else None,
                "source": "cbnexus",
            }
            rows.append(rec)
    # Note: unlike sources 1 & 2, we deliberately do NOT dedupe same-name
    # rows here against each other blindly — two different real people can
    # share a name (see Arjun Mehta below), so within-source dedup for this
    # file is done via exact phone match only.
    seen = {}
    deduped = []
    for rec in rows:
        key = rec["phone"]
        if key and key in seen:
            log_issue(
                "Duplicate row within source3 (same phone)",
                f"Phone {key} appears twice for '{rec['full_name']}' — merged",
            )
            continue
        if key:
            seen[key] = rec
        deduped.append(rec)
    return deduped


# ---------------------------------------------------------------------------
# Cross-source entity resolution
# ---------------------------------------------------------------------------

def resolve(s1, s2, s3):
    """Merge the three cleaned row-lists into one list of unified people.

    Strategy: index everything by normalized phone. Where phone is absent
    (source2 has no phone field at all), fall back to email. Anything that
    doesn't match on phone or email stays a separate record — we log a
    fuzzy-name-similarity check purely as a flag for human review, we never
    silently merge on name alone.
    """
    merged = {}  # match_key -> unified dict
    order = []

    def get_or_create(key):
        if key not in merged:
            merged[key] = {
                "full_name": None, "email": None, "phone": None, "city": None,
                "experience_years": None, "current_ctc_annual_inr": None,
                "applied_date": None, "gig_rate_amount": None, "gig_rate_unit": None,
                "gig_status": None, "cbnexus_verified": None,
                "cbnexus_projects_completed": None, "skills": set(),
                "sources": set(),
            }
            order.append(key)
        return merged[key]

    def merge_into(target, rec):
        for f in ("full_name", "email", "phone", "city", "experience_years",
                   "current_ctc_annual_inr", "applied_date", "gig_rate_amount",
                   "gig_rate_unit", "gig_status", "cbnexus_verified",
                   "cbnexus_projects_completed"):
            if rec.get(f) not in (None, "") and target.get(f) in (None, ""):
                target[f] = rec[f]
        target["skills"] |= rec.get("skills", set())
        target["sources"].add(rec["source"])

    # Source1 seeds the base set of people (has both phone & email)
    phone_index = {}
    email_index = {}
    for rec in s1:
        key = ("phone", rec["phone"]) if rec["phone"] else ("email", rec["email"])
        t = get_or_create(key)
        merge_into(t, rec)
        if rec["phone"]:
            phone_index[rec["phone"]] = key
        if rec["email"]:
            email_index[rec["email"]] = key

    # Source2 only has email -> match on email, else new record
    for rec in s2:
        key = email_index.get(rec["email"])
        if key is None:
            key = ("email", rec["email"])
        t = get_or_create(key)
        merge_into(t, rec)
        if rec["email"]:
            email_index[rec["email"]] = key

    # Source3 only has phone -> match on phone, else new record.
    # Also try a cautious fuzzy name+city check against unmatched people,
    # purely to log as a review flag (does not auto-merge).
    for rec in s3:
        key = phone_index.get(rec["phone"]) if rec["phone"] else None
        if key is None:
            # fuzzy check against existing merged people for a possible-dup flag
            for existing_key in order:
                cand = merged[existing_key]
                if not cand["full_name"] or not rec["full_name"]:
                    continue
                score = fuzz.ratio(cand["full_name"].lower(), rec["full_name"].lower())
                if score > 90 and cand["city"] == rec["city"] and cand["phone"] != rec["phone"]:
                    log_issue(
                        "Possible duplicate needs review (name+city match, phone differs)",
                        f"CBNexus '{rec['full_name']}' (phone {rec['phone']}, {rec['city']}) looks similar to "
                        f"already-merged '{cand['full_name']}' (phone {cand['phone']}) but phones don't match — "
                        f"kept as a SEPARATE record rather than risk a false merge",
                    )
            key = ("phone", rec["phone"]) if rec["phone"] else ("cbnexus_only", rec["full_name"])
        t = get_or_create(key)
        merge_into(t, rec)
        if rec["phone"]:
            phone_index[rec["phone"]] = key

    people = []
    for key in order:
        p = merged[key]
        p["skills"] = ", ".join(sorted(p["skills"]))
        p["sources"] = ", ".join(sorted(p["sources"]))
        people.append(p)
    return people


# ---------------------------------------------------------------------------
# SQLite load
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    person_id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT,
    email TEXT,
    phone TEXT,
    city TEXT,
    experience_years REAL,
    current_ctc_annual_inr REAL,
    applied_date TEXT,
    gig_rate_amount REAL,
    gig_rate_unit TEXT,
    gig_status TEXT,
    cbnexus_verified TEXT,
    cbnexus_projects_completed INTEGER,
    skills TEXT,
    sources TEXT,
    skill_category TEXT,          -- filled in later by the n8n automation (Task 2)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audio_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER REFERENCES people(person_id),
    submitted_name TEXT,
    submitted_phone TEXT,
    filename TEXT,
    filepath TEXT,
    duration_sec REAL,
    sample_rate_hz INTEGER,
    bitrate_kbps REAL,
    loudness_db REAL,
    noise_estimate TEXT,
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def load_to_sqlite(people):
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    cur = conn.cursor()
    for p in people:
        cur.execute(
            """INSERT INTO people (full_name, email, phone, city, experience_years,
               current_ctc_annual_inr, applied_date, gig_rate_amount, gig_rate_unit,
               gig_status, cbnexus_verified, cbnexus_projects_completed, skills, sources)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["full_name"], p["email"], p["phone"], p["city"], p["experience_years"],
             p["current_ctc_annual_inr"], p["applied_date"], p["gig_rate_amount"],
             p["gig_rate_unit"], p["gig_status"], p["cbnexus_verified"],
             p["cbnexus_projects_completed"], p["skills"], p["sources"]),
        )
    conn.commit()
    conn.close()


def write_issues_report(people, s1, s2, s3):
    with open(ISSUES_PATH, "w") as f:
        f.write("# Data Issues Found & How They Were Handled\n\n")
        f.write(f"Auto-generated by `merge.py` on {datetime.now().isoformat(timespec='seconds')}.\n")
        f.write(f"Every issue below was actually detected while parsing, not written after the fact.\n\n")
        f.write(f"**Summary:** {len(s1)} clean rows from source1, {len(s2)} from source2, "
                f"{len(s3)} from source3 → **{len(people)} unified people** after merge.\n\n")
        by_cat = {}
        for cat, detail in issues:
            by_cat.setdefault(cat, []).append(detail)
        for cat, details in by_cat.items():
            f.write(f"## {cat} ({len(details)})\n\n")
            for d in details:
                f.write(f"- {d}\n")
            f.write("\n")
    print(f"Wrote {ISSUES_PATH} ({len(issues)} issues logged)")


def main():
    s1 = load_source1()
    s2 = load_source2()
    s3 = load_source3()
    people = resolve(s1, s2, s3)
    load_to_sqlite(people)
    write_issues_report(people, s1, s2, s3)
    print(f"Loaded {len(people)} unified people into {DB_PATH}")
    multi_source = sum(1 for p in people if "," in p["sources"])
    print(f"{multi_source} people were matched across 2+ sources")


if __name__ == "__main__":
    main()
