"""HomePilot's brain.

Agora Conversational AI sends every user turn here in OpenAI Chat Completions format
(our /chat/completions endpoint). We:
  1. replace Agora's system message with our own prompt + live context (memory),
  2. run the LLM with tools; while it asks for tools we execute them,
  3. stream the final answer back as OpenAI-style SSE chunks, which Agora speaks with Murf.

While tools run we immediately stream a short filler ("One sec, checking stock.") so the
technician never hears dead air."""
import asyncio
import difflib
import json
from datetime import datetime
import re
import time
import uuid
from collections.abc import AsyncIterator

from openai import APIError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from . import config, db, languages, tools

MAX_TOOL_ROUNDS = 10
MAX_HISTORY = 10

SYSTEM_PROMPT = """You are HomePilot, a warm, efficient voice assistant for the Sharma family's
home. You talk with Anika (a parent). Everything you write is spoken aloud.

VOICE
- Max 3 short sentences. No lists, numbering, symbols or markdown. Friendly, never chatty.
  Mention only what matters now. For appliance problems: one safe step at a time.

FACTS
- Use tools for every fact; never invent events, tasks, prices, slots or history.
- "What's on today" or planning: get_today. Appliance problem: get_appliance, then
  search_manual for the error code. get_appliance may return an "insight" (repeat fault):
  say it first in one sentence, then give the first safe check.
- Need a technician: find_service_slots, then IMMEDIATELY book_service for its best slot in
  the same turn (it only prepares it). Then say in one breath which slot clashed with the
  family calendar and was skipped, the chosen slot and fee, and ask ONE yes/no question.
- Respect the family's preferences (vegetarian, budget, quiet hours, Dadi's medicine).

ACTIONS
- book_service, order_groceries, store_checkout and urgent alerts return awaiting_confirmation: nothing has happened yet. Ask a
  clear yes/no question. On yes call confirm_action(approved=true) with the pending id from
  LIVE CONTEXT; on no / don't / wait / cancel use approved=false.
- Never say something was done unless a tool returned success. One tool call per requested
  action. Don't write an answer in the same message as tool calls. A done booking is done:
  thanks / okay are not new bookings.
- Shopping: add_to_shopping_list; "X bought the milk": mark_bought; "take it off the list":
  remove_from_shopping_list; "what's left / how much": shopping_status; "just order it":
  order_groceries = home delivery from the family's store (needs a yes, like bookings). Quote
  totals only from shopping_status or LIVE CONTEXT; never estimate prices yourself. Chores: assign_task (it notifies the person). "X did it":
  complete_task. Medicine or other nudges: set_reminder (only a time the user said; ask if
  not). Calendar: add_event. "Get Rohan / my friend Priya on the call": call_family_member (it sends a join link; you
  never phone anyone).
- Ordering food or anything from the store (biryani, ghee, chips, perfume…): store_search,
  then store_add_to_cart with the item ids, then store_checkout, which reads back the cart
  and needs a yes. After the yes the order is placed and the PAYMENT IS APPROVED ON THE
  USER'S PHONE (UPI-style request; QR on the dashboard). Never ask for a PIN, card or bank
  details, and never say it is paid until store_order_status says so.
- "Dadi took her tablet" / "done": reminder_done. "How much did we spend?": spending_summary
  (quote its numbers only).
- SMART HOME: the AC and washing machine are connected. get_device_status reads live
  temperature, fan speed and error code; use it instead of asking the user to read the
  display. Devices also report faults by themselves and you announce them.
- GOOGLE CALENDAR / PHONE: HomePilot's calendar DOES sync to phones. If the user mentions
  Google Calendar, their phone, a QR code, or "what if this device is off": call
  calendar_on_phone (shows a QR code; they scan it and tap Add / Subscribe; then events and
  alarms alert on their phone too). Never say you can't connect to Google Calendar.
- Changing a time ("make it 6 instead of 7"): call add_event with the new time (it MOVES the
  event, no duplicate) and cancel_reminder + set_reminder for the alarm. cancel_event removes one.
- Wrap-up ("that's all", "update the family"): daily_summary, then a one-sentence recap.
- A booking already made and the user says "no / don't book it / cancel the technician":
  call cancel_booking (NOT cancel_reminder). Never claim it's cancelled unless the tool says so.
- ALARMS: "alarms at 6 and 7" = TWO set_reminder calls, one per time. An alarm is NOT a
  calendar event: add one event for the occasion itself (e.g. the hackathon at 8 AM) only if
  the user gives its time. Call calendar_on_phone ONCE per conversation.
- DATES: after midnight and before 5 AM, "tomorrow morning" or "today" both mean the coming
  morning. Always read back the day name and "in X hours" from the tool (e.g. "Saturday at
  6 AM, in 4 hours 40 minutes"). Speech-to-text mishears: "two days" is usually "today";
  if a day sounds odd, ask instead of guessing.
- Alarms are reminders: set_reminder / cancel_reminder. They ring on the HomePilot dashboard,
  are spoken in this call and posted to the family channel; with calendar_on_phone they also
  alert on the user's phone. If nobody confirms within 5 minutes, the family is alerted. If the user
  is unclear about AM or PM, ask. Never compute durations yourself: use "rings_in".
- WHAT HOMEPILOT IS (use these facts, never invent others): a voice assistant with a live
  dashboard that is view-only. Everything is done by asking you: to delete an alarm the user
  just says "cancel the 10:50 alarm". When a reminder is due, the dashboard shows a pop-up
  with a chime, you say it out loud in the call, and it is posted to the family channel.
  There is no separate app, swipe or delete button. Never invent screens or features.
- You CANNOT: book hotels or travel, make phone calls or dial numbers, cancel a
  sent message or invitation, or shop on other websites. To remember something (a birthday, a
  preference, a number), use remember_fact; only then may you say it is saved. Say so in one sentence
  and offer a real alternative (a reminder, a task or a shopping item). Never say you "noted" or "saved" something unless a tool did it.
- send_family_update is ONLY for when the user asks you to tell / message the family. Never
  use it for your own notes, apologies or to record what the user said.
- Tool error: say it briefly and offer the next best option."""


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
                for c in rnd.calls.values():  # "call_family_model" -> "call_family_member"
                    if c["name"] not in tools.TOOLS:
                        near = difflib.get_close_matches(c["name"], list(tools.TOOLS), n=1,
                                                         cutoff=0.75)
                        if near:
                            db.log("llm", f"Corrected tool name {c['name']} -> {near[0]}")
                            c["name"] = near[0]
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
    """Memory injected into every turn: time, family, active issue, what is pending."""
    lines = ([languages.instruction()] if languages.current() != "en" else []) + [
             f"Current local time: {db.now().strftime('%A %d %B %Y, %H:%M')} (Asia/Kolkata).",
             "Family: " + "; ".join(f"{m['name']} ({m['role']}, {m['notes']})"
                                   for m in db.q("SELECT * FROM members")) + ".",
             "Preferences: " + "; ".join(f"{h['key']}: {h['value']}"
                                        for h in db.q("SELECT * FROM household")) + "."]
    shop = tools.shopping_status()
    lines.append("Shopping list: " + (", ".join(shop["to_buy"]) if shop["to_buy"] else "empty")
                 + (f" (estimated total {shop['estimated_total_inr']} rupees)" if shop["to_buy"] else "")
                 + (". Bought: " + ", ".join(shop["bought"]) if shop["bought"] else "") + ".")
    facts = tools.recall_facts()
    if facts:
        lines.append("Remembered for the family: " + "; ".join(facts) + ".")
    active = db.recall("active_appliance")
    if active:
        a = db.one("SELECT name, model FROM appliances WHERE id=?", (active,))
        if a:
            lines.append(f"Active home issue: {a['name']} ({a['model']}).")
    for p in db.q("SELECT id, summary FROM pending_actions WHERE status='awaiting_confirmation' "
                  "ORDER BY created_at DESC LIMIT 3"):
        lines.append(f"Pending confirmation {p['id']}: {p['summary']}")
    for r in db.q("SELECT text, member, remind_at FROM reminders WHERE status='scheduled' "
                  "ORDER BY remind_at LIMIT 5"):
        d = datetime.fromisoformat(r["remind_at"])
        lines.append(f"Scheduled reminder for {r['member']}: {r['text']} at "
                     f"{d.strftime('%I:%M %p').lstrip('0')} ({tools._until(d)} from now).")
    for b in db.q("SELECT id, provider, slot_at FROM bookings WHERE status='booked'"):
        lines.append(f"Booked {b['id']}: {b['provider']} at {b['slot_at']}.")
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
    # Merge consecutive turns from the same speaker. Agora can resend a growing sentence
    # ("Yes." -> "Yes. I'm good." -> ...): keep the longest version instead of all of them.
    merged: list[dict] = []
    for m in convo:
        if merged and merged[-1]["role"] == m["role"]:
            prev = merged[-1]["content"]
            if m["content"].startswith(prev[: max(1, len(prev) - 2)]):
                merged[-1] = m
            elif not prev.startswith(m["content"]):
                merged[-1] = {"role": m["role"], "content": prev + " " + m["content"]}
        else:
            merged.append(m)
    # The conversation must end with the user (Gemini rejects a request ending with a model
    # turn, and Agora sometimes sends the assistant's last line after the user's).
    while merged and merged[-1]["role"] == "assistant":
        merged.pop()
    if not merged:
        merged = [{"role": "user", "content": "(The user is waiting. Continue helpfully.)"}]
    convo = merged[-MAX_HISTORY:]
    while convo and convo[0]["role"] == "assistant":
        convo = convo[1:]
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "LIVE CONTEXT (from enterprise systems):\n"
                                          + live_context()}] + convo


FILLERS = {
    "get_today": "Let me look at today.",
    "get_appliance": "Checking the AC's history.",
    "search_manual": "Let me check the manual.",
    "find_service_slots": "Checking technicians against the family calendar.",
    "book_service": "Preparing the booking.",
    "confirm_action": "Done, one second.",
    "add_event": "Adding it to the calendar.",
    "add_to_shopping_list": "Adding that.",
    "mark_bought": "Ticking that off.",
    "remove_from_shopping_list": "Taking that off the list.",
    "shopping_status": "Checking the list.",
    "order_groceries": "Putting the order together.",
    "store_search": "Looking that up in the store.",
    "store_add_to_cart": "Adding it to the cart.",
    "store_view_cart": "Checking the cart.",
    "store_checkout": "Getting the order ready.",
    "store_order_status": "Checking the order.",
    "assign_task": "Sending that over.",
    "complete_task": "Ticking that off.",
    "set_reminder": "Setting the reminder.",
    "reminder_done": "Great, noting that.",
    "calendar_on_phone": "Putting a QR code on the screen.",
    "cancel_event": "Removing it.",
    "cancel_booking": "Cancelling the booking.",
    "get_device_status": "Checking the AC's sensors.",
    "spending_summary": "Adding it up.",
    "remember_fact": "I'll remember that.",
    "cancel_reminder": "Cancelling that.",
    "send_family_update": "Messaging the family.",
    "daily_summary": "Sending the family a summary.",
    "call_family_member": "Sending them a link to join.",
}


_TURN = {"n": 0}


async def run_turn(incoming: list[dict]) -> AsyncIterator[str]:
    """Yields text pieces of the spoken reply.

    Agora can send a sentence twice while the user is still talking ("I would like..." then
    "I would like to add a Google Calendar"). Only the newest request answers: an older turn
    stops as soon as a newer one starts, so the user never hears two replies or gets
    double actions."""
    _TURN["n"] += 1
    me = _TURN["n"]
    stale = lambda: _TURN["n"] != me
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
            if stale():
                return
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
        if stale():
            return
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
            result = await asyncio.to_thread(tools.call, c["name"], c["args"])
            db.log("tool", c["name"], json.dumps(result)[:400])
            messages.append({"role": "tool", "tool_call_id": c["id"] or f"call_{i}",
                             "content": json.dumps(result, default=str)})
    else:
        msg = "Sorry, that took too many steps. Can you say that again more simply?"
        spoken.append(msg)
        yield msg
    db.log("agent", "FieldPilot", "".join(spoken).strip())
    db.remember("last_assistant_text", "".join(spoken).strip()[-400:])


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
