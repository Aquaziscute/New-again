"""Discord UI components: tickets, appeals, teams, matches, and invites."""

from __future__ import annotations

import asyncio
import io
from datetime import timezone
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

import discord

from .match_manager import Match, MatchManager
from .mod_data import load_mod, save_mod, add_record
from .team_manager import Team, TeamManager

if TYPE_CHECKING:
    from .bot import LeagueBot


# ── Role/channel helpers ──────────────────────────────────────────────────────

def _hex_to_colour(hex_code: str) -> discord.Colour:
    return discord.Colour(int(hex_code.lstrip("#"), 16))


async def _reply(interaction: discord.Interaction, message: str, **kwargs) -> None:
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(message, **kwargs)
    else:
        await interaction.response.send_message(message, **kwargs)


async def _safe_add_role(member: discord.Member, role: Optional[discord.Role], *, reason: str) -> Optional[str]:
    if not role:
        return None
    try:
        await member.add_roles(role, reason=reason)
    except discord.Forbidden:
        return f"Missing permissions to add the {role.name} role."
    except discord.HTTPException:
        return f"Discord rejected adding the {role.name} role."
    return None


async def _safe_remove_role(member: discord.Member, role: Optional[discord.Role], *, reason: str) -> Optional[str]:
    if not role:
        return None
    try:
        await member.remove_roles(role, reason=reason)
    except discord.Forbidden:
        return f"Missing permissions to remove the {role.name} role."
    except discord.HTTPException:
        return f"Discord rejected removing the {role.name} role."
    return None


async def _safe_delete_role(role: Optional[discord.Role], *, reason: str) -> Optional[str]:
    if not role:
        return None
    try:
        await role.delete(reason=reason)
    except discord.Forbidden:
        return f"Missing permissions to delete the {role.name} role."
    except discord.HTTPException:
        return f"Discord rejected deleting the {role.name} role."
    return None


# ── Team embed ────────────────────────────────────────────────────────────────

def build_team_embed(team: Team, guild: discord.Guild) -> discord.Embed:
    role = guild.get_role(team.role_id)
    embed = discord.Embed(title=f"{team.name} roster", colour=_hex_to_colour(team.hex_color))
    icon = (role.display_icon.url if role and role.display_icon else None) or team.icon_url
    if icon:
        embed.set_thumbnail(url=icon)
    captain = guild.get_member(team.captain_id)
    lines = [f"👑 {captain.display_name if captain else f'<@{team.captain_id}>'}"]
    for uid in team.members:
        if uid == team.captain_id:
            continue
        m = guild.get_member(uid)
        name = m.display_name if m else f"<@{uid}>"
        prefix = "👑👑 " if uid in team.co_captains else ""
        lines.append(f"{prefix}{name}")
    embed.add_field(name="Members", value="\n".join(lines) or "No members yet.", inline=False)
    if team.invites:
        embed.add_field(name="Pending invites", value=", ".join(f"<@{uid}>" for uid in team.invites), inline=False)
    return embed


# ── Ticket helpers ────────────────────────────────────────────────────────────

async def send_transcript(bot: "LeagueBot", channel: discord.TextChannel, closer: str, reason: Optional[str] = None) -> None:
    transcript_channel_id = bot.config.transcript_channel_id
    if not transcript_channel_id:
        return
    tc = bot.get_channel(transcript_channel_id)
    if not tc:
        return
    lines = [f"Ticket Transcript: {channel.name}\n{'=' * 50}\n"]
    async for msg in channel.history(limit=None, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{ts}] {msg.author}: {msg.content or '[No text]'}")
        for a in msg.attachments:
            lines.append(f"  [Attachment: {a.url}]")
    from datetime import datetime
    buf = io.BytesIO("\n".join(lines).encode())
    embed = discord.Embed(
        title=f"Transcript: {channel.name}",
        description=f"Closed by: {closer}\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        color=discord.Color.red(),
    )
    if reason:
        embed.add_field(name="Reason", value=reason)
    await tc.send(embed=embed, file=discord.File(buf, f"transcript-{channel.name}.txt"))


# =============================================================================
#  TICKET VIEWS
# =============================================================================

TICKET_PANEL_DESCRIPTION = (
    "**General Support:**\n"
    "- Reporting a community member\n- Contacting staff\n- Questions\n"
    "- Appealing a punishment\n- Affiliate support\n\n"
    "**Ranked Support:**\n"
    "- Bot issues (syncing, joining codes, checking rank)\n"
    "- Request ReSync [MUST PROVIDE REASON]\n"
    "- Incorrect MMR gain\n- Ranked blacklist appeal\n\n"
    "**Management Support:**\n"
    "- Reporting a staff member\n- Questions for management\n- Server issues\n"
)


class TicketSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Open a ticket below!",
            options=[
                discord.SelectOption(label="General Support",    emoji="🎫"),
                discord.SelectOption(label="Ranked Support",     emoji="🏆"),
                discord.SelectOption(label="Management Support", emoji="⚙️"),
            ],
            min_values=1, max_values=1, custom_id="ticket_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        mapping = {
            "General Support":    bot.config.general_support_category_id,
            "Ranked Support":     bot.config.ranked_support_category_id,
            "Management Support": bot.config.management_support_category_id,
        }
        await _create_ticket(interaction, self.values[0], mapping.get(self.values[0]))


class TicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        await send_transcript(bot, interaction.channel, interaction.user.name)
        await interaction.response.send_message("Closing ticket...", ephemeral=True)
        await interaction.channel.delete()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="✋", custom_id="claim_ticket")
    async def claim_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(title="Ticket Claimed", description=f"Handled by {interaction.user.mention}", color=discord.Color.green())
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("You claimed this ticket!", ephemeral=True)


class CloseRequestView(discord.ui.View):
    def __init__(self, owner_id: Optional[int] = None) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id

    async def _check_owner(self, interaction: discord.Interaction) -> bool:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        if interaction.user.id != bot.ticket_owners.get(interaction.channel.id):
            await interaction.response.send_message("Only the ticket owner can respond!", ephemeral=True)
            return False
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success, emoji="✅", custom_id="close_yes")
    async def close_yes(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        await send_transcript(bot, interaction.channel, interaction.user.name, "Accepted close request")
        await interaction.response.send_message("Closing ticket...")
        await interaction.channel.delete()

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger, emoji="❌", custom_id="close_no")
    async def close_no(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self._check_owner(interaction):
            return
        embed = discord.Embed(title="Close Request Denied", description=f"{interaction.user.mention} denied the close request.", color=discord.Color.red())
        await interaction.channel.send(embed=embed)
        await interaction.response.send_message("Close request denied!", ephemeral=True)


async def _create_ticket(interaction: discord.Interaction, ticket_type: str, category_id: Optional[int]) -> None:
    from .bot import LeagueBot
    bot: LeagueBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    category = discord.utils.get(guild.categories, id=category_id) if category_id else None
    channel_name = f"{ticket_type.lower().replace(' ', '-')}-{interaction.user.name}"
    staff_role  = guild.get_role(bot.config.staff_role_ids[0]) if bot.config.staff_role_ids else None
    ranked_role = guild.get_role(bot.config.ranked_role_id) if bot.config.ranked_role_id else None
    mgmt_role   = guild.get_role(bot.config.management_role_id) if bot.config.management_role_id else None
    rw = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    no = discord.PermissionOverwrite(read_messages=False)
    overwrites: dict = {guild.default_role: no, interaction.user: rw, guild.me: rw}
    if category_id == bot.config.general_support_category_id:
        for role in (staff_role, ranked_role, mgmt_role):
            if role: overwrites[role] = rw
    elif category_id == bot.config.ranked_support_category_id:
        for role in (ranked_role, mgmt_role):
            if role: overwrites[role] = rw
    elif category_id == bot.config.management_support_category_id:
        if mgmt_role: overwrites[mgmt_role] = rw
    channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
    bot.ticket_owners[channel.id] = interaction.user.id
    age_years = (discord.utils.utcnow() - interaction.user.created_at).days // 365
    embed = discord.Embed(title=ticket_type, description="Thank you for opening a ticket! Staff will assist you shortly.", color=discord.Color.red())
    embed.add_field(name="Please provide:", value="-> Evidence if needed\n-> Description of your issue\n-> Username if reporting", inline=False)
    embed.add_field(name="Please don't:", value="-> Spam ping staff\n-> Disrespect support agents\n-> Waste our time", inline=False)
    embed.add_field(name="User", value=f"{interaction.user.mention} — account {age_years}y old", inline=False)
    await channel.send(content=staff_role.mention if staff_role else "", embed=embed, view=TicketControlView())
    await interaction.followup.send(f"Ticket created: {channel.mention}", ephemeral=True)


# =============================================================================
#  APPEAL VIEWS
# =============================================================================

class AppealModal(discord.ui.Modal, title="Ban Appeal"):
    date_and_reason = discord.ui.TextInput(label="Date of ban & reason",                  style=discord.TextStyle.short)
    explanation     = discord.ui.TextInput(label="Explanation of incident",                style=discord.TextStyle.paragraph)
    appeal_reason   = discord.ui.TextInput(label="Reason for appeal / changes since ban", style=discord.TextStyle.paragraph)
    commitments     = discord.ui.TextInput(label="Commitments to future behavior",        style=discord.TextStyle.paragraph)
    extra           = discord.ui.TextInput(label="Additional comments?",                   style=discord.TextStyle.paragraph, required=False)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        try:
            data = load_mod()
            uid = str(interaction.user.id)
            now = discord.utils.utcnow().replace(tzinfo=None)
            appeals = data.setdefault("appeals", {})
            if uid in appeals:
                existing = appeals[uid]
                if existing.get("status") == "pending":
                    await interaction.response.send_message("You already have a **pending** appeal.", ephemeral=True)
                    return
                if existing.get("status") in ("accepted", "denied") and "submitted_at" in existing:
                    import datetime
                    diff = now - datetime.datetime.fromisoformat(existing["submitted_at"])
                    if diff.days < 90:
                        await interaction.response.send_message(f"You can appeal again in **{90 - diff.days} day(s)**.", ephemeral=True)
                        return
            appeals[uid] = {
                "user": str(interaction.user), "user_id": interaction.user.id,
                "date_and_reason": self.date_and_reason.value, "explanation": self.explanation.value,
                "appeal_reason": self.appeal_reason.value, "commitments": self.commitments.value,
                "extra": self.extra.value or "None", "submitted_at": now.isoformat(),
                "status": "pending", "appeal_guild_id": interaction.guild_id,
            }
            save_mod(data)
            ch = None
            if bot.config.appeal_channel_id:
                try:
                    ch = await bot.fetch_channel(bot.config.appeal_channel_id)
                except (discord.NotFound, discord.Forbidden):
                    ch = None
            if ch:
                embed = discord.Embed(title="New Ban Appeal", color=discord.Color.yellow())
                embed.set_thumbnail(url=interaction.user.display_avatar.url)
                embed.add_field(name="User",                           value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                embed.add_field(name="Date of ban & reason",           value=self.date_and_reason.value,   inline=False)
                embed.add_field(name="Explanation of incident",        value=self.explanation.value,        inline=False)
                embed.add_field(name="Reason for appeal / changes",    value=self.appeal_reason.value,      inline=False)
                embed.add_field(name="Commitments to future behavior", value=self.commitments.value,        inline=False)
                embed.add_field(name="Additional comments",            value=self.extra.value or "None",    inline=False)
                embed.set_footer(text="Accept or Deny.")
                await ch.send(embed=embed, view=AppealActionView(interaction.user.id))
            await interaction.response.send_message("Your appeal has been submitted! Staff will review it shortly.", ephemeral=True)
        except Exception as exc:
            try:
                await interaction.response.send_message(f"An error occurred: `{exc}`", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"An error occurred: `{exc}`", ephemeral=True)


class _AppealButton(discord.ui.Button):
    def __init__(self, *, label: str, style: discord.ButtonStyle, action: str, appellant_id: int) -> None:
        super().__init__(label=label, style=style, custom_id=f"appeal_{action}_{appellant_id}")
        self.action = action
        self.appellant_id = appellant_id

    async def callback(self, interaction: discord.Interaction) -> None:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        if not bot.is_staff(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        import datetime
        data = load_mod()
        uid_str = str(self.appellant_id)
        now = datetime.datetime.utcnow()
        if uid_str in data.get("appeals", {}):
            data["appeals"][uid_str].update({"status": self.action, "reviewed_by": str(interaction.user), "reviewed_at": now.isoformat()})
            save_mod(data)
        for item in self.view.children:
            item.disabled = True
        new_embed = interaction.message.embeds[0]
        if self.action == "accepted":
            new_embed.color = discord.Color.green()
            new_embed.set_footer(text=f"Accepted by {interaction.user} - {now.strftime('%Y-%m-%d %H:%M')} UTC")
            await interaction.message.edit(embed=new_embed, view=self.view)
            user = None
            try:
                user = await bot.fetch_user(self.appellant_id)
                await interaction.guild.unban(user, reason=f"Appeal accepted by {interaction.user}")
                add_record(self.appellant_id, "unban", "Appeal accepted", str(interaction.user), None)
            except (discord.NotFound, discord.HTTPException):
                pass
            if user:
                try:
                    dm = discord.Embed(title="Appeal Accepted", description=f"Your appeal has been **accepted**.\n\nYou may rejoin here: {bot.config.appeal_server_invite}", color=discord.Color.green())
                    await user.send(embed=dm)
                except (discord.NotFound, discord.Forbidden):
                    pass
            appeal_guild_id = data.get("appeals", {}).get(uid_str, {}).get("appeal_guild_id")
            if appeal_guild_id:
                ag = bot.get_guild(appeal_guild_id)
                if ag:
                    m = ag.get_member(self.appellant_id)
                    if m:
                        try: await m.kick(reason="Ban appeal accepted.")
                        except discord.Forbidden: pass
            mention = user.mention if user else f"<@{self.appellant_id}>"
            await interaction.response.send_message(f"Appeal accepted. {mention} has been unbanned.", ephemeral=True)
        else:
            new_embed.color = discord.Color.red()
            new_embed.set_footer(text=f"Denied by {interaction.user} - {now.strftime('%Y-%m-%d %H:%M')} UTC")
            await interaction.message.edit(embed=new_embed, view=self.view)
            next_ts = int((now + datetime.timedelta(days=90)).replace(tzinfo=timezone.utc).timestamp())
            try:
                user = await bot.fetch_user(self.appellant_id)
                dm = discord.Embed(title="Appeal Declined", description="Your appeal has been declined.", color=discord.Color.red())
                dm.add_field(name="Next Allowed Appeal", value=f"<t:{next_ts}:F>", inline=False)
                await user.send(embed=dm)
            except (discord.NotFound, discord.Forbidden):
                pass
            await interaction.response.send_message("Appeal denied. User has been notified.", ephemeral=True)


class AppealActionView(discord.ui.View):
    def __init__(self, appellant_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(_AppealButton(label="Accept", style=discord.ButtonStyle.success, action="accepted", appellant_id=appellant_id))
        self.add_item(_AppealButton(label="Deny",   style=discord.ButtonStyle.danger,  action="denied",   appellant_id=appellant_id))


# =============================================================================
#  INVITE VIEWS
# =============================================================================

class InviteDecisionView(discord.ui.View):
    def __init__(self, *, bot: "LeagueBot", manager: TeamManager, guild: discord.Guild, team_role_id: int, team_name: str, member_role_id: Optional[int]) -> None:
        super().__init__(timeout=7 * 24 * 60 * 60)
        self.bot = bot
        self.manager = manager
        self.guild = guild
        self.team_role_id = team_role_id
        self.team_name = team_name
        self.member_role_id = member_role_id

    def _get_team(self) -> Optional[Team]:
        return self.manager.get_team_by_role(self.team_role_id) or self.manager.get_team(self.team_name)

    async def _resolve_member(self, user_id: int) -> Optional[discord.Member]:
        m = self.guild.get_member(user_id)
        if m: return m
        try: return await self.guild.fetch_member(user_id)
        except discord.HTTPException: return None

    def _disable(self) -> None:
        for child in self.children: child.disabled = True

    async def _finalise(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(message)
        self._disable()
        if interaction.message:
            try: await interaction.message.edit(view=self)
            except discord.HTTPException: pass
        self.stop()

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        team = self._get_team()
        if not team: await self._finalise(interaction, "That team no longer exists."); return

        guild = self.bot.get_guild(self.guild.id)
        if not guild:
            await self._finalise(interaction, "Could not reach the server. Please try again.")
            return

        member = guild.get_member(interaction.user.id)
        if not member:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                member = None
        if not member: await self._finalise(interaction, "You need to be in the server to accept this invite."); return

        if self.manager.is_roster_full(team): await self._finalise(interaction, f"{team.name} already has the maximum of {self.manager.max_roster_size()} players."); return
        if member.id in team.members: self.manager.remove_invite(team, member.id); await self._finalise(interaction, "You're already on that roster."); return
        existing = self.manager.find_team_for_member(member.id)
        if existing and existing.name != team.name:
            await self._finalise(interaction, f"You're already on **{existing.name}**. Leave that team before joining another.")
            return

        notes: List[str] = []

        team_role   = guild.get_role(team.role_id)
        member_role = guild.get_role(self.member_role_id) if self.member_role_id else None

        if not team_role:
            notes.append(f"Could not find the {team.name} team role — please contact an admin.")
        if self.member_role_id and not member_role:
            notes.append("Could not find the team member role — please contact an admin.")

        msg = await _safe_add_role(member, team_role, reason="Accepted team invite")
        if msg: notes.append(msg)
        msg = await _safe_add_role(member, member_role, reason="Joined team roster")
        if msg: notes.append(msg)

        try: self.manager.add_member(team, member.id)
        except ValueError as exc: await self._finalise(interaction, str(exc)); return

        lines = [f"You joined {team.name}!"]
        if notes: lines.extend(notes)
        await self._finalise(interaction, "\n".join(lines))
        await self.bot.log_event(guild, f"{member.mention} has joined **{team.name}**")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        team = self._get_team()
        if team: self.manager.remove_invite(team, interaction.user.id)
        await self._finalise(interaction, f"Declined the invite to {self.team_name}.")


# =============================================================================
#  TEAM MANAGEMENT VIEWS
# =============================================================================

class ConfirmView(discord.ui.View):
    def __init__(self, *, timeout: Optional[float] = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.value: Optional[bool] = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = True; self.stop()
        await interaction.response.edit_message(content="Action confirmed.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.value = False; self.stop()
        await interaction.response.edit_message(content="Action cancelled.", view=None)


class InviteUserSelect(discord.ui.UserSelect):
    def __init__(self, callback: Callable[[discord.Interaction, discord.Member], Awaitable[None]]) -> None:
        super().__init__(placeholder="Select a player to invite", min_values=1, max_values=1)
        self._on_select = callback

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild: await interaction.response.send_message("Only usable in a server.", ephemeral=True); return
        selected = self.values[0]
        member: Optional[discord.Member] = None
        if isinstance(selected, discord.Member):
            member = selected
        else:
            uid = getattr(selected, "id", None)
            if uid:
                member = guild.get_member(uid)
                if not member:
                    try: member = await guild.fetch_member(uid)
                    except discord.HTTPException: pass
        if not member: await interaction.response.send_message("Member is not in this server.", ephemeral=True); return
        await self._on_select(interaction, member)


class MemberSelect(discord.ui.Select):
    def __init__(self, *, team: Team, guild: discord.Guild, on_select: Callable[[int], None]) -> None:
        options = []
        for uid in team.members:
            m = guild.get_member(uid)
            label = m.display_name if m else f"Member {uid}"
            if uid == team.captain_id: label = f"{label}"
            elif uid in team.co_captains: label = f"{label}"
            options.append(discord.SelectOption(label=label, value=str(uid)))
        super().__init__(placeholder="Select a member", options=options)
        self._on_select = on_select

    async def callback(self, interaction: discord.Interaction) -> None:
        self._on_select(int(self.values[0]))
        await interaction.response.defer()


class ManageTeamView(discord.ui.View):
    """Interactive roster management view for captains and admins."""

    def __init__(self, *, interaction: discord.Interaction, team: Team, manager: TeamManager, bot: "LeagueBot",
                 is_admin: bool, roster_locked: bool, can_invite: bool, allow_force_add: bool = False,
                 captain_role: Optional[discord.Role] = None, co_captain_role: Optional[discord.Role] = None,
                 member_role: Optional[discord.Role] = None) -> None:
        super().__init__(timeout=300)
        self.interaction = interaction
        self.team = team
        self.manager = manager
        self.bot = bot
        self.is_admin = is_admin
        self.roster_locked = roster_locked
        self.can_invite = can_invite
        self.allow_force_add = allow_force_add
        self.captain_role = captain_role
        self.co_captain_role = co_captain_role
        self.member_role = member_role
        self.selected_member: Optional[int] = None
        self.guild = interaction.guild

        user_id = interaction.user.id
        self.is_co_captain = user_id in team.co_captains
        self.is_captain_or_admin = is_admin or user_id == team.captain_id
        self.can_manage_roster = is_admin or user_id == team.captain_id or self.is_co_captain

        self.member_select = MemberSelect(team=team, guild=self.guild, on_select=self._on_member_selected)
        if self.member_select.options: self.add_item(self.member_select)

        self.invite_button = discord.ui.Button(label="Invite", style=discord.ButtonStyle.green)
        self.invite_button.callback = self._on_invite  # type: ignore[assignment]
        self.add_item(self.invite_button)

        self.cancel_invite_button = discord.ui.Button(label="Cancel Invite", style=discord.ButtonStyle.secondary)
        self.cancel_invite_button.callback = self._on_cancel_invite  # type: ignore[assignment]
        self.add_item(self.cancel_invite_button)

        if allow_force_add:
            self.force_add_button = discord.ui.Button(label="Add Player", style=discord.ButtonStyle.primary)
            self.force_add_button.callback = self._on_force_add  # type: ignore[assignment]
            self.add_item(self.force_add_button)

        self.disband_button = discord.ui.Button(label="Disband", style=discord.ButtonStyle.danger)
        self.disband_button.callback = self._on_disband  # type: ignore[assignment]
        self.disband_button.disabled = not self.is_captain_or_admin
        self.add_item(self.disband_button)

        self.transfer_button = discord.ui.Button(label="Transfer Captain", style=discord.ButtonStyle.blurple)
        self.transfer_button.callback = self._on_transfer  # type: ignore[assignment]
        self.transfer_button.disabled = not self.is_captain_or_admin
        self.add_item(self.transfer_button)

        self.kick_button = discord.ui.Button(label="Kick Member", style=discord.ButtonStyle.danger, disabled=True)
        self.kick_button.callback = self._on_kick  # type: ignore[assignment]
        self.add_item(self.kick_button)

        self.promote_button = discord.ui.Button(label="Promote to Co-Captain", style=discord.ButtonStyle.primary, disabled=True)
        self.promote_button.callback = self._on_promote  # type: ignore[assignment]
        self.add_item(self.promote_button)

        self._update_roster_actions()

    def _on_member_selected(self, member_id: int) -> None:
        self.selected_member = member_id
        is_captain = member_id == self.team.captain_id
        self.kick_button.disabled = not self.can_manage_roster or (is_captain and not self.is_admin)
        self.promote_button.disabled = not self.is_captain_or_admin or is_captain
        if not is_captain:
            self.promote_button.label = "Remove Co-Captain" if member_id in self.team.co_captains else "Promote to Co-Captain"
        asyncio.create_task(self._redraw())

    async def _redraw(self) -> None:
        self._rebuild_member_options()
        self._update_roster_actions()
        await self.interaction.edit_original_response(embed=build_team_embed(self.team, self.guild), view=self)

    def _rebuild_member_options(self) -> None:
        options = []
        for uid in self.team.members:
            m = self.guild.get_member(uid)
            label = m.display_name if m else f"Member {uid}"
            if uid == self.team.captain_id: label = f"{label}"
            elif uid in self.team.co_captains: label = f"{label}"
            options.append(discord.SelectOption(label=label, value=str(uid)))
        if options: self.member_select.options = options
        elif self.member_select in self.children: self.remove_item(self.member_select)

    def _update_roster_actions(self) -> None:
        full = self.manager.is_roster_full(self.team)
        can = self.can_invite and self.can_manage_roster and not full
        self.invite_button.disabled = not can
        self.invite_button.style = discord.ButtonStyle.green if can else discord.ButtonStyle.gray
        has_invites = bool(self.team.invites)
        self.cancel_invite_button.disabled = not (has_invites and self.can_manage_roster)
        self.cancel_invite_button.style = discord.ButtonStyle.secondary if (has_invites and self.can_manage_roster) else discord.ButtonStyle.gray
        if self.allow_force_add and hasattr(self, "force_add_button"):
            self.force_add_button.disabled = full

    async def _ensure_member(self, member_id: int) -> Optional[discord.Member]:
        m = self.guild.get_member(member_id)
        if not m:
            try: m = await self.guild.fetch_member(member_id)
            except discord.NotFound: pass
        return m

    async def _on_invite(self, interaction: discord.Interaction) -> None:
        if not self.can_invite:
            await _reply(interaction, "Rosters are locked." if self.roster_locked else "Inviting is currently disabled.")
            return
        if self.manager.is_roster_full(self.team):
            await _reply(interaction, f"Roster already has the maximum of {self.manager.max_roster_size()} players.")
            return
        view = discord.ui.View()

        async def handle(select_interaction: discord.Interaction, member: discord.Member) -> None:
            if member.id in self.team.members:
                await select_interaction.response.send_message("Already on the roster.", ephemeral=True); return
            if member.id in self.team.invites:
                await select_interaction.response.send_message("Already has a pending invite.", ephemeral=True); return
            if self.manager.is_roster_full(self.team):
                await select_interaction.response.send_message(
                    f"Roster already has the maximum of {self.manager.max_roster_size()} players.", ephemeral=True); return
            existing = self.manager.find_team_for_member(member.id)
            if existing and existing.name != self.team.name:
                await select_interaction.response.send_message(
                    f"{member.mention} is already on **{existing.name}**. They must leave that team first.",
                    ephemeral=True,
                )
                return
            self.manager.add_invite(self.team, member.id)
            inviter = self.guild.get_member(self.interaction.user.id) or self.interaction.user
            embed = discord.Embed(
                title=f"You've been invited to {self.team.name}",
                description=f"{getattr(inviter, 'mention', str(inviter))} invited you to join **{self.team.name}**.",
                colour=_hex_to_colour(self.team.hex_color),
            )
            invite_view = InviteDecisionView(
                bot=self.bot, manager=self.manager, guild=self.guild,
                team_role_id=self.team.role_id, team_name=self.team.name,
                member_role_id=self.member_role.id if self.member_role else None,
            )
            try:
                await select_interaction.response.defer(ephemeral=True, thinking=True)
                await member.send(embed=embed, view=invite_view)
            except discord.Forbidden:
                self.manager.remove_invite(self.team, member.id)
                await _reply(select_interaction, f"Couldn't DM {member.mention}. They may have DMs disabled.")
                return
            await _reply(select_interaction, f"Invite sent to {member.mention}.")
            await self._redraw()

        view.add_item(InviteUserSelect(handle))
        await interaction.response.send_message("Search for a player to invite:", view=view, ephemeral=True)

    async def _on_cancel_invite(self, interaction: discord.Interaction) -> None:
        if not self.team.invites: await _reply(interaction, "No pending invites to cancel."); return
        options = []
        for uid in self.team.invites:
            m = self.guild.get_member(uid)
            options.append(discord.SelectOption(label=m.display_name if m else f"User {uid}", value=str(uid)))
        view = discord.ui.View()

        class _CancelSelect(discord.ui.Select):
            def __init__(self_, opts):
                super().__init__(placeholder="Select invite to cancel", options=opts)
            async def callback(self_, inter: discord.Interaction) -> None:
                uid = int(self_.values[0])
                if not inter.response.is_done(): await inter.response.defer(ephemeral=True, thinking=True)
                self.manager.remove_invite(self.team, uid)
                m = await self._ensure_member(uid)
                if m:
                    try: await m.send(f"Your invite to **{self.team.name}** has been cancelled.")
                    except discord.Forbidden: pass
                await _reply(inter, f"Cancelled invite for {m.mention if m else f'<@{uid}>'}.")
                await self._redraw()

        view.add_item(_CancelSelect(options))
        await interaction.response.send_message("Choose an invite to cancel:", view=view, ephemeral=True)

    async def _on_force_add(self, interaction: discord.Interaction) -> None:
        if self.manager.is_roster_full(self.team):
            await _reply(interaction, f"Roster already has the maximum of {self.manager.max_roster_size()} players."); return
        view = discord.ui.View()

        async def handle(select_interaction: discord.Interaction, member: discord.Member) -> None:
            if member.id in self.team.members:
                await select_interaction.response.send_message("Already on this roster.", ephemeral=True); return
            existing = self.manager.find_team_for_member(member.id)
            if existing and existing.name != self.team.name:
                await select_interaction.response.send_message(
                    f"{member.mention} is already on **{existing.name}**. "
                    f"Remove them from that team first before force-adding.",
                    ephemeral=True,
                )
                return
            try: self.manager.add_member(self.team, member.id)
            except ValueError as exc: await select_interaction.response.send_message(str(exc), ephemeral=True); return
            notes: List[str] = []
            for role in (self.guild.get_role(self.team.role_id), self.member_role):
                msg = await _safe_add_role(member, role, reason="Added to team by admin")
                if msg: notes.append(msg)
            response = f"Added {member.mention} to the roster."
            if notes: response = "\n".join([response, *dict.fromkeys(notes)])
            await select_interaction.response.send_message(response, ephemeral=True)
            await self._redraw()
            await self.bot.log_event(self.guild, f"{member.mention} has joined **{self.team.name}**")

        view.add_item(InviteUserSelect(handle))
        await interaction.response.send_message("Select a player to add:", view=view, ephemeral=True)

    async def _on_disband(self, interaction: discord.Interaction) -> None:
        if not self.is_captain_or_admin:
            await _reply(interaction, "Only the captain or an admin can disband the team."); return
        confirm = ConfirmView()
        await interaction.response.send_message(f"Are you sure you want to disband **{self.team.name}**? This cannot be undone.", view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value: return
        notes: List[str] = []
        role = self.guild.get_role(self.team.role_id)
        msg = await _safe_delete_role(role, reason="Team disbanded")
        if msg: notes.append(msg)
        cap = await self._ensure_member(self.team.captain_id)
        if cap:
            msg = await _safe_remove_role(cap, self.captain_role, reason="Team disbanded")
            if msg: notes.append(msg)
        for uid in list(self.team.co_captains):
            m = await self._ensure_member(uid)
            if m:
                msg = await _safe_remove_role(m, self.co_captain_role, reason="Team disbanded")
                if msg: notes.append(msg)
        for uid in list(self.team.members):
            m = await self._ensure_member(uid)
            if m:
                msg = await _safe_remove_role(m, self.member_role, reason="Team disbanded")
                if msg: notes.append(msg)
        team_name = self.team.name
        self.manager.delete_team(team_name)
        await self.interaction.edit_original_response(content="Team disbanded.", embed=None, view=None)
        self.stop()
        await self.bot.log_event(self.guild, f"## 🔴 Team Disbanded\n> **{team_name}** has been disbanded.")
        if notes: await interaction.followup.send("\n".join(dict.fromkeys(notes)), ephemeral=True)

    async def _on_transfer(self, interaction: discord.Interaction) -> None:
        if not self.is_captain_or_admin:
            await _reply(interaction, "Only the captain or an admin can transfer captaincy."); return
        options = [
            discord.SelectOption(
                label=(self.guild.get_member(uid) or uid).display_name if hasattr(self.guild.get_member(uid), "display_name") else str(uid),
                value=str(uid),
            )
            for uid in self.team.members if uid != self.team.captain_id
        ]
        if not options: await _reply(interaction, "No eligible members to transfer to."); return
        select = discord.ui.Select(placeholder="Select new captain", options=options)

        async def callback(inter: discord.Interaction) -> None:
            new_cap_id = int(select.values[0])
            member = await self._ensure_member(new_cap_id)
            if not member: await inter.response.send_message("Member is no longer in the server.", ephemeral=True); return
            old_cap_id = self.team.captain_id
            self.manager.set_captain(self.team, new_cap_id)
            notes: List[str] = []
            msg = await _safe_add_role(member, self.captain_role, reason="Promoted to captain")
            if msg: notes.append(msg)
            if old_cap_id != new_cap_id:
                old_cap = await self._ensure_member(old_cap_id)
                if old_cap:
                    msg = await _safe_remove_role(old_cap, self.captain_role, reason="Captaincy transferred")
                    if msg: notes.append(msg)
            msg = await _safe_add_role(member, self.member_role, reason="Joined team roster")
            if msg: notes.append(msg)
            response = f"Transferred captaincy to {member.mention}."
            if notes: response = "\n".join([response, *dict.fromkeys(notes)])
            await inter.response.send_message(response, ephemeral=True)
            await self._redraw()
            if old_cap_id != new_cap_id:
                await self.bot.log_event(
                    self.guild,
                    f"**{member.mention}** is now the captain of **{self.team.name}**",
                )

        select.callback = callback  # type: ignore[assignment]
        view = discord.ui.View()
        view.add_item(select)
        await interaction.response.send_message("Choose the new captain:", view=view, ephemeral=True)

    async def _on_kick(self, interaction: discord.Interaction) -> None:
        if not self.can_manage_roster:
            await _reply(interaction, "Only the captain, co-captains, or an admin can kick members."); return
        if not self.selected_member: await _reply(interaction, "Select a member first."); return
        was_co_cap = self.selected_member in self.team.co_captains
        kicked_id = self.selected_member
        try: self.manager.remove_member(self.team, self.selected_member)
        except ValueError as exc: await _reply(interaction, str(exc)); return
        member = await self._ensure_member(self.selected_member)
        notes: List[str] = []
        if member:
            role = self.guild.get_role(self.team.role_id)
            for r in filter(None, [role, self.co_captain_role if was_co_cap else None, self.member_role]):
                msg = await _safe_remove_role(member, r, reason="Removed from team")
                if msg: notes.append(msg)
        self.selected_member = None
        self.kick_button.disabled = True
        self.promote_button.disabled = True
        response = "Member removed from the roster."
        if notes: response = "\n".join([response, *dict.fromkeys(notes)])
        await interaction.response.send_message(response, ephemeral=True)
        await self._redraw()
        kicked_mention = member.mention if member else f"<@{kicked_id}>"
        await self.bot.log_event(
            self.guild,
            f"Roster Kick - {kicked_mention} was removed from **{self.team.name}** "
            f"by {interaction.user.mention}",
        )

    async def _on_promote(self, interaction: discord.Interaction) -> None:
        if not self.is_captain_or_admin:
            await _reply(interaction, "Only the captain or an admin can promote or demote co-captains."); return
        if not self.selected_member: await _reply(interaction, "Select a member first."); return
        promoted_id = self.selected_member
        try: promoted = self.manager.toggle_co_captain(self.team, self.selected_member)
        except ValueError as exc: await _reply(interaction, str(exc)); return
        member = await self._ensure_member(self.selected_member)
        notes: List[str] = []
        if member:
            fn = _safe_add_role if promoted else _safe_remove_role
            msg = await fn(member, self.co_captain_role, reason="Co-captain status changed")
            if msg: notes.append(msg)
        response = ("Promoted to co-captain" if promoted else "Removed as co-captain") + " successfully."
        if notes: response = "\n".join([response, *dict.fromkeys(notes)])
        await interaction.response.send_message(response, ephemeral=True)
        await self._redraw()
        target_mention = member.mention if member else f"<@{promoted_id}>"
        action_str = "promoted to co-captain" if promoted else "removed as co-captain"
        await self.bot.log_event(
            self.guild,
            f"{target_mention} was {action_str} in **{self.team.name}** by {interaction.user.mention}",
        )


# =============================================================================
#  ROSTER LOOKUP VIEW
# =============================================================================

class RosterLookupView(discord.ui.View):
    def __init__(self, *, interaction: discord.Interaction, teams: List[Team]) -> None:
        super().__init__(timeout=180)
        if not teams: raise ValueError("RosterLookupView requires at least one team.")
        self.interaction = interaction
        self._team_map = {t.name.lower(): t for t in teams}
        sorted_teams = sorted(teams, key=lambda t: t.name.lower())
        self.current_team = sorted_teams[0]
        options = [discord.SelectOption(label=t.name, value=t.name.lower(), description=f"{len(t.members)} member(s)") for t in sorted_teams[:25]]
        if options: options[0].default = True
        select = discord.ui.Select(placeholder="Select a team to view", options=options)
        select.callback = self._on_select  # type: ignore[assignment]
        self.add_item(select)
        self._select = select

    async def _on_select(self, interaction: discord.Interaction) -> None:
        team = self._team_map.get(self._select.values[0])
        if not team: await interaction.response.send_message("Team not found.", ephemeral=True); return
        self.current_team = team
        await interaction.response.edit_message(embed=build_team_embed(team, interaction.guild or self.interaction.guild), view=self)

    async def on_timeout(self) -> None:
        self._select.disabled = True
        try: await self.interaction.edit_original_response(view=self)
        except discord.HTTPException: pass


# =============================================================================
#  MATCH ASSIGNMENT VIEW
# =============================================================================

class _AssignmentButton(discord.ui.Button):
    def __init__(self, *, label: str, style: discord.ButtonStyle, role_id: Optional[int], channel_id: int) -> None:
        super().__init__(label=label, style=style)
        self.role_id = role_id
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not guild: await _reply(interaction, "Use this inside the assignments channel."); return
        channel = guild.get_channel(self.channel_id)
        if not isinstance(channel, discord.TextChannel):
            try: channel = await guild.fetch_channel(self.channel_id)
            except (discord.Forbidden, discord.HTTPException): channel = None
        if not isinstance(channel, discord.TextChannel): await _reply(interaction, "I can't find the match channel."); return
        member = interaction.user
        if not isinstance(member, discord.Member): await _reply(interaction, "Join the server to claim this match."); return
        required_role = guild.get_role(self.role_id) if self.role_id else None
        if required_role and required_role not in member.roles: await _reply(interaction, f"You need the {required_role.mention} role to claim this."); return
        try: await channel.set_permissions(member, view_channel=True, send_messages=True)
        except discord.HTTPException: await _reply(interaction, "I couldn't update the channel permissions."); return
        self.disabled = True
        if interaction.message:
            try: await interaction.message.edit(view=self.view)
            except discord.HTTPException: pass
        await _reply(interaction, f"You're set for {channel.mention} as {self.label}.")
        try: await channel.send(f"{member.mention} accepted this match as {self.label}.")
        except discord.HTTPException: pass


class AssignmentClaimView(discord.ui.View):
    def __init__(self, *, bot: "LeagueBot", match_channel_id: int) -> None:
        super().__init__(timeout=7 * 24 * 60 * 60)
        for label, style, role_id in [
            ("Accept as Caster", discord.ButtonStyle.primary,   bot.config.caster_role_id),
            ("Accept as Ref",    discord.ButtonStyle.primary,   bot.config.ref_role_id),
            ("Accept as Mod",    discord.ButtonStyle.secondary, bot.config.mod_role_id),
        ]:
            self.add_item(_AssignmentButton(label=label, style=style, role_id=role_id, channel_id=match_channel_id))


class ConfirmTimeView(discord.ui.View):
    def __init__(self, *, scheduled_time: str, on_confirm: Callable[[discord.Interaction], Awaitable[None]], on_change: Callable[[discord.Interaction], Awaitable[None]]) -> None:
        super().__init__(timeout=7 * 24 * 60 * 60)
        self._on_confirm = on_confirm
        self._on_change = on_change

    def _disable(self) -> None:
        for child in self.children: child.disabled = True

    @discord.ui.button(label="Confirm Time", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._on_confirm(interaction)
        self._disable()
        if interaction.message:
            try: await interaction.message.edit(view=self)
            except discord.HTTPException: pass

    @discord.ui.button(label="Change Time", style=discord.ButtonStyle.secondary)
    async def change(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._on_change(interaction)
        self._disable()
        if interaction.message:
            try: await interaction.message.edit(view=self)
            except discord.HTTPException: pass


# =============================================================================
#  HELP SYSTEM
# =============================================================================

HELP_CATEGORIES = {
    "Ticket Commands": {
        "description": "Commands for managing support tickets.",
        "color": discord.Color.red(),
        "fields": [
            ("/setup",        "Send the ticket panel in this channel. *(Admin only)*"),
            ("/close",        "Close the current ticket and save a transcript."),
            ("/closerequest", "Send a close request to the ticket owner."),
            ("/add",          "Add a user or role to the current ticket."),
            ("/remove",       "Remove a user or role from the current ticket."),
            ("/claim",        "Claim the current ticket as yours to handle."),
            ("/unclaim",      "Release your claim on the current ticket."),
            ("/rename",       "Rename the current ticket channel."),
        ],
    },
    "Moderation Commands": {
        "description": "Commands for moderating server members.",
        "color": discord.Color.orange(),
        "fields": [
            ("/punish",       "Apply a punishment (warn/timeout/kick/ban) to a user."),
            ("/unban",        "Unban a user using their Discord ID."),
            ("/history",      "View a user's last 10 moderation records."),
            ("/clearrecords", "Wipe all moderation records for a user."),
            ("/note",         "Add a private staff note to a user's record."),
            ("/info",         "View a full member profile including infractions and notes."),
        ],
    },
    "Team Commands": {
        "description": "Commands for managing competitive teams.",
        "color": discord.Color.green(),
        "fields": [
            ("/create-team",       "Create a new team. *(Admin only)*"),
            ("/manage-team",       "Manage your team roster."),
            ("/admin-manage",      "Admin: manage any team."),
            ("/leave",             "Leave your current team."),
            ("/roster",            "Browse team rosters."),
            ("/admin-edit",        "Admin: edit a team's settings."),
            ("/admin-lock",        "Admin: toggle roster lock."),
            ("/admin-disband-all", "Admin: disband every team."),
        ],
    },
    "Match Commands": {
        "description": "Commands for scheduling and reporting matches.",
        "color": discord.Color.blue(),
        "fields": [
            ("/admin-create-match",  "Admin: create a private match channel."),
            ("/submit-time",         "Propose and confirm your match time."),
            ("/edit-match",          "Staff: assign caster and/or ref to a match."),
            ("/match-history",       "View official match results by type."),
            ("/admin-submit-scores", "Admin: override match scores."),
            ("/getschedules",        "View all upcoming match schedules."),
        ],
    },
    "General Commands": {
        "description": "Miscellaneous commands available to everyone.",
        "color": discord.Color.blurple(),
        "fields": [
            ("/appeal", "Submit a ban appeal (3-month cooldown)."),
            ("/help",   "Show this help menu."),
            ("/ping",   "Check bot latency."),
        ],
    },
    "AI Commands": {
        "description": "Commands to manage the AI support assistant.",
        "color": discord.Color.purple(),
        "fields": [
            ("/aiexample", "Add a Q&A training example. *(AI Admins only)*"),
            ("/ailist",    "View all AI training examples. *(AI Admins only)*"),
            ("/aidelete",  "Delete a training example by number. *(AI Admins only)*"),
            ("/aiclear",   "Wipe ALL AI training data. *(AI Admins only)*"),
        ],
    },
}


class HelpSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [discord.SelectOption(label=cat, description=data["description"], value=cat) for cat, data in HELP_CATEGORIES.items()]
        super().__init__(placeholder="Select a category...", options=options, custom_id="help_select")

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        if selected == "Moderation Commands" and not bot.is_staff(interaction):
            await interaction.response.edit_message(embed=discord.Embed(title="Access Denied", description="Staff only.", color=discord.Color.red()), view=self.view)
            return
        if selected == "AI Commands" and not bot.is_ai_admin(interaction):
            await interaction.response.edit_message(embed=discord.Embed(title="Access Denied", description="You don't have permission to view AI commands.", color=discord.Color.red()), view=self.view)
            return
        cat = HELP_CATEGORIES[selected]
        embed = discord.Embed(title=selected, description=cat["description"], color=cat["color"])
        for name, value in cat["fields"]:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="Use the dropdown below to switch categories.")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=120)
        self.add_item(HelpSelect())


async def prompt_confirmation(interaction: discord.Interaction, message: str) -> bool:
    view = ConfirmView()
    await interaction.response.send_message(message, view=view, ephemeral=True)
    await view.wait()
    return bool(view.value)


# =============================================================================
#  EDIT MATCH VIEW  (staff assign caster / ref)
# =============================================================================

class EditMatchSelect(discord.ui.Select):
    """Dropdown to pick which open match to edit."""

    def __init__(self, matches: list, bot: "LeagueBot", guild: discord.Guild) -> None:
        self._matches = {m.id: m for m in matches}
        self._bot = bot
        self._guild = guild
        options = [
            discord.SelectOption(
                label=f"{m.team_one} vs {m.team_two}",
                description=f"Week {m.week} · {m.match_type.capitalize()} · Due {m.due_at[:10]}",
                value=m.id,
            )
            for m in matches[:25]
        ]
        super().__init__(placeholder="Select a match…", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        match = self._matches.get(self.values[0])
        if not match:
            await interaction.response.send_message("Match not found.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"{match.team_one} vs {match.team_two}",
            description="What would you like to edit for this match?",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Week", value=str(match.week), inline=True)
        embed.add_field(name="Type", value=match.match_type.capitalize(), inline=True)
        embed.add_field(name="Due", value=match.due_at[:10], inline=True)
        if match.scheduled_time:
            embed.add_field(name="Scheduled Time", value=match.scheduled_time, inline=False)

        action_view = EditMatchActionView(bot=self._bot, match=match, guild=self._guild)
        await interaction.response.edit_message(embed=embed, view=action_view)


class EditMatchView(discord.ui.View):
    """Initial view — just the match picker dropdown."""

    def __init__(self, *, bot: "LeagueBot", matches: list, guild: discord.Guild) -> None:
        super().__init__(timeout=120)
        self.add_item(EditMatchSelect(matches=matches, bot=bot, guild=guild))


class EditMatchActionView(discord.ui.View):
    """Shown after selecting a match — buttons for what to assign."""

    def __init__(self, *, bot: "LeagueBot", match, guild: discord.Guild) -> None:
        super().__init__(timeout=120)
        self._bot = bot
        self._match = match
        self._guild = guild

    @discord.ui.button(label="Assign Caster", style=discord.ButtonStyle.primary, emoji="🎙️")
    async def assign_caster(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show_role_picker(interaction, role_type="caster")

    @discord.ui.button(label="Assign Ref", style=discord.ButtonStyle.primary, emoji="🧑‍⚖️")
    async def assign_ref(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show_role_picker(interaction, role_type="ref")

    @discord.ui.button(label="Assign Both", style=discord.ButtonStyle.success, emoji="✅")
    async def assign_both(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._show_role_picker(interaction, role_type="both")

    async def _show_role_picker(self, interaction: discord.Interaction, role_type: str) -> None:
        view = AssignStaffView(
            bot=self._bot,
            match=self._match,
            guild=self._guild,
            role_type=role_type,
        )
        embed = discord.Embed(
            title=f"Assign {'Caster & Ref' if role_type == 'both' else role_type.capitalize()}",
            description=(
                f"**Match:** {self._match.team_one} vs {self._match.team_two}\n\n"
                "Use the member selector(s) below to choose the staff member(s)."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=view)


class _StaffMemberSelect(discord.ui.UserSelect):
    """A UserSelect for picking a caster or ref."""

    def __init__(self, *, placeholder: str, row: int) -> None:
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, row=row)
        self.chosen: Optional[discord.Member] = None

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        guild = interaction.guild
        member = None
        if isinstance(selected, discord.Member):
            member = selected
        else:
            uid = getattr(selected, "id", None)
            if uid:
                member = guild.get_member(uid)
                if not member:
                    try:
                        member = await guild.fetch_member(uid)
                    except discord.HTTPException:
                        pass
        self.chosen = member
        await interaction.response.defer()


class AssignStaffView(discord.ui.View):
    """View with one or two UserSelects + a Confirm button."""

    def __init__(self, *, bot: "LeagueBot", match, guild: discord.Guild, role_type: str) -> None:
        super().__init__(timeout=180)
        self._bot = bot
        self._match = match
        self._guild = guild
        self._role_type = role_type  # "caster" | "ref" | "both"

        self._caster_select: Optional[_StaffMemberSelect] = None
        self._ref_select: Optional[_StaffMemberSelect] = None

        if role_type in ("caster", "both"):
            self._caster_select = _StaffMemberSelect(placeholder="Select Caster…", row=0)
            self.add_item(self._caster_select)

        if role_type in ("ref", "both"):
            self._ref_select = _StaffMemberSelect(placeholder="Select Referee…", row=1 if role_type == "both" else 0)
            self.add_item(self._ref_select)

        confirm_btn = discord.ui.Button(
            label="Confirm Assignment",
            style=discord.ButtonStyle.success,
            emoji="✔️",
            row=2,
        )
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        caster: Optional[discord.Member] = self._caster_select.chosen if self._caster_select else None
        ref: Optional[discord.Member] = self._ref_select.chosen if self._ref_select else None

        if self._role_type == "caster" and not caster:
            await interaction.response.send_message("Please select a caster first.", ephemeral=True)
            return
        if self._role_type == "ref" and not ref:
            await interaction.response.send_message("Please select a referee first.", ephemeral=True)
            return
        if self._role_type == "both" and (not caster or not ref):
            await interaction.response.send_message("Please select both a caster and a referee.", ephemeral=True)
            return

        match = self._match
        guild = self._guild

        # Post to the match channel
        match_channel = guild.get_channel(match.channel_id)
        if isinstance(match_channel, discord.TextChannel):
            lines = [f"**Staff Assignment — {match.team_one} vs {match.team_two}**"]
            if caster:
                lines.append(f"🎙️ **Caster:** {caster.mention}")
            if ref:
                lines.append(f"🧑‍⚖️ **Referee:** {ref.mention}")
            try:
                await match_channel.send("\n".join(lines))
                for member in filter(None, [caster, ref]):
                    try:
                        await match_channel.set_permissions(
                            member, view_channel=True, send_messages=True
                        )
                    except discord.HTTPException:
                        pass
            except discord.HTTPException:
                pass

        # Post to assignments channel
        assignments_channel_id = self._bot.config.match_assignments_channel_id
        if assignments_channel_id:
            assignments_channel = guild.get_channel(assignments_channel_id)
            if isinstance(assignments_channel, discord.TextChannel):
                embed = discord.Embed(
                    title="Match Staff Assignment",
                    color=discord.Color.green(),
                )
                embed.add_field(
                    name="Match",
                    value=f"{match.team_one} vs {match.team_two} (Week {match.week})",
                    inline=False,
                )
                if match.scheduled_time:
                    embed.add_field(name="Scheduled Time", value=match.scheduled_time, inline=False)
                if caster:
                    embed.add_field(name="🎙️ Caster", value=caster.mention, inline=True)
                if ref:
                    embed.add_field(name="🧑‍⚖️ Referee", value=ref.mention, inline=True)
                embed.set_footer(text=f"Assigned by {interaction.user}")
                try:
                    await assignments_channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        # Show confirmation back to staff
        summary_embed = discord.Embed(
            title="✅ Assignment Confirmed",
            color=discord.Color.green(),
        )
        summary_embed.add_field(
            name="Match",
            value=f"{match.team_one} vs {match.team_two}",
            inline=False,
        )
        if caster:
            summary_embed.add_field(name="🎙️ Caster", value=caster.mention, inline=True)
        if ref:
            summary_embed.add_field(name="🧑‍⚖️ Referee", value=ref.mention, inline=True)
        summary_embed.set_footer(text="Posted to match channel and assignments channel.")

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=summary_embed, view=self)
        self.stop()
