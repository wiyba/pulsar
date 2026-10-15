# pulsar

`STATE_DIRECTORY` used to specify directory for `state.json` with sessions, toggles and credentials, using current by default

`LINKS` used to specify SSE-source of links, using `http://127.0.0.1:8891/links` by default, only `https://pulse.mirea.ru/lessons/visiting-logs/selfapprove?token=<uuid>` is accepted

`/setup <chat> [thread]` sent to the bot creates a message with the latest link in that chat (thread for forum groups), pins it and saves its ids to `state.json` to keep it updated

### startup

```
pulsar config.json
```

### config example
```json
{
  "token": "telegram bot token",
  "users": {
    "me": {
      "telegram": 1,
      "login": "s@edu.mirea.ru",
      "password": "abcdefgh",
      "imap": { "host": "mail.example.org", "user": "me@example.org", "password": "abcdefgh", "folders": ["INBOX"] }
    },
    "someone": { "telegram": 2 }
  }
}
```
