# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Entry point for local Google OAuth and hosted Cuan MCP modes."""

import os


def _runtime_mode() -> str:
    mode = os.environ.get("GOOGLE_ADS_MCP_MODE", "local")
    if mode not in {"local", "cuan"}:
        raise ValueError("GOOGLE_ADS_MCP_MODE must be 'local' or 'cuan'")
    return mode


def _create_local_server():
    # Keep local ADC and upstream FastMCP OAuth imports out of hosted Cuan mode.
    from ads_mcp.coordinator import mcp

    return mcp


def _create_cuan_server():
    if os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID") or os.environ.get(
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET"
    ):
        raise ValueError(
            "FastMCP OAuth must be unset when GOOGLE_ADS_MCP_MODE=cuan"
        )
    for name in (
        "CUAN_GOOGLE_ADS_RUNTIME_URL",
        "GOOGLE_ADS_PRIVATE_SERVICE_ID",
        "GOOGLE_ADS_PRIVATE_SERVICE_SECRET",
    ):
        if not os.environ.get(name):
            raise ValueError(
                f"{name} is required when GOOGLE_ADS_MCP_MODE=cuan"
            )

    from ads_mcp.hosted_server import create_hosted_server

    return create_hosted_server()


_IMPORTED_MODE = _runtime_mode()
mcp = (
    _create_cuan_server()
    if _IMPORTED_MODE == "cuan"
    else _create_local_server()
)

if _IMPORTED_MODE == "local":
    # These imports register upstream resources on the local coordinator.
    from ads_mcp.resources import (
        discovery,
        metrics,
        release_notes,
        segments,
    )  # noqa: F401


def run_server() -> None:
    mode = _runtime_mode()
    port = int(os.environ.get("PORT", "8080"))

    if mode == "cuan":
        server = mcp if _IMPORTED_MODE == "cuan" else _create_cuan_server()
        server.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            uvicorn_config={"access_log": False},
        )
    elif os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID") and os.environ.get(
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET"
    ):
        server = mcp if _IMPORTED_MODE == "local" else _create_local_server()
        server.run(
            transport="streamable-http",
            port=port,
            host="0.0.0.0",
            uvicorn_config={"access_log": False},
        )
    else:
        server = mcp if _IMPORTED_MODE == "local" else _create_local_server()
        server.run()


if __name__ == "__main__":
    run_server()
