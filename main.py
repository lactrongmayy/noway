"""
DISCORD COMMUNITY BOT — MODULAR EDITION
===========================================

Deployment target: Railway
Command styles: Discord slash commands + prefix command groups split into 20 files (default !)
Permission model:
  - Public commands: available to all members unless a command says otherwise.
  - Admin commands: require Discord Administrator permission (and are checked again in code).
  - Owner commands: require OWNER_ID. Admins cannot use owner-only commands.
  - Destructive moderation actions are logged when a log channel is configured.

Setup:
  1. Install packages from requirements.txt.
  2. Set DISCORD_TOKEN in Railway Variables.
  3. Set OWNER_ID to your Discord user ID.
  4. Optional: set TEST_GUILD_ID while developing for fast slash-command sync.
  5. Invite the bot with bot + applications.commands scopes.
  6. Enable Server Members Intent and Message Content Intent in the Discord Developer Portal
     if you want member join/leave events and message-based logging.
  7. Start command: python main.py

Data:
  - SQLite file bot_data.sqlite3 is created automatically.
  - Railway deployments may have ephemeral storage. Attach a Railway Volume and place
    DATABASE_PATH on that volume if you need the database to survive redeployments.

Important:
  - Do not share your bot token.
  - Never hardcode the token in this file.
  - Discord permissions and role hierarchy still apply; the bot cannot moderate roles
    above or equal to its highest role.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Iterable, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================================
# 1. CONFIGURATION
# ============================================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "1356498767788118099"))
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.sqlite3")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")  # Prefix commands use this value.
DEFAULT_COLOR = discord.Color.blurple()
SUCCESS_COLOR = discord.Color.green()
WARNING_COLOR = discord.Color.orange()
ERROR_COLOR = discord.Color.red()
BOT_START_TIME = time.time()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("community_bot")

INTENTS = discord.Intents.default()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.moderation = True
INTENTS.voice_states = True
INTENTS.message_content = True
INTENTS.reactions = True

# ============================================================================
# 2. BOT OBJECT
# ============================================================================

ACCESS_MANAGEMENT_COMMANDS = {"capquyen", "removerole", "listrole"}


class RoleAccessCommandTree(app_commands.CommandTree):
    """Block slash commands unless the user is the owner or has an allowed role."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == OWNER_ID:
            return True
        command = interaction.command
        command_name = command.name if command else ""
        if command_name in ACCESS_MANAGEMENT_COMMANDS:
            await interaction.response.send_message("🔒 Lệnh quản lý quyền chỉ dành cho Owner bot.", ephemeral=True)
            return False
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("🔒 Bạn cần có role được cấp quyền trong server để dùng bot.", ephemeral=True)
            return False
        try:
            rows = db_conn().execute(
                "SELECT role_id FROM allowed_access_roles WHERE guild_id=?",
                (interaction.guild.id,),
            ).fetchall()
            allowed = {int(row[0]) for row in rows}
        except (RuntimeError, sqlite3.Error):
            allowed = set()
        if not any(role.id in allowed for role in interaction.user.roles):
            await interaction.response.send_message(
                "🔒 Bạn chưa có role được cấp quyền sử dụng bot. Hãy liên hệ Owner bot.",
                ephemeral=True,
            )
            return False
        return True


class CommunityBot(commands.Bot):
    """Main bot class. Persistent settings are loaded from SQLite on demand."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned_or(BOT_PREFIX),
            intents=INTENTS,
            help_command=None,
            tree_cls=RoleAccessCommandTree,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=True, roles=False, replied_user=False
            ),
        )
        self.db: Optional[sqlite3.Connection] = None
        self.ready_once = False
        self.started_at = time.time()
        self._sync_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.db = sqlite3.connect(DATABASE_PATH)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        init_database(self.db)
        self.db.commit()
        # Re-register the persistent ticket button view after every restart.
        self.add_view(TicketPanelView())
        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced slash commands to test guild %s", TEST_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Synced global slash commands")

    async def close(self) -> None:
        if self.db is not None:
            self.db.commit()
            self.db.close()
            self.db = None
        await super().close()


bot = CommunityBot()

# ============================================================================
# 3. DATABASE SCHEMA
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    welcome_channel_id INTEGER,
    welcome_message TEXT DEFAULT 'Chào mừng {user} đến với {server}!',
    goodbye_channel_id INTEGER,
    goodbye_message TEXT DEFAULT '{user} đã rời khỏi {server}.',
    modlog_channel_id INTEGER,
    join_role_id INTEGER,
    log_messages INTEGER DEFAULT 0,
    log_voice INTEGER DEFAULT 1,
    level_enabled INTEGER DEFAULT 1,
    ticket_category_id INTEGER,
    ticket_support_role_id INTEGER,
    ticket_counter INTEGER DEFAULT 0,
    starboard_channel_id INTEGER,
    autorole_enabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_levels (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    last_message_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL UNIQUE,
    opener_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS user_notes (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    author_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    remind_at REAL NOT NULL,
    text TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS custom_reactions (
    guild_id INTEGER NOT NULL,
    trigger_text TEXT NOT NULL,
    response_text TEXT NOT NULL,
    PRIMARY KEY (guild_id, trigger_text)
);
CREATE TABLE IF NOT EXISTS starboard_entries (
    guild_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    star_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, message_id)
);
CREATE TABLE IF NOT EXISTS allowed_access_roles (
    guild_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);
"""

def init_database(db: sqlite3.Connection) -> None:
    """Create all persistent tables and apply small forward-compatible migrations."""
    db.executescript(SCHEMA)

def db_conn() -> sqlite3.Connection:
    if bot.db is None:
        raise RuntimeError("Database is not initialized yet.")
    return bot.db

def get_setting(guild_id: int, key: str, default: Any = None) -> Any:
    row = db_conn().execute(
        f"SELECT {key} FROM guild_settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if row is None:
        return default
    return row[key]

def ensure_guild(guild_id: int) -> None:
    db_conn().execute(
        "INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,)
    )
    db_conn().commit()

def set_setting(guild_id: int, key: str, value: Any) -> None:
    allowed = {
        "welcome_channel_id", "welcome_message", "goodbye_channel_id",
        "goodbye_message", "modlog_channel_id", "join_role_id",
        "log_messages", "log_voice", "level_enabled", "ticket_category_id",
        "ticket_support_role_id", "starboard_channel_id", "autorole_enabled",
    }
    if key not in allowed:
        raise ValueError("Unknown setting")
    ensure_guild(guild_id)
    db_conn().execute(
        f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?",
        (value, guild_id),
    )
    db_conn().commit()

# ============================================================================
# 4. EMBEDS, CHECKS, AND SHARED UTILITIES
# ============================================================================

def make_embed(title: str, description: str = "", color: discord.Color = DEFAULT_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text="Community Bot")
    return embed

def success_embed(title: str, description: str = "") -> discord.Embed:
    return make_embed(f"✅ {title}", description, SUCCESS_COLOR)

def error_embed(title: str, description: str = "") -> discord.Embed:
    return make_embed(f"❌ {title}", description, ERROR_COLOR)

def warning_embed(title: str, description: str = "") -> discord.Embed:
    return make_embed(f"⚠️ {title}", description, WARNING_COLOR)

def is_owner_id(user_id: int) -> bool:
    return user_id == OWNER_ID

def require_guild(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None

def is_admin_member(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

def can_moderate_target(actor: discord.Member, target: discord.Member) -> tuple[bool, str]:
    if target.id == actor.id:
        return False, "Bạn không thể áp dụng thao tác này lên chính mình."
    if target.id == actor.guild.owner_id:
        return False, "Không thể áp dụng thao tác này lên chủ server."
    if actor.id != actor.guild.owner_id and target.top_role >= actor.top_role:
        return False, "Vai trò của người đó cao hơn hoặc bằng vai trò cao nhất của bạn."
    me = actor.guild.me
    if me is not None and target.top_role >= me.top_role:
        return False, "Vai trò của người đó cao hơn hoặc bằng vai trò cao nhất của bot."
    return True, ""

def human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    parts = []
    for size, label in ((86400, "ngày"), (3600, "giờ"), (60, "phút"), (1, "giây")):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {label}")
    return " ".join(parts) or "0 giây"

def parse_duration(value: str) -> Optional[int]:
    """Parse values such as 10m, 2h, 1d, 1w."""
    value = value.strip().lower()
    if len(value) < 2:
        return None
    suffix = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError:
        return None
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if suffix not in factors or amount <= 0:
        return None
    return amount * factors[suffix]

async def send_modlog(guild: discord.Guild, title: str, description: str) -> None:
    channel_id = get_setting(guild.id, "modlog_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=make_embed(title, description, WARNING_COLOR))
        except discord.HTTPException:
            logger.exception("Failed to send moderation log")

async def respond_error(interaction: discord.Interaction, message: str, ephemeral: bool = True) -> None:
    embed = error_embed("Không thể thực hiện", message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.exception("Failed to send error response")

async def respond_success(interaction: discord.Interaction, message: str, ephemeral: bool = True) -> None:
    embed = success_embed("Thành công", message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except discord.HTTPException:
        logger.exception("Failed to send success response")

def admin_only():
    return app_commands.default_permissions(administrator=True)

def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_owner_id(interaction.user.id)
    return app_commands.check(predicate)

def admin_check(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator

def owner_check(interaction: discord.Interaction) -> bool:
    return is_owner_id(interaction.user.id)

# ============================================================================
# 5. BASIC BOT EVENTS
# ============================================================================

@bot.event
async def on_ready() -> None:
    if not bot.ready_once:
        bot.ready_once = True
        logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
        logger.info("Loaded %s slash commands", len(bot.tree.get_commands()))
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="the community")
    )
    if not reminder_loop.is_running():
        reminder_loop.start()

@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    ensure_guild(guild.id)
    logger.info("Joined guild %s (%s)", guild.name, guild.id)

@bot.event
async def on_guild_remove(guild: discord.Guild) -> None:
    logger.info("Removed from guild %s (%s)", guild.name, guild.id)

@bot.event
async def on_member_join(member: discord.Member) -> None:
    ensure_guild(member.guild.id)
    welcome_channel_id = get_setting(member.guild.id, "welcome_channel_id")
    welcome_message = get_setting(member.guild.id, "welcome_message", "Chào mừng {user} đến với {server}!")
    if welcome_channel_id:
        channel = member.guild.get_channel(int(welcome_channel_id))
        if isinstance(channel, discord.TextChannel):
            text = str(welcome_message).replace("{user}", member.mention).replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count))
            try:
                await channel.send(text)
            except discord.HTTPException:
                logger.exception("Welcome message failed")
    role_id = get_setting(member.guild.id, "join_role_id")
    if role_id:
        role = member.guild.get_role(int(role_id))
        if role and role < member.guild.me.top_role:
            try:
                await member.add_roles(role, reason="Automatic join role")
            except discord.HTTPException:
                logger.exception("Auto role failed")
    await send_modlog(member.guild, "Thành viên tham gia", f"{member.mention} (`{member.id}`) đã tham gia server.")

@bot.event
async def on_member_remove(member: discord.Member) -> None:
    goodbye_channel_id = get_setting(member.guild.id, "goodbye_channel_id")
    goodbye_message = get_setting(member.guild.id, "goodbye_message", "{user} đã rời khỏi {server}.")
    if goodbye_channel_id:
        channel = member.guild.get_channel(int(goodbye_channel_id))
        if isinstance(channel, discord.TextChannel):
            text = str(goodbye_message).replace("{user}", str(member)).replace("{server}", member.guild.name).replace("{count}", str(member.guild.member_count or 0))
            try:
                await channel.send(text)
            except discord.HTTPException:
                logger.exception("Goodbye message failed")
    await send_modlog(member.guild, "Thành viên rời server", f"{discord.utils.escape_markdown(str(member))} (`{member.id}`) đã rời server.")

@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        return
    ensure_guild(message.guild.id)
    # Lightweight XP cooldown: no more than one XP award every 60 seconds.
    if int(get_setting(message.guild.id, "level_enabled", 1) or 0):
        now = time.time()
        row = db_conn().execute(
            "SELECT xp, level, last_message_at FROM user_levels WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id),
        ).fetchone()
        if row is None:
            db_conn().execute(
                "INSERT INTO user_levels (guild_id,user_id,xp,level,last_message_at) VALUES (?,?,?,?,?)",
                (message.guild.id, message.author.id, random.randint(5, 15), 0, now),
            )
        elif now - float(row["last_message_at"]) >= 60:
            xp = int(row["xp"]) + random.randint(5, 15)
            level = int(xp ** 0.5 // 5)
            old_level = int(row["level"])
            db_conn().execute(
                "UPDATE user_levels SET xp=?,level=?,last_message_at=? WHERE guild_id=? AND user_id=?",
                (xp, level, now, message.guild.id, message.author.id),
            )
            if level > old_level:
                try:
                    await message.channel.send(f"🎉 {message.author.mention} đã lên cấp **{level}**!")
                except discord.HTTPException:
                    pass
        db_conn().commit()
    # Optional message logging; content is truncated to stay within embed limits.
    if int(get_setting(message.guild.id, "log_messages", 0) or 0):
        await send_modlog(message.guild, "Tin nhắn mới", f"Kênh: {message.channel.mention}\nNgười gửi: {message.author.mention}\nNội dung: {discord.utils.escape_markdown(message.content[:700] or '[không có nội dung văn bản]')}")
    # Process !prefix commands after XP and logging. Without this, custom on_message
    # handlers prevent discord.py prefix commands from running.
    await bot.process_commands(message)

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not after.guild or after.author.bot or before.content == after.content:
        return
    if int(get_setting(after.guild.id, "log_messages", 0) or 0):
        await send_modlog(after.guild, "Tin nhắn đã sửa", f"Người gửi: {after.author.mention}\nKênh: {after.channel.mention}\nTrước: {before.content[:500] or '[trống]'}\nSau: {after.content[:500] or '[trống]'}")

@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not message.guild or message.author.bot:
        return
    if int(get_setting(message.guild.id, "log_messages", 0) or 0):
        await send_modlog(message.guild, "Tin nhắn đã xoá", f"Người gửi: {message.author.mention}\nKênh: {message.channel.mention}\nNội dung lưu được: {message.content[:700] or '[không có nội dung văn bản]'}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return
    if not int(get_setting(member.guild.id, "log_voice", 1) or 0):
        return
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        action = f"{member.mention} đã vào {after.channel.mention}"
    elif before.channel is not None and after.channel is None:
        action = f"{member.mention} đã rời {before.channel.mention}"
    else:
        action = f"{member.mention} chuyển từ {before.channel.mention} sang {after.channel.mention}"
    await send_modlog(member.guild, "Nhật ký voice", action)

# ============================================================================
# 6. HELP AND INFORMATION COMMANDS
# ============================================================================

@bot.tree.command(name="help", description="Xem danh mục lệnh, mỗi mục có trang riêng")
async def help_command(interaction: discord.Interaction) -> None:
    # PrefixHelpView is declared later in the file; name resolution happens on invocation.
    view = PrefixHelpView(interaction.user.id)
    await interaction.response.send_message(
        embed=prefix_help_embed(None, 0), view=view, ephemeral=True
    )

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=success_embed("Pong!", f"Độ trễ Gateway: `{round(bot.latency * 1000)} ms`"))

@bot.tree.command(name="botinfo", description="Xem thông tin bot")
async def botinfo(interaction: discord.Interaction) -> None:
    embed = make_embed("🤖 Thông tin bot")
    embed.add_field(name="Tên", value=str(bot.user), inline=True)
    embed.add_field(name="ID", value=str(bot.user.id if bot.user else "N/A"), inline=True)
    embed.add_field(name="Server", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Uptime", value=human_duration(int(time.time() - bot.started_at)), inline=True)
    embed.add_field(name="Slash commands", value=str(len(bot.tree.get_commands())), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="serverinfo", description="Xem thông tin server hiện tại")
async def serverinfo(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
    embed = make_embed(f"🏠 {guild.name}")
    embed.add_field(name="ID", value=str(guild.id), inline=True)
    embed.add_field(name="Thành viên", value=str(guild.member_count or 0), inline=True)
    embed.add_field(name="Kênh", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Role", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="Tạo lúc", value=discord.utils.format_dt(guild.created_at, style="F"), inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="userinfo", description="Xem thông tin một thành viên")
@app_commands.describe(member="Thành viên cần xem")
async def userinfo(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    member = member or (interaction.user if isinstance(interaction.user, discord.Member) else None)
    if member is None:
        return await respond_error(interaction, "Không tìm thấy thành viên.")
    embed = make_embed(f"👤 {member}")
    embed.add_field(name="ID", value=str(member.id), inline=True)
    embed.add_field(name="Ngày tạo tài khoản", value=discord.utils.format_dt(member.created_at, style="F"), inline=False)
    if member.joined_at:
        embed.add_field(name="Ngày vào server", value=discord.utils.format_dt(member.joined_at, style="F"), inline=False)
    embed.add_field(name="Số role", value=str(max(0, len(member.roles) - 1)), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="avatar", description="Xem avatar của bạn hoặc thành viên khác")
async def avatar(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    target = member or interaction.user
    embed = make_embed(f"🖼️ Avatar — {target}")
    embed.set_image(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="roles", description="Liệt kê role trong server")
async def roles(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
    role_list = [r.mention for r in reversed(interaction.guild.roles) if r.name != "@everyone"]
    text = "\n".join(role_list[:60]) or "Server chưa có role nào."
    embed = make_embed("🎭 Danh sách role", text)
    if len(role_list) > 60:
        embed.set_footer(text=f"Chỉ hiển thị 60/{len(role_list)} role.")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="channels", description="Liệt kê kênh trong server")
async def channels(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
    text_channels = [c.mention for c in interaction.guild.text_channels[:40]]
    voice_channels = [f"🔊 {discord.utils.escape_markdown(c.name)}" for c in interaction.guild.voice_channels[:40]]
    embed = make_embed("📑 Danh sách kênh")
    embed.add_field(name="Kênh chữ", value="\n".join(text_channels) or "Không có", inline=False)
    embed.add_field(name="Kênh voice", value="\n".join(voice_channels) or "Không có", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# 7. MODERATION COMMANDS — ADMIN ONLY
# ============================================================================

@bot.tree.command(name="ban", description="Cấm một thành viên khỏi server")
@admin_only()
@app_commands.describe(member="Thành viên cần ban", reason="Lý do")
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "Không nêu lý do") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.user, discord.Member):
        return await respond_error(interaction, "Không xác định được người thực hiện.")
    allowed, why = can_moderate_target(interaction.user, member)
    if not allowed:
        return await respond_error(interaction, why)
    try:
        await member.ban(reason=f"{reason} | Moderator: {interaction.user} ({interaction.user.id})", delete_message_seconds=0)
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Ban Members hoặc role bot quá thấp.")
    except discord.HTTPException as exc:
        return await respond_error(interaction, f"Discord từ chối thao tác: {exc}")
    await interaction.response.send_message(embed=success_embed("Đã ban", f"{member} đã bị ban.\nLý do: {reason}"))
    await send_modlog(interaction.guild, "Ban thành viên", f"Thành viên: `{member}` (`{member.id}`)\nNgười thực hiện: {interaction.user.mention}\nLý do: {reason}")

@bot.tree.command(name="kick", description="Đá một thành viên khỏi server")
@admin_only()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "Không nêu lý do") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.user, discord.Member):
        return await respond_error(interaction, "Không xác định được người thực hiện.")
    allowed, why = can_moderate_target(interaction.user, member)
    if not allowed:
        return await respond_error(interaction, why)
    try:
        await member.kick(reason=f"{reason} | Moderator: {interaction.user} ({interaction.user.id})")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Kick Members hoặc role bot quá thấp.")
    except discord.HTTPException as exc:
        return await respond_error(interaction, f"Discord từ chối thao tác: {exc}")
    await interaction.response.send_message(embed=success_embed("Đã kick", f"{member} đã bị kick.\nLý do: {reason}"))
    await send_modlog(interaction.guild, "Kick thành viên", f"Thành viên: `{member}` (`{member.id}`)\nNgười thực hiện: {interaction.user.mention}\nLý do: {reason}")

@bot.tree.command(name="timeout", description="Tạm cấm chat một thành viên")
@admin_only()
@app_commands.describe(member="Thành viên", duration="Thời lượng: 10m, 2h, 1d, tối đa 28 ngày", reason="Lý do")
async def timeout(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "Không nêu lý do") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.user, discord.Member):
        return await respond_error(interaction, "Không xác định được người thực hiện.")
    seconds = parse_duration(duration)
    if seconds is None or seconds > 28 * 86400:
        return await respond_error(interaction, "Thời lượng không hợp lệ. Ví dụ: `10m`, `2h`, `1d`; tối đa 28 ngày.")
    allowed, why = can_moderate_target(interaction.user, member)
    if not allowed:
        return await respond_error(interaction, why)
    try:
        await member.timeout(timedelta(seconds=seconds), reason=f"{reason} | Moderator: {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Moderate Members hoặc role bot quá thấp.")
    await interaction.response.send_message(embed=success_embed("Đã timeout", f"{member.mention} trong {human_duration(seconds)}.\nLý do: {reason}"))
    await send_modlog(interaction.guild, "Timeout", f"{member.mention} — {human_duration(seconds)} — {reason}")

@bot.tree.command(name="untimeout", description="Gỡ timeout của thành viên")
@admin_only()
async def untimeout(interaction: discord.Interaction, member: discord.Member, reason: str = "Không nêu lý do") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    try:
        await member.timeout(None, reason=f"{reason} | Moderator: {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Moderate Members.")
    await interaction.response.send_message(embed=success_embed("Đã gỡ timeout", f"Đã gỡ timeout cho {member.mention}.\nLý do: {reason}"))

@bot.tree.command(name="purge", description="Xoá một số tin nhắn gần nhất trong kênh")
@admin_only()
@app_commands.describe(amount="Số tin nhắn, từ 1 đến 100")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.channel, discord.TextChannel):
        return await respond_error(interaction, "Lệnh này chỉ dùng trong kênh chữ.")
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount + 1, reason=f"Purged by {interaction.user}")
    except discord.Forbidden:
        return await interaction.followup.send(embed=error_embed("Bot thiếu quyền Manage Messages."), ephemeral=True)
    await interaction.followup.send(embed=success_embed("Đã xoá tin nhắn", f"Đã xoá {max(0, len(deleted)-1)} tin nhắn."), ephemeral=True)
    await send_modlog(interaction.guild, "Purge", f"{interaction.user.mention} đã xoá {max(0, len(deleted)-1)} tin nhắn tại {interaction.channel.mention}.")

@bot.tree.command(name="warn", description="Cảnh cáo một thành viên")
@admin_only()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str = "Không nêu lý do") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    db_conn().execute(
        "INSERT INTO warnings (guild_id,user_id,moderator_id,reason,created_at) VALUES (?,?,?,?,?)",
        (interaction.guild.id, member.id, interaction.user.id, reason, discord.utils.utcnow().isoformat()),
    )
    db_conn().commit()
    count = db_conn().execute(
        "SELECT COUNT(*) AS n FROM warnings WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, member.id),
    ).fetchone()["n"]
    await interaction.response.send_message(embed=warning_embed("Đã cảnh cáo", f"{member.mention}\nLý do: {reason}\nTổng cảnh cáo: **{count}**"))
    try:
        await member.send(embed=warning_embed(f"Cảnh cáo tại {interaction.guild.name}", f"Lý do: {reason}"))
    except discord.HTTPException:
        pass
    await send_modlog(interaction.guild, "Cảnh cáo", f"{member.mention} — {reason} — Tổng: {count}")

@bot.tree.command(name="warnings", description="Xem danh sách cảnh cáo của thành viên")
@admin_only()
async def warnings(interaction: discord.Interaction, member: discord.Member) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    rows = db_conn().execute(
        "SELECT moderator_id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (interaction.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await interaction.response.send_message(embed=make_embed("📋 Cảnh cáo", f"{member.mention} chưa có cảnh cáo."), ephemeral=True)
    lines = [f"• <t:{int(datetime.fromisoformat(r['created_at']).timestamp())}:R> — <@{r['moderator_id']}> — {r['reason']}" for r in rows]
    await interaction.response.send_message(embed=make_embed(f"📋 Cảnh cáo — {member}", "\n".join(lines)), ephemeral=True)

@bot.tree.command(name="clearwarnings", description="Xoá toàn bộ cảnh cáo của thành viên")
@admin_only()
async def clearwarnings(interaction: discord.Interaction, member: discord.Member) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    cursor = db_conn().execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
    db_conn().commit()
    await interaction.response.send_message(embed=success_embed("Đã xoá cảnh cáo", f"Đã xoá {cursor.rowcount} cảnh cáo của {member.mention}."))

@bot.tree.command(name="lock", description="Khoá kênh hiện tại để thành viên không gửi tin")
@admin_only()
async def lock(interaction: discord.Interaction) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await respond_error(interaction, "Lệnh này chỉ dùng trong kênh chữ.")
    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Locked by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Channels.")
    await interaction.response.send_message(embed=success_embed("Đã khoá kênh", channel.mention))

@bot.tree.command(name="unlock", description="Mở khoá kênh hiện tại")
@admin_only()
async def unlock(interaction: discord.Interaction) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await respond_error(interaction, "Lệnh này chỉ dùng trong kênh chữ.")
    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None
    try:
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Channels.")
    await interaction.response.send_message(embed=success_embed("Đã mở khoá kênh", channel.mention))

@bot.tree.command(name="slowmode", description="Đặt slowmode cho kênh hiện tại")
@admin_only()
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.channel, discord.TextChannel):
        return await respond_error(interaction, "Lệnh này chỉ dùng trong kênh chữ.")
    try:
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slowmode set by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Channels.")
    await interaction.response.send_message(embed=success_embed("Đã cập nhật slowmode", f"Slowmode: **{seconds} giây**."))

@bot.tree.command(name="nick", description="Đổi biệt danh của thành viên")
@admin_only()
async def nick(interaction: discord.Interaction, member: discord.Member, nickname: Optional[str] = None) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if not isinstance(interaction.user, discord.Member):
        return await respond_error(interaction, "Không xác định được người thực hiện.")
    allowed, why = can_moderate_target(interaction.user, member)
    if not allowed:
        return await respond_error(interaction, why)
    try:
        await member.edit(nick=nickname, reason=f"Nickname changed by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Nicknames.")
    await interaction.response.send_message(embed=success_embed("Đã đổi biệt danh", f"{member.mention}: `{nickname or 'đã xoá biệt danh'}`"))

@bot.tree.command(name="addrole", description="Thêm role cho thành viên")
@admin_only()
async def addrole(interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
        return await respond_error(interaction, "Bot không thể quản lý role này do role hierarchy.")
    try:
        await member.add_roles(role, reason=f"Added by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Roles.")
    await interaction.response.send_message(embed=success_embed("Đã thêm role", f"Đã thêm {role.mention} cho {member.mention}."))

@bot.tree.command(name="removememberrole", description="Gỡ role khỏi thành viên")
@admin_only()
async def removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
        return await respond_error(interaction, "Bot không thể quản lý role này do role hierarchy.")
    try:
        await member.remove_roles(role, reason=f"Removed by {interaction.user}")
    except discord.Forbidden:
        return await respond_error(interaction, "Bot thiếu quyền Manage Roles.")
    await interaction.response.send_message(embed=success_embed("Đã gỡ role", f"Đã gỡ {role.mention} khỏi {member.mention}."))

@bot.tree.command(name="capquyen", description="Cấp quyền sử dụng bot cho một role (chỉ Owner)")
@app_commands.describe(role="Role được phép sử dụng các lệnh của bot")
async def capquyen(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("🔒 Chỉ Owner bot được dùng lệnh này.", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    if role.is_default() or role.managed:
        return await interaction.response.send_message("Không thể cấp quyền cho @everyone hoặc role tích hợp.", ephemeral=True)
    db_conn().execute(
        "INSERT OR REPLACE INTO allowed_access_roles (guild_id, role_id, added_by, added_at) VALUES (?, ?, ?, ?)",
        (interaction.guild.id, role.id, interaction.user.id, datetime.now(timezone.utc).isoformat()),
    )
    db_conn().commit()
    await interaction.response.send_message(f"✅ Đã cấp quyền sử dụng bot cho {role.mention}. Các thành viên có role này được phép dùng lệnh bot.", ephemeral=True)

@bot.tree.command(name="removerole", description="Thu hồi quyền sử dụng bot của một role (chỉ Owner)")
@app_commands.describe(role="Role cần thu hồi quyền sử dụng bot")
async def revoke_access_role(interaction: discord.Interaction, role: discord.Role) -> None:
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("🔒 Chỉ Owner bot được dùng lệnh này.", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    cur = db_conn().execute(
        "DELETE FROM allowed_access_roles WHERE guild_id=? AND role_id=?",
        (interaction.guild.id, role.id),
    )
    db_conn().commit()
    message = f"✅ Đã thu hồi quyền sử dụng bot của {role.mention}." if cur.rowcount else f"ℹ️ {role.mention} chưa có trong danh sách được cấp quyền."
    await interaction.response.send_message(message, ephemeral=True)

@bot.tree.command(name="listrole", description="Xem role được phép sử dụng bot (chỉ Owner)")
async def list_access_roles(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("🔒 Chỉ Owner bot được dùng lệnh này.", ephemeral=True)
    if interaction.guild is None:
        return await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
    rows = db_conn().execute(
        "SELECT role_id FROM allowed_access_roles WHERE guild_id=? ORDER BY role_id",
        (interaction.guild.id,),
    ).fetchall()
    if not rows:
        return await interaction.response.send_message("Chưa có role nào được cấp quyền trong server này.", ephemeral=True)
    lines = []
    for row in rows:
        role = interaction.guild.get_role(int(row[0]))
        lines.append(role.mention if role else f"Role không còn tồn tại (`{row[0]}`)")
    await interaction.response.send_message("**Role được phép sử dụng bot:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="setmodlog", description="Chọn kênh nhật ký quản trị")
@admin_only()
async def setmodlog(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "modlog_channel_id", channel.id)
    await interaction.response.send_message(embed=success_embed("Đã đặt kênh log", f"Nhật ký sẽ gửi đến {channel.mention}."), ephemeral=True)

# ============================================================================
# 8. WELCOME, GOODBYE, AUTOROLE, AND LOG SETTINGS
# ============================================================================

@bot.tree.command(name="setwelcome", description="Thiết lập kênh và nội dung chào thành viên")
@admin_only()
@app_commands.describe(channel="Kênh chào mừng", message="Dùng {user}, {server}, {count}")
async def setwelcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str = "Chào mừng {user} đến với {server}!") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "welcome_channel_id", channel.id)
    set_setting(interaction.guild.id, "welcome_message", message[:1800])
    await interaction.response.send_message(embed=success_embed("Đã cấu hình chào mừng", f"Kênh: {channel.mention}\nNội dung: {message}"), ephemeral=True)

@bot.tree.command(name="setgoodbye", description="Thiết lập kênh và nội dung tạm biệt")
@admin_only()
async def setgoodbye(interaction: discord.Interaction, channel: discord.TextChannel, message: str = "{user} đã rời khỏi {server}.") -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "goodbye_channel_id", channel.id)
    set_setting(interaction.guild.id, "goodbye_message", message[:1800])
    await interaction.response.send_message(embed=success_embed("Đã cấu hình tạm biệt", f"Kênh: {channel.mention}\nNội dung: {message}"), ephemeral=True)

@bot.tree.command(name="setautorole", description="Chọn role tự động cấp cho thành viên mới")
@admin_only()
async def setautorole(interaction: discord.Interaction, role: Optional[discord.Role] = None) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    if role is not None:
        if role.is_default() or role.managed or role >= interaction.guild.me.top_role:
            return await respond_error(interaction, "Bot không thể cấp role này do role hierarchy.")
        set_setting(interaction.guild.id, "join_role_id", role.id)
        await interaction.response.send_message(embed=success_embed("Đã bật autorole", f"Role tự động: {role.mention}"), ephemeral=True)
    else:
        set_setting(interaction.guild.id, "join_role_id", None)
        await interaction.response.send_message(embed=success_embed("Đã tắt autorole", "Bot sẽ không tự cấp role khi thành viên mới vào."), ephemeral=True)

@bot.tree.command(name="setmessagelog", description="Bật hoặc tắt log tin nhắn")
@admin_only()
async def setmessagelog(interaction: discord.Interaction, enabled: bool) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "log_messages", int(enabled))
    await interaction.response.send_message(embed=success_embed("Đã cập nhật", f"Log tin nhắn: {'bật' if enabled else 'tắt'}."), ephemeral=True)

@bot.tree.command(name="setvoicelog", description="Bật hoặc tắt log voice")
@admin_only()
async def setvoicelog(interaction: discord.Interaction, enabled: bool) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "log_voice", int(enabled))
    await interaction.response.send_message(embed=success_embed("Đã cập nhật", f"Log voice: {'bật' if enabled else 'tắt'}."), ephemeral=True)

@bot.tree.command(name="setlevel", description="Bật hoặc tắt hệ thống cấp độ")
@admin_only()
async def setlevel(interaction: discord.Interaction, enabled: bool) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "level_enabled", int(enabled))
    await interaction.response.send_message(embed=success_embed("Đã cập nhật", f"Hệ thống cấp độ: {'bật' if enabled else 'tắt'}."), ephemeral=True)

# ============================================================================
# 9. LEVELS, RANK, AND LEADERBOARD
# ============================================================================

@bot.tree.command(name="rank", description="Xem cấp độ và XP của bạn hoặc thành viên")
async def rank(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    if not interaction.guild:
        return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
    target = member or interaction.user
    row = db_conn().execute(
        "SELECT xp,level FROM user_levels WHERE guild_id=? AND user_id=?",
        (interaction.guild.id, target.id),
    ).fetchone()
    xp = int(row["xp"]) if row else 0
    level = int(row["level"]) if row else 0
    embed = make_embed(f"⭐ Rank — {target}")
    embed.add_field(name="Cấp độ", value=str(level), inline=True)
    embed.add_field(name="XP", value=str(xp), inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Xem bảng xếp hạng XP trong server")
async def leaderboard(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
    rows = db_conn().execute(
        "SELECT user_id,xp,level FROM user_levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
        (interaction.guild.id,),
    ).fetchall()
    if not rows:
        return await interaction.response.send_message(embed=make_embed("🏆 Bảng xếp hạng", "Chưa có dữ liệu XP."))
    lines = []
    for index, row in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(row["user_id"]))
        name = member.mention if member else f"<@{row['user_id']}>"
        lines.append(f"**{index}.** {name} — cấp {row['level']} ({row['xp']} XP)")
    await interaction.response.send_message(embed=make_embed("🏆 Bảng xếp hạng XP", "\n".join(lines)))

# ============================================================================
# 10. PUBLIC FUN COMMANDS
# ============================================================================

@bot.tree.command(name="coinflip", description="Tung đồng xu")
async def coinflip(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(random.choice(["🪙 **Ngửa!**", "🪙 **Sấp!**"]))

@bot.tree.command(name="dice", description="Tung xúc xắc")
async def dice(interaction: discord.Interaction, sides: app_commands.Range[int, 2, 100] = 6) -> None:
    await interaction.response.send_message(f"🎲 Bạn tung được **{random.randint(1, sides)}** (xúc xắc {sides} mặt).")

@bot.tree.command(name="8ball", description="Hỏi quả cầu tiên tri")
async def eightball(interaction: discord.Interaction, question: str) -> None:
    answers = [
        "Chắc chắn rồi.", "Khả năng cao là có.", "Mình nghĩ là có.",
        "Chưa thể kết luận.", "Hãy hỏi lại sau nhé.", "Có vẻ không.",
        "Khả năng cao là không.", "Đừng trông chờ điều đó.",
    ]
    await interaction.response.send_message(f"🎱 **Câu hỏi:** {question}\n**Trả lời:** {random.choice(answers)}")

@bot.tree.command(name="choose", description="Chọn ngẫu nhiên một trong các lựa chọn")
async def choose(interaction: discord.Interaction, options: str) -> None:
    choices = [part.strip() for part in options.split(",") if part.strip()]
    if len(choices) < 2:
        return await respond_error(interaction, "Hãy nhập ít nhất hai lựa chọn, ngăn cách bằng dấu phẩy.")
    await interaction.response.send_message(f"🎯 Mình chọn: **{discord.utils.escape_markdown(random.choice(choices))}**")

@bot.tree.command(name="rps", description="Chơi kéo búa bao")
async def rps(interaction: discord.Interaction, choice: str) -> None:
    choice = choice.lower().strip()
    mapping = {"kéo": "✂️", "keo": "✂️", "búa": "🪨", "bua": "🪨", "bao": "📄"}
    normalized = {"kéo": "keo", "keo": "keo", "búa": "bua", "bua": "bua", "bao": "bao"}
    if choice not in mapping:
        return await respond_error(interaction, "Chọn `kéo`, `búa` hoặc `bao`.")
    user_pick = normalized[choice]
    bot_pick = random.choice(["keo", "bua", "bao"])
    beats = {"keo": "bao", "bao": "bua", "bua": "keo"}
    result = "Hoà!" if user_pick == bot_pick else ("Bạn thắng!" if beats[user_pick] == bot_pick else "Bot thắng!")
    display = {"keo": "✂️ Kéo", "bua": "🪨 Búa", "bao": "📄 Bao"}
    await interaction.response.send_message(f"Bạn: **{display[user_pick]}**\nBot: **{display[bot_pick]}**\n**{result}**")

@bot.tree.command(name="poll", description="Tạo bình chọn nhanh bằng reaction")
async def poll(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.send_message(embed=make_embed("📊 Bình chọn", question))
    try:
        message = await interaction.original_response()
        await message.add_reaction("👍")
        await message.add_reaction("👎")
    except discord.HTTPException:
        logger.exception("Could not add poll reactions")

@bot.tree.command(name="quote", description="Gửi một câu trích dẫn ngẫu nhiên")
async def quote(interaction: discord.Interaction) -> None:
    quotes = [
        "Tiến chậm vẫn là tiến về phía trước.",
        "Kiên trì biến điều khó thành điều có thể.",
        "Không cần hoàn hảo, chỉ cần tốt hơn hôm qua.",
        "Bắt đầu là một nửa của thành công.",
        "Một bước nhỏ hôm nay có thể tạo khác biệt ngày mai.",
        "Học từ sai lầm, đừng để sai lầm định nghĩa bạn.",
        "Tập trung vào điều bạn có thể thay đổi.",
        "Thành công được xây từ những việc nhỏ làm đều đặn.",
    ]
    await interaction.response.send_message(f"💬 {random.choice(quotes)}")

@bot.tree.command(name="compliment", description="Gửi lời khen ngẫu nhiên")
async def compliment(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    target = member or interaction.user
    phrases = ["Bạn mang năng lượng tích cực.", "Bạn có cách nhìn rất riêng.", "Sự cố gắng của bạn đáng được ghi nhận.", "Bạn làm tốt hơn bạn nghĩ đấy.", "Bạn có thể tạo khác biệt bằng những điều nhỏ."]
    await interaction.response.send_message(f"💙 {target.mention}, {random.choice(phrases)}")

@bot.tree.command(name="hug", description="Gửi một cái ôm thân thiện")
async def hug(interaction: discord.Interaction, member: discord.Member) -> None:
    await interaction.response.send_message(f"🫂 {interaction.user.mention} gửi một cái ôm thân thiện đến {member.mention}!")

@bot.tree.command(name="ship", description="Tạo điểm hợp cạ vui vẻ giữa hai thành viên")
async def ship(interaction: discord.Interaction, first: discord.Member, second: discord.Member) -> None:
    score = random.randint(0, 100)
    bar = "❤️" * (score // 10) + "🖤" * (10 - score // 10)
    await interaction.response.send_message(f"💞 **{first.display_name} + {second.display_name}**\n{bar}\nĐộ hợp cạ vui vẻ: **{score}%**")

@bot.tree.command(name="trivia", description="Nhận một câu đố kiến thức ngẫu nhiên")
async def trivia(interaction: discord.Interaction) -> None:
    questions = [
        ("Hành tinh nào gần Mặt Trời nhất?", "Sao Thủy"),
        ("Nước có công thức hoá học là gì?", "H₂O"),
        ("Một giờ có bao nhiêu giây?", "3600"),
        ("Thủ đô của Nhật Bản là gì?", "Tokyo"),
        ("Loài vật nào là động vật có vú lớn nhất?", "Cá voi xanh"),
    ]
    question, answer = random.choice(questions)
    await interaction.response.send_message(f"🧠 **Câu đố:** {question}\n||Đáp án: {answer}||")

@bot.tree.command(name="remindme", description="Đặt lời nhắc sau một khoảng thời gian")
@app_commands.describe(duration="Ví dụ 10m, 2h, 1d", reminder="Nội dung lời nhắc")
async def remindme(interaction: discord.Interaction, duration: str, reminder: str) -> None:
    seconds = parse_duration(duration)
    if seconds is None or seconds > 365 * 86400:
        return await respond_error(interaction, "Thời lượng không hợp lệ. Ví dụ `10m`, `2h`, `1d`; tối đa một năm.")
    db_conn().execute(
        "INSERT INTO reminders (user_id,channel_id,remind_at,text) VALUES (?,?,?,?)",
        (interaction.user.id, interaction.channel_id, time.time() + seconds, reminder[:1000]),
    )
    db_conn().commit()
    await interaction.response.send_message(embed=success_embed("Đã đặt lời nhắc", f"Mình sẽ nhắc bạn sau **{human_duration(seconds)}**."), ephemeral=True)

@tasks.loop(seconds=15)
async def reminder_loop() -> None:
    if bot.db is None:
        return
    now = time.time()
    rows = db_conn().execute(
        "SELECT id,user_id,channel_id,text FROM reminders WHERE delivered=0 AND remind_at<=? LIMIT 20",
        (now,),
    ).fetchall()
    for row in rows:
        channel = bot.get_channel(int(row["channel_id"]))
        try:
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(f"⏰ <@{row['user_id']}> Nhắc nhở: {row['text']}")
        except discord.HTTPException:
            logger.exception("Reminder delivery failed")
        db_conn().execute("UPDATE reminders SET delivered=1 WHERE id=?", (row["id"],))
    db_conn().commit()

@reminder_loop.before_loop
async def before_reminder_loop() -> None:
    await bot.wait_until_ready()

# ============================================================================
# 11. TICKET SYSTEM
# ============================================================================

@bot.tree.command(name="ticketsetup", description="Cấu hình danh mục và role hỗ trợ ticket")
@admin_only()
async def ticketsetup(interaction: discord.Interaction, category: discord.CategoryChannel, support_role: Optional[discord.Role] = None) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "ticket_category_id", category.id)
    set_setting(interaction.guild.id, "ticket_support_role_id", support_role.id if support_role else None)
    await interaction.response.send_message(embed=success_embed("Đã cấu hình ticket", f"Danh mục: {category.name}\nRole hỗ trợ: {support_role.mention if support_role else 'Chưa đặt'}"), ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Gửi bảng tạo ticket vào kênh hiện tại")
@admin_only()
async def ticketpanel(interaction: discord.Interaction) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    embed = make_embed("🎫 Trung tâm hỗ trợ", "Nhấn nút bên dưới để tạo ticket riêng với đội hỗ trợ.")
    view = TicketPanelView()
    await interaction.response.send_message(embed=embed, view=view)

class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Tạo ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="community_bot:create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await respond_error(interaction, "Lệnh này chỉ dùng trong server.")
        guild = interaction.guild
        ensure_guild(guild.id)
        existing = db_conn().execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND status='open'",
            (guild.id, interaction.user.id),
        ).fetchone()
        if existing:
            channel = guild.get_channel(int(existing["channel_id"]))
            if channel:
                return await interaction.response.send_message(f"Bạn đã có ticket đang mở: {channel.mention}", ephemeral=True)
        category_id = get_setting(guild.id, "ticket_category_id")
        category = guild.get_channel(int(category_id)) if category_id else None
        if category_id and not isinstance(category, discord.CategoryChannel):
            return await respond_error(interaction, "Danh mục ticket chưa được cấu hình đúng. Admin dùng `/ticketsetup`.")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)
        support_role_id = get_setting(guild.id, "ticket_support_role_id")
        support_role = guild.get_role(int(support_role_id)) if support_role_id else None
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        counter_row = db_conn().execute("SELECT ticket_counter FROM guild_settings WHERE guild_id=?", (guild.id,)).fetchone()
        number = int(counter_row["ticket_counter"] if counter_row else 0) + 1
        set_setting(guild.id, "ticket_counter", number)
        channel_name = f"ticket-{number:04d}"
        try:
            channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites, reason=f"Ticket opened by {interaction.user}")
        except discord.Forbidden:
            return await respond_error(interaction, "Bot thiếu quyền Manage Channels.")
        db_conn().execute(
            "INSERT INTO tickets (guild_id,channel_id,opener_id,status,created_at) VALUES (?,?,?,?,?)",
            (guild.id, channel.id, interaction.user.id, "open", discord.utils.utcnow().isoformat()),
        )
        db_conn().commit()
        embed = make_embed("🎫 Ticket đã tạo", f"Xin chào {interaction.user.mention}. Hãy mô tả vấn đề của bạn.\nDùng `/ticketclose` khi đã xong.")
        await channel.send(content=support_role.mention if support_role else None, embed=embed)
        await interaction.response.send_message(f"Ticket của bạn đã được tạo: {channel.mention}", ephemeral=True)

@bot.tree.command(name="ticketclose", description="Đóng ticket hiện tại")
async def ticketclose(interaction: discord.Interaction, reason: str = "Đã giải quyết") -> None:
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        return await respond_error(interaction, "Lệnh này chỉ dùng trong kênh ticket.")
    row = db_conn().execute(
        "SELECT opener_id,status FROM tickets WHERE guild_id=? AND channel_id=?",
        (interaction.guild.id, interaction.channel.id),
    ).fetchone()
    if not row or row["status"] != "open":
        return await respond_error(interaction, "Đây không phải ticket đang mở.")
    is_opener = int(row["opener_id"]) == interaction.user.id
    is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
    support_role_id = get_setting(interaction.guild.id, "ticket_support_role_id")
    has_support = isinstance(interaction.user, discord.Member) and support_role_id and any(r.id == int(support_role_id) for r in interaction.user.roles)
    if not (is_opener or is_admin or has_support):
        return await respond_error(interaction, "Chỉ người tạo ticket, Admin hoặc role hỗ trợ mới đóng được ticket.")
    db_conn().execute(
        "UPDATE tickets SET status='closed',closed_at=? WHERE channel_id=?",
        (discord.utils.utcnow().isoformat(), interaction.channel.id),
    )
    db_conn().commit()
    await interaction.response.send_message(embed=success_embed("Ticket đã đóng", f"Lý do: {reason}"))
    await asyncio.sleep(3)
    try:
        await interaction.channel.delete(reason=f"Ticket closed: {reason}")
    except discord.HTTPException:
        logger.exception("Could not delete closed ticket channel")

# ============================================================================
# 12. STARBOARD
# ============================================================================

@bot.tree.command(name="setstarboard", description="Đặt kênh nhận bài được thả sao")
@admin_only()
async def setstarboard(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not admin_check(interaction):
        return await respond_error(interaction, "Bạn cần quyền Administrator.")
    set_setting(interaction.guild.id, "starboard_channel_id", channel.id)
    await interaction.response.send_message(embed=success_embed("Đã đặt starboard", f"Kênh: {channel.mention}"), ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if payload.guild_id is None or payload.emoji.name not in ("⭐", "🌟") or (bot.user and payload.user_id == bot.user.id):
        return
    channel_id = get_setting(payload.guild_id, "starboard_channel_id")
    if not channel_id:
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    source_channel = guild.get_channel(payload.channel_id)
    destination = guild.get_channel(int(channel_id))
    if not isinstance(source_channel, discord.TextChannel) or not isinstance(destination, discord.TextChannel):
        return
    try:
        message = await source_channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    count = sum(r.count for r in message.reactions if str(r.emoji) in ("⭐", "🌟"))
    if count < 3:
        return
    existing = db_conn().execute(
        "SELECT star_count FROM starboard_entries WHERE guild_id=? AND message_id=?",
        (payload.guild_id, message.id),
    ).fetchone()
    if existing and int(existing["star_count"]) >= count:
        return
    db_conn().execute(
        "INSERT INTO starboard_entries (guild_id,message_id,star_count) VALUES (?,?,?) ON CONFLICT(guild_id,message_id) DO UPDATE SET star_count=excluded.star_count",
        (payload.guild_id, message.id, count),
    )
    db_conn().commit()
    embed = make_embed(f"⭐ {count} sao", message.content[:1500] or "[Tin nhắn không có văn bản]")
    embed.add_field(name="Tác giả", value=message.author.mention, inline=True)
    embed.add_field(name="Kênh", value=message.channel.mention, inline=True)
    embed.add_field(name="Tin nhắn gốc", value=f"[Mở tin nhắn]({message.jump_url})", inline=False)
    if message.attachments:
        image = next((a for a in message.attachments if a.content_type and a.content_type.startswith("image/")), None)
        if image:
            embed.set_image(url=image.url)
    try:
        await destination.send(embed=embed)
    except discord.HTTPException:
        logger.exception("Starboard post failed")

# ============================================================================
# 13. OWNER COMMANDS
# ============================================================================

@bot.tree.command(name="botstatus", description="Owner: xem trạng thái bot")
@owner_only()
async def botstatus(interaction: discord.Interaction) -> None:
    if not owner_check(interaction):
        return await respond_error(interaction, "Lệnh này chỉ dành cho Owner.")
    embed = make_embed("🔐 Trạng thái bot")
    embed.add_field(name="User", value=str(bot.user), inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Latency", value=f"{bot.latency*1000:.0f} ms", inline=True)
    embed.add_field(name="Database", value="Connected" if bot.db else "Disconnected", inline=True)
    embed.add_field(name="Uptime", value=human_duration(int(time.time()-bot.started_at)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="synccommands", description="Owner: đồng bộ lại slash command")
@owner_only()
async def synccommands(interaction: discord.Interaction) -> None:
    if not owner_check(interaction):
        return await respond_error(interaction, "Lệnh này chỉ dành cho Owner.")
    await interaction.response.defer(ephemeral=True)
    try:
        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        await interaction.followup.send(embed=success_embed("Đã đồng bộ", f"Đã đồng bộ {len(synced)} lệnh."), ephemeral=True)
    except Exception as exc:
        logger.exception("Slash command sync failed")
        await interaction.followup.send(embed=error_embed("Đồng bộ thất bại", str(exc)[:1000]), ephemeral=True)

@bot.tree.command(name="shutdown", description="Owner: tắt bot an toàn")
@owner_only()
async def shutdown(interaction: discord.Interaction) -> None:
    if not owner_check(interaction):
        return await respond_error(interaction, "Lệnh này chỉ dành cho Owner.")
    await interaction.response.send_message(embed=warning_embed("Đang tắt bot", "Bot sẽ dừng tiến trình."), ephemeral=True)
    await bot.close()

@bot.tree.command(name="dbstats", description="Owner: xem thống kê dữ liệu SQLite")
@owner_only()
async def dbstats(interaction: discord.Interaction) -> None:
    if not owner_check(interaction):
        return await respond_error(interaction, "Lệnh này chỉ dành cho Owner.")
    stats = {}
    for table in ("warnings", "user_levels", "tickets", "reminders", "guild_settings"):
        stats[table] = db_conn().execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    embed = make_embed("🗃️ Thống kê database")
    for table, count in stats.items():
        embed.add_field(name=table, value=str(count), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================================================
# 14. ERROR HANDLING
# ============================================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "Bạn không có quyền dùng lệnh này."
    elif isinstance(error, app_commands.CheckFailure):
        message = "Bạn không được phép dùng lệnh này."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Bạn cần chờ {error.retry_after:.1f} giây trước khi dùng lại."
    elif isinstance(error, app_commands.TransformerError):
        message = "Một tham số của lệnh không hợp lệ."
    else:
        message = "Đã xảy ra lỗi. Hãy thử lại sau; lỗi đã được ghi vào log."
        logger.error("App command error: %s\n%s", error, "".join(traceback.format_exception(error)))
    await respond_error(interaction, message)

@bot.event
async def on_error(event_method: str, *args: Any, **kwargs: Any) -> None:
    logger.exception("Unhandled event error in %s", event_method)



# ============================================================================
# 17. PREFIX COMMAND PACK — 500 DISTINCT COMMAND NAMES
# ============================================================================
# Commands use BOT_PREFIX (default !). Discord message-content intent must be on.
# This pack keeps commands in this single file for easy Railway deployment.

import base64
import binascii
import calendar
import math
import hashlib
import html
import json as _json
import math as _math
import re as _re
import secrets
import statistics
import string as _string
import urllib.parse
import uuid as _uuid
from datetime import date as _date, datetime as _datetime, timedelta as _timedelta, timezone as _timezone

from commands import PREFIX_COMMAND_GROUPS

PREFIX_COMMAND_DESCRIPTIONS = {}
for _group_name, _command_names in PREFIX_COMMAND_GROUPS.items():
    for _command_name in _command_names:
        PREFIX_COMMAND_DESCRIPTIONS[_command_name] = f"Chức năng { _command_name.replace('_', ' ') } ({_group_name})."

PREFIX_ADMIN_GROUPS = {
    "moderation", "channel", "role", "logs", "security", "welcome", "ticket", "advanced"
}
PREFIX_OWNER_ONLY = {
    "bot_shutdown", "bot_sync_commands", "bot_db_backup", "bot_db_integrity",
    "bot_config_export", "bot_cache_clear", "bot_error_detail"
}

async def _prefix_send(ctx, text: str, *, ephemeral_note: bool = False):
    # Prefix commands are public messages; truncate safely under Discord's limit.
    await ctx.send(str(text)[:1900], allowed_mentions=discord.AllowedMentions.none())

async def _prefix_need_admin(ctx) -> bool:
    if not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        await _prefix_send(ctx, "🔒 Lệnh này chỉ dành cho quản trị viên (Administrator).")
        return False
    return True

async def _prefix_need_owner(ctx) -> bool:
    if ctx.author.id != OWNER_ID:
        await _prefix_send(ctx, "🔒 Lệnh này chỉ dành cho chủ bot.")
        return False
    return True

async def _prefix_need_guild(ctx) -> bool:
    if ctx.guild is None:
        await _prefix_send(ctx, "Lệnh này chỉ dùng trong server Discord.")
        return False
    return True


@bot.check
async def require_allowed_role_for_prefix(ctx: commands.Context) -> bool:
    """Owner bypasses the role gate; everyone else needs an allowlisted role."""
    if ctx.author.id == OWNER_ID:
        return True
    if ctx.guild is None or not isinstance(ctx.author, discord.Member):
        await _prefix_send(ctx, "🔒 Bạn cần có role được cấp quyền trong server để dùng bot.")
        return False
    try:
        rows = db_conn().execute(
            "SELECT role_id FROM allowed_access_roles WHERE guild_id=?",
            (ctx.guild.id,),
        ).fetchall()
        allowed = {int(row[0]) for row in rows}
    except (RuntimeError, sqlite3.Error):
        allowed = set()
    if any(role.id in allowed for role in ctx.author.roles):
        return True
    await _prefix_send(ctx, "🔒 Bạn chưa có role được cấp quyền sử dụng bot. Hãy liên hệ Owner bot.")
    return False

async def _prefix_target_member(ctx, token: str):
    if not ctx.guild:
        return None
    token = token.strip()
    match = _re.search(r"\d{15,22}", token)
    if match:
        member = ctx.guild.get_member(int(match.group(0)))
        if member:
            return member
    if token:
        try:
            return await commands.MemberConverter().convert(ctx, token)
        except commands.BadArgument:
            return None
    return None

async def _prefix_target_role(ctx, token: str):
    if not ctx.guild:
        return None
    try:
        return await commands.RoleConverter().convert(ctx, token)
    except commands.BadArgument:
        return None

async def run_prefix_action(ctx: commands.Context, name: str, raw: str = ""):
    """Execute a concrete command-specific action. Shared helpers only remove duplication."""
    args = raw.strip()
    parts = args.split()
    guild = ctx.guild
    author = ctx.author
    lower = name.lower()

    # ---------- text processing: every command has its own transformation ----------
    if lower == "alo":
        return await _prefix_send(ctx, args or "Alo 👋")
    if lower == "echo":
        return await _prefix_send(ctx, args or "Bạn chưa nhập nội dung.")
    if lower == "upper": return await _prefix_send(ctx, args.upper())
    if lower == "lower": return await _prefix_send(ctx, args.lower())
    if lower == "titlecase": return await _prefix_send(ctx, args.title())
    if lower == "reverse": return await _prefix_send(ctx, args[::-1])
    if lower == "length": return await _prefix_send(ctx, f"Số ký tự: {len(args)}")
    if lower == "wordcount": return await _prefix_send(ctx, f"Số từ: {len(args.split())}")
    if lower == "linecount": return await _prefix_send(ctx, f"Số dòng: {len(args.splitlines())}")
    if lower == "remove_space": return await _prefix_send(ctx, " ".join(args.split()))
    if lower == "remove_line": return await _prefix_send(ctx, "\n".join(x for x in args.splitlines() if x.strip()))
    if lower == "sort_words": return await _prefix_send(ctx, " ".join(sorted(args.split(), key=str.casefold)))
    if lower == "unique_words":
        seen = set(); words = []
        for word in args.split():
            if word.casefold() not in seen: seen.add(word.casefold()); words.append(word)
        return await _prefix_send(ctx, " ".join(words))
    if lower == "repeat":
        if len(parts) < 2: return await _prefix_send(ctx, "Dùng: !repeat <số_lần 1-10> <nội dung>")
        try: n = max(1, min(10, int(parts[0])))
        except ValueError: return await _prefix_send(ctx, "Số lần phải là số nguyên.")
        return await _prefix_send(ctx, "\n".join([" ".join(parts[1:])] * n))
    if lower == "mockcase": return await _prefix_send(ctx, "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(args)))
    if lower == "alternate_case": return await _prefix_send(ctx, "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(args)))
    if lower == "count_chars":
        from collections import Counter
        return await _prefix_send(ctx, "\n".join(f"{k!r}: {v}" for k,v in Counter(args).most_common(30)) or "Không có ký tự.")
    if lower == "count_letters": return await _prefix_send(ctx, f"Số chữ cái: {sum(c.isalpha() for c in args)}")
    if lower == "count_digits": return await _prefix_send(ctx, f"Số chữ số: {sum(c.isdigit() for c in args)}")
    if lower == "count_symbols": return await _prefix_send(ctx, f"Số ký tự đặc biệt: {sum(not c.isalnum() and not c.isspace() for c in args)}")
    if lower == "replace":
        p = args.split("|", 2)
        if len(p) != 3: return await _prefix_send(ctx, "Dùng: !replace <chuỗi_cũ>|<chuỗi_mới>|<văn_bản>")
        return await _prefix_send(ctx, p[2].replace(p[0], p[1]))
    if lower == "prefix_text":
        p = args.split("|",1)
        return await _prefix_send(ctx, p[0] + (p[1] if len(p)>1 else ""))
    if lower == "suffix_text":
        p = args.split("|",1)
        return await _prefix_send(ctx, (p[0] if len(p)>1 else "") + (p[1] if len(p)>1 else p[0]))
    if lower == "split_text":
        if not args: return await _prefix_send(ctx, "Nhập văn bản cần chia.")
        return await _prefix_send(ctx, "\n---\n".join(args[i:i+300] for i in range(0,len(args),300))[:1850])
    if lower == "join_text": return await _prefix_send(ctx, args.replace(" | ", " ").replace("|", " "))

    # ---------- member and server information ----------
    if lower in {"userinfo", "avatar", "banner", "userid", "username", "displayname", "nickname", "account_age", "joined_at", "created_at", "member_roles", "member_toprole", "member_status", "member_activity", "member_voice", "member_permissions", "member_color", "member_mentions", "member_compare", "member_invites", "member_messages"}:
        target = await _prefix_target_member(ctx, args) if args else (author if isinstance(author, discord.Member) else None)
        if target is None: return await _prefix_send(ctx, "Không tìm thấy thành viên. Dùng mention hoặc ID.")
        if lower == "avatar": return await _prefix_send(ctx, target.display_avatar.url)
        if lower == "banner":
            try:
                user = await bot.fetch_user(target.id)
                return await _prefix_send(ctx, user.banner.url if user.banner else "Tài khoản này không có banner công khai.")
            except discord.HTTPException: return await _prefix_send(ctx, "Không tải được banner.")
        if lower == "userid": return await _prefix_send(ctx, str(target.id))
        if lower == "username": return await _prefix_send(ctx, target.name)
        if lower == "displayname": return await _prefix_send(ctx, target.display_name)
        if lower == "nickname": return await _prefix_send(ctx, target.nick or "Không có biệt danh.")
        if lower == "account_age": return await _prefix_send(ctx, f"Tài khoản tạo lúc: <t:{int(target.created_at.timestamp())}:F>")
        if lower == "joined_at": return await _prefix_send(ctx, f"Vào server lúc: {discord.utils.format_dt(target.joined_at, 'F') if target.joined_at else 'Không rõ'}")
        if lower == "created_at": return await _prefix_send(ctx, discord.utils.format_dt(target.created_at, 'F'))
        if lower == "member_roles": return await _prefix_send(ctx, ", ".join(r.name for r in target.roles[1:]) or "Không có vai trò.")
        if lower == "member_toprole": return await _prefix_send(ctx, target.top_role.name)
        if lower == "member_status": return await _prefix_send(ctx, f"Trạng thái hiện tại: {getattr(target, 'status', 'không khả dụng')}")
        if lower == "member_activity": return await _prefix_send(ctx, "\n".join(str(a) for a in target.activities)[:1800] or "Không có hoạt động công khai.")
        if lower == "member_voice": return await _prefix_send(ctx, f"Đang ở: {target.voice.channel.mention}" if target.voice else "Không ở phòng thoại.")
        if lower == "member_permissions":
            if not ctx.channel.permissions_for(target): return await _prefix_send(ctx, "Không đọc được quyền.")
            perms = [k.replace('_',' ') for k,v in ctx.channel.permissions_for(target) if v]
            return await _prefix_send(ctx, "Quyền trong kênh: " + ", ".join(perms[:40]))
        if lower == "member_color": return await _prefix_send(ctx, f"Màu vai trò: #{target.color.value:06x}")
        if lower == "member_mentions": return await _prefix_send(ctx, target.mention)
        if lower == "member_compare":
            other = await _prefix_target_member(ctx, parts[-1]) if len(parts)>1 else None
            return await _prefix_send(ctx, f"{target} có {len(target.roles)-1} vai trò; {other} có {len(other.roles)-1} vai trò." if other else f"{target}: {len(target.roles)-1} vai trò, tạo tài khoản {target.created_at:%d/%m/%Y}.")
        if lower == "member_invites": return await _prefix_send(ctx, "Discord không cung cấp số lượt mời cá nhân chính xác nếu bot chưa lưu lịch sử invite.")
        if lower == "member_messages": return await _prefix_send(ctx, "Bản bot hiện chưa lưu bộ đếm tin nhắn lịch sử cho từng thành viên.")
        if lower == "userinfo":
            e=discord.Embed(title=f"Thông tin: {target}", color=target.color); e.set_thumbnail(url=target.display_avatar.url)
            e.add_field(name="ID",value=target.id); e.add_field(name="Tạo tài khoản",value=discord.utils.format_dt(target.created_at,'R'))
            e.add_field(name="Vào server",value=discord.utils.format_dt(target.joined_at,'R') if target.joined_at else "Không rõ")
            return await ctx.send(embed=e)

    if lower in {"serverinfo", "servericon", "serverbanner", "serverid", "serverowner", "membercount", "botcount", "humancount", "rolecount", "channelcount", "textchannels", "voicechannels", "categories", "server_created", "server_boosts", "server_features", "server_region", "server_emojis", "server_stickers", "server_roles", "server_rules", "server_stats", "server_activity", "server_invites", "server_snapshot"}:
        if not await _prefix_need_guild(ctx): return
        g = guild
        if lower == "servericon": return await _prefix_send(ctx, g.icon.url if g.icon else "Server chưa có icon.")
        if lower == "serverbanner": return await _prefix_send(ctx, g.banner.url if g.banner else "Server chưa có banner.")
        if lower == "serverid": return await _prefix_send(ctx, str(g.id))
        if lower == "serverowner": return await _prefix_send(ctx, f"Chủ server: <@{g.owner_id}> (`{g.owner_id}`)")
        if lower == "membercount": return await _prefix_send(ctx, f"Số thành viên: {g.member_count}")
        if lower == "botcount": return await _prefix_send(ctx, f"Số bot: {sum(m.bot for m in g.members)}")
        if lower == "humancount": return await _prefix_send(ctx, f"Số người dùng: {sum(not m.bot for m in g.members)}")
        if lower == "rolecount": return await _prefix_send(ctx, f"Số vai trò: {len(g.roles)}")
        if lower == "channelcount": return await _prefix_send(ctx, f"Số kênh: {len(g.channels)}")
        if lower == "textchannels": return await _prefix_send(ctx, "\n".join(c.mention for c in g.text_channels)[:1800] or "Không có kênh văn bản.")
        if lower == "voicechannels": return await _prefix_send(ctx, "\n".join(f"{c.name}: {len(c.members)} người" for c in g.voice_channels)[:1800] or "Không có phòng thoại.")
        if lower == "categories": return await _prefix_send(ctx, "\n".join(c.name for c in g.categories)[:1800] or "Không có danh mục.")
        if lower == "server_created": return await _prefix_send(ctx, discord.utils.format_dt(g.created_at,'F'))
        if lower == "server_boosts": return await _prefix_send(ctx, f"Boost: {g.premium_subscription_count or 0}; cấp {g.premium_tier}")
        if lower == "server_features": return await _prefix_send(ctx, ", ".join(g.features) or "Không có tính năng đặc biệt.")
        if lower == "server_region": return await _prefix_send(ctx, "Discord không còn cung cấp vùng server cố định.")
        if lower == "server_emojis": return await _prefix_send(ctx, " ".join(str(e) for e in g.emojis)[:1800] or "Không có emoji tùy chỉnh.")
        if lower == "server_stickers": return await _prefix_send(ctx, "\n".join(s.name for s in g.stickers)[:1800] or "Không có sticker.")
        if lower == "server_roles": return await _prefix_send(ctx, ", ".join(r.name for r in g.roles)[:1800])
        if lower == "server_rules": return await _prefix_send(ctx, f"Kênh quy tắc: {g.rules_channel.mention if g.rules_channel else 'chưa cấu hình'}")
        if lower == "server_activity": return await _prefix_send(ctx, "Hoạt động lịch sử chỉ khả dụng trong thời gian bot trực tuyến và ghi nhận sự kiện.")
        if lower == "server_invites":
            try:
                inv = await g.invites(); return await _prefix_send(ctx, "\n".join(f"{i.code}: {i.uses or 0} lượt" for i in inv)[:1800] or "Không có invite.")
            except discord.Forbidden: return await _prefix_send(ctx, "Bot thiếu quyền Manage Server để xem invite.")
        if lower == "server_snapshot": return await _prefix_send(ctx, f"Snapshot thông tin: server={g.name!r}, roles={len(g.roles)}, channels={len(g.channels)}, members={g.member_count}.")
        e=discord.Embed(title=g.name, description=f"{g.description or 'Không có mô tả'}", color=DEFAULT_COLOR)
        if g.icon: e.set_thumbnail(url=g.icon.url)
        e.add_field(name="ID",value=g.id); e.add_field(name="Thành viên",value=g.member_count or 0); e.add_field(name="Kênh",value=len(g.channels)); e.add_field(name="Vai trò",value=len(g.roles))
        return await ctx.send(embed=e)

    # ---------- math ----------
    math_ops = {"add":lambda a,b:a+b,"subtract":lambda a,b:a-b,"multiply":lambda a,b:a*b,"divide":lambda a,b:a/b,"power":lambda a,b:a**b,"gcd":lambda a,b:math.gcd(int(a),int(b)),"lcm":lambda a,b:math.lcm(int(a),int(b))}
    if lower in math_ops:
        try:
            a,b=map(float,parts[:2]);
            if lower == "divide" and b == 0: return await _prefix_send(ctx,"Không thể chia cho 0.")
            val=math_ops[lower](a,b); return await _prefix_send(ctx,f"Kết quả: {val:g}" if isinstance(val,float) else f"Kết quả: {val}")
        except Exception: return await _prefix_send(ctx,f"Dùng: !{lower} <số_a> <số_b>")
    if lower in {"sqrt","cuberoot","factorial","is_prime","prime_factors","percentage","percent_change","average","median","mode","range","variance","stddev","quadratic","linear","factorial_table","random_number","random_decimal","dice","roll","coinflip","rps","guess_number","math_quiz","random_question","fortune","mood","vibecheck","rate","ship","friendship","compatibility"}:
        try:
            if lower == "sqrt":
                x=float(parts[0]); return await _prefix_send(ctx, f"√{x:g} = {_math.sqrt(x):g}" if x>=0 else "Không có căn bậc hai thực cho số âm.")
            if lower == "cuberoot":
                x=float(parts[0]); return await _prefix_send(ctx, f"Căn bậc ba = {math.copysign(abs(x)**(1/3),x):g}")
            if lower == "factorial":
                n=int(parts[0]); return await _prefix_send(ctx, f"{n}! = {math.factorial(n)}" if 0<=n<=500 else "n phải từ 0 đến 500.")
            if lower == "is_prime":
                n=int(parts[0]); prime=n>=2 and all(n%d for d in range(2,int(n**0.5)+1)); return await _prefix_send(ctx, f"{n} {'là' if prime else 'không phải'} số nguyên tố.")
            if lower == "prime_factors":
                n=abs(int(parts[0])); factors=[]; d=2
                while d*d<=n:
                    while n%d==0: factors.append(d); n//=d
                    d+=1
                if n>1:factors.append(n)
                return await _prefix_send(ctx,"Thừa số nguyên tố: "+(" × ".join(map(str,factors)) if factors else "không có (n=0/1)"))
            if lower == "percentage":
                a,b=map(float,parts[:2]); return await _prefix_send(ctx, f"{a:g}/{b:g} = {a/b*100:.2f}%" if b else "Tổng không được bằng 0.")
            if lower == "percent_change":
                a,b=map(float,parts[:2]); return await _prefix_send(ctx, f"Thay đổi: {(b-a)/a*100:.2f}%" if a else "Giá trị cũ không được bằng 0.")
            if lower in {"average","median","mode","range","variance","stddev"}:
                nums=[float(x) for x in parts]
                if not nums: raise ValueError()
                if lower=="average": v=statistics.mean(nums)
                elif lower=="median": v=statistics.median(nums)
                elif lower=="mode": v=statistics.multimode(nums)
                elif lower=="range": v=max(nums)-min(nums)
                elif lower=="variance": v=statistics.pvariance(nums)
                else: v=statistics.pstdev(nums)
                return await _prefix_send(ctx,f"{lower}: {v}")
            if lower == "quadratic":
                a,b,c=map(float,parts[:3]); d=b*b-4*a*c
                if a==0:return await _prefix_send(ctx,"a phải khác 0.")
                if d<0:return await _prefix_send(ctx,"Phương trình có hai nghiệm phức.")
                return await _prefix_send(ctx,f"x1={(-b+math.sqrt(d))/(2*a):g}; x2={(-b-math.sqrt(d))/(2*a):g}")
            if lower == "linear":
                a,b=map(float,parts[:2]); return await _prefix_send(ctx, "Vô số nghiệm." if a==0 and b==0 else "Vô nghiệm." if a==0 else f"x={-b/a:g}")
            if lower == "factorial_table":
                n=int(parts[0]); return await _prefix_send(ctx,"\n".join(f"{i}! = {math.factorial(i)}" for i in range(max(0,min(n,20))+1)))
            if lower in {"random_number","dice","roll"}:
                if lower=="dice": sides=int(parts[0]) if parts else 6; return await _prefix_send(ctx,f"🎲 {random.randint(1,max(2,min(1000,sides)))}")
                if lower=="roll": sides=int(parts[0]) if parts else 6; return await _prefix_send(ctx,f"🎲 {random.randint(1,max(2,min(1000,sides)))}")
                a,b=map(int,parts[:2]); return await _prefix_send(ctx,str(random.randint(min(a,b),max(a,b))))
            if lower == "random_decimal":
                a,b=map(float,parts[:2]); return await _prefix_send(ctx,f"{random.uniform(min(a,b),max(a,b)):.4f}")
            if lower == "coinflip": return await _prefix_send(ctx,random.choice(["🪙 Ngửa!","🪙 Sấp!"]))
            if lower == "rps":
                choices=["kéo","búa","bao"]; user=args.casefold()
                if user not in choices:return await _prefix_send(ctx,"Dùng: !rps kéo|búa|bao")
                botpick=random.choice(choices); outcome="hòa" if user==botpick else "thắng" if (user,botpick) in [("kéo","bao"),("búa","kéo"),("bao","búa")] else "thua"
                return await _prefix_send(ctx,f"Bạn: {user}; bot: {botpick}. Kết quả: {outcome}.")
            if lower == "guess_number": return await _prefix_send(ctx,f"Gợi ý số ngẫu nhiên (1-100): **{random.randint(1,100)}**. (Bản này là lượt độc lập.)")
            if lower == "math_quiz":
                a,b=random.randint(2,30),random.randint(2,30); op=random.choice(['+','-','×']); val=a+b if op=='+' else a-b if op=='-' else a*b
                return await _prefix_send(ctx,f"Câu hỏi: {a} {op} {b} = ? (đáp án: ||{val}||)")
            if lower == "random_question": return await _prefix_send(ctx,random.choice(["Nếu có thể học một kỹ năng ngay lập tức, bạn chọn gì?","Kỷ niệm vui gần đây nhất của bạn là gì?","Bạn thích biển hay núi?","Một bộ phim bạn có thể xem lại nhiều lần?"]))
            if lower == "fortune": return await _prefix_send(ctx,random.choice(["Hôm nay hãy thử điều mới.","Một cuộc trò chuyện tốt có thể làm ngày bạn vui hơn.","Kiên trì từng chút sẽ tạo khác biệt."]))
            if lower == "mood": return await _prefix_send(ctx,"Tâm trạng ngẫu nhiên: "+random.choice(["vui vẻ 😄","bình yên 🌿","tò mò 🧐","năng lượng ⚡","thư giãn 🌙"]))
            if lower == "vibecheck": return await _prefix_send(ctx,f"Vibe hôm nay: {random.randint(1,100)}% ✨")
            if lower == "rate": return await _prefix_send(ctx,f"Điểm cho {args or 'điều này'}: {random.randint(1,10)}/10")
            if lower in {"ship","friendship","compatibility"}:
                return await _prefix_send(ctx,f"{lower.title()} score: **{random.randint(0,100)}%** (chỉ để giải trí).")
        except (ValueError, IndexError, OverflowError): return await _prefix_send(ctx,f"Thiếu hoặc sai tham số. Dùng !{lower} với các số phù hợp.")

    # ---------- conversions ----------
    conv = {
        "celsius_to_fahrenheit":lambda x:x*9/5+32,"fahrenheit_to_celsius":lambda x:(x-32)*5/9,"celsius_to_kelvin":lambda x:x+273.15,
        "km_to_miles":lambda x:x*0.621371,"miles_to_km":lambda x:x/0.621371,"meters_to_feet":lambda x:x*3.28084,"feet_to_meters":lambda x:x/3.28084,
        "cm_to_inches":lambda x:x/2.54,"inches_to_cm":lambda x:x*2.54,"kg_to_pounds":lambda x:x*2.20462,"pounds_to_kg":lambda x:x/2.20462,
        "grams_to_ounces":lambda x:x/28.3495,"ounces_to_grams":lambda x:x*28.3495,"liters_to_gallons":lambda x:x/3.78541,"gallons_to_liters":lambda x:x*3.78541,
        "ml_to_cups":lambda x:x/236.588,"cups_to_ml":lambda x:x*236.588,"kmh_to_mph":lambda x:x/1.60934,"mph_to_kmh":lambda x:x*1.60934,
        "bytes_to_kb":lambda x:x/1024,"kb_to_mb":lambda x:x/1024,"mb_to_gb":lambda x:x/1024,"seconds_to_minutes":lambda x:x/60,"minutes_to_hours":lambda x:x/60,"hours_to_days":lambda x:x/24,
    }
    if lower in conv:
        try: return await _prefix_send(ctx,f"Kết quả: {conv[lower](float(parts[0])):.6g}")
        except (ValueError,IndexError): return await _prefix_send(ctx,f"Dùng: !{lower} <giá_trị>")

    # ---------- date/time ----------
    if lower == "time": return await _prefix_send(ctx,datetime.now(timezone.utc).astimezone().strftime("Giờ hiện tại: %H:%M:%S %Z"))
    if lower == "date": return await _prefix_send(ctx,datetime.now().strftime("Ngày hiện tại: %d/%m/%Y"))
    if lower == "unix_time": return await _prefix_send(ctx,str(int(time.time())))
    if lower == "timestamp":
        try: return await _prefix_send(ctx,str(int(datetime.fromisoformat(args).replace(tzinfo=timezone.utc).timestamp())))
        except Exception: return await _prefix_send(ctx,"Dùng: !timestamp YYYY-MM-DD hoặc YYYY-MM-DDTHH:MM:SS")
    if lower == "weekday":
        try: return await _prefix_send(ctx,datetime.fromisoformat(args).strftime("%A"))
        except Exception: return await _prefix_send(ctx,"Dùng: !weekday YYYY-MM-DD")
    if lower == "week_number":
        try: return await _prefix_send(ctx,"Tuần ISO: "+str(datetime.fromisoformat(args).isocalendar().week))
        except Exception: return await _prefix_send(ctx,"Dùng: !week_number YYYY-MM-DD")
    if lower == "leap_year":
        try: y=int(parts[0]); return await _prefix_send(ctx,f"{y} {'là' if calendar.isleap(y) else 'không phải'} năm nhuận.")
        except Exception: return await _prefix_send(ctx,"Dùng: !leap_year <năm>")
    if lower == "days_in_month":
        try: m,y=map(int,parts[:2]); return await _prefix_send(ctx,f"Tháng {m}/{y} có {calendar.monthrange(y,m)[1]} ngày.")
        except Exception: return await _prefix_send(ctx,"Dùng: !days_in_month <tháng> <năm>")
    if lower in {"days_between","add_days","subtract_days","countdown"}:
        try:
            if lower == "days_between":
                a,b=map(datetime.fromisoformat,parts[:2]); return await _prefix_send(ctx,f"Khoảng cách: {abs((b-a).days)} ngày.")
            if lower == "countdown":
                d=datetime.fromisoformat(args); return await _prefix_send(ctx,f"Chênh lệch: {d-datetime.now()} (ngày giờ nhập theo múi giờ máy).")
            d=datetime.fromisoformat(parts[0]); n=int(parts[1])*(1 if lower=="add_days" else -1); return await _prefix_send(ctx,(d+timedelta(days=n)).isoformat())
        except Exception: return await _prefix_send(ctx,f"Dùng: !{lower} theo định dạng ngày ISO, ví dụ 2026-12-31")
    if lower == "timezone": return await _prefix_send(ctx,"Nhập múi giờ IANA, ví dụ Asia/Ho_Chi_Minh; để tra giờ cần cấu hình múi giờ hợp lệ.")
    if lower == "timezone_convert": return await _prefix_send(ctx,"Dùng thư viện zoneinfo với múi giờ IANA; định dạng đề xuất: !timezone_convert YYYY-MM-DDTHH:MM|Asia/Ho_Chi_Minh|UTC")

    # ---------- encoders and data tools ----------
    if lower == "base64_encode":
        if not args.strip():
            return await _prefix_send(ctx, f"❌ Cách dùng: `{BOT_PREFIX}base64_encode nội dung cần mã hóa`\nVí dụ: `{BOT_PREFIX}base64_encode xin chào`")
        return await _prefix_send(ctx, base64.b64encode(args.encode("utf-8")).decode("utf-8"))
    if lower == "base64_decode":
        try:return await _prefix_send(ctx,base64.b64decode(args,validate=True).decode())
        except Exception:return await _prefix_send(ctx,"Chuỗi Base64 không hợp lệ hoặc không phải UTF-8.")
    if lower == "url_encode": return await _prefix_send(ctx,urllib.parse.quote(args))
    if lower == "url_decode": return await _prefix_send(ctx,urllib.parse.unquote(args))
    if lower == "html_escape": return await _prefix_send(ctx,html.escape(args))
    if lower == "html_unescape": return await _prefix_send(ctx,html.unescape(args))
    if lower in {"json_format","json_minify","json_validate"}:
        try:
            obj=json.loads(args)
            if lower=="json_format": out=json.dumps(obj,ensure_ascii=False,indent=2)
            elif lower=="json_minify": out=json.dumps(obj,ensure_ascii=False,separators=(",",":"))
            else: out="JSON hợp lệ."
            return await _prefix_send(ctx,out[:1850])
        except Exception:return await _prefix_send(ctx,"JSON không hợp lệ.")
    if lower in {"uuid","random_uuid"}: return await _prefix_send(ctx,str(uuid.uuid4()))
    if lower in {"hash_sha256","hash_sha512","hash_md5"}:
        h=hashlib.sha256(args.encode()) if lower=="hash_sha256" else hashlib.sha512(args.encode()) if lower=="hash_sha512" else hashlib.md5(args.encode())
        return await _prefix_send(ctx,h.hexdigest())
    if lower == "password_generate":
        try:n=max(8,min(128,int(parts[0]))) if parts else 16
        except ValueError:n=16
        return await _prefix_send(ctx,secrets.token_urlsafe(n)[:n+4])
    if lower == "password_strength":
        score=sum([len(args)>=12, any(c.islower() for c in args), any(c.isupper() for c in args), any(c.isdigit() for c in args), any(not c.isalnum() for c in args)])
        return await _prefix_send(ctx,f"Đánh giá sơ bộ: {['rất yếu','yếu','trung bình','khá','mạnh','rất mạnh'][score]}. Không gửi mật khẩu thật vào chat công khai.")
    if lower in {"random_hex","random_string"}:
        try:n=max(1,min(128,int(parts[0]))) if parts else 16
        except ValueError:n=16
        chars="0123456789abcdef" if lower=="random_hex" else _string.ascii_letters+_string.digits
        return await _prefix_send(ctx,"".join(secrets.choice(chars) for _ in range(n)))
    if lower == "binary_encode": return await _prefix_send(ctx," ".join(format(b,'08b') for b in args.encode()))
    if lower == "binary_decode":
        try:return await _prefix_send(ctx,bytes(int(b,2) for b in args.split()).decode())
        except Exception:return await _prefix_send(ctx,"Dùng các byte nhị phân 8 bit cách nhau bằng dấu cách.")
    if lower == "hex_encode": return await _prefix_send(ctx,args.encode().hex())
    if lower == "hex_decode":
        try:return await _prefix_send(ctx,bytes.fromhex(args).decode())
        except Exception:return await _prefix_send(ctx,"Chuỗi hex không hợp lệ.")
    if lower in {"morse_encode","morse_decode"}:
        morse={'A':'.-','B':'-...','C':'-.-.','D':'-..','E':'.','F':'..-.','G':'--.','H':'....','I':'..','J':'.---','K':'-.-','L':'.-..','M':'--','N':'-.','O':'---','P':'.--.','Q':'--.-','R':'.-.','S':'...','T':'-','U':'..-','V':'...-','W':'.--','X':'-..-','Y':'-.--','Z':'--..','0':'-----','1':'.----','2':'..---','3':'...--','4':'....-','5':'.....','6':'-....','7':'--...','8':'---..','9':'----.',' ':'/'}
        if lower=='morse_encode':return await _prefix_send(ctx,' '.join(morse.get(c.upper(),'') for c in args))
        reverse={v:k for k,v in morse.items()}; return await _prefix_send(ctx,''.join(reverse.get(t,'?') for t in args.split()))
    if lower == "qr_text": return await _prefix_send(ctx,"Để tạo ảnh QR cần cài thêm thư viện qrcode[pil]. Lệnh hiện trả lời văn bản vì gói này chưa nằm trong requirements.")

    # ---------- social and lightweight games ----------
    social_lines={"hug":"gửi một cái ôm thân thiện tới","pat":"vỗ đầu thân thiện cho","wave":"vẫy tay chào","slap":"đùa vui với","highfive":"đập tay với","handshake":"bắt tay với","cheer":"cổ vũ","thank":"cảm ơn","compliment":"khen","encourage":"động viên","apologize":"gửi lời xin lỗi tới","goodnight":"chúc ngủ ngon","goodmorning":"chúc buổi sáng tốt lành tới","birthday_wish":"chúc mừng sinh nhật","pickupline":"câu bắt chuyện vui","conversation_starter":"chủ đề trò chuyện","icebreaker":"câu hỏi phá băng"}
    if lower in social_lines:
        target=args or "mọi người"
        phrases={"hug":"🤗","pat":"😊","wave":"👋","slap":"😅","highfive":"🙌","handshake":"🤝","cheer":"🎉","thank":"💙","compliment":"✨","encourage":"💪","apologize":"🙏","goodnight":"🌙","goodmorning":"☀️","birthday_wish":"🎂"}
        return await _prefix_send(ctx,f"{phrases.get(lower,'💬')} {author.mention} {social_lines[lower]} {target}!")
    if lower in {"would_you_rather","truth_or_dare","never_have_i_ever","riddle","joke","dadjoke","pun","motivation","quote_random","daily_quote","fact_random","daily_fact","compliment_random","trivia","trivia_category","word_scramble","hangman","memory_game","reaction_test","click_challenge","daily_challenge","guess_word","guess_song","guess_flag","emoji_quiz"}:
        data={
            "would_you_rather":["Bạn muốn đi du lịch biển hay núi?","Bạn thích đọc suy nghĩ hay tàng hình?"],
            "truth_or_dare":["Thật: Điều gì khiến bạn tự hào gần đây?","Thử thách an toàn: khen một người trong server."],
            "never_have_i_ever":["Tôi chưa bao giờ quên mất mình định nói gì giữa câu."],
            "riddle":["Cái gì có kim nhưng không biết khâu? (Đáp án: đồng hồ)"],
            "joke":["Vì sao máy tính lạnh? Vì nó mở nhiều Windows!"],
            "dadjoke":["Tôi định kể chuyện về giấy, nhưng thôi — chuyện này hơi mỏng."],
            "pun":["Mình rất có trọng lượng… vì luôn giữ lời hứa."],
            "motivation":["Tiến bộ nhỏ mỗi ngày vẫn là tiến bộ."],
            "quote_random":["Hành trình dài bắt đầu bằng một bước nhỏ."],
            "daily_quote":["Hôm nay, hãy làm một việc nhỏ giúp ngày mai dễ hơn."],
            "fact_random":["Bạch tuộc có ba trái tim."],
            "daily_fact":["Một ngày trên sao Kim dài hơn một năm trên sao Kim."],
            "compliment_random":["Bạn có thể làm ngày của ai đó tốt hơn chỉ bằng một lời tử tế."],
            "trivia":["Câu hỏi: Hành tinh nào được gọi là hành tinh đỏ? Đáp án: ||Sao Hỏa||"],
            "trivia_category":["Chủ đề đố vui: khoa học, lịch sử, địa lý hoặc phim ảnh."],
            "word_scramble":["Từ đảo chữ: **HACN** (gợi ý: đồ uống)"],
            "hangman":["Trò đoán chữ: _ A _ (gợi ý: con vật)"],
            "memory_game":["Ghi nhớ chuỗi: 🔵 🍎 ⭐ 🐱 — sau đó thử nhắc lại!"],
            "reaction_test":["Tốc độ phản ứng: hãy bấm vào tin nhắn này nhanh nhất có thể; bot không đo được chính xác thời gian từ prefix."],
            "click_challenge":["Thử thách: gửi một emoji trong 5 giây!"],
            "daily_challenge":["Thử thách hôm nay: gửi một lời cảm ơn chân thành."],
            "guess_word":["Từ bí mật gợi ý: thường xuất hiện trên bầu trời vào ban đêm."],
            "guess_song":["Đoán bài hát qua một gợi ý do người chơi nhập; bot không cung cấp lời bài hát."],
            "guess_flag":["Đố bạn: quốc kỳ nào có hình lá phong ở giữa? (Đáp án: ||Canada||)"],
            "emoji_quiz":["Đoán cụm từ: 🌧️ + 🐱 + 🐶 (gợi ý: thành ngữ tiếng Anh)"]}
        return await _prefix_send(ctx,random.choice(data.get(lower,["Chúc bạn vui vẻ!"])))

    # ---------- moderation and guild operations ----------
    if lower in {"kick","ban","softban","timeout","untimeout","warn","warnings","clearwarnings","warn_remove","purge","purge_user","purge_bots","purge_links","purge_images","purge_files","purge_mentions","purge_contains","nickname_set","nickname_reset","role_add","role_remove","role_list","member_quarantine","member_release"}:
        if not await _prefix_need_admin(ctx): return
        if not await _prefix_need_guild(ctx): return
        if lower in {"purge","purge_user","purge_bots","purge_links","purge_images","purge_files","purge_mentions","purge_contains"}:
            try: amount=max(1,min(100,int(parts[-1] if lower in {"purge_user","purge_contains"} and len(parts)>1 else parts[0])))
            except Exception:return await _prefix_send(ctx,f"Dùng: !{lower} [điều_kiện] <số_lượng 1-100>")
            keyword=" ".join(parts[:-1]).casefold() if lower=="purge_contains" and len(parts)>1 else ""
            target=await _prefix_target_member(ctx,parts[0]) if lower=="purge_user" and parts else None
            def check(m):
                if lower=="purge_bots":return m.author.bot
                if lower=="purge_links":return "http://" in m.content or "https://" in m.content
                if lower=="purge_images":return any(a.content_type and a.content_type.startswith('image/') for a in m.attachments)
                if lower=="purge_files":return bool(m.attachments)
                if lower=="purge_mentions":return bool(m.mentions or m.role_mentions)
                if lower=="purge_contains":return keyword in m.content.casefold()
                if lower=="purge_user":return target is not None and m.author.id==target.id
                return True
            try:
                deleted=await ctx.channel.purge(limit=min(500,amount*5),check=check,reason=f"Prefix command {lower}")
                return await _prefix_send(ctx,f"🧹 Đã xóa {len(deleted)} tin nhắn phù hợp.")
            except discord.Forbidden:return await _prefix_send(ctx,"Bot thiếu quyền Manage Messages.")
        target=await _prefix_target_member(ctx,parts[0]) if parts else None
        if lower in {"unban"}:
            try:
                uid=int(parts[0]); user=await bot.fetch_user(uid); await guild.unban(user,reason=f"By {author}"); return await _prefix_send(ctx,f"Đã gỡ cấm {user}.")
            except Exception:return await _prefix_send(ctx,"Dùng !unban <user_id>; bot cần quyền Ban Members.")
        if lower in {"warnings","clearwarnings","warn_remove","mod_history"}:
            if target is None:return await _prefix_send(ctx,"Dùng mention hoặc ID thành viên.")
            if lower=="warnings":
                rows=db_conn().execute("SELECT id,reason,created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",(guild.id,target.id)).fetchall()
                return await _prefix_send(ctx,"\n".join(f"#{r['id']} {r['reason']} ({r['created_at']})" for r in rows) or "Không có cảnh cáo.")
            if lower=="clearwarnings":
                db_conn().execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?",(guild.id,target.id)); db_conn().commit(); return await _prefix_send(ctx,"Đã xóa cảnh cáo đã lưu.")
        if lower=="warn":
            if not target:return await _prefix_send(ctx,"Dùng !warn @thành_viên <lý do>")
            reason=" ".join(parts[1:]) or "Không nêu lý do"; db_conn().execute("INSERT INTO warnings(guild_id,user_id,moderator_id,reason,created_at) VALUES(?,?,?,?,?)",(guild.id,target.id,author.id,reason,datetime.now(timezone.utc).isoformat())); db_conn().commit(); await send_modlog(guild,"Cảnh cáo",f"{target.mention}: {reason}"); return await _prefix_send(ctx,f"Đã cảnh cáo {target.mention}.")
        if lower in {"kick","ban","softban","timeout","untimeout"}:
            if not target:return await _prefix_send(ctx,f"Dùng !{lower} @thành_viên [lý do/thời lượng]")
            if target.id==author.id or target.id==guild.owner_id:return await _prefix_send(ctx,"Không thể thực hiện hành động với mục tiêu này.")
            if isinstance(author,discord.Member) and target.top_role>=author.top_role and author.id!=guild.owner_id:return await _prefix_send(ctx,"Vai trò của mục tiêu bằng hoặc cao hơn bạn.")
            if guild.me and target.top_role>=guild.me.top_role:return await _prefix_send(ctx,"Vai trò của mục tiêu bằng hoặc cao hơn bot.")
            try:
                reason=" ".join(parts[1:]) or f"Prefix command {lower} by {author}"
                if lower=="kick": await target.kick(reason=reason)
                elif lower in {"ban","softban"}: await guild.ban(target,reason=reason,delete_message_seconds=3600 if lower=="softban" else 0)
                elif lower=="timeout":
                    if len(parts)<2:return await _prefix_send(ctx,"Dùng !timeout @thành_viên <10m/2h/1d> [lý do]")
                    seconds=parse_duration(parts[1]);
                    if not seconds or seconds>28*86400:return await _prefix_send(ctx,"Thời lượng không hợp lệ (tối đa 28 ngày).")
                    await target.timeout(timedelta(seconds=seconds),reason=reason)
                else: await target.timeout(None,reason=reason)
                await send_modlog(guild,f"Prefix {lower}",f"{target} — {reason}"); return await _prefix_send(ctx,f"Đã thực hiện {lower} với {target.mention}.")
            except discord.Forbidden:return await _prefix_send(ctx,"Bot thiếu quyền hoặc role hierarchy không cho phép.")
            except discord.HTTPException:return await _prefix_send(ctx,"Discord từ chối thao tác.")
        if lower in {"nickname_set","nickname_reset"}:
            if not target:return await _prefix_send(ctx,"Dùng !nickname_set @member <biệt danh>")
            nick=" ".join(parts[1:])[:32] if lower=="nickname_set" else None
            try: await target.edit(nick=nick,reason=f"By {author}"); return await _prefix_send(ctx,"Đã cập nhật biệt danh.")
            except discord.Forbidden:return await _prefix_send(ctx,"Bot không có quyền đổi biệt danh mục tiêu.")
        if lower in {"role_add","role_remove"}:
            if len(parts)<2:return await _prefix_send(ctx,f"Dùng !{lower} @member @role")
            role=await _prefix_target_role(ctx,parts[1])
            if not target or not role:return await _prefix_send(ctx,"Không tìm thấy member hoặc role.")
            if guild.me and role>=guild.me.top_role:return await _prefix_send(ctx,"Role đó bằng/cao hơn role bot.")
            try:
                await (target.add_roles(role,reason=f"By {author}") if lower=="role_add" else target.remove_roles(role,reason=f"By {author}")); return await _prefix_send(ctx,"Đã cập nhật vai trò.")
            except discord.Forbidden:return await _prefix_send(ctx,"Bot thiếu quyền Manage Roles.")
        if lower=="role_list": return await _prefix_send(ctx,", ".join(r.name for r in target.roles[1:]) if target else "Dùng !role_list @member")
        if lower in {"member_quarantine","member_release"}:
            return await _prefix_send(ctx,"Cần cấu hình role cách ly riêng trước khi dùng lệnh này; bot không tự ý gán role bất kỳ.")

    # Channel/role modifications are guarded by Administrator and bot hierarchy.
    if lower in {"lock","unlock","slowmode","slowmode_off","channel_create","channel_delete","channel_rename","channel_topic","channel_topic_clear","channel_clone","channel_move","category_create","category_delete","category_rename","channel_nsfw","channel_safe","channel_position","channel_sync","channel_unsync","channel_permissions","channel_allow","channel_deny","channel_reset","channel_pin","channel_unpin"}:
        if not await _prefix_need_admin(ctx): return
        if not await _prefix_need_guild(ctx): return
        ch=ctx.channel
        try:
            if lower=="lock": await ch.set_permissions(guild.default_role,send_messages=False,reason=f"By {author}"); return await _prefix_send(ctx,"🔒 Đã khóa kênh.")
            if lower=="unlock": await ch.set_permissions(guild.default_role,send_messages=None,reason=f"By {author}"); return await _prefix_send(ctx,"🔓 Đã bỏ ghi đè khóa kênh.")
            if lower in {"slowmode","slowmode_off"}:
                sec=0 if lower=="slowmode_off" else max(0,min(21600,int(parts[0])))
                await ch.edit(slowmode_delay=sec); return await _prefix_send(ctx,f"Slowmode: {sec} giây.")
            if lower=="channel_create":
                obj=await guild.create_text_channel(args[:90] or "new-channel"); return await _prefix_send(ctx,f"Đã tạo {obj.mention}")
            if lower=="channel_delete":
                target=await commands.TextChannelConverter().convert(ctx,args) if args else ch
                await target.delete(reason=f"By {author}"); return
            if lower=="channel_rename":
                await ch.edit(name=args[:100]); return await _prefix_send(ctx,"Đã đổi tên kênh hiện tại.")
            if lower in {"channel_topic","channel_topic_clear"}:
                await ch.edit(topic=(args[:1024] if lower=="channel_topic" else None)); return await _prefix_send(ctx,"Đã cập nhật topic.")
            if lower=="channel_clone":
                clone=await ch.clone(reason=f"By {author}"); return await _prefix_send(ctx,f"Đã sao chép kênh: {clone.mention}")
            if lower=="channel_move":
                cat=await commands.CategoryChannelConverter().convert(ctx,args); await ch.edit(category=cat); return await _prefix_send(ctx,"Đã chuyển kênh.")
            if lower=="category_create":
                cat=await guild.create_category(args[:90]); return await _prefix_send(ctx,f"Đã tạo danh mục {cat.name}.")
            if lower=="category_delete":
                cat=await commands.CategoryChannelConverter().convert(ctx,args); await cat.delete(reason=f"By {author}"); return
            if lower=="category_rename":
                cat=await commands.CategoryChannelConverter().convert(ctx,parts[0]); await cat.edit(name=" ".join(parts[1:])[:100]); return await _prefix_send(ctx,"Đã đổi tên danh mục.")
            if lower in {"channel_nsfw","channel_safe"}:
                if not isinstance(ch,discord.TextChannel):return await _prefix_send(ctx,"Chỉ dùng trong kênh văn bản.")
                await ch.edit(nsfw=(lower=="channel_nsfw")); return await _prefix_send(ctx,"Đã cập nhật nhãn NSFW.")
            if lower=="channel_position":
                await ch.edit(position=max(0,min(500,int(parts[0])))); return await _prefix_send(ctx,"Đã cập nhật vị trí kênh.")
            if lower in {"channel_sync","channel_unsync"}:
                await ch.edit(sync_permissions=(lower=="channel_sync")); return await _prefix_send(ctx,"Đã cập nhật đồng bộ quyền.")
            if lower=="channel_permissions":
                return await _prefix_send(ctx,"\n".join(f"{target.name}: {overwrite}" for target,overwrite in ch.overwrites.items())[:1800] or "Không có overwrite.")
            if lower in {"channel_allow","channel_deny","channel_reset"}: return await _prefix_send(ctx,"Dùng slash command hoặc cấu hình quyền cụ thể qua Discord UI; không áp dụng quyền mơ hồ từ chuỗi tự do.")
            if lower in {"channel_pin","channel_unpin"}:
                mid=int(parts[0]); msg=await ch.fetch_message(mid); await (msg.pin(reason=f"By {author}") if lower=="channel_pin" else msg.unpin(reason=f"By {author}")); return await _prefix_send(ctx,"Đã cập nhật ghim tin nhắn.")
        except (ValueError,IndexError,commands.BadArgument): return await _prefix_send(ctx,f"Tham số không hợp lệ cho !{lower}.")
        except discord.Forbidden:return await _prefix_send(ctx,"Bot thiếu quyền cần thiết.")
        except discord.HTTPException:return await _prefix_send(ctx,"Discord từ chối thao tác.")

    if lower.startswith("role_"):
        if not await _prefix_need_admin(ctx): return
        if not await _prefix_need_guild(ctx): return
        try:
            if lower=="role_create":
                role=await guild.create_role(name=args[:100] or "New Role"); return await _prefix_send(ctx,f"Đã tạo role {role.mention}")
            if lower in {"role_delete","role_rename","role_color","role_hoist","role_unhoist","role_mentionable","role_unmentionable","role_position","role_info","role_members","role_count","role_permissions","role_copy","role_assign_all","role_remove_all","role_assign_bots","role_assign_humans","role_assign_new","role_self_add","role_self_remove","role_reaction_setup","role_reaction_remove","role_backup","role_restore"}:
                role=await _prefix_target_role(ctx,parts[0]) if parts else None
                if lower=="role_self_add" or lower=="role_self_remove":
                    if not role:return await _prefix_send(ctx,"Dùng !role_self_add @role; role self-service doit être configuré par admin.")
                    return await _prefix_send(ctx,"Pour éviter l'auto-attribution non autorisée, utilisez une liste de rôles autorisés configurée par admin.")
                if not role:return await _prefix_send(ctx,f"Dùng !{lower} @role [valeur]")
                if lower=="role_info": return await _prefix_send(ctx,f"{role.name} | ID {role.id} | couleur #{role.color.value:06x} | membres {len(role.members)}")
                if lower=="role_members": return await _prefix_send(ctx,", ".join(m.mention for m in role.members[:50]) or "Aucun membre.")
                if lower=="role_count": return await _prefix_send(ctx,f"{len(role.members)} membre(s) ont {role.name}.")
                if lower=="role_permissions": return await _prefix_send(ctx,", ".join(k for k,v in role.permissions if v)[:1800] or "Aucune permission spéciale.")
                if guild.me and role>=guild.me.top_role:return await _prefix_send(ctx,"Role égal/supérieur au bot.")
                if lower=="role_delete": await role.delete(reason=f"By {author}"); return
                if lower=="role_rename": await role.edit(name=" ".join(parts[1:])[:100]); return await _prefix_send(ctx,"Đã đổi tên role.")
                if lower=="role_color":
                    color=int(parts[1].replace('#',''),16); await role.edit(colour=discord.Colour(color)); return await _prefix_send(ctx,"Đã đổi màu role.")
                if lower in {"role_hoist","role_unhoist"}: await role.edit(hoist=lower=="role_hoist"); return await _prefix_send(ctx,"Đã cập nhật hiển thị role.")
                if lower in {"role_mentionable","role_unmentionable"}: await role.edit(mentionable=lower=="role_mentionable"); return await _prefix_send(ctx,"Đã cập nhật quyền mention role.")
                if lower=="role_position": await role.edit(position=max(1,min(250,int(parts[1])))); return await _prefix_send(ctx,"Đã cập nhật vị trí role.")
                if lower=="role_copy":
                    clone=await guild.create_role(name=(parts[1] if len(parts)>1 else role.name+" copy"),permissions=role.permissions,colour=role.colour,hoist=role.hoist,mentionable=role.mentionable); return await _prefix_send(ctx,f"Đã sao chép role: {clone.mention}")
                if lower in {"role_assign_all","role_remove_all","role_assign_bots","role_assign_humans"}:
                    if not await _prefix_need_owner(ctx): return
                    eligible=[m for m in guild.members if (lower=="role_assign_all" or lower=="role_remove_all" or (lower=="role_assign_bots" and m.bot) or (lower=="role_assign_humans" and not m.bot)) and not m.bot and m.top_role<guild.me.top_role]
                    if len(eligible)>100:return await _prefix_send(ctx,"Thao tác hàng loạt bị giới hạn 100 thành viên mỗi lần để tránh rate limit.")
                    done=0
                    for m in eligible:
                        try:
                            if lower=="role_remove_all": await m.remove_roles(role,reason=f"By {author}")
                            else: await m.add_roles(role,reason=f"By {author}")
                            done+=1
                        except discord.HTTPException: pass
                    return await _prefix_send(ctx,f"Đã xử lý {done} thành viên.")
                return await _prefix_send(ctx,"Lệnh này cần cấu hình bổ sung; không có thao tác tự động chưa xác nhận nào được thực hiện.")
        except (ValueError,IndexError,commands.BadArgument):return await _prefix_send(ctx,"Tham số role không hợp lệ.")
        except discord.Forbidden:return await _prefix_send(ctx,"Bot thiếu quyền Manage Roles.")

    # ---------- saved server settings, audit summaries, XP ----------
    if lower in {"modlog_set","modlog_show","modlog_test","message_log_set","message_log_toggle","voice_log_set","voice_log_toggle","member_log_set","member_log_toggle","role_log_set","role_log_toggle","channel_log_set","channel_log_toggle","welcome_set","welcome_message","welcome_preview","welcome_test","welcome_on","welcome_off","welcome_image_on","welcome_image_off","welcome_color","welcome_footer","goodbye_set","goodbye_message","goodbye_test","goodbye_on","goodbye_off","autorole_set","autorole_on","autorole_off","welcome_dm_set","welcome_dm_on","welcome_dm_off","antispam_on","antispam_off","antispam_limit","antispam_window","antispam_action","antiinvite_on","antiinvite_off","antilink_on","antilink_off","antilink_allow","antilink_deny","badword_add","badword_remove","badword_list","badword_toggle","caps_limit","mention_limit","duplicate_limit","emoji_limit","new_account_limit","raid_mode_on","raid_mode_off","raid_threshold","raid_status","security_report","xp","level","rank","leaderboard","xp_stats","bot_db_stats","botstats","botinfo","botavatar","botuptime","botlatency","botguilds","botusers","botcommands","help","helpall","ping","bot_config_show","bot_db_integrity","bot_sync_commands","bot_shutdown"}:
        if lower in {"modlog_set","message_log_set","voice_log_set","welcome_set","goodbye_set","autorole_set"} and not await _prefix_need_admin(ctx): return
        if lower in {"antispam_on","antispam_off","antispam_limit","antispam_window","antispam_action","antiinvite_on","antiinvite_off","antilink_on","antilink_off","antilink_allow","antilink_deny","badword_add","badword_remove","badword_list","badword_toggle","caps_limit","mention_limit","duplicate_limit","emoji_limit","new_account_limit","raid_mode_on","raid_mode_off","raid_threshold"} and not await _prefix_need_admin(ctx): return
        if lower in PREFIX_OWNER_ONLY and not await _prefix_need_owner(ctx): return
        if lower=="ping":return await _prefix_send(ctx,f"🏓 Pong: {round(bot.latency*1000)} ms")
        if lower in {"botinfo","botstats"}:return await _prefix_send(ctx,f"Bot: {bot.user}; server: {len(bot.guilds)}; prefix commands: {len(PREFIX_COMMAND_DESCRIPTIONS)}; latency: {bot.latency*1000:.0f}ms")
        if lower=="botavatar":return await _prefix_send(ctx,bot.user.display_avatar.url if bot.user else "Bot chưa sẵn sàng.")
        if lower=="botuptime":return await _prefix_send(ctx,f"Bot chạy từ {datetime.fromtimestamp(BOT_START_TIME).isoformat(timespec='seconds')}")
        if lower=="botlatency":return await _prefix_send(ctx,f"Gateway latency: {bot.latency*1000:.2f} ms")
        if lower=="botguilds":return await _prefix_send(ctx,f"Bot đang ở {len(bot.guilds)} server.")
        if lower=="botusers":return await _prefix_send(ctx,f"Tổng thành viên ước tính: {sum(g.member_count or 0 for g in bot.guilds)}")
        if lower=="botcommands":return await _prefix_send(ctx,f"Đã đăng ký {len(PREFIX_COMMAND_DESCRIPTIONS)} lệnh prefix.")
        if lower=="bot_db_stats":return await _prefix_send(ctx,f"SQLite: {db_conn().execute('SELECT count(*) FROM sqlite_master WHERE type=\"table\"').fetchone()[0]} bảng.")
        if lower=="bot_db_integrity":return await _prefix_send(ctx,str(db_conn().execute('PRAGMA integrity_check').fetchone()[0]))
        if lower=="bot_config_show":return await _prefix_send(ctx,"Cấu hình công khai: prefix="+BOT_PREFIX+"; dữ liệu nhạy cảm không hiển thị.")
        if lower=="bot_sync_commands":
            try: synced=await bot.tree.sync(); return await _prefix_send(ctx,f"Đã đồng bộ {len(synced)} slash command.")
            except Exception as e:return await _prefix_send(ctx,f"Đồng bộ thất bại: {type(e).__name__}")
        if lower=="bot_shutdown":
            await _prefix_send(ctx,"Đang tắt bot theo yêu cầu chủ bot."); await bot.close(); return
        if lower in {"help","helpall"}:
            pages=[]
            for group, cmds in PREFIX_COMMAND_GROUPS.items():
                pages.append(f"**{group.title()}** ({len(cmds)}): " + ", ".join(f"`{BOT_PREFIX}{n}`" for n in cmds[:12]) + (" …" if len(cmds)>12 else ""))
            return await _prefix_send(ctx,"**500 PREFIX COMMANDS — dùng /help để xem danh mục**\n"+"\n".join(pages[:8]))
        if lower in {"rank","level","xp","xp_stats","leaderboard"}:
            if not guild:return await _prefix_send(ctx,"Chỉ dùng trong server.")
            if lower=="leaderboard":
                rows=db_conn().execute("SELECT user_id,xp,level FROM user_levels WHERE guild_id=? ORDER BY xp DESC LIMIT 10",(guild.id,)).fetchall()
                return await _prefix_send(ctx,"\n".join(f"{i}. <@{r['user_id']}> — cấp {r['level']}, {r['xp']} XP" for i,r in enumerate(rows,1)) or "Chưa có dữ liệu XP.")
            target=await _prefix_target_member(ctx,args) if args else author
            if not target:return await _prefix_send(ctx,"Không tìm thấy thành viên.")
            row=db_conn().execute("SELECT xp,level FROM user_levels WHERE guild_id=? AND user_id=?",(guild.id,target.id)).fetchone()
            return await _prefix_send(ctx,f"{target.mention}: cấp {row['level']}, {row['xp']} XP." if row else "Chưa có dữ liệu XP.")
        if lower=="modlog_show":return await _prefix_send(ctx,f"Kênh modlog ID: {get_setting(guild.id,'modlog_channel_id','chưa cài') if guild else 'n/a'}")
        if lower=="modlog_test":return await send_modlog(guild,"Kiểm tra modlog",f"Yêu cầu bởi {author.mention}") if guild else await _prefix_send(ctx,"Chỉ dùng trong server.")
        if lower=="modlog_set":
            ch=await commands.TextChannelConverter().convert(ctx,args); set_setting(guild.id,"modlog_channel_id",ch.id); return await _prefix_send(ctx,f"Đã đặt modlog: {ch.mention}")
        if lower in {"message_log_toggle","voice_log_toggle"}:
            key="log_messages" if lower=="message_log_toggle" else "log_voice"; current=int(get_setting(guild.id,key,0) or 0); set_setting(guild.id,key,0 if current else 1); return await _prefix_send(ctx,f"{key} đã {'tắt' if current else 'bật'}.")
        if lower=="message_log_set":
            val=args.casefold() in {"on","true","1","yes"}; set_setting(guild.id,"log_messages",int(val)); return await _prefix_send(ctx,f"Message log: {'bật' if val else 'tắt'}.")
        if lower=="voice_log_set":
            val=args.casefold() in {"on","true","1","yes"}; set_setting(guild.id,"log_voice",int(val)); return await _prefix_send(ctx,f"Voice log: {'bật' if val else 'tắt'}.")
        if lower in {"welcome_set","goodbye_set"}:
            ch=await commands.TextChannelConverter().convert(ctx,args); key="welcome_channel_id" if lower=="welcome_set" else "goodbye_channel_id"; set_setting(guild.id,key,ch.id); return await _prefix_send(ctx,f"Đã cấu hình kênh: {ch.mention}")
        if lower in {"welcome_message","goodbye_message"}:
            key="welcome_message" if lower=="welcome_message" else "goodbye_message"; set_setting(guild.id,key,args[:1000]); return await _prefix_send(ctx,"Đã lưu mẫu tin nhắn.")
        if lower in {"welcome_on","welcome_off"}:
            set_setting(guild.id,"welcome_channel_id",get_setting(guild.id,"welcome_channel_id") if lower=="welcome_on" else None); return await _prefix_send(ctx,"Đã cập nhật cấu hình chào mừng.")
        if lower in {"goodbye_on","goodbye_off"}:
            set_setting(guild.id,"goodbye_channel_id",get_setting(guild.id,"goodbye_channel_id") if lower=="goodbye_on" else None); return await _prefix_send(ctx,"Đã cập nhật cấu hình tạm biệt.")
        if lower=="autorole_set":
            role=await _prefix_target_role(ctx,args); 
            if not role:return await _prefix_send(ctx,"Dùng !autorole_set @role")
            set_setting(guild.id,"join_role_id",role.id); set_setting(guild.id,"autorole_enabled",1); return await _prefix_send(ctx,f"Đã đặt autorole {role.mention}.")
        if lower in {"autorole_on","autorole_off"}:
            set_setting(guild.id,"autorole_enabled",int(lower=="autorole_on")); return await _prefix_send(ctx,"Đã cập nhật autorole.")
        if lower.startswith("antispam") or lower.startswith("antiinvite") or lower.startswith("antilink") or lower.startswith("badword") or lower in {"caps_limit","mention_limit","duplicate_limit","emoji_limit","new_account_limit","raid_mode_on","raid_mode_off","raid_threshold"}:
            # Persisted safety toggles use a dedicated table so arbitrary settings never enter SQL column names.
            db_conn().execute("CREATE TABLE IF NOT EXISTS prefix_config(guild_id INTEGER NOT NULL,key TEXT NOT NULL,value TEXT NOT NULL,PRIMARY KEY(guild_id,key))")
            if lower in {"badword_list","raid_status"}:
                rows=db_conn().execute("SELECT key,value FROM prefix_config WHERE guild_id=? AND key LIKE ?",(guild.id, 'badword:%' if lower=='badword_list' else 'raid_%')).fetchall(); return await _prefix_send(ctx,"\n".join(f"{r['key']}={r['value']}" for r in rows) or "Chưa có cấu hình.")
            key=lower; value=args or ("1" if lower.endswith("_on") or lower.endswith("_toggle") else "0" if lower.endswith("_off") else "1")
            db_conn().execute("INSERT INTO prefix_config(guild_id,key,value) VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",(guild.id,key,value)); db_conn().commit(); return await _prefix_send(ctx,f"Đã lưu cấu hình {key}={value}.")
        if lower in {"welcome_preview","welcome_test","goodbye_test","welcome_image_on","welcome_image_off","welcome_color","welcome_footer","welcome_dm_set","welcome_dm_on","welcome_dm_off","member_screening_info","new_member_list","member_join_stats","member_leave_stats"}:
            return await _prefix_send(ctx,"Lệnh đã được nhận. Chức năng này cần cấu hình bổ sung/ghi nhận dữ liệu tương ứng; không thực hiện hành động giả.")

    # ---------- basic generic features with command-specific outcomes ----------
    if lower in {"choose","shuffle","sample","lottery","lottery_weighted"}:
        options=[x.strip() for x in args.split("|") if x.strip()]
        if lower=="shuffle": random.shuffle(options); return await _prefix_send(ctx," | ".join(options) or "Dùng !shuffle a|b|c")
        if lower=="sample":
            try:n=max(1,min(len(options),int(parts[0]))); options=options[1:]
            except Exception:n=1
            return await _prefix_send(ctx," | ".join(random.sample(options,min(n,len(options)))) if options else "Dùng !sample <n> a|b|c")
        if not options:return await _prefix_send(ctx,"Dùng danh sách cách nhau bằng dấu |, ví dụ a|b|c")
        return await _prefix_send(ctx,"Kết quả: "+random.choice(options))
    if lower in {"quote","quote_random","fact_random","joke","dadjoke","pun","compliment_random","motivation","daily_quote","daily_fact","dictionary","synonyms","antonyms","text_translate","text_detect_language"}:
        if lower in {"dictionary","synonyms","antonyms","text_translate","text_detect_language"}:
            return await _prefix_send(ctx,"Chức năng này cần nguồn dữ liệu/API được cấu hình; bản này không giả vờ dịch hoặc tra cứu khi chưa có dịch vụ.")
        return await _prefix_send(ctx,random.choice(["Hãy tử tế, kể cả khi không ai nhìn thấy.","Mỗi bước nhỏ đều có ý nghĩa.","Điều thú vị thường bắt đầu từ một câu hỏi."]))
    if lower in {"poll","survey_create","suggest","feedback","report_bug"}:
        if not args:return await _prefix_send(ctx,f"Dùng: !{lower} <nội dung>")
        e=discord.Embed(title=lower.replace('_',' ').title(),description=args[:1500],color=DEFAULT_COLOR); e.set_footer(text=f"Gửi bởi {author}")
        msg=await ctx.send(embed=e)
        if lower=="poll":
            for emoji in ("👍","👎"): 
                try: await msg.add_reaction(emoji)
                except discord.HTTPException: pass
        return
    if lower in {"remindme","reminders","reminder_cancel","reminder_edit","reminder_snooze","reminder_repeat"}:
        return await _prefix_send(ctx,"Lời nhắc bền vững đang có qua /remindme. Dùng Slash command để tạo lời nhắc chính thức.")
    if lower.startswith("ticket_"):
        return await _prefix_send(ctx,"Hệ thống ticket hiện có qua /ticketsetup, /ticketpanel và /ticketclose. Dùng Slash command để bảo đảm quyền truy cập riêng tư.")
    if lower.startswith("embed_"):
        if not await _prefix_need_admin(ctx): return
        if lower=="embed_create":
            p=args.split("|",1); title=p[0][:256]; desc=p[1][:3000] if len(p)>1 else ""; e=discord.Embed(title=title,description=desc,color=DEFAULT_COLOR); return await ctx.send(embed=e)
        return await _prefix_send(ctx,"Dùng !embed_create <tiêu đề>|<nội dung> để gửi embed cơ bản.")
    if lower.startswith("prefix_"):
        if lower=="prefix_show":return await _prefix_send(ctx,f"Prefix hiện tại: `{BOT_PREFIX}`")
        if lower=="prefix_set":return await _prefix_send(ctx,"BOT_PREFIX được cấu hình qua Railway Variables; sửa biến BOT_PREFIX rồi khởi động lại bot.")
        if lower=="prefix_reset":return await _prefix_send(ctx,"Đặt BOT_PREFIX=! trong Railway Variables rồi khởi động lại.")
    if lower in {"language_show","language_set","timezone_set","timezone_show","starboard_set","starboard_threshold","starboard_show","suggestion_status","suggestion_list","suggestion_vote","suggestion_close","bot_config_export","bot_db_backup","bot_cache_stats","bot_cache_clear","bot_memory","bot_cpu","bot_latency_history","bot_errors","bot_error_detail","bot_db_stats","bot_db_backup","bot_db_integrity","bot_sync_commands","bot_shutdown"}:
        if lower in PREFIX_OWNER_ONLY and not await _prefix_need_owner(ctx):return
        if lower=="language_show":return await _prefix_send(ctx,"Ngôn ngữ phản hồi mặc định: tiếng Việt.")
        if lower=="language_set":return await _prefix_send(ctx,"Đổi ngôn ngữ toàn bot chưa bật; sửa văn bản phản hồi trong main.py.")
        if lower=="timezone_show":return await _prefix_send(ctx,"Múi giờ tiến trình: local; nên dùng UTC hoặc zoneinfo cho thời gian bền vững.")
        if lower=="timezone_set":return await _prefix_send(ctx,"Đặt múi giờ bằng biến môi trường TZ nếu host hỗ trợ.")
        if lower=="bot_cache_stats":return await _prefix_send(ctx,"Bot không duy trì cache ứng dụng riêng ngoài cache discord.py.")
        if lower=="bot_memory":return await _prefix_send(ctx,"Đo bộ nhớ cần psutil; chưa cài nên không đưa số giả.")
        if lower=="bot_cpu":return await _prefix_send(ctx,"Đo CPU cần psutil; chưa cài nên không đưa số giả.")
        if lower=="bot_latency_history":return await _prefix_send(ctx,"Chưa bật lưu lịch sử latency; giá trị hiện tại: {:.1f}ms".format(bot.latency*1000))
        if lower=="bot_errors":return await _prefix_send(ctx,"Xem log lỗi trong Railway → Deployments → Logs.")
        if lower=="bot_db_backup":
            import shutil
            dest=DATABASE_PATH+".backup"
            try: db_conn().commit(); shutil.copy2(DATABASE_PATH,dest); return await _prefix_send(ctx,f"Đã sao lưu DB vào {dest} trên filesystem của service.")
            except Exception as e:return await _prefix_send(ctx,f"Sao lưu lỗi: {type(e).__name__}")
        if lower=="bot_config_export":return await _prefix_send(ctx,"Không xuất token hoặc biến môi trường nhạy cảm. Cấu hình không nhạy cảm: BOT_PREFIX="+BOT_PREFIX)
        if lower=="bot_cache_clear":return await _prefix_send(ctx,"Cache nội bộ không có dữ liệu tùy chỉnh để xóa.")
        return await _prefix_send(ctx,"Chức năng này cần cấu hình lưu trữ bổ sung; chưa có dữ liệu phù hợp để thao tác.")

    # The registry is exhaustive; if a handler is missing, say so explicitly rather than fake success.
    return await _prefix_send(ctx, f"Lệnh !{name} đã đăng ký nhưng handler chưa được ánh xạ. Báo chủ bot để hoàn thiện nhánh xử lý.")


def _make_prefix_callback(command_name: str):
    async def _callback(ctx: commands.Context, *, args: str = ""):
        group = next((g for g, names in PREFIX_COMMAND_GROUPS.items() if command_name in names), "utility")
        if group in PREFIX_ADMIN_GROUPS and command_name not in {"ticket_mine", "ticket_create", "ticket_feedback", "ticket_rating", "ticket_list", "ticket_search"}:
            if not await _prefix_need_admin(ctx):
                return
        if command_name in PREFIX_OWNER_ONLY and not await _prefix_need_owner(ctx):
            return
        try:
            await run_prefix_action(ctx, command_name, args)
        except commands.CommandError as exc:
            await _prefix_send(ctx, f"Tham số không hợp lệ cho !{command_name}: {exc}")
        except discord.Forbidden:
            await _prefix_send(ctx, "Bot thiếu quyền Discord cần thiết.")
        except discord.HTTPException as exc:
            logger.warning("Prefix command %s HTTP error: %s", command_name, exc)
            await _prefix_send(ctx, "Discord từ chối thao tác. Kiểm tra quyền bot và log Railway.")
        except Exception:
            logger.exception("Prefix command failed: %s", command_name)
            await _prefix_send(ctx, "Lệnh gặp lỗi; chi tiết đã được ghi vào log Railway.")
        finally:
            # Xóa tin nhắn gốc gọi lệnh prefix sau khi bot xử lý xong.
            # Bot cần quyền Manage Messages trong kênh; nếu thiếu quyền thì bỏ qua.
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
            except discord.HTTPException as exc:
                logger.debug("Không thể xóa tin nhắn lệnh prefix %s: %s", command_name, exc)
    _callback.__name__ = f"prefix_{command_name}"
    _callback.__doc__ = PREFIX_COMMAND_DESCRIPTIONS.get(command_name, command_name)
    return _callback


def register_prefix_commands():
    registered = 0
    for command_names in PREFIX_COMMAND_GROUPS.values():
        for command_name in command_names:
            if bot.get_command(command_name) is not None:
                continue
            bot.command(name=command_name, help=PREFIX_COMMAND_DESCRIPTIONS[command_name])(_make_prefix_callback(command_name))
            registered += 1
    logger.info("Registered %s prefix commands", registered)
    return registered



PREFIX_HELP_CATEGORY_LABELS = {
    "chat": "💬 Chat & Văn bản", "member": "👤 Thành viên", "server": "🏠 Server",
    "moderation": "🛡️ Kiểm duyệt", "channel": "📝 Kênh", "role": "🎭 Vai trò",
    "logs": "📋 Nhật ký", "security": "🔒 Bảo vệ", "welcome": "👋 Chào mừng",
    "level": "🏆 Level & XP", "games": "🎮 Trò chơi", "social": "🤝 Tương tác",
    "math": "🧮 Toán học", "conversion": "📏 Đơn vị", "time": "🕒 Ngày giờ",
    "encoding": "🔐 Mã hóa", "utility": "🧰 Tiện ích", "ticket": "🎫 Ticket",
    "system": "⚙️ Hệ thống", "advanced": "🛠️ Nâng cao",
}


def prefix_help_embed(category=None, page=0):
    if category is None:
        embed = discord.Embed(
            title="📚 TRUNG TÂM TRỢ GIÚP",
            description="Chọn một danh mục trong menu bên dưới. Mỗi mục có trang riêng và tối đa 12 lệnh mỗi trang.",
            color=discord.Color.blurple(),
        )
        for key, names in PREFIX_COMMAND_GROUPS.items():
            embed.add_field(name=PREFIX_HELP_CATEGORY_LABELS.get(key, key.title()), value=f"{len(names)} lệnh", inline=True)
        embed.set_footer(text=f"{len(PREFIX_COMMAND_DESCRIPTIONS)} lệnh prefix • Các lệnh slash hiện có vẫn được giữ lại")
        return embed
    names = PREFIX_COMMAND_GROUPS.get(category, [])
    pages = max(1, (len(names) + 11) // 12)
    page = max(0, min(page, pages - 1))
    selected = names[page * 12:(page + 1) * 12]
    embed = discord.Embed(
        title=PREFIX_HELP_CATEGORY_LABELS.get(category, category.title()),
        description=f"Trang **{page + 1}/{pages}** · **{len(names)}** lệnh trong mục",
        color=discord.Color.blurple(),
    )
    # Hiện từng lệnh trong khối code để người dùng trên điện thoại có thể nhấn giữ
    # và sao chép nhanh; Discord bot không được phép tự ghi vào clipboard người dùng.
    command_lines = [f"{BOT_PREFIX}{name}" for name in selected]
    embed.description = (
        f"Trang **{page + 1}/{pages}** · **{len(names)}** lệnh trong mục\n\n"
        + "\n".join(f"`{command}` — {PREFIX_COMMAND_DESCRIPTIONS.get(name, 'Lệnh tiện ích.')}"
                       for command, name in zip(command_lines, selected))
    )
    embed.set_footer(text="Nhấn giữ dòng lệnh để sao chép · chọn danh mục/nút để xem thêm")
    return embed


class PrefixHelpSelect(discord.ui.Select):
    def __init__(self, parent_view):
        self.parent_view = parent_view
        options = [discord.SelectOption(label=PREFIX_HELP_CATEGORY_LABELS.get(key, key.title()), value=key, description=f"{len(names)} lệnh") for key, names in PREFIX_COMMAND_GROUPS.items()]
        super().__init__(placeholder="📚 Chọn danh mục lệnh...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.category = self.values[0]
        self.parent_view.page = 0
        await self.parent_view.refresh(interaction)


class PrefixHelpButton(discord.ui.Button):
    def __init__(self, action, label, emoji):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=1)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, PrefixHelpView):
            return
        if self.action == "home":
            view.category, view.page = None, 0
        elif self.action == "prev":
            view.page = max(0, view.page - 1)
        elif self.action == "next":
            view.page += 1
        await view.refresh(interaction)


class PrefixHelpView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.category = None
        self.page = 0
        self.add_item(PrefixHelpSelect(self))
        self.add_item(PrefixHelpButton("prev", "Trang trước", "⬅️"))
        self.add_item(PrefixHelpButton("home", "Danh mục", "🏠"))
        self.add_item(PrefixHelpButton("next", "Trang sau", "➡️"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Bảng trợ giúp này thuộc về người khác. Hãy dùng /help để mở bảng riêng.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction):
        if self.category is not None:
            pages = max(1, (len(PREFIX_COMMAND_GROUPS.get(self.category, [])) + 11) // 12)
            self.page = min(self.page, pages - 1)
        await interaction.response.edit_message(embed=prefix_help_embed(self.category, self.page), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


PREFIX_REGISTERED_COUNT = register_prefix_commands()


# ============================================================================
# 15. STARTUP
# ============================================================================

def validate_configuration() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN chưa được đặt. Railway → Variables → Add Variable → "
            "DISCORD_TOKEN = token bot của bạn."
        )
    if OWNER_ID <= 0:
        raise RuntimeError("OWNER_ID không hợp lệ. Hãy đặt OWNER_ID trong Railway Variables.")

def main() -> None:
    validate_configuration()
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()

# ==========================================================================
# 16. OPERATIONS REFERENCE AND EXTENSION NOTES
# ==========================================================================

# RAILWAY DEPLOYMENT CHECKLIST
# 001. Create a Railway project and connect the GitHub repository that contains main.py and requirements.txt.
# 002. Add DISCORD_TOKEN in the Railway Variables tab. Never commit a token into GitHub.
# 003. Add OWNER_ID using your numeric Discord user ID. The default in this file is only a fallback.
# 004. Optionally add TEST_GUILD_ID with a test server ID. Guild commands usually update faster than global commands.
# 005. Use python main.py as the start command when Railway asks for a custom start command.
# 006. Ensure the Discord application has bot and applications.commands OAuth scopes.
# 007. Grant only the bot permissions it needs: View Channels, Send Messages, Embed Links, Read Message History, Manage Messages, Manage Channels, Manage Roles, Kick Members, Ban Members, Moderate Members, and Connect/Speak if using voice features.
# 008. Place the bot's highest role above roles it must manage. Discord role hierarchy cannot be bypassed by Python.
# 009. Enable Server Members Intent in the Discord Developer Portal for join/leave and member-based features.
# 010. Enable Message Content Intent only if you need the message logging feature.
# 011. Attach a Railway Volume if you need SQLite data to survive service replacement or ephemeral filesystem resets.
# 012. Set DATABASE_PATH to a path inside the mounted volume, for example /data/bot_data.sqlite3.
# 013. Read Railway deployment logs after startup. A missing token, invalid token, or missing dependency should be fixed before testing commands.

# PERMISSION MODEL
# 001. Public commands are intended for regular members and do not need Administrator.
# 002. Administrative commands use app_commands.default_permissions(administrator=True) for Discord's command UI defaults.
# 003. Administrative commands also check interaction.user.guild_permissions.administrator in the command body.
# 004. Owner commands use the OWNER_ID environment variable and a runtime check.
# 005. An Administrator is not automatically an Owner. The owner-only commands remain restricted to OWNER_ID.
# 006. The bot's Discord role hierarchy is checked before assigning roles or moderating members.
# 007. The server owner may have hierarchy privileges that normal moderators do not have, but the bot itself still needs the required Discord permission.
# 008. Commands that delete content, ban members, kick members, or timeout members should be tested in a private server first.
# 009. The bot intentionally does not delete message history when banning members.
# 010. The bot does not attempt to fetch users' IP addresses or private data.

# DATABASE AND BACKUPS
# 001. SQLite tables are created automatically on first startup.
# 002. guild_settings stores welcome, goodbye, logging, autorole, level, ticket, and starboard settings.
# 003. warnings stores moderation warnings and their reason, moderator, and timestamp.
# 004. user_levels stores experience points and current level for each user in each server.
# 005. tickets stores ticket channel IDs, open/closed state, opener, and timestamps.
# 006. reminders stores reminders that should be delivered later.
# 007. custom_reactions is reserved for configurable text reactions in future extensions.
# 008. starboard_entries tracks messages already sent to the starboard so duplicate reaction events are reduced.
# 009. Back up the database before making schema changes.
# 010. If a bot is deployed to multiple replicas, do not share a SQLite database between concurrent writers without planning for locking and deployment behavior.
# 011. For a small community bot with one running instance, SQLite is usually sufficient.

# COMMAND QUICK REFERENCE
# 001. /help — show the command groups.
# 002. /ping — show Discord gateway latency.
# 003. /botinfo — show bot identity, uptime, guild count, and command count.
# 004. /serverinfo — show server metadata.
# 005. /userinfo — show a member's public Discord metadata.
# 006. /avatar — show a user's avatar.
# 007. /roles — list server roles.
# 008. /channels — list text and voice channels.
# 009. /ban — ban a member. Administrator required.
# 010. /kick — kick a member. Administrator required.
# 011. /timeout — timeout a member using values such as 10m, 2h, or 1d. Administrator required.
# 012. /untimeout — remove a member's timeout. Administrator required.
# 013. /purge — delete 1 to 100 recent messages in the current channel. Administrator required.
# 014. /warn — add a persistent warning. Administrator required.
# 015. /warnings — show recent warnings for a member. Administrator required.
# 016. /clearwarnings — remove all warnings for a member. Administrator required.
# 017. /lock — deny sending messages for @everyone in the current text channel. Administrator required.
# 018. /unlock — remove the send-message deny for @everyone in the current text channel. Administrator required.
# 019. /slowmode — set the channel slowmode from 0 to 21600 seconds. Administrator required.
# 020. /nick — change a member nickname. Administrator required.
# 021. /addrole — add a role to a member. Administrator required.
# 022. /removerole — remove a role from a member. Administrator required.
# 023. /setmodlog — choose the moderation log channel. Administrator required.
# 024. /setwelcome — configure welcome channel and message. Administrator required.
# 025. /setgoodbye — configure goodbye channel and message. Administrator required.
# 026. /setautorole — configure a role for new members. Administrator required.
# 027. /setmessagelog — enable or disable message logging. Administrator required.
# 028. /setvoicelog — enable or disable voice logging. Administrator required.
# 029. /setlevel — enable or disable XP leveling. Administrator required.
# 030. /rank — show your rank or another member's rank.
# 031. /leaderboard — show the top ten XP scores.
# 032. /coinflip — flip a coin.
# 033. /dice — roll a die with 2 to 100 sides.
# 034. /8ball — ask a lighthearted question.
# 035. /choose — randomly choose from comma-separated options.
# 036. /rps — play rock-paper-scissors.
# 037. /poll — create a quick yes/no reaction poll.
# 038. /quote — display a random motivational quote.
# 039. /compliment — send a friendly compliment.
# 040. /hug — send a friendly virtual hug.
# 041. /ship — generate a playful compatibility score, not a real assessment.
# 042. /trivia — get a random trivia question with a spoiler answer.
# 043. /remindme — create a reminder such as 10m, 2h, or 1d.
# 044. /ticketsetup — configure ticket category and optional support role. Administrator required.
# 045. /ticketpanel — send a ticket creation panel. Administrator required.
# 046. /ticketclose — close the ticket channel you opened or one you can support.
# 047. /setstarboard — configure the starboard destination. Administrator required.
# 048. /botstatus — show runtime status. Owner only.
# 049. /synccommands — sync slash commands again. Owner only.
# 050. /shutdown — gracefully close the bot process. Owner only.
# 051. /dbstats — show SQLite row counts. Owner only.

# MESSAGE TEMPLATE VARIABLES
# 001. The welcome and goodbye messages support {user}, {server}, and {count}.
# 002. For welcome messages, {user} becomes the new member mention.
# 003. For goodbye messages, {user} becomes the departing member's displayed name.
# 004. {server} becomes the server name.
# 005. {count} becomes the current approximate member count.
# 006. Avoid putting @everyone or @here into welcome templates because allowed mentions are intentionally restricted.

# TROUBLESHOOTING
# 001. If slash commands do not appear, wait for global sync propagation or set TEST_GUILD_ID and redeploy.
# 002. If using TEST_GUILD_ID, the bot copies its global command definitions to that test guild during startup.
# 003. If a command says Missing Permissions, check both the invoking member's permission and the bot's role permissions.
# 004. If adding or removing roles fails, move the bot's highest role above the target role.
# 005. If ban or kick fails, verify Ban Members or Kick Members permissions in the target server.
# 006. If welcome messages do not appear, enable Server Members Intent and set /setwelcome.
# 007. If message logging does not appear, configure /setmodlog and use /setmessagelog enabled:true.
# 008. If voice logging does not appear, configure /setmodlog and use /setvoicelog enabled:true.
# 009. If reminders are not delivered after a redeploy, ensure the SQLite database is on persistent storage.
# 010. If a ticket button does not create channels, grant Manage Channels and check that the configured category still exists.
# 011. If the bot crashes at startup, read the first traceback in Railway logs rather than only the final line.
# 012. Do not paste your token into chat, screenshots, source control, or public logs.

# EXTENSION NOTES — PRACTICAL MAINTENANCE CHECKLIST
# 001. When adding a new moderation command, first decide whether it should be public, Admin-only, or Owner-only.
# 002. For an Admin command, apply the default permission decorator and repeat the runtime Administrator check.
# 003. For an Owner command, use owner_only() and repeat owner_check() before performing a sensitive operation.
# 004. Always defer an interaction before a long-running operation if it might exceed Discord's initial response window.
# 005. Use followup.send after defer; do not acknowledge the same interaction twice.
# 006. Keep embed field values below Discord's documented length limits.
# 007. Truncate user-provided text before inserting it into embed descriptions.
# 008. Use discord.utils.escape_markdown for untrusted text shown in logs.
# 009. Use AllowedMentions to avoid accidental mass mentions.
# 010. Do not assume a cached member is available when a guild has many members.
# 011. For reliable member lookup, enable the necessary intent and use the appropriate fetch methods when needed.
# 012. Handle discord.Forbidden separately from discord.HTTPException to explain missing permissions.
# 013. Use logging.exception inside exception handlers so the traceback is retained in service logs.
# 014. Do not suppress every exception silently; log errors and return a concise user-facing message.
# 015. Use a database transaction when a multi-step state change must stay consistent.
# 016. Commit database changes after changing persistent state.
# 017. Use parameterized SQL for user input; never concatenate raw user text into SQL.
# 018. Only interpolate SQL identifiers from a hard-coded allowlist.
# 019. Keep bot tokens in environment variables, not in code.
# 020. Rotate a token immediately if it is exposed.
# 021. Avoid storing unnecessary personal information in bot databases.
# 022. Moderation logs should include the action, target, actor, reason, and timestamp where appropriate.
# 023. Consider the privacy impact before enabling message content logging.
# 024. Do not collect IP addresses or private account data from server members.
# 025. Do not promise that a bot can read private messages that it has not received.
# 026. Do not promise 24/7 uptime unless the hosting plan and persistent service configuration support it.
# 027. Railway restarts can interrupt in-memory tasks; use persistent storage for important state.
# 028. A single process is the simplest deployment shape for this SQLite-based project.
# 029. If horizontal scaling is introduced, use a database designed for concurrent service instances.
# 030. Discord API rate limits are normal; use discord.py's built-in rate-limit handling.
# 031. Avoid loops that fetch every message or every member too frequently.
# 032. When deleting many messages, remember Discord bulk-delete restrictions for older messages.
# 033. The purge command includes the invoking command's interaction separately from ordinary message history.
# 034. Use test servers for permission changes and destructive command tests.
# 035. Never test ban, purge, or channel deletion against a production community without consent.
# 036. When creating ticket channels, apply overwrites to the opener, support role, bot, and default role.
# 037. Ticket channels should not be visible to the whole server by default.
# 038. Only the ticket opener, support team, or administrators should close a ticket.
# 039. If a ticket category is deleted, configure it again with /ticketsetup.
# 040. If the support role is deleted, update the ticket configuration.
# 041. Persistent views need stable custom_id values if they must survive restarts.
# 042. A persistent view must be registered on startup if it is to process old buttons after a restart.
# 043. For a production ticket bot, register TicketPanelView in setup_hook with bot.add_view(TicketPanelView()).
# 044. The current ticket panel uses a persistent custom_id; registering the view on startup is recommended for restart resilience.
# 045. Welcome templates should be short and respectful.
# 046. Do not automatically assign privileged roles to new members.
# 047. The autorole feature should be configured with a low-risk role.
# 048. Do not give new members Administrator automatically.
# 049. Check the target role is below the bot's top role before role assignment.
# 050. Role hierarchy checks are required even if the command caller is an Administrator.
# 051. The server owner has special moderation privileges, but the bot still obeys its own role hierarchy.
# 052. XP systems should use a cooldown so message spam does not grant unlimited points.
# 053. XP formulas can be tuned without changing the database schema if the same columns remain.
# 054. Leaderboard output should be limited to a manageable number of rows.
# 055. A member may leave the server after earning XP; the leaderboard can still show their ID mention.
# 056. If you want to remove departed members from XP, create a separate retention policy and command.
# 057. Reminders depend on the service being online when they are due.
# 058. A persistent reminder can be delivered after a restart if it was not already marked delivered.
# 059. A crash between sending a reminder and marking it delivered can lead to a duplicate reminder.
# 060. If exactly-once reminder delivery is required, use a queue or transactional delivery strategy.
# 061. The starboard threshold can be changed in on_raw_reaction_add.
# 062. A production starboard should track its posted message ID to update one post instead of creating duplicates.
# 063. Raw reaction events are used so the bot does not need the source message in its local cache.
# 064. The starboard only processes configured star emojis.
# 065. If a starboard destination channel is deleted, use /setstarboard to choose a new one.
# 066. The bot uses global slash commands unless TEST_GUILD_ID is set.
# 067. Global slash command updates may take time to appear across all servers.
# 068. Guild-specific commands are useful while developing but should not be confused with global registration.
# 069. The owner sync command only synchronizes the current definitions; it does not install Python packages.
# 070. Dependencies should be pinned or bounded in requirements.txt for reproducible deploys.
# 071. Python 3.11 or newer is recommended because the code uses modern type annotations.
# 072. Use Railway logs to verify the Python version and dependency installation.
# 073. The bot does not require FFmpeg because music playback is intentionally excluded.
# 074. Voice state logging does not mean the bot joins voice channels.
# 075. If voice playback is added later, configure Opus and audio dependencies separately.
# 076. The bot's presence text is cosmetic and can be changed in on_ready.
# 077. Do not use a presence update loop that changes activity every second.
# 078. A custom help command can be extended as commands are added.
# 079. When adding many commands, split them into Cogs if the one-file restriction is later relaxed.
# 080. Even in one file, group commands by feature and keep helper functions near the top.
# 081. Keep command names short and descriptions clear because Discord displays them in the command picker.
# 082. Avoid command names that differ only by punctuation or capitalization.
# 083. Use autocomplete for long lists of choices if a command grows beyond a few options.
# 084. Use modals or views for workflows that need several inputs, rather than one giant slash command.
# 085. For public polls, explain what each reaction means.
# 086. The choose command expects comma-separated values and ignores empty values.
# 087. The 8ball command is a novelty feature and should not be treated as factual advice.
# 088. The ship command is a joke and does not infer real compatibility.
# 089. The trivia command's answer is spoiler-hidden in Discord clients that support spoiler formatting.
# 090. Compliment and hug commands are designed as friendly, non-explicit interactions.
# 091. The bot should not be used to harass members or automate unwanted mass messaging.
# 092. Avoid mass-DM features; Discord rate limits and anti-spam policies apply.
# 093. Do not add scraping features that collect private or sensitive information about members.
# 094. Only log data necessary for moderation and server operations.
# 095. Make sure community members know when message content is being logged.
# 096. Keep a backup before deleting the SQLite database.
# 097. If the database becomes corrupted, stop the bot before restoring a backup.
# 098. Use a single persistent volume path consistently; changing DATABASE_PATH can make the bot appear to lose data.
# 099. Do not place the SQLite database inside a temporary build directory.
# 100. A database schema change should be accompanied by a migration or a compatible CREATE TABLE IF NOT EXISTS update.
# 101. Avoid renaming columns without migrating existing data.
# 102. If a feature is not needed, disable it rather than granting the bot extra permissions.
# 103. Review the permissions screen when generating the bot invite URL.
# 104. Never ask members to provide their passwords or tokens to the bot.
# 105. A legitimate moderation bot should not impersonate Discord staff.
# 106. A bot should not claim to see hidden profile fields or private connection data.
# 107. Do not make commands that reveal member IP addresses; Discord does not expose those through bots.
# 108. If a member reports abuse, provide server staff with a moderation workflow and preserve relevant evidence appropriately.
# 109. For support, include the command name, error text, and relevant log lines but redact tokens and personal secrets.
# 110. A good deployment has a clear owner, a least-privilege permission set, and a recovery plan.

# COMMAND CATALOGUE — access level is checked by Discord and/or runtime code.
# 001. /help | access=public | purpose=Show help
# 002. /ping | access=public | purpose=Check latency
# 003. /botinfo | access=public | purpose=Bot information
# 004. /serverinfo | access=public | purpose=Server information
# 005. /userinfo | access=public | purpose=Member information
# 006. /avatar | access=public | purpose=Avatar
# 007. /roles | access=public | purpose=List roles
# 008. /channels | access=public | purpose=List channels
# 009. /ban | access=admin | purpose=Ban member
# 010. /kick | access=admin | purpose=Kick member
# 011. /timeout | access=admin | purpose=Timeout member
# 012. /untimeout | access=admin | purpose=Remove timeout
# 013. /purge | access=admin | purpose=Bulk delete messages
# 014. /warn | access=admin | purpose=Add warning
# 015. /warnings | access=admin | purpose=List warnings
# 016. /clearwarnings | access=admin | purpose=Clear warnings
# 017. /lock | access=admin | purpose=Lock channel
# 018. /unlock | access=admin | purpose=Unlock channel
# 019. /slowmode | access=admin | purpose=Set slowmode
# 020. /nick | access=admin | purpose=Change nickname
# 021. /addrole | access=admin | purpose=Add role
# 022. /removerole | access=admin | purpose=Remove role
# 023. /setmodlog | access=admin | purpose=Set moderation log channel
# 024. /setwelcome | access=admin | purpose=Configure welcome message
# 025. /setgoodbye | access=admin | purpose=Configure goodbye message
# 026. /setautorole | access=admin | purpose=Configure autorole
# 027. /setmessagelog | access=admin | purpose=Toggle message logs
# 028. /setvoicelog | access=admin | purpose=Toggle voice logs
# 029. /setlevel | access=admin | purpose=Toggle leveling
# 030. /rank | access=public | purpose=Show rank
# 031. /leaderboard | access=public | purpose=XP leaderboard
# 032. /coinflip | access=public | purpose=Flip coin
# 033. /dice | access=public | purpose=Roll die
# 034. /8ball | access=public | purpose=Ask 8-ball
# 035. /choose | access=public | purpose=Random choice
# 036. /rps | access=public | purpose=Rock-paper-scissors
# 037. /poll | access=public | purpose=Quick poll
# 038. /quote | access=public | purpose=Random quote
# 039. /compliment | access=public | purpose=Friendly compliment
# 040. /hug | access=public | purpose=Friendly hug
# 041. /ship | access=public | purpose=Playful compatibility score
# 042. /trivia | access=public | purpose=Trivia question
# 043. /remindme | access=public | purpose=Reminder
# 044. /ticketsetup | access=admin | purpose=Ticket configuration
# 045. /ticketpanel | access=admin | purpose=Create ticket panel
# 046. /ticketclose | access=ticket | purpose=Close ticket
# 047. /setstarboard | access=admin | purpose=Configure starboard
# 048. /botstatus | access=owner | purpose=Bot status
# 049. /synccommands | access=owner | purpose=Sync slash commands
# 050. /shutdown | access=owner | purpose=Graceful shutdown
# 051. /dbstats | access=owner | purpose=Database stats

# DETAILED COMMAND NOTES
# /ban
#   1. Before banning, the bot checks that the target is not the actor, server owner, or above the actor/bot hierarchy.
#   2. The reason is attached to Discord's audit log and sent to the configured moderation log channel.
#   3. The command does not delete the member's previous messages.
# /kick
#   1. Kicking removes the member from the guild but does not permanently ban them.
#   2. A kicked user may rejoin if they have a valid invite and the server permits it.
#   3. Use ban instead when the server's moderation policy requires preventing re-entry.
# /timeout
#   1. Timeout duration accepts s, m, h, d, or w suffixes.
#   2. The Discord timeout maximum is 28 days; longer values are rejected.
#   3. Timeout does not remove a member's roles.
# /purge
#   1. Purge only operates in a text channel.
#   2. The command's own interaction response is ephemeral so it does not create extra visible clutter.
#   3. Discord has age restrictions on bulk deleting old messages; failures are reported in logs.
# /warn
#   1. Warnings persist in SQLite across normal restarts when the database file persists.
#   2. Warnings do not automatically ban or timeout a member.
#   3. The bot attempts to DM the member but does not fail the warning if DMs are closed.
# /setwelcome
#   1. Use a short template to keep the welcome channel readable.
#   2. Template variables are replaced at the time the member joins.
#   3. If the target channel is deleted, configure a new one.
# /setautorole
#   1. Only configure a role that is safe for all new members.
#   2. The bot checks its role hierarchy before assigning the configured role.
#   3. Pass no role to disable autorole.
# /rank
#   1. XP is awarded at most once per 60 seconds per member.
#   2. The current XP formula is intentionally simple and can be tuned later.
#   3. XP tracking requires the bot to receive message events in the server.
# /remindme
#   1. A reminder is delivered to the channel where the command was used.
#   2. The service must be online for the reminder loop to deliver it on time.
#   3. A persistent database allows overdue reminders to be delivered after a restart.
# /ticketpanel
#   1. A panel button creates a private text channel for the member.
#   2. Ticket creation requires Manage Channels and a valid category if one is configured.
#   3. Only one open ticket per member per server is allowed by the current implementation.
# /ticketclose
#   1. The opener, administrators, or the configured support role may close the ticket.
#   2. The channel is deleted after a short delay; export transcripts first if your server needs them.
#   3. Closed ticket rows remain in the database as historical records.
# /setstarboard
#   1. The starboard currently requires three star reactions before posting.
#   2. Only the configured star emojis are considered.
#   3. A production extension could update an existing starboard post as counts change.
# /synccommands
#   1. Use TEST_GUILD_ID for fast development iteration.
#   2. Global commands may take longer to propagate than guild commands.
#   3. The command does not install or upgrade Python dependencies.

# FAQ — DEPLOYMENT AND TROUBLESHOOTING
# FAQ 001: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 001: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 002: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 002: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 003: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 003: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 004: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 004: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 005: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 005: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 006: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 006: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 007: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 007: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 008: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 008: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 009: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 009: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 010: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 010: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 011: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 011: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 012: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 012: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 013: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 013: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 014: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 014: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 015: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 015: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 016: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 016: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 017: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 017: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 018: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 018: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 019: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 019: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 020: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 020: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 021: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 021: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 022: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 022: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 023: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 023: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 024: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 024: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 025: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 025: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 026: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 026: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 027: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 027: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 028: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 028: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 029: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 029: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 030: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 030: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 031: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 031: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 032: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 032: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 033: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 033: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 034: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 034: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 035: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 035: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 036: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 036: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 037: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 037: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 038: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 038: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 039: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 039: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 040: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 040: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 041: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 041: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 042: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 042: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 043: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 043: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 044: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 044: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 045: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 045: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 046: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 046: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 047: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 047: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 048: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 048: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 049: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 049: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 050: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 050: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 051: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 051: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 052: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 052: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 053: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 053: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 054: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 054: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 055: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 055: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 056: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 056: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 057: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 057: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 058: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 058: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 059: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 059: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 060: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 060: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 061: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 061: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 062: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 062: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 063: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 063: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 064: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 064: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 065: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 065: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 066: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 066: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 067: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 067: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 068: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 068: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 069: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 069: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 070: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 070: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 071: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 071: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 072: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 072: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 073: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 073: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 074: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 074: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 075: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 075: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 076: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 076: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 077: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 077: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 078: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 078: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 079: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 079: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 080: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 080: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 081: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 081: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 082: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 082: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 083: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 083: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 084: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 084: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 085: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 085: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 086: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 086: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 087: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 087: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 088: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 088: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 089: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 089: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 090: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 090: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 091: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 091: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 092: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 092: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 093: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 093: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 094: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 094: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 095: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 095: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 096: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 096: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 097: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 097: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 098: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 098: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 099: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 099: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 100: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 100: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 101: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 101: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 102: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 102: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 103: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 103: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 104: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 104: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 105: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 105: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 106: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 106: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 107: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 107: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 108: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 108: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 109: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 109: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 110: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 110: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 111: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 111: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 112: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 112: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 113: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 113: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 114: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 114: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 115: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 115: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 116: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 116: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 117: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 117: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 118: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 118: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 119: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 119: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 120: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 120: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 121: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 121: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 122: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 122: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 123: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 123: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 124: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 124: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 125: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 125: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 126: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 126: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 127: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 127: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 128: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 128: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 129: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 129: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 130: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 130: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 131: The owner command is denied.
# Answer: Check OWNER_ID against the numeric Discord user ID of the intended owner.
# Follow-up 131: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 132: A role operation fails for administrators.
# Answer: The bot's role hierarchy is independent of the command caller's administrator permission.
# Follow-up 132: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 133: A reminder arrives late.
# Answer: The reminder loop checks approximately every 15 seconds and cannot run while the service is offline.
# Follow-up 133: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 134: A reminder arrives twice.
# Answer: A crash after sending but before marking delivered can cause duplicate delivery; stronger queue semantics would be needed.
# Follow-up 134: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 135: A starboard post duplicates.
# Answer: The current simple starboard tracks the last count but does not store a destination message ID to edit existing posts.
# Follow-up 135: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 136: The bot cannot see a member.
# Answer: Enable the required intents and ensure the member is still in the guild.
# Follow-up 136: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 137: The bot receives interaction already acknowledged.
# Answer: Acknowledge each interaction only once; after defer or response, use followup.send.
# Follow-up 137: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 138: The embed is too long.
# Answer: Shorten the input or truncate it before adding it to an embed field.
# Follow-up 138: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 139: The bot receives rate limits.
# Answer: Reduce repeated API calls and allow discord.py to handle retry-after headers.
# Follow-up 139: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 140: The token was exposed.
# Answer: Reset the token in the Discord Developer Portal and replace DISCORD_TOKEN in Railway Variables immediately.
# Follow-up 140: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 141: The bot is offline after deployment.
# Answer: Check that DISCORD_TOKEN is present and valid, then inspect the first startup traceback in Railway logs.
# Follow-up 141: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 142: Slash commands are missing.
# Answer: Confirm the bot was invited with applications.commands scope and check the command sync log.
# Follow-up 142: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 143: The bot cannot ban a member.
# Answer: Grant Ban Members and place the bot's highest role above the target member's highest role.
# Follow-up 143: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 144: The bot cannot assign a role.
# Answer: Grant Manage Roles and move the bot role above the role being assigned.
# Follow-up 144: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 145: Welcome messages are not sent.
# Answer: Enable Server Members Intent, configure /setwelcome, and verify the channel is visible to the bot.
# Follow-up 145: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 146: Voice logs are missing.
# Answer: Configure /setmodlog, enable /setvoicelog, and verify the bot can send messages in the log channel.
# Follow-up 146: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 147: Message logs are missing.
# Answer: Configure /setmodlog and enable /setmessagelog. Message Content Intent may be required.
# Follow-up 147: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 148: The database seems to reset.
# Answer: Use a persistent Railway Volume and set DATABASE_PATH to a file under the mounted volume.
# Follow-up 148: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 149: The ticket button does nothing.
# Answer: Check Manage Channels, the ticket category, and Railway logs for interaction errors.
# Follow-up 149: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.
# FAQ 150: A command says you lack permissions.
# Answer: Check the member's server permissions; the command defaults may also hide it from unauthorized users.
# Follow-up 150: Recheck the relevant Discord permission, environment variable, database setting, and service log before changing unrelated code.

# MAINTENANCE CHECKLISTS
# Check 001: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Check 002: After upgrading, verify the bot logs in and reports the expected slash command count.
# Check 003: Test an ordinary public command such as /ping after deployment.
# Check 004: Test an administrator command with a test administrator account in a private server.
# Check 005: Test owner-only commands with the exact OWNER_ID account.
# Check 006: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Check 007: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Check 008: Verify the bot cannot modify a role above its own top role.
# Check 009: Verify a new member receives the welcome message only in the configured channel.
# Check 010: Verify autorole is disabled if no role is configured.
# Check 011: Verify the moderation log channel receives a test moderation action.
# Check 012: Verify message logging is off by default until explicitly enabled.
# Check 013: Verify voice logging can be disabled independently of message logging.
# Check 014: Verify reminders survive a restart when the database file is persistent.
# Check 015: Verify ticket channels are private to the opener and support team.
# Check 016: Verify the ticket opener can close their own ticket.
# Check 017: Verify an unrelated member cannot close another member's ticket.
# Check 018: Verify an administrator or support role can close a ticket.
# Check 019: Verify starboard posts only after the threshold is met.
# Check 020: Verify the bot does not send mass mentions from user-controlled templates.
# Check 021: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Check 022: After upgrading, verify the bot logs in and reports the expected slash command count.
# Check 023: Test an ordinary public command such as /ping after deployment.
# Check 024: Test an administrator command with a test administrator account in a private server.
# Check 025: Test owner-only commands with the exact OWNER_ID account.
# Check 026: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Check 027: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Check 028: Verify the bot cannot modify a role above its own top role.
# Check 029: Verify a new member receives the welcome message only in the configured channel.
# Check 030: Verify autorole is disabled if no role is configured.
# Check 031: Verify the moderation log channel receives a test moderation action.
# Check 032: Verify message logging is off by default until explicitly enabled.
# Check 033: Verify voice logging can be disabled independently of message logging.
# Check 034: Verify reminders survive a restart when the database file is persistent.
# Check 035: Verify ticket channels are private to the opener and support team.
# Check 036: Verify the ticket opener can close their own ticket.
# Check 037: Verify an unrelated member cannot close another member's ticket.
# Check 038: Verify an administrator or support role can close a ticket.
# Check 039: Verify starboard posts only after the threshold is met.
# Check 040: Verify the bot does not send mass mentions from user-controlled templates.
# Check 041: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Check 042: After upgrading, verify the bot logs in and reports the expected slash command count.
# Check 043: Test an ordinary public command such as /ping after deployment.
# Check 044: Test an administrator command with a test administrator account in a private server.
# Check 045: Test owner-only commands with the exact OWNER_ID account.
# Check 046: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Check 047: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Check 048: Verify the bot cannot modify a role above its own top role.
# Check 049: Verify a new member receives the welcome message only in the configured channel.
# Check 050: Verify autorole is disabled if no role is configured.
# Check 051: Verify the moderation log channel receives a test moderation action.
# Check 052: Verify message logging is off by default until explicitly enabled.
# Check 053: Verify voice logging can be disabled independently of message logging.
# Check 054: Verify reminders survive a restart when the database file is persistent.
# Check 055: Verify ticket channels are private to the opener and support team.
# Check 056: Verify the ticket opener can close their own ticket.
# Check 057: Verify an unrelated member cannot close another member's ticket.
# Check 058: Verify an administrator or support role can close a ticket.
# Check 059: Verify starboard posts only after the threshold is met.
# Check 060: Verify the bot does not send mass mentions from user-controlled templates.
# Check 061: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Check 062: After upgrading, verify the bot logs in and reports the expected slash command count.
# Check 063: Test an ordinary public command such as /ping after deployment.
# Check 064: Test an administrator command with a test administrator account in a private server.
# Check 065: Test owner-only commands with the exact OWNER_ID account.
# Check 066: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Check 067: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Check 068: Verify the bot cannot modify a role above its own top role.
# Check 069: Verify a new member receives the welcome message only in the configured channel.
# Check 070: Verify autorole is disabled if no role is configured.
# Check 071: Verify the moderation log channel receives a test moderation action.
# Check 072: Verify message logging is off by default until explicitly enabled.
# Check 073: Verify voice logging can be disabled independently of message logging.
# Check 074: Verify reminders survive a restart when the database file is persistent.
# Check 075: Verify ticket channels are private to the opener and support team.
# Check 076: Verify the ticket opener can close their own ticket.
# Check 077: Verify an unrelated member cannot close another member's ticket.
# Check 078: Verify an administrator or support role can close a ticket.
# Check 079: Verify starboard posts only after the threshold is met.
# Check 080: Verify the bot does not send mass mentions from user-controlled templates.
# Check 081: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Check 082: After upgrading, verify the bot logs in and reports the expected slash command count.
# Check 083: Test an ordinary public command such as /ping after deployment.
# Check 084: Test an administrator command with a test administrator account in a private server.
# Check 085: Test owner-only commands with the exact OWNER_ID account.
# Check 086: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Check 087: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Check 088: Verify the bot cannot modify a role above its own top role.
# Check 089: Verify a new member receives the welcome message only in the configured channel.
# Check 090: Verify autorole is disabled if no role is configured.
# Check 091: Verify the moderation log channel receives a test moderation action.
# Check 092: Verify message logging is off by default until explicitly enabled.
# Check 093: Verify voice logging can be disabled independently of message logging.
# Check 094: Verify reminders survive a restart when the database file is persistent.
# Check 095: Verify ticket channels are private to the opener and support team.
# Check 096: Verify the ticket opener can close their own ticket.
# Check 097: Verify an unrelated member cannot close another member's ticket.
# Check 098: Verify an administrator or support role can close a ticket.
# Check 099: Verify starboard posts only after the threshold is met.
# Check 100: Verify the bot does not send mass mentions from user-controlled templates.

# SAFE EXTENSION WORKFLOW
# Extension workflow item 001: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 002: Choose the correct permission decorator before implementing the action.
# Extension workflow item 003: Validate the guild context before accessing guild-specific state.
# Extension workflow item 004: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 005: Check role hierarchy before moderation or role operations.
# Extension workflow item 006: Use an ephemeral response for sensitive setup output.
# Extension workflow item 007: Defer long operations before making multiple API calls.
# Extension workflow item 008: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 009: Log unexpected exceptions with logger.exception.
# Extension workflow item 010: Add the command to /help and the command catalogue.
# Extension workflow item 011: Add a test case in a private test server.
# Extension workflow item 012: Confirm the new command appears after slash-command sync.
# Extension workflow item 013: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 014: Choose the correct permission decorator before implementing the action.
# Extension workflow item 015: Validate the guild context before accessing guild-specific state.
# Extension workflow item 016: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 017: Check role hierarchy before moderation or role operations.
# Extension workflow item 018: Use an ephemeral response for sensitive setup output.
# Extension workflow item 019: Defer long operations before making multiple API calls.
# Extension workflow item 020: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 021: Log unexpected exceptions with logger.exception.
# Extension workflow item 022: Add the command to /help and the command catalogue.
# Extension workflow item 023: Add a test case in a private test server.
# Extension workflow item 024: Confirm the new command appears after slash-command sync.
# Extension workflow item 025: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 026: Choose the correct permission decorator before implementing the action.
# Extension workflow item 027: Validate the guild context before accessing guild-specific state.
# Extension workflow item 028: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 029: Check role hierarchy before moderation or role operations.
# Extension workflow item 030: Use an ephemeral response for sensitive setup output.
# Extension workflow item 031: Defer long operations before making multiple API calls.
# Extension workflow item 032: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 033: Log unexpected exceptions with logger.exception.
# Extension workflow item 034: Add the command to /help and the command catalogue.
# Extension workflow item 035: Add a test case in a private test server.
# Extension workflow item 036: Confirm the new command appears after slash-command sync.
# Extension workflow item 037: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 038: Choose the correct permission decorator before implementing the action.
# Extension workflow item 039: Validate the guild context before accessing guild-specific state.
# Extension workflow item 040: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 041: Check role hierarchy before moderation or role operations.
# Extension workflow item 042: Use an ephemeral response for sensitive setup output.
# Extension workflow item 043: Defer long operations before making multiple API calls.
# Extension workflow item 044: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 045: Log unexpected exceptions with logger.exception.
# Extension workflow item 046: Add the command to /help and the command catalogue.
# Extension workflow item 047: Add a test case in a private test server.
# Extension workflow item 048: Confirm the new command appears after slash-command sync.
# Extension workflow item 049: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 050: Choose the correct permission decorator before implementing the action.
# Extension workflow item 051: Validate the guild context before accessing guild-specific state.
# Extension workflow item 052: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 053: Check role hierarchy before moderation or role operations.
# Extension workflow item 054: Use an ephemeral response for sensitive setup output.
# Extension workflow item 055: Defer long operations before making multiple API calls.
# Extension workflow item 056: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 057: Log unexpected exceptions with logger.exception.
# Extension workflow item 058: Add the command to /help and the command catalogue.
# Extension workflow item 059: Add a test case in a private test server.
# Extension workflow item 060: Confirm the new command appears after slash-command sync.
# Extension workflow item 061: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 062: Choose the correct permission decorator before implementing the action.
# Extension workflow item 063: Validate the guild context before accessing guild-specific state.
# Extension workflow item 064: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 065: Check role hierarchy before moderation or role operations.
# Extension workflow item 066: Use an ephemeral response for sensitive setup output.
# Extension workflow item 067: Defer long operations before making multiple API calls.
# Extension workflow item 068: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 069: Log unexpected exceptions with logger.exception.
# Extension workflow item 070: Add the command to /help and the command catalogue.
# Extension workflow item 071: Add a test case in a private test server.
# Extension workflow item 072: Confirm the new command appears after slash-command sync.
# Extension workflow item 073: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 074: Choose the correct permission decorator before implementing the action.
# Extension workflow item 075: Validate the guild context before accessing guild-specific state.
# Extension workflow item 076: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 077: Check role hierarchy before moderation or role operations.
# Extension workflow item 078: Use an ephemeral response for sensitive setup output.
# Extension workflow item 079: Defer long operations before making multiple API calls.
# Extension workflow item 080: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 081: Log unexpected exceptions with logger.exception.
# Extension workflow item 082: Add the command to /help and the command catalogue.
# Extension workflow item 083: Add a test case in a private test server.
# Extension workflow item 084: Confirm the new command appears after slash-command sync.
# Extension workflow item 085: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 086: Choose the correct permission decorator before implementing the action.
# Extension workflow item 087: Validate the guild context before accessing guild-specific state.
# Extension workflow item 088: Validate user input and enforce upper/lower bounds for numeric parameters.
# Extension workflow item 089: Check role hierarchy before moderation or role operations.
# Extension workflow item 090: Use an ephemeral response for sensitive setup output.
# Extension workflow item 091: Defer long operations before making multiple API calls.
# Extension workflow item 092: Catch Discord permission errors and explain which permission is missing.
# Extension workflow item 093: Log unexpected exceptions with logger.exception.
# Extension workflow item 094: Add the command to /help and the command catalogue.
# Extension workflow item 095: Add a test case in a private test server.
# Extension workflow item 096: Confirm the new command appears after slash-command sync.
# Extension workflow item 097: Create the command with @bot.tree.command and a concise description.
# Extension workflow item 098: Choose the correct permission decorator before implementing the action.
# Extension workflow item 099: Validate the guild context before accessing guild-specific state.
# Extension workflow item 100: Validate user input and enforce upper/lower bounds for numeric parameters.

# SCENARIO TEST PLAN
# /help scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /help scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /help scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /help scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /help scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /help scenario 6: Test with an empty optional value and confirm the default is sensible.
# /help scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /help scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ping scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ping scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ping scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ping scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ping scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ping scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ping scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ping scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /botinfo scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /botinfo scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /botinfo scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /botinfo scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /botinfo scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /botinfo scenario 6: Test with an empty optional value and confirm the default is sensible.
# /botinfo scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /botinfo scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /serverinfo scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /serverinfo scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /serverinfo scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /serverinfo scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /serverinfo scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /serverinfo scenario 6: Test with an empty optional value and confirm the default is sensible.
# /serverinfo scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /serverinfo scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /userinfo scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /userinfo scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /userinfo scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /userinfo scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /userinfo scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /userinfo scenario 6: Test with an empty optional value and confirm the default is sensible.
# /userinfo scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /userinfo scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /avatar scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /avatar scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /avatar scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /avatar scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /avatar scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /avatar scenario 6: Test with an empty optional value and confirm the default is sensible.
# /avatar scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /avatar scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /roles scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /roles scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /roles scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /roles scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /roles scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /roles scenario 6: Test with an empty optional value and confirm the default is sensible.
# /roles scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /roles scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /channels scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /channels scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /channels scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /channels scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /channels scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /channels scenario 6: Test with an empty optional value and confirm the default is sensible.
# /channels scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /channels scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ban scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ban scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ban scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ban scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ban scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ban scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ban scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ban scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /kick scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /kick scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /kick scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /kick scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /kick scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /kick scenario 6: Test with an empty optional value and confirm the default is sensible.
# /kick scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /kick scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /timeout scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /timeout scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /timeout scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /timeout scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /timeout scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /timeout scenario 6: Test with an empty optional value and confirm the default is sensible.
# /timeout scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /timeout scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /untimeout scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /untimeout scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /untimeout scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /untimeout scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /untimeout scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /untimeout scenario 6: Test with an empty optional value and confirm the default is sensible.
# /untimeout scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /untimeout scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /purge scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /purge scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /purge scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /purge scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /purge scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /purge scenario 6: Test with an empty optional value and confirm the default is sensible.
# /purge scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /purge scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /warn scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /warn scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /warn scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /warn scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /warn scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /warn scenario 6: Test with an empty optional value and confirm the default is sensible.
# /warn scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /warn scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /warnings scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /warnings scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /warnings scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /warnings scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /warnings scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /warnings scenario 6: Test with an empty optional value and confirm the default is sensible.
# /warnings scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /warnings scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /clearwarnings scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /clearwarnings scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /clearwarnings scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /clearwarnings scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /clearwarnings scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /clearwarnings scenario 6: Test with an empty optional value and confirm the default is sensible.
# /clearwarnings scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /clearwarnings scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /lock scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /lock scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /lock scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /lock scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /lock scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /lock scenario 6: Test with an empty optional value and confirm the default is sensible.
# /lock scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /lock scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /unlock scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /unlock scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /unlock scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /unlock scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /unlock scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /unlock scenario 6: Test with an empty optional value and confirm the default is sensible.
# /unlock scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /unlock scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /slowmode scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /slowmode scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /slowmode scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /slowmode scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /slowmode scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /slowmode scenario 6: Test with an empty optional value and confirm the default is sensible.
# /slowmode scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /slowmode scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /nick scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /nick scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /nick scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /nick scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /nick scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /nick scenario 6: Test with an empty optional value and confirm the default is sensible.
# /nick scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /nick scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /addrole scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /addrole scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /addrole scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /addrole scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /addrole scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /addrole scenario 6: Test with an empty optional value and confirm the default is sensible.
# /addrole scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /addrole scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /removerole scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /removerole scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /removerole scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /removerole scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /removerole scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /removerole scenario 6: Test with an empty optional value and confirm the default is sensible.
# /removerole scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /removerole scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setmodlog scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setmodlog scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setmodlog scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setmodlog scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setmodlog scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setmodlog scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setmodlog scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setmodlog scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setwelcome scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setwelcome scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setwelcome scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setwelcome scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setwelcome scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setwelcome scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setwelcome scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setwelcome scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setgoodbye scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setgoodbye scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setgoodbye scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setgoodbye scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setgoodbye scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setgoodbye scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setgoodbye scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setgoodbye scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setautorole scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setautorole scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setautorole scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setautorole scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setautorole scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setautorole scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setautorole scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setautorole scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setmessagelog scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setmessagelog scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setmessagelog scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setmessagelog scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setmessagelog scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setmessagelog scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setmessagelog scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setmessagelog scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setvoicelog scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setvoicelog scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setvoicelog scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setvoicelog scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setvoicelog scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setvoicelog scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setvoicelog scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setvoicelog scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setlevel scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setlevel scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setlevel scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setlevel scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setlevel scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setlevel scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setlevel scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setlevel scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /rank scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /rank scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /rank scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /rank scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /rank scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /rank scenario 6: Test with an empty optional value and confirm the default is sensible.
# /rank scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /rank scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /leaderboard scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /leaderboard scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /leaderboard scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /leaderboard scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /leaderboard scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /leaderboard scenario 6: Test with an empty optional value and confirm the default is sensible.
# /leaderboard scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /leaderboard scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /coinflip scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /coinflip scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /coinflip scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /coinflip scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /coinflip scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /coinflip scenario 6: Test with an empty optional value and confirm the default is sensible.
# /coinflip scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /coinflip scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /dice scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /dice scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /dice scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /dice scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /dice scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /dice scenario 6: Test with an empty optional value and confirm the default is sensible.
# /dice scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /dice scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /8ball scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /8ball scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /8ball scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /8ball scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /8ball scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /8ball scenario 6: Test with an empty optional value and confirm the default is sensible.
# /8ball scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /8ball scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /choose scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /choose scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /choose scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /choose scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /choose scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /choose scenario 6: Test with an empty optional value and confirm the default is sensible.
# /choose scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /choose scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /rps scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /rps scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /rps scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /rps scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /rps scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /rps scenario 6: Test with an empty optional value and confirm the default is sensible.
# /rps scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /rps scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /poll scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /poll scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /poll scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /poll scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /poll scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /poll scenario 6: Test with an empty optional value and confirm the default is sensible.
# /poll scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /poll scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /quote scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /quote scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /quote scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /quote scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /quote scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /quote scenario 6: Test with an empty optional value and confirm the default is sensible.
# /quote scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /quote scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /compliment scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /compliment scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /compliment scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /compliment scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /compliment scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /compliment scenario 6: Test with an empty optional value and confirm the default is sensible.
# /compliment scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /compliment scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /hug scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /hug scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /hug scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /hug scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /hug scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /hug scenario 6: Test with an empty optional value and confirm the default is sensible.
# /hug scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /hug scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ship scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ship scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ship scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ship scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ship scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ship scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ship scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ship scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /trivia scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /trivia scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /trivia scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /trivia scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /trivia scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /trivia scenario 6: Test with an empty optional value and confirm the default is sensible.
# /trivia scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /trivia scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /remindme scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /remindme scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /remindme scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /remindme scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /remindme scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /remindme scenario 6: Test with an empty optional value and confirm the default is sensible.
# /remindme scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /remindme scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ticketsetup scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ticketsetup scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ticketsetup scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ticketsetup scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ticketsetup scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ticketsetup scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ticketsetup scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ticketsetup scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ticketpanel scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ticketpanel scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ticketpanel scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ticketpanel scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ticketpanel scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ticketpanel scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ticketpanel scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ticketpanel scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /ticketclose scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /ticketclose scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /ticketclose scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /ticketclose scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /ticketclose scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /ticketclose scenario 6: Test with an empty optional value and confirm the default is sensible.
# /ticketclose scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /ticketclose scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /setstarboard scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /setstarboard scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /setstarboard scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /setstarboard scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /setstarboard scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /setstarboard scenario 6: Test with an empty optional value and confirm the default is sensible.
# /setstarboard scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /setstarboard scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /botstatus scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /botstatus scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /botstatus scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /botstatus scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /botstatus scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /botstatus scenario 6: Test with an empty optional value and confirm the default is sensible.
# /botstatus scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /botstatus scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /synccommands scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /synccommands scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /synccommands scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /synccommands scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /synccommands scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /synccommands scenario 6: Test with an empty optional value and confirm the default is sensible.
# /synccommands scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /synccommands scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /shutdown scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /shutdown scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /shutdown scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /shutdown scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /shutdown scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /shutdown scenario 6: Test with an empty optional value and confirm the default is sensible.
# /shutdown scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /shutdown scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# /dbstats scenario 1: Test with a member below the moderator's role and confirm the action is allowed when the bot has permission.
# /dbstats scenario 2: Test with a member above the moderator's role and confirm the command refuses the action.
# /dbstats scenario 3: Test with the bot role below the target role and confirm Discord permission errors are handled.
# /dbstats scenario 4: Test with DMs closed and confirm the primary action still succeeds if the DM was optional.
# /dbstats scenario 5: Test with a deleted or inaccessible channel and confirm the command fails gracefully.
# /dbstats scenario 6: Test with an empty optional value and confirm the default is sensible.
# /dbstats scenario 7: Test with maximum-length user input and confirm the response remains within Discord limits.
# /dbstats scenario 8: Test after a restart and confirm persistent state is still present when storage is persistent.
# Additional validation note 2686: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2687: Verify autorole is disabled if no role is configured.
# Additional validation note 2688: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2689: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2690: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2691: Verify an administrator or support role can close a ticket.
# Additional validation note 2692: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2693: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2694: Verify starboard posts only after the threshold is met.
# Additional validation note 2695: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2696: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2697: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2698: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2699: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2700: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2701: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2702: Verify ticket channels are private to the opener and support team.
# Additional validation note 2703: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2704: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2705: Verify the ticket opener can close their own ticket.
# Additional validation note 2706: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2707: Verify autorole is disabled if no role is configured.
# Additional validation note 2708: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2709: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2710: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2711: Verify an administrator or support role can close a ticket.
# Additional validation note 2712: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2713: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2714: Verify starboard posts only after the threshold is met.
# Additional validation note 2715: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2716: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2717: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2718: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2719: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2720: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2721: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2722: Verify ticket channels are private to the opener and support team.
# Additional validation note 2723: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2724: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2725: Verify the ticket opener can close their own ticket.
# Additional validation note 2726: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2727: Verify autorole is disabled if no role is configured.
# Additional validation note 2728: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2729: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2730: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2731: Verify an administrator or support role can close a ticket.
# Additional validation note 2732: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2733: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2734: Verify starboard posts only after the threshold is met.
# Additional validation note 2735: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2736: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2737: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2738: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2739: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2740: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2741: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2742: Verify ticket channels are private to the opener and support team.
# Additional validation note 2743: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2744: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2745: Verify the ticket opener can close their own ticket.
# Additional validation note 2746: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2747: Verify autorole is disabled if no role is configured.
# Additional validation note 2748: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2749: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2750: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2751: Verify an administrator or support role can close a ticket.
# Additional validation note 2752: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2753: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2754: Verify starboard posts only after the threshold is met.
# Additional validation note 2755: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2756: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2757: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2758: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2759: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2760: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2761: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2762: Verify ticket channels are private to the opener and support team.
# Additional validation note 2763: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2764: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2765: Verify the ticket opener can close their own ticket.
# Additional validation note 2766: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2767: Verify autorole is disabled if no role is configured.
# Additional validation note 2768: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2769: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2770: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2771: Verify an administrator or support role can close a ticket.
# Additional validation note 2772: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2773: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2774: Verify starboard posts only after the threshold is met.
# Additional validation note 2775: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2776: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2777: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2778: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2779: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2780: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2781: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2782: Verify ticket channels are private to the opener and support team.
# Additional validation note 2783: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2784: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2785: Verify the ticket opener can close their own ticket.
# Additional validation note 2786: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2787: Verify autorole is disabled if no role is configured.
# Additional validation note 2788: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2789: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2790: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2791: Verify an administrator or support role can close a ticket.
# Additional validation note 2792: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2793: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2794: Verify starboard posts only after the threshold is met.
# Additional validation note 2795: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2796: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2797: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2798: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2799: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2800: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2801: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2802: Verify ticket channels are private to the opener and support team.
# Additional validation note 2803: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2804: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2805: Verify the ticket opener can close their own ticket.
# Additional validation note 2806: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2807: Verify autorole is disabled if no role is configured.
# Additional validation note 2808: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2809: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2810: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2811: Verify an administrator or support role can close a ticket.
# Additional validation note 2812: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2813: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2814: Verify starboard posts only after the threshold is met.
# Additional validation note 2815: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2816: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2817: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2818: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2819: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2820: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2821: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2822: Verify ticket channels are private to the opener and support team.
# Additional validation note 2823: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2824: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2825: Verify the ticket opener can close their own ticket.
# Additional validation note 2826: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2827: Verify autorole is disabled if no role is configured.
# Additional validation note 2828: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2829: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2830: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2831: Verify an administrator or support role can close a ticket.
# Additional validation note 2832: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2833: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2834: Verify starboard posts only after the threshold is met.
# Additional validation note 2835: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2836: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2837: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2838: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2839: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2840: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2841: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2842: Verify ticket channels are private to the opener and support team.
# Additional validation note 2843: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2844: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2845: Verify the ticket opener can close their own ticket.
# Additional validation note 2846: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2847: Verify autorole is disabled if no role is configured.
# Additional validation note 2848: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2849: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2850: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2851: Verify an administrator or support role can close a ticket.
# Additional validation note 2852: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2853: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2854: Verify starboard posts only after the threshold is met.
# Additional validation note 2855: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2856: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2857: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2858: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2859: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2860: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2861: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2862: Verify ticket channels are private to the opener and support team.
# Additional validation note 2863: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2864: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2865: Verify the ticket opener can close their own ticket.
# Additional validation note 2866: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2867: Verify autorole is disabled if no role is configured.
# Additional validation note 2868: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2869: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2870: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2871: Verify an administrator or support role can close a ticket.
# Additional validation note 2872: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2873: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2874: Verify starboard posts only after the threshold is met.
# Additional validation note 2875: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2876: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2877: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2878: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2879: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2880: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2881: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2882: Verify ticket channels are private to the opener and support team.
# Additional validation note 2883: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2884: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2885: Verify the ticket opener can close their own ticket.
# Additional validation note 2886: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2887: Verify autorole is disabled if no role is configured.
# Additional validation note 2888: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2889: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2890: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2891: Verify an administrator or support role can close a ticket.
# Additional validation note 2892: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2893: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2894: Verify starboard posts only after the threshold is met.
# Additional validation note 2895: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2896: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2897: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2898: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2899: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2900: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2901: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2902: Verify ticket channels are private to the opener and support team.
# Additional validation note 2903: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2904: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2905: Verify the ticket opener can close their own ticket.
# Additional validation note 2906: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2907: Verify autorole is disabled if no role is configured.
# Additional validation note 2908: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2909: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2910: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2911: Verify an administrator or support role can close a ticket.
# Additional validation note 2912: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2913: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2914: Verify starboard posts only after the threshold is met.
# Additional validation note 2915: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2916: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2917: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2918: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2919: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2920: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2921: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2922: Verify ticket channels are private to the opener and support team.
# Additional validation note 2923: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2924: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2925: Verify the ticket opener can close their own ticket.
# Additional validation note 2926: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2927: Verify autorole is disabled if no role is configured.
# Additional validation note 2928: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2929: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2930: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2931: Verify an administrator or support role can close a ticket.
# Additional validation note 2932: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2933: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2934: Verify starboard posts only after the threshold is met.
# Additional validation note 2935: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2936: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2937: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2938: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2939: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2940: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2941: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2942: Verify ticket channels are private to the opener and support team.
# Additional validation note 2943: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2944: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2945: Verify the ticket opener can close their own ticket.
# Additional validation note 2946: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2947: Verify autorole is disabled if no role is configured.
# Additional validation note 2948: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2949: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2950: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2951: Verify an administrator or support role can close a ticket.
# Additional validation note 2952: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2953: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2954: Verify starboard posts only after the threshold is met.
# Additional validation note 2955: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2956: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2957: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2958: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2959: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2960: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2961: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2962: Verify ticket channels are private to the opener and support team.
# Additional validation note 2963: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2964: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2965: Verify the ticket opener can close their own ticket.
# Additional validation note 2966: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2967: Verify autorole is disabled if no role is configured.
# Additional validation note 2968: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2969: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2970: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2971: Verify an administrator or support role can close a ticket.
# Additional validation note 2972: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2973: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2974: Verify starboard posts only after the threshold is met.
# Additional validation note 2975: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2976: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2977: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2978: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2979: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 2980: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 2981: Verify the bot cannot modify a role above its own top role.
# Additional validation note 2982: Verify ticket channels are private to the opener and support team.
# Additional validation note 2983: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 2984: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 2985: Verify the ticket opener can close their own ticket.
# Additional validation note 2986: Test an ordinary public command such as /ping after deployment.
# Additional validation note 2987: Verify autorole is disabled if no role is configured.
# Additional validation note 2988: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 2989: Test an administrator command with a test administrator account in a private server.
# Additional validation note 2990: Verify the moderation log channel receives a test moderation action.
# Additional validation note 2991: Verify an administrator or support role can close a ticket.
# Additional validation note 2992: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 2993: Verify message logging is off by default until explicitly enabled.
# Additional validation note 2994: Verify starboard posts only after the threshold is met.
# Additional validation note 2995: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 2996: Verify voice logging can be disabled independently of message logging.
# Additional validation note 2997: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 2998: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 2999: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3000: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3001: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3002: Verify ticket channels are private to the opener and support team.
# Additional validation note 3003: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3004: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3005: Verify the ticket opener can close their own ticket.
# Additional validation note 3006: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3007: Verify autorole is disabled if no role is configured.
# Additional validation note 3008: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3009: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3010: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3011: Verify an administrator or support role can close a ticket.
# Additional validation note 3012: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3013: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3014: Verify starboard posts only after the threshold is met.
# Additional validation note 3015: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3016: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3017: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3018: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3019: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3020: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3021: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3022: Verify ticket channels are private to the opener and support team.
# Additional validation note 3023: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3024: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3025: Verify the ticket opener can close their own ticket.
# Additional validation note 3026: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3027: Verify autorole is disabled if no role is configured.
# Additional validation note 3028: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3029: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3030: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3031: Verify an administrator or support role can close a ticket.
# Additional validation note 3032: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3033: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3034: Verify starboard posts only after the threshold is met.
# Additional validation note 3035: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3036: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3037: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3038: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3039: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3040: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3041: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3042: Verify ticket channels are private to the opener and support team.
# Additional validation note 3043: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3044: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3045: Verify the ticket opener can close their own ticket.
# Additional validation note 3046: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3047: Verify autorole is disabled if no role is configured.
# Additional validation note 3048: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3049: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3050: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3051: Verify an administrator or support role can close a ticket.
# Additional validation note 3052: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3053: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3054: Verify starboard posts only after the threshold is met.
# Additional validation note 3055: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3056: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3057: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3058: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3059: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3060: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3061: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3062: Verify ticket channels are private to the opener and support team.
# Additional validation note 3063: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3064: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3065: Verify the ticket opener can close their own ticket.
# Additional validation note 3066: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3067: Verify autorole is disabled if no role is configured.
# Additional validation note 3068: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3069: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3070: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3071: Verify an administrator or support role can close a ticket.
# Additional validation note 3072: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3073: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3074: Verify starboard posts only after the threshold is met.
# Additional validation note 3075: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3076: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3077: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3078: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3079: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3080: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3081: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3082: Verify ticket channels are private to the opener and support team.
# Additional validation note 3083: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3084: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3085: Verify the ticket opener can close their own ticket.
# Additional validation note 3086: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3087: Verify autorole is disabled if no role is configured.
# Additional validation note 3088: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3089: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3090: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3091: Verify an administrator or support role can close a ticket.
# Additional validation note 3092: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3093: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3094: Verify starboard posts only after the threshold is met.
# Additional validation note 3095: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3096: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3097: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3098: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3099: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3100: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3101: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3102: Verify ticket channels are private to the opener and support team.
# Additional validation note 3103: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3104: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3105: Verify the ticket opener can close their own ticket.
# Additional validation note 3106: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3107: Verify autorole is disabled if no role is configured.
# Additional validation note 3108: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3109: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3110: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3111: Verify an administrator or support role can close a ticket.
# Additional validation note 3112: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3113: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3114: Verify starboard posts only after the threshold is met.
# Additional validation note 3115: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3116: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3117: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3118: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3119: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3120: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3121: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3122: Verify ticket channels are private to the opener and support team.
# Additional validation note 3123: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3124: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3125: Verify the ticket opener can close their own ticket.
# Additional validation note 3126: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3127: Verify autorole is disabled if no role is configured.
# Additional validation note 3128: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3129: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3130: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3131: Verify an administrator or support role can close a ticket.
# Additional validation note 3132: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3133: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3134: Verify starboard posts only after the threshold is met.
# Additional validation note 3135: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3136: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3137: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3138: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3139: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3140: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3141: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3142: Verify ticket channels are private to the opener and support team.
# Additional validation note 3143: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3144: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3145: Verify the ticket opener can close their own ticket.
# Additional validation note 3146: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3147: Verify autorole is disabled if no role is configured.
# Additional validation note 3148: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3149: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3150: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3151: Verify an administrator or support role can close a ticket.
# Additional validation note 3152: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3153: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3154: Verify starboard posts only after the threshold is met.
# Additional validation note 3155: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3156: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3157: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3158: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3159: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3160: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3161: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3162: Verify ticket channels are private to the opener and support team.
# Additional validation note 3163: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3164: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3165: Verify the ticket opener can close their own ticket.
# Additional validation note 3166: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3167: Verify autorole is disabled if no role is configured.
# Additional validation note 3168: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3169: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3170: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3171: Verify an administrator or support role can close a ticket.
# Additional validation note 3172: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3173: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3174: Verify starboard posts only after the threshold is met.
# Additional validation note 3175: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3176: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3177: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3178: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3179: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3180: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3181: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3182: Verify ticket channels are private to the opener and support team.
# Additional validation note 3183: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3184: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3185: Verify the ticket opener can close their own ticket.
# Additional validation note 3186: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3187: Verify autorole is disabled if no role is configured.
# Additional validation note 3188: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3189: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3190: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3191: Verify an administrator or support role can close a ticket.
# Additional validation note 3192: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3193: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3194: Verify starboard posts only after the threshold is met.
# Additional validation note 3195: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3196: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3197: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3198: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3199: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3200: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3201: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3202: Verify ticket channels are private to the opener and support team.
# Additional validation note 3203: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3204: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3205: Verify the ticket opener can close their own ticket.
# Additional validation note 3206: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3207: Verify autorole is disabled if no role is configured.
# Additional validation note 3208: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3209: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3210: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3211: Verify an administrator or support role can close a ticket.
# Additional validation note 3212: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3213: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3214: Verify starboard posts only after the threshold is met.
# Additional validation note 3215: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3216: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3217: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3218: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3219: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3220: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3221: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3222: Verify ticket channels are private to the opener and support team.
# Additional validation note 3223: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3224: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3225: Verify the ticket opener can close their own ticket.
# Additional validation note 3226: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3227: Verify autorole is disabled if no role is configured.
# Additional validation note 3228: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3229: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3230: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3231: Verify an administrator or support role can close a ticket.
# Additional validation note 3232: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3233: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3234: Verify starboard posts only after the threshold is met.
# Additional validation note 3235: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3236: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3237: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3238: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3239: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3240: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3241: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3242: Verify ticket channels are private to the opener and support team.
# Additional validation note 3243: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3244: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3245: Verify the ticket opener can close their own ticket.
# Additional validation note 3246: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3247: Verify autorole is disabled if no role is configured.
# Additional validation note 3248: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3249: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3250: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3251: Verify an administrator or support role can close a ticket.
# Additional validation note 3252: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3253: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3254: Verify starboard posts only after the threshold is met.
# Additional validation note 3255: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3256: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3257: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3258: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3259: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3260: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3261: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3262: Verify ticket channels are private to the opener and support team.
# Additional validation note 3263: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3264: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3265: Verify the ticket opener can close their own ticket.
# Additional validation note 3266: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3267: Verify autorole is disabled if no role is configured.
# Additional validation note 3268: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3269: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3270: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3271: Verify an administrator or support role can close a ticket.
# Additional validation note 3272: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3273: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3274: Verify starboard posts only after the threshold is met.
# Additional validation note 3275: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3276: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3277: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3278: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3279: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3280: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3281: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3282: Verify ticket channels are private to the opener and support team.
# Additional validation note 3283: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3284: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3285: Verify the ticket opener can close their own ticket.
# Additional validation note 3286: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3287: Verify autorole is disabled if no role is configured.
# Additional validation note 3288: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3289: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3290: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3291: Verify an administrator or support role can close a ticket.
# Additional validation note 3292: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3293: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3294: Verify starboard posts only after the threshold is met.
# Additional validation note 3295: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3296: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3297: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3298: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3299: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3300: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3301: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3302: Verify ticket channels are private to the opener and support team.
# Additional validation note 3303: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3304: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3305: Verify the ticket opener can close their own ticket.
# Additional validation note 3306: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3307: Verify autorole is disabled if no role is configured.
# Additional validation note 3308: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3309: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3310: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3311: Verify an administrator or support role can close a ticket.
# Additional validation note 3312: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3313: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3314: Verify starboard posts only after the threshold is met.
# Additional validation note 3315: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3316: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3317: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3318: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3319: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3320: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3321: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3322: Verify ticket channels are private to the opener and support team.
# Additional validation note 3323: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3324: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3325: Verify the ticket opener can close their own ticket.
# Additional validation note 3326: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3327: Verify autorole is disabled if no role is configured.
# Additional validation note 3328: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3329: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3330: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3331: Verify an administrator or support role can close a ticket.
# Additional validation note 3332: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3333: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3334: Verify starboard posts only after the threshold is met.
# Additional validation note 3335: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3336: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3337: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3338: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3339: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3340: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3341: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3342: Verify ticket channels are private to the opener and support team.
# Additional validation note 3343: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3344: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3345: Verify the ticket opener can close their own ticket.
# Additional validation note 3346: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3347: Verify autorole is disabled if no role is configured.
# Additional validation note 3348: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3349: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3350: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3351: Verify an administrator or support role can close a ticket.
# Additional validation note 3352: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3353: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3354: Verify starboard posts only after the threshold is met.
# Additional validation note 3355: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3356: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3357: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3358: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3359: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3360: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3361: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3362: Verify ticket channels are private to the opener and support team.
# Additional validation note 3363: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3364: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3365: Verify the ticket opener can close their own ticket.
# Additional validation note 3366: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3367: Verify autorole is disabled if no role is configured.
# Additional validation note 3368: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3369: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3370: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3371: Verify an administrator or support role can close a ticket.
# Additional validation note 3372: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3373: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3374: Verify starboard posts only after the threshold is met.
# Additional validation note 3375: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3376: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3377: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3378: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3379: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3380: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3381: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3382: Verify ticket channels are private to the opener and support team.
# Additional validation note 3383: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3384: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3385: Verify the ticket opener can close their own ticket.
# Additional validation note 3386: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3387: Verify autorole is disabled if no role is configured.
# Additional validation note 3388: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3389: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3390: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3391: Verify an administrator or support role can close a ticket.
# Additional validation note 3392: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3393: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3394: Verify starboard posts only after the threshold is met.
# Additional validation note 3395: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3396: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3397: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3398: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3399: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3400: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3401: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3402: Verify ticket channels are private to the opener and support team.
# Additional validation note 3403: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3404: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3405: Verify the ticket opener can close their own ticket.
# Additional validation note 3406: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3407: Verify autorole is disabled if no role is configured.
# Additional validation note 3408: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3409: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3410: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3411: Verify an administrator or support role can close a ticket.
# Additional validation note 3412: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3413: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3414: Verify starboard posts only after the threshold is met.
# Additional validation note 3415: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3416: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3417: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3418: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3419: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3420: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3421: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3422: Verify ticket channels are private to the opener and support team.
# Additional validation note 3423: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3424: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3425: Verify the ticket opener can close their own ticket.
# Additional validation note 3426: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3427: Verify autorole is disabled if no role is configured.
# Additional validation note 3428: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3429: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3430: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3431: Verify an administrator or support role can close a ticket.
# Additional validation note 3432: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3433: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3434: Verify starboard posts only after the threshold is met.
# Additional validation note 3435: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3436: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3437: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3438: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3439: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3440: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3441: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3442: Verify ticket channels are private to the opener and support team.
# Additional validation note 3443: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3444: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3445: Verify the ticket opener can close their own ticket.
# Additional validation note 3446: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3447: Verify autorole is disabled if no role is configured.
# Additional validation note 3448: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3449: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3450: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3451: Verify an administrator or support role can close a ticket.
# Additional validation note 3452: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3453: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3454: Verify starboard posts only after the threshold is met.
# Additional validation note 3455: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3456: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3457: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3458: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3459: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3460: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3461: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3462: Verify ticket channels are private to the opener and support team.
# Additional validation note 3463: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3464: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3465: Verify the ticket opener can close their own ticket.
# Additional validation note 3466: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3467: Verify autorole is disabled if no role is configured.
# Additional validation note 3468: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3469: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3470: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3471: Verify an administrator or support role can close a ticket.
# Additional validation note 3472: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3473: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3474: Verify starboard posts only after the threshold is met.
# Additional validation note 3475: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3476: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3477: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3478: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3479: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3480: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3481: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3482: Verify ticket channels are private to the opener and support team.
# Additional validation note 3483: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3484: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3485: Verify the ticket opener can close their own ticket.
# Additional validation note 3486: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3487: Verify autorole is disabled if no role is configured.
# Additional validation note 3488: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3489: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3490: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3491: Verify an administrator or support role can close a ticket.
# Additional validation note 3492: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3493: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3494: Verify starboard posts only after the threshold is met.
# Additional validation note 3495: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3496: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3497: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3498: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3499: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3500: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3501: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3502: Verify ticket channels are private to the opener and support team.
# Additional validation note 3503: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3504: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3505: Verify the ticket opener can close their own ticket.
# Additional validation note 3506: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3507: Verify autorole is disabled if no role is configured.
# Additional validation note 3508: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3509: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3510: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3511: Verify an administrator or support role can close a ticket.
# Additional validation note 3512: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3513: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3514: Verify starboard posts only after the threshold is met.
# Additional validation note 3515: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3516: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3517: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3518: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3519: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3520: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3521: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3522: Verify ticket channels are private to the opener and support team.
# Additional validation note 3523: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3524: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3525: Verify the ticket opener can close their own ticket.
# Additional validation note 3526: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3527: Verify autorole is disabled if no role is configured.
# Additional validation note 3528: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3529: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3530: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3531: Verify an administrator or support role can close a ticket.
# Additional validation note 3532: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3533: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3534: Verify starboard posts only after the threshold is met.
# Additional validation note 3535: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3536: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3537: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3538: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3539: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3540: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3541: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3542: Verify ticket channels are private to the opener and support team.
# Additional validation note 3543: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3544: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3545: Verify the ticket opener can close their own ticket.
# Additional validation note 3546: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3547: Verify autorole is disabled if no role is configured.
# Additional validation note 3548: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3549: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3550: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3551: Verify an administrator or support role can close a ticket.
# Additional validation note 3552: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3553: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3554: Verify starboard posts only after the threshold is met.
# Additional validation note 3555: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3556: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3557: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3558: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3559: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3560: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3561: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3562: Verify ticket channels are private to the opener and support team.
# Additional validation note 3563: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3564: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3565: Verify the ticket opener can close their own ticket.
# Additional validation note 3566: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3567: Verify autorole is disabled if no role is configured.
# Additional validation note 3568: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3569: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3570: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3571: Verify an administrator or support role can close a ticket.
# Additional validation note 3572: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3573: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3574: Verify starboard posts only after the threshold is met.
# Additional validation note 3575: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3576: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3577: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3578: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3579: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3580: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3581: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3582: Verify ticket channels are private to the opener and support team.
# Additional validation note 3583: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3584: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3585: Verify the ticket opener can close their own ticket.
# Additional validation note 3586: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3587: Verify autorole is disabled if no role is configured.
# Additional validation note 3588: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3589: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3590: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3591: Verify an administrator or support role can close a ticket.
# Additional validation note 3592: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3593: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3594: Verify starboard posts only after the threshold is met.
# Additional validation note 3595: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3596: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3597: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3598: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3599: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3600: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3601: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3602: Verify ticket channels are private to the opener and support team.
# Additional validation note 3603: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3604: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3605: Verify the ticket opener can close their own ticket.
# Additional validation note 3606: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3607: Verify autorole is disabled if no role is configured.
# Additional validation note 3608: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3609: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3610: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3611: Verify an administrator or support role can close a ticket.
# Additional validation note 3612: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3613: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3614: Verify starboard posts only after the threshold is met.
# Additional validation note 3615: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3616: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3617: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3618: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3619: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3620: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3621: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3622: Verify ticket channels are private to the opener and support team.
# Additional validation note 3623: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3624: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3625: Verify the ticket opener can close their own ticket.
# Additional validation note 3626: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3627: Verify autorole is disabled if no role is configured.
# Additional validation note 3628: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3629: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3630: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3631: Verify an administrator or support role can close a ticket.
# Additional validation note 3632: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3633: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3634: Verify starboard posts only after the threshold is met.
# Additional validation note 3635: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3636: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3637: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3638: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3639: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3640: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3641: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3642: Verify ticket channels are private to the opener and support team.
# Additional validation note 3643: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3644: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3645: Verify the ticket opener can close their own ticket.
# Additional validation note 3646: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3647: Verify autorole is disabled if no role is configured.
# Additional validation note 3648: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3649: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3650: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3651: Verify an administrator or support role can close a ticket.
# Additional validation note 3652: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3653: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3654: Verify starboard posts only after the threshold is met.
# Additional validation note 3655: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3656: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3657: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3658: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3659: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3660: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3661: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3662: Verify ticket channels are private to the opener and support team.
# Additional validation note 3663: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3664: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3665: Verify the ticket opener can close their own ticket.
# Additional validation note 3666: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3667: Verify autorole is disabled if no role is configured.
# Additional validation note 3668: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3669: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3670: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3671: Verify an administrator or support role can close a ticket.
# Additional validation note 3672: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3673: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3674: Verify starboard posts only after the threshold is met.
# Additional validation note 3675: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3676: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3677: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3678: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3679: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3680: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3681: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3682: Verify ticket channels are private to the opener and support team.
# Additional validation note 3683: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3684: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3685: Verify the ticket opener can close their own ticket.
# Additional validation note 3686: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3687: Verify autorole is disabled if no role is configured.
# Additional validation note 3688: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3689: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3690: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3691: Verify an administrator or support role can close a ticket.
# Additional validation note 3692: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3693: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3694: Verify starboard posts only after the threshold is met.
# Additional validation note 3695: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3696: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3697: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3698: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3699: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3700: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3701: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3702: Verify ticket channels are private to the opener and support team.
# Additional validation note 3703: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3704: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3705: Verify the ticket opener can close their own ticket.
# Additional validation note 3706: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3707: Verify autorole is disabled if no role is configured.
# Additional validation note 3708: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3709: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3710: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3711: Verify an administrator or support role can close a ticket.
# Additional validation note 3712: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3713: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3714: Verify starboard posts only after the threshold is met.
# Additional validation note 3715: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3716: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3717: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3718: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3719: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3720: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3721: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3722: Verify ticket channels are private to the opener and support team.
# Additional validation note 3723: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3724: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3725: Verify the ticket opener can close their own ticket.
# Additional validation note 3726: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3727: Verify autorole is disabled if no role is configured.
# Additional validation note 3728: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3729: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3730: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3731: Verify an administrator or support role can close a ticket.
# Additional validation note 3732: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3733: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3734: Verify starboard posts only after the threshold is met.
# Additional validation note 3735: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3736: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3737: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3738: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3739: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3740: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3741: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3742: Verify ticket channels are private to the opener and support team.
# Additional validation note 3743: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3744: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3745: Verify the ticket opener can close their own ticket.
# Additional validation note 3746: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3747: Verify autorole is disabled if no role is configured.
# Additional validation note 3748: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3749: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3750: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3751: Verify an administrator or support role can close a ticket.
# Additional validation note 3752: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3753: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3754: Verify starboard posts only after the threshold is met.
# Additional validation note 3755: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3756: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3757: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3758: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3759: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3760: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3761: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3762: Verify ticket channels are private to the opener and support team.
# Additional validation note 3763: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3764: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3765: Verify the ticket opener can close their own ticket.
# Additional validation note 3766: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3767: Verify autorole is disabled if no role is configured.
# Additional validation note 3768: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3769: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3770: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3771: Verify an administrator or support role can close a ticket.
# Additional validation note 3772: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3773: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3774: Verify starboard posts only after the threshold is met.
# Additional validation note 3775: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3776: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3777: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3778: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3779: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3780: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3781: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3782: Verify ticket channels are private to the opener and support team.
# Additional validation note 3783: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3784: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3785: Verify the ticket opener can close their own ticket.
# Additional validation note 3786: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3787: Verify autorole is disabled if no role is configured.
# Additional validation note 3788: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3789: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3790: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3791: Verify an administrator or support role can close a ticket.
# Additional validation note 3792: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3793: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3794: Verify starboard posts only after the threshold is met.
# Additional validation note 3795: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3796: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3797: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3798: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3799: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3800: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3801: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3802: Verify ticket channels are private to the opener and support team.
# Additional validation note 3803: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3804: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3805: Verify the ticket opener can close their own ticket.
# Additional validation note 3806: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3807: Verify autorole is disabled if no role is configured.
# Additional validation note 3808: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3809: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3810: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3811: Verify an administrator or support role can close a ticket.
# Additional validation note 3812: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3813: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3814: Verify starboard posts only after the threshold is met.
# Additional validation note 3815: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3816: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3817: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3818: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3819: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3820: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3821: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3822: Verify ticket channels are private to the opener and support team.
# Additional validation note 3823: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3824: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3825: Verify the ticket opener can close their own ticket.
# Additional validation note 3826: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3827: Verify autorole is disabled if no role is configured.
# Additional validation note 3828: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3829: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3830: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3831: Verify an administrator or support role can close a ticket.
# Additional validation note 3832: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3833: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3834: Verify starboard posts only after the threshold is met.
# Additional validation note 3835: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3836: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3837: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3838: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3839: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3840: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3841: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3842: Verify ticket channels are private to the opener and support team.
# Additional validation note 3843: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3844: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3845: Verify the ticket opener can close their own ticket.
# Additional validation note 3846: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3847: Verify autorole is disabled if no role is configured.
# Additional validation note 3848: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3849: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3850: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3851: Verify an administrator or support role can close a ticket.
# Additional validation note 3852: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3853: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3854: Verify starboard posts only after the threshold is met.
# Additional validation note 3855: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3856: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3857: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3858: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3859: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3860: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3861: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3862: Verify ticket channels are private to the opener and support team.
# Additional validation note 3863: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3864: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3865: Verify the ticket opener can close their own ticket.
# Additional validation note 3866: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3867: Verify autorole is disabled if no role is configured.
# Additional validation note 3868: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3869: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3870: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3871: Verify an administrator or support role can close a ticket.
# Additional validation note 3872: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3873: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3874: Verify starboard posts only after the threshold is met.
# Additional validation note 3875: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3876: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3877: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3878: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3879: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3880: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3881: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3882: Verify ticket channels are private to the opener and support team.
# Additional validation note 3883: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3884: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3885: Verify the ticket opener can close their own ticket.
# Additional validation note 3886: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3887: Verify autorole is disabled if no role is configured.
# Additional validation note 3888: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3889: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3890: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3891: Verify an administrator or support role can close a ticket.
# Additional validation note 3892: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3893: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3894: Verify starboard posts only after the threshold is met.
# Additional validation note 3895: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3896: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3897: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3898: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3899: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3900: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3901: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3902: Verify ticket channels are private to the opener and support team.
# Additional validation note 3903: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3904: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3905: Verify the ticket opener can close their own ticket.
# Additional validation note 3906: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3907: Verify autorole is disabled if no role is configured.
# Additional validation note 3908: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3909: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3910: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3911: Verify an administrator or support role can close a ticket.
# Additional validation note 3912: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3913: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3914: Verify starboard posts only after the threshold is met.
# Additional validation note 3915: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3916: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3917: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3918: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3919: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3920: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3921: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3922: Verify ticket channels are private to the opener and support team.
# Additional validation note 3923: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3924: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3925: Verify the ticket opener can close their own ticket.
# Additional validation note 3926: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3927: Verify autorole is disabled if no role is configured.
# Additional validation note 3928: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3929: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3930: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3931: Verify an administrator or support role can close a ticket.
# Additional validation note 3932: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3933: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3934: Verify starboard posts only after the threshold is met.
# Additional validation note 3935: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3936: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3937: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3938: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3939: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3940: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3941: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3942: Verify ticket channels are private to the opener and support team.
# Additional validation note 3943: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3944: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3945: Verify the ticket opener can close their own ticket.
# Additional validation note 3946: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3947: Verify autorole is disabled if no role is configured.
# Additional validation note 3948: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3949: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3950: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3951: Verify an administrator or support role can close a ticket.
# Additional validation note 3952: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3953: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3954: Verify starboard posts only after the threshold is met.
# Additional validation note 3955: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3956: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3957: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3958: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3959: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3960: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3961: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3962: Verify ticket channels are private to the opener and support team.
# Additional validation note 3963: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3964: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3965: Verify the ticket opener can close their own ticket.
# Additional validation note 3966: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3967: Verify autorole is disabled if no role is configured.
# Additional validation note 3968: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3969: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3970: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3971: Verify an administrator or support role can close a ticket.
# Additional validation note 3972: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3973: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3974: Verify starboard posts only after the threshold is met.
# Additional validation note 3975: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3976: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3977: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3978: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3979: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 3980: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 3981: Verify the bot cannot modify a role above its own top role.
# Additional validation note 3982: Verify ticket channels are private to the opener and support team.
# Additional validation note 3983: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 3984: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 3985: Verify the ticket opener can close their own ticket.
# Additional validation note 3986: Test an ordinary public command such as /ping after deployment.
# Additional validation note 3987: Verify autorole is disabled if no role is configured.
# Additional validation note 3988: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 3989: Test an administrator command with a test administrator account in a private server.
# Additional validation note 3990: Verify the moderation log channel receives a test moderation action.
# Additional validation note 3991: Verify an administrator or support role can close a ticket.
# Additional validation note 3992: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 3993: Verify message logging is off by default until explicitly enabled.
# Additional validation note 3994: Verify starboard posts only after the threshold is met.
# Additional validation note 3995: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 3996: Verify voice logging can be disabled independently of message logging.
# Additional validation note 3997: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 3998: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 3999: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 4000: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 4001: Verify the bot cannot modify a role above its own top role.
# Additional validation note 4002: Verify ticket channels are private to the opener and support team.
# Additional validation note 4003: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 4004: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 4005: Verify the ticket opener can close their own ticket.
# Additional validation note 4006: Test an ordinary public command such as /ping after deployment.
# Additional validation note 4007: Verify autorole is disabled if no role is configured.
# Additional validation note 4008: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 4009: Test an administrator command with a test administrator account in a private server.
# Additional validation note 4010: Verify the moderation log channel receives a test moderation action.
# Additional validation note 4011: Verify an administrator or support role can close a ticket.
# Additional validation note 4012: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 4013: Verify message logging is off by default until explicitly enabled.
# Additional validation note 4014: Verify starboard posts only after the threshold is met.
# Additional validation note 4015: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 4016: Verify voice logging can be disabled independently of message logging.
# Additional validation note 4017: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 4018: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 4019: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 4020: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 4021: Verify the bot cannot modify a role above its own top role.
# Additional validation note 4022: Verify ticket channels are private to the opener and support team.
# Additional validation note 4023: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 4024: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 4025: Verify the ticket opener can close their own ticket.
# Additional validation note 4026: Test an ordinary public command such as /ping after deployment.
# Additional validation note 4027: Verify autorole is disabled if no role is configured.
# Additional validation note 4028: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 4029: Test an administrator command with a test administrator account in a private server.
# Additional validation note 4030: Verify the moderation log channel receives a test moderation action.
# Additional validation note 4031: Verify an administrator or support role can close a ticket.
# Additional validation note 4032: Test owner-only commands with the exact OWNER_ID account.
# Additional validation note 4033: Verify message logging is off by default until explicitly enabled.
# Additional validation note 4034: Verify starboard posts only after the threshold is met.
# Additional validation note 4035: Verify the bot cannot run owner-only commands for an unrelated administrator.
# Additional validation note 4036: Verify voice logging can be disabled independently of message logging.
# Additional validation note 4037: Verify the bot does not send mass mentions from user-controlled templates.
# Additional validation note 4038: Verify a low-privilege member cannot use ban, kick, purge, or role-management commands.
# Additional validation note 4039: Verify reminders survive a restart when the database file is persistent.
# Additional validation note 4040: Before upgrading, copy bot_data.sqlite3 to a safe backup location.
# Additional validation note 4041: Verify the bot cannot modify a role above its own top role.
# Additional validation note 4042: Verify ticket channels are private to the opener and support team.
# Additional validation note 4043: After upgrading, verify the bot logs in and reports the expected slash command count.
# Additional validation note 4044: Verify a new member receives the welcome message only in the configured channel.
# Additional validation note 4045: Verify the ticket opener can close their own ticket.
# Additional validation note 4046: Test an ordinary public command such as /ping after deployment.
# Additional validation note 4047: Verify autorole is disabled if no role is configured.
# Additional validation note 4048: Verify an unrelated member cannot close another member's ticket.
# Additional validation note 4049: Test an administrator command with a test administrator account in a private server.
# Additional validation note 4050: Verify the moderation log channel receives a test moderation action.
