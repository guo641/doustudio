"""
Cython 编译入口 —— 仅用于把 src/doupool/license/verifier.pyx 编译为
_license_verify.cp{py_version}-win_amd64.pyd(信任根)。

通过 `pip install -e .` 也能触发,这里更便于在 build_exe.py 里独立调用。

Usage:
    python setup.py build_ext --inplace

**重要**:产物文件名要跟当前 Python 解释器版本匹配(cp312 / cp313 ...)
 不能硬编码 cp312,否则 Python 3.13 加载 .pyd 会 ImportError(命名不匹配)。
"""
from __future__ import annotations

import sys
from pathlib import Path

from setuptools import Extension, setup

# 把 `python setup.py build_ext --inplace` 当 pip 之外的手动入口使用
# 时,setuptools 会按 Pyrex/Cython 风格自动 cythonize。
# 但为了能在 build_exe.py 里幂等地校验 `.pyd` 产物,我们显式调一次
# Cython.Build.cythonize,避免依赖 setuptools 内部钩子的兼容行为变化。
from Cython.Build import cythonize

ROOT = Path(__file__).resolve().parent
PYX = ROOT / "src" / "doupool" / "license" / "verifier.pyx"
OUT_DIR = ROOT / "src" / "doupool" / "license"


def _build() -> None:
    if not PYX.exists():
        raise SystemExit(f"找不到 {PYX};先按 v0.3.0 plan 创建 verifier.pyx")

    extensions = [
        Extension(
            "doupool.license._license_verify",
            sources=[str(PYX)],
            # 不再把 verifier 的 docstring / lineno / lnotab 嵌入 .pyd
            # —— 反汇编时的可读性线索能少一条是一条。
            define_macros=[("NDEBUG", "1"), ("CYTHON_FAST_THREAD_STATE", "1")],
        ),
    ]
    cythonize(
        extensions,
        language_level=3,
        # v0.3.0:见 plan §D。boundscheck/wraparound 关掉防异常路径暴露;
        # embedsignature 不写调试表;cdivision 走纯 C 除法;
        # profile 关掉避免 .c 文件留下函数名清单。
        compiler_directives={
            "embedsignature": False,
            "boundscheck": False,
            "wraparound": False,
            "cdivision": True,
            "profile": False,
        },
        # 显式传 build_dir 让产物落在 src/doupool/license/
        build_dir=str(OUT_DIR),
    )

    # 直接走 setup() 把上面的 .pyx 编成 .pyd 到 src/doupool/license/
    # 注意:这里**不**指定 --build-lib,让 setuptools 把产物放到 Out_DIR 同级
    # (即 src/doupool/license/),用 --inplace。
    setup(
        name="doupool-license-ext",
        ext_modules=extensions,
        script_args=[
            "build_ext",
            "--inplace",
            "--build-lib", str(OUT_DIR),
            "--build-temp", str(OUT_DIR / "build"),
        ],
    )


if __name__ == "__main__":
    sys.exit(_build())