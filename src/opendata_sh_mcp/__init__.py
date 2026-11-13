"""MCP server for the Schleswig-Holstein open data portal."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("opendata-sh-mcp")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0.0.0"
