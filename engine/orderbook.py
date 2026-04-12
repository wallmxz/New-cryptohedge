from __future__ import annotations


def calc_maker_price(*, side: str, best_bid: float, best_ask: float, tick: float) -> float:
    spread = best_ask - best_bid
    if side == "sell":
        if spread > tick:
            price = best_ask - tick
        else:
            price = best_ask
        if price <= best_bid:
            price = best_bid + tick
        return round(price, 10)
    else:
        if spread > tick:
            price = best_bid + tick
        else:
            price = best_bid
        if price >= best_ask:
            price = best_ask - tick
        return round(price, 10)


def calc_aggressive_price(*, side: str, best_bid: float, best_ask: float, tick: float) -> float:
    if side == "sell":
        return round(best_bid + tick, 10)
    else:
        return round(best_ask - tick, 10)


def check_order_depth(*, side: str, price: float, book_levels: dict[float, float], max_depth: int = 3) -> str:
    if side == "sell":
        sorted_levels = sorted(book_levels.keys())
    else:
        sorted_levels = sorted(book_levels.keys(), reverse=True)

    if price not in book_levels:
        return "REPOST"

    level_index = sorted_levels.index(price)
    if level_index >= max_depth - 1:
        return "REPOST"
    return "HOLD"
