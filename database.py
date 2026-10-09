import re as _re
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict

_ROLE_STOP_WORDS = frozenset({
    'and', 'or', 'the', 'a', 'an', 'for', 'at', 'of', 'in', 'to', 'with',
    'by', 'amp', 'co', 'op',
    # cycle noise that the LLM sometimes prepends/appends to role names
    'summer', 'winter', 'fall', 'spring',
})

_ROUND_RE = _re.compile(
    r'\b(?:first|1st|second|2nd|third|3rd|fourth|4th|final)\s*[-\s]*round\b'
    r'|\bround\s*[1-9]\b'
    r'|\b(?:onsite|on-site|superday|phone\s*screen)\b',
    _re.IGNORECASE,
)


class Database:
    def __init__(self, db_path: str = "internships.db"):
        self.db_path = db_path
        self._init_db()
        self._migrate()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS applications (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    company         TEXT    NOT NULL,
                    role            TEXT,
                    status          TEXT    DEFAULT 'Applied',
                    cycle           TEXT    DEFAULT 'Summer 2027',
                    applied_date    TEXT,
                    last_updated    TEXT    DEFAULT CURRENT_TIMESTAMP,
                    email_thread_id TEXT    UNIQUE,
                    email_body      TEXT,
                    notes           TEXT,
                    link            TEXT,
                    location        TEXT,
                    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id  INTEGER,
                    type            TEXT,
                    message         TEXT,
                    seen            INTEGER DEFAULT 0,
                    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (application_id) REFERENCES applications(id)
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id  INTEGER NOT NULL,
                    issue_type      TEXT    NOT NULL,
                    suggested_status TEXT,
                    note            TEXT,
                    resolved        INTEGER DEFAULT 0,
                    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (application_id) REFERENCES applications(id)
                );
            """)
            conn.commit()

    def _migrate(self):
        """Add columns that may be missing in older DBs."""
        with self._conn() as conn:
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(applications)").fetchall()
            }
            if "email_body" not in existing:
                conn.execute("ALTER TABLE applications ADD COLUMN email_body TEXT")
                conn.commit()
            if "oa_completed" not in existing:
                conn.execute("ALTER TABLE applications ADD COLUMN oa_completed INTEGER DEFAULT 0")
                conn.commit()

    # ── Deduplication helpers ─────────────────────────────────────────────────

    @staticmethod
    def _normalize_company(name: str) -> str:
        s = name.lower().strip()
        s = _re.sub(
            r'\b(?:inc|corp|llc|ltd|co|company|group|technologies|tech|'
            r'solutions|services|international)\b\.?', ' ', s
        )
        s = _re.sub(r'[^a-z0-9\s]', ' ', s)
        return _re.sub(r'\s+', ' ', s).strip()

    @staticmethod
    def _normalize_role(role: str) -> str:
        s = role.lower().strip()
        s = _re.sub(r'[^a-z0-9\s]', ' ', s)
        s = _re.sub(r'\b20\d{2}\b', ' ', s)   # strip year tokens (2025, 2027, …)
        return _re.sub(r'\s+', ' ', s).strip()

    @classmethod
    def _roles_match(cls, role_a: str, role_b: str) -> bool:
        """Return True if two role strings refer to the same position."""
        a = cls._normalize_role(role_a)
        b = cls._normalize_role(role_b)
        if a == b:
            return True
        if not a or not b:
            return False
        tokens_a = frozenset(a.split()) - _ROLE_STOP_WORDS
        tokens_b = frozenset(b.split()) - _ROLE_STOP_WORDS
        if not tokens_a or not tokens_b:
            return a == b
        inter = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(inter) / len(union) >= 0.5

    @staticmethod
    def _extract_round(text: str) -> Optional[str]:
        """Return a normalised round label (e.g. 'final round') or None."""
        m = _ROUND_RE.search(text or '')
        return m.group(0).lower() if m else None

    def find_existing_application(
        self, company: str, role: str, cycle: str
    ) -> Optional[Dict]:
        """Find an existing entry by normalised company + role + cycle, ignoring thread IDs."""
        norm_co = self._normalize_company(company)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM applications WHERE cycle = ?"
                " ORDER BY applied_date ASC, created_at ASC",
                (cycle,),
            ).fetchall()
        for row in rows:
            row = dict(row)
            if (self._normalize_company(row['company']) == norm_co
                    and self._roles_match(role, row.get('role', ''))):
                return row
        return None

    # ── Applications ──────────────────────────────────────────────────────────

    def get_applications(self, cycle: str = "all", search: str = "") -> List[Dict]:
        q = "SELECT * FROM applications WHERE 1=1"
        params: list = []
        if cycle != "all":
            q += " AND cycle = ?"
            params.append(cycle)
        if search:
            q += " AND (company LIKE ? OR role LIKE ? OR location LIKE ?)"
            params += [f"%{search}%"] * 3
        q += " ORDER BY applied_date DESC, created_at DESC"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(q, params).fetchall()]

    def get_application(self, app_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
            return dict(row) if row else None

    def get_by_thread_id(self, thread_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE email_thread_id = ?", (thread_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_by_thread_id(self, thread_id: str) -> List[Dict]:
        """Return all applications associated with a Gmail thread (including role-scoped keys)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM applications WHERE email_thread_id = ? OR email_thread_id LIKE ?",
                (thread_id, f"{thread_id}:%"),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self, cycle: str = "all") -> Dict:
        q = ("SELECT status, COUNT(*) AS cnt FROM applications "
             "WHERE id NOT IN (SELECT application_id FROM feedback WHERE resolved=0)")
        params: list = []
        if cycle != "all":
            q += " AND cycle = ?"
            params.append(cycle)
        q += " GROUP BY status"
        stats = {"Applied": 0, "OA": 0, "Interview": 0, "Offer": 0, "Declined": 0, "total": 0}
        with self._conn() as conn:
            for row in conn.execute(q, params).fetchall():
                s = row["status"]
                if s in stats:
                    stats[s] = row["cnt"]
                stats["total"] += row["cnt"]
            # Flagged count
            stats["flagged"] = conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE resolved=0"
            ).fetchone()[0]
            # OA completed count (respects cycle filter)
            oa_q = "SELECT COUNT(*) FROM applications WHERE status='OA' AND oa_completed=1"
            oa_params: list = []
            if cycle != "all":
                oa_q += " AND cycle=?"
                oa_params.append(cycle)
            stats["oa_completed"] = conn.execute(oa_q, oa_params).fetchone()[0]
        return stats

    def create_application(self, data: Dict) -> int:
        now = datetime.now().isoformat()
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO applications
                    (company, role, status, cycle, applied_date, last_updated,
                     notes, link, location, email_thread_id, email_body, oa_completed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                data.get("company", ""),
                data.get("role", ""),
                data.get("status", "Applied"),
                data.get("cycle", "Summer 2027"),
                data.get("applied_date", now[:10]),
                now,
                data.get("notes", ""),
                data.get("link", ""),
                data.get("location", ""),
                data.get("email_thread_id"),
                data.get("email_body", ""),
                data.get("oa_completed", 0),
            ))
            app_id = cur.lastrowid
            conn.execute("""
                INSERT INTO notifications (application_id, type, message)
                VALUES (?, 'applied', ?)
            """, (app_id, f"Applied to {data.get('company','')} — {data.get('role','')}"))
            conn.commit()
        return app_id

    def upsert_by_thread(self, data: Dict) -> tuple:
        thread_id = data.get("email_thread_id")
        if not thread_id:
            return self.create_application(data), False

        new_role = data.get("role") or ""
        all_in_thread = self.get_all_by_thread_id(thread_id)

        # 1. Thread-based match using _roles_match (fixes the empty-fingerprint bug for roles
        #    like "Graduate Programme" / "Technology Internship" that share no _ROLE_KEYWORDS tokens)
        existing = None
        for entry in all_in_thread:
            if self._roles_match(new_role, entry.get("role") or ""):
                existing = entry
                break

        # 2. Cross-thread match — same company + role + cycle arriving in a different Gmail thread.
        #    Handles companies sending confirmation, invite, reminder, and calendar emails as
        #    separate threads rather than replies within the same conversation.
        if existing is None:
            cross = self.find_existing_application(
                data.get("company", ""),
                new_role,
                data.get("cycle", ""),
            )
            if cross:
                # Only create a separate entry when both emails explicitly name different rounds
                # (e.g. "first round" vs "final round"). Generic interview emails merge.
                new_round = self._extract_round(data.get("notes", ""))
                existing_round = self._extract_round(cross.get("notes", ""))
                if not (new_round and existing_round and new_round != existing_round):
                    existing = cross

        if existing:
            _STATUS_RANK = {"Applied": 0, "OA": 1, "Interview": 2, "Offer": 3, "Declined": 4}
            update = {}
            old_status = existing["status"]
            new_status = data.get("status", old_status)
            # Only advance status, never go backward
            if (old_status != new_status
                    and _STATUS_RANK.get(new_status, 0) > _STATUS_RANK.get(old_status, 0)):
                update["status"] = new_status
            if data.get("email_body"):
                update["email_body"] = data["email_body"]
            if data.get("oa_completed") and not existing.get("oa_completed"):
                update["oa_completed"] = 1
            if update:
                self.update_application(existing["id"], update)
                return existing["id"], old_status != new_status
            return existing["id"], False

        # Different role in the same thread — create with a role-scoped key so future
        # thread lookups can still distinguish the two positions.
        if all_in_thread:
            role_slug = _re.sub(r"[^a-z0-9]", "", new_role.lower()[:20])
            data = dict(data)
            data["email_thread_id"] = f"{thread_id}:{role_slug}"

        return self.create_application(data), False

    def consolidate_duplicates(self) -> int:
        """Merge existing duplicate rows that share the same company + role + cycle.

        For each group of duplicates:
        - The entry with the highest status rank is kept as canonical.
        - Notes from all duplicates are merged into the canonical entry.
        - The earliest applied_date is preserved.
        - Notifications and feedback rows are re-pointed to the canonical entry.
        - Duplicate rows are deleted.

        Returns the number of rows removed.
        """
        _STATUS_RANK = {"Applied": 0, "OA": 1, "Interview": 2, "Offer": 3, "Declined": 4}
        all_apps = self.get_applications()

        # Group by (normalised_company, cycle) — role clustering is handled by _roles_match
        # below, so we deliberately don't include the role in the key. This lets entries
        # like "Developer" and "Winter 2027 Developer" end up in the same bucket.
        groups: dict = {}
        for app in all_apps:
            norm_co = self._normalize_company(app["company"])
            key = (norm_co, app.get("cycle") or "")
            groups.setdefault(key, []).append(app)

        removed = 0
        for group in groups.values():
            if len(group) < 2:
                continue

            # Re-cluster within the group using _roles_match in case two distinct
            # normalised strings were coarsely bucketed into the same key.
            clusters: list = []
            for app in group:
                placed = False
                for cluster in clusters:
                    if self._roles_match(app.get("role") or "", cluster[0].get("role") or ""):
                        cluster.append(app)
                        placed = True
                        break
                if not placed:
                    clusters.append([app])

            for cluster in clusters:
                if len(cluster) < 2:
                    continue

                # Canonical = highest status; ties broken by oldest applied_date, then lowest id
                cluster.sort(key=lambda a: (
                    -_STATUS_RANK.get(a["status"], 0),
                    a.get("applied_date") or "",
                    a["id"],
                ))
                canonical = cluster[0]
                duplicates = cluster[1:]

                merged_notes = canonical.get("notes") or ""
                earliest_date = canonical.get("applied_date") or ""

                for dup in duplicates:
                    dup_note = (dup.get("notes") or "").strip()
                    if dup_note and dup_note not in merged_notes:
                        merged_notes = (merged_notes + "\n" + dup_note).strip()
                    dup_date = dup.get("applied_date") or ""
                    if dup_date and (not earliest_date or dup_date < earliest_date):
                        earliest_date = dup_date

                update: dict = {}
                if merged_notes != (canonical.get("notes") or ""):
                    update["notes"] = merged_notes
                if earliest_date != (canonical.get("applied_date") or ""):
                    update["applied_date"] = earliest_date
                if update:
                    self.update_application(canonical["id"], update)

                with self._conn() as conn:
                    for dup in duplicates:
                        conn.execute(
                            "UPDATE notifications SET application_id=? WHERE application_id=?",
                            (canonical["id"], dup["id"]),
                        )
                        conn.execute(
                            "UPDATE feedback SET application_id=? WHERE application_id=?",
                            (canonical["id"], dup["id"]),
                        )
                        conn.execute("DELETE FROM applications WHERE id=?", (dup["id"],))
                        removed += 1
                    conn.commit()

        return removed

    def update_application(self, app_id: int, data: Dict):
        now = datetime.now().isoformat()
        fields, params = [], []
        for key in ["company", "role", "status", "cycle", "applied_date",
                    "notes", "link", "location", "email_body", "oa_completed"]:
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return

        old_row = None
        if "status" in data:
            with self._conn() as conn:
                old_row = conn.execute(
                    "SELECT status, company FROM applications WHERE id=?", (app_id,)
                ).fetchone()

        fields.append("last_updated = ?")
        params += [now, app_id]

        with self._conn() as conn:
            conn.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id=?", params)
            if old_row and data.get("status") and old_row["status"] != data["status"]:
                conn.execute("""
                    INSERT INTO notifications (application_id, type, message)
                    VALUES (?, 'status_change', ?)
                """, (app_id, f"{old_row['company']}: {old_row['status']} → {data['status']}"))
            conn.commit()

    def delete_application(self, app_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM feedback WHERE application_id=?", (app_id,))
            conn.execute("DELETE FROM notifications WHERE application_id=?", (app_id,))
            conn.execute("DELETE FROM applications WHERE id=?", (app_id,))
            conn.commit()

    # ── Notifications ─────────────────────────────────────────────────────────

    def get_unseen_notifications(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT n.id, n.type, n.message, n.created_at, a.company, a.role, a.status
                FROM notifications n
                JOIN applications a ON n.application_id = a.id
                WHERE n.seen = 0
                ORDER BY n.created_at DESC
            """).fetchall()
            result = [dict(r) for r in rows]
            if result:
                ids = ",".join("?" * len(result))
                conn.execute(f"UPDATE notifications SET seen=1 WHERE id IN ({ids})",
                             [r["id"] for r in result])
                conn.commit()
            return result

    # ── Feedback ──────────────────────────────────────────────────────────────

    def create_feedback(self, data: Dict) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO feedback (application_id, issue_type, suggested_status, note)
                VALUES (?, ?, ?, ?)
            """, (
                data["application_id"],
                data.get("issue_type", "wrong_status"),
                data.get("suggested_status"),
                data.get("note", ""),
            ))
            conn.commit()
            return cur.lastrowid

    def get_feedback(self, resolved: bool = False) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT f.*, a.company, a.role, a.status AS current_status,
                       a.cycle, a.applied_date, a.email_body
                FROM feedback f
                JOIN applications a ON f.application_id = a.id
                WHERE f.resolved = ?
                ORDER BY f.created_at DESC
            """, (1 if resolved else 0,)).fetchall()
            return [dict(r) for r in rows]

    def resolve_feedback(self, feedback_id: int, apply_suggestion: bool = False):
        with self._conn() as conn:
            if apply_suggestion:
                row = conn.execute(
                    "SELECT application_id, suggested_status FROM feedback WHERE id=?",
                    (feedback_id,)
                ).fetchone()
                if row and row["suggested_status"]:
                    self.update_application(row["application_id"],
                                            {"status": row["suggested_status"]})
                elif row and not row["suggested_status"]:
                    # False positive — delete the application
                    self.delete_application(row["application_id"])
            conn.execute("UPDATE feedback SET resolved=1 WHERE id=?", (feedback_id,))
            conn.commit()

    def delete_feedback(self, feedback_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM feedback WHERE id=?", (feedback_id,))
            conn.commit()
