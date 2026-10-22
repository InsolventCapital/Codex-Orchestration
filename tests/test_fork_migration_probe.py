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


def pinned_source(name):
    """Require local baseline history; never substitute checkout bytes or fetch."""
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(probe.ROOT), "show", f"{probe.BASELINE}:{name}"],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise AssertionError(
            f"Pinned fixture unavailable: {probe.BASELINE}:{name}; "
            "local Git history must contain the probe baseline (no fallback or fetch)"
        )
    return result.stdout


def encode_toml(config):
    """Test-only native config serialization; positive harness never writes TOML."""
    lines = []
    def emit(table, path):
        if path:
            lines.append("[" + ".".join(json.dumps(key) for key in path) + "]")
        for key, value in table.items():
            if not isinstance(value, dict):
                lines.append(json.dumps(key) + " = " + json.dumps(value))
        for key, value in table.items():
            if isinstance(value, dict):
                emit(value, [*path, key])
    emit(config, [])
    return "\n".join(lines) + "\n"


class FakeRunner:
    """Caller-boundary simulation; never starts Codex, Git, MCP, or a model."""
    instances = []
    fail_upgrade = False

    def __init__(self, temp, env, report, port=None):
        self.temp, self.env, self.report = temp, env, report
        self.packages = {}
        self.port = port
        self.sources = {}
        self.calls = []
        self.__class__.instances.append(self)

    def run(self, args, **kwargs):
        self.calls.append(args)
        if str(self.temp / "registration_read.py") in args:
            config = tomllib.loads((Path(self.env["CODEX_HOME"]) / "config.toml").read_text())
            registration = config.get("marketplaces", {}).get(args[-1])
            return subprocess.CompletedProcess(args, 0, json.dumps(registration), "")
        if "--show-toplevel" in args:
            return subprocess.CompletedProcess(args, 0, args[args.index("-C") + 1] + "\n", "")
        if "remote.origin.url" in args:
            market = Path(args[args.index("-C") + 1]).name
            return subprocess.CompletedProcess(args, 0, self.sources[market] + "\n", "")
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
        if str(self.temp / "cas.py") in args:
            config_file = Path(self.env["CODEX_HOME"]) / "config.toml"
            config = tomllib.loads(config_file.read_text()) if config_file.exists() else {}
            import re
            for edit in json.loads(args[-1]):
                parts = [a or b for a, b in re.findall(r'"([^"]+)"|([^.]+)', edit["keyPath"])]
                value = config
                for part in parts[:-1]:
                    value = value.setdefault(part, {})
                value[parts[-1]] = edit["value"]
                if len(parts) == 3 and parts[1] in self.packages:
                    self.packages[parts[1]]["enabled"] = edit["value"]
            config_file.write_text(encode_toml(config))
            return subprocess.CompletedProcess(args, 0, '{"status":"ok"}', "")
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
            if args[1] == "add":
                market = args[2].split("/")[-1][:-4]
                self.sources[market] = args[2]
            return {}
        plugin_id = args[1]
        market = plugin_id.split("@")[1]
        if args[0] == "remove":
            self.packages.pop(plugin_id, None)
            config_file = Path(self.env["CODEX_HOME"]) / "config.toml"
            config = tomllib.loads(config_file.read_text())
            config.get("plugins", {}).pop(plugin_id, None)
            config_file.write_text(encode_toml(config))
            return {}
        source = self.temp / market / probe.PACKAGE
        version = json.loads((source / ".codex-plugin/plugin.json").read_text())["version"]
        dest = self.temp / "codex" / "cache" / market / version
        if not dest.exists():
            shutil.copytree(source, dest)
        cache = Path(self.env["CODEX_HOME"]) / ".tmp/marketplaces" / market
        (cache / ".git").mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, cache / probe.PACKAGE, dirs_exist_ok=True)
        config_file = Path(self.env["CODEX_HOME"]) / "config.toml"
        config = tomllib.loads(config_file.read_text()) if config_file.exists() else {}
        config.setdefault("plugins", {}).setdefault(plugin_id, {})["enabled"] = True
        config.setdefault("marketplaces", {})[market] = {
            "source_type": "git", "source": self.sources[market],
            "ref": "main", "last_revision": "a" * 40}
        config_file.write_text(encode_toml(config))
        entry = {"marketplaceSource": {"sourceType": "git", "source": self.sources[market]},
                 "source": {"path": str(cache / probe.PACKAGE)},
                 "pluginId": plugin_id, "version": version, "enabled": True,
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
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file:http")

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
        for args in (["plugin", "disable", "codex-orchestration@codex-orchestration", "--json"],
                     ["exec", "prompt"], ["--model", "claude-opus-5"], ["mcp", "list"],
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

    def test_transport_guard_pins_exact_owned_git_urls(self):
        sources = probe.fixture_urls(43123)
        guard = probe.write_guard(self.root, Path("/never-execute-native"), sources)
        for source, allowed in ((sources[0], True), (sources[1], True),
                                (str(self.root / probe.UPSTREAM), False),
                                (sources[0].replace("43123", "43124"), False),
                                (sources[0].replace("127.0.0.1", "localhost"), False),
                                (sources[0] + "?proxy=evil", False),
                                (sources[0] + "/../other", False)):
            with self.subTest(source=source), \
                    mock.patch.object(sys, "argv", [str(guard), "plugin", "marketplace", "add",
                                                   source, "--ref", "main", "--json"]), \
                    mock.patch.object(subprocess, "call", return_value=0) as call:
                with self.assertRaises(SystemExit) as result:
                    exec(compile(guard.read_text(), str(guard), "exec"), {})
                self.assertEqual(result.exception.code, 0 if allowed else 97)
                self.assertEqual(call.called, allowed)
        for port in (0, -1, 65536, True, "123"):
            with self.assertRaises(probe.Blocked):
                probe.fixture_urls(port)
        with self.assertRaises(probe.Blocked):
            probe.write_guard(self.root, Path("/never"), ("https://evil.invalid/repo",))

    def test_rpc_cas_scope_refuses_policy_unowned_and_missing_versions(self):
        guard = probe.write_guard(self.root, Path("/never-execute-native"))
        valid = {"edits": [{"keyPath": probe.plugin_key(probe.UPSTREAM),
                            "value": False, "mergeStrategy": "replace"}],
                 "expectedVersion": "measured-version", "reloadUserConfig": True}
        class Sink(io.BytesIO):
            def close(self):
                pass
        variants = [valid, [], None, {"edits": [{}]}]
        for key, value in (("expectedVersion", None), ("expectedVersion", ""),
                           ("reloadUserConfig", False)):
            variants.append(dict(valid, **{key: value}))
        for path in ("features.multi_agent_v2.multi_agent_mode_hint_text",
                     "mcp_servers.claude-verifier-python3.enabled",
                     "plugins.evil.enabled"):
            variants.append(dict(valid, edits=[dict(valid["edits"][0], keyPath=path)]))
        variants.extend([dict(valid, edits=[]),
                         dict(valid, edits=valid["edits"] * 2),
                         dict(valid, edits=[dict(valid["edits"][0], value="false")]),
                         dict(valid, edits=[dict(valid["edits"][0], mergeStrategy="upsert")]),
                         dict(valid, edits=[dict(valid["edits"][0], keyPath=[])])])
        for params in variants:
            sink = Sink()
            process = mock.Mock(stdin=sink)
            process.wait.return_value = 0
            with self.subTest(params=params), \
                    mock.patch.object(sys, "argv", [str(guard), "app-server", "--stdio"]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(
                        {"id": 1, "method": "config/batchWrite", "params": params}) + "\n")), \
                    mock.patch.object(subprocess, "Popen", return_value=process), \
                    mock.patch("threading.Thread", side_effect=lambda target, daemon: mock.Mock(start=target)):
                with self.assertRaises(SystemExit):
                    exec(compile(guard.read_text(), str(guard), "exec"), {})
            self.assertEqual(bool(sink.getvalue()), params == valid)

    def test_synthetic_claude_metadata_never_allows_print_or_inference(self):
        probe.write_guard(self.root, Path("/never"))
        shim = (self.root / "bin/claude").read_text()
        for args in (["--print", "hello"], ["--help", "--print"], [], ["--model", "claude-fable-5"]):
            with self.subTest(args=args), mock.patch.object(sys, "argv", ["claude", *args]):
                with self.assertRaises(SystemExit) as result:
                    exec(compile(shim, "synthetic-claude", "exec"), {})
                self.assertEqual(result.exception.code, 97)
        for args in (["auth", "status", "--json"], ["--help"], ["--version"]):
            with self.subTest(args=args), mock.patch.object(sys, "argv", ["claude", *args]), \
                    redirect_stdout(io.StringIO()) as output:
                exec(compile(shim, "synthetic-claude", "exec"), {})
                self.assertTrue(output.getvalue())

    def test_native_cas_helper_uses_exact_layer_version_and_fails_closed(self):
        native = self.load_native()
        edits = [{"keyPath": probe.plugin_key(probe.UPSTREAM), "value": False,
                  "mergeStrategy": "replace"}]
        home = str(self.root)
        def layer(version, enabled):
            return {"layers": [{"name": {"type": "user", "profile": None},
                                "version": version,
                                "config": {"plugins": {"codex-orchestration@" + probe.UPSTREAM:
                                                        {"enabled": enabled}}}}]}
        for version, write_status, readback, fails in (
                ("exact-v1", "ok", False, False),
                (None, "ok", False, True), ("", "ok", False, True),
                ("exact-v1", "versionMismatch", False, True),
                ("exact-v1", "ok", True, True),
                ("exact-v1", "okOverridden", False, True)):
            app = mock.Mock()
            app.request.side_effect = [layer(version, True), layer("v2", readback)]
            with self.subTest(version=version, status=write_status, readback=readback), \
                    mock.patch.dict(sys.modules, {"configure_native_routing": native}), \
                    mock.patch.object(native, "AppServer") as server, \
                    mock.patch.object(native, "_batch_write", return_value={"status": write_status}) as write, \
                    mock.patch.object(sys, "argv", ["cas.py", "/fixture", "/guard", home, json.dumps(edits)]), \
                    mock.patch.object(sys, "path", list(sys.path)), redirect_stdout(io.StringIO()):
                server.return_value.__enter__.return_value = app
                if fails:
                    with self.assertRaises(RuntimeError):
                        exec(compile(probe.CAS_HELPER, "fixture-cas", "exec"), {})
                else:
                    exec(compile(probe.CAS_HELPER, "fixture-cas", "exec"), {})
                if version:
                    write.assert_called_once_with(app, edits, version, reload_user_config=True)
                else:
                    write.assert_not_called()

    def test_loopback_sandbox_and_root_server_lifecycle(self):
        env = probe.child_env(self.root)
        with mock.patch.object(probe.sys, "platform", "darwin"), \
                mock.patch.object(Path, "is_file", return_value=True):
            probe.Runner(self.root, env, self.report(), 43123)
        profile = (self.root / "child.sb").read_text()
        self.assertIn('(remote ip "localhost:43123")', profile)
        self.assertNotIn('(remote ip "127.0.0.1:', profile)
        self.assertNotIn('(remote ip "*', profile)
        self.assertIn('(deny network*)', profile)
        self.assertIn('(deny file-write*)', profile)
        self.assertIn('(subpath ' + json.dumps(str(self.root)) + ')', profile)
        self.assertNotIn("(allow network*)", profile)
        for port in (0, 65536, True, "43123"):
            with mock.patch.object(probe.sys, "platform", "darwin"), \
                    mock.patch.object(Path, "is_file", return_value=True), \
                    self.assertRaises(probe.Blocked):
                probe.Runner(self.root, env, self.report(), port)
        server = mock.Mock(server_port=43123)
        thread = mock.Mock()
        thread.is_alive.return_value = False
        with mock.patch.object(probe, "ThreadingHTTPServer", return_value=server) as create, \
                mock.patch.object(probe, "Thread", return_value=thread):
            with self.assertRaisesRegex(RuntimeError, "caller failure"):
                with probe.serve_git(self.root) as port:
                    self.assertEqual(port, 43123)
                    raise RuntimeError("caller failure")
        self.assertEqual(create.call_args.args[0], ("127.0.0.1", 0))
        handler_class = create.call_args.args[1].func
        for path, allowed in (("/" + probe.UPSTREAM + ".git/info/refs", True),
                              ("/" + probe.UPSTREAM + ".git/info/refs?service=git-upload-pack", True),
                              ("/" + probe.CANDIDATE + ".git/HEAD", True),
                              ("/" + probe.UPSTREAM + ".git/../outside", False),
                              ("/" + probe.UPSTREAM + ".git/%2e%2e/outside", False),
                              ("/" + probe.UPSTREAM + ".git/HEAD?evil=true", False),
                              ("/other.git/HEAD", False)):
            self.assertEqual(handler_class.permitted(mock.Mock(path=path)), allowed, path)
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        thread.join.assert_called_once_with(timeout=5)

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
        directory = Path(tempfile.mkdtemp(prefix="pinned-native-", dir=self.root))
        source_directory = Path(probe.PACKAGE) / Path(probe.CONFIGURATOR).parent
        for name in ("routing_state.py", "configure_native_routing.py"):
            (directory / name).write_bytes(pinned_source((source_directory / name).as_posix()))
        spec = importlib.util.spec_from_file_location("routing_state", directory / "routing_state.py")
        routing_state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(routing_state)
        # A cached checkout validator must never satisfy this pinned import.
        with mock.patch.dict(sys.modules, {"routing_state": routing_state}):
            spec = importlib.util.spec_from_file_location("probe_native_fixture", directory / "configure_native_routing.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def test_native_fixture_imports_exact_pin_and_leaves_checkout_unchanged(self):
        names = [(Path(probe.PACKAGE) / Path(probe.CONFIGURATOR).parent / name).as_posix()
                 for name in ("configure_native_routing.py", "routing_state.py")]
        checkout = {name: (probe.ROOT / name).read_bytes() for name in names}
        with mock.patch.dict(sys.modules, {"routing_state": mock.Mock()}), \
                mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
            native = self.load_native()
        self.assertTrue(all(call.args[0][:4] == ["/usr/bin/git", "-C", str(probe.ROOT), "show"]
                            for call in popen.call_args_list))
        self.assertEqual(native.PLUGIN_ID, "codex-orchestration@" + probe.UPSTREAM)
        fixture = Path(native.__file__)
        validator = Path(native.validate_routing_state.__code__.co_filename)
        self.assertTrue(fixture.is_relative_to(self.root))
        self.assertEqual(validator.parent, fixture.parent)
        for name, path in zip(names, (fixture, validator)):
            self.assertEqual(path.read_bytes(), pinned_source(name))
        self.assertEqual({name: (probe.ROOT / name).read_bytes() for name in names}, checkout)

    def test_pinned_fixture_unavailable_fails_without_fallback(self):
        with mock.patch.object(subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 128, b"", b"missing")) as run:
            with self.assertRaisesRegex(AssertionError, "Pinned fixture unavailable: " + probe.BASELINE):
                pinned_source(probe.PACKAGE + "/" + probe.CONFIGURATOR)
        run.assert_called_once()

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

    def test_active_fable_fixture_uses_valid_registered_namespace_state(self):
        native = self.load_native()
        probe.seed_policy(self.root, active=True)
        saved_bytes = (self.root / probe.STATE).read_bytes()
        state = json.loads(saved_bytes)
        native.validate_routing_state(state)
        config = tomllib.loads((self.root / "config.toml").read_text())
        self.assertTrue(native._managed_matches(state, native._current_values(config)))
        before = probe.snapshot(self.root)
        old = config["plugins"]["codex-orchestration@" + probe.UPSTREAM]
        old["enabled"] = False
        old["mcp_servers"][probe.FABLE_SERVERS[0]]["enabled"] = False
        target = config["plugins"]["codex-orchestration@" + probe.CANDIDATE]
        target["enabled"] = True
        target["mcp_servers"] = {server: {"enabled": i == 0}
                                 for i, server in enumerate(probe.FABLE_SERVERS)}
        (self.root / "config.toml").write_text(encode_toml(config))
        with mock.patch.object(native, "PLUGIN_ID", "codex-orchestration@" + probe.CANDIDATE):
            self.assertTrue(native._managed_matches(state, native._current_values(config)))
        probe.preserved(before, probe.snapshot(self.root))
        self.assertEqual((self.root / probe.STATE).read_bytes(), saved_bytes)

    def test_preservation_checks_full_hints_registration_and_all_unowned_fields(self):
        probe.seed_policy(self.root)
        original = (self.root / "config.toml").read_text()
        before = probe.snapshot(self.root)
        for old, new, field in (("Synthetic existing policy.", "Changed policy.", "mode_usage"),
                                ("Synthetic usage.", "Changed usage.", "mode_usage"),
                                ("fixture-verifier-never-run", "other-verifier", "verifier_registration"),
                                ("unowned-5c81", "changed-unowned", "unowned")):
            with self.subTest(field=field):
                (self.root / "config.toml").write_text(original.replace(old, new))
                with self.assertRaisesRegex(probe.Failed, field):
                    probe.preserved(before, probe.snapshot(self.root))
        report = self.report()
        probe.check(report, "preservation", lambda: probe.preserved(before, probe.snapshot(self.root)))
        self.assertEqual(report["checks"]["preservation"]["status"], "FAIL")

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

    def test_mocked_native_migration_preserves_active_state_and_exercises_rollback(self):
        report = self.report()
        with mock.patch.object(probe, "Runner", FakeRunner), \
                mock.patch.object(probe, "source_fixture", minimal_sources), \
                mock.patch.object(probe, "serve_git") as server:
            server.return_value.__enter__.return_value = 43123
            probe.qualify(self.root, Path("/never-executed"), report)
        for key in ("upstream_lifecycle", "candidate_lifecycle", "identity_and_skills",
                    "registered_fixture", "unregistered_drift", "migration", "rollback", "cleanup_primitives",
                    "preservation", "rollback_preservation", "disable_negative_control"):
            self.assertEqual(report["checks"][key]["status"], "PASS", (key, report))
        for stage in ("after_migration", "after_rollback"):
            probe.preserved(report["snapshots"]["before"], report["snapshots"][stage])
        self.assertEqual(report["negative_control"]["preservation"], "FAIL")
        self.assertEqual(report["remove_negative_control"]["preservation"], "FAIL")
        self.assertIn("retain both installed packages", report["transition_semantics"])
        for active, inventory in report["transition_inventory"].items():
            self.assertEqual({entry["plugin_id"]: entry["enabled"] for entry in inventory}, {
                "codex-orchestration@" + market: market == active
                for market in (probe.UPSTREAM, probe.CANDIDATE)})
        runner = FakeRunner.instances[-1]
        self.assertFalse(runner.packages)
        for market in (probe.UPSTREAM, probe.CANDIDATE):
            plugin_id = "codex-orchestration@" + market
            removals = [i for i, args in enumerate(runner.calls)
                        if args[1:4] == ["plugin", "remove", plugin_id]]
            self.assertTrue(removals)
            # Every install/re-add precedes destructive cleanup. Moving never removes.
            self.assertTrue(all(i < removals[0] for i, args in enumerate(runner.calls)
                                if args[1:4] == ["plugin", "add", plugin_id]))
            self.assertEqual(report["git_provenance"][market], {
                "sourceType": "git", "source": probe.fixture_urls(43123)[
                    (probe.UPSTREAM, probe.CANDIDATE).index(market)]})
            measured = report["git_measurements"][market]
            self.assertEqual(measured["requested_ref"], "main")
            self.assertEqual(measured["resolved_commit"], "a" * 40)
            self.assertEqual(measured["native_registration"]["ref"], measured["requested_ref"])
            self.assertEqual(set(measured["package_hashes"]), {
                ".codex-plugin/plugin.json", ".mcp.json",
                "skills/codex-orchestration/SKILL.md", probe.CONFIGURATOR})
            self.assertEqual(measured["package_hashes"], report["fixture_package_hashes"][market])
        marketplace_adds = [args for args in runner.calls if args[1:4] == ["plugin", "marketplace", "add"]]
        self.assertEqual(len(marketplace_adds), 2)
        self.assertTrue(all(args[4].startswith("http://127.0.0.1:43123/") for args in marketplace_adds))
        self.assertTrue(any("clone" in args and "--bare" in args for args in runner.calls))
        self.assertTrue(any("update-server-info" in args for args in runner.calls))
        edits = [json.loads(args[-1]) for args in runner.calls if str(self.root / "cas.py") in args]
        self.assertTrue(edits)
        self.assertTrue(all(edit["keyPath"] in probe.CAS_KEYS for batch in edits for edit in batch))
        self.assertFalse(any(args[1:3] == ["plugin", "disable"] for args in runner.calls))
        positive = [args for args in runner.calls if probe.CONFIGURATOR in " ".join(args)
                    and "--disable" in args]
        self.assertEqual(len(positive), 2)  # Only drift and negative profiles.
        self.assertTrue(all(any(name in " ".join(args) for name in ("drift-profile", "negative-control"))
                            for args in positive))

    def test_remove_deletes_plugin_local_unowned_settings_and_fails_preservation(self):
        env = probe.child_env(self.root)
        home = Path(env["CODEX_HOME"])
        probe.seed_policy(home, active=True)
        before = probe.snapshot(home)
        runner = FakeRunner(self.root, env, self.report())
        runner.json(["guard", "plugin", "remove", "codex-orchestration@" + probe.UPSTREAM, "--json"])
        with self.assertRaisesRegex(probe.Failed, "unowned"):
            probe.preserved(before, probe.snapshot(home))
        config = tomllib.loads((home / "config.toml").read_text())
        self.assertNotIn("codex-orchestration@" + probe.UPSTREAM, config["plugins"])

    def test_native_cache_identity_and_path_mismatches_block(self):
        for fault in ("origin", "HEAD", "root", "path", "symlink", "gitfile", "package",
                      "source_type", "source_url", "missing_source", "short_commit"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as raw:
                temp = Path(raw)
                env = probe.child_env(temp)
                runner = FakeRunner(temp, env, self.report())
                roots = minimal_sources(runner, runner.report)
                market = probe.UPSTREAM
                url = probe.fixture_urls(43123)[0]
                runner.sources[market] = url
                inventory = runner.json(["guard", "plugin", "add", "codex-orchestration@" + market, "--json"])
                original_run = runner.run

                def bad_run(args, **kwargs):
                    result = original_run(args, **kwargs)
                    if fault == "origin" and "remote.origin.url" in args:
                        result.stdout = url + "/wrong\n"
                    if fault == "HEAD" and args[-1] == "HEAD":
                        result.stdout = "b" * 40 + "\n"
                    if fault == "root" and "--show-toplevel" in args:
                        result.stdout = str(temp) + "\n"
                    return result

                if fault == "path":
                    inventory["source"]["path"] += "/../codex-orchestration"
                if fault == "symlink":
                    git_dir = Path(inventory["source"]["path"]).parents[1] / ".git"
                    git_dir.rmdir()
                    git_dir.symlink_to(temp)
                if fault == "gitfile":
                    git_dir = Path(inventory["source"]["path"]).parents[1] / ".git"
                    git_dir.rmdir()
                    git_dir.write_text("gitdir: /outside/fixture\n")
                if fault == "package":
                    (Path(inventory["source"]["path"]) / ".codex-plugin/plugin.json").write_text("{}")
                if fault == "source_type":
                    inventory["marketplaceSource"]["sourceType"] = "local"
                if fault == "source_url":
                    inventory["marketplaceSource"]["source"] = url + "/wrong"
                if fault == "missing_source":
                    del inventory["marketplaceSource"]
                with mock.patch.object(runner, "run", side_effect=bad_run), self.assertRaises(probe.Blocked):
                    probe.measure_git_cache(runner, Path(env["CODEX_HOME"]), market,
                                            inventory, url, "a" * (7 if fault == "short_commit" else 40),
                                            roots[market])

    def test_native_registration_missing_drift_and_malformed_refuse_measurement(self):
        faults = {
            "missing": None, "nonobject": [],
            "missing_ref": {"ref": None}, "wrong_ref": {"ref": "other"},
            "sha_as_ref": {"ref": "a" * 40}, "wrong_revision": {"last_revision": "b" * 40},
            "short_revision": {"last_revision": "a" * 7},
            "wrong_url": {"source": "https://wrong.invalid/fork.git"},
            "wrong_type": {"source_type": "local"},
        }
        for fault, delta in faults.items():
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as raw:
                temp = Path(raw)
                env = probe.child_env(temp)
                runner = FakeRunner(temp, env, self.report())
                roots = minimal_sources(runner, runner.report)
                market = probe.UPSTREAM
                url = probe.fixture_urls(43123)[0]
                runner.sources[market] = url
                inventory = runner.json(["guard", "plugin", "add", "codex-orchestration@" + market, "--json"])
                config_file = Path(env["CODEX_HOME"]) / "config.toml"
                config = tomllib.loads(config_file.read_text())
                if delta is None:
                    del config["marketplaces"][market]
                elif isinstance(delta, dict):
                    # TOML has no null; absent fields must fail, never default.
                    for key, value in delta.items():
                        if value is None:
                            del config["marketplaces"][market][key]
                        else:
                            config["marketplaces"][market][key] = value
                else:
                    config["marketplaces"][market] = delta
                config_file.write_text(encode_toml(config))
                with self.assertRaises(probe.Blocked):
                    probe.measure_git_cache(runner, Path(env["CODEX_HOME"]), market,
                                            inventory, url, "a" * 40, roots[market])

    def test_null_registration_revision_still_requires_independent_cache_head(self):
        env = probe.child_env(self.root)
        runner = FakeRunner(self.root, env, self.report())
        roots = minimal_sources(runner, runner.report)
        market = probe.UPSTREAM
        url = probe.fixture_urls(43123)[0]
        runner.sources[market] = url
        inventory = runner.json(["guard", "plugin", "add", "codex-orchestration@" + market, "--json"])
        config_file = Path(env["CODEX_HOME"]) / "config.toml"
        config = tomllib.loads(config_file.read_text())
        del config["marketplaces"][market]["last_revision"]
        config_file.write_text(encode_toml(config))
        result = probe.measure_git_cache(runner, Path(env["CODEX_HOME"]), market,
                                         inventory, url, "a" * 40, roots[market])
        self.assertEqual(result["resolved_commit"], "a" * 40)
        self.assertIsNone(result["native_registration"].get("last_revision"))
        original = runner.run

        def wrong_head(args, **kwargs):
            output = original(args, **kwargs)
            if args[-2:] == ["rev-parse", "HEAD"]:
                output.stdout = "b" * 40 + "\n"
            return output

        with mock.patch.object(runner, "run", side_effect=wrong_head), \
                self.assertRaisesRegex(probe.Blocked, "HEAD mismatch"):
            probe.measure_git_cache(runner, Path(env["CODEX_HOME"]), market,
                                    inventory, url, "a" * 40, roots[market])

    def test_package_inventory_mutations_block_all_three_comparison_sites(self):
        original_json = FakeRunner.json
        faults = ("extra_file", "extra_skill", "empty_directory", "symlink",
                  "dangling_symlink", "directory_symlink", "fifo", "changed", "missing",
                  "file_to_directory")
        for site in ("marketplace", "installed", "readd"):
            for market in (probe.UPSTREAM, probe.CANDIDATE):
                for fault in faults:
                    with self.subTest(site=site, market=market, fault=fault), \
                            tempfile.TemporaryDirectory(dir=self.root) as raw:
                        report = self.report()
                        injected = []

                        def mutate(runner, args):
                            result = original_json(runner, args)
                            if args[1:4] != ["plugin", "add", "codex-orchestration@" + market]:
                                return result
                            adds = sum(call[1:4] == args[1:4] for call in runner.calls)
                            if adds != (3 if site == "readd" else 2):
                                return result
                            package = Path(result["source"]["path"] if site == "marketplace"
                                           else result["installedPath"])
                            expected = package / "skills/codex-orchestration/SKILL.md"
                            extra = package / "unexpected"
                            if fault == "extra_file":
                                extra.write_text("unexpected payload")
                            elif fault == "extra_skill":
                                skill = package / "skills/unexpected/SKILL.md"
                                skill.parent.mkdir()
                                skill.write_text("unexpected discoverable skill")
                            elif fault == "empty_directory":
                                extra.mkdir()
                            elif fault == "symlink":
                                extra.symlink_to(expected)
                            elif fault == "dangling_symlink":
                                extra.symlink_to(package / "absent")
                            elif fault == "directory_symlink":
                                extra.symlink_to(package / "skills", target_is_directory=True)
                            elif fault == "fifo":
                                os.mkfifo(extra)
                            elif fault == "changed":
                                expected.write_text("changed expected bytes")
                            elif fault == "missing":
                                expected.unlink()
                            elif fault == "file_to_directory":
                                expected.unlink()
                                expected.mkdir()
                            injected.append(package)
                            return result

                        with mock.patch.object(probe, "Runner", FakeRunner), \
                                mock.patch.object(FakeRunner, "json", mutate), \
                                mock.patch.object(probe, "source_fixture", minimal_sources):
                            probe.qualify_fixture(Path(raw), Path("/never-executed"), report, 43123)
                        self.assertEqual(len(injected), 1)
                        stage = {
                            "marketplace": "upstream_lifecycle" if market == probe.UPSTREAM else "candidate_lifecycle",
                            "installed": "identity_and_skills",
                            "readd": "migration" if market == probe.CANDIDATE else "rollback",
                        }[site]
                        result = report["checks"][stage]
                        self.assertEqual(result["status"], "BLOCKED", report)
                        self.assertIn({"marketplace": "native marketplace package differs from publisher",
                                       "installed": "native cache differs from synthetic fixture source",
                                       "readd": "native re-add changed installed package bytes"}[site],
                                      result["reason"])
                        if site == "readd":
                            self.assertNotIn(market, report.get("transition_inventory", {}))
                            self.assertEqual(report["checks"]["identity_and_skills"]["status"], "PASS")
                            if market == probe.UPSTREAM:
                                self.assertEqual(report["checks"]["migration"]["status"], "PASS")

    def test_package_comparison_rejects_unsafe_publisher_entries(self):
        for fault in ("symlink", "dangling_symlink", "fifo"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(dir=self.root) as raw:
                root = Path(raw)
                expected, actual = root / "expected", root / "actual"
                expected.mkdir()
                (expected / "payload").write_text("fixture")
                shutil.copytree(expected, actual)
                extra = expected / "unexpected"
                if fault == "fifo":
                    os.mkfifo(extra)
                else:
                    extra.symlink_to(expected / ("payload" if fault == "symlink" else "absent"))
                with self.assertRaisesRegex(probe.Blocked, "unsafe package entry"):
                    probe.compare_package(expected, actual, root, "publisher comparison")

    def test_readd_cache_head_mismatch_blocks_rollback(self):
        report = self.report()
        original_json = FakeRunner.json
        def changed_head_on_readd(runner, args):
            result = original_json(runner, args)
            if args[1:4] == ["plugin", "add", "codex-orchestration@" + probe.UPSTREAM]:
                adds = sum(call[1:4] == args[1:4] for call in runner.calls)
                if adds == 3:
                    original_run = runner.run

                    def wrong_head(command, **kwargs):
                        value = original_run(command, **kwargs)
                        if command[-1] == "HEAD" and "/.tmp/marketplaces/" in " ".join(command):
                            value.stdout = "b" * 40 + "\n"
                        return value
                    runner.run = wrong_head
            return result
        with mock.patch.object(probe, "Runner", FakeRunner), \
                mock.patch.object(FakeRunner, "json", changed_head_on_readd), \
                mock.patch.object(probe, "source_fixture", minimal_sources), \
                mock.patch.object(probe, "serve_git") as server:
            server.return_value.__enter__.return_value = 43123
            probe.qualify(self.root, Path("/never-executed"), report)
        self.assertEqual(report["checks"]["preservation"]["status"], "PASS")
        self.assertEqual(report["checks"]["rollback"]["status"], "BLOCKED")
        self.assertIn("HEAD mismatch", report["checks"]["rollback"]["reason"])

    def test_late_native_or_server_failure_cannot_report_pass(self):
        def late_failure(temp, codex, report):
            for name in report["checks"]:
                report["checks"][name] = {"status": "PASS"}
            (temp / "audit.jsonl").write_text('{"cli":"--version"}\n')
            raise probe.Blocked("owned loopback server did not shut down")
        target = self.root / "late-failure.json"
        with mock.patch.object(probe, "qualify", side_effect=late_failure), redirect_stdout(io.StringIO()):
            status = probe.main(["--native", "--codex-bin", sys.executable, "--report", str(target)])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(target.read_text())["qualification"], "BLOCKED")

    def test_missing_mandatory_native_upgrade_is_explicit_blocker(self):
        FakeRunner.fail_upgrade = True
        report = self.report()
        with mock.patch.object(probe, "Runner", FakeRunner), \
                mock.patch.object(probe, "source_fixture", minimal_sources), \
                mock.patch.object(probe, "serve_git") as server:
            server.return_value.__enter__.return_value = 43123
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
        checkout = {name: (probe.ROOT / name).read_bytes() for name in names}
        originals = {name: pinned_source(name) for name in names}
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
        self.assertEqual({name: (probe.ROOT / name).read_bytes() for name in names}, checkout)
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
