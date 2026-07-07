"""
Fury Tiers — Discord tier-testing bot
[1.21+] Fury Tiers [NA]

Commands (tester-only unless noted):
  /queue           Open a queue, or join an open one as an active tester.
  /pull [user]     Pull someone from the queue (pick the Discord user) into a private channel.
  /leave           (anyone) Remove yourself from the queue.
  /closequeue      Close the queue and post the "No Testers Online" message.
  /passeval        Inside a test channel: pass the player to high tier (guaranteed LT3),
                   rename the channel to hightest-N.
  /resetcooldown   Clear a chosen member's test cooldown so they can re-queue.
  /settier         Manually set a player's tier in the tier API (backfill existing ranks).
  /backfill        Scan the results channel and add all previously-tested players at once.
  /removetier      Remove a Minecraft player's tier from the API.

Buttons:
  Join Queue       (anyone) Opens a modal for Minecraft username + preferred server.
  Skip / Close     (testers) Inside a test channel. Close submits a result to the results
                   channel; Skip cancels without a result.

Cooldown:
  After a result is submitted via Close, that player waits one day before rejoining the
  queue. Leaving the queue or being skipped does NOT start a cooldown.

Roles:
  On Close, the bot gives the player the role named exactly like the earned tier
  (e.g. "LT5", "HT3") and removes any previous LT*/HT* tier roles. Requires Manage
  Roles, and the bot's role must sit above the tier roles.

Tier API:
  The bot runs a small HTTP API (API_HOST:API_PORT) that the Fury Tier Tagger mod
  reads from: GET /tier/<username-or-uuid> returns that player's tier as JSON.
  Tiers are recorded automatically on Close (UUID resolved via Mojang) and can be
  added manually with /settier. To reach it from other machines it must be hosted
  publicly or tunneled.
"""

import os
import re
import time
import json
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ----------------------------- Configuration -----------------------------
# RESET your token first, then set it as the BOT_TOKEN env var (or in a .env file).
TOKEN = os.environ.get("BOT_TOKEN") or ""

# Right-click your server name -> Copy Server ID. Strongly recommended (instant command sync).
# Can be set in code here, OR via the GUILD_ID environment variable on Render.
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))

TIER_TESTER_ROLE_ID = 1518428437419786324
OWNER_ROLE_ID       = 1518428437419786328   # only this role can use admin commands
REQUEST_CHANNEL_ID  = 1518428441542922263   # 🎫•request-test
TICKET_CATEGORY_ID  = 1518428441542922262   # category for test channels
RESULTS_CHANNEL_ID  = 1518428441089933364   # 🏆•results
PUNISHMENT_CHANNEL_ID = 1518428440649269407 # ⛔•punishment

# Restriction types -> the role to apply and how long it lasts.
# Discord caps native timeouts at 28 days, so anything longer (e.g. Alting's
# 30 days) keeps the role for the full time but the timeout itself is capped.
RESTRICTIONS = {
    "Cheating": {"role_id": 1518758986143502336, "days": 7,  "emoji": "🚫",
                 "phrase": "Cheating"},
    "Alting":   {"role_id": 1518759322199392459, "days": 30, "emoji": "👥",
                 "phrase": "Alting the Tierlist and Bypassing Testing Cooldown"},
    "Threats":  {"role_id": 1518759250975789126, "days": 14, "emoji": "⚠️",
                 "phrase": "Making Threats"},
    "Toxicity": {"role_id": 1518759156423725235, "days": 7,  "emoji": "🤬",
                 "phrase": "Toxicity"},
}
DISCORD_MAX_TIMEOUT_DAYS = 28

REGION      = "NA"
DEFAULT_KIT = "Crystal"
MAX_QUEUE   = 10
TEST_COOLDOWN = 24 * 60 * 60   # one day cooldown after a player is tested (in seconds)

CLOSE_DELAY = 8   # seconds before a closed channel is deleted
SKIP_DELAY  = 5

QUEUE_COLOR   = 0x5865F2
RESULTS_COLOR = 0x1ABC9C
CLOSED_COLOR  = 0xED4245
TICKET_COLOR  = 0x5865F2

DATA_FILE = "data.json"

# --- Tier API (the in-game Fury Tier Tagger mod reads from this) ---
API_HOST = "0.0.0.0"    # 0.0.0.0 = listen on all interfaces so the mod can reach it
# Render (and most cloud hosts) tell the app which port to use via the PORT env
# var. Use it when present; fall back to 8080 when running on your own PC.
API_PORT = int(os.environ.get("PORT", "8080"))


# ------------------------------ Persistence ------------------------------
state = {"guilds": {}}


def load_state():
    global state
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {"guilds": {}}
    state.setdefault("guilds", {})
    raw = state.get("players", {})
    if isinstance(raw, dict):
        migrated = {}
        for key, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            uuid = (rec.get("uuid") or "").lower()
            if key.startswith("name:") or (len(key) == 32 and all(c in "0123456789abcdef" for c in key)):
                migrated[key] = rec
            elif uuid:
                migrated[uuid] = rec
            else:
                migrated["name:" + key.lower()] = rec
        state["players"] = migrated
    else:
        state["players"] = {}


def save_state():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print("Failed to save state:", e)


def get_guild_state(guild_id):
    g = state["guilds"].setdefault(str(guild_id), {})
    g.setdefault("queue", [])               # [{user_id, mc, server, joined}]
    g.setdefault("testers", [])             # [user_id]
    g.setdefault("kit", DEFAULT_KIT)
    g.setdefault("queue_message_id", None)
    g.setdefault("closed_message_id", None)
    g.setdefault("last_test", None)         # unix timestamp
    g.setdefault("tickets", {})             # {channel_id: {player_id, mc, server, kind, provisional}}
    g.setdefault("cooldowns", {})           # {user_id: unix timestamp of last completed test}
    g.setdefault("user_accounts", {})       # {user_id: [mc uuids they've been tested on]}
    g.setdefault("offenses", {})            # {user_id: {reason: count}}
    g.setdefault("active_restrictions", []) # [{user_id, role_id, until, reason}]
    return g


load_state()


# ------------------------------- Helpers ---------------------------------
def member_is_tester(member) -> bool:
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    return any(r.id == TIER_TESTER_ROLE_ID for r in getattr(member, "roles", []))


def member_is_owner(member) -> bool:
    # Only members with the owner role may use the admin commands
    # (resetcooldown, settier, removetier, backfill).
    if member is None:
        return False
    return any(r.id == OWNER_ROLE_ID for r in getattr(member, "roles", []))


def ordinal(n: int) -> str:
    # 1 -> "1st", 2 -> "2nd", 3 -> "3rd", 4 -> "4th", ...
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def uuid_to_name(uuid):
    # Best-effort: find the username we have on record for a UUID.
    if not uuid:
        return None
    rec = state.get("players", {}).get(uuid)
    if rec and rec.get("username"):
        return rec["username"]
    return None


def _format_uuid(uuid):
    # Show the dashed UUID form like the example (8-4-4-4-12).
    if not uuid:
        return None
    h = uuid.replace("-", "")
    if len(h) == 32:
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
    return uuid


def build_restriction_embed(member, reason, accounts, offense_num,
                            moderator=None, auto=False):
    """accounts: list of (username, uuid) tuples for the account(s) involved."""
    info = RESTRICTIONS.get(reason, {})
    phrase = info.get("phrase", reason)
    days = info.get("days", 0)
    capped = min(days, DISCORD_MAX_TIMEOUT_DAYS)

    # Clean account list (skip empties, de-dup, keep order).
    clean = []
    seen = set()
    for name, uuid in accounts or []:
        key = (name or "", uuid or "")
        if key in seen:
            continue
        seen.add(key)
        if name or uuid:
            clean.append((name or "unknown", uuid))

    # --- Header line, mirroring the example:
    #   @user / @user — name1 / name2 — Restricted for <phrase>
    mention = member.mention
    mentions = " / ".join([mention] * max(1, len(clean)))
    names = " / ".join(f"**{n}**" for n, _ in clean) if clean else "**unknown**"
    header = f"{mentions} — {names}\nRestricted for **{phrase}**"

    # --- Account lines: `name` — `uuid`
    lines = []
    for name, uuid in clean:
        u = _format_uuid(uuid)
        if u:
            lines.append(f"`{name}` — `{u}`")
        else:
            lines.append(f"`{name}`")

    desc = header
    if lines:
        desc += "\n\n" + "\n".join(lines)

    embed = discord.Embed(
        description=desc,
        color=0xED4245,
        timestamp=datetime.now(timezone.utc),
    )

    footer_bits = [f"{days} Days", f"{ordinal(offense_num)} Offense"]
    if auto:
        footer_bits.append("Auto-detected")
    if moderator is not None:
        footer_bits.append(f"By {moderator.display_name}")
    embed.set_footer(text="  •  ".join(footer_bits))
    return embed, (days > capped)


async def apply_restriction(guild, member, reason, accounts=None,
                            moderator=None, auto=False):
    """Time the member out, give them the restriction role, announce it (matching
    the punishment-message style), and track it for automatic removal on expiry.
    accounts: list of (username, uuid) tuples involved (one for most; two for
    alting - the old account and the new one).
    Returns (ok: bool, detail: str)."""
    info = RESTRICTIONS.get(reason)
    if info is None:
        return False, f"Unknown restriction reason: {reason}"

    gs = get_guild_state(guild.id)
    uid = str(member.id)

    user_off = gs["offenses"].setdefault(uid, {})
    user_off[reason] = int(user_off.get(reason, 0)) + 1
    offense_num = user_off[reason]

    days = info["days"]
    role_id = info["role_id"]
    capped_days = min(days, DISCORD_MAX_TIMEOUT_DAYS)
    until_real = int(time.time()) + days * 86400

    # 1) Timeout (capped at Discord's 28-day max).
    timeout_failed = False
    try:
        until_dt = datetime.now(timezone.utc) + timedelta(days=capped_days)
        await member.timeout(until_dt, reason=f"{reason} restriction")
    except (discord.Forbidden, discord.HTTPException):
        timeout_failed = True

    # 2) Restriction role.
    role_failed = False
    role = guild.get_role(role_id)
    if role is not None:
        try:
            await member.add_roles(role, reason=f"{reason} restriction")
        except discord.HTTPException:
            role_failed = True
    else:
        role_failed = True

    # 3) Track for automatic removal when it expires.
    gs["active_restrictions"].append({
        "user_id": member.id,
        "role_id": role_id,
        "until": until_real,
        "reason": reason,
    })
    save_state()

    # 4) Announce in the punishment channel.
    channel = guild.get_channel(PUNISHMENT_CHANNEL_ID)
    if channel is not None:
        embed, _capped = build_restriction_embed(
            member, reason, accounts or [], offense_num,
            moderator=moderator, auto=auto,
        )
        try:
            await channel.send(content="@here", embed=embed,
                               allowed_mentions=discord.AllowedMentions(everyone=True, users=True))
        except discord.HTTPException:
            pass

    notes = []
    if timeout_failed:
        notes.append("couldn't time them out (check my permissions/role order)")
    if role_failed:
        notes.append("couldn't add the restriction role (missing role or permissions)")
    detail = "; ".join(notes) if notes else "applied"
    return (not timeout_failed and not role_failed), detail


async def remove_expired_restrictions():
    """Background sweep: lift restriction roles whose time has passed."""
    now = int(time.time())
    for gid, g in list(state.get("guilds", {}).items()):
        active = g.get("active_restrictions", [])
        if not active:
            continue
        guild = bot.get_guild(int(gid))
        still = []
        for rec in active:
            if rec.get("until", 0) > now:
                still.append(rec)
                continue
            # Expired -> remove the role if we can.
            if guild is not None:
                role = guild.get_role(rec.get("role_id"))
                member = guild.get_member(rec.get("user_id"))
                if member is None:
                    try:
                        member = await guild.fetch_member(rec.get("user_id"))
                    except discord.HTTPException:
                        member = None
                if role is not None and member is not None:
                    try:
                        await member.remove_roles(role, reason="Restriction expired")
                    except discord.HTTPException:
                        pass
        if len(still) != len(active):
            g["active_restrictions"] = still
            save_state()


def cooldown_remaining(gs, user_id):
    ts = gs.get("cooldowns", {}).get(str(user_id))
    if not ts:
        return 0, 0
    expiry = int(ts) + TEST_COOLDOWN
    remaining = expiry - int(time.time())
    if remaining <= 0:
        return 0, expiry
    return remaining, expiry


def expand_tier(code: str) -> str:
    s = (code or "").strip()
    m = re.fullmatch(r"(HT|LT)\s*(\d+)", s.upper())
    if m:
        prefix = "High Tier" if m.group(1) == "HT" else "Low Tier"
        return f"{prefix} {m.group(2)}"
    return s


def normalize_rank_code(code: str) -> str:
    return (code or "").strip().upper().replace(" ", "")


async def assign_tier_role(guild, member, earned_code):
    code = normalize_rank_code(earned_code)
    target = discord.utils.find(lambda r: r.name.upper() == code, guild.roles)
    if target is None:
        return None, "no_role"
    pattern = re.compile(r"^(HT|LT)\d+$", re.IGNORECASE)
    to_remove = [r for r in member.roles if pattern.match(r.name) and r.id != target.id]
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="Tier updated")
        await member.add_roles(target, reason=f"Earned tier {code}")
        return target, "ok"
    except discord.Forbidden:
        return target, "forbidden"
    except discord.HTTPException:
        return target, "error"


def lookup_player(identifier):
    players = state.get("players", {})
    ident = (identifier or "").strip()
    hex_id = ident.replace("-", "").lower()
    if len(hex_id) == 32 and all(c in "0123456789abcdef" for c in hex_id):
        return players.get(hex_id)
    low = ident.lower()
    rec = players.get("name:" + low)
    if rec is not None:
        return rec
    for r in players.values():
        if (r.get("username") or "").lower() == low:
            return r
    return None


def _store_player(uuid, canonical, tier, region):
    players = state.setdefault("players", {})
    rec = {
        "username": canonical,
        "uuid": uuid,
        "tier": normalize_rank_code(tier),
        "region": region,
        "updated": int(time.time()),
    }
    if uuid:
        players.pop("name:" + canonical.lower(), None)
        players[uuid] = rec
    else:
        players["name:" + canonical.lower()] = rec
    return rec


async def resolve_mojang(username):
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
            async with session.get(url) as r:
                if r.status == 200:
                    data = await r.json()
                    return (data.get("id") or "").lower(), (data.get("name") or username)
    except Exception:
        pass
    return None, username


async def record_player_tier(username, tier, region):
    uuid, canonical = await resolve_mojang(username)
    rec = _store_player(uuid, canonical, tier, region)
    save_state()
    return rec


def parse_tier_text(text):
    t = (text or "").strip()
    m = re.fullmatch(r"(HT|LT)\s*(\d+)", t.upper())
    if m:
        return m.group(1) + m.group(2)
    m = re.match(r"(high|low)\s*tier\s*(\d+)", t, re.IGNORECASE)
    if m:
        return ("HT" if m.group(1).lower() == "high" else "LT") + m.group(2)
    return None


def parse_result_embed(embed):
    mc = None
    tier = None
    for f in embed.fields:
        name = (f.name or "").lower().replace(":", "").strip()
        val = (f.value or "").strip()
        if mc is None and ("username" in name or name == "ign" or "minecraft" in name):
            mc = val
        if tier is None and "earned" in name:
            tier = parse_tier_text(val)
    if tier is None:
        for f in embed.fields:
            name = (f.name or "").lower()
            if "previous" in name:
                continue
            if "tier" in name or "rank" in name:
                c = parse_tier_text(f.value or "")
                if c:
                    tier = c
                    break
    if mc is None and embed.title:
        m = re.match(r"(.+?)'s Test Results", embed.title)
        if m:
            mc = m.group(1).strip()
    if mc and tier:
        return mc, tier
    return None


async def _api_root(request):
    return web.json_response({
        "service": "Fury Tiers API",
        "tierlist": "[1.21+] Fury Tiers [NA]",
        "players": len(state.get("players", {})),
    })


async def _api_tier(request):
    rec = lookup_player(request.match_info["identifier"])
    if rec is None:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({
        "username": rec.get("username"),
        "uuid": rec.get("uuid"),
        "tier": rec.get("tier"),
        "region": rec.get("region"),
        "updated": rec.get("updated"),
    })


async def _api_all(request):
    return web.json_response(list(state.get("players", {}).values()))


async def start_api():
    app = web.Application()
    app.router.add_get("/", _api_root)
    app.router.add_get("/tier/{identifier}", _api_tier)
    app.router.add_get("/all", _api_all)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, API_HOST, API_PORT)
    await site.start()
    print(f"Tier API listening on http://{API_HOST}:{API_PORT}  (try /tier/<username-or-uuid>)")
    return runner


def next_channel_number(category, prefix) -> int:
    nums = [0]
    if category is not None:
        for ch in category.channels:
            if ch.name.startswith(prefix):
                suffix = ch.name[len(prefix):]
                if suffix.isdigit():
                    nums.append(int(suffix))
    return max(nums) + 1


def build_queue_embed(gs, guild) -> discord.Embed:
    kit = gs.get("kit", DEFAULT_KIT)
    embed = discord.Embed(
        title=f"Tester(s) Available!  [{REGION} - {DEFAULT_KIT}]",
        description="🧪 The queue updates automatically. Use `/leave` to remove yourself from the queue.",
        color=QUEUE_COLOR,
    )

    queue = gs["queue"]
    if queue:
        lines = []
        for i, e in enumerate(queue, 1):
            m = guild.get_member(e["user_id"])
            mention = m.mention if m else f"<@{e['user_id']}>"
            lines.append(f"**{i}.** {mention}")
        queue_text = "\n".join(lines)
    else:
        queue_text = "*The queue is empty.*"
    embed.add_field(name=f"Queue ({len(queue)}/{MAX_QUEUE}):", value=queue_text, inline=False)

    testers = gs["testers"]
    if testers:
        t_lines = []
        for tid in testers:
            m = guild.get_member(tid)
            t_lines.append(m.mention if m else f"<@{tid}>")
        tester_text = "\n".join(t_lines)
    else:
        tester_text = "*None*"
    embed.add_field(name="Active Tester(s):", value=tester_text, inline=False)

    return embed


def build_closed_embed(gs) -> discord.Embed:
    embed = discord.Embed(
        title="No Testers Online",
        description=(
            "No testers for your region are available at this time. "
            "You will be pinged when a tester is available. Check back later!"
        ),
        color=CLOSED_COLOR,
    )
    last = gs.get("last_test")
    if last:
        value = f"<t:{int(last)}:f>  ( <t:{int(last)}:R> )"
    else:
        value = "No tests recorded yet."
    embed.add_field(name="Last Test At:", value=value, inline=False)
    return embed


def build_results_embed(tester, player_mention, mc, region, previous_rank, earned) -> discord.Embed:
    embed = discord.Embed(title=f"{mc}'s Test Results 🏆", color=RESULTS_COLOR)
    embed.add_field(name="Player:", value=player_mention, inline=False)
    embed.add_field(name="Tester:", value=tester.mention, inline=False)
    embed.add_field(name="Region:", value=region, inline=False)
    embed.add_field(name="Username:", value=mc, inline=False)
    embed.add_field(name="Previous Rank:", value=previous_rank, inline=False)
    embed.add_field(name="Rank Earned:", value=expand_tier(earned), inline=False)
    embed.set_thumbnail(url=f"https://mc-heads.net/body/{mc}")
    return embed


async def refresh_queue_message(guild):
    gs = get_guild_state(guild.id)
    channel = guild.get_channel(REQUEST_CHANNEL_ID)
    if channel is None:
        return
    embed = build_queue_embed(gs, guild)
    mid = gs.get("queue_message_id")
    if mid:
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(embed=embed, view=JoinQueueView())
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
    msg = await channel.send(embed=embed, view=JoinQueueView())
    gs["queue_message_id"] = msg.id
    save_state()


# ------------------------------- UI Views --------------------------------
class JoinModal(discord.ui.Modal, title="Join the Test Queue"):
    mc = discord.ui.TextInput(
        label="Minecraft Username",
        placeholder="e.g. Notch",
        required=True,
        max_length=16,
    )
    mc_confirm = discord.ui.TextInput(
        label="Reconfirm Username",
        placeholder="Type your username again, exactly",
        required=True,
        max_length=16,
    )
    server = discord.ui.TextInput(
        label="Preferred Server",
        placeholder="e.g. PVPClub",
        required=True,
        max_length=40,
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Both username fields must match exactly (case-insensitive).
        if str(self.mc).strip().lower() != str(self.mc_confirm).strip().lower():
            await interaction.response.send_message(
                "❌ The two usernames didn't match. Please open the queue and try again, "
                "typing your Minecraft username the same way both times.",
                ephemeral=True,
            )
            return
        gs = get_guild_state(interaction.guild_id)
        if not gs.get("queue_message_id"):
            await interaction.response.send_message("There's no open queue right now.", ephemeral=True)
            return
        cd_remaining, cd_expiry = cooldown_remaining(gs, interaction.user.id)
        if cd_remaining > 0:
            await interaction.response.send_message(
                f"⏳ You've already been tested recently. You can join the queue again <t:{cd_expiry}:R>.",
                ephemeral=True,
            )
            return
        if any(e["user_id"] == interaction.user.id for e in gs["queue"]):
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        if len(gs["queue"]) >= MAX_QUEUE:
            await interaction.response.send_message("The queue is full — try again later.", ephemeral=True)
            return

        gs["queue"].append({
            "user_id": interaction.user.id,
            "mc": str(self.mc),
            "server": str(self.server),
            "display": interaction.user.display_name,
            "joined": int(time.time()),
        })
        save_state()
        await refresh_queue_message(interaction.guild)
        await interaction.response.send_message(
            f"✅ You joined the queue as **{self.mc}** (position {len(gs['queue'])}).",
            ephemeral=True,
        )


class JoinQueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join Queue", style=discord.ButtonStyle.primary, custom_id="furytiers:join_queue")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        gs = get_guild_state(interaction.guild_id)
        if not gs.get("queue_message_id"):
            await interaction.response.send_message("There's no open queue right now.", ephemeral=True)
            return
        cd_remaining, cd_expiry = cooldown_remaining(gs, interaction.user.id)
        if cd_remaining > 0:
            await interaction.response.send_message(
                f"⏳ You've already been tested recently. You can join the queue again <t:{cd_expiry}:R>.",
                ephemeral=True,
            )
            return
        if any(e["user_id"] == interaction.user.id for e in gs["queue"]):
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        if len(gs["queue"]) >= MAX_QUEUE:
            await interaction.response.send_message("The queue is full — try again later.", ephemeral=True)
            return
        await interaction.response.send_modal(JoinModal())


class CloseModal(discord.ui.Modal, title="Submit Test Result"):
    earned = discord.ui.TextInput(
        label="Rank Earned",
        placeholder="e.g. LT5, HT3",
        required=True,
        max_length=20,
    )
    previous = discord.ui.TextInput(
        label="Previous Rank",
        default="Unranked",
        required=True,
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction):
        gs = get_guild_state(interaction.guild_id)
        ticket = gs["tickets"].get(str(interaction.channel.id))
        mc = ticket["mc"] if ticket else "Unknown"

        results_channel = interaction.guild.get_channel(RESULTS_CHANNEL_ID)
        player_mention = f"<@{ticket['player_id']}>" if ticket and ticket.get("player_id") is not None else "Unknown"
        if results_channel is not None:
            embed = build_results_embed(
                interaction.user, player_mention, mc, REGION, str(self.previous), str(self.earned)
            )
            await results_channel.send(embed=embed)

        gs["last_test"] = int(time.time())
        if ticket and ticket.get("player_id") is not None:
            gs["cooldowns"][str(ticket["player_id"])] = int(time.time())
        gs["tickets"].pop(str(interaction.channel.id), None)
        save_state()

        player_id = ticket["player_id"] if ticket and ticket.get("player_id") is not None else None
        target_member = None
        if player_id is not None:
            target_member = interaction.guild.get_member(player_id)
            if target_member is None:
                try:
                    target_member = await interaction.guild.fetch_member(player_id)
                except discord.HTTPException:
                    target_member = None

        role_note = ""
        if target_member is not None:
            role, status = await assign_tier_role(interaction.guild, target_member, str(self.earned))
            if status == "ok":
                role_note = f" {role.mention} role given."
            elif status == "no_role":
                role_note = f" (No role named **{normalize_rank_code(str(self.earned))}** found — assign it manually.)"
            elif status == "forbidden":
                role_note = " (Couldn't assign the role — check my permissions and that my role is above the tier roles.)"
            else:
                role_note = " (Role assignment failed.)"

        # Record the tier (and grab the resolved UUID for alting detection).
        mc_uuid = None
        if mc and mc != "Unknown":
            try:
                rec = await record_player_tier(mc, str(self.earned), REGION)
                mc_uuid = (rec or {}).get("uuid")
            except Exception:
                rec = None

        # --- Alting detection -------------------------------------------------
        # If this Discord user has previously been tested on a DIFFERENT
        # Minecraft account, auto-restrict them for alting.
        alt_note = ""
        if player_id is not None and mc_uuid:
            accounts = gs["user_accounts"].setdefault(str(player_id), [])
            prior_other = [u for u in accounts if u and u != mc_uuid]
            if prior_other and target_member is not None:
                # Show every account involved: the prior one(s) they alted from,
                # plus the account used in this test.
                accounts = [(uuid_to_name(puid), puid) for puid in prior_other]
                accounts.append((mc, mc_uuid))
                ok, detail = await apply_restriction(
                    interaction.guild, target_member, "Alting",
                    accounts=accounts, auto=True,
                )
                alt_note = (" ⚠️ This user was tested on a different account before — "
                            "auto-restricted for **Alting** (30 days).")
            if mc_uuid not in accounts:
                accounts.append(mc_uuid)
            save_state()
        # ----------------------------------------------------------------------

        await interaction.response.send_message(
            f"✅ Result submitted: **{expand_tier(str(self.earned))}**.{role_note}{alt_note} "
            f"Closing this channel in {CLOSE_DELAY} seconds…"
        )
        await asyncio.sleep(CLOSE_DELAY)
        try:
            await interaction.channel.delete(reason="Tier test closed")
        except discord.HTTPException:
            pass


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, custom_id="furytiers:ticket_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not member_is_tester(interaction.user):
            await interaction.response.send_message("Only testers can do this.", ephemeral=True)
            return
        gs = get_guild_state(interaction.guild_id)
        gs["tickets"].pop(str(interaction.channel.id), None)
        save_state()
        await interaction.response.send_message(
            f"⏭️ Test skipped by {interaction.user.mention}. Closing in {SKIP_DELAY} seconds…"
        )
        await asyncio.sleep(SKIP_DELAY)
        try:
            await interaction.channel.delete(reason="Tier test skipped")
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="furytiers:ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not member_is_tester(interaction.user):
            await interaction.response.send_message("Only testers can do this.", ephemeral=True)
            return
        await interaction.response.send_modal(CloseModal())


# --------------------------------- Bot -----------------------------------
class TierBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(JoinQueueView())
        self.add_view(TicketView())
        try:
            await start_api()
        except Exception as e:
            print(f"WARNING: tier API failed to start ({e}). The bot will still run.")
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            # Remove any stale GLOBAL commands left from an earlier run when GUILD_ID
            # was not set. That leftover global copy is what shows each command twice.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()


intents = discord.Intents.default()
intents.members = True

bot = TierBot(command_prefix="!", intents=intents)
tree = bot.tree


async def auto_backfill_from_results(limit: int = 2000):
    """Rebuild player tiers from the #results channel.

    Render's free plan wipes the disk on restart, so data.json (and the saved
    tiers) is lost every time the service restarts. The results channel is a
    permanent record though, so on startup we replay it to restore every tier
    automatically. No database or paid disk needed.
    """
    # Only worth doing if we actually lost the tier data.
    if state.get("players"):
        print(f"Tier data present ({len(state['players'])} players) - skipping auto-backfill.")
        return
    if not GUILD_ID:
        print("Auto-backfill skipped: GUILD_ID not set.")
        return
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        print("Auto-backfill skipped: bot can't see the guild yet.")
        return
    results_channel = guild.get_channel(RESULTS_CHANNEL_ID)
    if results_channel is None:
        print("Auto-backfill skipped: results channel not found.")
        return

    print("No saved tiers found - auto-backfilling from #results ...")
    state.setdefault("players", {})
    seen = set()
    added = 0
    try:
        async for message in results_channel.history(limit=limit):
            for embed in message.embeds:
                parsed = parse_result_embed(embed)
                if parsed is None:
                    continue
                mc, tier = parsed
                if mc.lower() in seen:
                    continue
                uuid, canonical = await resolve_mojang(mc)
                dedupe_key = uuid if uuid else "name:" + canonical.lower()
                if dedupe_key in seen:
                    seen.add(mc.lower())
                    continue
                seen.add(dedupe_key)
                seen.add(mc.lower())
                seen.add(canonical.lower())
                _store_player(uuid, canonical, tier, REGION)
                added += 1
                await asyncio.sleep(0.12)
    except discord.Forbidden:
        print("Auto-backfill failed: missing permission to read results history.")
        return
    except Exception as e:
        print(f"Auto-backfill error: {e}")
        return
    save_state()
    print(f"Auto-backfill complete: restored {added} players from #results.")


@bot.event
async def on_ready():
    try:
        await bot.change_presence(activity=discord.Game("[1.21+] Fury Tiers [NA]"))
    except Exception:
        pass
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print(f"Test cooldown: {TEST_COOLDOWN // 3600}h after a test | Command sync: {'guild (instant)' if GUILD_ID else 'global (slow)'}")
    # Restore tiers if the disk was wiped (Render free tier resets on restart).
    try:
        await auto_backfill_from_results()
    except Exception as e:
        print(f"Auto-backfill outer error: {e}")
    # Start the background sweep that lifts expired restriction roles.
    global _restriction_sweeper_started
    if not _restriction_sweeper_started:
        _restriction_sweeper_started = True
        bot.loop.create_task(restriction_sweeper_loop())


_restriction_sweeper_started = False


async def restriction_sweeper_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await remove_expired_restrictions()
        except Exception as e:
            print(f"Restriction sweeper error: {e}")
        await asyncio.sleep(600)  # check every 10 minutes


def guild_only(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None


# ------------------------------ Commands ---------------------------------
# Tester-only commands use @app_commands.default_permissions() so they are hidden
# from regular members. Grant the [TierTester] role access to them in
# Server Settings -> Integrations. /leave has no restriction and works in any
# channel for everyone.
@tree.command(name="queue", description="Open a tester queue, or join an open one as an active tester.")
@app_commands.default_permissions()
async def queue_cmd(interaction: discord.Interaction):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_tester(interaction.user):
        await interaction.response.send_message("You need the **[TierTester]** role to use this.", ephemeral=True)
        return

    gs = get_guild_state(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    if gs.get("queue_message_id"):
        if interaction.user.id in gs["testers"]:
            await interaction.followup.send("You're already an active tester on this queue.", ephemeral=True)
            return
        gs["testers"].append(interaction.user.id)
        save_state()
        await refresh_queue_message(interaction.guild)
        await interaction.followup.send("✅ You joined as an active tester.", ephemeral=True)
        return

    # Open a fresh queue.
    gs["queue"] = []
    gs["testers"] = [interaction.user.id]
    gs["queue_message_id"] = None

    request_channel = interaction.guild.get_channel(REQUEST_CHANNEL_ID)
    old_closed = gs.get("closed_message_id")
    if old_closed and request_channel is not None:
        try:
            m = await request_channel.fetch_message(old_closed)
            await m.delete()
        except discord.HTTPException:
            pass
    gs["closed_message_id"] = None
    save_state()

    await refresh_queue_message(interaction.guild)
    await interaction.followup.send(f"✅ Queue opened for **{REGION} - {DEFAULT_KIT}**.", ephemeral=True)


async def queue_autocomplete(interaction: discord.Interaction, current: str):
    gs = get_guild_state(interaction.guild_id)
    guild = interaction.guild
    choices = []
    for i, e in enumerate(gs["queue"], 1):
        member = guild.get_member(e["user_id"]) if guild else None
        display = e.get("display") or (member.display_name if member else f"User {e['user_id']}")
        label = f"{i}. {display}"
        if current.lower() in label.lower() or current.lower() in e["mc"].lower():
            choices.append(app_commands.Choice(name=label[:100], value=str(e["user_id"])))
    return choices[:25]


@tree.command(name="pull", description="Pull someone from the queue into a private test channel.")
@app_commands.default_permissions()
@app_commands.describe(user="Person in the queue to pull (pick from the list). Leave blank for the next person.")
@app_commands.autocomplete(user=queue_autocomplete)
async def pull_cmd(interaction: discord.Interaction, user: str = None):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_tester(interaction.user):
        await interaction.response.send_message("You need the **[TierTester]** role to use this.", ephemeral=True)
        return

    gs = get_guild_state(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    if not gs["queue"]:
        await interaction.followup.send("The queue is empty.", ephemeral=True)
        return

    if user:
        entry = None
        id_match = re.search(r"\d{15,25}", user)
        if id_match:
            uid = int(id_match.group())
            entry = next((e for e in gs["queue"] if e["user_id"] == uid), None)
        if entry is None:
            needle = user.lower()
            entry = next(
                (e for e in gs["queue"]
                 if needle in (e.get("display") or "").lower() or needle in e["mc"].lower()),
                None,
            )
        if entry is None:
            await interaction.followup.send(f"Couldn't find **{user}** in the queue.", ephemeral=True)
            return
    else:
        entry = gs["queue"][0]

    guild = interaction.guild
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send("The configured ticket category was not found.", ephemeral=True)
        return

    try:
        player = guild.get_member(entry["user_id"]) or await guild.fetch_member(entry["user_id"])
    except discord.HTTPException:
        player = None
    if player is None:
        await interaction.followup.send("That member is no longer in the server. Removing them from the queue.", ephemeral=True)
        gs["queue"] = [e for e in gs["queue"] if e["user_id"] != entry["user_id"]]
        save_state()
        await refresh_queue_message(guild)
        return

    tester_role = guild.get_role(TIER_TESTER_ROLE_ID)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True, manage_messages=True, embed_links=True,
        ),
        player: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
        ),
    }
    if tester_role is not None:
        overwrites[tester_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
        )

    number = next_channel_number(category, "test-")
    try:
        channel = await category.create_text_channel(
            name=f"test-{number}", overwrites=overwrites, reason=f"Tier test for {entry['mc']}"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't create the channel — I need **Manage Channels** and **Manage Roles** permissions.",
            ephemeral=True,
        )
        return

    # Remove from queue and refresh the public queue message.
    gs["queue"] = [e for e in gs["queue"] if e["user_id"] != entry["user_id"]]
    gs["tickets"][str(channel.id)] = {
        "player_id": entry["user_id"],
        "mc": entry["mc"],
        "server": entry["server"],
        "kind": "test",
        "provisional": None,
    }
    save_state()
    await refresh_queue_message(guild)

    ticket_embed = discord.Embed(
        title="🎫 Tier Test",
        description=f"Welcome {player.mention}! A tester will be with you shortly. Good luck!",
        color=TICKET_COLOR,
    )
    ticket_embed.add_field(name="Minecraft Username", value=entry["mc"], inline=True)
    ticket_embed.add_field(name="Preferred Server", value=entry["server"], inline=True)
    ticket_embed.add_field(name="Region", value=REGION, inline=True)
    ticket_embed.set_footer(text="Testers: Close = submit result • Skip = cancel • /passeval = high tier")
    await channel.send(content=player.mention, embed=ticket_embed, view=TicketView())

    await interaction.followup.send(f"✅ Pulled **{entry['mc']}** into {channel.mention}.", ephemeral=True)


@tree.command(name="leave", description="Remove yourself from the test queue.")
async def leave_cmd(interaction: discord.Interaction):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    gs = get_guild_state(interaction.guild_id)
    before = len(gs["queue"])
    gs["queue"] = [e for e in gs["queue"] if e["user_id"] != interaction.user.id]
    if len(gs["queue"]) == before:
        await interaction.response.send_message("You're not in the queue.", ephemeral=True)
        return
    save_state()
    await interaction.response.send_message("✅ You left the queue.", ephemeral=True)
    await refresh_queue_message(interaction.guild)


@tree.command(name="closequeue", description="Close the queue and post the 'No Testers Online' message.")
@app_commands.default_permissions()
async def closequeue_cmd(interaction: discord.Interaction):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_tester(interaction.user):
        await interaction.response.send_message("You need the **[TierTester]** role to use this.", ephemeral=True)
        return

    gs = get_guild_state(interaction.guild_id)
    await interaction.response.defer(ephemeral=True)

    if not gs.get("queue_message_id"):
        await interaction.followup.send("There's no open queue to close.", ephemeral=True)
        return

    testers = gs.get("testers", [])
    if interaction.user.id in testers and len(testers) > 1:
        gs["testers"] = [t for t in testers if t != interaction.user.id]
        save_state()
        await refresh_queue_message(interaction.guild)
        await interaction.followup.send(
            "✅ You left the active testers. The queue stays open for the other tester(s).",
            ephemeral=True,
        )
        return

    request_channel = interaction.guild.get_channel(REQUEST_CHANNEL_ID)
    mid = gs.get("queue_message_id")
    if mid and request_channel is not None:
        try:
            m = await request_channel.fetch_message(mid)
            await m.delete()
        except discord.HTTPException:
            pass

    gs["queue_message_id"] = None
    gs["queue"] = []
    gs["testers"] = []

    if request_channel is not None:
        closed = await request_channel.send(embed=build_closed_embed(gs))
        gs["closed_message_id"] = closed.id
    save_state()

    await interaction.followup.send("✅ Queue closed.", ephemeral=True)


@tree.command(name="passeval", description="Pass the player to high tier (guaranteed LT3). Use inside a test channel.")
@app_commands.default_permissions()
async def passeval_cmd(interaction: discord.Interaction):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_tester(interaction.user):
        await interaction.response.send_message("You need the **[TierTester]** role to use this.", ephemeral=True)
        return

    gs = get_guild_state(interaction.guild_id)
    ticket = gs["tickets"].get(str(interaction.channel.id))
    if ticket is None:
        await interaction.response.send_message("Use this inside an active test channel.", ephemeral=True)
        return
    if ticket.get("kind") == "hightest":
        await interaction.response.send_message("This is already a high tier test.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    category = interaction.guild.get_channel(TICKET_CATEGORY_ID)
    number = next_channel_number(category, "hightest-")
    try:
        await interaction.channel.edit(name=f"hightest-{number}")
    except discord.HTTPException:
        pass

    ticket["kind"] = "hightest"
    ticket["provisional"] = "LT3"
    save_state()

    player = interaction.guild.get_member(ticket["player_id"])
    player_mention = player.mention if player else "the player"
    await interaction.channel.send(
        f"✅ {player_mention} **passed the evaluation** and is guaranteed at least **Low Tier 3**. "
        f"A high tier tester will now continue the test."
    )
    await interaction.followup.send(f"Converted this channel to **hightest-{number}**.", ephemeral=True)


@tree.command(name="resetcooldown", description="Clear a member's test cooldown so they can join the queue again.")
@app_commands.default_permissions()
@app_commands.describe(user="The member whose cooldown you want to clear.")
async def resetcooldown_cmd(interaction: discord.Interaction, user: discord.Member):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_owner(interaction.user):
        await interaction.response.send_message("Only the **owner** role can use this.", ephemeral=True)
        return
    gs = get_guild_state(interaction.guild_id)
    had = gs["cooldowns"].pop(str(user.id), None)
    save_state()
    if had is not None:
        await interaction.response.send_message(
            f"✅ Cleared the test cooldown for {user.mention}. They can join the queue again.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"{user.mention} doesn't have an active cooldown.", ephemeral=True
        )


@tree.command(name="settier", description="Manually set a player's tier in the tier API (backfill existing ranks).")
@app_commands.default_permissions()
@app_commands.describe(username="Minecraft username", tier="Tier code, e.g. LT5 or HT3")
async def settier_cmd(interaction: discord.Interaction, username: str, tier: str):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_owner(interaction.user):
        await interaction.response.send_message("Only the **owner** role can use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    rec = await record_player_tier(username, tier, REGION)
    uuid_note = f" (UUID {rec['uuid']})" if rec.get("uuid") else " (couldn't resolve a UUID — stored by name only)"
    await interaction.followup.send(
        f"✅ Set **{rec['username']}** to **{normalize_rank_code(tier)}** in the tier API.{uuid_note}",
        ephemeral=True,
    )


@tree.command(name="backfill", description="Scan the results channel and add all previously-tested players to the tier API.")
@app_commands.default_permissions()
@app_commands.describe(limit="How many recent messages to scan (default 1000, max 5000).")
async def backfill_cmd(interaction: discord.Interaction, limit: int = 1000):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_owner(interaction.user):
        await interaction.response.send_message("Only the **owner** role can use this.", ephemeral=True)
        return

    results_channel = interaction.guild.get_channel(RESULTS_CHANNEL_ID)
    if results_channel is None:
        await interaction.response.send_message("I can't find the results channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    limit = max(1, min(limit, 5000))

    players = state.setdefault("players", {})
    seen = set()
    added = 0
    unresolved = 0
    try:
        async for message in results_channel.history(limit=limit):
            for embed in message.embeds:
                parsed = parse_result_embed(embed)
                if parsed is None:
                    continue
                mc, tier = parsed
                if mc.lower() in seen:
                    continue
                uuid, canonical = await resolve_mojang(mc)
                dedupe_key = uuid if uuid else "name:" + canonical.lower()
                if dedupe_key in seen:
                    seen.add(mc.lower())
                    continue
                seen.add(dedupe_key)
                seen.add(mc.lower())
                seen.add(canonical.lower())
                _store_player(uuid, canonical, tier, REGION)
                added += 1
                if uuid is None:
                    unresolved += 1
                await asyncio.sleep(0.12)
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to read the results channel history.", ephemeral=True)
        return
    save_state()

    note = ""
    if unresolved:
        note = f" {unresolved} had no Mojang UUID (name typo or name-changed) and won't show in-game until fixed with /settier."
    await interaction.followup.send(
        f"✅ Backfill done. Added/updated **{added}** players from the last {limit} messages.{note}",
        ephemeral=True,
    )


@tree.command(name="removetier", description="Remove a Minecraft player's tier from the tier API.")
@app_commands.default_permissions()
@app_commands.describe(username="Minecraft username whose tier should be removed.")
async def removetier_cmd(interaction: discord.Interaction, username: str):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_owner(interaction.user):
        await interaction.response.send_message("Only the **owner** role can use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    uuid, canonical = await resolve_mojang(username)
    players = state.setdefault("players", {})
    names = {username.lower(), (canonical or "").lower()}
    to_remove = []
    if uuid and uuid in players:
        to_remove.append(uuid)
    for k, r in list(players.items()):
        if k.startswith("name:") and k[5:] in names:
            to_remove.append(k)
        elif (r.get("username") or "").lower() in names:
            to_remove.append(k)
    removed = 0
    for k in set(to_remove):
        if players.pop(k, None) is not None:
            removed += 1
    save_state()

    if removed:
        await interaction.followup.send(f"✅ Removed **{canonical or username}**'s tier from the API.", ephemeral=True)
    else:
        await interaction.followup.send(f"No tier found for **{username}**.", ephemeral=True)


@tree.command(name="restrict", description="Restrict a member (owner only): times them out, gives the role, posts to punishments.")
@app_commands.default_permissions()
@app_commands.describe(
    user="The member to restrict.",
    reason="What they're being restricted for.",
    minecraft="Their Minecraft username (optional, shown in the announcement).",
)
@app_commands.choices(reason=[
    app_commands.Choice(name="Cheating (7 days)", value="Cheating"),
    app_commands.Choice(name="Alting (30 days)", value="Alting"),
    app_commands.Choice(name="Threats (14 days)", value="Threats"),
    app_commands.Choice(name="Toxicity (7 days)", value="Toxicity"),
])
async def restrict_cmd(interaction: discord.Interaction, user: discord.Member,
                       reason: app_commands.Choice[str], minecraft: str = None):
    if not guild_only(interaction):
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    if not member_is_owner(interaction.user):
        await interaction.response.send_message("Only the **owner** role can use this.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    reason_value = reason.value
    info = RESTRICTIONS.get(reason_value, {})

    # Try to resolve a UUID for the announcement if a username was given.
    mc_uuid = None
    mc_name = minecraft
    if minecraft:
        uuid, canonical = await resolve_mojang(minecraft)
        mc_uuid = uuid
        mc_name = canonical or minecraft

    ok, detail = await apply_restriction(
        interaction.guild, user, reason_value,
        accounts=[(mc_name, mc_uuid)] if (mc_name or mc_uuid) else None,
        moderator=interaction.user, auto=False,
    )

    days = info.get("days", "?")
    if ok:
        await interaction.followup.send(
            f"✅ Restricted {user.mention} for **{reason_value}** ({days} days). "
            f"Role applied, timed out, and posted to the punishment channel.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"⚠️ Restriction for {user.mention} (**{reason_value}**) was recorded and announced, "
            f"but: {detail}. Check my role order and that I have Moderate Members + Manage Roles.",
            ephemeral=True,
        )


# --------------------------------- Run -----------------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "No token found. RESET your bot token in the Developer Portal, then set the "
            "BOT_TOKEN environment variable (or put BOT_TOKEN=... in a .env file)."
        )
    if not GUILD_ID:
        print("WARNING: GUILD_ID is 0 — falling back to global command sync (can take up to 1 hour).")

    try:
        bot.run(TOKEN)
    except discord.HTTPException as e:
        # 429 = Discord is temporarily blocking logins because the bot tried to
        # connect too many times (Render restarting the process in a loop). If we
        # exit immediately, Render restarts us right away and we hit the block
        # again, keeping it alive forever. Sleeping first spaces the attempts out
        # so the block can clear.
        if getattr(e, "status", None) == 429:
            wait_s = 900  # 15 minutes
            print(
                f"Discord is rate-limiting logins (429 Too Many Requests). This happens when the "
                f"bot restarts too often. Waiting {wait_s // 60} minutes before exiting so the "
                f"limit can clear — do NOT keep redeploying, that resets the timer."
            )
            try:
                time.sleep(wait_s)
            except KeyboardInterrupt:
                pass
        raise
