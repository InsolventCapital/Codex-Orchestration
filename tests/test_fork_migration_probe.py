from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from scripts import fork_migration_probe as probe


class FakeRunner:
    """Caller-boundary simulation; never starts Codex, Git, MCP, or a model."""
    instances = []
    fail_upgrade = False

    def __init__(self, temp, env, report):
        self.temp, self.env, self.report = temp, env, report
        self.packages = {}
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[-1] == "--version":
            return subprocess.CompletedProcess(args, 0, "codex-cli 0.144.1\n", "")
        if probe.CONFIGURATOR in " ".join(args):
            home = Path(args[args.index("--codex-home") + 1])
            if "--status" in args:
                return subprocess.CompletedProcess(args, 0, "Native policy: installed and effective", "")
            if "--disable" in args:
                config = (home / "config.toml").read_text()
                if "UNREGISTERED POST-SETUP DRIFT" in config:
                    return subprocess.CompletedProcess(args, 1, "", "Refusing to erase those changes")
                # Model the observed restore semantics: pre-setup hints were absent.
                remaining = "\n".join(line for line in config.splitlines()
                                      if not line.startswith(("multi_agent_mode_hint_text", "usage_hint_text")))
                (home / "config.toml").write_text(remaining + "\n")
                (home / probe.STATE).unlink(missing_ok=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    def json(self, args):
        self.calls.append(args)
        args = args[2:-1]
        if args == ["list"]:
            return {"installed": list(self.packages.values())}
        if args[0] == "marketplace":
            if args[1] == "upgrade":
                if self.fail_upgrade:
                    raise probe.Blocked("mock native upgrade unavailable")
                return {"errors": [], "selectedMarketplaces": [args[2]]}
            return {}
        plugin_id = args[1]
        market = plugin_id.split("@")[1]
        if args[0] == "remove":
            self.packages.pop(plugin_id, None)
            return {}
        if args[0] == "disable":
            self.packages[plugin_id]["enabled"] = False
            return {}
        source = self.temp / market / probe.PACKAGE
        version = json.loads((source / ".codex-plugin/plugin.json").read_text())["version"]
        dest = self.temp / "codex" / "cache" / market / version
        if not dest.exists():
            shutil.copytree(source, dest)
        entry = {"pluginId": plugin_id, "version": version, "enabled": True,
                 "installedPath": str(dest)}
        self.packages[plugin_id] = entry
        return entry


def minimal_sources(runner, report):
    temp = runner.temp
    roots = {}
    for market in (probe.UPSTREAM, probe.CANDIDATE):
        root = temp / market
        package = root / probe.PACKAGE
        (package / ".codex-plugin").mkdir(parents=True)
        (package / "skills/codex-orchestration/scripts").mkdir(parents=True)
        (package / "skills/codex-orchestration/SKILL.md").write_text("stable skill label")
        (package / probe.CONFIGURATOR).write_text("# fixture, never executed")
        (package / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "fable-advisor-python3": {"enabled": False}}}))
        (package / ".codex-plugin/plugin.json").write_text(json.dumps({
            "version": "0.9.4" if market == probe.CANDIDATE else "0.9.3"}))
        roots[market] = root
    report["source_hashes"] = {"synthetic": probe.digest(b"synthetic")}
    return roots


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="probe-unit-")
        self.root = Path(self.directory.name).resolve()
        self.addCleanup(self.directory.cleanup)
        FakeRunner.instances.clear()
        FakeRunner.fail_upgrade = False

    def report(self):
        return {"checks": {name: {"status": "BLOCKED"} for name in probe.REQUIRED},
                "commands": [], "cli": {}}

    def test_no_native_flag_does_not_create_files_or_start_commands(self):
        with mock.patch.object(probe, "reserve_report") as reserve, \
                mock.patch.object(probe, "qualify") as qualify, redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(["--report", str(self.root / "absent")]), 0)
        reserve.assert_not_called()
        qualify.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_absolute_explicit_native_arguments_required(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(["--native", "--codex-bin", "relative",
                                         "--report", str(self.root / "report")]), 1)
        self.assertFalse((self.root / "report").exists())

    def test_environment_drops_all_parent_secret_and_override_families(self):
        parent = dict(os.environ)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET", "ANTHROPIC_AUTH_TOKEN": "SECRET",
                                        "HTTPS_PROXY": "SECRET", "CODEX_HOME": "/real",
                                        "GIT_CONFIG_COUNT": "2", "PYTHONPATH": "/hostile",
                                        "CLAUDE_CONFIG_DIR": "/real", "LD_PRELOAD": "evil"}):
            inherited = dict(os.environ)
            env = probe.child_env(self.root)
            self.assertEqual(dict(os.environ), inherited)
        self.assertEqual(dict(os.environ), parent)
        self.assertFalse(any("SECRET" in value for value in env.values()))
        for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY", "PYTHONPATH", "LD_PRELOAD"):
            self.assertNotIn(key, env)
        for key in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME"):
            self.assertTrue(Path(env[key]).is_relative_to(self.root))
            self.assertEqual(list(Path(env[key]).iterdir()), [])
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")

    def test_owned_path_rejects_symlinks_traversal_and_escape(self):
        (self.root / "link").symlink_to(self.root / "nonexistent")
        for path in (self.root / "link/file", self.root / "../escape", self.root.parent / "escape", Path("relative")):
            with self.subTest(path=path), self.assertRaises(probe.Blocked):
                probe.owned(path, self.root)

    def test_report_exclusive_owned_parent_and_no_symlinks(self):
        target = self.root / "report.json"
        fd = probe.reserve_report(target)
        os.write(fd, b"sentinel")
        os.close(fd)
        with self.assertRaises(probe.Blocked):
            probe.reserve_report(target)
        self.assertEqual(target.read_bytes(), b"sentinel")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(probe.Blocked):
            probe.reserve_report(link)
        directory = self.root / "unsafe"
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        with self.assertRaises(probe.Blocked):
            probe.reserve_report(directory / "report")
        with self.assertRaises(probe.Blocked):
            probe.reserve_report(probe.ROOT / "report.json")

    def test_unsupported_os_blocks_without_invoking_native(self):
        env = probe.child_env(self.root)
        with mock.patch.object(probe.sys, "platform", "unsupported"), \
                mock.patch.object(probe.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(probe.Blocked, "containment unavailable"):
                probe.Runner(self.root, env, self.report())
        popen.assert_not_called()

    def test_guard_refuses_model_mcp_and_gate_bypass_dispatch(self):
        guard = probe.write_guard(self.root, Path("/never-execute-native"))
        source = guard.read_text()
        for args in (["exec", "prompt"], ["--model", "claude-opus-5"], ["mcp", "list"],
                     ["plugin", "add", "unknown@evil", "--json"],
                     ["plugin", "add", "codex-orchestration@codex-orchestration", "--allow-incompatible-client"],
                     ["plugin", "marketplace", "add", "https://evil.invalid", "--ref", "main", "--json"]):
            with self.subTest(args=args), mock.patch.object(sys, "argv", [str(guard), *args]), \
                    mock.patch.object(subprocess, "call") as call, \
                    mock.patch.object(subprocess, "Popen") as popen:
                with self.assertRaises(SystemExit) as exc:
                    exec(compile(source, str(guard), "exec"), {})
                self.assertEqual(exc.exception.code, 97)
                call.assert_not_called()
                popen.assert_not_called()
        events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
        self.assertTrue(all(event == {"denied": "cli_dispatch"} for event in events))

    def test_guard_allows_only_metadata_caller_boundary(self):
        guard = probe.write_guard(self.root, Path("/never-execute-native"))
        with mock.patch.object(sys, "argv", [str(guard), "--version"]), \
                mock.patch.object(subprocess, "call", return_value=0) as call:
            with self.assertRaises(SystemExit):
                exec(compile(guard.read_text(), str(guard), "exec"), {})
        call.assert_called_once_with(["/never-execute-native", "--version"])

    def test_rpc_guard_refuses_inference_and_malformed_objects(self):
        guard = probe.write_guard(self.root, Path("/never-execute-native"))
        class Sink(io.BytesIO):
            def close(inner):
                inner.closed_by_guard = True
        for payload, denied in (({"method": "turn/start", "params": {"prompt": "secret"}}, "rpc_dispatch"),
                                ([], "malformed_rpc"),
                                ({"method": "config/read", "id": 1}, None)):
            sink = Sink()
            process = mock.Mock(stdin=sink)
            process.wait.return_value = 0
            def start_thread(*, target, daemon):
                return mock.Mock(start=target)
            with self.subTest(payload=payload), \
                    mock.patch.object(sys, "argv", [str(guard), "app-server", "--stdio"]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload) + "\n")), \
                    mock.patch.object(subprocess, "Popen", return_value=process), \
                    mock.patch("threading.Thread", side_effect=start_thread):
                with self.assertRaises(SystemExit):
                    exec(compile(guard.read_text(), str(guard), "exec"), {})
            events = [json.loads(line) for line in (self.root / "audit.jsonl").read_text().splitlines()]
            if denied:
                self.assertEqual(sink.getvalue(), b"")
                self.assertEqual(events[-1], {"denied": denied})
            else:
                self.assertEqual(json.loads(sink.getvalue()), payload)
                self.assertEqual(events[-1], {"rpc": "config/read"})
            self.assertNotIn("secret", json.dumps(events))

    def make_runner(self):
        env = probe.child_env(self.root)
        with mock.patch.object(probe.sys, "platform", "darwin"), \
                mock.patch.object(Path, "is_file", return_value=True):
            runner = probe.Runner(self.root, env, self.report())
        return runner

    def test_runner_uses_owned_cwd_sandbox_and_never_records_raw_outputs(self):
        runner = self.make_runner()
        def fake_popen(args, **kwargs):
            self.assertEqual(args[:2], ["/usr/bin/sandbox-exec", "-f"])
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"], runner.env)
            kwargs["stdout"].write(b"synthetic secret output")
            return mock.Mock(returncode=0)
        with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
            runner.run(["/mock/native", "--version"])
        result = runner.report["commands"][0]
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["stdout_sha256"], probe.digest(b"synthetic secret output"))
        with mock.patch.object(subprocess, "Popen") as popen:
            with self.assertRaises(probe.Blocked):
                runner.run(["/mock/native"], cwd=self.root.parent)
        popen.assert_not_called()
        profile = (self.root / "child.sb").read_text()
        self.assertIn("(deny network*)", profile)
        self.assertIn("(deny file-write*)", profile)

    def test_timeout_signals_only_identity_checked_owned_process_group(self):
        runner = self.make_runner()
        for pid, pgid, expected_signal in ((4321, 4321, True), (4321, 99, False), (1, 1, False)):
            process = mock.Mock(pid=pid)
            process.poll.return_value = None
            process.wait.side_effect = [subprocess.TimeoutExpired("mock", 60), 0]
            with self.subTest(pid=pid, pgid=pgid), \
                    mock.patch.object(subprocess, "Popen", return_value=process), \
                    mock.patch.object(os, "getpgid", return_value=pgid), \
                    mock.patch.object(os, "killpg") as kill:
                with self.assertRaisesRegex(probe.Blocked, "timed out"):
                    runner.run(["/mock/native"])
            self.assertEqual(kill.called, expected_signal)
            if expected_signal:
                self.assertEqual(kill.call_args.args[0], pid)

    def test_malformed_native_list_json_is_blocker(self):
        runner = self.make_runner()
        for output in ("not JSON", "[]", "null"):
            with self.subTest(output=output), mock.patch.object(runner, "run", return_value=
                    subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaisesRegex(probe.Blocked, "JSON"):
                    runner.json(["/mock/native"])

    def test_verifier_paragraph_mutation_is_detected_even_if_marker_survives(self):
        probe.seed_policy(self.root)
        before = probe.snapshot(self.root)
        config = self.root / "config.toml"
        config.write_text(config.read_text().replace(probe.PERSONAL, probe.PERSONAL + " changed"))
        with self.assertRaisesRegex(probe.Blocked, "personal_verifier"):
            probe.preserved(before, probe.snapshot(self.root))

    def test_audit_never_passes_missing_or_denied_dispatch_evidence(self):
        report = self.report()
        report["checks"]["containment"] = {"status": "PASS"}
        probe.audit_result(self.root, report)
        self.assertEqual(report["checks"]["no_model_calls"]["status"], "BLOCKED")
        audit = self.root / "audit.jsonl"
        audit.write_text(json.dumps({"cli": "--version"}) + "\n")
        probe.audit_result(self.root, report)
        self.assertEqual(report["checks"]["no_model_calls"]["status"], "PASS")
        audit.write_text(json.dumps({"denied": "rpc_dispatch"}) + "\n")
        probe.audit_result(self.root, report)
        self.assertEqual(report["checks"]["no_model_calls"]["status"], "BLOCKED")

    def load_native(self):
        directory = probe.ROOT / probe.PACKAGE / Path(probe.CONFIGURATOR).parent
        with mock.patch.object(sys, "path", [str(directory), *sys.path]):
            spec = importlib.util.spec_from_file_location("probe_native_fixture", directory / "configure_native_routing.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_registered_fixture_is_valid_but_disable_does_not_preserve_extension(self):
        native = self.load_native()
        probe.seed_policy(self.root)
        saved = json.loads((self.root / probe.STATE).read_text())
        native.validate_routing_state(saved)
        config = tomllib.loads((self.root / "config.toml").read_text())
        self.assertTrue(native._managed_matches(saved, native._current_values(config)))
        app = mock.Mock(codex_home=self.root)
        with mock.patch.object(native, "_batch_write", return_value={"status": "ok"}) as write, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(native._disable(app, config, "synthetic-version", saved, True), 0)
        edits = write.call_args.args[1]
        for suffix in ("multi_agent_mode_hint_text", "usage_hint_text"):
            self.assertTrue(any(e["keyPath"].endswith(suffix) and e["value"] is None for e in edits))
        self.assertFalse((self.root / probe.STATE).exists())
        self.assertTrue((self.root / "policy-receipt.txt").exists())

    def test_real_disable_refuses_unregistered_drift_before_any_write(self):
        native = self.load_native()
        probe.seed_policy(self.root, drift=True)
        before = probe.snapshot(self.root)
        state = json.loads((self.root / probe.STATE).read_text())
        config = tomllib.loads((self.root / "config.toml").read_text())
        with mock.patch.object(native, "_batch_write") as write:
            with self.assertRaisesRegex(native.ConfigurationError, "Refusing to erase"):
                native._disable(mock.Mock(codex_home=self.root), config, "v1", state, True)
        write.assert_not_called()
        self.assertEqual(before, probe.snapshot(self.root))

    def test_fixture_construction_preserves_existing_native_plugin_settings(self):
        (self.root / "config.toml").write_text('[plugins."codex-orchestration@codex-orchestration"]\nenabled = true\n')
        probe.seed_policy(self.root)
        config = tomllib.loads((self.root / "config.toml").read_text())
        self.assertTrue(config["plugins"]["codex-orchestration@codex-orchestration"]["enabled"])
        with self.assertRaises(probe.Blocked):
            probe.seed_policy(self.root)

    def test_mocked_native_migration_records_loss_and_still_exercises_rollback(self):
        report = self.report()
        with mock.patch.object(probe, "Runner", FakeRunner), \
                mock.patch.object(probe, "source_fixture", minimal_sources):
            probe.qualify(self.root, Path("/never-executed"), report)
        for key in ("upstream_lifecycle", "candidate_lifecycle", "identity_and_skills",
                    "registered_fixture", "unregistered_drift", "migration", "rollback", "cleanup_primitives"):
            self.assertEqual(report["checks"][key]["status"], "PASS", (key, report))
        for key in ("preservation", "rollback_preservation"):
            self.assertEqual(report["checks"][key]["status"], "BLOCKED")
            self.assertIn("personal_verifier", report["checks"][key]["reason"])
            self.assertIn("saved_state", report["checks"][key]["reason"])
        self.assertTrue(report["snapshots"]["before"]["personal_verifier_present"])
        self.assertFalse(report["snapshots"]["after_rollback"]["personal_verifier_present"])
        self.assertEqual(report["snapshots"]["before"]["receipt"], report["snapshots"]["after_rollback"]["receipt"])
        self.assertNotEqual(report["cache_roots"][probe.UPSTREAM], report["cache_roots"][probe.CANDIDATE])
        self.assertFalse(FakeRunner.instances[-1].packages)

    def test_missing_mandatory_native_upgrade_is_explicit_blocker(self):
        FakeRunner.fail_upgrade = True
        report = self.report()
        with mock.patch.object(probe, "Runner", FakeRunner), \
                mock.patch.object(probe, "source_fixture", minimal_sources):
            probe.qualify(self.root, Path("/never-executed"), report)
        self.assertEqual(report["checks"]["upstream_lifecycle"]["status"], "BLOCKED")
        self.assertIn("upgrade unavailable", report["checks"]["upstream_lifecycle"]["reason"])
        self.assertEqual(report["checks"]["rollback"]["status"], "BLOCKED")

    def test_report_survives_native_blocker_and_owned_temp_is_removed(self):
        report_path = self.root / "result.json"
        seen = []
        def blocked(temp, codex, report):
            seen.append(temp)
            (temp / "synthetic").write_text("owned disposable data")
            raise probe.Blocked("mandatory native primitive unavailable")
        with mock.patch.object(probe, "qualify", side_effect=blocked), redirect_stdout(io.StringIO()):
            status = probe.main(["--native", "--codex-bin", sys.executable,
                                 "--report", str(report_path)])
        self.assertEqual(status, 1)
        report = json.loads(report_path.read_text())
        self.assertEqual(report["qualification"], "BLOCKED")
        self.assertEqual(report["schema_version"], 1)
        self.assertIn("mandatory native primitive", report["blocker"])
        self.assertTrue(report["owned_temp_cleaned"])
        self.assertFalse(seen[0].exists())
        self.assertEqual(report["later_gates"][0]["status"], "BLOCKED")

    def test_source_fixture_uses_pinned_read_only_archive_and_only_three_fork_changes(self):
        names = (probe.MARKET, probe.PACKAGE + "/.codex-plugin/plugin.json",
                 probe.PACKAGE + "/.mcp.json", probe.PACKAGE + "/" + probe.CONFIGURATOR,
                 probe.PACKAGE + "/skills/codex-orchestration/SKILL.md")
        originals = {name: (probe.ROOT / name).read_bytes() for name in names}
        class ArchiveRunner:
            temp = self.root
            calls = []
            def run(inner, args, **kwargs):
                inner.calls.append((args, kwargs))
                if "archive" in args:
                    self.assertIn(probe.BASELINE, args)
                    self.assertEqual(args[1:3], ["-C", str(probe.ROOT)])
                    target = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
                    with tarfile.open(target, "w") as tar:
                        for name, value in originals.items():
                            info = tarfile.TarInfo(name)
                            info.size = len(value)
                            info.mode = 0o644
                            tar.addfile(info, io.BytesIO(value))
                else:
                    self.assertTrue(kwargs["cwd"].is_relative_to(self.root))
        runner = ArchiveRunner()
        report = self.report()
        roots = probe.source_fixture(runner, report)
        changed = {name for name in names if (roots[probe.UPSTREAM] / name).read_bytes()
                   != (roots[probe.CANDIDATE] / name).read_bytes()}
        self.assertEqual(changed, {probe.MARKET, probe.PACKAGE + "/.codex-plugin/plugin.json",
                                  probe.PACKAGE + "/" + probe.CONFIGURATOR})
        self.assertIn("codex-orchestration@" + probe.CANDIDATE,
                      (roots[probe.CANDIDATE] / probe.PACKAGE / probe.CONFIGURATOR).read_text())
        self.assertEqual({name: (probe.ROOT / name).read_bytes() for name in names}, originals)
        self.assertEqual(report["source_hashes"], {name: probe.digest(value) for name, value in originals.items()})

    def test_synthetic_source_archive_rejects_symlink(self):
        class ArchiveRunner:
            temp = self.root
            def run(inner, args, **kwargs):
                target = next(a.split("=", 1)[1] for a in args if a.startswith("--output="))
                with tarfile.open(target, "w") as tar:
                    info = tarfile.TarInfo("plugins/codex-orchestration/escape")
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/outside"
                    tar.addfile(info)
        with self.assertRaisesRegex(probe.Blocked, "links or oversized"):
            probe.source_fixture(ArchiveRunner(), self.report())
        self.assertFalse((self.root / probe.UPSTREAM).exists())


if __name__ == "__main__":
    unittest.main()
