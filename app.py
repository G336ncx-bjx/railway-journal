"""本地乘车记录程序，仅使用 Python 标准库。"""
import csv
import io
import json
import math
import os
import re
import sqlite3
import sys
import threading
import webbrowser
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ASSETS = Path(__file__).resolve().parent
FROZEN = getattr(sys, 'frozen', False)
ROOT = Path(os.environ.get('RAILWAY_JOURNAL_DATA_DIR') or (str(Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'RailwayJournal') if FROZEN else str(ASSETS))).resolve()
DB = ROOT / 'data' / 'rides.sqlite3'
SOURCE = ROOT.parent / '乘车记录.xlsx'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
FIELDS = ['date', 'train', 'type', 'bureau', 'operator', 'departure', 'depart_time', 'arrival', 'arrive_time', 'origin', 'destination', 'seat_type', 'seat', 'price', 'distance', 'receipt', 'receipt_path', 'notes']
LABELS = ['日期', '车次', '列车类型', '路局', '担当局段', '出发站', '开点', '到达站', '到点', '始发站', '终到站', '席别', '席位', '价格', '里程', '车票/报销凭证', '票据路径', '备注']

def duration(start, end):
    if not start or not end:
        return None
    a = re.fullmatch(r'(\d{2}):(\d{2})', start)
    b = re.fullmatch(r'(\d{2}):(\d{2})(?:\+(\d+))?', end)
    if not a or not b:
        raise ValueError('时间格式应为 HH:MM，到点跨日请写 HH:MM+1 或 +2')
    h, m = map(int, a.groups())
    eh, em = map(int, b.groups()[:2])
    days = int(b.group(3) or 0)
    if h > 23 or eh > 23 or m > 59 or em > 59 or days > 30:
        raise ValueError('时间超出有效范围')
    minutes = days * 1440 + eh * 60 + em - h * 60 - m
    if minutes < 0:
        raise ValueError('到点早于开点，跨日请在到点后填写 +1')
    return minutes

def normalize(raw):
    r = {k: str(raw.get(k, '') if raw.get(k) is not None else '').strip() for k in FIELDS}
    r['date'] = r['date'].replace('.', '-').replace('/', '-')
    try:
        r['date'] = date.fromisoformat(r['date']).isoformat()
    except ValueError:
        raise ValueError('请填写有效乘车日期')
    for k in ('train', 'departure', 'arrival'):
        if not r[k]:
            raise ValueError('日期、车次、出发站、到达站为必填项')
    r['train'] = r['train'].upper()
    for k in ('price', 'distance'):
        if r[k] == '':
            r[k] = None
        else:
            try:
                r[k] = float(r[k])
            except ValueError:
                raise ValueError('票价和里程应为数字')
            if not math.isfinite(r[k]) or r[k] < 0:
                raise ValueError('票价和里程应为非负有限数字')
    if r['depart_time']:
        duration(r['depart_time'], r['depart_time'])
    if r['arrive_time']:
        duration('00:00', r['arrive_time'])
    duration(r['depart_time'], r['arrive_time'])
    return r

def enriched(r):
    r = dict(r)
    minutes = duration(r['depart_time'], r['arrive_time'])
    r['minutes'] = minutes
    r['unit_price'] = r['price'] / r['distance'] if r['price'] is not None and r['distance'] else None
    r['speed'] = r['distance'] * 60 / minutes if r['distance'] is not None and minutes else None
    return r

def read_excel(path):
    with zipfile.ZipFile(path) as z:
        ss = [''.join(x.itertext()) for x in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si', NS)] if 'xl/sharedStrings.xml' in z.namelist() else []
        def rows(sheet):
            result = []
            for row in ET.fromstring(z.read(f'xl/worksheets/sheet{sheet}.xml')).findall('s:sheetData/s:row', NS):
                cells = {}
                for c in row:
                    v = c.find('s:v', NS)
                    val = v.text if v is not None else ''
                    if c.get('t') == 's':
                        val = ss[int(val)]
                    elif c.get('t') == 'inlineStr':
                        val = ''.join(c.find('s:is', NS).itertext())
                    cells[re.sub(r'\d', '', c.get('r'))] = val
                result.append(cells)
            return result
        raw = rows(1)
        if raw[0].get('B') != '日期' or raw[0].get('C') != '车次':
            raise ValueError('该工作簿不是预期的乘车记录格式')
        links = {}
        relfile = 'xl/worksheets/_rels/sheet1.xml.rels'
        if relfile in z.namelist():
            rels = {r.get('Id'): r.get('Target') for r in ET.fromstring(z.read(relfile))}
            for h in ET.fromstring(z.read('xl/worksheets/sheet1.xml')).findall('s:hyperlinks/s:hyperlink', NS):
                target = rels.get(h.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'), '')
                if target:
                    links[h.get('ref')] = str((Path(path).parent / unquote(target).replace('\\', '/')).resolve())
        mapping = dict(zip(FIELDS[:15], ['B','C','D','E','F','G','H','I','J','L','M','N','O','P','Q']))
        records = []
        for i, row in enumerate(raw[1:], 2):
            if not row.get('C'):
                continue
            r = {k: row.get(col, '') for k, col in mapping.items()}
            for key in ('depart_time', 'arrive_time'):
                if re.fullmatch(r'\d+(\.\d+)?', r[key]):
                    total = round(float(r[key]) * 1440)
                    days, minute = divmod(total, 1440)
                    r[key] = f'{minute // 60:02d}:{minute % 60:02d}' + (f'+{days}' if days and key == 'arrive_time' else '')
            if re.fullmatch(r'\d+(\.\d+)?', r['date']):
                r['date'] = (date(1899, 12, 30) + timedelta(days=float(r['date']))).isoformat()
            r.update(receipt=row.get('T', ''), receipt_path=links.get(f'T{i}', ''))
            records.append(normalize(r))
        stations = {r['B']: r.get('D', '') for r in rows(4)[1:] if r.get('B')}
        return records, stations

@contextmanager
def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()

def insert(c, r):
    return c.execute(f"INSERT INTO rides ({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})", [r[k] for k in FIELDS]).lastrowid

def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS rides (id INTEGER PRIMARY KEY AUTOINCREMENT, ' + ','.join(k + (' REAL' if k in ('price','distance') else ' TEXT') for k in FIELDS) + ')')
        c.execute('CREATE TABLE IF NOT EXISTS stations (name TEXT PRIMARY KEY, location TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        if not c.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
            if SOURCE.exists():
                records, stations = read_excel(SOURCE)
                for r in records:
                    insert(c, r)
                c.executemany('INSERT INTO stations VALUES (?,?)', stations.items())
            c.execute("INSERT INTO settings VALUES ('initialized','1')")

def snapshot():
    with connect() as c:
        return {'version': 1, 'records': [dict(r) for r in c.execute('SELECT * FROM rides ORDER BY date DESC,id DESC')], 'stations': {r['name']:r['location'] for r in c.execute('SELECT * FROM stations')}}

def backup():
    folder = ROOT / 'data' / 'backups'
    folder.mkdir(exist_ok=True)
    (folder / (datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')).write_text(json.dumps(snapshot(), ensure_ascii=False, indent=2), encoding='utf-8')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if sys.stderr is not None:
            super().log_message(format, *args)

    def send(self, body, content='application/json; charset=utf-8', status=200, filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/info':
            return self.send({'app':'railway-journal','version':'1.0.1','data_dir':str(ROOT)})
        if path == '/api/records':
            data = snapshot()
            data['records'] = [enriched(r) for r in data['records']]
            for r in data['records']:
                r['receipt_exists'] = bool(r['receipt_path']) and Path(r['receipt_path']).is_file()
            return self.send(data)
        if path == '/api/backup':
            return self.send(json.dumps(snapshot(), ensure_ascii=False, indent=2).encode('utf-8'), filename='railway-backup.json')
        if path == '/api/export':
            out = io.StringIO(newline='')
            w = csv.writer(out)
            w.writerow(LABELS + ['图定历时(分钟)', '均价(元/km)', '平均旅速(km/h)'])
            for r in snapshot()['records']:
                r = enriched(r)
                vals = [r[k] for k in FIELDS] + [r['minutes'],r['unit_price'],r['speed']]
                w.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in vals])
            return self.send(out.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', filename='railway-records.csv')
        if path.startswith('/api/receipt/'):
            try:
                rid = int(path.rsplit('/',1)[1])
                with connect() as c:
                    r = c.execute('SELECT receipt_path FROM rides WHERE id=?',(rid,)).fetchone()
                if not r or not r[0]:
                    raise ValueError('未设置票据路径')
                p = Path(r[0]).resolve()
                if not p.is_file() or p.suffix.lower() not in ('.png','.jpg','.jpeg','.pdf','.webp'):
                    raise ValueError('票据不存在或文件格式不支持')
                import mimetypes
                return self.send(p.read_bytes(), mimetypes.guess_type(str(p))[0] or 'application/octet-stream')
            except (ValueError, OSError) as e:
                return self.send({'error':str(e)}, status=404)
        files = {'/':'index.html', '/app.js':'app.js', '/style.css':'style.css', '/logo.png':'logo.png', '/favicon.ico':'logo.ico'}
        if path in files:
            import mimetypes
            p = ASSETS / 'web' / files[path]
            content = mimetypes.guess_type(str(p))[0] or 'application/octet-stream'
            if p.suffix in ('.html', '.js', '.css'):
                content += '; charset=utf-8'
            return self.send(p.read_bytes(), content)
        self.send({'error':'未找到'}, status=404)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.send({'error':'请求来源不匹配'},status=403)
            size = int(self.headers.get('Content-Length',0))
            if size > 10_000_000:
                raise ValueError('导入文件不得超过 10 MB')
            data = json.loads(self.rfile.read(size))
            path = urlparse(self.path).path
            if path == '/api/save':
                r = normalize(data)
                rid = data.get('id')
                backup()
                with connect() as c:
                    if rid:
                        result = c.execute(f"UPDATE rides SET {','.join(k+'=?' for k in FIELDS)} WHERE id=?",[r[k] for k in FIELDS]+[int(rid)])
                        if not result.rowcount:
                            raise ValueError('记录不存在')
                    else:
                        rid = insert(c,r)
                return self.send({'id':rid})
            if path == '/api/delete':
                backup()
                with connect() as c:
                    c.execute('DELETE FROM rides WHERE id=?',(int(data['id']),))
                return self.send({'ok':True})
            if path == '/api/restore':
                if data.get('version') != 1 or not isinstance(data.get('records'),list) or not isinstance(data.get('stations'),dict):
                    raise ValueError('请选择本程序导出的 JSON 备份')
                records = [normalize(r) for r in data['records']]
                stations = {str(k):str(v) for k,v in data['stations'].items()}
                backup()
                with connect() as c:
                    c.execute('DELETE FROM rides')
                    c.execute('DELETE FROM stations')
                    for r in records:
                        insert(c,r)
                    c.executemany('INSERT INTO stations VALUES (?,?)',stations.items())
                return self.send({'count':len(records)})
            return self.send({'error':'未找到'},status=404)
        except (ValueError, KeyError, TypeError, sqlite3.Error) as e:
            self.send({'error':str(e)}, status=400)

def main():
    initialize()
    for port in range(8765,8785):
        try:
            server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('没有可用的本地端口')
    url = f'http://127.0.0.1:{port}'
    if sys.stdout is not None:
        print('乘车记录已启动：'+url+'\n关闭此窗口即可停止程序。',flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(.7,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
