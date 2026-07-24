from __future__ import annotations

# ruff: noqa: E501 -- fixture mirrors exact upstream JavaScript anchors.
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from grok_worker import acpx_runtime
from grok_worker.acpx_runtime import (
    AcpxRuntimeError,
    install_managed_runtime,
    patch_acpx_javascript,
    resolve_managed_acpx_command,
)


@pytest.fixture(autouse=True)
def _stable_windows_runtime_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep source-transform tests independent of the host OS toolchain."""
    original_which = acpx_runtime.which
    monkeypatch.setattr(
        acpx_runtime,
        "which",
        lambda name: "/test/pwsh" if name == "pwsh" else original_which(name),
    )
    monkeypatch.setattr(acpx_runtime, "_pwsh_info", lambda: ("/test/pwsh", "7.5.0"))
    monkeypatch.setattr(acpx_runtime, "_windows_cmd_encoding", lambda: (65001, "utf-8"))

UPSTREAM_FIXTURE = '''
function buildTerminalSpawnCommand(command, args) {
\treturn {
\t\tcommand,
\t\targs: args ?? [],
\t\tkillProcessGroup: false
\t};
}
function buildTerminalShellSpawnCommand(command, platform = process.platform) {
\tif (platform === "win32") return {
\t\tcommand: "cmd.exe",
\t\targs: [
\t\t\t"/d",
\t\t\t"/s",
\t\t\t"/c",
\t\t\tcommand
\t\t],
\t\tkillProcessGroup: true
\t};
\treturn {
\t\tcommand: "/bin/sh",
\t\targs: ["-c", command],
\t\tkillProcessGroup: true
\t};
}
function trimToUtf8Boundary(buffer, limit) {
\tif (limit <= 0) return Buffer.alloc(0);
\tif (buffer.length <= limit) return buffer;
\tlet start = buffer.length - limit;
\twhile (start < buffer.length && (buffer[start] & 192) === 128) start += 1;
\tif (start >= buffer.length) start = buffer.length - limit;
\treturn buffer.subarray(start);
}
\t\t\tconst terminal = {
\t\t\t\tprocess: proc,
\t\t\t\tkillProcessGroup: spawnCommand.killProcessGroup,
\t\t\t\tdescendantPids: /* @__PURE__ */ new Set(),
\t\t\t};
\t\t\tconst appendOutput = (chunk) => {
\t\t\t\tconst bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
\t\t\t\tif (bytes.length === 0) return;
\t\t\t\tterminal.output = Buffer.concat([terminal.output, bytes]);
\t\t\t\tif (terminal.output.length > terminal.outputByteLimit) {
\t\t\t\t\tterminal.output = trimToUtf8Boundary(terminal.output, terminal.outputByteLimit);
\t\t\t\t\tterminal.truncated = true;
\t\t\t\t}
\t\t\t};
\t\t\tproc.stdout.on("data", appendOutput);
\t\t\tproc.stderr.on("data", appendOutput);
\t\t\tproc.once("exit", (exitCode, signal) => {
\t\t\t\tterminal.exitCode = exitCode;
\tasync signalProcess(terminal, signal) {
\t\tconst pid = terminal.process.pid;
\t\tif (terminal.killProcessGroup && pid && process.platform === "win32") {
\t\t\tawait this.signalWindowsProcessGroup(terminal, pid, signal);
\t\t\treturn;
\t\t}
\t\tif (terminal.killProcessGroup && pid) {
\t\t\tawait this.signalPosixProcessGroup(terminal, pid, signal);
\t\t\treturn;
\t\t}
\t\tterminal.process.kill(signal);
\t}
\tasync signalWindowsProcessGroup(terminal, pid, signal) {
\t\tawait this.captureDescendantPids(terminal, pid);
\t\tif (this.isRunning(terminal)) {
\t\t\tawait killWindowsProcessTree(pid, signal);
\t\t\treturn;
\t\t}
\t}
\tasync captureDescendantPids(terminal, pid) {
\t\tif (!this.isRunning(terminal)) await terminal.processGroupSnapshotPromise?.catch(() => {});
\t\tfor (const descendantPid of await listDescendantPids(pid)) terminal.descendantPids.add(descendantPid);
\t}
async function spawnAndWait(spawnCommand, params, defaultCwd) {
\tconst spawnOptions = buildTerminalSpawnOptions(spawnCommand.command, params.cwd ?? defaultCwd, params.env);
\tif (spawnCommand.killProcessGroup) spawnOptions.detached = true;
\tconst proc = spawn(spawnCommand.command, spawnCommand.args, spawnOptions);
}
async function runProcessListCommand() {
\tif (process.platform === "win32") return await runWindowsProcessListCommand();
\treturn "posix";
}
async function rememberProcessGroupPids(terminal) {
\tconst processGroupId = terminal.process.pid;
\tif (!terminal.killProcessGroup || !processGroupId) return;
\tif (process.platform === "win32") {
\t\tfor (const pid of await listDescendantPids(processGroupId)) terminal.descendantPids.add(pid);
\t\treturn;
\t}
}
async function runWindowsProcessListCommand() {
\treturn await new Promise((resolve, reject) => {
\t\tconst child = spawn("powershell.exe", [
\t\t\t"-NoProfile",
\t\t\t"-NonInteractive",
\t\t\t"-Command",
\t\t\t["Get-CimInstance Win32_Process |", "ForEach-Object { $_.ProcessId }"].join(" ")
\t\t]);
\t});
}
'''


def _fake_package(root: Path, source: str = UPSTREAM_FIXTURE) -> Path:
    package = root / "acpx"
    dist = package / "dist"
    dist.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "acpx", "version": "0.12.0"}), encoding="utf-8"
    )
    (dist / "cli.js").write_text("import './live-checkpoint-test.js';\n", encoding="utf-8")
    (dist / "live-checkpoint-test.js").write_text(source, encoding="utf-8")
    return package


def test_patch_separates_windows_tree_cleanup_and_posix_detached() -> None:
    patched = patch_acpx_javascript(UPSTREAM_FIXTURE)

    assert "cleanupScope: platform === \"win32\" ? \"windows-tree\" : \"process\"" in patched
    assert "cleanupScope: \"windows-tree\"" in patched
    assert "cleanupScope: \"posix-group\"" in patched
    assert 'spawnCommand.cleanupScope === "posix-group"' in patched
    assert 'terminal.cleanupScope === "windows-tree"' in patched


def test_patch_enables_utf8_and_windows_verbatim_shell_arguments() -> None:
    patched = patch_acpx_javascript(UPSTREAM_FIXTURE)

    assert "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);" in patched
    assert 'const WINDOWS_CMD_OUTPUT_ENCODING = "gbk"' in patched
    assert "outputEncoding: WINDOWS_CMD_OUTPUT_ENCODING" in patched
    assert "createAdaptiveOutputDecoder(spawnCommand.outputEncoding)" in patched
    assert 'new TextDecoder("utf-8")' in patched
    assert 'new TextDecoder("utf-8", { fatal: true })' in patched
    assert "new TextDecoder(fallbackEncoding)" in patched
    assert 'mode = isUtf8(pending) ? "utf8" : "fallback"' in patched
    assert "if (!pending.includes(10)) return Buffer.alloc(0);" in patched
    assert 'appendOutput(chunk, stdoutDecoder)' in patched
    assert 'appendOutput(chunk, stderrDecoder)' in patched
    assert "appendBytes(stdoutDecoder.flush())" in patched
    assert "appendBytes(stderrDecoder.flush())" in patched
    assert "windowsVerbatimArguments: true" in patched
    assert "spawnOptions.windowsVerbatimArguments = true" in patched
    assert "if (start >= buffer.length) return Buffer.alloc(0);" in patched
    assert 'const normalizedCommand = usePwsh ? "pwsh.exe" : command' in patched
    assert "const useCmd" in patched
    assert "windowsVerbatimArguments: useCmd" in patched
    assert 'const child = spawn("pwsh.exe"' in patched
    assert '"-CommandWithArgs", invokeFile' in patched
    assert 'command.replace(/^\\s*powershell' in patched
    assert "const looksLikePowerShell" in patched
    assert '/[\\r\\n]/u.test(normalizedCommand)' in patched
    assert 'command: "pwsh.exe"' in patched
    assert 'args: ["-NoProfile", "-NonInteractive", "-Command"' in patched


def test_patch_batches_cim_and_avoids_duplicate_post_exit_query() -> None:
    patched = patch_acpx_javascript(UPSTREAM_FIXTURE)

    assert "windowsProcessListActive.started" in patched
    assert "windowsProcessListPending" in patched
    assert "queueMicrotask" in patched
    assert "await terminal.processGroupSnapshotPromise?.catch(() => {});\n\t\t\treturn;" in patched
    active_tree_branch = patched.split("async signalWindowsProcessGroup", 1)[1]
    assert active_tree_branch.index("if (this.isRunning(terminal))") < active_tree_branch.index(
        "captureDescendantPids"
    )


def test_patch_is_idempotent_and_rejects_unknown_input() -> None:
    patched = patch_acpx_javascript(UPSTREAM_FIXTURE)
    assert patch_acpx_javascript(patched) == patched
    with pytest.raises(AcpxRuntimeError, match="unsupported acpx JavaScript"):
        patch_acpx_javascript("console.log('different');")


def test_install_is_immutable_and_does_not_modify_source(tmp_path: Path) -> None:
    package = _fake_package(tmp_path / "source")
    original = (package / "dist" / "live-checkpoint-test.js").read_bytes()
    runtime_root = tmp_path / "managed"

    receipt = install_managed_runtime(
        source_package=package,
        runtime_root=runtime_root,
        allow_unpinned_fixture=True,
    )

    assert (package / "dist" / "live-checkpoint-test.js").read_bytes() == original
    assert receipt.runtime_id.startswith("acpx-0.12.0-gw-win-")
    assert Path(receipt.entry_path).is_file()
    assert json.loads((runtime_root / "current.json").read_text(encoding="utf-8"))[
        "runtime_id"
    ] == receipt.runtime_id
    first_entry = receipt.entry_path
    second = install_managed_runtime(
        source_package=package,
        runtime_root=runtime_root,
        allow_unpinned_fixture=True,
    )
    assert second.entry_path == first_entry


def test_resolve_managed_runtime_fails_closed_on_tamper(tmp_path: Path) -> None:
    package = _fake_package(tmp_path / "source")
    runtime_root = tmp_path / "managed"
    receipt = install_managed_runtime(
        source_package=package,
        runtime_root=runtime_root,
        allow_unpinned_fixture=True,
    )
    command = resolve_managed_acpx_command(runtime_root=runtime_root, node_bin="node.exe")
    assert command == ["node.exe", receipt.entry_path]

    Path(receipt.entry_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(AcpxRuntimeError, match="integrity"):
        resolve_managed_acpx_command(runtime_root=runtime_root, node_bin="node.exe")


def test_parallel_installers_publish_one_verified_runtime(tmp_path: Path) -> None:
    package = _fake_package(tmp_path / "source")
    runtime_root = tmp_path / "managed"

    def install() -> str:
        return install_managed_runtime(
            source_package=package,
            runtime_root=runtime_root,
            allow_unpinned_fixture=True,
        ).runtime_id

    with ThreadPoolExecutor(max_workers=12) as pool:
        runtime_ids = list(pool.map(lambda _index: install(), range(24)))

    assert len(set(runtime_ids)) == 1
    assert len([path for path in runtime_root.iterdir() if path.name.startswith("acpx-")]) == 1
    assert not list(runtime_root.glob("*.tmp"))
    assert resolve_managed_acpx_command(runtime_root=runtime_root, node_bin="node.exe")
