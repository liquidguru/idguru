#!/usr/bin/env python3
# =============================================================================
# idGuru — AI-Powered Underwater Species Identifier
# Copyright (c) 2025 Kaj Maney / liquidGuru (liquidguru.com)
#
# This software is licensed under the Creative Commons
# Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
#
# You are free to use and share this software for personal, non-commercial
# purposes, provided you give appropriate credit to the original author.
#
# Commercial use, redistribution as a paid product, or incorporation into
# a commercial service is prohibited without written permission from the author.
#
# Full licence: https://creativecommons.org/licenses/by-nc/4.0/
# Contact: kaj@liquidguru.com
# =============================================================================
import os, sys, json, sqlite3, base64, hashlib, subprocess, time, argparse, threading, queue, webbrowser
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

FRAME_INTERVAL  = 10
DEFAULT_WORKERS = 10
MODEL           = "claude-sonnet-4-6"
MODEL_BATCH     = "claude-sonnet-4-6"
VIDEO_EXTS      = {'.mp4','.mov','.avi','.mkv','.mts','.m2ts','.mpg','.mpeg','.mxf','.mod','.wmv','.flv'}
PHOTO_EXTS      = {'.jpg','.jpeg','.cr2','.nef','.arw','.dng','.orf','.raf','.rw2','.pef','.srw'}
DB_DIR          = Path.home() / ".underwater_index"
DB_PATH         = DB_DIR / "index.db"
FRAMES_DIR      = DB_DIR / "frames"

REGIONS = {
    "Indo-Pacific / Coral Triangle": "This footage is from the Indo-Pacific / Coral Triangle region, including locations such as Lembeh Strait, Ambon Bay, Raja Ampat, and similar Indonesian/Philippine waters. This is a world centre of marine biodiversity with exceptional muck diving and reef diving.",
    "Caribbean": "This footage is from the Caribbean region, including locations such as Bonaire, Cayman Islands, Roatan, Cozumel, and similar Caribbean waters. Common subjects include reef fish, cleaning stations, Caribbean reef sharks, spotted eagle rays, seahorses, frogfish, nudibranchs, and tropical invertebrates.",
    "Red Sea": "This footage is from the Red Sea, including locations such as Dahab, Sharm el-Sheikh, the Brothers Islands, and similar Red Sea dive sites. Common subjects include Red Sea clownfish, Napoleon wrasse, thresher sharks, hammerheads, lionfish, and endemic Red Sea species.",
    "UK / North Europe": "This footage is from UK or North European waters, including locations such as Scotland, Norway, the Farne Islands, or similar cold temperate Atlantic sites. Common subjects include grey seals, wolf fish, nudibranchs (Flabellina, Cuthona), cuttlefish, thornback ray, lumpsuckers, and colourful soft corals.",
    "West Coast USA": "This footage is from the West Coast USA, including locations such as California, Oregon, or Washington state kelp forest and rocky reef dive sites. Common subjects include giant sea bass, garibaldi, horn sharks, bat rays, leopard sharks, giant kelp, sea otters, and nudibranch species.",
    "East Coast USA": "This footage is from the East Coast USA or Atlantic waters, including locations such as North Carolina, Florida, or New England. Common subjects include sand tiger sharks, goliath grouper, loggerhead turtles, Atlantic spadefish, and temperate reef species.",
    "Australia / New Zealand": "This footage is from Australian or New Zealand waters, including locations such as the Great Barrier Reef, Coral Sea, Poor Knights Islands, or temperate southern waters. Common subjects include wobbegong sharks, weedy seadragons, leafy seadragons, blue-ringed octopus, nudibranchs, and endemic Antipodean species.",
    "Mediterranean": "This footage is from the Mediterranean Sea, including locations such as Malta, Croatia, Greece, or similar Mediterranean dive sites. Common subjects include Mediterranean barracuda, grouper, octopus, cuttlefish, Posidonia seagrass beds, red coral, seahorses, and endemic Mediterranean species.",
    "Japan": "This footage is from Japanese waters, including locations such as Izu Peninsula, Okinawa, or Hokkaido. Common subjects include Japanese spider crabs, frogfish, nudibranchs, mandarin fish, whale sharks (seasonal), hammerheads, and endemic Japanese marine species.",
}

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn, MofNCompleteColumn
    from rich.table import Table
    console = Console()
except ImportError:
    print("Run: pip install anthropic rich pillow flask"); sys.exit(1)


def init_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY, path TEXT UNIQUE, filename TEXT,
        duration REAL, filesize INTEGER, indexed_at TEXT, frame_count INTEGER,
        dive_site TEXT DEFAULT '', dive_date TEXT DEFAULT '',
        country TEXT DEFAULT '', region TEXT DEFAULT '', area TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS frames (
        id TEXT PRIMARY KEY, video_id TEXT, timestamp REAL,
        thumb_path TEXT, species TEXT, habitat TEXT,
        visibility TEXT, behaviours TEXT, notes TEXT,
        reviewed INTEGER DEFAULT 0, marked_delete INTEGER DEFAULT 0,
        FOREIGN KEY(video_id) REFERENCES videos(id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS photos (
        id TEXT PRIMARY KEY, path TEXT UNIQUE, filename TEXT,
        filesize INTEGER, indexed_at TEXT, thumb_path TEXT,
        species TEXT, habitat TEXT, visibility TEXT, behaviours TEXT, notes TEXT,
        reviewed INTEGER DEFAULT 0, marked_delete INTEGER DEFAULT 0,
        dive_site TEXT DEFAULT '', dive_date TEXT DEFAULT '',
        country TEXT DEFAULT '', region TEXT DEFAULT '', area TEXT DEFAULT '')""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fv ON frames(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fsp ON frames(species)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_psp ON photos(species)")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    for col in ["reviewed INTEGER DEFAULT 0", "marked_delete INTEGER DEFAULT 0"]:
        try: conn.execute(f"ALTER TABLE frames ADD COLUMN {col}")
        except: pass
    for col in ["dive_site TEXT DEFAULT ''", "dive_date TEXT DEFAULT ''",
                "country TEXT DEFAULT ''", "region TEXT DEFAULT ''", "area TEXT DEFAULT ''"]:
        try: conn.execute(f"ALTER TABLE videos ADD COLUMN {col}")
        except: pass
    try: conn.execute("ALTER TABLE frames ADD COLUMN id_confidence TEXT DEFAULT ''")
    except: pass
    try: conn.execute("ALTER TABLE photos ADD COLUMN id_confidence TEXT DEFAULT ''")
    except: pass
    conn.commit()
    return conn


def file_id(path):
    s = path.stat()
    return hashlib.md5(f"{path.name}{s.st_size}".encode()).hexdigest()


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json","-show_entries","format=duration",str(path)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        d = json.loads(r.stdout).get("format",{}).get("duration")
        return float(d) if d else None
    except: return None


def extract_frame(video_path, ts, out):
    for hwaccel in (["-hwaccel","cuda"], []):
        try:
            cmd = ["ffmpeg","-y"] + hwaccel + ["-ss",str(ts),"-i",str(video_path),"-vframes","1","-vf","scale=640:-2","-q:v","3",str(out)]
            subprocess.run(cmd, capture_output=True, timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return out.exists() and out.stat().st_size > 0
        except: continue
    return False


PROMPT = """You are an expert marine biologist and underwater naturalist specialising in Indonesian muck diving, with extensive field experience at Lembeh Strait (North Sulawesi) and Ambon Bay (Maluku). You are intimately familiar with the critters found at these sites and can identify them from partial views, unusual angles, and low visibility conditions. This footage was shot by an expert macro videographer with decades of experience at these sites. The footage may contain extremely rare species including Phylliroe bucephala, Histiophryne psychedelica, Kyonemichthys rumengani, planktonic octopus paralarvae, and other rarities. Look carefully before settling on an identification.

Analyse this underwater video frame carefully. Accuracy is more important than specificity.

Rules:
- Only identify to species level if genuinely confident from visible features
- Format species names as: "common name - Scientific name" e.g. "leaf scorpionfish - Taenianotus triacanthus"
- If no common name exists, use scientific name only e.g. "Tambja morosa"
- If unsure, use format: "leaf scorpionfish - possibly Taenianotus triacanthus" (plain hyphen, NOT em dash)
- Keep species names clean — NO parenthetical qualifiers like "(pale morph)" or "(two individuals)" in the name — put that detail in notes instead
- Never assign vertebrate ID to something that could be invertebrate
- Bornella sp: branched tree-like cerata, often near hydroids
- A wrong generic ID is worse than a correct vague one
- All footage is from Lembeh Strait and Ambon Bay muck diving
- IMPORTANT: Always use a plain hyphen (-) not em dash or long dash in species names

Common subjects (use "common name - Scientific name" format): Bornella sp., Chromodoris willani, Chromodoris lochi, Hypselodoris apolegma, Nembrotha kubaryana, Nembrotha cristata, Halgerda batangas, Miamira sinuata, Jorunna funebris, Phyllodesmium longicirrum, Tambja morosa, mimic octopus - Thaumoctopus mimicus, wonderpus - Wunderpus photogenicus, blue-ringed octopus - Hapalochlaena sp., flamboyant cuttlefish - Metasepia pfefferi, painted frogfish - Antennarius pictus, striated frogfish - Antennarius striatus, warty frogfish - Antennarius maculatus, giant frogfish - Antennarius commerson, psychedelic frogfish - Histiophryne psychedelica, ghost pipefish - Solenostomus paradoxus, robust ghost pipefish - Solenostomus cyanopterus, pygmy seahorse - Hippocampus bargibanti, Denise's pygmy seahorse - Hippocampus denise, pontoh's pygmy seahorse - Hippocampus pontohi, marble shrimp - Saron marmoratus, harlequin shrimp - Hymenocera picta, boxer crab - Lybia tessellata, mandarin fish - Synchiropus splendidus, leaf scorpionfish - Taenianotus triacanthus, weedy scorpionfish - Rhinopias frondosa, paddle-flap scorpionfish - Rhinopias eschmeyeri, sea moth - Eurypegasus draconis, pygmy pipefish - Kyonemichthys rumengani, coconut octopus - Amphioctopus marginatus, sea slug - Phylliroe bucephala, bobbit worm - Eunice aphroditois

Return ONLY valid JSON with no markdown:
{
  "isUnderwater": true or false,
  "species": ["common name - Scientific name for each animal, plain hyphen not em dash, NO parenthetical qualifiers"],
  "habitat": "concise description e.g. black sand muck with hydroid colonies",
  "visibility": "poor|fair|good|excellent",
  "behaviours": ["specific behaviours observed"],
  "notes": "confidence notes and anything else worth logging"
}"""


def analyze_frame(client, img_path, region=None):
    region_context = REGIONS.get(region, REGIONS["Indo-Pacific / Coral Triangle"]) if region else REGIONS["Indo-Pacific / Coral Triangle"]
    prompt_with_region = PROMPT.replace("All footage is from Lembeh Strait and Ambon Bay muck diving", region_context)
    with open(img_path,"rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    resp = client.messages.create(model=MODEL, max_tokens=600,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":b64}},
            {"type":"text","text":prompt_with_region}]}])
    txt = resp.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(txt)


def normalise_species(raw):
    seen, species = set(), []
    for s in raw:
        s = s.replace("\u2014","-").replace("\u2013","-").strip()
        key = s.lower()
        if key and key not in seen:
            seen.add(key); species.append(s)
    return species


def process_frame(args):
    client, video_path, vid_id, ts, frame_id, region = args
    out = FRAMES_DIR / f"{frame_id}.jpg"
    if not extract_frame(video_path, ts, out): return None
    try: result = analyze_frame(client, out, region)
    except Exception as e:
        console.print(f"[yellow]  API error @ {ts:.0f}s: {e}[/yellow]")
        return None
    if not result.get("isUnderwater", True):
        out.unlink(missing_ok=True); return None
    return {"id":frame_id,"video_id":vid_id,"timestamp":ts,"thumb_path":str(out),
            "species":json.dumps(normalise_species(result.get("species",[]))),
            "habitat":result.get("habitat",""),"visibility":result.get("visibility",""),
            "behaviours":json.dumps(result.get("behaviours",[])),"notes":result.get("notes","")}


def index_video(client, conn, path, workers, progress=None, task_id=None, region=None):
    vid_id = file_id(path)
    if conn.execute("SELECT 1 FROM videos WHERE id=?",(vid_id,)).fetchone(): return -1
    duration = probe_duration(path)
    if not duration:
        console.print(f"[yellow]  Skipping {path.name}[/yellow]"); return 0
    timestamps = [i*FRAME_INTERVAL+FRAME_INTERVAL/2 for i in range(int(duration/FRAME_INTERVAL))]
    args_list = [(client,path,vid_id,ts,f"{vid_id}_{i}",region) for i,ts in enumerate(timestamps)]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_frame,a):a for a in args_list}
        done = 0
        for fut in as_completed(futures):
            done += 1
            r = fut.result()
            if r: results.append(r)
            if progress and task_id is not None:
                progress.update(task_id,completed=done,description=f"[cyan]{path.name[:50]}[/cyan]  {done}/{len(timestamps)} frames")
    conn.execute("INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (vid_id,str(path),path.name,duration,path.stat().st_size,datetime.now().isoformat(),len(results),'','','','',''))
    for r in results:
        conn.execute("INSERT OR REPLACE INTO frames VALUES (?,?,?,?,?,?,?,?,?,0,0)",
            (r["id"],r["video_id"],r["timestamp"],r["thumb_path"],r["species"],r["habitat"],r["visibility"],r["behaviours"],r["notes"]))
    conn.commit()
    return len(results)


def run_batch(client, conn, videos, workers=8, progress_cb=None, region=None):
    all_items, vid_meta = [], {}
    region_context = REGIONS.get(region, REGIONS["Indo-Pacific / Coral Triangle"]) if region else REGIONS["Indo-Pacific / Coral Triangle"]
    prompt_with_region = PROMPT.replace("All footage is from Lembeh Strait and Ambon Bay muck diving", region_context)
    console.print(f"[cyan]Extracting frames from {len(videos)} video(s)...[/cyan]")
    for vp in videos:
        vid_id = file_id(vp)
        if conn.execute("SELECT 1 FROM videos WHERE id=?",(vid_id,)).fetchone(): continue
        dur = probe_duration(vp)
        if not dur: console.print(f"[yellow]  Skipping {vp.name}[/yellow]"); continue
        vid_meta[vid_id] = {"path":str(vp),"filename":vp.name,"duration":dur,"filesize":vp.stat().st_size}
        timestamps = [i*FRAME_INTERVAL+FRAME_INTERVAL/2 for i in range(int(dur/FRAME_INTERVAL))]
        for i,ts in enumerate(timestamps):
            frame_id = f"{vid_id}_{i}"
            out = FRAMES_DIR / f"{frame_id}.jpg"
            if extract_frame(vp, ts, out):
                with open(out,"rb") as f: b64 = base64.standard_b64encode(f.read()).decode()
                all_items.append({"custom_id":frame_id,"ts":ts,"b64":b64,"video_id":vid_id})
        console.print(f"  [dim]{vp.name}[/dim]")
    if not all_items: console.print("[green]Nothing new to batch.[/green]"); return 0
    console.print(f"\n[cyan]Submitting {len(all_items)} frames to Batch API...[/cyan]")
    requests = [{"custom_id":item["custom_id"],"params":{"model":MODEL_BATCH,"max_tokens":600,
        "messages":[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":item["b64"]}},
            {"type":"text","text":prompt_with_region}]}]}} for item in all_items]
    batch = client.beta.messages.batches.create(requests=requests)
    console.print(f"[green]Batch submitted: {batch.id}[/green]")
    while True:
        batch = client.beta.messages.batches.retrieve(batch.id)
        counts = batch.request_counts
        total = counts.processing+counts.succeeded+counts.errored+counts.canceled+counts.expired
        done = counts.succeeded+counts.errored+counts.canceled+counts.expired
        pct = int(done/total*100) if total else 0
        console.print(f"  [{pct}%] {done}/{total}", end="\r")
        if progress_cb: progress_cb(pct, done, total)
        if batch.processing_status == "ended": break
        time.sleep(15)
    console.print(f"\n[green]Batch complete![/green]")
    item_map = {i["custom_id"]:i for i in all_items}
    saved, vid_frames = 0, {}
    for result in client.beta.messages.batches.results(batch.id):
        if result.result.type != "succeeded": continue
        item = item_map.get(result.custom_id)
        if not item: continue
        try:
            txt = result.result.message.content[0].text.strip().lstrip("```json").rstrip("```").strip()
            analysis = json.loads(txt)
        except: continue
        if not analysis.get("isUnderwater",True):
            (FRAMES_DIR/f"{result.custom_id}.jpg").unlink(missing_ok=True); continue
        vid_id = item["video_id"]
        vid_frames[vid_id] = vid_frames.get(vid_id,0)+1
        conn.execute("INSERT OR REPLACE INTO frames VALUES (?,?,?,?,?,?,?,?,?,0,0)",
            (result.custom_id,vid_id,item["ts"],str(FRAMES_DIR/f"{result.custom_id}.jpg"),
             json.dumps(normalise_species(analysis.get("species",[]))),
             analysis.get("habitat",""),analysis.get("visibility",""),
             json.dumps(analysis.get("behaviours",[])),analysis.get("notes","")))
        saved += 1
    for vid_id,meta in vid_meta.items():
        conn.execute("INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid_id,meta["path"],meta["filename"],meta["duration"],meta["filesize"],
             datetime.now().isoformat(),vid_frames.get(vid_id,0),'','','','',''))
    conn.commit()
    console.print(f"[green bold]Saved {saved} frames.[/green bold]")
    return saved


def cmd_index(args, conn):
    import anthropic as ant
    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key: console.print("[red]Set ANTHROPIC_API_KEY[/red]"); sys.exit(1)
    client = ant.Anthropic(api_key=api_key)
    root = Path(args.path)
    if not root.exists(): console.print(f"[red]Path not found: {root}[/red]"); sys.exit(1)
    indexed = {r[0] for r in conn.execute("SELECT path FROM videos")}
    videos = sorted({p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS and str(p) not in indexed})
    if not videos: console.print("[green]No new videos.[/green]"); return
    console.print(f"\n[cyan]Found {len(videos)} unindexed video(s)[/cyan]\n")
    total = 0
    with Progress(SpinnerColumn(),TextColumn("[progress.description]{task.description}"),BarColumn(),MofNCompleteColumn(),TimeRemainingColumn()) as progress:
        vtask = progress.add_task("[white]Overall",total=len(videos))
        for vp in videos:
            dur = probe_duration(vp) or 0
            ft = progress.add_task(f"[cyan]{vp.name[:50]}[/cyan]",total=max(int(dur/FRAME_INTERVAL),1))
            count = index_video(client,conn,vp,args.workers,progress,ft)
            progress.remove_task(ft); progress.update(vtask,advance=1)
            if count==-1: console.print(f"  [dim]skipped[/dim]  {vp.name}")
            else: total+=count; console.print(f"  [green]ok[/green] {vp.name}  [dim]{count} frames[/dim]")
    console.print(f"\n[green bold]Done! {total} frames added.[/green bold]\n")


def cmd_batch(args, conn):
    import anthropic as ant
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key: console.print("[red]Set ANTHROPIC_API_KEY[/red]"); sys.exit(1)
    client = ant.Anthropic(api_key=api_key)
    root = Path(args.path)
    if not root.exists(): console.print(f"[red]Path not found: {root}[/red]"); sys.exit(1)
    indexed = {r[0] for r in conn.execute("SELECT path FROM videos")}
    videos = sorted({p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS and str(p) not in indexed})
    if not videos: console.print("[green]No new videos.[/green]"); return
    run_batch(client,conn,videos,args.workers)


def cmd_search(args, conn):
    sql = "SELECT v.filename, f.timestamp, f.species, f.habitat, f.visibility FROM frames f JOIN videos v ON f.video_id=v.id WHERE 1=1"
    params = []
    if args.query:
        sql += " AND (f.species LIKE ? OR f.habitat LIKE ? OR f.notes LIKE ? OR v.filename LIKE ?)"; q=f"%{args.query}%"; params+=[q,q,q,q]
    if args.species:
        sql += " AND f.species LIKE ?"; params.append(f"%{args.species}%")
    sql += " ORDER BY v.filename, f.timestamp LIMIT 200"
    rows = conn.execute(sql,params).fetchall()
    t = Table(title=f"Results ({len(rows)})",style="cyan")
    for col in ["File","Time","Species","Habitat","Visibility"]: t.add_column(col,no_wrap=col in ("Time","Visibility"))
    for r in rows:
        m,s=int(r[1]//60),int(r[1]%60)
        t.add_row(r[0][:40],f"{m}:{s:02d}",", ".join(json.loads(r[2]) if r[2] else [])[:60],(r[3] or "")[:40],r[4] or "")
    console.print(t)


def cmd_stats(conn):
    vc = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    fc = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    hrs = conn.execute("SELECT COALESCE(SUM(duration),0) FROM videos").fetchone()[0]/3600
    t = Table(title="Index Stats",style="cyan")
    t.add_column("Videos"); t.add_column("Frames"); t.add_column("Total footage")
    t.add_row(str(vc),str(fc),f"{hrs:.1f} hrs")
    console.print(t)
    top = conn.execute("SELECT value, COUNT(*) c FROM frames, json_each(frames.species) GROUP BY value ORDER BY c DESC LIMIT 15").fetchall()
    if top:
        t2 = Table(title="Top Species",style="blue")
        t2.add_column("Species"); t2.add_column("Frames",justify="right")
        for sp,c in top: t2.add_row(sp,str(c))
        console.print(t2)


def cmd_export(args, conn):
    import csv
    out = Path(args.output) if args.output else Path.cwd()/f"underwater_index_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    rows = conn.execute("""SELECT v.filename, v.path, f.timestamp, f.species, f.habitat, f.visibility,
        f.behaviours, f.notes, v.indexed_at, COALESCE(v.country,''), COALESCE(v.region,''),
        COALESCE(v.area,''), COALESCE(v.dive_site,''), COALESCE(v.dive_date,'')
        FROM frames f JOIN videos v ON f.video_id=v.id ORDER BY v.filename, f.timestamp""").fetchall()
    with open(out,'w',newline='',encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Filename','Full Path','Timestamp (s)','Timestamp','Species','Habitat','Visibility',
                    'Behaviours','Notes','Indexed At','Country','Region','Area','Dive Site','Dive Date'])
        for r in rows:
            m,s=int(r[2]//60),int(r[2]%60)
            w.writerow([r[0],r[1],round(r[2],1),f"{m}:{s:02d}",
                        ', '.join(json.loads(r[3] or '[]')),r[4],r[5],
                        ', '.join(json.loads(r[6] or '[]')),r[7],r[8],r[9],r[10],r[11],r[12],r[13]])
    console.print(f"[green]Exported {len(rows)} frames to {out}[/green]")
    os.startfile(out)


def resize_photo_for_thumb(src_path, out_path):
    """Resize a photo to max 1280px wide JPEG for thumbnail and Claude analysis."""
    try:
        from PIL import Image
    except ImportError:
        console.print("[red]pip install pillow[/red]"); return False
    ext = src_path.suffix.lower()
    try:
        if ext in {'.cr2','.nef','.arw','.dng','.orf','.raf','.rw2','.pef','.srw'}:
            try:
                import rawpy
                with rawpy.imread(str(src_path)) as raw:
                    rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
                img = Image.fromarray(rgb)
            except ImportError:
                console.print(f"[yellow]  rawpy not installed — skipping RAW {src_path.name}. Run: pip install rawpy[/yellow]")
                return False
        else:
            img = Image.open(src_path).convert('RGB')
        w, h = img.size
        if w > 1280:
            img = img.resize((1280, int(h * 1280 / w)), Image.LANCZOS)
        img.save(str(out_path), 'JPEG', quality=85)
        return out_path.exists() and out_path.stat().st_size > 0
    except Exception as e:
        console.print(f"[yellow]  Could not process {src_path.name}: {e}[/yellow]")
        return False


def index_photo(client, conn, path, region=None):
    """Index a single still photo. Returns 1=indexed, 0=failed, -1=already done."""
    photo_id = file_id(path)
    if conn.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone():
        return -1
    out = FRAMES_DIR / f"{photo_id}.jpg"
    if not resize_photo_for_thumb(path, out):
        return 0
    try:
        result = analyze_frame(client, out, region)
    except Exception as e:
        console.print(f"[yellow]  API error {path.name}: {e}[/yellow]")
        out.unlink(missing_ok=True)
        return 0
    conn.execute(
        "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?)",
        (photo_id, str(path), path.name, path.stat().st_size,
         datetime.now().isoformat(), str(out),
         json.dumps(normalise_species(result.get("species", []))),
         result.get("habitat", ""), result.get("visibility", ""),
         json.dumps(result.get("behaviours", [])), result.get("notes", ""),
         '', '', '', '', ''))
    conn.commit()
    return 1


def run_photo_batch(client, conn, photos, progress_cb=None, region=None):
    """Submit photos to Anthropic Batch API (50% cheaper, async)."""
    all_items = []
    region_context = REGIONS.get(region, REGIONS["Indo-Pacific / Coral Triangle"]) if region else REGIONS["Indo-Pacific / Coral Triangle"]
    prompt_with_region = PROMPT.replace("All footage is from Lembeh Strait and Ambon Bay muck diving", region_context)
    console.print(f"[cyan]Preparing {len(photos)} photo(s) for batch...[/cyan]")
    for pp in photos:
        photo_id = file_id(pp)
        if conn.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone():
            continue
        out = FRAMES_DIR / f"{photo_id}.jpg"
        if not resize_photo_for_thumb(pp, out):
            continue
        with open(out, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        all_items.append({"custom_id": photo_id, "path": str(pp), "filename": pp.name,
                          "filesize": pp.stat().st_size, "thumb": str(out), "b64": b64})
        console.print(f"  [dim]{pp.name}[/dim]")
    if not all_items:
        console.print("[green]Nothing new to batch.[/green]"); return 0
    console.print(f"\n[cyan]Submitting {len(all_items)} photos to Batch API...[/cyan]")
    requests = [{"custom_id": item["custom_id"], "params": {"model": MODEL_BATCH, "max_tokens": 600,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": item["b64"]}},
            {"type": "text", "text": prompt_with_region}]}]}} for item in all_items]
    batch = client.beta.messages.batches.create(requests=requests)
    console.print(f"[green]Batch submitted: {batch.id}[/green]")
    while True:
        batch = client.beta.messages.batches.retrieve(batch.id)
        counts = batch.request_counts
        total = counts.processing + counts.succeeded + counts.errored + counts.canceled + counts.expired
        done = counts.succeeded + counts.errored + counts.canceled + counts.expired
        pct = int(done / total * 100) if total else 0
        console.print(f"  [{pct}%] {done}/{total}", end="\r")
        if progress_cb: progress_cb(pct, done, total)
        if batch.processing_status == "ended": break
        time.sleep(15)
    console.print(f"\n[green]Batch complete![/green]")
    item_map = {i["custom_id"]: i for i in all_items}
    saved = 0
    for result in client.beta.messages.batches.results(batch.id):
        if result.result.type != "succeeded": continue
        item = item_map.get(result.custom_id)
        if not item: continue
        try:
            txt = result.result.message.content[0].text.strip().lstrip("```json").rstrip("```").strip()
            analysis = json.loads(txt)
        except: continue
        conn.execute(
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?)",
            (result.custom_id, item["path"], item["filename"], item["filesize"],
             datetime.now().isoformat(), item["thumb"],
             json.dumps(normalise_species(analysis.get("species", []))),
             analysis.get("habitat", ""), analysis.get("visibility", ""),
             json.dumps(analysis.get("behaviours", [])), analysis.get("notes", ""),
             '', '', '', '', ''))
        saved += 1
    conn.commit()
    console.print(f"[green bold]Saved {saved} photos.[/green bold]")
    return saved


def cmd_viewer(conn, headless=False):
    try:
        from flask import Flask, request, jsonify, send_file, render_template_string, Response, g
    except ImportError:
        console.print("[red]pip install flask[/red]"); sys.exit(1)

    import anthropic as ant
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    # Check settings DB for API key (takes priority over env var)
    try:
        _sconn = sqlite3.connect(DB_PATH)
        _row = _sconn.execute("SELECT value FROM settings WHERE key='api_key'").fetchone()
        if _row and _row[0]: api_key = _row[0]
        _sconn.close()
    except: pass
    # Use a mutable container so nested closures always see the latest client
    _client = {"obj": ant.Anthropic(api_key=api_key) if api_key else None}

    def get_ai_client():
        return _client["obj"]
    stop_flag = threading.Event()
    scan_queue = queue.Queue()
    photo_stop_flag = threading.Event()
    photo_queue = queue.Queue()
    app = Flask(__name__)

    def get_db():
        if 'db' not in g:
            g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
            g.db.execute("PRAGMA journal_mode=WAL")
        return g.db

    @app.teardown_appcontext
    def close_db(e=None):
        db = g.pop('db', None)
        if db: db.close()

    HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>idGuru - liquidGuru's Critter Finder</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#050f1f;color:#dbeafe;font-family:system-ui,sans-serif}
.tabs{display:flex;border-bottom:1px solid #1e3a5f;padding:0 20px;background:#07172b;position:sticky;top:0;z-index:40;align-items:center}
.tab{padding:14px 22px;cursor:pointer;color:#475569;font-size:14px;border-bottom:2px solid transparent;transition:all .15s}.tab.active{color:#38bdf8;border-bottom-color:#38bdf8}
.tab:hover:not(.active){color:#94a3b8}
.page{display:none;padding:20px}.page.active{display:block}
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}
.hdr h2{color:#38bdf8;font-size:18px}
.stat{color:#475569;font-size:12px;margin-top:2px}
input,select{background:#0c1e35;border:1px solid #1e3a5f;color:#dbeafe;padding:8px 12px;border-radius:8px;font-size:13px;max-width:100%}
select{text-overflow:ellipsis;overflow:hidden}
input{flex:1;min-width:200px}
button{border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-size:13px;transition:opacity .15s}
button:hover{opacity:.85}
.btn-blue{background:#0ea5e9;color:#fff}
.btn-green{background:#16a34a;color:#fff}
.btn-purple{background:#7c3aed;color:#fff}
.btn-amber{background:#b45309;color:#fff}
.btn-slate{background:#1e3a5f;color:#7dd4fc}
.btn-red{background:#7f1d1d;color:#f87171}
.btn-sm{padding:5px 10px;font-size:12px}
.sbar{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.card{background:#0c1e35;border-radius:10px;overflow:hidden;cursor:pointer;border:2px solid #1e3a5f;transition:border-color .15s;user-select:none}
.card:hover{border-color:#38bdf8}
.card.selected{border-color:#f59e0b;background:#1a1500}
.card.marked-delete{border-color:#7f1d1d;background:#1a0a0a}
.card img{width:100%;height:140px;object-fit:cover;display:block;pointer-events:none}
.ci{padding:8px 10px}
.fname{font-size:11px;color:#475569;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tags{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:4px}
.tag{background:#0c3a5e;color:#7dd4fc;font-size:10px;padding:2px 6px;border-radius:999px}
.bot{display:flex;justify-content:space-between;font-size:11px}
.hab{color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:75%}
.vis-poor{color:#ef4444}.vis-fair{color:#f59e0b}.vis-good{color:#22c55e}.vis-excellent{color:#06b6d4}
.sel-bar{position:fixed;bottom:0;left:0;right:0;background:#0c1e35;border-top:1px solid #1e3a5f;padding:12px 20px;display:flex;gap:10px;align-items:center;z-index:50}
.sel-bar span{color:#7dd4fc;font-size:13px;margin-right:8px}
.scan-box{background:#0c1e35;border:1px solid #1e3a5f;border-radius:12px;padding:20px;margin-bottom:20px}
.scan-box h3{color:#7dd4fc;font-size:14px;margin-bottom:14px}
.folder-row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.prog-wrap{margin-top:14px;display:none}
.prog-label{font-size:12px;color:#94a3b8;margin-bottom:6px}
.prog-bar-bg{background:#0f2540;border-radius:4px;height:6px;overflow:hidden}
.prog-bar{background:#0ea5e9;height:100%;width:0%;transition:width .3s}
.prog-log{margin-top:10px;font-size:11px;color:#475569;max-height:120px;overflow-y:auto;line-height:1.8}
.fl-item{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:#071828;border-radius:8px;margin-bottom:6px}
.fl-path{color:#7dd4fc;font-size:13px;word-break:break-all}
.fl-meta{color:#475569;font-size:11px;margin-top:2px}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:100;align-items:center;justify-content:center;padding:20px}
.modal.show{display:flex}
.mbox{background:#0c1e35;border:1px solid #1e3a5f;border-radius:14px;max-width:560px;width:100%;max-height:90vh;overflow-y:auto}
.mimg{width:100%;max-height:280px;object-fit:cover;border-radius:14px 14px 0 0;display:block}
.mbody{padding:20px}
.mtitle{color:#38bdf8;font-weight:600;font-size:15px;margin-bottom:4px}
.msub{color:#475569;font-size:12px;margin-bottom:10px}
.lbl{font-size:11px;color:#334155;letter-spacing:.06em;text-transform:uppercase;margin-bottom:5px;margin-top:10px}
.mactions{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
.mnote{font-size:11px;color:#475569;margin-top:8px}
.ac-wrap{position:relative;flex:1;min-width:140px}
.ac-list{position:absolute;top:100%;left:0;right:0;background:#0c2340;border:1px solid #1e3a5f;border-radius:0 0 8px 8px;max-height:160px;overflow-y:auto;z-index:200;display:none}
.ac-item{padding:6px 10px;font-size:12px;cursor:pointer;color:#dbeafe}
.ac-item:hover{background:#1e3a5f}
.tools-row{display:flex;gap:10px;margin-bottom:16px;align-items:flex-end;flex-wrap:wrap}
.preview-box{background:#071828;border-radius:8px;padding:12px;margin-top:12px;max-height:200px;overflow-y:auto;font-size:12px;color:#64748b}
.preview-item{padding:3px 0;border-bottom:1px solid #0c1e35;color:#94a3b8}
.meta-row{display:flex;gap:10px;margin-bottom:8px;flex-wrap:wrap;align-items:flex-end}
.meta-field{flex:1;min-width:120px;position:relative}
.meta-field .lbl{margin-top:0}
.meta-field input{width:100%;font-size:12px;padding:5px 9px;margin-top:4px}
</style>
</head>
<body>

<div class="tabs">
  <div style="display:flex;align-items:center;padding-right:20px;border-right:1px solid #1e3a5f;margin-right:8px">
    <span style="font-size:16px;margin-right:6px">🤿</span>
    <span style="color:#38bdf8;font-weight:700;font-size:15px;letter-spacing:.02em">idGuru</span>
  </div>
  <div class="tab active" id="tab-btn-home" onclick="switchTab('home')">Home</div>
  <div class="tab" id="tab-btn-browse" onclick="switchTab('browse')">Videos</div>
  <div class="tab" id="tab-btn-photos" onclick="switchTab('photos')">Photos</div>
  <div class="tab" id="tab-btn-scan" onclick="switchTab('scan')">Scan Videos/Photos</div>
  <div class="tab" id="tab-btn-tools" onclick="switchTab('tools')">Tools</div>
  <div style="margin-left:auto;display:flex;align-items:center;padding-right:4px">
    <button onclick="openSettings()" style="background:none;border:none;color:#475569;font-size:18px;cursor:pointer;padding:8px;border-radius:6px;transition:color .15s" title="Settings" onmouseover="this.style.color='#38bdf8'" onmouseout="this.style.color='#475569'">&#9881;</button>
  </div>
</div>

<!-- HOME -->
<div class="page active" id="tab-home">
  <div style="max-width:720px;margin:40px auto;padding:0 20px">
    <div style="text-align:center;margin-bottom:40px">
      <div style="font-size:48px;margin-bottom:12px">🤿</div>
      <h1 style="color:#38bdf8;font-size:28px;font-weight:700;margin-bottom:6px">idGuru</h1>
      <div style="color:#475569;font-size:14px">liquidGuru's AI-powered critter finder</div>
    </div>

    <div class="scan-box" style="margin-bottom:16px">
      <h3 style="color:#38bdf8;margin-bottom:12px">What is idGuru?</h3>
      <p style="color:#94a3b8;font-size:14px;line-height:1.7">
        idGuru is a personal archive and identification tool for underwater footage and photos.
        It uses Claude AI to automatically identify marine species in your video clips and still images,
        tagging each frame with species names, habitat, visibility, and behaviour notes.
        Your entire archive becomes searchable in seconds.
      </p>
    </div>

    <div class="scan-box" style="margin-bottom:16px">
      <h3 style="color:#38bdf8;margin-bottom:14px">How it works</h3>
      <div style="display:flex;flex-direction:column;gap:14px">
        <div style="display:flex;gap:14px;align-items:flex-start">
          <div style="background:#7c3aed;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0">0</div>
          <div>
            <div style="color:#7dd4fc;font-size:13px;font-weight:600;margin-bottom:3px">First time setup</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6">Click the <strong style="color:#94a3b8">⚙️ gear icon</strong> in the top right to open Settings. Enter your <strong style="color:#94a3b8">Anthropic API key</strong> (get one free at <a href="https://console.anthropic.com" target="_blank" style="color:#38bdf8;text-decoration:none">console.anthropic.com</a>) and set your <strong style="color:#94a3b8">default region</strong> — this tells Claude which part of the world your footage is from so it can make better identifications. You can override the region for each individual scan.</div>
          </div>
        </div>
        <div style="display:flex;gap:14px;align-items:flex-start">
          <div style="background:#0ea5e9;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0">1</div>
          <div>
            <div style="color:#7dd4fc;font-size:13px;font-weight:600;margin-bottom:3px">Scan your footage</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6">Point idGuru at a folder of video files or photos. For videos, a frame is extracted every 10 seconds. Photos are resized for analysis. RAW files (CR2, NEF, ARW etc) are supported.</div>
          </div>
        </div>
        <div style="display:flex;gap:14px;align-items:flex-start">
          <div style="background:#0ea5e9;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0">2</div>
          <div>
            <div style="color:#7dd4fc;font-size:13px;font-weight:600;margin-bottom:3px">AI identification</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6">Each frame or photo is sent to Claude AI — an expert in marine species from your chosen region. It identifies every animal visible, notes the habitat, visibility, and any interesting behaviours. Use <strong style="color:#94a3b8">Batch mode</strong> for overnight scanning at 50% lower cost.</div>
          </div>
        </div>
        <div style="display:flex;gap:14px;align-items:flex-start">
          <div style="background:#0ea5e9;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0">3</div>
          <div>
            <div style="color:#7dd4fc;font-size:13px;font-weight:600;margin-bottom:3px">Browse and search</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6">Search across your entire archive by species, habitat, dive site, date, or any keyword. Browse videos frame by frame or group photos by species. Click any result to open the file at the exact timestamp in VLC.</div>
          </div>
        </div>
        <div style="display:flex;gap:14px;align-items:flex-start">
          <div style="background:#0ea5e9;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0">4</div>
          <div>
            <div style="color:#7dd4fc;font-size:13px;font-weight:600;margin-bottom:3px">Edit and organise</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6">Correct any misidentifications, add species, tag dive sites and dates, rename files using species names, and mark footage for deletion. Multi-select with Ctrl+click or Shift+click to edit in bulk.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="scan-box" style="margin-bottom:16px">
      <h3 style="color:#38bdf8;margin-bottom:12px">Quick start</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn-blue" onclick="openSettings()">⚙️ Settings</button>
        <button class="btn-green" onclick="switchTab('scan')">📁 Scan Videos/Photos</button>
        <button class="btn-slate" onclick="switchTab('browse')">🎬 Browse Videos</button>
        <button class="btn-slate" onclick="switchTab('photos')">📷 Browse Photos</button>
      </div>
    </div>

    <div class="scan-box" style="margin-bottom:16px">
      <h3 style="color:#38bdf8;margin-bottom:14px">About liquidGuru</h3>
      <div style="display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap">
        <div style="flex:1;min-width:200px">
          <p style="color:#94a3b8;font-size:13px;line-height:1.8;margin-bottom:10px">
            idGuru was created by <strong style="color:#7dd4fc">Kaj Maney</strong>, a PADI instructor, underwater videographer, and the man behind <a href="https://www.liquidguru.com" target="_blank" style="color:#38bdf8;text-decoration:none">liquidguru.com</a>.
          </p>
          <p style="color:#94a3b8;font-size:13px;line-height:1.8;margin-bottom:10px">
            Kaj has spent decades diving the world's best macro sites — 8.5 years in the Caribbean (Roatan, Dominica, Belize), time in Tioman and Fiji, years as Dive Operations Manager at Kungkungan Bay Resort in Lembeh Strait, and started and was co-owner of <a href="https://www.diveintoambon.com" target="_blank" style="color:#38bdf8;text-decoration:none">Dive Into Ambon</a> with partner Barb Makohin.
          </p>
          <p style="color:#94a3b8;font-size:13px;line-height:1.8;margin-bottom:10px">
            Since catching the video bug in 2002, Kaj has dedicated himself to filming the bizarre, rare, and barely-seen creatures of the Indo-Pacific muck diving world — from psychedelic frogfish and mimic octopus to transparent swimming nudibranchs and crab skin-shedding ceremonies. His footage has introduced thousands of people to a completely unknown underwater world.
          </p>
          <p style="color:#94a3b8;font-size:13px;line-height:1.8">
            idGuru was born out of a very real problem: thousands of hours of footage sitting on hard drives, mostly unwatched. Now the AI watches it for you.
          </p>
        </div>
      </div>
      <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
        <a href="https://www.liquidguru.com" target="_blank" style="background:#0c3a5e;color:#7dd4fc;font-size:12px;padding:5px 12px;border-radius:8px;text-decoration:none">🌊 liquidguru.com</a>
        <a href="https://www.diveintoambon.com" target="_blank" style="background:#0c3a5e;color:#7dd4fc;font-size:12px;padding:5px 12px;border-radius:8px;text-decoration:none">🤿 Dive Into Ambon</a>
      </div>
    </div>

    <div style="text-align:center;margin-top:30px;padding-bottom:40px">
      <div id="home-stat" style="color:#334155;font-size:13px">Loading...</div>
    </div>
  </div>
</div>

<!-- BROWSE -->
<div class="page" id="tab-browse">
  <div class="hdr">
    <div>
      <h2>Videos</h2>
      <div class="stat" id="stat"></div>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn-amber" onclick="doExport()">Export CSV</button>
      <button id="btn-purge" class="btn-red" onclick="purgeMarked()" style="display:none">Delete Marked (<span id="marked-count">0</span>)</button>
    </div>
  </div>
  <div class="sbar">
    <input id="q" placeholder="Search species, habitat, site, notes, filename..." oninput="currentPage=1;load()">
    <div style="display:flex;gap:6px;background:#0c1e35;border:1px solid #1e3a5f;border-radius:8px;padding:3px">
      <button id="view-frames" class="btn-blue btn-sm" onclick="setView('frames')">Frames</button>
      <button id="view-clips" class="btn-sm" style="background:transparent;color:#475569" onclick="setView('clips')">Clips</button>
    </div>
    <button id="clips-select-mode-btn" style="display:none;background:#0c3a5e;color:#7dd4fc;border:1px solid #1e3a5f;padding:5px 12px;border-radius:8px;font-size:12px;cursor:pointer" onclick="toggleClipSelectMode()">Select Mode</button>
  </div>
  <div class="sbar" style="margin-top:-8px">
    <select id="sp" onchange="currentPage=1;load()" style="flex:1;min-width:0;max-width:200px;overflow:hidden;text-overflow:ellipsis"><option>All species</option></select>
    <select id="country-filter" onchange="currentPage=1;load()" style="flex:1;min-width:0"><option>All countries</option></select>
    <select id="region-filter" onchange="currentPage=1;load()" style="flex:1;min-width:0"><option>All regions</option></select>
    <select id="area-filter" onchange="currentPage=1;load()" style="flex:1;min-width:0"><option>All areas</option></select>
    <select id="site-filter" onchange="currentPage=1;load()" style="flex:1;min-width:0"><option>All sites</option></select>
    <select id="sort-filter" onchange="currentPage=1;load()" style="min-width:0;font-size:12px">
      <option value="">Sort: Default</option>
      <option value="confidence_asc">ID: Uncertain first</option>
      <option value="confidence_desc">ID: Confirmed first</option>
    </select>
    <span style="color:#475569;font-size:12px;white-space:nowrap" id="rcount"></span>
  </div>
  <div class="grid" id="grid"></div>
  <div id="pagination" style="display:none;justify-content:center;align-items:center;gap:12px;padding:20px 0 80px;flex-wrap:wrap">
    <button class="btn-slate" id="pg-prev" onclick="changePage(-1)">Previous</button>
    <span id="pg-info" style="color:#475569;font-size:13px"></span>
    <div style="display:flex;align-items:center;gap:6px">
      <span style="color:#475569;font-size:12px">Go to page:</span>
      <input id="pg-jump" type="number" min="1" style="width:60px;padding:5px 8px;font-size:12px" onkeydown="if(event.key==='Enter')jumpToPage()">
      <button class="btn-slate btn-sm" onclick="jumpToPage()">Go</button>
    </div>
    <button class="btn-slate" id="pg-next" onclick="changePage(1)">Next</button>
  </div>
  <div style="height:20px"></div>
</div>

<!-- PHOTOS -->
<div class="page" id="tab-photos">
  <div class="hdr">
    <div>
      <h2>Photo Archive</h2>
      <div class="stat" id="photo-stat">Loading...</div>
    </div>
    <div style="display:flex;gap:8px">
      <button id="btn-purge-photos" class="btn-red" onclick="purgeMarkedPhotos()" style="display:none">Delete Marked (<span id="photo-marked-count">0</span>)</button>
    </div>
  </div>

  <!-- Photo sub-tabs -->
  <div style="display:flex;gap:4px;margin-bottom:14px;background:#07172b;border:1px solid #1e3a5f;border-radius:8px;padding:3px;width:fit-content">
    <button id="photo-view-all" class="btn-blue btn-sm" onclick="setPhotoView('all')">All Photos</button>
    <button id="photo-view-species" class="btn-sm" style="background:transparent;color:#475569" onclick="setPhotoView('species')">By Species</button>
  </div>

  <!-- All Photos sub-view -->
  <div id="photo-view-all-panel">
    <div class="sbar">
      <input id="photo-q" placeholder="Search species, habitat, site, notes, filename..." oninput="photoPage=1;loadPhotos()">
    </div>
    <div class="sbar" style="margin-top:-8px">
      <select id="photo-sp" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0;max-width:200px;overflow:hidden;text-overflow:ellipsis"><option>All species</option></select>
      <select id="photo-country-filter" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0"><option>All countries</option></select>
      <select id="photo-region-filter" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0"><option>All regions</option></select>
      <select id="photo-area-filter" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0"><option>All areas</option></select>
      <select id="photo-site-filter" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0"><option>All sites</option></select>
      <select id="photo-sort-filter" onchange="photoPage=1;loadPhotos()" style="min-width:0;font-size:12px">
        <option value="">Sort: Default</option>
        <option value="confidence_asc">ID: Uncertain first</option>
        <option value="confidence_desc">ID: Confirmed first</option>
      </select>
      <span style="color:#475569;font-size:12px;white-space:nowrap" id="photo-rcount"></span>
    </div>
    <div class="grid" id="photo-grid"></div>
    <div id="photo-pagination" style="display:none;justify-content:center;align-items:center;gap:12px;padding:20px 0 80px;flex-wrap:wrap">
      <button class="btn-slate" id="photo-pg-prev" onclick="changePhotoPage(-1)">Previous</button>
      <span id="photo-pg-info" style="color:#475569;font-size:13px"></span>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="color:#475569;font-size:12px">Go to page:</span>
        <input id="photo-pg-jump" type="number" min="1" style="width:60px;padding:5px 8px;font-size:12px" onkeydown="if(event.key==='Enter')jumpToPhotoPage()">
        <button class="btn-slate btn-sm" onclick="jumpToPhotoPage()">Go</button>
      </div>
      <button class="btn-slate" id="photo-pg-next" onclick="changePhotoPage(1)">Next</button>
    </div>
  </div>

  <!-- By Species sub-view -->
  <div id="photo-view-species-panel" style="display:none">
    <div class="sbar">
      <input id="photo-sp-q" placeholder="Filter species..." oninput="loadPhotosBySpecies()">
      <span style="color:#475569;font-size:12px;white-space:nowrap" id="photo-sp-rcount"></span>
    </div>
    <div class="grid" id="photo-species-grid"></div>
  </div>

  <div style="height:20px"></div>
</div>

<!-- PHOTO SPECIES MODAL — photos of one species -->
<div class="modal" id="photo-species-modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="mbox" style="max-width:700px;max-height:85vh;overflow-y:auto" id="photo-species-mbox">
    <div style="position:sticky;top:0;background:#0c1e35;z-index:10;padding:14px 16px 10px;border-bottom:1px solid #1e3a5f;display:flex;justify-content:space-between;align-items:center">
      <div id="photo-species-title" style="color:#38bdf8;font-weight:600;font-size:14px"></div>
      <button class="btn-slate btn-sm" onclick="document.getElementById('photo-species-modal').classList.remove('show')">Close</button>
    </div>
    <div id="photo-species-inner" style="padding:16px"></div>
  </div>
</div>

<!-- SCAN -->
<div class="page" id="tab-scan">
  <div class="hdr"><h2>Scan Videos/Photos</h2></div>

  <h3 style="color:#38bdf8;font-size:15px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #1e3a5f">Scan Videos</h3>
  <div class="scan-box">
    <h3>Add folder to scan</h3>
    <div class="folder-row">
      <input id="scan-path" placeholder="Paste folder path e.g. V:\\Footage\\Lembeh">
      <button class="btn-slate" onclick="browseFolder()">Browse</button>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <label style="font-size:13px;color:#64748b">Workers:
        <select id="workers" style="margin-left:6px;width:70px">
          <option>4</option><option>8</option><option selected>10</option><option>12</option><option>16</option>
        </select>
      </label>
      <label style="font-size:13px;color:#64748b;display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="batch-mode"> Batch mode (50% cheaper, async)
      </label>
      <button class="btn-green" id="scan-btn" onclick="startScan()">Start Scan</button>
      <button class="btn-red" id="stop-btn" style="display:none" onclick="stopScan()">Stop</button>
    </div>
    <div style="margin-top:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <label style="font-size:13px;color:#64748b">Region:
        <select id="scan-region" style="margin-left:6px;min-width:200px;font-size:12px"></select>
      </label>
    </div>
    <div class="prog-wrap" id="prog-wrap">
      <div class="prog-label" id="prog-label">Starting...</div>
      <div class="prog-bar-bg"><div class="prog-bar" id="prog-bar"></div></div>
      <div class="prog-log" id="prog-log"></div>
    </div>
  </div>
  <div class="scan-box">
    <h3>Indexed video folders</h3>
    <div id="folder-list"><div style="color:#334155;font-size:13px">Loading...</div></div>
  </div>

  <h3 style="color:#38bdf8;font-size:15px;margin-bottom:12px;margin-top:24px;padding-bottom:8px;border-bottom:1px solid #1e3a5f">Scan Photos</h3>
  <div class="scan-box">
    <h3>Add folder to scan</h3>
    <p style="color:#475569;font-size:12px;margin-bottom:12px">Supports JPG/JPEG and RAW files (CR2, NEF, ARW, DNG and more). RAW requires <code style="color:#7dd4fc;font-size:11px">pip install rawpy</code>.</p>
    <div class="folder-row">
      <input id="photo-scan-path" placeholder="Paste folder path e.g. D:/Photos/Lembeh">
      <button class="btn-slate" onclick="browsePhotoFolder()">Browse</button>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <label style="font-size:13px;color:#64748b">Workers:
        <select id="photo-workers" style="margin-left:6px;width:70px">
          <option>4</option><option>8</option><option selected>10</option><option>12</option><option>16</option>
        </select>
      </label>
      <label style="font-size:13px;color:#64748b;display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="photo-batch-mode"> Batch mode (50% cheaper, async)
      </label>
      <button class="btn-green" id="photo-scan-btn" onclick="startPhotoScan()">Scan Photos</button>
      <button class="btn-red" id="photo-stop-btn" style="display:none" onclick="stopPhotoScan()">Stop</button>
    </div>
    <div style="margin-top:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <label style="font-size:13px;color:#64748b">Region:
        <select id="photo-scan-region" style="margin-left:6px;min-width:200px;font-size:12px"></select>
      </label>
    </div>
    <div class="prog-wrap" id="photo-prog-wrap">
      <div class="prog-label" id="photo-prog-label">Starting...</div>
      <div class="prog-bar-bg"><div class="prog-bar" id="photo-prog-bar"></div></div>
      <div class="prog-log" id="photo-prog-log"></div>
    </div>
  </div>
  <div class="scan-box">
    <h3>Indexed photo folders</h3>
    <div id="photo-folder-list"><div style="color:#334155;font-size:13px">Loading...</div></div>
  </div>
</div>

<!-- TOOLS -->
<div class="page" id="tab-tools">
  <div class="hdr"><h2>Tools</h2></div>
  <div class="scan-box">
    <h3>Find and Replace Species</h3>
    <p style="color:#475569;font-size:12px;margin-bottom:14px">Fix inconsistent species names across the entire index.</p>
    <div class="tools-row">
      <div style="flex:1;min-width:180px">
        <div style="font-size:11px;color:#334155;margin-bottom:5px;text-transform:uppercase;letter-spacing:.05em">Search (partial match)</div>
        <input id="fr-search" placeholder="e.g. bornella" oninput="frPreview()">
      </div>
      <div style="flex:1;min-width:180px">
        <div style="font-size:11px;color:#334155;margin-bottom:5px;text-transform:uppercase;letter-spacing:.05em">Replace with</div>
        <input id="fr-replace" placeholder="e.g. Bornella sp.">
      </div>
      <button class="btn-green" onclick="frExecute()" style="align-self:flex-end">Replace All</button>
    </div>
    <div id="fr-preview" class="preview-box" style="display:none"></div>
  </div>
  <div class="scan-box">
    <h3>Normalise Em Dashes</h3>
    <p style="color:#475569;font-size:12px;margin-bottom:14px">Replace all em dashes in species names with plain hyphens.</p>
    <button class="btn-amber" onclick="fixDashes()">Fix All Em Dashes</button>
    <div id="dash-result" style="margin-top:10px;font-size:12px;color:#64748b"></div>
  </div>
</div>

<!-- MULTISELECT BAR -->
<div class="sel-bar" id="sel-bar" style="display:none">
  <span id="sel-count">0 selected</span>
  <button class="btn-green btn-sm" onclick="bulkReviewed()">Mark Reviewed</button>
  <button class="btn-blue btn-sm" onclick="bulkSetSpecies()">Set Species</button>
  <button class="btn-slate btn-sm" onclick="bulkSetSiteDate()">Set Location/Date</button>
  <button class="btn-purple btn-sm" onclick="openConfirmIDPicker('frames')">&#10003; Confirm ID &amp; Lookup</button>
  <button class="btn-slate btn-sm" onclick="clearSelection()">Clear</button>
</div>

<!-- PHOTO MULTISELECT BAR -->
<div class="sel-bar" id="photo-sel-bar" style="display:none">
  <span id="photo-sel-count">0 selected</span>
  <button class="btn-green btn-sm" onclick="photosBulkReviewed()">Mark Reviewed</button>
  <button class="btn-blue btn-sm" onclick="photosBulkSetSpecies()">Set Species</button>
  <button class="btn-slate btn-sm" onclick="photosBulkSetSiteDate()">Set Location/Date</button>
  <button class="btn-purple btn-sm" onclick="openConfirmIDPicker('photos')">&#10003; Confirm ID &amp; Lookup</button>
  <button class="btn-amber btn-sm" onclick="openBatchRename('photos')">Batch Rename</button>
  <button class="btn-red btn-sm" onclick="photosBulkMarkDelete()">Mark for Delete</button>
  <button class="btn-slate btn-sm" onclick="clearPhotoSelection()">Clear</button>
</div>

<!-- BATCH RENAME MODAL -->
<div class="modal" id="batch-rename-modal" onclick="if(event.target===this)closeBatchRename()">
  <div class="mbox" style="max-width:440px">
    <div class="mbody">
      <div class="mtitle" style="margin-bottom:6px">Batch Rename Files</div>
      <p style="color:#475569;font-size:12px;margin-bottom:14px">Files will be renamed using the primary species name (first tag) followed by a number. Preview shown below.</p>
      <div class="lbl" style="margin-top:0">Custom prefix (optional)</div>
      <input id="batch-rename-prefix" placeholder="Leave blank to use species name" style="width:100%;font-size:13px;margin-top:6px;margin-bottom:10px" oninput="updateBatchRenamePreview()">
      <div class="lbl">Preview</div>
      <div id="batch-rename-preview" class="preview-box" style="margin-top:6px;margin-bottom:14px"></div>
      <div id="batch-rename-progress" style="font-size:12px;color:#f59e0b;margin-bottom:10px;display:none"></div>
      <div class="mactions">
        <button class="btn-amber" onclick="executeBatchRename()">Rename Files</button>
        <button class="btn-slate" onclick="closeBatchRename()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- CONFIRM ID SPECIES PICKER MODAL -->
<div class="modal" id="confirm-id-modal" onclick="if(event.target===this)closeConfirmIDModal()">
  <div class="mbox" style="max-width:420px">
    <div class="mbody">
      <div class="mtitle" style="margin-bottom:6px">&#10003; Confirm ID &amp; Lookup</div>
      <p style="color:#475569;font-size:12px;margin-bottom:14px">Tick the species you want to confirm and look up. The habitat, behaviours and notes will be updated for all selected items.</p>
      <div id="confirm-id-species-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:16px"></div>
      <div id="confirm-id-progress" style="font-size:12px;color:#f59e0b;margin-bottom:10px;display:none"></div>
      <div class="mactions">
        <button class="btn-purple" onclick="runConfirmIDLookup()">Look up selected</button>
        <button class="btn-slate" onclick="closeConfirmIDModal()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- CLIPS MULTISELECT BAR -->
<div class="sel-bar" id="clips-sel-bar" style="display:none">
  <span id="clips-sel-count">0 selected</span>
  <button class="btn-amber btn-sm" onclick="batchRenameSelectedClips()">Batch Rename</button>
  <button class="btn-purple btn-sm" onclick="confirmIDSelectedClips()">&#10003; Confirm ID &amp; Lookup</button>
  <button class="btn-red btn-sm" onclick="deleteSelectedClips()">Mark for Delete</button>
  <button class="btn-slate btn-sm" onclick="clearClipSelection()">Clear</button>
</div>

<!-- BULK SPECIES MODAL -->
<div class="modal" id="bulk-modal" onclick="if(event.target===this)closeBulkModal()">
  <div class="mbox" style="max-width:400px">
    <div class="mbody">
      <div class="mtitle" style="margin-bottom:12px">Set Species for <span id="bulk-count">0</span> selected frames</div>
      <p style="color:#475569;font-size:12px;margin-bottom:14px">Replaces all existing species tags on selected frames.</p>
      <div class="lbl" style="margin-top:0">Species</div>
      <div class="ac-wrap" style="margin-top:6px">
        <input id="bulk-species-input" placeholder="Type to search existing species..." style="width:100%;font-size:13px;padding:7px 10px" oninput="bulkAcSearch()" onkeydown="bulkAcKey(event)">
        <div class="ac-list" id="bulk-ac-list"></div>
      </div>
      <div id="bulk-species-tags" style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px"></div>
      <div class="mactions" style="margin-top:16px">
        <button class="btn-green" onclick="bulkSpeciesConfirm()">Apply to Selected</button>
        <button class="btn-slate" onclick="closeBulkModal()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- BULK LOCATION/DATE MODAL -->
<div class="modal" id="bulk-site-modal" onclick="if(event.target===this)closeBulkSiteModal()">
  <div class="mbox" style="max-width:440px">
    <div class="mbody">
      <div class="mtitle" style="margin-bottom:4px">Set Location/Date for <span id="bulk-site-count">0</span> frames</div>
      <p style="color:#475569;font-size:12px;margin-bottom:14px">Only filled fields will be updated. Leave blank to keep existing values.</p>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Country</div>
          <input id="bulk-country-input" placeholder="e.g. Indonesia" oninput="bulkCountryAcSearch()">
          <div class="ac-list" id="bulk-country-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Region</div>
          <input id="bulk-region-input" placeholder="e.g. North Sulawesi" oninput="bulkRegionAcSearch()">
          <div class="ac-list" id="bulk-region-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Area</div>
          <input id="bulk-area-input" placeholder="e.g. Lembeh" oninput="bulkAreaAcSearch()">
          <div class="ac-list" id="bulk-area-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Dive Site</div>
          <input id="bulk-site-input" placeholder="e.g. Black Sand" oninput="bulkSiteAcSearch()">
          <div class="ac-list" id="bulk-site-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Dive Date</div>
          <input id="bulk-date-input" type="date">
        </div>
      </div>
      <div class="mactions" style="margin-top:16px">
        <button class="btn-green" onclick="bulkSiteDateConfirm()">Apply to Selected</button>
        <button class="btn-slate" onclick="closeBulkSiteModal()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- CLIP FRAMES MODAL -->
<div class="modal" id="clip-frames-modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="mbox" style="max-width:700px;max-height:85vh;overflow-y:auto" id="clip-frames-mbox">
    <div style="position:sticky;top:0;background:#0c1e35;z-index:10;padding:14px 16px 10px;border-bottom:1px solid #1e3a5f;display:flex;justify-content:space-between;align-items:center;gap:8px">
      <div id="clip-frames-title" style="color:#38bdf8;font-weight:600;font-size:14px"></div>
      <div style="display:flex;gap:6px">
        <button class="btn-blue btn-sm" id="clip-set-species-btn">Set Species for all</button>
        <button class="btn-purple btn-sm" id="clip-confirm-id-btn">&#10003; Confirm ID &amp; Lookup</button>
        <button class="btn-amber btn-sm" id="clip-batch-rename-btn">Batch Rename</button>
        <button class="btn-red btn-sm" id="clip-delete-btn">Mark all for Delete</button>
      </div>
    </div>
    <div id="clip-frames-inner" style="padding:16px"></div>
  </div>
</div>

<!-- DETAIL MODAL -->
<div class="modal" id="modal" onclick="if(event.target===this)closeModal()">
  <div class="mbox">
    <img class="mimg" id="m-img" src="">
    <div class="mbody">
      <div class="mtitle" id="m-title"></div>
      <div class="msub" id="m-sub"></div>

      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Country</div>
          <input id="m-country" placeholder="e.g. Indonesia" oninput="countryAcSearch()">
          <div class="ac-list" id="country-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Region</div>
          <input id="m-region" placeholder="e.g. North Sulawesi" oninput="regionAcSearch()">
          <div class="ac-list" id="region-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Area</div>
          <input id="m-area" placeholder="e.g. Lembeh" oninput="areaAcSearch()">
          <div class="ac-list" id="area-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Dive Site</div>
          <input id="m-site" placeholder="e.g. Black Sand" oninput="siteAcSearch()">
          <div class="ac-list" id="site-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field" style="max-width:200px">
          <div class="lbl">Dive Date</div>
          <input id="m-date" type="date">
        </div>
        <button class="btn-slate btn-sm" style="align-self:flex-end" onclick="saveSiteDate()">Save</button>
      </div>

      <div class="lbl">Species</div>
      <div id="m-species" style="margin-bottom:6px"></div>
      <div style="display:flex;gap:6px;margin-bottom:14px;align-items:center">
        <div class="ac-wrap">
          <input id="new-species" placeholder="Add species..." style="width:100%;font-size:12px;padding:5px 9px" oninput="acSearch()" onkeydown="acKey(event)">
          <div class="ac-list" id="ac-list"></div>
        </div>
        <button class="btn-blue btn-sm" onclick="addSpecies()">Add</button>
      </div>

      <div style="display:flex;align-items:center;gap:10px;margin-top:4px;margin-bottom:8px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px">
          <div class="lbl" style="margin-top:0">Water Visibility</div>
          <select id="m-visibility" style="width:100%;font-size:12px;padding:5px 9px;margin-top:4px">
            <option value="">—</option>
            <option value="poor">Poor</option>
            <option value="fair">Fair</option>
            <option value="good">Good</option>
            <option value="excellent">Excellent</option>
          </select>
        </div>
        <div style="flex:1;min-width:140px">
          <div class="lbl" style="margin-top:0">ID Confidence</div>
          <select id="m-id-confidence" style="width:100%;font-size:12px;padding:5px 9px;margin-top:4px" onchange="idConfidenceChanged()">
            <option value="">—</option>
            <option value="uncertain">Uncertain</option>
            <option value="probable">Probable</option>
            <option value="confirmed">✅ Confirmed — Look up species</option>
          </select>
        </div>
        <div id="lookup-status" style="font-size:12px;color:#f59e0b;align-self:flex-end;padding-bottom:6px"></div>
      </div>

      <div class="lbl">Habitat</div>
      <textarea id="m-habitat" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#dbeafe;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:48px;margin-bottom:6px;font-family:inherit"></textarea>
      <div class="lbl" id="beh-lbl">Behaviours</div>
      <textarea id="m-behs" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#dbeafe;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:48px;margin-bottom:6px;font-family:inherit"></textarea>
      <div class="lbl">Notes</div>
      <textarea id="m-notes" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#94a3b8;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:60px;margin-bottom:4px;font-family:inherit"></textarea>
      <button class="btn-slate btn-sm" style="margin-bottom:10px" onclick="saveFrameDetails()">Save Details</button>

      <div class="mactions">
        <button class="btn-green" onclick="openFile()">Open in VLC</button>
        <button class="btn-amber" onclick="openFolder()">Open Folder</button>
        <button class="btn-purple" onclick="copyPath()">Copy Path</button>
        <button class="btn-slate" onclick="renameFile()">Rename File</button>
        <button id="btn-reviewed" onclick="toggleReviewed()" style="background:#0f2540;color:#475569;border:1px solid #1e3a5f">Mark Reviewed</button>
        <button class="btn-slate" onclick="closeModal()">Close</button>
      </div>
      <div class="mnote" id="m-note"></div>
    </div>
  </div>
</div>

<!-- PHOTO DETAIL MODAL -->
<div class="modal" id="photo-modal" onclick="if(event.target===this)closePhotoModal()">
  <div class="mbox">
    <img class="mimg" id="pm-img" src="">
    <div class="mbody">
      <div class="mtitle" id="pm-title"></div>
      <div class="msub" id="pm-sub"></div>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Country</div>
          <input id="pm-country" placeholder="e.g. Indonesia" oninput="pmCountryAcSearch()">
          <div class="ac-list" id="pm-country-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Region</div>
          <input id="pm-region" placeholder="e.g. North Sulawesi" oninput="pmRegionAcSearch()">
          <div class="ac-list" id="pm-region-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field">
          <div class="lbl">Area</div>
          <input id="pm-area" placeholder="e.g. Lembeh" oninput="pmAreaAcSearch()">
          <div class="ac-list" id="pm-area-ac-list"></div>
        </div>
        <div class="meta-field">
          <div class="lbl">Dive Site</div>
          <input id="pm-site" placeholder="e.g. Black Sand" oninput="pmSiteAcSearch()">
          <div class="ac-list" id="pm-site-ac-list"></div>
        </div>
      </div>
      <div class="meta-row">
        <div class="meta-field" style="max-width:200px">
          <div class="lbl">Dive Date</div>
          <input id="pm-date" type="date">
        </div>
        <button class="btn-slate btn-sm" style="align-self:flex-end" onclick="savePhotoSiteDate()">Save</button>
      </div>
      <div class="lbl">Species</div>
      <div id="pm-species" style="margin-bottom:6px"></div>
      <div style="display:flex;gap:6px;margin-bottom:14px;align-items:center">
        <div class="ac-wrap">
          <input id="pm-new-species" placeholder="Add species..." style="width:100%;font-size:12px;padding:5px 9px" oninput="pmAcSearch()" onkeydown="pmAcKey(event)">
          <div class="ac-list" id="pm-ac-list"></div>
        </div>
        <button class="btn-blue btn-sm" onclick="addPhotoSpecies()">Add</button>
      </div>

      <div style="display:flex;align-items:center;gap:10px;margin-top:4px;margin-bottom:8px;flex-wrap:wrap">
        <div style="flex:1;min-width:120px">
          <div class="lbl" style="margin-top:0">Water Visibility</div>
          <select id="pm-visibility" style="width:100%;font-size:12px;padding:5px 9px;margin-top:4px">
            <option value="">—</option>
            <option value="poor">Poor</option>
            <option value="fair">Fair</option>
            <option value="good">Good</option>
            <option value="excellent">Excellent</option>
          </select>
        </div>
        <div style="flex:1;min-width:140px">
          <div class="lbl" style="margin-top:0">ID Confidence</div>
          <select id="pm-id-confidence" style="width:100%;font-size:12px;padding:5px 9px;margin-top:4px" onchange="pmIdConfidenceChanged()">
            <option value="">—</option>
            <option value="uncertain">Uncertain</option>
            <option value="probable">Probable</option>
            <option value="confirmed">✅ Confirmed — Look up species</option>
          </select>
        </div>
        <div id="pm-lookup-status" style="font-size:12px;color:#f59e0b;align-self:flex-end;padding-bottom:6px"></div>
      </div>

      <div class="lbl">Habitat</div>
      <textarea id="pm-habitat" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#dbeafe;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:48px;margin-bottom:6px;font-family:inherit"></textarea>
      <div class="lbl" id="pm-beh-lbl">Behaviours</div>
      <textarea id="pm-behs" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#dbeafe;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:48px;margin-bottom:6px;font-family:inherit"></textarea>
      <div class="lbl">Notes</div>
      <textarea id="pm-notes" style="width:100%;background:#0c1e35;border:1px solid #1e3a5f;color:#94a3b8;padding:6px 9px;border-radius:8px;font-size:13px;line-height:1.5;resize:vertical;min-height:60px;margin-bottom:4px;font-family:inherit"></textarea>
      <button class="btn-slate btn-sm" style="margin-bottom:10px" onclick="savePhotoDetails()">Save Details</button>

      <div class="mactions">
        <button class="btn-green" onclick="openPhoto()">Open Photo</button>
        <button class="btn-amber" onclick="openPhotoFolder()">Open Folder</button>
        <button class="btn-purple" onclick="copyPhotoPath()">Copy Path</button>
        <button class="btn-slate" onclick="renamePhoto()">Rename File</button>
        <button id="pm-btn-reviewed" onclick="togglePhotoReviewed()" style="background:#0f2540;color:#475569;border:1px solid #1e3a5f">Mark Reviewed</button>
        <button id="pm-btn-delete" onclick="togglePhotoDelete()" style="background:#0f2540;color:#475569;border:1px solid #1e3a5f">Mark for Delete</button>
        <button class="btn-slate" onclick="closePhotoModal()">Close</button>
      </div>
      <div class="mnote" id="pm-note"></div>
    </div>
  </div>
</div>

<!-- SETTINGS MODAL -->
<div class="modal" id="settings-modal" onclick="if(event.target===this)closeSettings()">
  <div class="mbox" style="max-width:480px">
    <div class="mbody">
      <div class="mtitle" style="margin-bottom:16px;font-size:16px">&#9881; Settings</div>

      <div class="lbl" style="margin-top:0">Anthropic API Key</div>
      <div style="display:flex;gap:8px;margin-top:6px;margin-bottom:4px">
        <input id="settings-api-key" type="password" placeholder="sk-ant-..." style="flex:1;font-size:13px">
        <button class="btn-slate btn-sm" onclick="toggleApiKeyVisibility()" id="api-key-toggle">Show</button>
      </div>
      <div style="color:#334155;font-size:11px;margin-bottom:16px">Get your key at <a href="https://console.anthropic.com" target="_blank" style="color:#7dd4fc;text-decoration:none">console.anthropic.com</a></div>

      <div class="lbl">Default Region</div>
      <div style="color:#475569;font-size:11px;margin-bottom:6px">Used as the default for all new scans. Can be overridden per scan.</div>
      <select id="settings-region" style="width:100%;font-size:13px;margin-bottom:20px"></select>

      <div class="mactions">
        <button class="btn-green" onclick="saveSettings()">Save</button>
        <button class="btn-slate" onclick="closeSettings()">Cancel</button>
      </div>
      <div id="settings-note" class="mnote"></div>
    </div>
  </div>
</div>

<script>
var current = null, scanES = null, selected = {}, lastClickedFid = null;
var allSpecies = [], allSites = [], allCountries = [], allRegions = [], allAreas = [];
var clipSelectMode = false, selectedClips = {}, lastClickedVid = null;
var NONE = String.fromCharCode(110,111,110,101);
var currentView = 'frames';
var currentPage = 1;
var PAGE_SIZE = 100;
var currentPhoto = null, photoPage = 1, photoScanES = null;
var currentPhotoView = 'all';
var selectedPhotos = {}, lastClickedPid = null;

function setView(v) {
  currentView = v; currentPage = 1;
  clipSelectMode = false;
  selectedClips = {};
  document.getElementById('clips-sel-bar').style.display = NONE;
  document.getElementById('view-frames').className = v==='frames' ? 'btn-blue btn-sm' : 'btn-sm';
  document.getElementById('view-frames').style.background = v==='frames' ? '' : 'transparent';
  document.getElementById('view-frames').style.color = v==='frames' ? '' : '#475569';
  document.getElementById('view-clips').className = v==='clips' ? 'btn-blue btn-sm' : 'btn-sm';
  document.getElementById('view-clips').style.background = v==='clips' ? '' : 'transparent';
  document.getElementById('view-clips').style.color = v==='clips' ? '' : '#475569';
  var selBtn = document.getElementById('clips-select-mode-btn');
  selBtn.style.display = v==='clips' ? '' : NONE;
  selBtn.textContent = 'Select Mode';
  selBtn.style.background = '#0c3a5e';
  selBtn.style.color = '#7dd4fc';
  load();
}

function switchTab(t) {
  ['home','browse','photos','scan','tools'].forEach(function(n) {
    document.getElementById('tab-btn-'+n).classList.toggle('active', n===t);
    document.getElementById('tab-'+n).classList.toggle('active', n===t);
  });
  if (t==='scan') { loadFolderList(); loadPhotoFolderList(); }
  if (t==='photos') { loadPhotoStat(); loadPhotos(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas(); }
  if (t==='home') { loadHomeStat(); }
}

async function load() {
  var q = document.getElementById('q').value;
  var sp = document.getElementById('sp').value;
  var site = document.getElementById('site-filter').value;
  var country = document.getElementById('country-filter').value;
  var region = document.getElementById('region-filter').value;
  var area = document.getElementById('area-filter').value;
  var sort = document.getElementById('sort-filter').value;
  var p = new URLSearchParams();
  if (q) p.set('q', q);
  if (sp && sp !== 'All species') p.set('species', sp);
  if (site && site !== 'All sites') p.set('site', site);
  if (country && country !== 'All countries') p.set('country', country);
  if (region && region !== 'All regions') p.set('region', region);
  if (area && area !== 'All areas') p.set('area', area);
  if (sort) p.set('sort', sort);

  if (currentView === 'clips') {
    p.set('page', currentPage); p.set('page_size', PAGE_SIZE);
    var r = await fetch('/api/clips?' + p);
    var resp = await r.json();
    var data = resp.items || []; var total = resp.total || 0;
    var totalPages = Math.ceil(total / PAGE_SIZE);
    document.getElementById('rcount').textContent = total + ' clips';
    renderPagination(totalPages, total, 'clips');
    var html = '';
    window._clips = {};
    data.forEach(function(c) {
      window._clips[c.vid_id] = c;
      var isSelected = !!selectedClips[c.vid_id];
      var cls = 'card' + (c.marked_delete ? ' marked-delete' : '') + (isSelected ? ' selected' : '');
      var tags = c.species.slice(0,3).map(function(s){ return '<span class="tag">'+s+'</span>'; }).join('');
      var dur = Math.floor((c.duration||0)/60)+':'+(''+(Math.floor((c.duration||0)%60))).padStart(2,'0');
      html += '<div class="'+cls+'" data-vid="'+c.vid_id+'" data-filename="'+c.filename.replace(/"/g,'&quot;')+'">';
      html += '<div style="position:relative">';
      html += '<img src="/thumb/'+c.thumb_id+'" onerror="this.style.display=String.fromCharCode(110,111,110,101)">';
      html += '<span style="position:absolute;bottom:4px;right:6px;background:rgba(0,0,0,.7);color:#94a3b8;font-size:10px;padding:2px 5px;border-radius:4px">'+c.frame_count+' frames &middot; '+dur+'</span>';
      if (clipSelectMode) html += '<span style="position:absolute;top:6px;left:6px;background:'+(isSelected?'#f59e0b':'rgba(0,0,0,.5)')+';border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;font-size:12px">'+(isSelected?'&#10003;':'')+'</span>';
      html += '</div>';
      html += '<div class="ci"><div class="fname">'+c.filename+'</div>';
      html += '<div class="tags">'+tags+'</div>';
      html += '<div class="bot"><span class="hab">'+(c.area||c.dive_site||'')+'</span><span class="vis-'+(c.visibility||'')+'">'+(c.visibility||'')+'</span></div>';
      if (!clipSelectMode) {
        html += '<div style="display:flex;gap:4px;margin-top:6px;flex-wrap:wrap">';
        html += '<button class="btn-green btn-sm" style="font-size:10px;padding:3px 7px" data-path="'+c.path+'" onclick="event.stopPropagation();playClip(this.dataset.path)">Play in VLC</button>';
        html += '<button class="'+(c.marked_delete?'btn-amber':'btn-red')+' btn-sm" style="font-size:10px;padding:3px 7px" data-vid="'+c.vid_id+'" data-marked="'+(c.marked_delete?'1':'0')+'" onclick="event.stopPropagation();clipMarkDelete(this.dataset.vid, this.dataset.marked, this)">'+( c.marked_delete ? 'Unmark Delete' : 'Mark for Delete')+'</button>';
        html += '</div>';
      }
      html += '</div></div>';
    });
    document.getElementById('grid').innerHTML = html;
    document.getElementById('grid').querySelectorAll('.card[data-vid]').forEach(function(el) {
      el.addEventListener('click', function(e) {
        if (clipSelectMode) {
          clipCardSelect(el.dataset.vid, el.dataset.filename);
        } else {
          showClipFrames(el.dataset.vid, el.dataset.filename);
        }
      });
    });
    document.getElementById('marked-count').textContent = data.filter(function(c){return c.marked_delete;}).length;
    document.getElementById('btn-purge').style.display = data.filter(function(c){return c.marked_delete;}).length > 0 ? '' : NONE;
    return;
  }

  p.set('page', currentPage); p.set('page_size', PAGE_SIZE);
  var r = await fetch('/api/frames?' + p);
  var resp = await r.json();
  var data = resp.items; var total = resp.total;
  var totalPages = Math.ceil(total / PAGE_SIZE);
  window._frames = {};
  document.getElementById('rcount').textContent = total + ' results';
  renderPagination(totalPages, total, 'frames');
  var html = '';
  data.forEach(function(f) {
    window._frames[f.id] = f;
    var cls = 'card' + (f.marked_delete ? ' marked-delete' : '') + (selected[f.id] ? ' selected' : '');
    var del = f.marked_delete ? '[DEL] ' : '';
    var tags = (f.species||[]).slice(0,3).map(function(s){ return '<span class="tag">'+s+'</span>'; }).join('');
    html += '<div class="'+cls+'" data-fid="'+f.id+'">';
    html += '<img src="/thumb/'+f.id+'" onerror="this.style.display=String.fromCharCode(110,111,110,101)">';
    html += '<div class="ci"><div class="fname">'+del+f.filename+' '+fmt(f.timestamp)+'</div>';
    html += '<div class="tags">'+tags+'</div>';
    html += '<div class="bot"><span class="hab">'+(f.habitat||'')+'</span><span class="vis-'+(f.visibility||'')+'">'+(f.visibility||'')+'</span></div>';
    html += '</div></div>';
  });
  document.getElementById('grid').innerHTML = html;
  document.getElementById('grid').querySelectorAll('.card').forEach(function(el) {
    el.addEventListener('click', function(e) { cardClick(e, el.dataset.fid); });
  });
  var marked = data.filter(function(f){ return f.marked_delete; }).length;
  document.getElementById('marked-count').textContent = marked;
  document.getElementById('btn-purge').style.display = marked > 0 ? '' : NONE;
  updateSelBar();
}

async function showClipFrames(vidId, filename) {
  var r = await fetch('/api/frames?vid_id='+encodeURIComponent(vidId));
  var frames = await r.json();
  if (!frames.length) return;
  // Switch to frames view and show this clip's frames in the modal
  window._frames = {};
  frames.forEach(function(f){ window._frames[f.id] = f; });
  // Build a frame strip modal
  var html = '<div style="padding:16px"><div style="color:#38bdf8;font-weight:600;font-size:14px;margin-bottom:12px">'+filename+' — '+frames.length+' frames</div>';
  html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px">';
  frames.forEach(function(f) {
    var tags = (f.species||[]).slice(0,2).map(function(s){ return '<span class="tag">'+s+'</span>'; }).join('');
    html += '<div class="card" data-fid="'+f.id+'" style="cursor:pointer">';
    html += '<img src="/thumb/'+f.id+'" style="width:100%;height:100px;object-fit:cover;display:block" onerror="this.style.display=String.fromCharCode(110,111,110,101)">';
    html += '<div class="ci"><div class="fname">'+fmt(f.timestamp)+'</div><div class="tags">'+tags+'</div></div>';
    html += '</div>';
  });
  html += '</div></div>';
  document.getElementById('clip-frames-inner').innerHTML = html;
  document.getElementById('clip-frames-modal').classList.add('show');
  document.getElementById('clip-frames-mbox').scrollTop = 0;
  // Replace buttons to remove any previously attached listeners
  ['clip-set-species-btn','clip-confirm-id-btn','clip-batch-rename-btn','clip-delete-btn'].forEach(function(id){
    var old = document.getElementById(id);
    var fresh = old.cloneNode(true);
    old.parentNode.replaceChild(fresh, old);
  });
  document.getElementById('clip-set-species-btn').addEventListener('click', function(){ clipBulkSpecies(vidId); });
  document.getElementById('clip-confirm-id-btn').addEventListener('click', function(){
    var frameIds = Object.keys(window._frames);
    document.getElementById('clip-frames-modal').classList.remove('show');
    openConfirmIDPickerForClip(frameIds);
  });
  document.getElementById('clip-batch-rename-btn').addEventListener('click', function(){
    document.getElementById('clip-frames-modal').classList.remove('show');
    var firstFrame = window._frames[Object.keys(window._frames)[0]];
    if (firstFrame) openBatchRename('clips', [{id: vidId, path: firstFrame.path, species: firstFrame.species, filename: firstFrame.filename}]);
  });
  document.getElementById('clip-delete-btn').addEventListener('click', function(){ clipBulkDelete(vidId); });
  document.getElementById('clip-frames-inner').querySelectorAll('.card').forEach(function(el) {
    el.addEventListener('click', function() {
      document.getElementById('clip-frames-modal').classList.remove('show');
      showModalById(el.dataset.fid);
    });
  });
}

function clipBulkSpecies(vidId) {
  var ids = Object.keys(window._frames);
  if (!ids.length) return;
  selected = {};
  ids.forEach(function(id){ selected[id] = true; });
  document.getElementById('clip-frames-modal').classList.remove('show');
  document.getElementById('bulk-count').textContent = ids.length;
  document.getElementById('bulk-species-input').value = '';
  document.getElementById('bulk-ac-list').style.display = NONE;
  document.getElementById('bulk-species-tags').innerHTML = '';
  window._bulkSpecies = [];
  document.getElementById('bulk-modal').classList.add('show');
  setTimeout(function(){ document.getElementById('bulk-species-input').focus(); }, 100);
}

async function clipBulkDelete(vidId) {
  var ids = Object.keys(window._frames);
  if (!ids.length) return;
  if (!confirm('Mark all ' + ids.length + ' frames in this clip for deletion?')) return;
  await fetch('/api/bulk_update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: ids, update: {marked_delete: 1}})});
  document.getElementById('clip-frames-modal').classList.remove('show');
  load();
}

async function clipMarkDelete(vidId, currentMarked, btn) {
  var marking = currentMarked === '0';
  if (marking && !confirm('Mark all frames in this clip for deletion?')) return;
  var r = await fetch('/api/frames?vid_id='+encodeURIComponent(vidId));
  var frames = await r.json();
  if (!frames.length) return;
  var ids = frames.map(function(f){ return f.id; });
  await fetch('/api/bulk_update', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: ids, update: {marked_delete: marking ? 1 : 0}})});
  btn.textContent = marking ? 'Unmark Delete' : 'Mark for Delete';
  btn.className = (marking ? 'btn-amber' : 'btn-red') + ' btn-sm';
  btn.dataset.marked = marking ? '1' : '0';
  btn.closest('.card').classList.toggle('marked-delete', marking);
}

function renderPagination(totalPages, total, type) {
  var bar = document.getElementById('pagination');
  if (totalPages <= 1) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  var start = (currentPage-1)*PAGE_SIZE+1, end = Math.min(currentPage*PAGE_SIZE, total);
  document.getElementById('pg-info').textContent = 'Page '+currentPage+' of '+totalPages+' ('+start+'-'+end+' of '+total+' '+type+')';
  document.getElementById('pg-prev').disabled = currentPage <= 1;
  document.getElementById('pg-next').disabled = currentPage >= totalPages;
}

function changePage(dir) {
  currentPage += dir;
  load();
  window.scrollTo(0, 0);
}

function jumpToPage() {
  var val = parseInt(document.getElementById('pg-jump').value);
  if (!val || val < 1) return;
  currentPage = val;
  document.getElementById('pg-jump').value = '';
  load();
  window.scrollTo(0, 0);
}

async function playClip(path) {
  await fetch('/api/open?path='+encodeURIComponent(path)+'&ts=0');
}

function fmt(s) { var m=Math.floor(s/60); return m+':'+(''+(Math.floor(s%60))).padStart(2,'0'); }
function doExport() { window.location='/api/export_csv'; }

function cardClick(e, fid) {
  if (e.ctrlKey || e.metaKey) {
    if (selected[fid]) { delete selected[fid]; } else { selected[fid] = true; }
    var el = document.querySelector('[data-fid="'+fid+'"]');
    if (el) el.classList.toggle('selected', !!selected[fid]);
    lastClickedFid = fid; updateSelBar();
  } else if (e.shiftKey) {
    if (!lastClickedFid) {
      selected[fid] = true;
      var el = document.querySelector('[data-fid="'+fid+'"]');
      if (el) el.classList.add('selected');
      lastClickedFid = fid; updateSelBar();
    } else {
      var cards = Array.from(document.getElementById('grid').querySelectorAll('.card'));
      var ids = cards.map(function(c){ return c.dataset.fid; });
      var a = ids.indexOf(lastClickedFid), b = ids.indexOf(fid);
      var start = Math.min(a,b), end = Math.max(a,b);
      for (var i=start; i<=end; i++) { selected[ids[i]] = true; cards[i].classList.add('selected'); }
      updateSelBar();
    }
  } else {
    showModalById(fid); lastClickedFid = fid;
  }
}

function updateSelBar() {
  var n = Object.keys(selected).length;
  document.getElementById('sel-bar').style.display = n > 0 ? 'flex' : NONE;
  document.getElementById('sel-count').textContent = n + ' selected';
}

function clearSelection() { selected = {}; lastClickedFid = null; load(); }

// ── Clip Select Mode ──────────────────────────────────────────────────────────

function toggleClipSelectMode() {
  clipSelectMode = !clipSelectMode;
  selectedClips = {};
  var btn = document.getElementById('clips-select-mode-btn');
  if (clipSelectMode) {
    btn.textContent = 'Exit Select Mode';
    btn.style.background = '#f59e0b';
    btn.style.color = '#000';
  } else {
    btn.textContent = 'Select Mode';
    btn.style.background = '#0c3a5e';
    btn.style.color = '#7dd4fc';
    document.getElementById('clips-sel-bar').style.display = NONE;
  }
  load();
}

function clipCardSelect(vid, filename) {
  if (selectedClips[vid]) {
    delete selectedClips[vid];
  } else {
    selectedClips[vid] = { vid_id: vid, filename: filename,
      path: window._clips[vid] ? window._clips[vid].path : '',
      species: window._clips[vid] ? window._clips[vid].species : [] };
  }
  updateClipsSelBar();
  // Update card appearance
  var el = document.querySelector('[data-vid="'+vid+'"]');
  if (el) {
    el.classList.toggle('selected', !!selectedClips[vid]);
    var dot = el.querySelector('span[style*="position:absolute;top:6px"]');
    if (dot) {
      dot.style.background = selectedClips[vid] ? '#f59e0b' : 'rgba(0,0,0,.5)';
      dot.innerHTML = selectedClips[vid] ? '&#10003;' : '';
    }
  }
}

function updateClipsSelBar() {
  var n = Object.keys(selectedClips).length;
  document.getElementById('clips-sel-bar').style.display = n > 0 ? 'flex' : NONE;
  document.getElementById('clips-sel-count').textContent = n + ' clip' + (n !== 1 ? 's' : '') + ' selected';
}

function clearClipSelection() {
  selectedClips = {}; updateClipsSelBar(); load();
}

function batchRenameSelectedClips() {
  var items = Object.values(selectedClips);
  if (!items.length) return;
  openBatchRename('clips', items);
}

function confirmIDSelectedClips() {
  var items = Object.values(selectedClips);
  if (!items.length) return;
  // Collect all species across selected clips
  var allSp = {};
  items.forEach(function(c){ (c.species||[]).forEach(function(s){ allSp[s]=true; }); });
  _confirmIDTarget = 'clips';
  _confirmIDClipIds = items.map(function(c){ return c.vid_id; });
  showConfirmIDModal(Object.keys(allSp));
}

async function deleteSelectedClips() {
  var items = Object.values(selectedClips);
  if (!items.length) return;
  if (!confirm('Mark ' + items.length + ' clip(s) for deletion?')) return;
  for (var i = 0; i < items.length; i++) {
    var frames = await (await fetch('/api/frames?vid_id='+encodeURIComponent(items[i].vid_id))).json();
    if (frames.length) {
      var ids = frames.map(function(f){ return f.id; });
      await fetch('/api/bulk_update', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ids: ids, update: {marked_delete: 1}})});
    }
  }
  clearClipSelection();
}

async function bulkUpdate(payload) {
  var ids = Object.keys(selected);
  await fetch('/api/bulk_update', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:ids, update:payload})});
  clearSelection();
}

function bulkReviewed() { bulkUpdate({reviewed:1}); }
function bulkDelete() { bulkUpdate({marked_delete:1}); }

function bulkSetSpecies() {
  var n = Object.keys(selected).length; if (!n) return;
  document.getElementById('bulk-count').textContent = n;
  document.getElementById('bulk-species-input').value = '';
  document.getElementById('bulk-ac-list').style.display = NONE;
  document.getElementById('bulk-species-tags').innerHTML = '';
  window._bulkSpecies = [];
  document.getElementById('bulk-modal').classList.add('show');
  setTimeout(function(){ document.getElementById('bulk-species-input').focus(); }, 100);
}

function closeBulkModal() { document.getElementById('bulk-modal').classList.remove('show'); }

function bulkAcSearch() {
  var val = document.getElementById('bulk-species-input').value.toLowerCase().trim();
  var list = document.getElementById('bulk-ac-list');
  if (!val) { list.style.display=NONE; return; }
  var matches = allSpecies.filter(function(s){ return s.toLowerCase().indexOf(val) >= 0; }).slice(0,10);
  if (!matches.length) { list.style.display=NONE; return; }
  list.innerHTML = matches.map(function(s){ return '<div class="ac-item" data-val="'+s+'" onclick="bulkAcSelect(this.dataset.val)">'+s+'</div>'; }).join('');
  list.style.display = 'block';
}

function bulkAcSelect(s) {
  document.getElementById('bulk-species-input').value = '';
  document.getElementById('bulk-ac-list').style.display = NONE;
  if (!window._bulkSpecies) window._bulkSpecies = [];
  if (!window._bulkSpecies.includes(s)) { window._bulkSpecies.push(s); renderBulkTags(); }
}

function bulkAcKey(e) {
  if (e.key==='Enter') { e.preventDefault(); var v=document.getElementById('bulk-species-input').value.trim(); if(v) bulkAcSelect(v); }
  if (e.key==='Escape') document.getElementById('bulk-ac-list').style.display=NONE;
}

function renderBulkTags() {
  document.getElementById('bulk-species-tags').innerHTML = (window._bulkSpecies||[]).map(function(s,i){
    return '<span class="tag" style="font-size:12px;padding:3px 8px;display:inline-flex;align-items:center;gap:4px">'+s+
      '<span style="cursor:pointer;color:#ef4444;font-size:14px;line-height:1" onclick="removeBulkTag('+i+')">&times;</span></span>';
  }).join('');
}

function removeBulkTag(i) { window._bulkSpecies.splice(i,1); renderBulkTags(); }

async function bulkSpeciesConfirm() {
  var species = window._bulkSpecies || [];
  var manual = document.getElementById('bulk-species-input').value.trim();
  if (manual && !species.includes(manual)) species.push(manual);
  if (!species.length) { alert('Please add at least one species.'); return; }
  closeBulkModal();
  if (window._bulkTarget === 'photos') {
    await photosBulkUpdate({species: species});
  } else {
    await bulkUpdate({species: species});
  }
  window._bulkTarget = null;
}

function bulkSetSiteDate() {
  var n = Object.keys(selected).length; if (!n) return;
  document.getElementById('bulk-site-count').textContent = n;
  ['bulk-country-input','bulk-region-input','bulk-area-input','bulk-site-input','bulk-date-input'].forEach(function(id){ document.getElementById(id).value=''; });
  ['bulk-country-ac-list','bulk-region-ac-list','bulk-area-ac-list','bulk-site-ac-list'].forEach(function(id){ document.getElementById(id).style.display=NONE; });
  document.getElementById('bulk-site-modal').classList.add('show');
  setTimeout(function(){ document.getElementById('bulk-country-input').focus(); }, 100);
}

function closeBulkSiteModal() { document.getElementById('bulk-site-modal').classList.remove('show'); }

async function bulkSiteDateConfirm() {
  var site = document.getElementById('bulk-site-input').value.trim();
  var date = document.getElementById('bulk-date-input').value.trim();
  var country = document.getElementById('bulk-country-input').value.trim();
  var region = document.getElementById('bulk-region-input').value.trim();
  var area = document.getElementById('bulk-area-input').value.trim();
  if (!site && !date && !country && !region && !area) { alert('Please enter at least one field.'); return; }
  closeBulkSiteModal();
  if (window._bulkTarget === 'photos') {
    var ids = Object.keys(selectedPhotos);
    await fetch('/api/bulk_photo_site_date', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({photo_ids:ids, dive_site:site, dive_date:date, country:country, region:region, area:area})});
    clearPhotoSelection();
    loadPhotoSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas();
  } else {
    var ids = Object.keys(selected);
    await fetch('/api/bulk_site_date', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({frame_ids:ids, dive_site:site, dive_date:date, country:country, region:region, area:area})});
    clearSelection();
    loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas();
  }
  window._bulkTarget = null;
}

function makeAc(inputId, listId, dataArr, selectFn) {
  var val = document.getElementById(inputId).value.toLowerCase().trim();
  var list = document.getElementById(listId);
  if (!val) { list.style.display=NONE; return; }
  var matches = dataArr.filter(function(s){ return s.toLowerCase().indexOf(val) >= 0; }).slice(0,8);
  if (!matches.length) { list.style.display=NONE; return; }
  list.innerHTML = matches.map(function(s){ return '<div class="ac-item" data-val="'+s+'" onclick="'+selectFn+'(this.dataset.val)">'+s+'</div>'; }).join('');
  list.style.display = 'block';
}

function siteAcSearch() { makeAc('m-site','site-ac-list',allSites,'siteSelect'); }
function siteSelect(s) { document.getElementById('m-site').value=s; document.getElementById('site-ac-list').style.display=NONE; }
function countryAcSearch() { makeAc('m-country','country-ac-list',allCountries,'countrySelect'); }
function countrySelect(s) { document.getElementById('m-country').value=s; document.getElementById('country-ac-list').style.display=NONE; }
function regionAcSearch() { makeAc('m-region','region-ac-list',allRegions,'regionSelect'); }
function regionSelect(s) { document.getElementById('m-region').value=s; document.getElementById('region-ac-list').style.display=NONE; }
function areaAcSearch() { makeAc('m-area','area-ac-list',allAreas,'areaSelect'); }
function areaSelect(s) { document.getElementById('m-area').value=s; document.getElementById('area-ac-list').style.display=NONE; }
function bulkSiteAcSearch() { makeAc('bulk-site-input','bulk-site-ac-list',allSites,'bulkSiteSelect'); }
function bulkSiteSelect(s) { document.getElementById('bulk-site-input').value=s; document.getElementById('bulk-site-ac-list').style.display=NONE; }
function bulkCountryAcSearch() { makeAc('bulk-country-input','bulk-country-ac-list',allCountries,'bulkCountrySelect'); }
function bulkCountrySelect(s) { document.getElementById('bulk-country-input').value=s; document.getElementById('bulk-country-ac-list').style.display=NONE; }
function bulkRegionAcSearch() { makeAc('bulk-region-input','bulk-region-ac-list',allRegions,'bulkRegionSelect'); }
function bulkRegionSelect(s) { document.getElementById('bulk-region-input').value=s; document.getElementById('bulk-region-ac-list').style.display=NONE; }
function bulkAreaAcSearch() { makeAc('bulk-area-input','bulk-area-ac-list',allAreas,'bulkAreaSelect'); }
function bulkAreaSelect(s) { document.getElementById('bulk-area-input').value=s; document.getElementById('bulk-area-ac-list').style.display=NONE; }

async function saveSiteDate() {
  if (!current) return;
  var site=document.getElementById('m-site').value.trim(), date=document.getElementById('m-date').value.trim();
  var country=document.getElementById('m-country').value.trim(), region=document.getElementById('m-region').value.trim();
  var area=document.getElementById('m-area').value.trim();
  await fetch('/api/save_site_date', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({frame_id:current.id, dive_site:site, dive_date:date, country:country, region:region, area:area})});
  current.dive_site=site; current.dive_date=date; current.country=country; current.region=region; current.area=area;
  document.getElementById('m-note').textContent='Saved.';
  setTimeout(function(){ document.getElementById('m-note').textContent=''; }, 1500);
  loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas();
}

async function loadAllSpecies() {
  var r = await fetch('/api/species');
  allSpecies = await r.json();
  var sel = document.getElementById('sp');
  sel.innerHTML = '<option>All species</option>';
  allSpecies.forEach(function(s){
    var o = document.createElement('option');
    o.value = s;
    o.textContent = s.length > 50 ? s.substring(0,50)+'...' : s;
    sel.appendChild(o);
  });
}

async function loadAllSites() {
  var r = await fetch('/api/dive_sites');
  allSites = await r.json();
  var sel = document.getElementById('site-filter');
  sel.innerHTML = '<option>All sites</option>';
  allSites.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}

async function loadAllCountries() {
  var r = await fetch('/api/countries');
  allCountries = await r.json();
  var sel = document.getElementById('country-filter');
  sel.innerHTML = '<option>All countries</option>';
  allCountries.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}

async function loadAllRegions() {
  var r = await fetch('/api/regions');
  allRegions = await r.json();
  var sel = document.getElementById('region-filter');
  sel.innerHTML = '<option>All regions</option>';
  allRegions.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}

async function loadAllAreas() {
  var r = await fetch('/api/areas');
  allAreas = await r.json();
  var sel = document.getElementById('area-filter');
  sel.innerHTML = '<option>All areas</option>';
  allAreas.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}

function acSearch() {
  var val = document.getElementById('new-species').value.toLowerCase().trim();
  var list = document.getElementById('ac-list');
  if (!val) { list.style.display=NONE; return; }
  var matches = allSpecies.filter(function(s){ return s.toLowerCase().indexOf(val) >= 0; }).slice(0,10);
  if (!matches.length) { list.style.display=NONE; return; }
  list.innerHTML = matches.map(function(s){ return '<div class="ac-item" data-val="'+s+'" onclick="acSelectItem(this.dataset.val)">'+s+'</div>'; }).join('');
  list.style.display = 'block';
}

function acSelectItem(s) { document.getElementById('new-species').value=s; document.getElementById('ac-list').style.display=NONE; addSpecies(); }
function acKey(e) { if(e.key==='Enter'){e.preventDefault();addSpecies();} if(e.key==='Escape')document.getElementById('ac-list').style.display=NONE; }
function siteAcKey(e) { if(e.key==='Escape')document.getElementById('site-ac-list').style.display=NONE; }

document.getElementById('grid').addEventListener('click', function(e) {
  if (e.target===document.getElementById('grid')) { selected={}; lastClickedFid=null; load(); }
});

document.addEventListener('click', function(e) {
  if (!e.target.closest('.ac-wrap') && !e.target.closest('.meta-field')) {
    ['ac-list','site-ac-list','country-ac-list','region-ac-list','area-ac-list',
     'bulk-ac-list','bulk-site-ac-list','bulk-country-ac-list','bulk-region-ac-list','bulk-area-ac-list',
     'pm-ac-list','pm-site-ac-list','pm-country-ac-list','pm-region-ac-list','pm-area-ac-list']
    .forEach(function(id){ var el=document.getElementById(id); if(el) el.style.display=NONE; });
  }
});

async function browseFolder() {
  var r = await fetch('/api/browse'); var d = await r.json();
  if (d.path) document.getElementById('scan-path').value = d.path;
}

async function startScan() {
  var path = document.getElementById('scan-path').value.trim();
  var workers = parseInt(document.getElementById('workers').value);
  var batch = document.getElementById('batch-mode').checked;
  var region = document.getElementById('scan-region').value;
  if (!path) { alert('Please enter a folder path.'); return; }
  document.getElementById('scan-btn').style.display=NONE;
  document.getElementById('stop-btn').style.display='';
  document.getElementById('prog-wrap').style.display='block';
  document.getElementById('prog-log').innerHTML='';
  document.getElementById('prog-label').textContent='Starting...';
  document.getElementById('prog-bar').style.width='0%';
  fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:path,workers:workers,batch:batch,region:region})});
  if (scanES) scanES.close();
  scanES = new EventSource('/api/scan_progress');
  scanES.onmessage = function(e) {
    var d = JSON.parse(e.data);
    if (d.type==='ping') return;
    if (d.type==='progress') {
      document.getElementById('prog-label').textContent=d.msg;
      document.getElementById('prog-bar').style.width=(d.pct||0)+'%';
      if (d.file) { var log=document.getElementById('prog-log'); log.innerHTML+='<div>'+d.file+' - '+d.frames+' frames</div>'; log.scrollTop=log.scrollHeight; }
    } else if (d.type==='done') {
      document.getElementById('prog-label').textContent=d.msg;
      document.getElementById('prog-bar').style.width='100%';
      document.getElementById('scan-btn').style.display='';
      document.getElementById('stop-btn').style.display=NONE;
      scanES.close(); loadFolderList(); loadAllSpecies(); loadAllSites(); loadStat(); load();
    } else if (d.type==='error') {
      document.getElementById('prog-label').textContent='Error: '+d.msg;
      document.getElementById('scan-btn').style.display='';
      document.getElementById('stop-btn').style.display=NONE;
      scanES.close();
    }
  };
}

function stopScan() {
  fetch('/api/scan_stop',{method:'POST'}); if (scanES) scanES.close();
  document.getElementById('scan-btn').style.display='';
  document.getElementById('stop-btn').style.display=NONE;
  document.getElementById('prog-label').textContent='Stopped.';
}

async function loadFolderList() {
  var r = await fetch('/api/folders'); var data = await r.json();
  var el = document.getElementById('folder-list');
  if (!data.length) { el.innerHTML='<div style="color:#334155;font-size:13px">No folders indexed yet.</div>'; return; }
  var html='';
  data.forEach(function(f,i){
    html+='<div class="fl-item"><div><div class="fl-path">'+f.folder+'</div>';
    html+='<div class="fl-meta">'+f.videos+' video(s)  '+f.frames+' frames</div></div>';
    html+='<div style="display:flex;gap:6px">';
    html+='<button class="btn-amber btn-sm" data-idx="'+i+'">Folder</button>';
    html+='<button class="btn-red btn-sm" data-idx="'+i+'">Remove</button>';
    html+='</div></div>';
  });
  el.innerHTML=html;
  el.querySelectorAll('.btn-amber').forEach(function(btn){
    btn.addEventListener('click', function(){ openExplorer(data[btn.dataset.idx].folder); });
  });
  el.querySelectorAll('.btn-red').forEach(function(btn){
    btn.addEventListener('click', function(){ removeVideoFolder(data[btn.dataset.idx].folder); });
  });
}

async function openExplorer(path) { await fetch('/api/folder?path='+encodeURIComponent(path)); }

function renderSpeciesTags(species) {
  document.getElementById('m-species').innerHTML = (species||[]).map(function(s,i){
    return '<span class="tag" style="font-size:12px;padding:3px 8px;margin:2px;display:inline-flex;align-items:center;gap:4px">'+
      '<span style="cursor:pointer" title="Click to edit" onclick="editSpecies('+i+')">'+s+'</span>'+
      '<span style="cursor:pointer;color:#ef4444;font-size:14px;line-height:1" onclick="removeSpecies('+i+')">&times;</span></span>';
  }).join('');
}

function showModalById(id) { showModal(window._frames[id]); }

function showModal(f) {
  current = f; current.species = current.species || [];
  document.getElementById('m-img').src='/thumb/'+f.id;
  document.getElementById('m-title').textContent=f.filename;
  document.getElementById('m-sub').textContent='Timestamp: '+fmt(f.timestamp);
  document.getElementById('m-visibility').value=f.visibility||'';
  document.getElementById('m-id-confidence').value=f.id_confidence||'';
  document.getElementById('lookup-status').textContent='';
  document.getElementById('m-country').value=f.country||'';
  document.getElementById('m-region').value=f.region||'';
  document.getElementById('m-area').value=f.area||'';
  document.getElementById('m-site').value=f.dive_site||'';
  document.getElementById('m-date').value=f.dive_date||'';
  ['country-ac-list','region-ac-list','area-ac-list','site-ac-list'].forEach(function(id){ document.getElementById(id).style.display=NONE; });
  renderSpeciesTags(current.species);
  document.getElementById('m-habitat').value=f.habitat||'';
  document.getElementById('m-behs').value=(f.behaviours||[]).join(', ');
  document.getElementById('m-notes').value=f.notes||'';
  document.getElementById('m-note').textContent='';
  document.getElementById('new-species').value='';
  document.getElementById('ac-list').style.display=NONE;
  var rb=document.getElementById('btn-reviewed');
  rb.textContent=f.reviewed?'Reviewed':'Mark Reviewed';
  rb.style.background=f.reviewed?'#14532d':'#0f2540';
  rb.style.color=f.reviewed?'#86efac':'#475569';
  document.getElementById('modal').classList.add('show');
}

function closeModal() { document.getElementById('modal').classList.remove('show'); }

async function saveFrameDetails() {
  if (!current) return;
  var habitat = document.getElementById('m-habitat').value.trim();
  var behs = document.getElementById('m-behs').value.trim();
  var notes = document.getElementById('m-notes').value.trim();
  var visibility = document.getElementById('m-visibility').value;
  var id_confidence = document.getElementById('m-id-confidence').value;
  var behaviours = behs ? behs.split(',').map(function(s){return s.trim();}).filter(Boolean) : [];
  await fetch('/api/update_frame', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:current.id, habitat:habitat, behaviours:behaviours, notes:notes, visibility:visibility, id_confidence:id_confidence})});
  current.habitat=habitat; current.behaviours=behaviours; current.notes=notes; current.visibility=visibility; current.id_confidence=id_confidence;
  document.getElementById('m-note').textContent='Saved.';
  setTimeout(function(){document.getElementById('m-note').textContent='';},1500);
  load();
}

async function idConfidenceChanged() {
  var val = document.getElementById('m-id-confidence').value;
  if (val !== 'confirmed') return;
  if (!current || !current.species || !current.species.length) {
    alert('Please add a species name first before looking up.');
    document.getElementById('m-id-confidence').value = current.id_confidence || '';
    return;
  }
  var status = document.getElementById('lookup-status');
  status.textContent = 'Looking up species info...';
  status.style.color = '#f59e0b';
  var r = await fetch('/api/lookup_species', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({frame_id: current.id, species: current.species, type: 'frame'})});
  var d = await r.json();
  if (d.ok) {
    document.getElementById('m-habitat').value = d.habitat || '';
    document.getElementById('m-behs').value = (d.behaviours||[]).join(', ');
    document.getElementById('m-notes').value = d.notes || '';
    current.habitat=d.habitat; current.behaviours=d.behaviours; current.notes=d.notes;
    status.textContent = 'Done! Review and save.';
    status.style.color = '#22c55e';
    setTimeout(function(){status.textContent='';},3000);
  } else {
    status.textContent = 'Lookup failed: '+(d.error||'unknown');
    status.style.color = '#ef4444';
    document.getElementById('m-id-confidence').value = current.id_confidence || '';
  }
}

function editSpecies(i) {
  var n=prompt('Edit species name:',current.species[i]);
  if (n&&n.trim()&&n.trim()!==current.species[i]) { current.species[i]=n.trim(); saveSpecies(); }
}

function removeSpecies(i) { current.species.splice(i,1); saveSpecies(); }

function addSpecies() {
  var val=document.getElementById('new-species').value.trim(); if (!val) return;
  if (!current.species.includes(val)) { current.species.push(val); saveSpecies(); }
  document.getElementById('new-species').value=''; document.getElementById('ac-list').style.display=NONE;
}

async function saveSpecies() {
  renderSpeciesTags(current.species);
  await fetch('/api/update_frame',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current.id,species:current.species})});
  document.getElementById('m-note').textContent='Saved.';
  setTimeout(function(){document.getElementById('m-note').textContent='';},1500);
  load(); loadAllSpecies();
}

async function toggleReviewed() {
  current.reviewed=current.reviewed?0:1;
  var rb=document.getElementById('btn-reviewed');
  rb.textContent=current.reviewed?'Reviewed':'Mark Reviewed';
  rb.style.background=current.reviewed?'#14532d':'#0f2540';
  rb.style.color=current.reviewed?'#86efac':'#475569';
  await fetch('/api/update_frame',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current.id,reviewed:current.reviewed})});
  document.getElementById('m-note').textContent=current.reviewed?'Marked as reviewed.':'Unmarked.';
  setTimeout(function(){document.getElementById('m-note').textContent='';},1500);
}

async function toggleMarkDelete() {
  current.marked_delete=current.marked_delete?0:1;
  var btn=document.getElementById('btn-delete');
  btn.textContent=current.marked_delete?'Unmark Delete':'Mark for Delete';
  btn.style.background=current.marked_delete?'#7f1d1d':'#0f2540';
  btn.style.color=current.marked_delete?'#f87171':'#475569';
  await fetch('/api/update_frame',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current.id,marked_delete:current.marked_delete})});
  document.getElementById('m-note').textContent=current.marked_delete?'Marked for deletion.':'Unmarked.';
  setTimeout(function(){document.getElementById('m-note').textContent='';},1500);
  load();
}

function renameFile() {
  if (!current) return;
  var ext=current.filename.split('.').pop();
  var suggested=(current.species||[]).length>0?(current.species||[]).join(', ').replace(/[^a-zA-Z0-9 .,() -]/g,'_')+'.'+ext:current.filename;
  var newName=prompt('Rename file (append extra info after species name):',suggested);
  if (!newName||newName.trim()===current.filename) return;
  fetch('/api/rename_file',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:current.id,path:current.path,new_name:newName.trim()})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){current.filename=d.new_filename;current.path=d.new_path;document.getElementById('m-title').textContent=d.new_filename;document.getElementById('m-note').textContent='Renamed successfully.';load();}
    else{document.getElementById('m-note').textContent='Rename failed: '+(d.error||'unknown');}
    setTimeout(function(){document.getElementById('m-note').textContent='';},2500);
  });
}

async function purgeMarked() {
  var r=await fetch('/api/marked_files'); var files=await r.json(); if(!files.length) return;
  var list=files.slice(0,20).map(function(f){return f.filename;}).join(', ');
  var extra=files.length>20?' ...and '+(files.length-20)+' more':'';
  if(!confirm('Permanently delete '+files.length+' file(s) from disk? '+list+extra+' This cannot be undone!')) return;
  var r2=await fetch('/api/purge_marked',{method:'POST'}); var d=await r2.json();
  alert('Deleted '+d.deleted+' file(s). '+d.failed+' failed.'); load();
}

async function openFile() {
  if(!current) return;
  var r=await fetch('/api/open?path='+encodeURIComponent(current.path)+'&ts='+current.timestamp);
  var d=await r.json();
  document.getElementById('m-note').textContent=d.opened==='vlc'?'Opened in VLC at '+fmt(current.timestamp):d.opened==='default'?'Opened with default player.':'File not found.';
}

async function openFolder() {
  if(!current) return;
  await fetch('/api/folder?path='+encodeURIComponent(current.path));
  document.getElementById('m-note').textContent='Opened in Explorer.';
}

function copyPath() {
  if(!current) return;
  navigator.clipboard.writeText(current.path+' Timestamp: '+fmt(current.timestamp)+' ('+Math.round(current.timestamp)+'s)');
  document.getElementById('m-note').textContent='Copied!';
}

async function frPreview() {
  var q=document.getElementById('fr-search').value.trim();
  var box=document.getElementById('fr-preview');
  if(!q){box.style.display=NONE;return;}
  var r=await fetch('/api/species_search?q='+encodeURIComponent(q)); var data=await r.json();
  if(!data.length){box.innerHTML='<div style="color:#475569">No matches found.</div>';box.style.display='block';return;}
  var total=data.reduce(function(a,b){return a+b.count;},0);
  box.innerHTML='<div style="color:#7dd4fc;margin-bottom:6px">'+data.length+' variation(s) - '+total+' frames total:</div>'+
    data.map(function(d){return '<div class="preview-item">'+d.species+' <span style="color:#334155">('+d.count+' frames)</span></div>';}).join('');
  box.style.display='block';
}

async function frExecute() {
  var q=document.getElementById('fr-search').value.trim(), rep=document.getElementById('fr-replace').value.trim();
  if(!q||!rep){alert('Please fill in both fields.');return;}
  if(!confirm('Replace all species containing "'+q+'" with "'+rep+'"?')) return;
  var r=await fetch('/api/species_replace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({search:q,replace:rep})});
  var d=await r.json(); alert('Done! '+d.frames+' frames updated.');
  document.getElementById('fr-preview').style.display=NONE;
  document.getElementById('fr-search').value=''; document.getElementById('fr-replace').value='';
  loadAllSpecies(); load();
}

async function fixDashes() {
  var r=await fetch('/api/fix_dashes',{method:'POST'}); var d=await r.json();
  document.getElementById('dash-result').textContent='Fixed '+d.frames+' frames.';
  loadAllSpecies(); load();
}

async function loadHomeStat() {
  var rv = await fetch('/api/stat'); var dv = await rv.json();
  var rp = await fetch('/api/photo_stat'); var dp = await rp.json();
  document.getElementById('home-stat').textContent =
    dv.videos + ' videos  \u00b7  ' + dv.frames + ' frames  \u00b7  ' + dp.photos + ' photos indexed';
}

loadAllSpecies(); loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas(); load(); loadStat(); loadHomeStat(); loadSettings();

// ── Settings ──────────────────────────────────────────────────────────────────

var _settingsData = {};

async function loadSettings() {
  var r = await fetch('/api/settings'); var d = await r.json();
  _settingsData = d;
  // Populate region dropdowns
  var regions = d.regions || [];
  var defaultRegion = d.default_region || regions[0];
  ['scan-region','photo-scan-region','settings-region'].forEach(function(id) {
    var sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '';
    regions.forEach(function(rg) {
      var o = document.createElement('option');
      o.value = rg; o.textContent = rg;
      if (rg === defaultRegion) o.selected = true;
      sel.appendChild(o);
    });
  });
}

async function openSettings() {
  var r = await fetch('/api/settings'); var d = await r.json();
  _settingsData = d;
  // Populate region dropdown fresh
  var regions = d.regions || [];
  var sel = document.getElementById('settings-region');
  sel.innerHTML = '';
  regions.forEach(function(rg) {
    var o = document.createElement('option');
    o.value = rg; o.textContent = rg;
    if (rg === d.default_region) o.selected = true;
    sel.appendChild(o);
  });
  // Show key masked — just indicate if one is saved
  var keyField = document.getElementById('settings-api-key');
  keyField.value = d.api_key || '';
  keyField.type = 'password';
  document.getElementById('api-key-toggle').textContent = 'Show';
  document.getElementById('settings-note').textContent = '';
  document.getElementById('settings-modal').classList.add('show');
}

function closeSettings() {
  document.getElementById('settings-modal').classList.remove('show');
}

function toggleApiKeyVisibility() {
  var inp = document.getElementById('settings-api-key');
  var btn = document.getElementById('api-key-toggle');
  if (inp.type === 'password') { inp.type = 'text'; btn.textContent = 'Hide'; }
  else { inp.type = 'password'; btn.textContent = 'Show'; }
}

async function saveSettings() {
  var key = document.getElementById('settings-api-key').value.trim();
  var region = document.getElementById('settings-region').value;
  var r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({api_key: key, default_region: region})});
  var d = await r.json();
  if (d.ok) {
    _settingsData.api_key = key;
    _settingsData.default_region = region;
    // Update scan region dropdowns to new default
    ['scan-region','photo-scan-region'].forEach(function(id) {
      var sel = document.getElementById(id);
      if (sel) sel.value = region;
    });
    document.getElementById('settings-note').textContent = 'Saved!';
    setTimeout(function(){ closeSettings(); }, 800);
  } else {
    document.getElementById('settings-note').textContent = 'Save failed.';
  }
}

// ─── PHOTOS ───────────────────────────────────────────────────────────────────

function setPhotoView(v) {
  currentPhotoView = v;
  var allBtn = document.getElementById('photo-view-all');
  var spBtn = document.getElementById('photo-view-species');
  var allPanel = document.getElementById('photo-view-all-panel');
  var spPanel = document.getElementById('photo-view-species-panel');
  if (v === 'all') {
    allBtn.className = 'btn-blue btn-sm'; allBtn.style.background = ''; allBtn.style.color = '';
    spBtn.className = 'btn-sm'; spBtn.style.background = 'transparent'; spBtn.style.color = '#475569';
    allPanel.style.display = ''; spPanel.style.display = 'none';
    loadPhotos();
  } else {
    spBtn.className = 'btn-blue btn-sm'; spBtn.style.background = ''; spBtn.style.color = '';
    allBtn.className = 'btn-sm'; allBtn.style.background = 'transparent'; allBtn.style.color = '#475569';
    allPanel.style.display = 'none'; spPanel.style.display = '';
    loadPhotosBySpecies();
  }
}

async function loadPhotosBySpecies() {
  var q = (document.getElementById('photo-sp-q').value || '').toLowerCase().trim();
  var r = await fetch('/api/photo_species_grouped');
  var data = await r.json();
  if (q) data = data.filter(function(d){ return d.species.toLowerCase().indexOf(q) >= 0; });
  document.getElementById('photo-sp-rcount').textContent = data.length + ' species';
  var html = '';
  data.forEach(function(d, i) {
    html += '<div class="card" data-sp-idx="'+i+'">';
    html += '<div style="position:relative">';
    html += '<img src="/thumb/'+d.thumb_id+'" onerror="this.style.display=String.fromCharCode(110,111,110,101)" style="width:100%;height:140px;object-fit:cover;display:block">';
    html += '<span style="position:absolute;bottom:4px;right:6px;background:rgba(0,0,0,.75);color:#7dd4fc;font-size:10px;padding:2px 6px;border-radius:4px">'+d.count+' photo'+(d.count!==1?'s':'')+'</span>';
    html += '</div>';
    html += '<div class="ci"><div class="fname" style="font-size:12px;color:#7dd4fc;font-weight:500">'+d.species+'</div></div>';
    html += '</div>';
  });
  document.getElementById('photo-species-grid').innerHTML = html || '<div style="color:#334155;padding:20px">No species found.</div>';
  // Attach click handlers via event listeners to avoid quote-escaping issues
  document.getElementById('photo-species-grid').querySelectorAll('.card').forEach(function(el) {
    var idx = parseInt(el.dataset.spIdx);
    el.style.cursor = 'pointer';
    el.addEventListener('click', function() { showPhotosBySpecies(data[idx].species); });
  });
}

async function showPhotosBySpecies(species) {
  // Use the normalised name (parens stripped) as a partial search
  var searchTerm = species.split(' - ')[0].trim() || species;
  var r = await fetch('/api/photos?species='+encodeURIComponent(searchTerm)+'&page=1&page_size=200');
  var resp = await r.json();
  var photos = resp.items || [];
  document.getElementById('photo-species-title').textContent = species + ' — ' + photos.length + ' photo' + (photos.length!==1?'s':'');
  var html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px">';
  photos.forEach(function(p) {
    window._photos = window._photos || {};
    window._photos[p.id] = p;
    var tags = (p.species||[]).filter(function(s){return s!==species;}).slice(0,2).map(function(s){ return '<span class="tag">'+s+'</span>'; }).join('');
    html += '<div class="card" data-pid="'+p.id+'" style="cursor:pointer">';
    html += '<img src="/thumb/'+p.id+'" style="width:100%;height:110px;object-fit:cover;display:block" onerror="this.style.display=String.fromCharCode(110,111,110,101)">';
    html += '<div class="ci"><div class="fname">'+p.filename+'</div><div class="tags">'+tags+'</div></div>';
    html += '</div>';
  });
  html += '</div>';
  document.getElementById('photo-species-inner').innerHTML = html;
  document.getElementById('photo-species-modal').classList.add('show');
  document.getElementById('photo-species-mbox').scrollTop = 0;
  document.getElementById('photo-species-inner').querySelectorAll('.card').forEach(function(el) {
    el.addEventListener('click', function() {
      document.getElementById('photo-species-modal').classList.remove('show');
      showPhotoModal(window._photos[el.dataset.pid]);
    });
  });
}

async function loadPhotos() {
  var q = document.getElementById('photo-q').value;
  var sp = document.getElementById('photo-sp').value;
  var country = document.getElementById('photo-country-filter').value;
  var region = document.getElementById('photo-region-filter').value;
  var area = document.getElementById('photo-area-filter').value;
  var site = document.getElementById('photo-site-filter').value;
  var sort = document.getElementById('photo-sort-filter').value;
  var p = new URLSearchParams();
  if (q) p.set('q', q);
  if (sp && sp !== 'All species') p.set('species', sp);
  if (country && country !== 'All countries') p.set('country', country);
  if (region && region !== 'All regions') p.set('region', region);
  if (area && area !== 'All areas') p.set('area', area);
  if (site && site !== 'All sites') p.set('site', site);
  if (sort) p.set('sort', sort);
  p.set('page', photoPage); p.set('page_size', PAGE_SIZE);
  var r = await fetch('/api/photos?' + p);
  var resp = await r.json();
  var data = resp.items; var total = resp.total;
  var totalPages = Math.ceil(total / PAGE_SIZE);
  document.getElementById('photo-rcount').textContent = total + ' photos';
  renderPhotoPagination(totalPages, total);
  var html = '';
  window._photos = {};
  data.forEach(function(p) {
    window._photos[p.id] = p;
    var cls = 'card' + (p.marked_delete ? ' marked-delete' : '') + (selectedPhotos[p.id] ? ' selected' : '');
    var del = p.marked_delete ? '[DEL] ' : '';
    var tags = (p.species||[]).slice(0,3).map(function(s){ return '<span class="tag">'+s+'</span>'; }).join('');
    html += '<div class="'+cls+'" data-pid="'+p.id+'">';
    html += '<img src="/thumb/'+p.id+'" onerror="this.style.display=String.fromCharCode(110,111,110,101)">';
    html += '<div class="ci"><div class="fname">'+del+p.filename+'</div>';
    html += '<div class="tags">'+tags+'</div>';
    html += '<div class="bot"><span class="hab">'+(p.habitat||'')+'</span><span class="vis-'+(p.visibility||'')+'">'+(p.visibility||'')+'</span></div>';
    html += '</div></div>';
  });
  document.getElementById('photo-grid').innerHTML = html;
  document.getElementById('photo-grid').querySelectorAll('.card').forEach(function(el) {
    el.addEventListener('click', function(e) { photoCardClick(e, el.dataset.pid); });
  });
  var marked = data.filter(function(p){ return p.marked_delete; }).length;
  document.getElementById('photo-marked-count').textContent = marked;
  document.getElementById('btn-purge-photos').style.display = marked > 0 ? '' : NONE;
}

function renderPhotoPagination(totalPages, total) {
  var bar = document.getElementById('photo-pagination');
  if (totalPages <= 1) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  var start = (photoPage-1)*PAGE_SIZE+1, end = Math.min(photoPage*PAGE_SIZE, total);
  document.getElementById('photo-pg-info').textContent = 'Page '+photoPage+' of '+totalPages+' ('+start+'-'+end+' of '+total+' photos)';
  document.getElementById('photo-pg-prev').disabled = photoPage <= 1;
  document.getElementById('photo-pg-next').disabled = photoPage >= totalPages;
}

function changePhotoPage(dir) { photoPage += dir; loadPhotos(); window.scrollTo(0,0); }
function jumpToPhotoPage() {
  var val = parseInt(document.getElementById('photo-pg-jump').value);
  if (!val || val < 1) return;
  photoPage = val; document.getElementById('photo-pg-jump').value = ''; loadPhotos(); window.scrollTo(0,0);
}

async function loadPhotoStat() {
  var r = await fetch('/api/photo_stat'); var d = await r.json();
  document.getElementById('photo-stat').textContent = d.photos + ' photos indexed';
}

async function loadStat() {
  var r = await fetch('/api/stat'); var d = await r.json();
  document.getElementById('stat').textContent = d.videos + ' videos  ' + d.frames + ' frames indexed';
}

function showPhotoModal(p) {
  currentPhoto = p; currentPhoto.species = currentPhoto.species || [];
  document.getElementById('pm-img').src = '/thumb/'+p.id;
  document.getElementById('pm-title').textContent = p.filename;
  var kb = Math.round((p.filesize||0)/1024);
  document.getElementById('pm-sub').textContent = (kb > 1024 ? (kb/1024).toFixed(1)+' MB' : kb+' KB') + '  Visibility: '+(p.visibility||'');
  document.getElementById('pm-country').value = p.country||'';
  document.getElementById('pm-region').value = p.region||'';
  document.getElementById('pm-area').value = p.area||'';
  document.getElementById('pm-site').value = p.dive_site||'';
  document.getElementById('pm-date').value = p.dive_date||'';
  ['pm-country-ac-list','pm-region-ac-list','pm-area-ac-list','pm-site-ac-list'].forEach(function(id){ document.getElementById(id).style.display=NONE; });
  renderPhotoSpeciesTags(currentPhoto.species);
  document.getElementById('pm-habitat').value = p.habitat||'';
  var behs = p.behaviours||[];
  document.getElementById('pm-behs').value = behs.join(', ');
  document.getElementById('pm-notes').value = p.notes||'';
  document.getElementById('pm-visibility').value = p.visibility||'';
  document.getElementById('pm-id-confidence').value = p.id_confidence||'';
  document.getElementById('pm-lookup-status').textContent = '';
  document.getElementById('pm-note').textContent = '';
  document.getElementById('pm-new-species').value = '';
  document.getElementById('pm-ac-list').style.display = NONE;
  var rb = document.getElementById('pm-btn-reviewed');
  rb.textContent = p.reviewed ? 'Reviewed' : 'Mark Reviewed';
  rb.style.background = p.reviewed ? '#14532d' : '#0f2540';
  rb.style.color = p.reviewed ? '#86efac' : '#475569';
  var db = document.getElementById('pm-btn-delete');
  db.textContent = p.marked_delete ? 'Unmark Delete' : 'Mark for Delete';
  db.style.background = p.marked_delete ? '#7f1d1d' : '#0f2540';
  db.style.color = p.marked_delete ? '#f87171' : '#475569';
  document.getElementById('photo-modal').classList.add('show');
}

function closePhotoModal() { document.getElementById('photo-modal').classList.remove('show'); }

async function savePhotoDetails() {
  if (!currentPhoto) return;
  var habitat = document.getElementById('pm-habitat').value.trim();
  var behs = document.getElementById('pm-behs').value.trim();
  var notes = document.getElementById('pm-notes').value.trim();
  var visibility = document.getElementById('pm-visibility').value;
  var id_confidence = document.getElementById('pm-id-confidence').value;
  var behaviours = behs ? behs.split(',').map(function(s){return s.trim();}).filter(Boolean) : [];
  await fetch('/api/update_photo', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({id:currentPhoto.id, habitat:habitat, behaviours:behaviours, notes:notes, visibility:visibility, id_confidence:id_confidence})});
  currentPhoto.habitat=habitat; currentPhoto.behaviours=behaviours; currentPhoto.notes=notes; currentPhoto.visibility=visibility; currentPhoto.id_confidence=id_confidence;
  document.getElementById('pm-note').textContent='Saved.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
  loadPhotos();
}

async function pmIdConfidenceChanged() {
  var val = document.getElementById('pm-id-confidence').value;
  if (val !== 'confirmed') return;
  if (!currentPhoto || !currentPhoto.species || !currentPhoto.species.length) {
    alert('Please add a species name first before looking up.');
    document.getElementById('pm-id-confidence').value = currentPhoto.id_confidence || '';
    return;
  }
  var status = document.getElementById('pm-lookup-status');
  status.textContent = 'Looking up species info...';
  status.style.color = '#f59e0b';
  var r = await fetch('/api/lookup_species', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({frame_id: currentPhoto.id, species: currentPhoto.species, type: 'photo'})});
  var d = await r.json();
  if (d.ok) {
    document.getElementById('pm-habitat').value = d.habitat || '';
    document.getElementById('pm-behs').value = (d.behaviours||[]).join(', ');
    document.getElementById('pm-notes').value = d.notes || '';
    currentPhoto.habitat=d.habitat; currentPhoto.behaviours=d.behaviours; currentPhoto.notes=d.notes;
    status.textContent = 'Done! Review and save.';
    status.style.color = '#22c55e';
    setTimeout(function(){status.textContent='';},3000);
  } else {
    status.textContent = 'Lookup failed: '+(d.error||'unknown');
    status.style.color = '#ef4444';
    document.getElementById('pm-id-confidence').value = currentPhoto.id_confidence || '';
  }
}

function renderPhotoSpeciesTags(species) {
  document.getElementById('pm-species').innerHTML = (species||[]).map(function(s,i){
    return '<span class="tag" style="font-size:12px;padding:3px 8px;margin:2px;display:inline-flex;align-items:center;gap:4px">'+
      '<span style="cursor:pointer" title="Click to edit" onclick="editPhotoSpecies('+i+')">'+s+'</span>'+
      '<span style="cursor:pointer;color:#ef4444;font-size:14px;line-height:1" onclick="removePhotoSpecies('+i+')">&times;</span></span>';
  }).join('');
}

function pmAcSearch() {
  var val = document.getElementById('pm-new-species').value.toLowerCase().trim();
  var list = document.getElementById('pm-ac-list');
  if (!val) { list.style.display=NONE; return; }
  var matches = allSpecies.filter(function(s){ return s.toLowerCase().indexOf(val) >= 0; }).slice(0,10);
  if (!matches.length) { list.style.display=NONE; return; }
  list.innerHTML = matches.map(function(s){ return '<div class="ac-item" data-val="'+s+'" onclick="pmAcSelectItem(this.dataset.val)">'+s+'</div>'; }).join('');
  list.style.display = 'block';
}
function pmAcSelectItem(s) { document.getElementById('pm-new-species').value=s; document.getElementById('pm-ac-list').style.display=NONE; addPhotoSpecies(); }
function pmAcKey(e) { if(e.key==='Enter'){e.preventDefault();addPhotoSpecies();} if(e.key==='Escape')document.getElementById('pm-ac-list').style.display=NONE; }

function editPhotoSpecies(i) {
  var n = prompt('Edit species name:', currentPhoto.species[i]);
  if (n && n.trim() && n.trim() !== currentPhoto.species[i]) { currentPhoto.species[i]=n.trim(); savePhotoSpecies(); }
}
function removePhotoSpecies(i) { currentPhoto.species.splice(i,1); savePhotoSpecies(); }
function addPhotoSpecies() {
  var val = document.getElementById('pm-new-species').value.trim(); if (!val) return;
  if (!currentPhoto.species.includes(val)) { currentPhoto.species.push(val); savePhotoSpecies(); }
  document.getElementById('pm-new-species').value=''; document.getElementById('pm-ac-list').style.display=NONE;
}
async function savePhotoSpecies() {
  renderPhotoSpeciesTags(currentPhoto.species);
  await fetch('/api/update_photo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:currentPhoto.id,species:currentPhoto.species})});
  document.getElementById('pm-note').textContent='Saved.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
  loadPhotos(); loadPhotoSpecies();
}

function pmSiteAcSearch() { makeAc('pm-site','pm-site-ac-list',allSites,'pmSiteSelect'); }
function pmSiteSelect(s) { document.getElementById('pm-site').value=s; document.getElementById('pm-site-ac-list').style.display=NONE; }
function pmCountryAcSearch() { makeAc('pm-country','pm-country-ac-list',allCountries,'pmCountrySelect'); }
function pmCountrySelect(s) { document.getElementById('pm-country').value=s; document.getElementById('pm-country-ac-list').style.display=NONE; }
function pmRegionAcSearch() { makeAc('pm-region','pm-region-ac-list',allRegions,'pmRegionSelect'); }
function pmRegionSelect(s) { document.getElementById('pm-region').value=s; document.getElementById('pm-region-ac-list').style.display=NONE; }
function pmAreaAcSearch() { makeAc('pm-area','pm-area-ac-list',allAreas,'pmAreaSelect'); }
function pmAreaSelect(s) { document.getElementById('pm-area').value=s; document.getElementById('pm-area-ac-list').style.display=NONE; }

async function savePhotoSiteDate() {
  if (!currentPhoto) return;
  var site=document.getElementById('pm-site').value.trim(), date=document.getElementById('pm-date').value.trim();
  var country=document.getElementById('pm-country').value.trim(), region=document.getElementById('pm-region').value.trim();
  var area=document.getElementById('pm-area').value.trim();
  await fetch('/api/save_photo_site_date',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({photo_id:currentPhoto.id,dive_site:site,dive_date:date,country:country,region:region,area:area})});
  currentPhoto.dive_site=site; currentPhoto.dive_date=date; currentPhoto.country=country; currentPhoto.region=region; currentPhoto.area=area;
  document.getElementById('pm-note').textContent='Saved.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
  loadPhotoSites(); loadAllSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas();
}

async function togglePhotoReviewed() {
  currentPhoto.reviewed = currentPhoto.reviewed ? 0 : 1;
  var rb = document.getElementById('pm-btn-reviewed');
  rb.textContent = currentPhoto.reviewed ? 'Reviewed' : 'Mark Reviewed';
  rb.style.background = currentPhoto.reviewed ? '#14532d' : '#0f2540';
  rb.style.color = currentPhoto.reviewed ? '#86efac' : '#475569';
  await fetch('/api/update_photo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:currentPhoto.id,reviewed:currentPhoto.reviewed})});
  document.getElementById('pm-note').textContent = currentPhoto.reviewed ? 'Marked as reviewed.' : 'Unmarked.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
}

async function togglePhotoDelete() {
  currentPhoto.marked_delete = currentPhoto.marked_delete ? 0 : 1;
  var db = document.getElementById('pm-btn-delete');
  db.textContent = currentPhoto.marked_delete ? 'Unmark Delete' : 'Mark for Delete';
  db.style.background = currentPhoto.marked_delete ? '#7f1d1d' : '#0f2540';
  db.style.color = currentPhoto.marked_delete ? '#f87171' : '#475569';
  await fetch('/api/update_photo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:currentPhoto.id,marked_delete:currentPhoto.marked_delete})});
  document.getElementById('pm-note').textContent = currentPhoto.marked_delete ? 'Marked for deletion.' : 'Unmarked.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
  loadPhotos();
}

async function openPhoto() {
  if (!currentPhoto) return;
  var r = await fetch('/api/open_photo?path='+encodeURIComponent(currentPhoto.path));
  var d = await r.json();
  document.getElementById('pm-note').textContent = d.ok ? 'Opened.' : 'Could not open file.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
}

async function openPhotoFolder() {
  if (!currentPhoto) return;
  await fetch('/api/folder?path='+encodeURIComponent(currentPhoto.path));
  document.getElementById('pm-note').textContent = 'Opened in Explorer.';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
}

function copyPhotoPath() {
  if (!currentPhoto) return;
  navigator.clipboard.writeText(currentPhoto.path);
  document.getElementById('pm-note').textContent = 'Copied!';
  setTimeout(function(){document.getElementById('pm-note').textContent='';},1500);
}

function renamePhoto() {
  if (!currentPhoto) return;
  var ext = currentPhoto.filename.split('.').pop();
  var suggested = (currentPhoto.species||[]).length > 0
    ? (currentPhoto.species||[]).join(', ').replace(/[^a-zA-Z0-9 .,() -]/g,'_')+'.'+ext
    : currentPhoto.filename;
  var newName = prompt('Rename file:', suggested);
  if (!newName || newName.trim() === currentPhoto.filename) return;
  fetch('/api/rename_photo',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:currentPhoto.id,path:currentPhoto.path,new_name:newName.trim()})})
  .then(function(r){return r.json();})
  .then(function(d){
    if(d.ok){currentPhoto.filename=d.new_filename;currentPhoto.path=d.new_path;document.getElementById('pm-title').textContent=d.new_filename;document.getElementById('pm-note').textContent='Renamed.';loadPhotos();}
    else{document.getElementById('pm-note').textContent='Rename failed: '+(d.error||'unknown');}
    setTimeout(function(){document.getElementById('pm-note').textContent='';},2500);
  });
}

async function purgeMarkedPhotos() {
  var r = await fetch('/api/marked_photos'); var files = await r.json(); if (!files.length) return;
  var list = files.slice(0,20).map(function(f){return f.filename;}).join(', ');
  var extra = files.length > 20 ? ' ...and '+(files.length-20)+' more' : '';
  if (!confirm('Permanently delete '+files.length+' photo(s) from disk? '+list+extra+' This cannot be undone!')) return;
  var r2 = await fetch('/api/purge_marked_photos',{method:'POST'}); var d = await r2.json();
  alert('Deleted '+d.deleted+' photo(s). '+d.failed+' failed.'); loadPhotos();
}

async function loadPhotoSpecies() {
  var r = await fetch('/api/photo_species');
  var sps = await r.json();
  var sel = document.getElementById('photo-sp');
  sel.innerHTML = '<option>All species</option>';
  sps.forEach(function(s){ var o=document.createElement('option'); o.value=s; o.textContent=s.length>50?s.substring(0,50)+'...':s; sel.appendChild(o); });
}
async function loadPhotoSites() {
  var r = await fetch('/api/photo_sites');
  var data = await r.json();
  var sel = document.getElementById('photo-site-filter');
  sel.innerHTML = '<option>All sites</option>';
  data.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}
async function loadPhotoCountries() {
  var r = await fetch('/api/photo_countries');
  var data = await r.json();
  var sel = document.getElementById('photo-country-filter');
  sel.innerHTML = '<option>All countries</option>';
  data.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}
async function loadPhotoRegions() {
  var r = await fetch('/api/photo_regions');
  var data = await r.json();
  var sel = document.getElementById('photo-region-filter');
  sel.innerHTML = '<option>All regions</option>';
  data.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}
async function loadPhotoAreas() {
  var r = await fetch('/api/photo_areas');
  var data = await r.json();
  var sel = document.getElementById('photo-area-filter');
  sel.innerHTML = '<option>All areas</option>';
  data.forEach(function(s){ var o=document.createElement('option'); o.textContent=s; sel.appendChild(o); });
}

async function browsePhotoFolder() {
  var r = await fetch('/api/browse'); var d = await r.json();
  if (d.path) document.getElementById('photo-scan-path').value = d.path;
}

async function startPhotoScan() {
  var path = document.getElementById('photo-scan-path').value.trim();
  var workers = parseInt(document.getElementById('photo-workers').value);
  var batch = document.getElementById('photo-batch-mode').checked;
  var region = document.getElementById('photo-scan-region').value;
  if (!path) { alert('Please enter a folder path.'); return; }
  document.getElementById('photo-scan-btn').style.display = NONE;
  document.getElementById('photo-stop-btn').style.display = batch ? NONE : '';
  document.getElementById('photo-prog-wrap').style.display = 'block';
  document.getElementById('photo-prog-log').innerHTML = '';
  document.getElementById('photo-prog-label').textContent = 'Starting...';
  document.getElementById('photo-prog-bar').style.width = '0%';
  fetch('/api/scan_photos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:path,workers:workers,batch:batch,region:region})});
  if (photoScanES) photoScanES.close();
  photoScanES = new EventSource('/api/scan_photos_progress');
  photoScanES.onmessage = function(e) {
    var d = JSON.parse(e.data);
    if (d.type==='ping') return;
    if (d.type==='progress') {
      document.getElementById('photo-prog-label').textContent = d.msg;
      document.getElementById('photo-prog-bar').style.width = (d.pct||0)+'%';
      if (d.file) { var log=document.getElementById('photo-prog-log'); log.innerHTML+='<div>'+d.file+'</div>'; log.scrollTop=log.scrollHeight; }
    } else if (d.type==='done') {
      document.getElementById('photo-prog-label').textContent = d.msg;
      document.getElementById('photo-prog-bar').style.width = '100%';
      document.getElementById('photo-scan-btn').style.display = '';
      document.getElementById('photo-stop-btn').style.display = NONE;
      photoScanES.close(); loadPhotoFolderList(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoStat(); loadPhotos();
    } else if (d.type==='error') {
      document.getElementById('photo-prog-label').textContent = 'Error: '+d.msg;
      document.getElementById('photo-scan-btn').style.display = '';
      document.getElementById('photo-stop-btn').style.display = NONE;
      photoScanES.close();
    }
  };
}

function stopPhotoScan() {
  fetch('/api/scan_photos_stop',{method:'POST'}); if (photoScanES) photoScanES.close();
  document.getElementById('photo-scan-btn').style.display = '';
  document.getElementById('photo-stop-btn').style.display = NONE;
  document.getElementById('photo-prog-label').textContent = 'Stopped.';
}

// ── Photo multi-select ────────────────────────────────────────────────────────

function photoCardClick(e, pid) {
  if (e.ctrlKey || e.metaKey) {
    if (selectedPhotos[pid]) { delete selectedPhotos[pid]; } else { selectedPhotos[pid] = true; }
    var el = document.querySelector('[data-pid="'+pid+'"]');
    if (el) el.classList.toggle('selected', !!selectedPhotos[pid]);
    lastClickedPid = pid; updatePhotoSelBar();
  } else if (e.shiftKey) {
    if (!lastClickedPid) {
      selectedPhotos[pid] = true;
      var el = document.querySelector('[data-pid="'+pid+'"]');
      if (el) el.classList.add('selected');
      lastClickedPid = pid; updatePhotoSelBar();
    } else {
      var cards = Array.from(document.getElementById('photo-grid').querySelectorAll('.card'));
      var ids = cards.map(function(c){ return c.dataset.pid; });
      var a = ids.indexOf(lastClickedPid), b = ids.indexOf(pid);
      var start = Math.min(a,b), end = Math.max(a,b);
      for (var i=start; i<=end; i++) { selectedPhotos[ids[i]] = true; cards[i].classList.add('selected'); }
      updatePhotoSelBar();
    }
  } else {
    showPhotoModal(window._photos[pid]); lastClickedPid = pid;
  }
}

function updatePhotoSelBar() {
  var n = Object.keys(selectedPhotos).length;
  document.getElementById('photo-sel-bar').style.display = n > 0 ? 'flex' : NONE;
  document.getElementById('photo-sel-count').textContent = n + ' selected';
}

function clearPhotoSelection() { selectedPhotos = {}; lastClickedPid = null; loadPhotos(); }

async function photosBulkUpdate(payload) {
  var ids = Object.keys(selectedPhotos);
  await fetch('/api/bulk_update_photos', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:ids, update:payload})});
  clearPhotoSelection();
}

function photosBulkReviewed() { photosBulkUpdate({reviewed:1}); }
function photosBulkMarkDelete() { photosBulkUpdate({marked_delete:1}); }

// ── Batch Rename ──────────────────────────────────────────────────────────────

var _batchRenameItems = [];
var _batchRenameType = '';

function openBatchRename(type, items) {
  _batchRenameType = type;
  if (type === 'photos') {
    var ids = Object.keys(selectedPhotos);
    _batchRenameItems = ids.map(function(id){ return window._photos[id]; }).filter(Boolean);
  } else {
    // clips — items passed directly
    _batchRenameItems = items || [];
  }
  if (!_batchRenameItems.length) { alert('No items selected.'); return; }
  document.getElementById('batch-rename-prefix').value = '';
  document.getElementById('batch-rename-progress').style.display = 'none';
  updateBatchRenamePreview();
  document.getElementById('batch-rename-modal').classList.add('show');
}

function closeBatchRename() {
  document.getElementById('batch-rename-modal').classList.remove('show');
}

function updateBatchRenamePreview() {
  var prefix = document.getElementById('batch-rename-prefix').value.trim();
  var preview = document.getElementById('batch-rename-preview');
  var lines = _batchRenameItems.slice(0, 5).map(function(item, i) {
    var base = prefix || (item.species && item.species[0]) || item.filename;
    var ext = item.filename.split('.').pop();
    var num = String(i + 1).padStart(3, '0');
    var newName = base.replace(/[^a-zA-Z0-9 .,() -]/g, '_') + '_' + num + '.' + ext;
    return '<div class="preview-item"><span style="color:#475569">' + item.filename + '</span> → <span style="color:#7dd4fc">' + newName + '</span></div>';
  }).join('');
  if (_batchRenameItems.length > 5) lines += '<div style="color:#334155;font-size:11px;margin-top:4px">...and ' + (_batchRenameItems.length - 5) + ' more</div>';
  preview.innerHTML = lines || '<div style="color:#475569">No items to rename</div>';
  preview.style.display = 'block';
}

async function executeBatchRename() {
  var prefix = document.getElementById('batch-rename-prefix').value.trim();
  var progress = document.getElementById('batch-rename-progress');
  progress.style.display = 'block';
  progress.style.color = '#f59e0b';
  progress.textContent = 'Renaming ' + _batchRenameItems.length + ' file(s)...';
  var endpoint = _batchRenameType === 'photos' ? '/api/rename_photo' : '/api/rename_file';
  var errors = 0;
  for (var i = 0; i < _batchRenameItems.length; i++) {
    var item = _batchRenameItems[i];
    var base = prefix || (item.species && item.species[0]) || item.filename.split('.').slice(0,-1).join('.');
    var ext = item.filename.split('.').pop();
    var num = String(i + 1).padStart(3, '0');
    var newName = base.replace(/[^a-zA-Z0-9 .,() -]/g, '_') + '_' + num + '.' + ext;
    var idField = _batchRenameType === 'photos' ? 'id' : 'id';
    var r = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: item.id || item.vid_id, path: item.path, new_name: newName})});
    var d = await r.json();
    if (!d.ok) errors++;
  }
  if (errors === 0) {
    progress.style.color = '#22c55e';
    progress.textContent = 'Done! ' + _batchRenameItems.length + ' file(s) renamed.';
  } else {
    progress.style.color = '#f59e0b';
    progress.textContent = (_batchRenameItems.length - errors) + ' renamed, ' + errors + ' failed.';
  }
  setTimeout(function(){
    closeBatchRename();
    if (_batchRenameType === 'photos') { clearPhotoSelection(); }
    else { clearClipSelection(); }
  }, 1500);
}

var _confirmIDTarget = null; // 'frames', 'photos', or 'clip'
var _confirmIDClipIds = null;

function openConfirmIDPicker(target) {
  var ids = target === 'photos' ? Object.keys(selectedPhotos) : Object.keys(selected);
  if (!ids.length) return;
  _confirmIDTarget = target;
  _confirmIDClipIds = null;
  // Collect all unique species across selected items
  var allSpecies = {};
  var frames = target === 'photos' ? window._photos : window._frames;
  ids.forEach(function(id) {
    var item = frames[id];
    if (item && item.species) {
      item.species.forEach(function(s) { allSpecies[s] = true; });
    }
  });
  showConfirmIDModal(Object.keys(allSpecies));
}

function openConfirmIDPickerForClip(frameIds) {
  _confirmIDTarget = 'clip';
  _confirmIDClipIds = frameIds;
  var allSpecies = {};
  frameIds.forEach(function(id) {
    var item = window._frames[id];
    if (item && item.species) {
      item.species.forEach(function(s) { allSpecies[s] = true; });
    }
  });
  showConfirmIDModal(Object.keys(allSpecies));
}

function showConfirmIDModal(species) {
  var list = document.getElementById('confirm-id-species-list');
  if (!species.length) {
    alert('No species tagged on selected items. Please tag species first.');
    return;
  }
  list.innerHTML = species.map(function(s) {
    return '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:6px 8px;background:#071828;border-radius:6px">' +
      '<input type="checkbox" value="' + s.replace(/"/g, '&quot;') + '" style="width:14px;height:14px;cursor:pointer"> ' +
      '<span style="color:#dbeafe;font-size:13px">' + s + '</span></label>';
  }).join('');
  document.getElementById('confirm-id-progress').style.display = 'none';
  document.getElementById('confirm-id-modal').classList.add('show');
}

function closeConfirmIDModal() {
  document.getElementById('confirm-id-modal').classList.remove('show');
  _confirmIDTarget = null; _confirmIDClipIds = null;
}

async function runConfirmIDLookup() {
  var checked = Array.from(document.getElementById('confirm-id-species-list').querySelectorAll('input:checked'));
  if (!checked.length) { alert('Please tick at least one species.'); return; }
  var chosenSpecies = checked.map(function(c){ return c.value; });
  var ids = _confirmIDTarget === 'photos' ? Object.keys(selectedPhotos)
          : _confirmIDTarget === 'clip' ? _confirmIDClipIds
          : Object.keys(selected);
  var itemType = _confirmIDTarget === 'photos' ? 'photo' : 'frame';
  var progress = document.getElementById('confirm-id-progress');
  progress.style.display = 'block';
  progress.style.color = '#f59e0b';
  progress.textContent = 'Looking up ' + chosenSpecies.join(', ') + '...';
  // Run lookup once for the chosen species to get the info
  try {
    var r = await fetch('/api/lookup_species', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({species: chosenSpecies, type: itemType})});
    var d = await r.json();
    if (!d.ok) { progress.style.color='#ef4444'; progress.textContent='Lookup failed: '+(d.error||'unknown'); return; }
    // Now apply to all selected items
    progress.textContent = 'Applying to ' + ids.length + ' item(s)...';
    var table = itemType === 'photo' ? 'update_photo' : 'update_frame';
    for (var i = 0; i < ids.length; i++) {
      await fetch('/api/' + table, {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id: ids[i], habitat: d.habitat, behaviours: d.behaviours,
          notes: d.notes, id_confidence: 'confirmed', species: chosenSpecies})});
    }
    progress.style.color = '#22c55e';
    progress.textContent = 'Done! ' + ids.length + ' item(s) updated.';
    setTimeout(function(){
      closeConfirmIDModal();
      if (_confirmIDTarget === 'photos') { clearPhotoSelection(); }
      else { clearSelection(); }
    }, 1500);
  } catch(e) {
    progress.style.color = '#ef4444';
    progress.textContent = 'Error: ' + e.message;
  }
}

function photosBulkSetSpecies() {
  var n = Object.keys(selectedPhotos).length; if (!n) return;
  document.getElementById('bulk-count').textContent = n;
  document.getElementById('bulk-species-input').value = '';
  document.getElementById('bulk-ac-list').style.display = NONE;
  document.getElementById('bulk-species-tags').innerHTML = '';
  window._bulkSpecies = [];
  window._bulkTarget = 'photos';
  document.getElementById('bulk-modal').classList.add('show');
  setTimeout(function(){ document.getElementById('bulk-species-input').focus(); }, 100);
}

function photosBulkSetSiteDate() {
  var n = Object.keys(selectedPhotos).length; if (!n) return;
  document.getElementById('bulk-site-count').textContent = n;
  ['bulk-country-input','bulk-region-input','bulk-area-input','bulk-site-input','bulk-date-input'].forEach(function(id){ document.getElementById(id).value=''; });
  ['bulk-country-ac-list','bulk-region-ac-list','bulk-area-ac-list','bulk-site-ac-list'].forEach(function(id){ document.getElementById(id).style.display=NONE; });
  window._bulkTarget = 'photos';
  document.getElementById('bulk-site-modal').classList.add('show');
  setTimeout(function(){ document.getElementById('bulk-country-input').focus(); }, 100);
}

// ── Folder removal ────────────────────────────────────────────────────────────

async function removeVideoFolder(folder) {
  if (!confirm('Remove all videos and frames from "'+folder+'" from the index? Files on disk will NOT be deleted.')) return;
  var r = await fetch('/api/remove_video_folder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({folder:folder})});
  var d = await r.json();
  alert('Removed '+d.videos+' video(s) and '+d.frames+' frame(s) from index.');
  loadFolderList(); loadAllSpecies(); loadAllSites(); loadStat(); load();
}

async function removePhotoFolder(folder) {
  if (!confirm('Remove all photos from "'+folder+'" from the index? Files on disk will NOT be deleted.')) return;
  var r = await fetch('/api/remove_photo_folder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({folder:folder})});
  var d = await r.json();
  alert('Removed '+d.photos+' photo(s) from index.');
  loadPhotoFolderList(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoStat(); loadPhotos();
}

async function loadPhotoFolderList() {
  var r = await fetch('/api/photo_folders'); var data = await r.json();
  var el = document.getElementById('photo-folder-list');
  if (!data.length) { el.innerHTML='<div style="color:#334155;font-size:13px">No photo folders indexed yet.</div>'; return; }
  var html = '';
  data.forEach(function(f,i){
    html+='<div class="fl-item"><div><div class="fl-path">'+f.folder+'</div>';
    html+='<div class="fl-meta">'+f.photos+' photo(s)</div></div>';
    html+='<div style="display:flex;gap:6px">';
    html+='<button class="btn-amber btn-sm" data-idx="'+i+'">Folder</button>';
    html+='<button class="btn-red btn-sm" data-idx="'+i+'">Remove</button>';
    html+='</div></div>';
  });
  el.innerHTML = html;
  el.querySelectorAll('.btn-amber').forEach(function(btn){
    btn.addEventListener('click', function(){ openExplorer(data[btn.dataset.idx].folder); });
  });
  el.querySelectorAll('.btn-red').forEach(function(btn){
    btn.addEventListener('click', function(){ removePhotoFolder(data[btn.dataset.idx].folder); });
  });
}
</script>
</body>
</html>"""

    @app.route("/")
    def index(): return HTML

    @app.route("/api/stat")
    def stat():
        db = get_db()
        v = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        f = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        return jsonify({"videos":v,"frames":f})

    @app.route("/api/browse")
    def browse():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.wm_attributes('-topmost',True)
        path = filedialog.askdirectory(title="Select footage folder")
        root.destroy()
        return jsonify({"path":path or ""})

    @app.route("/api/scan", methods=["POST"])
    def scan():
        data = request.json
        path = data.get("path","")
        workers = data.get("workers", DEFAULT_WORKERS)
        batch_mode = data.get("batch", False)
        region = data.get("region") or None
        def run():
            stop_flag.clear()
            if not get_ai_client():
                scan_queue.put({"type":"error","msg":"No API key set. Open Settings (gear icon) and enter your Anthropic API key."}); return
            root = Path(path)
            if not root.exists():
                scan_queue.put({"type":"error","msg":"Path not found: "+path}); return
            indexed = {r[0] for r in conn.execute("SELECT path FROM videos")}
            videos = sorted({p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXTS and str(p) not in indexed})
            if not videos:
                scan_queue.put({"type":"done","msg":"No new videos found.","total":0}); return
            scan_queue.put({"type":"progress","msg":"Found "+str(len(videos))+" new video(s)...","pct":0})
            if batch_mode:
                def bp(pct,done,total):
                    scan_queue.put({"type":"progress","msg":"Batch: "+str(done)+"/"+str(total)+" frames","pct":pct})
                total = run_batch(get_ai_client(), conn, videos, workers, bp, region=region)
                scan_queue.put({"type":"done","msg":"Batch complete - "+str(total)+" frames added.","total":total})
                return
            total = 0
            for i,vp in enumerate(videos):
                if stop_flag.is_set(): break
                scan_queue.put({"type":"progress","msg":"["+str(i+1)+"/"+str(len(videos))+"] "+vp.name,"pct":int(i/len(videos)*100)})
                count = index_video(get_ai_client(),conn,vp,workers,region=region)
                if count > 0:
                    total += count
                    scan_queue.put({"type":"progress","msg":"["+str(i+1)+"/"+str(len(videos))+"] "+vp.name,
                                    "pct":int((i+1)/len(videos)*100),"file":vp.name,"frames":count})
            scan_queue.put({"type":"done","msg":str(total)+" frames added.","total":total})
        threading.Thread(target=run,daemon=True).start()
        return jsonify({"started":True})

    @app.route("/api/scan_stop", methods=["POST"])
    def scan_stop():
        stop_flag.set(); return jsonify({"stopped":True})

    @app.route("/api/scan_progress")
    def scan_progress():
        def stream():
            while True:
                try:
                    msg = scan_queue.get(timeout=30)
                    yield "data: "+json.dumps(msg)+"\n\n"
                    if msg.get("type") in ("done","error"): break
                except: yield "data: "+json.dumps({"type":"ping"})+"\n\n"
        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.route("/api/folders")
    def folders():
        rows = get_db().execute("SELECT path, filename, frame_count FROM videos").fetchall()
        fm = {}
        for path,fname,fc in rows:
            folder = str(Path(path).parent)
            if folder not in fm: fm[folder] = {"videos":0,"frames":0}
            fm[folder]["videos"] += 1
            fm[folder]["frames"] += (fc or 0)
        return jsonify([{"folder":k,"videos":v["videos"],"frames":v["frames"]} for k,v in sorted(fm.items())])

    @app.route("/api/clips")
    def clips():
        db = get_db()
        q = request.args.get("q","")
        sp = request.args.get("species","")
        site = request.args.get("site","")
        country = request.args.get("country","")
        region = request.args.get("region","")
        area = request.args.get("area","")
        sql = ("SELECT v.id, v.filename, v.path, v.duration, "
               "COALESCE(v.dive_site,''), COALESCE(v.dive_date,''), COALESCE(v.country,''), "
               "COALESCE(v.region,''), COALESCE(v.area,''), "
               "GROUP_CONCAT(f.species, '|||'), "
               "MIN(f.thumb_path), COUNT(f.id), "
               "MAX(COALESCE(f.marked_delete,0)), MIN(f.visibility) "
               "FROM videos v JOIN frames f ON f.video_id=v.id WHERE 1=1")
        params = []
        if q:
            sql += (" AND (f.species LIKE ? OR f.habitat LIKE ? OR f.notes LIKE ? OR v.filename LIKE ?"
                    " OR v.dive_site LIKE ? OR v.country LIKE ? OR v.region LIKE ? OR v.area LIKE ?)")
            params += ["%"+q+"%"]*8
        if sp: sql += " AND f.species LIKE ?"; params.append("%"+sp+"%")
        if site: sql += " AND v.dive_site=?"; params.append(site)
        if country: sql += " AND v.country=?"; params.append(country)
        if region: sql += " AND v.region=?"; params.append(region)
        if area: sql += " AND v.area=?"; params.append(area)
        where_only = sql[sql.find("WHERE"):]
        count_sql = "SELECT COUNT(DISTINCT v.id) FROM videos v JOIN frames f ON f.video_id=v.id " + where_only
        total = db.execute(count_sql, params).fetchone()[0]
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 100))
        sort = request.args.get("sort","")
        offset = (page-1)*page_size
        # Sort clips by min id_confidence of their frames
        confidence_order = "CASE MIN(COALESCE(f.id_confidence,'')) WHEN 'confirmed' THEN 2 WHEN 'probable' THEN 1 WHEN 'uncertain' THEN 0 ELSE -1 END"
        if sort == "confidence_desc":
            order = confidence_order + " DESC, v.filename"
        elif sort == "confidence_asc":
            order = confidence_order + " ASC, v.filename"
        else:
            order = "v.filename"
        sql += " GROUP BY v.id ORDER BY "+order+" LIMIT ? OFFSET ?"
        rows = db.execute(sql, params+[page_size, offset]).fetchall()
        result = []
        for r in rows:
            all_sp = []
            seen = set()
            for sp_json in (r[9] or "").split("|||"):
                try:
                    for s in json.loads(sp_json or "[]"):
                        k = s.lower()
                        if k and k not in seen: seen.add(k); all_sp.append(s)
                except: pass
            thumb_id = Path(r[10]).stem if r[10] else ""
            result.append({"vid_id":r[0],"filename":r[1],"path":r[2],"duration":round(r[3] or 0,1),
                "dive_site":r[4],"dive_date":r[5],"country":r[6],"region":r[7],"area":r[8],
                "species":all_sp,"thumb_id":thumb_id,"frame_count":r[11],
                "marked_delete":r[12],"visibility":r[13]})
        return jsonify({"items":result,"total":total})

    @app.route("/api/frames")
    def frames():
        db = get_db()
        q = request.args.get("q","")
        sp = request.args.get("species","")
        site = request.args.get("site","")
        country = request.args.get("country","")
        region = request.args.get("region","")
        area = request.args.get("area","")
        vid_id = request.args.get("vid_id","")
        sort = request.args.get("sort","")
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 100))
        offset = (page-1)*page_size

        base = ("FROM frames f JOIN videos v ON f.video_id=v.id WHERE 1=1")
        params = []
        if vid_id:
            base += " AND f.video_id=?"; params.append(vid_id)
        elif q:
            base += (" AND (f.species LIKE ? OR f.habitat LIKE ? OR f.notes LIKE ? OR v.filename LIKE ?"
                    " OR v.dive_site LIKE ? OR v.country LIKE ? OR v.region LIKE ? OR v.area LIKE ?)")
            params += ["%"+q+"%"]*8
        if sp: base += " AND f.species LIKE ?"; params.append("%"+sp+"%")
        if site: base += " AND v.dive_site=?"; params.append(site)
        if country: base += " AND v.country=?"; params.append(country)
        if region: base += " AND v.region=?"; params.append(region)
        if area: base += " AND v.area=?"; params.append(area)

        total = db.execute("SELECT COUNT(*) "+base, params).fetchone()[0]

        # Build ORDER BY
        confidence_order = "CASE COALESCE(f.id_confidence,'') WHEN 'confirmed' THEN 2 WHEN 'probable' THEN 1 WHEN 'uncertain' THEN 0 ELSE -1 END"
        if sort == "confidence_desc":
            order = confidence_order + " DESC, v.filename, f.timestamp"
        elif sort == "confidence_asc":
            order = confidence_order + " ASC, v.filename, f.timestamp"
        else:
            order = "v.filename, f.timestamp"

        sql = ("SELECT f.id, v.filename, v.path, f.timestamp, f.species, f.habitat, "
               "f.visibility, f.notes, f.behaviours, COALESCE(f.reviewed,0), COALESCE(f.marked_delete,0), "
               "COALESCE(v.dive_site,''), COALESCE(v.dive_date,''), COALESCE(v.country,''), "
               "COALESCE(v.region,''), COALESCE(v.area,''), COALESCE(f.id_confidence,'') "
               +base+" ORDER BY "+order+" LIMIT ? OFFSET ?")
        rows = db.execute(sql, params+[page_size, offset]).fetchall()
        items = [{"id":r[0],"filename":r[1],"path":r[2],"timestamp":r[3],
            "species":json.loads(r[4] or "[]"),"habitat":r[5],"visibility":r[6],
            "notes":r[7],"behaviours":json.loads(r[8] or "[]"),"reviewed":r[9],"marked_delete":r[10],
            "dive_site":r[11],"dive_date":r[12],"country":r[13],"region":r[14],"area":r[15],
            "id_confidence":r[16]} for r in rows]
        # For vid_id queries return flat list (used by clip frame strip)
        if vid_id: return jsonify(items)
        return jsonify({"items":items,"total":total})

    @app.route("/api/species")
    def species():
        rows = get_db().execute("SELECT DISTINCT value FROM frames, json_each(frames.species) ORDER BY value").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/dive_sites")
    def dive_sites():
        rows = get_db().execute("SELECT DISTINCT dive_site FROM videos WHERE dive_site != '' ORDER BY dive_site").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/countries")
    def countries():
        rows = get_db().execute("SELECT DISTINCT country FROM videos WHERE country != '' ORDER BY country").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/regions")
    def regions():
        rows = get_db().execute("SELECT DISTINCT region FROM videos WHERE region != '' ORDER BY region").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/areas")
    def areas():
        rows = get_db().execute("SELECT DISTINCT area FROM videos WHERE area != '' ORDER BY area").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/save_site_date", methods=["POST"])
    def save_site_date():
        data = request.json
        fid = data.get("frame_id")
        db = get_db()
        row = db.execute("SELECT video_id FROM frames WHERE id=?",(fid,)).fetchone()
        if not row: return jsonify({"error":"frame not found"}), 404
        db.execute("UPDATE videos SET dive_site=?, dive_date=?, country=?, region=?, area=? WHERE id=?",
                   (data.get("dive_site","").strip(), data.get("dive_date","").strip(),
                    data.get("country","").strip(), data.get("region","").strip(),
                    data.get("area","").strip(), row[0]))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/bulk_site_date", methods=["POST"])
    def bulk_site_date():
        data = request.json
        frame_ids = data.get("frame_ids", [])
        site = data.get("dive_site","").strip()
        date = data.get("dive_date","").strip()
        country = data.get("country","").strip()
        region = data.get("region","").strip()
        area = data.get("area","").strip()
        db = get_db()
        for fid in frame_ids:
            row = db.execute("SELECT video_id FROM frames WHERE id=?",(fid,)).fetchone()
            if row:
                if site: db.execute("UPDATE videos SET dive_site=? WHERE id=?",(site,row[0]))
                if date: db.execute("UPDATE videos SET dive_date=? WHERE id=?",(date,row[0]))
                if country: db.execute("UPDATE videos SET country=? WHERE id=?",(country,row[0]))
                if region: db.execute("UPDATE videos SET region=? WHERE id=?",(region,row[0]))
                if area: db.execute("UPDATE videos SET area=? WHERE id=?",(area,row[0]))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/species_search")
    def species_search():
        q = request.args.get("q","").lower()
        rows = get_db().execute("SELECT value, COUNT(*) c FROM frames, json_each(frames.species) GROUP BY value ORDER BY c DESC").fetchall()
        return jsonify([{"species":r[0],"count":r[1]} for r in rows if q in r[0].lower()])

    @app.route("/api/species_replace", methods=["POST"])
    def species_replace():
        data = request.json
        search = data.get("search","").lower()
        replace = data.get("replace","").strip()
        if not search or not replace: return jsonify({"error":"missing fields"}), 400
        db = get_db()
        rows = db.execute("SELECT id, species FROM frames").fetchall()
        updated = 0
        for fid, sp_json in rows:
            try: species_list = json.loads(sp_json or "[]")
            except: continue
            new_list = [replace if search in s.lower() else s for s in species_list]
            changed = new_list != species_list
            seen, deduped = set(), []
            for s in new_list:
                if s.lower() not in seen: seen.add(s.lower()); deduped.append(s)
            if changed:
                db.execute("UPDATE frames SET species=? WHERE id=?",(json.dumps(deduped),fid)); updated += 1
        db.commit()
        return jsonify({"frames":updated})

    @app.route("/api/fix_dashes", methods=["POST"])
    def fix_dashes():
        db = get_db()
        rows = db.execute("SELECT id, species FROM frames").fetchall()
        updated = 0
        for fid, sp_json in rows:
            try: species_list = json.loads(sp_json or "[]")
            except: continue
            new_list = [s.replace("\u2014","-").replace("\u2013","-") for s in species_list]
            if new_list != species_list:
                db.execute("UPDATE frames SET species=? WHERE id=?",(json.dumps(new_list),fid)); updated += 1
        db.commit()
        return jsonify({"frames":updated})

    @app.route("/api/update_frame", methods=["POST"])
    def update_frame():
        data = request.json
        fid = data.get("id")
        if not fid: return jsonify({"error":"no id"}), 400
        db = get_db()
        if "species" in data: db.execute("UPDATE frames SET species=? WHERE id=?",(json.dumps(data["species"]),fid))
        if "reviewed" in data: db.execute("UPDATE frames SET reviewed=? WHERE id=?",(data["reviewed"],fid))
        if "marked_delete" in data: db.execute("UPDATE frames SET marked_delete=? WHERE id=?",(data["marked_delete"],fid))
        if "habitat" in data: db.execute("UPDATE frames SET habitat=? WHERE id=?",(data["habitat"],fid))
        if "behaviours" in data: db.execute("UPDATE frames SET behaviours=? WHERE id=?",(json.dumps(data["behaviours"]),fid))
        if "notes" in data: db.execute("UPDATE frames SET notes=? WHERE id=?",(data["notes"],fid))
        if "visibility" in data: db.execute("UPDATE frames SET visibility=? WHERE id=?",(data["visibility"],fid))
        if "id_confidence" in data: db.execute("UPDATE frames SET id_confidence=? WHERE id=?",(data["id_confidence"],fid))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/lookup_species", methods=["POST"])
    def lookup_species():
        data = request.json
        species = data.get("species", [])
        fid = data.get("frame_id")
        item_type = data.get("type", "frame")
        if not species: return jsonify({"error": "no species"}), 400
        client = get_ai_client()
        if not client: return jsonify({"error": "No API key set"}), 400
        species_str = ", ".join(species)
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=800,
                messages=[{"role": "user", "content":
                    f"Give me a brief factual summary about the marine species: {species_str}\n\n"
                    f"Format your response in exactly these 3 sections with these exact labels:\n"
                    f"HABITAT: one sentence about substrate, depth range and environment\n"
                    f"BEHAVIOURS: comma-separated list of 3-4 known behaviours\n"
                    f"NOTES: 2-3 sentences covering interesting facts, conservation status, and typical size"}])
            txt = resp.content[0].text.strip()
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        # Parse plain text sections — no JSON needed
        habitat, behaviours_str, notes = "", "", ""
        for line in txt.splitlines():
            if line.startswith("HABITAT:"):
                habitat = line[8:].strip()
            elif line.startswith("BEHAVIOURS:"):
                behaviours_str = line[11:].strip()
            elif line.startswith("NOTES:"):
                notes = line[6:].strip()
            elif notes:
                notes += " " + line.strip()  # capture multi-line notes
        behaviours = [b.strip() for b in behaviours_str.split(",") if b.strip()]
        db = get_db()
        if fid:
            table = "frames" if item_type == "frame" else "photos"
            db.execute(f"UPDATE {table} SET habitat=?, behaviours=?, notes=?, id_confidence=? WHERE id=?",
                       (habitat, json.dumps(behaviours), notes, "confirmed", fid))
            db.commit()
        return jsonify({"ok": True, "habitat": habitat, "behaviours": behaviours, "notes": notes})

    @app.route("/api/bulk_update", methods=["POST"])
    def bulk_update():
        data = request.json
        ids = data.get("ids",[]); update = data.get("update",{})
        if not ids: return jsonify({"ok":True})
        db = get_db()
        for fid in ids:
            if "species" in update: db.execute("UPDATE frames SET species=? WHERE id=?",(json.dumps(update["species"]),fid))
            if "reviewed" in update: db.execute("UPDATE frames SET reviewed=? WHERE id=?",(update["reviewed"],fid))
            if "marked_delete" in update: db.execute("UPDATE frames SET marked_delete=? WHERE id=?",(update["marked_delete"],fid))
        db.commit()
        return jsonify({"ok":True,"updated":len(ids)})

    @app.route("/api/rename_file", methods=["POST"])
    def rename_file():
        data = request.json
        old_path = Path(data.get("path",""))
        new_name = data.get("new_name","").strip()
        if not old_path.exists(): return jsonify({"error":"File not found"}), 404
        old_ext = old_path.suffix
        if not new_name.lower().endswith(old_ext.lower()): new_name += old_ext
        new_path = old_path.parent / new_name
        try:
            old_path.rename(new_path)
            db = get_db()
            db.execute("UPDATE videos SET path=?, filename=? WHERE path=?",(str(new_path),new_name,str(old_path)))
            db.commit()
            return jsonify({"ok":True,"new_filename":new_name,"new_path":str(new_path)})
        except Exception as e: return jsonify({"error":str(e)}), 500

    @app.route("/api/marked_files")
    def marked_files():
        rows = get_db().execute("SELECT DISTINCT v.path, v.filename FROM frames f JOIN videos v ON f.video_id=v.id WHERE f.marked_delete=1").fetchall()
        return jsonify([{"path":r[0],"filename":r[1]} for r in rows])

    @app.route("/api/purge_marked", methods=["POST"])
    def purge_marked():
        db = get_db()
        rows = db.execute("SELECT DISTINCT v.id, v.path FROM frames f JOIN videos v ON f.video_id=v.id WHERE f.marked_delete=1").fetchall()
        deleted, failed = 0, 0
        for vid_id,path in rows:
            try:
                p = Path(path)
                if p.exists(): p.unlink()
                db.execute("DELETE FROM frames WHERE video_id=?",(vid_id,))
                db.execute("DELETE FROM videos WHERE id=?",(vid_id,))
                deleted += 1
            except: failed += 1
        db.commit()
        return jsonify({"deleted":deleted,"failed":failed})

    # ── Photo routes ──────────────────────────────────────────────────────────

    @app.route("/api/photo_stat")
    def photo_stat():
        p = get_db().execute("SELECT COUNT(*) FROM photos").fetchone()[0]
        return jsonify({"photos": p})

    @app.route("/api/photos")
    def photos_list():
        db = get_db()
        q = request.args.get("q","")
        sp = request.args.get("species","")
        site = request.args.get("site","")
        country = request.args.get("country","")
        region = request.args.get("region","")
        area = request.args.get("area","")
        sort = request.args.get("sort","")
        page = int(request.args.get("page",1))
        page_size = int(request.args.get("page_size",100))
        offset = (page-1)*page_size
        base = "FROM photos WHERE 1=1"
        params = []
        if q:
            base += " AND (species LIKE ? OR habitat LIKE ? OR notes LIKE ? OR filename LIKE ? OR dive_site LIKE ? OR country LIKE ? OR region LIKE ? OR area LIKE ?)"
            params += ["%"+q+"%"]*8
        if sp: base += " AND species LIKE ?"; params.append("%"+sp+"%")
        if site: base += " AND dive_site=?"; params.append(site)
        if country: base += " AND country=?"; params.append(country)
        if region: base += " AND region=?"; params.append(region)
        if area: base += " AND area=?"; params.append(area)
        total = db.execute("SELECT COUNT(*) "+base, params).fetchone()[0]
        confidence_order = "CASE COALESCE(id_confidence,'') WHEN 'confirmed' THEN 2 WHEN 'probable' THEN 1 WHEN 'uncertain' THEN 0 ELSE -1 END"
        if sort == "confidence_desc":
            order = confidence_order + " DESC, filename"
        elif sort == "confidence_asc":
            order = confidence_order + " ASC, filename"
        else:
            order = "filename"
        rows = db.execute("SELECT id,path,filename,filesize,species,habitat,visibility,behaviours,notes,"
                          "COALESCE(reviewed,0),COALESCE(marked_delete,0),"
                          "COALESCE(dive_site,''),COALESCE(dive_date,''),COALESCE(country,''),"
                          "COALESCE(region,''),COALESCE(area,''),COALESCE(id_confidence,'') "+base
                          +" ORDER BY "+order+" LIMIT ? OFFSET ?", params+[page_size,offset]).fetchall()
        items = [{"id":r[0],"path":r[1],"filename":r[2],"filesize":r[3],
                  "species":json.loads(r[4] or "[]"),"habitat":r[5],"visibility":r[6],
                  "behaviours":json.loads(r[7] or "[]"),"notes":r[8],
                  "reviewed":r[9],"marked_delete":r[10],
                  "dive_site":r[11],"dive_date":r[12],"country":r[13],"region":r[14],"area":r[15],
                  "id_confidence":r[16]} for r in rows]
        return jsonify({"items":items,"total":total})

    @app.route("/api/photo_species")
    def photo_species():
        rows = get_db().execute("SELECT DISTINCT value FROM photos, json_each(photos.species) ORDER BY value").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/photo_species_grouped")
    def photo_species_grouped():
        import re
        db = get_db()
        rows = db.execute(
            "SELECT value, p.id FROM photos p, json_each(p.species)").fetchall()
        # Normalise: strip parenthetical qualifiers for grouping
        groups = {}
        for sp, pid in rows:
            key = re.sub(r'\s*\([^)]*\)', '', sp).strip()
            if key not in groups:
                groups[key] = {"count": 0, "thumb_id": pid}
            groups[key]["count"] += 1
        result = sorted([{"species": k, "count": v["count"], "thumb_id": v["thumb_id"]}
                         for k, v in groups.items()], key=lambda x: (-x["count"], x["species"]))
        return jsonify(result)

    @app.route("/api/photo_sites")
    def photo_sites():
        rows = get_db().execute("SELECT DISTINCT dive_site FROM photos WHERE dive_site!='' ORDER BY dive_site").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/photo_countries")
    def photo_countries():
        rows = get_db().execute("SELECT DISTINCT country FROM photos WHERE country!='' ORDER BY country").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/photo_regions")
    def photo_regions():
        rows = get_db().execute("SELECT DISTINCT region FROM photos WHERE region!='' ORDER BY region").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/photo_areas")
    def photo_areas():
        rows = get_db().execute("SELECT DISTINCT area FROM photos WHERE area!='' ORDER BY area").fetchall()
        return jsonify([r[0] for r in rows])

    @app.route("/api/photo_folders")
    def photo_folders():
        rows = get_db().execute("SELECT path FROM photos").fetchall()
        fm = {}
        for (path,) in rows:
            folder = str(Path(path).parent)
            fm[folder] = fm.get(folder, 0) + 1
        return jsonify([{"folder":k,"photos":v} for k,v in sorted(fm.items())])

    @app.route("/api/update_photo", methods=["POST"])
    def update_photo():
        data = request.json
        pid = data.get("id")
        if not pid: return jsonify({"error":"no id"}), 400
        db = get_db()
        if "species" in data: db.execute("UPDATE photos SET species=? WHERE id=?",(json.dumps(data["species"]),pid))
        if "reviewed" in data: db.execute("UPDATE photos SET reviewed=? WHERE id=?",(data["reviewed"],pid))
        if "marked_delete" in data: db.execute("UPDATE photos SET marked_delete=? WHERE id=?",(data["marked_delete"],pid))
        if "habitat" in data: db.execute("UPDATE photos SET habitat=? WHERE id=?",(data["habitat"],pid))
        if "behaviours" in data: db.execute("UPDATE photos SET behaviours=? WHERE id=?",(json.dumps(data["behaviours"]),pid))
        if "notes" in data: db.execute("UPDATE photos SET notes=? WHERE id=?",(data["notes"],pid))
        if "visibility" in data: db.execute("UPDATE photos SET visibility=? WHERE id=?",(data["visibility"],pid))
        if "id_confidence" in data: db.execute("UPDATE photos SET id_confidence=? WHERE id=?",(data["id_confidence"],pid))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/save_photo_site_date", methods=["POST"])
    def save_photo_site_date():
        data = request.json
        pid = data.get("photo_id")
        if not pid: return jsonify({"error":"no id"}), 400
        db = get_db()
        db.execute("UPDATE photos SET dive_site=?,dive_date=?,country=?,region=?,area=? WHERE id=?",
                   (data.get("dive_site","").strip(), data.get("dive_date","").strip(),
                    data.get("country","").strip(), data.get("region","").strip(),
                    data.get("area","").strip(), pid))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/rename_photo", methods=["POST"])
    def rename_photo():
        data = request.json
        old_path = Path(data.get("path",""))
        new_name = data.get("new_name","").strip()
        if not old_path.exists(): return jsonify({"error":"File not found"}), 404
        old_ext = old_path.suffix
        if not new_name.lower().endswith(old_ext.lower()): new_name += old_ext
        new_path = old_path.parent / new_name
        try:
            old_path.rename(new_path)
            db = get_db()
            db.execute("UPDATE photos SET path=?,filename=? WHERE path=?",(str(new_path),new_name,str(old_path)))
            db.commit()
            return jsonify({"ok":True,"new_filename":new_name,"new_path":str(new_path)})
        except Exception as e: return jsonify({"error":str(e)}), 500

    @app.route("/api/marked_photos")
    def marked_photos():
        rows = get_db().execute("SELECT path,filename FROM photos WHERE marked_delete=1").fetchall()
        return jsonify([{"path":r[0],"filename":r[1]} for r in rows])

    @app.route("/api/purge_marked_photos", methods=["POST"])
    def purge_marked_photos():
        db = get_db()
        rows = db.execute("SELECT id,path,thumb_path FROM photos WHERE marked_delete=1").fetchall()
        deleted, failed = 0, 0
        for pid,path,thumb in rows:
            try:
                p = Path(path)
                if p.exists(): p.unlink()
                if thumb:
                    t = Path(thumb)
                    if t.exists(): t.unlink()
                db.execute("DELETE FROM photos WHERE id=?",(pid,))
                deleted += 1
            except: failed += 1
        db.commit()
        return jsonify({"deleted":deleted,"failed":failed})

    @app.route("/api/open_photo")
    def open_photo():
        path = request.args.get("path","")
        p = Path(path)
        if not p.exists(): return jsonify({"ok":False,"error":"File not found"}), 404
        try: os.startfile(str(p)); return jsonify({"ok":True})
        except Exception as e: return jsonify({"ok":False,"error":str(e)}), 500

    @app.route("/api/bulk_update_photos", methods=["POST"])
    def bulk_update_photos():
        data = request.json
        ids = data.get("ids", []); update = data.get("update", {})
        if not ids: return jsonify({"ok":True})
        db = get_db()
        for pid in ids:
            if "species" in update: db.execute("UPDATE photos SET species=? WHERE id=?",(json.dumps(update["species"]),pid))
            if "reviewed" in update: db.execute("UPDATE photos SET reviewed=? WHERE id=?",(update["reviewed"],pid))
            if "marked_delete" in update: db.execute("UPDATE photos SET marked_delete=? WHERE id=?",(update["marked_delete"],pid))
        db.commit()
        return jsonify({"ok":True,"updated":len(ids)})

    @app.route("/api/bulk_photo_site_date", methods=["POST"])
    def bulk_photo_site_date():
        data = request.json
        photo_ids = data.get("photo_ids", [])
        site = data.get("dive_site","").strip()
        date = data.get("dive_date","").strip()
        country = data.get("country","").strip()
        region = data.get("region","").strip()
        area = data.get("area","").strip()
        db = get_db()
        for pid in photo_ids:
            if site: db.execute("UPDATE photos SET dive_site=? WHERE id=?",(site,pid))
            if date: db.execute("UPDATE photos SET dive_date=? WHERE id=?",(date,pid))
            if country: db.execute("UPDATE photos SET country=? WHERE id=?",(country,pid))
            if region: db.execute("UPDATE photos SET region=? WHERE id=?",(region,pid))
            if area: db.execute("UPDATE photos SET area=? WHERE id=?",(area,pid))
        db.commit()
        return jsonify({"ok":True})

    @app.route("/api/remove_video_folder", methods=["POST"])
    def remove_video_folder():
        folder = request.json.get("folder","").strip()
        if not folder: return jsonify({"error":"no folder"}), 400
        db = get_db()
        rows = db.execute("SELECT id FROM videos WHERE path LIKE ?", (folder+"%",)).fetchall()
        vid_ids = [r[0] for r in rows]
        frames = 0
        for vid_id in vid_ids:
            fc = db.execute("SELECT COUNT(*) FROM frames WHERE video_id=?",(vid_id,)).fetchone()[0]
            frames += fc
            # Remove thumb files
            for (tp,) in db.execute("SELECT thumb_path FROM frames WHERE video_id=?",(vid_id,)).fetchall():
                try:
                    if tp: Path(tp).unlink(missing_ok=True)
                except: pass
            db.execute("DELETE FROM frames WHERE video_id=?",(vid_id,))
            db.execute("DELETE FROM videos WHERE id=?",(vid_id,))
        db.commit()
        return jsonify({"videos":len(vid_ids),"frames":frames})

    @app.route("/api/remove_photo_folder", methods=["POST"])
    def remove_photo_folder():
        folder = request.json.get("folder","").strip()
        if not folder: return jsonify({"error":"no folder"}), 400
        db = get_db()
        rows = db.execute("SELECT id, thumb_path FROM photos WHERE path LIKE ?", (folder+"%",)).fetchall()
        for pid, thumb in rows:
            try:
                if thumb: Path(thumb).unlink(missing_ok=True)
            except: pass
            db.execute("DELETE FROM photos WHERE id=?",(pid,))
        db.commit()
        return jsonify({"photos":len(rows)})

    @app.route("/api/scan_photos", methods=["POST"])
    def scan_photos():
        data = request.json
        path = data.get("path","")
        workers = data.get("workers", DEFAULT_WORKERS)
        batch_mode = data.get("batch", False)
        region = data.get("region") or None
        def run():
            photo_stop_flag.clear()
            if not get_ai_client():
                photo_queue.put({"type":"error","msg":"No API key set. Open Settings (gear icon) and enter your Anthropic API key."}); return
            root = Path(path)
            if not root.exists():
                photo_queue.put({"type":"error","msg":"Path not found: "+path}); return
            indexed = {r[0] for r in conn.execute("SELECT path FROM photos")}
            new_photos = sorted({p for p in root.rglob("*") if p.suffix.lower() in PHOTO_EXTS and str(p) not in indexed})
            if not new_photos:
                photo_queue.put({"type":"done","msg":"No new photos found.","total":0}); return
            photo_queue.put({"type":"progress","msg":"Found "+str(len(new_photos))+" new photo(s)...","pct":0})
            if batch_mode:
                def bp(pct, done, total):
                    photo_queue.put({"type":"progress","msg":"Batch: "+str(done)+"/"+str(total)+" photos","pct":pct})
                total = run_photo_batch(get_ai_client(), conn, new_photos, bp, region=region)
                photo_queue.put({"type":"done","msg":"Batch complete — "+str(total)+" photos indexed.","total":total})
                return
            total = 0
            for i, pp in enumerate(new_photos):
                if photo_stop_flag.is_set(): break
                photo_queue.put({"type":"progress","msg":"["+str(i+1)+"/"+str(len(new_photos))+"] "+pp.name,"pct":int(i/len(new_photos)*100)})
                result = index_photo(get_ai_client(), conn, pp, region=region)
                if result == 1:
                    total += 1
                    photo_queue.put({"type":"progress","msg":"["+str(i+1)+"/"+str(len(new_photos))+"] "+pp.name,
                                     "pct":int((i+1)/len(new_photos)*100),"file":pp.name})
            photo_queue.put({"type":"done","msg":str(total)+" photos indexed.","total":total})
        threading.Thread(target=run, daemon=True).start()
        return jsonify({"started":True})

    @app.route("/api/scan_photos_stop", methods=["POST"])
    def scan_photos_stop():
        photo_stop_flag.set(); return jsonify({"stopped":True})

    @app.route("/api/scan_photos_progress")
    def scan_photos_progress():
        def stream():
            while True:
                try:
                    msg = photo_queue.get(timeout=30)
                    yield "data: "+json.dumps(msg)+"\n\n"
                    if msg.get("type") in ("done","error"): break
                except: yield "data: "+json.dumps({"type":"ping"})+"\n\n"
        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.route("/api/export_csv")
    def export_csv():
        import csv, io
        rows = get_db().execute("""SELECT v.filename, v.path, f.timestamp, f.species, f.habitat, f.visibility,
            f.behaviours, f.notes, v.indexed_at, COALESCE(v.country,''), COALESCE(v.region,''),
            COALESCE(v.area,''), COALESCE(v.dive_site,''), COALESCE(v.dive_date,'')
            FROM frames f JOIN videos v ON f.video_id=v.id ORDER BY v.filename, f.timestamp""").fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['Filename','Full Path','Timestamp (s)','Timestamp','Species','Habitat','Visibility',
                    'Behaviours','Notes','Indexed At','Country','Region','Area','Dive Site','Dive Date'])
        for r in rows:
            m,s=int(r[2]//60),int(r[2]%60)
            w.writerow([r[0],r[1],round(r[2],1),f"{m}:{s:02d}",
                        ', '.join(json.loads(r[3] or '[]')),r[4],r[5],
                        ', '.join(json.loads(r[6] or '[]')),r[7],r[8],r[9],r[10],r[11],r[12],r[13]])
        buf.seek(0)
        return Response(buf.getvalue(), mimetype='text/csv',
                        headers={"Content-Disposition":"attachment;filename=underwater_index_"+datetime.now().strftime('%Y%m%d')+".csv"})

    @app.route("/api/folder")
    def open_folder_ep():
        path = request.args.get("path","")
        p = Path(path)
        if p.is_file(): subprocess.Popen(["explorer","/select,",str(p)])
        else: subprocess.Popen(["explorer",str(p)])
        return jsonify({"opened":True})

    @app.route("/api/open")
    def open_file():
        path = request.args.get("path","")
        ts = float(request.args.get("ts",0))
        if not path or not Path(path).exists(): return jsonify({"error":"File not found"}), 404
        for vlc in [r"C:\Program Files\VideoLAN\VLC\vlc.exe", r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"]:
            if Path(vlc).exists():
                subprocess.Popen([vlc,f"--start-time={int(ts)}",path])
                return jsonify({"opened":"vlc","ts":ts})
        os.startfile(path)
        return jsonify({"opened":"default"})

    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        db = get_db()
        rows = db.execute("SELECT key, value FROM settings").fetchall()
        s = {r[0]: r[1] for r in rows}
        return jsonify({
            "api_key": s.get("api_key", ""),
            "default_region": s.get("default_region", "Indo-Pacific / Coral Triangle"),
            "regions": list(REGIONS.keys())
        })

    @app.route("/api/settings", methods=["POST"])
    def save_settings():
        data = request.json
        db = get_db()
        if "api_key" in data:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('api_key',?)", (data["api_key"].strip(),))
        if "default_region" in data:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('default_region',?)", (data["default_region"],))
        db.commit()
        # Reinitialise ai_client if api_key changed
        api_key_new = data.get("api_key","").strip()
        if api_key_new:
            try: _client["obj"] = ant.Anthropic(api_key=api_key_new)
            except: pass
        return jsonify({"ok": True})

    @app.route("/thumb/<frame_id>")
    def thumb(frame_id):
        p = FRAMES_DIR / f"{frame_id}.jpg"
        return send_file(p) if p.exists() else ("",404)

    if not headless: webbrowser.open("http://localhost:5000")
    app.run(debug=False, port=5000)


def main():
    p = argparse.ArgumentParser(description="Underwater Footage Indexer")
    sub = p.add_subparsers(dest="cmd")
    pi = sub.add_parser("index")
    pi.add_argument("path"); pi.add_argument("--workers", type=int, default=DEFAULT_WORKERS); pi.add_argument("--api-key")
    pb = sub.add_parser("batch", help="Overnight batch indexing, 50%% cheaper")
    pb.add_argument("path"); pb.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ps = sub.add_parser("search"); ps.add_argument("--query","-q"); ps.add_argument("--species","-s")
    pe = sub.add_parser("export"); pe.add_argument("--output","-o")
    sub.add_parser("stats"); sub.add_parser("viewer"); sub.add_parser("viewer_headless")
    args = p.parse_args()
    if not args.cmd: p.print_help(); return
    conn = init_db()
    {"index": cmd_index, "batch": cmd_batch, "search": cmd_search, "export": cmd_export,
     "stats": lambda a,c: cmd_stats(c),
     "viewer": lambda a,c: cmd_viewer(c),
     "viewer_headless": lambda a,c: cmd_viewer(c, headless=True)}[args.cmd](args, conn)

if __name__ == "__main__":
    main()
