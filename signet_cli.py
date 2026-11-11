"""A thin wrapper around the `signet` command-line client.

The plugin never speaks to the server itself. Every drive operation is one
run of the `signet` binary, read back through its machine-readable output
(`--json` for lists, `--json-errors` for failures) and its documented exit
codes. Nothing secret is passed on the command line; keys stay in the
client's keystore and are selected by label.

Exit codes used here (the public CLI specification, "Exit codes"):
  2   invalid arguments, including a path that resolves to more than one entry
  10  authentication_failed      17  authorization_denied (a capability or an
                                     account in read-only grace)
  18  insufficient_storage       26  not_found            27  conflict
  31  network_error              32  transfer_stalled (re-run to resume)
  52  se_locked (the Mac is locked; temporary)
  60  transparency violation     61  identity_mismatch    62  unanchored
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

TEMPORARY_EXITS = {31, 32, 52}          # retry at the next trigger, nothing lost

# What a child process may inherit. The Hermes process holds provider keys and other secrets
# in its environment, and neither the Signet app nor codesign needs any of them. The app reads
# HOME, XDG_CONFIG_HOME and SIGNET_* and nothing else (its source at v1.2.0), and starts
# `launchctl` and `id` by bare name, so PATH must reach /usr/bin and /bin.
_ENV_PASS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "XDG_CONFIG_HOME")
_ENV_PASS_PREFIXES = ("LC_", "SIGNET_")
_PATH_NEEDS = ("/usr/bin", "/bin")


def minimal_env(extra: Optional[dict] = None, source: Optional[dict] = None) -> dict:
    """The environment for a child process: a short fixed list of names from this process,
    then the caller's own additions. Nothing else is inherited."""
    src = os.environ if source is None else source
    env = {k: v for k, v in src.items() if k in _ENV_PASS or k.startswith(_ENV_PASS_PREFIXES)}
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    parts += [p for p in _PATH_NEEDS if p not in parts]
    env["PATH"] = os.pathsep.join(parts)
    env.setdefault("HOME", str(Path.home()))
    env.update(extra or {})
    return env
REFUSED_EXITS = {10, 14, 15, 16, 17, 18, 60, 61, 62}


class SignetError(Exception):
    """One failed run of the client, with its exit code and machine code."""

    def __init__(self, exit_code: int, code: str, message: str, argv: list[str]):
        super().__init__(f"signet exit {exit_code} {code}: {message}")
        self.exit_code = exit_code
        self.code = code
        self.message = message
        self.argv = argv

    @property
    def temporary(self) -> bool:
        return self.exit_code in TEMPORARY_EXITS

    @property
    def locked(self) -> bool:
        return self.exit_code == 52

    @property
    def read_only(self) -> bool:
        return self.code == "account_read_only"

    @property
    def quota(self) -> bool:
        return self.exit_code == 18


@dataclass
class SignetClient:
    """Runs `signet` with a minimal, fixed environment and parses what comes back."""

    binary: str = "signet"
    server_url: Optional[str] = None
    handle: Optional[str] = None          # SIGNET_HANDLE, the harness-asserted identity
    key: Optional[str] = None             # --key label; the configured default if None
    kem_key: Optional[str] = None         # --kem-key label; the configured default if None
    timeout_s: int = 600                  # uploads and downloads
    read_timeout_s: int = 60              # reads and checks; a hung network must not hold a hook (Bob, run 1, 7)
    extra_env: dict = field(default_factory=dict)

    # ---- plumbing -------------------------------------------------------
    def _env(self) -> dict:
        env = minimal_env(self.extra_env)
        if self.handle:
            env["SIGNET_HANDLE"] = self.handle
        return env

    def _base(self) -> list[str]:
        argv = [self.binary]
        return argv

    def _common(self, crypto: bool) -> list[str]:
        argv: list[str] = []
        if self.server_url:
            argv += ["--server-url", self.server_url]
        if self.key:
            argv += ["--key", self.key]
        if crypto and self.kem_key:
            argv += ["--kem-key", self.kem_key]
        return argv

    def run(self, args: list[str], crypto: bool = True, parse_json: bool = True, long: bool = False) -> Any:
        """Run one command. Returns parsed JSON (or raw stdout) or raises SignetError."""
        argv = self._base() + args + self._common(crypto) + ["--json-errors"]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, env=self._env(),
                timeout=self.timeout_s if long else self.read_timeout_s, stdin=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError:
            raise SignetError(127, "client_missing", f"{self.binary} is not installed", argv)
        except subprocess.TimeoutExpired:
            raise SignetError(31, "network_error", "the client did not answer in time", argv)
        if proc.returncode != 0:
            raise SignetError(proc.returncode, *_error_fields(proc.stderr), argv)
        if not parse_json:
            return proc.stdout
        text = proc.stdout.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def run_raw(self, args: list[str], crypto: bool = False) -> tuple[int, str, str]:
        """Run one command and return (exit, stdout, stderr) without raising, for commands
        whose non-zero exits carry a verdict (transparency-verify)."""
        argv = self._base() + args + self._common(crypto) + ["--json-errors"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, env=self._env(),
                                  timeout=self.read_timeout_s, stdin=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            return 127, "", json.dumps({"error": {"code": "client_missing", "message": f"{self.binary} is not installed"}})
        except subprocess.TimeoutExpired:
            return 31, "", json.dumps({"error": {"code": "network_error", "message": "the client did not answer in time"}})
        return proc.returncode, proc.stdout, proc.stderr

    def transparency_verify_self(self) -> tuple[int, dict, str]:
        """The owner's watch: this account's keys against the anchored public log.
        Returns (exit, the JSON verdict or {}, the error code or '')."""
        rc, out, err = self.run_raw(["transparency-verify", "--self"])
        try:
            verdict = json.loads(out.strip()) if out.strip() else {}
        except json.JSONDecodeError:
            verdict = {}
        code = _error_fields(err)[0] if rc != 0 else ""
        return rc, verdict, code

    def binary_path(self) -> Optional[Path]:
        """The executable itself, symlinks resolved."""
        found = self.resolved_binary()
        return Path(found).resolve() if found else None

    # ---- account reads --------------------------------------------------
    def version(self) -> str:
        out = self.run(["version"], crypto=False, parse_json=False)
        return (out or "").strip()

    def whoami(self) -> dict:
        return self.run(["whoami"], crypto=False) or {}

    def quota(self) -> dict:
        return self.run(["quota"], crypto=False) or {}

    def connect(self) -> dict:
        """Pick up the agent's Drive access by signed pickup, once the Guardian has authorized it."""
        return self.run(["connect"], crypto=False) or {}

    # ---- shares and folders ---------------------------------------------
    def share_list(self) -> list[dict]:
        return _rows(self.run(["share", "list", "--json"]))

    def share_create(self, name: str) -> dict:
        return self.run(["share", "create", name]) or {}

    def folder_list(self, path: Optional[str]) -> list[dict]:
        args = ["folder", "list"] + ([path] if path else []) + ["--json"]
        return _rows(self.run(args))

    def folder_create(self, path: str) -> dict:
        return self.run(["folder", "create", path]) or {}

    # ---- files ----------------------------------------------------------
    def file_list(self, folder_path: str) -> list[dict]:
        return _rows(self.run(["file", "list", folder_path, "--json"]))

    def file_upload(self, dest_path: str, local: Path) -> dict:
        return self.run(["file", "upload", dest_path, "--in", str(local)], long=True) or {}

    def file_download(self, path: str, out: Path) -> None:
        self.run(["file", "download", path, "--out", str(out)], parse_json=False, long=True)

    def file_delete(self, paths: list[str]) -> dict:
        """Permanent (there is no trash). Used by the Memory Mirror's ceiling only, on its own
        retired points by exact name, never by the file tools."""
        return self.run(["file", "delete", *paths, "--yes"]) or {}

    # ---- the binary itself ---------------------------------------------
    def resolved_binary(self) -> Optional[str]:
        return shutil.which(self.binary) if os.sep not in self.binary else (
            self.binary if os.path.exists(self.binary) else None)


def _error_fields(stderr: str) -> tuple[str, str]:
    """(code, message) from `--json-errors` output, with a plain-text fallback."""
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            err = json.loads(line).get("error") or {}
            return str(err.get("code") or "error"), str(err.get("message") or line)
        except json.JSONDecodeError:
            continue
    text = stderr.strip().splitlines()[-1] if stderr.strip() else "no message"
    if text.startswith("signet error:"):
        rest = text[len("signet error:"):].strip()
        code, _, msg = rest.partition(":")
        return code.strip() or "error", msg.strip() or rest
    return "error", text


def _rows(value: Any) -> list[dict]:
    if value is None:
        return []
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    if isinstance(value, dict):
        for key in ("items", "rows", "files", "folders", "shares"):
            if isinstance(value.get(key), list):
                return [r for r in value[key] if isinstance(r, dict)]
    return []
