"""Which shell runs `bash` tool commands, and that its output reaches the model readable."""
from __future__ import annotations

import os
import shutil

import pytest

from codelet.tools.bash_tool import BashTool, decode_output, resolve_shell

needs_pwsh = pytest.mark.skipif(os.name != "nt" or shutil.which("pwsh") is None,
                                reason="PowerShell 7 is the Windows default only when installed")


def test_decode_output_reads_utf8_and_the_windows_console_codepage():
    assert decode_output(None) == ""
    assert decode_output("中文 ok".encode("utf-8")) == "中文 ok"
    if os.name != "nt":
        pytest.skip("the OEM code page fallback is Windows-only")
    msg = "'rg' 不是内部或外部命令"
    assert decode_output(msg.encode("oem")) == msg


def test_codelet_shell_overrides_the_choice(monkeypatch):
    monkeypatch.setenv("CODELET_SHELL", "sh")
    assert resolve_shell() == ("sh", None)
    if os.name == "nt":
        monkeypatch.setenv("CODELET_SHELL", "cmd")
        assert resolve_shell() == ("cmd", None)


@needs_pwsh
def test_windows_defaults_to_pwsh(monkeypatch):
    monkeypatch.delenv("CODELET_SHELL", raising=False)
    kind, exe = resolve_shell()
    assert kind == "pwsh" and exe
    assert "PowerShell 7" in BashTool().description


@needs_pwsh
def test_pwsh_chaining_exit_codes_and_utf8(monkeypatch):
    monkeypatch.delenv("CODELET_SHELL", raising=False)
    tool = BashTool()
    ok = tool.execute({"command": "echo one && echo two"})
    assert not ok.is_error and "one" in ok.output and "two" in ok.output
    # A native command's exit code fails the call; its UTF-8 output survives.
    native = tool.execute({"command": 'python -c "import sys; print(chr(20013)); sys.exit(3)"'})
    assert native.is_error and "中" in native.output
    # A cmdlet error fails the call and reads as text, not CLIXML.
    cmdlet = tool.execute({"command": "Get-Item definitely-not-here.txt"})
    assert cmdlet.is_error and "CLIXML" not in cmdlet.output
    # Quotes and $ survive the trip (the command goes over -EncodedCommand).
    env = tool.execute({"command": "$env:FOO = 'a \"b\" c'; python -c \"import os; print(os.environ['FOO'])\""})
    assert not env.is_error and 'a "b" c' in env.output


@needs_pwsh
def test_a_quoted_executable_path_is_called_not_echoed(monkeypatch):
    import sys

    monkeypatch.delenv("CODELET_SHELL", raising=False)
    r = BashTool().execute({"command": f'"{sys.executable}" -c "print(41 + 1)"'})
    assert not r.is_error and r.output.strip() == "42"
