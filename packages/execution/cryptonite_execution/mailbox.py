"""Thread-safe command mailbox bridging async gateway/poller to node worker thread."""

import queue
from typing import Any


class NodeCommandMailbox:
    """Thread-safe queue for dispatching operational commands into the dedicated LiveNode thread."""

    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)

    def push(self, command: dict[str, Any], timeout: float | None = 1.0) -> bool:
        """Push a command from an external or async thread into the mailbox."""
        try:
            self._queue.put(command, block=True, timeout=timeout)
            return True
        except queue.Full:
            return False

    def drain(self) -> list[dict[str, Any]]:
        """Non-blocking drain of all pending commands from within the node worker thread."""
        commands = []
        while True:
            try:
                cmd = self._queue.get_nowait()
                commands.append(cmd)
            except queue.Empty:
                break
        return commands

    @property
    def qsize(self) -> int:
        """Return approximate current size of mailbox."""
        return self._queue.qsize()
