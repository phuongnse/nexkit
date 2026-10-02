# NexKit documentation

New to NexKit? Read the [setup roadmap](setup-roadmap.md) first. It answers
whether you need a VPS, what to prepare, and how to choose between guided and
manual setup. The guides below link to technical details when you need them.

## Start here

| Guide | What you will learn |
|---|---|
| [Setup roadmap](setup-roadmap.md) | See the whole process, prerequisites, responsibilities and completion checklist |
| [Set up with Codex](getting-started.md) | Install the plugin, open your project and review the setup proposal |
| [Set up manually](manual-setup.md) | Copy and edit complete example files, then install them without an interactive agent |
| [Setup questions](setup-faq.md) | Understand VPS needs, account access, multiple projects and daily use without a plugin |
| [Configure your project](project-setup.md) | Choose pipelines, models, limits, checks and approval steps; change them later |
| [Daily use](daily-use.md) | Create a request, discuss it, approve it, follow progress and prepare a release |
| [How it works](architecture.md) | See what runs locally, on GitHub and on the agent runner |
| [Troubleshooting](operations.md) | Understand a waiting or failed run and recover it |

All diagrams are written directly in fenced `mermaid` blocks in these Markdown
files. GitHub renders them when you open a page. Edit the block to update the
diagram; there is no separate image or diagram source to keep in sync.

## Setup references

| Reference | Use it when you need to… |
|---|---|
| [GitHub setup](github-setup.md) | Configure Actions permissions and branch rules, with an editable rule proposal |
| [Configuration fields](configuration.md) | Look up a field, allowed value or numeric limit |
| [Build your own workflows](workflow-composition.md) | Connect your chosen GitHub triggers and jobs to NexKit |
| [Build a pipeline from steps](project-steps.md) | Compose standalone tasks and project-specific commands or native jobs |
| [Agent calls](agent-invocations.md) | Set the model, task, skills and runner for an individual agent call |
| [Optional approvals](stage-approvals.md) | Add issue or PR approval before selected work continues |
| [Existing issue intake](issue-intake.md) | Connect `/nexkit start` and understand its retry behavior |
| [Issue completion](issue-completion.md) | Choose which issues close after merge or release, including a PR that completes several issues |
| [Your own runner](self-hosted.md) | Provision, sign in to and maintain the subscription runner |
| [Windows host](windows-runner.md) | Prepare Ubuntu on WSL 2, Docker Engine and the runner supervisor |
| [Supported platform](support.md) | Check implemented integrations, host requirements and support boundaries |

[Workflow examples](examples/) show how to connect reusable jobs. Setup adapts
them to the project and replaces the placeholder pins and identifiers.

## Verification and development

- [Verification guide](acceptance.md): required checks, result interpretation and where to keep evidence.
- [How to verify NexKit](live-acceptance.md): acceptance criteria and repeatable checks.
- [Local development checks](operations.md#developing-nexkit): checks for changes to NexKit itself.
- [Components and sources](sources.md): dependencies, pins and upstream references.
- [Agent adapters](agent-adapters.md): provider contracts, capability checks and adding an integration.

## A few terms

| Term | Plain meaning |
|---|---|
| Project / consumer | A repository that uses NexKit |
| Codex session | The local conversation where you use the NexKit plugin's skills |
| Skill | Instructions that help an agent carry out a particular NexKit task |
| Pipeline | A named workflow for a kind of work in your project |
| Stage / job | One part of that workflow, such as planning or testing |
| Runner | The machine that executes a GitHub Actions job |
| Specification | The agreed description of the change and how to check it |
| Candidate | The exact code revision being checked, reviewed or released |
| Approval gate | A point where the workflow waits for an authorized person's decision |
| Kit pin | The exact NexKit commit your project's workflows use |

[Back to the project overview](../README.md)

## Versions

[Version 1.0.0 and schema 1](versions.md) explains release numbers, project pins
and the first stable format.
