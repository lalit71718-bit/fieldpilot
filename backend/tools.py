"""HomePilot's tools: plain Python functions plus OpenAI-style JSON schemas.

The same functions are used by:
  * brain.py      - the LLM tool-calling loop behind Agora's custom LLM endpoint
  * mcp_server.py - an MCP server, so any MCP client can use the same household actions.

Before / During / Action / After (the PS-1 workflow):
  get_today, get_appliance, search_manual         -> schedules, tasks, preferences, knowledge
  find_service_slots, book_service, add_event,     -> calendar, service requests,
  add_to_shopping_list, assign_task                    shopping list, tasks
  set_reminder, complete_task, send_family_update  -> reminders, completion, follow-ups
  call_family_member                               -> a family member joins the live Agora call

Critical actions (paid service bookings, urgent alerts) never execute directly: they create a
pending action and the user must say yes, which the model passes to confirm_action."""
import json
import re
import uuid
from datetime import datetime, timedelta

from . import config, db, integrations, rag


# ---------------------------------------------------------------- helpers

def _spoken_time(dt: datetime) -> str:
    """'Friday at 3:25 PM' (portable: '%-I' crashes on Windows)."""
    hour = dt.strftime("%I").lstrip("0") or "12"
    day = "today" if dt.date() == db.now().date() else (
        "tomorrow" if dt.date() == db.now().date() + timedelta(days=1) else dt.strftime("%A"))
    return f"{day} at {hour}:{dt.strftime('%M %p')}".replace(":00 ", " ")


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _parse_when(when: str) -> datetime | str:
    """Local ISO time from the model; converts UTC/offsets. Returns an error string if bad."""
    try:
        dt = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "Give the time as a local ISO date-time, e.g. 2026-11-21T18:30."
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def _member(name: str) -> dict | None:
    key = (name or "").strip().lower()
    for m in db.q("SELECT * FROM members"):
        if key in (m["id"], m["name"].lower()) or (key and key in m["role"].lower()):
            return m
    if key in ("me", "myself", "i", "mom", "mum", "mother"):
        return db.one("SELECT * FROM members WHERE id='anika'")
    if key in ("dad", "father", "husband"):
        return db.one("SELECT * FROM members WHERE id='rohan'")
    if key in ("grandma", "granny", "grandmother", "nani", "amma"):
        return db.one("SELECT * FROM members WHERE id='dadi'")
    return None


def _appliance(name: str) -> dict | None:
    key = (name or "").strip().lower()
    for a in db.q("SELECT * FROM appliances"):
        hay = f"{a['id']} {a['name']} {a['model']} {a['room']}".lower()
        if key and (key == a["id"].lower() or all(w in hay for w in key.split()
                                                  if w not in ("the", "my", "our"))):
            return a
    if any(w in key for w in ("ac", "air", "cooler")):
        return db.one("SELECT * FROM appliances WHERE id='AC-LIVING'")
    if "wash" in key:
        return db.one("SELECT * FROM appliances WHERE id='WM-01'")
    return None


def _clashes(start: datetime, minutes: int) -> list[dict]:
    end = start + timedelta(minutes=minutes)
    out = []
    for e in db.q("SELECT * FROM events"):
        s = _dt(e["start_at"])
        if s < end and start < s + timedelta(minutes=e["minutes"]):
            out.append(e)
    return out


def _household() -> dict:
    return {r["key"]: r["value"] for r in db.q("SELECT * FROM household")}


# ---------------------------------------------------------------- de-duplication helpers

_SYNONYMS = [("blood pressure", "bp"), ("tablets", "tablet"), ("medicines", "medicine"),
             ("tomatoes", "tomato"), ("potatoes", "potato")]
_FILLER = {"the", "a", "an", "some", "of", "for", "dadi's", "dadis", "dadi", "her", "his",
           "my", "our", "from", "on", "way", "home", "to", "up", "pick", "and", "freshmart"}


def _canon(text: str) -> set[str]:
    t = re.sub(r"'s\b", "", text.lower())                 # "dadi's" -> "dadi"
    t = " " + re.sub(r"[^a-z0-9 ]", " ", t) + " "
    for a, b in _SYNONYMS:
        t = t.replace(f" {a} ", f" {b} ")
    words = [w for w in t.split() if w not in _FILLER]
    return {w.rstrip("s") if len(w) > 3 else w for w in words}


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    import difflib
    ca, cb = _canon(a), _canon(b)
    if ca and cb and ca == cb:
        return True
    if ca and cb and len(ca & cb) / len(ca | cb) >= threshold:
        return True
    return difflib.SequenceMatcher(None, " ".join(sorted(ca)), " ".join(sorted(cb))).ratio() >= 0.85


# ---------------------------------------------------------------- Before: context

def get_today() -> dict:
    """Today's family schedule, pending tasks, reminders, shopping list and preferences."""
    now = db.now()
    horizon = now + timedelta(hours=30)
    events = [e for e in db.q("SELECT title, member, start_at, minutes, location FROM events "
                              "ORDER BY start_at") if now - timedelta(hours=1) <= _dt(e["start_at"]) <= horizon]
    for e in events:
        e["when"] = _spoken_time(_dt(e["start_at"]))
    tasks = db.q("SELECT id, title, assignee, due_at FROM tasks WHERE status='open' ORDER BY due_at")
    reminders = db.q("SELECT text, member, remind_at FROM reminders WHERE status='scheduled' "
                     "ORDER BY remind_at")
    for r in reminders:
        r["when"] = _spoken_time(_dt(r["remind_at"]))
        r["rings_in"] = _until(_dt(r["remind_at"]))
    shopping = [s["item"] for s in db.q("SELECT item FROM shopping WHERE status='needed'")]
    members = db.q("SELECT name, role, notes FROM members")
    db.log("lookup", "Read today's family plan", f"{len(events)} events, {len(tasks)} tasks")
    return {"now": _spoken_time(now), "events_next_30h": events, "open_tasks": tasks,
            "reminders": reminders, "shopping_list": shopping, "family": members,
            "preferences": _household()}


# ---------------------------------------------------------------- During: home issues

def get_appliance(appliance: str) -> dict:
    """Appliance details and service history. Makes it the active home issue."""
    a = _appliance(appliance)
    if not a:
        return {"error": f"No appliance matching '{appliance}'.",
                "known": [x["name"] for x in db.q("SELECT name FROM appliances")]}
    history = db.q("SELECT date, by_whom, summary FROM appliance_history WHERE appliance_id=? "
                   "ORDER BY date DESC", (a["id"],))
    bookings = db.q("SELECT id, provider, slot_at, status FROM bookings WHERE appliance_id=?",
                    (a["id"],))
    db.remember("active_appliance", a["id"])
    if _codes_in(db.recall("last_user_text")):
        db.remember("issue_text", db.recall("last_user_text"))
    db.log("lookup", f"Opened {a['name']}", a["model"])
    out = {"appliance": a, "service_history": history, "bookings": bookings,
           "in_warranty": a["warranty_until"] >= db.now().date().isoformat()}
    insight = fault_insight(a["id"])
    if insight:
        out["insight"] = insight
        db.log("insight", f"Repeat fault: {insight['code']} x{insight['previous'] + 1}",
               insight["summary"])
    return out


NUM_WORDS = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5", "SIX": "6",
             "SEVEN": "7", "EIGHT": "8", "NINE": "9"}


def _codes_in(text: str) -> list[str]:
    """Error codes in speech-to-text output: 'E4', 'e four', 'E-4', 'E 4'."""
    t = text.upper().replace("-", " ")
    for w, d in NUM_WORDS.items():
        t = re.sub(rf"\b([A-Z])\s+{w}\b", rf"\g<1>{d}", t)
    return [c.replace(" ", "") for c in re.findall(r"\b([A-Z]{1,2}\s?\d)\b", t)]


def fault_insight(appliance_id: str, extra_text: str = "") -> dict | None:
    """Spot a repeat fault: the reported error code already appears in the service history."""
    a = db.one("SELECT * FROM appliances WHERE id=?", (appliance_id,)) if appliance_id else None
    if not a:
        return None
    history = db.q("SELECT date, summary FROM appliance_history WHERE appliance_id=? ORDER BY date",
                   (a["id"],))
    seen = " ".join(h["summary"] for h in history)
    said = f"{extra_text} {db.recall('issue_text')} {db.recall('last_user_text')}"
    code = next((c for c in _codes_in(said) if re.search(rf"\b{c}\b", seen)), None)
    if not code:  # nothing matching was said: use the most recent code in the history
        found = re.findall(r"\b(E\d)\b", seen)
        code = found[-1] if found else None
    if not code:
        return None
    past = [h for h in history if re.search(rf"\b{code}\b", h["summary"])]
    cause = ("the outdoor fan capacitor, flagged by the technician but never replaced"
             if any("capacitor" in h["summary"].lower() for h in past) else "unclear from history")
    days = (db.now().date() - datetime.fromisoformat(past[0]["date"]).date()).days
    return {"code": code, "previous": len(past), "first_seen_days_ago": days,
            "likely_root_cause": cause,
            "summary": (f"{code} has happened {len(past)} times before on the {a['name']} "
                        f"in the last {days} days. Likely root cause: {cause}."),
            "recommendation": "Fix the cause instead of resetting the breaker again: book a "
                              "technician to replace the part."}


def search_manual(query: str) -> dict:
    """Search the appliance owner's manuals (RAG)."""
    hits = [{"section": h["section"], "text": h["text"]} for h in rag.search(query, k=2)]
    db.log("knowledge", "Searched manuals", query)
    return {"results": hits} if hits else {"results": [], "note": "Nothing relevant found."}


# ---------------------------------------------------------------- Action: service requests

def find_service_slots(appliance: str = "") -> dict:
    """Technician slots for an appliance, checked against the family calendar and preferences."""
    a = _appliance(appliance) if appliance else db.one(
        "SELECT * FROM appliances WHERE id=?", (db.recall("active_appliance") or "AC-LIVING",))
    prefs = _household()
    now = db.now()
    options = []
    for s in db.q("SELECT * FROM slots WHERE available=1 ORDER BY slot_at"):
        start = _dt(s["slot_at"])
        if start < now:
            continue
        clash = _clashes(start, 60)
        problems = [f"clashes with {c['title']}" for c in clash]
        if start.hour >= 20:
            problems.append("after the family's 8 PM quiet hours")
        options.append({"provider": s["provider"], "rating": s["rating"], "fee_inr": s["fee"],
                        "slot_at": s["slot_at"], "when": _spoken_time(start),
                        "ok": not problems, "problems": problems})
    good = [o for o in options if o["ok"]]
    best = max(good, key=lambda o: (o["rating"], -_dt(o["slot_at"]).timestamp())) if good else None
    skipped = [f"{o['provider']} {o['when']} {' and '.join(o['problems'])}"
               for o in options if not o["ok"] and "clashes" in " ".join(o["problems"])]
    advice = (f"{skipped[0][0].upper() + skipped[0][1:]}, so skip it. " if skipped else "")
    advice += (f"Best: {best['provider']} {best['when']}, rating {best['rating']}, visit fee "
               f"{best['fee_inr']} rupees." if best else "No slot fits the family's calendar.")
    db.log("calendar", "Checked technician slots vs family calendar", advice)
    return {"appliance": a["name"] if a else None, "options": options,
            "recommendation": advice, "budget": prefs.get("repair_budget"),
            "next_step": ("Now call book_service for the best slot. It only prepares the "
                          "booking; then tell the user the clash you skipped and ask one yes/no "
                          "question.") if best else "Ask the user for another day."}


def _pending(kind: str, payload: dict, summary: str) -> dict:
    # Re-asking for the same action reuses the open request instead of creating a duplicate.
    same = db.one("SELECT id FROM pending_actions WHERE kind=? AND payload=? "
                  "AND status='awaiting_confirmation'", (kind, json.dumps(payload)))
    if same:
        return {"status": "awaiting_confirmation", "action_id": same["id"], "summary": summary,
                "instruction": "Already prepared. If the user has agreed, call confirm_action now."}
    db.run("UPDATE pending_actions SET status='superseded' WHERE kind=? "
           "AND status='awaiting_confirmation'", (kind,))
    action_id = "ACT-" + uuid.uuid4().hex[:5].upper()
    db.run("INSERT INTO pending_actions VALUES (?,?,?,?,?,?)",
           (action_id, kind, json.dumps(payload), summary, "awaiting_confirmation",
            db.iso(db.now())))
    db.log("confirm", "Waiting for confirmation", summary)
    return {"status": "awaiting_confirmation", "action_id": action_id, "summary": summary,
            "instruction": "Read the summary and ask for a yes or no. Nothing has happened yet."}


def book_service(provider: str, slot_at: str, appliance: str = "") -> dict:
    """Prepare a paid technician booking. Needs the user's yes before it is placed."""
    a = _appliance(appliance) if appliance else db.one(
        "SELECT * FROM appliances WHERE id=?", (db.recall("active_appliance") or "AC-LIVING",))
    if not a:
        return {"error": f"No appliance matching '{appliance}'."}
    when = _parse_when(slot_at)
    if isinstance(when, str):
        return {"error": when}
    existing = db.one("SELECT id, slot_at FROM bookings WHERE appliance_id=? AND status='booked'",
                      (a["id"],))
    if existing:
        return {"status": "already_booked", "booking_id": existing["id"],
                "when": _spoken_time(_dt(existing["slot_at"])),
                "note": "A technician is already booked for this. Do not book again."}
    s = db.one("SELECT * FROM slots WHERE provider LIKE ? AND slot_at=? AND available=1",
               (f"%{provider.split()[0]}%", db.iso(when)))
    if not s:
        return {"error": "That slot isn't available. Call find_service_slots and pick one."}
    clash = _clashes(when, 60)
    if clash:
        return {"error": f"That time clashes with {clash[0]['title']}. Pick another slot."}
    summary = (f"Book {s['provider']} for the {a['name']} {_spoken_time(when)}, "
               f"visit fee {s['fee']} rupees.")
    return _pending("book_service", {"appliance_id": a["id"], "slot_id": s["id"]}, summary)


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
        db.log("confirm", "Blocked: user said no", db.recall("last_user_text"))
    if not approved:
        db.run("UPDATE pending_actions SET status='cancelled' WHERE id=?", (a["id"],))
        db.log("confirm", "Cancelled by user", a["summary"])
        return {"status": "cancelled", "action_id": a["id"]}
    p = json.loads(a["payload"])
    if a["kind"] == "book_service":
        s = db.one("SELECT * FROM slots WHERE id=? AND available=1", (p["slot_id"],))
        if not s:
            return {"error": "That slot was just taken. Find another."}
        app = db.one("SELECT * FROM appliances WHERE id=?", (p["appliance_id"],))
        bid = "BK-" + str(7000 + len(db.q("SELECT id FROM bookings")) + 1)
        start = _dt(s["slot_at"])
        db.run("UPDATE slots SET available=0 WHERE id=?", (s["id"],))
        db.run("INSERT INTO bookings VALUES (?,?,?,?,?,?,?)",
               (bid, app["id"], s["provider"], s["slot_at"], s["fee"], "booked", db.iso(db.now())))
        # Automation: the booking lands on the family calendar with a reminder before it.
        db.run("INSERT INTO events (title,member,start_at,minutes,location,source) "
               "VALUES (?,?,?,?,?,?)", (f"{app['name']} repair: {s['provider']}", "anika",
                                        s["slot_at"], 60, "home", f"booking {bid}"))
        db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
               (f"{s['provider']} technician arrives in 30 minutes ({bid})", "anika",
                db.iso(start - timedelta(minutes=30)), "scheduled", db.iso(db.now())))
        db.log("booking", f"Booked {bid}: {s['provider']}", a["summary"])
        db.log("calendar", "Added to family calendar", f"{app['name']} repair {_spoken_time(start)}")
        result = {"status": "done", "booking_id": bid, "when": _spoken_time(start),
                  "also_done": "added to the family calendar with a reminder 30 minutes before"}
    elif a["kind"] == "order_groceries":
        rows = [db.one("SELECT * FROM shopping WHERE id=?", (i,)) for i in p["ids"]]
        rows = [r for r in rows if r and r["status"] == "needed"]
        if not rows:
            return {"error": "Those items are no longer on the list."}
        oid = "GR-" + str(3100 + len(db.q("SELECT id FROM grocery_orders")) + 1)
        start = _dt(p["slot_at"])
        db.run("INSERT INTO grocery_orders VALUES (?,?,?,?,?,?)",
               (oid, json.dumps([r["item"] for r in rows]), p["total"], p["slot_at"], "placed",
                db.iso(db.now())))
        for r in rows:
            db.run("UPDATE shopping SET status='ordered', bought_by=?, bought_at=? WHERE id=?",
                   (oid, db.iso(db.now()), r["id"]))
        db.run("INSERT INTO events (title,member,start_at,minutes,location,source) VALUES (?,?,?,?,?,?)",
               (f"Grocery delivery ({oid})", "anika", p["slot_at"], 30, "home", f"order {oid}"))
        # The pickup errand is no longer needed: close it and tell the person.
        task = next((t for t in db.q("SELECT * FROM tasks WHERE status='open'")
                     if any(w in t["title"].lower() for w in ("pick up", "grocer", "buy", "milk"))), None)
        if task:
            db.run("UPDATE tasks SET status='done', done_at=? WHERE id=?", (db.iso(db.now()), task["id"]))
            who = db.one("SELECT name FROM members WHERE id=?", (task["assignee"],))
            _send_family(f"@{who['name'] if who else task['assignee']}: no need to stop at the shop, "
                         f"Anika ordered the groceries ({oid}), arriving {_spoken_time(start)}.")
        db.log("order", f"Grocery order {oid} placed", a["summary"])
        result = {"status": "done", "order_id": oid, "arriving": _spoken_time(start),
                  "also_done": "added to the calendar" + ("; pickup task closed and Rohan told"
                                                         if task else "")}
    elif a["kind"] == "store_order":
        cart = _store("view_cart", {"cart_id": CART_ID})
        if [(l["item_id"], l["qty"]) for l in cart.get("items", [])] != [tuple(x) for x in p["cart"]]:
            return {"error": "The cart changed since you confirmed. Read it back again."}
        r = _store("place_order", {"cart_id": CART_ID, "address": "Home"})
        if r.get("error"):
            return r
        oid = r["order_id"]
        db.remember("store_last_order", oid)
        orders = json.loads(db.recall("store_orders") or "[]")
        orders.append({"id": oid, "last": "awaiting_approval"})
        db.remember("store_orders", json.dumps(orders[-5:]))
        db.log("order", f"Store order {oid}: payment requested", a["summary"])
        result = {"status": "payment_requested", "order_id": oid, "total_inr": r["total_inr"],
                  "say": "Tell the user the payment request is on their phone (QR on the "
                         "dashboard); the food is prepared once they approve it."}
    elif a["kind"] == "urgent_alert":
        sent = _send_family(p["message"], urgent=True)
        result = {"status": "done", "delivery": sent}
    else:
        return {"error": f"Unknown action kind {a['kind']}."}
    db.run("UPDATE pending_actions SET status='done' WHERE id=?", (a["id"],))
    return result


NO_WORDS = {"no", "nope", "nah", "don't", "dont", "cancel", "stop", "wait", "not",
            "never", "hold", "nahi", "mat"}
# Only explicit agreement counts as yes: "No, don't book it" must stay a no.
YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "haan", "confirm", "proceed",
             "correct", "absolutely"}


def _said_no(text: str) -> bool:
    words = set(text.lower().replace(",", " ").replace(".", " ").replace("!", " ").split())
    phrase = " ".join(text.lower().split())
    neg = bool(words & NO_WORDS) or "do not" in phrase
    pos = bool(words & YES_WORDS) or "go ahead" in phrase or "do it" in phrase.replace("don't do it", "")
    return neg and not pos


# ---------------------------------------------------------------- Action: family logistics

TIME_WORDS = ("tomorrow", "today", "tonight", "morning", "afternoon", "evening", "noon",
              "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
              "next week", "o'clock", "thirty", "ten", "eleven", "twelve", "nine", "eight",
              "seven", "six", "five", "four", "three", "two", "one", "kal", "am", "pm", "daily")


def _time_said() -> bool:
    said = db.recall("last_user_text").lower()
    return not said or any(w in said for w in TIME_WORDS) or any(c.isdigit() for c in said)


def add_event(title: str, start_at: str, member: str = "", minutes: int = 60) -> dict:
    """Add an event to the family calendar (warns about clashes)."""
    when = _parse_when(start_at)
    if isinstance(when, str):
        return {"error": when}
    if not _time_said():
        return {"error": "The user hasn't said a time. Ask when."}
    clash = _clashes(when, minutes)
    if clash:
        return {"status": "clash", "clashes_with": [c["title"] for c in clash],
                "note": "Tell the user and ask whether to keep both or pick another time."}
    m = _member(member) if member else None
    db.run("INSERT INTO events (title,member,start_at,minutes,location,source) VALUES (?,?,?,?,?,?)",
           (title, m["id"] if m else "family", db.iso(when), minutes, "", "voice"))
    db.log("calendar", f"Added: {title}", _spoken_time(when))
    return {"status": "added", "when": _spoken_time(when)}


def add_to_shopping_list(items: list[str], note: str = "") -> dict:
    """Add items to the family shopping list (duplicates are skipped)."""
    if isinstance(items, str):
        items = [i.strip() for i in re.split(r",| and ", items) if i.strip()]
    have = [s["item"] for s in db.q("SELECT item FROM shopping WHERE status='needed'")]
    added, skipped = [], []
    for it in items:
        it = it.strip().rstrip(".")
        if not it:
            continue
        if any(_similar(it, h, 0.99) for h in have):  # "BP tablet" == "blood pressure tablets"
            skipped.append(it)
            continue
        db.run("INSERT INTO shopping (item,note,status,created_at) VALUES (?,?,?,?)",
               (it[:1].upper() + it[1:], note, "needed", db.iso(db.now())))
        have.append(it)
        added.append(it)
    if added:
        db.log("shopping", "Shopping list updated", ", ".join(added))
    return {"status": "updated", "added": added, "already_on_list": skipped,
            "list": [s["item"] for s in db.q("SELECT item FROM shopping WHERE status='needed'")],
            "store": _household().get("grocery_store")}


# Typical prices at the family's store, used for estimates and the demo order (rupees).
PRICES = {"milk": 60, "paneer": 90, "banana": 60, "bp tablet": 120, "bread": 45, "curd": 40,
          "butter": 58, "rice": 85, "atta": 70, "dal": 140, "tomato": 40, "onion": 35,
          "potato": 30, "apple": 160, "egg": 84, "sugar": 50, "tea": 120, "coffee": 190,
          "oil": 160, "soap": 45, "detergent": 120, "biscuit": 30, "spinach": 30,
          "coriander": 10, "ghee": 280, "water": 20, "juice": 110, "yogurt": 40}


def _price(item: str) -> int | None:
    words = _canon(item)
    for key, p in PRICES.items():
        if set(key.split()) <= words:
            return p
    return None


def _list_rows(status: str = "needed") -> list[dict]:
    return db.q("SELECT * FROM shopping WHERE status=? ORDER BY id", (status,))


def shopping_status() -> dict:
    """What's on the list, what's been bought (and by whom), and an estimated total."""
    needed, bought = _list_rows("needed"), _list_rows("bought")
    est = [(_price(r["item"]), r["item"]) for r in needed]
    total = sum(p for p, _ in est if p)
    unknown = [i for p, i in est if p is None]
    return {"to_buy": [r["item"] for r in needed],
            "bought": [f"{r['item']} ({r['bought_by'] or 'someone'})" for r in bought],
            "estimated_total_inr": total, "no_price_for": unknown,
            "store": _household().get("grocery_store")}


def _match_items(items, rows: list[dict]) -> list[dict]:
    if isinstance(items, str):
        if items.strip().lower() in ("all", "everything", "all of it", "the whole list"):
            return rows
        items = [i.strip() for i in re.split(r",| and ", items) if i.strip()]
    out = []
    for it in items:
        hit = next((r for r in rows if _similar(it, r["item"], 0.5) or
                    _canon(it) & _canon(r["item"])), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def _close_pickup_if_done(by: str) -> str:
    """When nothing is left to buy, the open grocery pickup task is completed automatically."""
    if _list_rows("needed"):
        return ""
    task = next((t for t in db.q("SELECT * FROM tasks WHERE status='open'")
                 if any(w in t["title"].lower() for w in ("pick up", "grocer", "buy", "milk"))), None)
    if not task:
        return ""
    db.run("UPDATE tasks SET status='done', done_at=? WHERE id=?", (db.iso(db.now()), task["id"]))
    db.log("task", "Task done", f"{task['title']} (all items bought)")
    _send_family(f"✅ Shopping done{' by ' + by if by else ''}. Everything on the list is bought.")
    return task["title"]


def mark_bought(items, by: str = "") -> dict:
    """Tick items off the shopping list ('Rohan bought the milk'); 'all' ticks everything."""
    rows = _list_rows("needed")
    hit = _match_items(items, rows)
    if not hit:
        return {"error": "None of those are on the list.", "to_buy": [r["item"] for r in rows]}
    m = _member(by) if by else None
    who = m["name"] if m else (by.title() if by else "")
    for r in hit:
        db.run("UPDATE shopping SET status='bought', bought_by=?, bought_at=? WHERE id=?",
               (who or None, db.iso(db.now()), r["id"]))
    db.log("shopping", f"Bought{' by ' + who if who else ''}", ", ".join(r["item"] for r in hit))
    closed = _close_pickup_if_done(who)
    left = [r["item"] for r in _list_rows("needed")]
    return {"status": "updated", "bought": [r["item"] for r in hit], "still_to_buy": left,
            **({"task_completed": closed} if closed else {})}


def remove_from_shopping_list(items) -> dict:
    """Take items off the list without buying them."""
    hit = _match_items(items, _list_rows("needed"))
    if not hit:
        return {"error": "None of those are on the list."}
    for r in hit:
        db.run("UPDATE shopping SET status='removed' WHERE id=?", (r["id"],))
    db.log("shopping", "Removed from list", ", ".join(r["item"] for r in hit))
    return {"status": "removed", "removed": [r["item"] for r in hit],
            "still_to_buy": [r["item"] for r in _list_rows("needed")]}


def order_groceries() -> dict:
    """Prepare a home-delivery order for everything on the list (demo store). Needs a yes."""
    rows = _list_rows("needed")
    if not rows:
        return {"error": "The shopping list is empty."}
    if db.one("SELECT id FROM grocery_orders WHERE status='placed'"):
        return {"status": "already_ordered", "note": "A grocery delivery is already on its way."}
    priced = [(r["item"], _price(r["item"])) for r in rows]
    items_total = sum(p or 0 for _, p in priced)
    fee = 0 if items_total >= 499 else 25
    # Earliest delivery slot (every 30 min from 45 min out) that avoids the calendar and quiet hours.
    t = (db.now() + timedelta(minutes=45)).replace(second=0, microsecond=0)
    t = t.replace(minute=0 if t.minute < 30 else 30) + timedelta(minutes=30)
    while _clashes(t, 30) or not (8 <= t.hour < 20):
        t += timedelta(minutes=30)
        if t.hour >= 20:
            t = (t + timedelta(days=1)).replace(hour=8, minute=0)
    store = _household().get("grocery_store", "the store").split(",")[0]
    names = ", ".join(i for i, _ in priced)
    summary = (f"Order {names} from {store} for about {items_total + fee} rupees"
               f"{' including 25 rupees delivery' if fee else ''}, delivered {_spoken_time(t)}.")
    return _pending("order_groceries", {"ids": [r["id"] for r in rows], "total": items_total + fee,
                                        "slot_at": db.iso(t)}, summary)


# ---------------------------------------------------------------- Action: ordering over MCP
CART_ID = "homepilot-anika"


def _store(tool: str, args: dict) -> dict:
    from . import store_client
    return store_client.call(tool, args)


def store_search(query: str) -> dict:
    """Search the delivery store (food, groceries, snacks, personal care) over MCP."""
    r = _store("search_items", {"query": query})
    db.log("store", "Searched store", query)
    return r


def store_add_to_cart(items: list) -> dict:
    """Add items to the delivery cart: [{"item_id": "F101", "qty": 1}] or names."""
    if isinstance(items, (str, dict)):
        items = [items]
    added, problems, cart = [], [], None
    for it in items:
        if isinstance(it, str):
            it = {"name": it}
        qty = int(it.get("qty") or it.get("quantity") or 1)
        item_id = it.get("item_id") or it.get("id")
        if not item_id:
            hits = _store("search_items", {"query": it.get("name", "")}).get("results") or []
            if not hits:
                problems.append(f"Couldn't find '{it.get('name')}'.")
                continue
            item_id = hits[0]["id"]
        r = _store("add_to_cart", {"cart_id": CART_ID, "item_id": item_id, "qty": qty})
        if r.get("error"):
            problems.append(r["error"])
        else:
            cart = r["cart"]
            added.append(next(l["name"] for l in cart["items"] if l["item_id"] == item_id))
    if added:
        db.log("store", "Added to cart", ", ".join(added))
    return {"added": added, "problems": problems, "cart": cart or _store("view_cart", {"cart_id": CART_ID})}


def store_view_cart() -> dict:
    """Show the delivery cart and total."""
    return _store("view_cart", {"cart_id": CART_ID})


def store_checkout() -> dict:
    """Read back the cart and prepare the order. Needs the user's yes; then payment is
    approved on their phone."""
    cart = _store("view_cart", {"cart_id": CART_ID})
    if cart.get("error") or not cart.get("items"):
        return {"error": cart.get("error") or "The cart is empty."}
    items = ", ".join(f"{l['qty']} {l['name']}" for l in cart["items"])
    fee = f" including {cart['delivery_fee_inr']} rupees delivery" if cart["delivery_fee_inr"] else ""
    summary = (f"Order {items} from FreshMart for {cart['total_inr']} rupees{fee}, arriving in "
               f"about {cart['eta_min']} minutes. You'll approve the payment on your phone.")
    return _pending("store_order", {"cart": [(l["item_id"], l["qty"]) for l in cart["items"]],
                                    "total": cart["total_inr"]}, summary)


def store_order_status(order_id: str = "") -> dict:
    """Payment and delivery status of the latest (or a given) store order."""
    oid = order_id or db.recall("store_last_order")
    if not oid:
        return {"error": "No store order yet."}
    return _store("order_status", {"order_id": oid})


def assign_task(title: str, assignee: str, due_at: str = "") -> dict:
    """Give a family member a task and notify them on the family channel."""
    m = _member(assignee)
    if not m:
        return {"error": f"Who is '{assignee}'? Family: " +
                         ", ".join(x["name"] for x in db.q("SELECT name FROM members"))}
    due = None
    if due_at:
        due = _parse_when(due_at)
        if isinstance(due, str):
            return {"error": due}
    dup = next((t for t in db.q("SELECT * FROM tasks WHERE assignee=? AND status='open'", (m["id"],))
                if _similar(title, t["title"])), None)
    if dup:
        if due and dup["due_at"] != db.iso(due):
            db.run("UPDATE tasks SET due_at=? WHERE id=?", (db.iso(due), dup["id"]))
        return {"status": "already_assigned", "task": dup["title"],
                "note": "This task already exists; nothing new was sent."}
    db.run("INSERT INTO tasks (title,assignee,due_at,status,created_at) VALUES (?,?,?,?,?)",
           (title, m["id"], db.iso(due) if due else None, "open", db.iso(db.now())))
    when = f" by {_spoken_time(due)}" if due else ""
    extra = ""
    if any(w in title.lower() for w in ("pick up", "grocer", "buy", "shop", "milk")):
        from urllib.parse import quote
        base = config.PUBLIC_BASE_URL or "http://localhost:8000"
        extra = f" Live list (tick items as you shop): {base}/list.html?name={quote(m['name'])}"
    sent = _send_family(f"@{m['name']}: new task from Anika: {title}{when}.{extra}")
    db.log("task", f"Task for {m['name']}", title + when)
    return {"status": "assigned", "to": m["name"], "notified": sent}


def complete_task(task: str) -> dict:
    """Mark a family task as done (track completion)."""
    rows = db.q("SELECT * FROM tasks WHERE status='open'")
    words = [w for w in task.lower().split() if len(w) > 2]
    match = next((t for t in rows if task.strip() == str(t["id"])), None) or max(
        rows, key=lambda t: sum(w in t["title"].lower() for w in words), default=None)
    if not match or not any(w in match["title"].lower() for w in words):
        return {"error": f"No open task like '{task}'.",
                "open_tasks": [t["title"] for t in rows]}
    db.run("UPDATE tasks SET status='done', done_at=? WHERE id=?", (db.iso(db.now()), match["id"]))
    db.log("task", "Task done", f"{match['title']} ({match['assignee']})")
    left = db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='open'")["n"]
    return {"status": "done", "task": match["title"], "open_tasks_left": left}


def _until(dt: datetime) -> str:
    mins = max(0, round((dt - db.now()).total_seconds() / 60))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} hour{'s' if h > 1 else ''} {m} minute{'s' if m != 1 else ''}"
    return f"{h} hour{'s' if h > 1 else ''}" if h else f"{m} minute{'s' if m != 1 else ''}"


def set_reminder(text: str, at: str, member: str = "") -> dict:
    """Schedule a reminder or alarm for a family member (e.g. medicine). It rings on the
    HomePilot dashboard, is spoken in the live call, and is posted to the family channel."""
    if not _time_said():
        return {"error": "The user hasn't said a time. Ask when."}
    when = _parse_when(at)
    if isinstance(when, str):
        return {"error": when}
    if when < db.now() - timedelta(minutes=1):
        when = when + timedelta(days=1)  # "8 PM" said after 8 PM means tomorrow
    m = _member(member) if member else None
    who = m["id"] if m else "anika"
    near = db.one("SELECT id, remind_at FROM reminders WHERE member=? AND status='scheduled' "
                  "AND remind_at BETWEEN ? AND ?",
                  (who, db.iso(when - timedelta(minutes=2)), db.iso(when + timedelta(minutes=2))))
    if near:
        return {"status": "already_set", "when": _spoken_time(when), "rings_in": _until(when)}
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           (text, who, db.iso(when), "scheduled", db.iso(db.now())))
    db.log("reminder", f"Reminder for {m['name'] if m else 'Anika'}", f"{text}, {_spoken_time(when)}")
    return {"status": "scheduled", "for": m["name"] if m else "Anika", "when": _spoken_time(when),
            "rings_in": _until(when),
            "say": "Read back the time with AM or PM and 'in X' exactly as given here."}


def cancel_reminder(which: str = "all") -> dict:
    """Cancel reminders/alarms: 'all', a time like '10:50 PM', or words from the reminder."""
    rows = db.q("SELECT * FROM reminders WHERE status='scheduled' ORDER BY remind_at")
    key = (which or "all").strip().lower()
    if key in ("all", "everything", "all alarms", "all reminders", "every alarm"):
        hit = rows
    else:
        t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", key)
        hit = []
        if t:
            hh, mm = int(t.group(1)), int(t.group(2) or 0)
            ap = (t.group(3) or "").replace(".", "")
            for r in rows:
                d = _dt(r["remind_at"])
                hours = {hh % 12 + (12 if ap == "pm" else 0)} if ap else {hh % 12, hh % 12 + 12}
                if d.hour in hours and d.minute == mm:
                    hit.append(r)
        if not hit:
            words = [w for w in key.split() if len(w) > 2]
            hit = [r for r in rows if any(w in r["text"].lower() for w in words)]
    if not hit:
        return {"status": "nothing_matched",
                "scheduled": [f"{r['text']} {_spoken_time(_dt(r['remind_at']))}" for r in rows]}
    for r in hit:
        db.run("UPDATE reminders SET status='cancelled' WHERE id=?", (r["id"],))
    db.log("reminder", f"Cancelled {len(hit)} reminder{'s' if len(hit) > 1 else ''}",
           ", ".join(_spoken_time(_dt(r["remind_at"])) for r in hit))
    left = db.one("SELECT COUNT(*) AS n FROM reminders WHERE status='scheduled'")["n"]
    return {"status": "cancelled", "count": len(hit),
            "cancelled": [f"{r['text']} {_spoken_time(_dt(r['remind_at']))}" for r in hit],
            "still_scheduled": left}


def due_reminders() -> list[dict]:
    """Mark reminders whose time has come as fired and return them (called every few seconds)."""
    due = db.q("SELECT * FROM reminders WHERE status='scheduled' AND remind_at<=? ORDER BY remind_at",
               (db.iso(db.now()),))
    for r in due:
        db.run("UPDATE reminders SET status='fired' WHERE id=?", (r["id"],))
        m = db.one("SELECT name FROM members WHERE id=?", (r["member"],))
        name = m["name"] if m else "Anika"
        r["spoken"] = f"Reminder for {name}: {r['text']}."
        db.log("alarm", f"Reminder for {name}", r["text"])
        _send_family(f":alarm_clock: Reminder for {name}: {r['text']}")
    return due


def _send_family(message: str, urgent: bool = False) -> str:
    prefix = ":rotating_light: URGENT " if urgent else ":house: "
    delivered = integrations.send_slack(prefix + message, _household().get("family_channel",
                                                                          "#family"))
    return "Slack" if delivered else "dashboard only (Slack not configured)"


def send_family_update(message: str, urgent: bool = False) -> dict:
    """Post an update to the family channel. Urgent alerts need the user's yes."""
    said = db.recall("last_user_text").lower()
    asked = not said or bool(re.search(
        r"\b(tell|message|text|inform|notify|update|ping|let)\s+(the\s+)?"
        r"(family|everyone|everybody|them|rohan|kabir|dadi|grandma|all)\b"
        r"|\b(send|post)\b.*\b(family|everyone|update|message|group|slack|whatsapp)\b"
        r"|\bfamily (update|group|channel|chat)\b|\bannounce\b", said))
    if not asked:
        return {"error": "The user did not ask to message the family. Don't post your own "
                         "notes; just answer the user."}
    if urgent:
        return _pending("urgent_alert", {"message": message},
                        f"Send an urgent alert to the whole family: {message}")
    recent = db.one("SELECT id FROM notifications WHERE message=? AND created_at>=?",
                    (":house: " + message, db.iso(db.now() - timedelta(minutes=5))))
    if recent:
        return {"status": "already_sent"}
    sent = _send_family(message)
    db.log("notify", "Family update sent", message)
    return {"status": "sent", "delivery": sent}


def daily_summary() -> dict:
    """Wrap-up: what got done today, what's still open, what's coming next. Posts to family."""
    today = db.now().date().isoformat()
    done = [t["title"] for t in db.q("SELECT title FROM tasks WHERE status='done' AND done_at>=?",
                                     (today,))]
    open_t = [f"{t['title']} ({t['assignee']})" for t in
              db.q("SELECT title, assignee FROM tasks WHERE status='open'")]
    bookings = [f"{b['provider']} {_spoken_time(_dt(b['slot_at']))} ({b['id']})" for b in
                db.q("SELECT * FROM bookings WHERE status='booked'")]
    rem = [f"{r['text']} ({_spoken_time(_dt(r['remind_at']))})" for r in
           db.q("SELECT * FROM reminders WHERE status='scheduled' ORDER BY remind_at LIMIT 4")]
    shop = [s["item"] for s in db.q("SELECT item FROM shopping WHERE status='needed'")]
    lines = ["Family update from HomePilot"]
    if bookings:
        lines.append("Booked: " + "; ".join(bookings))
    if done:
        lines.append("Done: " + "; ".join(done))
    if open_t:
        lines.append("Still open: " + "; ".join(open_t))
    if shop:
        lines.append("Shopping list: " + ", ".join(shop))
    if rem:
        lines.append("Reminders: " + "; ".join(rem))
    body = "\n".join(lines)
    recent = db.one("SELECT id FROM notifications WHERE message LIKE ? AND created_at>=?",
                    (":house: Family update from HomePilot%",
                     db.iso(db.now() - timedelta(minutes=3))))
    if not recent:
        _send_family(body)
        db.log("notify", "Daily summary sent to family", f"{len(done)} done, {len(open_t)} open")
    return {"status": "sent", "summary": body}


def _invite_one(raw: str, reason: str) -> dict:
    m = _member(raw)
    if m and m["id"] == "anika":
        return {"who": "Anika", "error": "Anika is already in the call."}
    if m:
        name, key = m["name"], m["id"]
    else:
        # Guests by name are fine ("my friend Priya"); phone numbers can't be dialled.
        cleaned = re.sub(r"^(my|our)\s+(friend|neighbou?r|cousin|uncle|aunt|colleague)\s+", "",
                         raw.strip(), flags=re.I).strip()
        if not cleaned or re.search(r"\d", cleaned) or len(cleaned.split()) > 3:
            return {"who": raw, "error": "I can only invite people by name with a join link. "
                                         "I can't dial phone numbers."}
        name, key = cleaned.title(), "guest_" + re.sub(r"\W+", "_", cleaned.lower())
    last = db.recall("invite_" + key)
    if last and datetime.fromisoformat(last) > db.now() - timedelta(minutes=3):
        return {"who": name, "status": "already_invited"}
    db.remember("invite_" + key, db.iso(db.now()))
    from urllib.parse import quote
    base = config.PUBLIC_BASE_URL or "http://localhost:8000"
    link = f"{base}/expert.html?name={quote(name)}"
    _send_family(f":telephone_receiver: @{name}, Anika wants you on a live call"
                 f"{': ' + reason if reason else ''}. Join now: {link}")
    invites = [i for i in json.loads(db.recall("invites") or "[]") if i["who"] != name]
    invites.append({"who": name, "link": link, "at": db.iso(db.now()), "joined": False,
                    "guest": not m})
    db.remember("invites", json.dumps(invites[-6:]))
    db.remember("expert_call", db.iso(db.now()))
    db.log("call", f"{name} invited to live call" + (" (guest)" if not m else ""), reason)
    return {"who": name, "status": "link_sent", "join_link": link}


def call_family_member(member: str, reason: str = "") -> dict:
    """Invite one or more people into this live voice call (Agora RTC): 'Rohan',
    'Priya and Kabir'. Each gets a join link in the family channel; a QR code shows on screen."""
    names = [n.strip() for n in re.split(r",|&|\band\b|\balso\b", member or "", flags=re.I)
             if n.strip()]
    if not names:
        return {"error": "Who should I invite?"}
    results = [_invite_one(n, reason) for n in names[:5]]
    sent = [r["who"] for r in results if r.get("status") == "link_sent"]
    already = [r["who"] for r in results if r.get("status") == "already_invited"]
    errors = [f"{r['who']}: {r['error']}" for r in results if r.get("error")]
    out = {"status": "link_sent" if sent else ("already_invited" if already else "error"),
           "invited": sent, "already_invited": already, "errors": errors,
           "say": ("Say you've sent a join link to " + " and ".join(sent) + "; it's in the family "
                   "channel and as a QR code on the screen. Never say you are phoning anyone.")
                  if sent else "Explain briefly."}
    if len(results) == 1 and results[0].get("join_link"):
        out["who"], out["join_link"] = results[0]["who"], results[0]["join_link"]
    if not sent and not already:
        out["error"] = "; ".join(errors)
    return out


def remember_fact(fact: str) -> dict:
    """Remember something the user asked you to keep (birthdays, preferences, numbers)."""
    fact = fact.strip().rstrip(".")
    if not fact:
        return {"error": "Nothing to remember."}
    facts = recall_facts()
    if any(_similar(fact, f, 0.8) for f in facts):
        return {"status": "already_remembered"}
    db.run("INSERT INTO memory VALUES (?,?)", (f"fact_{len(facts) + 1:03d}", fact))
    db.log("memory", "Remembered", fact)
    return {"status": "remembered", "fact": fact}


def recall_facts() -> list[str]:
    return [r["value"] for r in db.q("SELECT value FROM memory WHERE key LIKE 'fact_%' ORDER BY key")]


def shift_briefing() -> str:
    """Spoken greeting when the agent joins: the day at a glance."""
    now = db.now()
    events = [e for e in db.q("SELECT title, start_at FROM events ORDER BY start_at")
              if now <= _dt(e["start_at"]) <= now.replace(hour=23, minute=59)]
    tasks = db.one("SELECT COUNT(*) AS n FROM tasks WHERE status='open'")["n"]
    part = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    if events:
        first = events[0]
        ev = (f"Today you have {len(events)} thing{'s' if len(events) > 1 else ''} on the family "
              f"calendar. Next is {first['title']} {_spoken_time(_dt(first['start_at']))}.")
    else:
        ev = "Nothing else is on the family calendar today."
    return (f"{part} Anika, HomePilot here. {ev} There are {tasks} open family tasks. "
            "How can I help?")


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
WHEN = {"type": "string", "description": "Local time, no timezone, e.g. 2026-11-21T18:30"}
TOOLS = {
    "get_today": (get_today, _s(
        "Today's family calendar, open tasks, reminders, shopping list and preferences.")),
    "get_appliance": (get_appliance, _s(
        "Appliance details and service history (detects repeat faults).",
        {"appliance": STR}, ["appliance"])),
    "search_manual": (search_manual, _s(
        "Search appliance manuals: error codes, safe checks, care.", {"query": STR}, ["query"])),
    "find_service_slots": (find_service_slots, _s(
        "Technician slots checked against the family calendar and quiet hours.",
        {"appliance": STR})),
    "book_service": (book_service, _s(
        "Prepare a paid technician booking (needs the user's yes).",
        {"provider": STR, "slot_at": WHEN, "appliance": STR}, ["provider", "slot_at"])),
    "confirm_action": (confirm_action, _s(
        "Execute or cancel a pending action after the user said yes or no.",
        {"action_id": STR, "approved": {"type": "boolean"}}, ["action_id", "approved"])),
    "add_event": (add_event, _s(
        "Add an event to the family calendar (only a time the user said).",
        {"title": STR, "start_at": WHEN, "member": STR, "minutes": {"type": "integer"}},
        ["title", "start_at"])),
    "add_to_shopping_list": (add_to_shopping_list, _s(
        "Add items to the family shopping list.",
        {"items": {"type": "array", "items": STR}, "note": STR}, ["items"])),
    "shopping_status": (shopping_status, _s(
        "What's still to buy, what's been bought and by whom, and the estimated total.")),
    "mark_bought": (mark_bought, _s(
        "Tick items off the shopping list when someone bought them ('all' for everything).",
        {"items": {"type": "array", "items": STR}, "by": STR}, ["items"])),
    "remove_from_shopping_list": (remove_from_shopping_list, _s(
        "Remove items from the shopping list without buying them.",
        {"items": {"type": "array", "items": STR}}, ["items"])),
    "order_groceries": (order_groceries, _s(
        "Prepare a home delivery for everything on the list (needs the user's yes). Picks a "
        "delivery slot that avoids the family calendar.")),
    "store_search": (store_search, _s(
        "Search the delivery store over MCP: food (biryani, dosa…), groceries, snacks, personal care.",
        {"query": STR}, ["query"])),
    "store_add_to_cart": (store_add_to_cart, _s(
        "Add items to the delivery cart (item_id from store_search, or a name) with quantity.",
        {"items": {"type": "array", "items": {"type": "object", "properties": {
            "item_id": STR, "name": STR, "qty": {"type": "integer"}}}}}, ["items"])),
    "store_view_cart": (store_view_cart, _s("Show the delivery cart and total.")),
    "store_checkout": (store_checkout, _s(
        "Read back the cart and prepare the order (needs the user's yes; payment is then "
        "approved on their phone).")),
    "store_order_status": (store_order_status, _s(
        "Payment and delivery status of the latest store order.", {"order_id": STR})),
    "assign_task": (assign_task, _s(
        "Give a family member a task and notify them.",
        {"title": STR, "assignee": STR, "due_at": WHEN}, ["title", "assignee"])),
    "complete_task": (complete_task, _s(
        "Mark a family task as done.", {"task": STR}, ["task"])),
    "set_reminder": (set_reminder, _s(
        "Schedule a reminder or alarm (only a time the user said). It rings on the HomePilot "
        "dashboard, is spoken in the call and posted to the family channel.",
        {"text": STR, "at": WHEN, "member": STR}, ["text", "at"])),
    "cancel_reminder": (cancel_reminder, _s(
        "Cancel reminders or alarms: 'all', a time like '10:50 PM', or words from the text.",
        {"which": STR}, ["which"])),
    "remember_fact": (remember_fact, _s(
        "Save something the user explicitly asks you to remember (a birthday, a preference, a "
        "phone number). Saved facts appear in LIVE CONTEXT.", {"fact": STR}, ["fact"])),
    "send_family_update": (send_family_update, _s(
        "Post a message to the family channel. urgent=true needs the user's yes.",
        {"message": STR, "urgent": {"type": "boolean"}}, ["message"])),
    "daily_summary": (daily_summary, _s(
        "Wrap-up: send the family what got done, what's open, bookings and reminders.")),
    "call_family_member": (call_family_member, _s(
        "Invite one or more people into this live voice call with join links: pass every name "
        "in one call, e.g. 'Priya and Kabir'. Family or guests by name; it never phones anyone.",
        {"member": STR, "reason": STR}, ["member"])),
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
