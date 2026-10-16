"""Compatibility launcher for the separately installed ADK client."""


def main() -> None:
    try:
        from fitcheck_adk_client.client import main as standalone_main
    except ImportError as exc:
        raise SystemExit(
            "The ADK client has a separate environment. Install "
            "./clients/adk in a clean venv and run fitcheck-adk-client; see "
            "clients/adk/README.md."
        ) from exc
    standalone_main()


if __name__ == "__main__":
    main()
