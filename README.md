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

| Methode | Pfad                  | Auth   | Body                              | Response |
| ------- | --------------------- | ------ | --------------------------------- | -------- |
| `POST`  | `/login`              | –      | `username`, `password` (form)     | 302 + Cookie |
| `GET`   | `/auth/oidc/login`    | –      | –                                 | 302 zu OIDC |
| `POST`  | `/api/convert`        | Bearer/Cookie | `files[]` (multipart)     | JSON: pro Datei `markdown` oder `error` |
| `POST`  | `/api/convert-zip`    | Bearer/Cookie | `files[]` (multipart)     | `.zip` mit allen `.md`s |
| `GET`   | `/health`             | –      | –                                 | `{"status":"ok"}` |

API-Auth: `Authorization: Bearer <jwt>` oder Cookie `md_session`.

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
