# DoubaoManager（豆包多账号视频管理器）

本地桌面应用，用于管理多个豆包账号会话，并批量提交 **文生视频 / 图生视频** 任务。

- 桌面壳：PyWebView
- 后端：FastAPI + SQLite
- 浏览器自动化：Playwright Chromium
- 前端：Vue 3 + TypeScript

---

## 功能概览

| 模块 | 说明 |
| --- | --- |
| 账号池 | 扫码登录豆包，独立浏览器会话，启停/删除账号 |
| 视频任务 | 文生 / 图生（1–9 张图），自动分配账号，串行同账号、可并行不同账号 |
| 生成结果 | 成功任务的预览、下载、复制链接 |
| 运行日志 | 本地结构化日志（自动脱敏 Cookie / Token 等） |
| 设置 | 并发、每日额度、调度策略、默认模型参数、备份数据库 |

图生规则：**有图=图生，无图=文生**，最多 9 张图。

---

## Windows 用户：下载安装包

1. 打开 [Releases](https://github.com/shukeCyp/DoubaoManager/releases) 下载  
   `DoubaoManager-windows-x64.zip`
2. 解压到任意目录
3. 双击 `DoubaoManager.exe` 启动

安装包已包含：

- 应用本体与前端资源
- **Playwright Chromium 浏览器**（无需再单独安装浏览器）

> 首次启动若被 Windows Defender / SmartScreen 拦截，选择「仍要运行」即可（本地自建应用常见提示）。

---

## 从源码运行

### Windows

1. 安装 [Node.js](https://nodejs.org/)（含 npm）
2. 双击 `run.bat`  
   （使用仓库内置 `tools\uv.exe`，无需全局安装 uv）

### macOS / Linux

```bash
# 需要 uv、Node.js、npm
./run.sh
```

---

## 打包 Windows exe（含浏览器）

在 **Windows** 上执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1
```

产物：

```text
dist/DoubaoManager/DoubaoManager.exe
dist/DoubaoManager/ms-playwright/   # Chromium
dist/DoubaoManager/frontend/dist/
dist/DoubaoManager-windows-x64.zip
```

### GitHub Actions

推送版本标签即可自动打包并上传到 Release：

```bash
git tag v0.1.0
git push origin v0.1.0
```

也可在 Actions 页手动运行 **Build Windows Release**。

---

## 本地数据目录

| 系统 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\DoubaoManager` |
| macOS | `~/Library/Application Support/DoubaoManager` |
| Linux | `~/.local/share/DoubaoManager` |

- 每个账号独立浏览器 profile  
- 不保存密码；Cookie 不入库  
- 日志会脱敏敏感字段  

可通过环境变量覆盖：

```text
DOUPOOL_DATA_DIR
DOUPOOL_LOG_DIR
DOUPOOL_FRONTEND_DIR
DOUPOOL_DEBUG=1
```

---

## 开发验证

```bash
uv sync --group dev
uv run pytest -v
cd frontend && npm ci && npm test && npm run build
```

---

## 目录结构（简要）

```text
DoubaoManager/
├── src/doupool/          # Python 后端
├── frontend/             # Vue 前端
├── packaging/            # PyInstaller 规格与入口
├── scripts/
│   ├── package_windows.ps1
│   └── capture_doubao_video.py
├── tools/uv.exe          # Windows 内置 uv（开发用）
├── run.bat / run.sh
└── .github/workflows/build-windows.yml
```

---

## 注意事项

1. 依赖豆包网页协议，页面改版可能导致接口失效，需重新抓包适配。  
2. 请仅在已授权的自有账号上使用，遵守豆包服务条款。  
3. 打包体积较大（含 Chromium），属正常现象。  

---

## License

仅供个人学习与自用管理，请勿用于未授权的商业用途或滥用。
