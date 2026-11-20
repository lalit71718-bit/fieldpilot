# Demo day checklist

## 30 minutes before
1. Laptop charged, sleep turned off (Settings → Power → Never), notifications off (Focus).
2. Phone hotspot ready in case the venue Wi-Fi fails. Slack app open on the phone.
3. Headphones or earbuds with a mic, so the agent doesn't hear itself.

## Start (one command)
```
cd "C:\Users\D E L L\fieldpilot"
.venv\Scripts\activate
git pull
python run_demo.py
```
It prints the tunnel address, checks it, and opens http://localhost:8000.

## Check (1 minute)
- Open http://localhost:8000/api/diag:
  - `missing_for_voice` is `[]`
  - at least one model under `llm` shows `"ok": true`
  - `tunnel` shows `"ok": true`
- Click **Reset demo**.
- Type "What's my first job?" in the text box: it should answer TK-1042. Click **Reset demo** again.

## Live demo (about 60 seconds)
1. Click the mic and wait for "Hi Ravi, FieldPilot here".
2. "What's my first job?"
3. "I'm at the chiller, it shows E4 again. What should I check?"
4. "I measured the capacitor, it reads thirty seven."
5. "Do we have a replacement?"
6. "Yes, order it." → point at the **yellow banner** → "Yes." → point at stock 2 → 1 and PO-5001.
7. "Wrap it up. I'll come back tomorrow at ten thirty. Send the report and tell Priya." → show the Slack message on your phone.

Say each sentence in one go, then wait for the answer.

## If something fails
| Problem | Do this |
|---|---|
| Agent says "Sorry, I lost connection" | Ctrl+C, then `python run_demo.py` again (new tunnel) |
| Slow answers | Free AI limit: keep talking slowly; failover kicks in |
| Voice won't start | Use the text box (same agent, same dashboard) |
| Wi-Fi down | Switch to the hotspot and restart, or play the backup video |

Backup video: [add link]
