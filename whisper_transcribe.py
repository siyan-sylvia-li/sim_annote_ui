import whisper
import torchaudio
import os
from moviepy import VideoFileClip
import tqdm
import json
import torch
from logging import getLogger
import logging
import subprocess

logger = getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def transcribe_with_whisper(file_path: str, segment_dir: str, save_json: bool = True):
    if not file_path.endswith(".wav"):
        file_path_wav = os.path.splitext(file_path)[0] + ".wav"
        if not os.path.exists(file_path_wav):
            logger.info(f"Extracting audio from video file using FFmpeg: {file_path}")
            # Use FFmpeg directly for all video formats to avoid moviepy issues
            try:
                # Extract audio to WAV format suitable for Whisper
                subprocess.run([
                    'ffmpeg', '-i', file_path,
                    '-vn',  # No video
                    '-acodec', 'pcm_s16le',  # 16-bit PCM WAV
                    '-ar', '16000',  # 16kHz sample rate (good for speech/Whisper)
                    '-ac', '1',  # Mono
                    '-y',  # Overwrite output file if exists
                    file_path_wav
                ], check=True, capture_output=True, text=True)
                logger.info(f"Successfully extracted audio to: {file_path_wav}")
            except subprocess.CalledProcessError as e:
                logger.error(f"FFmpeg extraction failed: {e.stderr}")
                raise Exception(f"Failed to extract audio: {e.stderr}")
        file_path = file_path_wav
    else:
        file_path = file_path
    logger.info("Current file path " + file_path)
    
    if not torch.cuda.is_available():
        device = "cpu"
        model = whisper.load_model("small", device=device)
    else:
        device = "cuda"
        model = whisper.load_model("large", device=device)
    
    try:
        # if os.path.exists(f"{segment_dir}/whisper_results.json"):
        #     result = json.load(open(f"{segment_dir}/whisper_results.json"))
        # elif os.path.exists(os.path.join(os.path.dirname(file_path), "whisper_results.json")):
        #     result = json.load(open(os.path.join(os.path.dirname(file_path), "whisper_results.json")))
        # else:
        # verbose=False shows a progress bar instead of printing nothing until it's done
        result = model.transcribe(file_path, word_timestamps=True, verbose=False)
        if save_json:
            json.dump(result, open(f"{segment_dir}/whisper_results.json", "w"))
    except Exception as e:
        logger.info(str(e))
    return result, file_path
