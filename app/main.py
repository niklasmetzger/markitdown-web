"""FastAPI app: login, file upload → markdown."""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    SESSION_COOKIE,
    create_session_token,
    get_current_user,
    get_current_user_optional,
    get_oauth_client,
)
from .config import Settings, get_settings
from .converter import convert_upload
from .users import UserStore

logger = logging.getLogger("markitdown-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug)

    base = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=str(base / "templates"))

    # Bootstrap admin user on first start
    if settings.bootstrap_user and settings.bootstrap_password:
        store = UserStore(settings.users_file)
        if not store.get_by_username(settings.bootstrap_user):
            try:
                store.create_local_user(
                    settings.bootstrap_user,
                    settings.bootstrap_password,
                    role="admin",
                )
                logger.info("Bootstrap user '%s' created.", settings.bootstrap_user)
            except ValueError:
                pass

    # --- Context for templates -------------------------------------------------
    @app.middleware("http")
    async def add_user_context(request: Request, call_next):
        request.state.user = None
        response = await call_next(request)
        return response

    # --- Pages ----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, user=Depends(get_current_user_optional)):
        if user:
            return RedirectResponse(url="/upload", status_code=302)
        return RedirectResponse(url="/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(
        request: Request,
        error: Optional[str] = None,
        next: Optional[str] = "/upload",
        settings: Settings = Depends(get_settings),
        user=Depends(get_current_user_optional),
    ):
        if user:
            return RedirectResponse(url="/upload", status_code=302)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": error,
                "next": next or "/upload",
                "local_auth_enabled": settings.local_auth_enabled,
                "oidc_enabled": settings.oidc_configured,
            },
        )

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/upload"),
        settings: Settings = Depends(get_settings),
    ):
        if not settings.local_auth_enabled:
            raise HTTPException(status_code=403, detail="Local auth disabled")
        store = UserStore(settings.users_file)
        user = store.verify_password(username, password)
        if not user:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Benutzername oder Passwort falsch.",
                    "next": next,
                    "local_auth_enabled": settings.local_auth_enabled,
                    "oidc_enabled": settings.oidc_configured,
                },
                status_code=401,
            )
        store.update_last_login(user["id"])
        token = create_session_token(user["id"], user["username"], settings)
        resp = RedirectResponse(url=next or "/upload", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax",
            max_age=settings.session_ttl_hours * 3600,
        )
        return resp

    # --- OIDC ----------------------------------------------------------------
    @app.get("/auth/oidc/login")
    async def oidc_login(request: Request, settings: Settings = Depends(get_settings)):
        oauth = get_oauth_client(settings)
        if not oauth:
            raise HTTPException(status_code=404, detail="OIDC not configured")
        redirect_uri = request.url_for("oidc_callback")
        return await oauth.oidc.authorize_redirect(request, redirect_uri)

    @app.get("/auth/oidc/callback")
    async def oidc_callback(
        request: Request,
        settings: Settings = Depends(get_settings),
    ):
        oauth = get_oauth_client(settings)
        if not oauth:
            raise HTTPException(status_code=404, detail="OIDC not configured")
        try:
            token = await oauth.oidc.authorize_access_token(request)
        except Exception as e:
            logger.exception("OIDC callback failed")
            raise HTTPException(status_code=400, detail=f"OIDC error: {e}")
        claims = token.get("userinfo") or {}
        if not claims:
            # Some providers put id_token claims at the top level
            try:
                claims = await oauth.oidc.parse_id_token(request, token)
            except Exception:
                claims = {}

        sub = claims.get("sub")
        if not sub:
            raise HTTPException(status_code=400, detail="OIDC: no subject claim")

        store = UserStore(settings.users_file)
        if not settings.oidc_auto_create_users:
            user = next(
                (u for u in store.list_users() if u.get("oidc_sub") == sub or u.get("email", "").lower() == claims.get("email", "").lower()),
                None,
            )
            if not user:
                raise HTTPException(status_code=403, detail="User not provisioned. Ask admin to create your account.")
        else:
            user = store.upsert_oidc_user(
                sub=sub,
                email=claims.get("email", ""),
                name=claims.get("name", ""),
                preferred_username=claims.get("preferred_username", ""),
            )
        store.update_last_login(user["id"])

        session = create_session_token(user["id"], user["username"], settings)
        resp = RedirectResponse(url="/upload", status_code=302)
        resp.set_cookie(
            SESSION_COOKIE, session, httponly=True, samesite="lax",
            max_age=settings.session_ttl_hours * 3600,
        )
        return resp

    @app.post("/logout")
    async def logout():
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    # --- Upload & convert -----------------------------------------------------
    @app.get("/upload", response_class=HTMLResponse)
    async def upload_page(
        request: Request,
        user=Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ):
        return templates.TemplateResponse(
            "upload.html",
            {
                "request": request,
                "user": user,
                "max_upload_size": settings.max_upload_size,
                "app_name": settings.app_name,
            },
        )

    @app.post("/api/convert")
    async def api_convert(
        files: list[UploadFile] = File(...),
        user=Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ):
        if not files:
            raise HTTPException(status_code=400, detail="no files uploaded")
        results = []
        for f in files:
            content = await f.read()
            if len(content) > settings.max_upload_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"{f.filename!r} exceeds max size {settings.max_upload_size} bytes",
                )
            try:
                md = convert_upload(f.filename or "upload.bin", content)
            except Exception as e:
                logger.exception("convert failed for %s", f.filename)
                results.append({"filename": f.filename, "ok": False, "error": str(e)})
                continue
            results.append({
                "filename": f.filename,
                "ok": True,
                "markdown": md,
                "size": len(md),
            })
        # Single file → flat response; multiple → keep array
        if len(results) == 1:
            r = results[0]
            if not r["ok"]:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)
        return JSONResponse({"results": results})

    @app.post("/api/convert-zip")
    async def api_convert_zip(
        files: list[UploadFile] = File(...),
        user=Depends(get_current_user),
        settings: Settings = Depends(get_settings),
    ):
        """Convert all uploaded files and return a single .zip of .md files."""
        if not files:
            raise HTTPException(status_code=400, detail="no files uploaded")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            errors = []
            for f in files:
                content = await f.read()
                if len(content) > settings.max_upload_size:
                    errors.append(f"{f.filename}: too large")
                    continue
                try:
                    md = convert_upload(f.filename or "upload.bin", content)
                except Exception as e:
                    errors.append(f"{f.filename}: {e}")
                    continue
                md_name = (Path(f.filename or "upload").stem or "upload") + ".md"
                zf.writestr(md_name, md.encode("utf-8"))
            if errors:
                zf.writestr("_errors.txt", "\n".join(errors).encode("utf-8"))
        buf.seek(0)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="markitdown-{stamp}.zip"'},
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "app": get_settings().app_name}

    return app


app = create_app()
