"""
Email delivery via Resend API.
Sends deal alerts and heartbeat digests to the configured recipient.
"""
import os
import requests

RESEND_API_KEY = os.environ["RESEND_API_KEY"]
ALERT_RECIPIENT = os.environ.get("ALERT_RECIPIENT", "tahirinavid@gmail.com")
# Defaults to Resend's shared sandbox sender — works immediately without domain verification.
# Set SENDER_EMAIL to your own verified domain address for production.
SENDER = os.environ.get("SENDER_EMAIL", "onboarding@resend.dev")

RESEND_URL = "https://api.resend.com/emails"


def _send(subject: str, html_body: str) -> None:
    resp = requests.post(
        RESEND_URL,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": SENDER,
            "to": [ALERT_RECIPIENT],
            "subject": subject,
            "html": html_body,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"[email] Sent: {subject}")


def send_deal_alert(subject: str, rows: list[dict]) -> None:
    """
    Send a deal alert email.

    Each row in `rows` is a dict with keys:
      - title (str)
      - true_cost (str)          e.g. "$610/mo true $0-down" or "$1.89/W"
      - advertised (str)         e.g. "$549/mo advertised"
      - url (str)
      - fine_print (str)         one-sentence summary
    """
    cards = ""
    for r in rows:
        cards += f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:16px;font-family:sans-serif;">
          <h2 style="margin:0 0 4px;font-size:18px;color:#1a202c;">{r['title']}</h2>
          <p style="margin:0;font-size:22px;font-weight:bold;color:#2f855a;">✅ {r['true_cost']}</p>
          <p style="margin:4px 0;font-size:13px;color:#718096;">Advertised: {r['advertised']}</p>
          <p style="margin:8px 0;font-size:13px;color:#4a5568;">{r['fine_print']}</p>
          <a href="{r['url']}" style="display:inline-block;margin-top:8px;padding:8px 14px;background:#3182ce;color:#fff;border-radius:4px;text-decoration:none;font-size:13px;">View Listing →</a>
        </div>
        """

    html = f"""
    <html><body style="background:#f7fafc;padding:24px;">
      <h1 style="font-family:sans-serif;color:#1a202c;margin-bottom:4px;">🔥 Deal Alert</h1>
      <p style="font-family:sans-serif;color:#718096;margin-top:0;">The following deal(s) match your criteria:</p>
      {cards}
      <p style="font-family:sans-serif;font-size:11px;color:#a0aec0;margin-top:32px;">
        Sent by Steal Deals Agent · <a href="https://github.com" style="color:#a0aec0;">GitHub Actions</a>
      </p>
    </body></html>
    """
    _send(subject, html)


def send_heartbeat(module: str, status: str, details: str = "") -> None:
    """Send a heartbeat/status email confirming the agent ran."""
    html = f"""
    <html><body style="font-family:sans-serif;background:#f7fafc;padding:24px;">
      <h2 style="color:#2d3748;">🤖 Heartbeat: {module}</h2>
      <p style="color:#4a5568;">Status: <strong>{status}</strong></p>
      {f'<p style="color:#718096;font-size:13px;">{details}</p>' if details else ''}
      <p style="font-size:11px;color:#a0aec0;">No deals found this run — agent is healthy.</p>
    </body></html>
    """
    _send(f"[Heartbeat] {module} — {status}", html)
