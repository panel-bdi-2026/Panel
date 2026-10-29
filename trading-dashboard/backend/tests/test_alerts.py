"""Tests del módulo de alertas por email."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.alerts import send_alert


class _SettingsResend:
    resend_api_key = "re_test_key"
    alert_email_from = "Trading <onboarding@resend.dev>"
    alert_email_to = "dest@gmail.com"
    smtp_host = ""
    smtp_port = 587
    smtp_user = ""
    smtp_password = ""


class _SettingsSmtp:
    resend_api_key = ""
    alert_email_from = ""
    alert_email_to = "dest@gmail.com"
    smtp_host = "smtp.gmail.com"
    smtp_port = 587
    smtp_user = "user@gmail.com"
    smtp_password = "secret"


class _SettingsEmpty:
    resend_api_key = ""
    alert_email_from = ""
    alert_email_to = ""
    smtp_host = ""
    smtp_port = 587
    smtp_user = ""
    smtp_password = ""


def test_send_alert_no_config_returns_false():
    assert send_alert(_SettingsEmpty(), "Asunto", "Cuerpo") is False


def test_send_alert_resend_success():
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("httpx.post", return_value=mock_response):
        result = send_alert(_SettingsResend(), "Test alerta", "Cuerpo del email")

    assert result is True


def test_send_alert_resend_http_error_returns_false():
    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = "Unprocessable Entity"

    with patch("httpx.post", return_value=mock_response):
        result = send_alert(_SettingsResend(), "Test", "Cuerpo")
    assert result is False


def test_send_alert_resend_network_error_returns_false():
    with patch("httpx.post", side_effect=OSError("network unreachable")):
        result = send_alert(_SettingsResend(), "Test", "Cuerpo")
    assert result is False


def test_send_alert_smtp_success():
    mock_server = MagicMock()
    mock_server.__enter__ = lambda s: s
    mock_server.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_server):
        result = send_alert(_SettingsSmtp(), "Test SMTP", "Cuerpo")

    assert result is True
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@gmail.com", "secret")


def test_send_alert_smtp_error_returns_false():
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        result = send_alert(_SettingsSmtp(), "Test", "Cuerpo")
    assert result is False


def test_resend_takes_priority_over_smtp():
    """Si ambos están configurados, Resend tiene prioridad."""
    class _BothSettings(_SettingsResend):
        smtp_host = "smtp.gmail.com"
        smtp_user = "user@gmail.com"
        smtp_password = "secret"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("httpx.post", return_value=mock_response) as mock_resend, \
         patch("smtplib.SMTP") as mock_smtp:
        send_alert(_BothSettings(), "Test", "Cuerpo")

    mock_resend.assert_called_once()
    mock_smtp.assert_not_called()
