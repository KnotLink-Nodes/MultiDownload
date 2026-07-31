from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, auto


class TaskStatus(IntEnum):
    WAITING = auto()
    RUNNING = auto()
    PAUSED = auto()
    COMPLETED = auto()
    FAILED = auto()


class FileSize(IntEnum):
    UNKNOWN = 0
    NOT_SUPPORTED = -1


class DownloadError(Exception):
    """Download error with format-string message and named parameters."""

    def __init__(self, message: str, **params):
        super().__init__(message)
        self.message = message
        self.params = params

    def __str__(self):
        return self.message.format_map(self.params) if self.params else self.message


@dataclass(frozen=True)
class StepError:
    """Immutable step error summary."""
    message: str
    params: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message.format_map(self.params)

    def __bool__(self) -> bool:
        return bool(self.message)


@dataclass(kw_only=True)
class DownloadStep:
    """Base class for a single download step.

    Subclasses must override ``run()``.
    """

    stepIndex: int
    status: TaskStatus = TaskStatus.WAITING
    progress: float = 0.0
    receivedBytes: int = 0
    speed: int = 0
    error: StepError | None = field(default=None, init=False)

    # ── status helpers ──────────────────────────────────────────

    def setStatus(self, status: TaskStatus) -> None:
        self.status = status
        if status == TaskStatus.COMPLETED:
            self.progress = 100
            self.speed = 0
            self.error = None
        elif status in {TaskStatus.WAITING, TaskStatus.PAUSED}:
            self.speed = 0
            self.error = None
        elif status == TaskStatus.FAILED:
            self.speed = 0

    def setError(self, error: StepError) -> None:
        self.error = error
        self.status = TaskStatus.FAILED
        self.speed = 0

    # ── interface ────────────────────────────────────────────────

    @property
    def outputPath(self) -> str:
        """Override in subclasses to return the destination file path."""
        return ""

    async def run(self, reportSpeed, waitForSpeedLimit) -> None:
        """Execute this step.

        Args:
            reportSpeed: ``(byteCount: int) -> None`` called for each chunk.
            waitForSpeedLimit: ``() -> Awaitable[None]`` awaited before each chunk.
        """
        raise NotImplementedError
