"""HomePilot backend.

  POST /chat/completions   custom LLM endpoint that Agora Conversational AI calls (OpenAI format)
  GET  /api/config         what the browser needs to join the Agora channel
  POST /api/agent/start    start the Agora voice agent (Murf TTS + our brain)
  POST /api/agent/stop
  POST /api/chat           text fallback: same brain, no voice (dev + demo backup)
  GET  /api/state          live data for the dashboard
  POST /api/reset          reseed demo data
  GET  /                   web app
"""
import asyncio
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agora, brain, config, db, tools

async def _reminder_clock():
    """Every 10 s: fire due reminders. They show on the dashboard, go to the family channel,
    and HomePilot says them out loud in the live call (Agora speak API)."""
    while True:
        try:
            for r in tools.due_reminders():
                await agora.speak(r["spoken"])
        except Exception as e:  # never let the clock die
            db.log("error", "Reminder clock", str(e)[:200])
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_reminder_clock())
    yield
    task.cancel()


app = FastAPI(title="HomePilot", lifespan=lifespan)


def _check_key(authorization: str | None) -> None:
    if not config.BRAIN_API_KEY:
        return
    if (authorization or "").removeprefix("Bearer ").strip() != config.BRAIN_API_KEY:
        db.log("error", "Agora call rejected: BRAIN_API_KEY mismatch",
               "header missing" if not authorization else "key differs from .env")
        raise HTTPException(401, "Invalid API key")


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str | None = Header(None)):
    _check_key(authorization)
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model") or config.LLM_MODEL
    if body.get("stream", True):
        return StreamingResponse(brain.sse_stream(messages, model),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    text = "".join([p async for p in brain.run_turn(messages)])
    return JSONResponse({
        "id": "chatcmpl-fieldpilot", "object": "chat.completion", "created": int(time.time()),
        "model": model, "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": text}}]})


@app.get("/api/config")
def api_config():
    # Fresh uid per browser session, so a stale tab or earlier attempt never causes UID_CONFLICT.
    uid = random.randint(10_000, 99_999)
    return {"app_id": config.AGORA_APP_ID, "channel": config.AGORA_CHANNEL,
            "uid": uid, "token": agora.rtc_token(uid) or None,
            "missing": agora.missing_settings(), "llm_model": config.LLM_MODEL}


@app.post("/api/agent/start")
async def agent_start(request: Request):
    body = await request.json() if await request.body() else {}
    return await agora.start_agent(user_uid=int(body.get("uid") or agora.USER_UID))


@app.post("/api/agent/stop")
async def agent_stop():
    return await agora.stop_agent()


@app.post("/api/chat")
async def text_chat(request: Request):
    body = await request.json()
    try:
        text = "".join([p async for p in brain.run_turn(body.get("messages", []))])
    except Exception as e:
        db.log("error", "Brain error", str(e)[:300])
        raise HTTPException(502, f"LLM error: {e}")
    return {"reply": text.strip()}


@app.get("/api/state")
def state():
    active = db.recall("active_appliance")
    return {
        "members": db.q("SELECT * FROM members"),
        "household": db.q("SELECT * FROM household"),
        "events": db.q("SELECT * FROM events ORDER BY start_at"),
        "tasks": db.q("SELECT * FROM tasks ORDER BY status DESC, due_at"),
        "shopping": db.q("SELECT * FROM shopping WHERE status='needed' ORDER BY id"),
        "appliance": db.one("SELECT * FROM appliances WHERE id=?", (active,)) if active else None,
        "history": db.q("SELECT * FROM appliance_history WHERE appliance_id=? ORDER BY date DESC",
                        (active,)) if active else [],
        "bookings": db.q("SELECT * FROM bookings ORDER BY created_at DESC"),
        "reminders": db.q("SELECT * FROM reminders WHERE status='scheduled' ORDER BY remind_at"),
        "fired": db.q("SELECT * FROM activity WHERE kind='alarm' ORDER BY id DESC LIMIT 5"),
        "pending": db.q("SELECT * FROM pending_actions WHERE status='awaiting_confirmation'"),
        "notifications": db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 10"),
        "activity": db.q("SELECT * FROM activity WHERE kind NOT IN ('tool','llm') "
                         "ORDER BY id DESC LIMIT 120"),
        "insight": tools.fault_insight(active) if active else None,
        "expert_called": bool(db.recall("expert_call")),
        "call_active": bool(agora._state.get("agent_id")),
        "now": db.iso(db.now()),
    }


@app.post("/api/reset")
def reset():
    db.reset_db()
    db.log("system", "Demo data reset")
    return {"ok": True}


@app.get("/api/diag")
async def diag():
    """One-page health check. Shows no secrets, only whether each one is set."""
    import httpx

    out: dict = {"settings": {
        "LLM_BASE_URL": config.LLM_BASE_URL, "LLM_MODEL": config.LLM_MODEL,
        "LLM_FALLBACK_MODELS": config.LLM_FALLBACK_MODELS,
        "LLM_API_KEY_set": bool(config.LLM_API_KEY),
        "LLM_API_KEY_starts": config.LLM_API_KEY[:4],
        "LLM2_set": bool(config.LLM2_BASE_URL and config.LLM2_API_KEY),
        "PUBLIC_BASE_URL": config.PUBLIC_BASE_URL,
        "BRAIN_API_KEY_set": bool(config.BRAIN_API_KEY),
        "missing_for_voice": agora.missing_settings()}}
    # 1. Can we reach the LLM with our real tools?
    results = []
    for client, model in brain._targets():
        t = time.time()
        try:
            r = await client.chat.completions.create(
                model=model, max_tokens=60, tools=brain.tools.openai_tools(),
                messages=[{"role": "user", "content": "Say OK."}])
            results.append({"model": model, "ok": True, "secs": round(time.time() - t, 2),
                            "reply": (r.choices[0].message.content or "")[:60]})
        except Exception as e:
            results.append({"model": model, "ok": False, "error": brain._err(e)})
    out["llm"] = results
    # 2. Can the public tunnel reach this server?
    if config.PUBLIC_BASE_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{config.PUBLIC_BASE_URL}/healthz")
            out["tunnel"] = {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:
            out["tunnel"] = {"ok": False, "error": str(e)[:200]}
    out["llm_cooldowns_secs"] = {m: round(t - time.time()) for m, t in brain._cooldown.items()
                                 if t > time.time()}
    out["recent_llm_failures"] = db.q("SELECT ts, title, detail FROM activity WHERE kind='llm' "
                                      "ORDER BY id DESC LIMIT 5")
    out["recent_errors"] = db.q("SELECT ts, title, detail FROM activity WHERE kind='error' "
                                "ORDER BY id DESC LIMIT 5")
    return out


@app.post("/api/speak")
async def speak(request: Request):
    """Have the live agent say a short text (used to repeat a ringing alarm)."""
    body = await request.json()
    text = str(body.get("text", ""))[:300]
    return {"ok": await agora.speak(text) if text else False}


@app.post("/api/alarm/ack")
async def alarm_ack(request: Request):
    """'Got it' stops the alarm; 'Snooze' rings it again in 5 minutes."""
    from datetime import timedelta
    body = await request.json()
    title, detail, how = (str(body.get(k, "")) for k in ("title", "detail", "how"))
    who = title.removeprefix("Reminder for ").strip() or "Anika"
    if how == "snooze":
        m = db.one("SELECT id FROM members WHERE name=?", (who,))
        db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
               (detail, m["id"] if m else "anika", db.iso(db.now() + timedelta(minutes=5)),
                "scheduled", db.iso(db.now())))
        db.log("reminder", f"Snoozed 5 min: {who}", detail)
    else:
        db.log("reminder", f"{'Acknowledged' if how == 'done' else 'Missed'}: {who}", detail)
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="web")
