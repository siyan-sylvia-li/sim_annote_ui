"""
Make transcripts on Colab (GPU) for the CLC annotation app.

Whisper needs a GPU and the recordings live in Box, so: in the annotation app (signed in with
Box), open a recording and click "Copy Colab key". The key lets this script download just that
recording's files from Box, for about an hour, and nothing else. Then on Colab:

    python colab_transcribe.py --key "$CLC_KEY"      # or set CLC_KEY (e.g. with getpass)
    python colab_transcribe.py --audio file.wav ...  # audio already on this machine

It writes <recording>.transcript.json to --out; load it in the app with "Import transcript".
Per-person-mic recordings get their speakers from the file names; for single-mic recordings
speakers are assigned in the app. The downloaded audio is deleted when the script finishes.
"""
import base64
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path

import requests

from recordings import group_recordings, media_kind
from transcription import transcribe_files

BASE_DIR = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger('colab_transcribe')


def load_scheme() -> dict:
    with open(BASE_DIR / 'schemes' / 'clc.json') as f:
        return json.load(f)


def decode_key(key: str) -> dict:
    key = key.strip()
    if not key.startswith('clc1.'):
        sys.exit('That doesn\'t look like a Colab key from the app (it should start with "clc1.")')
    body = key[len('clc1.'):]
    payload = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
    if payload['expires_at'] < time.time():
        sys.exit('This key has expired (they last about an hour). Copy a new one in the app (Copy Colab key).')
    return payload


def download_from_box(payload: dict, folder: Path) -> list:
    """Fetch the recording's files with the key's download-only tokens. Returns [{'role', 'path'}]."""
    wanted = payload['files']
    if not any(f['role'] for f in wanted):
        # Camera angles of one recording share a soundtrack; only the first is transcribed
        wanted = wanted[:1]
    files = []
    for f in wanted:
        path = folder / f"{f['id']}_{re.sub(r'[^A-Za-z0-9._ #-]+', '_', f['name'])}"
        logger.info(f"Downloading {f['name']} from Box")
        resp = requests.get(f"https://api.box.com/2.0/files/{f['id']}/content",
                            headers={'Authorization': f"Bearer {f['token']}"}, stream=True, timeout=600)
        if resp.status_code == 401:
            sys.exit('Box rejected the key (it may have expired). Copy a new one in the app.')
        if resp.status_code == 403:
            sys.exit(f"Box won't let this account download {f['name']} (downloads are turned off for it).")
        resp.raise_for_status()
        with open(path, 'wb') as out:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                out.write(chunk)
        files.append({'role': f['role'], 'path': path})
    return files


def local_files(paths: list, aliases: dict, work: Path) -> tuple:
    """
    Group local files the same way the app groups Box files. Returns (recording name, [{'role', 'path'}]).
    The files are linked into `work`, so the .wav files Whisper writes next to its input land
    there instead of in the source folder (e.g. Google Drive).
    """
    entries = [{'id': str(Path(p).resolve()), 'name': Path(p).name, 'folder': '', 'folder_key': ''}
               for p in paths if media_kind(Path(p).name)]
    if not entries:
        sys.exit('None of the --audio files is an audio/video file')
    recordings = group_recordings(entries, aliases)
    if len(recordings) > 1:
        sys.exit('Those files look like more than one recording; run the script once per recording:\n  '
                 + '\n  '.join(r['name'] for r in recordings))
    rec = recordings[0]
    files = []
    for f in rec['files']:
        link = work / Path(f['id']).name
        link.symlink_to(f['id'])
        files.append({'role': f['role'], 'path': link})
    return rec['name'], files


def output_path(out_dir: Path, recording: str, suffix: str) -> Path:
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', re.sub(r'\.\w+$', '', recording)).strip('._') or 'recording'
    return out_dir / f"{name}.{suffix}.json"


def main():
    parser = ArgumentParser(description=__doc__.split('\n\n')[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--key', default=os.environ.get('CLC_KEY'), help='Colab key from the app (default: $CLC_KEY)')
    source.add_argument('--audio', nargs='+', help='Audio/video files of ONE recording already on this machine')
    parser.add_argument('--out', default='transcripts', help='Folder for the output JSON (default: ./transcripts)')
    args = parser.parse_args()
    if not args.key and not args.audio:
        parser.error('Give --key (or set CLC_KEY) or --audio')

    scheme = load_scheme()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='clc_'))
    try:
        if args.key:
            payload = decode_key(args.key)
            recording = payload['recording']
            files = download_from_box(payload, work)
        else:
            recording, files = local_files(args.audio, scheme.get('role_aliases', {}), work)
        logger.info(f"Recording: {recording} ({len(files)} file{'s' if len(files) > 1 else ''})")

        transcript = transcribe_files(files, scheme.get('speakers', []))
        transcript['recording'] = recording
        path = output_path(out_dir, recording, 'transcript')
        with open(path, 'w') as f:
            json.dump(transcript, f, indent=2)
        logger.info(f"Transcribed {len(transcript['segments'])} segments")
        print(f"\nWrote {path}\nIn the app: open \"{recording}\" → Import transcript")
    finally:
        # Don't leave session audio lying around on the Colab machine
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
