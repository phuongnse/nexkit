# Host compatibility — verified September 26, 2026

| Product/variant | Version checked | Installation and loading | Interactive model use | Actions engine |
|---|---|---|---|---|
| OpenAI Codex CLI | 0.156.1 | Native install/reinstall/update/remove passed; fresh host discovery loaded all 9 plugin skills | Live local implementer/reviewer read skills, used tools, fixed code and verified tests/CLI | Live clarification on both consumers; implementation/review/merge still pending |
| Anthropic Claude Code CLI | 2.1.282 | Native manifest validation and `--plugin-dir` loading passed; details discovered all 9 skills | No authenticated model task verified | Not provided in this candidate |
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
`nexkit setup ... --host <host> --apply`. In Codex, type `$` or open `/skills` and
select `nexkit:nexkit-init` for the native plugin. Directly installed project
skills use `$nexkit-init`. The native Claude plugin uses `/nexkit:nexkit-init`.
Other hosts use their official skill discovery mechanism.

The [September 26 host check](validation/host-install-2026-09-26.json) used the
extracted candidate in a fresh container without host credentials. Network was
disconnected after installing the pinned host binaries. Codex's native
`skills/list` returned nine enabled plugin skills whose bytes matched the archive.
A development cachebuster update loaded in a fresh process; uninstall removed
the plugin from discovery and preserved consumer knowledge and the package.
Claude's documented `--plugin-dir` loading discovered the same nine skills.
The earlier September 25 check also exercised Claude marketplace installation.

Personal host settings and the two live runner logins were not changed. The VPS
currently has Codex 0.157.0 on PATH; this check deliberately used pinned 0.156.1.
Installation/loading alone does not prove interactive model use or delivery on
Actions. Start a new host session after updating a plugin before using its skills.

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
