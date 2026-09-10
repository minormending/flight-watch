from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

import requests

from .config import NotifyConfig

logger = logging.getLogger(__name__)


def send_ntfy(cfg: NotifyConfig, title: str, body: str) -> None:
    """Push to an ntfy topic. No account needed -- the topic name is the secret."""
    response = requests.post(
        f"{cfg.ntfy_server}/{cfg.ntfy_topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": "high",
            "Tags": "airplane,money_with_wings",
        },
        timeout=15,
    )
    response.raise_for_status()


def send_email(cfg: NotifyConfig, title: str, body: str) -> None:
    """Send via Gmail SMTP using an app password."""
    msg = EmailMessage()
    msg["From"] = cfg.gmail_username
    msg["To"] = cfg.alert_to_email
    msg["Subject"] = title
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
        smtp.login(cfg.gmail_username, cfg.gmail_app_password)
        smtp.send_message(msg)


def notify(cfg: NotifyConfig, title: str, body: str) -> list[str]:
    """Fan out to every configured channel. Returns the ones that succeeded.

    A channel failing is logged but never raises: a broken notifier must not
    cost us the scan data we just collected.
    """
    delivered: list[str] = []

    if cfg.ntfy_enabled:
        try:
            send_ntfy(cfg, title, body)
            delivered.append("ntfy")
            logger.info("Alert pushed to ntfy topic %s", cfg.ntfy_topic)
        except Exception as exc:  # noqa: BLE001
            logger.error("ntfy delivery failed: %s", exc)

    if cfg.email_enabled:
        try:
            send_email(cfg, title, body)
            delivered.append("email")
            logger.info("Alert emailed to %s", cfg.alert_to_email)
        except smtplib.SMTPAuthenticationError as exc:
            logger.error(
                "Gmail auth failed. Check GMAIL_USERNAME / GMAIL_APP_PASSWORD "
                "(app password, 2FA required). Raw: %s",
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Email delivery failed: %s", exc)

    if not (cfg.ntfy_enabled or cfg.email_enabled):
        logger.warning(
            "Alert fired but no channel is configured. Set FW_NTFY_TOPIC in .env."
        )

    return delivered
