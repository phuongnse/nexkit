"""One reading of a repository's open work, for `nexkit status`.

`nexkit status` asks the same questions as an autopilot sweep, about every open issue, and
over REST that is several requests per issue, many of them twice. A `Snapshot` answers
them from a few GraphQL queries instead: one lists what is open, then queries that run side
by side read `PAGE` issues each, with their labels, milestone, parent, sub-issues,
*blocked by* relationships, edits and comments, and the open pull requests with their
head, mergeability and comments, and one more the latest closed pull requests. Every other
read goes to the client once and is kept for the rest of the run. Its answers have the
shapes REST gives, so the code that reads them is the one that sweeps run.

What a query cannot hold whole, such as a thread of more than `COMMENTS` comments, is read
over REST, once. A client without GraphQL, such as an older GitHub Enterprise Server or a
test's fake, gets the same reads, each made once.
"""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from .github import GitHubError

# How many issues or pull requests one query reads in full, how many such queries run at
# once, and the most of each connection a query holds: an item with more is read over
# REST. The list of what is open comes first, in pages of `LIST`.
PAGE = 10
WORKERS = 6
LIST = 100
COMMENTS = 100
RELATED = 50
EDITS = 100
# The closed pull requests that status reads, as REST lists them: by their last update.
CLOSED = 100

_ACTOR = "author { __typename login }"
_RELATED = f"""
  number state stateReason closedAt body {_ACTOR}
  repository {{ nameWithOwner }}
  milestone {{ number title dueOn }}
  labels(first: 50) {{ nodes {{ name }} }}
"""
_COMMENTS = f"""
  comments(first: {COMMENTS}) {{
    totalCount
    nodes {{ fullDatabaseId body createdAt updatedAt url authorAssociation {_ACTOR} }}
  }}
"""

# The numbers of the open issues and pull requests, a page of each at a time.
LIST_QUERY = f"""
query OpenWork($owner: String!, $name: String!, $issues: String, $pulls: String) {{
  repository(owner: $owner, name: $name) {{
    issues(states: OPEN, first: {LIST}, after: $issues) {{
      totalCount
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ number }}
    }}
    pullRequests(states: OPEN, first: {LIST}, after: $pulls) {{
      totalCount
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ number }}
    }}
  }}
}}
"""

ISSUE_FIELDS = f"""
fragment OpenIssue on Issue {{
  number fullDatabaseId title body state stateReason createdAt updatedAt closedAt url
  {_ACTOR}
  labels(first: 50) {{ totalCount nodes {{ name }} }}
  milestone {{ number title dueOn }}
  parent {{ {_RELATED} }}
  subIssues(first: {RELATED}) {{ totalCount nodes {{ {_RELATED} }} }}
  blockedBy(first: {RELATED}) {{ totalCount nodes {{ {_RELATED} }} }}
  blocking(first: {RELATED}) {{ totalCount nodes {{ {_RELATED} }} }}
  userContentEdits(first: {EDITS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ editedAt editor {{ __typename login }} }}
  }}
  timelineItems(itemTypes: [RENAMED_TITLE_EVENT], first: {EDITS}) {{
    pageInfo {{ hasNextPage }}
    nodes {{ ... on RenamedTitleEvent {{ createdAt actor {{ __typename login }} }} }}
  }}
  {_COMMENTS}
}}
"""

PULL_FIELDS = f"""
fragment OpenPull on PullRequest {{
  number fullDatabaseId title body state createdAt updatedAt closedAt url isDraft
  {_ACTOR}
  headRefName headRefOid headRepository {{ nameWithOwner }}
  baseRefName mergeable mergeStateStatus
  labels(first: 50) {{ totalCount nodes {{ name }} }}
  milestone {{ number title dueOn }}
  {_COMMENTS}
}}
"""

# The latest closed pull requests, without their comments, which `comments` reads for
# those status shows.
CLOSED_QUERY = f"""
query Closed($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(states: [CLOSED, MERGED], first: {CLOSED},
                 orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      nodes {{
        number title url state createdAt updatedAt closedAt mergedAt
        mergedBy {{ __typename login }}
        headRefName headRefOid headRepository {{ nameWithOwner }} baseRefName
      }}
    }}
  }}
}}
"""

COMMENT_FIELDS = f"""
fragment Thread on IssueOrPullRequest {{
  ... on Issue {{ number {_COMMENTS} }}
  ... on PullRequest {{ number {_COMMENTS} }}
}}
"""


def batch_query(name, fragment, field, numbers):
    """A query that reads the items `numbers` in full, each under an alias of its number."""
    items = "\n".join(f"    n{n}: {field}(number: {n}) {{ ...{fragment} }}" for n in numbers)
    text = {"OpenIssue": ISSUE_FIELDS, "OpenPull": PULL_FIELDS, "Thread": COMMENT_FIELDS}
    text = text[fragment]
    return (
        f"query {name}($owner: String!, $name: String!) {{\n"
        f"  repository(owner: $owner, name: $name) {{\n{items}\n  }}\n}}\n{text}"
    )


MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}
# The client's methods that change something: never kept.
WRITES = frozenset(
    {
        "request",
        "comment",
        "update_comment",
        "react",
        "create_issue",
        "update_issue",
        "add_labels",
        "add_sub_issue",
        "add_blocked_by",
        "close_issue",
        "close_pull",
        "create_pull",
        "create_review",
        "update_branch",
        "merge",
        "set_status",
        "dispatch",
        "delete_branch",
        "cancel_run",
        "rerun_failed_jobs",
    }
)


def _user(actor):
    """A REST user from a GraphQL actor: GraphQL names an App's bot without the `[bot]`
    that REST gives it, and REST shows a deleted account as `ghost`."""
    if not actor:
        return {"login": "ghost", "type": "User"}
    bot = actor.get("__typename") == "Bot"
    login = actor.get("login") or "ghost"
    return {"login": f"{login}[bot]" if bot else login, "type": "Bot" if bot else "User"}


def _login(actor):
    """A login as REST writes it, or "" for someone GitHub cannot name."""
    return _user(actor)["login"] if actor else ""


def _lower(value):
    return value.lower() if isinstance(value, str) else None


def _milestone(found):
    if not found:
        return None
    return {"number": found["number"], "title": found.get("title"), "due_on": found.get("dueOn")}


def _labels(node):
    return [{"name": item["name"]} for item in (node.get("labels") or {}).get("nodes") or []]


class Snapshot:
    """A read-only view of a GitHub client for one run of `nexkit status`: each object is
    read once, the open issues and pull requests in batches (see `load`). It answers the
    client's read methods; anything it does not know goes to the client, once."""

    def __init__(self, gh):
        self._gh = gh
        self.repository = gh.repository
        self._api = getattr(gh, "api_url", "https://api.github.com")
        self._issues = {}  # number -> open issue, or a pull request as /issues lists it
        self._pulls = {}  # number -> open pull request, as /pulls/<number> gives it
        self._known = {}  # (what, number) -> what a page held whole about it
        self._closed = None  # the latest closed pull requests, as /pulls lists them
        self._memo = {}
        self._lock = threading.Lock()
        self.loaded = False

    # -- reading in batches -----------------------------------------------------------

    def load(self, progress=None):
        """Read the open issues and pull requests in batches, `PAGE` items a query and
        `WORKERS` queries at once, and the latest closed pull requests, saying how far it
        got through `progress(text)`. A client without GraphQL, or a GraphQL that lacks a
        field, as on an older GitHub Enterprise Server, leaves every read to the client."""
        say = progress or (lambda text: None)
        if not hasattr(self._gh, "graphql"):
            return self
        try:
            issues, pulls = self._open_numbers()
            say(f"found {len(issues)} open issues and {len(pulls)} open pull requests")
            jobs = [
                ("Issues", "OpenIssue", "issue", issues[at : at + PAGE])
                for at in range(0, len(issues), PAGE)
            ]
            jobs += [
                ("Pulls", "OpenPull", "pullRequest", pulls[at : at + PAGE])
                for at in range(0, len(pulls), PAGE)
            ]
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                closed = pool.submit(self._query, CLOSED_QUERY)
                for job, found in zip(jobs, pool.map(self._batch, jobs), strict=True):
                    for number in job[3]:
                        node = found.get(f"n{number}")
                        if node:
                            (self._add_issue if job[2] == "issue" else self._add_pull)(node)
                    if job[2] == "issue":
                        done = sum(1 for n in issues if n in self._issues)
                        say(f"read {done} of {len(issues)} open issues")
                    else:
                        done = sum(1 for n in pulls if n in self._pulls)
                        say(f"read {done} of {len(pulls)} open pull requests")
                nodes = (closed.result().get("pullRequests") or {}).get("nodes") or []
            self._closed = [self._closed_pull(node) for node in nodes if node]
        except GitHubError as exc:
            say(f"could not read in batches ({exc}); reading one by one")
            self._issues, self._pulls, self._known, self._closed = {}, {}, {}, None
            return self
        self.loaded = True
        return self

    def _batch(self, job):
        return self._query(batch_query(*job))

    def _query(self, query, **variables):
        owner, name = self.repository.split("/", 1)
        data = self._gh.graphql(query, {"owner": owner, "name": name, **variables})
        return data.get("repository") or {}

    def _open_numbers(self):
        """(numbers of the open issues, numbers of the open pull requests), oldest first."""
        found = {"issues": [], "pullRequests": []}
        after = {"issues": None, "pullRequests": None}
        more = {"issues": True, "pullRequests": True}
        while any(more.values()):
            page = self._query(LIST_QUERY, issues=after["issues"], pulls=after["pullRequests"])
            for field in found:
                if not more[field]:
                    continue
                part = page.get(field) or {}
                found[field] += [n["number"] for n in part.get("nodes") or [] if n]
                info = part.get("pageInfo") or {}
                more[field] = bool(info.get("hasNextPage"))
                after[field] = info.get("endCursor")
        return sorted(set(found["issues"])), sorted(set(found["pullRequests"]))

    def read_comments(self, numbers):
        """Read the comments of the issues or pull requests `numbers` in batches, such as
        the closed pull requests status shows, when this snapshot reads in batches."""
        wanted = [n for n in dict.fromkeys(numbers) if ("comments", n) not in self._known]
        if not self.loaded or not wanted:
            return
        kind = ("Threads", "Thread", "issueOrPullRequest")
        jobs = [(*kind, wanted[at : at + PAGE]) for at in range(0, len(wanted), PAGE)]
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for (*_, part), found in zip(jobs, pool.map(self._batch, jobs), strict=True):
                    for number in part:
                        if found.get(f"n{number}"):
                            self._comments(number, found[f"n{number}"])
        except GitHubError:
            return

    def _repository_url(self, node):
        name = ((node.get("repository") or {}).get("nameWithOwner")) or self.repository
        return f"{self._api}/repos/{name}"

    def _related(self, node):
        """An issue that a relationship names, as REST lists it."""
        return {
            "number": node["number"],
            "state": _lower(node.get("state")),
            "state_reason": _lower(node.get("stateReason")),
            "closed_at": node.get("closedAt"),
            "body": node.get("body") or "",
            "user": _user(node.get("author")),
            "labels": _labels(node),
            "milestone": _milestone(node.get("milestone")),
            "repository_url": self._repository_url(node),
        }

    def _item(self, node):
        """What /issues lists for an issue or a pull request."""
        return {
            "number": node["number"],
            "id": int(node["fullDatabaseId"]) if node.get("fullDatabaseId") else None,
            "title": node.get("title") or "",
            "body": node.get("body") or "",
            "state": _lower(node.get("state")) or "open",
            "state_reason": _lower(node.get("stateReason")),
            "user": _user(node.get("author")),
            "labels": _labels(node),
            "milestone": _milestone(node.get("milestone")),
            "html_url": node.get("url") or "",
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
            "closed_at": node.get("closedAt"),
            "comments": (node.get("comments") or {}).get("totalCount") or 0,
            "repository_url": f"{self._api}/repos/{self.repository}",
        }

    def _comments(self, number, node):
        found = node.get("comments") or {}
        nodes = found.get("nodes") or []
        if (found.get("totalCount") or 0) > len(nodes):
            return
        self._known[("comments", number)] = [
            {
                "id": int(c["fullDatabaseId"]),
                "body": c.get("body") or "",
                "user": _user(c.get("author")),
                "author_association": c.get("authorAssociation"),
                "created_at": c.get("createdAt"),
                "updated_at": c.get("updatedAt"),
                "html_url": c.get("url") or "",
            }
            for c in nodes
            if c and c.get("fullDatabaseId")
        ]

    def _connection(self, what, number, found):
        nodes = (found or {}).get("nodes") or []
        if (found or {}).get("totalCount", 0) <= len(nodes):
            self._known[(what, number)] = [self._related(n) for n in nodes if n]

    def _add_issue(self, node):
        number = node["number"]
        if (node.get("labels") or {}).get("totalCount", 0) > len(_labels(node)):
            # A page holds only the first labels: this issue is read over REST.
            return
        self._issues[number] = self._item(node)
        parent = node.get("parent")
        self._known[("parent", number)] = self._related(parent) if parent else None
        self._connection("sub_issues", number, node.get("subIssues"))
        self._connection("blocked_by", number, node.get("blockedBy"))
        self._connection("blocking", number, node.get("blocking"))
        self._comments(number, node)
        edits = node.get("userContentEdits") or {}
        renames = node.get("timelineItems") or {}
        if not (renames.get("pageInfo") or {}).get("hasNextPage"):
            changes = [
                (_login(e.get("editor")), e.get("editedAt")) for e in edits.get("nodes") or [] if e
            ]
            if (edits.get("pageInfo") or {}).get("hasNextPage"):
                changes.append(("", None))
            changes += [
                (_login(r.get("actor")), r.get("createdAt"))
                for r in renames.get("nodes") or []
                if r and "createdAt" in r
            ]
            self._known[("edits", number)] = changes

    def _closed_pull(self, node):
        """A closed pull request, as /pulls/<number> gives it."""
        head_repo = (node.get("headRepository") or {}).get("nameWithOwner")
        merged_by = node.get("mergedBy")
        return {
            "number": node["number"],
            "state": "closed",
            "title": node.get("title") or "",
            "html_url": node.get("url") or "",
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
            "closed_at": node.get("closedAt"),
            "merged_at": node.get("mergedAt"),
            "merged_by": _user(merged_by) if merged_by else None,
            "head": {
                "ref": node.get("headRefName"),
                "sha": node.get("headRefOid"),
                "repo": {"full_name": head_repo} if head_repo else None,
            },
            "base": {"ref": node.get("baseRefName")},
        }

    def _add_pull(self, node):
        number = node["number"]
        if (node.get("labels") or {}).get("totalCount", 0) > len(_labels(node)):
            return
        head_repo = (node.get("headRepository") or {}).get("nameWithOwner")
        self._issues[number] = {**self._item(node), "pull_request": {}}
        self._pulls[number] = {
            "number": number,
            "state": "open",
            "title": node.get("title") or "",
            "body": node.get("body") or "",
            "html_url": node.get("url") or "",
            "user": _user(node.get("author")),
            "created_at": node.get("createdAt"),
            "updated_at": node.get("updatedAt"),
            "draft": bool(node.get("isDraft")),
            "head": {
                "ref": node.get("headRefName"),
                "sha": node.get("headRefOid"),
                "repo": {"full_name": head_repo} if head_repo else None,
            },
            "base": {"ref": node.get("baseRefName")},
            "mergeable": MERGEABLE.get(node.get("mergeable")),
            "mergeable_state": _lower(node.get("mergeStateStatus")) or "unknown",
            "labels": _labels(node),
            "milestone": _milestone(node.get("milestone")),
        }
        self._comments(number, node)

    # -- answers ---------------------------------------------------------------------

    def _once(self, name, *args, **kwargs):
        """The client's answer to a read, asked once per run."""
        key = (name, args, tuple(sorted(kwargs.items())))
        with self._lock:
            if key in self._memo:
                return copy.copy(self._memo[key])
        found = getattr(self._gh, name)(*args, **kwargs)
        with self._lock:
            self._memo.setdefault(key, found)
            return copy.copy(self._memo[key])

    def _known_or(self, what, number, name):
        if self.loaded and (what, number) in self._known:
            return copy.copy(self._known[(what, number)])
        return self._once(name, number)

    def __getattr__(self, name):
        # Reads this class does not answer itself go to the client, once each. Status never
        # writes; a write would go through as it is.
        found = getattr(self._gh, name)
        if not callable(found) or name in WRITES:
            return found
        return lambda *args, **kwargs: self._once(name, *args, **kwargs)

    def _open(self, pulls=True):
        return [
            item
            for item in self._issues.values()
            if item["state"] == "open" and (pulls or "pull_request" not in item)
        ]

    def open_issues(self):
        if not self.loaded:
            return self._once("open_issues")
        return sorted(self._open(), key=lambda i: -i["number"])

    def scoped_issues(self, milestone=None):
        if not self.loaded:
            return self._once("scoped_issues", milestone=milestone)
        found = [
            item
            for item in self._open()
            if milestone is None or (item.get("milestone") or {}).get("number") == milestone
        ]
        return sorted(found, key=lambda i: i["number"])

    def labeled_issues(self, label):
        if not self.loaded:
            return self._once("labeled_issues", label)
        found = [i for i in self._open(pulls=False) if label in {n["name"] for n in i["labels"]}]
        return sorted(found, key=lambda i: i["number"])

    def labeled_without_milestone(self, label):
        if not self.loaded:
            return self._once("labeled_without_milestone", label)
        return [i for i in self.labeled_issues(label)[::-1] if not i.get("milestone")]

    def issues_by(self, creator, limit=100, state="all", since=None):
        if not self.loaded or state != "open" or since:
            return self._once("issues_by", creator, limit, state, since)
        # GitHub counts pull requests toward `limit`, so this lists them before it cuts.
        found = [i for i in self._open() if i["user"]["login"] == creator]
        found = sorted(found, key=lambda i: -i["number"])[:limit]
        return [i for i in found if "pull_request" not in i]

    def list_pulls(self, state="open", limit=1000):
        if self.loaded and state == "closed" and limit <= CLOSED:
            return [dict(pull) for pull in self._closed[:limit]]
        if not self.loaded or state != "open":
            return self._once("list_pulls", state, limit)
        found = sorted(self._pulls.values(), key=lambda p: p["updated_at"] or "", reverse=True)
        return found[:limit]

    def issue(self, number):
        if self.loaded and number in self._issues:
            return dict(self._issues[number])
        return self._once("issue", number)

    def pull(self, number):
        if self.loaded and number in self._pulls:
            return copy.deepcopy(self._pulls[number])
        closed = {p["number"]: p for p in self._closed or []}
        if self.loaded and number in closed:
            return copy.deepcopy(closed[number])
        return self._once("pull", number)

    def comments(self, number):
        return self._known_or("comments", number, "comments")

    def parent_issue(self, number):
        if self.loaded and ("parent", number) in self._known:
            parent = self._known[("parent", number)]
            if parent is None:
                return None
            here = parent["repository_url"].lower().endswith(f"/repos/{self.repository}".lower())
            if here and parent["number"] in self._issues:
                return dict(self._issues[parent["number"]])
            return dict(parent)
        return self._once("parent_issue", number)

    def sub_issues(self, number):
        return self._known_or("sub_issues", number, "sub_issues")

    def blocked_by(self, number):
        return self._known_or("blocked_by", number, "blocked_by")

    def blocking(self, number):
        return self._known_or("blocking", number, "blocking")

    def issue_edits(self, number):
        return self._known_or("edits", number, "issue_edits")

    def issue_editors(self, number):
        return sorted({login for login, _ in self.issue_edits(number)})
