"""In-memory job store with multi-use downloads, sliding TTL, and per-user history.

Design:
  - Jobs live in process memory only. Nothing touches disk.
  - Each job is multi-use: download endpoints do NOT purge the job. The job
    stays available until manually deleted or until its TTL expires.
  - TTL is sliding by default: every read/download extends the expiration.
  - Per-user isolation: `list_by_owner` and `purge_by_owner` scope by username.
  - A background reaper removes jobs that exceeded their TTL without any access.
  - All transitions are logged for auditability.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("markitdown-web.jobs")


@dataclass
class Job:
    id: str
    files: list[dict]            # [{"filename": "...", "markdown": "..."}, ...]
    created_at: float
    last_access_at: float        # updated by get()/touch() when sliding TTL is on
    expires_at: float            # computed: last_access_at + ttl (when sliding)
    owner: str = ""              # username, for per-user scoping
    file_count: int = 0
    total_size: int = 0          # sum of markdown byte-lengths at creation


class JobStore:
    def __init__(self, ttl_seconds: int = 600, sliding_ttl: bool = True) -> None:
        self._jobs: dict[str, Job] = {}
        self._ttl = ttl_seconds
        self._sliding = sliding_ttl
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def sliding(self) -> bool:
        return self._sliding

    def create(self, files: list[dict], owner: str = "") -> Job:
        if not files:
            raise ValueError("cannot create a job with no files")
        now = time.time()
        total_size = sum(len(f.get("markdown") or "") for f in files)
        job = Job(
            id=secrets.token_urlsafe(16),
            files=files,
            created_at=now,
            last_access_at=now,
            expires_at=now + self._ttl,
            owner=owner,
            file_count=len(files),
            total_size=total_size,
        )
        with self._lock:
            self._jobs[job.id] = job
        logger.info(
            "job %s created owner=%s files=%d size=%d ttl=%ds sliding=%s",
            job.id, owner or "?", len(files), total_size, self._ttl, self._sliding,
        )
        return job

    def get(self, job_id: str, touch: bool = True) -> Optional[Job]:
        """Read a job. Returns None if missing or expired.

        If sliding TTL is enabled and `touch=True` (default), updates
        `last_access_at` and pushes `expires_at` forward by `ttl_seconds`.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            now = time.time()
            if now > job.expires_at:
                return None
            if self._sliding and touch:
                job.last_access_at = now
                job.expires_at = now + self._ttl
            return job

    def touch(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            now = time.time()
            if now > job.expires_at:
                return False
            if self._sliding:
                job.last_access_at = now
                job.expires_at = now + self._ttl
            return True

    def purge(self, job_id: str, reason: str = "manual") -> Optional[Job]:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job:
            logger.info(
                "job %s purged reason=%s owner=%s files=%d",
                job.id, reason, job.owner or "?", len(job.files),
            )
            self.release_payloads(job)
        return job

    def release_payloads(self, job: Job) -> None:
        """Help GC: drop the markdown strings. Safe to call multiple times."""
        for f in job.files:
            f["markdown"] = None  # type: ignore[assignment]

    def list_by_owner(self, owner: str) -> list[dict]:
        """List all active (non-expired) jobs owned by `owner`, newest first.

        With sliding TTL, viewing the list counts as an access and refreshes
        each job's expiry.
        """
        now = time.time()
        result: list[dict] = []
        with self._lock:
            for j in self._jobs.values():
                if j.owner != owner:
                    continue
                if now > j.expires_at:
                    continue
                if self._sliding:
                    j.last_access_at = now
                    j.expires_at = now + self._ttl
                result.append({
                    "job_id": j.id,
                    "created_at": j.created_at,
                    "last_access_at": j.last_access_at,
                    "expires_at": j.expires_at,
                    "ttl_remaining": int(j.expires_at - now),
                    "files": [
                        {"filename": f["filename"], "size": len(f.get("markdown") or "")}
                        for f in j.files
                    ],
                })
        result.sort(key=lambda x: x["created_at"], reverse=True)
        return result

    def purge_by_owner(self, owner: str, reason: str = "manual") -> int:
        purged = 0
        with self._lock:
            to_purge = [jid for jid, j in self._jobs.items() if j.owner == owner]
        for jid in to_purge:
            job = self.purge(jid, reason=reason)
            if job is not None:
                purged += 1
        return purged

    def reap_expired(self) -> int:
        now = time.time()
        with self._lock:
            expired_ids = [jid for jid, j in self._jobs.items() if now > j.expires_at]
        count = 0
        for jid in expired_ids:
            job = self.purge(jid, reason="expired")
            if job is not None:
                count += 1
        return count

    def stats(self) -> dict:
        with self._lock:
            return {
                "active": len(self._jobs),
                "ttl_seconds": self._ttl,
                "sliding_ttl": self._sliding,
            }
