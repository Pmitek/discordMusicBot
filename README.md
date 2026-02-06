# discordMusicBot

## How to avoid YouTube 403 / age / region blocks

The bot now resolves a fresh stream URL right before playback and passes yt-dlp's HTTP
headers to ffmpeg. If you still hit `403 Forbidden` on some tracks:

- Keep `yt-dlp` fresh: `pip install -U yt-dlp`
- Provide cookies so requests look like your logged-in browser session:
  - Recommended (works well with Docker): export cookies to a `cookies.txt` file
    (Netscape format) and set `YTDL_COOKIES=/path/to/cookies.txt`.
  - Alternative (only when the bot has access to the browser profile on disk):
    set `YTDL_BROWSER=chrome` (also `firefox`/`brave`) to let yt-dlp read cookies
    directly from the browser profile via `cookiesfrombrowser`.
- Optionally set `COOKIES_TXT` (alias of `YTDL_COOKIES`).

After setting env vars, restart the bot. Cookies help with age-gated / region-restricted
videos and reduce `HTTP error 403` during streaming.

### Getting `cookies.txt`

1) Log in to YouTube in your normal browser (Chrome/Firefox/Brave).
2) Use a browser extension that exports cookies in Netscape `cookies.txt` format.
3) Export cookies for `youtube.com` and save as `cookies.txt`.
4) Copy the file to the server/container and set `YTDL_COOKIES` to that path.

Security note: treat `cookies.txt` like a password (don't commit it to git).
