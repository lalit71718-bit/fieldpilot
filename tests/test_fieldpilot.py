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


def test_rag_finds_e4_procedure():
    hits = rag.search("chiller error E4 high pressure what to check")
    assert hits and "E4" in hits[0]["text"]


def test_inventory_and_confirmed_order_flow():
    inv = tools.call("check_inventory", {"part": "fan capacitor"})["parts"][0]
    assert "Whitefield" in inv["out_of_stock_at"]
    assert inv["in_stock_at"][0]["warehouse"] == "HSR Layout" and "HSR Layout" in inv["recommendation"]
    bad = tools.call("request_part_order", {"ticket_id": "TK-1042", "sku": "CAP-45", "warehouse": "Whitefield"})
    assert "alternatives" in bad
    p = tools.call("request_part_order", {"ticket_id": "TK-1042", "sku": "CAP-45", "warehouse": "HSR Layout"})
    assert p["status"] == "awaiting_confirmation"
    assert db.q("SELECT * FROM orders") == []  # nothing happens before the yes
    done = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert done["status"] == "done"
    assert db.one("SELECT qty FROM stock WHERE sku='CAP-45' AND warehouse='HSR Layout'")["qty"] == 1
    assert db.one("SELECT status FROM tickets WHERE id='TK-1042'")["status"] == "waiting_for_parts"
    again = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert "error" in again


def test_resolve_needs_confirmation_and_cancel():
    p = tools.call("update_ticket", {"ticket_id": "TK-1043", "status": "resolved", "note": "topped up"})
    assert p["status"] == "awaiting_confirmation"
    tools.call("confirm_action", {"action_id": p["action_id"], "approved": False})
    assert db.one("SELECT status FROM tickets WHERE id='TK-1043'")["status"] == "open"


def test_bad_tool_args_do_not_crash():
    assert "error" in tools.call("get_ticket", {"nope": 1})
    assert "error" in tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "tomorrow", "purpose": "x"})


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
                yield _chunk(tool=("check_inventory", {"part": "CAP-45"}))
            else:
                yield _chunk("HSR Layout has two. ")
                yield _chunk("Shall I order one?")
        return gen()


def test_chat_completions_streams_sse_with_tools(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(brain, "_targets", lambda: [(fake, "m")])
    c = TestClient(app)
    body = {"model": "x", "stream": True, "messages": [
        {"role": "system", "content": "agora default prompt"},
        {"role": "user", "content": "Do we have the fan capacitor?"}]}
    assert c.post("/chat/completions", json=body).status_code == 401
    r = c.post("/chat/completions", json=body, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    lines = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lines[-1] == "[DONE]"
    text = "".join(json.loads(l)["choices"][0]["delta"].get("content", "") for l in lines[:-1])
    assert text == "One sec, checking stock. HSR Layout has two. Shall I order one?"
    # our system prompt + live context replaced Agora's, and the tool result went back to the LLM
    first = fake.calls[0]
    assert first[0]["content"].startswith("You are FieldPilot")
    assert all(m["content"] != "agora default prompt" for m in first)
    assert any(m.get("role") == "tool" and "HSR Layout" in m["content"] for m in fake.calls[1])
    kinds = [a["kind"] for a in db.q("SELECT kind FROM activity")]
    assert "user" in kinds and "agent" in kinds and "inventory" in kinds


def test_state_and_text_chat(monkeypatch):
    monkeypatch.setattr(brain, "_targets", lambda: [(FakeLLM(), "m")])
    c = TestClient(app)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "stock?"}]})
    assert r.json()["reply"].endswith("Shall I order one?")
    s = c.get("/api/state").json()
    assert {"tickets", "stock", "orders", "pending", "activity"} <= s.keys()
    assert c.get("/").status_code == 200


def test_agora_extra_fields_are_stripped():
    msgs = brain.build_messages([
        {"role": "user", "content": "hi", "turn_id": 1, "timestamp": 2},
        {"role": "assistant", "content": "Hello", "metadata": {"interrupted": False}},
        {"role": "user", "content": [{"type": "text", "text": "first job?"}]}])
    convo = msgs[2:]
    assert convo == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello"},
                     {"role": "user", "content": "first job?"}]


def test_no_means_no_even_if_model_approves():
    p = tools.call("notify_supervisor", {"message": "Need help", "urgent": True})
    assert p["status"] == "awaiting_confirmation"
    db.remember("last_user_text", "No. No. No.")
    r = tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})
    assert r["status"] == "cancelled"
    assert db.q("SELECT * FROM notifications") == []
    p = tools.call("notify_supervisor", {"message": "Need help", "urgent": True})
    db.remember("last_user_text", "Yes please send it")
    assert tools.call("confirm_action", {"action_id": p["action_id"], "approved": True})["status"] == "done"
    assert len(db.q("SELECT * FROM notifications")) == 1


def test_falls_back_when_model_invents_tool(monkeypatch):
    class Bad(FakeLLM):
        async def create(self, **kw):
            async def gen():
                yield _chunk(tool=("check inventory", {"part": "x"}))
            return gen()
    monkeypatch.setattr(brain, "_targets", lambda: [(Bad(), "weak"), (FakeLLM(), "good")])
    c = TestClient(app)
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "stock?"}]})
    assert r.json()["reply"].endswith("Shall I order one?")


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
                        NS(index=None, id="a", function=NS(name="get_my_tickets", arguments="{}")),
                        NS(index=None, id="b", function=NS(name="check_inventory",
                                                           arguments='{"part": "CAP-45"}'))]))])
                else:
                    yield _chunk("Two tools done.")
            return gen()
    fake = NoIndex()
    monkeypatch.setattr(brain, "_targets", lambda: [(fake, "gemini")])
    r = TestClient(app).post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
    assert r.json()["reply"].endswith("Two tools done.")
    assert len([m for m in fake.calls[1] if m.get("role") == "tool"]) == 2


def test_spoken_time_and_speech_cleanup():
    from datetime import datetime
    assert tools._spoken_time(datetime(2026, 11, 20, 15, 5)) == "Friday at 3:05 PM"
    assert tools._spoken_time(datetime(2026, 11, 20, 10, 0)) == "Friday at 10 AM"
    assert brain.speakable("rated 45/5 uF") == "rated 45 by 5 microfarad"


def test_repeated_requests_do_not_duplicate_actions():
    a = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T10:30", "purpose": "fit"})
    b = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T10:30:00", "purpose": "revisit"})
    assert a["status"] == "scheduled" and b["status"] == "already_scheduled"
    r1 = tools.call("generate_service_report", {"ticket_id": "TK-1042", "findings": "f", "actions_taken": "a"})
    r2 = tools.call("generate_service_report", {"ticket_id": "TK-1042", "findings": "f", "actions_taken": "a"})
    assert r2["report_id"] == r1["report_id"] and len(db.q("SELECT * FROM reports")) == 1


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


def test_no_double_orders():
    a = tools.call("request_part_order", {"ticket_id": "TK-1042", "sku": "CAP-45", "warehouse": "HSR Layout"})
    db.remember("last_user_text", "yes")
    tools.call("confirm_action", {"action_id": a["action_id"], "approved": True})
    again = tools.call("request_part_order", {"ticket_id": "TK-1042", "sku": "CAP-45", "warehouse": "HSR Layout"})
    assert again["status"] == "already_ordered" and len(db.q("SELECT * FROM orders")) == 1
    extra = tools.call("request_part_order", {"ticket_id": "TK-1042", "sku": "CAP-45",
                                              "warehouse": "HSR Layout", "additional": True})
    assert extra["status"] == "awaiting_confirmation"


def test_followup_is_moved_not_duplicated_and_utc_converted():
    from datetime import datetime, timezone
    tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T10:30", "purpose": "a"})
    r = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T12:00", "purpose": "b"})
    assert r["status"] == "rescheduled" and len(db.q("SELECT * FROM followups")) == 1
    utc = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T05:00Z", "purpose": "c"})
    expected = datetime(2030, 1, 1, 5, 0, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert utc["when"] == expected.isoformat(sep=" ")


def test_followup_needs_a_time_from_the_technician():
    db.remember("last_user_text", "Wrap it up please")
    r = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T05:30", "purpose": "x"})
    assert "error" in r
    db.remember("last_user_text", "I'll come back tomorrow at ten thirty")
    r = tools.call("schedule_followup", {"ticket_id": "TK-1042", "when": "2030-01-01T10:30", "purpose": "x"})
    assert r["status"] == "scheduled"


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
