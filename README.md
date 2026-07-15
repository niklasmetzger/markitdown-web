# MarkItDown Web

Web-UI für [Microsoft MarkItDown](https://github.com/microsoft/markitdown) — Dateien (PPTX, PDF, DOCX, XLSX, Bilder, Audio, …) per Drag & Drop in Markdown konvertieren.

- **Login**: lokales Benutzer/Passwort + optional Single Sign-On via OIDC (z. B. Authentik, Keycloak, Auth0)
- **Upload**: einzelne Dateien oder mehrere auf einmal, Download als .md oder gesammelt als .zip
- **Docker**: läuft standalone oder hinter Reverse Proxy

## Schnellstart (lokal, Python)

```bash
cd webapp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Editiere .env, mindestens SECRET_KEY setzen

uvicorn app.main:app --reload --port 8000
```

Dann `http://localhost:8000` öffnen. Beim ersten Start wird der `BOOTSTRAP_USER` (default `admin` / `admin`) angelegt — **direkt danach Passwort ändern**.

## Schnellstart (Docker)

```bash
cd webapp
cp .env.example .env
# SECRET_KEY, BOOTSTRAP_PASSWORD anpassen

docker compose up -d --build
# Web-UI: http://localhost:8000
```

Daten (User-JSON, Sessions) liegen in `./data/` und werden als Volume gemountet.

## Authentik-Setup (optional)

1. Authentik starten:
   ```bash
   echo "AUTHENTIK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
   echo "AUTHENTIK_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')" >> .env
   docker compose -f docker-compose.yml -f docker-compose.authentik.yml up -d
   ```
2. In `http://localhost:9000` einloggen (Default: `akadmin` / `AUTHENTIK_SECRET_KEY` aus den Server-Logs).
3. **Provider** anlegen: *Applications → Providers → Create → OAuth2/OpenID Provider*.
   - Redirect-URI: `http://localhost:8000/auth/oidc/callback` (oder dein `BASE_URL` + Pfad)
4. **App** anlegen, Provider zuweisen. In der App-Übersicht siehst du Client-ID/Secret und Issuer.
5. In `.env` setzen:
   ```
   OIDC_ENABLED=true
   OIDC_ISSUER=https://authentik.example.com/application/o/<app-slug>/
   OIDC_CLIENT_ID=...
   OIDC_CLIENT_SECRET=...
   ```
6. `docker compose restart markitdown-web`

## API

| Methode | Pfad                                | Auth   | Body                              | Response |
| ------- | ----------------------------------- | ------ | --------------------------------- | -------- |
| `POST`  | `/login`                            | –      | `username`, `password` (form)     | 302 + Cookie |
| `GET`   | `/auth/oidc/login`                  | –      | –                                 | 302 zu OIDC |
| `POST`  | `/api/jobs`                         | Bearer/Cookie | `files[]` (multipart)     | JSON: `{job_id, files[], expires_at}` |
| `GET`   | `/api/jobs`                         | Bearer/Cookie | –                        | JSON: `{jobs: [...]}` — User-eigene aktive Jobs |
| `GET`   | `/api/jobs/{id}`                    | Bearer/Cookie | –                        | JSON: Job-Status (oder 404) |
| `GET`   | `/api/jobs/{id}/download?format=md\|zip\|auto` | Bearer/Cookie | –         | Datei-Stream (Multi-Use) |
| `DELETE`| `/api/jobs/{id}`                    | Bearer/Cookie | –                        | `{purged: true}` |
| `DELETE`| `/api/jobs`                         | Bearer/Cookie | –                        | `{purged: N}` — alle eigenen Jobs löschen |
| `GET`   | `/health`                           | –      | –                                 | `{"status":"ok","jobs":{...}}` |

API-Auth: `Authorization: Bearer <jwt>` oder Cookie `md_session`.

### Job-Lifecycle (Multi-Use mit Sliding TTL)

1. **Upload** → Server konvertiert in-memory, legt Job an, gibt `job_id` zurück.
2. **Downloads** sind **multi-use** — gleiche `job_id` kann beliebig oft abgerufen werden.
3. **TTL ist sliding**: bei jedem Download/View wird die Ablaufzeit um `DATA_RETENTION_SECONDS` nach vorn geschoben (Default: 600s = 10 min).
4. **Manuelles Löschen**: `DELETE /api/jobs/{id}` (einzeln) oder `DELETE /api/jobs` (alle eigenen).
5. **Ablauf**: Background-Reaper (alle `REAPER_INTERVAL_SECONDS`) löscht nicht-zugegriffene Jobs.

**Garantien:**
- **Keine Disk-Persistenz** — Jobs existieren nur im Prozess-RAM.
- **Per-User-Isolation** — `GET /api/jobs` listet nur eigene Jobs; fremde Job-IDs liefern 404.
- **Server-Restart** → alle Jobs weg (kein Persistenz-Layer).
- **Audit-Log** — jeder Lifecycle-Schritt: `job XYZ created/purged reason=...`.

## Konfiguration (.env)

| Variable | Default | Zweck |
| -------- | ------- | ----- |
| `SECRET_KEY` | zufällig | JWT-Signing-Key. **In Prod setzen.** |
| `BOOTSTRAP_USER` / `BOOTSTRAP_PASSWORD` | – | Wird beim ersten Start als Admin angelegt. |
| `LOCAL_AUTH_ENABLED` | `true` | Username/Passwort-Login |
| `OIDC_ENABLED` | `false` | OIDC-Login aktivieren |
| `OIDC_ISSUER` | – | Issuer-URL (z. B. Authentik-App-URL) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | – | OIDC-Credentials |
| `OIDC_AUTO_CREATE_USERS` | `true` | User aus OIDC automatisch anlegen |
| `MAX_UPLOAD_SIZE` | `104857600` | Max. Bytes pro Datei (default 100 MB) |
| `DATA_RETENTION_SECONDS` | `600` | Idle-Zeit, nach der ein Job vom Reaper gelöscht wird (Sliding: jeder Zugriff setzt zurück) |
| `SLIDING_TTL` | `true` | Wenn `true`: TTL wird bei jedem Download/View erneuert. Wenn `false`: harte Ablaufzeit ab Erstellung. |
| `REAPER_INTERVAL_SECONDS` | `60` | Wie oft der Reaper-Job nach abgelaufenen Jobs schaut |

## Projektstruktur

```
webapp/
├── app/
│   ├── main.py            # FastAPI routes
│   ├── auth.py            # JWT + OIDC
│   ├── converter.py       # markitdown-wrapper (+ .potx-Fix)
│   ├── users.py           # JSON-backed user storage
│   ├── config.py          # Settings (pydantic-settings)
│   ├── templates/         # Jinja2 (base, login, upload)
│   └── static/            # CSS
├── requirements.txt
├── Dockerfile
├── docker-compose.yml              # nur Web-App
├── docker-compose.authentik.yml    # Overlay: Authentik-Stack
├── .env.example
└── README.md
```

## Sicherheits-Hinweise

- **Reverse Proxy**: in Prod hinter nginx/Caddy/Traefik mit HTTPS terminiert. Uvicorn's `--proxy-headers` ist aktiv.
- **SECRET_KEY**: in Prod ein langer Random-String (`python -c "import secrets; print(secrets.token_urlsafe(32))"`).
- **BOOTSTRAP_PASSWORD**: nach erstem Login ändern oder User über die UI/Config löschen.
- **File-Uploads**: Inhalte werden im Speicher verarbeitet, nicht persistiert. Kein State-Loss beim Container-Neustart.
