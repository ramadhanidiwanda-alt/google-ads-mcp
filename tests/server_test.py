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

"""Test cases for the server module."""

import logging
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch


class TestUtils(unittest.TestCase):
    """Test cases for the server module."""

    def test_server_initialization(self):
        """Tests that the MCP server instance is initialized.

        This servers as a smoke test to confirm there are no obvious issues
        with initialization, such as missing imports.
        """
        from ads_mcp import server

        self.assertIsNotNone(server.mcp, "MCP server instance not initialized")

    def test_oauth_server_uses_stateful_streamable_http(self):
        """OAuth mode supports modern and legacy Streamable HTTP clients."""
        from ads_mcp import server

        env = {
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "test-client",
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "test-secret",
            "PORT": "18080",
        }
        with patch.dict(server.os.environ, env, clear=True):
            with patch.object(server.mcp, "run") as run:
                server.run_server()

        run.assert_called_once_with(
            transport="streamable-http",
            port=18080,
            host="0.0.0.0",
            uvicorn_config={"access_log": False},
        )

    def test_oauth_access_token_not_logged(self):
        """Tests that OAuth access token is not logged."""
        import asyncio
        import httpx2
        from fastmcp.server.auth.providers.google import GoogleTokenVerifier

        token = "secret-token"
        # Include audience (aud) and subject (sub) claims in the mocked tokeninfo response
        # since they're required by fastmcp's GoogleTokenVerifier's verify_token to make the userinfo request
        # https://github.com/PrefectHQ/fastmcp/blob/490049f0f9742922f4af16c937db0b898dc9802b/fastmcp_slim/fastmcp/server/auth/providers/google.py#L132-L150
        transport = httpx2.MockTransport(
            lambda _: httpx2.Response(200, json={"aud": "aud", "sub": "sub"})
        )
        verifier = GoogleTokenVerifier(
            http_client=httpx2.AsyncClient(transport=transport)
        )

        with self.assertLogs(level=logging.DEBUG) as logs:
            asyncio.run(verifier.verify_token(token))

        for line in logs.output:
            self.assertNotIn(token, line)

    def test_server_without_oauth_uses_stdio(self):
        """Local credential mode retains FastMCP's default stdio transport."""
        from ads_mcp import server

        with patch.dict(server.os.environ, {}, clear=True):
            with patch.object(server.mcp, "run") as run:
                server.run_server()

        run.assert_called_once_with()

    def test_cuan_mode_uses_streamable_http_without_upstream_oauth(self):
        from ads_mcp import server

        env = {
            "GOOGLE_ADS_MCP_MODE": "cuan",
            "CUAN_GOOGLE_ADS_RUNTIME_URL": "https://cuan.example/functions/v1/google-ads-runtime",
            "GOOGLE_ADS_PRIVATE_SERVICE_ID": "ads-mcp",
            "GOOGLE_ADS_PRIVATE_SERVICE_SECRET": "s" * 40,
            "GOOGLE_ADS_INGRESS_SECRET": "i" * 40,
            "PORT": "18081",
        }
        hosted = Mock()
        with patch.dict(server.os.environ, env, clear=True):
            with patch.object(
                server, "_create_cuan_server", return_value=hosted
            ), patch("uvicorn.run") as run:
                server.run_server()

        hosted.http_app.assert_called_once_with()
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["port"], 18081)

    def test_cuan_mode_rejects_fastmcp_oauth_configuration(self):
        from ads_mcp import server

        env = {
            "GOOGLE_ADS_MCP_MODE": "cuan",
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "local-client",
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "local-secret",
        }
        with patch.dict(server.os.environ, env, clear=True):
            with self.assertRaisesRegex(
                ValueError, "FastMCP OAuth must be unset"
            ):
                server._create_cuan_server()

    def test_fresh_cuan_process_never_imports_local_coordinator(self):
        env = os.environ.copy()
        env.update(
            {
                "GOOGLE_ADS_MCP_MODE": "cuan",
                "CUAN_GOOGLE_ADS_RUNTIME_URL": "https://cuan.example/functions/v1/google-ads-runtime",
                "GOOGLE_ADS_PRIVATE_SERVICE_ID": "google-ads-mcp",
                "GOOGLE_ADS_PRIVATE_SERVICE_SECRET": "s" * 40,
                "GOOGLE_ADS_INGRESS_SECRET": "i" * 40,
            }
        )
        env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID", None)
        env.pop("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import ads_mcp.server; "
                "assert 'ads_mcp.coordinator' not in sys.modules",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
