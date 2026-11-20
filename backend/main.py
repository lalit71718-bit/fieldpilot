"""FieldPilot backend.

  POST /chat/completions   custom LLM endpoint that Agora Conversational AI calls (OpenAI format)
  GET  /api/config         what the browser needs to join the Agora channel
  POST /api/agent/start    start the Agora voice agent (Murf TTS + our brain)
  POST /api/agent/stop
  POST /api/chat           text fallback: same brain, no voice (dev + demo backup)
  GET  /api/state          live data for the dashboard
  POST /api/reset          reseed demo data
  GET  /                   web app
"""
import random
import time

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agora, brain, config, db

app = FastAPI(title="FieldPilot")


def _check_key(authorization: str | None) -> None:
    if not config.BRAIN_API_KEY:
        return
    if (authorization or "").removeprefix("Bearer ").strip() != config.BRAIN_API_KEY:
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
    return {
        "technician": db.one("SELECT id, name, region, supervisor_name FROM technicians"),
        "active_ticket": db.recall("active_ticket"),
        "tickets": db.q("SELECT t.*, a.name AS asset_name, a.model, a.site FROM tickets t "
                        "JOIN assets a ON a.id=t.asset_id ORDER BY t.id"),
        "stock": db.q("SELECT s.sku, p.name, s.warehouse, s.qty FROM stock s "
                      "JOIN parts p ON p.sku=s.sku ORDER BY s.sku, s.eta_hours"),
        "orders": db.q("SELECT * FROM orders ORDER BY created_at DESC"),
        "pending": db.q("SELECT * FROM pending_actions WHERE status='awaiting_confirmation'"),
        "notifications": db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 10"),
        "followups": db.q("SELECT * FROM followups ORDER BY when_at"),
        "reports": db.q("SELECT * FROM reports ORDER BY created_at DESC"),
        "activity": db.q("SELECT * FROM activity ORDER BY id DESC LIMIT 60"),
    }


@app.post("/api/reset")
def reset():
    db.reset_db()
    db.log("system", "Demo data reset")
    return {"ok": True}


@app.get("/healthz")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="web")
