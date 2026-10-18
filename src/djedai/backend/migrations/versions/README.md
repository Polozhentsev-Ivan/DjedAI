# Файлы миграций базы данных

Здесь сохраняются версионированные файлы миграций Alembic (`alembic revision --autogenerate`).
Миграции применяются командой:
```sh
uv run alembic upgrade head
```
