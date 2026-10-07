"""
Turning a recording into a transcript, for colab_transcribe.py (Whisper needs a GPU, so this
runs on Colab) and for cleaning up transcripts the app imports.

  * One file (a single-mic WAV, or a video): Whisper on it; speakers are assigned in the app.
  * One WAV per person's mic: Whisper on each mic, the speaker taken from the file name, merged
    into one timeline. Speech that leaked onto other people's mics (the same words at the same
    time) is kept only on the mic where it was loudest.

whisper_transcribe (and numpy, for the mic comparison) are imported only when transcribing,
so the app can import this file on a laptop without them.
"""
import logging
import subprocess
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


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


def transcribe_files(files: list, default_speakers: list) -> dict:
    """
    files: [{'role', 'path'}] for one recording (role is None for a single video/WAV).
    Returns a transcript {'source', 'speakers', 'segments'}.
    """
    from whisper_transcribe import transcribe_with_whisper

    role_tracks = [f for f in files if f['role']]
    if role_tracks:
        tracks = []
        for f in role_tracks:
            logger.info(f"Transcribing {f['role']} mic ({Path(f['path']).name}) with Whisper")
            # save_json=False: the caller writes the transcript, so the folder goes unused
            results, audio_path = transcribe_with_whisper(str(f['path']), tempfile.gettempdir(), save_json=False)
            tracks.append({'role': f['role'], 'segments': results.get('segments', []), 'audio': audio_path})
        return {
            'source': 'whisper_role_tracks',
            'speakers': list(dict.fromkeys(f['role'] for f in role_tracks)),
            'segments': normalize_segments(merge_role_segments(tracks)),
        }

    # Several files without roles (camera angles) share one soundtrack: use the first
    logger.info(f"Transcribing {Path(files[0]['path']).name} with Whisper")
    results, _ = transcribe_with_whisper(str(files[0]['path']), tempfile.gettempdir(), save_json=False)
    return {
        'source': 'whisper',
        'speakers': list(default_speakers),
        'segments': normalize_segments(results.get('segments', [])),
    }


# ---------- Per-person mics ----------

def load_mono(path):
    """Decode any audio/video file to 16 kHz mono float samples (ffmpeg handles 24-bit WAVs etc.)."""
    import numpy as np
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 's16le', '-ac', '1', '-ar', str(SAMPLE_RATE), '-'],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _rms(samples, start: float, end: float) -> float:
    import numpy as np
    chunk = samples[int(start * SAMPLE_RATE):max(int(end * SAMPLE_RATE), int(start * SAMPLE_RATE) + 1)]
    return float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0


def _overlap_ratio(a: dict, b: dict) -> float:
    overlap = min(a['end'], b['end']) - max(a['start'], b['start'])
    shorter = min(a['end'] - a['start'], b['end'] - b['start'])
    return overlap / shorter if shorter > 0 else 0.0


def merge_role_segments(tracks: list, min_overlap: float = 0.5, min_similarity: float = 0.6) -> list:
    """
    tracks: [{'role', 'segments' (Whisper segments), 'audio' (path to that mic)}]
    Returns one timeline of segments with `speaker` set to the mic's role. When the same words
    appear on two mics at the same time (mic bleed), only the copy from the mic where they were
    loudest is kept; if about equally loud, the copy that caught more of the words.
    """
    candidates = []
    for track in tracks:
        samples = load_mono(track['audio'])
        for seg in track['segments']:
            text = str(seg.get('text', '')).strip()
            if not text.replace('.', '').strip():
                continue
            candidates.append({
                'start': float(seg['start']), 'end': float(seg['end']), 'text': text,
                'speaker': track['role'], 'speaker_source': 'track',
                'words': seg.get('words') or [],
                '_energy': _rms(samples, float(seg['start']), float(seg['end'])),
            })
    candidates.sort(key=lambda s: s['start'])

    dropped = set()
    for i, a in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            if b['start'] >= a['end']:
                break
            if a['speaker'] == b['speaker'] or i in dropped or j in dropped:
                continue
            similar = SequenceMatcher(None, a['text'].lower(), b['text'].lower()).ratio() >= min_similarity
            if similar and _overlap_ratio(a, b) >= min_overlap:
                louder, quieter = (a, b) if a['_energy'] >= b['_energy'] else (b, a)
                if quieter['_energy'] >= 0.9 * louder['_energy']:
                    louder, quieter = (a, b) if len(a['text']) >= len(b['text']) else (b, a)
                dropped.add(i if quieter is a else j)

    merged = []
    for i, seg in enumerate(candidates):
        if i not in dropped:
            seg.pop('_energy')
            seg['id'] = len(merged)
            merged.append(seg)
    return merged
