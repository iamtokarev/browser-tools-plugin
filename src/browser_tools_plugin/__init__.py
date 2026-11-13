"""Browser tools that any agent can use over MCP."""


def main() -> None:
    """Run the `browser-tools` command: the MCP server over stdio, or `doctor`."""
    from .cli import main as run

    run()
