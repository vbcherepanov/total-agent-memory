"""Entry point for pip-installed package: `tam setup` runs the wizard, `tam report` prints an activity report,
`tam daemon start|stop|status|restart` manages the shared warm daemon, `tam proxy` is its stdio front end,
`tam redact-existing` finds and redacts stored credentials, `tam import` imports chat exports and notes,
`tam export --encrypted` / `tam import-bundle` move the store between machines, `tam forget-auto` deletes the
records hooks saved on their own, anything else starts src/server.py."""

import importlib.util
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = os.path.join(_root, "src")
_server_path = os.path.join(_src, "server.py")
_server = None


def _load_server():
    global _server
    if _server is None:
        if not os.path.exists(_server_path):
            sys.stderr.write("Error: server.py not found\n")
            sys.exit(1)
        spec = importlib.util.spec_from_file_location("_server", _server_path)
        _server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_server)
    return _server


def __getattr__(name):
    if name in ("main", "run"):
        return getattr(_load_server(), name)
    raise AttributeError(name)


def _setup_wizard():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from setup_wizard import cli
    return cli


def _report_cli():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from memory_reports import cli
    return cli


def _portable_cli(command):
    """`tam import` (chat/notes importers), `tam export --encrypted` and `tam import-bundle`."""
    if _src not in sys.path:
        sys.path.insert(0, _src)
    if command == "import":
        from importers import cli
        return cli.main
    import portable_export
    return portable_export.main_export if command == "export" else portable_export.main_import


def _daemon_cli():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from tam_daemon import cli
    return cli


def _forget_auto_cli():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from ambient import forget
    return forget


def _adopt_existing_install():
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from setup_wizard.upgrade import adopt_existing
    adopt_existing(os.environ)


DISTRIBUTION = "total-agent-memory"
HELP_FLAGS = ("-h", "--help", "help")
VERSION_FLAGS = ("-V", "--version")
SUBCOMMANDS = ("setup", "report", "daemon", "proxy", "redact-existing", "import", "export", "import-bundle",
               "forget-auto")
USAGE = """usage: tam [COMMAND] [ARGS...]

With no command, tam starts the MCP server on stdio (MCP clients run it this way;
MCP_TRANSPORT=http serves HTTP). In a terminal, the first run opens the setup wizard.

commands:
  setup                  configure clients and the store (tam setup --help)
  report                 activity report for a project and period
  daemon                 start|stop|status|restart the shared warm daemon
  proxy                  stdio front end for the daemon
  redact-existing        find and redact stored credentials
  import                 import chat exports and notes
  export --encrypted     export the store to an encrypted bundle
  import-bundle          import an encrypted bundle
  forget-auto            delete records that hooks saved on their own

options:
  -h, --help             show this message and exit
  -V, --version          show the installed version and exit
"""


def _installed_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        import tomllib

        with open(os.path.join(_root, "pyproject.toml"), "rb") as handle:
            return tomllib.load(handle)["project"]["version"]


def _answer_without_server(argv) -> int | None:
    """Exit code for `tam --help` / `tam --version`, which must not start the server or touch the store."""
    if argv[:1] and argv[0] in HELP_FLAGS:
        sys.stdout.write(USAGE)
        return 0
    if argv[:1] and argv[0] in VERSION_FLAGS:
        sys.stdout.write(f"{DISTRIBUTION} {_installed_version()}\n")
        return 0
    return None


def _warn_unknown_arguments(argv) -> None:
    """The server takes no arguments; say so instead of silently starting it."""
    if argv and argv[0] not in SUBCOMMANDS:
        sys.stderr.write(f"tam: ignoring unknown argument(s): {' '.join(argv)}; starting the MCP server. "
                         "Run `tam --help` for commands.\n")


def main_sync():
    """Synchronous entry point for console_scripts."""
    argv = sys.argv[1:]
    answered = _answer_without_server(argv)
    if answered is not None:
        sys.exit(answered)
    # Human-facing subcommands print paths and arrows; the MCP transports (no argument, `proxy`) own stdout.
    if argv and argv[0] != "proxy":
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from stdio_utf8 import never_fail_on_output

        never_fail_on_output()
    if argv[:1] == ["report"]:
        sys.exit(_report_cli().main(argv[1:]))
    if argv[:1] == ["redact-existing"]:
        if _src not in sys.path:
            sys.path.insert(0, _src)
        import stored_secrets
        sys.exit(stored_secrets.main(argv[1:]))
    if argv[:1] == ["daemon"]:
        sys.exit(_daemon_cli().main(argv[1:]))
    _adopt_existing_install()
    if argv[:1] in (["import"], ["export"], ["import-bundle"]):
        sys.exit(_portable_cli(argv[0])(argv[1:]))
    if argv[:1] == ["forget-auto"]:
        sys.exit(_forget_auto_cli().main(argv[1:]))
    if argv[:1] == ["proxy"]:
        sys.exit(_daemon_cli().proxy_main(argv[1:]))
    if argv[:2] == ["setup", "register"]:
        from setup_wizard import register
        sys.exit(register.main(argv[2:]))
    if argv[:1] == ["setup"]:
        sys.exit(_setup_wizard().main(argv[1:]))
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        cli = _setup_wizard()
        if cli.should_autorun(os.environ):
            sys.exit(cli.main([], first_run=True))
    _warn_unknown_arguments(argv)
    _load_server().run()


if __name__ == "__main__":
    main_sync()
