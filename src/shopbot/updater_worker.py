"""Installed as a root-owned, stdlib-only systemd helper.

The manager only controls the fixed bot service. Repository code, pip, file
operations and request parsing run under the installation owner's UID/GID.
This file is copied during console installation, never replaced by a bot update.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

REPOSITORY = "https://github.com/renderr03/PlayerokHelper.git"
SERVICE = "playerokhelper.service"


class UpdateError(Exception):
    pass


def read(path, default=None):
    try:
        if path.stat().st_size > 100_000:
            raise UpdateError("invalid_state")
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def version(value):
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise UpdateError("invalid_version")
    return tuple(map(int, match.groups()))


def run(args, *, cwd=None, timeout=600):
    account = pwd.getpwuid(os.geteuid())
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": account.pw_dir,
        "LANG": "C.UTF-8",
        "PIP_NO_INPUT": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        return subprocess.run(
            args, cwd=cwd, env=environment, check=True, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        raise UpdateError("command_failed") from None


class Worker:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config["directory"])
        self.folder = self.directory / ".updates"
        self.data = Path(config["data_dir"])

    def git(self, *args):
        return run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.directory), *args])

    def request(self):
        request = read(self.folder / "request.json")
        if not request:
            request = (read(self.folder / "prepared.json", {}) or {}).get("request")
        if not isinstance(request, dict):
            raise UpdateError("invalid_request")
        if not re.fullmatch(r"[0-9a-f]{32}", str(request.get("nonce", ""))):
            raise UpdateError("invalid_request")
        if not re.fullmatch(r"[0-9a-f]{40}", str(request.get("sha", ""))):
            raise UpdateError("invalid_request")
        version(request.get("tag", ""))
        return request

    def state(self):
        state = read(self.folder / "prepared.json", {})
        return state if state.get("request", {}).get("nonce") == self.request()["nonce"] else {}

    def current_version(self):
        return run(
            [
                str(self.directory / ".venv/bin/python"),
                "-c",
                "from importlib.metadata import version;print(version('PlayerokHelper'))",
            ]
        )

    def clean(self):
        if self.git("status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("modified_project")
        if self.git("branch", "--show-current") != "main":
            raise UpdateError("main_branch_required")
        if self.git("remote", "get-url", "origin") not in {
            REPOSITORY,
            "git@github.com:renderr03/PlayerokHelper.git",
        }:
            raise UpdateError("unexpected_repository")

    def prepare(self):
        request = self.request()
        self.clean()
        previous = self.current_version()
        if version(request["tag"]) <= version(previous):
            raise UpdateError("not_a_new_version")
        old_sha = self.git("rev-parse", "HEAD")
        options = ["--no-tags"]
        if self.git("rev-parse", "--is-shallow-repository") == "true":
            options.append("--unshallow")
        ref = "refs/playerokhelper/" + request["nonce"]
        self.git("fetch", *options, REPOSITORY, f"refs/tags/{request['tag']}:{ref}")
        if self.git("rev-parse", ref + "^{commit}") != request["sha"]:
            raise UpdateError("release_tag_changed")
        self.git("merge-base", "--is-ancestor", old_sha, request["sha"])
        protected = [
            ".env",
            ".venv",
            ".updates",
            "data",
            "secrets",
            "shop.sqlite",
            "shop.sqlite-wal",
            "shop.sqlite-shm",
        ]
        if self.data.is_relative_to(self.directory):
            protected.append(self.data.relative_to(self.directory).as_posix())
        for name in self.git("diff", "--name-only", old_sha, request["sha"]).splitlines():
            if any(name == p or name.startswith(p + "/") for p in protected):
                raise UpdateError("release_changes_private_files")
        stage = self.folder / "releases" / request["nonce"]
        stage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stage.exists():
            shutil.rmtree(stage)
        run(["git", "clone", "--shared", "--no-checkout", str(self.directory), str(stage)])
        run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(stage),
                "checkout",
                "--detach",
                request["sha"],
            ]
        )
        run([self.config["python"], "-m", "venv", str(stage / ".venv")])
        run(
            [
                str(stage / ".venv/bin/python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-c",
                str(stage / "constraints.txt"),
                str(stage),
            ]
        )
        installed = run(
            [str(stage / ".venv/bin/python"), "-c", "from shopbot import __version__;print(__version__)"]
        )
        if version(installed) != version(request["tag"]):
            raise UpdateError("package_version_mismatch")
        state = {
            "request": request,
            "old_sha": old_sha,
            "old_version": previous,
            "phase": "prepared",
            "started_at": time.time(),
        }
        write(self.folder / "prepared.json", state)
        return state

    @contextmanager
    def database_lock(self):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.data / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpdateError("worker_still_running") from None
            yield

    def activate(self):
        state = self.state()
        request = state["request"]
        nonce = request["nonce"]
        with self.database_lock():
            self.clean()
            if self.git("rev-parse", "HEAD") != state["old_sha"]:
                raise UpdateError("project_changed_during_update")
            backup = self.folder / "backups" / nonce
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            db_path = self.data / "shop.sqlite"
            state["had_db"] = db_path.exists()
            if state["had_db"]:
                with sqlite3.connect(db_path) as source, sqlite3.connect(backup / "shop.sqlite") as target:
                    source.backup(target)
                os.chmod(backup / "shop.sqlite", 0o600)
            shutil.copy2(self.directory / ".env", backup / ".env")
            os.chmod(backup / ".env", 0o600)
            # Persist recovery information before the first live-file change.
            state["phase"] = "activating"
            state["changed"] = True
            write(self.folder / "prepared.json", state)
            write(self.folder / "maintenance.json", request)
            self.git("merge", "--ff-only", "--no-edit", request["sha"])
            os.replace(self.directory / ".venv", self.folder / ("env-before-" + nonce))
            (self.directory / ".venv").symlink_to(
                self.folder / "releases" / nonce / ".venv", target_is_directory=True
            )
            state["phase"] = "active"
            write(self.folder / "prepared.json", state)
        return state

    def rollback(self):
        state = self.state()
        nonce = state["request"]["nonce"]
        if state["phase"] == "prepared" or (state["phase"] == "restored" and not state.get("changed")):
            with self.database_lock():
                write(self.folder / "maintenance.json", state["request"])
                state.update(phase="restored", changed=False)
                write(self.folder / "prepared.json", state)
            return state
        if state["phase"] not in {"activating", "active", "restored"}:
            return state
        with self.database_lock():
            write(self.folder / "maintenance.json", state["request"])
            self.git("reset", "--hard", state["old_sha"])
            saved_env = self.folder / ("env-before-" + nonce)
            if saved_env.exists() or saved_env.is_symlink():
                live = self.directory / ".venv"
                if live.is_symlink():
                    live.unlink()
                elif live.exists():
                    raise UpdateError("unexpected_live_environment")
                os.replace(saved_env, live)
            backup = self.folder / "backups" / nonce
            shutil.copy2(backup / ".env", self.directory / ".env")
            if state["had_db"]:
                with (
                    sqlite3.connect(backup / "shop.sqlite") as source,
                    sqlite3.connect(self.data / "shop.sqlite") as target,
                ):
                    source.backup(target)
                os.chmod(self.data / "shop.sqlite", 0o600)
            else:
                for suffix in ["", "-wal", "-shm"]:
                    (self.data / ("shop.sqlite" + suffix)).unlink(missing_ok=True)
            state["phase"] = "restored"
            write(self.folder / "prepared.json", state)
        return state

    def finish(self, status):
        try:
            request = self.request()
        except (UpdateError, ValueError, TypeError):
            (self.folder / "request.json").unlink(missing_ok=True)
            return {"status": "failed"}
        state = self.state()
        if state.get("phase") == "finished":
            status = state["status"]
        result = {
            "nonce": request["nonce"],
            "tag": request["tag"],
            "status": status,
            "finished_at": time.time(),
            "previous_version": state.get("old_version"),
        }
        error = read(self.folder / "error.json", {})
        if isinstance(error, dict) and error.get("nonce") == request["nonce"]:
            result["reason"] = error.get("code")
        elif status in {"rolled_back", "manual_required"}:
            result["reason"] = "health_check_failed"
        write(self.folder / "result.json", result)
        state.update(phase="finished", status=status, request=request)
        write(self.folder / "prepared.json", state)
        if status != "manual_required":
            (self.folder / "maintenance.json").unlink(missing_ok=True)
        (self.folder / "request.json").unlink(missing_ok=True)
        return result

    def inspect(self):
        state = self.state()
        return {"recover": state.get("phase") in {"activating", "active", "restored"}, **state}

    def health(self):
        value = read(self.folder / "health.json", {})
        return {k: value.get(k) for k in ("nonce", "version", "pid", "at")}

    def notify_failure(self):
        # Keep secrets inside the unprivileged child, never in arguments or output.
        script = """
import sys, urllib.request, urllib.parse
from dotenv import dotenv_values
settings = dotenv_values(sys.argv[1], interpolate=False)
token = settings.get('TELEGRAM_BOT_TOKEN', '')
for admin in settings.get('ADMIN_IDS', '').split(','):
    if not admin.strip().isdigit():
        continue
    body = urllib.parse.urlencode({'chat_id': admin.strip(), 'text':
        'Автоматическое восстановление PlayerokHelper не завершено. Нужна проверка сервера: '
        'systemctl status playerokhelper; journalctl -u playerokhelper-update -n 50 --no-pager.'}).encode()
    with urllib.request.urlopen(urllib.request.Request(
            'https://api.telegram.org/bot' + token + '/sendMessage', data=body), timeout=10):
        pass
"""
        run(
            [str(self.directory / ".venv/bin/python"), "-c", script, str(self.directory / ".env")], timeout=45
        )
        return {"notified": True}


def orchestrate(config_path, config, *, execute=subprocess.run, sleep=time.sleep, clock=time.monotonic):
    """Only the fixed service is controlled as root; worker output never becomes a root command."""
    helper = str(Path(__file__).resolve())

    def worker(phase):
        account = pwd.getpwuid(config["uid"])
        result = execute(
            [sys.executable, helper, "--config", str(config_path), "--worker", phase],
            user=config["uid"],
            group=config["gid"],
            extra_groups=[],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": account.pw_dir, "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            check=True,
            timeout=900,
        )
        return json.loads(result.stdout)

    def control(action):
        return execute(
            ["/usr/bin/systemctl", action, SERVICE], capture_output=True, text=True, check=True, timeout=60
        )

    def wait_healthy(request, expected):
        deadline, previous = clock() + 90, None
        while clock() < deadline:
            health = worker("health")
            pid = execute(
                ["/usr/bin/systemctl", "show", "--property=MainPID", "--value", SERVICE],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
            if (
                health.get("nonce") == request["nonce"]
                and health.get("version") == expected.lstrip("v")
                and str(health.get("pid")) == pid
                and pid != "0"
                and isinstance(health.get("at"), (float, int))
                and 0 <= time.time() - health["at"] < 15
            ):
                if previous is not None and previous[0] == pid and health["at"] > previous[1]:
                    return
                previous = (pid, health["at"])
            else:
                previous = None
            sleep(2)
        raise UpdateError("health_check_failed")

    stopped = False
    try:
        state = worker("inspect")
        if state.get("phase") == "finished":
            return worker("finish:" + state["status"])
        if state.get("recover"):
            stopped = True
            raise UpdateError("interrupted_update")
        state = worker("prepare")
        stopped = True
        control("stop")
        worker("activate")
        control("start")
        wait_healthy(state["request"], state["request"]["tag"])
        return worker("finish:success")
    except Exception:
        if stopped:
            try:
                control("stop")
                restored = worker("rollback")
                control("start")
                if restored.get("phase") == "restored":
                    wait_healthy(restored["request"], restored["old_version"])
                return worker("finish:rolled_back")
            except Exception:
                result = worker("finish:manual_required")
                try:
                    worker("notify_failure")
                except Exception:
                    pass
                return result
        return worker("finish:failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--worker")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = read(args.config)
        if args.worker:
            worker = Worker(config)
            if args.worker.startswith("finish:"):
                status = args.worker.split(":", 1)[1]
                if status not in {"success", "failed", "rolled_back", "manual_required"}:
                    raise UpdateError("invalid_status")
                result = worker.finish(status)
            elif args.worker in {"inspect", "prepare", "activate", "rollback", "health", "notify_failure"}:
                result = getattr(worker, args.worker)()
            else:
                raise UpdateError("invalid_phase")
            print(json.dumps(result))
        else:
            if os.geteuid() != 0 or args.config.stat().st_uid != 0 or args.config.stat().st_mode & 0o022:
                raise UpdateError("root_owned_configuration_required")
            with Path("/run/playerokhelper-updater.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = orchestrate(args.config, config)
                print("PlayerokHelper updater: " + result.get("status", "unknown"))
    except Exception as exc:
        if args.worker:
            try:
                worker = Worker(config)
                request = worker.request()
                write(
                    worker.folder / "error.json",
                    {
                        "nonce": request["nonce"],
                        "code": str(exc) if isinstance(exc, UpdateError) else "internal_error",
                    },
                )
            except Exception:
                pass
        # Subprocess output may contain private data: never print it.
        print("Update helper failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
