"""Bridge transport for the live Spark planner.

The Meta Model API credential lives ONLY on the user's PC, in the Muse
configuration (``%USERPROFILE%\\.config\\muse\\auth.json``,
``providers.meta.api_key`` / ``api_base_url``). It is never copied to this
machine, never printed and never logged.

This transport therefore delegates the actual API call to the PC through
a bridge CLI (``bridge.py ssh <host>``): a small
PowerShell snippet runs ON the PC, reads the key locally, POSTs the chat
completions payload to the Meta Model API and writes back ONLY the model's
reply text. The payload travels base64-encoded inside the
``-EncodedCommand`` script (UTF-16LE, the bridge's established encoding);
the reply is decoded as UTF-8 with a cp1252 fallback (the PC console's
native code page).

The transport is a plain callable ``payload -> str`` so tests can inject
a fake; the subprocess runner is injectable for the same reason.

Configuration — deliberately NO machine-specific default is baked in:

- ``bridge_path`` argument, else the ``GAB_BRIDGE_PY`` environment
  variable: the path of the bridge CLI script.
- ``target`` argument, else the ``GAB_BRIDGE_HOST`` environment
  variable: the bridge target (SSH host) of the PC.

Constructor arguments win over the environment. If either value is
missing, constructing the transport raises :class:`SparkTransportError`
with a configuration message — the live transport is never silently
pointed at an assumed machine.

Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

ENV_BRIDGE_HOST = "GAB_BRIDGE_HOST"
ENV_BRIDGE_PY = "GAB_BRIDGE_PY"
DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_ERROR_CHARS = 300

# The PowerShell snippet executed on the PC. ``__BODY_B64__`` is replaced
# with the base64 (UTF-8) chat-completions payload. The snippet reads the
# credential from the local Muse config, calls the API, and emits ONLY
# the model's text on stdout; failures go to stderr with a stable prefix
# and a non-zero exit code. The key is used in the Authorization header
# and nowhere else — it is never written to any stream.
_PS_SNIPPET = """\
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
try {
  $authPath = Join-Path $env:USERPROFILE '.config\\muse\\auth.json'
  $cfg = Get-Content -Raw $authPath | ConvertFrom-Json
  $key = $cfg.providers.meta.api_key
  $base = $cfg.providers.meta.api_base_url
  if (-not $base) { $base = 'https://api.meta.ai/v1' }
  if (-not $key) { throw 'no Meta Model API credential in the Muse auth config' }
  $body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__BODY_B64__'))
  $resp = Invoke-RestMethod -Method Post -Uri ($base.TrimEnd('/') + '/chat/completions') -Headers @{ Authorization = ('Bearer ' + $key) } -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
  $text = $resp.choices[0].message.content
  [Console]::Out.Write($text)
} catch {
  [Console]::Error.Write('SPARK_BRIDGE_ERROR: ' + $_.Exception.Message)
  exit 1
}
"""


class SparkTransportError(RuntimeError):
    """The bridge/API round trip failed; the message is safe to log."""


def _resolve_bridge_path(bridge_path) -> Path:
    """Resolve the bridge CLI path: argument, else ``GAB_BRIDGE_PY``.

    There is deliberately no built-in default: the bridge script lives
    wherever the operator installed it, and deriving a location from
    this file's position would point the transport at an assumed
    workspace. Missing configuration is a construction-time error.
    """
    if bridge_path is not None:
        return Path(bridge_path)
    override = os.environ.get(ENV_BRIDGE_PY)
    if override:
        return Path(override)
    raise SparkTransportError(
        "bridge CLI path not configured: pass bridge_path=... to "
        "BridgeSparkTransport or set the GAB_BRIDGE_PY environment "
        "variable to the path of the bridge CLI script"
    )


def _resolve_target(target) -> str:
    """Resolve the bridge host: argument, else ``GAB_BRIDGE_HOST``."""
    if target:
        return target
    override = os.environ.get(ENV_BRIDGE_HOST)
    if override:
        return override
    raise SparkTransportError(
        "bridge host not configured: pass target=... to "
        "BridgeSparkTransport or set the GAB_BRIDGE_HOST environment "
        "variable to the bridge host of the PC"
    )


def _decode_console(data: bytes) -> str:
    """Decode PC console output: UTF-8 if possible, else cp1252."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _sanitize_error(text: str) -> str:
    return " ".join(text.split())[:_MAX_ERROR_CHARS]


def _default_runner(argv: list[str], timeout: float):
    return subprocess.run(argv, capture_output=True, timeout=timeout)


class BridgeSparkTransport:
    """Callable transport: chat payload dict -> the model's reply text."""

    def __init__(
        self,
        bridge_path: str | Path | None = None,
        target: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        runner=None,
    ):
        self.bridge_path = _resolve_bridge_path(bridge_path)
        self.target = _resolve_target(target)
        self.timeout = timeout
        self.runner = runner if runner is not None else _default_runner

    def build_argv(self, payload: dict) -> list[str]:
        """Build the bridge CLI argv for one chat-completions payload."""
        body_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        script = _PS_SNIPPET.replace("__BODY_B64__", body_b64)
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [
            sys.executable, str(self.bridge_path), "ssh", self.target, "--",
            "powershell", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", encoded,
        ]

    def __call__(self, payload: dict) -> str:
        argv = self.build_argv(payload)
        try:
            proc = self.runner(argv, self.timeout)
        except Exception as exc:  # timeout, missing bridge, ssh failure...
            raise SparkTransportError(
                f"bridge transport could not run: {type(exc).__name__}: "
                f"{_sanitize_error(str(exc))}"
            ) from exc
        if proc.returncode != 0:
            detail = _sanitize_error(_decode_console(proc.stderr or b""))
            raise SparkTransportError(
                f"bridge call failed (exit {proc.returncode})"
                + (f": {detail}" if detail else "")
            )
        text = _decode_console(proc.stdout or b"").lstrip("\ufeff").strip()
        if not text:
            raise SparkTransportError("empty reply from the Spark bridge call")
        return text
