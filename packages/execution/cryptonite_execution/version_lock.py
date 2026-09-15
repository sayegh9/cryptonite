"""NautilusTrader version lock verification."""

import nautilus_trader

REQUIRED_NAUTILUS_VERSION = "2.0.0rc4"


class VersionLockError(RuntimeError):
    """Raised when the runtime NautilusTrader version does not match the pinned contract."""


def assert_version_locked() -> None:
    """Refuse to start or run if nautilus_trader version does not strictly match 2.0.0rc4."""
    current_version = getattr(nautilus_trader, "__version__", "unknown")
    if current_version != REQUIRED_NAUTILUS_VERSION:
        raise VersionLockError(
            f"NautilusTrader version lock violation: required '{REQUIRED_NAUTILUS_VERSION}', "
            f"found '{current_version}'. Execution halted."
        )
