"""Radar-specific settings, layered on top of core.config."""
from core import config as core_config

# The chain the app opens on. The picker lists every network from GET /onchain/networks (app/chains.py).
DEFAULT_CHAIN = "solana"

SOURCES = {
    "trending_1h": "Trending (1h)",
    "trending_24h": "Trending (24h)",
    "new_pools": "New pools",
    "safe_movers": "Safe movers",
}

DEFAULT_TOP_N_TOKENS = 10
DEFAULT_WALLETS_PROFILED = 30
DEFAULT_BUDGET_USD = 100
DEFAULT_FOLLOW_POLL_S = 30
DEFAULT_RESCAN_HOURS = 6
DEFAULT_MAX_CREDITS_PER_DAY = 20_000
DEFAULT_TOP_K_FOLLOW = 5

# Safe-movers megafilter preset (PLAN.md §2 / BRIEFS.md).
SAFE_MOVERS_FILTERS = {
    "checks": "no_honeypot,good_gt_score",
    "min_reserve_in_usd": 20_000,
    "min_24h_volume_usd": 20_000,
    "sort": "h24_volume_usd_desc",
    "page": 1,
}

RUNS_DIR = "runs"

# =====================================================================================
# WHALE ACCUMULATION TRACKER (app/accumulation.py) -- edit the lists below freely.
# =====================================================================================
# Each holder's CoinGecko `label` is matched, case-insensitively, against these keyword lists in
# this order: contract, then exchange, then multisig. First match wins.
#   contract / exchange -> EXCLUDED from the whale list (still shown, with the label and the reason)
#   multisig            -> KEPT as a whale and tagged "Multisig"
#   any other label     -> KEPT as a whale, and the label is shown in the table
# Keywords of 3 characters or fewer (AMM, LP, OKX) must match a whole word; longer ones match anywhere
# in the label ("Pool" matches "Liquidity Pool").
WHALE_NETWORKS = ["eth", "base", "bsc"]
WHALE_WINDOW_DAYS = (7, 30)  # the choices offered; the first one is the default
WHALE_DEFAULT_HOLDERS = 50  # the API returns at most 50 holders on non-Solana networks
WHALE_MAX_PAGES = 10  # trade/transfer pages (300 rows each, so ~3,000 rows) fetched per call
WHALE_MAX_CREDITS_PER_WALLET = 20  # hard cap per whale (1 credit per page, trades + transfers together): pages per call = min(WHALE_MAX_PAGES, this / 2)
# A wallet that fills every page it is allowed is "Incomplete data" ("very active wallet, possible bot or market maker")
# and stays out of the totals.
WHALE_MAX_CREDITS_PER_SCAN = 1500  # safety cap for a whole scan: past it no more wallets are started (they are listed as "Not scanned")
WHALE_CONCURRENCY = 6  # wallets analysed at the same time
WHALE_VOLUME_LIQUIDITY_WARN_RATIO = 50  # warn when the token's 24h volume is more than this many times its liquidity
WHALE_LOCKED_INFLOW_MIN_SHARE = 0.5  # a "New position" whose inflow is at least this share Locked/LP (removed from a pool...) is "Holding"

# Wallets excluded by these keywords (or as one of the token's pools) are also the "Locked/LP" side of the
# flow: tokens sent to or received from them (locking in Voting Escrow, adding to an LP, a gauge or a vault)
# are shown as their own part and never count as accumulating or distributing.
WHALE_CONTRACT_KEYWORDS = ["Voting Escrow", "AMM", "LP", "Pool", "Router", "Vault", "Gauge", "Bridge", "Token", "Contract"]
WHALE_EXCHANGE_KEYWORDS = [
    "Hot Wallet", "Cold Wallet", "Exchange",
    "Binance", "Coinbase", "Kraken", "OKX", "Bybit", "KuCoin", "Bitget", "Gate.io", "HTX", "Huobi",
    "Crypto.com", "MEXC", "Bitfinex", "Bitstamp", "Gemini", "Upbit", "Bithumb", "Robinhood",
]
WHALE_MULTISIG_KEYWORDS = ["Gnosis Safe", "Safe Proxy", "Multisig", "Multi-sig"]
WHALE_BURN_ADDRESSES = [
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0xdead000000000000000000000000000000000000",
]

# Stance rules (share of the wallet's balance at the start of the window):
WHALE_NEW_POSITION_MAX_START_PCT = 1.0  # start balance below 1% of the current balance -> "New position"
WHALE_STANCE_THRESHOLD_PCT = 2.0  # net flow above +2% of the start balance -> Accumulating, below -2% -> Distributing

WHALE_EXPLORERS = {"eth": "https://etherscan.io", "base": "https://basescan.org", "bsc": "https://bscscan.com"}  # wallet links in the Whales tab

WHALE_SCANS_DIR = "data/whales"  # one JSON file per scan (kept out of runs/ so the Runs tab is not confused)

REPO_NAME = "whale-accumulation-tracker"
BASE_URL_UTM = f"utm_source=github&utm_content={REPO_NAME}"
