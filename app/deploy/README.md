# Deploy — supervisor + backups

Three independent, all-optional pieces. Neither is installed or enabled automatically — copy
what you want in.

## Keeping the web UI running

`agent.run_web_ui` (`agent/web/server.py` under uvicorn) has no built-in supervisor — until now,
a crash or a reboot meant starting it back up by hand. `localai-web.service` is a systemd **user**
unit that does that for you: restarts it on crash, and can start it automatically on login/boot.

### Install

```sh
mkdir -p ~/.config/systemd/user
cp deploy/localai-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now localai-web.service
```

Check it's up:

```sh
systemctl --user status localai-web.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
```

### Optional: survive logout / start on boot without logging in

A user unit normally only runs while you have an active login session. To have it start at boot
and keep running after you log out:

```sh
loginctl enable-linger "$USER"
```

### Logs

```sh
journalctl --user -u localai-web.service -f
```

### Uninstall

```sh
systemctl --user disable --now localai-web.service
rm ~/.config/systemd/user/localai-web.service
systemctl --user daemon-reload
```

### Updating after a code change

The unit runs `agent.run_web_ui` from this checkout's `.venv` — a code change needs a restart to
take effect, same as running it by hand did:

```sh
systemctl --user restart localai-web.service
```

## Keeping the knowledge-RAG embedding server running

`agent/knowledge_rag/`'s `security_reference_search` tool needs a second, CPU-only llama-server
instance serving the embedding model (127.0.0.1:8091) — separate from the main GPU-loaded
decision model so the two never contend for VRAM (see the unit file's own comment on why
`CUDA_VISIBLE_DEVICES=""` is required, not optional, on a GPU with no headroom to spare).

### Install

```sh
mkdir -p ~/.config/systemd/user
cp deploy/localai-knowledge-rag.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now localai-knowledge-rag.service
```

Check it's up:

```sh
systemctl --user status localai-knowledge-rag.service
curl -s http://127.0.0.1:8091/health
```

If the index hasn't been built yet, do that once the server is up:

```sh
python3 -m agent.knowledge_rag.build_index
```

### Logs

```sh
journalctl --user -u localai-knowledge-rag.service -f
```

### Uninstall

```sh
systemctl --user disable --now localai-knowledge-rag.service
rm ~/.config/systemd/user/localai-knowledge-rag.service
systemctl --user daemon-reload
```

## Backing up local state

Everything this project keeps — sessions, findings, evidence, the technique KB, audit logs, and
every engagement's RoE/scope/SQLite stores (`agent/state/`, `engagements/`, the legacy
`engagement/`) — lives only on this machine, in plain files. `backup-localai.sh` tars them into a
timestamped archive and prunes old ones past a retention count; `localai-backup.timer` runs it
daily via systemd. This is about surviving a disk failure or a bad `rm`, not remote redundancy —
point `BACKUP_DIR` (the script's first argument) at a mounted remote/removable volume, or sync
the archives elsewhere yourself, if you want off-machine copies too.

### Run it once, by hand

```sh
./deploy/backup-localai.sh                      # -> ~/localai-backups, keeps newest 14
./deploy/backup-localai.sh /mnt/backups 30       # custom dir + retention count
```

### Install the daily timer

```sh
mkdir -p ~/.config/systemd/user
cp deploy/localai-backup.service deploy/localai-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now localai-backup.timer
```

Check it's scheduled, and see what it's done so far:

```sh
systemctl --user list-timers localai-backup.timer
journalctl --user -u localai-backup.service
```

### Restoring

The archive extracts back to exactly the layout it was taken from — with the web UI (and
`agent.run_memory_service`, if running) stopped first, extract over the project root:

```sh
systemctl --user stop localai-web.service   # if the supervisor unit is installed
tar -xzf ~/localai-backups/localai-state-<timestamp>.tar.gz -C /home/nicotine/localAI
systemctl --user start localai-web.service
```

### Uninstall

```sh
systemctl --user disable --now localai-backup.timer
rm ~/.config/systemd/user/localai-backup.timer ~/.config/systemd/user/localai-backup.service
systemctl --user daemon-reload
```
