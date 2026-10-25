"""Tests for server/orca-serve-update using a simulated system (no root, no systemd)."""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("osu", str(ROOT / "server" / "orca-serve-update"))
_spec = importlib.util.spec_from_loader("osu", _loader)
osu = importlib.util.module_from_spec(_spec)
_loader.exec_module(osu)

ME = __import__("pwd").getpwuid(os.getuid()).pw_name
ARCH = "amd64"


def deb_bytes(version, arch=ARCH, pkg="orca-ide"):
    return f"FAKEDEB:{pkg}:{version}:{arch}".encode() + b"\0" * 64


def release(version, prerelease=False, draft=False, digest=None, arch=ARCH, tag=None):
    data = deb_bytes(version, arch)
    name = f"orca-ide_{version}_{arch}.deb"
    tag = tag or f"v{version}"
    return {
        "tag_name": tag, "prerelease": prerelease, "draft": draft, "published_at": "2026-10-24T00:00:00Z",
        "assets": [{
            "name": name, "size": len(data),
            "digest": digest or "sha256:" + hashlib.sha256(data).hexdigest(),
            "browser_download_url": f"https://github.com/stablyai/orca/releases/download/{tag}/{name}",
        }],
    }


PROTO_SRC = {
    "1.4.229": "export const PROTOCOL_VERSION = 48\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [\n 46, 47\n] as const\n",
    "1.4.239": "export const PROTOCOL_VERSION = 51\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [\n 47, 48, 49, 50\n] as const\n",
    "1.4.240": "export const PROTOCOL_VERSION = 60\nexport const PREVIOUS_DAEMON_PROTOCOL_VERSIONS = [\n 59\n] as const\n",
}


class FakeHttp:
    def __init__(self, releases):
        self.releases = releases  # list of release dicts
        self.fail_download = None  # exception to raise
        self.corrupt = False
        self.downloads = []
        self.lock = threading.Lock()

    def get_json(self, url, timeout=30):
        osu._check_url(url)
        if "/contents/src/main/daemon/daemon-protocol-version.ts" in url:
            v = url.rsplit("ref=v", 1)[1]
            import base64
            if v not in PROTO_SRC:
                raise osu.UpdateError("github_http_error", "404")
            return {"content": base64.b64encode(PROTO_SRC[v].encode()).decode()}
        if url.endswith("/releases?per_page=40"):
            return self.releases
        tag = url.rsplit("/", 1)[1]
        for r in self.releases:
            if r["tag_name"] == tag:
                return r
        raise osu.UpdateError("github_http_error", f"GitHub API 404 for {url}")

    def download(self, url, fd, max_bytes, timeout=60):
        osu._check_url(url)
        with self.lock:
            self.downloads.append(url)
        if self.fail_download:
            os.write(fd, b"partial")
            raise self.fail_download
        name = url.rsplit("/", 1)[1]
        _, version, arch = name[:-4].split("_")
        data = deb_bytes(version, arch)
        if self.corrupt:
            data = data[:-1] + b"X"
        os.write(fd, data)
        return hashlib.sha256(data).hexdigest()


class FakeSystem(osu.Runner):
    """Simulates dpkg, systemctl, ss, loginctl and the Orca CLI."""

    def __init__(self, installed="1.4.229"):
        self.installed = installed
        self.active = True
        self.main_pid = 1000
        self.n_restarts = 0
        self.runtime_version = installed
        self.fail_start_for = set()      # versions whose start fails
        self.bad_runtime_for = set()     # versions that start but report wrong version
        self.flap_for = set()            # versions that restart during stability window
        self.fail_dpkg_for = set()
        self.agents = []                 # list of agent dicts for worktree ps
        self.orca_unreachable = False
        self.linger = "yes"
        self.worker_active = False
        self.spawned = []
        self.calls = []
        self._stable_checks = 0

    def cp(self, out="", rc=0, err=""):
        return subprocess.CompletedProcess([], rc, out, err)

    def run(self, argv, *, timeout=120, env=None, check=False):
        self.calls.append(list(argv))
        a = list(argv)
        if a[:1] == ["runuser"]:
            a = a[a.index("--") + 1:]
        cmd = a[0]
        if cmd == "dpkg" and a[1] == "--print-architecture":
            return self.cp(ARCH + "\n")
        if cmd == "dpkg-query" and a[1] == "-W":
            return self.cp(f"install ok installed\t{self.installed}") if self.installed else self.cp("", 1)
        if cmd == "dpkg-query" and a[1] == "-L":
            return self.cp("")
        if cmd == "dpkg-deb":
            data = Path(a[2]).read_bytes()
            if not data.startswith(b"FAKEDEB:"):
                return self.cp("", 2, "not a debian archive")
            _, pkg, ver, arch = data.split(b"\0", 1)[0].decode().split(":")
            return self.cp(f"Package: {pkg}\nVersion: {ver}\nArchitecture: {arch}\n")
        if cmd == "dpkg" and a[1] == "-i":
            ver = Path(a[-1]).read_bytes().split(b"\0", 1)[0].decode().split(":")[2]
            if ver in self.fail_dpkg_for:
                return self.cp("", 1, "dpkg: error processing")
            self.installed = ver
            return self.cp("ok")
        if cmd == "systemctl":
            return self.systemctl(a[1:])
        if cmd == "systemd-run":
            if self.worker_active:
                return self.cp("", 1, "Unit already exists")
            self.spawned.append(a)
            return self.cp("Running as unit: orca-serve-update-worker.service")
        if cmd == "ss":
            return self.cp("LISTEN 0 511 0.0.0.0:6768 0.0.0.0:*\n" if self.active else "")
        if cmd == "loginctl":
            return self.cp(self.linger + "\n")
        if cmd == "/usr/bin/orca-ide":
            return self.orca(a[1:])
        raise AssertionError(f"unexpected command {argv}")

    def systemctl(self, a):
        if a[0] == "show":
            if "ExecStart" in " ".join(a) and "--value" in a:
                if a[1] != "orca-serve.service":
                    return self.cp("{ path=/usr/sbin/sshd ; argv[]=/usr/sbin/sshd -D ; }")
                return self.cp("{ path=/usr/bin/orca-ide ; argv[]=/usr/bin/orca-ide serve --port 6768 ; }")
            if "User,ExecStart,FragmentPath" in a:
                return self.cp(f"User={ME}\nExecStart={{ path=/usr/bin/orca-ide ; argv[]=/usr/bin/orca-ide serve "
                               f"--port 6768 --json ; }}\nFragmentPath=/etc/systemd/system/orca-serve.service\n")
            state = "active" if self.active else "failed" if self.installed in self.fail_start_for else "inactive"
            if self.active and self.installed in self.flap_for:
                self._stable_checks += 1
                if self._stable_checks > 1:
                    self.n_restarts += 1
                    self.main_pid += 1
            return self.cp(f"ActiveState={state}\nSubState={'running' if self.active else 'dead'}\n"
                           f"MainPID={self.main_pid if self.active else 0}\nNRestarts={self.n_restarts}\n")
        if a[0] == "is-active":
            return self.cp("active\n" if self.worker_active else "inactive\n", 0 if self.worker_active else 3)
        if a[0] == "list-units":
            return self.cp("orca-serve.service loaded active running Orca runtime server\n"
                           "ssh.service loaded active running OpenBSD Secure Shell server\n")
        if a[0] == "stop":
            self.active = False
            return self.cp()
        if a[0] == "start":
            if self.installed in self.fail_start_for:
                self.active = False
                return self.cp("", 1, "Job for orca-serve.service failed")
            self.active = True
            self.main_pid += 10
            self.runtime_version = self.installed
            self._stable_checks = 0
            return self.cp()
        if a[0] == "reset-failed":
            return self.cp()
        raise AssertionError(f"unexpected systemctl {a}")

    def orca(self, a):
        if self.orca_unreachable or not self.active:
            return self.cp('{"ok": false, "error": {"code": "runtime_unavailable"}}')
        if a[0] == "status":
            v = "9.9.9" if self.installed in self.bad_runtime_for else self.runtime_version
            return self.cp(json.dumps({"ok": True, "result": {"runtime": {
                "reachable": True, "state": "ready", "appVersion": v, "runtimeId": "r1"}}}))
        if a[:2] == ["worktree", "ps"]:
            return self.cp(json.dumps({"ok": True, "result": {"worktrees": [
                {"displayName": "wt", "liveTerminalCount": len(self.agents), "unverifiableTerminalCount": 0,
                 "agents": self.agents}], "truncated": False}}))
        raise AssertionError(f"unexpected orca {a}")


class TestUpdater(osu.Updater):
    def __init__(self, *a, daemons=None, **kw):
        super().__init__(*a, **kw)
        self._daemons = daemons if daemons is not None else [{"pid": 4242, "protocol": 48}]

    def daemon_processes(self):
        return list(self._daemons)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        t = self.tmp
        self.paths = {"config": f"{t}/etc/config.json", "state_dir": f"{t}/state",
                      "cache_dir": f"{t}/state/cache", "state": f"{t}/state/state.json",
                      "log_dir": f"{t}/log", "audit": f"{t}/log/audit.jsonl", "lock": f"{t}/lock",
                      "self": "/usr/local/sbin/orca-serve-update"}
        os.makedirs(f"{t}/etc", mode=0o755)
        Path(self.paths["config"]).write_text(json.dumps({
            "service": "orca-serve.service", "service_user": ME, "port": 6768, "cli": "/usr/bin/orca-ide",
            "verify_timeout_seconds": 30, "stability_seconds": 1}))
        self.sys = FakeSystem()
        self.http = FakeHttp([release("1.4.239"), release("1.4.229"), release("1.4.240-rc1", tag="v1.4.240-rc1"),
                              release("1.4.241", prerelease=True), release("1.4.240")])
        self.clock_t = [0.0]

        def clock():
            self.clock_t[0] += 1
            return self.clock_t[0]

        self.u = TestUpdater(runner=self.sys, http=self.http, paths=self.paths, require_root=False,
                             sleep=lambda s: None, clock=clock)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self):
        return json.loads(Path(self.paths["state"]).read_text())

    def run_worker(self, op_id="abcdef123456", kind="update", version="1.4.239", mode="safe"):
        # inside the transient unit, systemd reports the worker unit itself as active
        self.sys.worker_active = True
        try:
            with mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
                return self.u.worker(kind, version, mode, op_id)
        finally:
            self.sys.worker_active = False


class ReleaseResolution(Base):
    def test_latest_ignores_prereleases_drafts_and_non_semver_tags(self):
        self.http.releases.append(release("9.0.0", draft=True))
        self.assertEqual(osu.resolve_release(self.http, "latest", ARCH)["version"], "1.4.240")

    def test_specific_prerelease_refused(self):
        with self.assertRaises(osu.UpdateError) as e:
            osu.resolve_release(self.http, "1.4.241", ARCH)
        self.assertEqual(e.exception.code, "not_stable_release")

    def test_missing_digest_refused(self):
        r = release("1.4.239")
        r["assets"][0]["digest"] = None
        with self.assertRaises(osu.UpdateError) as e:
            osu._release_info(r, ARCH)
        self.assertEqual(e.exception.code, "missing_checksum")

    def test_foreign_asset_url_refused(self):
        r = release("1.4.239")
        r["assets"][0]["browser_download_url"] = "https://evil.example/orca-ide_1.4.239_amd64.deb"
        with self.assertRaises(osu.UpdateError) as e:
            osu._release_info(r, ARCH)
        self.assertEqual(e.exception.code, "forbidden_url")

    def test_arch_must_match(self):
        with self.assertRaises(osu.UpdateError):
            osu._release_info(release("1.4.239", arch="arm64"), ARCH)
        with self.assertRaises(osu.UpdateError):
            osu.resolve_release(self.http, "latest", "i386")

    def test_version_validation_blocks_injection(self):
        for bad in ["1.4", "1.4.239;rm -rf /", "../1.4.239", "1.4.239\n", "v1.4.239", "01.4.239", "1.4.239-rc1"]:
            with self.assertRaises(osu.UpdateError, msg=bad):
                osu.validate_version_arg(bad)

    def test_url_allowlist(self):
        for bad in ["http://github.com/x", "https://github.com.evil.io/x", "https://example.com/a.deb",
                    "file:///etc/passwd"]:
            with self.assertRaises(osu.UpdateError):
                osu._check_url(bad)
        osu._check_url("https://objects.githubusercontent.com/x")


class Prepare(Base):
    def test_prepare_fetches_target_and_rollback_and_records_protocol(self):
        out = self.u.cmd_prepare("latest")
        self.assertTrue(out["ok"])
        self.assertEqual(out["target"], "1.4.240")
        cached = sorted(os.listdir(self.paths["cache_dir"]))
        self.assertIn("orca-ide_1.4.240_amd64.deb", cached)
        self.assertIn("orca-ide_1.4.229_amd64.deb", cached)
        meta = json.loads(Path(self.paths["cache_dir"], "orca-ide_1.4.240_amd64.deb.json").read_text())
        self.assertEqual(meta["daemon_protocol"]["protocol"], 60)
        self.assertEqual(oct(os.stat(self.paths["cache_dir"]).st_mode & 0o777), "0o700")

    def test_prepare_is_idempotent(self):
        self.u.cmd_prepare("1.4.239")
        n = len(self.http.downloads)
        out = self.u.cmd_prepare("1.4.239")
        self.assertEqual(len(self.http.downloads), n)
        self.assertTrue(all(f["cached"] for f in out["fetched"]))

    def test_checksum_mismatch_never_activates_file(self):
        self.http.corrupt = True
        with self.assertRaises(osu.UpdateError) as e:
            self.u.cmd_prepare("1.4.239")
        self.assertEqual(e.exception.code, "checksum_mismatch")
        self.assertEqual([f for f in os.listdir(self.paths["cache_dir"]) if f.endswith(".deb")], [])

    def test_download_failure_cleans_up(self):
        self.http.fail_download = osu.UpdateError("download_failed", "network down")
        with self.assertRaises(osu.UpdateError) as e:
            self.u.cmd_prepare("1.4.239")
        self.assertEqual(e.exception.code, "download_failed")
        self.assertEqual(os.listdir(self.paths["cache_dir"]), [])

    def test_disk_full_detected_before_download(self):
        du = namedtuple("du", "total used free")
        with mock.patch.object(osu.shutil, "disk_usage", return_value=du(1, 1, 1024)):
            with self.assertRaises(osu.UpdateError) as e:
                self.u.cmd_prepare("1.4.239")
        self.assertEqual(e.exception.code, "disk_full")
        self.assertEqual(self.http.downloads, [])

    def test_package_field_mismatch_refused(self):
        orig = self.http.download

        def wrong_pkg(url, fd, max_bytes, timeout=60):
            data = deb_bytes("1.4.239", pkg="evil")
            os.write(fd, data)
            return hashlib.sha256(data).hexdigest()

        self.http.download = wrong_pkg
        r = release("1.4.239")
        r["assets"][0]["digest"] = "sha256:" + hashlib.sha256(deb_bytes("1.4.239", pkg="evil")).hexdigest()
        r["assets"][0]["size"] = len(deb_bytes("1.4.239", pkg="evil"))
        self.http.releases = [r, release("1.4.229")]
        with self.assertRaises(osu.UpdateError) as e:
            self.u.cmd_prepare("1.4.239")
        self.assertEqual(e.exception.code, "bad_package")
        self.http.download = orig

    def test_dry_run_downloads_nothing(self):
        out = self.u.cmd_prepare("1.4.239", dry_run=True)
        self.assertEqual(out["would_fetch"], ["orca-ide_1.4.239_amd64.deb", "orca-ide_1.4.229_amd64.deb"])
        self.assertEqual(self.http.downloads, [])

    def test_cache_symlink_rejected(self):
        os.makedirs(self.paths["state_dir"], mode=0o755)
        os.symlink("/tmp", self.paths["cache_dir"])
        with self.assertRaises(osu.UpdateError) as e:
            self.u.cmd_prepare("1.4.239")
        self.assertEqual(e.exception.code, "unsafe_path")

    def test_tampered_cache_detected_at_apply(self):
        self.u.cmd_prepare("1.4.239")
        p = Path(self.paths["cache_dir"], "orca-ide_1.4.239_amd64.deb")
        p.write_bytes(p.read_bytes() + b"tamper")
        rep = self.u.preflight("1.4.239", "safe")
        self.assertTrue(any("not prepared/verified" in b for b in rep["blockers"]))


class Apply(Base):
    def setUp(self):
        super().setUp()
        self.u.cmd_prepare("1.4.239")

    def test_same_version_is_skipped(self):
        self.sys.installed = "1.4.239"
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "skipped")
        self.assertEqual(self.sys.spawned, [])

    def test_apply_spawns_transient_systemd_unit(self):
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "started")
        argv = self.sys.spawned[0]
        self.assertIn("--unit=orca-serve-update-worker", argv)
        self.assertIn("NoNewPrivileges=yes", argv)
        i = argv.index("/usr/bin/python3")
        self.assertEqual(argv[i:i + 3], ["/usr/bin/python3", "-I", "/usr/local/sbin/orca-serve-update"])
        self.assertIn("_worker", argv)
        self.assertIn("--op-id", argv)

    def test_apply_dry_run_spawns_nothing(self):
        out = self.u.cmd_apply("1.4.239", "safe", dry_run=True)
        self.assertEqual(out["result"], "dry_run_ok")
        self.assertEqual(self.sys.spawned, [])

    def test_double_execution_refused_by_running_worker(self):
        self.sys.worker_active = True
        out = self.u.cmd_apply("1.4.239", "force")
        self.assertEqual(out["result"], "blocked")
        self.assertTrue(any("already running" in b for b in out["blockers"]))

    def test_running_worker_beats_version_equality(self):
        self.sys.installed = "1.4.239"   # a rollback is mid-flight and has not switched yet
        self.sys.worker_active = True
        out = self.u.cmd_apply("1.4.239", "force")
        self.assertEqual(out["result"], "blocked")

    def test_double_execution_refused_by_lock(self):
        with self.u.lock(blocking=False):
            with self.assertRaises(osu.UpdateError) as e:
                self.run_worker()
            self.assertEqual(e.exception.code, "locked")

    def test_safe_defers_on_working_agent(self):
        self.sys.agents = [{"agentType": "claude", "state": "working"}]
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "deferred")
        self.assertEqual(self.sys.spawned, [])

    def test_monitoring_loop_does_not_block_forever(self):
        self.sys.agents = [{"agentType": "claude", "state": "working", "workingMode": "monitoring"},
                           {"agentType": "codex", "state": "blocked"}]
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "started")
        self.assertTrue(any("monitoring" in w for w in out["warnings"]))

    def test_force_proceeds_with_warning(self):
        self.sys.agents = [{"agentType": "claude", "state": "working"}]
        out = self.u.cmd_apply("1.4.239", "force")
        self.assertEqual(out["result"], "started")
        self.assertTrue(any("FORCE" in w for w in out["warnings"]))

    def test_safe_defers_when_activity_unknown(self):
        self.sys.orca_unreachable = True
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "deferred")

    def test_safe_defers_without_linger(self):
        self.sys.linger = "no"
        out = self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(out["result"], "deferred")
        self.assertTrue(any("lingering" in b for b in out["blockers"]))

    def test_safe_defers_when_protocol_would_strand_terminals(self):
        self.u.cmd_prepare("1.4.240")  # protocol 60, previous [59]; live daemon speaks 48
        out = self.u.cmd_apply("1.4.240", "safe")
        self.assertEqual(out["result"], "deferred")
        self.assertEqual(out["session_crossing"], "strands-live-terminals")

    def test_attachable_crossing_reported(self):
        out = self.u.cmd_apply("1.4.239", "safe", dry_run=True)
        self.assertEqual(out["session_crossing"], "attachable")

    def test_downgrade_refused_without_flag(self):
        self.sys.installed = "1.4.240"
        with self.assertRaises(osu.UpdateError) as e:
            self.u.cmd_apply("1.4.239", "safe")
        self.assertEqual(e.exception.code, "downgrade_refused")

    def test_not_prepared_is_blocked_not_deferred(self):
        out = self.u.cmd_apply("1.4.240", "safe")
        self.assertEqual(out["result"], "blocked")


class Worker(Base):
    def setUp(self):
        super().setUp()
        self.u.cmd_prepare("1.4.239")

    def test_successful_update(self):
        rc = self.run_worker()
        st = self.state()
        self.assertEqual(rc, 0)
        self.assertEqual(st["result"], "succeeded")
        self.assertEqual(self.sys.installed, "1.4.239")
        self.assertTrue(st["sessions_preserved"])
        self.assertEqual(st["last_success_previous"], "1.4.229")
        phases = [h["phase"] for h in st["history"]]
        self.assertEqual(phases[:6], ["preflight", "stopping_service", "installing", "starting_service",
                                      "verifying", "done"])
        audit = Path(self.paths["audit"]).read_text().splitlines()
        self.assertTrue(any('"phase": "installing"' in l for l in audit))

    def test_start_failure_rolls_back(self):
        self.sys.fail_start_for = {"1.4.239"}
        rc = self.run_worker()
        st = self.state()
        self.assertEqual(rc, 0)
        self.assertEqual(st["result"], "rolled_back")
        self.assertIn("start_failed", st["error"])
        self.assertEqual(self.sys.installed, "1.4.229")
        self.assertTrue(self.sys.active)

    def test_wrong_runtime_version_rolls_back(self):
        self.sys.bad_runtime_for = {"1.4.239"}
        self.run_worker()
        st = self.state()
        self.assertEqual(st["result"], "rolled_back")
        self.assertIn("verify_timeout", st["error"])

    def test_flapping_service_rolls_back(self):
        self.sys.flap_for = {"1.4.239"}
        self.run_worker()
        self.assertEqual(self.state()["result"], "rolled_back")
        self.assertIn("unstable", self.state()["error"])

    def test_dpkg_failure_restarts_old_and_rolls_back(self):
        self.sys.fail_dpkg_for = {"1.4.239"}
        self.run_worker()
        st = self.state()
        self.assertEqual(st["result"], "rolled_back")
        self.assertIn("install_failed", st["error"])
        self.assertTrue(self.sys.active)

    def test_rollback_failure_reported(self):
        self.sys.fail_start_for = {"1.4.239", "1.4.229"}
        rc = self.run_worker()
        self.assertEqual(rc, 1)
        self.assertEqual(self.state()["result"], "rollback_failed")

    def test_agents_appearing_after_apply_defer_in_worker(self):
        self.sys.agents = [{"agentType": "claude", "state": "working"}]
        self.run_worker()
        self.assertEqual(self.state()["result"], "deferred")
        self.assertEqual(self.sys.installed, "1.4.229")

    def test_manual_rollback(self):
        self.run_worker()
        out = self.u.cmd_rollback(None)
        self.assertEqual(out["target"], "1.4.229")
        self.run_worker(op_id="0123456789ab", kind="rollback", version="1.4.229", mode=None)
        self.assertEqual(self.state()["result"], "succeeded")
        self.assertEqual(self.sys.installed, "1.4.229")

    def test_recover_after_crash_mid_install(self):
        # simulate crash: state says installing, package switched, service stopped
        st = {"op_id": "deadbeef0000", "op": "update", "target": "1.4.239", "previous": "1.4.229",
              "phase": "installing", "result": None, "history": []}
        os.makedirs(self.paths["state_dir"], exist_ok=True)
        Path(self.paths["state"]).write_text(json.dumps(st))
        self.sys.installed, self.sys.active = "1.4.239", False
        self.assertTrue(self.u.cmd_status()["stale_operation"])
        out = self.u.cmd_recover()
        self.assertEqual(out["result"], "started")
        self.run_worker(op_id="0123456789ab", kind="recover", version=None, mode=None)
        st = self.state()
        self.assertEqual(st["result"], "succeeded")
        self.assertTrue(self.sys.active)

    def test_recover_rolls_back_unhealthy_switch(self):
        st = {"op_id": "deadbeef0000", "op": "update", "target": "1.4.239", "previous": "1.4.229",
              "phase": "starting_service", "result": None, "history": []}
        os.makedirs(self.paths["state_dir"], exist_ok=True)
        Path(self.paths["state"]).write_text(json.dumps(st))
        self.sys.installed, self.sys.active = "1.4.239", False
        self.sys.fail_start_for = {"1.4.239"}
        self.run_worker(op_id="0123456789ab", kind="recover", version=None, mode=None)
        self.assertEqual(self.state()["result"], "rolled_back")
        self.assertEqual(self.sys.installed, "1.4.229")

    def test_worker_requires_systemd(self):
        rc = osu.main(["--json", "_worker", "update", "--version", "1.4.239", "--mode", "safe",
                       "--op-id", "abcdef123456"], updater=self.u)
        self.assertEqual(rc, 2)

    def test_bad_op_id_rejected(self):
        with self.assertRaises(osu.UpdateError):
            self.run_worker(op_id="../../etc")


class Misc(Base):
    def test_discovery_finds_single_serve_unit(self):
        found = self.u.discover_service(None)
        self.assertEqual(found["service"], "orca-serve.service")
        self.assertEqual(found["service_user"], ME)
        self.assertEqual(found["port"], 6768)

    def test_config_validation(self):
        for bad in [{"service": "x;rm.service", "service_user": "a", "port": 1},
                    {"service": "a.service", "service_user": "Bad User", "port": 1},
                    {"service": "a.service", "service_user": "a", "port": 70000}]:
            with self.assertRaises(osu.UpdateError):
                osu.Updater._validate_config(bad)

    def test_status_reports_everything(self):
        st = self.u.cmd_status()
        self.assertEqual(st["installed_version"], "1.4.229")
        self.assertEqual(st["service"]["ActiveState"], "active")
        self.assertTrue(st["activity"]["known"])
        self.assertTrue(st["linger"])

    def test_root_required_for_privileged_commands(self):
        u = osu.Updater(runner=self.sys, http=self.http, paths=self.paths, require_root=True)
        if os.geteuid() != 0:
            with self.assertRaises(osu.UpdateError) as e:
                u.cmd_prepare("1.4.239")
            self.assertEqual(e.exception.code, "permission_denied")

    def test_apply_latest_rejected(self):
        rc = osu.main(["--json", "apply", "--version", "latest"], updater=self.u)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
