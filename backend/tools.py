"""The agent's tools. Each tool is a plain Python function plus an OpenAI-style JSON schema.

The same functions are used by:
  * brain.py      - the LLM tool-calling loop behind Agora's custom LLM endpoint
  * mcp_server.py - an MCP server, so any MCP client (Claude Desktop, MCP Inspector, other
                    agents) can use the same enterprise actions.

Critical actions (ordering parts, closing tickets) never execute directly: they create a
pending action and the technician must say yes, which the model passes to confirm_action."""
import json
import uuid
from datetime import datetime, timedelta

from . import config, db, integrations, rag


def _tech() -> dict:
    return db.one("SELECT * FROM technicians WHERE id=?", (config.DEFAULT_TECHNICIAN_ID,)) or {}


def _ticket_or_error(ticket_id: str) -> dict | None:
    return db.one("SELECT * FROM tickets WHERE id=?", (ticket_id.strip().upper(),))


# ---------------------------------------------------------------- read tools (Before / Understand)

def get_my_tickets() -> dict:
    """Open tickets assigned to the technician, highest priority first."""
    rows = db.q(
        "SELECT t.id, t.title, t.priority, t.status, t.sla_due, a.name AS asset, a.site "
        "FROM tickets t JOIN assets a ON a.id=t.asset_id WHERE t.technician_id=? "
        "AND t.status NOT IN ('resolved','closed') "
        "ORDER BY CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.sla_due",
        (config.DEFAULT_TECHNICIAN_ID,))
    return {"technician": _tech().get("name"), "open_tickets": rows}


def get_ticket(ticket_id: str) -> dict:
    """Full ticket with asset details and maintenance history. Makes it the active ticket."""
    t = _ticket_or_error(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    asset = db.one("SELECT * FROM assets WHERE id=?", (t["asset_id"],))
    history = db.q("SELECT date, technician, summary FROM asset_history WHERE asset_id=? "
                   "ORDER BY date DESC", (t["asset_id"],))
    orders = db.q("SELECT id, sku, qty, warehouse, status, eta FROM orders WHERE ticket_id=?",
                  (t["id"],))
    db.remember("active_ticket", t["id"])
    db.log("lookup", f"Opened {t['id']}", t["title"])
    return {"ticket": t, "asset": asset, "maintenance_history": history, "orders": orders}


def search_manual(query: str) -> dict:
    """Search the equipment service manuals (RAG)."""
    hits = rag.search(query, k=3)
    db.log("knowledge", "Searched manuals", query)
    return {"results": hits} if hits else {"results": [], "note": "Nothing relevant found."}


def check_inventory(part: str) -> dict:
    """Stock of a part (SKU or name words) in every warehouse, nearest to the technician first."""
    term = part.strip()
    parts = db.q("SELECT * FROM parts WHERE sku=? COLLATE NOCASE", (term,))
    if not parts:
        words = [w for w in term.lower().replace("-", " ").split() if len(w) > 2]
        parts = [p for p in db.q("SELECT * FROM parts")
                 if words and all(w in (p["name"] + " " + p["sku"]).lower() for w in words)]
        if not parts:
            parts = [p for p in db.q("SELECT * FROM parts")
                     if any(w in (p["name"] + " " + p["sku"]).lower() for w in words)]
    if not parts:
        return {"error": f"No part matching '{part}'.",
                "known_parts": db.q("SELECT sku, name FROM parts")}
    base = _tech().get("base_warehouse", "")
    out = []
    for p in parts[:3]:
        stock = db.q("SELECT warehouse, qty, eta_hours FROM stock WHERE sku=? "
                     "ORDER BY eta_hours", (p["sku"],))
        available = [s for s in stock if s["qty"] > 0]
        out.append({
            "sku": p["sku"], "name": p["name"], "unit_price_inr": p["unit_price"],
            "fits": p["compatible_models"], "stock": stock,
            "nearest_warehouse": base,
            "nearest_has_stock": any(s["warehouse"] == base and s["qty"] > 0 for s in stock),
            "best_option": available[0] if available else None,
        })
    db.log("inventory", f"Checked stock: {parts[0]['sku']}", part)
    return {"parts": out}


# ---------------------------------------------------------------- action tools (Execute)

def _pending(kind: str, payload: dict, summary: str) -> dict:
    # Re-asking for the same action reuses the open request instead of creating a duplicate.
    same = db.one("SELECT id FROM pending_actions WHERE kind=? AND payload=? "
                  "AND status='awaiting_confirmation'", (kind, json.dumps(payload)))
    if same:
        return {"status": "awaiting_confirmation", "action_id": same["id"], "summary": summary,
                "instruction": "Already prepared. If the technician has agreed, call "
                               "confirm_action now with this action_id."}
    # A new request of the same kind replaces older unanswered ones.
    db.run("UPDATE pending_actions SET status='superseded' WHERE kind=? "
           "AND status='awaiting_confirmation'", (kind,))
    action_id = "ACT-" + uuid.uuid4().hex[:5].upper()
    db.run("INSERT INTO pending_actions VALUES (?,?,?,?,?,?)",
           (action_id, kind, json.dumps(payload), summary, "awaiting_confirmation",
            db.iso(db.now())))
    db.log("confirm", "Waiting for confirmation", summary)
    return {"status": "awaiting_confirmation", "action_id": action_id, "summary": summary,
            "instruction": "Read the summary to the technician and ask for a yes or no. "
                           "Then call confirm_action with this action_id."}


def request_part_order(ticket_id: str, sku: str, warehouse: str, qty: int = 1) -> dict:
    """Prepare a spare part order. Needs the technician's confirmation before it is placed."""
    t = _ticket_or_error(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    p = db.one("SELECT * FROM parts WHERE sku=? COLLATE NOCASE", (sku,))
    if not p:
        return {"error": f"Unknown part {sku}."}
    s = db.one("SELECT * FROM stock WHERE sku=? AND warehouse=? COLLATE NOCASE",
               (p["sku"], warehouse))
    if not s or s["qty"] < qty:
        alts = db.q("SELECT warehouse, qty, eta_hours FROM stock WHERE sku=? AND qty>=? "
                    "ORDER BY eta_hours", (p["sku"], qty))
        return {"error": f"{warehouse} does not have {qty} x {p['sku']} in stock.",
                "alternatives": alts}
    eta = db.now() + timedelta(hours=s["eta_hours"])
    total = p["unit_price"] * qty
    summary = (f"Order {qty} {p['name'].split(' ' + p['name'].split()[-2])[0].lower()} "
               f"from {s['warehouse']}, {total:,} rupees, arriving "
               f"{eta.strftime('%A at %-I:%M %p').replace(':00', '')}.")
    return _pending("order_part", {"ticket_id": t["id"], "sku": p["sku"],
                                   "warehouse": s["warehouse"], "qty": qty,
                                   "total": total, "eta": db.iso(eta.replace(second=0))},
                    summary)


def update_ticket(ticket_id: str, status: str = "", note: str = "") -> dict:
    """Add a work note and/or change status. Closing (resolved) needs confirmation."""
    t = _ticket_or_error(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    status = status.strip().lower()
    allowed = {"", "in_progress", "waiting_for_parts", "resolved"}
    if status not in allowed:
        return {"error": f"Status must be one of {sorted(allowed - {''})}."}
    if status == "resolved":
        return _pending("resolve_ticket", {"ticket_id": t["id"], "note": note},
                        f"Mark ticket {t['id']} as resolved" + (f" with note: {note}" if note else "."))
    _apply_ticket_update(t["id"], status, note)
    return {"status": "updated", "ticket_id": t["id"], "new_status": status or t["status"]}


def _apply_ticket_update(ticket_id: str, status: str, note: str) -> None:
    t = _ticket_or_error(ticket_id)
    notes = t["notes"] or ""
    if note:
        notes += f"[{db.now().strftime('%H:%M')}] {note}\n"
    db.run("UPDATE tickets SET status=?, notes=? WHERE id=?", (status or t["status"], notes, t["id"]))
    db.log("ticket", f"Updated {t['id']}", (f"status -> {status}. " if status else "") + note)


def confirm_action(action_id: str, approved: bool) -> dict:
    """Execute (approved=true) or cancel (approved=false) a pending critical action."""
    a = db.one("SELECT * FROM pending_actions WHERE id=?", (action_id.strip().upper(),))
    if not a:
        return {"error": f"No pending action {action_id}."}
    if a["status"] != "awaiting_confirmation":
        return {"error": f"Action {a['id']} is already {a['status']}."}
    if approved and _said_no(db.recall("last_user_text")):
        # Code-level guard: the model may misread a refusal, the system never will.
        approved = False
        db.log("confirm", "Blocked: technician said no", db.recall("last_user_text"))
    if not approved:
        db.run("UPDATE pending_actions SET status='cancelled' WHERE id=?", (a["id"],))
        db.log("confirm", "Cancelled by technician", a["summary"])
        return {"status": "cancelled", "action_id": a["id"]}
    p = json.loads(a["payload"])
    result: dict
    if a["kind"] == "order_part":
        s = db.one("SELECT qty FROM stock WHERE sku=? AND warehouse=?", (p["sku"], p["warehouse"]))
        if not s or s["qty"] < p["qty"]:
            return {"error": "Stock changed, the part is no longer available there."}
        order_id = "PO-" + str(5000 + len(db.q("SELECT id FROM orders")) + 1)
        db.run("UPDATE stock SET qty=qty-? WHERE sku=? AND warehouse=?",
               (p["qty"], p["sku"], p["warehouse"]))
        db.run("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
               (order_id, p["ticket_id"], p["sku"], p["qty"], p["warehouse"], p["total"],
                "placed", p["eta"], db.iso(db.now())))
        _apply_ticket_update(p["ticket_id"], "waiting_for_parts",
                             f"Ordered {p['qty']} x {p['sku']} ({order_id}) from {p['warehouse']}.")
        db.log("order", f"Order {order_id} placed", a["summary"])
        result = {"status": "done", "order_id": order_id, "eta": p["eta"],
                  "ticket_status": "waiting_for_parts"}
    elif a["kind"] == "escalate":
        sent = _send_supervisor(p["message"], p.get("ticket_id", ""), urgent=True)
        result = {"status": "done", "delivery": sent["status"], "to": sent["to"]}
    elif a["kind"] == "resolve_ticket":
        _apply_ticket_update(p["ticket_id"], "resolved", p.get("note", ""))
        result = {"status": "done", "ticket_id": p["ticket_id"], "ticket_status": "resolved"}
    else:
        return {"error": f"Unknown action kind {a['kind']}."}
    db.run("UPDATE pending_actions SET status='done' WHERE id=?", (a["id"],))
    return result


NO_WORDS = {"no", "nope", "nah", "don't", "dont", "do not", "cancel", "stop", "wait", "not",
            "never", "hold", "nahi", "mat"}
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go", "haan", "ha", "confirm",
             "please", "proceed", "correct", "right", "do it", "go ahead", "order", "send"}


def _said_no(text: str) -> bool:
    words = set(text.lower().replace(",", " ").replace(".", " ").replace("!", " ").split())
    phrase = " ".join(text.lower().split())
    neg = bool(words & NO_WORDS) or "do not" in phrase
    pos = bool(words & YES_WORDS) or "do it" in phrase or "go ahead" in phrase
    return neg and not pos


def notify_supervisor(message: str, ticket_id: str = "", urgent: bool = False) -> dict:
    """Send a message to the supervisor's Slack. Urgent escalations need confirmation."""
    if urgent:
        return _pending("escalate", {"message": message, "ticket_id": ticket_id},
                        f"Send an urgent escalation to your supervisor: {message}")
    return _send_supervisor(message, ticket_id, urgent=False)


def _send_supervisor(message: str, ticket_id: str, urgent: bool) -> dict:
    tech = _tech()
    prefix = ":rotating_light: ESCALATION " if urgent else ":wrench: "
    text = f"{prefix}{('[' + ticket_id + '] ') if ticket_id else ''}{message} - {tech.get('name')}"
    delivered = integrations.send_slack(text, tech.get("supervisor_channel", "#field-ops"))
    db.log("notify", "Escalated to supervisor" if urgent else "Supervisor notified", message)
    return {"status": "sent" if delivered else "recorded (Slack not configured)",
            "to": tech.get("supervisor_name")}


def schedule_followup(ticket_id: str, when: str, purpose: str) -> dict:
    """Schedule a revisit or follow-up. `when` is an ISO date-time like 2026-11-21T10:30."""
    t = _ticket_or_error(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    try:
        dt = datetime.fromisoformat(when.replace("Z", ""))
    except ValueError:
        return {"error": "Give `when` as an ISO date-time, e.g. 2026-11-21T10:30."}
    if dt < db.now() - timedelta(minutes=5):
        return {"error": "That time is in the past."}
    db.run("INSERT INTO followups (ticket_id,when_at,purpose,created_at) VALUES (?,?,?,?)",
           (t["id"], db.iso(dt), purpose, db.iso(db.now())))
    db.log("schedule", f"Follow-up booked {dt.strftime('%a %d %b %I:%M %p')}", purpose)
    return {"status": "scheduled", "ticket_id": t["id"], "when": db.iso(dt)}


def generate_service_report(ticket_id: str, findings: str, actions_taken: str,
                            next_steps: str = "") -> dict:
    """Write the service report for a visit, store it on the ticket and share it."""
    t = _ticket_or_error(ticket_id)
    if not t:
        return {"error": f"Ticket {ticket_id} not found."}
    asset = db.one("SELECT * FROM assets WHERE id=?", (t["asset_id"],))
    orders = db.q("SELECT id, sku, qty, warehouse, eta FROM orders WHERE ticket_id=?", (t["id"],))
    fups = db.q("SELECT when_at, purpose FROM followups WHERE ticket_id=?", (t["id"],))
    tech = _tech()
    lines = [
        f"SERVICE REPORT {t['id']}  ({db.now().strftime('%d %b %Y %H:%M')})",
        f"Technician: {tech.get('name')}   Asset: {asset['name']} {asset['model']}",
        f"Site: {asset['site']}",
        f"Issue: {t['title']}",
        f"Findings: {findings}",
        f"Actions taken: {actions_taken}",
    ]
    if orders:
        lines.append("Parts ordered: " + "; ".join(
            f"{o['qty']} x {o['sku']} ({o['id']}, {o['warehouse']}, ETA {o['eta']})" for o in orders))
    if fups:
        lines.append("Follow-up: " + "; ".join(f"{f['when_at']} - {f['purpose']}" for f in fups))
    if next_steps:
        lines.append(f"Next steps: {next_steps}")
    lines.append(f"Status: {_ticket_or_error(t['id'])['status']}")
    body = "\n".join(lines)
    rid = "SR-" + t["id"].split("-")[-1] + "-" + uuid.uuid4().hex[:3].upper()
    db.run("INSERT INTO reports VALUES (?,?,?,?)", (rid, t["id"], body, db.iso(db.now())))
    integrations.send_slack(f":page_facing_up: Service report {rid}\n```{body}```",
                            tech.get("supervisor_channel", "#field-ops"))
    db.log("report", f"Report {rid} generated", t["title"])
    return {"status": "created", "report_id": rid, "shared_with": tech.get("supervisor_name")}


# ---------------------------------------------------------------- registry

def _s(desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    # Gemini rejects an object schema with empty "properties", so tools without arguments
    # get no "parameters" at all (valid for every OpenAI-compatible provider).
    if not props:
        return {"description": desc}
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {"description": desc, "parameters": schema}


STR = {"type": "string"}
TOOLS = {
    "get_my_tickets": (get_my_tickets, _s("List the technician's open tickets.")),
    "get_ticket": (get_ticket, _s(
        "Get a ticket with asset details and maintenance history.",
        {"ticket_id": STR}, ["ticket_id"])),
    "search_manual": (search_manual, _s(
        "Search equipment service manuals for error codes, procedures, specs, safety.",
        {"query": STR}, ["query"])),
    "check_inventory": (check_inventory, _s(
        "Check spare part stock across warehouses. Accepts a SKU or part name.",
        {"part": STR}, ["part"])),
    "request_part_order": (request_part_order, _s(
        "Prepare a spare part order (needs technician confirmation).",
        {"ticket_id": STR, "sku": STR, "warehouse": STR, "qty": {"type": "integer"}},
        ["ticket_id", "sku", "warehouse"])),
    "update_ticket": (update_ticket, _s(
        "Add a work note and/or set status: in_progress, waiting_for_parts, resolved "
        "(resolved needs confirmation).",
        {"ticket_id": STR, "status": STR, "note": STR}, ["ticket_id"])),
    "confirm_action": (confirm_action, _s(
        "Execute or cancel a pending action after the technician said yes or no.",
        {"action_id": STR, "approved": {"type": "boolean"}}, ["action_id", "approved"])),
    "notify_supervisor": (notify_supervisor, _s(
        "Message the supervisor on Slack. urgent=true escalates the incident.",
        {"message": STR, "ticket_id": STR, "urgent": {"type": "boolean"}}, ["message"])),
    "schedule_followup": (schedule_followup, _s(
        "Book a revisit/follow-up for a ticket at an ISO date-time.",
        {"ticket_id": STR, "when": STR, "purpose": STR}, ["ticket_id", "when", "purpose"])),
    "generate_service_report": (generate_service_report, _s(
        "Create and share the service report for the visit.",
        {"ticket_id": STR, "findings": STR, "actions_taken": STR, "next_steps": STR},
        ["ticket_id", "findings", "actions_taken"])),
}


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n, **schema}}
            for n, (_, schema) in TOOLS.items()]


def call(name: str, arguments: str | dict) -> dict:
    if name not in TOOLS:
        return {"error": f"Unknown tool {name}."}
    try:
        args = json.loads(arguments or "{}") if isinstance(arguments, str) else arguments
        return TOOLS[name][0](**args)
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # never let a tool crash the voice turn
        return {"error": f"{name} failed: {e}"}
