"""Deterministic, product-neutral task routing.

Routes select a worker profile and an owner role. They never select a local
repository; repository selection is exclusively controlled by the explicit
runtime project registry.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class Route:
    worker_type: str
    owner_session: str
    reasoning: str
    reason: str


ROUTES: Tuple[Tuple[str, Iterable[str], Route], ...] = (
    (
        "security",
        ("security", "permission", "allowlist", "threat model", "安全", "权限"),
        Route("security", "security", "xhigh", "security or access-control work"),
    ),
    (
        "infrastructure",
        (
            "deploy",
            "deployment",
            "infrastructure",
            "container",
            "ci pipeline",
            "environment",
            "部署",
            "运维",
            "环境",
        ),
        Route("infra", "infrastructure", "xhigh", "infrastructure or deployment work"),
    ),
    (
        "browser",
        (
            "browser",
            "playwright",
            "screenshot",
            "visual regression",
            "accessibility",
            "浏览器",
            "视觉回归",
            "截图",
        ),
        Route("browser", "browser-qa", "high", "browser or visual verification"),
    ),
    (
        "qa",
        (
            "test",
            "verify",
            "regression",
            "smoke",
            "acceptance",
            "测试",
            "回归",
            "验收",
            "冒烟",
        ),
        Route("qa", "qa", "high", "software verification work"),
    ),
    (
        "mobile",
        ("react native", "expo", "android", "ios", "mobile", "移动端"),
        Route("cli", "mobile", "high", "mobile application work"),
    ),
    (
        "backend",
        (
            "backend",
            "api",
            "server",
            "database",
            "migration",
            "后端",
            "接口",
            "数据库",
        ),
        Route("cli", "backend", "xhigh", "backend or data-layer work"),
    ),
    (
        "frontend",
        (
            "frontend",
            "web",
            "component",
            "stylesheet",
            "responsive",
            "前端",
            "网页",
            "组件",
            "响应式",
        ),
        Route("cli", "frontend", "high", "frontend application work"),
    ),
    (
        "documentation",
        ("documentation", "readme", "runbook", "tutorial", "文档", "说明书"),
        Route("cli", "documentation", "high", "documentation work"),
    ),
)

ROLE_PATTERN = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
EXPLICIT_ROLE_PATTERN = re.compile(
    r"(?:^|\s)(?:owner|role)\s*[:=]\s*([a-z][a-z0-9._-]{0,63})(?:\s|$)",
    re.I,
)
ROLE_DEFAULTS = {
    "security": ("security", "xhigh"),
    "infrastructure": ("infra", "xhigh"),
    "browser-qa": ("browser", "high"),
    "qa": ("qa", "high"),
}


def explicit_owner_role(scope_summary: str) -> str:
    match = EXPLICIT_ROLE_PATTERN.search(scope_summary or "")
    if not match:
        return ""
    role = match.group(1).lower()
    return role if ROLE_PATTERN.fullmatch(role) is not None else ""


def route_task(title: str, scope_summary: str = "", repository: str = "") -> Route:
    explicit = explicit_owner_role(scope_summary)
    if explicit:
        worker_type, reasoning = ROLE_DEFAULTS.get(explicit, ("cli", "high"))
        return Route(worker_type, explicit, reasoning, "explicit owner role")
    title_haystack = title.lower()
    for _name, keywords, route in ROUTES:
        if any(keyword in title_haystack for keyword in keywords):
            return route
    haystack = " ".join((scope_summary, repository)).lower()
    for _name, keywords, route in ROUTES:
        if any(keyword in haystack for keyword in keywords):
            return route
    return Route("cli", "coordinator", "high", "default coordination route")


def matching_routes(
    title: str, scope_summary: str = "", repository: str = ""
) -> List[Route]:
    """Return deterministic generic domain matches without choosing a project."""

    explicit = explicit_owner_role(scope_summary)
    if explicit:
        return [route_task(title, scope_summary, repository)]
    haystack = " ".join((title, scope_summary, repository)).lower()
    matches: List[Route] = []
    owners = set()
    for _name, keywords, route in ROUTES:
        if any(keyword in haystack for keyword in keywords):
            if route.owner_session not in owners:
                owners.add(route.owner_session)
                matches.append(route)
    return matches


def route_metadata(route: Route) -> Dict[str, str]:
    return {
        "worker_type": route.worker_type,
        "owner_session": route.owner_session,
        "reasoning": route.reasoning,
        "route_reason": route.reason,
    }
