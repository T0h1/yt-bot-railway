import os
import sys
import subprocess

# Install dependencies if needed
def install_deps():
    try:
        import telegram
        import yt_dlp
        import mutagen
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])

install_deps()

# Import and run the bot
from youtube_downloader_bot import main

if __name__ == "__main__":
    main()
