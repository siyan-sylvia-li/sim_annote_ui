# Video Transcription & Speaker Labeling Interface

A web-based interface for transcribing videos using Whisper and labeling speakers in the transcription segments.

## Features

- **Video Upload**: Load videos by providing the full file path
- **Whisper Transcription**: Automatic speech-to-text transcription using OpenAI's Whisper
- **Speaker Labeling**: Manually assign speakers to transcription segments
- **Speaker Identification**: Automated speaker identification using audio analysis
- **Export**: Export labeled segments in JSON format
- **Responsive Interface**: Modern, mobile-friendly web interface

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd inspire_revamp
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Ensure data directories exist**:
   ```bash
   mkdir -p data uploads
   ```

## Usage

1. **Start the application**:
   ```bash
   python app.py
   ```

2. **Open your browser** and navigate to `http://localhost:5000`

3. **Load a video**:
   - Enter the full path to your video file
   - Click "Load Video"

4. **Transcribe with Whisper**:
   - Click "Transcribe with Whisper"
   - Wait for the transcription to complete

5. **Label speakers**:
   - Click "Assign Speaker" on any segment
   - Select a speaker from the list or add new speakers
   - Continue labeling segments as needed

6. **Run speaker identification** (optional):
   - After labeling some segments, click "Run Speaker Identification"
   - This will attempt to automatically assign speakers to remaining segments

7. **Export labels**:
   - Click "Export Labels" to download the labeled segments as JSON

## File Structure

```
inspire_revamp/
├── app.py                      # Flask application
├── whisper_transcribe.py       # Whisper transcription module
├── speaker_identification.py   # Speaker identification module
├── templates/
│   └── index.html             # Main HTML template
├── static/
│   ├── css/
│   │   └── style.css          # Custom styles
│   └── js/
│       └── app.js             # Frontend JavaScript
├── data/                       # Data storage directory
├── uploads/                    # Upload directory
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

## API Endpoints

- `GET /` - Main interface
- `POST /whisper_transcribe` - Transcribe video with Whisper
- `POST /speaker_identification` - Run speaker identification
- `GET /get_segments` - Get transcription segments
- `POST /update_segment_speaker` - Update speaker for a segment
- `GET /export_labels` - Export labeled segments

## Configuration

The application uses the following default settings:
- **Port**: 5000
- **Host**: 0.0.0.0 (accessible from any network)
- **Upload folder**: `uploads/`
- **Data folder**: `data/`

## Dependencies

- **Flask**: Web framework
- **OpenAI Whisper**: Speech recognition
- **PyTorch/TorchAudio**: Audio processing
- **MoviePy**: Video processing
- **scikit-learn**: Machine learning utilities
- **librosa**: Audio analysis

## Troubleshooting

1. **Import errors**: Ensure all dependencies are installed with `pip install -r requirements.txt`

2. **Video loading issues**: Verify the video file path is correct and accessible

3. **Transcription failures**: Check that the video file is in a supported format (MP4, AVI, MOV, etc.)

4. **Memory issues**: Large video files may require significant RAM for processing

## License

This project is provided as-is for educational and research purposes.

## Contributing

Feel free to submit issues, feature requests, or pull requests to improve the application.

## CLC Annotation App (replaces the CLC Excel recording sheet)

`annotate_app.py` is a separate app for closed-loop communication (CLC) coding. Raters play a session recording, mark call-outs on a timeline and fill in the same columns as the "Closed Loop Communication Data Recording Sheet". Each rater's work is saved as one JSON file per recording.

### Running it with Box (raters, on your own laptop)

Recordings stream from Box through Box's own viewer with a preview-only token, so nothing is downloaded and **view access to the Box folder is enough**.

1. Once, in Terminal (Mac): `git clone -b clc-annotation-app https://github.com/siyan-sylvia-li/sim_annote_ui.git ~/sim_annote_ui && open ~/sim_annote_ui`
2. Every time: double-click **Start CLC Tool.command** in that folder. It updates the tool (`git pull`), asks for the settings the first time (the `BOX_` lines from the project lead's email, copied to the clipboard) and saves them as `.env`, installs `flask` and `requests` if needed, starts the app and opens `http://localhost:5050`.
3. Sign in with Box, pick a recording, enter your rater name and click **Load**.

(Or by hand: put the settings in `.env`, see `.env.example`, then `python annotate_app.py --storage box`.)

Annotations autosave **on your laptop** in `data/annotations/<recording>/<rater>.json` (git-ignored); **Export JSON** downloads a copy. Unsaved work is also kept in the browser until the save is confirmed, and you are offered to restore it if it didn't make it.

**Raters vs. project lead.** Raters' settings have the recordings and transcripts folders. The project lead's settings also have `BOX_VIDEO_SESSIONS_FOLDER_ID`, which turns on the buttons for preparing recordings (**Copy Colab key**, **Import transcript**, **Copy video sync key**, **Re-sync**); raters don't see them and just get the shared transcripts and synced videos from Box.

**Box app setup (project lead, once):** with a free [Box Developer account](https://account.box.com/signup/developer) (a regular Box account can't save these settings), create a *Platform App → User Authentication (OAuth 2.0)*. Set the redirect URI to `http://localhost:5050/box/callback`, add `http://localhost:5050` to *CORS Domains* (the viewer needs it), and enable *Read all files and folders*. Raters still sign in with their own Box accounts; the developer account only holds the app's settings.

### Recordings

The app lists **recordings**, grouped automatically from the files in the Box folder (`recordings.py`):

- **A single video or WAV** with the whole conversation: one recording per file. A folder with several such files (e.g. several scenario runs) gives several recordings.
- **One WAV per person's mic**, named like `Audio 4 TL#02.wav`: all files with the same `#NN` in a folder form one recording. The **Listen to** menu switches between people's mics at the same timestamp. Role codes map to names via `role_aliases` in `schemes/clc.json` (TL → Team Leader, MED RN → Med Nurse, DOC RN → Documenting Nurse, RT → Airway, CPR / CPR2 → CPR Personnel / CPR Personnel 2).
- **Several camera angles**, named alike except `_C200_`, `_C201_`, …: one recording with a camera picker.

### Transcripts

Whisper needs a GPU, so transcripts are made on Google Colab and then imported:

1. In the app (signed in with Box), open the recording and click **Copy Colab key**. The key lets Colab download only that recording's files, for about an hour. The app first checks that Box allows your account to download them.
2. Open `colab_transcribe.ipynb` in Colab, switch to a GPU runtime (**Runtime → Change runtime type → T4 GPU**), run the cells and paste the key when asked. It fetches the audio from Box, runs Whisper (`whisper_transcribe.py`), deletes the audio and produces a `.transcript.json`.
3. With shared transcripts set up (below), Colab adds the transcript to Box and every rater gets it when they load the recording. Otherwise, download the file and use **Import transcript** in the app.

**Shared transcripts:** set `BOX_TRANSCRIPTS_FOLDER_ID` in `.env` to a Box folder that whoever makes transcripts can edit and raters can view. The app then loads each recording's transcript from that folder, so all raters work from the same one. The first transcript for a recording (uploaded by Colab, or by **Import transcript** when none exists yet) is the shared one and is never replaced. Speaker changes a rater makes stay on their laptop.

Recordings with one mic per person get their speakers from the file names; speech picked up on other people's mics is kept only where it was loudest (`transcription.py`). For single-mic recordings, assign speakers in the app. `colab_transcribe.py --audio <files>` also works on audio already on the machine.

**Import transcript** also accepts plain Whisper output (with `segments`) or the labels exported from the original app. It appears under the player; click a segment (Shift+click for a range) to select it, and assign speakers from the menu on each segment. Labels created from selected segments store their ids (`segment_ids`), linking each call-out and check back to its utterances.

### Videos (syncing a session's video with its audio)

Each session's camera angles are in a zip in the sessions folder (`BOX_VIDEO_SESSIONS_FOLDER_ID`), and the video and the audio recording don't start at the same moment. Linking them is done once per audio recording, on Colab:

1. In the app, open the audio recording and click **Copy video sync key** (under the player). The app finds the zip in the session folder with the same session number, checks your Box account can download it and add files next to the audio, and asks you to confirm the match.
2. Open `colab_sync.ipynb` in Colab (GPU runtime), run the cells and paste the key. `colab_sync.py` downloads the audio and the zip, unzips the camera files, and works out the offset (and any clock drift) between video and audio by matching their sound (`video_sync.py`), cross-checked by matching spoken lines with Whisper. It then adds a sync file next to the shared transcripts and the camera files next to the audio. Nothing is downloaded to the laptop, and Colab's copy is deleted at the end.
3. Load the recording again: the video plays above the timeline, muted, with sound from the audio recording (switchable to the camera), and a camera picker. Annotations and transcripts stay in audio time.

If the alignment couldn't be confirmed, the app shows a note under the video so someone can check it by eye. Running step 2 again adds a newer sync file (the newest is used) and skips camera files already in Box.

### Development with local files

```bash
python annotate_app.py --media-root /path/to/recordings
```

Annotations go to `data/annotations/` (change with `--annotations-root`).

### Using the app

- **Selecting**: drag on the timeline, or click transcript segments, then press `N` / `I` / `T` / `C` or use the buttons that appear.
- **Coding scheme**: `schemes/clc.json` defines the columns, options, help text (from the CLC Audio Tagging Training Guide) and consistency warnings. The form, the timeline and the table are all generated from it.
- **Loops table**: the **Loops table** button opens all call-outs in the recording sheet's layout; cells there can be edited too.
- **Shortcuts**: `Space` play/pause · `N` new call-out · `I` / `T` / `C` add Information Check Back / Task Completion Check Back / Loop Closure at the current time · `←`/`→` seek 5s (Shift: 1s) · `[` / `]` previous/next call-out.
