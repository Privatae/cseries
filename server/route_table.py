"""The routes a FastAPI app dispatches to, read through FastAPI's public API.

Up to fastapi 0.136, include_router copied each route of the included router
onto app.routes with its prefix and dependencies applied, so app.routes was the
whole table. 0.137.0 keeps the included router itself in app.routes instead, and
FastAPI's release notes call router.routes an internal detail from then on.
0.137.2 added fastapi.routing.iter_route_contexts, which walks that tree and
yields one RouteContext per effective route: path, methods, endpoint and name
are its own properties, and any other attribute (include_in_schema, dependant)
is read from the effective route.

effective_routes() uses iter_route_contexts where FastAPI has it and app.routes
where it does not, so a caller reads the same attributes either way.

Before 0.143.1, iter_route_contexts reports an included route that is not an
APIRoute (a WebSocket route, a plain starlette Route, a Mount) with no path;
0.143.1 is the first release that reports the rebuilt route.
scripts/test_app_conformance.py checks an included WebSocket route, so a pin in
that range fails there instead of dropping those routes from the table.
"""
from __future__ import annotations


def effective_routes(app) -> list:
    """Every route `app` dispatches to, with include_router prefixes and
    dependencies applied, in dispatch order. Each item answers path, methods,
    endpoint, name, include_in_schema and dependant the way a route does; use
    declared_route() for an isinstance check."""
    routes = list(getattr(app, "routes", None) or [])
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return routes
    return list(iter_route_contexts(routes))


def declared_route(route):
    """The route object as it was declared (an APIRoute, an APIWebSocketRoute, a
    Mount), for an item effective_routes() returned. Its path is the declared
    one, without an include prefix; read the path from the item itself."""
    return getattr(route, "original_route", route)
