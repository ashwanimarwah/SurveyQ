# India School Education Survey (self-hosted)

A one-page online survey for the 12-question questionnaire. Every response is saved as a row in
`data/responses.csv` (change the location with the `RESPONSES_FILE` environment variable).
The file opens directly in Excel; back it up regularly during fieldwork.

## Run it
    pip install flask
    ADMIN_PASSWORD=choose-a-password python app.py

- Survey: http://localhost:8000/?rid=RESPONDENT_ID
- Admin (user `admin`, your password): http://localhost:8000/admin
- CSV export: http://localhost:8000/admin/export.csv

For production, run behind HTTPS, e.g. `pip install gunicorn` then
`gunicorn -w 2 -b 0.0.0.0:8000 app:app`.

## Panel integration
Pass the panel respondent id as `?rid=`. Each rid can respond once (a second visit sees "already taken part").
Set `REDIRECT_COMPLETE` and `REDIRECT_TERMINATE` (use `{rid}` as a placeholder) to send respondents back to the panel.
Set `REQUIRE_RID=1` to block entry without an id.

## What's stored (`data/responses.csv`)
One row per respondent: respondent id, status (complete / terminated), termination reason, quality flags,
start/finish times, length of interview, every answer as a code and a label (multi-selects joined with `;`),
"Other – specify" text, time received and browser. Writes are locked, so simultaneous submissions are safe,
including with several gunicorn workers on Linux.

## Survey logic implemented
- Q1 = No → terminate. Q6 = F (not directly involved) → terminate. Termination message from the questionnaire.
- Q2 searchable State/UT list; selected State piped into Q7.
- Q3 = Other → specify text, flagged "review eligibility".
- Q4 mandatory, 30–500 characters; auto-flags short/generic, nonsense-looking or off-topic text for review (never auto-disqualifies).
- Q5 NGO/department/EdTech/Other → flagged "confirm relevance". Role vs organisation mismatches flagged.
- Q6 multi-select; E and F exclusive.
- Q10 max 3; M and N exclusive; Other → specify.
- Speeders (under 2 minutes) and "Not sure" on all rating questions are flagged.

Edit `static/survey.html` to change wording; the questions are defined at the top of its script.

## Deploying from GitHub

### Railway
1. railway.com → New Project → Deploy from GitHub repo → choose **SurveyQ**. Railway detects Python from
   `requirements.txt` and uses the start command in `railway.json`.
2. **Add a Volume (required, or responses are lost on every redeploy):** right-click the service →
   Attach Volume → mount path `/data`. The app writes to `/data/responses.csv` automatically.
3. Service → Variables → add `ADMIN_PASSWORD` (and optionally `REDIRECT_COMPLETE`, `REDIRECT_TERMINATE`, `REQUIRE_RID`).
4. Service → Settings → Networking → Generate Domain. Survey: `https://<domain>/?rid=TEST1`, admin: `/admin`.
5. Every push to `main` redeploys automatically; the Volume keeps the responses.
Keep a single replica: the CSV lives on one Volume, so don't scale to multiple instances.

### PythonAnywhere (simplest; storage is permanent)
1. Open a Bash console on PythonAnywhere and run:
   `git clone https://github.com/ashwanimarwah/SurveyQ.git` then `pip install --user -r SurveyQ/requirements.txt`
2. Web tab → Add a new web app → Manual configuration → choose a Python 3 version.
3. Edit the WSGI file so it contains:
   ```python
   import os, sys
   sys.path.insert(0, "/home/YOUR_USERNAME/SurveyQ")
   os.environ["ADMIN_PASSWORD"] = "choose-a-strong-password"
   from app import app as application
   ```
4. Reload the web app. Survey: `https://YOUR_USERNAME.pythonanywhere.com/?rid=TEST1`, admin: `/admin`.
5. To update later: `cd SurveyQ && git pull`, then Reload.

### Your own server (Ubuntu VM, Lightsail, DigitalOcean)
    git clone https://github.com/ashwanimarwah/SurveyQ.git && cd SurveyQ
    python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
    ADMIN_PASSWORD=choose-a-password gunicorn -w 2 -b 0.0.0.0:8000 app:app
Put it behind HTTPS (e.g. Caddy or nginx + Let's Encrypt).

### Getting the data
- Browser: open `/admin`, log in as `admin`, click **Download responses.csv**.
- Server: copy `data/responses.csv` (it is git-ignored, so it is never pushed to GitHub).

---

# Golden Corral survey (`/gc`)

USA gen pop, n=500, ~15 min, expected IR ~70%. Lives in the same app as the India survey.

- Survey link for the panel: `https://<your-domain>/gc?rid={panel_id}`
- Fieldwork dashboard: `https://<your-domain>/admin/gc` (completes vs quota, incidence, screen-out reasons, median LOI)
- Data: **Download gc_responses.csv** on that page, or `gc_responses.csv` on the volume.

## Screening
| Question | Rule |
|---|---|
| S1 ZIP | Must be one of the 7,581 ZIPs within 10 miles of the 353 Golden Corral stores (`gc_zips.json`, from the client file). Checked again on the server at submission. |
| S2 Age | Under 18 → terminate |
| S3 Gender | Recorded for quotas |
| S4 Household income | $75,000–$99,999 or $100,000–$149,999 → terminate; "Prefer not to say" → terminate |
| S5 Industry | Market research/advertising or restaurant/food service → terminate |

## Main questionnaire (10)
Q1 unaided buffet awareness (open, 3 boxes) · Q2 brand funnel grid (10 brands, randomized) ·
Q3 Golden Corral frequency (P12M visitors) · Q4 who they went with (P12M) · Q5 attribute ratings (P12M) ·
Q6 recommend 0–10 (ever visited) · Q7 visit intent next 3 months (aware) · Q8 barriers (aware, not visited P12M) ·
Q9 motivators (aware) · Q10 attitude grid (all) with attention check · D1 children in household · D2 Hispanic origin.
The brand is not shown until Q2, so Q1 is truly unaided.

## Status values in the data
`complete`, `terminated` (screen-out), `quality` (failed the Q10 attention check), `overquota`.
Flags for review: speeders (< 150 s), straight-lining in Q5/Q10, gibberish in Q1.
Each row also carries `gc_store_numbers` / `gc_store_locations` for the respondent's ZIP.

## Settings (Railway → Variables)
- `GC_QUOTA` (default 500): the survey closes once this many completes are in.
- `GC_REDIRECT_COMPLETE`, `GC_REDIRECT_TERMINATE`, `GC_REDIRECT_OVERQUOTA`, `GC_REDIRECT_QUALITY`:
  panel return links, use `{rid}` where the respondent id goes.
- `GC_REQUIRE_RID=1` to block entry without a panel id.
- Uses the same `ADMIN_PASSWORD` as the India survey.

---

# Germany cash-only merchants survey (`/de`)

B2C small businesses in Germany that accept cash only: why, the cost of cash, intent to adopt card or
digital payments, preferred solutions, fee tolerance and provider choice. n=260, LOI 10–15 min.
German by default; reviewers can switch to English with the DE/EN toggle or `?lang=en`.
Codes are stable and the `_label` columns are in English whatever language the respondent used.

- Panel link: `https://<your-domain>/de?rid={panel_id}`
- Dashboard: `https://<your-domain>/admin/de` · data: **Download de_responses.csv**

## Screener
| Q | Rule |
|---|---|
| S1 Age | under 18 or over 75 → terminate |
| S2 Gender, S3 Bundesland | recorded |
| S4 Employment | unemployed, retired, student/apprentice, homemaker, other → terminate |
| S5 Position | recorded |
| S6 Industry | market research/marketing or advertising/PR → terminate |
| S7 Employees | 100+ or don't know → terminate (max 99) |
| S8 Customers | about equal / mostly / only businesses → terminate ("mostly consumers" passes, flagged) |
| S9 Where customers buy | online only → terminate (mobile stand passes, flagged) |
| S10 Payment decision | not involved → terminate |
| S11 Payment methods accepted | anything other than cash alone → terminate |

## Main questionnaire
Q1 years in business · Q2 average purchase · Q3 reasons for cash only · Q4 how often customers ask for card ·
Q5 lost sales · Q6 cash-handling attitudes grid (attention check row → `quality`) · Q7 likelihood to adopt in 12 months ·
Q8 preferred solution (likely adopters) · Q9 reasons not to adopt (unlikely) · Q10 acceptable fee · Q11 provider choice factors ·
Q12 information sources · Q13 open end: what would need to change · D1 revenue · D2 locations.

## Settings (Railway → Variables)
`DE_QUOTA` (default 260), `DE_REDIRECT_COMPLETE`, `DE_REDIRECT_TERMINATE`, `DE_REDIRECT_OVERQUOTA`,
`DE_REDIRECT_QUALITY` ({rid} placeholder), `DE_REQUIRE_RID=1`.

## Adding another survey
`survey_blueprint.make_survey(slug, title, page, default_quota)` gives any new page the same storage,
quota, de-duplication, redirects and dashboard; register it in `app.py`.

---

## Screen-out reasons shown to respondents
When someone is screened out, the end page shows the question that ended the survey, their answer and the
requirement (German/English on `/de`). This is on by default, which helps testing. For live fieldwork you may
want to hide it so respondents can't learn how to qualify: set `SHOW_REASON=0` (India), `GC_SHOW_REASON=0`
(Golden Corral) or `DE_SHOW_REASON=0` (Germany) in Railway → Variables. With a panel redirect set, the page
waits 8 seconds before redirecting when a reason is shown.
