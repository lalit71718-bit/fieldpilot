# Demo day checklist (HomePilot)

## 30 minutes before
1. Laptop charged, sleep turned off, notifications off.
2. Phone hotspot ready in case the venue Wi-Fi fails. Slack app open on the phone (the #family channel).
3. Headphones or earbuds with a mic, so the assistant doesn't hear itself.

## Start (one command)
```
cd "C:\Users\D E L L\fieldpilot"
.venv\Scripts\activate
git pull
python run_demo.py
```
It opens the website; click **Open the app** for the voice dashboard (`/app.html`). Or use the Render deployment: **https://fieldpilot-gu0j.onrender.com** (app: https://fieldpilot-gu0j.onrender.com/app.html). Open it 5 minutes early so it wakes up.

## Check (1 minute)
- Open `/api/diag`: `missing_for_voice` is `[]`, at least one model under `llm` is `"ok": true`, `tunnel` is `"ok": true`.
- Click **Reset demo**. Type "What's on today?" in the text box: it should read the family plan. Click **Reset demo** again.

## Live demo (about 90 seconds)
1. Click the mic and listen to the daily briefing.
2. Click **Simulate AC fault** (Smart home card) → HomePilot speaks up first: the AC reported E4 by itself. Or say: "The living room AC is showing E four again and blowing warm air." → point at the blue **Repeat fault** banner.
3. "The outdoor fan isn't spinning. Can you get someone to fix it?" → it skips the slot that clashes with Dadi's doctor visit → **yellow banner**.
4. "Yes." → point at the calendar (new orange row) and the reminder.
5. "Add milk, paneer and Dadi's BP tablets to the shopping list, and ask Rohan to pick them up on his way home." → shopping list, task, Slack on your phone.
6. "Remind Dadi to take her BP tablet at 8 PM."
7. "Kabir already took out the recycling." → the chore gets a green tick.
8. "That's all. Send the family an update." → summary on Slack.
9. (Proactive) "Remind me in one minute to call the plumber." → a minute later HomePilot says it out loud and the dashboard pops up with a chime.
10. (Wow) "Get Rohan on this call." → open the Slack link on your phone → **Join call** → talk from the phone.

11. "Put it on my phone." → scan the QR → tap **Add** → it's in your phone's calendar.
12. "Order a veg biryani." → "Yes." → scan the payment QR → **Approve** → "preparing".
13. "How much did we spend this week?"

Safety moment: at the yellow banner say **"No, don't book it."** Nothing is booked. After a booking, "Cancel the technician" undoes it.

Say each sentence in one go, then wait for the answer.

## If something fails
| Problem | Do this |
|---|---|
| "Sorry, I lost connection" | Ctrl+C, then `python run_demo.py` again (new tunnel) |
| Slow answers | Free AI limit: keep talking slowly; failover kicks in |
| Voice won't start | Use the text box (same assistant, same dashboard) |
| AI quota / rate limit, or no time | Click **▶ Demo mode**: the whole story through the real tools in about 2 minutes, no LLM needed |
| Speech-to-text mishears names | Speak slower, use headphones; Demo mode as a backup |
| Wi-Fi down | Switch to the hotspot, or play the backup video |

Backup video: [add link]
