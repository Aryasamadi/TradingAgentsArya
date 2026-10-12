# TradingAgentsArya — Safe Telegram Controller

## What this change protects

- Provider/model settings are backed up as an encrypted SQLite snapshot on the dedicated `bot-state` branch. The controller checks that a backup succeeds before accepting new runs or confirming critical model/provider changes.
- Engine cache and report artifacts are encrypted before they are uploaded to GitHub Actions. The controller decrypts them only when needed. The PDF file bytes are preserved exactly; this process does not edit the PDF or original agent files.
- The short Telegram explanation is generated into a separate `telegram_summary.json`. It does not replace `report.json`, the PDF, agent prompts, or agent-produced files. Amount/value are shown only when explicitly found in source text; missing values are not guessed.
- A local empty SQLite shell is not treated as the source of truth when an encrypted remote snapshot exists. If a snapshot cannot be decrypted or verified, startup fails rather than silently initializing an empty database.

## Required GitHub repository secrets

Set these under **Settings → Secrets and variables → Actions**:

- `TELEGRAM_BOT_TOKEN`
- `BOT_GITHUB_TOKEN`
- `BOT_STATE_KEY`
- `TELEGRAM_ADMIN_IDS`
- `TELEGRAM_ALLOWED_USER_IDS` (optional, if different from admins)

`BOT_GITHUB_TOKEN` must be limited to this repository and have the permissions needed by the controller: **Contents read/write** (encrypted backup branch), **Actions read/write** (workflow runs/artifacts), and **repository Actions secrets read/write** (updating provider/model settings used by workflows). The exact permission labels depend on the token type.

`BOT_STATE_KEY` must be a strong, private, fixed secret. Generate it on a trusted machine, for example with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the result into GitHub Secrets and keep a secure backup. **Do not rotate or delete this key casually**: without it, existing encrypted state and artifacts cannot be decrypted. Never paste it into Telegram, a public issue, or source code.

## Start and check the controller

1. Open **Actions → TradingAgents Engine + Telegram Controller** (the workflow in `.github/workflows/tradingagents.yml`).
2. Choose **Run workflow** to start the Telegram controller job.
3. Confirm the job starts successfully. If startup stops at the encrypted-state check, verify `BOT_STATE_KEY` and the write permissions of `BOT_GITHUB_TOKEN`; do not work around the check by uploading a plaintext database.
4. Provider setup is accepted in the bot's **private chat** only. API-token messages are deleted after receipt. Non-local provider URLs must use HTTPS.

The controller stores encrypted snapshots on `bot-state` during operation; the workflow also creates an encrypted end-of-job backup. **Do not delete the `bot-state` branch or `.bot-state/bot_state.db.enc`** if you want to retain saved models/providers.

## Rollback and recovery

Revert the code PR/commit if the controller has a regression, but preserve the `bot-state` branch and `BOT_STATE_KEY`. Restore code first, then verify that the encrypted database can still be read. Never replace an unreadable encrypted snapshot with a blank SQLite file.

Artifacts uploaded before this change are not retroactively encrypted. Older Actions artifacts may remain available until their existing retention period expires; deleting those artifacts can make older PDF downloads unavailable.
