from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger(__name__)

VANTAGE_RED = 0xD7263D
SUPPORT_SERVER_URL = "https://discord.gg/yourserver"
TRANSIENT_FEEDBACK_SECONDS = 15
SETUP_PANEL_TITLE = "Vantage Modlog Setup"
MODLOG_FOOTER_TEXT = "Vantage Moderation"
AUDIT_LOG_SCAN_LIMIT = 8
AUDIT_LOG_WINDOW_SECONDS = 20
MEMBER_UPDATE_ACTIONS: Dict[str, str] = {
    "default": "Member Updated",
    "timeout_added": "Timeout Added",
    "timeout_removed": "Timeout Removed",
    "timeout_updated": "Timeout Updated",
    "roles_updated": "Roles Updated",
    "nickname_updated": "Nickname Updated",
}

EVENT_GROUP_LABELS: Dict[str, str] = {
    "message_events": "Messages",
    "member_events": "Members",
    "moderation_events": "Moderation",
    "channel_events": "Channels",
    "thread_events": "Threads",
    "role_events": "Roles",
    "voice_events": "Voice",
    "invite_events": "Invites",
    "emoji_sticker_events": "Emojis/Stickers",
    "guild_events": "Server",
    "misc_events": "Misc",
}

ENTRY_BUTTON_OPTIONS: Dict[str, str] = {
    "user_id": "User ID",
    "jump_link": "Jump to Message",
    "support_server": "Support Server",
}

DEFAULT_GUILD_SETTINGS: Dict[str, Any] = {
    "setup_complete": False,
    "log_channel_id": None,
    "entry_buttons": ["user_id", "jump_link"],
    "events": {key: True for key in EVENT_GROUP_LABELS},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def truncate(value: Optional[str], *, limit: int = 900, fallback: str = "Nothing to show.") -> str:
    if value is None:
        return fallback
    cleaned = value.strip()
    if not cleaned:
        return fallback
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 3]}..."


def mention_user(user: discord.abc.User) -> str:
    return f"{user.mention} (`{user.id}`)"


def bool_emoji(value: bool) -> str:
    return "✅" if value else "❌"


def full_and_relative(dt: Optional[datetime]) -> str:
    if dt is None:
        return "Unknown"
    return f"{discord.utils.format_dt(dt, style='F')} ({discord.utils.format_dt(dt, style='R')})"


def action_with_icon(action: str) -> str:
    if "Timeout" in action:
        return f"⏱️ {action}"
    if "Banned" in action:
        return f"⛔ {action}"
    if "Unbanned" in action:
        return f"✅ {action}"
    if "Deleted" in action:
        return f"🗑️ {action}"
    if "Created" in action:
        return f"✨ {action}"
    if "Updated" in action:
        return f"🛠️ {action}"
    if "Joined" in action:
        return f"👋 {action}"
    if "Left" in action or "Kicked" in action:
        return f"🚪 {action}"
    return action


class UserIdButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, cog: "VantageModlog", user_id: int):
        super().__init__(label="User ID", style=discord.ButtonStyle.secondary)
        self.cog = cog
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        content = f"User ID: `{self.user_id}`"
        await self.cog.send_transient_interaction_message(interaction, content)


class LogEntryActionsView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: "VantageModlog",
        selected_buttons: Sequence[str],
        target_user_id: Optional[int],
        jump_url: Optional[str],
    ):
        super().__init__(timeout=180)

        if "user_id" in selected_buttons and target_user_id:
            self.add_item(UserIdButton(cog, target_user_id))

        if "jump_link" in selected_buttons and jump_url:
            self.add_item(
                discord.ui.Button(
                    label="Jump",
                    style=discord.ButtonStyle.link,
                    url=jump_url,
                )
            )

        if "support_server" in selected_buttons:
            self.add_item(
                discord.ui.Button(
                    label="Support Server",
                    style=discord.ButtonStyle.link,
                    url=SUPPORT_SERVER_URL,
                )
            )


class LogChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: "ModlogSetupView"):
        super().__init__(
            placeholder="Choose your modlog channel",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.values:
            await self.parent_view.cog.send_transient_interaction_message(
                interaction,
                "Please choose a channel.",
            )
            return

        channel = self.values[0]
        await self.parent_view.cog.config.guild(self.parent_view.guild).log_channel_id.set(channel.id)

        settings = await self.parent_view.cog.get_settings(self.parent_view.guild)
        self.parent_view.sync_finalize_button(settings)
        embed = self.parent_view.cog.build_dashboard_embed(
            self.parent_view.guild,
            settings,
            first_time=not settings["setup_complete"],
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class EventCoverageSelect(discord.ui.Select):
    def __init__(self, parent: "ModlogSetupView", selected: Sequence[str]):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=key in selected,
                description=f"Turn {label.lower()} logging on or off.",
            )
            for key, label in EVENT_GROUP_LABELS.items()
        ]
        super().__init__(
            placeholder="Pick event groups to log",
            min_values=1,
            max_values=len(options),
            options=options,
            row=1,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_keys = set(self.values)
        updated = {
            key: key in selected_keys
            for key in EVENT_GROUP_LABELS
        }
        await self.parent_view.cog.config.guild(self.parent_view.guild).events.set(updated)

        settings = await self.parent_view.cog.get_settings(self.parent_view.guild)
        self.parent_view.rebuild_selects(settings)
        self.parent_view.sync_finalize_button(settings)

        embed = self.parent_view.cog.build_dashboard_embed(
            self.parent_view.guild,
            settings,
            first_time=not settings["setup_complete"],
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class EntryButtonsSelect(discord.ui.Select):
    def __init__(self, parent: "ModlogSetupView", selected: Sequence[str]):
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                default=key in selected,
                description=f"Show the {label.lower()} button on each log entry.",
            )
            for key, label in ENTRY_BUTTON_OPTIONS.items()
        ]
        super().__init__(
            placeholder="Choose log-entry buttons",
            min_values=0,
            max_values=len(options),
            options=options,
            row=2,
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        values = list(self.values)
        await self.parent_view.cog.config.guild(self.parent_view.guild).entry_buttons.set(values)

        settings = await self.parent_view.cog.get_settings(self.parent_view.guild)
        self.parent_view.rebuild_selects(settings)
        embed = self.parent_view.cog.build_dashboard_embed(
            self.parent_view.guild,
            settings,
            first_time=not settings["setup_complete"],
        )
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ModlogSetupView(discord.ui.View):
    def __init__(
        self,
        cog: "VantageModlog",
        guild: discord.Guild,
        author: discord.abc.User,
        *,
        settings: Dict[str, Any],
    ):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild = guild
        self.author_id = author.id
        self.rebuild_selects(settings)
        self.sync_finalize_button(settings)

    def clear_dynamic_items(self) -> None:
        for item in list(self.children):
            if isinstance(item, (LogChannelSelect, EventCoverageSelect, EntryButtonsSelect)):
                self.remove_item(item)

    def rebuild_selects(self, settings: Dict[str, Any]) -> None:
        self.clear_dynamic_items()
        selected_groups = [
            key
            for key, enabled in settings["events"].items()
            if enabled
        ]
        self.add_item(LogChannelSelect(self))
        self.add_item(EventCoverageSelect(self, selected_groups))
        self.add_item(EntryButtonsSelect(self, settings.get("entry_buttons", [])))

    def sync_finalize_button(self, settings: Dict[str, Any]) -> None:
        self.finalize_changes.disabled = settings.get("log_channel_id") is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await self.cog.send_transient_interaction_message(
                interaction,
                "This setup panel is tied to the user who opened `/modlog`.",
            )
            return False

        user = interaction.user
        if isinstance(user, discord.Member) and user.guild_permissions.manage_guild:
            return True

        await self.cog.send_transient_interaction_message(
            interaction,
            "You need the **Manage Server** permission to edit modlog settings.",
        )
        return False

    @discord.ui.button(label="Preview Log Entry", style=discord.ButtonStyle.secondary, row=3)
    async def send_test_entry(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_test_log(self.guild, interaction.user)
        await self.cog.send_transient_interaction_message(
            interaction,
            "Test entry sent to your selected modlog channel.",
        )

    @discord.ui.button(label="Save & Activate", style=discord.ButtonStyle.success, row=4)
    async def finalize_changes(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        settings = await self.cog.get_settings(self.guild)
        if not settings.get("log_channel_id"):
            await self.cog.send_transient_interaction_message(
                interaction,
                "Pick a log channel first.",
            )
            return

        if settings.get("setup_complete"):
            message = "Settings are already active. Your latest changes are saved."
        else:
            await self.cog.config.guild(self.guild).setup_complete.set(True)
            settings["setup_complete"] = True
            message = "First-time setup finished. Vantage Modlog is now active."

        self.sync_finalize_button(settings)
        embed = self.cog.build_dashboard_embed(self.guild, settings, first_time=False)
        await interaction.response.edit_message(embed=embed, view=self)
        await self.cog.send_transient_interaction_message(interaction, message)

    @discord.ui.button(label="Reload Panel", style=discord.ButtonStyle.secondary, row=4)
    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        settings = await self.cog.get_settings(self.guild)
        self.rebuild_selects(settings)
        self.sync_finalize_button(settings)
        embed = self.cog.build_dashboard_embed(
            self.guild,
            settings,
            first_time=not settings.get("setup_complete"),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class VantageModlog(commands.Cog):
    """Vantage branded, interactive, per-server modlog."""

    __author__ = "Vantage"
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8_247_019_663, force_registration=True)
        self.config.register_guild(**copy.deepcopy(DEFAULT_GUILD_SETTINGS))
        self._invite_uses_cache: Dict[int, Dict[str, int]] = {}
        self._vanity_uses_cache: Dict[int, Optional[int]] = {}

    async def get_settings(self, guild: discord.Guild) -> Dict[str, Any]:
        settings = await self.config.guild(guild).all()
        changed = False

        for key, value in DEFAULT_GUILD_SETTINGS.items():
            if key not in settings:
                settings[key] = copy.deepcopy(value)
                changed = True

        for key, value in DEFAULT_GUILD_SETTINGS["events"].items():
            if key not in settings["events"]:
                settings["events"][key] = value
                changed = True

        if changed:
            await self.config.guild(guild).set(settings)

        return settings

    async def send_transient_interaction_message(
        self,
        interaction: discord.Interaction,
        content: str,
    ) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    content,
                    delete_after=TRANSIENT_FEEDBACK_SECONDS,
                )
            else:
                await interaction.response.send_message(
                    content,
                    delete_after=TRANSIENT_FEEDBACK_SECONDS,
                )
        except discord.HTTPException:
            return

    async def refresh_invite_cache(self, guild: discord.Guild) -> None:
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            return

        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return

        self._invite_uses_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

        vanity_uses: Optional[int] = None
        if guild.vanity_url_code:
            try:
                vanity = await guild.vanity_invite()
                vanity_uses = vanity.uses or 0
            except (discord.Forbidden, discord.HTTPException):
                vanity_uses = None
        self._vanity_uses_cache[guild.id] = vanity_uses

    async def detect_join_source(self, guild: discord.Guild) -> Tuple[str, str]:
        me = guild.me
        if me is None or not me.guild_permissions.manage_guild:
            return (
                "Unknown (grant **Manage Server** to detect invite usage).",
                "Unavailable",
            )

        previous_uses = self._invite_uses_cache.get(guild.id)
        if previous_uses is None:
            await self.refresh_invite_cache(guild)
            return ("Unknown (invite cache is warming up).", "Unavailable")

        try:
            invites = await guild.invites()
        except (discord.Forbidden, discord.HTTPException):
            return ("Unknown (could not fetch invites).", "Unavailable")

        invite_match: Optional[discord.Invite] = None
        for invite in invites:
            old_uses = previous_uses.get(invite.code)
            current_uses = invite.uses or 0
            if old_uses is None and current_uses > 0:
                invite_match = invite
                break
            if old_uses is not None and current_uses > old_uses:
                invite_match = invite
                break

        self._invite_uses_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

        if invite_match:
            inviter_text = mention_user(invite_match.inviter) if invite_match.inviter else "Unknown inviter"
            source = f"Invite `{invite_match.code}` by {inviter_text}"
            return source, f"https://discord.gg/{invite_match.code}"

        vanity_code = guild.vanity_url_code
        if vanity_code:
            old_vanity_uses = self._vanity_uses_cache.get(guild.id)
            try:
                vanity_invite = await guild.vanity_invite()
                new_vanity_uses = vanity_invite.uses or 0
                self._vanity_uses_cache[guild.id] = new_vanity_uses
                if old_vanity_uses is not None and new_vanity_uses > old_vanity_uses:
                    return ("Vanity invite", f"https://discord.gg/{vanity_code}")
            except (discord.Forbidden, discord.HTTPException):
                pass

        return ("Unknown (could not determine which invite was used).", "Unavailable")

    def build_dashboard_embed(
        self,
        guild: discord.Guild,
        settings: Dict[str, Any],
        *,
        first_time: bool,
    ) -> discord.Embed:
        color = discord.Color(VANTAGE_RED)
        title = SETUP_PANEL_TITLE
        description_lines = []

        if first_time:
            description_lines.extend(
                [
                    "Welcome to Vantage Modlog.",
                    "Work through the controls below, then click **Save & Activate**.",
                ]
            )
        else:
            description_lines.append("Manage your modlog settings here. Changes save instantly.")

        steps = [
            ("Log channel selected", settings.get("log_channel_id") is not None),
            ("Event coverage selected", any(settings["events"].values())),
            ("Action buttons configured", True),
            ("Setup activated", settings.get("setup_complete", False)),
        ]
        checklist = "\n".join(f"{bool_emoji(done)} {label}" for label, done in steps)

        channel_id = settings.get("log_channel_id")
        channel_text = f"<#{channel_id}>" if channel_id else "Not selected"

        enabled_groups = [
            label for key, label in EVENT_GROUP_LABELS.items() if settings["events"].get(key)
        ]
        enabled_text = ", ".join(enabled_groups) if enabled_groups else "None"

        selected_buttons = [
            ENTRY_BUTTON_OPTIONS[key]
            for key in settings.get("entry_buttons", [])
            if key in ENTRY_BUTTON_OPTIONS
        ]
        button_text = ", ".join(selected_buttons) if selected_buttons else "No buttons selected"

        embed = discord.Embed(
            title=title,
            description="\n".join(description_lines),
            color=color,
            timestamp=now_utc(),
        )
        embed.add_field(name="Setup Progress", value=checklist, inline=False)
        embed.add_field(name="Log Destination", value=channel_text, inline=False)
        embed.add_field(name="Enabled Event Groups", value=truncate(enabled_text, limit=1000), inline=False)
        embed.add_field(
            name="Entry Action Buttons",
            value=truncate(button_text, limit=1000),
            inline=False,
        )
        embed.add_field(
            name="Branding",
            value=(
                f"Log title format: `{guild.name} Modlog`\n"
                f"Support URL: `{SUPPORT_SERVER_URL}`\n"
                f"Footer: `{MODLOG_FOOTER_TEXT}`"
            ),
            inline=False,
        )
        embed.set_footer(text=MODLOG_FOOTER_TEXT)

        return embed

    async def send_setup_panel(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        settings = await self.get_settings(ctx.guild)
        first_time = not settings.get("setup_complete", False)

        view = ModlogSetupView(self, ctx.guild, ctx.author, settings=settings)
        embed = self.build_dashboard_embed(ctx.guild, settings, first_time=first_time)

        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(
                    embed=embed,
                    view=view,
                )
            else:
                await ctx.interaction.response.send_message(
                    embed=embed,
                    view=view,
                )
        else:
            await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="modlog", description="Open the Vantage modlog setup/editor.")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def modlog(self, ctx: commands.Context) -> None:
        """Interactive setup and editing panel for Vantage Modlog."""
        await self.send_setup_panel(ctx)

    async def build_log_embed(
        self,
        guild: discord.Guild,
        *,
        action: str,
        details: str,
        category: str,
        fields: Optional[Iterable[Tuple[str, str, bool]]] = None,
        target_user: Optional[discord.abc.User] = None,
    ) -> Optional[discord.Embed]:
        settings = await self.get_settings(guild)
        if not settings.get("setup_complete"):
            return None
        if not settings["events"].get(category, True):
            return None

        embed = discord.Embed(
            title=f"{guild.name} Modlog • {action_with_icon(action)}",
            description=details,
            color=discord.Color(VANTAGE_RED),
            timestamp=now_utc(),
        )

        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=truncate(value, limit=1024), inline=inline)

        embed.set_footer(text=MODLOG_FOOTER_TEXT)
        if target_user:
            avatar_url = target_user.display_avatar.url
            embed.set_author(name=f"{target_user} ({target_user.id})", icon_url=avatar_url)
            embed.set_thumbnail(url=avatar_url)

        return embed

    async def send_log(
        self,
        guild: discord.Guild,
        *,
        category: str,
        action: str,
        details: str,
        fields: Optional[Iterable[Tuple[str, str, bool]]] = None,
        target_user_id: Optional[int] = None,
        jump_url: Optional[str] = None,
        target_user: Optional[discord.abc.User] = None,
    ) -> None:
        settings = await self.get_settings(guild)
        if not settings.get("setup_complete"):
            return

        if not settings["events"].get(category, True):
            return

        channel_id = settings.get("log_channel_id")
        if not channel_id:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            channel = self.bot.get_channel(channel_id)

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        embed = await self.build_log_embed(
            guild,
            action=action,
            details=details,
            category=category,
            fields=fields,
            target_user=target_user,
        )
        if embed is None:
            return

        view = LogEntryActionsView(
            cog=self,
            selected_buttons=settings.get("entry_buttons", []),
            target_user_id=target_user_id,
            jump_url=jump_url,
        )

        try:
            if view.children:
                await channel.send(embed=embed, view=view)
            else:
                await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def send_test_log(self, guild: discord.Guild, actor: discord.abc.User) -> None:
        await self.send_log(
            guild,
            category="misc_events",
            action="Test Entry",
            details=(
                "Your Vantage modlog setup is working. "
                "This is how real server events will look."
            ),
            fields=[
                ("Triggered By", mention_user(actor), False),
                ("Server", f"{guild.name} (`{guild.id}`)", False),
            ],
            target_user_id=actor.id,
        )

    async def get_recent_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
        *,
        window_seconds: int = AUDIT_LOG_WINDOW_SECONDS,
    ) -> Optional[discord.AuditLogEntry]:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        window = timedelta(seconds=window_seconds)
        try:
            async for entry in guild.audit_logs(limit=AUDIT_LOG_SCAN_LIMIT, action=action):
                if now_utc() - entry.created_at > window:
                    break
                if entry.target and entry.target.id == target_id:
                    if now_utc() - entry.created_at <= window:
                        return entry
        except discord.Forbidden:
            return None
        except discord.HTTPException as exc:
            log.warning(
                "Failed to fetch audit log entries for guild %s (action=%s, target_id=%s): %s",
                guild.id,
                action,
                target_id,
                exc,
            )
            return None

        return None

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            if guild.id not in self._invite_uses_cache:
                await self.refresh_invite_cache(guild)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None:
            return

        if before.content == after.content and before.attachments == after.attachments:
            return

        await self.send_log(
            before.guild,
            category="message_events",
            action="Message Edited",
            details=f"A message by {mention_user(before.author)} was edited in {before.channel.mention}.",
            fields=[
                ("Before", truncate(before.content), False),
                ("After", truncate(after.content), False),
                ("Message ID", str(before.id), True),
            ],
            target_user_id=before.author.id,
            jump_url=after.jump_url,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        attachment_count = len(message.attachments)
        attachment_text = (
            f"{attachment_count} attachment(s) were included."
            if attachment_count
            else "No attachments."
        )

        await self.send_log(
            message.guild,
            category="message_events",
            action="Message Deleted",
            details=f"A message by {mention_user(message.author)} was deleted in {message.channel.mention}.",
            fields=[
                ("Content", truncate(message.content), False),
                ("Attachments", attachment_text, False),
                ("Message ID", str(message.id), True),
            ],
            target_user_id=message.author.id,
        )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None or payload.cached_message is not None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id) or self.bot.get_channel(payload.channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            channel_text = channel.mention
        else:
            channel_text = f"<#{payload.channel_id}>"

        await self.send_log(
            guild,
            category="message_events",
            action="Message Deleted (Uncached)",
            details=f"An uncached message was deleted in {channel_text}.",
            fields=[
                ("Message ID", str(payload.message_id), True),
                ("Author", "Unknown (message was not cached).", False),
            ],
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id) or self.bot.get_channel(payload.channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            channel_text = channel.mention
        else:
            channel_text = f"<#{payload.channel_id}>"

        await self.send_log(
            guild,
            category="message_events",
            action="Bulk Delete (Uncached)",
            details=(
                f"{len(payload.message_ids)} uncached message(s) were deleted in {channel_text}."
            ),
            fields=[("Channel ID", str(payload.channel_id), True)],
        )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None or payload.cached_message is not None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id) or self.bot.get_channel(payload.channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            channel_text = channel.mention
        else:
            channel_text = f"<#{payload.channel_id}>"

        new_content = payload.data.get("content", "No content value in payload.")
        await self.send_log(
            guild,
            category="message_events",
            action="Message Edited (Uncached)",
            details=f"An uncached message was edited in {channel_text}.",
            fields=[
                ("After", truncate(str(new_content)), False),
                ("Message ID", str(payload.message_id), True),
            ],
            jump_url=f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}",
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: Sequence[discord.Message]) -> None:
        if not messages:
            return

        first = messages[0]
        if first.guild is None:
            return

        await self.send_log(
            first.guild,
            category="message_events",
            action="Bulk Delete",
            details=(
                f"{len(messages)} message(s) were deleted in {first.channel.mention}. "
                "Audit logs can help identify who performed the purge."
            ),
            fields=[("Channel", first.channel.mention, False)],
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        join_source, join_link = await self.detect_join_source(member.guild)
        user_type = "Bot" if member.bot else "Human"
        joined_server = full_and_relative(member.joined_at or now_utc())
        await self.send_log(
            member.guild,
            category="member_events",
            action="Member Joined",
            details=f"{mention_user(member)} joined the server.",
            fields=[
                ("Account Type", user_type, True),
                ("Joined Discord", full_and_relative(member.created_at), False),
                ("Joined This Server", joined_server, False),
                ("Join Source", join_source, False),
                ("Join Link", join_link, False),
                ("Avatar", f"[Open Profile Picture]({member.display_avatar.url})", False),
                ("Member Count", str(member.guild.member_count), True),
            ],
            target_user_id=member.id,
            target_user=member,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        fields: List[Tuple[str, str, bool]] = [
            ("Joined This Server", full_and_relative(member.joined_at), False),
            ("Joined Discord", full_and_relative(member.created_at), False),
            ("Account Type", "Bot" if member.bot else "Human", True),
            ("Avatar", f"[Open Profile Picture]({member.display_avatar.url})", False),
        ]

        action = "Member Left"
        details = f"{mention_user(member)} left the server."

        kick_entry = await self.get_recent_audit_entry(
            member.guild,
            discord.AuditLogAction.kick,
            member.id,
        )
        if kick_entry:
            action = "Member Kicked"
            details = f"{mention_user(member)} was removed from the server."
            moderator = mention_user(kick_entry.user) if kick_entry.user else "Unknown moderator"
            fields.append(("Moderator", moderator, False))
            fields.append(("Reason", kick_entry.reason or "No reason provided.", False))

        await self.send_log(
            member.guild,
            category="member_events",
            action=action,
            details=details,
            fields=fields,
            target_user_id=member.id,
            target_user=member,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        changes: List[Tuple[str, str, bool]] = []
        timeout_changed = False

        if before.nick != after.nick:
            changes.append(("Nickname", f"`{before.nick or 'None'}` → `{after.nick or 'None'}`", False))

        before_roles = {role.id for role in before.roles}
        after_roles = {role.id for role in after.roles}
        added = after_roles - before_roles
        removed = before_roles - after_roles

        if added:
            added_roles = [f"<@&{role_id}>" for role_id in sorted(added)]
            changes.append(("Roles Added", ", ".join(added_roles), False))

        if removed:
            removed_roles = [f"<@&{role_id}>" for role_id in sorted(removed)]
            changes.append(("Roles Removed", ", ".join(removed_roles), False))

        before_timeout = before.timed_out_until
        after_timeout = after.timed_out_until
        if before_timeout != after_timeout:
            timeout_changed = True
            before_text = discord.utils.format_dt(before_timeout, style="F") if before_timeout else "Not timed out"
            after_text = discord.utils.format_dt(after_timeout, style="F") if after_timeout else "Not timed out"
            changes.append(("Timeout", f"{before_text} → {after_text}", False))

        if not changes:
            return

        action = MEMBER_UPDATE_ACTIONS["default"]
        if timeout_changed:
            if before_timeout is None and after_timeout is not None:
                action = MEMBER_UPDATE_ACTIONS["timeout_added"]
            elif before_timeout is not None and after_timeout is None:
                action = MEMBER_UPDATE_ACTIONS["timeout_removed"]
            else:
                action = MEMBER_UPDATE_ACTIONS["timeout_updated"]
        elif added or removed:
            action = MEMBER_UPDATE_ACTIONS["roles_updated"]
        elif before.nick != after.nick:
            action = MEMBER_UPDATE_ACTIONS["nickname_updated"]

        audit_entry = await self.get_recent_audit_entry(
            after.guild,
            discord.AuditLogAction.member_update,
            after.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            changes.append(("Moderator", moderator, False))
            if audit_entry.reason:
                changes.append(("Reason", audit_entry.reason, False))

        await self.send_log(
            after.guild,
            category="moderation_events",
            action=action,
            details=f"{mention_user(after)} had profile or moderation changes.",
            fields=changes,
            target_user_id=after.id,
            target_user=after,
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        fields: List[Tuple[str, str, bool]] = []
        audit_entry = await self.get_recent_audit_entry(
            guild,
            discord.AuditLogAction.ban,
            user.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            fields.append(("Moderator", moderator, False))
            if audit_entry.reason:
                fields.append(("Reason", audit_entry.reason, False))

        await self.send_log(
            guild,
            category="moderation_events",
            action="Member Banned",
            details=f"{mention_user(user)} was banned.",
            fields=fields,
            target_user_id=user.id,
            target_user=user,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        fields: List[Tuple[str, str, bool]] = []
        audit_entry = await self.get_recent_audit_entry(
            guild,
            discord.AuditLogAction.unban,
            user.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            fields.append(("Moderator", moderator, False))
            if audit_entry.reason:
                fields.append(("Reason", audit_entry.reason, False))

        await self.send_log(
            guild,
            category="moderation_events",
            action="Member Unbanned",
            details=f"{mention_user(user)} was unbanned.",
            fields=fields,
            target_user_id=user.id,
            target_user=user,
        )

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.send_log(
            channel.guild,
            category="channel_events",
            action="Channel Created",
            details=f"{channel.mention} was created.",
            fields=[("Type", str(channel.type), True), ("Channel ID", str(channel.id), True)],
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.send_log(
            channel.guild,
            category="channel_events",
            action="Channel Deleted",
            details=f"`#{channel.name}` was deleted.",
            fields=[("Type", str(channel.type), True), ("Channel ID", str(channel.id), True)],
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        updates: List[Tuple[str, str, bool]] = []
        if before.name != after.name:
            updates.append(("Name", f"`{before.name}` → `{after.name}`", False))

        before_topic = getattr(before, "topic", None)
        after_topic = getattr(after, "topic", None)
        if before_topic != after_topic:
            updates.append(("Topic", f"{truncate(before_topic)} → {truncate(after_topic)}", False))

        before_slowmode = getattr(before, "slowmode_delay", None)
        after_slowmode = getattr(after, "slowmode_delay", None)
        if before_slowmode != after_slowmode:
            updates.append(("Slowmode", f"`{before_slowmode or 0}s` → `{after_slowmode or 0}s`", True))

        if not updates:
            return

        mention = after.mention if isinstance(after, discord.TextChannel) else f"`#{after.name}`"
        await self.send_log(
            after.guild,
            category="channel_events",
            action="Channel Updated",
            details=f"{mention} had configuration changes.",
            fields=updates,
        )

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        await self.send_log(
            thread.guild,
            category="thread_events",
            action="Thread Created",
            details=f"Thread **{thread.name}** was created in {thread.parent.mention if thread.parent else 'an unknown channel'}.",
            fields=[("Thread ID", str(thread.id), True)],
        )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        await self.send_log(
            thread.guild,
            category="thread_events",
            action="Thread Deleted",
            details=f"Thread **{thread.name}** was deleted.",
            fields=[("Thread ID", str(thread.id), True)],
        )

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        updates: List[Tuple[str, str, bool]] = []
        if before.name != after.name:
            updates.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.archived != after.archived:
            updates.append(("Archived", f"`{before.archived}` → `{after.archived}`", True))
        if before.locked != after.locked:
            updates.append(("Locked", f"`{before.locked}` → `{after.locked}`", True))

        if not updates:
            return

        await self.send_log(
            after.guild,
            category="thread_events",
            action="Thread Updated",
            details=f"Thread **{after.name}** changed.",
            fields=updates,
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        fields: List[Tuple[str, str, bool]] = [
            ("Role Name", f"`{role.name}`", False),
            ("Role ID", str(role.id), True),
            ("Color", str(role.color), True),
        ]

        audit_entry = await self.get_recent_audit_entry(
            role.guild,
            discord.AuditLogAction.role_create,
            role.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            fields.append(("Moderator", moderator, False))
            if audit_entry.reason:
                fields.append(("Reason", audit_entry.reason, False))

        await self.send_log(
            role.guild,
            category="role_events",
            action="Role Created",
            details=f"Role `{role.name}` was created.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        fields: List[Tuple[str, str, bool]] = [
            ("Role Name", f"`{role.name}`", False),
            ("Role ID", str(role.id), True),
            ("Color", str(role.color), True),
        ]

        audit_entry = await self.get_recent_audit_entry(
            role.guild,
            discord.AuditLogAction.role_delete,
            role.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            fields.append(("Moderator", moderator, False))
            fields.append(("Reason", audit_entry.reason or "No reason provided.", False))

        await self.send_log(
            role.guild,
            category="role_events",
            action="Role Deleted",
            details=f"Role `{role.name}` was deleted.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        updates: List[Tuple[str, str, bool]] = []

        if before.name != after.name:
            updates.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.color != after.color:
            updates.append(("Color", f"`{before.color}` → `{after.color}`", True))
        if before.mentionable != after.mentionable:
            updates.append(("Mentionable", f"`{before.mentionable}` → `{after.mentionable}`", True))
        if before.hoist != after.hoist:
            updates.append(("Shown Separately", f"`{before.hoist}` → `{after.hoist}`", True))
        if before.permissions != after.permissions:
            updates.append(("Permissions", "Role permissions changed.", False))

        audit_entry = await self.get_recent_audit_entry(
            after.guild,
            discord.AuditLogAction.role_update,
            after.id,
        )
        if audit_entry:
            moderator = mention_user(audit_entry.user) if audit_entry.user else "Unknown moderator"
            updates.append(("Moderator", moderator, False))
            if audit_entry.reason:
                updates.append(("Reason", audit_entry.reason, False))

        if not updates:
            return

        await self.send_log(
            after.guild,
            category="role_events",
            action="Role Updated",
            details=f"Role {after.mention} changed.",
            fields=updates,
        )

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: Sequence[discord.Emoji],
        after: Sequence[discord.Emoji],
    ) -> None:
        before_ids = {emoji.id for emoji in before}
        after_ids = {emoji.id for emoji in after}

        added = [emoji.name for emoji in after if emoji.id not in before_ids]
        removed = [emoji.name for emoji in before if emoji.id not in after_ids]

        fields: List[Tuple[str, str, bool]] = []
        if added:
            fields.append(("Added", ", ".join(f"`:{name}:`" for name in added), False))
        if removed:
            fields.append(("Removed", ", ".join(f"`:{name}:`" for name in removed), False))

        if not fields:
            fields.append(("Update", "Emoji settings changed.", False))

        await self.send_log(
            guild,
            category="emoji_sticker_events",
            action="Emoji Updated",
            details="Server emoji list changed.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: Sequence[discord.GuildSticker],
        after: Sequence[discord.GuildSticker],
    ) -> None:
        before_ids = {sticker.id for sticker in before}
        after_ids = {sticker.id for sticker in after}

        added = [sticker.name for sticker in after if sticker.id not in before_ids]
        removed = [sticker.name for sticker in before if sticker.id not in after_ids]

        fields: List[Tuple[str, str, bool]] = []
        if added:
            fields.append(("Added", ", ".join(f"`{name}`" for name in added), False))
        if removed:
            fields.append(("Removed", ", ".join(f"`{name}`" for name in removed), False))

        if not fields:
            fields.append(("Update", "Sticker settings changed.", False))

        await self.send_log(
            guild,
            category="emoji_sticker_events",
            action="Stickers Updated",
            details="Server sticker list changed.",
            fields=fields,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        fields: List[Tuple[str, str, bool]] = []

        if before.channel != after.channel:
            before_channel = before.channel.mention if before.channel else "None"
            after_channel = after.channel.mention if after.channel else "None"
            fields.append(("Channel", f"{before_channel} → {after_channel}", False))

        voice_flags = [
            ("Self Mute", before.self_mute, after.self_mute),
            ("Self Deaf", before.self_deaf, after.self_deaf),
            ("Server Mute", before.mute, after.mute),
            ("Server Deaf", before.deaf, after.deaf),
            ("Streaming", before.self_stream, after.self_stream),
            ("Video", before.self_video, after.self_video),
            ("Suppressed", before.suppress, after.suppress),
        ]
        for label, old, new in voice_flags:
            if old != new:
                fields.append((label, f"`{old}` → `{new}`", True))

        if not fields:
            return

        await self.send_log(
            member.guild,
            category="voice_events",
            action="Voice State Updated",
            details=f"Voice status changed for {mention_user(member)}.",
            fields=fields,
            target_user_id=member.id,
        )

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return

        invite_cache = self._invite_uses_cache.setdefault(invite.guild.id, {})
        invite_cache[invite.code] = invite.uses or 0

        inviter = mention_user(invite.inviter) if invite.inviter else "Unknown"
        max_uses = str(invite.max_uses) if invite.max_uses else "Unlimited"

        await self.send_log(
            invite.guild,
            category="invite_events",
            action="Invite Created",
            details=f"Invite `{invite.code}` was created.",
            fields=[
                ("Inviter", inviter, False),
                ("Channel", invite.channel.mention if invite.channel else "Unknown", False),
                ("Max Uses", max_uses, True),
            ],
            target_user_id=invite.inviter.id if invite.inviter else None,
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return

        invite_cache = self._invite_uses_cache.setdefault(invite.guild.id, {})
        invite_cache.pop(invite.code, None)

        await self.send_log(
            invite.guild,
            category="invite_events",
            action="Invite Deleted",
            details=f"Invite `{invite.code}` was deleted.",
            fields=[("Channel", invite.channel.mention if invite.channel else "Unknown", False)],
        )

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        updates: List[Tuple[str, str, bool]] = []

        if before.name != after.name:
            updates.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.description != after.description:
            updates.append(("Description", f"{truncate(before.description)} → {truncate(after.description)}", False))
        if before.verification_level != after.verification_level:
            updates.append(("Verification", f"`{before.verification_level}` → `{after.verification_level}`", True))
        if before.afk_timeout != after.afk_timeout:
            updates.append(("AFK Timeout", f"`{before.afk_timeout}s` → `{after.afk_timeout}s`", True))
        if before.system_channel != after.system_channel:
            before_ch = before.system_channel.mention if before.system_channel else "None"
            after_ch = after.system_channel.mention if after.system_channel else "None"
            updates.append(("System Channel", f"{before_ch} → {after_ch}", False))

        if not updates:
            return

        await self.send_log(
            after,
            category="guild_events",
            action="Server Updated",
            details="Server-wide settings changed.",
            fields=updates,
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        await self.send_log(
            channel.guild,
            category="misc_events",
            action="Webhooks Updated",
            details=f"Webhook configuration changed in {channel.mention if hasattr(channel, 'mention') else channel.name}.",
            fields=[("Channel", channel.mention if hasattr(channel, "mention") else channel.name, False)],
        )

    @commands.Cog.listener()
    async def on_guild_channel_pins_update(
        self,
        channel: discord.abc.GuildChannel,
        last_pin: Optional[datetime],
    ) -> None:
        pin_text = discord.utils.format_dt(last_pin, style="F") if last_pin else "No pinned messages"
        await self.send_log(
            channel.guild,
            category="misc_events",
            action="Pins Updated",
            details=f"Pinned messages changed in {channel.mention if hasattr(channel, 'mention') else channel.name}.",
            fields=[("Latest Pin", pin_text, False)],
        )

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage_instance: discord.StageInstance) -> None:
        guild = stage_instance.guild
        await self.send_log(
            guild,
            category="misc_events",
            action="Stage Started",
            details=f"Stage instance started in {stage_instance.channel.mention}.",
            fields=[("Topic", truncate(stage_instance.topic), False)],
        )

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage_instance: discord.StageInstance) -> None:
        guild = stage_instance.guild
        await self.send_log(
            guild,
            category="misc_events",
            action="Stage Ended",
            details=f"Stage instance ended in {stage_instance.channel.mention}.",
            fields=[("Topic", truncate(stage_instance.topic), False)],
        )

    @commands.Cog.listener()
    async def on_stage_instance_update(
        self,
        before: discord.StageInstance,
        after: discord.StageInstance,
    ) -> None:
        updates: List[Tuple[str, str, bool]] = []
        if before.topic != after.topic:
            updates.append(("Topic", f"{truncate(before.topic)} → {truncate(after.topic)}", False))
        if before.privacy_level != after.privacy_level:
            updates.append(("Privacy", f"`{before.privacy_level}` → `{after.privacy_level}`", True))

        if not updates:
            return

        await self.send_log(
            after.guild,
            category="misc_events",
            action="Stage Updated",
            details=f"Stage instance in {after.channel.mention} changed.",
            fields=updates,
        )
