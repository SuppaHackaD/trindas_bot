# main.py
import asyncio, uvicorn, os, json
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from contextlib import asynccontextmanager
from requests_oauthlib import OAuth2Session
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

from discord_bot import bot
from music_cog import MusicCog
from music_local_cog import MusicLocalCog
from help_cog import HelpCog
from config import DISCORD_TOKEN, OAUTH2_CLIENT_ID, OAUTH2_CLIENT_SECRET, SERVER_IP

from mumble_cog import MumbleCog

from fastapi.responses import FileResponse

API_BASE_URL = 'https://discord.com/api/v10'
AUTHORIZATION_BASE_URL = 'https://discord.com/api/oauth2/authorize'
TOKEN_URL = 'https://discord.com/api/oauth2/token'
REDIRECT_URI = 'https://virginia-handed-exceptions-cure.trycloudflare.com/callback' 


# --- ADICIONE ESTA LINHA ---
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
# ---------------------------


class ConnectionManager:
    def __init__(self): self.active_connections: dict[int, set[WebSocket]] = {}
    async def connect(self, websocket: WebSocket, guild_id: int): await websocket.accept(); self.active_connections.setdefault(guild_id, set()).add(websocket)
    def disconnect(self, websocket: WebSocket, guild_id: int):
        if guild_id in self.active_connections: self.active_connections[guild_id].remove(websocket)
    async def broadcast(self, guild_id: int, message: dict):
        if guild_id in self.active_connections:
            for connection in self.active_connections[guild_id]: await connection.send_json(message)

manager = ConnectionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Carregando Cogs e iniciando o bot..."); bot.manager = manager
    
    # --- MUDANÇA AQUI: Adicionei o MusicLocalCog na lista ---
    await bot.add_cog(MusicCog(bot))
    await bot.add_cog(HelpCog(bot))
    await bot.add_cog(MusicLocalCog(bot))
    await bot.add_cog(MumbleCog(bot))
    # --------------------------------------------------------
    
    asyncio.create_task(bot.start(DISCORD_TOKEN))
    yield
    print("Desligando..."); await bot.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.urandom(24), max_age=2592000)
app.mount("/static", StaticFiles(directory="static"), name="static")

class PlayPayload(BaseModel): query: str
class MoveSongPayload(BaseModel): old_index: int; new_index: int
class IndexPayload(BaseModel): index: int
class VolumePayload(BaseModel): volume: int

@app.get("/login")
async def login():
    scope = ["identify", "guilds"]; discord_session = OAuth2Session(OAUTH2_CLIENT_ID, redirect_uri=REDIRECT_URI, scope=scope)
    authorization_url, state = discord_session.authorization_url(AUTHORIZATION_BASE_URL)
    return RedirectResponse(authorization_url)

@app.get("/callback")
async def callback(request: Request):
    try:
        discord_session = OAuth2Session(OAUTH2_CLIENT_ID, redirect_uri=REDIRECT_URI)
        loop = asyncio.get_event_loop()
        token = await loop.run_in_executor(None, lambda: discord_session.fetch_token(TOKEN_URL, authorization_response=str(request.url), client_secret=OAUTH2_CLIENT_SECRET))
        request.session['oauth2_token'] = token
        user_response = discord_session.get(f'{API_BASE_URL}/users/@me')
        if user_response.ok: request.session['user_id'] = user_response.json()['id']
        return RedirectResponse("/")
    except Exception as e: print(f"ERRO CALLBACK: {e}"); raise HTTPException(status_code=500, detail="Erro na autenticação.")

async def get_validated_member(request: Request, guild_id: int, check_voice=True):
    token = request.session.get('oauth2_token')
    if not token: raise HTTPException(status_code=401, detail="Não autenticado")
    user_id = request.session.get('user_id')
    if not user_id: raise HTTPException(status_code=403, detail="Sessão de usuário inválida.")
    guild = bot.get_guild(guild_id)
    if not guild: raise HTTPException(status_code=404, detail="Bot não está neste servidor.")
    member = guild.get_member(int(user_id))
    if check_voice and (not member or not member.voice or not member.voice.channel):
        raise HTTPException(status_code=403, detail="Você não está conectado a um canal de voz.")
    return member

@app.get("/api/me")
async def get_current_user(request: Request):
    token = request.session.get('oauth2_token')
    if not token: raise HTTPException(status_code=401, detail="Não autenticado")
    discord_session = OAuth2Session(OAUTH2_CLIENT_ID, token=token)
    user_data = discord_session.get(f'{API_BASE_URL}/users/@me').json()
    guilds_data = discord_session.get(f'{API_BASE_URL}/users/@me/guilds').json()
    bot_guild_ids = {g.id for g in bot.guilds}
    admin_guilds = [{"id": str(g['id']), "name": g['name']} for g in guilds_data if (int(g['permissions']) & 0x8) == 0x8 and int(g['id']) in bot_guild_ids]
    return {"user": user_data, "guilds": admin_guilds}

# --- ROTEADOR INTELIGENTE (Decide se usa o Mumble, Local ou Online) ---
def get_active_cog(guild_id: int):
    # 1. Checa o Mumble primeiro (Se tiver música na fila ou tocando, ele assume o painel)
    mumble_cog = bot.get_cog("Mumble Integration")
    if mumble_cog and (mumble_cog.is_playing or mumble_cog.mumble_queue):
        return mumble_cog, "mumble"

    # 2. Verifica se tem algo tocando no Local
    local_cog = bot.get_cog("Música Local")
    if local_cog and (local_cog.current_local.get(guild_id) or local_cog.local_queues.get(guild_id)):
        return local_cog, "local"
    
    # 3. Se não, cai pro Online (Padrão)
    return bot.get_cog("Música"), "online"

@app.get("/api/guilds/{guild_id}/queue")
async def get_guild_queue(guild_id: int):
    cog, _ = get_active_cog(guild_id)
    if not cog: raise HTTPException(status_code=500, detail="Music Cog não carregado.")
    return cog.get_queue_data(guild_id)

@app.post("/api/guilds/{guild_id}/control/{action}")
async def player_control(request: Request, guild_id: int, action: str):
    cog, cog_type = get_active_cog(guild_id)

    precisa_estar_na_call = (cog_type != "mumble")
    await get_validated_member(request, guild_id, check_voice=precisa_estar_na_call)
    
    # Mapeamento dinâmico dependendo de qual Cog está tocando
    if cog_type == "mumble":
        actions = { 
            "pause-resume": cog.pause_resume_mumble, "skip": cog.skip_mumble, 
            "previous": cog.previous_mumble, "shuffle": cog.shuffle_mumble, 
            "toggle-loop": cog.toggle_loop_mumble, "leave": cog.leave_mumble 
        }
    elif cog_type == "local":
        actions = { 
            "pause-resume": cog.pause_resume_local, "skip": cog.skip_local, 
            "previous": cog.previous_local, "shuffle": cog.shuffle_local, 
            "toggle-loop": cog.toggle_loop_local, "leave": cog.cleanup_local 
        }
    else:
        actions = { 
            "pause-resume": cog.pause_resume_from_web, "skip": cog.skip_from_web, 
            "previous": cog.previous_from_web, "shuffle": cog.shuffle_from_web, 
            "toggle-loop": cog.toggle_loop_from_web, "leave": cog.cleanup 
        }

    if action not in actions: raise HTTPException(status_code=400, detail="Ação inválida")
    
    # "leave" precisa receber o objeto guild, o resto recebe só o ID
    if action == "leave":
        guild = bot.get_guild(guild_id)
        # O cleanup do local só pede guild_id, o do online pede a Guild. Ajustando:
        result = await actions[action](guild_id) if cog_type == "local" else await actions[action](guild)
    else:
        result = await actions[action](guild_id)
        
    return {"status": "success", "action": action, "result": result}

@app.post("/api/guilds/{guild_id}/play")
async def play_song(request: Request, guild_id: int, payload: PlayPayload):
    member = await get_validated_member(request, guild_id)
    # Sempre usa o cog Online para pedidos feitos pela barra de busca do site
    music_cog = bot.get_cog("Música") 
    await music_cog.play_from_web(guild=bot.get_guild(guild_id), member=member, query=payload.query)
    return {"status": "success", "action": "play_request_sent"}

@app.post("/api/guilds/{guild_id}/volume")
async def set_volume(request: Request, guild_id: int, payload: VolumePayload):
    cog, cog_type = get_active_cog(guild_id)
    # Libera a barreira do Discord se for o Mumble
    await get_validated_member(request, guild_id, check_voice=(cog_type != "mumble"))
    
    if cog_type == "mumble": await cog.set_volume_mumble(guild_id, payload.volume)
    elif cog_type == "local": await cog.set_volume_local(guild_id, payload.volume)
    else: await cog.set_volume_from_web(guild_id, payload.volume)
    return {"status": "success", "action": "volume_set"}

@app.post("/api/guilds/{guild_id}/queue/move")
async def move_song_in_queue(request: Request, guild_id: int, payload: MoveSongPayload):
    cog, cog_type = get_active_cog(guild_id)
    await get_validated_member(request, guild_id, check_voice=False)
    
    # Agora sim ele direciona pro comando certo do Mumble!
    if cog_type == "mumble": 
        success = await cog.move_mumble(guild_id, payload.old_index, payload.new_index)
    elif cog_type == "local": 
        success = await cog.move_local(guild_id, payload.old_index, payload.new_index)
    else: 
        success = await cog.move_song(guild_id, payload.old_index, payload.new_index)
        
    if not success:
        raise HTTPException(status_code=400, detail="Índices inválidos.")
    return {"status": "success", "action": "move"}

@app.post("/api/guilds/{guild_id}/skipto")
async def skipto_song(request: Request, guild_id: int, payload: IndexPayload):
    cog, cog_type = get_active_cog(guild_id)
    await get_validated_member(request, guild_id, check_voice=(cog_type != "mumble"))
    
    if cog_type == "mumble": await cog.skipto_mumble(guild_id, payload.index)
    elif cog_type == "local": await cog.skipto_local(guild_id, payload.index)
    else: await cog.skipto_from_web(guild_id, payload.index)
    return {"status": "success", "action": "skipto"}

@app.post("/api/guilds/{guild_id}/remove")
async def remove_song(request: Request, guild_id: int, payload: IndexPayload):
    await get_validated_member(request, guild_id, check_voice=False) 
    cog, cog_type = get_active_cog(guild_id)
    
    if cog_type == "local": await cog.remove_local(guild_id, payload.index)
    else: await cog.remove_from_web(guild_id, payload.index)
    return {"status": "success", "action": "remove"}

@app.websocket("/ws/{guild_id}")
async def websocket_endpoint(websocket: WebSocket, guild_id: int):
    await manager.connect(websocket, guild_id)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect: manager.disconnect(websocket, guild_id)

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)


@app.post("/api/guilds/{guild_id}/queue/move")
async def move_song_in_queue(request: Request, guild_id: int, payload: MoveSongPayload):
    await get_validated_member(request, guild_id, check_voice=False)
    cog, cog_type = get_active_cog(guild_id)
    
    if cog_type == "mumble":
        success = await cog.move_mumble(guild_id, payload.old_index, payload.new_index)
    elif cog_type == "local": 
        success = await cog.move_local(guild_id, payload.old_index, payload.new_index)
    else: 
        success = await cog.move_song(guild_id, payload.old_index, payload.new_index)
        
    if not success:
        raise HTTPException(status_code=400, detail="Índices inválidos.")
    return {"status": "success", "action": "move"}

@app.post("/api/guilds/{guild_id}/skipto")
async def skipto_song(request: Request, guild_id: int, payload: IndexPayload):
    # Ignora verificação de voz se for Mumble
    cog, cog_type = get_active_cog(guild_id)
    await get_validated_member(request, guild_id, check_voice=(cog_type != "mumble"))
    
    if cog_type == "mumble": await cog.skipto_mumble(guild_id, payload.index)
    elif cog_type == "local": await cog.skipto_local(guild_id, payload.index)
    else: await cog.skipto_from_web(guild_id, payload.index)
    return {"status": "success", "action": "skipto"}

@app.post("/api/guilds/{guild_id}/remove")
async def remove_song(request: Request, guild_id: int, payload: IndexPayload):
    await get_validated_member(request, guild_id, check_voice=False) 
    cog, cog_type = get_active_cog(guild_id)
    
    if cog_type == "mumble": await cog.remove_mumble(guild_id, payload.index)
    elif cog_type == "local": await cog.remove_local(guild_id, payload.index)
    else: await cog.remove_from_web(guild_id, payload.index)
    return {"status": "success", "action": "remove"}



@app.get("/Trindas_logo.jfif", include_in_schema=False)
async def serve_logo():
    # Isso assume que você colocou a imagem dentro da pasta static/
    return FileResponse("static/Trindas_logo.jfif")