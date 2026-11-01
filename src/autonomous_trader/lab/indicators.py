"""Plain-Python indicators over a list of bars (dicts with open/high/low/close/volume), causal only:
every value at index i uses bars[0..i] and nothing later."""


def sma(values, n, i):
    if i + 1 < n:
        return None
    return sum(values[i + 1 - n:i + 1]) / n


def rsi(closes, n, i):
    """Wilder-free simple RSI over the last n changes (Connors-style short RSI)."""
    if i < n:
        return None
    gains = losses = 0.0
    for k in range(i + 1 - n, i + 1):
        d = closes[k] - closes[k - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if gains + losses == 0:
        return 50.0
    return 100.0 * gains / (gains + losses)


def atr(bars, n, i):
    if i < n:
        return None
    total = 0.0
    for k in range(i + 1 - n, i + 1):
        h, l, pc = bars[k]['high'], bars[k]['low'], bars[k - 1]['close']
        total += max(h - l, abs(h - pc), abs(l - pc))
    return total / n


def highest(values, n, i):
    if i + 1 < n:
        return None
    return max(values[i + 1 - n:i + 1])


def lowest(values, n, i):
    if i + 1 < n:
        return None
    return min(values[i + 1 - n:i + 1])


def ibs(bar):
    rng = bar['high'] - bar['low']
    return 0.5 if rng <= 0 else (bar['close'] - bar['low']) / rng
