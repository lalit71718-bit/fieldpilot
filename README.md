# FieldPilot: hands-free voice copilot for field technicians

> **Build with Agora, Bengaluru 2026** · Track **PS-2: From Workplace Assistance to Intelligent Action** (Field Technician Copilot)
>
> 🎥 **Demo video:** [add link] · 📊 **Pitch deck:** [add link] · 👥 **Team SpeakX:** Lalit Kumar Singh

A technician fixing a broken machine has both hands busy. They just **talk**. FieldPilot pulls up
their ticket and the machine's history, walks them through the repair using the service manual,
finds the spare part across warehouses, orders it **only after the technician says yes**, updates
the ticket, writes the service report, messages the supervisor on Slack and books the revisit.

![FieldPilot dashboard after a full voice run](docs/dashboard.png)

## How it meets the hackathon requirements

| Requirement | How FieldPilot does it |
|---|---|
| **Real-time conversational AI** | Agora **Conversational AI** agent in an Agora **RTC** channel: speech-to-text, turn-taking, barge-in. It calls our backend as a custom LLM. |
| **Natural voice** | **Murf Falcon TTS** speaks every reply. A short filler ("One sec, checking stock") covers tool calls, so there's no dead air. |
| **Agentic workflow** | 10 tools: tickets, asset history, manual search (RAG), inventory, part orders, ticket updates, Slack, follow-ups, service reports. Also exposed as an **MCP server**. |
| **End-to-end journey** | *Before:* assigned tickets and history. *During:* voice troubleshooting from the manual. *Action:* stock check, order, ticket update. *After:* report, supervisor update, revisit booked. |
| **Confirm critical actions** | Orders, closing tickets and urgent escalations wait for a spoken **yes**. "No" is enforced in code, even if the model says approve. |
| **Contextual intelligence** | Live memory each turn: technician, active ticket, pending approvals, placed orders. Points out repeat faults from the machine's history. |
| **Error handling** | Out-of-stock at the nearest warehouse → best alternative. Invalid or repeated actions are blocked (no double orders). LLM failover across providers. |
| **Predictive insight** | Spots **repeat faults** from the asset history (E4 for the third time, capacitor flagged but never replaced) and steers to the root cause; the insight also lands in the service report. |
| **Human escalation over Agora** | "Get me a senior engineer": FieldPilot posts a join link to Slack, and the expert joins the **same live Agora call** as the technician and the AI, from their phone (`/expert.html`). |
| **Proactive start** | The agent opens with a **shift briefing**: open jobs, top priority, SLA countdown. KPI strip on the dashboard. |
| **Measurable efficiency** | The dashboard's "Paperwork saved" counter estimates minutes saved per visit from the actions the agent took. |

## Architecture

```
 Technician (browser mic, live dashboard)
        │  Agora RTC audio
        ▼
 Agora Conversational AI agent ── speech-to-text, turn-taking, interruptions
        │  OpenAI-format POST /chat/completions (public URL via Cloudflare tunnel)
        ▼
 FieldPilot brain (FastAPI)
   ├─ brain.py       tool-calling loop, streams the reply (SSE), LLM failover, speech cleanup
   ├─ memory         live context every turn: technician, active ticket, pending approvals
   ├─ rag.py         BM25 search over equipment service manuals
   ├─ tools.py       10 enterprise tools + confirmation gate + "no means no" guard
   └─ mcp_server.py  the same tools over MCP
        │
        ▼ reply text
 Murf Falcon TTS (via Agora) ──► technician hears the answer
 Slack incoming webhook ──► supervisor's channel
```

## Demo script

Click **Reset demo** first. Say each line in one go, then wait for the answer.

| Step | Technician says | What happens on screen |
|---|---|---|
| Initiate | "What's my first job?" | `get_my_tickets` → TK-1042, Chiller 3, error E4, high priority |
| Understand | "I'm at the chiller, it shows E4 again. What should I check?" | `get_ticket` (same E4 two months ago, weak capacitor) + `search_manual` → one safe step at a time |
| Diagnose | "I measured the capacitor, it reads thirty seven." | Manual: replace below 40.5 microfarad |
| Execute | "Do we have a replacement?" | `check_inventory`: nearest warehouse **out of stock**, HSR Layout has 2 |
| Confirm | "Yes, order it." → **"Yes."** | Yellow approval banner → `confirm_action` → PO-5001, stock 2 → 1, ticket "waiting for parts" |
| Follow up | "Wrap it up. I'll come back tomorrow at ten thirty. Send the report and tell Priya." | `schedule_followup`, `generate_service_report`, Slack message |

Safety moment for judges: at the yellow banner say **"No"**. The order is cancelled and stock doesn't change.

Wow moment: say **"I'm stuck, get a senior engineer on this call."** A Slack message with a join link arrives; open it on a phone, tap **Join call**, and the expert talks with the technician while FieldPilot listens and keeps working.

## Run it

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # then fill in keys (macOS/Linux: cp)
python run_demo.py
```

`run_demo.py` starts a Cloudflare tunnel, passes its address to the server automatically, checks it
works and opens the app. You need `cloudflared` (`winget install --id Cloudflare.cloudflared`).
Without voice keys, the **text box** still runs the full agent.

| Setting | Where to get it |
|---|---|
| `LLM_*` (+ optional `LLM2_*` failover) | Groq or Gemini key (free tiers work); any OpenAI-compatible API |
| `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE` | Agora Console → Projects |
| `AGORA_CUSTOMER_ID`, `AGORA_CUSTOMER_SECRET` | Agora Console → RESTful API Keys |
| `MURF_API_KEY` | murf.ai → API dashboard |
| `SLACK_WEBHOOK_URL` (optional) | api.slack.com/apps → app → Incoming Webhooks |
| `BRAIN_API_KEY` | Any password you choose; Agora uses it to call the brain |

Health check: **http://localhost:8000/api/diag** shows each model, the tunnel and missing settings.
See [DEMO_DAY.md](DEMO_DAY.md) for the checklist before presenting.

## Reliability notes
- **Free LLM tiers** have small per-minute limits. The brain fails over instantly to the next model or provider, skips models that are throttled, gone or hanging, and keeps prompts small.
- **Agora sends extra fields** on messages; they're stripped before calling the LLM. Gemini's thought signatures are kept for Gemini and removed for other providers.
- **Repeated sentences** (Agora resends speech that arrives in pieces) can't duplicate orders, reports or revisits.

## MCP
```bash
mcp dev backend/mcp_server.py      # inspect and call the 10 tools in MCP Inspector
```

## Tests
```bash
pytest -q        # 23 tests, no keys needed (the LLM is faked)
```

## Project layout
```
run_demo.py        one-command demo launcher (tunnel + server + browser)
backend/
  main.py          HTTP API, /chat/completions for Agora, /api/diag, serves the web app
  brain.py         prompt, live context, tool loop, streaming, failover, speech cleanup
  tools.py         enterprise tools, confirmation gate, safety guards
  rag.py           manual search
  db.py            SQLite stand-in for CMMS / ERP / inventory, demo seed data
  agora.py         RTC tokens, start/stop the Conversational AI agent, Murf TTS config
  integrations.py  Slack webhook
  mcp_server.py    MCP server over the same tools
  manuals/         service manuals used for RAG
frontend/          voice call panel + live dashboard (Agora Web SDK)
tests/
```

## What's next
- Live connectors for Jira / ServiceNow / SAP (same function signatures in `tools.py`).
- Hindi and Kannada voice for technicians (`AGORA_ASR_LANGUAGE`, a Murf multilingual voice).
- Camera: the technician shows the error display, and a vision model reads the code.
- Offline queue for sites with no signal.
