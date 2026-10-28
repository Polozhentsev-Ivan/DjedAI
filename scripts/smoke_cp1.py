"""CSV → каталог → настоящий агент → SQL → сохранение → проверка в новом процессе.

Запуск из корня: uv run --locked python scripts/smoke_cp1.py
Полный сценарий требует кода PR #139 и DEEPINFRA_API_KEY в окружении или .env.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
import sqlglot  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from hk_datasets import DownloadError, download, load_manifest, manifest_files  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlglot import exp  # noqa: E402

from djedai.agent.client import BIModelClient, ModelResponse  # noqa: E402
from djedai.agent.config import reasoning_profile  # noqa: E402
from djedai.agent.graph import build_agent_graph  # noqa: E402
from djedai.analytics.registry import execute_tool  # noqa: E402
from djedai.backend.chat_service import AgentRuntime, stream_chat_turn  # noqa: E402
from djedai.backend.config import get_settings  # noqa: E402
from djedai.backend.db.repositories import (  # noqa: E402
    create_chat,
    create_dataset,
    get_chat_messages,
    get_dataset,
    get_or_create_user,
    load_tool_result,
)
from djedai.backend.db.session import create_db_engine  # noqa: E402
from djedai.config import LimitSettings  # noqa: E402
from djedai.contracts.catalog import DatasetCatalog  # noqa: E402
from djedai.data.catalog_builder import build_csv_catalog  # noqa: E402
from djedai.data.csv_ingest import load_raw_csv  # noqa: E402
from djedai.data.profiler import profile_table  # noqa: E402
from djedai.data.typed_views import create_typed_view  # noqa: E402

DATASET = manifest_files(load_manifest())["08_Фоновые_задания.csv"]
BASE_URL = "https://api.deepinfra.com/v1/openai"
MODEL = "Qwen/Qwen3.6-35B-A3B"


class SmokeFailure(RuntimeError):
    """Сценарий не подтвердил корректность вычисления или сохранность данных."""


class ModelTrace:
    """Сохраняет типизированные ответы API; ключ и запросы в отчёт не попадают."""

    def __init__(self, client: BIModelClient, path: Path):
        self.client = client
        self.path = path

    def generate_typed_response[T: BaseModel](
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        *,
        stage: str | None = None,
    ) -> ModelResponse[T]:
        response = self.client.generate_typed_response(
            system_prompt, user_prompt, response_model, stage=stage
        )
        record = {
            "schema": response_model.__name__,
            "stage": stage,
            "status": response.status,
            "data": response.data.model_dump(mode="json") if response.data else None,
            "error_type": response.error_type,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return response


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def download_dataset(cache: Path) -> Path:
    try:
        return download(DATASET, cache)
    except DownloadError as error:
        raise SmokeFailure(str(error)) from error


def model_client(args: argparse.Namespace) -> BIModelClient:
    values = {**dotenv_values(args.env_file), **os.environ}
    limits = LimitSettings(_env_file=args.env_file)
    key = values.get("DEEPINFRA_API_KEY") or values.get("MODEL_API_KEY")
    require(bool(key and key.strip()), "Задайте DEEPINFRA_API_KEY в окружении или .env")
    return BIModelClient(
        api_key=key,
        base_url=args.base_url,
        model_name=args.model,
        timeout=limits.model_timeout_seconds,
        max_output_tokens=limits.model_max_output_tokens,
        reasoning=reasoning_profile(limits),
        reasoning_supported=limits.model_reasoning_supported,
    )


def answer_count(markdown: str) -> int:
    """Вопрос требует ровно одно целое число; произвольное упоминание эталона не засчитываем."""
    text = markdown.strip().strip("`*_ ").replace("\u00a0", "").replace("\u202f", "")
    require(
        bool(re.fullmatch(r"\d{1,3}(?: \d{3})+|\d+", text)),
        f"Ожидался текстовый ответ только с целым числом, получено: {markdown!r}",
    )
    return int(text.replace(" ", ""))


def reads_dataset(sql: str | None, table: str) -> bool:
    """Принимает только прямой COUNT(*) всей таблицы, а не константу или число из CTE."""
    if not sql:
        return False
    statement = sqlglot.parse_one(sql, read="duckdb")
    if not isinstance(statement, exp.Select) or len(statement.expressions) != 1:
        return False
    count = statement.expressions[0].unalias()
    source = statement.args.get("from_")
    if not isinstance(count, exp.Count) or not isinstance(count.this, exp.Star) or source is None:
        return False
    reference = source.this
    return (
        isinstance(reference, exp.Table)
        and reference.name.casefold() == table.casefold()
        and reference.db in ("", "main")
        and not reference.catalog
        and not any(
            value
            for key, value in reference.args.items()
            if key not in {"this", "db", "catalog", "alias"}
        )
        and not any(
            statement.args.get(clause)
            for clause in ("with_", "joins", "where", "group", "having", "qualify", "sample")
        )
    )


def verify_saved(report: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Проверяет сохранённые сообщения, Parquet, каталог и исходные таблицы без модели."""
    engine = create_db_engine(f"sqlite:///{(directory / 'app.db').as_posix()}")
    try:
        with Session(engine) as db:
            dataset = get_dataset(db, report["dataset_id"])
            require(dataset is not None, "Датасет не найден после перезапуска")
            catalog = DatasetCatalog.model_validate(dataset.catalog)
            require(catalog.model_dump(mode="json") == report["catalog"], "Каталог изменился")
            messages = get_chat_messages(db, report["chat_id"])
            require(
                [message.role for message in messages] == ["user", "assistant"],
                "Не сохранились вопрос и ответ",
            )
            require(messages[0].content == report["question"], "Изменился сохранённый вопрос")
            assistant = messages[-1]
            require(assistant.message_id == report["message_id"], "Изменился ID ответа")
            require(assistant.result_ids == report["result_ids"], "Изменились ссылки на результаты")
            markdown = "\n".join(block.get("markdown", "") for block in assistant.blocks or [])
            require(markdown == report["answer"], "Изменился сохранённый ответ")
            require(answer_count(markdown) == report["expected_count"], "Неверное число в ответе")
            parquet_files = {path.name for path in (directory / "results").glob("*.parquet")}
            for expected in report["results"]:
                require(
                    f"{expected['result_id']}.parquet" in parquet_files,
                    "Не найден сохранённый Parquet; одного preview в SQLite недостаточно",
                )
                result = load_tool_result(
                    db, expected["result_id"], storage_dir=directory / "results"
                )
                require(result is not None, "Результат не найден после перезапуска")
                require(
                    result.model_dump(mode="json") == expected, "Сохранённый ToolResult изменился"
                )
    finally:
        engine.dispose()
    with duckdb.connect(str(directory / "data.duckdb"), read_only=True) as connection:
        table = report["table"].replace('"', '""')
        actual = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    require(actual == report["expected_count"], "Исходные данные не сохранились")
    return {"status": "passed", "messages": 2, "results": len(report["results"])}


def run_smoke(args: argparse.Namespace) -> Path:
    client = None if args.prepare_only else model_client(args)
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"status": "running", "model": args.model, "base_url": args.base_url}
    report_path = directory / "report.json"
    try:
        source = args.csv.resolve() if args.csv else download_dataset(args.cache)
        with source.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        report["source"] = {
            "file": str(source),
            "sha256": checksum,
            "url": DATASET.url if not args.csv else None,
            "revision": load_manifest()["revision"] if not args.csv else None,
        }
        print("Загрузка, типизация и профилирование CSV…", flush=True)
        dataset_id = "ds_smoke"
        with duckdb.connect(str(directory / "data.duckdb")) as connection:
            raw = load_raw_csv(connection, source, header=True)
            view = create_typed_view(connection, raw)
            profile = profile_table(connection, view)
            catalog = build_csv_catalog(connection, raw, view, profile, dataset_id=dataset_id)
        # Independent Python CSV parser, not SQL, establishes the expected answer.
        with source.open(encoding=raw.encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=raw.delimiter, quotechar=raw.quotechar)
            next(reader)
            expected_count = sum(1 for row in reader if row)
        require(expected_count > 0, "Пустой CSV не подходит для smoke-прогона")
        require(profile.row_count == expected_count, "Загрузчик потерял или добавил строки CSV")
        report.update(
            dataset_id=dataset_id,
            table=view.view_name,
            expected_count=expected_count,
            catalog=catalog.model_dump(mode="json"),
        )
        write_json(directory / "catalog.json", report["catalog"])

        database_url = f"sqlite:///{(directory / 'app.db').as_posix()}"
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(config, "head")
        os.environ["RESULTS_DATA_DIR"] = str(directory / "results")
        get_settings.cache_clear()
        engine = create_db_engine(database_url)
        try:
            with Session(engine) as db:
                user = get_or_create_user(db, "u_smoke", "smoke")
                create_dataset(db, dataset_id, user.id, source.name, catalog=report["catalog"])
                if args.prepare_only:
                    report["status"] = "prepared"
                    write_json(report_path, report)
                    return report_path
                chat = create_chat(db, "c_smoke", user.id, dataset_id)
                question = (
                    f'Сколько всего строк в таблице "{view.view_name}"? '
                    "Посчитай через run_sql, включая дубли. "
                    "Ответь только одним целым числом, без пояснений и других блоков."
                )
                report.update(chat_id=chat.chat_id, question=question)

                def load_catalog(requested_id: str) -> DatasetCatalog:
                    record = get_dataset(db, requested_id)
                    require(record is not None, "Агент запросил неизвестный датасет")
                    return DatasetCatalog.model_validate(record.catalog)

                def dispatch(name: str, params: dict[str, Any]):
                    with duckdb.connect(
                        str(directory / "data.duckdb"),
                        read_only=True,
                        config={"enable_external_access": False},
                    ) as connection:
                        return execute_tool(name, params, connection=connection)

                runtime = AgentRuntime(
                    graph=build_agent_graph(),
                    client=ModelTrace(client, directory / "model_calls.jsonl"),
                    tool_dispatcher=dispatch,
                    catalog_loader=load_catalog,
                )
                events = []
                with (directory / "events.jsonl").open("w", encoding="utf-8") as stream:
                    # This trusted CLI creates its own account and resources locally;
                    # unlike HTTP, it passes that persisted owner's ID explicitly.
                    for event in stream_chat_turn(db, chat, question, runtime, owner_id=user.id):
                        events.append(event)
                        serialized = event.model_dump_json().replace(
                            client.client.api_key, "[REDACTED]"
                        )
                        stream.write(serialized + "\n")
                        stream.flush()
                        print(f"agent: {event.event}", flush=True)
                require(
                    bool(events) and events[-1].event == "done",
                    "Агент не завершил ответ: см. events.jsonl",
                )
                require(not any(event.event == "error" for event in events), "Ошибка агента")
                assistant = get_chat_messages(db, chat.chat_id)[-1]
                require(bool(assistant.result_ids), "Агент не сохранил ни одного результата")
                results = [
                    load_tool_result(db, result_id, storage_dir=directory / "results")
                    for result_id in assistant.result_ids
                ]
                require(
                    all(result is not None for result in results), "Не найден результат инструмента"
                )
                require(
                    any(
                        result.tool == "run_sql"
                        and result.status == "ok"
                        and result.data is not None
                        and result.data.rows == [[expected_count]]
                        and reads_dataset(result.sql, view.view_name)
                        for result in results
                    ),
                    "run_sql не вернул эталонное число строк",
                )
                require(
                    all(block.get("type") == "text" for block in assistant.blocks or []),
                    "Ожидался только текстовый ответ",
                )
                answer = "\n".join(block.get("markdown", "") for block in assistant.blocks or [])
                require(
                    answer_count(answer) == expected_count, "Ответ не совпал с независимым эталоном"
                )
                report.update(
                    message_id=assistant.message_id,
                    answer=answer,
                    result_ids=assistant.result_ids,
                    results=[result.model_dump(mode="json") for result in results],
                )
        finally:
            engine.dispose()
        write_json(report_path, report)
        # A new interpreter cannot reuse the original graph, connections or in-memory data.
        child = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--verify-only", str(report_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        require(child.returncode == 0, f"Проверка после перезапуска не прошла: {child.stderr}")
        report.update(status="passed", restart_check=json.loads(child.stdout))
        write_json(report_path, report)
        return report_path
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {
            "type": type(error).__name__,
            "message": str(error) if isinstance(error, SmokeFailure) else None,
        }
        write_json(report_path, report)
        raise
    finally:
        if client is not None:
            client.client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv", type=Path, help="Локальный CSV вместо закреплённого файла GitVerse"
    )
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/hk-datasets")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "artifacts/cp1-smoke"
        / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:6]}",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument(
        "--prepare-only", action="store_true", help="Только данные и каталог; не LLM-приёмка"
    )
    parser.add_argument(
        "--verify-only", type=Path, help="Повторная проверка сохранённого report.json"
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            report_path = args.verify_only.resolve()
            report = json.loads(report_path.read_text(encoding="utf-8"))
            print(json.dumps(verify_saved(report, report_path.parent), ensure_ascii=False))
        else:
            report_path = run_smoke(args)
            print(f"Отчёт: {report_path}", flush=True)
        return 0
    except Exception as error:
        # Credentials are never printed, including SDK errors that may echo request data.
        print(
            f"Smoke failed ({type(error).__name__}): "
            f"{error if isinstance(error, SmokeFailure) else 'см. локальные артефакты прогона'}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
