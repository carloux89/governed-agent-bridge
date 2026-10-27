"""Bridge transport for the live Spark planner.

The Meta Model API credential lives ONLY on the user's PC, in the Muse
configuration (``%USERPROFILE%\\.config\\muse\\auth.json``,
``providers.meta.api_key`` / ``api_base_url``). It is never copied to this
machine, never printed and never logged.

This transport therefore delegates the actual API call to the PC through
a bridge CLI (``bridge.py ssh <host>``): PowerShell runs ON the PC, reads
the key locally, POSTs the chat completions payload to the Meta Model API
and writes back ONLY the model's reply text.

Chunked deposit protocol — why: a single ``-EncodedCommand`` carrying
script + payload blows the Windows command-line length limit (the remote
command line is capped well under the 32 767 chars of CreateProcess —
the first live attempt failed with "La ligne de commande est trop
longue." at ~10.4k encoded chars). So the payload never rides inside a
command line:

1. The payload JSON is base64-encoded (UTF-8) here and deposited into a
   unique temp file on the PC (``%TEMP%\\gab_payload_<token>.b64``) via
   several SHORT bridge calls: the first chunk with
   ``[IO.File]::WriteAllText``, the rest with ``AppendAllText``. Chunks
   are 2000 base64 chars, so every encoded command stays ~5.7k chars —
   far below the limit.
2. ONE compact runner call, whose PowerShell contains NO payload: it
   reads the temp file, decodes it, reads the credential from the local
   Muse config, POSTs to ``/chat/completions``, prints the assistant's
   reply as BASE64 of its UTF-8 bytes, and deletes the temp file (in a
   ``finally``).

The reply channel is deliberately BYTES, not console text. Windows
PowerShell 5.1 decodes an HTTP response body with the legacy ANSI code
page when the response declares no charset, and console output can be
re-encoded the same way on its way back — in the first live runs a
French justification arrived double-encoded ("RÃ©dige" for
"Rédige"): UTF-8 bytes read as cp1252, then re-encoded as UTF-8. The
runner therefore moves bytes end to end (raw response bytes decoded
explicitly as UTF-8 on the PC; the reply returned as base64), and a
guarded repair at reception (see :func:`repair_mojibake`) undoes any
residual double-encoding BEFORE the plan is validated or journalised.
The audit log only ever stores the repaired text — the repair happens
at reception, never by rewriting a journal after the fact.

The key is used in the Authorization header and nowhere else — it is
never written to any stream. PC hygiene: temp files only, always cleaned
up (a best-effort delete also runs here if the sequence aborts); no
scheduled tasks, no persistent script installs on the PC.

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
import re
import subprocess
import sys
import uuid
from pathlib import Path

ENV_BRIDGE_HOST = "GAB_BRIDGE_HOST"
ENV_BRIDGE_PY = "GAB_BRIDGE_PY"
DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_ERROR_CHARS = 300

# Base64 chars of payload per deposit call. The chunk rides inside a
# -EncodedCommand (UTF-16LE -> base64 inflates ~2.7x), so 2000 chars of
# chunk keep the whole encoded command around ~5.7k chars, far below the
# Windows command-line limit that killed the single-shot protocol.
CHUNK_B64_CHARS = 2000

_TEMP_PREFIX = "gab_payload_"
_TEMP_SUFFIX = ".b64"

# Deposit snippets executed on the PC. ``__FNAME__`` is the temp file
# name (unique per call, hex token only — safe in a quoted literal) and
# ``__CHUNK__`` one base64 slice (alphabet [A-Za-z0-9+/=], quote-safe).
_PS_DEPOSIT_FIRST = """\
$ErrorActionPreference = 'Stop'
[IO.File]::WriteAllText((Join-Path $env:TEMP '__FNAME__'), '__CHUNK__')
"""

_PS_DEPOSIT_APPEND = """\
$ErrorActionPreference = 'Stop'
[IO.File]::AppendAllText((Join-Path $env:TEMP '__FNAME__'), '__CHUNK__')
"""

_PS_CLEANUP = """\
Remove-Item (Join-Path $env:TEMP '__FNAME__') -ErrorAction SilentlyContinue
"""

# The runner snippet executed on the PC. It contains NO payload: it reads
# the deposited temp file, decodes the chat-completions body, reads the
# credential from the local Muse config, calls the API, and emits ONLY
# the model's reply — base64 of its UTF-8 bytes — on stdout; failures go
# to stderr with a stable prefix and a non-zero exit code. The temp file
# is deleted in the finally. The POST deliberately goes through
# Net.WebClient so the response comes back as raw bytes that this
# snippet decodes as UTF-8 itself: Invoke-RestMethod on Windows
# PowerShell 5.1 decodes a charset-less JSON body with the ANSI code
# page, which is exactly how the accents were mangled in the first runs.
_PS_RUNNER = """\
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$p = Join-Path $env:TEMP '__FNAME__'
try {
  $authPath = Join-Path $env:USERPROFILE '.config\\muse\\auth.json'
  $cfg = Get-Content -Raw $authPath | ConvertFrom-Json
  $key = $cfg.providers.meta.api_key
  $base = $cfg.providers.meta.api_base_url
  if (-not $base) { $base = 'https://api.meta.ai/v1' }
  if (-not $key) { throw 'no Meta Model API credential in the Muse auth config' }
  $b64 = [IO.File]::ReadAllText($p)
  $body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
  $wc = New-Object Net.WebClient
  $wc.Headers.Add('Authorization', ('Bearer ' + $key))
  $wc.Headers.Add('Content-Type', 'application/json')
  $respBytes = $wc.UploadData(($base.TrimEnd('/') + '/chat/completions'), [Text.Encoding]::UTF8.GetBytes($body))
  $resp = ([Text.Encoding]::UTF8.GetString($respBytes)) | ConvertFrom-Json
  $text = $resp.choices[0].message.content
  [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text)))
} catch {
  [Console]::Error.Write('SPARK_BRIDGE_ERROR: ' + $_.Exception.Message)
  exit 1
} finally {
  Remove-Item $p -ErrorAction SilentlyContinue
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


# Markers of UTF-8 bytes that were decoded as cp1252 somewhere along the
# way: "Ã" followed by a Latin-1 character (é -> "Ã©", è -> "Ã¨",
# clés -> "clÃ©s") and the "â€" prefix of mangled punctuation
# (a curly quote's E2 80 99 -> "â€™").
_MOJIBAKE_HINT = re.compile(r"Ã[\u0080-\u00ff]|â€")


def repair_mojibake(text: str) -> str:
    """Undo one UTF-8-decoded-as-cp1252 round trip, conservatively.

    Applied at RECEPTION, before the plan is validated or anything is
    journalised — never as a post-hoc rewrite of an audit log. The
    repair fires ONLY when mojibake markers are present AND the
    cp1252 -> UTF-8 round trip succeeds AND it strictly reduces the
    marker count. Clean text — including text that legitimately
    contains accented characters — is returned untouched.
    """
    markers = len(_MOJIBAKE_HINT.findall(text))
    if markers == 0:
        return text
    try:
        repaired = text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if len(_MOJIBAKE_HINT.findall(repaired)) < markers:
        return repaired
    return text


def _decode_reply(stdout: bytes) -> str:
    """Decode the runner's reply channel: base64 of UTF-8 bytes.

    The runner emits ONLY base64 (pure ASCII), so no console code page
    between the PC and this process can corrupt the content in
    transit. Anything that is not valid base64-of-UTF-8 is a transport
    failure — fail closed, the planner will record the fallback.
    """
    channel = _decode_console(stdout or b"").lstrip("\ufeff")
    compact = "".join(channel.split())
    try:
        raw = base64.b64decode(compact, validate=True)
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SparkTransportError(
            "reply channel decode failed (expected base64 of UTF-8): "
            f"{type(exc).__name__}"
        ) from exc


def _sanitize_error(text: str) -> str:
    return " ".join(text.split())[:_MAX_ERROR_CHARS]


def _encode_command(script: str) -> str:
    """Encode a PowerShell script as a -EncodedCommand argument."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _default_runner(argv: list[str], timeout: float):
    return subprocess.run(argv, capture_output=True, timeout=timeout)


class BridgeSparkTransport:
    """Callable transport: chat payload dict -> the model's reply text.

    One call = N short deposit calls (payload chunks into a PC temp
    file) + one payload-free runner call (PC-side API POST). See the
    module docstring for the protocol and its rationale.
    """

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

    # -- argv builders ---------------------------------------------------
    def _argv(self, script: str) -> list[str]:
        return [
            sys.executable, str(self.bridge_path), "ssh", self.target, "--",
            "powershell", "-NoProfile", "-NonInteractive",
            "-EncodedCommand", _encode_command(script),
        ]

    @staticmethod
    def _temp_name(token: str) -> str:
        return f"{_TEMP_PREFIX}{token}{_TEMP_SUFFIX}"

    def build_deposit_argvs(self, payload: dict, token: str) -> list[list[str]]:
        """One short bridge argv per payload chunk, in deposit order."""
        body_b64 = base64.b64encode(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        chunks = [
            body_b64[i:i + CHUNK_B64_CHARS]
            for i in range(0, len(body_b64), CHUNK_B64_CHARS)
        ] or [""]
        fname = self._temp_name(token)
        argvs = []
        for index, chunk in enumerate(chunks):
            template = _PS_DEPOSIT_FIRST if index == 0 else _PS_DEPOSIT_APPEND
            script = template.replace("__FNAME__", fname) \
                             .replace("__CHUNK__", chunk)
            argvs.append(self._argv(script))
        return argvs

    def build_runner_argv(self, token: str) -> list[str]:
        """The single payload-free runner call for a deposited payload."""
        script = _PS_RUNNER.replace("__FNAME__", self._temp_name(token))
        return self._argv(script)

    def build_cleanup_argv(self, token: str) -> list[str]:
        """Best-effort temp-file delete, used when a sequence aborts."""
        script = _PS_CLEANUP.replace("__FNAME__", self._temp_name(token))
        return self._argv(script)

    # -- execution -------------------------------------------------------
    def _invoke(self, argv: list[str]):
        """Run one bridge call; any failure -> SparkTransportError."""
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
        return proc

    def _cleanup(self, token: str) -> None:
        """Best-effort temp-file removal; never raises."""
        try:
            self.runner(self.build_cleanup_argv(token),
                        min(self.timeout, 30.0))
        except Exception:
            pass

    def __call__(self, payload: dict) -> str:
        token = uuid.uuid4().hex
        try:
            for argv in self.build_deposit_argvs(payload, token):
                self._invoke(argv)
            proc = self._invoke(self.build_runner_argv(token))
        except SparkTransportError:
            # The runner deletes the temp file itself when it runs; if
            # the sequence aborted before/inside it, sweep from here.
            self._cleanup(token)
            raise
        text = repair_mojibake(_decode_reply(proc.stdout or b"")).strip()
        if not text:
            raise SparkTransportError("empty reply from the Spark bridge call")
        return text
