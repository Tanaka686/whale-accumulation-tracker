/* Whales tab: form, loading state and results. Plain JS, no build step. Self-contained (does not use app.js internals).
   Everything that comes from the API is escaped before it is put in the page. The API key never reaches the browser. */
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ADDR = /^0x[0-9a-fA-F]{40}$/;
  const FALLBACK_CFG = { networks: [{ id: "eth", label: "Ethereum" }, { id: "base", label: "Base" }, { id: "bsc", label: "BNB Chain" }], days: [7, 30], holders: [20, 50], default_holders: 50, credit_cap: 1500, credits_per_wallet: 20, explorers: { eth: "https://etherscan.io", base: "https://basescan.org", bsc: "https://bscscan.com" }, thresholds: { new_position_max_start_pct: 1, stance_pct: 2 } };
  const S = { cfg: FALLBACK_CFG, days: 7, holders: 50, data: null, sort: "absnet", dir: -1, showAll: false, clock: null, running: false };

  // ---------- formatting ----------
  const MINUS = "−";
  function money(v, signed = false) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    const n = Number(v), a = Math.abs(n);
    if (a < 0.5) return "$0";
    const body = a >= 1000 ? Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: a >= 1e6 ? 2 : 1 }).format(a) : Math.round(a).toLocaleString("en");
    return `${n < 0 ? MINUS : signed ? "+" : ""}$${body}`;
  }
  const moneyFull = (v) => (v == null ? "" : `${Number(v) < 0 ? MINUS : ""}$${Math.abs(Number(v)).toLocaleString("en", { maximumFractionDigits: 2 })}`);
  function price(v) {
    if (v == null) return "—";
    const n = Number(v), a = Math.abs(n);
    if (a >= 1) return `$${n.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (a === 0) return "$0";
    return `$${n.toFixed(Math.min(10, Math.max(2, Math.ceil(-Math.log10(a)) + 3)))}`;
  }
  const pct = (v, digits = 1) => (v == null ? "—" : `${v > 0 ? "+" : v < 0 ? MINUS : ""}${Math.abs(v) >= 100 ? Math.round(Math.abs(v)).toLocaleString("en") : Math.abs(v).toFixed(digits)}%`);
  const shortAddr = (a) => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—");
  const cls = (v) => (v > 0.5 ? "up" : v < -0.5 ? "down" : "dim");
  const cell = (v) => `<span class="${cls(v)}" title="${esc(moneyFull(v))}">${Math.abs(v ?? 0) < 0.5 ? "–" : money(v, true)}</span>`;
  const utc = (s) => {
    const d = new Date(`${s}:00Z`);
    return Number.isNaN(d.getTime()) ? s : d.toLocaleString("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" });
  };
  const walletLink = (net, a, text) => {
    const base = S.cfg.explorers?.[net];
    const label = esc(text ?? shortAddr(a));
    return base && ADDR.test(a || "") ? `<a href="${esc(base)}/address/${esc(a)}" target="_blank" rel="noopener" title="${esc(a)}">${label}</a>` : `<span title="${esc(a)}">${label}</span>`;
  };
  const tagsHtml = (w) => (w.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("");
  const STANCE_CLASS = { Accumulating: "acc", Distributing: "dist", Holding: "hold", "New position": "new", "Incomplete data": "inc", "Not scanned": "ns" };
  const pill = (s) => `<span class="stance ${STANCE_CLASS[s] || "err"}">${esc(s || "Error")}</span>`;
  const isComplete = (w) => !w.error && w.stance && w.stance !== "Incomplete data" && w.stance !== "Not scanned" && w.breakdown;

  // ---------- form ----------
  function seg(el, values, current, fmt, onPick) {
    el.innerHTML = values.map((v) => `<button type="button" data-v="${v}" class="${v === current ? "on" : ""}">${fmt(v)}</button>`).join("");
    el.querySelectorAll("button").forEach((b) => (b.onclick = () => { onPick(Number(b.dataset.v)); seg(el, values, Number(b.dataset.v), fmt, onPick); }));
  }
  function remember() { try { localStorage.setItem("whales-form", JSON.stringify({ network: $("#w-network").value, token: $("#w-token").value.trim(), days: S.days, holders: S.holders })); } catch (e) { /* storage can be blocked */ } }
  function restore() { try { return JSON.parse(localStorage.getItem("whales-form") || "null"); } catch (e) { return null; } }

  function buildForm() {
    const c = S.cfg;
    $("#w-network").innerHTML = c.networks.map((n) => `<option value="${esc(n.id)}">${esc(n.label)}</option>`).join("");
    const saved = restore();
    S.days = c.days.includes(saved?.days) ? saved.days : c.days[0];
    S.holders = c.holders.includes(saved?.holders) ? saved.holders : c.default_holders;
    if (saved?.network && c.networks.some((n) => n.id === saved.network)) $("#w-network").value = saved.network;
    if (saved?.token) $("#w-token").value = saved.token;
    seg($("#w-days"), c.days, S.days, (v) => `${v}d`, (v) => (S.days = v));
    seg($("#w-holders"), c.holders, S.holders, (v) => String(v), (v) => (S.holders = v));
    $("#w-cost").textContent = `Uses up to ${c.credits_per_wallet} credits per whale; a scan stops at ${c.credit_cap.toLocaleString("en")} credits.`;
    document.querySelectorAll("#panel-whales .chip[data-token]").forEach((b) => (b.onclick = () => { $("#w-network").value = b.dataset.network; $("#w-token").value = b.dataset.token; $("#w-formerr").textContent = ""; }));
    $("#w-form").onsubmit = (e) => { e.preventDefault(); run(); };
  }

  // ---------- loading + errors ----------
  function showLoading() {
    const t0 = Date.now();
    $("#w-status").innerHTML = `<div class="control-card w-loading" role="status" aria-live="polite"><div class="big-spin"></div><h3>Scanning whales…</h3><p>Reading the token's top holders, then every whale's trades and transfers for this token. Busy tokens can take a minute or two.</p><div class="w-bar"><i></i></div><div class="clock" id="w-clock">0s</div></div>`;
    clearInterval(S.clock);
    S.clock = setInterval(() => { const el = $("#w-clock"); if (el) el.textContent = `${Math.floor((Date.now() - t0) / 1000)}s`; }, 500);
  }
  function stopLoading() { clearInterval(S.clock); $("#w-status").innerHTML = ""; }
  function showError(msg) { $("#w-status").innerHTML = `<div class="card w-error"><b>Scan failed.</b> ${esc(msg)}</div>`; }
  function lockedCard(x) { return `<div class="locked-card"><div class="lock-icon">◈</div><b>${esc(x.feature || "This feature is plan-gated")}</b><a href="${esc(x.upgrade_url || "https://www.coingecko.com/en/api/pricing")}" target="_blank" rel="noopener">View plans ↗</a></div>`; }

  async function postScan(payload) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6 * 60 * 1000);
    try {
      const r = await fetch("/api/whales/scan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: ctl.signal });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error || `Request failed (${r.status})`);
      return data;
    } catch (e) {
      if (e.name === "AbortError") throw new Error("The scan took too long and was stopped. Try fewer holders or a shorter window.");
      throw e;
    } finally { clearTimeout(timer); }
  }

  async function run() {
    if (S.running) return;
    const token = $("#w-token").value.trim();
    const network = $("#w-network").value;
    if (!ADDR.test(token)) { $("#w-formerr").textContent = "Paste a token contract address: 0x followed by 40 hex characters."; $("#w-token").focus(); return; }
    $("#w-formerr").textContent = "";
    remember();
    S.running = true;
    const btn = $("#w-run");
    btn.disabled = true; btn.textContent = "Scanning…";
    $("#w-results").innerHTML = "";
    showLoading();
    try {
      const d = await postScan({ network, token, days: S.days, holders: S.holders });
      if (d.locked) { stopLoading(); $("#w-status").innerHTML = lockedCard(d); return; }
      stopLoading();
      S.data = d; S.sort = "absnet"; S.dir = -1; S.showAll = false;
      render();
    } catch (e) {
      stopLoading(); showError(e.message || "Something went wrong.");
    } finally { S.running = false; btn.disabled = false; btn.textContent = "Scan whales"; }
  }

  // ---------- results ----------
  const STD_WARNINGS = ["Only current top holders", "The starting balance"];
  function bannerItems(d) {
    const s = d.summary, items = [];
    for (const w of d.warnings || []) {
      if (STD_WARNINGS.some((p) => w.startsWith(p)) || w.startsWith("Incomplete data")) continue;
      items.push(esc(w));
    }
    if (s.incomplete > 0) items.push(`${s.incomplete} very active wallet${s.incomplete === 1 ? "" : "s"} (possible bot or market maker) ${s.incomplete === 1 ? "has" : "have"} incomplete data and ${s.incomplete === 1 ? "is" : "are"} left out of the totals.`);
    if (s.errors > 0) items.push(`${s.errors} wallet${s.errors === 1 ? "" : "s"} could not be loaded and ${s.errors === 1 ? "is" : "are"} left out of the totals.`);
    return items;
  }

  function header(d) {
    const net = d.network, sym = d.symbol || "?";
    const logo = d.image_url ? `<img src="${esc(d.image_url)}" alt="" onerror="this.remove()">` : esc(sym.slice(0, 2).toUpperCase());
    const chain = S.cfg.networks.find((n) => n.id === net)?.label || net;
    const scanned = new Date(d.scanned_at);
    return `<div class="control-card"><div class="w-head"><div class="w-logo">${logo}</div><div class="w-title"><h2>${esc(d.name || sym)}<span>${esc(sym)}</span></h2><div class="addr">${walletLink(net, d.token, d.token)}</div></div>
      <div class="w-meta"><div><span>Chain</span><b><i class="pill-chain">${esc(chain)}</i></b></div><div><span>Price</span><b>${esc(price(d.price_usd))}</b></div><div><span>Window</span><b>${esc(utc(d.window.from))} → ${esc(utc(d.window.to))} UTC <small class="dim">(${d.days}d)</small></b></div><div><span>Scanned</span><b>${Number.isNaN(scanned.getTime()) ? "" : esc(scanned.toLocaleString("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }))}</b></div></div></div></div>`;
  }

  function cards(d) {
    const s = d.summary, th = S.cfg.thresholds || {}, t = th.stance_pct ?? 2, n = th.new_position_max_start_pct ?? 1;
    const scanned = s.accumulating + s.new_position + s.holding + s.distributing;
    const card = (c, label, value, sub) => `<div class="w-card ${c}"><span>${label}</span><b>${value}</b><small>${sub}</small></div>`;
    return `<div class="w-cards">
      ${card("acc", "Accumulating", s.accumulating, `net flow above +${t}% of start`)}
      ${card("new", "New position", s.new_position, `held under ${n}% of today's balance at the start`)}
      ${card("hold", "Holding", s.holding, `within ±${t}%`)}
      ${card("dist", "Distributing", s.distributing, `net flow below ${MINUS}${t}%`)}
      <div class="w-card net"><span>Total whale net flow</span><b class="${cls(s.total_net_flow_usd)}" title="${esc(moneyFull(s.total_net_flow_usd))}">${money(s.total_net_flow_usd, true)}</b><small>${scanned} whales counted · ${s.total_net_flow_tokens < 0 ? MINUS : ""}${esc(Math.abs(Math.round(s.total_net_flow_tokens)).toLocaleString("en"))} ${esc(d.symbol || "")}</small></div></div>`;
  }

  function flow(d) {
    const t = d.summary.totals, keys = ["dex", "exchange", "other_transfers", "locked_lp"];
    const peak = Math.max(1, ...keys.map((k) => Math.abs(t[k].usd)));
    const mini = (v) => { const w = Math.min(50, (Math.abs(v) / peak) * 50); return `<div class="w-mini"><span class="zero" style="left:50%"></span><i class="${v >= 0 ? "pos" : "neg"}" style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%"></i></div>`; };
    const part = (k, label, sub, extra = "") => `<div class="part ${extra}"><span>${label}</span><b class="${extra ? "" : cls(t[k].usd)}" title="${esc(moneyFull(t[k].usd))}">${money(t[k].usd, true)}</b>${mini(t[k].usd)}<small>${sub}</small></div>`;
    return `<div class="card"><div class="table-head"><span>WHERE THE NET FLOW CAME FROM</span></div><div class="w-flow">
      ${part("dex", "DEX", "bought minus sold on exchanges like Uniswap")}
      ${part("exchange", "Exchange", "withdrawals from (+) and deposits to (−) the exchange wallets we set aside")}
      ${part("other_transfers", "Other transfers", "every other wallet-to-wallet transfer")}
      <div class="sep"></div>
      ${part("locked_lp", "Locked / LP", "moved to or from Voting Escrow, LPs, gauges or vaults. Shown separately: not a buy or a sell", "locked")}
    </div><div class="w-eq">DEX + Exchange + Other = <b class="${cls(d.summary.total_net_flow_usd)}">${money(d.summary.total_net_flow_usd, true)}</b> total net flow. Locked/LP is not included and never changes a stance.</div></div>`;
  }

  function chart(d) {
    const rows = d.whales.filter(isComplete);
    if (!rows.length) return "";
    let list = [...rows].sort((a, b) => Math.abs(b.net_flow_usd) - Math.abs(a.net_flow_usd));
    const limited = !S.showAll && list.length > 20;
    if (limited) list = list.slice(0, 20);
    list.sort((a, b) => b.net_flow_usd - a.net_flow_usd);
    const vals = list.map((w) => w.net_flow_usd), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1, zero = (-lo / span) * 100;
    const bar = (v) => (v >= 0 ? `<i class="pos" style="left:${zero}%;width:${(v / span) * 100}%"></i>` : `<i class="neg" style="left:${((v - lo) / span) * 100}%;width:${(-v / span) * 100}%"></i>`);
    const body = list.map((w) => `<div class="w-chart-row" title="${esc(`${w.stance}: ${w.stance_reason}`)}"><div class="who">#${esc(w.rank)} ${walletLink(d.network, w.address, shortAddr(w.address))}${(w.tags || []).map((t) => `<em>${esc(t)}</em>`).join("")}</div><div class="w-track"><span class="zero" style="left:${zero}%"></span>${bar(w.net_flow_usd)}</div><div class="val ${cls(w.net_flow_usd)}">${money(w.net_flow_usd, true)}</div></div>`).join("");
    const toggle = rows.length > 20 ? `<button class="ghost" id="w-chart-toggle" type="button">${limited ? `Show all ${rows.length} whales` : "Show the 20 biggest movers"}</button>` : "";
    return `<div class="card"><div class="table-head"><span>NET FLOW PER WHALE <small class="dim">· DEX + Exchange + Other, in USD</small></span>${toggle}</div>${body}<div class="w-chart-note"><span>◀ sold or sent out</span><span>bought or received ▶</span></div></div>`;
  }

  const COLS = [
    { k: "rank", t: "#", v: (w) => w.rank, num: true },
    { k: "wallet", t: "Wallet" },
    { k: "label", t: "Label / tag" },
    { k: "supply", t: "% supply", v: (w) => w.supply_pct, num: true },
    { k: "hold", t: "Holding", v: (w) => w.balance_usd, num: true },
    { k: "dex", t: "DEX", v: (w) => w.breakdown.dex.usd, num: true },
    { k: "exchange", t: "Exchange", v: (w) => w.breakdown.exchange.usd, num: true },
    { k: "other", t: "Other", v: (w) => w.breakdown.other_transfers.usd, num: true },
    { k: "locked", t: "Locked/LP", v: (w) => w.breakdown.locked_lp.usd, num: true },
    { k: "net", t: "Net flow", v: (w) => w.net_flow_usd, num: true },
    { k: "chg", t: "Change", v: (w) => w.net_flow_pct_of_start ?? -Infinity, num: true },
    { k: "stance", t: "Stance", v: (w) => w.stance },
    { k: "reason", t: "Reason" },
  ];
  const sortVal = (w) => {
    if (S.sort === "absnet") return Math.abs(w.net_flow_usd);
    if (S.sort === "net") return w.net_flow_usd;
    return COLS.find((c) => c.k === S.sort)?.v(w);
  };
  function table(d) {
    const rows = d.whales.filter(isComplete);
    if (!rows.length) return `<div class="card w-empty">No whales left after filtering: the top holders are all exchanges, contracts or pools, or none could be scanned. Open "Excluded wallets" below to see why.</div>`;
    rows.sort((a, b) => { const x = sortVal(a), y = sortVal(b); return (x < y ? -1 : x > y ? 1 : 0) * S.dir; });
    const arrow = (c) => ((S.sort === c.k || (c.k === "net" && (S.sort === "absnet" || S.sort === "net"))) ? (S.dir < 0 ? " ↓" : " ↑") : "");
    const head = COLS.map((c) => `<th class="${c.v ? "sort" : ""} ${c.num ? "num" : ""} ${S.sort === c.k || (c.k === "net" && S.sort === "absnet") ? "sorted" : ""}" data-k="${c.k}" ${c.k === "net" ? 'title="Click to cycle: biggest movers, most bought, most sold"' : ""}>${esc(c.t)}${arrow(c)}</th>`).join("");
    const body = rows.map((w) => {
      const b = w.breakdown, isNew = w.stance === "New position";
      return `<tr><td class="num dim">${esc(w.rank)}</td><td class="wallet">${walletLink(d.network, w.address)}</td><td>${tagsHtml(w)}${w.label ? `<span class="lbl">${esc(w.label)}</span>` : w.tags?.length ? "" : '<span class="dim">—</span>'}</td><td class="num">${w.supply_pct == null ? "—" : `${w.supply_pct.toFixed(w.supply_pct < 1 ? 2 : 1)}%`}</td><td class="num" title="${esc(moneyFull(w.balance_usd))}">${money(w.balance_usd)}</td><td class="num">${cell(b.dex.usd)}</td><td class="num">${cell(b.exchange.usd)}</td><td class="num">${cell(b.other_transfers.usd)}</td><td class="num locked-col" title="${esc(moneyFull(b.locked_lp.usd))}">${Math.abs(b.locked_lp.usd) < 0.5 ? '<span class="dim">–</span>' : money(b.locked_lp.usd, true)}</td><td class="num net ${cls(w.net_flow_usd)}" title="${esc(moneyFull(w.net_flow_usd))}">${money(w.net_flow_usd, true)}</td><td class="num ${isNew ? "dim" : cls(w.net_flow_pct_of_start)}">${isNew ? "new" : pct(w.net_flow_pct_of_start)}</td><td>${pill(w.stance)}</td><td class="reason">${esc(w.stance_reason)}</td></tr>`;
    }).join("");
    return `<div class="card"><div class="table-head"><span>WHALES <small class="dim">· ${rows.length} scanned · click a column to sort</small></span></div><div class="w-table-wrap"><table class="w-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></div>`;
  }

  function folds(d) {
    const out = [];
    const inc = d.whales.filter((w) => w.stance === "Incomplete data");
    if (inc.length) {
      out.push(`<details class="w-fold"><summary>Incomplete data <small>· ${inc.length} wallet${inc.length === 1 ? "" : "s"} · left out of the totals</small></summary><div class="inner"><div class="w-table-wrap"><table class="w-table"><thead><tr><th>#</th><th>Wallet</th><th>Label / tag</th><th class="num">Holding</th><th>Why</th></tr></thead><tbody>${inc.map((w) => `<tr><td class="dim">${esc(w.rank)}</td><td class="wallet">${walletLink(d.network, w.address)}</td><td>${tagsHtml(w)}${esc(w.label || "—")}</td><td class="num">${money(w.balance_usd)}</td><td class="reason">${esc(w.stance_reason)}</td></tr>`).join("")}</tbody></table></div></div></details>`);
    }
    const ns = d.whales.filter((w) => w.stance === "Not scanned");
    if (ns.length) {
      out.push(`<details class="w-fold"><summary>Not scanned <small>· ${ns.length} wallet${ns.length === 1 ? "" : "s"} · the scan's credit cap was reached</small></summary><div class="inner"><div class="w-table-wrap"><table class="w-table"><tbody>${ns.map((w) => `<tr><td class="dim">${esc(w.rank)}</td><td class="wallet">${walletLink(d.network, w.address)}</td><td class="num">${money(w.balance_usd)}</td></tr>`).join("")}</tbody></table></div></div></details>`);
    }
    const bad = d.whales.filter((w) => w.error);
    if (bad.length) {
      out.push(`<details class="w-fold"><summary>Could not be loaded <small>· ${bad.length} wallet${bad.length === 1 ? "" : "s"}</small></summary><div class="inner"><div class="w-table-wrap"><table class="w-table"><tbody>${bad.map((w) => `<tr><td class="dim">${esc(w.rank)}</td><td class="wallet">${walletLink(d.network, w.address)}</td><td class="reason">${esc(w.error)}</td></tr>`).join("")}</tbody></table></div></div></details>`);
    }
    const ex = d.excluded || [];
    const group = ex.reduce((m, e) => { m[e.reason_code] = (m[e.reason_code] || 0) + 1; return m; }, {});
    const names = { exchange_label: "exchange", contract_label: "contract", liquidity_pool: "pool", burn_address: "burn", token_contract: "token contract" };
    const summary = Object.entries(group).map(([k, n]) => `${n} ${names[k] || k}`).join(" · ");
    out.push(`<details class="w-fold"><summary>Excluded wallets <small>· ${ex.length} set aside${summary ? ` (${esc(summary)})` : ""} · not whales</small></summary><div class="inner">${ex.length ? `<div class="w-table-wrap"><table class="w-table"><thead><tr><th>#</th><th>Wallet</th><th>Label</th><th class="num">% supply</th><th class="num">Holding</th><th>Reason</th></tr></thead><tbody>${ex.map((e) => `<tr><td class="dim">${esc(e.rank)}</td><td class="wallet w-ex">${walletLink(d.network, e.address)}</td><td>${e.label ? esc(e.label) : '<span class="dim">—</span>'}</td><td class="num">${e.supply_pct == null ? "—" : `${e.supply_pct.toFixed(1)}%`}</td><td class="num">${money(e.balance_usd)}</td><td class="reason">${esc(e.reason)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="w-empty">Nothing was excluded.</div>'}</div></details>`);
    const notes = (d.warnings || []).filter((w) => STD_WARNINGS.some((p) => w.startsWith(p)));
    out.push(`<details class="w-fold"><summary>How to read this <small>· limits of the method</small></summary><div class="inner"><ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}<li>The starting balance is today's balance minus the net flow (Locked/LP included, since it moved the balance). Only DEX + Exchange + Other decide the stance.</li><li>"Exchange" means the wallets CoinGecko labels as hot or cold wallets or exchanges among the top holders. Exchange deposit addresses that are not in the holder list appear under "Other".</li></ul></div></details>`);
    return out.join("");
  }

  function footer(d) {
    const cap = S.cfg.credit_cap;
    return `<div class="w-foot"><span><b>${esc(d.credits)}</b> CoinGecko API credits used${cap ? ` (cap ${cap.toLocaleString("en")})` : ""}</span><span>Scan took <b>${d.elapsed_ms == null ? "—" : `${(d.elapsed_ms / 1000).toFixed(1)}s`}</b></span><span>Saved as <code>${esc(d.saved_to || "not saved")}</code></span></div>`;
  }

  function render() {
    const d = S.data;
    if (!d) return;
    const items = bannerItems(d);
    const banner = items.length ? `<div class="w-banner" role="alert"><b>Heads up</b><ul>${items.map((i) => `<li>${i}</li>`).join("")}</ul></div>` : "";
    $("#w-results").innerHTML = header(d) + banner + cards(d) + flow(d) + chart(d) + table(d) + folds(d) + footer(d);
    const toggle = $("#w-chart-toggle");
    if (toggle) toggle.onclick = () => { S.showAll = !S.showAll; render(); };
    document.querySelectorAll("#w-results th.sort").forEach((th) => (th.onclick = () => {
      const k = th.dataset.k;
      if (k === "net") {
        // cycle: biggest movers -> most bought -> most sold
        if (S.sort === "absnet") { S.sort = "net"; S.dir = -1; } else if (S.sort === "net" && S.dir < 0) { S.dir = 1; } else { S.sort = "absnet"; S.dir = -1; }
      } else if (S.sort === k) { S.dir = -S.dir; } else { S.sort = k; S.dir = k === "rank" || k === "stance" ? 1 : -1; }
      render();
    }));
  }

  // ---------- boot ----------
  async function boot() {
    try { const r = await fetch("/api/whales/config"); if (r.ok) S.cfg = { ...FALLBACK_CFG, ...(await r.json()) }; } catch (e) { /* the fallback keeps the form usable */ }
    buildForm();
    try {
      const caps = await (await fetch("/api/capabilities")).json();
      if (!caps.analyst) {
        $("#w-locked").innerHTML = lockedCard({ feature: "Whale scans need the Analyst plan or higher (top holders and wallet endpoints).", upgrade_url: caps.upgrade_url });
        $("#w-run").disabled = true;
      }
    } catch (e) { /* the scan itself reports a locked plan */ }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
