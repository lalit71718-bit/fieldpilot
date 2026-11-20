"""The agent's brain.

Agora Conversational AI sends every user turn here in OpenAI Chat Completions format
(our /chat/completions endpoint). We:
  1. replace Agora's system message with our own prompt + live context (memory),
  2. run the LLM with tools; while it asks for tools we execute them,
  3. stream the final answer back as OpenAI-style SSE chunks, which Agora speaks with Murf.

While tools run we immediately stream a short filler ("One sec, checking stock.") so the
technician never hears dead air."""
import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator

from openai import APIStatusError, AsyncOpenAI, RateLimitError

from . import config, db, tools

MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 16

SYSTEM_PROMPT = """You are FieldPilot, a hands-free voice copilot for field service technicians.
The technician is on site, hands busy, talking to you through a headset. Everything you write is
spoken aloud by a text-to-speech voice.

VOICE RULES
- Speak in short, natural sentences. Usually 1 to 3 sentences. No markdown, lists, emojis or
  symbols. Say numbers the way people say them ("forty-five microfarad", "twelve hundred rupees").
- During troubleshooting give ONE step at a time, then wait for the technician's result.
- Always put safety first (isolate power, discharge capacitors) when the step needs it.

HOW YOU WORK
- Use tools for facts. Never invent ticket data, stock, prices or procedures. For any error code,
  specification or procedure, call search_manual and base your answer on it.
- At the start of a conversation, if you don't know the ticket yet, call get_my_tickets.
- When a ticket is discussed, call get_ticket and use the maintenance history: point out repeat
  faults (for example the same error and part flagged before).
- If a part is needed: check_inventory, choose the fastest available warehouse, then
  request_part_order. If the nearest warehouse is out of stock, say so and offer the best
  alternative.
- Critical actions (orders, resolving tickets) return status awaiting_confirmation. Read the
  summary and ask a clear yes/no question. Only after the technician clearly agrees call
  confirm_action with approved=true. If they say no or change their mind, approved=false.
  If LIVE CONTEXT shows a pending confirmation and the technician says yes / go ahead / order
  it, call confirm_action with that id directly. Do not prepare the same action again.
- After real actions, keep systems in sync with a short ticket note.
- WRAP-UP: when the technician is finishing, do all that apply, each with its own tool call:
  schedule_followup (if a revisit time is mentioned or a part is on order),
  generate_service_report, notify_supervisor (one-line status). Then recap in one sentence.
- If a tool returns an error, explain it briefly and propose the next best option.
- Never say something was done (ordered, updated, sent, notified, booked) unless a tool
  returned success for it in this conversation. If the technician asks for several actions,
  call a tool for each one.
- When you call tools, do not also write an answer in the same message; answer after the
  results arrive. Use plain words only: write "microfarad", "degrees", "rupees", no symbols.
- Safety risk, customer escalation or something beyond the technician's skill: offer to escalate
  with notify_supervisor urgent=true.
- When the work is done, give a one-sentence recap of what was updated."""


def _client() -> AsyncOpenAI:
    # No silent SDK retries: on a rate limit we switch model instantly instead of waiting.
    return AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "missing",
                       max_retries=0, timeout=20)


def _models() -> list[str]:
    return [config.LLM_MODEL] + [m for m in config.LLM_FALLBACK_MODELS if m != config.LLM_MODEL]


async def _create(client, messages):
    """Call the first model that is not rate-limited (free tiers have small per-model limits)."""
    last: Exception | None = None
    for attempt in range(2):
        for model in _models():
            try:
                return await client.chat.completions.create(
                    model=model, messages=messages, tools=tools.openai_tools(),
                    tool_choice="auto", temperature=0.3, max_tokens=500, stream=True)
            except RateLimitError as e:
                last = e
                db.log("system", f"{model} rate-limited, trying next model")
            except APIStatusError as e:
                if e.status_code not in (404, 503):
                    raise
                last = e
        if attempt == 0:
            await asyncio.sleep(4)  # every model throttled: wait once for the per-minute window
    raise last or RuntimeError("No LLM model available")


_SPOKEN = [(re.compile(p), r) for p, r in [
    (r"\s?[µμ]F", " microfarad"), (r"[µμ]", " micro"), (r"\s?°\s?C", " degrees"), (r"₹\s?", "rupees "),
    (r"[‐‑‒–—]", "-"), (r"[*#`_]+", ""), (r"\s+-\s+", ", ")]]


def speakable(text: str) -> str:
    for pat, rep in _SPOKEN:
        text = pat.sub(rep, text)
    return text


def live_context() -> str:
    """Memory injected into every turn: who, what, where, what is pending."""
    tech = db.one("SELECT * FROM technicians WHERE id=?", (config.DEFAULT_TECHNICIAN_ID,)) or {}
    active = db.recall("active_ticket")
    lines = [f"Current local time: {db.now().strftime('%A %d %B %Y, %H:%M')} (Asia/Kolkata).",
             f"Technician: {tech.get('name')} ({tech.get('id')}), region {tech.get('region')}, "
             f"nearest warehouse {tech.get('base_warehouse')}, supervisor "
             f"{tech.get('supervisor_name')}. Preferences: {tech.get('preferences')}."]
    if active:
        t = db.one("SELECT id, title, status, notes FROM tickets WHERE id=?", (active,))
        if t:
            lines.append(f"Active ticket: {t['id']} '{t['title']}', status {t['status']}."
                         + (f" Notes so far: {t['notes'].strip()}" if t["notes"] else ""))
    pending = db.q("SELECT id, summary FROM pending_actions WHERE status='awaiting_confirmation' "
                   "ORDER BY created_at DESC LIMIT 3")
    for p in pending:
        lines.append(f"Pending confirmation {p['id']}: {p['summary']}")
    orders = db.q("SELECT id, sku, warehouse, eta FROM orders ORDER BY created_at DESC LIMIT 3")
    for o in orders:
        lines.append(f"Placed order {o['id']}: {o['sku']} from {o['warehouse']}, ETA {o['eta']}.")
    return "\n".join(lines)


def _text(content) -> str:
    """Agora/OpenAI content can be a string or a list of parts; keep only the text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    return ""


def build_messages(incoming: list[dict]) -> list[dict]:
    # Rebuild each message with only role + text: Agora adds extra fields (e.g. "metadata",
    # turn_id) that strict providers like Groq reject.
    convo = [{"role": m["role"], "content": _text(m.get("content"))}
             for m in incoming if m.get("role") in ("user", "assistant")]
    convo = [m for m in convo if m["content"]][-MAX_HISTORY:]
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "LIVE CONTEXT (from enterprise systems):\n"
                                          + live_context()}] + convo


FILLERS = {
    "check_inventory": "One sec, checking stock.",
    "search_manual": "Let me check the manual.",
    "get_ticket": "Pulling up the ticket.",
    "get_my_tickets": "Checking your tickets.",
    "request_part_order": "Preparing the order.",
    "confirm_action": "Done, give me a second.",
    "generate_service_report": "Writing the report now.",
    "notify_supervisor": "Messaging your supervisor.",
    "schedule_followup": "Booking that.",
    "update_ticket": "Updating the ticket.",
}


async def run_turn(incoming: list[dict]) -> AsyncIterator[str]:
    """Yields text pieces of the spoken reply."""
    if not config.LLM_API_KEY:
        yield "My language model key is not configured yet. Please set LLM_API_KEY."
        return
    user_text = next((_text(m.get("content")) for m in reversed(incoming)
                      if m.get("role") == "user"), "")
    if user_text:
        db.log("user", "Technician", user_text)

    messages = build_messages(incoming)
    client = _client()
    said_filler = False
    spoken: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        stream = await _create(client, messages)
        content, calls = "", {}
        async for chunk in stream:
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if d.content:
                content += d.content
                piece = speakable(d.content)
                spoken.append(piece)
                yield piece
            for tc in d.tool_calls or []:
                slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                slot["id"] = tc.id or slot["id"]
                if tc.function:
                    slot["name"] += tc.function.name or ""
                    slot["args"] += tc.function.arguments or ""
        if not calls:
            break
        if not said_filler and not content:
            filler = FILLERS.get(calls[min(calls)]["name"], "One moment.")
            spoken.append(filler + " ")
            yield filler + " "
            said_filler = True
        messages.append({"role": "assistant", "content": content or None, "tool_calls": [
            {"id": c["id"] or f"call_{i}", "type": "function",
             "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
            for i, c in sorted(calls.items())]})
        for i, c in sorted(calls.items()):
            result = tools.call(c["name"], c["args"])
            db.log("tool", c["name"], json.dumps(result)[:400])
            messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                             "content": json.dumps(result, default=str)})
    else:
        msg = "Sorry, that took too many steps. Can you say that again more simply?"
        spoken.append(msg)
        yield msg
    db.log("agent", "FieldPilot", "".join(spoken).strip())


def sse_chunk(cid: str, model: str, delta: dict, finish: str | None = None) -> str:
    payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
               "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}\n\n"


async def sse_stream(incoming: list[dict], model: str) -> AsyncIterator[str]:
    cid = "chatcmpl-" + uuid.uuid4().hex[:12]
    yield sse_chunk(cid, model, {"role": "assistant", "content": ""})
    try:
        async for piece in run_turn(incoming):
            yield sse_chunk(cid, model, {"content": piece})
    except Exception as e:  # speak a graceful error instead of going silent
        db.log("error", "Brain error", str(e)[:300])
        yield sse_chunk(cid, model, {"content": "Sorry, I hit a problem reaching our systems. "
                                                "Please try again."})
    yield sse_chunk(cid, model, {}, "stop")
    yield "data: [DONE]\n\n"
