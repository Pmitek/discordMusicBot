# discordMusicBot

## How to avoid YouTube 403 / age / region blocks

The bot now resolves a fresh stream URL right before playback and passes yt-dlp's HTTP
headers to ffmpeg. If you still hit `403 Forbidden` on some tracks:

- Keep `yt-dlp` fresh: `pip install -U yt-dlp`
- Provide cookies so requests look like your logged-in browser session:
  - Export `YTDL_COOKIES=/path/to/cookies.txt` (Netscape/yt-dlp format)
    - or `YTDL_BROWSER=chrome` (also `firefox`/`brave`) to let yt-dlp read cookies
    directly from your browser profile via `cookiesfrombrowser`.
- Optionally set `COOKIES_TXT` (alias of `YTDL_COOKIES`).

After setting env vars, restart the bot. Cookies help with age-gated / region-restricted
videos and reduce `HTTP error 403` during streaming.
