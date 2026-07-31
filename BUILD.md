# 构建 MultiDownload.exe

使用 Nuitka 将 Python 项目编译为独立可执行文件，作为 KnotLink 插件分发。

## 安装 Nuitka

```bash
pip install nuitka ordered-set zstandard
```

## 编译

在项目根目录执行：

```bash
nuitka --standalone --windows-console-mode=disable \
  --include-package=multidownload \
  --include-package-data=multidownload.knotlink \
  --output-dir=build \
  --output-filename=MultiDownload.exe \
  main.py
```

> `--windows-console-mode=disable` 去掉黑框，插件后台运行不需要控制台窗口。

## 产物

```
build/main.dist/MultiDownload.exe
```

将此 exe 与 `com.knotlink.multidownload/plugin_manifest.json`、`com.knotlink.multidownload/FuncList.json` 打包为 zip 即可通过 KnotHub 插件市场分发。

## 分发结构

```
com.knotlink.multidownload.zip
├── MultiDownload.exe
├── plugin_manifest.json
└── FuncList.json
```

> JSON 文件位于 `com.knotlink.multidownload/` 目录下，按 appID 组织。

安装后 Core 自动启动 `MultiDownload.exe`，连上 KL 总线即可接收下载请求。
