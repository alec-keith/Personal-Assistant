"""
Stock quotes via yfinance (Yahoo Finance) — no API key required.
Returns current price, day change, and basic info for one or more symbols.
"""

import logging

logger = logging.getLogger(__name__)


async def get_stock_quotes(symbols: list[str]) -> str:
    """
    Fetch current price + day change for a list of ticker symbols.
    Runs yfinance in a thread pool to avoid blocking the event loop.
    """
    try:
        import asyncio
        import functools
        return await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(_fetch_quotes, symbols)
        )
    except ImportError:
        return "Stock quotes not available — yfinance not installed."
    except Exception as e:
        logger.exception("Stock quote fetch failed")
        return f"Error fetching stock data: {e}"


def _fetch_quotes(symbols: list[str]) -> str:
    import yfinance as yf

    lines = []
    for symbol in symbols:
        try:
            ticker = yf.Ticker(symbol.upper())
            info = ticker.fast_info
            price = info.last_price
            prev_close = info.previous_close
            if price is None:
                lines.append(f"{symbol.upper()}: no data")
                continue
            change = price - prev_close if prev_close else 0
            pct = (change / prev_close * 100) if prev_close else 0
            sign = "+" if change >= 0 else ""
            lines.append(
                f"{symbol.upper()}: ${price:.2f}  {sign}{change:.2f} ({sign}{pct:.2f}%)"
            )
        except Exception as e:
            lines.append(f"{symbol.upper()}: error — {e}")

    return "\n".join(lines) if lines else "No data returned."
