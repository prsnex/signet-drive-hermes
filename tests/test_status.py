"""The start check's two trust checks against the fake client: the key check's three
answers (Bob's case 2 in shape), and the client check's byte tie through a zip made here."""
import importlib.util, io, json, shutil, sys, tempfile, unittest, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FAKE = HERE / "fake_signet.py"


def load_package():
    name = "signet_drive_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod)
    return mod


class StatusCase(unittest.TestCase):
    def setUp(self):
        self.pkg = load_package()
        self.tmp = Path(tempfile.mkdtemp(prefix="sds-"))
        self.store = self.tmp / "drive.json"
        self.env = {"SIGNET_FAKE_STORE": str(self.store)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, **env):
        return self.pkg.SignetClient(binary=str(FAKE), handle="test-agent", extra_env={**self.env, **env})

    def test_case_2_key_check_three_answers(self):
        kc = self.pkg.sd_status.key_check
        ok, detail, upd = kc(self.client(), {})
        self.assertIs(ok, True); self.assertIn("matches", detail)
        ok, detail, upd = kc(self.client(SIGNET_FAKE_TV="pending"), {})
        self.assertIsNone(ok); self.assertIn("wait for the public log's next publish", detail); self.assertIn("twice a day", detail); self.assertTrue(upd.get("key_pending_since"))
        old = "2026-01-01T00:00:00+00:00"
        ok, detail, upd = kc(self.client(SIGNET_FAKE_TV="pending"), {"key_pending_since": old})
        self.assertIsNone(ok); self.assertIn("OVERDUE", detail); self.assertIn("24 h", detail); self.assertTrue(upd.get("key_overdue"))
        ok, detail, upd = kc(self.client(SIGNET_FAKE_TV="violation"), {})
        self.assertIs(ok, False); self.assertIn("FAILED", detail)

    def test_client_check_ties_the_installed_bytes_to_the_published_zip(self):
        # a fake "installed" binary inside an .app, and a zip of the same app
        app = self.tmp / "Fake.app" / "Contents" / "MacOS"; app.mkdir(parents=True)
        binary = app / "signet"; binary.write_bytes(b"\x00fake signet 9.9.9\x00" * 50)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("signet.app/Contents/MacOS/signet", binary.read_bytes())
        zip_bytes = buf.getvalue()
        manifest = json.dumps({"zip_sha256": __import__("hashlib").sha256(zip_bytes).hexdigest(), "version": "9.9.9"}).encode()
        fetched = []
        def fetch(url, limit=0):
            fetched.append(url)
            return manifest if url.endswith(".manifest.json") else zip_bytes
        class C:
            server_url = "https://example.test"
            def binary_path(self): return binary
            def version(self): return "signet 9.9.9 (macos-aarch64)"
        import unittest.mock as um
        cg = self.pkg.sd_status.client_genuine
        # the local signature checks are Apple's tools; stand them in for this fake app
        with um.patch.object(self.pkg.sd_status.subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: um.Mock(stderr="Identifier=ai.prsnex.signet\nTeamIdentifier=F2FY22A25D\n", returncode=0)
            ok, detail, upd = cg(C(), {}, fetch=fetch, server_url="https://example.test")
            self.assertIs(ok, True, detail)
            self.assertIn("byte-equal", detail)
            self.assertEqual(fetched[1], "https://example.test/cli/signet-9.9.9-macos.zip")
            # cached: no fetch the second time
            ok2, detail2, _ = cg(C(), upd, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched again")))
            self.assertIs(ok2, True)
            # a tampered installed binary fails
            binary.write_bytes(b"tampered")
            ok3, detail3, _ = cg(C(), {}, fetch=fetch)
            self.assertIs(ok3, False); self.assertIn("not the published", detail3)
            # the wrong team fails before any fetch
            run.side_effect = lambda argv, **kw: um.Mock(stderr="Identifier=ai.prsnex.signet\nTeamIdentifier=XXXXXXXXXX\n", returncode=0)
            ok4, detail4, _ = cg(C(), {}, fetch=lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetched")))
            self.assertIs(ok4, False); self.assertIn("team", detail4)




class RunOneStatusCase(StatusCase):
    def test_case_3_unreachable_drive_reads_behind_not_not_enrolled(self):
        st = self.pkg.sd_status.Status(self.tmp / "data")
        c = self.client(SIGNET_FAKE_FAIL="31:network_error", SIGNET_FAKE_FAIL_N="9")
        self.store.write_text(json.dumps({"shares": {}, "folders": {}, "files": {}, "uploads": 0, "fails_left": None, "quota": {"bytes_used": 0, "bytes_quota": 10}}))
        import unittest.mock as um
        with um.patch.object(self.pkg.sd_status, "client_genuine", return_value=(True, "stub", {})):
            r = st.start_check(c)
        names = {x["name"]: x for x in r["checks"]}
        self.assertEqual(r["state"], "behind")
        self.assertNotIn("enrolled", names)
        self.assertIn("not reachable", names["drive"]["detail"])
        self.assertIn("drive not reachable", st.line())
        self.assertTrue(r["mirror_may_run"] is False)

    def test_case_4_an_old_check_shows_its_age(self):
        st = self.pkg.sd_status.Status(self.tmp / "data")
        st.write(last_check={"at": "2026-01-01T10:00:00+00:00", "state": "ok", "checks": []})
        self.assertIn("days ago", st.line())

    def test_case_5_an_outage_error_reaches_the_line_and_the_state(self):
        st = self.pkg.sd_status.Status(self.tmp / "data")
        st.write(last_check={"at": self.pkg.sd_status._now().isoformat(), "state": "ok", "checks": []},
                 last_pass={"at": "2026-11-07T10:00:00+00:00", "status": "in_step", "line": "Mirror in step."},
                 last_error="network_error: the drive did not answer", last_error_at=self.pkg.sd_status._now().isoformat())
        self.assertIn("Last attempt failed", st.line())
        self.assertEqual(st.summary()["state"], "behind")

    def test_key_check_exit_map_stops_only_on_13_and_60(self):
        kc = self.pkg.sd_status.key_check
        ok, detail, _ = kc(self.client(SIGNET_FAKE_FAIL="32:transfer_stalled", SIGNET_FAKE_FAIL_N="9"), {})
        self.assertIsNone(ok); self.assertIn("could not be read", detail)
        ok, detail, _ = kc(self.client(SIGNET_FAKE_FAIL="2:invalid_arguments", SIGNET_FAKE_FAIL_N="9"), {})
        self.assertIsNone(ok); self.assertIn("could not be checked", detail)

    def test_pending_clock_runs_from_the_enrolment(self):
        kc = self.pkg.sd_status.key_check
        ok, detail, upd = kc(self.client(SIGNET_FAKE_TV="pending"), {"enrolled_at": "2026-01-01T00:00:00+00:00"})
        self.assertIn("OVERDUE", detail)

class LockedMacCase(StatusCase):
    def test_a_locked_mac_reads_behind_in_its_own_words_and_forbids_the_pass(self):
        # 2026-11-07 20:17Z: the Mac locked while Chris was away; exit 52 on every key use.
        st = self.pkg.sd_status.Status(self.tmp / "data")
        c = self.client(SIGNET_FAKE_FAIL="52:se_locked", SIGNET_FAKE_FAIL_N="9")
        self.store.write_text(json.dumps({"shares": {}, "folders": {}, "files": {}, "uploads": 0, "fails_left": None, "quota": {"bytes_used": 0, "bytes_quota": 10}}))
        import unittest.mock as um
        with um.patch.object(self.pkg.sd_status, "client_genuine", return_value=(True, "stub", {})):
            r = st.start_check(c)
        names = {x["name"]: x for x in r["checks"]}
        self.assertEqual(r["state"], "behind")
        # class-neutral since 1.2.0: the sentence names the keys and the unlock, not the lock itself
        self.assertIn("unlocks it", names["drive"]["detail"])
        self.assertIn("unavailable", st.line())
        self.assertNotIn("not reachable", st.line())
        self.assertIs(r["mirror_may_run"], False)

    def test_two_threads_writing_through_fresh_instances_lose_nothing(self):
        # The runtime makes a fresh Status per call; on 2026-11-07 a per-instance lock let a
        # pass's write carry the old check back over the start check's write.
        import threading
        data = self.tmp / "data"
        n = 150

        def writer(key):
            for i in range(n):
                self.pkg.sd_status.Status(data).write(**{key: i})
        ts = [threading.Thread(target=writer, args=(k,)) for k in ("a", "b", "c")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        s = self.pkg.sd_status.Status(data).read()
        self.assertEqual((s.get("a"), s.get("b"), s.get("c")), (n - 1, n - 1, n - 1))


class OverdueDriveCase(StatusCase):
    def test_an_overdue_key_still_shows_the_drive(self):
        st = self.pkg.sd_status.Status(self.tmp / "data")
        st.write(enrolled_at="2026-01-01T00:00:00+00:00")
        import unittest.mock as um
        with um.patch.object(self.pkg.sd_status, "client_genuine", return_value=(True, "stub", {})):
            r = st.start_check(self.client(SIGNET_FAKE_TV="pending"))
        names = {x["name"]: x for x in r["checks"]}
        self.assertEqual(r["state"], "needs_you")
        self.assertIn("OVERDUE", names["key_matches_log"]["detail"])
        self.assertTrue(names["drive"]["ok"])
        self.assertTrue(r["mirror_may_run"])
        line = st.line()
        self.assertIn("OVERDUE", line); self.assertIn("rive reachable", line)


class ThreadScopeCase(unittest.TestCase):
    """The hooks' threads keep the caller's context: Hermes binds a served profile's home in a
    ContextVar, and a bare thread loses it (the 1.2.0 client check wrote to the root home)."""

    def setUp(self):
        self.pkg = load_package()

    def test_thread_and_timer_carry_the_callers_context(self):
        import contextvars, threading
        var = contextvars.ContextVar("sd_test_home", default="root")
        seen = {}
        done = threading.Event()
        def read(tag):
            seen[tag] = var.get()
            if len(seen) == 3:
                done.set()
        var.set("profile")
        self.pkg._thread(read, "thread").start()
        tm = self.pkg._timer(0.01, read, "timer"); tm.start()
        threading.Thread(target=read, args=("bare",)).start()   # the control
        self.assertTrue(done.wait(5))
        self.assertEqual(seen["thread"], "profile")
        self.assertEqual(seen["timer"], "profile")
        self.assertEqual(seen["bare"], "root")


class SetupCommandCase(StatusCase):
    """`/signet setup` in the Hermes chat runs the same narration as the terminal command and
    returns it as text, so a human never needs a terminal (Chris, 2026-11-08)."""

    def _runtime(self, client):
        pkg = self.pkg
        case = self
        class Ctx:
            def __init__(self): self.set = {}
            def set_config(self, k, v): self.set[k] = v
        class RT:
            cfg = {"mmd_hours": 24, "keep_days": 9, "prune": True, "settle_minutes": 30,
                   "root_name": "Memory Mirror", "files_folder": "Files", "handle": "test-agent"}
            ctx = Ctx()
            started = 0
            def client(self): return client
            def status(self): return pkg.sd_status.Status(case.tmp / "data")
            def start(self): RT.started += 1
        return RT()

    def test_setup_without_a_code_narrates_the_first_steps_and_the_status(self):
        rt = self._runtime(self.client())
        lines = []
        self.pkg._setup(rt, None, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("Signet Drive for Hermes. Setup.", text)
        self.assertIn("both its halves", text)              # Hermes turns both on at install (Hlin, 11-08)
        self.assertIn("connected the Signet app", text)     # the account page's gate, in Chris's words
        self.assertIn("PRSN Accounts", text)
        self.assertIn("Status now.", text)
        self.assertNotIn(";", text.replace("plugins.entries", ""))   # Chris's standard on the prose
        self.assertNotIn("\u2014", text)

    def test_the_chat_form_names_the_chat_command_and_the_terminal_form_the_terminal(self):
        rt = self._runtime(self.client())
        chat, term = [], []
        self.pkg._setup(rt, None, out=chat.append, form="chat")
        self.pkg._setup(rt, None, out=term.append, form="terminal")
        self.assertTrue(any("type /signet setup again, this time followed by that code" in l for l in chat))
        self.assertTrue(any("run hermes signet-drive setup again, this time followed by that code" in l for l in term))
        self.assertTrue(any("Found, version " in l and "(" not in l for l in chat))   # the number, not the platform

    def test_a_second_setup_in_an_enrolled_profile_refuses_and_changes_nothing(self):
        # Bob's reading 1 on case 33 (2026-11-08): never switch an enrolled profile's identity silently
        rt = self._runtime(self.client())
        rt.cfg["handle"] = "already-enrolled-ai"
        lines = []
        self.pkg._setup(rt, "SOMECODE", out=lines.append, form="chat")
        text = "\n".join(lines)
        self.assertIn("already set up as already-enrolled-ai", text)
        self.assertIn("nothing was changed", text)
        self.assertEqual(rt.ctx.set, {})
        self.assertNotIn("Enrolling", text)

    def test_the_chat_form_hands_the_chat_back_before_the_enrolment(self):
        # Bob's reading 2 and Hlin's source read (enroll blocks through two human steps)
        rt = self._runtime(self.client())
        rt.cfg["handle"] = ""
        lines = []
        self.pkg._setup(rt, "SOMECODE", out=lines.append, form="chat")
        text = "\n".join(lines)
        self.assertIn("approve it on your account page", text.lower())
        self.assertNotIn("Enrolled as", text)     # the enrolment itself is the command's thread, not this call

    def test_about_prints_the_plain_words(self):
        rt = self._runtime(self.client())
        text = "\n".join(self.pkg._plain_words(rt))
        self.assertIn("What happens from here", text)
        self.assertIn("Secure Enclave", text)
        self.assertIn("Every 30 days", text)
        self.assertNotIn(";", text)
        self.assertEqual(len([l for l in text.splitlines() if l[:2] in ("1.","2.","3.","4.","5.","6.","7.","8.","9.")]), 9)   # a short numbered list, Chris's shape

    def test_the_code_is_found_inside_the_pages_pasted_block(self):
        x = self.pkg._extract_code
        self.assertEqual(x("A0qhoL3eJTahmggQTsWhNh5ncxch3jcxPOLkm2E10ZM"), "A0qhoL3eJTahmggQTsWhNh5ncxch3jcxPOLkm2E10ZM")
        block = ("You're being enrolled in Signet Drive by your guardian.\n"
                 "Run: signet enroll A0qhoL3eJTahmggQTsWhNh5ncxch3jcxPOLkm2E10ZM\n"
                 "This creates your keys in this Mac's Secure Enclave. If it fails, send your guardian the exact output.")
        self.assertEqual(x(block), "A0qhoL3eJTahmggQTsWhNh5ncxch3jcxPOLkm2E10ZM")
        self.assertIsNone(x(""))
        self.assertIsNone(x("please set me up with my drive"))

    def test_the_chat_setup_leaves_a_blank_line_between_every_line(self):
        rt = self._runtime(self.client())
        lines = []
        self.pkg._setup(rt, None, out=lines.append, form="chat")
        text = "\n\n".join(lines)
        self.assertNotIn("\n1.", text.replace("\n\n1.", ""))   # no item follows another without a blank line

    def test_setup_waits_for_the_approval_before_it_connects(self):
        # 2026-11-08: connect ran before the Guardian's passkey approval reached the server
        rt = self._runtime(self.client())
        seen = iter(["none", "none", "awaiting_pickup"])
        self.pkg._access_status = lambda _rt, _it=seen: next(_it, "awaiting_pickup")
        notes = []
        status = self.pkg._wait_for_approval(rt, notes.append, limit_s=5, poll_s=0.01)
        self.assertEqual(status, "awaiting_pickup")

    def test_setup_narration_is_identical_through_print_and_through_a_list(self):
        rt = self._runtime(self.client())
        a, b = [], []
        self.pkg._setup(rt, None, out=a.append)
        self.pkg._setup(rt, None, out=b.append)
        self.assertEqual(a, b)


class SettleCase(StatusCase):
    """Settle, then save (Chris, 2026-11-08 evening): a memory change starts the clock, a further
    change resets it, one pass runs when it runs out, and a session's end only starts a clock
    when the files differ from the newest point. The clock is the runtime's, so it is tested
    here through a Runtime with a fake Hermes home and the fake client."""

    def setUp(self):
        super().setUp()
        import os
        self.home = self.tmp / "hermes"
        (self.home / "memories").mkdir(parents=True)
        self._env_before = {k: os.environ.get(k) for k in ("HERMES_HOME", "SIGNET_FAKE_STORE")}
        os.environ["HERMES_HOME"] = str(self.home)            # _hermes_home() falls back to it outside Hermes
        os.environ["SIGNET_FAKE_STORE"] = str(self.store)     # the fake client's drive, inherited by its process

    def tearDown(self):
        import os
        for k, v in self._env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def _runtime(self, settle_minutes):
        vals = {"binary": str(FAKE), "handle": "test-agent", "settle_minutes": settle_minutes}
        class Ctx:
            def get_config(self, key, default=None):
                return vals.get(key, default)
        return self.pkg.Runtime(Ctx())

    def _write(self, text):
        (self.home / "memories" / "MEMORY.md").write_text(text)

    def _points(self):
        d = json.loads(self.store.read_text()) if self.store.exists() else {"files": {}}
        return sorted(p for p in d["files"] if p.startswith("Memory Mirror/") and p.endswith(".tar.gz"))

    def _memory_in(self, point):
        import tarfile
        blob = bytes.fromhex(json.loads(self.store.read_text())["files"][point]["blob"])
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            return tar.extractfile("memories/MEMORY.md").read().decode()

    def _settled(self, rt, limit_s=20.0):
        import time
        end = time.time() + limit_s
        while time.time() < end:
            if not rt.settle_pending() and rt.status().read().get("last_pass"):
                return True
            time.sleep(0.05)
        return False

    def test_a_burst_of_changes_is_one_point_holding_the_final_state(self):
        import time
        rt = self._runtime(settle_minutes=0.6 / 60)            # 0.6 s
        self._write("one\n"); rt.schedule_pass("after memory")
        time.sleep(0.3)
        self._write("two\n"); rt.schedule_pass("after memory")    # resets the clock
        self.assertTrue(rt.settle_pending())
        self.assertTrue(rt.status().read().get("settle_due"))
        self.assertIn("A copy is due at", rt.status().line({"last_check": {"at": "2026-01-01T00:00:00+00:00", "checks": []},
                                                           "settle_due": rt.status().read()["settle_due"]}))
        self.assertEqual(self._points(), [], "nothing is saved before the clock runs out")
        self.assertTrue(self._settled(rt))
        self.assertEqual(len(self._points()), 1, self._points())
        self.assertEqual(self._memory_in(self._points()[0]), "two\n")
        self.assertIsNone(rt.status().read().get("settle_due"))

    def test_a_session_end_starts_the_clock_only_on_a_change_and_never_resets_a_running_one(self):
        import time
        rt = self._runtime(settle_minutes=0.5 / 60)
        self._write("first\n")
        rt.run_pass("session_start")
        self.assertEqual(len(self._points()), 1)
        self.assertFalse(rt.schedule_if_changed("session_end"), "in step: no clock")
        self.assertFalse(rt.settle_pending())
        self._write("changed\n")
        self.assertTrue(rt.schedule_if_changed("session_end"))
        due = rt.status().read()["settle_due"]
        time.sleep(0.1)
        self.assertFalse(rt.schedule_if_changed("session_end"), "a running clock is not reset by the next turn")
        self.assertEqual(rt.status().read()["settle_due"], due)
        rt.status().write(last_pass=None)
        self.assertTrue(self._settled(rt))
        self.assertEqual(len(self._points()), 1, "the day's newer point replaced the older")
        self.assertEqual(self._memory_in(self._points()[0]), "changed\n")

    def test_a_due_time_already_past_says_the_change_waits_for_the_next_start(self):
        from datetime import timedelta
        rt = self._runtime(settle_minutes=30)
        st = rt.status()
        check = {"last_check": {"at": self.pkg.sd_status._now().isoformat(), "checks": []}}
        ahead = (self.pkg.sd_status._now() + timedelta(minutes=20)).isoformat()
        behind = (self.pkg.sd_status._now() - timedelta(minutes=5)).isoformat()
        self.assertIn("A copy is due at", st.line({**check, "settle_due": ahead}))
        past = st.line({**check, "settle_due": behind})
        self.assertNotIn("A copy is due at", past)
        self.assertIn("waits for the next session start", past)

    def test_the_line_drops_the_last_passs_verdict_while_a_change_waits(self):
        from datetime import timedelta
        rt = self._runtime(settle_minutes=30)
        st = rt.status()
        now = self.pkg.sd_status._now()
        base = {"last_check": {"at": now.isoformat(), "checks": []},
                "last_pass": {"at": (now - timedelta(minutes=50)).isoformat(), "status": "uploaded",
                              "line": "Mirror in step. Point 20261108T234756Z-f3764b holds 1 file(s)."}}
        quiet = st.line(base)
        self.assertIn("Mirror in step", quiet)
        ahead = st.line({**base, "settle_due": (now + timedelta(minutes=20)).isoformat()})
        self.assertNotIn("in step", ahead)
        self.assertIn("Last mirror", ahead)
        self.assertIn("A copy is due at", ahead)
        past = st.line({**base, "settle_due": (now - timedelta(minutes=5)).isoformat()})
        self.assertNotIn("in step", past)
        self.assertIn("waits for the next session start", past)

    def test_the_clock_is_a_daemon_so_a_short_session_is_not_held_open(self):
        rt = self._runtime(settle_minutes=30)
        self._write("x\n")
        rt.schedule_pass("after memory")
        try:
            self.assertTrue(rt._debounce.daemon)
            self.assertAlmostEqual(rt._debounce.interval, 1800.0)
        finally:
            rt._debounce.cancel()


if __name__ == "__main__":
    unittest.main()
