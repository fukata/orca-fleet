# orca-fleet 導入ガイド（別環境向け）

別の環境に orca-fleet を導入し、Orca automations で毎日自動更新するまでの手順です。

設計と安全性の詳細は [README](../README.md) を参照してください。この文書は「手元の環境をどう合わせるか」に絞っています。

内容は 2026-10-25 に次の構成で導入したときの記録をもとにしています。

- クライアント: Windows 11 の Orca 1.4.239
- サーバー: Ubuntu 24.04 の2台
  - サーバー1（実機）: .deb 版、user unit で起動していた
  - サーバー2（WSL2）: AppImage 版、user unit で起動していた

---

## 0. 全体の流れ

| Step | 作業 | どこで | sudo |
|---|---|---|---|
| 1 | 現状調査と判定 | クライアント、各サーバー | 不要 |
| 2 | SSH の準備 | クライアント | 不要 |
| 3 | serve を system unit に移行（必要な場合のみ） | 各サーバー | 必要 |
| 4 | orca-serve-update を導入 | 各サーバー | 必要 |
| 5 | クライアントを導入 | クライアント | 不要 |
| 6 | 動作確認 | クライアント | 不要 |
| 7 | 自動更新（Orca automations） | クライアント | 不要 |

serve が再起動するのは Step 3 だけです。Step 4 では稼働中のサービスを止めません。

## 1. 現状調査と判定

### クライアント

```bash
orca --version                 # Windows / macOS の CLI 名は orca
orca-ide --version             # Linux の CLI 名は orca-ide（素の orca は GNOME の画面読み上げソフト）
orca host list --json          # kind: "environment" がペアリング済みのサーバー
orca status --json --environment <name>   # appVersion と installMode を確認
```

対象になるのは、`installMode: "unsupported-headless-serve"` の環境（Linux の headless serve）だけです。

### 各サーバー

```bash
. /etc/os-release; echo $PRETTY_NAME; uname -m      # Ubuntu/Debian 系、x86_64 または arm64
python3 --version                                   # 3.10 以上
dpkg-query -W orca-ide                              # .deb 版かどうか
systemctl list-units --type=service --all | grep -i orca          # system unit
systemctl --user list-units --type=service --all | grep -i orca   # user unit
loginctl show-user $USER -p Linger
ps -eo pid,args | grep 'cli/index.js serve' | grep -v grep        # serve の実体（AppImage なら squashfs-root や .mount_ を含む）
```

### 判定

| serve の起動方法 | インストール形式 | 必要な作業 |
|---|---|---|
| system unit（`User=` 指定あり） | .deb | Step 4 から |
| user unit（`~/.config/systemd/user/`） | .deb | Step 3 → 4 |
| user unit | AppImage | Step 3 を `--install-deb` 付きで → 4 |
| system unit | AppImage | 対象外。手作業で .deb に入れ替えてから Step 4 |
| 手動起動や tmux など、systemd の外 | — | 対象外。先に system unit を作る |
| macOS / Windows のデスクトップ版 | — | 対象外 |

orca-serve-update が扱えるのは「system unit（`User=` 指定）＋ `ExecStart` が `orca-ide serve` を直接起動している＋ .deb 版」の構成だけです。

`script -qfc "orca-ide serve ..."` のように別のコマンドで包んでいると、CLI のパスを `/usr/bin/script` と誤認します。Step 3 のスクリプトは包まない形の unit を作ります。

## 2. SSH の準備（クライアント）

`orca-fleet` は `ssh <接続先> sudo -n /usr/local/sbin/orca-serve-update ...` を実行します。そのため **パスワードなしで SSH ログインできる** ことが必要です。

```bash
# 鍵がなければ作成（Windows の場合は Git Bash か PowerShell で）
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
# サーバーの ~/.ssh/authorized_keys に公開鍵を追記（ssh-copy-id か手動）
```

`~/.ssh/config` に接続先名を定義しておくと、設定ファイルが読みやすくなります。Tailscale の IP を直接書くと、MagicDNS がなくても確実に届きます。

```
Host orca-srv1
	HostName <サーバー1の Tailscale IP>
	User <user>

Host orca-srv2
	HostName <サーバー2の Tailscale IP>
	User <user>
	IdentityFile ~/.ssh/id_ed25519
```

確認します。

```bash
ssh -o BatchMode=yes orca-srv1 hostname
```

## 3. serve を system unit に移行（user unit の場合のみ）

`server/migrate-to-system-unit.sh` を使います。手順は次のとおりです。

1. 稼働中の serve プロセスから起動引数を読み取ります。`PATH`、`LANG`、`DISPLAY`、`WAYLAND_DISPLAY`、`LIBGL_ALWAYS_SOFTWARE` も同時に読み取り、そのまま引き継ぎます。エージェント CLI（claude、node など）は serve の PATH から解決されるためです。
2. `--install-deb VERSION` を指定すると、公式の .deb に入れ替えます。sha256 は GitHub のリリース digest と照合します。あわせて `~/.local/bin/orca-ide` を .deb 版に向け直し、元のリンクは `.appimage-bak` として残します。
3. linger を有効にし、`/etc/systemd/system/orca-serve.service` を作ります。
4. user unit を停止・無効化し、system unit を起動します。ポートが LISTEN しなければ user unit に戻します。
5. `--then-install` を指定すると、続けて Step 4 の `install.sh` も実行します。

```bash
# クライアントから送る（必ず LF のまま送ること。後述「つまずきポイント」参照）
scp -r server <host>:/tmp/osu

# 確認（何も変更しない）
ssh -t <host> 'cd /tmp/osu && sudo ./migrate-to-system-unit.sh --dry-run'
# AppImage の場合: 現在と同じバージョンを指定すると、バージョンを変えずに形式だけ移せる
ssh -t <host> 'cd /tmp/osu && sudo ./migrate-to-system-unit.sh --install-deb 1.4.237 --dry-run'

# 実行（serve が数秒止まる。エージェントが作業していないときに行う）
ssh -t <host> 'cd /tmp/osu && sudo ./migrate-to-system-unit.sh [--install-deb X.Y.Z] --then-install'
```

注意点です。

- PTY デーモンは serve とは別の scope で動いています。そのためターミナルは残る想定です。今回の2台では、既存のデーモンが移行後も生き残りました。ただし保証はありません。
- 古い user unit のファイルは、無効化したうえで残します。不要になったら削除してください。
- ペアリングは serve の設定ディレクトリ（`~/.config/orca`）に保存されています。移行してもペアリングし直す必要はありません。

## 4. orca-serve-update を導入（各サーバー）

Step 3 で `--then-install` を指定した場合は不要です。

```bash
ssh -t <host> 'cd /tmp/osu && sudo ./install.sh --dry-run'            # 確認
ssh -t <host> 'cd /tmp/osu && sudo ./install.sh --operator <user>'    # 導入
ssh <host> 'sudo -n /usr/local/sbin/orca-serve-update --json status'  # パスワードなしで通ること
cat /etc/orca-serve-update/config.json                                # cli が /usr/bin/orca-ide になっていること
```

`install.sh` が行うことは次の3つです。

- helper を `/usr/local/sbin` に置く
- `orca-updaters` グループを作り、公開サブコマンドだけを許可する sudoers を置く
- serve の unit を見つけて config.json を書く

sudo の許可はグループ経由です。グループ追加の直後でも、新しい SSH セッションなら反映されます。

## 5. クライアントを導入

### Linux / macOS

```bash
client/install.sh            # ~/.local/bin/orca-fleet と ~/.claude/skills/orca-fleet
orca-fleet discover          # 一覧の確認
orca-fleet discover --write  # ~/.config/orca-fleet/config.json を生成
```

### Windows

Git Bash で次を実行します。

```bash
client/install.sh            # orca-fleet と orca-fleet.cmd（PowerShell / cmd 用）が入る
```

前提条件:

- python 3.10 以上
- OpenSSH クライアント（Windows 標準）
- `%USERPROFILE%\.local\bin` が PATH に入っていること

`orca-ide` が見つからない場合は、自動で `orca` を使います。

### 設定ファイル

`discover --write` は、環境名をそのまま SSH の接続先名として書き込みます。Step 2 で別名を付けた場合は、`ssh` を書き換えてください。

```json
{"orca_cli": "orca",
 "servers": [
  {"name": "srv1", "environment": "<環境名1>", "ssh": "orca-srv1", "enabled": true, "control": false},
  {"name": "srv2", "environment": "<環境名2>", "ssh": "orca-srv2", "enabled": true, "control": false}
 ]}
```

- `environment` は `orca host list` に表示される環境名です。
- 操作役のホスト自身も更新対象にする場合は、そのサーバーに `control: true` を付けます。最後に更新されます。

## 6. 動作確認

```bash
orca-fleet status          # SERVICE が active/running、ERROR が空であること
orca-fleet plan            # 各サーバーの ACTION
orca-fleet update --dry-run   # ダウンロード予定（ターゲットとロールバック用）だけを表示
```

手動で更新する場合は `orca-fleet update` を実行します。計画が表示され、y/N で承認すると、並列で準備したあと1台ずつ更新します。

## 7. 自動更新（Orca automations）

クライアント上の Orca automation で、毎日決まった時刻に実行します。

- precheck で更新が必要かを判定し、不要なら agent を起動しません。そのため token を消費しません。
- 必要な場合だけ Claude が `update` を実行し、結果を報告します。
- automation はクライアントで動きます。更新対象の serve が再起動しても、途中で切れません。

```bash
orca automations create \
  --name "orca-fleet daily update" \
  --trigger daily --time 04:00 --timezone Asia/Tokyo \
  --provider claude \
  --workspace "path:<orca-fleet リポジトリの絶対パス>" \
  --precheck "orca-fleet plan --needs-action" --precheck-timeout 120 \
  --missed-run-grace-minutes 120 \
  --fresh-session --enabled \
  --prompt "$(cat <<'EOF'
orca-fleet で Orca Remote Server (headless serve) を最新安定版に揃える定期実行です。

1. `orca-fleet update --yes --wait-idle 1800 --track` を実行する（SAFE モード）。
2. 終了後 `orca-fleet status` を実行する。
3. 結果を日本語で短く報告する: サーバーごとの 旧→新バージョン と結果（succeeded / skipped / deferred / rolled_back / failed / unverified）。
   失敗・rolled_back・unverified があれば `orca-fleet progress` の error を添える。

禁止: `--mode force`、`rollback`、`recover`、設定ファイルやサーバーの手動変更。問題があれば報告だけして終了する。
EOF
)"
```

| 設定 | 意味 |
|---|---|
| `plan --needs-action` | 更新対象（update / defer）か要復旧（recover-first）のサーバーがあれば exit 0 で実行します。全台が skip か unreachable なら exit 1 で、その回はスキップされます。 |
| `--wait-idle 1800` | SAFE で延期されたサーバーについて、最大30分エージェントの終了を待って再試行します。それでも作業中なら次の回に回します。 |
| `--track` | 各サーバーの結果を Orchestration の Run/Task に記録します。 |
| `--missed-run-grace-minutes` | PC がスリープしていて実行時刻を逃した場合に、何分後まで遅れて実行するかです。既定は 720 分（12時間）なので、日中に実行されることがあります。 |

作成後の確認です。

```bash
orca automations list
orca automations run <id>     # 今すぐ実行する。更新対象があれば本当に更新される
orca automations runs <id>    # 実行履歴（skipped / 成功 / 失敗）
```

初回は手動で `run` して、次の点を確かめてください。

- precheck のコマンドが PATH から見つかるか
- Claude が確認なしでコマンドを実行できるか（ホストのエージェント起動引数の設定による）

## 8. つまずきポイント（今回実際に起きたこと）

| 症状 | 原因 | 対処 |
|---|---|---|
| サーバーでスクリプトが `$'\r': command not found` などで失敗する | Windows でチェックアウトしたファイルが CRLF になっていた | リポジトリは `.gitattributes` で LF に固定済みです。古い clone では `git add --renormalize . && git checkout -- .` を実行するか、サーバー上で `sed -i 's/\r$//'` で変換します。 |
| `init` が `has no explicit User=` で失敗する | serve が user unit で動いている | Step 3 で system unit に移行します。 |
| config.json の `cli` が `/usr/bin/script` になる | `ExecStart` が `script -qfc` で包まれている | `orca-ide serve ... --json` を直接起動する形にします（Step 3 のスクリプトはそうします）。 |
| `sudo -n` で password is required になる | `install.sh` が実行されていない、またはグループに追加されていない | `ls /etc/sudoers.d/orca-serve-update` と `id` で確認し、`install.sh --operator <user>` を実行し直します。 |
| SAFE で常に延期される | linger が無効 | `sudo loginctl enable-linger <user>` を実行します（Step 3 のスクリプトはこれも行います）。 |
| WSL に SSH で入れない | Windows 側の鍵が WSL の `authorized_keys` に登録されていない | `wsl -- bash -c 'cat >> ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub` を実行します。 |
| WSL の serve が止まる | WSL ディストリビューション自体が停止している | WSL で systemd を有効にし（`/etc/wsl.conf` の `[boot] systemd=true`）、linger も有効にします。Windows にログインしていない間は WSL が動いていないことがあります。 |

## 9. 撤去

```bash
# クライアント
orca automations remove <id>
rm ~/.local/bin/orca-fleet ~/.local/bin/orca-fleet.cmd ~/.config/orca-fleet/config.json
rm -r ~/.claude/skills/orca-fleet

# サーバー
sudo rm /usr/local/sbin/orca-serve-update /etc/sudoers.d/orca-serve-update
sudo rm -r /etc/orca-serve-update /var/lib/orca-serve-update /var/log/orca-serve-update
sudo groupdel orca-updaters
# user unit に戻す場合
sudo systemctl disable --now orca-serve.service && sudo rm /etc/systemd/system/orca-serve.service && sudo systemctl daemon-reload
systemctl --user enable --now orca-serve.service
```
