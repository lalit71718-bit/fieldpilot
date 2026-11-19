"""Outbound integrations. Each one degrades gracefully: if no credentials are set, the action
is still recorded in the dashboard so the demo never breaks."""
import httpx

from . import config, db


def send_slack(message: str, recipient: str = "#field-ops") -> bool:
    delivered = False
    if config.SLACK_WEBHOOK_URL:
        try:
            r = httpx.post(config.SLACK_WEBHOOK_URL, json={"text": message}, timeout=8)
            delivered = r.status_code < 300
        except httpx.HTTPError:
            delivered = False
    db.run("INSERT INTO notifications (channel,recipient,message,delivered,created_at) "
           "VALUES (?,?,?,?,?)", ("slack", recipient, message, int(delivered), db.iso(db.now())))
    return delivered
