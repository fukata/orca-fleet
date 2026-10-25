# Orca Remote Server 一括アップデート (orca-fleet / orca-serve-update)

複数の Linux サーバーで動く `orca serve` を、クライアントから同じ Orca バージョンへ揃えるための仕組みです。

- **`client/orca-fleet`**: クライアント側の CLI です。次の処理を行います。
  - 状態確認と計画作成
  - サーバーごとの並列ダウンロード
  - サーバーを1台ずつ更新し、検証する
  - Orchestration への状態記録
- **`server/orca-serve-update`**: 各サーバーに入れるヘルパーです。root で実行します。
  - 公式リリースの取得と検証を行います。
  - 更新処理は transient systemd unit に引き渡して実行します。
  - 状態の永続化、監査ログ、ロールバック、中断からの復旧を担います。
- **`skill/orca-fleet`**: Orca 上の Claude から自然言語で操作するためのスキルです。

---

## 1. 現状調査結果（2026-10-25 時点）

### クライアント（このPC）

- Orca IDE 1.4.239 を `/opt/Orca` にインストールしています。CLI は `orca-ide` です。
- **`orca` という名前は GNOME の画面読み上げソフトを指します。** 誤って実行しないよう、ツールは常に `orca-ide` を使います。

### Orca CLI で実在を確認した機能

| 機能 | 結果 |
|---|---|
| `orca-ide host list --json` | 実在します。local、SSH ターゲット、ペアリング済みの環境（`kind: "environment"`）を返します。 |
| `--environment <name>` | 実在します。ほぼ全コマンドで使えます。ペアリング済みのリモート Orca に対して実行されます。 |
| `orca-ide environment list/add/rm` | 実在します。 |
| `orca-ide status --json --environment X` | `appVersion` と `remoteUpdateSupport` を返します。headless serve では `installMode: "unsupported-headless-serve"` になります。 |
| `orca-ide worktree ps --json` | 稼働中のエージェントを判定できます。`agents[].state` は `working`、`blocked`、`waiting`、`done` のいずれかです。`workingMode: "monitoring"` は `/loop` などの監視ループを表します。 |
| Orchestration (`run-create` / `task-*` / `worker-start --on <env>` / `check`) | 実在します。Run、Task、Dispatch で AI エージェントの Worker を管理します。 |

### 環境の状況

| 環境 | 状況 |
|---|---|
| `dev` | Linux（Ubuntu 24.04、x86_64）。到達できます。**現在は 1.4.229 です。** |
| `mac` / `win` | 到達できませんでした（offline）。どちらも Linux の serve ではないため対象外です。 |

### dev サーバーの構成

| 項目 | 値 |
|---|---|
| 配布形式 | **.deb パッケージ** `orca-ide`。AppImage ではありません。本体は `/opt/Orca` で、`/usr/bin/orca-ide` はそこへのシンボリックリンクです。 |
| サービス | `/etc/systemd/system/orca-serve.service` |
| systemd 設定 | system unit、`User=<user>`、`KillMode=control-group`、`Restart=on-failure`、`RestartPreventExitStatus=3` |
| 起動コマンド | `/usr/bin/orca-ide serve --port 6768 --pairing-address <tailscale-ip> --json` |
| linger | `Linger=yes` |
| 過去の更新 | 手作業で `dpkg -i` を実行し、サービスを再起動していました（dpkg.log で確認）。 |

### 更新元

GitHub の `stablyai/orca` Releases です。

- 安定版のタグは `vX.Y.Z` 形式です。`mobile-android-v*` という別系統のリリースもあり、prerelease フラグが付いているものがあります。
- アセット名は `orca-ide_<ver>_amd64.deb` と `orca-ide_<ver>_arm64.deb` です。
- GitHub API がアセットごとに **sha256 の digest** を返します。`latest-linux.yml` にも sha512 があります。
- GPG 署名や `.sig` ファイルはありません。

## 2. 実現可能性と制約

### セッション保護（v1.4.239 のソースコードと E2E で確認）

- **PTY デーモンは `orca serve` の子プロセスではありません。**
  - デーモンは `systemd-run --user --scope` で独立した scope に置かれます（`user@UID.service/app.slice/orca-daemon-*.scope`）。
  - 各ターミナルも個別の scope に置かれます。
  - そのため `orca serve` を再起動しても、Claude Code などのプロセスは止まりません。
- **前提条件は linger が有効なことです。** linger が無効だと、デーモンがサービスの cgroup に残ります。その状態で `KillMode=control-group` のサービスを止めると、デーモンも一緒に終了します。
- **バージョンをまたぐ場合の挙動:**
  - 新しいビルドが旧デーモンのプロトコルを扱える場合（`PROTOCOL_VERSION` または `PREVIOUS_DAEMON_PROTOCOL_VERSIONS` に含まれる場合）は、旧デーモンを残します。既存のセッションは旧デーモン上で継続し、新しいターミナルは新デーモンで作られます。
  - 扱えない場合、既存のターミナルは取り残されます（`strands-live-terminals`）。
  - 本ツールは、ターゲットのタグにある公式ソースからこの互換性を読み取って判定します。
- **E2E での実証:** dev と同じ条件で確認しました。1.4.229 から 1.4.239 へ、プロトコル v48 から v51 への更新です。
  - 長時間動くプロセスと PTY デーモンが更新後も生き残りました。
  - 既存のターミナルは connected かつ writable のままでした。
  - 新しいターミナルの入出力も正常でした。
- ただし、これは Orca の文書化された挙動を確認したものであり、**保証ではありません。** ツールは「安全」と断定せず、警告として表示します。

### Orca Orchestration だけでは実現できない点

1. **headless serve には公式のリモート更新手段がありません。**
   - `updater.remote-control` は headless の orcad では `remote_update_manual_required` を返します。
   - `--supervise` が更新を待機する機能は macOS 専用です。Linux では、dpkg で更新しても serve は再起動されません。
2. **Worker は AI エージェントです。** Orchestration にはリモートで決まったコマンドを確実に実行する仕組みがありません。`worker-start` には `--agent` か既存ターミナルの指定が必要です。
3. **root 権限が必要です。** dpkg と systemctl の操作には root が要りますが、Orca にはその権限を扱う仕組みがありません。
4. **再起動中は連携が途切れます。** 更新対象の serve を経由した連携は、その serve の再起動中に途切れます。
5. **Orchestration のコマンドは Orca ターミナル内でしか動きません。** 送信元のターミナルハンドル（`ORCA_TERMINAL_HANDLE` または `--from`）が必要です。
6. **Android アプリには CLI がありません。** Android から操作するには、操作役のホストで動く Claude にチャットで依頼する形になります（`skill/` を使用）。Android 実機での確認はまだです。

### 採用した構成

| 役割 | 担当 |
|---|---|
| Discovery、独立検証、稼働判定、互換性判定、Run/Task 記録 | Orca の標準機能 |
| 確実な実行 | SSH、sudo、systemd |

### 前提条件

- サーバーへの SSH 接続（Tailscale SSH でも可）
- sudo
- python3 3.10 以上
- systemd
- dpkg（.deb でインストールしている環境）

RPM や AppImage でインストールしている環境は対象外です。

## 3. 全体アーキテクチャ

```
 [Orca client / Claude(スキル)]                         [server N]
  orca-fleet ──(orca-ide host list/status --environment)──▶ orca serve (Orca接続: 独立検証)
      │
      └─(ssh → sudo -n orca-serve-update …)──────────────▶ orca-serve-update
                                                             ├ prepare: GitHub公式 .deb 取得→sha256→dpkg-deb検証
                                                             ├ apply: preflight → systemd-run (transient unit)
                                                             │          orca-serve-update-worker.service
                                                             │            stop → dpkg -i → start → verify
                                                             │            失敗→ 旧.deb で rollback → verify
                                                             ├ state: /var/lib/orca-serve-update/state.json
                                                             └ audit: /var/log/orca-serve-update/audit.jsonl
```

### 役割分担

- **Coordinator（orca-fleet、または Orca ターミナル内の Claude）** が担当すること:
  - 計画の作成
  - 承認の取得
  - 並列での Prepare
  - サーバーを1台ずつ Apply
  - 状態のポーリング
  - 独立検証
  - Run/Task への記録（`--track`）
- **サーバー側のワーカー（transient systemd unit）** が担当すること:
  - 実際の切り替え、検証、ロールバック
  - SSH や Orca の接続、クライアントが切れても処理を続けます。

## 4. 更新処理の設計

| Phase | 内容 |
|---|---|
| 1 Discovery | 環境を列挙し、Orca 接続でのバージョン、SSH 経由のサービス状態、エージェントの稼働、進行中の操作を取得します。そのうえで計画（update / skip / defer / unreachable / recover-first）を表示します。 |
| 2 Prepare（並列） | ターゲットと、**現在のバージョン（ロールバック用）** の .deb を取得します。手順は次のとおりです。<br>・root 所有で 0700 のキャッシュに一時ファイル名で取得<br>・サイズ、sha256（GitHub digest）、`dpkg-deb` のフィールド（Package / Version / Architecture）を検証<br>・すべて通ってから正式なファイル名に rename<br>この処理は冪等です。 |
| 3 Update（1台ずつ） | 二重起動を防ぐ仕組みは3層あります。固定名のワーカー unit（systemd）、flock、状態のチェックです。<br>処理の流れ:<br>・ワーカー内で preflight を再実行<br>・`systemctl stop`<br>・`dpkg -i`（ダウングレードは拒否）<br>・インストールされたバージョンを確認<br>・`systemctl start` |
| 4 Verify | 次の条件がすべて満たされることを確認します。<br>・`ActiveState=active` で MainPID がある<br>・ポートが LISTEN している<br>・`orca-ide status` が `ready` で、`appVersion` がターゲットと一致<br>さらに15秒間、PID と NRestarts が変わらないことを確認します。<br>その後、クライアント側からも Orca 接続でバージョンを確認します。 |
| 5 Rollback | 失敗したら旧 .deb を `--force-downgrade` で入れ直し、起動と検証をやり直します。結果は `rolled_back` か `rollback_failed` として記録します。 |
| Recover | 状態ファイルが途中のままでワーカーがいない場合は stale と判定します。`recover` を実行すると次のどちらかになります。<br>・パッケージがすでにターゲットで正常に動いていれば、更新を完了扱いにする<br>・そうでなければロールバックする |

### SAFE / FORCE

| 条件 | SAFE | FORCE |
|---|---|---|
| ターン実行中のエージェント（`state: working`、monitoring 以外） | 延期 | 警告して続行 |
| エージェントの稼働状況が取得できない | 延期 | 警告 |
| linger が無効で、セッションがある | 延期 | 警告 |
| 新ビルドが旧デーモンのプロトコルを扱えない | 延期 | 警告 |
| `/loop` などの monitoring 中、または権限待ち（blocked） | 警告のみ | 警告のみ |
| 準備不足、空き容量不足、インストール先の不正 | blocked | blocked |

監視ループが常に working と表示されることで、更新が永久にできなくなる事態を避けています。待ちたい場合は `--wait-idle 秒` を指定します。定期実行したい場合は、Orca の automations や cron から `orca-fleet update --yes` を呼べます。**自動更新は初期状態では無効です。**

### セキュリティ

- 取得元は `stablyai/orca` に固定しています。
  - URL は許可リストで制限しています（https、api.github.com / github.com / *.githubusercontent.com のみ）。
  - アセットの URL はタグから組み立てた期待値と完全に一致する必要があります。
  - prerelease と draft は拒否します。
- バージョン文字列は `^\d+\.\d+\.\d+\Z` で厳密に検証します（末尾の改行も拒否）。
- サブプロセスはすべて argv 配列で実行し、shell は使いません。SSH の引数は `shlex.quote` でエスケープします。
- ファイル操作の安全対策:
  - キャッシュや状態のディレクトリは root 所有で、シンボリックリンクを拒否します。
  - ファイルは O_NOFOLLOW で開き、一時ファイルを rename して配置します。
  - 有効化の直前にキャッシュを再検証し、改ざんがあれば検出します。
- インストール先（`/opt/Orca`）が root 所有で group/other に書き込み権限がないこと、`cli` がその中を指していることを確認します。
- sudo の権限設定:
  - `orca-updaters` グループの所属者だけが、公開サブコマンドを sudo で実行できます。
  - `_worker` と `init` は sudoers で拒否しています。さらに `_worker` は systemd 内（`INVOCATION_ID` あり）でしか動きません。
  - `env_reset` と `!setenv` を指定しています。
- ワーカー unit は `NoNewPrivileges`、`ProtectKernel*`、`ProtectClock`、`LockPersonality`、`RuntimeMaxSec=1800` で制限しています。
- 監査ログは JSON Lines 形式です。sudo したユーザー、op_id、各フェーズを記録します。

## 5. 実装ファイル

```
client/orca-fleet            クライアントCLI (python3 標準ライブラリのみ)
client/orca-fleet.cmd        Windows (PowerShell / cmd) 用ランチャー
client/install.sh            ~/.local/bin と ~/.claude/skills へ配置
server/orca-serve-update     サーバーヘルパー (python3 標準ライブラリのみ)
server/install.sh            サーバーへの導入 (helper / sudoers / config)
server/migrate-to-system-unit.sh  user unit / AppImage の serve を system unit + .deb へ移行
server/systemd/…/recommended.conf  任意: RestartPreventExitStatus=3 78
skill/orca-fleet/SKILL.md   自然言語操作用スキル
docs/setup-guide.md          別環境への導入ガイド (現状調査 → 移行 → 導入 → automations)
tests/test_server.py         サーバー側ユニットテスト (51)
tests/test_client.py         クライアント側ユニットテスト (28)
tests/e2e/                   systemd コンテナでの実 .deb E2E
```

## 6. systemd

- 常駐させるユニットは追加しません。更新のたびに `systemd-run` で `orca-serve-update-worker.service` を作り、終了後は `--collect` で後始末されます。
- 既存の `orca-serve.service` は変更しません。
- `recommended.conf` は任意です。終了コード 3（別の Orca が稼働中）や 78（設定不備）のときに再起動ループに入るのを防ぎます。

## 7. インストール・設定手順

別の環境に導入する場合は、[導入ガイド](docs/setup-guide.md) に沿って進めてください。現状調査、user unit や AppImage からの移行、Orca automations での自動更新までを扱っています。

### サーバーごと

この作業では稼働中のサービスを止めません。

```bash
scp -r server dev:/tmp/osu && ssh -t dev 'cd /tmp/osu && sudo ./install.sh --dry-run'   # 確認
ssh -t dev 'cd /tmp/osu && sudo ./install.sh --operator <user>'                        # 導入
ssh dev 'sudo -n /usr/local/sbin/orca-serve-update --json status'                       # 確認
```

- `init` は `orca-ide serve` を実行しているユニットを探し、サービス名、ユーザー、ポート、CLI のパスを `/etc/orca-serve-update/config.json` に書き込みます。
- 該当するユニットが複数ある場合は `--service` で指定します。
- linger が無効な場合は警告を出します。

### クライアント

```bash
client/install.sh
orca-fleet discover            # paired 環境を列挙 (headless serve のみ enabled)
orca-fleet discover --write    # ~/.config/orca-fleet/config.json
```

#### Windows クライアント

- Git Bash で `client/install.sh` を実行します。`~/.local/bin` に `orca-fleet` と `orca-fleet.cmd` が入るので、PowerShell や cmd からも `orca-fleet` で呼べます（`~/.local/bin` を PATH に入れておきます）。
- 前提は python 3.10 以上と OpenSSH クライアント（Windows 標準）です。
- Windows と macOS の Orca CLI は `orca` です。`orca-ide` が見つからない場合は `orca` を使います。明示したい場合は設定ファイルで `"orca_cli": "orca"` を指定します。
- リポジトリは `.gitattributes` で LF に固定しています。サーバーへ `scp` するスクリプトが CRLF になると動かないためです。それ以前に clone した作業ツリーは `git add --renormalize .` のあと、`git checkout -- .` でチェックアウトし直してください。

設定ファイルの例です。`ssh` には SSH の接続先名を書きます。`control: true` を付けたサーバーは最後に更新します。

```json
{"orca_cli": "orca-ide",
 "servers": [{"name": "dev", "environment": "dev", "ssh": "dev", "enabled": true, "control": false}]}
```

## 8. 実行方法

```bash
orca-fleet status                       # 全サーバーの状態とバージョン、バージョンのずれ (fleet 行)
orca-fleet status --check               # 全台が到達可能かつターゲット版なら exit 0、ずれがあれば exit 1
orca-fleet plan [--version 1.4.239]     # 更新計画 (read-only)
orca-fleet update --dry-run             # 取得予定のみ表示
orca-fleet update                       # 計画表示 → y/N 承認 → 並列prepare → 1台ずつ更新 → 結果表
orca-fleet update --wait-idle 1800      # SAFE で延期されたら最大30分待って再試行
orca-fleet update --mode force          # FORCE: "FORCE" と入力して承認
orca-fleet progress                     # サーバー側の最終操作の状態
orca-fleet retry                        # failed / rolled_back / deferred のサーバーだけ再実行
orca-fleet rollback --server dev [--to 1.4.229]
orca-fleet recover --server dev         # 中断した操作の整合
orca-fleet update --yes --track         # (Orca ターミナル内) Run/Task に記録
```

### 実行結果の判定（outcome）と通知

`update` / `retry` は最後に、その回の結果を1語で分類した `outcome` と、全サーバーのバージョン分布（`fleet` 行）を出力します。`--json` の出力にも同じ内容が入ります。

| outcome | 意味 |
|---|---|
| `nothing_to_update` | 更新対象のサーバーがない（全台 skip か unreachable） |
| `updated` | 更新したサーバーがすべて成功した |
| `deferred` | 失敗はないが、SAFE で延期したサーバーがある |
| `rolled_back` | 自動ロールバックが発生した（旧版で正常稼働） |
| `failed` | 上記以外の失敗がある（failed、rollback_failed、prepare_failed、unverified、not_attempted など） |

複数に該当する場合は `failed` → `rolled_back` → `deferred` → `updated` の順に優先します。

```
outcome: deferred  (succeeded: srv1; deferred: srv2; unreachable: srv3)
fleet: 1.4.239 x1 (srv1), 1.4.229 x1 (srv2)  -- DRIFT: behind 1.4.239: srv2; version unknown: srv3; not reachable over Orca: srv3
```

`fleet` 行のバージョンには、Orca 接続で見えるバージョン（クライアントが実際に話している serve のバージョン）を使います。Orca で到達できないときだけ、SSH で取得したインストール済みのバージョンを使います。

設定ファイルに `notify_command`（argv の配列）を書くと、`update` / `retry` が終わるたびにそのコマンドを実行します。`--dry-run` のときと、確認で中止したときは実行しません。コマンドには次のものが渡されます。

- 標準入力: `outcome`、`target`、`servers`（結果ごとのサーバー名）、`fleet`（バージョン分布とずれ）、`errors` を含む JSON
- 環境変数 `ORCA_FLEET_OUTCOME`

シェルを経由しないので、パイプを使いたい場合はスクリプトを用意します。

```json
{"notify_command": ["/home/<user>/.local/bin/orca-fleet-notify"]}
```

```bash
#!/bin/sh
# 例: ntfy に送る。成功・更新なしは低優先度にする
case "$ORCA_FLEET_OUTCOME" in failed|rolled_back) prio=high ;; deferred) prio=default ;; *) prio=low ;; esac
curl -fsS -H "Title: orca-fleet: $ORCA_FLEET_OUTCOME" -H "Priority: $prio" --data-binary @- https://ntfy.sh/<topic>
```

automation の precheck（`plan --needs-action`）で全台 skip だった日は `update` 自体が起動しないため、通知は送られません。その日の結果は automation の実行履歴に skipped として残ります。

自然言語で操作する場合は、スキルを入れた Claude（Orca ターミナル内）に、たとえば「接続しているすべてのOrcaサーバーを最新安定版に揃えて」と依頼します。Claude は計画を表示して承認を求め、承認後に `update --yes` を実行して結果の表を返します。

## 9. テスト

```bash
python3 -m unittest discover -s tests -v       # 79 tests
```

### ユニットテストで扱うケース

- 同じバージョンなら skip する
- 更新の成功
- 並列での Prepare と、1台ずつの Apply
- 再起動中の切断に耐える
- ダウンロード失敗、ディスク容量不足、チェックサム不一致、パッケージのフィールド不一致、キャッシュの改ざん
- 起動失敗、バージョン不一致、再起動を繰り返す状態、dpkg の失敗のそれぞれでロールバックする
- ロールバック自体の失敗
- 二重実行（ワーカーと flock）
- 実行中のロールバックとバージョン比較の競合
- SAFE の延期、monitoring の扱い、FORCE
- linger 無効、プロトコル非互換
- ダウングレードの拒否
- 中断からの復旧
- インジェクション（バージョン文字列、SSH の接続先）
- URL の許可リスト
- 失敗時にロールアウトを止める、`continue-on-error`
- 独立検証との不一致（unverified）
- retry
- `--track`

### E2E

`tests/e2e/`。Ubuntu 24.04 の systemd コンテナで、本物の GitHub リリースと dpkg を使いました。

| シナリオ | 結果 |
|---|---|
| 1.4.238 → 1.4.239 の更新 | 成功。PTY のプロセスとデーモンが生き残りました。 |
| 1.4.239 → 1.4.238 の手動ロールバック | 成功。実行中の二重実行は `locked` で拒否されました。 |
| 1.4.239 の起動失敗を注入 | 自動ロールバックで 1.4.238 に戻り、正常に動きました（`rolled_back`）。 |
| 起動ユニット自体を壊した場合 | `rollback_failed` を正しく報告しました。 |
| **1.4.229 → 1.4.239（dev と同条件、v48→v51）を `orca-fleet update --yes` で実行** | 成功。独立検証も OK で、既存のターミナルが継続しました。 |
| ワーカーを SIGKILL で強制終了 | stale を検出し、`recover` で整合しました。 |
| sudoers | グループ外は拒否、`_worker` と `init` も拒否されました。 |

E2E の途中で、ユニットテストでは見つからなかったバグを2件発見し、修正しました。

1. ワーカーが自分自身を「実行中のワーカー」と判定していました。
2. ロールバック中に apply すると skip と判定されていました。

### 未検証の項目

- `--track` を Orca ターミナル内で実行すること
- Android からの操作
- `dpkg` 実行中にプロセスを強制終了された場合（パッケージが中途半端な状態になったとき。`recover` で `dpkg -i` を再実行して修復する設計です）
- arm64 環境
- 本番の dev への適用

## 10. 運用・復旧手順

| 状況 | 対応 |
|---|---|
| `deferred` | エージェントが作業中です。終わるのを待つか、`--wait-idle` を使うか、承認を得てから FORCE で実行します。 |
| `rolled_back` | 旧バージョンで正常に動いています。`orca-fleet progress` で `error` を確認し、サーバー上の `journalctl -u orca-serve` と `/var/log/orca-serve-update/audit.jsonl` を調べます。 |
| `rollback_failed` | サービスが止まっている可能性があります。`ssh dev 'sudo systemctl status orca-serve; sudo journalctl -u orca-serve -n 100'` で状況を確認します。<br>修復するには、`sudo orca-serve-update --json rollback --to <旧版>` を実行するか、キャッシュの .deb（`/var/lib/orca-serve-update/cache/`）を `sudo dpkg -i` で手動で入れ直してから `sudo systemctl restart orca-serve` を実行します。<br>PTY デーモンは別の scope にあるため、この間もターミナルは生き続けます。 |
| `unverified` | サーバー側は成功ですが、クライアントからは新しいバージョンが見えていません。ペアリングやネットワークを確認し、`orca-fleet status` で再確認します。 |
| `stale_operation` | ワーカーが中断されています（再起動や kill など）。`orca-fleet recover --server X` を実行します。 |
| ワーカーの確認 | `systemctl status orca-serve-update-worker`、`journalctl -u orca-serve-update-worker` |
| アンインストール | `sudo rm /usr/local/sbin/orca-serve-update /etc/sudoers.d/orca-serve-update; sudo rm -r /etc/orca-serve-update /var/lib/orca-serve-update /var/log/orca-serve-update; sudo groupdel orca-updaters` |

## License

MIT. See [LICENSE](LICENSE).
