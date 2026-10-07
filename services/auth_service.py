from typing import Any, Dict, Optional
import hmac

import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.user import User


class AuthService:
    """认证服务"""

    @staticmethod
    def login_user(
        db: Session,
        username: str,
        password: str,
    ) -> Dict[str, Any]:
        user = AuthService.get_user_by_username(db, username)

        if not user:
            return {
                "success": False,
                "error": "用户名不存在",
            }

        password_valid, should_upgrade = AuthService.verify_password(user.password, password)
        if not password_valid:
            return {
                "success": False,
                "error": "密码错误",
            }

        if should_upgrade:
            user.password = AuthService.hash_password(password)

        if hasattr(user, "is_active") and not user.is_active:
            return {
                "success": False,
                "error": "用户已被禁用",
            }

        user.last_login_at = func.now()
        db.commit()
        db.refresh(user)

        return {
            "success": True,
            "user_id": user.id,
            "username": user.username,
            "login_time": user.last_login_at.isoformat() if user.last_login_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        }

    @staticmethod
    def get_user_by_username(db: Session, username: str) -> Optional[User]:
        return db.query(User).filter(User.username == username).first()

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    @staticmethod
    def is_hashed_password(stored_password: str) -> bool:
        return stored_password.startswith("$2a$") or stored_password.startswith("$2b$") or stored_password.startswith("$2y$")

    @staticmethod
    def verify_password(stored_password: str | None, provided_password: str) -> tuple[bool, bool]:
        """校验密码，返回 (是否有效, 是否需要升级为 bcrypt 哈希)。

        存量 NULL/空串一律拒绝（避免空串与空串相等造成的越权登录）；
        明文存量用常数时间比较，登录成功后由调用方升级为 bcrypt。
        """
        if not stored_password:
            return False, False

        if AuthService.is_hashed_password(stored_password):
            is_valid = bcrypt.checkpw(
                provided_password.encode("utf-8"),
                stored_password.encode("utf-8"),
            )
            return is_valid, False

        return hmac.compare_digest(
            stored_password.encode("utf-8"),
            provided_password.encode("utf-8"),
        ), True

    @staticmethod
    def create_user(
        db: Session,
        username: str,
        password: str,
        **kwargs,
    ) -> Dict[str, Any]:
        try:
            existing_user = AuthService.get_user_by_username(db, username)
            if existing_user:
                return {
                    "success": False,
                    "error": "用户名已存在",
                }

            new_user = User(
                username=username,
                password=AuthService.hash_password(password),
                **kwargs,
            )

            db.add(new_user)
            db.commit()
            db.refresh(new_user)

            return {
                "success": True,
                "user_id": new_user.id,
                "username": new_user.username,
            }

        except Exception as e:
            db.rollback()
            return {
                "success": False,
                "error": "创建用户失败，请稍后重试",
            }

    @staticmethod
    def get_user_id_from_session(session_data: Dict) -> Optional[int]:
        return session_data.get("user_id")

    @staticmethod
    def require_user_id(session_data: Dict) -> int:
        user_id = AuthService.get_user_id_from_session(session_data)
        if not user_id:
            raise HTTPException(status_code=401, detail="未登录")
        return user_id

    @staticmethod
    def require_request_user_id(request: Request) -> int:
        return AuthService.require_user_id(request.session)

    @staticmethod
    def get_optional_request_user_id(request: Request) -> Optional[int]:
        return request.session.get("user_id")

    @staticmethod
    def unauthorized_json_response() -> JSONResponse:
        return JSONResponse(status_code=401, content={"error": "未登录"})


def require_user(request: Request) -> int:
    """FastAPI 依赖：未登录抛 401，已登录返回 user_id。

    供各端点以 `user_id: int = Depends(require_user)` 取代
    "get_optional_request_user_id + 401 分支"的重复样板。
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user_id
