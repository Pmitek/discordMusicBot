FROM python:3.11-slim


ENV PYTHONDONTWRITEBYTECODE=1 \
PYTHONUNBUFFERED=1 \
PIP_NO_CACHE_DIR=1


# System deps
RUN apt-get update && \
apt-get install -y --no-install-recommends ffmpeg && \
rm -rf /var/lib/apt/lists/*


WORKDIR /app


# Install Python deps (pin to maintained libs)
RUN pip install --no-cache-dir "discord.py~=2.4" "yt-dlp>=2024.12.13"


# Create non-root user
RUN useradd -u 10001 -m botuser


# Copy app
COPY discord_music_bot.py /app/


USER botuser


# The bot reads DISCORD_TOKEN from the environment
CMD ["python", "discord_music_bot.py"]