from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def not_found(resource: str, resource_id: str) -> DomainError:
    return DomainError(
        404,
        f"{resource}_not_found",
        f"{resource.replace('_', ' ').title()} was not found",
        {"id": resource_id},
    )
