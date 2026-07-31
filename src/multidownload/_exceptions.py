from __future__ import annotations


class PermanentDownloadError(Exception):
    """Server returned an unrecoverable HTTP status (4xx, cf-mitigated)."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class RangeNotSupportedError(Exception):
    """Server does not support HTTP Range requests."""
