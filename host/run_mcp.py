"""Absolute-path launcher for MCP clients which do not support a cwd setting."""
if __package__:
    from .mcp_server import main
else:
    import pathlib
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from host.mcp_server import main
if __name__ == '__main__':
    main()
