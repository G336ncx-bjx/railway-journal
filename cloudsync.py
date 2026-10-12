"""Seafile 资料库令牌存储与乘车档案同步。凭据仅保存为 Windows DPAPI 密文。"""
import ctypes
import hashlib
import json
import os
import posixpath
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

DEFAULT_SERVER = 'https://cloud.tsinghua.edu.cn'
DEFAULT_FOLDER = '/铁道手记'
MAX_FILE_SIZE = 10_000_000

class CloudError(Exception):
    pass

class ConflictError(CloudError):
    pass

def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

def protect(data, decrypt=False):
    if os.name != 'nt':
        raise CloudError('云盘凭据存储需要 Windows 用户加密服务')
    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    entropy_buffer = ctypes.create_string_buffer(b'RailwayJournal.Cloud.v1')
    entropy = Blob(23, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    ok = operation(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
    if not ok:
        raise CloudError('无法解密本机云盘凭据，请在当前 Windows 用户下重新连接')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)

def read_config(root):
    path = Path(root) / 'data' / 'cloud-secrets.bin'
    if not path.exists():
        return None
    return json.loads(protect(path.read_bytes(), decrypt=True))

def write_config(root, config):
    atomic_write(Path(root) / 'data' / 'cloud-secrets.bin', protect(json.dumps(config, ensure_ascii=False).encode()))

def fingerprint(snapshot):
    content = {'version':1, 'records':sorted(snapshot['records'], key=lambda r:r['id']), 'stations':snapshot['stations']}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        previous, target = urlparse(req.full_url), urlparse(newurl)
        if target.scheme != 'https' or target.netloc != previous.netloc:
            raise CloudError('云盘请求跳转到其他服务器，已停止连接')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class SeafileClient:
    def __init__(self, config):
        self.server = config['server'].rstrip('/')
        self.token = config['token']
        self.opener = build_opener(SafeRedirect())

    def request(self, url, method='GET', body=None, content_type=None, authenticated=True):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != urlparse(self.server).netloc:
            raise CloudError('云盘返回的文件地址不是同一 HTTPS 服务器')
        headers = {'Accept':'application/json', 'User-Agent':'RailwayJournal/1.1.0'}
        if authenticated:
            headers['Authorization'] = 'Token ' + self.token
        if content_type:
            headers['Content-Type'] = content_type
        try:
            with self.opener.open(Request(url, data=body, headers=headers, method=method), timeout=25) as response:
                data = response.read(MAX_FILE_SIZE + 1)
            if len(data) > MAX_FILE_SIZE:
                raise CloudError('云端档案超过 10 MB，无法读取')
            return data
        except HTTPError as error:
            code = error.code
            error.close()
            if code == 404:
                raise FileNotFoundError('云端文件或目录不存在') from None
            if code in (401,403):
                raise CloudError('资料库令牌失效或权限不足，请检查读写权限') from None
            raise CloudError(f'云盘请求失败（HTTP {code}）') from None
        except (URLError, TimeoutError, OSError) as error:
            raise CloudError('暂时无法连接清华云盘，记录已保留在本机，可稍后重试') from None

    def api(self, endpoint, params=None, form=None):
        url = self.server + '/api/v2.1/via-repo-token/' + endpoint + '/'
        if params:
            url += '?' + urlencode(params)
        data = self.request(url, 'POST' if form is not None else 'GET', urlencode(form).encode() if form is not None else None, 'application/x-www-form-urlencoded' if form is not None else None)
        try:
            return json.loads(data)
        except ValueError:
            raise CloudError('云盘返回了无效响应') from None

    def repo_info(self):
        info = self.api('repo-info')
        directory = self.api('dir', {'path':'/'})
        if isinstance(directory,dict):
            info['permission'] = directory.get('user_perm') or info.get('permission')
        return info

    def list_dir(self, path):
        result = self.api('dir', {'path':path})
        return result.get('dirent_list', result.get('items', [])) if isinstance(result,dict) else result

    def ensure_dir(self, path):
        current = ''
        for part in path.strip('/').split('/'):
            if not part:
                continue
            current += '/' + part
            try:
                self.list_dir(current)
            except FileNotFoundError:
                try:
                    self.api('dir', {'path':current}, {'operation':'mkdir'})
                except CloudError:
                    self.list_dir(current)

    def current_id(self, folder):
        for item in self.list_dir(folder):
            if item.get('name') == 'records.json' and item.get('type') == 'file':
                return item.get('id')
        return None

    def read_snapshot(self, path):
        link = self.api('download-link', {'path':path})
        if not isinstance(link, str):
            raise CloudError('云盘未返回文件下载地址')
        try:
            result = json.loads(self.request(link, authenticated=False))
        except ValueError:
            raise CloudError('云端档案不是有效 JSON 文件') from None
        if not isinstance(result,dict) or result.get('version') != 1 or not isinstance(result.get('records'),list) or not isinstance(result.get('stations'),dict):
            raise CloudError('云端文件不是铁道手记档案，已停止同步')
        return result

    def upload_snapshot(self, folder, filename, snapshot):
        link = self.api('upload-link', {'path':folder})
        if not isinstance(link,str):
            raise CloudError('云盘未返回文件上传地址')
        boundary = 'RailwayJournal' + uuid.uuid4().hex
        fields = []
        for key,value in [('parent_dir',folder),('replace','1' if filename=='records.json' else '0')]:
            fields.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        fields.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/json\r\n\r\n'.encode())
        fields.append(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode())
        fields.append(f'\r\n--{boundary}--\r\n'.encode())
        url = link + ('&' if '?' in link else '?') + 'ret-json=1'
        self.request(url, 'POST', b''.join(fields), 'multipart/form-data; boundary=' + boundary, authenticated=False)

class CloudSync:
    def __init__(self, root, snapshot, restore, data_lock, client_factory=SeafileClient):
        self.root = root
        self.snapshot = snapshot
        self.restore = restore
        self.data_lock = data_lock
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.timer = None
        self.stop_event = threading.Event()
        self.worker = None
        self.phase = None

    def state(self):
        try:
            return json.loads((self.root() / 'data' / 'cloud-state.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            return {}

    def save_state(self, state):
        atomic_write(self.root() / 'data' / 'cloud-state.json', json.dumps(state, ensure_ascii=False).encode())

    def status(self):
        config = read_config(self.root())
        state = self.state()
        result = {key:state.get(key) for key in ('phase','message','last_sync','library','remote_count','last_action')}
        result.update(configured=bool(config), auto=bool(config and config.get('auto')), server=config['server'] if config else DEFAULT_SERVER, folder=config['folder'] if config else DEFAULT_FOLDER)
        if self.phase:
            result['phase'] = self.phase
        if not config:
            result.update(phase='disconnected',message='尚未连接清华云盘')
        return result

    def configure(self, values):
        with self.lock:
            previous = read_config(self.root()) or {}
            server = str(values.get('server',DEFAULT_SERVER)).rstrip('/')
            parsed = urlparse(server)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise CloudError('云盘地址应为完整的 HTTPS 服务器地址')
            if previous and server != previous.get('server') and not values.get('token'):
                raise CloudError('更换云盘服务器时，请填写该服务器的新令牌')
            token = str(values.get('token') or previous.get('token','')).strip()
            if not re.fullmatch(r'[0-9a-fA-F]{40}',token):
                raise CloudError('请填写 40 位资料库 API token')
            folder = str(values.get('folder',DEFAULT_FOLDER)).strip()
            if not folder.startswith('/') or folder=='/' or any(p in ('.','..') for p in folder.split('/')) or '\\' in folder or any(ord(c)<32 for c in folder):
                raise CloudError('请填写独立的云盘目录，例如 /铁道手记')
            folder = posixpath.normpath(folder)
            config = {'server':server,'token':token,'folder':folder,'auto':bool(values.get('auto',True))}
            client = self.client_factory(config)
            info = client.repo_info()
            if info.get('permission') == 'r':
                raise CloudError('该令牌只有读取权限，请使用读写资料库 API token')
            if any(previous.get(k)!=config[k] for k in ('server','token','folder')):
                state = {}
            else:
                state = self.state()
            state.update(phase='ready',message='已连接，等待同步',library=info.get('repo_name') or info.get('name') or '资料库')
            write_config(self.root(),config)
            self.save_state(state)
            return self.status()

    def disconnect(self):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            (self.root() / 'data' / 'cloud-secrets.bin').unlink(missing_ok=True)
            (self.root() / 'data' / 'cloud-state.json').unlink(missing_ok=True)
            return self.status()

    def sync(self, mode='auto'):
        if mode not in ('auto','upload','download'):
            raise CloudError('未知同步操作')
        with self.lock:
            config = read_config(self.root())
            if not config:
                return self.status()
            state = self.state()
            self.phase = 'syncing'
            try:
                client = self.client_factory(config)
                client.ensure_dir(config['folder'])
                with self.data_lock:
                    checked_local = self.snapshot()
                if mode == 'auto' and state.get('remote_id') and fingerprint(checked_local) == state.get('last_hash') and hasattr(client,'current_id'):
                    if client.current_id(config['folder']) == state['remote_id']:
                        state.update(phase='synced',message='本机与云端数据一致',last_action='unchanged',last_sync=utc_now())
                        self.save_state(state)
                        self.phase = None
                        return self.status()
                remote_path = config['folder'] + '/records.json'
                try:
                    remote = client.read_snapshot(remote_path)
                except FileNotFoundError:
                    remote = None
                with self.data_lock:
                    local = self.snapshot()
                local_hash = fingerprint(local)
                remote_hash = fingerprint(remote) if remote is not None else None
                baseline = state.get('last_hash')
                action = None
                if mode == 'download':
                    if remote is None:
                        raise CloudError('云端还没有乘车档案，请先上传本机数据')
                    action = 'download'
                elif mode == 'upload':
                    action = 'upload'
                elif remote_hash == local_hash:
                    action = 'unchanged'
                elif remote is None:
                    if baseline:
                        raise ConflictError('云端档案被移动或删除，请选择重新上传本机数据')
                    action = 'upload'
                elif baseline is None:
                    if not local['records'] and not local['stations']:
                        action = 'download'
                    else:
                        raise ConflictError('本机与云端已有不同档案，请选择保留哪一份')
                elif local_hash == baseline:
                    action = 'download'
                elif remote_hash == baseline:
                    action = 'upload'
                else:
                    raise ConflictError('本机和云端都发生了修改，自动同步已暂停，请选择保留哪一份')
                if action == 'upload':
                    history = config['folder'] + '/历史备份'
                    client.ensure_dir(history)
                    filename = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12] + '.json'
                    if remote is not None and remote_hash != local_hash:
                        client.upload_snapshot(history, filename, remote)
                    # 初次上传同样保留版本，云端 records.json 始终是可恢复的完整 JSON 档案。
                    client.upload_snapshot(history, 'local-' + filename, local)
                    outgoing = {**local, 'cloud':{'updated_at':utc_now(),'hash':local_hash}}
                    client.upload_snapshot(config['folder'], 'records.json', outgoing)
                    state.update(last_hash=local_hash,remote_count=len(local['records']))
                elif action == 'download':
                    with self.data_lock:
                        if fingerprint(self.snapshot()) != local_hash:
                            raise ConflictError('同步期间本机记录发生修改，已停止覆盖，请再次同步')
                        self.restore(remote)
                        state.update(last_hash=fingerprint(self.snapshot()),remote_count=len(remote['records']))
                else:
                    state.update(last_hash=local_hash,remote_count=len(local['records']))
                if hasattr(client,'current_id'):
                    state['remote_id'] = client.current_id(config['folder'])
                state.update(phase='synced',message={'upload':'本机档案已保存到云盘','download':'已读取云端档案','unchanged':'本机与云端数据一致'}[action],last_sync=utc_now(),last_action=action)
                self.save_state(state)
            except Exception as error:
                if isinstance(error,ConflictError):
                    state.update(phase='conflict',message=str(error))
                elif isinstance(error,(CloudError,ValueError,KeyError,TypeError)):
                    state.update(phase='error',message=str(error) if isinstance(error,CloudError) else '档案内容无效，已停止同步，请检查云端文件')
                else:
                    state.update(phase='error',message='同步未完成，本机数据已保留，请稍后重试')
                self.save_state(state)
            finally:
                self.phase = None
            return self.status()

    def schedule(self):
        if not (self.root() / 'data' / 'cloud-secrets.bin').exists():
            return
        try:
            config = read_config(self.root())
        except (CloudError,OSError,ValueError):
            state = self.state()
            state.update(phase='error',message='本机凭据无法读取，请重新连接清华云盘')
            try:
                self.save_state(state)
            except OSError:
                pass
            return
        if not config or not config.get('auto'):
            return
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(1.5,self.sync)
        self.timer.daemon = True
        self.timer.start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.schedule()
        def watch():
            while not self.stop_event.wait(30):
                try:
                    self.schedule()
                except CloudError:
                    pass
        self.worker = threading.Thread(target=watch,daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        if self.timer:
            self.timer.cancel()
