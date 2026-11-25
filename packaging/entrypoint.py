"""PyInstaller entrypoint for Phonos standalone executables."""
from __future__ import annotations

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--kernel-worker":
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        from phonos_app.helpers.local_kernel_worker import main as kernel_main
        kernel_main()
        raise SystemExit(0)

    from phonos_app.cli import main as cli_main
    from phonos_app.launcher import main as launcher_main

    if len(sys.argv) > 1 and sys.argv[1] in (
        "playground",
        "production",
        "serve",
        "both",
        "setup",
        "install-ui",
        "accounts",
        "update",
        "--version",
    ):
        raise SystemExit(cli_main(sys.argv[1:]))
    raise SystemExit(launcher_main(sys.argv[1:]))
