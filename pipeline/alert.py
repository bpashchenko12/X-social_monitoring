"""E-mail alerts (SMTP, stdlib). Alerting must never break the pipeline: send_email never raises."""
import os
import re
import smtplib
from email.message import EmailMessage

SECRET_ENV = ("APIFY_TOKEN", "OPENROUTER_API_KEY", "SMTP_PASSWORD")
MAX_BODY = 20000


def configured(env=None):
    env = os.environ if env is None else env
    return bool(env.get("SMTP_HOST") and env.get("ALERT_EMAIL_TO"))


def scrub(text, env=None):
    """Replace any secret value found in the environment by ***."""
    env = os.environ if env is None else env
    text = str(text)
    for k in SECRET_ENV:
        v = env.get(k)
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    return text


def send_email(subject, body, env=None, smtp_cls=smtplib.SMTP, smtp_ssl_cls=smtplib.SMTP_SSL):
    """-> (sent, error). Secrets are scrubbed from subject/body/error; header injection is prevented."""
    env = os.environ if env is None else env
    if not configured(env):
        return False, "alert e-mail not configured (SMTP_HOST / ALERT_EMAIL_TO)"
    try:
        port = int(env.get("SMTP_PORT") or 587)
        sender = env.get("ALERT_EMAIL_FROM") or env.get("SMTP_USER") or "xm-monitor@localhost"
        recipients = [a.strip() for a in re.split(r"[,;]", env["ALERT_EMAIL_TO"]) if a.strip()]
        msg = EmailMessage()
        msg["Subject"] = re.sub(r"[\r\n]+", " ", scrub(subject, env))[:200]
        msg["From"] = re.sub(r"[\r\n]+", "", sender)
        msg["To"] = ", ".join(re.sub(r"[\r\n]+", "", r) for r in recipients)
        msg.set_content(scrub(body, env)[:MAX_BODY])
        cls = smtp_ssl_cls if port == 465 else smtp_cls
        with cls(env["SMTP_HOST"], port, timeout=30) as s:
            if port != 465:
                s.starttls()
            if env.get("SMTP_USER") and env.get("SMTP_PASSWORD"):
                s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
            s.send_message(msg)
        return True, ""
    except Exception as e:  # noqa: BLE001 - alerting must not crash the run
        return False, scrub(f"{type(e).__name__}: {e}", env)
