"""Capture launcher context before configuration can load repository dotenv files."""

import os


CLAUDE_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")


def cli():
    from hippo_memory.cli import app

    app()


def mcp():
    from hippo_memory.server import main

    main()
