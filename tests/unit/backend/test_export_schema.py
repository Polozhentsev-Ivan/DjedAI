"""Тесты скрипта экспорта схем OpenAPI и автономных JSON Schema (scripts/export_schema.py)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.export_schema import (  # noqa: E402
    REQUIRED_SCHEMAS,
    build_openapi_schema,
    build_standalone_schema,
    check_schemas,
    check_standalone_schemas,
    get_component_refs,
    main,
)


def test_build_openapi_schema_contains_paths_and_components():
    """Схема OpenAPI генерируется и содержит компоненты и пути."""
    schema = build_openapi_schema()
    assert "openapi" in schema
    assert "components" in schema
    assert "schemas" in schema["components"]
    assert "paths" in schema
    assert len(schema["components"]["schemas"]) >= len(REQUIRED_SCHEMAS)


def test_check_schemas_finds_all_required():
    """Все обязательные контракты присутствуют в схеме."""
    schema = build_openapi_schema()
    missing = check_schemas(schema)
    assert not missing, f"Missing required schemas: {missing}"


def test_check_schemas_detects_missing():
    """Функция выявления пропущенных контрактов возвращает имя отсутствующей модели."""
    dummy_schema = {"components": {"schemas": {"AssistantMessage": {}}}}
    missing = check_schemas(dummy_schema)
    assert "TextBlock" in missing
    assert "HealthCheck" in missing


def test_get_component_refs():
    """get_component_refs правильно извлекает имена ссылок на компоненты."""
    dummy = {
        "items": {"$ref": "#/components/schemas/TextBlock"},
        "mapping": {"chart": "#/components/schemas/ChartBlock"},
    }
    refs = get_component_refs(dummy)
    assert refs == {"TextBlock", "ChartBlock"}


def test_build_standalone_schema_without_dependencies():
    """Автономная схема для модели без зависимостей содержит $schema и $id."""
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]
    standalone = build_standalone_schema("HealthCheck", components)

    assert standalone["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert standalone["$id"] == "HealthCheck.json"
    assert "$defs" not in standalone


def test_build_standalone_schema_with_dependencies():
    """Автономная схема для AssistantMessage включает $defs и не содержит внешних ссылок."""
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]
    standalone = build_standalone_schema("AssistantMessage", components)

    assert standalone["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert standalone["$id"] == "AssistantMessage.json"
    assert "$defs" in standalone

    raw = json.dumps(standalone)
    # Ни одной ссылки на #/components/schemas/ не должно остаться
    leftovers = re.findall(r"#/components/schemas/[a-zA-Z0-9_]+", raw)
    assert not leftovers, f"Leftover OpenAPI refs: {leftovers}"

    # Все ссылки #/$defs/<dep> должны существовать в $defs
    defs_refs = set(re.findall(r"#/\$defs/([a-zA-Z0-9_]+)", raw))
    assert defs_refs, "Expected defs references in AssistantMessage"
    for target in defs_refs:
        assert target in standalone["$defs"], f"Missing $defs entry for {target}"

    # Проверяем, что discriminator mapping тоже переписан
    blocks_items = standalone["properties"]["blocks"]["items"]
    mapping = blocks_items.get("discriminator", {}).get("mapping", {})
    for kind, ref in mapping.items():
        assert ref.startswith("#/$defs/"), f"Mapping {kind} -> {ref} should start with #/$defs/"


def test_check_standalone_schemas_all_valid():
    """Все 63 схемы из components.schemas успешно строятся без висячих ссылок."""
    schema = build_openapi_schema()
    components = schema["components"]["schemas"]
    errors = check_standalone_schemas(components)
    assert not errors, f"Errors in standalone schemas: {errors}"


def test_cli_check_flag(capsys):
    """Флаг --check отрабатывает с кодом 0."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["export_schema.py", "--check"])
        code = main()
        assert code == 0
    captured = capsys.readouterr()
    assert "Схема проверена успешно" in captured.out


def test_cli_schemas_dir(tmp_path: Path):
    """Флаг --schemas-dir записывает все автономные файлы схем."""
    target_dir = tmp_path / "schemas"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["export_schema.py", "--schemas-dir", str(target_dir)])
        code = main()
        assert code == 0

    exported_files = list(target_dir.glob("*.json"))
    assert len(exported_files) >= len(REQUIRED_SCHEMAS)

    # Проверяем файл AssistantMessage.json
    am_file = target_dir / "AssistantMessage.json"
    assert am_file.exists()
    am_data = json.loads(am_file.read_text(encoding="utf-8"))
    assert am_data["$id"] == "AssistantMessage.json"
    assert "$defs" in am_data

    # Проверяем, что во всех файлах каталога ссылки внутридокументные и разрешимые
    for f in exported_files:
        content = json.loads(f.read_text(encoding="utf-8"))
        raw = json.dumps(content)
        assert "#/components/schemas/" not in raw, f"File {f.name} contains OpenAPI ref"
        defs_refs = set(re.findall(r"#/\$defs/([a-zA-Z0-9_]+)", raw))
        if defs_refs:
            assert "$defs" in content, f"File {f.name} has refs but no $defs"
            for ref_name in defs_refs:
                assert ref_name in content["$defs"], f"File {f.name} missing $defs/{ref_name}"


def test_cli_ts_command_flag(capsys):
    """Флаг --ts-command выводит команду генерации типов."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["export_schema.py", "--ts-command"])
        code = main()
        assert code == 0
    captured = capsys.readouterr()
    assert "openapi-typescript" in captured.out
