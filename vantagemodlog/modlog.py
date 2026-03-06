from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

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
    "support_server_url": "",
    "entry_buttons": ["user_id", "jump_link"],
    "embed": {
        "title_prefix": "Vantage Modlog",
        "footer_text": "Vantage Logging",
        "thumbnail_url": "",
        "color": 0x5865F2,
    },
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


class UserIdButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, user_id: int):
        super().__init__(label="User ID", style=discord.ButtonStyle.secondary)
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"User ID: `{self.user_id}`",
            ephemeral=True,
        )


class LogEntryActionsView(discord.ui.View):
    def __init__(
        self,
        *,
        selected_buttons: Sequence[str],
        target_user_id: Optional[int],
        jump_url: Optional[str],
        support_server_url: Optional[str],
    ):
        super().__init__(timeout=180)

        if "user_id" in selected_buttons and target_user_id:
            self.add_item(UserIdButton(target_user_id))

        if "jump_link" in selected_buttons and jump_url:
            self.add_item(
                discord.ui.Button(
                    label="Jump",
                    style=discord.ButtonStyle.link,
                    url=jump_url,
                )
            )

        if "support_server" in selected_buttons and support_server_url:
            self.add_item(
                discord.ui.Button(
                    label="Support Server",
                    style=discord.ButtonStyle.link,
                    url=support_server_url,
                )
            )


class EmbedStyleModal(discord.ui.Modal):
    def __init__(self, cog: "VantageModlog", guild: discord.Guild, settings: Dict[str, Any]):
        super().__init__(title="Vantage Embed Style")
        self.cog = cog
        self.guild = guild

        embed_settings = settings["embed"]

        self.title_prefix = discord.ui.TextInput(
            label="Embed title prefix",
            default=embed_settings["title_prefix"],
            max_length=100,
            required=True,
            placeholder="Vantage Modlog",
        )
        self.color_hex = discord.ui.TextInput(
            label="Accent color (hex)",
            default=f"#{embed_settings['color']:06X}",
            max_length=7,
            min_length=6,
            required=True,
            placeholder="#5865F2",
        )
        self.footer_text = discord.ui.TextInput(
            label="Footer text",
            default=embed_settings["footer_text"],
            max_length=100,
            required=True,
            placeholder="Vantage Logging",
        )
        self.thumbnail_url = discord.ui.TextInput(
            label="Thumbnail URL (optional)",
            default=embed_settings["thumbnail_url"],
            required=False,
            placeholder="https://...",
            max_length=300,
        )

        self.add_item(self.title_prefix)
        self.add_item(self.color_hex)
        self.add_item(self.footer_text)
        self.add_item(self.thumbnail_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color_value = self.color_hex.value.strip().lstrip("#")
        try:
            parsed_color = int(color_value, 16)
        except ValueError:
            await interaction.response.send_message(
                "Please provide a valid hex color like `#5865F2`.",
                ephemeral=True,
            )
            return

        if parsed_color < 0 or parsed_color > 0xFFFFFF:
            await interaction.response.send_message(
                "Color must be between `#000000` and `#FFFFFF`.",
                ephemeral=True,
            )
            return

        settings = await self.cog.get_settings(self.guild)
        embed_settings = settings["embed"]
        embed_settings["title_prefix"] = self.title_prefix.value.strip()
        embed_settings["color"] = parsed_color
        embed_settings["footer_text"] = self.footer_text.value.strip()
        embed_settings["thumbnail_url"] = self.thumbnail_url.value.strip()

        await self.cog.config.guild(self.guild).embed.set(embed_settings)
        await interaction.response.send_message(
            "Embed style saved. Use `/modlog` again to view the updated dashboard preview.",
            ephemeral=True,
        )


class SupportServerModal(discord.ui.Modal):
    def __init__(self, cog: "VantageModlog", guild: discord.Guild, support_url: str):
        super().__init__(title="Vantage Support Server URL")
        self.cog = cog
        self.guild = guild

        self.support_url = discord.ui.TextInput(
            label="Support server URL",
            default=support_url,
            required=False,
            max_length=300,
            placeholder="https://discord.gg/...",
        )
        self.add_item(self.support_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        url = self.support_url.value.strip()
        if url and not url.startswith("https://"):
            await interaction.response.send_message(
                "Please enter a full URL starting with `https://`.",
                ephemeral=True,
            )
            return

        await self.cog.config.guild(self.guild).support_server_url.set(url)
        await interaction.response.send_message(
            "Support server link saved.",
            ephemeral=True,
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
            await interaction.response.send_message(
                "Please choose a channel.",
                ephemeral=True,
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
            await interaction.response.send_message(
                "This setup panel is tied to the user who opened `/modlog`.",
                ephemeral=True,
            )
            return False

        user = interaction.user
        if isinstance(user, discord.Member) and user.guild_permissions.manage_guild:
            return True

        await interaction.response.send_message(
            "You need the **Manage Server** permission to edit modlog settings.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Edit Embed Style", style=discord.ButtonStyle.primary, row=3)
    async def edit_embed_style(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        settings = await self.cog.get_settings(self.guild)
        await interaction.response.send_modal(EmbedStyleModal(self.cog, self.guild, settings))

    @discord.ui.button(label="Support URL", style=discord.ButtonStyle.secondary, row=3)
    async def edit_support_url(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        support_url = await self.cog.config.guild(self.guild).support_server_url()
        await interaction.response.send_modal(SupportServerModal(self.cog, self.guild, support_url))

    @discord.ui.button(label="Send Test Entry", style=discord.ButtonStyle.secondary, row=3)
    async def send_test_entry(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await self.cog.send_test_log(self.guild, interaction.user)
        await interaction.response.send_message(
            "Test entry sent to your selected modlog channel.",
            ephemeral=True,
        )

    @discord.ui.button(label="Finalize Changes", style=discord.ButtonStyle.success, row=4)
    async def finalize_changes(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        settings = await self.cog.get_settings(self.guild)
        if not settings.get("log_channel_id"):
            await interaction.response.send_message(
                "Pick a log channel first.",
                ephemeral=True,
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
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=4)
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

    async def get_settings(self, guild: discord.Guild) -> Dict[str, Any]:
        settings = await self.config.guild(guild).all()
        changed = False

        for key, value in DEFAULT_GUILD_SETTINGS.items():
            if key not in settings:
                settings[key] = copy.deepcopy(value)
                changed = True

        for key, value in DEFAULT_GUILD_SETTINGS["embed"].items():
            if key not in settings["embed"]:
                settings["embed"][key] = value
                changed = True

        for key, value in DEFAULT_GUILD_SETTINGS["events"].items():
            if key not in settings["events"]:
                settings["events"][key] = value
                changed = True

        if changed:
            await self.config.guild(guild).set(settings)

        return settings

    def build_dashboard_embed(
        self,
        guild: discord.Guild,
        settings: Dict[str, Any],
        *,
        first_time: bool,
    ) -> discord.Embed:
        embed_cfg = settings["embed"]
        color = discord.Color(embed_cfg["color"])

        title = "Vantage Modlog Setup" if first_time else "Vantage Modlog Editor"
        description_lines = []

        if first_time:
            description_lines.extend(
                [
                    "Welcome to your first-time setup.",
                    "Use the menus and buttons below, then click **Finalize Changes**.",
                ]
            )
        else:
            description_lines.append("Update any setting below. Changes save immediately.")

        steps = [
            ("Choose log channel", settings.get("log_channel_id") is not None),
            ("Adjust embed style", True),
            ("Choose event coverage", any(settings["events"].values())),
            ("Pick log-entry buttons", True),
            ("Finalize", settings.get("setup_complete", False)),
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
        embed.add_field(name="Checklist", value=checklist, inline=False)
        embed.add_field(name="Log Channel", value=channel_text, inline=False)
        embed.add_field(name="Logged Event Groups", value=truncate(enabled_text, limit=1000), inline=False)
        embed.add_field(
            name="Entry Buttons",
            value=truncate(button_text, limit=1000),
            inline=False,
        )

        embed_preview = (
            f"Prefix: `{embed_cfg['title_prefix']}`\n"
            f"Color: `#{embed_cfg['color']:06X}`\n"
            f"Footer: `{embed_cfg['footer_text']}`"
        )
        embed.add_field(name="Embed Preview", value=embed_preview, inline=False)

        support_url = settings.get("support_server_url") or "Not set"
        embed.add_field(name="Support Server Button", value=truncate(support_url, limit=1000), inline=False)

        footer_text = embed_cfg.get("footer_text", "").strip()
        if footer_text:
            footer_text = f"{footer_text} • Vantage Modlog"
        else:
            footer_text = "Vantage Modlog"
        embed.set_footer(text=footer_text)
        if embed_cfg.get("thumbnail_url"):
            embed.set_thumbnail(url=embed_cfg["thumbnail_url"])

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
                    ephemeral=True,
                )
            else:
                await ctx.interaction.response.send_message(
                    embed=embed,
                    view=view,
                    ephemeral=True,
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
    ) -> Optional[discord.Embed]:
        settings = await self.get_settings(guild)
        if not settings.get("setup_complete"):
            return None
        if not settings["events"].get(category, True):
            return None

        embed_settings = settings["embed"]
        embed = discord.Embed(
            title=f"{embed_settings['title_prefix']} • {action}",
            description=details,
            color=discord.Color(embed_settings["color"]),
            timestamp=now_utc(),
        )

        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=truncate(value, limit=1024), inline=inline)

        embed.set_footer(text=embed_settings["footer_text"])
        if embed_settings.get("thumbnail_url"):
            embed.set_thumbnail(url=embed_settings["thumbnail_url"])

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
        )
        if embed is None:
            return

        view = LogEntryActionsView(
            selected_buttons=settings.get("entry_buttons", []),
            target_user_id=target_user_id,
            jump_url=jump_url,
            support_server_url=settings.get("support_server_url"),
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

    async def maybe_kick_reason(self, guild: discord.Guild, user_id: int) -> Optional[str]:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == user_id:
                    if now_utc() - entry.created_at <= timedelta(seconds=15):
                        actor_text = mention_user(entry.user) if entry.user else "Unknown moderator"
                        reason_text = entry.reason or "No reason provided."
                        return f"User was kicked by {actor_text}. Reason: {reason_text}"
        except (discord.Forbidden, discord.HTTPException):
            return None

        return None

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
        created_at = discord.utils.format_dt(member.created_at, style="R")
        await self.send_log(
            member.guild,
            category="member_events",
            action="Member Joined",
            details=f"{mention_user(member)} joined the server.",
            fields=[
                ("Account Created", created_at, False),
                ("Member Count", str(member.guild.member_count), True),
            ],
            target_user_id=member.id,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        kick_details = await self.maybe_kick_reason(member.guild, member.id)
        details = kick_details or f"{mention_user(member)} left the server."

        await self.send_log(
            member.guild,
            category="member_events",
            action="Member Left",
            details=details,
            fields=[("Joined At", discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown", False)],
            target_user_id=member.id,
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        changes: List[Tuple[str, str, bool]] = []

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
            before_text = discord.utils.format_dt(before_timeout, style="F") if before_timeout else "Not timed out"
            after_text = discord.utils.format_dt(after_timeout, style="F") if after_timeout else "Not timed out"
            changes.append(("Timeout", f"{before_text} → {after_text}", False))

        if not changes:
            return

        await self.send_log(
            after.guild,
            category="moderation_events",
            action="Member Updated",
            details=f"{mention_user(after)} had profile or moderation changes.",
            fields=changes,
            target_user_id=after.id,
        )

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.send_log(
            guild,
            category="moderation_events",
            action="Member Banned",
            details=f"{mention_user(user)} was banned.",
            target_user_id=user.id,
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.send_log(
            guild,
            category="moderation_events",
            action="Member Unbanned",
            details=f"{mention_user(user)} was unbanned.",
            target_user_id=user.id,
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
        await self.send_log(
            role.guild,
            category="role_events",
            action="Role Created",
            details=f"Role {role.mention} was created.",
            fields=[("Role ID", str(role.id), True)],
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.send_log(
            role.guild,
            category="role_events",
            action="Role Deleted",
            details=f"Role `{role.name}` was deleted.",
            fields=[("Role ID", str(role.id), True)],
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
