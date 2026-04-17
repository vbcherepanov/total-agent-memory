# systemd units (Linux)

Linux equivalent of the macOS LaunchAgents in `../launchagents/`.

`install.sh` auto-installs these when running on Linux — it substitutes
`@INSTALL_DIR@` and `@MEMORY_DIR@`, copies the files to
`~/.config/systemd/user/`, and enables them via `systemctl --user`.

## Units

- **`claude-memory-reflection.path`** — watches `~/.claude-memory/.reflect-pending`
  and fires the service whenever `memory_save` touches the trigger file.
- **`claude-memory-reflection.service`** — `Type=oneshot`, runs
  `src/tools/run_reflection.py`, drains `triple_extraction_queue`,
  `deep_enrichment_queue`, and `representations_queue` through Ollama.

## Manual management

```bash
systemctl --user status claude-memory-reflection.path
systemctl --user status claude-memory-reflection.service
journalctl --user -u claude-memory-reflection.service -f

# disable auto-drain entirely
systemctl --user disable --now claude-memory-reflection.path

# run a one-off drain
systemctl --user start claude-memory-reflection.service
```
