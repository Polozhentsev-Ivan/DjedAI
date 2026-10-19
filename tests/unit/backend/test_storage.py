"""Тесты хранилища приложения, моделей, связей владения и миграций (§7.1, B5-2)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from djedai.backend.db.models import (
    MessageModel,
    ToolResultModel,
    UserModel,
)
from djedai.backend.db.repositories import (
    _resolve_data_ref_path,
    create_chat,
    create_dashboard,
    create_dataset,
    get_chat,
    get_chat_messages,
    get_dataset,
    get_or_create_user,
    load_tool_result,
    save_assistant_message,
    save_message,
    save_tool_result,
    write_parquet,
)
from djedai.backend.db.session import create_db_engine, init_db
from djedai.contracts.chart import ChartSpec, XAxis, YMeasure
from djedai.contracts.message import (
    AssistantMessage,
    ChartBlock,
    ClarificationBlock,
    TableBlock,
    TextBlock,
    TraceStep,
)
from djedai.contracts.tool_result import (
    ChartHint,
    Fact,
    ToolError,
    ToolResult,
    ToolResultColumn,
    ToolResultData,
)


def test_models_and_ownership_relationships() -> None:
    """Проверяет создание всех сущностей со связями владельца (users -> datasets -> ...)."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        # 1. Создаем пользователя (владельца)
        user = get_or_create_user(db, user_id="u_alice", username="alice", email="alice@corp.ai")
        assert user.id == "u_alice"
        assert user.username == "alice"

        # 2. Создаем датасет
        dataset = create_dataset(
            db,
            dataset_id="ds_sales",
            owner_id=user.id,
            name="Продажи 2026",
            source_kind="csv",
            catalog={"tables": [{"name": "sales", "columns": []}]},
        )
        assert dataset.owner_id == "u_alice"

        # 3. Создаем чат
        chat = create_chat(
            db,
            chat_id="c_101",
            owner_id=user.id,
            dataset_id=dataset.dataset_id,
            title="Анализ динамики",
        )
        assert chat.owner_id == "u_alice"
        assert chat.dataset_id == "ds_sales"

        # 4. Создаем сообщение
        msg = save_message(
            db,
            message_id="m_1",
            chat_id=chat.chat_id,
            role="user",
            content="Покажи продажи по регионам",
        )
        assert msg.chat_id == "c_101"

        # 5. Создаем дашборд
        dashboard = create_dashboard(
            db,
            dashboard_id="dash_1",
            owner_id=user.id,
            dataset_id=dataset.dataset_id,
            title="Сводка по продажам",
            widgets=[{"widget_id": "w_1", "tool_call": {"tool": "run_sql", "params": {}}}],
            source_chat_id=chat.chat_id,
        )
        assert dashboard.owner_id == "u_alice"
        assert dashboard.source_chat_id == "c_101"

        # Проверяем связи через ORM relationship
        db.refresh(user)
        assert len(user.datasets) == 1
        assert user.datasets[0].dataset_id == "ds_sales"
        assert len(user.chats) == 1
        assert user.chats[0].chat_id == "c_101"
        assert len(user.dashboards) == 1
        assert user.dashboards[0].dashboard_id == "dash_1"

        db.refresh(chat)
        assert len(chat.messages) == 1
        assert chat.messages[0].message_id == "m_1"


def test_cascade_delete_owner() -> None:
    """Проверяет каскадное удаление ресурсов при удалении пользователя-владельца."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        user = get_or_create_user(db, user_id="u_bob", username="bob")
        ds = create_dataset(db, dataset_id="ds_bob", owner_id=user.id, name="Bob Dataset")
        chat = create_chat(db, chat_id="c_bob", owner_id=user.id, dataset_id=ds.dataset_id)
        save_message(db, message_id="m_bob", chat_id=chat.chat_id, role="user", content="Hi")

        # Удаляем пользователя
        db.delete(user)
        db.commit()

        # Датасет, чат и сообщения удалены каскадом
        assert get_dataset(db, "ds_bob") is None
        assert get_chat(db, "c_bob") is None
        assert db.query(MessageModel).filter_by(message_id="m_bob").first() is None


def test_save_and_load_tool_result_ok(tmp_path: Path) -> None:
    """Проверяет сохранение и загрузку успешного ToolResult со строками и Parquet."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    res = ToolResult(
        result_id="r_ok_1",
        status="ok",
        tool="run_sql",
        params={"sql": "SELECT region, sum(revenue) FROM sales GROUP BY region"},
        sql="SELECT region, sum(revenue) FROM sales GROUP BY region",
        data=ToolResultData(
            columns=[
                ToolResultColumn(name="region", type="string", label="Регион"),
                ToolResultColumn(name="revenue", type="money", label="Выручка"),
            ],
            rows=[["Москва", 150000.5], ["Санкт-Петербург", 92000.0]],
            row_count=2,
            truncated=False,
        ),
        facts=[Fact(text="Выручка Москвы составила 150 000 руб.", values={"revenue": 150000.5})],
        chart_hint=ChartHint(type="bar", x="region", y=["revenue"]),
        duration_ms=45,
    )

    with session_factory() as db:
        record = save_tool_result(db, res, storage_dir=tmp_path)
        assert record.result_id == "r_ok_1"
        assert record.status == "ok"
        assert record.sql == res.sql
        assert record.data_ref is not None
        assert "r_ok_1.parquet" in record.data_ref

        # Загружаем обратно
        loaded = load_tool_result(db, "r_ok_1", storage_dir=tmp_path)
        assert loaded is not None
        assert loaded.result_id == "r_ok_1"
        assert loaded.status == "ok"
        assert loaded.tool == "run_sql"
        assert loaded.sql == res.sql
        assert loaded.data is not None
        assert loaded.data.columns == [
            ToolResultColumn(name="region", type="string", label="Регион"),
            ToolResultColumn(name="revenue", type="money", label="Выручка"),
        ]
        assert len(loaded.data.rows) == 2
        assert loaded.data.rows[0][0] == "Москва"
        assert loaded.data.rows[0][1] == 150000.5
        assert len(loaded.facts) == 1
        assert loaded.facts[0].values["revenue"] == 150000.5
        assert loaded.chart_hint is not None
        assert loaded.chart_hint.type == "bar"
        assert loaded.duration_ms == 45


def test_save_and_load_tool_result_error(tmp_path: Path) -> None:
    """Проверяет сохранение вызова инструмента с ошибкой (error)."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    res = ToolResult(
        result_id="r_err_1",
        status="error",
        tool="run_sql",
        params={"sql": "SELECT bad_column FROM sales"},
        sql="SELECT bad_column FROM sales",
        error=ToolError(
            code="invalid_column",
            message="Колонка 'bad_column' не найдена в таблице sales",
            suggestion="Проверьте каталог данных: доступные колонки: region, revenue",
        ),
        duration_ms=12,
    )

    with session_factory() as db:
        save_tool_result(db, res, storage_dir=tmp_path)
        loaded = load_tool_result(db, "r_err_1", storage_dir=tmp_path)

        assert loaded is not None
        assert loaded.result_id == "r_err_1"
        assert loaded.status == "error"
        assert loaded.tool == "run_sql"
        assert loaded.sql == "SELECT bad_column FROM sales"
        assert loaded.error is not None
        assert loaded.error.code == "invalid_column"
        assert "не найдена" in loaded.error.message
        assert loaded.error.suggestion is not None
        assert loaded.data is None


def test_save_and_load_tool_result_empty(tmp_path: Path) -> None:
    """Проверяет сохранение пустого результата вычислений (empty)."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    res = ToolResult(
        result_id="r_empty_1",
        status="empty",
        tool="run_sql",
        params={"sql": "SELECT * FROM sales WHERE revenue < 0"},
        sql="SELECT * FROM sales WHERE revenue < 0",
        data=ToolResultData(columns=["id", "revenue"], rows=[], row_count=0, truncated=False),
        duration_ms=8,
    )

    with session_factory() as db:
        save_tool_result(db, res, storage_dir=tmp_path)
        loaded = load_tool_result(db, "r_empty_1", storage_dir=tmp_path)

        assert loaded is not None
        assert loaded.result_id == "r_empty_1"
        assert loaded.status == "empty"
        assert loaded.tool == "run_sql"
        assert loaded.data is not None
        assert loaded.data.columns == ["id", "revenue"]
        assert len(loaded.data.rows) == 0


def test_large_data_ref_recovery_after_restart(tmp_path: Path) -> None:
    """Проверяет восстановление больших данных по data_ref после полного перезапуска приложения."""
    db_file = tmp_path / "app_restart_test.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    storage_dir = tmp_path / "parquet_storage"

    # Шаг 1: Первый запуск приложения
    engine1 = create_db_engine(db_url)
    init_db(engine1)
    session_factory1 = sessionmaker(bind=engine1)

    row_count = 5000
    rows = [[f"order_{i}", i * 10.5, i % 3 == 0] for i in range(row_count)]
    expected_columns = [
        ToolResultColumn(name="order_id", type="string", label="Номер заказа"),
        ToolResultColumn(name="amount", type="money", label="Сумма заказа"),
        ToolResultColumn(name="is_paid", type="boolean", label="Оплачен"),
    ]
    large_result = ToolResult(
        result_id="r_large_5k",
        status="ok",
        tool="run_sql",
        params={"limit": 5000},
        sql="SELECT order_id, amount, is_paid FROM orders",
        data=ToolResultData(
            columns=expected_columns,
            rows=rows,
            row_count=row_count,
            truncated=False,
        ),
        duration_ms=120,
    )

    with session_factory1() as db1:
        user = get_or_create_user(db1, user_id="u_restart", username="restart_tester")
        ds = create_dataset(db1, dataset_id="ds_restart", owner_id=user.id, name="Orders")
        chat = create_chat(db1, chat_id="c_restart", owner_id=user.id, dataset_id=ds.dataset_id)
        save_tool_result(db1, large_result, chat_id=chat.chat_id, storage_dir=storage_dir)

    # Имитируем перезапуск приложения: закрываем старый engine, создаем новый
    engine1.dispose()

    # Шаг 2: Новый запуск приложения
    engine2 = create_db_engine(db_url)
    session_factory2 = sessionmaker(bind=engine2)

    with session_factory2() as db2:
        # Проверяем сохранность связей владельца и чата
        user2 = db2.query(UserModel).filter_by(id="u_restart").first()
        assert user2 is not None
        assert len(user2.datasets) == 1
        assert len(user2.chats) == 1

        # Загружаем большой результат по data_ref
        loaded = load_tool_result(db2, "r_large_5k", storage_dir=storage_dir)
        assert loaded is not None
        assert loaded.result_id == "r_large_5k"
        assert loaded.status == "ok"
        assert loaded.data_ref is not None
        assert loaded.data is not None
        assert loaded.data.columns == expected_columns
        assert isinstance(loaded.data.columns[0], ToolResultColumn)
        assert loaded.data.columns[0].label == "Номер заказа"
        assert loaded.data.columns[1].type == "money"
        assert loaded.data.row_count == 5000
        assert loaded.data.truncated is False
        assert len(loaded.data.rows) == 5000
        assert loaded.data.rows[0] == ["order_0", 0.0, True]
        assert loaded.data.rows[4999] == ["order_4999", 4999 * 10.5, False]

    engine2.dispose()


def test_full_process_restart_recovery(tmp_path: Path) -> None:
    """Проверяет физический перезапуск приложения через отдельные процессы ОС."""
    db_file = tmp_path / "proc_restart.db"
    db_url = f"sqlite:///{db_file.as_posix()}"
    storage_dir = tmp_path / "proc_parquet"

    # Процесс 1: инициализирует БД, создает записи и результаты ok/error/empty
    write_script = f"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))
from sqlalchemy.orm import sessionmaker
from djedai.backend.db.session import create_db_engine, init_db
from djedai.backend.db.repositories import (
    get_or_create_user, create_dataset, create_chat, save_message,
    create_dashboard, save_tool_result
)
from djedai.contracts.tool_result import ToolResult, ToolResultData, ToolError

engine = create_db_engine('{db_url}')
init_db(engine)
with sessionmaker(bind=engine)() as db:
    u = get_or_create_user(db, 'u_p1', 'proc_user')
    ds = create_dataset(db, 'ds_p1', u.id, 'Proc DS')
    c = create_chat(db, 'c_p1', u.id, ds.dataset_id, 'Proc Chat')
    save_message(db, 'm_p1', c.chat_id, 'user', 'Run query')
    create_dashboard(db, 'd_p1', u.id, ds.dataset_id, 'Proc Dash')

    rows = [[f'item_{{i}}', i * 2.0] for i in range(1000)]
    r_ok = ToolResult(
        result_id='r_proc_ok',
        status='ok',
        tool='run_sql',
        sql='SELECT item, val FROM items',
        data=ToolResultData(columns=['item', 'val'], rows=rows, row_count=1000),
    )
    save_tool_result(db, r_ok, chat_id=c.chat_id, storage_dir='{storage_dir.as_posix()}')

    r_err = ToolResult(
        result_id='r_proc_err',
        status='error',
        tool='run_sql',
        sql='SELECT error',
        error=ToolError(code='bad_query', message='Bad SQL query'),
    )
    save_tool_result(db, r_err, chat_id=c.chat_id, storage_dir='{storage_dir.as_posix()}')

    r_empty = ToolResult(
        result_id='r_proc_empty',
        status='empty',
        tool='run_sql',
        sql='SELECT empty',
        data=ToolResultData(columns=['a'], rows=[]),
    )
    save_tool_result(db, r_empty, chat_id=c.chat_id, storage_dir='{storage_dir.as_posix()}')
engine.dispose()
"""
    subprocess.run([sys.executable, "-c", write_script], check=True, cwd=str(Path.cwd()))

    # Процесс 2: абсолютно новый процесс ОС, проверяющий холодный старт
    read_script = f"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))
from sqlalchemy.orm import sessionmaker
from djedai.backend.db.session import create_db_engine
from djedai.backend.db.models import UserModel
from djedai.backend.db.repositories import (
    load_tool_result, get_chat_messages, list_dashboards
)

engine = create_db_engine('{db_url}')
with sessionmaker(bind=engine)() as db:
    u = db.query(UserModel).filter_by(id='u_p1').first()
    assert u is not None
    assert u.username == 'proc_user'
    assert len(u.datasets) == 1
    assert u.datasets[0].name == 'Proc DS'
    assert len(u.chats) == 1
    assert u.chats[0].title == 'Proc Chat'

    msgs = get_chat_messages(db, 'c_p1')
    assert len(msgs) == 1
    assert msgs[0].content == 'Run query'

    dashes = list_dashboards(db, owner_id=u.id)
    assert len(dashes) == 1
    assert dashes[0].title == 'Proc Dash'

    res_ok = load_tool_result(db, 'r_proc_ok', storage_dir='{storage_dir.as_posix()}')
    assert res_ok is not None
    assert res_ok.status == 'ok'
    assert res_ok.sql == 'SELECT item, val FROM items'
    assert res_ok.data is not None
    assert res_ok.data.columns == ['item', 'val']
    assert len(res_ok.data.rows) == 1000
    assert res_ok.data.rows[999] == ['item_999', 1998.0]

    res_err = load_tool_result(db, 'r_proc_err', storage_dir='{storage_dir.as_posix()}')
    assert res_err is not None
    assert res_err.status == 'error'
    assert res_err.error.code == 'bad_query'

    res_empty = load_tool_result(db, 'r_proc_empty', storage_dir='{storage_dir.as_posix()}')
    assert res_empty is not None
    assert res_empty.status == 'empty'
    assert len(res_empty.data.rows) == 0

engine.dispose()
"""
    subprocess.run([sys.executable, "-c", read_script], check=True, cwd=str(Path.cwd()))


def test_assistant_message_blocks_persistence() -> None:
    """Проверяет сохранение и восстановление сообщений с блоками AssistantMessage."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        user = get_or_create_user(db, "u_test", "tester")
        ds = create_dataset(db, "ds_1", user.id, "DS 1")
        chat = create_chat(db, "c_blocks", user.id, ds.dataset_id)
        assert chat.chat_id == "c_blocks"

        msg = AssistantMessage(
            message_id="m_assist_1",
            chat_id="c_blocks",
            role="assistant",
            created_at=datetime.now(UTC),
            blocks=[
                TextBlock(markdown="Продажи по регионам рассчитаны успешно:"),
                TableBlock(result_id="r_ok_1", columns=["region", "revenue"], limit=10),
                ChartBlock(
                    chart=ChartSpec(
                        chart_id="ch_1",
                        result_id="r_ok_1",
                        type="bar",
                        x=XAxis(field="region"),
                        y=[YMeasure(field="revenue")],
                        title="Выручка по регионам",
                    )
                ),
                ClarificationBlock(
                    question="Хотите детализировать по месяцам?",
                    options=["Да, покажи по месяцам", "Нет, достаточно общей"],
                ),
            ],
            result_ids=["r_ok_1"],
            trace=[TraceStep(node="plan", tool="run_sql", ms=30)],
        )

        save_assistant_message(db, msg)

        # Читаем обратно
        history = get_chat_messages(db, "c_blocks")
        assert len(history) == 1
        saved = history[0]
        assert saved.message_id == "m_assist_1"
        assert saved.role == "assistant"
        assert saved.result_ids == ["r_ok_1"]
        assert len(saved.blocks) == 4
        assert saved.blocks[0]["type"] == "text"
        assert saved.blocks[1]["type"] == "table"
        assert saved.blocks[2]["type"] == "chart"
        assert saved.blocks[3]["type"] == "clarification"
        assert len(saved.trace) == 1
        assert saved.trace[0]["node"] == "plan"


def test_alembic_migrations_lifecycle(tmp_path: Path) -> None:
    """Проверяет применение миграций Alembic к пустой БД и откат (upgrade/downgrade)."""
    db_file = tmp_path / "alembic_lifecycle.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Применяем миграции к пустой БД
    command.upgrade(alembic_cfg, "head")

    # Проверяем, что таблицы созданы и в них можно писать связанные записи
    test_engine = create_db_engine(db_url)
    session_factory = sessionmaker(bind=test_engine)

    with session_factory() as db:
        user = get_or_create_user(db, "u_migr", "migr_user")
        ds = create_dataset(db, "ds_migr", user.id, "Migration DS")
        chat = create_chat(db, "c_migr", user.id, ds.dataset_id)
        msg = save_message(db, "m_migr", chat.chat_id, "user", "Test")
        dash = create_dashboard(db, "dash_migr", user.id, ds.dataset_id, "Migr Dash")
        assert user.id == "u_migr"
        assert ds.dataset_id == "ds_migr"
        assert chat.chat_id == "c_migr"
        assert msg.message_id == "m_migr"
        assert dash.dashboard_id == "dash_migr"

    test_engine.dispose()

    # 2. Откатываем миграции
    command.downgrade(alembic_cfg, "base")

    # 3. Повторно накатываем миграции
    command.upgrade(alembic_cfg, "head")


def test_data_ref_relative_directory() -> None:
    """[P1] Проверяет сохранение и загрузку при относительном каталоге RESULTS_DATA_DIR."""
    rel_dir = Path("./var/test_relative_results")
    if rel_dir.exists():
        shutil.rmtree(rel_dir, ignore_errors=True)

    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    row_count = 100
    rows = [[f"item_{i}", i * 1.5] for i in range(row_count)]
    result = ToolResult(
        result_id="r_rel_100",
        status="ok",
        tool="run_sql",
        sql="SELECT item, price FROM items",
        data=ToolResultData(
            columns=["item", "price"],
            rows=rows,
            row_count=row_count,
            truncated=False,
        ),
    )

    try:
        with session_factory() as db:
            saved = save_tool_result(db, result, storage_dir=rel_dir)
            assert saved.data_ref == "parquet://r_rel_100.parquet"

            loaded = load_tool_result(db, "r_rel_100", storage_dir=rel_dir)
            assert loaded is not None
            assert loaded.data is not None
            # Проверяем, что загружены все 100 строк из Parquet, а не 50 из preview
            assert len(loaded.data.rows) == 100
            assert loaded.data.row_count == 100
            assert loaded.data.rows[99] == ["item_99", 99 * 1.5]
    finally:
        if rel_dir.exists():
            shutil.rmtree(rel_dir, ignore_errors=True)


def test_save_tool_result_with_decimals_and_dates(tmp_path: Path) -> None:
    """[P1] Проверяет сохранение ToolResult с Decimal, date и datetime объектами в JSON."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    res = ToolResult(
        result_id="r_decimal_date",
        status="ok",
        tool="run_sql",
        params={
            "amount": Decimal("1234.56"),
            "target_date": date(2026, 10, 20),
            "nested": {"val": Decimal("42.0")},
        },
        sql="SELECT total, dt FROM ledger",
        data=ToolResultData(
            columns=["total", "dt"],
            rows=[
                [Decimal("100.50"), date(2026, 5, 1)],
                [Decimal("200.75"), datetime(2026, 5, 2, 10, 0, tzinfo=UTC)],
            ],
            row_count=2,
            truncated=False,
        ),
        facts=[
            Fact(
                text="Сумма за дату",
                values={"total": Decimal("301.25"), "created": date(2026, 5, 1)},
            )
        ],
    )

    with session_factory() as db:
        saved = save_tool_result(db, res, storage_dir=tmp_path)
        assert saved.result_id == "r_decimal_date"

        loaded = load_tool_result(db, "r_decimal_date", storage_dir=tmp_path)
        assert loaded is not None
        assert loaded.params["amount"] == 1234.56
        assert loaded.params["target_date"] == "2026-10-20"
        assert loaded.data is not None
        assert len(loaded.data.rows) == 2
        assert loaded.facts[0].values["total"] == 301.25


def test_duckdb_type_inference_mixed_ints_and_floats(tmp_path: Path) -> None:
    """[P2] Проверяет вывод типов по всей колонке (целые и дробные числа не округляются)."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    # Первая строка — int (1), вторая строка — float (2.75). DuckDB не должен округлить 2.75 в 3!
    res = ToolResult(
        result_id="r_mixed_types",
        status="ok",
        tool="run_sql",
        sql="SELECT val FROM stats",
        data=ToolResultData(
            columns=["val"],
            rows=[[1], [2.75], [3]],
            row_count=3,
            truncated=False,
        ),
    )

    with session_factory() as db:
        save_tool_result(db, res, storage_dir=tmp_path)
        loaded = load_tool_result(db, "r_mixed_types", storage_dir=tmp_path)

        assert loaded is not None
        assert loaded.data is not None
        assert len(loaded.data.rows) == 3
        assert loaded.data.rows[0] == [1.0]
        assert loaded.data.rows[1] == [2.75]
        assert loaded.data.rows[2] == [3.0]


def test_sql_identifier_and_literal_escaping(tmp_path: Path) -> None:
    """[P2] Проверяет экранирование кавычек в именах колонок и путях каталогов в DuckDB."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    weird_dir = tmp_path / "O'Neil_Storage"
    weird_dir.mkdir(parents=True, exist_ok=True)

    res = ToolResult(
        result_id="r_weird_sql",
        status="ok",
        tool="run_sql",
        sql='SELECT col"name, select FROM data',
        data=ToolResultData(
            columns=['col"name', "select"],
            rows=[["alpha", 10], ["beta", 20]],
            row_count=2,
            truncated=False,
        ),
    )

    with session_factory() as db:
        save_tool_result(db, res, storage_dir=weird_dir)
        loaded = load_tool_result(db, "r_weird_sql", storage_dir=weird_dir)

        assert loaded is not None
        assert loaded.data is not None
        assert loaded.data.columns == ['col"name', "select"]
        assert len(loaded.data.rows) == 2
        assert loaded.data.rows[0] == ["alpha", 10]


def test_metadata_restoration(tmp_path: Path) -> None:
    """[P2] Проверяет восстановление полных ToolResultColumn, row_count и truncated."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    res = ToolResult(
        result_id="r_meta_restore",
        status="ok",
        tool="run_sql",
        sql="SELECT amount, category FROM transactions",
        data=ToolResultData(
            columns=[
                ToolResultColumn(name="amount", type="money", label="Сумма операции"),
                ToolResultColumn(name="category", type="string", label="Категория"),
            ],
            rows=[[150.0, "Еда"], [300.0, "Транспорт"]],
            row_count=5000,
            truncated=True,
        ),
    )

    with session_factory() as db:
        save_tool_result(db, res, storage_dir=tmp_path)
        loaded = load_tool_result(db, "r_meta_restore", storage_dir=tmp_path)

        assert loaded is not None
        assert loaded.data is not None
        assert len(loaded.data.columns) == 2
        col0 = loaded.data.columns[0]
        assert isinstance(col0, ToolResultColumn)
        assert col0.name == "amount"
        assert col0.type == "money"
        assert col0.label == "Сумма операции"
        assert loaded.data.row_count == 5000
        assert loaded.data.truncated is True


def test_tool_result_owner_cascade_delete(tmp_path: Path) -> None:
    """[P2] Проверяет связь ToolResultModel с пользователем и каскадное удаление."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        user = get_or_create_user(db, "u_owner_test", "owner_tester")
        ds = create_dataset(db, "ds_owner", user.id, "Owner DS")
        chat = create_chat(db, "c_owner", user.id, ds.dataset_id)

        res = ToolResult(
            result_id="r_cascade_owner",
            status="ok",
            tool="run_sql",
            sql="SELECT 1",
            data=ToolResultData(columns=["col"], rows=[[1]], row_count=1, truncated=False),
        )

        saved = save_tool_result(db, res, chat_id=chat.chat_id, storage_dir=tmp_path)
        assert saved.owner_id == user.id

        db.refresh(user)
        assert len(user.results) == 1
        assert user.results[0].result_id == "r_cascade_owner"

        db.delete(user)
        db.commit()

        rem = db.query(ToolResultModel).filter_by(result_id="r_cascade_owner").first()
        assert rem is None


def test_assistant_message_preserves_created_at() -> None:
    """[P3] Проверяет сохранение и неизменность метки created_at в save_assistant_message."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    custom_ts = datetime(2026, 1, 15, 12, 34, 56, tzinfo=UTC)

    with session_factory() as db:
        user = get_or_create_user(db, "u_ts", "ts_tester")
        ds = create_dataset(db, "ds_ts", user.id, "TS DS")
        chat = create_chat(db, "c_ts", user.id, ds.dataset_id)

        msg = AssistantMessage(
            message_id="m_ts_1",
            chat_id=chat.chat_id,
            role="assistant",
            created_at=custom_ts,
            blocks=[TextBlock(markdown="Проверка метки времени")],
        )

        saved = save_assistant_message(db, msg)
        saved_ts = (
            saved.created_at.replace(tzinfo=UTC)
            if saved.created_at.tzinfo is None
            else saved.created_at
        )
        assert saved_ts == custom_ts

        history = get_chat_messages(db, chat.chat_id)
        assert len(history) == 1
        loaded_ts = (
            history[0].created_at.replace(tzinfo=UTC)
            if history[0].created_at.tzinfo is None
            else history[0].created_at
        )
        assert loaded_ts == custom_ts


def test_save_tool_result_owner_mismatch(tmp_path: Path) -> None:
    """[Security] Проверяет запрет сохранения результата с несовпадающим владельцем чата."""
    engine = create_db_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        user_alice = get_or_create_user(db, "u_alice", "alice")
        user_bob = get_or_create_user(db, "u_bob", "bob")
        ds = create_dataset(db, "ds_alice", user_alice.id, "Alice DS")
        chat = create_chat(db, "c_alice", user_alice.id, ds.dataset_id)

        res = ToolResult(
            result_id="r_mismatch",
            status="ok",
            tool="run_sql",
            sql="SELECT 1",
            data=ToolResultData(columns=["val"], rows=[[1]]),
        )

        with pytest.raises(ValueError, match="Конфликт владельцев"):
            save_tool_result(
                db,
                res,
                chat_id=chat.chat_id,
                owner_id=user_bob.id,
                storage_dir=tmp_path,
            )


def test_data_ref_path_traversal_protection(tmp_path: Path) -> None:
    """[Security] Проверяет защиту от Path Traversal и подмены файлов при разрешении data_ref."""
    safe_storage = tmp_path / "safe_results"
    safe_storage.mkdir(parents=True, exist_ok=True)

    # 1. Попытка выйти за пределы каталога через ../..
    with pytest.raises(ValueError, match="directory traversal or path separators not allowed"):
        _resolve_data_ref_path("parquet://../../etc/passwd", base_dir=safe_storage)

    # 2. Попытка замаскировать переход под имя файла ../../secret.parquet
    with pytest.raises(ValueError, match="directory traversal or path separators not allowed"):
        _resolve_data_ref_path("parquet://../../secret.parquet", base_dir=safe_storage)

    # 3. Попытка передать посторонний путь со слэшами
    with pytest.raises(ValueError, match="directory traversal or path separators not allowed"):
        _resolve_data_ref_path("parquet://sub/other.parquet", base_dir=safe_storage)

    # 4. Некорректное расширение
    with pytest.raises(ValueError, match="invalid data_ref filename"):
        _resolve_data_ref_path("parquet://malicious.exe", base_dir=safe_storage)

    # 5. Подмена файла: result_id не совпадает с data_ref
    with pytest.raises(ValueError, match="does not match expected result_id"):
        _resolve_data_ref_path(
            "parquet://r_victim.parquet",
            base_dir=safe_storage,
            expected_result_id="r_attacker",
        )

    # 6. Отклонение любой схемы, кроме parquet:// (например, file://)
    with pytest.raises(ValueError, match="unsupported data_ref scheme"):
        _resolve_data_ref_path("file://r_test.parquet", base_dir=safe_storage)


def test_atomic_parquet_write(tmp_path: Path) -> None:
    """[P1] Проверяет атомарную запись Parquet и очистку временных файлов."""
    out_file = tmp_path / "atomic_test.parquet"
    res = write_parquet(out_file, ["col"], [[1], [2]])
    assert res.exists()
    assert res.stat().st_size > 0
    # Проверяем отсутствие оставшихся временных файлов .tmp_*
    tmp_files = list(tmp_path.glob("*.tmp*"))
    assert len(tmp_files) == 0
