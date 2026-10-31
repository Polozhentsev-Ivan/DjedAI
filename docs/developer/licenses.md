# Лицензии использованных компонентов

Срез от 31.10.2026 по `uv.lock`, `frontend/package-lock.json`, `compose.yaml` и `infra/`.
Проект распространяется под лицензией [MIT](../../LICENSE), указанной в `pyproject.toml`.
Dockerfile предусматривают копирование лицензии в `/app/LICENSE` backend и
`/usr/share/licenses/djedai/LICENSE` frontend.

Проверены метаданные точных версий зависимостей, файлы лицензий NumPy/SciPy,
`certifi`/`orjson`, официальная лицензия модели и upstream расширения DuckDB.
[Инвентаризация](license-evidence-20261031/license-evidence-inventory.json) содержит
версии, лицензионные поля, хеши доступных файлов лицензий и границы проверки.
Это не полный аудит условий распространения готовых бинарников.

В Linux-бинарниках NumPy/SciPy присутствуют GCC runtime / `libgfortran` под
`GPL-3.0-or-later WITH GCC-exception-3.1` и `libquadmath` под `LGPL-2.1-or-later`.
Поэтому отсутствие GPL/LGPL во всей поставке не подтверждается. Условия
[GCC Runtime Exception](https://gcc.gnu.org/onlinedocs/libstdc++/manual/license.html)
нужно учитывать отдельно; само наличие этих компонентов не означает лицензию GPL
для всего приложения. MPL-2.0 используется у `certifi`, части `orjson` и `lightningcss`.

## Модель

| Компонент | Лицензия | Источник |
| --- | --- | --- |
| `Qwen/Qwen3.6-27B`, 27,8 млрд параметров | Apache-2.0 | [LICENSE ревизии `6a9e13b`](https://huggingface.co/Qwen/Qwen3.6-27B/blob/6a9e13bd6fc8f0983b9b99948120bc37f49c13e9/LICENSE) |

Проверенная upstream-ревизия — `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`,
27 781 427 952 параметра BF16; [метаданные](license-evidence-20261031/qwen-official-metadata.json).
Она не идентифицирует точные байты FP8-весов, обслуживаемых DeepInfra:
ревизия и хеш внешнего сервиса не установлены. `MODEL_REVISION` при локальной
поставке нужно фиксировать отдельно; это не настройка загрузки весов провайдера.

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
| Альтернативная, составная и прочие | 11 | `greenlet` (MIT и PSF-2.0), `typing-extensions` (PSF-2.0), `cffi` (MIT-0) |

`certifi` (MPL-2.0) и `orjson` (MPL-2.0 и Apache-2.0 или MIT) входят в последнюю строку.
В Linux-сборку не входят условные `tzdata` и `httpx2-jsfetch`, их лицензии не проверялись.

Таблицы выше отражают основные лицензии пакетов, а не все встроенные компоненты.
Для Linux NumPy `2.5.4` и SciPy `1.18.1` отдельно сверены тексты `LICENSE.txt` и
наличие библиотек `libgfortran`/`libquadmath`:
[бинарные компоненты и хеши](license-evidence-20261031/linux-numerical-license-evidence.json).
Проверена существующая изолированная Linux-установка этих версий, не финальный Docker-образ.

Подключение к PostgreSQL использует загружаемое расширение DuckDB `postgres`.
[Официальный upstream LICENSE](https://github.com/duckdb/duckdb-postgres/blob/main/LICENSE) — MIT.
Точная сборка загружаемого расширения и включённый `libpq` в эту проверку не вошли.

## Frontend: поставка

| Пакет | Лицензия |
| --- | --- |
| react, react-dom, react-router, @radix-ui/*, @tanstack/react-query, clsx, tailwind-merge | MIT |
| echarts | Apache-2.0 |
| zrender (зависимость echarts) | BSD-3-Clause |
| lucide-react | ISC |
| tslib | 0BSD |
| @fontsource/inter, @fontsource/jetbrains-mono | OFL-1.1 |

Всего 60 записей lock-файла, из них 52 под MIT; установленные версии всех 60 совпали
с lock. Из всего frontend-набора 422 записи инвентаризированы по lock, для 376
дополнительно сопоставлены установленные метаданные; все платформенные бинарники
и содержимое финального `dist` не проверялись.

## Инструменты разработки и сборки

Это зависимости разработки и сборки. Наличие группы dev не заменяет проверку
лицензионных уведомлений в итоговой сборке frontend.

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

- На 31.10 репозиторий приватный. MIT задаёт условия использования полученной копии,
  но не меняет видимость репозитория и не означает публикацию исходников.
- Проверенный inference остаётся облачным. T10 и #50 не закрыты лицензией проекта
  или этой инвентаризацией: локальная модель и весь состав поставки не подтверждены.
- Системные пакеты образов, notices и обязательства по исходникам встроенных
  бинарных компонентов нужно проверить на реально распространяемой сборке.
- Условия данных [HK_Datasets](https://gitverse.ru/kban/HK_Datasets): не проверялись, сами данные в репозиторий не входят.
