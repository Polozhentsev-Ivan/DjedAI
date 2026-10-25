"""Ответы кнопками на уточнения качества данных связываются с исходным вопросом (B2-10)."""

from __future__ import annotations

from djedai.backend.chat_service import _history_message, _with_quality_choice
from djedai.backend.db.models import MessageModel

FIRST = "orders.d1: день/месяц/год (%d/%m/%Y)"
SECOND = "orders.d2: месяц/день/год (%m/%d/%Y)"


def user(text: str) -> MessageModel:
    return MessageModel(role="user", content=text)


def clarification(*options: str) -> MessageModel:
    blocks = [{"type": "clarification", "question": "Формат?", "options": list(options)}]
    return MessageModel(role="assistant", content=None, blocks=blocks)


def answer(text: str) -> MessageModel:
    return MessageModel(role="assistant", content=None, blocks=[{"type": "text", "markdown": text}])


def chosen(messages: list[MessageModel]) -> list[str]:
    history = [_history_message(m) for m in messages]
    return [m.content for m in _with_quality_choice(messages, history)]


def test_single_choice_joins_the_question():
    messages = [user("Сколько по датам?"), clarification(FIRST, "x.y: z"), user(FIRST)]
    assert chosen(messages) == [f"Сколько по датам?\nУточнение пользователя: {FIRST}."]


def test_chain_of_choices_keeps_the_original_question():
    messages = [
        answer("Прошлый ответ"),
        user("Сколько по датам?"),
        clarification(FIRST, "orders.d1: другой"),
        user(FIRST),
        clarification(SECOND, "orders.d2: другой"),
        user(SECOND),
    ]
    assert chosen(messages) == [
        "Прошлый ответ",
        f"Сколько по датам?\nУточнение пользователя: {FIRST}.\nУточнение пользователя: {SECOND}.",
    ]


def test_new_question_after_a_choice_is_left_alone():
    messages = [
        user("Сколько по датам?"),
        clarification(FIRST, "orders.d1: другой"),
        user(FIRST),
        answer("12 дат."),
        user("А по d1 за март?"),
    ]
    assert chosen(messages)[-1] == "А по d1 за март?"


def test_text_that_is_not_an_offered_option_is_left_alone():
    messages = [user("Сколько по датам?"), clarification(FIRST, "orders.d1: другой"), user(SECOND)]
    assert chosen(messages)[-1] == SECOND


def test_question_outside_the_history_window_is_not_replaced_by_another_message():
    messages = [clarification(FIRST, "orders.d1: другой"), user(FIRST)]
    assert chosen(messages) == [f"Уточнение пользователя: {FIRST}."]
