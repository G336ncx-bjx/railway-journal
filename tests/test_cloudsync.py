from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import cloudsync

def document(value=1):
    return {'version':1,'records':[{'id':7,'value':value}],'stations':{'示例车站':'示例地区'}}

class Remote:
    def __init__(self, initial=None):
        self.current=deepcopy(initial)
        self.history=[]
        self.offline=False

    def repo_info(self):
        return {'repo_name':'测试资料库','permission':'rw'}

    def ensure_dir(self,path):
        if self.offline:
            raise cloudsync.CloudError('测试网络断开')

    def read_snapshot(self,path):
        if self.current is None:
            raise FileNotFoundError()
        return deepcopy(self.current)

    def current_id(self,folder):
        return cloudsync.fingerprint(self.current) if self.current else None

    def upload_snapshot(self,folder,filename,data):
        if filename=='records.json':
            self.current=deepcopy(data)
        else:
            self.history.append(deepcopy(data))

class CloudTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.root=Path(self.directory.name)
        self.local=document()
        self.backups=[]
        self.remote=Remote()
        self.config={'server':cloudsync.DEFAULT_SERVER,'folder':'/测试档案','token':'a'*40,'auto':True}
        self.reader=patch.object(cloudsync,'read_config',lambda root:deepcopy(self.config))
        self.reader.start()
        def restore(data):
            self.backups.append(deepcopy(self.local))
            self.local={'version':1,'records':deepcopy(data['records']),'stations':deepcopy(data['stations'])}
        self.manager=cloudsync.CloudSync(lambda:self.root,lambda:deepcopy(self.local),restore,threading.RLock(),lambda config:self.remote)

    def tearDown(self):
        self.manager.stop()
        self.reader.stop()
        self.directory.cleanup()

    def test_first_upload_and_unchanged(self):
        result=self.manager.sync()
        self.assertEqual(result['phase'],'synced')
        self.assertEqual(self.remote.current['records'],self.local['records'])
        self.assertEqual(len(self.remote.history),1)
        self.assertNotIn('token',json.dumps(result))
        self.assertEqual(self.manager.sync()['last_action'],'unchanged')
        self.assertEqual(len(self.remote.history),1)

    def test_offline_retry_keeps_local_changes(self):
        self.manager.sync()
        self.local['records'][0]['value']=2
        self.remote.offline=True
        self.assertEqual(self.manager.sync()['phase'],'error')
        self.assertEqual(self.local['records'][0]['value'],2)
        self.assertEqual(self.remote.current['records'][0]['value'],1)
        self.remote.offline=False
        self.assertEqual(self.manager.sync()['last_action'],'upload')
        self.assertEqual(self.remote.current['records'][0]['value'],2)
        self.assertTrue(any(d['records'][0]['value']==1 for d in self.remote.history))

    def test_unchanged_version_skips_full_download(self):
        self.manager.sync()
        with patch.object(self.remote,'read_snapshot',side_effect=AssertionError('不应再次下载档案')):
            result=self.manager.sync()
        self.assertEqual(result['phase'],'synced')
        self.assertEqual(result['last_action'],'unchanged')

    def test_remote_changes_are_read_and_locally_backed_up(self):
        self.manager.sync()
        self.remote.current=document(3)
        self.assertEqual(self.manager.sync()['last_action'],'download')
        self.assertEqual(self.local['records'][0]['value'],3)
        self.assertEqual(self.local['records'][0]['id'],7)
        self.assertEqual(self.backups[0]['records'][0]['value'],1)
        self.assertEqual(self.manager.sync()['last_action'],'unchanged')

    def test_conflict_requires_choice_and_keeps_both(self):
        self.manager.sync()
        self.local['records'][0]['value']=2
        self.remote.current=document(3)
        history_count=len(self.remote.history)
        self.assertEqual(self.manager.sync()['phase'],'conflict')
        self.assertEqual(self.local['records'][0]['value'],2)
        self.assertEqual(self.remote.current['records'][0]['value'],3)
        self.assertEqual(len(self.remote.history),history_count)
        self.assertEqual(self.manager.sync('upload')['last_action'],'upload')
        self.assertTrue(any(d['records'][0]['value']==3 for d in self.remote.history))

    def test_new_device_reads_cloud(self):
        self.local={'version':1,'records':[],'stations':{}}
        self.remote.current=document(4)
        self.assertEqual(self.manager.sync()['last_action'],'download')
        self.assertEqual(self.local['records'][0]['value'],4)

    def test_unrecognized_existing_data_is_not_overwritten(self):
        self.remote.current=document(8)
        self.assertEqual(self.manager.sync()['phase'],'conflict')
        self.assertEqual(self.remote.current['records'][0]['value'],8)

    def test_server_change_requires_explicit_token(self):
        with self.assertRaises(cloudsync.CloudError):
            self.manager.configure({'server':'https://example.org','folder':'/测试档案','auto':True})

    def test_change_during_download_stops_restore(self):
        self.manager.sync()
        self.remote.current=document(3)
        original=self.remote.read_snapshot
        def read(path):
            result=original(path)
            self.local['records'][0]['value']=2
            return result
        # 在网络读取之后、本机快照之前的修改应纳入双端冲突判断。
        self.remote.read_snapshot=read
        self.assertEqual(self.manager.sync()['phase'],'conflict')
        self.assertEqual(self.local['records'][0]['value'],2)

class CredentialTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows DPAPI')
    def test_dpapi_round_trip_and_no_plaintext(self):
        value=json.dumps({'token':'a'*40}).encode()
        encrypted=cloudsync.protect(value)
        self.assertNotIn(b'a'*40,encrypted)
        self.assertEqual(cloudsync.protect(encrypted,decrypt=True),value)

    def test_signed_links_never_receive_library_token(self):
        client=cloudsync.SeafileClient({'server':cloudsync.DEFAULT_SERVER,'token':'a'*40})
        with patch.object(client,'api',return_value=cloudsync.DEFAULT_SERVER+'/seafhttp/download/test'),patch.object(client,'request',return_value=json.dumps(document()).encode()) as request:
            client.read_snapshot('/测试档案/records.json')
            self.assertFalse(request.call_args.kwargs['authenticated'])

    def test_cross_host_transfer_link_is_rejected(self):
        client=cloudsync.SeafileClient({'server':cloudsync.DEFAULT_SERVER,'token':'a'*40})
        with self.assertRaises(cloudsync.CloudError):
            client.request('https://example.org/upload',authenticated=False)
