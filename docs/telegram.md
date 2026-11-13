# Telegram bot

An operator bot inside the proxy: account status and controls, and alerts when
something needs attention. Set up in the admin UI, **Telegram** tab.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and paste its token into the
   Telegram tab, then **Save**. The proxy checks it (`getMe`) and starts polling.
2. **Link an admin** gives a one-time link `https://t.me/<bot>?start=<code>`
   (valid 10 minutes). Open it in Telegram and press Start: that chat becomes an admin.
   Repeat for every admin; remove them in the same tab.
3. Optional: the public `https://` address of the admin UI (messages get an
   "Open admin UI" button), and a proxy for `api.telegram.org` if the server cannot
   reach it directly.

Settings are kept in `<data_dir>/telegram.json` (mode 0600). Changing the token forgets
the admins (a new bot has new chats). Only one process may poll a token: a second
proxy with the same bot would take the updates from the first.

## Commands (admins, private chat)

| Command | |
|---|---|
| `/status` | accounts: state, tier, 5-hour and weekly usage, cooldowns, last error |
| `/accounts` | the same with buttons: enable/disable, reset limits, check (re-initialise) |
| `/stats` | requests in the last 24 hours: totals, median time, top models/accounts/clients |
| `/events` | the last events |

Anyone else gets "no access". Signing in to an account needs the browser in the admin
UI, so the bot links there instead.

## Alerts

Chosen in the tab; each kind for the same account is sent at most once per 30 minutes.

| Kind | When |
|---|---|
| `logged_out` | an account's session was lost (needs a new sign-in) |
| `abuse` | Google flagged an account; it is switched off automatically |
| `quota` | an account ran out for a model; it cools down until the reset |
| `blocked` | Google answered 429 / "unusual traffic" |
| `all_down` | no account could serve a request |
| `recovered` | an account works again after one of the above |
