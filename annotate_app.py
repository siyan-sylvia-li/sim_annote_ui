"""
Closed Loop Communication (CLC) annotation app.

Replaces the CLC Excel recording sheet: raters play a session recording, mark
call-outs on a timeline (or an imported transcript) and fill in the same columns as
the sheet. Annotations are saved on this computer as one JSON document per
(recording, rater).

Usage:
    python annotate_app.py --storage box                   # raters: recordings streamed from Box
    python annotate_app.py --media-root /path/to/videos    # development with local files

Box settings (BOX_CLIENT_ID, BOX_CLIENT_SECRET, BOX_FOLDER_ID, ...) are read from the
environment or a `.env` file next to this script; see `.env.example`.
"""
import json
import logging
import os
import re
import secrets
from argparse import ArgumentParser
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session
from werkzeug.exceptions import HTTPException

from annotation_storage import AuthRequired, BoxAPIError, BoxStorage, LocalStorage, new_oauth_state

BASE_DIR = Path(__file__).resolve().parent
SCHEME_DIR = BASE_DIR / 'schemes'
SCHEME_ID = 'clc'

app = Flask(__name__)
# Keep keys in spreadsheet column order instead of alphabetizing them
app.json.sort_keys = False
# Make browsers revalidate JS/CSS so raters always get the latest version after an update
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# Only used to sign the OAuth state cookie; a fresh key per run is fine
app.config['SECRET_KEY'] = secrets.token_hex(32)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

storage = None


@app.errorhandler(HTTPException)
def api_error(e):
    # API callers get {'error': message} so the UI can show it; pages keep Flask's HTML errors
    if request.path.startswith('/api/'):
        return jsonify({'error': e.description}), e.code
    return e


@app.errorhandler(AuthRequired)
def auth_required(e):
    return jsonify({'error': str(e), 'login': True}), 401


@app.errorhandler(BoxAPIError)
def box_error(e):
    logger.error(str(e))
    return jsonify({'error': str(e), 'box_code': e.code}), 502


def load_env_file(path: Path):
    """Minimal .env reader (KEY=value lines) so secrets stay out of the code and the repo."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_scheme(scheme_id: str) -> dict:
    path = SCHEME_DIR / f"{scheme_id}.json"
    if not path.exists():
        abort(404, f"Unknown scheme {scheme_id}")
    with open(path) as f:
        return json.load(f)


@app.route('/')
def index():
    return render_template('annotate.html')


# ---------- Box sign-in ----------

@app.route('/api/auth')
def auth_status():
    return jsonify({'login_required': storage.requires_login, 'authenticated': storage.is_authenticated(),
                    'save_location': storage.save_location})


@app.route('/box/login')
def box_login():
    if not isinstance(storage, BoxStorage):
        abort(404)
    # Start on the same host Box will redirect back to (e.g. localhost, not 127.0.0.1),
    # otherwise the state cookie set here isn't sent with the callback
    redirect_host = urlparse(storage.redirect_uri).netloc
    if request.host != redirect_host:
        return redirect(f"{urlparse(storage.redirect_uri).scheme}://{redirect_host}/box/login")
    session['oauth_state'] = new_oauth_state()
    return redirect(storage.authorize_url(session['oauth_state']))


@app.route('/box/callback')
def box_callback():
    if not isinstance(storage, BoxStorage):
        abort(404)
    if request.args.get('error'):
        return f"Box sign-in was cancelled: {request.args.get('error_description', request.args['error'])}", 400
    # The state check stops another site from completing a sign-in into this app
    if not request.args.get('state') or request.args.get('state') != session.pop('oauth_state', None):
        return 'Sign-in expired or was started elsewhere. Go back to the app and try again.', 400
    try:
        storage.finish_login(request.args.get('code', ''))
    except AuthRequired as e:
        return str(e), 400
    return redirect('/')


@app.route('/box/logout', methods=['POST'])
def box_logout():
    if isinstance(storage, BoxStorage):
        storage.logout()
    return jsonify({'ok': True})


@app.route('/api/scheme/<scheme_id>')
def get_scheme(scheme_id):
    return jsonify(load_scheme(scheme_id))


@app.route('/api/sessions')
def list_sessions():
    return jsonify(storage.list_sessions())


@app.route('/api/player')
def get_player():
    session_id = request.args.get('session', '')
    try:
        return jsonify(storage.get_player(session_id))
    except FileNotFoundError:
        abort(404, f"Session not found: {session_id}")


@app.route('/api/player/token')
def get_preview_token():
    """Box only: a fresh preview-only token for one file of a recording (the viewer asks as needed)."""
    if not isinstance(storage, BoxStorage):
        abort(404)
    try:
        return jsonify(storage.preview_token(request.args.get('session', ''), request.args.get('file', '')))
    except FileNotFoundError:
        abort(404, 'File not found in this recording')


@app.route('/media/<path:file_id>')
def serve_media(file_id):
    # Only used by LocalStorage; Box media is rendered by the Box Preview SDK instead.
    if not isinstance(storage, LocalStorage):
        abort(404)
    try:
        path = storage.resolve_media(file_id)
    except FileNotFoundError:
        abort(404)
    # conditional=True enables HTTP range requests, which the browser needs for seeking
    return send_file(path, conditional=True)


@app.route('/api/annotations', methods=['GET'])
def get_annotations():
    session_id = request.args.get('session', '')
    rater = request.args.get('rater', '').strip()
    if not session_id or not rater:
        abort(400, 'session and rater are required')
    return jsonify(storage.load_annotations(session_id, rater))


@app.route('/api/annotations', methods=['PUT'])
def save_annotations():
    doc = request.get_json(silent=True)
    if not isinstance(doc, dict) or not doc.get('session_id') or not str(doc.get('rater', '')).strip():
        abort(400, 'Annotation document must include session_id and rater')
    if not isinstance(doc.get('loops'), list):
        abort(400, 'Annotation document must include a loops list')
    saved = storage.save_annotations(doc['session_id'], doc['rater'].strip(), doc)
    logger.info(f"Saved {len(doc['loops'])} loops for {doc['session_id']} / {doc['rater']}")
    return jsonify({'updated_at': saved['updated_at']})


# ---------- Transcript ----------
#
# A transcript is imported from a JSON file (Whisper output, or labels exported from the
# original app) and saved with the recording's annotations. Raters can then assign or fix
# the speaker of each segment.


def normalize_segments(raw_segments):
    """
    Keep the fields the UI needs; drop empty / punctuation-only segments like the original app.
    Existing segment ids are kept (annotations link to them), so deleting a segment never renumbers the rest.
    """
    segments = []
    used_ids = set()
    next_id = max([s['id'] for s in raw_segments if isinstance(s.get('id'), int)] + [-1]) + 1
    for seg in raw_segments:
        text = str(seg.get('text', '')).strip()
        if not text.replace('.', '').strip():
            continue
        seg_id = seg.get('id')
        if not isinstance(seg_id, int) or seg_id in used_ids:
            seg_id = next_id
            next_id += 1
        used_ids.add(seg_id)
        out = {
            'id': seg_id,
            'start': round(float(seg.get('start', 0.0)), 2),
            'end': round(float(seg.get('end', 0.0)), 2),
            'text': text,
            'speaker': seg.get('speaker', '') or '',
            'speaker_source': seg.get('speaker_source') or ('manual' if seg.get('speaker') else ''),
        }
        if seg.get('words'):
            out['words'] = [{'word': w.get('word', ''), 'start': w.get('start'), 'end': w.get('end')} for w in seg['words']]
        segments.append(out)
    return segments


def session_arg():
    session_id = request.args.get('session') or (request.get_json(silent=True) or {}).get('session')
    if not session_id:
        abort(400, 'session is required')
    return session_id


@app.route('/api/transcript', methods=['GET'])
def get_transcript():
    return jsonify(storage.load_transcript(session_arg()))


@app.route('/api/transcript', methods=['PUT'])
def save_transcript():
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('segments'), list):
        abort(400, 'Transcript must include a segments list')
    session_id = session_arg()
    existing = storage.load_transcript(session_id) or {}
    existing.update({
        'source': body.get('source', existing.get('source', 'import')),
        'speakers': body.get('speakers', existing.get('speakers', [])),
        'segments': normalize_segments(body['segments']),
    })
    saved = storage.save_transcript(session_id, existing)
    return jsonify(saved)


@app.route('/api/transcript/segment', methods=['PATCH'])
def update_segment():
    """Change one segment's speaker or delete it. Per-segment so raters editing at once don't overwrite each other."""
    body = request.get_json(silent=True) or {}
    session_id = session_arg()
    seg_id = body.get('id')
    if not isinstance(seg_id, int):
        abort(400, 'Segment id is required')

    def apply(transcript):
        if body.get('add_speaker') and body['add_speaker'] not in transcript.setdefault('speakers', []):
            transcript['speakers'].append(body['add_speaker'])
        if body.get('delete'):
            transcript['segments'] = [s for s in transcript['segments'] if s['id'] != seg_id]
            return
        for seg in transcript['segments']:
            if seg['id'] == seg_id:
                seg['speaker'] = body.get('speaker', '') or ''
                seg['speaker_source'] = 'manual' if seg['speaker'] else ''
                return
        abort(404, f'Segment {seg_id} not found')

    try:
        saved = storage.update_transcript(session_id, apply)
    except FileNotFoundError:
        abort(404, 'No transcript for this session')
    return jsonify({'updated_at': saved['updated_at'], 'speakers': saved.get('speakers', [])})


def main():
    global storage
    load_env_file(BASE_DIR / '.env')
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=['local', 'box'], default='local')
    parser.add_argument('--media-root', help='Folder of session recordings (local storage)')
    parser.add_argument('--annotations-root', default=str(BASE_DIR / 'data' / 'annotations'),
                        help='Folder on this computer where annotation JSON is written')
    parser.add_argument('--box-folder-id', default=os.environ.get('BOX_FOLDER_ID'),
                        help='Box folder holding the session recordings (default: $BOX_FOLDER_ID)')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5050)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    # Filename role codes ("MED RN") -> speaker names ("Med Nurse"), from the coding scheme
    role_aliases = load_scheme(SCHEME_ID).get('role_aliases', {})
    if args.storage == 'local':
        if not args.media_root:
            parser.error('--media-root is required with --storage local')
        storage = LocalStorage(args.media_root, args.annotations_root, role_aliases)
    else:
        # Accept a pasted folder URL or shared-link form ("…/folder/123?s=…") as well as the bare id
        match = re.search(r'(?:folder/)?(\d+)', args.box_folder_id or '')
        try:
            storage = BoxStorage(
                client_id=os.environ.get('BOX_CLIENT_ID'),
                client_secret=os.environ.get('BOX_CLIENT_SECRET'),
                folder_id=match.group(1) if match else None,
                # Must match the redirect URI registered in the Box Developer Console
                redirect_uri=os.environ.get('BOX_REDIRECT_URI', f"http://localhost:{args.port}/box/callback"),
                annotations_root=args.annotations_root,
                role_aliases=role_aliases,
            )
        except ValueError as e:
            parser.error(str(e))

    logger.info(f"Annotations are saved to: {storage.save_location}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == '__main__':
    main()
