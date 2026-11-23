/* Lead Outreach CRM — web UI (vanilla JS, no framework). */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ credentials: "same-origin" }, opts || {}));
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(data.error || ("Request failed (" + r.status + ")"));
  return data;
}
const post = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let templates = [];
let currentLead = null;
let currentConv = null;

async function checkAuth() {
  try {
    const me = await api("/api/me");
    $("#login").classList.add("hidden");
    $("#app").classList.remove("hidden");
    $("#providerBadge").textContent = "Provider: " + me.provider + (me.provider_configured ? "" : " (not set up)");
    $("#providerBadge").classList.toggle("warn", !me.provider_configured);
    $("#capBadge").textContent = "Sent today: " + me.sent_today + " / " + me.daily_cap;
    loadTab("dashboard");
  } catch (e) {
    $("#login").classList.remove("hidden");
    $("#app").classList.add("hidden");
  }
}

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginError").textContent = "";
  try {
    await post("/api/login", { password: $("#password").value });
    $("#password").value = "";
    checkAuth();
  } catch (err) { $("#loginError").textContent = err.message; }
});
$("#logoutBtn").addEventListener("click", async () => { await post("/api/logout", {}); checkAuth(); });

$$(".tab").forEach((b) => b.addEventListener("click", () => loadTab(b.dataset.tab)));
function loadTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.add("hidden"));
  $("#tab-" + name).classList.remove("hidden");
  ({ dashboard: loadDashboard, leads: loadLeads, inbox: loadInbox,
     templates: loadTemplates, import: loadBatches, settings: loadSettings })[name]();
  if (name === "leads") {
    loadCampaigns();
    if ($("#bulkTemplate").options.length <= 1) loadTemplatesInto([$("#bulkTemplate")]);
  }
}

async function loadTemplatesInto(selects) {
  templates = (await api("/api/templates")).templates;
  selects.forEach((sel) => {
    const cur = sel.value;
    sel.innerHTML = '<option value="">— pick a template —</option>' +
      templates.map((t) => '<option value="' + t.id + '">' + esc(t.name) + "</option>").join("");
    sel.value = cur;
  });
}

/* ---- dashboard ---- */
async function loadDashboard() {
  const d = await api("/api/dashboard");
  const labels = { new: "New", texted: "Texted", replied: "Replied", taken_over: "Taken over", closed: "Closed", not_qualified: "Not qualified", do_not_contact: "Do not contact" };
  const camp = d.campaign_counts || {};
  $("#dashCards").innerHTML = Object.keys(labels).map((k) =>
    '<div class="stat stat-link" data-status="' + k + '"><b>' + d.counts[k] + "</b><span>" + labels[k] + "</span></div>").join("") +
    Object.keys(camp).sort().map((c) =>
    '<div class="stat stat-link" data-campaign="' + esc(c) + '"><b>' + camp[c] + "</b><span>" + esc(c) + " campaign</span></div>").join("") +
    '<div class="stat"><b>' + d.sent_today + " / " + d.daily_cap + "</b><span>Sent today / cap</span></div>";
  $$("#dashCards .stat-link[data-status]").forEach((el) => el.addEventListener("click", () => {
    leadPage = 0;
    $("#statusFilter").value = el.dataset.status;
    loadTab("leads");
  }));
  $$("#dashCards .stat-link[data-campaign]").forEach((el) => el.addEventListener("click", async () => {
    // The campaign filter options come from /api/campaigns, so load
    // them before setting the value — otherwise the option doesn't
    // exist yet and the selection is lost.
    leadPage = 0;
    await loadCampaigns();
    $("#campaignFilter").value = el.dataset.campaign;
    loadTab("leads");
  }));
  $("#dashRecent").innerHTML = d.recent.map((n) =>
    "<li>" + esc(n.text) + ' <span class="muted">' + esc(n.created_at) + "</span></li>").join("") ||
    "<li class='muted'>Nothing yet.</li>";
  const unread = d.unread_notifications;
  $("#inboxUnread").textContent = unread;
  $("#inboxUnread").classList.toggle("hidden", !unread);
}

/* ---- leads ---- */
let leadSort = { col: "updated_at", dir: "desc" };
const LEAD_PAGE_SIZE = 500;
let leadPage = 0;
function reloadLeadsFromStart() { leadPage = 0; loadLeads(); }
function shortSite(ws) {
  const s = (ws || "").replace(/^https?:\/\//i, "");
  return s.length > 10 ? s.slice(0, 10) + "…" : s;
}
function renderSortArrows() {
  $$("#leadsTable thead th[data-sort]").forEach((th) => {
    const base = th.dataset.sort === "business" ? "Business" :
      th.dataset.sort === "phone" ? "Phone" : th.dataset.sort === "email" ? "Email" :
      th.dataset.sort === "website" ? "Website" : th.dataset.sort === "address" ? "Address" : th.dataset.sort === "name" ? "Contact name" :
      th.dataset.sort === "source" ? "Generated by" : th.dataset.sort === "campaign" ? "Campaign" : "Status";
    th.textContent = base + (leadSort.col === th.dataset.sort ? (leadSort.dir === "asc" ? " ▲" : " ▼") : "");
  });
}
async function loadCampaigns() {
  const d = await api("/api/campaigns");
  const sel = $("#campaignFilter"), cur = sel.value;
  sel.innerHTML = '<option value="">All campaigns</option>' +
    d.campaigns.map((c) => '<option value="' + esc(c) + '">' + esc(c) + "</option>").join("");
  sel.value = cur;
}
async function loadLeads() {
  const status = $("#statusFilter").value, q = $("#leadSearch").value.trim(),
        campaign = $("#campaignFilter").value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  if (campaign) params.set("campaign", campaign);
  params.set("sort", leadSort.col);
  params.set("dir", leadSort.dir);
  params.set("limit", LEAD_PAGE_SIZE);
  params.set("offset", leadPage * LEAD_PAGE_SIZE);
  const d = await api("/api/leads?" + params.toString());
  renderSortArrows();
  const totalPages = Math.max(1, Math.ceil(d.total / LEAD_PAGE_SIZE));
  if (leadPage > totalPages - 1) { leadPage = totalPages - 1; loadLeads(); return; }
  $("#leadsTable tbody").innerHTML = d.leads.map((l) =>
    '<tr class="rowbtn" data-id="' + l.id + '"><td>' + esc(l.business || "") + "</td><td style=\"white-space:nowrap\">" + esc(l.phone_display || "") +
    "</td><td>" + esc(l.email || "") + "</td><td style=\"white-space:nowrap\">" + (l.website ? '<a href="' + esc(/^https?:\/\//i.test(l.website) ? l.website : "https://" + l.website) + '" target="_blank" rel="noopener">' + esc(shortSite(l.website)) + "</a>" : "") + "</td><td>" + esc(l.address || "") + "</td><td>" + esc(l.name || "—") + "</td><td>" + esc(l.source || "") +
    "</td><td>" + esc(l.campaign || "") +
    "</td><td>" + esc(l.status_label) + (l.opted_out ? " (opted out)" : "") + "</td><td>Open ›</td></tr>").join("");
  const first = d.total === 0 ? 0 : leadPage * LEAD_PAGE_SIZE + 1;
  const last = Math.min(d.total, (leadPage + 1) * LEAD_PAGE_SIZE);
  $("#leadsCount").textContent = first + "–" + last + " of " + d.total + " lead(s)";
  $("#leadsPageLabel").textContent = "Page " + (leadPage + 1) + " of " + totalPages;
  $("#leadsPrev").disabled = leadPage === 0;
  $("#leadsNext").disabled = leadPage >= totalPages - 1;
  $$("#leadsTable tbody tr").forEach((tr) => tr.addEventListener("click", () => openLead(+tr.dataset.id)));
  $$("#leadsTable tbody a").forEach((a) => a.addEventListener("click", (e) => e.stopPropagation()));
}
$("#leadsPrev").addEventListener("click", () => { if (leadPage > 0) { leadPage--; loadLeads(); $("#leadsTable").scrollIntoView(); } });
$("#leadsNext").addEventListener("click", () => { leadPage++; loadLeads(); $("#leadsTable").scrollIntoView(); });
$("#statusFilter").addEventListener("change", reloadLeadsFromStart);
$("#campaignFilter").addEventListener("change", reloadLeadsFromStart);
$$("#leadsTable thead th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const col = th.dataset.sort;
  if (leadSort.col === col) {
    leadSort.dir = leadSort.dir === "asc" ? "desc" : "asc";
  } else {
    leadSort = { col: col, dir: "asc" };
  }
  reloadLeadsFromStart();
}));
$("#leadSearchBtn").addEventListener("click", reloadLeadsFromStart);
$("#leadSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") reloadLeadsFromStart(); });
let leadSearchTimer = null;
$("#leadSearch").addEventListener("input", () => {
  clearTimeout(leadSearchTimer);
  leadSearchTimer = setTimeout(reloadLeadsFromStart, 300);
});
$("#addLeadBtn").addEventListener("click", () => $("#addLeadForm").classList.toggle("hidden"));
$("#nlCancel").addEventListener("click", () => $("#addLeadForm").classList.add("hidden"));
$("#nlSave").addEventListener("click", async () => {
  $("#nlError").textContent = "";
  try {
    await post("/api/leads", { name: $("#nlName").value, business: $("#nlBusiness").value,
      phone: $("#nlPhone").value, email: $("#nlEmail").value, website: $("#nlWebsite").value,
      address: $("#nlAddress").value,
      city: $("#nlCity").value, campaign: $("#nlCampaign").value, notes: $("#nlNotes").value });
    ["#nlName", "#nlBusiness", "#nlPhone", "#nlEmail", "#nlWebsite", "#nlAddress", "#nlCity", "#nlCampaign", "#nlNotes"].forEach((id) => { $(id).value = ""; });
    $("#addLeadForm").classList.add("hidden");
    loadLeads();
  } catch (err) { $("#nlError").textContent = err.message; }
});
$("#bulkTextBtn").addEventListener("click", async () => {
  const tplId = $("#bulkTemplate").value;
  if (!tplId) { alert("Pick a template from the dropdown first."); return; }
  const status = $("#statusFilter").value, q = $("#leadSearch").value.trim(),
        campaign = $("#campaignFilter").value;
  try {
    const r = await post("/api/send-bulk", { status: status || undefined, q: q || undefined,
      campaign: campaign || undefined, template_id: +tplId, lead_ids: undefined });
    alert("Sent: " + r.sent + ", queued: " + r.queued + ", failed: " + r.failed + ", skipped (opted out): " + r.skipped_opted_out);
    loadLeads();
  } catch (err) { alert(err.message); }
});

async function openLead(id) {
  const d = await api("/api/leads/" + id);
  currentLead = d.lead;
  await loadTemplatesInto([$("#mTemplate")]);
  $("#mName").textContent = (d.lead.name || d.lead.business || d.lead.phone_display || "Lead");
  $("#mMeta").textContent = [d.lead.business, d.lead.phone_display, d.lead.email, d.lead.address, d.lead.city, d.lead.source, d.lead.campaign ? "campaign: " + d.lead.campaign : ""].filter(Boolean).join(" · ") +
    " — " + d.lead.status_label + (d.lead.opted_out ? " · opted out" : "");
  const ws = (d.lead.website || "").trim();
  const mW = $("#mWebsite");
  if (ws) {
    const href = /^https?:\/\//i.test(ws) ? ws : "https://" + ws;
    mW.innerHTML = '<a href="' + esc(href) + '" target="_blank" rel="noopener">' + esc(ws) + "</a>";
    mW.classList.remove("hidden");
  } else {
    mW.innerHTML = "";
    mW.classList.add("hidden");
  }
  $("#mStatus").value = d.lead.status;
  $("#mCampaign").value = d.lead.campaign || "";
  renderThread($("#mThread"), d.messages);
  $("#mBody").value = "";
  $("#mSendStatus").textContent = "";
  $("#leadModal").classList.remove("hidden");
}
$("#modalClose").addEventListener("click", () => { $("#leadModal").classList.add("hidden"); currentLead = null; });
$("#mStatus").addEventListener("change", async () => {
  await api("/api/leads/" + currentLead.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: $("#mStatus").value }) });
  loadLeads();
});
$("#mCampaign").addEventListener("change", async () => {
  await api("/api/leads/" + currentLead.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ campaign: $("#mCampaign").value.trim() }) });
  loadLeads();
});
$("#mTemplate").addEventListener("change", () => {
  const t = templates.find((x) => String(x.id) === $("#mTemplate").value);
  if (t) $("#mBody").value = t.body;
});
$("#mSend").addEventListener("click", async () => {
  $("#mSendStatus").textContent = "Sending…";
  try {
    const r = await post("/api/send", { lead_id: currentLead.id, body: $("#mBody").value });
    $("#mSendStatus").textContent = r.message.status === "sent" ? "Sent." : "Queued (no provider configured — not sent).";
    openLead(currentLead.id); loadLeads();
  } catch (err) { $("#mSendStatus").textContent = err.message; }
});
$("#mDelete").addEventListener("click", async () => {
  if (!confirm("Delete this lead and its messages?")) return;
  await api("/api/leads/" + currentLead.id, { method: "DELETE" });
  $("#leadModal").classList.add("hidden"); loadLeads();
});

function renderThread(el, msgs) {
  el.innerHTML = msgs.map((m) => {
    const cls = m.direction === "in" ? "in" : ("out" + (m.status === "queued" ? " queued" : "") + (m.status === "failed" ? " failed" : ""));
    let sub = esc(m.created_at) + (m.direction === "out" ? " · " + esc(m.status) + (m.provider ? " via " + esc(m.provider) : "") : "");
    if (m.error) sub += " · " + esc(m.error);
    return '<div class="msg ' + cls + '">' + esc(m.body) + "<small>" + sub + "</small></div>";
  }).join("") || '<p class="muted">No messages yet.</p>';
  el.scrollTop = el.scrollHeight;
}

/* ---- inbox ---- */
async function loadInbox() {
  const d = await api("/api/inbox");
  $("#convList").innerHTML = d.conversations.map((c) =>
    '<div class="conv-item' + (c.unread ? " unread" : "") + '" data-id="' + c.id + '"><strong>' +
    esc(c.name || c.business || c.phone_display) + '</strong> <span class="badge">' + esc(c.status_label) + "</span><small>" +
    esc(c.last_body || "") + "</small></div>").join("") || '<p class="muted" style="padding:10px">No conversations yet.</p>';
  $$("#convList .conv-item").forEach((el) => el.addEventListener("click", () => openConv(+el.dataset.id)));
}
async function openConv(id) {
  const d = await api("/api/inbox/" + id);
  currentConv = d.lead;
  await loadTemplatesInto([$("#replyTemplate")]);
  $("#convEmpty").classList.add("hidden");
  $("#convDetail").classList.remove("hidden");
  $("#convName").textContent = d.lead.name || d.lead.business || d.lead.phone_display;
  $("#convPhone").textContent = d.lead.phone_display + (d.lead.business ? " · " + d.lead.business : "");
  $("#convStatus").textContent = d.lead.status_label + (d.lead.opted_out ? " · opted out" : "");
  $("#convMeta").textContent = [d.lead.email, d.lead.city, d.lead.source].filter(Boolean).join(" · ");
  $("#takeoverBtn").classList.toggle("hidden", d.lead.status === "taken_over");
  $("#releaseBtn").classList.toggle("hidden", d.lead.status !== "taken_over");
  renderThread($("#thread"), d.messages);
  $("#replyBody").value = "";
  $("#replyStatus").textContent = "";
  $$("#convList .conv-item").forEach((el) => el.classList.toggle("active", +el.dataset.id === id));
  loadDashboard();
  // On a phone the conversation list stacks above the detail, so bring
  // the opened thread into view — otherwise tapping a row looks dead.
  // Instant, not smooth: a smooth scroll eats the next tap on Android.
  if (window.innerWidth <= 700) $("#convDetail").scrollIntoView({ block: "start" });
}
$("#replyTemplate").addEventListener("change", () => {
  const t = templates.find((x) => String(x.id) === $("#replyTemplate").value);
  if (t) $("#replyBody").value = t.body;
});
$("#replySend").addEventListener("click", async () => {
  $("#replyStatus").textContent = "Sending…";
  try {
    const r = await post("/api/inbox/" + currentConv.id + "/reply", { body: $("#replyBody").value });
    $("#replyStatus").textContent = r.message.status === "sent" ? "Sent." : "Queued (no provider configured — not sent).";
    openConv(currentConv.id);
  } catch (err) { $("#replyStatus").textContent = err.message; }
});
$("#takeoverBtn").addEventListener("click", async () => {
  await post("/api/inbox/" + currentConv.id + "/takeover", {});
  openConv(currentConv.id); loadLeads();
});
$("#releaseBtn").addEventListener("click", async () => {
  await post("/api/inbox/" + currentConv.id + "/release", {});
  openConv(currentConv.id); loadLeads();
});

/* ---- templates ---- */
async function loadTemplates() {
  await loadTemplatesInto([$("#bulkTemplate")]);
  $("#tplList").innerHTML = templates.map((t) =>
    '<li><strong>' + esc(t.name) + '</strong> <span class="muted">(id ' + t.id + ")</span><br>" + esc(t.body) +
    ' <button class="btn small" data-del="' + t.id + '">Delete</button></li>').join("") ||
    "<li class='muted'>No templates yet.</li>";
  $$("#tplList [data-del]").forEach((b) => b.addEventListener("click", async () => {
    await api("/api/templates/" + b.dataset.del, { method: "DELETE" }); loadTemplates();
  }));
}
$("#tplSave").addEventListener("click", async () => {
  $("#tplError").textContent = "";
  try {
    await post("/api/templates", { name: $("#tplName").value.trim(), body: $("#tplBody").value });
    $("#tplName").value = ""; $("#tplBody").value = ""; loadTemplates();
  } catch (err) { $("#tplError").textContent = err.message; }
});

/* ---- import ---- */
$("#importBtn").addEventListener("click", async () => {
  const f = $("#csvFile").files[0];
  if (!f) { $("#importResult").textContent = "Pick a CSV file first."; return; }
  const fd = new FormData();
  fd.append("file", f);
  fd.append("imported_by", $("#importedBy").value.trim() || "ui");
  $("#importResult").textContent = "Importing…";
  try {
    const r = await fetch("/api/import", { method: "POST", body: fd, credentials: "same-origin" });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Import failed");
    $("#importResult").textContent = "Imported " + d.imported + " of " + d.total_rows +
      " rows (" + d.skipped_dupes + " duplicates skipped, " + d.invalid + " invalid)." +
      (d.problems && d.problems.length ? " " + d.problems.join("; ") : "");
    loadBatches();
  } catch (err) { $("#importResult").textContent = err.message; }
});
async function loadBatches() {
  const d = await api("/api/import-batches");
  $("#batchList").innerHTML = d.batches.map((b) =>
    "<li><strong>" + esc(b.filename) + "</strong> — imported " + b.imported + " of " + b.total_rows +
    " (" + b.skipped_dupes + " dupes, " + b.invalid + " invalid) by " + esc(b.imported_by || "") +
    ' <span class="muted">' + esc(b.created_at) + "</span></li>").join("") ||
    "<li class='muted'>No imports yet.</li>";
}

/* ---- settings ---- */
async function loadSettings() {
  const d = await api("/api/settings");
  const s = d.settings;
  document.querySelector('input[name="provider"][value="' + s.provider + '"]').checked = true;
  $("#sVoipEmail").value = s.voipms_email || "";
  $("#sVoipDid").value = s.voipms_did || "";
  $("#sTwSid").value = s.twilio_sid || "";
  $("#sTwNum").value = s.twilio_number || "";
  $("#sCap").value = s.daily_cap || 100;
  $("#sVoipPass").placeholder = s.voipms_api_password_configured ? "set — leave blank to keep" : "leave blank to keep";
  $("#sTwToken").placeholder = s.twilio_token_configured ? "set — leave blank to keep" : "leave blank to keep";
  const keys = await api("/api/api-keys");
  $("#keyList").innerHTML = keys.keys.map((k) =>
    "<li><strong>" + esc(k.name) + "</strong> <code>" + esc(k.key_prefix) + "…</code> created " + esc(k.created_at) +
    " · last used " + esc(k.last_used_at || "never") +
    ' <button class="btn small" data-revoke="' + k.id + '">Revoke</button></li>').join("") ||
    "<li class='muted'>No API keys yet.</li>";
  $$("#keyList [data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Revoke this key?")) return;
    await api("/api/api-keys/" + b.dataset.revoke, { method: "DELETE" }); loadSettings();
  }));
  const notifs = await api("/api/notifications");
  $("#notifList").innerHTML = notifs.notifications.map((n) =>
    "<li>" + (n.is_read ? "" : "<strong>• </strong>") + esc(n.text) + ' <span class="muted">' + esc(n.created_at) + "</span></li>").join("") ||
    "<li class='muted'>No notifications.</li>";
  const log = await api("/api/api-log");
  $("#apiLogList").innerHTML = log.log.map((e) =>
    "<li>" + esc(e.created_at) + " · " + esc(e.key_name || "bad key") + " · " + esc(e.method) + " " + esc(e.endpoint) +
    " → " + e.status + "</li>").join("") || "<li class='muted'>No API calls yet.</li>";
}
$("#saveSettings").addEventListener("click", async () => {
  const provider = document.querySelector('input[name="provider"]:checked').value;
  try {
    await post("/api/settings", { provider, voipms_email: $("#sVoipEmail").value,
      voipms_api_password: $("#sVoipPass").value, voipms_did: $("#sVoipDid").value,
      twilio_sid: $("#sTwSid").value, twilio_token: $("#sTwToken").value,
      twilio_number: $("#sTwNum").value, daily_cap: $("#sCap").value });
    $("#settingsStatus").textContent = "Saved.";
    $("#sVoipPass").value = ""; $("#sTwToken").value = "";
    checkAuth();
  } catch (err) { $("#settingsStatus").textContent = err.message; }
});
$("#testConn").addEventListener("click", async () => {
  $("#settingsStatus").textContent = "Testing…";
  try {
    const r = await post("/api/test-connection", {});
    $("#settingsStatus").textContent = r.ok ? ("✓ " + r.detail) : ("✗ " + r.detail);
  } catch (err) { $("#settingsStatus").textContent = err.message; }
});
$("#syncInbox").addEventListener("click", async () => {
  $("#settingsStatus").textContent = "Syncing…";
  try {
    const r = await post("/api/sync-inbox", {});
    $("#settingsStatus").textContent = r.ok ? ("Synced " + r.imported + " new replies.") : ("✗ " + r.detail);
  } catch (err) { $("#settingsStatus").textContent = err.message; }
});
$("#createKey").addEventListener("click", async () => {
  const name = $("#keyName").value.trim();
  if (!name) { $("#newKey").textContent = "Give the key a name first."; return; }
  try {
    const r = await post("/api/api-keys", { name });
    $("#newKey").textContent = "New key for " + name + ": " + r.key + " — copy it now, it is shown only once.";
    $("#keyName").value = "";
    loadSettings();
  } catch (err) { $("#newKey").textContent = err.message; }
});
$("#markRead").addEventListener("click", async () => { await post("/api/notifications/read", {}); loadSettings(); });
$("#backupBtn").addEventListener("click", async () => {
  try {
    const r = await post("/api/backup", {});
    $("#backupStatus").textContent = "Backed up to " + r.file;
  } catch (err) { $("#backupStatus").textContent = err.message; }
});

checkAuth();
