"""Run: pytest -q   (no API keys needed: the LLM is faked)."""
import json
import os
import tempfile
from types import SimpleNamespace as NS

os.environ["FIELDPILOT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["LLM_API_KEY"] = "test"
os.environ["BRAIN_API_KEY"] = "secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import brain, config, db, rag, tools  # noqa: E402
from backend.main import app  # noqa: E402

config.LLM_API_KEY = "test"
config.BRAIN_API_KEY = "secret"


@pytest.fixture(autouse=True)
def fresh():
    db.reset_db()


# ---------------------------------------------------------------- brain with a fake LLM

def _chunk(content=None, tool=None):
    tcs = None
    if tool:
        tcs = [NS(index=0, id="call_1", function=NS(name=tool[0], arguments=json.dumps(tool[1])))]
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tcs))])


class FakeLLM:
    """Round 1: asks for check_inventory. Round 2: answers using the tool result."""
    def __init__(self):
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw["messages"])
        tool_msgs = [m for m in kw["messages"] if m.get("role") == "tool"]

        async def gen():
            if not tool_msgs:
                yield _chunk(tool=("find_service_slots", {}))
            else:
                yield _chunk("CoolCare has a free slot. ")
                yield _chunk("Shall I book it?")
        return gen()


def test_chat_completions_streams_sse_with_tools(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(brain, "_targets", lambda: [(fake, "m")])
    c = TestClient(app)
    body = {"model": "x", "stream": True, "messages": [
        {"role": "system", "content": "agora default prompt"},
        {"role": "user", "content": "Can someone fix the AC?"}]}
    assert c.post("/chat/completions", json=body).status_code == 401
    r = c.post("/chat/completions", json=body, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    lines = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    text = "".join(json.loads(l)["choices"][0]["delta"].get("content", "") for l in lines[:-1])
    assert text == "Checking technicians against the family calendar. CoolCare has a free slot. Shall I book it?"
    # our system prompt + live context replaced Agora's, and the tool result went back to the LLM
    first = fake.calls[0]
    assert first[0]["content"].startswith("You are HomePilot")
    assert all(m["content"] != "agora default prompt" for m in first)
    assert any(m.get("role") == "tool" and "CoolCare" in m["content"] for m in fake.calls[1])
    kinds = [a["kind"] for a in db.q("SELECT kind FROM activity")]
    assert "user" in kinds and "agent" in kinds and "calendar" in kinds


def test_state_and_text_chat(monkeypatch):
    monkeypatch.setattr(brain, "_targets", lambda: [(FakeLLM(), "m")])
    c = TestClient(app)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "stock?"}]})
    assert r.json()["reply"].endswith("Shall I book it?")
    s = c.get("/api/state").json()
    assert {"events", "tasks", "shopping", "bookings", "pending", "activity"} <= s.keys()
    assert c.get("/").status_code == 200


def test_agora_extra_fields_are_stripped():
    msgs = brain.build_messages([
        {"role": "user", "content": "hi", "turn_id": 1, "timestamp": 2},
        {"role": "assistant", "content": "Hello", "metadata": {"interrupted": False}},
        {"role": "user", "content": [{"type": "text", "text": "first job?"}]}])
    convo = msgs[2:]
    assert convo == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello"},
                     {"role": "user", "content": "first job?"}]


def test_falls_back_when_model_invents_tool(monkeypatch):
    class Bad(FakeLLM):
        async def create(self, **kw):
            async def gen():
                yield _chunk(tool=("find slots", {}))
            return gen()
    monkeypatch.setattr(brain, "_targets", lambda: [(Bad(), "weak"), (FakeLLM(), "good")])
    c = TestClient(app)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "stock?"}]})
    assert r.json()["reply"].endswith("Shall I book it?")


def test_error_turns_are_dropped_from_history():
    msgs = brain.build_messages([
        {"role": "user", "content": "first job?"},
        {"role": "assistant", "content": "Sorry, I lost connection to our systems for a moment."},
        {"role": "user", "content": "try again"}])
    assert [m["content"] for m in msgs[2:]] == ["first job? try again"]  # merged, no error turn


def test_tool_calls_without_index(monkeypatch):
    """Gemini-style stream: two complete tool calls, no index field."""
    class NoIndex(FakeLLM):
        async def create(self, **kw):
            self.calls.append(kw["messages"])
            done = any(m.get("role") == "tool" for m in kw["messages"])
            async def gen():
                if not done:
                    yield NS(choices=[NS(delta=NS(content=None, tool_calls=[
                        NS(index=None, id="a", function=NS(name="get_today", arguments="{}")),
                        NS(index=None, id="b", function=NS(name="find_service_slots",
                                                           arguments='{}'))]))])
                else:
                    yield _chunk("Two tools done.")
            return gen()
    fake = NoIndex()
    monkeypatch.setattr(brain, "_targets", lambda: [(fake, "gemini")])
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200, r.text
    assert r.json()["reply"].endswith("Two tools done.")
    assert len([m for m in fake.calls[1] if m.get("role") == "tool"]) == 2


def test_dead_and_throttled_models_are_skipped(monkeypatch):
    import httpx
    from openai import NotFoundError
    seen = []
    class Gone(FakeLLM):
        async def create(self, **kw):
            seen.append("gone")
            raise NotFoundError("gone", response=httpx.Response(404, request=httpx.Request("POST", "http://x")), body=None)
    brain._cooldown.clear()
    monkeypatch.setattr(brain, "_targets", lambda: [(Gone(), "dead-model"), (FakeLLM(), "good")])
    c = TestClient(app)
    for _ in range(2):
        c.post("/api/chat", json={"messages": [{"role": "user", "content": "stock?"}]})
    assert seen == ["gone"]  # second turn skipped the dead model
    brain._cooldown.clear()


def test_gemini_gets_thought_signatures_and_groq_does_not():
    from types import SimpleNamespace
    hist = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "get_my_tickets", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "get_ticket", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": "REAL"}}}]}]
    gem = brain._for_provider(hist, SimpleNamespace(base_url="https://generativelanguage.googleapis.com/v1beta/openai/"))
    sigs = [tc["extra_content"]["google"]["thought_signature"] for tc in gem[0]["tool_calls"]]
    assert sigs == [brain.DUMMY_SIGNATURE, "REAL"]
    groq = brain._for_provider(hist, SimpleNamespace(base_url="https://api.groq.com/openai/v1"))
    assert all("extra_content" not in tc for tc in groq[0]["tool_calls"])
    assert brain.speakable("status waiting_for_parts") == "status waiting for parts"


def test_lists_are_flattened_for_speech():
    out = brain.speakable("1. Check coil. 2. Test 1x CAP-45, 4 hrs. Confirm order? (Yes/No)")
    assert out == "Check coil. Test 1 CAP-45, 4 hours. Confirm order?"


def test_units_split_across_stream_chunks(monkeypatch):
    class Split(FakeLLM):
        async def create(self, **kw):
            async def gen():
                for p in ["It should be 40.5 ", "µ", "F at least."]:
                    yield _chunk(p)
            return gen()
    monkeypatch.setattr(brain, "_targets", lambda: [(Split(), "m")])
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.json()["reply"] == "It should be 40.5 microfarad at least."


def test_run_demo_finds_tunnel_url():
    import run_demo
    lines = ["2026 INF Requesting new quick Tunnel on trycloudflare.com...",
             "2026 INF |  https://happy-tiger-walks-moon.trycloudflare.com  |"]
    assert run_demo.find_tunnel_url(iter(lines)) == "https://happy-tiger-walks-moon.trycloudflare.com"
    assert run_demo.find_tunnel_url(iter(["no url here"])) is None




# ---------------------------------------------------------------- HomePilot family workflow

def _ok_slot():
    return next(o for o in tools.find_service_slots()["options"] if o["ok"])


def test_rag_finds_e4_safe_checks():
    hits = rag.search("AC error E4 warm air what can I check")
    assert hits and "E4" in " ".join(h["text"] for h in hits)


def test_before_today_has_calendar_tasks_and_preferences():
    t = tools.call("get_today", {})
    titles = [e["title"] for e in t["events_next_30h"]]
    assert any("doctor" in x for x in titles) and t["open_tasks"] and "diet" in t["preferences"]
    b = tools.shift_briefing()
    assert "Anika" in b and "open family tasks" in b


def test_repeat_fault_from_spoken_code():
    db.remember("last_user_text", "the living room AC shows e four and blows warm air")
    r = tools.call("get_appliance", {"appliance": "living room AC"})
    assert r["insight"]["code"] == "E4" and r["insight"]["previous"] == 2
    assert "capacitor" in r["insight"]["likely_root_cause"]
    assert tools._codes_in("E-4 again") == ["E4"]


def test_slots_skip_calendar_clash_and_quiet_hours():
    f = tools.call("find_service_slots", {"appliance": "AC"})
    bad = [o for o in f["options"] if not o["ok"]]
    assert any("doctor" in " ".join(o["problems"]) for o in bad)
    assert all(o["ok"] is False for o in f["options"] if int(o["slot_at"][11:13]) >= 20)
    assert "Best:" in f["recommendation"] and "clashes with Dadi" in f["recommendation"]
    clash = next(o for o in bad if "doctor" in " ".join(o["problems"]))
    assert "error" in tools.call("book_service", {"provider": clash["provider"], "slot_at": clash["slot_at"]})


def test_booking_needs_yes_and_lands_on_calendar_once():
    o = _ok_slot()
    p = tools.call("book_service", {"provider": o["provider"], "slot_at": o["slot_at"].replace(" ", "T")})
    assert p["status"] == "awaiting_confirmation" and db.q("SELECT * FROM bookings") == []
    db.remember("last_user_text", "yes book it")
    done = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert done["status"] == "done"
    assert db.one("SELECT * FROM events WHERE source LIKE 'booking%'")
    assert db.one("SELECT * FROM reminders WHERE text LIKE '%technician arrives%'")
    again = tools.call("book_service", {"provider": o["provider"], "slot_at": o["slot_at"]})
    assert again["status"] == "already_booked" and len(db.q("SELECT * FROM bookings")) == 1


def test_no_means_no_even_if_model_approves():
    o = _ok_slot()
    p = tools.call("book_service", {"provider": o["provider"], "slot_at": o["slot_at"]})
    db.remember("last_user_text", "No. No, don't book it.")
    r = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert r["status"] == "cancelled" and db.q("SELECT * FROM bookings") == []


def test_shopping_tasks_completion_and_reminders():
    r = tools.call("add_to_shopping_list", {"items": ["milk", "Paneer", "bananas"]})
    assert r["added"] == ["milk", "Paneer"]  # bananas already on the list
    a = tools.call("assign_task", {"title": "Pick up groceries", "assignee": "Rohan"})
    assert a["status"] == "assigned" and db.q("SELECT * FROM notifications")
    assert tools.call("assign_task", {"title": "Pick up groceries", "assignee": "dad"})["status"] == "already_assigned"
    d = tools.call("complete_task", {"task": "recycling"})
    assert d["status"] == "done" and "recycling" in d["task"].lower()
    db.remember("last_user_text", "remind Dadi at 8 pm to take her BP tablet")
    rem = tools.call("set_reminder", {"text": "BP tablet", "at": "2030-01-01T20:00", "member": "grandma"})
    assert rem["status"] == "scheduled" and rem["for"] == "Dadi"
    db.remember("last_user_text", "remind me about that")
    assert "error" in tools.call("set_reminder", {"text": "x", "at": "2030-01-01T09:00"})


def test_calendar_event_clash_and_utc():
    db.remember("last_user_text", "add a call tomorrow at 9")
    doc = db.one("SELECT start_at FROM events WHERE title LIKE '%doctor%'")["start_at"]
    r = tools.call("add_event", {"title": "Parent meeting", "start_at": doc.replace(" ", "T")})
    assert r["status"] == "clash"
    ok = tools.call("add_event", {"title": "Plumber", "start_at": "2030-01-01T05:00Z"})
    assert ok["status"] == "added"


def test_summary_family_call_and_state():
    tools.call("complete_task", {"task": "recycling"})
    s1 = tools.call("daily_summary", {})
    tools.call("daily_summary", {})
    assert "Done: Take out the recycling" in s1["summary"]
    assert len([n for n in db.q("SELECT message FROM notifications") if "Family update" in n["message"]]) == 1
    c = tools.call("call_family_member", {"member": "Rohan", "reason": "AC repair"})
    assert c["status"] == "link_sent" and "/expert.html?name=Rohan" in c["join_link"]
    db.remember("last_user_text", "living room AC shows E4")
    tools.call("get_appliance", {"appliance": "AC"})
    st = TestClient(app).get("/api/state").json()
    assert st["expert_called"] and st["insight"]["code"] == "E4" and st["appliance"]["id"] == "AC-LIVING"
    assert TestClient(app).get("/expert.html").status_code == 200


def test_bad_tool_args_do_not_crash():
    assert "error" in tools.call("get_appliance", {"nope": 1})
    assert "error" in tools.call("assign_task", {"title": "x", "assignee": "the neighbour"})
    assert "error" in tools.call("book_service", {"provider": "X", "slot_at": "tomorrow"})


def test_spoken_time_and_speech_cleanup():
    from datetime import datetime, timedelta
    d = datetime.now().replace(hour=15, minute=5) + timedelta(days=3)
    assert tools._spoken_time(d).endswith("at 3:05 PM")
    assert brain.speakable("rated 45/5 uF") == "rated 45 by 5 microfarad"


def test_alarms_set_dedupe_cancel_and_fire():
    from datetime import timedelta
    from backend import agora
    t = (db.now() + timedelta(hours=1, minutes=5)).replace(second=0)
    db.remember("last_user_text", "set an alarm for ten fifty pm")
    r = tools.call("set_reminder", {"text": "Alarm", "at": t.isoformat()})
    assert r["status"] == "scheduled" and r["rings_in"] in ("1 hour 4 minutes", "1 hour 5 minutes")
    # the same request repeated (Agora resends a growing sentence) does not add another
    again = tools.call("set_reminder", {"text": "Alarm", "at": (t + timedelta(minutes=1)).isoformat()})
    assert again["status"] == "already_set"
    tools.call("set_reminder", {"text": "BP tablet", "at": (t + timedelta(hours=2)).isoformat(), "member": "Dadi"})
    c = tools.call("cancel_reminder", {"which": t.strftime("%I:%M %p")})
    assert c["status"] == "cancelled" and c["count"] == 1 and c["still_scheduled"] == 1
    assert tools.call("cancel_reminder", {"which": "all"})["count"] == 1
    assert tools.call("cancel_reminder", {"which": "all"})["status"] == "nothing_matched"
    # a due reminder fires once: alarm activity + family channel message
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           ("Take BP tablet", "dadi", db.iso(db.now() - timedelta(seconds=5)), "scheduled", db.iso(db.now())))
    fired = tools.due_reminders()
    assert len(fired) == 1 and fired[0]["spoken"] == "Reminder for Dadi: Take BP tablet."
    assert tools.due_reminders() == []
    assert TestClient(app).get("/api/state").json()["fired"][0]["title"] == "Reminder for Dadi"
    td = agora.join_body("ch", 1)["properties"]["turn_detection"]["config"]["end_of_speech"]
    assert td["mode"] == "semantic" and td["semantic_config"]["pause_state_enabled"] is True


def test_guards_from_live_test():
    # shopping: synonyms and plurals are not added twice
    tools.call("add_to_shopping_list", {"items": ["Dadi's BP tablets", "milk"]})
    r = tools.call("add_to_shopping_list", {"items": ["Blood pressure tablets", "Milk"]})
    assert r["added"] == [] and len(r["already_on_list"]) == 2
    # tasks: a reworded repeat of the same chore is not a new task (and no new Slack message)
    tools.call("assign_task", {"title": "Pick up milk, paneer and BP tablet from FreshMart", "assignee": "Rohan"})
    n = len(db.q("SELECT id FROM notifications"))
    again = tools.call("assign_task", {"title": "Pick up milk, paneer, and blood pressure tablets on the way home", "assignee": "Rohan"})
    assert again["status"] == "already_assigned" and len(db.q("SELECT id FROM notifications")) == n
    # family channel: only when the user asked to message the family
    db.remember("last_user_text", "my number is nine six three")
    assert "error" in tools.call("send_family_update", {"message": "My correct number is 963."})
    db.remember("last_user_text", "tell the family dinner is at 8")
    assert tools.call("send_family_update", {"message": "Dinner is at 8."})["status"] == "sent"
    assert tools.call("send_family_update", {"message": "Dinner is at 8."})["status"] == "already_sent"
    # live call invites: family members only, no repeats
    assert "error" in tools.call("call_family_member", {"member": "963299527"})
    assert tools.call("call_family_member", {"member": "Matthew"})["status"] == "link_sent"  # guest by name
    assert tools.call("call_family_member", {"member": "Rohan"})["status"] == "link_sent"
    assert tools.call("call_family_member", {"member": "Rohan"})["status"] == "already_invited"


def test_tell_me_is_not_tell_the_family_and_memory():
    db.remember("last_user_text", "Then can you please tell me what is the number of Rohan?")
    assert "error" in tools.call("send_family_update", {"message": "Rohan's number is 963"})
    for said in ["tell the family dinner is at 8", "let everyone know I'm late",
                 "send the family an update", "message Rohan that the AC is fixed"]:
        db.remember("last_user_text", said)
        assert "error" not in tools.call("send_family_update", {"message": said})
    assert tools.call("remember_fact", {"fact": "Anika's birthday is 22 August 2005"})["status"] == "remembered"
    assert tools.call("remember_fact", {"fact": "Anika's birthday is 22 August 2005."})["status"] == "already_remembered"
    assert "birthday is 22 August 2005" in brain.live_context()


def test_near_miss_tool_name_is_corrected(monkeypatch):
    class Typo(FakeLLM):
        async def create(self, **kw):
            self.calls.append(kw["messages"])
            done = any(m.get("role") == "tool" for m in kw["messages"])
            async def gen():
                if not done:
                    yield _chunk(tool=("call_family_model", {"member": "Rohan"}))
                else:
                    yield _chunk("Rohan is invited.")
            return gen()
    monkeypatch.setattr(brain, "_targets", lambda: [(Typo(), "m")])
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "get Rohan"}]})
    assert r.json()["reply"].endswith("Rohan is invited.")
    assert db.q("SELECT * FROM activity WHERE kind='call'")


def test_alarm_ack_and_snooze():
    c = TestClient(app)
    assert c.post("/api/alarm/ack", json={"title": "Reminder for Dadi", "detail": "Take BP tablet", "how": "snooze"}).json()["ok"]
    r = db.one("SELECT * FROM reminders WHERE status='scheduled' AND member='dadi'")
    assert r and r["text"] == "Take BP tablet"
    c.post("/api/alarm/ack", json={"title": "Reminder for Dadi", "detail": "Take BP tablet", "how": "done"})
    assert db.one("SELECT * FROM activity WHERE title='Acknowledged: Dadi'")
    assert c.post("/api/speak", json={"text": ""}).json()["ok"] is False


def test_invite_guests_by_name_not_numbers_and_join_status():
    r = tools.call("call_family_member", {"member": "my friend Priya", "reason": "planning dinner"})
    assert r["status"] == "link_sent" and r["who"] == "Priya" and "name=Priya" in r["join_link"]
    assert "error" in tools.call("call_family_member", {"member": "9632995271"})
    assert "error" in tools.call("call_family_member", {"member": "Anika"})
    assert tools.call("call_family_member", {"member": "Kabir"})["status"] == "link_sent"
    c = TestClient(app)
    both = tools.call("call_family_member", {"member": "Rohan and my friend Meera"})
    assert both["invited"] == ["Rohan", "Meera"]
    c.post("/api/joined", json={"name": "Kabir"})
    st = c.get("/api/state").json()
    who = {i["who"]: i["joined"] for i in st["invites"]}
    assert who["Kabir"] is True and who["Rohan"] is False and "Priya" in who
    assert any(a["title"] == "Kabir joined the call" for a in st["activity"])


def test_history_ends_with_user_and_growing_sentences_merge():
    msgs = brain.build_messages([
        {"role": "assistant", "content": "Hi!"},
        {"role": "user", "content": "Yes."},
        {"role": "user", "content": "Yes. I'm good."},
        {"role": "user", "content": "Yes. I'm good. What about you?"},
        {"role": "assistant", "content": "I'm here."}])
    convo = msgs[2:]
    assert convo == [{"role": "user", "content": "Yes. I'm good. What about you?"}]


def test_unexpected_joiner_is_listed():
    c = TestClient(app)
    tools.call("call_family_member", {"member": "Rohan"})
    c.post("/api/joined", json={"name": "Lalit Kumar Singh"})
    who = {i["who"]: i["joined"] for i in c.get("/api/state").json()["invites"]}
    assert who == {"Rohan": False, "Lalit Kumar Singh": True}


def test_shopping_lifecycle_bought_auto_closes_pickup():
    tools.call("add_to_shopping_list", {"items": ["milk", "paneer"]})
    t = tools.call("assign_task", {"title": "Pick up milk and paneer", "assignee": "Rohan"})
    assert t["status"] == "assigned"
    assert "/list.html?name=Rohan" in db.q("SELECT message FROM notifications")[-1]["message"]
    st = tools.call("shopping_status", {})
    assert st["estimated_total_inr"] == 60 + 90 + 60  # milk + paneer + bananas (seeded)
    assert tools.call("remove_from_shopping_list", {"items": ["bananas"]})["removed"] == ["Bananas"]
    r = tools.call("mark_bought", {"items": ["milk"], "by": "Rohan"})
    assert r["bought"] == ["Milk"] and r["still_to_buy"] == ["Paneer"]
    # ticking the last item from the phone page closes Rohan's pickup task automatically
    pid = db.one("SELECT id FROM shopping WHERE item='Paneer'")["id"]
    out = TestClient(app).post("/api/shopping/toggle", json={"id": pid, "by": "Rohan"}).json()
    assert out.get("task_completed") == "Pick up milk and paneer"
    assert db.one("SELECT status FROM tasks WHERE title='Pick up milk and paneer'")["status"] == "done"


def test_grocery_order_needs_yes_avoids_calendar_and_closes_pickup():
    tools.call("add_to_shopping_list", {"items": ["milk", "paneer"]})
    tools.call("assign_task", {"title": "Pick up milk, paneer and bananas", "assignee": "Rohan"})
    p = tools.call("order_groceries", {})
    assert p["status"] == "awaiting_confirmation" and db.q("SELECT * FROM grocery_orders") == []
    db.remember("last_user_text", "yes order it")
    done = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert done["status"] == "done" and done["order_id"].startswith("GR-")
    order = db.one("SELECT * FROM grocery_orders")
    from datetime import datetime
    slot = datetime.fromisoformat(order["slot_at"])
    assert tools._clashes(slot, 30) == [e for e in tools._clashes(slot, 30) if "Grocery" in e["title"]]
    assert 8 <= slot.hour < 20
    assert db.q("SELECT * FROM shopping WHERE status='needed'") == []
    assert db.one("SELECT status FROM tasks WHERE title LIKE 'Pick up milk%'")["status"] == "done"
    assert "no need to stop at the shop" in db.q("SELECT message FROM notifications")[-1]["message"]
    again = tools.call("order_groceries", {})
    assert again.get("status") == "already_ordered" or "error" in again


def test_store_order_over_mcp_needs_yes_then_phone_approval(monkeypatch):
    from backend import config, store_server
    monkeypatch.setattr(config, "STORE_MCP_URL", "internal")  # same MCP protocol, in-process
    store_server.reset_store()
    hits = tools.call("store_search", {"query": "veg biryani"})["results"]
    assert hits[0]["name"] == "Veg Biryani" and hits[0]["price_inr"] == 139
    cart = tools.call("store_add_to_cart", {"items": [{"name": "kurkure", "qty": 2}, {"item_id": "F101"}]})["cart"]
    assert cart["total_inr"] == 2 * 20 + 139
    p = tools.call("store_checkout", {})
    assert p["status"] == "awaiting_confirmation" and "You'll approve the payment on your phone" in p["summary"]
    assert store_server.all_orders() == []                     # nothing ordered before "yes"
    db.remember("last_user_text", "yes place it")
    r = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert r["status"] == "payment_requested"
    st = tools.call("store_order_status", {})
    assert st["payment"] == "awaiting_approval" and st["status"] == "awaiting_payment"
    c = TestClient(app)
    assert c.post("/api/store/pay", json={"order": r["order_id"], "approve": True}).json()["status"] == "preparing"
    assert tools.call("store_order_status", {})["payment"] == "approved"
    # a declined payment orders nothing
    tools.call("store_add_to_cart", {"items": ["ghee"]})
    p2 = tools.call("store_checkout", {})
    r2 = tools.call("confirm_action", {"action_id": p2["action_id"], "approved": True})
    assert c.post("/api/store/pay", json={"order": r2["order_id"], "approve": False}).json()["payment"] == "declined"


# ---------------------------------------------------------------- calendar sync, missed reminders, spending

def test_calendar_feed_is_valid_ics_with_bookings():
    o = _ok_slot()
    p = tools.call("book_service", {"provider": o["provider"], "slot_at": o["slot_at"]})
    tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    r = TestClient(app).get("/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR") and body.rstrip().endswith("END:VCALENDAR")
    assert body.count("BEGIN:VEVENT") == body.count("END:VEVENT") >= 2
    assert "repair" in body.lower() and "TRIGGER:-PT30M" in body
    ev = TestClient(app).get("/api/state").json()["events"]
    assert all(e["gcal"].startswith("https://calendar.google.com/") for e in ev)


def test_unconfirmed_medicine_reminder_alerts_the_family(monkeypatch):
    from datetime import timedelta
    sent = []
    monkeypatch.setattr(tools, "_send_family", lambda m, urgent=False: sent.append(m) or "sent")
    past = db.iso(db.now() - timedelta(minutes=7))
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           ("take her BP tablet", "dadi", past, "scheduled", past))
    tools.due_reminders()
    out = tools.escalate_missed_reminders()
    assert len(out) == 1 and "Dadi hasn't confirmed" in out[0]["message"] and "BP tablet" in sent[-1]
    assert tools.escalate_missed_reminders() == []             # alerts once only


def test_got_it_stops_the_escalation():
    from datetime import timedelta
    past = db.iso(db.now() - timedelta(minutes=7))
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           ("take her BP tablet", "dadi", past, "scheduled", past))
    tools.due_reminders()
    TestClient(app).post("/api/alarm/ack", json={"title": "Reminder for Dadi",
                                                 "detail": "take her BP tablet", "how": "done"})
    assert tools.escalate_missed_reminders() == []


def test_voice_confirmation_marks_reminder_done():
    from datetime import timedelta
    past = db.iso(db.now() - timedelta(minutes=1))
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           ("take her BP tablet", "dadi", past, "scheduled", past))
    tools.due_reminders()
    assert tools.call("reminder_done", {"member": "Dadi", "what": "BP tablet"})["status"] == "confirmed"
    assert tools.escalate_missed_reminders() == []


def test_spending_summary_adds_repairs_and_orders(monkeypatch):
    from backend import config as cfg, store_server
    monkeypatch.setattr(cfg, "STORE_MCP_URL", "internal")
    store_server.reset_store()
    assert tools.call("spending_summary", {"period": "week"})["total_inr"] == 0
    o = _ok_slot()
    p = tools.call("book_service", {"provider": o["provider"], "slot_at": o["slot_at"]})
    tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    tools.call("store_add_to_cart", {"items": [{"item_id": "F101"}]})
    r = tools.call("confirm_action", {"action_id": tools.call("store_checkout", {})["action_id"], "approved": True})
    s = tools.call("spending_summary", {"period": "week"})
    fee = db.one("SELECT fee FROM bookings")["fee"]
    assert s["total_inr"] == fee                         # unpaid order not counted yet
    store_server.respond_to_payment(r["order_id"], True)
    s = tools.call("spending_summary", {"period": "week"})
    assert s["total_inr"] == fee + 139 and s["by_category"]["Food & delivery"] == 139
    assert TestClient(app).get("/api/state").json()["spent_week"] == s["total_inr"]


def test_changing_the_time_moves_the_event_instead_of_duplicating():
    from datetime import timedelta
    day = (db.now() + timedelta(days=1)).date().isoformat()
    assert tools.call("add_event", {"title": "Hackathon", "start_at": f"{day}T07:00", "member": "Anika"})["status"] == "added"
    r = tools.call("add_event", {"title": "Hackathon wake up", "start_at": f"{day}T06:00"})
    assert r["status"] == "moved"
    ev = db.q("SELECT * FROM events WHERE title LIKE 'Hackathon%'")
    assert len(ev) == 1 and ev[0]["start_at"].endswith("06:00:00") and ev[0]["member"] == "anika"
    assert tools.call("cancel_event", {"title": "hackathon"})["status"] == "removed"
    assert not db.q("SELECT * FROM events WHERE title LIKE 'Hackathon%'")


def test_calendar_on_phone_shows_qr_and_single_item_ics(monkeypatch):
    monkeypatch.setattr(tools, "_send_family", lambda m, urgent=False: "sent")
    from datetime import timedelta
    at = db.iso(db.now() + timedelta(hours=8))
    db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
           ("Wake up for Hackathon", "anika", at, "scheduled", at))
    c = TestClient(app)
    assert c.get("/api/state").json()["cal_qr"] is False
    assert tools.call("calendar_on_phone", {})["status"] == "qr_shown"
    st = c.get("/api/state").json()
    assert st["cal_qr"] is True and st["reminders"][0]["gcal"].startswith("https://calendar.google.com/")
    rid = st["reminders"][0]["id"]
    one = c.get(f"/calendar/reminder/{rid}.ics").text
    assert one.count("BEGIN:VEVENT") == 1 and "Wake up for Hackathon" in one and "TRIGGER:PT0M" in one
    ev = c.get(f"/calendar/event/{st['events'][0]['id']}.ics").text
    assert ev.count("BEGIN:VEVENT") == 1 and "BEGIN:VALARM" in ev
    assert c.get("/cal.html").status_code == 200


def test_yes_after_offer_to_post_counts_as_asking(monkeypatch):
    monkeypatch.setattr(tools, "_send_family", lambda m, urgent=False: "sent")
    db.remember("last_assistant_text", "Would you like me to post it to the family channel?")
    db.remember("last_user_text", "Yes. Sure.")
    assert tools.call("send_family_update", {"message": "Wake-up at 6 AM"})["status"] == "sent"
    db.remember("last_assistant_text", "Anything else?")
    db.remember("last_user_text", "Yes. Sure.")
    assert "error" in tools.call("send_family_update", {"message": "Another note"})


def test_older_request_stops_when_a_newer_one_arrives(monkeypatch):
    import asyncio

    async def slow_round(messages, rnd):
        for w in ["one ", "two ", "three "]:
            await asyncio.sleep(0.01)
            yield w

    monkeypatch.setattr(config, "LLM_API_KEY", "x")
    monkeypatch.setattr(brain, "_stream_round", slow_round)

    async def run():
        async def collect(text):
            return [p async for p in brain.run_turn([{"role": "user", "content": text}])]
        first = asyncio.create_task(collect("I would like"))
        await asyncio.sleep(0.015)
        second = await collect("I would like to add a Google Calendar")
        return await first, second

    old, new = asyncio.run(run())
    assert "".join(new).strip() == "one two three" and "three" not in "".join(old)
