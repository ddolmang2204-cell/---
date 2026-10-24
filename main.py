import os
import discord
from discord import app_commands
from discord.ext import commands
import datetime
import collections
from dotenv import load_dotenv

# 로컬 환경에서는 .env 파일을 로드하고, Render 등에서는 시스템 환경 변수를 사용합니다.
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("오류: DISCORD_TOKEN 환경 변수가 설정되지 않았습니다.")
    exit(1)

# 봇 기본 설정 (Intents 설정)
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.moderation = True  
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 메모리 기반 데이터 저장소
whitelist = {}        # { guild_id: set(user_ids) }
security_enabled = {} # { guild_id: bool }

# 감지 시스템용 추적 데이터 (단시간 반복 테러 감지용)
action_tracker = collections.defaultdict(lambda: collections.defaultdict(list))

# 화이트리스트 체크 함수
def is_whitelisted(guild_id: int, user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:  # 서버 소유자는 항상 예외
        return True
    return user_id in whitelist.get(guild_id, set())

# 초당 동작 수 체크 (예: 5초 내 3회 이상 채널 삭제 시 테러 감지)
def is_suspicious_activity(guild_id: int, user_id: int, action_type: str, threshold: int = 3, timeframe: int = 5) -> bool:
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    user_actions = action_tracker[guild_id][(user_id, action_type)]
    
    # 지정된 시간 내의 기록만 남기기
    user_actions = [t for t in user_actions if now - t < timeframe]
    user_actions.append(now)
    action_tracker[guild_id][(user_id, action_type)] = user_actions
    
    return len(user_actions) >= threshold

# 모든 사람이 볼 수 있는 일반 채팅 채널을 자동으로 찾는 함수
def get_default_log_channel(guild: discord.Guild) -> discord.TextChannel:
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            return channel
            
    return None

# 제재 및 임베드 로그 전송 함수 (10시간 타임아웃 + 임베드 알림)
async def punish_and_log(guild: discord.Guild, user: discord.Member, reason: str, details: str):
    try:
        # 1. 10시간 타임아웃 적용
        duration = datetime.timedelta(hours=10)
        await user.timeout(duration, reason=f"[보안봇 자동제재] {reason}")
        
        # 2. 일반 채널을 자동으로 찾아 임베드 전송
        log_channel = get_default_log_channel(guild)
        if log_channel:
            embed = discord.Embed(
                title="🚨 보안 위협 감지 및 자동 제재",
                description=f"비동기적 테러 행위가 감지되어 **10시간 타임아웃** 조치되었습니다.",
                color=discord.Color.red(),
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )
            embed.add_field(name="👤 제재 대상", value=f"{user.mention} (`{user}`)", inline=False)
            embed.add_field(name="📝 감지 사유", value=reason, inline=False)
            embed.add_field(name="📋 상세 정보", value=details, inline=False)
            embed.set_footer(text=f"Server ID: {guild.id}")
            
            await log_channel.send(embed=embed)
                
    except discord.Forbidden:
        print(f"[{guild.name}] 권한이 부족하여 {user}를 제재하지 못했습니다.")
    except Exception as e:
        print(f"제재 처리 중 오류 발생: {e}")

@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user.name} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"슬래시 명령어 {len(synced)}개 동기화 완료.")
    except Exception as e:
        print(f"명령어 동기화 실패: {e}")

# ==================== 슬래시 명령어 ====================

@bot.tree.command(name="설정", description="서버 보안 감지 기능을 활성화합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_security(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    security_enabled[guild_id] = True
    
    if guild_id not in whitelist:
        whitelist[guild_id] = set()
        
    embed = discord.Embed(
        title="🛡️ 서버 보안 설정 완료",
        description="서버 자동 보안 모드가 **활성화**되었습니다.\n테러 감지 시 10시간 타임아웃과 함께 서버의 기본 채팅 채널에 경고 임베드가 전송됩니다.",
        color=discord.Color.green()
    )
    embed.add_field(name="보안 감지 항목", value="• 연속 채널 삭제 테러\n• 연속 채널 생성 테러", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 화이트리스트 그룹 명령어
whitelist_group = app_commands.Group(name="화이트리스트", description="화이트리스트 관리 명령어")

@whitelist_group.command(name="추가", description="보안 감지 예외 대상 유저를 화이트리스트에 추가합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_add(interaction: discord.Interaction, member: discord.Member):
    guild_id = interaction.guild_id
    if guild_id not in whitelist:
        whitelist[guild_id] = set()
        
    whitelist[guild_id].add(member.id)
    await interaction.response.send_message(f"✅ {member.mention} 님이 보안 화이트리스트에 추가되었습니다.", ephemeral=True)

@whitelist_group.command(name="제거", description="유저를 화이트리스트에서 제거합니다.")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_remove(interaction: discord.Interaction, member: discord.Member):
    guild_id = interaction.guild_id
    if guild_id in whitelist and member.id in whitelist[guild_id]:
        whitelist[guild_id].remove(member.id)
        await interaction.response.send_message(f"❌ {member.mention} 님이 화이트리스트에서 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {member.mention} 님은 화이트리스트에 존재하지 않습니다.", ephemeral=True)

bot.tree.add_command(whitelist_group)

# ==================== 비동기 감지 이벤트 ====================

# 1. 채널 삭제 감지 (원래 채널 이름 임베드 출력)
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    guild = channel.guild
    if not security_enabled.get(guild.id, False):
        return

    async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_delete, limit=1):
        user = entry.user
        if not user or user.bot or is_whitelisted(guild.id, user.id, guild.owner_id):
            return

        # 5초 내에 3개 이상 채널 삭제 시 테러 감지
        if is_suspicious_activity(guild.id, user.id, "channel_delete", threshold=3, timeframe=5):
            member = guild.get_member(user.id)
            if member:
                deleted_channel_info = f"• 삭제된 채널 이름: **#{channel.name}**\n• 채널 유형: `{channel.type}`\n• 채널 ID: `{channel.id}`"
                await punish_and_log(guild, member, "단시간 연속 채널 삭제 테러 감지", deleted_channel_info)

# 2. 채널 생성 감지
@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel):
    guild = channel.guild
    if not security_enabled.get(guild.id, False):
        return

    async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_create, limit=1):
        user = entry.user
        if not user or user.bot or is_whitelisted(guild.id, user.id, guild.owner_id):
            return

        if is_suspicious_activity(guild.id, user.id, "channel_create", threshold=3, timeframe=5):
            member = guild.get_member(user.id)
            if member:
                created_channel_info = f"• 생성된 채널 이름: **#{channel.name}**"
                await punish_and_log(guild, member, "단시간 연속 채널 생성 테러 감지", created_channel_info)
            try:
                await channel.delete(reason="보안봇 자동 원상복구")
            except:
                pass

# 봇 실행
bot.run(TOKEN)