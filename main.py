"""
TradingView -> Alpaca Webhook Bot — v2 (May 2026)
═══════════════════════════════════════════════════════════════════════════
LONG-ONLY MODE  |  Based on 4.5 month backtest analysis
─────────────────────────────────────────────────────────────────────────
Buy signal  → open $2000 LONG if flat, skip if already long
Sell signal → close existing long if any, then go FLAT (no shorts)
              skip entirely if no position

v2 ADDITIONS (May 2026):
  TIER 1:
    - Pinned alpaca-py >=0.30
    - Telegram notifications on every order + system events
  TIER 2 (stop loss):
    - Trailing stop on every long position
    - Stop = max(price - 3%, price - 2×ATR)  → uses WIDER for breathing room
    - ATR computed from 14-period 1H bars
    - Background check every 60 seconds during market hours
  TIER 3 (risk safeguards):
    - Hard close after 3 trading days regardless of signal
    - Auto-disable at -5% daily drawdown (positions stay; new buys blocked)
    - Daily equity snapshot at market open
"""

import os
import logging
import json
import time
import threading
import traceback
from datetime import datetime, timezone, date, timedelta
from collections import defaultdict
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockLatestTradeRequest, CryptoLatestTradeRequest,
    StockBarsRequest, CryptoBarsRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import requests as http_requests

# Trade journal
from trade_journal import TradeJournal

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("webhook.log"),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Config — env vars
# ──────────────────────────────────────────────
ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER              = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "")
IS_PAPER           = "paper" in PAPER

# Telegram (optional — bot still works without these)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED   = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ──────────────────────────────────────────────
# Risk settings
# ──────────────────────────────────────────────
DEFAULT_NOTIONAL     = 2000
TIER_A_NOTIONAL      = 2000
TIER_B_NOTIONAL      = 1000
MAX_NOTIONAL         = 5000
PDT_WARN_LIMIT       = 3

# TIER 2 — Stop loss settings
STOP_LOSS_FIXED_PCT  = 0.03      # 3% below entry/peak
STOP_LOSS_ATR_MULT   = 2.0       # 2x ATR below entry/peak
ATR_PERIOD           = 14
ATR_TIMEFRAME_MIN    = 60        # 1H bars for ATR
ATR_LOOKBACK_DAYS    = 10        # how many days of bars to fetch for ATR calc
TRAILING_CHECK_SEC   = 60        # how often to check trailing stops

# TIER 3 — Max hold + drawdown
MAX_HOLD_DAYS        = 3         # close any position after N trading days
DAILY_DRAWDOWN_LIMIT = 0.05      # 5% — auto-disable bot at this loss

# EXTENDED HOURS — pre-market + after-hours trading
ENABLE_EXTENDED_HOURS = True     # set False to revert to regular-hours-only
EXT_LIMIT_BUFFER_PCT  = 0.01     # 1% limit-price buffer for extended-hours fills

# Where we persist the bot's trading-state snapshot (Railway ephemeral, OK)
STATE_FILE           = Path("bot_state.json")

if not all([ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_PASSPHRASE]):
    raise EnvironmentError(
        "Missing required env vars: ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_PASSPHRASE"
    )

# ──────────────────────────────────────────────
# Crypto symbol map
# ──────────────────────────────────────────────
CRYPTO_MAP = {
    "BTCUSD":  "BTC/USD", "ETHUSD":  "ETH/USD", "SOLUSD":  "SOL/USD",
    "DOGEUSD": "DOGE/USD","XRPUSD":  "XRP/USD", "LTCUSD":  "LTC/USD",
    "AVAXUSD": "AVAX/USD","LINKUSD": "LINK/USD","UNIUSD":  "UNI/USD",
    "AAVEUSD": "AAVE/USD",
}

# ──────────────────────────────────────────────
# Alpaca clients
# ──────────────────────────────────────────────
client             = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=IS_PAPER)
stock_data_client  = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
crypto_data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# Trade journal — universal schema. On Railway the file is ephemeral but
# still useful between restarts. For persistent storage, enable a Railway volume.
try:
    _account_id = client.get_account().account_number
except Exception:
    _account_id = "unknown"
journal = TradeJournal(
    bot_name="tradingview",
    account_id=_account_id,
    journal_path=Path("trade_journal.json"),
)

# ──────────────────────────────────────────────
# In-memory bot state (best-effort persistence to STATE_FILE)
# Structure:
#   {
#     "trailing_stops": {symbol: {"peak_price": float, "stop_price": float, "atr": float}},
#     "drawdown": {"date": "2026-05-20", "starting_equity": 100000.0, "halted": False},
#     "day_trade_log": {"2026-05-20": 3, ...},
#   }
# ──────────────────────────────────────────────
_state_lock = threading.Lock()
_state: dict = {
    "trailing_stops": {},
    "drawdown": {"date": None, "starting_equity": None, "halted": False, "halt_reason": None},
    "day_trade_log": {},
}


def _load_state():
    global _state
    if not STATE_FILE.exists():
        return
    try:
        with _state_lock:
            _state = json.loads(STATE_FILE.read_text())
        log.info(f"Loaded state from {STATE_FILE}")
    except Exception as e:
        log.warning(f"Could not load {STATE_FILE}: {e}. Starting with fresh state.")


def _save_state():
    try:
        with _state_lock:
            STATE_FILE.write_text(json.dumps(_state, indent=2, default=str))
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────
def send_telegram(text: str, parse_mode: str = "HTML"):
    if not TELEGRAM_ENABLED:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        http_requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
        }, timeout=5)
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ──────────────────────────────────────────────
# PDT tracker
# ──────────────────────────────────────────────
def record_day_trade():
    today = str(date.today())
    with _state_lock:
        _state["day_trade_log"][today] = _state["day_trade_log"].get(today, 0) + 1
        # Trim old entries (>7 days)
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        _state["day_trade_log"] = {
            d: c for d, c in _state["day_trade_log"].items() if d >= cutoff
        }
        total_week = sum(_state["day_trade_log"].values())
        today_count = _state["day_trade_log"][today]
    log.info(f"Day trade recorded. Today: {today_count} | This week: {total_week}")
    _save_state()
    if total_week >= PDT_WARN_LIMIT:
        log.warning(
            f"PDT INFO: {total_week} day trades this week. Limit only applies to accounts "
            f"under $25,000. Your paper account is well above this threshold."
        )


# ──────────────────────────────────────────────
# Price + ATR helpers
# ──────────────────────────────────────────────
def get_current_price(symbol: str, crypto: bool) -> float:
    try:
        if crypto:
            req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
            trades = crypto_data_client.get_crypto_latest_trade(req)
        else:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trades = stock_data_client.get_stock_latest_trade(req)
        for _, trade in trades.items():
            return float(trade.price)
    except Exception as e:
        log.error(f"Failed to fetch current price for {symbol}: {e}")
        raise


def compute_atr(symbol: str, crypto: bool) -> Optional[float]:
    """Compute 14-period ATR on 1H bars. Returns None if unable."""
    try:
        end = datetime.now(timezone.utc) - timedelta(minutes=2)
        start = end - timedelta(days=ATR_LOOKBACK_DAYS)
        tf = TimeFrame(ATR_TIMEFRAME_MIN, TimeFrameUnit.Minute)
        if crypto:
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            bars = crypto_data_client.get_crypto_bars(req).df
        else:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end)
            bars = stock_data_client.get_stock_bars(req).df

        if bars is None or bars.empty or len(bars) < ATR_PERIOD + 1:
            return None
        bars = bars.reset_index()

        # Wilder ATR
        high  = bars["high"]
        low   = bars["low"]
        close = bars["close"]
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = tr1.combine(tr2, max).combine(tr3, max)
        atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
        latest_atr = float(atr.iloc[-1])
        return latest_atr if latest_atr > 0 else None
    except Exception as e:
        log.warning(f"ATR compute failed for {symbol}: {e}")
        return None


def compute_stop_price(symbol: str, entry_or_peak: float, crypto: bool) -> float:
    """Stop = max(entry_or_peak - 3%, entry_or_peak - 2*ATR). WIDER wins."""
    fixed_stop = entry_or_peak * (1 - STOP_LOSS_FIXED_PCT)
    atr = compute_atr(symbol, crypto)
    if atr is None:
        log.info(f"{symbol}: ATR unavailable, using fixed 3% stop")
        return fixed_stop
    atr_stop = entry_or_peak - atr * STOP_LOSS_ATR_MULT
    chosen = min(fixed_stop, atr_stop)  # min = lower = WIDER stop
    log.info(
        f"{symbol}: stop calc | peak=${entry_or_peak:.2f} "
        f"| fixed_stop=${fixed_stop:.2f} | atr_stop=${atr_stop:.2f} "
        f"| chosen=${chosen:.2f} ({'ATR' if atr_stop < fixed_stop else 'fixed'} wider)"
    )
    return chosen


# ──────────────────────────────────────────────
# Position / account helpers
# ──────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().strip()
    return CRYPTO_MAP.get(s, s)


def is_crypto(symbol: str) -> bool:
    return "/" in symbol


def get_position(symbol: str):
    """Try multiple formats to find a position — handles BTCUSD vs BTC/USD quirks."""
    candidates = {symbol, symbol.replace("/", ""), symbol.replace("/", "%2F")}
    for candidate in candidates:
        try:
            pos = client.get_open_position(candidate)
            if pos:
                return pos
        except Exception as e:
            err = str(e).lower()
            if "position does not exist" in err or "not found" in err or "404" in err:
                continue
            raise
    try:
        all_positions = client.get_all_positions()
        symbol_clean = symbol.replace("/", "").upper()
        for pos in all_positions:
            if pos.symbol.replace("/", "").upper() == symbol_clean:
                return pos
    except Exception as e:
        log.error(f"Error scanning all positions: {e}")
    return None


def get_account():
    return client.get_account()


def is_market_open() -> bool:
    return client.get_clock().is_open


def get_market_session(crypto: bool) -> str:
    """
    Classify the current trading session for ENTRY decisions.
      "crypto"   — 24/7, always tradeable
      "regular"  — US regular hours (market clock says open)
      "extended" — US pre-market or after-hours window
      "closed"   — overnight or weekend, no trading possible

    Uses a generous UTC window that covers both EST and EDT:
      Pre-market  ~ 08:00–13:30 UTC
      After-hours ~ 20:00–01:00 UTC
    Weekends (Sat/Sun UTC) are always "closed".
    """
    if crypto:
        return "crypto"
    try:
        if is_market_open():
            return "regular"
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() >= 5:  # Sat/Sun
            return "closed"
        hour = now_utc.hour + now_utc.minute / 60.0
        in_premarket  = 8.0 <= hour < 14.0
        in_afterhours = 20.0 <= hour <= 23.999 or 0.0 <= hour < 1.0
        if in_premarket or in_afterhours:
            return "extended"
        return "closed"
    except Exception as e:
        log.error(f"Error determining market session: {e}")
        return "closed"


def cancel_open_orders(symbol: str):
    symbol_clean = symbol.replace("/", "").upper()
    orders = client.get_orders()
    for order in orders:
        if order.symbol.replace("/", "").upper() == symbol_clean:
            client.cancel_order_by_id(str(order.id))
            log.info(f"Cancelled open order {order.id} for {symbol}")


# ──────────────────────────────────────────────
# Drawdown tracking
# ──────────────────────────────────────────────
def _maybe_snapshot_starting_equity():
    """Snapshot today's starting equity if not already done. Resets halt flag at market open."""
    today = str(date.today())
    with _state_lock:
        dd = _state["drawdown"]
        if dd.get("date") == today and dd.get("starting_equity") is not None:
            return  # already snapshotted
        try:
            account = get_account()
            eq = float(account.equity)
        except Exception as e:
            log.warning(f"Equity snapshot failed: {e}")
            return
        _state["drawdown"] = {
            "date": today,
            "starting_equity": eq,
            "halted": False,
            "halt_reason": None,
        }
    log.info(f"Starting equity snapshotted: ${eq:,.2f}")
    send_telegram(
        f"🌅 <b>TV Bot — New trading day</b>\n"
        f"Starting equity: ${eq:,.2f}\n"
        f"Drawdown limit: -${eq * DAILY_DRAWDOWN_LIMIT:,.2f} (-{DAILY_DRAWDOWN_LIMIT*100:.0f}%)"
    )
    _save_state()


def _check_drawdown_halt():
    """Check if today's drawdown exceeds the limit. Sets halt flag."""
    with _state_lock:
        dd = _state["drawdown"]
        if dd.get("halted"):
            return True
        starting = dd.get("starting_equity")
    if not starting:
        return False
    try:
        eq = float(get_account().equity)
    except Exception:
        return False
    pnl_pct = (eq - starting) / starting
    if pnl_pct <= -DAILY_DRAWDOWN_LIMIT:
        with _state_lock:
            _state["drawdown"]["halted"] = True
            _state["drawdown"]["halt_reason"] = f"PnL {pnl_pct*100:.2f}% <= -{DAILY_DRAWDOWN_LIMIT*100:.0f}%"
        log.warning(
            f"🛑 DRAWDOWN HALT: equity ${eq:,.2f} vs start ${starting:,.2f} "
            f"= {pnl_pct*100:+.2f}%. New buys disabled."
        )
        send_telegram(
            f"🛑 <b>TV Bot — Drawdown halt</b>\n"
            f"Equity: ${eq:,.2f}\n"
            f"Starting: ${starting:,.2f}\n"
            f"PnL: {pnl_pct*100:+.2f}%\n"
            f"New buys disabled until tomorrow."
        )
        _save_state()
        return True
    return False


# ──────────────────────────────────────────────
# Order submission helper
# ──────────────────────────────────────────────
def submit(symbol: str, side: OrderSide, qty: float = None,
           notional: float = None, crypto: bool = False, session: str = "regular"):
    """
    Build and submit an order.

    Regular hours / crypto -> MARKET order (notional or qty)
    Extended hours stocks  -> LIMIT order with extended_hours=True and a
                              1% price buffer. Alpaca requires limit orders
                              for pre/post-market, and limit orders need an
                              integer share qty (no notional, no fractions).
    """
    if session == "extended" and not crypto:
        current_price = get_current_price(symbol, crypto=False)
        buffer = current_price * EXT_LIMIT_BUFFER_PCT
        if side == OrderSide.BUY:
            limit_price = round(current_price + buffer, 2)
            if notional and not qty:
                qty = int(notional / current_price)
        else:
            limit_price = round(current_price - buffer, 2)
            if qty:
                qty = int(qty)  # extended-hours limit needs whole shares

        if not qty or qty < 1:
            raise ValueError(
                f"Extended-hours order for {symbol} needs >=1 whole share "
                f"(price ${current_price:.2f}). Skipping."
            )

        order_data = LimitOrderRequest(
            symbol=symbol, qty=qty, side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price, extended_hours=True,
        )
        order = client.submit_order(order_data)
        log.info(
            f"Order submitted | id={order.id} | {side} {qty}x {symbol} "
            f"@ LIMIT ${limit_price} (ext-hours) | status={order.status}"
        )
        return order

    # Regular hours or crypto -> market order
    tif = TimeInForce.GTC if crypto else TimeInForce.DAY
    if notional:
        order_data = MarketOrderRequest(
            symbol=symbol, notional=round(notional, 2),
            side=side, time_in_force=tif,
        )
    else:
        order_data = MarketOrderRequest(
            symbol=symbol, qty=qty,
            side=side, time_in_force=tif,
        )
    order = client.submit_order(order_data)
    log.info(
        f"Order submitted | id={order.id} | {side} "
        f"{'$' + str(notional) if notional else str(qty) + 'x'} {symbol} | status={order.status}"
    )
    return order


def wait_for_fill(order_id: str, max_wait_seconds: int = 8) -> bool:
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        try:
            order = client.get_order_by_id(order_id)
            status = str(order.status).lower()
            if "filled" in status and "partial" not in status:
                return True
            if any(bad in status for bad in ("rejected", "canceled", "expired")):
                return False
        except Exception as e:
            log.warning(f"Error polling order {order_id}: {e}")
        time.sleep(0.4)
    return False


# ──────────────────────────────────────────────
# Core order logic — LONG ONLY
# ──────────────────────────────────────────────
def place_order(symbol: str, side: str, notional: float = None, qty: float = None):
    symbol = normalize_symbol(symbol)
    side   = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side '{side}'. Must be 'buy' or 'sell'.")

    _maybe_snapshot_starting_equity()

    account = get_account()
    if account.trading_blocked:
        raise RuntimeError("Account trading is blocked.")

    buying_power = float(account.buying_power)
    log.info(f"Account buying power: ${buying_power:,.2f}")

    crypto      = is_crypto(symbol)
    session     = get_market_session(crypto)
    # Can we trade at all right now?
    if session == "closed":
        log.warning(f"Market CLOSED (overnight/weekend). Skipping {side.upper()} {symbol}.")
        return None
    if session == "extended" and not ENABLE_EXTENDED_HOURS:
        log.warning(f"Extended hours disabled. Skipping {side.upper()} {symbol}.")
        return None
    # Tradeable in this session
    market_open = session in ("regular", "extended", "crypto")
    spend       = notional if notional else DEFAULT_NOTIONAL

    if spend > MAX_NOTIONAL:
        log.warning(f"Requested ${spend:,.2f} exceeds MAX_NOTIONAL ${MAX_NOTIONAL:,.2f}. Capping.")
        spend = MAX_NOTIONAL

    if spend > buying_power and side == "buy":
        raise RuntimeError(
            f"Not enough buying power. Want ${spend:,.2f} but only have ${buying_power:,.2f}."
        )

    cancel_open_orders(symbol)
    existing = get_position(symbol)
    orders_placed = []

    # ── BUY signal ──────────────────────────
    if side == "buy":
        # TIER 3 — drawdown halt blocks new buys
        if _check_drawdown_halt():
            log.warning(f"Drawdown halt active — skipping BUY {symbol}.")
            send_telegram(f"⏸️ <b>TV Bot — Buy skipped</b>\n{symbol} BUY rejected (drawdown halt)")
            try:
                p = get_current_price(symbol, crypto)
            except Exception:
                p = 0.0
            journal.log_skip(symbol, "drawdown_halt", price=p,
                            metadata={"notional_requested": spend})
            return None

        if existing:
            held_qty  = abs(float(existing.qty))
            held_side = str(existing.side).lower()
            if "long" in held_side:
                log.info(f"Already LONG {held_qty} {symbol}. Skipping duplicate buy.")
                try:
                    p = get_current_price(symbol, crypto)
                except Exception:
                    p = 0.0
                journal.log_skip(symbol, "already_long", price=p,
                                metadata={"held_qty": held_qty, "notional_requested": spend})
                return None
            if "short" in held_side:
                if not market_open:
                    log.warning(f"Market CLOSED. Cannot close SHORT on {symbol}. Skipping.")
                    return None
                log.info(f"Closing legacy SHORT {held_qty} {symbol} before opening LONG.")
                close_order = submit(symbol, OrderSide.BUY, qty=held_qty, crypto=crypto, session=session)
                orders_placed.append(close_order)
                if not crypto:
                    record_day_trade()
                wait_for_fill(str(close_order.id))

        if not market_open:
            log.warning(f"Market CLOSED. Skipping fresh LONG on {symbol}.")
            journal.log_skip(symbol, "market_closed", price=0.0,
                            metadata={"notional_requested": spend, "crypto": crypto})
            return None

        log.info(f"Opening LONG ${spend} {symbol} ({session}).")
        long_order = submit(symbol, OrderSide.BUY, notional=spend, crypto=crypto, session=session)
        orders_placed.append(long_order)

        # TIER 2 — wait for fill so we can initialize trailing stop at actual entry
        if wait_for_fill(str(long_order.id)):
            try:
                price = get_current_price(symbol, crypto)
                stop = compute_stop_price(symbol, price, crypto)
                atr = compute_atr(symbol, crypto)
                with _state_lock:
                    _state["trailing_stops"][symbol] = {
                        "peak_price": price,
                        "stop_price": stop,
                        "atr": atr,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                        "entry_price": price,
                    }
                _save_state()
                # Journal: log ENTRY
                journal.log_entry(
                    symbol=symbol, qty=spend / price, price=price,
                    reason="tv_buy_signal",
                    metadata={
                        "notional": spend,
                        "trailing_stop": stop,
                        "atr": atr,
                        "crypto": crypto,
                    },
                )
                send_telegram(
                    f"🟢 <b>TV Bot — LONG opened</b>\n"
                    f"<b>{symbol}</b> @ ${price:.2f}\n"
                    f"Notional: ${spend:.0f}\n"
                    f"Trailing stop: ${stop:.2f} ({(stop/price - 1)*100:.2f}%)\n"
                    f"ATR: {atr:.4f}" if atr else
                    f"🟢 <b>TV Bot — LONG opened</b>\n"
                    f"<b>{symbol}</b> @ ${price:.2f}\n"
                    f"Notional: ${spend:.0f}\n"
                    f"Trailing stop: ${stop:.2f} (fixed 3%)"
                )
            except Exception as e:
                log.warning(f"Could not initialize trailing stop for {symbol}: {e}")
                send_telegram(
                    f"🟢 <b>TV Bot — LONG opened</b> (no trailing stop yet)\n"
                    f"<b>{symbol}</b> @ ${spend:.0f} notional"
                )

    # ── SELL signal ─────────────────────────
    elif side == "sell":
        if not existing:
            log.info(f"No position in {symbol}. Long-only: skipping sell signal.")
            try:
                p = get_current_price(symbol, crypto)
            except Exception:
                p = 0.0
            journal.log_skip(symbol, "sell_no_position", price=p,
                            metadata={})
            return None

        held_qty  = abs(float(existing.qty))
        held_side = str(existing.side).lower()
        if "short" in held_side:
            log.info(f"Position is SHORT (legacy). Long-only: skipping.")
            return None

        if "long" in held_side:
            if not market_open:
                log.warning(f"Market CLOSED. Cannot close LONG on {symbol}. Skipping.")
                return None
            # Extended-hours sells are LIMIT orders requiring whole shares.
            # If the position is fractional, defer the close to regular hours
            # so we don't strand a fractional remainder.
            if session == "extended" and not crypto and held_qty != int(held_qty):
                log.warning(
                    f"{symbol}: fractional position ({held_qty}) can't be cleanly closed "
                    f"with an extended-hours limit order. Deferring close to regular hours."
                )
                journal.log_skip(symbol, "sell_deferred_fractional_exthours",
                                price=get_current_price(symbol, crypto),
                                metadata={"held_qty": held_qty})
                return None
            entry_price = float(existing.avg_entry_price)
            log.info(f"Closing LONG {held_qty} {symbol} (signal, {session}). Going FLAT.")
            close_order = submit(symbol, OrderSide.SELL, qty=held_qty, crypto=crypto, session=session)
            orders_placed.append(close_order)
            if not crypto:
                record_day_trade()
            if wait_for_fill(str(close_order.id)):
                exit_price = get_current_price(symbol, crypto)
                pnl = (exit_price - entry_price) * held_qty
                pnl_pct = (exit_price / entry_price - 1) * 100
                emoji = "💚" if pnl >= 0 else "❤️"
                send_telegram(
                    f"{emoji} <b>TV Bot — LONG closed (signal)</b>\n"
                    f"<b>{symbol}</b>: ${entry_price:.2f} → ${exit_price:.2f}\n"
                    f"Qty: {held_qty:.4f}\n"
                    f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)"
                )
                # Journal: log EXIT
                journal.log_exit(
                    symbol=symbol, qty=held_qty, price=exit_price, pnl=pnl,
                    reason="tv_sell_signal",
                    metadata={
                        "entry_price": entry_price,
                        "pnl_pct": pnl_pct,
                        "crypto": crypto,
                    },
                )
            with _state_lock:
                _state["trailing_stops"].pop(symbol, None)
            _save_state()

    return orders_placed[-1] if orders_placed else None


# ──────────────────────────────────────────────
# Background loop — trailing stop + max hold + drawdown
# ──────────────────────────────────────────────
def _close_position_now(symbol: str, reason: str):
    """Close out a long position at market. Used by trailing stop + max hold."""
    pos = get_position(symbol)
    if pos is None:
        with _state_lock:
            _state["trailing_stops"].pop(symbol, None)
        _save_state()
        return
    crypto = is_crypto(symbol)
    if not crypto and not is_market_open():
        log.info(f"Cannot close {symbol} now — market closed. Will retry next cycle.")
        return
    held_qty = abs(float(pos.qty))
    held_side = str(pos.side).lower()
    if "long" not in held_side:
        # not a long, ignore
        with _state_lock:
            _state["trailing_stops"].pop(symbol, None)
        return
    entry_price = float(pos.avg_entry_price)
    log.info(f"AUTO-CLOSE {symbol} qty={held_qty} reason={reason}")
    try:
        cancel_open_orders(symbol)
        order = submit(symbol, OrderSide.SELL, qty=held_qty, crypto=crypto)
        if not crypto:
            record_day_trade()
        if wait_for_fill(str(order.id)):
            exit_price = get_current_price(symbol, crypto)
            pnl = (exit_price - entry_price) * held_qty
            pnl_pct = (exit_price / entry_price - 1) * 100
            emoji = "💚" if pnl >= 0 else "❤️"
            send_telegram(
                f"{emoji} <b>TV Bot — AUTO-CLOSE ({reason})</b>\n"
                f"<b>{symbol}</b>: ${entry_price:.2f} → ${exit_price:.2f}\n"
                f"Qty: {held_qty:.4f}\n"
                f"PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)"
            )
            # Journal: log EXIT (auto-close)
            journal.log_exit(
                symbol=symbol, qty=held_qty, price=exit_price, pnl=pnl,
                reason=f"autoclose_{reason.split()[0].replace('@', 'stop').lower()}",
                metadata={
                    "entry_price": entry_price,
                    "pnl_pct": pnl_pct,
                    "auto_close_reason": reason,
                    "crypto": crypto,
                },
            )
        with _state_lock:
            _state["trailing_stops"].pop(symbol, None)
        _save_state()
    except Exception as e:
        log.error(f"Auto-close failed for {symbol}: {e}")
        send_telegram(
            f"⚠️ <b>TV Bot — auto-close FAILED</b>\n"
            f"{symbol}: {reason}\nError: {e}"
        )


def _check_position_safeguards():
    """Walk every open position. Apply trailing stop + max hold."""
    try:
        positions = client.get_all_positions()
    except Exception as e:
        log.warning(f"Could not fetch positions: {e}")
        return

    if not positions:
        return

    now = datetime.now(timezone.utc)

    for pos in positions:
        symbol = pos.symbol
        # alpaca-py may return BTC/USD or BTCUSD depending; use Alpaca's format directly
        try:
            held_side = str(pos.side).lower()
            if "long" not in held_side:
                continue

            crypto = is_crypto(symbol)
            # only auto-close stocks during market hours; crypto 24/7
            if not crypto and not is_market_open():
                continue

            current_price = float(pos.current_price)
            entry_price   = float(pos.avg_entry_price)

            # Look up bot state for this symbol
            with _state_lock:
                state = _state["trailing_stops"].get(symbol)

            # If we don't have state for it (e.g. server restarted), initialize from current
            if state is None:
                stop = compute_stop_price(symbol, current_price, crypto)
                atr  = compute_atr(symbol, crypto)
                with _state_lock:
                    _state["trailing_stops"][symbol] = {
                        "peak_price": current_price,
                        "stop_price": stop,
                        "atr": atr,
                        # We don't know real opened_at; assume now (conservative — gives 3 days)
                        "opened_at": now.isoformat(),
                        "entry_price": entry_price,
                    }
                _save_state()
                log.info(f"Reconstructed trailing-stop state for {symbol} (server restart)")
                continue  # next cycle will check it

            # TIER 3b — max hold
            opened_at = datetime.fromisoformat(state["opened_at"])
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            held_days = (now - opened_at).days
            if held_days >= MAX_HOLD_DAYS:
                _close_position_now(symbol, f"max hold ({MAX_HOLD_DAYS}d)")
                continue

            # TIER 2 — trailing stop
            peak = state["peak_price"]
            if current_price > peak:
                # Move stop up (ratchet)
                new_stop = compute_stop_price(symbol, current_price, crypto)
                # Stop only ever moves UP — never down
                old_stop = state["stop_price"]
                new_stop = max(new_stop, old_stop)
                with _state_lock:
                    _state["trailing_stops"][symbol]["peak_price"] = current_price
                    _state["trailing_stops"][symbol]["stop_price"] = new_stop
                _save_state()
                log.info(
                    f"{symbol}: peak ${peak:.2f} → ${current_price:.2f} | "
                    f"stop ${old_stop:.2f} → ${new_stop:.2f}"
                )

            # Check if stop is hit
            stop_price = state["stop_price"]
            if current_price <= stop_price:
                _close_position_now(symbol, f"trailing stop @ ${stop_price:.2f}")

        except Exception as e:
            log.error(f"Error processing {symbol}: {e}\n{traceback.format_exc()}")


def _background_loop():
    """Background thread — runs forever, checks safeguards every TRAILING_CHECK_SEC."""
    log.info(f"Background safeguard loop started (interval {TRAILING_CHECK_SEC}s)")
    while True:
        try:
            _maybe_snapshot_starting_equity()
            _check_drawdown_halt()
            _check_position_safeguards()
        except Exception as e:
            log.error(f"Background loop error: {e}")
            traceback.print_exc()
        time.sleep(TRAILING_CHECK_SEC)


# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Webhook server starting — LONG ONLY MODE (v2)")
    log.info(f"   Paper trading        : {'YES' if IS_PAPER else 'NO - LIVE'}")
    log.info(f"   Default notional     : ${DEFAULT_NOTIONAL}")
    log.info(f"   Max cap              : ${MAX_NOTIONAL}")
    log.info(f"   Stop loss (fixed)    : -{STOP_LOSS_FIXED_PCT*100:.0f}%")
    log.info(f"   Stop loss (ATR)      : {STOP_LOSS_ATR_MULT}x")
    log.info(f"   Max hold             : {MAX_HOLD_DAYS} days")
    log.info(f"   Daily drawdown limit : -{DAILY_DRAWDOWN_LIMIT*100:.0f}%")
    log.info(f"   Trailing check       : every {TRAILING_CHECK_SEC}s")
    log.info(f"   Extended hours       : {'ENABLED' if ENABLE_EXTENDED_HOURS else 'disabled'}")
    log.info(f"   Telegram             : {'ENABLED' if TELEGRAM_ENABLED else 'disabled'}")

    _load_state()
    _maybe_snapshot_starting_equity()

    # Start background safeguards thread
    t = threading.Thread(target=_background_loop, daemon=True)
    t.start()

    send_telegram(
        f"🚀 <b>TV Bot v2 — STARTED</b>\n"
        f"Paper: {IS_PAPER}\n"
        f"Notional: ${DEFAULT_NOTIONAL}\n"
        f"Stop loss: -{STOP_LOSS_FIXED_PCT*100:.0f}% / {STOP_LOSS_ATR_MULT}x ATR (wider wins)\n"
        f"Max hold: {MAX_HOLD_DAYS}d\n"
        f"Drawdown halt: -{DAILY_DRAWDOWN_LIMIT*100:.0f}%"
    )

    yield
    log.info("Server shutting down")
    send_telegram("🛑 <b>TV Bot v2 — STOPPED</b>")


app = FastAPI(title="TradingView->Alpaca Long-Only Webhook v2", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        account = get_account()
        clock   = client.get_clock()
        with _state_lock:
            total_day_trades = sum(_state["day_trade_log"].values())
            dd = dict(_state["drawdown"])
            trailing_count = len(_state["trailing_stops"])
        return {
            "status"          : "ok",
            "mode"            : "LONG_ONLY_v2",
            "extended_hours"  : ENABLE_EXTENDED_HOURS,
            "current_session" : get_market_session(crypto=False),
            "market_open"     : clock.is_open,
            "next_open"       : str(clock.next_open),
            "buying_power"    : str(account.buying_power),
            "equity"          : str(account.equity),
            "paper"           : IS_PAPER,
            "day_trades_week" : total_day_trades,
            "drawdown"        : dd,
            "trailing_stops"  : trailing_count,
            "telegram"        : TELEGRAM_ENABLED,
            "timestamp"       : datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    # Validate fast (no network), ACK immediately, then do the heavy Alpaca
    # work in the background. This guarantees TradingView always gets a quick
    # 200 and never times out / auto-disables the alert.
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    log.info(f"Webhook received: {json.dumps(data)}")

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        log.warning("Bad passphrase — request rejected.")
        raise HTTPException(status_code=403, detail="Forbidden")

    missing = [f for f in ("symbol", "side") if f not in data]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    background_tasks.add_task(_process_signal, data)
    return JSONResponse({
        "status"   : "accepted",
        "symbol"   : data["symbol"],
        "side"     : data["side"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


def _process_signal(data: dict):
    """Runs AFTER the 200 has already gone out. Does the actual order work.
    Exceptions can't reach TradingView here, so we log + Telegram on failure."""
    try:
        notional = float(data["notional"]) if "notional" in data else None
        qty      = float(data["qty"])      if "qty"      in data else None
        order = place_order(
            symbol   = data["symbol"],
            side     = data["side"],
            notional = notional,
            qty      = qty,
        )
        if order is None:
            log.info(f"{data['symbol']} {data['side']}: no action (long-only/skip).")
        else:
            log.info(f"{data['symbol']} {data['side']}: order {order.id} submitted "
                     f"({order.status}).")
    except Exception as e:
        log.exception(f"_process_signal failed for {data.get('symbol')}: {e}")
        send_telegram(
            f"⚠️ <b>TV Bot — signal FAILED</b>\n"
            f"<b>{data.get('symbol')}</b> {data.get('side')}\n"
            f"{type(e).__name__}: {e}"
        )


@app.get("/positions")
def list_positions():
    try:
        positions = client.get_all_positions()
        with _state_lock:
            stops = dict(_state["trailing_stops"])
        return [
            {
                "symbol"          : p.symbol,
                "qty"             : str(p.qty),
                "side"            : str(p.side),
                "avg_entry_price" : str(p.avg_entry_price),
                "current_price"   : str(p.current_price),
                "unrealized_pl"   : str(p.unrealized_pl),
                "unrealized_plpc" : str(p.unrealized_plpc),
                "trailing_stop"   : stops.get(p.symbol),
            }
            for p in positions
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/orders")
def list_orders():
    try:
        orders = client.get_orders()
        return [
            {
                "id"              : str(o.id),
                "symbol"          : o.symbol,
                "side"            : str(o.side),
                "qty"             : str(o.qty),
                "filled_qty"      : str(o.filled_qty),
                "type"            : str(o.order_type),
                "status"          : str(o.status),
                "filled_at"       : str(o.filled_at),
                "filled_avg_price": str(o.filled_avg_price),
            }
            for o in orders
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/daytrades")
def day_trades():
    with _state_lock:
        log_copy = dict(_state["day_trade_log"])
    total = sum(log_copy.values())
    return {
        "log"         : log_copy,
        "total_week"  : total,
        "pdt_limit"   : PDT_WARN_LIMIT,
    }


@app.get("/state")
def state_dump():
    """Diagnostic — dump current bot state."""
    with _state_lock:
        return dict(_state)