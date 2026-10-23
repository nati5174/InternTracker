"""Gmail scanning and email classification for internship applications."""

import os
import re
import json
import base64
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google.auth.transport.requests import Request
from google.auth.exceptions import TransportError
from requests.exceptions import RequestException
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.path.join(os.environ.get("DATA_DIR", "."), "token.json")

# ── Classification patterns (priority: Offer > Declined > Interview > OA > Applied) ──

STATUS_PATTERNS = {
    "Offer": [
        r"offer letter", r"pleased to offer", r"excited to offer", r"job offer",
        r"offer of (employment|internship)", r"we.*?offer.*?position",
        r"officially.*?offer", r"extend.*?offer", r"formal offer",
    ],
    "Declined": [
        r"unfortunately.{0,120}(not moving forward|other candidates|not selected|no longer consider|not proceed|not advanc|unable to offer|not be moving|won.t be moving)",
        r"not moving forward", r"pursue other candidates",
        r"decided to move forward with other", r"position has been filled",
        r"no longer consider",
        r"(have|has) not been selected", r"(were|wasn.t|weren.t) selected for further",
        r"you(?:'re| are) not (?:being )?selected to move forward",
        r"regret to inform",
        r"not a match", r"not proceeding", r"not advancing",
        r"has been canceled", r"application.*canceled", r"withdrawn",
    ],
    "Interview": [
        r"(invited?|inviting you).{0,60}interview",
        r"(schedule|book|confirm).{0,60}(interview|call|chat|time)",
        r"interview.{0,60}(schedule|invitation|invite|request|slot|time|date)",
        r"phone (screen|call)", r"video (call|interview|chat)",
        r"technical (interview|screen)", r"hiring manager",
        r"recruiter.*?call", r"next step.*?interview",
        r"in-person", r"calendly",
    ],
    "OA": [
        r"online assessment", r"coding (challenge|test|assessment|exercise)",
        r"technical assessment", r"hackerrank", r"codesignal", r"codility",
        r"take[- ]home", r"assessment link", r"complete.*?test",
        r"hirevue", r"pymetrics", r"coding problem",
    ],
    "Applied": [
        r"application (received|submitted|confirmed)",
        r"thank(?:s| you) for (applying|your application|submitting)",
        r"(?:we(?:'ve| have)|has) received your application",
        r"received your application",
        r"submitting your application",
        r"your application.*?(?:submitted|received)",
        r"reviewing your application",
        r"application for.*?(?:position|role)",
        r"we(?:'ve| have) got your application",
        r"confirming.{0,40}(?:received|got).{0,20}application",
        r"you(?:'ve| have)? applied",
        r"successfully applied",
        r"application has been (received|submitted)",
        r"completing your application",
        r"we('ve| have) received your",
    ],
}

RECRUITING_DOMAINS = {
    "greenhouse.io", "lever.co", "workday.com", "ashbyhq.com",
    "myworkdayjobs.com", "icims.com", "taleo.net", "smartrecruiters.com",
    "jobvite.com", "breezy.hr", "recruitee.com",
}

# Emails matching these patterns are NOT internship-related (job alerts, hackathons, etc.)
SKIP_PATTERNS = [
    r"hackathon", r"\w*hacks\b",                                   # hackathon events (SteelHacks, VTHacks, etc.)
    r"hackmit", r"hack the north", r"hackathon application",       # specific events
    r"^job alert", r"new jobs? for", r"jobs? matching",            # Indeed/LinkedIn job alerts
    r"recommended jobs?", r"^\d+ new jobs?",
    r"salary insight", r"people also viewed",
    r"verification code", r"verify your email", r"security code",  # auth/verification emails
    r"confirm your (?:email|identity|account)",                     # identity verification emails
    r"newsletter", r"unsubscribe",                                  # marketing
]

# Suffixes to strip from extracted company names
COMPANY_SUFFIXES = [
    r"\s+campus\s+recruitment", r"\s+recruitment",
    r"\s+hr\s*workday(\s+notifications?)?", r"\s+workday(\s+notifications?)?",
    r"\s+people\s+services", r"\s+human\s+resources",
    r"\s+early\s+careers?", r"\s+early$",
    r"\s+careers?$", r"\s+jobs?$", r"\s+hiring$",
    r"\s+team$", r"\s+talent$",
    r"\s+&\s+co\.$",
    r"\s+application\s+received$",       # "AbbVie Application Received"
    r"\s+application$",                  # "AMD Application"
    r"\s+employment\s+application$",     # "Point72 Employment Application"
    r"\s+recruiting$",                   # "Goldman Sachs Recruiting"
    r"\s+hr$",                           # "NVIDIA HR"
    r"\s+hr\s+workday.*$",               # "Capital One HR Workday..."
    r"\s+notifications?$",               # "Workday Notifications"
    r"\s+do\s+not\s+reply.*$",           # "Company Do Not Reply"
    r"\s+no\s*reply.*$",                 # "Company Noreply..."
    r"\s+invites?$",                     # "Blackstone Invites"
]

# Known job-board / ATS sender domains — not the actual company
PLATFORM_DOMAINS = {
    "gmail", "yahoo", "hotmail", "outlook",
    "greenhouse", "greenhouse-mail", "lever",
    "workday", "myworkdayjobs", "myworkday", "workdaysystem",
    "ashby", "ashbyhq", "noreply", "no-reply",
    "indeed", "glassdoor", "linkedin", "jobvite", "icims",
    "taleo", "smartrecruiters", "breezy", "recruitee",
    "hiltonrecruitment", "sendgrid", "amazonses",
    "oraclecloud", "successfactors", "ultipro", "bamboohr",
    "applytojob", "apply", "notifications", "donotreply", "do-not-reply",
}

# Sender display-name fragments that indicate it's a person, not a company
PERSON_NAME_PATTERN = re.compile(
    r"^(mr|ms|mrs|dr|prof)\.?\s|"       # titles
    r"^[A-Z][a-z]+ [A-Z][a-z]+$",       # "First Last"
)


def _html_to_text(html: str) -> str:
    """Strip HTML to readable plain text."""
    # Remove style/script blocks entirely (content included)
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    # Block elements → newlines
    html = re.sub(r"<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
    # Clean up whitespace
    html = re.sub(r" {2,}", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


_HTML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|nbsp|quot|apos|#\d{1,5}|#x[\da-fA-F]{1,5});", re.IGNORECASE)


def _decode_body(payload: dict) -> str:
    """Prefer plain-text parts; fall back to HTML-to-text conversion."""
    plain_parts: list = []
    html_parts: list = []

    def _collect(p):
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data", "")
        if data:
            decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            if mime == "text/plain":
                # Some emails mislabel HTML content as text/plain (contains entities/tags).
                # Route those to html_parts so they don't duplicate the real plain text.
                if _HTML_ENTITY_RE.search(decoded) or "</" in decoded:
                    html_parts.append(decoded)
                else:
                    plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for sub in p.get("parts", []):
            _collect(sub)

    _collect(payload)

    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return _html_to_text("\n".join(html_parts))
    return ""


class GmailScanner:
    def __init__(self):
        self.creds: Optional[Credentials] = None
        self._load_token()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _load_token(self):
        if os.path.exists(TOKEN_FILE):
            self.creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if self.creds and self.creds.expired and self.creds.refresh_token:
            try:
                self.creds.refresh(Request())
                self._save_token()
            except (TransportError, RequestException):
                # A temporary outage does not mean the user revoked access.
                raise
            except Exception:
                self.creds = None

    def _save_token(self):
        if self.creds:
            with open(TOKEN_FILE, "w") as f:
                f.write(self.creds.to_json())

    def is_authenticated(self) -> bool:
        return bool(self.creds and self.creds.valid)

    def credentials_exist(self) -> bool:
        return os.path.exists(CREDENTIALS_FILE)

    def get_auth_url(self, redirect_uri: str):
        import secrets, hashlib, base64
        code_verifier = secrets.token_urlsafe(96)[:128]
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri
        )
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return url, code_verifier

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str = None):
        flow = Flow.from_client_secrets_file(
            CREDENTIALS_FILE, scopes=SCOPES, redirect_uri=redirect_uri
        )
        kwargs = {"code": code, "timeout": 30}
        if code_verifier:
            kwargs["code_verifier"] = code_verifier
        flow.fetch_token(**kwargs)
        self.creds = flow.credentials
        self._save_token()

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _should_skip(self, subject: str) -> bool:
        """Return True if this email is definitely not an internship email."""
        subj = subject.lower().strip()
        for pat in SKIP_PATTERNS:
            if re.search(pat, subj, re.IGNORECASE):
                return True
        return False

    def is_internship_related(self, subject: str, body: str, sender: str) -> bool:
        if self._should_skip(subject):
            return False

        text = (subject + " " + body[:800]).lower()

        # Indeed job-match/recommendation emails — NOT application confirmations
        if "match.indeed.com" in sender.lower():
            return False
        if "it looks like your background could be a match" in (body[:400]).lower():
            return False

        # Strong "you applied" signals — if present, trust it regardless of "intern" keyword
        applied_signals = [
            "thank you for applying", "thanks for applying",
            "thank you for your application", "thanks for your application",
            "you have successfully applied", "you've successfully applied",
            "successfully applied to", "application has been received",
            "application has been submitted", "we received your application",
            "completing your application", "you applied to",
        ]
        if any(sig in text for sig in applied_signals):
            return True

        # Strong interview / offer signals — trust even without "intern" keyword
        interview_signals = [
            "interview request", "interview invitation", "interview schedule",
            "we would like to invite you", "we'd like to invite you",
            "meeting confirmation", "meeting has been confirmed",
            "pleased to invite you", "next steps", "next round",
        ]
        if any(sig in text for sig in interview_signals):
            return True

        keywords = [
            "intern", "internship", "co-op", "coop", "new grad",
            "summer 2027", "winter 2027", "fall 2027",
            "software engineer", "data science", "machine learning",
            "your application", "application received", "you applied",
            "interview request", "interview invitation",
        ]
        # ATS domain in sender — only trust if the email also has application-related content
        _ats_signals = ["application", "applied", "your submission", "thank you", "we received"]
        for domain in RECRUITING_DOMAINS:
            if domain in sender.lower():
                if any(sig in text for sig in _ats_signals):
                    return True
                return False  # ATS domain but no application content (e.g. marketing newsletter)
        return any(kw in text for kw in keywords)

    # ── Classification ────────────────────────────────────────────────────────

    # High-confidence patterns that indicate a confirmation email, not a rejection/invitation.
    # If any of these match, Declined / Interview / Offer are skipped (unless explicit scheduling).
    _CONFIRMATION_GUARD = re.compile(
        r"thank(?:s| you) for (?:applying|your application|submitting|completing)|"
        r"thanks for completing your application|"
        r"application (?:received|submitted|confirmed)|"
        r"we(?:'ve| have) received your application|"
        r"your application.*?(?:submitted|received|complete)|"
        r"you(?:'ve| have) successfully applied|"
        r"confirming.*?(?:received|got).*?application|"
        r"you(?:'ve| have) applied to|"
        r"you applied to|"
        r"thank you for your (?:job )?application|"
        r"we received your (?:job )?application",
        re.IGNORECASE,
    )

    # OA completion confirmation — candidate has already submitted their assessment
    _OA_COMPLETION_RE = re.compile(
        r"thank(?:s| you) for completing.*?(?:assessment|test|challenge|exercise)|"
        r"(?:your|the) assessment.*?(?:has been |is )(?:submitted|received|complete)|"
        r"assessment.*?submission.*?(?:received|confirmed)|"
        r"(?:successfully )?(?:submitted|completed).*?(?:assessment|test|challenge)|"
        r"hackerrank.*?(?:submitted|completed)|"
        r"codesignal.*?(?:submitted|completed)|"
        r"codility.*?(?:submitted|completed)|"
        r"hirevue.*?(?:submitted|completed|recorded)|"
        r"your (?:online )?assessment has been (?:received|submitted)|"
        r"we have received your (?:completed )?assessment",
        re.IGNORECASE,
    )

    # Explicit interview scheduling language — requires the candidate to take action NOW
    # (not just a mention that scheduling MAY happen in the future)
    _INTERVIEW_SCHEDULING = re.compile(
        r"please (?:select|choose|pick) a time|"
        r"click (?:here )?to (?:schedule|book)|"
        r"use this link to (?:schedule|book)|"
        r"calendly\.com|"
        r"invite you to (?:a |an )?(?:phone|video|technical|virtual|in-?person)\s*(?:screen|interview|call)|"
        r"you(?:'ve| have) been (?:selected|invited) (?:for|to) (?:an? )?interview|"
        r"we(?:'d| would) like to (?:schedule|invite you for)",
        re.IGNORECASE,
    )

    def classify(self, subject: str, body: str, sender: str) -> Optional[str]:
        text = (subject + " " + body[:3000]).lower()
        full = subject + " " + body[:3000]
        is_confirmation = bool(self._CONFIRMATION_GUARD.search(full))
        has_scheduling = bool(self._INTERVIEW_SCHEDULING.search(text))

        for status in ("Offer", "Declined", "Interview", "OA", "Applied"):
            if status == "Declined" and is_confirmation:
                continue
            # Skip Interview and Offer on confirmation emails unless there's explicit scheduling language
            if status in ("Interview", "Offer") and is_confirmation and not has_scheduling:
                continue
            for pat in STATUS_PATTERNS[status]:
                if re.search(pat, text, re.IGNORECASE):
                    return status
        for domain in RECRUITING_DOMAINS:
            if domain in sender.lower():
                return "Applied"
        return None

    # ── Company extraction ────────────────────────────────────────────────────

    def _split_camel_company(self, name: str) -> str:
        """Split CamelCase/concatenated company names: 'Americanexpress' → 'American Express'."""
        _KNOWN = {
            "americanexpress": "American Express",
            "capitaloneHR": "Capital One", "capitalone": "Capital One",
            "janestreet": "Jane Street",
            "epicgames": "Epic Games", "thehartford": "The Hartford",
            "transmarketgroup": "TransMarket Group",
            "nuclearpromisx": "Nuclear Promise X",
            "veritiongroup": "Verition Group", "veritiongroupllc": "Verition Group",
            "fanniemae": "Fannie Mae", "freddiemac": "Freddie Mac",
            "hiltonrecruitment": "Hilton",
            "oraclecloud": "Oracle",
            "workdaysystemdonotreply": "Unknown Company",
            "humanaoroneof": "Humana",
        }
        lower = re.sub(r"[\s_\-.]", "", name.lower())
        for key, val in _KNOWN.items():
            if lower.startswith(key):
                return val
        return name

    def _clean_company_name(self, name: str) -> Optional[str]:
        """Strip platform/HR suffixes and validate the company name."""
        if not name:
            return None
        name = name.strip().rstrip(".,;:-")

        # Fix known CamelCase/concatenated company names
        name = self._split_camel_company(name)

        # Skip if it looks like a person's name
        if PERSON_NAME_PATTERN.match(name):
            return None

        # Handle "Talent Team at Company" / "Recruiting at Company" display names
        m_at = re.search(r"\bat\s+([A-Z][A-Za-z0-9 &'.,\-]{1,40})$", name)
        if m_at:
            name = m_at.group(1).strip()

        # Strip leading junk phrases before extracting the real company name
        name = re.sub(
            r"^(?:applying\s+(?:to|at|for)\s+|regarding\s+(?:the\s+)?|re:\s*|fwd:\s*|your\s+)",
            "", name, flags=re.IGNORECASE
        ).strip()
        if not name:
            return None

        # Skip known non-company names
        bad = {"noreply", "no-reply", "careers", "jobs", "hiring", "us",
               "team", "talent", "hr", "early", "notifications", "applying",
               "sending", "thanking", "reviewing", "reaching", "considering",
               "regarding", "re", "fwd", "your", "the", "link"}
        if name.lower() in bad:
            return None

        # Skip single-word verb forms ending in -ing (e.g. "Applying", "Hiring")
        if re.match(r"^[A-Za-z]+ing$", name):
            return None

        # Strip suffixes — run multiple passes until stable (handles stacked suffixes like "Hiring Team")
        for _ in range(3):
            prev = name
            for suffix in COMPANY_SUFFIXES:
                name = re.sub(suffix, "", name, flags=re.IGNORECASE).strip()
            if name == prev:
                break

        name = name.strip().rstrip(".,;:-")
        if not name or len(name) < 2:
            return None
        return name

    def extract_company(self, subject: str, sender: str, body: str = "") -> str:
        # 1. "applying to/at [Company]" or "applied to [Company]" in subject
        m = re.search(r"(?:applying|applied)\s+(?:at|to)\s+([A-Z][A-Za-z0-9 &'.,\-]{1,50}?)(?:[!?,.]|$)", subject, re.IGNORECASE)
        if m:
            raw = re.split(r"[–\-|]", m.group(1))[0]
            name = self._clean_company_name(raw)
            if name:
                return name

        # "Thank you for your interest in [Company]" in subject
        m = re.search(r"interest in\s+([A-Z][A-Za-z0-9 &'.,\-]{1,50}?)(?:[!?,.]|$)", subject, re.IGNORECASE)
        if m:
            name = self._clean_company_name(m.group(1).strip())
            if name:
                return name

        # 2. Workday sender — try "[Company]: [Role]" or "[Company] | Workday" in subject
        if "workday" in sender.lower():
            m = re.match(r"([A-Z][A-Za-z0-9 &'.,\-]{1,50}):\s+\S", subject)
            if m:
                name = self._clean_company_name(m.group(1))
                if name:
                    return name
            m = re.search(r"from\s+([A-Z][A-Za-z0-9 &'.,\-]{1,50})", subject, re.IGNORECASE)
            if m:
                name = self._clean_company_name(m.group(1))
                if name:
                    return name

        # 3. Indeed/Glassdoor sender — extract real company from subject
        if "indeed.com" in sender.lower() or "glassdoor" in sender.lower():
            # "Applied to [Role] at [Company]" or "[Company] sent you a message"
            for pat in [
                r"(?:at|from)\s+([A-Z][A-Za-z0-9 &'.,\-]{1,50}?)(?:\s*[-–|,]|\s*$)",
                r"([A-Z][A-Za-z0-9 &'.,\-]{2,50})\s+sent you",
            ]:
                m = re.search(pat, subject)
                if m:
                    name = self._clean_company_name(m.group(1))
                    if name:
                        return name

        # 4. Standard subject patterns (anchored start first to avoid "for Applying" false positives)
        for pat in [
            r"^([A-Z][A-Za-z0-9 &'.,\-]{1,40}?)\s*[-–|:]",
            r"([A-Z][A-Za-z0-9 &'.,\-]{1,40}?)\s+(?:internship|intern position|is excited)",
            r"(?:at|to|with|for)\s+([A-Z][A-Za-z0-9 &'.,\-]{1,40}?)(?:\s+(?:for|internship|position|role|and\b|has\b|was\b|is\b)|[,.!?]|$)",
        ]:
            m = re.search(pat, subject)
            if m:
                name = self._clean_company_name(m.group(1))
                if name:
                    return name

        # 5. Sender display name (after stripping platform junk)
        m = re.match(r'"?([^"<\n]+)"?\s*<', sender)
        if m:
            raw = m.group(1).strip()
            name = self._clean_company_name(raw)
            if name and len(name) < 50:
                return name

        # 6. Sender domain — use the second-level domain (skips subdomains like "mail" in mail.amazon.com)
        m = re.search(r"@([\w.-]+)", sender)
        if m:
            parts = m.group(1).lower().split(".")
            # Second-level domain is parts[-2] (e.g. "amazon" from "mail.amazon.com")
            domain = parts[-2] if len(parts) >= 2 else parts[0]
            if domain not in PLATFORM_DOMAINS:
                return domain.capitalize()

        return "Unknown Company"

    # ── Role / Cycle extraction ───────────────────────────────────────────────

    def extract_role(self, subject: str, body: str) -> str:
        text = subject + " " + body[:600]
        patterns = [
            # Full role phrase from confirmation sentences ("interest in the X position/role")
            r"(?:interest in(?: the)?|applying for(?: the)?|application for(?: the)?)\s+([^,\n]{5,80}?)\s*(?:,|\s+(?:position|role|opportunity)\b)",
            # Keyword-anchored role (software/data/ai/etc. engineer/developer/...)
            r"((?:software|data|product|ml|ai|research|hardware|systems?|backend|frontend|full.?stack|mobile|devops|sre|platform|quant)\s+(?:engineer(?:ing)?|developer|scientist|analyst|intern(?:ship)?)[^,\n]{0,40})",
            r"(intern(?:ship)?\s+(?:position|role|opportunity)[^,\n]{0,30})",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                role = m.group(1).strip()
                # Strip leading articles/pronouns
                role = re.sub(r"^(?:our|the|your|a|an|this)\s+", "", role, flags=re.IGNORECASE).strip()
                # Strip trailing noise: "at Company", "@ Company", "Hi Name", "! ...", trailing filler
                role = re.sub(r"\s+at\s+[A-Z]\S+.*$", "", role).strip()
                role = re.sub(r"\s+@\s*\S+.*$", "", role).strip()
                role = re.sub(r"[,!]\s+[Hh]i\b.*$", "", role).strip()
                role = re.sub(r"\s+[Hh]i\b\s+\w+.*$", "", role).strip()
                # Strip trailing: "role", "position! ...", "opening", "opportunity"
                role = re.sub(r"\s+(?:role|position[^.]*|opening|opportunity)\s*[!.]?.*$", "", role, flags=re.IGNORECASE).strip()
                role = role.rstrip("!.,;: ")
                _JUNK_ROLES = {
                    "career", "open", "this", "npx", "position", "role",
                    "opportunity", "job", "work", "joining", "team",
                    "interest", "applying", "application", "intern",
                }
                if role and len(role) >= 4 and role.lower() not in _JUNK_ROLES and len(role) < 80:
                    return role
        return "Software Engineering Intern"

    def detect_cycle(self, subject: str, body: str, email_date: str = "") -> Optional[str]:
        text = (subject + " " + body[:1000]).lower()

        if "summer 2027" in text:
            return "Summer 2027"
        if "winter 2027" in text:
            return "Winter 2027"

        # Skip emails explicitly about other cycles (e.g. fall 2026 co-ops)
        if "fall 2026" in text or "winter 2026" in text or "fall 2027" in text:
            return None

        # Default by date — if we're in the fall recruiting season, it's Summer 2027
        return "Summer 2027"

    # ── LLM Classification (Anthropic Claude Haiku) ──────────────────────────

    def _classify_with_llm(self, subject: str, sender: str, body: str) -> Optional[dict]:
        """
        Use Claude Haiku to extract company, role, status, and cycle from an email.
        Returns a dict or None if the call fails.
        """
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("[LLM] No ANTHROPIC_API_KEY set — skipping")
            return None

        try:
            import anthropic as _anthropic

            # Strip long URLs and image links before sending to LLM so they don't eat the char budget
            import re as _re
            clean_body = _re.sub(r'https?://\S{60,}', '', body)   # remove long URLs
            clean_body = _re.sub(r'\[https?://[^\]]+\]', '', clean_body)  # remove markdown links
            clean_body = _re.sub(r' {2,}|\n{3,}', ' ', clean_body).strip()

            prompt = f"""You are parsing an internship/job application email. Extract structured data as JSON.

Email:
Subject: {subject}
From: {sender}
Body (first 1200 chars): {clean_body[:1200]}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "is_internship": true or false,
  "company": "Exact company name (e.g. Capital One, NXP, American Express) or null",
  "role": "Job title only (e.g. Software Engineering Intern, Data Science Intern) or null",
  "status": "Applied" | "OA" | "Interview" | "Offer" | "Declined",
  "cycle": "Summer 2027" | "Winter 2027" | null
}}

Rules:
- is_internship=true: application confirmation, OA invite, interview invite, offer, or rejection for an internship/new-grad role
- is_internship=false: job alert, recommendation email, newsletter, verification code, hackathon, marketing
- company: the ACTUAL hiring company, never the ATS platform (Greenhouse, Lever, Workday, Ashby, iCIMS, etc.)
  - e.g. sender "Capital One HR Workday <noreply@myworkdayjobs.com>" means company is "Capital One"
  - Fix camelCase/concatenated names: "Americanexpress" -> "American Express", "Janestreet" -> "Jane Street"
  - Remove suffixes: "AMD Application" -> "AMD", "Goldman Sachs Recruiting" -> "Goldman Sachs"
- role: clean job title only, no body text (NOT "joining our team at ...")
- status: if is_internship=true, always provide — Applied=confirmation, OA=assessment invited, Interview=interview scheduled, Offer=offer extended, Declined=rejection
- cycle: "Summer 2027" or "Winter 2027" only if explicitly stated, else null

Return only the JSON object."""

            client = _anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
            return json.loads(text)

        except Exception as e:
            print(f"[LLM] Failed ({type(e).__name__}: {e})")
            return None

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(self, db, max_results: int = 300) -> Tuple[int, int]:
        import time
        service = build("gmail", "v1", credentials=self.creds)

        search_query = (
            "(internship OR intern OR \"software engineer intern\" OR co-op OR coop OR "
            "\"winter 2027\" OR \"summer 2027\" OR "
            "interview OR \"your application\" OR \"application received\" OR \"you applied\" OR "
            "\"thank you for applying\" OR \"thank you for submitting\" OR "
            "\"we received your application\" OR \"received your application\" OR "
            "\"submitting your application\") "
            "(application OR interview OR offer OR assessment OR declined OR "
            "congratulations OR \"not moving forward\" OR unfortunately OR "
            "\"thank you\" OR received OR submitted OR request OR confirmation OR developer)"
        )

        response = service.users().messages().list(
            userId="me", q=search_query, maxResults=max_results
        ).execute()

        messages = response.get("messages", [])
        new_count = updated_count = 0

        for i, ref in enumerate(messages):
            # Pause every 10 requests to stay under quota
            if i > 0 and i % 10 == 0:
                time.sleep(1)
            try:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="full"
                ).execute()

                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                subject = headers.get("Subject", "")
                sender = headers.get("From", "")
                date_str = headers.get("Date", "")
                thread_id = msg["threadId"]

                body = _decode_body(msg["payload"])

                try:
                    email_date = parsedate_to_datetime(date_str).isoformat()
                except Exception:
                    email_date = datetime.now().isoformat()

                # ── Try LLM first, fall back to regex ──────────────────────
                llm = self._classify_with_llm(subject, sender, body)

                if not llm:
                    # LLM unavailable — skip and log clearly
                    print(f"[sync] SKIPPED (LLM failed): '{subject[:80]}' | from: {sender[:60]}")
                    continue

                if llm.get("is_internship") is False:
                    continue  # LLM said not an internship email

                if not llm.get("is_internship") or not llm.get("status"):
                    continue  # LLM uncertain — skip

                company = llm.get("company") or "Unknown Company"
                role = llm.get("role") or "Software Engineering Intern"
                cycle = llm.get("cycle") or self.detect_cycle(subject, body, email_date)
                if cycle is None:
                    continue
                status = llm["status"]

                # Detect OA completion confirmation emails (override LLM status if needed)
                oa_completed = 0
                if self._OA_COMPLETION_RE.search(subject + " " + body[:2000]):
                    status = "OA"
                    oa_completed = 1

                app_data = {
                    "company": company,
                    "role": role,
                    "status": status,
                    "cycle": cycle,
                    "applied_date": email_date[:10],
                    "email_thread_id": thread_id,
                    "notes": f"Auto-synced | {subject[:120]}",
                    "email_body": f"Subject: {subject}\nFrom: {sender}\n\n{body[:8000]}",
                    "oa_completed": oa_completed,
                }

                _, was_updated = db.upsert_by_thread(app_data)
                if was_updated:
                    updated_count += 1
                else:
                    new_count += 1

            except Exception as e:
                print(f"[gmail_scanner] Error on {ref['id']}: {e}")
                continue

        return new_count, updated_count
