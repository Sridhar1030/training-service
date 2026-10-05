"""Sanitized API errors shared by handwritten handlers."""


class ApiError(Exception):
    """An error using the approved ErrorResponse envelope."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
