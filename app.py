import os
import secrets
from hmac import compare_digest
from urllib.parse import urlparse, parse_qs
from requests.exceptions import RequestException
from google.auth.exceptions import TransportError
from oauthlib.oauth2 import OAuth2Error
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, Response

from database import Database
from gmail_scanner import GmailScanner

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
production = os.environ.get("APP_ENV") == "production"
if production and not all(os.environ.get(key) for key in ("SECRET_KEY", "APP_USERNAME", "APP_PASSWORD")):
    raise RuntimeError("Production requires SECRET_KEY, APP_USERNAME, and APP_PASSWORD")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=production)

data_dir = os.environ.get("DATA_DIR", os.path.dirname(__file__))
os.makedirs(data_dir, exist_ok=True)
db = Database(os.path.join(data_dir, "internships.db"))


def gmail_error(message, status):
    if request.path.startswith("/api/"):
        return jsonify({"error": message}), status
    return render_template("gmail_error.html", message=message), status


@app.errorhandler(RequestException)
@app.errorhandler(TransportError)
def gmail_network_error(error):
    # Do not log request URLs or exception text: OAuth requests contain secrets.
    app.logger.warning("Gmail connection failed (%s)", type(error).__name__)
    return gmail_error("Cannot reach Google right now. Check the server's internet access, then connect Gmail again.", 503)


@app.errorhandler(OAuth2Error)
def gmail_oauth_error(error):
    return gmail_error("Google could not complete sign-in. Please start a new Gmail connection.", 400)


@app.before_request
def protect_tracker():
    if request.path == "/healthz":
        return None
    password = os.environ.get("APP_PASSWORD")
    if password:
        auth = request.authorization
        if not (auth and compare_digest(auth.username or "", os.environ.get("APP_USERNAME", "owner"))
                and compare_digest(auth.password or "", password)):
            return Response("Sign in to InternTrack", 401, {"WWW-Authenticate": 'Basic realm="InternTrack"'})
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("Origin")
        expected = os.environ.get("PUBLIC_URL", request.host_url).rstrip("/")
        if origin and origin.rstrip("/") != expected:
            return jsonify({"error": "Invalid request origin"}), 403


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ── Pages ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Gmail OAuth ──────────────────────────────────────────────────────────────

REDIRECT_URI = os.environ.get("PUBLIC_URL", "http://localhost:5000").rstrip("/") + "/auth/callback"


@app.route("/auth/gmail")
def auth_gmail():
    scanner = GmailScanner()
    if not scanner.credentials_exist():
        return "credentials.json not found. See README.md for setup instructions.", 400
    auth_url, code_verifier = scanner.get_auth_url(REDIRECT_URI)
    session["oauth_code_verifier"] = code_verifier
    session["oauth_state"] = parse_qs(urlparse(auth_url).query)["state"][0]
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    state = session.pop("oauth_state", None)
    if not state or not compare_digest(state, request.args.get("state", "")):
        return gmail_error("This Gmail sign-in has expired. Please connect Gmail again.", 400)
    if request.args.get("error"):
        session.pop("oauth_code_verifier", None)
        return gmail_error("Gmail access was not granted. You can try connecting again.", 400)
    code = request.args.get("code")
    if code:
        scanner = GmailScanner()
        scanner.exchange_code(code, REDIRECT_URI, session.pop("oauth_code_verifier", None))
    return redirect(url_for("index"))


# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/auth-status")
def auth_status():
    scanner = GmailScanner()
    return jsonify({
        "authenticated": scanner.is_authenticated(),
        "credentials_exist": scanner.credentials_exist(),
    })


@app.route("/api/sync", methods=["POST"])
def sync():
    scanner = GmailScanner()
    if not scanner.is_authenticated():
        return jsonify({"error": "Not authenticated with Gmail"}), 401
    new, updated = scanner.sync(db)
    return jsonify({"new": new, "updated": updated})


@app.route("/api/applications", methods=["GET"])
def get_applications():
    cycle = request.args.get("cycle", "all")
    search = request.args.get("search", "")
    return jsonify(db.get_applications(cycle=cycle, search=search))


@app.route("/api/applications", methods=["POST"])
def create_application():
    data = request.get_json()
    if not data or not data.get("company"):
        return jsonify({"error": "company is required"}), 400
    app_id = db.create_application(data)
    return jsonify({"id": app_id, "success": True}), 201


@app.route("/api/applications/<int:app_id>", methods=["PUT"])
def update_application(app_id):
    data = request.get_json()
    db.update_application(app_id, data)
    return jsonify({"success": True})


@app.route("/api/applications/<int:app_id>", methods=["DELETE"])
def delete_application(app_id):
    db.delete_application(app_id)
    return jsonify({"success": True})


@app.route("/api/stats")
def get_stats():
    cycle = request.args.get("cycle", "all")
    return jsonify(db.get_stats(cycle=cycle))


@app.route("/api/notifications")
def get_notifications():
    return jsonify(db.get_unseen_notifications())


# ── Feedback ──────────────────────────────────────────────────────────────────

@app.route("/api/applications/<int:app_id>/diagnose", methods=["GET"])
def diagnose_application(app_id):
    from gmail_scanner import GmailScanner
    app_data = db.get_application(app_id)
    if not app_data:
        return jsonify({"error": "Not found"}), 404

    # Parse subject / sender / body from the stored email_body field
    email_body = app_data.get("email_body") or ""
    subject, sender, body_text = "", "", email_body
    for i, line in enumerate(email_body.splitlines()[:6]):
        if line.startswith("Subject: "):
            subject = line[9:]
        elif line.startswith("From: "):
            sender = line[6:]
        elif line == "" and i > 1:
            body_text = "\n".join(email_body.splitlines()[i + 1:])
            break

    scanner = GmailScanner.__new__(GmailScanner)
    scanner.creds = None

    issues = []

    # 1. Wrong status — re-classify and compare
    reclassified = scanner.classify(subject, body_text, sender)
    if reclassified and reclassified != app_data["status"]:
        issues.append({
            "issue_type": "wrong_status",
            "suggested_status": reclassified,
            "note": f"Classifier suggests '{reclassified}' but stored as '{app_data['status']}'",
        })

    # 2. False positive — email doesn't look like an internship
    if email_body and not scanner.is_internship_related(subject, body_text, sender):
        issues.append({
            "issue_type": "false_positive",
            "suggested_status": None,
            "note": "Email does not appear to be an internship application",
        })

    # 3. Wrong cycle — re-detect and compare
    re_cycle = scanner.detect_cycle(subject, body_text) if email_body else None
    if re_cycle and re_cycle != app_data.get("cycle"):
        issues.append({
            "issue_type": "wrong_cycle",
            "suggested_status": app_data["status"],
            "note": f"Cycle should be '{re_cycle}' (stored as '{app_data.get('cycle')}')",
        })

    # 4. Duplicate — same company + overlapping role keywords
    import re as _re
    _ROLE_KW = {"software","data","product","ml","ai","hardware","systems","backend",
                "frontend","fullstack","mobile","devops","engineer","developer",
                "analyst","scientist","manager","intern"}
    def _fp(role):
        return set(_re.findall(r"\b\w+\b", (role or "").lower())) & _ROLE_KW

    this_fp = _fp(app_data.get("role", ""))
    for other in db.get_applications():
        if other["id"] == app_id:
            continue
        if other["company"].lower() == app_data["company"].lower() and (_fp(other.get("role", "")) & this_fp):
            issues.append({
                "issue_type": "duplicate",
                "suggested_status": None,
                "note": f"Possible duplicate of #{other['id']} — {other['company']}: {other['role']}",
            })
            break

    primary = issues[0] if issues else {
        "issue_type": "wrong_status",
        "suggested_status": None,
        "note": "",
    }
    return jsonify({**primary, "all_issues": issues})


@app.route("/api/feedback", methods=["GET"])
def get_feedback():
    resolved = request.args.get("resolved", "false").lower() == "true"
    return jsonify(db.get_feedback(resolved=resolved))


@app.route("/api/feedback", methods=["POST"])
def create_feedback():
    data = request.get_json()
    if not data or not data.get("application_id"):
        return jsonify({"error": "application_id is required"}), 400
    fid = db.create_feedback(data)
    return jsonify({"id": fid, "success": True}), 201


@app.route("/api/feedback/<int:fid>/resolve", methods=["POST"])
def resolve_feedback(fid):
    apply = request.get_json(silent=True) or {}
    db.resolve_feedback(fid, apply_suggestion=apply.get("apply", False))
    return jsonify({"success": True})


@app.route("/api/feedback/<int:fid>", methods=["DELETE"])
def delete_feedback(fid):
    db.delete_feedback(fid)
    return jsonify({"success": True})


@app.route("/api/consolidate-duplicates", methods=["POST"])
def consolidate_duplicates():
    removed = db.consolidate_duplicates()
    return jsonify({"removed": removed})


@app.route("/api/reclassify-all", methods=["POST"])
def reclassify_all():
    """Re-run classifier on all stored emails and correct obviously wrong statuses."""
    from gmail_scanner import GmailScanner
    scanner = GmailScanner.__new__(GmailScanner)
    scanner.creds = None

    apps = db.get_applications()
    fixed = 0
    _STATUS_RANK = {"Applied": 0, "OA": 1, "Interview": 2, "Offer": 3, "Declined": 4}

    for app_data in apps:
        email_body = app_data.get("email_body") or ""
        if not email_body:
            continue

        subject, sender, body_text = "", "", email_body
        for i, line in enumerate(email_body.splitlines()[:6]):
            if line.startswith("Subject: "):
                subject = line[9:]
            elif line.startswith("From: "):
                sender = line[6:]
            elif line == "" and i > 1:
                body_text = "\n".join(email_body.splitlines()[i + 1:])
                break

        new_status = scanner.classify(subject, body_text, sender)
        old_status = app_data["status"]

        if not new_status or new_status == old_status:
            continue

        # Only allow downgrading if the new status is lower rank (correcting false positives)
        # e.g. Interview→Applied, Offer→Applied, Interview→OA
        if _STATUS_RANK.get(new_status, 0) < _STATUS_RANK.get(old_status, 0):
            db.update_application(app_data["id"], {"status": new_status})
            print(f"[reclassify] #{app_data['id']} {app_data['company']}: {old_status} → {new_status}")
            fixed += 1

    return jsonify({"fixed": fixed})


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", "5000")))
