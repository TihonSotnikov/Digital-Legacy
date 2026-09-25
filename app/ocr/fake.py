"""FakeOcrProvider для тестов: заранее заданные результаты по очереди — строка или исключение."""

from __future__ import annotations

from collections import deque
from typing import Any

from app.ocr.provider import OcrUnavailable


class FakeOcrProvider:
    def __init__(self) -> None:
        self.results: deque[Any] = deque()
        self.calls = 0

    def push(self, *results: str | BaseException) -> None:
        self.results.extend(results)

    def recognize(self, image: Any) -> str:
        self.calls += 1
        if not self.results:
            raise OcrUnavailable("fake OCR: результат не задан")
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def reset(self) -> None:
        self.results.clear()
        self.calls = 0
