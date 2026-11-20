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
    assert [m["content"] for m in msgs[2:]] == ["first job?", "try again"]


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
    assert c["status"] == "invited" and c["join_link"].endswith("/expert.html")
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
