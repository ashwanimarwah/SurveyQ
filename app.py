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
    # ONLY_SURVEY may list several slugs ("chase,opt"); "/" and "/admin" go to the first one.
    if not ONLY_SURVEY:
        return None
    allowed = [x.strip() for x in ONLY_SURVEY.split(",") if x.strip()]
    p = request.path.rstrip("/") or "/"
    from flask import redirect
    if p == "/":
        qs = request.query_string.decode()
        return redirect(f"/{allowed[0]}" + (f"?{qs}" if qs else ""), 302)
    if p == "/admin":
        return redirect(f"/admin/{allowed[0]}", 302)
    if p == "/healthz":
        return None
    for s in allowed:
        if (p == f"/{s}" or p.startswith(f"/api/{s}/") or p == f"/admin/{s}" or p.startswith(f"/admin/{s}/")
                or p.startswith(f"/static/{s}/")):
            return None
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
app.register_blueprint(make_survey(slug="resto", title="Independent Industry Research – Canada", page="resto.html", default_quota=300))
# USA: H&R Block DIY customers on Free or Deluxe plans, GenPop 18+ (n=200) at /hrb
app.register_blueprint(make_survey(slug="hrb", title="H&R Block DIY Customer Survey (Free/Deluxe)", page="hrb.html", default_quota=200))
# Chase Media Solutions A&U Tracker, Wave 3: US merchant/agency digital marketing decision makers (n=500) at /chase
_CHASE_IND = {11: "Big Box Retail", 12: "E-commerce & Subscription", 13: "Specialty Retail", 3: "Fashion / Clothing / Apparel", 6: "Grocery",
              16: "Food & Beverage", 4: "Gas / Convenience", 7: "Health & Wellness", 18: "Travel & Leisure", 17: "Tech / Telco / B2B",
              8: "Media & Entertainment", 14: "Luxury Retail", 19: "Financial Services (monitor)"}
app.register_blueprint(make_survey(
    slug="chase", title="Chase Media Solutions A&U Tracker – Wave 3", page="chase.html", default_quota=500,
    cell_caps={**{f"ind_{k}": 50 for k in _CHASE_IND if k != 19}, "agency": 125, "b2b": 50},
    cell_labels={**{f"ind_{k}": f"Industry: {v}" for k, v in _CHASE_IND.items()}, "agency": "Agency (S5=1)", "company": "Consumer/Business-facing (S5=2)",
                 "b2b": "B2B end user (S14=2) – min 25", "clo_nonuser": "Non-users of CLOs (monitor)",
                 "exp1to5_manager": "1–5 yrs experience, Manager (monitor)", "exp1to5_analyst": "1–5 yrs experience, Analyst (monitor)"}))

# Questionlab x Optivate: Peligroso microgame vs. sweepstakes, two monadic cells (n=400) at /opt
_OPT_REG = {"South": 76, "West": 48, "Midwest": 42, "Northeast": 34}
_opt_caps = {}
_opt_labels = {}
for _arm, _nm in ((1, "Cell A (Microgame)"), (2, "Cell B (Sweepstakes)")):
    _opt_caps[f"cell{_arm}"] = 200
    _opt_labels[f"cell{_arm}"] = _nm
    for _g in ("male", "female"):
        _opt_caps[f"c{_arm}_{_g}"] = 100
        _opt_labels[f"c{_arm}_{_g}"] = f"{_nm} – {_g.title()}"
    for _r, _n in _OPT_REG.items():
        _opt_caps[f"c{_arm}_{_r}"] = _n
        _opt_labels[f"c{_arm}_{_r}"] = f"{_nm} – {_r}"
app.register_blueprint(make_survey(
    slug="opt", title="Optivate Peligroso microgame test", page="opt.html", default_quota=400,
    cell_caps=_opt_caps, cell_labels=_opt_labels, public_env=("GAME_URL", "FORM_URL", "RETURN_MODE")))


# Chuckit! color assortment (Dog owners): Gen pop n=400 first, then Chuckit! buyers n=400, at /clh
_CLH_REG = ("West", "South", "Midwest", "Northeast")
_CLH_AG = {"ym": ("Age 18-44 Male", 24), "yf": ("Age 18-44 Female", 36), "om": ("Age 45+ Male", 16), "of": ("Age 45+ Female", 24)}
_clh_caps = {"gp": 400, "by": 400}
_clh_labels = {"gp": "TARGET 1 – Gen pop (total)", "by": "TARGET 2 – Chuckit! buyers (total)"}
for _r in _CLH_REG:
    _clh_caps[f"gp_{_r}"] = 100
    _clh_labels[f"gp_{_r}"] = f"Gen pop – {_r}"
    for _k, (_nm, _n) in _CLH_AG.items():
        _clh_caps[f"gp_{_r}_{_k}"] = _n
        _clh_labels[f"gp_{_r}_{_k}"] = f"Gen pop – {_r} – {_nm}"
for _r in _CLH_REG:
    _clh_caps[f"by_{_r}"] = 100
    _clh_labels[f"by_{_r}"] = f"Buyers – {_r}"
    for _k, (_nm, _n) in _CLH_AG.items():
        _clh_caps[f"by_{_r}_{_k}"] = 55   # leaves room for the min 15 in each of the other three cells
        _clh_labels[f"by_{_r}_{_k}"] = f"Buyers – {_r} – {_nm} (min 15)"


def _clh_phase(counts, cap_for, env):
    phase = (env("PHASE") or "sequential").lower()
    stage = phase
    if phase == "sequential":
        stage = "buyers" if counts.get("gp", 0) >= (cap_for("gp") or 400) else "genpop"
    return {"phase": phase, "stage": stage}


app.register_blueprint(make_survey(
    slug="clh", title="Chuckit! Color Assortment – Dog Owners", page="clh.html", default_quota=800,
    cell_caps=_clh_caps, cell_labels=_clh_labels, extra_config=_clh_phase))

@app.get("/api/opt/mock")
def opt_mock():
    """Stand-in for the Optivate game / sweepstakes until OPT_GAME_URL and OPT_FORM_URL are set (QA only)."""
    from html import escape as _esc
    cell = request.args.get("cell", "1")
    ret = request.args.get("return", "")
    if not ret.startswith(("/", "http://localhost", "https://localhost")) and request.host not in ret:
        ret = ""
    kind = "game" if cell == "1" else "sweepstakes entry"
    back = (ret + ("&" if "?" in ret else "?") + "status=complete&score=1234") if ret else ""
    return Response(f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Test stand-in</title><body style="font-family:system-ui,sans-serif;padding:24px;max-width:520px">
<p style="color:#666">TEST STAND-IN – replaced by Optivate's real {_esc(kind)} once its link is set</p>
<h1>Peligroso {_esc(kind)}</h1><p>This placeholder simulates the {_esc(kind)} so the survey flow can be tested.</p>
<p><a id=done href="{_esc(back)}" style="display:inline-block;background:#2f4f4a;color:#fff;padding:12px 20px;border-radius:999px;text-decoration:none">Finish</a></p>""",
                    mimetype="text/html")



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
