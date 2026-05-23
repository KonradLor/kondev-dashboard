"""
Dashboard backend - FastAPI aplikacija.

Endpoint'ai (atitinka 05_dashboard_ai_prompt.txt kontraktą):
    GET  /api/stats                          - CPU/RAM/Disk realtime
    GET  /api/server-info                    - hostname, IP, OS, ...
    GET  /api/services                       - servisų sąrašas su statusais
    POST /api/services/{name}/start          - paleidžia konteinerį
    GET  /api/events?limit=10                - paskutiniai įvykiai
    GET  /api/services/resource-breakdown    - CPU/RAM pasiskirstymas

Žinios apie Docker prieigą:
    Konteineris turi mount'intą /var/run/docker.sock. Jis priklauso
    docker grupei (GID 988 host'e). Dashboard vartotojas turi būti
    šios grupės narys, kitaip negalės kalbėti su Docker daemon.
"""

import hmac
import json
import logging
import os
import platform
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import docker
import httpx
import psutil
import yaml
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Response

# ============================================
# KONFIGŪRACIJA
# ============================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("dashboard")

SERVICES_CONFIG_PATH = Path(os.getenv("SERVICES_CONFIG", "/app/services.yaml"))
EVENTS_LOG_PATH = Path(os.getenv("EVENTS_LOG", "/data/events.log"))
PUBLIC_IP = os.getenv("PUBLIC_IP", "130.61.182.31")
CPU_MODEL = os.getenv("CPU_MODEL", "ARM Neoverse-N1")
HOST_HOSTNAME = os.getenv("HOST_HOSTNAME", socket.gethostname())

# Sleep mode konfigūracija
# IDLE_THRESHOLD_SECONDS - kiek laiko be aktyvumo prieš sustabdant servisą.
# IDLE_CHECK_INTERVAL_SECONDS - kaip dažnai tikrinti idle būseną.
IDLE_THRESHOLD_SECONDS = int(os.getenv("IDLE_THRESHOLD_SECONDS", "7200"))    # 2h
IDLE_CHECK_INTERVAL_SECONDS = int(os.getenv("IDLE_CHECK_INTERVAL_SECONDS", "60"))

# Stats collector - kaip dažnai gauti realtime CPU/RAM iš Docker stats API.
# Mažiau dažnai = mažiau apkrova; daugiau dažnai = "gyvesnis" grafikas.
STATS_REFRESH_INTERVAL_SECONDS = int(os.getenv("STATS_REFRESH_INTERVAL_SECONDS", "15"))

# Admin paskyros - skaitomi iš .env failo per docker-compose env_file
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "7"))

# Administratoriui rodomas vardas. SSO atveju username ateina iš Authentik
# (pvz. "akadmin"); UI'e norim rodyti draugišką vardą "Konradas".
ADMIN_DISPLAY_NAME = os.getenv("ADMIN_DISPLAY_NAME", "Konradas")

# --- OIDC (Authentik) - centrinis prisijungimas visiems vartotojams ---
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_AUTHORIZE_URL = os.getenv("OIDC_AUTHORIZE_URL", "")
OIDC_TOKEN_URL = os.getenv("OIDC_TOKEN_URL", "")
OIDC_USERINFO_URL = os.getenv("OIDC_USERINFO_URL", "")
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "")
OIDC_ADMIN_GROUP = os.getenv("OIDC_ADMIN_GROUP", "authentik Admins")
OIDC_POST_LOGOUT_URL = os.getenv("OIDC_POST_LOGOUT_URL", "/")

# --- Authentik API (FAZĖ 4 - vartotojų valdymas iš dashboard) ---
# Vidinis adresas per "web" docker tinklą; tokenas = akadmin API tokenas.
AUTHENTIK_API_URL = os.getenv("AUTHENTIK_API_URL", "http://authentik-server:9000").rstrip("/")
# Viešas Authentik domenas - laiškų nuorodoms (kad ne vidinis docker vardas)
AUTHENTIK_PUBLIC_HOST = os.getenv("AUTHENTIK_PUBLIC_HOST", "auth.kondev.app")
AUTHENTIK_API_TOKEN = os.getenv("AUTHENTIK_API_TOKEN", "")
AUTHENTIK_RECOVERY_EMAIL_STAGE = os.getenv("AUTHENTIK_RECOVERY_EMAIL_STAGE", "")
AUTHENTIK_ENROLL_EMAIL_STAGE = os.getenv("AUTHENTIK_ENROLL_EMAIL_STAGE", "")
AUTHENTIK_USERS_GROUP = os.getenv("AUTHENTIK_USERS_GROUP", "users")

# --- Vidinis service-to-service tokenas + servisų adresai (deaktyvavimo propagavimui) ---
# Kai adminas išjungia vartotoją, dashboard praneša servisams (vault, voice), kad
# išjungimas įsigaliotų IŠKART (ne tik naujiems prisijungimams). Tokenas bendras.
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
VAULT_INTERNAL_URL = os.getenv("VAULT_INTERNAL_URL", "http://konradvault-backend:8000").rstrip("/")
VOICE_INTERNAL_URL = os.getenv("VOICE_INTERNAL_URL", "http://voice-app:8000").rstrip("/")

# Spalvos servisams (resource breakdown grafikui)
SERVICE_COLORS = {
    "caddy": "#f59e0b",
    "konradvault": "#fb923c",
    "beszel": "#8b5cf6",
    "dashboard": "#ec4899",
    "minecraft": "#22c55e",
    "nextcloud": "#3b82f6",
    "gitea": "#10b981",
    "vaultwarden": "#a855f7",
    "free": "#27272a",
}


def get_color(name: str) -> str:
    return SERVICE_COLORS.get(name, "#6b7280")


# ============================================
# DOCKER KLIENTAS
# ============================================

try:
    docker_client = docker.from_env()
    docker_client.ping()
    logger.info("Docker klientas prijungtas")
except Exception as exc:
    docker_client = None
    logger.error(f"Docker klientas NEPAVYKO: {exc}")


# ============================================
# KONFIGŪRACIJOS NUSKAITYMAS
# ============================================

def load_services_config() -> list[dict[str, Any]]:
    """Nuskaito services.yaml. Failas perskaitomas per kiekvieną užklausą,
    kad pakeitimai veiktų be konteinerio restart'o."""
    if not SERVICES_CONFIG_PATH.exists():
        logger.warning(f"Services config nerastas: {SERVICES_CONFIG_PATH}")
        return []
    try:
        with SERVICES_CONFIG_PATH.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, list) else []
    except Exception as exc:
        logger.error(f"Nepavyko nuskaityti services.yaml: {exc}")
        return []


# ============================================
# ĮVYKIŲ LOGGINIMAS
# ============================================

def log_event(event_type: str, service: str, message: str) -> None:
    """Įrašo įvykį į /data/events.log (JSON eilutė)."""
    EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": int(time.time() * 1000),
        "type": event_type,
        "service": service,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with EVENTS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.error(f"Nepavyko įrašyti įvykio: {exc}")


# ============================================
# DOCKER STATUSO HELPERS
# ============================================

def container_status(name: str) -> str:
    """Grąžina konteinerio statusą - running / stopped / starting / error."""
    if not docker_client:
        return "error"
    try:
        c = docker_client.containers.get(name)
        s = c.status
        if s == "running":
            return "running"
        elif s in ("restarting", "created"):
            return "starting"
        else:
            return "stopped"
    except docker.errors.NotFound:
        return "stopped"
    except Exception as exc:
        logger.error(f"Nepavyko gauti {name} statuso: {exc}")
        return "error"


# ============================================
# SLEEP MODE - IDLE DETEKTORIUS
# ============================================
# Servisai, kuriems galima taikyti sleep mode, ir kaip patikrinti jų aktyvumą.
# Kiti servisai (caddy, dashboard, beszel) - infrastruktūra, visada veikia.
#
# Logika:
#   1) Kas IDLE_CHECK_INTERVAL_SECONDS tikrinam pasirinktus servisus.
#   2) Jei servisas neaktyvus - įsimenam, KADA jis tapo neaktyvus.
#   3) Po IDLE_THRESHOLD_SECONDS sustabdom konteinerį.
#   4) Vartotojas dashboard'e mato "Miega" + "Paleisti" mygtuką.
#   5) Paspaudus "Paleisti" - per /api/services/{name}/start vėl paleidžiama.

# Tracking state - kada kuris servisas pirmą kartą tapo idle
idle_since: dict[str, datetime] = {}
_idle_lock = threading.Lock()


def check_minecraft_idle() -> Optional[bool]:
    """
    Grąžina:
        True   - Minecraft veikia, bet 0 žaidėjų (idle)
        False  - Minecraft veikia, yra žaidėjų (aktyvus)
        None   - Negalim patikrinti (konteineris ne veikia, rcon dar nepasiruošęs, ar t.t.)
    """
    if not docker_client:
        return None
    try:
        container = docker_client.containers.get("minecraft")
        if container.status != "running":
            return None
        exec_result = container.exec_run("rcon-cli list", demux=False)
        if exec_result.exit_code != 0:
            return None
        output = exec_result.output.decode("utf-8", errors="replace")
        # Pavyzdys: "There are 0 of a max of 4 players online:"
        match = re.search(r"There are (\d+) of", output)
        if match:
            return int(match.group(1)) == 0
    except Exception as exc:
        logger.debug(f"Minecraft idle check failed: {exc}")
    return None


# Sleep-eligible servisai: vardas -> idle check funkcija
SLEEP_ELIGIBLE: dict[str, Any] = {
    "minecraft": check_minecraft_idle,
}


def idle_monitor_tick() -> None:
    """Vienas idle patikrinimo ciklas. Iškviečiamas iš background thread'o."""
    now = datetime.now(timezone.utc)
    for svc_name, check_fn in SLEEP_ELIGIBLE.items():
        idle_result = check_fn()
        if idle_result is None:
            # Negalim patikrinti - praleidžiam šitą ciklą
            continue

        with _idle_lock:
            if idle_result:  # IS IDLE
                if svc_name not in idle_since:
                    idle_since[svc_name] = now
                    logger.info(f"Sleep monitor: {svc_name} tapo idle")
                else:
                    elapsed = (now - idle_since[svc_name]).total_seconds()
                    if elapsed >= IDLE_THRESHOLD_SECONDS:
                        # Stabdom!
                        logger.info(f"Sleep monitor: {svc_name} idle {elapsed:.0f}s, stabdom")
                        try:
                            container = docker_client.containers.get(svc_name)
                            container.stop(timeout=30)
                            log_event(
                                "service_sleep",
                                svc_name,
                                f"{svc_name} užmigo (neaktyvus {int(elapsed/3600)}h)",
                            )
                            del idle_since[svc_name]
                        except Exception as exc:
                            logger.error(f"Klaida stabdant {svc_name}: {exc}")
            else:  # NOT IDLE
                if svc_name in idle_since:
                    logger.info(f"Sleep monitor: {svc_name} vėl aktyvus")
                    del idle_since[svc_name]


def idle_monitor_thread() -> None:
    """Background thread - paleidžiamas modulio inicializacijos metu."""
    logger.info(
        f"Sleep monitor pradėtas "
        f"(threshold={IDLE_THRESHOLD_SECONDS}s, interval={IDLE_CHECK_INTERVAL_SECONDS}s)"
    )
    # Pirmas patikrinimas - po vieno intervalo (kad konteineris turėtų laiko paleisti)
    while True:
        time.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        try:
            idle_monitor_tick()
        except Exception as exc:
            logger.error(f"Sleep monitor klaida: {exc}", exc_info=True)


# Paleidžiam thread'ą modulio inicializacijos metu (daemon=True - mirs su procesu)
if docker_client is not None:
    threading.Thread(target=idle_monitor_thread, daemon=True).start()


# ============================================
# REALTIME STATS CACHE
# ============================================
# Background thread'as periodiškai paima docker stats kiekvienam konteineriui
# ir kešuoja rezultatus. /api/services/resource-breakdown skaito iš kešo.
#
# Kodėl kešas: docker stats() yra lėtas (~1-2s per konteinerį, nes daemon'as
# turi apskaičiuoti delta tarp dviejų matavimų). Pollinant kas 15s ir gaunant
# stats lygiagrečiai - apkrova maža, o duomenys "pakankamai šviesūs".

container_stats_cache: dict[str, dict[str, float]] = {}
_stats_lock = threading.Lock()


def _compute_cpu_percent(stats: dict) -> float:
    """Apskaičiuoja CPU% iš docker stats raw JSON.
    Grąžina vertę 0-400% (4 CPU mašinai), kur 100% = vienas pilnas CPU."""
    try:
        cpu_delta = (
            stats["cpu_stats"]["cpu_usage"]["total_usage"]
            - stats["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        system_delta = (
            stats["cpu_stats"]["system_cpu_usage"]
            - stats["precpu_stats"]["system_cpu_usage"]
        )
        # online_cpus naujesnėse Docker versijose; senesnėse - percpu_usage masyvo ilgis
        num_cpus = stats["cpu_stats"].get("online_cpus")
        if num_cpus is None:
            num_cpus = len(stats["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])) or 1
        if system_delta > 0 and cpu_delta > 0:
            return (cpu_delta / system_delta) * num_cpus * 100.0
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return 0.0


def _fetch_one_stats(svc_name: str, container_name: str) -> tuple[str, dict | None]:
    """Vienam konteineriui paima stats. Vykdoma lygiagrečiai."""
    try:
        c = docker_client.containers.get(container_name)
        if c.status != "running":
            return svc_name, None
        raw = c.stats(stream=False)
        return svc_name, {
            "cpu_percent": _compute_cpu_percent(raw),
            "mem_bytes": float(raw.get("memory_stats", {}).get("usage", 0)),
        }
    except Exception as exc:
        logger.debug(f"Stats fetch failed for {container_name}: {exc}")
        return svc_name, None


def _refresh_stats_cache() -> None:
    """Atnaujina kešą - paima stats VISIEMS servisams lygiagrečiai."""
    from concurrent.futures import ThreadPoolExecutor

    services = load_services_config()
    if not services:
        return

    workers = max(1, len(services))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(_fetch_one_stats, cfg["name"], cfg["container"])
            for cfg in services
        ]
        results = [f.result() for f in futures]

    new_cache: dict[str, dict[str, float]] = {}
    for svc_name, data in results:
        if data is not None:
            new_cache[svc_name] = data

    with _stats_lock:
        container_stats_cache.clear()
        container_stats_cache.update(new_cache)


def stats_collector_thread() -> None:
    """Background thread - kas STATS_REFRESH_INTERVAL_SECONDS atnaujina kešą."""
    logger.info(f"Stats collector pradėtas (interval={STATS_REFRESH_INTERVAL_SECONDS}s)")
    # Iškart paimam pirmus stats (kitaip pirmas API call grąžins fallback)
    try:
        _refresh_stats_cache()
    except Exception as exc:
        logger.error(f"Pirmas stats refresh nepavyko: {exc}")
    while True:
        time.sleep(STATS_REFRESH_INTERVAL_SECONDS)
        try:
            _refresh_stats_cache()
        except Exception as exc:
            logger.error(f"Stats refresh klaida: {exc}")


# Paleidžiam stats collector'ių taip pat
if docker_client is not None:
    threading.Thread(target=stats_collector_thread, daemon=True).start()


# ============================================
# AUTENTIFIKACIJA - SESIJOS
# ============================================
# Paprasta sesijos schema:
#   - POST /api/login su {username, password} -> grąžina session cookie
#   - Cookie 'session' nukreipia į SESSIONS dict'ą
#   - Atminties sesijos: dashboard'o restart'as išvalo. Admin tada re-logins.
#   - hmac.compare_digest - apsauga nuo timing attack'ų

SESSIONS: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _verify_credentials(username: str, password: str) -> bool:
    """Saugus credentials patikrinimas (constant-time compare)."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return False
    user_ok = hmac.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
    return user_ok and pass_ok


def _create_session(username: str, is_admin: bool = True, display_name: Optional[str] = None) -> str:
    """Sukuria sesijos token'ą, įdeda į SESSIONS, grąžina token.
    is_admin: ar vartotojas administratorius (OIDC nustato pagal grupę).
    Senas /sso/login ir /api/login - adminai (default True)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with _sessions_lock:
        SESSIONS[token] = {
            "user": username,
            "is_admin": is_admin,
            "display_name": display_name or username,
            "expires": expires,
        }
    return token


def _drop_session(token: str) -> None:
    with _sessions_lock:
        SESSIONS.pop(token, None)


def current_session_info(
    session: Optional[str] = Cookie(default=None),
    x_authentik_username: Optional[str] = Header(default=None),
) -> Optional[dict]:
    """FastAPI dependency: grąžina sesijos info dict {user, is_admin, display_name}
    arba None (neprisijungęs).

    Autentifikacijos būdai:
      1. Sesijos cookie (OIDC login arba senas .env login). is_admin saugomas sesijoje.
      2. SSO forward-auth header X-Authentik-Username (senas admin kelias) - adminas.
         (Caddy forward-auth kondev-sso apribotas TIK 'authentik Admins' grupei.)
    """
    # 1. Sesijos cookie
    if session:
        with _sessions_lock:
            info = SESSIONS.get(session)
            if info:
                if info["expires"] < datetime.now(timezone.utc):
                    SESSIONS.pop(session, None)
                else:
                    return info
    # 2. Forward-auth header (senas admin kelias; tik adminai praeina policy)
    if x_authentik_username:
        return {"user": x_authentik_username, "is_admin": True,
                "display_name": ADMIN_DISPLAY_NAME}
    return None


def get_current_user(s: Optional[dict] = Depends(current_session_info)) -> Optional[str]:
    """Grąžina vartotojo vardą arba None."""
    return s["user"] if s else None


def require_admin(s: Optional[dict] = Depends(current_session_info)) -> str:
    """FastAPI dependency: grąžina admin vartotoją arba 403.
    SVARBU: tikrina is_admin (ne tik 'prisijungęs') - paprasti vartotojai NEpraeina."""
    if not s:
        raise HTTPException(status_code=401, detail="prisijungimas reikalingas")
    if not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="administratoriaus teisės reikalingos")
    return s["user"]


# ============================================
# FASTAPI APLIKACIJA
# ============================================

app = FastAPI(
    title="Dashboard API",
    description="Backend serverio valdymo skydeliui",
    version="0.1.0",
)


# ---- /sso/login (admin SSO per Google) -----------------------------------
# Šis kelias Caddy'je apsaugotas forward-auth. Kai admin spaudžia
# "Login with Google", patenka čia su X-Authentik-Username header'iu.
# Sukuriam admin sesiją ir nukreipiam atgal į dashboard'ą.

@app.get("/sso/login")
def sso_login(x_authentik_username: Optional[str] = Header(default=None)):
    from fastapi.responses import RedirectResponse
    if not x_authentik_username:
        # Neturėtų nutikti (Caddy forward-auth garantuoja), bet saugumui
        raise HTTPException(status_code=401, detail="SSO header trūksta")
    token = _create_session(x_authentik_username)
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        path="/",
    )
    return resp


# ---- /api/login ----------------------------------------------------------

@app.post("/api/login")
def login(credentials: dict, response: Response):
    """Prisijungimas. Grąžina cookie 'session' su token'u."""
    username = str(credentials.get("username", ""))
    password = str(credentials.get("password", ""))
    if not _verify_credentials(username, password):
        raise HTTPException(status_code=401, detail="neteisingi duomenys")
    token = _create_session(username)
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        path="/",
    )
    logger.info(f"Login: {username}")
    return {"ok": True, "user": username}


# ---- OIDC (Authentik) centrinis prisijungimas ----------------------------
# /auth/login  -> nukreipia į Authentik authorize
# /auth/callback -> code -> token -> userinfo -> sesija (is_admin pagal grupę)
# /auth/logout -> išvalo sesiją

@app.get("/auth/login")
def auth_login():
    from fastapi.responses import RedirectResponse
    import urllib.parse
    if not OIDC_CLIENT_ID or not OIDC_AUTHORIZE_URL:
        raise HTTPException(status_code=500, detail="OIDC nesukonfigūruotas")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    url = OIDC_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    resp = RedirectResponse(url=url, status_code=302)
    # State saugomas cookie (CSRF apsauga) - palyginsim callback'e
    resp.set_cookie("oidc_state", state, httponly=True, secure=True,
                    samesite="lax", max_age=600, path="/")
    return resp


@app.get("/auth/callback")
def auth_callback(
    code: str = "",
    state: str = "",
    oidc_state: Optional[str] = Cookie(default=None),
):
    from fastapi.responses import RedirectResponse
    if not code or not state or state != oidc_state:
        raise HTTPException(status_code=400, detail="OIDC state/code klaida")
    try:
        with httpx.Client(timeout=15) as client:
            tok = client.post(OIDC_TOKEN_URL, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OIDC_REDIRECT_URI,
                "client_id": OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
            })
            if tok.status_code != 200:
                logger.error(f"OIDC token klaida: {tok.status_code} {tok.text[:200]}")
                raise HTTPException(status_code=502, detail="OIDC token mainai nepavyko")
            access_token = tok.json().get("access_token")
            ui = client.get(OIDC_USERINFO_URL,
                            headers={"Authorization": f"Bearer {access_token}"})
            if ui.status_code != 200:
                raise HTTPException(status_code=502, detail="OIDC userinfo nepavyko")
            info = ui.json()
    except httpx.HTTPError as exc:
        logger.error(f"OIDC tinklo klaida: {exc}")
        raise HTTPException(status_code=502, detail="OIDC serveris nepasiekiamas")

    username = info.get("preferred_username") or info.get("email") or info.get("sub")
    groups = info.get("groups", []) or []
    is_admin = OIDC_ADMIN_GROUP in groups
    display = ADMIN_DISPLAY_NAME if is_admin else (info.get("name") or username)

    token = _create_session(username, is_admin=is_admin, display_name=display)
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie("session", token, httponly=True, secure=True,
                    samesite="lax", max_age=SESSION_TTL_DAYS * 24 * 3600, path="/")
    resp.delete_cookie("oidc_state", path="/")
    logger.info(f"OIDC login: {username} (admin={is_admin})")
    return resp


@app.get("/auth/logout")
def auth_logout(session: Optional[str] = Cookie(default=None)):
    from fastapi.responses import RedirectResponse
    if session:
        _drop_session(session)
    resp = RedirectResponse(url=OIDC_POST_LOGOUT_URL, status_code=302)
    resp.delete_cookie("session", path="/")
    return resp


# ---- /api/logout ---------------------------------------------------------

@app.post("/api/logout")
def logout(response: Response, session: Optional[str] = Cookie(default=None)):
    """Atsijungimas - panaikina sesiją ir cookie."""
    if session:
        _drop_session(session)
    response.delete_cookie(key="session", path="/")
    return {"ok": True}


# ---- /api/me -------------------------------------------------------------

@app.get("/api/me")
def me(s: Optional[dict] = Depends(current_session_info)):
    """Grąžina dabartinio vartotojo info.
    authenticated: ar prisijungęs (svečias = False)
    is_admin: ar administratorius (pagal Authentik grupę OIDC atveju)
    user: rodomas vardas (display name)"""
    if not s:
        return {"authenticated": False, "user": None, "is_admin": False}
    return {
        "authenticated": True,
        "user": s.get("display_name") or s.get("user"),
        "is_admin": bool(s.get("is_admin")),
    }


# ---- /api/health ---------------------------------------------------------

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "docker_available": docker_client is not None,
    }


# ---- /api/stats ----------------------------------------------------------

@app.get("/api/stats")
def get_stats():
    """Realaus laiko CPU/RAM/Disk panaudojimas (host'o, ne konteinerio).
    Docker'iui be cgroup limits, psutil reportina host'o reikšmes."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_percent": round(cpu, 1),
        "ram_used_gib": round(mem.used / (1024**3), 2),
        "ram_total_gib": round(mem.total / (1024**3), 2),
        "ram_percent": round(mem.percent, 1),
        "disk_used_gb": round(disk.used / (1024**3), 2),
        "disk_total_gb": round(disk.total / (1024**3), 2),
        "disk_percent": round(disk.percent, 1),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---- /api/server-info ----------------------------------------------------

@app.get("/api/server-info")
def server_info(user: str = Depends(require_admin)):
    """Statinė info apie serverį + uptime. ADMIN ONLY."""
    return {
        "hostname": HOST_HOSTNAME,
        "public_ip": PUBLIC_IP,
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_model": CPU_MODEL,
        "cpu_cores": psutil.cpu_count() or 0,
        "ram_gib": round(psutil.virtual_memory().total / (1024**3), 1),
        "uptime_seconds": int(time.time() - psutil.boot_time()),
    }


# ---- /api/services -------------------------------------------------------

@app.get("/api/services")
def get_services(s: Optional[dict] = Depends(current_session_info)):
    """Servisų sąrašas su realiu statusu iš Docker'io + sleep info.
    Svečias IR paprastas vartotojas mato tik `public: true` servisus (vartotojui
    skirtus appsus). Admin mato visus (įsk. infrastruktūrą)."""
    now = datetime.now(timezone.utc)
    is_admin = bool(s and s.get("is_admin"))
    services = []
    for cfg in load_services_config():
        # Filtras pagal rolę
        if not is_admin and not cfg.get("public", False):
            continue
        status = container_status(cfg["container"])
        svc_name = cfg["name"]

        # Sleep info - jei servisas yra sleep-eligible ir šiuo metu idle
        sleep_in_seconds = None
        is_idle_now = False
        if svc_name in SLEEP_ELIGIBLE and svc_name in idle_since:
            elapsed = (now - idle_since[svc_name]).total_seconds()
            sleep_in_seconds = max(0, IDLE_THRESHOLD_SECONDS - int(elapsed))
            is_idle_now = True

        services.append({
            "name": svc_name,
            "display_name": cfg["display_name"],
            "description": cfg.get("description", ""),
            "icon": cfg.get("icon", "📦"),
            "category": cfg.get("category", "other"),
            "status": status,
            "url": cfg.get("url") if status == "running" else None,
            "estimated_ram_mb": cfg.get("estimated_ram_mb", 100),
            "estimated_cpu_percent": cfg.get("estimated_cpu_percent", 1),
            "uptime_seconds": 0,
            "last_error": None,
            "sleep_eligible": svc_name in SLEEP_ELIGIBLE,
            "is_idle_now": is_idle_now,
            "sleep_in_seconds": sleep_in_seconds,
        })
    return {"services": services}


# ---- GET /api/docker/unmanaged (admin only) ------------------------------

@app.get("/api/docker/unmanaged")
def get_unmanaged_containers(user: str = Depends(require_admin)):
    """Admin-only: VEIKIANTYS Docker konteineriai, kurių NĖRA services.yaml.
    Rodo 'naujai pajungtus dockerius' - kai atsiranda naujas appas (pvz. voice),
    jis automatiškai matomas dashboard'e be rankinio services.yaml redagavimo."""
    if not docker_client:
        return {"containers": []}

    # Infrastruktūros konteineriai - NErodom kaip "naujų" (vidinė virtuvė).
    ignore_prefixes = ("authentik-",)            # SSO stako vidus (db/redis/worker/server)
    ignore_exact = {"beszel-agent"}              # monitoringo agentas

    known = {cfg.get("container") for cfg in load_services_config()}
    result = []
    try:
        for c in docker_client.containers.list():  # tik veikiantys
            if c.name in known or c.name in ignore_exact:
                continue
            if any(c.name.startswith(p) for p in ignore_prefixes):
                continue
            try:
                image = c.image.tags[0] if c.image.tags else (c.image.short_id or "?")
            except Exception:
                image = "?"
            result.append({
                "name": c.name,
                "image": image,
                "status": c.status,
                "created": c.attrs.get("Created", ""),
            })
    except Exception as exc:
        logger.error(f"Nepavyko gauti konteinerių sąrašo: {exc}")
    result.sort(key=lambda x: x["name"])
    return {"containers": result}


# ---- POST /api/services/{name}/start -------------------------------------

@app.post("/api/services/{name}/start", status_code=202)
def start_service(name: str, user: Optional[str] = Depends(get_current_user)):
    """Paleidžia sustabdytą konteinerį per Docker SDK.
    Guest gali paleisti tik public servisus. Admin - bet kurį."""
    cfg = next((s for s in load_services_config() if s["name"] == name), None)
    if not cfg:
        raise HTTPException(status_code=404, detail="service not found")

    # Guest negali paleisti non-public servisų
    if user is None and not cfg.get("public", False):
        raise HTTPException(status_code=403, detail="admin reikalingas")

    if not docker_client:
        raise HTTPException(status_code=500, detail="docker not available")

    container_name = cfg["container"]
    try:
        c = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail="container not found")

    if c.status == "running":
        return {"status": "running", "estimated_ready_seconds": 0}

    try:
        c.start()
        log_event("service_started", cfg["name"], f"{cfg['display_name']} paleistas")
        logger.info(f"Paleista: {container_name}")
        return {"status": "starting", "estimated_ready_seconds": 10}
    except Exception as exc:
        logger.error(f"Klaida paleidžiant {container_name}: {exc}")
        log_event("service_error", cfg["name"], f"Paleidimas nepavyko: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---- /api/events ---------------------------------------------------------

@app.get("/api/events")
def get_events(limit: int = 10, user: str = Depends(require_admin)):
    """Paskutinieji įvykiai iš /data/events.log. ADMIN ONLY."""
    events: list[dict] = []
    if not EVENTS_LOG_PATH.exists():
        return {"events": events}

    try:
        with EVENTS_LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.error(f"Nepavyko nuskaityti events.log: {exc}")

    # Naujausi pirma
    events.reverse()
    return {"events": events[:limit]}


# ---- /api/services/resource-breakdown ------------------------------------

@app.get("/api/services/resource-breakdown")
def resource_breakdown(user: Optional[str] = Depends(get_current_user)):
    """CPU/RAM pasiskirstymas pagal servisus.
    Guest mato tik public servisus + free; admin - visus.

    Naudoja REALTIME duomenis iš docker stats (kešuotus per stats_collector
    thread'ą). Jei stats kešo nėra (pvz., konteineris ką tik startavo) -
    fallback'as į estimated reikšmes iš services.yaml.

    CPU% reikšmė normalizuota:
        Docker stats grąžina 0-400% (4 CPU sistemai), kur 100% = 1 CPU pilnas.
        Padaliname iš CPU branduolių skaičiaus -> "kiek % iš VISO serverio CPU".
        Stacked bar sumos viskas neviršys 100%.
    """
    total_ram_gib = psutil.virtual_memory().total / (1024**3)
    cpu_cores = max(1, psutil.cpu_count() or 1)
    is_admin = user is not None
    services = load_services_config()

    with _stats_lock:
        cache_snapshot = dict(container_stats_cache)

    cpu_breakdown: list[dict] = []
    ram_breakdown: list[dict] = []
    used_cpu = 0.0
    used_ram_gib = 0.0

    for cfg in services:
        # Filtras pagal rolę
        if not is_admin and not cfg.get("public", False):
            continue
        if container_status(cfg["container"]) != "running":
            continue

        svc_name = cfg["name"]
        stats = cache_snapshot.get(svc_name)

        if stats is not None:
            # REALTIME duomenys iš docker stats
            # CPU: stats per-CPU procentai (0-400% 4-core); normalizuojam į % iš serverio
            cpu_pct = stats["cpu_percent"] / cpu_cores
            ram_gib = stats["mem_bytes"] / (1024**3)
        else:
            # Fallback: services.yaml įverčiai (kol stats dar nepasiruošė)
            cpu_pct = float(cfg.get("estimated_cpu_percent", 1))
            ram_gib = float(cfg.get("estimated_ram_mb", 100)) / 1024

        cpu_breakdown.append({
            "service": svc_name,
            "percent": round(cpu_pct, 2),
            "color": get_color(svc_name),
        })
        ram_breakdown.append({
            "service": svc_name,
            "percent": round(ram_gib / total_ram_gib * 100, 2),
            "color": get_color(svc_name),
        })
        used_cpu += cpu_pct
        used_ram_gib += ram_gib

    # "Free" elementas - likutis iki 100%
    cpu_breakdown.append({
        "service": "free",
        "percent": max(0.0, round(100.0 - used_cpu, 2)),
        "color": get_color("free"),
    })
    ram_breakdown.append({
        "service": "free",
        "percent": max(0.0, round(100.0 - (used_ram_gib / total_ram_gib * 100), 2)),
        "color": get_color("free"),
    })

    return {
        "cpu_breakdown": cpu_breakdown,
        "ram_breakdown": ram_breakdown,
    }


# ============================================
# FAZĖ 4 - VARTOTOJŲ VALDYMAS (Authentik API)
# ============================================
# Visi šios sekcijos endpoint'ai reikalauja require_admin (tik Konradas).
# Kalbam su Authentik per vidinį "web" tinklą su akadmin API tokenu.

def _ak(method: str, path: str, json_body: Optional[dict] = None) -> Any:
    """Authentik API užklausa. Grąžina parsed JSON (arba None jei 204).
    Klaidos atveju keliam HTTPException su aiškia žinute."""
    if not AUTHENTIK_API_TOKEN:
        raise HTTPException(status_code=503, detail="Authentik API nesukonfigūruotas")
    url = f"{AUTHENTIK_API_URL}/api/v3{path}"
    # X-Forwarded-* priverčia Authentik kurti VIEŠAS nuorodas (auth.kondev.app),
    # o ne vidinį docker adresą (authentik-server:9000). Svarbu recovery/verify
    # laiškų nuorodoms, kad vartotojas galėtų jas atidaryti naršyklėje.
    headers = {"Authorization": f"Bearer {AUTHENTIK_API_TOKEN}",
               "Content-Type": "application/json",
               "X-Forwarded-Host": AUTHENTIK_PUBLIC_HOST,
               "X-Forwarded-Proto": "https"}
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        logger.error(f"Authentik API nepasiekiamas: {exc}")
        raise HTTPException(status_code=502, detail="Authentik nepasiekiamas")
    if resp.status_code >= 400:
        logger.warning(f"Authentik API {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Authentik klaida ({resp.status_code})")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def _ak_2fa_user_ids() -> set[int]:
    """Vartotojų (pk), kurie turi bent vieną patvirtintą MFA įrenginį, aibė."""
    try:
        devices = _ak("GET", "/authenticators/admin/all/")
    except HTTPException:
        return set()
    ids: set[int] = set()
    for dev in devices or []:
        if dev.get("confirmed", True) and dev.get("user") is not None:
            ids.add(dev["user"])
    return ids


@app.get("/api/admin/users")
def admin_list_users(_: str = Depends(require_admin)):
    """Vartotojų sąrašas iš Authentik. Praleidžia service account'us/outpost'us
    (rodom tik tikrus žmones: internal=adminas, external=registruoti vartotojai)."""
    data = _ak("GET", "/core/users/?page_size=200")
    mfa_ids = _ak_2fa_user_ids()
    users = []
    for u in data.get("results", []):
        if u.get("type") not in ("internal", "external"):
            continue  # service account / outpost - nerodom
        groups = [g.get("name") for g in u.get("groups_obj", [])]
        users.append({
            "pk": u.get("pk"),
            "username": u.get("username"),
            "name": u.get("name") or u.get("username"),
            "email": u.get("email") or "",
            "is_active": bool(u.get("is_active")),
            "is_admin": OIDC_ADMIN_GROUP in groups,
            "has_2fa": u.get("pk") in mfa_ids,
            "last_login": u.get("last_login"),
        })
    # Adminai viršuje, tada pagal vardą
    users.sort(key=lambda x: (not x["is_admin"], x["name"].lower()))
    return {"users": users}


def _ak_get_user(pk: int) -> dict:
    """Saugiai gauna vartotoją; blokuoja veiksmus su service account'ais."""
    u = _ak("GET", f"/core/users/{pk}/")
    if u.get("type") not in ("internal", "external"):
        raise HTTPException(status_code=400, detail="neleistinas vartotojo tipas")
    return u


@app.post("/api/admin/users/{pk}/reset-password")
def admin_reset_password(pk: int, admin: str = Depends(require_admin)):
    """Išsiunčia slaptažodžio atstatymo (recovery) laišką vartotojui."""
    u = _ak_get_user(pk)
    if not u.get("email"):
        raise HTTPException(status_code=400, detail="vartotojas neturi email")
    if not AUTHENTIK_RECOVERY_EMAIL_STAGE:
        raise HTTPException(status_code=503, detail="recovery email stage nesukonfigūruotas")
    _ak("POST", f"/core/users/{pk}/recovery_email/",
        {"email_stage": AUTHENTIK_RECOVERY_EMAIL_STAGE})
    log_event("admin", "users", f"{admin}: slaptažodžio reset laiškas -> {u.get('username')}")
    return {"ok": True, "message": f"Atstatymo laiškas išsiųstas: {u['email']}"}


@app.post("/api/admin/users/{pk}/resend-verification")
def admin_resend_verification(pk: int, admin: str = Depends(require_admin)):
    """Pakartoja patvirtinimo (enroll) laišką vartotojui."""
    u = _ak_get_user(pk)
    if not u.get("email"):
        raise HTTPException(status_code=400, detail="vartotojas neturi email")
    stage = AUTHENTIK_ENROLL_EMAIL_STAGE or AUTHENTIK_RECOVERY_EMAIL_STAGE
    if not stage:
        raise HTTPException(status_code=503, detail="email stage nesukonfigūruotas")
    _ak("POST", f"/core/users/{pk}/recovery_email/", {"email_stage": stage})
    log_event("admin", "users", f"{admin}: patvirtinimo laiškas -> {u.get('username')}")
    return {"ok": True, "message": f"Patvirtinimo laiškas išsiųstas: {u['email']}"}


@app.post("/api/admin/users/{pk}/email")
def admin_change_email(pk: int, body: dict, admin: str = Depends(require_admin)):
    """Pakeičia vartotojo email adresą."""
    new_email = (body.get("email") or "").strip()
    if "@" not in new_email or len(new_email) < 5:
        raise HTTPException(status_code=400, detail="neteisingas email")
    u = _ak_get_user(pk)
    _ak("PATCH", f"/core/users/{pk}/", {"email": new_email})
    log_event("admin", "users",
              f"{admin}: email {u.get('email')} -> {new_email} ({u.get('username')})")
    return {"ok": True, "message": f"Email pakeistas į {new_email}"}


def _propagate_active(username: str, is_active: bool) -> list[str]:
    """Praneša servisams (vault, voice) apie vartotojo aktyvumo pasikeitimą, kad
    deaktyvavimas įsigaliotų iškart (ne tik naujiems prisijungimams).
    Best-effort: jei servisas nepasiekiamas, tylim (grąžinam pastabą)."""
    if not INTERNAL_API_TOKEN:
        return ["vidinis tokenas nesukonfigūruotas - propagavimas praleistas"]
    headers = {"X-Internal-Token": INTERNAL_API_TOKEN, "Content-Type": "application/json"}
    body = {"username": username, "is_active": is_active}
    notes: list[str] = []
    targets = [("vault", f"{VAULT_INTERNAL_URL}/api/internal/set-active"),
               ("voice", f"{VOICE_INTERNAL_URL}/internal/set-active")]
    for name, url in targets:
        try:
            with httpx.Client(timeout=8) as client:
                r = client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                notes.append(f"{name}: klaida {r.status_code}")
                logger.warning(f"propagate {name} {r.status_code}: {r.text[:200]}")
        except httpx.HTTPError as exc:
            notes.append(f"{name}: nepasiekiamas")
            logger.warning(f"propagate {name} nepasiekiamas: {exc}")
    return notes


@app.post("/api/admin/users/{pk}/active")
def admin_set_active(pk: int, body: dict, admin: str = Depends(require_admin)):
    """Aktyvuoja / deaktyvuoja vartotoją. Adminų negalima deaktyvuoti.
    Pakeitimas propaguojamas į servisus (vault, voice) - kad įsigaliotų iškart."""
    is_active = bool(body.get("is_active"))
    u = _ak_get_user(pk)
    groups = [g.get("name") for g in u.get("groups_obj", [])]
    if not is_active and OIDC_ADMIN_GROUP in groups:
        raise HTTPException(status_code=400, detail="administratoriaus negalima deaktyvuoti")
    _ak("PATCH", f"/core/users/{pk}/", {"is_active": is_active})
    notes = _propagate_active(u.get("username"), is_active)
    veiksmas = "aktyvuotas" if is_active else "deaktyvuotas (visi servisai)"
    log_event("admin", "users", f"{admin}: {u.get('username')} {veiksmas}")
    msg = f"Vartotojas {veiksmas}"
    if notes:
        msg += " — pastaba: " + "; ".join(notes)
    return {"ok": True, "message": msg}


def _propagate_delete(username: str) -> list[str]:
    """Ištrina vartotojo duomenis servisuose: vault (DB įrašai + failai iš disko)
    ir voice (sesijos + aktyvūs pokalbiai). Best-effort, grąžina pastabas."""
    if not INTERNAL_API_TOKEN:
        return ["vidinis tokenas nesukonfigūruotas - servisų valymas praleistas"]
    headers = {"X-Internal-Token": INTERNAL_API_TOKEN, "Content-Type": "application/json"}
    notes: list[str] = []
    # 1) Vault - ištrinti vartotoją + visus jo failus
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(f"{VAULT_INTERNAL_URL}/api/internal/delete-user",
                            headers=headers, json={"username": username})
        if r.status_code >= 400:
            notes.append(f"vault: klaida {r.status_code}")
            logger.warning(f"delete-user vault {r.status_code}: {r.text[:200]}")
        else:
            data = r.json()
            logger.info(f"vault delete-user '{username}': {data}")
    except httpx.HTTPError as exc:
        notes.append("vault: nepasiekiamas")
        logger.warning(f"delete-user vault nepasiekiamas: {exc}")
    # 2) Voice - išmesti sesijas + atjungti (naudojam set-active is_active=False)
    try:
        with httpx.Client(timeout=8) as client:
            r = client.post(f"{VOICE_INTERNAL_URL}/internal/set-active",
                            headers=headers, json={"username": username, "is_active": False})
        if r.status_code >= 400:
            notes.append(f"voice: klaida {r.status_code}")
    except httpx.HTTPError as exc:
        notes.append("voice: nepasiekiamas")
        logger.warning(f"delete-user voice nepasiekiamas: {exc}")
    return notes


@app.delete("/api/admin/users/{pk}")
def admin_delete_user(pk: int, admin: str = Depends(require_admin)):
    """NEGRĮŽTAMAI ištrina vartotoją iš VISŲ sluoksnių:
    vault (DB + failai diske) -> voice (sesijos) -> Authentik (tapatybė).
    Adminų ištrinti negalima."""
    u = _ak_get_user(pk)
    groups = [g.get("name") for g in u.get("groups_obj", [])]
    if OIDC_ADMIN_GROUP in groups:
        raise HTTPException(status_code=400, detail="administratoriaus negalima ištrinti")
    username = u.get("username")

    # 1) Pirma išvalom duomenis servisuose (kol dar turim username)
    notes = _propagate_delete(username)
    # 2) Tada ištrinam tapatybę iš Authentik
    _ak("DELETE", f"/core/users/{pk}/")

    log_event("admin", "users", f"{admin}: IŠTRINTAS vartotojas {username} (visi servisai)")
    msg = f"Vartotojas '{username}' ištrintas (Authentik + vault failai + voice)"
    if notes:
        msg += " — pastaba: " + "; ".join(notes)
    return {"ok": True, "message": msg}
