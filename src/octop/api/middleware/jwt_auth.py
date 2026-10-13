"""Require JWT for all /api/* routes except an explicit allowlist.

Validated users are cached on ``request.state.octop_user`` so route-level
``Depends(current_user)`` can reuse the result without re-decoding.

When the access token is past the sliding-renew threshold, a fresh token is
attached as ``X-Octop-Access-Token`` on the response.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import (
    is_agentcomputer_path,
    octop_error_response,
    protocol_error_response,
)
from octop.api.deps import (
    ACCESS_TOKEN_RESPONSE_HEADER,
    authenticate_request,
    extract_raw_token,
    is_jwt_exempt_request,
    maybe_sliding_renew_token,
)
from octop.infra.errors import OctopError

_INSTALL_ATTR = "_octop_jwt_auth_installed"
logger = logging.getLogger(__name__)


def install(app: Any, server: Any) -> None:
    if getattr(app, _INSTALL_ATTR, False):
        return
    setattr(app, _INSTALL_ATTR, True)

    @app.middleware("http")  # type: ignore[untyped-decorator]
    async def _jwt_auth(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        path = request.url.path
        if not path.startswith("/api/") or is_jwt_exempt_request(request):
            return await call_next(request)

        agentcomputer = is_agentcomputer_path(path)
        if agentcomputer and (
            "access_token" in request.query_params
            or not extract_raw_token(authorization=request.headers.get("authorization"))
        ):
            return protocol_error_response("authenticationRequired", 401)
        if agentcomputer and server.agentcomputer_http_operations.closing:
            return protocol_error_response("runtimeUnavailable", 503)

        raw = extract_raw_token(
            authorization=request.headers.get("authorization"),
            access_token=request.query_params.get("access_token"),
        )
        try:
            request.state.octop_user = authenticate_request(request, server)
        except OctopError as exc:
            # Middleware returns directly (skips FastAPI exception handlers).
            if exc.status >= 500:
                logger.error(
                    "OctopError %s in %s: %s",
                    exc.code.value,
                    request.url.path,
                    exc.message,
                    exc_info=exc,
                )
            if agentcomputer:
                return octop_error_response(exc)
            return JSONResponse(status_code=exc.status, content=exc.to_envelope())

        response = await call_next(request)
        if raw is not None and not (agentcomputer and server.agentcomputer_http_operations.closing):
            try:
                renewed = maybe_sliding_renew_token(server, raw, request.state.octop_user)
            except OctopError:
                renewed = None
            if renewed:
                response.headers[ACCESS_TOKEN_RESPONSE_HEADER] = renewed
        return response
