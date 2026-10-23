"""Ask Joern for a role mapping's interaction order.

The whole derivation happens in Joern, in ``joern/walk.sc``: it loads the repo's
CPG, locates each role's node, and walks the control-flow graph from the entry
role, returning the ordered interactions themselves. Nothing is walked here.
Joern is either a query service (``JOERN_SERVICE_URL``, :mod:`.joern_service`) or a
local install (:mod:`.joern_local`) — the same script and the same result either
way.

``walk.sc`` is the walk and nothing else. What differs between frontends — where
a declaration's line is, how an import maps to a file, whether a parameter can
carry a default the frontend discarded — is a ``LangSpec`` profile beside it,
``joern/python.sc`` or ``joern/java.sc``, and :func:`_script` sends the pair as
one script.

Nothing is walked in Python: one implementation of the walk, in Joern, serves
every caller.

**What the caller supplies, and why each is a parameter rather than a default.**
``roles`` is the mapping to derive for — Level 3 sends a *conditioned* mapping
(narrowed to the roles both sides detected), so the derivation cannot be hoisted
out and done once per repo. ``behaviour`` is the pattern's prescribed order,
which decides the entry role and sequences roles annotated onto the same code.
The entry role is resolved *here*, before the call, by :func:`entry_role`;
``entry`` supplies it outright and skips that.

Every failure returns a :class:`DetectionResult` with no interactions rather than
raising: a derivation that could not run is a fact about the Joern setup, and the
caller has a role mapping in hand that cost a full agent run. ``reason`` says
what happened, and is empty when nothing did.

**A citation the graph cannot locate is not a failure either.** ``walk.sc`` reads
it as a library call, found by name at the cited line, whatever the row's
``status`` says. A ``present`` row that turns out to be a library call (``equals``,
``@PreAuthorize``) is a status disagreement, and Level 1 is where statuses are
compared; it does not stop the derivation.

**Several roles cited on one library call are not a failure either.** The walk
reaches that call as every one of those roles, but reads no interaction *between*
them: what passes between them happens inside the library, where nothing shows
which part plays which role (``sameLibraryCall`` in ``walk.sc``). Until 2026-10-04
this raised ``SharedExternalEntity``; it was removed for the same reason as
``AnnotationGraphMismatch`` -- a recovery's citations cannot be corrected after the
run, so the raise only cost the record its Level 3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import config, joern_local, joern_service

logger = logging.getLogger(__name__)

#: Bumped when ``walk.sc``'s output shape changes. A result announcing a
#: different version is refused rather than half-read. 2: ``path`` and ``parent``.
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Step:
    """One call the walk followed on the way from an interaction's source role to its target."""

    #: The method making the call (Joern fullName).
    caller: str
    file: str
    line: int
    #: The call's source text, on one line; empty for a hop no statement names
    #: (a framework binding on entry, a read of a field).
    code: str
    #: What the call ran: a method's fullName, a library call's, or a field's name.
    callee: str
    #: This hop's own mechanism — the interaction's ``mechanism`` summarises the path.
    mechanism: str = "call"
    #: The framework route the hop crossed (``[spring] ProviderManager.authenticate``), if any.
    route: str = ""

    def to_dict(self) -> dict:
        out = {
            "caller": self.caller,
            "file": self.file,
            "line": self.line,
            "code": self.code,
            "callee": self.callee,
            "mechanism": self.mechanism,
        }
        if self.route:
            out["route"] = self.route
        return out


@dataclass(frozen=True)
class Interaction:
    """One directed role-to-role interaction, with the site that evidences it."""

    source: str
    target: str
    file: str
    line: int
    #: Non-role helper functions the call passed through, innermost last.
    via: tuple[str, ...] = ()
    #: What established this interaction. ``call`` — a call site reached in the
    #: source role's body. ``data`` — a read or write of the target's data.
    #: ``nested-definition`` — a function declared inside the source role's body,
    #: which nothing calls, so the declaration statement is the only evidence it
    #: exists. ``dependency-injection`` / ``route-registration`` — the framework
    #: wired it, and nothing in either body names the other. ``dispatch`` — the
    #: call names an interface or library method, and the edge runs into a
    #: repository override of it: which one runs is decided at runtime (Spring
    #: injects it), so the walk keeps the override reaching the most roles.
    #: ``co-located`` — both roles are cited on one entity and the pattern has a
    #: step between them: no call separates them, so the edge is the pattern's,
    #: read at that entity. Recorded rather than
    #: inferred because it is exactly what a reader needs to judge an edge: a
    #: literal call is in the code, a wired one is a claim about runtime.
    mechanism: str = "call"
    #: Every call from the source role's body to the target, in order; the last
    #: one lands in the target and is at ``file``:``line``. ``via`` names the
    #: methods in between, this says where each was called and how.
    path: tuple[Step, ...] = ()
    #: The token of the interaction that brought the walk into the source role's
    #: body — following it back reaches the entry role. "" for an interaction
    #: made from the entry role itself.
    parent: str = ""

    @property
    def token(self) -> str:
        """The identity Kendall's Tau compares. Order-bearing, evidence-free."""
        return f"{self.source} → {self.target}"

    def to_dict(self) -> dict:
        return {
            "interaction": self.token,
            "from": self.source,
            "to": self.target,
            "file": self.file,
            "line": self.line,
            "via": list(self.via),
            "mechanism": self.mechanism,
            "path": [s.to_dict() for s in self.path],
            "parent": self.parent,
        }


@dataclass
class DetectionResult:
    """The derived ordering for one side, plus why it looks the way it does."""

    interactions: list[Interaction] = field(default_factory=list)
    entry_role: str = ""
    #: How the entry role was chosen: ``behaviour`` (the pattern's first
    #: prescribed speaker, and it was locatable), ``code-root`` (the pattern
    #: named nobody locatable, so a role nothing else calls decided), or "".
    entry_rule: str = ""
    #: role -> the kind of node it was located on (``METHOD``, ``TYPE_DECL``,
    #: ``PARAM``, ``LOCAL``, ``MEMBER``), or ``external`` when the citation
    #: matched nothing.
    anchored_roles: dict[str, str] = field(default_factory=dict)
    #: Why there is no order, when there is none. Empty on success.
    reason: str = ""

    @property
    def tokens(self) -> list[str]:
        return [i.token for i in self.interactions]

    @property
    def available(self) -> bool:
        return not self.reason


def entry_role(behaviour: list[dict], mappings: dict[str, dict]) -> str:
    """The role the walk should start at, read off the pattern's walkthrough.

    The walkthrough is a numbered script, so its opening speaker is the entry the
    pattern itself prescribes. That role is regularly absent from the repository
    — step 1 of both authentication patterns is spoken by ``Subject``, the browser
    or client outside the codebase — and the script does not stop there: step 2
    has its own speaker and the flow continues from it. So the steps are walked in
    order and the first speaker the mapping grounds is taken, rather than
    abandoning the pattern the moment step 1 is unmapped.

    Only ``from`` roles are candidates: a role merely spoken *to* is somewhere the
    walk arrives, not where it begins.

    Resolved here rather than in ``walk.sc`` because it needs nothing the graph
    holds — the pattern's step order and the mapping are both in hand before the
    call — and one Python function is cheaper to read, change and test than the
    same choice expressed in Scala inside a query. The script keeps the final say
    only on whether the role it is handed can actually originate a walk: a role
    grounded on a variable has no body, and ``walk.sc`` falls back to its
    code-root heuristic and reports ``entryRule: code-root`` when that happens.

    The script also overrules a speaker that *is* walkable but is not where the
    code begins. A role some other cited role reaches cannot open the flow — in a
    framework-driven repo the pattern's first speaker is regularly a leaf, since
    the framework calling it is not in the codebase — so the pattern chooses among
    the call graph's roots rather than outright. ``entryRule`` says which happened:
    ``behaviour`` or ``code-root``.

    **There is no fallback.** When every cited role is reached by another — a
    cycle — no root exists, and rather than start somewhere arbitrary the script
    derives nothing. An order whose first element is an artefact of that choice
    reads, in the output, exactly like one the code really starts with, and the
    point of the derivation is to be evidence.

    Grounded means *named*: ``walk.sc`` locates a role by ``(name, file, line)``
    equality, so a row naming no entity locates nothing and cannot be an entry.
    Returns "" when the pattern names no grounded speaker, which leaves the choice
    to the script.
    """
    named = {
        role
        for role, row in mappings.items()
        if (row.get("entity") or "").strip()
    }
    steps = sorted(
        enumerate(behaviour or []),
        key=lambda pair: _step_number(pair[1], pair[0]),
    )
    for _, step in steps:
        speaker = (step.get("from") or "").strip()
        if speaker and speaker in named:
            return speaker
    return ""


def _step_number(step: dict, fallback: int) -> int:
    """A step's own number, falling back to its position when it carries none."""
    try:
        return int(str(step.get("step", "")).strip())
    except (TypeError, ValueError):
        return fallback


def detect(
    repo: str,
    language: str,
    mappings: dict[str, dict],
    *,
    behaviour: list[dict] | None = None,
    entry: str = "",
    repo_path: Path | None = None,
) -> DetectionResult:
    """The interaction order for ``mappings``, derived on Joern.

    ``mappings`` is ``{role name: role row}`` for a single side — ground truth or
    recovery result; both go through here identically. ``repo`` is the bare name
    the service resolves against its own checkouts, which is also the name the
    caller has the repo under locally.

    ``repo_path`` is the checkout on this machine. The service ignores it — it has
    its own checkouts — but a local Joern (``JOERN_SERVICE_URL`` unset) needs it,
    both to build the CPG and to read source the walk consults.
    """
    script = _script(language)
    if script is None:
        return DetectionResult(
            reason=(
                f"no Joern walk could be assembled for {language or 'an unnamed language'} — "
                f"either the language has no profile in this installation, or walk.sc is "
                f"missing. See the joern: log line above."
            )
        )

    roles = [
        {
            "role": role,
            "entity": (row.get("entity") or "").strip(),
            "file": (row.get("file") or "").strip(),
            "lines": str(row.get("lines") or "").strip(),
        }
        for role, row in mappings.items()
    ]

    params = {
        "roles": json.dumps(roles),
        "behaviour": json.dumps(behaviour or []),
        "entry": entry or entry_role(behaviour or [], mappings),
    }
    if config.JOERN_SERVICE_URL:
        result = joern_service.query(repo, language, script, params=params)
        where = "the Joern query service"
    elif repo_path is None:
        return DetectionResult(
            reason=(
                f"JOERN_SERVICE_URL is unset, so the walk runs on local Joern, which needs "
                f"the checkout of {repo} — and the caller passed no repo_path."
            )
        )
    else:
        result = joern_local.query(repo, language, script, repo_path, params=params)
        where = "local Joern"
    if result is None:
        return DetectionResult(
            reason=(
                f"{where} did not return an interaction order for {repo}. See the "
                "joern: log lines above for what happened."
            )
        )
    return _read(result, repo)


#: Repo language -> the profile that answers ``walk.sc``'s ``LangSpec`` for it.
#: A language absent from this map has no profile and is not derived: the walk
#: cannot run without one, and guessing a near neighbour would silently apply
#: Python's line rule to a frontend that does not share it.
_PROFILES = {"python": "python.sc", "java": "java.sc"}


def _script(language: str) -> str | None:
    """``walk.sc`` followed by the language's profile, as one script.

    The service runs whatever single script it is posted, so the pair is joined
    here rather than imported there — which also keeps the whole analysis pinned
    to this source tree, profile included. Order matters only for readability:
    ``walk.sc`` declares ``LangSpec`` and the profile defines ``object Lang``,
    and Scala resolves the reference either way round.
    """
    home = Path(__file__).parent / "joern"
    profile = _PROFILES.get((language or "").strip().lower())
    if profile is None:
        logger.warning(
            "joern: no language profile for %r — the walk needs one to know where a "
            "declaration's line is and how an import maps to a file. Profiles: %s",
            language, ", ".join(sorted(_PROFILES)),
        )
        return None
    parts = []
    for name in ("walk.sc", profile):
        path = home / name
        if not path.is_file():
            logger.error("joern: %s is missing from this installation (%s)", name, path)
            return None
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _step(row: dict) -> Step:
    """One ``path`` entry of a ``walk.sc`` interaction."""
    return Step(
        caller=(row.get("caller") or "").strip(),
        file=(row.get("file") or "").strip(),
        line=int(row.get("line") or -1),
        code=(row.get("code") or "").strip(),
        callee=(row.get("callee") or "").strip(),
        mechanism=(row.get("mechanism") or "call").strip() or "call",
        route=(row.get("route") or "").strip(),
    )


def _read(result: dict, repo: str) -> DetectionResult:
    """One ``walk.sc`` result as a :class:`DetectionResult`.

    A version mismatch is refused rather than partially read: a service deployed
    against an older ``walk.sc`` would otherwise contribute an order missing
    whatever the newer one added, and that silently becomes recovery drift.
    """
    if not isinstance(result, dict):
        return DetectionResult(reason=f"Joern answered {repo} with no result object.")
    version = result.get("schemaVersion")
    if version != SCHEMA_VERSION:
        return DetectionResult(
            reason=(
                f"Joern answered {repo} with walk.sc schema {version}, and "
                f"this version reads {SCHEMA_VERSION} — redeploy it from this source tree."
            )
        )

    interactions = [
        Interaction(
            source=(row.get("from") or "").strip(),
            target=(row.get("to") or "").strip(),
            file=(row.get("file") or "").strip(),
            line=int(row.get("line") or -1),
            via=tuple(row.get("via") or ()),
            mechanism=(row.get("mechanism") or "call").strip() or "call",
            path=tuple(_step(s) for s in row.get("path") or () if isinstance(s, dict)),
            parent=(row.get("parent") or "").strip(),
        )
        for row in result.get("interactions") or []
        if (row.get("from") or "").strip() and (row.get("to") or "").strip()
    ]
    anchored = {
        (row.get("role") or "").strip(): (row.get("kind") or "").strip()
        for row in result.get("anchoredRoles") or []
        if (row.get("role") or "").strip()
    }
    return DetectionResult(
        interactions=interactions,
        entry_role=(result.get("entryRole") or "").strip(),
        entry_rule=(result.get("entryRule") or "").strip(),
        anchored_roles=anchored,
    )
