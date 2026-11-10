"""
Golden Corral A&U survey (USA, n=500), served at /gc.

Screening: ZIP within 10 miles of a Golden Corral (gc_zips.json, built from the client's list),
age 18+, household income NOT $75,000-$149,999, no market research / restaurant industry.
Each response is one row in gc_responses.csv (same folder as the India survey's file).

Environment variables (all optional except ADMIN_PASSWORD for the admin page)
    GC_QUOTA                completes needed before the survey closes (default 500)
    GC_RESPONSES_FILE       CSV path (default: <volume or data>/gc_responses.csv)
    GC_REDIRECT_COMPLETE    panel redirect URLs; {rid} is replaced with the respondent id
    GC_REDIRECT_TERMINATE
    GC_REDIRECT_OVERQUOTA
    GC_REDIRECT_QUALITY     (falls back to GC_REDIRECT_TERMINATE)
    GC_REQUIRE_RID          "1" to refuse entry without ?rid=
"""
import csv
import hmac
import io
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from html import escape

try:
    import fcntl
except ImportError:
    fcntl = None

from flask import Blueprint, Response, abort, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(BASE, "data")
FILE = os.environ.get("GC_RESPONSES_FILE", os.path.join(_DIR, "gc_responses.csv"))
LOCK = FILE + ".lock"
PAGE = os.path.join(BASE, "static", "golden-corral.html")

with open(os.path.join(BASE, "gc_zips.json"), encoding="utf-8") as f:
    _Z = json.load(f)
ZIPS, STORES = _Z["zips"], _Z["stores"]

STATUSES = {"complete", "terminated", "quality", "overquota"}
HEAD = ["respondent_id", "status", "terminate_reason", "flags", "started_at", "completed_at", "duration_sec",
        "s1_zip", "gc_store_numbers", "gc_store_locations"]
TAIL = ["source", "received_at", "user_agent"]
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")

bp = Blueprint("gc", __name__)
_tlock = threading.Lock()


def SHOW_REASON():
    return os.environ.get("GC_SHOW_REASON", "1") != "0"


def quota():
    try:
        return int(os.environ.get("GC_QUOTA", "500"))
    except ValueError:
        return 500


@contextmanager
def locked():
    with _tlock:
        os.makedirs(os.path.dirname(FILE), exist_ok=True)
        with open(LOCK, "a") as lf:
            if fcntl:
                fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_UN)


def rows():
    if not os.path.exists(FILE):
        return []
    with open(FILE, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def completes(rs=None):
    return sum(1 for r in (rs if rs is not None else rows()) if r.get("status") == "complete")


def redirect_for(status, rid):
    key = {"complete": "GC_REDIRECT_COMPLETE", "terminated": "GC_REDIRECT_TERMINATE",
           "overquota": "GC_REDIRECT_OVERQUOTA", "quality": "GC_REDIRECT_QUALITY"}[status]
    url = os.environ.get(key) or (os.environ.get("GC_REDIRECT_TERMINATE") if status == "quality" else "")
    return url.replace("{rid}", rid) if url else ""


def clean(v, limit=500):
    if v is None or v is False:
        return ""
    if v is True:
        return 1
    if isinstance(v, (int, float)):
        return v
    s = str(v)[:limit].replace("\r", " ").replace("\n", " ")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def wrap(config):
    with open(PAGE, encoding="utf-8") as f:
        body = f.read().replace("window.SURVEY_SERVER = null;", "window.SURVEY_SERVER = " + json.dumps(config) + ";", 1)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
            "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>" + body + "</body></html>")


@bp.get("/gc")
def page():
    rid = (request.args.get("rid") or "").strip()[:100]
    if os.environ.get("GC_REQUIRE_RID") == "1" and not rid:
        return Response("<p>This survey link is missing a respondent id.</p>", 400)
    rs = rows()
    if rid and any(r.get("respondent_id") == rid for r in rs):
        return Response("<p style='font-family:sans-serif;padding:24px'>You have already taken part in this survey. Thank you.</p>")
    if completes(rs) >= quota():
        return Response(wrap({"rid": rid, "closed": True, "redirect": redirect_for("overquota", rid) if rid else ""}), mimetype="text/html")
    return Response(wrap({"rid": rid, "showReason": SHOW_REASON()}), mimetype="text/html")


@bp.post("/api/gc/zip")
def zip_check():
    z = str((request.get_json(silent=True) or {}).get("zip", "")).strip()
    return jsonify({"eligible": z in ZIPS, "quotaFull": completes() >= quota()})


@bp.post("/api/gc/responses")
def save():
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in STATUSES:
        abort(400, "invalid status")
    rid = (str(data.get("rid") or "").strip()[:100]) or ("anon-" + uuid.uuid4().hex[:12])
    rec = {}
    for k, v in data.items():
        if k == "rid" or not KEY_RE.match(k) or len(rec) > 300:
            continue
        rec[k] = clean(v)
    z = str(data.get("s1_zip", "")).strip()
    # Never trust the browser on screening: re-check the ZIP here.
    if status == "complete" and z not in ZIPS:
        status, rec["terminate_reason"] = "terminated", "S1: ZIP not in target area (server check)"
    stores = ZIPS.get(z, [])
    rec.update(
        respondent_id=clean(rid), status=status, s1_zip=z,
        gc_store_numbers=";".join(str(s) for s in stores),
        gc_store_locations="; ".join(f"{STORES[str(s)]['city']}, {STORES[str(s)]['st']}" for s in stores if str(s) in STORES),
        source="server", received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        user_agent=clean(request.headers.get("User-Agent"), 300),
    )
    with locked():
        rs = rows()
        if any(r.get("respondent_id") == rec["respondent_id"] for r in rs):
            return jsonify({"error": "duplicate"}), 409
        if status == "complete" and completes(rs) >= quota():
            status = rec["status"] = "overquota"
            rec["terminate_reason"] = "Quota full at submission"
        new = not os.path.exists(FILE) or os.path.getsize(FILE) == 0
        if new:
            middle = [k for k in rec if k not in HEAD and k not in TAIL]
            header = HEAD + middle + TAIL
        else:
            with open(FILE, newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f))
        with open(FILE, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(rec)
            f.flush()
            os.fsync(f.fileno())
    return jsonify({"ok": True, "status": status, "redirect": redirect_for(status, rid)})


def admin_only(fn):
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


@bp.get("/admin/gc")
@admin_only
def admin():
    rs = list(reversed(rows()))
    by = lambda s: sum(1 for r in rs if r.get("status") == s)
    n = by("complete")
    starts = len(rs)
    ir = (n / (n + by("terminated"))) if (n + by("terminated")) else 0
    durs = sorted(int(r["duration_sec"]) for r in rs if r.get("status") == "complete" and str(r.get("duration_sec", "")).isdigit())
    med = durs[len(durs) // 2] if durs else 0
    reasons = {}
    for r in rs:
        if r.get("status") in ("terminated", "quality"):
            reasons[r.get("terminate_reason", "")] = reasons.get(r.get("terminate_reason", ""), 0) + 1
    rtxt = "".join(f"<li>{escape(k)}: {v}</li>" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) or "<li>None yet</li>"
    trs = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            escape(r.get("received_at", "")), escape(r.get("respondent_id", "")), escape(r.get("status", "")),
            escape(r.get("terminate_reason", "")), escape(r.get("s1_zip", "")), escape(r.get("gc_store_locations", "")),
            escape(str(r.get("duration_sec", ""))), escape(r.get("flags", "")).replace(" | ", "<br>"))
        for r in rs[:500]) or "<tr><td colspan=8>No responses yet.</td></tr>"
    html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Golden Corral survey admin</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#1f1c1a}}table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #ddd6cf;padding:7px 9px;text-align:left;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#645d57}}
.t{{display:inline-block;margin:0 28px 12px 0}}.t b{{font-size:26px;display:block}}a.btn{{display:inline-block;background:#6b3f1d;color:#fff;padding:8px 16px;border-radius:999px;text-decoration:none;margin:8px 0 16px}}</style>
<h1>Golden Corral survey: fieldwork</h1>
<div><span class=t>Completes<b>{n} / {quota()}</b></span><span class=t>Screened out<b>{by('terminated')}</b></span>
<span class=t>Quality terms<b>{by('quality')}</b></span><span class=t>Over quota<b>{by('overquota')}</b></span>
<span class=t>Incidence<b>{ir:.0%}</b></span><span class=t>Median LOI<b>{med // 60}m {med % 60:02d}s</b></span><span class=t>Total records<b>{starts}</b></span></div>
<a class=btn href="/admin/gc/export.csv">Download gc_responses.csv</a>
<h3>Screen-out reasons</h3><ul>{rtxt}</ul>
<div style="overflow-x:auto"><table><tr><th>Received (UTC)</th><th>Respondent</th><th>Status</th><th>Reason</th><th>ZIP</th><th>Nearby store(s)</th><th>LOI (s)</th><th>Flags</th></tr>{trs}</table></div>"""
    return Response(html, mimetype="text/html")


@bp.get("/admin/gc/export.csv")
@admin_only
def export():
    if not os.path.exists(FILE):
        return Response("﻿no responses yet\r\n", mimetype="text/csv")
    with open(FILE, encoding="utf-8-sig") as f:
        body = "﻿" + f.read()
    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="golden-corral-survey-{stamp}.csv"'})
