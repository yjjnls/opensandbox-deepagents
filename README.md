# opensandbox-deepagents

[OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) sandbox backend and provider for
[Deep Agents](https://github.com/langchain-ai/deepagents) and [Deep Agents Code](https://github.com/langchain-ai/deepagents) (`dcode`).

Run Deep Agents file and shell tooling inside a self-hosted OpenSandbox deployment: the
`OpenSandboxSandbox` backend implements the `BaseSandbox` contract on top of the official
OpenSandbox Python SDK, and `OpenSandboxProvider` plugs into `dcode` through its
`deepagents_code.sandbox_providers` entry-point group — no upstream changes required.

## Install

```bash
pip install opensandbox-deepagents

# with the dcode provider integration (deepagents-code requires Python >= 3.14):
pip install "opensandbox-deepagents[dcode]"
```

Environment configuration follows the OpenSandbox SDK: `OPEN_SANDBOX_API_KEY` /
`OPEN_SANDBOX_DOMAIN` (or a `ConnectionConfigSync` passed to `create`/`connect`),
same as any `opensandbox` SDK program.

## Use with the Deep Agents SDK

```python
from opensandbox import SandboxSync

from opensandbox_deepagents import OpenSandboxSandbox

# Create (or connect with SandboxSync.connect(sandbox_id=...))
sandbox = SandboxSync.create("python:3.12-slim", timeout=None)

backend = OpenSandboxSandbox(sandbox=sandbox)

# use as any other Deep Agents backend, e.g.
#   agent = create_deep_agent(model=model, backend=backend)
result = backend.execute("python3 -c 'print(1 + 1)'")
print(result.output, result.exit_code)

# destroy when done (caller-owned lifecycle)
sandbox.destroy()
```

## Use with Deep Agents Code

After `pip install "opensandbox-deepagents[dcode]"` the provider is discovered through the
`deepagents_code.sandbox_providers` entry-point group:

```bash
dcode --sandbox opensandbox

# connect to an existing sandbox
dcode --sandbox opensandbox --sandbox-id <id>

# restore a named snapshot (built from the image on first use)
dcode --sandbox opensandbox --snapshot-name my-snapshot
```

Config-level parameters (`~/.deepagents/config.toml`) are forwarded to the provider:

```toml
[sandboxes.providers.opensandbox.params]
image = "python:3.12-slim"
lifetime_seconds = 3600   # 0 = live until explicitly deleted
```

## Interface mapping

| Deep Agents contract | OpenSandbox SDK |
| --- | --- |
| `execute(command, timeout)` | `SandboxSync.commands.run(command, opts=RunCommandOpts(timeout=...))` |
| `upload_files([(path, bytes)])` | parent `mkdir -p` via `commands.run`, then per-file `files.write_file(path, data)` |
| `download_files([paths])` | `files.get_file_info(paths)` for error classification, then per-file `files.read_bytes(path)` |
| `id` | `SandboxSync.id` |
| timeout `None` | provider default (30 min per command) |
| timeout `0` | no deadline (`RunCommandOpts.timeout=None`) |
| stdout/stderr merge | log messages interleaved chronologically by sandbox timestamp, line boundaries preserved |
| `provider.get_or_create(sandbox_id=...)` | `SandboxSync.connect(sandbox_id)` |
| `provider.get_or_create(image=...)` | `SandboxSync.create(image, timeout=...)` |
| `provider.get_or_create(snapshot=name)` | `SandboxManagerSync.list_snapshots(SnapshotFilter(name=...))` → reuse, else build: `SandboxSync.create(image)` + `create_snapshot(name)` + `destroy()`, then restore via `SandboxSync.create(snapshot_id=...)` |
| `provider.delete(sandbox_id)` | `SandboxManagerSync.kill_sandbox(sandbox_id)` |

All other file operations (`read`, `write`, `edit`, `ls`, `glob`, `grep`, `delete`) are
derived by `BaseSandbox` from `execute()` and require a POSIX shell plus `python3` in the
sandbox image (`python:3.12-slim` works as-is).

## Tests

```bash
uv sync --group test
uv run pytest
```

Unit tests run fully offline against fakes. To run the official Deep Agents conformance
suite against a live deployment:

```python
# tests/integration_tests/test_live.py
from langchain_tests.integration_tests import SandboxIntegrationTests
from opensandbox import SandboxSync

from opensandbox_deepagents import OpenSandboxSandbox


class TestOpenSandboxStandard(SandboxIntegrationTests):
    @pytest.fixture(scope="class")
    def sandbox(self):
        sandbox = SandboxSync.create("python:3.12-slim", timeout=None)
        try:
            yield OpenSandboxSandbox(sandbox=sandbox)
        finally:
            sandbox.destroy()
```

## Status

- Backend: command execution, upload/download with per-file partial success, parent
  directory creation, and the shared error vocabulary (`invalid_path`, `file_not_found`,
  `permission_denied`, `is_directory`).
- Provider: connect by id, create from image, snapshot-name resolution with build-on-miss,
  delete.
- Snapshot states: the lifecycle state vocabulary is deployment-specific; the provider
  treats states containing `fail`/`error`/`delet`/`abort` as terminal and defers restore
  readiness to `SandboxSync.create`'s own health checks.

## License

MIT. Depends on `deepagents` (MIT) and the OpenSandbox Python SDK (Apache-2.0).
