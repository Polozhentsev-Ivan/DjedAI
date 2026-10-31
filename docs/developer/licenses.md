# Лицензии использованных компонентов

Срез от 31.10.2026 по `uv.lock`, `frontend/package-lock.json`, `compose.yaml` и `infra/`.
Лицензии библиотек взяты из метаданных пакетов, тексты лицензий не сверялись.
Это справка, а не юридическое заключение.

GPL, LGPL и AGPL в метаданных библиотек не найдены. Из лицензий со слабым копилефтом есть
только MPL-2.0: `certifi` и часть `orjson` в поставке, `lightningcss` при сборке frontend.

## Модель

| Компонент | Лицензия | Источник |
| --- | --- | --- |
| `Qwen/Qwen3.6-27B`, 27,8 млрд параметров | Apache-2.0 | [карточка на Hugging Face](https://huggingface.co/Qwen/Qwen3.6-27B) |

## Backend: прямые зависимости

| Пакет | Лицензия |
| --- | --- |
| alembic, argon2-cffi, charset-normalizer, duckdb, fastapi, langgraph, pydantic, pydantic-settings, sqlalchemy, sqlglot | MIT |
| pandas, python-dotenv, scipy, starlette, statsmodels, uvicorn | BSD |
| numpy | BSD-3-Clause, с компонентами под 0BSD, MIT, Zlib и CC0-1.0 |
| openai, python-multipart | Apache-2.0 |
| cryptography | Apache-2.0 или BSD-3-Clause |

Всего в поставке 72 Python-пакета, включая транзитивные:

| Лицензия | Пакетов | Примеры |
| --- | --- | --- |
| MIT | 32 | см. таблицу выше, `anyio`, `langchain-core` |
| BSD | 22 | см. таблицу выше, `click`, `httpcore` |
| Apache-2.0 | 7 | `requests`, `tenacity` |
| Двойная, составная и прочие разрешительные | 11 | `greenlet` (MIT и PSF-2.0), `typing-extensions` (PSF-2.0), `cffi` (MIT-0) |

`certifi` (MPL-2.0) и `orjson` (MPL-2.0 и Apache-2.0 или MIT) входят в последнюю строку.
В Linux-сборку не входят условные `tzdata` и `httpx2-jsfetch`, их лицензии не проверялись.

Подключение к PostgreSQL использует расширение DuckDB `postgres`, которое загружается
при работе. Его лицензия не проверялась.

## Frontend: поставка

| Пакет | Лицензия |
| --- | --- |
| react, react-dom, react-router, @radix-ui/*, @tanstack/react-query, clsx, tailwind-merge | MIT |
| echarts | Apache-2.0 |
| zrender (зависимость echarts) | BSD-3-Clause |
| lucide-react | ISC |
| tslib | 0BSD |
| @fontsource/inter, @fontsource/jetbrains-mono | OFL-1.1 |

Всего 60 записей lock-файла, из них 52 под MIT.

## Инструменты разработки и сборки

В поставку не входят.

| Группа | Лицензия |
| --- | --- |
| Python: pytest, pytest-cov, pytest-mock, pytest-xdist, ruff | MIT |
| Python: coverage | Apache-2.0 |
| Python: pygments | BSD-2-Clause |
| Frontend, 362 записи lock-файла | В основном MIT (273), Apache-2.0 (22), ISC (21) |
| Frontend, остальное | BSD, MPL-2.0 (`lightningcss`), BlueOak-1.0.0, MIT-0, CC0-1.0, CC-BY-4.0 (`caniuse-lite`), Python-2.0 (`argparse`) |

## Образы контейнеров

Лицензии основного ПО указаны по его общеизвестным условиям, в метаданных образов они не
проверялись. Системные пакеты внутри образов (Debian, Alpine) имеют собственные лицензии,
в том числе GPL; их состав не проверялся.

| Образ | Лицензия основного ПО | Где используется |
| --- | --- | --- |
| `python:3.13.16-slim-bookworm` | PSF-2.0 | backend |
| `nginx:1.30.0-alpine` | BSD-2-Clause | frontend |
| `node:24.22.0-bookworm-slim` | MIT | только сборка frontend |
| `ghcr.io/astral-sh/uv:0.13.1` | MIT или Apache-2.0 | сборка backend и запуск команд |
| `postgres:17.11-bookworm` | PostgreSQL License | только демо-профиль |
| `clickhouse/clickhouse-server:26.3.60.2` | Apache-2.0 | только демо-профиль |

## Что не определено

- Лицензия самого проекта: файла лицензии в репозитории нет.
- Условия данных [HK_Datasets](https://gitverse.ru/kban/HK_Datasets): не проверялись, сами данные в репозиторий не входят.
