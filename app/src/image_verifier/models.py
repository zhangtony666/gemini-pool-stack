from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verdict: Literal["yes", "no", "uncertain"]
    reason: str = Field(min_length=1, max_length=120)


class BackendResult(BaseModel):
    verdict: Verdict
    actual_model: str
    simulated: bool = False
    provider_request_id: str | None = None


@dataclass(frozen=True)
class ImageInput:
    digest: str
    mime: str
    content: bytes
    filename: str


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class BackendError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        unknown: bool = False,
        pause: bool = False,
        retry_after: float = 0,
    ):
        # Codes only: never persist raw provider responses or secrets.
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.unknown = unknown
        self.pause = pause
        self.retry_after = retry_after
