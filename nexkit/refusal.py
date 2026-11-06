"""Why GitHub refused NexKit's merge of a pull request, and what a person does about it.

`report` hands a refusal it cannot pass by itself (see `report._refused`) to `explain`,
which names the rule that refused the merge, the step that unblocks it and, for a code
owner review, the files and their owners. GitHub's data decides: the base branch's rules,
the pull request's reviews, files and conversations, and the repository's merge settings.
GitHub's own words count only where its data cannot say, such as for classic branch
protection, which NexKit's token may not read.
"""

from __future__ import annotations

import json
import re

from .autopilot import quoted
from .github import GitHubError

# Where GitHub looks for the code owners, in its order: the first one found counts.
CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
# The most owned files a reason names one by one.
FILES = 5
APP = "NexKit's App cannot review its own pull request"
# What follows an approval: NexKit merges then (see `maintain.merge_approved`).
MERGES = "NexKit then merges"
FALLBACK_DO = (
    "read GitHub's answer in the round's comment, then merge the pull request yourself or "
    "change the rule"
)


def _pattern(text):
    """A regular expression for a CODEOWNERS pattern, which follows `.gitignore` rules:
    one with a slash before its end, or that starts with one, is anchored at the root; one
    that ends with a slash matches a folder; `*` stays within a folder and `**` does not;
    a pattern matches what is under a folder it names, except one that ends in `/*`."""
    folder = text.endswith("/")
    anchored = "/" in text.rstrip("/")
    text = text.strip("/")
    body = ""
    index = 0
    while index < len(text):
        if text.startswith("**/", index):
            body += "(?:.*/)?"
            index += 3
        elif text.startswith("**", index):
            body += ".*"
            index += 2
        elif text[index] == "*":
            body += "[^/]*"
            index += 1
        elif text[index] == "?":
            body += "[^/]"
            index += 1
        else:
            body += re.escape(text[index])
            index += 1
    prefix = "" if anchored else "(?:.*/)?"
    if folder:
        suffix = "/.*"
    elif text.endswith("/*"):
        suffix = ""
    else:
        suffix = "(?:/.*)?"
    return re.compile(f"^{prefix}{body}{suffix}$")


def codeowners(text):
    """[(pattern, owners)] of a CODEOWNERS file, in its order: the last pattern that
    matches a path names its owners, and one without owners leaves the path unowned."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("#", 1)[0].split()
        if parts:
            rules.append((_pattern(parts[0]), parts[1:]))
    return rules


def owners_of(rules, path):
    """The owners of `path` under `rules`, or [] when nobody owns it."""
    found = []
    for pattern, owners in rules:
        if pattern.match(path):
            found = owners
    return found


def _message(exc):
    """GitHub's own words in a refusal, on one line: the `message` of its JSON answer."""
    text = str(exc)
    start = text.find("{")
    if start >= 0:
        try:
            found = json.loads(text[start:])
        except ValueError:
            found = None
        if isinstance(found, dict) and found.get("message"):
            text = str(found["message"])
    return " ".join(text.split())


def _approvers(gh, pr):
    """The logins, in lower case, whose latest review of `pr` approves it."""
    latest = {}
    for review in gh.reviews(pr):
        login = ((review.get("user") or {}).get("login") or "").lower()
        if login and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = review["state"]
    return {login for login, state in latest.items() if state == "APPROVED"}


def _requesters(gh, pr):
    """The logins whose latest review of `pr` requests changes."""
    latest = {}
    for review in gh.reviews(pr):
        login = (review.get("user") or {}).get("login") or ""
        if login and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = review["state"]
    return [login for login, state in latest.items() if state == "CHANGES_REQUESTED"]


def _people(logins):
    return ", ".join(quoted(login if login.startswith("@") else f"@{login}") for login in logins)


def owned_files(gh, pr, base):
    """[(file, owners)] for the files of `pr` that someone owns in the CODEOWNERS file of
    `base` and whose owners include no person who approved it, or None without a
    CODEOWNERS file. A team or an email address counts as not approved: NexKit cannot tell
    who is in a team."""
    text = None
    for path in CODEOWNERS_PATHS:
        text = gh.file_text(path, base)
        if text is not None:
            break
    if text is None:
        return None
    rules = codeowners(text)
    approved = _approvers(gh, pr)
    found = []
    for item in gh.pull_files(pr):
        owners = owners_of(rules, item.get("filename") or "")
        people = {owner.lstrip("@").lower() for owner in owners if "/" not in owner}
        if owners and not people & approved:
            found.append((item, owners))
    return found


def _file_line(item, owners):
    added, removed = item.get("additions") or 0, item.get("deletions") or 0
    return f"{quoted(item.get('filename'), 200)} (+{added} −{removed}, owned by {_people(owners)})"


def _code_owners(owned):
    """(do, why) for a merge that waits for a code owner's review of the files `owned`
    (see `owned_files`), or of files NexKit cannot name."""
    if not owned:
        return (
            f"a code owner approves the pull request, which {MERGES}",
            f"GitHub requires a code owner's review, and {APP}",
        )
    owners = list(dict.fromkeys(owner for _, found in owned for owner in found))
    files = "; ".join(_file_line(item, found) for item, found in owned[:FILES])
    more = f"; and {len(owned) - FILES} more" if len(owned) > FILES else ""
    one = len(owned) == 1
    return (
        f"approve the pull request as a code owner ({_people(owners)}), which {MERGES}",
        f"GitHub requires a code owner's review for {'this file' if one else 'these files'}, "
        f"and {APP}: {files}{more}",
    )


def _approvals(gh, pr, review, said=""):
    # Classic branch protection, which NexKit's token may not read, names its count only
    # in GitHub's words, such as "At least 2 approving reviews are required".
    words = re.search(r"at least (\d+) approving review", said)
    count = review.get("required_approving_review_count") or (int(words.group(1)) if words else 1)
    given = len(_approvers(gh, pr))
    plural = "s" if count != 1 else ""
    return (
        f"approve the pull request, which {MERGES}",
        f"GitHub requires {count} approving review{plural} ({given} given), and {APP}",
    )


def _reviews(gh, target, rules, state, lowered):
    """(do, why) when the pull request's reviews keep it from merging, or None."""
    pr, base = target["pr"], target["base"]
    review = rules.get("pull_request") or {}
    decision = state.get("decision")
    if decision == "CHANGES_REQUESTED":
        requesters = _requesters(gh, pr)
        who = f" ({_people(requesters)})" if requesters else ""
        return (
            f"the reviewer who requested changes{who} approves the pull request, which "
            f"{MERGES}, or dismisses the review and merges it",
            "GitHub does not merge a pull request while a review requests changes",
        )
    # GitHub's decision covers classic branch protection too, which NexKit's token may not
    # read; only its words then say that the review is a code owner's.
    said = "code owner" in lowered
    owners = bool(review.get("require_code_owner_review")) or said
    needed = decision == "REVIEW_REQUIRED" or (
        decision is None and (said or "approving review" in lowered)
    )
    if needed:
        owned = owned_files(gh, pr, base) if owners else []
        if owned or said or (owners and owned is None):
            return _code_owners(owned)
        return _approvals(gh, pr, review, lowered)
    unresolved = state.get("unresolved") or 0
    if unresolved and (
        review.get("required_review_thread_resolution") or "conversation" in lowered
    ):
        plural = "s" if unresolved != 1 else ""
        return (
            f"resolve the pull request's {unresolved} open review conversation{plural}, then "
            "merge it",
            "GitHub merges only once every review conversation is resolved",
        )
    return None


def _rules(gh, base):
    """{type: parameters} of the rules of `base`, the rules of one type from several
    rulesets joined, as GitHub applies them all: a flag set by any, the highest count, the
    merge methods that every one allows. {} when they cannot be read."""
    try:
        found = gh.branch_rules(base)
    except GitHubError:
        return {}
    rules = {}
    for rule in found:
        joined = rules.setdefault(rule.get("type"), {})
        for key, value in (rule.get("parameters") or {}).items():
            if key not in joined:
                joined[key] = value
            elif isinstance(value, bool):
                joined[key] = joined[key] or value
            elif isinstance(value, int):
                joined[key] = max(joined[key], value)
            elif key == "allowed_merge_methods" and isinstance(value, list):
                joined[key] = [m for m in joined[key] if m in value]
            elif isinstance(value, list):
                joined[key] = list(dict.fromkeys([*joined[key], *value]))
    return rules


def _review_state(gh, pr):
    try:
        return gh.review_state(pr)
    except GitHubError:
        return {}


def _squash_allowed(gh, rules):
    methods = (rules.get("pull_request") or {}).get("allowed_merge_methods")
    if isinstance(methods, list) and methods and "squash" not in methods:
        return False
    try:
        return gh.settings().get("allow_squash_merge", True) is not False
    except GitHubError:
        return True


def explain(gh, target, exc):
    """(what a person does, why NexKit cannot) for a merge of `target`'s pull request that
    GitHub refused with `exc` for a rule that NexKit does not pass by itself. `target` names
    the pull request's `pr` and `base`."""
    base = target["base"]
    said = _message(exc)
    lowered = said.lower()
    rules = _rules(gh, base)
    state = _review_state(gh, target["pr"])
    if state.get("draft"):
        return (
            "mark the pull request ready for review, then merge it",
            "it is a draft, which GitHub does not merge",
        )
    found = _reviews(gh, target, rules, state, lowered)
    if found:
        return found
    if "merge_queue" in rules or "merge queue" in lowered:
        return (
            "add the pull request to the merge queue",
            f"`{base}` takes changes only through its merge queue, which NexKit does not use",
        )
    if "required_signatures" in rules or "signature" in lowered:
        return (
            "merge the pull request yourself, or let NexKit's App bypass the rule",
            f"`{base}` requires signed commits, and NexKit's commits are not signed",
        )
    if "required_deployments" in rules or "deployment" in lowered:
        names = (rules.get("required_deployments") or {}).get("required_deployment_environments")
        where = f" to {', '.join(quoted(name) for name in names)}" if names else ""
        return (
            f"deploy the pull request{where}, then merge it",
            f"`{base}` requires a successful deployment before a merge",
        )
    if not _squash_allowed(gh, rules) or "squash" in lowered:
        return (
            "allow squash merges in the repository's settings, or merge the pull request yourself",
            "NexKit merges by squashing, which this repository does not allow",
        )
    return FALLBACK_DO, f"GitHub refused the merge: {quoted(said, 300)}"


def explain_safely(gh, target, exc):
    """`explain` for a run that must not fail because of it."""
    try:
        return explain(gh, target, exc)
    except GitHubError:
        return FALLBACK_DO, f"GitHub refused the merge: {quoted(_message(exc), 300)}"
