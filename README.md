# zfs-sync

Pull ZFS snapshots from a TrueNAS appliance into a Linux OpenZFS host.
The tool runs on the **Linux** side; TrueNAS is only the source.

For every mapped dataset it compares local and remote snapshot lists, then
streams only the missing snapshots over SSH
(`zfs send | mbuffer` on TrueNAS -> `mbuffer | zfs receive` locally):

- dataset missing locally -> full send of the oldest snapshot, then an
  incremental chain for every later snapshot;
- dataset present locally -> incremental sends (`-i`) from the newest common
  snapshot to every newer remote one;
- interrupted receive -> continued automatically on the next run using the
  ZFS `receive_resume_token`;
- local dataset tagged with a different source (`zfs-sync:source`) -> refused,
  nothing is written.

## Requirements

- Linux host with OpenZFS userspace (`zfs` in `PATH`), Python 3.9+.
- `mbuffer` on both sides (optional; disable with `mbuffer =` in config).
- SSH access to TrueNAS (root, or a user allowed to run `zfs`, see below).

## Install

```sh
install -m 0755 zfs-sync /usr/local/bin/zfs-sync
cp zfs-sync.conf.example /etc/zfs-sync.conf
chmod 0600 /etc/zfs-sync.conf        # contains key paths / host names
mkdir -p /var/lib/zfs-sync
zfs-sync --dry-run                   # review the planned streams
zfs-sync                             # first real run
```

### Daily schedule (systemd)

```sh
cp zfs-sync.service zfs-sync.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now zfs-sync.timer
```

The timer runs daily at 02:30 (`Persistent=true` catches up after downtime).

Cron alternative:

```cron
30 2 * * * /usr/local/bin/zfs-sync -c /etc/zfs-sync.conf >> /var/log/zfs-sync.log 2>&1
```

## Configuration

See `zfs-sync.conf.example` for every option with inline comments. The
essentials:

| Section | Key | Meaning |
|---|---|---|
| `[remote]` | `host`, `port`, `user` | TrueNAS SSH endpoint |
| | `identity_file` | private key for the connection |
| | `command_prefix` | e.g. `sudo -n` when the SSH user is not root |
| `[transfer]` | `mbuffer`, `mbuffer_size` | streaming buffer (empty = disabled) |
| | `retries`, `retry_delay` | per-dataset stream retry policy |
| `[zfs]` | `recv_flags` | flags for local `zfs receive` (default `-Fsu`) |
| | `include_children` | replicate remote children as individual streams |
| | `tag_local_source` | refuse to write datasets tagged with another source |
| | `use_resume_token` | resume interrupted receives |
| | `state_dir` | status files + lock file location |
| `[mappings]` | `<local> = <remote>` | one mapping per dataset pair |

Local datasets must live in a pool that already exists on the Linux host
(the tool never creates pool roots).

## TrueNAS notes

- **SSH key**: generate a dedicated key pair, put the public key in the sync
  user's `authorized_keys` on TrueNAS.
- **Non-root sync user (SCALE)**: add a sudoers line so
  `command_prefix = sudo -n` works:

  ```
  zfsync ALL=(ALL) NOPASSWD: /usr/sbin/zfs
  ```

- **mbuffer**: install on TrueNAS too (`pkg install mbuffer` on CORE, APT on
  SCALE); if it only exists on one side, set `mbuffer =` and accept the
  buffering loss, or move the buffer to the side that has it via the config.

## Behavior details

- **Dry run**: `zfs-sync -n` prints the exact remote send pipelines and local
  receive commands without transferring anything.
- **Selective run**: `zfs-sync tank-10-0/vm1` restricts to named mappings.
- **Resume**: after a broken receive, `receive_resume_token` on the local
  dataset is used with `zfs send -t <token>` on the next run.
- **No common snapshot**: if local and remote history are unrelated, the tool
  full-sends the newest remote snapshot; with `-F` in `recv_flags` this
  **rolls back local history** (logged as a warning).
- **Status**: per-dataset results are written to
  `<state_dir>/<local-dataset>.json`; a lock file prevents overlapping runs.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | all mappings ok or skipped |
| 1 | at least one mapping failed |
| 2 | configuration / argument error |
| 3 | another run is already active (lock) |

## Tests

Unit tests plus an end-to-end test that runs the tool against a fake `zfs`
and fake `ssh` (JSON-store simulator in `tests/`, no real pool is ever
touched):

```sh
python3 -m unittest discover -s tests -v
```
