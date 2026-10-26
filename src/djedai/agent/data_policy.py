"""Единый контракт передачи данных внешней модели (#173).

Политика описывает, какие категории контекста разрешено отправлять модели в
каждом режиме. Она применяется к структурированным данным до рендеринга промпта
(узлы графа и описания каталога) и как последний guard в ``BIModelClient`` перед
сетевым вызовом. Секреты, DSN и учётные данные не передаются никогда.

Категории и режимы описаны в ``docs/decisions/173-data-transfer-policy.md``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from dotenv import dotenv_values

REDACTED = "[REDACTED]"
POLICY_RESTRICTED_MESSAGE = "Запрос содержит данные, запрещённые к передаче модели."
RESTRICTED_CONTEXT_MESSAGE = (
    "Недостаточно данных, разрешённых к передаче модели. "
    "Уточните запрос или проверьте политику передачи данных."
)
MAX_PROMPT_ROWS = 20
MAX_CHART_ROWS = 5

# DSN/URL с учётными данными, ключи провайдеров, bearer-токены, PEM и присваивания
# секретных параметров. После редактирования значения заменяются на ``[REDACTED]``,
# поэтому повторная проверка не находит секрет.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mariadb|clickhouse|mongodb(?:\+srv)?|redis|amqp|https?)"
        r"://[^\s/@:]+:[^\s/@]+@[^\s]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret|token)\s*[=:]\s*"
        r"(?!\[REDACTED\])\S+"
    ),
)

_SENSITIVE_KEY_NAMES = {
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "privatekey",
    "dsn",
    "connectionstring",
    "accesskey",
    "secretkey",
    "clientsecret",
}
_SENSITIVE_KEY_WORDS = {
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "privatekey",
    "dsn",
}


class DataMode(StrEnum):
    """Режим ``ALLOW_DATA_SHARING``."""

    RESTRICTED = "restricted"
    SHARING = "sharing"


class DataContext(StrEnum):
    """Откуда берётся значение: это влияет на restricted-режим."""

    USER = "user"
    CURRENT_RESULT = "current_result"
    HISTORY = "history"
    CATALOG = "catalog"


class DataCategory(StrEnum):
    """Категории контекста из контракта #173."""

    METADATA = "metadata"
    USER_TEXT = "user_text"
    CELL_VALUE = "cell_value"
    FREE_TEXT = "free_text"
    FACT = "fact"
    AGGREGATE = "aggregate"
    SECRET = "secret"


class DataTransferRestricted(RuntimeError):
    """Политика не разрешила передачу; сообщение не содержит данных."""

    def __init__(self, reason: Literal["secret", "context"], *, stage: str | None = None) -> None:
        self.reason = reason
        self.stage = stage
        super().__init__(POLICY_RESTRICTED_MESSAGE)


def data_sharing_allowed() -> bool:
    """Разрешён ли полный режим передачи данных (``ALLOW_DATA_SHARING=true``).

    Безопасное значение — ``false``. Оно не запрещает согласованный внешний
    инференс: метаданные, текст пользователя и истории, факты, агрегаты и значения
    текущего результата по-прежнему передаются. Запрещены только выборки каталога
    и сырые строки прошлых результатов; секреты запрещены в обоих режимах.

    Значение берётся из окружения, затем из ``.env``; окружение процесса не меняется.
    """
    value = os.environ.get("ALLOW_DATA_SHARING")
    if value is None:
        value = dotenv_values().get("ALLOW_DATA_SHARING")
    return (value or "false").strip().lower() == "true"


@dataclass(frozen=True)
class DataTransferPolicy:
    """Одна политика на ход/клиент: режим и правила категорий."""

    mode: DataMode = DataMode.RESTRICTED

    @classmethod
    def from_env(cls) -> DataTransferPolicy:
        return cls(DataMode.SHARING if data_sharing_allowed() else DataMode.RESTRICTED)

    @classmethod
    def restricted(cls) -> DataTransferPolicy:
        return cls(DataMode.RESTRICTED)

    @classmethod
    def sharing(cls) -> DataTransferPolicy:
        return cls(DataMode.SHARING)

    @property
    def shares_values(self) -> bool:
        return self.mode is DataMode.SHARING

    def allows(self, category: DataCategory, context: DataContext = DataContext.USER) -> bool:
        """Разрешена ли категория в данном контексте."""
        if category is DataCategory.SECRET:
            return False
        if self.mode is DataMode.SHARING:
            return True
        if category in {
            DataCategory.METADATA,
            DataCategory.USER_TEXT,
            DataCategory.FACT,
            DataCategory.AGGREGATE,
        }:
            return True
        # Сырые значения нужны, чтобы ответить на текущий вопрос. Выборки каталога
        # и строки прошлых ходов — дополнительная экспозиция, поэтому запрещены.
        return context is DataContext.CURRENT_RESULT

    def sanitize_text(self, text: str | None) -> str:
        """Убирает секреты из свободного текста, сохраняя остальной смысл."""
        return _redact_text(text or "")

    def contains_secret(self, text: str | None) -> bool:
        return _contains_secret(text or "")

    def sanitize_value(self, value: Any) -> Any:
        """Рекурсивно редактирует секреты в словарях, списках и моделях."""
        return _sanitize_value(value)

    def sanitize_fact(self, fact: Any) -> dict[str, Any]:
        """Факт в виде JSON-совместимого словаря без секретов."""
        raw = _as_mapping(fact)
        text = self.sanitize_text(str(raw.get("text") or ""))
        return {
            "text": text,
            "kind": raw.get("kind"),
            "values": self.sanitize_value(raw.get("values") or {}),
        }

    def sanitize_fact_model(self, fact: Any) -> Any:
        """Копия ``Fact`` с отредактированными текстом и значениями."""
        safe = self.sanitize_fact(fact)
        if hasattr(fact, "model_copy"):
            return fact.model_copy(update={"text": safe["text"], "values": safe["values"]})
        return safe

    def sanitize_error(self, error: Any) -> Any:
        """Копия ``ToolError`` без секретов в сообщении и подсказке."""
        if error is None:
            return None
        raw = _as_mapping(error)
        message = self.sanitize_text(str(raw.get("message") or ""))
        suggestion = raw.get("suggestion")
        safe_suggestion = self.sanitize_text(str(suggestion)) if suggestion else None
        if hasattr(error, "model_copy"):
            return error.model_copy(update={"message": message, "suggestion": safe_suggestion})
        return raw | {"message": message, "suggestion": safe_suggestion}

    def result_data(
        self,
        data: Any,
        *,
        context: DataContext,
        max_rows: int = MAX_PROMPT_ROWS,
    ) -> dict[str, Any]:
        """Данные результата для промпта: метаданные всегда, строки — по политике."""
        raw = _as_mapping(data)
        columns = raw.get("columns") or []
        names = [_column_name(column) for column in columns]
        payload: dict[str, Any] = {"columns": [self.sanitize_value(column) for column in columns]}
        if raw.get("row_count") is not None:
            payload["row_count"] = raw["row_count"]
        if raw.get("truncated"):
            payload["truncated"] = True

        rows = raw.get("rows") or []
        if rows and self.allows(DataCategory.CELL_VALUE, context):
            payload["rows"] = [self._sanitize_row(row, names) for row in rows[:max_rows]]
            if len(rows) > max_rows:
                payload["rows_truncated"] = True
        elif rows:
            payload["rows_omitted"] = "data_policy"
        return payload

    def chart_result(self, result: Any, *, context: DataContext) -> dict[str, Any]:
        """Компактное описание результата для стадии visualize."""
        data = getattr(result, "data", None)
        payload: dict[str, Any] = {
            "result_id": getattr(result, "result_id", ""),
            "tool": getattr(result, "tool", None),
        }
        if data is not None:
            raw = _as_mapping(data)
            columns = raw.get("columns") or []
            names = [_column_name(column) for column in columns]
            payload["columns"] = [self.sanitize_value(column) for column in columns]
            if raw.get("row_count") is not None:
                payload["row_count"] = raw["row_count"]
            rows = raw.get("rows") or []
            if rows and self.allows(DataCategory.CELL_VALUE, context):
                payload["sample_rows"] = [
                    self._sanitize_row(row, names) for row in rows[:MAX_CHART_ROWS]
                ]
            elif rows:
                payload["sample_rows_omitted"] = "data_policy"
        facts = getattr(result, "facts", [])
        if facts:
            payload["facts"] = [
                fact["text"] for fact in (self.sanitize_fact(item) for item in facts)
            ]
        chart_hint = getattr(result, "chart_hint", None)
        if chart_hint is not None:
            payload["chart_hint"] = self.sanitize_value(chart_hint)
        return payload

    def guard(self, system_prompt: str, user_prompt: str, *, stage: str | None = None) -> None:
        """Последняя проверка перед сетью: секрет останавливает вызов."""
        if self.contains_secret(system_prompt) or self.contains_secret(user_prompt):
            raise DataTransferRestricted("secret", stage=stage)

    def _sanitize_row(self, row: Any, names: list[str]) -> list[Any]:
        return [
            REDACTED
            if index < len(names) and _is_sensitive_key(names[index])
            else self.sanitize_value(value)
            for index, value in enumerate(row)
        ]


def policy_for_client(client: Any | None) -> DataTransferPolicy:
    """Политика клиента, иначе безопасная политика из окружения.

    ``BIModelClient`` всегда хранит политику. Для подставных клиентов в тестах и
    для будущих реализаций без политики используется режим из ``ALLOW_DATA_SHARING``.
    """
    candidate = getattr(client, "data_policy", None)
    if isinstance(candidate, DataTransferPolicy):
        return candidate
    return DataTransferPolicy.from_env()


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return {}


def _column_name(column: Any) -> str:
    if isinstance(column, str):
        return column
    if isinstance(column, Mapping):
        return str(column.get("name") or "")
    return str(getattr(column, "name", "") or "")


def _key_parts(key: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return [part for part in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if part]


def _is_sensitive_key(key: str) -> bool:
    parts = _key_parts(key)
    if "".join(parts) in _SENSITIVE_KEY_NAMES:
        return True
    return any(part in _SENSITIVE_KEY_WORDS for part in parts)


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                safe[key_text] = REDACTED
            else:
                safe[_redact_text(key_text)] = _sanitize_value(item)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, bytes):
        return REDACTED
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _sanitize_value(dump(mode="json"))
        except Exception:
            return REDACTED
    return value
