# FieldPilot: hands-free voice copilot for field technicians

> Build with Agora, Bengaluru 2026: **PS-2 "From Workplace Assistance to Intelligent Action"** (Field Technician Copilot)

A technician at a broken machine has both hands busy. They just talk. FieldPilot pulls up their
ticket and the machine's history, walks them through the repair using the service manual,
finds the spare part across warehouses, orders it **after the technician says yes**, updates the
ticket, writes the service report, notifies the supervisor on Slack and books the revisit.

## Architecture

```
 Technician (browser mic)
        │  Agora RTC audio
        ▼
 Agora Conversational AI agent  ── speech-to-text, turn-taking, barge-in
        │  OpenAI-format POST  /chat/completions   (public URL via tunnel)
        ▼
 FieldPilot backend (FastAPI)  = the brain
   ├─ brain.py   LLM tool-calling loop, streams reply as SSE (+ instant filler while tools run)
   ├─ memory     live context injected every turn: technician, active ticket, pending approvals
   ├─ rag.py     BM25 search over equipment service manuals
   ├─ tools.py   10 enterprise tools (tickets, history, inventory, orders, Slack, follow-ups, report)
   │             critical actions -> pending action -> technician says yes -> confirm_action
   └─ mcp_server.py  same tools exposed over MCP
        │
        ▼ reply text
 Murf Falcon TTS (via Agora)  ──►  technician hears the answer
 Dashboard (same web app) polls /api/state and shows every system change live
```

## Demo script (matches the required flow)

| Phase | Technician says | What happens |
|---|---|---|
| Initiate | "What's my first job?" | `get_my_tickets`: TK-1042 Chiller 3 E4, high priority |
| Understand | "I'm at the chiller, it's showing E4 again." | `get_ticket` (history: same E4 two months ago, weak capacitor) + `search_manual` E4 |
| Diagnose | "Capacitor reads thirty-seven." | Manual says replace below 40.5 uF. Gives temporary fix (limit capacity to 60%) |
| Execute | "Do we have one?" | `check_inventory`: Whitefield **out of stock**, HSR Layout has 2 (error handling) |
| Confirm | "Order it." → **"Yes."** | `request_part_order` → yellow approval banner → `confirm_action` → stock decreases, PO created, ticket set to waiting for parts |
| Follow up | "Wrap it up, I'll come back tomorrow at 10:30." | `schedule_followup`, `generate_service_report`, `notify_supervisor` (Slack). Dashboard: "Paperwork saved: N min" |

Click **Reset demo** before each run.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in keys
uvicorn backend.main:app --reload --port 8000
```

Open http://localhost:8000. **The text box works with only `LLM_API_KEY` set**, so you can build and
test the whole agent before voice is wired up.

### Turn on voice
1. **Agora** ([console.agora.io](https://console.agora.io)): create a project, copy the App ID and App
   Certificate, enable **Conversational AI**, and create RESTful API credentials
   (Customer ID + Secret).
2. **Murf**: create an API key and choose a voice ID.
3. **Public URL**: Agora's cloud must reach your `/chat/completions`, so localhost does not work:
   ```bash
   cloudflared tunnel --url http://localhost:8000      # or: ngrok http 8000
   ```
   Put the https URL in `PUBLIC_BASE_URL`, then restart uvicorn.
4. Open the app (localhost is fine for the browser), click the mic, and talk.

### ⚠ Check against the official docs (unverified field names)
`backend/agora.py` builds the agent "join" request. The Murf `tts.params` field names
(`api_key`, `voice_id`, `model`, `locale`) were **not checked against the docs**. Compare them
with [Agora's Murf page](https://docs.agora.io/en/ai/models/tts/murf) and
[Murf's Agora guide](https://murf.ai/api/docs/integrations/agora). Fix them without editing code:
- `AGORA_TTS_PARAMS_JSON={"...": "..."}` replaces the Murf params.
- `AGORA_JOIN_OVERRIDES_JSON={"properties": {...}}` is deep-merged into the whole join body.

If the agent fails to start, the Agora error appears in the call panel and in the activity feed.

## LLM choice and rate limits (important for demo day)
Tested end to end with Groq `openai/gpt-oss-120b`: each voice turn takes about 1.3 to 2.7 s
including tool calls. **Groq's free tier allows only about 8k tokens per minute per model**, and
one voice turn can use 2 to 4 model calls. The brain therefore switches to `LLM_FALLBACK_MODELS`
immediately when a model is rate-limited, and then to an optional second provider (`LLM2_*`,
e.g. Cerebras free tier). Calls with invented tool names are retried on the next model. For the live demo, use a paid key (Groq Dev Tier, or
OpenAI `gpt-4o-mini`, about $1 for the whole hackathon) so the judges never hit a limit.

## Safety: human in the loop
Orders, closing tickets and urgent escalations never run straight away. They create a pending
action, and the technician must say yes. As a code-level guard, `confirm_action` refuses to
execute if the technician's last words were a refusal ("no", "don't", "wait", "cancel"),
even if the LLM approves.

## MCP
```bash
mcp dev backend/mcp_server.py          # inspect and call the tools in MCP Inspector
```
or add to any MCP client: `{"command": "python", "args": ["-m", "backend.mcp_server"]}`.

## Tests
```bash
pytest -q        # no keys needed; the LLM is faked
```

## Project layout
```
backend/
  main.py          HTTP API + serves the web app
  brain.py         system prompt, live context (memory), tool loop, SSE streaming
  tools.py         enterprise tools + confirmation gate (shared by brain and MCP)
  rag.py           manual search
  db.py            SQLite stand-in for CMMS / ERP / inventory, with demo seed data
  agora.py         RTC tokens, start/stop the Conversational AI agent, Murf TTS config
  integrations.py  Slack (falls back to dashboard-only when not configured)
  mcp_server.py    MCP server over the same tools
  manuals/         service manuals used for RAG (add .md files here)
frontend/          index.html, app.js, styles.css (Agora Web SDK from CDN)
tests/
```

## Ideas if time allows
- Swap SQLite tickets for real **Jira / Linear** and inventory for **Google Sheets** (same function signatures in `tools.py`).
- Hindi / Kannada voice (`AGORA_ASR_LANGUAGE`, a Murf multilingual voice).
- Camera: technician shows the error display, and a vision model reads the code.
