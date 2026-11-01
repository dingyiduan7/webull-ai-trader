# I built an AI trading system. The hard part was finding evidence it deserved to trade.

*Case study, October 2026. Personal research on a real brokerage cash account. Not investment advice.*

## Summary

Over several weeks I built a working system that traded real money: model-generated trade proposals, broker execution, cash reconciliation, risk controls and recovery. The harder question was whether it deserved to trade. I simulated the portfolio rules intended for a $500 account over five years and compared them with simply holding SPY.

| Measure | Result |
|---|---|
| $500 held in SPY, Oct 26, 2021 to Oct 21, 2026 (price only) | **$906** |
| Median of 200 simulations of the rule-based portfolio | **$492** |
| Simulations that ended above SPY | **1 of 200** |
| Simulations that lost money | 107 of 200 |

**Scope.** The simulations test rule-based signals and portfolio construction. They are not a replay of the live language models' decisions.

---

## 1. The system

The trader ran unattended on a real cash brokerage account each market day. Its execution controls were designed to keep model decisions within defined account limits.

- **Two-model decisions.** Two language models from different providers each proposed trades from live quotes. A buy or a sell went through only when both agreed.
- **A ledger reconciled to the broker.** Every order, fill and dollar of cash was recorded in PostgreSQL and reconciled against the broker. When the two disagreed, or a broker read failed, trading paused until fresh readings matched. That strictness protected the account and also cost several trading days. Later versions recovered from read failures automatically and treated the broker's figures as the source of truth.
- **Hard risk limits enforced in code**, independent of the models: per-position loss caps, exposure limits, daily loss stops and settlement-aware cash.
- **Controlled releases.** Code changes installed only at the daily start, as hash-verified bundles, behind roughly 1,160 automated tests.

**Live result, Oct 15–19, 2026:** three round trips, **−$12.67** realized. That figure includes $0.03 in broker fees and excludes model API costs.

| Date | Symbol | Shares | Entry | Exit | P&L |
|---|---|---|---|---|---|
| Oct 15 | CRWV | 3 | $66.34 | $67.12 | +$2.34 |
| Oct 19 | INTC | 2 | $93.89 | $92.58 | −$2.62 |
| Oct 19 | CRWV | 3 | $63.42 | $59.30 | −$12.36 |
| | | | | Fees | −$0.03 |
| | | | | **Total** | **−$12.67** |

Three trades are far too few to judge anything, which is why I built a lab.

## 2. The lab: five years, 362 stocks, 13 rule-based strategies

**Data.** Daily bars from the brokerage API for 362 liquid US stocks and ETFs plus SPY, Oct 26, 2021 to Oct 21, 2026 (1,252 SPY sessions).

**Method.**
- Each strategy decides at a day's close and enters at the next day's open.
- **Costs** scale with liquidity: 0.10% per side above $200M median daily dollar volume, 0.20% for $50–200M, 0.35% below.
- **Stops** are checked first each day; a gap below the stop fills at the open.
- **One open trade** per symbol per strategy.
- **Promotion gates** (stage PAPER):
  - at least 40 trades;
  - at least 15 trades in each half of the history, and profitable in both halves;
  - profit factor at least 1.2 and t-statistic at least 1.5;
  - return per day held above the better of two buy-anything baselines.

| Strategy | Trades | Avg / trade | 1st half | 2nd half | Verdict |
|---|---|---|---|---|---|
| Momentum leaders, hold | 3,437 | +1.75% | +0.05% | +3.46% | passed |
| Relative strength, hold | 6,368 | +0.92% | +0.67% | +1.18% | passed |
| Baseline: buy anything, hold 20 days | 16,279 | +1.14% | +0.11% | +2.18% | benchmark |
| ETF trend, hold | 402 | +0.80% | +0.04% | +1.54% | rejected |
| 20-day channel breakout | 8,428 | +0.75% | +0.38% | +1.13% | rejected |
| Near 50-day high, momentum | 5,903 | +0.72% | +0.28% | +1.16% | rejected |
| 50-day reclaim | 4,392 | +0.10% | +0.50% | −0.31% | rejected |
| Baseline: buy anything, hold 5 days | 40,193 | +0.02% | −0.16% | +0.21% | benchmark |
| Weekly loser reversal | 9,767 | +0.02% | −0.29% | +0.32% | rejected |
| RSI-2 dip buy | 21,782 | −0.07% | −0.27% | +0.13% | rejected |
| IBS reversal | 21,552 | −0.10% | −0.29% | +0.10% | rejected |
| Three down days | 7,347 | −0.23% | −0.36% | −0.10% | rejected |
| Trend pullback to 20-day | 6,065 | −0.26% | −0.40% | −0.12% | rejected |

Per day held, the two passing strategies earned +0.08% and +0.07%, against +0.06% for the buy-anything baseline. Most of their return was the market going up.

## 3. The test that mattered: averages are not portfolios

"+0.92% per trade" averages thousands of trades. A $500 account holds three positions. So I simulated the portfolio rules intended for live use.

**Rules simulated.**
- Up to 3 positions from the two passing strategies: relative strength first, momentum second at half size.
- 2% of equity at risk per position, measured from entry to a 3-ATR stop.
- At most 40% of equity in one stock; entries limited by available cash.
- The lab's exits and liquidity-tiered costs.

I ran it 200 times over the same five years, varying only which of each day's equally ranked signals got picked. Equity is marked at realized exits (positions held at cost while open).

**Distribution of ending balances (200 runs, $500 start):**

| Ending balance | Runs |
|---|---|
| $200–249 | 1 |
| $250–299 | 5 |
| $300–349 | 12 |
| $350–399 | 24 |
| $400–449 | 38 |
| $450–499 | 27 |
| $500–549 | 22 |
| $550–599 | 27 |
| $600–649 | 16 |
| $650–699 | 15 |
| $700–749 | 5 |
| $750–799 | 3 |
| $800–849 | 3 |
| $850–899 | 1 |
| $900–949 | 0 |
| $950–999 | 1 |

Median $492; 10th percentile $354; 90th percentile $672; minimum $236; maximum $973. SPY: $906.

**Other variants tried** (one run each, top-ranked first unless noted): 5 or 10 slots, momentum only, relative strength only, weakest-first, lowest-volatility-first. They ended between $272 and $869. None ended above SPY.

**Sanity check.** Taking every signal at 0.5% of equity, with no slot limit, returned about +30% over five years with most cash idle. The per-trade edge is real on average; it does not survive a three-position account.

### What this does and doesn't test

- These are rule-based signals and portfolio construction, not a replay of the live language models' decisions.
- The 200 runs measure how sensitive one historical period is to signal selection. They are not 200 independent market histories.
- Both sides use daily closes with dividends excluded. API costs are not included.
- **Survivorship bias.** The universe is today's stocks, with no delisted names, which flatters the strategies.
- **Settlement.** The simulation does not model next-day settlement of sale proceeds in a cash account.

## 4. The moonshot: short squeezes

**Data.** Free FINRA data for the same universe:
- Reg SHO daily short volume: 720 sessions from Dec 6, 2023.
- Consolidated short interest: from Nov 15, 2023, 24,955 rows. Each figure is used only 10 calendar days after its settlement date, roughly when FINRA publishes it, to avoid look-ahead.

**Rule.**
- Entry: close at a 20-day high, up at least 3% on at least twice the 20-day average volume. Enter at the next open.
- Exit: a 2-ATR stop, a close below the prior 5-session low, or 15 sessions, whichever comes first.

Every row covers Dec 2023 to Oct 2026.

| Breakout filter | Trades | Avg / trade | Doubled | +50% run | Fell 15%+ |
|---|---|---|---|---|---|
| None (control) | 1,913 | +0.92% | 0.8% | 3.5% | 7.3% |
| Days to cover ≥ 3 | 1,058 | +0.63% | 0.9% | 3.3% | 7.2% |
| Days to cover ≥ 5 | 440 | +0.45% | 1.6% | 3.6% | 9.5% |
| Days to cover ≥ 8 | 152 | +0.27% | 2.0% | 3.9% | 13.2% |
| Days to cover ≥ 10 | 85 | −0.89% | 1.2% | 3.5% | 14.1% |
| Top-decile 20-day short-volume ratio | 161 | −0.20% | 0.0% | 0.6% | 5.0% |

"Doubled" and "+50% run" mean the highest price reached during the hold, relative to entry.

**$500 account, 3 equal slots, 100 simulations:**

| Strategy | Median | 10th percentile | 90th percentile |
|---|---|---|---|
| Days to cover ≥ 5 | $335 | $248 | $484 |
| Plain breakout | $446 | $256 | $1,014 |
| SPY, same period | $881 | | |

More crowding meant more doubles, a worse average and more bleeding: the profile of a lottery ticket. The universe holds liquid large and mid caps, so this says little about small-cap meme squeezes.

## 5. What I learned

1. **Test the economics early, alongside the engineering.** A positive average per trade did not survive position limits, sizing, cash and costs. Simulate the real account before trusting any average, and before building machinery around it.
2. **Pick the benchmark first.** "Profitable" is the wrong bar. The right one is beating a buy-anything baseline and the index after costs, and most ideas fail it.
3. **I haven't demonstrated an edge from model judgment.** Fluent reasoning about a stock is not the same as information the market lacks. The live sample is too small to show anything either way, and a rule-based backtest doesn't settle it. Testing it properly would need many recorded model decisions scored against a benchmark.
4. **Execution controls have a cost.** Reconciliation pauses protected the account and also stopped it from trading on several days. Complexity should be earned by an edge, not built ahead of one.
5. **A clear negative result is a result.** The lab cost an evening of compute instead of real money.

## 6. Next

Live trading is being wound down. The lab stays on as a paper-only research tool.

A new idea goes live only if it beats SPY at full-account level in a backtest, and then again on data it has never seen, in forward paper trading. Searching the same history repeatedly until something wins tends to find overfit strategies, so the forward test is what counts. The first candidate is clustered insider buying from SEC Form 4 filings.

---

**Stack:** Python, PostgreSQL, a brokerage REST API, two LLM APIs, pytest.
**Data:** brokerage daily bars; FINRA Reg SHO daily short volume and consolidated short interest.

### Reproduction

Everything is in `code/`. The scripts use their own copy of the lab modules in `code/lab/` and never touch `src/`.

| File | What it holds |
|---|---|
| `lab/backtest.py` | Fill model, statistics, promotion gates |
| `lab/strategies.py` | Strategy rules |
| `lab/indicators.py` | Indicators |
| `lab/portfolio.py` | v3 portfolio rules and simulator |
| `lab/squeeze.py` | Squeeze variants |
| `run_portfolio_sim.py` | Section 3 results |
| `run_squeeze_sim.py` | Section 4 results |

Run from the repo root:

```
.venv\Scripts\python.exe docs\case-study\code\run_portfolio_sim.py
.venv\Scripts\python.exe docs\case-study\code\run_squeeze_sim.py
```

Inputs are `.local/lab/bars.json` (written by `scripts/strategy_lab.py`) and `.local/lab/short_data.json` (written by `scripts/squeeze_data.py`). Both scripts reproduce the figures in this article exactly.
