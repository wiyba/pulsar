# pulsar

`STATE_DIRECTORY` used to specify directory for `state.json` with sessions, toggles and credentials, using current by default

`LINKS` used to specify SSE-source of links, using `http://127.0.0.1:8891/links` by default

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
