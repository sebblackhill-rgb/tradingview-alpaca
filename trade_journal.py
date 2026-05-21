"""
trade_journal.py — Universal trade journal helper

Shared across all bots. Each bot writes to its own folder, but all use the
SAME schema so a unified dashboard can aggregate across bots.

Schema (JSON, append-only):
  Required core (8 fields):
    timestamp       ISO 8601 with timezone
    bot_name        e.g. "ubot", "five_indicator", "tradingview"
    account_id      Alpaca account number (resolved at startup)
    symbol          e.g. "NVDA"
    action          "ENTRY" | "EXIT" | "SKIP"
    qty             float (positive for buy entries, positive for sell exits, 0 for skip)
    price           float (signal price or fill price)
    reason          short tag like "ut_buy_cross" or "tp_hit" or "skip_max_positions"

  Optional:
    pnl             float, only for EXIT (None for ENTRY and SKIP)
    metadata        dict, bot-specific extras (ml_prob, composite, ATR, etc.)

Usage from a bot:
    from trade_journal import TradeJournal

    journal = TradeJournal(
        bot_name="ubot",
        account_id=trading_client.get_account().account_number,
        journal_path=Path(__file__).parent / "trade_journal.json"
    )

    # Log an entry
    journal.log_entry("NVDA", qty=10, price=750.25,
                      reason="ut_buy_cross",
                      metadata={"atr": 2.34, "vol_ratio": 1.8})

    # Log an exit
    journal.log_exit("NVDA", qty=10, price=755.50, pnl=52.50,
                     reason="opposite_signal",
                     metadata={"hold_minutes": 25})

    # Log an "almost trade" (passed filters but skipped)
    journal.log_skip("AAPL", reason="max_positions_reached", price=189.50,
                     metadata={"open_positions": 10, "max_positions": 10})
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional


class TradeJournal:
    """Append-only JSON journal. Thread-safe within a single process."""

    SCHEMA_VERSION = "1.0"

    def __init__(self, bot_name: str, account_id: str, journal_path: Path):
        self.bot_name = bot_name
        self.account_id = account_id or "unknown"
        self.path = Path(journal_path)
        self._lock = Lock()

        # Ensure parent directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize empty journal if doesn't exist
        if not self.path.exists():
            self.path.write_text(json.dumps([], indent=2))

    def _append(self, record: dict) -> None:
        """Atomic append. Reads entire file, appends, writes back.
        For high-volume use, consider switching to JSONL (one record per line)."""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                data = []

            data.append(record)

            # Write to temp file then rename for atomicity
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            tmp.replace(self.path)

    def _record(self, action: str, symbol: str, qty: float, price: float,
                reason: str, pnl: Optional[float] = None,
                metadata: Optional[dict] = None) -> dict:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_name": self.bot_name,
            "account_id": self.account_id,
            "symbol": str(symbol).upper(),
            "action": action,
            "qty": float(qty),
            "price": float(price) if price is not None else 0.0,
            "pnl": float(pnl) if pnl is not None else None,
            "reason": reason,
            "metadata": metadata or {},
            "_schema": self.SCHEMA_VERSION,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_entry(self, symbol: str, qty: float, price: float,
                  reason: str, metadata: Optional[dict] = None) -> None:
        """Log a position entry."""
        rec = self._record("ENTRY", symbol, qty, price, reason, None, metadata)
        self._append(rec)

    def log_exit(self, symbol: str, qty: float, price: float, pnl: float,
                 reason: str, metadata: Optional[dict] = None) -> None:
        """Log a position exit with realized PnL."""
        rec = self._record("EXIT", symbol, qty, price, reason, pnl, metadata)
        self._append(rec)

    def log_skip(self, symbol: str, reason: str, price: float = 0.0,
                 metadata: Optional[dict] = None) -> None:
        """Log an 'almost trade' — passed filters but was skipped.

        Use for cases like:
          - max positions reached
          - sector cap reached
          - cooldown active
          - dry-run mode
          - drawdown halt active
        """
        rec = self._record("SKIP", symbol, 0.0, price, reason, None, metadata)
        self._append(rec)

    # Convenience: bulk replay / analysis
    def read_all(self) -> list:
        """Read all records from this journal."""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return []

    def summary(self) -> dict:
        """Quick statistics over the journal."""
        records = self.read_all()
        if not records:
            return {"total": 0}

        entries = [r for r in records if r["action"] == "ENTRY"]
        exits = [r for r in records if r["action"] == "EXIT"]
        skips = [r for r in records if r["action"] == "SKIP"]

        exit_pnls = [r["pnl"] for r in exits if r.get("pnl") is not None]
        total_pnl = sum(exit_pnls) if exit_pnls else 0.0
        wins = [p for p in exit_pnls if p > 0]
        win_rate = (len(wins) / len(exit_pnls) * 100) if exit_pnls else 0.0

        return {
            "total_records": len(records),
            "entries": len(entries),
            "exits": len(exits),
            "skips": len(skips),
            "total_pnl": round(total_pnl, 2),
            "win_rate_pct": round(win_rate, 1),
            "first_record": records[0]["timestamp"] if records else None,
            "last_record": records[-1]["timestamp"] if records else None,
        }
