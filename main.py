import os
import logging
import json
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, StopOrderRequest, StopLimitOrderRequest
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
# Config — all values come from environment variables
# Never hardcode credentials here
# ──────────────────────────────────────────────
ALPACA_API_KEY     = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER              = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "")
IS_PAPER           = "paper" in PAPER

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
# Alpaca client
# ──────────────────────────────────────────────
client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=IS_PAPER)

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    """Convert TradingView symbol format to Alpaca format."""
    s = symbol.upper().strip()
    return CRYPTO_MAP.get(s, s)

def get_position(symbol: str):
    """Get open position, returns None if flat. Handles crypto slash encoding."""
    try:
        encoded = symbol.replace("/", "%2F")
        return client.get_open_position(encoded)
    except Exception as e:
        err = str(e).lower()
        if "position does not exist" in err or "not found" in err or "404" in err:
            return None
        raise

def is_market_open() -> bool:
    clock = client.get_clock()
    return clock.is_open

def get_account():
    return client.get_account()

def cancel_open_orders(symbol: str):
    orders = client.get_orders()
    for order in orders:
        if order.symbol == symbol:
            client.cancel_order_by_id(str(order.id))
            log.info(f"Cancelled open order {order.id} for {symbol}")

# ──────────────────────────────────────────────
# Order logic
# ──────────────────────────────────────────────
def place_order(symbol: str, side: str, qty: float, order_type: str = "market",
                limit_price: float = None, stop_price: float = None):

    symbol = normalize_symbol(symbol)
    side   = side.lower().strip()

    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side '{side}'. Must be 'buy' or 'sell'.")
    if qty <= 0:
        raise ValueError(f"Invalid qty '{qty}'. Must be > 0.")

    if not is_market_open():
        log.warning(f"Market is CLOSED - order for {symbol} will queue.")

    account = get_account()
    if account.trading_blocked:
        raise RuntimeError("Account trading is blocked.")

    log.info(f"Account buying power: ${float(account.buying_power):,.2f}")

    # Always-in flip logic
    # qty from alert = desired position size
    # if already in opposite position, flip by trading 2x qty
    existing_position = get_position(symbol)

    if existing_position:
        held_qty  = abs(float(existing_position.qty))
        held_side = str(existing_position.side).lower()

        if side == "buy":
            if "long" in held_side:
                order_qty = qty
                log.info(f"Already LONG {held_qty} {symbol}. Adding {order_qty} more.")
            else:
                order_qty = held_qty + qty
                log.info(f"Flipping SHORT {held_qty} to LONG {qty} {symbol}. Buying {order_qty}.")
        else:
            if "short" in held_side:
                order_qty = qty
                log.info(f"Already SHORT {held_qty} {symbol}. Selling {order_qty} more.")
            else:
                order_qty = held_qty + qty
                log.info(f"Flipping LONG {held_qty} to SHORT {qty} {symbol}. Selling {order_qty}.")
    else:
        order_qty = qty
        log.info(f"No position in {symbol}. Entering {side.upper()} {order_qty}.")

    cancel_open_orders(symbol)

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if order_type == "market":
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=order_qty,
            side=order_side,
            time_in_force=TimeInForce.GTC
        )
    elif order_type == "limit":
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=order_qty,
            side=order_side,
            time_in_force=TimeInForce.GTC,
            limit_price=limit_price
        )
    elif order_type == "stop":
        order_data = StopOrderRequest(
            symbol=symbol,
            qty=order_qty,
            side=order_side,
            time_in_force=TimeInForce.GTC,
            stop_price=stop_price
        )
    elif order_type == "stop_limit":
        order_data = StopLimitOrderRequest(
            symbol=symbol,
            qty=order_qty,
            side=order_side,
            time_in_force=TimeInForce.GTC,
            limit_price=limit_price,
            stop_price=stop_price
        )
    else:
        raise ValueError(f"Unknown order_type: {order_type}")

    order = client.submit_order(order_data)
    log.info(f"Order submitted | id={order.id} | {side.upper()} {order_qty}x {symbol} @ {order_type} | status={order.status}")
    return order

# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Webhook server starting")
    log.info(f"   Paper trading: {'YES' if IS_PAPER else 'NO - LIVE'}")
    yield
    log.info("Server shutting down")

app = FastAPI(title="TradingView->Alpaca Webhook", lifespan=lifespan)

@app.get("/health")
def health():
    try:
        account = get_account()
        clock   = client.get_clock()
        return {
            "status"      : "ok",
            "market_open" : clock.is_open,
            "next_open"   : str(clock.next_open),
            "buying_power": str(account.buying_power),
            "equity"      : str(account.equity),
            "paper"       : IS_PAPER,
            "timestamp"   : datetime.now(timezone.utc).isoformat(),
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

    if data.get("passphrase") != WEBHOOK_PASSPHRASE:
        log.warning("Bad passphrase")
        raise HTTPException(status_code=403, detail="Forbidden")

    missing = [f for f in ("symbol", "side", "qty") if f not in data]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")

    try:
        order = place_order(
            symbol      = data["symbol"],
            side        = data["side"],
            qty         = float(data["qty"]),
            order_type  = data.get("order_type", "market"),
            limit_price = data.get("limit_price"),
            stop_price  = data.get("stop_price"),
        )

        if order is None:
            return JSONResponse({"status": "skipped", "reason": "no position to sell"})

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