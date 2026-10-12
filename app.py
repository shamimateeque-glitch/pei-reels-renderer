"""PEI Reels renderer: free voiceover (Edge neural TTS) + FFmpeg video with word-highlight captions.

POST /render   JSON job  -> {"id", "file", "duration"}
GET  /files/<name>       -> the MP4
GET  /health
"""
import asyncio, json, os, re, shutil, subprocess, time, urllib.request, uuid
from pathlib import Path
from aiohttp import web
import edge_tts

OUT = Path(os.environ.get("OUT_DIR", "/data/reels"))
WORK = Path(os.environ.get("WORK_DIR", "/tmp/pei-work"))
KEY = os.environ.get("RENDER_KEY", "")
KEEP_HOURS = float(os.environ.get("KEEP_HOURS", "72"))
FONT = os.environ.get("CAPTION_FONT", "Montserrat")
W, H, FPS = 1080, 1920, 30
OUT.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
LOCK = asyncio.Lock()


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-1500:]}")
    return p.stdout


def probe_duration(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]).strip())


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PEI-Reels-Renderer)"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    if dest.stat().st_size < 10_000:
        raise RuntimeError(f"Download too small: {url}")


PIPER_DIR = Path(os.environ.get("PIPER_DIR", "voices"))
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
_piper = None


def piper_synthesize(text, path):
    """Offline fallback voice (Piper). Writes a WAV and estimates word timings per sentence."""
    global _piper
    import wave
    from piper import PiperVoice, SynthesisConfig
    if _piper is None:
        _piper = PiperVoice.load(str(PIPER_DIR / f"{PIPER_VOICE}.onnx"))
    cfg = SynthesisConfig(length_scale=0.95)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    words, t = [], 0.0
    gap = 0.18
    with wave.open(str(path), "wb") as wf:
        rate = None
        for si, sent in enumerate(sentences):
            pcm = b""
            for chunk in _piper.synthesize(sent, syn_config=cfg):
                if rate is None:
                    rate = chunk.sample_rate
                    wf.setframerate(rate); wf.setsampwidth(2); wf.setnchannels(1)
                pcm += chunk.audio_int16_bytes
            if not pcm:
                continue
            wf.writeframes(pcm)
            dur = len(pcm) / 2 / rate
            ws = sent.split()
            weights = [len(w) + 2 for w in ws]
            total = sum(weights) or 1
            pos = t + 0.05
            span = max(dur - 0.12, 0.1)
            for w, wt in zip(ws, weights):
                d = span * wt / total
                words.append((pos, pos + d * 0.9, w))
                pos += d
            t += dur
            if si < len(sentences) - 1:
                wf.writeframes(b"\x00\x00" * int(rate * gap))
                t += gap
    if not words:
        raise RuntimeError("Piper produced no audio")
    return words


async def synthesize(text, voice, rate, path):
    """Returns (word timings [(start, end, word)], audio_path).
    Tries Microsoft Edge neural voices first; falls back to the offline Piper voice."""
    last = None
    for attempt in range(2):
        try:
            words, audio = [], bytearray()
            comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
            async def collect():
                async for ch in comm.stream():
                    if ch["type"] == "audio":
                        audio.extend(ch["data"])
                    elif ch["type"] == "WordBoundary":
                        s = ch["offset"] / 1e7
                        words.append((s, s + ch["duration"] / 1e7, ch["text"]))
            await asyncio.wait_for(collect(), 60)
            if not audio or not words:
                raise RuntimeError("Edge TTS returned no audio")
            path.write_bytes(bytes(audio))
            return words, path
        except Exception as e:  # noqa
            last = e
            await asyncio.sleep(3)
    print(f"Edge voice unavailable ({str(last)[:120]}); using offline Piper voice", flush=True)
    wav = path.with_suffix(".wav")
    words = await asyncio.to_thread(piper_synthesize, text, wav)
    return words, wav


def ass_color(hex_rgb, alpha="00"):
    h = hex_rgb.lstrip("#")
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def esc(s):
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def build_ass(timed_words, hook, hook_end, handle, total, accent, highlight):
    acc = ass_color(accent)
    hil = ass_color(highlight)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{FONT},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,5,80,80,0,1",
        f"Style: Hook,{FONT},96,&H00FFFFFF,&H00FFFFFF,{acc},&H00000000,-1,0,0,0,100,100,0,0,3,22,0,8,90,90,330,1",
        f"Style: Handle,{FONT},38,&H40FFFFFF,&H40FFFFFF,&H80000000,&H00000000,-1,0,0,0,100,100,1,0,1,2,0,8,40,40,110,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if handle:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Handle,,0,0,0,,{esc(handle)}")
    if hook:
        lines.append(f"Dialogue: 1,{ass_time(0.05)},{ass_time(hook_end)},Hook,,0,0,0,,{{\\fad(200,250)}}{esc(hook).upper()}")
    # group words into short pages (max 3 words / 18 chars), break on punctuation
    pages, cur = [], []
    for w in timed_words:
        cur.append(w)
        chars = sum(len(x[2]) + 1 for x in cur)
        if len(cur) >= 3 or chars >= 18 or re.search(r"[.!?,;:]$", w[2]) or w[3]:
            pages.append(cur); cur = []
    if cur: pages.append(cur)
    for pi, page in enumerate(pages):
        page_end = page[-1][1] + 0.15
        if pi + 1 < len(pages):
            page_end = min(max(page_end, page[-1][1]), pages[pi + 1][0][0])
        for i, (s, e, _, _) in enumerate(page):
            end = page[i + 1][0] if i + 1 < len(page) else page_end
            if end <= s: continue
            parts = []
            for j, (_, _, txt, _) in enumerate(page):
                t = esc(txt).upper()
                parts.append(f"{{\\c{hil}\\fscx112\\fscy112}}{t}{{\\c&H00FFFFFF&\\fscx100\\fscy100}}" if j == i else t)
            lines.append(f"Dialogue: 2,{ass_time(s)},{ass_time(end)},Caption,,0,0,0,,{{\\pos({W//2},{int(H*0.70)})}}" + " ".join(parts))
    return "\n".join(lines) + "\n"


async def render(job):
    rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    wd = WORK / rid
    wd.mkdir(parents=True)
    try:
        scenes = [s for s in job.get("scenes", []) if str(s.get("voiceover", "")).strip() and s.get("video_url")]
        if not scenes: raise ValueError("job.scenes is empty")
        voice = job.get("voice") or "en-CA-ClaraNeural"
        rate = job.get("rate") or "+5%"
        gap = 0.25
        timed, durs, vo_files, clip_files = [], [], [], []
        t0 = 0.0
        for i, sc in enumerate(scenes):
            mp3 = wd / f"vo{i}.mp3"
            words, mp3 = await synthesize(sc["voiceover"].strip(), voice, rate, mp3)
            d = probe_duration(mp3) + gap
            for k, (s, e, txt) in enumerate(words):
                timed.append((t0 + s, t0 + e, txt, k == len(words) - 1))
            vo_files.append(mp3); durs.append(d); t0 += d
            clip = wd / f"clip{i}.mp4"
            await asyncio.to_thread(download, sc["video_url"], clip)
            clip_files.append(clip)
        total = sum(durs)
        tail = 0.6
        total_out = total + tail
        durs[-1] += tail
        hook_end = min(durs[0], 4.0)
        ass = wd / "subs.ass"
        ass.write_text(build_ass(timed, job.get("hook_text", ""), hook_end, job.get("handle", ""), total_out, job.get("accent_color", "#B5452B"), job.get("highlight_color", "#FFD23F")), encoding="utf-8")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for c, d in zip(clip_files, durs):
            cmd += ["-stream_loop", "-1", "-t", f"{d:.3f}", "-i", str(c)]
        for v in vo_files:
            cmd += ["-i", str(v)]
        n = len(scenes)
        music = job.get("music_url")
        if music:
            mp = wd / "music.mp3"; await asyncio.to_thread(download, music, mp)
            cmd += ["-stream_loop", "-1", "-i", str(mp)]
        fl = []
        for i, d in enumerate(durs):
            zoom = f"scale={W}*1.06:{H}*1.06:force_original_aspect_ratio=increase,crop={W}*1.06:{H}*1.06"
            fl.append(f"[{i}:v]{zoom},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,trim=duration={d:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
        fl.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
        subs_path = str(ass).replace("\\", "/").replace(":", "\\:")
        fl.append(f"[vc]ass='{subs_path}',fade=t=in:st=0:d=0.25,fade=t=out:st={total_out-0.5:.3f}:d=0.5[vout]")
        for i, d in enumerate(durs):
            fl.append(f"[{n+i}:a]aresample=44100,aformat=channel_layouts=stereo,apad=whole_dur={d:.3f}[a{i}]")
        fl.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[voice]")
        if music:
            fl.append(f"[{2*n}:a]aresample=44100,aformat=channel_layouts=stereo,volume={float(job.get('music_volume', 0.12))}[mus]")
            fl.append("[voice][mus]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[amix]")
            alast = "amix"
        else:
            alast = "voice"
        fl.append(f"[{alast}]afade=t=out:st={total_out-0.6:.3f}:d=0.6,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[aout]")
        out = OUT / f"{rid}.mp4"
        cmd += ["-filter_complex", ";".join(fl), "-map", "[vout]", "-map", "[aout]", "-t", f"{total_out:.3f}",
                "-c:v", "libx264", "-preset", os.environ.get("X264_PRESET", "veryfast"), "-crf", "21", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-movflags", "+faststart", str(out)]
        await asyncio.to_thread(run, cmd)
        return {"id": rid, "file": out.name, "duration": round(total_out, 2), "words": len(timed)}
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def cleanup():
    cutoff = time.time() - KEEP_HOURS * 3600
    for f in OUT.glob("*.mp4"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def authed(req):
    return not KEY or req.headers.get("X-Render-Key") == KEY


async def h_render(req):
    if not authed(req): return web.json_response({"error": "unauthorized"}, status=401)
    try:
        job = await req.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    async with LOCK:  # one render at a time keeps small VPSes healthy
        cleanup()
        try:
            res = await render(job)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response(res)


async def h_file(req):
    name = req.match_info["name"]
    if not re.fullmatch(r"[\w.-]+\.mp4", name): raise web.HTTPNotFound()
    f = OUT / name
    if not f.exists(): raise web.HTTPNotFound()
    return web.FileResponse(f, headers={"Content-Type": "video/mp4"})


async def h_health(_):
    return web.json_response({"ok": True})


def make_app():
    app = web.Application(client_max_size=4 * 1024 * 1024)
    app.add_routes([web.post("/render", h_render), web.get("/files/{name}", h_file), web.get("/health", h_health)])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
