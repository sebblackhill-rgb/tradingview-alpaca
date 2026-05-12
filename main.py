import os
import logging
import json
from datetime import datetime, timezone, date
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

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
# Config
# ──────────────────────────────────────────────
ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER              = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "")
IS_PAPER           = "paper" in PAPER

# ──────────────────────────────────────────────
# Risk settings
# ──────────────────────────────────────────────
DEFAULT_NOTIONAL   = 500      # fallback if alert doesn't specify
TIER_A_NOTIONAL    = 2000     # Tier A = all filters agree (high confidence)
TIER_B_NOTIONAL    = 1000     # Tier B = UT + RSI only (standard)
MAX_NOTIONAL       = 5000     # safety cap — never spend more than this per signal
PDT_WARN_LIMIT     = 3        # warn when day trades reach this number in a week

if not all([ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_PASSPHRASE]):
    raise EnvironmentError(
        "Missing required env vars: ALPACA_API_KEY, ALPACA_SECRET_KEY, WEBHOOK_PASSPHRASE"
    )

# ──────────────────────────────────────────────
# Crypto symbol map
# TradingView sends BTCUSD, Alpaca needs BTC/USD
# ──────────────────────────────────────────────
CRYPTO_MAP = {
    "BTCUSD":  "BTC/USD",
    "ETHUSD":  "ETH/USD",
    "SOLUSD":  "SOL/USD",
    "DOGEUSD": "DOGE/USD",
    "XRPUSD":  "XRP/USD",
    "LTCUSD":  "LTC/USD",
    "AVAXUSD": "AVAX/USD",
    "LINKUSD": "LINK/USD",
    "UNIUSD":  "UNI/USD",
    "AAVEUSD": "AAVE/USD",
}

# ──────────────────────────────────────────────
# PDT tracker (in-memory, resets on server restart)
# Tracks how many round-trip day trades per day
# ──────────────────────────────────────────────
day_trade_log: dict = defaultdict(int)   # { "2026-05-11": 2 }

def record_day_trade():
    today = str(date.today())
    day_trade_log[today] += 1
    total_week = sum(day_trade_log.values())
    log.info(f"Day trade recorded. Today: {day_trade_log[today]} | This week total: {total_week}")
    if total_week >= PDT_WARN_LIMIT:
        log.warning(
            f"PDT WARNING: {total_week} day trades recorded this week. "
            f"Limit is 3 in 5 days for accounts under $25,000. "
            f"Consider pausing stock trading for the rest of the week."
        )

# ──────────────────────────────────────────────
# Alpaca client
# ──────────────────────────────────────────────
client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=IS_PAPER)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    s = symbol.upper().strip()
    return CRYPTO_MAP.get(s, s)

def is_crypto(symbol: str) -> bool:
    return "/" in symbol

def get_position(symbol: str):
    """
    Look up an open position by symbol.
    Tries multiple formats to handle Alpaca storing crypto as BTCUSD
    internally even when ordered as BTC/USD, and vice versa.
    """
    # Build a list of formats to try
    # e.g. BTC/USD -> also try BTCUSD and BTC%2FUSD
    candidates = set()
    candidates.add(symbol)
    candidates.add(symbol.replace("/", ""))      # BTC/USD -> BTCUSD
    candidates.add(symbol.replace("/", "%2F"))   # BTC/USD -> BTC%2FUSD

    for candidate in candidates:
        try:
            pos = client.get_open_position(candidate)
            if pos:
                log.info(f"Found position for {symbol} using lookup key: {candidate}")
                return pos
        except Exception as e:
            err = str(e).lower()
            if "position does not exist" in err or "not found" in err or "404" in err:
                continue
            raise

    # Final fallback — scan all positions and match by symbol loosely
    try:
        all_positions = client.get_all_positions()
        symbol_clean = symbol.replace("/", "").upper()
        for pos in all_positions:
            pos_clean = pos.symbol.replace("/", "").upper()
            if pos_clean == symbol_clean:
                log.info(f"Found position for {symbol} via full scan: {pos.symbol}")
                return pos
    except Exception as e:
        log.error(f"Error scanning all positions: {e}")

    return None

def get_account():
    return client.get_account()

def is_market_open() -> bool:
    clock = client.get_clock()
    return clock.is_open

def cancel_open_orders(symbol: str):
    symbol_clean = symbol.replace("/", "").upper()
    orders = client.get_orders()
    for order in orders:
        order_clean = order.symbol.replace("/", "").upper()
        if order_clean == symbol_clean:
            client.cancel_order_by_id(str(order.id))
            log.info(f"Cancelled open order {order.id} for {symbol}")

# ──────────────────────────────────────────────
# Core order logic — Always-In with Flip
# ──────────────────────────────────────────────
def submit(symbol: str, side: OrderSide, qty: float = None,
           notional: float = None, crypto: bool = False):
    """Submit a single market order. Use qty OR notional, not both."""
    tif = TimeInForce.GTC if crypto else TimeInForce.DAY
    if notional:
        order_data = MarketOrderRequest(
            symbol=symbol,
            notional=round(notional, 2),
            side=side,
            time_in_force=tif,
        )
    else:
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=tif,
        )
    order = client.submit_order(order_data)
    log.info(
        f"Order submitted | id={order.id} | {side} "
        f"{'$'+str(notional) if notional else str(qty)+'x'} {symbol} | status={order.status}"
    )
    return order


def place_order(symbol: str, side: str, notional: float = None, qty: float = None):
    """
    Always-In Flip Logic — applied to ALL assets (stocks + crypto).

    BUY signal:
      - No position       -> open $500 LONG
      - Already LONG      -> skip (already in the right direction)
      - Currently SHORT   -> close short + open $500 LONG

    SELL signal:
      - No position       -> open $500 SHORT
      - Already SHORT     -> skip (already in the right direction)
      - Currently LONG    -> close long + open $500 SHORT

    PDT note: each flip on stocks counts as a day trade.
    Short selling stocks requires the stock to be shortable on Alpaca.
    If the short leg is rejected, the close leg still executes.
    """

    symbol = normalize_symbol(symbol)
    side   = side.lower().strip()

    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side '{side}'. Must be 'buy' or 'sell'.")

    account = get_account()
    if account.trading_blocked:
        raise RuntimeError("Account trading is blocked.")

    buying_power = float(account.buying_power)
    log.info(f"Account buying power: ${buying_power:,.2f}")

    crypto = is_crypto(symbol)
    spend  = notional if notional else DEFAULT_NOTIONAL

    # Safety cap — never accidentally spend more than MAX_NOTIONAL on a single signal
    if spend > MAX_NOTIONAL:
        log.warning(f"Requested ${spend:,.2f} exceeds MAX_NOTIONAL ${MAX_NOTIONAL:,.2f}. Capping spend.")
        spend = MAX_NOTIONAL

    # Stocks only — warn if market closed
    if not crypto and not is_market_open():
        log.warning(f"Market is CLOSED. Order for {symbol} will be queued for next open.")

    if spend > buying_power:
        raise RuntimeError(
            f"Not enough buying power. Want ${spend:,.2f} but only have ${buying_power:,.2f}."
        )

    cancel_open_orders(symbol)
    existing = get_position(symbol)

    orders_placed = []

    # ── BUY signal ───────────────────────────
    if side == "buy":
        if existing:
            held_qty  = abs(float(existing.qty))
            held_side = str(existing.side).lower()

            if "long" in held_side:
                log.info(f"Already LONG {held_qty} {symbol}. Signal agrees — skipping.")
                return None

            if "short" in held_side:
                # Close the short first
                log.info(f"Closing SHORT {held_qty} {symbol} before opening LONG.")
                close_order = submit(symbol, OrderSide.BUY, qty=held_qty, crypto=crypto)
                orders_placed.append(close_order)
                if not crypto:
                    record_day_trade()

        # Open long
        log.info(f"Opening LONG ${spend} {symbol}.")
        long_order = submit(symbol, OrderSide.BUY, notional=spend, crypto=crypto)
        orders_placed.append(long_order)

    # ── SELL signal ──────────────────────────
    elif side == "sell":
        if existing:
            held_qty  = abs(float(existing.qty))
            held_side = str(existing.side).lower()

            if "short" in held_side:
                log.info(f"Already SHORT {held_qty} {symbol}. Signal agrees — skipping.")
                return None

            if "long" in held_side:
                # Close the long first
                log.info(f"Closing LONG {held_qty} {symbol} before opening SHORT.")
                close_order = submit(symbol, OrderSide.SELL, qty=held_qty, crypto=crypto)
                orders_placed.append(close_order)
                if not crypto:
                    record_day_trade()

        # Open short
        log.info(f"Opening SHORT ${spend} {symbol}.")
        try:
            short_order = submit(symbol, OrderSide.SELL, notional=spend, crypto=crypto)
            orders_placed.append(short_order)
        except Exception as e:
            log.warning(
                f"SHORT order failed for {symbol} — stock may not be shortable. "
                f"Long position closed but short not opened. Error: {e}"
            )

    return orders_placed[-1] if orders_placed else None

# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Webhook server starting")
    log.info(f"   Paper trading   : {'YES' if IS_PAPER else 'NO - LIVE'}")
    log.info(f"   Tier A notional : ${TIER_A_NOTIONAL}")
    log.info(f"   Tier B notional : ${TIER_B_NOTIONAL}")
    log.info(f"   Default fallback: ${DEFAULT_NOTIONAL}")
    log.info(f"   Max cap         : ${MAX_NOTIONAL}")
    yield
    log.info("Server shutting down")

app = FastAPI(title="TradingView->Alpaca Webhook", lifespan=lifespan)

# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        account = get_account()
        clock   = client.get_clock()
        total_day_trades = sum(day_trade_log.values())
        return {
            "status"          : "ok",
            "market_open"     : clock.is_open,
            "next_open"       : str(clock.next_open),
            "buying_power"    : str(account.buying_power),
            "equity"          : str(account.equity),
            "paper"           : IS_PAPER,
            "day_trades_week" : total_day_trades,
            "pdt_warning"     : total_day_trades >= PDT_WARN_LIMIT,
            "default_notional": DEFAULT_NOTIONAL,
            "tier_a_notional" : TIER_A_NOTIONAL,
            "tier_b_notional" : TIER_B_NOTIONAL,
            "max_notional"    : MAX_NOTIONAL,
            "timestamp"       : datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    log.info(f"Webhook received: {json.dumps(data)}")

    # Passphrase check
    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        log.warning("Bad passphrase — request rejected.")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Required fields
    missing = [f for f in ("symbol", "side") if f not in data]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    # notional (dollars) takes priority over qty — both optional
    # falls back to DEFAULT_NOTIONAL ($500) if neither provided
    notional = float(data["notional"]) if "notional" in data else None
    qty      = float(data["qty"])      if "qty"      in data else None

    try:
        order = place_order(
            symbol   = data["symbol"],
            side     = data["side"],
            notional = notional,
            qty      = qty,
        )

        if order is None:
            return JSONResponse({"status": "skipped", "reason": "no action needed"})

        return JSONResponse({
            "status"      : "order_submitted",
            "order_id"    : str(order.id),
            "symbol"      : order.symbol,
            "side"        : str(order.side),
            "qty"         : str(order.qty),
            "type"        : str(order.order_type),
            "order_status": str(order.status),
            "timestamp"   : datetime.now(timezone.utc).isoformat(),
        })

    except ValueError as e:
        log.error(f"Validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        log.error(f"Account error: {e}")
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        log.exception(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/positions")
def list_positions():
    try:
        positions = client.get_all_positions()
        return [
            {
                "symbol"          : p.symbol,
                "qty"             : str(p.qty),
                "side"            : str(p.side),
                "avg_entry_price" : str(p.avg_entry_price),
                "current_price"   : str(p.current_price),
                "unrealized_pl"   : str(p.unrealized_pl),
                "unrealized_plpc" : str(p.unrealized_plpc),
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
    """Check how many day trades have been recorded this week."""
    total = sum(day_trade_log.values())
    return {
        "log"         : dict(day_trade_log),
        "total_week"  : total,
        "pdt_warning" : total >= PDT_WARN_LIMIT,
        "pdt_limit"   : PDT_WARN_LIMIT,
    }