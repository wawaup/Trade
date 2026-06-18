DEFAULT_TAKER_FEE_RATE = 0.001


def coerce_fee_rate(value, default=DEFAULT_TAKER_FEE_RATE):
    try:
        fee = float(value if value is not None else default)
    except (TypeError, ValueError):
        fee = float(default)
    if fee < 0:
        return 0.0
    if fee > 0.05:
        fee = fee / 100.0
    return min(fee, 0.05)


def trailing_exit_locks_net_profit(side, *, entry_price, exit_price, fee_rate, extra_buffer=0.0):
    try:
        entry = float(entry_price or 0.0)
        exit_px = float(exit_price or 0.0)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or exit_px <= 0:
        return False

    fee = coerce_fee_rate(fee_rate, default=0.0)
    try:
        extra = max(0.0, float(extra_buffer or 0.0))
    except (TypeError, ValueError):
        extra = 0.0
    min_move = max(0.0, 2.0 * fee + extra)

    if side == "long":
        return exit_px >= entry * (1.0 + min_move)
    if side == "short":
        return exit_px <= entry * (1.0 - min_move)
    return False
