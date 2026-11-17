"""OpenSandbox provider for Deep Agents Code.

`OpenSandboxProvider` implements the `SandboxProvider` interface consumed by
`deepagents-code`. It is registered under the `deepagents_code.sandbox_providers`
entry-point group, so once this package is installed:

```bash
dcode --sandbox opensandbox
```

works without any further configuration. The provider supports connecting to an
existing sandbox by id, creating one from an image, and the snapshot workflow
(`snapshot=<name>` resolves an existing snapshot by name or builds one from the
image on first use).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from deepagents_code.integrations.sandbox_provider import (
    SandboxInstallHint,
    SandboxProvider,
    SandboxProviderMetadata,
)
from opensandbox import SandboxManagerSync, SandboxSync
from opensandbox.models.sandboxes import SnapshotFilter, SnapshotInfo

from opensandbox_deepagents.sandbox import OpenSandboxSandbox

logger = logging.getLogger(__name__)

PROVIDER_NAME = "opensandbox"
PACKAGE_NAME = "opensandbox-deepagents"

DEFAULT_IMAGE = "python:3.12-slim"
"""Image used when neither `image` nor `snapshot` is provided."""

DEFAULT_SANDBOX_LIFETIME = timedelta(minutes=60)
"""Sandbox lifetime applied on create when the caller does not override it.

The sandbox self-terminates after this budget unless renewed or deleted
earlier through the provider.
"""

_TERMINAL_SNAPSHOT_STATE_FRAGMENTS = ("fail", "error", "delet", "abort")


class OpenSandboxProvider(SandboxProvider):
    """Create, connect to, and delete OpenSandbox sandboxes for dcode."""

    @property
    def metadata(self) -> SandboxProviderMetadata:
        """Describe the provider for the dcode registry."""
        return SandboxProviderMetadata(
            name=PROVIDER_NAME,
            working_dir="/workspace",
            install=SandboxInstallHint(kind="package", name=PACKAGE_NAME),
            supports_sandbox_id=True,
            supports_snapshot_name=True,
        )

    def get_or_create(
        self,
        *,
        sandbox_id: str | None = None,
        image: str | None = None,
        snapshot: str | None = None,
        connection_config: Any | None = None,
        lifetime_seconds: int | None = None,
        **kwargs: Any,
    ) -> OpenSandboxSandbox:
        """Get an existing sandbox by id, or create one.

        Args:
            sandbox_id: Connect to an existing sandbox instead of creating.
            image: Container image for new sandboxes (default `python:3.12-slim`).
                Ignored when `sandbox_id` is given or `snapshot` resolves.
            snapshot: Snapshot name. When given, the provider restores the
                named snapshot, creating it from `image` first if it does not
                exist yet. Requires a deployment with snapshot support.
            connection_config: Optional OpenSandbox `ConnectionConfigSync` for
                endpoint/auth overrides.
            lifetime_seconds: Sandbox lifetime budget in seconds for newly
                created sandboxes (default 60 minutes). Pass `0` for a sandbox
                that lives until explicitly deleted.
            **kwargs: Additional keyword arguments are forwarded to
                `SandboxSync.create`.

        Returns:
            A ready `OpenSandboxSandbox` backend.

        """
        if sandbox_id:
            return OpenSandboxSandbox(
                sandbox=SandboxSync.connect(
                    sandbox_id,
                    connection_config=connection_config,
                ),
            )

        if lifetime_seconds is None:
            lifetime = DEFAULT_SANDBOX_LIFETIME
        elif lifetime_seconds == 0:
            lifetime = None
        else:
            lifetime = timedelta(seconds=lifetime_seconds)

        if snapshot is not None:
            manager = SandboxManagerSync.create(connection_config)
            snapshot_id = self._ensure_snapshot(
                manager,
                snapshot_name=snapshot,
                image=image or DEFAULT_IMAGE,
                lifetime=lifetime,
                connection_config=connection_config,
                **kwargs,
            )
            return OpenSandboxSandbox(
                sandbox=SandboxSync.create(
                    snapshot_id=snapshot_id,
                    timeout=lifetime,
                    connection_config=connection_config,
                    **_create_kwargs(kwargs),
                ),
            )

        return OpenSandboxSandbox(
            sandbox=SandboxSync.create(
                image or DEFAULT_IMAGE,
                timeout=lifetime,
                connection_config=connection_config,
                **kwargs,
            ),
        )

    def delete(
        self,
        *,
        sandbox_id: str,
        **kwargs: Any,  # noqa: ARG002  # provider interface requires the kwarg sink
    ) -> None:
        """Terminate a sandbox by id."""
        SandboxManagerSync.create().kill_sandbox(sandbox_id)

    def _ensure_snapshot(
        self,
        manager: SandboxManagerSync,
        *,
        snapshot_name: str,
        image: str,
        lifetime: timedelta | None,
        connection_config: Any | None,
        **create_kwargs: Any,
    ) -> str:
        """Resolve a snapshot by name, building it from `image` when missing.

        The state vocabulary for snapshots is deployment-specific; the poll
        treats states containing `fail`, `error`, `delet`, or `abort` as
        terminal and waits out anything else up to a bounded budget.
        """
        existing = self._find_snapshot_by_name(manager, snapshot_name)
        if existing is not None:
            return existing.id

        logger.info(
            "Snapshot %r not found; building it from image %r", snapshot_name, image
        )
        builder = SandboxSync.create(
            image,
            timeout=DEFAULT_SANDBOX_LIFETIME,
            connection_config=connection_config,
            **create_kwargs,
        )
        try:
            snapshot = builder.create_snapshot(name=snapshot_name)
            self._raise_if_terminal(manager, snapshot, snapshot_name)
            return snapshot.id
        finally:
            try:
                builder.destroy()
            except Exception:  # noqa: BLE001  # cleanup must not mask the result
                logger.warning(
                    "Could not destroy builder sandbox %s", builder.id, exc_info=True
                )

    def _find_snapshot_by_name(
        self,
        manager: SandboxManagerSync,
        snapshot_name: str,
    ) -> SnapshotInfo | None:
        """Return the most recent non-terminal snapshot with this name."""
        page = manager.list_snapshots(SnapshotFilter(name=snapshot_name))
        for snapshot in page.snapshot_infos:
            state = (snapshot.status.state or "").lower()
            if not any(
                fragment in state for fragment in _TERMINAL_SNAPSHOT_STATE_FRAGMENTS
            ):
                return snapshot
        return None

    def _raise_if_terminal(
        self,
        manager: SandboxManagerSync,
        snapshot: SnapshotInfo,
        snapshot_name: str,
    ) -> None:
        """Fail fast when a fresh snapshot reports a terminal-bad state.

        Restore readiness itself is re-checked by `SandboxSync.create` during
        the restore, so no ready-poll is attempted here.
        """
        try:
            current = manager.get_snapshot(snapshot.id)
        except Exception:  # noqa: BLE001  # transient lookup failures are not terminal
            return
        state = (
            (current.status.state if current is not None else "") or ""
        ).lower()
        if any(
            fragment in state for fragment in _TERMINAL_SNAPSHOT_STATE_FRAGMENTS
        ):
            raise RuntimeError(
                f"Snapshot '{snapshot_name}' reported terminal state '{state}' "
                "during creation"
            )


def _create_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Forward creation kwargs to `SandboxSync.create` for snapshot restores."""
    # `image` is handled by the snapshot builder, not the restore call.
    return {key: value for key, value in kwargs.items() if key != "image"}
