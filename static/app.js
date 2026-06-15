"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let signedIn = false;
let pollTimer = null;

// --- Toast -----------------------------------------------------------------
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
}

// --- Status ----------------------------------------------------------------
async function loadStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    signedIn = s.signed_in;
    renderChips(s);
  } catch (e) {
    renderChips({ graph_configured: false });
  }
}

function renderChips(s) {
  const chips = $("#status-chips");
  chips.innerHTML = "";

  // Microsoft chip
  let msCls, msText;
  if (!s.graph_configured) { msCls = "warn"; msText = "Graph not configured"; }
  else if (s.signed_in) { msCls = "ok"; msText = "MS: " + (s.account || "signed in"); }
  else { msCls = "warn clickable"; msText = "Sign in to Microsoft"; }
  const ms = el("span", "chip " + msCls, `<span class="dot"></span>${esc(msText)}`);
  if (s.graph_configured && !s.signed_in) ms.onclick = startSignin;
  chips.appendChild(ms);

  // Claude chip
  chips.appendChild(el("span", "chip " + (s.claude_enabled ? "ok" : "off"),
    `<span class="dot"></span>Claude ${s.claude_enabled ? "on" : "off (keyword mode)"}`));

  // Contacts chip
  if (s.has_contacts) chips.appendChild(el("span", "chip ok", `<span class="dot"></span>Contacts loaded`));
}

// --- Sign-in flow ----------------------------------------------------------
async function startSignin() {
  openModal("signin-modal");
  const body = $("#signin-body");
  body.innerHTML = `<p><span class="spinner"></span> Starting sign-in…</p>`;
  try {
    const r = await fetch("/api/signin/start", { method: "POST" });
    const d = await r.json();
    if (!r.ok) { body.innerHTML = `<p class="muted">${esc(d.error || "Failed to start")}</p>`; return; }
    if (d.signed_in) { closeModal("signin-modal"); toast("Already signed in"); loadStatus(); return; }
    body.innerHTML = `
      <p>1. Open <a href="${esc(d.verification_uri)}" target="_blank" rel="noopener">${esc(d.verification_uri)}</a></p>
      <p>2. Enter this code:</p>
      <div class="code-box">${esc(d.user_code)}</div>
      <p class="muted"><span class="spinner"></span> Waiting for you to finish signing in…</p>`;
    pollSignin();
  } catch (e) {
    body.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
  }
}

function pollSignin() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const d = await (await fetch("/api/signin/poll")).json();
      if (d.status === "done") {
        clearInterval(pollTimer);
        closeModal("signin-modal");
        toast("Signed in as " + (d.account || "you"));
        loadStatus();
      } else if (d.status === "error") {
        clearInterval(pollTimer);
        $("#signin-body").innerHTML = `<p class="muted">Sign-in failed: ${esc(d.error)}</p>`;
      }
    } catch (e) { /* keep polling */ }
  }, 2500);
}

// --- Upload ----------------------------------------------------------------
const dz = $("#dropzone");
$("#browse-btn").onclick = () => $("#file-input").click();
$("#file-input").onchange = (e) => { if (e.target.files[0]) uploadFile(e.target.files[0]); };
["dragover", "dragenter"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f) uploadFile(f); });

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  try {
    const r = await fetch("/api/upload", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) { toast(d.error || "Upload failed", true); return; }
    renderContacts(d);
    toast(`Loaded ${d.count} contact${d.count === 1 ? "" : "s"}`);
    loadStatus();
  } catch (e) { toast(e.message, true); }
}

function renderContacts(d) {
  const sum = $("#contacts-summary");
  sum.classList.remove("hidden");
  const detected = Object.keys(d.columns).join(", ") || "none";
  sum.textContent = `${d.count} contacts · detected columns: ${detected}`;

  const t = $("#contacts-table");
  t.classList.remove("hidden");
  const head = "<thead><tr><th>#</th><th>Name</th><th>Email</th><th>Title</th><th>Company</th><th>Override</th></tr></thead>";
  const rows = d.contacts.map((c, i) =>
    `<tr><td>${i + 1}</td><td>${esc(c.first_name)} ${esc(c.last_name)}</td><td>${esc(c.email)}</td>` +
    `<td>${esc(c.title)}</td><td>${esc(c.company)}</td><td>${esc(c.category_override)}</td></tr>`).join("");
  t.innerHTML = head + "<tbody>" + rows + "</tbody>";
}

// --- Options helpers -------------------------------------------------------
function readOptions() {
  const limit = $("#limit").value ? parseInt($("#limit").value, 10) : null;
  return {
    test_email: $("#test-email").value.trim(),
    dry_run: $("#dry-run").checked,
    use_claude: $("#use-claude").checked,
    limit,
    delay: parseFloat($("#delay").value || "2"),
    body_type: $("#body-type").value,
  };
}

// --- Preview ---------------------------------------------------------------
$("#preview-btn").onclick = async () => {
  const btn = $("#preview-btn");
  btn.disabled = true; btn.textContent = "Building…";
  try {
    const opts = readOptions();
    const r = await fetch("/api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: opts.limit, use_claude: opts.use_claude }),
    });
    const d = await r.json();
    if (!r.ok) { toast(d.error || "Preview failed", true); return; }
    renderPreview(d);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; btn.textContent = "Preview emails"; }
};

function renderPreview(d) {
  $("#preview-card").classList.remove("hidden");
  $("#preview-count").textContent = `· ${d.count} email${d.count === 1 ? "" : "s"}` +
    (d.claude_used ? "" : " · keyword mode");
  const list = $("#preview-list");
  list.innerHTML = "";
  d.contacts.forEach((c) => {
    const row = el("div", "preview-row");
    row.innerHTML =
      `<span class="badge ${c.category}">${esc(c.category)}</span>` +
      `<div class="grow"><div class="who">${esc(c.first_name) || "(no name)"} ` +
      `<span class="meta">· ${esc(c.email)}</span></div>` +
      `<div class="subject">${esc(c.subject)}</div></div>`;
    row.onclick = () => openEmail(c);
    list.appendChild(row);
  });
  $("#preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function openEmail(c) {
  $("#modal-to").textContent = "To: " + c.email + "   ·   " + c.category;
  $("#modal-subject").textContent = c.subject;
  $("#modal-body").textContent = c.body;
  openModal("email-modal");
}

// --- Send ------------------------------------------------------------------
$("#send-btn").onclick = async () => {
  const opts = readOptions();
  if (!opts.dry_run && !signedIn) {
    toast("Sign in to Microsoft first", true);
    startSignin();
    return;
  }
  if (!opts.dry_run) {
    const who = opts.test_email ? `as a TEST to ${opts.test_email}` : "to the REAL recipients";
    if (!confirm(`Send emails ${who}?`)) return;
  }

  const card = $("#progress-card");
  card.classList.remove("hidden");
  card.scrollIntoView({ behavior: "smooth", block: "start" });
  $("#progress-list").innerHTML = "";
  $("#progress-fill").style.width = "0%";
  $("#progress-summary").textContent = "Starting…";
  const sendBtn = $("#send-btn"); sendBtn.disabled = true;

  try {
    const r = await fetch("/api/send", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts),
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "Send failed", true); return; }
    await readStream(r.body, handleEvent);
  } catch (e) { toast(e.message, true); }
  finally { sendBtn.disabled = false; loadStatus(); loadFollowupStatus(); }
};

async function readStream(body, handler) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) handler(JSON.parse(line));
    }
  }
}

function handleEvent(ev) {
  if (ev.type === "start") {
    $("#progress-summary").textContent =
      (ev.dry_run ? "Dry run · " : "") + `Processing ${ev.total} contact${ev.total === 1 ? "" : "s"}…`;
  } else if (ev.type === "progress") {
    const pct = Math.round((ev.index / ev.total) * 100);
    $("#progress-fill").style.width = pct + "%";
    const row = el("div", "progress-row");
    row.innerHTML =
      `<span class="badge ${ev.category}">${esc(ev.category)}</span>` +
      `<span class="grow">${esc(ev.first_name) || "(no name)"} · ${esc(ev.recipient)}</span>` +
      `<span class="status-pill ${ev.status}">${esc(ev.status)}</span>`;
    if (ev.status === "error") row.title = ev.detail;
    $("#progress-list").appendChild(row);
    $("#progress-list").scrollTop = $("#progress-list").scrollHeight;
  } else if (ev.type === "done") {
    const verb = $("#dry-run").checked ? "previewed" : "sent";
    let msg = `Done · ${ev.sent} ${verb}`;
    if (ev.failed) msg += ` · ${ev.failed} failed`;
    $("#progress-summary").textContent = msg;
    toast(msg, ev.failed > 0);
  }
}

// --- Modals ----------------------------------------------------------------
function openModal(id) { $("#" + id).classList.remove("hidden"); }
function closeModal(id) { $("#" + id).classList.add("hidden"); if (id === "signin-modal") clearInterval(pollTimer); }
document.querySelectorAll("[data-close]").forEach((b) => b.onclick = () => closeModal(b.dataset.close));
document.querySelectorAll(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) closeModal(m.id); }));

// --- Follow-ups ------------------------------------------------------------
function fuStat(num, lbl, extra) {
  return `<div class="fu-stat"><span class="num">${num}</span>` +
    (extra ? `<span class="sub">${extra}</span>` : "") +
    `<span class="lbl">${lbl}</span></div>`;
}

async function loadFollowupStatus() {
  try {
    const d = await (await fetch("/api/followups/status")).json();
    const r = d.report;
    $("#followup-intervals").textContent =
      "Sequence: initial, then follow-ups after " + d.intervals.join(", ") + " days (stops on reply).";
    if (!r.contacts) {
      $("#followup-stats").innerHTML = `<span class="muted">No contacts in a sequence yet — send some emails first.</span>`;
      $("#followup-breakdown").textContent = "";
      return;
    }
    $("#followup-stats").innerHTML = [
      fuStat(r.total_emails, "emails sent"),
      fuStat(r.replied, "replied", r.reply_rate + "%"),
      fuStat(r.active, "in sequence"),
      fuStat(r.due_now, "due now"),
      fuStat(r.bounced, "bounced"),
      fuStat(r.completed, "completed"),
    ].join("");
    $("#followup-breakdown").textContent =
      `${r.contacts} contacts · ${r.initial_sent} initial + ${r.followups_sent} follow-ups sent` +
      (r.errored ? ` · ${r.errored} errored` : "");
  } catch (e) { /* ignore */ }
}

async function runFollowups(dryRun) {
  const btn = dryRun ? $("#fu-dry") : $("#fu-run");
  if (!dryRun && !signedIn) { toast("Sign in to Microsoft first", true); startSignin(); return; }
  if (!dryRun && !confirm("Detect replies and send all due follow-ups now?")) return;
  btn.disabled = true;
  $("#fu-progress").innerHTML = "";
  try {
    const r = await fetch("/api/followups/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: dryRun, use_claude: $("#use-claude").checked, delay: parseFloat($("#delay").value || "2") }),
    });
    if (!r.ok) { const d = await r.json().catch(() => ({})); toast(d.error || "Failed", true); return; }
    await readStream(r.body, handleFollowupEvent);
  } catch (e) { toast(e.message, true); }
  finally { btn.disabled = false; loadFollowupStatus(); loadStatus(); }
}

function handleFollowupEvent(ev) {
  const list = $("#fu-progress");
  if (ev.type === "reply") {
    list.appendChild(el("div", "progress-row",
      `<span class="status-pill ${ev.status === "replied" ? "sent" : "error"}">${esc(ev.status)}</span>` +
      `<span class="grow">${esc(ev.email)}</span>`));
  } else if (ev.type === "detect_done") {
    list.appendChild(el("div", "progress-row muted",
      `<span class="grow">Detected ${ev.replied} replied, ${ev.bounced} bounced${ev.detect_ok ? "" : " (detection FAILED)"}</span>`));
  } else if (ev.type === "detect_error") {
    toast("Reply detection failed — check Mail.Read permission", true);
  } else if (ev.type === "aborted") {
    list.appendChild(el("div", "progress-row", `<span class="status-pill error">aborted</span><span class="grow">${esc(ev.reason)}</span>`));
  } else if (ev.type === "followup") {
    list.appendChild(el("div", "progress-row",
      `<span class="status-pill ${ev.status === "preview" ? "preview" : (ev.status === "sent" ? "sent" : "error")}">FU#${ev.step} ${esc(ev.status)}</span>` +
      `<span class="grow">${esc(ev.email)} · ${esc(ev.subject)}</span>`));
    list.scrollTop = list.scrollHeight;
  } else if (ev.type === "done") {
    const msg = `Follow-ups: ${ev.sent} sent, ${ev.due} due, ${ev.replied} replied, ${ev.bounced} bounced`;
    list.appendChild(el("div", "progress-row muted", `<span class="grow">${msg}</span>`));
    toast(msg, ev.failed > 0);
  }
}

$("#fu-refresh").onclick = loadFollowupStatus;
$("#fu-dry").onclick = () => runFollowups(true);
$("#fu-run").onclick = () => runFollowups(false);

// --- Init ------------------------------------------------------------------
loadStatus();
loadFollowupStatus();
