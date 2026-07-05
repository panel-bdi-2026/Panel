"""Alertas por email para eventos críticos del sistema."""
from __future__ import annotations

import logging
import smtplib
import socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_hostname: str | None = None


def _get_hostname() -> str:
    global _hostname
    if _hostname is None:
        try:
            _hostname = socket.gethostname()
        except Exception:
            _hostname = "desconocido"
    return _hostname


def send_alert(settings, subject: str, body: str) -> bool:
    """Envía un email de alerta vía SMTP TLS.

    Retorna True si el envío fue exitoso. No lanza excepciones — registra
    el error en el log y retorna False para que el caller pueda continuar.
    Si SMTP no está configurado (faltan vars en .env), retorna False en
    silencio (caso esperado en desarrollo/testing).
    """
    if not all([
        settings.smtp_host,
        settings.smtp_user,
        settings.smtp_password,
        settings.alert_email_to,
    ]):
        logger.debug("SMTP no configurado, alerta ignorada: %s", subject)
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_user
        msg["To"] = settings.alert_email_to
        msg["Subject"] = f"[Trading Dashboard] {subject}"
        full_body = f"{body}\n\nServidor: {_get_hostname()}"
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)

        logger.info("Alerta enviada: %s", subject)
        return True
    except Exception as exc:
        logger.error("Error enviando alerta por email: %s", exc)
        return False
