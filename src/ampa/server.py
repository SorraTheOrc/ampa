"""AMPA HTTP server — combined /metrics, /health, /respond, /admin, /session.

This module provides the WSGI application and threaded server that exposes
Prometheus metrics, health checks, the responder endpoint, admin fallback
controls, and session state lookups. Tests may start the server via
``start_metrics_server``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Optional, Tuple

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

from . import responder
from . import fallback
from .conversation_manager import (
    InvalidStateError,
    NotFoundError,
    SDKError,
    TimedOutError,
)
from .scheduler_types import CommandRunResult, _to_iso

_LOG = logging.getLogger("ampa.server")

# Module-level reference to the running scheduler, registered by the daemon.
# When set, the /run endpoint delegates command execution to this scheduler
# so that manual runs appear in the daemon log and scheduler store.
_scheduler = None


def register_scheduler(sched) -> None:
    """Register the running scheduler instance for /run endpoint delegation.

    Called by the daemon after the scheduler is loaded so that HTTP-triggered
    runs go through the same scheduler that processes scheduled runs.
    """
    global _scheduler
    _scheduler = sched

# Registry-local metrics so they do not clash with external collectors during
# tests or when the package is imported multiple times.
registry = CollectorRegistry()

ampa_heartbeat_sent_total = Counter(
    "ampa_heartbeat_sent_total",
    "Total number of successful AMPA heartbeat sends",
    registry=registry,
)
ampa_heartbeat_failure_total = Counter(
    "ampa_heartbeat_failure_total",
    "Total number of failed AMPA heartbeat sends",
    registry=registry,
)
ampa_last_heartbeat_timestamp_seconds = Gauge(
    "ampa_last_heartbeat_timestamp_seconds",
    "Last successful heartbeat time as epoch seconds",
    registry=registry,
)


def _tool_output_dir() -> str:
    path = os.getenv("AMPA_TOOL_OUTPUT_DIR")
    if path:
        return path
    return os.path.join(tempfile.gettempdir(), "opencode_tool_output")


def _read_session_state(session_id: str) -> Optional[dict]:
    tool_dir = _tool_output_dir()
    session_path = os.path.join(tool_dir, f"session_{session_id}.json")
    if not os.path.exists(session_path):
        return None
    try:
        with open(session_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("session", session_id)
    data.setdefault("session_id", session_id)
    return data


def _json_response(start_response, status: str, payload: dict) -> "list[bytes]":
    body = json.dumps(payload).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _read_json_body(environ) -> Optional[dict]:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        length = 0
    if length <= 0:
        return None
    try:
        raw = environ.get("wsgi.input").read(length)
    except Exception:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _wsgi_app(environ, start_response):
    path = environ.get("PATH_INFO", "")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    if path == "/metrics":
        data = generate_latest(registry)
        start_response("200 OK", [("Content-Type", CONTENT_TYPE_LATEST)])
        return [data]

    if path == "/health":
        # Fatal misconfiguration = missing AMPA_DISCORD_BOT_TOKEN
        bot_token = os.getenv("AMPA_DISCORD_BOT_TOKEN")
        if bot_token and bot_token.strip():
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"OK"]
        start_response("503 Service Unavailable", [("Content-Type", "text/plain")])
        return [b"misconfigured"]

    if path == "/respond":
        if method != "POST":
            return _json_response(
                start_response, "405 Method Not Allowed", {"error": "POST required"}
            )
        payload = _read_json_body(environ)
        if payload is None:
            return _json_response(
                start_response, "400 Bad Request", {"error": "invalid JSON"}
            )
        if isinstance(payload, dict) and "project_id" in payload:
            project_id = payload.get("project_id")
            if project_id:
                is_public = False
            else:
                is_public = "project_id" not in payload
            # Support per-decision overrides: callers may include a
            # ``decision`` field (freeform string) in the payload.
            decision = payload.get("decision") if isinstance(payload, dict) else None
            mode = fallback.resolve_mode(
                project_id, is_public=is_public, decision=decision
            )
            if mode == "auto-accept" and "action" not in payload:
                payload["action"] = "accept"
            elif mode == "auto-decline" and "action" not in payload:
                payload["action"] = "decline"
            elif mode == "accept-recommendation" and "action" not in payload:
                # Auto-accept the agent's recommendation when present;
                # otherwise fall back to hold (no action injected).
                recommendation = payload.get("recommendation")
                if isinstance(recommendation, dict) and recommendation.get("action"):
                    rec_action = str(recommendation["action"]).strip().lower()
                    if rec_action in ("accept", "decline"):
                        payload["action"] = rec_action
                    else:
                        # Unrecognised recommendation action — treat as hold
                        _LOG.info(
                            "accept-recommendation: unrecognised recommendation "
                            "action %r, falling back to hold",
                            rec_action,
                        )
                else:
                    _LOG.info(
                        "accept-recommendation: no recommendation in payload, "
                        "falling back to hold"
                    )
            elif mode == "discuss-options" and "action" not in payload:
                # Placeholder: discuss-options falls back to hold with logging.
                # Future: integrate with conversation_manager for multi-turn
                # discussion flows.
                _LOG.info(
                    "discuss-options mode requested but not yet implemented; "
                    "falling back to hold"
                )
        try:
            if not isinstance(payload, dict):
                return _json_response(
                    start_response, "400 Bad Request", {"error": "invalid JSON"}
                )
            result = responder.resume_from_payload(payload)
            return _json_response(start_response, "200 OK", result)
        except NotFoundError as exc:
            return _json_response(start_response, "404 Not Found", {"error": str(exc)})
        except InvalidStateError as exc:
            return _json_response(start_response, "409 Conflict", {"error": str(exc)})
        except TimedOutError as exc:
            return _json_response(start_response, "410 Gone", {"error": str(exc)})
        except SDKError as exc:
            return _json_response(
                start_response, "502 Bad Gateway", {"error": str(exc)}
            )
        except ValueError as exc:
            return _json_response(
                start_response, "400 Bad Request", {"error": str(exc)}
            )
        except Exception as exc:
            return _json_response(
                start_response, "500 Internal Server Error", {"error": str(exc)}
            )

    if path == "/run":
        if method != "POST":
            return _json_response(
                start_response, "405 Method Not Allowed", {"error": "POST required"}
            )
        payload = _read_json_body(environ)
        if payload is None or "command_id" not in payload:
            return _json_response(
                start_response, "400 Bad Request", {"error": "command_id required"}
            )
        if _scheduler is None:
            return _json_response(
                start_response,
                "503 Service Unavailable",
                {"error": "scheduler not running"},
            )
        command_id = str(payload["command_id"])
        spec = _scheduler.store.get_command(command_id)
        if spec is None:
            return _json_response(
                start_response,
                "404 Not Found",
                {"error": f"Unknown command id: {command_id}"},
            )
        try:
            import socket as _socket

            instance = _socket.gethostname()
            run = _scheduler.start_command(spec)
            output = run.output if isinstance(run, CommandRunResult) else None
            result = {
                "id": spec.command_id,
                "name": spec.title or spec.command_id,
                "status": "success" if run.exit_code == 0 else "failed",
                "started_at": _to_iso(run.start_ts),
                "finished_at": _to_iso(run.end_ts),
                "duration_seconds": round(run.duration_seconds, 3),
                "exit_code": run.exit_code,
                "output": output,
                "metadata": getattr(run, "metadata", None),
                "instance": instance,
            }
            return _json_response(start_response, "200 OK", result)
        except Exception as exc:
            _LOG.exception("Error executing command %s via /run", command_id)
            return _json_response(
                start_response,
                "500 Internal Server Error",
                {"error": str(exc)},
            )

    if path == "/admin/fallback":
        token = os.getenv("AMPA_ADMIN_TOKEN")
        if token:
            auth = environ.get("HTTP_AUTHORIZATION", "")
            if not auth.startswith("Bearer "):
                return _json_response(
                    start_response, "401 Unauthorized", {"error": "Unauthorized"}
                )
            provided = auth[len("Bearer ") :].strip()
            if provided != token:
                return _json_response(
                    start_response, "403 Forbidden", {"error": "Forbidden"}
                )
        if method == "GET":
            config = fallback.load_config()
            return _json_response(start_response, "200 OK", config)
        if method != "POST":
            return _json_response(
                start_response, "405 Method Not Allowed", {"error": "POST required"}
            )
        payload = _read_json_body(environ)
        if payload is None:
            return _json_response(
                start_response, "400 Bad Request", {"error": "invalid JSON"}
            )
        if not isinstance(payload, dict):
            return _json_response(
                start_response, "400 Bad Request", {"error": "invalid JSON"}
            )
        config = fallback.save_config(payload)
        return _json_response(start_response, "200 OK", config)

    if path.startswith("/session"):
        session_id = None
        if path.startswith("/session/"):
            session_id = path.split("/", 2)[2]
        if not session_id:
            return _json_response(
                start_response, "400 Bad Request", {"error": "session_id required"}
            )
        state = _read_session_state(session_id)
        if not state:
            return _json_response(
                start_response, "404 Not Found", {"error": "session not found"}
            )
        return _json_response(start_response, "200 OK", state)

    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


class _ThreadedWSGIServer(threading.Thread):
    def __init__(self, host: str, port: int):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self._server: Optional[Tuple[WSGIServer, int]] = None

    def run(self) -> None:  # pragma: no cover - exercised in integration tests
        httpd = make_server(self.host, self.port, _wsgi_app)
        # communicate the chosen port back to the thread owner via attribute
        self._server = (httpd, httpd.server_port)
        try:
            httpd.serve_forever()
        finally:
            try:
                httpd.server_close()
            except Exception:
                pass

    def get_port(self) -> Optional[int]:
        if self._server:
            return self._server[1]
        return None


def start_metrics_server(
    host: str = "127.0.0.1", port: int = 8000
) -> Tuple[_ThreadedWSGIServer, int]:
    """Start the combined metrics+health server in a background thread.

    Returns the thread object and the bound port (useful when port=0 was passed).
    """
    thr = _ThreadedWSGIServer(host, port)
    thr.start()

    # Wait for server to be created and bound
    for _ in range(200):
        p = thr.get_port()
        if p:
            return thr, p
        time.sleep(0.01)
    # Last-ditch: return whatever we have
    return thr, port


__all__ = [
    "ampa_heartbeat_sent_total",
    "ampa_heartbeat_failure_total",
    "ampa_last_heartbeat_timestamp_seconds",
    "register_scheduler",
    "start_metrics_server",
]
