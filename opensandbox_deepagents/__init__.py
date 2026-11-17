"""OpenSandbox integration for Deep Agents.

Exposes:

- `OpenSandboxSandbox`: a `deepagents.backends.sandbox.BaseSandbox` backend
  over the official OpenSandbox Python SDK (`SandboxSync`).
- `OpenSandboxProvider`: a `deepagents-code` sandbox provider, registered under
  the `deepagents_code.sandbox_providers` entry-point group (requires the
  optional `dcode` extra).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opensandbox_deepagents.sandbox import OpenSandboxSandbox

if TYPE_CHECKING:
    from opensandbox_deepagents.provider import OpenSandboxProvider

__version__ = "0.1.0"

__all__ = [
    "OpenSandboxProvider",
    "OpenSandboxSandbox",
    "__version__",
]


def __getattr__(name: str) -> object:
    """Lazy-import the provider so `deepagents-code` stays an optional dep."""
    if name == "OpenSandboxProvider":
        from opensandbox_deepagents.provider import OpenSandboxProvider

        return OpenSandboxProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
