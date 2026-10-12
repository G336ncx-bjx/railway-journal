"""Windows 独立窗口启动器与单文件 EXE 入口。"""
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import urllib.request

import app

def migrate_local_data():
    if not getattr(sys,'frozen',False) or '--no-migrate' in sys.argv or app.DB.exists():
        return
    exe_dir=Path(sys.executable).resolve().parent
    for root in (exe_dir,exe_dir.parent):
        old=root/'data'/'rides.sqlite3'
        if old.exists() and old.resolve()!=app.DB.resolve():
            app.DB.parent.mkdir(parents=True,exist_ok=True)
            source=sqlite3.connect(old)
            target=sqlite3.connect(app.DB)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            break
    for root in (exe_dir,exe_dir.parent,exe_dir.parent.parent):
        source=root/'乘车记录.xlsx'
        if source.exists():
            app.SOURCE=source
            break

def open_window(url):
    import webview
    webview.settings['ALLOW_DOWNLOADS']=True
    if '--debug' in sys.argv:
        webview.settings['REMOTE_DEBUGGING_PORT']=9224
        webview.settings['OPEN_DEVTOOLS_IN_DEBUG']=False
    webview.create_window('铁道手记',url,width=1280,height=860,min_size=(900,600),background_color='#f5f6f8',text_select=True)
    webview.start(gui='edgechromium',private_mode=True,storage_path=str(app.ROOT/'window-profile'),icon=str(app.ASSETS/'web'/'logo.ico'))

def existing_url():
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in range(8765,8785):
        url=f'http://127.0.0.1:{port}'
        try:
            with opener.open(url+'/api/info',timeout=.15) as response:
                info=json.loads(response.read())
            if info.get('app')=='railway-journal' and Path(info.get('data_dir','')).resolve()==app.ROOT:
                return url
        except (OSError,ValueError):
            pass
    return None

def main():
    if '--no-browser' in sys.argv:
        migrate_local_data()
        app.main()
        return
    url=existing_url()
    if url:
        import ctypes
        handle=ctypes.windll.user32.FindWindowW(None,'铁道手记')
        if handle:
            ctypes.windll.user32.ShowWindow(handle,9)
            ctypes.windll.user32.SetForegroundWindow(handle)
            return
        open_window(url)
        return
    migrate_local_data()
    app.initialize()
    for port in range(8765,8785):
        try:
            server=app.ThreadingHTTPServer(('127.0.0.1',port),app.Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('本地端口不可用，请关闭旧版本后重试')
    url=f'http://127.0.0.1:{port}'
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:
        open_window(url)
    finally:
        server.shutdown()
        server.server_close()
        worker.join()

if __name__=='__main__':
    try:
        main()
    except Exception as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None,'启动失败：'+str(error),'铁道手记',0x10)
        sys.exit(1)
