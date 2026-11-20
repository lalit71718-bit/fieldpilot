// FieldPilot web app: joins the Agora channel (mic in, agent voice out), starts the AI agent,
// and renders the live state of the "enterprise systems" the agent is changing.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hhmm = (ts) => (ts || "").slice(11, 16);

let rtc = null, mic = null, live = false, cfg = null, busy = false;
const textHistory = [];

// Minutes of manual work each automated action replaces (shown as "paperwork saved").
const MINUTES = { order: 8, ticket: 3, report: 12, notify: 3, schedule: 4, knowledge: 5, inventory: 4 };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

function setChip(id, text, on) { const el = $(id); el.textContent = text; el.classList.toggle("on", !!on); }
function setStatus(main, hint) { $("call-status").textContent = main; if (hint !== undefined) $("call-hint").textContent = hint; }

// ------------------------------------------------------------------ voice (Agora)
async function startVoice() {
  cfg = await api("/api/config");
  if (cfg.missing.length) {
    $("setup").classList.remove("hidden");
    $("setup").innerHTML = `Voice needs these settings in <code>.env</code>: <b>${cfg.missing.join(", ")}</b>.<br>You can still use the text box below.`;
    if (!cfg.app_id) return;
  }
  setStatus("Connecting...", "");
  rtc = AgoraRTC.createClient({ mode: "rtc", codec: "vp8" });
  rtc.on("user-published", async (user, type) => {
    await rtc.subscribe(user, type);
    if (type === "audio") { user.audioTrack.play(); watchAgentVoice(user.audioTrack); setChip("chip-agent", "Agent: in call", true); }
  });
  rtc.on("user-left", () => setChip("chip-agent", "Agent: left", false));
  await rtc.join(cfg.app_id, cfg.channel, cfg.token, cfg.uid);
  mic = await AgoraRTC.createMicrophoneAudioTrack({ AEC: true, ANS: true, AGC: true });
  await rtc.publish([mic]);
  setChip("chip-rtc", "Voice: live", true);
  live = true; $("mic").classList.add("live");
  setStatus("Starting FieldPilot agent...", "");
  const res = await api("/api/agent/start", { method: "POST", body: JSON.stringify({ uid: cfg.uid }) });
  if (!res.ok) { setStatus("Agent failed to start", res.error?.slice(0, 160) || ""); return; }
  setChip("chip-agent", "Agent: joining", true);
  setStatus("Listening - just talk", 'Try: "What\'s my first job?"');
}

async function stopVoice() {
  live = false; $("mic").classList.remove("live", "speaking");
  await api("/api/agent/stop", { method: "POST" }).catch(() => {});
  if (mic) { mic.stop(); mic.close(); mic = null; }
  if (rtc) { await rtc.leave(); rtc = null; }
  setChip("chip-rtc", "Voice: offline", false); setChip("chip-agent", "Agent: idle", false);
  setStatus("Session ended", "Tap to start again");
}

function watchAgentVoice(track) {
  const tick = () => {
    if (!live) return;
    const lvl = track.getVolumeLevel ? track.getVolumeLevel() : 0;
    $("mic").classList.toggle("speaking", lvl > 0.05);
    $("mic").style.setProperty("--lvl", 1 + Math.min(lvl, 1) * 0.25);
    requestAnimationFrame(tick);
  };
  tick();
}

$("mic").onclick = async () => {
  if (busy) return;  // ignore double clicks while connecting
  busy = true;
  try { live ? await stopVoice() : await startVoice(); }
  catch (e) {
    setStatus("Voice error", e.message); console.error(e);
    // clean up a half-open session so the next click starts fresh
    try { if (mic) { mic.close(); mic = null; } if (rtc) { await rtc.leave(); rtc = null; } } catch {}
    live = false; $("mic").classList.remove("live", "speaking");
  } finally { busy = false; }
};

// Leave the channel and stop the agent when the tab closes or reloads.
window.addEventListener("pagehide", () => {
  if (live) navigator.sendBeacon("/api/agent/stop");
  if (rtc) rtc.leave();
});

// ------------------------------------------------------------------ text backup mode
$("text-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const text = $("text-input").value.trim();
  if (!text) return;
  $("text-input").value = "";
  textHistory.push({ role: "user", content: text });
  try {
    const { reply } = await api("/api/chat", { method: "POST", body: JSON.stringify({ messages: textHistory }) });
    textHistory.push({ role: "assistant", content: reply });
  } catch (e) { textHistory.pop(); alert(e.message); }
  refresh();
};

$("reset").onclick = async () => {
  if (!confirm("Reset all demo data (tickets, stock, orders)?")) return;
  textHistory.length = 0; await api("/api/reset", { method: "POST" }); refresh();
};

// ------------------------------------------------------------------ dashboard
const prevStock = {};
function render(s) {
  // Transcript comes from what the brain actually heard and said (voice or text).
  const convo = s.activity.filter((a) => a.kind === "user" || a.kind === "agent").reverse();
  $("transcript").innerHTML = convo.map((a) =>
    `<div class="msg ${a.kind}"><div class="who">${a.kind === "user" ? "You" : "FieldPilot"} · ${hhmm(a.ts)}</div>${esc(a.detail)}</div>`).join("")
    || '<div class="muted">The conversation will appear here.</div>';
  $("transcript").scrollTop = 1e6;

  // Pending confirmation banner (human in the loop).
  const p = s.pending[0];
  $("pending").classList.toggle("hidden", !p);
  if (p) $("pending").innerHTML = `<b>Waiting for your OK</b> &nbsp;${esc(p.summary)} <span class="muted">Say "yes" or "no".</span>`;

  // Active ticket.
  const t = s.tickets.find((x) => x.id === s.active_ticket);
  $("ticket-status").textContent = t ? t.status.replace(/_/g, " ") : "-";
  $("ticket-status").className = "pill " + (t ? t.status : "");
  if (t) {
    $("ticket").classList.remove("muted");
    $("ticket").innerHTML = `
      <div class="title">${esc(t.id)} · ${esc(t.title)}</div>
      <div class="kv"><span>Asset <b>${esc(t.asset_name)} (${esc(t.model)})</b></span><span>Site <b>${esc(t.site)}</b></span>
      <span>Priority <b>${esc(t.priority)}</b></span><span>SLA due <b>${esc(t.sla_due.slice(5, 16))}</b></span></div>
      <div class="muted" style="font-size:13px">${esc(t.description)}</div>
      ${t.notes ? `<h3 style="margin-top:12px">Work notes</h3><div class="notes">${esc(t.notes.trim())}</div>` : ""}`;
  } else {
    $("ticket").innerHTML = s.tickets.map((x) => `<div>${esc(x.id)} · ${esc(x.title)} <span class="pill ${x.status}">${esc(x.status)}</span></div>`).join("");
  }

  // Stock (flash cells that changed).
  $("stock").innerHTML = `<table><tr><th>Part</th><th>Warehouse</th><th>Qty</th></tr>${s.stock.map((r) => {
    const k = r.sku + r.warehouse, changed = prevStock[k] !== undefined && prevStock[k] !== r.qty; prevStock[k] = r.qty;
    return `<tr><td>${esc(r.sku)}</td><td>${esc(r.warehouse)}</td><td class="${r.qty === 0 ? "zero" : ""} ${changed ? "flash" : ""}">${r.qty}</td></tr>`;
  }).join("")}</table>`;

  // Orders + follow-ups.
  const o = s.orders.map((x) => `<div class="hist"><b>${esc(x.id)}</b> ${x.qty} × ${esc(x.sku)} from ${esc(x.warehouse)} · ₹${x.total}<div class="d">${esc(x.status)} · ETA ${esc(x.eta.slice(5, 16))}</div></div>`);
  const f = s.followups.map((x) => `<div class="hist">📅 <b>${esc(x.when_at.slice(5, 16))}</b> ${esc(x.purpose)}<div class="d">${esc(x.ticket_id)}</div></div>`);
  $("orders").innerHTML = [...o, ...f].join("") || '<div class="muted">Nothing yet.</div>';

  $("notifications").innerHTML = s.notifications.map((n) =>
    `<div class="n"><div class="meta">${esc(n.recipient)} · ${hhmm(n.created_at)} · ${n.delivered ? "delivered to Slack" : "logged"}</div>${esc(n.message.slice(0, 400))}</div>`).join("")
    || '<div class="muted">No messages yet.</div>';

  const r = s.reports[0];
  $("report").textContent = r ? r.body : "Not generated yet.";
  $("report").classList.toggle("muted", !r);

  $("activity").innerHTML = s.activity.filter((a) => !["user", "agent", "tool", "llm"].includes(a.kind)).map((a) =>
    `<div class="ev"><span class="t">${hhmm(a.ts)}</span><span class="tag ${a.kind}">${esc(a.kind)}</span><span title="${esc(a.detail)}"><b>${esc(a.title)}</b> <span class="d">${esc(a.detail)}</span></span></div>`).join("")
    || '<div class="muted">Actions the agent takes will stream here.</div>';

  const saved = s.activity.reduce((m, a) => m + (MINUTES[a.kind] || 0), 0);
  $("saved").textContent = saved;
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { console.warn(e); } }
setInterval(refresh, 1500);
refresh();
