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

"""Tests for the hosted Cuan Google Ads runtime."""

import asyncio
import unittest
import json
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import URLError

from ads_mcp.hosted_runtime import (
    CuanGoogleAdsRuntimeClient,
    GoogleAdsRestClient,
    HostedGoogleAdsService,
    campaign_query,
    parse_customer_id,
    parse_page_size,
    rename_digest,
)


class HostedInputTests(unittest.TestCase):
    def test_customer_id_requires_exactly_ten_digits(self):
        self.assertEqual(parse_customer_id("1234567890"), "1234567890")
        for value in ("123-456-7890", "123456789", "12345678901", 1234567890):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_customer_id(value)

    def test_page_size_is_bounded(self):
        self.assertEqual(parse_page_size(1), 1)
        self.assertEqual(parse_page_size(100), 100)
        for value in (0, 101, -1, True, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_page_size(value)

    def test_campaign_query_is_fixed_and_uses_only_bounded_limit(self):
        self.assertEqual(
            campaign_query(10),
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.resource_name FROM campaign LIMIT 10",
        )

    def test_rest_boundary_rejects_arbitrary_gaql_before_network_dispatch(self):
        client = GoogleAdsRestClient(api_version="v25")
        with patch("ads_mcp.hosted_runtime.urlopen") as urlopen:
            with self.assertRaisesRegex(ValueError, "query is not allowed"):
                client._search_campaigns(
                    {"accessToken": "token"},
                    "1234567890",
                    "SELECT * FROM campaign",
                    10,
                )
        urlopen.assert_not_called()

    def test_rest_rename_updates_only_campaign_name_with_runtime_token(self):
        client = GoogleAdsRestClient(api_version="v25")
        response = BytesIO(
            b'{"mutateOperationResponses":[{"campaignResult":'
            b'{"resourceName":"customers/1234567890/campaigns/42"}}]}'
        )
        with patch(
            "ads_mcp.hosted_runtime.urlopen", return_value=nullcontext(response)
        ) as urlopen:
            result = client._rename_campaign(
                {
                    "accessToken": "ephemeral-token",
                    "developerToken": "developer-token",
                },
                "1234567890",
                "42",
                "Spring 2026",
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        update = payload["mutateOperations"][0]["campaignOperation"]
        self.assertEqual(result, "customers/1234567890/campaigns/42")
        self.assertEqual(update["updateMask"], "name")
        self.assertEqual(
            update["update"],
            {
                "resourceName": "customers/1234567890/campaigns/42",
                "name": "Spring 2026",
            },
        )
        self.assertEqual(
            request.get_header("Authorization"), "Bearer ephemeral-token"
        )
        self.assertEqual(
            request.get_header("Developer-token"), "developer-token"
        )

    def test_cuan_client_forwards_service_and_connection_auth_headers(self):
        response = BytesIO(b'{"allowed":true}')
        client = CuanGoogleAdsRuntimeClient(
            "https://cuan.example/functions/v1/google-ads-runtime",
            "google-ads-mcp",
            "s" * 40,
        )
        with patch(
            "ads_mcp.hosted_runtime.urlopen", return_value=nullcontext(response)
        ) as urlopen:
            result = asyncio.run(
                client._post(
                    "ci_mcp_ck_" + "a" * 64,
                    "authorize",
                    {"operation": "list_campaigns", "customerId": "1234567890"},
                )
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(result["allowed"], True)
        self.assertEqual(
            request.get_header("X-cuan-google-ads-service-id"), "google-ads-mcp"
        )
        self.assertEqual(
            request.get_header("X-cuan-google-ads-service-secret"), "s" * 40
        )
        self.assertEqual(
            request.get_header("X-cuan-mcp-connection-key"),
            "ci_mcp_ck_" + "a" * 64,
        )

    def test_cuan_client_rejects_invalid_connection_key_before_network(self):
        client = CuanGoogleAdsRuntimeClient(
            "https://cuan.example/functions/v1/google-ads-runtime",
            "google-ads-mcp",
            "s" * 40,
        )
        with patch("ads_mcp.hosted_runtime.urlopen") as urlopen:
            with self.assertRaisesRegex(PermissionError, "Connection Key"):
                asyncio.run(client._post("caller-value", "authorize", {}))
        urlopen.assert_not_called()

    def test_cuan_client_rejects_insecure_or_incomplete_configuration(self):
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            CuanGoogleAdsRuntimeClient(
                "http://cuan.example/runtime", "service", "s" * 40
            )
        with self.assertRaisesRegex(ValueError, "credentials are invalid"):
            CuanGoogleAdsRuntimeClient(
                "https://cuan.example/runtime", "service", "short"
            )

    def test_cuan_authorize_binds_campaign_id_when_present(self):
        client = CuanGoogleAdsRuntimeClient(
            "https://cuan.example/runtime", "service", "s" * 40
        )
        with patch.object(client, "_post", new_callable=AsyncMock) as post:
            post.return_value = {"allowed": True}
            result = asyncio.run(
                client.authorize(
                    "ci_mcp_ck_" + "a" * 64,
                    "rename_campaign",
                    "1234567890",
                    "42",
                )
            )
        self.assertTrue(result["allowed"])
        post.assert_awaited_once_with(
            "ci_mcp_ck_" + "a" * 64,
            "authorize",
            {
                "operation": "rename_campaign",
                "customerId": "1234567890",
                "campaignId": "42",
            },
        )

    def test_cuan_client_sanitizes_runtime_transport_failure(self):
        client = CuanGoogleAdsRuntimeClient(
            "https://cuan.example/runtime", "service", "s" * 40
        )
        with patch(
            "ads_mcp.hosted_runtime.urlopen",
            side_effect=URLError("private detail"),
        ):
            with self.assertRaisesRegex(
                PermissionError, "Cuan Google Ads runtime request failed"
            ) as error:
                asyncio.run(
                    client._post(
                        "ci_mcp_ck_" + "a" * 64,
                        "authorize",
                        {"operation": "list_campaigns"},
                    )
                )
        self.assertNotIn("private detail", str(error.exception))

    def test_rest_client_rejects_invalid_version_and_credentials(self):
        with self.assertRaisesRegex(ValueError, "API_VERSION is invalid"):
            GoogleAdsRestClient(api_version="latest")
        client = GoogleAdsRestClient(api_version="v25")
        with self.assertRaisesRegex(PermissionError, "credential was invalid"):
            client._headers({})
        with self.assertRaisesRegex(PermissionError, "manager customer ID"):
            client._headers({"accessToken": "token", "loginCustomerId": "123"})

    def test_rest_search_posts_only_fixed_query_and_bounds_response(self):
        client = GoogleAdsRestClient(api_version="v25")
        response = BytesIO(b'[{"results":[{"campaign":{"id":"42"}}]}]')
        with patch(
            "ads_mcp.hosted_runtime.urlopen", return_value=nullcontext(response)
        ) as urlopen:
            rows = client._search_campaigns(
                {"accessToken": "ephemeral"},
                "1234567890",
                campaign_query(1),
                1,
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(rows, [{"campaign": {"id": "42"}}])
        self.assertEqual(json.loads(request.data), {"query": campaign_query(1)})

    def test_rest_rename_rejects_unverified_response(self):
        client = GoogleAdsRestClient(api_version="v25")
        response = BytesIO(b'{"mutateOperationResponses":[]}')
        with patch(
            "ads_mcp.hosted_runtime.urlopen", return_value=nullcontext(response)
        ):
            with self.assertRaisesRegex(ValueError, "response was invalid"):
                client._rename_campaign(
                    {"accessToken": "ephemeral"},
                    "1234567890",
                    "42",
                    "Spring 2026",
                )


class HostedGoogleAdsServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.control = AsyncMock()
        self.ads = AsyncMock()
        self.grant = {
            "allowed": True,
            "provider": "google",
            "operation": "list_campaigns",
            "customerId": "1234567890",
            "scope": "https://www.googleapis.com/auth/adwords",
            "policyRevision": "policy-1",
            "grantId": "grant-1",
            "credentialRef": "credential-1",
        }
        self.control.authorize.return_value = self.grant
        self.control.resolve_credential.return_value = {
            "accessToken": "runtime-token"
        }
        self.ads.search_campaigns.return_value = [
            {
                "campaign": {
                    "id": "42",
                    "name": "Spring",
                    "status": "PAUSED",
                    "resourceName": "customers/1234567890/campaigns/42",
                }
            }
        ]
        self.now = 1_800_000_000_000
        self.service = HostedGoogleAdsService(
            self.control, self.ads, now_ms=lambda: self.now
        )

    async def test_list_campaigns_authorizes_resolves_and_uses_fixed_query(
        self,
    ):
        rows = await self.service.list_campaigns(
            "connection-key", "1234567890", page_size=10
        )
        self.assertEqual(rows[0]["id"], "42")
        self.control.authorize.assert_awaited_once_with(
            "connection-key", "list_campaigns", "1234567890", None
        )
        self.control.resolve_credential.assert_awaited_once_with(
            "connection-key", "list_campaigns", "1234567890", "credential-1"
        )
        self.ads.search_campaigns.assert_awaited_once_with(
            {"accessToken": "runtime-token"},
            "1234567890",
            campaign_query(10),
            10,
        )

    async def test_list_campaigns_rejects_customer_before_cuan_call(self):
        with self.assertRaises(ValueError):
            await self.service.list_campaigns("key", "123-456-7890")
        self.control.authorize.assert_not_awaited()

    async def test_list_campaigns_denies_mismatched_runtime_grant(self):
        self.control.authorize.return_value = {
            **self.grant,
            "customerId": "9999999999",
        }
        with self.assertRaises(PermissionError):
            await self.service.list_campaigns("key", "1234567890")
        self.control.resolve_credential.assert_not_awaited()

    async def test_list_campaigns_rejects_oversized_provider_response(self):
        self.ads.search_campaigns.return_value = [{"campaign": {}}] * 101
        with self.assertRaises(ValueError):
            await self.service.list_campaigns("key", "1234567890", 100)

    def configure_rename(self):
        self.grant.update({"operation": "rename_campaign", "campaignId": "42"})
        binding = {
            "customerId": "1234567890",
            "campaignId": "42",
            "expectedOldName": "Spring",
            "newName": "Spring 2026",
            "expiresAt": self.now + 300_000,
        }
        binding["requestDigest"] = rename_digest(binding, self.grant)
        self.control.authorize.return_value = self.grant
        self.control.resolve_credential.return_value = {
            "accessToken": "runtime-token"
        }
        self.control.issue_preview.return_value = {
            **binding,
            "previewId": "preview-1",
            "confirmationToken": "token_1234567890",
        }
        self.control.claim_execution.return_value = {
            **binding,
            "claimed": True,
            "previewId": "preview-1",
            "executionId": "exec-12345678",
        }
        self.control.finalize_execution.side_effect = lambda key, data: {
            **data,
            "acknowledged": True,
        }
        self.ads.search_campaigns.return_value = [
            {
                "campaign": {
                    "id": "42",
                    "name": "Spring",
                    "status": "PAUSED",
                    "resourceName": "customers/1234567890/campaigns/42",
                }
            }
        ]
        self.ads.rename_campaign.return_value = (
            "customers/1234567890/campaigns/42"
        )
        return binding

    async def test_preview_rename_binds_exact_paused_campaign(self):
        binding = self.configure_rename()
        preview = await self.service.preview_campaign_rename(
            "connection-key",
            "1234567890",
            "42",
            "Spring",
            "Spring 2026",
        )
        self.assertEqual(preview["status"], "PAUSED")
        self.assertEqual(preview["requestDigest"], binding["requestDigest"])
        self.control.issue_preview.assert_awaited_once_with(
            "connection-key", binding
        )

    async def test_rename_claims_then_dispatches_and_consumes_reservation(self):
        self.configure_rename()
        updated = {
            "campaign": {
                "id": "42",
                "name": "Spring 2026",
                "status": "PAUSED",
                "resourceName": "customers/1234567890/campaigns/42",
            }
        }
        self.ads.search_campaigns.side_effect = [
            [self.ads.search_campaigns.return_value[0]],
            [updated],
        ]
        result = await self.service.execute_campaign_rename(
            "connection-key",
            "1234567890",
            "42",
            "Spring",
            "Spring 2026",
            "preview-1",
            "token_1234567890",
            self.now + 300_000,
            "exec-12345678",
            True,
        )
        self.assertEqual(result["newName"], "Spring 2026")
        self.control.claim_execution.assert_awaited_once()
        self.ads.rename_campaign.assert_awaited_once()
        finalization = self.control.finalize_execution.await_args.args[1]
        self.assertEqual(finalization["outcome"], "confirmed")
        self.assertEqual(finalization["reservationDisposition"], "consume")

    async def test_unknown_rename_outcome_preserves_reservation(self):
        self.configure_rename()
        self.ads.rename_campaign.side_effect = TimeoutError("network timeout")
        with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
            await self.service.execute_campaign_rename(
                "connection-key",
                "1234567890",
                "42",
                "Spring",
                "Spring 2026",
                "preview-1",
                "token_1234567890",
                self.now + 300_000,
                "exec-12345678",
                True,
            )
        finalization = self.control.finalize_execution.await_args.args[1]
        self.assertEqual(finalization["outcome"], "uncertain")
        self.assertEqual(finalization["reservationDisposition"], "preserve")

    async def test_pre_dispatch_stale_name_releases_reservation(self):
        self.configure_rename()
        self.ads.search_campaigns.return_value[0]["campaign"][
            "name"
        ] = "Changed"
        with self.assertRaisesRegex(PermissionError, "name changed"):
            await self.service.execute_campaign_rename(
                "connection-key",
                "1234567890",
                "42",
                "Spring",
                "Spring 2026",
                "preview-1",
                "token_1234567890",
                self.now + 300_000,
                "exec-12345678",
                True,
            )
        self.ads.rename_campaign.assert_not_awaited()
        finalization = self.control.finalize_execution.await_args.args[1]
        self.assertEqual(finalization["outcome"], "not_dispatched")
        self.assertEqual(finalization["reservationDisposition"], "release")


if __name__ == "__main__":
    unittest.main()
