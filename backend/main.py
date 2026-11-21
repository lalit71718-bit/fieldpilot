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
import json
import random
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agora, brain, config, db, store_server, tools

async def _order_watcher():
    """Every 5 s: follow store orders over MCP and announce changes (paid, on the way…)."""
    words = {"preparing": "Payment approved. {items} is being prepared, arriving around {t}.",
             "out_for_delivery": "Your order {id} is out for delivery.",
             "delivered": "Your order {id} has been delivered. Enjoy!",
             "payment_declined": "The payment for order {id} was declined, so nothing was ordered."}
    while True:
        try:
            orders = json.loads(db.recall("store_orders") or "[]")
            changed = False
            for o in orders:
                if o["last"] in ("delivered", "payment_declined"):
                    continue
                st = await asyncio.to_thread(tools._store, "order_status", {"order_id": o["id"]})
                new = "payment_declined" if st.get("payment") == "declined" else st.get("status")
                if new and new != o["last"] and new in words:
                    o["last"] = new; changed = True
                    t = (st.get("arrives_at") or "")[11:16]
                    msg = words[new].format(items=", ".join(st.get("items", [])), t=t, id=o["id"])
                    db.log("order", f"Order {o['id']}: {new.replace('_', ' ')}", msg)
                    await agora.speak(msg)
            if changed:
                db.remember("store_orders", json.dumps(orders))
        except Exception as e:
            db.log("error", "Order watcher", str(e)[:200])
        await asyncio.sleep(5)


async def _reminder_clock():
    """Every 10 s: fire due reminders. They show on the dashboard, go to the family channel,
    and HomePilot says them out loud in the live call (Agora speak API)."""
    while True:
        try:
            for r in tools.due_reminders():
                await agora.speak(r["spoken"])
            for e in tools.escalate_missed_reminders():  # nobody pressed "Got it"
                await agora.speak(e["message"])
        except Exception as e:  # never let the clock die
            db.log("error", "Reminder clock", str(e)[:200])
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(_reminder_clock()), asyncio.create_task(_order_watcher())]
    async with store_server.mcp.session_manager.run():  # the store's MCP endpoint at /store/mcp
        yield
    for t in tasks:
        t.cancel()


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
        "events": [dict(e, gcal=tools.calendar_links(e["title"], e["start_at"], e["minutes"] or 60)["google"])
                   for e in db.q("SELECT * FROM events ORDER BY start_at")],
        "tasks": db.q("SELECT * FROM tasks ORDER BY status DESC, due_at"),
        "shopping": db.q("SELECT * FROM shopping WHERE status IN ('needed','bought','ordered') "
                         "ORDER BY status DESC, id"),
        "shopping_total": tools.shopping_status()["estimated_total_inr"],
        "grocery_orders": db.q("SELECT * FROM grocery_orders ORDER BY created_at DESC"),
        "store_orders": store_server.all_orders(),
        "spent_week": tools.spending_summary("week")["total_inr"] if db.q(
            "SELECT id FROM bookings UNION SELECT id FROM grocery_orders") or db.recall("store_orders") else 0,
        "store_cart": store_server._cart(tools.CART_ID),
        "appliance": db.one("SELECT * FROM appliances WHERE id=?", (active,)) if active else None,
        "history": db.q("SELECT * FROM appliance_history WHERE appliance_id=? ORDER BY date DESC",
                        (active,)) if active else [],
        "bookings": db.q("SELECT * FROM bookings ORDER BY created_at DESC"),
        "reminders": [dict(r, gcal=tools.calendar_links("⏰ " + r["text"], r["remind_at"], 10)["google"])
                      for r in db.q("SELECT * FROM reminders WHERE status='scheduled' ORDER BY remind_at")],
        "cal_qr": _recent(db.recall("show_cal_qr"), 15),
        "devices": tools.devices(),
        "fired": db.q("SELECT * FROM activity WHERE kind='alarm' ORDER BY id DESC LIMIT 5"),
        "pending": db.q("SELECT * FROM pending_actions WHERE status='awaiting_confirmation'"),
        "notifications": db.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 10"),
        "activity": db.q("SELECT * FROM activity WHERE kind NOT IN ('tool','llm') "
                         "ORDER BY id DESC LIMIT 120"),
        "insight": tools.fault_insight(active) if active else None,
        "expert_called": bool(db.recall("expert_call")),
        "call_active": bool(agora._state.get("agent_id")),
        "invites": json.loads(db.recall("invites") or "[]"),
        "now": db.iso(db.now()),
    }


@app.post("/api/reset")
def reset():
    db.reset_db()
    store_server.reset_store()
    db.log("system", "Demo data reset")
    return {"ok": True}


@app.post("/api/iot/event")
async def iot_event(request: Request):
    """Webhook for connected appliances (or the dashboard's simulator):
    {"device": "AC-LIVING", "status": {"error": "E4", "outdoor_fan_rpm": 0}}"""
    body = await request.json()
    r = tools.iot_event(str(body.get("device", "")), dict(body.get("status") or {}))
    if r.get("spoken"):
        await agora.speak(r["spoken"])   # said out loud in the live call, if one is running
    return r


@app.post("/api/demo/step")
async def demo_step(request: Request):
    """Demo mode: run one scripted step of the family story through the real tools."""
    from . import demo
    n = int((await request.json()).get("step", 0))
    return await asyncio.to_thread(demo.run_step, n)


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
    m = db.one("SELECT id FROM members WHERE name=?", (who,))
    if how in ("done", "snooze"):
        tools.acknowledge_reminder(m["id"] if m else "anika", detail)
    if how == "snooze":
        db.run("INSERT INTO reminders (text,member,remind_at,status,created_at) VALUES (?,?,?,?,?)",
               (detail, m["id"] if m else "anika", db.iso(db.now() + timedelta(minutes=5)),
                "scheduled", db.iso(db.now())))
        db.log("reminder", f"Snoozed 5 min: {who}", detail)
    else:
        db.log("reminder", f"{'Acknowledged' if how == 'done' else 'Missed'}: {who}", detail)
    return {"ok": True}


@app.post("/api/joined")
async def joined(request: Request):
    """The join page reports who joined or left, so the dashboard can show it."""
    body = await request.json()
    name = str(body.get("name") or "A family member")[:40]
    left = bool(body.get("left"))
    invites = json.loads(db.recall("invites") or "[]")
    match = [inv for inv in invites if inv["who"].lower() == name.lower()]
    if match:
        for inv in match:
            inv["joined"] = not left
    elif not left:  # someone joined from the QR / link under another name: show them too
        invites.append({"who": name, "link": "", "at": db.iso(db.now()), "joined": True,
                        "guest": True})
    db.remember("invites", json.dumps(invites[-8:]))
    db.log("call", f"{name} {'left' if left else 'joined'} the call")
    return {"ok": True}


@app.post("/api/shopping/toggle")
async def shopping_toggle(request: Request):
    """Tick or untick an item from the phone list or the dashboard."""
    body = await request.json()
    row = db.one("SELECT * FROM shopping WHERE id=?", (int(body.get("id", 0)),))
    if not row or row["status"] not in ("needed", "bought"):
        raise HTTPException(404, "No such item")
    by = str(body.get("by") or "")[:30]
    if row["status"] == "needed":
        return tools.mark_bought([row["item"]], by)
    db.run("UPDATE shopping SET status='needed', bought_by=NULL, bought_at=NULL WHERE id=?", (row["id"],))
    db.log("shopping", "Back on the list", row["item"])
    return {"status": "unticked", "item": row["item"]}


@app.get("/api/store/order/{order_id}")
def store_order(order_id: str):
    """Used by the phone payment page."""
    for o in store_server.all_orders():
        if o["order_id"] == order_id.upper():
            return o
    raise HTTPException(404, "No such order")


@app.post("/api/store/pay")
async def store_pay(request: Request):
    """The customer approves or declines the payment request on their phone (demo of the
    UPI-app step; no PIN or bank details are ever sent here)."""
    body = await request.json()
    r = store_server.respond_to_payment(str(body.get("order", "")), bool(body.get("approve")))
    if r.get("error"):
        raise HTTPException(404, r["error"])
    db.log("order", f"Payment {'approved' if body.get('approve') else 'declined'} on phone",
           r["order_id"])
    return r


def _recent(stamp: str, minutes: int) -> bool:
    from datetime import datetime as _dt, timedelta as _td
    try:
        return bool(stamp) and _dt.fromisoformat(stamp) >= db.now() - _td(minutes=minutes)
    except ValueError:
        return False


@app.post("/api/calendar/qr")
def calendar_qr(show: bool = True):
    db.remember("show_cal_qr", db.iso(db.now()) if show else "")
    return {"ok": True}


def _ics_escape(t: str) -> str:
    return t.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.get("/calendar/{kind}/{item_id}.ics")
def calendar_item(kind: str, item_id: int):
    """One event or alarm as an .ics file: tapping it on a phone adds it to the phone's
    calendar with an alert (works on iPhone and Android without any account linking)."""
    return _ics((kind, item_id))


@app.get("/calendar.ics")
def calendar_feed():
    return _ics(None)


def _ics(only):
    """The family calendar as a live iCalendar feed. Subscribe once from Google Calendar
    ('From URL') or the phone's calendar (webcal://) and HomePilot's bookings, deliveries and
    appointments show up there, with alarms."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from fastapi.responses import Response
    fmt = lambda d: d.astimezone().astimezone(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
    now = fmt(db.now())
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//HomePilot//Family calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:HomePilot · Family",
             "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M"]
    names = {m["id"]: m["name"] for m in db.q("SELECT id, name FROM members")}
    for e in db.q("SELECT * FROM events"):
        if only and only != ("event", e["id"]):
            continue
        st = _dt.fromisoformat(e["start_at"])
        lines += ["BEGIN:VEVENT", f"UID:homepilot-event-{e['id']}@homepilot", f"DTSTAMP:{now}",
                  f"DTSTART:{fmt(st)}", f"DTEND:{fmt(st + _td(minutes=e['minutes'] or 60))}",
                  f"SUMMARY:{_ics_escape(e['title'])}",
                  f"DESCRIPTION:{_ics_escape('For ' + names.get(e['member'], 'the family') + ' · added by HomePilot')}"]
        if e["location"]:
            lines.append(f"LOCATION:{_ics_escape(e['location'])}")
        if only or (e["source"] or "").startswith(("booking", "order")):
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(e['title'])}",
                      "TRIGGER:-PT30M", "END:VALARM"]
        lines.append("END:VEVENT")
    for r in db.q("SELECT * FROM reminders WHERE status='scheduled'"):
        if only and only != ("reminder", r["id"]):
            continue
        st = _dt.fromisoformat(r["remind_at"])
        lines += ["BEGIN:VEVENT", f"UID:homepilot-reminder-{r['id']}@homepilot", f"DTSTAMP:{now}",
                  f"DTSTART:{fmt(st)}", f"DTEND:{fmt(st + _td(minutes=10))}",
                  f"SUMMARY:{_ics_escape('⏰ ' + names.get(r['member'], 'Anika') + ': ' + r['text'])}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(r['text'])}",
                  "TRIGGER:PT0M", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": "inline; filename=" + (
                        f"homepilot-{only[0]}-{only[1]}.ics" if only else "homepilot-family.ics")})


@app.get("/healthz")
def health():
    return {"ok": True}


# The demo store's MCP server (what Swiggy / Zomato expose) at /store/mcp
app.mount("/store", store_server.mcp.streamable_http_app(streamable_http_path="/mcp",
                                                         stateless_http=True, json_response=True))
app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="web")
