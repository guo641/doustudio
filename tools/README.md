# 内置工具

| 文件 | 说明 |
| --- | --- |
| `uv.exe` | [uv](https://github.com/astral-sh/uv) Windows x64 便携版（当前 `0.11.28`） |
| `uvx.exe` | uv 附带的 `uvx` 入口 |

Windows 启动请双击项目根目录的 `run.bat`，脚本会优先使用本目录的 `uv.exe`，无需全局安装 uv。

仍需本机已安装 **Node.js / npm**（用于构建前端）。

如需升级 uv：

```text
https://github.com/astral-sh/uv/releases
下载 uv-x86_64-pc-windows-msvc.zip，解压后覆盖 tools\uv.exe
```
