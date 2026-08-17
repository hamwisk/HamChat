# HamChat

HamChat is a local-first Linux desktop application for talking to large language models. It combines private, Ollama-focused chat with multiple user accounts, AI Profiles, persistent memory, image-capable conversations, model-aware context planning, and safe application updates in a comfortable PyQt6 interface.

**Current release: HamChat 2.7.3**

HamChat is a complete working application under active development. Linux is its primary and currently tested platform.

## Highlights

### Local-first conversations

- Run conversations against local models through Ollama.
- Keep account data, conversations, memories, attachments, and configuration on your own machine.
- Maintain separate user accounts with administrative roles and permissions.
- Preserve conversation history, fork chats, and export or import chat JSON.
- Stream responses with cancellation and useful request diagnostics.

### HamMem persistent memory

HamMem lets HamChat recall useful information beyond the immediate context window.

- Create, inspect, edit, and delete memories in the Memory Manager.
- Scope memories to a user, chat, AI Profile, administrator, or the whole installation.
- Enable or disable memory independently for each conversation.
- Preserve a chat's HamMem preference when forking it.
- Build and rebuild embeddings used for relevant memory retrieval.
- Control memory availability through account roles and permissions.

### AI Profiles

- Create reusable AI personalities and behavioural profiles.
- Associate persistent memories with individual AI Profiles.
- Manage profile avatars and media inside HamChat's selected data directory.
- Switch profiles without losing the identity and context attached to them.

### Model-aware context planning

HamChat inspects the active Ollama model instead of assuming every model behaves alike.

- Discover the model's effective runtime context window.
- Choose **Auto**, **Low/4K**, **Mid/8K**, or **High/16K** context allocation.
- Account for the complete prompt while reserving room for the response.
- Stop requests that cannot fit and provide useful guidance instead of failing mysteriously.
- Record diagnostics for interrupted streams, malformed responses, and model preparation.

### Thinking support

Compatible models can display transient reasoning output in a dedicated, collapsible Thinking panel.

- Select an available thinking-effort level.
- Keep thinking separate from the final answer.
- Automatically disable or constrain controls according to the model's capabilities.
- Avoid storing transient thinking as ordinary conversation content.

### Images and attachments

- Attach images to conversations with compatible multimodal models.
- Accept WebP and other common raster formats supported by Pillow.
- Normalize model input to a bounded PNG while preserving the original attachment bytes.
- Store attachments in HamChat's content-addressed storage and display thumbnails in the chat interface.

### Safe automatic updates

Beginning with HamChat 2.7.1, the supported automatic-update system provides four menu actions:

- **Off**
- **Ask Before Installing**
- **Install Automatically**
- **Check for Updates**

Updates are checked after the splash screen appears and before normal database and model initialization. HamChat validates release metadata, archive size, cryptographic digest, archive structure, and the complete managed-file inventory before installation.

Installation is journaled and supports rollback and interrupted-update recovery. The updater manages declared HamChat system files only: it does not install into `data/` or `settings/`, and it blocks updates that require an incompatible database schema, data layout, or data migration.

Installations older than 2.7.1 require one final manual update before they can use this system.

## Requirements

- Python 3.10 or newer
- A modern Linux desktop
- [Ollama](https://ollama.com/) and at least one installed model for local AI responses
- Hardware appropriate for the models you choose

HamChat is developed and tested on Linux Mint. Other Linux distributions may work, but other operating systems are not currently tested.

## Installation

Clone the repository and create a virtual environment:

```bash
git clone https://github.com/hamwisk/HamChat.git
cd HamChat

python3.10 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The supplied helper can perform the virtual-environment setup instead:

```bash
./setup_venv.sh
```

## Running HamChat

The recommended launcher enables HamChat's normal logging setup:

```bash
./run_hamchat.sh
```

You can also run HamChat directly from the activated environment:

```bash
python main.py
```

For detailed terminal and file logging:

```bash
python main.py --log-level DEBUG
```

Runtime logs are written to `data/logs/app.log`.

## Configuration and user data

Application configuration and shipped model-knowledge files live under `settings/`. Optional user registry layers allow local context and modality overrides to remain separate from HamChat's shipped defaults.

HamChat keeps its user-owned state beneath its selected `data/` directory, including databases, logs, profile media, and content-addressed attachment storage. This separation lets application files be updated without replacing personal data.

You should still keep an independent backup of important user data. Automatic update safety is not a substitute for an ordinary backup policy.

## Release notes

See [`updates/2.7.3.md`](updates/2.7.3.md) for the current release notes.

For the complete HamChat 2.7 feature overview, see [`updates/2.7.0.md`](updates/2.7.0.md). The [`2.7.1 maintenance notes`](updates/2.7.1.md) document the corrected automatic-updater baseline.

## Future possibilities

HamChat's major planned capabilities are now present. Future work may include:

- Speech-to-text input
- Text-to-speech responses and voice options
- Broader remote/API backend support
- Additional platform testing and compatibility
- Continued interface, accessibility, and packaging polish

Speech features remain dependent on finding implementations that fit the available hardware without compromising the local-first experience.

## Project status

HamChat is an independently developed personal project. It is usable today, but active development means behaviour and configuration may continue to evolve. Bug reports and careful testing are welcome.

## License

HamChat is licensed under the GNU General Public License v3.0. See [`LICENSE`](LICENSE) for details.
