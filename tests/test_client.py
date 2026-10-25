"""Tests for client/orca-fleet with a fake Orca CLI + SSH boundary."""

import importlib.machinery
import importlib.util
import io
import threading
import time
import unittest
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
    def __init__(self, name, version="1.4.229", busy=0):
        self.name, self.version, self.busy = name, version, busy
        self.state = {}
        self.prepared = set()
        self.apply_result = "succeeded"   # what the worker ends with
        self.unreachable_polls = 0        # simulate disconnects during the restart
        self.ssh_down = False
        self.prepare_fail = None
        self.client_sees = None           # override version seen over Orca after update
        self.applies = 0


class FakeEnv(fleet.Env):
    def __init__(self, servers, cfg_extra=None):
        super().__init__({"servers": [{"name": s.name, "environment": s.name, "ssh": s.name, **(cfg_extra or {}).get(s.name, {})}
                                      for s in servers]})
        self.servers = {s.name: s for s in servers}
        self.calls = []
        self.lock = threading.Lock()
        self.active_prepares = 0
        self.max_parallel_prepares = 0
        self.orca_calls = []
        self.tasks = {}

    def releases(self):
        return RELEASES

    def sleep(self, s):
        pass

    def orca(self, args, timeout=45):
        self.orca_calls.append(args)
        if args[0] == "status":
            s = self.servers[args[args.index("--environment") + 1]]
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
                                           [{"kind": "environment", "name": n} for n in self.servers]}}
        raise AssertionError(args)

    def ssh_helper(self, server, args, sudo, timeout=900):
        s = self.servers[server["name"]]
        with self.lock:
            self.calls.append((s.name, args[0], sudo))
        if s.ssh_down:
            return {"ok": False, "error": {"code": "ssh_failed", "message": "unreachable"}}
        cmd = args[0]
        if cmd == "status":
            if s.unreachable_polls > 0:
                s.unreachable_polls -= 1
                return {"ok": False, "error": {"code": "ssh_failed", "message": "connection reset"}}
            return {"ok": True, "installed_version": s.version,
                    "service": {"ActiveState": "active", "SubState": "running"},
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
        lines = {l.split()[0]: l for l in out.splitlines()[3:]}
        self.assertIn("yes", lines["a"])  # needs update
        self.assertRegex(lines["b"], r"1\.4\.239\s+no")

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


if __name__ == "__main__":
    unittest.main()
