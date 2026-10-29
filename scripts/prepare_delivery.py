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
新增买入自动扣除成交金额与费用，新增卖出自动增加扣除费用后的成交金额。
旧版交易保留原来的现金处理方式；初始持仓不重复扣减现金，余额核对仍可手动录入。
手动补记分红默认仅计入统计，勾选“同时增加证券现金”才记录现金到账。
自动生成的待确认分红，确认后计入证券现金。

更新前可在“数据与设置”中导出完整 JSON 备份。
JSON 备份和 CSV 导出均弹出系统“另存为”窗口，由你选择保存位置。
账户支持修改性质、拖动排序，也可在设置中按类型或金额排序。
证券持仓支持沪深 A 股与 ETF（例如 518880）。ETF 价格保留三位小数。
关注条件及自选股息率默认采用近 12 个月 TTM；分红预测独立选择 1／3／5 年，默认 3 年。
关注条件可直接搜索未持有的标的，显示当前值、目标值及价差，支持编辑和拖动排序。
每次打开软件、从托盘恢复或重新启动时，按最近有效行情重新提醒已达标的关注条件；后台轮询不会反复提醒同一次持续达标。
提醒通过 Windows 通知中心发送，启动时补齐本应用的通知标识，发送后核实通知中心收到消息；设置中可检查状态和发送测试通知。
ETF 的报价、历史行情及分红均有备用源，主源失败时自动切换，未知的登记日与到账日保持为空。
收息统计按每笔分红的股权登记日还原持股数，并汇总已经到派息日的分红；已有到账记录优先，回溯项显示税前标记，不改变账户余额。
市场模块使用独立的 market.sqlite3 缓存，个人账本导入、导出和目录迁移不修改市场数据。
默认显示上证指数日 K，可多选常用指数，并选择日／周／月 K、收盘折线、均线及成交量。
均线默认关闭，预设 M10／M20／M50／M250，周期按交易日计算，周月 K 仍沿用日均线。
图表可拖动平移、按住 Ctrl 滚轮缩放，也可拖动底部时间滑块；多指数对比可使用共同起点归一化。
初次加载指数会获取数据源可提供的全部历史；较新指数仅显示实际存在的历史，后续以增量为主更新。
市场页面切换只读取缓存；每天北京时间 18:00 后自动更新一次，错过时在下次启动后补齐，也可手动更新。
TRIN 后台限速补齐历史，先恢复近期曲线，再逐年向前扩展。批次即时保存，退出后下次自动继续；可取消“后台补齐 TRIN 历史”暂停。
历史完成状态按实际数据行核对，空响应会保留缺口；失败请求延后重试，等待时间逐步增加。只有完整样本生成正式 TRIN 数值。
回补按股票和日期复用已保存的数据，只下载缺失区间；已完整缓存的历史可直接离线补算。
本地补算与联网任务独立运行，遵循后台回补开关。待补算日期持久保存，输入变化时只更新相关日期。
图表按数据版本缓存日／周／月聚合结果，数据未变化时不重复读取整段历史。
TRIN 的盘中值单独标记，沪深 A 股全市场口径可与各指数叠加对比。
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
