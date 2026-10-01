"""The static demo folder: sanitised scan, links, footnote, and nothing that looks like a key or a local path."""
import json
import re
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "demo"
SENSITIVE = re.compile(r"[A-Za-z]:\|/Users/|\.env\b|api[_-]?key|x-cg-|CG-[A-Za-z0-9]{8,}", re.I)


def test_the_saved_scan_is_virtual_30d_and_has_no_local_paths_or_keys():
    text = (DEMO / "scan.json").read_text(encoding="utf-8")
    d = json.loads(text)
    assert d["symbol"] == "VIRTUAL" and d["days"] == 30 and d["whales"]
    assert "saved_to" not in d and "elapsed_ms" not in d
    assert not SENSITIVE.search(text)


def test_the_page_has_banner_links_logo_and_footnote():
    html = (DEMO / "index.html").read_text(encoding="utf-8")
    assert "https://github.com/Tanaka686/whale-accumulation-tracker" in html
    assert "https://www.coingecko.com/en/api?utm_source=vercel&utm_content=tanaka_l2" in html
    assert "Data powered by CoinGecko API" in html and (DEMO / "brand" / "coingecko-api-on-dark.svg").exists()
    assert "Stance labels, reasons and the flow breakdown are computed by this tool" in html


def test_the_demo_is_read_only_and_talks_to_no_server():
    js = (DEMO / "demo.js").read_text(encoding="utf-8")
    assert "/api/" not in js and "localStorage" not in js and "POST" not in js
    assert 'fetch("scan.json")' in js
