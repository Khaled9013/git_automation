"""Entry point: run the web app with uvicorn."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    uvicorn.run("git_automation.web.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
