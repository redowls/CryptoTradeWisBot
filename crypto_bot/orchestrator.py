"""Orchestration & scheduling (Phase 13).

`run_cycle()` runs the whole pipeline once and logs the run to `bot_runs`:

    ingest -> indicators -> S/R -> signal -> score -> (size -> execute) ->
    manage open trades -> daily summary

Robustness:
  - per-symbol error isolation: one bad symbol can't kill the cycle;
  - a single-instance file lock prevents overlapping runs (cron + manual);
  - Telegram alerts fire at meaningful steps but never break the loop;
  - every cycle writes a `bot_runs` row (status OK / PARTIAL / ERROR).

Scheduling: `main()` starts a blocking scheduler firing just after each bar
close (top of the hour). Alternatively run `run_cycle` from cron / a systemd
timer.

CLI:
  python -m crypto_bot.orchestrator once        # one cycle
  python -m crypto_bot.orchestrator run         # start the hourly scheduler
  python -m crypto_bot.orchestrator test N       # run N cycles back-to-back
"""

from __future__ import annotations

import fcntl
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from crypto_bot import config_store
from crypto_bot.alerts import telegram as tg
from crypto_bot.analysis.scorer import persist_signal, score_decision
from crypto_bot.analysis.signal_engine import Cfg, generate_signal
from crypto_bot.data.ingest import ingest_all
from crypto_bot.indicators.engine import compute_all
from crypto_bot.analysis.levels import detect_all
from crypto_bot.db import get_session
from crypto_bot.execution.broker import place_order
from crypto_bot.execution.exits import manage_open_trades
from crypto_bot.logging_setup import get_logger, setup_logging
from crypto_bot.models import AccountSnapshot, BotRun, Trade, Watchlist
from crypto_bot.reporting.daily import generate_daily_summary
from crypto_bot.risk.sizing import RiskCfg, size_signal

log = get_logger(__name__)

_LOCKFILE = Path(__file__).resolve().parent.parent / ".orchestrator.lock"


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def single_instance_lock():
    """Prevent overlapping cycles across processes (non-blocking flock)."""
    f = open(_LOCKFILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        f.close()
        raise AlreadyRunning("another orchestrator cycle is already running") from e
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _process_symbol(session, symbol, sig_cfg, risk_cfg, threshold, place_orders) -> bool:
    """Signal -> score -> persist -> (size -> execute). Returns True if a signal was made."""
    decision = generate_signal(session, symbol, sig_cfg)
    if decision is None:
        return False
    score, breakdown = score_decision(decision, sig_cfg)
    sig_row = persist_signal(session, decision, score, breakdown, threshold)

    if decision.signal == "BUY" and score >= threshold:
        tg.notify_signal(symbol=symbol, signal=decision.signal, source=decision.signal_source,
                         score=score, entry=decision.entry_price, stop=decision.stop_price,
                         target=decision.target_price, rr=decision.rr_ratio)
        if place_orders:
            sizing = size_signal(session, decision, score, risk_cfg)
            if sizing.approved and sizing.qty > 0:
                res = place_order(session, symbol=symbol, qty=sizing.qty,
                                  stop=decision.stop_price, target=decision.target_price,
                                  signal_id=sig_row.signal_id, score=score,
                                  source=decision.signal_source, risk_pct=sizing.risk_pct)
                if res.get("recorded"):
                    tg.notify_entry(symbol=symbol, qty=res.get("qty"),
                                    price=res.get("entry_price"), trade_id=res.get("trade_id"))
            else:
                log.info("%s sizing rejected: %s", symbol, sizing.reasons)
    return True


def run_cycle(*, place_orders: bool = True, do_ingest: bool = True,
              do_indicators: bool = True, do_levels: bool = True) -> dict:
    """Run the full pipeline once and log to bot_runs. Never raises."""
    started = datetime.now(timezone.utc)
    with get_session() as session:
        run = BotRun(started_at=started, status="RUNNING")
        session.add(run)
        session.commit()
        run_id = run.run_id

    symbols_scanned = 0
    signals_generated = 0
    errors: list[str] = []
    status = "OK"

    try:
        if do_ingest:
            ingest_all()
        if do_indicators:
            compute_all(persist=True)
        if do_levels:
            detect_all(persist=True)

        sig_cfg, risk_cfg = Cfg.from_store(), RiskCfg.from_store()
        threshold = config_store.get_float("CONFIDENCE_THRESHOLD", 70.0)

        with get_session() as session:
            symbols = list(session.scalars(
                select(Watchlist.symbol).where(Watchlist.is_active == True)).all())  # noqa: E712
            for symbol in symbols:
                symbols_scanned += 1
                try:
                    if _process_symbol(session, symbol, sig_cfg, risk_cfg, threshold, place_orders):
                        signals_generated += 1
                except Exception as e:  # noqa: BLE001 — one symbol can't kill the run
                    log.exception("Symbol %s failed in cycle", symbol)
                    errors.append(f"{symbol}: {e}")
                    tg.notify_error(symbol, e)

            # Exit management + daily reporting (isolated).
            try:
                manage_open_trades(session)
            except Exception as e:  # noqa: BLE001
                log.exception("manage_open_trades failed")
                errors.append(f"exits: {e}")
            try:
                generate_daily_summary(session)
            except Exception as e:  # noqa: BLE001
                log.exception("daily summary failed")
                errors.append(f"daily: {e}")

        status = "PARTIAL" if errors else "OK"
    except Exception as e:  # noqa: BLE001 — pipeline-level failure
        log.exception("Cycle failed")
        errors.append(f"cycle: {e}")
        status = "ERROR"
        tg.notify_error("cycle", e)

    finished = datetime.now(timezone.utc)
    with get_session() as session:
        run = session.get(BotRun, run_id)
        run.finished_at = finished
        run.status = status
        run.symbols_scanned = symbols_scanned
        run.signals_generated = signals_generated
        run.error_message = "\n".join(errors) if errors else None
        session.commit()

    secs = (finished - started).total_seconds()
    log.info("Cycle #%d %s in %.1fs: scanned=%d signals=%d errors=%d",
             run_id, status, secs, symbols_scanned, signals_generated, len(errors))
    return {"run_id": run_id, "status": status, "seconds": round(secs, 1),
            "symbols_scanned": symbols_scanned, "signals_generated": signals_generated,
            "errors": errors}


def run_cycle_safe(**kwargs) -> dict | None:
    """Cycle wrapper for the scheduler: single-instance lock + never propagate."""
    try:
        with single_instance_lock():
            return run_cycle(**kwargs)
    except AlreadyRunning:
        log.warning("Skipping cycle — previous run still in progress.")
        return None
    except Exception:  # noqa: BLE001 — scheduler must keep ticking
        log.exception("run_cycle_safe caught unexpected error")
        return None


def heartbeat() -> bool:
    """Send a Telegram health ping (last run, equity, open positions, disk/load)."""
    import os
    import shutil

    with get_session() as session:
        last = session.scalar(select(BotRun).order_by(BotRun.run_id.desc()).limit(1))
        snap = session.scalar(select(AccountSnapshot)
                              .order_by(AccountSnapshot.snapshot_id.desc()).limit(1))
        open_n = len(session.scalars(select(Trade).where(Trade.status == "OPEN")).all())

    du = shutil.disk_usage("/")
    disk_free_pct = du.free / du.total * 100.0
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = -1.0

    text = (
        "💓 <b>Heartbeat</b> CryptoTradeWisBot\n"
        f"last run #{last.run_id if last else '-'} "
        f"{last.status if last else '-'} "
        f"(scanned {last.symbols_scanned if last else '-'})\n"
        f"equity {float(snap.equity):,.2f} · open {open_n}\n"
        f"disk free {disk_free_pct:.0f}% · load {load1:.2f}"
        if snap else
        "💓 <b>Heartbeat</b> CryptoTradeWisBot — no snapshot yet"
    )
    ok = tg.send_message(text)
    log.info("Heartbeat sent=%s", ok)
    return ok


def build_scheduler():
    """Blocking scheduler: cycle just after each bar close (HH:01 UTC) + 6h heartbeat."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(run_cycle_safe, CronTrigger(minute=1), id="cycle",
                  max_instances=1, coalesce=True, misfire_grace_time=300)
    sched.add_job(heartbeat, CronTrigger(hour="*/6", minute=5), id="heartbeat",
                  max_instances=1, coalesce=True)
    return sched


def main():
    setup_logging()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "run":
        log.info("Starting hourly scheduler (HH:01 UTC). Ctrl-C to stop.")
        build_scheduler().start()
    elif cmd == "heartbeat":
        print("heartbeat sent:", heartbeat())
    elif cmd == "test":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for _ in range(n):
            print(run_cycle_safe())
    else:  # once
        print(run_cycle_safe())


if __name__ == "__main__":
    main()
