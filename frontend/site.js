// HomePilot site: call replay with waveform + agent trace, day carousel,
// sticky "how it works" panel, guardrail demo, timed text console.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- nav ---------- */
const nav = $(".nav");
addEventListener("scroll", () => nav.classList.toggle("stuck", scrollY > 8), { passive: true });
$("#menu").addEventListener("click", (e) => {
  const open = $("#nav-links").classList.toggle("open");
  e.currentTarget.setAttribute("aria-expanded", open);
});
$$("#nav-links a").forEach((a) => a.addEventListener("click", () => $("#nav-links").classList.remove("open")));

/* ---------- reveal on scroll ---------- */
const reveal = new IntersectionObserver((es) => es.forEach((e) => {
  if (e.isIntersecting) { e.target.classList.add("in"); reveal.unobserve(e.target); }
}), { threshold: 0.12 });
$$(".section-head, .index li, .moment, .pipeline, .code, .pending, .console, .close-row").forEach((el, i) => {
  el.classList.add("rv"); el.style.transitionDelay = `${(i % 5) * 60}ms`; reveal.observe(el);
});

/* ---------- waveform ---------- */
const wave = $("#wave"), ctx = wave.getContext("2d");
let speaking = 0;            // 0 idle, 1 user, 2 agent
function sizeWave() { wave.width = wave.clientWidth * devicePixelRatio; wave.height = 56 * devicePixelRatio; }
sizeWave(); addEventListener("resize", sizeWave);
const bars = 64, level = new Array(bars).fill(0.08);
function drawWave(t) {
  const w = wave.width, h = wave.height, gap = w / bars;
  ctx.clearRect(0, 0, w, h);
  for (let i = 0; i < bars; i++) {
    const target = speaking
      ? 0.15 + 0.85 * Math.abs(Math.sin(t / 180 + i * 0.55) * Math.sin(t / 410 + i * 0.21))
      : 0.06 + 0.04 * Math.sin(t / 600 + i);
    level[i] += (target - level[i]) * 0.18;
    const bh = Math.max(2, level[i] * h * 0.9);
    ctx.fillStyle = speaking === 2 ? "#F08C6A" : speaking === 1 ? "#EDE8DD" : "#4A463B";
    ctx.fillRect(i * gap + gap * 0.25, (h - bh) / 2, gap * 0.5, bh);
  }
  if (!calm) requestAnimationFrame(drawWave);
}
requestAnimationFrame(drawWave);

/* ---------- call replay ---------- */
const CALL = [
  { t: 1, who: "agent", text: "Good afternoon, Anika. Dadi’s check-up is at 12 tomorrow, and Kabir still owes the recycling." ,
    trace: [["fn", "get_today()"], ["", "3 events · 2 open chores"]] },
  { t: 7, who: "you", text: "The living room AC is showing E4 again." },
  { t: 9, who: "agent", text: "That’s the third E4 in 64 days, and the fan capacitor was flagged last time. Switch it off at the breaker for five minutes first.",
    trace: [["fn", "get_appliance(\"living room AC\")"], ["warn", "repeat fault · E4 ×3 / 64 days"], ["fn", "search_manual(\"E4\")"]] },
  { t: 17, who: "you", text: "Can you get someone to fix it?" },
  { t: 19, who: "agent", text: "CoolCare’s 12:00 slot clashes with Dadi’s appointment, so I picked 2:30 for 499 rupees. Shall I book it?",
    trace: [["fn", "find_service_slots()"], ["warn", "skip 12:00 · clash: Dadi’s check-up"], ["fn", "book_service(\"CoolCare\", \"14:30\")"], ["", "awaiting_confirmation"]] },
  { t: 26, who: "you", text: "Yes." },
  { t: 27, who: "agent", text: "Booked. It’s on the family calendar, with a reminder at 2.",
    trace: [["fn", "confirm_action(approved=true)"], ["ok", "BK-7001 · calendar + reminder"]] },
];
const ts = (s) => `00:${String(s).padStart(2, "0")}`;
async function replay() {
  const list = $("#transcript"), trace = $("#trace"), clock = $("#call-clock");
  for (;;) {
    list.innerHTML = ""; trace.innerHTML = "";
    let now = 0;
    for (const line of CALL) {
      await sleep(calm ? 400 : (line.t - now) * 420);
      now = line.t; clock.textContent = ts(now);
      speaking = line.who === "agent" ? 2 : 1;
      const li = document.createElement("li");
      li.className = line.who;
      li.innerHTML = `<span class="ts">${ts(line.t)}</span><span><span class="who">${line.who === "agent" ? "HomePilot" : "Anika"}</span>${esc(line.text)}</span>`;
      list.appendChild(li);
      while (list.scrollHeight > list.clientHeight && list.children.length > 2) list.firstElementChild.remove();
      for (const [cls, txt] of line.trace || []) {
        await sleep(calm ? 0 : 260);
        const t = document.createElement("li");
        t.innerHTML = cls === "fn" ? `<span class="fn">→ ${esc(txt)}</span>` : `<span class="${cls}">  ${esc(txt)}</span>`;
        trace.appendChild(t);
        while (trace.scrollHeight > trace.clientHeight && trace.children.length > 2) trace.firstElementChild.remove();
      }
      await sleep(calm ? 600 : Math.min(3200, 900 + line.text.length * 28));
      speaking = 0;
    }
    await sleep(4000);
  }
}
replay();

/* ---------- day carousel ---------- */
const track = $("#track"), bar = $("#progress");
const step = () => ($(".moment", track).offsetWidth + 18);
$("#prev").addEventListener("click", () => track.scrollBy({ left: -step(), behavior: "smooth" }));
$("#next").addEventListener("click", () => track.scrollBy({ left: step(), behavior: "smooth" }));
track.addEventListener("keydown", (e) => {
  if (e.key === "ArrowRight") { e.preventDefault(); track.scrollBy({ left: step(), behavior: "smooth" }); }
  if (e.key === "ArrowLeft") { e.preventDefault(); track.scrollBy({ left: -step(), behavior: "smooth" }); }
});
const updateBar = () => {
  const max = track.scrollWidth - track.clientWidth;
  bar.style.width = `${max > 0 ? (track.scrollLeft / max) * 100 : 100}%`;
};
track.addEventListener("scroll", updateBar, { passive: true }); updateBar();
// drag to scroll with the mouse (touch already scrolls natively)
let down = false, startX = 0, startLeft = 0, moved = false;
track.addEventListener("pointerdown", (e) => {
  if (e.pointerType !== "mouse") return;
  down = true; moved = false; startX = e.clientX; startLeft = track.scrollLeft; track.classList.add("drag");
});
addEventListener("pointermove", (e) => {
  if (!down) return;
  const dx = e.clientX - startX; if (Math.abs(dx) > 4) moved = true;
  track.scrollLeft = startLeft - dx;
});
addEventListener("pointerup", () => {
  if (!down) return; down = false; track.classList.remove("drag");
  // snap to the nearest card after a drag
  const s = step(); track.scrollTo({ left: Math.round(track.scrollLeft / s) * s, behavior: "smooth" });
});

/* ---------- sticky "how it works" panel ---------- */
const PANELS = [
  { title: "Live context · every turn", body: `<dl class="kv">
      <dt>family</dt><dd>Anika, Rohan, Kabir (10), Dadi (72)</dd>
      <dt>today</dt><dd>Rohan’s call 10:00 · Dadi’s check-up 12:00</dd>
      <dt>open chores</dt><dd>Recycling (Kabir) · Electricity bill (Rohan)</dd>
      <dt>rules</dt><dd>Vegetarian · repairs under ₹1,500 · no visits after 8 PM</dd>
      <dt>remembered</dt><dd>Anika’s birthday, 22 August</dd></dl>` },
  { title: "Turn detection · Agora", body: `<dl class="kv">
      <dt>speech</dt><dd>Streaming ASR in the RTC channel</dd>
      <dt>end of turn</dt><dd>Semantic, with pause detection (“hold on…”)</dd>
      <dt>barge-in</dt><dd>Talk over it and it stops to listen</dd>
      <dt>voice</dt><dd>Murf Falcon, streamed sentence by sentence</dd>
      <dt>turn time</dt><dd>~2 s including tool calls, measured</dd></dl>` },
  { title: "Tool call · with a gate", body: `<dl class="kv">
      <dt>request</dt><dd>“Get someone to fix it.”</dd>
      <dt>tool</dt><dd class="mono">find_service_slots()</dd>
      <dt>skipped</dt><dd>12:00 · clashes with Dadi’s check-up</dd>
      <dt>prepared</dt><dd class="mono">book_service → awaiting_confirmation</dd>
      <dt>executes</dt><dd>Only after a spoken “yes”</dd></dl>` },
  { title: "Follow-up · the same evening", body: `<dl class="kv">
      <dt>20:00</dt><dd>Spoken: “Dadi, time for your BP tablet”</dd>
      <dt>chores</dt><dd>Recycling ✓ (Kabir)</dd>
      <dt>calendar</dt><dd>AC repair tomorrow 14:30, reminder 14:00</dd>
      <dt>family</dt><dd>One summary posted to the family channel</dd></dl>` },
];
const panel = $("#how-panel");
const showPanel = (i) => {
  panel.innerHTML = `<div class="panel-swap"><p class="panel-title">${PANELS[i].title}</p>${PANELS[i].body}</div>`;
  $$(".how-step").forEach((s) => s.classList.toggle("on", +s.dataset.step === i));
};
showPanel(0);
const stepObs = new IntersectionObserver((es) => es.forEach((e) => {
  if (e.isIntersecting) showPanel(+e.target.dataset.step);
}), { rootMargin: "-45% 0px -45% 0px" });
$$(".how-step").forEach((s) => stepObs.observe(s));

/* ---------- guardrail demo ---------- */
const pending = $("#pending"), verdict = $("#verdict");
$$("#pending [data-say]").forEach((b) => b.addEventListener("click", () => {
  const yes = b.dataset.say === "yes";
  pending.classList.add("done");
  $$("#pending button").forEach((x) => (x.disabled = true));
  verdict.innerHTML = yes
    ? `<div class="v yes"><b>Booked BK-7001.</b> Added to the calendar for 14:30, reminder at 14:00.<span class="mono">confirm_action(approved=true) → done</span><button id="reset-g">Reset</button></div>`
    : `<div class="v no"><b>Cancelled. Nothing was booked.</b> Even if the model had approved it, the server would have blocked it.<span class="mono">_said_no("no, don't") → True · approved = False</span><button id="reset-g">Reset</button></div>`;
  $("#reset-g").addEventListener("click", () => {
    verdict.innerHTML = ""; pending.classList.remove("done");
    $$("#pending button").forEach((x) => (x.disabled = false));
  });
}));

/* ---------- text console (real agent) ---------- */
const log = $("#log"), q = $("#q"), send = $("#send"), lat = $("#latency");
const history = [];
function line(cls, html) {
  const p = document.createElement("p"); p.className = `line ${cls}`; p.innerHTML = html;
  log.appendChild(p); log.scrollTop = log.scrollHeight; return p;
}
async function ask(text) {
  text = text.trim(); if (!text || send.disabled) return;
  line("you", esc(text)); history.push({ role: "user", content: text });
  const wait = line("wait mono", "thinking…"); send.disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages: history }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "error");
    const ms = Math.round(performance.now() - t0);
    lat.textContent = `${(ms / 1000).toFixed(1)} s round trip`;
    wait.remove();
    line("bot", `<span class="who">HomePilot</span>${esc(data.reply || "(no reply)")}`);
    history.push({ role: "assistant", content: data.reply });
  } catch {
    wait.remove(); history.pop();
    line("sys mono", "Couldn’t reach the agent. Is the server running? Try the app at /app.html.");
  } finally { send.disabled = false; }
}
$("#ask").addEventListener("submit", (e) => { e.preventDefault(); const t = q.value; q.value = ""; ask(t); });
$$("#prompts button").forEach((b) => b.addEventListener("click", () => { ask(b.textContent); $("#try").scrollIntoView({ behavior: "smooth", block: "nearest" }); }));
