"""
Link a session's video to an audio recording, on Colab, for the CLC annotation app.

The camera files are in a zip in the sessions folder, and the video and audio recordings don't
start at the same moment. In the app (signed in with Box), open the audio recording and click
"Copy video sync key". Then on Colab:

    python colab_sync.py --key "$CLC_KEY"      # or set CLC_KEY (e.g. with getpass)

This downloads the recording's audio and the session's zip (download-only, about an hour),
unzips the camera files, works out how video time lines up with audio time (video_sync.py),
checks it by matching spoken lines with Whisper, and adds to Box:
  * a sync file next to the shared transcripts (offset and drift), and
  * the camera files, next to the audio (skipped if they're already there).
Then the app shows the video alongside that recording. Everything downloaded is deleted at the end.
"""
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

import requests

import video_sync
from recordings import VIDEO_EXTENSIONS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('colab_sync')

UPLOAD = 'https://upload.box.com/api/2.0'
SIMPLE_UPLOAD_LIMIT = 50 * 1024 * 1024    # Box takes bigger files only in chunks


def decode_key(key: str) -> dict:
    key = (key or '').strip()
    if not key.startswith('clc1.'):
        sys.exit('That doesn\'t look like a key from the app (it should start with "clc1.")')
    body = key[len('clc1.'):]
    payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    if payload.get('kind') != 'video_sync':
        sys.exit('This is a transcription key. For video, use "Copy video sync key" in the app.')
    if payload['expires_at'] < time.time():
        sys.exit('This key has expired (they last about an hour). Copy a new one in the app.')
    return payload


def download(file_id: str, token: str, name: str, path: Path):
    logger.info(f"Downloading {name} from Box")
    resp = requests.get(f"https://api.box.com/2.0/files/{file_id}/content",
                        headers={'Authorization': f"Bearer {token}"}, stream=True, timeout=600)
    if resp.status_code == 401:
        sys.exit('Box rejected the key (it may have expired). Copy a new one in the app.')
    if resp.status_code == 403:
        sys.exit(f"Box won't let this account download {name}.")
    resp.raise_for_status()
    done, reported = 0, 0
    with open(path, 'wb') as out:
        for chunk in resp.iter_content(chunk_size=8 << 20):
            out.write(chunk)
            done += len(chunk)
            if done - reported >= 500 << 20:
                logger.info(f"  {done >> 20} MB")
                reported = done
    return path


def unzip_videos(zip_path: Path, folder: Path) -> list:
    """Extract the camera files (any folder inside the zip), keeping their names."""
    videos = []
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            name = Path(member.filename).name
            if member.is_dir() or name.startswith('._') or '__MACOSX' in member.filename:
                continue
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                target = folder / name
                with z.open(member) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 8 << 20)
                videos.append(target)
    if not videos:
        sys.exit(f"No video files found in {zip_path.name}")
    return sorted(videos)


def duration(path: Path) -> float:
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                         check=True, capture_output=True, text=True).stdout
    return float(out.strip())


def transcribe_small(path: Path) -> list:
    """Quick Whisper pass (small model) just for matching spoken lines; not saved anywhere."""
    import whisper
    model = transcribe_small.model = getattr(transcribe_small, 'model', None) or whisper.load_model('small')
    return model.transcribe(str(path), verbose=False)['segments']


# ---------- Uploads (upload-only tokens from the key) ----------

def _unique_name(base: str, existing: set) -> str:
    """'<base>.json', or '<base>.2.json', ... if a re-run already left one (newest is used)."""
    name, n = f"{base}.json", 1
    while name in existing:
        n += 1
        name = f"{base}.{n}.json"
    return name


def upload_bytes(token: str, folder_id: str, name: str, content: bytes) -> str:
    resp = requests.post(f"{UPLOAD}/files/content", headers={'Authorization': f"Bearer {token}"}, timeout=300,
                         files={'attributes': (None, json.dumps({'name': name, 'parent': {'id': folder_id}})),
                                'file': (name, content)})
    if resp.status_code == 409:
        return 'exists'
    resp.raise_for_status()
    return 'uploaded'


def upload_large(token: str, folder_id: str, path: Path) -> str:
    """Box chunked upload: open a session, send parts with their SHA-1, commit."""
    size = path.stat().st_size
    auth = {'Authorization': f"Bearer {token}"}
    resp = requests.post(f"{UPLOAD}/files/upload_sessions", headers=auth, timeout=60,
                         json={'folder_id': folder_id, 'file_size': size, 'file_name': path.name})
    if resp.status_code == 409:
        return 'exists'
    resp.raise_for_status()
    session = resp.json()
    part_size = session['part_size']
    parts, whole = [], hashlib.sha1()
    with open(path, 'rb') as f:
        offset = 0
        while offset < size:
            chunk = f.read(part_size)
            whole.update(chunk)
            digest = base64.b64encode(hashlib.sha1(chunk).digest()).decode()
            end = offset + len(chunk) - 1
            r = requests.put(session['session_endpoints']['upload_part'], data=chunk, timeout=300,
                             headers={**auth, 'Content-Type': 'application/octet-stream', 'Digest': f"sha={digest}",
                                      'Content-Range': f"bytes {offset}-{end}/{size}"})
            r.raise_for_status()
            parts.append(r.json()['part'])
            offset = end + 1
            logger.info(f"  {path.name}: {offset * 100 // size}%")
    digest = base64.b64encode(whole.digest()).decode()
    for _ in range(30):
        r = requests.post(session['session_endpoints']['commit'], timeout=120, json={'parts': parts},
                          headers={**auth, 'Digest': f"sha={digest}"})
        if r.status_code == 202:   # Box is still putting the parts together
            time.sleep(int(r.headers.get('Retry-After', 5)))
            continue
        r.raise_for_status()
        return 'uploaded'
    raise RuntimeError(f"Box didn't finish committing {path.name}")


def upload_file(token: str, folder_id: str, path: Path) -> str:
    if path.stat().st_size < SIMPLE_UPLOAD_LIMIT:
        return upload_bytes(token, folder_id, path.name, path.read_bytes())
    return upload_large(token, folder_id, path)


# ---------- Alignment ----------

def decide(sound: dict, words: dict, video_seconds: float, audio_seconds: float) -> dict:
    """Pick the alignment to save and say how sure it is."""
    confident = sound['confidence'] >= 8 and sound['windows'] >= 3
    sync = {'offset': sound['offset'], 'rate': sound['rate'], 'method': 'sound', 'warning': None}
    if words['agrees'] is False and words['matches'] >= 5:
        # The spoken words clearly disagree with the sound match: trust the words
        sync.update(offset=words['offset'], rate=1.0, method='spoken words',
                    warning='Lined up by matching spoken words only; check that the video looks in sync.')
    elif not confident and words['agrees'] is not True:
        sync['warning'] = "Couldn't confirm the alignment; check that the video looks in sync."
    covered = min(video_seconds, sync['offset'] + sync['rate'] * audio_seconds) - max(0.0, sync['offset'])
    if covered < 0.5 * audio_seconds and not sync['warning']:
        sync['warning'] = 'The video covers less than half of this recording; check that it is the right video.'
    return sync


def main():
    parser = ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--key', default=os.environ.get('CLC_KEY'), help='Video sync key from the app (default: $CLC_KEY)')
    parser.add_argument('--no-word-check', action='store_true', help="Skip the Whisper cross-check (faster)")
    args = parser.parse_args()
    payload = decode_key(args.key)
    logger.info(f"Recording: {payload['recording']}  |  video: {payload['zip']['folder']} / {payload['zip']['name']}")

    work = Path(tempfile.mkdtemp(prefix='clc_sync_'))
    try:
        # 1. Audio (the clock) and the session's camera files
        audio_paths = [download(f['id'], f['token'], f['name'], work / f"{f['id']}_{Path(f['name']).name}")
                       for f in payload['audio_files']]
        zip_path = download(payload['zip']['id'], payload['zip']['token'], payload['zip']['name'], work / 'session.zip')
        (work / 'videos').mkdir()
        videos = unzip_videos(zip_path, work / 'videos')
        zip_path.unlink()
        logger.info(f"Camera files: {', '.join(v.name for v in videos)}")

        # 2. Line up: all camera angles share one clock, so the first one's soundtrack is used
        audio = video_sync.mix([video_sync.load_mono(p) for p in audio_paths])
        video_audio = video_sync.load_mono(videos[0])
        audio_seconds = len(audio) / video_sync.SAMPLE_RATE
        video_seconds = duration(videos[0])
        logger.info('Matching the sound of the video against the audio...')
        sound = video_sync.align(video_sync.onset_envelope(audio), video_sync.onset_envelope(video_audio))
        logger.info(f"  offset {sound['offset']:.3f} s, rate {sound['rate']:.6f}, confidence {sound['confidence']:.0f}, "
                    f"{sound['windows']} windows agree")

        # 3. Cross-check by matching spoken lines
        words = {'matches': 0, 'agrees': None, 'offset': None}
        if not args.no_word_check:
            logger.info('Cross-checking with Whisper (matching spoken lines)...')
            if payload.get('transcript'):
                t = payload['transcript']
                path = download(t['id'], t['token'], 'shared transcript', work / 'transcript.json')
                audio_segments = json.loads(path.read_text())['segments']
            else:
                wav = work / 'audio_mix.wav'
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(video_sync.SAMPLE_RATE), '-ac', '1',
                                '-i', '-', str(wav)], input=audio.tobytes(), check=True)
                audio_segments = transcribe_small(wav)
            pairs = video_sync.word_offsets(audio_segments, transcribe_small(videos[0]))
            words = video_sync.check_with_words(sound, pairs)
            logger.info(f"  {words['matches']} matching lines; "
                        f"{'agrees' if words['agrees'] else 'disagrees' if words['agrees'] is False else 'not enough to tell'}")

        chosen = decide(sound, words, video_seconds, audio_seconds)
        sync = {
            'recording': payload['recording'], 'session_id': payload['session_id'],
            'video_folder_id': payload['upload_videos']['folder_id'],
            'video_files': [v.name for v in videos],
            'zip': f"{payload['zip']['folder']} / {payload['zip']['name']}",
            'offset': round(chosen['offset'], 4), 'rate': chosen['rate'],
            'video_duration': round(video_seconds, 3), 'audio_duration': round(audio_seconds, 3),
            'method': chosen['method'], 'warning': chosen['warning'],
            'checks': {'sound_confidence': round(sound['confidence'], 1), 'sound_windows': sound['windows'],
                       'word_matches': words['matches'], 'words_agree': words['agrees'],
                       'word_offset': words['offset']},
            'created_at': datetime.now(timezone.utc).isoformat(),
        }

        # 4. Save the sync file first (small), then the camera files (big, slow)
        up = payload['upload_sync']
        name = _unique_name(up['name'], set(up['existing']))
        while upload_bytes(up['token'], up['folder_id'], name, json.dumps(sync, indent=2).encode()) == 'exists':
            name = _unique_name(up['name'], set(up['existing']) | {name})
        logger.info(f"Saved the sync file to Box: {name}")

        uv = payload['upload_videos']
        for v in videos:
            if v.name in uv['existing']:
                logger.info(f"{v.name} is already in Box; skipped")
                continue
            logger.info(f"Uploading {v.name} ({v.stat().st_size >> 20} MB) to Box...")
            try:
                status = upload_file(uv['token'], uv['folder_id'], v)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else '?'
                sys.exit(f"Uploading {v.name} failed (HTTP {code}). If the key expired, copy a new one and run again: "
                         "files already uploaded are skipped.")
            logger.info(f"  {v.name}: {'already in Box' if status == 'exists' else 'uploaded'}")

        drift = '' if abs(sync['rate'] - 1) < 1e-6 else f" (clock drift {(sync['rate'] - 1) * 100:+.4f}%)"
        print(f"\nDone. The video starts {abs(sync['offset']):.2f} s {'after' if sync['offset'] < 0 else 'before'} "
              f"the audio{drift}.")
        if sync['warning']:
            print(f"Note: {sync['warning']}")
        print(f'In the app, load "{payload["recording"]}" again to see the video.')
    finally:
        # Don't leave session audio or video lying around on the Colab machine
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
