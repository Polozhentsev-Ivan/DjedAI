"""Детерминированная проверка чисел в ответе агента (#167).

Проверяются только числа, для которых есть происхождение в разрешённых фактах и
данных текущего ответа: факт, ячейка результата, счётчик строк, метрика, период
или явная формула из таких операндов. Модуль не ищет «похожие» числа по всей
истории и не масштабирует их сам: «тыс.», «млн» и проценты учитываются только
когда множитель или единица явно записаны в тексте.

Неподтверждённое утверждение отклоняется с безопасным кодом причины. Граф
агента не публикует такой черновик: он либо просит compose исправиться, либо
проверяет исправленный моделью текст тем же алгоритмом и при неудаче завершает
ответ явной ошибкой.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlglot
from sqlglot import exp

from djedai.contracts.tool_result import ToolResult
from djedai.contracts.verification import (
    NumericAssertion,
    VerificationDenominator,
    VerificationEvidence,
    VerificationIssue,
    VerificationOperand,
    VerificationPeriod,
    VerificationReport,
)

NUMBER_RE = re.compile(
    r"(?<![\w])"
    r"(?P<number>"
    r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?"
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d+(?:[.,]\d+)?)"
    r")"
    r"(?P<suffix>\s*(?:%|‰|тыс\.?|тысяч[а-яё]*|млн\.?|миллион[а-яё]*|млрд\.?"
    r"|миллиард[а-яё]*|трлн\.?|k|K|M|bn|RUB|USD|EUR|GBP|KZT|BYN|руб\.?"
    r"|рублей|доллар[а-яё]*|евро|тенге|₽|\$|€|£|₸))?"
    r"(?!\w)",
    re.IGNORECASE,
)

_COUNT_SUFFIX_RE = re.compile(
    r"^\s*(?:шт\.?|штук[а-яё]*|строк[а-яё]*|запис[а-яё]*|дат[а-яё]*|дн[а-яё]*|дней"
    r"|групп[а-яё]*|категор[а-яё]*|уник[а-яё]*|заказ[а-яё]*|клиент[а-яё]*"
    r"|товар[а-яё]*|таблиц[а-яё]*|значен[а-яё]*|count|rows?|items?|records?)",
    re.IGNORECASE,
)
_PERCENT_SUFFIX_RE = re.compile(r"^\s*(?:%|процент[а-яё]*|п\.п\.)", re.IGNORECASE)
_CURRENCY_AFTER_RE = re.compile(
    r"^\s*(RUB|USD|EUR|GBP|KZT|BYN|руб\.?|рублей|доллар[а-яё]*|евро|тенге|₽|\$|€|£|₸)",
    re.IGNORECASE,
)
_SCALE_SUFFIX_RE = re.compile(
    r"^\s*(тыс\.?|тысяч[а-яё]*|млн\.?|миллион[а-яё]*|млрд\.?|миллиард[а-яё]*"
    r"|трлн\.?|k|M|bn)\b",
    re.IGNORECASE,
)

_LIST_MARKER_RE = re.compile(
    r"(?m)^(?P<prefix>\s{0,3}(?:[-*+]\s+)?)(?P<num>\d{1,3})(?P<tail>[.)])\s+"
)
_VERSION_RE = re.compile(
    r"(?<![\w])(?P<raw>(?:v|версия|version)\s*\d+(?:\.\d+)*)(?![\w])", re.IGNORECASE
)
_CODE_RE = re.compile(r"`[^`\n]*`")
_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)
# «Топ-5»: дефис в названии, а не отрицательное число.
_NAME_NUMBER_RE = re.compile(r"(?<=[^\W\d_])-\d+(?![\d.,]*\d)")
_URL_RE = re.compile(r"https?://\S+")
_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_ORDINAL_RE = re.compile(r"(?<!\d)\d{1,2}-(?:й|я|е|го|му|м|х)(?![а-яё])", re.IGNORECASE)
_CLAUSE_PUNCT_RE = re.compile(r"[!?;\n]|\.(?=\s|$)")
# Границы подписей внутри факта: «Мин: 10, Макс: 100». Запятая внутри
# десятичной дроби («31,62») и разрядов («1,000») не разделяет подписи.
_FACT_SEPARATOR_RE = re.compile(r"[;|\n]|,(?=\s*[A-Za-zА-Яа-яЁё])")

_ISO_DATETIME_RE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?(?!\d)"
)
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?!\d)")
_ISO_MONTH_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})(?![\d-])")
_YEAR_RANGE_RE = re.compile(
    r"(?<![\d])((?:19|20)\d{2})\s*(?:[-–—]|по|до)\s*((?:19|20)\d{2})(?![\d])",
    re.IGNORECASE,
)
_YEAR_WORD_RE = re.compile(
    r"(?<![\d])((?:19|20)\d{2})\s*(?:год|году|года|годов|гг?\.)(?![а-яё])",
    re.IGNORECASE,
)
_YEAR_PREPOSITION_RE = re.compile(
    r"(?<![\w\d])(?:в|за|с|по|до|на|от|к)\s+((?:19|20)\d{2})(?![\d-])",
    re.IGNORECASE,
)
_QUARTER_RE = re.compile(
    r"(?<!\w)(?:(?P<year1>(?:19|20)\d{2})\s*[-–—]?\s*)?"
    r"(?:Q\s*(?P<q1>[1-4])|(?P<q2>[1-4])\s*(?:кв\.?|квартал[а-яё]*))"
    r"(?:\s*[-–—]?\s*(?P<year2>(?:19|20)\d{2}))?(?!\w)",
    re.IGNORECASE,
)

_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_MONTH_YEAR_RE = re.compile(
    r"(?<!\w)(?P<month>"
    + "|".join(sorted((re.escape(key) for key in _MONTHS), key=len, reverse=True))
    + r")[а-яё]*\s+(?P<year>(?:19|20)\d{2})(?!\d)",
    re.IGNORECASE,
)

_POPULATION_RE = re.compile(
    r"(?<![\w])(?P<marker>без|исключая|кроме|только|среди|among|without|excluding|except)"
    r"\s+(?P<body>[^,.;:()\n]{2,60})",
    re.IGNORECASE,
)

_STOPWORDS = {
    "в",
    "во",
    "на",
    "за",
    "с",
    "со",
    "по",
    "до",
    "от",
    "к",
    "ко",
    "из",
    "для",
    "и",
    "или",
    "а",
    "но",
    "что",
    "это",
    "как",
    "при",
    "около",
    "примерно",
    "всего",
    "итого",
    "составляет",
    "составил",
    "составила",
    "составило",
    "равно",
    "равняется",
    "было",
    "будет",
    "есть",
    "данным",
    "данные",
    "показатель",
    "значение",
    "число",
    "год",
    "года",
    "году",
    "годы",
    "годов",
    "гг",
    "квартал",
    "квартала",
    "квартале",
    "месяц",
    "месяца",
    "месяце",
    "месяцев",
    "период",
    "периоде",
    "the",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "to",
    "by",
    "at",
    "as",
    "with",
    "from",
}

_SCALE_WORDS = {
    "руб": "RUB",
    "руб.": "RUB",
    "рублей": "RUB",
    "₽": "RUB",
    "доллар": "USD",
    "долларов": "USD",
    "доллара": "USD",
    "$": "USD",
    "usd": "USD",
    "евро": "EUR",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "тенге": "KZT",
    "₸": "KZT",
    "kzt": "KZT",
    "byn": "BYN",
}

_LABEL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("__total__", ("итог", "сумм", "total", "sum", "всего")),
    (
        "__count__",
        (
            "количество",
            "кол-во",
            "count",
            "дат",
            "дней",
            "групп",
            "категор",
            "уник",
            "шт",
            "штук",
        ),
    ),
    ("__rows__", ("row", "rows", "строк", "запис")),
    ("__percent__", ("процент", "pct", "percent", "share", "ratio", "доля", "дол")),
    ("__average__", ("средн", "average", "avg", "mean")),
    ("__min__", ("мин", "min", "минимум")),
    ("__max__", ("макс", "max", "максимум")),
    ("__median__", ("медиан", "median")),
    ("__std__", ("std", "stdev", "стандарт", "отклон", "deviation")),
    ("__distinct__", ("уникаль", "distinct", "unique")),
    ("__missing__", ("пропуск", "null", "пуст", "missing")),
    ("__revenue__", ("выручк", "revenue", "sales", "продаж")),
    ("__amount__", ("amount", "сумм", "стоим", "цена")),
    ("__orders__", ("заказ", "order")),
    ("__customers__", ("клиент", "customer", "покупател")),
    ("__cancel__", ("отмен", "cancel")),
    ("__active__", ("актив", "active")),
    ("__errors__", ("ошиб", "error", "fail", "сбо")),
    ("__attempts__", ("попыт", "attempt", "попыток")),
)

_FORMULA_CONSTANTS = {Decimal(0), Decimal(1), Decimal(100), Decimal(1000)}
_FORMULA_OPERATORS = "+-*/×÷:"

_REASON_TEXT = {
    "number_not_found": "Число не найдено среди фактов и данных текущего ответа.",
    "ambiguous_number": "Число встречается в нескольких несвязанных результатах.",
    "unrelated_result": "Подпись числа относится к другому результату.",
    "unit_mismatch": "Единица измерения числа не совпадает с источником.",
    "currency_mismatch": "Валюта числа не подтверждена источником.",
    "period_mismatch": "Период числа не подтверждён источником.",
    "population_mismatch": "Популяция или фильтр числа не подтверждены источником.",
    "denominator_mismatch": "Знаменатель доли не подтверждён источником.",
    "zero_denominator": "Знаменатель формулы равен нулю.",
    "missing_operand": "Операнд формулы не найден в фактах и данных.",
    "invalid_formula": "Формула не разобрана или её операнды несовместимы.",
    "rounding_mismatch": "Результат формулы не совпадает с указанным значением.",
    "unsupported_assertion": "Утверждение не относится к поддерживаемым числовым проверкам.",
    "non_analytical": "Не аналитическое число: дата, нумерация списка или константа.",
    "llm_reported": "Модель отметила утверждение как неподтверждённое.",
    "invalid_correction": "Исправленный моделью текст не прошёл детерминированную проверку.",
    "llm_unavailable": "Проверка ответа моделью недоступна.",
}


@dataclass(frozen=True)
class _NumberToken:
    start: int
    end: int
    raw: str
    value: Decimal
    scale: Decimal
    unit: str
    currency: str | None
    decimals: int


@dataclass(frozen=True)
class _DateClaim:
    start: int
    end: int
    raw: str
    period: VerificationPeriod


@dataclass(frozen=True)
class _FactSegment:
    """Фрагмент факта с одной подписью: «Мин: 10» или «Среднее: 55»."""

    start: int
    end: int
    text: str


@dataclass
class _Draft:
    kind: str
    text: str
    raw_text: str
    value: Decimal | None
    scale: Decimal = Decimal(1)
    unit: str = "unknown"
    currency: str | None = None
    period: VerificationPeriod | None = None
    population: str | None = None
    denominator: VerificationDenominator | None = None
    label: str | None = None
    formula: str | None = None
    operands: list[VerificationOperand] = field(default_factory=list)
    allow_bare_year_period: bool = False
    status: str = "verified"
    reason_code: str | None = None
    reason: str | None = None
    evidence: list[VerificationEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class _FormulaSpec:
    left: tuple[_NumberToken, ...]
    result: _NumberToken
    expression: str
    raw: str
    start: int
    end: int
    percent_result: bool = False
    tree: ast.Expression | None = None


class _FormulaError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse_decimal(raw: str) -> tuple[Decimal, int]:
    """Разбирает число в русской или английской записи без неявных множителей."""
    text = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    decimals = 0
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
        decimals = len(text.rsplit(".", 1)[-1]) if "." in text else 0
    elif "," in text:
        parts = text.split(",")
        if len(parts) > 2:
            text = "".join(parts)
        elif len(parts[1]) == 3 and parts[0] not in ("", "0"):
            text = parts[0] + parts[1]
        else:
            text = parts[0] + "." + parts[1]
            decimals = len(parts[1])
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 2:
            if all(len(part) == 3 for part in parts[1:]) and len(parts[0]) <= 3:
                text = "".join(parts)
            else:
                text = "".join(parts[:-1]) + "." + parts[-1]
                decimals = len(parts[-1])
        else:
            decimals = len(parts[1])
    try:
        return Decimal(text or "0"), decimals
    except InvalidOperation as exc:
        raise ValueError(raw) from exc


def _currency(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower().rstrip(".")
    if cleaned in _SCALE_WORDS:
        return _SCALE_WORDS[cleaned]
    if cleaned.startswith("доллар"):
        return "USD"
    if cleaned.startswith("руб"):
        return "RUB"
    return cleaned.upper() if cleaned in {"rub", "usd", "eur", "gbp", "kzt", "byn"} else None


def _scale(value: str | None) -> Decimal | None:
    if not value:
        return None
    cleaned = value.strip().lower().rstrip(".")
    if cleaned.startswith("тыс") or cleaned == "k":
        return Decimal(1000)
    if cleaned.startswith(("млн", "миллион")) or cleaned == "m":
        return Decimal(1000000)
    if cleaned.startswith(("млрд", "миллиард")) or cleaned == "bn":
        return Decimal(1000000000)
    if cleaned.startswith("трлн"):
        return Decimal(1000000000000)
    return None


def _tokens(text: str) -> list[_NumberToken]:
    tokens: list[_NumberToken] = []
    for match in NUMBER_RE.finditer(text):
        raw = match.group("number")
        suffix = match.group("suffix") or ""
        start = match.start("number")
        sign = ""
        if start > 0 and text[start - 1] in "+-−":
            before_sign = text[start - 2] if start > 1 else ""
            # Знак относится к числу, только если он не является оператором
            # между двумя операндами: ``1000-200`` и ``1000 - 200`` — формулы,
            # ``-500`` и ``= -500`` — отрицательные значения.
            if not before_sign.isdigit() and before_sign not in (")", "]", "}"):
                sign = "-" if text[start - 1] in "-−" else ""
                start -= 1
        try:
            value, decimals = _parse_decimal(raw)
        except ValueError:
            continue
        if sign:
            value = -value
        scale = _scale(suffix)
        unit = "unknown"
        currency: str | None = None
        if "%" in suffix or "‰" in suffix:
            unit = "percent"
        else:
            currency = _currency(suffix.strip()) if suffix else None
            if currency:
                unit = "money"
            elif scale is not None:
                unit = "number"
        after = text[match.end() : match.end() + 24]
        if unit == "unknown":
            percent = _PERCENT_SUFFIX_RE.match(after)
            if percent:
                unit = "percent"
            else:
                currency_match = _CURRENCY_AFTER_RE.match(after)
                currency = _currency(currency_match.group(1)) if currency_match else None
                if currency:
                    unit = "money"
                elif _COUNT_SUFFIX_RE.match(after):
                    unit = "count"
        elif unit == "number":
            currency_match = _CURRENCY_AFTER_RE.match(after)
            if currency_match:
                currency = _currency(currency_match.group(1))
                if currency:
                    unit = "money"
        tokens.append(
            _NumberToken(
                start=start,
                end=match.end(),
                raw=(sign + raw) if sign else raw,
                value=value,
                scale=scale or Decimal(1),
                unit=unit,
                currency=currency,
                decimals=decimals,
            )
        )
    return tokens


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _year_period(year: int, raw: str, *, grain: str = "year") -> VerificationPeriod:
    return VerificationPeriod(
        start=date(year, 1, 1).isoformat(),
        end=date(year + 1, 1, 1).isoformat(),
        label=raw.strip(),
        grain=grain,  # type: ignore[arg-type]
    )


def _month_period(year: int, month: int, raw: str) -> VerificationPeriod:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return VerificationPeriod(
        start=start.isoformat(), end=end.isoformat(), label=raw.strip(), grain="month"
    )


def _day_period(value: date, raw: str) -> VerificationPeriod:
    return VerificationPeriod(
        start=value.isoformat(),
        end=date.fromordinal(value.toordinal() + 1).isoformat(),
        label=raw.strip(),
        grain="day",
    )


def _quarter_period(year: int, quarter: int, raw: str) -> VerificationPeriod:
    first_month = (quarter - 1) * 3 + 1
    start = date(year, first_month, 1)
    end_year = year + (quarter == 4)
    end_month = 1 if quarter == 4 else first_month + 3
    return VerificationPeriod(
        start=start.isoformat(),
        end=date(end_year, end_month, 1).isoformat(),
        label=raw.strip(),
        grain="quarter",
    )


def _week_period(start: date, raw: str) -> VerificationPeriod:
    return VerificationPeriod(
        start=start.isoformat(),
        end=(start + timedelta(days=7)).isoformat(),
        label=raw.strip(),
        grain="week",
    )


_FREQ_GRAIN = {
    "minute": "day",
    "hour": "day",
    "day": "day",
    "week": "week",
    "month": "month",
    "quarter": "quarter",
    "year": "year",
}


def _result_period_grain(result: ToolResult | None) -> str:
    """Частота временного бакета результата: ``time_aggregate``/``time_summary``.

    Строка ``2024-10-01T00:00:00`` сама по себе выглядит как день. Только
    параметр ``freq`` или факт ``time_summary`` говорят, что это начало месяца
    или квартала. Без такого свидетельства дата остаётся днём.
    """
    if result is None:
        return "day"
    params = getattr(result, "params", None)
    if isinstance(params, Mapping):
        freq = params.get("freq")
        if isinstance(freq, str) and freq.lower() in _FREQ_GRAIN:
            return _FREQ_GRAIN[freq.lower()]
    for fact in getattr(result, "facts", ()) or ():
        values = fact.get("values") if isinstance(fact, Mapping) else getattr(fact, "values", None)
        if not isinstance(values, Mapping):
            continue
        freq = values.get("freq")
        if isinstance(freq, str) and freq.lower() in _FREQ_GRAIN:
            return _FREQ_GRAIN[freq.lower()]
    return "day"


def _bucket_period(period: VerificationPeriod, grain: str) -> VerificationPeriod:
    """Расширяет дату начала бакета до месяца, квартала, года или недели."""
    start = _date(period.start)
    if start is None or grain not in ("week", "month", "quarter", "year"):
        return period
    if grain == "week":
        return _week_period(start, period.label or "")
    if grain == "month":
        return _month_period(start.year, start.month, f"{start.year}-{start.month:02d}")
    if grain == "quarter":
        quarter = (start.month - 1) // 3 + 1
        return _quarter_period(start.year, quarter, f"{start.year}-Q{quarter}")
    return _year_period(start.year, str(start.year))


def _date_claims(text: str) -> list[_DateClaim]:
    claims: list[_DateClaim] = []
    for match in _ISO_DATETIME_RE.finditer(text):
        parsed = _date(match.group(0))
        if parsed:
            claims.append(
                _DateClaim(
                    match.start(), match.end(), match.group(0), _day_period(parsed, match.group(0))
                )
            )
    for match in _ISO_DATE_RE.finditer(text):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        claims.append(
            _DateClaim(
                match.start(), match.end(), match.group(0), _day_period(parsed, match.group(0))
            )
        )
    for match in _DMY_DATE_RE.finditer(text):
        year = int(match.group(3))
        if year < 100:
            year += 2000
        try:
            parsed = date(year, int(match.group(2)), int(match.group(1)))
        except ValueError:
            continue
        claims.append(
            _DateClaim(
                match.start(), match.end(), match.group(0), _day_period(parsed, match.group(0))
            )
        )
    for match in _ISO_MONTH_RE.finditer(text):
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            continue
        claims.append(
            _DateClaim(
                match.start(),
                match.end(),
                match.group(0),
                _month_period(parsed.year, parsed.month, match.group(0)),
            )
        )
    for match in _MONTH_YEAR_RE.finditer(text):
        month_text = match.group("month").lower()
        month = next((value for key, value in _MONTHS.items() if month_text.startswith(key)), None)
        if month is None:
            continue
        claims.append(
            _DateClaim(
                match.start(),
                match.end(),
                match.group(0),
                _month_period(int(match.group("year")), month, match.group(0)),
            )
        )
    for match in _QUARTER_RE.finditer(text):
        quarter = int(match.group("q1") or match.group("q2"))
        year_text = match.group("year1") or match.group("year2")
        year = int(year_text) if year_text else None
        if year is None:
            period = VerificationPeriod(label=match.group(0).strip(), grain="quarter")
        else:
            period = _quarter_period(year, quarter, match.group(0))
        claims.append(_DateClaim(match.start(), match.end(), match.group(0), period))
    for match in _YEAR_RANGE_RE.finditer(text):
        start_year = int(match.group(1))
        end_year = int(match.group(2))
        if start_year > end_year:
            start_year, end_year = end_year, start_year
        claims.append(
            _DateClaim(
                match.start(),
                match.end(),
                match.group(0),
                VerificationPeriod(
                    start=date(start_year, 1, 1).isoformat(),
                    end=date(end_year + 1, 1, 1).isoformat(),
                    label=match.group(0).strip(),
                    grain="range",
                ),
            )
        )
    for match in _YEAR_WORD_RE.finditer(text):
        claims.append(
            _DateClaim(
                match.start(),
                match.end(),
                match.group(0),
                _year_period(int(match.group(1)), match.group(0)),
            )
        )
    for match in _YEAR_PREPOSITION_RE.finditer(text):
        claims.append(
            _DateClaim(
                match.start(),
                match.end(),
                match.group(0),
                _year_period(int(match.group(1)), match.group(0)),
            )
        )
    chosen: list[_DateClaim] = []
    for claim in sorted(claims, key=lambda item: (item.start, -(item.end - item.start))):
        if any(not (claim.end <= other.start or claim.start >= other.end) for other in chosen):
            continue
        chosen.append(claim)
    return sorted(chosen, key=lambda item: item.start)


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    end = len(text)
    for match in _CLAUSE_PUNCT_RE.finditer(text):
        if match.end() <= position:
            start = match.end()
        elif match.start() >= position:
            end = match.start()
            break
    return start, end


def _clause(text: str, position: int) -> str:
    start, end = _clause_bounds(text, position)
    return text[start:end]


def _period_for(
    text: str, position: int, claims: Sequence[_DateClaim]
) -> VerificationPeriod | None:
    start, end = _clause_bounds(text, position)
    inside = [claim for claim in claims if claim.start >= start and claim.end <= end]
    if not inside:
        return None
    before = [claim for claim in inside if claim.end <= position]
    if before:
        return before[-1].period
    return inside[0].period


def _population(text: str) -> str | None:
    found = []
    for match in _POPULATION_RE.finditer(text):
        body = match.group("body").strip().strip(".,;:")
        if body:
            found.append(f"{match.group('marker').lower()} {body}")
    return "; ".join(dict.fromkeys(found)) or None


def _period_covered(claim: VerificationPeriod, evidence: Iterable[VerificationPeriod]) -> bool:
    periods = list(evidence)
    if not periods:
        return False
    claim_start = _date(claim.start)
    claim_end = _date(claim.end)
    # Период подтверждает значение, только если это тот же самый период.
    # Год не подтверждает месяц, а месяц — конкретный день: иначе число,
    # посчитанное за весь год, выглядело бы проверенным для октября.
    if claim_start and claim_end:
        for period in periods:
            start = _date(period.start)
            end = _date(period.end)
            if start and end and start == claim_start and end == claim_end:
                return True
            if (
                period.label
                and claim.label
                and period.label.strip() == claim.label.strip()
                and not (start and end)
            ):
                return True
        return False
    if claim_start:
        for period in periods:
            start = _date(period.start)
            end = _date(period.end)
            if start and end and start <= claim_start < end and period.grain == claim.grain:
                return True
            if (
                period.label
                and claim.label
                and period.label.strip() == claim.label.strip()
                and not (start and end)
            ):
                return True
        return False
    if claim.label:
        wanted = _label_keys(claim.label)
        return any(wanted and wanted & _label_keys(period.label or "") for period in periods)
    return True


def _label_keys(text: str | None) -> set[str]:
    if not text:
        return set()
    normalized = text.lower().replace("ё", "е").replace("_", " ")
    keys: set[str] = set()
    if re.search(r"\bcount\s*\(\s*\*\s*\)", normalized):
        keys.add("__rows__")
    for token in re.findall(r"[a-zа-я0-9_]+", normalized):
        if token in _STOPWORDS or token.isdigit() or len(token) < 2:
            continue
        stem = token[:5] if len(token) > 5 else token
        keys.add(stem)
        for group, roots in _LABEL_GROUPS:
            if any(token.startswith(root) or root.startswith(stem) for root in roots):
                keys.add(group)
    return keys


def _same_label(left: str | None, right: str | None) -> bool:
    left_keys = _label_keys(left)
    right_keys = _label_keys(right)
    aggregates = {
        "__average__",
        "__min__",
        "__max__",
        "__median__",
        "__std__",
        "__variance__",
        "__total__",
    }
    requested = left_keys & aggregates
    actual = right_keys & aggregates
    # Общее имя меры (amount) не делает AVG и MAX одной операцией.
    if requested and actual and requested.isdisjoint(actual):
        return False
    counts = {"__count__", "__rows__"}
    if (
        "__rows__" in left_keys
        and "__rows__" not in right_keys
        and right_keys & (aggregates | counts)
    ):
        return False
    # «Всего строк» допустимо для COUNT(*), но среднее/минимум/максимум — нет.
    if requested - {"__total__"} and not actual and right_keys & counts:
        return False
    if actual - {"__total__"} and not requested and left_keys & counts:
        return False
    return bool(left_keys and right_keys and left_keys & right_keys)


def _population_compatible(left: str | None, right: str | None) -> bool:
    if not left:
        return True
    if not right:
        return False

    def signature(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
        negative_words = {"без", "кроме", "without", "excluding", "except"}
        scope_words = {"только", "среди", "among", "only"}
        conditions: list[tuple[str, tuple[str, ...]]] = []
        marker = "context"
        body: list[str] = []

        def normalize(word: str) -> str:
            if re.fullmatch(r"[а-я]+", word):
                stem = re.sub(
                    r"(?:ыми|ими|ого|его|ому|ему|ых|их|ые|ие|ый|ий|ой|ая|яя|ое|ее|ую|юю|"
                    r"ами|ями|ов|ев|ам|ям|ах|ях|ом|ем|ы|и|а|я|у|ю|е)$",
                    "",
                    word,
                )
                return stem if len(stem) >= 3 else word
            return word[:-1] if len(word) > 3 and word.endswith("s") else word

        for word in _words(text):
            is_negative = word in negative_words or word == "исключая"
            if is_negative or word in scope_words:
                if body or marker != "context":
                    conditions.append((marker, tuple(body)))
                marker = "exclude" if is_negative else "include"
                body = []
                continue
            body.append(normalize(word))
        if body or marker != "context":
            conditions.append((marker, tuple(body)))
        return tuple(conditions)

    # Общие слова «без»/without и «заказы»/orders не подтверждают условие
    # фильтра. Каждый маркер остаётся связан со своим условием; общий набор
    # слов не разрешает переставить включённую и исключённую популяции.
    claimed = signature(left)
    actual = signature(right)
    return bool(claimed) and claimed == actual


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9_]+", text.lower().replace("ё", "е"))


def _normalize_period(period: VerificationPeriod | None) -> tuple[str, ...] | None:
    if period is None:
        return None
    return (period.start or "", period.end or "", period.label or "", period.grain)


def _normalize_population(population: str | None) -> str | None:
    return " ".join(_words(population)) if population else None


def _unknown_fields(
    evidence: VerificationEvidence,
) -> list[str]:
    unknown = []
    if evidence.value is None:
        unknown.append("value")
    if evidence.period is None:
        unknown.append("period")
    if not evidence.population:
        unknown.append("population")
    if evidence.denominator is None:
        unknown.append("denominator")
    if evidence.currency is None:
        unknown.append("currency")
    return unknown


def _evidence(
    *,
    result_id: str,
    source: str,
    value: Decimal | None,
    unit: str = "unknown",
    scale: Decimal = Decimal(1),
    currency: str | None = None,
    period: VerificationPeriod | None = None,
    population: str | None = None,
    denominator: VerificationDenominator | None = None,
    label: str | None = None,
    fact_id: str | None = None,
    cell_address: str | None = None,
) -> VerificationEvidence:
    item = VerificationEvidence(
        result_id=result_id,
        fact_id=fact_id,
        cell_address=cell_address,
        value=value,
        scale=scale,
        unit=unit,  # type: ignore[arg-type]
        currency=currency,
        period=period,
        population=population,
        denominator=denominator,
        label=label,
        source=source,  # type: ignore[arg-type]
    )
    return item.model_copy(update={"unknown": _unknown_fields(item)})


def _metric_population(metric: Any) -> str | None:
    if metric is None:
        return None
    parts = []
    for condition in getattr(metric, "filters", []) or []:
        column = getattr(condition, "column", None)
        name = (
            f"{getattr(column, 'table', '')}.{getattr(column, 'column', '')}".strip(".")
            if column is not None
            else "фильтр"
        )
        op = getattr(condition, "op", "")
        value = getattr(condition, "value", None)
        negative = " без " if op in ("ne", "not_in") else " "
        parts.append(f"{name}{negative}{value}".strip())
    if not parts:
        grain = getattr(metric, "grain", None)
        table = getattr(grain, "table", None) if grain is not None else None
        return f"все строки {table}" if table else "все строки"
    return "; ".join(parts)


def _period_grain_for(start_date: date, end_date: date) -> str:
    if end_date == start_date + timedelta(days=1):
        return "day"
    if end_date == start_date + timedelta(days=7):
        return "week"
    if start_date.day == 1 and end_date.day == 1:
        months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
        if months == 1:
            return "month"
        if months == 3 and (start_date.month - 1) % 3 == 0:
            return "quarter"
        if months == 12 and start_date.month == 1:
            return "year"
    return "range"


def _metric_period(metric: Any) -> VerificationPeriod | None:
    if metric is None:
        return None
    start = getattr(metric, "period_start", None)
    end = getattr(metric, "period_end", None)
    if not start and not end:
        return None
    start_date = _date(start)
    end_date = _date(end)
    if start_date and end_date:
        grain = _period_grain_for(start_date, end_date)
        if grain == "year":
            label = str(start_date.year)
        elif grain == "month":
            label = f"{start_date.year}-{start_date.month:02d}"
        elif grain == "quarter":
            quarter = (start_date.month - 1) // 3 + 1
            label = f"{start_date.year}-Q{quarter}"
        elif grain == "day":
            label = start_date.isoformat()
        elif grain == "range" and start_date.month == 1 and start_date.day == 1:
            label = f"{start_date.year}–{end_date.year - 1}"
        else:
            label = f"{start} – {end}"
    else:
        label = f"{start or '…'} – {end or '…'}"
        grain = "unknown"
    return VerificationPeriod(start=start, end=end, label=label, grain=grain)  # type: ignore[arg-type]


# --- Сбор происхождения из очищенных фактов и данных ответа ---

_MONEY_WORDS = (
    "amount",
    "revenue",
    "sales",
    "cost",
    "price",
    "сумм",
    "выручк",
    "стоим",
    "цена",
    "доход",
    "прибыл",
)
_COUNT_WORDS = (
    "count",
    "кол-во",
    "количество",
    "row_count",
    "строк",
    "запис",
    "штук",
    "orders",
    "order_count",
)
_PERCENT_WORDS = ("percent", "pct", "процент", "п.п.")
_RATIO_WORDS = ("share", "ratio", "доля", "дол")


def _column_name(column: Any) -> str:
    if isinstance(column, Mapping):
        return str(column.get("name") or "")
    return str(column or "")


def _column_type(column: Any) -> str | None:
    if isinstance(column, Mapping):
        return str(column.get("type") or "") or None
    return None


def _column_label(column: Any) -> str:
    if isinstance(column, Mapping):
        return str(column.get("label") or column.get("name") or "")
    return str(column or "")


def _unit_from_name(name: str, default: str = "unknown") -> str:
    lowered = name.lower()
    if any(word in lowered for word in _PERCENT_WORDS):
        return "percent"
    if any(word in lowered for word in _RATIO_WORDS):
        return "ratio"
    if any(word in lowered for word in _COUNT_WORDS):
        return "count"
    if any(word in lowered for word in _MONEY_WORDS):
        return "money"
    return default


def _currency_from_text(text: str) -> str | None:
    for token in _tokens(text):
        if token.currency:
            return token.currency
    lowered = text.lower()
    for word, code in _SCALE_WORDS.items():
        if word in lowered:
            return code
    return None


def _numeric_parts(value: Any) -> tuple[Decimal, Decimal, str, str | None, int] | None:
    """Число ячейки/факта с явным масштабом; даты и bool не считаются числами."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        decimals = max(0, -decimal.as_tuple().exponent)
        return decimal, Decimal(1), "number", None, decimals
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or _date_claims(stripped):
            return None
        tokens = _tokens(stripped)
        if len(tokens) != 1:
            return None
        token = tokens[0]
        if stripped[token.start : token.end] != stripped:
            return None
        return token.value, token.scale, token.unit, token.currency, token.decimals
    return None


def _numeric_leaves(value: Any, prefix: str = "") -> list[tuple[str, Decimal]]:
    leaves: list[tuple[str, Decimal]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            leaves.extend(_numeric_leaves(item, path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            leaves.extend(_numeric_leaves(item, f"{prefix}[{index}]"))
    elif isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        leaves.append((prefix, value if isinstance(value, Decimal) else Decimal(str(value))))
    return leaves


def _value_period(value: Any) -> VerificationPeriod | None:
    if isinstance(value, datetime):
        return _day_period(value.date(), value.isoformat())
    if isinstance(value, date):
        return _day_period(value, value.isoformat())
    if isinstance(value, str):
        claims = _date_claims(value.strip())
        if claims and claims[0].start == 0:
            return claims[0].period
    return None


def _ratio_columns(columns: Sequence[Any]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, column in enumerate(columns):
        name = _column_name(column).lower()
        if name in ("numerator", "denominator", "value"):
            positions[name] = index
    return positions


def _ratio_denominator(
    *,
    entry: Mapping[str, Any],
    row: Sequence[Any],
    row_index: int,
    positions: Mapping[str, int],
    result_id: str,
    metric_currency: str | None,
) -> VerificationDenominator | None:
    if "denominator" not in positions:
        return None
    position = positions["denominator"]
    if position >= len(row):
        return None
    parts = _numeric_parts(row[position])
    value = parts[0] * parts[1] if parts is not None else None
    numerator_position = positions.get("numerator")
    numerator = None
    if numerator_position is not None and numerator_position < len(row):
        numerator_parts = _numeric_parts(row[numerator_position])
        if numerator_parts is not None:
            numerator = numerator_parts[0] * numerator_parts[1]
    label = "числитель/знаменатель"
    if numerator is not None:
        label = f"{numerator} / {value}"
    return VerificationDenominator(
        label=label,
        value=value,
        result_id=result_id,
        cell_address=f"data.rows[{row_index}].denominator",
    )


def _ratio_denominator_for_value(
    entry: Mapping[str, Any], *, result_id: str
) -> VerificationDenominator | None:
    data = entry.get("data")
    if not isinstance(data, Mapping):
        return None
    columns = data.get("columns") or []
    rows = data.get("rows") or []
    positions = _ratio_columns(columns)
    if "denominator" not in positions or not rows:
        return None
    return _ratio_denominator(
        entry=entry,
        row=rows[0],
        row_index=0,
        positions=positions,
        result_id=result_id,
        metric_currency=None,
    )


def _fact_segments(text: str) -> list[_FactSegment]:
    """Делит факт на фрагменты с одной подписью, не разрывая десятичные запятые."""
    if not text:
        return []
    segments: list[_FactSegment] = []
    start = 0
    for match in _FACT_SEPARATOR_RE.finditer(text):
        if match.start() > start:
            raw = text[start : match.start()]
            stripped = raw.strip()
            if stripped:
                offset = start + len(raw) - len(raw.lstrip())
                segments.append(_FactSegment(offset, offset + len(stripped), stripped))
        start = match.end()
    if start < len(text):
        raw = text[start:]
        stripped = raw.strip()
        if stripped:
            offset = start + len(raw) - len(raw.lstrip())
            segments.append(_FactSegment(offset, offset + len(stripped), stripped))
    return segments or [_FactSegment(0, len(text), text.strip())]


def _segment_at(segments: Sequence[_FactSegment], start: int, end: int) -> str | None:
    for segment in segments:
        if segment.start <= start and end <= segment.end:
            return segment.text
    return None


def _segment_has_value(text: str, value: Decimal) -> bool:
    return any(token.value * token.scale == value for token in _tokens(text))


def _fact_key_keys(key: str) -> set[str]:
    keys = _label_keys(key.replace("_", " "))
    if key.lower().startswith(("non_", "not_")):
        # non_null — это счётчик заполненных значений, а не пропусков.
        keys.discard("__missing__")
        keys.add("__count__")
    return keys


def _segment_for_value(
    segments: Sequence[_FactSegment], key: str, value: Decimal
) -> _FactSegment | None:
    """Находит фрагмент факта, который относится именно к ключу значения."""
    key_keys = _fact_key_keys(key)
    if key_keys:
        matching = [segment for segment in segments if key_keys & _label_keys(segment.text)]
        if len(matching) == 1:
            return matching[0]
        if matching:
            by_value = [segment for segment in matching if _segment_has_value(segment.text, value)]
            if len(by_value) == 1:
                return by_value[0]
            return None
    by_value = [segment for segment in segments if _segment_has_value(segment.text, value)]
    if len(by_value) == 1:
        return by_value[0]
    if len(segments) == 1:
        return segments[0]
    return None


def _fact_evidence(
    entry: Mapping[str, Any],
    *,
    result_id: str,
    metric: Any,
    metric_period: VerificationPeriod | None,
    metric_population: str | None,
    metric_currency: str | None,
    ratio_denominator: VerificationDenominator | None,
) -> list[VerificationEvidence]:
    evidence: list[VerificationEvidence] = []
    facts = entry.get("facts") or []
    for index, fact in enumerate(facts):
        if not isinstance(fact, Mapping):
            continue
        text = str(fact.get("text") or "")
        fact_id = f"{result_id}:fact:{index}"
        segments = _fact_segments(text)
        claims = _date_claims(text)
        claim_spans = [(claim.start, claim.end) for claim in claims]
        # Период внутри одного факта относится к его числам: «Выручка за 2023
        # год: 1000 RUB» связывает 1000 именно с 2023 годом.
        fact_period = metric_period or (claims[0].period if len(claims) == 1 else None)
        for claim in claims:
            evidence.append(
                _evidence(
                    result_id=result_id,
                    source="period",
                    value=None,
                    period=claim.period,
                    label=_segment_at(segments, claim.start, claim.end) or text or None,
                    fact_id=fact_id,
                )
            )
        fact_population = metric_population or _population(text)
        fact_currency = metric_currency or _currency_from_text(text)
        for token in _tokens(text):
            if any(token.start >= start and token.end <= end for start, end in claim_spans):
                continue
            unit = token.unit
            if unit == "unknown" and metric is not None:
                if getattr(metric, "unit", None) == "ratio":
                    unit = "ratio"
                elif getattr(metric, "unit", None) == "money":
                    unit = "money"
            currency = token.currency or fact_currency
            denominator = None
            if unit == "ratio" or (
                metric is not None
                and getattr(metric, "unit", None) == "ratio"
                and ratio_denominator is not None
            ):
                denominator = ratio_denominator
            evidence.append(
                _evidence(
                    result_id=result_id,
                    source="fact",
                    value=token.value,
                    scale=token.scale,
                    unit=unit,
                    currency=currency,
                    period=fact_period,
                    population=fact_population,
                    denominator=denominator,
                    label=_metric_value_caption(
                        _segment_at(segments, token.start, token.end), metric
                    )
                    if fact.get("kind") == "metric"
                    else _segment_at(segments, token.start, token.end),
                    fact_id=fact_id,
                )
            )
        values = fact.get("values") or {}
        if not isinstance(values, Mapping):
            continue
        for path, value in _numeric_leaves(values):
            if path.lower().endswith(("version", "metric_id")):
                continue
            key = path.rsplit(".", 1)[-1]
            metric_unit = getattr(metric, "unit", None) if metric is not None else None
            if key in ("numerator", "denominator") and metric_unit == "ratio":
                unit = "count"
            elif metric_unit == "ratio" and key == "value":
                unit = "ratio"
            elif metric_unit == "money" and key == "value":
                unit = "money"
            else:
                unit = _unit_from_name(key, "number")
            if (
                unit == "number"
                and fact_currency
                and key.lower() in ("value", "total", "amount", "sum", "revenue", "sales")
            ):
                unit = "money"
            denominator = None
            if unit == "ratio" and ratio_denominator is not None:
                denominator = ratio_denominator
            segment = _segment_for_value(segments, key, value)
            label = f"{path} {segment.text}".strip() if segment is not None else path
            if fact.get("kind") == "metric" and key == "value":
                label = _metric_value_caption(label, metric)
            evidence.append(
                _evidence(
                    result_id=result_id,
                    source="fact",
                    value=value,
                    unit=unit,
                    currency=fact_currency if unit == "money" else None,
                    period=fact_period,
                    population=fact_population,
                    denominator=denominator,
                    label=label or None,
                    fact_id=fact_id,
                )
            )
    return evidence


_SQL_AGGREGATE_LABELS: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.Count, "count rows количество строк"),
    (exp.Sum, "sum total сумма итог"),
    (exp.Avg, "average mean среднее"),
    (exp.Min, "min minimum минимум"),
    (exp.Max, "max maximum максимум"),
    (exp.Median, "median медиана"),
    (exp.Stddev, "std standard deviation стандартное отклонение"),
    (exp.StddevPop, "std standard deviation стандартное отклонение"),
    (exp.StddevSamp, "std standard deviation стандартное отклонение"),
    (exp.Variance, "variance дисперсия"),
    (exp.VariancePop, "variance дисперсия"),
)


def _sql_compatible_caption(caption: str, semantics: str) -> str:
    aggregate_keys = {
        "__average__",
        "__min__",
        "__max__",
        "__median__",
        "__std__",
        "__variance__",
        "__total__",
        "__count__",
        "__rows__",
        "__distinct__",
    }
    actual = _label_keys(semantics) & aggregate_keys
    return " ".join(
        word
        for word in caption.replace("_", " ").split()
        if (_label_keys(word) & aggregate_keys) <= actual
    )


def _sql_projection_label(projection: exp.Expression) -> str:
    """Смысл выходной колонки SQL не зависит от её произвольного алиаса."""
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    parts: list[str] = []
    for node_type, label in _SQL_AGGREGATE_LABELS:
        if isinstance(inner, node_type):
            if isinstance(inner, exp.Count):
                operand = inner.this
                while isinstance(operand, (exp.Paren, exp.Neg)):
                    operand = operand.this
                if isinstance(inner.this, exp.Distinct):
                    parts.append("count distinct уникальные значения")
                elif isinstance(operand, (exp.Star, exp.Literal, exp.Boolean)):
                    # COUNT(non-NULL literal), включая COUNT(1), считает все строки.
                    # NULL — отдельный узел AST и сюда не попадает.
                    parts.append(label)
                else:
                    # COUNT(column/expression) исключает NULL и не доказывает
                    # число строк исходной таблицы.
                    parts.append("count nonnull непустые значения")
            else:
                parts.append(label)
            break
    else:
        if not isinstance(inner, (exp.Column, exp.Cast, exp.TryCast, exp.Paren)):
            return ""
    column = inner if isinstance(inner, exp.Column) else inner.find(exp.Column)
    if column is not None:
        parts.append(
            _sql_compatible_caption(column.name, " ".join(parts)) if parts else column.name
        )
    return " ".join(dict.fromkeys(part for part in parts if part))


def _metric_value_caption(caption: str | None, metric: Any) -> str | None:
    """Смысл простой агрегатной метрики сохраняется и у факта, и у ячейки value."""
    formula = getattr(metric, "formula", None) if metric is not None else None
    if not formula:
        return caption
    try:
        expression = sqlglot.parse_one(formula, read="duckdb")
    except Exception:
        return caption
    if not any(isinstance(expression, node_type) for node_type, _ in _SQL_AGGREGATE_LABELS):
        return caption
    semantics = _sql_projection_label(expression)
    return f"{_sql_compatible_caption(caption or '', semantics)} {semantics}".strip()


def _counts_distinct_rows(select: exp.Select) -> bool:
    """COUNT(*) по подзапросу или CTE с DISTINCT/GROUP BY считает уникальные значения."""
    source = select.args.get("from_") or select.args.get("from")
    if source is None or select.args.get("joins"):
        return False
    table = source.this
    if isinstance(table, exp.Subquery):
        inner = table.this
    elif isinstance(table, exp.Table) and not table.db:
        with_ = select.args.get("with_") or select.args.get("with")
        cte = next(
            (item for item in (with_.expressions if with_ else []) if item.alias == table.name),
            None,
        )
        inner = cte.this if cte is not None else None
    else:
        return False
    return isinstance(inner, exp.Select) and bool(
        inner.args.get("distinct") or inner.args.get("group")
    )


def _inner_select(select: exp.Select) -> exp.Select | None:
    """Единственный источник SELECT: подзапрос или CTE."""
    source = select.args.get("from_") or select.args.get("from")
    if source is None or select.args.get("joins"):
        return None
    table = source.this
    if isinstance(table, exp.Subquery):
        return table.this if isinstance(table.this, exp.Select) else None
    with_ = select.args.get("with_") or select.args.get("with")
    if isinstance(table, exp.Table) and with_:
        for cte in with_.expressions:
            if cte.alias == table.name and isinstance(cte.this, exp.Select):
                return cte.this
    return None


def _with_source_markers(label: str, projection: exp.Expression, select: exp.Select) -> str:
    """Смысл колонки берётся из её выражения: через CTE, а не из алиаса или значений строки."""
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    owner = select
    # Внешний SELECT по CTE лишь переносит колонку: смотрим её исходное выражение.
    while isinstance(inner, exp.Column) and (nested := _inner_select(owner)) is not None:
        found = next(
            (item for item in nested.expressions if item.alias_or_name == inner.name), None
        )
        if found is None:
            break
        inner = found.this if isinstance(found, exp.Alias) else found
        owner = nested
    markers = []
    if isinstance(inner, exp.Count) and isinstance(inner.this, exp.Star | exp.Literal):
        if owner.args.get("where") or owner.args.get("group"):
            markers.append(_FILTERED_COUNT)
    # COUNT(amount_rub) считает строки: его результат не денежная сумма.
    columns = () if isinstance(inner, exp.Count) else inner.find_all(exp.Column)
    if any(re.search(r"(?:^|_)(?:rub|руб)$", column.name, re.I) for column in columns):
        markers.append(_RUB_SOURCE)
    return " ".join([label, *markers]).strip()


def _sql_column_semantics(result: ToolResult | None) -> dict[str, str]:
    """Сопоставляет выходные колонки run_sql с их агрегатной семантикой."""
    if result is None or result.tool != "run_sql":
        return {}
    sql = result.sql or ""
    if not sql:
        params = result.params if isinstance(result.params, Mapping) else {}
        sql = str(params.get("sql") or "")
    if not sql.strip():
        return {}
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return {}
    select = parsed if isinstance(parsed, exp.Select) else parsed.find(exp.Select)
    if select is None:
        return {}
    semantics: dict[str, str] = {}
    for projection in select.expressions:
        alias = projection.alias_or_name
        if not alias:
            continue
        label = _sql_projection_label(projection)
        label = _with_source_markers(label, projection, select)
        if label:
            if "__rows__" in _label_keys(label) and _counts_distinct_rows(select):
                label = f"{label} count distinct уникальные значения"
            semantics[alias.lower()] = label
    return semantics


def _data_evidence(
    entry: Mapping[str, Any],
    *,
    result: ToolResult | None,
    result_id: str,
    metric: Any,
    metric_period: VerificationPeriod | None,
    metric_population: str | None,
    metric_currency: str | None,
) -> list[VerificationEvidence]:
    data = entry.get("data")
    if not isinstance(data, Mapping):
        return []
    columns = data.get("columns") or []
    rows = data.get("rows") or []
    row_count = data.get("row_count")
    evidence: list[VerificationEvidence] = []
    positions = _ratio_columns(columns)
    metric_unit = getattr(metric, "unit", None) if metric is not None else None
    metric_title = getattr(metric, "title", None) if metric is not None else None
    sql_labels = _sql_column_semantics(result) if metric is None else {}
    bucket_grain = _result_period_grain(result)
    for row_index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
            continue
        row_dates = [
            _bucket_period(period, bucket_grain) if period is not None else None
            for period in (_value_period(value) for value in row)
        ]
        periods = [period for period in row_dates if period is not None]
        # Дата строки точнее периода всей метрики: строка временного ряда
        # относится к своему бакету, а не ко всему запрошенному диапазону.
        row_period = (periods[0] if periods else None) or metric_period
        row_label = " ".join(
            str(value)
            for value in row
            if value is not None
            and not isinstance(value, bool)
            and not isinstance(value, (int, float, Decimal))
            and _value_period(value) is None
        )
        for column_index, value in enumerate(row):
            if column_index >= len(columns):
                column = ""
            else:
                column = columns[column_index]
            name = _column_name(column)
            label = _column_label(column)
            sql_label = sql_labels.get(name.lower(), "")
            if sql_label:
                label = f"{_sql_compatible_caption(label or name, sql_label)} {sql_label}".strip()
            column_type = (_column_type(column) or "").lower()
            period = row_period
            value_period = row_dates[column_index] if column_index < len(row_dates) else None
            if value_period is not None:
                evidence.append(
                    _evidence(
                        result_id=result_id,
                        source="period",
                        value=None,
                        period=value_period,
                        label=f"{label} {row_label}".strip() or None,
                        cell_address=f"data.rows[{row_index}].{name or column_index}",
                    )
                )
                continue
            parts = _numeric_parts(value)
            if parts is None:
                continue
            number, scale, unit, currency, _ = parts
            if metric_unit == "ratio" and name == "value":
                unit = "ratio"
            elif metric_unit == "ratio" and name in ("numerator", "denominator"):
                unit = "count"
            elif metric_unit == "money" and name == "value":
                unit = "money"
            elif unit == "unknown":
                unit = _unit_from_name(name, "unknown")
            if unit == "unknown" and column_type in ("money", "currency"):
                unit = "money"
            if unit in ("unknown", "number"):
                inferred = _unit_from_name(name, "number")
                if inferred != "number":
                    unit = inferred
            if unit == "unknown" and column_type in ("number", "integer", "decimal", "float"):
                unit = "count" if name in ("row_count", "count") else "number"
            currency = currency or metric_currency
            if currency is None:
                currency = _currency_from_text(f"{name} {label}")
            denominator = None
            if metric_unit == "ratio" and name == "value":
                denominator = _ratio_denominator(
                    entry=entry,
                    row=row,
                    row_index=row_index,
                    positions=positions,
                    result_id=result_id,
                    metric_currency=metric_currency,
                )
            evidence.append(
                _evidence(
                    result_id=result_id,
                    source="data_cell",
                    value=number,
                    scale=scale,
                    unit=unit,
                    currency=currency if unit == "money" else None,
                    period=period,
                    population=metric_population,
                    denominator=denominator,
                    label=(
                        " ".join(
                            part
                            for part in (
                                (
                                    _metric_value_caption(metric_title, metric)
                                    if metric is not None and name == "value"
                                    else metric_title
                                )
                                or label
                                or name,
                                row_label,
                            )
                            if part
                        )
                        or None
                    ),
                    cell_address=f"data.rows[{row_index}].{name or column_index}",
                )
            )
    if isinstance(row_count, int) and not isinstance(row_count, bool):
        evidence.append(
            _evidence(
                result_id=result_id,
                source="row_count",
                value=Decimal(row_count),
                unit="count",
                period=metric_period,
                population=metric_population,
                label="row_count строк rows",
                cell_address="data.row_count",
            )
        )
    elif rows and not data.get("truncated"):
        evidence.append(
            _evidence(
                result_id=result_id,
                source="row_count",
                value=Decimal(len(rows)),
                unit="count",
                period=metric_period,
                population=metric_population,
                label="rows строк",
                cell_address="data.rows",
            )
        )
    return evidence


def _evidence_key(item: VerificationEvidence) -> tuple[Any, ...]:
    return (
        item.result_id,
        item.source,
        item.fact_id,
        item.cell_address,
        item.value,
        item.scale,
        item.unit,
        item.currency,
        _normalize_period(item.period),
        _normalize_population(item.population),
        item.denominator.value if item.denominator else None,
        item.label,
    )


def _with_total_currency(evidence: list[VerificationEvidence]) -> list[VerificationEvidence]:
    """Итог колонки наследует рубли от исходной колонки её ячеек."""
    rub_columns = {
        (item.result_id, item.cell_address.rsplit(".", 1)[-1])
        for item in evidence
        if item.cell_address and _RUB_SOURCE in (item.label or "")
    }
    updated = []
    for item in evidence:
        match = re.search(r"column_totals\.(\S+)", item.label or "")
        if match and (item.result_id, match.group(1)) in rub_columns:
            item = item.model_copy(update={"label": f"{item.label} {_RUB_SOURCE}"})
        updated.append(item)
    return updated


def build_evidence(
    results: Sequence[ToolResult], payload: Sequence[Mapping[str, Any]]
) -> list[VerificationEvidence]:
    """Собирает допустимое происхождение из того, что модель действительно видела.

    ``payload`` — результат ``_answer_payload``: очищенные факты и разрешённые
    строки. Сырые строки сюда не передаются, иначе проверка подтверждала бы числа,
    которых не было в контексте ответа.
    """
    by_id = {result.result_id: result for result in results}
    evidence: list[VerificationEvidence] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        result_id = str(entry.get("result_id") or "")
        if not result_id:
            continue
        result = by_id.get(result_id)
        metric = result.metric if result is not None else None
        metric_period = _metric_period(metric)
        metric_population = _metric_population(metric)
        metric_currency = getattr(metric, "currency", None) if metric is not None else None
        ratio_denominator = _ratio_denominator_for_value(entry, result_id=result_id)
        evidence.extend(
            _fact_evidence(
                entry,
                result_id=result_id,
                metric=metric,
                metric_period=metric_period,
                metric_population=metric_population,
                metric_currency=metric_currency,
                ratio_denominator=ratio_denominator,
            )
        )
        evidence.extend(
            _data_evidence(
                entry,
                result=result,
                result_id=result_id,
                metric=metric,
                metric_period=metric_period,
                metric_population=metric_population,
                metric_currency=metric_currency,
            )
        )
    unique: dict[tuple[Any, ...], VerificationEvidence] = {}
    for item in evidence:
        unique.setdefault(_evidence_key(item), item)
    return _with_total_currency(list(unique.values()))


# --- Разбор утверждений ---

_CONSTANT_PREFIX_RE = re.compile(
    r"(?<!\w)(?:№|No\.?|п\.|пункт|шаг|этап|рис\.?|figure|table)\s*(?P<num>\d{1,4})(?!\d)",
    re.IGNORECASE,
)
_DENOMINATOR_AFTER_RE = re.compile(
    r"^\s*(?:%|процент[а-яё]*|п\.п\.)?\s*(?:от|среди)\s+(?P<body>[^,.;:()\n]{2,60})",
    re.IGNORECASE,
)
_DENOMINATOR_BEFORE_RE = re.compile(
    r"(?:доля|процент[а-яё]*|share|ratio)\s+(?:от|среди)\s+"
    r"(?P<body>[^,.;:()\n]{2,60}?)\s*(?:—|-|:|=|составляет|равна|равен)?\s*$",
    re.IGNORECASE,
)


def _suppressed_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    for match in _FENCE_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _NAME_NUMBER_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _CODE_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _URL_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _TIME_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _ORDINAL_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _VERSION_RE.finditer(text):
        spans.append((match.start(), match.end(), "constant"))
    for match in _CONSTANT_PREFIX_RE.finditer(text):
        spans.append((match.start("num"), match.end("num"), "constant"))
    return spans


def _formula_spans(text: str, tokens: Sequence[_NumberToken]) -> list[tuple[int, int]]:
    return [(spec.start, spec.end) for spec in _formula_specs(text, tokens)]


def _operator(gap: str) -> str | None:
    if gap == "+":
        return "+"
    if gap in ("-", "−"):
        return "-"
    if gap in ("*", "×"):
        return "*"
    if gap in ("/", "÷", ":"):
        return "/"
    return None


def _formula_specs(text: str, tokens: Sequence[_NumberToken]) -> list[_FormulaSpec]:
    specs: list[_FormulaSpec] = []
    date_spans = [(claim.start, claim.end) for claim in _date_claims(text)]

    def is_date(token: _NumberToken) -> bool:
        return any(token.start >= start and token.end <= end for start, end in date_spans)

    for equal in re.finditer(r"(?<![=<>!])(?:=|≈|~)(?!=)", text):
        left, right = _clause_bounds(text, equal.start())
        right_tokens = [
            token for token in tokens if equal.end() <= token.start < right and not is_date(token)
        ]
        left_tokens = [
            token
            for token in tokens
            if left <= token.start and token.end <= equal.start() and not is_date(token)
        ]
        if len(right_tokens) != 1 or len(left_tokens) < 2:
            continue
        result = right_tokens[0]
        # A caption can contain a separately checked calendar count before the
        # formula: ``February (28 days): 1732 / (28 * 40) = 1.55``.
        # Leave that prefix outside the formula instead of consuming its number.
        for first in range(max(0, len(left_tokens) - 32), len(left_tokens) - 1):
            operands = left_tokens[first:]
            expression_start = operands[0].start
            while expression_start > left and text[expression_start - 1] in "( \t":
                expression_start -= 1
            tree = _formula_tree(text, operands, expression_start, equal.start())
            if tree is not None:
                left_tokens = operands
                break
        else:
            continue
        expression = text[expression_start : equal.start()].strip()
        spec = _FormulaSpec(
            left=tuple(left_tokens),
            result=result,
            expression=expression,
            raw=text[expression_start : result.end].strip(),
            start=expression_start,
            end=result.end,
            percent_result=result.unit == "percent",
            tree=tree,
        )
        specs.append(spec)
    chosen: list[_FormulaSpec] = []
    for spec in sorted(specs, key=lambda item: (item.start, -(item.end - item.start))):
        if any(not (spec.end <= other.start or spec.start >= other.end) for other in chosen):
            continue
        chosen.append(spec)
    return chosen


def _formula_tree(
    text: str, tokens: Sequence[_NumberToken], start: int, end: int
) -> ast.Expression | None:
    """Parse only bounded arithmetic over the already extracted operands."""
    parts = []
    position = start
    for index, token in enumerate(tokens):
        gap = text[position : token.start]
        if not re.fullmatch(r"[\s()+*/×÷:−-]*", gap):
            return None
        parts.extend((gap, f"n{index}"))
        position = token.end
    tail = text[position:end]
    if not re.fullmatch(r"[\s)]*", tail):
        return None
    parts.append(tail)
    expression = "".join(parts).translate(str.maketrans({"×": "*", "÷": "/", ":": "/", "−": "-"}))
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, RecursionError):
        return None
    allowed = (ast.Expression, ast.BinOp, ast.Name, ast.Load, ast.Add, ast.Sub, ast.Mult, ast.Div)
    if any(not isinstance(node, allowed) for node in ast.walk(tree)):
        return None
    if sum(1 for _ in ast.walk(tree)) > 128:
        return None
    return tree


def _claim_label(
    text: str,
    token: _NumberToken,
    *,
    suppress: Sequence[tuple[int, int]],
) -> str | None:
    clause_start, clause_end = _clause_bounds(text, token.start)
    clause = text[clause_start:clause_end]
    offset = token.start - clause_start
    segment_start = 0
    segment_end = len(clause)
    for match in re.finditer(r"[;,\n]|(?<=\s)(?:и|and)(?=\s)", clause, re.IGNORECASE):
        if match.end() <= offset:
            segment_start = match.end()
        elif match.start() >= token.end - clause_start:
            segment_end = match.start()
            break
    absolute_start = clause_start + segment_start
    absolute_end = clause_start + segment_end
    chars = list(text[absolute_start:absolute_end])
    all_spans = [
        *((item.start, item.end) for item in _tokens(text)),
        *suppress,
    ]
    for start, end in all_spans:
        for index in range(max(0, start - absolute_start), min(len(chars), end - absolute_start)):
            chars[index] = " "
    cleaned = re.sub(r"[=+×÷*/:—–-]+", " ", "".join(chars))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:!?()[]«»\"'")
    return cleaned or None


def _claim_denominator(text: str, token: _NumberToken) -> VerificationDenominator | None:
    start, end = _clause_bounds(text, token.start)
    after = text[token.end : end]
    match = _DENOMINATOR_AFTER_RE.match(after)
    if match:
        body = match.group("body").strip(" .,;:-—")
        if body:
            return VerificationDenominator(label=body)
    prefix = text[start : token.start]
    match = _DENOMINATOR_BEFORE_RE.search(prefix)
    if match:
        body = match.group("body").strip(" .,;:-—")
        if body:
            return VerificationDenominator(label=body)
    return None


def _draft_from_token(
    text: str,
    token: _NumberToken,
    claims: Sequence[_DateClaim],
    suppress: Sequence[tuple[int, int]],
) -> _Draft:
    period = _period_for(text, token.start, claims)
    clause = _clause(text, token.start)
    population = _population(clause)
    denominator = _claim_denominator(text, token) if token.unit in ("percent", "ratio") else None
    return _Draft(
        kind="quantity",
        text=clause.strip(),
        raw_text=token.raw,
        value=token.value,
        scale=token.scale,
        unit=token.unit,
        currency=token.currency,
        period=period,
        population=population,
        denominator=denominator,
        label=_claim_label(text, token, suppress=suppress),
    )


def _year_value(value: Decimal) -> int | None:
    if value == value.to_integral_value() and 1900 <= int(value) <= 2100:
        return int(value)
    return None


def _extract_drafts(text: str) -> list[_Draft]:
    tokens = _tokens(text)
    date_claims = _date_claims(text)
    date_spans = [(claim.start, claim.end) for claim in date_claims]
    formula_specs = _formula_specs(text, tokens)
    formula_spans = [(spec.start, spec.end) for spec in formula_specs]
    list_spans: list[tuple[int, int]] = []
    drafts: list[_Draft] = []
    for match in _LIST_MARKER_RE.finditer(text):
        if any(not (match.end() <= start or match.start() >= end) for start, end in date_spans):
            continue
        number = match.group("num")
        list_spans.append((match.start("num"), match.end("num")))
        drafts.append(
            _Draft(
                kind="list_number",
                text=match.group(0).strip(),
                raw_text=number,
                value=Decimal(number),
                unit="number",
                status="ignored",
                reason_code="non_analytical",
                reason=_REASON_TEXT["non_analytical"],
            )
        )
    suppress = [
        *date_spans,
        *formula_spans,
        *list_spans,
        *((start, end) for start, end, _ in _suppressed_spans(text)),
    ]
    for spec in formula_specs:
        drafts.append(_formula_draft(text, spec))
    for token in tokens:
        if any(token.start >= start and token.end <= end for start, end in date_spans):
            continue
        if any(token.start >= start and token.end <= end for start, end in formula_spans):
            continue
        if any(token.start >= start and token.end <= end for start, end in list_spans):
            continue
        constant = next(
            (
                (start, end)
                for start, end, _ in _suppressed_spans(text)
                if token.start >= start and token.end <= end
            ),
            None,
        )
        if constant is not None:
            drafts.append(
                _Draft(
                    kind="constant",
                    text=_clause(text, token.start).strip(),
                    raw_text=token.raw,
                    value=token.value,
                    scale=token.scale,
                    unit=token.unit,
                    currency=token.currency,
                    status="ignored",
                    reason_code="non_analytical",
                    reason=_REASON_TEXT["non_analytical"],
                )
            )
            continue
        drafts.append(_draft_from_token(text, token, date_claims, suppress))
    for claim in date_claims:
        kind = "date" if claim.period.grain == "day" else "period"
        drafts.append(
            _Draft(
                kind=kind,
                text=_clause(text, claim.start).strip(),
                raw_text=claim.raw,
                value=None,
                period=claim.period,
                status="verified",
            )
        )
    drafts.sort(key=lambda item: _draft_order(text, item))
    return drafts


def _draft_order(text: str, draft: _Draft) -> tuple[int, str]:
    position = text.find(draft.raw_text)
    return (position if position >= 0 else len(text), draft.raw_text)


def _formula_draft(text: str, spec: _FormulaSpec) -> _Draft:
    clause_start, clause_end = _clause_bounds(text, spec.start)
    claims = [
        claim
        for claim in _date_claims(text)
        if clause_start <= claim.start and claim.end <= clause_end
    ]
    # Период формулы известен, только если в её предложении назван ровно один
    # период. Несколько дат описывают сравнение и не задают период результата.
    period = claims[0].period if len(claims) == 1 else None
    return _Draft(
        kind="formula",
        text=_clause(text, spec.start).strip(),
        raw_text=spec.raw,
        value=spec.result.value,
        scale=spec.result.scale,
        unit=spec.result.unit,
        currency=spec.result.currency,
        period=period,
        formula=spec.expression,
        status="verified",
    )


# --- Сопоставление утверждений с происхождением ---

_SCALAR_UNITS = {"number", "count", "percent", "ratio", "unknown"}


def _canonical(value: Decimal | None, scale: Decimal) -> Decimal | None:
    return value * scale if value is not None else None


def _values_equal(left: Decimal, right: Decimal, decimals: int = 0) -> bool:
    tolerance = Decimal(1).scaleb(-max(0, decimals)) / 2
    return abs(left - right) <= tolerance + abs(right) * Decimal("1e-9")


def _value_matches(draft: _Draft, evidence: VerificationEvidence) -> bool:
    claimed = _canonical(draft.value, draft.scale)
    actual = _canonical(evidence.value, evidence.scale)
    if claimed is None or actual is None:
        return False
    if draft.unit == "percent":
        if evidence.unit == "percent":
            return _values_equal(claimed, actual, _draft_decimals(draft))
        if evidence.unit == "ratio":
            return _values_equal(claimed / 100, actual, _draft_decimals(draft) + 2)
        return False
    if draft.unit == "ratio" and evidence.unit == "percent":
        return _values_equal(claimed * 100, actual, _draft_decimals(draft))
    if draft.unit == "money" or evidence.unit == "money":
        rub_column = draft.currency == "RUB" and _RUB_SOURCE in (evidence.label or "")
        if draft.unit not in ("money", "unknown") or (
            evidence.unit not in ("money", "unknown") and not rub_column
        ):
            return False
    if draft.unit == "count" and evidence.unit not in ("count", "number", "unknown"):
        return False
    if draft.unit == "number" and evidence.unit not in ("count", "number", "unknown"):
        return False
    return _values_equal(claimed, actual, _draft_decimals(draft))


def _draft_decimals(draft: _Draft) -> int:
    value = draft.value
    if value is None:
        return 0
    exponent = value.as_tuple().exponent
    return max(0, -exponent)


def _unit_reason(draft: _Draft, evidence: Sequence[VerificationEvidence]) -> str | None:
    claimed = _canonical(draft.value, draft.scale)
    if claimed is None:
        return None
    loose: list[VerificationEvidence] = []
    for item in evidence:
        actual = _canonical(item.value, item.scale)
        if actual is None:
            continue
        if (
            _values_equal(claimed, actual, _draft_decimals(draft))
            or _values_equal(claimed / 100, actual, _draft_decimals(draft) + 2)
            or _values_equal(claimed * 100, actual, _draft_decimals(draft))
        ):
            loose.append(item)
    if not loose:
        return None
    if draft.label and _label_keys(draft.label):
        labelled = [item for item in loose if item.label]
        if labelled and not any(_same_label(draft.label, item.label) for item in labelled):
            return "unrelated_result"
    for item in loose:
        if draft.currency and draft.currency != item.currency:
            return "currency_mismatch"
        if draft.unit == "money" and item.unit != "money":
            return "unit_mismatch"
        if draft.unit == "percent" and item.unit not in ("percent", "ratio"):
            return "unit_mismatch"
        if draft.unit == "count" and item.unit == "money":
            return "unit_mismatch"
    return None


_DENOMINATOR_GENERIC_KEYS = {"все", "всех", "всем", "весь", "all", "each", "кажд"}


def _denominator_label_matches(claim: str, actual: str) -> bool:
    claim_keys = _label_keys(claim) - _DENOMINATOR_GENERIC_KEYS
    actual_keys = _label_keys(actual)
    if not claim_keys:
        return True
    if not actual_keys:
        return False
    claim_groups = {key for key in claim_keys if key.startswith("__")}
    if claim_groups:
        actual_groups = {key for key in actual_keys if key.startswith("__")}
        return claim_groups <= actual_groups
    return claim_keys <= actual_keys


def _denominator_reason(draft: _Draft, evidence: VerificationEvidence) -> str | None:
    if draft.denominator is None:
        return None
    actual = evidence.denominator
    if actual is None:
        return "denominator_mismatch"
    if actual.value == 0:
        return "zero_denominator"
    if draft.denominator.label:
        actual_keys = _label_keys(actual.label)
        if actual_keys:
            if not _denominator_label_matches(draft.denominator.label, actual.label):
                return "denominator_mismatch"
        elif evidence.population and not _denominator_label_matches(
            draft.denominator.label, evidence.population
        ):
            return "denominator_mismatch"
    return None


# Порог, прямо стоящий перед числом: «не менее чем 100», «порог 100».
_THRESHOLD_BEFORE_RE = re.compile(
    r"(?<!\w)(?:(?:не\s+)?(?:менее|более|меньше|больше)(?:\s+чем)?|порог\w*|минимум)\s*$",
    re.IGNORECASE,
)
_DAY_BEFORE_MONTH_RE = re.compile(
    r"^\s*(?:январ|феврал|март|апрел|ма[яйе]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)
_DAYS_AFTER_RE = re.compile(r"^\s*(?:дн[яе]\w*|день)(?!\w)", re.IGNORECASE)
_PERCENT_FACTOR_BEFORE_RE = re.compile(r"[*×·]\s*$")
_POPULATION_SKIP = {"среди", "без", "из", "кроме", "не"}
_GENERIC_MEASURE_COLUMNS = {"value", "cnt", "count", "n", "total", "yhat"}
_RUB_SOURCE = "source_rub"
_FILTERED_COUNT = "filtered_count"


@dataclass(frozen=True)
class _ResultConditions:
    """Условия одного запроса: чужой фильтр или делитель этот результат не подтверждают."""

    start: date | None = None
    end: date | None = None
    divisors: frozenset[Decimal] = frozenset()
    cutoffs: frozenset[tuple[str, str, date]] = frozenset()
    percent_scaling: bool = False

    def covers(self, period: VerificationPeriod) -> bool:
        first, last = _date(period.start), _date(period.end)
        if not (self.start and self.end and first and last):
            return False
        return self.start <= first and last <= self.end


@dataclass(frozen=True)
class _QueryContext:
    """Что написал пользователь и чем SQL ограничил свой результат.

    Контекст подтверждает только условия расчёта: порог и период. Значения
    результата он подтвердить не может, а переформулировка модели в него не входит.
    """

    thresholds: frozenset[Decimal] = frozenset()
    question: str = ""
    results: Mapping[str, _ResultConditions] = field(default_factory=dict)
    active: bool = False

    def names_population(self, population: str | None) -> bool:
        # Граница популяции в тексте размыта: сверяем её предлог и первое слово.
        words = _words(population or "")
        if not words:
            return False
        if words[0] in _POPULATION_SKIP and words[0] not in _words(self.question):
            return False
        named = next((word for word in words if word not in _POPULATION_SKIP), None)
        keys = _label_keys(named)
        return bool(keys) and keys <= _label_keys(self.question)

    def covers(
        self, period: VerificationPeriod | None, result_ids: Iterable[str] | None = None
    ) -> bool:
        if period is None:
            return False
        ids = self.results if result_ids is None else result_ids
        return any(
            conditions.covers(period)
            for result_id in ids
            if (conditions := self.results.get(result_id)) is not None
        )

    def divisors(self, result_ids: Iterable[str] | None = None) -> set[Decimal]:
        ids = self.results if result_ids is None else result_ids
        found: set[Decimal] = set()
        for result_id in ids:
            if (conditions := self.results.get(result_id)) is not None:
                found |= conditions.divisors
        return found

    def has_dates(self) -> bool:
        return any(conditions.start is not None for conditions in self.results.values())

    def as_of(self, draft: _Draft, result_ids: Iterable[str] | None = None) -> bool:
        requested = _as_of_day(self.question)
        if requested is None:
            return False
        cutoff = requested + timedelta(days=1)
        ids = self.results if result_ids is None else result_ids
        conditions = [self.results[result_id] for result_id in ids if result_id in self.results]
        if not any(
            any(operator == "lt" and value == cutoff for _, operator, value in condition.cutoffs)
            for condition in conditions
        ):
            return False
        stated = _as_of_day(draft.text)
        if stated is not None:
            return stated == requested
        # A separate explanation can repeat the exclusive SQL boundary, but
        # cannot turn that boundary into an analytical measurement period.
        if draft.raw_text != cutoff.isoformat():
            return False
        captions = re.findall(
            rf"(ранее|позже|до|после)\s+{re.escape(draft.raw_text)}(?!\d)",
            draft.text,
            re.I,
        )
        required = {"lt" if caption.lower() in ("ранее", "до") else "gte" for caption in captions}
        return bool(required) and any(
            required <= {operator for _, operator, value in condition.cutoffs if value == cutoff}
            for condition in conditions
        )


def _direct_divisors(expression: exp.Expression) -> set[Decimal]:
    """Литералы, на которые действительно делят: `x / 28.0`, `x / (28.0 * n)`, но не `(2 + 5)`."""
    while isinstance(expression, exp.Paren):
        expression = expression.this
    if isinstance(expression, exp.Mul):
        return _direct_divisors(expression.left) | _direct_divisors(expression.right)
    if isinstance(expression, exp.Literal) and not expression.is_string:
        with suppress(InvalidOperation):
            return {Decimal(expression.this)}
    return set()


def _sql_conditions(sql: str) -> _ResultConditions:
    """Даты из сравнений условий и делители расчёта: комментарии и LIMIT сюда не входят."""
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return _ResultConditions()
    bounds: list[date] = []
    cutoffs: set[tuple[str, str, date]] = set()
    for scope in parsed.find_all(exp.Where, exp.Having, exp.If):
        for comparison in scope.find_all(exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between):
            if isinstance(comparison, (exp.LT, exp.GTE)) and isinstance(
                comparison.this, exp.Column
            ):
                literal = comparison.expression
                if (
                    isinstance(literal, exp.Literal)
                    and literal.is_string
                    and re.fullmatch(r"\d{4}-\d{2}-\d{2}", literal.this)
                    and (cutoff := _date(literal.this))
                ):
                    cutoffs.add((comparison.this.name.lower(), comparison.key, cutoff))
            for literal in comparison.find_all(exp.Literal):
                if literal.is_string and (parsed_date := _date(literal.this[:10])):
                    bounds.append(parsed_date)
    divisors: set[Decimal] = set()
    for division in parsed.find_all(exp.Div):
        divisors |= _direct_divisors(division.expression)
    return _ResultConditions(
        min(bounds, default=None),
        max(bounds, default=None),
        frozenset(divisors),
        frozenset(cutoffs),
        any(
            isinstance(product.expression, exp.Literal)
            and not product.expression.is_string
            and product.expression.this in ("100", "100.0")
            and isinstance(product.this.unnest(), (exp.Div, exp.Sub))
            for product in parsed.find_all(exp.Mul)
        ),
    )


def _query_context(question: str | None, sql: Mapping[str, str] | None = None) -> _QueryContext:
    question_text = question or ""
    spans = [(claim.start, claim.end) for claim in _date_claims(question_text)]
    thresholds: set[Decimal] = set()
    for token in _tokens(question_text):
        inside_date = any(token.start >= start and token.end <= end for start, end in spans)
        if not inside_date and _year_value(token.value) is None:
            thresholds.add(token.value * token.scale)
    return _QueryContext(
        frozenset(thresholds),
        question_text,
        {result_id: _sql_conditions(text) for result_id, text in (sql or {}).items()},
        active=True,
    )


def _label_only_rejection(
    draft: _Draft, evidence: Sequence[VerificationEvidence], context: _QueryContext
) -> bool:
    """Спорна только подпись: без неё проходят валюта, период, популяция и знаменатель."""
    if draft.status != "rejected" or draft.kind != "quantity":
        return False
    if draft.reason_code not in ("unrelated_result", "ambiguous_number"):
        return False
    requested = _aggregate_keys(draft.label)
    matching = [item for item in evidence if _value_matches(draft, item)]
    # Подпись называет агрегат: подтвердить её может только значение этого агрегата.
    if requested and not any(requested & _aggregate_keys(item.label) for item in matching):
        return False
    _, reason = _match_quantity(replace(draft, label=None), matching, context)
    return reason in (None, "ambiguous_number")


_MONTH_DAYS = (
    ("январ", 31),
    ("феврал", 28),
    ("март", 31),
    ("апрел", 30),
    ("ма[яйе]", 31),
    ("июн", 30),
    ("июл", 31),
    ("август", 31),
    ("сентябр", 30),
    ("октябр", 31),
    ("ноябр", 30),
    ("декабр", 31),
)


def _as_of_day(text: str) -> date | None:
    for month, (pattern, _) in enumerate(_MONTH_DAYS, 1):
        match = re.search(rf"на\s+конец\s+(\d{{1,2}})\s+{pattern}\w*\s+(\d{{4}})", text, re.I)
        if match:
            with suppress(ValueError):
                return date(int(match[2]), month, int(match[1]))
    return None


def is_calendar_days(text: str, start: int, end: int, value: Decimal) -> bool:
    """«в феврале 2025 (28 дней)»: длина названного месяца, а не измеренная длительность."""
    if not _DAYS_AFTER_RE.match(text[end:]) or re.search(
        r"длительн|средн|срок|duration", text[max(0, start - 60) : start], re.I
    ):
        return False
    # Each calendar caption belongs to its own clause. In particular, February
    # must not capture the following March caption after a semicolon.
    left = max(text.rfind(";", 0, start), text.rfind("\n", 0, start)) + 1
    right = min(
        (position for separator in (";", "\n") if (position := text.find(separator, end)) >= 0),
        default=len(text),
    )
    window_start = max(left, start - 40)
    window = text[window_start:start] + " " + text[end : min(right, end + 25)]
    months = []
    for pattern, days in _MONTH_DAYS:
        for match in re.finditer(rf"(?<!\w){pattern}", window, re.I):
            distance = min(
                abs(match.start() - (start - window_start)),
                abs(match.end() - (start - window_start)),
            )
            months.append((distance, days))
    if months:
        days = min(months)[1]
        return value == days or (days == 28 and value == 29)
    return False


def _is_query_condition(draft: _Draft, context: _QueryContext) -> bool:
    """Число ответа пересказывает условие вопроса, а не результат расчёта."""
    if draft.value is None or draft.currency:
        return False
    start = draft.text.find(draft.raw_text)
    if start < 0:
        return False
    before, after = draft.text[:start], draft.text[start + len(draft.raw_text) :]
    value = _canonical(draft.value, draft.scale)
    # «(28 дней)» и «… * 100 =»: постоянные расчёта, а не его результат.
    if value is not None and is_calendar_days(
        draft.text, start, start + len(draft.raw_text), value
    ):
        return True
    if draft.unit not in ("unknown", "number"):
        return False
    if value in context.thresholds and _THRESHOLD_BEFORE_RE.search(before):
        return True
    if value == 100 and _PERCENT_FACTOR_BEFORE_RE.search(before):
        return True
    if (
        value == 100
        and re.search(r"умноженн\w*\s+на\s*$", before, re.I)
        and (
            re.search(r"(?:рост|изменени\w*|дол\w*)\s+в\s+процент", draft.text, re.I)
            or re.search(r"(?:рост|изменени\w*).*разниц\w*.*дел[её]нн\w*\s+на", draft.text, re.I)
        )
        and any(result.percent_scaling for result in context.results.values())
    ):
        return True
    # «31 марта 2025»: день даты, а период проверяется отдельным утверждением.
    return (
        draft.value == draft.value.to_integral_value()
        and 1 <= draft.value <= 31
        and context.has_dates()
        and bool(_DAY_BEFORE_MONTH_RE.match(after))
    )


def _match_quantity(
    draft: _Draft,
    evidence: Sequence[VerificationEvidence],
    context: _QueryContext | None = None,
) -> tuple[list[VerificationEvidence], str | None]:
    context = context or _QueryContext()
    candidates = [item for item in evidence if _value_matches(draft, item)]
    if not candidates:
        reason = _unit_reason(draft, evidence)
        if reason:
            return [], reason
        if draft.denominator is not None:
            return [], "denominator_mismatch"
        return [], "number_not_found"

    # Equal COUNT(*) and COUNT(DISTINCT ...) values are different evidence.
    # A uniqueness claim must select the latter, even when a generic caption
    # such as "total" also matches the catalog's row count.
    if "__distinct__" in _label_keys(draft.label):
        candidates = [item for item in candidates if "__distinct__" in _label_keys(item.label)]
        if not candidates:
            return [], "unrelated_result"

    if draft.label and _label_keys(draft.label):
        labelled = [item for item in candidates if _same_label(draft.label, item.label)]
        if labelled:
            candidates = labelled
        elif context.active and _exact_fractional_cell(draft, candidates):
            # Имя колонки модели (p90_hours) не обязано совпасть с подписью в ответе.
            # Дробное значение ячейки одного результата случайно не совпадает.
            candidates = _exact_fractional_cell(draft, candidates)
        elif context.active and _filtered_count_cell(draft, candidates):
            candidates = _filtered_count_cell(draft, candidates)
        elif any(_same_label(draft.label, item.label) for item in evidence):
            return [], "unrelated_result"
        elif len({item.result_id for item in candidates}) > 1:
            return [], "ambiguous_number"
        elif any(not item.label for item in candidates):
            pass
        else:
            return [], "unrelated_result"

    if draft.currency:
        with_currency = [
            item
            for item in candidates
            if item.currency == draft.currency
            or (
                item.currency is None
                and draft.currency == "RUB"
                and _RUB_SOURCE in (item.label or "")
            )
        ]
        if not with_currency:
            return [], "currency_mismatch"
        candidates = with_currency

    if draft.population:
        compatible = [
            item for item in candidates if _population_compatible(draft.population, item.population)
        ]
        if compatible:
            candidates = compatible
        elif not context.names_population(draft.population) or any(
            item.population for item in candidates
        ):
            return [], "population_mismatch"

    if draft.period:
        covered = [
            item
            for item in candidates
            if item.period is not None and _period_covered(draft.period, [item.period])
        ]
        if not covered:
            # Подпись могла сузить выбор до «Минимум» без бакета: пара значение–период
            # подтверждается и другой ячейкой того же результата.
            covered = [
                item
                for item in evidence
                if _value_matches(draft, item)
                and _same_measure(draft, item)
                and item.period is not None
                and _period_covered(draft.period, [item.period])
            ]
        if covered:
            candidates = covered
        elif not (
            context.covers(draft.period, {item.result_id for item in candidates})
            or _spans_whole_result(draft.period, candidates, evidence)
            or context.as_of(draft, {item.result_id for item in candidates})
        ) or any(item.period for item in evidence if _value_matches(draft, item)):
            return [], "period_mismatch"

    denominator_reason = next(
        (reason for item in candidates if (reason := _denominator_reason(draft, item))),
        None,
    )
    if denominator_reason:
        return [], denominator_reason

    result_ids = {item.result_id for item in candidates}
    # Одно и то же дробное значение в запросах одного хода — один показатель.
    if len(result_ids) > 1 and not (context.active and _exact_fractional_cell(draft, candidates)):
        return [], "ambiguous_number"
    return candidates, None


def _spans_whole_result(
    period: VerificationPeriod | None,
    candidates: Sequence[VerificationEvidence],
    evidence: Sequence[VerificationEvidence],
) -> bool:
    """«За весь период»: утверждение охватывает все даты результата, откуда взято число."""
    if period is None:
        return False
    start, end = _date(period.start), _date(period.end)
    result_ids = {item.result_id for item in candidates}
    bounds = [
        day
        for item in evidence
        if item.period is not None and item.result_id in result_ids
        for day in (_date(item.period.start), _date(item.period.end))
        if day
    ]
    return bool(start and end and bounds) and start <= min(bounds) and max(bounds) <= end


def _same_measure(draft: _Draft, item: VerificationEvidence) -> bool:
    """Колонка без названия показателя не спорит с подписью, а Profit не подтверждает Revenue."""
    column = (item.cell_address or "").rsplit(".", 1)[-1].lower()
    return column in _GENERIC_MEASURE_COLUMNS or _same_label(draft.label, item.label)


def _exact_fractional_cell(
    draft: _Draft, candidates: Sequence[VerificationEvidence]
) -> list[VerificationEvidence]:
    value = _canonical(draft.value, draft.scale)
    if value is None or value == value.to_integral_value():
        return []
    return [
        item
        for item in candidates
        if not (_aggregate_keys(draft.label) and not _aggregates_agree(draft.label, item.label))
    ]


def _filtered_count_cell(
    draft: _Draft, candidates: Sequence[VerificationEvidence]
) -> list[VerificationEvidence]:
    """COUNT(*) с собственным WHERE или GROUP BY считает отобранные сущности, а не таблицу."""
    if draft.currency or draft.unit == "percent":
        return []
    return [
        item for item in candidates if item.cell_address and _FILTERED_COUNT in (item.label or "")
    ]


def _aggregate_keys(label: str | None) -> set[str]:
    return _label_keys(label) & {
        "__average__",
        "__min__",
        "__max__",
        "__median__",
        "__std__",
        "__variance__",
        "__total__",
    }


def _aggregates_agree(left: str | None, right: str | None) -> bool:
    actual = _aggregate_keys(right)
    return not actual or not _aggregate_keys(left).isdisjoint(actual)


def _match_date(
    draft: _Draft,
    evidence: Sequence[VerificationEvidence],
    context: _QueryContext | None = None,
) -> tuple[list[VerificationEvidence], str | None]:
    if draft.period is None:
        return [], "period_mismatch"
    candidates = [
        item
        for item in evidence
        if item.period is not None and _period_covered(draft.period, [item.period])
    ]
    if not candidates:
        if context is not None and context.as_of(draft):
            return [], None
        if context is not None and (
            context.covers(draft.period) or _spans_whole_result(draft.period, evidence, evidence)
        ):
            return [], None
        return [], "period_mismatch"
    return candidates, None


def _reject(draft: _Draft, code: str) -> None:
    draft.status = "rejected"
    draft.reason_code = code
    draft.reason = _REASON_TEXT.get(code, _REASON_TEXT["unsupported_assertion"])


def _accept(draft: _Draft, evidence: Sequence[VerificationEvidence]) -> None:
    draft.status = "verified"
    draft.reason_code = None
    draft.reason = None
    draft.evidence = list(evidence)


def _resolve_draft(
    draft: _Draft,
    evidence: Sequence[VerificationEvidence],
    context: _QueryContext | None = None,
) -> None:
    if draft.status == "ignored":
        return
    if draft.kind == "formula":
        _resolve_formula(draft, evidence, context)
        return
    if draft.kind in ("date", "period"):
        matched, reason = _match_date(draft, evidence, context)
        if reason:
            _reject(draft, reason)
        else:
            _accept(draft, matched)
        return
    if context is not None and _is_query_condition(draft, context):
        draft.kind = "constant"
        draft.status = "ignored"
        draft.reason_code = "non_analytical"
        draft.reason = _REASON_TEXT["non_analytical"]
        return
    matched, reason = _match_quantity(draft, evidence, context)
    if reason:
        if (
            reason == "number_not_found"
            and _year_value(draft.value or Decimal(0)) is not None
            and not draft.currency
            and draft.unit in ("unknown", "number")
        ):
            year = _year_value(draft.value or Decimal(0))
            year_period = _year_period(year, draft.raw_text)
            if any(
                item.period is not None and _period_covered(year_period, [item.period])
                for item in evidence
            ):
                draft.kind = "period"
                draft.period = year_period
                draft.value = None
                matched, reason = _match_date(draft, evidence)
        if reason:
            _reject(draft, reason)
            return
    _accept(draft, matched)


# --- Явные формулы ---


def _formula_role(index: int, operator: str | None) -> str:
    if operator == "/":
        return "numerator" if index == 0 else "denominator"
    if operator == "*":
        return "factor"
    if operator == "+":
        return "addend"
    if operator == "-":
        return "minuend" if index == 0 else "subtrahend"
    return "operand"


def _formula_unit(unit: str) -> str:
    return unit if unit in ("money", "count", "number", "percent", "ratio", "unknown") else "number"


def _formula_operand_value(token: _NumberToken) -> Decimal:
    value = token.value * token.scale
    if token.unit == "percent":
        return value / 100
    return value


def _apply_formula(left: Decimal, operator: str, right: Decimal) -> Decimal:
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if right == 0:
        raise _FormulaError("zero_denominator")
    return left / right


def _evaluate_formula(
    tokens: Sequence[_NumberToken], operators: Sequence[str], tree: ast.Expression | None = None
) -> tuple[Decimal, str]:
    values = [_formula_operand_value(tokens[0])]
    units = [_formula_unit(tokens[0].unit)]
    currencies = [tokens[0].currency]
    pending: list[tuple[str, int]] = []
    precedence = {"+": 1, "-": 1, "*": 2, "/": 2}

    def apply() -> None:
        operator = pending.pop()[0]
        right = values.pop()
        left = values.pop()
        right_unit = units.pop()
        left_unit = units.pop()
        right_currency = currencies.pop()
        left_currency = currencies.pop()
        if operator in ("+", "-"):
            known = {unit for unit in (left_unit, right_unit) if unit != "unknown"}
            if len(known) > 1:
                raise _FormulaError("unit_mismatch")
            if left_currency and right_currency and left_currency != right_currency:
                raise _FormulaError("currency_mismatch")
            unit = next(iter(known), "unknown")
            currency = left_currency or right_currency
        elif operator == "*":
            if left_unit == "money" and right_unit == "money":
                raise _FormulaError("invalid_formula")
            unit = (
                "money"
                if "money" in (left_unit, right_unit)
                else ("count" if left_unit == "count" or right_unit == "count" else "number")
            )
            currency = left_currency or right_currency
        else:
            if right_unit == "money":
                raise _FormulaError("invalid_formula")
            unit = left_unit if left_unit != "unknown" else "number"
            currency = left_currency
        values.append(_apply_formula(left, operator, right))
        units.append(unit)
        currencies.append(currency)

    if tree is not None:
        values.clear()
        units.clear()
        currencies.clear()

        def visit(node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                token = tokens[int(node.id[1:])]
                values.append(_formula_operand_value(token))
                units.append(_formula_unit(token.unit))
                currencies.append(token.currency)
            elif isinstance(node, ast.BinOp):
                visit(node.left)
                visit(node.right)
                operator = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}[type(node.op)]
                pending.append((operator, 0))
                apply()
            else:
                raise _FormulaError("invalid_formula")

        visit(tree.body)
        return values[0], units[0]

    for index, operator in enumerate(operators):
        while pending and precedence[pending[-1][0]] >= precedence[operator]:
            apply()
        pending.append((operator, index))
        values.append(_formula_operand_value(tokens[index + 1]))
        units.append(_formula_unit(tokens[index + 1].unit))
        currencies.append(tokens[index + 1].currency)
    while pending:
        apply()
    return values[0], units[0]


def _formula_result_matches(
    draft: _Draft, calculated: Decimal, unit: str, currency: str | None
) -> bool:
    claimed = _canonical(draft.value, draft.scale)
    if claimed is None:
        return False
    if draft.unit == "percent":
        if unit not in ("percent", "ratio", "number", "unknown"):
            return False
        calculated = calculated * 100
    elif draft.unit == "money":
        if unit != "money":
            return False
        if draft.currency and draft.currency != currency:
            return False
    elif draft.unit == "count":
        if unit == "money" or unit == "percent":
            return False
    elif draft.unit == "number" and unit == "money":
        return False
    return _values_equal(claimed, calculated, _draft_decimals(draft))


def _resolve_formula(
    draft: _Draft,
    evidence: Sequence[VerificationEvidence],
    context: _QueryContext | None = None,
) -> None:
    spec = _formula_specs(draft.text, _tokens(draft.text))
    raw_spec = next(
        (item for item in spec if item.raw == draft.raw_text or item.expression == draft.formula),
        None,
    )
    if raw_spec is None:
        _reject(draft, "invalid_formula")
        return
    operands: list[VerificationOperand] = []
    matched_evidence: list[VerificationEvidence] = []
    resolved_tokens: list[_NumberToken] = []
    shared_result_ids: set[str] | None = None
    sql_divisors: set[Decimal] = set()
    operators: list[str] = []
    for before, after in zip(raw_spec.left, raw_spec.left[1:], strict=False):
        gap = draft.text[before.end : after.start].strip().replace("(", "").replace(")", "").strip()
        operator = _operator(gap)
        if operator is None:
            _reject(draft, "invalid_formula")
            return
        operators.append(operator)
    for index, token in enumerate(raw_spec.left):
        operator = operators[index] if index < len(operators) else operators[-1]
        operand_draft = _Draft(
            kind="quantity",
            text=draft.text,
            raw_text=token.raw,
            value=token.value,
            scale=token.scale,
            unit=token.unit,
            currency=token.currency,
            period=draft.period,
        )
        matched: list[VerificationEvidence] = []
        reason = None
        matched, reason = _match_quantity(operand_draft, evidence)
        if reason:
            if (
                reason == "number_not_found"
                and (
                    token.value * token.scale in _FORMULA_CONSTANTS
                    or (context and token.value * token.scale in context.divisors())
                )
                and token.unit in ("unknown", "number")
            ):
                if token.value * token.scale not in _FORMULA_CONSTANTS:
                    sql_divisors.add(token.value * token.scale)
                # Постоянные коэффициенты (0, 1, 100, 1000) не требуют
                # происхождения; совпавшее значение всё равно сохраняем.
                matched, reason = [], None
            else:
                _reject(draft, reason)
                return
        if matched:
            result_ids = {item.result_id for item in matched}
            shared_result_ids = (
                result_ids if shared_result_ids is None else shared_result_ids & result_ids
            )
            if not shared_result_ids:
                _reject(draft, "unrelated_result")
                return
            units = {item.unit for item in matched} - {"unknown", "number"}
            currencies = {item.currency for item in matched if item.currency}
            if len(units) > 1 or len(currencies) > 1:
                _reject(draft, "ambiguous_number")
                return
            token = replace(
                token,
                unit=(
                    next(iter(units), token.unit)
                    if token.unit in ("unknown", "number")
                    else token.unit
                ),
                currency=next(iter(currencies), token.currency),
            )
        resolved_tokens.append(token)
        matched_evidence.extend(matched)
        operands.append(
            VerificationOperand(
                role=_formula_role(index, operator),  # type: ignore[arg-type]
                raw_text=token.raw,
                value=token.value * token.scale,
                unit=token.unit,  # type: ignore[arg-type]
                currency=token.currency,
                evidence=matched,
            )
        )
    # Делитель должен принадлежать тому же запросу, что и остальные числа формулы.
    if sql_divisors and not (
        context and shared_result_ids and sql_divisors <= context.divisors(shared_result_ids)
    ):
        _reject(draft, "number_not_found")
        return
    try:
        calculated, unit = _evaluate_formula(resolved_tokens, operators, raw_spec.tree)
    except _FormulaError as error:
        _reject(draft, error.code)
        return
    # An explicit final ``* 100`` already expresses a ratio as percentage points.
    body = raw_spec.tree.body if raw_spec.tree else None
    if (
        draft.unit == "percent"
        and unit in ("number", "ratio", "unknown", "percent")
        and isinstance(body, ast.BinOp)
        and isinstance(body.op, ast.Mult)
        and isinstance(body.right, ast.Name)
        and resolved_tokens[int(body.right.id[1:])].value == 100
    ):
        calculated /= 100
    if not _formula_result_matches(
        draft,
        calculated,
        unit,
        next((token.currency for token in resolved_tokens if token.currency), None),
    ):
        _reject(draft, "rounding_mismatch")
        return
    draft.operands = operands
    draft.formula = raw_spec.expression
    _accept(draft, matched_evidence)


# --- Публичная проверка ---


def _assertion(draft: _Draft, index: int) -> NumericAssertion:
    return NumericAssertion(
        assertion_id=f"a{index + 1}",
        kind=draft.kind,  # type: ignore[arg-type]
        status=draft.status,  # type: ignore[arg-type]
        text=draft.text or draft.raw_text,
        raw_text=draft.raw_text,
        value=draft.value,
        scale=draft.scale,
        unit=draft.unit,  # type: ignore[arg-type]
        currency=draft.currency,
        period=draft.period,
        population=draft.population,
        denominator=draft.denominator,
        label=draft.label,
        evidence=draft.evidence,
        formula=draft.formula,
        operands=draft.operands,
        reason_code=draft.reason_code,  # type: ignore[arg-type]
        reason=draft.reason,
    )


def verify_numeric_text(
    text: str,
    evidence: Sequence[VerificationEvidence],
    *,
    result_ids: Sequence[str] | None = None,
    question: str | None = None,
    sql: Mapping[str, str] | None = None,
) -> VerificationReport:
    """Проверяет числа текста по заранее собранному происхождению.

    Проверка не ищет числа в других результатах, не масштабирует их неявно и не
    угадывает связи между таблицами. ``result_ids`` — границы ответа: в отчёте
    перечисляются только результаты, по которым действительно проверяли текст.
    """
    scope = (
        list(result_ids)
        if result_ids is not None
        else list(dict.fromkeys(item.result_id for item in evidence))
    )
    scoped = [item for item in evidence if item.result_id in scope]
    drafts = _extract_drafts(text)
    assertions: list[NumericAssertion] = []
    rejected: list[NumericAssertion] = []
    issues: list[VerificationIssue] = []
    context = _query_context(question, sql) if question or sql else None
    for draft in drafts:
        _resolve_draft(draft, scoped, context)
        if context is not None and _label_only_rejection(draft, scoped, context):
            # Число есть в результате, спорна только подпись: ответ не блокируем,
            # причина остаётся в отчёте у этого утверждения.
            draft.status = "ignored"
        assertion = _assertion(draft, len(assertions))
        assertions.append(assertion)
        if draft.status == "rejected":
            rejected.append(assertion)
            issues.append(
                VerificationIssue(
                    code=draft.reason_code or "unsupported_assertion",  # type: ignore[arg-type]
                    message=draft.reason or _REASON_TEXT["unsupported_assertion"],
                    assertion_id=assertion.assertion_id,
                )
            )
    if rejected:
        status = "failed"
    elif any(item.status == "verified" for item in assertions):
        status = "verified"
    else:
        status = "not_applicable"
    return VerificationReport(
        status=status,  # type: ignore[arg-type]
        assertions=assertions,
        rejected_assertions=rejected,
        issues=issues,
        result_ids=scope,
    )
