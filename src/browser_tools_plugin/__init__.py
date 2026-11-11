"""Browser tools that any agent can use over MCP."""


def main() -> None:
    """Run the MCP server over stdio."""
    from .server import create_server

    create_server().run()
