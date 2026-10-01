// CLC annotation app.
//
// Everything about the coding scheme (columns, options, validation rules) comes from
// schemes/clc.json; this file only knows the field *types* (choice, timestamps, notes).
// One loop = one row of the old Excel recording sheet.

const SCHEME_ID = 'clc';
const SAVE_DEBOUNCE_MS = 800;
const TICK_MS = 100;

const state = {
    scheme: null,
    sessionId: null,
    rater: null,
    doc: null,            // annotation document being edited
    player: null,
    transcript: null,     // imported transcript: {segments, speakers}
    activeLoopId: null,
    selection: null,      // {start, end, segmentIds} from the timeline or transcript
    playUntil: null,      // stop time when playing a selection
    dirty: false,
    saving: false,
    saveTimer: null,
};

// ---------- Utilities ----------

function $(id) { return document.getElementById(id); }

function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return node;
}

function formatTime(seconds, withTenths = false) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '–';
    const mins = Math.floor(seconds / 60);
    const secs = seconds - mins * 60;
    if (withTenths) return `${mins}:${secs.toFixed(1).padStart(4, '0')}`;
    return `${mins}:${Math.floor(secs).toString().padStart(2, '0')}`;
}

// Accepts "m:ss", "m:ss.s", "h:mm:ss" or plain seconds
function parseTime(text) {
    text = String(text).trim();
    if (!text) return null;
    const parts = text.split(':').map(Number);
    if (parts.some(isNaN)) return null;
    return parts.reduce((total, part) => total * 60 + part, 0);
}

function roundTime(t) { return Math.round(t * 10) / 10; }

function uid() {
    return (crypto.randomUUID && crypto.randomUUID()) || `l${Date.now()}${Math.random().toString(16).slice(2)}`;
}

function toast(message, type = 'info') {
    const node = $('toast');
    node.textContent = message;
    node.className = `app-toast show ${type}`;
    clearTimeout(node._timer);
    // Errors stay up longer so there's time to read (or copy) them
    node._timer = setTimeout(() => { node.className = 'app-toast'; }, type === 'danger' ? 8000 : 2500);
}

// All API calls go through here: a 401 asking for login means the Box sign-in has expired
async function fetchApi(url, options) {
    const resp = await fetch(url, options);
    if (resp.status === 401) {
        const data = await resp.clone().json().catch(() => ({}));
        if (data.login) showSignIn(data.error);
    }
    return resp;
}

function showSignIn(message) {
    $('workspace').classList.add('d-none');
    $('emptyState').classList.remove('d-none');
    $('emptyState').innerHTML = '';
    $('emptyState').appendChild(el('div', {},
        el('i', { class: 'fas fa-box-archive fa-3x mb-3' }),
        el('p', { text: message && message !== 'Sign in with Box' ? message : 'Sign in with your Columbia Box account to open the session videos.' }),
        el('a', { class: 'btn btn-primary', href: '/box/login' }, el('i', { class: 'fas fa-right-to-bracket' }), ' Sign in with Box')));
    for (const id of ['sessionSelect', 'raterInput', 'loadBtn', 'exportBtn', 'tableBtn']) $(id).disabled = true;
}

function isTyping(event) {
    const tag = event.target.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || event.target.isContentEditable;
}

// ---------- Scheme helpers ----------

function fields() { return state.scheme.fields; }

function timestampLists() {
    // Flat list of {field, list, path} for every timestamp column, e.g. path "verbal_check_back.information"
    const lists = [];
    for (const field of fields()) {
        if (field.type !== 'timestamps') continue;
        for (const list of field.lists) {
            lists.push({ field, list, path: field.lists.length === 1 ? field.key : `${field.key}.${list.key}` });
        }
    }
    return lists;
}

// A field with a single timestamp list stores the array directly (loop.loop_closure = [...]);
// with several lists it stores an object of arrays (loop.verbal_check_back.information = [...]).
function getPath(loop, path) {
    return path.split('.').reduce((obj, key) => (obj == null ? undefined : obj[key]), loop);
}

function getList(loop, path) { return getPath(loop, path); }

function newLoop(start, end = null, segmentIds = []) {
    const loop = {
        id: uid(),
        callout: { number: null, start: roundTime(start), end: end === null ? null : roundTime(end), summary: '', segment_ids: segmentIds },
    };
    for (const field of fields()) {
        if (field.type === 'choice') loop[field.key] = null;
        else if (field.type === 'timestamps') {
            if (field.lists.length === 1) loop[field.key] = [];
            else loop[field.key] = Object.fromEntries(field.lists.map(l => [l.key, []]));
        } else if (field.type === 'notes') loop[field.key] = { tags: [], text: '' };
    }
    return loop;
}

function isEmpty(value) {
    return value === null || value === undefined || value === '' || (Array.isArray(value) && value.length === 0);
}

function checkCondition(loop, cond, isExpectation) {
    if (cond.any) return cond.any.some(c => checkCondition(loop, c, isExpectation));
    const value = getPath(loop, cond.field);
    // Unanswered fields are reported as "missing", so don't also flag them as rule violations
    if (isExpectation && isEmpty(value) && (cond.equals !== undefined || cond.not_in)) return true;
    if (cond.equals !== undefined) return value === cond.equals;
    if (cond.not_in) return !cond.not_in.includes(value);
    if (cond.empty !== undefined) return isEmpty(value) === cond.empty;
    if (cond.max_count !== undefined) return (value || []).length <= cond.max_count;
    return true;
}

function loopWarnings(loop) {
    return (state.scheme.rules || [])
        .filter(rule => rule.when.every(c => checkCondition(loop, c, false)) && !rule.expect.every(c => checkCondition(loop, c, true)))
        .map(rule => rule.message);
}

function missingFields(loop) {
    return fields().filter(f => f.required && isEmpty(loop[f.key])).map(f => f.label);
}

// ---------- Loops ----------

function sortedLoops() {
    return [...state.doc.loops].sort((a, b) => a.callout.start - b.callout.start);
}

// Call-out numbers follow time order, so a loop added out of order slots in correctly
function renumber() {
    sortedLoops().forEach((loop, i) => { loop.callout.number = i + 1; });
}

function activeLoop() {
    return state.doc ? state.doc.loops.find(l => l.id === state.activeLoopId) || null : null;
}

function setActive(loopId, { seek = false } = {}) {
    state.activeLoopId = loopId;
    const loop = activeLoop();
    if (seek && loop) state.player.seek(loop.callout.start);
    renderAll();
    if (loop) {
        const row = document.querySelector(`#loopTable tr[data-loop="${loop.id}"]`);
        // Scroll only the table's own container; scrollIntoView would also scroll the page
        const container = row && row.closest('.table-scroll');
        if (container) {
            const head = container.querySelector('thead').offsetHeight;
            if (row.offsetTop - head < container.scrollTop) container.scrollTop = row.offsetTop - head;
            else if (row.offsetTop + row.offsetHeight > container.scrollTop + container.clientHeight) {
                container.scrollTop = row.offsetTop + row.offsetHeight - container.clientHeight;
            }
        }
    }
}

function createLoop(start, end = null, segmentIds = []) {
    const loop = newLoop(start, end, segmentIds);
    // Pre-fill the summary with the call-out's words when it came from the transcript
    if (segmentIds.length) loop.callout.summary = segmentsById(segmentIds).map(s => s.text).join(' ');
    state.doc.loops.push(loop);
    renumber();
    state.activeLoopId = loop.id;
    changed();
    toast(`Call-out #${loop.callout.number} at ${formatTime(start)}`, 'success');
}

function deleteLoop(loop) {
    if (!confirm(`Delete call-out #${loop.callout.number} (${formatTime(loop.callout.start)})?`)) return;
    state.doc.loops = state.doc.loops.filter(l => l.id !== loop.id);
    state.activeLoopId = null;
    renumber();
    changed();
}

function addTimestamp(path, time, segmentIds = []) {
    const loop = activeLoop();
    if (!loop) {
        toast('Open a call-out first (press N or click one on the timeline)', 'warning');
        return;
    }
    const list = getList(loop, path);
    list.push({ time: roundTime(time), note: '', segment_ids: segmentIds });
    list.sort((a, b) => a.time - b.time);
    changed();
    const entry = timestampLists().find(t => t.path === path);
    toast(`${entry.list.label} at ${formatTime(time)} → call-out #${loop.callout.number}`, 'success');
}

// Time to use for "mark" actions: start of the selection if there is one, else the playhead
function markTime() {
    return state.selection ? state.selection.start : state.player.getTime();
}

// Transcript segments to link to a new label: the selected ones, if any
function markSegments() {
    return state.selection ? state.selection.segmentIds.slice() : [];
}

function stepLoop(direction) {
    const loops = sortedLoops();
    if (!loops.length) return;
    const idx = loops.findIndex(l => l.id === state.activeLoopId);
    let next;
    if (idx === -1) {
        const now = state.player.getTime();
        next = direction > 0 ? loops.find(l => l.callout.start > now) || loops[loops.length - 1]
                             : [...loops].reverse().find(l => l.callout.start < now) || loops[0];
    } else {
        next = loops[Math.max(0, Math.min(loops.length - 1, idx + direction))];
    }
    setActive(next.id, { seek: true });
}

// ---------- Persistence ----------

// Keys are written in spreadsheet column order so the JSON reads like the old sheet
function exportDoc() {
    renumber();
    const { session_id, rater, created_at, updated_at } = state.doc;
    return {
        session_id,
        rater,
        scheme: { id: state.scheme.id, version: state.scheme.version },
        created_at,
        updated_at,
        loops: sortedLoops().map(loop => ({
            id: loop.id,
            callout: {
                number: loop.callout.number, start: loop.callout.start, end: loop.callout.end,
                summary: loop.callout.summary, segment_ids: loop.callout.segment_ids || [],
            },
            ...Object.fromEntries(fields().map(f => [f.key, loop[f.key]])),
        })),
    };
}

// `structural` changes re-render the editor; text edits skip it so inputs keep focus
// Unsaved work is mirrored in this browser until the server confirms the save, so a lost
// connection or an expired Box sign-in never loses annotations
function backupKey(sessionId = state.sessionId, rater = state.rater) { return `clcBackup:${sessionId}:${rater}`; }

function writeBackup() {
    try { localStorage.setItem(backupKey(), JSON.stringify({ saved_at: new Date().toISOString(), doc: exportDoc() })); } catch (e) { /* storage unavailable */ }
}

function readBackup(sessionId, rater) {
    try { return JSON.parse(localStorage.getItem(backupKey(sessionId, rater))); } catch (e) { return null; }
}

function clearBackup() {
    try { localStorage.removeItem(backupKey()); } catch (e) { /* storage unavailable */ }
}

function changed({ structural = true } = {}) {
    state.dirty = true;
    writeBackup();
    renderSaveStatus();
    if (structural) renderAll();
    else { renderTimeline(); renderTable(); }
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
}

async function save() {
    if (!state.dirty || !state.doc) return;
    if (state.saving) {
        state.saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
        return;
    }
    state.saving = true;
    state.dirty = false;
    renderSaveStatus();
    try {
        const resp = await fetchApi('/api/annotations', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(exportDoc()),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const result = await resp.json();
        state.doc.updated_at = result.updated_at;
        state.lastSaved = new Date();
        if (!state.dirty) clearBackup();
    } catch (err) {
        console.error(err);
        state.dirty = true;
        state.saveError = true;
        toast('Save failed – will retry', 'danger');
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(save, 5000);
    } finally {
        state.saving = false;
        if (!state.dirty) state.saveError = false;
        renderSaveStatus();
    }
}

function renderSaveStatus() {
    const node = $('saveStatus');
    if (!state.doc) { node.textContent = ''; return; }
    if (state.saving) node.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Saving…';
    else if (state.saveError) node.innerHTML = '<span class="text-danger"><i class="fas fa-exclamation-triangle"></i> Not saved</span>';
    else if (state.dirty) node.innerHTML = '<i class="fas fa-pen"></i> Unsaved changes';
    else if (state.lastSaved) node.innerHTML = `<i class="fas fa-check text-success"></i> Saved ${state.lastSaved.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
    else node.innerHTML = '<i class="fas fa-check text-success"></i> Saved';
}

function downloadJson() {
    const doc = exportDoc();
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
    const base = state.sessionId.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9_-]+/g, '_');
    const rater = state.rater.replace(/[^A-Za-z0-9_-]+/g, '_');
    const link = el('a', { href: URL.createObjectURL(blob), download: `${base}_${rater}_${SCHEME_ID}.json` });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(link.href);
}

// ---------- Loading ----------

async function loadSessionList() {
    const resp = await fetchApi('/api/sessions');
    if (!resp.ok) {
        if (resp.status !== 401) toast('Could not list session videos', 'danger');
        return;
    }
    const sessions = await resp.json();
    const select = $('sessionSelect');
    select.innerHTML = '';
    select.appendChild(el('option', { value: '', text: sessions.length ? 'Select a session…' : 'No videos found' }));
    const kindLabel = { video: 'video', audio: 'audio', role_tracks: 'per-person mics' };
    for (const s of sessions) select.appendChild(el('option', { value: s.id, text: `${s.name}  ·  ${kindLabel[s.kind] || s.kind}` }));
}

async function init() {
    state.scheme = await (await fetchApi(`/api/scheme/${SCHEME_ID}`)).json();
    const auth = await (await fetchApi('/api/auth')).json();
    $('saveStatus').title = auth.save_location === 'Box' ? 'Annotations are saved to Box' : `Annotations are saved on this computer in ${auth.save_location}`;
    if (auth.login_required) {
        $('signOutBtn').classList.toggle('d-none', !auth.authenticated);
        $('signOutBtn').addEventListener('click', signOut);
    }
    if (auth.login_required && !auth.authenticated) showSignIn();
    else await loadSessionList();

    try { $('raterInput').value = localStorage.getItem('clcRater') || ''; } catch (e) { /* storage unavailable */ }

    $('loadBtn').addEventListener('click', loadSession);
    $('raterInput').addEventListener('keydown', e => { if (e.key === 'Enter') loadSession(); });
    $('exportBtn').addEventListener('click', downloadJson);
    $('playBtn').addEventListener('click', togglePlay);
    $('backBtn').addEventListener('click', () => nudge(-5));
    $('fwdBtn').addEventListener('click', () => nudge(5));
    $('sourceSelect').addEventListener('change', e => switchSource(parseInt(e.target.value, 10)));
    $('rateSelect').addEventListener('change', e => state.player && state.player.setRate(parseFloat(e.target.value)));
    $('newLoopBtn').addEventListener('click', newLoopAtPlayhead);
    $('zoomRange').addEventListener('input', () => { renderTimeline(); followPlayhead(true); });
    setupTimelineMouse();
    setupTooltips();
    setupTranscriptControls();
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('beforeunload', e => {
        if (state.dirty || state.saving) { e.preventDefault(); e.returnValue = ''; }
    });
    setInterval(tick, TICK_MS);
}

async function loadSession() {
    const sessionId = $('sessionSelect').value;
    const rater = $('raterInput').value.trim();
    if (!sessionId) return toast('Pick a session video', 'warning');
    if (!rater) return toast('Enter your rater name', 'warning');

    if (state.dirty) await save();
    try { localStorage.setItem('clcRater', rater); } catch (e) { /* storage unavailable */ }

    const params = new URLSearchParams({ session: sessionId, rater });
    const [playerResp, annResp] = await Promise.all([
        fetchApi(`/api/player?${params}`),
        fetchApi(`/api/annotations?${params}`),
    ]);
    if (!playerResp.ok || !annResp.ok) {
        const failed = playerResp.ok ? annResp : playerResp;
        if (failed.status !== 401) {
            const data = await failed.json().catch(() => ({}));
            toast(data.error || 'Could not open that session', 'danger');
        }
        return;
    }
    const descriptor = await playerResp.json();
    const saved = await annResp.json();

    if (state.player) state.player.destroy();
    state.sessionId = sessionId;
    state.rater = rater;
    state.activeLoopId = null;
    state.selection = null;
    state.dirty = false;
    state.lastSaved = null;
    state.doc = saved || {
        session_id: sessionId,
        rater,
        created_at: new Date().toISOString(),
        updated_at: null,
        loops: [],
    };
    const backup = readBackup(sessionId, rater);
    if (backup && backup.doc && (!saved || !saved.updated_at || new Date(backup.saved_at) > new Date(saved.updated_at))
        && confirm(`You have unsaved changes for this session from ${new Date(backup.saved_at).toLocaleString()} (${backup.doc.loops.length} call-outs). Restore them?`)) {
        state.doc = backup.doc;
        state.dirty = true;
        clearTimeout(state.saveTimer);
        state.saveTimer = setTimeout(save, SAVE_DEBOUNCE_MS);
    } else if (backup) {
        try { localStorage.removeItem(backupKey(sessionId, rater)); } catch (e) { /* storage unavailable */ }
    }

    $('emptyState').classList.add('d-none');
    $('workspace').classList.remove('d-none');
    $('exportBtn').disabled = false;
    $('tableBtn').disabled = false;

    const container = $('playerContainer');
    container.classList.toggle('audio-only', descriptor.media === 'audio');
    container.classList.toggle('box-player', descriptor.type === 'box');
    state.player = createPlayer(container, descriptor, sessionId);
    try {
        await state.player.load();
    } catch (err) {
        toast(err.message, 'danger');
        return;
    }
    state.player.setRate(parseFloat($('rateSelect').value));
    renderSourceSelect(descriptor);
    await loadTranscript();
    $('duration').textContent = formatTime(state.player.getDuration());
    renderAll();
    renderSaveStatus();
    const transcriptNote = segments().length ? ` · ${segments().length} transcript segments` : ' · no transcript yet';
    toast((saved ? `Loaded ${saved.loops.length} call-outs` : 'New annotation file') + transcriptNote, 'info');
}

// "Listen to" / "Watch": switch between a recording's files (one person's mic, or one camera)
function renderSourceSelect(descriptor) {
    const select = $('sourceSelect');
    select.innerHTML = '';
    descriptor.sources.forEach((src, i) => select.appendChild(el('option', { value: String(i), text: src.label })));
    select.value = '0';
    select.classList.toggle('d-none', descriptor.sources.length < 2);
    renderAudioLabel();
}

function renderAudioLabel() {
    const container = $('playerContainer');
    const existing = container.querySelector('.audio-label');
    if (existing) existing.remove();
    if (!container.classList.contains('audio-only')) return;
    const src = state.player.sources[state.player.sourceIndex];
    container.appendChild(el('div', { class: 'audio-label' }, el('i', { class: 'fas fa-headphones' }), src.label));
}

async function switchSource(index) {
    try {
        await state.player.switchSource(index);
        renderAudioLabel();
    } catch (err) {
        toast(err.message, 'danger');
    }
}

async function signOut() {
    if (state.dirty) await save();
    await fetch('/box/logout', { method: 'POST' });
    window.location.reload();
}

// ---------- Playback ----------

function togglePlay() {
    if (!state.player) return;
    state.playUntil = null;
    state.player.togglePlay();
}

function nudge(seconds) {
    if (!state.player) return;
    state.player.seek(state.player.getTime() + seconds);
}

function newLoopAtPlayhead() {
    if (!state.player) return;
    state.player.pause();
    if (state.selection) {
        createLoop(state.selection.start, state.selection.end, markSegments());
        clearSelection();
    } else {
        createLoop(state.player.getTime());
    }
}

function playSelection() {
    if (!state.selection) return;
    state.player.seek(state.selection.start);
    state.playUntil = state.selection.end;
    state.player.play();
}

function tick() {
    if (!state.player || !state.doc) return;
    const t = state.player.getTime();
    if (state.playUntil !== null && t >= state.playUntil) {
        state.player.pause();
        state.playUntil = null;
    }
    $('currentTime').textContent = formatTime(t, true);
    $('playBtn').innerHTML = state.player.isPlaying() ? '<i class="fas fa-pause"></i>' : '<i class="fas fa-play"></i>';
    const playhead = document.querySelector('#timelineInner .playhead');
    if (playhead) playhead.style.left = `${pct(t)}%`;
    if (state.player.isPlaying()) followPlayhead(false);
    highlightCurrentSegment();
}

function onKeyDown(e) {
    if (!state.player || isTyping(e) || e.metaKey || e.ctrlKey || e.altKey) return;
    const key = e.key.toLowerCase();
    const hotkeys = Object.fromEntries(timestampLists().filter(t => t.list.hotkey).map(t => [t.list.hotkey, t.path]));
    if (e.key === ' ') { e.preventDefault(); togglePlay(); }
    else if (key === 'n') { e.preventDefault(); newLoopAtPlayhead(); }
    else if (hotkeys[key]) { e.preventDefault(); addTimestamp(hotkeys[key], markTime(), markSegments()); clearSelection(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); nudge(e.shiftKey ? -1 : -5); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); nudge(e.shiftKey ? 1 : 5); }
    else if (e.key === '[') stepLoop(-1);
    else if (e.key === ']') stepLoop(1);
    else if (e.key === 'Escape') { closeSpeakerMenu(); clearSelection(); }
}

// ---------- Timeline ----------

function duration() { return (state.player && state.player.getDuration()) || 1; }
function pct(t) { return (t / duration()) * 100; }

function timeFromEvent(event) {
    const rect = $('timelineInner').getBoundingClientRect();
    const x = Math.max(0, Math.min(event.clientX - rect.left, rect.width));
    return (x / rect.width) * duration();
}

function followPlayhead(center) {
    const scroll = $('timelineScroll');
    const inner = $('timelineInner');
    if (!state.player || inner.scrollWidth <= scroll.clientWidth) return;
    const x = (state.player.getTime() / duration()) * inner.scrollWidth;
    if (center) scroll.scrollLeft = x - scroll.clientWidth / 2;
    else if (x < scroll.scrollLeft || x > scroll.scrollLeft + scroll.clientWidth - 40) scroll.scrollLeft = x - 40;
}

function tickInterval(pxPerSecond) {
    // Pick a ruler spacing that keeps labels roughly 70px apart
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
    return steps.find(s => s * pxPerSecond >= 70) || 600;
}

function renderTimeline() {
    const inner = $('timelineInner');
    if (!state.player || !state.doc) return;
    const zoom = parseInt($('zoomRange').value, 10);
    inner.style.width = `${zoom * 100}%`;
    inner.innerHTML = '';

    const dur = duration();
    const width = $('timelineScroll').clientWidth * zoom;
    const step = tickInterval(width / dur);
    const ruler = el('div', { class: 'ruler' });
    for (let t = 0; t <= dur; t += step) {
        ruler.appendChild(el('div', { class: 'tick', style: `left:${pct(t)}%` }, el('span', { text: formatTime(t) })));
    }
    inner.appendChild(ruler);

    const calloutLane = el('div', { class: 'lane lane-callouts' });
    const markerLane = el('div', { class: 'lane lane-markers' });
    for (const loop of state.doc.loops) {
        const active = loop.id === state.activeLoopId;
        const start = loop.callout.start;
        const end = loop.callout.end !== null ? loop.callout.end : start;
        const warn = loopWarnings(loop).length > 0;
        const incomplete = missingFields(loop).length > 0;
        calloutLane.appendChild(el('div', {
            class: `callout-bar${active ? ' active' : ''}${warn ? ' warn' : ''}${incomplete ? ' incomplete' : ''}`,
            style: `left:${pct(start)}%;width:max(14px, ${pct(end - start)}%)`,
            title: `#${loop.callout.number} ${formatTime(start)} ${loop.callout.summary || ''}`,
            onmousedown: e => e.stopPropagation(),
            onclick: e => { e.stopPropagation(); setActive(loop.id, { seek: true }); },
        }, String(loop.callout.number)));

        for (const { path, list } of timestampLists()) {
            for (const entry of getList(loop, path)) {
                const kind = path.split('.').pop();
                markerLane.appendChild(el('div', {
                    class: `marker mk-${kind}${active ? ' active' : ''}`,
                    style: `left:${pct(entry.time)}%`,
                    title: `#${loop.callout.number} ${list.label} ${formatTime(entry.time)}${entry.note ? ' – ' + entry.note : ''}`,
                    onmousedown: e => e.stopPropagation(),
                    onclick: e => { e.stopPropagation(); setActive(loop.id); state.player.seek(entry.time); },
                }));
                if (active) {
                    // Connector from the call-out to each of its check backs / closures
                    const from = Math.min(start, entry.time);
                    const to = Math.max(start, entry.time);
                    markerLane.appendChild(el('div', { class: 'connector', style: `left:${pct(from)}%;width:${pct(to - from)}%` }));
                }
            }
        }
    }
    inner.appendChild(calloutLane);
    inner.appendChild(markerLane);

    if (state.selection) {
        inner.appendChild(el('div', {
            class: 'selection',
            style: `left:${pct(state.selection.start)}%;width:${pct(state.selection.end - state.selection.start)}%`,
        }));
    }
    inner.appendChild(el('div', { class: 'playhead', style: `left:${pct(state.player.getTime())}%` }));
    renderSelectionBar();
}

function setupTimelineMouse() {
    const inner = $('timelineInner');
    let dragStart = null;
    let startX = 0;

    inner.addEventListener('mousedown', e => {
        if (!state.player || e.button !== 0) return;
        dragStart = timeFromEvent(e);
        startX = e.clientX;
        e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
        if (dragStart === null || Math.abs(e.clientX - startX) < 4) return;
        const t = timeFromEvent(e);
        state.selection = { start: roundTime(Math.min(dragStart, t)), end: roundTime(Math.max(dragStart, t)), segmentIds: [] };
        renderTimeline();
        updateTranscriptSelection();
    });
    window.addEventListener('mouseup', e => {
        if (dragStart === null) return;
        if (Math.abs(e.clientX - startX) < 4) {
            // A click, not a drag: jump there
            state.selection = null;
            state.player.seek(dragStart);
            renderTimeline();
            updateTranscriptSelection();
        } else {
            state.player.pause();
            state.player.seek(state.selection.start);
        }
        dragStart = null;
    });
}

function clearSelection() {
    state.selection = null;
    state.playUntil = null;
    renderTimeline();
    updateTranscriptSelection();
}

function renderSelectionBar() {
    const bar = $('selectionBar');
    const sel = state.selection;
    // Extra bottom padding so the floating bar never hides the last segments
    $('transcriptList').classList.toggle('has-selection', Boolean(sel));
    if (!sel) { bar.classList.add('d-none'); return; }
    bar.classList.remove('d-none');
    bar.innerHTML = '';
    const loop = activeLoop();
    bar.appendChild(el('span', { class: 'sel-label' },
        el('i', { class: sel.segmentIds.length ? 'fas fa-align-left' : 'fas fa-i-cursor' }),
        ` ${formatTime(sel.start, true)} – ${formatTime(sel.end, true)} `,
        el('span', { class: 'text-muted', text: sel.segmentIds.length
            ? `(${sel.segmentIds.length} segment${sel.segmentIds.length > 1 ? 's' : ''})`
            : `(${(sel.end - sel.start).toFixed(1)}s)` })));
    bar.appendChild(el('button', { class: 'btn btn-sm btn-light', onclick: playSelection }, el('i', { class: 'fas fa-play' }), ' Play'));
    bar.appendChild(el('button', { class: 'btn btn-sm btn-success', onclick: () => { createLoop(sel.start, sel.end, markSegments()); clearSelection(); } },
        el('i', { class: 'fas fa-plus' }), ' New call-out'));
    if (loop) {
        bar.appendChild(el('span', { class: 'sel-divider', text: `→ #${loop.callout.number}:` }));
        bar.appendChild(el('button', {
            class: 'btn btn-sm btn-outline-secondary',
            onclick: () => {
                loop.callout.start = sel.start;
                loop.callout.end = sel.end;
                loop.callout.segment_ids = markSegments();
                renumber();
                clearSelection();
                changed();
            },
        }, 'Set call-out span'));
        for (const { path, list } of timestampLists()) {
            const kind = path.split('.').pop();
            bar.appendChild(el('button', {
                class: `btn btn-sm btn-outline-secondary btn-mk-${kind}`,
                onclick: () => { addTimestamp(path, sel.start, markSegments()); clearSelection(); },
            }, `+ ${list.label}`));
        }
    }
    bar.appendChild(el('button', { class: 'btn btn-sm btn-link ms-auto', onclick: clearSelection, title: 'Clear (Esc)' }, el('i', { class: 'fas fa-times' })));
}

// ---------- Transcript ----------
//
// One transcript per recording (imported segments + speaker labels).
// Selecting segments here is the same as dragging on the timeline, except the chosen
// segment ids are stored with the label so call-outs can be tied back to utterances.

function segments() { return (state.transcript && state.transcript.segments) || []; }

function segmentsById(ids) {
    const set = new Set(ids);
    return segments().filter(s => set.has(s.id));
}

function speakerList() {
    const speakers = (state.transcript && state.transcript.speakers) || [];
    return speakers.length ? speakers : (state.scheme.speakers || []);
}

function currentSegment(t) {
    return segments().find(s => t >= s.start && t < s.end) || null;
}

// Which loop labels sit on each segment: linked segment ids if the label has them,
// otherwise whichever segment contains the label's time
function segmentBadges() {
    const badges = new Map();
    const add = (ids, time, badge) => {
        const targets = ids && ids.length ? ids : [(currentSegment(time) || {}).id];
        for (const id of targets) {
            if (id === undefined) continue;
            if (!badges.has(id)) badges.set(id, []);
            badges.get(id).push(badge);
        }
    };
    if (!state.doc) return badges;
    for (const loop of state.doc.loops) {
        const n = loop.callout.number;
        add(loop.callout.segment_ids, loop.callout.start, { loop, kind: 'callout', text: `#${n} call-out` });
        for (const { path, list } of timestampLists()) {
            const kind = path.split('.').pop();
            for (const entry of getList(loop, path)) {
                add(entry.segment_ids, entry.time, { loop, kind, text: `#${n} ${list.label.replace(' Check Back', ' CB')}` });
            }
        }
    }
    return badges;
}

async function loadTranscript() {
    const resp = await fetchApi(`/api/transcript?${new URLSearchParams({ session: state.sessionId })}`);
    state.transcript = resp.ok ? await resp.json() : null;
    state.lastSegmentClicked = null;
}

// Replaces the whole transcript (used for imports)
async function saveTranscript() {
    const resp = await fetchApi('/api/transcript', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: state.sessionId, ...state.transcript }),
    });
    if (!resp.ok) { toast('Could not save transcript', 'danger'); return; }
    state.transcript = await resp.json();
}

// Speaker changes and deletes are sent per segment, so raters editing at once don't overwrite each other
async function patchSegment(change) {
    const resp = await fetchApi('/api/transcript/segment', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: state.sessionId, ...change }),
    });
    if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        toast(data.error || 'Could not save speaker change', 'danger');
        return;
    }
    state.transcript.speakers = (await resp.json()).speakers;
}

function renderTranscript() {
    const list = $('transcriptList');
    if (!state.doc) return;
    list.innerHTML = '';
    const all = segments();
    $('segmentCount').textContent = all.length ? all.length : '';
    if (!all.length) {
        list.appendChild(el('div', { class: 'transcript-empty' },
            el('p', { class: 'mb-1' }, 'No transcript for this session yet.'),
            el('p', { class: 'small text-muted mb-0' }, 'If you have one, load it with ', el('strong', { text: 'Import transcript' }),
                '. You can annotate from the recording and timeline either way.')));
        return;
    }
    const filter = $('segmentFilter').value;
    const visible = all.filter(s => filter === 'all' || (filter === 'unlabeled' && !s.speaker));
    const badges = segmentBadges();
    for (const seg of visible) {
        const row = el('div', { class: 'seg', 'data-seg': seg.id, onclick: e => onSegmentClick(e, seg) },
            el('button', { class: 'seg-time', title: 'Jump here', onclick: e => { e.stopPropagation(); state.player.seek(seg.start); } }, formatTime(seg.start)),
            el('button', {
                class: `seg-speaker${seg.speaker ? '' : ' none'}`,
                title: 'Assign speaker',
                onclick: e => { e.stopPropagation(); openSpeakerMenu(e.currentTarget, seg); },
            }, seg.speaker || 'Speaker?', el('i', { class: 'fas fa-caret-down ms-1' })),
            el('div', { class: 'seg-body' },
                el('div', { class: 'seg-text', text: seg.text }),
                badges.has(seg.id) ? el('div', { class: 'seg-badges' }, ...badges.get(seg.id).map(b =>
                    el('button', {
                        class: `seg-badge bd-${b.kind}${b.loop.id === state.activeLoopId ? ' active' : ''}`,
                        onclick: e => { e.stopPropagation(); setActive(b.loop.id); },
                    }, b.text))) : null));
        list.appendChild(row);
    }
    updateTranscriptSelection();
    highlightCurrentSegment(true);
}

// Click selects one segment; Shift+click extends to a range (like selecting rows in the original app)
function onSegmentClick(e, seg) {
    const all = segments();
    let ids = [seg.id];
    if (e.shiftKey && state.lastSegmentClicked !== null) {
        const a = all.findIndex(s => s.id === state.lastSegmentClicked);
        const b = all.findIndex(s => s.id === seg.id);
        if (a !== -1 && b !== -1) ids = all.slice(Math.min(a, b), Math.max(a, b) + 1).map(s => s.id);
    } else if (state.selection && state.selection.segmentIds.length === 1 && state.selection.segmentIds[0] === seg.id) {
        // Clicking the only selected segment again deselects it
        clearSelection();
        state.lastSegmentClicked = null;
        return;
    }
    state.lastSegmentClicked = seg.id;
    const chosen = segmentsById(ids);
    state.selection = {
        start: roundTime(Math.min(...chosen.map(s => s.start))),
        end: roundTime(Math.max(...chosen.map(s => s.end))),
        segmentIds: ids,
    };
    state.player.pause();
    state.player.seek(state.selection.start);
    renderTimeline();
    updateTranscriptSelection();
}

// Selected segments, plus segments under a timeline drag selection
function updateTranscriptSelection() {
    const sel = state.selection;
    const ids = new Set(sel ? sel.segmentIds : []);
    document.querySelectorAll('#transcriptList .seg').forEach(row => {
        const seg = segments().find(s => s.id === Number(row.dataset.seg));
        const selected = ids.has(seg.id);
        const overlaps = sel && !sel.segmentIds.length && seg.start < sel.end && seg.end > sel.start;
        row.classList.toggle('selected', selected);
        row.classList.toggle('in-range', Boolean(overlaps));
    });
}

function highlightCurrentSegment(force = false) {
    const seg = currentSegment(state.player ? state.player.getTime() : 0);
    const id = seg ? seg.id : null;
    if (!force && id === state.currentSegmentId) return;
    state.currentSegmentId = id;
    document.querySelectorAll('#transcriptList .seg.current').forEach(r => r.classList.remove('current'));
    if (id === null) return;
    const row = document.querySelector(`#transcriptList .seg[data-seg="${id}"]`);
    if (!row) return;
    row.classList.add('current');
    if ($('followToggle').checked && state.player.isPlaying()) {
        // Scroll only the transcript list, not the page
        const list = $('transcriptList');
        if (row.offsetTop < list.scrollTop || row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) {
            list.scrollTop = row.offsetTop - list.clientHeight / 3;
        }
    }
}

function openSpeakerMenu(anchor, seg) {
    const menu = $('speakerMenu');
    menu.innerHTML = '';
    const assign = async (speaker, addSpeaker = null) => {
        seg.speaker = speaker;
        seg.speaker_source = speaker ? 'manual' : '';
        closeSpeakerMenu();
        renderTranscript();
        await patchSegment({ id: seg.id, speaker, add_speaker: addSpeaker });
    };
    for (const speaker of speakerList()) {
        menu.appendChild(el('button', { class: `menu-item${seg.speaker === speaker ? ' selected' : ''}`, onclick: () => assign(speaker) },
            seg.speaker === speaker ? el('i', { class: 'fas fa-check me-1' }) : null, speaker));
    }
    menu.appendChild(el('div', { class: 'menu-divider' }));
    menu.appendChild(el('button', {
        class: 'menu-item', onclick: () => {
            const name = (prompt('New speaker name') || '').trim();
            if (!name) return;
            if (!speakerList().includes(name)) state.transcript.speakers = [...speakerList(), name];
            assign(name, name);
        },
    }, el('i', { class: 'fas fa-plus me-1' }), 'New speaker…'));
    if (seg.speaker) menu.appendChild(el('button', { class: 'menu-item', onclick: () => assign('') }, el('i', { class: 'fas fa-eraser me-1' }), 'Clear speaker'));
    menu.appendChild(el('button', {
        class: 'menu-item text-danger', onclick: async () => {
            closeSpeakerMenu();
            if (!confirm(`Delete this segment?\n\n"${seg.text}"`)) return;
            state.transcript.segments = segments().filter(s => s.id !== seg.id);
            renderTranscript();
            await patchSegment({ id: seg.id, delete: true });
        },
    }, el('i', { class: 'fas fa-trash me-1' }), 'Delete segment'));

    menu.classList.remove('d-none');
    const rect = anchor.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    menu.style.left = `${Math.min(rect.left, window.innerWidth - menuRect.width - 8)}px`;
    menu.style.top = `${rect.bottom + menuRect.height + 4 > window.innerHeight ? rect.top - menuRect.height - 4 : rect.bottom + 4}px`;
}

function closeSpeakerMenu() { $('speakerMenu').classList.add('d-none'); }

// Accepts Whisper output ({segments: [...]}), this app's transcript, or the original app's
// exported labels ([{speaker, start, end, text}, ...])
async function importTranscript(file) {
    let data;
    try { data = JSON.parse(await file.text()); } catch (e) { toast('That file is not valid JSON', 'danger'); return; }
    const raw = Array.isArray(data) ? data : data.segments;
    if (!Array.isArray(raw) || !raw.every(s => s && s.start !== undefined && s.end !== undefined && s.text !== undefined)) {
        toast('Expected segments with start, end and text', 'danger');
        return;
    }
    // Compare just the file / take part: a transcript made from a Drive copy has no Box folder in its name
    const current = $('sessionSelect').selectedOptions[0]?.textContent.split('  ·  ')[0];
    const tail = name => name.split(' / ').pop();
    if (data.recording && current && tail(data.recording) !== tail(current)
        && !confirm(`This transcript was made for "${data.recording}", but you have "${current}" open. Import it anyway?`)) return;
    if (segments().length && !confirm('Replace the current transcript with the imported one?')) return;
    const found = raw.map(s => s.speaker).filter(Boolean);
    state.transcript = {
        source: 'import',
        speakers: [...new Set([...speakerList(), ...(data.speakers || []), ...found])],
        segments: raw,
    };
    await saveTranscript();
    renderTranscript();
    toast(`Imported ${segments().length} segments`, 'success');
}

function setupTranscriptControls() {
    $('segmentFilter').addEventListener('change', renderTranscript);
    $('importBtn').addEventListener('click', () => $('importInput').click());
    $('importInput').addEventListener('change', e => {
        if (e.target.files[0]) importTranscript(e.target.files[0]);
        e.target.value = '';
    });
    document.addEventListener('mousedown', e => {
        if (!e.target.closest('#speakerMenu') && !e.target.closest('.seg-speaker')) closeSpeakerMenu();
    });
    document.addEventListener('scroll', e => { if (!e.target.closest || !e.target.closest('#speakerMenu')) closeSpeakerMenu(); }, true);
}

// ---------- Loop editor ----------

function timeInput(value, onSet, placeholder) {
    const input = el('input', { class: 'form-control form-control-sm time-input', value: value === null ? '' : formatTime(value, true), placeholder });
    input.addEventListener('change', () => {
        const t = parseTime(input.value);
        if (input.value.trim() && t === null) { toast('Use m:ss, e.g. 2:23', 'warning'); return; }
        onSet(t === null ? null : roundTime(t));
    });
    return input;
}

function helpIcon(text) {
    return text ? el('i', { class: 'fas fa-circle-info help-icon', 'data-tip': text, tabindex: '0' }) : null;
}

// One shared tooltip for every [data-tip] element. It lives on <body> so the editor's
// scroll container can't clip it, and it doesn't depend on native title tooltips.
function setupTooltips() {
    const tip = el('div', { class: 'help-tooltip' });
    document.body.appendChild(tip);
    const show = target => {
        tip.textContent = target.dataset.tip;
        tip.classList.add('show');
        const rect = target.getBoundingClientRect();
        const tipRect = tip.getBoundingClientRect();
        const left = Math.max(8, Math.min(rect.left + rect.width / 2 - tipRect.width / 2, window.innerWidth - tipRect.width - 8));
        const above = rect.top - tipRect.height - 6;
        tip.style.left = `${left}px`;
        tip.style.top = `${above >= 8 ? above : rect.bottom + 6}px`;
    };
    const hide = () => tip.classList.remove('show');
    for (const [on, off] of [['mouseover', 'mouseout'], ['focusin', 'focusout']]) {
        document.addEventListener(on, e => { const t = e.target.closest('[data-tip]'); if (t) show(t); });
        document.addEventListener(off, e => { if (e.target.closest('[data-tip]')) hide(); });
    }
    document.addEventListener('scroll', hide, true);
}

function renderChoice(loop, field) {
    const wrap = el('div', { class: 'choice-wrap' });
    let currentGroup = null;
    let groupNode = wrap;
    for (const opt of field.options) {
        if ((opt.group || null) !== currentGroup) {
            currentGroup = opt.group || null;
            if (currentGroup) {
                groupNode = el('div', { class: 'choice-group' }, el('span', { class: 'choice-group-label', text: `${currentGroup}:` }));
                wrap.appendChild(groupNode);
            } else {
                groupNode = wrap;
            }
        }
        const selected = loop[field.key] === opt.value;
        groupNode.appendChild(el('button', {
            class: `choice-btn${selected ? ' selected' : ''}`,
            onclick: () => { loop[field.key] = selected ? null : opt.value; changed(); },
        }, opt.label));
    }
    return wrap;
}

function renderTimestamps(loop, field) {
    const wrap = el('div', { class: 'ts-wrap' });
    for (const list of field.lists) {
        const path = field.lists.length === 1 ? field.key : `${field.key}.${list.key}`;
        const kind = path.split('.').pop();
        const entries = getList(loop, path);
        const block = el('div', { class: 'ts-list' });
        if (field.lists.length > 1) block.appendChild(el('div', { class: 'ts-list-label' }, el('i', { class: `lg lg-${kind}` }), ` ${list.label} `, helpIcon(list.help)));
        entries.forEach((entry, idx) => {
            const note = el('input', { class: 'form-control form-control-sm', value: entry.note, placeholder: 'note (e.g. "ok", "epi given")' });
            note.addEventListener('input', () => { entry.note = note.value; changed({ structural: false }); });
            block.appendChild(el('div', { class: 'ts-entry' },
                el('span', { class: 'ts-y', text: 'y' }),
                el('button', { class: 'btn btn-sm btn-link ts-time', title: 'Jump here', onclick: () => state.player.seek(entry.time) }, formatTime(entry.time, true)),
                note,
                el('button', { class: 'btn btn-sm btn-link text-danger', title: 'Remove', onclick: () => { entries.splice(idx, 1); changed(); } }, el('i', { class: 'fas fa-times' }))));
        });
        block.appendChild(el('button', {
            class: `btn btn-sm btn-outline-secondary mark-btn btn-mk-${kind}`,
            onclick: () => { addTimestamp(path, markTime(), markSegments()); clearSelection(); },
        }, el('i', { class: 'fas fa-stopwatch' }), ' Mark now ', list.hotkey ? el('kbd', { text: list.hotkey.toUpperCase() }) : null));
        wrap.appendChild(block);
    }
    return wrap;
}

function renderNotes(loop, field) {
    const wrap = el('div', { class: 'notes-wrap' });
    const tags = el('div', { class: 'choice-wrap' });
    for (const tag of field.tags) {
        const on = loop[field.key].tags.includes(tag.value);
        tags.appendChild(el('button', {
            class: `choice-btn tag${on ? ' selected' : ''}`,
            onclick: () => {
                const list = loop[field.key].tags;
                if (on) list.splice(list.indexOf(tag.value), 1); else list.push(tag.value);
                changed();
            },
        }, tag.label));
    }
    wrap.appendChild(tags);
    const text = el('textarea', { class: 'form-control form-control-sm', rows: '2', placeholder: 'Free-text notes' });
    text.value = loop[field.key].text;
    text.addEventListener('input', () => { loop[field.key].text = text.value; changed({ structural: false }); });
    wrap.appendChild(text);
    return wrap;
}

function renderLoopEditor() {
    const editor = $('loopEditor');
    editor.innerHTML = '';
    const loop = activeLoop();
    if (!loop) {
        editor.appendChild(el('div', { class: 'editor-empty' },
            el('i', { class: 'fas fa-hand-pointer fa-2x mb-2' }),
            el('p', {}, 'Press ', el('kbd', { text: 'N' }), ' when you hear a call-out (task request or question), or drag across the timeline to select it.'),
            el('p', { class: 'small text-muted' }, 'Click a call-out on the timeline or in the table to edit it.')));
        return;
    }

    const c = loop.callout;
    editor.appendChild(el('div', { class: 'editor-header' },
        el('h5', { class: 'mb-0' }, `Call-out #${c.number}`),
        el('div', {},
            el('button', { class: 'btn btn-sm btn-light', title: 'Previous call-out ([)', onclick: () => stepLoop(-1) }, el('i', { class: 'fas fa-chevron-left' })),
            el('button', { class: 'btn btn-sm btn-light', title: 'Next call-out (])', onclick: () => stepLoop(1) }, el('i', { class: 'fas fa-chevron-right' })),
            el('button', { class: 'btn btn-sm btn-light', title: 'Close editor', onclick: () => setActive(null) }, el('i', { class: 'fas fa-times' })),
            el('button', { class: 'btn btn-sm btn-outline-danger ms-2', title: 'Delete call-out', onclick: () => deleteLoop(loop) }, el('i', { class: 'fas fa-trash' })))));

    // Call-out (timestamp) column
    const summary = el('input', { class: 'form-control form-control-sm', value: c.summary, placeholder: 'What was the call-out? (a few words)' });
    summary.addEventListener('input', () => { c.summary = summary.value; changed({ structural: false }); });
    editor.appendChild(el('div', { class: 'field' },
        el('div', { class: 'field-label' }, 'Call-Out (timestamp)'),
        el('div', { class: 'callout-times' },
            el('label', { text: 'Start' }),
            timeInput(c.start, t => { if (t !== null) { c.start = t; renumber(); changed(); } }),
            el('button', { class: 'btn btn-sm btn-light', title: 'Set start to current time', onclick: () => { c.start = roundTime(state.player.getTime()); renumber(); changed(); } }, el('i', { class: 'fas fa-stopwatch' })),
            el('label', { text: 'End' }),
            timeInput(c.end, t => { c.end = t; changed(); }, 'optional'),
            el('button', { class: 'btn btn-sm btn-light', title: 'Set end to current time', onclick: () => { c.end = roundTime(state.player.getTime()); changed(); } }, el('i', { class: 'fas fa-stopwatch' })),
            el('button', { class: 'btn btn-sm btn-dark', title: 'Play from call-out', onclick: () => { state.player.seek(c.start); state.player.play(); } }, el('i', { class: 'fas fa-play' }))),
        summary));

    for (const field of fields()) {
        const missing = field.required && isEmpty(loop[field.key]);
        let body;
        if (field.type === 'choice') body = renderChoice(loop, field);
        else if (field.type === 'timestamps') body = renderTimestamps(loop, field);
        else if (field.type === 'notes') body = renderNotes(loop, field);
        editor.appendChild(el('div', { class: `field${missing ? ' missing' : ''}` },
            el('div', { class: 'field-label' }, field.label, ' ', helpIcon(field.help)),
            body));
    }

    const warnings = loopWarnings(loop);
    const missing = missingFields(loop);
    if (warnings.length || missing.length) {
        editor.appendChild(el('div', { class: 'warnings' },
            missing.length ? el('div', { class: 'missing-line' }, el('i', { class: 'fas fa-circle-exclamation' }), ` Not filled in: ${missing.join(', ')}`) : null,
            ...warnings.map(w => el('div', { class: 'warn-line' }, el('i', { class: 'fas fa-triangle-exclamation' }), ` ${w}`))));
    }
}

// ---------- Loop table (spreadsheet view) ----------

// Columns mirror the Excel sheet: one column per option for choice fields, one per
// list for timestamp fields. Options with a `group` get an extra header row.
function tableColumns() {
    const cols = [];
    for (const field of fields()) {
        if (field.type === 'choice') {
            for (const opt of field.options) cols.push({ field, opt, kind: 'choice', label: opt.label, group: opt.group || null });
        } else if (field.type === 'timestamps') {
            for (const list of field.lists) {
                const path = field.lists.length === 1 ? field.key : `${field.key}.${list.key}`;
                cols.push({ field, list, path, kind: 'timestamps', label: list.label, group: null });
            }
        } else if (field.type === 'notes') {
            cols.push({ field, kind: 'notes', label: field.label, group: null });
        }
    }
    return cols;
}

function renderTable() {
    const table = $('loopTable');
    if (!state.doc) return;
    table.innerHTML = '';
    const cols = tableColumns();
    const hasGroups = cols.some(c => c.group);
    const headerRows = hasGroups ? 3 : 2;

    // Header row 1: field names
    const r1 = el('tr', {}, el('th', { rowspan: headerRows, class: 'col-num', text: '#' }),
        el('th', { rowspan: headerRows, class: 'col-callout', text: 'Call-Out (timestamp)' }));
    const r2 = el('tr');
    const r3 = el('tr');
    for (const field of fields()) {
        const fieldCols = cols.filter(c => c.field === field);
        const single = fieldCols.length === 1 && field.type !== 'choice';
        r1.appendChild(el('th', { colspan: fieldCols.length, rowspan: single ? headerRows : 1, class: 'field-head', text: field.label }));
        if (single) continue;
        // Header rows 2/3: options, with grouped options (e.g. Task Direction → Med Order…) split across rows
        for (let i = 0; i < fieldCols.length; i++) {
            const col = fieldCols[i];
            if (col.group) {
                const span = fieldCols.filter(c => c.group === col.group).length;
                if (i === 0 || fieldCols[i - 1].group !== col.group) r2.appendChild(el('th', { colspan: span, text: col.group }));
                r3.appendChild(el('th', { class: 'opt-head', text: col.label }));
            } else {
                r2.appendChild(el('th', { rowspan: headerRows - 1, class: 'opt-head', text: col.label }));
            }
        }
    }
    r1.appendChild(el('th', { rowspan: headerRows, class: 'col-flags', title: 'Missing fields / warnings' }, el('i', { class: 'fas fa-triangle-exclamation' })));
    const thead = el('thead', {}, r1, r2);
    if (hasGroups) thead.appendChild(r3);
    table.appendChild(thead);

    const tbody = el('tbody');
    const loops = sortedLoops();
    $('loopCount').textContent = loops.length;
    if (!loops.length) {
        tbody.appendChild(el('tr', {}, el('td', { colspan: cols.length + 3, class: 'text-muted text-center py-3', text: 'No call-outs yet' })));
    }
    for (const loop of loops) {
        const row = el('tr', { 'data-loop': loop.id, class: loop.id === state.activeLoopId ? 'active' : '' });
        row.appendChild(el('td', { class: 'col-num' },
            el('button', { class: 'btn btn-sm btn-link p-0', title: 'Open and jump to call-out', onclick: () => setActive(loop.id, { seek: true }) }, String(loop.callout.number))));
        row.appendChild(el('td', { class: 'col-callout', onclick: () => setActive(loop.id) },
            el('div', { text: `${loop.callout.number} (${formatTime(loop.callout.start)})` }),
            loop.callout.summary ? el('div', { class: 'cell-sub', text: loop.callout.summary }) : null));

        for (const col of cols) {
            if (col.kind === 'choice') {
                const on = loop[col.field.key] === col.opt.value;
                row.appendChild(el('td', {
                    class: `cell-y${on ? ' on' : ''}${isEmpty(loop[col.field.key]) ? ' empty-field' : ''}`,
                    title: `${col.field.label}: ${col.label}`,
                    onclick: () => { loop[col.field.key] = on ? null : col.opt.value; state.activeLoopId = loop.id; changed(); },
                }, on ? 'Y' : ''));
            } else if (col.kind === 'timestamps') {
                const entries = getList(loop, col.path);
                row.appendChild(el('td', { class: 'cell-ts', onclick: () => setActive(loop.id) },
                    ...entries.map(e => el('div', { text: `y (${e.note ? e.note + ' ' : ''}${formatTime(e.time)})` }))));
            } else if (col.kind === 'notes') {
                const notes = loop[col.field.key];
                const tagLabels = notes.tags.map(v => (col.field.tags.find(t => t.value === v) || { label: v }).label);
                row.appendChild(el('td', { class: 'cell-notes', onclick: () => setActive(loop.id) },
                    tagLabels.length ? el('div', { class: 'cell-sub', text: tagLabels.join(', ') }) : null,
                    notes.text ? el('div', { text: notes.text }) : null));
            }
        }
        const warnings = loopWarnings(loop);
        const missing = missingFields(loop);
        const flagTitle = [...missing.map(m => `Missing: ${m}`), ...warnings].join('\n');
        row.appendChild(el('td', { class: 'col-flags', title: flagTitle },
            warnings.length ? el('i', { class: 'fas fa-triangle-exclamation text-warning' }) : null,
            missing.length ? el('span', { class: 'missing-count', text: String(missing.length) }) : null,
            !warnings.length && !missing.length ? el('i', { class: 'fas fa-check text-success' }) : null));
        tbody.appendChild(row);
    }
    table.appendChild(tbody);
}

function renderAll() {
    renderLoopEditor();
    renderTimeline();
    renderTable();
    renderTranscript();
}

document.addEventListener('DOMContentLoaded', init);
