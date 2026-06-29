"""Domain errors that map cleanly onto the API's ``{"error": {...}}`` shape."""

from __future__ import annotations


class GitAutomationError(Exception):
    """A domain error carrying a stable ``code`` and HTTP ``status_code``.

    The web layer renders these as ``{"error": {"code": ..., "message": ...}}``
    with ``status_code`` as the HTTP status.
    """

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def to_payload(self) -> dict[str, dict[str, str]]:
        """Return the JSON body for this error."""
        return {"error": {"code": self.code, "message": self.message}}
