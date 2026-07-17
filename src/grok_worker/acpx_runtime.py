"""Install and resolve the pinned, grok-worker-owned acpx Windows runtime."""

# ruff: noqa: E501 -- exact upstream JavaScript anchors must stay byte-for-byte readable.

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from shutil import which

from grok_worker.locks import FileLock

ACPX_VERSION = "0.12.0"
UPSTREAM_JAVASCRIPT_SHA256 = (
    "d92153086f058880623af7297a6867166068e27f9b2cc97a7b94c6a80e2b20da"
)
PATCH_LEVEL = "gw-win-2"
PATCH_MARKER = "/* grok-worker managed Windows runtime: gw-win-2 */"


class AcpxRuntimeError(RuntimeError):
    """The managed acpx runtime is missing, unsupported, or corrupted."""


@dataclass(frozen=True)
class RuntimeReceipt:
    runtime_id: str
    entry_path: str
    source_version: str
    source_javascript_sha256: str
    patched_javascript_sha256: str
    entry_sha256: str
    powershell_path: str
    powershell_version: str


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise AcpxRuntimeError(
            f"unsupported acpx JavaScript: expected one {label} anchor, found {count}"
        )
    return text.replace(old, new, 1)


def patch_acpx_javascript(text: str) -> str:
    """Apply the audited Windows terminal fixes to acpx 0.12.0 JavaScript."""
    if PATCH_MARKER in text:
        return text

    text = _replace_once(
        text,
        '''function buildTerminalSpawnCommand(command, args) {
\treturn {
\t\tcommand,
\t\targs: args ?? [],
\t\tkillProcessGroup: false
\t};
}''',
        '''function buildTerminalSpawnCommand(command, args, platform = process.platform) {
\tconst normalizedArgs = [...args ?? []];
\tconst usePwsh = platform === "win32" && /(?:^|[\\\\/])powershell(?:\\.exe)?$/iu.test(command);
\tconst useCmd = platform === "win32" && /(?:^|[\\\\/])cmd(?:\\.exe)?$/iu.test(command);
\tconst normalizedCommand = usePwsh ? "pwsh.exe" : command;
\tif (useCmd && !normalizedArgs.some((arg) => /^\\/u$/iu.test(arg))) {
\t\tconst insertAt = normalizedArgs.findIndex((arg) => /^\\/d$/iu.test(arg)) + 1;
\t\tnormalizedArgs.splice(Math.max(0, insertAt), 0, "/u");
\t}
\tif (usePwsh || platform === "win32" && /(?:^|[\\\\/])pwsh(?:\\.exe)?$/iu.test(command)) {
\t\tconst utf8Prefix = "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);";
\t\tconst commandIndex = normalizedArgs.findIndex((arg) => /^-(?:command|c)$/iu.test(arg));
\t\tif (commandIndex >= 0 && commandIndex + 1 < normalizedArgs.length) {
\t\t\tif (!normalizedArgs[commandIndex + 1].startsWith(utf8Prefix)) normalizedArgs[commandIndex + 1] = `${utf8Prefix} ${normalizedArgs[commandIndex + 1]}`;
\t\t} else {
\t\t\tconst fileIndex = normalizedArgs.findIndex((arg) => /^-(?:file|f)$/iu.test(arg));
\t\t\tif (fileIndex >= 0 && fileIndex + 1 < normalizedArgs.length) {
\t\t\t\tconst fileArgs = normalizedArgs.slice(fileIndex + 1);
\t\t\t\tconst invokeFile = `${utf8Prefix} $path = $args[0]; $rest = if ($args.Count -gt 1) { $args[1..($args.Count - 1)] } else { @() }; & $path @rest`;
\t\t\t\tnormalizedArgs.splice(fileIndex, normalizedArgs.length - fileIndex, "-CommandWithArgs", invokeFile, ...fileArgs);
\t\t\t}
\t\t}
\t}
\treturn {
\t\tcommand: normalizedCommand,
\t\targs: normalizedArgs,
\t\tkillProcessGroup: false,
\t\tcleanupScope: platform === "win32" ? "windows-tree" : "process",
\t\toutputEncoding: useCmd ? "utf-16le" : void 0
\t};
}''',
        "direct spawn",
    )
    text = _replace_once(
        text,
        '''\tif (platform === "win32") return {
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
\t};''',
        '''\tif (platform === "win32") {
\t\tconst normalizedCommand = command.replace(/^\\s*powershell(?:\\.exe)?(?=\\s)/iu, "pwsh.exe");
\t\treturn {
\t\tcommand: "cmd.exe",
\t\targs: [
\t\t\t"/d",
\t\t\t"/u",
\t\t\t"/s",
\t\t\t"/c",
\t\t\tnormalizedCommand
\t\t],
\t\tkillProcessGroup: false,
\t\tcleanupScope: "windows-tree",
\t\toutputEncoding: "utf-16le",
\t\twindowsVerbatimArguments: true
\t};
\t}
\treturn {
\t\tcommand: "/bin/sh",
\t\targs: ["-c", command],
\t\tkillProcessGroup: true,
\t\tcleanupScope: "posix-group"
\t};''',
        "shell spawn",
    )
    text = _replace_once(
        text,
        '''\twhile (start < buffer.length && (buffer[start] & 192) === 128) start += 1;
\tif (start >= buffer.length) start = buffer.length - limit;
\treturn buffer.subarray(start);''',
        '''\twhile (start < buffer.length && (buffer[start] & 192) === 128) start += 1;
\tif (start >= buffer.length) return Buffer.alloc(0);
\treturn buffer.subarray(start);''',
        "UTF-8 tail boundary",
    )
    text = _replace_once(
        text,
        "\t\t\t\tkillProcessGroup: spawnCommand.killProcessGroup,\n\t\t\t\tdescendantPids:",
        "\t\t\t\tkillProcessGroup: spawnCommand.killProcessGroup,\n"
        "\t\t\t\tcleanupScope: spawnCommand.cleanupScope,\n\t\t\t\tdescendantPids:",
        "terminal cleanup scope",
    )
    text = _replace_once(
        text,
        '''\t\t\tconst appendOutput = (chunk) => {
\t\t\t\tconst bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
\t\t\t\tif (bytes.length === 0) return;
\t\t\t\tterminal.output = Buffer.concat([terminal.output, bytes]);
\t\t\t\tif (terminal.output.length > terminal.outputByteLimit) {
\t\t\t\t\tterminal.output = trimToUtf8Boundary(terminal.output, terminal.outputByteLimit);
\t\t\t\t\tterminal.truncated = true;
\t\t\t\t}
\t\t\t};
\t\t\tproc.stdout.on("data", appendOutput);
\t\t\tproc.stderr.on("data", appendOutput);''',
        '''\t\t\tconst stdoutDecoder = spawnCommand.outputEncoding ? new TextDecoder(spawnCommand.outputEncoding) : void 0;
\t\t\tconst stderrDecoder = spawnCommand.outputEncoding ? new TextDecoder(spawnCommand.outputEncoding) : void 0;
\t\t\tconst appendOutput = (chunk, decoder) => {
\t\t\t\tconst rawBytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
\t\t\t\tconst bytes = decoder ? Buffer.from(decoder.decode(rawBytes, { stream: true }), "utf8") : rawBytes;
\t\t\t\tif (bytes.length === 0) return;
\t\t\t\tterminal.output = Buffer.concat([terminal.output, bytes]);
\t\t\t\tif (terminal.output.length > terminal.outputByteLimit) {
\t\t\t\t\tterminal.output = trimToUtf8Boundary(terminal.output, terminal.outputByteLimit);
\t\t\t\t\tterminal.truncated = true;
\t\t\t\t}
\t\t\t};
\t\t\tproc.stdout.on("data", (chunk) => appendOutput(chunk, stdoutDecoder));
\t\t\tproc.stderr.on("data", (chunk) => appendOutput(chunk, stderrDecoder));''',
        "terminal output decoding",
    )

    old_signal = '''\tasync signalProcess(terminal, signal) {
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
\t}'''
    new_signal = '''\tasync signalProcess(terminal, signal) {
\t\tconst pid = terminal.process.pid;
\t\tif (terminal.cleanupScope === "windows-tree" && pid) {
\t\t\tawait this.signalWindowsProcessGroup(terminal, pid, signal);
\t\t\treturn;
\t\t}
\t\tif (terminal.cleanupScope === "posix-group" && pid) {
\t\t\tawait this.signalPosixProcessGroup(terminal, pid, signal);
\t\t\treturn;
\t\t}
\t\tterminal.process.kill(signal);
\t}'''
    text = _replace_once(text, old_signal, new_signal, "signal routing")

    old_windows_signal = '''\tasync signalWindowsProcessGroup(terminal, pid, signal) {
\t\tawait this.captureDescendantPids(terminal, pid);
\t\tif (this.isRunning(terminal)) {
\t\t\tawait killWindowsProcessTree(pid, signal);
\t\t\treturn;
\t\t}'''
    new_windows_signal = '''\tasync signalWindowsProcessGroup(terminal, pid, signal) {
\t\tif (this.isRunning(terminal)) {
\t\t\tawait killWindowsProcessTree(pid, signal);
\t\t\treturn;
\t\t}
\t\tawait this.captureDescendantPids(terminal, pid);'''
    text = _replace_once(
        text, old_windows_signal, new_windows_signal, "active Windows tree cleanup"
    )
    text = _replace_once(
        text,
        '''\tasync captureDescendantPids(terminal, pid) {
\t\tif (!this.isRunning(terminal)) await terminal.processGroupSnapshotPromise?.catch(() => {});
\t\tfor (const descendantPid of await listDescendantPids(pid)) terminal.descendantPids.add(descendantPid);
\t}''',
        '''\tasync captureDescendantPids(terminal, pid) {
\t\tif (!this.isRunning(terminal) && terminal.processGroupSnapshotPromise) {
\t\t\tawait terminal.processGroupSnapshotPromise?.catch(() => {});
\t\t\treturn;
\t\t}
\t\tfor (const descendantPid of await listDescendantPids(pid)) terminal.descendantPids.add(descendantPid);
\t}''',
        "post-exit descendant snapshot",
    )
    text = _replace_once(
        text,
        '''\tif (spawnCommand.killProcessGroup) spawnOptions.detached = true;
\tconst proc = spawn(spawnCommand.command, spawnCommand.args, spawnOptions);''',
        '''\tif (spawnCommand.cleanupScope === "posix-group") spawnOptions.detached = true;
\tif (spawnCommand.windowsVerbatimArguments) spawnOptions.windowsVerbatimArguments = true;
\tconst proc = spawn(spawnCommand.command, spawnCommand.args, spawnOptions);''',
        "spawn options",
    )
    text = _replace_once(
        text,
        '''async function runProcessListCommand() {
\tif (process.platform === "win32") return await runWindowsProcessListCommand();''',
        '''let windowsProcessListActive;
let windowsProcessListPending;
function startWindowsProcessListBatch() {
\tconst batch = { started: false, promise: void 0 };
\tbatch.promise = new Promise((resolve) => queueMicrotask(resolve)).then(() => {
\t\tbatch.started = true;
\t\treturn runWindowsProcessListCommand();
\t}).finally(() => {
\t\tif (windowsProcessListActive === batch) windowsProcessListActive = void 0;
\t\tconst pending = windowsProcessListPending;
\t\twindowsProcessListPending = void 0;
\t\tif (pending) startWindowsProcessListBatch().then(pending.resolve, pending.reject);
\t});
\twindowsProcessListActive = batch;
\treturn batch.promise;
}
function runBatchedWindowsProcessListCommand() {
\tif (!windowsProcessListActive) return startWindowsProcessListBatch();
\tif (!windowsProcessListActive.started) return windowsProcessListActive.promise;
\tif (!windowsProcessListPending) {
\t\tlet resolve;
\t\tlet reject;
\t\tconst promise = new Promise((onResolve, onReject) => {
\t\t\tresolve = onResolve;
\t\t\treject = onReject;
\t\t});
\t\twindowsProcessListPending = { promise, resolve, reject };
\t}
\treturn windowsProcessListPending.promise;
}
async function runProcessListCommand() {
\tif (process.platform === "win32") return await runBatchedWindowsProcessListCommand();''',
        "Windows process-list batching",
    )
    text = _replace_once(
        text,
        '''async function rememberProcessGroupPids(terminal) {
\tconst processGroupId = terminal.process.pid;
\tif (!terminal.killProcessGroup || !processGroupId) return;
\tif (process.platform === "win32") {
\t\tfor (const pid of await listDescendantPids(processGroupId)) terminal.descendantPids.add(pid);
\t\treturn;
\t}''',
        '''async function rememberProcessGroupPids(terminal) {
\tconst processGroupId = terminal.process.pid;
\tif (terminal.cleanupScope === "process" || !processGroupId) return;
\tif (terminal.cleanupScope === "windows-tree") {
\t\tfor (const pid of await listDescendantPids(processGroupId)) terminal.descendantPids.add(pid);
\t\treturn;
\t}''',
        "exit snapshot scope",
    )
    text = _replace_once(
        text,
        '''async function runWindowsProcessListCommand() {
\treturn await new Promise((resolve, reject) => {
\t\tconst child = spawn("powershell.exe", [''',
        '''async function runWindowsProcessListCommand() {
\treturn await new Promise((resolve, reject) => {
\t\tconst child = spawn("pwsh.exe", [''',
        "PowerShell 7 process-list provider",
    )

    # Keep compatibility with code paths that still use the old boolean as a quick guard.
    text = text.replace(
        "if (!this.isRunning(terminal) && !terminal.killProcessGroup) return;",
        'if (!this.isRunning(terminal) && terminal.cleanupScope === "process") return;',
        1,
    ).replace(
        "if (await this.waitForCleanupAfterSignal(terminal) && !terminal.killProcessGroup) return;",
        'if (await this.waitForCleanupAfterSignal(terminal) && terminal.cleanupScope === "process") return;',
        1,
    )
    return f"{PATCH_MARKER}\n{text}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pwsh_info() -> tuple[str, str]:
    executable = which("pwsh")
    if not executable:
        raise AcpxRuntimeError("PowerShell 7 (pwsh) is required for the Windows acpx runtime")
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        startupinfo=startupinfo,
    )
    version = result.stdout.strip()
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise AcpxRuntimeError(f"cannot determine PowerShell 7 version: {version!r}") from exc
    if result.returncode != 0 or major < 7:
        raise AcpxRuntimeError(f"PowerShell 7 or newer is required, got {version!r}")
    return str(Path(executable).resolve()), version


def default_runtime_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local_app_data:
        return Path(local_app_data) / "grok-worker" / "runtimes" / "acpx"
    return Path.home() / ".local" / "share" / "grok-worker" / "runtimes" / "acpx"


def discover_acpx_package() -> Path:
    launcher = which("acpx")
    if not launcher:
        raise AcpxRuntimeError("cannot locate acpx 0.12.0; pass --source-package")
    resolved = Path(launcher).resolve()
    if resolved.suffix.lower() in {".cmd", ".bat"}:
        package = resolved.parent / "node_modules" / "acpx"
    elif resolved.name == "cli.js" and resolved.parent.name == "dist":
        package = resolved.parent.parent
    else:
        package = resolved.parent / "node_modules" / "acpx"
    if not package.is_dir():
        raise AcpxRuntimeError(f"cannot locate acpx package beside {resolved}")
    return package.resolve()


def _validate_source(package: Path, *, allow_unpinned_fixture: bool) -> tuple[Path, str]:
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcpxRuntimeError(f"invalid acpx package metadata: {exc}") from exc
    if metadata.get("name") != "acpx" or metadata.get("version") != ACPX_VERSION:
        raise AcpxRuntimeError(f"expected acpx {ACPX_VERSION}, got {metadata!r}")
    candidates = sorted((package / "dist").glob("live-checkpoint-*.js"))
    if len(candidates) != 1:
        raise AcpxRuntimeError(f"expected one acpx live-checkpoint chunk, found {len(candidates)}")
    source_hash = _sha256(candidates[0])
    if not allow_unpinned_fixture and source_hash != UPSTREAM_JAVASCRIPT_SHA256:
        raise AcpxRuntimeError(
            f"unsupported acpx JavaScript sha256 {source_hash}; expected pinned 0.12.0 build"
        )
    return candidates[0], source_hash


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def install_managed_runtime(
    *,
    source_package: Path | None = None,
    runtime_root: Path | None = None,
    allow_unpinned_fixture: bool = False,
) -> RuntimeReceipt:
    """Copy, patch, and atomically publish an immutable acpx runtime."""
    package = (source_package or discover_acpx_package()).resolve()
    root = (runtime_root or default_runtime_root()).resolve()
    source_js, source_hash = _validate_source(
        package, allow_unpinned_fixture=allow_unpinned_fixture
    )
    for item in package.rglob("*"):
        if item.is_symlink():
            raise AcpxRuntimeError(f"refusing symlink in acpx package: {item}")
    patched = patch_acpx_javascript(source_js.read_text(encoding="utf-8"))
    patched_hash = hashlib.sha256(patched.encode("utf-8")).hexdigest()
    powershell_path, powershell_version = _pwsh_info()
    runtime_id = f"acpx-{ACPX_VERSION}-gw-win-{patched_hash[:12]}"
    target = root / runtime_id
    package_target = target / "package"
    entry = package_target / "dist" / "cli.js"
    manifest_path = target / "manifest.json"

    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / ".install.lock"):
        if not target.exists():
            staging = root / f".{runtime_id}.{os.getpid()}.{time.time_ns()}.tmp"
            try:
                shutil.copytree(package, staging / "package")
                copied_js = staging / "package" / "dist" / source_js.name
                copied_js.write_text(patched, encoding="utf-8", newline="\n")
                staged_entry = staging / "package" / "dist" / "cli.js"
                receipt = RuntimeReceipt(
                    runtime_id=runtime_id,
                    entry_path=str(entry.resolve()),
                    source_version=ACPX_VERSION,
                    source_javascript_sha256=source_hash,
                    patched_javascript_sha256=_sha256(copied_js),
                    entry_sha256=_sha256(staged_entry),
                    powershell_path=powershell_path,
                    powershell_version=powershell_version,
                )
                _atomic_write_json(staging / "manifest.json", asdict(receipt))
                staging.rename(target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

        receipt = _read_receipt(manifest_path)
        _verify_receipt(root, receipt)
        _atomic_write_json(root / "current.json", {"runtime_id": runtime_id})
        return receipt


def _read_receipt(path: Path) -> RuntimeReceipt:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return RuntimeReceipt(**raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise AcpxRuntimeError(f"invalid managed acpx manifest {path}: {exc}") from exc


def _verify_receipt(root: Path, receipt: RuntimeReceipt) -> None:
    entry = Path(receipt.entry_path).resolve()
    expected_parent = (root / receipt.runtime_id).resolve()
    if expected_parent not in entry.parents or not entry.is_file():
        raise AcpxRuntimeError("managed acpx entry escapes its immutable runtime")
    if _sha256(entry) != receipt.entry_sha256:
        raise AcpxRuntimeError("managed acpx entry integrity check failed")
    patched_files = sorted((entry.parent).glob("live-checkpoint-*.js"))
    if len(patched_files) != 1 or _sha256(patched_files[0]) != receipt.patched_javascript_sha256:
        raise AcpxRuntimeError("managed acpx JavaScript integrity check failed")
    current_pwsh = which("pwsh")
    if not current_pwsh or Path(current_pwsh).resolve() != Path(receipt.powershell_path).resolve():
        raise AcpxRuntimeError("managed acpx PowerShell 7 dependency changed; reinstall the runtime")


def resolve_managed_acpx_command(
    *, runtime_root: Path | None = None, node_bin: str | None = None
) -> list[str]:
    root = (runtime_root or default_runtime_root()).resolve()
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8"))
        runtime_id = str(current["runtime_id"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise AcpxRuntimeError(
            "managed acpx runtime is not installed; run grok-worker acpx-runtime-install"
        ) from exc
    manifest = root / runtime_id / "manifest.json"
    receipt = _read_receipt(manifest)
    if receipt.runtime_id != runtime_id:
        raise AcpxRuntimeError("managed acpx current pointer and manifest disagree")
    _verify_receipt(root, receipt)
    node = node_bin or which("node")
    if not node:
        raise AcpxRuntimeError("cannot locate node for managed acpx runtime")
    return [node, receipt.entry_path]


def managed_runtime_identity(runtime_root: Path | None = None) -> str:
    """Return the verified immutable runtime id for session pinning."""
    root = (runtime_root or default_runtime_root()).resolve()
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8"))
        runtime_id = str(current["runtime_id"])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise AcpxRuntimeError("managed acpx runtime is not installed") from exc
    receipt = _read_receipt(root / runtime_id / "manifest.json")
    _verify_receipt(root, receipt)
    return receipt.runtime_id
