# -*- mode: python ; coding: utf-8 -*-
"""v0.3.0:PyInstaller hook for doupool.license package.

The trust root is a Cython-compiled .pyd (`_license_verify.cp312-win_amd64.pyd`).
PyInstaller's static analysis won't find it via Python imports (Cython is opaque
to the bytecode walker), so we explicitly collect the dynamic libraries and
embed them in the COLLECT output.
"""
from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("doupool.license")