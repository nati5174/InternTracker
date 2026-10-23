"""Gmail outages should not expose a traceback or look like revoked access."""
import importlib.util
from pathlib import Path
from unittest.mock import patch, Mock

import pytest
from requests.exceptions import ConnectionError
from google.auth.exceptions import TransportError
from oauthlib.oauth2 import InvalidGrantError


@pytest.fixture
def tracker(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    spec = importlib.util.spec_from_file_location("tracker_error_test", Path(__file__).with_name("app.py"))
    module = importlib.util.module_from_spec(spec)
    with patch("database.Database"):
        spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


def test_callback_network_failure_shows_retry_page(tracker):
    client = tracker.app.test_client()
    with client.session_transaction() as session:
        session.update(oauth_state="valid-state", oauth_code_verifier="verifier")
    with patch.object(tracker, "GmailScanner") as scanner:
        scanner.return_value.exchange_code.side_effect = ConnectionError("secret-token")
        response = client.get("/auth/callback?state=valid-state&code=code")
    assert response.status_code == 503
    assert b"Connect Gmail again" in response.data
    assert b"secret-token" not in response.data


def test_sync_transport_failure_returns_json(tracker):
    with patch.object(tracker, "GmailScanner", side_effect=TransportError("secret")):
        response = tracker.app.test_client().post("/api/sync")
    assert response.status_code == 503
    assert "Cannot reach Google" in response.json["error"]


def test_expired_oauth_code_shows_retry_page(tracker):
    client = tracker.app.test_client()
    with client.session_transaction() as session:
        session["oauth_state"] = "state"
    with patch.object(tracker, "GmailScanner") as scanner:
        scanner.return_value.exchange_code.side_effect = InvalidGrantError()
        response = client.get("/auth/callback?state=state&code=expired")
    assert response.status_code == 400
    assert b"Connect Gmail again" in response.data


def test_invalid_state_does_not_exchange_code(tracker):
    with patch.object(tracker, "GmailScanner") as scanner:
        response = tracker.app.test_client().get("/auth/callback?state=wrong&code=code")
    assert response.status_code == 400
    scanner.assert_not_called()


def test_refresh_outage_preserves_token_file(tmp_path):
    import gmail_scanner
    token = tmp_path / "token.json"
    token.write_text("existing-token")
    credentials = Mock(expired=True, refresh_token="refresh")
    credentials.refresh.side_effect = TransportError("offline")
    with patch.object(gmail_scanner, "TOKEN_FILE", str(token)), patch.object(
        gmail_scanner.Credentials, "from_authorized_user_file", return_value=credentials
    ):
        with pytest.raises(TransportError):
            gmail_scanner.GmailScanner()
    assert token.read_text() == "existing-token"
