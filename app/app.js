/* inboxHero interactive Gmail demo UI (X4) */
(() => {
  const state = {
    bootstrap: null,
    view: "primary",
    selectedId: null,
    selectedOutboxId: null,
    search: "",
    calCursor: new Date(2026, 8, 1),
    relatedInviteMsg: null,
    welcomeTimer: null,
    composeMode: null,
    composeMeta: null,
    composeOriginalBody: "",
    composeCache: {},
    composePrefetch: {},
    pendingAction: null,
    pollTimer: null,
    prefsSeen: new Set(),
  };

  const VIEW_TITLES = {
    primary: "Primary",
    pending: "Pending actions",
    commitments: "Commitments",
    flagged: "Flagged",
    spam: "Spam",
    drafts: "Drafts",
    outbox: "Outbox",
  };

  const $ = (id) => document.getElementById(id);

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || res.statusText);
    }
    return res.json();
  }

  function formatTime(ts) {
    if (!ts) return "";
    try {
      const d = new Date(ts);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return ts;
    }
  }

  function shortFrom(from) {
    if (!from) return "";
    const m = from.match(/^([^<]+)/);
    return (m ? m[1] : from).trim();
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setView(view) {
    state.view = view;
    state.selectedOutboxId = null;
    document.querySelectorAll(".nav-item").forEach((el) => {
      el.classList.toggle("active", el.dataset.view === view);
    });
    $("list-header").textContent = VIEW_TITLES[view] || view;
    renderList();
    const cal = $("calendar-view");
    const empty = $("empty-read");
    const read = $("read-content");
    if (view === "commitments") {
      cal.classList.remove("hidden");
      empty.classList.add("hidden");
      read.classList.add("hidden");
      renderCalendar();
    } else {
      cal.classList.add("hidden");
      if (!state.selectedId && !state.selectedOutboxId) {
        empty.classList.remove("hidden");
        read.classList.add("hidden");
      }
    }
  }

  function filteredMessages() {
    const msgs = state.bootstrap?.messages || [];
    const q = state.search.trim().toLowerCase();
    if (!q) return msgs;
    return msgs.filter((m) => {
      const blob = `${m.from} ${m.subject} ${m.snippet} ${m.id}`.toLowerCase();
      return blob.includes(q);
    });
  }

  function appendMailRow(body, m) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `mail-row${m.read ? "" : " unread"}${state.selectedId === m.id ? " active" : ""}`;
    const draftBadge = m.has_draft ? `<span class="badge draft-ready">Draft ready</span>` : "";
    btn.innerHTML = `
      <span class="mail-time">${formatTime(m.timestamp)}</span>
      <div class="mail-from">${escapeHtml(shortFrom(m.from))}</div>
      <div class="mail-subject">${escapeHtml(m.subject || "")}</div>
      <div class="mail-snippet">${draftBadge}${m.disposition ? `<span class="badge disp">${escapeHtml(m.disposition)}</span>` : ""}${m.disposition_reason ? `<span class="disp-reason">${escapeHtml(String(m.disposition_reason).slice(0, 80))}</span>` : ""}${escapeHtml(m.snippet || "")}</div>
    `;
    btn.addEventListener("click", () => openMessage(m.id));
    body.appendChild(btn);
  }

  function showLearnToast(fact) {
    const host = $("learn-toasts");
    if (!host) return;
    const el = document.createElement("div");
    el.className = "learn-toast";
    const key = fact.key || "preference";
    const value = fact.value || "";
    const source = fact.source ? ` · from ${fact.source}` : "";
    el.innerHTML = `<strong>Learning preference…</strong><span>${escapeHtml(key)} → ${escapeHtml(value)}${escapeHtml(source)}</span>`;
    host.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 400);
    }, 4200);
  }

  function maybeToastNewPrefs() {
    const facts = state.bootstrap?.preferences?.facts || [];
    facts.forEach((f) => {
      const sig = `${f.key}|${f.value}`;
      if (state.prefsSeen.has(sig)) return;
      state.prefsSeen.add(sig);
      if (sessionStorage.getItem("inboxhero_toast_seeded") === "1") {
        showLearnToast(f);
      }
    });
    if (facts.length) sessionStorage.setItem("inboxhero_toast_seeded", "1");
  }

  function cacheKey(messageId, mode) {
    return `${messageId}|${mode}`;
  }

  function prefetchCompose(messageId) {
    ["reply", "reply_all"].forEach((mode) => {
      const key = cacheKey(messageId, mode);
      if (state.composeCache[key] || state.composePrefetch[key]) return;
      state.composePrefetch[key] = api("/api/compose", {
        method: "POST",
        body: JSON.stringify({ message_id: messageId, mode }),
      })
        .then((res) => {
          state.composeCache[key] = res;
          return res;
        })
        .catch(() => null)
        .finally(() => {
          delete state.composePrefetch[key];
        });
    });
  }

  function fillComposePanel(res, mode) {
    state.composeMeta = res;
    state.composeOriginalBody = res.body || "";
    $("compose-to").value = res.to || "";
    $("compose-cc").value = res.cc || "";
    $("compose-bcc").value = res.bcc || "";
    $("compose-body").value = res.body || "";
    $("compose-prompt-id").textContent = res.prompt_id
      ? `Using prompt: ${res.prompt_id}${res.reason ? ` (${res.reason})` : ""}`
      : "";
    renderCiteButtons($("compose-cites"), res.cited_message_ids || []);
    $("compose-sources").innerHTML = (res.sources || []).length
      ? `<span>Sources:</span> ${escapeHtml((res.sources || []).join(" · "))}`
      : "";
    const provider = state.bootstrap?.llm || "LLM";
    $("compose-status").textContent = res.used_llm
      ? `Generated with ${provider}`
      : res.reason === "existing_draft"
        ? "Loaded grounded draft from drafts.json"
        : "Deterministic grounded fallback";
  }

  async function approvePending() {
    if (!state.pendingAction || !state.selectedId) return;
    const action = state.pendingAction.proposed_action;
    try {
      const res = await api("/api/pending/act", {
        method: "POST",
        body: JSON.stringify({
          message_id: state.selectedId,
          action,
          execute: true,
        }),
      });
      state.pendingAction = null;
      await loadBootstrap();
      alert(res.result || `Approved ${action}`);
    } catch (err) {
      alert(`Pending action failed: ${err.message}`);
    }
  }

    function renderCiteButtons(el, ids) {
    el.innerHTML = "";
    if (!ids || !ids.length) return;
    el.innerHTML = "<span>Cited:</span>";
    ids.forEach((id) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = id;
      b.addEventListener("click", () => openMessage(id));
      el.appendChild(b);
    });
  }

  function renderList() {
    const body = $("list-body");
    body.innerHTML = "";
    const b = state.bootstrap;
    if (!b) {
      body.innerHTML = "<p style='padding:16px;color:#5f6368'>Loading…</p>";
      return;
    }

    if (state.view === "primary") {
      filteredMessages().forEach((m) => appendMailRow(body, m));
      return;
    }

    if (state.view === "spam") {
      const rows = b.spam || [];
      rows.forEach((m) => appendMailRow(body, m));
      if (!rows.length) body.innerHTML = "<p style='padding:16px;color:#5f6368'>No spam dispositions.</p>";
      return;
    }

    if (state.view === "drafts") {
      const rows = b.drafts || [];
      rows.forEach((d) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `action-row${state.selectedId === d.target_id ? " active" : ""}`;
        btn.innerHTML = `
          <div class="mail-from"><span class="badge draft-ready">Draft ready</span> ${escapeHtml(d.target_id)}</div>
          <div class="mail-subject">${escapeHtml(d.subject || "")}</div>
          <div class="mail-snippet">${escapeHtml(d.snippet || "")}</div>
        `;
        btn.addEventListener("click", () => openMessage(d.target_id));
        body.appendChild(btn);
      });
      if (!rows.length) body.innerHTML = "<p style='padding:16px;color:#5f6368'>No grounded drafts yet.</p>";
      return;
    }

    if (state.view === "outbox") {
      const rows = b.outbox || [];
      rows.forEach((o) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `action-row${state.selectedOutboxId === o.outbox_id ? " active" : ""}`;
        btn.innerHTML = `
          <div class="mail-from">${escapeHtml(shortFrom(o.to || o.from || ""))} · <span class="badge warn">${escapeHtml(o.status)}</span></div>
          <div class="mail-subject">${escapeHtml(o.subject || o.outbox_id)}</div>
          <div class="mail-snippet">${o.kind ? `<span class="badge">${escapeHtml(o.kind)}</span>` : ""}${escapeHtml(o.snippet || "")}</div>
        `;
        btn.addEventListener("click", () => showOutboxDetail(o));
        body.appendChild(btn);
      });
      if (!rows.length) body.innerHTML = "<p style='padding:16px;color:#5f6368'>Outbox empty.</p>";
      return;
    }

    if (state.view === "pending") {
      (b.pending || []).forEach((row) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `action-row${state.selectedId === row.message_id ? " active" : ""}`;
        const cites = (row.cited_message_ids || []).slice(0, 4).map((id) => `<button type="button" class="cite-chip" data-cite="${escapeHtml(id)}">${escapeHtml(id)}</button>`).join(" ");
        btn.innerHTML = `
          <div class="mail-from">${escapeHtml(shortFrom(row.from))} · <span class="badge warn">${escapeHtml(row.proposed_action)}</span></div>
          <div class="mail-subject">${escapeHtml(row.subject || "")}</div>
          <div class="mail-snippet">${escapeHtml(row.why_human || "")}</div>
          <div class="cite-row pending-cites">${cites || ""}</div>
        `;
        btn.addEventListener("click", (e) => {
          const chip = e.target.closest("[data-cite]");
          if (chip) {
            e.stopPropagation();
            openMessage(chip.getAttribute("data-cite"));
            return;
          }
          state.pendingAction = row;
          openMessage(row.message_id);
        });
        body.appendChild(btn);
      });
      if (!(b.pending || []).length) body.innerHTML = "<p style='padding:16px;color:#5f6368'>No pending gated actions.</p>";
      return;
    }

    if (state.view === "flagged") {
      (b.flagged || []).forEach((row) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `action-row${state.selectedId === row.id ? " active" : ""}`;
        btn.innerHTML = `
          <div class="mail-from">${escapeHtml(shortFrom(row.from))}</div>
          <div class="mail-subject">${escapeHtml(row.subject || "")}</div>
          <div class="mail-snippet"><span class="badge danger">${escapeHtml(row.kind)}</span>${escapeHtml(row.attempted_summary || row.system_did || "")}</div>
        `;
        btn.addEventListener("click", () => openMessage(row.id));
        body.appendChild(btn);
      });
      if (!(b.flagged || []).length) body.innerHTML = "<p style='padding:16px;color:#5f6368'>Nothing flagged.</p>";
      return;
    }

    if (state.view === "commitments") {
      const items = [
        ...(b.commitments || []).map((c) => ({ ...c, source: "commitment" })),
        ...(b.calendar_events || []).map((e) => ({
          id: e.id,
          title: e.title,
          when_start: e.start,
          when_end: e.end,
          kind: "calendar",
          source: "calendar",
          attendees: e.attendees,
          notes: e.notes,
          conflict: false,
        })),
      ];
      items.sort((a, b2) => String(a.when_start || "").localeCompare(String(b2.when_start || "")));
      items.forEach((c) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "action-row";
        const conflict = c.conflict ? `<span class="badge danger">conflict</span>` : "";
        const cites = (c.cited_message_ids || []).join(", ");
        btn.innerHTML = `
          <div class="mail-from">${escapeHtml(c.title || c.id)}</div>
          <div class="mail-subject">${escapeHtml(formatTime(c.when_start))} · ${escapeHtml(c.kind || c.source)}</div>
          <div class="mail-snippet">${conflict}${cites ? `cites ${escapeHtml(cites)}` : escapeHtml((c.attendees || []).join(", "))}</div>
        `;
        btn.addEventListener("click", () => {
          showCommitmentDetail(c);
          if (c.cited_message_ids?.[0]) openMessage(c.cited_message_ids[0], { keepCal: true });
        });
        body.appendChild(btn);
      });
    }
  }

  function showOutboxDetail(o) {
    state.selectedOutboxId = o.outbox_id;
    state.selectedId = null;
    $("calendar-view").classList.add("hidden");
    $("compose-panel").classList.add("hidden");
    $("empty-read").classList.add("hidden");
    $("read-content").classList.remove("hidden");
    $("read-subject").textContent = o.subject || o.outbox_id;
    $("read-meta").textContent = `${o.from || ""} → ${o.to || ""} · ${o.status || "queued"} · ${o.outbox_id}`;
    const badges = $("read-badges");
    badges.innerHTML = "";
    if (o.status) badges.innerHTML += `<span class="badge warn">${escapeHtml(o.status)}</span>`;
    if (o.kind) badges.innerHTML += `<span class="badge">${escapeHtml(o.kind)}</span>`;
    $("read-body").textContent = o.body || o.snippet || "";
    $("draft-box").classList.add("hidden");
    if (o.in_reply_to) state.relatedInviteMsg = o.in_reply_to;
    renderList();
  }

  async function openMessage(id, opts = {}) {
    state.selectedId = id;
    state.selectedOutboxId = null;
    state.relatedInviteMsg = id;
    if (!opts.keepCal && state.view !== "commitments") {
      $("calendar-view").classList.add("hidden");
    }
    try {
      const data = await api(`/api/messages/${encodeURIComponent(id)}`);
      try {
        await api(`/api/messages/${encodeURIComponent(id)}/read`, { method: "POST", body: "{}" });
        const row = (state.bootstrap?.messages || []).find((m) => m.id === id);
        if (row) row.read = true;
        const spamRow = (state.bootstrap?.spam || []).find((m) => m.id === id);
        if (spamRow) spamRow.read = true;
      } catch {
        /* non-fatal */
      }
      $("empty-read").classList.add("hidden");
      $("read-content").classList.remove("hidden");
      $("compose-panel").classList.add("hidden");
      const m = data.message;
      $("read-subject").textContent = m.subject || "";
      $("read-meta").textContent = `${m.from} → ${m.to || ""} · ${formatTime(m.timestamp)} · ${m.id}`;
      const badges = $("read-badges");
      badges.innerHTML = "";
      if (data.disposition) {
        badges.innerHTML += `<span class="badge disp">${escapeHtml(data.disposition.disposition)}</span>`;
        badges.innerHTML += `<span class="badge">${escapeHtml(data.disposition.route || "")}</span>`;
      }
      if (data.draft && data.draft.status === "drafted") {
        badges.innerHTML += `<span class="badge draft-ready">Draft ready</span>`;
      }
      $("read-body").textContent = m.body || "";
      const draftBox = $("draft-box");
      if (data.draft && (data.draft.body || data.draft.draft_body)) {
        draftBox.classList.remove("hidden");
        $("draft-body").textContent =
          data.draft.body || data.draft.draft_body || "";
        renderCiteButtons($("draft-cites"), data.draft.cited_message_ids || []);
        const ret = data.draft.retrieval || {};
        const src = [];
        if (ret.thread_ids_read?.length) src.push(`thread: ${ret.thread_ids_read.join(", ")}`);
        if (ret.vector_hit_ids?.length) src.push(`vector: ${ret.vector_hit_ids.slice(0, 6).join(", ")}`);
        if (data.draft.notes) src.push(data.draft.notes);
        $("draft-sources").innerHTML = src.length
          ? `<span>Sources:</span> ${escapeHtml(src.join(" · "))}`
          : "";
      } else {
        draftBox.classList.add("hidden");
      }
      $("chat-hint").textContent = `Context: ${m.id} — ${m.subject || ""}`;
      const pendingRow = (state.bootstrap?.pending || []).find((p) => p.message_id === id);
      const why = $("pending-why");
      const approveBtn = $("btn-pending-approve");
      if (pendingRow) {
        state.pendingAction = pendingRow;
        why.classList.remove("hidden");
        why.innerHTML = `<strong>Pending: ${escapeHtml(pendingRow.proposed_action)}</strong> — ${escapeHtml(pendingRow.why_human || "")}`;
        const citeHost = document.createElement("div");
        citeHost.className = "cite-row";
        why.appendChild(citeHost);
        renderCiteButtons(citeHost, pendingRow.cited_message_ids || [id]);
        approveBtn.classList.remove("hidden");
        approveBtn.textContent = `Approve ${pendingRow.proposed_action}`;
      } else {
        why.classList.add("hidden");
        why.innerHTML = "";
        approveBtn.classList.add("hidden");
        if (state.view !== "pending") state.pendingAction = null;
      }
      if (data.disposition?.reason && !pendingRow) {
        badges.innerHTML += `<span class="badge">${escapeHtml(String(data.disposition.reason).slice(0, 60))}</span>`;
      }
      prefetchCompose(id);
      renderList();
    } catch (err) {
      alert(`Could not load ${id}: ${err.message}`);
    }
  }

  async function openCompose(mode, opts = {}) {
    if (!state.selectedId) {
      alert("Select a message first.");
      return;
    }
    state.composeMode = mode;
    $("compose-panel").classList.remove("hidden");
    $("compose-mode-label").textContent =
      mode === "reply_all" ? "Reply all" : mode.toUpperCase();
    $("compose-cc-wrap").classList.toggle("hidden", mode !== "cc" && mode !== "reply_all");
    $("compose-bcc-wrap").classList.toggle("hidden", mode !== "bcc");
    const key = cacheKey(state.selectedId, mode);
    const bypass = !!opts.regenerate;
    if (!bypass && state.composeCache[key]) {
      fillComposePanel(state.composeCache[key], mode);
      return;
    }
    $("compose-status").textContent = "Generating grounded message…";
    if (!state.composeCache[key]) $("compose-body").value = "";
    try {
      let res;
      if (!bypass && state.composePrefetch[key]) {
        res = await state.composePrefetch[key];
      }
      if (!res || bypass) {
        res = await api("/api/compose", {
          method: "POST",
          body: JSON.stringify({
            message_id: state.selectedId,
            mode,
            extra_cc: $("compose-cc").value || "",
            extra_bcc: $("compose-bcc").value || "",
          }),
        });
        state.composeCache[key] = res;
      }
      fillComposePanel(res, mode);
    } catch (err) {
      $("compose-status").textContent = `Failed: ${err.message}`;
    }
  }

  async function sendCompose() {
    if (!state.selectedId || !state.composeMeta) return;
    try {
      const res = await api("/api/compose/send", {
        method: "POST",
        body: JSON.stringify({
          message_id: state.selectedId,
          to: $("compose-to").value,
          cc: $("compose-cc").value,
          bcc: $("compose-bcc").value,
          subject: state.composeMeta.subject || "",
          body: $("compose-body").value,
          cited_message_ids: state.composeMeta.cited_message_ids || [],
          execute: true,
          mode: state.composeMode || "reply",
          original_body: state.composeOriginalBody || "",
        }),
      });
      $("compose-status").textContent = `Sent to outbox: ${res.outbox_path || "ok"}`;
      (res.learned || []).forEach((f) => showLearnToast(f));
      delete state.composeCache[cacheKey(state.selectedId, state.composeMode || "reply")];
      await loadBootstrap();
    } catch (err) {
      $("compose-status").textContent = `Send failed: ${err.message}`;
    }
  }

  function showCommitmentDetail(c) {
    const el = $("cal-detail");
    const cites = (c.cited_message_ids || [])
      .map(
        (id) =>
          `<button type="button" class="btn ghost cite-link" data-id="${escapeHtml(id)}">${escapeHtml(id)}</button>`
      )
      .join(" ");
    el.innerHTML = `
      <strong>${escapeHtml(c.title || c.id)}</strong>
      <div>${escapeHtml(formatTime(c.when_start))} → ${escapeHtml(formatTime(c.when_end))}</div>
      <div>${c.conflict ? '<span class="badge danger">conflict</span>' : ""} ${escapeHtml(c.kind || c.source || "")}</div>
      ${c.notes ? `<p>${escapeHtml(c.notes)}</p>` : ""}
      <div>Cited: ${cites || "—"}</div>
    `;
    el.querySelectorAll(".cite-link").forEach((btn) => {
      btn.addEventListener("click", () => openMessage(btn.dataset.id, { keepCal: true }));
    });
  }

  function monthMatrix(cursor) {
    const year = cursor.getFullYear();
    const month = cursor.getMonth();
    const first = new Date(year, month, 1);
    const startPad = (first.getDay() + 6) % 7;
    const start = new Date(year, month, 1 - startPad);
    const days = [];
    for (let i = 0; i < 42; i++) {
      const d = new Date(start);
      d.setDate(start.getDate() + i);
      days.push(d);
    }
    return days;
  }

  function ymd(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function renderCalendar() {
    const b = state.bootstrap;
    if (!b) return;
    const cursor = state.calCursor;
    $("cal-title").textContent = cursor.toLocaleString(undefined, {
      month: "long",
      year: "numeric",
    });
    const byDate = {};
    (b.commitments || []).forEach((c) => {
      const key = String(c.when_start || "").slice(0, 10);
      if (!key) return;
      (byDate[key] ||= []).push({ ...c, chip: "commitment" });
    });
    (b.calendar_events || []).forEach((e) => {
      const key = String(e.start || "").slice(0, 10);
      if (!key) return;
      (byDate[key] ||= []).push({
        id: e.id,
        title: e.title,
        when_start: e.start,
        when_end: e.end,
        kind: "calendar",
        chip: "event",
        attendees: e.attendees,
        notes: e.notes,
        cited_message_ids: [],
      });
    });
    const conflicts = (b.commitments || []).filter((c) => c.conflict);
    const banner = $("cal-conflict-banner");
    if (conflicts.length) {
      banner.classList.remove("hidden");
      banner.textContent = `Conflict callout: ${conflicts.map((c) => c.title).join(" · ")} (same slot — mirrors R6).`;
    } else {
      banner.classList.add("hidden");
    }
    const grid = $("cal-grid");
    grid.innerHTML = "";
    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((label) => {
      const h = document.createElement("div");
      h.style.fontSize = "0.75rem";
      h.style.color = "#5f6368";
      h.style.padding = "4px";
      h.textContent = label;
      grid.appendChild(h);
    });
    monthMatrix(cursor).forEach((d) => {
      const key = ymd(d);
      const cell = document.createElement("div");
      const outside = d.getMonth() !== cursor.getMonth();
      const items = byDate[key] || [];
      const hasConflict = items.some((x) => x.conflict);
      cell.className = `cal-day${outside ? " outside" : ""}${hasConflict ? " has-conflict" : ""}`;
      cell.innerHTML = `<div class="cal-day-num">${d.getDate()}</div>`;
      items.slice(0, 4).forEach((item) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = `cal-chip${item.conflict ? " conflict" : ""}${item.chip === "event" ? " event" : ""}`;
        chip.textContent = item.title || item.id;
        chip.addEventListener("click", () => {
          showCommitmentDetail(item);
          if (item.cited_message_ids?.[0]) openMessage(item.cited_message_ids[0], { keepCal: true });
        });
        cell.appendChild(chip);
      });
      grid.appendChild(cell);
    });
  }

  function updateCounts() {
    const b = state.bootstrap;
    if (!b) return;
    $("count-primary").textContent = String((b.messages || []).length);
    $("count-pending").textContent = String((b.pending || []).length);
    $("count-flagged").textContent = String((b.flagged || []).length);
    $("count-spam").textContent = String((b.spam || []).length);
    $("count-drafts").textContent = String((b.drafts || []).length);
    $("count-outbox").textContent = String((b.outbox || []).length);
    $("count-commitments").textContent = String(
      (b.commitments || []).length + (b.calendar_events || []).length
    );
  }

  function fillWelcomeSummary(sim) {
    const s = sim.summary || {};
    $("welcome-lead").textContent =
      "Agent Mode is optimizing your work: triage, grounded drafts, gated actions, and commitments — Primary starts unread.";
    $("welcome-stats").innerHTML = `
      <li><strong>${s.message_count ?? 0}</strong> messages ready (all unread)</li>
      <li><strong>${s.pending_count ?? 0}</strong> pending · <strong>${s.flagged_count ?? 0}</strong> flagged · <strong>${s.spam_count ?? 0}</strong> spam</li>
      <li><strong>${s.draft_count ?? 0}</strong> drafts · <strong>${s.outbox_count ?? 0}</strong> outbox</li>
      <li><strong>${s.commitment_count ?? 0}</strong> commitments · <strong>${s.calendar_events ?? 0}</strong> calendar events · <strong>${s.conflict_count ?? 0}</strong> conflicts</li>
    `;
    $("welcome-note").textContent = sim.note || "";
  }

  function playWorkingSequence(sim) {
    return new Promise((resolve) => {
      const modal = $("welcome-modal");
      const working = $("welcome-working");
      const summary = $("welcome-summary");
      const feed = $("action-feed");
      const track = $("action-ticker-track");
      const status = $("working-status");

      if (state.welcomeTimer) {
        clearTimeout(state.welcomeTimer);
        state.welcomeTimer = null;
      }

      working.classList.remove("hidden");
      summary.classList.add("hidden");
      feed.innerHTML = "";
      fillWelcomeSummary(sim);

      const lines = (sim.action_log && sim.action_log.length
        ? sim.action_log
        : ["Loading mailbox…", "Triaging…", "Staging drafts…", "Agent Mode ready"]).slice();

      track.innerHTML = lines.concat(lines).map((l) => `<span>${escapeHtml(l)}</span>`).join("");
      track.style.animation = "none";
      void track.offsetWidth;
      track.style.animation = "";

      modal.classList.remove("hidden");

      let i = 0;
      const stepMs = 1000;

      function tick() {
        if (i >= lines.length) {
          status.textContent = "Done";
          working.classList.add("hidden");
          summary.classList.remove("hidden");
          resolve();
          return;
        }
        const li = document.createElement("li");
        li.textContent = lines[i];
        li.className = "latest";
        feed.querySelectorAll("li").forEach((el) => el.classList.remove("latest"));
        feed.appendChild(li);
        while (feed.children.length > 6) feed.removeChild(feed.firstChild);
        status.textContent = `Action ${i + 1} of ${lines.length}`;
        i += 1;
        state.welcomeTimer = setTimeout(tick, stepMs);
      }

      tick();
    });
  }

  async function loadBootstrap() {
    state.bootstrap = await api("/api/bootstrap");
    updateCounts();
    maybeToastNewPrefs();
    renderList();
    if (state.view === "commitments") renderCalendar();
  }

  async function runSimulate(rebuild = false) {
    try {
      const sim = await api("/api/simulate", {
        method: "POST",
        body: JSON.stringify({ rebuild }),
      });
      await loadBootstrap();
      (state.bootstrap?.preferences?.facts || []).forEach((f) => {
        const sig = `${f.key}|${f.value}`;
        if (!state.prefsSeen.has(sig)) {
          state.prefsSeen.add(sig);
          showLearnToast(f);
        }
      });
      sessionStorage.setItem("inboxhero_toast_seeded", "1");
      await playWorkingSequence(sim);
    } catch (err) {
      alert(`Simulate failed: ${err.message}`);
    }
  }

  function defaultInviteTimes() {
    const start = new Date(2026, 8, 15, 10, 0);
    const end = new Date(2026, 8, 15, 10, 45);
    const toLocal = (d) => {
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    $("invite-start").value = toLocal(start);
    $("invite-end").value = toLocal(end);
  }

  function openInviteModal() {
    defaultInviteTimes();
    $("invite-preview").classList.add("hidden");
    $("invite-modal").classList.remove("hidden");
  }

  function invitePayload(execute) {
    const attendees = $("invite-attendees")
      .value.split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const startVal = $("invite-start").value;
    const endVal = $("invite-end").value;
    return {
      title: $("invite-title").value.trim(),
      start: startVal ? `${startVal}:00-07:00` : "",
      end: endVal ? `${endVal}:00-07:00` : "",
      attendees,
      notes: $("invite-notes").value.trim(),
      related_message_id: state.relatedInviteMsg || "msg_002",
      execute,
    };
  }

  async function submitInvite(execute) {
    try {
      const result = await api("/api/calendar/invite", {
        method: "POST",
        body: JSON.stringify(invitePayload(execute)),
      });
      $("invite-preview").classList.remove("hidden");
      $("invite-preview").textContent = JSON.stringify(result, null, 2);
      if (execute) {
        await loadBootstrap();
        if (state.view === "commitments") renderCalendar();
      }
    } catch (err) {
      alert(`Invite failed: ${err.message}`);
    }
  }

  function appendChat(role, text) {
    const log = $("chat-log");
    const div = document.createElement("div");
    div.className = `chat-bubble ${role === "user" ? "user" : "bot"}`;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async function sendChat(question) {
    appendChat("user", question);
    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          question,
          message_id: state.selectedId || null,
        }),
      });
      const suffix = res.used_llm ? "" : "\n\n(fallback — LLM offline)";
      const promptNote = res.prompt_id ? `\n\n[prompt: ${res.prompt_id}]` : "";
      const cites = (res.cited_message_ids || []).length
        ? `\n\nCited: ${res.cited_message_ids.join(", ")}`
        : "";
      appendChat("bot", `${res.answer}${cites}${promptNote}${suffix}`);
    } catch (err) {
      appendChat("bot", `Error: ${err.message}`);
    }
  }

  function wire() {
    document.querySelectorAll(".nav-item").forEach((el) => {
      el.addEventListener("click", () => setView(el.dataset.view));
    });
    $("search").addEventListener("input", (e) => {
      state.search = e.target.value;
      if (state.view === "primary" || state.view === "spam") renderList();
    });
    $("welcome-close").addEventListener("click", () => $("welcome-modal").classList.add("hidden"));
    $("btn-reply").addEventListener("click", () => openCompose("reply"));
    $("btn-reply-all").addEventListener("click", () => openCompose("reply_all"));
    $("btn-cc").addEventListener("click", () => openCompose("cc"));
    $("btn-bcc").addEventListener("click", () => openCompose("bcc"));
    $("btn-pending-approve").addEventListener("click", () => approvePending());
    $("compose-close").addEventListener("click", () => $("compose-panel").classList.add("hidden"));
    $("compose-regenerate").addEventListener("click", () => openCompose(state.composeMode || "reply", { regenerate: true }));
    $("compose-send").addEventListener("click", () => sendCompose());
    $("btn-invite-from-mail").addEventListener("click", openInviteModal);
    $("invite-close").addEventListener("click", () => $("invite-modal").classList.add("hidden"));
    $("invite-preview-btn").addEventListener("click", () => submitInvite(false));
    $("invite-execute-btn").addEventListener("click", () => submitInvite(true));
    $("cal-prev").addEventListener("click", () => {
      state.calCursor = new Date(state.calCursor.getFullYear(), state.calCursor.getMonth() - 1, 1);
      renderCalendar();
    });
    $("cal-next").addEventListener("click", () => {
      state.calCursor = new Date(state.calCursor.getFullYear(), state.calCursor.getMonth() + 1, 1);
      renderCalendar();
    });
    $("chat-fab").addEventListener("click", () => $("chat-drawer").classList.toggle("hidden"));
    $("chat-close").addEventListener("click", () => $("chat-drawer").classList.add("hidden"));
    $("chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("chat-input");
      const q = input.value.trim();
      if (!q) return;
      input.value = "";
      sendChat(q);
    });
  }

  function startPoll() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (document.hidden) return;
      if (!$("welcome-modal").classList.contains("hidden")) return;
      loadBootstrap().catch(() => {});
    }, 8000);
  }

  async function init() {
    wire();
    try {
      await loadBootstrap();
      startPoll();
      if (!state.bootstrap.artifacts?.dashboard_data) {
        await runSimulate(true);
      } else if (!sessionStorage.getItem("inboxhero_welcomed")) {
        await runSimulate(false);
        sessionStorage.setItem("inboxhero_welcomed", "1");
      }
    } catch (err) {
      $("list-body").innerHTML = `<p style="padding:16px;color:#d93025">Failed to load: ${escapeHtml(err.message)}. Is the server running?</p>`;
    }
  }

  init();
})();
