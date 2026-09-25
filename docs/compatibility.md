# Host compatibility — verified September 25, 2026

| Product/variant | Version checked | Installation and loading | Interactive model use | Actions engine |
|---|---|---|---|---|
| OpenAI Codex CLI | 0.156.1 | Native marketplace add, plugin add and list passed; rc.1 enabled | Live local implementer/reviewer read skills, used tools, fixed code and verified tests/CLI | Official adapter implemented; live AI Actions run pending |
| Anthropic Claude Code CLI | 2.1.282 | Native marketplace install/details passed; all 8 skills discovered | No authenticated model task verified | Not provided in this candidate |
| GitHub Copilot CLI | Not installed | `.github/skills` checked against official documentation | Unverified | Not provided |
| Google Gemini CLI | Registry version 0.61.0 inspected; not installed | `.gemini/skills` checked against official documentation | Unverified | Not provided |
| Cursor desktop Agent | No installed version verified | `.cursor/skills` checked against official documentation | Unverified | Not provided |
| Google Antigravity IDE | No installed version verified | `.agents/skills` checked against official documentation | Unverified | Not provided |

Generating host files does not establish host acceptance. An interactive host
may submit work to the configured Codex engine without itself serving as a CI engine.

Load the Claude Code plugin from source or an extracted archive:

```sh
claude --plugin-dir /absolute/path/to/nexkit/plugins/nexkit
```

The installer places portable skills in the appropriate project directory with
`nexkit setup ... --host <host> --apply`. Codex uses `$nexkit-init`; the native
Claude plugin uses `/nexkit:nexkit-init`. Other hosts use their official skill
discovery mechanism. NexKit does not invent host slash-command APIs.

Native installation was exercised in isolated temporary host configuration
directories, including installation from the extracted candidate archive.
Personal host settings were not changed. Installation/loading alone does not
prove interactive model use or headless CI execution.

[Live local Codex evidence](validation/local-codex-2026-09-25.json) used the
existing local `gpt-6-astra` configuration. This does not establish Actions API
authentication or interactive support on the other hosts.

Official sources: [Codex skills](https://developers.openai.com/codex/skills),
[OpenAI plugins](https://developers.openai.com/plugins/build/plugins),
[Claude Code plugins](https://code.claude.com/docs/en/plugins),
[Copilot CLI skills](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference),
[Gemini skills](https://geminicli.com/docs/cli/skills/),
[Cursor skills](https://prod.cursor.com/docs/skills),
[Antigravity skills](https://antigravity.google/docs/skills).
