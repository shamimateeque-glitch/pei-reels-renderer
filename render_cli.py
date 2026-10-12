"""Run one render job from the JOB_JSON env var (used by GitHub Actions)."""
import asyncio, json, os, shutil, sys
os.environ.setdefault("OUT_DIR", "out")
os.environ.setdefault("WORK_DIR", "work")
import app

job = json.loads(os.environ["JOB_JSON"])
job_id = os.environ["JOB_ID"]
res = asyncio.run(app.render(job))
dest = f"{job_id}.mp4"
shutil.move(str(app.OUT / res["file"]), dest)
print(json.dumps({**res, "file": dest}))
