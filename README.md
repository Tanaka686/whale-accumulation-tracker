<picture>
  <source media="(prefers-color-scheme: dark)" srcset="core/brand/coingecko-api-on-dark.svg">
  <img src="core/brand/coingecko-api-on-light.svg" alt="Data powered by CoinGecko API" height="32">
</picture>

# Smart Money Radar

Scan today's hottest tokens, find the wallets trading them, score every wallet on real PnL and
copyability, then paper copy-trade the best ones -- on autopilot if you want. It's the CoinGecko
API wallet endpoints doing what a screener can't: your own rules, running on real trade history,
24/7.

![Demo](docs/demo.gif)

## Get your API key

Grab a free key at [coingecko.com/en/api](https://www.coingecko.com/en/api?utm_source=github&utm_content=smart-money-radar). The
Scan and Wallets tabs need an **Analyst** plan or higher (that's where `top_traders`, `top_holders`
and the wallet endpoints live); trending/new pools work on the free Demo plan, and the app shows a
locked card instead of hanging when a feature isn't on your plan.

## What you need

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- A CoinGecko API key ([get one](https://www.coingecko.com/en/api?utm_source=github&utm_content=smart-money-radar))
- An AI coding agent (Claude Code or Codex) if you want to reskin or extend this

## Important data and chain notes

### CoinGecko data vs. repo-derived outputs

CoinGecko API supplies the underlying market, token, pool, trade and wallet data used by this
project. Wallet labels, skill and copyability scores, bot-like classifications, shortlists,
signals, paper-trading decisions and reports are computed by this repository. They are editable
examples, not CoinGecko API fields, official CoinGecko classifications, financial advice or
validated trading signals. Inspect the supporting data and adapt the formulas before relying on
them in your own workflow.

### Recommended chains for end-to-end testing

For demos that combine discovery with the complete wallet workflow, start with **Ethereum, Base,
BNB Chain, Robinhood Chain or Arc Chain**. Solana still offers useful market, token, pool and trade
data together with wallet P&L and wallet-trade history, while its wallet balance and transfer
coverage is currently more limited. Use one of the recommended chains when your build depends on
those additional wallet views.

This note is implementation context for you and your coding agent; chain-coverage gaps do not need
to become the topic of creator-facing content.

## Quickstart

```
make install
cp env.example .env   # then paste your key into .env
make run               # http://localhost:8000
```

## Make it yours with your AI agent

Paste any of these into Claude Code or Codex, from inside this repo:

1. "Change the Scan tab's default source from trending_1h to new_pools, and change the default budget to $250."
2. "Restyle this to a purple/black theme. Keep the CoinGecko badge and the links block intact."
3. "Add a filter to the Wallets tab for wallets seen in 3+ tokens this scan."
4. "Make Base the default chain and explain how `WALLET_CHAIN_CAPS` handles endpoint differences."
5. "Change the copyability formula in `app/scoring.py` to weight trade frequency more heavily."
6. "Add a Telegram or Discord webhook that posts every autopilot decision."

See `AGENTS.md` for the full repo map and customization recipes.

## How it works

```
Discovery                Real-time-ish              Your logic              Execution
trending/new pools   →   poll followed wallets   →   scoring + rules   →   paper trades
top_traders/holders      every N seconds             (app/scoring.py,        (core/paper.py)
wallet pnl/trades                                     app/backtest.py)
```

| Layer | Endpoints used |
|---|---|
| Discovery | trending pools, new pools, megafilter, `top_traders`, `top_holders` |
| Wallet profiling | `wallets/{address}/pnl`, `networks/{network}/wallets/{address}/trades`, `wallets/{address}/balances` |
| Decision logic | your rules in `app/scoring.py` + `app/backtest.py` |
| Execution | paper trading only (`core/paper.py`) |

### The four modes

```
make backtest SOURCE=trending_1h CHAIN=base     # walk-forward: select on the first 60%, replay the last 40%
make forward MINUTES=3                          # live on paper, logs every decision
make autopilot                                  # rescans + re-scores + trades on paper, forever
make report RUN=latest                          # report.html + report-card.png for any run
make article-kit RUN=latest HANDLE=you           # article-kit/ ready for an X Article
```

## What you can do on each plan

| Feature | Demo (free) | Analyst+ |
|---|---|---|
| Trending / new pools | ✅ | ✅ |
| Safe-movers megafilter | 🔒 [upgrade](https://www.coingecko.com/en/api/pricing?utm_source=github&utm_content=smart-money-radar) | ✅ |
| `top_traders` / `top_holders` (Scan) | 🔒 [upgrade](https://www.coingecko.com/en/api/pricing?utm_source=github&utm_content=smart-money-radar) | ✅ |
| Wallet PnL / trades / balances (Wallets, Follow) | 🔒 [upgrade](https://www.coingecko.com/en/api/pricing?utm_source=github&utm_content=smart-money-radar) | ✅ |

Demo software. Paper trading only. CoinGecko API provides market data; it doesn't execute trades or
give financial advice.

<!-- coingecko-links:start -->
- CoinGecko API: https://www.coingecko.com/en/api?utm_source=github&utm_content=smart-money-radar
- Pricing: https://www.coingecko.com/en/api/pricing?utm_source=github&utm_content=smart-money-radar
- Docs: https://docs.coingecko.com?utm_source=github&utm_content=smart-money-radar
- Agent Skill + MCP: https://docs.coingecko.com/ai-integration?utm_source=github&utm_content=smart-money-radar
<!-- coingecko-links:end -->
