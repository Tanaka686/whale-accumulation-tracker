"""FastAPI app: serves the web/ pages and the Radar / X-ray / Wallet / Follow / Runs API.

Pages: /  (Radar)   /xray   /wallet   /follow   /runs   /kit?run=<id>
Every paid-plan feature checks the plan probed at startup first and returns a locked payload with
the pricing link instead of hanging. Every call that a chain doesn't support comes back with a
per-section status the UI turns into a one-line notice; the rest of the page keeps working.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from core import wallets as w
from core.articlekit import build as build_article_kit
from core.assumptions import load as load_assumptions
from core.assumptions import save as save_assumptions
from core.client import CoinGeckoClient, CoinGeckoError, PlanRestrictedError
from core.plan import locked, probe_capabilities
from core.recorder import Recorder, list_fixtures, read_all
from core.recorder import replay as replay_fixture
from core.report import build as build_report
from core.store import Store

from . import backtest as backtest_mod
from . import chains, config, profile, runs, scan, xray
from .follow import FollowEngine

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
RECORDINGS = Path("data/recordings")
CHAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}$")
ADDR_RE = re.compile(r"^[A-Za-z0-9]{20,90}$")
log = logging.getLogger("app.server")

state: dict = {
    "capabilities": None,
    "follow": None,
    "follow_task": None,
    "last_scan": None,
    "last_profiles": {},
    "jobs": {},
    "autopilot": None,
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = CoinGeckoClient()
    state["capabilities"] = await probe_capabilities(app.state.client)
    state["store"] = Store("state.db")
    yield
    for key in ("follow_task",):
        if state.get(key):
            state[key].cancel()
    if state.get("autopilot") and state["autopilot"].get("task"):
        state["autopilot"]["task"].cancel()
    state["store"].close()
    await app.state.client.close()


app = FastAPI(title="Smart Money Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
app.mount("/core-web", StaticFiles(directory=str(ROOT / "core" / "web")), name="core-web")
app.mount("/brand", StaticFiles(directory=str(ROOT / "core" / "brand")), name="brand")


@app.middleware("http")
async def no_cache_pages(request: Request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ---------- helpers ----------


def caps() -> dict:
    return state["capabilities"] or {"analyst": False, "paid": False, "upgrade_url": config.core_config.PRICING_URL}


def upgrade_url() -> str:
    return caps().get("upgrade_url") or config.core_config.PRICING_URL


def _chain(chain: str) -> str:
    if not CHAIN_RE.match(chain or ""):
        raise HTTPException(400, "unknown chain id")
    return chain


def _addr(address: str) -> str:
    address = (address or "").strip()
    if not ADDR_RE.match(address):
        raise HTTPException(400, "that doesn't look like an address")
    return address


def sse(gen, rec: Recorder | None = None):
    """Wraps an async (event, data) generator as a text/event-stream, optionally recording it."""

    async def stream():
        try:
            async for ev, data in gen:
                if rec:
                    rec.write(ev, data)
                yield f"event: {ev}\ndata: {json.dumps(data, default=str)}\n\n"
        except PlanRestrictedError:
            yield f"event: locked\ndata: {json.dumps(locked('This needs a paid plan.', upgrade_url()))}\n\n"
        except CoinGeckoError as e:
            yield f"event: fail\ndata: {json.dumps({'error': f'CoinGecko API error {e.status}. Try again, or another chain.'})}\n\n"
        except Exception as e:  # keep the stream well-formed; never leak internals
            log.exception("stream failed")
            yield f"event: fail\ndata: {json.dumps({'error': type(e).__name__})}\n\n"
        finally:
            if rec:
                rec.close()

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def locked_stream(message: str):
    async def gen():
        yield "locked", locked(message, upgrade_url())

    return sse(gen())


def recorder(demo: str, tag: str) -> Recorder:
    RECORDINGS.parent.mkdir(parents=True, exist_ok=True)
    return Recorder(demo, re.sub(r"[^a-z0-9_-]", "", tag.lower())[:20] or "session", fixtures_dir=RECORDINGS)


# ---------- pages ----------

PAGES = {"/": "index.html", "/xray": "index.html", "/wallet": "index.html", "/follow": "index.html", "/runs": "index.html", "/kit": "index.html"}


def _page(name: str):
    return lambda: FileResponse(str(WEB / name))


for _path, _file in PAGES.items():
    app.add_api_route(_path, _page(_file), methods=["GET"], include_in_schema=False)


# ---------- meta ----------


@app.get("/api/capabilities")
async def api_capabilities():
    return state["capabilities"] or await probe_capabilities(app.state.client)


@app.get("/api/config")
def api_config():
    return {
        "sources": config.SOURCES,
        "default_chain": config.DEFAULT_CHAIN,
        "defaults": {
            "n_tokens": config.DEFAULT_TOP_N_TOKENS,
            "wallets_profiled": config.DEFAULT_WALLETS_PROFILED,
            "budget_usd": config.DEFAULT_BUDGET_USD,
            "poll_s": config.DEFAULT_FOLLOW_POLL_S,
            "top_k": config.DEFAULT_TOP_K_FOLLOW,
        },
        "wallet_caps": config.core_config.WALLET_CHAIN_CAPS,
        "label_rules": w.LABEL_RULES,
        "assumptions": load_assumptions(),
        "repo_name": config.REPO_NAME,
    }


@app.get("/api/stats")
def api_stats():
    engine = state.get("follow")
    pilot = state.get("autopilot") or {}
    return {
        "credits_used": app.state.client.credits_used,
        "following": bool(state.get("follow_task") and not state["follow_task"].done()),
        "follow_polls": engine.polls if engine else 0,
        "autopilot": bool(pilot.get("task") and not pilot["task"].done()),
    }


@app.get("/api/chains")
async def api_chains():
    return await chains.catalog(app.state.client)


@app.get("/api/assumptions")
def api_assumptions():
    return load_assumptions()


@app.post("/api/assumptions")
def api_assumptions_save(body: dict):
    current = load_assumptions()
    for k in ("slippage_bps", "fee_bps", "starting_cash", "max_position_pct", "cooldown_s"):
        if k in body:
            try:
                current[k] = float(body[k])
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number")
    save_assumptions(current)
    return current


# ---------- search + trending (Demo-friendly) ----------


def pool_row(p: dict) -> dict:
    """A GeckoTerminal-style pool row for search results and token strips."""
    a = p.get("attributes", {})
    base = p.get("_base_token") or {}
    pid = p.get("id", "")
    network = pid.split("_", 1)[0] if "_" in pid else None
    base_id = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
    change = a.get("price_change_percentage") or {}
    tx = (a.get("transactions") or {}).get("h24") or {}
    image = base.get("image_url")
    return {
        "id": pid,
        "network": network,
        "pool": a.get("address"),
        "pool_name": a.get("name"),
        "dex": p.get("_dex"),
        "token": {
            "address": base.get("address") or (base_id.split("_", 1)[1] if "_" in base_id else None),
            "symbol": base.get("symbol") or (a.get("name") or "").split(" / ")[0],
            "name": base.get("name"),
            "image_url": image if image and "missing" not in image else None,
        },
        "price_usd": w._f(a.get("base_token_price_usd")),
        "change_1h": w._f(change.get("h1")),
        "change_24h": w._f(change.get("h24")),
        "liquidity_usd": w._f(a.get("reserve_in_usd")),
        "volume_24h_usd": w._f((a.get("volume_usd") or {}).get("h24")),
        "fdv_usd": w._f(a.get("fdv_usd")),
        "txns_24h": (tx.get("buys") or 0) + (tx.get("sells") or 0),
        "created_at": a.get("pool_created_at"),
    }


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=2, max_length=120), chain: str | None = None):
    q = q.strip()
    kind = w.address_kind(q)
    out = {"query": q, "wallet": {"address": q, "kind": kind} if kind else None, "pools": []}
    try:
        pools = await app.state.client.search_pools(q, _chain(chain) if chain else None)
        out["pools"] = [pool_row(p) for p in pools][:20]
    except CoinGeckoError:
        out["error"] = "search is unavailable right now"
    return out


@app.get("/api/trending")
async def api_trending(chain: str = config.DEFAULT_CHAIN, source: str = "trending_1h", n: int = Query(12, ge=1, le=30)):
    chain = _chain(chain)
    try:
        if source == "new_pools":
            pools = await app.state.client.new_pools(chain, n=40)
        elif source == "safe_movers":
            if not caps().get("analyst"):
                return locked("Safe movers uses the pools megafilter (Analyst plan or higher).", upgrade_url())
            pools = await app.state.client.megafilter(networks=chain, **config.SAFE_MOVERS_FILTERS)
        else:
            pools = await app.state.client.trending_pools(chain, "24h" if source == "trending_24h" else "1h", n=40)
    except PlanRestrictedError:
        return locked("This source needs a higher plan.", upgrade_url())
    except CoinGeckoError as e:
        return {"unavailable": True, "status": e.status, "rows": []}
    rows, seen = [], set()
    for p in pools:
        r = pool_row(p)
        key = (r["token"]["address"] or r["pool"] or "").lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    return {"rows": rows[:n]}


# ---------- Radar ----------


@app.post("/api/scan")
async def api_scan(body: dict):
    if not caps().get("analyst"):
        return locked("Scan needs the top_traders endpoint (Analyst plan or higher).", upgrade_url())
    chain = _chain(body.get("chain", config.DEFAULT_CHAIN))
    source = body.get("source", "trending_1h")
    n_tokens = max(1, min(int(body.get("n_tokens", config.DEFAULT_TOP_N_TOKENS)), 20))
    credits0 = app.state.client.credits_used
    t0 = time.perf_counter()
    try:
        result = await scan.scan(app.state.client, chain, source, n_tokens)
    except PlanRestrictedError:
        return locked("This source needs a higher plan.", upgrade_url())
    except CoinGeckoError as e:
        return JSONResponse({"unavailable": True, "status": e.status, "chain": chain}, status_code=200)
    result["candidates"] = result["candidates"][:150]
    result["batch"] = secrets.token_hex(5)
    result["credits"] = app.state.client.credits_used - credits0
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    state["last_scan"] = result
    return result


@app.get("/api/radar/profile")
async def api_radar_profile(chain: str, ids: str, budget: float = config.DEFAULT_BUDGET_USD):
    if not caps().get("analyst"):
        return locked_stream("Wallet profiling needs the wallet endpoints (Analyst plan or higher).")
    chain = _chain(chain)
    addresses = [a for a in dict.fromkeys(x.strip() for x in ids.split(",")) if ADDR_RE.match(a)][:60]
    rec = recorder("radar", chain)
    if state.get("last_scan") and state["last_scan"].get("chain") == chain:
        rec.write("scan", state["last_scan"])

    async def gen():
        async for ev, data in profile.stream_profiles(app.state.client, chain, addresses, budget):
            if ev == "wallet" and not data.get("error"):
                state["last_profiles"][data["address"].lower()] = data
            yield ev, data

    return sse(gen(), rec)


@app.get("/api/recordings")
def api_recordings(demo: str = Query("radar", pattern="^(radar|xray)$")):
    return list_fixtures(demo, RECORDINGS)


@app.get("/api/radar/snapshot")
def api_radar_snapshot(name: str):
    try:
        recs = read_all(name, RECORDINGS)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "recording not found")
    scan_ev = next((r["data"] for r in recs if r["ev"] == "scan"), None)
    if not scan_ev:
        raise HTTPException(404, "recording has no scan")
    return scan_ev


@app.get("/api/radar/replay")
async def api_radar_replay(name: str, speed: float = 1.0):
    try:
        read_all(name, RECORDINGS)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "recording not found")
    return sse(replay_fixture(name, speed=speed, skip=("scan",), fixtures_dir=RECORDINGS))


@app.post("/api/wallets/profile")
async def api_wallets_profile(body: dict):
    if not caps().get("analyst"):
        return locked("Wallet profiling needs the wallet endpoints (Analyst plan or higher).", upgrade_url())
    chain = _chain(body.get("chain", config.DEFAULT_CHAIN))
    addresses = body.get("addresses", [])[: config.DEFAULT_WALLETS_PROFILED]
    budget = float(body.get("budget", config.DEFAULT_BUDGET_USD))
    return await profile.profile_candidates(app.state.client, chain, addresses, budget)


# ---------- Wallet profile ----------


@app.get("/api/wallet/profile")
async def api_wallet_profile(address: str, chain: str = "auto", budget: float = config.DEFAULT_BUDGET_USD):
    address = _addr(address)
    if not w.address_kind(address):
        raise HTTPException(400, "Paste an EVM (0x...) or Solana address.")
    if chain != "auto":
        chain = _chain(chain)
    if not caps().get("analyst"):
        return locked("Wallet profiles use the wallet PnL, trades and balances endpoints (Analyst plan or higher).", upgrade_url())
    try:
        data = await profile.wallet_page(app.state.client, address, chain, budget)
    except PlanRestrictedError:
        return locked("Wallet profiles need an Analyst plan or higher.", upgrade_url())
    data["following"] = any(f["address"].lower() == address.lower() for f in follow_list())
    return data


# ---------- Token X-ray ----------


@app.get("/api/xray/token")
async def api_xray_token(chain: str, token: str, pool: str | None = None):
    chain, token = _chain(chain), _addr(token)
    try:
        ctx = await xray.token_context(app.state.client, chain, token, pool)
    except CoinGeckoError as e:
        raise HTTPException(404, f"Couldn't find that token on {chains.label(chain)} ({e.status}).")
    return xray.public_context(ctx)


@app.get("/api/xray/run")
async def api_xray_run(chain: str, token: str, budget: float = config.DEFAULT_BUDGET_USD):
    if not caps().get("analyst"):
        return locked_stream("The wallet X-ray uses top holders, top traders and wallet PnL (Analyst plan or higher).")
    chain, token = _chain(chain), _addr(token)
    try:
        ctx = await xray.token_context(app.state.client, chain, token)
    except CoinGeckoError:
        ctx = {}
    return sse(xray.run(app.state.client, chain, token, budget, ctx), recorder("xray", chain))


# ---------- Follow (paper copy-trade) ----------


def follow_list() -> list[dict]:
    return state["store"].get("follow_list", []) if state.get("store") else []


def _save_follow_list(items: list[dict]):
    state["store"].set("follow_list", items)


@app.get("/api/follow/list")
def api_follow_list():
    return follow_list()


@app.post("/api/follow/add")
def api_follow_add(body: dict):
    address = _addr(body.get("address", ""))
    chain = _chain(body.get("chain", config.DEFAULT_CHAIN))
    items = [f for f in follow_list() if f["address"].lower() != address.lower()]
    meta = state["last_profiles"].get(address.lower()) or {}
    items.append(
        {
            "address": address,
            "chain": chain,
            "label": body.get("label") or meta.get("label"),
            "skill_score": body.get("skill_score", meta.get("skill_score")),
            "copyability": body.get("copyability", meta.get("copyability")),
            "added_ts": time.time(),
        }
    )
    _save_follow_list(items)
    if state.get("follow") and state.get("follow_task") and not state["follow_task"].done():
        state["follow"].targets.append({"address": address, "chain": chain})
    return items


@app.post("/api/follow/remove")
def api_follow_remove(body: dict):
    address = (body.get("address") or "").lower()
    items = [f for f in follow_list() if f["address"].lower() != address]
    _save_follow_list(items)
    if state.get("follow"):
        state["follow"].targets = [t for t in state["follow"].targets if t["address"].lower() != address]
    return items


@app.post("/api/follow/autopick")
def api_follow_autopick(body: dict | None = None):
    body = body or {}
    k = int(body.get("k", config.DEFAULT_TOP_K_FOLLOW))
    profiles = [p for p in state["last_profiles"].values() if p.get("copyable") and p.get("label") != "bot_like"]
    profiles.sort(key=lambda p: (p.get("skill_score") or 0, p.get("copyability") or 0), reverse=True)
    if not profiles:
        return {"picked": [], "list": follow_list(), "message": "Profile some wallets on the Radar first."}
    items = {f["address"].lower(): f for f in follow_list()}
    picked = []
    for p in profiles[:k]:
        items[p["address"].lower()] = {
            "address": p["address"],
            "chain": p["chain"],
            "label": p.get("label"),
            "skill_score": p.get("skill_score"),
            "copyability": p.get("copyability"),
            "added_ts": time.time(),
        }
        picked.append(p["address"])
    _save_follow_list(list(items.values()))
    return {"picked": picked, "list": list(items.values())}


@app.post("/api/follow/start")
async def api_follow_start(body: dict):
    if not caps().get("analyst"):
        return locked("Following wallets needs the wallet endpoints (Analyst plan or higher).", upgrade_url())
    if state.get("follow_task") and not state["follow_task"].done():
        raise HTTPException(400, "already following; stop first")
    chain = _chain(body.get("chain", config.DEFAULT_CHAIN))
    addresses = body.get("addresses") or [{"address": f["address"], "chain": f["chain"]} for f in follow_list()]
    if not addresses:
        raise HTTPException(400, "follow at least one wallet first")
    budget = float(body.get("budget", config.DEFAULT_BUDGET_USD))
    poll_s = max(10.0, float(body.get("poll_s", config.DEFAULT_FOLLOW_POLL_S)))
    engine = FollowEngine(app.state.client, chain, addresses, budget, load_assumptions(), poll_s)
    state["follow"] = engine
    state["run_dir"] = runs.new_run_dir("forward")
    run_dir = state["run_dir"]

    def persist():
        runs.write_metrics(run_dir, {"mode": "forward", "chain": chain, "addresses": engine.addresses, "credits_used": app.state.client.credits_used, **engine.status()})
        runs.write_equity(run_dir, engine.portfolio.equity_curve)
        runs.write_trades_csv(run_dir, engine.portfolio.closed)

    async def loop():
        try:
            while True:
                for d in await engine.poll_once():
                    runs.append_decision(run_dir, d)
                persist()
                await asyncio.sleep(engine.poll_s)
        finally:
            persist()

    state["follow_task"] = asyncio.create_task(loop())
    return {"started": True, "run": run_dir.name}


@app.post("/api/follow/stop")
async def api_follow_stop():
    task = state.get("follow_task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        state["follow_task"] = None
    return {"stopped": True, "run": state["run_dir"].name if state.get("run_dir") else None}


@app.get("/api/follow/status")
def api_follow_status():
    running = bool(state.get("follow_task") and not state["follow_task"].done())
    if not state.get("follow"):
        return {"following": False, "running": False, "list": follow_list()}
    return {
        "following": True,
        "running": running,
        "run": state["run_dir"].name if state.get("run_dir") else None,
        "credits_used": app.state.client.credits_used,
        "list": follow_list(),
        **state["follow"].status(),
    }


# ---------- Runs, reports, article kits ----------


@app.get("/api/runs")
def api_runs():
    return runs.run_cards()


def _run_dir(run_id: str) -> Path:
    if run_id == "latest":
        run_id = runs.resolve_run_id(run_id)
    path = runs.safe_run_dir(run_id)
    if not path:
        raise HTTPException(404, "no such run")
    return path


def _build_report(run_dir: Path) -> dict:
    metrics = runs.read_metrics(run_dir)
    if not metrics:
        raise HTTPException(404, "no metrics for this run")
    equity = runs.read_equity(run_dir)
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    return build_report(run_dir.name, scenario_metrics, equity, credits_used=metrics.get("credits_used", 0), out_dir=run_dir.parent)


def _build_kit(run_dir: Path, handle: str, screenshot_urls: list[str] | None) -> dict:
    metrics = runs.read_metrics(run_dir)
    if not metrics:
        raise HTTPException(404, "no metrics for this run")
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    return build_article_kit(
        title=f"Smart Money Radar: {run_dir.name}",
        handle=handle,
        metrics=scenario_metrics,
        equity_curve=runs.read_equity(run_dir),
        credits_used=metrics.get("credits_used", 0),
        out_dir=run_dir / "article-kit",
        screenshot_urls=screenshot_urls,
    )


@app.post("/api/runs/{run_id}/report")
def api_run_report(run_id: str):
    run_dir = _run_dir(run_id)
    paths = _build_report(run_dir)
    return {**paths, "url": f"/runs/{run_dir.name}/files/report.html"}


@app.post("/api/runs/{run_id}/article-kit")
async def api_run_article_kit(run_id: str, request: Request, body: dict | None = None):
    body = body or {}
    run_dir = _run_dir(run_id)
    base = str(request.base_url).rstrip("/")
    shots = [f"{base}/runs?record=1", f"{base}/follow?record=1"] if body.get("screenshots", True) else None
    try:
        paths = await run_in_threadpool(_build_kit, run_dir, body.get("handle", "yourhandle"), shots)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {**paths, "url": f"/kit?run={run_dir.name}"}


@app.get("/api/runs/{run_id}/kit")
def api_run_kit_manifest(run_id: str):
    """What the article-kit preview page shows: every asset with view/download URLs, plus the draft text."""
    run_dir = _run_dir(run_id)
    kit = run_dir / "article-kit"
    if not (kit / "article-draft.md").exists():
        return {"built": False, "run": run_dir.name}
    assets = []
    order = ["cover.png", "report-card.png", "equity.png", "architecture.png"]
    files = [kit / f for f in order if (kit / f).exists()]
    if (kit / "screenshots").exists():
        files += sorted((kit / "screenshots").glob("*.png"))
    for f in files:
        rel = f.relative_to(run_dir).as_posix()
        assets.append({"name": f.name, "url": f"/runs/{run_dir.name}/files/{rel}", "download": f"/runs/{run_dir.name}/files/{rel}?download=1"})
    return {
        "built": True,
        "run": run_dir.name,
        "summary": runs.summarize(run_dir),
        "assets": assets,
        "draft": (kit / "article-draft.md").read_text(),
        "draft_download": f"/runs/{run_dir.name}/files/article-kit/article-draft.md?download=1",
    }


@app.get("/runs/{run_id}/report")
def run_report_page(run_id: str):
    """Builds the report if needed, then shows it (the Runs page opens this in a new tab)."""
    run_dir = _run_dir(run_id)
    if not (run_dir / "report.html").exists():
        _build_report(run_dir)
    return RedirectResponse(f"/runs/{run_dir.name}/files/report.html")


@app.get("/runs/{run_id}/files/{path:path}")
def run_file(run_id: str, path: str, download: int = 0):
    run_dir = _run_dir(run_id).resolve()
    target = (run_dir / path).resolve()
    if run_dir not in target.parents or not target.is_file():
        raise HTTPException(404, "not found")
    if download:
        return FileResponse(str(target), filename=f"{run_dir.name}-{target.name}")
    return FileResponse(str(target))


# ---------- background jobs: backtest + autopilot from the UI ----------


@app.post("/api/backtest")
async def api_backtest(body: dict):
    if not caps().get("analyst"):
        return locked("Backtests replay wallet trade history (Analyst plan or higher).", upgrade_url())
    chain = _chain(body.get("chain", config.DEFAULT_CHAIN))
    wallets = [a for a in (body.get("wallets") or []) if ADDR_RE.match(a or "")][:20]
    if not wallets:
        wallets = [f["address"] for f in follow_list() if f.get("chain") == chain][:20]
    if not wallets:
        raise HTTPException(400, "select wallets on the Radar, or follow some, first")
    budget = float(body.get("budget", config.DEFAULT_BUDGET_USD))
    max_pages = max(1, min(int(body.get("max_pages", 3)), 10))
    job_id = secrets.token_hex(4)
    job = {"id": job_id, "kind": "backtest", "status": "running", "chain": chain, "wallets": len(wallets), "started_ts": time.time(), "progress": {}}
    state["jobs"][job_id] = job

    async def work():
        try:
            run_dir, metrics = await backtest_mod.run_live(
                app.state.client, chain, wallets, budget, load_assumptions(), max_pages=max_pages, on_progress=lambda p: job.update(progress=p)
            )
            job.update(status="done", run=run_dir.name, finished_ts=time.time())
        except Exception as e:
            job.update(status="failed", error=type(e).__name__, finished_ts=time.time())

    job["task"] = asyncio.create_task(work())
    return {k: v for k, v in job.items() if k != "task"}


@app.get("/api/jobs")
def api_jobs():
    return [{k: v for k, v in j.items() if k != "task"} for j in sorted(state["jobs"].values(), key=lambda j: -j["started_ts"])]


@app.post("/api/autopilot/start")
async def api_autopilot_start(body: dict):
    if not caps().get("analyst"):
        return locked("Autopilot rescans and profiles wallets (Analyst plan or higher).", upgrade_url())
    pilot = state.get("autopilot")
    if pilot and pilot.get("task") and not pilot["task"].done():
        raise HTTPException(400, "autopilot is already running")
    from . import autopilot

    live: dict = {}
    params = {
        "chain": _chain(body.get("chain", config.DEFAULT_CHAIN)),
        "source": body.get("source", "trending_1h"),
        "budget_usd": float(body.get("budget", config.DEFAULT_BUDGET_USD)),
        "top_k": int(body.get("top_k", config.DEFAULT_TOP_K_FOLLOW)),
        "rescan_hours": float(body.get("rescan_hours", config.DEFAULT_RESCAN_HOURS)),
        "poll_s": max(10.0, float(body.get("poll_s", config.DEFAULT_FOLLOW_POLL_S))),
    }
    task = asyncio.create_task(autopilot.run(**params, live=live))
    state["autopilot"] = {"task": task, "live": live, "params": params, "started_ts": time.time()}
    return {"started": True, **params}


@app.post("/api/autopilot/stop")
async def api_autopilot_stop():
    pilot = state.get("autopilot")
    if pilot and pilot.get("task") and not pilot["task"].done():
        pilot["task"].cancel()
        try:
            await pilot["task"]
        except (asyncio.CancelledError, Exception):
            pass
    run_dir = (pilot or {}).get("live", {}).get("run_dir")
    return {"stopped": True, "run": run_dir.name if run_dir else None}


@app.get("/api/autopilot/status")
def api_autopilot_status():
    pilot = state.get("autopilot")
    if not pilot:
        return {"running": False}
    live = pilot.get("live") or {}
    engine = live.get("engine")
    running = not pilot["task"].done()
    error = None
    if not running and not pilot["task"].cancelled() and pilot["task"].exception():
        error = type(pilot["task"].exception()).__name__
    return {
        "running": running,
        "error": error,
        "params": pilot["params"],
        "started_ts": pilot["started_ts"],
        "run": live["run_dir"].name if live.get("run_dir") else None,
        "following": engine.addresses if engine else [],
        "polls": engine.polls if engine else 0,
        "metrics": engine.portfolio.metrics(engine.last_price) if engine else None,
        "credits_used": live["client"].credits_used if live.get("client") else 0,
    }
