"""Alertas por email para eventos críticos del sistema.

Soporta dos backends (en orden de preferencia):
  1. Resend (API HTTP, puerto 443) — recomendado: DigitalOcean bloquea SMTP.
     Requiere RESEND_API_KEY y ALERT_EMAIL_FROM en el .env.
  2. SMTP (fallback) — útil en entornos sin restricción de puertos.
     Requiere SMTP_HOST, SMTP_USER, SMTP_PASSWORD.

En ambos casos, ALERT_EMAIL_TO debe estar definido.
"""
from __future__ import annotations

import json
import logging
import smtplib
import socket
import urllib.error
import urllib.request
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


def _send_via_resend(settings, subject: str, body: str) -> bool:
    """Envía via Resend API (HTTPS, sin restricciones de puerto)."""
    if not all([settings.resend_api_key, settings.alert_email_from, settings.alert_email_to]):
        return False
    try:
        payload = json.dumps({
            "from": settings.alert_email_from,
            "to": [settings.alert_email_to],
            "subject": f"[Trading Dashboard] {subject}",
            "text": f"{body}\n\nServidor: {_get_hostname()}",
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status in (200, 201):
                logger.info("Alerta enviada via Resend: %s", subject)
                return True
            logger.error("Resend retornó status %s", resp.status)
            return False
    except urllib.error.HTTPError as exc:
        logger.error("Error Resend HTTP %s: %s", exc.code, exc.read().decode(errors="replace"))
        return False
    except Exception as exc:
        logger.error("Error enviando alerta via Resend: %s", exc)
        return False


def _send_via_smtp(settings, subject: str, body: str) -> bool:
    """Envía via SMTP TLS (puerto 587). Bloqueado en DigitalOcean por defecto."""
    if not all([settings.smtp_host, settings.smtp_user, settings.smtp_password, settings.alert_email_to]):
        return False
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_user
        msg["To"] = settings.alert_email_to
        msg["Subject"] = f"[Trading Dashboard] {subject}"
        msg.attach(MIMEText(f"{body}\n\nServidor: {_get_hostname()}", "plain", "utf-8"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)
        logger.info("Alerta enviada via SMTP: %s", subject)
        return True
    except Exception as exc:
        logger.error("Error enviando alerta via SMTP: %s", exc)
        return False


def send_alert(settings, subject: str, body: str) -> bool:
    """Envía un email de alerta usando Resend (preferido) o SMTP como fallback.

    Retorna True si el envío fue exitoso. No lanza excepciones — registra
    el error y retorna False para que el caller pueda continuar.
    Si ningún backend está configurado, retorna False en silencio.
    """
    if settings.resend_api_key:
        return _send_via_resend(settings, subject, body)
    if settings.smtp_host:
        return _send_via_smtp(settings, subject, body)
    logger.debug("Sin backend de email configurado, alerta ignorada: %s", subject)
    return False
