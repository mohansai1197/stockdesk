"""Password hashing and session authentication without an external identity service."""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from database import audit


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


class StaffInput(Credentials):
    display_name: str = Field(min_length=1, max_length=80)
    role: str = Field(default="cashier", pattern=r"^(owner|manager|cashier)$")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, saved):
    salt, expected = saved.split(":")
    actual = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1
    )
    return hmac.compare_digest(actual.hex(), expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def install_auth(app, store):
    failed = defaultdict(list)
    fake_hash = hash_password(secrets.token_urlsafe(24))

    def session(db, user, response):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (token_hash(token), user["id"], csrf, time.time() + 8 * 3600),
        )
        response.set_cookie(
            "stockdesk_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=8 * 3600,
        )
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "csrf_token": csrf,
        }

    @app.get("/api/auth/status")
    def status():
        with store.transaction() as db:
            return {
                "needs_setup": db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                == 0
            }

    @app.post("/api/auth/setup", status_code=201)
    def setup(body: Credentials, response: Response):
        with store.transaction(write=True) as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise HTTPException(409, "An owner already exists. Sign in instead.")
            cursor = db.execute(
                "INSERT INTO users(username,display_name,password_hash,role) VALUES (?,?,?,'owner')",
                (body.username, body.username, hash_password(body.password)),
            )
            user = db.execute(
                "SELECT * FROM users WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            audit(db, body.username, "owner.created", body.username)
            return session(db, user, response)

    @app.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        key = request.client.host if request.client else "local"
        recent = [stamp for stamp in failed[key] if time.time() - stamp < 300]
        failed[key] = recent
        if len(recent) >= 10:
            raise HTTPException(
                429, "Too many failed attempts. Try again in five minutes."
            )
        with store.transaction(write=True) as db:
            user = db.execute(
                "SELECT * FROM users WHERE username=? AND active=1", (body.username,)
            ).fetchone()
            valid = verify_password(
                body.password, user["password_hash"] if user else fake_hash
            )
            if not user or not valid:
                failed[key].append(time.time())
                audit(db, body.username, "login.failed")
                # Commit this event before reporting the failure.
                db.commit()
                raise HTTPException(401, "Username or password is incorrect")
            failed.pop(key, None)
            audit(db, user["username"], "login.succeeded")
            return session(db, user, response)

    def require(*roles):
        def dependency(request: Request):
            with store.lease(exempt=request.url.path.endswith("/restore")):
                token = request.cookies.get("stockdesk_session", "")
                with store.transaction() as db:
                    user = db.execute(
                        "SELECT u.id,u.username,u.display_name,u.role,s.csrf_token FROM sessions s "
                        "JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
                        (token_hash(token), time.time()),
                    ).fetchone()
                if not user:
                    raise HTTPException(401, "Sign in to continue")
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if not hmac.compare_digest(
                        request.headers.get("x-csrf-token", ""), user["csrf_token"]
                    ):
                        raise HTTPException(
                            403, "Session verification failed. Refresh and try again."
                        )
                if roles and user["role"] not in roles:
                    with store.transaction(write=True) as db:
                        audit(db, user["username"], "access.denied", request.url.path)
                    raise HTTPException(
                        403, "Your staff role cannot perform this action"
                    )
                yield dict(user)

        return dependency

    from fastapi import Depends

    @app.get("/api/auth/me")
    def me(user=Depends(require())):
        return user

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, user=Depends(require())):
        with store.transaction(write=True) as db:
            db.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (token_hash(request.cookies.get("stockdesk_session", "")),),
            )
            audit(db, user["username"], "logout")
        response.delete_cookie("stockdesk_session")
        return {"ok": True}

    @app.get("/api/staff")
    def staff(user=Depends(require("owner"))):
        with store.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,username,display_name,role,active FROM users ORDER BY id"
                )
            ]

    @app.post("/api/staff", status_code=201)
    def add_staff(body: StaffInput, user=Depends(require("owner"))):
        with store.transaction(write=True) as db:
            if db.execute(
                "SELECT 1 FROM users WHERE username=?", (body.username,)
            ).fetchone():
                raise HTTPException(409, "That username is already in use")
            cursor = db.execute(
                "INSERT INTO users(username,display_name,password_hash,role) VALUES (?,?,?,?)",
                (
                    body.username,
                    body.display_name.strip(),
                    hash_password(body.password),
                    body.role,
                ),
            )
            audit(
                db,
                user["username"],
                "staff.created",
                body.username,
                {"role": body.role},
            )
            return {"id": cursor.lastrowid}

    @app.post("/api/staff/{staff_id}/deactivate")
    def deactivate(staff_id: int, user=Depends(require("owner"))):
        with store.transaction(write=True) as db:
            target = db.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (staff_id,)
            ).fetchone()
            if not target:
                raise HTTPException(404, "Active staff account not found")
            if target["id"] == user["id"]:
                raise HTTPException(409, "You cannot deactivate your own account")
            if (
                target["role"] == "owner"
                and db.execute(
                    "SELECT COUNT(*) FROM users WHERE role='owner' AND active=1"
                ).fetchone()[0]
                <= 1
            ):
                raise HTTPException(409, "Keep at least one active owner")
            db.execute("UPDATE users SET active=0 WHERE id=?", (staff_id,))
            db.execute("DELETE FROM sessions WHERE user_id=?", (staff_id,))
            audit(db, user["username"], "staff.deactivated", target["username"])
        return {"ok": True}

    return require
