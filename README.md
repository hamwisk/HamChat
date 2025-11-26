# HamChat

HamChat is a local-first desktop chat client for large language models. It’s built for running local LLMs (via Ollama or other backends), with multi-user sessions, admin tools and a comfy PyQt/QML UI.

> Status: pre-alpha, expect dragons and breaking changes.

## Features (current & planned)

- Local-first, privacy-friendly chat with LLMs
- Desktop UI built with PyQt6 + QML
- Multiple user accounts with admin mode
- Conversation history and long-term memory (WIP)
- AI profiles / personas (planned)
- Image attachments and thumbnails in the chat UI (WIP)
- Pluggable backends (currently focused on local Ollama; API support planned)

## Getting started

### Requirements

- Python 3.10+
- A modern Linux desktop  
  (developed on Linux Mint; other platforms not tested yet)
- A local LLM backend (e.g. [Ollama]) if you want actual responses

### Setup

Clone the repo and install dependencies:

```bash
git clone git@github.com:hamwisk/HamChat.git
cd HamChat

python3.10 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

There are also helper scripts:
./setup_venv.sh      # optional convenience script for the steps above
./run_hamchat.sh     # run the app with logging

Or run directly:
python main.py --log-level DEBUG

Configuration

Basic settings (models, runtime mode, etc.) live under the settings/ directory.
HamChat is currently wired to talk to local LLMs (e.g. via Ollama); model IDs and related options can be adjusted there.

Roadmap (short version)

  - Finish multi-user + admin flows

  - Stabilise chat controller / streaming & cancel logic

  - Flesh out AI profiles and memory system

  - Add cleaner configuration for different backends

  - Polish the UI and theming

License

HamChat is licensed under the GNU GPL v3.0.
See the LICENSE file for details.
