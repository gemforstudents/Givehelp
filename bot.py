import logging
import os
import random
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

DB_PATH = os.getenv("DB_PATH", "giveaway.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

REWARD_TYPE_ALIASES = {
    "mailpass": "mailpass",
    "mail:pass": "mailpass",
    "emailpass": "mailpass",
    "redeemcode": "redeemcode",
    "redeem": "redeemcode",
    "code": "redeemcode",
    "custom": "custom",
    "text": "custom",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table});").fetchall()
    }
    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition};"
        )


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                winner_id INTEGER,
                reward_type TEXT,
                reward_value TEXT,
                reward_set_by INTEGER,
                reward_set_at TEXT,
                closed_at TEXT
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (giveaway_id, user_id),
                FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
            );
            """
        )
        ensure_column(conn, "giveaways", "reward_type", "TEXT")
        ensure_column(conn, "giveaways", "reward_value", "TEXT")
        ensure_column(conn, "giveaways", "reward_set_by", "INTEGER")
        ensure_column(conn, "giveaways", "reward_set_at", "TEXT")


def ensure_owner_admin() -> None:
    if OWNER_ID <= 0:
        raise ValueError("OWNER_ID must be set to a valid Telegram user id.")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO admins (user_id, added_by, added_at)
            VALUES (?, ?, ?);
            """,
            (OWNER_ID, OWNER_ID, utc_now()),
        )


def is_admin(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM admins WHERE user_id = ? LIMIT 1;",
            (user_id,),
        ).fetchone()
    return row is not None


def parse_user_id(text: str) -> int | None:
    if not text:
        return None
    value = text.strip()
    if not value.isdigit():
        return None
    return int(value)


def parse_giveaway_id(args: list[str]) -> int | None:
    if not args:
        return None
    return parse_user_id(args[0])


def normalize_reward_type(value: str) -> str | None:
    if not value:
        return None
    return REWARD_TYPE_ALIASES.get(value.strip().lower())


def parse_reward_args(args: list[str]) -> tuple[int, str, str] | None:
    if len(args) < 3:
        return None
    giveaway_id = parse_user_id(args[0])
    if giveaway_id is None:
        return None
    reward_type = normalize_reward_type(args[1])
    if reward_type is None:
        return None
    reward_value = " ".join(args[2:]).strip()
    if not reward_value:
        return None
    return giveaway_id, reward_type, reward_value


def reward_label(reward_type: str | None) -> str:
    if reward_type == "mailpass":
        return "Mail:pass"
    if reward_type == "redeemcode":
        return "Redeem code"
    return "Reward"


def reward_description(row: sqlite3.Row) -> str:
    return f"{reward_label(row['reward_type'])}: {row['reward_value']}"


def require_admin(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return is_admin(user.id)


def giveaway_overview_row(row: sqlite3.Row) -> str:
    status = row["status"].upper()
    closed_suffix = f" (closed {row['closed_at']})" if row["closed_at"] else ""
    return f"#{row['id']} | {row['title']} | {status}{closed_suffix}"


def fetch_giveaway(giveaway_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT g.*,
                   COUNT(p.user_id) AS participants
            FROM giveaways g
            LEFT JOIN participants p ON p.giveaway_id = g.id
            WHERE g.id = ?
            GROUP BY g.id;
            """,
            (giveaway_id,),
        ).fetchone()
    return row


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.full_name if user else "there"
    await update.message.reply_text(
        f"Hi {name}! I can help you run professional giveaways.\n"
        "Use /help to see commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Giveaway bot commands:\n"
        "/help - Show this help message\n"
        "/listgiveaways - List recent giveaways\n"
        "/giveaway <id> - View giveaway details\n"
        "/join <id> - Join an open giveaway\n"
        "/claim <id> - Claim reward if you won\n"
        "/admins - List current admins\n"
        "\nAdmin commands:\n"
        "/creategiveaway Title | Description\n"
        "/closegiveaway <id>\n"
        "/winner <id> [reroll]\n"
        "/setreward <id> <mailpass|redeemcode|custom> <value>\n"
        "/reward <id>\n"
        "/addadmin <user_id> (or reply to a user)\n"
        "/removeadmin <user_id> (or reply to a user)"
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, added_by, added_at FROM admins ORDER BY added_at ASC;"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No admins found.")
        return
    lines = ["Admins:"]
    for row in rows:
        owner_label = " (owner)" if row["user_id"] == OWNER_ID else ""
        lines.append(
            f"- {row['user_id']}{owner_label} (added {row['added_at']})"
        )
    await update.message.reply_text("\n".join(lines))


def extract_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user.id
    if context.args:
        return parse_user_id(context.args[0])
    return None


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can add other admins.")
        return
    target_id = extract_target_user_id(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Provide a user id or reply to a user to add as admin."
        )
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("The owner is already an admin.")
        return
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO admins (user_id, added_by, added_at)
            VALUES (?, ?, ?);
            """,
            (target_id, update.effective_user.id, utc_now()),
        )
    await update.message.reply_text(f"Added admin: {target_id}")


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can remove admins.")
        return
    target_id = extract_target_user_id(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Provide a user id or reply to a user to remove as admin."
        )
        return
    if target_id == OWNER_ID:
        await update.message.reply_text("The owner cannot be removed.")
        return
    with get_conn() as conn:
        result = conn.execute(
            "DELETE FROM admins WHERE user_id = ?;",
            (target_id,),
        )
    if result.rowcount == 0:
        await update.message.reply_text("That user is not an admin.")
    else:
        await update.message.reply_text(f"Removed admin: {target_id}")


async def create_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can create giveaways.")
        return
    payload = " ".join(context.args).strip()
    if "|" not in payload:
        await update.message.reply_text(
            "Usage: /creategiveaway Title | Description"
        )
        return
    title, description = [part.strip() for part in payload.split("|", 1)]
    if not title:
        await update.message.reply_text("Title cannot be empty.")
        return
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO giveaways
                (title, description, created_by, created_at, status)
            VALUES (?, ?, ?, ?, 'open');
            """,
            (title, description, update.effective_user.id, utc_now()),
        )
        giveaway_id = cursor.lastrowid
    await update.message.reply_text(
        f"Giveaway created: #{giveaway_id}\n"
        f"Title: {title}\n"
        f"Share /join {giveaway_id} to let users join."
    )


async def list_giveaways(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, closed_at
            FROM giveaways
            ORDER BY id DESC
            LIMIT 20;
            """
        ).fetchall()
    if not rows:
        await update.message.reply_text("No giveaways yet.")
        return
    lines = ["Recent giveaways:"]
    lines.extend(giveaway_overview_row(row) for row in rows)
    await update.message.reply_text("\n".join(lines))


async def giveaway_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /giveaway <id>")
        return
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    user = update.effective_user
    user_id = user.id if user else None
    is_admin_user = user_id is not None and is_admin(user_id)
    is_winner_user = user_id is not None and row["winner_id"] == user_id
    status = row["status"].upper()
    winner = str(row["winner_id"]) if row["winner_id"] else "Not selected"
    description = row["description"] or "No description."
    if row["reward_value"]:
        if is_admin_user:
            reward_line = f"Reward: {reward_description(row)}"
        else:
            reward_line = "Reward: Set (ask admin)"
    else:
        reward_line = "Reward: Not set"
    response = (
        f"Giveaway #{row['id']} - {row['title']}\n"
        f"Status: {status}\n"
        f"Participants: {row['participants']}\n"
        f"Winner: {winner}\n"
        f"{reward_line}\n"
        f"Description: {description}"
    )
    if is_winner_user and row["reward_value"]:
        response = f"{response}\nUse /claim {row['id']} to receive your reward."
    await update.message.reply_text(response)


async def join_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /join <id>")
        return
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    if row["status"] != "open":
        await update.message.reply_text("That giveaway is closed.")
        return
    user = update.effective_user
    if user is None:
        await update.message.reply_text("Could not identify user.")
        return
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO participants (giveaway_id, user_id, joined_at)
                VALUES (?, ?, ?);
                """,
                (giveaway_id, user.id, utc_now()),
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text("You already joined this giveaway.")
            return
    await update.message.reply_text(
        f"You joined giveaway #{giveaway_id}. Good luck!"
    )


async def close_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can close giveaways.")
        return
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /closegiveaway <id>")
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM giveaways WHERE id = ?;",
            (giveaway_id,),
        ).fetchone()
        if row is None:
            await update.message.reply_text("Giveaway not found.")
            return
        if row["status"] == "closed":
            await update.message.reply_text("That giveaway is already closed.")
            return
        conn.execute(
            """
            UPDATE giveaways
            SET status = 'closed', closed_at = ?
            WHERE id = ?;
            """,
            (utc_now(), giveaway_id),
        )
    await update.message.reply_text(f"Giveaway #{giveaway_id} closed.")


async def set_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can set rewards.")
        return
    parsed = parse_reward_args(context.args)
    if parsed is None:
        await update.message.reply_text(
            "Usage: /setreward <id> <mailpass|redeemcode|custom> <value>"
        )
        return
    giveaway_id, reward_type, reward_value = parsed
    if reward_type == "mailpass" and ":" not in reward_value:
        await update.message.reply_text(
            "Mail:pass should look like email:password."
        )
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM giveaways WHERE id = ?;",
            (giveaway_id,),
        ).fetchone()
        if row is None:
            await update.message.reply_text("Giveaway not found.")
            return
        conn.execute(
            """
            UPDATE giveaways
            SET reward_type = ?,
                reward_value = ?,
                reward_set_by = ?,
                reward_set_at = ?
            WHERE id = ?;
            """,
            (
                reward_type,
                reward_value,
                update.effective_user.id,
                utc_now(),
                giveaway_id,
            ),
        )
    await update.message.reply_text(
        f"Reward set for giveaway #{giveaway_id}: {reward_label(reward_type)}"
    )


async def reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can view rewards.")
        return
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /reward <id>")
        return
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    if not row["reward_value"]:
        await update.message.reply_text("No reward set for this giveaway.")
        return
    lines = [
        f"Giveaway #{row['id']} reward:",
        reward_description(row),
    ]
    if row["reward_set_by"] and row["reward_set_at"]:
        lines.append(
            f"Set by {row['reward_set_by']} on {row['reward_set_at']}"
        )
    await update.message.reply_text("\n".join(lines))


async def claim_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /claim <id>")
        return
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    user = update.effective_user
    if user is None:
        await update.message.reply_text("Could not identify user.")
        return
    if row["winner_id"] is None:
        await update.message.reply_text("Winner not selected yet.")
        return
    if row["winner_id"] != user.id:
        await update.message.reply_text("Only the winner can claim this reward.")
        return
    if not row["reward_value"]:
        await update.message.reply_text("Reward not set yet.")
        return
    chat = update.effective_chat
    is_private = chat is not None and chat.type == "private"
    reward_message = (
        f"Reward for giveaway #{row['id']} - {row['title']}:\n"
        f"{reward_description(row)}"
    )
    if is_private:
        await update.message.reply_text(reward_message)
        return
    sent = await send_reward_message(context, user.id, row)
    if sent:
        await update.message.reply_text(
            "I sent your reward in a private message."
        )
    else:
        await update.message.reply_text(
            "I could not send you a private message. Start the bot in a "
            "private chat and try /claim again."
        )


async def send_reward_message(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, row: sqlite3.Row
) -> bool:
    message = (
        f"Reward for giveaway #{row['id']} - {row['title']}:\n"
        f"{reward_description(row)}"
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=message)
        return True
    except TelegramError:
        LOGGER.warning("Failed to send reward to user %s", user_id)
        return False


async def pick_winner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not require_admin(update):
        await update.message.reply_text("Only admins can pick winners.")
        return
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /winner <id> [reroll]")
        return
    reroll = len(context.args) > 1 and context.args[1].lower() == "reroll"
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    if row["winner_id"] and not reroll:
        await update.message.reply_text(
            f"Winner already selected: {row['winner_id']}. "
            "Add 'reroll' to pick again."
        )
        return
    with get_conn() as conn:
        participants = conn.execute(
            "SELECT user_id FROM participants WHERE giveaway_id = ?;",
            (giveaway_id,),
        ).fetchall()
    if not participants:
        await update.message.reply_text("No participants yet.")
        return
    winner_id = random.choice([row["user_id"] for row in participants])
    with get_conn() as conn:
        conn.execute(
            "UPDATE giveaways SET winner_id = ? WHERE id = ?;",
            (winner_id, giveaway_id),
        )
    reward_note = " Reward not set."
    if row["reward_value"]:
        sent = await send_reward_message(context, winner_id, row)
        if sent:
            reward_note = " Reward sent via private message."
        else:
            reward_note = (
                " Could not DM reward. Ask the winner to /claim."
            )
    await update.message.reply_text(
        f"Winner for giveaway #{giveaway_id}: {winner_id}.{reward_note}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled error: %s", context.error)
    if isinstance(update, Update) and update.message:
        await update.message.reply_text(
            "Something went wrong. Please try again later."
        )


def build_application() -> Application:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN must be set.")
    init_db()
    ensure_owner_admin()
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admins", admins_command))
    application.add_handler(CommandHandler("addadmin", add_admin))
    application.add_handler(CommandHandler("removeadmin", remove_admin))
    application.add_handler(CommandHandler("creategiveaway", create_giveaway))
    application.add_handler(CommandHandler("listgiveaways", list_giveaways))
    application.add_handler(CommandHandler("giveaway", giveaway_info))
    application.add_handler(CommandHandler("join", join_giveaway))
    application.add_handler(CommandHandler("claim", claim_reward))
    application.add_handler(CommandHandler("closegiveaway", close_giveaway))
    application.add_handler(CommandHandler("setreward", set_reward))
    application.add_handler(CommandHandler("reward", reward_command))
    application.add_handler(CommandHandler("winner", pick_winner))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
