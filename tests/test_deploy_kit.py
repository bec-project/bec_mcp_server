"""Keep the deployment kit in deploy/ in step with the server it deploys.

The kit is mostly configuration and prose, so nothing else notices when a flag is renamed or an
environment variable is dropped. These tests read the kit's files and compare them with the code.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
PACKAGE = REPO / "bec_mcp_server"

UNIT = DEPLOY / "host" / "bec-mcp.service"
ENV_FILE = DEPLOY / "host" / "bec-mcp.env"
JSON_FILES = [
    DEPLOY / "clients" / "claude-mcp.json",
    DEPLOY / "clients" / "qwen-settings.json",
    DEPLOY / "agent" / "claude-settings.json",
]
DOCS = [DEPLOY / "README.md", DEPLOY / "clients" / "README.md"]


def _server_help() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "bec_mcp_server", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return result.stdout


def _package_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.rglob("*.py"))


def _server_command_flags() -> set[str]:
    """Flags given to bec-mcp-server in the unit and in the guides' command examples."""
    texts = [line for line in UNIT.read_text(encoding="utf-8").splitlines() if "ExecStart=" in line]
    for doc in DOCS:
        # join continuation lines so a multi-line command is one string
        joined = doc.read_text(encoding="utf-8").replace("\\\n", " ")
        texts += [line for line in joined.splitlines() if "bec-mcp-server " in line]
    flags = set()
    for text in texts:
        command = text.split("bec-mcp-server", 1)[1]
        flags.update(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", command))
    return flags


def test_guides_and_unit_use_only_flags_the_server_accepts():
    flags = _server_command_flags()
    assert "--token-file" in flags and "--allowed-hosts" in flags  # the parser found the commands
    help_text = _server_help()
    unknown = sorted(flag for flag in flags if flag not in help_text)
    assert not unknown, f"flags not in bec-mcp-server --help: {unknown}"


def test_environment_variables_in_the_kit_are_read_by_the_server():
    names = set()
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            names.add(line.split("=", 1)[0])
    names.update(re.findall(r"^Environment=(BEC_[A-Z_]+)=", UNIT.read_text(), flags=re.M))
    source = _package_source()
    unread = sorted(name for name in names if f'"{name}"' not in source)
    assert names and not unread, f"set by the kit but never read: {unread}"


def test_unit_paths_agree_with_the_guide():
    unit = UNIT.read_text(encoding="utf-8")
    guide = (DEPLOY / "README.md").read_text(encoding="utf-8")
    secrets = re.findall(r"^Environment=BEC_MCP_\w+_FILE=(\S+)$", unit, flags=re.M)
    assert len(secrets) == 2
    for secret in secrets:
        assert f"> {secret}" in guide, f"the guide never creates {secret}"
    assert "/opt/bec-mcp/venv/bin/bec-mcp-server" in unit
    assert "/opt/bec-mcp/venv" in guide


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda path: path.name)
def test_client_files_are_valid_json_without_a_token(path):
    text = path.read_text(encoding="utf-8")
    json.loads(text)
    for header in re.findall(r'"Authorization":\s*"([^"]*)"', text):
        assert header in ("Bearer ${BEC_MCP_TOKEN}", "Bearer $BEC_MCP_TOKEN")
