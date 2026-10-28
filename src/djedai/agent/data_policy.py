"""Единый контракт передачи данных внешней модели (#173).

Политика описывает, какие категории контекста разрешено отправлять модели в
каждом режиме. Она применяется к структурированным данным до рендеринга промпта
(узлы графа и описания каталога) и как последний guard в ``BIModelClient`` перед
сетевым вызовом. Секреты, DSN и учётные данные не передаются никогда.

Категории и режимы описаны в ``docs/decisions/173-data-transfer-policy.md``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

import sqlglot
from dotenv import dotenv_values
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

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
    # Ключ может быть в JSON/YAML: кавычки вокруг имени и значения, `:` или `=`.
    # Значение в кавычках может содержать пробелы и экранированные кавычки;
    # без кавычек — до разделителя.
    re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?key|secret|token|client[_-]?secret|"
        r"access[_-]?token|secret[_-]?key|private[_-]?key|authorization|credentials?)\b"
        r"[\"'“”‘’]?\s*[=:]\s*"
        r"(?:"
        r'"(?!\[REDACTED\])(?:[^"\\]|\\.)*"'
        r"|'(?!\[REDACTED\])(?:[^'\\]|\\.)*'"
        r"|“(?!\[REDACTED\])(?:[^”\\]|\\.)*”"
        r"|‘(?!\[REDACTED\])(?:[^’\\]|\\.)*’"
        r"|(?!\[REDACTED\])(?!\s)[^\n,;}\]\"'“”‘’]+"
        r")"
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
    # Нормализованные compound-имена теряют границы слов, поэтому перечислены
    # явно; camelCase-варианты (`userPassword`) ловятся по частям выше.
    "userpassword",
    "userpasswd",
    "passwordhash",
    "hashedpassword",
    "passwdhash",
    "hashedpasswd",
    "apitoken",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "bearertoken",
    "sessiontoken",
    "securitytoken",
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
# После нормализации compound-имя теряет границы слов. Чувствительный компонент
# в конце (`dbPassword` -> `dbpassword`) или перед суффиксом, который может
# хранить значение (`dbPasswordHash` -> `dbpasswordhash`), всё ещё означает
# credential. Суффиксы выбраны так, чтобы `tokenizer`, `secretary` и
# `passwordless` не считались секретами.
_SENSITIVE_DERIVED_SUFFIXES = {
    "hash",
    "hashed",
    "digest",
    "salt",
    "pepper",
    "value",
    "values",
    "data",
    "raw",
    "plain",
    "plaintext",
    "cleartext",
    "encoded",
    "encrypted",
    "ciphertext",
    "cipher",
    "key",
    "keys",
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

    def sanitize_fact(self, fact: Any, *, sensitive: bool = False) -> dict[str, Any]:
        """Факт в виде JSON-совместимого словаря без секретов.

        ``sensitive`` означает, что факт получен из чувствительной колонки. В этом
        случае недостаточно проверить шаблоны секретов: обычное значение пароля
        может быть числом или датой. Поэтому значения факта очищаются, а их
        текстовые представления заменяются в ``text``. Счётчики и метаданные
        (``count``, ``total``, ``distinct``, имя колонки) сохраняются.
        """
        raw = _as_mapping(fact)
        text = self.sanitize_text(str(raw.get("text") or ""))
        values = self.sanitize_value(raw.get("values") or {})
        if not isinstance(values, Mapping):
            values = {}
        values = dict(values)
        if sensitive:
            secret_values = _fact_values_to_redact(values)
            text = _replace_text_values(text, secret_values)
            values = _redact_fact_values(values)
            # Если структурированных значений нет, текст мог быть единственным
            # носителем значения чувствительной колонки.
            if not values:
                text = REDACTED
        return {"text": text, "kind": raw.get("kind"), "values": values}

    def sanitize_fact_model(self, fact: Any, *, sensitive: bool = False) -> Any:
        """Копия ``Fact`` с отредактированными текстом и значениями."""
        safe = self.sanitize_fact(fact, sensitive=sensitive)
        if hasattr(fact, "model_copy"):
            return fact.model_copy(update={"text": safe["text"], "values": safe["values"]})
        return safe

    def _result_sensitivity(
        self, result: Any, sensitive_columns: Collection[str]
    ) -> tuple[bool, set[str]]:
        """Чувствительность результата и его выходные колонки, которые нужно скрыть.

        Имена выходных колонок недостаточны: ``SELECT dbpasswordtext AS value``
        сохраняет значение пароля под нейтральным алиасом. Поэтому для SQL
        прослеживаются проекции до исходных колонок, а ``params.group_by`` и
        другие исходные параметры инструментов проверяются отдельно.
        """
        known = _folded_names(sensitive_columns)
        output_names = _result_output_names(result)
        sensitive_outputs = {
            name for name in output_names if name and _is_sensitive_name(name, known)
        }
        source_sensitive = any(
            _is_sensitive_name(name, known) for name in _result_source_names(result) if name
        )
        sql = _result_sql(result)
        if sql is not None:
            sql_outputs, unresolved = _sensitive_sql_outputs(sql, known)
            source_sensitive = source_sensitive or bool(sql_outputs) or unresolved
            if unresolved:
                sensitive_outputs.update(output_names)
            else:
                sensitive_outputs.update(sql_outputs)
        if source_sensitive and not sensitive_outputs:
            # Источник чувствителен, но выходной алиас не удалось сопоставить:
            # безопаснее скрыть все значения строк, чем пропустить пароль.
            sensitive_outputs.update(output_names)
        return source_sensitive or bool(sensitive_outputs), sensitive_outputs

    def result_has_sensitive_source(
        self, result: Any, *, sensitive_columns: Collection[str] = ()
    ) -> bool:
        """Есть ли у результата чувствительный источник или выходная колонка.

        У ``describe_column``/``value_counts``/``segment_profile`` имя источника
        лежит в ``params``; у SQL-результатов — в проекции запроса, поэтому
        алиас ``value`` не скрывает происхождение из ``dbpasswordtext``.
        Проверяются нормализованное имя, исходный заголовок и
        ``sensitive_columns`` — набор имён, полученных из ``original_name``
        каталога (``dbPasswordText`` -> ``dbpasswordtext``).
        """
        return self._result_sensitivity(result, sensitive_columns)[0]

    def sensitive_result_columns(
        self, result: Any, *, sensitive_columns: Collection[str] = ()
    ) -> set[str]:
        """Выходные имена результата, значения которых нельзя передавать модели."""
        return self._result_sensitivity(result, sensitive_columns)[1]

    def sanitize_catalog(self, catalog: Any) -> Any:
        """Копия каталога без значений чувствительных колонок.

        ``DatasetCatalog.to_prompt()`` выводит min/max числовых и временных колонок.
        Если колонка называется ``password``, сами значения не похожи на секрет и
        не ловятся шаблонами. Поэтому до рендеринга очищаются все value-bearing
        поля таких колонок: примеры, top-values, статистика и неудачные примеры
        парсинга. Метаданные (имя, тип, роль, число уникальных) остаются.
        """
        dump = getattr(catalog, "model_dump", None)
        if not callable(dump):
            return catalog
        safe = self.sanitize_value(dump(mode="json"))
        if not isinstance(safe, Mapping):
            return safe
        safe = dict(safe)
        for table in safe.get("tables") or []:
            if not isinstance(table, dict):
                continue
            for column in table.get("columns") or []:
                if not isinstance(column, dict) or not _is_sensitive_column(column):
                    continue
                sensitive_values = _column_values(column)
                column["sample_values"] = []
                column["top_values"] = []
                stats = column.get("stats")
                if isinstance(stats, dict):
                    for key in ("min", "max"):
                        if stats.get(key) is not None:
                            stats[key] = REDACTED
                    for key in ("mean", "median", "std"):
                        if key in stats:
                            stats[key] = None
                parse = column.get("parse")
                if isinstance(parse, dict) and parse.get("failed_examples"):
                    parse["failed_examples"] = []
                description = column.get("description")
                if description:
                    column["description"] = _replace_text_values(
                        self.sanitize_text(str(description)), sensitive_values
                    )
        validator = getattr(type(catalog), "model_validate", None)
        if callable(validator):
            try:
                return validator(safe)
            except Exception:
                return safe
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
        sensitive_columns: Collection[str] = (),
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
            payload["rows"] = [
                self._sanitize_row(row, names, sensitive_columns=sensitive_columns)
                for row in rows[:max_rows]
            ]
            if len(rows) > max_rows:
                payload["rows_truncated"] = True
        elif rows:
            payload["rows_omitted"] = "data_policy"
        return payload

    def chart_result(
        self,
        result: Any,
        *,
        context: DataContext,
        sensitive_columns: Collection[str] = (),
    ) -> dict[str, Any]:
        """Компактное описание результата для стадии visualize."""
        data = getattr(result, "data", None)
        sensitive_outputs = self.sensitive_result_columns(
            result, sensitive_columns=sensitive_columns
        )
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
                    self._sanitize_row(row, names, sensitive_columns=sensitive_outputs)
                    for row in rows[:MAX_CHART_ROWS]
                ]
            elif rows:
                payload["sample_rows_omitted"] = "data_policy"
        facts = getattr(result, "facts", [])
        if facts:
            sensitive = self.result_has_sensitive_source(
                result, sensitive_columns=sensitive_columns
            )
            payload["facts"] = [
                fact["text"]
                for fact in (self.sanitize_fact(item, sensitive=sensitive) for item in facts)
            ]
        chart_hint = getattr(result, "chart_hint", None)
        if chart_hint is not None:
            payload["chart_hint"] = self.sanitize_value(chart_hint)
        return payload

    def guard(self, system_prompt: str, user_prompt: str, *, stage: str | None = None) -> None:
        """Последняя проверка перед сетью: секрет останавливает вызов."""
        if self.contains_secret(system_prompt) or self.contains_secret(user_prompt):
            raise DataTransferRestricted("secret", stage=stage)

    def sanitize_description_payload(self, payload: Any) -> Any:
        """Очищает payload описания каталога с учётом имён колонок.

        ``sanitize_value`` видит только ключи ``sample_values``/``value`` и не знает,
        что колонка называется ``password``. Поэтому примеры и min/max чувствительных
        колонок заменяются на ``[REDACTED]`` до сериализации — как строки результатов.
        Обрабатываются payload таблицы (``columns``) и summary (``tables[].periods``).
        """
        safe = self.sanitize_value(payload)
        if not isinstance(payload, Mapping) or not isinstance(safe, dict):
            return safe
        raw_columns = payload.get("columns")
        safe_columns = safe.get("columns")
        if isinstance(raw_columns, list) and isinstance(safe_columns, list):
            for raw, column in zip(raw_columns, safe_columns, strict=False):
                if not isinstance(column, dict) or not _is_sensitive_column(raw):
                    continue
                for key in ("sample_values", "top_values"):
                    if key in column:
                        column[key] = _redact_examples(column[key])
                stats = column.get("stats")
                if isinstance(stats, dict):
                    for key in ("min", "max"):
                        if key in stats:
                            stats[key] = REDACTED
        raw_tables = payload.get("tables")
        safe_tables = safe.get("tables")
        if isinstance(raw_tables, list) and isinstance(safe_tables, list):
            for raw_table, safe_table in zip(raw_tables, safe_tables, strict=False):
                if not isinstance(safe_table, dict):
                    continue
                raw_periods = raw_table.get("periods") if isinstance(raw_table, Mapping) else None
                safe_periods = safe_table.get("periods")
                if not isinstance(raw_periods, list) or not isinstance(safe_periods, list):
                    continue
                for raw_period, period in zip(raw_periods, safe_periods, strict=False):
                    if not isinstance(period, dict) or not _is_sensitive_column(raw_period):
                        continue
                    for key in ("min", "max"):
                        if key in period:
                            period[key] = REDACTED
        return safe

    def _sanitize_row(
        self,
        row: Any,
        names: list[str],
        *,
        sensitive_columns: Collection[str] = (),
    ) -> list[Any]:
        known = _folded_names(sensitive_columns)
        return [
            REDACTED
            if index < len(names)
            and (names[index].casefold() in known or _is_sensitive_key(names[index]))
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


def _column_names(column: Any) -> list[str]:
    """Имена колонки, по которым определяется чувствительность."""
    if isinstance(column, str):
        return [column]
    raw = _as_mapping(column)
    return [
        str(raw.get("name") or ""),
        str(raw.get("original_name") or ""),
        str(raw.get("column") or ""),
    ]


def _folded_names(names: Collection[str]) -> set[str]:
    return {str(name).casefold() for name in names if name}


def _is_sensitive_name(name: str, known: Collection[str]) -> bool:
    """Имя колонки чувствительно по каталогу или встроенной эвристике."""
    return name.casefold() in known or _is_sensitive_key(name)


def _result_output_names(result: Any) -> list[str]:
    raw = _as_mapping(getattr(result, "data", None))
    return [_column_name(column) for column in raw.get("columns") or []]


def _result_source_names(result: Any) -> list[str]:
    """Имена исходных колонок из параметров инструмента."""
    names: list[str] = []
    params = getattr(result, "params", None)
    if not isinstance(params, Mapping):
        return names
    for key in (
        "column",
        "column_name",
        "original_name",
        "segment_col",
        "measure",
        "time_col",
        "id_column",
    ):
        value = params.get(key)
        if value:
            names.append(str(value))
    for key in ("columns", "group_by", "measures"):
        values = params.get(key)
        if isinstance(values, (list, tuple)):
            names.extend(name for column in values for name in _column_names(column))
    return names


def _result_sql(result: Any) -> str | None:
    params = getattr(result, "params", None)
    if isinstance(params, Mapping):
        sql = params.get("sql")
        if isinstance(sql, str) and sql.strip():
            return sql
    sql = getattr(result, "sql", None)
    return sql if isinstance(sql, str) and sql.strip() else None


def _root_select_scopes(root: Scope) -> list[Scope]:
    """SELECT-ветви, формирующие выходные колонки корневого запроса."""
    if isinstance(root.expression, exp.Select):
        return [root]
    if not isinstance(root.expression, exp.SetOperation):
        return []
    branches: list[Scope] = []
    for branch in root.set_operation_scopes:
        if isinstance(branch.expression, exp.Select):
            branches.append(branch)
        elif isinstance(branch.expression, exp.SetOperation):
            branches.extend(_root_select_scopes(branch))
    return branches


def _source_for_table(scope: Scope, table: str) -> Any:
    source = scope.sources.get(table)
    if source is not None:
        return source
    return next(
        (value for alias, value in scope.sources.items() if alias.casefold() == table.casefold()),
        None,
    )


def _scope_output_aliases(scope: Scope) -> list[str]:
    """Имена, заданные списком колонок у CTE или подзапроса (``t(a, b)``)."""
    parent = scope.expression.parent
    alias = parent.args.get("alias") if parent is not None else None
    if isinstance(alias, exp.TableAlias):
        return [column.name for column in alias.columns]
    return []


def _output_index(selects: list[Scope], aliases: list[str], name: str) -> int | None:
    """Позиция выходной колонки по имени или по списку псевдонимов."""
    folded = name.casefold()
    if aliases:
        return next(
            (index for index, alias in enumerate(aliases) if alias.casefold() == folded),
            None,
        )
    first = selects[0].expression
    return next(
        (
            index
            for index, projection in enumerate(first.expressions)
            if projection.alias_or_name.casefold() == folded
        ),
        None,
    )


def _projection_sensitive(
    scope: Scope,
    name: str,
    sensitive: Collection[str],
    seen: set[tuple[int, str]],
) -> bool:
    key = (id(scope), name)
    if key in seen:
        return True  # An unresolved recursive projection must remain private.
    # Track only the current recursion path. Sibling projections may reuse the
    # same CTE column and must independently retain its sensitivity.
    seen = seen | {key}
    selects = _root_select_scopes(scope)
    if not selects:
        return False
    aliases = _scope_output_aliases(scope)
    index = _output_index(selects, aliases, name)
    if index is not None:
        for select in selects:
            projections = select.expression.expressions
            if index >= len(projections):
                continue
            projection = projections[index]
            is_star, _ = _star_projection(projection)
            if is_star:
                if aliases:
                    # Переименованная звёздочка: позицию колонки в источнике
                    # без схемы определить нельзя, поэтому скрываем её.
                    return True
                names, unresolved = _star_sensitive_names(select, projection, sensitive, seen)
                if names or unresolved:
                    return True
                continue
            if _expression_mentions_sensitive(projection, select, sensitive, seen):
                return True
        return False
    # Звёздочка раскрывается в набор колонок; имя могло прийти из неё.
    for select in selects:
        for projection in select.expression.expressions:
            is_star, _ = _star_projection(projection)
            if not is_star:
                continue
            star_names, star_unresolved = _star_sensitive_names(select, projection, sensitive, seen)
            if star_unresolved or name.casefold() in _folded_names(star_names):
                return True
    # СУБД может сгенерировать имя из выражения без алиаса. Сопоставить его
    # с проекцией нельзя, поэтому проверяем все безымянные выражения.
    return any(
        not projection.alias_or_name
        and _expression_mentions_sensitive(projection, select, sensitive, seen)
        for select in selects
        for projection in select.expression.expressions
    )


def _column_mentions_sensitive(
    column: exp.Column,
    scope: Scope,
    sensitive: Collection[str],
    seen: set[tuple[int, str]],
) -> bool:
    is_star, _ = _star_projection(column)
    if is_star:
        names, _ = _star_sensitive_names(scope, column, sensitive, seen)
        return bool(names)
    name = column.name.casefold()
    if name in sensitive or _is_sensitive_key(column.name):
        return True
    sources = (
        [_source_for_table(scope, column.table)] if column.table else list(scope.sources.values())
    )
    return any(
        isinstance(source, Scope) and _projection_sensitive(source, name, sensitive, seen)
        for source in sources
        if source is not None
    )


def _expression_mentions_sensitive(
    expression: exp.Expression,
    scope: Scope,
    sensitive: Collection[str],
    seen: set[tuple[int, str]],
) -> bool:
    return any(
        _column_mentions_sensitive(column, scope, sensitive, seen)
        for column in expression.find_all(exp.Column)
    )


def _star_expression(expression: exp.Expression) -> exp.Star | None:
    """Узел звёздочки у проекции ``*`` или ``a.*``."""
    if isinstance(expression, exp.Star):
        return expression
    if isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star):
        return expression.this
    return None


def _star_projection(expression: exp.Expression) -> tuple[bool, str | None]:
    """Является ли проекция звёздочкой и какая таблица у неё указана."""
    star = _star_expression(expression)
    if star is None:
        return False, None
    table = expression.table if isinstance(expression, exp.Column) else None
    return True, table


def _star_modifier_items(star: exp.Star) -> list[exp.Alias]:
    items: list[exp.Alias] = []
    for key in ("replace", "rename"):
        items.extend(item for item in star.args.get(key) or [] if isinstance(item, exp.Alias))
    return items


def _star_modifier_names(
    star: exp.Star,
    scope: Scope,
    sensitive: Collection[str],
    seen: set[tuple[int, str]],
) -> set[str]:
    """Выходные имена ``* REPLACE``/``* RENAME``, куда попадает чувствительное значение."""
    return {
        item.alias
        for item in _star_modifier_items(star)
        if item.alias and _expression_mentions_sensitive(item.this, scope, sensitive, seen)
    }


def _star_sensitive_names(
    scope: Scope,
    expression: exp.Expression,
    sensitive: Collection[str],
    seen: set[tuple[int, str]],
) -> tuple[set[str], bool]:
    """Чувствительные колонки, раскрываемые звёздочкой; ``unresolved`` — если их не видно."""
    is_star, table = _star_projection(expression)
    if not is_star:
        return set(), False
    key = (id(scope), f"*{id(expression)}")
    if key in seen:
        return set(), True
    seen = seen | {key}
    star = _star_expression(expression)
    names = _star_modifier_names(star, scope, sensitive, seen) if star is not None else set()
    # REPLACE/RENAME могут создать одинаковые имена колонок; СУБД добавит
    # суффикс, и позицию чувствительного значения нельзя сопоставить с именем.
    # Без единого чувствительного признака модификатор безопасен.
    modifiers = _star_modifier_items(star) if star is not None else []
    unresolved = bool(
        modifiers
        and (
            bool(sensitive)
            or bool(names)
            or any(item.alias and _is_sensitive_key(item.alias) for item in modifiers)
        )
    )
    sources = [_source_for_table(scope, table)] if table else list(scope.sources.values())
    for source in sources:
        if not isinstance(source, Scope):
            # Состав таблицы неизвестен; прямые имена колонок проверит результат.
            continue
        selects = _root_select_scopes(source)
        if not selects:
            continue
        aliases = _scope_output_aliases(source)
        first = selects[0].expression
        for index, projection in enumerate(first.expressions):
            output_name = aliases[index] if index < len(aliases) else projection.alias_or_name
            is_star, _ = _star_projection(projection)
            if is_star:
                if aliases:
                    # Переименованная звёздочка: позиции выходных колонок
                    # нельзя сопоставить с исходными без схемы.
                    unresolved = True
                    continue
                inner_names, inner_unresolved = _star_sensitive_names(
                    source, projection, sensitive, seen
                )
                names.update(inner_names)
                unresolved = unresolved or inner_unresolved
                continue
            hit = any(
                index < len(branch.expression.expressions)
                and _expression_mentions_sensitive(
                    branch.expression.expressions[index], branch, sensitive, seen
                )
                for branch in selects
            )
            if not hit:
                continue
            if output_name:
                names.add(output_name)
            else:
                unresolved = True
    return names, unresolved


def _sql_mentions_sensitive(sql: str, sensitive: Collection[str]) -> bool:
    folded = sql.casefold()
    return any(name in folded for name in sensitive if name)


def _sensitive_sql_outputs(sql: str, sensitive: Collection[str]) -> tuple[set[str], bool]:
    """Выходные имена SQL, которые происходят из чувствительных колонок.

    Второе значение — ``unresolved``: SQL содержит чувствительную проекцию, но
    её выходное имя нельзя сопоставить с ``ToolResultData.columns`` (например,
    выражение без алиаса). Тогда вызывающий очищает все выходные колонки.
    """
    known = _folded_names(sensitive)
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception:
        return set(), _sql_mentions_sensitive(sql, known)
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        return set(), _sql_mentions_sensitive(sql, known)
    try:
        scopes = list(traverse_scope(statements[0]))
    except Exception:
        return set(), _sql_mentions_sensitive(sql, known)
    branches = _root_select_scopes(scopes[-1]) if scopes else []
    if not branches:
        return set(), _sql_mentions_sensitive(sql, known)

    seen: set[tuple[int, str]] = set()
    names: set[str] = set()
    unresolved = False
    first = branches[0].expression
    for index, projection in enumerate(first.expressions):
        hit = False
        for branch in branches:
            if index >= len(branch.expression.expressions):
                continue
            expression = branch.expression.expressions[index]
            is_star, _ = _star_projection(expression)
            if is_star:
                star_names, star_unresolved = _star_sensitive_names(branch, expression, known, seen)
                names.update(star_names)
                unresolved = unresolved or star_unresolved
                hit = hit or bool(star_names) or star_unresolved
            elif _expression_mentions_sensitive(expression, branch, known, seen):
                hit = True
        if not hit:
            continue
        is_star, _ = _star_projection(projection)
        if is_star:
            # В UNION звёздочка первой ветви раскрывается в несколько выходных
            # колонок, поэтому позицию чувствительной ветви нельзя сопоставить
            # с именем. Безопаснее очистить весь результат.
            if len(branches) > 1:
                unresolved = True
            continue
        alias = projection.alias_or_name
        if alias:
            names.add(alias)
        else:
            unresolved = True
    return names, unresolved


def _is_sensitive_column(column: Any) -> bool:
    return any(_is_sensitive_key(name) for name in _column_names(column) if name)


def sensitive_column_names(catalog: Any) -> tuple[str, ...]:
    """Нормализованные имена колонок, чувствительные по исходному заголовку.

    ``dbPasswordText`` после нормализации становится ``dbpasswordtext``: границы
    слов потеряны, и суффикс ``text`` сам по себе ничего не говорит о секрете.
    Поэтому чувствительность определяется по ``original_name`` каталога, а в
    результат добавляется и нормализованное имя — по нему колонку можно найти в
    ``ToolResult``.
    """
    names: set[str] = set()
    for table in _as_mapping(catalog).get("tables") or []:
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            original = str(column.get("original_name") or "")
            if (name and _is_sensitive_key(name)) or (original and _is_sensitive_key(original)):
                names.update(item for item in (name, original) if item)
    return tuple(sorted(names))


def _column_values(column: Mapping[str, Any]) -> list[Any]:
    """Значения колонки, которые нельзя выводить в промпт."""
    values: list[Any] = list(column.get("sample_values") or [])
    for item in column.get("top_values") or []:
        if isinstance(item, Mapping):
            values.append(item.get("value"))
    stats = column.get("stats")
    if isinstance(stats, Mapping):
        values.extend(stats.get(key) for key in ("min", "max", "mean", "median", "std"))
    parse = column.get("parse")
    if isinstance(parse, Mapping):
        values.extend(parse.get("failed_examples") or [])
    return [value for value in values if value is not None]


def _flatten_values(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return [leaf for item in value.values() for leaf in _flatten_values(item)]
    if isinstance(value, (list, tuple, set)):
        return [leaf for item in value for leaf in _flatten_values(item)]
    return [] if value is None else [value]


def _text_forms(value: Any) -> list[str]:
    forms = {str(value)}
    if isinstance(value, float) and value.is_integer():
        forms.add(str(int(value)))
    return [form for form in forms if form and form != REDACTED]


def _replace_text_values(text: str, values: Any) -> str:
    """Заменяет в тексте строковые представления значений чувствительной колонки."""
    forms = sorted(
        {form for value in values for form in _text_forms(value)},
        key=len,
        reverse=True,
    )
    for form in forms:
        text = text.replace(form, REDACTED)
    return text


# Факты из чувствительной колонки: счётчики и имена — агрегаты/метаданные,
# остальные значения считаются значением ячейки и очищаются.
_SAFE_FACT_VALUE_KEYS = {
    "count",
    "total",
    "non_null",
    "distinct",
    "null_count",
    "row_count",
    "rows",
    "columns",
    "segment_count",
    "total_rows",
    "size",
    "table",
    "column",
    "name",
    "label",
    "kind",
}


def _fact_values_to_redact(values: Mapping[str, Any]) -> list[Any]:
    return [
        leaf
        for key, value in values.items()
        if key not in _SAFE_FACT_VALUE_KEYS
        for leaf in _flatten_values(value)
    ]


def _redact_fact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _redact_fact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_fact_value(item) for item in value]
    return REDACTED


def _redact_fact_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value if key in _SAFE_FACT_VALUE_KEYS else _redact_fact_value(value)
        for key, value in values.items()
    }


def _redact_examples(value: Any) -> Any:
    """Сохраняет форму примеров, заменяя значения на ``[REDACTED]``."""
    if isinstance(value, Mapping):
        return {key: REDACTED if key == "value" else item for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_examples(item) for item in value]
    return REDACTED


def _key_parts(key: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key)
    return [part for part in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if part]


def _is_sensitive_suffix(suffix: str) -> bool:
    """Может ли хвост после чувствительного слова хранить его значение."""
    if not suffix:
        return True
    for derived in _SENSITIVE_DERIVED_SUFFIXES:
        if suffix == derived:
            return True
        if suffix.startswith(derived) and _is_sensitive_suffix(suffix[len(derived) :]):
            return True
    return False


def _is_sensitive_key(key: str) -> bool:
    parts = _key_parts(key)
    joined = "".join(parts)
    if joined in _SENSITIVE_KEY_NAMES:
        return True
    if any(part in _SENSITIVE_KEY_WORDS for part in parts):
        return True
    for word in _SENSITIVE_KEY_WORDS:
        start = joined.find(word)
        while start != -1:
            if _is_sensitive_suffix(joined[start + len(word) :]):
                return True
            start = joined.find(word, start + 1)
    return False


def _sanitize_json_fragments(text: str) -> str:
    """Очищает JSON-объекты и массивы внутри свободного текста.

    ``json.dumps`` экранирует кавычки внутри значений (``"prefix\\"tail"``).
    Регулярное выражение видит только часть такого значения, поэтому валидный
    JSON разбирается целиком, очищается структурно и сериализуется обратно.
    Если JSON не изменился, исходный фрагмент сохраняется без переформатирования.
    """
    if "{" not in text and "[" not in text:
        return text
    decoder = json.JSONDecoder()
    result = text
    index = 0
    while index < len(result):
        if result[index] not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(result, index)
        except (json.JSONDecodeError, ValueError, RecursionError):
            index += 1
            continue
        try:
            safe = _sanitize_value(value)
        except RecursionError:
            safe = REDACTED
        if safe == value:
            index = end
            continue
        try:
            replacement = json.dumps(safe, ensure_ascii=False)
        except (TypeError, ValueError):
            index += 1
            continue
        result = result[:index] + replacement + result[end:]
        index += len(replacement)
    return result


def _redact_text(text: str) -> str:
    redacted = _sanitize_json_fragments(text)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def _contains_secret(text: str) -> bool:
    if any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS):
        return True
    if "{" not in text and "[" not in text:
        return False
    return _sanitize_json_fragments(text) != text


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
