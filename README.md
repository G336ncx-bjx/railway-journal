# 铁道手记 · Railway Journal

<img src="web/logo.png" alt="铁道手记 Logo" width="140">

用表单和可视化统计维护个人乘车记录。支持跨日历时、均价和平均旅速自动计算，以及年度统计、车站足迹、票据查看和数据备份。

## 下载使用

在 [Releases](https://github.com/G336ncx-bjx/railway-journal/releases/latest) 下载 `RailwayJournal.exe`，双击即可启动。无需安装 Python，无需手动解压。

支持 Windows 10/11 64 位，使用内嵌 WebView2 的独立软件窗口，不打开完整浏览器，也不使用浏览器账号。关闭软件窗口后，本地服务停止。需要 Microsoft Edge WebView2 Runtime；Windows 11 和多数 Windows 10 设备已安装。如果启动时提示缺少它，可安装微软提供的 [WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/)。

可执行版本把记录保存在 `%LOCALAPPDATA%\RailwayJournal\data`，修改前的自动备份位于其 `backups` 子目录。更新或移动 EXE 不会清空数据。程序首次运行时，会尝试读取 EXE 所在目录或上一级目录中旧版程序的 `data/rides.sqlite3`，原数据保持不变。也可以在「数据与备份」中恢复完整 JSON 备份。

本仓库和发布包只包含程序，不包含个人记录、票据或真实数据截图。票据文件不嵌入 JSON 备份，换电脑时请一起迁移票据文件并检查路径。

## 功能

- 新增、编辑、删除、搜索和筛选乘车记录。
- 根据同车次最近记录填入尚为空的字段；保存前可调整时刻、区间和票价。
- 到点支持 `14:25+1`、`14:25+2` 等跨日格式。
- 自动汇总里程、票价、图定历时及实际乘降车站。
- 导出 Excel 可打开的 CSV，备份和恢复完整 JSON。
- 使用清华云盘资料库 API 令牌保存档案，支持自动同步、云端读取、历史备份及双端冲突提示。
- 兼容原乘车记录工作簿的固定字段布局。未发现工作簿时从空档案开始。

## 运行源码

需要 Python 3.11 或更高版本。

```powershell
python -m pip install -r requirements.txt
python desktop.py
```

源码版本默认使用项目目录下的 `data`。仅启动本地服务、不打开界面时，不需要第三方 Python 包：

```powershell
python app.py --no-browser
```

程序仅监听 `127.0.0.1`。启用云盘后，通过 HTTPS 同步到自己指定的资料库。默认端口 8765；被占用时自动选择后续端口。环境变量 `RAILWAY_JOURNAL_DATA_DIR` 可指定独立的数据目录。

## 清华云盘

打开「数据与云盘」，填写资料库的 **读写 API token**，点击「保存并连接」。默认服务器是 `https://cloud.tsinghua.edu.cn`，默认目录为 `/铁道手记`。也可以填写自己的 Seafile HTTPS 服务器。

云盘目录中的 `records.json` 是完整的乘车记录和车站地区档案；`历史备份` 目录保留上传版本和被替换的云端档案。JSON 包含票据的本机路径，票据图片文件需要另行迁移。API token 不包含在 JSON 备份中，使用 Windows DPAPI 加密保存为本机用户专用凭据；换电脑后需要重新填写令牌。

开启自动同步后，修改记录会触发同步，程序运行期间也会检查云端变更。离线时本机保存照常可用，重连后重试。新电脑的空档案可以自动读取已有云端记录；已有不同档案时，需要选择「以本机数据更新云端」或「读取云端数据」，替换前分别保留云端历史版本或本机备份。

双端都修改过会提示冲突，自动同步暂停，等待用户选择。同步以完整档案为单位，不做逐条合并；不适用于多人同时编辑同一档案。程序仅操作指定的铁道手记目录。断开连接只删除本机云盘凭据，不删除任何乘车记录或云端文件。

接入参考了云雀播放器的资料库令牌模式及 [Seafile Repo-Token API](https://seafile-api.readme.io/reference/get_api-v2-1-via-repo-token-repo-info)。

## 构建与验证

在 Windows 上执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
python -m unittest discover -s tests -v
```

生成 `dist/RailwayJournal.exe`。打包使用 [PyInstaller](https://pyinstaller.org/en/stable/usage.html)，网页资源随 EXE 一起打包。测试使用临时数据库，不修改真实记录。

EXE 当前没有代码签名；Windows 下载保护可能显示发布者未知。
