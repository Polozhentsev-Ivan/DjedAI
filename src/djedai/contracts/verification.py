"""Контракт проверки числовых утверждений ответа (#167).

Проверка детерминированная: каждое аналитическое число в опубликованном тексте
должно ссылаться на факт, ячейку результата, счётчик строк или метрику либо быть
результатом явной формулы из таких операндов. Контракт хранит не только итоговый
статус, но и происхождение принятого утверждения и безопасную причину отказа.

``None`` у необязательного поля означает «неизвестно», а не «любое значение».
Поле ``unknown`` явно перечисляет неизвестные части происхождения, чтобы
потребитель не принимал отсутствие данных за подтверждение.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VerificationUnit = Literal["money", "count", "number", "percent", "ratio", "date", "unknown"]
EvidenceSource = Literal["fact", "data_cell", "row_count", "metric", "period"]
PeriodGrain = Literal["day", "week", "month", "quarter", "year", "range", "unknown"]
AssertionKind = Literal["quantity", "formula", "date", "period", "list_number", "constant"]
AssertionStatus = Literal["verified", "rejected", "ignored"]
VerificationStatus = Literal["verified", "corrected", "failed", "not_applicable"]
VerificationReason = Literal[
    "number_not_found",
    "ambiguous_number",
    "unrelated_result",
    "unit_mismatch",
    "currency_mismatch",
    "period_mismatch",
    "population_mismatch",
    "denominator_mismatch",
    "zero_denominator",
    "missing_operand",
    "invalid_formula",
    "rounding_mismatch",
    "unsupported_assertion",
    "non_analytical",
    "llm_reported",
    "invalid_correction",
    "llm_unavailable",
]
OperandRole = Literal[
    "addend", "minuend", "subtrahend", "factor", "numerator", "denominator", "operand"
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationPeriod(_Model):
    """Период, к которому относится значение.

    ``start`` включительно, ``end`` не включительно, даты — ISO. ``None`` означает,
    что граница неизвестна; ``unknown`` у evidence перечисляет такие поля.
    """

    start: str | None = Field(default=None, description="Начало периода включительно (ISO)")
    end: str | None = Field(default=None, description="Конец периода не включительно (ISO)")
    label: str | None = Field(default=None, description="Период так, как он назван в источнике")
    grain: PeriodGrain = Field(default="unknown", description="Гранулярность периода")


class VerificationDenominator(_Model):
    """Знаменатель доли или процента и его происхождение."""

    label: str | None = Field(default=None, description="Что именно является знаменателем")
    value: Decimal | None = Field(default=None, description="Значение знаменателя")
    result_id: str | None = Field(
        default=None, description="Результат, из которого взят знаменатель"
    )
    fact_id: str | None = Field(default=None, description="Факт, из которого взят знаменатель")
    cell_address: str | None = Field(default=None, description="Ячейка знаменателя, если известна")


class VerificationEvidence(_Model):
    """Одно допустимое происхождение числа.

    ``value`` — значение в единицах источника, ``scale`` — явный множитель
    (например, 1000 для «тыс.»). Каноническое значение — ``value * scale``.
    Неявного умножения на 1000 проверка не делает.
    """

    result_id: str = Field(description="ID результата инструмента")
    fact_id: str | None = Field(default=None, description="ID факта вида result:fact:index")
    cell_address: str | None = Field(
        default=None, description="Адрес ячейки вида data.rows[0].revenue или data.row_count"
    )
    value: Decimal | None = Field(default=None, description="Значение в единицах источника")
    scale: Decimal = Field(default=Decimal(1), description="Явный множитель из текста источника")
    unit: VerificationUnit = Field(default="unknown", description="Единица измерения значения")
    currency: str | None = Field(default=None, description="Код валюты, если он известен")
    period: VerificationPeriod | None = Field(default=None, description="Период значения")
    population: str | None = Field(default=None, description="Популяция или фильтры значения")
    denominator: VerificationDenominator | None = Field(
        default=None, description="Знаменатель для доли или процента"
    )
    label: str | None = Field(default=None, description="Подпись значения из факта или колонки")
    source: EvidenceSource = Field(description="Откуда взято происхождение")
    unknown: list[str] = Field(
        default_factory=list,
        description="Явно неизвестные части происхождения: period, population, denominator и т. п.",
    )


class VerificationOperand(_Model):
    """Операнд явной формулы с собственной ссылкой на происхождение."""

    role: OperandRole = Field(description="Роль операнда в формуле")
    raw_text: str = Field(description="Как операнд записан в ответе")
    value: Decimal = Field(description="Каноническое значение операнда")
    unit: VerificationUnit = Field(default="unknown", description="Единица измерения операнда")
    currency: str | None = Field(default=None, description="Валюта операнда")
    evidence: list[VerificationEvidence] = Field(
        default_factory=list, description="Происхождение операнда; пусто только у константы формулы"
    )


class NumericAssertion(_Model):
    """Одно числовое утверждение и результат его детерминированной проверки."""

    assertion_id: str = Field(description="Стабильный ID утверждения внутри ответа")
    kind: AssertionKind = Field(description="Вид утверждения")
    status: AssertionStatus = Field(description="Принято, отклонено или не аналитическое")
    text: str = Field(description="Фрагмент ответа, в котором встретилось утверждение")
    raw_text: str = Field(description="Число или формула так, как записаны в ответе")
    value: Decimal | None = Field(default=None, description="Каноническое значение")
    scale: Decimal = Field(default=Decimal(1), description="Явный множитель утверждения")
    unit: VerificationUnit = Field(default="unknown", description="Единица измерения")
    currency: str | None = Field(default=None, description="Валюта утверждения")
    period: VerificationPeriod | None = Field(default=None, description="Период утверждения")
    population: str | None = Field(default=None, description="Популяция утверждения")
    denominator: VerificationDenominator | None = Field(
        default=None, description="Знаменатель утверждения"
    )
    label: str | None = Field(default=None, description="Подпись утверждения из текста")
    evidence: list[VerificationEvidence] = Field(
        default_factory=list, description="Принятое происхождение утверждения"
    )
    formula: str | None = Field(default=None, description="Явная формула производного значения")
    operands: list[VerificationOperand] = Field(
        default_factory=list, description="Операнды формулы с ссылками на происхождение"
    )
    reason_code: VerificationReason | None = Field(
        default=None, description="Безопасный код причины отказа"
    )
    reason: str | None = Field(default=None, description="Безопасное пояснение причины отказа")


class VerificationIssue(_Model):
    """Безопасная причина, по которой утверждение не было принято."""

    code: VerificationReason = Field(description="Код причины")
    message: str = Field(description="Пояснение, пригодное для пользователя")
    assertion_id: str | None = Field(default=None, description="Связанное утверждение")


class VerificationReport(_Model):
    """Итог проверки чисел в опубликованном ответе."""

    status: VerificationStatus = Field(description="Итоговый статус проверки")
    assertions: list[NumericAssertion] = Field(
        default_factory=list, description="Утверждения финального текста"
    )
    rejected_assertions: list[NumericAssertion] = Field(
        default_factory=list, description="Отклонённые утверждения предыдущих черновиков"
    )
    issues: list[VerificationIssue] = Field(
        default_factory=list, description="Причины отказа и исправления"
    )
    result_ids: list[str] = Field(
        default_factory=list, description="Результаты, в границах которых проверялся ответ"
    )
