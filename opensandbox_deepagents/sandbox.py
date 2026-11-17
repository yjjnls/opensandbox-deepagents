"""OpenSandbox backend for Deep Agents.

`OpenSandboxSandbox` implements the Deep Agents `BaseSandbox` contract on top of
the official OpenSandbox Python SDK (`SandboxSync`). All high-level file
operations (list, read, write, edit, glob, grep) are inherited from
`BaseSandbox` and run through `execute()`; this module only provides the
command-execution and bulk file-transfer primitives.
"""

from __future__ import annotations

import logging
import stat
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import OutputMessage, RunCommandOpts

if TYPE_CHECKING:
    from opensandbox import SandboxSync
    from opensandbox.models.execd import Execution
    from opensandbox.models.filesystem import EntryInfo

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TIMEOUT = 30 * 60
"""Default `execute()` timeout in seconds when the caller passes ``None``.

Matches the default used by the other Deep Agents sandbox integrations.
"""

_NO_TIMEOUT = 0
"""`timeout=0` sentinel: wait indefinitely (no server-side deadline)."""


def _combine_output(execution: Execution) -> str:
    """Merge stdout and stderr messages into one output string.

    Messages are ordered chronologically by their sandbox-side timestamp;
    ties keep emission order so line boundaries within each stream are never
    reordered relative to each other. Text is joined without inserting
    separators because each message already carries its own trailing newline
    (or is a fragment).
    """
    ordered: list[tuple[int, int, OutputMessage]] = []
    for index, messages in (
        (0, execution.logs.stdout),
        (1, execution.logs.stderr),
    ):
        for position, message in enumerate(messages):
            ordered.append((message.timestamp, index * 10_000_000 + position, message))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return "".join(message.text for _, _, message in ordered)


class OpenSandboxSandbox(BaseSandbox):
    """Deep Agents sandbox backend backed by an OpenSandbox sandbox.

    The backend wraps a caller-owned `SandboxSync` instance and implements the
    two primitives the `BaseSandbox` contract requires: `execute()` (shell
    commands) and bulk `upload_files()` / `download_files()`. Everything else
    (read, write, edit, ls, glob, grep, delete) is derived by `BaseSandbox`
    from those primitives and assumes a POSIX shell plus `python3` in the
    sandbox image.

    Example:
        ```python
        from opensandbox import SandboxSync

        from opensandbox_deepagents import OpenSandboxSandbox

        sandbox = SandboxSync.create("python:3.12-slim")
        backend = OpenSandboxSandbox(sandbox=sandbox)
        ```

    !!! note

        The integration uses the synchronous SDK deliberately: the Deep Agents
        sync contract maps to it directly, and Deep Agents already provides
        non-blocking async wrappers (`aexecute`, `aread`, ...) that run sync
        backends in worker threads.

    """

    def __init__(
        self,
        *,
        sandbox: SandboxSync,
        default_timeout: int = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        """Create a backend wrapping an existing OpenSandbox sandbox.

        Args:
            sandbox: A connected `SandboxSync` instance. Ownership stays with
                the caller: the backend does not destroy the sandbox on
                garbage collection; call `sandbox.destroy()` yourself (or go
                through the provider, which manages lifecycle).
            default_timeout: Command timeout in seconds used when `execute()`
                is called without an explicit `timeout`.

        """
        self._sandbox = sandbox
        self._default_timeout = default_timeout

    @property
    def id(self) -> str:
        """Return the OpenSandbox sandbox id."""
        return str(self._sandbox.id)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command inside the sandbox.

        Args:
            command: Full shell command string to execute.
            timeout: Maximum time in seconds to wait for the command. The
                deadline is enforced server-side by OpenSandbox, which
                terminates the process when it fires.

                - `None` uses `default_timeout`.
                - `0` waits indefinitely (no server-side deadline).

        Returns:
            `ExecuteResponse` with stdout and stderr merged in chronological
                order, the command exit code, and `truncated=False` (the
                OpenSandbox command transport does not clip output).

                When the server reports an execution error without an exit
                code (e.g. the command was killed at the deadline), the error
                text is appended to the output and the exit code is `1`;
                when no exit code is available and no error is reported
                (older servers), the exit code is `0`.

        """
        effective_timeout = self._default_timeout if timeout is None else timeout
        opts = RunCommandOpts(
            timeout=(
                None
                if effective_timeout == _NO_TIMEOUT
                else timedelta(seconds=effective_timeout)
            ),
        )
        execution = self._sandbox.commands.run(command, opts=opts)
        output = _combine_output(execution)
        exit_code = execution.exit_code
        if exit_code is None:
            if execution.error is not None:
                detail = f"[error] {execution.error.name}: {execution.error.value}"
                output = f"{output}\n{detail}" if output else detail
                exit_code = 1
            else:
                exit_code = 0
        return ExecuteResponse(output=output, exit_code=exit_code, truncated=False)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Upload files into the sandbox, preserving input order.

        Parent directories are created first (`mkdir -p` semantics) for all
        valid target paths. Each file is transferred individually so a failure
        on one file does not discard the others.

        Args:
            files: `(absolute_path, content)` pairs. Relative paths are
                rejected with `error="invalid_path"`.

        Returns:
            One `FileUploadResponse` per input path, in input order, with
                `error` set on per-file failures.

        """
        responses: list[FileUploadResponse] = []
        valid: list[tuple[str, bytes]] = []
        for path, content in files:
            if not path.startswith("/"):
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
            else:
                valid.append((path, content))
                responses.append(FileUploadResponse(path=path, error=None))

        if valid:
            self._ensure_parent_directories([path for path, _ in valid])
            for index, (path, content) in enumerate(valid):
                try:
                    self._sandbox.files.write_file(path, content)
                except Exception as exc:  # noqa: BLE001  # per-file partial success
                    responses[index] = FileUploadResponse(
                        path=path, error=_normalize_fs_error(exc)
                    )

        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download files from the sandbox, preserving input order.

        Args:
            paths: Absolute file paths. Relative paths are rejected with
                `error="invalid_path"`.

        Returns:
            One `FileDownloadResponse` per input path, in input order. Known
                failures are normalized to `file_not_found`,
                `permission_denied`, or `is_directory`; anything else keeps a
                short description of the underlying error.

        """
        responses: list[FileDownloadResponse] = []
        valid: list[str] = []
        for path in paths:
            if not path.startswith("/"):
                responses.append(
                    FileDownloadResponse(path=path, content=None, error="invalid_path")
                )
            else:
                valid.append(path)

        infos = self._get_file_infos(valid)
        for path in valid:
            info = infos.get(path) if infos else None
            try:
                content = self._sandbox.files.read_bytes(path)
            except Exception as exc:  # noqa: BLE001  # per-file partial success
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        content=None,
                        error=_map_download_error(exc, info),
                    )
                )
            else:
                responses.append(
                    FileDownloadResponse(path=path, content=content, error=None)
                )

        return responses

    def _ensure_parent_directories(self, paths: list[str]) -> None:
        """Create parent directories for all target paths in one command."""
        parents: list[str] = []
        seen: set[str] = set()
        for path in paths:
            parent = path.rsplit("/", 1)[0]
            if parent and parent not in seen:
                seen.add(parent)
                parents.append(parent)
        if not parents:
            return
        command = "mkdir -p " + " ".join(
            "'" + parent.replace("'", "'\\''") + "'" for parent in parents
        )
        result = self.execute(command, timeout=30)
        if result.exit_code != 0:
            logger.warning(
                "Parent directory creation failed for upload targets: %s",
                result.output.strip()[:200],
            )

    def _get_file_infos(self, paths: list[str]) -> dict[str, EntryInfo] | None:
        """Best-effort metadata lookup used to classify download errors."""
        if not paths:
            return None
        try:
            return self._sandbox.files.get_file_info(paths)
        except Exception:  # noqa: BLE001  # metadata is advisory only
            return None


def _is_directory_info(info: Any) -> bool:
    """Whether an `EntryInfo` record describes a directory."""
    if info is None:
        return False
    entry_type = getattr(info, "entry_type", None)
    if isinstance(entry_type, str) and "dir" in entry_type.lower():
        return True
    mode = getattr(info, "mode", None)
    if isinstance(mode, int):
        return stat.S_ISDIR(mode)
    return False


def _map_download_error(exc: Exception, info: Any) -> str:
    """Normalize a download failure into the shared error vocabulary."""
    if _is_directory_info(info):
        return "is_directory"
    if isinstance(exc, SandboxApiException):
        status = getattr(exc, "status_code", None)
        if status == 404:
            return "file_not_found"
        if status == 403:
            return "permission_denied"
        if status == 400:
            # Some deployments report directories and special files as 400s.
            return "invalid_path"
    return str(exc)[:200]


def _normalize_fs_error(exc: Exception) -> str:
    """Shorten an upload failure into a stable error string."""
    if isinstance(exc, SandboxApiException):
        status = getattr(exc, "status_code", None)
        if status == 403:
            return "permission_denied"
        if status == 404:
            return "file_not_found"
        if status == 400:
            return "invalid_path"
    return str(exc)[:200]
