"""
CEIGAS Gate Middleware — the operator kill switch on every request under a
MAPPED prefix. While the switch is set (ceigas_kill_switch), a mapped request
is refused with 403 unless it is a named stop or a core.governance crossing; with
the switch off, every request passes. A /api prefix that is not in
ROUTE_DOMAIN_MAP is never refused here, so the map is the whole of what this
gate covers; scripts/test_ceigas_gate_routes.py holds every key to a live route
and every /api/applications route to the gate.

Grants are not checked here. What a caller may do is decided per app token
(requires_permission on each route) and, for the Host's own tools, by the
capability gate (runtime/tools.py). This gate is the stop.

Maps HTTP routes to CEIGAS domains and operations:
  /api/files/*         → device.filesystem
  /api/applications/*  → container.relay (launch, close, install, publish, sites)
  /api/system/*        → core.governance (read only)
  /api/ceigas/*        → core.governance
  /api/screen/*        → device.filesystem (screen data)
  /api/input/*         → container.relay (machine control)
  /api/clipboard/*     → device.filesystem (clipboard data)
  /api/brain/*         → core.memory (the entity's own memory)

Static files (dashboard HTML/CSS/JS) bypass CEIGAS — the UI is always served.
/api/system/health AND everything under /api/system/health/ bypass CEIGAS —
the Docker healthcheck and BOTH kubelet probes (liveness on /health every 20s,
readiness on /health/deep every 5s) have to reach it. The sub-path half is a
prefix, not a second literal, so a later readiness sub-path is covered the day
it is added.
"""

import json
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("ceigas.gate")

# Route → (source_domain, target_domain, operation)
ROUTE_DOMAIN_MAP = {
    "/api/files": ("container.relay", "device.filesystem", None),  # operation from method
    # Apps acting on the Host: the launcher list and manifests, launch, close,
    # the resident runtime switch, install/uninstall, store purchases, publishing
    # and the whole sites surface (domains, DNS records, databases, Stripe). The
    # Host's own kernel_op calls land here too (in-process, through this stack).
    # Same crossing the launcher always had: it was keyed "/api/apps" until
    # routes/apps.py was retired (33f6668c) and /api/applications
    # took over; the key never moved, so from then on the kill switch did not
    # reach any app route. This key is what puts them under it.
    "/api/applications": ("container.relay", "container.relay", None),
    "/api/system": ("container.relay", "core.governance", "read"),
    "/api/ceigas": ("core.governance", "core.governance", None),
    "/api/screen": ("container.relay", "device.filesystem", None),  # reading screen data
    "/api/input": ("container.relay", "container.relay", None),     # controlling the machine
    "/api/clipboard": ("container.relay", "device.filesystem", None),  # clipboard is device data
    # brain = the entity's OWN cognition/memory/sessions: the request enters
    # the pod through the container relay and lands in core.memory.
    "/api/brain": ("container.relay", "core.memory", None),
    # The Host's own database (routes/world.py): its own data, the
    # same crossing as its memory.
    "/api/world": ("container.relay", "core.memory", None),
}

# HTTP method → CEIGAS operation
METHOD_OPERATION = {
    "GET": "read",
    "HEAD": "read",
    "POST": "write",
    "PUT": "write",
    "PATCH": "write",
    "DELETE": "delete",
}

# Paths that bypass CEIGAS (must always be accessible)
BYPASS_PATHS = {
    "/api/system/health",
    "/api/system/status",
    "/ws",
}
# "/api/system/health/" is the kubelet's readiness surface. BYPASS_PATHS above is
# an EXACT-match set, so it covers the 20s liveness probe on /api/system/health;
# the 5s readiness probe on /api/system/health/deep
# (controller/kopf_handlers/__init__.py) is a child path, so a PREFIX covers it
# and any later health sub-path. Probes must reach the pod with the switch on.
# The trailing slash is load-bearing: it matches the health endpoint's CHILDREN
# only, so a sibling route that merely starts with the word (a future
# /api/system/healthz) stays gated.
#
# "/api/connector/" is the messaging connectors' cross-pod surface: a
# connector app's pod
# with no entity domain, authenticated in-handler by the
# connector capability (routes/connector.require_connector_cap).
# "/api/owner/" is the owner's sign-in and setup (routes/owner.py): it has to
# work before anything else does, and it checks its own credentials.
BYPASS_PREFIXES = ("/api/system/health/", "/api/connector/", "/api/owner/")

# An app's OWN runtime pod and its two routes back to this kernel: GET
# /api/applications/<id>/source (its install dir, once at pod start) and POST
# /api/applications/<id>/runtime-log (what the app prints). The same posture as
# the editor harness lanes above: a cross-pod caller with no app window,
# authenticated in the handler by the per-app source token
# (harness_backend.check_app_source_token), and exempted from the localhost gate
# by the same two shapes (localhost_only._SELF_AUTH_SHAPES). Matched by shape
# because the app id is in the middle; the id must be ONE path segment, so
# nothing deeper slips through.
BYPASS_SHAPES = (("/api/applications/", "/source"),
                 ("/api/applications/", "/runtime-log"))


def _one_segment_between(path: str, prefix: str, suffix: str) -> bool:
    if path.startswith(prefix) and path.endswith(suffix):
        middle = path[len(prefix):len(path) - len(suffix)]
        return bool(middle) and "/" not in middle
    return False


def _bypass_shape(path: str) -> bool:
    return any(_one_segment_between(path, p, s) for p, s in BYPASS_SHAPES)


# App requests that only REDUCE running activity. The gate classes them as
# operation "stop" instead of "write", and that operation on the
# container.relay self-loop is the one crossing the kill switch lets through
# besides core.governance's (ceigas_kill_switch.KILL_SWITCH_STOP_CROSSINGS).
# With
# the switch on, closing an app and stopping or disabling its process still
# work; launch, install and every settings change stay refused.
#   POST /api/applications/<id>/close    releases a window's hold on the app's
#                                        process; the last close stops it
#   POST /api/applications/<id>/runtime  only when the body's op is stop or
#                                        disable (routes/applications.py
#                                        RUNTIME_STOP_OPS); start, enable and
#                                        status on the same route are writes
# (method, prefix, suffix, whether the body's runtime op must be a stop op)
STOP_OPERATION = "stop"
STOP_ROUTES = (
    ("POST", "/api/applications/", "/close", False),
    ("POST", "/api/applications/", "/runtime", True),
)
# A /runtime body is {"op": "..."}; anything larger is not read here and is
# not a stop.
_STOP_BODY_MAX = 4096


async def _runtime_op(request: Request) -> str | None:
    """The op the /runtime handler will act on, read the way it reads it
    (routes/applications.py runtime_op_of), or None when this cannot be sure.
    None is never a stop, so every doubt leaves the request a write."""
    from server.routes.applications import runtime_op_of
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        return None
    if not length or length > _STOP_BODY_MAX:
        return None
    try:
        body = json.loads(await request.body())
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    try:
        return runtime_op_of(body)
    except Exception:
        return None


async def _is_stop(request: Request, path: str) -> bool:
    for method, prefix, suffix, needs_stop_op in STOP_ROUTES:
        if request.method != method or not _one_segment_between(path, prefix, suffix):
            continue
        if not needs_stop_op:
            return True
        from server.routes.applications import RUNTIME_STOP_OPS
        return await _runtime_op(request) in RUNTIME_STOP_OPS
    return False


class CEIGASGateMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, entity_id: int = 0):
        super().__init__(app)
        self.entity_id = entity_id

    async def dispatch(self, request: Request, call_next):
        path = request.scope["path"]  # the routed path; request.url.path follows the Host header

        # Static files and bypass paths skip CEIGAS
        if not path.startswith("/api/") or path in BYPASS_PATHS \
                or path.startswith(BYPASS_PREFIXES) or _bypass_shape(path):
            return await call_next(request)

        # brain/ is on the kernel's PYTHONPATH; a module that cannot load is a
        # fault, and the gate fails closed rather than ungate every /api
        # crossing. Health/status/ws and static files bypassed above, so the
        # UI shell and probes stay up.
        try:
            from ceigas_kill_switch import KILL_SWITCH_REASON, is_kill_switch_active, kill_switch_refuses
        except Exception as e:
            logger.error("CEIGAS gate: kill-switch module unavailable (%s) — DENY path=%s", e, path)
            return JSONResponse(status_code=503,
                                content={"error": "governance temporarily unavailable"})

        # The switch off is the normal state: one stat, then the request.
        if not is_kill_switch_active():
            return await call_next(request)

        route_match = None
        for prefix, mapping in ROUTE_DOMAIN_MAP.items():
            if path.startswith(prefix):
                route_match = mapping
                break

        if not route_match:
            return await call_next(request)

        source_domain, target_domain, fixed_operation = route_match
        operation = fixed_operation or METHOD_OPERATION.get(request.method, "read")
        if await _is_stop(request, path):
            operation = STOP_OPERATION

        if kill_switch_refuses(source_domain, target_domain, operation):
            logger.warning(
                "CEIGAS DENIED: entity=%d path=%s reason=%s",
                self.entity_id, path, KILL_SWITCH_REASON,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error": "ceigas_denied",
                    "reason": KILL_SWITCH_REASON,
                    "conductance": 0.0,
                },
            )
        return await call_next(request)
