# Professional Telegram Giveaway Helper Bot

Run streamlined giveaways with a clear admin workflow. Owners can manage
giveaways while trusted admins can add other admins.

## Features
- Owner-defined admin system with multi-admin support
- Create, list, and close giveaways
- Participants join with a single command
- Random winner selection with optional reroll
- Reward support for redeem codes and mail:pass credentials
- Redeem codes that unlock the stored reward
- SQLite persistence for admins, giveaways, and entries

## Requirements
- Python 3.10+
- A Telegram bot token from BotFather
- Telegram user id for the owner

## Setup
1. Create a virtual environment and install dependencies:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
   - `python -m pip install -r requirements.txt`
2. Copy the example environment file and update values:
   - `cp .env.example .env`
3. Export environment variables or use your preferred method:
   - `export BOT_TOKEN="your_bot_token"`
   - `export OWNER_ID="123456789"`
   - `export DB_PATH="giveaway.db"`
   - `export REQUIRED_CHANNELS="@ownerchannel,-1001234567890"`
   - `export TEMP_BAN_HOURS="24"`
4. Start the bot:
   - `python bot.py`

## Commands
General:
- `/help` - List all commands
- `/listgiveaways` - Recent giveaways
- `/giveaway <id>` - Giveaway details
- `/join <id>` - Join a giveaway
- `/claim <id>` - Claim reward if you won
- `/redeem <id> <code>` - Redeem a code for reward
- `/admins` - List admins

Admin only:
- `/creategiveaway Title | Description`
- `/closegiveaway <id>`
- `/winner <id> [reroll]`
- `/setreward <id> <mailpass|redeemcode|custom> <value>`
- `/reward <id>`
- `/addcode <id> <code>`
- `/addcodes <id> <code1,code2,...>`
- `/codes <id>`
- `/unban <user_id>` (or reply to a user)
- `/addadmin <user_id>` (or reply to a user)
- `/removeadmin <user_id>` (or reply to a user)

## Notes
- The owner id provided in `OWNER_ID` is automatically an admin.
- The database file is created in the working directory (default: `giveaway.db`).
- Rewards and redeem codes are stored in plaintext in SQLite, so keep access restricted.
- Users must remain in the required channel(s). Leaving triggers a temporary ban,
  and repeat violations cause a permanent ban until an admin uses `/unban`.
- For private channels, add the bot to the channel and allow it to read members.