from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .config import Config
from .db import Database
from .kosell import KOSell
from .models import ServiceError, now, rub
from .runtime import market_from_config, readiness, serve


async def doctor(config):
    if not config.encryption_key:
        raise ValueError("Заполните STOCK_ENCRYPTION_KEY")
    db = Database(config.data_dir / "shop.sqlite", config.encryption_key)
    market = market_from_config(config)
    kosell = KOSell(config.kosell_url, config.kosell_key)
    try:
        ready, issues = await readiness(db, market, kosell)
        print(json.dumps({"ready": ready, "issues": issues}, ensure_ascii=False, indent=2))
        return 0 if ready else 2
    finally:
        await market.close()
        await kosell.close()
        db.close()


async def verify_visibility(config, item_id):
    import fcntl

    config.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (config.data_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError("stop_worker_before_visibility_test") from None
        await _verify_visibility(config, item_id)


async def _verify_visibility(config, item_id):
    db = Database(config.data_dir / "shop.sqlite", config.encryption_key)
    market = market_from_config(config)
    try:
        before = await market.profile()
        if not market.visibility:
            raise ServiceError("visibility_contract_missing")
        item = await market.item(item_id)
        if (item.get("user") or {}).get("id") != market.user_id or item["status"] != "APPROVED":
            raise ServiceError("test_requires_own_active_listing")
        # If a previous attempt failed, do not leave a stale success proof.
        db.set("visibility_proof", None)
        db.audit("visibility_test_started", item_id)
        hidden = False
        try:
            await market.set_visible(item_id, False)
            hidden = True
        finally:
            # Restore even if the hide response was lost; verify actual state first.
            current = await market.item(item_id)
            if current["status"] in market.visibility["hide"]["hidden_statuses"]:
                await market.set_visible(item_id, True)
        if not hidden:
            raise ServiceError("hide_roundtrip_uncertain")
        restored = await market.item(item_id)
        if restored["status"] != "APPROVED":
            raise ServiceError("restore_not_verified")
        after = await market.profile()
        if rub(after["balance"]["value"]) < rub(before["balance"]["value"]):
            raise ServiceError("visibility_test_balance_decreased_check_transactions")
        db.set(
            "visibility_proof",
            {
                "account_id": market.user_id,
                "fingerprint": market.fingerprint(),
                "item_id": item_id,
                "verified_at": now(),
            },
        )
        db.audit("visibility_test_passed", item_id)
        print("Скрытие и восстановление подтверждены. Выполните playerok-bot doctor.")
    finally:
        await market.close()
        db.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="PlayerokHelper — автоматизация Playerok и KOSell")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Telegram panel and worker")
    sub.add_parser("keygen", help="Generate a new encryption key; do not rotate an existing database key")
    sub.add_parser("doctor", help="Read-only connection checks; no purchases or listing changes")
    setup_parser = sub.add_parser("setup", help="Настроить токен бота и Telegram ID администратора в консоли")
    setup_parser.add_argument("--directory", type=Path, default=Path.cwd())
    setup_parser.add_argument(
        "--connections", action="store_true", help="Также настроить KOSell и сессию Playerok"
    )
    verify = sub.add_parser(
        "verify-visibility", help="Explicit live test: hide and restore your test listing"
    )
    verify.add_argument("--item", required=True)
    verify.add_argument("--confirm-reversible-test", action="store_true", required=True)
    backup = sub.add_parser("backup", help="Create an online database backup")
    backup.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.command == "keygen":
        print(Fernet.generate_key().decode())
        return
    if args.command == "setup":
        from .setup_console import setup

        try:
            setup(args.directory, connections=args.connections)
        except (EOFError, KeyboardInterrupt):
            parser.exit(2, "Настройка отменена.\n")
        except (ValueError, OSError):
            parser.exit(
                2, "Настройки не сохранены: проверьте консоль, права файлов и существующий ключ шифрования.\n"
            )
        return
    config = Config.load()
    logging.basicConfig(level=logging.ERROR, format="%(name)s: %(message)s")
    try:
        if args.command == "run":
            asyncio.run(serve(config))
        elif args.command == "doctor":
            raise SystemExit(asyncio.run(doctor(config)))
        elif args.command == "verify-visibility":
            import uuid

            asyncio.run(verify_visibility(config, str(uuid.UUID(args.item))))
        elif args.command == "backup":
            db = Database(config.data_dir / "shop.sqlite", config.encryption_key)
            try:
                print(db.backup(args.directory or config.data_dir / "backups"))
            finally:
                db.close()
    except ServiceError as exc:
        parser.exit(2, "Операция не завершена: " + exc.code + "\n")
    except (ValueError, OSError, InvalidToken) as exc:
        # Do not leak key material, cookie JSON, URLs with credentials, or transport errors.
        parser.exit(2, "Ошибка настройки: " + type(exc).__name__ + ". Проверьте .env и файлы подключения.\n")


if __name__ == "__main__":
    main()
