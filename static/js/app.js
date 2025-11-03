// Global variables
let currentVideo = null;
let currentSegments = [];
let currentSpeakers = [];
let currentFilter = 'all';
let progressModalTimeout = null;

// Initialize the application
document.addEventListener('DOMContentLoaded', function() {
    loadSpeakers();
    setupEventListeners();
});

function setupEventListeners() {
    // Enter key in video path input
    document.getElementById('videoPath').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            loadVideo();
        }
    });

    // Enter key in new speaker input
    document.getElementById('newSpeaker').addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            addSpeaker();
        }
    });

    // Range slider event listeners
    document.getElementById('denoiseProp').addEventListener('input', function(e) {
        document.getElementById('denoisePropValue').textContent = e.target.value;
    });

    document.getElementById('verificationThreshold').addEventListener('input', function(e) {
        document.getElementById('verificationThresholdValue').textContent = e.target.value;
    });
}

// Video loading functions
function loadVideo() {
    const videoPath = document.getElementById('videoPath').value.trim();
    if (!videoPath) {
        showStatus('Please enter a video file path', 'error');
        return;
    }

    showStatus('Loading video...', 'info');
    document.getElementById('loadBtn').disabled = true;

    // Send video path to backend
    fetch('/load_video', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ video_path: videoPath })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            currentVideo = data;
            document.getElementById('transcribeBtn').disabled = false;
            showStatus('Video loaded successfully!', 'success');
            displayVideo(data.video_url, data.filename);
        } else {
            showStatus('Error loading video: ' + data.error, 'error');
        }
        document.getElementById('loadBtn').disabled = false;
    })
    .catch(error => {
        showStatus('Error loading video: ' + error.message, 'error');
        document.getElementById('loadBtn').disabled = false;
    });
}

function displayVideo(videoUrl, filename) {
    const container = document.getElementById('videoContainer');
    container.innerHTML = `
        <div class="text-center">
            <video id="videoPlayer" controls class="w-100" style="max-height: 400px;">
                <source src="${videoUrl}" type="video/mp4">
                Your browser does not support the video tag.
            </video>
            <p class="mt-2 text-muted">${filename}</p>
        </div>
    `;
    
    // Show video controls and set up time update
    document.getElementById('videoControls').style.display = 'block';
    setupVideoTimeUpdate();
}

function seekToSegment(timeInSeconds) {
    const videoPlayer = document.getElementById('videoPlayer');
    if (videoPlayer) {
        videoPlayer.currentTime = timeInSeconds;
        videoPlayer.play();
    }
}

function setupVideoTimeUpdate() {
    const videoPlayer = document.getElementById('videoPlayer');
    if (videoPlayer) {
        videoPlayer.addEventListener('timeupdate', function() {
            const currentTime = document.getElementById('currentTime');
            if (currentTime) {
                currentTime.textContent = formatTime(videoPlayer.currentTime);
            }
        });
    }
}

function changePlaybackSpeed() {
    const videoPlayer = document.getElementById('videoPlayer');
    const speedSelect = document.getElementById('playbackSpeed');
    if (videoPlayer && speedSelect) {
        videoPlayer.playbackRate = parseFloat(speedSelect.value);
    }
}

function browseVideo() {
    // This would typically open a file dialog
    // For now, we'll just show a message
    showStatus('File browser not implemented in this demo. Please enter the path manually.', 'info');
}

// Segments file upload functions
function handleSegmentsFileSelect() {
    const fileInput = document.getElementById('segmentsFile');
    const uploadBtn = document.getElementById('uploadSegmentsBtn');
    
    if (fileInput.files && fileInput.files.length > 0) {
        const file = fileInput.files[0];
        if (file.type === 'application/json' || file.name.toLowerCase().endsWith('.json')) {
            uploadBtn.disabled = false;
            showStatus(`Selected file: ${file.name}`, 'info');
        } else {
            uploadBtn.disabled = true;
            showStatus('Please select a JSON file', 'error');
        }
    } else {
        uploadBtn.disabled = true;
    }
}

function uploadSegmentsFile() {
    const fileInput = document.getElementById('segmentsFile');
    const uploadBtn = document.getElementById('uploadSegmentsBtn');
    
    if (!fileInput.files || fileInput.files.length === 0) {
        showStatus('Please select a file first', 'error');
        return;
    }
    
    const file = fileInput.files[0];
    const formData = new FormData();
    formData.append('file', file);
    
    showProgressModal('Uploading segments file...', 'Processing and validating the uploaded segments.');
    uploadBtn.disabled = true;
    
    fetch('/upload_segments', {
        method: 'POST',
        body: formData
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        hideProgressModal();
        uploadBtn.disabled = false;
        
        if (data.success) {
            showStatus(`Successfully uploaded ${data.segments_count} segments!`, 'success');
            // Enable export button since we now have segments
            document.getElementById('exportBtn').disabled = false;
            // Load and display the segments
            loadSegments();
        } else {
            showStatus('Upload failed: ' + data.error, 'error');
        }
    })
    .catch(error => {
        console.error('Upload error:', error);
        hideProgressModal();
        uploadBtn.disabled = false;
        showStatus('Error uploading file: ' + error.message, 'error');
    })
    .finally(() => {
        // Ensure modal is hidden and button is re-enabled
        hideProgressModal();
        uploadBtn.disabled = false;
    });
}

// Transcription functions
function transcribeVideo() {
    if (!currentVideo) {
        showStatus('Please load a video first', 'error');
        return;
    }

    showProgressModal('Transcribing video with Whisper...', 'This may take several minutes depending on video length.');
    
    fetch('/whisper_transcribe', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({})
    })
    .then(response => response.json())
    .then(data => {
        hideProgressModal();
        if (data.success) {
            showStatus('Transcription completed successfully!', 'success');
            document.getElementById('speakerIdBtn').disabled = false;
            document.getElementById('exportBtn').disabled = false;
            loadSegments();
        } else {
            showStatus('Transcription failed: ' + data.error, 'error');
        }
    })
    .catch(error => {
        hideProgressModal();
        showStatus('Error during transcription: ' + error.message, 'error');
    });
}

// Speaker identification functions
function runSpeakerIdentification() {
    if (!currentVideo) {
        showStatus('Please load a video and transcribe first', 'error');
        return;
    }

    showProgressModal('Running speaker identification...', 'This process will analyze audio patterns to identify speakers.');
    
    // Get values from UI controls
    const denoise = document.getElementById('denoiseSwitch').checked;
    const denoiseProp = parseFloat(document.getElementById('denoiseProp').value);
    const verificationThreshold = parseFloat(document.getElementById('verificationThreshold').value);

    fetch('/speaker_identification', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({
            denoise: denoise,
            denoise_prop: denoiseProp,
            verification_threshold: verificationThreshold
        })
    })
    .then(response => response.json())
    .then(data => {
        hideProgressModal();
        if (data.success) {
            showStatus('Speaker identification completed!', 'success');
            loadSegments(); // Reload segments with speaker assignments
            console.log(data);
        } else {
            showStatus('Speaker identification failed: ' + data.error, 'error');
        }
    })
    .catch(error => {
        hideProgressModal();
        showStatus('Error during speaker identification: ' + error.message, 'error');
    });
}

// Segment management functions
function loadSegments() {
    // Load segments from the backend
    fetch('/get_segments')
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showStatus('Error loading segments: ' + data.error, 'error');
                return;
            }
            currentSegments = data;
            renderSegments();
        })
        .catch(error => {
            showStatus('Error loading segments: ' + error.message, 'error');
            // Fallback to demo segments for testing
            loadDemoSegments();
        });
}

function loadDemoSegments() {
    // Demo segments for testing when backend is not available
    currentSegments = [
        {
            id: 0,
            start: 0.0,
            end: 2.04,
            text: " make sure that if we forget anything or...",
            speaker: ""
        },
        {
            id: 1,
            start: 2.04,
            end: 2.68,
            text: " Patient has arrived.",
            speaker: ""
        },
        {
            id: 2,
            start: 4.12,
            end: 4.72,
            text: " Patient's here.",
            speaker: ""
        },
        {
            id: 3,
            start: 6.38,
            end: 9.54,
            text: " Let's dry off and stimulate the patient if that hasn't been done.",
            speaker: ""
        }
    ];
    renderSegments();
}

function renderSegments() {
    const container = document.getElementById('segmentsContainer');
    
    if (currentSegments.length === 0) {
        container.innerHTML = `
            <div class="text-center text-muted py-5">
                <i class="fas fa-microphone fa-3x mb-3"></i>
                <p>No segments found</p>
            </div>
        `;
        return;
    }

    const filteredSegments = filterSegments(currentSegments, currentFilter);
    
    container.innerHTML = filteredSegments.map(segment => `
        <div class="segment-item ${segment.speaker ? 'labeled' : 'unlabeled'} fade-in">
            <div class="segment-header d-flex justify-content-between align-items-center">
                <span class="segment-time" style="cursor: pointer;" onclick="seekToSegment(${segment.start})" title="Click to seek to this time">
                    ${formatTime(segment.start)} - ${formatTime(segment.end)}
                </span>
                <button class="btn btn-sm btn-outline-primary" onclick="selectSpeaker(${segment.id})">
                    ${segment.speaker ? 'Change Speaker' : 'Assign Speaker'}
                </button>
            </div>
            <div class="segment-text">${segment.text}</div>
            <div class="segment-speaker">
                <span class="speaker-badge ${segment.speaker ? '' : 'unassigned'}">
                    ${segment.speaker || 'Unassigned'}
                </span>
            </div>
        </div>
    `).join('');
}

// ADDED EDIT CAPABILITY
function enableEdit(segmentId) {
    const textDiv = document.getElementById(`segment-text-${segmentId}`);
    if (!textDiv) return;

    // Prevent re-enabling edit mode if already active
    if (textDiv.contentEditable === "true") return;

    textDiv.contentEditable = "true";
    textDiv.focus();

    // Place cursor at the end of the text when Edit button is clicked
    const range = document.createRange();
    range.selectNodeContents(textDiv);
    range.collapse(false);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);

    // Add a Save button directly after the text div
    const saveBtn = document.createElement('button');
    saveBtn.textContent = "SAVE UPDATES";
    saveBtn.className = "btn btn-sm btn-success ms-2";
    saveBtn.setAttribute('data-save-for', String(segmentId));
    saveBtn.onclick = () => saveEditedText(segmentId);
    textDiv.parentNode.insertBefore(saveBtn, textDiv.nextSibling);
}

function saveEditedText(segmentId) {
    const textDiv = document.getElementById(`segment-text-${segmentId}`);
    if (!textDiv) return;

    const newText = textDiv.innerText.trim();
    textDiv.contentEditable = "false";
    // After save button clicked, locks the text so it is not editable anymore
    
    // Remove inline Save button
    const nextEl = textDiv.nextSibling;
    if (nextEl && nextEl.tagName === 'BUTTON' && nextEl.getAttribute('data-save-for') === String(segmentId)) {
        nextEl.remove();
    }

    // Update locally
    const seg = currentSegments.find(s => s.id === segmentId);
    if (seg) seg.text = newText;

    // Send update to backend
    fetch('/update_segment_text', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segment_id: segmentId, text: newText })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) showStatus(`Segment ${segmentId} text updated successfully`, 'success');
        else showStatus(`Error updating text: ${data.error}`, 'error');
    })
    .catch(err => showStatus(`Error updating text: ${err.message}`, 'error'));
}
// End of Added Functionality Segment!
function filterSegments(segments, filter) {
    switch (filter) {
        case 'unlabeled':
            return segments.filter(s => !s.speaker);
        case 'labeled':
            return segments.filter(s => s.speaker);
        default:
            return segments;
    }
}

function showAllSegments() {
    currentFilter = 'all';
    renderSegments();
}

function showUnlabeledSegments() {
    currentFilter = 'unlabeled';
    renderSegments();
}

function showLabeledSegments() {
    currentFilter = 'labeled';
    renderSegments();
}

// Speaker management functions
function loadSpeakers() {
    // For demo purposes, we'll create some default speakers
    currentSpeakers = [];
    
    renderSpeakerList();
}

function renderSpeakerList() {
    const container = document.getElementById('speakerList');
    container.innerHTML = currentSpeakers.map(speaker => `
        <div class="list-group-item d-flex justify-content-between align-items-center">
            <div>
                <strong>${speaker.name}</strong>
                <small class="text-muted d-block">${speaker.description}</small>
            </div>
            <button class="btn btn-sm btn-outline-danger" onclick="removeSpeaker('${speaker.name}')">
                <i class="fas fa-trash"></i>
            </button>
        </div>
    `).join('');
}

function addSpeaker() {
    const nameInput = document.getElementById('newSpeaker');
    const name = nameInput.value.trim();
    
    if (!name) {
        showStatus('Please enter a speaker name', 'error');
        return;
    }
    
    if (currentSpeakers.some(s => s.name === name)) {
        showStatus('Speaker already exists', 'error');
        return;
    }
    
    const newSpeaker = {
        name: name,
        description: `Custom speaker: ${name}`
    };
    
    currentSpeakers.push(newSpeaker);
    renderSpeakerList();
    nameInput.value = '';
    showStatus(`Speaker "${name}" added successfully`, 'success');
}

function removeSpeaker(name) {
    currentSpeakers = currentSpeakers.filter(s => s.name !== name);
    renderSpeakerList();
    showStatus(`Speaker "${name}" removed`, 'success');
}

function selectSpeaker(segmentId) {
    const segment = currentSegments.find(s => s.id === segmentId);
    if (!segment) return;
    
    const modal = document.getElementById('speakerModal');
    const optionsContainer = document.getElementById('speakerOptions');
    
    // Populate speaker options
    optionsContainer.innerHTML = currentSpeakers.map(speaker => `
        <div class="list-group-item" onclick="assignSpeaker(${segmentId}, '${speaker.name}')">
            <strong>${speaker.name}</strong>
            <small class="text-muted d-block">${speaker.description}</small>
        </div>
    `).join('');
    
    // Show modal
    const bsModal = new bootstrap.Modal(modal);
    bsModal.show();
}

function assignSpeaker(segmentId, speakerName) {
    const segment = currentSegments.find(s => s.id === segmentId);
    if (segment) {
        // Update locally first
        segment.speaker = speakerName;
        renderSegments();
        
        // Close modal
        const modal = document.getElementById('speakerModal');
        const bsModal = bootstrap.Modal.getInstance(modal);
        bsModal.hide();
        
        // Send update to backend
        fetch('/update_segment_speaker', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                segment_id: segmentId,
                speaker: speakerName
            })
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showStatus(`Speaker "${speakerName}" assigned to segment`, 'success');
            } else {
                showStatus('Error updating speaker: ' + data.error, 'error');
            }
        })
        .catch(error => {
            showStatus('Error updating speaker: ' + error.message, 'error');
        });
    }
}

// Utility functions
function formatTime(seconds) {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

function showStatus(message, type) {
    const statusDiv = document.getElementById('status');
    statusDiv.textContent = message;
    statusDiv.className = `alert alert-${type === 'error' ? 'danger' : type}`;
    statusDiv.style.display = 'block';
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

function showProgressModal(title, message) {
    document.getElementById('progressMessage').textContent = message;
    const modalElement = document.getElementById('progressModal');

    // Clear any existing timeout
    if (progressModalTimeout) {
        clearTimeout(progressModalTimeout);
        progressModalTimeout = null;
    }

    // Hide any existing modal first
    const existingModal = bootstrap.Modal.getInstance(modalElement);
    if (existingModal) {
        existingModal.hide();
    }

    // Create and show new modal
    const modal = new bootstrap.Modal(modalElement, {
        backdrop: 'static',
        keyboard: false
    });
    modal.show();

    // Set a timeout to automatically hide the modal after 5 minutes as a safety measure
    progressModalTimeout = setTimeout(() => {
        console.warn('Progress modal timeout - forcing hide');
        hideProgressModal();
    }, 300000); // 5 minutes
}

function hideProgressModal() {
    // Clear timeout
    if (progressModalTimeout) {
        clearTimeout(progressModalTimeout);
        progressModalTimeout = null;
    }

    const modalElement = document.getElementById('progressModal');
    const modal = bootstrap.Modal.getInstance(modalElement);
    if (modal) {
        modal.hide();
    } else {
        // If no instance exists, create one and hide it
        const newModal = new bootstrap.Modal(modalElement);
        newModal.hide();
    }
}

// Export functions
function exportLabels() {
    if (currentSegments.length === 0) {
        showStatus('No segments to export', 'error');
        return;
    }
    
    // Get export data from backend
    fetch('/export_labels')
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                showStatus('Error exporting labels: ' + data.error, 'error');
                return;
            }
            
            const dataStr = JSON.stringify(data, null, 2);
            const dataBlob = new Blob([dataStr], { type: 'application/json' });
            
            const link = document.createElement('a');
            link.href = URL.createObjectURL(dataBlob);
            link.download = 'speaker_labels.json';
            link.click();
            
            showStatus('Labels exported successfully', 'success');
        })
        .catch(error => {
            showStatus('Error exporting labels: ' + error.message, 'error');
        });
}
