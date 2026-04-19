# WO-45 — Messaging Integrations

## Goal

Push observation feed updates to external messaging platforms (Telegram, Discord, Slack) so the user can monitor what their coding agent is doing without watching the web UI.

## Background

When a long extraction or import job is running, or when an agent is working in the background, the user may want notifications on key events — new decisions, failures, completed TODOs, or session summaries. Currently the only way to see this is the web UI.

## Deliverables

### 1. Notification framework

Create a plugin-based notification system in `src/callmem/notifications/`:

```python
# base.py
class NotificationChannel(ABC):
    @abstractmethod
    async def send(self, message: str, level: str = "info") -> bool: ...
    
    @abstractmethod
    async def send_entity(self, entity: Entity) -> bool: ...
    
    @abstractmethod
    async def send_session_summary(self, session: Session, summary: str) -> bool: ...
```

### 2. Telegram integration

```python
# telegram.py
class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str): ...
```

Config:
```toml
[notifications.telegram]
enabled = true
bot_token = "..."       # or env var: CALLMEM_TELEGRAM_TOKEN
chat_id = "..."         # or env var: CALLMEM_TELEGRAM_CHAT_ID
notify_on = ["failure", "decision", "session_end"]  # which events trigger notifications
```

### 3. Discord integration

```python
# discord.py
class DiscordChannel(NotificationChannel):
    def __init__(self, webhook_url: str): ...
```

Uses Discord webhook (no bot required). Config:
```toml
[notifications.discord]
enabled = true
webhook_url = "..."     # or env var: CALLMEM_DISCORD_WEBHOOK
notify_on = ["failure", "decision", "session_end"]
```

### 4. Slack integration

```python
# slack.py
class SlackChannel(NotificationChannel):
    def __init__(self, webhook_url: str): ...
```

Uses Slack incoming webhook. Config:
```toml
[notifications.slack]
enabled = true
webhook_url = "..."     # or env var: CALLMEM_SLACK_WEBHOOK
notify_on = ["failure", "decision", "session_end"]
```

### 5. Event hooks

Wire notifications into the existing event pipeline:
- After entity extraction: if entity type is in `notify_on`, send notification
- After session end: if "session_end" in `notify_on`, send session summary
- On failure entities: always notify if the channel is enabled (opt-out, not opt-in)

### 6. Message formatting

Format messages for each platform:
- **Telegram**: Markdown with entity type emoji, title, and truncated content
- **Discord**: Embed with colour-coded entity type, fields for title/content/project
- **Slack**: Block Kit with section and context blocks

### 7. Setup wizard

```
── Notifications (optional) ──

  Send notifications for key events?
    1) Telegram
    2) Discord
    3) Slack
    4) Skip
  Choice [default: 4]:
```

If selected, prompt for the relevant token/webhook/chat_id.

### 8. CLI test

```bash
callmem notify --test -p .    # send a test message to all configured channels
```

## Constraints

- Python 3.10 compatible
- No AI attribution
- All messaging deps are optional (`pip install callmem[telegram]`, etc.) — use `httpx` for HTTP calls (already a dependency) rather than platform-specific SDKs
- Notification failures must not crash the daemon — log and continue
- Rate limiting: max 1 notification per 30 seconds per channel (debounce rapid entity creation)
- Sensitive data: notifications should respect the vault — never send encrypted/redacted content to messaging platforms

## Acceptance criteria

- [ ] Telegram notifications work with bot token + chat ID
- [ ] Discord notifications work with webhook URL
- [ ] Slack notifications work with webhook URL
- [ ] `notify_on` config controls which entity types trigger notifications
- [ ] Session end summaries sent when configured
- [ ] `--test` command sends test message
- [ ] Notification failures don't crash the daemon
- [ ] Rate limiting prevents spam during bulk extraction
- [ ] Sensitive data is not leaked to messaging platforms
- [ ] All existing tests pass
