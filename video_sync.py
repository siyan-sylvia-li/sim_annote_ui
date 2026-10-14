"""
Lining up a session's video with its separately recorded audio.

The audio recording is the clock the app uses (annotations and transcripts are in audio time),
so this finds how video time relates to it:

    video_time = offset + rate * audio_time

`offset` is where the audio's start falls in the video (negative if the audio started first);
`rate` is 1 unless one device's clock runs slightly fast.

How:
  1. Both soundtracks are reduced to an "onset" envelope (spectral flux): for each 10 ms, how much
     louder it got than the last, summed over 8 frequency bands. Different mics, rooms and
     volumes don't matter much; when people start talking does.
  2. The envelopes are cross-correlated over every possible lag; the best peak is the overall
     offset, and its strength (how far it stands out) is the confidence.
  3. The offset is refined in one-minute windows along the recording and a line is fitted
     through them, which gives the drift (rate).
  4. Separately, spoken lines are matched between the two transcripts (`word_offsets`) as a
     cross-check the caller can compare against.
"""
import subprocess
from difflib import SequenceMatcher

import numpy as np

SAMPLE_RATE = 8000
HOP = 0.01          # envelope resolution: 10 ms


def load_mono(path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any audio/video file to mono float samples (ffmpeg handles .m4v, 24-bit WAVs etc.)."""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-f', 's16le', '-ac', '1',
                          '-ar', str(sample_rate), '-'], check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def mix(signals: list) -> np.ndarray:
    """Sum several mics (per-person recordings) into one track, as a room mic would hear them."""
    out = np.zeros(max(len(s) for s in signals), dtype=np.float32)
    for s in signals:
        out[:len(s)] += s
    return out


def onset_envelope(samples: np.ndarray, sample_rate: int = SAMPLE_RATE, bands: int = 8) -> np.ndarray:
    """
    Spectral flux every 10 ms: the rise in log energy in each of `bands` frequency bands
    (200-3800 Hz, where speech is), summed and normalised. Robust to mic type, room and gain.
    """
    hop = int(sample_rate * HOP)
    size = 4 * hop                       # 40 ms frames, overlapping
    frames = (len(samples) - size) // hop + 1
    if frames < 2:
        return np.zeros(max(frames, 0), dtype=np.float32)
    window = np.hanning(size).astype(np.float32)
    flux = np.zeros(frames, dtype=np.float32)
    freqs = np.fft.rfftfreq(size, 1 / sample_rate)
    edges = np.geomspace(200, min(3800, sample_rate / 2 - 1), bands + 1)
    band_of = np.digitize(freqs, edges) - 1
    previous = None
    # In chunks, so an hour of audio doesn't need a giant spectrogram in memory
    for start in range(0, frames, 20000):
        idx = np.arange(start, min(start + 20000, frames))
        chunk = samples[(idx[:, None] * hop) + np.arange(size)] * window
        power = np.abs(np.fft.rfft(chunk, axis=1)) ** 2
        energy = np.stack([power[:, band_of == b].sum(axis=1) for b in range(bands)], axis=1)
        log_energy = np.log(energy + 1e-6)
        if previous is None:
            previous = log_energy[:1]
        rise = np.diff(np.vstack([previous, log_energy]), axis=0)
        flux[idx] = np.maximum(rise, 0).sum(axis=1)
        previous = log_energy[-1:]
    return (flux - flux.mean()) / (flux.std() + 1e-9)


def _correlate(a: np.ndarray, v: np.ndarray):
    """corr[i] = sum_t a[t] * v[t + lag_i] for every lag where the two overlap; returns (lags, corr)."""
    n = len(a) + len(v) - 1
    size = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(v, size) * np.conj(np.fft.rfft(a, size)), size)
    # Non-negative lags sit at the start, negative lags wrap around to the end
    lags = np.concatenate([np.arange(0, len(v)), np.arange(-(len(a) - 1), 0)])
    values = np.concatenate([corr[:len(v)], corr[size - (len(a) - 1):]])
    order = np.argsort(lags)
    return lags[order], values[order]


def _peak(lags: np.ndarray, corr: np.ndarray):
    """Best lag (sub-frame, by parabolic interpolation) and how far the peak stands out (z-score)."""
    i = int(np.argmax(corr))
    shift = 0.0
    if 0 < i < len(corr) - 1:
        y0, y1, y2 = corr[i - 1], corr[i], corr[i + 1]
        denom = y0 - 2 * y1 + y2
        shift = 0.5 * (y0 - y2) / denom if denom else 0.0
    # Confidence: the peak against everything more than 1 s away from it
    away = np.abs(lags - lags[i]) > int(1 / HOP)
    rest = corr[away] if away.any() else corr
    z = (corr[i] - rest.mean()) / (rest.std() + 1e-9)
    return lags[i] + shift, float(z)


def align(audio_env: np.ndarray, video_env: np.ndarray, window: float = 90.0, step: float = 30.0,
          search: float = 3.0) -> dict:
    """
    Offset and rate such that video_time = offset + rate * audio_time.
    Returns {'offset', 'rate', 'confidence', 'windows' (how many 90-second checks agreed)}.
    """
    lags, corr = _correlate(audio_env, video_env)
    lag, confidence = _peak(lags, corr)
    offset = lag * HOP

    # Refine in windows along the recording: each gives the local offset at its middle
    win, hop_w, srch = int(window / HOP), int(step / HOP), int(search / HOP)
    points = []
    for start in range(0, max(len(audio_env) - win, 0) + 1, hop_w):
        seg = audio_env[start:start + win]
        v_start = start + int(round(lag)) - srch
        v_end = v_start + win + 2 * srch
        if v_start < 0 or v_end > len(video_env) or seg.std() < 1e-6:
            continue   # this part of the audio isn't covered by the video
        w_lags, w_corr = _correlate(seg, video_env[v_start:v_end])
        keep = (w_lags >= 0) & (w_lags <= 2 * srch)
        w_lag, w_z = _peak(w_lags[keep], w_corr[keep])
        if w_z >= 5:
            middle = (start + win / 2) * HOP
            points.append((middle, (v_start + w_lag - start) * HOP))

    rate = 1.0
    if len(points) >= 3:
        t, o = np.array(points).T
        # Fit a line, drop windows far from it, refit with a tighter limit. Measuring against the
        # line (not the median) keeps the early and late windows, which is where drift shows.
        good = np.abs(o - np.median(o)) < 1.0
        for limit in (0.5, 0.2, 0.08):
            if good.sum() < 3:
                break
            slope, intercept = np.polyfit(t[good], o[good], 1)
            good = np.abs(o - (intercept + slope * t)) < limit
        if good.sum() >= 3:
            slope, intercept = np.polyfit(t[good], o[good], 1)
            span = t[good].max() - t[good].min()
            # Real clock drift is tiny (well under 0.1%); anything bigger is a bad fit. Drift that
            # moves the video by under 30 ms over the whole recording is just measurement noise.
            if abs(slope) < 1e-3 and abs(slope) * span >= 0.03:
                offset, rate = float(intercept), 1.0 + float(slope)
            else:
                offset = float(np.median(o[good]))
        points = [p for p, g in zip(points, good) if g]
    return {'offset': float(offset), 'rate': float(rate), 'confidence': confidence, 'windows': len(points)}


def word_offsets(audio_segments: list, video_segments: list, min_chars: int = 15, min_similarity: float = 0.75) -> list:
    """
    Match spoken lines between two transcripts (Whisper segments with start/text). Returns
    [(audio_start, video_start)] for lines that clearly say the same thing in both.
    """
    pairs = []
    norm = lambda t: ' '.join(str(t).lower().split())
    audio = [(s['start'], norm(s['text'])) for s in audio_segments if len(norm(s['text'])) >= min_chars]
    for v in video_segments:
        vt = norm(v['text'])
        if len(vt) < min_chars:
            continue
        best = max(audio, key=lambda a: SequenceMatcher(None, a[1], vt).ratio(), default=None)
        if best and SequenceMatcher(None, best[1], vt).ratio() >= min_similarity:
            pairs.append((float(best[0]), float(v['start'])))
    return pairs


def check_with_words(sync: dict, pairs: list, tolerance: float = 1.0) -> dict:
    """
    Compare an alignment with matched spoken lines. Returns {'matches', 'agrees', 'offset'} where
    `offset` is the word-based offset (median) on its own, for when the sound match looks wrong.
    """
    if not pairs:
        return {'matches': 0, 'agrees': None, 'offset': None}
    a, v = np.array(pairs).T
    errors = v - (sync['offset'] + sync['rate'] * a)
    word_offset = float(np.median(v - a))
    agrees = bool(np.median(np.abs(errors)) < tolerance) if len(pairs) >= 3 else None
    return {'matches': len(pairs), 'agrees': agrees, 'offset': word_offset}
