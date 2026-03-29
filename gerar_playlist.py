import os

# Configurações
PASTA_MUSICAS = "./musicas"
PASTA_PLAYLISTS = "./playlists"
NOME_PLAYLIST = "trindas" # O nome que você quer

def reconstruir_playlist():
    # Cria a pasta playlists se não existir
    if not os.path.exists(PASTA_PLAYLISTS):
        os.makedirs(PASTA_PLAYLISTS)

    # Pega todos os arquivos .mp3 da pasta
    try:
        arquivos = [f for f in os.listdir(PASTA_MUSICAS) if f.endswith(".mp3")]
    except FileNotFoundError:
        print(f"❌ Erro: Pasta '{PASTA_MUSICAS}' não encontrada!")
        return

    # Opcional: Ordenar alfabeticamente para ficar bonitinho
    arquivos.sort()

    caminho_arquivo = os.path.join(PASTA_PLAYLISTS, f"{NOME_PLAYLIST}.m3u")

    with open(caminho_arquivo, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n") # Cabeçalho obrigatório
        
        for arquivo in arquivos:
            # Tira o .mp3 do nome para usar como Título no player
            titulo = os.path.splitext(arquivo)[0]
            
            f.write(f"#EXTINF:-1,{titulo}\n") # Metadados
            f.write(f"{arquivo}\n")           # Nome do arquivo

    print(f"✅ Playlist RECONSTRUÍDA com sucesso!")
    print(f"📄 Arquivo: {caminho_arquivo}")
    print(f"🎵 Total de músicas: {len(arquivos)}")
    input("PTF")

if __name__ == "__main__":
    reconstruir_playlist()