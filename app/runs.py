"""Every backtest / forward / autopilot run writes decisions.jsonl, trades.csv and metrics.json under runs/<id>/.

This module is the one place that knows that layout, so the CLI, the server and `make report` agree on it.
"""
import csv
import json
import time
from pathlib import Path

from . import config


def new_run_dir(mode: str, base: str | Path = config.RUNS_DIR) -> Path:
    """Creates and returns runs/<timestamp>-<mode>/."""
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    path = Path(base) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_metrics(run_dir: str | Path, metrics: dict):
    (Path(run_dir) / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))


def read_metrics(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "metrics.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_decisions(run_dir: str | Path, decisions: list[dict]):
    path = Path(run_dir) / "decisions.jsonl"
    with path.open("w") as fh:
        for d in decisions:
            fh.write(json.dumps(d, default=str) + "\n")


def append_decision(run_dir: str | Path, decision: dict):
    path = Path(run_dir) / "decisions.jsonl"
    with path.open("a") as fh:
        fh.write(json.dumps(decision, default=str) + "\n")


def write_trades_csv(run_dir: str | Path, closed_trades: list):
    """closed_trades: core.paper.ClosedTrade instances or equivalent dicts."""
    path = Path(run_dir) / "trades.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "qty", "buy_usd", "sell_usd", "pnl_usd", "opened_ts", "closed_ts"])
        for t in closed_trades:
            row = t.__dict__ if hasattr(t, "__dict__") else t
            writer.writerow([row["symbol"], row["qty"], row["buy_usd"], row["sell_usd"], row["pnl_usd"], row["opened_ts"], row["closed_ts"]])


def write_equity(run_dir: str | Path, equity_curve: list[tuple[float, float]]):
    (Path(run_dir) / "equity.json").write_text(json.dumps(equity_curve))


def read_equity(run_dir: str | Path) -> list[tuple[float, float]]:
    path = Path(run_dir) / "equity.json"
    return json.loads(path.read_text()) if path.exists() else []


def list_runs(base: str | Path = config.RUNS_DIR) -> list[dict]:
    """Every run directory, newest first, with its metrics and whether a report/article-kit already exists."""
    root = Path(base)
    if not root.exists():
        return []
    out = []
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        out.append(
            {
                "id": path.name,
                "mode": path.name.rsplit("-", 1)[-1],
                "metrics": read_metrics(path),
                "has_report": (path / "report.html").exists(),
                "has_article_kit": (path / "article-kit").exists(),
            }
        )
    return out


def resolve_run_id(run_id: str, base: str | Path = config.RUNS_DIR) -> str:
    """Resolves 'latest' to the newest run directory name; otherwise returns run_id unchanged."""
    if run_id != "latest":
        return run_id
    runs = list_runs(base)
    if not runs:
        raise FileNotFoundError("no runs yet")
    return runs[0]["id"]
