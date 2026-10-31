"""Распознавание узких вопросов, на которые можно ответить без вызова модели.

Быстрый маршрут покрывает только точное число строк явно выбранной таблицы и
её структуру. Любой фильтр, JOIN, группировка, ссылка на прошлый ответ или
изменение метрики оставляет вопрос обычному агенту. Имена таблиц разрешаются
только по текущему каталогу датасета: совпадение должно быть однозначным.
"""

from __future__ import annotations

import re
from datetime import UTC
from decimal import Decimal

from djedai.contracts.agent import FastPath
from djedai.contracts.catalog import DatasetCatalog
from djedai.contracts.tool_result import ToolResult

_GENERIC_TABLE = re.compile(
    r"^(?:(?:этой|данной|this|the)\s+)?"
    r"(?:таблица|таблице|таблицы|таблицу|table)$",
    re.IGNORECASE,
)
_TABLE_PREFIX = re.compile(
    r"^(?:(?:этой|данной|this|the)\s+)?"
    r"(?:таблица|таблице|таблицы|таблицу|table)\s+",
    re.IGNORECASE,
)
_QUOTE_PAIRS = {'"': '"', "'": "'", "«": "»", "“": "”", "‘": "’", "`": "`"}

_COUNT_PREFIX = (
    r"(?:сколько(?:\s+всего)?|какое\s+количество|каково\s+количество|число)\s+"
    r"(?:строк|записей)"
    r"|how\s+many\s+(?:rows|records)(?:\s+are)?"
    r"|(?:what\s+is\s+)?(?:the\s+)?(?:row\s+count|number\s+of\s+rows)"
)
_COUNT_RE = re.compile(
    rf"^(?:{_COUNT_PREFIX})"
    r"(?:\s+(?:в|во|in|of)\s+"
    r"(?:(?:этой|данной|this|the)\s+)?"
    r"(?:(?:таблице|таблицы|таблица|таблицу|table)\s*)?"
    r"(?P<table>.+))?$",
    re.IGNORECASE,
)

_STRUCTURE_PATTERNS = [
    re.compile(
        r"^(?:покажи|выведи|показать|вывести)\s+(?:мне\s+)?"
        r"(?:структуру|схему)(?:\s+(?:таблицы|таблица|таблицу))?\s*"
        r"(?P<table>.*)$"
    ),
    re.compile(
        r"^(?:структура|схема)(?:\s+(?:таблицы|таблица|таблицу))?\s*"
        r"(?P<table>.*)$"
    ),
    re.compile(
        r"^(?:какая|какова)\s+(?:структура|схема)(?:\s+(?:у|в))?\s*"
        r"(?:(?:таблицы|таблица|таблице|таблицу)\s*)?"
        r"(?P<table>.*)$"
    ),
    re.compile(
        r"^(?:опиши|описание)\s+(?:структуру|схему)"
        r"(?:\s+(?:таблицы|таблица|таблицу))?\s*(?P<table>.*)$"
    ),
    re.compile(r"^опиши\s+(?:таблицу|таблица|таблицы)\s+(?P<table>.+)$"),
    re.compile(
        r"^(?:какие|какой)\s+(?:колонки|столбцы|поля)"
        r"(?:\s+(?:в|у|есть))?\s*"
        r"(?:(?:таблице|таблицы|таблица|таблицу)\s*)?"
        r"(?P<table>.*)$"
    ),
    re.compile(
        r"^(?:колонки|столбцы|поля)"
        r"(?:\s+(?:таблицы|таблица|таблице))?\s*(?P<table>.*)$"
    ),
    re.compile(
        r"^(?:show|display|describe)\s+(?:me\s+)?(?:the\s+)?"
        r"(?:structure|schema)(?:\s+of)?\s*"
        r"(?:(?:the\s+)?(?:table|dataset)\s*)?(?P<table>.*)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:what|which)\s+(?:columns|fields)\s+(?:are\s+)?(?:in|does)\s+"
        r"(?:the\s+)?(?:(?:table)\s*)?(?P<table>.+?)(?:\s+have)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:show|display)\s+(?:me\s+)?(?:the\s+)?(?:columns|fields)\s+"
        r"(?:of|in)\s+(?:the\s+)?(?:(?:table)\s*)?(?P<table>.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"^describe\s+(?:the\s+)?(?:table\s+)?(?P<table>.+)$", re.IGNORECASE),
]


class FastPathError(ValueError):
    """Ответ быстрого маршрута не может быть подтверждён без домысливания."""

    def __init__(self, message: str, suggestion: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion


def _normalize(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?:\s*[?!.])+$", "", text)
    text = re.sub(r"\s*,?\s*(?:пожалуйста|please)$", "", text, flags=re.IGNORECASE)
    return text.strip().casefold()


def _strip_quotes(token: str) -> tuple[str, bool]:
    """Убирает обрамляющие кавычки и сообщает, были ли они."""
    cleaned = token.strip().strip("?!. ")
    if len(cleaned) >= 2:
        opener = cleaned[0]
        closer = _QUOTE_PAIRS.get(opener)
        if closer is not None and cleaned[-1] == closer:
            return cleaned[1:-1].strip(), True
    return cleaned, False


def _matching_table_ids(catalog: DatasetCatalog, name: str) -> list[str]:
    folded = name.casefold()
    return [
        table.table_id
        for table in catalog.tables
        if table.table_id.casefold() == folded or table.original_name.casefold() == folded
    ]


def _without_table_prefix(name: str) -> str:
    previous = None
    while name != previous:
        previous = name
        name = _TABLE_PREFIX.sub("", name).strip()
    return name


def _only_table_id(catalog: DatasetCatalog) -> str | None:
    """Без имени таблицы выбор допустим только при единственной таблице."""
    return catalog.tables[0].table_id if len(catalog.tables) == 1 else None


def _resolve_table(catalog: DatasetCatalog, token: str | None) -> str | None:
    """Разрешает имя таблицы по table_id или original_name, только если оно одно.

    Сначала проверяется точное имя из вопроса. Это важно, когда само имя
    начинается с «table » или «таблица »: снимать такой префикс можно только
    как запасной вариант. Имя в кавычках считается буквальным и не очищается.
    """
    if not token:
        return _only_table_id(catalog)

    cleaned, quoted = _strip_quotes(token)
    if quoted:
        candidates = [cleaned] if cleaned else []
    else:
        if _GENERIC_TABLE.fullmatch(cleaned):
            return _only_table_id(catalog)
        candidates = [cleaned]
        stripped = _without_table_prefix(cleaned)
        if stripped and stripped != cleaned:
            candidates.append(stripped)
            # Префикс может стоять вне кавычек: table "orders.csv".
            unquoted, inner_quoted = _strip_quotes(stripped)
            if inner_quoted and unquoted and unquoted != stripped:
                candidates.append(unquoted)

    for candidate in candidates:
        matches = _matching_table_ids(catalog, candidate)
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Неоднозначное точное имя нельзя «уточнять» ещё одним разбором.
            return None
    return None


def _structure_match(text: str) -> str | None:
    for pattern in _STRUCTURE_PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return match.group("table")
    return None


def classify_fast_query(text: str, catalog: DatasetCatalog) -> FastPath | None:
    """Возвращает поддерживаемый быстрый маршрут или ``None``.

    Проверка намеренно требует полного совпадения с шаблоном. Дополнительные
    условия, продолжения и уточнения не отбрасываются из вопроса: они уводят
    его в обычный маршрут, где смысл сохраняет планировщик.
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    count = _COUNT_RE.fullmatch(normalized)
    if count is not None:
        table_id = _resolve_table(catalog, count.group("table"))
        return FastPath(kind="row_count", table_id=table_id) if table_id else None

    table_token = _structure_match(normalized)
    if table_token is not None:
        table_id = _resolve_table(catalog, table_token)
        return FastPath(kind="structure", table_id=table_id) if table_id else None
    return None


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def count_sql(table_id: str) -> str:
    """Прямой COUNT(*) всей проверенной таблицы, без фильтров и группировок."""
    return f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table_id)}"


def count_from_result(result: ToolResult) -> int:
    """Читает число из результата инструмента, не подменяя NULL нулём."""
    if result.status == "error":
        error = result.error
        raise FastPathError(
            error.message if error is not None else "Не удалось посчитать строки.",
            error.suggestion if error is not None else None,
        )
    if result.data is None or not result.data.rows:
        raise FastPathError("Расчёт не вернул число строк.")
    row = result.data.rows[0]
    if not row or row[0] is None:
        raise FastPathError("Расчёт вернул NULL вместо числа строк; ответ не сформирован.")
    value = row[0]
    if isinstance(value, bool):
        raise FastPathError("Расчёт вернул не число строк.")
    if isinstance(value, int):
        count = value
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        count = int(value)
    elif isinstance(value, float) and value.is_integer():
        count = int(value)
    else:
        raise FastPathError("Расчёт вернул не число строк.")
    if count < 0:
        raise FastPathError("Расчёт вернул отрицательное число строк.")
    return count


def count_markdown(table_id: str, count: int) -> str:
    return f"Строк в таблице `{table_id}`: {count}."


def _moment(value) -> str:
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def structure_markdown(catalog: DatasetCatalog, table_id: str) -> str:
    """Структура из текущего каталога; row_count профиля сюда не подставляется."""
    table = catalog.table(table_id)
    if table is None:
        raise FastPathError("Таблица не найдена в текущем каталоге датасета.")

    lines = [f"**Структура таблицы `{table.table_id}`**"]
    notes = []
    if table.original_name != table.table_id:
        notes.append(f"исходное имя «{table.original_name}»")
    if table.description:
        notes.append(table.description)
    if notes:
        lines.append(": " + "; ".join(notes))

    for column in table.columns:
        label = f"`{column.name}`"
        if column.original_name != column.name:
            label += f" («{column.original_name}»)"
        details = [column.semantic_type, column.role]
        if column.name in table.primary_key_candidates:
            details.append("ключ")
        if column.is_unique:
            details.append("уникальная")
        if column.null_ratio:
            details.append(f"пропусков {column.null_ratio:.1%}")
        if column.description:
            details.append(column.description)
        lines.append(f"- {label} — " + ", ".join(details))

    if catalog.revision is not None:
        lines += [
            "",
            "Источник: текущий каталог датасета "
            f"(ревизия {catalog.revision.number}, профиль от "
            f"{_moment(catalog.revision.profiled_at)}).",
        ]
    else:
        lines += ["", "Источник: текущий каталог датасета."]
    return "\n".join(lines)
