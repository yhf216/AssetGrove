"""将本机已构建的 Windows 产物整理为安装版和免安装压缩包。"""
import hashlib
import json
import pathlib
import shutil
import zipfile

root=pathlib.Path(__file__).resolve().parents[1]
version=json.loads((root/'src-tauri/tauri.conf.json').read_text(encoding='utf-8'))['version']
target=root/'src-tauri/target/release'
installer=target/'bundle/nsis'/f'AssetGrove_{version}_x64-setup.exe'
app=target/'assetgrove.exe'
backend=root/'src-tauri/binaries/assetgrove-backend-x86_64-pc-windows-msvc.exe'
for item in [installer,app,backend]:
    if not item.is_file():
        raise SystemExit('缺少构建产物：'+str(item))
release=root/'releases'
release.mkdir(exist_ok=True)
setup=release/f'AssetGrove-{version}-Windows-x64-setup.exe'
shutil.copy2(installer,setup)
instructions='''AssetGrove 使用说明

推荐使用安装版：双击 setup.exe，安装完成后从桌面或开始菜单打开 AssetGrove。
无需安装 Python、Node.js 或 Rust，无需执行命令。

免安装版：完整解压 ZIP 至一个文件夹，然后双击 AssetGrove.exe。
请保留同文件夹内的 assetgrove-backend.exe，不要只取出主程序。
依赖 Windows 10/11 的 WebView2；安装版在运行库缺失时会协助安装。

窗口边缘可拖动调整大小，标题栏可最大化；内容和弹窗支持滚轮上下滑动。
点击窗口右上角关闭按钮会隐藏至系统托盘，后台监控继续运行。
托盘菜单可重新打开窗口，也可选择完全退出；设置页面也有完全退出按钮。

应用强制直连，不使用系统或环境代理，不修改系统代理设置。
数据源是否可用以同步状态为准；过期或无法核实时间的报价不会触发提醒。

个人账本默认保存在安装目录下的 data 文件夹，设置保存在安装目录的 settings.json。
可在“数据与设置”选择文件夹并迁移账本；迁移会保留原文件，不覆盖目标已有账本。
字号支持 14–24，修改即时生效。外观支持浅色、深色、跟随系统。

资产录入填写记录后的余额；股票买卖填写变动股数、日期、成交价格和费用。
股票买卖不自动改变证券现金，请在资产模块单独更新证券现金余额。
手动补记分红默认仅计入统计，勾选“同时增加证券现金”才记录现金到账。
自动生成的待确认分红，确认后计入证券现金。

更新前可在“数据与设置”中导出完整 JSON 备份。
卸载：在 Windows“已安装的应用”中卸载 AssetGrove，或运行安装目录中的 uninstall.exe。
默认保留账本；勾选卸载窗口的“删除应用数据”后，同时删除默认目录中的个人账本和应用缓存。
升级不会删除账本。自行导出的备份、手动指定的数据目录不会自动删除。
共享的 WebView2 运行库会保留，供其他应用使用。
'''
(release/'使用说明.txt').write_text(instructions,encoding='utf-8')
portable=release/f'AssetGrove-{version}-Windows-x64-portable.zip'
with zipfile.ZipFile(portable,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
    archive.write(app,'AssetGrove/AssetGrove.exe')
    archive.write(backend,'AssetGrove/assetgrove-backend.exe')
    archive.writestr('AssetGrove/使用说明.txt',instructions.encode('utf-8'))
hashes=[]
for item in [setup,portable]:
    hashes.append(hashlib.sha256(item.read_bytes()).hexdigest()+'  '+item.name)
    print(item.name,round(item.stat().st_size/1024/1024,2),'MiB')
(release/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='utf-8')
