"""Unified Discord bot: tickets, moderation, teams, matches, and AI support."""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from groq import AsyncGroq

from .config import BotConfig
from .match_manager import MatchManager
from .mod_data import add_record, load_mod, load_training, parse_duration, save_mod, save_training
from .rules import fetch_rules_text, parse_all_rules
from .team_manager import TeamManager
from .views import (
    AppealActionView,
    AppealModal,
    AssignmentClaimView,
    AssignStaffView,
    ConfirmTimeView,
    ConfirmView,
    HelpView,
    ManageTeamView,
    RosterLookupView,
    TicketControlView,
    TicketView,
    CloseRequestView,
    TICKET_PANEL_DESCRIPTION,
    HELP_CATEGORIES,
    _safe_add_role,
    _safe_delete_role,
    _safe_remove_role,
    build_team_embed,
    prompt_confirmation,
    send_transcript,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# =============================================================================
#  BOT CLASS
# =============================================================================

class LeagueBot(commands.Bot):
    """Unified bot wiring together all features."""

    def __init__(self, *, config: BotConfig, data_path: Path, match_path: Path) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

        self.config = config
        self.team_manager = TeamManager(data_path)
        self.match_manager = MatchManager(match_path)

        self.ticket_owners: dict[int, int] = {}
        self.ticket_ai_history: dict[int, list] = {}

        self._groq: Optional[AsyncGroq] = None
        if config.groq_api_key:
            self._groq = AsyncGroq(api_key=config.groq_api_key)

        self._web_runner: Optional[web.AppRunner] = None
        self._web_site: Optional[web.TCPSite] = None

    # ── Permission helpers ────────────────────────────────────────────────────

    def is_staff(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        user_role_ids = {r.id for r in interaction.user.roles}
        return bool(user_role_ids & set(self.config.staff_role_ids))

    def is_admin(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        return bool({r.id for r in member.roles} & set(self.config.admin_role_ids))

    def is_ai_admin(self, interaction: discord.Interaction) -> bool:
        return self.is_staff(interaction)

    def require_admin(self, interaction: discord.Interaction) -> bool:
        return isinstance(interaction.user, discord.Member) and self.is_admin(interaction.user)

    def in_ticket(self, interaction: discord.Interaction) -> bool:
        if not interaction.channel or not interaction.channel.category:
            return False
        return interaction.channel.category_id in self.config.ticket_category_ids

    def has_player_role(self, member: discord.Member) -> bool:
        role_id = self.config.team_member_role_id
        return role_id is not None and any(r.id == role_id for r in member.roles)

    # ── Logging/events ────────────────────────────────────────────────────────

    async def log_event(self, guild: discord.Guild, content: str) -> None:
        channel_id = self.config.transactions_channel_id or self.config.log_channel_id
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException):
                return
        try:
            await channel.send(content)
        except discord.HTTPException:
            pass

    async def send_mod_log(self, guild: discord.Guild, title: str, color: discord.Color, **fields) -> None:
        channel_id = self.config.log_channel_id
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        for name, value in fields.items():
            embed.add_field(name=name.replace("_", " ").title(), value=value, inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ── Member resolver ───────────────────────────────────────────────────────

    async def resolve_member(self, interaction: discord.Interaction, member: str) -> Optional[discord.Member]:
        raw = member.strip("<@!>")
        if raw.isdigit():
            resolved = interaction.guild.get_member(int(raw))
            if not resolved:
                try:
                    resolved = await interaction.guild.fetch_member(int(raw))
                except (discord.NotFound, discord.HTTPException):
                    return None
            return resolved
        return discord.utils.find(
            lambda m: m.name.lower() == member.lower() or m.display_name.lower() == member.lower(),
            interaction.guild.members,
        )

    # ── AI system prompt ──────────────────────────────────────────────────────

    def _build_ai_system_prompt(self) -> str:
        prompt = (
            "You are a professional AI support assistant for a Discord server called Pro For All. "
            "Help users inside support tickets clearly, concisely, and professionally. "
            "Be polite and friendly but stay brief. "
            "If a question requires staff action (bans, unbans, rank changes, account verification, reports), "
            "tell the user that staff will follow up and they should describe their issue clearly. "
            "Never make up information. Never pretend to be a human staff member. "
            "Do not respond to spam, trolling, or off-topic messages. "
            "Keep responses under 3 sentences unless more detail is truly necessary."
        )
        examples = load_training()
        if examples:
            prompt += "\n\n--- TRAINED KNOWLEDGE BASE ---\n"
            prompt += "Use the following Q&A pairs to answer questions accurately. Prioritise these above all else.\n"
            for ex in examples:
                prompt += f"\nQ: {ex['question']}\nA: {ex['answer']}\n"
        return prompt

    # ── Match helpers ─────────────────────────────────────────────────────────

    def _match_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        if not self.config.match_category_id:
            return None
        ch = guild.get_channel(self.config.match_category_id)
        return ch if isinstance(ch, discord.CategoryChannel) else None

    def _team_ping(self, guild: discord.Guild, team_name: str) -> str:
        team = self.team_manager.get_team(team_name)
        if team:
            role = guild.get_role(team.role_id)
            if role:
                return role.mention
        return f"**{team_name}**"

    def _week_window(self) -> tuple[datetime.datetime, datetime.datetime, int]:
        now = datetime.datetime.utcnow()
        due = now + timedelta(days=7)
        week = int(((now - datetime.datetime(now.year, 1, 1)).days // 7) + 1)
        return now, due, week

    async def _post_results(self, guild: discord.Guild, *, winner: str, loser: str, score_one: int,
                            score_two: int, team_one: str, team_two: str, match_type: str = "seeding") -> None:
        channel_id = self.config.match_results_channel_id
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        type_label = "Top Bracket" if match_type == "bracket" else "Seeding"
        msg = (
            f"## PFA Season 4 Match {type_label}\n"
            f"# __{team_one}__ *v.s* __{team_two}__\n"
            f"## Score: ||*{score_one}-{score_two}*||\n"
            f"## Winner: ||{winner}||"
        )
        try:
            await channel.send(msg)
        except discord.HTTPException:
            pass

    async def _lock_match_channel(self, channel: discord.TextChannel, match: Any) -> None:
        try:
            await channel.set_permissions(channel.guild.default_role, view_channel=False, send_messages=False)
            for name in (match.team_one, match.team_two):
                team = self.team_manager.get_team(name)
                if team:
                    role = channel.guild.get_role(team.role_id)
                    if role:
                        await channel.set_permissions(role, view_channel=False, send_messages=False)
        except discord.HTTPException:
            pass

    async def _send_staff_alert(self, guild: discord.Guild, content: str) -> None:
        channel_id = self.config.match_staff_alert_channel_id
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(content)
        except discord.HTTPException:
            pass

    async def _report_challonge(self, match: Any, scores: Dict[str, int]) -> Optional[str]:
        cfg = self.config
        if not all([cfg.challonge_username, cfg.challonge_api_key, cfg.challonge_tournament]):
            return None

        auth = aiohttp.BasicAuth(cfg.challonge_username, cfg.challonge_api_key)
        base = f"https://api.challonge.com/v1/tournaments/{cfg.challonge_tournament}"

        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.get(f"{base}/participants.json") as resp:
                if resp.status != 200:
                    return f"Challonge participants request failed ({resp.status})."
                participants = {
                    str(p["participant"].get("display_name") or p["participant"].get("name", "")).lower(): p["participant"]["id"]
                    for p in await resp.json()
                }

            p1_id = participants.get(match.team_one.lower())
            p2_id = participants.get(match.team_two.lower())
            if not p1_id or not p2_id:
                return "Teams are not registered in Challonge; skipping bracket update."

            async with session.get(f"{base}/matches.json") as resp:
                if resp.status != 200:
                    return f"Challonge matches request failed ({resp.status})."
                target = next(
                    (
                        m["match"] for m in await resp.json()
                        if {m["match"]["player1_id"], m["match"]["player2_id"]} == {p1_id, p2_id}
                        and m["match"]["state"] != "complete"
                    ),
                    None,
                )

            if not target:
                return "Unable to locate the Challonge match for these teams."

            mp1, mp2 = int(target["player1_id"]), int(target["player2_id"])
            s1 = scores.get(match.team_one, 0) if mp1 == p1_id else scores.get(match.team_two, 0)
            s2 = scores.get(match.team_two, 0) if mp2 == p2_id else scores.get(match.team_one, 0)
            winner_id = p1_id if scores.get(match.team_one, 0) > scores.get(match.team_two, 0) else p2_id

            async with session.put(
                f"{base}/matches/{target['id']}.json",
                json={"match": {"scores_csv": f"{s1}-{s2}", "winner_id": winner_id}},
            ) as resp:
                if resp.status != 200:
                    return f"Challonge update failed ({resp.status})."
        return None

    # ── Web server ────────────────────────────────────────────────────────────

    async def _start_status_site(self) -> None:
        host = self.config.web_host or "0.0.0.0"
        port = self.config.web_port or 8080

        app = web.Application()
        app.router.add_get("/", lambda _: web.json_response({"status": "ok", "bot": str(self.user)}))
        app.router.add_get("/ping", lambda _: web.Response(text="pong"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
        self._web_runner = runner
        self._web_site = site
        log.info("Status site running on http://%s:%s", host, port)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def setup_hook(self) -> None:
        await self.add_cog(LeagueCommands(self))

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Commands synced to guild %s", self.config.guild_id)
        else:
            for guild in self.guilds:
                obj = discord.Object(id=guild.id)
                self.tree.copy_global_to(guild=obj)
                await self.tree.sync(guild=obj)
            log.info("Commands synced globally")

        await self._start_status_site()

    async def on_ready(self) -> None:
        self.add_view(TicketView())
        self.add_view(TicketControlView())
        self.add_view(CloseRequestView())

        data = load_mod()
        for uid_str, appeal in data.get("appeals", {}).items():
            if appeal.get("status") == "pending":
                try:
                    self.add_view(AppealActionView(int(appeal.get("user_id", uid_str))))
                except (ValueError, TypeError):
                    pass

        log.info(
            "Logged in as %s | Teams: %d | Matches: %d",
            self.user,
            len(list(self.team_manager.all_teams())),
            len(list(self.match_manager.all_matches())),
        )

    async def on_message(self, message: discord.Message) -> None:
        await self.process_commands(message)
        if message.author.bot:
            return
        if not (message.channel.category and message.channel.category_id in self.config.ticket_category_ids):
            return
        if isinstance(message.author, discord.Member):
            if (
                message.author.guild_permissions.administrator
                or any(r.id in self.config.staff_role_ids for r in message.author.roles)
            ):
                return
        if not self._groq or len(message.content.strip()) < 8:
            return

        channel_id = message.channel.id
        history = self.ticket_ai_history.setdefault(channel_id, [])
        history.append({"role": "user", "content": message.content})
        if len(history) > 20:
            self.ticket_ai_history[channel_id] = history[-20:]

        async with message.channel.typing():
            try:
                completion = await self._groq.chat.completions.create(
                    model=self.config.groq_model,
                    messages=[{"role": "system", "content": self._build_ai_system_prompt()}] + history,
                    max_tokens=512,
                    temperature=0.4,
                )
                reply = completion.choices[0].message.content.strip()
                self.ticket_ai_history[channel_id].append({"role": "assistant", "content": reply})
                embed = discord.Embed(description=reply, color=discord.Color.purple())
                embed.set_author(name="AI Support Assistant")
                embed.set_footer(text="AI-generated - Staff will follow up on complex issues.")
                await message.channel.send(embed=embed)
            except Exception as exc:
                log.warning("AI response error: %s", exc)

    async def close(self) -> None:
        if self._web_site:
            await self._web_site.stop()
        if self._web_runner:
            await self._web_runner.cleanup()
        await super().close()


# =============================================================================
#  PAGINATED RULE PICKER
# =============================================================================

PAGE_SIZE = 25


class RuleSelect(discord.ui.Select):
    def __init__(self, rules_page: list[dict], target_member: discord.Member, bot: "LeagueBot") -> None:
        self._rules_page = rules_page
        self._target = target_member
        self._bot = bot
        options = [
            discord.SelectOption(label=r["label"], value=str(i))
            for i, r in enumerate(rules_page)
        ]
        super().__init__(placeholder="Select a rule...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        rule = self._rules_page[int(self.values[0])]
        dur = rule["duration"]
        if dur is None:
            dur_str = "permanent"
        else:
            total_seconds = int(dur.total_seconds())
            days = total_seconds // 86400
            dur_str = f"{days}d" if days else f"{total_seconds // 3600}h"

        modal = PunishModal(
            action=rule["action"],
            member=self._target,
            bot=self._bot,
            prefill_reason=f"Rule {rule['rule']} – {rule['title']} ({rule['punishment']})",
            prefill_duration=dur_str,
        )
        await interaction.response.send_modal(modal)


class RulePickerView(discord.ui.View):
    def __init__(self, *, all_rules: list[dict], target_member: discord.Member, bot: "LeagueBot", page: int = 0) -> None:
        super().__init__(timeout=120)
        self._all = all_rules
        self._target = target_member
        self._bot = bot
        self._page = page
        self._total_pages = max(1, (len(all_rules) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        start = self._page * PAGE_SIZE
        page_rules = self._all[start: start + PAGE_SIZE]
        self.add_item(RuleSelect(page_rules, self._target, self._bot))

        prev = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self._page == 0, row=1)
        prev.callback = self._prev
        self.add_item(prev)

        indicator = discord.ui.Button(label=f"Page {self._page + 1} / {self._total_pages}", style=discord.ButtonStyle.secondary, disabled=True, row=1)
        self.add_item(indicator)

        nxt = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, disabled=self._page >= self._total_pages - 1, row=1)
        nxt.callback = self._next
        self.add_item(nxt)

    async def _prev(self, interaction: discord.Interaction) -> None:
        self._page -= 1
        self._rebuild()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _next(self, interaction: discord.Interaction) -> None:
        self._page += 1
        self._rebuild()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    def _embed(self) -> discord.Embed:
        start = self._page * PAGE_SIZE + 1
        end = min(start + PAGE_SIZE - 1, len(self._all))
        return discord.Embed(
            title="Select a Rule",
            description=(
                f"Punishing: {self._target.mention}\n"
                f"Showing rules {start}–{end} of {len(self._all)}.\n"
                "Pick from the dropdown below."
            ),
            color=discord.Color.red(),
        )


# =============================================================================
#  COMMANDS COG
# =============================================================================

class _MatchCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Channel", style=discord.ButtonStyle.danger)
    async def close_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from .bot import LeagueBot
        bot: LeagueBot = interaction.client  # type: ignore[assignment]
        if not bot.is_staff(interaction):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        await interaction.response.send_message("Closing channel...", ephemeral=True)
        await interaction.channel.delete()


class LeagueCommands(commands.Cog):
    """All slash commands for the unified bot."""

    def __init__(self, bot: LeagueBot) -> None:
        self.bot = bot
        self._reminder_loop.start()

    def cog_unload(self) -> None:
        self._reminder_loop.cancel()

    async def _send(self, interaction: discord.Interaction, *args, **kwargs) -> None:
        kwargs.setdefault("ephemeral", True)
        if interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await interaction.response.send_message(*args, **kwargs)

    # ── Autocomplete ──────────────────────────────────────────────────────────

    async def _team_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=t.name, value=t.name)
            for t in self.bot.team_manager.all_teams()
            if current.lower() in t.name.lower()
        ][:25]

    # -------------------------------------------------------------------------
    #  TICKET COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="setup", description="Send the ticket panel (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Open a Ticket Below!",
            description=TICKET_PANEL_DESCRIPTION,
            color=discord.Color.red(),
        )
        await interaction.channel.send(embed=embed, view=TicketView())
        await self._send(interaction, "Ticket panel sent!")

    @app_commands.command(name="close", description="Close the current ticket")
    async def close(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        await send_transcript(self.bot, interaction.channel, interaction.user.name, reason)
        await interaction.response.send_message("Closing ticket...")
        await interaction.channel.delete()

    @app_commands.command(name="closerequest", description="Request to close the ticket")
    async def close_request(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        embed = discord.Embed(
            title="Close Request",
            description=f"{interaction.user.mention} wants to close this ticket.",
            color=discord.Color.red(),
        )
        if reason:
            embed.add_field(name="Reason", value=reason)
        owner_id = self.bot.ticket_owners.get(interaction.channel.id)
        owner = interaction.guild.get_member(owner_id) if owner_id else None
        await interaction.channel.send(
            content=owner.mention if owner else None,
            embed=embed,
            view=CloseRequestView(interaction.user.id),
        )
        await self._send(interaction, "Close request sent!")

    @app_commands.command(name="add", description="Add a user or role to the ticket")
    async def add(self, interaction: discord.Interaction, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        target = user or role
        if not target:
            await self._send(interaction, "Specify a user or role!")
            return
        await interaction.channel.set_permissions(target, read_messages=True, send_messages=True)
        await interaction.response.send_message(f"Added {target.mention}.")

    @app_commands.command(name="remove", description="Remove a user or role from the ticket")
    async def remove(self, interaction: discord.Interaction, user: Optional[discord.Member] = None, role: Optional[discord.Role] = None) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        target = user or role
        if not target:
            await self._send(interaction, "Specify a user or role!")
            return
        await interaction.channel.set_permissions(target, overwrite=None)
        await interaction.response.send_message(f"Removed {target.mention}.")

    @app_commands.command(name="claim", description="Claim the current ticket")
    async def claim(self, interaction: discord.Interaction) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        embed = discord.Embed(title="Ticket Claimed", description=f"Handled by {interaction.user.mention}", color=discord.Color.green())
        await interaction.channel.send(embed=embed)
        await self._send(interaction, "Claimed!")

    @app_commands.command(name="unclaim", description="Unclaim the current ticket")
    async def unclaim(self, interaction: discord.Interaction) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        embed = discord.Embed(title="Ticket Unclaimed", description=f"{interaction.user.mention} unclaimed this ticket.", color=discord.Color.orange())
        await interaction.channel.send(embed=embed)
        await self._send(interaction, "Unclaimed!")

    @app_commands.command(name="rename", description="Rename the current ticket channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def rename(self, interaction: discord.Interaction, new_name: str) -> None:
        if not self.bot.in_ticket(interaction):
            await self._send(interaction, "Only usable inside tickets!")
            return
        await interaction.channel.edit(name=new_name)
        await interaction.response.send_message(f"Renamed to **{new_name}**.")

    # -------------------------------------------------------------------------
    #  MODERATION COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="punish", description="Apply a punishment to a user using the rule book")
    @app_commands.describe(member="User ID, mention, or username")
    async def punish(self, interaction: discord.Interaction, member: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return

        resolved = await self.bot.resolve_member(interaction, member)
        if not resolved:
            await self._send(interaction, "Could not find that member. Try their **User ID**.")
            return
        if resolved == interaction.user:
            await self._send(interaction, "Can't punish yourself.")
            return
        if (
            resolved.top_role >= interaction.user.top_role
            and not interaction.user.guild_permissions.administrator
        ):
            await self._send(interaction, "Can't punish someone with equal or higher role.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            text = await fetch_rules_text()
            all_rules = parse_all_rules(text)
        except Exception as exc:
            log.warning("Failed to fetch rules: %s", exc)
            await interaction.followup.send(
                "Could not load the rulebook right now. Try again in a moment.",
                ephemeral=True,
            )
            return

        if not all_rules:
            await interaction.followup.send("No rules found in the rulebook.", ephemeral=True)
            return

        view = RulePickerView(all_rules=all_rules, target_member=resolved, bot=self.bot)
        await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.describe(user_id="Banned user's ID", reason="Reason for unban")
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided") -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return
        try:
            uid = int(user_id)
            user = await self.bot.fetch_user(uid)
            await interaction.guild.unban(user, reason=reason)
        except (ValueError, discord.NotFound):
            await self._send(interaction, "Invalid ID or user not banned.")
            return
        except discord.Forbidden:
            await self._send(interaction, "No permission to unban.")
            return

        add_record(uid, "unban", reason, str(interaction.user), None)
        await self.bot.send_mod_log(
            interaction.guild, "UNBAN", discord.Color.green(),
            user=f"{user} (`{uid}`)", moderator=str(interaction.user), reason=reason,
        )
        try:
            dm = discord.Embed(title="You have been unbanned!", color=discord.Color.green())
            dm.description = f"**Reason:** {reason}\n\nYou may rejoin here: {self.bot.config.appeal_server_invite}"
            await user.send(embed=dm)
        except (discord.NotFound, discord.Forbidden):
            pass
        await self._send(interaction, f"Unbanned **{user}**.")

    @app_commands.command(name="history", description="View a user's moderation history")
    @app_commands.describe(member="User ID, mention, or username")
    async def history(self, interaction: discord.Interaction, member: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return
        resolved = await self.bot.resolve_member(interaction, member)
        raw = member.strip("<@!>")
        if resolved:
            uid, name = str(resolved.id), str(resolved)
        elif raw.isdigit():
            uid, name = raw, f"User `{raw}`"
        else:
            await self._send(interaction, "Could not find that member. Try their **User ID**.")
            return

        records = load_mod()["records"].get(uid, [])
        if not records:
            await self._send(interaction, f"**{name}** has no records.")
            return

        embed = discord.Embed(title=f"Mod History - {name}", color=discord.Color.orange())
        for i, r in enumerate(records[-10:], 1):
            dur = f" | Duration: {r['duration']}" if r.get("duration") else ""
            embed.add_field(
                name=f"#{i} - {r['action'].upper()} ({r['timestamp'][:10]})",
                value=f"**Reason:** {r['reason']}\n**Mod:** {r['mod']}{dur}",
                inline=False,
            )
        embed.set_footer(text=f"Total: {len(records)}")
        await self._send(interaction, embed=embed)

    @app_commands.command(name="clearrecords", description="Clear a user's moderation records")
    @app_commands.describe(member="User ID, mention, or username")
    async def clearrecords(self, interaction: discord.Interaction, member: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return
        resolved = await self.bot.resolve_member(interaction, member)
        raw = member.strip("<@!>")
        if resolved:
            uid, display, mention = str(resolved.id), f"{resolved} (`{resolved.id}`)", resolved.mention
        elif raw.isdigit():
            uid, display, mention = raw, f"User `{raw}`", f"<@{raw}>"
        else:
            await self._send(interaction, "Could not find that member. Try their **User ID**.")
            return

        data = load_mod()
        if not data["records"].get(uid):
            await self._send(interaction, "No records to clear.")
            return
        data["records"][uid] = []
        save_mod(data)
        await self.bot.send_mod_log(
            interaction.guild, "Records Cleared", discord.Color.blurple(),
            user=display, cleared_by=str(interaction.user),
        )
        await self._send(interaction, f"Cleared records for {mention}.")

    @app_commands.command(name="note", description="Add a staff note to a user's record")
    @app_commands.describe(member="User ID, mention, or username", note="The note to add")
    async def note(self, interaction: discord.Interaction, member: str, note: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return
        resolved = await self.bot.resolve_member(interaction, member)
        raw = member.strip("<@!>")
        if resolved:
            uid, mention = resolved.id, resolved.mention
        elif raw.isdigit():
            uid, mention = int(raw), f"<@{raw}>"
        else:
            await self._send(interaction, "Could not find that member. Try their **User ID**.")
            return
        add_record(uid, "note", note, str(interaction.user), None)
        await self._send(interaction, f"Note added to {mention}'s record.")

    @app_commands.command(name="info", description="View full info on a member")
    @app_commands.describe(member="User ID, mention, or username")
    async def info(self, interaction: discord.Interaction, member: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "No permission.")
            return

        resolved = await self.bot.resolve_member(interaction, member)
        raw = member.strip("<@!>")
        data = load_mod()

        def _split(records):
            infractions = [r for r in records if r["action"] != "note"]
            notes = [r for r in records if r["action"] == "note"]
            return infractions, notes

        if resolved:
            uid = str(resolved.id)
            infractions, notes = _split(data["records"].get(uid, []))
            embed = discord.Embed(
                title=str(resolved),
                color=resolved.color if resolved.color.value else discord.Color.blurple(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_thumbnail(url=resolved.display_avatar.url)
            embed.add_field(name="ID",      value=uid, inline=True)
            embed.add_field(name="Created", value=resolved.created_at.strftime("%d %b %Y"), inline=True)
            embed.add_field(name="Joined",  value=resolved.joined_at.strftime("%d %b %Y") if resolved.joined_at else "?", inline=True)
            embed.add_field(name="Roles",   value=", ".join(r.mention for r in resolved.roles[1:]) or "None", inline=False)
        elif raw.isdigit():
            uid = raw
            infractions, notes = _split(data["records"].get(uid, []))
            try:
                user = await self.bot.fetch_user(int(uid))
                title, avatar, created = str(user), user.display_avatar.url, user.created_at.strftime("%d %b %Y")
            except (discord.NotFound, discord.HTTPException):
                title, avatar, created = f"User `{uid}`", None, "Unknown"
            embed = discord.Embed(title=title, color=discord.Color.greyple(), timestamp=discord.utils.utcnow())
            if avatar:
                embed.set_thumbnail(url=avatar)
            embed.add_field(name="ID",      value=uid,     inline=True)
            embed.add_field(name="Created", value=created, inline=True)
            embed.add_field(name="Status",  value="Not in server", inline=True)
        else:
            await self._send(interaction, "Could not find that member. Try their **User ID**.")
            return

        embed.add_field(
            name="Summary",
            value=(
                f"Warns: **{sum(1 for r in infractions if r['action'] == 'warn')}** | "
                f"Timeouts: **{sum(1 for r in infractions if r['action'] == 'timeout')}** | "
                f"Kicks: **{sum(1 for r in infractions if r['action'] == 'kick')}** | "
                f"Bans: **{sum(1 for r in infractions if r['action'] in ('ban', 'softban'))}**"
            ),
            inline=False,
        )
        if infractions:
            lines = [
                f"**{r['action'].upper()}** ({r['timestamp'][:10]}) - {r['reason']} *(by {r['mod']})*"
                for r in infractions[-5:]
            ]
            embed.add_field(name=f"Recent Infractions ({len(infractions)} total)", value="\n".join(lines), inline=False)
        if notes:
            lines = [f"`{n['timestamp'][:10]}` - {n['reason']} *(by {n['mod']})*" for n in notes]
            embed.add_field(name=f"Notes ({len(notes)})", value="\n".join(lines), inline=False)
        appeal = data.get("appeals", {}).get(uid)
        if appeal:
            embed.add_field(name="Appeal", value=f"**{appeal['status'].upper()}** | {appeal['submitted_at'][:10]}", inline=False)
        embed.set_footer(text=f"Requested by {interaction.user}")
        await self._send(interaction, embed=embed)

    @app_commands.command(name="appeal", description="Submit a ban appeal (once every 3 months)")
    async def appeal(self, interaction: discord.Interaction) -> None:
        if self.bot.config.appeal_server_id and interaction.guild_id != self.bot.config.appeal_server_id:
            await self._send(interaction, "This command can only be used in the **Appeal Server**.")
            return
        await interaction.response.send_modal(AppealModal())

    @app_commands.command(name="clearappeal", description="Clear a user's appeal record (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def clearappeal(self, interaction: discord.Interaction, user_id: str) -> None:
        data = load_mod()
        if user_id in data.get("appeals", {}):
            del data["appeals"][user_id]
            save_mod(data)
            await self._send(interaction, f"Cleared appeal for `{user_id}`.")
        else:
            await self._send(interaction, f"No appeal found for `{user_id}`.")

    # -------------------------------------------------------------------------
    #  TEAM COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="create-team", description="Create a new league team (Admin only)")
    @app_commands.describe(team_name="Name of the team", hex_code="Role colour in hex format (e.g. #ff8800)", profile_picture="Role icon to use", team_captain="Captain that will lead the team")
    async def create_team(self, interaction: discord.Interaction, team_name: str, hex_code: str, team_captain: discord.Member, profile_picture: Optional[discord.Attachment] = None) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Only administrators can create teams.")
            return

        hex_code = hex_code.strip().lstrip("#")
        if len(hex_code) not in {6, 8}:
            await self._send(interaction, "Hex codes must be 6 or 8 characters.")
            return

        guild = interaction.guild
        await interaction.response.defer(ephemeral=True, thinking=True)

        colour = discord.Colour(int(hex_code, 16))
        role = await guild.create_role(name=team_name, colour=colour, reason="New league team")

        icon_url = None
        notes: List[str] = []

        if profile_picture:
            icon_url = profile_picture.url
            try:
                await role.edit(display_icon=await profile_picture.read())
            except discord.Forbidden:
                notes.append("Role icon could not be applied - server needs to be Level 2.")
            except discord.HTTPException:
                notes.append("Role icon could not be applied due to a Discord error.")

        try:
            team = self.bot.team_manager.create_team(
                name=team_name, hex_color=f"#{hex_code}", role_id=role.id,
                captain_id=team_captain.id, icon_url=icon_url,
            )
        except ValueError as exc:
            await role.delete(reason="Rolling back team creation")
            await self._send(interaction, str(exc))
            return

        team_role = role
        captain_role = guild.get_role(self.bot.config.captain_role_id) if self.bot.config.captain_role_id else None
        member_role = guild.get_role(self.bot.config.team_member_role_id) if self.bot.config.team_member_role_id else None

        if not captain_role:
            notes.append(f"Captain role not assigned — CAPTAIN_ROLE_ID ({self.bot.config.captain_role_id}) is not set or does not exist in this server.")
        if not member_role:
            notes.append(f"Team member role not assigned — TEAM_MEMBER_ROLE_ID ({self.bot.config.team_member_role_id}) is not set or does not exist in this server.")

        for r, reason in [
            (team_role,    "Team captain assigned — team role"),
            (captain_role, "Team captain assigned — captain role"),
            (member_role,  "Team captain assigned — member role"),
        ]:
            msg = await _safe_add_role(team_captain, r, reason=reason)
            if msg:
                notes.append(msg)

        message = f"Team **{team.name}** created with captain {team_captain.mention}!"
        if notes:
            message = "\n".join([message, *notes])
        await self._send(interaction, message)
        await self.bot.log_event(guild, f"## New Team Created!\n\n- Team Name: {role.mention}\n- Team Captain: {team_captain.mention}")

    @app_commands.command(name="manage-team", description="Manage your team roster")
    async def manage_team(self, interaction: discord.Interaction) -> None:
        team = self.bot.team_manager.find_team_for_member(interaction.user.id)
        if not team:
            await self._send(interaction, "You are not a member of any team.")
            return

        is_captain = interaction.user.id == team.captain_id
        is_co_captain = interaction.user.id in team.co_captains
        is_admin = self.bot.require_admin(interaction)

        if not (is_captain or is_co_captain or is_admin):
            await self._send(interaction, "You are not authorised to manage this team.")
            return

        guild = interaction.guild
        view = ManageTeamView(
            interaction=interaction, team=team, manager=self.bot.team_manager, bot=self.bot,
            is_admin=is_admin, roster_locked=self.bot.team_manager.roster_locked,
            can_invite=not self.bot.team_manager.roster_locked,
            captain_role=guild.get_role(self.bot.config.captain_role_id),
            co_captain_role=guild.get_role(self.bot.config.co_captain_role_id),
            member_role=guild.get_role(self.bot.config.team_member_role_id),
        )
        await self._send(interaction, embed=build_team_embed(team, guild), view=view)

    @app_commands.command(name="roster", description="Browse rosters for any team")
    async def roster(self, interaction: discord.Interaction) -> None:
        teams = sorted(self.bot.team_manager.all_teams(), key=lambda t: t.name.lower())
        if not teams:
            await self._send(interaction, "No teams have been created yet.")
            return
        view = RosterLookupView(interaction=interaction, teams=teams)
        await self._send(interaction, embed=build_team_embed(view.current_team, interaction.guild), view=view)

    @app_commands.command(name="leave", description="Leave your current team")
    async def leave_team(self, interaction: discord.Interaction) -> None:
        team = self.bot.team_manager.find_team_for_member(interaction.user.id)
        if not team:
            await self._send(interaction, "You are not on a roster.")
            return
        if interaction.user.id == team.captain_id:
            await self._send(interaction, "Captains must transfer or disband their team first.")
            return

        confirmed = await prompt_confirmation(interaction, f"Leave **{team.name}**? This will remove your team role.")
        if not confirmed:
            return

        self.bot.team_manager.remove_member(team, interaction.user.id)
        guild = interaction.guild
        notes: List[str] = []

        for role_id in (team.role_id, self.bot.config.co_captain_role_id, self.bot.config.team_member_role_id):
            role = guild.get_role(role_id) if role_id else None
            msg = await _safe_remove_role(interaction.user, role, reason="Left team")
            if msg:
                notes.append(msg)

        message = f"You have left **{team.name}**."
        if notes:
            message = "\n".join([message, *dict.fromkeys(notes)])
        await interaction.followup.send(message, ephemeral=True)
        await self.bot.log_event(guild, f"{interaction.user.mention} has left **{team.name}**")

    @app_commands.command(name="admin-edit", description="Admin: edit a team's settings")
    @app_commands.autocomplete(team_name=_team_autocomplete)
    @app_commands.describe(team_name="Team to edit", new_name="New team name", new_hex="Updated hex colour (e.g. #00ff00)", new_logo="Upload a new role icon", new_captain="Transfer captaincy")
    async def admin_edit(self, interaction: discord.Interaction, team_name: str, new_name: Optional[str] = None,
                         new_hex: Optional[str] = None, new_logo: Optional[discord.Attachment] = None,
                         new_captain: Optional[discord.Member] = None) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return
        team = self.bot.team_manager.get_team(team_name)
        if not team:
            await self._send(interaction, "Team not found.")
            return

        role = interaction.guild.get_role(team.role_id)
        if new_name:
            if role:
                await role.edit(name=new_name)
            self.bot.team_manager.rename(team, new_name)
        if new_hex:
            hex_val = new_hex.strip().lstrip("#")
            if role:
                await role.edit(colour=discord.Colour(int(hex_val, 16)))
            self.bot.team_manager.set_hex(team, f"#{hex_val}")
        if new_logo:
            if role:
                await role.edit(display_icon=await new_logo.read())
            self.bot.team_manager.set_icon_url(team, new_logo.url)
        if new_captain:
            old_cap_id = team.captain_id
            try:
                self.bot.team_manager.set_captain(team, new_captain.id)
            except ValueError as exc:
                await self._send(interaction, str(exc))
                return
            cap_role = interaction.guild.get_role(self.bot.config.captain_role_id)
            old_cap = interaction.guild.get_member(old_cap_id)
            if old_cap and cap_role:
                await old_cap.remove_roles(cap_role, reason="Captaincy transferred")
            if cap_role:
                await new_captain.add_roles(cap_role, reason="Captaincy granted")
            if role:
                await new_captain.add_roles(role)
            member_role = interaction.guild.get_role(self.bot.config.team_member_role_id)
            if member_role:
                await new_captain.add_roles(member_role)

        await self._send(interaction, "Team updated successfully.")

    @app_commands.command(name="admin-manage", description="Admin: manage any team")
    @app_commands.autocomplete(team_name=_team_autocomplete)
    async def admin_manage(self, interaction: discord.Interaction, team_name: str) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return
        team = self.bot.team_manager.get_team(team_name)
        if not team:
            await self._send(interaction, "Team not found.")
            return

        guild = interaction.guild
        view = ManageTeamView(
            interaction=interaction, team=team, manager=self.bot.team_manager, bot=self.bot,
            is_admin=True, roster_locked=self.bot.team_manager.roster_locked, can_invite=True,
            allow_force_add=True, captain_role=guild.get_role(self.bot.config.captain_role_id),
            co_captain_role=guild.get_role(self.bot.config.co_captain_role_id),
            member_role=guild.get_role(self.bot.config.team_member_role_id),
        )
        await self._send(interaction, embed=build_team_embed(team, guild), view=view)

    @app_commands.command(name="admin-lock", description="Admin: toggle roster lock")
    async def admin_lock(self, interaction: discord.Interaction) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return
        locked = not self.bot.team_manager.roster_locked
        self.bot.team_manager.set_roster_locked(locked)
        state = "## 🔒 Roster Lock ENABLED" if locked else "## 🔓 Roster Lock DISABLED"
        await self.bot.log_event(interaction.guild, state)
        await self._send(interaction, state)

    @app_commands.command(name="admin-disband-all", description="Admin: disband every team (irreversible)")
    async def admin_disband_all(self, interaction: discord.Interaction) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return
        teams = list(self.bot.team_manager.all_teams())
        if not teams:
            await self._send(interaction, "There are no teams to disband.")
            return

        for step, prompt in enumerate([
            "This will delete every team, role, and roster entry. Confirm (1/3).",
            "Second confirmation required (2/3).",
            "Final confirmation (3/3). This **cannot** be undone.",
        ], 1):
            view = ConfirmView()
            if step == 1:
                await self._send(interaction, prompt, view=view)
            else:
                await interaction.followup.send(prompt, view=view, ephemeral=True)
            await view.wait()
            if not view.value:
                return

        guild = interaction.guild
        captain_role = guild.get_role(self.bot.config.captain_role_id)
        co_captain_role = guild.get_role(self.bot.config.co_captain_role_id)
        member_role = guild.get_role(self.bot.config.team_member_role_id)

        disbanded, notes = 0, []
        for team in teams:
            disbanded += 1
            team_role = guild.get_role(team.role_id)
            msg = await _safe_delete_role(team_role, reason="Admin disband-all")
            if msg:
                notes.append(f"{team.name}: {msg}")

            all_member_ids = {team.captain_id, *team.co_captains, *team.members}
            for uid in all_member_ids:
                m = guild.get_member(uid)
                if not m:
                    try:
                        m = await guild.fetch_member(uid)
                    except discord.HTTPException:
                        continue
                for role, cond in [
                    (team_role,       True),
                    (captain_role,    uid == team.captain_id),
                    (co_captain_role, uid in team.co_captains),
                    (member_role,     True),
                ]:
                    if cond:
                        msg = await _safe_remove_role(m, role, reason="Admin disband-all")
                        if msg:
                            notes.append(f"{team.name}: {msg}")

            await self.bot.log_event(guild, f"## 🔴 Team Disbanded\n> **{team.name}** has been disbanded.")
            self.bot.team_manager.delete_team(team.name)

        summary = f"Disbanded **{disbanded}** team(s)."
        if notes:
            summary = "\n".join([summary, *dict.fromkeys(notes)])
        await interaction.followup.send(summary, ephemeral=True)

    # -------------------------------------------------------------------------
    #  MATCH COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="admin-create-match", description="Admin: create a match channel for two teams")
    @app_commands.autocomplete(team_one=_team_autocomplete, team_two=_team_autocomplete)
    @app_commands.describe(team_one="Home team", team_two="Away team", type="Match type", week="Week number")
    @app_commands.choices(type=[
        app_commands.Choice(name="Seeding", value="seeding"),
        app_commands.Choice(name="Bracket", value="bracket"),
    ])
    async def admin_create_match(self, interaction: discord.Interaction, team_one: str, team_two: str,
                                  type: app_commands.Choice[str], week: Optional[int] = None) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return

        guild = interaction.guild
        if team_one.lower() == team_two.lower():
            await self._send(interaction, "Pick two different teams.")
            return

        category = self.bot._match_category(guild)
        if not category:
            await self._send(interaction, "Set MATCH_CATEGORY_ID to a valid category first.")
            return

        t1 = self.bot.team_manager.get_team(team_one)
        t2 = self.bot.team_manager.get_team(team_two)
        if not t1 or not t2:
            await self._send(interaction, "Both teams must exist before scheduling a match.")
            return

        match_type = type.value
        days = 6 if match_type == "seeding" else 7
        due_at = datetime.datetime.utcnow() + timedelta(days=days)
        _, _, default_week = self.bot._week_window()
        week_number = week or default_week

        channel_name = (
            f"{t1.name.lower().replace(' ', '-')}"
            f"-vs-"
            f"{t2.name.lower().replace(' ', '-')}"
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for team in (t1, t2):
            role = guild.get_role(team.role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=channel_name, category=category, overwrites=overwrites, reason="New league match",
            topic=f"Week {week_number} | {match_type.capitalize()} match | Due by {due_at.date().isoformat()}",
        )
        match = self.bot.match_manager.create_match(
            team_one=t1.name, team_two=t2.name, channel_id=channel.id,
            due_at=due_at, week=week_number, match_type=match_type,
        )
        due_ts = int(due_at.replace(tzinfo=timezone.utc).timestamp())

        if match_type == "seeding":
            opening_message = (
                f"# Welcome your *Official Pro For All Seeding* match!\n"
                f"__Here are the ground rules:__\n"
                f"> **- 6 days in total to schedule and play this match.\n"
                f"> - If you fail to play this match by the specified deadline, you will be forfeited.\n"
                f"> - Your team may only be forfeited 2 times in this season before your team gets disbanded.\n"
                f"> - We may disband your team early if you show no signs of scheduling.**\n"
                f"***If you have any questions let us know!***"
            )
        else:
            opening_message = (
                f"# Welcome your *Official Pro For All Bracket* match!\n"
                f"__Here are the ground rules:__\n"
                f"> **- 7 days in total to schedule and play this match.\n"
                f"> - If you fail to play this match by the specified deadline, you will be forfeited.\n"
                f"> - Your team may only be forfeited 2 times in this season before your team gets disbanded.\n"
                f"> - We may disband your team early if you show no signs of scheduling.**\n"
                f"***Congratulations on making it this far!***\n"
                f"***If you have any questions let us know!***"
            )

        await channel.send(content=f"{self.bot._team_ping(guild, t1.name)} {self.bot._team_ping(guild, t2.name)}")
        await channel.send(opening_message)
        await channel.send(
            content=f"**Due:** <t:{due_ts}:F> | **Week:** {week_number} | **Match ID:** {match.id}\n"
                    f"Use `/submit-time` to propose a match time. Use `/admin-submit-scores` to report results (Staff only)."
        )
        await self._send(interaction, f"Match channel created: {channel.mention}")

    @app_commands.command(name="submit-time", description="Submit your scheduled match time")
    @app_commands.describe(time="Time of the match (e.g. 9:00 PM EST)", date="Date of the match in MM/DD/YY format (e.g. 03/25/26)")
    async def submit_time(self, interaction: discord.Interaction, time: str, date: str) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self._send(interaction, "Run this inside your match channel.")
            return

        match = self.bot.match_manager.find_by_channel(channel.id)
        if not match or match.status != "open":
            await self._send(interaction, "This channel is not tied to an open match.")
            return

        team = self.bot.team_manager.find_team_for_member(interaction.user.id)
        if not team or team.name not in {match.team_one, match.team_two}:
            await self._send(interaction, "Only players on this match's teams can submit the time.")
            return
        if interaction.user.id not in (team.captain_id, *team.co_captains):
            await self._send(interaction, "Only captains and co-captains can submit the time.")
            return

        import re
        date = date.strip()
        time = time.strip()
        if not re.fullmatch(r"\d{2}/\d{2}/\d{2}", date):
            await self._send(interaction, "Date must be in **MM/DD/YY** format (e.g. `03/25/26`).")
            return
        if not time:
            await self._send(interaction, "Please include a time (e.g. `9:00 PM EST`).")
            return

        scheduled = f"{time} — {date}"

        if match.scheduled_time and not match.scheduled_confirmed:
            async def _confirm(inter: discord.Interaction) -> None:
                assignments_channel = guild.get_channel(self.bot.config.match_assignments_channel_id) if self.bot.config.match_assignments_channel_id else None
                if not isinstance(assignments_channel, discord.TextChannel):
                    await self._send(inter, "Set MATCH_ASSIGNMENTS_CHANNEL_ID first.")
                    return
                self.bot.match_manager.set_scheduled_time(match, scheduled_time=match.scheduled_time, confirmed=True)
                post = (
                    f"**{match.team_one} vs {match.team_two}**\n"
                    f"**{match.scheduled_time}**\n"
                    f"- Referee: \n- Caster: \n- Commentator: "
                )
                await assignments_channel.send(post)
                await channel.send(f"Match time confirmed: **{match.scheduled_time}**")
                await self._send(inter, "Time confirmed and posted for staff claims.")

            async def _change(inter: discord.Interaction) -> None:
                self.bot.match_manager.set_scheduled_time(match, scheduled_time=None, confirmed=False)
                await self._send(inter, "Time cleared. Run `/submit-time` again with the new proposal.")

            view = ConfirmTimeView(scheduled_time=match.scheduled_time, on_confirm=_confirm, on_change=_change)
            await self._send(interaction, f"Current match time: **{match.scheduled_time}**. Confirm or change?", view=view)
            return

        self.bot.match_manager.set_scheduled_time(match, scheduled_time=scheduled, confirmed=False)
        await channel.send(
            f"Proposed match time from {interaction.user.mention}: **{scheduled}**.\n"
            f"{self.bot._team_ping(guild, match.team_one)} {self.bot._team_ping(guild, match.team_two)} "
            "- have the opposing captain run `/submit-time` here to confirm."
        )
        await self._send(interaction, "Time saved. Ask the other captain to confirm it.")

    @app_commands.command(name="admin-submit-scores", description="Admin: submit match scores")
    @app_commands.describe(team_one_score="Score for the first team (0-5)", team_two_score="Score for the second team (0-5)")
    async def admin_submit_scores(self, interaction: discord.Interaction,
                                   team_one_score: app_commands.Range[int, 0, 5],
                                   team_two_score: app_commands.Range[int, 0, 5]) -> None:
        if not self.bot.require_admin(interaction):
            await self._send(interaction, "Administrator permissions required.")
            return
        guild = interaction.guild
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self._send(interaction, "Run this inside a match channel.")
            return
        match = self.bot.match_manager.find_by_channel(channel.id)
        if not match or match.status != "open":
            await self._send(interaction, "This channel is not tied to an open match.")
            return
        if max(team_one_score, team_two_score) != 5 or team_one_score == team_two_score:
            await self._send(interaction, "First to 5 only: one side must reach 5 and scores cannot tie.")
            return

        final_scores = {match.team_one: team_one_score, match.team_two: team_two_score}
        self.bot.match_manager.mark_completed(match, scores=final_scores, rounds=[])
        await self.bot._lock_match_channel(channel, match)

        winner = match.team_one if team_one_score > team_two_score else match.team_two
        loser = match.team_two if winner == match.team_one else match.team_one

        await self.bot._post_results(
            interaction.guild, winner=winner, loser=loser,
            score_one=team_one_score, score_two=team_two_score,
            team_one=match.team_one, team_two=match.team_two, match_type=match.match_type,
        )
        await channel.send(f"Scores submitted: **{match.team_one} {team_one_score} - {team_two_score} {match.team_two}**")

        staff_pings = " ".join(
            guild.get_role(rid).mention
            for rid in self.bot.config.staff_role_ids
            if guild.get_role(rid)
        )
        close_view = _MatchCloseView()
        await channel.send(
            f"{staff_pings}\nScores have been submitted. Please review and close this channel when ready.",
            view=close_view,
        )

        challonge_note = await self.bot._report_challonge(match, final_scores)
        response = "Scores submitted and results posted."
        if challonge_note:
            response = "\n".join([response, challonge_note])
        await self._send(interaction, response)

    @app_commands.command(name="getschedules", description="View all upcoming match schedules")
    async def getschedules(self, interaction: discord.Interaction) -> None:
        scheduled = [m for m in self.bot.match_manager.open_matches() if m.scheduled_time]
        if not scheduled:
            await self._send(interaction, "No scheduled matches yet.")
            return
        embed = discord.Embed(title="Upcoming Match Schedules", color=0x0099FF)
        for m in scheduled:
            value = f"**{m.scheduled_time}**"
            if m.scheduled_confirmed:
                value += " - Confirmed"
            embed.add_field(name=f"{m.team_one} vs {m.team_two} (Week {m.week})", value=value, inline=False)
        await self._send(interaction, embed=embed)

    @app_commands.command(name="match-history", description="View official Pro For All match results")
    @app_commands.describe(type="Filter by match type (optional)")
    @app_commands.choices(type=[
        app_commands.Choice(name="Seeding", value="seeding"),
        app_commands.Choice(name="Bracket", value="bracket"),
    ])
    async def match_history(self, interaction: discord.Interaction, type: Optional[app_commands.Choice[str]] = None) -> None:
        all_matches = list(self.bot.match_manager.all_matches())
        completed = [m for m in all_matches if m.status == "completed"]

        if type:
            completed = [m for m in completed if m.match_type == type.value]

        if not completed:
            label = f" ({type.name})" if type else ""
            await self._send(interaction, f"No completed matches found{label}.")
            return

        type_label = type.name if type else "All"
        embed = discord.Embed(title=f"Official Pro For All Match History — {type_label}", color=0x5865F2)

        for m in completed[-20:]:
            s1 = m.scores.get(m.team_one, 0)
            s2 = m.scores.get(m.team_two, 0)
            winner = m.team_one if s1 > s2 else m.team_two
            score_str = f"**{m.team_one}** {s1} — {s2} **{m.team_two}**"
            match_type_label = m.match_type.capitalize()
            embed.add_field(
                name=f"Week {m.week} — {m.team_one} vs {m.team_two} [{match_type_label}]",
                value=f"{score_str}\nWinner: **{winner}**",
                inline=False,
            )

        embed.set_footer(text=f"Showing {len(completed)} result(s)")
        await self._send(interaction, embed=embed)

    # -------------------------------------------------------------------------
    #  EDIT MATCH COMMAND
    # -------------------------------------------------------------------------

    @app_commands.command(name="edit-match", description="Staff: assign caster and/or ref to a match")
    @app_commands.describe(match_id="The Match ID shown in the match channel")
    async def edit_match(self, interaction: discord.Interaction, match_id: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "Staff only.")
            return

        match = self.bot.match_manager._matches.get(match_id)
        if not match:
            await self._send(interaction, f"No match found with ID `{match_id}`.")
            return

        if match.status != "open":
            await self._send(interaction, "That match is already completed or overdue.")
            return

        view = AssignStaffView(bot=self.bot, match=match, guild=interaction.guild)
        embed = discord.Embed(
            title=f"Assign Staff — {match.team_one} vs {match.team_two}",
            description="Use the selector(s) below to assign caster and/or referee.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Week", value=str(match.week), inline=True)
        embed.add_field(name="Type", value=match.match_type.capitalize(), inline=True)
        if match.scheduled_time:
            embed.add_field(name="Scheduled Time", value=match.scheduled_time, inline=False)
        await self._send(interaction, embed=embed, view=view)

    # -------------------------------------------------------------------------
    #  AI COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="aiexample", description="Add a Q&A training example to the AI (Staff only)")
    @app_commands.describe(question="The question", answer="The correct answer")
    async def aiexample(self, interaction: discord.Interaction, question: str, answer: str) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "Staff only.")
            return
        data = load_training()
        data.append({"question": question, "answer": answer, "added_by": str(interaction.user), "added_at": discord.utils.utcnow().isoformat()})
        save_training(data)
        embed = discord.Embed(title="AI Training Example Added", color=discord.Color.green())
        embed.add_field(name="Question", value=question, inline=False)
        embed.add_field(name="Answer",   value=answer,   inline=False)
        embed.set_footer(text=f"Total training examples: {len(data)}")
        await self._send(interaction, embed=embed)

    @app_commands.command(name="ailist", description="View all AI training examples (Staff only)")
    async def ailist(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "Staff only.")
            return
        data = load_training()
        if not data:
            await self._send(interaction, "No training examples yet.")
            return
        embed = discord.Embed(title="AI Training Examples", description=f"Total: **{len(data)}** - showing last 10", color=discord.Color.purple())
        for i, ex in enumerate(data[-10:], start=max(1, len(data) - 9)):
            q = ex["question"][:60] + ("..." if len(ex["question"]) > 60 else "")
            a = ex["answer"][:100]  + ("..." if len(ex["answer"]) > 100 else "")
            embed.add_field(name=f"#{i} - {q}", value=f"{a}\n*Added by {ex.get('added_by', 'Unknown')}*", inline=False)
        await self._send(interaction, embed=embed)

    @app_commands.command(name="aidelete", description="Delete an AI training example by number (Staff only)")
    @app_commands.describe(index="The example number shown in /ailist")
    async def aidelete(self, interaction: discord.Interaction, index: int) -> None:
        if not self.bot.is_staff(interaction):
            await self._send(interaction, "Staff only.")
            return
        data = load_training()
        if not data:
            await self._send(interaction, "No training examples to delete.")
            return
        if index < 1 or index > len(data):
            await self._send(interaction, f"Invalid index. Range: **1 - {len(data)}**")
            return
        removed = data.pop(index - 1)
        save_training(data)
        embed = discord.Embed(title="Training Example Deleted", color=discord.Color.red())
        embed.add_field(name="Removed Question", value=removed["question"], inline=False)
        embed.set_footer(text=f"Remaining: {len(data)}")
        await self._send(interaction, embed=embed)

    @app_commands.command(name="aiclear", description="Wipe ALL AI training data (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def aiclear(self, interaction: discord.Interaction) -> None:
        save_training([])
        self.bot.ticket_ai_history.clear()
        await self._send(interaction, "All AI training data and conversation history cleared.")

    # -------------------------------------------------------------------------
    #  GENERAL COMMANDS
    # -------------------------------------------------------------------------

    @app_commands.command(name="ping", description="Check bot latency")
    async def ping(self, interaction: discord.Interaction) -> None:
        await self._send(interaction, f"Pong! Latency: **{round(self.bot.latency * 1000)}ms**")

    @app_commands.command(name="help", description="View all bot commands")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="Bot Help", description="Select a category from the dropdown below.", color=discord.Color.blurple())
        for category, data in HELP_CATEGORIES.items():
            embed.add_field(name=category, value=data["description"], inline=False)
        embed.set_footer(text="Use the dropdown to browse commands.")
        await self._send(interaction, embed=embed, view=HelpView())

    @app_commands.command(name="forceregister", description="Admin: force re-sync slash commands")
    @app_commands.checks.has_permissions(administrator=True)
    async def forceregister(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            for guild in self.bot.guilds:
                obj = discord.Object(id=guild.id)
                self.bot.tree.copy_global_to(guild=obj)
                await self.bot.tree.sync(guild=obj)
            await interaction.followup.send("Commands force re-synced!", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed: {exc}", ephemeral=True)

    # -------------------------------------------------------------------------
    #  BACKGROUND TASK
    # -------------------------------------------------------------------------

    @tasks.loop(minutes=30)
    async def _reminder_loop(self) -> None:
        await self.bot.wait_until_ready()
        now = datetime.datetime.utcnow()

        for match in self.bot.match_manager.open_matches():
            if now < match.due_datetime():
                continue

            channel = self.bot.get_channel(match.channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue

            guild = channel.guild
            self.bot.match_manager.mark_overdue(match)

            if not channel.name.startswith("overdue-"):
                try:
                    await channel.edit(name=f"overdue-{channel.name}")
                except discord.HTTPException:
                    pass

            mod_ping = ""
            if self.bot.config.mod_role_id:
                role = guild.get_role(self.bot.config.mod_role_id)
                if role:
                    mod_ping = f" {role.mention}"

            await channel.send(
                f"No score was reported before the weekly deadline. "
                f"{self.bot._team_ping(guild, match.team_one)} vs "
                f"{self.bot._team_ping(guild, match.team_two)}{mod_ping}"
            )


# =============================================================================
#  PUNISHMENT MODAL
# =============================================================================

class PunishModal(discord.ui.Modal, title="Confirm Punishment"):
    reason   = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)
    duration = discord.ui.TextInput(
        label="Duration (e.g. 10m, 2h, 3d, 1mo, permanent)",
        placeholder="Leave blank for permanent",
        required=False,
    )

    def __init__(
        self,
        action: str,
        member: discord.Member,
        bot: "LeagueBot",
        prefill_reason: str = "",
        prefill_duration: str = "",
    ) -> None:
        super().__init__()
        self.action = action
        self.member = member
        self.bot = bot
        if prefill_reason:
            self.reason.default = prefill_reason
        if prefill_duration:
            self.duration.default = prefill_duration

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason.value.strip()
        raw_dur = self.duration.value.strip() or "permanent"
        dur_delta = parse_duration(raw_dur)
        dur_str = raw_dur if not dur_delta else str(dur_delta)
        action, member, guild = self.action, self.member, interaction.guild

        try:
            if action == "warn":
                dm = discord.Embed(title="You have been warned in Pro For All", color=discord.Color.red())
                dm.add_field(name="Reason", value=reason, inline=False)
                try:
                    await member.send(embed=dm)
                except discord.Forbidden:
                    pass

            elif action == "timeout":
                if not dur_delta:
                    await interaction.response.send_message("Timeout requires a finite duration.", ephemeral=True)
                    return
                await member.timeout(dur_delta, reason=reason)
                expiry_ts = int((discord.utils.utcnow() + dur_delta).timestamp())
                dm = discord.Embed(title="You have been timed out!", color=discord.Color.red())
                dm.description = f"**Reason:** {reason}\nExpires: <t:{expiry_ts}:F>."
                try:
                    await member.send(embed=dm)
                except discord.Forbidden:
                    pass

            elif action == "kick":
                dm = discord.Embed(title="You have been kicked!", color=discord.Color.red())
                dm.description = f"**Reason:** {reason}"
                try:
                    await member.send(embed=dm)
                except discord.Forbidden:
                    pass
                await member.kick(reason=reason)

            elif action == "ban":
                dm = discord.Embed(title="You have been banned!", color=discord.Color.red())
                dur_line = (
                    "This punishment is **permanent**."
                    if dur_delta is None
                    else f"Expires: <t:{int((discord.utils.utcnow() + dur_delta).timestamp())}:F>."
                )
                dm.description = (
                    f"**Reason:** {reason}\n{dur_line}\n\n"
                    f"**Appeal:** Join [this server]({self.bot.config.appeal_server_invite}) and run `/appeal`."
                )
                try:
                    await member.send(embed=dm)
                except discord.Forbidden:
                    pass
                await member.ban(reason=reason, delete_message_days=0)

        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to do that.", ephemeral=True)
            return
        except Exception as exc:
            await interaction.response.send_message(f"Error: {exc}", ephemeral=True)
            return

        add_record(member.id, action, reason, str(interaction.user), dur_str)
        await self.bot.send_mod_log(
            guild, action.upper(), discord.Color.red(),
            user=f"{member} (`{member.id}`)",
            moderator=str(interaction.user),
            duration=dur_str,
            reason=reason,
        )
        await interaction.response.send_message(
            f"**{action.upper()}** applied to {member.mention} | {reason}", ephemeral=True
        )


# =============================================================================
#  ENTRY POINT
# =============================================================================

def run_bot() -> None:
    config = BotConfig.from_env()
    bot = LeagueBot(
        config=config,
        data_path=Path("data/teams.json"),
        match_path=Path("data/matches.json"),
    )
    bot.run(config.token)


if __name__ == "__main__":
    run_bot()
