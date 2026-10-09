"""Google Drive sync for the notebook library.

Only the standard library is used. Authorisation is the OAuth "installed app"
flow with PKCE: the user's browser opens Google's consent page and a one-shot
loopback server receives the code. The scope is drive.file, so the app can only
see files it created itself, inside one "Inkpad" folder.

Sync is an index-driven delta merge. Every notebook has a permanent id, and one
small index file on Drive lists each id with its path, content hash, Drive file
and state. A sync downloads that index, compares it per id with the library and
with what both sides looked like last time, moves only the notebooks that
differ, then writes the index back. Deleted notebooks stay in the index as
tombstones, which is how a deletion reaches other devices instead of being
undone by them; renames and moves are just a changed path on the same id.
Nothing is destroyed: deletions go to the local trash or the Drive bin, and a
notebook edited on both sides keeps the remote version as a separate copy.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gi.repository import GLib

from .i18n import tr
from . import settings
from .model import Document, merge, notebook_id, set_notebook_id

SCOPE = 'https://www.googleapis.com/auth/drive.file'
CONFIG = settings.CONFIG_DIR   # follows INKPAD_HOME, so an isolated run has no credentials
CLIENT_PATH = os.path.join(CONFIG, 'google_client.json')
TOKEN_PATH = os.path.join(CONFIG, 'google_token.json')
API = 'https://www.googleapis.com/drive/v3'
UPLOAD = 'https://www.googleapis.com/upload/drive/v3/files'
FOLDER_MIME = 'application/vnd.google-apps.folder'
ROOT_NAME = 'Inkpad'
INDEX_NAME = '_inkpad_index.json'
EXT = '.inkpad'


CHECKPOINT = 10   # seconds of transfers after which a sync notes, in the index, how far it has got


class DriveError(Exception):
    code = None   # the HTTP status, when Google answered with an error


# -- credentials ------------------------------------------------------------

def _write_private(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    os.replace(path + '.tmp', path)


def load_client():
    try:
        with open(CLIENT_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data.get('installed') or data.get('web')


def install_client(source):
    """Adopt an OAuth client file downloaded from the Google Cloud console."""
    with open(source, encoding='utf-8') as f:
        data = json.load(f)
    client = data.get('installed')
    if not client or not client.get('client_id') or not client.get('token_uri'):
        raise DriveError(tr('这不是“桌面应用”类型的 OAuth 客户端凭据文件'))
    # A file exported from Inkpad names the folder to sync with; one from Google does not,
    # and leaves that as it is here.
    data.setdefault('inkpad_folder', folder_id())
    _write_private(CLIENT_PATH, data)


def save_client(client_id, secret):
    """Use an OAuth client typed in by hand. Changing the client drops the current sign-in."""
    client_id, secret = client_id.strip(), secret.strip()
    if not client_id.endswith('.apps.googleusercontent.com'):
        raise DriveError(tr('客户端 ID 应以 .apps.googleusercontent.com 结尾'))
    old = load_client() or {}
    if old.get('client_id') != client_id:
        try:
            os.remove(TOKEN_PATH)
        except OSError:
            pass
    _write_private(CLIENT_PATH, {'inkpad_folder': folder_id(), 'installed': {
        'client_id': client_id, 'client_secret': secret,
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token'}})


def folder_id():
    """The Drive folder the user asked the library to be kept in, or '' for a folder named
    Inkpad at the top of their Drive. It is kept with the client credentials, so that the
    one file carries everything another device needs."""
    try:
        with open(CLIENT_PATH, encoding='utf-8') as f:
            return str(json.load(f).get('inkpad_folder') or '')
    except (OSError, ValueError):
        return ''


def parse_folder(text):
    """A folder id out of what was typed: the id itself, or the address of the folder."""
    text = text.strip()
    for mark in ('/folders/', 'id='):
        if mark in text:
            text = text.split(mark, 1)[1]
    for stop in '?/&#':
        text = text.split(stop, 1)[0]
    if text and not all(c.isalnum() or c in '-_' for c in text):
        raise DriveError(tr('这不像是 Google Drive 的文件夹 ID'))
    return text


def set_folder_id(value):
    try:
        with open(CLIENT_PATH, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise DriveError(tr('请先填写并保存凭据')) from None
    data['inkpad_folder'] = value
    _write_private(CLIENT_PATH, data)


def export_client(dest):
    """Write the client credentials and the folder id out, for setting up another device."""
    with open(CLIENT_PATH, encoding='utf-8') as f:
        data = json.load(f)
    _write_private(dest, data)


def connected():
    return os.path.exists(TOKEN_PATH) and load_client() is not None


def _post(url, fields):
    request = urllib.request.Request(url, urllib.parse.urlencode(fields).encode(), method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:300]
        raise DriveError(tr('Google 拒绝了请求（%d）：%s') % (exc.code, detail)) from None
    except urllib.error.URLError as exc:
        raise DriveError(tr('无法连接 Google：%s') % exc.reason) from None


class _CodeHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'code' in query or 'error' in query:
            self.server.result = query
            text = tr('授权完成，可以关闭此页面并回到 Inkpad。') if 'code' in query else tr('授权被取消。')
        else:
            text = 'Inkpad'
        body = ('<!doctype html><meta charset="utf-8"><title>Inkpad</title>'
                '<body style="font-family:sans-serif;text-align:center;margin-top:18vh">'
                '<h2>%s</h2></body>' % text).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def authorize(open_url, timeout=300):
    """Run the browser consent flow and store the tokens. Blocks; call from a thread."""
    client = load_client()
    if client is None:
        raise DriveError(tr('尚未提供 Google 客户端凭据'))
    server = http.server.HTTPServer(('127.0.0.1', 0), _CodeHandler)
    server.result = None
    server.timeout = 1
    redirect = 'http://127.0.0.1:%d' % server.server_address[1]
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = secrets.token_urlsafe(16)
    open_url(client['auth_uri'] + '?' + urllib.parse.urlencode({
        'client_id': client['client_id'], 'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPE,
        'access_type': 'offline', 'prompt': 'consent', 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'}))
    deadline = time.time() + timeout
    try:
        while server.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if result is None:
        raise DriveError(tr('等待授权超时'))
    if 'code' not in result or result.get('state', [''])[0] != state:
        raise DriveError(tr('授权未完成'))
    token = _post(client['token_uri'], {
        'client_id': client['client_id'], 'client_secret': client.get('client_secret', ''),
        'code': result['code'][0], 'code_verifier': verifier, 'grant_type': 'authorization_code',
        'redirect_uri': redirect})
    if 'refresh_token' not in token:
        raise DriveError(tr('Google 没有返回刷新令牌，请重试'))
    _write_private(TOKEN_PATH, {'refresh_token': token['refresh_token'], 'access_token': token['access_token'],
                                'expires_at': time.time() + token.get('expires_in', 3600) - 60})


def disconnect():
    """Forget the account: revoke the grant (best effort) and delete the stored tokens."""
    try:
        with open(TOKEN_PATH, encoding='utf-8') as f:
            token = json.load(f).get('refresh_token')
        if token:
            _post('https://oauth2.googleapis.com/revoke', {'token': token})
    except (OSError, ValueError, DriveError):
        pass
    try:
        os.remove(TOKEN_PATH)
    except OSError:
        pass


# -- Drive REST -------------------------------------------------------------

class Drive:
    def __init__(self):
        self.client = load_client()
        try:
            with open(TOKEN_PATH, encoding='utf-8') as f:
                self.token = json.load(f)
        except (OSError, ValueError):
            raise DriveError(tr('尚未连接 Google Drive')) from None

    def _access(self):
        if time.time() >= self.token.get('expires_at', 0):
            fresh = _post(self.client['token_uri'], {
                'client_id': self.client['client_id'], 'client_secret': self.client.get('client_secret', ''),
                'refresh_token': self.token['refresh_token'], 'grant_type': 'refresh_token'})
            self.token['access_token'] = fresh['access_token']
            self.token['expires_at'] = time.time() + fresh.get('expires_in', 3600) - 60
            _write_private(TOKEN_PATH, self.token)
        return self.token['access_token']

    on_bytes = None   # called with (bytes so far, total) during an upload or a download

    def _open(self, method, url, data=None, headers=None):
        request = urllib.request.Request(url, data, method=method)
        request.add_header('Authorization', 'Bearer ' + self._access())
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            return urllib.request.urlopen(request, timeout=120)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            try:
                detail = json.loads(detail)['error']['message']
            except (ValueError, KeyError, TypeError):
                detail = detail[:300]
            error = DriveError(tr('Google Drive 错误（%d）：%s') % (exc.code, detail))
            error.code = exc.code
            raise error from None
        except urllib.error.URLError as exc:
            raise DriveError(tr('无法连接 Google Drive：%s') % exc.reason) from None

    def _json(self, method, url, body=None):
        data = None if body is None else json.dumps(body).encode()
        with self._open(method, url, data, {'Content-Type': 'application/json'} if body is not None else None) as r:
            return json.load(r)

    def _children(self, folder):
        out, page = [], None
        while True:
            query = {'q': "'%s' in parents and trashed = false" % folder, 'pageSize': '1000',
                     'fields': 'nextPageToken, files(id, name, mimeType, md5Checksum)'}
            if page:
                query['pageToken'] = page
            data = self._json('GET', API + '/files?' + urllib.parse.urlencode(query))
            out += data.get('files', [])
            page = data.get('nextPageToken')
            if not page:
                return out

    def alive(self, file_id):
        """Whether a file or folder is still there, and not in the bin. (One that has been
        binned still answers to its id, which is why this has to be asked.)"""
        try:
            return not self._json('GET', '%s/files/%s?fields=trashed' % (API, file_id)).get('trashed')
        except DriveError as exc:
            if exc.code == 404:
                return False
            raise

    def children(self, folder):
        return self._children(folder)

    def root(self):
        query = {'q': "name = '%s' and mimeType = '%s' and 'root' in parents and trashed = false"
                      % (ROOT_NAME, FOLDER_MIME), 'fields': 'files(id)'}
        found = self._json('GET', API + '/files?' + urllib.parse.urlencode(query)).get('files', [])
        return found[0]['id'] if found else self.create_folder(ROOT_NAME, 'root')

    def find(self, name, parent):
        query = {'q': "name = '%s' and '%s' in parents and trashed = false" % (name, parent), 'fields': 'files(id)'}
        found = self._json('GET', API + '/files?' + urllib.parse.urlencode(query)).get('files', [])
        return found[0]['id'] if found else None

    def get_json(self, file_id):
        with self._open('GET', '%s/files/%s?alt=media' % (API, file_id)) as r:
            return json.load(r)

    def put_json(self, name, value, parent, file_id=None):
        """Create or replace a small JSON file; returns its id."""
        boundary = 'inkpad' + secrets.token_hex(12)
        meta = {} if file_id else {'name': name, 'parents': [parent]}
        body = ('--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n'
                '--%s\r\nContent-Type: application/json\r\n\r\n%s\r\n--%s--'
                % (boundary, json.dumps(meta), boundary, json.dumps(value, ensure_ascii=False), boundary)).encode()
        url = UPLOAD + ('/%s' % file_id if file_id else '') + '?uploadType=multipart&fields=id'
        with self._open('PATCH' if file_id else 'POST', url, body,
                        {'Content-Type': 'multipart/related; boundary=' + boundary}) as r:
            return json.load(r)['id']

    def rename(self, file_id, name):
        self._json('PATCH', '%s/files/%s?fields=id' % (API, file_id), {'name': name})

    def create_folder(self, name, parent):
        return self._json('POST', API + '/files?fields=id',
                          {'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent]})['id']

    def upload(self, path, name, parent, file_id=None):
        """Create or replace a file with a resumable upload; returns (id, md5)."""
        if file_id:
            url, method, meta = '%s/%s?uploadType=resumable&fields=id,md5Checksum' % (UPLOAD, file_id), 'PATCH', {}
        else:
            url, method = UPLOAD + '?uploadType=resumable&fields=id,md5Checksum', 'POST'
            meta = {'name': name, 'parents': [parent]}
        size = os.path.getsize(path)
        with self._open(method, url, json.dumps(meta).encode(),
                        {'Content-Type': 'application/json; charset=UTF-8',
                         'X-Upload-Content-Type': 'application/zip', 'X-Upload-Content-Length': str(size)}) as r:
            session = r.headers['Location']
        with open(path, 'rb') as f:
            body = _Counting(f, size, self.on_bytes)
            with self._open('PUT', session, body, {'Content-Length': str(size), 'Content-Type': 'application/zip'}) as r:
                data = json.load(r)
        return data['id'], data.get('md5Checksum')

    def download(self, file_id, dest):
        with self._open('GET', '%s/files/%s?alt=media' % (API, file_id)) as r, open(dest + '.part', 'wb') as f:
            total, got = int(r.headers.get('Content-Length') or 0), 0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if self.on_bytes and total:
                    self.on_bytes(got, total)
        os.replace(dest + '.part', dest)

    def trash(self, file_id):
        self._json('PATCH', '%s/files/%s?fields=id' % (API, file_id), {'trashed': True})


class _Counting:
    """A file being uploaded that reports how much of it has been read."""

    def __init__(self, f, size, report):
        self.f, self.size, self.report, self.sent = f, size, report, 0

    def read(self, n=-1):
        data = self.f.read(n)
        self.sent += len(data)
        if self.report and self.size:
            self.report(self.sent, self.size)
        return data


# -- sync -------------------------------------------------------------------

def _md5(path):
    digest = hashlib.md5()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _unique(path):
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(path):
        path = '%s %d%s' % (base, n, ext)
        n += 1
    return path


def _scan(root):
    """(notebooks, folders) in the library: relative path -> absolute path, and a set of relative paths."""
    files, folders = {}, set()
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        prefix = os.path.relpath(base, root).replace(os.sep, '/')
        prefix = '' if prefix == '.' else prefix + '/'
        folders.update(prefix + d for d in dirs)
        for name in names:
            if name.endswith(EXT) and not name.startswith('.'):
                files[prefix + name] = os.path.join(base, name)
    return files, folders


def _load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, value):
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(path + '.tmp', path)


def _cloud_folder(state, drive):
    """The id of the Drive folder to sync with, checked to be there. When it is not the folder
    this device synced with before, what was remembered about that one is dropped, so that
    the library is compared with the new folder from nothing."""
    wanted = folder_id()
    if state.get('folder_set', '') != wanted:
        state.clear()
        state['folder_set'] = wanted
    if wanted:
        try:
            there = drive.alive(wanted)
        except DriveError as exc:
            if exc.code not in (403, 404):
                raise
            there = False
        if not there:
            # With the drive.file permission Google shows the app only what the app itself made.
            raise DriveError(tr('无法使用文件夹 %s：它不存在、在回收站里，或者不是 Inkpad 创建的。Inkpad 只被允许访问它自己'
                                '创建的文件夹；可以先留空同步一次，把生成的“Inkpad”文件夹在 Google Drive 里移到想放的位置，'
                                '再把它的 ID 填到其它设备上。') % wanted)
        state['root'] = wanted
    elif not state.get('root') or not drive.alive(state['root']):
        state['root'] = drive.root()
    return state['root']


def cloud_folder(root):
    """The id of the folder this library last synced with, or ''."""
    return _load(os.path.join(root, '.sync.json'), {}).get('root') or ''


def sync(root, drive, skip=(), progress=None):
    """Merge the library folder with the Drive index; returns a summary dict.

    skip is a collection of absolute paths to leave alone (a notebook being edited).
    progress(fraction, text) reports how far along the sync is: one step for reading the
    index, one per notebook (advancing with the bytes of its transfer) and one for the end.
    """
    report = progress or (lambda fraction, text: None)
    report(0.0, tr('正在读取云端目录…'))
    state_path = os.path.join(root, '.sync.json')
    state = _load(state_path, {})
    # What was remembered about the cloud is checked against what is really there: the folder
    # may be another one now, or have been deleted on Drive, and so may any of the files in it.
    root_id = _cloud_folder(state, drive)
    known = state.setdefault('items', {})  # id -> {'path', 'trashed', 'md5'} when both sides last agreed
    trash_dir = os.path.join(root, '.trash')
    os.makedirs(trash_dir, exist_ok=True)
    summary = {'up': 0, 'down': 0, 'conflicts': 0, 'merged': 0, 'removed': 0, 'moved': 0, 'meta': False}

    live = {f['id']: f for f in drive.children(root_id)}
    index_id = state.get('index')
    if index_id not in live:
        index_id = next((i for i, f in live.items() if f.get('name') == INDEX_NAME), None)
    cloud = drive.get_json(index_id) if index_id else {}
    items = cloud.setdefault('items', {})  # id -> {'path', 'trashed', 'md5', 'file', 'deleted', 'ts'}
    for nid in [n for n, e in items.items() if e.get('file') not in live]:
        if items[nid].get('deleted'):
            items[nid]['file'] = None   # still a deletion to pass on, with nothing left to restore
        else:
            # Listed, but its file is gone from Drive. Forgotten here, it is sent again by
            # whichever device still has it.
            del items[nid]
    saved_at = [time.monotonic()]

    def checkpoint():
        """Write down, in the cloud and here, what has been done so far, so that a sync that is
        cut off goes on from there the next time."""
        nonlocal index_id
        if time.monotonic() - saved_at[0] < CHECKPOINT:
            return
        index_id = state['index'] = drive.put_json(INDEX_NAME, cloud, root_id, index_id)
        _save(state_path, state)
        saved_at[0] = time.monotonic()

    def abspath(rel):
        return os.path.join(root, *rel.split('/'))

    # What is on this device: id -> [absolute path, relative path, in trash?].
    skip = {os.path.abspath(p) for p in skip}
    skip_ids = set()
    local = {}
    files, _ = _scan(root)
    trashed = {name: os.path.join(trash_dir, name) for name in sorted(os.listdir(trash_dir)) if name.endswith(EXT)}
    for rel, path, in_trash in [(r, p, False) for r, p in sorted(files.items())] + \
                              [(n, p, True) for n, p in trashed.items()]:
        try:
            nid = notebook_id(path)
        except Exception:
            continue  # not a readable notebook; leave it alone
        if os.path.abspath(path) in skip:
            skip_ids.add(nid)
            continue
        if nid in local:
            # A copy made outside the app shares its original's id; make it a notebook of its own.
            # The original is the one still where the last sync left it, whichever was met first.
            was = known.get(nid, {}).get('path')
            if rel == was and local[nid][1] != was:
                (path, rel, in_trash), local[nid] = local[nid], [path, rel, in_trash]
            nid = uuid.uuid4().hex
            set_notebook_id(path, nid)
        local[nid] = [path, rel, in_trash]

    def place(nid, rel, in_trash):
        """Move a local notebook to where the index says it belongs."""
        path = local[nid][0]
        target = os.path.join(trash_dir, os.path.basename(rel)) if in_trash else abspath(rel)
        if os.path.abspath(target) != os.path.abspath(path):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            target = _unique(target)
            shutil.move(path, target)
        rel = os.path.basename(target) if in_trash else os.path.relpath(target, root).replace(os.sep, '/')
        local[nid] = [target, rel, in_trash]

    def push(nid, entry, mine):
        """Upload a local notebook's content and record it in the index entry."""
        path, rel, in_trash = local[nid]
        file_id, theirs = drive.upload(path, os.path.basename(rel), root_id, entry.get('file'))
        summary['up'] += 1
        entry.update(file=file_id, md5=theirs or mine, path=rel, trashed=in_trash, deleted=False, ts=time.time())

    ids = sorted(set(local) | set(items) | set(known))
    steps = len(ids) + 2
    moved = 0
    for n, nid in enumerate(ids):
        here, entry, before = local.get(nid), items.get(nid), known.get(nid)
        name = os.path.basename((here[1] if here else (entry or {}).get('path')) or '')
        report((n + 1) / steps, name)
        # A transfer moves the bar smoothly through this notebook's step.
        drive.on_bytes = lambda got, total, n=n, name=name: report((n + 1 + got / total) / steps, name)
        if nid in skip_ids:
            continue
        if here is None and entry is None:
            known.pop(nid, None)
            continue
        if entry is None:
            # New on this device. Something already in the trash is not worth sending.
            if here[2]:
                continue
            entry = items[nid] = {}
            push(nid, entry, _md5(here[0]))
        elif here is None:
            if entry.get('deleted'):
                known.pop(nid, None)
                continue
            if before is not None and entry.get('md5') == before['md5']:
                # Gone from this device and untouched elsewhere: leave a tombstone. The file
                # itself stays on Drive, so a deletion can always be recovered from there.
                entry.update(deleted=True, ts=time.time())
                known.pop(nid, None)
                summary['removed'] += 1
                continue
            if entry.get('trashed') and before is None:
                continue  # sitting in another device's trash; no need to fetch it
            target = os.path.join(trash_dir, os.path.basename(entry['path'])) if entry.get('trashed') \
                else abspath(entry['path'])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            target = _unique(target)
            drive.download(entry['file'], target)
            local[nid] = [target, os.path.relpath(target, trash_dir if entry.get('trashed') else root)
                          .replace(os.sep, '/'), bool(entry.get('trashed'))]
            summary['down'] += 1
        else:
            mine = _md5(here[0])
            unchanged = before is not None and mine == before['md5']
            if entry.get('deleted'):
                if here[2] or unchanged:
                    # Deleted elsewhere and not edited here since: follow, into the trash.
                    place(nid, here[1], True)
                    known.pop(nid, None)
                    summary['removed'] += not here[2]
                    continue
                push(nid, entry, mine)  # edited after the deletion: the edit wins
            elif mine != entry.get('md5'):
                if unchanged:
                    drive.download(entry['file'], here[0])
                    summary['down'] += 1
                elif before is not None and entry.get('md5') == before['md5']:
                    push(nid, entry, mine)
                else:
                    # Edited on both sides: merge the two, item by item. Only if that is not
                    # possible is the remote version kept as a notebook of its own.
                    theirs = here[0] + '.theirs'
                    drive.download(entry['file'], theirs)
                    try:
                        merged = Document.load(here[0])
                        merge(merged, Document.load(theirs))
                        merged.save(here[0])
                        os.remove(theirs)
                        summary['merged'] += 1
                    except Exception:
                        copy = _unique(os.path.splitext(here[0])[0] + tr('（云端副本）') + EXT)
                        os.replace(theirs, copy)
                        set_notebook_id(copy, uuid.uuid4().hex)
                        summary['conflicts'] += 1
                    push(nid, entry, _md5(here[0]))
            # Name, folder and trash state travel separately from the content.
            where, there = (here[1], here[2]), (entry.get('path'), bool(entry.get('trashed')))
            if where != there:
                if before is not None and where == (before['path'], before['trashed']):
                    place(nid, *there)
                else:
                    if entry.get('file') and os.path.basename(where[0]) != os.path.basename(there[0] or ''):
                        drive.rename(entry['file'], os.path.basename(where[0]))
                    entry.update(path=where[0], trashed=where[1], ts=time.time())
                summary['moved'] += 1
        path, rel, in_trash = local[nid]
        entry = items[nid]
        entry.update(path=rel, trashed=in_trash)
        known[nid] = {'path': rel, 'trashed': in_trash, 'md5': entry['md5']}
        if summary['up'] + summary['down'] != moved:
            moved = summary['up'] + summary['down']
            checkpoint()

    drive.on_bytes = None
    report((steps - 1) / steps, tr('正在更新云端目录…'))
    # Folders, so that empty ones and their removal carry over too.
    folders = cloud.setdefault('folders', {})  # relative path -> deleted?
    seen = set(state.get('folders', []))
    _, local_dirs = _scan(root)
    for rel in sorted(local_dirs, key=len, reverse=True):
        if folders.get(rel) is True and rel in seen and not os.listdir(abspath(rel)):
            os.rmdir(abspath(rel))
            local_dirs.discard(rel)
        else:
            folders[rel] = False
    for rel, deleted in sorted(folders.items()):
        if not deleted and rel not in local_dirs:
            if rel in seen:
                folders[rel] = True
            else:
                os.makedirs(abspath(rel), exist_ok=True)
                local_dirs.add(rel)
    state['folders'] = sorted(local_dirs)

    # Favourites and folder styles: the newer copy wins as a whole.
    meta_path = os.path.join(root, '.library.json')
    meta = _load(meta_path, {})
    shared = {key: meta.get(key) for key in ('favorites', 'folders')}
    theirs = cloud.get('meta')
    if theirs and theirs.get('ts', 0) > meta.get('ts', 0):
        meta.update({key: theirs[key] for key in ('favorites', 'folders') if theirs.get(key) is not None})
        meta['ts'] = theirs['ts']
        _save(meta_path, meta)
        summary['meta'] = True
    elif meta.get('ts', 0) > (theirs or {}).get('ts', 0):
        cloud['meta'] = dict(shared, ts=meta['ts'])

    state['index'] = drive.put_json(INDEX_NAME, cloud, root_id, index_id)
    state['last'] = time.time()
    _save(state_path, state)
    report(1.0, '')
    return summary


def cloud_deleted(root, drive):
    """Notebooks the index marks as deleted while their file is still on Drive.

    Nothing is downloaded: this only reads the index. Returns [{'id', 'path', 'ts'}], newest first.
    """
    state = _load(os.path.join(root, '.sync.json'), {})
    root_id = _cloud_folder(state, drive)
    index_id = state.get('index') or drive.find(INDEX_NAME, root_id)
    cloud = drive.get_json(index_id) if index_id else {}
    out = [{'id': nid, 'path': entry.get('path') or nid, 'ts': entry.get('ts', 0)}
           for nid, entry in cloud.get('items', {}).items() if entry.get('deleted') and entry.get('file')]
    return sorted(out, key=lambda item: -item['ts'])


def cloud_restore(root, drive, ids, progress=None):
    """Download the chosen deleted notebooks back to the paths they had; returns how many."""
    report = progress or (lambda fraction, text: None)
    state_path = os.path.join(root, '.sync.json')
    state = _load(state_path, {})
    root_id = _cloud_folder(state, drive)
    index_id = state.get('index') or drive.find(INDEX_NAME, root_id)
    cloud = drive.get_json(index_id) if index_id else {}
    items, known = cloud.get('items', {}), state.setdefault('items', {})
    done = 0
    for n, nid in enumerate(ids):
        entry = items.get(nid)
        if not entry or not entry.get('file'):
            continue
        rel = entry.get('path') or nid + EXT
        report(n / len(ids), os.path.basename(rel))
        drive.on_bytes = lambda got, total, n=n, rel=rel: report((n + got / total) / len(ids), os.path.basename(rel))
        target = _unique(os.path.join(root, *rel.split('/')))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        drive.download(entry['file'], target)
        rel = os.path.relpath(target, root).replace(os.sep, '/')
        # It is a live notebook again, on both sides, with what the cloud holds.
        entry.update(deleted=False, trashed=False, path=rel, ts=time.time())
        known[nid] = {'path': rel, 'trashed': False, 'md5': entry.get('md5')}
        done += 1
    drive.on_bytes = None
    if done:
        state['index'] = drive.put_json(INDEX_NAME, cloud, root_id, index_id)
        _save(state_path, state)
    report(1.0, '')
    return done


def last_sync(root):
    return _load(os.path.join(root, '.sync.json'), {}).get('last')
