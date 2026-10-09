"""Regression tests for duplicate-application prevention and consolidation."""

import pytest
from database import Database


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "test.db"))


def _app(company, role, status="Applied", cycle="Summer 2027",
         thread_id=None, notes="", body=""):
    return {
        "company": company,
        "role": role,
        "status": status,
        "cycle": cycle,
        "applied_date": "2025-01-01",
        "email_thread_id": thread_id,
        "notes": notes,
        "email_body": body,
    }


# ── _roles_match ──────────────────────────────────────────────────────────────

class TestRolesMatch:
    def test_identical(self, db):
        assert db._roles_match("Software Engineering Intern", "Software Engineering Intern")

    def test_case_insensitive(self, db):
        assert db._roles_match("software engineering intern", "Software Engineering Intern")

    def test_punctuation_ignored(self, db):
        assert db._roles_match("AI, Data & ML Engineering Intern", "AI Data ML Engineering Intern")

    def test_high_overlap(self, db):
        # "Software Engineering Intern" vs "Software Engineer Intern" — 2/4 tokens shared → 0.5
        assert db._roles_match("Software Engineering Intern", "Software Engineer Intern")

    def test_different_roles(self, db):
        assert not db._roles_match("Applied ML Intern", "Applied AI Program Design & Delivery Intern")

    def test_empty_roles(self, db):
        assert db._roles_match("", "")

    def test_one_empty(self, db):
        # One empty means we can't distinguish — treat as non-match to avoid false merges
        assert not db._roles_match("Software Engineering Intern", "")

    def test_roles_outside_keyword_set(self, db):
        # "Graduate Programme" has no tokens in _ROLE_KEYWORDS — must still match itself
        assert db._roles_match("Graduate Programme", "Graduate Programme")

    def test_technology_internship(self, db):
        assert db._roles_match("Technology Internship", "Technology Internship")

    def test_totally_different(self, db):
        assert not db._roles_match("Software Engineering Intern", "Data Science Intern")


# ── _normalize_company ────────────────────────────────────────────────────────

class TestNormalizeCompany:
    def test_strips_inc(self, db):
        assert db._normalize_company("Nike, Inc.") == db._normalize_company("Nike")

    def test_lowercase(self, db):
        assert db._normalize_company("RBC") == db._normalize_company("rbc")

    def test_punctuation(self, db):
        assert db._normalize_company("Nike, Inc.") == db._normalize_company("Nike Inc")


# ── upsert_by_thread: same-thread deduplication ───────────────────────────────

class TestSameThreadDedup:
    def test_same_thread_same_role_updates(self, db):
        """Two emails in the same thread for the same role → one entry, status advances."""
        tid = "thread_abc"
        id1, _ = db.upsert_by_thread(_app("Tesla", "Software Engineering Intern",
                                          thread_id=tid, status="Applied"))
        id2, changed = db.upsert_by_thread(_app("Tesla", "Software Engineering Intern",
                                                 thread_id=tid, status="Interview"))
        assert id1 == id2
        assert changed
        row = db.get_application(id1)
        assert row["status"] == "Interview"

    def test_same_thread_no_status_downgrade(self, db):
        """Status never goes backward on re-sync."""
        tid = "thread_abc"
        id1, _ = db.upsert_by_thread(_app("Tesla", "SWE Intern",
                                          thread_id=tid, status="Interview"))
        id2, changed = db.upsert_by_thread(_app("Tesla", "SWE Intern",
                                                 thread_id=tid, status="Applied"))
        assert id1 == id2
        assert not changed
        assert db.get_application(id1)["status"] == "Interview"

    def test_roles_outside_keyword_set_same_thread(self, db):
        """Roles with no _ROLE_KEYWORDS tokens (e.g. 'Graduate Programme') deduplicate correctly."""
        tid = "thread_grad"
        id1, _ = db.upsert_by_thread(_app("Brevan Howard", "Graduate Programme",
                                          thread_id=tid, status="Applied"))
        id2, _ = db.upsert_by_thread(_app("Brevan Howard", "Graduate Programme",
                                          thread_id=tid, status="OA"))
        assert id1 == id2
        assert db.get_application(id1)["status"] == "OA"

    def test_different_roles_same_thread_creates_two_entries(self, db):
        """Genuinely different roles in one thread each get their own row."""
        tid = "thread_multi"
        id1, _ = db.upsert_by_thread(_app("Amazon", "Software Engineering Intern",
                                          thread_id=tid))
        id2, _ = db.upsert_by_thread(_app("Amazon", "Data Science Intern",
                                          thread_id=tid))
        assert id1 != id2


# ── upsert_by_thread: cross-thread deduplication ─────────────────────────────

class TestCrossThreadDedup:
    def test_reminder_different_thread_merges(self, db):
        """Interview reminder in a new thread merges into the existing entry."""
        id1, _ = db.upsert_by_thread(_app("Vector Institute", "Applied Machine Learning Intern",
                                          thread_id="thread_1", status="Interview"))
        id2, _ = db.upsert_by_thread(_app("Vector Institute", "Applied Machine Learning Intern",
                                          thread_id="thread_2", status="Interview"))
        assert id1 == id2
        assert db.get_application(id1)["status"] == "Interview"

    def test_calendar_confirmation_different_thread_merges(self, db):
        """Calendar confirmation email (new thread) doesn't create a duplicate."""
        id1, _ = db.upsert_by_thread(_app("Nike, Inc.", "AI, Data & ML Engineering Intern",
                                          thread_id="thread_a", status="Interview"))
        id2, _ = db.upsert_by_thread(_app("Nike", "AI Data ML Engineering Intern",
                                          thread_id="thread_b", status="Interview"))
        assert id1 == id2

    def test_cross_thread_status_advances(self, db):
        """New thread with higher status advances the existing entry."""
        id1, _ = db.upsert_by_thread(_app("RBC", "Developer",
                                          thread_id="thread_1", status="Applied"))
        id2, changed = db.upsert_by_thread(_app("RBC", "Developer",
                                                 thread_id="thread_2", status="Interview"))
        assert id1 == id2
        assert changed
        assert db.get_application(id1)["status"] == "Interview"

    def test_idempotent_resync(self, db):
        """Re-processing the same emails never creates extra rows."""
        emails = [
            _app("Coinbase", "Software Engineering Intern", thread_id="t1", status="Applied"),
            _app("Coinbase", "Software Engineering Intern", thread_id="t2", status="Applied"),
            _app("Coinbase", "Software Engineering Intern", thread_id="t3", status="Applied"),
            _app("Coinbase", "Software Engineering Intern", thread_id="t1", status="Applied"),
        ]
        for e in emails:
            db.upsert_by_thread(e)
        results = db.get_applications()
        coinbase = [r for r in results if r["company"] == "Coinbase"]
        assert len(coinbase) == 1

    def test_different_positions_same_company_not_merged(self, db):
        """Two different roles at the same company stay separate."""
        id1, _ = db.upsert_by_thread(_app("Vector Institute",
                                          "Applied Machine Learning Intern",
                                          thread_id="t1", status="Interview"))
        id2, _ = db.upsert_by_thread(_app("Vector Institute",
                                          "Applied AI Program Design & Delivery Intern",
                                          thread_id="t2", status="Interview"))
        assert id1 != id2

    def test_different_companies_not_merged(self, db):
        """Same role at different companies stays separate."""
        id1, _ = db.upsert_by_thread(_app("Tesla", "Software Engineering Intern",
                                          thread_id="t1"))
        id2, _ = db.upsert_by_thread(_app("Google", "Software Engineering Intern",
                                          thread_id="t2"))
        assert id1 != id2

    def test_different_cycles_not_merged(self, db):
        """Same role, same company but different cycles stay separate."""
        id1, _ = db.upsert_by_thread(_app("Apple", "Software Engineering Intern",
                                          cycle="Summer 2027", thread_id="t1"))
        id2, _ = db.upsert_by_thread(_app("Apple", "Software Engineering Intern",
                                          cycle="Winter 2027", thread_id="t2"))
        assert id1 != id2


# ── upsert_by_thread: interview round preservation ───────────────────────────

class TestInterviewRoundPreservation:
    def test_different_explicit_rounds_stay_separate(self, db):
        """Emails with explicitly different round labels are kept as separate entries."""
        id1, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t1", status="Interview",
                                          notes="Auto-synced | First round interview"))
        id2, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t2", status="Interview",
                                          notes="Auto-synced | Final round interview"))
        assert id1 != id2

    def test_same_round_label_merges(self, db):
        """Two emails for the same round (both say 'final round') merge."""
        id1, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t1", status="Interview",
                                          notes="Auto-synced | Final round interview"))
        id2, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t2", status="Interview",
                                          notes="Auto-synced | Final round reminder"))
        assert id1 == id2

    def test_no_round_info_merges(self, db):
        """Generic interview emails with no round info merge (most common case)."""
        id1, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t1", status="Interview",
                                          notes="Auto-synced | Interview invitation"))
        id2, _ = db.upsert_by_thread(_app("Stripe", "Software Engineering Intern",
                                          thread_id="t2", status="Interview",
                                          notes="Auto-synced | Interview confirmation"))
        assert id1 == id2


# ── consolidate_duplicates ────────────────────────────────────────────────────

class TestConsolidateDuplicates:
    def test_merges_existing_duplicates(self, db):
        """Existing duplicate rows (from old syncs) are merged into one."""
        # Simulate pre-fix state: three rows for the same position, different thread IDs
        db.create_application(_app("Tesla", "Software Engineering Intern",
                                   thread_id="t1", status="Applied"))
        db.create_application(_app("Tesla", "Software Engineering Intern",
                                   thread_id="t2", status="Applied"))
        db.create_application(_app("Tesla", "Software Engineering Intern",
                                   thread_id="t3", status="Interview"))

        removed = db.consolidate_duplicates()
        assert removed == 2
        remaining = [r for r in db.get_applications() if r["company"] == "Tesla"]
        assert len(remaining) == 1
        assert remaining[0]["status"] == "Interview"  # highest status preserved

    def test_preserves_different_positions(self, db):
        """Different roles at the same company are not merged."""
        db.create_application(_app("Vector Institute", "Applied Machine Learning Intern",
                                   thread_id="t1", status="Interview"))
        db.create_application(_app("Vector Institute",
                                   "Applied AI Program Design & Delivery Intern",
                                   thread_id="t2", status="Interview"))

        removed = db.consolidate_duplicates()
        assert removed == 0
        remaining = [r for r in db.get_applications()
                     if r["company"] == "Vector Institute"]
        assert len(remaining) == 2

    def test_merges_notes(self, db):
        """Notes from duplicate rows are merged into the canonical entry."""
        db.create_application(_app("Nike", "AI Data ML Engineering Intern",
                                   thread_id="t1", notes="Auto-synced | Invitation email"))
        db.create_application(_app("Nike", "AI Data ML Engineering Intern",
                                   thread_id="t2", notes="Auto-synced | Reminder email"))

        db.consolidate_duplicates()
        remaining = db.get_applications()
        assert len(remaining) == 1
        assert "Invitation email" in remaining[0]["notes"]
        assert "Reminder email" in remaining[0]["notes"]

    def test_no_false_merges_different_companies(self, db):
        """consolidate_duplicates never merges entries from different companies."""
        db.create_application(_app("Tesla", "Software Engineering Intern", thread_id="t1"))
        db.create_application(_app("Google", "Software Engineering Intern", thread_id="t2"))

        removed = db.consolidate_duplicates()
        assert removed == 0
        assert len(db.get_applications()) == 2

    def test_idempotent(self, db):
        """Running consolidate_duplicates twice is safe and produces the same result."""
        db.create_application(_app("Nike", "AI Data ML Engineering Intern",
                                   thread_id="t1", status="Interview"))
        db.create_application(_app("Nike", "AI Data ML Engineering Intern",
                                   thread_id="t2", status="Interview"))
        db.create_application(_app("Nike", "AI Data ML Engineering Intern",
                                   thread_id="t3", status="Interview"))

        removed1 = db.consolidate_duplicates()
        count_after_first = len(db.get_applications())
        removed2 = db.consolidate_duplicates()
        count_after_second = len(db.get_applications())

        assert removed1 == 2
        assert removed2 == 0
        assert count_after_first == count_after_second == 1

    def test_notifications_reassigned(self, db):
        """Notifications from duplicate rows are transferred to the canonical entry."""
        id1 = db.create_application(_app("RBC", "Developer", thread_id="t1"))
        id2 = db.create_application(_app("RBC", "Developer", thread_id="t2"))

        db.consolidate_duplicates()

        # Both notification rows should now point to whichever entry survived
        surviving = db.get_applications()
        assert len(surviving) == 1
        survivor_id = surviving[0]["id"]
        with db._conn() as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE application_id NOT IN "
                "(SELECT id FROM applications)"
            ).fetchone()[0]
        assert orphans == 0
