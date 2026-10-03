FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

# ----------------------------------------------------------------------------
# scout dev container
#
# Given a company domain: find its careers page and list its open jobs with
# their text (see README.md). Analysing the ads is a later step.
#
# Many career pages are rendered client-side or embed a hiring-system widget,
# so a headless browser (Playwright/Chromium) is installed for the pages that
# need it; most never do.
#
# Python comes from Ubuntu 24.04 (python3.12); deps are managed by Poetry into
# an in-project .venv. Chromium is installed in postCreateCommand via the
# Python playwright package so the browser always matches the locked version.
# ----------------------------------------------------------------------------

RUN mkdir -p /home/vscode/project
WORKDIR /home/vscode/project

# --- Base CLI + Python build tooling ----------------------------------------
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv \
        build-essential \
        libxml2-dev libxslt1-dev zlib1g-dev \
        unzip jq ca-certificates openssh-client rsync \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- GitHub CLI -------------------------------------------------------------
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- zsh niceties -----------------------------------------------------------
RUN git clone https://github.com/zsh-users/zsh-completions.git /home/vscode/.oh-my-zsh/custom/plugins/zsh-completions \
    && git clone https://github.com/zsh-users/zsh-syntax-highlighting.git /home/vscode/.oh-my-zsh/custom/plugins/zsh-syntax-highlighting \
    && git clone https://github.com/zsh-users/zsh-autosuggestions.git /home/vscode/.oh-my-zsh/custom/plugins/zsh-autosuggestions \
    && sed -i 's/plugins=(git)/plugins=(git zsh-completions zsh-syntax-highlighting zsh-autosuggestions)/' /home/vscode/.zshrc

# --- Node.js (Claude Code / misc agent tooling) -----------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Playwright system deps -------------------------------------------------
# Shared libs Chromium needs. The browser binary itself is installed per-user
# in postCreateCommand (`poetry run playwright install chromium`).
RUN apt-get update && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2t64 libpango-1.0-0 libcairo2 libatspi2.0-0 \
        libwayland-client0 libxext6 libx11-xcb1 xvfb fonts-freefont-ttf \
        fonts-noto-color-emoji libvulkan1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# --- Python tooling: Poetry -------------------------------------------------
# Pinned so the lock file and the tool agree after a rebuild.
USER vscode
RUN curl -sSL https://install.python-poetry.org | POETRY_VERSION=2.5.1 python3 - \
    && /home/vscode/.local/bin/poetry config virtualenvs.in-project true

# --- AI agent tooling: Claude Code ------------------------------------------
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root

# --- PATH wiring (zsh) ------------------------------------------------------
RUN printf '%s\n' \
      'export PATH="$PATH:/home/vscode/.local/bin"' \
      'export PATH="$PATH:/home/vscode/.claude/local/bin"' \
      >> /home/vscode/.zshrc

# --- Ownership --------------------------------------------------------------
RUN chown -R vscode:vscode /home/vscode
