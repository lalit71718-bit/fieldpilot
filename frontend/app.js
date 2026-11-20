// HomePilot web app: joins the Agora channel (mic in, assistant voice out), starts the AI agent,
// and renders the live state of the family's connected services the assistant is changing.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const hhmm = (ts) => (ts || "").slice(11, 16);

let rtc = null, mic = null, live = false, cfg = null, busy = false;
const textHistory = [];

// Minutes of manual work each automated action replaces (shown as "time saved").
const MINUTES = { order: 12, booking: 10, calendar: 3, shopping: 2, task: 2, reminder: 2, notify: 3, knowledge: 5, call: 3, insight: 3 };
const ACTION_KINDS = ["order", "booking", "calendar", "shopping", "task", "reminder", "notify", "call"];
const fmtTime = (ts) => {
  const d = new Date(ts.replace(" ", "T")), now = new Date();
  const day = d.toDateString() === now.toDateString() ? "Today" :
    d.toDateString() === new Date(now.getTime() + 864e5).toDateString() ? "Tomorrow" : d.toLocaleDateString(undefined, { weekday: "short" });
  return `${day} ${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
};
let remoteUsers = new Set();
let lastAlarmId = null;
let lastInviteAt = "";  // newest alarm already shown

// Short chime with Web Audio (no sound file needed).
function chime() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [880, 660, 880].forEach((f, i) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
      const t = ctx.currentTime + i * 0.35;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.3, t + 0.03);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
      o.start(t); o.stop(t + 0.32);
    });
  } catch (e) { /* audio blocked until the user has clicked something */ }
}
// Alarms keep ringing (chime + voice every 10 s) until "Got it" or "Snooze".
const alarmQueue = [];
let alarmTimer = null, alarmRings = 0, currentAlarm = null;
const MAX_RINGS = 30;  // safety stop after ~5 minutes

function sayAlarm(a) {
  const text = `${a.title}. ${a.detail}`;
  if (live) {
    // In a live call, HomePilot says it again in its own voice (Agora speak API).
    fetch("/api/speak", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) }).catch(() => {});
  } else if ("speechSynthesis" in window) {
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
}
function ring() {
  if (!currentAlarm) return;
  alarmRings += 1;
  chime();
  // The first announcement in a call already came from the server; repeat from the 2nd ring.
  if (alarmRings > 1 || !live) sayAlarm(currentAlarm);
  $("alarm-count").textContent = alarmRings > 1 ? `Ringing · ${alarmRings} times` : "Ringing";
  if (alarmRings >= MAX_RINGS) stopAlarm("missed");
}
function startAlarm(a) {
  currentAlarm = a; alarmRings = 0;
  $("alarm-text").textContent = `${a.title}: ${a.detail}`;
  $("alarm").classList.remove("hidden");
  ring();
  alarmTimer = setInterval(ring, 10000);
  $("alarm-ok").focus();
}
function showAlarm(a) {
  if (currentAlarm) { alarmQueue.push(a); return; }
  startAlarm(a);
}
function stopAlarm(how) {
  clearInterval(alarmTimer); alarmTimer = null;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (currentAlarm) {
    fetch("/api/alarm/ack", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: currentAlarm.title, detail: currentAlarm.detail, how }) }).catch(() => {});
  }
  currentAlarm = null;
  $("alarm").classList.add("hidden");
  const next = alarmQueue.shift();
  if (next) setTimeout(() => startAlarm(next), 400);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
  return data;
}

function setChip(id, text, on) { const el = $(id); el.textContent = text; el.classList.toggle("on", !!on); }
function setStatus(main, hint) { $("call-status").textContent = main; if (hint !== undefined) $("call-hint").textContent = hint; }

// ------------------------------------------------------------------ voice (Agora)
// Browsers can block call audio that starts without a click: offer a one-tap fix.
if (window.AgoraRTC) AgoraRTC.onAutoplayFailed = () => {
  setStatus("Tap anywhere to turn on sound", "");
  document.addEventListener("click", () => { if (rtc) rtc.remoteUsers.forEach((u) => u.audioTrack && u.audioTrack.play()); }, { once: true });
};

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
    if (type === "audio") { user.audioTrack.play(); watchAgentVoice(user.audioTrack); setChip("chip-agent", "Assistant: in call", true); }
  });
  rtc.on("user-joined", (u) => { remoteUsers.add(u.uid); });
  rtc.on("user-left", (u) => { remoteUsers.delete(u.uid); if (!remoteUsers.size) setChip("chip-agent", "Assistant: left", false); });
  await rtc.join(cfg.app_id, cfg.channel, cfg.token, cfg.uid);
  mic = await AgoraRTC.createMicrophoneAudioTrack({ AEC: true, ANS: true, AGC: true });
  $("mute-btn").classList.remove("hidden");
  await rtc.publish([mic]);
  setChip("chip-rtc", "Voice: live", true);
  live = true; $("mic").classList.add("live");
  setStatus("Starting HomePilot...", "");
  const res = await api("/api/agent/start", { method: "POST", body: JSON.stringify({ uid: cfg.uid }) });
  if (!res.ok) { setStatus("Agent failed to start", res.error?.slice(0, 160) || ""); return; }
  setChip("chip-agent", "Assistant: joining", true);
  setStatus("Listening - just talk", 'Try: "What\'s on today?"');
}

async function stopVoice() {
  live = false; $("mic").classList.remove("live", "speaking");
  await api("/api/agent/stop", { method: "POST" }).catch(() => {});
  if (mic) { mic.stop(); mic.close(); mic = null; }
  if (rtc) { await rtc.leave(); rtc = null; }
  micMuted = false; $("mute-btn").classList.add("hidden"); $("mute-btn").textContent = "Mute mic";
  setChip("chip-rtc", "Voice: offline", false); setChip("chip-agent", "Assistant: idle", false);
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

let micMuted = false;
$("mute-btn").onclick = async () => {
  if (!mic) return;
  micMuted = !micMuted;
  await mic.setEnabled(!micMuted);
  $("mute-btn").textContent = micMuted ? "Unmute mic" : "Mute mic";
  $("mute-btn").setAttribute("aria-pressed", micMuted);
  $("mute-btn").classList.toggle("on-mute", micMuted);
};
$("alarm-ok").onclick = () => stopAlarm("done");
$("invite-copy").onclick = async () => { try { await navigator.clipboard.writeText($("invite-link").value); $("invite-copy").textContent = "Copied"; setTimeout(() => ($("invite-copy").textContent = "Copy link"), 1500); } catch { $("invite-link").select(); } };
$("invite-close").onclick = () => { $("invite").dataset.hidden = lastInviteAt; $("invite").classList.add("hidden"); };
$("alarm-snooze").onclick = () => stopAlarm("snooze");

$("reset").onclick = async () => {
  if (!confirm("Reset all demo data (calendar, tasks, shopping, bookings)?")) return;
  textHistory.length = 0; await api("/api/reset", { method: "POST" }); refresh();
};

// ------------------------------------------------------------------ dashboard
function render(s) {
  const nameOf = (id) => (s.members.find((m) => m.id === id) || {}).name || id;

  // Transcript: what the assistant actually heard and said (voice or text).
  const convo = s.activity.filter((a) => a.kind === "user" || a.kind === "agent").reverse();
  $("transcript").innerHTML = convo.map((a) =>
    `<div class="msg ${a.kind}"><div class="who">${a.kind === "user" ? "You" : "HomePilot"} · ${hhmm(a.ts)}</div>${esc(a.detail)}</div>`).join("")
    || '<div class="muted">The conversation will appear here.</div>';
  $("transcript").scrollTop = 1e6;

  // Waiting for a yes (human in the loop).
  const p = s.pending[0];
  $("pending").classList.toggle("hidden", !p);
  if (p) $("pending").innerHTML = `<b>Waiting for your OK</b> &nbsp;${esc(p.summary)} <span class="muted">Say "yes" or "no".</span>`;

  // Repeat-fault insight.
  $("insight").classList.toggle("hidden", !s.insight);
  if (s.insight) $("insight").innerHTML = `<b>Repeat fault: ${esc(s.insight.code)} &times;${s.insight.previous + 1}</b> in ${s.insight.first_seen_days_ago} days. Likely root cause: ${esc(s.insight.likely_root_cause)}. <span class="muted">${esc(s.insight.recommendation)}</span>`;

  // Family calendar (from one hour ago).
  const since = new Date(new Date(s.now.replace(" ", "T")).getTime() - 36e5);
  const events = s.events.filter((e) => new Date(e.start_at.replace(" ", "T")) >= since);
  $("calendar").innerHTML = events.length ? `<table><tr><th>When</th><th>What</th><th>Who</th></tr>${events.map((e) =>
    `<tr class="${e.source.startsWith("booking") ? "new-row" : ""}"><td>${esc(fmtTime(e.start_at))}</td><td>${esc(e.title)} <a class="gcal" href="${esc(e.gcal || "")}" target="_blank" rel="noopener" title="Add to Google Calendar">+ cal</a></td><td>${esc(nameOf(e.member))}</td></tr>`).join("")}</table>`
    : '<div class="muted">Nothing scheduled.</div>';

  // Home issue: appliance, history, booking.
  const a = s.appliance, bk = s.bookings[0];
  $("issue-status").textContent = bk ? "technician booked" : a ? "needs attention" : "-";
  $("issue-status").className = "pill " + (bk ? "resolved" : a ? "waiting_for_parts" : "");
  if (a) {
    $("issue").classList.remove("muted");
    $("issue").innerHTML = `<div class="title">${esc(a.name)}</div>
      <div class="kv"><span>Model <b>${esc(a.model)}</b></span><span>Room <b>${esc(a.room)}</b></span><span>Warranty to <b>${esc(a.warranty_until)}</b></span></div>
      ${bk ? `<div class="notes"><b>${esc(bk.id)}</b> · ${esc(bk.provider)} · ${esc(fmtTime(bk.slot_at))} · ₹${bk.fee}</div>` : ""}
      <h3 style="margin-top:12px">Service history</h3>
      ${s.history.map((h) => `<div class="hist"><div class="d">${esc(h.date)} · ${esc(h.by_whom)}</div>${esc(h.summary)}</div>`).join("")}`;
  } else { $("issue").classList.add("muted"); $("issue").textContent = "No appliance problem reported."; }

  // Tasks and shopping.
  $("tasks").innerHTML = s.tasks.map((t) =>
    `<div class="task ${t.status}"><span class="box">${t.status === "done" ? "✓" : ""}</span><span>${esc(t.title)} <span class="muted">· ${esc(nameOf(t.assignee))}${t.due_at && t.status !== "done" ? " · " + esc(fmtTime(t.due_at)) : ""}</span></span></div>`).join("")
    || '<div class="muted">No tasks.</div>';
  const todo = s.shopping.filter((x) => x.status === "needed"), got = s.shopping.filter((x) => x.status !== "needed");
  $("shop-total").textContent = todo.length ? `≈ ₹${s.shopping_total}` : got.length ? "all done" : "-";
  $("shop-total").className = "pill " + (todo.length ? "" : got.length ? "resolved" : "");
  $("shopping").innerHTML = s.shopping.length ? `<ul class="shop-list shop-dash">${[...todo, ...got].map((x) => {
    const done = x.status !== "needed";
    const by = x.status === "ordered" ? "ordered online" : x.bought_by ? "bought by " + esc(x.bought_by) : done ? "bought" : "";
    return `<li><button class="tick ${done ? "on" : ""}" data-shop="${x.id}" ${x.status === "ordered" ? "disabled" : ""} aria-label="${done ? "Untick" : "Tick"} ${esc(x.item)}">${done ? "✓" : ""}</button><span style="${done ? "text-decoration:line-through;color:var(--muted)" : ""}">${esc(x.item)}</span>${by ? ` <small class="muted">${by}</small>` : ""}</li>`;
  }).join("")}</ul>` : '<div class="muted">List is empty.</div>';
  document.querySelectorAll("[data-shop]").forEach((b) => b.onclick = async () => {
    await api("/api/shopping/toggle", { method: "POST", body: JSON.stringify({ id: b.dataset.shop, by: "Anika" }) }).catch(() => {});
    refresh();
  });
  const go = s.grocery_orders && s.grocery_orders[0];
  $("shop-order").innerHTML = go ? `🚚 <b>${esc(go.id)}</b> · ₹${go.total} · arrives ${esc(fmtTime(go.slot_at))}` : "";

  // Reminders and family channel.
  $("reminders").innerHTML = s.reminders.map((r) =>
    `<div class="hist">⏰ <b>${esc(fmtTime(r.remind_at))}</b> ${esc(r.text)}<div class="d">for ${esc(nameOf(r.member))}</div></div>`).join("")
    || '<div class="muted">No reminders.</div>';
  $("notifications").innerHTML = s.notifications.map((n) =>
    `<div class="n"><div class="meta">${esc(n.recipient)} · ${hhmm(n.created_at)} · ${n.delivered ? "delivered to Slack" : "logged"}</div>${esc(n.message.slice(0, 400))}</div>`).join("")
    || '<div class="muted">No messages yet.</div>';

  $("activity").innerHTML = s.activity.filter((x) => !["user", "agent", "tool", "llm"].includes(x.kind)).map((x) =>
    `<div class="ev"><span class="t">${hhmm(x.ts)}</span><span class="tag k-${x.kind}">${esc(x.kind)}</span><span title="${esc(x.detail)}"><b>${esc(x.title)}</b> <span class="d">${esc(x.detail)}</span></span></div>`).join("")
    || '<div class="muted">Actions the assistant takes will stream here.</div>';

  // KPIs.
  const now = new Date(s.now.replace(" ", "T"));
  $("kpi-events").textContent = s.events.filter((e) => { const d = new Date(e.start_at.replace(" ", "T")); return d >= now && d - now < 864e5; }).length;
  $("kpi-tasks").textContent = s.tasks.filter((t) => t.status === "open").length;
  $("kpi-actions").textContent = s.activity.filter((x) => ACTION_KINDS.includes(x.kind)).length;
  $("kpi-shop").textContent = s.shopping.filter((x) => x.status === "needed").length;
  $("kpi-spent").textContent = "\u20B9" + (s.spent_week || 0).toLocaleString("en-IN");
  $("saved").textContent = s.activity.reduce((m, x) => m + (MINUTES[x.kind] || 0), 0);

  // Alarms that fired since the page loaded.
  const newest = s.fired[0];
  if (newest) {
    if (lastAlarmId === null) lastAlarmId = newest.id;           // don't replay old ones on load
    else if (newest.id !== lastAlarmId) { lastAlarmId = newest.id; showAlarm(newest); }
  } else if (lastAlarmId === null) lastAlarmId = 0;

  // Invites in the last 15 minutes: one QR (general join link) + status per person.
  const nowT = new Date(s.now.replace(" ", "T"));
  const invs = (s.invites || []).filter((i) => nowT - new Date(i.at.replace(" ", "T")) < 15 * 60000);
  const card = $("invite");
  lastInviteAt = invs.length ? invs[invs.length - 1].at : "";
  if (invs.length && card.dataset.hidden !== lastInviteAt) {
    card.classList.remove("hidden");
    const withLink = invs.find((i) => i.link) || invs[0];
    const general = (withLink.link || location.origin + "/expert.html").split("?")[0];
    if (card.dataset.link !== general) {
      card.dataset.link = general;
      $("invite-link").value = general;
      if (window.qrcode) { const q = qrcode(0, "M"); q.addData(general); q.make(); $("invite-qr").innerHTML = q.createSvgTag({ cellSize: 4, margin: 2, scalable: true }); }
    }
    $("invite-who").innerHTML = invs.map((i) =>
      `<li class="${i.joined ? "joined" : ""}">${i.joined ? "✓" : "…"} <b>${esc(i.who)}</b> ${i.joined ? "joined" : "waiting to join"}${i.guest ? ' <span class="muted">(guest)</span>' : ""}</li>`).join("");
  } else card.classList.add("hidden");

  // Store order placed over MCP: payment QR, then the delivery progress.
  const so = (s.store_orders || [])[0], sc = s.store_cart;
  const storeCard = $("store");
  if (so) {
    storeCard.classList.remove("hidden");
    const link = location.origin + so.payment_link;
    if (storeCard.dataset.link !== link) {
      storeCard.dataset.link = link;
      if (window.qrcode) { const q = qrcode(0, "M"); q.addData(link); q.make(); $("store-qr").innerHTML = q.createSvgTag({ cellSize: 4, margin: 2, scalable: true }); }
    }
    const waiting = so.payment === "awaiting_approval";
    $("store-qr").style.display = waiting ? "" : "none";
    storeCard.style.gridTemplateColumns = waiting ? "" : "1fr";
    $("store-title").textContent = `${so.items.join(", ")} · ₹${so.total_inr}`;
    const order = ["awaiting_payment", "preparing", "out_for_delivery", "delivered"];
    const names = ["Approve on phone", "Preparing", "On the way", "Delivered"];
    const at = so.payment === "declined" ? -1 : order.indexOf(so.status);
    $("store-steps").innerHTML = names.map((n, i) => `<span class="${i < at || so.status === "delivered" ? "done" : i === at ? "now" : ""}">${n}</span>`).join("");
    $("store-sub").innerHTML = so.payment === "declined" ? "Payment declined on the phone. Nothing was ordered."
      : waiting ? `Scan to approve <b>₹${so.total_inr}</b> on your phone · <a href="${so.payment_link}" target="_blank" rel="noopener">open payment page</a>`
      : `${esc(so.order_id)} · arrives around ${esc((so.arrives_at || "").slice(11, 16))}`;
  } else if (sc && sc.items && sc.items.length) {
    storeCard.classList.remove("hidden");
    $("store-qr").style.display = "none"; storeCard.style.gridTemplateColumns = "1fr";
    $("store-title").textContent = `Cart: ${sc.items.map((l) => `${l.qty} × ${l.name}`).join(", ")} · ₹${sc.total_inr}`;
    $("store-steps").innerHTML = ""; $("store-sub").textContent = "Say “yes” to place the order.";
  } else storeCard.classList.add("hidden");

  // Family member on the live call.
  const chip = $("chip-expert");
  chip.classList.toggle("hidden", !s.expert_called);
  if (s.expert_called) { const inCall = remoteUsers.size >= 2; chip.textContent = inCall ? "Family call: joined" : "Family call: invited"; chip.className = "chip expert" + (inCall ? " on" : ""); }
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { console.warn(e); } }
setInterval(refresh, 1500);
refresh();

// Real calendar sync: the family calendar as a live .ics feed (bookings, deliveries, reminders).
(function calendarSync() {
  const feed = location.origin + "/calendar.ics";
  const webcal = feed.replace(/^https?:/, "webcal:");
  const g = document.getElementById("cal-google"), p = document.getElementById("cal-phone"), c = document.getElementById("cal-copy");
  if (g) g.href = "https://calendar.google.com/calendar/render?cid=" + encodeURIComponent(webcal);
  if (p) p.href = webcal;
  if (c) c.onclick = async () => {
    try { await navigator.clipboard.writeText(feed); c.textContent = "Copied"; } catch { prompt("Calendar feed link:", feed); }
    setTimeout(() => (c.textContent = "Copy link"), 1500);
  };
})();
