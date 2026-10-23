"""POST one query to the Joern service and read its result back.

The only way this package reaches Joern. ``detector.py`` sends
``joern/walk.sc`` and gets an interaction order back; the service loads the
repo's prebuilt CPG, runs the script against it, and returns whatever JSON the
script wrote.

**The script goes over the wire.** The service runs whatever query it is sent,
so this posts the script from *this* source tree, and the frontend is resolved
here too. Together those keep the analysis pinned to this package: the
deployed service decides where the checkout is and which JVM runs, and nothing
about what is computed.

**The repo is named, not sent.** The service resolves a bare repo name against
its own checkout roots (``/data/cloned_repo`` for Python, ``/data_java/cloned_repo``
for Java), so a caller that has the repo locally under the same directory name
needs to send nothing but the name. What it does mean is
that the *service* must have the checkout: a repo only on the caller's disk comes
back 404, which is why that case gets its own message.

**This client only queries; it never builds.** The service keeps a CPG per repo
and building one is a separate, minutes-long call against its ``POST /cpg`` — run
ahead of time, by an operator, for every repo a batch is about to touch (the
service ships ``prebuild.sh`` for exactly that). A repo with no CPG comes back
404 and this reports it as unavailable with the command to fix it, rather than
quietly triggering a build: a scoring run that pauses ten minutes mid-batch to
construct a graph is a surprise, and deciding *when* to spend that time is the
operator's call, not a side effect of asking a question.

``urllib`` rather than ``httpx``/``requests``: this package has no dependencies,
and a JSON POST is not a reason to add one. The calls are blocking, so an async caller should run them
in a worker thread.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from . import config

logger = logging.getLogger(__name__)

#: Repo language -> the Joern frontend that handles it. Joern auto-detects when
#: given nothing, which is right for a mixed repo but wrong for one whose
#: recovery result already names a language; naming it skips the guess and, more
#: importantly, stops a repo with one stray ``.js`` file from being analysed as a
#: JavaScript project. A language absent from this map falls through to
#: auto-detection rather than failing.
_FRONTENDS = {
    "python": "PYTHONSRC",
    "java": "JAVASRC",
    "kotlin": "KOTLIN",
    "javascript": "JSSRC",
    "typescript": "JSSRC",
    "go": "GOLANG",
    "c": "NEWC",
    "cpp": "NEWC",
    "csharp": "CSHARP",
    "php": "PHP",
    "ruby": "RUBYSRC",
}


def frontend_for(language: str) -> str:
    """The Joern frontend name for a repo language, or "" to auto-detect."""
    return _FRONTENDS.get((language or "").strip().lower(), "")

#: Returned by :func:`_post` for the one 404 that is not a failure — see
#: :func:`_is_not_built`.
_NOT_BUILT = object()

#: How much of the service's error body is logged. A failed export answers with
#: Joern's own stdout/stderr tails, which is the only thing that ever explains
#: one — but they are long.
_DETAIL_TAIL = 2000


class ServiceError(RuntimeError):
    """The service could not be reached, or answered with something unusable."""


def query(
    repo: str, language: str, script: str, params: dict[str, str] | None = None
) -> dict | None:
    """Run ``script`` against ``repo``'s CPG; its JSON result, or ``None``.

    ``params`` are passed to the script's ``@main`` as ``--param k=v``, on top of
    the ``cpgPath`` / ``inputPath`` / ``outputPath`` the service supplies itself.

    ``None`` rather than an exception: every way this fails is a fact about the
    service or the deployment, not about the repo being analysed, and the caller
    turns it into one ``BackendUnavailable`` naming the repo. Everything that went
    wrong is logged at WARNING with enough of the service's answer to diagnose it
    — including the one failure that is really an instruction, "build the CPG
    first".
    """
    url = config.JOERN_SERVICE_URL
    if not url:
        logger.warning(
            "joern: JOERN_SERVICE_URL is unset — there is nothing to POST to, and the "
            "query service is the only way this package obtains a call graph. Point it "
            "at a deployment of the joern_query_service/ project."
        )
        return None

    sent = dict(params or {})
    frontend = frontend_for(language)
    if frontend:
        # Resolved here, not there. See the module docstring.
        sent["language"] = frontend
    payload = {
        "repo": repo,
        "language": language or "",
        "query": script,
        "params": sent,
        "timeout": config.JOERN_TIMEOUT_SECONDS,
    }

    logger.info("joern: querying %s (%s) at %s", repo, language or "auto", url)
    body = _post(url, "/query", payload, repo)
    if body is _NOT_BUILT:
        logger.warning(
            "joern: the query service has no CPG for %s, and this client does not build "
            "one — building is a separate, minutes-long call so that it never happens "
            "unexpectedly inside a scoring run. On the service host:\n"
            "    ./prebuild.sh %s            # or: curl -XPOST $URL/cpg -d "
            "'{\"repo\":\"%s\",\"language\":\"%s\"}'",
            repo, repo, repo, language or "python",
        )
        return None
    if not isinstance(body, dict):
        return None

    result = body.get("graph")
    if not isinstance(result, dict):
        # The service returns 200 with `graph: null` when the script wrote no
        # output file — a query that only prints. For an export that is a
        # failure, and a silent one unless it is called out here.
        logger.warning(
            "joern: the query service ran the script for %s but it wrote no result "
            "(exit %s). stdout tail: %s",
            repo, body.get("returnCode"), (body.get("stdout") or "")[-_DETAIL_TAIL:],
        )
        return None

    logger.info(
        "joern: the query service answered %s in %ss from %s",
        repo, body.get("elapsedSeconds", "?"), body.get("cpgPath", "?"),
    )
    return result


def _post(url: str, path: str, payload: dict, repo: str) -> dict | object | None:
    """POST ``payload`` and return the decoded body, ``_NOT_BUILT``, or ``None``.

    ``_NOT_BUILT`` is its own return rather than an error because it is the one
    4xx that is not a problem — it is how the service says "build it first", and
    :func:`export` does exactly that.
    """
    request = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if config.JOERN_SERVICE_TOKEN:
        request.add_header("Authorization", f"Bearer {config.JOERN_SERVICE_TOKEN}")

    try:
        with urllib.request.urlopen(
            request, timeout=config.JOERN_SERVICE_TIMEOUT_SECONDS
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and _is_not_built(e):
            return _NOT_BUILT
        _log_http_error(e, repo, url)
        return None
    except urllib.error.URLError as e:
        logger.warning(
            "joern: cannot reach the query service at %s: %s. Is it deployed and is the "
            "tunnel/port open? `curl %s/health` answers without a token.",
            url, e.reason, url,
        )
        return None
    except TimeoutError:
        logger.warning(
            "joern: the query service did not answer within "
            "JOERN_SERVICE_TIMEOUT_SECONDS=%ds for %s. The CPG is prebuilt, so a query "
            "should be seconds — check the service's logs.",
            config.JOERN_SERVICE_TIMEOUT_SECONDS, repo,
        )
        return None
    except (ValueError, OSError) as e:
        logger.warning("joern: unreadable answer from the query service for %s: %s", repo, e)
        return None


def _is_not_built(e: urllib.error.HTTPError) -> bool:
    """Whether a 404 is "no CPG yet" rather than "no such repo here".

    Read from the body's ``code``, which the service sets explicitly, rather than
    from the status alone — the two 404s need opposite responses, and one of them
    must not trigger a build for a repo that does not exist.
    """
    try:
        detail = json.loads(e.read().decode("utf-8")).get("detail")
    except Exception:  # noqa: BLE001 — the body is diagnostic, never load-bearing
        return False
    return isinstance(detail, dict) and detail.get("code") == "cpg_not_built"


def _log_http_error(e: urllib.error.HTTPError, repo: str, url: str) -> None:
    """One WARNING naming what the status actually means for this caller.

    The statuses are distinct on purpose (see the service's ``/query`` docstring)
    and each has a different fix, so they are not collapsed into "the service
    said no".
    """
    try:
        detail = e.read().decode("utf-8", errors="replace")[-_DETAIL_TAIL:]
    except Exception:  # noqa: BLE001 — the body is diagnostic, never load-bearing
        detail = ""

    if e.code == 401:
        hint = (
            "the service rejected the token. JOERN_SERVICE_TOKEN here must match the "
            "one the container was deployed with."
        )
    elif e.code == 404:
        # Reached only for the *other* 404 — a repo the service does not have.
        # "no CPG built yet" is handled by _is_not_built and never gets here.
        hint = (
            f"the service has no checkout named {repo!r}. It resolves repos against its "
            f"own roots, so the repo has to exist *there*, under the same directory name "
            f"it has here. `curl {url}/repos?language=python` lists what it has."
        )
    elif e.code == 400:
        hint = "the service rejected the repo name."
    elif e.code == 503:
        hint = "the service's build queue never cleared — another export is still running."
    elif e.code == 504:
        hint = (
            "the build ran past the service's own JOERN_TIMEOUT_SECONDS. Raise it on the "
            "service, not here."
        )
    else:
        hint = "joern ran and failed there; the detail below is its own diagnostics."

    logger.warning("joern: export for %s failed (HTTP %d) — %s\n%s", repo, e.code, hint, detail)
