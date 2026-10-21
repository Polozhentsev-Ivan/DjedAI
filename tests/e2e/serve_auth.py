"""Isolated real AUTH API plus protected transport probes; never loaded in production.

The probes exercise the session dependency, not resource ownership (AUTH-3).
No model, datasets or user-interface login forms are involved.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import Depends, File, UploadFile  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from sqlalchemy import update  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from djedai.backend.app import create_app  # noqa: E402
from djedai.backend.auth.dependencies import get_current_user, session_token  # noqa: E402
from djedai.backend.auth.storage import _token_hash  # noqa: E402
from djedai.backend.config import Settings  # noqa: E402
from djedai.backend.db.models import SessionModel, UserModel  # noqa: E402
from djedai.backend.db.session import get_db  # noqa: E402

User = Annotated[UserModel, Depends(get_current_user)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18082)
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        _env_file=None,
        app_env="test",
        cp1_local_mode=False,
        app_database_url=f"sqlite:///{directory / 'app.db'}",
        datasets_data_dir=str(directory / "datasets"),
        auth_rate_limit_requests=100,
    )
    migration = Config(str(ROOT / "alembic.ini"))
    migration.set_main_option("script_location", str(ROOT / "src/djedai/backend/migrations"))
    migration.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(migration, "head")
    app = create_app(settings)

    @app.post("/api/_auth-test/json")
    def json_probe(user: User, body: dict) -> dict:
        return {"user_id": user.id, "body": body}

    @app.post("/api/_auth-test/upload")
    async def upload_probe(user: User, file: Annotated[UploadFile, File()]) -> dict:
        return {"user_id": user.id, "name": file.filename, "body": (await file.read()).decode()}

    @app.api_route("/api/_auth-test/events", methods=["GET", "POST"])
    def stream_probe(user: User) -> StreamingResponse:
        return StreamingResponse(
            iter([f"event: done\ndata: {json.dumps({'user_id': user.id})}\n\n"]),
            media_type="text/event-stream",
        )

    @app.post("/api/_auth-test/expire")
    def expire_probe(
        user: User,
        token: Annotated[str, Depends(session_token)],
        db: Annotated[Session, Depends(get_db)],
    ) -> dict:
        now = datetime.now(UTC)
        db.execute(
            update(SessionModel)
            .where(SessionModel.token_hash == _token_hash(token))
            .values(created_at=now - timedelta(seconds=2), expires_at=now - timedelta(seconds=1))
        )
        db.commit()
        return {"user_id": user.id}

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
