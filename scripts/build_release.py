"""开发者的一键 Windows 封装入口；使用者只需下载安装版。"""
import os
import pathlib
import shutil
import subprocess
import sys

root=pathlib.Path(__file__).resolve().parents[1]
env=dict(os.environ)
cargo=pathlib.Path.home()/'.cargo/bin'
env['PATH']=str(cargo)+os.pathsep+env.get('PATH','')
npm=shutil.which('npm.cmd')
if not npm:
    raise SystemExit('构建机器需要安装 Node.js / npm')
for command in [[sys.executable,'scripts/package_backend.py'],[npm,'run','desktop:build','--','--bundles','nsis'],[sys.executable,'scripts/prepare_delivery.py']]:
    subprocess.run(command,cwd=root,env=env,check=True)
