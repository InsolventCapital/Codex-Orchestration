#!/usr/bin/env python3
"""Phase A disposable LOCAL_FIXTURE qualification; never a migration helper.

Threat model: inherited credentials/config, hostile output paths, Git hooks,
network/model dispatch, cache identity collisions, and destructive restoration.
Children run with an allowlisted environment and a mandatory OS write/network
sandbox. The RPC guard permits config/catalog operations only. No prompt, MCP,
agent, or model execution is permitted. Missing containment is a blocker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "5ac2b355ffd42f7b857d1301fec43a6f93744c05"
UPSTREAM = "codex-orchestration"
CANDIDATE = "insolvent-capital-codex-orchestration"
PACKAGE = "plugins/codex-orchestration"
MARKET = ".agents/plugins/marketplace.json"
CONFIGURATOR = "skills/codex-orchestration/scripts/configure_native_routing.py"
STATE = ".codex-orchestration-routing.json"
PERSONAL = "Owner personal Verifier extension: fixture-only unique paragraph 8e37."
RECEIPT = "Synthetic owner policy receipt 7f19; never production authority."
REQUIRED = (
    "containment", "source", "native_version", "upstream_lifecycle",
    "candidate_lifecycle", "identity_and_skills", "registered_fixture",
    "unregistered_drift", "migration", "preservation", "rollback",
    "rollback_preservation", "cleanup_primitives", "bare_fable_exclusion", "no_model_calls",
)


class Blocked(RuntimeError):
    pass


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def owned(path: Path, root: Path) -> Path:
    """Reject traversal and symlink components, including dangling links."""
    if not path.is_absolute() or ".." in path.parts:
        raise Blocked("path must be absolute without traversal")
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise Blocked("path escapes owned fixture") from exc
    for item in (path, *path.parents):
        if item.is_symlink():
            raise Blocked("symlink in owned path")
        if item == root:
            break
    return path


def reserve_report(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise Blocked("report must be absolute without traversal")
    require(not any(part.startswith(".") for part in path.parts if part not in ("/",)),
            "report cannot target hidden configuration or Git directories")
    require(not path.is_relative_to(Path.home() / "Library"),
            "report cannot target personal Library caches or credentials")
    require(not path.is_relative_to(ROOT), "report must be outside the source worktree")
    directory = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open("/", flags)
        for component in path.parent.parts[1:]:
            next_directory = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
            info = os.fstat(directory)
            require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX),
                    "report has a writable non-sticky ancestor")
        parent = os.fstat(directory)
        require(parent.st_uid == os.getuid() and not parent.st_mode & 0o022,
                "report parent must be owned and not group/world writable")
        return os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=directory)
    except OSError as exc:
        raise Blocked("report exists, has a symlink ancestor, or cannot be created safely") from exc
    finally:
        if directory is not None:
            os.close(directory)



def child_env(temp: Path) -> dict[str, str]:
    # Allowlist rather than trying to enumerate every vendor's secret names.
    env = {
        "PATH": str(temp / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
    }
    for key, name in (("HOME", "home"), ("CODEX_HOME", "codex"),
                      ("XDG_CONFIG_HOME", "xdg"), ("TMPDIR", "tmp"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
        path = owned(temp / name, temp)
        path.mkdir(mode=0o700)
        env[key] = str(path)
    return env


# This wrapper is a dispatch firewall, including configurator AppServer traffic.
# It never interprets a model name as permission to invoke it.
GUARD = r'''import json, subprocess, sys, threading
from pathlib import Path
REAL = __REAL__
AUDIT = Path(__AUDIT__)
ALLOWED_RPC = {"initialize", "initialized", "config/read", "config/batchWrite", "model/list"}
def log(event):
    with AUDIT.open("a") as f:
        f.write(json.dumps(event) + "\n")
args = sys.argv[1:]
def allowed(args):
    if args == ["--version"] or args == ["app-server", "--stdio"]:
        return True
    if args and args[0] == "plugin":
        if args == ["plugin", "list", "--json"]:
            return True
        ids = {"codex-orchestration@codex-orchestration",
               "codex-orchestration@insolvent-capital-codex-orchestration"}
        names = {"codex-orchestration", "insolvent-capital-codex-orchestration"}
        if len(args) == 4 and args[1] in {"add", "disable", "remove"}:
            return args[2] in ids and args[3] == "--json"
        if args[1:2] == ["marketplace"]:
            if len(args) == 5 and args[2] in {"upgrade", "remove"}:
                return args[3] in names and args[4] == "--json"
            if len(args) == 7 and args[2] == "add" and args[4:] == ["--ref", "main", "--json"]:
                source = Path(args[3])
                return source.parent == AUDIT.parent and source.name in names
        return False
    rest = list(args)
    while rest[:1] == ["-c"] and len(rest) >= 2:
        if not rest[1].startswith("features.multi_agent_v2."):
            return False
        rest = rest[2:]
    return rest == ["features", "list"]
if not allowed(args):
    log({"denied": "cli_dispatch"})
    sys.exit(97)
log({"cli": "app-server" if args[:1] == ["app-server"] else args[0]})
if args != ["app-server", "--stdio"]:
    sys.exit(subprocess.call([REAL, *args]))
p = subprocess.Popen([REAL, *args], stdin=subprocess.PIPE)
def forward():
    try:
        for line in sys.stdin:
            if len(line) > 1000000:
                raise ValueError("oversized RPC")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("non-object RPC")
            method = request.get("method")
            if method not in ALLOWED_RPC:
                log({"denied": "rpc_dispatch"})
                return
            log({"rpc": method})
            p.stdin.write(line.encode())
            p.stdin.flush()
    except (ValueError, BrokenPipeError, OSError):
        log({"denied": "malformed_rpc"})
    finally:
        try:
            p.stdin.close()
        except BrokenPipeError:
            pass
threading.Thread(target=forward, daemon=True).start()
sys.exit(p.wait())
'''


def write_guard(temp: Path, codex: Path) -> Path:
    bindir = temp / "bin"
    bindir.mkdir()
    guard = bindir / "codex"
    guard.write_text(f"#!{sys.executable}\n" + GUARD.replace(
        "__REAL__", repr(str(codex))).replace("__AUDIT__", repr(str(temp / "audit.jsonl"))))
    guard.chmod(0o700)
    # Any accidental Claude dispatch is refused, including --print/model calls.
    claude = bindir / "claude"
    claude.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                      f"p = Path({str(temp / 'claude-denied')!r})\n"
                      "p.write_text('dispatch refused')\nsys.exit(97)\n")
    claude.chmod(0o700)
    return guard


class Runner:
    def __init__(self, temp: Path, env: dict[str, str], report: dict):
        self.temp, self.env, self.report = temp, env, report
        sandbox = Path("/usr/bin/sandbox-exec")
        if sys.platform != "darwin" or not sandbox.is_file():
            raise Blocked("mandatory sandbox-exec network/write containment unavailable")
        profile = temp / "child.sb"
        profile.write_text('(version 1) (allow default) (deny network*) '
                           '(deny file-write*) (allow file-write* (subpath '
                           + json.dumps(str(temp)) + ') (literal "/dev/null"))')
        self.prefix = [str(sandbox), "-f", str(profile)]

    def run(self, args: list[str], *, cwd: Path | None = None,
            env: dict[str, str] | None = None, required: bool = True) -> subprocess.CompletedProcess:
        cwd = owned(cwd or self.temp, self.temp)
        result = None
        try:
            with tempfile.TemporaryFile(dir=self.temp) as out, \
                    tempfile.TemporaryFile(dir=self.temp) as err:
                proc = subprocess.Popen([*self.prefix, *args], cwd=cwd,
                                        env=env or self.env, stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, start_new_session=True)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    # Only the group created above; never kill by name or user.
                    if proc.poll() is None and proc.pid > 1 and os.getpgid(proc.pid) == proc.pid:
                        os.killpg(proc.pid, signal.SIGKILL)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise Blocked("owned child did not exit; no unrelated process signalled") from None
                    raise Blocked("owned child command timed out") from None
                out.seek(0)
                err.seek(0)
                stdout, stderr = out.read(4_000_001), err.read(4_000_001)
                if len(stdout) > 4_000_000 or len(stderr) > 4_000_000:
                    raise Blocked("child output exceeded bounded result size")
                result = subprocess.CompletedProcess(args, proc.returncode,
                                                     stdout.decode(errors="replace"),
                                                     stderr.decode(errors="replace"))
                self.report["commands"].append({
                    "operation": Path(args[0]).name,
                    "arguments": [a.replace(str(self.temp), "$FIXTURE") for a in args[1:]],
                    "returncode": result.returncode,
                    "stdout_sha256": digest(stdout), "stderr_sha256": digest(stderr),
                })
        except OSError as exc:
            raise Blocked("child process unavailable: " + type(exc).__name__) from exc
        if required and result.returncode:
            raise Blocked(f"native primitive failed (exit {result.returncode}); "
                          "see sanitized command and output hashes")
        return result

    def json(self, args: list[str]) -> dict:
        try:
            value = json.loads(self.run(args).stdout)
        except ValueError as exc:
            raise Blocked("native primitive returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise Blocked("native primitive returned non-object JSON")
        return value


def check(report: dict, name: str, operation) -> bool:
    try:
        operation()
    except Exception as exc:
        report["checks"][name] = {"status": "BLOCKED", "reason": failure_reason(exc)}
        return False
    report["checks"][name] = {"status": "PASS"}
    return True


def failure_reason(exc: Exception) -> str:
    # Preserve explicit measured reasons, never echo arbitrary child payloads.
    return str(exc) if isinstance(exc, Blocked) else type(exc).__name__ + " while measuring fixture"


def require(value: bool, reason: str) -> None:
    if not value:
        raise Blocked(reason)


def source_fixture(runner: Runner, report: dict) -> dict[str, Path]:
    # archive reads a pinned commit; it never clones/runs hooks from the worktree.
    git = "/usr/bin/git"
    target = runner.temp / "source.tar"
    runner.run([git, "-C", str(ROOT), "archive", "--output=" + str(target),
                BASELINE, PACKAGE, MARKET])
    files = {}
    total_bytes = 0
    with tarfile.open(target) as tar:
        for member in tar:
            path = Path(member.name)
            require(not path.is_absolute() and ".." not in path.parts,
                    "unsafe source archive member")
            if member.isdir():
                continue
            require(member.isfile() and member.size < 2_000_000,
                    "source archive contains links or oversized members")
            require(member.name == MARKET or member.name.startswith(PACKAGE + "/"),
                    "unexpected source archive path")
            total_bytes += member.size
            require(member.name not in files and len(files) < 1000 and total_bytes <= 50_000_000,
                    "duplicate or excessive archive members")
            files[member.name] = (tar.extractfile(member).read(), member.mode)
    report["source_hashes"] = {name: digest(data) for name, (data, _) in files.items()}
    roots = {}
    for market in (UPSTREAM, CANDIDATE):
        root = runner.temp / market
        root.mkdir()
        for name, (data, mode) in files.items():
            dest = owned(root / name, runner.temp)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            dest.chmod(mode & 0o777)
        if market == CANDIDATE:
            for name, key, value in ((MARKET, "name", CANDIDATE),
                                     (PACKAGE + "/.codex-plugin/plugin.json", "version", "0.9.5")):
                path = root / name
                payload = json.loads(path.read_text())
                payload[key] = value
                path.write_text(json.dumps(payload, indent=2) + "\n")
            path = root / PACKAGE / CONFIGURATOR
            text = path.read_text()
            old = f'PLUGIN_ID = "codex-orchestration@{UPSTREAM}"'
            require(text.count(old) == 1, "fixture PLUGIN_ID replacement ambiguous")
            path.write_text(text.replace(old, f'PLUGIN_ID = "codex-orchestration@{CANDIDATE}"'))
        manifest = root / PACKAGE / ".codex-plugin/plugin.json"
        payload = json.loads(manifest.read_text())
        payload["version"] = "0.9.4" if market == CANDIDATE else "0.9.3"
        manifest.write_text(json.dumps(payload, indent=2) + "\n")
        mcp = json.loads((root / PACKAGE / ".mcp.json").read_text())["mcpServers"]
        require(all(v.get("enabled") is False for v in mcp.values()),
                "source fixture contains enabled model servers")
        for args in (["-c", "init.templateDir=", "init", "--quiet", "--initial-branch=main"],
                     ["-c", "core.hooksPath=/dev/null", "add", "--", PACKAGE, MARKET],
                     ["-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                      "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                      "commit", "--quiet", "-m", "Synthetic LOCAL_FIXTURE"]):
            runner.run([git, *args], cwd=root)
        roots[market] = root
    return roots


def seed_policy(home: Path, *, drift: bool = False) -> None:
    """Fixture construction, not migration: register managed extension bytes together."""
    mode = "[codex-orchestration managed-policy v1]\nSynthetic existing policy.\n\n" + PERSONAL
    usage = "[codex-orchestration managed-policy v1]\nSynthetic usage.\n\n" + PERSONAL
    state = {
        "schema": 5, "policy_version": 5, "managed_by": "codex-orchestration",
        "config_file": str(home / "config.toml"),
        "executor": {"kind": "model", "model": "gpt-5.6-luna", "effort": "high"},
        "planner": None, "advisor": None, "designer": None,
        "managed": {"mode": mode, "usage": usage, "metadata": False, "namespace": "agents"},
        "previous": {key: {"known": True, "present": False}
                     for key in ("mode", "usage", "metadata", "namespace")},
        "scalar_origin": None, "managed_feature": None,
    }
    for name in (STATE, "config.toml", "policy-receipt.txt"):
        owned(home / name, home)
    config = home / "config.toml"
    existing = config.read_text() if config.exists() else ""
    parsed = tomllib.loads(existing)
    require(not (home / STATE).exists()
            and not any(k in parsed for k in ("model", "probe_unowned", "features")),
            "fixture construction would overwrite existing config fields or state")
    (home / STATE).write_text(json.dumps(state, indent=2) + "\n")
    if drift:
        mode += "\nUNREGISTERED POST-SETUP DRIFT 93f4"
    config.write_text(
        'model = "fixture-unowned-model"\n'
        '[probe_unowned]\nsentinel = "unowned-5c81"\n'
        '[features.multi_agent_v2]\nhide_spawn_agent_metadata = false\n'
        'tool_namespace = "agents"\nmulti_agent_mode_hint_text = ' + json.dumps(mode)
        + '\nusage_hint_text = ' + json.dumps(usage) + '\n' + existing)
    (home / "policy-receipt.txt").write_text(RECEIPT)


def snapshot(home: Path) -> dict:
    for name in (STATE, "config.toml", "policy-receipt.txt"):
        owned(home / name, home)
    config_path = home / "config.toml"
    raw = config_path.read_bytes()
    config = tomllib.loads(raw.decode())
    feature = config.get("features", {}).get("multi_agent_v2", {})
    paragraphs = [feature.get(key, "") for key in ("multi_agent_mode_hint_text", "usage_hint_text")]
    extension = [[paragraph for paragraph in text.split("\n\n")
                  if "Owner personal Verifier extension:" in paragraph] for text in paragraphs]
    state = home / STATE
    return {
        "personal_verifier": digest(json.dumps(extension).encode()),
        "personal_verifier_present": all(extension),
        "receipt": digest((home / "policy-receipt.txt").read_bytes()),
        "saved_state": digest(state.read_bytes()) if state.exists() else None,
        "unowned": digest(json.dumps({"model": config.get("model"),
                                      "probe_unowned": config.get("probe_unowned")},
                                     sort_keys=True).encode()),
        "config": digest(raw),
    }


def preserved(before: dict, after: dict) -> None:
    lost = [key for key in ("personal_verifier", "receipt", "saved_state", "unowned")
            if before[key] != after[key]]
    require(not lost, "supported native path changed/dropped synthetic " + ", ".join(lost))


def qualify(temp: Path, codex: Path, report: dict) -> None:
    env = child_env(temp)
    guard = write_guard(temp, codex)
    runner = Runner(temp, env, report)
    runner.run(["/usr/bin/true"])
    report["checks"]["containment"] = {"status": "PASS", "reason": "OS denies network and writes outside fixture"}
    roots = source_fixture(runner, report)
    report["checks"]["source"] = {"status": "PASS"}
    version = runner.run([str(guard), "--version"]).stdout.strip()
    require(version.startswith("codex-cli ") and len(version) < 200
            and all(c.isalnum() or c in " .-_+()" for c in version), "unrecognized CLI version")
    report["cli"]["version"] = version
    report["checks"]["native_version"] = {"status": "PASS"}
    installed = {}
    home_root = Path(env["CODEX_HOME"])

    def no_bare_fable():
        report["checks"]["bare_fable_exclusion"] = {
            "status": "BLOCKED", "reason": "checking fixture defaults and native config overrides"}
        config_path = home_root / "config.toml"
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        for market in (UPSTREAM, CANDIDATE):
            settings = config.get("plugins", {}).get(f"codex-orchestration@{market}", {})
            overrides = settings.get("mcp_servers", {})
            require(not any(server.startswith("fable-advisor-") and value.get("enabled") is True
                            for server, value in overrides.items()),
                    "unexpected enabled bare Fable override in disposable profile")
        report["checks"]["bare_fable_exclusion"] = {
            "status": "PASS", "reason": "fixture defaults disabled; config overrides checked between native operations"}

    def cli(*args):
        no_bare_fable()
        result = runner.json([str(guard), "plugin", *args, "--json"])
        no_bare_fable()
        return result

    def disable(market):
        cli("disable", f"codex-orchestration@{market}")
        values = cli("list").get("installed", [])
        matches = [e for e in values if isinstance(e, dict)
                   and e.get("pluginId") == f"codex-orchestration@{market}"]
        require(len(matches) == 1 and matches[0].get("enabled") is False,
                "native disable did not disable the distribution")

    def entry(market):
        values = cli("list").get("installed")
        require(isinstance(values, list), "plugin list missing installed array")
        matches = [e for e in values if isinstance(e, dict)
                   and e.get("pluginId") == f"codex-orchestration@{market}"]
        require(len(matches) == 1, "plugin ID not discovered uniquely")
        require(matches[0].get("enabled") is True, "installed plugin is not enabled")
        return matches[0]

    def install(market):
        cli("marketplace", "add", str(roots[market]), "--ref", "main")
        result = cli("add", f"codex-orchestration@{market}")
        path = owned(Path(result["installedPath"]), home_root)
        expected = "0.9.5" if market == CANDIDATE else "0.9.4"
        previous = "0.9.4" if market == CANDIDATE else "0.9.3"
        require(result.get("version") == previous and entry(market).get("version") == previous,
                "initial synthetic install/discovery mismatch")
        require(path.is_dir(), "native installedPath missing")
        manifest = roots[market] / PACKAGE / ".codex-plugin/plugin.json"
        payload = json.loads(manifest.read_text())
        payload["version"] = expected
        manifest.write_text(json.dumps(payload, indent=2) + "\n")
        for args in (["-c", "core.hooksPath=/dev/null", "add", "--", str(manifest)],
                     ["-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                      "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                      "commit", "--quiet", "-m", "Synthetic version upgrade"]):
            runner.run(["/usr/bin/git", *args], cwd=roots[market])
        upgrade = cli("marketplace", "upgrade", market)
        require(upgrade.get("errors") == [] and upgrade.get("selectedMarketplaces") == [market],
                "mandatory marketplace upgrade unavailable or failed")
        refreshed = cli("add", f"codex-orchestration@{market}")
        require(refreshed.get("version") == expected and entry(market).get("version") == expected,
                "post-upgrade add did not discover expected version")
        installed[market] = owned(Path(refreshed["installedPath"]), home_root)
        require(installed[market].is_dir(), "refreshed installedPath missing")
        require(path != installed[market], "upgrade reused prior version cache root")

    if not check(report, "upstream_lifecycle", lambda: install(UPSTREAM)):
        return
    if not check(report, "candidate_lifecycle", lambda: install(CANDIDATE)):
        return

    def identities():
        require(installed[UPSTREAM] != installed[CANDIDATE]
                and not installed[UPSTREAM].is_relative_to(installed[CANDIDATE])
                and not installed[CANDIDATE].is_relative_to(installed[UPSTREAM]), "cache roots collide")
        report["fixture_package_hashes"] = {}
        for market, path in installed.items():
            hashes = {}
            for source in sorted((roots[market] / PACKAGE).rglob("*")):
                if source.is_file():
                    relative = source.relative_to(roots[market] / PACKAGE)
                    cached = owned(path / relative, temp)
                    require(cached.is_file() and cached.read_bytes() == source.read_bytes(),
                            "native cache differs from synthetic fixture source")
                    hashes[relative.as_posix()] = digest(source.read_bytes())
            report["fixture_package_hashes"][market] = hashes
            skill = path / "skills/codex-orchestration/SKILL.md"
            require(skill.read_bytes() == (roots[market] / PACKAGE / "skills/codex-orchestration/SKILL.md").read_bytes(),
                    "stable skill label/content discovery changed")
            mcp = json.loads((path / ".mcp.json").read_text())["mcpServers"]
            require(all(value.get("enabled") is False for value in mcp.values()),
                    "bare fixture Fable servers must be disabled by default")
        report["cache_roots"] = {k: str(v.relative_to(temp)) for k, v in installed.items()}
    if not check(report, "identity_and_skills", identities):
        return
    home = Path(env["CODEX_HOME"])
    # Both packages are safe disabled-by-default; only one distribution stays enabled.
    disable(CANDIDATE)
    seed_policy(home)
    report["fixture_construction"] = "synthetic schema-5 registered extension: config and managed state seeded together; no native Verifier field"
    before = snapshot(home)
    report["snapshots"] = {"before": before}

    def routing(market, *args, child_environment=None, required=True):
        no_bare_fable()
        result = runner.run([sys.executable, "-B", str(installed[market] / CONFIGURATOR),
                             "--codex-bin", str(guard), "--codex-home",
                             (child_environment or env)["CODEX_HOME"], *args],
                            env=child_environment, required=required)
        no_bare_fable()
        return result

    def validate_fixture():
        # Validator and --status prove supported registered state without inference.
        result = routing(UPSTREAM, "--status", "--require-effective")
        require("Native policy: installed and effective" in result.stdout,
                "synthetic registered policy not effective")
    if not check(report, "registered_fixture", validate_fixture):
        return

    def drift_check():
        drift_home = temp / "drift-profile"
        drift_home.mkdir()
        seed_policy(drift_home, drift=True)
        drift_env = dict(env, CODEX_HOME=str(drift_home))
        prior = snapshot(drift_home)
        result = routing(UPSTREAM, "--disable", "--apply", child_environment=drift_env, required=False)
        require(result.returncode != 0 and "Refusing to erase" in result.stderr + result.stdout,
                "unregistered drift was not explicitly refused")
        require(prior == snapshot(drift_home), "drift refusal erased synthetic bytes")
        report["snapshots"]["drift_before"] = prior
        report["snapshots"]["drift_after"] = snapshot(drift_home)
    check(report, "unregistered_drift", drift_check)

    def migrate():
        routing(UPSTREAM, "--disable", "--apply")
        report["snapshots"]["after_disable"] = snapshot(home)
        disable(UPSTREAM)
        cli("add", f"codex-orchestration@{CANDIDATE}")
        routing(CANDIDATE, "--executor-model", "gpt-5.6-luna", "--executor-effort", "high", "--apply")
    check(report, "migration", migrate)
    report["snapshots"]["after_migration"] = snapshot(home)
    check(report, "preservation", lambda: preserved(before, snapshot(home)))

    def rollback():
        routing(CANDIDATE, "--disable", "--apply")
        disable(CANDIDATE)
        cli("remove", f"codex-orchestration@{CANDIDATE}")
        cli("add", f"codex-orchestration@{UPSTREAM}")
        routing(UPSTREAM, "--executor-model", "gpt-5.6-luna", "--executor-effort", "high", "--apply")
        entry(UPSTREAM)
    check(report, "rollback", rollback)
    report["snapshots"]["after_rollback"] = snapshot(home)
    check(report, "rollback_preservation", lambda: preserved(before, snapshot(home)))

    def cleanup():
        for market in (CANDIDATE, UPSTREAM):
            # Remove candidate only if rollback did not already remove it.
            entries = cli("list").get("installed", [])
            if any(e.get("pluginId") == f"codex-orchestration@{market}" for e in entries):
                disable(market)
                cli("remove", f"codex-orchestration@{market}")
            cli("marketplace", "remove", market)
        require(not cli("list").get("installed"), "native removal left installed fixtures")
    check(report, "cleanup_primitives", cleanup)


def audit_result(temp: Path, report: dict) -> None:
    audit = temp / "audit.jsonl"
    try:
        events = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        require(all(isinstance(event, dict) for event in events), "malformed dispatch audit")
    except (OSError, ValueError, Blocked):
        report["checks"]["no_model_calls"] = {"status": "BLOCKED", "reason": "malformed or unreadable dispatch audit"}
        return
    denied = any("denied" in e for e in events) or (temp / "claude-denied").exists()
    report["no_model_calls"] = {
        "cli_and_rpc_dispatch_allowlisted": True,
        "network_denied_by_os": report["checks"]["containment"]["status"] == "PASS",
        "audit_sha256": digest(json.dumps(events, sort_keys=True).encode()),
        "rpc_methods": sorted({e["rpc"] for e in events if "rpc" in e}),
        "denied_dispatch": denied, "credential_environment": "allowlist; no inherited auth/config",
    }
    report["checks"]["no_model_calls"] = {
        "status": "PASS" if events and not denied and report["no_model_calls"]["network_denied_by_os"] else "BLOCKED",
        "reason": "guarded metadata/config operations only; no inference authority",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true", help="root-only actual native CLI qualification")
    parser.add_argument("--codex-bin", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not args.native:
        parser.print_help()
        return 0
    if not args.codex_bin or not args.report:
        parser.error("--native requires explicit --codex-bin ABS and --report ABS")
    codex = args.codex_bin
    try:
        require(codex.is_absolute() and ".." not in codex.parts and codex.is_file()
                and os.access(codex, os.X_OK), "codex-bin must be an absolute executable file")
        codex = codex.resolve(strict=True)
        fd = reserve_report(args.report)
    except (Blocked, OSError) as exc:
        print(json.dumps({"qualification": "BLOCKED", "reason": str(exc)}))
        return 1
    report = {
        "schema_version": 1, "qualification": "BLOCKED", "baseline": BASELINE,
        "tier": "FULL", "execution_route": "direct-supervised",
        "provenance": "LOCAL_FIXTURE", "commands": [],
        "transport": "owned local Git fixture paths; no HTTPS fork provenance",
        "fixture_versions": {UPSTREAM: ["0.9.3", "0.9.4"], CANDIDATE: ["0.9.4", "0.9.5"]},
        "cli": {"path": str(codex)},
        "checks": {key: {"status": "BLOCKED", "reason": "not reached; prerequisite unavailable"} for key in REQUIRED},
        "later_gates": [{"status": "BLOCKED", "gate": "exact HTTPS fork provenance",
                         "source": "https://github.com/InsolventCapital/Codex-Orchestration.git"}],
        "scope": "disposable local qualification only; no live migration or release",
    }
    try:
        report["cli"]["sha256"] = digest(codex.read_bytes())
        report["harness_sha256"] = digest(Path(__file__).read_bytes())
        with tempfile.TemporaryDirectory(prefix="fork-migration-probe-") as raw:
            temp = Path(raw).resolve()
            try:
                qualify(temp, codex, report)
            except Exception as exc:
                report["blocker"] = failure_reason(exc)
            finally:
                audit_result(temp, report)
        report["owned_temp_cleaned"] = not temp.exists()
        if all(value["status"] == "PASS" for value in report["checks"].values()) and report["owned_temp_cleaned"]:
            report["qualification"] = "QUALIFIED"
    except Exception as exc:
        report["blocker"] = failure_reason(exc)
    finally:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps({"qualification": report["qualification"], "report": str(args.report),
                      "provenance": "LOCAL_FIXTURE"}))
    return 0 if report["qualification"] == "QUALIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
