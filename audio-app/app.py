"""
ConsultBae assignment — Task 3: mini audio collection app.

A tiny Flask app where a person enters name + phone, records/uploads audio,
and we auto-extract duration, sample rate, bitrate, and loudness (dB) plus
a rough noise estimate. Every submission also creates/links a row in the
`people` table from Task 1 (matched by phone, same normalization logic as
merge.py) so the two tasks share one database.

Why ffprobe + pydub instead of just pydub: pydub can decode audio and give
you dBFS, but it does NOT reliably report the *container* bitrate for
compressed formats — that's metadata ffprobe reads straight from the file
headers. So we use ffprobe for duration/sample_rate/bitrate (exact, from
the file) and pydub for loudness + a crude noise estimate (computed from
the decoded waveform, since that's a property of the audio content, not
the container).

Run: python3 app.py, then open http://localhost:5000
"""

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, url_for
from pydub import AudioSegment

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR.parent / "merge" / "people.db"

ALLOWED_EXT = {"wav", "webm", "mp3", "m4a", "ogg"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB cap per upload


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def norm_phone(raw):
    """Same normalization rule as merge.py: digits only, last 10 digits,
    so a submission from this app matches an existing person from Task 1."""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def find_or_create_person(db, name, phone):
    norm = norm_phone(phone)
    row = db.execute("SELECT person_id FROM people WHERE phone = ?", (norm,)).fetchone()
    if row:
        return row["person_id"]
    cur = db.execute(
        "INSERT INTO people (full_name, phone, sources) VALUES (?, ?, ?)",
        (name, norm, "audio_app"),
    )
    db.commit()
    return cur.lastrowid


def ffprobe_metadata(filepath):
    """Pull exact duration, sample rate, and bitrate straight from the file
    via ffprobe (works for wav/mp3/webm/m4a/ogg alike)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(filepath),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    audio_stream = next(s for s in data["streams"] if s["codec_type"] == "audio")
    duration = float(data["format"].get("duration") or audio_stream.get("duration") or 0)
    sample_rate = int(audio_stream.get("sample_rate") or 0)
    # bit_rate may live on the stream or only on the overall format
    bitrate_bps = audio_stream.get("bit_rate") or data["format"].get("bit_rate")
    bitrate_kbps = round(int(bitrate_bps) / 1000, 1) if bitrate_bps else None
    return duration, sample_rate, bitrate_kbps


def analyze_loudness_and_noise(filepath):
    """Decode with pydub and compute:
    - loudness_db: overall dBFS (how loud the clip is, relative to max = 0dB)
    - noise_estimate: a rough label from the gap between average loudness and
      peak loudness (crest factor). A clean, well-spoken clip has a fairly
      steady level; a lot of background hiss/noise flattens that gap.
      This is a heuristic, not a real noise-floor measurement — flagged as
      such in the report/README, this is the "bonus" ask, not a lab-grade
      SNR calculation.
    """
    audio = AudioSegment.from_file(filepath)
    loudness_db = audio.dBFS  # average loudness
    peak_db = audio.max_dBFS
    crest = peak_db - loudness_db if loudness_db != float("-inf") else 0
    if loudness_db == float("-inf"):
        noise_estimate = "silent/empty clip"
    elif crest < 6:
        noise_estimate = "likely noisy (flat level, little headroom between avg and peak)"
    elif crest < 12:
        noise_estimate = "moderate"
    else:
        noise_estimate = "clean (good dynamic range)"
    return round(loudness_db, 1) if loudness_db != float("-inf") else None, noise_estimate


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/submissions")
def submissions():
    db = get_db()
    rows = db.execute(
        """SELECT a.*, p.full_name AS person_name FROM audio_submissions a
           LEFT JOIN people p ON a.person_id = p.person_id
           ORDER BY a.submitted_at DESC"""
    ).fetchall()
    return render_template("submissions.html", rows=rows)


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/api/submit", methods=["POST"])
def api_submit():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    audio_file = request.files.get("audio")

    if not name or not phone:
        return jsonify({"error": "Name and phone are required"}), 400
    if not audio_file or audio_file.filename == "":
        return jsonify({"error": "Audio file is required"}), 400

    ext = audio_file.filename.rsplit(".", 1)[-1].lower() if "." in audio_file.filename else "webm"
    if ext not in ALLOWED_EXT:
        ext = "webm"  # browser MediaRecorder blobs often arrive without a clean extension

    fname = f"{uuid.uuid4().hex}.{ext}"
    fpath = UPLOAD_DIR / fname
    audio_file.save(fpath)

    try:
        duration, sample_rate, bitrate_kbps = ffprobe_metadata(fpath)
        loudness_db, noise_estimate = analyze_loudness_and_noise(fpath)
    except Exception as e:
        fpath.unlink(missing_ok=True)
        return jsonify({"error": f"Could not process audio: {e}"}), 400

    db = get_db()
    person_id = find_or_create_person(db, name, phone)
    db.execute(
        """INSERT INTO audio_submissions
           (person_id, submitted_name, submitted_phone, filename, filepath,
            duration_sec, sample_rate_hz, bitrate_kbps, loudness_db, noise_estimate)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (person_id, name, norm_phone(phone), fname, str(fpath),
         round(duration, 2), sample_rate, bitrate_kbps, loudness_db, noise_estimate),
    )
    db.commit()

    return jsonify({
        "ok": True,
        "duration_sec": round(duration, 2),
        "sample_rate_hz": sample_rate,
        "bitrate_kbps": bitrate_kbps,
        "loudness_db": loudness_db,
        "noise_estimate": noise_estimate,
    })



# ---------------------------------------------------------------------------
# Small API for the n8n automation (Task 2) to call.
# One flow, picked from the assignment's menu: an LLM step auto-tags each
# person's skill category from their merged `skills` column and writes the
# result back to `people.skill_category`. n8n has no native SQLite node, so
# it talks to the app over plain HTTP instead — see automation/README.md.
# ---------------------------------------------------------------------------

@app.route("/api/people/untagged")
def api_people_untagged():
    """People with skills on file but no skill_category yet — what the n8n
    flow should process this run."""
    db = get_db()
    rows = db.execute(
        "SELECT person_id, full_name, skills FROM people "
        "WHERE skills IS NOT NULL AND skills != '' AND skill_category IS NULL"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/people/<int:person_id>/tag", methods=["POST"])
def api_tag_person(person_id):
    """n8n posts back {"skill_category": "..."} after the LLM step classifies."""
    payload = request.get_json(force=True, silent=True) or {}
    category = (payload.get("skill_category") or "").strip()
    if not category:
        return jsonify({"error": "skill_category is required"}), 400
    db = get_db()
    db.execute("UPDATE people SET skill_category = ? WHERE person_id = ?", (category, person_id))
    db.commit()
    return jsonify({"ok": True, "person_id": person_id, "skill_category": category})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
