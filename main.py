
import os
import json
import asyncio
import threading
import datetime
import collections
import logging

from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ==========================================
# 1. 로그 설정
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("SecurityBot")

# ==========================================
# 2. Render 웹 서버
# ==========================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ==========================================
# 3. 봇 설정
# ==========================================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN 환경 변수가 없습니다.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.moderation = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

STATE_FILE = "security_state.json"

security_enabled = {}
whitelist = {}
message_tracker = collections.defaultdict(
    lambda: collections.defaultdict(collections.deque)
)

# ==========================================
# 4. 설정 저장 및 불러오기
# ==========================================
def load_state():
    global security_enabled, whitelist

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        security_enabled = {
            int(guild_id): bool(enabled)
            for guild_id, enabled in data.get("enabled", {}).items()
        }

        whitelist = {
            int(guild_id): set(map(int, user_ids))
            for guild_id, user_ids in data.get("whitelist", {}).items()
        }

        logger.info("보안 설정 파일을 불러왔습니다.")

    except FileNotFoundError:
        logger.info("기존 설정 파일이 없습니다. 새 설정을 사용합니다.")

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.error("설정 파일을 읽지 못했습니다: %s", e)


def save_state():
    data = {
        "enabled": {
            str(guild_id): enabled
            for guild_id, enabled in security_enabled.items()
        },
        "whitelist": {
            str(guild_id): list(user_ids)
            for guild_id, user_ids in whitelist.items()
        }
    }

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    except OSError as e:
        logger.error("설정 저장 실패: %s", e)


load_state()

# ==========================================
# 5. 공통 기능
# ==========================================
def is_whitelisted(guild_id, user_id, owner_id):
    return (
        user_id == owner_id
        or user_id in whitelist.get(guild_id, set())
    )


def get_default_log_channel(guild):
    me = guild.me

    if me is None:
        return None

    if guild.system_channel:
        perms = guild.system_channel.permissions_for(me)
        if perms.send_messages:
            return guild.system_channel

    for channel in guild.text_channels:
        perms = channel.permissions_for(me)
        if perms.send_messages:
            return channel

    return None


async def send_log(guild, title, description, color=discord.Color.orange()):
    channel = get_default_log_channel(guild)

    if channel is None:
        logger.warning("[%s] 로그를 보낼 수 있는 채널이 없습니다.", guild.name)
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
    )

    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        logger.error("로그 전송 실패: %s", e)


# ==========================================
# 6. 타임아웃 처리
# ==========================================
async def punish_and_log(
    guild,
    user,
    reason,
    details,
    timeout_hours=10
):
    logger.info(
        "[제재 요청] 서버=%s 대상=%s 사유=%s",
        guild.name, user, reason
    )

    try:
        me = guild.me

        if me is None:
            logger.error("봇 멤버 정보를 가져올 수 없습니다.")
            return False

        if user.id == guild.owner_id:
            logger.warning("서버 소유자는 타임아웃할 수 없습니다.")
            return False

        if user.id == bot.user.id:
            logger.warning("봇 자신은 타임아웃할 수 없습니다.")
            return False

        if user.top_role >= me.top_role:
            logger.error(
                "역할 순위 문제: 대상 역할=%s, 봇 역할=%s",
                user.top_role, me.top_role
            )
            return False

        if not me.guild_permissions.moderate_members:
            logger.error("봇에게 멤버 타임아웃 권한이 없습니다.")
            return False

        await user.timeout(
            datetime.timedelta(hours=timeout_hours),
            reason=f"[보안봇 자동제재] {reason}"
        )

        logger.info(
            "[제재 성공] %s / %s시간",
            user, timeout_hours
        )

        embed = discord.Embed(
            title="🚨 보안 위협 감지 및 자동 제재",
            description=f"**{timeout_hours}시간 타임아웃** 조치가 완료되었습니다.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(
            name="👤 제재 대상",
            value=f"{user.mention} (`{user}`)",
            inline=False
        )
        embed.add_field(name="📝 감지 사유", value=reason, inline=False)
        embed.add_field(name="📋 상세 정보", value=details, inline=False)
        embed.set_footer(text=f"Server ID: {guild.id}")

        channel = get_default_log_channel(guild)

        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                logger.error("제재는 성공했지만 로그 전송에 실패했습니다: %s", e)

        return True

    except discord.Forbidden as e:
        logger.error(
            "타임아웃 권한 거부: %s | 봇 역할 순위와 권한을 확인하세요.",
            e
        )

    except discord.HTTPException as e:
        logger.error(
            "Discord API 오류: HTTP %s / %s",
            e.status, e.text
        )

    except Exception:
        logger.exception("타임아웃 처리 중 예상하지 못한 오류")

    return False


# ==========================================
# 7. 봇 시작
# ==========================================
@bot.event
async def on_ready():
    logger.info("로그인 성공: %s (%s)", bot.user, bot.user.id)

    for guild in bot.guilds:
        logger.info(
            "서버 확인: %s | 보안 활성화=%s",
            guild.name, security_enabled.get(guild.id, False)
        )

    if not getattr(bot, "_synced", False):
        try:
            synced = await bot.tree.sync()
            logger.info("슬래시 명령어 %s개 동기화 완료", len(synced))
            bot._synced = True
        except Exception:
            logger.exception("슬래시 명령어 동기화 실패")


# ==========================================
# 8. 보안 설정 명령어
# ==========================================
@bot.tree.command(
    name="설정",
    description="서버 보안 감지 기능을 활성화합니다."
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_security(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버 안에서 사용해 주세요.",
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    security_enabled[guild_id] = True
    whitelist.setdefault(guild_id, set())
    save_state()

    logger.info("[%s] 보안 기능 활성화", interaction.guild.name)

    await interaction.response.send_message(
        "🛡️ 보안 기능이 활성화되었습니다.\n"
        "• 채널 생성 및 삭제 감지\n"
        "• 3초 동안 메시지 5개 이상 전송 감지\n"
        "• 채널 변경 제재: 10시간\n"
        "• 도배 제재: 4시간\n\n"
        "주의: 봇 권한과 역할 순위에 따라 제재가 실패할 수 있습니다.",
        ephemeral=True
    )


@bot.tree.command(
    name="보안상태",
    description="보안 기능과 봇의 권한 상태를 확인합니다."
)
@app_commands.checks.has_permissions(administrator=True)
async def security_status(interaction: discord.Interaction):
    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "서버 안에서 사용해 주세요.",
            ephemeral=True
        )
        return

    me = guild.me
    enabled = security_enabled.get(guild.id, False)

    if me is None:
        permission_text = "봇 멤버 정보를 확인할 수 없습니다."
    else:
        permission_text = (
            f"타임아웃 권한: {'있음' if me.guild_permissions.moderate_members else '없음'}\n"
            f"감사 로그 보기: {'있음' if me.guild_permissions.view_audit_log else '없음'}\n"
            f"봇 최고 역할: {me.top_role.mention}"
        )

    await interaction.response.send_message(
        f"🛡️ **보안 상태**\n"
        f"보안 기능: {'활성화' if enabled else '비활성화'}\n"
        f"화이트리스트 인원: {len(whitelist.get(guild.id, set()))}명\n\n"
        f"**권한 확인**\n{permission_text}",
        ephemeral=True
    )


# ==========================================
# 9. 화이트리스트
# ==========================================
whitelist_group = app_commands.Group(
    name="화이트리스트",
    description="보안 감지 예외 유저 관리"
)


@whitelist_group.command(name="추가", description="예외 유저를 추가합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_add(
    interaction: discord.Interaction,
    member: discord.Member
):
    guild_id = interaction.guild_id

    whitelist.setdefault(guild_id, set()).add(member.id)
    save_state()

    await interaction.response.send_message(
        f"✅ {member.mention} 님을 화이트리스트에 추가했습니다.",
        ephemeral=True
    )


@whitelist_group.command(name="제거", description="예외 유저를 제거합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_remove(
    interaction: discord.Interaction,
    member: discord.Member
):
    guild_id = interaction.guild_id
    members = whitelist.setdefault(guild_id, set())

    if member.id not in members:
        await interaction.response.send_message(
            "⚠️ 해당 유저는 화이트리스트에 없습니다.",
            ephemeral=True
        )
        return

    members.remove(member.id)
    save_state()

    await interaction.response.send_message(
        f"❌ {member.mention} 님을 화이트리스트에서 제거했습니다.",
        ephemeral=True
    )


bot.tree.add_command(whitelist_group)


# ==========================================
# 10. 감사 로그에서 행위자 확인
# ==========================================
async def find_audit_actor(guild, action, target_id):
    me = guild.me

    if me is None or not me.guild_permissions.view_audit_log:
        logger.error("[%s] 감사 로그 보기 권한이 없습니다.", guild.name)
        return None

    # Discord 감사 로그 반영 지연을 고려합니다.
    await asyncio.sleep(1.5)

    try:
        async for entry in guild.audit_logs(action=action, limit=8):
            target = entry.target

            if getattr(target, "id", None) != target_id:
                continue

            age = (
                discord.utils.utcnow() - entry.created_at
            ).total_seconds()

            # 너무 오래된 작업 기록은 무시합니다.
            if age < 0 or age > 20:
                continue

            logger.info(
                "[감사 로그] 작업=%s 행위자=%s 대상=%s",
                action, entry.user, target_id
            )
            return entry.user

    except discord.Forbidden:
        logger.error("감사 로그 접근이 거부되었습니다.")

    except discord.HTTPException as e:
        logger.error("감사 로그 조회 실패: %s", e)

    logger.warning(
        "[감사 로그] 행위자를 찾지 못했습니다. 대상 ID=%s",
        target_id
    )
    return None


# ==========================================
# 11. 채널 삭제 감지
# ==========================================
@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild

    logger.info(
        "[감지 이벤트] 채널 삭제: %s / 보안=%s",
        channel.name, security_enabled.get(guild.id, False)
    )

    if not security_enabled.get(guild.id, False):
        return

    actor = await find_audit_actor(
        guild,
        discord.AuditLogAction.channel_delete,
        channel.id
    )

    if actor is None or actor.bot:
        return

    if is_whitelisted(guild.id, actor.id, guild.owner_id):
        logger.info("화이트리스트 대상의 채널 삭제입니다.")
        return

    member = guild.get_member(actor.id)

    if member is None:
        logger.warning("삭제 행위자를 서버 멤버로 찾지 못했습니다: %s", actor)
        return

    await punish_and_log(
        guild,
        member,
        "무단 채널 삭제 감지",
        f"삭제된 채널: #{channel.name}",
        timeout_hours=10
    )


# ==========================================
# 12. 채널 생성 감지
# ==========================================
@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild

    logger.info(
        "[감지 이벤트] 채널 생성: %s / 보안=%s",
        channel.name, security_enabled.get(guild.id, False)
    )

    if not security_enabled.get(guild.id, False):
        return

    actor = await find_audit_actor(
        guild,
        discord.AuditLogAction.channel_create,
        channel.id
    )

    if actor is None or actor.bot:
        return

    if is_whitelisted(guild.id, actor.id, guild.owner_id):
        logger.info("화이트리스트 대상의 채널 생성입니다.")
        return

    member = guild.get_member(actor.id)

    if member:
        await punish_and_log(
            guild,
            member,
            "무단 채널 생성 감지",
            f"생성된 채널: #{channel.name}",
            timeout_hours=10
        )

    try:
        await channel.delete(reason="보안봇 자동 원상복구")
        logger.info("생성된 채널을 삭제했습니다: %s", channel.name)
    except discord.Forbidden:
        logger.error("채널 삭제 권한이 없습니다.")
    except discord.HTTPException as e:
        logger.error("채널 삭제 실패: %s", e)


# ==========================================
# 13. 채팅 도배 감지
# ==========================================
@bot.event
async def on_message(message):
    if message.author.bot or message.guild is None:
        return

    guild = message.guild
    user = message.author

    if not security_enabled.get(guild.id, False):
        await bot.process_commands(message)
        return

    if is_whitelisted(guild.id, user.id, guild.owner_id):
        await bot.process_commands(message)
        return

    now = discord.utils.utcnow().timestamp()
    timestamps = message_tracker[guild.id][user.id]

    # 최근 3초 이내의 메시지만 유지
    while timestamps and now - timestamps[0] >= 3:
        timestamps.popleft()

    timestamps.append(now)

    logger.info(
        "[메시지 감지] 서버=%s 유저=%s 최근 3초 메시지=%s",
        guild.name, user, len(timestamps)
    )

    if len(timestamps) >= 5:
        timestamps.clear()

        member = guild.get_member(user.id)

        if member:
            logger.warning("[도배 감지] %s", user)

            await punish_and_log(
                guild,
                member,
                "채팅 도배(스팸) 행위 감지",
                f"채널: {message.channel.mention}\n"
                f"메시지 예시: {message.content[:200]}",
                timeout_hours=4
            )

    await bot.process_commands(message)


# ==========================================
# 14. 오류 확인
# ==========================================
@bot.tree.error
async def on_app_command_error(interaction, error):
    logger.error(
        "슬래시 명령어 오류: %s",
        error,
        exc_info=(type(error), error, error.__traceback__)
    )

    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어를 사용하려면 관리자 권한이 필요합니다."
    else:
        message = "명령어 실행 중 오류가 발생했습니다. 봇 콘솔을 확인해 주세요."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


# ==========================================
# 15. 실행
# ==========================================
bot.run(TOKEN)
