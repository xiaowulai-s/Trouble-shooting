# -*- mode: python ; coding: utf-8 -*-
"""QYH-GD300 上位机 v0.1 —— PyInstaller 打包配置（单文件桌面 exe）

用法（在 gd300-host 目录下，或直接用 build/build_exe.ps1）：
    python -m PyInstaller build/gd300.spec --noconfirm --clean ^
        --workpath build/pyinstaller-work --distpath build/dist

产物：build/dist/QYH-GD300上位机.exe
    · 单文件、免安装，双击即弹出原生窗口
    · 不弹控制台黑窗（console=False）
    · 运行前提：系统装有 Microsoft Edge WebView2 运行时（Win10/11 自带）
"""

import os

SRC_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# 前端静态资源必须一起打包（FastAPI StaticFiles 从 _MEIPASS/web/static 读取）
datas = [
    (os.path.join(SRC_ROOT, "web", "static"), "web/static"),
]

# uvicorn / websockets 大量使用字符串动态导入，PyInstaller 静态分析扫不到，需显式声明
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets.legacy",
    "websockets.legacy.server",
]

# 本机装有 PySide6 / OpenCV / EasyOCR 等重型库，但本项目不需要，显式排除以免误打包
excludes = [
    "PySide6", "PySide2", "PyQt5", "PyQt6",
    "tkinter", "matplotlib", "numpy", "cv2", "PIL",
    "easyocr", "torch", "torchvision", "pandas", "scipy",
    "IPython", "jupyter", "notebook", "pytest",
]

a = Analysis(
    [os.path.join(SRC_ROOT, "service", "desktop_main.py")],
    pathex=[SRC_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QYH-GD300上位机",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                    # 桌面程序，不弹黑窗
    disable_windowed_traceback=False,  # 未捕获异常时弹窗提示，而不是静默退出
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)