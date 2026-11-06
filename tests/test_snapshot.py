"""`nexkit status` reads a repository in a few batched requests (nexkit/snapshot.py).

These tests run the real `GitHub` client against a recorded API: an in-memory repository
that answers its REST paths and GraphQL queries as GitHub does, and logs each request.
"""

import json
import re
import unittest

from nexkit import snapshot
from nexkit.github import GitHub, GitHubError
from nexkit.state import PLAN_MARKER, render_run
from nexkit.status import collect, render
from tests.support import BOT_LOGIN, make_config

API = "https://api.github.com"
REPO = "acme/app"
ROLES = {"alice": "write", "olivia": "admin", "bob": "read"}


def _bot(login):
    return login.endswith("[bot]")


def _actor(login):
    """A GraphQL actor for a REST login."""
    if not login:
        return None
    if _bot(login):
        return {"__typename": "Bot", "login": login.removesuffix("[bot]")}
    return {"__typename": "User", "login": login}


class Recorded:
    """An in-memory repository that answers as GitHub's API does."""

    def __init__(self):
        self.items = {}  # number -> issue or pull request
        self.milestones = [{"number": 1, "title": "M1", "due_on": None}]
        self.next_comment = 5_000_000_000  # comment ids beyond 32 bits, as GitHub's are

    # -- the repository ---------------------------------------------------------------

    def issue(self, number, user="alice", milestone=1, labels=(), body="Please add it.", **kw):
        self.items[number] = {
            "number": number,
            "title": f"Issue {number}",
            "body": body,
            "user": user,
            "milestone": milestone,
            "labels": list(labels),
            "comments": [],
            "parent": None,
            "blocked_by": [],
            "edits": [],
            "renames": [],
            "pull": None,
            "created_at": f"2026-01-01T00:{number % 60:02d}:00Z",
            **kw,
        }
        return self.items[number]

    def pull(self, number, issue, mergeable=True):
        item = self.issue(number, user=BOT_LOGIN, milestone=None, body="Closes it.")
        item["pull"] = {
            "head": f"nexkit/issue-{issue}",
            "sha": f"{number:040x}",
            "mergeable": mergeable,
            "state": "clean" if mergeable else "dirty",
        }
        return item

    def comment(self, number, body, user=BOT_LOGIN, association="NONE"):
        self.next_comment += 1
        self.items[number]["comments"].append(
            {
                "id": self.next_comment,
                "body": body,
                "user": user,
                "association": association,
                "created_at": "2026-01-02T00:00:00Z",
            }
        )

    # -- REST --------------------------------------------------------------------------

    def rest_issue(self, item):
        found = {
            "number": item["number"],
            "id": 9_000_000_000 + item["number"],
            "title": item["title"],
            "body": item["body"],
            "state": item.get("state", "open"),
            "state_reason": None,
            "user": {"login": item["user"], "type": "Bot" if _bot(item["user"]) else "User"},
            "labels": [{"name": name} for name in item["labels"]],
            "milestone": self.rest_milestone(item["milestone"]),
            "html_url": f"https://github.com/{REPO}/issues/{item['number']}",
            "created_at": item["created_at"],
            "updated_at": item["created_at"],
            "closed_at": None,
            "comments": len(item["comments"]),
            "repository_url": f"{API}/repos/{REPO}",
        }
        if item["pull"]:
            found["pull_request"] = {}
            found["html_url"] = f"https://github.com/{REPO}/pull/{item['number']}"
        return found

    def rest_milestone(self, number):
        if number is None:
            return None
        found = next(m for m in self.milestones if m["number"] == number)
        return {"number": number, "title": found["title"], "due_on": found["due_on"]}

    def rest_comment(self, number, c):
        return {
            "id": c["id"],
            "body": c["body"],
            "user": {"login": c["user"], "type": "Bot" if _bot(c["user"]) else "User"},
            "author_association": c["association"],
            "created_at": c["created_at"],
            "updated_at": c["created_at"],
            "html_url": f"https://github.com/{REPO}/issues/{number}#issuecomment-{c['id']}",
        }

    def rest_pull(self, item, detail=False):
        pull = item["pull"]
        found = {
            "number": item["number"],
            "state": item.get("state", "open"),
            "title": item["title"],
            "body": item["body"],
            "html_url": f"https://github.com/{REPO}/pull/{item['number']}",
            "closed_at": pull.get("closed_at"),
            "merged_at": pull.get("merged_at"),
            "user": {"login": item["user"], "type": "Bot"},
            "created_at": item["created_at"],
            "updated_at": item["created_at"],
            "draft": False,
            "head": {"ref": pull["head"], "sha": pull["sha"], "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
            "labels": [{"name": name} for name in item["labels"]],
            "milestone": self.rest_milestone(item["milestone"]),
        }
        if detail:
            found.update(mergeable=pull["mergeable"], mergeable_state=pull["state"])
            by = pull.get("merged_by")
            found["merged_by"] = (
                {"login": by, "type": "Bot" if _bot(by) else "User"} if by else None
            )
        return found

    def sub_issues(self, number):
        return [i for i in self.items.values() if i["parent"] == number]

    def blocking(self, number):
        return [i for i in self.items.values() if number in i["blocked_by"]]

    def answer(self, method, path, body, params):
        if method == "POST" and path.endswith("/graphql"):
            return self.graphql(body["query"], body["variables"])
        base = f"/repos/{REPO}"
        rest = path.removeprefix(base)
        page, size = int(params.get("page", 1)), int(params.get("per_page", 30))

        def paged(found):
            return found[(page - 1) * size : page * size]

        if rest == "":
            return {"default_branch": "main", "delete_branch_on_merge": False}
        if rest == "/milestones":
            return paged(
                [
                    {
                        **m,
                        "open_issues": sum(
                            i["milestone"] == m["number"] for i in self.items.values()
                        ),
                        "closed_issues": 0,
                    }
                    for m in self.milestones
                ]
            )
        match = re.fullmatch(r"/collaborators/([^/]+)/permission", rest)
        if match:
            return {"role_name": ROLES.get(match.group(1), "none")}
        if rest == "/issues":
            return paged(self.list_issues(params))
        if rest == "/pulls":
            state = params.get("state")
            found = [
                i
                for i in self.items.values()
                if i["pull"] and state in ("all", i.get("state", "open"))
            ]
            found.sort(key=lambda i: i["created_at"], reverse=True)
            return paged([self.rest_pull(i) for i in found])
        match = re.fullmatch(r"/pulls/(\d+)", rest)
        if match:
            return self.rest_pull(self.items[int(match.group(1))], detail=True)
        match = re.fullmatch(r"/issues/(\d+)(/.*)?", rest)
        if match:
            item = self.items.get(int(match.group(1)))
            if item is None:
                raise GitHubError(404, "Not Found")
            return self.issue_path(item, match.group(2) or "", paged)
        raise AssertionError(f"no answer for {method} {path}")

    def list_issues(self, params):
        state = params.get("state", "open")
        found = [
            i for i in self.items.values() if state == "all" or i.get("state", "open") == state
        ]
        if "milestone" in params:
            wanted = params["milestone"]
            found = [
                i
                for i in found
                if (wanted == "none" and i["milestone"] is None) or str(i["milestone"]) == wanted
            ]
        if "labels" in params:
            found = [i for i in found if params["labels"] in i["labels"]]
        if "creator" in params:
            found = [i for i in found if i["user"] == params["creator"]]
        ascending = params.get("direction") == "asc"
        found.sort(key=lambda i: i["number"] if ascending else -i["number"])
        return [self.rest_issue(i) for i in found]

    def issue_path(self, item, rest, paged):
        number = item["number"]
        if rest == "":
            return self.rest_issue(item)
        if rest == "/comments":
            return paged([self.rest_comment(number, c) for c in item["comments"]])
        if rest == "/parent":
            if item["parent"] is None:
                raise GitHubError(404, "Not Found")
            return self.rest_issue(self.items[item["parent"]])
        if rest == "/sub_issues":
            return paged([self.rest_issue(i) for i in self.sub_issues(number)])
        if rest == "/dependencies/blocked_by":
            return paged([self.rest_issue(self.items[n]) for n in item["blocked_by"]])
        if rest == "/dependencies/blocking":
            return paged([self.rest_issue(i) for i in self.blocking(number)])
        if rest == "/events":
            events = [
                {"event": "renamed", "actor": {"login": login}, "created_at": at}
                for login, at in item["renames"]
            ]
            return paged(events)
        raise AssertionError(f"no answer for /issues/{number}{rest}")

    # -- GraphQL -----------------------------------------------------------------------

    def related(self, item):
        return {
            "number": item["number"],
            "state": item.get("state", "open").upper(),
            "stateReason": None,
            "closedAt": None,
            "body": item["body"],
            "author": _actor(item["user"]),
            "repository": {"nameWithOwner": REPO},
            "milestone": self.gql_milestone(item["milestone"]),
            "labels": {"nodes": [{"name": n} for n in item["labels"]]},
        }

    def gql_milestone(self, number):
        found = self.rest_milestone(number)
        return found and {"number": number, "title": found["title"], "dueOn": None}

    def connection(self, items, size):
        return {"totalCount": len(items), "nodes": [self.related(i) for i in items[:size]]}

    def gql_comments(self, item):
        return {
            "totalCount": len(item["comments"]),
            "nodes": [
                {
                    "fullDatabaseId": str(c["id"]),
                    "body": c["body"],
                    "createdAt": c["created_at"],
                    "updatedAt": c["created_at"],
                    "url": f"https://github.com/{REPO}/issues/{item['number']}#issuecomment-{c['id']}",
                    "authorAssociation": c["association"],
                    "author": _actor(c["user"]),
                }
                for c in item["comments"][: snapshot.COMMENTS]
            ],
        }

    def gql_item(self, item):
        return {
            "number": item["number"],
            "fullDatabaseId": str(9_000_000_000 + item["number"]),
            "title": item["title"],
            "body": item["body"],
            "state": "OPEN",
            "stateReason": None,
            "createdAt": item["created_at"],
            "updatedAt": item["created_at"],
            "closedAt": None,
            "url": self.rest_issue(item)["html_url"],
            "author": _actor(item["user"]),
            "labels": {
                "totalCount": len(item["labels"]),
                "nodes": [{"name": n} for n in item["labels"][:50]],
            },
            "milestone": self.gql_milestone(item["milestone"]),
            "comments": self.gql_comments(item),
        }

    def gql_issue(self, item):
        number = item["number"]
        return {
            **self.gql_item(item),
            "parent": self.related(self.items[item["parent"]]) if item["parent"] else None,
            "subIssues": self.connection(self.sub_issues(number), snapshot.RELATED),
            # GitHub's GraphQL lists them in another order than REST does.
            "blockedBy": self.connection(
                [self.items[n] for n in item["blocked_by"]][::-1], snapshot.RELATED
            ),
            "blocking": self.connection(self.blocking(number), snapshot.RELATED),
            "userContentEdits": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [{"editedAt": at, "editor": _actor(login)} for login, at in item["edits"]],
            },
            "timelineItems": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {"createdAt": at, "actor": _actor(login)} for login, at in item["renames"]
                ],
            },
        }

    def gql_pull(self, item):
        pull = item["pull"]
        state = {True: "MERGEABLE", False: "CONFLICTING", None: "UNKNOWN"}[pull["mergeable"]]
        return {
            **self.gql_item(item),
            "isDraft": False,
            "headRefName": pull["head"],
            "headRefOid": pull["sha"],
            "headRepository": {"nameWithOwner": REPO},
            "baseRefName": "main",
            "mergeable": state,
            "mergeStateStatus": pull["state"].upper(),
        }

    def graphql(self, query, variables):
        open_ = sorted(
            (i for i in self.items.values() if i.get("state", "open") == "open"),
            key=lambda i: i["number"],
        )
        if query == snapshot.LIST_QUERY:
            found = {}
            for field, pulls, after in (
                ("issues", False, variables["issues"]),
                ("pullRequests", True, variables["pulls"]),
            ):
                items = [i for i in open_ if bool(i["pull"]) == pulls]
                start = int(after or 0)
                found[field] = {
                    "totalCount": len(items),
                    "pageInfo": {
                        "hasNextPage": start + snapshot.LIST < len(items),
                        "endCursor": str(start + snapshot.LIST),
                    },
                    "nodes": [
                        {"number": i["number"]} for i in items[start : start + snapshot.LIST]
                    ],
                }
            return {"data": {"repository": found}}
        if query == snapshot.CLOSED_QUERY:
            closed = [i for i in self.items.values() if i["pull"] and i.get("state") == "closed"]
            closed.sort(key=lambda i: i["created_at"], reverse=True)
            nodes = [self.gql_closed(i) for i in closed[: snapshot.CLOSED]]
            return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}
        batch = re.match(r"query (Issues|Pulls|Threads)\(", query)
        if batch:
            found = {}
            for number in map(int, re.findall(r"n(\d+): \w+\(number: \d+\)", query)):
                item = self.items.get(number)
                if item is None:
                    found[f"n{number}"] = None
                elif batch.group(1) == "Issues":
                    found[f"n{number}"] = self.gql_issue(item)
                elif batch.group(1) == "Pulls":
                    found[f"n{number}"] = self.gql_pull(item)
                else:
                    found[f"n{number}"] = {"number": number, "comments": self.gql_comments(item)}
            return {"data": {"repository": found}}
        if "userContentEdits" in query:
            item = self.items[variables["number"]]
            edits = self.gql_issue(item)["userContentEdits"]
            return {"data": {"repository": {"issue": {"userContentEdits": edits}}}}
        raise AssertionError(f"no answer for the query {query[:80]}")

    def gql_closed(self, item):
        pull = item["pull"]
        return {
            "number": item["number"],
            "title": item["title"],
            "url": self.rest_issue(item)["html_url"],
            "state": "MERGED" if pull.get("merged_at") else "CLOSED",
            "createdAt": item["created_at"],
            "updatedAt": item["created_at"],
            "closedAt": pull.get("closed_at"),
            "mergedAt": pull.get("merged_at"),
            "mergedBy": _actor(pull.get("merged_by")),
            "headRefName": pull["head"],
            "headRefOid": pull["sha"],
            "headRepository": {"nameWithOwner": REPO},
            "baseRefName": "main",
        }


class RecordedGitHub(GitHub):
    """The real client, with every request answered by `Recorded` and logged."""

    def __init__(self, repo):
        super().__init__(repository=REPO, token="t", api_url=API)
        self.repo = repo
        self.log = []

    def request(self, method, path, body=None, params=None):
        path = path.removeprefix(API)
        self.log.append((method, path, json.dumps(params or {}, sort_keys=True)))
        return self.repo.answer(method, path, body, params or {})

    def reads(self, pattern):
        """The GET requests whose path matches `pattern`, by path."""
        found = {}
        for method, path, _ in self.log:
            if method == "GET" and re.fullmatch(pattern, path):
                found[path] = found.get(path, 0) + 1
        return found


def repository(issues):
    """A repository with `issues` issues of the kinds autopilot meets most, and their pull
    requests, plus a CI issue NexKit opened and a split issue with two parts."""
    repo = Recorded()
    plan = f"{PLAN_MARKER}\n## Plan\n\nAdd it."
    for number in range(1, issues + 1):
        kind = number % 6
        item = repo.issue(number)
        if kind == 1:  # planned, with a question
            repo.comment(
                number,
                render_run(
                    {
                        "url": "u",
                        "command": "plan",
                        "status": "success",
                        "outcome": "planned",
                        "attention": "answer it",
                    },
                    "Done.",
                ),
            )
            repo.comment(number, plan)
        elif kind == 2:  # implemented, with its pull request ready
            repo.comment(
                number,
                render_run(
                    {"url": "u", "command": "go", "status": "success", "outcome": "ready"}, "Done."
                ),
            )
            pull = 1000 + number
            repo.pull(pull, number, mergeable=number % 4 != 0)
            row = {
                "round": 1,
                "trigger": "implement",
                "url": "u",
                "status": "success",
                "outcome": "ready",
                "head": f"{pull:040x}",
            }
            repo.comment(pull, render_run(row, "Ready."))
        elif kind == 3:  # blocked by the two issues before it
            item["blocked_by"] = [n for n in (number - 1, number - 3) if n > 0]
        elif kind == 4:  # opened by someone without write access, vouched for, then edited
            item["user"] = "bob"
            repo.comment(number, "/nexkit trust", user="alice", association="COLLABORATOR")
            item["edits"] = [("bob", "2026-01-03T00:00:00Z")]
            item["renames"] = [("olivia", "2026-01-04T00:00:00Z")]
        elif kind == 5:  # a person's bug issue, in no milestone
            item.update(milestone=None, labels=["bug"])
            repo.comment(number, "It fails.", user="alice", association="COLLABORATOR")
    base = 900
    repo.issue(
        base, user=BOT_LOGIN, labels=["bug", "nexkit:ci"], body="<!-- nexkit:ci ci.yml -->\nRed."
    )
    repo.issue(base + 1)
    repo.comment(base + 1, plan)
    for part in (base + 2, base + 3):
        repo.issue(
            part, user=BOT_LOGIN, body=f"<!-- nexkit:part {base + 1} -->\nA part.", parent=base + 1
        )
    repo.items[base + 3]["blocked_by"] = [base + 2]
    # A pull request NexKit merged, and one it closed because its base branch held it.
    for pull, issue, outcome in ((950, 951, "merged"), (952, 953, "already_on_base")):
        repo.issue(issue, state="closed")
        item = repo.pull(pull, issue)
        merged = outcome == "merged"
        item["state"] = "closed"
        item["pull"].update(
            closed_at="2026-01-05T00:00:00Z",
            merged_at="2026-01-05T00:00:00Z" if merged else None,
            merged_by=BOT_LOGIN if merged else None,
        )
        row = {
            "round": 1,
            "trigger": "implement",
            "url": "u",
            "status": "success",
            "outcome": outcome,
        }
        repo.comment(pull, render_run(row, "Done."))
    return repo


CONFIG = {"autopilot": {"enabled": True, "priority": ["nexkit:ci", "bug"]}, "notify": ["olivia"]}


def status(issues, batch=True, change=None):
    """(client, `collect`'s data) for `repository(issues)`, after `change(repo)`."""
    repo = repository(issues)
    if change:
        change(repo)
    gh = RecordedGitHub(repo)
    data = collect(gh, make_config(**CONFIG), workers=1, batch=batch)
    return gh, data


def closed_blocker(repo):
    """#3 waits for #2, which closed: it is in no batch, and closed after #3's last go."""
    repo.items[2]["state"] = "closed"
    repo.comment(3, render_run({"url": "u", "command": "go", "status": "failure"}, "Failed."))


class SnapshotTests(unittest.TestCase):
    def test_the_output_is_the_same_as_when_every_item_is_read_alone(self):
        _, alone = status(30, batch=False)
        gh, batched = status(30)
        self.assertEqual(batched, alone)
        closed = status(30, change=closed_blocker)[1]
        self.assertEqual(closed, status(30, False, closed_blocker)[1])
        self.assertEqual(render(batched), render(alone))
        # The repository has every kind the comparison is about.
        self.assertTrue(batched["pull_requests"] and batched["needs_person"])
        self.assertEqual([m["number"] for m in batched["recent_merges"]], [950])
        self.assertEqual([c["number"] for c in batched["recent_closed"]], [952])
        states = {item["state"] for item in batched["autopilot"]["issues"]}
        self.assertTrue({"start", "waits"} <= states, states)

    def test_the_requests_do_not_grow_with_the_issues_beyond_a_page_each(self):
        few, _ = status(10)
        many, _ = status(10 + 4 * snapshot.PAGE)
        # One query for each page of issues, and of the pull requests they opened.
        self.assertLessEqual(len(many.log) - len(few.log), 4 + 1, many.log)
        # Read alone, every issue costs requests of its own.
        alone, _ = status(10, batch=False)
        self.assertGreater(len(alone.log), 5 * len(few.log))

    def test_each_issue_pull_request_and_comment_list_is_read_once_at_most(self):
        gh, _ = status(30)
        reads = gh.reads(r"/repos/acme/app/(issues|pulls)/\d+(/.*)?")
        # The open ones come in batches; the rest, such as a closed blocker, once each.
        self.assertEqual(reads, {})
        gh, _ = status(30, change=closed_blocker)
        self.assertEqual({n for n in gh.reads(r".*/\d+(/.*)?").values()} - {1}, set())
        # Read alone, as before, the same comment lists are read several times.
        alone, _ = status(30, batch=False)
        self.assertGreater(max(alone.reads(r".*/comments").values()), 1)

    def test_a_long_thread_is_read_over_rest_once(self):
        repo = repository(6)
        for _ in range(snapshot.COMMENTS + 1):
            repo.comment(5, "And again.", user="alice", association="COLLABORATOR")
        gh = RecordedGitHub(repo)
        batched = collect(gh, make_config(**CONFIG), workers=1)
        self.assertEqual(
            gh.reads(r"/repos/acme/app/issues/\d+/comments"),
            {"/repos/acme/app/issues/5/comments": 2},
        )
        alone = collect(
            RecordedGitHub(repository(6)), make_config(**CONFIG), workers=1, batch=False
        )
        repo2 = repository(6)
        for _ in range(snapshot.COMMENTS + 1):
            repo2.comment(5, "And again.", user="alice", association="COLLABORATOR")
        alone = collect(RecordedGitHub(repo2), make_config(**CONFIG), workers=1, batch=False)
        self.assertEqual(batched, alone)

    def test_without_graphql_fields_it_reads_one_by_one(self):
        gh = RecordedGitHub(repository(6))
        said = []

        read = gh.graphql

        def graphql(query, variables):
            if query == snapshot.LIST_QUERY:
                raise GitHubError(200, "Field 'subIssues' doesn't exist on type 'Issue'")
            return read(query, variables)

        gh.graphql = graphql
        found = collect(gh, make_config(**CONFIG), workers=1, progress=said.append)
        self.assertEqual(found, status(6, batch=False)[1])
        self.assertIn("reading one by one", said[0])

    def test_an_issue_with_more_labels_than_a_query_holds_stays_in_status(self):
        def many_labels(repo):
            repo.items[1]["labels"] = [f"l{n}" for n in range(60)] + ["bug"]

        batched = status(12, change=many_labels)
        self.assertEqual(batched[1], status(12, False, many_labels)[1])
        self.assertEqual(batched[0].reads(r".*/issues/1"), {"/repos/acme/app/issues/1": 1})

    def test_it_says_how_far_it_got(self):
        said = []
        gh = RecordedGitHub(repository(30))
        collect(gh, make_config(**CONFIG), workers=1, progress=said.append)
        self.assertEqual(
            said[:2], ["found 34 open issues and 5 open pull requests", "read 10 of 34 open issues"]
        )
        self.assertIn("read 34 of 34 open issues", said)
        self.assertIn("read 5 of 5 open pull requests", said)
        self.assertIn("read 2 closed pull requests", said)


if __name__ == "__main__":
    unittest.main()
