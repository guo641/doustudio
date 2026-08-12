"""
v0.3.0:import-time 闸门。

`main.py` 第 10 行前加 `import doupool.license.verify_at_import  # noqa: F401`,
触发本模块执行 → 调 doupool.license.ensure_activated_or_exit() → 跑
verifier.import-time side-effect。

为什么必须有这个独立模块(而不是直接在 __init__.py 跑):
  - __init__.py import-time 调用 ensure_activated_or_exit() 也行,但
    doupool.license.__init__ 已经被 api/app.py / main.py 之外的模块
    (测试、签发工具)以"作为模块查看 API"加载,那几种 import 不应触发
    闸门 —— 开发者自己跑 `python -c "import doupool.license; print(...)"`
    不应该被踢。
  - 把闸门放到独立模块 require 显式 import,谁想要这行为谁 import。
    main.py 是唯一入口,因此唯一在该处 import verify_at_import。

行为:
  - missing  → 直接通过(主 UI 渲染激活窗引导用户)
  - valid    → 通过
  - expired  → sys.exit(0),无 UI,无 log,无声触发(见 plan §H.7)
  - uncompiled → 通过(开发机 / keygen 工具会命中此分支)
"""
from __future__ import annotations

import doupool.license as _license

_license.ensure_activated_or_exit()
