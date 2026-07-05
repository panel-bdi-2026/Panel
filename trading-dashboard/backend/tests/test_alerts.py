"""Tests del módulo de alertas por email."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.alerts import send_alert


class _Settings:
    smtp_host = "smtp.gmail.com"
    smtp_port = 587
    smtp_user = "user@gmail.com"
    smtp_password = "secret"
    alert_email_to = "dest@gmail.com"


class _SettingsEmpty:
    smtp_host = ""
    smtp_port = 587
    smtp_user = ""
    smtp_password = ""
    alert_email_to = ""


def test_send_alert_no_config_returns_false():
    assert send_alert(_SettingsEmpty(), "Asunto", "Cuerpo") is False


def test_send_alert_smtp_success():
    mock_server = MagicMock()
    mock_server.__enter__ = lambda s: s
    mock_server.__exit__ = MagicMock(return_value=False)

    with patch("smtplib.SMTP", return_value=mock_server) as mock_smtp:
        result = send_alert(_Settings(), "Asunto de prueba", "Cuerpo del email")

    assert result is True
    mock_smtp.assert_called_once_with("smtp.gmail.com", 587, timeout=15)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@gmail.com", "secret")
    mock_server.send_message.assert_called_once()


def test_send_alert_smtp_error_returns_false():
    with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
        result = send_alert(_Settings(), "Asunto", "Cuerpo")
    assert result is False


def test_send_alert_subject_prefix():
    sent_msg = {}

    def capture_send(msg):
        sent_msg["subject"] = msg["Subject"]

    mock_server = MagicMock()
    mock_server.__enter__ = lambda s: s
    mock_server.__exit__ = MagicMock(return_value=False)
    mock_server.send_message.side_effect = capture_send

    with patch("smtplib.SMTP", return_value=mock_server):
        send_alert(_Settings(), "IBKR desconectado", "texto")

    assert sent_msg["subject"] == "[Trading Dashboard] IBKR desconectado"
