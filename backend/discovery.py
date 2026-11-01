import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from azure.core.exceptions import AzureError

from .azure import credential, error_label
from .models import (
    Connection,
    DiscoveryBase,
    Foundries,
    FoundryDeployment,
    FoundryDetail,
    FoundryQuery,
    FoundrySummary,
    LogTarget,
    OtherDestination,
    now,
)

ARM = "https://management.azure.com"
ARM_SCOPE = f"{ARM}/.default"
GRAPH_API = "2022-10-01"
ACCOUNT_API = "2024-10-01"
DIAGNOSTICS_API = "2021-05-01-preview"
WORKSPACE_API = "2023-09-01"
FOUNDRY_LIMIT = 200
DEPLOYMENT_LIMIT = 200
PAGE_LIMIT = 10
TOKEN_MARGIN = 300
WORKSPACE_PATH = re.compile(
    r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[\w.()-]+"
    r"/providers/Microsoft\.OperationalInsights/workspaces/[\w-]+",
    re.IGNORECASE,
)
# Omitting "subscriptions" makes Resource Graph cover every subscription the identity can read.
FOUNDRIES_QUERY = """resources
| where type =~ "microsoft.cognitiveservices/accounts"
| where kind in~ ("AIServices", "OpenAI")
| join kind=leftouter (
    resourcecontainers
    | where type =~ "microsoft.resources/subscriptions"
    | project subscriptionId, subscription = name
) on subscriptionId
| project id, name, location, kind, resourceGroup, subscriptionId, subscription,
    endpoint = tostring(properties.endpoint)
| order by name asc"""

TokenProvider = Callable[[DiscoveryBase], Awaitable[tuple[str, float]]]


async def arm_token(body: DiscoveryBase) -> tuple[str, float]:
    async with credential(body.logs_auth_mode, body.managed_identity_client_id) as identity:
        token = await identity.get_token(ARM_SCOPE)
    return token.token, float(token.expires_on)


def describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"HTTP {code}：该身份没有读取权限，需要在订阅、资源组或资源上授予 Reader。"
        if code == 404:
            return "HTTP 404：资源不存在，或该身份看不到它。"
        return f"Azure Resource Manager 返回 HTTP {code}。"
    if isinstance(exc, AzureError):
        return f"{error_label(exc)}：无法获取 ARM 令牌，请确认 az login 或托管身份可用。"
    if isinstance(exc, TimeoutError):
        return "Azure Resource Manager 请求超时。"
    return error_label(exc)


def mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()[:200]
    return "" if re.search(r"[\x00-\x1f\x7f]", value) else value


def endpoint(value: object) -> str:
    try:
        return Connection.azure_endpoint(text(value)) if text(value) else ""
    except ValueError:
        return ""


def arm_link(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    url = urlsplit(value)
    # The bearer token is attached to every request, so paging must stay on ARM.
    if (
        len(value) > 4000
        or url.scheme != "https"
        or url.hostname != "management.azure.com"
        or url.port not in (None, 443)
        or url.username
        or url.password
    ):
        raise ValueError("Refusing a nextLink outside Azure Resource Manager.")
    return value


def resource_label(value: str) -> str:
    match = re.search(r"/providers/[^/]+/[^/]+/([^/]+)", value)
    return text(match.group(1) if match else value.rsplit("/", 1)[-1])


def summary(row: dict[str, object]) -> FoundrySummary | None:
    try:
        resource_id = Connection.resource_path(text(row.get("id")))
    except ValueError:
        return None
    if not resource_id:
        return None
    return FoundrySummary(
        resource_id=resource_id,
        name=text(row.get("name")) or resource_id.rsplit("/", 1)[-1],
        location=text(row.get("location")),
        kind=text(row.get("kind")),
        resource_group=text(row.get("resourceGroup")),
        subscription_id=text(row.get("subscriptionId")),
        subscription=text(row.get("subscription")),
        endpoint=endpoint(row.get("endpoint")),
    )


def deployment(row: dict[str, object]) -> FoundryDeployment | None:
    name = text(row.get("name"))
    if not name:
        return None
    properties = mapping(row.get("properties"))
    model = mapping(properties.get("model"))
    sku = mapping(row.get("sku"))
    capacity = sku.get("capacity")
    return FoundryDeployment(
        name=name,
        model=text(model.get("name")),
        version=text(model.get("version")),
        format=text(model.get("format")),
        sku=text(sku.get("name")),
        capacity=capacity if isinstance(capacity, int) and not isinstance(capacity, bool) else None,
        state=text(properties.get("provisioningState")),
    )


def destinations(data: dict[str, object]) -> tuple[list[LogTarget], list[OtherDestination]]:
    targets: dict[str, LogTarget] = {}
    others: list[OtherDestination] = []
    for setting in items(data.get("value")):
        name = text(setting.get("name"))
        properties = mapping(setting.get("properties"))
        logs = [entry for entry in items(properties.get("logs")) if entry.get("enabled") is True]
        groups = {text(entry.get("categoryGroup")).lower() for entry in logs}
        categories = {text(entry.get("category")).lower() for entry in logs}
        # The "audit" group does not include RequestResponse or AzureOpenAIRequestUsage.
        everything = "alllogs" in groups
        workspace = text(properties.get("workspaceId"))
        if workspace and WORKSPACE_PATH.fullmatch(workspace):
            target = targets.setdefault(
                workspace.lower(),
                LogTarget(resource_id=workspace, name=workspace.rsplit("/", 1)[-1]),
            )
            target.settings.append(name)
            target.request_logs |= everything or "requestresponse" in categories
            target.usage_logs |= everything or "azureopenairequestusage" in categories
            target.dedicated |= (
                text(properties.get("logAnalyticsDestinationType")).lower() == "dedicated"
            )
        for kind, field in (
            ("storage", "storageAccountId"),
            ("event_hub", "eventHubAuthorizationRuleId"),
            ("partner", "marketplacePartnerId"),
        ):
            value = text(properties.get(field))
            if value:
                others.append(OtherDestination(setting=name, kind=kind, name=resource_label(value)))
    return list(targets.values()), others


class AzureDiscovery:
    """Read-only Azure Resource Manager discovery; it never changes Azure resources."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        token: TokenProvider | None = None,
    ):
        self.transport = transport
        self.token = token or arm_token
        self.tokens: dict[tuple[str, str], tuple[str, float]] = {}

    async def bearer(self, body: DiscoveryBase) -> str:
        key = (body.logs_auth_mode, body.managed_identity_client_id)
        cached = self.tokens.get(key)
        if cached and cached[1] - TOKEN_MARGIN > time.time():
            return cached[0]
        self.tokens[key] = await self.token(body)
        return self.tokens[key][0]

    @asynccontextmanager
    async def client(self, body: DiscoveryBase) -> AsyncIterator[httpx.AsyncClient]:
        token = await self.bearer(body)
        try:
            async with httpx.AsyncClient(
                base_url=ARM,
                transport=self.transport,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                yield client
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                self.tokens.pop((body.logs_auth_mode, body.managed_identity_client_id), None)
            raise

    @staticmethod
    async def get(client: httpx.AsyncClient, path: str, api: str | None) -> dict[str, object]:
        response = await client.get(path, params={"api-version": api} if api else None)
        response.raise_for_status()
        return mapping(response.json())

    async def pages(
        self, client: httpx.AsyncClient, path: str, api: str, limit: int
    ) -> tuple[list[dict[str, object]], bool]:
        rows: list[dict[str, object]] = []
        url, version = path, api
        for _ in range(PAGE_LIMIT):
            data = await self.get(client, url, version)
            rows.extend(items(data.get("value")))
            if len(rows) > limit:
                return rows[:limit], True
            link = arm_link(data.get("nextLink"))
            if not link:
                return rows, False
            url, version = link, None
        return rows, True

    async def foundries(self, body: DiscoveryBase) -> Foundries:
        async with self.client(body) as client:
            response = await client.post(
                "/providers/Microsoft.ResourceGraph/resources",
                params={"api-version": GRAPH_API},
                json={
                    "query": FOUNDRIES_QUERY,
                    "options": {"resultFormat": "objectArray", "$top": FOUNDRY_LIMIT + 1},
                },
            )
            response.raise_for_status()
        data = mapping(response.json())
        rows = items(data.get("data"))
        found = [item for row in rows[:FOUNDRY_LIMIT] if (item := summary(row))]
        found.sort(key=lambda item: (item.name.lower(), item.resource_id.lower()))
        limited = len(rows) > FOUNDRY_LIMIT or str(data.get("resultTruncated")).lower() == "true"
        message = (
            f"找到 {len(found)} 个可访问的 Foundry / Azure OpenAI 资源。"
            if found
            else "该身份在可访问的订阅中没有找到 Foundry / Azure OpenAI 资源（需要 Reader）。"
        )
        if limited:
            message += f" 仅显示前 {FOUNDRY_LIMIT} 个。"
        return Foundries(
            state="ok" if found else "empty",
            message=message,
            checked_at=now(),
            items=found,
            limited=limited,
        )

    async def foundry(self, body: FoundryQuery) -> FoundryDetail:
        resource = body.resource_id
        async with self.client(body) as client:
            account, listed, diagnostics = await asyncio.gather(
                self.get(client, resource, ACCOUNT_API),
                self.pages(client, f"{resource}/deployments", ACCOUNT_API, DEPLOYMENT_LIMIT),
                self.get(
                    client,
                    f"{resource}/providers/Microsoft.Insights/diagnosticSettings",
                    DIAGNOSTICS_API,
                ),
                return_exceptions=True,
            )
            if isinstance(account, BaseException):
                raise account
            properties = mapping(account.get("properties"))
            detail = FoundryDetail(
                resource_id=resource,
                name=text(account.get("name")) or resource.rsplit("/", 1)[-1],
                location=text(account.get("location")),
                kind=text(account.get("kind")),
                endpoint=endpoint(properties.get("endpoint")),
                checked_at=now(),
            )
            if isinstance(listed, BaseException):
                detail.deployments_error = describe(listed)
            else:
                rows, detail.deployments_limited = listed
                detail.deployments = sorted(
                    (item for row in rows if (item := deployment(row))),
                    key=lambda item: item.name.lower(),
                )
            if isinstance(diagnostics, BaseException):
                detail.diagnostics_error = describe(diagnostics)
            else:
                detail.workspaces, detail.other_destinations = destinations(diagnostics)
                await asyncio.gather(*(self.resolve(client, item) for item in detail.workspaces))
                detail.workspaces.sort(
                    key=lambda item: (
                        not item.request_logs,
                        not item.workspace_id,
                        item.name.lower(),
                    )
                )
        problems = [
            detail.deployments_error,
            detail.diagnostics_error,
            *(item.error for item in detail.workspaces),
        ]
        if any(problems):
            detail.state = "partial"
            detail.message = "部分信息读取失败，可在下方手动填写。"
        return detail

    async def resolve(self, client: httpx.AsyncClient, target: LogTarget) -> None:
        try:
            data = await self.get(client, target.resource_id, WORKSPACE_API)
        except (httpx.HTTPError, ValueError) as exc:
            target.error = describe(exc)
            return
        target.name = text(data.get("name")) or target.name
        target.location = text(data.get("location"))
        try:
            target.workspace_id = str(UUID(text(mapping(data.get("properties")).get("customerId"))))
        except ValueError:
            target.error = "Workspace 没有返回 customerId。"
