# Set up your own runner

[Documentation](README.md) / Runner administration

This guide is for the person who manages the machine running NexKit's
subscription-authenticated agent jobs. For the initial choice between API and
subscription authentication, start with the
[setup roadmap](setup-roadmap.md#1-choose-how-the-pipeline-will-run).
The steps below provision infrastructure and require administrator access.

Choose the guide for your runner:

| Runner machine | Setup |
|---|---|
| Ubuntu x64 | Follow the Linux container instructions below; Ubuntu 24.04 is the CI reference |
| Windows x64 with WSL 2 | Use Ubuntu and Docker Engine; follow [Windows host setup](windows-runner.md) |

Both choices run the same Linux container. Docker Desktop is optional.

Setup selects runner labels and authentication in the consumer's
`.nexkit/project.json`. Installing NexKit does not register a runner, reuse the
author's VPS or grant access to somebody else's account.

GitHub Actions jobs use `GITHUB_TOKEN` for their GitHub operations. Automated
Git operations use `github-actions[bot]` and
`41898282+github-actions[bot]@users.noreply.github.com` as the commit identity.
The VPS or Windows machine is still a GitHub Actions runner; the machine's local
account and the Codex model login are separate from this Git identity.

```json
{
  "engine": {"name": "codex", "auth": "chatgpt"},
  "environment": {
    "runner": "ubuntu-24.04",
    "agent_runner": ["self-hosted", "linux", "x64", "project-runner"],
    "setup": []
  }
}
```

This is a fragment of the [complete project configuration](configuration.md).
Choose models, effort, commands and bounded usage with the project owner.
Clarification, implementation and independent review use this runner; controllers,
verification, merge and release use GitHub-hosted runners. No API key is required
for `chatgpt` mode. Codex uses the signed-in account's available subscription
allowance and may stop when that allowance or the login is unavailable.

## Support boundary

GitHub's self-hosted runner and Codex's ChatGPT login are official components.
OpenAI's [advanced CI authentication guide](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
recommends API credentials for automation and explicitly excludes public/open-source
repositories from its account-cache workflow. The public-repository integration
here is experimental and was selected by this project's owner. These controls
do not turn it into an OpenAI-recommended public CI setup.

The [Dockerfile](../runner/Dockerfile) declares reproducible defaults for the
base images, Actions runner and GitHub CLI. Provisioning selects Codex from the
optional `engine.install.version`; without a pin it installs the current npm
release. CLI capability checks do not compare version labels. Hosts need
Ubuntu x86_64, Docker Engine and systemd; their actual release is recorded instead
of being required to equal 24.04. Windows hosts provide that Linux environment
through WSL 2. Ubuntu 24.04 remains the CI reference environment.
The Codex filesystem permission profile is beta and
must be reverified when the CLI, host kernel or container policies change.
[Acceptance](acceptance.md) distinguishes infrastructure probes from live AI delivery.

## Runtime and workflow maintenance

The supplied reusable jobs use [the controller runtime action](../actions/python-runtime/action.yml)
to install Python 3.12 and verify that the same executable runs through sudo with
an empty environment, with matching version and installation prefixes. If a
shared-library installation needs a loader entry, the action finds the library
in the actual installation prefix, including a relocated runner tool cache,
and installs that entry before consumer setup. Credentials and loader
environment variables are never forwarded to the privileged helper.

Controller contexts, prompts and sealed snapshots live under `/tmp/nexkit`,
outside `/opt/actions-runner`. The Codex parent can use those inputs; its tools
cannot read them. The adapter collapses a private child already covered by a
private parent, and its pre-login probe tests both source access and the accepted
workspace role. This does not depend on a CLI release label.

The image enables the official runner's signal handler. Stop the service through
`scripts/manage_runner.py` or the documented host supervisor; Docker gives the
listener thirty seconds to shut down before killing the container. After a
restart, verify registration and receipt of a new job. A local signal test alone
does not prove that GitHub released the old session.

NexKit maintains its supplied image, actions and reusable workflows. A consumer
owns its accepted kit pin and native workflow composition. If it copies or
modifies reusable jobs, it also owns keeping those copies consistent with runtime,
private-directory and credential boundaries. A plugin update does not update
accepted workflow hashes, the source pin or an existing runner image. Plan and
accept those changes through setup, then reprovision and repeat verification.
Use [troubleshooting](operations.md) to distinguish runtime, sandbox, login and
session failures.

For protected setup, [administrative admission](administration.md#subscription-runner-admission)
temporarily permits one exact bootstrap proposal/commit before expiry. Preview
and apply it only with a stopped runner and a capable immutable image. After
merge, `scripts/manage_runner.py rebind` refreshes workflow admission and removes
that temporary entry while preserving registration and login. An older image
still needs deliberate maintenance and verification.

## Linux runner

The remaining commands in this guide are for the Linux container adapter.
Use the separate Windows guide for WSL 2 prerequisites and Windows commands.

## Prepare a runner binding

Finish the proposed configuration before provisioning. The selected pipeline's `agent_workflows` lists the exact caller paths that may use
the runner. Include each credential-bearing caller, such as `discuss.yml` and
`change.yml` in the manual example. There are no wildcard workflow names.

For the pre-login admission checks below, copy the
[administrative probe example](examples/subscription-runner-probe.yml) into the
proposal as `.github/workflows/nexkit-runner-probe.yml`. Change its runner labels
to match your selected `environment.agent_runner`. Add that path to the
pipeline's `agent_workflows` and include its hash in `files`. In the
[manual walkthrough](manual-setup.md), the copy command is:

```sh
cp "$NEXKIT_SOURCE/docs/examples/subscription-runner-probe.yml" \
  "$NEXKIT_PLAN/bundle/.github/workflows/nexkit-runner-probe.yml"
```

Then refresh the bundle hashes and apply the proposal. All selected callers
must be on the project's default branch before you use them. The probe performs
only a literal marker command, with no checkout, secret or model call.

Provisioning copies the workflow list into the runner's immutable binding.
Provisioning does not add a probe name automatically. Changing the workflow
allowlist after provisioning requires deliberate runner administration; editing
project JSON alone does not change the host binding.

## Provision on an authorized host

The administrator needs Docker access, `sudo`, systemd, seccomp, iptables and
an authenticated GitHub CLI with permission to register this repository's runner.
Inspect existing host networking and choose a maintenance window when needed.
Do not expose Docker's socket, host HOME, host PID/network namespaces or other
projects' credentials to the runner.

From the reviewed kit checkout on that Linux host, select your pipeline
explicitly in both preview and apply.
The manual example uses `maintenance`:

```sh
python3 scripts/provision_runner.py \
  --config /path/to/accepted-project.json --pipeline maintenance
python3 scripts/provision_runner.py \
  --config /path/to/accepted-project.json --pipeline maintenance --check-host
python3 scripts/provision_runner.py \
  --config /path/to/accepted-project.json --pipeline maintenance --apply
```

For an individually composed call with a runner override, also select its
`--invocation NAME`. The provisioning helper takes exactly one project-specific
label in addition to `self-hosted`, `linux` and `x64`.

The preview names the repository, container, labels, host state directory,
image tag and exact Docker build command, including any optional installation
pin. Apply builds that image, then creates
one repo-scoped official runner, an empty private login directory,
an immutable repository binding and a systemd service. Registration uses a
short-lived GitHub token over stdin. The administrator's GitHub credential stays
on the host. Existing containers are never silently replaced.
The binding records the adapter, immutable Docker image ID and
`policy_directory` containing the installed sandbox files. Login uses that
image and checks its required CLI capabilities before mounting the private
login directory. Management uses the installed files without comparing toolkit
or CLI version labels. Reported CLI versions are diagnostic metadata.

To select different Actions Runner or GitHub CLI versions, build a reviewed
image with the Dockerfile's version and corresponding SHA-256 arguments, then
pass `--image YOUR_LOCAL_IMAGE` in preview and apply. This skips the default
build and verifies that image's required capabilities before registration. The actual
image ID is recorded, and the same native isolation checks still apply.

The dedicated `nexkit-runners` bridge blocks access to the host, other containers,
private and link-local networks. Rules affect that bridge only. The service
installs them before starting its listener. IPv6 is disabled in this integration.
When the host kernel supports AppArmor, its named profile explicitly allows
nested user namespaces. It is an unconfined-mode profile, not an additional
comprehensive MAC boundary. Hosts without AppArmor still require seccomp and
passing physical container/sandbox checks. A scoped
seccomp profile extends Docker's defaults for the CLI's native sandbox. The
container receives neither `--privileged` nor `CAP_SYS_ADMIN`.

For a public consumer, require approval for all external-contributor fork
workflows in GitHub Actions settings. Protect the default branch and managed
workflows using the project's accepted rules. Do not add arbitrary workflow
names to the root-owned runner binding.

## Verify before adding a login

The host-installed pre-job hook accepts only the bound repository, default
branch, known NexKit caller workflows and supported events. Labels alone are
not access control. Rejected jobs terminate the pinned Actions Worker before
workflow steps, including `always()` steps. A missing expected Worker ancestry
terminates the scoped Tini container; an unsupported image/init requires repair.

Use the accepted administrative `nexkit-runner-probe.yml` to check an admitted
default-branch run and rejection when the same workflow is dispatched from a
temporary non-default branch. The marker step should run only on the default
branch. In a controlled pre-login probe, also exercise a PR-triggered marker job
and confirm that admission rejects it before any step executes. Remove that
temporary PR trigger before adding account access. Confirm recovery with another
default-branch dispatch.

The example provides the dispatch-only marker workflow. The negative PR probe
requires a deliberate temporary workflow change by the administrator; it is not
part of the ordinary credential-bearing job flow. Keep accepted hashes and
installed files in sync when changing probe workflows. No probe needs to print
or read an actual credential. Remove the administrative workflow through a setup
update after verification if it is no longer needed.

Run the repeatable container checks from the reviewed kit checkout:

```sh
python3 scripts/verify_docker_host.py
```

It uses fake credentials, actual Codex tools and simulated model responses,
with zero real model calls. It builds a test image, creates isolated temporary
containers and checks public network reachability as a positive control. It
also checks denied access to a harmless host listener, command isolation and
role permissions. It removes its scoped containers and network rules afterward.
These results establish the tested boundaries, not complete live SDLC acceptance.

## Complete the official login

Use the container/service name and host state path from the provisioning preview.
Stop that consumer's runner while logging in so a job cannot clean up a login
process. Run the official CLI in a temporary container with only that consumer's
login directory mounted:

```sh
python3 scripts/manage_runner.py stop \
  --config /path/to/accepted-project.json --pipeline maintenance
python3 scripts/manage_runner.py login \
  --config /path/to/accepted-project.json --pipeline maintenance
python3 scripts/manage_runner.py start \
  --config /path/to/accepted-project.json --pipeline maintenance
```

Use the same pipeline and any invocation override used to provision. The account owner
completes the official browser/device flow. The browser may be on the owner's
personal computer; the login command runs in the container context shown above.
Independently seed each consumer's
session; do not clone a login cache into multiple concurrent refreshers. NexKit
does not parse tokens, implement OAuth, put account files into GitHub Secrets or
upload them as artifacts. Codex refreshes its own persistent file.

Inspect `codex login status` as UID 1101 with the same `CODEX_HOME`. A status check
does not prove model access: the live clarification/delivery run must still use
the configured model, installed skill and tools. Expired/revoked sessions require
another official login; there is no promise of unattended access forever.

## Execution and maintenance

Consumer setup runs under a different Unix account from the credential-owning
Codex parent. Workspace identity is checked after setup, trusted Git metadata and
role controls are restored, and installed dependencies remain in the same HOME.
Native CLI metadata Git commands are disabled outside the sandbox; terminal tools
receive real Git inside it. Tool permissions deny the entire login directory,
runner state and private logs, and disable external network access. External
plugins, hooks, MCP, browser and remote-control features are disabled. Dependencies
requiring network access belong in the declared setup commands. Local TCP sockets
are also unavailable to sandboxed tools. HTTP/network E2E runs in the separate
verification job; its actual results are passed to review and repair. Offline
checks remain available to the CLI. Never replace blocked E2E with a passing stub.

Each role gets a fresh session, prompt, schema, timeout and workspace. Review has
read-only source access. The runner kills both job accounts' processes and clears
their HOME directories at job boundaries; the next job clears the prior checkout.
Runner registration/diagnostics, the private CLI log and Codex's login/runtime
directory persist. Cleanup does not erase every path in `/tmp` or `/var/tmp`.
Model output and changes still cross
the same validation and exact-candidate checks used by API mode.

The latest CLI JSON log is root-only at `/var/log/nexkit/last-cli.log` inside the
container. It is not uploaded to Actions. An administrator may inspect it locally
to diagnose a failed session; publish only bounded, reviewed summaries. Actions
artifacts contain validated result/bundle data and have seven-day retention.

Use `systemctl status CONTAINER.service`, repository runner settings and Actions
runs to inspect operation. Stop the service before an update or login repair.
Rebuild the pinned image, remove the old repository registration and container,
then reprovision deliberately while preserving only the consumer's login directory
when appropriate. Reverify admission, sandbox, networking and real model access.
Removing plugin files does not stop or unregister infrastructure: explicitly remove
the service and repository runner when retiring the consumer, then decide whether
to revoke/delete its login. Do not remove a shared bridge or policy while another
authorized consumer still uses it.
