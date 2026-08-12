# DouStudio(豆包工作室)

本地桌面应用,基于 [DoubaoManager v0.1.0](https://github.com/shukeCyp/DoubaoManager) 二次开发,
用于管理多个豆包账号会话,并批量提交 **文生视频 / 图生视频** 任务。

- 桌面壳:PyWebView
- 后端:FastAPI + SQLite
- 浏览器自动化:Playwright Chromium
- 前端:Vue 3 + TypeScript

---

## 功能概览

| 模块 | 说明 |
| --- | --- |
| 账号池 | 扫码登录豆包,独立浏览器会话,启停/删除账号 |
| 视频任务 | 文生 / 图生(1-9 张图),自动分配账号,串行同账号、可并行不同账号 |
| 分组任务 | textarea 多行 prompt 自动归到同一 group_id,后端按组聚合查询 |
| 失败改 prompt | 5 种失败分类,违规自动剥离风险关键词并软化重试,最多 2 次 |
| 去水印 | 视频生成后自动调 [zhuceka](https://api.zhuceka.cn) 拿无水印直链 |
| 生成结果 | 成功任务的预览、下载、复制链接;无水印版本优先展示 |
| 运行日志 | 本地结构化日志(自动脱敏 Cookie / Token 等) |
| 设置 | 并发、每日额度、调度策略、默认模型参数、备份数据库、zhuceka UID/KEY |
| 检查更新 | 设置面板手动检查 + 自动调 GitHub Releases latest |

图生规则:**有图=图生,无图=文生**,最多 9 张图。

---

## Windows 用户:下载安装包

1. 打开 [Releases](../../releases/latest) 下载
   `DouStudio-v0.1.0-windows-x86_64.zip`
2. 解压到任意目录
3. 双击 `DouStudio.exe` 启动

安装包已包含:

- 应用本体与前端资源
- **Playwright Chromium 浏览器**(无需再单独安装浏览器)

> 首次启动若被 Windows Defender / SmartScreen 拦截,选择「仍要运行」即可(本地自建应用常见提示)。

---

## 从源码运行

### Windows

```powershell
# 1. 安装 Python 3.12+
# 2. 安装 Node.js 22+
# 3. 安装依赖
pip install -e .
cd frontend && npm ci && npm run build && cd ..

# 4. 启动
python -m doupool.main
```

### Linux / macOS

```bash
pip install -e .
cd frontend && npm ci && npm run build && cd ..
python -m doupool.main
```

---

## 打包 exe(含浏览器)

### 本地打包

```bash
python scripts/build_exe.py --mode onedir --version v0.1.0
```

产物:`dist/DouStudio-v0.1.0-windows-x86_64.zip` + `.sha256`

### GitHub Actions

```bash
git tag v0.1.0
git push origin v0.1.0
```

Actions 自动跑 Windows + Linux 矩阵,构建后自动上传到 GitHub Release。

---

## 本地数据目录

| 系统 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\DoubaoManager`(沿用上游 DoubaoManager 路径名)|
| macOS | `~/Library/Application Support/DoubaoManager` |
| Linux | `~/.local/share/DoubaoManager` |

- 每个账号独立浏览器 profile
- 不保存密码;Cookie 不入库
- 日志会脱敏敏感字段

可通过环境变量覆盖:

```text
DOUPOOL_DATA_DIR
DOUPOOL_LOG_DIR
DOUPOOL_FRONTEND_DIR
DOUPOOL_DEBUG=1
DOUSTUDIO_VERSION=0.1.0
DOUSTUDIO_GH_REPO=guo641/doustudio
```

---

## 开发验证

```bash
pip install -e ".[dev]"
PYTHONPATH=src python -m pytest tests/ -v
cd frontend && npm ci && npm test && npm run build
```

---

## 目录结构(简要)

```
DouStudio/
├── src/doupool/          # Python 后端
│   ├── api/              # FastAPI 应用
│   ├── login/            # 扫码登录 + 状态机
│   ├── video/            # 视频生成(走豆包内部 API)
│   ├── watermark/        # 去水印(zhuceka)
│   ├── prompt_reviser.py # 失败自动改 prompt 重试
│   ├── updater.py        # GitHub Releases 热更新检查
│   └── settings/         # 设置服务
├── frontend/             # Vue 3 + Vite
├── packaging/            # PyInstaller spec + entry
├── scripts/
│   └── build_exe.py      # 打包 + 上传
└── .github/workflows/
    └── release.yml       # 矩阵构建 + 自动发布
```

---

## 离线激活(v0.3.0+)

DouStudio 自 v0.3.0 起**强制要求激活码**才能进入主界面。用户首次启动会看到激活窗:

- **机器码**:64 位 HMAC,绑定本机硬件(主板 / CPU / UUID),换主板或换硬盘后变化。
- **激活码**:base32(payload).base32(signature) 一段字符串,从开发者处获取。
- **续期**:到期后激活窗只显示「退出软件」+「复制机器码」,联系开发者发新码。

### 签发激活码(开发者)

详见 [tools/license_keygen/README.md](tools/license_keygen/README.md):

```bash
# 一次性:生成密钥对 + 公钥嵌入主程序 + 编译 Cython .pyd
openssl genpkey -algorithm Ed25519 -outform PEM -out tools/license_keygen/developer_private.key
openssl pkey -in tools/license_keygen/developer_private.key -pubout -out tools/license_keygen/developer_public.key
python tools/license_keygen/scripts/embed_pubkey.py
python setup.py build_ext --inplace

# 日常:给用户签发
python scripts/build_exe.py --keygen   # 出独立 LicenseKeygen.exe
```

### 安全限制

1. 激活码 = Ed25519 签名 + HMAC fingerprint 绑定,签发者离线持私钥。
2. 私钥泄露 = 任何机器都能签码,务必像 TLS 私钥一样对待(USB 盘 + 离线备份)。
3. 反调试 + Cython .pyd 是威慑而非绝对安全 —— **目标:挡住脚本小子,不是 NSA。**
4. 硬件更换 = 重新签发,激活窗会提示用户把新机器码发给开发者。

---

## 注意事项

1. 依赖豆包网页协议,页面改版可能导致接口失效,需重新抓包适配。
2. 请仅在已授权的自有账号上使用,遵守豆包服务条款。
3. 去水印依赖第三方 zhuceka 服务,需自备 UID/KEY。
4. 打包体积较大(含 Chromium),属正常现象。

---

## License

MIT — 基于 [DoubaoManager v0.1.0](https://github.com/shukeCyp/DoubaoManager) 二次开发,致敬原作者 shukeCyp。
