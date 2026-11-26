# Documentation

This is the map of NexKit's docs. Each topic has one home, and other docs link to it rather than
repeat it. Start from the reading path below by what you want, then find the page in the tables and
read only the sections you need. What goes in which doc, and how the docs stay true, is in
[writing the docs](writing-docs.md).

## Where to start

| You want to | Start with | Then |
| --- | --- | --- |
| Understand NexKit | The [README](../README.md) | [How it works](how-it-works.md) and the [roadmap](roadmap.md) |
| Set NexKit up on a repository | [Set up a repository](../README.md#set-up-a-repository) in the README, or the `setup` skill | The [configuration reference](configuration.md), [The GitHub App](configuration.md#the-github-app) first |
| Find why a run stopped or waits | [Troubleshooting](troubleshooting.md) | [When a run fails](failure-handling.md#failure-handling) |
| Work on NexKit | [AGENTS.md](../AGENTS.md) | [Architecture](architecture.md), [security](security.md), [Delivery](delivery.md), [testing](testing.md), [writing the docs](writing-docs.md), [the engine](engine.md) and the [roadmap](roadmap.md) |

## Using NexKit

Read by people who set NexKit up on a repository and work with it there.

| Doc | What it holds | Read it when |
| --- | --- | --- |
| [How it works](how-it-works.md) | Who does what, the jobs of a run, what you see during a run, logs and artifacts, state and what you decide about security | You want to know what NexKit does on your repository, and why |
| [Plan, implement, fix and review](stages.md) | The stages of a run: the plan, the implementation and fix rounds, the review, and the merge and close | You want to know what a stage does, what it reads and what it publishes |
| [Work NexKit starts by itself](work-without-a-command.md) | The events that start a run without a command, autopilot and usage limits | You want to know why NexKit started a run, or what autopilot does next |
| [Agents and profiles](agents-and-profiles.md) | The agents that run each stage and the profiles that choose them | You choose agents, or want to know which agent a stage uses |
| [When a run fails](failure-handling.md) | What NexKit does for each failure and which failures it retries | You want to know whether NexKit retries a failure, and when |
| [Configuration reference](configuration.md) | Every setting of `.nexkit/config.json`, its default and what it does; the GitHub App; upgrading | You change a setting, or upgrade NexKit |
| [Troubleshooting](troubleshooting.md) | What each stop and wait means, and what to do | A run failed, stopped or waits for a person |
| [Security](security.md) | What NexKit trusts, and for each input it does not, what an attacker could reach, the rule that contains it and the tests that check it | You decide how far to trust an input, or change a job, a token or a secret |

The plugin's skills, under `skills/`, carry out the same tasks step by step: `setup` sets NexKit up,
`request` writes issues for it, and `status` says what NexKit is doing.

## Working on NexKit

Read by people, and agents, who change NexKit itself.

| Doc | What it holds | Read it when |
| --- | --- | --- |
| [Architecture](architecture.md) | How NexKit is built: the package and its modules, the workflows and their jobs, tokens and secrets, state, the engine and its hosts, and the plugin | You change a module, a job, or what a job holds |
| [The engine](engine.md) | The engine, its protocol, states, events and actions, hosts, and the way there from NexKit 6 | You change a decision of the loop, the protocol or a host |
| [Roadmap](roadmap.md) | NexKit's features, one milestone each, done and under way, and what labels are for | You need a feature's milestone or the work under way |
| [Delivery](delivery.md) | Which issue a session takes, pull requests, labels and milestones, NexKit on this repository, and releases | You take an issue, open a pull request or prepare a release |
| [Testing](testing.md) | Which check covers what, the smoke test, and what a change that moves a decision into the engine proves | You add a test, or change what a check covers |
| [Decisions](decisions.md) | The choices behind NexKit's rules, each with its reasons, one file per decision, listed in the index | You change a rule, or make a choice that is costly to undo |
| [Writing the docs](writing-docs.md) | What goes in which doc, decisions, style, structure, and how the docs stay correct | You write or change any doc |
