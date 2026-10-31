"""Скрипт окружения Alembic для DjedAI."""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context

from djedai.backend.config import get_settings
from djedai.backend.db import Base
from djedai.backend.db.session import create_db_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("alembic.env")

# Метаданные для автогенерации миграций
target_metadata = Base.metadata


def get_url() -> str:
    """Возвращает URL базы данных из конфигурации приложения или Alembic config."""
    cfg_url = config.get_main_option("sqlalchemy.url")
    if cfg_url:
        return cfg_url
    settings = get_settings()
    url = settings.app_database_url
    if not url:
        url = "sqlite:///./var/app.db"
    return url


def run_migrations_offline() -> None:
    """Запуск миграций в оффлайн-режиме (без подключения к СУБД)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Запуск миграций в онлайн-режиме с реальным подключением."""
    connectable = create_db_engine(get_url())

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
