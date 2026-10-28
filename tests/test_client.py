"""Tests for client/orca-fleet with a fake Orca CLI + SSH boundary."""

import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("fleet", str(ROOT / "client" / "orca-fleet"))
_spec = importlib.util.spec_from_loader("fleet", _loader)
fleet = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleet)

RELEASES = [
    {"tag_name": "v1.4.239", "prerelease": False, "draft": False, "published_at": "x"},
    {"tag_name": "v1.4.238", "prerelease": False, "draft": False, "published_at": "x"},
    {"tag_name": "v1.4.240", "prerelease": True, "draft": False, "published_at": "x"},
    {"tag_name": "mobile-android-v0.0.58", "prerelease": False, "draft": False, "published_at": "x"},
]


class FakeServer:
    def __init__(self, name, version="1.4.229", busy=0, local=False):
        self.name, self.version, self.busy, self.local = name, version, busy, local
        self.state = {}
        self.prepared = set()
        self.apply_result = "succeeded"   # what the worker ends with
        self.unreachable_polls = 0        # simulate disconnects during the restart
        self.ssh_down = False
        self.prepare_fail = None
        self.client_sees = None           # override version seen over Orca after update
        self.applies = 0
        self.helper_self_update = None   # response of the helper `self-update` (None = up to date)


class FakeEnv(fleet.Env):
    def __init__(self, servers, cfg_extra=None):
        super().__init__({"servers": [({"name": s.name, "local": True} if s.local else
                                       {"name": s.name, "environment": s.name, "ssh": s.name})
                                      | (cfg_extra or {}).get(s.name, {}) for s in servers]})
        self.servers = {s.name: s for s in servers}
        self.calls = []
        self.lock = threading.Lock()
        self.active_prepares = 0
        self.max_parallel_prepares = 0
        self.orca_calls = []
        self.tasks = {}
        self.notifications = []

    def releases(self):
        return RELEASES

    def sleep(self, s):
        pass

    def notify(self, argv, payload, outcome):
        self.notifications.append((argv, json.loads(payload), outcome))

    def orca(self, args, timeout=45):
        self.orca_calls.append(args)
        if args[0] == "status":
            if "--environment" in args:
                s = self.servers[args[args.index("--environment") + 1]]
            else:
                s = next((x for x in self.servers.values() if x.local), None)
                if s is None:
                    return {"ok": True, "result": {"runtime": {"reachable": True, "state": "ready",
                                                               "appVersion": "1.4.229"}}}
            if s.ssh_down:
                return {"ok": False, "error": {"message": "remote_runtime_unavailable"}}
            v = s.client_sees or s.version
            return {"ok": True, "result": {"runtime": {"reachable": True, "state": "ready", "appVersion": v,
                                                       "remoteUpdateSupport": {"installMode": "unsupported-headless-serve"}}}}
        if args[:2] == ["orchestration", "run-create"]:
            return {"ok": True, "result": {"run": {"id": "run_1"}}}
        if args[:2] == ["orchestration", "task-create"]:
            tid = f"task_{len(self.tasks) + 1}"
            self.tasks[tid] = {"title": args[args.index("--task-title") + 1], "updates": []}
            return {"ok": True, "result": {"task": {"id": tid}}}
        if args[:2] == ["orchestration", "task-update"]:
            tid = args[args.index("--id") + 1]
            self.tasks[tid]["updates"].append(args[args.index("--status") + 1])
            return {"ok": True, "result": {}}
        if args[:2] == ["host", "list"]:
            return {"ok": True, "result": {"hosts": [{"kind": "local", "name": "this machine"}] +
                                           [{"kind": "environment", "name": n} for n, x in self.servers.items()
                                            if not x.local]}}
        raise AssertionError(args)

    def ssh_helper(self, server, args, sudo, timeout=900):
        s = self.servers.get(server["name"])
        if s is None:
            return {"ok": False, "error": {"code": "helper_missing", "message": "No such file"}}
        assert bool(server.get("local")) == s.local
        with self.lock:
            self.calls.append((s.name, args[0], sudo))
        if s.ssh_down:
            return {"ok": False, "error": {"code": "ssh_failed", "message": "unreachable"}}
        cmd = args[0]
        if cmd == "self-update":
            return s.helper_self_update or {"ok": True, "changed": False, "commit": "c" * 40}
        if cmd == "status":
            if s.unreachable_polls > 0:
                s.unreachable_polls -= 1
                return {"ok": False, "error": {"code": "ssh_failed", "message": "connection reset"}}
            return {"ok": True, "installed_version": s.version,
                    "service": {"ActiveState": "active", "SubState": "running",
                                **({"kind": "desktop"} if s.local else {})},
                    "activity": {"known": True, "agents": s.busy, "busy": [{}] * s.busy},
                    "state": s.state, "worker_active": False}
        if cmd == "prepare":
            assert sudo
            if "--dry-run" in args:
                return {"ok": True, "would_fetch": [f"orca-ide_{args[2]}_amd64.deb"]}
            with self.lock:
                self.active_prepares += 1
                self.max_parallel_prepares = max(self.max_parallel_prepares, self.active_prepares)
            time.sleep(0.05)
            with self.lock:
                self.active_prepares -= 1
            if s.prepare_fail:
                return {"ok": False, "error": {"code": s.prepare_fail}}
            s.prepared.add(args[2])
            return {"ok": True}
        if cmd == "apply":
            assert sudo
            s.applies += 1
            v, mode = args[2], args[4]
            if s.busy and mode == "safe":
                return {"ok": True, "result": "deferred", "blockers": ["busy"], "deferrals": ["busy"]}
            op = f"op{s.applies:010d}"
            s.state = {"op_id": op, "op": "update", "phase": "done", "result": s.apply_result, "target": v}
            if s.apply_result == "succeeded":
                s.version = v
            return {"ok": True, "result": "started", "op_id": op, "unit": "orca-serve-update-worker.service"}
        raise AssertionError(args)


def run(env, argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fleet.main(argv, env=env)
    return rc, buf.getvalue()


class ClientTests(unittest.TestCase):
    def test_target_resolution_ignores_prerelease_and_mobile_tags(self):
        env = FakeEnv([FakeServer("a")])
        self.assertEqual(fleet.resolve_target(env, "latest")["version"], "1.4.239")
        with self.assertRaises(SystemExit):
            fleet.resolve_target(env, "1.4.240")
        with self.assertRaises(SystemExit):
            fleet.resolve_target(env, "1.4.239;id")

    def test_status_table(self):
        env = FakeEnv([FakeServer("a"), FakeServer("b", "1.4.239")])
        rc, out = run(env, ["status"])
        self.assertEqual(rc, 0)
        self.assertIn("target: 1.4.239", out)
        lines = {l.split()[0]: l for l in out.splitlines()[3:] if l.strip()}
        self.assertIn("yes", lines["a"])  # needs update
        self.assertRegex(lines["b"], r"1\.4\.239\s+no")

    def test_status_shows_version_drift(self):
        env = FakeEnv([FakeServer("a"), FakeServer("b", "1.4.239"), FakeServer("c", "1.4.239"), FakeServer("d")])
        env.servers["d"].ssh_down = True
        rc, out = run(env, ["status"])
        self.assertEqual(rc, 0)
        self.assertIn("fleet: 1.4.239 x2 (b, c), 1.4.229 x1 (a)", out)
        self.assertIn("behind 1.4.239: a", out)
        self.assertIn("version unknown: d", out)
        self.assertIn("not reachable over Orca: d", out)
        self.assertEqual(run(env, ["status", "--check"])[0], 1)
        ok = FakeEnv([FakeServer("a", "1.4.239"), FakeServer("b", "1.4.239")])
        rc, out = run(ok, ["status", "--check"])
        self.assertEqual(rc, 0)
        self.assertIn("fleet: all 2 server(s) at 1.4.239", out)
        self.assertNotIn("DRIFT", out)
        rc, out = run(env, ["status", "--json"])
        fl = json.loads(out)["fleet"]
        self.assertEqual((fl["behind"], fl["consistent"]), (["a"], False))

    def test_status_summarizes_missing_releases(self):
        body = ("## Faster and safer\n\nIntro.\n\n## The short version\n\n- **Speed:** Terminals are faster.\n"
                "- **Fixes:** Many fixes.\n\n**Phone updates:** Phones redraw less.\n\n"
                "## Compatibility & upgrade notes\n\n- **Chat:** Restart chats.\n\n## Product experience\n\n"
                "### Terminals\n\n*Lighter terminals.*\n\n"
                "* Use less idle CPU by @a-b in https://github.com/stablyai/orca/pull/123\n"
                "* Fix Korean input by @x and @y in https://github.com/stablyai/orca/pull/124\n\n"
                "### Tests, builds & maintenance\n\n<details>\n<summary>All</summary>\n\n"
                "* test: drop suites by @a in https://github.com/stablyai/orca/pull/9\n\n</details>\n\n"
                "## New Contributors\n* @z made their first contribution\n")
        rels = [{"tag_name": "v1.4.239", "prerelease": False, "draft": False, "published_at": "2026-10-02T00:00:00Z",
                 "html_url": "https://example/v1.4.239", "body": body},
                {"tag_name": "v1.4.238", "prerelease": False, "draft": False, "published_at": "2026-10-01T00:00:00Z",
                 "body": "Plain notes\n- one fix\n"},
                {"tag_name": "v1.4.237", "prerelease": False, "draft": False, "published_at": "x", "body": "- old\n"}]
        env = FakeEnv([FakeServer("a", "1.4.237"), FakeServer("b", "1.4.238")])
        env.releases = lambda: rels
        rc, out = run(env, ["status"])
        self.assertEqual(rc, 0)
        self.assertIn("1.4.238 (2026-10-01; missing on a)", out)
        self.assertIn("1.4.239 (2026-10-02; missing on a, b)  Faster and safer", out)
        self.assertIn("    - Speed: Terminals are faster.", out)
        self.assertIn("    - one fix", out)
        self.assertIn("    ! Chat: Restart chats.", out)
        self.assertIn("    - Phone updates: Phones redraw less.", out)
        self.assertIn("    [Terminals] Lighter terminals.\n      * Use less idle CPU (#123)\n"
                      "      * Fix Korean input (#124)\n", out)
        self.assertIn("[Tests, builds & maintenance] 1 item(s) omitted", out)
        self.assertNotIn("test: drop suites", out)
        self.assertNotIn("first contribution", out)
        short = run(env, ["status", "--changes", "short"])[1]
        self.assertIn("! Chat: Restart chats.", short)
        self.assertNotIn("[Terminals]", short)
        self.assertNotIn("- old", out)
        self.assertLess(out.index("  1.4.238 ("), out.index("  1.4.239 ("))
        changes = json.loads(run(env, ["status", "--json"])[1])["changes"]
        self.assertEqual([c["version"] for c in changes], ["1.4.238", "1.4.239"])
        self.assertEqual(changes[1]["notes"], ["Chat: Restart chats."])
        self.assertEqual([(x["name"], len(x["items"])) for x in changes[1]["sections"]],
                         [("Terminals", 2), ("Tests, builds & maintenance", 1)])
        self.assertNotIn("changes an update", run(env, ["status", "--no-changes"])[1])
        rc, out = run(FakeEnv([FakeServer("a", "1.4.239")]), ["status"])
        self.assertNotIn("changes an update", out)

    def test_drift_prefers_version_seen_over_orca(self):
        s = FakeServer("a", "1.4.239")
        s.client_sees = "1.4.229"  # package installed but the running serve is still old
        rc, out = run(FakeEnv([s]), ["status"])
        self.assertIn("behind 1.4.239: a", out)

    def test_outcome_and_notify(self):
        cfg = {"notify_command": ["/bin/notify"]}

        def outcome(servers, argv=("update", "--yes")):
            env = FakeEnv(servers)
            env.cfg.update(cfg)
            rc, out = run(env, list(argv))
            self.assertEqual(len(env.notifications), 1, out)
            argv_, payload, oc = env.notifications[0]
            self.assertEqual((argv_, payload["outcome"]), (["/bin/notify"], oc))
            self.assertIn(f"outcome: {oc}", out)
            return oc, payload

        oc, payload = outcome([FakeServer("a", "1.4.239")])
        self.assertEqual(oc, "nothing_to_update")
        self.assertEqual(payload["servers"], {"skip": ["a"]})
        self.assertTrue(payload["fleet"]["consistent"])
        oc, payload = outcome([FakeServer("a"), FakeServer("b", "1.4.239")])
        self.assertEqual(oc, "updated")
        self.assertEqual(payload["servers"], {"succeeded": ["a"], "skip": ["b"]})
        self.assertTrue(payload["fleet"]["consistent"])
        oc, payload = outcome([FakeServer("a", busy=1), FakeServer("b")])
        self.assertEqual(oc, "deferred")
        self.assertEqual(payload["fleet"]["behind"], ["a"])
        self.assertEqual(outcome([FakeServer("a", busy=1)])[0], "deferred")  # nothing attempted
        rb = FakeServer("a")
        rb.apply_result = "rolled_back"
        oc, payload = outcome([rb])
        self.assertEqual((oc, payload["servers"]), ("rolled_back", {"rolled_back": ["a"]}))
        rb2, bad = FakeServer("a"), FakeServer("b")
        rb2.apply_result, bad.prepare_fail = "rolled_back", "checksum_mismatch"
        self.assertEqual(outcome([rb2, bad])[0], "failed")

    def test_no_notify_on_dry_run_or_without_config(self):
        env = FakeEnv([FakeServer("a")])
        run(env, ["update", "--yes"])
        env2 = FakeEnv([FakeServer("a")])
        env2.cfg["notify_command"] = ["/bin/notify"]
        run(env2, ["update", "--dry-run"])
        self.assertEqual(env.notifications + env2.notifications, [])

    def test_notify_command_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"notify_command": "curl x | sh", "servers": []}, f)
            with self.assertRaises(SystemExit):
                fleet.load_config(path)

    def test_plan_classifies_servers(self):
        env = FakeEnv([FakeServer("same", "1.4.239"), FakeServer("old"), FakeServer("busy", busy=1),
                       FakeServer("down")])
        env.servers["down"].ssh_down = True
        rc, out = run(env, ["plan"])
        self.assertIn("skip (already at target)", out)
        self.assertIn("defer (agents active; SAFE)", out)
        self.assertIn("unreachable", out)
        self.assertRegex(out, r"old\s+1\.4\.229.*update")
        self.assertFalse(any(c[1] in {"prepare", "apply"} for c in env.calls))

    def test_plan_needs_action_exit_code(self):
        env = FakeEnv([FakeServer("same", "1.4.239"), FakeServer("down")])
        env.servers["down"].ssh_down = True
        self.assertEqual(run(env, ["plan", "--needs-action"])[0], 1)
        self.assertEqual(run(FakeEnv([FakeServer("same", "1.4.239"), FakeServer("busy", busy=1)]),
                             ["plan", "--needs-action"])[0], 0)
        self.assertEqual(run(FakeEnv([FakeServer("old")]), ["plan", "--needs-action"])[0], 0)

    def test_dry_run_changes_nothing(self):
        env = FakeEnv([FakeServer("a"), FakeServer("b")])
        rc, out = run(env, ["update", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY RUN", out)
        self.assertFalse(any(c[1] == "apply" for c in env.calls))
        self.assertEqual(env.servers["a"].prepared, set())

    def test_update_requires_confirmation_without_tty(self):
        env = FakeEnv([FakeServer("a")])
        with self.assertRaises(SystemExit):
            run(env, ["update"])
        self.assertFalse(any(c[1] == "apply" for c in env.calls))

    def test_force_with_yes_needs_explicit_ack(self):
        env = FakeEnv([FakeServer("a")])
        with self.assertRaises(SystemExit):
            run(env, ["update", "--mode", "force", "--yes"])

    def test_parallel_prepare_sequential_apply(self):
        servers = [FakeServer(n) for n in "abcd"]
        env = FakeEnv(servers)
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 0, out)
        self.assertGreater(env.max_parallel_prepares, 1)
        self.assertTrue(all(s.version == "1.4.239" for s in servers))
        # every apply happens after all prepares, one at a time
        seq = [c for c in env.calls if c[1] in {"prepare", "apply"}]
        first_apply = next(i for i, c in enumerate(seq) if c[1] == "apply")
        self.assertTrue(all(c[1] == "prepare" for c in seq[:first_apply]))
        self.assertEqual([c[0] for c in seq if c[1] == "apply"], ["a", "b", "c", "d"])

    def test_control_host_updated_last(self):
        servers = [FakeServer("ctl"), FakeServer("b")]
        env = FakeEnv(servers, cfg_extra={"ctl": {"control": True}})
        run(env, ["update", "--yes"])
        self.assertEqual([c[0] for c in env.calls if c[1] == "apply"], ["b", "ctl"])

    def test_disconnect_during_restart_is_tolerated(self):
        s = FakeServer("a")
        s.unreachable_polls = 0
        env = FakeEnv([s])
        orig = env.ssh_helper

        def flaky(server, args, sudo, timeout=900):
            r = orig(server, args, sudo, timeout)
            if args[0] == "apply":
                s.unreachable_polls = 3  # next status polls fail like a restarting host
            return r

        env.ssh_helper = flaky
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 0, out)
        self.assertIn("waiting (no contact: ssh_failed)", out)
        self.assertIn("a: succeeded", out)

    def test_failure_stops_rollout(self):
        servers = [FakeServer("a"), FakeServer("b"), FakeServer("c")]
        servers[1].apply_result = "rolled_back"
        env = FakeEnv(servers)
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 1)
        self.assertEqual(servers[0].version, "1.4.239")
        self.assertEqual(servers[2].applies, 0)
        self.assertRegex(out, r"c\s.*not_attempted")

    def test_continue_on_error(self):
        servers = [FakeServer("a"), FakeServer("b")]
        servers[0].apply_result = "failed"
        env = FakeEnv(servers)
        rc, _ = run(env, ["update", "--yes", "--continue-on-error"])
        self.assertEqual(rc, 1)
        self.assertEqual(servers[1].version, "1.4.239")

    def test_prepare_failure_excludes_server(self):
        servers = [FakeServer("a"), FakeServer("b")]
        servers[0].prepare_fail = "checksum_mismatch"
        env = FakeEnv(servers)
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 1)
        self.assertEqual(servers[0].applies, 0)
        self.assertEqual(servers[1].version, "1.4.239")
        self.assertIn("PREPARE FAILED", out)

    def test_independent_client_verification(self):
        s = FakeServer("a")
        s.client_sees = "1.4.229"  # server claims success; Orca connection disagrees
        env = FakeEnv([s])
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("unverified", out)

    def test_force_mode_updates_busy_server(self):
        s = FakeServer("a", busy=2)
        env = FakeEnv([s])
        rc, _ = run(env, ["update", "--yes", "--mode", "force", "--i-understand-force"])
        self.assertEqual(rc, 0)
        self.assertEqual(s.version, "1.4.239")

    def test_safe_skips_busy_server_without_attempt(self):
        s = FakeServer("a", busy=1)
        env = FakeEnv([s, FakeServer("b")])
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(s.applies, 0)
        self.assertEqual(env.servers["b"].version, "1.4.239")

    def test_wait_idle_retries_deferred_server(self):
        s = FakeServer("a", busy=1)
        env = FakeEnv([s])
        orig = env.ssh_helper

        def becomes_idle(server, args, sudo, timeout=900):
            if args[0] == "apply" and s.applies >= 2:
                s.busy = 0  # agent finished its turn while we waited
            return orig(server, args, sudo, timeout)

        env.ssh_helper = becomes_idle
        rc, out = run(env, ["update", "--yes", "--wait-idle", "3600"])
        self.assertEqual(rc, 0, out)
        self.assertIn("re-checking in 60s", out)
        self.assertEqual(s.version, "1.4.239")

    def test_retry_targets_failed_servers_only(self):
        a, b = FakeServer("a", "1.4.239"), FakeServer("b")
        b.state = {"op_id": "x", "result": "rolled_back"}
        env = FakeEnv([a, b])
        rc, out = run(env, ["retry", "--yes"])
        self.assertEqual(rc, 0, out)
        self.assertIn("Retrying: b", out)
        self.assertEqual(b.version, "1.4.239")

    def test_track_records_orchestration_tasks(self):
        env = FakeEnv([FakeServer("a"), FakeServer("b")])
        rc, out = run(env, ["update", "--yes", "--track"])
        self.assertEqual(rc, 0)
        self.assertIn("orchestration run: run_1", out)
        self.assertEqual(len(env.tasks), 2)
        for t in env.tasks.values():
            self.assertEqual(t["updates"][-1], "completed")
            self.assertIn("dispatched", t["updates"])

    def test_discover_marks_headless_servers(self):
        env = FakeEnv([FakeServer("dev")])
        rc, out = run(env, ["discover"])
        self.assertIn("dev", out)
        self.assertRegex(out, r"dev\s+yes\s+1\.4\.229\s+yes\s+yes")

    def test_ssh_host_validation(self):
        env = fleet.Env({"servers": []})
        r = env.ssh_helper({"name": "x", "ssh": "-oProxyCommand=id"}, ["status"], sudo=False)
        self.assertEqual(r["error"]["code"], "bad_host")

    def test_never_runs_screen_reader(self):
        import os
        if os.path.realpath(fleet.shutil.which("orca") or "") == "/usr/bin/orca":
            with self.assertRaises(SystemExit):
                fleet.resolve_orca_cli({"orca_cli": "orca"})

    def test_cli_fallback_per_platform(self):
        found = {"orca": "/opt/bin/orca"}
        with mock.patch.object(fleet.shutil, "which", side_effect=found.get):
            for platform in ("win32", "darwin"):
                with mock.patch.object(fleet.sys, "platform", platform):
                    self.assertEqual(fleet.resolve_orca_cli({}), "/opt/bin/orca")
            with mock.patch.object(fleet.sys, "platform", "linux"), self.assertRaises(SystemExit):
                fleet.resolve_orca_cli({})


@unittest.skipUnless(fleet.shutil.which("git"), "git not installed")
class SelfUpdateTests(unittest.TestCase):
    """A bare 'origin', a clone the tool updates from, and a second clone to push changes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(fleet.shutil.rmtree, self.tmp, ignore_errors=True)
        origin, self.clone, self.dev = self.tmp / "origin.git", self.tmp / "clone", self.tmp / "dev"
        self.git("init", "-q", "--bare", "-b", "main", str(origin))
        self.git("clone", "-q", str(origin), str(self.dev))
        self.write(self.dev, "client/orca-fleet", "print('v1')\n")
        self.write(self.dev, "client/orca-fleet.cmd", "@echo off\r\n")
        self.write(self.dev, "skill/orca-fleet/SKILL.md", "# v1\n")
        self.commit(self.dev, "v1")
        self.git("clone", "-q", str(origin), str(self.clone))
        bin_dir, skill_dir = self.tmp / "bin", self.tmp / "skills"
        for name, value in (("INSTALL_DIR", str(bin_dir)), ("SKILL_DIR", str(skill_dir))):
            p = mock.patch.object(fleet, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.installed = bin_dir / "orca-fleet"
        self.cfg = {"source_repo": str(self.clone)}

    def git(self, *a, cwd=None):
        fleet.subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                             check=True, capture_output=True)

    def write(self, repo, rel, text):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())

    def commit(self, repo, msg):
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", msg, cwd=repo)
        self.git("push", "-q", "origin", "HEAD:main", cwd=repo)

    def test_installs_then_pulls_only_what_changed(self):
        res = fleet.self_update(self.cfg)
        self.assertEqual(res["warnings"], [])
        self.assertEqual(self.installed.read_text(), "print('v1')\n")
        self.assertEqual(fleet.self_update(self.cfg)["installed"], [])   # nothing differs now
        self.write(self.dev, "client/orca-fleet", "print('v2')\n")
        self.commit(self.dev, "v2")
        res = fleet.self_update(self.cfg)
        self.assertTrue(res["pulled"])
        self.assertEqual(res["installed"], [str(self.installed)])
        self.assertEqual(self.installed.read_text(), "print('v2')\n")

    def test_dirty_clone_is_left_alone(self):
        self.write(self.clone, "client/orca-fleet", "print('local edit')\n")
        res = fleet.self_update(self.cfg)
        self.assertEqual(res["installed"], [])
        self.assertTrue(any("uncommitted" in w for w in res["warnings"]))
        self.assertFalse(self.installed.exists())

    def test_unparsable_client_is_not_installed(self):
        fleet.self_update(self.cfg)
        self.write(self.dev, "client/orca-fleet", "def broken(:\n")
        self.commit(self.dev, "broken")
        res = fleet.self_update(self.cfg)
        self.assertTrue(res["pulled"])
        self.assertTrue(any("not installed orca-fleet" in w for w in res["warnings"]))
        self.assertEqual(self.installed.read_text(), "print('v1')\n")

    def test_missing_clone_only_warns(self):
        with mock.patch.object(fleet.os, "getcwd", return_value=str(self.tmp)):
            res = fleet.self_update({})
        self.assertIsNone(res["repo"])
        self.assertTrue(res["warnings"])

    def test_server_helpers_are_asked_to_self_update(self):
        a, b, c = FakeServer("a", "1.4.239"), FakeServer("b", "1.4.239"), FakeServer("c", "1.4.239")
        b.helper_self_update = {"ok": False, "error": {"code": "self_update_disabled", "message": "off"}}
        c.helper_self_update = {"ok": False, "error": {"code": "sudo_denied", "message": "password"}}
        env = FakeEnv([a, b, c])
        res = fleet.self_update_servers(env, fleet.select_servers(env.cfg, None))
        self.assertEqual(sorted(res), ["a", "b", "c"])
        self.assertEqual(fleet.server_self_update_problems(res), ["c"])   # disabled is not a problem
        self.assertIn(("a", "self-update", True), env.calls)
        buf = io.StringIO()
        fleet.print_server_self_update(res, buf)
        self.assertIn("re-run install.sh once", buf.getvalue())

    def test_global_flag_never_blocks_the_command(self):
        env = FakeEnv([FakeServer("a", "1.4.239")])
        env.cfg["source_repo"] = str(self.tmp / "nope")
        with mock.patch.object(fleet.os, "getcwd", return_value=str(self.tmp)):
            rc, out = run(env, ["--self-update", "plan"])
        self.assertEqual(rc, 0)
        self.assertIn("skip (already at target)", out)


if __name__ == "__main__":
    unittest.main()


class LocalDesktopTests(unittest.TestCase):
    def env(self):
        return FakeEnv([FakeServer("local", local=True), FakeServer("a"), FakeServer("b")],
                       cfg_extra={"a": {"control": True}})

    def test_local_desktop_is_skipped_without_all(self):
        env = self.env()
        rc, out = run(env, ["update", "--yes"])
        self.assertEqual(rc, 0, out)
        self.assertEqual([n for n, c, _ in env.calls if c == "apply"], ["b", "a"])
        self.assertIn("skip (local desktop; pass --all)", out)
        self.assertEqual(env.servers["local"].version, "1.4.229")
        self.assertNotIn("The local desktop app is updated last", out)

    def test_naming_the_local_server_includes_it(self):
        env = self.env()
        rc, out = run(env, ["update", "--yes", "--server", "local"])
        self.assertEqual(rc, 0, out)
        self.assertEqual([n for n, c, _ in env.calls if c == "apply"], ["local"])

    def test_local_desktop_is_updated_last_through_local_helper(self):
        env = self.env()
        rc, out = run(env, ["update", "--yes", "--all"])
        self.assertEqual(rc, 0, out)
        applies = [n for n, c, _ in env.calls if c == "apply"]
        self.assertEqual(applies, ["b", "a", "local"])   # after the control host
        self.assertEqual(env.servers["local"].version, "1.4.239")
        self.assertIn("The local desktop app is updated last", out)
        # verified over this machine's own runtime, not a paired environment
        self.assertIn(["status"], env.orca_calls)
        self.assertIn("outcome: updated", out)

    def test_status_marks_the_desktop_row(self):
        rc, out = run(self.env(), ["status"])
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if l.startswith("local "))
        self.assertIn("app:active/running", line)

    def test_local_entry_needs_no_environment_or_ssh(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            Path(p).write_text(json.dumps({"servers": [{"name": "local", "local": True}]}))
            self.assertEqual(fleet.load_config(p)["servers"][0]["name"], "local")

    def test_local_helper_runs_without_ssh(self):
        env = fleet.Env({})
        cp = mock.Mock(stdout='{"ok": true}\n', stderr="", returncode=0)
        with mock.patch.object(fleet.subprocess, "run", return_value=cp) as run_:
            self.assertTrue(env.ssh_helper({"name": "local", "local": True}, ["status"], sudo=True)["ok"])
        self.assertEqual(run_.call_args[0][0], ["sudo", "-n", fleet.HELPER, "--json", "status"])

    def test_missing_local_helper_is_reported_not_raised(self):
        with mock.patch.object(fleet.subprocess, "run", side_effect=FileNotFoundError(fleet.HELPER)):
            r = fleet.Env({}).ssh_helper({"name": "local", "local": True}, ["status"], sudo=False)
        self.assertEqual(r["error"]["code"], "helper_missing")

    def test_discover_adds_local_desktop_when_helper_is_installed(self):
        env = FakeEnv([FakeServer("local", local=True), FakeServer("a")])
        env.cfg = {"servers": []}
        rc, out = run(env, ["discover"])
        self.assertEqual(rc, 0)
        line = next(l for l in out.splitlines() if l.startswith("local "))
        self.assertIn("(local)", line)
        self.assertRegex(line, r"\byes\b.*\(local\)")   # enabled

    def test_discover_hints_when_no_local_helper(self):
        rc, out = run(FakeEnv([FakeServer("a")]), ["discover"])
        self.assertIn("install.sh --desktop", out)
