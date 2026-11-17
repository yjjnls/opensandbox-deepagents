"""Unit tests for `OpenSandboxSandbox` (no network, SDK surface faked)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from deepagents.backends.protocol import ExecuteResponse
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import (
    Execution,
    ExecutionError,
    ExecutionLogs,
    OutputMessage,
)

from opensandbox_deepagents import OpenSandboxSandbox


@dataclass
class FakeCommands:
    execution: Execution
    calls: list[dict] = field(default_factory=list)

    def run(self, command, *, opts=None, handlers=None):  # noqa: ANN001, ARG002
        self.calls.append({"command": command, "opts": opts})
        return self.execution


@dataclass
class FakeFiles:
    contents: dict[str, bytes] = field(default_factory=dict)
    infos: dict = field(default_factory=dict)
    write_calls: list[str] = field(default_factory=list)
    write_errors: dict[str, Exception] = field(default_factory=dict)

    def write_file(self, path, data, **kwargs):  # noqa: ANN001, ARG002
        self.write_calls.append(path)
        error = self.write_errors.get(path)
        if error is not None:
            raise error
        self.contents[path] = data

    def read_bytes(self, path, **kwargs):  # noqa: ANN001, ARG002
        error = self.read_errors.get(path)
        if error is not None:
            raise error
        if path not in self.contents:
            raise SandboxApiException(
                "not found", status_code=404, error=None, request_id=None
            )
        return self.contents[path]

    read_errors: dict[str, Exception] = field(default_factory=dict)

    def get_file_info(self, paths):  # noqa: ANN001
        return {path: self.infos[path] for path in paths if path in self.infos}


@dataclass
class FakeSandbox:
    id: str = "sbx-123"
    commands: FakeCommands = None  # type: ignore[assignment]
    files: FakeFiles = None  # type: ignore[assignment]


def _execution(
    *,
    stdout: list[str] | None = None,
    stderr: list[str] | None = None,
    exit_code: int | None = 0,
    error: ExecutionError | None = None,
    ts_base: int = 1_000,
) -> Execution:
    logs = ExecutionLogs(
        stdout=[
            OutputMessage(text=text, timestamp=ts_base + index * 10)
            for index, text in enumerate(stdout or [])
        ],
        stderr=[
            OutputMessage(text=text, timestamp=ts_base + 5 + index * 10)
            for index, text in enumerate(stderr or [])
        ],
    )
    return Execution(exit_code=exit_code, logs=logs, error=error)


def _backend(
    commands: FakeCommands,
    files: FakeFiles | None = None,
) -> OpenSandboxSandbox:
    return OpenSandboxSandbox(
        sandbox=FakeSandbox(commands=commands, files=files or FakeFiles()),
    )


class TestExecute:
    def test_happy_path_merges_streams_chronologically(self) -> None:
        commands = FakeCommands(
            _execution(stdout=["out1\n", "out2\n"], stderr=["err1\n"])
        )
        result = _backend(commands).execute("echo hi")
        assert isinstance(result, ExecuteResponse)
        assert result.exit_code == 0
        assert result.truncated is False
        assert result.output == "out1\nerr1\nout2\n"

    def test_none_timeout_uses_default(self) -> None:
        commands = FakeCommands(_execution())
        _backend(commands).execute("true")
        assert commands.calls[0]["opts"].timeout == timedelta(seconds=30 * 60)

    def test_zero_timeout_means_no_deadline(self) -> None:
        commands = FakeCommands(_execution())
        _backend(commands).execute("true", timeout=0)
        assert commands.calls[0]["opts"].timeout is None

    def test_explicit_timeout_maps_to_timedelta(self) -> None:
        commands = FakeCommands(_execution())
        _backend(commands).execute("true", timeout=45)
        assert commands.calls[0]["opts"].timeout == timedelta(seconds=45)

    def test_error_without_exit_code_yields_exit_one(self) -> None:
        commands = FakeCommands(
            _execution(
                stdout=["partial\n"],
                exit_code=None,
                error=ExecutionError(
                    name="CommandTimeout", value="killed at deadline", timestamp=1
                ),
            )
        )
        result = _backend(commands).execute("sleep 999")
        assert result.exit_code == 1
        assert "partial" in result.output
        assert "CommandTimeout" in result.output
        assert "killed at deadline" in result.output

    def test_no_exit_code_and_no_error_yields_zero(self) -> None:
        commands = FakeCommands(_execution(exit_code=None))
        assert _backend(commands).execute("true").exit_code == 0


class TestUploadFiles:
    def test_relative_path_rejected(self) -> None:
        commands = FakeCommands(_execution())
        files = FakeFiles()
        responses = _backend(commands, files).upload_files([("rel/path.txt", b"x")])
        assert responses == [
            type(responses[0])(path="rel/path.txt", error="invalid_path")
        ]
        assert files.write_calls == []

    def test_parents_created_before_writes(self) -> None:
        commands = FakeCommands(_execution())
        files = FakeFiles()
        responses = _backend(commands, files).upload_files([("/a/b/c.txt", b"hi")])
        assert responses[0].error is None
        mkdir_calls = [
            call["command"] for call in commands.calls if "mkdir -p" in call["command"]
        ]
        assert mkdir_calls and "/a/b" in mkdir_calls[0]
        assert files.contents["/a/b/c.txt"] == b"hi"

    def test_partial_success_preserves_order(self) -> None:
        commands = FakeCommands(_execution())
        files = FakeFiles(
            write_errors={
                "/bad.txt": SandboxApiException(
                    "denied", status_code=403, error=None, request_id=None
                )
            }
        )
        responses = _backend(commands, files).upload_files(
            [("/ok1.txt", b"1"), ("/bad.txt", b"2"), ("/ok2.txt", b"3")]
        )
        assert [r.path for r in responses] == ["/ok1.txt", "/bad.txt", "/ok2.txt"]
        assert responses[0].error is None
        assert responses[1].error == "permission_denied"
        assert responses[2].error is None
        assert files.contents["/ok1.txt"] == b"1"
        assert files.contents["/ok2.txt"] == b"3"


class TestDownloadFiles:
    def test_relative_path_rejected(self) -> None:
        backend = _backend(FakeCommands(_execution()), FakeFiles())
        responses = backend.download_files(["rel.txt"])
        assert responses == [
            type(responses[0])(path="rel.txt", content=None, error="invalid_path")
        ]

    def test_missing_file_maps_not_found(self) -> None:
        backend = _backend(FakeCommands(_execution()), FakeFiles())
        responses = backend.download_files(["/missing.txt"])
        assert responses[0].content is None
        assert responses[0].error == "file_not_found"

    def test_directory_maps_is_directory(self) -> None:
        files = FakeFiles()

        class Info:
            entry_type = "directory"

        files.infos["/dir"] = Info()
        backend = _backend(FakeCommands(_execution()), files)
        responses = backend.download_files(["/dir"])
        assert responses[0].error == "is_directory"

    def test_permission_denied_maps(self) -> None:
        files = FakeFiles(
            read_errors={
                "/secret.txt": SandboxApiException(
                    "forbidden", status_code=403, error=None, request_id=None
                )
            }
        )
        backend = _backend(FakeCommands(_execution()), files)
        responses = backend.download_files(["/secret.txt"])
        assert responses[0].error == "permission_denied"

    def test_success_roundtrip_preserves_order(self) -> None:
        files = FakeFiles(contents={"/a.txt": b"A", "/b.txt": b"B"})
        backend = _backend(FakeCommands(_execution()), files)
        responses = backend.download_files(["/b.txt", "/a.txt"])
        assert [(r.path, r.content, r.error) for r in responses] == [
            ("/b.txt", b"B", None),
            ("/a.txt", b"A", None),
        ]


class TestId:
    def test_id_returns_sandbox_id(self) -> None:
        assert _backend(FakeCommands(_execution())).id == "sbx-123"
