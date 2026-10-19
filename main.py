import os
import asyncio
import logging
from collections import Counter
import discord
from discord import app_commands
from discord.ext import commands
# =====================================================
# CẤU HÌNH
# =====================================================
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = 1356498767788118099
# Cập nhật tiến độ sau mỗi số kênh này
PROGRESS_EVERY = 5
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.members = True
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)
# Chỉ cho phép một lượt quét chạy tại một thời điểm
scan_lock = asyncio.Lock()
# =====================================================
# KIỂM TRA OWNER
# =====================================================
def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID
# =====================================================
# LẤY CÁC KÊNH BOT CÓ QUYỀN ĐỌC
# =====================================================
def get_readable_channels(guild: discord.Guild):
    me = guild.me
    if me is None:
        return []
    channels = []
    for channel in guild.text_channels:
        permissions = channel.permissions_for(me)
        if (
            permissions.view_channel
            and permissions.read_message_history
        ):
            channels.append(channel)
    return channels
# =====================================================
# QUÉT LỊCH SỬ TIN NHẮN
# =====================================================
async def scan_channels(
    channels,
    target_user_id=None,
    progress_message=None
):
    """
    Quét mỗi kênh một lần.
    target_user_id=None:
        Đếm tin nhắn của tất cả người dùng.
    target_user_id=<ID>:
        Chỉ đếm tin nhắn của người được chọn,
        đồng thời vẫn đếm tổng số tin nhắn từng người
        để tính thứ hạng chính xác.
    """
    counter = Counter()
    scanned_channels = 0
    total_channels = len(channels)
    failed_channels = []
    for channel in channels:
        try:
            async for message in channel.history(
                limit=None,
                oldest_first=False
            ):
                # Bỏ qua bot và webhook
                if message.author.bot:
                    continue
                if message.webhook_id is not None:
                    continue
                # Một lượt quét thống kê tất cả người dùng.
                # Điều này cho phép tính thứ hạng mà không
                # cần gọi API đọc lịch sử thêm lần nữa.
                counter[message.author.id] += 1
        except discord.Forbidden:
            failed_channels.append(channel.name)
            logging.warning(
                "Không có quyền đọc lịch sử kênh: %s",
                channel.name
            )
        except discord.HTTPException as exc:
            failed_channels.append(channel.name)
            logging.warning(
                "Lỗi khi quét kênh %s: %s",
                channel.name,
                exc
            )
        except asyncio.CancelledError:
            raise
        scanned_channels += 1
        # Chỉ cập nhật tiến độ mỗi vài kênh
        should_update = (
            scanned_channels % PROGRESS_EVERY == 0
            or scanned_channels == total_channels
        )
        if progress_message is not None and should_update:
            try:
                await progress_message.edit(
                    content=(
                        "🔎 Đang quét lịch sử tin nhắn...\n"
                        f"📚 Tiến độ: "
                        f"{scanned_channels}/{total_channels} kênh"
                    )
                )
            except discord.HTTPException:
                pass
        # Nhường quyền chạy cho tác vụ khác
        await asyncio.sleep(0)
    return counter, scanned_channels, failed_channels
# =====================================================
# TẠO EMBED TOP 10
# =====================================================
def make_top_embed(
    guild,
    counter,
    scope_name,
    scanned_channels,
    failed_channels
):
    embed = discord.Embed(
        title="🏆 TOP 10 NGƯỜI NHẮN TIN NHIỀU NHẤT",
        description=(
            f"📍 Phạm vi: **{scope_name}**\n"
            f"🗂️ Số kênh đã xử lý: **{scanned_channels}**"
        ),
        color=discord.Color.blurple()
    )
    ranking = counter.most_common(10)
    if not ranking:
        embed.add_field(
            name="Không có dữ liệu",
            value="Không tìm thấy tin nhắn phù hợp.",
            inline=False
        )
    else:
        lines = []
        medals = ["🥇", "🥈", "🥉"]
        for index, (user_id, count) in enumerate(ranking):
            member = guild.get_member(user_id)
            if member:
                username = member.display_name
            else:
                user = bot.get_user(user_id)
                username = (
                    user.name if user
                    else f"User {user_id}"
                )
            prefix = (
                medals[index]
                if index < 3
                else f"**{index + 1}.**"
            )
            lines.append(
                f"{prefix} "
                f"**{discord.utils.escape_markdown(username)}**"
                f" — **{count:,}** tin nhắn"
            )
        embed.add_field(
            name="Bảng xếp hạng",
            value="\n".join(lines),
            inline=False
        )
    if failed_channels:
        embed.add_field(
            name="⚠️ Kênh chưa quét được",
            value=(
                f"**{len(failed_channels)}** kênh gặp lỗi "
                "quyền truy cập hoặc API. "
                "Số liệu có thể chưa đầy đủ."
            ),
            inline=False
        )
    embed.set_footer(
        text="Chỉ tính tin nhắn người dùng còn trong lịch sử."
    )
    return embed
# =====================================================
# LỆNH /QUET
# =====================================================
@bot.tree.command(
    name="quet",
    description="Quét TOP 10 người nhắn tin nhiều nhất"
)
@app_commands.describe(
    kenh="Chọn kênh cần quét. Bỏ trống để quét toàn server."
)
async def quet(
    interaction: discord.Interaction,
    kenh: discord.TextChannel = None
):
    if not is_owner(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có quyền sử dụng lệnh này.",
            ephemeral=True
        )
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng được trong server.",
            ephemeral=True
        )
        return
    if scan_lock.locked():
        await interaction.response.send_message(
            "⏳ Một lượt quét đang chạy. "
            "Vui lòng đợi lượt đó hoàn thành.",
            ephemeral=True
        )
        return
    if kenh is not None:
        me = guild.me
        permissions = kenh.permissions_for(me)
        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            await interaction.response.send_message(
                "❌ Bot không có quyền đọc lịch sử kênh này.",
                ephemeral=True
            )
            return
        channels = [kenh]
        scope_name = f"#{kenh.name}"
    else:
        channels = get_readable_channels(guild)
        scope_name = "Toàn server"
    if not channels:
        await interaction.response.send_message(
            "❌ Không tìm thấy kênh nào có thể quét.",
            ephemeral=True
        )
        return
    await interaction.response.defer(
        ephemeral=False,
        thinking=True
    )
    async with scan_lock:
        progress = await interaction.followup.send(
            "🔎 Đang bắt đầu quét lịch sử tin nhắn...",
            wait=True
        )
        try:
            counter, scanned, failed = await scan_channels(
                channels,
                progress_message=progress
            )
            embed = make_top_embed(
                guild,
                counter,
                scope_name,
                scanned,
                failed
            )
            await progress.edit(
                content="✅ Đã hoàn tất quét lịch sử tin nhắn!",
                embed=embed
            )
        except Exception:
            logging.exception("Lỗi khi chạy lệnh /quet")
            await progress.edit(
                content=(
                    "❌ Có lỗi khi quét lịch sử. "
                    "Hãy kiểm tra log Railway."
                ),
                embed=None
            )
# =====================================================
# LỆNH /SOTN
# =====================================================
@bot.tree.command(
    name="sotn",
    description="Kiểm tra số tin nhắn của một người"
)
@app_commands.describe(
    ten="Người dùng cần kiểm tra",
    pham_vi="Chọn all hoặc kenh",
    kenh="Chọn kênh nếu phạm vi là kenh"
)
@app_commands.choices(
    pham_vi=[
        app_commands.Choice(
            name="all - Toàn server",
            value="all"
        ),
        app_commands.Choice(
            name="kenh - Một kênh",
            value="kenh"
        )
    ]
)
async def sotn(
    interaction: discord.Interaction,
    ten: discord.Member,
    pham_vi: app_commands.Choice[str],
    kenh: discord.TextChannel = None
):
    if not is_owner(interaction):
        await interaction.response.send_message(
            "❌ Bạn không có quyền sử dụng lệnh này.",
            ephemeral=True
        )
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng được trong server.",
            ephemeral=True
        )
        return
    if ten.bot:
        await interaction.response.send_message(
            "❌ Không thể thống kê tài khoản bot.",
            ephemeral=True
        )
        return
    if scan_lock.locked():
        await interaction.response.send_message(
            "⏳ Một lượt quét đang chạy. "
            "Vui lòng đợi lượt đó hoàn thành.",
            ephemeral=True
        )
        return
    if pham_vi.value == "kenh":
        if kenh is None:
            await interaction.response.send_message(
                "❌ Bạn cần chọn kênh khi phạm vi là kenh.",
                ephemeral=True
            )
            return
        permissions = kenh.permissions_for(guild.me)
        if not (
            permissions.view_channel
            and permissions.read_message_history
        ):
            await interaction.response.send_message(
                "❌ Bot không có quyền đọc lịch sử kênh này.",
                ephemeral=True
            )
            return
        channels = [kenh]
        scope_name = f"#{kenh.name}"
    else:
        channels = get_readable_channels(guild)
        scope_name = "Toàn server"
    if not channels:
        await interaction.response.send_message(
            "❌ Không tìm thấy kênh nào có thể quét.",
            ephemeral=True
        )
        return
    await interaction.response.defer(
        ephemeral=False,
        thinking=True
    )
    async with scan_lock:
        progress = await interaction.followup.send(
            f"🔎 Đang thống kê tin nhắn của {ten.display_name}...",
            wait=True
        )
        try:
            # Chỉ quét một lượt. Counter chứa số tin nhắn
            # của tất cả thành viên để tính cả số lượng
            # và thứ hạng của người được chọn.
            counter, scanned, failed = await scan_channels(
                channels,
                progress_message=progress
            )
            message_count = counter.get(ten.id, 0)
            ranking = counter.most_common()
            rank = next(
                (
                    index
                    for index, (user_id, _) in enumerate(
                        ranking,
                        start=1
                    )
                    if user_id == ten.id
                ),
                None
            )
            embed = discord.Embed(
                title="📊 THỐNG KÊ TIN NHẮN",
                color=discord.Color.green()
            )
            embed.add_field(
                name="👤 Người dùng",
                value=ten.mention,
                inline=False
            )
            embed.add_field(
                name="💬 Tổng số tin nhắn",
                value=f"**{message_count:,}**",
                inline=True
            )
            embed.add_field(
                name="🏆 Xếp hạng",
                value=f"**#{rank}**" if rank else "Chưa xếp hạng",
                inline=True
            )
            embed.add_field(
                name="📍 Phạm vi",
                value=scope_name,
                inline=True
            )
            embed.add_field(
                name="🗂️ Số kênh đã xử lý",
                value=str(scanned),
                inline=True
            )
            if failed:
                embed.add_field(
                    name="⚠️ Kênh chưa quét được",
                    value=(
                        f"{len(failed)} kênh gặp lỗi. "
                        "Kết quả có thể chưa đầy đủ."
                    ),
                    inline=False
                )
            embed.set_footer(
                text="Thống kê lịch sử tin nhắn mà bot có thể truy cập."
            )
            await progress.edit(
                content="✅ Đã hoàn tất thống kê!",
                embed=embed
            )
        except Exception:
            logging.exception("Lỗi khi chạy lệnh /sotn")
            await progress.edit(
                content=(
                    "❌ Có lỗi khi thống kê tin nhắn. "
                    "Hãy kiểm tra log Railway."
                ),
                embed=None
            )
# =====================================================
# ĐỒNG BỘ SLASH COMMAND
# =====================================================
@bot.event
async def on_ready():
    logging.info("Bot đã đăng nhập: %s", bot.user)
    if not getattr(bot, "commands_synced", False):
        try:
            synced = await bot.tree.sync()
            logging.info(
                "Đã đồng bộ %s slash command.",
                len(synced)
            )
            bot.commands_synced = True
        except Exception:
            logging.exception(
                "Không thể đồng bộ slash command."
            )
# =====================================================
# KHỞI ĐỘNG
# =====================================================
if not TOKEN:
    raise RuntimeError(
        "Thiếu DISCORD_TOKEN trong Environment Variables."
    )
bot.run(TOKEN)