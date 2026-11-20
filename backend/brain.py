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

from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from . import config, db, tools

MAX_TOOL_ROUNDS = 10
MAX_HISTORY = 10

SYSTEM_PROMPT = """You are FieldPilot, a hands-free voice copilot for a field technician on site.
Everything you write is spoken aloud.

VOICE
- Max 3 short sentences. No lists, numbering, symbols or markdown. Give only the facts that
  matter now. Troubleshooting: one step at a time, safety first, then wait for the result.

FACTS
- Use tools for every fact; never invent tickets, history, stock, prices or procedures.
- Unknown job: get_my_tickets. Ticket discussed: get_ticket (point out repeat faults in its
  history). Error codes, specs, procedures: search_manual.
- Part needed: check_inventory, follow its "recommendation", then request_part_order.

ACTIONS
- Orders, resolving tickets and urgent escalations return awaiting_confirmation: nothing has
  happened yet. Ask a clear yes/no question. On yes call confirm_action(approved=true) with the
  pending id from LIVE CONTEXT; on no / don't / wait / cancel use approved=false.
- Never say something was done unless a tool returned success. One tool call per requested
  action. Don't write an answer in the same message as tool calls.
- An order that is done is done: thanks / okay are not new orders. confirm_action already
  updates the ticket, so don't add a note for the same order.
- Wrap-up: schedule_followup (ask the time if not given), generate_service_report,
  notify_supervisor. Then a one-sentence recap.
- get_ticket may return an "insight" (repeat fault): say it first, in one sentence ("This is the
  third E4 on this unit..."), then still guide the first safe check before suggesting an order. Technician stuck or asks for an expert: call_expert.
- Tool error: say it briefly and offer the next best option. Safety risk or beyond skill:
  offer notify_supervisor with urgent=true."""


def _client(base_url: str, api_key: str) -> AsyncOpenAI:
    # No silent SDK retries: on a rate limit we switch model instantly instead of waiting.
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "missing", max_retries=0, timeout=8)


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


def _err(e: Exception) -> str:
    """Full provider error text (Gemini's error bodies are not parsed by the SDK)."""
    resp = getattr(e, "response", None)
    detail = ""
    if resp is not None:
        try:
            detail = resp.text
        except Exception:
            detail = ""
    return f"{e} {detail}".strip()[:400]


_cooldown: dict[str, float] = {}  # model -> time until which we skip it


def _cool(model: str, e: Exception) -> None:
    """Skip models that are gone (404) or throttled (429) instead of re-trying every turn."""
    text = _err(e)
    if getattr(e, "status_code", None) == 404:
        _cooldown[model] = time.time() + 3600
    elif isinstance(e, APITimeoutError):
        _cooldown[model] = time.time() + 300  # hanging model: voice can't wait for it
    elif isinstance(e, RateLimitError):
        m = re.search(r"try again in ([\d.]+)(ms|s)", text)
        wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 20
        if "free_tier_requests" in text or "per day" in text.lower():
            wait = 3600  # daily quota gone
        _cooldown[model] = time.time() + min(wait + 1, 3600)


# Gemini 3 requires a "thought signature" on every replayed function call. Calls made by
# another provider (fallback mid-turn) have none, so Google's documented placeholder is used.
DUMMY_SIGNATURE = "skip_thought_signature_validator"


def _for_provider(messages: list[dict], client) -> list[dict]:
    gemini = "googleapis" in str(getattr(client, "base_url", ""))
    out = []
    for m in messages:
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                tc = {k: v for k, v in tc.items() if k != "extra_content"}
                if gemini:
                    extra = next((o.get("extra_content") for o in m["tool_calls"]
                                  if o["id"] == tc["id"] and o.get("extra_content")), None)
                    tc["extra_content"] = extra or {
                        "google": {"thought_signature": DUMMY_SIGNATURE}}
                calls.append(tc)
            m = {**m, "tool_calls": calls}
        out.append(m)
    return out


class _Round:
    def __init__(self):
        self.content = ""
        self.calls: dict[int, dict] = {}


async def _stream_round(messages, rnd: _Round) -> AsyncIterator[str]:
    """One LLM call. Falls through to the next model on rate limits, provider errors and
    invalid tool calls, as long as nothing has been spoken yet in this round."""
    last: Exception | None = None
    all_targets = _targets()
    for attempt in range(2):
        targets = [t for t in all_targets if _cooldown.get(t[1], 0) < time.time()] or all_targets
        for client, model in targets:
            try:
                stream = await client.chat.completions.create(
                    model=model, messages=_for_provider(messages, client), tools=tools.openai_tools(),
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
                        extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
                        if extra:
                            slot["extra"] = extra
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
                _cool(model, e)
                db.log("llm", f"{model} {reason}, trying next model", _err(e))
        if attempt == 0:
            await asyncio.sleep(3)  # everything throttled: wait once for the per-minute window
    raise last or RuntimeError("No LLM model available")


_SPOKEN = [(re.compile(p), r) for p, r in [
    (r"\s?[µμ]F\b", " microfarad"), (r"\s?\bmicroF\b", " microfarad"), (r"[µμ]", " micro"), (r"\s?°\s?C", " degrees"), (r"₹\s?", "rupees "),
    (r"(?m)(^|\s)\d{1,2}[.)]\s+", r"\1"), (r"(?i)\s?\((?:yes/no|y/n)\)", ""), (r"\b(\d+)\s?x\b", r"\1"),
    (r"\bhrs?\b", "hours"), (r":\s*,", ","), (r"\s?\buF\b", " microfarad"), (r"(\d)/(\d)", r"\1 by \2"), (r"[‐‑‒–—]", "-"), (r"_", " "), (r"[*#`]+", ""), (r"\s+-\s+", ", "), (r" {2,}", " ")]]


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
    "call_expert": "Calling a senior engineer into this call.",
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
        buf = ""  # clean speech on whole words: units like "uF" can arrive split across chunks
        async for piece in _stream_round(messages, rnd):
            buf += piece
            cut = max(buf.rfind(" "), buf.rfind("\n"))
            if cut > 0:
                out, buf = speakable(buf[:cut]), buf[cut:]  # keep the space with the next word
                spoken.append(out)
                yield out
        if buf:
            out = speakable(buf)
            spoken.append(out)
            yield out
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
             "function": {"name": c["name"], "arguments": c["args"] or "{}"},
             **({"extra_content": c["extra"]} if c.get("extra") else {})}
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
        db.log("error", "Brain error", _err(e))
        yield sse_chunk(cid, model, {"content": "Sorry, I hit a problem reaching our systems. "
                                                "Please try again."})
    yield sse_chunk(cid, model, {}, "stop")
    yield "data: [DONE]\n\n"
