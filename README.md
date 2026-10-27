# Fetch IG

A Telegram bot for Instagram. Every user connects their own Instagram account inside the bot, then can:

- paste an Instagram link (reel, post, story, highlight) and get the media; Reels carry a button for their original audio (needs `ffmpeg` installed for a clean .m4a)
- `/posts`, `/reels`, `/stories` and highlights use the same caption as pasted links. `/posts` and `/reels` read everything unless you add a count (up to 500). Fewer than 10 items are sent normally; 10 or more ask whether to send one by one or as a ZIP (split into parts if over the 50 MB Telegram limit)
- send a username (`x.tahseen`, `@x.tahseen` or an `instagram.com/x.tahseen` link) to open a public profile: posts, reels, stories, highlights and followers/following exports (`/pfp` gives the profile picture)
- watch public accounts from the Watchlist (`/watchlist`): new posts, stories, highlights, profile changes, edits and milestones are sent to the chat
- find accounts by name with `/search <name>`, page through the results and tap one to open it
- every lookup shows the same progress message while it works
- check `/status`, change `/settings`, and delete everything with `/logout`

Commands: `/start` `/help` `/status` `/watchlist` `/settings` `/connect` `/logout` `/cancel` `/search` `/profile` `/pfp` `/posts` `/reels` `/stories` `/highlights` `/followers` `/following`. `/help` lists them with usage.

There is no bridge, web dashboard or realtime connection. The bot only talks to Instagram when you ask it to, or when the watcher checks.

## Connecting an account

`/connect` offers two ways, both inside the chat:

- **Cookie:** paste the `sessionid` cookie from instagram.com. Quick, but Instagram can reject a browser cookie sooner than a full login.
- **Password:** username and password. If Instagram asks for a verification code, send it as the next message. The password is used once and never saved; your message with it is deleted.

Only the resulting session is stored, per Telegram user. Nobody can use anyone else's account, and `/logout` deletes the session, settings and watchlist.

Limits: 10 watched accounts per user (`MAX_WATCH`), one running job per chat, files up to 50 MB. Public accounts only.

## Run

```
pip install -r requirements.txt
cp .env.example .env   # set TELEGRAM_BOT_TOKEN
python main.py
```

Docker: `docker build -t fetch-ig . && docker run --env-file .env -v fetchig-data:/app/data fetch-ig`

Set `MONGO_URI` to keep data on a database instead of files. Instagram flags datacenter IPs and repeated logins, so for a server deployment set `IG_PROXY` to a residential proxy and keep one stable proxy per deployment (all accounts share `IG_PROXY`). Set `IG_READ_TIMEOUT` if large story or audio downloads time out.

## Add a feature

Drop a file in `modules/` with a `BaseModule` subclass. It is loaded for every user automatically. See `modules/status.py` for the smallest example.

