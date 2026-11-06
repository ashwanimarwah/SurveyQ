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
