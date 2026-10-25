---
name: orca-fleet-update
description: Check and update the Orca versions of remote `orca serve` servers (paired Orca environments) so they all run the same release. Use when the user asks things like "すべてのOrcaサーバーのバージョンを確認して", "最新安定版に更新できるサーバーを一覧表示して", "すべてのOrcaサーバーを同じバージョンに更新して", "実行中のエージェントがあるサーバーはスキップして", "更新に失敗したサーバーを確認して", "接続しているすべてのOrcaサーバーを最新安定版に揃えて", or asks to roll back / recover an Orca server update.
---

# Orca fleet update

`orca-fleet` (client) drives `orca-serve-update` (installed on each server).
It discovers servers through Orca (`orca-ide host list` / `status --environment`),
downloads official releases on every server in parallel, restarts `orca serve`
one server at a time through a transient systemd unit, verifies on the server and
again from this client over the Orca connection, and rolls back on failure.

Never run bare `orca` on Linux (GNOME screen reader). The tools use `orca-ide`.

## Map requests to commands

| Request | Command (read-only unless noted) |
| --- | --- |
| バージョンを確認 / status | `orca-fleet status` |
| 更新できるサーバーを一覧 | `orca-fleet plan` (add `--version X.Y.Z` for a specific release) |
| 何が起きるか確認だけ | `orca-fleet update --dry-run` |
| 同じバージョンに揃えて / 最新安定版に揃えて | plan → approval → `orca-fleet update --yes [--track]` (**mutating**) |
| エージェント実行中はスキップ | default SAFE mode already defers those servers; say so |
| 空くまで待ってから更新 | add `--wait-idle 1800` |
| 失敗したサーバーを確認 | `orca-fleet progress` |
| 失敗したサーバーを再実行 | plan → approval → `orca-fleet retry --yes` (**mutating**) |
| ロールバック | `orca-fleet rollback --server NAME --dry-run`, then approval, then `--yes` (**mutating**) |
| 中断された更新の復旧 | `orca-fleet recover --server NAME` (**mutating**) |
| サーバーの追加・確認 | `orca-fleet discover` (`--write` updates the config) |

## Approval rule (mandatory)

Before any mutating command:

1. Run `orca-fleet plan` (with the same `--version`, `--server`, `--mode`) and show
   the user the table: servers, current → target version, running agents, action.
2. Ask for explicit approval naming the servers and version. Do not infer approval
   from the original request — "揃えて" is a request for a plan, not consent.
3. Only after the user approves in this conversation, run the command with `--yes`.

FORCE mode (`--mode force --i-understand-force`) restarts even while agents are
running a turn. Use it only when the user explicitly asks for FORCE after you have
warned them that terminals usually survive (they live in a separate Orca daemon)
but survival is not guaranteed.

## Reporting

After a mutating run, report per server: result (succeeded / skipped / deferred /
rolled_back / failed / unverified), versions before → after, and any error. Use
`orca-fleet progress` for the server-side record if the run output was cut off.
A `deferred` server is not a failure: say why (agents working, activity unknown,
protocol would strand terminals, lingering off) and offer `--wait-idle` or FORCE.

`--track` records each server as a Task in an Orca orchestration Run; it needs to
run inside an Orca terminal (ORCA_TERMINAL_HANDLE set). Outside one it prints a
warning and continues without tracking.

## Safety notes

- Updating the server that hosts this agent restarts the `orca serve` you may be
  talking through. Mark it `"control": true` in `~/.config/orca-fleet/config.json`
  so it goes last; the agent process itself survives (it lives in the PTY daemon),
  but the client connection drops briefly.
- Never edit `/etc/orca-serve-update`, sudoers, or systemd units on servers
  without the user asking.
