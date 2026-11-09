"""The Memory Mirror: dated restore points of an agent's memory files and its
own skills in its Signet Drive, and a restore for the same Mac.

The drive holds one share folder, by default "Memory Mirror", and in it only
files, one per restore point:

  memory-<stamp>.tar.gz    everything the mirror tracks at that moment (the two
                           memory files, the agent's own skills) and a
                           manifest.json naming every member's sha256

A point is a single upload, so an interrupted one leaves no file and can never
be chosen. There are no folders under the root, so a human looking at the
drive sees a short list of dated files and nothing else (Chris, 2026-11-07).

Fewer points. A pass runs at session start, after each memory or skill write
and at session end, but a new point is made only when something changed and
either the session is ending, the human asked, or the newest point is older
than the interval (one hour by default). Writes inside a session coalesce.

A ceiling (Chris, 2026-11-07): one file per day, nine days back, the newest
never removed. Within a day the newer point replaces the older, so the folder
never shows two files for one day; a point older than `keep_days` (9) is
removed; the newest point is kept whatever its age, so an idle agent keeps its
last point. Ten files at most, with a point every day. A point made before a
restore is the restore's undo, named `-before-restore`, and stands outside the
one-per-day rule for the same `keep_days`, else a restore followed by the day's
next point would erase the agent's newest memory from the drive as well as the
Mac (Bob, 2026-11-07). The mirror removes only
points it made itself, by exact name, never a file the human has renamed (so a
name carrying "pinned", or any other name, is kept forever and counted in the
line). The plugin prunes (Chris: "Yes, plugin prunes"); with the setting off,
the line says how many retired points wait.

A point is written to the local index as pending before its upload and
confirmed after, so a pass killed mid-upload whose client child finishes on its
own leaves a point the next pass adopts into the index and the ceiling then
governs (Bob, run 3, case 10). A stale build temp is swept at the next pass.

If a file the newest point held is missing locally, no point is made and the
pass reports the missing files: that is a question for the human (restore, or
confirm the removal), not a decision the mirror takes on its own.

A restore downloads one point, checks every member's sha256 against the
manifest inside it and, for a point this Mac made, the whole file against the
hash recorded at upload, and only then writes the files. Before it writes
anything it makes a point of its own, so the state being replaced is itself a
restore point. Skills are code, so a restore that would add or change a skill
is listed first and applied only on a yes.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import tarfile
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

try:  # inside the plugin package
    from .signet_cli import SignetClient, SignetError
except ImportError:  # the tests import this module on its own
    from signet_cli import SignetClient, SignetError

MEMORY_FILES = ("MEMORY.md", "USER.md")
POINT_PREFIX = "memory-"
POINT_SUFFIX = ".tar.gz"
STAMP_RE = re.compile(r"(\d{8}T\d{6}Z-[0-9a-f]{6})")
MANIFEST_MEMBER = "manifest.json"
# Reasons that make a point at once when something changed, interval or not.
FORCE_REASONS = {"session_end", "command", "cli", "accepted removal", "pre_restore"}


# ---------------------------------------------------------------- local side
@dataclass(frozen=True)
class Tracked:
    rel: str          # "memories/MEMORY.md" or "skills/<name>/SKILL.md"
    local: Path
    size: int
    sha256: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hermes_lists(skills_dir: Path) -> tuple[set[str], set[str]]:
    """The skill names Hermes itself installed: bundled, and from its hub.

    Formats as the installed Hermes (v0.23.0) writes them: `.bundled_manifest`
    is one `name:hash` per line (older files carry plain names), and
    `.hub/lock.json` is `{"version": 1, "installed": {name: {...}}}`
    (`tools/skills_sync.py`, `tools/skills_hub.py`). Both are read
    defensively: a missing or unreadable list reads as empty, which errs
    toward copying more, never less.
    """
    bundled: set[str] = set()
    hub: set[str] = set()
    try:
        for line in (skills_dir / ".bundled_manifest").read_text(encoding="utf-8", errors="replace").splitlines():
            name = line.split(":", 1)[0].strip()
            if name and not name.startswith("#"):
                bundled.add(name)
    except OSError:
        pass
    try:
        data = json.loads((skills_dir / ".hub" / "lock.json").read_text(encoding="utf-8-sig", errors="replace"))
        installed = data.get("installed") if isinstance(data, dict) else None
        if isinstance(installed, dict):
            hub = {str(k) for k in installed.keys()}
    except (OSError, ValueError):
        pass
    return bundled, hub


def own_skill_dirs(skills_dir: Path) -> list[Path]:
    """The agent's own skills: every skill folder neither of Hermes's lists names.

    A bundled skill the agent has edited is not copied by this rule. That is
    an open question in the design, said plainly in the README.
    """
    if not skills_dir.is_dir():
        return []
    bundled, hub = hermes_lists(skills_dir)
    out = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in bundled or child.name in hub:
            continue
        if (child / "SKILL.md").exists():
            out.append(child)
    return out


def scan(hermes_home: Path, include_skills: bool = True) -> dict[str, Tracked]:
    """Every file the mirror tracks, keyed by its relative path in a point."""
    tracked: dict[str, Tracked] = {}
    mem = hermes_home / "memories"
    for name in MEMORY_FILES:
        p = mem / name
        if p.is_file():
            tracked[f"memories/{name}"] = Tracked(f"memories/{name}", p, p.stat().st_size, sha256_file(p))
    if include_skills:
        skills = hermes_home / "skills"
        for d in own_skill_dirs(skills):
            for p in sorted(d.rglob("*")):
                if p.is_file() and not p.name.startswith(".") and "__pycache__" not in p.parts:
                    rel = "skills/" + p.relative_to(skills).as_posix()
                    tracked[rel] = Tracked(rel, p, p.stat().st_size, sha256_file(p))
    return tracked


# ------------------------------------------------------------------ manifest
@dataclass
class Manifest:
    stamp: str
    created_at: str
    kind: str = "pass"                     # "pass" or "pre_restore"
    entries: dict = field(default_factory=dict)   # rel -> {"sha256","size"}
    client_version: str = ""
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "Manifest":
        d = json.loads(text)
        return Manifest(
            stamp=d["stamp"], created_at=d.get("created_at", ""), kind=d.get("kind", "pass"),
            entries=dict(d.get("entries", {})),
            client_version=d.get("client_version", ""), note=d.get("note", ""),
        )

    def created(self) -> datetime:
        return _parse_time(self.created_at)


def new_stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


UNDO_MARK = "-before-restore"


def point_name(stamp: str, kind: str = "pass") -> str:
    mark = UNDO_MARK if kind == "pre_restore" else ""
    return f"{POINT_PREFIX}{stamp}{mark}{POINT_SUFFIX}"


def stamp_of(name: str) -> Optional[str]:
    """The stamp inside a point's file name, wherever the human has put it by renaming."""
    m = STAMP_RE.search(name or "")
    return m.group(1) if m else None


def is_point(name: str) -> bool:
    return str(name).endswith(POINT_SUFFIX) and stamp_of(str(name)) is not None


def stamp_time(stamp: str) -> datetime:
    return datetime.strptime(stamp[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def _parse_time(text: str) -> datetime:
    try:
        t = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- the result
@dataclass
class PassResult:
    status: str                 # in_step | uploaded | pending | needs_you | behind | refused | error
    stamp: Optional[str] = None
    uploaded: list = field(default_factory=list)   # the rel paths the point holds
    missing: list = field(default_factory=list)
    reason: str = ""
    error: Optional[str] = None
    pruned: list = field(default_factory=list)      # point names removed by the ceiling at this pass
    next_point_at: str = ""
    retirable: int = 0                              # points the ceiling would remove, when thinning is off
    others: int = 0                                 # point files in the folder this plugin did not make or has lost by rename
    undo: int = 0                                   # restores' undo points kept beside the day's point
    tracked: int = -1                               # local files the mirror tracks at this pass; 0 is a profile with no memory yet

    def line(self) -> str:
        if self.status == "in_step":
            if self.tracked == 0:
                return "Nothing to copy yet. Your agent has no memory files until it saves one." + self._tail()
            s = "Mirror in step."
            if self.pruned:
                s += f" {len(self.pruned)} retired point(s) removed."
            return s + self._tail()
        if self.status == "uploaded":
            s = f"Mirror in step. Point {self.stamp} holds {len(self.uploaded)} file(s)."
            if self.pruned:
                s += f" {len(self.pruned)} retired point(s) removed."
            return s + self._tail()
        if self.status == "pending":
            when = self.next_point_at[11:16] + "Z" if len(self.next_point_at) >= 16 else "the interval's end"
            return f"Mirror in step. Changes wait for the next point at {when} or at the session's end."
        if self.status == "needs_you":
            return (f"Drive needs you. {len(self.missing)} file(s) the last point held are missing "
                    f"locally: {', '.join(self.missing[:3])}{'…' if len(self.missing) > 3 else ''}. "
                    "Restore them or confirm the removal.")
        if self.status == "behind":
            return f"Mirror behind. {self.reason}"
        if self.status == "refused":
            return f"Mirror stopped. {self.reason}"
        return f"Mirror error. {self.error or self.reason}"

    def _tail(self) -> str:
        s = f" {self.retirable} retired point(s) wait for removal." if self.retirable else ""
        if self.undo:
            s += f" {self.undo} undo point(s) from restores kept."
        if self.others:
            s += f" {self.others} point file(s) in the folder are not the mirror's own (pinned or placed there), and they are kept and never removed."
        return s


@dataclass
class RestorePlan:
    manifest: Manifest
    name: str                  # the point's file name in the drive
    origin: str                # "this Mac" when the point is in this plugin's own index, else "unknown"
    memory_changes: list       # rel paths of memory files that would change
    skill_changes: list        # rel paths under skills/ that would be added or changed
    unchanged: list

    def needs_yes(self) -> bool:
        return bool(self.skill_changes) or self.origin != "this Mac"


class MirrorBusy(Exception):
    pass


# ------------------------------------------------------------------ the mirror
class Mirror:
    def __init__(self, client: SignetClient, hermes_home: Path, data_dir: Path,
                 root_name: str = "Memory Mirror", include_skills: bool = True,
                 prune: bool = True, keep_days: int = 9,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.client = client
        self.hermes_home = Path(hermes_home)
        self.data_dir = Path(data_dir)
        self.root_name = root_name
        self.include_skills = include_skills
        self.prune = bool(prune)
        self.keep_days = int(keep_days)        # a point older than this many days is removed; one point per day is kept
        self.now = now
        self._lock = threading.Lock()
        self._root_known = False
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- local state ----------------------------------------------------
    @property
    def _latest_path(self) -> Path:
        return self.data_dir / "latest-manifest.json"

    @property
    def _state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def _index_path(self) -> Path:
        return self.data_dir / "points.json"

    def state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes) -> None:
        s = self.state()
        s.update(changes)
        _atomic_write(self._state_path, json.dumps(s, indent=2, sort_keys=True))

    def index(self) -> dict:
        """The points this plugin made, by stamp: name, sha256, size, created_at, kind."""
        try:
            return json.loads(self._index_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_index(self, idx: dict) -> None:
        _atomic_write(self._index_path, json.dumps(idx, indent=2, sort_keys=True))

    def latest_point(self, refresh: bool = False) -> Optional[Manifest]:
        """The newest point's manifest, from the local cache or, on refresh, the drive."""
        if refresh:
            m = self._fetch_latest_manifest()
            if m is not None:
                _atomic_write(self._latest_path, m.to_json())
            else:
                self._latest_path.unlink(missing_ok=True)      # no point in the drive: no cache either
            return m
        try:
            m = Manifest.from_json(self._latest_path.read_text())
        except (OSError, ValueError):
            return None
        # A cached manifest that is not one of this plugin's own points (the shape before
        # 2026-11-07 kept copies under folders; a refresh may have cached another Mac's point)
        # is not a baseline: the next pass makes a point of its own.
        e = self.index().get(m.stamp)
        return m if e and e.get("status", "uploaded") == "uploaded" else None

    def _fetch_latest_manifest(self) -> Optional[Manifest]:
        names = self.list_points()
        if not names:
            return None
        manifest, _ = self._read_point(names[-1], keep=False)
        return manifest

    # ---- drive layout ---------------------------------------------------
    def ensure_root(self) -> None:
        """The share folder exists, created once. Never created twice, since
        names are not unique on the server and a duplicate would make every
        path ambiguous."""
        if self._root_known:
            return
        if not self.client.handle:
            raise SignetError(61, "not_enrolled", "no handle configured, so the mirror does not run as a default identity", [])
        # An agent's own top-level folders show under `folder list` at the top level;
        # `share list` lists folders shared WITH the caller (the public CLI specification,
        # and a duplicate created on 2026-11-07 by reading the wrong one).
        names = [str(r.get("name")) for r in self.client.folder_list(None)]
        if names.count(self.root_name) > 1:
            raise SignetError(2, "ambiguous_root", f"{self.root_name!r} exists more than once at the top level, and the human must remove the extra", [])
        if self.root_name not in names:
            self.client.share_create(self.root_name)
        self._root_known = True

    def list_points(self) -> list[str]:
        """Every point file in the root, by stamp order, whatever the human renamed it to."""
        self.ensure_root()
        rows = self.client.file_list(self.root_name)
        names = [str(r.get("name")) for r in rows if is_point(str(r.get("name", "")))]
        return sorted(names, key=lambda n: stamp_of(n) or "")

    # ---- the pass -------------------------------------------------------
    def run_pass(self, reason: str = "trigger", kind: str = "pass", note: str = "") -> PassResult:
        """One reconcile. Thread-safe: a second caller while one runs gets MirrorBusy."""
        if not self._lock.acquire(blocking=False):
            # The running pass scanned before this caller's change: ask it to scan once more at its
            # end, so a write that arrives during an upload lands in the day's point instead of
            # waiting for the next trigger (Bob, case 11 at 4c84361, 2026-11-08).
            self._rerun_wanted = True
            raise MirrorBusy("a pass is already running")
        # A second lock across processes: the gateway and a CLI session are two (Bob, run 1, case 11).
        lock_path = self.data_dir / "pass.lock"
        fh = open(lock_path, "a+")
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise MirrorBusy("a pass is already running in another process")
            try:
                self._rerun_wanted = False
                result = self._pass(reason, kind, note)
                if getattr(self, "_rerun_wanted", False) and kind == "pass":
                    self._rerun_wanted = False
                    result = self._pass(reason + ", rerun for a change that arrived meanwhile", kind, note)
                return result
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._lock.release()

    def _pass(self, reason: str, kind: str, note: str) -> PassResult:
        started = self.now()
        if not self.client.handle:
            return PassResult("refused", reason="Not enrolled, no handle configured, so nothing runs as a default identity.")
        self._sweep_temps()
        try:
            self.reconcile_index()
        except SignetError:
            pass                                 # the folder did not answer; the pass itself says so below
        local = scan(self.hermes_home, self.include_skills)
        latest = self.latest_point()
        prev = latest.entries if latest else {}
        accepted = set(self.state().get("accepted_missing", []))
        returned = sorted(accepted & set(local))          # an accepted removal whose file is back is tracked again
        if returned:
            accepted -= set(returned)
            self._save_state(accepted_missing=sorted(accepted))

        changed = [rel for rel, t in local.items() if prev.get(rel, {}).get("sha256") != t.sha256]
        missing = [rel for rel in prev if rel not in local and rel not in accepted]
        removed = [rel for rel in prev if rel not in local and rel in accepted]   # accepted, not yet in a point

        if kind == "pre_restore":
            missing = []      # the point being saved is what exists now; the restore itself answers the loss

        if not changed and not missing and not removed:
            self._save_state(last_pass=started.isoformat(), last_status="in_step", last_reason=reason)
            # The ceiling holds on a quiet pass too, so a folder left over the rule (a setting
            # changed, a restore's extra point) converges without waiting for a change.
            pruned: list[str] = []
            if self.prune and kind != "pre_restore":
                try:
                    pruned = self.thin(started)
                except SignetError as e:
                    self._save_state(thin_error=f"{e.exit_code} {e.code}: {e.message}", thin_error_at=started.isoformat())
            retirable, others, undo = self._counts_quiet()
            return PassResult("in_step", pruned=pruned, retirable=retirable, others=others, undo=undo, tracked=len(local))

        if missing:
            # Nothing is uploaded and nothing is removed: a point that enshrined the loss as the
            # newest would be the wrong record. The human restores or confirms the removal.
            self._save_state(last_pass=started.isoformat(), last_status="needs_you",
                             last_reason=reason, missing=missing)
            return PassResult("needs_you", missing=missing)

        # Every change makes a point, and the day's newer point replaces the older (Chris, 2026-11-08:
        # Hermes Desktop ends a session after every turn, so v06's hourly limit could never apply there,
        # and a backup that saves every change is the better promise; the ceiling keeps the folder small).

        stamp = new_stamp(started)
        name = point_name(stamp, kind)
        if removed and not note:
            note = "removal accepted: " + ", ".join(removed)
        manifest = Manifest(stamp=stamp, created_at=started.isoformat(), kind=kind,
                            entries={rel: {"sha256": t.sha256, "size": t.size} for rel, t in local.items()},
                            client_version=self.state().get("client_version", ""), note=note)
        tmp = self.data_dir / (name + ".tmp")
        try:
            _write_bundle(tmp, manifest, local)
            bundle_sha = sha256_file(tmp)
            size = tmp.stat().st_size
            self.ensure_root()
            # The index entry goes in as pending BEFORE the upload: a pass killed here leaves
            # the client child running, and the point it finishes is adopted at the next pass
            # rather than left in the folder beyond the ceiling's reach (Bob, run 3, case 10).
            idx = self.index()
            idx[stamp] = {"name": name, "sha256": bundle_sha, "size": size, "created_at": manifest.created_at,
                          "kind": kind, "files": len(local), "status": "pending"}
            self._save_index(idx)
            _atomic_write(self._pending_manifest_path(stamp), manifest.to_json())   # promoted on adoption (Bob, run 4)
            self.client.file_upload(f"{self.root_name}/{name}", tmp)
            idx = self.index()
            if stamp in idx:
                idx[stamp]["status"] = "uploaded"
                self._save_index(idx)
            _atomic_write(self._latest_path, manifest.to_json())
            self._pending_manifest_path(stamp).unlink(missing_ok=True)
            if removed:
                acc = set(self.state().get("accepted_missing", []))
                self._save_state(accepted_missing=sorted(acc - set(removed)))   # recorded in a point, so no longer a question
            self._save_state(last_pass=started.isoformat(), last_status="uploaded", last_reason=reason,
                             last_stamp=stamp, missing=[], pending_since=None, next_point_at=None)
            pruned: list[str] = []
            retirable = others = undo = 0
            try:
                # A pre-restore point never thins: the point the human just chose stays in the
                # folder while they look at the result; the ceiling applies at the next ordinary point.
                if self.prune and kind != "pre_restore":
                    pruned = self.thin(started)
                elif not self.prune:
                    retirable = len(self.retirable(started))
                others = self.others()
                undo = self.undo_points()
            except SignetError as e:   # the ceiling's failure never undoes the point
                self._save_state(thin_error=f"{e.exit_code} {e.code}: {e.message}", thin_error_at=started.isoformat())
            return PassResult("uploaded", stamp=stamp, uploaded=sorted(local), pruned=pruned, retirable=retirable, others=others, undo=undo)
        except SignetError as e:
            # The upload did not complete from this process's view; the pending entry stays and
            # the next pass reconciles it against the folder (the child may have finished).
            return self._failed(e, started, reason)
        finally:
            tmp.unlink(missing_ok=True)

    def _pending_manifest_path(self, stamp: str) -> Path:
        return self.data_dir / f"pending-{stamp}.manifest.json"

    def _sweep_temps(self) -> None:
        """Build temps a killed pass left behind. Only a pass writes them, under the lock this
        pass holds, so every one present is stale (Bob, run 3: a 40 MB `.tmp` after a kill)."""
        for p in self.data_dir.glob(f"{POINT_PREFIX}*{POINT_SUFFIX}.tmp"):
            p.unlink(missing_ok=True)

    def reconcile_index(self, present: Optional[set[str]] = None) -> dict:
        """Pending entries meet the folder: a point that is there is ours and confirmed; one
        that is not never completed and is forgotten. Returns {adopted: [...], dropped: [...]}."""
        idx = self.index()
        pending = [s for s, e in idx.items() if e.get("status") == "pending"]
        if not pending:
            return {"adopted": [], "dropped": []}
        if present is None:
            present = set(self.list_points())
        adopted, dropped = [], []
        for s in pending:
            pm = self._pending_manifest_path(s)
            if idx[s].get("name") in present:
                idx[s]["status"] = "uploaded"
                adopted.append(idx[s]["name"])
                # The adopted point's manifest becomes the baseline when it is the newest, so the
                # next pass does not upload the same content again (Bob, run 4, reading 1).
                try:
                    current = Manifest.from_json(self._latest_path.read_text())
                except (OSError, ValueError):
                    current = None
                if pm.exists() and (current is None or current.stamp < s or current.stamp not in idx):
                    os.replace(pm, self._latest_path)
            else:
                del idx[s]
                dropped.append(s)
            pm.unlink(missing_ok=True)
        self._save_index(idx)
        if adopted or dropped:
            log = (self.state().get("reconciled", []) + [{"at": self.now().isoformat(), "adopted": adopted, "dropped": dropped}])[-20:]
            self._save_state(reconciled=log)
        return {"adopted": adopted, "dropped": dropped}

    def _failed(self, e: SignetError, started, reason) -> PassResult:
        if e.locked:
            status, text = "behind", f"This Mac's keys are unavailable, behind since {started.strftime('%H:%M')}Z, and it catches up at the next start."
        elif e.temporary:
            status, text = "behind", f"The drive did not answer ({e.code}), behind since {started.strftime('%H:%M')}Z."
        elif e.read_only:
            status, text = "refused", "Account read-only, copies not being saved."
        elif e.quota:
            status, text = "refused", "The drive is full; nothing was pruned to make room."
        else:
            status, text = "refused", f"The drive refused the copy ({e.code}: {e.message})."
        self._save_state(last_pass=started.isoformat(), last_status=status, last_reason=reason,
                         last_error=f"{e.exit_code} {e.code}")
        return PassResult(status, reason=text, error=str(e))

    def accept_missing(self, rels: Iterable[str]) -> None:
        """The human (or the agent on its word) confirms that files are gone on purpose."""
        acc = set(self.state().get("accepted_missing", []))
        acc.update(rels)
        self._save_state(accepted_missing=sorted(acc))

    # ---- the ceiling ----------------------------------------------------
    def _keep(self, stamps: list[str], now: datetime) -> set[str]:
        """The stamps the rule keeps, out of the points this plugin made (Chris, 2026-11-07).

        One point per day, the day's newest; a day older than `keep_days` keeps nothing;
        the newest point overall is always kept. With a point every day that is
        `keep_days` + 1 files, ten by default. Days are UTC dates.
        """
        if not stamps:
            return set()
        idx = self.index()

        def order(s: str):
            # two points in one second differ only by their random suffix; the recorded time decides
            return (idx.get(s, {}).get("created_at", ""), s)
        today = now.date()
        newest_per_day: dict = {}
        keep: set[str] = set()
        for s in sorted(stamps, key=order):             # ascending, so the last write per day is the newest
            d = stamp_time(s).date()
            if (today - d).days > self.keep_days:
                continue
            if idx.get(s, {}).get("kind") == "pre_restore":
                keep.add(s)                           # a restore's undo stands outside the one-per-day rule
            else:
                newest_per_day[d] = s
        keep.update(newest_per_day.values())
        keep.add(max(stamps, key=order))
        return keep

    def retirable(self, now: Optional[datetime] = None) -> list[str]:
        """The point files the ceiling would remove: ours by exact name, present in the
        drive, outside the kept set. A renamed file is not ours by name and is never here."""
        now = now or self.now()
        idx = self.index()
        if not idx:
            return []
        present = set(self.list_points())
        ours = {s: e["name"] for s, e in idx.items() if e.get("name") in present}
        keep = self._keep(sorted(ours), now)
        return [ours[s] for s in sorted(ours) if s not in keep]

    def others(self) -> int:
        """Point files in the folder this plugin did not make by that name: a human's upload, a
        renamed (pinned) point, another Mac's. Never removed; counted so a pile of them is seen."""
        present = self.list_points()
        self.reconcile_index(set(present))
        ours = {e.get("name") for e in self.index().values()}
        return sum(1 for n in present if n not in ours)

    def undo_points(self) -> int:
        """Restores' undo points this plugin made that are still in the folder."""
        present = set(self.list_points())
        return sum(1 for e in self.index().values() if e.get("kind") == "pre_restore" and e.get("name") in present)

    def _counts_quiet(self) -> tuple[int, int, int]:
        try:
            return (0 if self.prune else len(self.retirable())), self.others(), self.undo_points()
        except SignetError:
            return 0, 0, 0

    def thin(self, now: Optional[datetime] = None) -> list[str]:
        """Remove the retired points, one delete per file, and forget them in the index."""
        names = self.retirable(now)
        if not names:
            return []
        removed: list[str] = []
        idx = self.index()
        for name in names:
            self.client.file_delete([f"{self.root_name}/{name}"])
            removed.append(name)
            for s, e in list(idx.items()):
                if e.get("name") == name:
                    del idx[s]
            self._save_index(idx)
        log = self.state().get("thinned", [])
        log = (log + [{"at": (now or self.now()).isoformat(), "removed": removed}])[-20:]
        self._save_state(thinned=log)
        return removed

    # ---- restore --------------------------------------------------------
    def _read_point(self, name: str, keep: bool) -> tuple[Manifest, Optional[Path]]:
        """Download one point and read its manifest. With keep, the bundle stays on disk."""
        tmp = self.data_dir / ("restore-" + name)
        self.client.file_download(f"{self.root_name}/{name}", tmp)
        try:
            with tarfile.open(tmp, "r:gz") as tar:
                member = tar.getmember(MANIFEST_MEMBER)
                f = tar.extractfile(member)
                if f is None:
                    raise ValueError(f"{name}: no manifest inside")
                manifest = Manifest.from_json(f.read().decode("utf-8"))
        except (tarfile.TarError, KeyError, ValueError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"{name}: not a readable point ({e})")
        if keep:
            return manifest, tmp
        tmp.unlink(missing_ok=True)
        return manifest, None

    def _name_for(self, stamp: str) -> str:
        for name in self.list_points():
            if stamp_of(name) == stamp:
                return name
        raise SignetError(26, "not_found", f"no point with stamp {stamp}", [])

    def plan_restore(self, stamp: str) -> RestorePlan:
        name = self._name_for(stamp)
        manifest, _ = self._read_point(name, keep=False)
        return self._plan(manifest, name)

    def _plan(self, manifest: Manifest, name: str) -> RestorePlan:
        local = scan(self.hermes_home, self.include_skills)
        mem, sk, same = [], [], []
        for rel, entry in sorted(manifest.entries.items()):
            if local.get(rel) and local[rel].sha256 == entry["sha256"]:
                same.append(rel)
            elif rel.startswith("skills/"):
                sk.append(rel)
            else:
                mem.append(rel)
        origin = "this Mac" if manifest.stamp in self.index() else "unknown"
        return RestorePlan(manifest, name, origin, mem, sk, same)

    def restore(self, stamp: str, yes: bool = False) -> dict:
        """Apply one point. Returns what was written. Refuses, with the plan, when a
        skill would change or the point is not one this Mac made, and `yes` is False."""
        name = self._name_for(stamp)
        manifest, bundle = self._read_point(name, keep=True)
        assert bundle is not None
        try:
            plan = self._plan(manifest, name)
            if plan.needs_yes() and not yes:
                return {"applied": False, "needs_yes": True, "origin": plan.origin,
                        "skills": plan.skill_changes, "memory": plan.memory_changes}
            to_write = plan.memory_changes + plan.skill_changes
            if not to_write:
                return {"applied": True, "written": [], "origin": plan.origin, "note": "already at that point"}
            known = self.index().get(manifest.stamp)
            if known and sha256_file(bundle) != known.get("sha256"):
                raise ValueError(f"{name} does not hash as the point this Mac uploaded ({known.get('sha256', '')[:12]})")
            # The present state becomes a restore point first.
            pre = self.run_pass(reason="pre_restore", kind="pre_restore", note=f"before restore to {stamp}")
            if pre.status not in ("in_step", "uploaded"):
                return {"applied": False, "blocked_by": pre.line()}
            staged: list[tuple[Path, Path]] = []
            stage_dir = self.data_dir / f"restore-{stamp}"
            stage_dir.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(bundle, "r:gz") as tar:
                    for rel in to_write:
                        entry = manifest.entries[rel]
                        _safe_rel(rel)
                        f = tar.extractfile(rel)
                        if f is None:
                            raise ValueError(f"{rel}: not in the point")
                        data = f.read()
                        got = hashlib.sha256(data).hexdigest()
                        if got != entry["sha256"]:
                            raise ValueError(f"{rel}: the member hashes {got[:12]}, the manifest says {entry['sha256'][:12]}")
                        tmp = stage_dir / rel.replace("/", "__")
                        tmp.write_bytes(data)
                        staged.append((tmp, self._local_path(rel)))
                written = []
                for tmp, dest in staged:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(tmp, dest)
                    written.append(dest.relative_to(self.hermes_home).as_posix())
                return {"applied": True, "written": written, "pre_restore": pre.stamp, "origin": plan.origin,
                        "note": "Hermes reads its memory files when a session starts, so this shows in the next session."}
            finally:
                for p in stage_dir.glob("*"):
                    p.unlink(missing_ok=True)
                try:
                    stage_dir.rmdir()
                except OSError:
                    pass
        finally:
            bundle.unlink(missing_ok=True)

    def _local_path(self, rel: str) -> Path:
        _safe_rel(rel)
        if rel.startswith("memories/"):
            return self.hermes_home / "memories" / rel[len("memories/"):]
        if rel.startswith("skills/"):
            return self.hermes_home / "skills" / rel[len("skills/"):]
        raise ValueError(f"unexpected path in manifest: {rel}")


# ------------------------------------------------------------------ bundles
def _safe_rel(rel: str) -> None:
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or "" in parts or "\\" in rel or "\x00" in rel:
        raise ValueError(f"unsafe path in manifest: {rel!r}")


def _write_bundle(path: Path, manifest: Manifest, local: dict[str, Tracked]) -> None:
    """One compressed archive: the manifest first, then every tracked file at its rel path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        data = manifest.to_json().encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_MEMBER)
        info.size = len(data)
        info.mtime = int(manifest.created().timestamp())
        tar.addfile(info, io.BytesIO(data))
        for rel in sorted(local):
            _safe_rel(rel)
            tar.add(local[rel].local, arcname=rel, recursive=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
