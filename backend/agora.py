"""Agora: RTC tokens + starting/stopping the Conversational AI agent.

The agent joins the same RTC channel as the user's browser. Agora handles speech-to-text,
turn-taking and interruptions, calls OUR /chat/completions as its LLM, and speaks the reply with
Murf Falcon TTS.

Field names follow Agora's Conversational AI v2 REST API docs (join + Murf TTS). If anything differs,
override any part of the request with AGORA_JOIN_OVERRIDES_JSON (deep-merged) or the Murf block
with AGORA_TTS_PARAMS_JSON - no code change needed."""
import base64
import json
import os
import random
import time
import uuid

import httpx
from agora_token_builder import RtcTokenBuilder

from . import config, db, tools

API = "https://api.agora.io/api/conversational-ai-agent/v2/projects"
USER_UID = 1001
AGENT_UID = 2001
ROLE_PUBLISHER = 1

_state = {"agent_id": None}


def rtc_token(uid: int, channel: str | None = None, ttl: int = 3600) -> str:
    if not config.AGORA_APP_CERTIFICATE:
        return ""  # project in "testing mode" (App ID only)
    return RtcTokenBuilder.buildTokenWithUid(
        config.AGORA_APP_ID, config.AGORA_APP_CERTIFICATE, channel or config.AGORA_CHANNEL,
        uid, ROLE_PUBLISHER, int(time.time()) + ttl)


def _auth_header() -> dict:
    raw = f"{config.AGORA_CUSTOMER_ID}:{config.AGORA_CUSTOMER_SECRET}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Content-Type": "application/json"}


def murf_tts() -> dict:
    if config.AGORA_TTS_PARAMS_JSON:
        params = json.loads(config.AGORA_TTS_PARAMS_JSON)
    else:
        # Field names from docs.agora.io/en/ai/models/tts/murf (REST API sample).
        params = {"api_key": config.MURF_API_KEY,
                  "base_url": "wss://global.api.murf.ai/v1/speech/stream-input",
                  "voiceId": config.MURF_VOICE_ID, "locale": config.MURF_LOCALE,
                  "rate": 0, "pitch": 0, "model": config.MURF_MODEL, "sample_rate": 24000}
    return {"vendor": "murf", "params": params}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def join_body(channel: str, user_uid: int = USER_UID) -> dict:
    # Fresh agent uid each start, so a leftover agent from an earlier run never conflicts.
    agent_uid = random.randint(100_000, 999_999)
    body = {
        "name": f"homepilot-{uuid.uuid4().hex[:8]}",
        "properties": {
            "channel": channel,
            "token": rtc_token(agent_uid, channel),
            "agent_rtc_uid": str(agent_uid),
            "remote_rtc_uids": [str(user_uid)],
            "idle_timeout": 180,
            "asr": {"language": config.AGORA_ASR_LANGUAGE},
            "llm": {
                "url": f"{config.PUBLIC_BASE_URL}/chat/completions",
                "api_key": config.BRAIN_API_KEY,
                "vendor": "custom",
                "style": "openai",
                "system_messages": [{"role": "system", "content": "HomePilot"}],
                "max_history": 24,
                "greeting_message": tools.shift_briefing(),
                "failure_message": "Sorry, I lost connection for a moment.",
                "params": {"model": config.LLM_MODEL},
            },
            "tts": murf_tts(),
        },
    }
    overrides = os.getenv("AGORA_JOIN_OVERRIDES_JSON", "").strip()
    return _merge(body, json.loads(overrides)) if overrides else body


def missing_settings() -> list[str]:
    need = {"AGORA_APP_ID": config.AGORA_APP_ID, "AGORA_CUSTOMER_ID": config.AGORA_CUSTOMER_ID,
            "AGORA_CUSTOMER_SECRET": config.AGORA_CUSTOMER_SECRET,
            "PUBLIC_BASE_URL": config.PUBLIC_BASE_URL, "LLM_API_KEY": config.LLM_API_KEY}
    if not config.AGORA_TTS_PARAMS_JSON:
        need["MURF_API_KEY"] = config.MURF_API_KEY
    missing = [k for k, v in need.items() if not v]
    host = config.PUBLIC_BASE_URL.split("://", 1)[-1].split("/")[0]
    if config.PUBLIC_BASE_URL and ("." not in host or "xxxx" in host or host.startswith(
            ("localhost", "127.0.0.1"))):
        missing.append("PUBLIC_BASE_URL (must be your cloudflared https address, not "
                       f"'{config.PUBLIC_BASE_URL}')")
    return missing


async def start_agent(channel: str | None = None, user_uid: int = USER_UID) -> dict:
    channel = channel or config.AGORA_CHANNEL
    missing = missing_settings()
    if missing:
        return {"ok": False, "error": "Missing settings: " + ", ".join(missing)}
    if _state["agent_id"]:
        await stop_agent()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{API}/{config.AGORA_APP_ID}/join", headers=_auth_header(),
                         json=join_body(channel, user_uid))
    if r.status_code >= 300:
        db.log("error", "Agent start failed", f"{r.status_code} {r.text[:300]}")
        return {"ok": False, "status": r.status_code, "error": r.text}
    data = r.json()
    _state["agent_id"] = data.get("agent_id")
    db.log("system", "Voice agent joined", f"agent {_state['agent_id']} in {channel}")
    return {"ok": True, "agent_id": _state["agent_id"], "channel": channel}


async def stop_agent() -> dict:
    agent_id = _state["agent_id"]
    if not agent_id:
        return {"ok": True, "note": "No agent running."}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{API}/{config.AGORA_APP_ID}/agents/{agent_id}/leave",
                         headers=_auth_header())
    _state["agent_id"] = None
    db.log("system", "Voice agent left", agent_id)
    return {"ok": r.status_code < 300, "status": r.status_code}
