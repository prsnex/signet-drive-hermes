"""Signet Drive for Hermes. The plugin's registration.

One ordinary plugin: a check at every start, the Memory Mirror on three
triggers, four tools, one slash command and one CLI command. It calls the
`signet` client and holds no credential of its own.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .signet_cli import SignetClient
from . import sd_mirror, sd_schemas, sd_status, sd_tools
from .signet_cli import SignetError as _SignetError


def _thread(fn, *args, daemon: bool = True, name: str = "signet-drive") -> threading.Thread:
    """A thread that keeps the caller's context. Hermes binds a served profile's home and
    secret scope in context variables, which a new thread does not inherit: a start check
    on a bare thread resolved `get_hermes_home()` to the root `~/.hermes`, wrote its result
    there and ran a pass over the root profile's files (2026-11-08, found at the 1.2.0
    client check). `copy_context().run` is the host's own shape (gateway/run.py)."""
    ctx = contextvars.copy_context()
    return threading.Thread(target=ctx.run, args=(fn, *args), name=name, daemon=daemon)


def _timer(delay_s: float, fn, *args) -> threading.Timer:
    """A Timer that keeps the caller's context, for the same reason as `_thread`."""
    ctx = contextvars.copy_context()
    return threading.Timer(delay_s, ctx.run, args=(fn, *args))
sd_mirror.SignetError = _SignetError

PLUGIN_ID = "signet-drive"
MEMORY_TOOLS = {"memory", "skill_manage"}
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ wiring
def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # the active profile's home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        d = _hermes_home() / "plugin-data" / PLUGIN_ID
        d.mkdir(parents=True, exist_ok=True)
        return d


class Runtime:
    """Everything the hooks, tools and commands share. Built once per register()."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = {
            "binary": ctx.get_config("binary", default="signet"),
            "server_url": ctx.get_config("server_url", default="") or None,
            "handle": ctx.get_config("handle", default="") or None,
            "root_name": ctx.get_config("root_name", default="Memory Mirror"),
            "files_folder": ctx.get_config("files_folder", default="Files"),
            "include_skills": bool(ctx.get_config("include_skills", default=True)),
            # The log's maximum merge delay as the Log Spec declares it (24 h at v1.1.x). The server
            # publishes it on no endpoint a plugin can read, so it is a setting named for its source.
            "mmd_hours": float(ctx.get_config("mmd_hours", default=24)),
            # The Memory Mirror's shape (Chris, 2026-11-07): one file per point, fewer points, a ceiling.
            "point_interval_hours": float(ctx.get_config("point_interval_hours", default=1)),
            "prune": bool(ctx.get_config("prune", default=True)),
            "keep_days": int(ctx.get_config("keep_days", default=9)),
        }
        self._debounce = None
        self._debounce_lock = threading.Lock()
        try:
            self.status().write(config=self.cfg)
        except Exception:
            pass

    def client(self) -> SignetClient:
        return SignetClient(binary=self.cfg["binary"], server_url=self.cfg["server_url"], handle=self.cfg["handle"])

    def status(self) -> sd_status.Status:
        return sd_status.Status(_data_dir())

    def mirror(self) -> sd_mirror.Mirror:
        return sd_mirror.Mirror(self.client(), _hermes_home(), _data_dir(),
                                root_name=self.cfg["root_name"], include_skills=self.cfg["include_skills"],
                                point_interval_s=self.cfg["point_interval_hours"] * 3600.0, prune=self.cfg["prune"],
                                keep_days=self.cfg["keep_days"])

    # ---- the work, always off the agent's thread --------------------------
    def run_pass(self, reason: str) -> None:
        st = self.status()
        if not st.mirror_may_run():
            logger.warning("signet-drive: the last check forbids a pass (%s)", reason)
            return
        try:
            m = self.mirror()
            m.ensure_root()
            result = m.run_pass(reason)
            st.record_pass(result)
            if result.status in ("in_step", "uploaded", "pending"):
                st.write(last_error=None)
            self._broadcast()
        except sd_mirror.MirrorBusy:
            logger.info("signet-drive: a pass is already running (%s)", reason)
        except Exception as e:  # a hook must never take the agent down
            logger.warning("signet-drive: pass failed (%s): %s", reason, e)
            st.write(last_error=f"{type(e).__name__}: {e}", last_error_at=sd_status._now().isoformat())
            self._broadcast()

    def start(self) -> None:
        st = self.status()
        try:
            check = st.start_check(self.client(), mmd_hours=self.cfg["mmd_hours"])
            if check.get("mirror_may_run", check["state"] == "ok"):
                try:
                    m = self.mirror()
                    m.ensure_root()
                    m.latest_point(refresh=True)
                except sd_mirror.SignetError as e:   # an outage at start is a result the line shows
                    st.write(last_error=f"{e.code}: {e.message}", last_error_at=sd_status._now().isoformat())
                self.run_pass("session_start")
            else:
                self._broadcast()
        except Exception as e:
            logger.warning("signet-drive: start check failed: %s", e)
            st.write(last_error=f"{type(e).__name__}: {e}", last_error_at=sd_status._now().isoformat())

    def in_background(self, fn, *args, join_s: float = 0.0) -> None:
        t = _thread(fn, *args, daemon=join_s == 0.0)
        t.start()
        if join_s:
            t.join(join_s)

    def schedule_pass(self, reason: str, delay_s: float = 2.0) -> None:
        """Coalesce writes that arrive close together into one pass. The timer is not a
        daemon, so a session that ends a moment after a memory write still completes
        its pass before the process goes (a short CLI session showed the daemon form lost it)."""
        with self._debounce_lock:
            if self._debounce is not None:
                self._debounce.cancel()
            self._debounce = _timer(delay_s, self.run_pass, reason)
            self._debounce.daemon = False
            self._debounce.start()

    def _broadcast(self) -> None:
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event
            broadcast_plugin_event(PLUGIN_ID, "status.changed", self.status().summary())
        except Exception:
            pass


# ------------------------------------------------------------------ register
def register(ctx):
    rt = Runtime(ctx)
    handlers = sd_tools.make_handlers(rt.client, rt.cfg["files_folder"], rt.mirror, _data_dir(), hermes_home=_hermes_home())

    # hooks
    def on_session_start(session_id=None, **kwargs):
        logger.info("signet-drive: session start (%s)", session_id)
        rt.in_background(rt.start)

    def on_session_end(session_id=None, **kwargs):
        # Inside the hook, not on a thread: Hermes bounds a hook at 30 s and a pass of a few
        # files takes seconds, and a thread would die with a session that ends at once.
        logger.info("signet-drive: session end (%s), pass now", session_id)
        rt.run_pass("session_end")

    def post_tool_call(tool_name=None, args=None, result=None, **kwargs):
        if tool_name in MEMORY_TOOLS:
            logger.info("signet-drive: %s wrote; pass scheduled", tool_name)
            rt.schedule_pass(f"after {tool_name}")

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("post_tool_call", post_tool_call)

    # tools
    for schema in (sd_schemas.SAVE, sd_schemas.FETCH, sd_schemas.LIST, sd_schemas.RESTORE):
        ctx.register_tool(name=schema["name"], toolset="signet_drive", schema=schema,
                          handler=handlers[schema["name"]], description=schema["description"], emoji="🔏")

    # the slash command
    def command(raw_args: str = "", **kwargs) -> str:
        words = (raw_args or "").split()
        verb = words[0] if words else "status"
        if verb == "status":
            return rt.status().line()
        if verb == "setup":
            code = _extract_code(" ".join(words[1:]))
            lines: list = []
            _setup(rt, code, out=lines.append, form="chat")
            if code and rt.client().resolved_binary() and not rt.cfg.get("handle"):
                def note(line: str) -> None:   # the chat is handed back, so the thread speaks through the status file
                    st = rt.status()
                    st.write(last_error=line.strip() if "did not complete" in line or "no name" in line else None,
                             last_error_at=sd_status._now().isoformat(), setup_note=line.strip())
                    rt._broadcast()
                def enrol() -> None:
                    _setup_enrol(rt, rt.client(), code, note, "chat")
                _thread(enrol, daemon=False, name="signet-drive-setup").start()
                lines.append("   Enrolling now. Approve it on your account page with your passkey. When the page shows your agent as connected, type /signet status here. Then /signet about says what happens from here.")
            return "\n\n".join(lines)     # a blank line between every line: Hermes draws command output small and gray (Chris, 2026-11-08)
        if verb == "about":
            return "\n\n".join(_plain_words(rt))
        if verb == "check":
            rt.start()
            return rt.status().line()
        if verb == "pass":
            rt.run_pass("command")
            return rt.status().line()
        if verb == "accept-missing":
            st = rt.status().read()
            missing = (st.get("last_pass") or {}).get("missing") or []
            if not missing:
                return "Nothing is missing."
            rt.mirror().accept_missing(missing)
            rt.run_pass("accepted removal")
            return f"Accepted the removal of {len(missing)} file(s). " + rt.status().line()
        if verb in ("copies", "points"):
            return "\n".join(rt.mirror().list_points()) or "No restore points yet."
        if verb == "thin":
            m = rt.mirror()
            if not m.prune:
                waiting = m.retirable()
                return (f"Pruning is off, and {len(waiting)} retired point(s) would be removed. "
                        "Your human turns it on with: hermes config set plugins.entries.signet-drive.settings.prune true")
            removed = m.thin()
            return f"Removed {len(removed)} retired point(s)." if removed else "Nothing to remove."
        return "Usage: /signet [status|setup [CODE]|about|check|pass|points|thin|accept-missing]"

    ctx.register_command("signet", command, description="Signet Drive. The status line, the setup with your one-time code, a check, a pass, the restore points",
                         args_hint="[status|check|pass|points|thin|accept-missing]")

    # the CLI command: hermes signet-drive setup|status
    def setup_argparse(sub):
        subs = sub.add_subparsers(dest="sd_cmd")
        p = subs.add_parser("setup", help="Narrated setup, the check and then the enrolment with a one-time code")
        p.add_argument("code", nargs="?", help="The one-time code from the web app")
        subs.add_parser("status", help="The status line")
        subs.add_parser("pass", help="Run one mirror pass now")
        sub.set_defaults(func=cli)

    def cli(args):
        verb = getattr(args, "sd_cmd", None)
        if verb == "status":
            print(rt.status().line()); return
        if verb == "pass":
            rt.run_pass("cli"); print(rt.status().line()); return
        if verb == "setup":
            _setup(rt, _extract_code(getattr(args, "code", None) or ""), out=lambda line: print(line + "\n")); return
        print("Usage: hermes signet-drive <setup [CODE]|status|pass>")

    ctx.register_cli_command(name="signet-drive", help="Signet Drive for Hermes", setup_fn=setup_argparse, handler_fn=cli)
    logger.info("signet-drive registered (root %r, files %r)", rt.cfg["root_name"], rt.cfg["files_folder"])


def _version_number(client) -> str:
    """"signet 1.2.0 (macos-aarch64)" -> "1.2.0"."""
    words = (client.version() or "").split()
    return words[1] if len(words) > 1 else (words[0] if words else "unknown")


_CODE_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{32,})(?![A-Za-z0-9_-])")


def _extract_code(raw: str) -> Optional[str]:
    """The one-time code from whatever the human typed or pasted: the bare code, or the account
    page's whole block ("Run: signet enroll <code> …"), where the token after `enroll` wins and
    otherwise the longest code-shaped token does (Chris met the block on 2026-11-08)."""
    text = (raw or "").strip()
    if not text:
        return None
    m = re.search(r"signet\s+enroll\s+([A-Za-z0-9_-]{16,})", text)
    if m:
        return m.group(1)
    tokens = _CODE_RE.findall(text)
    if tokens:
        return max(tokens, key=len)
    return text.split()[0] if len(text.split()) == 1 else None


def _how_to_give_the_code(form: str) -> str:
    return ("Then come back to this chat and type /signet setup again, this time followed by that code." if form == "chat"
            else "Then come back to this terminal and run hermes signet-drive setup again, this time followed by that code.")


def _setup_intro(rt: Runtime, client, form: str) -> list:
    """Steps 1 and 2, the same in both forms."""
    lines = ["Signet Drive for Hermes. Setup.",
             "You have already installed this plugin, which turned on both its halves in Hermes (the agent half and the Desktop half, and you can turn the Desktop half off), and restarted the gateway. Please follow the steps below for anything still needed."]
    if not client.resolved_binary():
        lines.append("1. The Signet app. Not found. In the Signet web app, under Signet Drive access, install the signed Signet app once on this Mac and enter the setup code that page gives you, so the app is connected to your account. Then run this again.")
        return lines
    lines.append(f"1. The Signet app. Found, version {_version_number(client)}.")
    lines.append("2. Set up your agent's account. This is the step you do in your browser. Sign in to Signet Drive. If you have not yet connected the Signet app to your account, the page guides you through that first. Then open PRSN Accounts, choose Add account, then select Natively on this Mac. The page shows a one-time code and waits. " + _how_to_give_the_code(form))
    return lines


def _plain_words(rt: Runtime) -> list:
    """What happens from here, as a short numbered list (Chris's words, 2026-11-08). Shown at
    the end of a terminal setup and by `/signet about` at any time."""
    days = rt.cfg["keep_days"]
    lines = ["What happens from here.",
             f"1. Copies of your agent's memory and skills go to a folder in its drive called {rt.cfg['root_name']}, one dated file per day.",
             f"2. Copies from the last {days} days are kept. The newest is never removed."]
    if rt.cfg["prune"]:
        lines.append("3. The plugin removes only its own older copies. It never touches a file you renamed or anything it did not make.")
    else:
        lines.append("3. The plugin removes nothing on its own. The status line counts the older copies waiting, and you can turn pruning on.")
    lines += ["4. Memory means the agent's two memory files and its own skills, never your conversations.",
              "5. Before a restore the present state is saved first, so a restore can be undone.",
              "6. The agent's keys live only in this Mac's Secure Enclave.",
              "7. What your agent saves counts against your storage.",
              "8. If your Signet Drive plan or a free trial ends, your account and your agent's are deleted together.",
              "9. Every 30 days you re-confirm your agent's access on your account page. Miss it and the copies pause."]
    return lines


_ENROL_TIMEOUT_S = 35 * 60   # the code lives 30 minutes; the client blocks through two human steps
_APPROVAL_WAIT_S = 15 * 60   # how long setup waits for the Guardian's passkey approval before leaving it to the next check
_APPROVAL_POLL_S = 5.0


def _access_status(rt: Runtime) -> str:
    try:
        return str(((rt.client().whoami() or {}).get("drive_access") or {}).get("status") or "")
    except Exception:
        return ""


def _wait_for_approval(rt: Runtime, out, limit_s: float = _APPROVAL_WAIT_S, poll_s: float = _APPROVAL_POLL_S) -> str:
    """Poll the agent's access until the Guardian's approval shows (`awaiting_pickup`) or the
    access is already `authorized`; `none` means the passkey step has not happened yet. On
    2026-11-08 the connect ran one second after the enrolment, before Chris's approval had
    reached the server, and the page sat at "waiting to connect"."""
    deadline = time.monotonic() + limit_s
    status = _access_status(rt)
    while status in ("", "none") and time.monotonic() < deadline:
        time.sleep(poll_s)
        status = _access_status(rt)
    return status


def _setup_enrol(rt: Runtime, client, code: str, out, form: str) -> bool:
    """Step 3 onward: the enrolment with the code, the connect, the first check and pass.
    Returns True when the agent is enrolled and connected. In the chat form `out` is a
    status-file writer, since the chat was handed back before this runs."""
    import json
    import subprocess
    argv = [client.binary, "enroll", code] + (["--server-url", client.server_url] if client.server_url else [])
    env = client._env()
    env.pop("SIGNET_HANDLE", None)          # the human names the agent; the handle comes back from the server
    try:
        proc = subprocess.run(argv, env=env, stdout=subprocess.PIPE, text=True, timeout=_ENROL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        out("   The enrolment did not complete within the code's lifetime. Nothing was saved. Start a new enrolment on your account page and run setup again with its code.")
        return False
    if proc.returncode != 0:
        out(f"   The enrolment did not complete (exit {proc.returncode}). Nothing was saved. Start a new enrolment on your account page and run setup again with its code.")
        return False
    try:
        handle = str(json.loads(proc.stdout.strip() or "{}").get("handle") or "")
    except ValueError:
        handle = ""
    if not handle:
        out("   The client reported success but gave no name for the agent. Set it by hand: hermes config set plugins.entries.signet-drive.settings.handle <handle>")
        return False
    rt.ctx.set_config("handle", handle)
    rt.cfg["handle"] = handle
    rt.status().write(config=rt.cfg, enrolled_at=sd_status._now().isoformat())
    out(f"4. Enrolled as {handle}. Waiting for your approval on your account page, then connecting.")
    status = _wait_for_approval(rt, out)
    if status == "authorized":
        out("   Connected already. Your account page shows the agent as connected.")
    elif status == "awaiting_pickup":
        try:
            rt.client().connect()
            out("   Connected. Your account page shows the agent as connected.")
        except Exception as e:  # the start check says what is missing
            out(f"   Not connected yet ({e}). The check below says what is missing.")
    else:
        out("   Your approval has not reached the server yet. Approve on your account page, and the next check connects on its own.")
    out("5. The first check, and the first copy of your agent's memory.")
    rt.start()
    out("   " + rt.status().line())
    return True


def _setup(rt: Runtime, code, out=print, form: str = "terminal"):
    """The guided setup, narrated in the human's terms. Their steps are named as theirs.
    `out` takes each line, `print` for the terminal command and a list's append for the chat.
    The terminal form runs the enrolment inline, since its lines print live. The chat form
    hands the chat back before the enrolment (see the command), since the client blocks
    through two human steps and the chat would show nothing until the human had done both."""
    client = rt.client()
    for line in _setup_intro(rt, client, form):
        out(line)
    if not client.resolved_binary():
        return
    if not code:
        rt.status().start_check(client, mmd_hours=rt.cfg["mmd_hours"])
        out("3. Status now. " + rt.status().line())
        return
    if rt.cfg.get("handle"):
        out(f"3. This profile is already set up as {rt.cfg['handle']}, so nothing was changed. One profile serves one agent. To enrol a different agent here, your human first changes the handle setting (hermes config set plugins.entries.signet-drive.settings.handle, to the new name or to nothing) and then runs setup again with a code.")
        return
    out("3. Enrolling your agent with that code. You approve it on your account page with your passkey. The agent's keys are made in this Mac's Secure Enclave and never leave it.")
    if form == "chat":
        return   # the command runs _setup_enrol on a thread and tells the human what to do meanwhile
    if _setup_enrol(rt, client, code, out, form):
        out("")
        for line in _plain_words(rt):
            out(line)
