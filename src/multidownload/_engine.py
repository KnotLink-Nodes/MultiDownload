from __future__ import annotations

import asyncio
import errno
import logging
import os
from asyncio import TaskGroup, CancelledError
from contextlib import suppress
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from struct import unpack, pack

from multidownload._client import buildClient, toEmulation
from multidownload._config import DownloadConfig
from multidownload._exceptions import PermanentDownloadError, RangeNotSupportedError
from multidownload._io import ftruncate, pwrite
from multidownload._models import DownloadStep, DownloadError, TaskStatus, FileSize, StepError

logger = logging.getLogger(__name__)

STREAM_READ_TIMEOUT = 30
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 410, 451})
FATAL_IO_ERRNO = frozenset({
    errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO,
    getattr(errno, "ENODEV", 39),  # 39 = ENODEV on some platforms
    getattr(errno, "ENOMEDIUM", 112),  # 112 on some platforms
})


# ── subworker ────────────────────────────────────────────────────

@dataclass
class Subworker:
    """A byte-range download unit that writes directly to its file offset."""

    index: int
    start: int
    end: int
    receivedBytes: int = 0

    @property
    def position(self) -> int:
        return self.start + self.receivedBytes


# ── HTTP download step ───────────────────────────────────────────

@dataclass(kw_only=True)
class HttpStep(DownloadStep):
    """IDM-style async multi-chunk HTTP download step.

    Opens *N* concurrent byte-range connections, writes each chunk
    directly to the target file offset (zero merge step), and
    dynamically rebalances slow subworkers.

    Usage::

        step = HttpStep(
            stepIndex=1, url="https://...", fileSize=1048576,
            outputFile="./out.bin", subworkerCount=8,
            canUseRangeRequests=True,
        )
        await step.run(reportSpeed, waitForSpeedLimit, config=DownloadConfig())
    """

    url: str = ""
    fileSize: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    clientProfile: str = ""
    userAgent: str = ""
    subworkerCount: int = 8
    canUseRangeRequests: bool = False
    lastModified: str = ""
    isAccelerated: bool = False
    outputFile: str = ""
    subworkers: list[Subworker] = field(default_factory=list, repr=False)

    # ── internals ────────────────────────────────────────────────

    _config: DownloadConfig = field(default_factory=DownloadConfig, repr=False)

    @property
    def canPause(self) -> bool:
        return self.canUseRangeRequests

    @property
    def outputPath(self) -> str:
        return self.outputFile

    # ── file management ──────────────────────────────────────────

    def deleteFiles(self):
        path = Path(self.outputPath)
        try:
            if path.is_dir() and not path.is_symlink():
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            Path(f"{self.outputPath}.ghd").unlink(missing_ok=True)
        except OSError:
            pass

    def moveFiles(self, oldFolder: Path, newFolder: Path) -> None:
        from shutil import move
        raw = self.outputPath
        if not raw:
            return
        oldPath = Path(raw)
        if not oldPath.exists():
            return
        try:
            rel = oldPath.relative_to(oldFolder)
        except ValueError:
            return
        newPath = newFolder / rel
        newPath.parent.mkdir(parents=True, exist_ok=True)
        move(str(oldPath), str(newPath))
        ghd = Path(f"{raw}.ghd")
        if ghd.exists():
            move(str(ghd), str(newFolder / f"{rel}.ghd"))
        self.outputFile = str(newPath)

    def setOptions(self, options: dict) -> None:
        if "headers" in options:
            self.headers = options["headers"]
        if "clientProfile" in options:
            self.clientProfile = options["clientProfile"]
        if "userAgent" in options:
            self.userAgent = options["userAgent"]
        if "subworkerCount" in options:
            self.subworkerCount = options["subworkerCount"]

    # ── progress record (.ghd) ───────────────────────────────────

    def _loadRecord(self) -> list[Subworker]:
        recordPath = Path(f"{self.outputPath}.ghd")
        if not recordPath.exists():
            return []
        try:
            result = []
            with open(recordPath, "rb") as f:
                index = 0
                while data := f.read(24):
                    start, position, end = unpack("<QQQ", data)
                    result.append(Subworker(
                        index=index, start=start, end=end,
                        receivedBytes=position - start,
                    ))
                    index += 1
            return result
        except Exception:
            logger.error("Failed to restore subworkers from %s", self.outputPath, exc_info=True)
            return []

    def _deleteRecord(self) -> None:
        target = Path(f"{self.outputPath}.ghd")
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
        except OSError:
            logger.error("Failed to delete progress file %s", target, exc_info=True)

    # ── chunking ─────────────────────────────────────────────────

    def _buildSubworkers(self) -> list[Subworker]:
        if not self.canUseRangeRequests:
            return [Subworker(index=0, start=0, end=FileSize.NOT_SUPPORTED)]

        if self.fileSize == FileSize.UNKNOWN:
            return [Subworker(index=0, start=0, end=FileSize.UNKNOWN)]

        count = min(self.subworkerCount, self.fileSize)
        chunkSize = self.fileSize // count
        result = []
        start = 0
        for i in range(count - 1):
            end = start + chunkSize - 1
            result.append(Subworker(index=i, start=start, end=end))
            start = end + 1
        result.append(Subworker(index=count - 1, start=start, end=self.fileSize - 1))
        return result

    def _splitSlowest(self) -> Subworker | None:
        slowest = max(self.subworkers, key=lambda sw: sw.end - sw.position + 1)
        remaining = slowest.end - slowest.position + 1
        if remaining < 2:
            return None

        base = remaining // 2
        remainder = remaining % 2
        oldEnd = slowest.end
        slowest.end = slowest.position + base + remainder - 1

        newSw = Subworker(
            index=len(self.subworkers),
            start=slowest.end + 1,
            end=oldEnd,
        )
        self.subworkers.insert(self.subworkers.index(slowest) + 1, newSw)
        return newSw

    def _reassignSubworker(self) -> None:
        if self.fileSize <= 0:
            return
        slowest = max(self.subworkers, key=lambda sw: sw.end - sw.position + 1)
        if slowest.end - slowest.position + 1 < self._config.max_reassign_size_kb * 1024:
            return
        newSw = self._splitSlowest()
        if newSw:
            self._taskGroup.create_task(self._runSubworker(newSw, self._fd))

    # ── AI acceleration ──────────────────────────────────────────

    def _autoSpeedUp(self) -> None:
        if self.isAccelerated or not self._config.auto_speed_up:
            return

        self._speedHistory.append(self.speed)
        if len(self._speedHistory) > 5:
            self._speedHistory.pop(0)
        if len(self._speedHistory) < 5:
            return

        avgSpeed = sum(self._speedHistory) / len(self._speedHistory)
        if avgSpeed == 0:
            return

        maxDeviation = max(abs(s - avgSpeed) / avgSpeed for s in self._speedHistory)
        if maxDeviation > 0.15:
            return

        if self._accelCheckTime == 0:
            self._accelInitialWorkers = len(self.subworkers)
            self._accelInitialSpeed = avgSpeed
            self._accelCheckTime = asyncio.get_event_loop().time()
            for _ in range(4):
                self._reassignSubworker()
        else:
            elapsed = asyncio.get_event_loop().time() - self._accelCheckTime
            if elapsed <= 5:
                return
            workerRatio = (len(self.subworkers) - self._accelInitialWorkers) / max(self._accelInitialWorkers, 1)
            speedRatio = (avgSpeed - self._accelInitialSpeed) / max(self._accelInitialSpeed, 1)
            if speedRatio < 0.8 * workerRatio:
                self.isAccelerated = True
                logger.info(
                    "Auto-acceleration disabled — worker increase: %.2f%%, speed increase: %.2f%%",
                    workerRatio * 100, speedRatio * 100,
                )
            else:
                self._accelCheckTime = 0
                logger.info(
                    "Continuing auto-acceleration — worker increase: %.2f%%, speed increase: %.2f%%",
                    workerRatio * 100, speedRatio * 100,
                )

    # ── supervisor ───────────────────────────────────────────────

    async def _supervise(self) -> None:
        recordFile = None
        if self.canUseRangeRequests:
            recordFile = open(f"{self.outputPath}.ghd", "wb")
        try:
            self.receivedBytes = sum(sw.receivedBytes for sw in self.subworkers)
            while True:
                if recordFile is not None:
                    data = tuple(
                        val for sw in self.subworkers
                        for val in (sw.start, sw.position, sw.end)
                    )
                    recordFile.seek(0)
                    recordFile.write(pack("<" + "Q" * len(data), *data))
                    recordFile.flush()
                    recordFile.truncate()

                receivedBytes = sum(sw.receivedBytes for sw in self.subworkers)
                self.speed = receivedBytes - self.receivedBytes
                self.receivedBytes = receivedBytes
                if self.fileSize > 0:
                    self.progress = (receivedBytes / self.fileSize) * 100
                else:
                    self.progress = 0

                self._autoSpeedUp()
                await asyncio.sleep(1)
        except CancelledError:
            pass
        finally:
            if recordFile is not None:
                recordFile.close()

    # ── subworker runner ─────────────────────────────────────────

    async def _runSubworker(self, subworker: Subworker, fd: int) -> None:
        client = buildClient(
            emulation=self._emulation,
            userAgent=self.userAgent or None,
            readTimeout=STREAM_READ_TIMEOUT,
            verifySsl=self._config.verify_ssl,
            proxyUrl=self._config.proxy_url,
        )
        try:
            await self._runSubworkerWith(subworker, fd, client)
        finally:
            client.close()

    async def _runSubworkerWith(self, subworker: Subworker, fd: int, client) -> None:
        effectiveUrl = self._effectiveUrl
        effectiveHeaders = self._effectiveHeaders

        # ── mode 1: unknown size ─────────────────────────────────
        if subworker.end == FileSize.UNKNOWN:
            while True:
                try:
                    headers = {
                        **effectiveHeaders,
                        "range": f"bytes={subworker.position}-",
                        "accept-encoding": "identity",
                    }
                    response = await client.get(effectiveUrl, headers=headers)
                    try:
                        status = response.status.as_int()
                        if status in PERMANENT_STATUS or response.headers.contains_key("cf-mitigated"):
                            raise PermanentDownloadError(status)
                        if status == 200:
                            raise RangeNotSupportedError()
                        if status != 206:
                            raise Exception(f"Server rejected range request, status: {status}")
                        async for chunk in response.stream():
                            if not chunk:
                                continue
                            pwrite(fd, chunk, subworker.position)
                            subworker.receivedBytes += len(chunk)
                            self._reportSpeed(len(chunk))
                            await self._waitForSpeedLimit()
                    finally:
                        response.close()
                    return
                except CancelledError:
                    raise
                except (PermanentDownloadError, RangeNotSupportedError):
                    raise
                except Exception as e:
                    if isinstance(e, OSError) and e.errno in FATAL_IO_ERRNO:
                        raise
                    logger.error(
                        "Subworker chunk failed, retrying in 5s: %s", self.outputPath, exc_info=True,
                    )
                    await asyncio.sleep(5)

        # ── mode 2: single stream (no range support) ─────────────
        elif subworker.end == FileSize.NOT_SUPPORTED:
            while True:
                try:
                    ftruncate(fd, 0)
                    subworker.receivedBytes = 0
                    response = await client.get(effectiveUrl, headers=dict(effectiveHeaders))
                    try:
                        status = response.status.as_int()
                        if status in PERMANENT_STATUS or response.headers.contains_key("cf-mitigated"):
                            raise PermanentDownloadError(status)
                        if status != 200:
                            raise Exception(f"Server returned unexpected status: {status}")
                        async for chunk in response.stream():
                            if not chunk:
                                continue
                            pwrite(fd, chunk, subworker.receivedBytes)
                            subworker.receivedBytes += len(chunk)
                            self._reportSpeed(len(chunk))
                            await self._waitForSpeedLimit()
                    finally:
                        response.close()
                    ftruncate(fd, subworker.receivedBytes)
                    return
                except CancelledError:
                    raise
                except PermanentDownloadError:
                    raise
                except Exception as e:
                    if isinstance(e, OSError) and e.errno in FATAL_IO_ERRNO:
                        raise
                    logger.error(
                        "Single-stream download failed, retrying in 5s: %s", self.outputPath, exc_info=True,
                    )
                    await asyncio.sleep(5)

        # ── mode 3: fixed byte range ─────────────────────────────
        else:
            while subworker.position <= subworker.end:
                try:
                    headers = {
                        **effectiveHeaders,
                        "range": f"bytes={subworker.position}-{subworker.end}",
                        "accept-encoding": "identity",
                    }
                    response = await client.get(effectiveUrl, headers=headers)
                    try:
                        status = response.status.as_int()
                        if status in PERMANENT_STATUS or response.headers.contains_key("cf-mitigated"):
                            raise PermanentDownloadError(status)
                        if status == 200:
                            raise RangeNotSupportedError()
                        if status != 206:
                            raise Exception(f"Server rejected range request, status: {status}")
                        async for chunk in response.stream():
                            if not chunk:
                                continue
                            remaining = subworker.end - subworker.position + 1
                            if len(chunk) > remaining:
                                chunk = chunk[:remaining]
                            pwrite(fd, chunk, subworker.position)
                            subworker.receivedBytes += len(chunk)
                            self._reportSpeed(len(chunk))
                            await self._waitForSpeedLimit()
                            if subworker.position > subworker.end:
                                break
                    finally:
                        response.close()

                    if subworker.position > subworker.end:
                        subworker.receivedBytes = subworker.end - subworker.start + 1

                except CancelledError:
                    raise
                except (PermanentDownloadError, RangeNotSupportedError):
                    raise
                except Exception as e:
                    if isinstance(e, OSError) and e.errno in FATAL_IO_ERRNO:
                        raise
                    logger.error(
                        "Subworker chunk failed, retrying in 5s: %s", self.outputPath, exc_info=True,
                    )
                    await asyncio.sleep(5)

            self._reassignSubworker()

    # ── main entry point ─────────────────────────────────────────

    async def run(self, reportSpeed, waitForSpeedLimit, *,
                  config: DownloadConfig | None = None) -> None:
        """Run the download.

        Args:
            reportSpeed: ``(byteCount: int) -> None`` — called per chunk.
            waitForSpeedLimit: ``() -> Awaitable[None]`` — awaited per chunk.
            config: Optional :class:`DownloadConfig` override.
        """
        self._config = config or DownloadConfig()
        self._reportSpeed = reportSpeed
        self._waitForSpeedLimit = waitForSpeedLimit
        self._speedHistory: list[int] = []
        self._accelCheckTime = 0
        shouldDeleteRecord = False

        Path(self.outputPath).parent.mkdir(parents=True, exist_ok=True)

        self._effectiveHeaders = {**self.headers}
        if self.userAgent and not any(k.lower() == "user-agent" for k in self.headers):
            self._effectiveHeaders["user-agent"] = self.userAgent

        self._emulation = toEmulation(
            self.clientProfile or self._config.client_profile, ""
        )

        # ── probe URL (follow redirects) ─────────────────────────
        probeHeaders = {
            **self._effectiveHeaders,
            "range": "bytes=0-0",
            "accept-encoding": "identity",
        }
        client = buildClient(
            emulation=self._emulation,
            userAgent=self.userAgent or None,
            timeout=30,
            verifySsl=self._config.verify_ssl,
            proxyUrl=self._config.proxy_url,
        )
        try:
            response = await client.get(self.url, headers=probeHeaders)
            self._effectiveUrl = str(response.url)
            response.close()
        finally:
            client.close()

        # ── restore or build subworkers ──────────────────────────
        restored = False
        if self.canUseRangeRequests:
            loaded = self._loadRecord()
            if loaded:
                self.subworkers = loaded
                restored = True

        if not restored:
            if not self.canUseRangeRequests:
                self._deleteRecord()
            self.subworkers = self._buildSubworkers()
        elif self.fileSize > 0:
            target = min(self.subworkerCount, self.fileSize)
            while len(self.subworkers) < target:
                if not self._splitSlowest():
                    break

        # ── open output file ─────────────────────────────────────
        openMode = os.O_RDWR | os.O_CREAT
        if not self.canUseRangeRequests:
            openMode |= os.O_TRUNC
        self._fd = os.open(self.outputPath, openMode, 0o666)

        if not restored and self.fileSize > 0:
            try:
                ftruncate(self._fd, self.fileSize)
            except Exception:
                logger.error(
                    "Failed to pre-allocate file: %s", self.outputPath, exc_info=True,
                )

        # ── download loop ────────────────────────────────────────
        try:
            while True:
                supervisor = asyncio.create_task(self._supervise())
                try:
                    self._taskGroup = TaskGroup()
                    async with self._taskGroup:
                        for subworker in self.subworkers:
                            self._taskGroup.create_task(
                                self._runSubworker(subworker, self._fd)
                            )

                    self.setStatus(TaskStatus.COMPLETED)
                    shouldDeleteRecord = True
                    break
                except CancelledError:
                    self.setStatus(TaskStatus.PAUSED)
                    raise
                except ExceptionGroup as eg:
                    if self.canUseRangeRequests and any(
                        isinstance(e, RangeNotSupportedError) for e in eg.exceptions
                    ):
                        logger.warning(
                            "Server does not support range requests, "
                            "falling back to single stream: %s",
                            self.outputPath,
                        )
                        self.canUseRangeRequests = False
                        self.subworkers = self._buildSubworkers()
                        ftruncate(self._fd, 0)
                        self._deleteRecord()
                        self._speedHistory.clear()
                        self._accelCheckTime = 0
                        continue

                    cause = eg.exceptions[0]
                    if isinstance(cause, PermanentDownloadError):
                        raise DownloadError(
                            "Server returned an error ({status})", status=cause.status,
                        ) from eg
                    if isinstance(cause, OSError) and cause.errno in FATAL_IO_ERRNO:
                        raise DownloadError("Insufficient disk space") from eg
                    raise cause from eg
                finally:
                    if not supervisor.done():
                        supervisor.cancel()
                        with suppress(CancelledError):
                            await supervisor
        finally:
            os.close(self._fd)
            if shouldDeleteRecord:
                self._deleteRecord()
                if self._config.preserve_last_modified and self.lastModified:
                    try:
                        mtime = parsedate_to_datetime(self.lastModified).timestamp()
                        os.utime(self.outputPath, (mtime, mtime))
                    except Exception:
                        logger.warning(
                            "Failed to set file modification time: %s", self.outputPath, exc_info=True,
                        )
