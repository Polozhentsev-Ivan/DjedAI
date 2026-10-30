"""Operator-only credential storage. No public HTTP routes for assignment or reading keys."""

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from djedai.agent.client import valid_api_key
from djedai.backend.db.models import UserModel, UserModelKeyModel
from djedai.backend.source_secrets import SourceSecrets


class UserModelKeys:
    def __init__(self, session_factory: sessionmaker[Session], secrets: SourceSecrets) -> None:
        self.session_factory = session_factory
        self.secrets = secrets

    def ciphertext(self, user_id: str | None) -> str | None:
        if user_id is None:
            return None
        try:
            with self.session_factory() as db:
                row = db.get(UserModelKeyModel, user_id)
                return row.api_key_ciphertext if row else None
        except SQLAlchemyError:
            raise HTTPException(503, "Хранилище назначенных ключей недоступно") from None

    def assign(self, user_id: str, api_key: str | None) -> None:
        if api_key is not None and (
            not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 8192
            or not valid_api_key(api_key)
        ):
            raise ValueError("Недопустимый ключ API")
        ciphertext = self.secrets.encrypt_user_model_key(user_id, api_key) if api_key else None
        try:
            with self.session_factory() as db:
                if db.get(UserModel, user_id) is None:
                    raise HTTPException(404, "Пользователь не найден")
                row = db.get(UserModelKeyModel, user_id)
                if ciphertext is None:
                    if row is not None:
                        db.delete(row)
                elif row is None:
                    db.add(UserModelKeyModel(user_id=user_id, api_key_ciphertext=ciphertext))
                else:
                    row.api_key_ciphertext = ciphertext
                db.commit()
        except SQLAlchemyError:
            raise HTTPException(503, "Хранилище назначенных ключей недоступно") from None
