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


def suppress_beats(envelope: np.ndarray, min_period: float = 0.3, max_period: float = 1.5, threshold: float = 0.1) -> np.ndarray:
    """
    Remove a steady beat (CPR compressions, a metronome, monitor beeps) from an onset envelope.
    A beat makes the soundtracks line up equally well one beat early or late; speech doesn't
    repeat on a fixed rhythm, so it survives. The beat period is found from the envelope's
    autocorrelation; each frame then has the average of the frames one period either side
    subtracted (a comb filter), blurred over +-1 frame since a beat is never perfectly steady.
    """
    e = envelope - envelope.mean()
    n = len(e)
    lo, hi = int(min_period / HOP), int(max_period / HOP)
    if n < 4 * hi:
        return envelope
    spectrum = np.fft.rfft(e, 2 * n)
    auto = np.fft.irfft(spectrum * np.conj(spectrum))[:hi + 1]
    auto /= auto[0] + 1e-9
    period = lo + int(np.argmax(auto[lo:hi + 1]))
    if auto[period] < threshold:
        return envelope            # no steady beat
    neighbours = np.zeros_like(e)
    for p in (period - 1, period, period + 1):
        neighbours += np.roll(e, p) + np.roll(e, -p)
    out = e - neighbours / 6
    return (out - out.mean()) / (out.std() + 1e-9)


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


def _windowed(audio_env: np.ndarray, video_env: np.ndarray, lag: float, window: float, step: float, search: float) -> list:
    """Local offset (seconds) at the middle of each window along the audio, searching +-`search` s around `lag` (frames)."""
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
            points.append(((start + win / 2) * HOP, (v_start + w_lag - start) * HOP))
    return points


def _fit(points: list, fallback_offset: float):
    """Offset and rate from window offsets: a line fitted with outliers dropped. Returns (offset, rate, windows used)."""
    offset, rate = fallback_offset, 1.0
    if len(points) < 3:
        return offset, rate, len(points)
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
    return offset, rate, int(good.sum())


def _top_peaks(lags: np.ndarray, corr: np.ndarray, count: int = 5, spacing: float = 0.25) -> list:
    """The strongest correlation peaks at least `spacing` s apart: [(offset s, z-score)]; for diagnosing."""
    z = (corr - corr.mean()) / (corr.std() + 1e-9)
    order = np.argsort(corr)[::-1]
    peaks = []
    for i in order:
        if all(abs(lags[i] - p) * HOP >= spacing for p, _ in peaks):
            peaks.append((lags[i], z[i]))
        if len(peaks) == count:
            break
    return [(round(float(p * HOP), 3), round(float(v), 1)) for p, v in peaks]


def align(audio_env: np.ndarray, video_env: np.ndarray, window: float = 90.0, step: float = 30.0,
          search: float = 3.0) -> dict:
    """
    Offset and rate such that video_time = offset + rate * audio_time, searching every lag.
    Returns {'offset', 'rate', 'confidence', 'windows' (how many 90-second checks agreed), 'peaks'}.
    """
    lags, corr = _correlate(audio_env, video_env)
    lag, confidence = _peak(lags, corr)
    offset, rate, windows = _fit(_windowed(audio_env, video_env, lag, window, step, search), lag * HOP)
    return {'offset': float(offset), 'rate': float(rate), 'confidence': confidence, 'windows': windows,
            'peaks': _top_peaks(lags, corr)}


def refine_near(audio_env: np.ndarray, video_env: np.ndarray, guess: float, search: float = 1.5,
                window: float = 90.0, step: float = 30.0) -> dict:
    """
    Precise offset/rate by matching the sound only within +-`search` s of `guess` (e.g. from matched
    spoken lines). Repeating sounds (beeps, compressions) can make the full search pick a spot one
    beat off; anchoring near a rough estimate avoids that.
    """
    offset, rate, windows = _fit(_windowed(audio_env, video_env, guess / HOP, window, step, search), guess)
    return {'offset': float(offset), 'rate': float(rate), 'windows': windows}


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
    # Compare the typical (median) error, not the typical size of the error: a phrase said several
    # times ("check a pulse") can be matched to the wrong occurrence, giving a few huge errors
    agrees = bool(abs(np.median(errors)) < tolerance) if len(pairs) >= 3 else None
    return {'matches': len(pairs), 'agrees': agrees, 'offset': word_offset}
