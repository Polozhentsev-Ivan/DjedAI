"""Idempotent, explicit CP1 bootstrap through its public command line."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from djedai.backend.db.models import UserModel
from djedai.backend.db.session import create_db_engine, init_db

ROOT = Path(__file__).resolve().parents[3]


def test_local_bootstrap_cli_uses_configured_database_and_owner(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'owners.db').as_posix()}"
    engine = create_db_engine(url)
    init_db(engine)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "APP_DATABASE_URL": url,
            "CP1_LOCAL_MODE": "true",
            "CP1_LOCAL_USER_ID": "my-local-owner",
        }
    )
    args = [
        sys.executable,
        "-m",
        "djedai.backend.bootstrap_local_user",
        "--username",
        "my-local-name",
    ]
    try:
        for _ in range(2):
            process = subprocess.run(
                args,
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            assert "my-local-owner" in process.stdout
            assert "RuntimeWarning" not in process.stderr
        with sessionmaker(bind=engine)() as db:
            users = list(db.scalars(select(UserModel)))
            assert len(users) == 1
            assert (users[0].id, users[0].username) == ("my-local-owner", "my-local-name")
            assert users[0].password_hash is None
        environment["CP1_LOCAL_MODE"] = "false"
        disabled = subprocess.run(
            args,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert disabled.returncode == 2
        assert "CP1_LOCAL_MODE=true" in disabled.stderr
        optional = subprocess.run(
            [*args, "--if-enabled"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        assert optional.returncode == 0
        with sessionmaker(bind=engine)() as db:
            assert len(list(db.scalars(select(UserModel)))) == 1
    finally:
        engine.dispose()
