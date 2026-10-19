"""Тесты хранилища приложения, моделей, связей владения и миграций (§7.1, B5-2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

from djedai.backend.db.models import (
    MessageModel,
    UserModel,
)
from djedai.backend.db.repositories import (
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
        assert loaded.data.columns == ["region", "revenue"]
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
    large_result = ToolResult(
        result_id="r_large_5k",
        status="ok",
        tool="run_sql",
        params={"limit": 5000},
        sql="SELECT order_id, amount, is_paid FROM orders",
        data=ToolResultData(
            columns=["order_id", "amount", "is_paid"],
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
        assert loaded.data.columns == ["order_id", "amount", "is_paid"]
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

    from datetime import UTC, datetime

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
