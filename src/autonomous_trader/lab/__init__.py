"""Strategy lab (owner request 2026-10-19): rule-based strategies tested on history before money touches them.

Research only: no orders, no broker writes, no execution authority. The lab produces evidence (backtest
statistics and today's signals) that the decision models read; promotion to live size is staged and gated by
results. Pure Python on daily bars, so it runs on the owner's PC without extra packages."""
