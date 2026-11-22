from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import ErrorEvent

from .analytics import Analytics
from .config import Config
from .conversations import Conversations
from .db import Database
from .engine import Engine
from .kosell import KOSell
from .listings import Listings
from .models import ServiceError, now
from .playerok import Playerok
from .telegram_ui import TelegramUI, dispatcher


class UnconfiguredPlayerok:
    user_id = ""
    visibility = None

    async def profile(self):
        raise ServiceError("playerok_session_not_configured")

    async def close(self):
        pass

    def __getattr__(self, name):
        async def unavailable(*args, **kwargs):
            raise ServiceError("playerok_session_not_configured")

        return unavailable


def market_from_config(config):
    if not config.session_file.exists():
        return UnconfiguredPlayerok()
    session = json.loads(config.session_file.read_text())
    return Playerok(session, config.visibility_file)


async def readiness(db: Database, market, kosell, *, needs_kosell=True) -> tuple[bool, list[str]]:
    issues = []
    try:
        profile = await market.profile()
        # Persist only the account fields used by the panel, not email or cookies.
        db.set("profile", {k: profile.get(k) for k in ("id", "username", "balance")})
        db.set("last_profile", now())
    except ServiceError as exc:
        issues.append(exc.code)
    if needs_kosell:
        try:
            db.set("kosell_balance", await kosell.balance())
        except ServiceError as exc:
            issues.append(exc.code)
    if not market.visibility:
        issues.append("visibility_contract_missing")
    else:
        proof = db.get("visibility_proof") or {}
        if proof.get("account_id") != market.user_id or proof.get("fingerprint") != market.fingerprint():
            issues.append("visibility_roundtrip_not_verified")
    db.set("readiness_status", "готово" if not issues else ", ".join(issues))
    # KOSell outages must not disable Playerok hiding or existing-stock delivery.
    market_ready = not any(i.startswith("playerok_") or i.startswith("visibility_") for i in issues)
    db.set("live_enabled", market_ready)
    return not issues, issues


class Runtime:
    def __init__(self, config, db, engine, ui):
        self.config, self.db, self.engine, self.ui = config, db, engine, ui
        self.conversations = Conversations(engine)
        self.listings = Listings(engine)
        self.analytics = Analytics(db)

    async def cycle(self):
        if now() - self.db.get("last_readiness", 0) >= 60:
            await readiness(self.db, self.engine.market, self.engine.kosell)
            self.db.set("last_readiness", now())
        # Telegram delivery remains usable while live marketplace actions are gated.
        if self.db.get("live_enabled"):
            if now() - self.db.get("last_listings", 0) >= 60:
                await self.listings.tick()
                self.db.set("last_listings", now())
            if not self.db.get("paused"):
                await self.engine.poll_orders()
                await self.conversations.poll()
                await self.engine.run_jobs()
                self.db.set("last_poll", now())
        await self.engine.flush(self.ui.send_admin)
        self.maintenance()

    def maintenance(self):
        local = datetime.now(self.engine.tz)
        day = local.date().isoformat()
        if self.db.get("last_backup_day") != day:
            self.db.backup(self.config.data_dir / "backups")
            self.db.set("last_backup_day", day)
        report_time = self.db.get("report_time")
        if (
            self.db.get("daily_report")
            and local.strftime("%H:%M") >= report_time
            and self.db.get("last_report_day") != day
        ):
            start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            path = self.analytics.export(self.config.data_dir / "reports" / f"daily-{day}.xlsx", start=start)
            with self.db.transaction():
                for admin in self.config.admins:
                    self.db.enqueue(
                        "telegram", str(admin), path.name, f"daily:{day}:{admin}", purpose="report"
                    )
                self.db.set("last_report_day", day)
        # Reports contain buyer identifiers: bound their local retention.
        for path in (self.config.data_dir / "reports").glob("*.xlsx"):
            if path.stat().st_mtime < now() - timedelta(days=7).total_seconds():
                referenced = any(
                    self.db.decrypt(r["ciphertext"]) == path.name
                    for r in self.db.all(
                        "SELECT ciphertext FROM outbox WHERE purpose='report' AND state IN ('pending','sending')"
                    )
                )
                if not referenced:
                    path.unlink()

    async def loop(self):
        while True:
            try:
                await self.cycle()
            except asyncio.CancelledError:
                raise
            except ServiceError as exc:
                self.db.notify("Сверка остановлена: " + exc.code, f"cycle:{exc.code}:{int(now() // 3600)}")
            except Exception as exc:
                # Never log exception repr, update objects, or HTTP response bodies.
                logging.getLogger("shopbot").error("Worker failed: %s", type(exc).__name__)
                self.db.notify(
                    "Ошибка обработки. Проверьте /status и журнал типов ошибок.",
                    f"worker_error:{int(now() // 3600)}",
                )
            await asyncio.sleep(self.config.poll_seconds)


async def serve(config: Config):
    config.validate()
    os.umask(0o077)
    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl

    lock = (config.data_dir / "worker.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("Уже запущен процесс с этим DATA_DIR") from None
    db = Database(config.data_dir / "shop.sqlite", config.encryption_key)
    db.set("admins", list(config.admins))
    if db.get("cutoff") is None:
        db.set("cutoff", now())
    db.set("live_enabled", False)
    market = market_from_config(config)
    kosell = KOSell(config.kosell_url, config.kosell_key)
    bot = Bot(config.token)
    engine = Engine(db, market, kosell, config.timezone)
    engine.recover()
    ui = TelegramUI(bot, engine, config.admins, config.data_dir)
    dp = dispatcher(ui)

    async def handle_error(event: ErrorEvent):
        logging.getLogger("shopbot").error("Telegram handler failed: %s", type(event.exception).__name__)
        return True

    dp.errors.register(handle_error)
    runtime = Runtime(config, db, engine, ui)
    worker = asyncio.create_task(runtime.loop())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await market.close()
        await kosell.close()
        await bot.session.close()
        db.close()
        lock.close()
