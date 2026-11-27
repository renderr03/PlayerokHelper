from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.types import ErrorEvent

from . import __version__
from .alerts import Alerts
from .analytics import Analytics
from .checks import connection_snapshot, needs_kosell, save_connections
from .config import Config
from .connections import ManagedClient
from .conversations import Conversations
from .db import Database
from .engine import Engine
from .kosell import KOSell
from .listings import Listings
from .loyalty import Loyalty
from .maintenance import prune_updates
from .models import ServiceError, now
from .playerok import Playerok
from .presentation import Cards
from .rentals import Rentals
from .telegram_ui import TelegramUI, dispatcher
from .updater_worker import write
from .updates import Updates, read_json

TASK_LABELS = {
    "orders": "Новые заказы",
    "chats": "Сообщения покупателей",
    "listings": "Управление лотами",
    "jobs": "Отложенные действия",
    "delivery": "Отправка уведомлений",
    "providers": "Заказы поставщиков",
    "local": "Отчёты и обслуживание",
    "versions": "Поиск обновлений",
    "connections": "Связь с магазином",
    "recovery": "Восстановление лота",
}


class UnconfiguredPlayerok:
    configured = False
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


def market_from_config(config, db=None):
    if db and db.get("playerok_session"):
        return Playerok(json.loads(db.decrypt(db.get("playerok_session"))), config.visibility_file)
    if not config.session_file.exists():
        return UnconfiguredPlayerok()
    session = json.loads(config.session_file.read_text())
    return Playerok(session, config.visibility_file)


async def readiness(db: Database, market, kosell, *, needs_kosell=True) -> tuple[bool, list[str]]:
    snapshot = await connection_snapshot(db, market, kosell, check_kosell=needs_kosell)
    save_connections(db, snapshot)
    return not snapshot["issues"], snapshot["issues"]


class Runtime:
    def __init__(self, config, db, engine, ui):
        self.config, self.db, self.engine, self.ui = config, db, engine, ui
        self.conversations = Conversations(engine)
        self.listings = Listings(engine)
        self.analytics = Analytics(db)
        self.updates = Updates(db, getattr(config, "project_dir", None))
        self.recovered = False

    async def cycle(self):
        if self.updates.folder and (self.updates.folder / "maintenance.json").exists():
            return
        if not self.recovered:
            self.engine.recover()
            self.recovered = True
        self.updates.consume_result()
        Rentals(self.engine).tick()
        if not self.db.get("paused") and self.db.get("live_enabled"):
            Loyalty(self.engine).tick()
        if getattr(self.config, "updates_enabled", False):
            await self.updates.check()
        if now() - self.db.get("last_readiness", 0) >= 60:
            await readiness(
                self.db, self.engine.market, self.engine.kosell, needs_kosell=needs_kosell(self.db)
            )
            snapshot = self.db.get("connection_snapshot")
            alerts = Alerts(self.engine)
            alerts.connections(snapshot["services"])
            if not self.db.get("live_enabled"):
                await alerts.availability(self.db.all("SELECT * FROM bindings"), None, None, {})
            self.db.set("last_readiness", now())
        # Telegram delivery remains usable while live marketplace actions are gated.
        if self.db.get("live_enabled"):
            if now() - self.db.get("last_listings", 0) >= 60:
                await self.listings.tick(snapshot=self.db.get("connection_snapshot"))
                self.db.set("last_listings", now())
            if not self.db.get("paused"):
                await self.engine.poll_orders()
                await self.conversations.poll()
                await self.engine.run_jobs()
                await self.engine.external.tick()
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
            end = local.timestamp()
            path = self.analytics.export(
                self.config.data_dir / "reports" / f"daily-{day}.xlsx", start=start, end=end
            )
            caption = Cards(self.engine).daily(start, end)
            with self.db.transaction():
                self.db.set("report_caption:" + path.name, self.db.encrypt(caption))
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
                    self.db.execute("DELETE FROM settings WHERE key=?", ("report_caption:" + path.name,))

    def guard(self):
        if self.updates.folder and (self.updates.folder / "maintenance.json").exists():
            return False
        if not self.recovered:
            self.engine.recover()
            self.recovered = True
        return True

    async def connections(self):
        await self.ui.expire_logins()
        await readiness(self.db, self.engine.market, self.engine.kosell, needs_kosell=needs_kosell(self.db))
        snapshot = self.db.get("connection_snapshot")
        Alerts(self.engine).connections(snapshot["services"])
        if not self.db.get("live_enabled"):
            await Alerts(self.engine).availability(self.db.all("SELECT * FROM bindings"), None, None, {})
        self.db.set("last_readiness", now())

    async def recovery_cycle(self):
        # Legacy test recovery is independent of authentication and other lots.
        await self.ui.automatic_recovery()

    async def orders_cycle(self):
        if self.db.get("live_enabled") and not self.db.get("paused"):
            await self.engine.poll_orders()
            self.db.set("last_poll", now())

    async def chats_cycle(self):
        if self.db.get("live_enabled") and not self.db.get("paused"):
            await self.conversations.poll()

    async def listing_cycle(self):
        if self.db.get("live_enabled"):
            await self.listings.tick(snapshot=self.db.get("connection_snapshot"))

    async def jobs_cycle(self):
        await self.engine.run_jobs()

    async def delivery_cycle(self):
        await self.engine.flush(self.ui.send_admin)

    async def external_cycle(self):
        await self.engine.external.tick()

    async def local_cycle(self):
        self.updates.consume_result()
        Rentals(self.engine).tick()
        if self.db.get("live_enabled") and not self.db.get("paused"):
            Loyalty(self.engine).tick()
        self.maintenance()
        result_nonce = self.db.get("update_result_seen")
        if (
            self.updates.directory
            and not self.updates.busy()
            and (
                now() - self.db.get("last_update_cleanup", 0) >= 86400
                or result_nonce != self.db.get("cleanup_result_seen")
            )
        ):
            await asyncio.to_thread(prune_updates, self.updates.directory)
            self.db.set("last_update_cleanup", now())
            self.db.set("cleanup_result_seen", result_nonce)
        self.db.execute("DELETE FROM chat_messages WHERE created_at<?", (now() - 30 * 86400,))

    async def version_cycle(self):
        if getattr(self.config, "updates_enabled", False):
            await self.updates.check()

    async def scheduled(self, name, operation, interval):
        failures = 0
        while True:
            delay = interval
            try:
                if not self.guard():
                    await asyncio.sleep(interval)
                    continue
                await operation()
                failures = 0
                self.db.set("worker_health:" + name, {"ok": True, "at": now()})
                Alerts(self.engine).observe(
                    "worker:" + name,
                    False,
                    "",
                    "✅ " + TASK_LABELS.get(name, "Магазин") + ": работа восстановлена.",
                    successes=2,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = max(interval, min(300, interval * 2 ** min(failures, 5)))
                if isinstance(exc, ServiceError):
                    delay = max(delay, min(3600, exc.retry_after))
                logging.getLogger("shopbot").error("Worker %s failed: %s", name, type(exc).__name__)
                self.db.set("worker_health:" + name, {"ok": False, "at": now(), "failures": failures})
                Alerts(self.engine).observe(
                    "worker:" + name,
                    True,
                    "⚠️ " + TASK_LABELS.get(name, "Магазин") + ": временно недоступно. "
                    "Бот повторит попытку автоматически. Подробности — в состоянии магазина.",
                    "✅ " + TASK_LABELS.get(name, "Магазин") + ": работа восстановлена.",
                    failures=3,
                    successes=2,
                )
            await asyncio.sleep(delay)

    async def loop(self):
        jobs = [
            ("connections", self.connections, 60),
            ("recovery", self.recovery_cycle, 30),
            ("orders", self.orders_cycle, self.config.poll_seconds),
            ("chats", self.chats_cycle, self.config.poll_seconds),
            ("listings", self.listing_cycle, 60),
            ("jobs", self.jobs_cycle, self.config.poll_seconds),
            ("delivery", self.delivery_cycle, 2),
            ("providers", self.external_cycle, 15),
            ("local", self.local_cycle, 15),
            ("versions", self.version_cycle, 60),
        ]
        tasks = [asyncio.create_task(self.scheduled(*job)) for job in jobs]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


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
    market = ManagedClient(market_from_config(config, db))
    kosell = ManagedClient(
        KOSell(
            config.kosell_url,
            db.decrypt(db.get("kosell_token")) if db.get("kosell_token") else config.kosell_key,
        )
    )
    bot = Bot(config.token)
    engine = Engine(db, market, kosell, config.timezone)
    ui = TelegramUI(bot, engine, config.admins, config.data_dir, config.project_dir)
    ui.connection_config = config
    dp = dispatcher(ui)

    async def handle_error(event: ErrorEvent):
        logging.getLogger("shopbot").error("Telegram handler failed: %s", type(event.exception).__name__)
        return True

    dp.errors.register(handle_error)
    runtime = Runtime(config, db, engine, ui)
    worker = health = None
    try:
        await bot.get_me()
        worker = asyncio.create_task(runtime.loop())
        health = asyncio.create_task(health_loop(config.project_dir))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in [worker, health]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await ui.expire_logins_all()
        await engine.market.close()
        await engine.kosell.close()
        await bot.session.close()
        db.close()
        lock.close()


async def health_loop(directory: Path):
    folder = directory / ".updates"
    while True:
        marker = read_json(folder / "maintenance.json", {})
        if isinstance(marker, dict) and marker.get("nonce"):
            write(
                folder / "health.json",
                {
                    "nonce": marker["nonce"],
                    "pid": os.getpid(),
                    "version": __version__,
                    "at": now(),
                },
            )
        await asyncio.sleep(5)
