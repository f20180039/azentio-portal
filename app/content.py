"""Public marketing content for Solutions, AI Lab and Technology. Synthetic and illustrative; no client names."""
from dataclasses import dataclass

from .constants import LabStage


@dataclass(frozen=True)
class Solution:
    segment: str
    summary: str
    challenges: tuple[str, ...]
    offerings: tuple[str, ...]


SOLUTIONS = (
    Solution("Retail and SME banks", "Faster, explainable credit for small businesses without loosening policy.",
             ("Analysts re-key statements and spreadsheets", "Seasonal businesses are flagged as declining", "Reviewers can't see why a recommendation was made"),
             ("sme-credit-decisioning", "financial-analysis-intelligence", "similar-portfolio-intelligence")),
    Solution("Non-bank lenders and microfinance", "Reach thin-file borrowers with evidence you can defend.",
             ("Phone-photo statements and low-quality scans", "Policy changes are hard to test before launch", "Small portfolios make comparisons noisy"),
             ("credit-strategy-simulator", "financial-analysis-intelligence", "similar-portfolio-intelligence")),
    Solution("Cooperative and community banks", "Modern decisioning and reporting sized for lean teams.",
             ("Manual regulatory returns", "Limited analyst capacity", "Board needs clear policy simulations"),
             ("riva", "credit-strategy-simulator", "sme-credit-decisioning")),
    Solution("Regulatory reporting teams", "Map, validate and submit returns with a full trail.",
             ("Blank or inconsistent subtotals found late", "Field mappings live in spreadsheets", "Every change needs an audit trail"),
             ("riva",)),
)


@dataclass(frozen=True)
class Experiment:
    title: str
    summary: str
    stage: LabStage
    offering: str


EXPERIMENTS = (
    Experiment("Same-season comparisons", "Explain seasonal revenue dips by comparing with the same period last year before flagging decline.",
               LabStage.PILOTING, "financial-analysis-intelligence"),
    Experiment("Statement extraction from phone photos", "Quality checks and page-join repair so photographed statements can be read reliably.",
               LabStage.EXPLORING, "financial-analysis-intelligence"),
    Experiment("Policy what-if narratives", "Plain-language summaries of what a credit policy change does to approvals and losses.",
               LabStage.EXPLORING, "credit-strategy-simulator"),
    Experiment("Validation rule suggestions", "Suggest validation rules for regulatory returns from past correction patterns.",
               LabStage.GRADUATED, "riva"),
)

PRINCIPLES = (
    ("Checked on the server", "Every page, API call and media file checks identity, active organisation, entitlements and the item's audience."),
    ("Least privilege", "Moderation, publishing, product ownership and administration are separate grants. Employee status implies none of them."),
    ("Private by default", "Client records start private. Shared learning uses a separate, moderated, de-identified copy."),
    ("Evidence, not prevalence", "Themes count distinct organisations and stay hidden below a minimum cohort; filters that could single one out are suppressed."),
    ("Cited AI", "AIRA answers only from approved contributions and every citation is checked against what was retrieved."),
    ("Tamper-evident audit", "Every privileged action is written to a hash-chained log; editing any entry breaks the chain."),
)
