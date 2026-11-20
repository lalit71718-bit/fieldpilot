// HomePilot website: hero conversation, scroll reveals, counters, "Try it", confirmation demo.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ---------------------------------------------------------------- nav
const nav = $(".nav");
addEventListener("scroll", () => nav.classList.toggle("scrolled", scrollY > 10), { passive: true });
const menuBtn = $("#menu-btn"), links = $(".nav-links");
menuBtn.addEventListener("click", () => {
  const open = links.classList.toggle("open");
  menuBtn.setAttribute("aria-expanded", open);
});
$$(".nav-links a").forEach((a) => a.addEventListener("click", () => links.classList.remove("open")));

// ---------------------------------------------------------------- scroll reveal + counters
const io = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    e.target.classList.add("in");
    $$("[data-count]", e.target.closest(".stats") ? e.target : document.createElement("i")).forEach(countUp);
    io.unobserve(e.target);
  }
}, { threshold: 0.15 });
$$(".reveal").forEach((el, i) => { el.style.transitionDelay = `${(i % 4) * 80}ms`; io.observe(el); });

function countUp(el) {
  if (el.dataset.done) return;
  el.dataset.done = 1;
  const end = +el.dataset.count, pre = el.dataset.prefix || "", suf = el.dataset.suffix || "";
  if (reduced) { el.textContent = pre + end + suf; return; }
  const t0 = performance.now(), dur = 1200;
  const tick = (t) => {
    const p = Math.min(1, (t - t0) / dur), v = Math.round(end * (1 - Math.pow(1 - p, 3)));
    el.textContent = pre + v + suf;
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

// subtle tilt on feature cards
$$(".tilt").forEach((card) => {
  if (reduced) return;
  card.addEventListener("mousemove", (e) => {
    const r = card.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
    card.style.transform = `perspective(700px) rotateX(${-y * 6}deg) rotateY(${x * 6}deg) translateY(-3px)`;
  });
  card.addEventListener("mouseleave", () => { card.style.transform = ""; });
});

// ---------------------------------------------------------------- hero conversation (scripted)
const SCRIPT = [
  { who: "bot", text: "Good evening Anika. Dadi’s doctor visit is tomorrow at 12, and Kabir still has to take out the recycling." },
  { who: "user", text: "The living room AC is showing E4 again." },
  { who: "bot", text: "That’s the third E4 in 64 days. The technician flagged the fan capacitor last time. Switch it off at the breaker for five minutes first.", action: "🔁 Repeat fault detected" },
  { who: "user", text: "Can you get someone to fix it?" },
  { who: "bot", text: "CoolCare at 12 clashes with Dadi’s appointment, so I picked 2:30 PM for 499 rupees. Shall I book it?", action: "📅 Checked family calendar" },
  { who: "user", text: "Yes." },
  { who: "bot", text: "Booked. It’s on the family calendar with a reminder 30 minutes before.", action: "✅ Booked BK-7001" },
  { who: "user", text: "And ask Rohan to pick up milk and Dadi’s BP tablets." },
  { who: "bot", text: "Done. Added to the list and Rohan got the task in the family chat.", action: "🛒 Shopping list +2 · 👨 Task for Rohan" },
];
const chat = $("#hero-chat"), actions = $("#hero-actions"), orb = $("#orb"), status = $("#device-status");

async function playHero() {
  while (true) {
    chat.innerHTML = ""; actions.innerHTML = "";
    for (const line of SCRIPT) {
      if (line.who === "bot") {
        orb.classList.add("speaking"); status.textContent = "Speaking…";
        const typing = document.createElement("div");
        typing.className = "bubble bot typing"; typing.textContent = "…";
        chat.appendChild(typing); trim();
        await sleep(reduced ? 0 : 700);
        typing.remove();
      } else {
        orb.classList.remove("speaking"); status.textContent = "Listening…";
      }
      const b = document.createElement("div");
      b.className = `bubble ${line.who}`; b.textContent = line.text;
      chat.appendChild(b); trim();
      if (line.action) {
        const a = document.createElement("span");
        a.className = "action"; a.textContent = line.action;
        actions.appendChild(a);
      }
      await sleep(reduced ? 1500 : Math.max(1700, line.text.length * 38));
    }
    orb.classList.remove("speaking"); status.textContent = "All done";
    await sleep(3500);
  }
}
function trim() { while (chat.scrollHeight > chat.clientHeight && chat.children.length > 2) chat.firstElementChild.remove(); }
playHero();

// ---------------------------------------------------------------- Try it (real assistant)
const log = $("#try-log"), input = $("#try-input"), form = $("#try-form"), send = $("#try-send");
const history = [];
function addBubble(who, text, cls = "") {
  const b = document.createElement("div");
  b.className = `bubble ${who} ${cls}`; b.textContent = text;
  log.appendChild(b); log.scrollTop = log.scrollHeight;
  return b;
}
async function ask(text) {
  text = text.trim();
  if (!text || send.disabled) return;
  addBubble("user", text);
  history.push({ role: "user", content: text });
  const wait = addBubble("bot", "Thinking…", "typing");
  send.disabled = true;
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ messages: history }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Something went wrong");
    wait.remove();
    addBubble("bot", data.reply || "(no reply)");
    history.push({ role: "assistant", content: data.reply });
  } catch (e) {
    wait.remove();
    addBubble("bot", "I couldn’t reach the assistant right now. Open the app to try again.");
    history.pop();
  } finally { send.disabled = false; }
}
form.addEventListener("submit", (e) => { e.preventDefault(); const t = input.value; input.value = ""; ask(t); });
$$("#try-chips .chip").forEach((c) => c.addEventListener("click", () => ask(c.textContent)));

// ---------------------------------------------------------------- confirmation demo (local)
const card = $("#confirm-card"), result = $("#confirm-result");
$$("#confirm-card [data-answer]").forEach((btn) => btn.addEventListener("click", () => {
  const yes = btn.dataset.answer === "yes";
  card.classList.add("done");
  $$("#confirm-card button").forEach((b) => (b.disabled = true));
  result.innerHTML = yes
    ? `<div class="result yes"><b>✅ Booked BK-7001.</b><ul><li>Added to the family calendar: tomorrow 2:30 PM</li><li>Reminder set for 2:00 PM</li><li>Only now, after your “yes”</li></ul><button class="btn btn-ghost btn-sm" id="again">Try again</button></div>`
    : `<div class="result no"><b>🛑 Cancelled. Nothing was booked.</b><ul><li>“No”, “wait” and “don’t” always cancel</li><li>Enforced in code, even if the AI says yes</li></ul><button class="btn btn-ghost btn-sm" id="again">Try again</button></div>`;
  $("#again").addEventListener("click", () => {
    result.innerHTML = ""; card.classList.remove("done");
    $$("#confirm-card button").forEach((b) => (b.disabled = false));
  });
}));
