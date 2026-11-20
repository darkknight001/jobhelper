// Bring the selected status filter into view when the filter row is wider than the screen.
document.querySelector(".tabs a.on")?.scrollIntoView({ block: "nearest", inline: "center" });

// Floating chat assistant. Server text is only ever set with textContent.
(() => {
  const KEY = "jh-chat";
  let log = [];
  try { log = JSON.parse(sessionStorage.getItem(KEY) || "[]"); } catch (e) { log = []; }
  const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(log.slice(-60))); } catch (e) {} };

  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  const fab = el("button", "chat-fab");
  fab.type = "button";
  fab.setAttribute("aria-label", "Open the assistant");
  fab.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>';

  const panel = el("section", "chat-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "JobHelper assistant");
  panel.hidden = true;
  const head = el("div", "chat-head");
  head.append(el("b", null, "Assistant"));
  const clear = el("button", "chat-link", "Clear");
  const close = el("button", "chat-link", "Close");
  clear.type = close.type = "button";
  head.append(clear, close);
  const list = el("div", "chat-log");
  list.setAttribute("aria-live", "polite");
  const form = el("form", "chat-form");
  const input = el("textarea");
  input.rows = 1;
  input.placeholder = "Ask, or tell me what to change";
  input.setAttribute("aria-label", "Message");
  const send = el("button", "btn primary", "Send");
  form.append(input, send);
  panel.append(head, list, form);
  document.body.append(fab, panel);

  function render() {
    list.replaceChildren();
    if (!log.length) list.append(el("p", "chat-hint", "Try: “what happened today?”, “why was #35 skipped?”, “add Pune to my locations”, “pause for 2 hours”. Changes only happen after you tap Confirm."));
    for (const m of log) {
      if (m.role === "proposal") {
        const card = el("div", "chat-prop");
        card.append(el("div", "chat-prop-text", m.summary));
        if (m.state === "open") {
          const row = el("div", "chat-prop-row");
          const ok = el("button", "btn primary", "Confirm");
          const no = el("button", "btn", "Cancel");
          ok.type = no.type = "button";
          ok.onclick = () => confirm(m);
          no.onclick = () => { m.state = "cancelled"; save(); render(); };
          row.append(ok, no);
          card.append(row);
        } else {
          card.append(el("div", "chat-prop-state", m.state === "busy" ? "Working…" : m.state === "cancelled" ? "Cancelled" : m.result || "Done"));
        }
        list.append(card);
      } else {
        list.append(el("div", "chat-msg " + m.role, m.text));
      }
    }
    list.scrollTop = list.scrollHeight;
  }

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}), credentials: "same-origin" });
    if (!(r.headers.get("content-type") || "").includes("json")) throw new Error("Signed out? Reload the page.");
    return r.json();
  }

  async function confirm(m) {
    m.state = "busy"; render();
    try { const out = await post("/assistant/confirm/" + encodeURIComponent(m.id)); m.result = out.result; }
    catch (e) { m.result = "Didn't go through: " + e.message; }
    m.state = "done"; save(); render();
  }

  let busy = false;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true; send.disabled = true; input.value = "";
    log.push({ role: "user", text }); save(); render();
    const wait = el("div", "chat-msg assistant chat-wait", "Thinking…");
    list.append(wait); list.scrollTop = list.scrollHeight;
    try {
      const msgs = log.filter((m) => m.role === "user" || m.role === "assistant").map((m) => ({ role: m.role, text: m.text }));
      const out = await post("/assistant/chat", { messages: msgs });
      log.push({ role: "assistant", text: out.reply || "(no reply)" });
      for (const p of out.proposals || []) log.push({ role: "proposal", id: p.id, summary: p.summary, state: "open" });
    } catch (e) {
      log.push({ role: "assistant", text: "Couldn't reach JobHelper: " + e.message });
    }
    busy = false; send.disabled = false; save(); render(); input.focus();
  };
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); } });

  const open = (yes) => { panel.hidden = !yes; fab.hidden = yes; if (yes) { render(); input.focus(); } else fab.focus(); };
  fab.onclick = () => open(true);
  close.onclick = () => open(false);
  clear.onclick = () => { log = []; save(); render(); };
  panel.addEventListener("keydown", (ev) => { if (ev.key === "Escape") open(false); });
})();
