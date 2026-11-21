"""Demo mode: the whole HomePilot story, step by step, without the language model.

Each step is what Anika would say plus the tool calls the agent makes for it. The tools are
the real ones (same database, same Slack posts, same MCP store), so the dashboard fills in
exactly as it does in a live voice call. Only the wording of the replies is scripted, which
keeps the demo working when the LLM provider is rate-limited or the Wi-Fi is bad.
"""
from datetime import timedelta

from . import db, tools


def _today() -> tuple[str, list]:
    t = tools.call("get_today", {})
    ev = t.get("events_next_30h", [])[:2]
    plan = " and ".join(f"{e['title']} {e['when']}" for e in ev) or "a quiet day"
    return (f"Good morning, Anika. Coming up: {plan}. {len(t.get('open_tasks', []))} chores are "
            f"open, and Dadi's BP tablet is at 8 PM."), []


def _ac_issue() -> tuple[str, list]:
    a = tools.call("get_appliance", {"appliance": "living room AC"})
    tools.call("search_manual", {"query": "E4 outdoor fan not spinning warm air"})
    ins = a.get("insight") or {}
    rep = (f"That's the third E4 in {ins.get('first_seen_days_ago', 'a few')} days, so it's a "
           f"repeat fault, likely {ins.get('likely_root_cause', 'the outdoor fan motor')}. "
           if ins else "")
    return rep + "Safe check from the manual: switch it off at the breaker for 5 minutes, then restart.", []


def _find_slot() -> tuple[str, list]:
    s = tools.call("find_service_slots", {"appliance": "living room AC"})
    best = next((o for o in s.get("options", []) if o["ok"]), None)
    if not best:
        return "No technician slot fits the family calendar today.", []
    p = tools.call("book_service", {"provider": best["provider"],
                                    "slot_at": best["slot_at"], "appliance": "AC-LIVING"})
    return f"{s.get('recommendation', '')} Shall I book it?", [p.get("action_id")]


def _confirm() -> tuple[str, list]:
    pend = db.one("SELECT id FROM pending_actions WHERE status='awaiting_confirmation' ORDER BY created_at DESC")
    if not pend:
        return "Nothing is waiting for approval.", []
    r = tools.call("confirm_action", {"action_id": pend["id"], "approved": True})
    if r.get("booking_id"):
        return (f"Booked, {r['booking_id']}, {r.get('when', '')}. It's on the family calendar "
                f"with a reminder 30 minutes before."), []
    if r.get("order_id"):
        return (f"Order {r['order_id']} placed. Approve the payment on your phone: scan the QR "
                f"code on the screen."), []
    return "Done.", []


def _shopping() -> tuple[str, list]:
    tools.call("add_to_shopping_list", {"items": ["milk", "paneer", "Dadi's BP tablets"]})
    tools.call("assign_task", {"title": "Pick up groceries on the way home", "assignee": "Rohan"})
    total = tools.call("shopping_status", {}).get("estimated_total_inr")
    est = f" About {total} rupees." if total else ""
    return f"Added milk, paneer and Dadi's BP tablets, and I've asked Rohan to pick them up.{est}", []


def _reminder() -> tuple[str, list]:
    at = db.now().replace(hour=20, minute=0, second=0)
    if at <= db.now():
        at += timedelta(days=1)
    r = tools.call("set_reminder", {"text": "Take the BP tablet", "at": db.iso(at), "member": "Dadi"})
    when = r.get("when", "at 8 PM")
    return (f"Dadi's reminder is set {when}. If she doesn't press Got it within 5 minutes, the "
            f"family gets an alert."), []


def _order() -> tuple[str, list]:
    tools.call("store_add_to_cart", {"items": [{"item_id": "F101", "qty": 1}]})
    c = tools.call("store_checkout", {})
    if c.get("error"):
        return f"The store didn't respond: {c['error']}", []
    return c.get("summary", "Order ready.") + " Shall I place it?", []


def _spending() -> tuple[str, list]:
    s = tools.call("spending_summary", {"period": "week"})
    parts = ", ".join(f"{k} {v} rupees" for k, v in s.get("by_category", {}).items())
    waiting = tools.call("store_order_status", {})
    tail = (" The biryani is added once you approve its payment."
            if waiting.get("payment") == "awaiting_approval" else "")
    return (f"This week you've spent {s.get('total_inr', 0)} rupees"
            f"{': ' + parts if parts else ''}. {s.get('note', '')}{tail}"), []


def _phone() -> tuple[str, list]:
    tools.call("calendar_on_phone", {})
    return ("There's a QR code on the screen. Scan it and tap Add, and the alarms ring on your "
            "phone too, even if this screen is off."), []


def _wrap() -> tuple[str, list]:
    tools.call("daily_summary", {})
    return "I've sent the family today's summary. Have a good day, Anika.", []


STEPS = [
    ("What's on today?", _today),
    ("The living room AC is showing E four again and blowing warm air.", _ac_issue),
    ("The outdoor fan isn't spinning. Can you get someone to fix it?", _find_slot),
    ("Yes, book it.", _confirm),
    ("Add milk, paneer and Dadi's BP tablets to the list, and ask Rohan to pick them up.", _shopping),
    ("Remind Dadi to take her BP tablet at 8 PM.", _reminder),
    ("Order a veg biryani for lunch.", _order),
    ("Yes, place it.", _confirm),
    ("How much did we spend this week?", _spending),
    ("Put the alarms on my phone.", _phone),
    ("That's all. Send the family an update.", _wrap),
]


def run_step(n: int) -> dict:
    if not 0 <= n < len(STEPS):
        return {"done": True, "total": len(STEPS)}
    said, fn = STEPS[n]
    db.remember("last_user_text", said)   # the tools' "did the user say a time / yes" guards
    db.log("user", "Demo", said)
    try:
        reply, _ = fn()
    except Exception as e:  # keep the demo moving
        db.log("error", "Demo step", str(e)[:200])
        reply = "Sorry, that step hit a problem. Moving on."
    reply = " ".join(reply.split())
    db.log("agent", "HomePilot", reply)
    db.remember("last_assistant_text", reply)
    return {"step": n, "total": len(STEPS), "user": said, "reply": reply,
            "done": n == len(STEPS) - 1}
