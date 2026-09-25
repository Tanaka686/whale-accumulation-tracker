"""FastAPI app: serves the UI and the Scan / Wallets / Follow / Runs API."""
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.articlekit import build as build_article_kit
from core.assumptions import load as load_assumptions
from core.client import CoinGeckoClient, PlanRestrictedError
from core.plan import locked, probe_capabilities
from core.report import build as build_report

from . import config, profile, runs, scan
from .follow import FollowEngine

ROOT = Path(__file__).resolve().parent.parent
state: dict = {"capabilities": None, "follow": None, "follow_task": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = CoinGeckoClient()
    state["capabilities"] = await probe_capabilities(app.state.client)
    yield
    if state.get("follow_task"):
        state["follow_task"].cancel()
    await app.state.client.close()


app = FastAPI(title="Smart Money Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "web")), name="static")
app.mount("/core-web", StaticFiles(directory=str(ROOT / "core" / "web")), name="core-web")
app.mount("/brand", StaticFiles(directory=str(ROOT / "core" / "brand")), name="brand")


@app.get("/")
def index():
    return FileResponse(str(ROOT / "web" / "index.html"))


@app.get("/api/capabilities")
async def api_capabilities():
    return state["capabilities"] or await probe_capabilities(app.state.client)


@app.get("/api/config")
def api_config():
    return {
        "chains": config.WALLET_CHAINS,
        "sources": config.SOURCES,
        "defaults": {
            "n_tokens": config.DEFAULT_TOP_N_TOKENS,
            "wallets_profiled": config.DEFAULT_WALLETS_PROFILED,
            "budget_usd": config.DEFAULT_BUDGET_USD,
            "poll_s": config.DEFAULT_FOLLOW_POLL_S,
        },
        "assumptions": load_assumptions(),
        "repo_name": config.REPO_NAME,
    }


@app.post("/api/scan")
async def api_scan(body: dict):
    if not state["capabilities"].get("analyst"):
        return locked("Scan needs the top_traders endpoint (Analyst plan or higher).", state["capabilities"]["upgrade_url"])
    chain = body.get("chain", config.WALLET_CHAINS[0])
    source = body.get("source", "trending_1h")
    n_tokens = int(body.get("n_tokens", config.DEFAULT_TOP_N_TOKENS))
    try:
        result = await scan.scan(app.state.client, chain, source, n_tokens)
    except PlanRestrictedError:
        return locked("This source needs a higher plan.", state["capabilities"]["upgrade_url"])
    return result


@app.post("/api/wallets/profile")
async def api_wallets_profile(body: dict):
    if not state["capabilities"].get("analyst"):
        return locked("Wallet profiling needs the wallet endpoints (Analyst plan or higher).", state["capabilities"]["upgrade_url"])
    chain = body.get("chain", config.WALLET_CHAINS[0])
    addresses = body.get("addresses", [])[: config.DEFAULT_WALLETS_PROFILED]
    budget = float(body.get("budget", config.DEFAULT_BUDGET_USD))
    return await profile.profile_candidates(app.state.client, chain, addresses, budget)


@app.get("/api/wallets/{address}/drawer")
async def api_wallet_drawer(address: str, chain: str = config.WALLET_CHAINS[0]):
    if not state["capabilities"].get("analyst"):
        return locked("The wallet drawer needs the wallet endpoints (Analyst plan or higher).", state["capabilities"]["upgrade_url"])
    return await profile.wallet_drawer(app.state.client, chain, address)


@app.post("/api/follow/start")
async def api_follow_start(body: dict):
    if not state["capabilities"].get("analyst"):
        return locked("Following wallets needs the wallet endpoints (Analyst plan or higher).", state["capabilities"]["upgrade_url"])
    if state.get("follow_task") and not state["follow_task"].done():
        raise HTTPException(400, "already following; call /api/follow/stop first")
    chain = body.get("chain", config.WALLET_CHAINS[0])
    addresses = body.get("addresses", [])
    budget = float(body.get("budget", config.DEFAULT_BUDGET_USD))
    poll_s = float(body.get("poll_s", config.DEFAULT_FOLLOW_POLL_S))
    assumptions = load_assumptions()
    engine = FollowEngine(app.state.client, chain, addresses, budget, assumptions, poll_s)
    state["follow"] = engine
    state["run_dir"] = runs.new_run_dir("forward")

    async def loop():
        while True:
            new = await engine.poll_once()
            for d in new:
                runs.append_decision(state["run_dir"], d)
            runs.write_metrics(state["run_dir"], {"mode": "forward", "chain": chain, "addresses": addresses, "credits_used": app.state.client.credits_used, **engine.status()})
            await asyncio.sleep(engine.poll_s)

    state["follow_task"] = asyncio.create_task(loop())
    return {"started": True, "run": state["run_dir"].name}


@app.post("/api/follow/stop")
async def api_follow_stop():
    if state.get("follow_task"):
        state["follow_task"].cancel()
        state["follow_task"] = None
    if state.get("follow") and state.get("run_dir"):
        engine = state["follow"]
        runs.write_equity(state["run_dir"], engine.portfolio.equity_curve)
        runs.write_trades_csv(state["run_dir"], engine.portfolio.closed)
    return {"stopped": True}


@app.get("/api/follow/status")
def api_follow_status():
    if not state.get("follow"):
        return {"following": False}
    return {"following": True, **state["follow"].status()}


@app.get("/api/runs")
def api_runs():
    return runs.list_runs()


@app.post("/api/runs/{run_id}/report")
def api_run_report(run_id: str):
    run_id = runs.resolve_run_id(run_id)
    run_dir = Path(config.RUNS_DIR) / run_id
    metrics = runs.read_metrics(run_dir)
    if not metrics:
        raise HTTPException(404, "no metrics for this run")
    equity = runs.read_equity(run_dir)
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    paths = build_report(run_id, scenario_metrics, equity, credits_used=metrics.get("credits_used", 0), out_dir=run_dir.parent)
    return paths


@app.post("/api/runs/{run_id}/article-kit")
def api_run_article_kit(run_id: str, body: dict | None = None):
    body = body or {}
    run_id = runs.resolve_run_id(run_id)
    run_dir = Path(config.RUNS_DIR) / run_id
    metrics = runs.read_metrics(run_dir)
    if not metrics:
        raise HTTPException(404, "no metrics for this run")
    equity = runs.read_equity(run_dir)
    scenario_metrics = metrics.get("blind") or metrics.get("metrics") or metrics
    paths = build_article_kit(
        title=f"Smart Money Radar: {run_id}",
        handle=body.get("handle", "yourhandle"),
        metrics=scenario_metrics,
        equity_curve=equity,
        credits_used=metrics.get("credits_used", 0),
        out_dir=run_dir / "article-kit",
    )
    return paths
