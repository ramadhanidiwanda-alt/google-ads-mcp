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

"""Cuan-owned credentials and bounded Google Ads operations for HTTP mode."""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
CAMPAIGN_FIELDS = (
    "campaign.id, campaign.name, campaign.status, campaign.resource_name"
)
CUSTOMER_ID = re.compile(r"^\d{10}$")
CAMPAIGN_ID = re.compile(r"^[1-9]\d{0,18}$")
CONNECTION_KEY = re.compile(r"^ci_mcp_ck_[0-9a-f]{64}$")
OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
EXECUTION_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PREVIEW_TTL_MS = 5 * 60 * 1000


class GoogleAdsApiError(RuntimeError):
    """A bounded provider error containing no messages, queries or credentials."""

    @classmethod
    def from_http_error(cls, error: HTTPError) -> "GoogleAdsApiError":
        codes = []
        try:
            payload = json.loads(error.read(65537))
            if isinstance(payload, list) and len(payload) == 1:
                payload = payload[0]
            detail = payload.get("error", {}) if isinstance(payload, dict) else {}
            status = detail.get("status") if isinstance(detail, dict) else None
            if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status):
                codes.append(status)
            details = detail.get("details", []) if isinstance(detail, dict) else []
            for item in details[:8] if isinstance(details, list) else []:
                reason = item.get("reason") if isinstance(item, dict) else None
                if isinstance(reason, str) and re.fullmatch(r"[A-Z_]{1,100}", reason):
                    codes.append(reason)
                errors = item.get("errors", []) if isinstance(item, dict) else []
                for failure in errors[:8] if isinstance(errors, list) else []:
                    enum = failure.get("errorCode", {}) if isinstance(failure, dict) else {}
                    for category, value in enum.items() if isinstance(enum, dict) else []:
                        if (isinstance(category, str) and re.fullmatch(r"[a-zA-Z]{1,64}", category)
                                and isinstance(value, str) and re.fullmatch(r"[A-Z_]{1,100}", value)):
                            codes.append(f"{category}.{value}")
        except (ValueError, OSError):
            pass
        return cls(f"Google Ads API HTTP {error.code}" +
                   (" " + " ".join(dict.fromkeys(codes)) if codes else ""))


def parse_customer_id(value: Any) -> str:
    """Validate a customer ID without silently normalizing caller input."""
    if not isinstance(value, str) or not CUSTOMER_ID.fullmatch(value):
        raise ValueError("customer_id must be exactly ten digits")
    return value


def parse_page_size(value: Any) -> int:
    """Keep hosted report reads bounded to a single page of at most 100 rows."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 100
    ):
        raise ValueError("page_size must be an integer from 1 to 100")
    return value


def campaign_query(page_size: int) -> str:
    """Build the only GAQL query exposed by hosted mode."""
    size = parse_page_size(page_size)
    return f"SELECT {CAMPAIGN_FIELDS} FROM campaign LIMIT {size}"


def campaign_lookup_query(campaign_id: str) -> str:
    """Build a lookup for one validated campaign, never caller-supplied GAQL."""
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID.fullmatch(
        campaign_id
    ):
        raise ValueError("campaign_id is invalid")
    return (
        f"SELECT {CAMPAIGN_FIELDS} FROM campaign "
        f"WHERE campaign.id = {campaign_id} LIMIT 2"
    )


def _valid_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 255
        and value.strip() == value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rename_digest(binding: dict[str, Any], grant: dict[str, Any]) -> str:
    """Match the Cuan Edge/Adstream canonical rename digest exactly."""
    values = (
        binding.get("customerId"),
        binding.get("campaignId"),
        binding.get("expectedOldName"),
        binding.get("newName"),
        grant.get("policyRevision"),
        grant.get("grantId"),
        grant.get("credentialRef"),
        binding.get("expiresAt"),
    )
    if not all(isinstance(value, str) and value for value in values[:-1]):
        raise ValueError("Cuan rename grant reference is incomplete")
    if isinstance(values[-1], bool) or not isinstance(values[-1], int):
        raise ValueError("Cuan rename expiry is invalid")
    canonical = [
        "rename_campaign",
        binding["customerId"],
        binding["campaignId"],
        binding["expectedOldName"],
        binding["newName"],
        grant["policyRevision"],
        grant["grantId"],
        grant["credentialRef"],
        binding["expiresAt"],
    ]
    return hashlib.sha256(_canonical_json(canonical).encode()).hexdigest()


class CuanGoogleAdsRuntimeClient:
    """Call the private Google Ads runtime without persisting provider tokens."""

    def __init__(
        self,
        endpoint: str,
        service_id: str,
        service_secret: str,
        timeout: float = 10.0,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("CUAN_GOOGLE_ADS_RUNTIME_URL must use HTTPS")
        if not service_id or len(service_id) > 128 or len(service_secret) < 32:
            raise ValueError(
                "Cuan Google Ads private service credentials are invalid"
            )
        self.endpoint = endpoint
        self.service_id = service_id
        self.service_secret = service_secret
        self.timeout = timeout

    async def _post(
        self, connection_key: str, action: str, request_data: dict[str, Any]
    ) -> dict[str, Any]:
        if not CONNECTION_KEY.fullmatch(connection_key):
            raise PermissionError("Cuan Connection Key is missing or invalid")
        body = json.dumps({"action": action, "request": request_data}).encode()
        headers = {
            "content-type": "application/json",
            "x-cuan-google-ads-service-id": self.service_id,
            "x-cuan-google-ads-service-secret": self.service_secret,
            "x-cuan-mcp-connection-key": connection_key,
        }
        req = Request(self.endpoint, data=body, headers=headers, method="POST")

        def send() -> dict[str, Any]:
            try:
                with urlopen(req, timeout=self.timeout) as response:
                    result = json.loads(response.read())
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                raise PermissionError(
                    "Cuan Google Ads runtime request failed"
                ) from exc
            if not isinstance(result, dict) or result.get("ok") is False:
                raise PermissionError(
                    "Cuan Google Ads runtime denied the request"
                )
            return result

        return await asyncio.to_thread(send)

    async def authorize(
        self,
        connection_key: str,
        operation: str,
        customer_id: str,
        campaign_id: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": operation,
            "customerId": customer_id,
        }
        if campaign_id is not None:
            payload["campaignId"] = campaign_id
        return await self._post(connection_key, "authorize", payload)

    async def resolve_credential(
        self,
        connection_key: str,
        operation: str,
        customer_id: str,
        credential_ref: str,
    ) -> dict[str, Any]:
        return await self._post(
            connection_key,
            "resolveCredential",
            {
                "provider": "google",
                "operation": operation,
                "customerId": customer_id,
                "credentialRef": credential_ref,
            },
        )

    async def issue_preview(
        self, connection_key: str, binding: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post(connection_key, "issuePreview", binding)

    async def claim_execution(
        self, connection_key: str, request_data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post(connection_key, "claimExecution", request_data)

    async def finalize_execution(
        self, connection_key: str, request_data: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post(
            connection_key, "finalizeExecution", request_data
        )


class GoogleAdsRestClient:
    """Minimal REST client; accepts only a server-built campaign query."""

    def __init__(self, api_version: str | None = None) -> None:
        self.api_version = api_version or os.environ.get(
            "GOOGLE_ADS_API_VERSION", "v25"
        )
        if not re.fullmatch(r"v\d+", self.api_version):
            raise ValueError("GOOGLE_ADS_API_VERSION is invalid")

    async def search_campaigns(
        self,
        credential: dict[str, Any],
        customer_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._search_campaigns, credential, customer_id, query, limit
        )

    def _headers(self, credential: dict[str, Any]) -> dict[str, str]:
        access_token = credential.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise PermissionError("Cuan provider credential was invalid")
        headers = {
            "authorization": f"Bearer {access_token}",
            "content-type": "application/json",
        }
        developer_token = credential.get("developerToken")
        if developer_token:
            if not isinstance(developer_token, str) or "\n" in developer_token:
                raise PermissionError(
                    "Cuan Google Ads developer token was invalid"
                )
            headers["developer-token"] = developer_token
        login_customer_id = credential.get("loginCustomerId")
        if login_customer_id:
            if not isinstance(
                login_customer_id, str
            ) or not CUSTOMER_ID.fullmatch(login_customer_id):
                raise PermissionError("Cuan manager customer ID was invalid")
            headers["login-customer-id"] = login_customer_id
        return headers

    async def search_performance(self, credential: dict[str, Any], customer_id: str,
                                 since: str, until: str, limit: int, page_token: str = "",
                                 offset: int = 0) -> dict[str, Any]:
        # Search has fixed 10,000-row pages. Keep the query identical between
        # pages and expose bounded subpages through our stateless cursor.
        query = ("SELECT segments.date, campaign.id, campaign.name, "
                 "metrics.cost_micros, customer.currency_code, metrics.clicks, "
                 "metrics.impressions FROM campaign WHERE segments.date BETWEEN "
                 f"'{since}' AND '{until}' ORDER BY segments.date ASC, campaign.id ASC")
        return await asyncio.to_thread(self._search_performance, credential,
                                       customer_id, query, limit, page_token, offset)

    def _search_performance(self, credential: dict[str, Any], customer_id: str,
                            query: str, limit: int, page_token: str,
                            offset: int) -> dict[str, Any]:
        customer_id = parse_customer_id(customer_id)
        if (type(limit) is not int or not 1 <= limit <= 1000 or
                type(offset) is not int or not 0 <= offset < 10000 or
                not isinstance(page_token, str) or len(page_token) > 1024):
            raise ValueError("Google Ads report page invalid")
        body = {"query": query}
        if page_token:
            body["pageToken"] = page_token
        request = Request(
            f"https://googleads.googleapis.com/{self.api_version}/customers/{customer_id}/googleAds:search",
            data=json.dumps(body).encode(), headers=self._headers(credential), method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Google Ads report response too large")
                payload = json.loads(raw)
        except HTTPError as exc:
            raise GoogleAdsApiError.from_http_error(exc) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("Google Ads performance request failed") from exc
        rows = payload.get("results", []) if isinstance(payload, dict) else None
        next_token = payload.get("nextPageToken", "") if isinstance(payload, dict) else None
        if (not isinstance(rows, list) or len(rows) > 10000 or
                any(not isinstance(row, dict) for row in rows) or
                not isinstance(next_token, str) or len(next_token) > 1024 or
                (next_token and next_token == page_token) or offset > len(rows)):
            raise ValueError("Google Ads performance response invalid")
        end = min(offset + limit, len(rows))
        next_page = {"token": page_token, "offset": end} if end < len(rows) else (
            {"token": next_token, "offset": 0} if next_token else None)
        return {"rows": rows[offset:end], "next": next_page}


    def _search_campaigns(
        self,
        credential: dict[str, Any],
        customer_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        customer_id = parse_customer_id(customer_id)
        limit = parse_page_size(limit)
        # Rebuild and compare the query at the network boundary, so even an
        # accidental caller cannot turn hosted mode into arbitrary GAQL.
        is_list_query = query == campaign_query(limit)
        is_lookup_query = (
            limit == 2 and query == campaign_lookup_query_from_query(query)
        )
        if not (is_list_query or is_lookup_query):
            raise ValueError("hosted campaign query is not allowed")
        request = Request(
            f"https://googleads.googleapis.com/{self.api_version}/customers/"
            f"{customer_id}/googleAds:searchStream",
            data=json.dumps({"query": query}).encode(),
            headers=self._headers(credential),
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise GoogleAdsApiError.from_http_error(exc) from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("Google Ads campaign list failed") from exc
        if not isinstance(payload, list) or any(
            not isinstance(chunk, dict)
            or not isinstance(chunk.get("results", []), list)
            for chunk in payload
        ):
            raise ValueError("Google Ads campaign response was invalid")
        rows = [row for chunk in payload for row in chunk.get("results", [])]
        if len(rows) > limit:
            raise ValueError("Google Ads returned more than the requested page")
        return rows

    async def rename_campaign(
        self,
        credential: dict[str, Any],
        customer_id: str,
        campaign_id: str,
        new_name: str,
    ) -> str:
        return await asyncio.to_thread(
            self._rename_campaign,
            credential,
            customer_id,
            campaign_id,
            new_name,
        )

    def _rename_campaign(
        self,
        credential: dict[str, Any],
        customer_id: str,
        campaign_id: str,
        new_name: str,
    ) -> str:
        customer_id = parse_customer_id(customer_id)
        if not CAMPAIGN_ID.fullmatch(campaign_id) or not _valid_name(new_name):
            raise ValueError("campaign rename input is invalid")
        resource_name = f"customers/{customer_id}/campaigns/{campaign_id}"
        request = Request(
            f"https://googleads.googleapis.com/{self.api_version}/customers/"
            f"{customer_id}/googleAds:mutate",
            data=json.dumps(
                {
                    "mutateOperations": [
                        {
                            "campaignOperation": {
                                "update": {
                                    "resourceName": resource_name,
                                    "name": new_name,
                                },
                                "updateMask": "name",
                            }
                        }
                    ],
                    "partialFailure": False,
                }
            ).encode(),
            headers=self._headers(credential),
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("Google Ads campaign rename failed") from exc
        responses = (
            payload.get("mutateOperationResponses")
            if isinstance(payload, dict)
            else None
        )
        if (
            not isinstance(responses, list)
            or len(responses) != 1
            or not isinstance(responses[0], dict)
            or not isinstance(responses[0].get("campaignResult"), dict)
            or responses[0]["campaignResult"].get("resourceName")
            != resource_name
        ):
            raise ValueError("Google Ads rename response was invalid")
        return resource_name


def campaign_lookup_query_from_query(query: str) -> str | None:
    """Recognize and reconstruct the exact campaign lookup form."""
    match = re.fullmatch(
        rf"SELECT {re.escape(CAMPAIGN_FIELDS)} FROM campaign "
        r"WHERE campaign\.id = ([1-9]\d{0,18}) LIMIT 2",
        query,
    )
    return campaign_lookup_query(match.group(1)) if match else None


class UnifiedGoogleAdsService:
    """Execute one centrally admitted invocation with one permit redemption."""

    TOOLS = {
        "google_ads_list_customers", "google_ads_list_campaigns",
        "google_ads_get_campaign_performance", "google_ads_preview_campaign_rename",
        "google_ads_rename_campaign",
    }

    def __init__(self, endpoint: str, service_id: str, service_secret: str,
                 ads: Any) -> None:
        if not endpoint.startswith("https://") or not service_id or len(service_secret) < 32:
            raise ValueError("Cuan Google Ads redeem configuration invalid")
        self.endpoint, self.service_id, self.service_secret = endpoint, service_id, service_secret
        self.ads = ads

    async def _redeem(self, invocation: dict[str, Any]) -> dict[str, Any]:
        req = Request(self.endpoint, data=json.dumps({"googleInvocation": invocation}).encode(),
                      headers={"content-type": "application/json",
                               "x-cuan-google-ads-service-id": self.service_id,
                               "x-cuan-google-ads-service-secret": self.service_secret}, method="POST")
        def send() -> dict[str, Any]:
            try:
                with urlopen(req, timeout=10) as response:
                    result = json.loads(response.read())
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                raise PermissionError("Cuan Google Ads permit redemption failed") from exc
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise PermissionError("Cuan Google Ads permit denied")
            return result
        return await asyncio.to_thread(send)

    async def _finalize(self, invocation: dict[str, Any], outcome: str) -> None:
        endpoint = self.endpoint.rsplit("/", 1)[0] + "/mcp-finalize-execution"
        body = json.dumps({"executionId": invocation["executionId"],
                           "permit": invocation["permit"], "outcome": outcome}).encode()
        req = Request(endpoint, data=body, headers={"content-type": "application/json",
            "x-cuan-google-ads-service-id": self.service_id,
            "x-cuan-google-ads-service-secret": self.service_secret}, method="POST")
        def send() -> None:
            try:
                with urlopen(req, timeout=10) as response:
                    ack = json.loads(response.read())
            except (HTTPError, URLError, TimeoutError, ValueError) as exc:
                raise RuntimeError("Cuan Google Ads finalization unavailable; inspect before retrying") from exc
            if not isinstance(ack, dict) or ack.get("ok") is not True:
                raise RuntimeError("Cuan Google Ads finalization denied; inspect before retrying")
        await asyncio.to_thread(send)

    @staticmethod
    def _input(invocation: Any, tool: str) -> dict[str, Any]:
        if not isinstance(invocation, dict) or set(invocation) != {
            "version", "publicTool", "provider", "resourceId", "canonicalArgumentsJson",
            "digest", "executionId", "permit",
        } or invocation.get("version") != 1 or invocation.get("publicTool") != tool or \
            invocation.get("provider") != "google" or \
            not isinstance(invocation.get("resourceId"), str) or \
            not isinstance(invocation.get("canonicalArgumentsJson"), str) or \
            not isinstance(invocation.get("digest"), str) or \
            not re.fullmatch(r"[0-9a-f]{64}", invocation["digest"]) or \
            not isinstance(invocation.get("permit"), str) or \
            not re.fullmatch(r"[A-Za-z0-9_-]{43}", invocation["permit"]) or \
            not isinstance(invocation.get("executionId"), str) or \
            not EXECUTION_ID.fullmatch(invocation["executionId"]):
            raise PermissionError("Cuan Google Ads invocation invalid")
        raw = invocation["canonicalArgumentsJson"].encode("utf-8")
        if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != invocation["digest"]:
            raise PermissionError("Cuan Google Ads argument digest mismatch")
        try:
            args = json.loads(raw)
        except ValueError as exc:
            raise ValueError("Google Ads arguments invalid") from exc
        if not isinstance(args, dict):
            raise ValueError("Google Ads arguments invalid")
        if tool != "google_ads_list_customers" and args.get("accountId") != invocation["resourceId"]:
            raise PermissionError("Google Ads target mismatch")
        if tool == "google_ads_list_customers" and invocation["resourceId"]:
            raise PermissionError("Google Ads discovery target invalid")
        return args

    async def invoke(self, tool: str, invocation: Any) -> Any:
        state = {"validated": False, "redeemed": False, "mutation_dispatched": False}
        try:
            result = await self._invoke(tool, invocation, state)
        except Exception:
            if state["validated"]:
                await self._finalize(invocation, "failed_after_dispatch" if state["mutation_dispatched"] else "failed_before_dispatch")
            raise
        if state["redeemed"]:
            await self._finalize(invocation, "succeeded")
        return result

    async def _invoke(self, tool: str, invocation: Any, state: dict[str, bool]) -> Any:
        if tool not in self.TOOLS:
            raise ValueError("Google Ads tool unsupported")
        args = self._input(invocation, tool)
        state["validated"] = True
        redeemed = await self._redeem(invocation)
        state["redeemed"] = True
        if redeemed.get("provider") != "google" or \
            redeemed.get("resourceId") != invocation["resourceId"]:
            raise PermissionError("Cuan Google Ads redeemed target mismatch")
        if tool == "google_ads_list_customers":
            resources = redeemed.get("resources")
            if not isinstance(resources, list):
                raise PermissionError("Cuan Google Ads grants unavailable")
            return {"customers": resources}
        token = redeemed.get("accessToken")
        if not isinstance(token, str) or not token or len(token) > 8192:
            raise PermissionError("Cuan Google Ads token unavailable")
        customer = parse_customer_id(invocation["resourceId"])
        if redeemed.get("providerTarget") not in (None, customer):
            raise PermissionError("Google Ads provider target mismatch")
        credential = {"accessToken": token,
                      "loginCustomerId": redeemed.get("loginCustomerId")}
        if tool == "google_ads_list_campaigns":
            size = parse_page_size(args.get("pageSize", 20))
            rows = await self.ads.search_campaigns(credential, customer, campaign_query(size), size)
            return {"campaigns": [HostedGoogleAdsService._parse_campaign(row, customer) for row in rows]}
        if tool == "google_ads_get_campaign_performance":
            since, until = args.get("since"), args.get("until")
            try:
                start, end = date.fromisoformat(since), date.fromisoformat(until)
            except (TypeError, ValueError) as exc:
                raise ValueError("Google Ads report dates invalid") from exc
            if end < start or (end - start).days >= 31:
                raise ValueError("Google Ads report range exceeds 31 days")
            limit = args.get("limit", 100)
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError("Google Ads report limit invalid")
            prefix = hashlib.sha256(f"{customer}:{since}:{until}:{limit}".encode()).hexdigest()[:16]
            cursor = args.get("cursor")
            page_token, offset = "", 0
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor.startswith(prefix + ":") or len(cursor) > 2048:
                    raise ValueError("Google Ads report cursor invalid")
                encoded = cursor[len(prefix) + 1:]
                try:
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
                        raise ValueError()
                    page = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                    if (not isinstance(page, dict) or set(page) != {"token", "offset"} or
                            not isinstance(page["token"], str) or len(page["token"]) > 1024 or
                            type(page["offset"]) is not int or not 0 <= page["offset"] < 10000):
                        raise ValueError()
                    page_token, offset = page["token"], page["offset"]
                except (ValueError, TypeError) as exc:
                    raise ValueError("Google Ads report cursor invalid") from exc
            page = await self.ads.search_performance(credential, customer, since, until,
                                                     limit, page_token, offset)
            next_cursor = None
            if page["next"] is not None:
                encoded = base64.urlsafe_b64encode(json.dumps(page["next"],
                    separators=(",", ":")).encode()).decode().rstrip("=")
                next_cursor = prefix + ":" + encoded
            return {"rows": page["rows"], "nextCursor": next_cursor}

        campaign_id = args.get("campaignId")
        expected = args.get("expectedOldName")
        proposed = args.get("newName")
        HostedGoogleAdsService._validate_rename(customer, campaign_id, expected, proposed)
        service = HostedGoogleAdsService(None, self.ads)
        try:
            current = await service._read_campaign(credential, customer, campaign_id)
            service._require_rename_preconditions(current, expected)
        except Exception:
            raise
        if tool == "google_ads_preview_campaign_rename":
            if not isinstance(redeemed.get("previewId"), str) or not redeemed["previewId"] or \
                not isinstance(redeemed.get("approvalDigest"), str) or \
                not re.fullmatch(r"[0-9a-f]{64}", redeemed["approvalDigest"]):
                raise PermissionError("Cuan Google Ads preview binding unavailable")
            return {"customerId": customer, "campaignId": campaign_id, "oldName": expected,
                    "newName": proposed, "status": current["status"], "requiresConfirmation": True,
                    "previewId": redeemed["previewId"], "approvalDigest": redeemed["approvalDigest"]}
        if args.get("confirmed") is not True:
            raise PermissionError("Explicit Google Ads rename confirmation required")
        if redeemed.get("previewId") != args.get("previewId") or \
            redeemed.get("approvalDigest") != args.get("approvalDigest") or \
            not isinstance(args.get("previewId"), str) or \
            not isinstance(args.get("approvalDigest"), str) or \
            not re.fullmatch(r"[0-9a-f]{64}", args["approvalDigest"]):
            raise PermissionError("Cuan Google Ads rename claim mismatch")
        try:
            state["mutation_dispatched"] = True
            resource = await self.ads.rename_campaign(credential, customer, campaign_id, proposed)
            updated = await service._read_campaign(credential, customer, campaign_id)
        except Exception as exc:
            raise RuntimeError("Google Ads rename outcome unknown; inspect campaign before retrying") from exc
        if resource != f"customers/{customer}/campaigns/{campaign_id}" or \
            updated["name"] != proposed or updated["status"] != current["status"]:
            raise RuntimeError("Google Ads rename outcome unknown; inspect campaign before retrying")
        return {"customerId": customer, "campaignId": campaign_id, "oldName": expected,
                "newName": proposed, "status": updated["status"], "executionId": invocation["executionId"]}


class HostedGoogleAdsService:
    """Applies Cuan policy before using a short-lived Google Ads credential."""

    def __init__(self, control: Any, ads: Any, now_ms: Any = None) -> None:
        self.control = control
        self.ads = ads
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    async def list_campaigns(
        self, connection_key: str, customer_id: str, page_size: int = 20
    ) -> list[dict[str, str]]:
        customer_id = parse_customer_id(customer_id)
        page_size = parse_page_size(page_size)
        grant = await self.control.authorize(
            connection_key, "list_campaigns", customer_id, None
        )
        self._validate_grant(grant, "list_campaigns", customer_id, None)
        credential = await self.control.resolve_credential(
            connection_key,
            "list_campaigns",
            customer_id,
            grant["credentialRef"],
        )
        if not isinstance(credential, dict) or not isinstance(
            credential.get("accessToken"), str
        ):
            raise PermissionError("Cuan provider credential was invalid")
        rows = await self.ads.search_campaigns(
            credential, customer_id, campaign_query(page_size), page_size
        )
        if not isinstance(rows, list) or len(rows) > page_size:
            raise ValueError("Google Ads returned an invalid campaign page")
        return [self._parse_campaign(row, customer_id) for row in rows]

    async def preview_campaign_rename(
        self,
        connection_key: str,
        customer_id: str,
        campaign_id: str,
        expected_old_name: str,
        new_name: str,
    ) -> dict[str, Any]:
        customer_id, campaign_id = self._validate_rename(
            customer_id, campaign_id, expected_old_name, new_name
        )
        grant = await self.control.authorize(
            connection_key, "rename_campaign", customer_id, campaign_id
        )
        self._validate_grant(grant, "rename_campaign", customer_id, campaign_id)
        credential = await self.control.resolve_credential(
            connection_key,
            "rename_campaign",
            customer_id,
            grant["credentialRef"],
        )
        if not isinstance(credential, dict) or not isinstance(
            credential.get("accessToken"), str
        ):
            raise PermissionError("Cuan provider credential was invalid")
        refreshed_grant = await self.control.authorize(
            connection_key, "rename_campaign", customer_id, campaign_id
        )
        self._validate_grant(
            refreshed_grant, "rename_campaign", customer_id, campaign_id
        )
        if any(
            refreshed_grant[key] != grant[key]
            for key in ("grantId", "credentialRef")
        ):
            raise PermissionError("Cuan Google Ads grant changed during preview")
        grant = refreshed_grant
        current = await self._read_campaign(
            credential, customer_id, campaign_id
        )
        self._require_rename_preconditions(current, expected_old_name)

        binding = {
            "customerId": customer_id,
            "campaignId": campaign_id,
            "expectedOldName": expected_old_name,
            "newName": new_name,
            "expiresAt": self.now_ms() + PREVIEW_TTL_MS,
        }
        binding["requestDigest"] = rename_digest(binding, grant)
        issued = await self.control.issue_preview(connection_key, binding)
        if (
            not isinstance(issued, dict)
            or not self._same_binding(issued, binding)
            or not self._valid_opaque(issued.get("previewId"))
            or not self._valid_opaque(issued.get("confirmationToken"))
            or not isinstance(issued.get("expiresAt"), int)
            or issued["expiresAt"] <= self.now_ms()
            or issued["expiresAt"] > binding["expiresAt"]
        ):
            raise PermissionError(
                "Cuan Google Ads rename preview was not available"
            )
        return {
            **binding,
            "expiresAt": issued["expiresAt"],
            "previewId": issued["previewId"],
            "confirmationToken": issued["confirmationToken"],
            "status": current["status"],
        }

    async def execute_campaign_rename(
        self,
        connection_key: str,
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
        customer_id, campaign_id = self._validate_rename(
            customer_id, campaign_id, expected_old_name, new_name
        )
        if confirmed is not True:
            raise PermissionError("Explicit rename confirmation is required")
        if (
            not self._valid_opaque(preview_id)
            or not self._valid_opaque(confirmation_token)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or expires_at <= self.now_ms()
            or expires_at > self.now_ms() + PREVIEW_TTL_MS
            or not isinstance(execution_id, str)
            or not EXECUTION_ID.fullmatch(execution_id)
        ):
            raise ValueError(
                "Google Ads rename confirmation is invalid or expired"
            )
        grant = await self.control.authorize(
            connection_key, "rename_campaign", customer_id, campaign_id
        )
        self._validate_grant(grant, "rename_campaign", customer_id, campaign_id)
        credential = await self.control.resolve_credential(
            connection_key,
            "rename_campaign",
            customer_id,
            grant["credentialRef"],
        )
        if not isinstance(credential, dict) or not isinstance(
            credential.get("accessToken"), str
        ):
            raise PermissionError("Cuan provider credential was invalid")
        refreshed_grant = await self.control.authorize(
            connection_key, "rename_campaign", customer_id, campaign_id
        )
        self._validate_grant(
            refreshed_grant, "rename_campaign", customer_id, campaign_id
        )
        if any(
            refreshed_grant[key] != grant[key]
            for key in ("grantId", "credentialRef")
        ):
            raise PermissionError("Cuan Google Ads grant changed during execution")
        grant = refreshed_grant
        binding: dict[str, Any] = {
            "customerId": customer_id,
            "campaignId": campaign_id,
            "expectedOldName": expected_old_name,
            "newName": new_name,
            "expiresAt": expires_at,
        }
        binding["requestDigest"] = rename_digest(binding, grant)
        claim_request = {
            **binding,
            "previewId": preview_id,
            "confirmationToken": confirmation_token,
            "executionId": execution_id,
        }
        claim = await self.control.claim_execution(
            connection_key, claim_request
        )
        if (
            not isinstance(claim, dict)
            or claim.get("claimed") is not True
            or claim.get("previewId") != preview_id
            or claim.get("executionId") != execution_id
            or not self._same_binding(claim, binding)
            or not isinstance(claim.get("expiresAt"), int)
            or claim["expiresAt"] <= self.now_ms()
            or claim["expiresAt"] > expires_at
        ):
            raise PermissionError(
                "Cuan Google Ads execution claim was not available"
            )

        outcome = "not_dispatched"
        failure: Exception | None = None
        try:
            current = await self._read_campaign(
                credential, customer_id, campaign_id
            )
            self._require_rename_preconditions(current, expected_old_name)
        except Exception as exc:
            failure = exc

        if failure is None:
            # A network or response error after this point has unknown outcome.
            outcome = "uncertain"
            try:
                resource = await self.ads.rename_campaign(
                    credential, customer_id, campaign_id, new_name
                )
                expected_resource = (
                    f"customers/{customer_id}/campaigns/{campaign_id}"
                )
                if resource != expected_resource:
                    raise RuntimeError(
                        "Google Ads mutation resource did not match"
                    )
                updated = await self._read_campaign(
                    credential, customer_id, campaign_id
                )
                if updated["name"] != new_name or updated["status"] != current["status"]:
                    raise RuntimeError(
                        "Google Ads rename readback did not match"
                    )
                outcome = "confirmed"
            except Exception as exc:
                failure = RuntimeError(
                    "Google Ads rename outcome is unknown; inspect campaign before retrying"
                )
                failure.__cause__ = exc

        disposition = {
            "confirmed": "consume",
            "not_dispatched": "release",
            "uncertain": "preserve",
        }[outcome]
        finalization = {
            **binding,
            "previewId": preview_id,
            "executionId": execution_id,
            "outcome": outcome,
            "reservationDisposition": disposition,
        }
        try:
            ack = await self.control.finalize_execution(
                connection_key, finalization
            )
        except Exception as exc:
            raise RuntimeError(
                "Cuan execution finalization was not acknowledged; inspect before retrying"
            ) from exc
        if (
            not isinstance(ack, dict)
            or ack.get("acknowledged") is not True
            or not self._same_binding(ack, finalization)
            or ack.get("previewId") != preview_id
            or ack.get("executionId") != execution_id
            or ack.get("outcome") != outcome
            or ack.get("reservationDisposition") != disposition
        ):
            raise RuntimeError(
                "Cuan execution finalization was not acknowledged; inspect before retrying"
            )
        if failure:
            raise failure
        return {
            "customerId": customer_id,
            "campaignId": campaign_id,
            "oldName": expected_old_name,
            "newName": new_name,
            "status": current["status"],
            "executionId": execution_id,
        }

    async def _read_campaign(
        self, credential: dict[str, Any], customer_id: str, campaign_id: str
    ) -> dict[str, str]:
        rows = await self.ads.search_campaigns(
            credential,
            customer_id,
            campaign_lookup_query(campaign_id),
            2,
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("Exact Google Ads campaign was not found")
        campaign = self._parse_campaign(rows[0], customer_id)
        if campaign["id"] != campaign_id:
            raise ValueError("Google Ads campaign identity did not match")
        return campaign

    @staticmethod
    def _validate_rename(
        customer_id: Any,
        campaign_id: Any,
        expected_old_name: Any,
        new_name: Any,
    ) -> tuple[str, str]:
        customer_id = parse_customer_id(customer_id)
        if (
            not isinstance(campaign_id, str)
            or not CAMPAIGN_ID.fullmatch(campaign_id)
            or not _valid_name(expected_old_name)
            or not _valid_name(new_name)
            or expected_old_name == new_name
        ):
            raise ValueError("Google Ads campaign rename input is invalid")
        return customer_id, campaign_id

    @staticmethod
    def _require_rename_preconditions(
        campaign: dict[str, str], expected_old_name: str
    ) -> None:
        if campaign["name"] != expected_old_name:
            raise PermissionError(
                "Google Ads campaign name changed since preview"
            )

    @staticmethod
    def _valid_opaque(value: Any) -> bool:
        return isinstance(value, str) and bool(OPAQUE.fullmatch(value))

    @staticmethod
    def _same_binding(left: Any, right: dict[str, Any]) -> bool:
        return isinstance(left, dict) and all(
            left.get(key) == value for key, value in right.items()
        )

    @staticmethod
    def _validate_grant(
        grant: Any, operation: str, customer_id: str, campaign_id: str | None
    ) -> None:
        if (
            not isinstance(grant, dict)
            or grant.get("allowed") is not True
            or grant.get("provider") != "google"
            or grant.get("operation") != operation
            or grant.get("customerId") != customer_id
            or grant.get("campaignId") != campaign_id
            or grant.get("scope") != ADS_SCOPE
            or any(
                not isinstance(grant.get(key), str)
                or not OPAQUE.fullmatch(grant[key])
                for key in ("policyRevision", "grantId", "credentialRef")
            )
        ):
            raise PermissionError(
                "Cuan Google Ads authorization was not available"
            )

    @staticmethod
    def _parse_campaign(row: Any, customer_id: str) -> dict[str, str]:
        if not isinstance(row, dict) or not isinstance(
            row.get("campaign"), dict
        ):
            raise ValueError("Google Ads campaign response was invalid")
        campaign = row["campaign"]
        campaign_id = campaign.get("id")
        if isinstance(campaign_id, int) and not isinstance(campaign_id, bool):
            campaign_id = str(campaign_id)
        if (
            not isinstance(campaign_id, str)
            or not CAMPAIGN_ID.fullmatch(campaign_id)
            or campaign.get("resourceName")
            != f"customers/{customer_id}/campaigns/{campaign_id}"
            or not isinstance(campaign.get("name"), str)
            or not isinstance(campaign.get("status"), str)
        ):
            raise ValueError("Google Ads campaign identity was invalid")
        return {
            "id": campaign_id,
            "name": campaign["name"],
            "status": campaign["status"],
        }
