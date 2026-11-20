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

from openai import APIError, APIStatusError, AsyncOpenAI, RateLimitError

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
  with notify_supervisor urgent=true (it needs the technician's yes, like orders).
- You can order parts yourself with request_part_order; don't ask the supervisor to do it.
- "No", "don't", "cancel", "wait" mean NO: call confirm_action with approved=false.
- When the work is done, give a one-sentence recap of what was updated."""


def _client(base_url: str, api_key: str) -> AsyncOpenAI:
    # No silent SDK retries: on a rate limit we switch model instantly instead of waiting.
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "missing", max_retries=0, timeout=20)


def _targets() -> list[tuple[AsyncOpenAI, str]]:
    """Every (provider, model) we may use, in order. Free tiers have small per-model limits,
    so we spread load across models and, optionally, a second provider (LLM2_*)."""
    out = []
    c1 = _client(config.LLM_BASE_URL, config.LLM_API_KEY)
    for m in dict.fromkeys([config.LLM_MODEL, *config.LLM_FALLBACK_MODELS]):
        out.append((c1, m))
    if config.LLM2_BASE_URL and config.LLM2_API_KEY:
        c2 = _client(config.LLM2_BASE_URL, config.LLM2_API_KEY)
        out += [(c2, m) for m in config.LLM2_MODELS]
    return out


class _Round:
    def __init__(self):
        self.content = ""
        self.calls: dict[int, dict] = {}


async def _stream_round(messages, rnd: _Round) -> AsyncIterator[str]:
    """One LLM call. Falls through to the next model on rate limits, provider errors and
    invalid tool calls, as long as nothing has been spoken yet in this round."""
    last: Exception | None = None
    targets = _targets()
    for attempt in range(2):
        for client, model in targets:
            try:
                stream = await client.chat.completions.create(
                    model=model, messages=messages, tools=tools.openai_tools(),
                    tool_choice="auto", temperature=0.3, max_tokens=500, stream=True)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    d = chunk.choices[0].delta
                    if d.content:
                        rnd.content += d.content
                        yield d.content
                    for tc in d.tool_calls or []:
                        # Some providers (e.g. Gemini) omit `index`: a new id means a new call.
                        key = tc.index
                        if key is None:
                            ids = [c["id"] for c in rnd.calls.values()]
                            key = (len(rnd.calls) if (tc.id and tc.id not in ids) or not rnd.calls
                                   else max(rnd.calls))
                        slot = rnd.calls.setdefault(key, {"id": "", "name": "", "args": ""})
                        slot["id"] = tc.id or slot["id"]
                        if tc.function:
                            slot["name"] += tc.function.name or ""
                            slot["args"] += tc.function.arguments or ""
                bad = [c["name"] for c in rnd.calls.values() if c["name"] not in tools.TOOLS]
                if bad and not rnd.content:
                    raise ValueError(f"invented tool {bad}")
                return
            except (RateLimitError, APIStatusError, APIError, ValueError) as e:
                if rnd.content:  # already speaking: can't switch model mid-sentence
                    raise
                last = e
                rnd.calls.clear()
                reason = "rate-limited" if isinstance(e, RateLimitError) else "failed"
                db.log("system", f"{model} {reason}, trying next model", str(e)[:160])
        if attempt == 0:
            await asyncio.sleep(3)  # everything throttled: wait once for the per-minute window
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


ERROR_MARKERS = ("hit a problem", "lost connection", "connectivity", "glitch", "rate limit",
                 "too many steps", "language model key")


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
    # Drop our own error/apology turns so one failure doesn't derail the rest of the call.
    convo = [m for m in convo if m["content"] and not (
        m["role"] == "assistant" and any(k in m["content"].lower() for k in ERROR_MARKERS))]
    convo = convo[-MAX_HISTORY:]
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

    db.remember("last_user_text", user_text)
    messages = build_messages(incoming)
    said_filler = False
    spoken: list[str] = []
    for _ in range(MAX_TOOL_ROUNDS):
        rnd = _Round()
        async for piece in _stream_round(messages, rnd):
            piece = speakable(piece)
            spoken.append(piece)
            yield piece
        content, calls = rnd.content, rnd.calls
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
