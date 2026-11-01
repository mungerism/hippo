"""Capture launcher context before configuration can load repository dotenv files."""

import os


CLAUDE_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")


def cli():
    import sys

    if sys.argv[1:3] == ["hook", "capture"]:
        from hippo_memory.capture import try_fast_capture
        if try_fast_capture(sys.argv[1:]):
            return
    from hippo_memory.cli import app

    app()


def mcp():
    from hippo_memory.server import main

    main()
