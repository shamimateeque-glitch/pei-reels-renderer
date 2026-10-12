# Discovering PEI: reel renderer (GitHub Actions)

n8n starts this workflow for each reel. It makes the voiceover (Microsoft Edge's free neural voices),
burns in captions, renders a 1080x1920 MP4 with FFmpeg, and uploads it to the `reels` release.
Videos older than 3 days are deleted automatically.

GitHub Actions is free for public repositories.
