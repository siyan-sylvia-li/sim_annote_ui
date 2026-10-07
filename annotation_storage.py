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

from recordings import group_recordings


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

    def list_sessions(self) -> list:
        """Every annotatable recording: [{'id', 'name', 'kind'}] (kind: video, audio or role_tracks)."""
        recordings = group_recordings(self._media_files(), self.role_aliases)
        self._recordings = {r['id']: r for r in recordings}
        return [{'id': r['id'], 'name': r['name'], 'kind': r['kind']} for r in recordings]

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
        return {'type': self.player_type, 'media': 'video' if rec['kind'] == 'video' else 'audio', 'sources': sources}

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
    """
    requires_login = True
    player_type = 'box'
    API = 'https://api.box.com/2.0'
    AUTHORIZE_URL = 'https://account.box.com/api/oauth2/authorize'
    TOKEN_URL = 'https://api.box.com/oauth2/token'
    PREVIEW_SCOPES = 'base_preview item_preview'

    def __init__(self, client_id: str, client_secret: str, folder_id: str, redirect_uri: str,
                 annotations_root: str, role_aliases: dict = None):
        if not folder_id:
            raise ValueError('BOX_FOLDER_ID is required for Box storage')
        if not (client_id and client_secret):
            raise ValueError('Set BOX_CLIENT_ID and BOX_CLIENT_SECRET for Box storage')
        super().__init__(annotations_root, role_aliases)
        self.client_id = client_id
        self.client_secret = client_secret
        self.folder_id = str(folder_id)
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
            params = {'fields': 'id,type,name', 'limit': 1000, 'usemarker': 'true'}
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
                if item['type'] == 'folder':
                    walk(item['id'], f"{path}/{item['name']}" if path else item['name'])
                elif item['type'] == 'file':
                    files.append({'id': item['id'], 'name': item['name'], 'folder': path, 'folder_key': folder_id})

        walk(self.folder_id, '')
        return files

    def _file_source(self, file_id: str) -> dict:
        return {'file_id': file_id}

    def _downscoped(self, file_id: str, scope: str) -> dict:
        """Exchange the rater's token for one limited to `scope` on this one file."""
        resp = requests.post(self.TOKEN_URL, timeout=30, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
            'subject_token': self._token(),
            'subject_token_type': 'urn:ietf:params:oauth:token-type:access_token',
            'scope': scope,
            'resource': f"{self.API}/files/{file_id}",
        })
        if resp.status_code != 200:
            raise FileNotFoundError(file_id)
        return resp.json()

    def preview_token(self, session_id: str, file_id: str) -> dict:
        """A token that can only preview `file_id`, which must belong to this recording."""
        if file_id not in {f['id'] for f in self.recording(session_id)['files']}:
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
        key = 'clc1.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        return {'key': key, 'recording': rec['name'], 'files': len(files), 'expires_in': expires_in}


def new_oauth_state() -> str:
    return secrets.token_urlsafe(24)
