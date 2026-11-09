"""Tests for the Memory Mirror against the fake client.

Numbers in test names follow Bob's failure suite where a case is covered
(hermes-plugin-failure-suite v05). A green run here proves the plugin's
logic against the specification's shapes, not the real client.

The shape under test (2026-11-07, Chris): one file per restore point, fewer
points, a ceiling, and the mirror removing only its own retired points.
"""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from signet_cli import SignetClient, SignetError  # noqa: E402
import sd_mirror as M  # noqa: E402

FAKE = HERE / "fake_signet.py"
T0 = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """An injectable now, moved by the tests."""

    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + timedelta(**kw)


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sdh-"))
        self.home = self.tmp / "hermes"
        (self.home / "memories").mkdir(parents=True)
        (self.home / "memories" / "MEMORY.md").write_text("§ first fact\n")
        (self.home / "memories" / "USER.md").write_text("§ the human\n")
        # A skill of the agent's own, present so the tests prove it is NOT copied (Chris, 2026-11-08 evening).
        own = self.home / "skills" / "my-skill"
        (own / "scripts").mkdir(parents=True)
        (own / "SKILL.md").write_text("# my skill\n")
        (own / "scripts" / "run.sh").write_text("echo hi\n")
        self.store = self.tmp / "drive.json"
        self.env = {"SIGNET_FAKE_STORE": str(self.store)}
        self.data = self.tmp / "plugin-data"
        self.clock = Clock()
        self.client = SignetClient(binary=str(FAKE), handle="test-agent", extra_env=self.env)
        self.mirror = M.Mirror(self.client, self.home, self.data, now=self.clock)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def drive(self):
        return json.loads(self.store.read_text()) if self.store.exists() else {"files": {}, "folders": {}}

    def drive_files(self):
        return sorted(self.drive()["files"].keys())

    def points(self):
        return [p for p in self.drive_files() if p.startswith("Memory Mirror/") and p.endswith(".tar.gz")]

    def members(self, path):
        blob = bytes.fromhex(self.drive()["files"][path]["blob"])
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            return sorted(tar.getnames())

    def fail(self, spec, n="1"):
        self.client.extra_env = {**self.env, "SIGNET_FAKE_FAIL": spec, "SIGNET_FAKE_FAIL_N": n}

    def unfail(self):
        self.client.extra_env = dict(self.env)
        d = self.drive()
        d["fails_left"] = None
        self.store.write_text(json.dumps(d))

    def write_memory(self, text):
        (self.home / "memories" / "MEMORY.md").write_text(text)

    # ---- the point
    def test_first_point_bundles_the_two_memory_files_and_no_skill_as_one_file(self):
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(sorted(r.uploaded), ["memories/MEMORY.md", "memories/USER.md"])
        self.assertEqual(len(self.points()), 1)
        self.assertEqual(self.drive_files(), self.points(), "nothing but point files in the drive")
        self.assertEqual(self.drive()["folders"], {"Memory Mirror": self.drive()["folders"]["Memory Mirror"]},
                         "one folder, no folders under it")
        self.assertEqual(self.members(self.points()[0]),
                         ["manifest.json", "memories/MEMORY.md", "memories/USER.md"])
        self.assertFalse(any(m.startswith("skills/") for m in self.members(self.points()[0])), "skills are not copied")
        m = self.mirror.latest_point()
        self.assertEqual(m.stamp, r.stamp)
        self.assertIn(r.stamp, self.mirror.index())

    def test_a_cached_manifest_from_the_old_shape_is_not_a_baseline(self):
        # The shape before 2026-11-07 left `latest-manifest.json` in the data folder with
        # entries pointing at copies/ folders. The same files unchanged must still get a
        # first point in the new shape, or a wipe would find nothing to restore from.
        local = M.scan(self.home)
        old = {"stamp": "20261107T165738Z-f54e28", "created_at": "2026-11-07T16:57:38+00:00", "kind": "pass",
               "entries": {rel: {"sha256": t.sha256, "size": t.size, "copy": f"copies/x/{rel}"} for rel, t in local.items()},
               "uploaded": [], "client_version": "", "note": ""}
        (self.data).mkdir(parents=True, exist_ok=True)
        (self.data / "latest-manifest.json").write_text(json.dumps(old))
        self.assertIsNone(self.mirror.latest_point(), "not ours, so not a baseline")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(len(self.points()), 1)
        self.assertEqual(self.mirror.latest_point().stamp, r.stamp)

    def test_a_refresh_that_finds_no_point_clears_the_cache(self):
        self.mirror.run_pass("session_start")
        d = self.drive(); d["files"] = {}; self.store.write_text(json.dumps(d))   # the human emptied the folder
        self.assertIsNone(self.mirror.latest_point(refresh=True))
        self.assertFalse((self.data / "latest-manifest.json").exists())

    def test_case_7_no_change_writes_nothing(self):
        self.mirror.run_pass("session_start")
        r = self.mirror.run_pass("after memory")
        self.assertEqual(r.status, "in_step")
        self.assertEqual(len(self.points()), 1)

    def test_every_change_makes_a_point_and_the_day_keeps_one_file(self):
        # Chris, 2026-11-08: no hourly limit (Hermes Desktop ends a session after every turn, so the
        # limit could never apply there); every change is saved and the day's newer point replaces the older.
        self.mirror.run_pass("session_start")
        self.clock.advance(minutes=2)
        self.write_memory("§ second fact\n")
        r = self.mirror.run_pass("after memory")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(len(self.points()), 1, "one file per day: the newer point replaced the older")
        self.assertEqual(M.stamp_of(self.points()[0]), r.stamp)
        self.assertEqual(len(r.pruned), 1)
        self.assertIn("memories/MEMORY.md", self.members(self.points()[-1]))
        self.clock.advance(minutes=3)
        r = self.mirror.run_pass("session_end")
        self.assertEqual(r.status, "in_step", "nothing changed since, so the turn's end uploads nothing")
        self.assertEqual(len(self.points()), 1)

    def test_case_6_local_wipe_deletes_nothing_and_asks(self):
        self.mirror.run_pass("session_start")
        before = self.drive_files()
        shutil.rmtree(self.home / "memories")
        self.clock.advance(minutes=1)
        r = self.mirror.run_pass("session_end")
        self.assertEqual(r.status, "needs_you", r)
        self.assertEqual(sorted(r.missing), ["memories/MEMORY.md", "memories/USER.md"])
        self.assertEqual(self.drive_files(), before, "nothing deleted, nothing added")
        self.assertIn("Restore them or confirm the removal", r.line())
        # the human confirms the removal: the next point records it and the question is gone
        self.mirror.accept_missing(r.missing)
        self.clock.advance(minutes=1)
        r2 = self.mirror.run_pass("accepted removal")
        self.assertEqual(r2.status, "uploaded", r2)
        self.assertNotIn("memories/MEMORY.md", self.members(self.points()[-1]))
        self.assertIn("removal accepted", self.mirror.latest_point().note)

    def test_case_10_interrupted_upload_leaves_no_point_and_the_next_completes(self):
        self.fail("31:network_error", n="upload:1")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "behind", r)
        self.assertEqual(self.points(), [], "no file, so nothing a restore could choose")
        self.assertIsNone(self.mirror.latest_point())
        self.assertEqual(self.mirror.index(), {})
        self.unfail()
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(len(self.points()), 1)

    def test_locked_mac_reads_behind(self):
        self.fail("52:se_locked")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "behind")
        self.assertIn("locked", r.line())

    def test_case_12_quota_full_refuses_and_prunes_nothing(self):
        self.fail("18:insufficient_storage")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "refused")
        self.assertIn("full", r.line())
        self.assertEqual(self.points(), [])

    def test_case_16_read_only_grace_names_the_reason(self):
        self.fail("17:account_read_only")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "refused")
        self.assertIn("read-only", r.line())

    def test_case_11_two_passes_at_once_run_one(self):
        results, errors = [], []

        def go():
            try:
                results.append(self.mirror.run_pass("x"))
            except M.MirrorBusy as e:
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(results) + len(errors), 2)
        self.assertLessEqual(len(self.points()), 1)

    def test_root_is_found_under_folder_list_and_never_created_twice(self):
        self.mirror.run_pass("session_start")
        second = M.Mirror(self.client, self.home, self.data / "other", now=self.clock)
        second.run_pass("session_start")
        self.assertEqual([p for p in self.drive()["folders"] if "/" not in p].count("Memory Mirror"), 1)

    def test_a_pass_refuses_without_a_handle(self):
        self.client.handle = None
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "refused")
        self.assertIn("Not enrolled", r.reason)
        self.assertEqual(self.drive_files(), [])

    def test_wrapper_reads_json_errors(self):
        self.fail("26:not_found")
        with self.assertRaises(SignetError) as cm:
            self.client.file_list("Memory Mirror")
        self.assertEqual(cm.exception.exit_code, 26)
        self.assertEqual(cm.exception.code, "not_found")

    # ---- restore
    def test_case_8_restore_to_a_point_in_time(self):
        self.mirror.run_pass("session_start")                           # day 1: both files
        self.clock.advance(days=1)
        (self.home / "memories" / "USER.md").unlink()
        self.mirror.accept_missing(["memories/USER.md"])
        self.write_memory("§ day 2\n")
        self.mirror.run_pass("session_end")                             # day 2: USER gone (accepted), MEMORY edited
        day2 = self.mirror.latest_point().stamp
        self.clock.advance(days=1)
        self.write_memory("§ day 3, wrong\n")
        self.mirror.run_pass("session_end")
        out = self.mirror.restore(day2)
        self.assertTrue(out["applied"], out)
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ day 2\n")
        self.assertFalse((self.home / "memories" / "USER.md").exists(), "the day-2 point decides: USER.md stays gone")
        self.assertEqual(out["origin"], "this Mac")

    def test_restore_saves_the_present_first_and_checks_every_hash(self):
        self.mirror = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror.run_pass("session_start")
        stamp = self.mirror.latest_point().stamp
        self.clock.advance(hours=2)
        self.write_memory("§ newer\n")
        self.mirror.run_pass("session_end")
        n_before = len(self.points())
        self.write_memory("§ unsaved\n")                   # the present differs from the newest point
        out = self.mirror.restore(stamp)
        self.assertTrue(out["applied"], out)
        self.assertEqual(len(self.points()), n_before + 1, "the present state became a point first")
        self.assertEqual(self.mirror.latest_point().kind, "pre_restore")
        self.assertEqual(out["pre_restore"], self.mirror.latest_point().stamp)
        # a tampered point: the member no longer matches the manifest inside it
        d = self.drive()
        path = [p for p in d["files"] if M.stamp_of(p) == stamp][0]
        blob = bytes.fromhex(d["files"][path]["blob"])
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
            for mem in src.getmembers():
                data = src.extractfile(mem).read()
                if mem.name == "memories/MEMORY.md":
                    data = b"\xa7 planted\n"
                    mem.size = len(data)
                dst.addfile(mem, io.BytesIO(data))
        d["files"][path]["blob"] = buf.getvalue().hex()
        self.store.write_text(json.dumps(d))
        self.write_memory("§ newer again\n")
        with self.assertRaises(ValueError):
            self.mirror.restore(stamp, yes=True)
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ newer again\n", "nothing written")

    def test_case_9_a_point_this_mac_did_not_make_is_restored_only_on_a_yes(self):
        self.mirror = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror.run_pass("session_start")
        # a second Mac (an empty index) made a point; this Mac did not
        other = M.Mirror(self.client, self.home, self.data / "other-mac", now=self.clock)
        self.clock.advance(hours=2)
        self.write_memory("§ from the other Mac\n")
        other.run_pass("session_end")
        foreign = other.latest_point().stamp
        self.write_memory("§ mine\n")
        plan = self.mirror.plan_restore(foreign)
        self.assertEqual(plan.origin, "unknown")
        out = self.mirror.restore(foreign)
        self.assertFalse(out["applied"])
        self.assertEqual(out["origin"], "unknown")
        self.assertTrue(self.mirror.restore(foreign, yes=True)["applied"])
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ from the other Mac\n")

    # ---- the ceiling
    def _points_over(self, days, per_day=1):
        """Make one point a day (or more) going back `days` days from T0, through the mirror."""
        start = T0 - timedelta(days=days)
        self.clock.t = start
        n = 0
        for d in range(days + 1):
            for k in range(per_day):
                self.clock.t = start + timedelta(days=d, hours=2 * k)
                self.write_memory(f"§ fact {d}.{k}\n")
                r = self.mirror.run_pass("session_end")
                self.assertEqual(r.status, "uploaded", r)
                n += 1
        self.clock.t = T0
        return n

    def test_the_keep_rule_is_one_per_day_nine_days_back_newest_always(self):
        m = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror = m
        self._points_over(days=40, per_day=3)        # three a day for 41 days: 123 points
        stamps = sorted(m.index())
        kept = m._keep(stamps, T0)
        by_age = {s: (T0.date() - M.stamp_time(s).date()).days for s in stamps}
        ages = sorted(by_age[s] for s in kept)
        self.assertEqual(ages, list(range(10)), "one per day, today and nine back")
        for s in kept:
            self.assertEqual(s, max(t for t in stamps if by_age[t] == by_age[s]), "the day's newest")
        self.assertIn(max(stamps), kept)
        self.assertEqual(len(m.retirable(T0)), 123 - 10)

    def test_the_keep_rule_with_sparse_points_and_the_newest_whatever_its_age(self):
        m = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror = m
        for back in (30, 9, 0):
            self.clock.t = T0 - timedelta(days=back)
            self.write_memory(f"§ {back}\n")
            self.assertEqual(m.run_pass("session_end").status, "uploaded")
        self.clock.t = T0
        ages = sorted((T0.date() - M.stamp_time(s).date()).days for s in m._keep(sorted(m.index()), T0))
        self.assertEqual(ages, [0, 9], "today and the nine-day one stay; the 30-day one retires")
        # an agent idle for forty days keeps its last point
        self.clock.t = T0 + timedelta(days=40)
        kept = m._keep(sorted(m.index()), self.clock.t)
        self.assertEqual(kept, {max(m.index())}, "the newest point is kept whatever its age")
        self.assertEqual(m.keep_days, 9)
        self.assertEqual(M.Mirror(self.client, self.home, self.data, keep_days=3).keep_days, 3, "the one number is a setting")

    def test_thinning_removes_only_retired_points_of_its_own_and_keeps_the_newest(self):
        m = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror = m
        self._points_over(days=20, per_day=2)
        before = set(self.points())
        # a human's file that only looks like a point, and a pinned (renamed) point of ours
        d = self.drive()
        human = "Memory Mirror/memory-20260101T000000Z-abcdef.tar.gz"
        d["files"][human] = dict(d["files"][sorted(d["files"])[0]])
        oldest = sorted(p for p in d["files"] if p != human)[0]
        pinned = oldest.replace(".tar.gz", "-pinned.tar.gz")
        d["files"][pinned] = d["files"].pop(oldest)
        self.store.write_text(json.dumps(d))
        retirable = m.retirable(T0)
        self.assertNotIn(human.split("/")[1], retirable)
        self.assertNotIn(pinned.split("/")[1], retirable)
        self.assertNotIn(max(m.index())[0:0] + m.index()[max(m.index())]["name"], retirable, "the newest is never retired")
        removed = m.thin(T0)
        self.assertEqual(sorted(removed), sorted(retirable))
        after = set(self.points())
        self.assertIn(human, after)
        self.assertIn(pinned, after)
        self.assertTrue(all(("Memory Mirror/" + n) in before for n in removed))
        self.assertEqual(len(after), len(before) + 1 - len(removed))
        # the index forgot them, and a point this plugin made but the human renamed counts as not ours
        self.assertFalse(any(e["name"] in removed for e in m.index().values()))
        self.assertEqual(m.others(), 2)

    def test_thinning_runs_at_a_point_when_pruning_is_on_and_only_counts_when_off(self):
        off = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror = off
        self._points_over(days=12, per_day=1)
        r = off.run_pass("after memory")      # in step; counts what waits
        self.assertEqual(r.status, "in_step")
        self.assertGreater(r.retirable, 0)
        self.assertIn("wait for removal", r.line())
        n = len(self.points())
        on = M.Mirror(self.client, self.home, self.data, prune=True, now=self.clock)
        self.clock.advance(hours=2)
        self.write_memory("§ one more\n")
        r = on.run_pass("session_end")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(len(r.pruned), r.pruned and len(r.pruned))
        self.assertGreater(len(r.pruned), 0)
        self.assertEqual(len(self.points()), n + 1 - len(r.pruned))
        self.assertIn("removed", r.line())

    def test_a_quiet_pass_thins_a_folder_left_over_the_rule(self):
        off = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        off.run_pass("session_start")
        self.clock.advance(hours=2)
        self.write_memory("§ later\n")
        off.run_pass("session_end")
        self.assertEqual(len(self.points()), 2, "pruning off: two files for one day")
        on = M.Mirror(self.client, self.home, self.data, prune=True, now=self.clock)
        r = on.run_pass("after memory")                    # nothing changed
        self.assertEqual(r.status, "in_step", r)
        self.assertEqual(len(r.pruned), 1)
        self.assertEqual(len(self.points()), 1, "the quiet pass brought the folder to one per day")
        self.assertIn("removed", r.line())

    def test_a_failed_removal_never_undoes_the_point(self):
        on = M.Mirror(self.client, self.home, self.data, prune=True, now=self.clock)
        self.mirror = on
        self._points_over(days=12, per_day=1)
        n = len(self.points())
        self.clock.advance(hours=2)
        self.write_memory("§ one more\n")
        self.fail("31:network_error", n=f"delete:{self.drive().get('deletes', 0) + 1}")      # the ceiling's next removal fails
        r = on.run_pass("session_end")
        self.assertEqual(r.status, "uploaded", r)
        self.assertEqual(r.pruned, [])
        self.assertEqual(len(self.points()), n + 1, "the point stands; nothing was removed")
        self.assertIn("thin_error", on.state())
        self.unfail()
        self.clock.advance(hours=2)
        self.write_memory("§ and another\n")
        r = on.run_pass("session_end")
        self.assertGreater(len(r.pruned), 0, "the next point thins")

    def test_a_restore_from_a_saved_state_makes_no_duplicate_point(self):
        self.mirror = M.Mirror(self.client, self.home, self.data, prune=False, now=self.clock)
        self.mirror.run_pass("session_start")
        first = self.mirror.latest_point().stamp
        self.clock.advance(hours=2)
        self.write_memory("§ later\n")
        self.mirror.run_pass("session_end")
        n = len(self.points())
        out = self.mirror.restore(first)                  # the present equals the newest point already
        self.assertTrue(out["applied"], out)
        self.assertIsNone(out.get("pre_restore"), "no pre-restore point when the newest point already holds the present")
        self.assertEqual(len(self.points()), n, "no duplicate file")
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ first fact\n")

    def test_a_pre_restore_point_does_not_thin_away_the_point_being_restored(self):
        on = M.Mirror(self.client, self.home, self.data, prune=True, now=self.clock)
        self.mirror = on
        on.run_pass("session_start")
        for k in range(2):
            self.clock.advance(hours=2)
            self.write_memory(f"§ later {k}\n")
            self.assertEqual(on.run_pass("session_end").status, "uploaded")
        self.assertEqual(len(self.points()), 1, "one file per day: the newer replaced the older")
        target = M.stamp_of(on.list_points()[0])
        self.write_memory("§ unsaved\n")
        out = on.restore(target)
        self.assertTrue(out["applied"], out)
        self.assertIn(M.point_name(target), [p.split("/")[1] for p in self.points()], "the restored point is still there")
        self.assertEqual(len(self.points()), 2, "one over the rule until the next ordinary point")
        self.clock.advance(hours=2)
        self.write_memory("§ after\n")
        r = on.run_pass("session_end")
        self.assertEqual(r.status, "uploaded")
        self.assertEqual(len(self.points()), 2, "the day's point and the restore's undo point, which stands outside one-per-day")
        self.assertEqual(r.undo, 1)
        self.assertIn("undo point", r.line())

    def test_bobs_undo_a_restore_the_same_day_can_be_undone_after_the_next_point(self):
        # Bob, 2026-11-07: point B; the agent writes C unsaved; restore to B, whose pre-restore
        # point saves C; the next point an hour later on the same UTC day must not erase C.
        on = M.Mirror(self.client, self.home, self.data, prune=True, now=self.clock)
        self.mirror = on
        on.run_pass("session_start")
        b = on.latest_point().stamp
        self.clock.advance(hours=1)
        self.write_memory("§ C, the newest memory, unsaved\n")
        out = on.restore(b)
        self.assertTrue(out["applied"], out)
        undo = out["pre_restore"]
        self.assertTrue(M.point_name(undo, "pre_restore").endswith("-before-restore.tar.gz"))
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ first fact\n")
        self.clock.advance(hours=1)
        self.write_memory("§ D, after the restore\n")
        r = on.run_pass("session_end")                   # the same UTC day
        self.assertEqual(r.status, "uploaded", r)
        names = [p.split("/")[1] for p in self.points()]
        self.assertIn(M.point_name(undo, "pre_restore"), names, "the undo point survives the day's next point")
        self.assertEqual(len(names), 2, "the day's point and the undo point")
        back = on.restore(undo)
        self.assertTrue(back["applied"], back)
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ C, the newest memory, unsaved\n", "C is back")
        # the undo point retires by age like any point
        self.clock.advance(days=10)
        self.write_memory("§ ten days on\n")
        on.run_pass("session_end")
        self.assertNotIn(M.point_name(undo, "pre_restore"), [p.split("/")[1] for p in self.points()])

    def test_a_killed_pass_whose_child_finished_is_adopted_at_the_next_pass(self):
        # Bob, run 3, case 10: the pass dies after `file upload` started; the client child
        # finishes on its own. The index already holds the point as pending, so the next pass
        # adopts it and the ceiling governs it. A pending entry with no file is forgotten.
        self.mirror.run_pass("session_start")
        idx = self.mirror.index()
        (stamp,) = idx.keys()
        idx[stamp]["status"] = "pending"                                   # as the dying pass left it
        (self.data / "latest-manifest.json").unlink()                       # never written by the dying pass
        self.mirror._pending_manifest_path(stamp).write_text(json.dumps({
            "stamp": stamp, "created_at": idx[stamp]["created_at"], "kind": "pass",
            "entries": {rel: {"sha256": t.sha256, "size": t.size} for rel, t in M.scan(self.home).items()},
            "client_version": "", "note": ""}))
        ghost = M.new_stamp(T0 - timedelta(hours=1))
        idx[ghost] = {"name": M.point_name(ghost), "sha256": "0" * 64, "size": 1, "created_at": "",
                      "kind": "pass", "files": 0, "status": "pending"}   # an upload that never completed
        self.mirror._save_index(idx)
        self.assertIsNone(self.mirror.latest_point(), "a pending point is not a baseline until confirmed")
        r = self.mirror.run_pass("after memory")
        idx = self.mirror.index()
        self.assertEqual(idx[stamp]["status"], "uploaded", "adopted")
        self.assertNotIn(ghost, idx, "forgotten")
        rec = self.mirror.state()["reconciled"][-1]
        self.assertEqual(rec["adopted"], [M.point_name(stamp)])
        self.assertEqual(rec["dropped"], [ghost])
        self.assertEqual(self.mirror.others(), 0, "an adopted point is ours, not foreign")
        self.assertEqual(r.status, "in_step", "the adopted point's manifest is the baseline, so nothing is uploaded again (Bob, run 4)")
        self.assertEqual(self.mirror.latest_point().stamp, stamp)
        self.assertFalse(self.mirror._pending_manifest_path(stamp).exists())
        self.assertFalse(self.mirror._pending_manifest_path(ghost).exists())

    def test_a_stale_build_temp_is_swept_at_the_next_pass(self):
        stale = self.data / M.point_name(M.new_stamp(T0 - timedelta(hours=3))) 
        stale = stale.with_name(stale.name + ".tmp")
        self.data.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"x" * 1024)
        self.mirror.run_pass("session_start")
        self.assertFalse(stale.exists())

    def test_an_accepted_removal_clears_once_its_point_is_written(self):
        self.mirror.run_pass("session_start")
        (self.home / "memories" / "USER.md").unlink()
        self.mirror.accept_missing(["memories/USER.md"])
        self.assertEqual(self.mirror.state()["accepted_missing"], ["memories/USER.md"])
        self.assertEqual(self.mirror.run_pass("accepted removal").status, "uploaded")
        self.assertEqual(self.mirror.state()["accepted_missing"], [], "recorded in a point, so no longer a question")

class RunOneCase(MirrorCase):
    """Bob's run 1 findings, kept green across the shape change."""

    def test_finding_1_restore_after_a_wipe_is_not_blocked(self):
        self.mirror.run_pass("session_start")
        stamp = self.mirror.latest_point().stamp
        shutil.rmtree(self.home / "memories")
        self.assertEqual(self.mirror.run_pass("session_end").status, "needs_you")
        out = self.mirror.restore(stamp)
        self.assertTrue(out["applied"], out)
        self.assertEqual((self.home / "memories" / "MEMORY.md").read_text(), "§ first fact\n")
        self.assertEqual(self.mirror.run_pass("session_end").status, "in_step")

    def test_finding_2_an_accepted_removal_clears_when_the_file_returns(self):
        self.mirror.run_pass("session_start")
        (self.home / "memories" / "USER.md").unlink()
        self.mirror.accept_missing(["memories/USER.md"])
        self.assertEqual(self.mirror.run_pass("accepted removal").status, "uploaded")
        (self.home / "memories" / "USER.md").write_text("§ back\n")
        self.clock.advance(hours=2)
        self.assertEqual(self.mirror.run_pass("after memory").status, "uploaded")
        self.assertEqual(self.mirror.state().get("accepted_missing"), [])
        (self.home / "memories" / "USER.md").unlink()
        self.assertEqual(self.mirror.run_pass("session_end").status, "needs_you", "a second wipe is reported")

    def test_case_11_two_processes_make_one_point(self):
        import subprocess
        self.write_memory("§ changed\n")
        code = (
            "import sys; sys.path.insert(0, %r); import sd_mirror as M; from signet_cli import SignetClient; "
            "c = SignetClient(binary=%r, handle='test-agent', extra_env=%r); "
            "m = M.Mirror(c, %r, %r); "
            "import time; time.sleep(0.2); "
            "\n"
            "try:\n    print(m.run_pass('x').status)\nexcept M.MirrorBusy:\n    print('busy')\n"
        ) % (str(HERE.parent), str(FAKE), self.env, str(self.home), str(self.data))
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) for _ in range(2)]
        outs = [p.communicate()[0].strip() for p in procs]
        self.assertEqual(sorted(outs).count("uploaded"), 1, outs)
        self.assertEqual(len(self.points()), 1)

    def test_ensure_root_failure_is_a_result_not_an_exception(self):
        self.fail("31:network_error", n="2")
        r = self.mirror.run_pass("session_start")
        self.assertEqual(r.status, "behind", r)
        self.assertIn("did not answer", r.line())


class EmptyProfileCase(unittest.TestCase):
    """A fresh profile has no memory files until the agent saves one; the pass says so instead
    of "in step" (the demo agent's first check, 2026-11-08)."""

    def test_a_profile_with_no_memory_files_reads_nothing_to_copy_yet(self):
        tmp = Path(tempfile.mkdtemp(prefix="sde-"))
        try:
            home = tmp / "hermes"; (home / "memories").mkdir(parents=True)
            store = tmp / "drive.json"
            client = SignetClient(binary=str(FAKE), handle="empty-agent", extra_env={"SIGNET_FAKE_STORE": str(store)})
            m = M.Mirror(client, home, tmp / "data")
            r = m.run_pass("session_start")
            self.assertEqual(r.status, "in_step")
            self.assertEqual(r.tracked, 0)
            self.assertIn("Nothing to copy yet", r.line())
            self.assertNotIn(";", r.line())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
