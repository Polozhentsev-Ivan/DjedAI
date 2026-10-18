"""Тесты инициализации базы данных и сессий."""

from __future__ import annotations

from sqlalchemy import Column, Integer, String

from djedai.backend.db.base import Base
from djedai.backend.db.session import create_db_engine, get_db, init_db


class DummyModel(Base):
    __tablename__ = "dummy_test_table"
    id = Column(Integer, primary_key=True)
    name = Column(String)


def test_create_engine_and_init_db() -> None:
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)

    # Таблица создалась
    with engine.connect() as conn:
        result = conn.execute(DummyModel.__table__.select())
        assert result.fetchall() == []


def test_get_db_generator() -> None:
    gen = get_db()
    session = next(gen)
    assert session is not None
    # Корректное закрытие
    try:
        next(gen)
    except StopIteration:
        pass
