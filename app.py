"""
India School Education Survey: self-hosted server.

Serves the survey page and saves every response (completes and terminates)
as one row in a CSV file. Includes an admin page and CSV download.

Run:
    pip install flask
    ADMIN_PASSWORD=change-me python app.py
Then open http://localhost:8000/?rid=TEST001  (survey)
       and http://localhost:8000/admin       (results; user "admin")

Optional environment variables
    RESPONSES_FILE      CSV file the responses are written to
                        (default: data/responses.csv next to this file)
    ADMIN_PASSWORD      password for /admin (required to open the admin page)
    REDIRECT_COMPLETE   panel redirect after a complete, e.g. https://panel.example/complete?rid={rid}
    REDIRECT_TERMINATE  panel redirect after a screen-out, e.g. https://panel.example/term?rid={rid}
    REQUIRE_RID         "1" to refuse entry without a ?rid= respondent id
    PORT                default 8000
"""
import csv
import hmac
import io
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from html import escape

try:
    import fcntl  # file locking on Linux/macOS, safe with several gunicorn workers
except ImportError:  # Windows
    fcntl = None

from flask import Flask, Response, abort, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
# On Railway, attach a Volume: Railway sets RAILWAY_VOLUME_MOUNT_PATH and responses are kept there,
# so they survive redeploys and restarts.
_DEFAULT_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(BASE, "data")
RESPONSES_FILE = os.environ.get("RESPONSES_FILE", os.path.join(_DEFAULT_DIR, "responses.csv"))
LOCK_FILE = RESPONSES_FILE + ".lock"
PAGE_PATH = os.path.join(BASE, "static", "survey.html")

CSV_COLUMNS = [
    "respondent_id", "status", "terminate_reason", "flags", "started_at", "completed_at", "duration_sec",
    "q1", "q1_label", "q2", "q3", "q3_label", "q3_other", "q4", "q5", "q5_label", "q5_other", "q6", "q6_label",
    "q7", "q7_label", "q8", "q8_label", "q9", "q9_label", "q10", "q10_label", "q10_other",
    "q11", "q11_label", "q12", "q12_label", "q12_other", "source", "received_at", "user_agent",
]
ANSWER_KEYS = [c for c in CSV_COLUMNS if c not in ("respondent_id", "source", "received_at", "user_agent")]
VALID_STATUS = {"complete", "terminated"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# ONLY_SURVEY=<slug> (e.g. "de") turns this deployment into a single-survey service: "/" goes to that
# survey and every other survey's pages and data are unreachable from it.
ONLY_SURVEY = os.environ.get("ONLY_SURVEY", "").strip().lower()


# DISABLED_SURVEYS=de,gc switches surveys off on this deployment. If <SLUG>_MOVED_TO is set (e.g.
# DE_MOVED_TO=https://surveyq-de-production.up.railway.app), the survey page redirects there, keeping ?rid=.
DISABLED_SURVEYS = {s.strip().lower() for s in os.environ.get("DISABLED_SURVEYS", "").split(",") if s.strip()}


@app.before_request
def _disabled_surveys():
    p = request.path.rstrip("/") or "/"
    for s in DISABLED_SURVEYS:
        if p == f"/{s}":
            target = os.environ.get(f"{s.upper()}_MOVED_TO", "").rstrip("/")
            if target:
                from flask import redirect
                qs = request.query_string.decode()
                return redirect(target + "/" + (f"?{qs}" if qs else ""), 301)
            return Response("This survey is closed.", 404)
        if p.startswith(f"/api/{s}/") or p == f"/admin/{s}" or p.startswith(f"/admin/{s}/"):
            return Response("Not found", 404)
    return None


@app.before_request
def _only_one_survey():
    if not ONLY_SURVEY:
        return None
    p = request.path.rstrip("/") or "/"
    s = ONLY_SURVEY
    if p == "/":
        from flask import redirect
        qs = request.query_string.decode()
        return redirect(f"/{s}" + (f"?{qs}" if qs else ""), 302)
    if p == "/healthz" or p == f"/{s}" or p.startswith(f"/api/{s}/") or p == f"/admin/{s}" or p.startswith(f"/admin/{s}/"):
        return None
    if p == "/admin":
        from flask import redirect
        return redirect(f"/admin/{s}", 302)
    return Response("Not found", 404)
_thread_lock = threading.Lock()


# ---------------- file storage ----------------
@contextmanager
def locked():
    """Exclusive lock so two submissions never write the file at the same time."""
    with _thread_lock:
        os.makedirs(os.path.dirname(RESPONSES_FILE), exist_ok=True)
        with open(LOCK_FILE, "a") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_UN)


def read_rows():
    if not os.path.exists(RESPONSES_FILE):
        return []
    with open(RESPONSES_FILE, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def has_responded(rid):
    return any(r.get("respondent_id") == rid for r in read_rows())


def append_row(rec):
    new_file = not os.path.exists(RESPONSES_FILE) or os.path.getsize(RESPONSES_FILE) == 0
    # utf-8-sig writes a BOM only on a new file, so Excel shows Indian-language text correctly.
    with open(RESPONSES_FILE, "a", newline="", encoding="utf-8-sig" if new_file else "utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow(rec)
        f.flush()
        os.fsync(f.fileno())


# ---------------- survey ----------------
def wrap_page(config):
    with open(PAGE_PATH, encoding="utf-8") as f:
        body = f.read()
    body = body.replace("window.SURVEY_SERVER = null;", "window.SURVEY_SERVER = " + json.dumps(config) + ";", 1)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
        "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>"
        "</head><body>" + body + "</body></html>"
    )


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/")
def survey():
    rid = (request.args.get("rid") or "").strip()[:100]
    if os.environ.get("REQUIRE_RID") == "1" and not rid:
        return Response("<p>This survey link is missing a respondent id.</p>", 400)
    if rid and has_responded(rid):
        return Response("<p style='font-family:sans-serif;padding:24px'>You have already taken part in this survey. Thank you.</p>")
    return Response(wrap_page({"rid": rid, "showReason": os.environ.get("SHOW_REASON", "1") != "0"}), mimetype="text/html")


def _clean(v, limit=1000):
    if v is None:
        return ""
    # Neutralise spreadsheet formulas typed into text answers (CSV injection).
    s = str(v)[:limit].replace("\r", " ")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@app.post("/api/responses")
def save_response():
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in VALID_STATUS:
        abort(400, "invalid status")
    rec = {k: _clean(data.get(k)) for k in ANSWER_KEYS}
    rec["q4"] = rec["q4"][:500]
    try:
        rec["duration_sec"] = int(data.get("duration_sec") or 0)
    except (TypeError, ValueError):
        rec["duration_sec"] = 0
    rid = (str(data.get("rid") or "").strip()[:100]) or ("anon-" + uuid.uuid4().hex[:12])
    rec.update(
        respondent_id=_clean(rid),
        source="server",
        received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        user_agent=_clean(request.headers.get("User-Agent"), 300),
    )
    with locked():
        if has_responded(rec["respondent_id"]):
            return jsonify({"error": "duplicate"}), 409
        append_row(rec)

    target = os.environ.get("REDIRECT_COMPLETE" if status == "complete" else "REDIRECT_TERMINATE", "")
    return jsonify({"ok": True, "redirect": target.replace("{rid}", rid) if target else ""})


# ---------------- admin ----------------
def require_admin(fn):
    @wraps(fn)
    def inner(*a, **kw):
        pw = os.environ.get("ADMIN_PASSWORD")
        auth = request.authorization
        if not pw:
            return Response("Set the ADMIN_PASSWORD environment variable to use the admin page.", 503)
        if not auth or auth.username != "admin" or not hmac.compare_digest(auth.password or "", pw):
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Survey admin"'})
        return fn(*a, **kw)
    return inner


@app.get("/admin")
@require_admin
def admin():
    rows = list(reversed(read_rows()))
    comp = [r for r in rows if r.get("status") == "complete"]
    term = [r for r in rows if r.get("status") == "terminated"]
    flagged = [r for r in rows if r.get("flags")]
    trs = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            escape(r.get("received_at", "")), escape(r.get("respondent_id", "")), escape(r.get("status", "")),
            escape(r.get("q2", "")), escape(r.get("q3_label", "")), escape(r.get("q4", "")),
            escape(r.get("duration_sec", "")), escape(r.get("flags", "")).replace(" | ", "<br>"),
        ) for r in rows
    ) or "<tr><td colspan=8>No responses yet.</td></tr>"
    html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Survey admin</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#17242b}}table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #d8dfdd;padding:7px 9px;text-align:left;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#5a6a70}}
.t{{display:inline-block;margin-right:28px}}.t b{{font-size:26px;display:block}}a.btn{{display:inline-block;background:#1d4a3f;color:#fff;padding:8px 16px;border-radius:999px;text-decoration:none;margin:16px 0}}</style>
<h1>India School Education Survey: responses</h1>
<div><span class=t>Completes<b>{len(comp)}</b></span><span class=t>Terminated<b>{len(term)}</b></span><span class=t>Flagged<b>{len(flagged)}</b></span></div>
<a class=btn href="admin/export.csv">Download responses.csv</a>
<p style="color:#5a6a70;font-size:14px">Saved to: {escape(RESPONSES_FILE)}</p>
<div style="overflow-x:auto"><table><tr><th>Received (UTC)</th><th>Respondent</th><th>Status</th><th>State/UT</th><th>Role</th><th>Q4</th><th>LOI (s)</th><th>Flags</th></tr>{trs}</table></div>"""
    return Response(html, mimetype="text/html")


@app.get("/admin/export.csv")
@require_admin
def export_csv():
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in read_rows():
        w.writerow(r)
    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="school-education-survey-{stamp}.csv"'})


from golden_corral import bp as golden_corral_bp  # noqa: E402  (Golden Corral survey at /gc)
app.register_blueprint(golden_corral_bp)

from survey_blueprint import make_survey  # noqa: E402
# Germany: cash-only B2C small businesses, payments study (n=260) at /de
app.register_blueprint(make_survey(slug="de", title="Germany cash-only merchants survey", page="de-cash.html", default_quota=260))
# USA: general survey of men 18+ (n=500) at /men
app.register_blueprint(make_survey(slug="men", title="US men 18+ general survey", page="men.html", default_quota=500))
# Canada B2B: independent full-service restaurant owners/managers, Toronto/Montreal/Vancouver/Calgary (n=300+) at /resto
app.register_blueprint(make_survey(slug="resto", title="Canada independent restaurant operators survey", page="resto.html", default_quota=300))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
