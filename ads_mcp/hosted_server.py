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

"""Strict HTTP-only tool surface backed by the Cuan private Google Ads runtime."""

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations

from ads_mcp.hosted_runtime import (
    CuanGoogleAdsRuntimeClient,
    GoogleAdsRestClient,
    GoogleAdsApiError,
    HostedGoogleAdsService,
    UnifiedGoogleAdsService,
)


def create_hosted_server(
    control: Any | None = None, ads: Any | None = None
) -> FastMCP:
    """Build a server that exposes only Cuan-authorized bounded tools."""
    if control is None:
        control = CuanGoogleAdsRuntimeClient(
            endpoint=os.environ["CUAN_GOOGLE_ADS_RUNTIME_URL"],
            service_id=os.environ["GOOGLE_ADS_PRIVATE_SERVICE_ID"],
            service_secret=os.environ["GOOGLE_ADS_PRIVATE_SERVICE_SECRET"],
        )
    if ads is None:
        ads = GoogleAdsRestClient()
    service = HostedGoogleAdsService(control, ads)
    redeem_url = os.environ.get("CUAN_GOOGLE_ADS_REDEEM_URL")
    if not redeem_url and os.environ.get("CUAN_GOOGLE_ADS_RUNTIME_URL"):
        redeem_url = os.environ["CUAN_GOOGLE_ADS_RUNTIME_URL"].rsplit("/", 1)[0] + "/mcp-redeem-google-permit"
    unified = UnifiedGoogleAdsService(redeem_url,
        os.environ["GOOGLE_ADS_PRIVATE_SERVICE_ID"],
        os.environ["GOOGLE_ADS_PRIVATE_SERVICE_SECRET"], ads) if redeem_url else None
    server = FastMCP("Cuan Google Ads")

    def connection_key() -> str:
        headers = get_http_headers(include={"x-cuan-mcp-connection-key"})
        return headers.get("x-cuan-mcp-connection-key", "")

    async def invoke_unified(tool: str, googleInvocation: dict[str, Any]) -> Any:
        if connection_key() or unified is None:
            raise ToolError("Cuan Google Ads private invocation denied")
        try:
            return await unified.invoke(tool, googleInvocation)
        except (ValueError, PermissionError, GoogleAdsApiError) as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError("Google Ads provider operation failed or outcome unknown") from exc

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def google_ads_list_customers(googleInvocation: dict[str, Any]) -> Any:
        """List only Google Ads customers allocated to this Cuan grant."""
        return await invoke_unified("google_ads_list_customers", googleInvocation)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def google_ads_list_campaigns(googleInvocation: dict[str, Any]) -> Any:
        """List campaigns for one allocated Google Ads customer."""
        return await invoke_unified("google_ads_list_campaigns", googleInvocation)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def google_ads_get_campaign_performance(googleInvocation: dict[str, Any]) -> Any:
        """Read bounded campaign performance from Google Ads."""
        return await invoke_unified("google_ads_get_campaign_performance", googleInvocation)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def google_ads_preview_campaign_rename(googleInvocation: dict[str, Any]) -> Any:
        """Preview a name-only update on an owned campaign."""
        return await invoke_unified("google_ads_preview_campaign_rename", googleInvocation)

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def google_ads_rename_campaign(googleInvocation: dict[str, Any]) -> Any:
        """Rename one owned campaign after central approval and claim."""
        return await invoke_unified("google_ads_rename_campaign", googleInvocation)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ads_list_campaigns(
        customer_id: str, page_size: int = 20
    ) -> list[dict[str, str]]:
        """List a bounded page of campaigns authorized by Cuan for this customer."""
        try:
            return await service.list_campaigns(
                connection_key(), customer_id, page_size
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(
                "Cuan Google Ads campaign access was denied"
            ) from exc

    @server.tool()
    async def ads_preview_campaign_rename(
        customer_id: str,
        campaign_id: str,
        expected_old_name: str,
        new_name: str,
    ) -> dict[str, Any]:
        """Prepare a five-minute rename preview for an authorized paused campaign."""
        try:
            return await service.preview_campaign_rename(
                connection_key(),
                customer_id,
                campaign_id,
                expected_old_name,
                new_name,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(
                "Cuan Google Ads rename preview was denied"
            ) from exc

    @server.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def ads_rename_campaign(
        customer_id: str,
        campaign_id: str,
        expected_old_name: str,
        new_name: str,
        preview_id: str,
        confirmation_token: str,
        expires_at: int,
        execution_id: str,
        confirmed: bool,
    ) -> dict[str, str]:
        """Rename a paused campaign after explicit confirmation and Cuan claim."""
        try:
            return await service.execute_campaign_rename(
                connection_key(),
                customer_id,
                campaign_id,
                expected_old_name,
                new_name,
                preview_id,
                confirmation_token,
                expires_at,
                execution_id,
                confirmed,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError("Cuan Google Ads rename execution failed") from exc

    # Expose the service only for in-process tests and diagnostics.
    server.hosted_service = service
    return server
