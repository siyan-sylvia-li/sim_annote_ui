"""
How session files become annotatable recordings.

The sessions come in three shapes:
  * one video                                   -> one recording ('video')
  * one WAV with the whole conversation         -> one recording ('audio'); a session can hold
                                                   several of these (one per scenario run)
  * one WAV per person's mic, named like        -> one recording per take ('role_tracks'):
    "Audio 4 TL#02.wav" (channel, role, take)      every "...#02" file in the folder together
  * several camera angles of one recording,      -> one recording ('video') with a camera
    named alike except "_C200_" / "_C201_" ...     picker

Recordings with several files play one file at a time; the player can switch between them
(one person's mic, or one camera) at the same timestamp.
"""
import re
from pathlib import Path

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm'}
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.m4a'}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

# "Audio 4 TL#02.wav" -> channel 4, role "TL", take "02"
ROLE_TRACK = re.compile(r'^Audio\s*(?P<channel>\d+)\s+(?P<role>.+?)\s*#(?P<take>\d+)\.(?:wav|mp3|m4a)$', re.IGNORECASE)

# "OR_260915_1646_C201_a1d1....m4v" -> recording "OR_260915_1646" (+ same suffix), camera "C201"
CAMERA_ANGLE = re.compile(r'^(?P<prefix>.+?)_(?P<camera>C\d{2,4})_(?P<suffix>[^.]+)\.(?:mp4|mov|m4v|webm)$', re.IGNORECASE)


def media_kind(name: str):
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    return None


def _natural_key(text: str):
    # "Session 2" sorts before "Session 10", and "Session3" sorts with "Session 3"
    return [int(part) if part.isdigit() else part.lower().replace(' ', '') for part in re.split(r'(\d+)', text)]


def role_name(role: str, aliases: dict) -> str:
    """Display name for a filename role code, e.g. 'MED RN' -> 'Med Nurse'."""
    return aliases.get(role.strip().upper(), role.strip())


def group_recordings(files: list, aliases: dict) -> list:
    """
    files: [{'id', 'name', 'folder' (display path, '' for the root), 'folder_key'}]
    Returns recordings: [{'id', 'name', 'kind', 'files': [{'id', 'name', 'role', 'channel'}]}].
    Single-file recordings use the file id as their id; role-track recordings use
    '<folder_key>#<take>'.
    """
    recordings = []
    by_folder = {}
    for f in files:
        if media_kind(f['name']):
            by_folder.setdefault(f['folder_key'], []).append(f)

    for folder_key, folder_files in by_folder.items():
        takes = {}
        for f in folder_files:
            m = ROLE_TRACK.match(f['name'])
            if m:
                takes.setdefault(m.group('take'), []).append((f, m))
        grouped = set()
        for take, members in takes.items():
            # A lone "Audio 1 X#05.wav" is just a single recording
            if len(members) < 2:
                continue
            members.sort(key=lambda fm: int(fm[1].group('channel')))
            folder = members[0][0]['folder']
            recordings.append({
                'id': f"{folder_key}#{take}",
                'name': f"{folder + ' / ' if folder else ''}#{take} ({len(members)} mics)",
                'kind': 'role_tracks',
                'files': [{'id': f['id'], 'name': f['name'], 'role': role_name(m.group('role'), aliases),
                           'channel': int(m.group('channel'))} for f, m in members],
            })
            grouped.update(f['id'] for f, _ in members)
        cameras = {}
        for f in folder_files:
            m = CAMERA_ANGLE.match(f['name'])
            if m and f['id'] not in grouped:
                cameras.setdefault((m.group('prefix'), m.group('suffix')), []).append((f, m))
        for (prefix, _), members in cameras.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda fm: fm[1].group('camera'))
            folder = members[0][0]['folder']
            recordings.append({
                'id': f"{folder_key}#{prefix}",
                'name': f"{folder + ' / ' if folder else ''}{prefix} ({len(members)} cameras)",
                'kind': 'video',
                'files': [{'id': f['id'], 'name': f['name'], 'role': None, 'channel': None,
                           'label': f"Camera {m.group('camera').upper()}"} for f, m in members],
            })
            grouped.update(f['id'] for f, _ in members)
        for f in folder_files:
            if f['id'] not in grouped:
                recordings.append({
                    'id': f['id'],
                    'name': f"{f['folder'] + ' / ' if f['folder'] else ''}{f['name']}",
                    'kind': media_kind(f['name']),
                    'files': [{'id': f['id'], 'name': f['name'], 'role': None, 'channel': None}],
                })
    return sorted(recordings, key=lambda r: _natural_key(r['name']))
