import yt_dlp
import os
import re

# --- CONFIGURAÇÕES ---
DESTINO_MUSICAS = "./musicas"
DESTINO_PLAYLISTS = "./playlists"
ARQUIVO_HISTORICO = "download_history.txt"
COOKIES_FILE = "cookies.txt" # Se tiver, ele usa. Se não, vai sem.

def sanitize_filename(name):
    """Limpa caracteres proibidos no Windows"""
    return re.sub(r'[<>:"/\\|?*]', '', name)

def run_downloader(url, playlist_name=None):
    if not os.path.exists(DESTINO_MUSICAS): os.makedirs(DESTINO_MUSICAS)
    if not os.path.exists(DESTINO_PLAYLISTS): os.makedirs(DESTINO_PLAYLISTS)

    print(f"🚀 Iniciando download da playlist: {playlist_name}")
    print("⏳ Isso vai baixar o .webm (rápido) e converter para .mp3 (processamento)...")

    ydl_opts = {
        'format': 'bestaudio/best', 
        'outtmpl': f'{DESTINO_MUSICAS}/%(title)s.%(ext)s',
        
        # --- CONFIGURAÇÃO ANTI-BAN TOTAL ---
        
        # Dorme entre 2 e 5 segundos antes de CHECAR qualquer música
        # (Isso impede que ele verifique as 500 músicas em 1 segundo)
        'sleep_interval_requests': 2,
        
        # Dorme entre 5 e 15 segundos depois de BAIXAR uma música
        'sleep_interval': 5,
        'max_sleep_interval': 15,
        # -----------------------------------

        'download_archive': ARQUIVO_HISTORICO,
        'ignoreerrors': True,
        'no_warnings': True,
        
        'postprocessors': [
            {
                'key': 'SponsorBlock',
                'categories': ['sponsor', 'intro', 'outro', 'selfpromo', 'interaction'],
            },
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            },
            {
                'key': 'EmbedThumbnail',
            },
            {
                'key': 'FFmpegMetadata',
                'add_metadata': True,
            }
        ],
        'writethumbnail': True,
        'cookiefile': None, # Modo Anônimo (Melhor pra evitar bloqueio de conta)
        
        'replace_in_metadata': [
            {'key': 'title', 'regex': r'(?i)\s*[\[\(](official|video|lyrics|audio|4k|hd).*?[\]\)]', 'replace': ''}
        ]
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
            if playlist_name:
                print(f"\n📝 Gerando playlist: {playlist_name}.m3u")
                generate_m3u(info, playlist_name)
        except Exception as e:
            print(f"\n❌ Erro: {e}")

def generate_m3u(info, name):
    m3u_path = os.path.join(DESTINO_PLAYLISTS, f"{name}.m3u")
    
    # Se for single, transforma em lista pra não quebrar o loop
    entries = info['entries'] if 'entries' in info else [info]

    with open(m3u_path, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        count = 0
        for entry in entries:
            if not entry: continue
            
            # Recria a lógica de limpeza de nome pra achar o arquivo certo
            title = entry.get('title', 'Unknown')
            clean_title = re.sub(r'(?i)\s*[\[\(](official|video|lyrics|audio|4k|hd).*?[\]\)]', '', title)
            clean_title = sanitize_filename(clean_title).strip()
            
            # O arquivo final será .mp3 (pois o FFmpeg converteu)
            filename = f"{clean_title}.mp3"
            
            # Caminho relativo para o bot ler
            # Se o bot rodar na raiz, ele vai procurar em musicas/arquivo.mp3
            # Mas no m3u a gente costuma por só o nome se o player for esperto.
            # Vamos por o nome puro, e o seu código Python do bot já sabe procurar na pasta 'musicas'
            f.write(f"#EXTINF:-1,{title}\n")
            f.write(f"{filename}\n")
            count += 1
                
    print(f"✅ Playlist finalizada! {count} músicas prontas em: {m3u_path}")

    input("pressione enter para fechar")

if __name__ == "__main__":
    # Pode deixar fixo pra facilitar seus testes ou usar input
    url = input("URL da Playlist: ").strip()
    nome = input("Nome da Playlist (sem espaços): ").strip()
    run_downloader(url, nome)