"""Tests for client/orca-desktop-update with a simulated Windows (runs on any OS)."""

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("odu", str(ROOT / "client" / "orca-desktop-update"))
_spec = importlib.util.spec_from_loader("odu", _loader)
odu = importlib.util.module_from_spec(_spec)
_loader.exec_module(odu)

SIGNER = "CN=SignPath Foundation, O=SignPath Foundation, L=Lewes, S=Delaware, C=US"
PROTO_SRC = {
    "1.4.239": "export const PROTOCOL_VERSION = 51\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [50] as const\n",
    "1.4.240": "export const PROTOCOL_VERSION = 51\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [50] as const\n",
    "1.4.244": "export const PROTOCOL_VERSION = 53\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [51, 52] as const\n",
}


def exe_bytes(version):
    return f"FAKEEXE:{version}".encode() + b"\0" * 64


def release(version, digest=None, name=odu.ASSET_NAME):
    data = exe_bytes(version)
    return {"tag_name": f"v{version}", "prerelease": False, "draft": False, "published_at": "x",
            "assets": [{"name": name, "size": len(data),
                        "digest": digest or "sha256:" + hashlib.sha256(data).hexdigest(),
                        "browser_download_url": f"https://github.com/stablyai/orca/releases/download/v{version}/{name}"}]}


class FakeHttp:
    def __init__(self, releases):
        self.releases = releases

    def get_json(self, url, timeout=30):
        odu._check_url(url)
        if "daemon-protocol-version.ts" in url:
            return {"content": base64.b64encode(PROTO_SRC[url.rsplit("ref=v", 1)[1]].encode()).decode()}
        if url.endswith("/releases?per_page=40"):
            return self.releases
        tag = url.rsplit("/", 1)[1]
        return next(r for r in self.releases if r["tag_name"] == tag)

    def download(self, url, fd, max_bytes, timeout=60):
        data = exe_bytes(url.split("/download/v", 1)[1].split("/", 1)[0])
        os.write(fd, data)
        return hashlib.sha256(data).hexdigest()


class FakeWindows(odu.System):
    """Processes, the uninstall registry entry, the installer and Explorer of one user."""

    def __init__(self, paths, version="1.4.240"):
        self.paths, self.version = paths, version
        self.next_pid = 1000
        self.procs = []
        self.agents = []                 # agent states reported by `worktree ps`
        self.terminals = [700]           # shells under the PTY daemon
        self.daemon = {"pid": 500, "ppid": 4, "path": os.path.join(paths["daemon_root"], "prof", "1.4.239",
                                                                   "build-1", "Orca.exe"), "cmd": "daemon resources\\app"}
        self.hung = False                # the window ignores WM_CLOSE
        self.installer_rc = 0
        self.start_fails = False
        self.runtime_version = None      # override what the relaunched runtime reports
        self.crash_after_start = False   # the relaunched main process is replaced right away
        self.signature = "Valid"
        self.calls = []
        self.start_app()

    # --- simulated app ---
    def start_app(self):
        pid = self.next_pid
        self.next_pid += 10
        app = self.paths["app"]
        self.procs = [{"pid": pid, "ppid": 1, "path": app, "cmd": f'"{app}"'},
                      {"pid": pid + 1, "ppid": pid, "path": app, "cmd": f'"{app}" --type=gpu-process'},
                      {"pid": pid + 2, "ppid": pid, "path": app, "cmd": f"{app} session-scanner-service-entry.js"}]

    def main_pid(self):
        return next((p["pid"] for p in self.procs if "--type" not in p["cmd"] and ".js" not in p["cmd"]), None)

    # --- System interface ---
    def processes(self):
        if self.crash_after_start and self.procs:
            self.start_app()
        return [dict(p) for p in self.procs + ([self.daemon] if self.daemon else [])]

    def children(self, pids):
        return list(self.terminals) if self.daemon and self.daemon["pid"] in pids else []

    def pids_alive(self, pids):
        alive = {p["pid"] for p in self.procs} | set(self.terminals) | ({self.daemon["pid"]} if self.daemon else set())
        return [p for p in pids if p in alive]

    def uninstall_entries(self):
        un = os.path.join(self.paths["install_dir"], "Uninstall Orca.exe")
        return [{"DisplayName": "Other", "DisplayVersion": "9.9.9", "UninstallString": "x"},
                {"DisplayName": "Orca", "DisplayVersion": self.version, "Publisher": "stablyai",
                 "UninstallString": f'"{un}" /currentuser'}]

    def installer_facts(self, path):
        version = Path(path).read_bytes().split(b"\0")[0].decode().split(":")[1]
        return {"status": self.signature, "signer": SIGNER, "product_version": version + ".0"}

    def close_window(self, pid):
        self.calls.append(("close", pid))
        if not self.hung:
            self.procs = []

    def kill(self, pid):
        self.calls.append(("kill", pid))
        self.procs = [p for p in self.procs if p["pid"] != pid]

    def open_app(self, app):
        self.calls.append(("open", app))
        if not self.start_fails:
            self.start_app()

    def spawn_detached(self, argv, log_path, env):
        self.calls.append(("spawn", argv, env.get(odu.WORKER_ENV)))
        return 4242

    def arch(self):
        return "x64"

    def run(self, argv, *, timeout=120, cwd=None, env=None):
        if argv[0] == self.paths["cli"]:
            if argv[1] == "status":
                rt = {"reachable": bool(self.main_pid()), "state": "ready" if self.main_pid() else "down",
                      "appVersion": self.runtime_version or self.version}
                return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": {"runtime": rt}}), "")
            if argv[1:3] == ["worktree", "ps"]:
                wt = {"displayName": "main", "liveTerminalCount": len(self.terminals),
                      "agents": [{"agentType": "claude", "state": s} for s in self.agents]}
                return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "result": {"worktrees": [wt]}}), "")
        if argv[0].startswith(self.paths["cache_dir"]):
            self.calls.append(("install", argv))
            if self.procs:
                return subprocess.CompletedProcess(argv, 1, "", "Orca is running")
            if self.installer_rc == 0:
                self.version = Path(argv[0]).read_bytes().split(b"\0")[0].decode().split(":")[1]
            return subprocess.CompletedProcess(argv, self.installer_rc, "", "")
        raise AssertionError(argv)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        inst = self.tmp / "Programs" / "orca"
        (inst / "resources" / "bin").mkdir(parents=True)
        (inst / "Orca.exe").write_bytes(b"MZ")
        state = self.tmp / "state"
        self.paths = {"install_dir": str(inst), "app": str(inst / "Orca.exe"),
                      "cli": str(inst / "resources" / "bin" / "orca.exe"),
                      "daemon_root": str(self.tmp / "daemon-host-profiles"),
                      "state_dir": str(state), "cache_dir": str(state / "cache"),
                      "state": str(state / "state.json"), "audit": str(state / "audit.jsonl"),
                      "protocols": str(state / "daemon-protocols.json"), "lock": str(state / "op.lock"),
                      "worker_lock": str(state / "worker.lock"), "worker_log": str(state / "worker.log")}
        state.mkdir()
        self.win = FakeWindows(self.paths)
        self.http = FakeHttp([release("1.4.239"), release("1.4.240"), release("1.4.244")])
        self.now = [0.0]

        def sleep(s):
            self.now[0] += s

        self.u = odu.Updater(system=self.win, http=self.http, paths=self.paths, sleep=sleep,
                             clock=lambda: self.now[0])

    def prepare(self, version="1.4.244"):
        r = self.u.cmd_prepare(version)
        self.assertTrue(r["ok"])
        return r

    def update(self, version="1.4.244", mode="safe"):
        self.prepare(version)
        rc = self.u.worker("update", version, mode, "a" * 12)
        return rc, self.u.load_state()


class Release(Base):
    def test_windows_installer_asset_with_digest(self):
        rel = odu.resolve_release(self.http, "latest")
        self.assertEqual((rel["version"], rel["asset"]), ("1.4.244", "orca-windows-setup.exe"))

    def test_foreign_asset_url_and_missing_digest_refused(self):
        bad = release("1.4.244")
        bad["assets"][0]["browser_download_url"] = "https://evil.example/orca-windows-setup.exe"
        with self.assertRaises(odu.UpdateError) as e:
            odu._release_info(bad)
        self.assertEqual(e.exception.code, "forbidden_url")
        bad = release("1.4.244")
        bad["assets"][0]["digest"] = ""
        with self.assertRaises(odu.UpdateError) as e:
            odu._release_info(bad)
        self.assertEqual(e.exception.code, "missing_checksum")

    def test_only_x64(self):
        self.win.arch = lambda: "arm64"
        with self.assertRaises(odu.UpdateError) as e:
            self.u.cmd_prepare("1.4.244")
        self.assertEqual(e.exception.code, "unsupported_arch")


class Prepare(Base):
    def test_fetches_target_and_rollback_installers(self):
        r = self.prepare()
        self.assertEqual([f["version"] for f in r["fetched"]], ["1.4.244", "1.4.240"])
        self.assertEqual(self.u.prepared_versions(), ["1.4.240", "1.4.244"])
        self.assertEqual(self.u.load_state()["prepared"]["rollback"], "1.4.240")

    def test_unsigned_installer_never_lands_in_the_cache(self):
        self.win.signature = "NotSigned"
        with self.assertRaises(odu.UpdateError) as e:
            self.u.cmd_prepare("1.4.244")
        self.assertEqual(e.exception.code, "bad_package")
        self.assertEqual(self.u.prepared_versions(), [])

    def test_checksum_mismatch_refused(self):
        self.http.releases = [release("1.4.244", digest="sha256:" + "0" * 64), release("1.4.240")]
        with self.assertRaises(odu.UpdateError) as e:
            self.u.cmd_prepare("1.4.244")
        self.assertEqual(e.exception.code, "checksum_mismatch")

    def test_tampered_cache_blocks_apply(self):
        self.prepare()
        Path(self.u.cache_path("1.4.244")).write_bytes(exe_bytes("1.4.244") + b"x")
        r = self.u.cmd_apply("1.4.244", "safe")
        self.assertEqual(r["result"], "blocked")


class Apply(Base):
    def test_spawns_a_detached_worker(self):
        self.prepare()
        r = self.u.cmd_apply("1.4.244", "safe")
        self.assertEqual(r["result"], "started")
        kind, argv, guard = self.win.calls[-1]
        self.assertEqual(kind, "spawn")
        self.assertEqual(argv[-8:], ["_worker", "update", "--version", "1.4.244", "--mode", "safe",
                                     "--op-id", r["op_id"]])
        self.assertEqual(guard, r["op_id"])
        self.assertEqual(r["session_crossing"], "attachable")

    def test_safe_defers_on_working_agent_force_proceeds(self):
        self.prepare()
        self.win.agents = ["working"]
        self.assertEqual(self.u.cmd_apply("1.4.244", "safe")["result"], "deferred")
        self.assertEqual(self.u.cmd_apply("1.4.244", "force")["result"], "started")

    def test_same_version_skipped_and_downgrade_refused(self):
        self.assertEqual(self.u.cmd_apply("1.4.240", "safe")["result"], "skipped")
        with self.assertRaises(odu.UpdateError):
            self.u.cmd_apply("1.4.239", "safe")

    def test_daemon_that_target_cannot_attach_defers(self):
        PROTO_SRC["1.4.239"] = PROTO_SRC["1.4.239"].replace("= 51", "= 40")
        self.addCleanup(PROTO_SRC.__setitem__, "1.4.239", PROTO_SRC["1.4.239"].replace("= 40", "= 51"))
        self.prepare()
        r = self.u.cmd_apply("1.4.244", "safe")
        self.assertEqual(r["result"], "deferred")
        self.assertEqual(r["session_crossing"], "strands-live-terminals")

    def test_worker_guard(self):
        rc = odu.main(["--json", "_worker", "update", "--version", "1.4.244", "--mode", "safe",
                       "--op-id", "a" * 12], updater=self.u)
        self.assertEqual(rc, 2)


class Worker(Base):
    def test_successful_update_quits_installs_relaunches(self):
        old_main = self.win.main_pid()
        rc, st = self.update()
        self.assertEqual((rc, st["result"]), (0, "succeeded"))
        self.assertEqual(self.win.version, "1.4.244")
        self.assertIn(("close", old_main), self.win.calls)
        install = next(c for c in self.win.calls if c[0] == "install")[1]
        self.assertEqual(install[1:], ["/S", "/currentuser"])
        self.assertIn(("open", self.paths["app"]), self.win.calls)
        self.assertNotIn(("kill", 500), self.win.calls)       # the PTY daemon is never touched
        self.assertTrue(st["sessions_preserved"])
        self.assertTrue(st["verify"]["stable"])
        self.assertEqual(st["last_success_previous"], "1.4.240")

    def test_hung_window_is_force_killed(self):
        self.win.hung = True
        main = self.win.main_pid()
        rc, st = self.update()
        self.assertEqual(st["result"], "succeeded")
        self.assertEqual(st["app_killed"], main)
        self.assertIn(("kill", main), self.win.calls)

    def test_closed_app_stays_closed(self):
        self.win.procs = []
        rc, st = self.update()
        self.assertEqual(st["result"], "succeeded")
        self.assertFalse(any(c[0] == "open" for c in self.win.calls))
        self.assertEqual(st["verify"], {"installed_version": "1.4.244", "app_running": False})

    def test_installer_failure_relaunches_old_app_and_rolls_back(self):
        self.win.installer_rc = 2
        rc, st = self.update()
        self.assertEqual(st["result"], "rollback_failed")  # the rollback installer fails the same way
        self.assertIn("install_failed", st["error"])
        self.assertTrue(self.win.main_pid())               # the old app is back

    def test_wrong_runtime_version_rolls_back(self):
        self.win.runtime_version = "1.4.240"
        rc, st = self.update()
        self.assertIn("verify_timeout", st["error"])
        self.assertEqual((st["result"], self.win.version), ("rolled_back", "1.4.240"))

    def test_crashing_app_rolls_back(self):
        orig = self.win.open_app

        def open_and_crash(app):
            orig(app)
            self.win.crash_after_start = self.win.version == "1.4.244"
        self.win.open_app = open_and_crash
        rc, st = self.update()
        self.assertIn("unstable", st["error"])
        self.assertEqual(st["result"], "rolled_back")

    def test_lost_terminal_is_reported(self):
        orig = self.win.run

        def run(argv, **kw):
            cp = orig(argv, **kw)
            if argv[0].startswith(self.paths["cache_dir"]):
                self.win.terminals = []
            return cp
        self.win.run = run
        rc, st = self.update()
        self.assertEqual(st["result"], "succeeded")
        self.assertEqual((st["pty_sessions_lost"], st["sessions_preserved"]), (1, False))

    def test_manual_rollback_and_recover(self):
        self.update()
        rc = self.u.worker("rollback", "1.4.240", None, "b" * 12)
        self.assertEqual((rc, self.win.version), (0, "1.4.240"))
        st = self.u.load_state()
        st.update(op="update", target="1.4.244", previous="1.4.240", phase="installing", result=None)
        self.u.save_state(st)
        self.assertTrue(self.u.cmd_status()["stale_operation"])
        self.assertEqual(self.u.cmd_recover()["result"], "started")
        rc = self.u.worker("recover", None, None, "c" * 12)
        self.assertEqual(self.u.load_state()["result"], "rolled_back")
        self.assertEqual(self.win.version, "1.4.240")


class Status(Base):
    def test_status_matches_the_helper_protocol(self):
        s = self.u.cmd_status()
        self.assertEqual(s["service"]["kind"], "desktop")
        self.assertEqual(s["service"]["ActiveState"], "active")
        self.assertEqual(s["installed_version"], "1.4.240")
        self.assertEqual(s["daemons"], [{"pid": 500, "version": "1.4.239", "protocol": 51}])
        self.assertTrue(s["activity"]["known"])
        self.assertFalse(s["worker_active"])

    def test_missing_install_has_no_service(self):
        os.unlink(self.paths["app"])
        s = self.u.cmd_status()
        self.assertNotIn("service", s)
        self.assertIn("not found", s["config_error"])

    def test_running_worker_is_seen(self):
        with odu.FileLock(self.paths["worker_lock"], "busy"):
            self.assertTrue(self.u.worker_active())
            self.prepare()
            self.assertIn("an update worker is already running", self.u.cmd_apply("1.4.244", "safe")["blockers"])
        self.assertFalse(self.u.worker_active())


if __name__ == "__main__":
    unittest.main()
