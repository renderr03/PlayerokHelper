import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_setup_console import TOKEN, FakeConsole

from shopbot import service
from shopbot.setup_console import configure


@pytest.fixture
def installation(tmp_path, monkeypatch):
    directory = tmp_path / "project with spaces"
    executable = directory / ".venv/bin/playerokhelper"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    configure(directory, FakeConsole([TOKEN, "42"]))
    monkeypatch.setattr(service, "UNIT_PATH", tmp_path / service.SERVICE_NAME)
    monkeypatch.setattr(service, "UPDATE_HELPER_PATH", tmp_path / "libexec/updater.py")
    monkeypatch.setattr(service, "UPDATE_CONFIG_PATH", tmp_path / "updater.json")
    monkeypatch.setattr(service, "check_platform", lambda: None)
    monkeypatch.setattr(service.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(service.os, "getegid", lambda: 1000)
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/" + name)
    return directory


def test_service_is_enabled_started_and_checked_with_same_installation_user(installation):
    calls, messages, units = [], [], []

    def runner(args, **kwargs):
        calls.append(args)
        if "install" in args:
            if "-d" in args:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0, "", "")
            unit = Path(args[-2]).read_text()
            units.append(unit)
            Path(args[-1]).write_text(unit)
        return subprocess.CompletedProcess(args, 0, "", "")

    service.install_service(installation, runner=runner, output=messages.append)
    assert [c[1:3] for c in calls if "systemctl" in c] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable"],
        ["systemctl", "is-active"],
        ["systemctl", "enable"],
        ["systemctl", "reset-failed"],
        ["systemctl", "restart"],
        ["systemctl", "is-active"],
    ]
    assert all(c[0] == "sudo" for c in calls)
    assert "User=1000" in units[0] and "Group=1000" in units[0]
    assert "Restart=always" in units[0] and "WantedBy=multi-user.target" in units[0]
    assert "RestartSec=10" in units[0] and "UMask=0077" in units[0]
    assert "StartLimitIntervalSec=0" in units[0]
    assert TOKEN not in units[0] and TOKEN not in "\n".join(messages)
    assert "EnvironmentFile" not in units[0]
    assert "запущен" in messages[-1]
    config = __import__("json").loads(service.UPDATE_CONFIG_PATH.read_text())
    assert config["uid"] == 1000 and config["directory"] == str(installation)
    assert TOKEN not in service.UPDATE_CONFIG_PATH.read_text()
    assert (installation / ".updates/enabled.json").exists()
    before = (installation / ".env").read_bytes()
    service.install_service(installation, runner=runner, output=messages.append)
    assert (installation / ".env").read_bytes() == before


def test_root_service_install_does_not_require_sudo(installation, monkeypatch):
    monkeypatch.setattr(service.os, "geteuid", lambda: 0)
    calls = []
    service.install_service(
        installation, runner=lambda args, **kwargs: calls.append(args), output=lambda _: None
    )
    assert calls[0][0] == "install"
    assert calls[-1] == ["systemctl", "is-active", "--quiet", service.SERVICE_NAME]


def test_service_start_failure_does_not_report_success_or_echo_provider_output(installation):
    messages = []

    def runner(args, **kwargs):
        if "restart" in args:
            raise subprocess.CalledProcessError(1, args, stderr=TOKEN)
        return subprocess.CompletedProcess(args, 0)

    with pytest.raises(service.ServiceSetupError) as error:
        service.install_service(installation, runner=runner, output=messages.append)
    assert TOKEN not in str(error.value)
    assert not any("запущен" in text for text in messages)


def test_different_installation_or_manually_owned_service_is_preserved(installation):
    original = "[Service]\nExecStart=/usr/bin/sleep infinity\n"
    service.UNIT_PATH.write_text(original)
    with pytest.raises(service.ServiceSetupError, match="другой установке"):
        service.install_service(installation, runner=lambda *a, **k: pytest.fail("Must not run commands"))
    assert service.UNIT_PATH.read_text() == original
    service.UNIT_PATH.write_text(service.render_unit(installation.parent / "another", 1000, 1000))
    with pytest.raises(service.ServiceSetupError, match="другой установке"):
        service.install_service(installation, runner=lambda *a, **k: pytest.fail("Must not run commands"))


def test_unsupported_platform_has_explicit_manual_mode(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    with pytest.raises(service.ServiceSetupError, match="SHOPBOT_AUTOSTART=0"):
        service.check_platform()


def test_paths_escape_systemd_specifiers_and_exec_environment_variables(tmp_path):
    unit = service.render_unit(tmp_path / 'folder %h ${TOKEN} "quoted"', 1000, 1000)
    working = next(line for line in unit.splitlines() if line.startswith("WorkingDirectory="))
    executable = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "%%h" in working and "${TOKEN}" in working
    assert "%%h" in executable and "$${TOKEN}" in executable
    assert '\\"quoted\\"' in executable
    with pytest.raises(service.ServiceSetupError):
        service.render_unit(tmp_path / "bad\nExecStart=/bin/false", 1000, 1000)


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze is unavailable")
def test_systemd_accepts_generated_unit_syntax(installation):
    path = service.UNIT_PATH
    path.write_text(service.render_unit(installation, os.getuid(), os.getgid()))
    runtime = installation.parent / "runtime"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(path)],
        env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze is unavailable")
def test_systemd_accepts_update_manager_and_path_units(installation):
    paths = []
    for name, text in zip(
        ["playerokhelper-update.service", "playerokhelper-update.path"],
        service.render_update_units(installation),
        strict=True,
    ):
        path = installation.parent / name
        path.write_text(text)
        paths.append(str(path))
        assert TOKEN not in text
        if name.endswith(".service"):
            assert 'ExecStart="/usr/bin/python3"' in text
    runtime = installation.parent / "runtime"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", *paths],
        env=dict(os.environ, XDG_RUNTIME_DIR=str(runtime)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "mode,expected",
    [
        (None, ["pip", "setup", "telegram-check", "install-service"]),
        ("0", ["pip", "setup", "telegram-check"]),
    ],
)
def test_shell_installer_defaults_to_service_but_can_opt_out(tmp_path, mode, expected):
    directory = tmp_path / "installer"
    (directory / "src/shopbot").mkdir(parents=True)
    (directory / "pyproject.toml").touch()
    shutil.copyfile(Path(__file__).parents[1] / "install.sh", directory / "install.sh")
    binary = directory / ".venv/bin"
    binary.mkdir(parents=True)
    (binary / "python").write_text('#!/bin/sh\nprintf "pip\\n" >> "$SHOPBOT_TEST_LOG"\n')
    (binary / "playerokhelper").write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$SHOPBOT_TEST_LOG"\n')
    for executable in binary.iterdir():
        executable.chmod(0o755)
    log = tmp_path / "calls.log"
    env = dict(os.environ, SHOPBOT_TEST_LOG=str(log), SHOPBOT_PYTHON=sys.executable)
    env.pop("SHOPBOT_AUTOSTART", None)
    if mode is not None:
        env["SHOPBOT_AUTOSTART"] = mode
    result = subprocess.run(["bash", str(directory / "install.sh")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == expected


def test_invalid_autostart_option_fails_before_installation(tmp_path):
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "install.sh")],
        env=dict(os.environ, SHOPBOT_AUTOSTART="invalid"),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "SHOPBOT_AUTOSTART" in result.stderr
