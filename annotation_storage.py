"""
Storage backends for the CLC annotation app.

The app only talks to a `Storage` object, which does three things:
  * media       - list recordings (see recordings.py for how files are grouped) and tell the
                  front end how to play one
  * annotations - load/save one JSON document per (recording, rater)
  * transcripts - load/save the recording's transcript (imported JSON with speaker labels)

Annotations and transcripts are always files on this computer (`LocalDocs`). Media comes from
either a local folder (`LocalStorage`, for development with test files) or Box (`BoxStorage`):
Box recordings are played through the Box Content Preview SDK with a preview-only token, so
raters never download them and view access to the Box folder is enough. Switching is a config
change (`--storage box`); the UI only sees the `player` descriptor returned by `get_player()`.
"""
import base64
import json
import os
import re
import secrets
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from recordings import CAMERA_ANGLE, group_recordings, session_number


def _safe_name(name: str) -> str:
    """Make a string safe to use as a single path component."""
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._') or 'unnamed'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, doc: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a uniquely named temp file, then swap it in: a crash mid-write never corrupts
    # the saved file, and concurrent writes can't trip over each other's temp file
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)


class ChooseZip(Exception):
    """More than one zip could be a recording's video: the user has to pick one."""

    def __init__(self, choices: list):
        self.choices = choices
        super().__init__('Several zip files could hold this recording\'s video; pick one')


class AuthRequired(Exception):
    """The storage needs the user to sign in (Box) before it can be used."""


class BoxAPIError(Exception):
    """A Box API call failed; carries Box's own error code and message so the UI can show them."""

    def __init__(self, status: int, code: str, message: str, action: str):
        self.status, self.code = status, code
        super().__init__(f"Box refused to {action}: {message} ({code})")


def _check(resp, action: str):
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {}
    raise BoxAPIError(resp.status_code, body.get('code', str(resp.status_code)),
                      body.get('message', resp.text[:200]), action)


class LocalDocs:
    """
    Annotations and transcripts kept as files on this computer:
    <root>/<recording folder>/<rater>.json and _transcript.json.
    """

    def __init__(self, root, folder_name):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._folder_name = folder_name   # recording id -> folder name
        self._lock = threading.Lock()

    def folder(self, session_id: str) -> Path:
        return self.root / self._folder_name(session_id)

    @staticmethod
    def _read(path: Path):
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    def load_annotations(self, session_id: str, rater: str):
        return self._read(self.folder(session_id) / f"{_safe_name(rater)}.json")

    def save_annotations(self, session_id: str, rater: str, doc: dict) -> dict:
        doc['updated_at'] = _now()
        _write_json(self.folder(session_id) / f"{_safe_name(rater)}.json", doc)
        return doc

    # Leading underscore: rater names go through _safe_name, which strips it, so no collision
    def load_transcript(self, session_id: str):
        return self._read(self.folder(session_id) / '_transcript.json')

    def save_transcript(self, session_id: str, transcript: dict) -> dict:
        transcript['updated_at'] = _now()
        _write_json(self.folder(session_id) / '_transcript.json', transcript)
        return transcript

    def update_transcript(self, session_id: str, update) -> dict:
        """Load, apply `update(transcript)` and save under a lock, so concurrent edits merge."""
        with self._lock:
            transcript = self.load_transcript(session_id)
            if transcript is None:
                raise FileNotFoundError(session_id)
            update(transcript)
            return self.save_transcript(session_id, transcript)


class Storage(ABC):
    # Login is only needed for Box
    requires_login = False
    player_type = None

    def __init__(self, annotations_root: str, role_aliases: dict = None):
        self.role_aliases = role_aliases or {}
        self._recordings = {}
        self.docs = LocalDocs(annotations_root, self._folder_name)
        self.save_location = str(self.docs.root)

    def is_authenticated(self) -> bool:
        return True

    @abstractmethod
    def _media_files(self) -> list:
        """Every file under the media root: [{'id', 'name', 'folder', 'folder_key'}]."""

    @abstractmethod
    def _file_source(self, file_id: str) -> dict:
        """Player source fields for one media file."""

    def _folder_name(self, session_id: str) -> str:
        """Folder for a recording's annotations: readable name plus the id, so two recordings never share one."""
        name = re.sub(r'\.\w+$', '', self.recording(session_id)['name'])  # drop a file extension, keep the session path
        return f"{_safe_name(name)}__{_safe_name(session_id)}"

    def _sync_infos(self) -> list:
        """
        Video sync files (made by colab_sync.py), newest per audio recording. Each says which
        camera files belong to which audio recording and how their clocks line up:
        video_time = offset + rate * audio_time.
        """
        return []

    def _store_sync(self, base_name: str, sync: dict):
        """Save a new sync file (never replacing an old one; the newest is used)."""
        raise NotImplementedError

    def adjust_sync(self, session_id: str, offset: float, rater: str) -> dict:
        """A manual correction of when the video plays against the audio, saved for everyone as a new sync file."""
        current = next((s for s in self._sync_infos() if s.get('session_id') == session_id), None)
        if not current:
            raise FileNotFoundError(session_id)
        sync = {**current, 'offset': round(float(offset), 3), 'method': 'manual adjustment', 'warning': None,
                'adjusted_from': current.get('offset'), 'adjusted_by': rater, 'created_at': _now()}
        self._store_sync(f"{self._folder_name(session_id)}.sync", sync)
        self.recording(session_id)['video'].update(offset=sync['offset'], warning=None, method=sync['method'])
        return sync

    def list_sessions(self) -> list:
        """Every annotatable recording: [{'id', 'name', 'kind', 'has_video'}] (kind: video, audio or role_tracks)."""
        media = self._media_files()
        recordings = group_recordings(media, self.role_aliases)
        self._attach_videos(recordings, media)
        self._recordings = {r['id']: r for r in recordings}
        return [{'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'has_video': 'video' in r} for r in recordings]

    def _attach_videos(self, recordings: list, media: list):
        """Give audio recordings their synced camera files, and stop listing those files as recordings of their own."""
        by_id = {r['id']: r for r in recordings}
        file_ids = {(str(f['folder_key']), f['name']): f['id'] for f in media}
        linked = set()
        for sync in self._sync_infos():
            rec = by_id.get(sync.get('session_id'))
            if not rec or rec['kind'] == 'video':
                continue
            files = []
            for name in sync.get('video_files', []):
                fid = file_ids.get((str(sync.get('video_folder_id')), name))
                if fid:
                    m = CAMERA_ANGLE.match(name)
                    files.append({'id': fid, 'name': name, 'label': f"Camera {m.group('camera').upper()}" if m else 'Video'})
            if not files:
                continue   # sync file made, but the camera files aren't in Box (yet)
            rec['video'] = {'files': files, 'offset': float(sync.get('offset', 0.0)), 'rate': float(sync.get('rate', 1.0)),
                            'duration': sync.get('video_duration'), 'warning': sync.get('warning'),
                            'method': sync.get('method'), 'checks': sync.get('checks')}
            linked.update(f['id'] for f in files)
        recordings[:] = [r for r in recordings if not ({f['id'] for f in r['files']} <= linked)]

    def recording(self, session_id: str) -> dict:
        if session_id not in self._recordings:
            self.list_sessions()
        if session_id not in self._recordings:
            raise FileNotFoundError(session_id)
        return self._recordings[session_id]

    def get_player(self, session_id: str) -> dict:
        """
        How the front end should play a recording:
        {'type': 'html5' | 'box', 'media': 'video' | 'audio', 'sources': [{'label', 'src' | 'file_id'}]}
        Recordings with several files (one per mic or camera) list one source per file.
        """
        rec = self.recording(session_id)
        sources = []
        for f in rec['files']:
            label = f.get('label') or (f"{f['role']} mic" if f['role'] else ('Video' if rec['kind'] == 'video' else 'Audio'))
            sources.append({'label': label, **self._file_source(f['id'])})
        player = {'type': self.player_type, 'media': 'video' if rec['kind'] == 'video' else 'audio', 'sources': sources}
        if rec.get('video'):
            # A synced video shown alongside the audio (which stays the clock and the sound)
            v = rec['video']
            player['video'] = {'sources': [{'label': f['label'], **self._file_source(f['id'])} for f in v['files']],
                               'offset': v['offset'], 'rate': v['rate'], 'duration': v['duration'], 'warning': v['warning'],
                               'method': v['method'], 'checks': v['checks']}
        return player

    def load_annotations(self, session_id: str, rater: str):
        return self.docs.load_annotations(session_id, rater)

    def save_annotations(self, session_id: str, rater: str, doc: dict) -> dict:
        return self.docs.save_annotations(session_id, rater, doc)

    def load_transcript(self, session_id: str):
        return self.docs.load_transcript(session_id)

    def save_transcript(self, session_id: str, transcript: dict) -> dict:
        return self.docs.save_transcript(session_id, transcript)

    def update_transcript(self, session_id: str, update) -> dict:
        return self.docs.update_transcript(session_id, update)


class LocalStorage(Storage):
    """Recordings from a folder on this computer (development with test files)."""
    player_type = 'html5'

    def __init__(self, media_root: str, annotations_root: str, role_aliases: dict = None):
        self.media_root = Path(media_root).expanduser().resolve()
        if not self.media_root.is_dir():
            raise FileNotFoundError(f"Media root does not exist: {self.media_root}")
        super().__init__(annotations_root, role_aliases)

    def resolve_media(self, file_id: str) -> Path:
        """Map a file id (path relative to media_root) to a file, refusing anything outside media_root."""
        path = (self.media_root / file_id).resolve()
        if self.media_root not in path.parents or not path.is_file():
            raise FileNotFoundError(file_id)
        return path

    def _media_files(self) -> list:
        files = []
        for path in self.media_root.rglob('*'):
            if path.is_file():
                rel = path.relative_to(self.media_root)
                folder = '' if rel.parent == Path('.') else rel.parent.as_posix()
                files.append({'id': rel.as_posix(), 'name': path.name, 'folder': folder, 'folder_key': folder})
        return files

    def _file_source(self, file_id: str) -> dict:
        return {'src': f"/media/{quote(file_id)}"}

    def _sync_infos(self) -> list:
        # Same layout as Box: sync files sit in a 'transcripts' folder under the media root
        latest = {}
        for path in sorted((self.media_root / 'transcripts').glob('*.sync*.json'), key=lambda p: (p.stat().st_mtime, p.name)):
            with open(path) as f:
                sync = json.load(f)
            latest[sync.get('session_id')] = sync
        return list(latest.values())

    def _store_sync(self, base_name: str, sync: dict):
        folder = self.media_root / 'transcripts'
        n, path = 1, folder / f"{base_name}.json"
        while path.exists():
            n += 1
            path = folder / f"{base_name}.{n}.json"
        _write_json(path, sync)


class BoxStorage(Storage):
    """
    Recordings from Box, for raters running the app on their own laptop.

    * Sign-in: OAuth 2.0 as the rater, so Box's own folder permissions decide what they can
      open. Tokens are kept in memory only (signing in again after restarting the app).
    * Recordings: every video/audio file under `folder_id` (sub-folders included), grouped as
      in recordings.py. Single-file recordings are identified by the Box file id, grouped
      recordings by '<folder id>#<take or camera prefix>'.
    * Playback: the front end renders each file with Box Content Preview using a token
      downscoped to preview-only scopes on that one file, so it can't be used to download.
    * Transcription: `colab_key` hands colab_transcribe.py download-only tokens for one
      recording's files, so Colab can fetch the audio without the rater downloading anything.
    * Shared transcripts: with `transcripts_folder_id`, each recording's transcript lives in that
      Box folder so every rater works from the same one. The first transcript uploaded for a
      recording (by Colab, or by "Import transcript") is the shared one and is never replaced.
      Raters keep a local copy for their speaker edits; it's refreshed if Box has a different one.
    * Synced video: `video_sync_key` hands colab_sync.py the recording's audio and its session's
      zip from `video_sessions_folder_id`; Colab puts the camera files next to the audio and a
      sync file (offset/drift) next to the transcripts, and the app links them up when listing.
    """
    requires_login = True
    player_type = 'box'
    API = 'https://api.box.com/2.0'
    AUTHORIZE_URL = 'https://account.box.com/api/oauth2/authorize'
    TOKEN_URL = 'https://api.box.com/oauth2/token'
    PREVIEW_SCOPES = 'base_preview item_preview'

    def __init__(self, client_id: str, client_secret: str, folder_id: str, redirect_uri: str,
                 annotations_root: str, role_aliases: dict = None, transcripts_folder_id: str = None,
                 video_sessions_folder_id: str = None):
        if not folder_id:
            raise ValueError('BOX_FOLDER_ID is required for Box storage')
        if not (client_id and client_secret):
            raise ValueError('Set BOX_CLIENT_ID and BOX_CLIENT_SECRET for Box storage')
        super().__init__(annotations_root, role_aliases)
        self.client_id = client_id
        self.client_secret = client_secret
        self.folder_id = str(folder_id)
        self.transcripts_folder_id = str(transcripts_folder_id) if transcripts_folder_id else None
        self.video_sessions_folder_id = str(video_sessions_folder_id) if video_sessions_folder_id else None
        self._sync_cache = {}   # sync file id -> contents (sync files are never edited, only added)
        self.redirect_uri = redirect_uri
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0
        self._auth_lock = threading.Lock()

    # ---------- Auth ----------

    def is_authenticated(self) -> bool:
        return bool(self._access_token)

    def authorize_url(self, state: str) -> str:
        return f"{self.AUTHORIZE_URL}?{urlencode({'response_type': 'code', 'client_id': self.client_id, 'redirect_uri': self.redirect_uri, 'state': state})}"

    def finish_login(self, code: str):
        self._store_tokens(self._token_request({
            'grant_type': 'authorization_code', 'code': code,
            'client_id': self.client_id, 'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
        }))

    def logout(self):
        with self._auth_lock:
            self._access_token = self._refresh_token = None
            self._expires_at = 0

    def _token_request(self, data: dict) -> dict:
        resp = requests.post(self.TOKEN_URL, data=data, timeout=30)
        if resp.status_code != 200:
            raise AuthRequired(f"Box sign-in failed: {resp.text[:200]}")
        return resp.json()

    def _store_tokens(self, tokens: dict):
        self._access_token = tokens['access_token']
        self._refresh_token = tokens.get('refresh_token')
        # Refresh a minute early so a request never goes out with a token about to expire
        self._expires_at = time.time() + tokens.get('expires_in', 3600) - 60

    def _token(self) -> str:
        with self._auth_lock:
            if not self._access_token:
                raise AuthRequired('Sign in with Box')
            if time.time() >= self._expires_at:
                if not self._refresh_token:
                    self._access_token = None
                    raise AuthRequired('Box session expired; sign in again')
                try:
                    self._store_tokens(self._token_request({
                        'grant_type': 'refresh_token', 'refresh_token': self._refresh_token,
                        'client_id': self.client_id, 'client_secret': self.client_secret,
                    }))
                except AuthRequired:
                    self._access_token = None
                    raise
            return self._access_token

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop('headers', {})
        resp = requests.request(method, url, headers={**headers, 'Authorization': f"Bearer {self._token()}"},
                                timeout=kwargs.pop('timeout', 60), **kwargs)
        if resp.status_code == 401:
            # Token revoked or expired early: force a refresh and retry once
            self._expires_at = 0
            resp = requests.request(method, url, headers={**headers, 'Authorization': f"Bearer {self._token()}"},
                                    timeout=60, **kwargs)
        if resp.status_code == 401:
            raise AuthRequired('Sign in with Box')
        return resp

    # ---------- Media ----------

    def _items(self, folder_id: str) -> list:
        items, marker = [], None
        while True:
            params = {'fields': 'id,type,name,created_at,size', 'limit': 1000, 'usemarker': 'true'}
            if marker:
                params['marker'] = marker
            resp = self._request('GET', f"{self.API}/folders/{folder_id}/items", params=params)
            _check(resp, 'list a folder')
            data = resp.json()
            items.extend(data['entries'])
            marker = data.get('next_marker')
            if not marker:
                return items

    def _media_files(self) -> list:
        files = []

        def walk(folder_id: str, path: str):
            for item in self._items(folder_id):
                if item['type'] == 'folder' and item['id'] != self.transcripts_folder_id:
                    walk(item['id'], f"{path}/{item['name']}" if path else item['name'])
                elif item['type'] == 'file':
                    files.append({'id': item['id'], 'name': item['name'], 'folder': path, 'folder_key': folder_id})

        walk(self.folder_id, '')
        return files

    def _file_source(self, file_id: str) -> dict:
        return {'file_id': file_id}

    def _downscoped(self, file_id: str, scope: str, kind: str = 'files') -> dict:
        """Exchange the rater's token for one limited to `scope` on this one file (or folder)."""
        resp = requests.post(self.TOKEN_URL, timeout=30, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'subject_token': self._token(),
            'subject_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'scope': scope,
            'resource': f"{self.API}/{kind}/{file_id}",
        })
        if resp.status_code != 200:
            raise FileNotFoundError(file_id)
        return resp.json()

    def preview_token(self, session_id: str, file_id: str) -> dict:
        """A token that can only preview `file_id`, which must belong to this recording (or its synced video)."""
        rec = self.recording(session_id)
        allowed = {f['id'] for f in rec['files']} | {f['id'] for f in rec.get('video', {}).get('files', [])}
        if file_id not in allowed:
            raise FileNotFoundError(file_id)
        token = self._downscoped(file_id, self.PREVIEW_SCOPES)
        return {'token': token['access_token'], 'expires_in': token.get('expires_in')}

    def colab_key(self, session_id: str) -> dict:
        """
        A key for colab_transcribe.py: one token per file of this recording, each limited to
        downloading that one file and expiring after about an hour. It can't list, change or
        share anything, and doesn't contain the rater's own sign-in.
        """
        rec = self.recording(session_id)
        files, expires_in = [], 3600
        for f in rec['files']:
            # Box can switch downloads off for a folder or role; a key would then fail on Colab,
            # so say so here instead
            resp = self._request('GET', f"{self.API}/files/{f['id']}", params={'fields': 'permissions'})
            _check(resp, f"read the permissions of '{f['name']}'")
            if not resp.json().get('permissions', {}).get('can_download', False):
                raise BoxAPIError(403, 'downloads_not_allowed',
                                  f"your Box account isn't allowed to download '{f['name']}', so Colab can't fetch it. "
                                  "Ask the folder owner to allow downloads for whoever runs the transcription",
                                  'make a Colab key')
            token = self._downscoped(f['id'], 'item_download')
            expires_in = min(expires_in, token.get('expires_in', 3600))
            files.append({'id': f['id'], 'name': f['name'], 'role': f['role'], 'token': token['access_token']})
        payload = {'recording': rec['name'], 'session_id': session_id, 'kind': rec['kind'],
                   'expires_at': int(time.time()) + int(expires_in), 'files': files}
        # Let Colab add the finished transcript to the shared folder: a token that can only
        # upload into that one folder (it can't read, change or delete anything there)
        will_share = self.transcripts_folder_id is not None and self._can_upload_transcripts()
        if will_share:
            token = self._downscoped(self.transcripts_folder_id, 'item_upload', kind='folders')
            payload['transcript_upload'] = {'folder_id': self.transcripts_folder_id,
                                            'name': self.transcript_file_name(session_id),
                                            'token': token['access_token']}
        key = 'clc1.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return {'key': key, 'recording': rec['name'], 'files': len(files), 'expires_in': expires_in,
                'shares_transcript': will_share, 'transcripts_configured': self.transcripts_folder_id is not None}

    # ---------- Synced video ----------

    def _sync_infos(self) -> list:
        if not self.transcripts_folder_id:
            return []
        latest = {}
        items = [i for i in self._items(self.transcripts_folder_id)
                 if i['type'] == 'file' and '.sync' in i['name'] and i['name'].endswith('.json')]
        # A re-run adds a new sync file rather than replacing one, so the newest wins
        for item in sorted(items, key=lambda i: i.get('created_at') or ''):
            if item['id'] not in self._sync_cache:
                resp = self._request('GET', f"{self.API}/files/{item['id']}/content")
                _check(resp, f"read '{item['name']}'")
                self._sync_cache[item['id']] = resp.json()
            sync = self._sync_cache[item['id']]
            latest[sync.get('session_id')] = sync
        return list(latest.values())

    def _store_sync(self, base_name: str, sync: dict):
        if not self._can_upload_transcripts():
            raise BoxAPIError(403, 'can_upload_not_allowed',
                              "your Box account can't add files to the shared transcripts folder", 'save the video timing')
        existing = {i['name'] for i in self._items(self.transcripts_folder_id) if i['type'] == 'file'}
        n, name = 1, f"{base_name}.json"
        while name in existing:
            n += 1
            name = f"{base_name}.{n}.json"
        resp = self._request('POST', 'https://upload.box.com/api/2.0/files/content',
                             files={'attributes': (None, json.dumps({'name': name, 'parent': {'id': self.transcripts_folder_id}})),
                                    'file': (name, json.dumps(sync, indent=2).encode(), 'application/json')})
        _check(resp, 'save the video timing')

    def _require_permission(self, kind: str, item_id: str, permission: str, message: str):
        resp = self._request('GET', f"{self.API}/{kind}/{item_id}", params={'fields': 'permissions'})
        _check(resp, 'check permissions')
        if not resp.json().get('permissions', {}).get(permission, False):
            raise BoxAPIError(403, f'{permission}_not_allowed', message, 'make a video sync key')

    def session_zips(self, session_id: str) -> list:
        """Zip files in the session folder (in `video_sessions_folder_id`) with the same session number as this recording."""
        rec = self.recording(session_id)
        number = session_number(rec['folder'] or rec['name'])
        if number is None:
            raise BoxAPIError(404, 'no_session_number', f"can't tell which session '{rec['name']}' belongs to", 'find the video')
        folders = [i for i in self._items(self.video_sessions_folder_id)
                   if i['type'] == 'folder' and session_number(i['name']) == number]
        zips = [{'id': z['id'], 'name': z['name'], 'folder': f['name'], 'size': z.get('size')}
                for f in folders for z in self._items(f['id']) if z['type'] == 'file' and z['name'].lower().endswith('.zip')]
        if not zips:
            raise BoxAPIError(404, 'no_zip', f"no zip file found for session {number} in the sessions folder", 'find the video')
        return zips

    def video_sync_key(self, session_id: str, zip_id: str = None) -> dict:
        """
        A key for colab_sync.py: download-only tokens for this recording's audio and its session's
        video zip, upload-only tokens for the recording's audio folder (camera files) and the
        transcripts folder (the sync file, and the transcript if the recording has none yet), and
        the shared transcript if there is one (to check the alignment against). All expire after
        about an hour.
        """
        if not (self.video_sessions_folder_id and self.transcripts_folder_id):
            raise BoxAPIError(400, 'not_configured', 'set BOX_VIDEO_SESSIONS_FOLDER_ID and BOX_TRANSCRIPTS_FOLDER_ID in .env',
                              'make a video sync key')
        rec = self.recording(session_id)
        if rec['kind'] == 'video':
            raise BoxAPIError(400, 'already_video', 'this recording is already a video', 'make a video sync key')
        zips = self.session_zips(session_id)
        if zip_id:
            zips = [z for z in zips if z['id'] == zip_id]
        if len(zips) != 1:
            raise ChooseZip(zips)
        zip_file = zips[0]
        self._require_permission('files', zip_file['id'], 'can_download',
                                 f"your Box account isn't allowed to download '{zip_file['name']}'")
        for f in rec['files']:
            self._require_permission('files', f['id'], 'can_download', f"your Box account isn't allowed to download '{f['name']}'")
        self._require_permission('folders', rec['folder_key'], 'can_upload',
                                 f"your Box account can't add files to '{rec['folder']}', where the camera files go")
        self._require_permission('folders', self.transcripts_folder_id, 'can_upload',
                                 "your Box account can't add files to the shared transcripts folder, where the sync file goes")

        def token(item_id, scope, kind='files'):
            return self._downscoped(item_id, scope, kind=kind)['access_token']

        existing = [i['name'] for i in self._items(rec['folder_key']) if i['type'] == 'file']
        transcript_id = self._shared_transcript_id(session_id)
        payload = {
            'kind': 'video_sync', 'recording': rec['name'], 'session_id': session_id, 'recording_kind': rec['kind'],
            'expires_at': int(time.time()) + 3600,
            'audio_files': [{'id': f['id'], 'name': f['name'], 'role': f['role'], 'token': token(f['id'], 'item_download')}
                            for f in rec['files']],
            'zip': {**zip_file, 'token': token(zip_file['id'], 'item_download')},
            'transcript': {'id': transcript_id, 'token': token(transcript_id, 'item_download')} if transcript_id else None,
            'upload_videos': {'folder_id': rec['folder_key'], 'existing': existing,
                              'token': token(rec['folder_key'], 'item_upload', kind='folders')},
            'upload_sync': {'folder_id': self.transcripts_folder_id, 'name': f"{self._folder_name(session_id)}.sync",
                            'existing': [i['name'] for i in self._items(self.transcripts_folder_id) if i['type'] == 'file'],
                            # No shared transcript yet: the sync makes one and shares it (same folder, same token)
                            'transcript_name': None if transcript_id else self.transcript_file_name(session_id),
                            'token': token(self.transcripts_folder_id, 'item_upload', kind='folders')},
        }
        key = 'clc1.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return {'key': key, 'recording': rec['name'], 'zip': f"{zip_file['folder']} / {zip_file['name']}", 'expires_in': 3600}

    # ---------- Shared transcripts ----------

    def transcript_file_name(self, session_id: str) -> str:
        return f"{self._folder_name(session_id)}.transcript.json"

    def _can_upload_transcripts(self) -> bool:
        resp = self._request('GET', f"{self.API}/folders/{self.transcripts_folder_id}", params={'fields': 'permissions'})
        _check(resp, 'read the shared transcripts folder')
        return bool(resp.json().get('permissions', {}).get('can_upload', False))

    def has_shared_transcript(self, session_id: str) -> bool:
        return bool(self.transcripts_folder_id and self._shared_transcript_id(session_id))

    def _shared_transcript_id(self, session_id: str):
        name = self.transcript_file_name(session_id)
        for item in self._items(self.transcripts_folder_id):
            if item['type'] == 'file' and item['name'] == name:
                return item['id']
        return None

    def load_transcript(self, session_id: str):
        """The shared transcript from Box if there is one (kept locally with this rater's speaker edits), else the local copy."""
        if not self.transcripts_folder_id:
            return self.docs.load_transcript(session_id)
        local = self.docs.load_transcript(session_id)
        box_id = self._shared_transcript_id(session_id)
        if not box_id:
            return local
        if local and local.get('box_file_id') == box_id:
            return local
        resp = self._request('GET', f"{self.API}/files/{box_id}/content")
        _check(resp, 'read the shared transcript')
        shared = resp.json()
        shared['box_file_id'] = box_id
        # The rater's own speaker edits start from the shared version
        return self.docs.save_transcript(session_id, shared)

    def share_transcript(self, session_id: str, transcript: dict) -> str:
        """
        Upload a transcript as the shared one for this recording, unless one already exists.
        Returns 'shared', 'exists' (another transcript is already shared), 'no_permission' or
        'not_configured'.
        """
        if not self.transcripts_folder_id:
            return 'not_configured'
        if self._shared_transcript_id(session_id):
            return 'exists'
        if not self._can_upload_transcripts():
            return 'no_permission'
        name = self.transcript_file_name(session_id)
        body = {k: v for k, v in transcript.items() if k not in ('box_file_id', 'updated_at')}
        resp = self._request('POST', 'https://upload.box.com/api/2.0/files/content',
                             files={'attributes': (None, json.dumps({'name': name, 'parent': {'id': self.transcripts_folder_id}})),
                                    'file': (name, json.dumps(body, indent=2).encode(), 'application/json')})
        if resp.status_code == 409:
            return 'exists'   # someone else shared one a moment ago
        _check(resp, 'add the transcript to the shared folder')
        box_id = resp.json()['entries'][0]['id']
        self.docs.update_transcript(session_id, lambda t: t.update(box_file_id=box_id))
        return 'shared'


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)
