// Media players for the annotation app.
//
// The rest of the app only uses this interface, so the local player can be swapped for
// the Box preview player without touching the annotation code:
//   load() -> Promise (resolves once duration is known)
//   play(), pause(), togglePlay(), isPlaying()
//   seek(seconds), getTime(), getDuration(), setRate(rate)
//   sources, sourceIndex, switchSource(index) -> Promise   (e.g. one person's mic, or one camera)
//   setMuted(muted), onClick (optional: what clicking the picture does)
//   destroy()
// A recording's sources all share one clock, so switching keeps the current time.

class Html5Player {
    constructor(container, descriptor) {
        this.container = container;
        this.sources = descriptor.sources;
        this.sourceIndex = 0;
        this.video = null;
        this.rate = 1;
    }

    load() {
        this.container.innerHTML = '';
        this.video = document.createElement('video');
        this.video.preload = 'auto';
        this.video.controls = false;
        this.video.addEventListener('click', () => (this.onClick || (() => this.togglePlay()))());
        this.container.appendChild(this.video);
        return this._open(0);
    }

    _open(startAt) {
        this.video.src = this.sources[this.sourceIndex].src;
        return new Promise((resolve, reject) => {
            this.video.addEventListener('loadedmetadata', () => {
                this.video.playbackRate = this.rate;
                if (startAt) this.seek(startAt);
                resolve();
            }, { once: true });
            this.video.addEventListener('error', () => reject(new Error('Could not load media')), { once: true });
        });
    }

    async switchSource(index) {
        const t = this.getTime();
        const wasPlaying = this.isPlaying();
        this.sourceIndex = index;
        await this._open(t);
        if (wasPlaying) this.play();
    }

    play() { this.video.play(); }
    pause() { this.video.pause(); }
    togglePlay() { this.isPlaying() ? this.pause() : this.play(); }
    isPlaying() { return !this.video.paused && !this.video.ended; }
    seek(seconds) { this.video.currentTime = Math.max(0, Math.min(seconds, this.getDuration())); }
    getTime() { return this.video.currentTime; }
    getDuration() { return this.video.duration || 0; }

    setRate(rate) {
        this.rate = rate;
        this.video.playbackRate = rate;
    }

    setMuted(muted) { this.video.muted = muted; }

    destroy() {
        if (this.video) {
            this.video.pause();
            this.video.removeAttribute('src');
            this.video.load();
        }
        this.container.innerHTML = '';
    }
}

// Plays Box files through Box Content Preview (https://github.com/box/box-content-preview),
// so media streams from Box with a preview-only token and is never downloaded.
// Box documents `startAt` for the initial position; for seeking and reading the time we use
// the media viewer's underlying element (`viewer.mediaEl`), which Box's own controls also
// drive. If a future Preview version stops exposing it, seek() falls back to reopening the
// preview at the new time.
// The standalone Preview library on Box's CDN (newer npm 3.x builds aren't published there)
const BOX_PREVIEW_VERSION = '2.111.0';
const BOX_PREVIEW_BASE = `https://cdn01.boxcdn.net/platform/preview/${BOX_PREVIEW_VERSION}/en-US`;

function loadBoxPreviewSdk() {
    if (window.Box && window.Box.Preview) return Promise.resolve();
    if (!loadBoxPreviewSdk.promise) {
        loadBoxPreviewSdk.promise = new Promise((resolve, reject) => {
            const css = document.createElement('link');
            css.rel = 'stylesheet';
            css.href = `${BOX_PREVIEW_BASE}/preview.css`;
            document.head.appendChild(css);
            const script = document.createElement('script');
            script.src = `${BOX_PREVIEW_BASE}/preview.js`;
            script.onload = () => resolve();
            script.onerror = () => {
                loadBoxPreviewSdk.promise = null;  // allow a retry
                reject(new Error(`Could not load the Box viewer from ${script.src}`));
            };
            document.head.appendChild(script);
        });
    }
    return loadBoxPreviewSdk.promise;
}

class BoxPreviewPlayer {
    constructor(container, descriptor, sessionId) {
        this.container = container;
        this.sources = descriptor.sources;
        this.sourceIndex = 0;
        this.sessionId = sessionId;
        this.preview = null;
        this.media = null;
        this.rate = 1;
        this.lastTime = 0;
    }

    fileId() { return this.sources[this.sourceIndex].file_id; }

    // Preview asks for a token whenever it needs one, so a long annotation session gets a
    // fresh preview-only token instead of failing when the first one expires
    async token(fileId) {
        const resp = await fetch(`/api/player/token?${new URLSearchParams({ session: this.sessionId, file: fileId })}`);
        if (!resp.ok) throw new Error('Could not get a Box preview token');
        return (await resp.json()).token;
    }

    async load(startAt = 0) {
        await loadBoxPreviewSdk();
        if (this.preview) this.preview.destroy();
        this.container.innerHTML = '';
        this.media = null;
        const fileId = this.fileId();
        this.preview = new window.Box.Preview();
        const loaded = new Promise((resolve, reject) => {
            this.preview.addListener('load', data => {
                if (data && data.error) { reject(new Error(data.error.message || 'Box could not preview this file')); return; }
                const viewer = (data && data.viewer) || this.preview.getCurrentViewer();
                this.media = viewer && viewer.mediaEl;
                if (!this.media) { reject(new Error('This Box file did not open in the media player')); return; }
                this.media.playbackRate = this.rate;
                this.media.muted = Boolean(this.muted);
                if (this.media.readyState >= 1) resolve();
                else this.media.addEventListener('loadedmetadata', () => resolve(), { once: true });
            });
        });
        this.preview.show(fileId, () => this.token(fileId), {
            container: this.container,
            header: 'none',
            showDownload: false,
            fileOptions: { [fileId]: { startAt: { unit: 'seconds', value: Math.floor(startAt) } } },
        });
        return loaded;
    }

    async switchSource(index) {
        const t = this.getTime();
        const wasPlaying = this.isPlaying();
        this.sourceIndex = index;
        await this.load(t);
        // startAt is whole seconds; land on the exact time
        if (this.media) this.media.currentTime = t;
        if (wasPlaying) this.play();
    }

    play() { if (this.media) this.media.play(); }
    pause() { if (this.media) this.media.pause(); }
    togglePlay() { this.isPlaying() ? this.pause() : this.play(); }
    isPlaying() { return Boolean(this.media && !this.media.paused && !this.media.ended); }

    seek(seconds) {
        const t = Math.max(0, Math.min(seconds, this.getDuration() || seconds));
        if (this.media) this.media.currentTime = t;
        else this.load(t);
    }

    getTime() {
        if (this.media) this.lastTime = this.media.currentTime;
        return this.lastTime;
    }

    getDuration() { return (this.media && this.media.duration) || 0; }

    setRate(rate) {
        this.rate = rate;
        if (this.media) this.media.playbackRate = rate;
    }

    setMuted(muted) {
        this.muted = muted;   // re-applied when the preview reloads (switching camera)
        if (this.media) this.media.muted = muted;
    }

    destroy() {
        if (this.preview) this.preview.destroy();
        this.preview = null;
        this.media = null;
        this.container.innerHTML = '';
    }
}

// A recording's audio plus its synced video (made by colab_sync.py). The audio is the clock:
// time, seeking and annotations are all in audio time, and the video follows it using
// video_time = offset + rate * audio_time. Sound comes from the audio unless switched to the
// camera; where the video doesn't cover the audio, the picture shows a "no video" note.
class SyncedPlayer {
    constructor(audio, video, sync, gapEl) {
        this.audio = audio;
        this.video = video;
        this.offset = sync.offset;
        this.rate = sync.rate || 1;
        this.gapEl = gapEl;
        this.sound = 'audio';
        this.timer = null;
        this.playbackRate = 1;
        this.videoError = null;
        // Clicking the picture plays/pauses everything, not just the video
        audio.onClick = video.onClick = () => this.togglePlay();
    }

    get sources() { return this.audio.sources; }
    get sourceIndex() { return this.audio.sourceIndex; }
    get cameras() { return this.video.sources; }
    get cameraIndex() { return this.video.sourceIndex; }

    async load() {
        await this.audio.load();
        // A video that won't load shouldn't stop annotation: carry on with the audio alone
        try { await this.video.load(); } catch (err) { this.videoError = err; }
        this.applySound();
        this.timer = setInterval(() => this.follow(false), 200);
        this.follow(true);
    }

    videoTime(audioTime) { return this.offset + this.rate * audioTime; }

    // Keep the video where the audio says it should be (within 0.15 s), and running when the audio runs
    follow(force) {
        if (this.videoError) return;
        const vt = this.videoTime(this.audio.getTime());
        const inside = vt >= 0 && vt < (this.video.getDuration() || Infinity) - 0.05;
        if (this.gapEl) this.gapEl.classList.toggle('d-none', inside);
        this.applySound(inside);
        if (!inside) {
            if (this.video.isPlaying()) this.video.pause();
            if (force && this.video.getDuration()) this.video.seek(vt < 0 ? 0 : this.video.getDuration());
            return;
        }
        if (force || Math.abs(this.video.getTime() - vt) > 0.15) this.video.seek(vt);
        if (this.audio.isPlaying() && !this.video.isPlaying()) this.video.play();
        if (!this.audio.isPlaying() && this.video.isPlaying()) this.video.pause();
    }

    // Camera sound only while the camera has footage; otherwise fall back to the audio
    applySound(inside = true) {
        const useCamera = this.sound === 'video' && inside && !this.videoError;
        this.audio.setMuted(useCamera);
        this.video.setMuted(!useCamera);
    }

    setSound(which) { this.sound = which; this.follow(false); }

    play() { this.audio.play(); this.follow(true); }
    pause() { this.audio.pause(); this.video.pause(); }
    togglePlay() { this.isPlaying() ? this.pause() : this.play(); }
    isPlaying() { return this.audio.isPlaying(); }
    seek(seconds) { this.audio.seek(seconds); this.follow(true); }
    getTime() { return this.audio.getTime(); }
    getDuration() { return this.audio.getDuration(); }

    setRate(rate) {
        this.playbackRate = rate;
        this.audio.setRate(rate);
        this.video.setRate(rate * this.rate);
    }

    setMuted(muted) { this.audio.setMuted(muted); }

    async switchSource(index) {
        await this.audio.switchSource(index);
        this.follow(true);
    }

    async switchCamera(index) {
        await this.video.switchSource(index);
        this.video.setRate(this.playbackRate * this.rate);
        this.follow(true);
    }

    destroy() {
        clearInterval(this.timer);
        this.audio.destroy();
        this.video.destroy();
    }
}

function createPlayer(container, descriptor, sessionId) {
    switch (descriptor.type) {
        case 'html5': return new Html5Player(container, descriptor);
        case 'box': return new BoxPreviewPlayer(container, descriptor, sessionId);
        default: throw new Error(`Unknown player type: ${descriptor.type}`);
    }
}
