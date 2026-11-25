# Contributing to Phonos

Thank you for your interest in contributing to Phonos!

## Development Setup
1. Create a Python 3.11+ virtual environment and install Phonos in editable mode:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -e ".[dev]"
   ```
2. Initialize local Playground / Production workspaces or start the dev server:
   ```bash
   .venv/bin/phonos playground
   ```
3. Run static checks and tests before opening a pull request:
   ```bash
   ./scripts/check.sh
   .venv/bin/pytest tests/test_main.py tests/test_model_io.py tests/test_custom_models.py -q
   ```

## Security & Hygiene Rules
- Never commit API keys (`nvapi-...`, `sk-...`), `.env` files, RSA private keys (`*.pem`), SQLite databases (`*.db`), or proprietary/redistribution-restricted datasets (`*.mmdb`).
- Keep third-party copyleft tools (such as GPL-licensed chemical converters) strictly optional and unbundled.

## License & Contributions
By submitting a pull request to `https://github.com/mxsp/phonos`, you confirm that you have the right to submit the contribution and agree that it may be distributed under the repository's [LICENSE](./LICENSE) as well as any commercial licenses granted by the copyright holders.
