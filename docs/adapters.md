# Provider adapters

Root AGENTS.md is canonical; executable discovery reads `.claude/skills/*/skill.yaml`. CLAUDE.md retains its AGENTS.md import. The generator never writes either file.

```sh
python3 tools/adapters/render_adapter.py list-targets
python3 tools/adapters/render_adapter.py preview --target codex
python3 tools/adapters/render_adapter.py apply --target codex
python3 tools/adapters/render_adapter.py uninstall --target codex
```

Targets and destinations are in manifest.yaml. Codex receives a discoverable `.agents/skills/bounty-harness/SKILL.md` and conservative read-only config. OpenCode receives research/reviewer agents using v2 permissions. Claude receives a command and local permission settings. Generic/Cursor targets receive reference guides. All route to the same [JSON interface](agent-interface.md).

Preview and apply share the same merge calculation. Ownership is recorded in ignored state.json. Unmanaged existing files and changed generated content cause conflicts, including uninstall; --force never bypasses conflicts. Preserve or restore the recorded generated content before regenerating, or manually reconcile and remove the ownership record. Unrelated Markdown outside the owned content survives regeneration and uninstall. Config files require whole-file ownership. Writes are atomic and backups use unique names. A crash between file and state replacement fails closed on the next attempt. Concurrent generator invocations are unsupported. Legacy state.yaml requires explicit reconciliation.

Native checks on 2026-11-05: Codex CLI 0.165.0 accepted generated configuration with `--strict-config app-server --print-startup-config`; OpenCode v2.2.0 discovered both generated agents with `debug agents --standalone` in a temporary fixture. Claude executable was unavailable. No paid model calls or target traffic were made. Native skill invocation, effective runtime permissions, and end-to-end native session operation remain unverified. Codex read-only defaults permit discovery but require a separately operated writable transport for session/task persistence. Template generation alone is not full native compatibility proof.

Configuration references consulted: [Codex skills](https://developers.openai.com/codex/skills/), [Codex security](https://developers.openai.com/codex/security/), [OpenCode v2 agents](https://dev.opencode.ai/v2/docs/agents/), [OpenCode v2 permissions](https://dev.opencode.ai/v2/docs/permissions/), [Claude skills](https://code.claude.com/docs/en/skills), [Claude permissions](https://code.claude.com/docs/en/permissions).
