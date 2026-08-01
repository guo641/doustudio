from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

hiddenimports = collect_submodules("webview")
datas = collect_data_files("webview")
binaries = collect_dynamic_libs("webview")
