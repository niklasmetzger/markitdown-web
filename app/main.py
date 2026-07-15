"""FastAPI app: login, upload → job → multi-use download with sliding TTL."""
from __future__ import annotations

import asyncio
import io
import logging
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
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
from .jobs import JobStore
from .users import UserStore

logger = logging.getLogger("markitdown-web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: launch background reaper. Shutdown: cancel it cleanly."""
    settings = get_settings()
    app.state.job_store = JobStore(ttl_seconds=settings.data_retention_seconds)
    app.state.reaper_stop = asyncio.Event()

    async def reaper_loop() -> None:
        interval = settings.reaper_interval_seconds
        logger.info("reaper started: scan every %ds, ttl=%ds", interval, settings.data_retention_seconds)
        while not app.state.reaper_stop.is_set():
            try:
                purged = app.state.job_store.reap_expired()
                if purged:
                    logger.info("reaper: purged %d expired job(s)", purged)
            except Exception:
                logger.exception("reaper iteration failed")
            try:
                await asyncio.wait_for(app.state.reaper_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        logger.info("reaper stopped")

    app.state.reaper_task = asyncio.create_task(reaper_loop())
    yield
    app.state.reaper_stop.set()
    try:
        await asyncio.wait_for(app.state.reaper_task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        app.state.reaper_task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

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

    def job_store(request: Request) -> JobStore:
        return request.app.state.job_store

    def owner_of(user: dict) -> str:
        return user.get("username", "")

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
                "retention_seconds": settings.data_retention_seconds,
                "app_name": settings.app_name,
            },
        )

    # --- Job API --------------------------------------------------------------
    @app.post("/api/jobs")
    async def create_job(
        files: list[UploadFile] = File(...),
        user=Depends(get_current_user),
        settings: Settings = Depends(get_settings),
        jobs: JobStore = Depends(job_store),
    ):
        """Convert uploaded files into a server-side job. Returns job_id + summary.

        Jobs are multi-use: downloads do NOT purge them. They remain available
        until manually deleted or until the idle TTL expires (sliding by default).
        """
        if not files:
            raise HTTPException(status_code=400, detail="no files uploaded")

        successful: list[dict] = []
        errors: list[dict] = []
        for f in files:
            content = await f.read()
            if len(content) > settings.max_upload_size:
                errors.append({"filename": f.filename, "ok": False, "error": "exceeds max size"})
                continue
            try:
                md = convert_upload(f.filename or "upload.bin", content)
            except Exception as e:
                logger.exception("convert failed for %s", f.filename)
                errors.append({"filename": f.filename, "ok": False, "error": str(e)})
                continue
            successful.append({
                "filename": f.filename or "upload.bin",
                "markdown": md,
            })

        if not successful:
            return JSONResponse({"results": errors}, status_code=400)

        job = jobs.create(successful, owner=owner_of(user))
        return {
            "job_id": job.id,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "ttl_seconds": int(job.expires_at - job.created_at),
            "files": [
                {"filename": f["filename"], "size": len(f["markdown"] or "")}
                for f in job.files
            ],
            "errors": errors,
        }

    @app.get("/api/jobs")
    async def list_jobs(
        user=Depends(get_current_user),
        jobs: JobStore = Depends(job_store),
    ):
        """List the current user's active jobs, newest first."""
        return {"jobs": jobs.list_by_owner(owner_of(user))}

    @app.get("/api/jobs/{job_id}")
    async def get_job(
        job_id: str,
        user=Depends(get_current_user),
        jobs: JobStore = Depends(job_store),
    ):
        job = jobs.get(job_id)  # touches if sliding
        if not job:
            raise HTTPException(status_code=404, detail="job not found or expired")
        # Per-user isolation: don't reveal other users' jobs
        if job.owner != owner_of(user):
            raise HTTPException(status_code=404, detail="job not found")
        return {
            "job_id": job.id,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "ttl_remaining": int(job.expires_at - time.time()),
            "files": [
                {"filename": f["filename"], "size": len(f["markdown"] or "")}
                for f in job.files
            ],
        }

    @app.get("/api/jobs/{job_id}/download")
    async def download_job(
        job_id: str,
        format: str = "auto",
        user=Depends(get_current_user),
        jobs: JobStore = Depends(job_store),
    ):
        """Multi-use download. Sliding TTL is refreshed on each call.

        Auto: 1 file → .md, multiple → .zip.
        """
        job = jobs.get(job_id, touch=True)
        if not job:
            raise HTTPException(status_code=404, detail="job not found or expired")
        if job.owner != owner_of(user):
            raise HTTPException(status_code=404, detail="job not found")

        if format == "auto":
            format = "md" if len(job.files) == 1 else "zip"

        if format == "md" and len(job.files) == 1:
            f = job.files[0]
            md_name = (Path(f["filename"]).stem or "output") + ".md"
            return Response(
                content=f["markdown"] or "",
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{md_name}"'},
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in job.files:
                md_name = (Path(f["filename"]).stem or "output") + ".md"
                zf.writestr(md_name, (f["markdown"] or "").encode("utf-8"))
        buf.seek(0)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="markitdown-{stamp}.zip"'},
        )

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(
        job_id: str,
        user=Depends(get_current_user),
        jobs: JobStore = Depends(job_store),
    ):
        # Verify ownership before purging
        existing = jobs.get(job_id, touch=False)
        if not existing or existing.owner != owner_of(user):
            raise HTTPException(status_code=404, detail="job not found")
        purged = jobs.purge(job_id, reason="manual")
        return {"purged": True, "job_id": job_id}

    @app.delete("/api/jobs")
    async def delete_all_jobs(
        user=Depends(get_current_user),
        jobs: JobStore = Depends(job_store),
    ):
        """Purge all jobs owned by the current user."""
        count = jobs.purge_by_owner(owner_of(user), reason="manual_all")
        return {"purged": count}

    @app.get("/health")
    async def health(request: Request):
        return {
            "status": "ok",
            "app": get_settings().app_name,
            "jobs": request.app.state.job_store.stats(),
        }

    return app


app = create_app()
