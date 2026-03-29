import discord
from discord.ext import commands
import os
import hashlib
import random
import time
from collections import deque
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC

# Caminhos locais
LOCAL_MUSIC_DIR = "./musicas"
PLAYLISTS_DIR = "./playlists"

# Opções simplificadas para arquivos locais
FFMPEG_OPTS_LOCAL = {
    'options': '-vn -loglevel error'
}

class LocalMusicControls(discord.ui.View):
    def __init__(self, local_cog, text_channel):
        super().__init__(timeout=None)
        self.local_cog = local_cog
        self.text_channel = text_channel
        self.update_buttons()

    def update_buttons(self):
        guild_id = self.text_channel.guild.id
        loop_mode = self.local_cog.loop_states.get(guild_id, 'off')
        vc = self.text_channel.guild.voice_client

        for child in self.children:
            if getattr(child, 'custom_id', '') == 'loop_btn':
                if loop_mode == 'off': child.emoji = '🔁'; child.style = discord.ButtonStyle.secondary
                elif loop_mode == 'queue': child.emoji = '🔁'; child.style = discord.ButtonStyle.primary
                elif loop_mode == 'song': child.emoji = '🔂'; child.style = discord.ButtonStyle.primary
            
            if getattr(child, 'custom_id', '') == 'pause_btn':
                if vc and vc.is_paused(): child.emoji = "▶️"
                else: child.emoji = "⏸️"

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await self.local_cog.shuffle_local(interaction.guild.id)
        if success: await interaction.response.send_message("🔀 Fila embaralhada!", ephemeral=True, delete_after=3)
        else: await interaction.response.send_message("Poucas músicas para embaralhar.", ephemeral=True, delete_after=3)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await self.local_cog.previous_local(interaction.guild.id)
        if success: await interaction.response.send_message("⏪ Voltando...", ephemeral=True, delete_after=3)
        else: await interaction.response.send_message("❌ Nenhum histórico encontrado.", ephemeral=True, delete_after=3)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0, custom_id="pause_btn")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.local_cog.pause_resume_local(interaction.guild.id)
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.local_cog.skip_local(interaction.guild.id)
        await interaction.response.send_message("⏩ Pulando música...", ephemeral=True, delete_after=3)

    @discord.ui.button(emoji="🛑", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.local_cog.cleanup_local(interaction.guild.id)
        if interaction.guild.voice_client: await interaction.guild.voice_client.disconnect()
        self.stop()
        await interaction.response.send_message("🛑 Sessão local encerrada.", ephemeral=True, delete_after=3)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1, custom_id="loop_btn")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        next_mode = await self.local_cog.toggle_loop_local(interaction.guild.id)
        self.update_buttons()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"Modo repetição: **{next_mode}**", ephemeral=True)


class MusicLocalCog(commands.Cog, name="Música Local"):
    def __init__(self, bot):
        self.bot = bot
        self.manager = bot.manager # Conecta ao WebSocket do main.py
        self.local_queues = {}
        self.current_local = {}
        self.player_messages = {}
        self.history = {}
        self.loop_states = {}
        self.start_times = {}
        self.pause_times = {}
        self.volume_levels = {}

    # --- INTEGRAÇÃO WEB ---
    def get_queue_data(self, guild_id: int):
        queue = self.local_queues.get(guild_id, [])
        current = self.current_local.get(guild_id)
        start_time = self.start_times.get(guild_id, 0)
        
        elapsed = 0
        if current and start_time > 0:
            vc = self.bot.get_guild(guild_id).voice_client
            if vc and vc.is_paused() and guild_id in self.pause_times:
                elapsed = self.pause_times[guild_id] - start_time
            else:
                elapsed = time.time() - start_time

        def serialize_song(song):
            if not song: return None
            requester = song.get('requested_by')
            
            # Ajusta o caminho da imagem para o frontend ler (/static/covers/...)
            thumb = song.get('thumbnail')
            if thumb and thumb.startswith('./'): thumb = thumb[1:]
                
            return {
                "title": song.get('title', 'Carregando...'), 
                "webpage_url": "#", # Local não tem URL
                "thumbnail": thumb, 
                "duration": song.get('duration'), 
                "requester": str(requester.display_name if requester else "Desconhecido")
            }
            
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        is_paused = vc.is_paused() if vc else False
        
        return {
            "guild_id": guild_id, 
            "current_song": serialize_song(current), 
            "queue": [serialize_song(s) for s in queue], 
            "loop_mode": self.loop_states.get(guild_id, 'off'), 
            "volume": self.volume_levels.get(guild_id, 1.0) * 100, 
            "is_paused": is_paused, 
            "elapsed": elapsed
        }

    async def broadcast_queue_update(self, guild_id: int):
        await self.manager.broadcast(guild_id, self.get_queue_data(guild_id))

    async def pause_resume_local(self, guild_id: int):
        vc = self.bot.get_guild(guild_id).voice_client
        if not vc: return "nada"
        action = "nada"
        if vc.is_playing():
            vc.pause()
            self.pause_times[guild_id] = time.time()
            action = "paused"
        elif vc.is_paused():
            pause_start_time = self.pause_times.pop(guild_id, None)
            if pause_start_time and guild_id in self.start_times:
                self.start_times[guild_id] += (time.time() - pause_start_time)
            vc.resume()
            action = "resumed"
        await self.broadcast_queue_update(guild_id)
        return action

    async def skip_local(self, guild_id: int):
        vc = self.bot.get_guild(guild_id).voice_client
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()

    async def shuffle_local(self, guild_id):
        queue = self.local_queues.get(guild_id)
        if queue and len(queue) > 1:
            random.shuffle(queue)
            await self.broadcast_queue_update(guild_id)
            return True
        return False

    async def toggle_loop_local(self, guild_id):
        current_mode = self.loop_states.get(guild_id, 'off')
        next_mode = {'off': 'queue', 'queue': 'song', 'song': 'off'}.get(current_mode)
        self.loop_states[guild_id] = next_mode
        await self.broadcast_queue_update(guild_id)
        return next_mode

    async def previous_local(self, guild_id):
        vc = self.bot.get_guild(guild_id).voice_client
        history = self.history.get(guild_id)
        if not vc or not history: return False
        
        prev_song = history.pop()
        current = self.current_local.get(guild_id)
        
        if current: self.local_queues.setdefault(guild_id, []).insert(0, current)
        self.local_queues.setdefault(guild_id, []).insert(0, prev_song)
        self.current_local[guild_id] = None 
        
        if vc.is_playing() or vc.is_paused(): vc.stop()
        return True

    async def set_volume_local(self, guild_id: int, volume: int):
        if not 0 <= volume <= 200: return
        new_vol = volume / 100.0
        self.volume_levels[guild_id] = new_vol
        guild = self.bot.get_guild(guild_id)
        if guild and guild.voice_client and guild.voice_client.source: 
            guild.voice_client.source.volume = new_vol
        await self.broadcast_queue_update(guild_id)

    async def remove_local(self, guild_id: int, index: int):
        queue = self.local_queues.get(guild_id)
        if queue and 0 <= index < len(queue): 
            queue.pop(index)
            await self.broadcast_queue_update(guild_id)

    async def skipto_local(self, guild_id: int, index: int):
        queue = self.local_queues.get(guild_id)
        if not queue or not 0 <= index < len(queue): return
        self.local_queues[guild_id] = queue[index:]
        vc = self.bot.get_guild(guild_id).voice_client
        if vc and (vc.is_playing() or vc.is_paused()): vc.stop()
    
    async def move_local(self, guild_id: int, old_index: int, new_index: int):
        queue = self.local_queues.get(guild_id)
        # Verifica se a fila existe e se os índices são válidos
        if not queue or not (0 <= old_index < len(queue)) or not (0 <= new_index < len(queue)): 
            return False
            
        song_to_move = queue.pop(old_index)
        queue.insert(new_index, song_to_move)
        
        await self.broadcast_queue_update(guild_id)
        return True
    # ---------------------------------------------

    async def cleanup_local(self, guild_id):
        self.local_queues.pop(guild_id, None)
        self.current_local.pop(guild_id, None)
        self.history.pop(guild_id, None)
        self.loop_states.pop(guild_id, None)
        self.start_times.pop(guild_id, None)
        self.pause_times.pop(guild_id, None)
        
        msg = self.player_messages.pop(guild_id, None)
        if msg:
            try: await msg.edit(content="Sessão local encerrada. 🏁", embed=None, view=None, attachments=[])
            except Exception: pass
        await self.broadcast_queue_update(guild_id)

    def play_next_local(self, error, text_channel):
        if error: print(f"Erro no player local: {error}")
        self.bot.loop.create_task(self.handle_after_playing(text_channel))

    async def handle_after_playing(self, text_channel):
        guild_id = text_channel.guild.id
        current = self.current_local.get(guild_id)
        
        if current:
            self.history.setdefault(guild_id, deque(maxlen=50)).append(current)
            loop_mode = self.loop_states.get(guild_id, 'off')
            if loop_mode == 'song': self.local_queues.setdefault(guild_id, []).insert(0, current)
            elif loop_mode == 'queue': self.local_queues.setdefault(guild_id, []).append(current)

        await self._play_next_local_task(text_channel)

    def _extract_metadata(self, filepath, filename, requester):
        title = filename.replace('.mp3', '')
        duration = 0
        cover_path = None

        try:
            audio = MP3(filepath)
            duration = int(audio.info.length) if audio.info else 0
            
            if audio.tags:
                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        cover_dir = "./static/covers"
                        if not os.path.exists(cover_dir): os.makedirs(cover_dir)
                        
                        safe_name = hashlib.md5(filename.encode()).hexdigest()
                        cover_path = os.path.join(cover_dir, f"{safe_name}.jpg")
                        
                        if not os.path.exists(cover_path):
                            with open(cover_path, "wb") as img: img.write(tag.data)
                        break 
        except Exception as e:
            pass 

        return {
            'title': title, 'path': filepath, 'thumbnail': cover_path,
            'duration': duration, 'requested_by': requester
        }

    async def _play_next_local_task(self, text_channel):
        guild_id = text_channel.guild.id
        queue = self.local_queues.get(guild_id, [])

        if queue:
            next_song = queue.pop(0)
            self.current_local[guild_id] = next_song
            
            vc = text_channel.guild.voice_client
            if vc and vc.is_connected():
                # ADICIONADO CONTROLE DE VOLUME AQUI
                volume = self.volume_levels.get(guild_id, 1.0)
                source = discord.FFmpegPCMAudio(next_song['path'], **FFMPEG_OPTS_LOCAL)
                player = discord.PCMVolumeTransformer(source, volume=volume)
                
                vc.play(player, after=lambda e: self.play_next_local(e, text_channel))
                
                # Reseta tempo
                self.start_times[guild_id] = time.time()
                self.pause_times.pop(guild_id, None)
                await self.broadcast_queue_update(guild_id)
                
                # --- EMBED NO DISCORD ---
                view = LocalMusicControls(self, text_channel)
                m, s = divmod(next_song['duration'], 60)
                dur_str = f"{m:02d}:{s:02d}" if next_song['duration'] > 0 else "Desconhecido"

                embed = discord.Embed(
                    title="💿 Tocando Local", 
                    description=f"**{next_song['title']}**\n⏳ Duração: `{dur_str}`", 
                    color=discord.Color.green()
                )
                embed.set_footer(text=f"Pedida por: {next_song['requested_by'].display_name}", 
                                 icon_url=next_song['requested_by'].display_avatar.url)

                attachments = []
                if next_song.get('thumbnail') and os.path.exists(next_song['thumbnail']):
                    file = discord.File(next_song['thumbnail'], filename="cover.jpg")
                    embed.set_thumbnail(url="attachment://cover.jpg")
                    attachments.append(file)
                
                old_message = self.player_messages.get(guild_id)
                if old_message:
                    try: await old_message.edit(embed=embed, view=view, attachments=attachments)
                    except discord.NotFound: old_message = None
                
                if not old_message:
                    msg = await text_channel.send(embed=embed, view=view, files=attachments)
                    self.player_messages[guild_id] = msg
        else:
            await self.cleanup_local(guild_id)
            await text_channel.send("A fila local acabou! 🏁")

    @commands.command(name='playl', help="Toca uma música da pasta local.")
    async def playl(self, ctx, *, filename: str):
        if not ctx.author.voice: return await ctx.send("Entre num canal de voz!")
        if not filename.endswith('.mp3'): filename += ".mp3"
        
        path = os.path.join(LOCAL_MUSIC_DIR, filename)
        if not os.path.exists(path):
            return await ctx.send(f"❌ Não achei o arquivo `{filename}`.")

        song_info = self._extract_metadata(path, filename, ctx.author)
        self.local_queues.setdefault(ctx.guild.id, []).append(song_info)

        vc = ctx.guild.voice_client
        if not vc:
            try:
                # Tenta conectar com um timeout maior (60 segundos)
                vc = await ctx.author.voice.channel.connect(timeout=60.0)
            except Exception as e:
                return await ctx.send(f"⏳ O Discord não me deixou entrar na call a tempo. Tente novamente! (Erro: `{type(e).__name__}`)")
        
        if not vc.is_playing() and not vc.is_paused():
            await self._play_next_local_task(ctx.channel)
        else:
            await ctx.send(f"💾 Adicionado à fila local: **{song_info['title']}**", delete_after=5)
            await self.broadcast_queue_update(ctx.guild.id)

    @commands.command(name='playlist', help="Carrega uma playlist .m3u local.")
    async def playlist_local(self, ctx, playlist_name: str):
        if not ctx.author.voice: return await ctx.send("Entre num canal de voz!")
        if not playlist_name.endswith('.m3u'): playlist_name += ".m3u"
        
        path = os.path.join(PLAYLISTS_DIR, playlist_name)
        if not os.path.exists(path):
            return await ctx.send(f"❌ Playlist `{playlist_name}` não encontrada.")

        added = 0
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                
                filename = os.path.basename(line)
                mp3_path = os.path.join(LOCAL_MUSIC_DIR, filename)
                
                if os.path.exists(mp3_path):
                    song_info = self._extract_metadata(mp3_path, filename, ctx.author)
                    self.local_queues.setdefault(ctx.guild.id, []).append(song_info)
                    added += 1

        if added > 0:
            await ctx.send(f"✅ Playlist **{playlist_name}** carregada com {added} músicas!", delete_after=10)
            
            # --- SUBSTITUA DAQUI ---
            vc = ctx.guild.voice_client
            if not vc:
                try:
                    vc = await ctx.author.voice.channel.connect(timeout=60.0)
                except Exception as e:
                    return await ctx.send(f"⏳ Playlist carregada, mas o Discord travou na hora de eu entrar na call. Chame de novo! (Erro: `{type(e).__name__}`)")
            # --- ATÉ AQUI ---

            if not vc.is_playing() and not vc.is_paused():
                await self._play_next_local_task(ctx.channel)
            else:
                await self.broadcast_queue_update(ctx.guild.id)

async def setup(bot):
    await bot.add_cog(MusicLocalCog(bot))