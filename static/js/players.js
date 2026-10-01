// Media players for the annotation app.
//
// The rest of the app only uses this interface, so the local player can be swapped for
// the Box preview player without touching the annotation code:
//   load() -> Promise (resolves once duration is known)
//   play(), pause(), togglePlay(), isPlaying()
//   seek(seconds), getTime(), getDuration(), setRate(rate)
//   sources, sourceIndex, switchSource(index) -> Promise   (e.g. one person's mic, or one camera)
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
        this.video.addEventListener('click', () => this.togglePlay());
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

    destroy() {
        if (this.preview) this.preview.destroy();
        this.preview = null;
        this.media = null;
        this.container.innerHTML = '';
    }
}

function createPlayer(container, descriptor, sessionId) {
    switch (descriptor.type) {
        case 'html5': return new Html5Player(container, descriptor);
        case 'box': return new BoxPreviewPlayer(container, descriptor, sessionId);
        default: throw new Error(`Unknown player type: ${descriptor.type}`);
    }
}
