#!/bin/bash
# Runs once after the container is created.
#
# Install locked deps into an in-project .venv with Poetry, then install the
# Chromium build that matches the locked playwright package. The browser cache is a named
# volume, so rebuilds don't re-download it.
set -e
cd /home/vscode/project

export PATH="$PATH:/home/vscode/.local/bin"

# Volumes are created root-owned on first mount.
sudo chown -R vscode:vscode /home/vscode/.cache/ms-playwright /home/vscode/.claude

poetry config virtualenvs.in-project true
poetry install
poetry run playwright install chromium

[ -f .env ] || cp .env.example .env
