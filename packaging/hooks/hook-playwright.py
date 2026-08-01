from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

hiddenimports = collect_submodules("playwright")
datas = collect_data_files("playwright")
binaries = collect_dynamic_libs("playwright")
