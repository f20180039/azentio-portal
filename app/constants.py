"""Single source for fixed values: URL paths, roles, permissions, audiences, lifecycle states and form options.
Enum values are what the database stores and the browser sends, so a StrEnum compares equal to its raw string."""
from enum import StrEnum
from typing import TypeVar

E = TypeVar("E", bound=StrEnum)


class Route(StrEnum):
    """Every fixed URL. Members with {placeholders} are FastAPI route paths and build links through .url(**params)."""

    HOME = "/"
    HEALTH = "/health"
    PRODUCTS = "/products"
    PRODUCT = "/products/{slug}"
    MEDIA = "/media"
    ABOUT = "/about"
    CONNECT = "/connect"
    HACKATHON = "/hackathon/register"
    RESOURCE = "/resources/{rid}"
    MEDIA_FILE = "/media/{rid}"

    SIGNIN = "/signin"
    SIGNIN_VERIFY = "/signin/verify"
    SIGNOUT = "/signout"
    ENTRA_START = "/auth/entra/start"
    ENTRA_CALLBACK = "/auth/entra/callback"
    WORKSPACE = "/workspace"
    MAILBOX = "/mailbox"
    INVITE = "/invite/{token}"
    ACCESS = "/access"
    ACCESS_REQUEST = "/access/request"
    NOTIFICATIONS = "/notifications"
    DEMO = "/demo"
    DEMO_VIEW_AS = "/demo/view-as"
    DEMO_RESET = "/demo/reset"

    DASHBOARD = "/dashboard"
    OBSERVATIONS = "/observations"
    OBSERVATION_NEW = "/observations/new"
    OBSERVATION = "/observations/{oid}"
    OBSERVATION_EDIT = "/observations/{oid}/edit"
    OBSERVATION_SHARE = "/observations/{oid}/share"
    OBSERVATION_WITHDRAW = "/observations/{oid}/withdraw"
    OBSERVATION_ATTACH = "/observations/{oid}/attachments"
    ATTACHMENT = "/observations/{oid}/attachments/{aid}"
    ATTACHMENT_DELETE = "/observations/{oid}/attachments/{aid}/delete"

    LEARNING = "/learning"
    THEMES = "/learning/themes"
    THEME = "/learning/themes/{tid}"
    THEME_ASSIGN = "/learning/themes/{tid}/assign"
    TRENDS = "/learning/trends"
    TRENDS_FILE = "/learning/trends/file"
    IDEAS = "/learning/ideas"
    ASK = "/learning/ask"
    SHARED_REPORT = "/learning/shared/{sid}/report"

    MODERATION = "/moderation"
    MODERATION_REVIEW = "/moderation/{oid}"
    MODERATION_PUBLISH = "/moderation/{oid}/publish"
    MODERATION_RETURN = "/moderation/{oid}/return"
    MODERATION_SUSPEND = "/moderation/shared/{sid}/suspend"
    MODERATION_REPORT = "/moderation/reports/{rid}"

    IDEA_NEW = "/ideas/new"
    IDEA_CREATE = "/ideas"
    IDEA_STATUS = "/ideas/{iid}/status"

    PLAYGROUND = "/playground"
    PLAYGROUND_FEEDBACK = "/playground/feedback"
    PLAYGROUND_SCENARIO = "/playground/{slug}"
    PLAYGROUND_RUN = "/playground/runs/{rid}"
    PLAYGROUND_UPLOAD = "/playground/{slug}/upload"
    PLAYGROUND_TEMPLATE = "/playground/{slug}/template.csv"
    PLAYGROUND_SAVED_DELETE = "/playground/saved/{sid}/delete"
    PLAYGROUND_RUN_FEEDBACK = "/playground/runs/{rid}/feedback"
    PLAYGROUND_RUN_EXPLAIN = "/playground/runs/{rid}/explain"

    LIVE = "/live"
    LIVE_SESSION = "/live/{sid}"
    LIVE_REGISTER = "/live/{sid}/register"
    LIVE_STATUS = "/live/{sid}/status"
    LIBRARY = "/library"
    LIBRARY_ITEM = "/library/{iid}"
    LIBRARY_REVIEW = "/library/{iid}/review"
    LIBRARY_FEEDBACK = "/library/{iid}/feedback"

    CONTENT = "/content"
    CONTENT_RESOURCE = "/content/resources/{rid}"
    ORG_MEMBERS = "/org/members"
    ORG_INVITE = "/org/invite"
    ORG_INVITE_REVOKE = "/org/invite/{iid}/revoke"
    ORG_MEMBER_REVOKE = "/org/members/{uid}/revoke"
    ORG_MEMBER_ROLE = "/org/members/{uid}/role"
    ORG_SUPPORT = "/org/support/{gid}"
    SUPPORT_REVOKE = "/support/{gid}/revoke"
    ADMIN = "/admin"
    ADMIN_AUDIT = "/admin/audit"
    ADMIN_REQUEST = "/admin/requests/{rid}"
    ADMIN_GRANTS = "/admin/grants"
    ADMIN_ENTITLEMENTS = "/admin/entitlements"
    ADMIN_SUPPORT = "/admin/support"
    ADMIN_OUTBOX_SEND = "/admin/outbox/send"
    ADMIN_DIGEST = "/admin/outbox/digest"
    ADMIN_SUPPORT_RECORDS = "/admin/support/{gid}"

    LEARNING_EXPORT = "/learning/export.csv"
    CONFIGURATOR = "/configurator"
    SHARED_VOTE = "/learning/shared/{sid}/vote"
    SHARED_COMMENT = "/learning/shared/{sid}/comments"
    SHARED_BOOKMARK = "/learning/shared/{sid}/bookmark"
    COMMENT_REPORT = "/learning/comments/{cid}/report"
    MODERATION_COMMENT = "/moderation/comments/{cid}"
    SAVED = "/learning/saved"
    SAVED_SEARCHES = "/learning/searches"
    SAVED_SEARCH_DELETE = "/learning/searches/{qid}/delete"
    SOLUTIONS = "/solutions"
    EXPERIMENTS = "/lab/experiments"
    EXPERIMENT = "/lab/experiments/{eid}"
    EXPERIMENT_UPDATE = "/lab/experiments/{eid}/update"
    EXPERIMENT_COMMENT = "/lab/experiments/{eid}/comments"
    TEAMS = "/hackathon/teams"
    TEAM = "/hackathon/teams/{tid}"
    TEAM_MEMBER = "/hackathon/teams/{tid}/members"
    TEAM_POST = "/hackathon/teams/{tid}/posts"
    RELEASES = "/releases"
    FEDERATED = "/federated"
    FEDERATED_ROUND = "/federated/rounds"
    FEDERATED_MODEL = "/federated/models/{mid}"
    FEDERATED_CONSENT = "/federated/consent"
    LAB = "/lab"
    TECHNOLOGY = "/technology"
    INSIGHTS = "/insights"
    API_DOCS = "/api/docs"
    API_ME = "/api/v1/me"
    API_OBSERVATIONS = "/api/v1/observations"
    API_OBSERVATION = "/api/v1/observations/{oid}"
    API_SHARED = "/api/v1/shared"
    API_THEMES = "/api/v1/themes"
    API_IDEAS = "/api/v1/ideas"
    API_RESOURCE = "/api/v1/resources/{rid}"
    API_AIRA = "/api/v1/aira"
    API_MODERATION_QUEUE = "/api/v1/moderation/queue"

    def url(self, **params) -> str:
        return self.value.format(**params)


class OrgKind(StrEnum):
    AZENTIO = "azentio"
    CLIENT = "client"


class Role(StrEnum):
    EMPLOYEE = "employee"
    MEMBER = "member"
    ADMIN = "admin"


CLIENT_ROLES = (Role.MEMBER, Role.ADMIN)


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class Permission(StrEnum):
    """Platform grants: separate from employee status and from each other (IAM-02)."""

    MODERATE = "moderate"
    PUBLISH_CONTENT = "publish_content"
    PORTAL_ADMIN = "portal_admin"
    PRODUCT_OWNER = "product_owner"

    @property
    def label(self) -> str:
        return {"moderate": "Learning moderator", "publish_content": "Content publisher",
                "portal_admin": "Portal administrator", "product_owner": "Product owner"}[self.value]


class Audience(StrEnum):
    """Who may open a resource or session. There is no default: missing audience means no access."""

    PUBLIC = "public"
    EMPLOYEES = "employees"
    ORG = "org"
    ENTITLED = "entitled"

    @property
    def label(self) -> str:
        return {"public": "Public", "employees": "Employees only", "org": "One organisation only", "entitled": "Entitled clients"}[self.value]


class Visibility(StrEnum):
    PRIVATE = "private"
    ORG = "org"


class ShareState(StrEnum):
    """Where an observation stands in the sharing lifecycle."""

    NONE = "none"
    SUBMITTED = "submitted"
    RETURNED = "returned"
    PUBLISHED = "published"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    SUSPENDED = "suspended"


class SharedStatus(StrEnum):
    """State of one published shared version."""

    PUBLISHED = "published"
    SUSPENDED = "suspended"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


class ReportStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class InviteStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RequestStatus(StrEnum):
    """Employee access requests and support-access grants."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"


class IdeaStatus(StrEnum):
    PROPOSED = "Proposed"
    UNDER_REVIEW = "Under review"
    PLANNED = "Planned"
    IN_PROGRESS = "In progress"
    RELEASED = "Released"
    NOT_PROCEEDING = "Not proceeding"


class Priority(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class SessionStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    ENDED = "ended"
    CANCELLED = "cancelled"


class LibraryVerdict(StrEnum):
    ACCURATE = "accurate"
    OUTDATED = "outdated"


class Accuracy(StrEnum):
    """How a Playground result compared with what the person expected."""

    AS_EXPECTED = "As expected"
    PARTLY = "Partly as expected"
    NOT_EXPECTED = "Not as expected"


class EvidenceVote(StrEnum):
    """Participants' quality feedback on a shared contribution; counts only, never a leaderboard."""

    USEFUL = "useful"
    NEEDS_EVIDENCE = "needs_evidence"

    @property
    def label(self) -> str:
        return {"useful": "Useful", "needs_evidence": "Needs evidence"}[self.value]


class EvidenceQuality(StrEnum):
    """Moderator's assessment of the evidence behind a published contribution."""

    STRONG = "Strong"
    MODERATE = "Moderate"
    LIMITED = "Limited"


class Setting(StrEnum):
    """Portal settings a portal administrator can change in the Configurator."""

    MIN_COHORT = "min_cohort"
    SMALL_SAMPLE = "small_sample"
    SUPPORT_MINUTES = "support_minutes"
    INVITE_DAYS = "invite_days"
    AIRA_ENABLED = "aira_enabled"
    AIRA_PER_MINUTE = "aira_per_minute"
    PUBLISHING_PAUSED = "publishing_paused"


class ExperimentStage(StrEnum):
    """Internal stage of an employee experiment."""

    PROPOSED = "Proposed"
    RUNNING = "Running"
    CONCLUDED = "Concluded"
    STOPPED = "Stopped"


class TeamRole(StrEnum):
    LEAD = "lead"
    MEMBER = "member"


class LabStage(StrEnum):
    """Public stage of an AI Lab experiment."""

    EXPLORING = "Exploring"
    PILOTING = "Piloting with clients"
    GRADUATED = "Now in a product"


class CommentStatus(StrEnum):
    """Comments inherit their contribution's audience; moderation applies to them too."""

    VISIBLE = "visible"
    HELD = "held"  # flagged by the restricted-data screen, waiting for a moderator
    REPORTED = "reported"
    HIDDEN = "hidden"


class EmailStatus(StrEnum):
    """Outbox state. Only SENT means the provider accepted the message; nothing is ever labelled delivered before that."""

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {"queued": "Email queued, not sent yet", "sent": "Email accepted by the provider", "failed": "Email could not be sent"}[self.value]


class ModelStatus(StrEnum):
    """Federated model registry: every aggregate starts as a candidate and needs a human decision."""

    CANDIDATE = "candidate"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    RETIRED = "retired"


class ParticipantOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DROPPED_OUT = "dropped out"


class ConsentEvent(StrEnum):
    """Consent history of one contribution (brief §09): each change is kept, never overwritten."""

    STORED = "stored"
    PROPOSED = "proposed"
    WITHDRAWN = "withdrawn"

    @property
    def label(self) -> str:
        return {"stored": "Saved privately", "proposed": "Proposed for shared learning", "withdrawn": "Sharing withdrawn"}[self.value]


class Action(StrEnum):
    """Values of the submit buttons named 'action' on decision forms."""

    APPROVE = "approve"
    DENY = "deny"
    ADD = "add"
    REMOVE = "remove"
    GRANT = "grant"
    UPHOLD = "uphold"
    DISMISS = "dismiss"
    RETURN = "return"
    REJECT = "reject"
    CANCEL = "cancel"
    HIDE = "hide"
    RESTORE = "restore"
    SAVE = "save"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    OPT_IN = "opt_in"
    OPT_OUT = "opt_out"


class EnquiryKind(StrEnum):
    DEMO = "demo"
    HACKATHON = "hackathon"


class Category(StrEnum):
    MARKET = "Market observation"
    TREND = "Emerging trend"
    PRODUCT_GAP = "Product gap"
    OPERATIONAL = "Operational challenge"
    PRACTICE = "Successful practice"
    SUGGESTION = "Improvement suggestion"


class EvidenceType(StrEnum):
    PORTFOLIO = "Portfolio data (aggregated)"
    CUSTOMER_FEEDBACK = "Customer feedback"
    INTERNAL_REVIEW = "Internal review"
    REGULATORY = "Regulatory change"
    MARKET_REPORT = "Market report"


class Confidence(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


class Sector(StrEnum):
    """Coarse sector on shared copies; never the organisation's own industry label."""

    BANKING = "Banking"
    LENDING = "Lending"
    INSURANCE = "Insurance"
    FINANCIAL_SERVICES = "Financial services"


def parse(enum: type[E], value: str | None) -> E | None:
    """Turn untrusted form input into an enum member, or None when it is not one."""
    try:
        return enum(value)
    except ValueError:
        return None


def sql_in(enum: type[StrEnum]) -> str:
    """A CHECK-constraint value list built from an enum, so the schema cannot drift from the code."""
    return ",".join(f"'{m.value}'" for m in enum)
