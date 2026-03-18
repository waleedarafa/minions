from __future__ import annotations

from src.tools.security import SecurityChecker, SecurityPolicy, get_security_events, reset_security_events


def test_security_checker_blocks_writes_outside_workdir(tmp_path):
    checker = SecurityChecker(SecurityPolicy(working_dir=str(tmp_path), allow_write=True))
    assert checker.check_file_access("/tmp/outside.txt", "w") is False


def test_security_checker_resolves_relative_paths_against_workdir(tmp_path):
    checker = SecurityChecker(SecurityPolicy(working_dir=str(tmp_path), allow_write=True))
    assert checker.check_file_access("src/app.py", "w") is True


def test_security_checker_blocks_disallowed_commands():
    reset_security_events()
    checker = SecurityChecker(SecurityPolicy(allowed_commands=["git"], working_dir="."))
    assert checker.check_command("python -m pytest") is False
    assert any(event["reason"] == "disallowed_command" for event in get_security_events())


def test_security_checker_blocks_shell_control_operators():
    reset_security_events()
    checker = SecurityChecker(SecurityPolicy(allowed_commands=["python"], working_dir="."))
    assert checker.check_command("python -m pytest && echo done") is False
    assert any(event["reason"] == "shell_control" for event in get_security_events())


def test_security_checker_blocks_destructive_commands():
    checker = SecurityChecker(SecurityPolicy(allowed_commands=["rm"], working_dir="."))
    assert checker.check_command("rm -rf /tmp/test") is False


def test_security_checker_blocks_network_commands_when_network_disabled():
    checker = SecurityChecker(SecurityPolicy(allowed_commands=["curl"], working_dir=".", allow_network=False))
    assert checker.check_command("curl https://example.com") is False
