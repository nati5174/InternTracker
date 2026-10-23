# InternTrack 2027

Dashboard to track Summer 2027 & Winter 2027 internship applications synced from Gmail.

## Setup

### 1. Install dependencies

```bash
cd internship-tracker
pip install -r requirements.txt
```

### 2. Set up Google Cloud credentials (for Gmail sync)

1. Go to https://console.cloud.google.com/
2. Create a new project (e.g. "InternTrack")
3. Go to **APIs & Services → Library** → enable **Gmail API**
4. Go to **APIs & Services → OAuth consent screen**
   - Choose **External**, fill in app name, add your Gmail as test user
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:5000/auth/callback`
6. Download the JSON and save it as `credentials.json` in this folder

> Without `credentials.json` the app still works — you can add applications manually.

### 3. Run

```bash
python app.py
```

Open http://localhost:5000

### 4. Connect Gmail

Click the green/red dot in the top-right corner's area or click **Sync Gmail** — it will redirect you to authorize Gmail access. After authorizing, click **Sync Gmail** to import emails.

## Production hosting (personal tracker)

Deploy the `render.yaml` Blueprint from the `master` branch. It provisions a paid web service with a 1 GB persistent disk. Choose a strong `APP_PASSWORD`; the login username is `owner`. Set `PUBLIC_URL` to the service's HTTPS URL (no trailing slash), and update it after creation if Render assigns a different hostname.

Upload your Google OAuth client JSON as a Render secret file named `credentials.json`. In Google Cloud, add `<PUBLIC_URL>/auth/callback` to the client's authorized redirect URIs. Open the hosted tracker and connect Gmail again. Gmail tokens and the SQLite database persist under `/var/data`; local applications are not automatically transferred. Never commit credentials, tokens, or your database.

The app runs under Gunicorn with one worker. `/healthz` is public; all tracker pages and API routes require the configured password in production. This deployment is for one owner and one Gmail account.

## Features

- **Kanban board** with 5 columns: Applied / OA / Interview / Offer / Declined
- **Table view** toggle
- **Gmail auto-detection** — classifies emails from Greenhouse, Lever, Workday, etc.
- **Browser notifications** when new applications are tracked
- **Cycle filter** — Summer 2027 vs Winter 2027
- **Manual add/edit** with Ctrl+K shortcut
- **Search** across companies and roles
- **Auto-refresh** every 5 minutes when the tab is open

## Email classification

The scanner looks for keywords in email subject + body:

| Status | Keywords |
|--------|----------|
| Applied | "application received", "thank you for applying" |
| OA | "online assessment", "HackerRank", "CodeSignal", "coding challenge" |
| Interview | "interview", "phone screen", "Calendly" |
| Offer | "offer letter", "pleased to offer" |
| Declined | "unfortunately", "not moving forward" |
