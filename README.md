# MarkItDown Web

Web-UI für [Microsoft MarkItDown](https://github.com/microsoft/markitdown) — Dateien (PPTX, PDF, DOCX, XLSX, Bilder, Audio, …) per Drag & Drop in Markdown konvertieren.

- **Login**: lokales Benutzer/Passwort + optional Single Sign-On via OIDC (z. B. Authentik, Casdoor, Keycloak, Auth0)
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

## Casdoor-Setup (optional)

MarkItDown Web nutzt für OIDC authlib's OIDC-Discovery, d. h. jede standardkonforme OIDC-Implementierung funktioniert. Casdoor ist OIDC-konform — kein code-side Sonderfall nötig.

### 1. Casdoor bereitstellen

**Variante A — Selbst gehostet (Docker):**

```bash
docker run -d --name casdoor \
  -p 8000:8000 \
  -e CASDOOR_ORIGIN=http://localhost:8000 \
  casdoor/casdoor:latest
```

UI auf `http://localhost:8000`, Default-Login: `admin` / `123` (sofort ändern!).

**Variante B — Casdoor Cloud:** casdoor.com → Sign-up, Instance erstellen, Domain merken (z. B. `myorg.casdoor.com`).

### 2. Organization anlegen

In Casdoor UI: **Organizations → Add**. Name (slug) merken, z. B. `builtfirst`.

### 3. OIDC-Application anlegen

**Applications → Add → Type: OIDC.**

| Feld | Wert |
|------|------|
| Name | `markitdown-web` |
| Organization | die eben angelegte Org |
| Redirect URIs | `https://your-host/auth/oidc/callback` (bzw. `http://localhost:8000/auth/oidc/callback` für lokale Tests) |
| Token/Sign method | `HS256` (Default reicht) |

Nach dem Anlegen: **Client ID** und **Client Secret** notieren (im App-Detail sichtbar).

### 4. .env setzen

```
OIDC_ENABLED=true
OIDC_ISSUER=https://your-casdoor-host   # z. B. http://localhost:8000 oder https://myorg.casdoor.com
OIDC_CLIENT_ID=<aus Schritt 3>
OIDC_CLIENT_SECRET=<aus Schritt 3>
OIDC_ORGANIZATION=builtfirst            # Org-Name aus Schritt 2 (für Doku/Debug)
OIDC_APP_NAME=markitdown-web            # App-Name aus Schritt 3 (für Doku/Debug)
OIDC_BUTTON_LABEL="Mit Casdoor anmelden"
```

`docker compose restart markitdown-web` (bzw. lokal: `uvicorn app.main:app` neu starten).

### 5. Erster Login

Auf `/login` klickt man jetzt auf den Button „Mit Casdoor anmelden". Casdoor fragt nach Username/Passwort, redirectet zurück zu `/auth/oidc/callback`, MarkItDown legt den User automatisch an (`OIDC_AUTO_CREATE_USERS=true`) und du landest auf `/upload`.

### Casdoor-Claim-Mapping

- `sub` → wird 1:1 als `oidc_sub` in `users.json` gespeichert. Casdoor-Format ist typischerweise `org/app/<user-id>` — wir akzeptieren das verbatim.
- `preferred_username` → wird zum lokalen `username` (Fallback: E-Mail-Prefix).
- `email`, `name` → werden mitgespeichert.

## Keycloak / Auth0 / generische OIDC-Provider

Der gleiche Flow funktioniert mit jedem OIDC-konformen Provider. Wesentlich:

1. App vom Typ OIDC anlegen.
2. Redirect-URI `https://your-host/auth/oidc/callback` setzen.
3. `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` in `.env` setzen.
4. `OIDC_BUTTON_LABEL` auf einen passenden Anzeigetext ändern.

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
| `OIDC_ISSUER` | – | Issuer-URL (z. B. Authentik-App-URL, Casdoor-Host, Keycloak-Realm) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | – | OIDC-Credentials |
| `OIDC_SCOPES` | `openid profile email` | OIDC-Scopes |
| `OIDC_AUTO_CREATE_USERS` | `true` | User aus OIDC automatisch anlegen |
| `OIDC_ORGANIZATION` | – | (Casdoor) Org-Name — nur für Doku/Debug |
| `OIDC_APP_NAME` | – | (Casdoor) App-Name — nur für Doku/Debug |
| `OIDC_BUTTON_LABEL` | `Mit Single Sign-On anmelden` | Text auf dem SSO-Button |
| `MAX_UPLOAD_SIZE` | `104857600` | Max. Bytes pro Datei (default 100 MB) |
| `DATA_RETENTION_SECONDS` | `600` | Idle-Zeit, nach der ein Job vom Reaper gelöscht wird (Sliding: jeder Zugriff setzt zurück) |
| `SLIDING_TTL` | `true` | Wenn `true`: TTL wird bei jedem Download/View erneuert. Wenn `false`: harte Ablaufzeit ab Erstellung. |
| `REAPER_INTERVAL_SECONDS` | `60` | Wie oft der Reaper-Job nach abgelaufenen Jobs schaut |

## Projektstruktur

```
webapp/
├── app/
│   ├── main.py            # FastAPI routes
│   ├── auth.py            # JWT + OIDC (provider-agnostic)
│   ├── converter.py       # markitdown-wrapper (+ .potx-Fix)
│   ├── users.py           # JSON-backed user storage
│   ├── config.py          # Settings (pydantic-settings)
│   ├── templates/         # Jinja2 (base, login, upload)
│   └── static/            # CSS + Brand-Assets
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
