"""Utility helpers that centralize product plan definitions and logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from config import Config


@dataclass(frozen=True)
class PlanCTA:
    """Represents a call-to-action link rendered in the UI."""

    text: str
    href: str

    def to_dict(self) -> dict:
        return {"text": self.text, "href": self.href}


@dataclass(frozen=True)
class PlanDefinition:
    """Immutable metadata for a subscription plan tier."""

    name: str
    headline: str
    tagline: str
    description: str
    highlights: Tuple[str, ...]
    monthly_credits: int
    storage_quota_bytes: int
    max_projects: int = 10
    price_display: str = ""
    price_caption: str = ""
    primary_cta: Optional[PlanCTA] = None
    secondary_cta: Optional[PlanCTA] = None
    self_service: bool = True
    status_notice: Optional[str] = None

    def to_public_dict(self) -> dict:
        """Serialize the plan definition for API responses."""

        return {
            "name": self.name,
            "headline": self.headline,
            "tagline": self.tagline,
            "description": self.description,
            "highlights": list(self.highlights),
            "monthly_credits": self.monthly_credits,
            "storage_quota_bytes": self.storage_quota_bytes,
            "max_projects": self.max_projects,
            "price_display": self.price_display,
            "price_caption": self.price_caption,
            "primary_cta": self.primary_cta.to_dict() if self.primary_cta else None,
            "secondary_cta": self.secondary_cta.to_dict() if self.secondary_cta else None,
            "self_service": self.self_service,
            "status_notice": self.status_notice,
        }


DEFAULT_PLAN = "Standard"


PLAN_DEFINITIONS: Dict[str, PlanDefinition] = {
    "Standard": PlanDefinition(
        name="Standard",
        headline="Essential tools for individuals",
        tagline="Perfect for individual researchers and lightweight collaborations.",
        description=(
            "Launch personal research and collaborate with a close-knit team without "
            "overhead. Standard keeps credits topped off monthly so side projects stay moving."
        ),
        highlights=(
            "1 GB secure storage",
            "1,000 compute credits each month",
            "Up to 10 projects",
            "Community support & shared best practices",
        ),
        monthly_credits=1000,
        storage_quota_bytes=Config.USER_STORAGE_QUOTA,
        max_projects=10,
        price_display="Free",
        price_caption="with every account",
        primary_cta=PlanCTA("Explore documentation", "https://docs.phonos.local/standard"),
        secondary_cta=PlanCTA("Compare plans", "https://docs.phonos.local/pricing"),
    ),
    "Premium": PlanDefinition(
        name="Premium",
        headline="Production-ready collaboration",
        tagline="Accelerate team pipelines with priority compute, collaboration, and support.",
        description=(
            "Accelerate pipeline delivery with priority compute, dedicated onboarding, and "
            "a success engineer who keeps deployments on track. Premium unlocks the "
            "project collaboration workspace so teams can coordinate in real time."
        ),
        highlights=(
            "2 GB encrypted storage",
            "10,000 compute credits & burst capacity",
            "Up to 100 projects",
            "Project collaboration workspace with role controls",
            "Priority queueing for AlphaFold & RosettaFold",
            "Dedicated success engineer & onboarding",
        ),
        monthly_credits=10000,
        storage_quota_bytes=2 * 1024 * 1024 * 1024,
        max_projects=100,
        price_display="$249",
        price_caption="per month, billed monthly",
        primary_cta=PlanCTA(
            "Talk to sales",
            "mailto:sales@phonos.ai?subject=Premium%20Plan%20Inquiry",
        ),
        secondary_cta=PlanCTA(
            "Download premium overview",
            "/static/downloads/premium-overview.pdf",
        ),
    ),
    "Managed Premium": PlanDefinition(
        name="Managed Premium",
        headline="Managed collaboration for growing teams",
        tagline="Unlock organization governance with concierge support and priority compute.",
        description=(
            "Graduate from individual workspaces to organization-wide coordination. Managed Premium pairs "
            "priority compute with dedicated onboarding so teams can govern projects and scale securely "
            "without committing to the full Enterprise stack."
        ),
        highlights=(
            "Organization workspaces with role management",
            "Dedicated onboarding and workspace migration support",
            "15 GB encrypted storage pooled across the team",
            "15,000 compute credits with proactive burst scheduling",
            "Up to 100 projects",
            "Quarterly roadmap sessions with a success engineer",
        ),
        monthly_credits=15000,
        storage_quota_bytes=15 * 1024 * 1024 * 1024,
        max_projects=100,
        price_display="$399",
        price_caption="per month, billed monthly",
        primary_cta=PlanCTA(
            "Book managed onboarding",
            "mailto:sales@phonos.ai?subject=Managed%20Premium%20Inquiry",
        ),
        secondary_cta=PlanCTA(
            "Compare team plans",
            "https://docs.phonos.local/pricing",
        ),
    ),
    "Enterprise": PlanDefinition(
        name="Enterprise",
        headline="Enterprise controls & governance",
        tagline="Scale regulated workloads with compliance tooling and dedicated support.",
        description=(
            "Scale regulated workloads with layered security, automation hooks, and a "
            "white-glove success pod ready to support complex deployments."
        ),
        highlights=(
            "Unlimited encrypted storage & retention policies",
            "50,000 compute credits with dedicated allocation",
            "Unlimited projects",
            "Granular admin roles with audit exports",
            "Dedicated compute clusters & isolation guarantees",
            "On-call bioinformatics success pod",
            "SAML/SCIM SSO with automatic provisioning",
            "Change management playbooks & compliance toolkit",
        ),
        monthly_credits=50000,
        storage_quota_bytes=100 * 1024 * 1024 * 1024 * 1024,  # 100 TB equivalent
        max_projects=10000,  # Effectively unlimited
        price_display="Custom",
        price_caption="Tailored to your compliance scope",
        primary_cta=PlanCTA(
            "Schedule enterprise briefing",
            "mailto:enterprise@phonos.ai?subject=Enterprise%20Briefing%20Request",
        ),
        secondary_cta=PlanCTA(
            "Download security brief",
            "/static/downloads/enterprise-security-brief.pdf",
        ),
        self_service=False,
        status_notice=(
            "Enterprise onboarding is no longer paused—work with the Phonos team to "
            "tailor dedicated clusters, compliance, and governance for your organization."
        ),
    ),
}


PLAN_ORDER: Tuple[str, ...] = tuple(PLAN_DEFINITIONS.keys())

PLAN_ALIASES: Dict[str, str] = {
    "free": "Standard",
    "standard": "Standard",
    "pro": "Premium",
    "premium": "Premium",
    "mpremium": "Managed Premium",
    "managed premium": "Managed Premium",
    "managed-premium": "Managed Premium",
    "enterprise": "Enterprise",
}


def resolve_plan(plan: Optional[str]) -> str:
    """Return the canonical plan key or raise ``KeyError`` if unknown."""

    if plan is None:
        raise KeyError("No plan specified")

    candidate = plan.strip()
    if not candidate:
        raise KeyError("No plan specified")

    alias = PLAN_ALIASES.get(candidate.lower())
    if alias:
        return alias

    if candidate in PLAN_DEFINITIONS:
        return candidate

    raise KeyError(f"Unknown plan '{plan}'.")


def normalize_plan(plan: Optional[str], *, fallback: str = DEFAULT_PLAN) -> str:
    """Gracefully map an arbitrary plan label onto a supported tier."""

    try:
        return resolve_plan(plan)
    except KeyError:
        return fallback


def is_valid_plan(plan: Optional[str]) -> bool:
    """Return ``True`` if the value maps to a known plan tier."""

    try:
        resolve_plan(plan)
        return True
    except KeyError:
        return False


def get_plan_definition(plan: Optional[str]) -> PlanDefinition:
    """Return the :class:`PlanDefinition` for the provided plan label."""

    return PLAN_DEFINITIONS[resolve_plan(plan)]


def list_plan_definitions() -> List[PlanDefinition]:
    """Return all plan definitions in display order."""

    return [PLAN_DEFINITIONS[key] for key in PLAN_ORDER]


def get_plan_credit_quota(plan: Optional[str]) -> int:
    """Return the monthly credit allowance for a plan label."""

    return get_plan_definition(plan).monthly_credits


def get_plan_storage_quota(plan: Optional[str]) -> int:
    """Return the storage quota (in bytes) for a plan label."""

    return get_plan_definition(plan).storage_quota_bytes


def get_plan_max_projects(plan: Optional[str]) -> int:
    """Return the maximum number of projects allowed for a plan label."""

    return get_plan_definition(plan).max_projects


def list_plan_keys() -> Iterable[str]:
    """Yield the canonical plan keys in display order."""

    return PLAN_ORDER


def is_enterprise_plan(plan: Optional[str]) -> bool:
    """Check whether a plan label corresponds to the Enterprise tier."""

    try:
        return resolve_plan(plan) == "Enterprise"
    except KeyError:
        return False


def plan_supports_organizations(plan: Optional[str]) -> bool:
    """Return ``True`` when the plan grants organization governance controls."""

    try:
        return resolve_plan(plan) in {"Managed Premium", "Enterprise"}
    except KeyError:
        return False


def serialize_plan_catalog() -> List[dict]:
    """Return plan metadata formatted for API consumers."""

    return [plan.to_public_dict() for plan in list_plan_definitions()]

