"""MultiDownload CLI — IDM-style async multi-threaded HTTP downloader."""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from multidownload import HttpStep, DownloadConfig, TaskStatus, DownloadError
from multidownload._client import buildClient, toEmulation


def _formatSize(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


async def _probe(url: str, config: DownloadConfig) -> tuple[int, bool, str, str]:
    emulation = toEmulation(config.client_profile, "")
    defaultHeaders = {
        "accept-language": "zh-CN,zh;q=0.9",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    client = buildClient(
        emulation=emulation, headers=defaultHeaders, timeout=30,
        verifySsl=config.verify_ssl, proxyUrl=config.proxy_url,
    )
    try:
        response = await client.get(url, headers={**defaultHeaders, "range": "bytes=0-0"})
        try:
            status = response.status.as_int()
            rh = {k.decode().lower(): v.decode() for k, v in response.headers}
            canRange = status == 206 and "content-range" in rh
            cr = rh.get("content-range", "")
            _, _, total = cr.rpartition("/")
            fileSize = int(total) if total and total != "*" else 0
            if fileSize <= 0:
                cl = rh.get("content-length", "")
                fileSize = int(cl) if cl.isdigit() else 0
            finalUrl = str(response.url)
        finally:
            response.close()
    finally:
        client.close()

    name = ""
    cd = rh.get("content-disposition", "")
    if cd and "filename=" in cd:
        import re
        m = re.search(r'filename[^;=\n]*="?([^";\n]+)"?', cd)
        if m:
            name = m.group(1)
    if not name:
        name = finalUrl.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return fileSize, canRange, name, finalUrl


async def _download(url: str, output: str, config: DownloadConfig, subworkers: int) -> int:
    outputPath = Path(output)
    fileSize, canRange, name, _finalUrl = await _probe(url, config)

    if canRange and not fileSize:
        canRange = False

    if not outputPath.name or outputPath.is_dir() or output.endswith(("/", "\\")):
        outputPath = outputPath / name if name else outputPath / "download"

    print(f"  Size: {_formatSize(fileSize) if fileSize else 'unknown'}")
    print(f"  Chunks: {subworkers}  Range: {canRange}")
    print(f"  → {outputPath}\n")

    reqHeaders = {
        "accept-language": "zh-CN,zh;q=0.9",
        "cookie": "down_ip=1",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }
    step = HttpStep(
        stepIndex=1, url=url, fileSize=fileSize,
        outputFile=str(outputPath), subworkerCount=subworkers,
        canUseRangeRequests=canRange, clientProfile=config.client_profile,
        headers=reqHeaders,
    )
    step.setStatus(TaskStatus.RUNNING)

    start = time.time()
    lastReport = start
    lastBytes = 0
    lastSpeed = 0

    def reportSpeed(n):
        nonlocal lastReport, lastBytes, lastSpeed
        lastBytes += n
        now = time.time()
        if now - lastReport >= 0.3:
            elapsed = now - lastReport
            lastSpeed = int(lastBytes / elapsed) if elapsed > 0 else 0
            pct = step.progress
            total = step.fileSize
            barW = 30
            filled = int(barW * pct / 100) if total > 0 else 0
            bar = "█" * filled + "░" * (barW - filled)
            if total:
                print(f"\r  [{bar}] {pct:5.1f}%  {_formatSize(lastSpeed)}/s", end="", flush=True)
            else:
                print(f"\r  {step.receivedBytes:,} bytes  {_formatSize(lastSpeed)}/s", end="", flush=True)
            lastReport = now
            lastBytes = 0

    async def waitForSpeedLimit():
        pass

    try:
        await step.run(reportSpeed, waitForSpeedLimit, config=config)
    except DownloadError as e:
        print(f"\n  ERROR: {e}")
        return 1

    elapsed = time.time() - start
    actual = outputPath.stat().st_size if outputPath.exists() else 0
    print(f"\n  Done in {elapsed:.1f}s — {_formatSize(actual)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="MultiDownload — async chunked HTTP downloader")
    parser.add_argument("url", help="URL to download")
    parser.add_argument("-o", "--output", default=".", help="Output file or directory")
    parser.add_argument("-n", "--subworkers", type=int, default=8, help="Chunk count (default: 8)")
    parser.add_argument("-p", "--profile", default="auto", help="TLS emulation profile (auto|chrome|raw|...)")
    parser.add_argument("--proxy", help="Proxy URL")
    parser.add_argument("--no-ssl-verify", action="store_true", help="Disable TLS verification")
    parser.add_argument("--no-speedup", action="store_true", help="Disable auto-acceleration")
    args = parser.parse_args()

    config = DownloadConfig(
        client_profile=args.profile,
        auto_speed_up=not args.no_speedup,
        verify_ssl=not args.no_ssl_verify,
        proxy_url=args.proxy,
    )

    exitCode = asyncio.run(_download(args.url, args.output, config, args.subworkers))
    sys.exit(exitCode)


if __name__ == "__main__":
    main()
