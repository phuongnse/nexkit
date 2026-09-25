# A runner and Codex login owned by each consumer

Setup selects runner labels and authentication in the consumer's
`.nexkit/project.json`. Installing NexKit does not register a runner, reuse the
author's VPS or grant access to somebody else's account.

```json
{
  "engine": {"name": "codex", "version": "0.156.1", "auth": "chatgpt"},
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

The current integration pins Ubuntu 24.04 x86_64, Actions runner 2.337.0,
Codex 0.156.1, Node 24.18.0 and GitHub CLI 2.101.0. It uses Docker and systemd on
an authorized Linux host. The native filesystem permission profile is beta and
must be reverified when the CLI, host kernel or container policies change.
[Acceptance](acceptance.md) distinguishes infrastructure probes from live AI delivery.

## Provision on an authorized host

The administrator needs Docker access, `sudo`, systemd, AppArmor, iptables and
an authenticated GitHub CLI with permission to register this repository's runner.
Inspect existing host networking and choose a maintenance window when needed.
Do not expose Docker's socket, host HOME, host PID/network namespaces or other
projects' credentials to the runner.

From the reviewed kit checkout:

```sh
docker build -t nexkit-runner:0.1.0-rc.1 runner
python3 scripts/provision_runner.py --config /path/to/accepted-project.json
python3 scripts/provision_runner.py --config /path/to/accepted-project.json --apply
```

The preview names the repository, container, labels and host state directory.
Apply creates one repo-scoped official runner, an empty private login directory,
an immutable repository binding and a systemd service. Registration uses a
short-lived GitHub token over stdin. The administrator's GitHub credential stays
on the host. Existing containers are never silently replaced.

The dedicated `nexkit-runners` bridge blocks access to the host, other containers,
private and link-local networks. Rules affect that bridge only. The service
installs them before starting its listener. IPv6 is disabled in this integration.
The named AppArmor profile explicitly allows nested user namespaces; it is an
unconfined-mode profile, not an additional comprehensive MAC boundary. A scoped
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

Use an administrative `nexkit-runner-probe.yml` workflow with a harmless marker
step to verify an admitted default-branch run, rejection of a non-default branch
and a PR, then recovery on the next default-branch run. This filename is included
in the immutable binding for that purpose. Do this before adding account access.
No test needs to print or read an actual credential. Remove the probe workflow
after setup if it is no longer needed.

Run the native sandbox canary from the reviewed kit checkout:

```sh
docker run --rm --network nexkit-runners --cap-drop NET_RAW \
  --security-opt apparmor=nexkit-runner \
  --security-opt seccomp=/opt/nexkit-runner/seccomp.json \
  --mount "type=bind,src=$PWD,dst=/kit,readonly" --env PYTHONPATH=/kit \
  nexkit-runner:0.1.0-rc.1 python3 -m nexkit.subscription --probe
```

It uses a newly created fake credential and makes zero model calls. Verify public
network reachability outside the tool sandbox as a positive control. Separately
check that a harmless listener on the host cannot be reached from the container.
Successful probes establish those tested boundaries, not complete live SDLC acceptance.

## Complete the official login

Use the container/service name and host state path from the provisioning preview.
Stop that consumer's runner while logging in so a job cannot clean up a login
process. Run the official CLI in a temporary container with only that consumer's
login directory mounted:

```sh
sudo systemctl stop CONTAINER.service
docker run --rm -it --network nexkit-runners --user 1101:1101 \
  --env CODEX_HOME=/var/lib/nexkit/codex \
  --mount type=bind,src=HOST_STATE/codex,dst=/var/lib/nexkit/codex \
  nexkit-runner:0.1.0-rc.1 codex login --device-auth
sudo systemctl start CONTAINER.service
```

Replace `CONTAINER` and `HOST_STATE` with the printed values. The account owner
completes the official browser/device flow. Independently seed each consumer's
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
