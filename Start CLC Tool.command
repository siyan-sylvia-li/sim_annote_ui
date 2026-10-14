#!/bin/bash
# Double-click this file to start the CLC annotation tool (Mac).
# It updates the tool, sets it up the first time, starts it, and opens it in the browser.
# Keep the window it opens running while you annotate; closing it stops the tool.

cd "$(dirname "$0")" || exit 1
echo ""
echo "  CLC annotation tool"
echo "  -------------------"

# 1. Get the latest version (quietly carry on with the current one if offline)
if [ -d .git ]; then
    echo "  Checking for updates..."
    git pull --ff-only -q 2>/dev/null || echo "  (Couldn't check for updates; starting the version you have.)"
fi

# 2. Settings from the project lead's email: needed the first time, and again if they've changed
needs_settings() {
    [ ! -f .env ] && return 0
    for key in BOX_CLIENT_ID BOX_CLIENT_SECRET BOX_FOLDER_ID BOX_TRANSCRIPTS_FOLDER_ID; do
        grep -q "^$key=." .env || return 0
    done
    return 1
}
while needs_settings; do
    echo ""
    echo "  The tool needs its settings (the block of lines starting with BOX_ in the project lead's email)."
    echo "  1. In the email, select all of those lines and copy them (Cmd+C)."
    echo "  2. Come back to this window and press Enter."
    read -r _
    if pbpaste | grep -q "^BOX_CLIENT_ID=."; then
        pbpaste | tr -d '\r' | grep "^BOX_" > .env
        chmod 600 .env
        needs_settings && echo "  Some settings are missing. Make sure you copied every line starting with BOX_."
    else
        echo "  That doesn't look like the settings. Copy the lines starting with BOX_ and try again."
    fi
done

# 3. The two small packages the tool needs
if ! python3 -c "import flask, requests" 2>/dev/null; then
    echo "  Installing what the tool needs (first time only)..."
    python3 -m pip install --user -q flask requests || {
        echo "  Installing failed. Send a screenshot of this window to the project lead."
        read -r _
        exit 1
    }
fi

# 4. Start it (or just open it if it's already running in another window)
if curl -s -o /dev/null http://localhost:5050/api/auth; then
    echo "  The tool is already running; opening it."
    open http://localhost:5050
    exit 0
fi
echo "  Starting... it will open in your browser. Keep this window open while you work."
echo ""
(sleep 2; open http://localhost:5050) &
python3 annotate_app.py --storage box
