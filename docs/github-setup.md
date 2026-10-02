# Configure GitHub for NexKit

[Documentation](README.md) / GitHub setup

This page is for the repository administrator. Guided and manual setup both
need these settings. The NexKit installer writes local project files; it does
not change GitHub settings.

## Enable the selected workflows

In the repository's **Settings → Actions → General**, allow the pinned NexKit
reusable workflows and the actions they use. Check organization restrictions too.
Under workflow permissions, enable **Allow GitHub Actions to create and approve
pull requests**. NexKit needs the setting to create its delivery PR; it does
not supply a person's approval for an optional PR gate.

The example workflows declare permissions per job. The default token permission
can remain read-only. An administrator can also set the required PR permission
through the [GitHub permissions API](https://docs.github.com/en/rest/actions/permissions#set-default-workflow-permissions-for-a-repository):

```sh
gh api --method PUT repos/OWNER/REPO/actions/permissions/workflow \
  -F can_approve_pull_request_reviews=true
```

Use your actual repository name in this page's commands. Commit the accepted
workflows to the default branch. Check that the repository enables Issues and
the merge method selected by `merge_method`.

For public repositories using the subscription runner, require approval for
all outside-contributor fork workflows in Actions settings. Keep ordinary PR
test jobs on runners without the subscription login. See GitHub's
[fork approval documentation](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks)
and NexKit's [runner guide](self-hosted.md).

## Match the branch policy to the workflow

The current NexKit delivery controller requires:

- An active default-branch ruleset requiring exactly `NexKit verification` and
  `NexKit review`, with both checks attributed to the GitHub Actions App.
- Strict checks: the candidate must be up to date with the default branch.
- No blanket bypass actors.
- A native PR review count matching the configured PR approval policy: zero
  for the manual example, or the configured count with stale-review dismissal.
- No additional unsupported merge conditions. The
  [configuration reference](configuration.md#github-settings) lists the boundary.

Inspect existing repository, organization and classic branch rules first.
Preserve their intended verification in the accepted project checks or an
explicitly integrated workflow. Additional required check names and unsupported
review rules cause setup to report a problem. Do not remove existing protections
just to obtain a ready result; resolve the integration with the project owner.

Scope default-branch protection to that branch. The jobs also need to write
`nexkit/state` and their delivery branches. A blanket rule blocking those writes
prevents the pipeline from recording progress or publishing its candidate.

## Apply the example rule to a repository without existing rules

The following steps create a rule; they are intended for a new branch policy.
For an existing policy, prepare an update to the applicable rules instead of
creating an overlapping rule.

The [manual example](examples/manual-setup/branch-rules.json) proposes a PR
requirement with zero additional approving reviews, the two NexKit checks,
strict updates, and protection against deletion and force pushes. It has no
bypass actors. It pairs with that example's requirement approval and separate
AI review. Add the composed approval flow before choosing a nonzero PR count.

After following the manual guide, select its proposed rules file:

```sh
export NEXKIT_RULES="$NEXKIT_PLAN/branch-rules.json"
```

If you arrived here separately, copy the linked JSON file to a local proposal
file and set `NEXKIT_RULES` to its absolute path.

Fill the check source IDs using GitHub's actual Actions App:

```sh
python3 - <<'PY'
import json
import os
from pathlib import Path
import subprocess

path = Path(os.environ["NEXKIT_RULES"])
rules = json.loads(path.read_text())
app = json.loads(subprocess.check_output(
    ["gh", "api", "apps/github-actions"], text=True
))
for rule in rules["rules"]:
    if rule["type"] == "required_status_checks":
        for check in rule["parameters"]["required_status_checks"]:
            check["integration_id"] = app["id"]
path.write_text(json.dumps(rules, indent=2) + "\n")
PY
```

Read the resulting file. Once the initial setup files are on the default branch
and the policy matches your decisions, the administrator can apply it with the
[ruleset API](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset):

```sh
gh api --method POST repos/OWNER/REPO/rulesets --input "$NEXKIT_RULES"
```

Record the returned rule ID for later administration. This creates a new rule
each time; inspect the existing rule rather than repeating the POST. The API
can name the NexKit checks before their first real run, so setup does not need
fabricated successful check runs. Repository visibility, account plan and
organization policy must allow the required active ruleset.

## Inspect the result

Read the settings in GitHub or use these commands:

```sh
gh api repos/OWNER/REPO/actions/permissions/workflow
gh api 'repos/OWNER/REPO/rulesets?includes_parents=true'
```

After model access is configured and your local checkout matches the installed
default branch, run from that checkout:

```sh
nexkit doctor --online --checks
```

It audits the actual branch rules, including inherited rules and bypasses, with
the administrator's GitHub access. The pipeline does not retain that account's
administrative credential. Rule settings are only part of readiness; model
access and live execution are checked separately.

Return to [manual setup, step 7](manual-setup.md#7-configure-the-selected-model-access)
or [guided setup](getting-started.md#7-check-readiness-and-start-a-request).
