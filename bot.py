"""Main entry point for the Media Bot."""

import sys
import os
import asyncio

# Add current directory to path for local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from youtube_downloader_bot import main

if __name__ == "__main__":
    asyncio.run(main())