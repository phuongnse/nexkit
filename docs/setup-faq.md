# Common setup questions

[Documentation](README.md) / Setup questions

For the full order of steps, start with the [setup roadmap](setup-roadmap.md).

## Do I have to rent a VPS first?

No. Choose model access first. GitHub-hosted jobs with API authentication need
no VPS. Subscription authentication needs your own supported Linux or Windows runner;
you can use a compatible machine you already operate. Prepare the project
configuration before provisioning it so its repository, labels and allowed
workflows are known. See [runner choices](setup-roadmap.md#1-choose-how-the-pipeline-will-run).

## Can I set everything up without chatting with an agent?

Yes. The [manual guide](manual-setup.md) provides a complete editable example
and the CLI steps. You need to understand your project's commands and edit JSON
and GitHub Actions YAML. There is no terminal setup wizard in this version.

The plugin supplies skills for Codex. The CLI installs
project files and checks the setup. The pipeline later runs Codex through
GitHub Actions, whether the files were prepared by you or an assistant.

## Is installing the plugin enough?

No. It makes NexKit's skills available to your assistant. Each application still
needs its own project configuration, workflows, GitHub settings and model access.
Issue triggers become available after the workflow files reach the default branch.

## Does the plugin create a VPS or use the author's runner?

No. It grants no access to the author's machines or accounts. The runner helper
registers and configures a runner on a supported machine you already provide. It does
not rent infrastructure. Runner labels and workflow admission belong to the
project being installed.

## Can several projects use the same physical VPS?

The integration can provision separate repository-bound runner containers on
one supported Ubuntu host, including Ubuntu under WSL 2 on Windows. Each has its
own labels, allowed workflows and login state.
Provision and sign in to each deliberately; do not copy a login cache between
concurrent runners. The host must have capacity for the jobs you allow.
See [runner administration](self-hosted.md).

Separate containers use the same Linux runtime on both host choices. See
[Windows host setup](windows-runner.md) when using WSL 2.

## Does a Windows runner need someone to stay signed in?

The supplied Windows command keeps WSL alive while it supervises the consumer's
Linux service. Closing the command stops the runner. It can be launched at
Windows sign-in; starting without any sign-in after reboot needs a separately
verified deployment. See [runner lifetime](windows-runner.md#6-keep-the-runner-online).

## Is Docker Desktop required on Windows?

No. Install Docker Engine and its CLI inside Ubuntu on WSL 2; the guide uses
Ubuntu 24.04 as the CI reference. Docker
Desktop is an optional product; NexKit uses the Linux Engine directly. A native
Windows container engine cannot run this Linux image.

## Does the container replace the Codex sandbox?

Both are used. The container separates the runner from its host. Codex's tool
sandbox separates generated commands from the model login held by the Codex
parent inside that container. Public network access needed by the parent and
dependency setup is denied to model tools.

## Do I need two GitHub repositories?

One application repository is enough. NexKit's two acceptance projects cover a
new application and an existing application. They are verification fixtures,
not installation requirements. Your local NexKit toolkit checkout is separate
from the application, but you do not need to create your own GitHub fork of it.

## What if my project has no code or tests yet?

Start with a repository containing a README and a default branch. Record
`application: absent`, the intended behavior and the checks the first change
will need to create. Missing or empty tests do not count as passing checks.
The first approved request can build the initial application and its checks;
until then, report the application verification as incomplete.

## Does my local Codex subscription automatically work in Actions?

No. Guided setup's interactive login and the pipeline's login are separate.
For subscription mode, complete Codex login in the project's dedicated runner
environment. The device-login page can be opened on another computer.
The account's model access and remaining allowance still apply.

API mode uses the project's `OPENAI_API_KEY` secret instead. API usage is billed
separately from a ChatGPT subscription. See
[OpenAI authentication](https://learn.chatgpt.com/docs/auth).

## Is subscription mode on a public repository the standard OpenAI CI setup?

NexKit's public subscription integration is experimental. OpenAI's documented
[account-authentication flow for CI](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
is restricted to trusted private automation. Read NexKit's
[support boundary](self-hosted.md#support-boundary) and acceptance evidence before
choosing that mode.

## Does a VPS move every job off GitHub-hosted runners?

No. In the current subscription integration, model jobs use the dedicated
runner. Controllers, application verification, merge and release jobs still
use GitHub-hosted runners. Account usage and GitHub Actions usage are separate;
check the applicable allowances for both.

## Must I keep the setup chat or my laptop open?

No. Once setup is on the default branch, GitHub receives the events and runs
the workflows. A dedicated runner must remain online for its jobs. If your
laptop is also the runner host, turning it off makes that runner unavailable.
Workflow time limits still apply.

## Can teammates work entirely through GitHub?

Yes. After setup, collaborators can start an issue, discuss its specification,
approve the exact version and review PRs in GitHub. They do not need a local
plugin installation or a personal VPS for those actions. Start commands and
requirement approvals require current write, maintain or admin access.

When optional PR approval is configured, `reviewers: "repository"` allows
eligible collaborators rather than requiring a fixed login. Native GitHub
review rules still apply. See [daily use](daily-use.md) and
[approval configuration](stage-approvals.md).

## Can I choose different pipelines, models and approval steps later?

Yes. Use `nexkit-init` again, or prepare a separate proposed configuration and
workflow bundle manually. Preview and apply the update through the installer.
Models, reasoning, budgets, workflow structure and optional approval gates are
project choices. A new JSON approval entry also needs its corresponding workflow
jobs and continuation events. See [project setup](project-setup.md).

## Why are there hashes in the configuration?

They identify the exact workflow and instruction files the project accepted.
The installer and running jobs compare the files against those values. When
you deliberately change a managed workflow, update its hash in the proposed
configuration and reinstall it. The manual guide shows how to do this.

## Does a green doctor result mean the model has been tested?

No. `doctor --online --checks` inspects configuration, GitHub settings, secret
metadata or runner labels, and runs the declared application checks. It does
not make a model call. Verify a small real request separately and report how
far it got: clarification, delivery, configured approvals and merge.

## What should I read when setup stops?

Start with the reported problem and [troubleshooting](operations.md). A job
waiting for a runner, a model login failure, an unapproved requirement and a
failed application check need different fixes. Retrying does not reset the
project's recorded budgets or create an approval.
