import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

DB_PATH = os.getenv("DB_PATH", "giveaway.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
REQUIRED_CHANNELS = [
    channel.strip()
    for channel in os.getenv("REQUIRED_CHANNELS", "").split(",")
    if channel.strip()
]
TEMP_BAN_HOURS = int(os.getenv("TEMP_BAN_HOURS", "24"))

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


def utc_after(hours: int) -> str:
    safe_hours = max(hours, 0)
    return (
        datetime.now(timezone.utc) + timedelta(hours=safe_hours)
    ).isoformat(timespec="seconds")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_channel_id(value: str) -> str | int:
    trimmed = value.strip()
    if trimmed.lstrip("-").isdigit():
        return int(trimmed)
    if trimmed.startswith("@"):
        return trimmed
    return f"@{trimmed}"


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_status (
                user_id INTEGER PRIMARY KEY,
                strikes INTEGER NOT NULL DEFAULT 0,
                temp_ban_until TEXT,
                perm_ban INTEGER NOT NULL DEFAULT 0,
                ever_member INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeem_codes (
                giveaway_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL,
                claimed_by INTEGER,
                claimed_at TEXT,
                PRIMARY KEY (giveaway_id, code),
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


def parse_redeem_args(args: list[str]) -> tuple[int, str] | None:
    if len(args) < 2:
        return None
    giveaway_id = parse_user_id(args[0])
    if giveaway_id is None:
        return None
    code = " ".join(args[1:]).strip()
    if not code:
        return None
    return giveaway_id, code


def dedupe_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_codes_payload(args: list[str]) -> tuple[int, list[str]] | None:
    if len(args) < 2:
        return None
    giveaway_id = parse_user_id(args[0])
    if giveaway_id is None:
        return None
    payload = " ".join(args[1:]).strip()
    if not payload:
        return None
    raw_codes = [code.strip() for code in payload.split(",")]
    codes = dedupe_values([code for code in raw_codes if code])
    if not codes:
        return None
    return giveaway_id, codes


def reward_label(reward_type: str | None) -> str:
    if reward_type == "mailpass":
        return "Mail:pass"
    if reward_type == "redeemcode":
        return "Redeem code"
    return "Reward"


def reward_description(row: sqlite3.Row) -> str:
    return f"{reward_label(row['reward_type'])}: {row['reward_value']}"


def fetch_redeem_stats(giveaway_id: int) -> tuple[int, int]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN claimed_by IS NOT NULL THEN 1 ELSE 0 END)
                       AS claimed
            FROM redeem_codes
            WHERE giveaway_id = ?;
            """,
            (giveaway_id,),
        ).fetchone()
    total = row["total"] or 0
    claimed = row["claimed"] or 0
    return total, claimed


def user_has_redeemed(giveaway_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM redeem_codes
            WHERE giveaway_id = ? AND claimed_by = ?
            LIMIT 1;
            """,
            (giveaway_id, user_id),
        ).fetchone()
    return row is not None


def get_or_create_user_status(user_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT user_id, strikes, temp_ban_until, perm_ban, ever_member
            FROM user_status
            WHERE user_id = ?;
            """,
            (user_id,),
        ).fetchone()
        if row:
            return row
        conn.execute(
            """
            INSERT INTO user_status
                (user_id, strikes, temp_ban_until, perm_ban, ever_member, updated_at)
            VALUES (?, 0, NULL, 0, 0, ?);
            """,
            (user_id, utc_now()),
        )
        return conn.execute(
            """
            SELECT user_id, strikes, temp_ban_until, perm_ban, ever_member
            FROM user_status
            WHERE user_id = ?;
            """,
            (user_id,),
        ).fetchone()


def mark_user_member(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_status
                (user_id, strikes, temp_ban_until, perm_ban, ever_member, updated_at)
            VALUES (?, 0, NULL, 0, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                ever_member = 1,
                updated_at = excluded.updated_at;
            """,
            (user_id, utc_now()),
        )


def record_violation(user_id: int) -> sqlite3.Row:
    current = get_or_create_user_status(user_id)
    strikes = (current["strikes"] or 0) + 1
    perm_ban = 1 if strikes >= 2 else 0
    temp_ban_until = None if perm_ban else utc_after(TEMP_BAN_HOURS)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_status
                (user_id, strikes, temp_ban_until, perm_ban, ever_member, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                strikes = excluded.strikes,
                temp_ban_until = excluded.temp_ban_until,
                perm_ban = excluded.perm_ban,
                ever_member = excluded.ever_member,
                updated_at = excluded.updated_at;
            """,
            (
                user_id,
                strikes,
                temp_ban_until,
                perm_ban,
                current["ever_member"] or 0,
                utc_now(),
            ),
        )
        return conn.execute(
            """
            SELECT user_id, strikes, temp_ban_until, perm_ban, ever_member
            FROM user_status
            WHERE user_id = ?;
            """,
            (user_id,),
        ).fetchone()


def clear_ban(user_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_status
                (user_id, strikes, temp_ban_until, perm_ban, ever_member, updated_at)
            VALUES (?, 0, NULL, 0, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                strikes = 0,
                temp_ban_until = NULL,
                perm_ban = 0,
                updated_at = excluded.updated_at;
            """,
            (user_id, utc_now()),
        )


def temp_ban_active(row: sqlite3.Row) -> bool:
    ban_until = parse_timestamp(row["temp_ban_until"])
    return ban_until is not None and ban_until > datetime.now(timezone.utc)


def require_admin(update: Update) -> bool:
    user = update.effective_user
    if user is None:
        return False
    return is_admin(user.id)


def format_required_channels(channels: list[str]) -> str:
    if not channels:
        return "the required channel"
    display = ", ".join(channels)
    return display


async def is_member_of_channel(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, channel: str
) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=normalize_channel_id(channel),
            user_id=user_id,
        )
    except TelegramError:
        LOGGER.warning("Failed to check channel %s for user %s", channel, user_id)
        return False
    status = member.status
    if status in {"left", "kicked"}:
        return False
    if status == "restricted" and not getattr(member, "is_member", False):
        return False
    return True


async def check_required_channels(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, list[str]]:
    if not REQUIRED_CHANNELS:
        return True, []
    user = update.effective_user
    if user is None:
        return False, REQUIRED_CHANNELS
    missing: list[str] = []
    for channel in REQUIRED_CHANNELS:
        is_member = await is_member_of_channel(context, user.id, channel)
        if not is_member:
            missing.append(channel)
    return len(missing) == 0, missing


async def enforce_access(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if not REQUIRED_CHANNELS:
        return True
    if is_admin(user.id):
        return True
    status = get_or_create_user_status(user.id)
    if status["perm_ban"]:
        await update.message.reply_text(
            "You are permanently banned from using this bot. "
            "Contact an admin to request unban."
        )
        return False
    if status["temp_ban_until"] and temp_ban_active(status):
        await update.message.reply_text(
            "You are temporarily banned for leaving required channels. "
            f"Try again after {status['temp_ban_until']} UTC."
        )
        return False
    is_member, missing = await check_required_channels(update, context)
    if is_member:
        mark_user_member(user.id)
        return True
    if status["ever_member"]:
        updated = record_violation(user.id)
        if updated["perm_ban"]:
            await update.message.reply_text(
                "You left required channels too many times and are now "
                "permanently banned. Contact an admin to request unban."
            )
            return False
        await update.message.reply_text(
            "You left required channels and are temporarily banned. "
            f"Try again after {updated['temp_ban_until']} UTC. "
            f"Rejoin: {format_required_channels(missing)}"
        )
        return False
    await update.message.reply_text(
        "Please join the required channel(s) to use this bot: "
        f"{format_required_channels(missing)}"
    )
    return False


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
    if not await enforce_access(update, context):
        return
    user = update.effective_user
    name = user.full_name if user else "there"
    await update.message.reply_text(
        f"Hi {name}! I can help you run professional giveaways.\n"
        "Use /help to see commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    await update.message.reply_text(
        "Giveaway bot commands:\n"
        "/help - Show this help message\n"
        "/listgiveaways - List recent giveaways\n"
        "/giveaway <id> - View giveaway details\n"
        "/join <id> - Join an open giveaway\n"
        "/claim <id> - Claim reward if you won\n"
        "/redeem <id> <code> - Redeem a code for a reward\n"
        "/admins - List current admins\n"
        "\nAdmin commands:\n"
        "/creategiveaway Title | Description\n"
        "/closegiveaway <id>\n"
        "/winner <id> [reroll]\n"
        "/setreward <id> <mailpass|redeemcode|custom> <value>\n"
        "/reward <id>\n"
        "/addcode <id> <code>\n"
        "/addcodes <id> <code1,code2,...>\n"
        "/codes <id>\n"
        "/unban <user_id> (or reply to a user)\n"
        "/addadmin <user_id> (or reply to a user)\n"
        "/removeadmin <user_id> (or reply to a user)"
    )


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    redeem_total, redeem_claimed = fetch_redeem_stats(giveaway_id)
    if row["reward_value"]:
        if is_admin_user:
            reward_line = f"Reward: {reward_description(row)}"
        else:
            reward_line = "Reward: Set (ask admin)"
    else:
        reward_line = "Reward: Not set"
    if redeem_total:
        if is_admin_user:
            redeem_line = (
                f"Redeem codes: {redeem_total} total, {redeem_claimed} claimed"
            )
        else:
            redeem_line = "Redeem codes: Required"
    else:
        redeem_line = "Redeem codes: None"
    response = (
        f"Giveaway #{row['id']} - {row['title']}\n"
        f"Status: {status}\n"
        f"Participants: {row['participants']}\n"
        f"Winner: {winner}\n"
        f"{reward_line}\n"
        f"{redeem_line}\n"
        f"Description: {description}"
    )
    if is_winner_user and row["reward_value"]:
        response = f"{response}\nUse /claim {row['id']} to receive your reward."
    await update.message.reply_text(response)


async def join_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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
    if not await enforce_access(update, context):
        return
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


async def add_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    if not require_admin(update):
        await update.message.reply_text("Only admins can add redeem codes.")
        return
    parsed = parse_redeem_args(context.args)
    if parsed is None:
        await update.message.reply_text("Usage: /addcode <id> <code>")
        return
    giveaway_id, code = parsed
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM giveaways WHERE id = ?;",
            (giveaway_id,),
        ).fetchone()
        if row is None:
            await update.message.reply_text("Giveaway not found.")
            return
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO redeem_codes
                (giveaway_id, code, added_by, added_at)
            VALUES (?, ?, ?, ?);
            """,
            (giveaway_id, code, update.effective_user.id, utc_now()),
        )
    if cursor.rowcount == 0:
        await update.message.reply_text("That code already exists.")
        return
    await update.message.reply_text(
        f"Redeem code added for giveaway #{giveaway_id}."
    )


async def add_codes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    if not require_admin(update):
        await update.message.reply_text("Only admins can add redeem codes.")
        return
    parsed = parse_codes_payload(context.args)
    if parsed is None:
        await update.message.reply_text(
            "Usage: /addcodes <id> <code1,code2,...>"
        )
        return
    giveaway_id, codes = parsed
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM giveaways WHERE id = ?;",
            (giveaway_id,),
        ).fetchone()
        if row is None:
            await update.message.reply_text("Giveaway not found.")
            return
        added = 0
        for code in codes:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO redeem_codes
                    (giveaway_id, code, added_by, added_at)
                VALUES (?, ?, ?, ?);
                """,
                (giveaway_id, code, update.effective_user.id, utc_now()),
            )
            if cursor.rowcount:
                added += 1
    await update.message.reply_text(
        f"Added {added} of {len(codes)} redeem codes to giveaway #{giveaway_id}."
    )


async def codes_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    if not require_admin(update):
        await update.message.reply_text("Only admins can view redeem codes.")
        return
    giveaway_id = parse_giveaway_id(context.args)
    if giveaway_id is None:
        await update.message.reply_text("Usage: /codes <id>")
        return
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    total, claimed = fetch_redeem_stats(giveaway_id)
    if total == 0:
        await update.message.reply_text("No redeem codes added yet.")
        return
    available = total - claimed
    await update.message.reply_text(
        f"Redeem codes for giveaway #{giveaway_id}: "
        f"{total} total, {claimed} claimed, {available} available."
    )


async def reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
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


async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    if not require_admin(update):
        await update.message.reply_text("Only admins can unban users.")
        return
    target_id = extract_target_user_id(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Provide a user id or reply to a user to unban."
        )
        return
    clear_ban(target_id)
    await update.message.reply_text(f"User {target_id} has been unbanned.")


async def redeem_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
    parsed = parse_redeem_args(context.args)
    if parsed is None:
        await update.message.reply_text("Usage: /redeem <id> <code>")
        return
    giveaway_id, code = parsed
    row = fetch_giveaway(giveaway_id)
    if row is None:
        await update.message.reply_text("Giveaway not found.")
        return
    if not row["reward_value"]:
        await update.message.reply_text("Reward not set yet.")
        return
    user = update.effective_user
    if user is None:
        await update.message.reply_text("Could not identify user.")
        return
    if user_has_redeemed(giveaway_id, user.id):
        await update.message.reply_text(
            "You already redeemed a code for this giveaway."
        )
        return
    with get_conn() as conn:
        code_row = conn.execute(
            """
            SELECT claimed_by
            FROM redeem_codes
            WHERE giveaway_id = ? AND code = ?;
            """,
            (giveaway_id, code),
        ).fetchone()
        if code_row is None:
            await update.message.reply_text("Invalid redeem code.")
            return
        if code_row["claimed_by"]:
            await update.message.reply_text("This redeem code is already used.")
            return
        conn.execute(
            """
            UPDATE redeem_codes
            SET claimed_by = ?, claimed_at = ?
            WHERE giveaway_id = ? AND code = ?;
            """,
            (user.id, utc_now(), giveaway_id, code),
        )
    chat = update.effective_chat
    is_private = chat is not None and chat.type == "private"
    if is_private:
        await update.message.reply_text(
            f"Reward for giveaway #{row['id']} - {row['title']}:\n"
            f"{reward_description(row)}"
        )
        return
    sent = await send_reward_message(context, user.id, row)
    if sent:
        await update.message.reply_text(
            "Redeem successful. I sent your reward in a private message."
        )
    else:
        await update.message.reply_text(
            "Redeem successful, but I could not DM you. "
            "Start the bot in private chat and use /claim."
        )


async def claim_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await enforce_access(update, context):
        return
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
    has_redeemed = user_has_redeemed(giveaway_id, user.id)
    if row["winner_id"] is None and not has_redeemed:
        await update.message.reply_text("Winner not selected yet.")
        return
    is_winner = row["winner_id"] == user.id if row["winner_id"] else False
    if not (is_winner or has_redeemed):
        await update.message.reply_text(
            "Only the winner or a redeemed-code user can claim this reward."
        )
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
    if not await enforce_access(update, context):
        return
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
    application.add_handler(CommandHandler("redeem", redeem_code))
    application.add_handler(CommandHandler("claim", claim_reward))
    application.add_handler(CommandHandler("closegiveaway", close_giveaway))
    application.add_handler(CommandHandler("setreward", set_reward))
    application.add_handler(CommandHandler("reward", reward_command))
    application.add_handler(CommandHandler("addcode", add_code))
    application.add_handler(CommandHandler("addcodes", add_codes))
    application.add_handler(CommandHandler("codes", codes_status))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("winner", pick_winner))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
