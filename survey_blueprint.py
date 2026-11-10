"""
Reusable server for a single-page survey: serves the page, stores each response as one
CSV row on the volume, enforces a completes quota, de-duplicates panel ids and offers an
admin dashboard with CSV download.

    make_survey(slug="de", title="...", page="de-cash.html", default_quota=260)

gives these routes:
    /<slug>?rid=<panel id>          survey
    /api/<slug>/responses           POST (used by the page)
    /admin/<slug>                   fieldwork dashboard (user "admin", ADMIN_PASSWORD)
    /admin/<slug>/export.csv        all responses

Environment variables use the slug in upper case, e.g. for slug "de":
    DE_QUOTA, DE_RESPONSES_FILE, DE_REQUIRE_RID, DE_SHOW_REASON (0 hides the screen-out reason),
    DE_REDIRECT_COMPLETE, DE_REDIRECT_TERMINATE, DE_REDIRECT_OVERQUOTA, DE_REDIRECT_QUALITY ({rid} placeholder)
"""
import csv
import hmac
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
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(BASE, "data")
STATUSES = {"complete", "terminated", "quality", "overquota"}
HEAD = ["respondent_id", "status", "terminate_reason", "flags", "started_at", "completed_at", "duration_sec", "lang"]
TAIL = ["source", "received_at", "user_agent"]
KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def _clean(v, limit=1000):
    if v is None or v is False:
        return ""
    if v is True:
        return 1
    if isinstance(v, (int, float)):
        return v
    s = str(v)[:limit].replace("\r", " ").replace("\n", " ")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def make_survey(slug, title, page, default_quota):
    env = lambda k, d="": os.environ.get(f"{slug.upper()}_{k}", d)
    path = env("RESPONSES_FILE") or os.path.join(DATA_DIR, f"{slug}_responses.csv")
    lock_path = path + ".lock"
    page_path = os.path.join(BASE, "static", page)
    bp = Blueprint(f"survey_{slug}", __name__)
    tlock = threading.Lock()

    def quota():
        try:
            return int(env("QUOTA", str(default_quota)))
        except ValueError:
            return default_quota

    @contextmanager
    def locked():
        with tlock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(lock_path, "a") as lf:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(lf, fcntl.LOCK_UN)

    def rows():
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def completes(rs):
        return sum(1 for r in rs if r.get("status") == "complete")

    def redirect_for(status, rid):
        key = {"complete": "REDIRECT_COMPLETE", "terminated": "REDIRECT_TERMINATE",
               "overquota": "REDIRECT_OVERQUOTA", "quality": "REDIRECT_QUALITY"}[status]
        url = env(key) or (env("REDIRECT_TERMINATE") if status == "quality" else "")
        return url.replace("{rid}", rid) if url and rid else url

    def wrap(config):
        with open(page_path, encoding="utf-8") as f:
            body = f.read().replace("window.SURVEY_SERVER = null;", "window.SURVEY_SERVER = " + json.dumps(config) + ";", 1)
        return ('<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
                "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>" + body + "</body></html>")

    @bp.get(f"/{slug}")
    def survey_page():
        rid = (request.args.get("rid") or "").strip()[:100]
        if env("REQUIRE_RID") == "1" and not rid:
            return Response("<p>Survey link is missing a respondent id.</p>", 400)
        rs = rows()
        if rid and any(r.get("respondent_id") == rid for r in rs):
            return Response("<p style='font-family:sans-serif;padding:24px'>Sie haben bereits an dieser Umfrage teilgenommen. Vielen Dank! / You have already taken part. Thank you.</p>")
        cfg = {"rid": rid, "api": f"api/{slug}/responses", "showReason": env("SHOW_REASON", "1") != "0"}
        if completes(rs) >= quota():
            cfg.update(closed=True, redirect=redirect_for("overquota", rid))
        return Response(wrap(cfg), mimetype="text/html")

    @bp.post(f"/api/{slug}/responses")
    def save():
        data = request.get_json(silent=True) or {}
        status = data.get("status")
        if status not in STATUSES:
            abort(400, "invalid status")
        rid = (str(data.get("rid") or "").strip()[:100]) or ("anon-" + uuid.uuid4().hex[:12])
        rec = {k: _clean(v) for k, v in list(data.items())[:400] if k != "rid" and KEY_RE.match(k)}
        rec.update(respondent_id=_clean(rid), status=status, source="server",
                   received_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   user_agent=_clean(request.headers.get("User-Agent"), 300))
        with locked():
            rs = rows()
            if any(r.get("respondent_id") == rec["respondent_id"] for r in rs):
                return jsonify({"error": "duplicate"}), 409
            if status == "complete" and completes(rs) >= quota():
                status = rec["status"] = "overquota"
                rec["terminate_reason"] = "Quota full at submission"
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            if new:
                header = HEAD + [k for k in rec if k not in HEAD and k not in TAIL] + TAIL
            else:
                with open(path, newline="", encoding="utf-8-sig") as f:
                    header = next(csv.reader(f))
            with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
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

    @bp.get(f"/admin/{slug}")
    @admin_only
    def admin():
        rs = list(reversed(rows()))
        by = lambda s: sum(1 for r in rs if r.get("status") == s)
        n, term = by("complete"), by("terminated")
        ir = n / (n + term) if (n + term) else 0
        durs = sorted(int(r["duration_sec"]) for r in rs if r.get("status") == "complete" and str(r.get("duration_sec", "")).isdigit())
        med = durs[len(durs) // 2] if durs else 0
        reasons = {}
        for r in rs:
            if r.get("status") in ("terminated", "quality"):
                k = r.get("terminate_reason", "")
                reasons[k] = reasons.get(k, 0) + 1
        rtxt = "".join(f"<li>{escape(k)}: {v}</li>" for k, v in sorted(reasons.items(), key=lambda x: -x[1])) or "<li>None yet</li>"
        trs = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                escape(r.get("received_at", "")), escape(r.get("respondent_id", "")), escape(r.get("status", "")),
                escape(r.get("terminate_reason", "")), escape(r.get("lang", "")), escape(str(r.get("duration_sec", ""))),
                escape(r.get("flags", "")).replace(" | ", "<br>"))
            for r in rs[:500]) or "<tr><td colspan=7>No responses yet.</td></tr>"
        html = f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{escape(title)} admin</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#16191d}}table{{border-collapse:collapse;width:100%;font-size:14px}}
td,th{{border-bottom:1px solid #d9dde3;padding:7px 9px;text-align:left;vertical-align:top}}th{{font-size:12px;text-transform:uppercase;color:#5b6370}}
.t{{display:inline-block;margin:0 28px 12px 0}}.t b{{font-size:26px;display:block}}a.btn{{display:inline-block;background:#1f4e79;color:#fff;padding:8px 16px;border-radius:999px;text-decoration:none;margin:8px 0 16px}}</style>
<h1>{escape(title)}: fieldwork</h1>
<div><span class=t>Completes<b>{n} / {quota()}</b></span><span class=t>Screened out<b>{term}</b></span>
<span class=t>Quality terms<b>{by('quality')}</b></span><span class=t>Over quota<b>{by('overquota')}</b></span>
<span class=t>Incidence<b>{ir:.0%}</b></span><span class=t>Median LOI<b>{med // 60}m {med % 60:02d}s</b></span><span class=t>Total records<b>{len(rs)}</b></span></div>
<a class=btn href="/admin/{slug}/export.csv">Download {slug}_responses.csv</a>
<h3>Screen-out reasons</h3><ul>{rtxt}</ul>
<div style="overflow-x:auto"><table><tr><th>Received (UTC)</th><th>Respondent</th><th>Status</th><th>Reason</th><th>Lang</th><th>LOI (s)</th><th>Flags</th></tr>{trs}</table></div>"""
        return Response(html, mimetype="text/html")

    @bp.get(f"/admin/{slug}/export.csv")
    @admin_only
    def export():
        if not os.path.exists(path):
            return Response("﻿no responses yet\r\n", mimetype="text/csv")
        with open(path, encoding="utf-8-sig") as f:
            body = "﻿" + f.read()
        stamp = datetime.now().strftime("%Y-%m-%d")
        return Response(body, mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{slug}-survey-{stamp}.csv"'})

    return bp
