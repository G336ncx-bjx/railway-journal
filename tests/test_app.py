import importlib.util
import json
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP_PATH = Path(__file__).resolve().parents[1] / 'app.py'
spec = importlib.util.spec_from_file_location('railway', APP_PATH)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

class RailwayTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(app.duration('23:59','14:25+1'),866)
        self.assertEqual(app.duration('11:02','12:23+1'),1521)
        self.assertEqual(app.duration('23:00','01:00+2'),1560)
        self.assertIsNone(app.duration('', '12:00'))
        for start,end in [('12:00','11:00'),('24:00','25:00'),('09:99','10:00')]:
            with self.assertRaises(ValueError):
                app.duration(start,end)

    def test_excel_import_with_numeric_times(self):
        namespace=app.NS['s']
        def sheet(rows):
            root=ET.Element('worksheet',xmlns=namespace)
            data=ET.SubElement(root,'sheetData')
            for i,values in enumerate(rows,1):
                row=ET.SubElement(data,'row',r=str(i))
                for col,value in values.items():
                    cell=ET.SubElement(row,'c',r=f'{col}{i}')
                    if isinstance(value,(float,int)):
                        ET.SubElement(cell,'v').text=str(value)
                    else:
                        cell.set('t','inlineStr')
                        ET.SubElement(ET.SubElement(cell,'is'),'t').text=value
            return ET.tostring(root,encoding='utf-8')
        with tempfile.TemporaryDirectory(dir=app.ROOT) as directory:
            filename=Path(directory)/'sample.xlsx'
            with zipfile.ZipFile(filename,'w') as workbook:
                workbook.writestr('xl/worksheets/sheet1.xml',sheet([
                    {'B':'日期','C':'车次'},
                    {'B':'2026.01.01','C':'G100','G':'示例出发站','H':.5,'I':'示例到达站','J':'13:30','P':50,'Q':120}
                ]))
                workbook.writestr('xl/worksheets/sheet4.xml',sheet([{'B':'车站','D':'地区'},{'B':'示例出发站','D':'示例地区'}]))
            rows,stations=app.read_excel(filename)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['depart_time'],'12:00')
            self.assertEqual(app.enriched(rows[0])['minutes'],90)
            self.assertEqual(rows[0]['price'],50)
            self.assertEqual(stations,{'示例出发站':'示例地区'})

    def test_crud_restore_validation(self):
        original_root,original_db,original_source=app.ROOT,app.DB,app.SOURCE
        with tempfile.TemporaryDirectory(dir=original_root) as folder:
            app.ROOT=Path(folder)
            app.DB=app.ROOT/'data'/'rides.sqlite3'
            app.SOURCE=app.ROOT/'missing.xlsx'
            app.initialize()
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            def request(path,data=None):
                req=Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
                with urlopen(req) as res:
                    return json.loads(res.read())
            try:
                row={'date':'2025-01-01','train':'T100','departure':'示例出发站','arrival':'示例到达站','depart_time':'23:59','arrive_time':'14:25+1','distance':1087,'price':0}
                rid=request('/api/save',row)['id']
                saved=request('/api/records')['records'][0]
                self.assertEqual(saved['minutes'],866)
                self.assertEqual(saved['unit_price'],0)
                self.assertAlmostEqual(saved['speed'],1087*60/866)
                row.update(id=rid,price=138.5)
                request('/api/save',row)
                saved=request('/api/records')['records'][0]
                self.assertEqual(saved['price'],138.5)
                data=request('/api/backup')
                with self.assertRaises(HTTPError) as invalid:
                    request('/api/save',{**row,'distance':'NaN'})
                invalid.exception.close()
                self.assertEqual(len(request('/api/records')['records']),1)
                request('/api/delete',{'id':rid})
                self.assertEqual(request('/api/records')['records'],[])
                request('/api/restore',data)
                self.assertEqual(request('/api/records')['records'][0]['price'],138.5)
                with self.assertRaises(HTTPError) as invalid:
                    request('/api/restore',{'version':1,'records':[{}],'stations':{}})
                invalid.exception.close()
                self.assertEqual(len(request('/api/records')['records']),1)
                self.assertEqual(len(list((app.ROOT/'data'/'backups').glob('*.json'))),4)
                with urlopen(base+'/api/export') as res:
                    export=res.read().decode('utf-8-sig')
                    self.assertIn('示例出发站',export)
                    self.assertIn('138.5',export)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.ROOT,app.DB,app.SOURCE=original_root,original_db,original_source

if __name__=='__main__':
    unittest.main()
