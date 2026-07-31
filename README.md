# MultiDownload — KnotLink 多线程下载器

基于 [Ghost-Downloader-3](https://github.com/XiaoYouChR/Ghost-Downloader-3) 核心下载引擎，封装为 KnotLink 插入式插件节点。

## 版权声明

本项目下载引擎提取自 **Ghost-Downloader-3**（作者 [XiaoYouChR](https://github.com/XiaoYouChR)），
遵循 **GPL-3.0-only** 开源协议。保留了原作者版权信息，详见 `pyproject.toml` 和 `LICENSE`。

## 功能

- IDM 风格多线程分块 HTTP 下载
- TLS 指纹模拟（wreq）
- 断点续传
- KnotLink 协议集成——通过 KL 信号异步推送下载进度

## KnotLink 接口

| 类型 | ID | 说明 |
|------|-----|------|
| appID | `com.knotlink.multidownload` | |
| OpenSocket | `download` | 接收下载请求 |
| Signal | `progress` | 下载进度（≥5% 变更时广播） |
| Signal | `completed` | 下载完成 |
| Signal | `failed` | 下载失败 |

### 请求下载

```
com.knotlink.multidownload-download&*&cmd=start;url=...;dest=...;reqID=uuid
```

### 信号格式

```
# 进度
com.knotlink.multidownload-progress;reqID=uuid;percent=45;downloaded=3932160;total=8746496;speed=512000

# 完成
com.knotlink.multidownload-completed;reqID=uuid;path=C:\...\out.zip;size=8746496

# 失败
com.knotlink.multidownload-failed;reqID=uuid;error=message
```

## 安装与运行

### 依赖

- Python ≥ 3.11
- wreq（可选，TLS 指纹模拟）: `pip install multidownload[tls]`

### 独立测试（不需要 Core）

```bash
python main.py --standalone "https://example.com/file.zip" "C:\downloads\out.zip"
```

### 作为 KnotLink 插件运行

```bash
python main.py
```

需要 KnotLink Core 在线。启动后自动注册到 KL 总线。

## 许可

本项目继承 Ghost-Downloader-3 的 **GPL-3.0-only** 协议。
KnotLink SDK（`src/multidownload/knotlink/`）采用 MIT 协议。
