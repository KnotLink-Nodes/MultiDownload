"""
MultiDownload — KnotLink 插件节点入口。
接收下载请求，通过 OpenSocket 返回 OK，进度/完成/失败通过 Signal 异步广播。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from multidownload.knotlink import OpenSocketResponser, SignalSender, KLKVMap
from multidownload import HttpStep, DownloadConfig, TaskStatus, DownloadError
from multidownload._client import buildClient

APPID = "com.knotlink.multidownload"

# ── KL 通信注册 ─────────────────────────────────────────────
responser = OpenSocketResponser(APPID, "download")
sig_progress  = SignalSender(APPID, "progress")
sig_completed = SignalSender(APPID, "completed")
sig_failed    = SignalSender(APPID, "failed")

# ── 后台下载任务管理 ────────────────────────────────────────
_tasks: dict[str, asyncio.Task] = {}
_loop: asyncio.AbstractEventLoop | None = None


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


async def _probe(url: str) -> tuple[int, bool]:
    """探测文件大小和是否支持 Range"""
    client = buildClient(emulation=None, headers={
        "accept-language": "zh-CN,zh;q=0.9",
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }, timeout=30, verifySsl=True, proxyUrl=None)
    try:
        response = await client.get(url, headers={"range": "bytes=0-0"})
        try:
            status = response.status.as_int()
            rh = {k.decode().lower(): v.decode() for k, v in response.headers}
            can_range = status == 206 and "content-range" in rh
            cr = rh.get("content-range", "")
            _, _, total = cr.rpartition("/")
            file_size = int(total) if total and total != "*" else 0
            if file_size <= 0:
                cl = rh.get("content-length", "")
                file_size = int(cl) if cl.isdigit() else 0
        finally:
            response.close()
    finally:
        client.close()
    return file_size, can_range


async def _do_download(url: str, dest: str, req_id: str, threads: int = 8) -> None:
    """在后台 asyncio task 中执行下载，通过 KL Signal 报告进度。"""
    output_path = Path(dest)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 先探头获取文件大小
    file_size, can_range = await _probe(url)

    step = HttpStep(
        stepIndex=1,
        url=url,
        fileSize=file_size,
        outputFile=str(output_path),
        subworkerCount=threads,
        canUseRangeRequests=can_range,
        clientProfile="auto",
        headers={
            "accept-language": "zh-CN,zh;q=0.9",
            "cookie": "down_ip=1",
        },
    )

    try:
        step.setStatus(TaskStatus.RUNNING)

        # ── 开始 ──────────────────────────────────────
        print(f"[{req_id[:8]}] 开始下载: {url}")
        start_kv = KLKVMap()
        start_kv["reqID"] = req_id
        start_kv["percent"] = "0"
        start_kv["downloaded"] = "0"
        start_kv["total"] = "0"
        start_kv["speed"] = "0"
        sig_progress.emitt(start_kv.serialize())

        last_pct = 0
        last_bytes = 0
        last_time = time.time()

        received_total = 0

        def report_speed(byte_count: int) -> None:
            nonlocal last_pct, last_bytes, last_time, received_total
            received_total += byte_count
            total = step.fileSize
            pct = (received_total / total * 100) if total > 0 else 0

            if int(pct) - last_pct >= 5 or (total > 0 and received_total >= total):
                last_bytes += byte_count
                now = time.time()
                elapsed = now - last_time
                speed = int(last_bytes / elapsed) if elapsed > 0 else 0
                last_bytes = 0
                last_time = now
                last_pct = int(pct)

                print(f"[{req_id[:8]}] {int(pct)}%  {_format_size(received_total)}/{_format_size(total)}  {_format_size(speed)}/s")
                kv = KLKVMap()
                kv["reqID"] = req_id
                kv["percent"] = str(int(pct))
                kv["downloaded"] = str(received_total)
                kv["total"] = str(total)
                kv["speed"] = str(speed)
                sig_progress.emitt(kv.serialize())
            else:
                last_bytes += byte_count

        async def wait_speed_limit() -> None:
            pass  # 不限速

        config = DownloadConfig(
            client_profile="auto",
            auto_speed_up=True,
            verify_ssl=True,
        )

        await step.run(report_speed, wait_speed_limit, config=config)

        # ── 完成 ──────────────────────────────────────
        actual_size = output_path.stat().st_size if output_path.exists() else 0
        print(f"[{req_id[:8]}] 下载完成! {_format_size(actual_size)} → {output_path}")
        done_kv = KLKVMap()
        done_kv["reqID"] = req_id
        done_kv["path"] = str(output_path)
        done_kv["size"] = str(actual_size)
        sig_completed.emitt(done_kv.serialize())

        # 发最后一次 100% 进度
        final_kv = KLKVMap()
        final_kv["reqID"] = req_id
        final_kv["percent"] = "100"
        final_kv["downloaded"] = str(actual_size)
        final_kv["total"] = str(actual_size)
        final_kv["speed"] = "0"
        sig_progress.emitt(final_kv.serialize())

    except Exception as e:
        # ── 失败 ──────────────────────────────────────
        print(f"[{req_id[:8]}] 下载失败: {e}")
        err_kv = KLKVMap()
        err_kv["reqID"] = req_id
        err_kv["error"] = str(e)
        sig_failed.emitt(err_kv.serialize())

    finally:
        _tasks.pop(req_id, None)


# ── OpenSocket 回调 ─────────────────────────────────────────
def handle_download(data: str) -> str:
    """接收下载请求，启动后台任务，立即返回 OK。"""
    req = KLKVMap()
    req.deserialize(data)

    cmd     = req.get("cmd", "")

    if cmd == "ping":
        return "pong"

    url     = req.get("url", "")
    dest    = req.get("dest", "")
    req_id  = req.get("reqID", "")
    threads = int(req.get("threads", "8"))

    if not url or not dest:
        return "error: url and dest required"

    if req_id in _tasks and not _tasks[req_id].done():
        return "error: reqID already in progress"

    coro = _do_download(url, dest, req_id, threads)
    task = asyncio.run_coroutine_threadsafe(coro, _loop)
    _tasks[req_id] = task
    return "OK"


responser.set_RecvFunc(handle_download)

# ── 主循环 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[MultiDownload] 已启动，appID={APPID}")

    # 创建后台事件循环（供 SDK 回调线程提交 asyncio 任务）
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True).start()

    # 独立测试模式
    if "--standalone" in sys.argv:
        print("[MultiDownload] 独立测试模式")
        test_url = sys.argv[2] if len(sys.argv) > 2 else "https://github.com/KnotLink-Nodes/GetUSBSerialNumber/releases/download/v1.0.0/GetUSBSerialNumber.zip"
        test_dest = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.environ.get("TEMP", "."), "md_standalone_test.zip")
        print(f"  下载: {test_url}")
        print(f"  保存: {test_dest}")
        asyncio.run_coroutine_threadsafe(_do_download(test_url, test_dest, "standalone-test"), _loop)
        time.sleep(60)
        sys.exit(0)

    # 检查连接状态
    if responser.KLresponser.connected:
        print("[MultiDownload] 已连接到 Core (127.0.0.1:6378)")
    else:
        print("[MultiDownload] ⚠ 未能连接到 Core (127.0.0.1:6378)")

    try:
        while True:
            time.sleep(5)
            if not responser.KLresponser.connected:
                print("[MultiDownload] ⚠ 与 Core 的连接已断开")
                break
    except KeyboardInterrupt:
        print("[MultiDownload] 已退出")
