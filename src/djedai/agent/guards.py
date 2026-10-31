"""Детерминированные проверки вопроса: работают до модели и не зависят от её силы.

Срабатывают только на явных формах (перепутанные даты, команда на запись, чтение
внешнего источника). Всё сомнительное остаётся модели: упоминание SQL или слова
«удаление» в обычном вопросе не должно приводить к отказу.
"""

import calendar
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from djedai.contracts.agent import Block, Clarification, Understanding

WRITE_REJECTION = (
    "Запрос отклонён: он требует изменить данные или обойти режим только чтения, "
    "а аналитический доступ к данным открыт только на чтение. Команда не отправлена "
    "на исполнение, исходная таблица не изменена. Могу показать нужные строки "
    "фильтром или посчитать их без изменения данных."
)
EXTERNAL_REJECTION = (
    "Запрос отклонён: чтение внешних ссылок и локальных файлов вне выбранного "
    "датасета недоступно. Команда не отправлена на исполнение, внешнее содержимое "
    "не получено. Чтобы проанализировать этот файл, загрузите его как источник "
    "штатной загрузкой, и я смогу работать с ним."
)
MUTATION_QUESTION = (
    "Исходные данные не изменены: аналитический доступ открыт только на чтение, "
    "поэтому удалить или перезаписать строки нельзя. Можно выбрать безопасный анализ "
    "или фильтрацию результата."
)
MUTATION_OPTIONS = [
    "Показать нужные строки фильтром, исходные данные не меняются",
    "Сравнить количество строк по категориям",
]
MISSING_MONEY_QUESTION = (
    "В выбранном датасете нет денежных полей, поэтому расчёт не выполнялся. Выручку, "
    "прибыль и другие денежные показатели нельзя получить из имеющихся счётчиков "
    "и других полей. Что сделать вместо этого?"
)
# Формулировки кнопок сами уходят в чат следующим вопросом: они не должны снова
# вызывать проверку, поэтому здесь нет названий показателей и повелительного «удали».
MISSING_MONEY_OPTIONS = [
    "Показать, какие показатели есть в этом датасете",
    "Загрузить источник с денежными данными и уточнить связь с этим датасетом",
]
OTHER_PERIOD_OPTION = "Другой период: указать правильные начальную и конечную даты"

# --- Период ---

_MONTH_STEMS = {
    "январ": 1,
    "jan": 1,
    "феврал": 2,
    "feb": 2,
    "март": 3,
    "mar": 3,
    "апрел": 4,
    "apr": 4,
    "май": 5,
    "мая": 5,
    "may": 5,
    "июн": 6,
    "jun": 6,
    "июл": 7,
    "jul": 7,
    "август": 8,
    "aug": 8,
    "сентябр": 9,
    "sep": 9,
    "октябр": 10,
    "oct": 10,
    "ноябр": 11,
    "nov": 11,
    "декабр": 12,
    "dec": 12,
}
_MONTH_NAMES = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
_MONTH_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MONTH = "(?:" + "|".join(_MONTH_STEMS) + r")\w*"
_YEAR_WORD = r"(?:\s*(?:г\.|гг\.?|года?|году)(?!\w))"


def _date_pattern(prefix: str, *forms: str) -> str:
    """Одна граница периода в перечисленных формах; порядок важен для разбора."""
    p = prefix
    patterns = {
        "iso": rf"(?P<{p}iy>\d{{4}})-(?P<{p}im>\d{{2}})-(?P<{p}id>\d{{2}})",
        "dots": (
            rf"(?P<{p}nd>\d{{1,2}})\.(?P<{p}nm>\d{{1,2}})"
            rf"(?:\.(?P<{p}ny>\d{{4}}|\d{{2}}))?{_YEAR_WORD}?"
        ),
        "dots_year": rf"(?P<{p}nd>\d{{1,2}})\.(?P<{p}nm>\d{{1,2}})\.(?P<{p}ny>\d{{4}})",
        "slash": rf"(?P<{p}sa>\d{{1,2}})/(?P<{p}sb>\d{{1,2}})/(?P<{p}sy>\d{{4}})",
        "text": (
            rf"(?P<{p}td>\d{{1,2}})\s+(?P<{p}tm>{_MONTH})"
            rf"(?:\s+(?P<{p}ty>\d{{4}}){_YEAR_WORD}?)?"
        ),
        "english": rf"(?P<{p}em>{_MONTH})\s+(?P<{p}ed>\d{{1,2}}),?(?:\s+(?P<{p}ey>\d{{4}}))?",
        "month": rf"(?P<{p}mm>{_MONTH})(?:\s+(?P<{p}my>\d{{4}}){_YEAR_WORD}?)?",
        "year": rf"(?P<{p}y>(?:19|20)\d{{2}})(?P<{p}yw>{_YEAR_WORD})?",
        "day": rf"(?P<{p}d>\d{{1,2}})",
    }
    return "(?:" + "|".join(patterns[form] for form in forms) + r")(?!\d)"


_FROM_TO_FORMS = ("iso", "dots", "slash", "text", "english", "month", "year")
_DAY_FORMS = ("iso", "dots", "slash", "text", "english")
_PERIOD_RE = re.compile(
    r"(?<![\w-])(?:со?|от|from)\s+"
    + _date_pattern("s", *_FROM_TO_FORMS, "day")
    + r"(?:,?\s+(?:включительно|inclusive))?,?\s+(?:по|до|to|until|through)\s+"
    + _date_pattern("e", *_FROM_TO_FORMS),
    re.IGNORECASE,
)
# «Между» — порядок не задаёт направление, если это сравнение двух дней, а не диапазон:
# годы и месяцы сюда не входят, а вопрос со словом сравнения пропускается.
_BETWEEN_RE = re.compile(
    r"(?<![\w-])(?:между|between)\s+"
    + _date_pattern("s", *_DAY_FORMS, "day")
    + r"\s+(?:и|and)\s+"
    + _date_pattern("e", *_DAY_FORMS),
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(r"сравн|разниц|отлич|compar|differ|versus", re.IGNORECASE)
# Диапазон через дефис или тире: только полные даты, чтобы не задеть «10.5 - 3.2» и «5 - 10».
_DASH_RE = re.compile(
    r"(?<![\w./-])"
    + _date_pattern("s", "iso", "dots_year", "slash", "text")
    + r"\s*[-–—]\s*"
    + _date_pattern("e", "iso", "dots_year", "slash", "text"),
    re.IGNORECASE,
)
_PERIOD_WORD_RE = re.compile(r"период|period", re.IGNORECASE)


@dataclass(frozen=True)
class _Point:
    year: int | None = None
    month: int | None = None
    day: int | None = None

    @property
    def label(self) -> str:
        year = f" {self.year}" if self.year else ""
        if self.day is not None and self.month is not None:
            return f"{self.day} {_MONTH_GENITIVE[self.month - 1]}{year}"
        if self.month is not None:
            return f"{_MONTH_NAMES[self.month - 1]}{year}"
        return f"{self.year} год"


def _month_number(text: str) -> int:
    lowered = text.lower()
    return next(number for stem, number in _MONTH_STEMS.items() if lowered.startswith(stem))


def _full_year(text: str | None) -> int | None:
    if not text:
        return None
    return int(text) + 2000 if len(text) == 2 else int(text)


def _point(match: re.Match[str], p: str, *, month_first: bool = False) -> _Point:
    groups = match.groupdict()
    if groups.get(f"{p}sa"):
        first, second = int(groups[f"{p}sa"]), int(groups[f"{p}sb"])
        day, month = (second, first) if month_first else (first, second)
        return _Point(int(groups[f"{p}sy"]), month, day)
    if groups.get(f"{p}iy"):
        return _Point(int(groups[f"{p}iy"]), int(groups[f"{p}im"]), int(groups[f"{p}id"]))
    if groups.get(f"{p}nd"):
        return _Point(_full_year(groups[f"{p}ny"]), int(groups[f"{p}nm"]), int(groups[f"{p}nd"]))
    if groups.get(f"{p}td"):
        return _Point(
            _full_year(groups[f"{p}ty"]), _month_number(groups[f"{p}tm"]), int(groups[f"{p}td"])
        )
    if groups.get(f"{p}em"):
        return _Point(
            _full_year(groups[f"{p}ey"]), _month_number(groups[f"{p}em"]), int(groups[f"{p}ed"])
        )
    if groups.get(f"{p}mm"):
        return _Point(_full_year(groups[f"{p}my"]), _month_number(groups[f"{p}mm"]))
    if groups.get(f"{p}y"):
        return _Point(int(groups[f"{p}y"]))
    return _Point(day=int(groups[f"{p}d"]))


def _complete(start: _Point, end: _Point) -> tuple[_Point, _Point] | None:
    """Достраивает опущенные месяц и год; None, если порядок нельзя определить.

    «С 30 ноября по 1 февраля» может переходить через Новый год, поэтому без обоих
    годов разные месяцы не сравниваются. Один месяц сравним и без года.
    """
    if start.month is None and start.day is not None:
        start = _Point(end.year, end.month, start.day)
    if start.year is not None and end.year is not None:
        return start, end
    if start.month is None or start.month != end.month:
        return None
    # Високосный год, чтобы «29 февраля» без года оставалось допустимой датой.
    year = start.year or end.year or 2000
    return _Point(year, start.month, start.day), _Point(year, end.month, end.day)


def _bounds(point: _Point) -> tuple[date, date] | None:
    """Первый и последний день, которые покрывает граница; None для несуществующей даты."""
    year = point.year or 2000
    first_month, last_month = (point.month, point.month) if point.month else (1, 12)
    try:
        if point.day is not None:
            day = date(year, first_month, point.day)
            return day, day
        last_day = calendar.monthrange(year, last_month)[1]
        return date(year, first_month, 1), date(year, last_month, last_day)
    except ValueError:
        return None


@dataclass(frozen=True)
class ReversedPeriod:
    start: _Point
    end: _Point

    def clarification(self) -> Clarification:
        return Clarification(
            question=(
                f"Начало периода ({self.start.label}) позже его окончания ({self.end.label}), "
                "поэтому расчёт не выполнялся. Переставлять даты автоматически нельзя: "
                "подтвердите правильные границы."
            ),
            options=[self._swapped_option(), OTHER_PERIOD_OPTION],
        )

    def _swapped_option(self) -> str:
        first, last = self.end, self.start
        if (
            first.year
            and first.year == last.year
            and first.month
            and first.month == last.month
            and first.day == 1
            and last.day == calendar.monthrange(first.year, first.month)[1]
        ):
            month = first.month - 1
            return (
                f"Весь {_MONTH_NAMES[month]} {first.year}: "
                f"с 1 по {last.day} {_MONTH_GENITIVE[month]} включительно"
            )
        return f"Период: {first.label} — {last.label}"


def _reversed_in_reading(
    match: re.Match[str], *, month_first: bool
) -> tuple[bool, ReversedPeriod | None]:
    """Допустимо ли это прочтение дат и, если да, перепутан ли в нём период."""
    start = _point(match, "s", month_first=month_first)
    end = _point(match, "e", month_first=month_first)
    if (completed := _complete(start, end)) is None:
        return False, None
    start_bounds, end_bounds = _bounds(completed[0]), _bounds(completed[1])
    if start_bounds is None or end_bounds is None:
        return False, None
    if start_bounds[0] <= end_bounds[1]:
        return True, None
    # В подписях остаётся то, что написал пользователь: без подставленного года.
    shown_start = _Point(
        start.year if start.year else completed[0].year if end.year else None,
        completed[0].month,
        completed[0].day,
    )
    shown_end = _Point(end.year or (start.year and completed[1].year), end.month, end.day)
    return True, ReversedPeriod(shown_start, shown_end)


def _reversed_match(match: re.Match[str]) -> ReversedPeriod | None:
    """Период считается перепутанным, только если это верно во всех допустимых прочтениях.

    «03/05/2026» бывает и 3 мая, и 5 марта: при сомнении вопрос уходит модели.
    """
    readings = [_reversed_in_reading(match, month_first=flag) for flag in (False, True)]
    valid = [period for is_valid, period in readings if is_valid]
    if not valid or any(period is None for period in valid):
        return None
    return valid[0]


def find_reversed_period(text: str) -> ReversedPeriod | None:
    """Первый явно заданный период, у которого начало позже конца."""
    for match in _PERIOD_RE.finditer(text):
        # Голые числа могут быть не годами: «с 2000 по 1990 рублей».
        if (
            match["sy"]
            and match["ey"]
            and not (match["syw"] or match["eyw"] or _PERIOD_WORD_RE.search(text))
        ):
            continue
        if (period := _reversed_match(match)) is not None:
            return period
    for match in _DASH_RE.finditer(text):
        if (period := _reversed_match(match)) is not None:
            return period
    if not _COMPARISON_RE.search(text):
        for match in _BETWEEN_RE.finditer(text):
            if (period := _reversed_match(match)) is not None:
                return period
    return None


# --- Цитаты в условиях фильтра ---

_QUOTED_RE = re.compile(r"«[^«»\n]*»|“[^”\n]*”|„[^“”\n]*[“”]|\"[^\"\n]*\"|'[^'\n]*'")
_QUOTE_PLACEHOLDER = "«»"
# Цитата — значение фильтра, только если стоит сразу за таким словом или за соседней цитатой.
_FILTER_BEFORE_QUOTE_RE = re.compile(
    r"(?:(?<!\w)(?:содерж\w*|текст\w*|слов\w*|фраз\w*|сообщени\w*|строк\w*|запис\w*|значени\w*"
    r"|равн\w*|встреча\w*|like|contain\w*|text|messages?|phrases?|strings?|values?)"
    r"|=|«»\s*(?:,|или|и|or|and))[\s:,-]*$",
    re.IGNORECASE,
)


# Что запускается: подсчёт и анализ сами по себе не приказ выполнить цитату.
_ANALYSIS_OBJECT_RE = re.compile(
    r"\s+(?:(?:a|an|the)\s+)?(?:подсч[её]т|расч[её]т|анализ|поиск|выборк|фильтрац|агрегац"
    r"|count|analysis|search|filter|lookup)\w*(?!\w)",
    re.IGNORECASE,
)
_QUOTE_LIST_RE = re.compile(r"«»(?:\s*(?:,|или|и|or|and)\s*«»)*", re.IGNORECASE)
# После такой границы идёт новая часть поручения: «подсчёт и строку «…»».
_CLAUSE_BREAK_RE = re.compile(
    r"[,;:]|(?<!\w)(?:и|затем|потом|после|а\s+также|then|and|after)(?!\w)", re.IGNORECASE
)


def _orders_to_run(sentence: str) -> bool:
    """Есть ли глагол запуска, который может относиться к цитате в конце фразы."""
    for verb in _RUN_VERB_RE.finditer(sentence):
        rest = sentence[verb.end() :]
        analysis = _ANALYSIS_OBJECT_RE.match(rest)
        if analysis is None:
            return True
        tail = _QUOTE_LIST_RE.sub(" ", rest[analysis.end() :])
        if _CLAUSE_BREAK_RE.search(tail):
            return True
    return False


def _mask_filter_quotes(text: str) -> str:
    """Скрывает цитаты, которые задают условие поиска: их содержимое не приказ агенту.

    Цитата после глагола запуска остаётся видимой: «Выполни строку "DROP TABLE orders"».
    Исключение — «Выполни подсчёт строк с текстом «…»»: запускается подсчёт, а не цитата.
    """
    parts: list[str] = []
    position = 0
    for match in _QUOTED_RE.finditer(text):
        parts.append(text[position : match.start()])
        position = match.end()
        sentence = _SENTENCE_END_RE.split("".join(parts))[-1]
        is_filter = _FILTER_BEFORE_QUOTE_RE.search(sentence) and not _orders_to_run(sentence)
        parts.append(_QUOTE_PLACEHOLDER if is_filter else match[0])
    parts.append(text[position:])
    return "".join(parts)


# --- Опасные запросы ---

# Только для вызова чтения в той же фразе: команды записи и обхода сами требуют приказа,
# поэтому «Что передо мной? Выполни DROP TABLE» исключением не прикрыть.
_EXPLANATION_RE = re.compile(
    r"^\s*(?:что|как|почему|зачем|объясни\w*|расскажи\w*|what|why|how|explain)\b", re.IGNORECASE
)
_WRITE_SQL = (
    r"(?:drop\s+(?:table|view|schema|database|index)\b|delete\s+from\b|truncate\s+(?:table\s+)?\S"
    r"|update\s+\S+\s+set\b|insert\s+into\b|alter\s+table\b"
    r"|create\s+(?:or\s+replace\s+)?(?:temp\w*\s+)?(?:table|view|schema|database)\b"
    r"|merge\s+into\b|attach\s+(?:database\s+)?['\"]|copy\s+\S+\s+(?:to|from)\s+['\"]"
    r"|grant\s+\w+\s+on\b)"
)
_RUN_VERB = r"(?:выполни|запусти|исполни|сделай|примени)(?:те)?|execute|run|perform"
_RUN_VERB_RE = re.compile(rf"(?<!\w)(?:{_RUN_VERB})(?!\w)", re.IGNORECASE)
# Команда на запись опасна как приказ: в начале сообщения, отдельным оператором после
# «;» или конца фразы, сразу за скрытой цитатой или за глаголом запуска.
# «Сколько запросов DROP TABLE в логе?» сюда не попадает.
_WRITE_COMMAND_RE = re.compile(
    rf"^\s*(?:sql\s*:\s*)?{_WRITE_SQL}|[;.!?\n]\s*{_WRITE_SQL}"
    rf"|{_QUOTE_PLACEHOLDER}[\s,]*(?:и|затем|потом|then|and)\s+{_WRITE_SQL}"
    rf"|(?<!\w)(?:{_RUN_VERB})(?!\w)[\s:,-]+(?:[\w\"'«»-]+[\s:,]+){{0,4}}[\"'«“„]?{_WRITE_SQL}",
    re.IGNORECASE,
)
_BYPASS_RE = re.compile(
    r"(?<!\w)(?:игнорируй|обойди|отключи|сними|забудь)(?:те)?\s+(?:[\w-]+\s+){0,3}"
    r"(?:режим\w*\s+только\s+(?:для\s+)?чтения|только\s+для\s+чтения|read[- ]?only"
    r"|предыдущ\w+\s+инструкци|системн\w+\s+(?:инструкци|промпт)"
    r"|ограничени\w+\s+(?:безопасност|доступа))"
    r"|(?<!\w)(?:ignore|bypass|disable|forget)\s+(?:[\w-]+\s+){0,3}"
    r"(?:read[- ]?only|previous\s+instructions|system\s+prompt)",
    re.IGNORECASE,
)
_DIRECT_COMMAND_RE = re.compile(
    rf"(?<!\w)(?:{_RUN_VERB}|(?:прочитай|прочти|открой|скачай|загрузи|покажи|выведи)(?:те)?)(?!\w)",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?\n]\s")
_READ_CALL_RE = re.compile(
    r"(?<!\w)(?:read_(?:csv|csv_auto|parquet|json|json_auto|ndjson|ndjson_auto|text|blob|xlsx)"
    r"|parquet_scan|csv_scan|sniff_csv|glob|sqlite_scan|postgres_scan|mysql_scan)"
    r"\s*\(\s*['\"\[]"
    r"|(?<!\w)from\s+['\"](?:https?:|s3:|file:|/|[a-z]:[\\/])",
    re.IGNORECASE,
)
_READ_LOCATION_RE = re.compile(
    r"(?<!\w)(?:прочитай|прочти|открой|скачай|загрузи|импортируй|read|open|download|fetch)"
    r"(?:те)?\s+(?:\S+\s+){0,5}?"
    r"(?:https?://|s3://|ftp://|file://|[a-z]:[\\/]|~/|/etc/|/home/|/root/|/var/)",
    re.IGNORECASE,
)
_MUTATION_RE = re.compile(
    r"(?:^|[.!?;:\n,]\s*)(?:пожалуйста,?\s+)?"
    r"(?:удали|сотри|очисти|перезапиши|delete|erase|wipe)(?:те)?[\s,]"
    r"[^.!?\n]*(?:таблиц|датасет|баз\w+\s+данных|исходн\w+\s+данн|загруженн\w+\s+данн"
    r"|dataset|table)",
    re.IGNORECASE,
)
# Убрать что-то из ответа или вычислить результат без дублей — это обычная аналитика.
_MUTATION_EXCEPTION_RE = re.compile(
    r"(?:из|from)\s+(?:\w+\s+)?(?:результат|ответ|график|диаграмм|дашборд|отч[её]т|chart|answer)"
    r"|посчитай|покажи|построй|сравни|выведи|дубл",
    re.IGNORECASE,
)

# --- Отсутствующие денежные поля ---

# «Прибыли» — ещё и глагол («посылки прибыли вовремя»), поэтому эта форма считается
# показателем только рядом со словом, которое её определяет; без него решает модель.
_MONEY_QUESTION_RE = re.compile(
    r"(?<!\w)(?:выручк\w*|прибыл(?:ь|ью|ей|ям|ями|ях|ьн\w*)"
    r"|(?:сумм\w*|размер\w*|рост\w*|динамик\w*|дол[яиюе]й?|показател\w*|расч[её]т\w*|объ[её]м\w*"
    r"|уров\w*|изменени\w*|падени\w*|снижени\w*|чист\w+|валов\w+|операционн\w+|средн\w+|по|от)"
    r"\s+прибыли|доход(?:а|у|ы|ов|ом|ах|ность\w*)?|марж\w*"
    r"|средн\w+\s+чек\w*|revenue|profit\w*|income|gross\s+margin|gmv|arpu)(?!\w)",
    re.IGNORECASE,
)
# Показатель назван как исключение или замена: считать его не просят.
_MONEY_EXCLUDED_BEFORE_RE = re.compile(
    r"(?<!\w)(?:без|вместо|кроме|исключая|нет|не\s+(?:считай|считайте|считать|учитывай|учитывайте"
    r"|показывай|показывайте|бери|берите|используй|используйте|нужн\w*|надо)"
    r"|without|instead\s+of|except|excluding|not)\s+(?:[\w-]+,?\s+){0,2}$",
    re.IGNORECASE,
)
_MONEY_EXCLUDED_AFTER_RE = re.compile(r"\s+(?:нет|не\s+нужн\w*|отсутству\w*)(?!\w)", re.IGNORECASE)
_MONEY_TOKEN_PREFIXES = (
    "выручк",
    "доход",
    "прибыл",
    "стоимост",
    "сумм",
    "денеж",
    "оплат",
    "платеж",
    "платёж",
    "рубл",
    "валют",
    "бюджет",
    "тариф",
    "продаж",
    "зарплат",
    "vyruchk",
    "dohod",
    "pribyl",
    "stoimost",
    "oplat",
    "platezh",
    "rubl",
    "valyut",
    "prodazh",
    "tarif",
    "byudzhet",
    "price",
    "amount",
    "revenue",
    "sale",
    "money",
    "payment",
    "income",
    "profit",
    "margin",
    "salary",
    "budget",
    "currency",
    "расход",
    "затрат",
    "оборот",
    "комисс",
    "скидк",
    "выплат",
    "налог",
    "оклад",
    "кредит",
    "гонорар",
    "rashod",
    "zatrat",
    "oborot",
    "komiss",
    "skidk",
    "vyplat",
    "nalog",
    "oklad",
    "kredit",
    "gonorar",
    "turnover",
    "discount",
    "commission",
)
_MONEY_TOKENS = frozenset(
    {
        "цена",
        "цены",
        "цене",
        "цену",
        "ценой",
        "чек",
        "чека",
        "tsena",
        "tseny",
        "cena",
        "summa",
        "sum",
        "total",
        "cost",
        "costs",
        "fee",
        "fees",
        "pay",
        "paid",
        "gmv",
        "arpu",
        "rub",
        "usd",
        "eur",
        # Точные формы: по префиксу попали бы «итоговый статус», «балансировщик», «сборка».
        "итог",
        "итога",
        "итоги",
        "итого",
        "баланс",
        "баланса",
        "балансе",
        "балансы",
        "балансов",
        "ндс",
        "сбор",
        "сбора",
        "сборы",
        "сборов",
        "премия",
        "премии",
        "премию",
        "премий",
        "itog",
        "itogi",
        "itogo",
        "balans",
        "balansa",
        "nds",
        "sbor",
        "sbora",
        "sbory",
        "premiya",
        "premii",
        "spend",
        "spending",
        "spent",
        "balance",
        "balances",
        "tax",
        "taxes",
        "earnings",
    }
)
_TOKEN_RE = re.compile(r"[^\W_]+")


def _has_money_words(text: str | None) -> bool:
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in _MONEY_TOKENS or token.startswith(_MONEY_TOKEN_PREFIXES):
            return True
    return False


def _asks_for_money(text: str) -> bool:
    """Есть ли показатель, который просят посчитать, а не исключают и не заменяют."""
    return any(
        not _MONEY_EXCLUDED_BEFORE_RE.search(text[: match.start()])
        and not _MONEY_EXCLUDED_AFTER_RE.match(text[match.end() :])
        for match in _MONEY_QUESTION_RE.finditer(text)
    )


def lacks_money_fields(catalog: Any, metrics: Mapping[str, Any]) -> bool:
    """True только если в каталоге и метриках нет ни одного признака денег.

    Смотрит на структуру, а не на текст промпта: в разделе «Метрики» всегда есть
    служебное упоминание выручки. Сомнение трактуется в пользу модели.
    """
    if catalog is None or not catalog.tables:
        return False
    for metric in metrics.values():
        if _has_money_words(metric.metric_id) or _has_money_words(metric.title):
            return False
    for table in catalog.tables:
        if any(
            _has_money_words(text)
            for text in (table.table_id, table.original_name, table.description)
        ):
            return False
        for column in table.columns:
            if column.semantic_type == "money" or column.currency:
                return False
            if any(
                _has_money_words(text)
                for text in (column.name, column.original_name, column.description)
            ):
                return False
    return True


def _reads_external_source(text: str) -> bool:
    if _READ_LOCATION_RE.search(text):
        return True
    for match in _READ_CALL_RE.finditer(text):
        sentence = _SENTENCE_END_RE.split(text[: match.start()])[-1]
        if not _EXPLANATION_RE.match(sentence) or _DIRECT_COMMAND_RE.search(sentence):
            return True
    return False


def _rejection(markdown: str) -> dict:
    return {
        "understanding": Understanding(intent="invalid"),
        "answer_blocks": [Block(type="text", markdown=markdown)],
    }


def _clarification(question: str, options: list[str]) -> dict:
    return {
        "understanding": Understanding(
            intent="invalid", clarification=Clarification(question=question, options=options)
        )
    }


def reversed_period_update(text: str | None) -> dict | None:
    """Обновление состояния для перепутанного периода; None, если его нет."""
    period = find_reversed_period(text or "")
    if period is None:
        return None
    return {"understanding": Understanding(intent="invalid", clarification=period.clarification())}


def guard_question(text: str, *, money_fields_absent: bool = False) -> dict | None:
    """Решение по вопросу без модели: обновление состояния графа или None."""
    text = _mask_filter_quotes(text)
    if (period := reversed_period_update(text)) is not None:
        return period
    if _reads_external_source(text):
        return _rejection(EXTERNAL_REJECTION)
    if _WRITE_COMMAND_RE.search(text) or _BYPASS_RE.search(text):
        return _rejection(WRITE_REJECTION)
    if _MUTATION_RE.search(text) and not _MUTATION_EXCEPTION_RE.search(text):
        return _clarification(MUTATION_QUESTION, MUTATION_OPTIONS)
    if money_fields_absent and _asks_for_money(text):
        return _clarification(MISSING_MONEY_QUESTION, MISSING_MONEY_OPTIONS)
    return None
