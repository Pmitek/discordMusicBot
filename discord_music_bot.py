"""
Discord Music Bot (Python, discord.py v2) — Option A applied (JIT stream resolve + headers)

What changed vs your version:
- Track no longer stores stream_url (YouTube signed URLs expire)
- Stream URL + http_headers are resolved right before playback (just-in-time)
- FFmpeg gets -headers built from yt-dlp's http_headers (User-Agent/Referer/Cookie etc.)
- One automatic retry on 403 (fresh URL resolve)

Requirements:
- python >= 3.10
- discord.py ~= 2.4
- yt-dlp (keep updated)
- ffmpeg on PATH
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Deque, Optional, List, Tuple
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp

# --------------------------- Logging ---------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("music-bot")

# --------------------------- yt-dlp / FFmpeg opts --------------------
ytdl_opts: dict = {
    # Prefer audio-only formats; fallback to best.
    "format": "bestaudio[acodec!=none]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "skip_download": True,
    # Reduce network flakiness and speed up extraction
    "source_address": "0.0.0.0",
}

# Input-side options: reconnect for HTTP/HLS etc.
FFMPEG_BEFORE_BASE: List[str] = [
    "-reconnect", "1",
    "-reconnect_streamed", "1",
    "-reconnect_delay_max", "5",
]

# Output-side options
FFMPEG_OUT_OPTIONS: str = " ".join([
    "-vn",  # drop video if present
    "-loglevel", "warning",
    "-ar", "48000",
    "-ac", "2",
])

# --------------------------- Models ---------------------------------
@dataclass
class Track:
    title: str
    webpage_url: str          # stable URL (YouTube page)
    duration: Optional[int]   # seconds
    requester_id: int

    def pretty_duration(self) -> str:
        if self.duration is None or self.duration <= 0:
            return "live"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


# --------------------------- YTDL Helper -----------------------------
class YTDL:
    _ytdl = yt_dlp.YoutubeDL(ytdl_opts)

    @classmethod
    def _pick_first_entry(cls, info: dict) -> dict:
        if info is None:
            raise RuntimeError("No results.")
        if "entries" in info:
            info = next((e for e in info["entries"] if e), None)
        if not info:
            raise RuntimeError("No entries found.")
        return info

    @classmethod
    async def extract_meta(cls, query: str) -> Track:
        """Resolve metadata (title/webpage_url/duration). Does NOT return stream URL."""
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: cls._ytdl.extract_info(query, download=False))
        info = cls._pick_first_entry(info)

        title = info.get("title") or "Unknown title"
        webpage_url = info.get("webpage_url") or query
        duration = info.get("duration")
        return Track(title=title, webpage_url=webpage_url, duration=duration, requester_id=0)

    @classmethod
    async def resolve_stream(cls, webpage_url: str) -> tuple[str, dict]:
        """Resolve a fresh signed stream URL + HTTP headers. Call right before playback."""
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: cls._ytdl.extract_info(webpage_url, download=False))
        info = cls._pick_first_entry(info)

        stream_url = info.get("url")
        if not stream_url:
            raise RuntimeError("Failed to resolve audio stream URL.")

        headers = info.get("http_headers") or {}
        return stream_url, headers


def build_ffmpeg_before_options(headers: dict) -> str:
    """
    Build FFmpeg input options, including -headers copied from yt-dlp http_headers.
    We keep reconnect flags and add the header block (CRLF separated).
    """
    # Some dicts may have non-str values; coerce cleanly
    header_lines: List[str] = []
    for k, v in headers.items():
        if v is None:
            continue
        header_lines.append(f"{k}: {v}")

    # FFmpeg expects CRLF between headers.
    # Quote and escape to survive subprocess args parsing.
    hdr_blob = "\r\n".join(header_lines) + ("\r\n" if header_lines else "")
    hdr_blob = hdr_blob.replace("\\", "\\\\").replace('"', '\\"')

    parts: List[str] = []
    parts.extend(FFMPEG_BEFORE_BASE)

    if hdr_blob:
        parts.extend(["-headers", f'"{hdr_blob}"'])

    return " ".join(parts)


# --------------------------- Player ---------------------------------
class GuildPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.loop_track: bool = False
        self._play_next = asyncio.Event()
        self._audio_task: Optional[asyncio.Task] = None
        self._panel_message: Optional[discord.Message] = None
        self._last_error: Optional[Exception] = None

    # ---- Voice helpers ----
    async def connect(self, interaction: discord.Interaction) -> discord.VoiceClient:
        assert interaction.user is not None
        assert isinstance(interaction.user, discord.Member)
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            raise commands.CommandError("You are not in a voice channel.")

        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
        return vc

    # ---- Queue ops ----
    def add(self, track: Track):
        self.queue.append(track)
        log.info("Queued: %s", track.title)

    def clear(self):
        self.queue.clear()

    def shuffle(self):
        import random
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    # ---- Playback loop ----
    async def ensure_player_task(self):
        if self._audio_task is None or self._audio_task.done():
            self._audio_task = asyncio.create_task(self._player_loop())

    def _after_callback(self, e: Optional[Exception]):
        self._last_error = e if isinstance(e, Exception) else None
        # after runs in a different thread, wake up the async loop safely
        self.bot.loop.call_soon_threadsafe(self._play_next.set)

    async def _play_with_fresh_url(self, vc: discord.VoiceClient, track: Track):
        stream_url, headers = await YTDL.resolve_stream(track.webpage_url)
        before_opts = build_ffmpeg_before_options(headers)

        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=before_opts,
            options=FFMPEG_OUT_OPTIONS,
        )

        self._last_error = None
        self._play_next.clear()
        vc.play(source, after=self._after_callback)

        # Update panel if present
        try:
            await self._update_panel()
        except Exception:
            log.exception("Failed to update panel message")

        await self._play_next.wait()

    async def _player_loop(self):
        await self.bot.wait_until_ready()

        while True:
            if self.loop_track and self.current is not None:
                next_track = self.current
            else:
                try:
                    next_track = self.queue.popleft()
                except IndexError:
                    self.current = None
                    return

            self.current = next_track

            vc = self.guild.voice_client
            if vc is None or not vc.is_connected():
                self.current = None
                return

            # 1st attempt
            try:
                await self._play_with_fresh_url(vc, next_track)
            except Exception as e:
                log.exception("Playback setup failed: %s", e)
                # If we fail before playback even starts, move on
                continue

            # If we got an FFmpeg error after playback finished, and it looks like a 403, retry once.
            if self._last_error and "403" in str(self._last_error):
                log.warning("Detected 403 during playback; retrying once with a fresh URL...")
                try:
                    await self._play_with_fresh_url(vc, next_track)
                except Exception as e:
                    log.exception("Retry failed: %s", e)
                    # continue to next track
                    continue

    async def stop(self):
        vc = self.guild.voice_client
        if vc and vc.is_connected():
            vc.stop()
            await vc.disconnect(force=False)
        self.clear()
        self.current = None
        self.loop_track = False

    async def skip(self):
        vc = self.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    # ---- Panel ----
    async def send_or_update_panel(self, channel: discord.TextChannel):
        embed, view = self._build_panel()
        if self._panel_message and not self._panel_message.is_system():
            try:
                await self._panel_message.edit(embed=embed, view=view)
                return self._panel_message
            except discord.HTTPException:
                pass
        self._panel_message = await channel.send(embed=embed, view=view)
        return self._panel_message

    async def _update_panel(self):
        if not self._panel_message:
            return
        try:
            embed, view = self._build_panel()
            await self._panel_message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    def _build_panel(self) -> Tuple[discord.Embed, discord.ui.View]:
        if self.current:
            desc = (
                f"Now playing: [{self.current.title}]({self.current.webpage_url})\n"
                f"Duration: {self.current.pretty_duration()}"
            )
        else:
            desc = "Queue is empty. Use /play to add songs."

        embed = discord.Embed(
            title="🎵 Music Controller",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Loop: {'ON' if self.loop_track else 'OFF'} | In: {self.guild.name}")
        view = ControlPanel(self)
        return embed, view


# --------------------------- UI View ---------------------------------
class ControlPanel(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _ensure_vc(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            await interaction.response.send_message("Not connected to voice.", ephemeral=True)
            return None
        return vc

    @discord.ui.button(label="Pause/Resume", style=discord.ButtonStyle.primary, emoji="⏯")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        vc = await self._ensure_vc(interaction)
        if not vc:
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await self.player._update_panel()

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        vc = await self._ensure_vc(interaction)
        if not vc:
            return
        await interaction.response.defer(ephemeral=True)
        await self.player.skip()
        await interaction.followup.send("Skipped.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        await interaction.response.defer(ephemeral=True)
        await self.player.stop()
        await interaction.followup.send("Stopped and disconnected.", ephemeral=True)
        await self.player._update_panel()

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.success, emoji="🔁")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        self.player.loop_track = not self.player.loop_track
        await interaction.response.send_message(
            f"Loop is now {'ON' if self.player.loop_track else 'OFF'}.",
            ephemeral=True,
        )
        await self.player._update_panel()

    @discord.ui.button(label="Show Queue", style=discord.ButtonStyle.secondary, emoji="📄")
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore[override]
        if not self.player.queue:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        lines = []
        for i, t in enumerate(list(self.player.queue)[:15], start=1):
            lines.append(f"**{i}.** [{t.title}]({t.webpage_url}) • {t.pretty_duration()}")
        msg = "\n".join(lines)
        await interaction.response.send_message(msg, ephemeral=True, suppress_embeds=True)


# --------------------------- Bot Setup -------------------------------
class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned_or("!"), intents=intents)
        self.players: dict[int, GuildPlayer] = {}

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def setup_hook(self) -> None:
        return None

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user and self.user.id)
        try:
            synced = await self.tree.sync()
            log.info("Synced %d app commands.", len(synced))
        except Exception:
            log.exception("Failed to sync commands")


bot = MusicBot()

# --------------------------- Slash Commands --------------------------
@bot.tree.command(name="play", description="Play a song from a YouTube URL or search query")
@app_commands.describe(query="URL or search keywords")
async def play(interaction: discord.Interaction, query: str):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)

    try:
        await player.connect(interaction)
    except commands.CommandError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    try:
        track = await YTDL.extract_meta(query)
        track.requester_id = interaction.user.id if interaction.user else 0
    except Exception as e:
        log.exception("Extraction failed")
        await interaction.followup.send(f"Failed to get audio metadata: {e}")
        return

    player.add(track)
    await player.ensure_player_task()

    if isinstance(interaction.channel, discord.TextChannel):
        await player.send_or_update_panel(interaction.channel)

    await interaction.followup.send(
        f"Queued **{discord.utils.escape_markdown(track.title)}** • {track.pretty_duration()}\n<{track.webpage_url}>"
    )


@bot.tree.command(name="skip", description="Skip the current song")
async def skip_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    await interaction.response.defer(ephemeral=True)
    await player.skip()
    await interaction.followup.send("Skipped.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.", ephemeral=True)
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop and disconnect")
async def stop_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    await interaction.response.defer(ephemeral=True)
    await player.stop()
    await interaction.followup.send("Stopped and disconnected.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the next songs in queue")
async def queue_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    if not player.queue:
        await interaction.response.send_message("Queue is empty.", ephemeral=True)
        return
    lines = []
    for i, t in enumerate(list(player.queue)[:20], start=1):
        lines.append(f"**{i}.** [{t.title}]({t.webpage_url}) • {t.pretty_duration()}")
    embed = discord.Embed(title="Upcoming Tracks", description="\n".join(lines), color=discord.Color.green())
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="np", description="Show the currently playing track")
async def now_playing(interaction: discord.Interaction):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    if not player.current:
        await interaction.response.send_message("Nothing playing.", ephemeral=True)
        return
    t = player.current
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{t.title}]({t.webpage_url})\nDuration: {t.pretty_duration()}",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="shuffle", description="Shuffle the queue")
async def shuffle_cmd(interaction: discord.Interaction):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    player.shuffle()
    await interaction.response.send_message("Shuffled queue.", ephemeral=True)


@bot.tree.command(name="remove", description="Remove an item at index from the queue (use /queue to see indexes)")
@app_commands.describe(index="1-based index of the item to remove")
async def remove_cmd(interaction: discord.Interaction, index: int):
    assert interaction.guild is not None
    player = bot.get_player(interaction.guild)
    if index < 1 or index > len(player.queue):
        await interaction.response.send_message("Invalid index.", ephemeral=True)
        return
    q_list = list(player.queue)
    track = q_list.pop(index - 1)
    player.queue = deque(q_list)
    await interaction.response.send_message(
        f"Removed **{discord.utils.escape_markdown(track.title)}** from queue.",
        ephemeral=True,
    )


# --------------------------- Housekeeping ----------------------------
@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    # If the bot is alone in a voice channel, leave after a grace period
    vc = member.guild.voice_client
    if vc and vc.channel:
        channel = vc.channel
        if channel and len([m for m in channel.members if not m.bot]) == 0:
            await asyncio.sleep(30)
            if channel and len([m for m in channel.members if not m.bot]) == 0 and vc.is_connected():
                try:
                    await vc.disconnect()
                    log.info("Disconnected due to inactivity in %s", member.guild.name)
                except Exception:
                    log.exception("Failed to disconnect on inactivity")


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Please set DISCORD_TOKEN environment variable.")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
