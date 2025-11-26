# Repository Guidelines

## Hard Rules for Automated Agents (Codex, Copilot, ChatGPT, etc.)

- **Do NOT create, modify, or delete any `.qml` files.**
- Treat all QML / UI layout files as **read-only**, even if a change *looks* tempting or “obvious”.
- If a bug or feature seems to require QML changes, **stop** and prefer adjustments in:
  - Python controllers / UI logic (`hamchat/ui/`, `hamchat/core/`, etc.),
  - configuration, or
  - data-handling code.
- Do not “modernize”, “refactor”, or “simplify” QML; its current behavior is intentional and must remain stable.
- Do not introduce new UI frameworks or replace QML with alternatives.

If a human explicitly asks for QML changes in a prompt, follow that prompt; otherwise, **leave QML strictly untouched**.

---

## Project Structure & Module Organization
- `main.py` boots the app and routes CLI flags to `hamchat/app.py`.
- `hamchat/` holds runtime code:
  - `core/` for settings/session helpers,
  - `ui/` for PyQt6 windows and QML integration (QML files here are **read-only for agents**),
  - `infra/` (future server/LLM plumbing),
  - shared modules like `db_ops.py`, `paths.py`, and `logging_config.py`.
- `data/` is the default writable workspace for logs, database, and models; override with `HAMCHAT_DATA_DIR` or `--data-dir`.
- `settings/` stores non-sensitive JSON config generated at runtime.
- `requirements.txt` captures pinned dependencies; `export_requirements.py` can regenerate it.

## Build, Test, and Development Commands
- Create an environment and install deps:
  - `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- Run desktop app (solo): `python main.py`.
- Server or agent modes:
  - `python main.py --ham`
  - `python main.py --snout --server-url http://localhost:8080`.
- Adjust logging and paths as needed:
  - `python main.py --log-level DEBUG --data-dir /tmp/hamchat`.

## Coding Style & Naming Conventions
- Python 3.10+ assumed; prefer type hints and `from __future__ import annotations` where helpful.
- Follow PEP 8: 4-space indentation, snake_case for functions/variables, PascalCase for classes.
- Keep UI strings and asset paths centralized; avoid hardcoding absolute paths—use helpers in `paths.py`.
- Logging: prefer structured logger names (e.g., `logging.getLogger("boot")`) and avoid `print` outside CLI errors.

## Testing Guidelines
- No automated suite is present yet; add `pytest` tests under `tests/` using `test_<module>.py` naming.
- Aim to cover mode selection, DB initialization fallbacks, and UI controller logic; keep GUI tests headless where possible.
- Run tests (once added): `pytest -q`.

## Commit & Pull Request Guidelines
- Use imperative, concise commit subjects (e.g., `Add snout mode validation`) and include rationale in the body when non-trivial.
- For PRs, include: problem statement, approach summary, testing done (commands/results), and screenshots for UI-facing changes.
- Reference related issues or tasks; call out behavior changes, migrations, or new flags (e.g., `--snout`, `--data-dir`).

## Security & Configuration Tips
- Sensitive data should not live in `settings/`; prefer environment variables for secrets and overrides.
- Database and logs default to `data/`; ensure that directory is writable in your environment.
- When running agents, validate `--server-url` endpoints and keep PyQt dependencies pinned to avoid ABI mismatches.

