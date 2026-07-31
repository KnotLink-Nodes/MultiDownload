from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DownloadConfig:
    """Explicit configuration for the HTTP download engine.

    Every value that was formerly read from GD3's global ``cfg`` singleton
    is now a plain field with the production default.
    """

    # ── chunking & acceleration ──────────────────────────────────

    max_reassign_size_kb: int = 512
    """Subworkers with fewer remaining bytes are NOT split.
    Original GD3 key: ``cfg.maxReassignSize``."""

    auto_speed_up: bool = True
    """When ``True``, dynamically increase subworker count when speed
    stabilises. Original GD3 key: ``cfg.autoSpeedUp``."""

    # ── TLS emulation ────────────────────────────────────────────

    client_profile: str = "auto"
    """wreq TLS-emulation profile. ``"raw"`` means no emulation.
    Original GD3 key: ``cfg.clientProfile``."""

    # ── metadata ─────────────────────────────────────────────────

    preserve_last_modified: bool = False
    """Set the output file's mtime from the ``Last-Modified`` response header.
    Original GD3 key: ``cfg.shouldPreserveLastModified``."""

    # ── network ──────────────────────────────────────────────────

    verify_ssl: bool = True
    """Verify TLS certificates. Original GD3 key: ``cfg.shouldVerifySsl``."""

    proxy_url: str | None = None
    """Proxy URL, e.g. ``"http://127.0.0.1:8080"``. ``None`` = no proxy."""
