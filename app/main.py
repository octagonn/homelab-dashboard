import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("homelab-dashboard")


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


PORTAINER_URL = os.getenv("PORTAINER_URL", "").rstrip("/")
PORTAINER_API_KEY = os.getenv("PORTAINER_API_KEY", "")
DEMO_MODE = env_bool("DEMO_MODE", True)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
DB_PATH = os.getenv("DB_PATH", "/data/dashboard.sqlite")

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"

app = FastAPI(title="Homelab Dashboard")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

session = requests.Session()
if PORTAINER_API_KEY:
    session.headers.update({"X-API-Key": PORTAINER_API_KEY})



IP_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    data_dir = os.path.dirname(DB_PATH)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            endpoint_id INTEGER PRIMARY KEY,
            name TEXT,
            url TEXT,
            ip TEXT,
            status TEXT,
            tags TEXT,
            last_seen TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS containers (
            endpoint_id INTEGER,
            container_id TEXT,
            name TEXT,
            image TEXT,
            state TEXT,
            status TEXT,
            ports TEXT,
            updated_at TEXT,
            PRIMARY KEY (endpoint_id, container_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stacks (
            endpoint_id INTEGER,
            stack_id INTEGER,
            name TEXT,
            status TEXT,
            type TEXT,
            updated_at TEXT,
            PRIMARY KEY (endpoint_id, stack_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_name TEXT,
            from_endpoint_id INTEGER,
            to_endpoint_id INTEGER,
            detected_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS container_last_seen (
            container_name TEXT PRIMARY KEY,
            endpoint_id INTEGER,
            container_id TEXT,
            last_seen TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )


def portainer_get(path):
    if DEMO_MODE:
        from app.demo import payload
        return payload(path)
    if not PORTAINER_URL or not PORTAINER_API_KEY:
        logger.error("Missing PORTAINER_URL or PORTAINER_API_KEY")
        return None
    url = f"{PORTAINER_URL}{path}"
    try:
        response = session.get(url, verify=True, timeout=10)
    except requests.RequestException as exc:
        logger.warning("Portainer request failed: %s", exc)
        return None
    if response.status_code != 200:
        logger.warning("Portainer request %s failed with status %s", path, response.status_code)
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("Portainer request %s returned invalid JSON", path)
        return None


def extract_ip(value):
    if not value:
        return ""
    match = IP_RE.search(str(value))
    return match.group(0) if match else ""


def normalize_status(status):
    if status in (1, "1", True):
        return "up"
    if status in (2, "2", False, 0, "0"):
        return "down"
    if status is None:
        return "unknown"
    return str(status)


def normalize_tags(endpoint):
    tags = endpoint.get("Tags")
    if isinstance(tags, list):
        tag_names = []
        for tag in tags:
            if isinstance(tag, dict):
                name = tag.get("Name") or tag.get("name") or tag.get("Id") or tag.get("id")
                if name is not None:
                    tag_names.append(str(name))
            else:
                tag_names.append(str(tag))
        return ", ".join([t for t in tag_names if t])
    tag_ids = endpoint.get("TagIds")
    if isinstance(tag_ids, list):
        return ", ".join([str(t) for t in tag_ids])
    return ""


def normalize_endpoint(endpoint):
    endpoint_id = endpoint.get("Id") or endpoint.get("id")
    name = endpoint.get("Name") or endpoint.get("name") or f"endpoint-{endpoint_id}"
    url = endpoint.get("URL") or endpoint.get("PublicURL") or endpoint.get("url") or ""
    status = normalize_status(endpoint.get("Status") or endpoint.get("status"))
    tags = normalize_tags(endpoint)
    ip = extract_ip(url)
    return {
        "endpoint_id": endpoint_id,
        "name": name,
        "url": url,
        "status": status,
        "tags": tags,
        "ip": ip,
    }


def format_ports(ports):
    if not ports:
        return ""
    items = []
    for port in ports:
        if not isinstance(port, dict):
            continue
        ip = port.get("IP") or ""
        private = port.get("PrivatePort")
        public = port.get("PublicPort")
        proto = port.get("Type") or ""
        if public:
            if ip:
                items.append(f"{ip}:{public}->{private}/{proto}")
            else:
                items.append(f"{public}->{private}/{proto}")
        elif private:
            items.append(f"{private}/{proto}")
    return ", ".join(items)


def normalize_container(container):
    container_id = container.get("Id") or container.get("id")
    names = container.get("Names") or container.get("names")
    name = ""
    if isinstance(names, list) and names:
        name = str(names[0]).lstrip("/")
    elif isinstance(names, str):
        name = names.lstrip("/")
    else:
        name = container.get("Name") or container.get("name") or ""
    image = container.get("Image") or container.get("image") or ""
    state = container.get("State") or container.get("state") or ""
    status = container.get("Status") or container.get("status") or ""
    ports = format_ports(container.get("Ports") or container.get("ports") or [])
    return {
        "container_id": container_id,
        "name": name,
        "image": image,
        "state": state,
        "status": status,
        "ports": ports,
    }


def poll_once():
    endpoints = portainer_get("/api/endpoints")
    if not isinstance(endpoints, list):
        logger.warning("Portainer endpoints list is empty or invalid")
        return

    now = utc_now()
    new_map = {}
    conn = db_connect()
    init_db(conn)

    with conn:
        for endpoint in endpoints:
            normalized = normalize_endpoint(endpoint)
            endpoint_id = normalized["endpoint_id"]
            if endpoint_id is None:
                continue

            last_seen = now if normalized["status"] == "up" else None
            conn.execute(
                """
                INSERT INTO nodes (endpoint_id, name, url, ip, status, tags, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(endpoint_id) DO UPDATE SET
                    name=excluded.name,
                    url=excluded.url,
                    ip=excluded.ip,
                    status=excluded.status,
                    tags=excluded.tags,
                    last_seen=COALESCE(excluded.last_seen, nodes.last_seen)
                """,
                (
                    endpoint_id,
                    normalized["name"],
                    normalized["url"],
                    normalized["ip"],
                    normalized["status"],
                    normalized["tags"],
                    last_seen,
                ),
            )

            containers = portainer_get(
                f"/api/endpoints/{endpoint_id}/docker/containers/json?all=1"
            )
            if containers is None:
                continue
            if not isinstance(containers, list):
                logger.warning("Unexpected containers payload for endpoint %s", endpoint_id)
                continue

            conn.execute("DELETE FROM containers WHERE endpoint_id = ?", (endpoint_id,))
            for container in containers:
                normalized_container = normalize_container(container)
                if not normalized_container["container_id"] or not normalized_container["name"]:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO containers
                    (endpoint_id, container_id, name, image, state, status, ports, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        endpoint_id,
                        normalized_container["container_id"],
                        normalized_container["name"],
                        normalized_container["image"],
                        normalized_container["state"],
                        normalized_container["status"],
                        normalized_container["ports"],
                        now,
                    ),
                )
                new_map[normalized_container["name"]] = (
                    endpoint_id,
                    normalized_container["container_id"],
                )

        stacks = portainer_get("/api/stacks")
        if isinstance(stacks, list):
            conn.execute("DELETE FROM stacks")
            for stack in stacks:
                endpoint_id = stack.get("EndpointId") or stack.get("endpointId")
                if endpoint_id is None:
                    continue
                stack_id = stack.get("Id") or stack.get("id")
                name = stack.get("Name") or stack.get("name") or ""
                status = stack.get("Status") or stack.get("status") or ""
                stack_type = stack.get("Type") or stack.get("type") or ""
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stacks
                    (endpoint_id, stack_id, name, status, type, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (endpoint_id, stack_id, name, status, stack_type, now),
                )
        elif stacks is None:
            logger.info("Stacks endpoint not available or returned error.")

        if new_map:
            rows = conn.execute(
                "SELECT container_name, endpoint_id FROM container_last_seen"
            ).fetchall()
            last_map = {row["container_name"]: row["endpoint_id"] for row in rows}
            for name, (endpoint_id, container_id) in new_map.items():
                previous = last_map.get(name)
                if previous is not None and previous != endpoint_id:
                    conn.execute(
                        """
                        INSERT INTO moves (container_name, from_endpoint_id, to_endpoint_id, detected_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (name, previous, endpoint_id, now),
                    )
                conn.execute(
                    """
                    INSERT INTO container_last_seen (container_name, endpoint_id, container_id, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(container_name) DO UPDATE SET
                        endpoint_id=excluded.endpoint_id,
                        container_id=excluded.container_id,
                        last_seen=excluded.last_seen
                    """,
                    (name, endpoint_id, container_id, now),
                )

        conn.execute(
            """
            INSERT INTO meta (key, value)
            VALUES ('last_poll', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (now,),
        )

    conn.close()


def poll_loop():
    while True:
        try:
            poll_once()
        except Exception:
            logger.exception("Poll failed")
        time.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event():
    conn = db_connect()
    init_db(conn)
    conn.close()
    thread = threading.Thread(target=poll_loop, daemon=True)
    thread.start()


def get_last_poll(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='last_poll'").fetchone()
    return row["value"] if row else None


@app.get("/health", response_class=JSONResponse)
def health():
    conn = db_connect()
    init_db(conn)
    last_poll = get_last_poll(conn)
    conn.close()
    return {"status": "ok", "last_poll": last_poll}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = db_connect()
    init_db(conn)
    nodes_rows = conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()
    containers_rows = conn.execute("SELECT * FROM containers ORDER BY name").fetchall()
    stacks_rows = conn.execute("SELECT * FROM stacks ORDER BY name").fetchall()
    recent_moves = conn.execute(
        """
        SELECT m.container_name, m.detected_at, m.from_endpoint_id, m.to_endpoint_id,
               n1.name AS from_name, n2.name AS to_name
        FROM moves m
        LEFT JOIN nodes n1 ON m.from_endpoint_id = n1.endpoint_id
        LEFT JOIN nodes n2 ON m.to_endpoint_id = n2.endpoint_id
        ORDER BY m.detected_at DESC
        LIMIT 20
        """
    ).fetchall()
    last_poll = get_last_poll(conn)
    conn.close()

    containers_by_endpoint = {}
    for row in containers_rows:
        containers_by_endpoint.setdefault(row["endpoint_id"], []).append(dict(row))

    stacks_by_endpoint = {}
    for row in stacks_rows:
        stacks_by_endpoint.setdefault(row["endpoint_id"], []).append(dict(row))

    nodes = []
    for row in nodes_rows:
        node = dict(row)
        node["containers"] = containers_by_endpoint.get(row["endpoint_id"], [])
        node["stacks"] = stacks_by_endpoint.get(row["endpoint_id"], [])
        nodes.append(node)

    return templates.TemplateResponse(
        request=request, name="index.html", context={
            "request": request,
            "nodes": nodes,
            "recent_moves": [dict(r) for r in recent_moves],
            "last_poll": last_poll,
        },
    )


@app.get("/node/{endpoint_id}", response_class=HTMLResponse)
def node_detail(request: Request, endpoint_id: int):
    conn = db_connect()
    init_db(conn)
    node_row = conn.execute(
        "SELECT * FROM nodes WHERE endpoint_id = ?", (endpoint_id,)
    ).fetchone()
    if not node_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Node not found")
    containers = conn.execute(
        "SELECT * FROM containers WHERE endpoint_id = ? ORDER BY name",
        (endpoint_id,),
    ).fetchall()
    stacks = conn.execute(
        "SELECT * FROM stacks WHERE endpoint_id = ? ORDER BY name",
        (endpoint_id,),
    ).fetchall()
    last_poll = get_last_poll(conn)
    conn.close()

    return templates.TemplateResponse(
        request=request, name="node.html", context={
            "request": request,
            "node": dict(node_row),
            "containers": [dict(c) for c in containers],
            "stacks": [dict(s) for s in stacks],
            "last_poll": last_poll,
        },
    )


@app.get("/moves", response_class=HTMLResponse)
def moves_page(request: Request):
    conn = db_connect()
    init_db(conn)
    moves = conn.execute(
        """
        SELECT m.container_name, m.detected_at, m.from_endpoint_id, m.to_endpoint_id,
               n1.name AS from_name, n2.name AS to_name
        FROM moves m
        LEFT JOIN nodes n1 ON m.from_endpoint_id = n1.endpoint_id
        LEFT JOIN nodes n2 ON m.to_endpoint_id = n2.endpoint_id
        ORDER BY m.detected_at DESC
        LIMIT 100
        """
    ).fetchall()
    last_poll = get_last_poll(conn)
    conn.close()

    return templates.TemplateResponse(
        request=request, name="moves.html", context={"request": request, "moves": [dict(m) for m in moves], "last_poll": last_poll},
    )
