#!/usr/bin/env bash
#
# bootstrap.sh -- one-command setup for people who don't normally use
# the command line. Installs git, Docker, and `just` if any of them
# are missing, then builds and starts the app.
#
# Run it from this directory:
#   ./bootstrap.sh
#
# Platform support:
#   Linux   -- tested and verified (Debian/Ubuntu, Fedora, Arch
#              families, via apt/dnf/pacman).
#   macOS   -- best-effort via Homebrew; not independently verified in
#              the environment this script was written in. Docker
#              Desktop's first launch needs one manual step (see
#              below) that can't be scripted.
#   Windows -- this is a bash script and will not run directly in
#              PowerShell or cmd. Install WSL2
#              (https://learn.microsoft.com/windows/wsl/install) and
#              Docker Desktop with its WSL2 backend enabled, then open
#              a WSL2 Ubuntu terminal and run this exact script there
#              -- from inside WSL2 this is a normal Linux environment,
#              and this script's Linux path applies unchanged.

set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
die() { printf '\nError: %s\n' "$1" >&2; exit 1; }

case "$(uname -s)" in
  Linux) PLATFORM=linux ;;
  Darwin) PLATFORM=macos ;;
  *)
    die "Unsupported platform '$(uname -s)'. This script supports Linux and macOS (and Windows via WSL2 -- see the comment at the top of this file)."
    ;;
esac

log "This will check for (and install if missing) git, Docker, and just, then build and start the app."
read -r -p "Press Enter to continue, or Ctrl+C to cancel... " _unused

# ─── git ───

if command -v git >/dev/null 2>&1; then
  log "git already installed ($(git --version))."
else
  log "Installing git..."
  if [ "$PLATFORM" = "linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update && sudo apt-get install -y git
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y git
    elif command -v pacman >/dev/null 2>&1; then
      sudo pacman -Sy --noconfirm git
    else
      die "Could not detect apt/dnf/pacman to install git automatically. Install git yourself (see https://git-scm.com/downloads), then re-run this script."
    fi
  else
    command -v brew >/dev/null 2>&1 || die "Homebrew is required to install git on macOS. Install it from https://brew.sh, then re-run this script."
    brew install git
  fi
fi

# ─── Docker ───

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  log "Docker (with the compose plugin) already installed ($(docker --version))."
else
  log "Installing Docker..."
  if [ "$PLATFORM" = "linux" ]; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER" || true
    log "Added $USER to the 'docker' group -- log out and back in (or run 'newgrp docker') before using Docker without sudo."
  else
    command -v brew >/dev/null 2>&1 || die "Homebrew is required to install Docker Desktop on macOS. Install it from https://brew.sh, then re-run this script."
    brew install --cask docker
    log "Docker Desktop was installed but needs to be launched once from your Applications folder to finish setup (it will ask for a few permissions) before this script can use it."
    read -r -p "Launch Docker Desktop now, wait for it to say it's running, then press Enter to continue... " _unused
  fi
fi

# ─── just ───

if command -v just >/dev/null 2>&1; then
  log "just already installed ($(just --version))."
else
  log "Installing just..."
  if [ "$PLATFORM" = "macos" ] && command -v brew >/dev/null 2>&1; then
    brew install just
  else
    curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to "$HOME/.local/bin"
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) ;;
      *)
        export PATH="$HOME/.local/bin:$PATH"
        log "Added $HOME/.local/bin to PATH for this session -- add it to your shell profile (~/.bashrc, ~/.zshrc, etc.) to keep it there next time."
        ;;
    esac
  fi
fi

# ─── Build and start ───

log "Running 'just install'..."
just install

log "Running 'just up' (first build downloads a full TeX Live distribution -- this can take several minutes)..."
just up

log "Done! Open http://localhost:8000 in your browser."
