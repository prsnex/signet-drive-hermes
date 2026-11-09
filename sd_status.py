"""The check at every start, and the status line.

Four things make the drive trustworthy for the mirror: the client is genuine,
the agent is enrolled, its key still matches the public log, and the drive
answers. This release runs the second and the fourth, and says plainly that
the first and the third are not yet built, rather than printing a pass it did
not earn. The status line is the one sentence the human sees.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .signet_cli import SignetClient, SignetError

import fcntl
import hashlib
import io
import re
import subprocess
import urllib.request
import zipfile

HASHES_REPO_RAW = "https://raw.githubusercontent.com/prsnex/signet-drive-public-hashes/main"
OUR_TEAM_ID = "F2FY22A25D"            # Developer ID Application: ID Lynx Ltd.
OUR_BUNDLE_ID = "ai.prsnex.signet"
KEY_PENDING_LIMIT_S = 24 * 3600       # our published promise, the 24 h maximum merge delay; past it the human hears it, the mirror runs on (Bob, 11-07)


def _fetch(url: str, limit: int = 64 << 20) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # public files, no credential
        return r.read(limit + 1)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def client_genuine(client: SignetClient, cache: dict, fetch=_fetch, server_url: str = "") -> tuple[Optional[bool], str, dict]:
    """Is the installed client the published, anchored release of its version?

    Local, every start: Apple's signature verifies, the Team ID is ours, the bundle id is ours.
    Once per version, cached: the published manifest from the public hashes repository, the
    zip the server serves hashed equal to the manifest's `zip_sha256`, and the binary inside
    the zip hashed equal to the installed binary. That ties the bytes on this Mac to the
    anchored release. Returns (ok, detail, cache_update).
    """
    path = client.binary_path()
    if path is None:
        return False, "client not found", {}
    installed = _sha256(path.read_bytes())
    app = path
    for parent in path.parents:
        if parent.suffix == ".app":
            app = parent
            break
    # local assurances
    try:
        out = subprocess.run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True, timeout=30).stderr
        team = re.search(r"TeamIdentifier=(\S+)", out)
        ident = re.search(r"Identifier=(\S+)", out)
        ok_sig = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        team = ident = None; ok_sig = False
    if not ok_sig or not team or team.group(1) != OUR_TEAM_ID or not ident or ident.group(1) != OUR_BUNDLE_ID:
        return False, f"signature {'verifies' if ok_sig else 'does not verify'}, team {team.group(1) if team else '?'}, id {ident.group(1) if ident else '?'}", {}
    version = (client.version() or "").split()[1] if len((client.version() or "").split()) > 1 else ""
    if not version:
        return None, "version unreadable", {}
    cached = cache.get("genuine") or {}
    if cached.get("version") == version and cached.get("installed_sha256") == installed and cached.get("ok") is True:
        return True, f"{version}, signed by our Developer ID, byte-equal to the published release (checked {cached.get('at', '')[:10]})", {}
    try:
        manifest_url = f"{HASHES_REPO_RAW}/releases/v{version}/signet-{version}-macos.manifest.json"
        manifest_bytes = fetch(manifest_url)
        manifest = json.loads(manifest_bytes)
        want_zip = manifest.get("zip_sha256", "")
        base = (server_url or "https://drive.mysignet.ca").rstrip("/")
        zip_bytes = fetch(f"{base}/cli/signet-{version}-macos.zip")
        if _sha256(zip_bytes) != want_zip:
            return False, f"the server's zip for {version} does not match the published hash", {}
        inner = None
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for name in z.namelist():
                if name.endswith(".app/Contents/MacOS/signet"):   # the published app carries its version in its name
                    inner = _sha256(z.read(name)); break
        if inner is None:
            return False, "the published zip holds no signet binary", {}
        if inner != installed:
            return False, f"the installed client is not the published {version} binary", {}
        upd = {"genuine": {"version": version, "installed_sha256": installed, "zip_sha256": want_zip,
                           "ok": True, "at": _now().isoformat()}}
        return True, f"{version}, signed by our Developer ID, byte-equal to the published release", upd
    except Exception as e:  # network or shape; say so, never a false pass
        return None, f"{version}, signed by our Developer ID, and the published-release check could not run ({type(e).__name__})", {}


def key_check(client: SignetClient, cache: dict, limit_s: float = KEY_PENDING_LIMIT_S) -> tuple[Optional[bool], str, dict]:
    """The agent's keys against the anchored public log, by the client's own watch.
    0 clean: ok. 62 not yet published: pending, with the time it has been pending.
    60 or 13: a failure, and the mirror stops. 31: unreachable."""
    rc, verdict, code = client.transparency_verify_self()
    if rc == 0 and verdict.get("verdict") == "clean":
        return True, f"matches the public log (epoch {verdict.get('epoch')}, {len(verdict.get('entries', []))} keys)", {"key_pending_since": None}
    if rc == 62:
        waiting = len(verdict.get("held_not_in_published_log", []))
        # The clock runs from the entries' own time when setup recorded the enrolment, else from first sight (Bob, run 1, 5).
        since = cache.get("key_pending_since") or cache.get("enrolled_at") or _now().isoformat()
        try:
            pending_s = (_now() - datetime.fromisoformat(since)).total_seconds()
        except ValueError:
            pending_s = 0
        if pending_s > limit_s:
            # Our publisher is late past the promise we publish. The human hears it; the mirror
            # runs on, since lateness is not evidence of a swapped key (only 60 or 13 is).
            return None, (f"OVERDUE, {waiting} key(s) unpublished for {int(pending_s // 3600)} h, past our {int(limit_s // 3600)} h promise, "
                          "the mirror continues"), {"key_pending_since": since, "key_overdue": True}
        return None, f"{waiting} key(s) not yet in a published root (size {verdict.get('scanned', '?')}), checked again next start", {"key_pending_since": since, "key_overdue": False}
    if rc in (31, 32, 52) or 30 <= rc <= 39:
        return None, f"the public anchor could not be read ({code or rc}), checked again next start", {}
    if rc in (13, 60):
        return False, f"FAILED ({code or rc}), {verdict.get('verdict') or verdict.get('reason') or 'see the client'}", {}
    # Any other answer (arguments, a changed epoch, a malformed response) is not evidence about the key:
    # say it, check again, never stop the mirror on it (Bob, run 1, 6b).
    return None, f"could not be checked (exit {rc}{', ' + code if code else ''}), checked again next start", {}


@dataclass
class Check:
    name: str
    ok: Optional[bool]      # None: not run or not built
    detail: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hhmm(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "?"


def _short_error(err: str) -> str:
    """A reason a human reads; the client's full text stays in the status file (Bob, run 2, 3)."""
    e = str(err)
    if "network_error" in e or "transfer_stalled" in e:
        return "the drive did not answer"
    if "se_locked" in e:
        return "this Mac's keys were unavailable until a human unlocked it"
    if "account_read_only" in e:
        return "the account is read-only"
    if "insufficient_storage" in e:
        return "the drive is full"
    if "not_enrolled" in e:
        return "not enrolled"
    head = e.split(":", 2)
    return head[1].strip() if len(head) > 2 and head[0].startswith("SignetError") else e[:60]


def _is_ahead(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso) > _now()
    except (ValueError, TypeError):
        return False


def _clock(iso: str) -> str:
    """A future instant as local wall-clock time."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "?"


def _when(iso: str) -> str:
    """'11:14' today; otherwise the age, so an old check never reads as fresh (Bob, run 1, case 4)."""
    try:
        t = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return "?"
    age = (_now() - t).total_seconds()
    if age < 20 * 3600 and t.astimezone().date() == _now().astimezone().date():
        return t.astimezone().strftime("%H:%M")
    if age < 48 * 3600:
        return f"{int(age // 3600)} h ago"
    return f"{int(age // 86400)} days ago"


_WRITE_LOCKS: dict = {}
_WRITE_LOCKS_GUARD = threading.Lock()


class Status:
    """State on disk, shared by the hooks, the tools, the command and the dashboard.

    Every writer read-modify-writes the whole file, so the lock is keyed by the file and
    not by the instance (the runtime makes a fresh instance per call, and a per-instance
    lock lost the start check's write to a pass's write on 2026-11-07), with a file lock
    beside it for a second process.
    """

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "status.json"
        with _WRITE_LOCKS_GUARD:
            self._lock = _WRITE_LOCKS.setdefault(str(self.path), threading.Lock())

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def write(self, **changes) -> dict:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    return self._write_locked(changes)
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _write_locked(self, changes: dict) -> dict:
        s = self.read()
        s.update(changes)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, indent=2, sort_keys=True, default=str))
        os.replace(tmp, self.path)
        return s

    # ---- the start check ------------------------------------------------
    def start_check(self, client: SignetClient, mmd_hours: float = 24) -> dict:
        checks: list[Check] = []
        state = "ok"
        version = ""
        try:
            version = client.version()
            checks.append(Check("client", True, version or "present"))
        except SignetError as e:
            checks.append(Check("client", False, e.message))
            state = "needs_you"
        cache = self.read()
        updates: dict = {}
        if state == "ok":
            ok, detail, upd = client_genuine(client, cache, server_url=client.server_url or "")
            updates.update(upd)
            checks.append(Check("client_genuine", ok, detail))
            if ok is False:
                state = "needs_you"
        handle = ""
        if state == "ok" and not client.handle:
            checks.append(Check("enrolled", False, "no handle configured, so setup has not yet been run with a one-time code"))
            state = "needs_you"
        if state == "ok":
            try:
                me = client.whoami()
                handle = str(me.get("handle", ""))
                checks.append(Check("enrolled", True,
                                    f"{handle} under {me.get('guardian') or me.get('guardian_handle', '?')}, sharing {me.get('prsn_sharing_capability', '?')}"))
                cap = str(me.get("prsn_sharing_capability", "?"))
                if cap == "read_write":
                    checks.append(Check("sharing", None, "read-write, so your agent can invite others to write into its folders, and the data label on fetched files is then the only guard. On your account page the choice is under Sharing capability, and Can share, read-only or Can't share with anyone else narrows it."))
                else:
                    checks.append(Check("sharing", True, cap))
                da = me.get("drive_access") or {}
                if da.get("status") == "awaiting_pickup":
                    try:
                        client.connect()
                        da = (client.whoami().get("drive_access") or {})
                    except SignetError as e:
                        da = {"status": f"awaiting_pickup ({e.code})"}
                if da.get("status") == "authorized":
                    checks.append(Check("drive_access", True, f"authorized, and your human re-confirms it every {da.get('reconfirm_interval_days', '?')} days"))
                else:
                    checks.append(Check("drive_access", False, f"{da.get('status', 'unknown')}, and your human re-confirms the agent's access on their account page"))
                    state = "needs_you"
            except SignetError as e:
                if e.locked:
                    checks.append(Check("drive", False, "this Mac's keys are unavailable until a human unlocks it, and copies resume then"))
                    state = "behind"
                elif e.temporary:
                    checks.append(Check("drive", False, f"not reachable ({e.code}), enrolment unchecked"))
                    state = "behind"
                else:
                    checks.append(Check("enrolled", False, f"{e.code}: {e.message}"))
                    state = "needs_you"
        if state == "ok":
            ok, detail, upd = key_check(client, cache, limit_s=float(mmd_hours) * 3600)
            updates.update(upd)
            checks.append(Check("key_matches_log", ok, detail))
            if ok is False or upd.get("key_overdue"):
                state = "needs_you"           # a failure stops the mirror too; overdue only tells the human
        # The drive check runs under an overdue key too, since the mirror runs on (Bob, run 2, 3).
        if state == "ok" or (state == "needs_you" and updates.get("key_overdue") and not any(c.ok is False for c in checks)):
            try:
                q = client.quota()
                checks.append(Check("drive", True, f"{int(q.get('bytes_available', 0)) // (1 << 20)} MB free"))
            except SignetError as e:
                if e.read_only:
                    checks.append(Check("drive", False, "Account read-only, copies not being saved"))
                    state = "needs_you"
                elif e.locked:
                    checks.append(Check("drive", False, "this Mac's keys are unavailable until a human unlocks it, and copies resume then"))
                    state = "behind"
                elif e.temporary:
                    checks.append(Check("drive", False, f"not reachable ({e.code})"))
                    state = "behind"
                else:
                    checks.append(Check("drive", False, f"{e.code}: {e.message}"))
                    state = "needs_you"
        failed = {c.name for c in checks if c.ok is False}
        result = {"at": _now().isoformat(), "state": state, "handle": handle,
                  "client_version": version, "checks": [asdict(c) for c in checks],
                  "key_failed": "key_matches_log" in failed,
                  # the mirror runs unless something that makes the drive untrustworthy or unreachable failed;
                  # an overdue key is told, not acted on (Bob, run 1, 6a)
                  "mirror_may_run": not (failed & {"client", "client_genuine", "enrolled", "drive_access", "key_matches_log", "drive"})}
        self.write(last_check=result, **updates)
        return result

    def key_failed(self) -> bool:
        return bool((self.read().get("last_check") or {}).get("key_failed"))

    def mirror_may_run(self) -> bool:
        c = self.read().get("last_check") or {}
        return bool(c.get("mirror_may_run", True))

    def record_pass(self, result) -> None:
        self.write(last_pass={"at": _now().isoformat(), "status": result.status, "stamp": result.stamp,
                              "uploaded": len(result.uploaded), "missing": result.missing,
                              "line": result.line()})

    # ---- the line and the summary -------------------------------------
    def summary(self) -> dict:
        s = self.read()
        check = s.get("last_check") or {}
        last = s.get("last_pass") or {}
        state = "unknown"
        if check:
            state = check.get("state", "unknown")
        if last.get("status") in ("needs_you", "refused"):
            state = "needs_you"
        elif last.get("status") == "behind" and state == "ok":
            state = "behind"
        err = s.get("last_error")
        if err and (not last or str(s.get("last_error_at", "")) > str(last.get("at", ""))) and state == "ok":
            state = "behind"
        word = {"ok": "Drive in step", "behind": "Drive behind", "needs_you": "Drive needs you",
                "unknown": "Drive not checked"}[state]
        return {"state": state, "word": word, "line": self.line(s), "last_check": check, "last_pass": last,
                "config": {k: v for k, v in (s.get("config") or {}).items() if k != "handle"}}

    def line(self, s: Optional[dict] = None) -> str:
        s = s if s is not None else self.read()
        check = s.get("last_check") or {}
        last = s.get("last_pass") or {}
        if not check:
            return "Not checked yet."
        parts = [f"Checked {_when(check.get('at', ''))}."]
        by = {c["name"]: c for c in check.get("checks", [])}
        g = by.get("client_genuine", {})
        if g.get("ok") is True:
            parts.append("Client genuine,")
        elif g.get("ok") is False:
            parts.append(f"CLIENT NOT GENUINE ({g.get('detail')}).")
        elif by.get("client", {}).get("ok"):
            parts.append(f"Client {by['client']['detail']} ({g.get('detail', 'genuineness unchecked')}),")
        else:
            parts.append(f"Client missing ({by.get('client', {}).get('detail', '?')}).")
        if by.get("enrolled", {}).get("ok"):
            parts.append(f"enrolled as {check.get('handle') or '?'},")
        elif "enrolled" in by:
            parts.append(f"not enrolled ({by['enrolled']['detail']}),")
        if "drive_access" in by and not by["drive_access"].get("ok"):
            parts.append(f"access {by['drive_access']['detail']}.")
        if by.get("drive", {}).get("ok"):
            parts.append("drive reachable.")
        elif "drive" in by:
            parts.append(f"drive {by['drive']['detail']}.")
        k = by.get("key_matches_log", {})
        if k.get("ok") is True:
            parts.append("Key matches the public log.")
        elif k.get("ok") is False:
            parts.append(f"KEY CHECK FAILED ({k.get('detail')}). The mirror is stopped until you act.")
        elif k and str(k.get("detail", "")).startswith("OVERDUE"):
            parts.append(f"Key {k.get('detail')}.")
        elif k:
            parts.append(f"Key pending, {k.get('detail')}.")
        # the drive segment comes after the key in every state, so an overdue key never hides it (Bob, run 2, 2)
        if by.get("drive", {}).get("ok") and not any(x.startswith("drive") for x in parts):
            parts.append("Drive reachable.")
        if last:
            parts.append(f"Last mirror {_when(last.get('at', ''))}, {last.get('line', '')}")
        due = s.get("settle_due")
        if due:
            # The clock lives in one process. If that process is gone, the due time passes with
            # nothing saved and the start pass saves the change (Bob, case 35, 2026-11-08).
            if _is_ahead(due):
                parts.append(f"A copy is due at {_clock(due)}.")
            else:
                parts.append("A change waits for the next session start to be saved.")
        err = s.get("last_error")
        if err and (not last or str(s.get("last_error_at", "")) > str(last.get("at", ""))):
            parts.append(f"Last attempt failed, {_short_error(err)}.")
        if parts and parts[-1].endswith(","):   # a part written to continue, with nothing after it (the not-enrolled case)
            parts[-1] = parts[-1][:-1] + "."
        return " ".join(parts)
