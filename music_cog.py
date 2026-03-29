#music_cog.py
import discord
from discord.ext import commands, tasks
import yt_dlp
import asyncio
import random
import aiohttp
import re
import time
import json
from collections import deque

# --- CONFIGURAÇÕES ---
YDL_OPTS_FULL_EXTRACT = {'format': 'bestaudio/best', 'noplaylist': True, 'quiet': True, 'no_warnings': True, 'ignoreerrors': True, 'default_search': 'ytsearch1'}
YDL_OPTS_PLAYLIST_FLAT = {'format': 'best', 'quiet': True, 'no_warnings': True, 'ignoreerrors': True, 'extract_flat': True, 'noplaylist': False}
FFMPEG_OPTS = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_on_network_error 1 -reconnect_on_http_error "4xx,5xx"', 'options': '-vn -loglevel error'}

# --- UI DE CONTROLES DO DISCORD ---
class MusicControls(discord.ui.View):
    def __init__(self, music_cog, text_channel):
        super().__init__(timeout=None)
        self.music_cog = music_cog
        self.text_channel = text_channel
        self.message = None
        self.update_all_buttons()

    def update_pause_resume_button_style(self):
        vc = self.text_channel.guild.voice_client
        if vc and vc.is_paused(): self.pause_resume.emoji = "▶️"
        else: self.pause_resume.emoji = "⏸️"

    def update_loop_button_style(self):
        loop_mode = self.music_cog.loop_states.get(self.text_channel.guild.id, 'off')
        if loop_mode == 'off': self.loop.emoji = '🔁'; self.loop.style = discord.ButtonStyle.secondary
        elif loop_mode == 'queue': self.loop.emoji = '🔁'; self.loop.style = discord.ButtonStyle.primary
        elif loop_mode == 'song': self.loop.emoji = '🔂'; self.loop.style = discord.ButtonStyle.primary

    def update_all_buttons(self):
        self.update_loop_button_style()
        self.update_pause_resume_button_style()
    
    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await self.music_cog.shuffle_from_web(interaction.guild.id)
        if success: await interaction.response.send_message("Fila embaralhada!", ephemeral=True, delete_after=5)
        else: await interaction.response.send_message("Não há músicas suficientes para embaralhar.", ephemeral=True, delete_after=5)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await self.music_cog.previous_from_web(interaction.guild.id)
        if success: await interaction.response.send_message("Voltando para a música anterior.", ephemeral=True, delete_after=5)
        else: await interaction.response.send_message("Não há histórico de músicas.", ephemeral=True, delete_after=5)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.music_cog.pause_resume_from_web(interaction.guild.id)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.skip_from_web(interaction.guild.id)
        await interaction.response.send_message("Música pulada.", ephemeral=True, delete_after=5)

    @discord.ui.button(emoji="🛑", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.music_cog.cleanup(interaction.guild)
        button.view.stop()
        await interaction.response.send_message("Player parado e bot desconectado.", ephemeral=True)
        
    @discord.ui.button(emoji="🎶", label="Fila", style=discord.ButtonStyle.secondary, row=1)
    async def queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue_ctx = await self.music_cog.bot.get_context(interaction.message)
        queue_ctx.author = interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.music_cog.queue(queue_ctx, interaction)
        
    @discord.ui.button(emoji="ℹ️", label="Tocando", style=discord.ButtonStyle.primary, row=1)
    async def now_playing(self, interaction: discord.Interaction, button: discord.ui.Button):
        np_ctx = await self.music_cog.bot.get_context(interaction.message)
        np_ctx.author = interaction.user
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.music_cog.nowplaying(np_ctx, interaction)
        
    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_id = interaction.guild.id
        next_mode = await self.music_cog.toggle_loop_from_web(guild_id)
        # Deferir antes de enviar a resposta de acompanhamento
        await interaction.response.defer()
        await interaction.followup.send(f"Modo de repetição alterado para: **{next_mode}**.", ephemeral=True)


class MusicCog(commands.Cog, name="Música"):
    def __init__(self, bot):
        self.bot = bot
        self.manager = bot.manager
        self.queues = {}
        self.current_song = {}
        self.loop_states = {}
        self.player_views = {}
        self.volume_levels = {}
        self.start_times = {}
        self.pause_times = {} # <--- ADICIONADO
        self.history = {}
        self.session = aiohttp.ClientSession()
        self.auto_leave.start()
        self.update_np_message.start()

    async def cog_unload(self):
        await self.session.close()
        self.auto_leave.cancel()
        self.update_np_message.cancel()

    async def update_discord_player_message(self, guild_id: int):
        view = self.player_views.get(guild_id)
        if view and view.message:
            new_embed = self._create_np_embed(guild_id)
            if not new_embed:
                return await self.cleanup(view.message.guild)
            
            view.update_all_buttons()
            
            try:
                await view.message.edit(embed=new_embed, view=view)
            except (discord.NotFound, discord.HTTPException):
                self.player_views.pop(guild_id, None)

    def get_queue_data(self, guild_id: int):
        queue = self.queues.get(guild_id, [])
        current = self.current_song.get(guild_id)
        start_time = self.start_times.get(guild_id, 0)
        
        # <--- MODIFICADO: Lógica de cálculo do tempo decorrido
        elapsed = 0
        if current and start_time > 0:
            vc = self.bot.get_guild(guild_id).voice_client
            # Se pausado, o tempo decorrido é fixo no momento da pausa
            if vc and vc.is_paused() and guild_id in self.pause_times:
                elapsed = self.pause_times[guild_id] - start_time
            else: # Senão, é o tempo atual menos o tempo de início
                elapsed = time.time() - start_time

        def serialize_song(song):
            if not song: return None
            requester = song.get('requested_by')
            return {"title": song.get('title', 'Carregando...'), "webpage_url": song.get('webpage_url'), "thumbnail": song.get('thumbnail'), "duration": song.get('duration'), "requester": str(requester.display_name if requester else "Desconhecido")}
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        is_paused = vc.is_paused() if vc else False
        return {"guild_id": guild_id, "current_song": serialize_song(current), "queue": [serialize_song(s) for s in queue], "loop_mode": self.loop_states.get(guild_id, 'off'), "volume": self.volume_levels.get(guild_id, 1.0) * 100, "is_paused": is_paused, "elapsed": elapsed}
    
    async def broadcast_queue_update(self, guild_id: int):
        await self.manager.broadcast(guild_id, self.get_queue_data(guild_id))
        await self.update_discord_player_message(guild_id)

    async def cleanup(self, guild):
        guild_id = guild.id
        if guild_id in self.player_views:
            view = self.player_views.pop(guild_id)
            if view and view.message:
                for item in view.children: item.disabled = True
                try: await view.message.edit(content="A sessão de música terminou.", embed=None, view=view)
                except discord.NotFound: pass
        if guild.voice_client: await guild.voice_client.disconnect()
        # <--- MODIFICADO: Limpa o estado de pause_times também
        self.queues.pop(guild_id, None); self.current_song.pop(guild_id, None); self.loop_states.pop(guild_id, None); self.start_times.pop(guild_id, None); self.history.pop(guild_id, None); self.pause_times.pop(guild_id, None)
        await self.manager.broadcast(guild_id, self.get_queue_data(guild_id))

    async def search_song(self, query):
        loop = self.bot.loop or asyncio.get_event_loop()
        is_url = query.startswith(('https://', 'http://'))
        opts = YDL_OPTS_PLAYLIST_FLAT if is_url and "list=" in query else YDL_OPTS_FULL_EXTRACT
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # Adicionamos um timeout de 15 segundos para a extração
                data = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False)),
                    timeout=15.0
                )
        except asyncio.TimeoutError:
            print("Erro: Busca no yt-dlp demorou demais."); return None
        except Exception as e: 
            print(f"Erro no yt-dlp: {e}"); return None
        if not data: return None
        if 'entries' in data:
            entries = [e for e in data['entries'] if e and e.get('url')]
            if not entries: return None
            if is_url and "list=" in query: return [{'is_stub': True, 'title': e.get('title', 'Título desconhecido'), 'webpage_url': e.get('url')} for e in entries]
            else: e = entries[0]; return {'is_stub': False, 'source': e['url'], 'title': e.get('title', 'Título desconhecido'), 'thumbnail': e.get('thumbnail'), 'duration': e.get('duration'), 'webpage_url': e.get('webpage_url')}
        else: return {'is_stub': False, 'source': data['url'], 'title': data.get('title', 'Título desconhecido'), 'thumbnail': data.get('thumbnail'), 'duration': data.get('duration'), 'webpage_url': data.get('webpage_url')}

    def after_playing_proxy(self, error, text_channel, requester):
        if self.bot.is_closed(): return
        self.bot.loop.create_task(self.handle_after_playing(error, text_channel, requester))

    async def handle_after_playing(self, error, text_channel, requester):
        if error: print(f'Player error: {error}'); await text_channel.send(f"❌ Erro ao tocar música: `{error}`. Pulando...")
        guild_id = text_channel.guild.id
        if self.current_song.get(guild_id):
            if guild_id not in self.history: self.history[guild_id] = deque(maxlen=50)
            self.history[guild_id].append(self.current_song[guild_id])
        await self.play_next_or_cleanup(text_channel, requester)

    async def play_next_or_cleanup(self, text_channel, requester):
        guild = text_channel.guild
        guild_id = guild.id; loop_mode = self.loop_states.get(guild_id, 'off')
        if loop_mode == 'song' and self.current_song.get(guild_id): self.queues.setdefault(guild_id, []).insert(0, self.current_song[guild_id])
        elif loop_mode == 'queue' and self.current_song.get(guild_id): self.queues.setdefault(guild_id, []).append(self.current_song[guild_id])
        
        if self.queues.get(guild_id):
            next_song_info = self.queues[guild_id].pop(0)
            if next_song_info.get('is_stub'):
                processed_info = await self.search_song(next_song_info['webpage_url'])
                if not processed_info: await text_channel.send(f"⚠️ Detalhes indisponíveis para '{next_song_info['title']}'. Pulando."); await self.play_next_or_cleanup(text_channel, requester); return
                processed_info['requested_by'] = next_song_info.get('requested_by'); song_info = processed_info
            else: song_info = next_song_info
            
            self.current_song[guild_id] = song_info; vc = guild.voice_client
            if vc and vc.is_connected():
                volume = self.volume_levels.get(guild_id, 1.0); source = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTS)
                player = discord.PCMVolumeTransformer(source, volume=volume)
                vc.play(player, after=lambda e: self.after_playing_proxy(e, text_channel, requester))
                self.start_times[guild_id] = time.time()
                self.pause_times.pop(guild_id, None) # <--- ADICIONADO: Garante que não há tempo de pausa antigo ao iniciar uma nova música
                await self.broadcast_queue_update(guild_id)
        else: await self.cleanup(guild)

    async def _add_to_queue_and_play(self, requester, text_channel, query: str):
        guild = text_channel.guild
        if not requester.voice or not requester.voice.channel: return
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            try:
                vc = await requester.voice.channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
            except asyncio.TimeoutError:
                return await text_channel.send("❌ Não consegui conectar ao canal de voz a tempo (Timeout).")
        elif vc.channel != requester.voice.channel:
            await vc.move_to(requester.voice.channel)
        
        async with text_channel.typing(): search_result = await self.search_song(query)
        if not search_result: return await text_channel.send("Não encontrei nada.")
        
        queue = self.queues.setdefault(guild.id, []); is_first_song = not vc.is_playing() and not vc.is_paused()
        
        if isinstance(search_result, list):
            for song in search_result: song['requested_by'] = requester
            queue.extend(search_result); await text_channel.send(f"✅ Adicionado **{len(search_result)}** músicas da playlist!")
        else:
            song = search_result; song['requested_by'] = requester; queue.append(song)
            if not is_first_song: await text_channel.send(f"✅ Adicionado à fila: **{song['title']}**")
        
        if is_first_song and self.queues.get(guild.id):
            if guild.id in self.player_views and self.player_views[guild.id].message:
                try: await self.player_views[guild.id].message.delete(); self.player_views.pop(guild.id, None)
                except: pass
            await self.play_next_or_cleanup(text_channel, requester)
            embed = self._create_np_embed(guild.id)
            if embed:
                controls = MusicControls(self, text_channel); message = await text_channel.send(embed=embed, view=controls)
                controls.message = message; self.player_views[guild.id] = controls
        else: await self.broadcast_queue_update(guild.id)

    @commands.command(name='play', aliases=['p'], help="Toca uma música ou playlist do YouTube.")
    async def play(self, ctx, *, query: str):
        if not ctx.author.voice or not ctx.author.voice.channel: return await ctx.send("Você precisa estar em um canal de voz!")
        await self._add_to_queue_and_play(ctx.author, ctx.channel, query)

    async def play_from_web(self, guild: discord.Guild, member: discord.Member, query: str):
        if not member.voice or not member.voice.channel: return
        vc = guild.voice_client
        if not vc or not vc.is_connected(): await member.voice.channel.connect()
        elif vc.channel != member.voice.channel: await vc.move_to(member.voice.channel)
        
        text_channel = None
        for channel in guild.text_channels:
            if any(name in channel.name.lower() for name in ["musica", "música", "comandos", "bot"]):
                if channel.permissions_for(guild.me).send_messages: text_channel = channel; break
        if not text_channel:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages: text_channel = channel; break
        if not text_channel: print(f"ERRO: Não encontrei um canal de texto para responder no servidor {guild.name}"); return
        
        await self._add_to_queue_and_play(member, text_channel, query)

    def _create_np_embed(self, guild_id):
        song_info = self.current_song.get(guild_id);
        if not song_info: return None
        embed = discord.Embed(title="Tocando Agora", description=f"**[{song_info.get('title', 'Carregando...')}]({song_info.get('webpage_url')})**", color=discord.Color.blue())
        if song_info.get('thumbnail'): embed.set_thumbnail(url=song_info.get('thumbnail'))
        start_time = self.start_times.get(guild_id)
        if start_time and song_info.get('duration'):
            # <--- MODIFICADO: Lógica de cálculo do tempo decorrido para o embed
            vc = self.bot.get_guild(guild_id).voice_client
            if vc and vc.is_paused() and guild_id in self.pause_times:
                elapsed = self.pause_times[guild_id] - start_time
            else:
                elapsed = time.time() - start_time
            
            elapsed = min(elapsed, song_info['duration'])
            progress_bar = self.create_progress_bar(elapsed, song_info['duration'])
            elapsed_str = self.format_duration(elapsed); total_str = self.format_duration(song_info['duration'])
            embed.add_field(name="Progresso", value=f"`{progress_bar}`\n`[{elapsed_str} / {total_str}]`", inline=False)
        requester = song_info.get('requested_by')
        if requester: embed.set_footer(text=f"Pedida por: {requester.display_name}", icon_url=requester.display_avatar.url)
        return embed

    @tasks.loop(seconds=5)
    async def update_np_message(self):
        for guild_id in list(self.player_views.keys()):
            guild = self.bot.get_guild(guild_id)
            if guild and guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                await self.broadcast_queue_update(guild_id)

    @update_np_message.before_loop
    async def before_update_np(self): await self.bot.wait_until_ready()
    
    async def skip_from_web(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()): guild.voice_client.stop()
        
    # <--- MODIFICADO: Lógica completa de pause/resume
    async def pause_resume_from_web(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not vc:
            return "nada"

        action = "nada"
        if vc.is_playing():
            vc.pause()
            self.pause_times[guild_id] = time.time()  # Registra o tempo da pausa
            action = "paused"
        elif vc.is_paused():
            pause_start_time = self.pause_times.pop(guild_id, None)
            # Se havia um tempo de pausa registrado, calcula a duração e ajusta o tempo de início
            if pause_start_time and guild_id in self.start_times:
                paused_duration = time.time() - pause_start_time
                self.start_times[guild_id] += paused_duration
            vc.resume()
            action = "resumed"

        await self.broadcast_queue_update(guild_id)
        return action

    async def previous_from_web(self, guild_id: int):
        vc = self.bot.get_guild(guild_id).voice_client; history = self.history.get(guild_id)
        if not vc or not vc.is_connected() or not history: return False
        prev_song = history.pop()
        if self.current_song.get(guild_id): self.queues.setdefault(guild_id, []).insert(0, self.current_song[guild_id])
        self.queues.setdefault(guild_id, []).insert(0, prev_song)
        if vc.is_playing() or vc.is_paused(): vc.stop()
        else:
            # O `requester` aqui pode ser o último que usou um comando, não é ideal mas é um fallback
            text_channel = self.player_views[guild_id].text_channel if guild_id in self.player_views else vc.channel
            last_requester = self.current_song.get(guild_id, {}).get('requested_by')
            await self.play_next_or_cleanup(text_channel, last_requester)
        return True
    async def shuffle_from_web(self, guild_id: int):
        queue = self.queues.get(guild_id)
        if not queue or len(queue) < 2: return False
        random.shuffle(queue); await self.broadcast_queue_update(guild_id); return True
    async def toggle_loop_from_web(self, guild_id: int):
        current_mode = self.loop_states.get(guild_id, 'off')
        next_mode = {'off': 'queue', 'queue': 'song', 'song': 'off'}.get(current_mode)
        self.loop_states[guild_id] = next_mode; await self.broadcast_queue_update(guild_id); return next_mode
    async def set_volume_from_web(self, guild_id: int, volume: int):
        if not 0 <= volume <= 200: return
        new_vol = volume / 100.0; self.volume_levels[guild_id] = new_vol; guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.source: guild.voice_client.source.volume = new_vol
        await self.broadcast_queue_update(guild_id)
    async def move_song(self, guild_id: int, old_index: int, new_index: int):
        queue = self.queues.get(guild_id)
        if not queue or not (0 <= old_index < len(queue)) or not (0 <= new_index < len(queue)): return False
        song_to_move = queue.pop(old_index); queue.insert(new_index, song_to_move); await self.broadcast_queue_update(guild_id); return True
    async def skipto_from_web(self, guild_id: int, index: int):
        queue = self.queues.get(guild_id)
        if not queue or not 0 <= index < len(queue): return
        self.queues[guild_id] = queue[index:]; vc = self.bot.get_guild(guild_id).voice_client
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
    async def remove_from_web(self, guild_id: int, index: int):
        queue = self.queues.get(guild_id)
        if queue and 0 <= index < len(queue): queue.pop(index); await self.broadcast_queue_update(guild_id)
    
    @commands.command(name='previous', aliases=['back', 'prev'], help="Toca a música anterior.")
    async def previous(self, ctx):
        if await self.previous_from_web(ctx.guild.id): await ctx.message.add_reaction('⏪')
        else: await ctx.send("Não há histórico de músicas.", delete_after=10)
    @commands.command(name='pause', help="Pausa a música atual.")
    async def pause(self, ctx): await self.pause_resume_from_web(ctx.guild.id); await ctx.message.add_reaction('✅')
    @commands.command(name='resume', help="Continua a música pausada.")
    async def resume(self, ctx): await self.pause_resume_from_web(ctx.guild.id); await ctx.message.add_reaction('✅')
    @commands.command(name='leave', help="Desconecta o bot e limpa a fila.")
    async def leave(self, ctx): await self.cleanup(ctx.guild); await ctx.send("Até a próxima! 👋")
    @commands.command(name='skip', help="Pula para a próxima música.")
    async def skip(self, ctx): await self.skip_from_web(ctx.guild.id); await ctx.message.add_reaction('⏩')
    @commands.command(name='volume', aliases=['vol'], help="Ajusta o volume (0-200).")
    async def volume(self, ctx, volume: int = None):
        if volume is None: return await ctx.send(f"🔊 Volume atual: **{self.volume_levels.get(ctx.guild.id, 1.0) * 100:.0f}%**.")
        await self.set_volume_from_web(ctx.guild.id, volume); await ctx.send(f"🔊 Volume ajustado para **{volume}%**.")
    @commands.command(name='nowplaying', aliases=['np'], help="Mostra a música atual e o progresso.")
    async def nowplaying(self, ctx, interaction: discord.Interaction = None):
        is_ephemeral = interaction is not None; embed = self._create_np_embed(ctx.guild.id)
        if not embed: msg = "Nada está tocando.";
        else:
            if is_ephemeral: return await interaction.followup.send(embed=embed, ephemeral=True)
            else: return await ctx.send(embed=embed)
        if is_ephemeral: await interaction.followup.send(msg, ephemeral=True)
        else: await ctx.send(msg)
    def create_progress_bar(self, current, total, bar_length=20):
        percentage = (current / total) if total > 0 else 0; filled_length = int(bar_length * percentage); return '█' * filled_length + '─' * (bar_length - filled_length)
    def format_duration(self, seconds):
        if seconds is None: return "??:??"
        try: m, s = divmod(int(seconds), 60); h, m = divmod(m, 60); return f"{m:02d}:{s:02d}" if h == 0 else f"{h}:{m:02d}:{s:02d}"
        except (ValueError, TypeError): return "??:??"
    @commands.command(name='loop', aliases=['repeat'], help="Define o modo de repetição (off, queue, song).")
    async def loop(self, ctx, mode: str):
        modes = ['off', 'queue', 'song'];
        if mode.lower() not in modes: return await ctx.send(f"Modo inválido. Use `{', '.join(modes)}`")
        self.loop_states[ctx.guild.id] = mode.lower(); await self.broadcast_queue_update(ctx.guild.id); await ctx.send(f"🔁 Modo de repetição: **{mode.lower()}**")
    @commands.command(name='shuffle', help="Embaralha a fila de músicas.")
    async def shuffle(self, ctx):
        if await self.shuffle_from_web(ctx.guild.id): await ctx.send("🔀 Fila embaralhada!")
        else: await ctx.send("Não há músicas suficientes para embaralhar.")
    @commands.command(name='remove', help="Remove uma música da fila pelo seu número.")
    async def remove(self, ctx, index: int):
        await self.remove_from_web(ctx.guild.id, index - 1); await ctx.message.add_reaction('🗑️')
    @commands.command(name='skipto', help="Pula para uma música específica na fila.")
    async def skipto(self, ctx, index: int):
        await self.skipto_from_web(ctx.guild.id, index - 1); await ctx.send(f"⏭️ Pulando para a música **{index}**.")
    @commands.command(name='clear', aliases=['limpar'], help="Limpa todas as músicas da fila.")
    async def clear(self, ctx):
        if ctx.guild.id in self.queues and self.queues[ctx.guild.id]:
            self.queues[ctx.guild.id].clear(); await ctx.send("🧹 Fila limpa!"); await self.broadcast_queue_update(ctx.guild.id)
        else: await ctx.send("A fila já está vazia.")
    @commands.command(name='lyrics', aliases=['letra'], help="Busca a letra da música atual.")
    async def lyrics(self, ctx, *, query: str = None):
        title = query or (self.current_song.get(ctx.guild.id) or {}).get('title')
        if not title: return await ctx.send("Especifique uma música ou toque uma primeiro.")
        async with ctx.typing():
            try:
                async with self.session.get(f"https://lrclib.net/api/search?q={title.replace('&', ' ')}") as resp:
                    if resp.status != 200: return await ctx.send("API de letras indisponível.")
                    data = await resp.json()
                if not data: return await ctx.send(f"Não encontrei a letra para '{title}'.")
                song = data[0]; lyrics = song.get('plainLyrics', 'Letra não disponível.')
                embed = discord.Embed(title=f"Letra de {song['trackName']} por {song['artistName']}", description=lyrics[:4096], color=discord.Color.gold())
                await ctx.send(embed=embed)
            except Exception as e: await ctx.send("Ocorreu um erro ao buscar a letra."); print(f"Erro na API de letras: {e}")
    @commands.command(name='queue', aliases=['q'], help="Mostra as próximas músicas na fila.")
    async def queue(self, ctx, interaction: discord.Interaction = None):
        is_ephemeral = interaction is not None; queue_list = self.queues.get(ctx.guild.id)
        if not queue_list: msg = "A fila está vazia!";
        else:
            embed = discord.Embed(title="Fila de Músicas", color=discord.Color.purple()); body = ""
            for i, song in enumerate(queue_list[:15]): body += f"`{i+1:2}`. `[{self.format_duration(song.get('duration'))}]` {song.get('title','Carregando...')[:60]}\n"
            embed.description = body
            if len(queue_list) > 15: embed.set_footer(text=f"E mais {len(queue_list) - 15} músicas...")
            if is_ephemeral: return await interaction.followup.send(embed=embed, ephemeral=True)
            else: return await ctx.send(embed=embed)
        if is_ephemeral: await interaction.followup.send(msg, ephemeral=True)
        else: await ctx.send(msg)
#mudança pra melhorar o traceback de erros
    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound): return
        
        # Isso vai mostrar EXATAMENTE em qual linha o erro aconteceu no journalctl
        import traceback
        import sys
        print(f"--- ERRO NO COMANDO {ctx.command} ---", file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"Faltou um argumento! Uso correto: `{ctx.prefix}{ctx.command.name} {ctx.command.signature}`")
        
        await ctx.send(f"Ocorreu um erro: `{error}`. Verifique o console para detalhes.")
    @tasks.loop(minutes=5)
    async def auto_leave(self):
        for guild in self.bot.guilds:
            vc = guild.voice_client
            if vc and not vc.is_playing() and not vc.is_paused() and len(vc.channel.members) == 1: await self.cleanup(guild)
    @auto_leave.before_loop
    async def before_auto_leave(self): await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(MusicCog(bot))