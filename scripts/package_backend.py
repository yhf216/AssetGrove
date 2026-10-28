"""从项目虚拟环境运行，生成当前平台的 Tauri sidecar。"""
import pathlib
import shutil
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parents[1]
rustc = shutil.which('rustc') or str(pathlib.Path.home()/'.cargo/bin/rustc.exe')
triple = next(line.split(': ',1)[1] for line in subprocess.check_output([rustc,'-vV'],text=True).splitlines() if line.startswith('host:'))
name = 'assetgrove-backend-' + triple
# onefile 将 Python 及数据依赖打包，发行时无需额外安装 Python。
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--onefile','--name',name,
                '--distpath',str(root/'src-tauri/binaries'),'--workpath',str(root/'build/pyinstaller'),
                '--specpath',str(root/'build'),'--collect-all','baostock','--collect-all','akshare',
                '--collect-all','tzdata','--collect-all','py_mini_racer',
                '--add-data',str(root/'backend/data')+':backend/data','--hidden-import','uvicorn.logging','--hidden-import','uvicorn.loops.auto',
                '--hidden-import','uvicorn.protocols.http.auto','--hidden-import','uvicorn.protocols.websockets.auto',
                '--hidden-import','uvicorn.lifespan.on',str(root/'backend_entry.py')],cwd=root,check=True)
