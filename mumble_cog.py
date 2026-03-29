import discord
from discord.ext import commands
import asyncio
import os
import ssl
# --- PATCH PARA O PYTHON 3.12 ---
# Recria a função wrap_socket que foi removida, usando o método moderno
if not hasattr(ssl, 'wrap_socket'):
    def _wrap_socket(sock, certfile=None, keyfile=None, ssl_version=None):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        if certfile:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        return context.wrap_socket(sock)
    ssl.wrap_socket = _wrap_socket
# --------------------------------
import pymumble_py3

# Precisamos das variáveis do Playit/Mumble. Adicione no seu config.py depois.
MUMBLE_HOST = "198.22.204.25"
MUMBLE_PORT = 5440 # A porta que o playit te deu
MUMBLE_USER = "TrindasBot"
MUMBLE_PASS = "Trindas4ever" # Se o servidor Mumble tiver senha
LOCAL_MUSIC_DIR = "./musicas"

class MumbleCog(commands.Cog, name="Mumble Integration"):
    def __init__(self, bot):
        self.bot = bot
        self.manager = getattr(bot, 'manager', None)
        self.mumble = None
        self.is_playing = False
        self.current_process = None
        self.mumble_queue = [] 
        self.current_song = None

    @commands.command(name='mconnect', help="Conecta o bot ao servidor Mumble.")
    async def mconnect(self, ctx):
        if self.mumble and self.mumble.is_alive():
            return await ctx.send("🔊 Já estou conectado ao Mumble!")

        msg = await ctx.send("⏳ Conectando ao Mumble via túnel...")
        try:
            loop = asyncio.get_event_loop()
            self.mumble = pymumble_py3.Mumble(MUMBLE_HOST, MUMBLE_USER, port=MUMBLE_PORT, password=MUMBLE_PASS)
            self.mumble.set_receive_sound(False)
            
            await loop.run_in_executor(None, self.mumble.start)
            
            conectado = False
            for _ in range(50):
                if not self.mumble.is_alive(): break
                if hasattr(self.mumble.users, 'myself_session') and self.mumble.users.myself_session is not None:
                    conectado = True
                    break
                await asyncio.sleep(0.2)
                
            if not conectado:
                return await msg.edit(content="❌ Falha na conexão! O servidor recusou ou a thread morreu.")

            self.mumble.sound_output.set_audio_per_packet(0.02)
            await msg.edit(content="✅ Conectado ao Mumble com sucesso!")
        except Exception as e:
            await msg.edit(content=f"❌ Erro ao conectar no Mumble: `{e}`")

    @commands.command(name='mplay', help="Adiciona uma música local na fila do Mumble.")
    async def mplay(self, ctx, *, filename: str):
        if not self.mumble or not self.mumble.is_alive():
            return await ctx.send("❌ Use `!mconnect` primeiro!")

        if not filename.endswith('.mp3'): filename += ".mp3"
        path = os.path.join(LOCAL_MUSIC_DIR, filename)
        
        if not os.path.exists(path): return await ctx.send(f"❌ Arquivo `{filename}` não encontrado.")

        self.mumble_queue.append({'title': filename, 'path': path, 'requester': ctx.author.display_name})
        await ctx.send(f"💾 Adicionado à fila do Mumble: **{filename}**")

        if not self.is_playing:
            self.bot.loop.create_task(self._play_next_mumble_task(ctx))
        else:
            await self.broadcast_queue_update(ctx.guild.id)

    @commands.command(name='mplaylist', help="Carrega uma playlist .m3u local pro Mumble.")
    async def mplaylist(self, ctx, playlist_name: str):
        if not self.mumble or not self.mumble.is_alive():
            return await ctx.send("❌ Use `!mconnect` primeiro!")

        if not playlist_name.endswith('.m3u'): playlist_name += ".m3u"
        path = os.path.join("./playlists", playlist_name)
        
        if not os.path.exists(path): return await ctx.send(f"❌ Playlist `{playlist_name}` não encontrada.")

        added = 0
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'): continue
                
                filename = os.path.basename(line)
                mp3_path = os.path.join(LOCAL_MUSIC_DIR, filename)
                if os.path.exists(mp3_path):
                    self.mumble_queue.append({'title': filename, 'path': mp3_path, 'requester': ctx.author.display_name})
                    added += 1

        if added > 0:
            await ctx.send(f"✅ Playlist **{playlist_name}** ({added} músicas) carregada no Mumble!")
            if not self.is_playing:
                self.bot.loop.create_task(self._play_next_mumble_task(ctx))
            else:
                await self.broadcast_queue_update(ctx.guild.id)

    async def _play_next_mumble_task(self, ctx):
        if not self.mumble_queue:
            self.is_playing = False
            self.current_song = None
            await self.broadcast_queue_update(ctx.guild.id)
            await ctx.send("🏁 A fila do Mumble acabou!")
            return

        self.is_playing = True
        self.current_song = self.mumble_queue.pop(0)
        await self.broadcast_queue_update(ctx.guild.id)
        await ctx.send(f"▶️ Tocando agora no Mumble: **{self.current_song['title']}**")

        cmd = ['ffmpeg', '-i', self.current_song['path'], '-ac', '1', '-ar', '48000', '-f', 's16le', '-loglevel', 'error', 'pipe:1']
        self.current_process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

        try:
            while self.is_playing:
                if self.mumble.sound_output.get_buffer_size() > 0.5:
                    await asyncio.sleep(0.01)
                    continue

                data = await self.current_process.stdout.read(1920)
                if not data: break 
                
                self.mumble.sound_output.add_sound(data)
                await asyncio.sleep(0.001) 
        except Exception as e:
            print(f"Erro no stream do Mumble: {e}")
        finally:
            if self.current_process:
                try: self.current_process.kill()
                except: pass
            
            if self.is_playing:
                self.bot.loop.create_task(self._play_next_mumble_task(ctx))

    @commands.command(name='mskip', help="Pula a música atual no Mumble.")
    async def mskip(self, ctx):
        await self.skip_mumble(ctx.guild.id)
        await ctx.send("⏩ Pulando música no Mumble...")

    @commands.command(name='mstop', help="Para a fila inteira no Mumble.")
    async def mstop(self, ctx):
        self.mumble_queue.clear()
        self.is_playing = False
        if self.current_process:
            try: self.current_process.kill()
            except: pass
        await self.broadcast_queue_update(ctx.guild.id)
        await ctx.send("🛑 Fila do Mumble parada e limpa.")

    @commands.command(name='mleave', help="Desconecta do Mumble.")
    async def mleave(self, ctx):
        await self.leave_mumble(ctx.guild.id)
        await ctx.send("👋 Desconectado do Mumble.")

    # ==========================================
    # --- INTEGRAÇÃO COM A DASHBOARD WEB ---
    # ==========================================
    def get_queue_data(self, guild_id: int):
        def serialize_song(song):
            if not song: return None
            return {
                "title": song.get('title', 'Carregando...'), "webpage_url": "#", 
                "thumbnail": "", "duration": 0, "requester": song.get('requester', 'Mumble User')
            }
        return {
            "guild_id": guild_id, "current_song": serialize_song(self.current_song), 
            "queue": [serialize_song(s) for s in self.mumble_queue], 
            "loop_mode": "off", "volume": 100, "is_paused": False, "elapsed": 0
        }

    async def broadcast_queue_update(self, guild_id: int):
        if self.manager: await self.manager.broadcast(guild_id, self.get_queue_data(guild_id))

    async def pause_resume_mumble(self, guild_id: int): return "nada" # Desativado no Mumble
    async def toggle_loop_mumble(self, guild_id: int): return "off"
    async def previous_mumble(self, guild_id: int): return False
    async def set_volume_mumble(self, guild_id: int, volume: int): return True

    async def skip_mumble(self, guild_id: int):
        if self.is_playing and self.current_process: self.current_process.kill()
        return "skipped"

    async def shuffle_mumble(self, guild_id: int):
        import random
        if len(self.mumble_queue) > 1:
            random.shuffle(self.mumble_queue)
            await self.broadcast_queue_update(guild_id)
            return True
        return False

    async def skipto_mumble(self, guild_id: int, index: int):
        if 0 <= index < len(self.mumble_queue):
            self.mumble_queue = self.mumble_queue[index:]
            if self.is_playing and self.current_process: self.current_process.kill()
        return "skipto"

    async def move_mumble(self, guild_id: int, old_index: int, new_index: int):
        if (0 <= old_index < len(self.mumble_queue)) and (0 <= new_index < len(self.mumble_queue)):
            song = self.mumble_queue.pop(old_index)
            self.mumble_queue.insert(new_index, song)
            await self.broadcast_queue_update(guild_id)
            return True
        return False

    async def remove_mumble(self, guild_id: int, index: int):
        if 0 <= index < len(self.mumble_queue):
            self.mumble_queue.pop(index)
            await self.broadcast_queue_update(guild_id)

    async def leave_mumble(self, guild_id_ou_guild):
        self.mumble_queue.clear()
        self.is_playing = False
        if self.current_process:
            try: self.current_process.kill()
            except: pass
        if self.mumble and self.mumble.is_alive(): self.mumble.stop()
        g_id = getattr(guild_id_ou_guild, 'id', guild_id_ou_guild)
        await self.broadcast_queue_update(g_id)
        return "left"

async def setup(bot):
    await bot.add_cog(MumbleCog(bot))