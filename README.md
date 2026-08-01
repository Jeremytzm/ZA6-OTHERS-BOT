# Telegram Points Bot

Tracks two teams' points across group outings, including bonus points and
shared reflections for outings that had a spiritual/significant impact, plus
a weekly points summary every Sunday.

## How it works

- **Teams**: an admin assigns each member to `team1` or `team2` with `/addmember`.
- **Logging an outing**: an admin runs `/logouting <description>`, taps
  everyone who attended from a checklist, then taps who (if anyone) had a
  spiritual/significant impact.
- **Points**:
  - Attending an outing: **+3 pts** to the person's team, announced in the group.
  - Spiritual/significant impact: **+3 more pts**, also announced.
  - If that person then DMs the bot to share about it, the share is posted to
    the group and they earn **+4 bonus pts**.
- **Weekly summary**: every Sunday, the bot posts both teams' current point
  totals to the group.

## 1. Create the bot

1. Open Telegram, message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, follow the prompts, and copy the token it gives you.
3. (Optional but recommended) send `/setprivacy` to BotFather, select your
   bot, and choose **Disable** — this lets the bot see all group messages,
   which it needs to auto-detect newly added members (see below). Without
   this, it only sees commands and messages that mention it.

## 2. Set up the project

Requires Python 3.10+.

```bash
cd telegram_points_bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and paste in your `BOT_TOKEN`. Adjust `TIMEZONE` /
`SUMMARY_HOUR` if needed (defaults to Sunday 6pm Asia/Singapore time).

## 3. Run it

```bash
python3 bot.py
```

Keep this running continuously (see **Deployment** below for always-on
hosting options). It uses a local `points_bot.db` SQLite file for all
storage — back this file up if points history matters to you.

## 4. Add the bot to your group & set it up

1. Add the bot to your Telegram group like any other member.
2. Make sure the bot is **not** demoted/restricted (it doesn't need to be an
   admin of the group, just a normal member — but see the privacy note above).
3. In the group, run **`/setgroup`** (as a Telegram group admin) — this tells
   the bot where to post outing announcements and the weekly summary.
4. Add members to teams:
   - `/addmember @handle team1` — works immediately if that person has ever
     posted in the group before; otherwise the bot remembers the assignment
     and applies it automatically the next time they post.
   - Or reply to one of their messages with `/addmember team1` — this works
     immediately every time, since Telegram gives the bot their ID directly.
5. Check everyone's in with `/listmembers`.

> **Why the "reply to their message" option exists**: Telegram bots cannot
> look up a user's ID from just their @handle unless that user has already
> interacted with the bot or the bot has seen them post. Replying to their
> message sidesteps this entirely.

## 5. Log an outing

In the group:

```
/logouting Beach cleanup & BBQ
```

The bot posts a checklist — tap each attendee, tap **Done selecting**, then
tap who had a spiritual/significant impact (or just tap **Finish** if none
did). The bot then:

- Posts one message per attendee with their points earned.
- DMs anyone marked as impacted, inviting them to share more (they must have
  started a private chat with the bot at least once — if not, the bot will
  ask them in the group to hit **Start** on it first).
- If they reply in that DM, their reflection is posted to the group and they
  get the bonus points.

Use `/points` anytime for an on-demand team total. The Sunday summary posts
automatically — no action needed.

## Commands reference

| Command | Who | Description |
|---|---|---|
| `/setgroup` | admin | Register this chat for announcements/summary |
| `/addmember @handle team1\|team2` | admin | Add or move a member |
| `/addmember team1\|team2` (as a reply) | admin | Add/move the replied-to user |
| `/removemember @handle` | admin | Remove a member |
| `/listmembers` | anyone | List all members by team |
| `/logouting <description>` | admin | Start the guided outing-logging flow |
| `/points` | anyone | Show current team totals |
| `/help` | anyone | Show command list |

## Deployment (always-on hosting)

Running `python3 bot.py` on your laptop only works while it's open. For
continuous, 24/7 operation, run it on something always-on instead. This repo
is set up to deploy on **Railway**:

1. Push this repo to GitHub (private repo is fine).
2. In Railway: **New Project → Deploy from GitHub repo**, select this repo.
   Railway auto-detects Python and uses `railway.json` to run
   `python3 bot.py` as a background worker (no port needed — it's fine that
   nothing binds to `$PORT`).
3. In the service's **Variables** tab, add:
   - `BOT_TOKEN` — from @BotFather
   - `TIMEZONE` — e.g. `Asia/Singapore`
   - `SUMMARY_HOUR` — e.g. `18`
   - `DB_PATH` — `/data/points_bot.db`
4. In the service's **Settings → Volumes**, add a volume mounted at `/data`.
   This is required — without it, `points_bot.db` lives in the container's
   ephemeral filesystem and gets wiped on every redeploy.
5. Deploy. Every future `git push` to the connected branch auto-redeploys.

Other options work too, if you'd rather not use Railway:

- **A cheap VPS** (e.g. a $5/mo box): install Python, copy the project over,
  run it inside `tmux`/`screen`, or better, as a `systemd` service so it
  restarts automatically on crash/reboot.
- **Render / Fly.io**: same idea as Railway — deploy as a worker/background
  service (not a web service), set the same env vars, and mount a
  persistent disk/volume for `DB_PATH`.
- **A Raspberry Pi** at home works fine too, same as the VPS approach.

Whichever you choose, make sure `points_bot.db` (via `DB_PATH`) is on a
persistent volume (not wiped on redeploy), or you'll lose points history.

## Notes & possible extensions

- Team names default to "Team 1"/"Team 2" — easy to hardcode your own in
  `db.py`'s `TEAM_NAMES` dict, or ask for a `/setteamname` command to be added.
- Currently designed for a single group chat per bot instance (one
  `group_chat_id` stored via `/setgroup`). Running it for multiple separate
  groups would need a small extension to key settings/members per chat.
- All data lives in `points_bot.db` (SQLite) — inspect it anytime with any
  SQLite browser if you want custom reports beyond `/points`.
