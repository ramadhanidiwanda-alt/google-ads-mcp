# Copyright 2026 Cuan Insight contributors.
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

"""Tests that hosted mode exposes only its bounded Cuan tool surface."""

import unittest
from unittest.mock import AsyncMock, patch

from fastmcp import Client
from ads_mcp.hosted_ingress import PrivateIngress
from ads_mcp.hosted_server import create_hosted_server


class HostedServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_lists_only_bounded_tools_and_uses_incoming_connection_key(
        self,
    ):
        control = AsyncMock()
        ads = AsyncMock()
        control.authorize.return_value = {
            "allowed": True,
            "provider": "google",
            "operation": "list_campaigns",
            "customerId": "1234567890",
            "scope": "https://www.googleapis.com/auth/adwords",
            "policyRevision": "policy-1",
            "grantId": "grant-1",
            "credentialRef": "credential-1",
        }
        control.resolve_credential.return_value = {"accessToken": "ephemeral"}
        ads.search_campaigns.return_value = []
        server = create_hosted_server(control, ads)

        with patch(
            "ads_mcp.hosted_server.get_http_headers",
            return_value={"x-cuan-mcp-connection-key": "connection-key"},
        ):
            async with Client(server) as client:
                tools = await client.list_tools()
                await client.call_tool(
                    "ads_list_campaigns",
                    {"customer_id": "1234567890", "page_size": 20},
                )

        self.assertEqual(
            {tool.name for tool in tools},
            {
                "ads_list_campaigns",
                "ads_preview_campaign_rename",
                "ads_rename_campaign",
                "google_ads_list_customers",
                "google_ads_list_campaigns",
                "google_ads_get_campaign_performance",
                "google_ads_preview_campaign_rename",
                "google_ads_rename_campaign",
            },
        )
        control.authorize.assert_awaited_once_with(
            "connection-key", "list_campaigns", "1234567890", None
        )


class PrivateIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_exact_host_and_private_secret(self):
        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        app = PrivateIngress(inner, "s" * 32, "google-ads-mcp:8080")

        async def status(headers):
            events = []

            async def send(event):
                events.append(event)

            async def receive():
                return {"type": "http.request", "body": b""}

            await app(
                {"type": "http", "headers": headers}, receive, send
            )
            return events[0]["status"]

        host = (b"host", b"google-ads-mcp:8080")
        proof = (b"x-cuan-google-ads-ingress-secret", b"s" * 32)
        self.assertEqual(await status([host, proof]), 200)
        self.assertEqual(await status([proof]), 403)
        self.assertEqual(await status([host]), 403)
        self.assertEqual(await status([host, host, proof]), 403)


if __name__ == "__main__":
    unittest.main()
