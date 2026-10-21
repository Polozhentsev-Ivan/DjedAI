#!/usr/bin/env python3
"""Скрипт экспорта схемы OpenAPI и JSON Schema контрактов приложения DjedAI (§7.3, #92, #83).

Позволяет экспортировать актуальную схему API для кодогенерации TypeScript-типов
в frontend без необходимости импорта Python-модулей в браузерный код.

Использование:
    # Экспорт OpenAPI-схемы в файл:
    uv run python scripts/export_schema.py --output openapi.json

    # Экспорт автономных JSON-схем в каталог:
    uv run python scripts/export_schema.py --schemas-dir contracts-schemas/

    # Показать команду генерации TypeScript-типов:
    uv run python scripts/export_schema.py --ts-command

    # Проверить корректность генерации схем:
    uv run python scripts/export_schema.py --check
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

# Гарантируем доступность модулей из src/ при прямом вызове
ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from djedai.backend.app import create_app  # noqa: E402
from djedai.backend.config import Settings  # noqa: E402

REQUIRED_SCHEMAS = [
    "AssistantMessage",
    "MessageBlock",
    "TextBlock",
    "ChartBlock",
    "TableBlock",
    "ClarificationBlock",
    "DashboardLinkBlock",
    "SuggestionsBlock",
    "ToolResult",
    "ToolResultData",
    "ToolResultColumn",
    "ChartSpec",
    "Dashboard",
    "DashboardWidget",
    "DatasetCatalog",
    "Table",
    "Column",
    "UserRead",
    "UserCreate",
    "UserLogin",
    "TokenResponse",
    "AgentStreamEvent",
    "DatasetStreamEvent",
    "DatasetSummary",
    "HealthCheck",
    "ChatCreate",
    "ChatCreated",
    "ChatSummary",
    "ChatMessage",
    "ChatDetail",
    "SendMessageRequest",
]

TS_COMMAND_INFO = """
Воспроизводимые команды генерации TypeScript-типов для #83:

1. Экспорт OpenAPI-схемы из backend:
   uv run python scripts/export_schema.py --output openapi.json

2. Генерация TypeScript-типов через openapi-typescript (в каталоге frontend/):
   npx --yes openapi-typescript openapi.json -o frontend/src/types/api.ts

3. Альтернативно — прямая генерация из запущенного backend-сервера:
   npx --yes openapi-typescript http://localhost:8000/api/openapi.json -o frontend/src/types/api.ts
"""


def build_openapi_schema() -> dict:
    """Создает приложение и извлекает полную схему OpenAPI."""
    settings = Settings(app_env="schema-export", _env_file=None)
    app = create_app(settings)
    return app.openapi()


def check_schemas(schema: dict) -> list[str]:
    """Проверяет наличие обязательных моделей контрактов в схеме."""
    components = schema.get("components", {}).get("schemas", {})
    missing = [name for name in REQUIRED_SCHEMAS if name not in components]
    return missing


def get_component_refs(schema_part: dict) -> set[str]:
    """Извлекает имена всех схем компонентов OpenAPI (#/components/schemas/...)."""
    raw = json.dumps(schema_part)
    return set(re.findall(r"#/components/schemas/([a-zA-Z0-9_]+)", raw))


def build_standalone_schema(name: str, components: dict[str, dict]) -> dict:
    """Формирует автономную JSON Schema для одной модели с включением всех зависимостей в $defs.

    Устраняет проблему PointerToNowhere при независимой валидации моделей:
    - Собирает транзитивное замыкание всех зависимостей модели из components.schemas;
    - Добавляет их в $defs корневого документа;
    - Переписывает внутренние ссылки #/components/schemas/<dep> в #/$defs/<dep>
      (и # для циклических ссылок на саму себя);
    - Добавляет метаданные Draft 2020-12 ($schema и $id).
    """
    model_schema = components[name]

    # Сбор транзитивных зависимостей
    deps: dict[str, dict] = {}
    to_visit = list(get_component_refs(model_schema) - {name})
    visited: set[str] = set()

    while to_visit:
        dep_name = to_visit.pop()
        if dep_name in visited or dep_name not in components:
            continue
        visited.add(dep_name)
        dep_schema = copy.deepcopy(components[dep_name])
        deps[dep_name] = dep_schema
        for child_dep in get_component_refs(dep_schema):
            if child_dep != name and child_dep not in visited:
                to_visit.append(child_dep)

    result = copy.deepcopy(model_schema)
    if deps:
        result["$defs"] = {dep_name: deps[dep_name] for dep_name in sorted(deps)}

    # Переписываем ссылки OpenAPI в стандартные внутридокументные JSON Pointer
    raw = json.dumps(result, ensure_ascii=False)
    # Ссылки на саму модель -> корень документа '#'
    raw = re.sub(rf"#/components/schemas/{re.escape(name)}\b", "#", raw)
    # Ссылки на зависимости -> '#/$defs/<dep>'
    raw = re.sub(r"#/components/schemas/([a-zA-Z0-9_]+)", r"#/$defs/\1", raw)

    standalone = json.loads(raw)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{name}.json",
        **standalone,
    }


def check_standalone_schemas(components: dict[str, dict]) -> list[str]:
    """Проверяет, что все автономные схемы имеют замкнутый граф ссылок без висячих $ref."""
    errors: list[str] = []
    for name in components:
        standalone = build_standalone_schema(name, components)
        raw = json.dumps(standalone)
        # 1. Не должно остаться открытых ссылок на #/components/schemas/
        leftover_openapi = re.findall(r"#/components/schemas/([a-zA-Z0-9_]+)", raw)
        if leftover_openapi:
            errors.append(f"{name}: остались OpenAPI-ссылки: {set(leftover_openapi)}")
        # 2. Все ссылки #/$defs/<dep> должны существовать в $defs
        defs = standalone.get("$defs", {})
        defs_refs = set(re.findall(r"#/\$defs/([a-zA-Z0-9_]+)", raw))
        missing_defs = defs_refs - set(defs.keys())
        if missing_defs:
            errors.append(f"{name}: отсутствуют зависимости в $defs: {missing_defs}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Экспорт схемы OpenAPI и JSON Schema для генерации TypeScript-типов"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Путь для сохранения файла openapi.json (по умолчанию выводит в stdout)",
    )
    parser.add_argument(
        "--schemas-dir",
        type=Path,
        help="Каталог для сохранения отдельных файлов JSON Schema каждого контракта",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Проверить наличие всех обязательных контрактов и корректность автономных схем",
    )
    parser.add_argument(
        "--ts-command",
        action="store_true",
        help="Вывести воспроизводимую команду генерации TypeScript-типов для #83",
    )

    args = parser.parse_args()

    if args.ts_command:
        print(TS_COMMAND_INFO.strip())
        return 0

    schema = build_openapi_schema()
    missing = check_schemas(schema)

    if args.check:
        if missing:
            print(f"Ошибка: в схеме отсутствуют обязательные контракты: {missing}", file=sys.stderr)
            return 1
        standalone_errors = check_standalone_schemas(
            schema.get("components", {}).get("schemas", {})
        )
        if standalone_errors:
            print(
                f"Ошибка: некорректные ссылки в автономных схемах: {standalone_errors}",
                file=sys.stderr,
            )
            return 1
        schemas_count = len(schema.get("components", {}).get("schemas", {}))
        print(f"Схема проверена успешно. Всего экспортировано контрактов/схем: {schemas_count}")
        return 0

    if missing:
        print(
            f"Предупреждение: в схеме отсутствуют некоторые контракты: {missing}",
            file=sys.stderr,
        )

    if args.schemas_dir:
        args.schemas_dir.mkdir(parents=True, exist_ok=True)
        components = schema.get("components", {}).get("schemas", {})
        for name in components:
            file_path = args.schemas_dir / f"{name}.json"
            standalone = build_standalone_schema(name, components)
            file_path.write_text(
                json.dumps(standalone, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        print(
            f"Экспортировано {len(components)} автономных JSON-схем в каталог {args.schemas_dir}",
            file=sys.stderr,
        )

    formatted_json = json.dumps(schema, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(formatted_json, encoding="utf-8")
        print(f"Схема OpenAPI успешно записана в {args.output}", file=sys.stderr)
    elif not args.schemas_dir:
        print(formatted_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
