# Creating your Discord bot (needed for Milestone 2+)

Milestone 1 has no live Discord connection yet, but here's the walkthrough
so the token is ready whenever we build it. This has to happen in Discord's
own web UI — nothing here can be automated for you.

## 1. Create the application

1. Go to <https://discord.com/developers/applications> and log in.
2. Click **New Application**, give it a name (e.g. "Trading Assistant"),
   accept the terms, and click **Create**.

## 2. Create the bot user

1. In the left sidebar, click **Bot**.
2. Click **Reset Token** (or it may already show **Add Bot** first — click
   that, then **Reset Token**) to generate a token.
3. Click **Copy** to copy the token. **Treat this like a password** — anyone
   with it can control your bot. Discord only shows it once; if you lose it,
   come back here and reset it again (this invalidates the old one).
4. Paste it into your `.env` file as `DISCORD_BOT_TOKEN=` — never commit
   `.env` to git (it's already in `.gitignore`).

## 3. Turn on the intents the bot will need

Still on the **Bot** page, scroll to **Privileged Gateway Intents** and
enable:

- **Message Content Intent** — needed to read command text
- **Server Members Intent** — optional, only if you later want per-member
  features

## 4. Set the bot's permissions and invite it to your server

1. In the left sidebar, click **OAuth2** → **URL Generator**.
2. Under **Scopes**, check `bot` and `applications.commands`.
3. Under **Bot Permissions**, check at minimum: `Send Messages`,
   `Embed Links`, `Attach Files`, `Read Message History`,
   `Use Slash Commands`. Add more later as features need them — permissions
   are additive, you can always re-invite with more.
4. Copy the generated URL at the bottom, open it in your browser, pick your
   server, and authorize it.

## 5. Get your server (guild) ID

1. In Discord, go to **User Settings → Advanced** and turn on
   **Developer Mode**.
2. Right-click your server's icon → **Copy Server ID**.
3. Paste it into `.env` as `DISCORD_GUILD_ID=` — this lets the bot register
   slash commands instantly to your one server during development, instead
   of waiting up to an hour for global command propagation.

## What's next

Once Milestone 2 (Discord bot skeleton) is approved, the app reads
`DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID` from `.env` automatically — no
code changes needed on your end beyond having those two values set.
