# Webull AI Trader

An autonomous stock-trading system that ran unattended on a real $500 Webull cash account (October 2026), plus the research lab used to test whether it had an edge.

**Result:** it did not. Over five years of data, no rule tested (34 variants across 19 ideas) beat simply holding SPY at this account size, and live trading lost $19.26. Live trading has been shut down. Full write-up: [docs/case-study/article.md](docs/case-study/article.md).

## What's here

| Path | Contents |
|---|---|
| `src/autonomous_trader/` | The trader: two-model decisions (both must agree), a PostgreSQL ledger reconciled against the broker, code-enforced risk limits, incident locks and recovery |
| `src/autonomous_trader/lab/` | Strategy lab: daily-bar backtester with liquidity-tiered costs, gap-aware stops, two-half stability checks and promotion gates |
| `scripts/` | Daily launcher, supervisor, strategy lab, data downloads (FINRA short data, SEC insider and earnings filings), forward paper tracker |
| `research/` | Edge-search harness with a locked holdout year, event studies and portfolio-level simulations vs SPY |
| `docs/case-study/` | The case study, with the code that reproduces every number in it |
| `tests/` | About 1,750 automated tests |

## Credentials

None are included. The trader reads Webull and model API keys from Windows-protected local files (`.secrets/`, `.local/`), which are excluded from this repository.

Not investment advice.
