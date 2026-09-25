// Smart Money Radar UI. Plain JS, no build step, no framework.

const state = {
  capabilities: null,
  config: null,
  scan: null,
  wallets: [],
  followSelection: new Set(),
  recording: new URLSearchParams(location.search).has("record"),
};

function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }

async function api(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}

function fmtUsd(n) {
  if (n === null || n === undefined) return "--";
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtPct(n) {
  if (n === null || n === undefined) return "--";
  return `${n > 0 ? "+" : ""}${n}%`;
}

// ---- tabs ----

function initTabs() {
  $all("nav.tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $all("nav.tabs button").forEach((b) => b.classList.remove("active"));
      $all(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#panel-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "runs") loadRuns();
    });
  });
}

// ---- capabilities / locked state ----

function lockedCardHtml(message, upgradeUrl) {
  return `<div class="locked-card">
    <p>🔒 ${message}</p>
    <a href="${upgradeUrl}" target="_blank" rel="noopener">See plans →</a>
  </div>`;
}

async function loadCapabilities() {
  state.capabilities = await api("/api/capabilities");
  state.config = await api("/api/config");
  const chainSel = $("#scan-chain");
  chainSel.innerHTML = state.config.chains.map((c) => `<option value="${c}">${c}</option>`).join("");
  const sourceSel = $("#scan-source");
  sourceSel.innerHTML = Object.entries(state.config.sources).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");

  if (!state.capabilities.analyst) {
    $("#scan-form").style.display = "none";
    $("#scan-locked").innerHTML = lockedCardHtml(
      "Scan needs top-trader and top-holder data to profile wallets. That is available on the Analyst plan and up.",
      state.capabilities.upgrade_url,
    );
    $("#scan-locked").style.display = "block";
    $("#wallets-locked").innerHTML = lockedCardHtml("Wallet profiling needs an Analyst plan or higher.", state.capabilities.upgrade_url);
    $("#wallets-locked").style.display = "block";
    $("#follow-locked").innerHTML = lockedCardHtml("Following wallets needs an Analyst plan or higher.", state.capabilities.upgrade_url);
    $("#follow-locked").style.display = "block";
  } else {
    $("#scan-form").style.display = "";
    $("#scan-locked").style.display = "none";
    $("#wallets-locked").style.display = "none";
    $("#follow-locked").style.display = "none";
  }

  const assumptions = state.config.assumptions;
  $("#assumptions-body").innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="label">Slippage</div><div class="value">${assumptions.slippage_bps} bps</div></div>
      <div class="stat"><div class="label">Fee</div><div class="value">${assumptions.fee_bps} bps</div></div>
      <div class="stat"><div class="label">Max position</div><div class="value">${Math.round(assumptions.max_position_pct * 100)}%</div></div>
      <div class="stat"><div class="label">Cooldown</div><div class="value">${assumptions.cooldown_s}s</div></div>
    </div>
    <p class="assumptions-note">${assumptions.latency_note} Edit <code>assumptions.yaml</code> to change these.</p>
  `;
}

// ---- Scan tab ----

function tokenChips(tokens) {
  return tokens.map((t) => `<span class="chip">${t.symbol}</span>`).join(" ");
}

function candidateRow(c) {
  const botBadge = c.likely_bot ? `<span class="badge-pill badge-bot_like">bot-like</span>` : "";
  return `<tr>
    <td><input type="checkbox" class="cand-check" data-address="${c.address}" ${c.likely_bot ? "" : "checked"}></td>
    <td>${c.short}</td>
    <td>${c.tokens_seen_in}</td>
    <td>${fmtUsd(c.realized_seen_usd)}</td>
    <td>${botBadge}</td>
  </tr>`;
}

async function runScan() {
  const chain = $("#scan-chain").value;
  const source = $("#scan-source").value;
  const n_tokens = parseInt($("#scan-n-tokens").value, 10);
  $("#scan-status").textContent = "Scanning...";
  const result = await api("/api/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chain, source, n_tokens }) });
  if (result.locked) {
    $("#scan-status").textContent = "";
    $("#scan-results").innerHTML = lockedCardHtml(result.feature, result.upgrade_url);
    return;
  }
  state.scan = result;
  $("#scan-status").textContent = `${result.tokens.length} tokens, ${result.candidates.length} candidate wallets`;
  $("#scan-results").innerHTML = `
    <div class="card">
      <h3>Tokens scanned</h3>
      <div class="row">${tokenChips(result.tokens)}</div>
    </div>
    <div class="card">
      <div class="row" style="justify-content: space-between;">
        <h3>Candidate wallets</h3>
        <button class="primary" id="profile-btn">Profile selected →</button>
      </div>
      <table>
        <thead><tr><th></th><th>Wallet</th><th>Tokens seen in</th><th>Realized PnL (seen)</th><th></th></tr></thead>
        <tbody>${result.candidates.slice(0, 60).map(candidateRow).join("")}</tbody>
      </table>
    </div>
  `;
  $("#profile-btn").addEventListener("click", profileSelected);
}

async function profileSelected() {
  const addresses = $all(".cand-check:checked").map((el) => el.dataset.address).slice(0, state.config.defaults.wallets_profiled);
  const budget = parseFloat($("#follow-budget") ? $("#follow-budget").value : state.config.defaults.budget_usd) || state.config.defaults.budget_usd;
  document.querySelector('nav.tabs button[data-tab="wallets"]').click();
  $("#wallets-status").textContent = `Profiling ${addresses.length} wallet(s)...`;
  const chain = $("#scan-chain").value;
  const profiles = await api("/api/wallets/profile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chain, addresses, budget }) });
  state.wallets = Array.isArray(profiles) ? profiles.filter((p) => !p.error) : [];
  $("#wallets-status").textContent = `${state.wallets.length} wallet(s) profiled`;
  renderWallets();
}

// ---- Wallets tab ----

function walletRow(w) {
  const checked = state.followSelection.has(w.address) ? "checked" : "";
  return `<tr class="clickable" data-address="${w.address}">
    <td><input type="checkbox" class="follow-check" data-address="${w.address}" ${checked}></td>
    <td>${w.address.slice(0, 6)}...${w.address.slice(-4)}</td>
    <td><span class="badge-pill badge-${w.label}">${w.label.replace("_", " ")}</span></td>
    <td>${w.skill_score}</td>
    <td>${w.copyability}</td>
    <td>${w.days_since_last_trade ?? "--"}d</td>
  </tr>`;
}

function renderWallets() {
  let list = state.wallets;
  const labelFilter = $("#wallet-filter-label").value;
  if (labelFilter !== "all") list = list.filter((w) => w.label === labelFilter);
  if ($("#wallet-filter-copyable").checked) list = list.filter((w) => w.copyability >= 50);
  list = [...list].sort((a, b) => b.skill_score - a.skill_score);
  $("#wallets-table-body").innerHTML = list.map(walletRow).join("") || `<tr><td colspan="6" class="empty-note">No wallets yet -- scan first.</td></tr>`;
  $all(".follow-check").forEach((el) => el.addEventListener("click", (e) => e.stopPropagation()));
  $all(".follow-check").forEach((el) =>
    el.addEventListener("change", (e) => {
      if (e.target.checked) state.followSelection.add(e.target.dataset.address);
      else state.followSelection.delete(e.target.dataset.address);
      updateFollowCount();
    }),
  );
  $all("tr.clickable").forEach((tr) => tr.addEventListener("click", () => openDrawer(tr.dataset.address)));
}

function updateFollowCount() {
  $("#follow-count").textContent = state.followSelection.size;
}

async function openDrawer(address) {
  const chain = $("#scan-chain").value;
  $("#drawer-backdrop").classList.add("open");
  $("#drawer-body").innerHTML = "<p>Loading...</p>";
  const d = await api(`/api/wallets/${address}/drawer?chain=${chain}`);
  if (d.locked) {
    $("#drawer-body").innerHTML = lockedCardHtml(d.feature, d.upgrade_url);
    return;
  }
  const caps = d.capabilities || {};
  const holdingsSection = caps.balances
    ? `<h3>Holdings</h3>
    <table><tbody>${(d.holdings || []).map((h) => `<tr><td>${h.symbol || "?"}</td><td>${fmtUsd(h.value_usd)}</td></tr>`).join("") || "<tr><td class='empty-note'>None on this chain</td></tr>"}</tbody></table>`
    : "";
  const tradesSection = caps.trades
    ? `<h3>Recent trades</h3>
    <table><tbody>${(d.recent_trades || []).slice(0, 10).map((t) => `<tr><td>${t.kind}</td><td>${fmtUsd(t.usd)}</td></tr>`).join("") || "<tr><td class='empty-note'>No recent trades</td></tr>"}</tbody></table>`
    : "";
  $("#drawer-body").innerHTML = `
    <h2>${address.slice(0, 8)}...${address.slice(-6)}</h2>
    <div class="stat-grid">
      <div class="stat"><div class="label">Lifetime realized</div><div class="value ${d.lifetime_realized_pnl_usd >= 0 ? "up" : "down"}">${fmtUsd(d.lifetime_realized_pnl_usd)}</div></div>
      <div class="stat"><div class="label">Tokens traded</div><div class="value">${d.tokens_traded ?? "--"}</div></div>
    </div>
    ${holdingsSection}
    <h3>Top performance by token</h3>
    <table><tbody>${(d.performance || []).slice(0, 8).map((p) => `<tr><td>${p.symbol || "?"}</td><td class="${(p.realized_pnl_usd || 0) >= 0 ? "up" : "down"}">${fmtUsd(p.realized_pnl_usd)}</td></tr>`).join("")}</tbody></table>
    ${tradesSection}
  `;
}

function closeDrawer() {
  $("#drawer-backdrop").classList.remove("open");
}

// ---- Follow tab ----

let followPoll = null;

async function startFollow() {
  const chain = $("#scan-chain").value;
  const budget = parseFloat($("#follow-budget").value) || state.config.defaults.budget_usd;
  const poll_s = parseFloat($("#follow-poll-s").value) || state.config.defaults.poll_s;
  let addresses = Array.from(state.followSelection);
  if (addresses.length === 0 && state.wallets.length) {
    addresses = [...state.wallets].sort((a, b) => b.copyability - a.copyability).slice(0, 5).map((w) => w.address);
  }
  const result = await api("/api/follow/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chain, addresses, budget, poll_s }) });
  if (result.locked) {
    $("#follow-status-panel").innerHTML = lockedCardHtml(result.feature, result.upgrade_url);
    return;
  }
  $("#follow-start").disabled = true;
  $("#follow-stop").disabled = false;
  pollFollowStatus();
  followPoll = setInterval(pollFollowStatus, 4000);
}

async function stopFollow() {
  await api("/api/follow/stop", { method: "POST" });
  $("#follow-start").disabled = false;
  $("#follow-stop").disabled = true;
  if (followPoll) clearInterval(followPoll);
}

async function pollFollowStatus() {
  const status = await api("/api/follow/status");
  if (!status.following) {
    $("#follow-status-panel").innerHTML = `<p class="empty-note">Not following yet. Pick wallets in the Wallets tab, or use the auto-picked top 5.</p>`;
    return;
  }
  const m = status.metrics;
  $("#follow-status-panel").innerHTML = `
    <div class="stat-grid">
      <div class="stat"><div class="label">PnL</div><div class="value ${m.pnl_usd >= 0 ? "up" : "down"}">${fmtUsd(m.pnl_usd)}</div></div>
      <div class="stat"><div class="label">PnL %</div><div class="value ${m.pnl_pct >= 0 ? "up" : "down"}">${fmtPct(m.pnl_pct)}</div></div>
      <div class="stat"><div class="label">Trades</div><div class="value">${m.trades}</div></div>
      <div class="stat"><div class="label">Win rate</div><div class="value">${m.win_rate ?? "--"}</div></div>
      <div class="stat"><div class="label">Max drawdown</div><div class="value">${m.max_drawdown_pct}%</div></div>
    </div>
    <p class="assumptions-note">${status.latency_note}</p>
    <h3>Recent decisions</h3>
    <table><tbody>${(status.recent_decisions || []).slice().reverse().map((d) => `<tr><td>${d.action}</td><td>${d.token || ""}</td><td>${d.reason || d.error || ""}</td></tr>`).join("") || "<tr><td class='empty-note'>No trades mirrored yet</td></tr>"}</tbody></table>
  `;
}

// ---- Runs tab ----

async function loadRuns() {
  const list = await api("/api/runs");
  $("#runs-table-body").innerHTML =
    list
      .map(
        (r) => `<tr>
      <td>${r.id}</td>
      <td>${r.mode}</td>
      <td>${r.metrics?.pnl_pct ?? r.metrics?.blind?.pnl_pct ?? "--"}</td>
      <td>${r.metrics?.credits_used ?? "--"}</td>
      <td><button class="ghost run-report" data-id="${r.id}">Report</button> <button class="ghost run-article" data-id="${r.id}">Article kit</button></td>
    </tr>`,
      )
      .join("") || `<tr><td colspan="5" class="empty-note">No runs yet. Try "make backtest" or "make forward".</td></tr>`;
  $all(".run-report").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const r = await api(`/api/runs/${btn.dataset.id}/report`, { method: "POST" });
      alert(`Report written: ${r.html}`);
    }),
  );
  $all(".run-article").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const handle = prompt("Your handle (for the cover image)?", "yourhandle") || "yourhandle";
      const r = await api(`/api/runs/${btn.dataset.id}/article-kit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ handle }) });
      alert(`Article kit written: ${r.article_draft}`);
    }),
  );
}

// ---- init ----

document.addEventListener("DOMContentLoaded", async () => {
  if (state.recording) document.body.classList.add("recording");
  initTabs();
  window.CGBrand.renderBadge($("#cg-badge"));
  window.CGBrand.renderFooter($("#cg-footer"));
  await loadCapabilities();
  $("#scan-run").addEventListener("click", runScan);
  $("#wallet-filter-label").addEventListener("change", renderWallets);
  $("#wallet-filter-copyable").addEventListener("change", renderWallets);
  $("#follow-auto-pick").addEventListener("click", () => {
    state.followSelection = new Set([...state.wallets].sort((a, b) => b.copyability - a.copyability).slice(0, 5).map((w) => w.address));
    updateFollowCount();
    renderWallets();
  });
  $("#follow-start").addEventListener("click", startFollow);
  $("#follow-stop").addEventListener("click", stopFollow);
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "drawer-backdrop") closeDrawer();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("#drawer-backdrop").classList.contains("open")) closeDrawer();
  });
});
