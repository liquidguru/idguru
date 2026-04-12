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
IS_WIN          = sys.platform == "win32"
IS_MAC          = sys.platform == "darwin"
POPEN_FLAGS     = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}
DEFAULT_WORKERS = 10
MODEL           = "claude-sonnet-4-6"
MODEL_BATCH     = "claude-sonnet-4-6"
VIDEO_EXTS      = {'.mp4','.mov','.avi','.mkv','.mts','.m2ts','.mpg','.mpeg','.mxf','.mod','.wmv','.flv'}
PHOTO_EXTS      = {'.jpg','.jpeg','.tif','.tiff','.cr2','.nef','.arw','.dng','.orf','.raf','.rw2','.pef','.srw'}
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


def resolve_to_unc(path):
    """Resolve mapped drive letters (e.g. V:\\) to UNC paths (e.g. \\\\server\\share\\)."""
    if IS_WIN and len(path) >= 2 and path[1] == ':':
        try:
            _r = subprocess.run(['net', 'use', path[:2]], capture_output=True,
                                text=True, **POPEN_FLAGS)
            for _line in _r.stdout.splitlines():
                if 'Remote name' in _line or 'Remotename' in _line:
                    return _line.split()[-1] + path[2:]
        except: pass
    return path


def normalise_db_paths(conn):
    """Fix existing DB records that still use mapped drive letters instead of UNC paths."""
    if not IS_WIN: return
    fixed = 0
    for vid, path in conn.execute("SELECT id, path FROM videos").fetchall():
        unc = resolve_to_unc(path)
        if unc != path:
            conn.execute("UPDATE videos SET path=? WHERE id=?", (unc, vid))
            fixed += 1
    for pid, path in conn.execute("SELECT id, path FROM photos").fetchall():
        unc = resolve_to_unc(path)
        if unc != path:
            conn.execute("UPDATE photos SET path=? WHERE id=?", (unc, pid))
            fixed += 1
    if fixed:
        conn.commit()
        console.print(f"[cyan]Normalised {fixed} path(s) from mapped drives to UNC.[/cyan]")


def file_id(path):
    s = path.stat()
    return hashlib.md5(f"{path.name}{s.st_size}".encode()).hexdigest()


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json","-show_entries","format=duration",str(path)],
            capture_output=True, text=True, timeout=30, **POPEN_FLAGS)
        d = json.loads(r.stdout).get("format",{}).get("duration")
        return float(d) if d else None
    except: return None


def extract_frame(video_path, ts, out):
    for hwaccel in (["-hwaccel","cuda"], []):
        try:
            cmd = ["ffmpeg","-y"] + hwaccel + ["-ss",str(ts),"-i",str(video_path),"-vframes","1","-vf","scale=640:-2","-q:v","3",str(out)]
            subprocess.run(cmd, capture_output=True, timeout=30, check=True, **POPEN_FLAGS)
            return out.exists() and out.stat().st_size > 0
        except: continue
    return False


PROMPT = """You are an expert marine biologist and underwater naturalist specialising in Indonesian muck diving, with extensive field experience at Lembeh Strait (North Sulawesi) and Ambon Bay (Maluku). You are intimately familiar with the critters found at these sites and can identify them from partial views, unusual angles, and low visibility conditions. This footage was shot by an expert macro videographer with decades of experience at these sites.

Analyse this underwater image carefully. ACCURACY IS EVERYTHING. A wrong confident ID is far worse than an honest uncertain one.

CRITICAL IDENTIFICATION RULES:
- When in doubt, go broader — use genus or family level rather than guessing species
- NEVER force a species ID if the diagnostic features are not clearly visible
- Small cryptic animals (nudibranchs, shrimps, crabs, small fish) are extremely hard to ID — default to genus or family level unless key features are unambiguous
- Do NOT confuse taxonomic groups — nudibranchs are not fish, porcelain crabs are not shrimps, seahorses are not pipefish
- A thecocera nudibranch and a frogfish look completely different — if you are uncertain of the taxon, say so
- "possibly" or "cf." are your friends — use them freely
- If you can only see part of an animal, state that clearly in notes
- The "notes" field must describe what features you used to make the ID and what you could NOT see
- If multiple species are possible, list the most likely with "possibly" rather than guessing confidently
- All footage is from Lembeh Strait and Ambon Bay muck diving
- IMPORTANT: Always use a plain hyphen (-) not em dash or long dash in species names

FORMAT RULES:
- Confident ID: "common name - Scientific name" e.g. "leaf scorpionfish - Taenianotus triacanthus"
- Uncertain ID: "possibly leaf scorpionfish - possibly Taenianotus triacanthus"
- Genus only: "Chromodoris sp." or "possibly Chromodoris sp."
- Family only: "nudibranch - family Chromodorididae" or "unidentified shrimp"
- No common name: scientific name only e.g. "Tambja morosa"
- NO parenthetical qualifiers like "(pale morph)" in the species name — put in notes
- Keep species names clean and unambiguous

NUDIBRANCHS — common at Lembeh/Ambon (key: look for cerata, gills, body shape):
Chromodoris willani, Chromodoris lochi, Chromodoris annae, Chromodoris dianae, Hypselodoris apolegma, Hypselodoris bullocki, Hypselodoris maculosa, Nembrotha kubaryana, Nembrotha cristata, Nembrotha lineolata, Halgerda batangas, Halgerda tessellata, Miamira sinuata, Jorunna funebris, Phyllodesmium longicirrum, Phyllodesmium briareum, Tambja morosa, Tambja limaciformis, Bornella sp. (branched tree-like cerata near hydroids), Bornella anguilla, Flabellina sp., Cuthona sp., Trinchesia sp., Pteraeolidia ianthina (blue dragon), Glaucus atlanticus, Marionia sp., Trapania sp., Mexichromis multituberculata, Risbecia tryoni, Goniobranchus geometricus, Goniobranchus kuniei, Polycera sp., Thecocera sp. (Bugs Bunny nudi — distinctive ear-like appendages), Gymnodoris ceylonica, Gymnodoris sp., Ardeadoris sp., Doriprismatica atromarginata, Phylliroe bucephala (transparent swimming sea slug)

FROGFISH — key: lure (esca), wrist-like pectoral fins, gaping mouth:
painted frogfish - Antennarius pictus, striated frogfish - Antennarius striatus, warty frogfish - Antennarius maculatus, giant frogfish - Antennarius commerson, psychedelic frogfish - Histiophryne psychedelica, Histiophryne sp., Lophiocharon sp., Nudiantennarius subteres

SCORPIONFISH & RELATIVES:
leaf scorpionfish - Taenianotus triacanthus, weedy scorpionfish - Rhinopias frondosa, paddle-flap scorpionfish - Rhinopias eschmeyeri, Scorpaenopsis sp., devil scorpionfish - Scorpaenopsis diabolus, ambon scorpionfish - Pteroidichthys amboinensis, hairy scorpionfish - Scorpaenopsis oxycephala, stonefish - Synanceia verrucosa, cockatoo waspfish - Ablabys taenianotus

OCTOPUS & CEPHALOPODS:
mimic octopus - Thaumoctopus mimicus, wonderpus - Wunderpus photogenicus, blue-ringed octopus - Hapalochlaena sp., coconut octopus - Amphioctopus marginatus, long-armed octopus - Octopus sp., flamboyant cuttlefish - Metasepia pfefferi, broadclub cuttlefish - Sepia latimanus, reef squid - Sepioteuthis lessoniana, bobtail squid - Euprymna sp.

SEAHORSES & PIPEFISHES:
pygmy seahorse - Hippocampus bargibanti (on Muricella gorgonian), Denise's pygmy seahorse - Hippocampus denise, pontoh's pygmy seahorse - Hippocampus pontohi, satomi's pygmy seahorse - Hippocampus satomiae, thorny seahorse - Hippocampus histrix, pygmy pipefish - Kyonemichthys rumengani, robust ghost pipefish - Solenostomus cyanopterus, ornate ghost pipefish - Solenostomus paradoxus, halimeda ghost pipefish - Solenostomus halimeda, rough-snout ghost pipefish - Solenostomus paegnius, alligator pipefish - Syngnathoides biaculeatus, pipefish - Corythoichthys sp.

SHRIMPS — key: count legs, body shape, claws (porcelain crabs have 3 pairs of walking legs, NOT shrimps):
harlequin shrimp - Hymenocera picta (large flat paddle claws, walks sideways), marble shrimp - Saron marmoratus, emperor shrimp - Periclimenes imperator, coleman shrimp - Periclimenes colemani, wire coral shrimp - Pontonides unciger, Neopontonides sp., Dasycaris zanzibarica, Periclimenaeus sp., Gnathophyllum sp., Rhynchocinetes durbanensis, mantis shrimp - Odontodactylus scyllarus, Lysiosquillina sp., snapping shrimp - Alpheus sp., Synalpheus sp.

CRABS — key: porcelain crabs have fan-like mouthparts and 3 pairs of walking legs (not shrimps or harlequin shrimps):
boxer crab - Lybia tessellata (holds anemones), porcelain crab - Neopetrolisthes sp. (on anemones — NOT a shrimp), decorator crab - various, soft coral crab - Hoplophrys oatesi, hairy crab - Pilumnus sp., Zebrida adamsii (on fire urchins), shame-faced crab - Calappa sp.

OTHER INVERTEBRATES:
bobbit worm - Eunice aphroditois, sea moth - Eurypegasus draconis, mantis shrimp - Odontodactylus scyllarus, feather star - Crinoidea, nudibranch egg ribbon, flatworm - Pseudoceros sp., Pseudobiceros sp., polyclad flatworm

FISH:
mandarin fish - Synchiropus splendidus, lembeh sea dragon - Inimicus didactylus, bat fish - Platax sp., crocodile fish - Cymbacephalus beauforti, flying gurnard - Dactyloptena orientalis, stargazer - Uranoscopus sp., toadfish - Halophryne sp., frogface blenny, painted frogfish - see above

Return ONLY valid JSON with no markdown:
{
  "isUnderwater": true or false,
  "species": ["use possibly/cf. freely, plain hyphen not em dash, NO parenthetical qualifiers"],
  "habitat": "concise description e.g. black sand muck with hydroid colonies",
  "visibility": "poor|fair|good|excellent",
  "behaviours": ["specific behaviours observed"],
  "notes": "REQUIRED: describe what diagnostic features were visible, what could NOT be seen, and why you chose this ID or remained uncertain. Be honest about confidence level."
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
    if IS_WIN: os.startfile(out)


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
                def _decode_raw():
                    with rawpy.imread(str(src_path)) as raw:
                        return raw.postprocess(use_camera_wb=True, output_bps=8)
                with ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(_decode_raw)
                    try:
                        rgb = fut.result(timeout=120)
                    except Exception:
                        console.print(f"[yellow]  Timed out decoding {src_path.name} — skipping[/yellow]")
                        return False
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
        "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,?)",
        (photo_id, str(path), path.name, path.stat().st_size,
         datetime.now().isoformat(), str(out),
         json.dumps(normalise_species(result.get("species", []))),
         result.get("habitat", ""), result.get("visibility", ""),
         json.dumps(result.get("behaviours", [])), result.get("notes", ""),
         '', '', '', '', '', ''))
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
            "INSERT OR REPLACE INTO photos VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?,?)",
            (result.custom_id, item["path"], item["filename"], item["filesize"],
             datetime.now().isoformat(), item["thumb"],
             json.dumps(normalise_species(analysis.get("species", []))),
             analysis.get("habitat", ""), analysis.get("visibility", ""),
             json.dumps(analysis.get("behaviours", [])), analysis.get("notes", ""),
             '', '', '', '', '', ''))
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
    reanalyse_stop_flag = threading.Event()
    reanalyse_queue = queue.Queue()
    # Normalise any existing DB paths from mapped drives to UNC
    normalise_db_paths(conn)
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
<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,AAABAAEAEBAAAAAAIACgAgAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAQAAAAEAgGAAAAH/P/YQAAAmdJREFUeJylkz+IXWUQxX/zffPdt08kuyS6IAQX11eKNmaDhCgsCGqn1loJlvaCWFiriKKF2FgEEtOGGAUxCJtGSBRsNMLbTTaYmOz758t7e9+dGYsbI1pJnO4MnMPhzBwZjqfB/xiNCO5JIQIRQUsp5JQIQP4rF0giNGaoLeZs7+7+JUqEI/JvKWn3CAgkSZg1rK2tIVsXLsT6eo+UEh5B1b2PxWKBL2rCAwg8gqyFkEzT1HRyYjyZcG13hzSZTKiqiqJKWery9ltv8vW5M4gWVBOlKMvLy1zub/PxR++yt3cTLRWdqjAejUk5Z8wdswYk0d+5wskvTqOlIne6DKdz9s25vjfgpx8ucvmXnwlJmDmqipo5EYEg1HVNr9djc/MZhuMJp058DrZPVRRSZmPjCKPRiJxzG6WAqrYgAHPnSv9XpqPHOXX6JN9vnacp90PO3Lx+gxefe5qsioe3nAjSwYOHSJIJb+8qkphMp1y7usNSt8vLr7zGB+9/yKuvv8E3578jpUREG27RQtKiIK0Hd6cU5cDyASbTKUc3Njj+5GMkMfbHv9NbfwRVxc0oqhx68AFUBIiGMCNpob99lS/PfsWxZ1/i0/fewSXR7/e5dOlH6vltVg8/ygvPF+rbUJVCms9mWGMEjviC1Yce5tZgxuaxo6weXueTz07w7dZFek88xSyWEFFUwMwZDAbIbzduRadT3fm2YD6fk3KmKkrTNAyHI5qmYWVlBTOj0+lQVQUzp673kfEfs3D3uy+bc4IIzJ2UEqoKgJkhIrg77n4ncEH/Jrc1aZqmlRLBzP6B7zZDWrcRwZ+BTTQXjzqu9AAAAABJRU5ErkJggg==">
<title>idGuru — Underwater ID</title>
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
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAB0gAAAJACAYAAAAHGjWAAAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAAEAAElEQVR4nOz9aYxkWV4ffn/P3eLGnlst3VWdta9d09v0DAzMYBjgwcb2C5Bl/MgybyyMjLFAGMuyZMlYlizLaIQsY8sWAntYPPhBfxmZMf4PxgwzwBhm6ZnppfY1q7fqriUrl4i463le3DgnTty8kZFZmVmZEfH9lEKZFRlx4+5x7/md3+8ARERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERJsidnsGiIiIiGjnnDx5XDabTTSbTdTrdfi+D9u2Yds2ACAIAqyurmJpaQnvv/8+rl27wetDoqfo+PHj0rZtWJYFIQSklEjTFEmSII5jLCws8JgkIiIiIqJCH//4x2WlUsHMzAzm5uYwMzODRqMB3/fhOA6EEGi327h9+zYuX76Md999F5cvX+Y9BhEAZ7dngIiIiGjcHD06L5vNJmZmZuD7PizLguM48DxP36AIS+rXSyn7HkKsf6+SJEnfewHo9wghdKBF/W5ZFlzXheu62Xy4PuI41u9Rr7979y6klPL69Zu8WaIte/75c7LRaKBUKqFUKqFSqWQBesuCEFLvo2q/T9NU/66Yvytpmhb+zZye+j3/d8uykKTQx2QURZBSQgUoHcfBV7/6VQRBAMuyEMcxpJS4devOlo+Jn//5n5ff/d3fjXK5rD/LcRzYtr1mXs11kqapNNdN0fFv/nRdd935MN9vnjfyzw1i/r3o84e9X20/U9F2nlTD1t9OT3/YtniS7buXWJa17t+fZP0MO2fR6Njq8bfb23+nzx/Dlm+r549xt9vn962+f6vfH7t9fOz1+Zt0w76fd/r8s9vbf9j8WZbVd59k3kuoexbP81Aul1GpVFAqlXSHaACIogie56HdbmN5eRlRFOH27dvyd3/3d/G5z31usk/ONPEYICUiItpG8/Pzktk+k+306ZPymWee0b03K5UKbNuG4zgolUpwXRcSib6pyWeOmTc+RQEeYO0NpHq/+qmeMwMv6v+2baPdCnSAdHFxEYuLi2g2m/B9H4uLi6hUKjJNUwRBwIxSeiIvvvgRefjwYezbtw/lclnfsJdKJURhZ02A0NynzZv5QcdA/jlz/5dS6v1ddRBQDyEEUin0ezqdDsIwxLvvvot6vQ7btvHgwQO8++67sCwLURSh0+ngxIlj8saNW5s6Fs6fPy89z9NB2BdffBE/+IM/CNd19efng8QbWeaiBiSzUWdYgMpcv09iUDBoow1Lank3Mv1JNOoNeLs9f8NsZ4B0WLB0EjkOm5hGmdmBpsik799btdMBxlEPQO90B55h1z9xHK/7d9pZW91+kyDfodS8lxh2fWNK0xSWZeH06dO4efMmPve5z+3gXBPtfbx6JSIiyjlx4oSs1+vwPA8A0G630Wq1cOPG+oGiU6dOyWq1ipmZGbm0tISbN5mFN0nOnz8rDx48iEqlAsuydJaYClYIIXSgpForw3Ud+L4P3/f7MkuBXgOjmd1l/q72TXUzpAKwKiCkAlE6Y68bGM2CRDZsy9UlPL/61a/i//yf/4OVlRUIIRDHsX49G8Jos86fPytVSafp6Wm9/0kpEYYh0jRFteLr/V/to6rzgApkAsX7P9B/fJjHgOoQoKbleR48z4PruvoYsSwbENnvvu9DCIEPPvgA/+2//TekaYrl5WVUKhWEYagzXwFgamoKU1NTMuu9nQVW33zzzb5z/Pd93/fJBw8eIEkSNJtNhGEIIQQcx0Ecx5ienka1Wt1QluV6hh2XwxqQtnpcbybblGjccJ9fH68bRhsDEDTOhp2f2MFjb5v07xd137OR63wVRDWZ91dxHOvOqjMzMzs2z0Sjgmd/IiIiwwsvvCBrtRqCIECSJJBSwvM8VKtV7Nu3Tz569AhXrlwpbB27du2aOH/+vLRtG+VyGS+++KJcXl5moHRCHDhwAM899xzK5TLu378PIQTCMESSJHAcRwdF0jSFE1iQMtWZZCqYY3VLjybJ2rK5ZpAoSZK+nqL5IJFt2zpjr1qt6uCrlL3sLUtaOjAbxzFKpZIOZqVpqqd59Oi8vH2bWdE03IkTx+SRI0cwPT2ts0PVeVQFLy3LQhAEkDKFbdu6hC3QXx5a/T/fOSAfnCvKRDXLSmf7dRme58JxskCpW/IQdKLs9+5rHj16hFqtpoOrSZLov3c6nTVlbYuCJL7vI01T3XChSl3Zto0oivS6UNMwrVe2Nm8zPcSLrBfg2UjwdlCm65MoWsZRD0DtdgPeTmd4Tvr2GbT8T2u77/X1v9fnj9a30yUqR33/GPXz+06XkN7tEtVbLRG810369/eoz/9Wqfvz/D1DvgqNahMokiRZFSvV2dp1Xfi+DwA4c+aMDIIAt2/fnuwVTROJAVIiIiIAx44dk/v27QMABEGANI27F5dZBmAUBQCASsXHCy9ckK+//mbhhWOUhHBLFdjCQafTQXO68fQWgnaVZVnwSg5sR8D1bFg2ICwJywYsG4BIdTZpp9WGLSxYEHBtB65jwbEFbDsLkDp2rgSltHT2pxDdkrqW1MEk23LgOa4O7jSbU6hUKqhUKjp7TlgWAAuQCRKZIk0TdDoBKlUfjptNTyKBRKKz3rLZ3VowhiaHBRtHnjsK28l6N6+urkKmKWzHgUxTCAAyTRHH2b6bJCnSVCJNJQABIbJ9HOjd5OeDpgDguNnxYFtu3/OW1csMLZVKqFar8H0frluC41hwHA+OYwFCAF72GWkSwRISlXIJQaeDcrmss6iTJEEURTrQC1hIEokkidBo1PDqq69I13Xx4Ycf4vr1m+L69auYmpoCAARBG57nIEkiWJaLMOygVHIBpJCyuAxWfuzU3WgI2shnbud8jWNj115fpr0+fzttp5Z/0tcrjYedHsNyt201wLfXl2+vj3G629MfdVw/k63o3iEfMC2iz3tCIE4TCNuCgICERAqJRPaqXRFNKgZIiYiIANTrdV0CEgDSNNaZdKpEicrMcxwHL7z0Efn6t95YcxV57cp1cfb8Gek4Dqanp3H//v2nviy0u7Is0QRpKpCmtu7VmaYppNU/dlmSJDpomu1bVl8wSMpuoAS2ESDNpm87opctJ7KgUK1WQ7lcRrlcQalUMrJSbQD9vU2zfTuBCkap53lzRFuVlbLtD2qaY22a+6L6m1kq2rLWvs9sALDs7pi6Vi8r1XEceF7JyBr14ftZ+V41P5ZlAcJCFqTsBkiNcXx6x0Xa97yifrcsC3EcIwgCOI6DZrOJU6dOyOnpaZ0Vq15f9AAEjzUiIiIiItoR63UIkZC6LULdt8RxzOxRmlgMkBIRERVQ5RrVGA2qNKJq1C6Xy3j5oy/Jb37jW2suIi9fvCJe/uhLcmlpCQcPHsS1K9d3YxG27Lkj83Jubk6Pwbpw+w4vmIdQ+4gqgWvbqR7rU5XXzXp/Cv28KpejyuyaY3/qjLJcgBSQuZKiNiqVCprNJmq1GizLycr3qp6mBTdISZLosqHrBWoYxKHNUPtyfh9WBgUN1Zg6ruvCcey+6a0JkHaDryroqUpKZ6V0PT12aLlcged5veMgxwyImvObD5L2Xg/9+UqaZqWyq9UqLMtCp9NZcwybYwFlz68Nju522T7auHEvIUlEREREo2XdgGiuSo26J4njWCcIXL9+nRewNLEYICUiIgLw+uuvixdffFF6ntcNhiaF5R3VhaRMYjiOg49/58fkvXv3cOdW/xiN3/zGt8THvuNV+ejRo11Znu1w986COHjwoJyZmUG1WkW9XpdvvVFcWpigAyoq6GkGRlTmXNHYfaqcrQrsqOCLeZNjCacvQOo4tg6Q2nY2/qIab9QvlwEpsjKiXVLKLGdPCGQlcxM9vwD6PnPQfBINY54zzeNAdQwwszaLjpdeJqg7cLxR86cKqqrjp1zOSkqXy2W4rtvfSQAAVDDWUpnead9xkL2kOHir5gEA4jjSHRpU5YE4jvU4o4PG/clno+aPLx5vRERERES0VUUBU3U/pu59VEUcoknHACkREVHXo0ePMDU1BcdxdCAU6DVkqyBSmqYQ3UyhWq2GgwcP4s6thTXTW1pawqFDhwC89TQXY1s9ePAA+/fv78sIo2KqRI268cg/zKCpmRFnWRYc24NjezqDVAcs015gRgVIVRCpF0zydUlRz/MAWL0KptLK0t5kt7SoBCBSvX+rfbwoKEu0WfmgphkElTLLeu6+EkB/diWAXJnd/nLT5vGi/m/bjpE96nfHG3Xh+37vtQUlcs3/m2WuBwUoe8/35jsf9M0yX52++SsyKIOURge3HRERERGNmnzn0yRJEEXRLs4R0d7AACkREVHXwsKCWFhYwOnTp2WjUQOANYEtk+/7egy6Vz/+Ufn1r36jr9X0yqWrolQqye/53k/JgwcP4v/3278zcq2qN6/fEPv375dmyUgaLL+/5EvupnZ/yU7zJsWyLJ2VpoOsaT44pDJIHdi2uyYwVFhKNBe0AqBvhqIo6stkK9rGDAbQZuTL6ap9TQU/Vely9Xczi9PMCDUDpebYn2apatf1dOa172cdBRzHgaUzOEVfgFTPm+wFbs0AaX45Bu37vu/3Hetq2czAqanouMpnpxIRERHR6GIJftpL1gxx0u2gaVaKMpMCiCYZA6REREQ5V69eFadPn5SqwV2Vi8yXRyyVSuh0Ouh0OqhWq3jplRflt177dt+dz+rqKizLwtLSEr7v+79XfvH//PHI3RkFQQDbtvV4lVTMHBOxqMSuCsT0l/ME0lTqdWtmv8lu5me23/WXFS2VynDs3liLjuNk5XOlMeGim3ApkSTZWCOdTgdhGK7ZruaNE9FmmGOEquCfmaWp9rU0tfT/VSazGaw0y+aqcrX9Y+f0xitVx4DneXAcb01J3SJmZqs5xrT62/rZn4BtO3q8HvW8OYZPqVQaONYqM0iJiIiIiGg75e8t8p2yk1wnTkuwghSRwgApERFRgatXs0HqX3jhgvR9Xwe9gO6YkZ6L1dVVOI4D3/cRhiHK5TJeeOkj8vVvvaGvTm9cuyleefVlOTs7O7LlS8IwhOd5qNVqOHv+nJRS4sqly2zdz1HB0XyJXRVwMcc8BHrZcypAJKXU5UJVUCjVscteSV4hbPilSi+YWhAQWpOZ1n0+SRJEcaSzR6Mo0sHcPN4s0ZNQHQVUZiUAvY8lSaLPpSogqvZ/NX6neo0KkqqyvGYWqeO4OuO6VCrp8rZCCD3OqDk/qrOANMZELR4zeO3y9FcPyKYTBAHSNO2WtIZeBvXc2oBo/9im+dgoj7XRwgwRIiIiItrLiipECQhI9F/H8j6ECCiow0ZERETK66+/KR49egSgl1mnGvhd10WapgjDsK8k5PGTx/quMl/7+jfFu+++iw8++GBXlmGr3nrjTaGCCqVSCVEUYX5+nlfSOSr4YZYEBXrlbFUQKAgCdDohpMyySsMw7AUswwQCNkpeGZ7r63KkKvhjWQ7K5TIqlYoOzvRFdbrBIBUo6tN9vt1uIwxDtFotHaBS+zKAvn1bStn7HKINMMcUVVmV6tgIggCtVgthGOoApXp9p9NBu93WHQkcx9HZ0er3UqmkM0d9v4JKpQbfr8D1fAhrbb9PMziq/9/9GYahnrdOp6P383xwyxwv2Py/yqpX5bNVhwVzDFLzWFLfG+qYzk/zaTPPUWZWu+rIM+jvg0oSmyWH8wHo/HvWy67dqUd+XrfyMLdf0WMz8zKKipalaAwrM2tcvW+zzExtALos/Hrrcjf2r6JtrL7f1e9F+6F5rIzDvrEdzPWnzhtqP5JS9nVqGfT+p3UuKWIeB+bvahnUPgysPUaGrRcpe6UQi967HZVehi3nbp/fdvrzR3X58n83zy2mov+bx1TR93v+O7zo+Cs6x+0l+fVjXuuo87R5vfqk2z9/fWd+9kYe5jVT0RA/7XYbANDpdPqmX/S7+j+/X3oGnZ9brdaa1z7t64b8dlTbzryWVseZmne1z6rvBfO7xSybm98HpJRwbAepTHWnbKJJxwxSIiKiIa5duyEA4CMfeV7WarXsZiqOdKN4qVRCkiRYWVmBEAJzc3O4ef1W3zTeeuOi+M7v+g75//nLPyj/4P/93yOXXrK8vAwhBDzPw8zMDNI4QaPRkG+++ebILctOMW9kVTlcoD8oo25yoihCGIZwXVsHSMMw1Flo5niN2fTsbuNEljW33viI3Q/r/W7cOKnSulES6cBOUYld1VjgeT5ef/11bmPaFCnluo1EYRjqbGvXdddkk6qGOHUcANCBRSkB1y3pTin58T4B9LKq8w1C3fkJw0h3WsgHJ7YrWFkU/OwF0LblI7ZENbCocV4V13XXvLZoe5iNOWb5b6VoPartXNTpQk1LnT+3Qu1/g+x0QHqnpz+sofNpLZ+qjhAEge68oL5r8vuVavhVGeHrMfc3ta90Oh091nZevtFvq/vPdojjWJ+jpMyqQ6RpiiiK9HnLPGbUOYjZx9D7iXmuUVTnFFP+eNjtDidqH1XLEcexvv5T9w1K0XjVw+bfsizEcdx3PjaPmfz62qxh7xn2990OxOz0+XGnpz/s/YPGTM8bNBa6WeVGVfwwp63+ryrMZFU7nL5MNPUa9Z0OYGQ6U7bbbZTLZX0cqk6oAPT91Xo2s/2fpBPcsGCV53nodDp9+4Hq6Jqfx6H3ihNIdVqMogilUkk/X6lU1rx2N9ddUWdNdR2lOooCvePOtm10Oh3dqVTdYwH93w9purYTDfcTogwDpERERBv0xhtvCQD42Mc+Kh3H0Q36vSCWpW82X3n1Zfna17/Zd7X59ttv4/nnn9/UZ545c0ZaloVLly7t6pXrjWvXxdHjx2Sj0YDv++iE7T3RCLlXmQFSAGsaQYMggOu6sO1sf1lZWUW5XEGttoparYZGowHP8yCEpRvVsveK7vsGXMKJtQ0ngESSRjqbL4oCBFGgG+1UoNS8kXccB61WC57n48iRI/LOnTu8c6JNy2fV9ToRJPoYcRxHZ+F3Oh20Wi1Uq5U1DXOqMVgIC47jwRIObMsGYGVj70oJiPUbrmQa6+PPLDGtSlwXzXd+eTa63EXT6k1zdxuQVdl0s0HTDN7kg1jm+cwMfucb81WA2/O8vuxa1dlDPdR0Bj222kC/2ddtt1FvoC9qgDep/afT6aBer+vsaHW8mmXkzWujjc6XurbIOhH1dwhaWlqC6qhmfkY+YL+e7dq/BlGZ42oZ4jhGpVJBEAQol8t9r813zGCQFPr80+l0UCqVdOP/ysqK3vZmdg2wufU2bP8YdnwN+3u+AbvT6aBWq8G2bbRaLVQqFR1UGnStOGz+VSl61bmuWq32dcAroqa92eNjs/vjsPPHbtvp8+8wW93/lPz1lVquIAj6Oo8VdVgzgy35790gCAZ2RgF6+7f53a5EUTTwfXuFOgenaYp2u41qtQrLsvD48WM0m00EQbDu+zd7/ZFf9xvZvvnvN/M5FdRTx78KhqlOOebnmNteLfOk37ur9aT20yAIIKWE7/tYXFxEo9EAMHg77fT1l1ktoegzVae01dVVuK6LcrmMdruNer0O3/f7qkEBG/+OmfTrDiKAAVIiIqJN+9rXviHOPX9Wj02qytyom5ZWq4VSqYQTp47LG9du6ivOtxfeEaVSaVN31leuXNkzV6y3b94SAPDckXm5b3YOtVoNR44ckbZt4+bNm3tmPneLWQJn0M2IylazulmalpX9fWVlBZ7noVLJyudOT093y4vauUYQsbGbW3Uz1W0Eycr7BgjDQAdKVaaPGSBVwdgkSTA1NYWlpRVMTU3hzp0727quaHLkG4XUT5UBozKogyBAu93G6uoq6vWaDuCrhrhesDT7aQmnP1O694GQxnjRSpokiOOor+S1WcosH8jJNzw+SaPobmfSbEQURTr4bPamVw3w6rxgrpswDPu2DZBVGfjwww+xuLiIixcv4vHjx7h37x4++OADLC4uYnV1FZ1OB3Ec6wYpoL+MmPr/MJsJcO1Eo89WAyg7nWG01f1uWJlO9Z1SKpXQbrchpYTruvhn/+yf4W/8jb/Rl11kjtW70TJuRQF8lR3xL//lv8Sf/dmf6fkYVJ5uPTvdQKw6CbRaLZTLZd2Z7sSJE/iN3/gNlMtlfb1gZi9RRp2Pfd8HkG3nN954Az/zMz+D1dVVOI6T63DTH2QeFqDbjgx1U1GHGCA7t6rvrn/8j/8xfvRHf1RnKeWDWpupYNBut3UnlF/5lV/BZz/7Wd3pJx+wKprPYZ+x3nG6kfkbtv53O0C504Yt/7Dz4LDlM4NgRYFPda3kui48z0OpVEKlUkGtVkO5XMazzz6L5557DqdPn8bhw4dRqVR0tpk5BIBZxtN1XX3eV51gzAxU9fl7PTgK9O55LMtCtVqFlBJ//Md/jF/4hV/A4uLi0PPxZrZf0fYZtn/kA2P5IKmqVpUkCer1Ov7pP/2n+NEf/dHCjg35fWmvHztPgzkEiDo+pJR477338HM/93O4devWmvc8zfVmlsUt+mzVoVTds6jri5/6qZ/CT/7kT+pjMN9BbVgHRO4bRAyQEhERPZFLb10Wx08ek1NTU7qElmrQtywLQRBgdnYWQgh5/eoNfTVqBkxH1d07C+LunQU8//zzstFoII5jzM/Py4WFhZFftq0wy4SqHtaKvjkxxhLpja1mwXVb8DwPy8vLqNfraLfbqFQqEJYFISWAXqkcYVmALG7Almm3kc3KAkJCCB0c7XTaCIIAQdBBEAT6Jkztt+aNu1qWmZkZ/Nmf/dlEb1favEE34eoGPElSvd+pTE7bzspNZ2P0dvrKu5klp4WwIGAD6C8jnf20us9ngf5eADVFmsb6s8zgqNmpIXtL8fig2TG8uXVg/lS/74VGCBV4Uo0nZlZnFEX67+qcoxoMVXDzxo0buHPnDl5//XW88cYbuHnzJr72ta/xPDHh3nrrLfn93//9WFlZwdzc3JpsSTOjYT2qKocZaFKluL/whS/gjTfeGMl9bWlpSd64cQONRkM/zCwvdX6qVqu7PKe7S2VDAb0x4jqdDr74xS+O5HYHgKtXr8r79+9jZmZGf7+Y14ibCZD6vq+v7a5evYqvfvWrI7teaPcdPXpUXrhwAd/xHd+BM2fO4OWXX8bMzAymp6f7OqkIIQaWx+90On1Z03uZbdtot9t9Wbb37t3Dl7/85ZE8jr7yla/IF154ATMzM5iamtLnlnwGMcAsQQB960URQuDBgwf47d/+7ZFdQZcvX5YrKysIwxDNZlN3MFJUB2iroET9Xrk3IdptDJASERE9oZvXb4nD84fkzMwMSqVSLzuwO26WEAL79+9HEATy7p23R/aie5C33npLfPzjH5dBEEx8yR4AOqNBZahlvbjdvl68Uvayb8Iw1M+7btawsLy8jOXlZbTb7V4JNrtbRtS0bomcXu/kbF5ChGGgA0+dTgftThtRFOl5Ue81s+Vs22ZwlJ5YUYBQUft2mqY6c9G2bZ1J2ul0EIZhYYlWQPRXqFWx0XwjkHGMyG4Z3TAMdcaq6tRiZpCagVE1na1kkO5lKktPNdKrUm1mA2gcx1hZWcGbb76JP/qjP8If/dEf4Utf+hLPCVToF37hF8Rrr70m//pf/+v4ju/4Dly4cKEvqy9/fA1ijmWqAkFqvPeZmZkdXYaddPPmTfE3/+bflJ/+9Kfx6quv4pOf/CSOHj2qM8LyWbOTysywUh0OHz9+vItztHWvvfYavvzlL+PIkSOYnZ1Fo9FAs9nUmXnAxjNb4zjG/fv3cfXqVVy9enUnZ5smwO3bt8Xt27fx+c9/vu/5v/bX/pr89Kc/je/93u/FmTNnUKlU9D2DKs2bDflhrwnG7GU6UNTNtAVGO3vuC1/4Amq1Gj71qU/hwoULsCwLtVpNZwabGCDtdfgzr30ty0Kz2dzN2dqyf//v/71YWlqSf/kv/2V85CMfwalTp+B5nr6/UUHzFBIC/fuBOh6IJh0DpERERFvw9sI74u2Fd3Du+bOyXC7rLLwkSbC6uopKpYJulqm8deP22N2ZqGDDsJJ8kyAMQ7RaLT0mlCqBo2S/ZzchMhWIoxRJ0uneuGQNx66bBSimp6dRq9VQKvndG9puqcvuarbsbqabvqFJ1Yfom6F2u6UzUlZXV9HurKLdbqPVaiEIY73NzPGqVDDI931dOproSRX1TDYDJlJKnTWqGqtU2c52u63PLa7rGmWich+SH9JTSsAS+vesk0DQzZ4OdBBWBWBV1ne2768tsfukDUpFPfZ709tbjXGdTkc3cKryjX/8x3+M3/3d38Uf/dEf7foY2DQ6Hj16hMuXL+P48eM6CK+C75s9llRDZrlcxv3797G8vIxHjx7txGw/NZcuXRKHDx+Whw4dQqvVQhzHSNO0W3Lfguu6I5GFtZPMkp1CCLz//vu4fPnyLs/V1ty5cwdf+9rXcP/+fZw4cQLPPfccyuVyX6nEjVLH0uPHj9FqtXZwrmmSff7znxcqaHr+/Hn5Qz/0Q/jUpz6F7//+70e9Xl9THhrodbzayyzL6hursdVqGRV9Rs/ly5fFtWvX5NTUFMIwxIEDB3DgwAG4rgvf9wuvvyedCvKbY5GOwz3vrVu38M1vfhONRgNzc9lQSEIIlEqlwoxiALqKDEv9EzFASkREtClHjjwnK5UKLl3qHxv00luXxcnTJ6QqmZamKRqNhh5/a9++fbh14/YuzfXOieMYvu/D8zy8+uqr8utf/7o4d+6cnMQG9dXVVSw9XoHtiL6sNMBoQBApBGxIkSJNBWQ34mlZ0D08S6USPvjgQ9TrDZRKfvemJmswTGKJJI36xgzK/p59lurV3elkYznq8R3bbbTDNsJ2iJVWK8t2hSp32h/cVhk7o9QjnPYQUTy+kjkWjm27yMre9mdc27adBfCDUGd7qn26bzpCAPkzjEh12FEgK0OtskSzaQW6w8CgMUiHtR1lAdrhnUGKgqL9AdfdD5Ca86gaRi5fvozf/u3fxr/4F/9i4s7ftD2SJNFj3QHQ412rRnNVZWM9aZrqfVKV1221Wnj//ffHojOWKiGsgqOe52XjKjODA0Cv3DCQlQNvt9v44IMPdnmutiZJErRaLSwtLWFlZQVpmsJ13TWdcNQYj8OkaYqHDx+OfGYtjYaLFy+Kixcv4pd+6Zdw8uRJ+SM/8iP40R/9URw/fhzlclkHYkahc4cK4prfJaMeHOp0OlhYWEC1WoVlWSiXy5iZmdHluKlHDYFjXmOMSnnoYdSY8GoYn06noyvFqA6ptutkhXhUWXcIBkiJungUEBERbUIigTiV+OjHXpEffvghFm7f1Xce16/eEEeOzctmswnHcbC6uopyuawb31965UX5rde+PVZ3Kp1OC/V6Fe12lglx/PjRiQyOAsCVK9dEp9ORBw7ug5QS5XJZN3imaaKDQ2kaI00kLEvqYHoQRAAs2HYLcfweHKeEIIhw//7DbCzSbiNa/3iMQjes2sgyl1WDqyqlmwWbAiRxjEQmSKIEURwj6s5PkiSIgghIJYQEPKcEIbPpj0NDND1dKRKkSABLIkmzn5YjkEqZhQSFjTiJ9JihluVASqH3f9dNjH23jXa7g04ngG07SFPZCyLIOCuha2edAyBUIqnszYlU5aVDxGGIKGgjCnrldQGJIOh0s63bcBynWyY9hrAkhLU2iKmCo47jwHEtrKysoFQq6WPFtlzEcayPa3U8VqtVPHz4EJblIE37M6SU7W7EUoGoKIr6xrtTwWiztOOlS5fwmc98Bp/97Gcn8txN20d1elDHgZQSlUoFcRz3jbm4HvXdpBrc1Xea+jnqVABYDcegSo1vNDg27pIk0RUFwjBEqVRCpVLZ7dnakmq1imeeeQaHDh3Cvn37+jLYgF5Wz3rHiJmxJ4TA1NTUyK8XGj3Xr18Xv/iLv4hf/MVfxI//+I/Lv//3/z5OnToFIQRmZmb6zmMqM179Xx3bwNprHrMU6E5Qn+d5ni4PrH6OcgYpkC1Ts9lErVZDvV7X35vqOjCfNTjJ1P20Wje2bWcdkMfgnrdUKumMUdVpwfd9nS3rOA5sKwuWOpYNgeyYFBKYmZoGABw/flzevHmTOwtNJF6BExERbcLbC72A6Ksf/6i0bbuvdO6dWwsCAC688LycmZnB4uKiDmKFYYiPfuwV+Y2vvdZ34Xnm3FlZq9Xwja99feQuSNWFt7r5mvQMiDt37grHcWStXtFlO4H+hq00TSCE3ZfxqTLdVLZMpxPivffeQ7PZRLlc1mOTqYwDINVjspVKJTjC0tNTPUejKEKUy8CTUiJVDRC2pRuzi0owjUNDND1lA7JH+1mQMhtf0GwgFkIgCALYtt0bK7fdRqfTgWVZutHNsiw4otuo4WTjkopuSV11Ak3S7FgKwjaCIEDczUhNoghJEkMmKZJu4xGwdvypQeV11f9XW8u6V7YqAWw2sLXbbZTLZdTrdbTb2Ty4rvvUgh9qPOTV1VXU63UAvcwkx3HQ6XTgOA5ee+01fOYzn8HnPve5kfvuodGz0bF88+MBq4yYSqWy58s3bgZLHxZTDfrqd9d1R367N5tNHSBVQyiYGUtFJdkHUePV1mo1VKvVHZtnomF+/dd/Xfz6r/86fvzHf1z+yI/8CI4dO4YzZ87oDmfqniXrCBqgXC7rYzs/9vtu3T9uZSiFvaJWq2F2dhYHDx5Es9lEtVqF53l9wej8/SiNp0qlgmq1imq1Ciml7hBgHmNmwDw/Hu9OdVAgGgUMkBIRET2hr3/1G+Lo8SPy3PNn5aW3Lvfdcbz5+lvi+Mljcnp6Wge+VEmfj7x4Qb7x7Tf1669cujyydyuWZekABpBdWM/PH5YLC2+P7DJt1Y0bt8TZcyelyh4zx15TgYuSW9K9e1UjgSo1KoTAysoKHjx40M0UteF5Hkqlkr5xcd3sOd/34fs+Sk6vEaIo6GMZ2acQIpsnx0aapoiiCGmarmkkGIdyQ/R05ccbHfZaoNdIBgCOY8GyoMfKVT9V2WfL6t7Ii6wclOuXskClZUN0w6MSUpfVDYIAnU4HcZCV+UyiGIlMEccJ4m7nAHNeVENB8bihvdep8bfUsarKV6lMMCALSKoMBZVFYQaEd5LqGV+v1/VnWpaFhw8f6nn/J//kn+Df/Jt/M7Hnadrb1PGlGvJqtdrYlH7Pl9xmw3U/81yphh0Y9e1er9fx7LPP4vDhw6hWq6hUKvq7YrPZXSrrrV6vo1ar7dQsE22YCpT+9E//tPyJn/gJlEolzM7O6go45XIZ5XK5L7t0ox1maLh6vY59+/bhmWeeQa1WQ6VS4T3cAOP+PavKBavqFKptAFgbGAXWjkHqui7m5+flwsLCeK8oogIMkBIREW3B7Zt3xJFj8/L02VPy6uVrfReTN6/fEh95sSrVTYrKkJqensZHXnxBvvHt10f64vPcuTOyUqkgSRJ4nodHjx7p8bQmnRn4zI9DqhoF1MMMwJgBFDVGItC7gVE8L8sEUw2Hvts/hpnqOWzbdhYY7ZZus20blm1n2W5JL+vUvGkyGyw++cnvkn/6p18Z6f2Unh5zv+57YG2GpvlTBRfNTOooinqZ0FHUHcM0O1bCtFsmzeru1263oRlSZ0WrR36s0VR2/959nZmtlA9YDNJqtfT71Lw3m00sPV6B67ool8t4+PChDpiqEl6qA8RONwqqLFzVGKlKN87MzOC///f/jp//+Z8HS2jRThrUCLnRxknzO9G2bZjXGqNOfS+rn2vGKidNlVkulUq7PStbogKaqvylbdu68Xqz1Hjevu+PxfFA4+OXf/mXxe///u/LH/7hH8b3fd/34YUXXsDJkyeRpimWl5dRq9X6OmSa+//T7ChSlEE3yjzP02PBlkolOI7Td48JjH9gcLPGdb2oztlF+/SgcsuqA/X8/LzUnamJJhADpERERFukyuoeO3FURlGEtxfe0Veeb3z7TXHqzEnZbDbRarV0w3mlUsP5C8/Li2++NbJX5mmaotVqIUkSBEGAixdHNxN2u6ksNBUMyZcSVQEf8zlTFEX6eTWNbNzEjBrfrdMJ0W4HugRvyXG75UhT3YtUBWdsW8K1AcsBhJCQ6eBAbRiGmJqawsOHD3dyNdEYWq+Rv+hvqmS0EEYZ6FyQM00TSGkh7e6zIs3eZ8duX5k2c/xDFRA19/E0TZGk/c/nj02zBFW+g4P6vVTKMlfb7TYqlQpWV1chpcRrr31LAMDLL78o1XRc19Xj/zytRocoivoyroIgQKvVws/+7M/iP//n/8zzNO0os+Ex/92ymfcDvXOG6ngwDg13qtR10VjEtNY4BDBUoNfs7Pak4wKq40l1viHaS27evCl++Zd/GcvLy3J2dhazs7Mol8toNpsAoO9lzPskYPcCVaNeYnd+fl4C/RmBQP816ygv39Mw6vtAXn5fyFfFMZlVfBYWFsSFCxfYU4smFgOkRERE20QIgaNHj+LthXf6nr925bo4duKobDabcBwHvu/r3t+j6syZU7JWqxVefFPGXCf5HtMSa2/Iinr6qgwTADrgkw+amsEk6WaBmCiKshKk3XEPVfkcaaewUhvCERCpWPOZ6vdKpYIHDx48jdVEY2S9Ert9WVK5jE0hrL7jI19+ek2QUhYHW/OZqEUB0nxwVJVzLAqEmvNtarfbSJIEq6urKJVKsCwLMzMz+u/f/Oa3xauvviJVySrVIUKNWbrTjf2u66LdbuuxUd9991382I/9GF577TWeqOmpetJrAzOwqo5T1dln1O32mHujIF9dYNSza/sqehiPosodw/Zxte+4rssymrRnffaznxVf/epX5Q//8A/j7NmzOHbsGF566SXMzs72vW43g3i7HZzdLup+MIoinW0/KEt31JeV1pf/Xlmvas1mhkUhmgS8KiciItomN6/fEsvLy/iuT35izdXmrRu3RRAEePDgARzHycoxLi1h/uiRkbsyPTz/nHRdV49fqQIR1FPUoJf/v9lgpqiGL8/zdGNwUc9W23JhCQeQFmQqEEcpojBBJ4zRDiJEUYIwjBEEEcIwRhQliOMUUdLbZmYQSc2P+VmlUgmdTgfz84dHbh+l3bORm+3efi1hWVjTYNwLVqYA1nYcyB8T+b+b4/oWBVvNRveiv+Wnnf/cZrOZjf1bKumApwqGKr7v950HnlZ5XSDrTFEulwEAX/7yl/FDP/RDDI7SSFHHiZkNMy6l34qy14HRb6TfLvmS/+o8PsqKKhIU/X2j+4BlWbqSAdFedenSJfGZz3xGfP7zn8eHH36ITqeDIAgQhmHf8B5mBZCnbdSzBxcWFkQURQiCAO12G2EYIo5jBr4GGOVtvVHqvkYdT/n7njwzqDrqxwPRVjCDlIiIaBtVKhXEcYzv/K7vkPfv38f1qzf0Vealt/pL0P7Y//dvycuXL2Ph9p2nP6NbMDc3B6sbtDCzGqnHLO8JrC0ZmKb945GaZUJVyTR1g6PHDs0FMBXVcBhFkc60cbrjtanPMN9nWVmAFUgKA01CCLRaLczNzWF1dXXkGybp6RmWeamofVkFR9Vz+QCIecPel61uTFoIAYHigCaANcdhNp20bx7VMaga6cxjxpyeen2r1QIAPY6cqgpg8n0fS0tLqFar8H0frVZLl1nc6WNKnZv/w3/4D/iZn/kZnpzpqdvqNYH6LlPfkU9Sqnevyl8f0PrUtdAoU2Xf1bjQqnKBWSJ3o/u2OjaYQUqj4uLFi/jqV7+K/fv3Y2VlBeVyGfV6HbVaTe/DlmU9tQDpuJ17VYA0CAJ0Oh14nqeHdlivvOokG/d1Ye7jZofr/L5vVrQ4cuQIxyClicYAKRER0Tb64IMPMDc3ByEEZmdnkZ5M5c3rtwqvwh8/foxnnnkG3/7mt57yXD655z9yQaZpCsfpBfFc1x37G43NMoOZWUNYf+DFtrObj3z2phLHsQ6WqqCNIqWEFBISEhAAcgEcKSWCKNINb/m/2bZAKgDXzf4ehiE8z+uOC5TNo+M4aLVacBwHly9f5calDTFLP5sdKBy3f39X2aNAfteS+pFlj+ayRi0JIWxImersZyEEUtnrpKGyEopKR6VpijjJxr8yM9TMIIzKWMoHe815t20bUkqEYQjHcQrL5n7wwQeYmppCq9XSx5ia9nacL6Mo0g2Lq6urqFQqALLjWUqJ3/md32FwlHaFOvbNzCBlI1lyUkqdka0yR8epI5aZ1a4CpeqcQsVU2chRJaVEFEW6BGa+EXozDdKWZSEMQ5RKJQZIaSRcu3ZN/NIv/RLefPNNeeHCBbz66qs4d+4cXn755b7XPa1zoNkxQXVyG2VJkiAMQwRB0FcZKF9qlfrLzo5z8NgMhqpguZLvcGZ2QuN1CE0yBkiJiIi2kSq1GIYhXNfF1NTUwNcuLy+PXK94z/O6F9BZY2WpVNJBUurxPA+WZa0padMbQ3Ht2K3DxqDqe+2A16jp6GzSgtelKeDaQJJANzz3ArRiYKYq0UYMyvbK9m8zazTte43ZoSA/rT4iHXoTn993i/blQUHUotcVTd/smS+EwNLSkv77D//wX5aLi4t49OgRqtUqKpUK7t+/j3a7rbNOt0IFR5MkgW3bqFarWFpaQqPRgBACv/M7v4Mf//Ef5wFMtAfxu3VzxuF6JF+NYKvUEA1moIdor/vf//t/i9XVVVmr1fDss8+i3W7r6z5130SbZ14/F1U/YaB0cjDASfTkGCAlIiLaRqrMjQo6+b6PF19+QX77m6+vuSP5sz/505G7S9E3X7KXBdFqtXhB3nX8+FE5NTUFx7X02FBFZXaVQTerw5/rH8dKCgEIkWWVQiBOYkjYgEiRihQ2bKQihZRZQEWGUmfmmMFb3jfTdjCD7mbDjGUJWJZ6ThhZNFI/X9iILNLsgbVjkappm1mgfW8VQnco6E17eFB0veCpKmEWRREcx0Ecx5idnQUA/PW//ldlEASwbRuu6+qM2o0EYzfKzByt1WqwLAvVahWPHj3C3bt38Xf+zt/hkUy0h213wGxcFXWcGUVqGbZrOVRAiZ0TadR85StfEadPn5ZF47LzfPhkVGUTNRxLvioRwMAZDTZsjGyiScEAKRER0Ta6ffOOkFLKO7cWBAAcnj8kx+mGL47jLOswzjKY4jhGpVJhIw2ACxcuyH37ZlGv17HaWtZl1PKNe2maIh+J3EjG23rPK/lM1TRNdXAGAGwna2xMwkQHSE0qo9ScFtFGmRnTRcH/7FjozxjNZ5Dmp2d2MCgqi2W+Ni8fSO1NY20G52Z61juOA9d1dcmqlZUVPHz4UP9+584d7Nu3D+VyGZ1OBypgWiqVtqXEbqfTge/7aDQaALJy7c1mE77v42//7b+9pWkT0c560u/3STUOY6Jtd6BXVathBimNojt37uD27duYn59HpVKB7/uYmprSw4rQ5qhziwqQFl1Pc71OjkEZxBvBe3+aZAyQEhERbTMVHAWAtxfeGas7EnXhXC6X0W638e1vvzFWy7cV09PTOHDgACqVCtIP474x1JSibNJhNy6FfxdqfBnzSalHcLQtC1ImSJIUaRojTW1kJU0lXMsFkt4Ntep5nB+HMT/vRMOst9/0lf1Ctu+qB9CfPaozQ5HofX0r8/Mk828+ihqagiDQGaJTU1MIggB/82/+DXnlyhVMTU1BSol2u60rCbRaLQDYlhK7vu/rMu5pmqLZbCKOY/z0T/803nzzTZ6Tifaw/LmO37P98pUBxiFACmxfqeBxWy80eb74xS+Kcrks7969i+PHj+PYsWM4deoUDh48uNuzNpKKSuwSbWQ/yN/vbGe1G6JRwysqIiIi2jBVwieKIjbM5Liuq7Np1fiEZkZdPrOuKHsuf2P7pDe5ZgZpkiSIoghhGOqfSZLohwrW5Muhqvmcnz/MOyXakHxw3dy3+htu+ve5fKOOud8PK1E9KMu0qGPCoPcOe21eqVSClBJBEGB1dRWLi4u4f/8+VlZWEIahHqsZ6J0zzVK720UIgTiO0Wq18IUvfAG/9mu/xlYxoj1uK9kdk4iBQKLx8/u///vit37rt/CHf/iHuHTpEh49esTjfAvW+x5hwGvyDGpLKLq/Ur8vLCzwYoQmGr+BiIiIaEM+8uIL0nEcSClZBmkANaanCvgUZaRtd6mbfHB1UEaKCpQGQYAwDHXAVAVJi8ZuVCWbiDZq0D5vjomUNXj3MmDMfdeyLAhLAiJdE7w0P6Nof1U/t9oTetj72u02AKBSqWBubg7VahX1eh1XrlzB3NwclpeXkaYp1PlSHWdSSiRJsun5yWu1WvA8D0EQoFQqwbZt/NRP/dSWp0tEO48ZPhuT7/gyyrYzK2fU1wWRcvPmTfHee+9hdXUVYRju9uyMNGYBUt6TfFdw36FJxhK7REREtCHVahVxHCOOY9iuw0aaAio4o3pB94JCKmsuCwDJpD8rbr2bkcKgaioBS0DIbNJCArL7E8jK7Jqfr6ZvBkPNm6AkSXRw11wWZm7QZpn7az77UwgJKQFhiW553d6YbBvJqlpvjFJzGoOOpyctuZvnOI7ubLCysgIpJTqdDvbt24eVlRXUajXEcYwwDOH7PizL0gFSx3G2XGK3Uqno8aAB4Cd+4ifY85toxPAaarCi8/o4YJCUqJ9t2/q6KE1T3nM8oaJqKAx00UZwPyHKMEBKREREG5KmMeI4hOe5iMIEtVptt2dpTxGiV0Y36ERZmV1HD7IICAEBG5ACQvQHPfNjNw7L2hQiu4Sz0I2Vyiz8apn3OBIQaTfY1B3jERJIBZCmEeI0gRVHKJfLELaFOE1guw6kyDLf2u02fN/H0tLStqwfGn8CNizhQKYCEJYuNSuRIJUpXCcbs1MKAdtxIJHtt46T7e8pssC/JRxAWn2Zp30BfUhYtqUzMktWCQICqUzXBGgHZW+r4L9Z/toM1hbR85ACkBbiKAWkhSSOESahPu7jONaNfkDWAcG2bXieA6DXCJg/BxQFgPW6zf1NZaj+6Z/+KX7jN36DreW0JwghkKap/g5L0xSu6yKOYwBg4zeycvxF3/EMemXMYQjSNIUQAp7n7fZsbYm5bfPDLZg2WmUkiiJW96Cx8ODBg77AqDrmNzrUwmapY0x9ZpqmiKJoy9PdTeY5JV8RiN8r/Qatj3FZT8NKLec7oqp9RXXcLKooRTQpeIdCREREG6Ia/dVNbKfT2eU52ls6nQ46nY5uCFY3qvqRQP9eVAapqDTpoIcNAbsb9Mz/3MhYplJAl/4MggBRFOlgk5pHNdYs0UYNGlN0o38ryiDdbKPFRm7sBzUebbRRwGyIUln1URTpn8OO361aXV2F53mwLAv/6l/9qy1Pj4horxqHDFIGLIiKvfbaa+Lhw4cAss5k6ppK3Y8Aw6uDTLpB19A81xARbRwDpERERDTU2fNnZJIkCMMQjx8/hm3behw+yvi+j3K5jFqtpoPIRSWP1A1/0RiKZsnR9aRi/UcC2feI0kQ/kiTRN81mYwQAPS5pGIZwXReWZaHRaODMmVNslaChzH3YPAbyfx/2KAqibqdBx17R3wa9X3UoUEHRmzdvCxUk7esYYfTG3mzj3qDlL5fLAICvf/3r+F//63+xBYyIaI8zs0bHPYuJaDOuXbuG1dVVLC4uotVqIYqiwqEIGCBd33Zm2hIRTRqW2CUiIqKhVLBMNczHYYLV1dVdnqu9JUkSRFEEIQSeffZZACgM9EgpYefGXczbzpvbfIAWAOI0gmVZOhBaqVTgeR6WlpZ09qjjOFheXu7rxU20HjM4Oigzc1BQdNDfzGkUvc60qQzQAdPZbBZpvtOD+r9aF0UZ4ptRdC5QY5r+23/7bzc1LSLafWy8Xt+g6gKjzAxcjPqyEG23b3/727h27RoAYHZ2FjMzM5ienmZJ9k0oqkhElJcv4/6k1XqIxhEDpERERDSUbdtI0xSPHz+GZVnotALcunWLV9MGVVqzWq0CyIIYtm0XZqdZQ0ogDWsU2GrZ0SgJ4TgOwjDUY/CsrKzg3Xfex6OHj/U8qDEkk4Q32zScEKJvnx80hpQZQDV/t4UFC/1/N6djSUB2H9aAXXJQw5DYxl04P2/qeDUzZ3eCWrZOp4P33nsPv/mbv8lzMNEIKvoOZ6N2sXEIKq5X/nKj444SjavLly+LP/zDP5T37t3DqVOncOrUKdTrdXiex84FG2AO6TJoKAdml5IyKEhKNOkYICUiIqKBjhybl9PT03AcB51OB5feusyr6AHu3buHarWKcrmMcrkMy7LgOA5s2waQuznNZcMpOoC6xSDLsHKhllOCbdv6761WC61WC4uLixBCwHVdhGGIJEm6N9vc7DRcvjzuepmiKrvSDDAWvX8jn7keSwKpcRgI2W1oQ3FW50YbCvLlhI8enZdm9qy5rNsR+DDntVQq4bd+67e2PE0i2j1slNyYcQiMmN8V+QoLRAT81//6X8Vf/at/VQZBgEqlgkOHDqFSqegKHarzHa2lgqOq4o9Z2QRg5xsioo1ggJSIiIj6HD95TM7NzekbLc/zsLq6Ct/3d3vW9rTr12+K69dv4vz5s/LgwYPwfR++78NxHH3zqjLsZPf3vI328H3Sm1013SDq6BK7lmVhdaWNKIrw4MEDWJaFer2Od999F3fu3GVrBG1YfhxRM4vaHHttTQYm1mZkCiFgQ8DqZotaEoDo7sO5BnPR/Zs+fmT2yGeZqm4HqpOCspGx4YqWUwV5VRa2/pzu/G93Q7havgcPHuC//Jf/sm3TJaKnp6g0OBv+i41LiV3z+5CIil25cgWHDh3C4uKirsoDMMA3jBkcNYOk/G6hvEH7BPcTIgZIiYiIyHDm3GnZbDYRxzHiOO67YK7Vars4Z6Pj4sXL4uLFyzhx4pisVqtwHEffsOoGstzNfr4RYNiNymYCqEVldCQAYUnEcYxSqYROO9TZrnNzc/jggw8YHKUnUhQkFULo/d98nfmzaDo7OY/YQvaoem1RGWFz/NGdKmH15S9/GTdv3uTxSUQ0IoqqKRBRz/Xr18V3f/d3yziO14w7rwJ+HJd0LbVuzOzRQaV2iYioGAOkREREBAB46ZUXpWVZSJIEQgj4vo+v/vnX2IrzhGZm5uB5Hh49egQVKA2CAKVSCXEYDX1/viFN3exKKSHsrTQQZD2M260OyuUykljC8zwEQYA33niL25ueWL5krpk5DfRnjKZx0g2gCt3o5TjZrYkFwB4wRluSJEC3TK4lAZF2jwkps6xRiL6OBuYjNY6hJEn6Mj9VRwY13vKghqV8Jwb1WvNzihqm8v/PB1bzy6rmUZXCllLCtm202238yq/8yhNsHaKnx9zfzePhSTNaVKNvkiTbNo+7RXUWUcuijndmFxYbh4b+fOUE5UmXS313MdBK40adF83qO67rbtv08x32VDWdUaaGQwmCAFGU3V/mh3sAmIkLDL4GGZd1k7/2yg/zYf6/6DuJHXhokjFASkRENOHOXzgnp6en0W63EYYhbNvWwQN6cl/7GoPLNLnyGaTqOaA3xm5Rpul23JhvtPGjqNTlsABOUUNC/pEfR/VJgkJSSjiOgziOdeA4SRLcuHEDX/jCF3huoT2r6DhmgxtNunFpgCd6GszOZwq/R4i2jscRUTEGSImIiCbUsRNHZaVSged5ePz4MaIoQrPZRBAEmJqa4gU0EW2KlFnZ5iiK9PicQH95ND0Obzdr1Awm2t2Heo+api6tBpGNNwpgs2cnC4AaJbRoHNS8YUFSM1vWDPia44+uN/31mB1UzLFNkyTBF7/4xU1Ni2i3FJUT5XVF/zmNJkO+BOagzjgcM5AmnRp7NI5jnSWdH6KB1uL3CW0X7ks0qRggJSIimlAzMzO6ZKPruqhWq0jTFN/+5utsnSGiTYvjGJ1OB47jwHXdvvE4VUB0UNZlUQnCbWkoTrPyu8pGgjWDyt+aQVv1/40sh3pPLygyfLmKppGmKT7/+c8PfS/RbmIG6fqKym2zQXK8med/BseJBmu1WkiSBGEY6goacRzD87zdnrU9yyxhT7Seoqo2+WEQiCYVA6REREQT4sixo7JeryOKIlQqvs58cl0XjuOg3W7rsUuIiDbr1q074uMfb0tzvCjLsvTYeur5/Bg4thA6ZFiYdSazx6C4Yj6vQGWiitx9fjZGabGiz83/fVDDQVFwVCluGF8/WKSmoRoH1Vikjx8/xh/8wR8w0kQ0whggmzz5DFIiKvbo0SNEUaSrkRSNRU9r8XuFtgP3I5pkDJASERFNCM/zUKvVEEURgqCNTqcDIQQuvnmJd5xEtC06nY4OhMZx3FcWTTVwmcFRZSNBxs0a9J58Fk++3O6wsrsqM7boPfle2U/C/BzHcZCmKWzbxje/+c0nmh7R01R0vLLBrYdZhJMpX15XfR8y6EPUc//+fcRxjCRJkCSJHmbgScdznwT8LqHNGHYMLSws8CCjicQAKRER0YQIwxBJkiCOY9TrdXQ6Hd5UEdG2Ur3+Pc8rPL9IKYF1AqP5zFGkWz9HmfOxXoboRhurzdcVjUFaNE2zvPCw066atuM4+jMA4H/8j/8xdN6I9oqiQCAbuGkSmRmkRDTY5cuXRZIkUo09qoZnAPj9sR52uqGtYIldosFVpoiIiGgMqZvMTqeDIAh0AzwR0XZRZXUdx9Hjkdq2rR9F4xOqIKIZbJRC3aqYPy0AG7uBlyK72ddN0lavVFvROIBCCMBaP0iaH2N0vfFH8w8VJN3QvBuNgpZlIY5j/PEf//GG3ku0m5hBuj42ZE+29bY/9wui/mojZlCUxwcREe0UtooSERFNiDu3bov9++dkmsaw4MIWDjyntNuzRTTSjh97Ts7O7sPc3Aymp2dRLpdQKpVh2wK27cK2BQALQkhkwb0UQtgQQsKSFqSV/UxFWvgzQQKRCqQi1T+lFABSPb00BaRMkCWnpFheXkWSRAiCCEtLi3j06DEePPgQ167f3vGu941GA416HZ7nIUkcRFGENE6yzhipzErGQsJS2ZQCSCQACMSphIwTlCQQpzJ73rIhIbKfMnu9kECaJkjSGKlMkEgJu/v5UkrEaYooSRCn2fshgThOEcUp0lRCJikcy0IQBDoIGccxYAkkSZJNB/3B0O7E9WeonyojyMwQLcoiNRv6LCNQa36Gmm6aprAsS/+UUuLWrVu4dInl0GlvU8eEZVnodDq6U4Truptu3B7YiWHEbVcp7nGm9iGgP0t/VJmZcGZnoHzZ0I0upxACURQhiqKdnG2iXdHpdJCmKXzf19VI1FADwww7n47judcs323+v6gz4qQblIU8LvuCuncAestk2/bAIU7yHXa4j9AkY4CUiIhoQhyePySBbCzSarmG9957D51OZ7dna2KcPXNCNptNVCoVuJ6vM+nSNNVj7URRhDAM8fjxYyRJgjAMcefO3W27Wzl67LC0bRtJFMP3fSRJ8lSCZuPgwvPn5IEDBzA11UC5XNaN/urhOE5fhqDKojQzItVPGSdrMg2VfJkjF/aa52zPXfPaNE31TXGj0cgChXGMJDmkx3P6wSSRaZri0eIS4jjG4uIi7t69i6tXr2/LPnDixDHp+75eP1EU6YZuc1xNlcmZV5SBmXTv22XarcwrBdbLIM03cqjsUWl8pAo65oOTAt1A5pDlLBo7Nf/7dlANHUIIvPXWW9s2XaKdEgSBPt8ocRyvKRdNRE9mXBryiQZ5+PAh0jRFFEVwHKf/Oo0BnIHGpRMREdFuYICUiIhoQlSrVURRhNXVVXRaAa5du8a7qB1y/twZOTs7i9nZaZRKJdi2Dc/zUCqV9M2+6gmdD5CqjAAV9PrEd35cqt7AynqNBGra+XEPzYBdEARwXReWZeGTn4yl+uy3334Xf/KnX5n4/eL8uTOyXq9jZmYK9Xpd92IvlUrwPA+e58FxettUrUulVCoNzCS0LAt2QRlW9X9gbY/e/O+r7TYA6KCo2n+SJIGUUo83DNhwHBvlst+Xyfjss89ieXkZjx/P4NlnnsHLL70kVbD04qUrT7z9XdeFCpBm8xd3M2lt3YPZsixdxlavE2HpjBozs8aSgCXXNgjng5tF62k9g8Yb3WzDszmdfK/srVLLqLImvvSlL23LdIl20tTUVF8w1HEcfY7aSPbPJGADNm1V0fi+ROPivffeg23bOhNS3buoDmPUb9C9BBERbRwDpERERBPg6PEjslqtIggCXLuyPdliNFij0cCBAwdw6NAzKJfLfZl+cRz3lYwzS3SqzDb1U70H6M9AjKKoMMBjZunlS3uar1GvcxwHSZJlM1arVTQaU3j48KHsBAGiKMLCwttjva8cPfKcnJqaQrVa1cFP13VRLpfRaNQwOzuLZrOp/5YFQx2dLWqOtakacIQQep2azLJ6aRwPHLtSvRYoDgwCgOf7fQ2kKlBq/j8MQ7Tbbf1YXV3FysoK2u02AAHXdXHw4H5YVrYPtFotHD58GM8//7x899138Wdf+fNNb3s13qhZ4kkFSVSAVAVJpOjti7aw9Hil5nrcyVJo5nGlP2uP9L7Pl10MggD/9//+312eK6L1HTlyRM7OzmJ+fh71el0f0/lx5Ght5jkDXbRR3Fdo3L3zzjv6vJgkCSsPDLHe/QQREW0MA6REREQj7PjJY7JaraLdbuP61RuFd0QnTh2XquQl7awLF87LZrOJudlZTE9Po1Kp6AxSlWkIZKVCVVYd0B9E2sjNrWeUczVfawbtVNaqGXhS0w+iGEDW0Hb9+nVcuXIFcRxjZmYGBw4cwP0HD5CmKU6fPim3q/zqXnH69En5zMGD8H0P1WoVtVoN9XodU1NTmJ6eRq1WQ7PZhG2vHSNMrUPLsiCRoD+zNwVEVvzV9XrHmhmIs6zuA9YGGzSKV30UB8Y0LTi2Bcty9HSy+SpjGk29DCpoGkURFh8tYXl5GY8ePUKr1YYQAo1GDdVqGfv3z2F+/jBeeuklmZXfvYrLV4Znm588eVyWPA+e62brAlmQXgVNhRAQqYTlOEjT/hLDar3YtgXbtuBYAo4lICxkA47qBwDIvgq7ZnBzo8xOA30dEDbZsFSUxbMdgSAzYGLbNt555x38xV/8xVgdh9vl7NmzUh2z09PTqNfrqNVqsCxLl75W5z1gY9tnM2OYPcn7VeeBnfr8fOZ6/j35fT8vjmOd9amqGkRRpJ8XQsD3ffi+D9d1s3GHGw00m00cOnQI+/fvx6FDh4zvOzbYmrg+iPq9+OKLcn5+Hs899xxqtRqA/nG9N9NZSp3/Br1uo+ff/HWC+t0sH17EvI4fdo1nXkOY0+90OlheXsbS0hIWFxfx4Ycf4q233pqYk8aHH36oy7WHYQghBEqlEjsHDGB2wGQWKRHRk2GAlIiIaISpBstGo4ETp47LG9du9t0RHTtxVM7NzaHVaqHdbg+9sacn99JLL8hDhw6h2WzC6QZEgf5GGnUDW6tlGYsqeJrP9lSN0EVlR4UQSKJoTYA0Hxj1PA+VSgWVSkU3ZKtgASxbf1an08Gbb77ZLeXp9gVmXNfF/PxhOeqZpKdOnZCzs7PYv38/Gs0aDuzbj2q1qgMq/dsB3QCj0AE+ldGrMoBtR+iAqdpOwNoGiXzpVR3AdvoDB/kGjaENgLLXwFc0Dc/z+sYkNT/b8zxUK3VYloUwDPHo0SPcv/8Ajx8/RhAEEEKgXC6jVkuwf/9+vPDCC7hz5468ePEiFhcXUSqVEHbLQKt9JE1TeJ6HcrncLS8sdYAKyErvZusvgQ0BDCg9bO7DRZ0FdIa0HNzAuZEGNLNBcq82Ipnb8+LFi7s8N7vv6NGj8uDBg9i/fz9mZmbQbDYxNzeH2dlZzMzMQB3fzWYTzWZT739mR5Sntc2H7YM73chrdoYqOicNW/4oipAkCeI4RhAE+tEr3Q1UKhX9fVIul1Eul/X/1bjDQK9UfL6zySRjAzZtxbhkHH/0ox+Vzz77LE6dOoVTp07h3LlzOHPmDKampgCsDZCa17rrMe9zitbTRgKk+cBoUaB0EPN6UF3bPGmAdHl5GYuLi/jggw/w/vvvy0ePHuHP//zP8fu///tjffJQy91oNAAAnucB6P9uo37D9jUiIlofA6REREQjbOH2XQEAH/3YK1I1KpiazaYekzCOY8Rx/LRncWI0Gg2dNRqFIQDogJSZyek4DsIwhO0IOKkFYUmI7j/VSFKulPumLaVEKuMseU5KlLplTM2GIhWscl1Xl4OtVKp9WazqxrkdZPuDCiBYltUdZ9PvCyakaTrSZXZfeeUl+eyzz+LgM/sxNzeHRqOBUqmEfbNzsIxxMFUGr1ofUkpAqJK1KSQkLNuCbTt6/ayX2eB5XmEjhQ4SxElhYHSjfM/V22dQFqP6PPNzzZ9RFMH1bDSaNRw/fhwrKyu4e/cu3nnnHbTbbaRplj3mOA7OnDmFw4efxbVr13Dt2jVUazXE3TLBtm1Dpml337Zg2wJpmgVIS46rSxDHcYzEkXBcG0j7g54qMOrlyuzme8NbEJDI8lO3ygxG6/VXMN7psGnkt+N2NUyZwaRvfetb2zLNUXPmzBl58uRJnDx5EseOHcOJEyfwzDPP4Pz5832ZoarB2zx3rdcYPWwb7XSG6W4bNL6xor6nfN/X2VxF1PlnvQCG7tTQHUuXMnt9H6G9bb2sxFHw3d/93fIHfuAH8IlPfALnzp1DpVKBEKJbvcNed7mGBTi3ep4pOidu5rrA/I7ZzDWe+RnVahVzc3N9f1edVizLwqVLl+S//tf/Gp/73OdGcwcY4tatW+L999+XzWYTjUYDlUplt2dpT9tItjIREa2PAVIiIqIxkCSJ7mmrvPTKi9LzPLTbbX3jfefWAu+adog5zqIKkKnyUEmS6MyactnXAUszO9Rs8FdjWA7qEWxJrAmQOo6jy/jWarVugLSms0fNDFVVllmNFbm6uookSVCtCgRBoJ8f1hC1Fx0/flTu378flUoFc3NzOHToEJ49dBCzs7NQpabNDFwzEKca/FMZ6yCBmQmlfnec/kvofKavGjNpzXbrPmfb2fuLGjLMz8lTr00g+16X30/M/cfMSlbMDK84jpFAolKp4Pz58zh//jxu3bqFd999F++/fw9hGMKyLP33U6dO4U/+9Ct6fQmxtrxt2g2Y2nYv8AwAIu1mk3aDq+a8byRzdLvkM3DMAOlG9vn8vOUzhbea3aMaWNV2unnz5pamN2pefPFFOTs7ixdeeAGf+tSn8NGPfhQHDx5ck5WvtkM+Q9TM+FY203A46gFQYP1lGHZsFQWRzfVpnhPVulffcUCWgaqyylUnilH8LiHai8Yhg1RV8Ni3bx8OHDgA13V1h6z1OrKsd31U9Nqt2olsb7Oyx0auc8yqLuoc+9xzz+Ezn/kM/sE/+Afyk5/85N7/QnoC9+7dw8GDB2HbNur1et+49kRERNuNAVIiIqIxEMcxHj9+rP9/7MRR6TgO2u02KpUK3vj2m2N5A72XqIw6y7IQR1Fv3MVuA7EZQFXBR7MMpBnkMrMU1U/1dyArsaqCcVlDUpY9WqlUUC6XUa3W4XmeDo5CCKD7OgiBOMmyR5Mkged5OkAbx1lgME1TuK6LpaWlXVufT+LY0Xl57OhRnD17Wo+Fp9al53qwIBAFIRynvwSlWn5hyW7jY3+wTsoEQvRKwFqWU1g61wx2r9eb20Zxo1hRA1nRc5a1thTvsMa2QeNq9U2nm0F54sQJHDp0CHfv3sW1a9ewtLTczTDOSvf+wPd/P65cuYJLly7BcRyUyyWsrq5iZmYKURTBsx3EYQSv4sP1skCpZXvodLIGLlVyV697Y92p8Xodx4EtLAgJIJVAKiGt4qBXfluawZii4DAAHcRW8+G6LsI4KiyXbGZzmtM1G3KLxn4yg9P949UWbyMdAO8Gm2zbRhRF+JM/+RNMgpdffll++tOfxqc+9Sn8wA/8gC7f7Ps+om5ZZ2BwQ/J2ZfOOQgB0mK0sw6Dz0JrzmJGpZf6uOkSYBpUhn0TmOUExzxmTvo5GPfg3iOoYl8903+z2VvuPusYcRZZlIY5jXV1FdagwO1oU2czy7tV1s9kAX9FyeJ6H6elpzM7O4ktf+pL8S3/pL+3Nhd2Cu3fvYnp6GrZto9FoYGZmZkPbdK9u952krlHV94pZXabo+nWcbbSD27iuG7MTifqp2hvynQvN16jS5OzMRpOMAVIiIqIx4TgOTp89JTudDg4cOKBL2q2srOz2rE0Mcwwh9UjTVAdjirIyVZA0P84osDZAqh9p0heQsawse7RcLsP3fZ1J6jhOb7xHIQCZZfuZ7zWp58IwhO/7KJfLOHHimLxx49aevXs8Mn9YHjp0CPv27cP+/ftx4MABzMxModFooFwuI0mSvkCgEEJngKpgtM7mtXsNDSqona2TXrZpFjTtD7Dmg6TmTWhRkNS17MJtbE7TlP9/KtY2ApjTURmkpvw6KAySymzeoyiC53k4deoUDh06hBs3buLq1atot9uYnp5Gq9XB2bNnsW/fPvz5n/85oihCs9nE0tISqtUq0ijWmdSlUqkvOGLbdnZsmFkRBVmk+TK7ZqNG0ToZtC7yP7cSOHtST/p5UkosLi7iypUre/b42w4nTpyQzWYTZ8+exf79+3H48GGUy+XCUoVm9jMRjZ9xyJDMM68Txqkxnp6+NE11yVnXdTE/P49/+A//ofx3/+7fjdWO9f7772P//v2YmprCvn37xi6QtZ02MjYuERGtjwFSIiKiMaCy1mq1GhqNBlZXVyGlhOu6qFaruz17E8NsxFcBUjOzVAdI0xQyToAkhQ0Bz3b6sgGKAmRmYMdyVBlSW489WqlUUKlkpXX1mKO2kckjJQALgIDojq9pZrWpRkk131EUIQgC3Lx5e0+3SOzbtw9nzpzCsWPH0Gw2USqV4Pt+d30nEALd4Jv56GXsmoE53XgpUh0gNXviqtea8gFSs0fyoICcI/qzDTcTLAV6JXbz86F+mgFgxQyEWrL3XPboD5iWuuXu0kRiujGFj5x/HnPTM7h+/TreffddlGtVpGmC/fvn8F3f9Z144403cO/ePRx9bh7tlVV4JRcQUo+Hq+bFgoDl2EiSSJfnNdeb6zpwXQeeY8NzbDiOBcsChFAjjwoIIQvLF+etdwzpBwYEinOKnttsQ91mXm9mGN27d29TnzNqfviHf1h+6lOfwquvvoojR45g//79iLolsM1xK/MBcqJRtVsdNUbJuK2bXvUJZlLT1qjMW9UR7sCBA/jJn/xJ/MEf/IEcp85Ud+7cQbPZxNzcHObn53d7dva09a5fiYbhfkOUYYCUiIhoDJhBG8uydFAijmNm2zwl+QxCM0jaX1pXBSEtnd2oMuvMxrN8ppzZqJoFiCQcx4PneSiXy6hUKvD9Sq+8q7ndpcyyR9G7iTbnTzGzXVdWVvZscPTE8aNyamoK09PTOHjwIObn57Fv3z5durW3HFnpVJWhawZBizIWVeDUdsSa1+eDeUo+MyQf8CxqDFcBykGZjYMC5fozZX8W8sDX5YJKegzBAQFS9YiiCJVKBXGUoN1uw/d9nDx5Umcnv/P+ezo7/ZlnnkG5XMaf/umfIgiCvn1X7ZuqIS/tZs6mTi/L1lxvalupctT50sVCiG4GdsF6HTDepBAC5q1/37rNbYdhjQSbzWDYaiO4EAJ3797d0jT2ugMHDuDw4cM4e/YspqenIYRArVYD0BvL1syOYGBh543DGKx7GYOjmzMO62vQ9QPRZpn3dOr+4syZM/hH/+gf4e/9vb+3i3O2ve7du4cPP/wQjx8/1teWNNx6nTOJBmGQlIgBUiIiorGgMgHTNEUcZ+UtHcdBvV5fdzwf2j75DEMzG1OV2jW3U5JI/ZBSZYM6hWMZ9t/spohlCttx4HXL6lbKNfi+D8dxeyVehQDUWJdCdD8jy45UY02qcr+9DFfoeb127caevLM+efKkPHniGJ577hAOHDiAarWKWq3WDQyrMq6WLpOrgqMAIJHAsrLLX8vK1rcKxmUZo7ZRRre/QVMIoUvwCvSXyDWDrtm0zfLHazNGnNxzRUFV05oM1CEZjYPGkFE3wLYREDQziVXc1S9lQU1L2PA8D1JmZXufffYw9u8/iG++/k0sLCygVquh1WphutHEX/rkp3Dz5s2+dVEul1Gr1RAFYfZ5XvZ5cRrpeTTXr+P0b4+iMrvZ/r1+40/R80UBaPP3/PTyJcuklBBqHRa8P/9ZRQE9HbAd0g5hjo969erV9V88wj7xiU/Iubk5VCoVVKvVvmoHKsMY6G+4YYcfGnWbyXyn8bBeRyqizVAVQtT1kaq48Lf+1t8aqwDp5cuXxenTp2Wn00EURTxeNmC98wpLFBPAawyi9TBASkRENAbUmJEqe7Ber+PBgwdIkgSvff2bvBp+SswGfZXFGMdxb9zF7v9Tx9bB7CiKsue65SRd110zTbNxDQBsJHBdt5c5Wipn7xMWIKWRPaqy6ywIHVewIWWig2IqezXLck33dHAUAEolF41GA3Nzc9i/f79uLMrWc5Y9rYLUOjAqZbcMtd3NMs0amTzP02V0VYDUzOTNB+eEla0vSzhrtku+4XO9Xtyu3T+Gaf73YQFSqyC7WC1n0esVHWjqBifNrFEzQFoul7G6uoowDLsBy6zkrlqvH/vYxyClxAcffIAoirC8vIxGo4Hz589n5b1h67F1K5UK2kZAMEkSiBR9AVI1zypAmg+U5ssXp+nGb/Lz2bNm8LJI/u/rNSqtl3k6KEi6mXlO0xRXrlzZ1HtHwV/5K39Fnjp1CidOnMD8/Dzm5+dh27Y+J6n9rKi8Lht3dh7X8c5igGxycdvTdlBjcZv3BbVaDX/37/5d+au/+qtjs3N1Oh2EYag7/gL8fipi3oeo/xMNo66pOYYtUYYBUiIiojEQRREA6FKui4uLaLVaHH/0KVIZouphBkfNBwBEdtawEUURwjBEEATwfV8HiYoyR/sCcI6A63nw/QpKpRIc14UKhkoBCKhoaPenca+sMlvjOO4r8auWwSy5u5fMz8/L6elp7N8/h7m5OdTKFbiWDVv0Gokc14bnurrxyBICthBwHAulUgmu68K2XUhhwXVLupyrZfXGCDNL7QrRe95s1LTtXoA0X7YXKC5x1ZdB2otWrzu+5aBGjgS9hqJ81mM2f8VZ4zrwFsV9/8/fHHc6IWq1BizLQqfTQRAEer0kSQKn5OB7vud78Gd/9mcIggBuuYLV1VUIC6jWKmi1WgAAz3FRLvlI46Q7vxJxHMORdl/2sloGtzsWr/kwA6S26JbYtQY3Mhetj42MK7pew/VGS44OC7xulNrfkiQZuwDpyy+/LM+fP49PfepTePnll+H7Pur1Osrlct/r8h0HVACVFRFo1LHxerhxXkeDvmPGeZlp++QrhKjrhdXVVXzf930ffvVXf3WX5mz7BUGAKOqNWW9WlqCe/FAUwHifQ4mIdgIDpERERGPAbJhX47ZNTU3xBukpUsHFfJA0HyhVgVHLsnQGqcoiVZmOruv2ynEKkZVk7QaIpBCw3SyQlAX9PAgVHIUEpNUXEM3Po5QJ4jjsK/1rllqN4/gprrWNeeGFF+TBg/uxf/9BzM3NoF7LAsMq89bzvG6J4BRhGGb/dxx4jgNh2yiXSvDKHmzYiJIEfqmkA6TZNpF9AVIVpM4aHES3dLGAEP0ldM1xSs0AtmrsHBQktURufNj87/njNvf/VKbd6doAUkDYgEwAWJDdn0Cq/y6l6PsJx4OUCSxpIRUpRCqQihSWtJBAolSSSCAh4wROKRtHVFoCaRSjE4VIkgiO4+B7v/d78aUvfQlXLl5Cs9lEEHbQ6XT0OnJdF77v66C7ZQFBFOrjw1wnKgjmui5c24HtdschdXoBaCkEdNOgtbFGoHw2qH70huRd2yFh3SnuPHUsBkGAd955Z5fnZnupzO8jR45gfn4enU4HnuchDEN93jO3k/o+G5b5SzRKeG22OaO+vooqGRA9KdUZT5XjT9MUvu/j6NGjuztj2ywMQ92RE+Bxs55BHTKJNoLX10QMkBIREY2FG9duiiRJ5MzMDGq1GpaXl3XA4cy50/LKpau8W9phAoAlBBzbRhzH3d7Olu79bAbNYmGh5AJxlCAMIiTlFFEYIwpjiKoFy8mCnwCQQsKxbEgByCSF4zo6IGhZdjcgKvQ8ZGV21Vx1s0FVhlualaoKwwBhGGBlZQVpmujPAlLY9t7bVZrNOo4em8fBA8/CKzkouR4gBGKZwhEWUgHA7gUrhe3AchxYjgu35MF2XEDYsB0Pjm/Bc/1s/dk2HNeF53k6Y1E1NqlAqWWju45TXVq3KGAjjCCpCoQr+QzNJE17wTjLWhsQHXCjqqZh2d0yzKnIguIpumWVbQhL6PnV+0aaBc8FbAjLQpoAlm1BwIatXidjCGnBFlkA15IppJ3CEYCQKjPZhu/YsJ0KHj58iGaziU9/+tOQUuLSpUtZRq5XQpymaHU6KFcrcEse3DhCqVRCp9OBbzuQMkGU9sbnFULAsm3YTrbdXNeDcLJ9PoFEIlOksTkGVbaPO8KC7GYQQwgIKYE0hZW9AraQcKysorCUKbIgcQKZrZLsYYwVLJNeZwFpjBlslvddz6CSx/ntP6zxSgUFV1ZWcOXKlb13QG6BWhdqffq+DwBwu5nf+Wxtc8xRVU6bRtukN8QVNfYzg7DfOK4PPT48+s+DwOYCGub3y6QfS5NoUPWLTqeD+fn5XZqrnWMeN3EcrxmG5Emsd30+ilSHWwBr7j+AyQqYbrTTZP7cOynryLz+Nqnl36tVpIieBt5lEhERjYnbN++IOI7l0tISnn32WSRJgtXVVUxNTe32rE0Ec9zRfEYvkPWEVjeurpUFUa1uMDUMQ4RhiCiKshKmTpYhajm9sfjU9G3bhm1lmVaWUDfCVn5mspt+dANxyOYrikKdsao+K5/tuBfpsT/t/iCJOe5OXzan0/vpOA4crxcEdRwHrlPqjkmarWfzb2obmdNVzEDZoN7ag8remtOBVby9VEDOGdAApKebZgFBCEDA6c8YlhbSbtBRWNDjzwpjH7H67gCyjGNLZE8KY56FbcE259VxkKbZvrhv3z50Wm24rouPfexjSNMUV69eRaVS0ftVu93WpVN72boC7bANO031Pgigr7yx7JbQTbvLlUDCEr2UT9G3NGszQPM3/n2N0kN2cSHXbkNzGk9LHMf44IMPnupnPg2VSgWVSkWf0yatcYqIiOhJDSozb9s2KpXKU56bnWfeU21HcHQcqXsWXkcRET05BkiJiIjGyNsL7wgAuH71Bk6ePiErlQo8z9vt2ZoI5rijZtlaNeYnAF1aN7IdBLYNiSxwFgQBgiBAJwoRxBGa3fKkqjHAcmw9Hdu2e8GsQTfDOuDZ/b/MGhjiOEYcx+h0OgjDUJf13cs31y++9LxsNpsolUo6AAq5NijWVyK3G+xUgU/P81AqlbLxWh0HnuvrDFEze7SoXC7Q3+NYCKEzhIvWWV8Z11zWoHok3febGaRCCB2MTJNEv6dImvRPM//Zdi7TzsyGBADHc/ten/8cc4yr/Bi16j1Od125rouTp04hDEMsLi5icXERpVIJQgisrKygWq32BcOEEJCW1KXh1PFirs/8+s/PxyDbFeQ3t1W+RK/6+05yHAdRFI1Ved0LFy7IRqOBEydOYN++fTpz1LQXzz9E220cMpaIaHflr4kcx0G9XseZM2fkOFWe4PlyOPPamYiIngwDpERERGPq+tUbAgBOnTkpzz1/Vrqui6WlJdy+eWdsbpz3EhXAzI/tqf5mjk0ahqEOvpgBUnO8HRXwMxtBdPBuk6Mkpt1svSiKEIYhgiDoy97baxmkR4/Oy5mZGew/MIepqSlMTU2hUqnoUltq+c35NoOknuf1ZYeaD8dxUClXdIDUzBxVjQuDym/ls0EHBe2GrUuzEUN2g5FmUNVxnDWfaX6ebRdfwqvPDIOgL9CrPlOVoh02n4MyFNR8x3EMCYlyuZwFOJMEx48fx+rqKv7kT/4EcZzN/+PHj2FZFsrlMqIo0svn+36WQd2dx/x+qEsl5wLUPRsvdZt/bERRgPxpHxtCCNy9e/epfuZOOXnypHz55Zdx8uRJnD59GocPH0atVissAzeOpTWp30ZL4I2rog40RESbZV5LCiHgeR6mp6d3cY62l1lZJDWGpqB+e+0ejohoFDFASkRENOauXbkuAODYiaPSzAyj7ZUfW0o9zKy7MAyzFye9oJht2wjDUGd15gOXUkoglRDd/2fBQfV+nSKa3RhLPTPdH6q0boB2u4Wg3S4ssZsPpu22Wr2CAwf34ciRI5iamtJBTKC7no2AaFGAVD3M7FHf9+H7fhYg7QZb1esGZYwq+V7ZZkZlUaONGgson32pgudWd7ua+0hqZHmqzN58JrI5P+Zza0oM5/5vrh/LspCka9+jFJWnzS+fbdtIkgiO7cBxXXTabfjlMp5//nksLS3h0qWLOlM5iiKUy2UdkE7TFAmyTGv12WapZzV9cxylogBpUYBhUEA0HzAdZrcbmtR5eowCpDh//jw+8YlPYHZ2FrVaDdVqtS9Dd7fXOdHTYnaeUrjvE9FmmOcM8zq0Vqvt4lxtL7PSC68R1sf1Q0S0NQyQEhERTYhbN24LADhybF7KJMXCwtu8k9pG+SCdMIJgqkE0TdMseFby+8YU7XQ6WaZdq4V2u60zSdV01U9LBYx0JDRXgtT8PxIdHO102lkAtps5agZiVTCmqKzpblFBzXK5DN/39frsWxdGgM9sRDFL5vq+j3K5jEqlogOkruui5PkDMwuLyuKazLLJqjysorZzFEV9AU/1+iiKEMcxku771HNqe8RxrJ/L7zdFehmlvWxYM+irMmMdx9GPrARxSa+fUqm0prywSe3Hal6EkLAswOlmsco01eVSPc/Dxz/+cXQ6bdy/fx/SGF/UDBRD9rKqHcdZEyjLB+zXC5Cul4W13RlaT/PYEELg3XfffWqft5MOHjyIAwcO4Pjx4/A8D5VKBaVSCQD6tj3RJGAGKRFtN3U9OE7DqjiOo68H1f0SDcbvFCKiJ8cAKRER0YS5c2tBHDnyHO+itpkKVJkZefkxHlXQLIDQASfHcWC5DhJIeMs+6vU6VlZWUKvV9FiOOlCUpFmJVKu/TKpOHVVxNJFCphIyThAHIaIgQBQECMMO4jjWATkVpDPHIbUsC/Pzh+VuBNDPnT8lG40GZmZm0Gg0dIlhlWlrWdmymoFos5SuyhQtlX14nrcmOKpK7Nq2GQSUUAG3bN11n5UyS8Q1gnlqDFf1MIPMKrCp1qcKfKp1bf6eRNm0giBAq9XC6uoqWq0WOp2Oft4MrpvyQcN85qxlWfA8T5caVoFm9XBdF67nwfezfa1arepAcrlchtf9m1qv2WeoyrwCgIBEAgFjLFXLQtQN6tcbDbz00kt4//33EYchyuUyHMfR2dMq09nc3/JB6UFjkPZ+T/s6IKj9d1AWd/6xFU8zoHfv3r2n8jk7rVKpoFqtolqt6sA8UJydzBK7NO4YHCWiJ5X/jlRVYIBeJuk4OHLkiGw0GroCCQOkg/E7hYho6xggJSIimkB37tzVd9C7FQwbN2YGKdAfHDUDpCp4BmRZm2EYwgqyMRvb7TZarRZWVlbQbrdRqVT6yqSqrFJX9RAvaAjJgmrd7NGwgyAI0Ol0uiV8A535mC/jai7D026EmJ8/LA8dOoTZuWnU63U0Gg1Uq1WdZQaokrBCNwDls0fN8UZrtVpfBmmpVNKZpWoMV5WFO6hhQQU9VWA0iiJdMjYMQ/2cGSxV48guLS2h0+lgZWUFq6uraLfbehtEUYSgHfaV1lWZoyq4qtZ/UXBUCNH3fD64mH+9Wneq5LBt27AdB9VqFc1mE/V6HbVaTa/3SqUC1Sil/qYCpmo/tKxs/dndssdpkkAIAdd1IdMUhw4fhud5SKIIpVKpLzPVsiykUX+AU48t212+ogbAQc8X2akA6dNqeFT73sOHD5/K5+00c1xg1ekjX1ZZYYCUxp3ZkYOI6Empa0Gzusq4nFemp6cxNzeHRqMB3/d5XTAEg6RERFvDACkREdGEOHXmpHRdFxffvNR3l8ng6PZQgTpVTlQFk1QDhhkASiERyxRJ0EGUJohlikqlgtXVVSwuLurMRxX0s2SvZ7hT8pCmKUq+DyBFEsewbbeb5pdCWAKyk6DTamF1dRlBECCK15ZwVcG5JElQKpUQhqEOZKRpigsXzss337z4VPYNx7VQqfqYnZ1Fs9nUPcZt24JtW3AcG1mWJ/rKrzqOo8vEquCLCqz6vq+zI83xS9M0hS2y98s0RdoNgqptZZbCNdeZCjQHQYAgCPTrgiBAu93G8vIylpeX0W63ce/ePf03FRQ1G7Js212zDswgojm2aJFBzytFjWRpmurMVCEklh4/wjtvLwDI9l0VTPY8D4cPH0az2cT+/fsxNzeHqakp1Ot1XRrVcrJgq2PZ+vPU+KJhGCJNE1SrVaTdbGUze1QF5s3jIZ8Nmh9/NB8UVQFZ8zlzRzXXTz5DUZVqS7rTMNeVmc3aKyncP3/rBWjzGa1FY59m5Zb7MyHM6arlf/z4Mf7iL/5i5M/Nx48fl4lRallRnRUUM4t9I9T2ycbDzY5hlfG81xsJt9rQOyrLl6apHmN4MyUSB62fcWkAZkP/xqnvbHUOGWXqOqOos856HZ3Ww31p8iRJAsdxEEURXNft6zBndqQcdadPn8bBgwfRbDb1/dV2Ma+59srQIluhrmfV9TXAcwMVG9SZYhyurYi2igFSIiKiCZGmKcrlMs49f1Yilbh06QrvnnaIeWNaFKwyS6cKIRAEgR47qNVqYXl5GSsrK6hWq4iiqC9A6kURSpWsVKqlyvl2P081jKggnspoDKPuzzDoy4xUD0VlGpZKpafayFKr1XRZ2PzYoPlxR80xN9VDlZJVDzNrVGU+mlT2Wr4srsrmbLfbiKJIP1RZYjU+rBkUXVpawtLSkt5mQXecVzNDND//u83MwlXrQZX5VWNfuq6LcrmMRqOBffv24dChQ5ifn8fs7CxqjXoWUC35fWOXqmnpMUKN7GQAOugsLblm3zO389OwlUYCM5t1ow3bmwnupGmKVqu1odfudSq7W3Ue2WgAdD1xHMNxHN15QWWSA9m5z8w8H0d74RyyHtUpolar9XUWUMHsSS+TmP9uIyLaKPUdmu+IooxLgPTo0aOYn5/HgQMHUC6XAbDCxCDDrmG53oiIhmOAlIiIaEKEYQjLstBsNmFB4NSpE/LatRtr7piOHTsib926wzupLRjUAKpL6wkghQRSqYNvUkq4rotWq4V2u50F6ToBLNnfKzxNUwjHgu+XYNk2hNEwkkZZwECVc1UB0igOEQRtxGGERKaIk15pXRWoMseEdF0XYRji+eefl2+99daO7QsfeeGcbDQaqNfraDabOqhpWaL7sPrWpTl/ruvqTFGVcVutVvVPlX2rGuNVUBgAoiBb32rdx3HcV0q31WohDMO+ddjpdHSAdHV1VWf7Pn78GK1WSwdGVQCgKNBrlpTdmnTI341xadeQOqNLzaeZpaP2Q5UB++jRI7z33nu4ffs2Ll26hHq9jv0HD2Bqagr75/ZhZmamrwwvkI0RCgCyu87zJW7NLNJ8T/7Cpdnmhp2i4OhmApj5Epn59w087jf4GWma4vHjxxual71uJ0roqv3MzBoNggCWZT2V4OhWe9qPegbpsPn3PE+/Ju6OU5xv1J9kZhUEmhzm+b/oO+NJjuvdPhfQ7lFVO8xzreM4CIJgl+dsexw/flx3yvO6w4ow0DdY/vzCcwMR0eYwQEpERDQh7t55W8zMzEjbtoFUotFo4CMfeV6+8UZ/AOzWrTviyJHnpDlOKW2dmXFm2VkDaSp7QU9zbFAVqCrqFawCTPnxKdV0etPIgnVJGiNN417GqEwhkxRJN3ClPktRmYCVSgVpCpw9e1Zevnx52/eFF196Xj733HNoNBp9WaCu62bVggsCzGb2qBpvVGWMquCoKhOrgn/mulVB0qDd0ctujieqsm7VmKEqq1IFrNvtNoIg0OOKrq6uotPp6PJWKjBgNlrlS+plAdLtXpubYwZGzeeydS90CTf1exzHePDgAR49epS97uoVNJtNHNi3H/v27cPc3Bzm5uYwPT2NSqUCy8q2k91dF2aQVO+DA/ZtoBdEKQqibpf1yngOk2+EKioZXPTa3s/1l0UIgcXFxQ0sxd6XD5Bv1zTNAD/QK9m7urqKarW6pekPm8+tLkfRuds0bF8f9v7tyNJdz7D5a7VaqFQq+rVmxuhOz9soYAbp5Ml/VxTZ7P7AAMjkUt+B5vlUdUZZXl7exTnbHidOnJD79+/H1NQUKpWK/g7h98dwPC8QET0ZBkiJiIgmxOH5Q1KNUSJkluXh+z7Onj0tL1++2tcyc+fOXXHkyHPS8zwUZZnS1uiMSMcuzCgwy9/mG9bMsRG7bwSQdn+q98b6dQAgJGAhG6MUEmumZQb0VAZhmqZ44423xOnTp3fkbtv3fdRqNdRqtTVZloMy8FRQRJXtVCV1VRap7/t9YxuqdWiWyk2SBFF3LE4VIFUZoisrK2i1WlhZWUG73cbKyooOhqqHen0URQjDUAdHs8Ayug+pNkeu3DIgxO438Nh2b6xTM4NYyiyrSWWb94K8vXVv27YOID+8/wA3b95EvV7Hvn378Mwzz2BmZgZzc7NZJm+5rMce7NuXrcEBTzPD1twn++2NU5IKhpvH5HqZYRttuBJCjE0GqWJmgG9XA54q2RqGIcrlMt5++23883/+z/HGG29sabo7HSCN43jdvw8LlGxkDOKdNOzzf+RHfgQ/+7M/q8/Tpnw5yEnEwOjkKhpuYSv7A4Mhk8eyLIRhqLMqFVX54+HDh7s0Z9tndnYWjUYDtVpND5lBg+U7XzCDlIho8xggJSIimhBCCDiOkzXISKDT6UAIgWq1ihdeuCBff/3NNUHSc+fOyAsXzsulpSUsLLzNVr0tGDTmYX680qIMtGFjG+Zfr4NRuVKs2ev6xycyA6n5nydOnJDDGvO3yhzDcr3gUn7sURXgNx/54KgKgprjiZpjsEZRpEvnrqys4PHjx3osUfWcKp9rBkbNMVzN8pHqec/z1skW2f1GC7PspZp3td8A6BvvKb9/qLKmSZIgCAJ0Oh0sLy/jwYMHeOedd1Cr1XDo0LOYnp7GgX37UKvV4LpuX7ZfKvpLOu9G+c1Bx9VG5iEfuM13WhgU4N8oIQSWlpY29Z69Kn9u2o5GOxVklVLCtm39+xtvvIFf+7Vf4/fULouiSP7UT/2UPo+Y48IyOFh8HLB05Pjb7kx6BkEml6ruoTIr1Xfhu+++i4WFhZE/kRw8eFBXhlFDb9D6GBwlItoaBkiJiGjXHZ5/TgohcPfO6N/U7WV377wtGo2G9DwvyyZE76badV1cuHBevvnmxb5tcOnSFXHixDHZaDRw4sQxeePGLW6jDRhUGlRnSeZfL7KHeo1r2XAtu28MTgCwur/ng1ZqimZGYJqmQJqNczpsHvPlQcMwxPT0NHy/gna7vb0rB8DRo/NSBczUmKJZ8NPuK6+bH3/UtkVh9qjv+0Z5XqHHuFQlh80Su0mSIE0SpHGMsNNBu1tGd3V5GStLS1hZXtZldVvdQKnKPo2jCGmSQAAQUsI25k1nEwKws4UoXM/qufXseEN5mkJYFiz0As/m/IWdTi942d1fs9K4EpGUsLodLRzH0cGqMAx1Gd6lpcdoNBr4YG4OMzMzOhMgG1/WgrRkXyaw2v5q2Qc9lK02/RRth82sczMwqvep3Di+appP8hlpmo5FmTwAejubHRe2I4MwDEMAQKlUQqVSYYBpD3n48CE++OADHSBVGenq/DzptjtQRqMhX1Z+K9uf+85kU9dsAPSY8gDw+uuv7+ZsbZtDhw7pTpCqE6U6Zia9AkGRouBo0T0oERENxgApERHtqqPHj8mpqSmsrq7u9qxMhMePH2djNfpllMtlJEmCdrsN27ZRq9Xw4osfkY8ePerLFr1x45Z44YULcmpqCufOnZGtVgscn3R9+YawIvksNDP7bFAGWhYMBZCma6crJdI0G3s0m9Da6QKABRtAPHDehBAolUpYXl7Gvn0H8LWvfW3bt7Xr2X2NHKoBRAUZzWzDbJ6zdWJbFhyrl0Wqgmuu6/YFYZK4N+ZqPjgqkxSpMeZop9NBp9PRWaKqdK750AHS7nQdJ7uElikgRZpFt0UKmQKpTBEGEYQFWMKGZQsIWBCWhOwm9GaZvbvXyOM6HoQFQAqkadKd7wQCFiAkHNvN9h8pIJHNtxDd5bCz7RLLbCxRM4BtWRYcx8HKykq2ftttPH78GNPT031jSVUbVZ0FbL5Xbfd1A6TSArD+GIwboRqOijoxDJMkiZ6GuX+p5VCBoEHH17C27TRNEQTB5hZoj1LZH/ks4q1I0xSlUkmXqlXH9riss1G3sLAg3nrrLem6LqampuD7vj4fs3G7uIMGjTduc9oucRzra1BTNizG1srL7xX79++H53n6GpHjNhMR0U5jgJSIiHbV1NQUXNfFtStXedfzFLy98I4AgAsXzkugNy6cCkypBs2Fhbf73re4uIiZmRlUKhVEUYTjx4/LmzdvcpsZbNtGGIaoVCp9mZ/mmHv6Bl9KyMQYtzALPUGkEjJOACEhLEBY3cCVBLqhKqRpAgcuRApAApZlA0IijmJEaYxUSIRJhERKwBJIu8EcIQQc4SCQWQA1n9mmAjqq9KzruvjiF7+47dt4fv6wbDSbKPl+lsVo20Z2mYRlCQgJpHGSrRfLhtMNjPquj0qloscbNcvsuq4LKaXO9Myml0KambRpN3MnzYKXtuVApkAYROi0A7SDEJ0wQhDFCOMEcSoRpxJhnCCK4m7vdRuJBCAsWK4NIIUQ3Z+2BUsHP7OfKdJsQyIFLDO4lxY29pjjl+b1gurDNosAUBygAwSkZWVBWmEBAtk2MOY7q7RrzLfdWx7RnUdHl+YFZDfym6YpwjDsZiFHWJar6IQhllZW8ODRI0xNTaFarSJNU5TLZbiWC8uzYMOGJS2IVOhS4EIIWMKCgMhSRqXZeUDAkgCSFDJOdOAlkRJxN4s3BZAKQFoiy862ug1sEDrI3TEyZXvjsPayFCzL0sFQc/uo59XDHKf59OmTMo5jXWbZDJwW9ewv6uXvui7efffdIdt47ztx4oQ8ePAgZmZmdHl3tW3zNtPwqc4XjuPoDhUAWIZvD3nnnXfQaDS6YxLPYXp6etPjyOW/N8elgTyfPZgPno3DMm5F0bXJqK8T27Z1afs4jvV43kWddIZRlV9Uxy3aXjtd4WOz08+fFxzH0dfoAPR3YBiG+MpXvrKledsrZmdnUa1WAfSGfAC2pwKFutZT0xqHDmnqO8WsLjQO503aGWrfN+9Bou59c/6eh2iSMEBKRES74uWPviJt24bneWi1Wrs9OxPnzTcvilOnTsh6vQ7XdRHHMYIg0OU28+V2FxbeFrZty2aziXK5DNt2GSTdAjNgkg+aFGWU5iNmfQ0sshs9wtoG5aJMu6JxEp9WVsOJE1nGeKNRR6VS0SVXB82f3X1YlgW3W6rT62aOep6ny+qa67LoYU5bTV8FglXGqDlOadzNMFXjjeptJIAUEjZsY46tvp9SCliWAKBKxqqAUO//UvZuQIvmV2eoDpj3YQHSQYHX/s8EevuHWsbs/9k26V+uzWS8Jjoo3x8MtCwLcRzDte2+TNw0TeH7vvHZ6y+T6D4GLe+g4MNmyhoWvc4saace+QZq1fidH5d00LYs2lZJkqDT6WxoPvey48eP4/jx45ibm0O9Xmf24ARptVpotVq6U5V5/HE/yDCbcONGfV2p7wOzQwftjl0f4mAbqH1Ije/c6XSwuLiI3/u939v7M78BnufBcZy+TgTA6J8HdtJGKhcREdFgDJASEdFTd+7cGbm8vIwoinDn1u2xuJkbRdeu3dBBUsdx9LiNlmWhWq3izJlT8sqVa3r73Lp1R5w5c0rWajUAFqanp3H27Fl5+fJlbsOcYTenZtYasPlgpQ70qIfIleEtGP9Sfc5GehXvVM/jubk57N+/H5YjsgxCoxSp2veKGkJ6Y5D2yuuq7FFVgkuNA6l+5pfdzBIEoDsFtNtt/eh0OgjDEEEQ6JK7qqyuuT5tuzjQrAxrqBCiuPSqmtagHry9z1o/wDBoH1LPq+zG/PPb2aiSZfMmSJK0r8Rxu92G0+25bGb8qaB3PqMmX/42v76H/V/Ny2aCo+b7ip5T+1lRBs/Nm7fFqVMnpGrANPfpQfOZ/z1JEqysrGxqXveij3zkIzh58iSeeeYZTE9Pj0TDM20Ps1HbLJ+92WnkjUPD75OciybZOKwrc7xqS1df4PiANFy+k1UYhvA8T5eaBwDf9/Gf/tN/2rV53E7Hjh2TasgMczx38+dWrdepblTlM0iJhuG+QtSPAVIiInpqjh6dl9PT03BdF1/96tfZKvCUnTpzWlqWhXa7jYXbdwSQBUlPnDgmVfk7lS0npUS5XF4TJL1y5Zq4cOG8dN0SVAYw9ayXETrIemMumo0C+YCh8R+oDNL8NId9Xn7eC6e/TY4fPyrr9TqazSaiJOzL/MyXhUq7AVE1bl1RsFQFStU0VOOjCmbml9dsjFTZo2ZgVI1DqgKj6pEkyZrtMKyxZtC67P0+uJTiehmkvZ8bz8Aq2v9UKaVB87DV7W8GQ1Sg1Pxsvzu2VLlc1tvS3G5mx4GiLMyNNpYVHX9P2iCgSrKZAdIkSQrHY1Z/M4/b/uNOrlmu/LIsLy8/0XzuJceOHcORI0cwPT2NarXKxpgJ4vu9cuhmpYCtnFvGZf8p6rikzgHjsozbSZ1zR5l5jaLKjQ96HYOmO+tpl8jdbqpMfRAE8H0fcRzj5s2b+M3f/M0d/dynpdlsrrnGUz+3o/rAuAZHB1XOIVoP9xWiHta3ISKip6bRaKBUKiFNU5w+e4ZXZE9ZqVTSY4KdOnVCr/8bN26Jhw8fotVq6fJfURTB8zzUajWcPHm8b1u9+eZFoQIaQRBgfn6e2xLFY8AUKWoEVUE/9dhIA4sQIguOdh9Fgc/1gqRFfzef3+5GHtd14bpu3xiERTf1gx5AL/NWjd+lAnFm5mh+ecz1CkBn/nU6nb6sUfUwy+ya5XUHBa/Xo15nzrPZM95cD2nay7SM47gv61I91/tbuO4jTeN1H/nlya/XzTNL8qJv+bKHhTTNxocNwxDLy8tYXl7GysqKDkyrdV+UATwsA7P7xJq/5bfTk2aQmtPIB0mLqGUoOhfkj/9BAdxRD5AeOXJEzszMQJVlV51p2Pg/GRzH0SXQAehz6VYDXePQmLfeNQKPj8ygKhijSn0fmN9tCrc5bUYYhnqIgiiKIITAz/3cz+H69etjsSNNT0/Dtu2+63ZgPM79O6Xo/pPrizZqHL5jibYDA6RERPRUXLhwXpo3c7OzswySPmWqvKht25idne0LfN68eVu88cZbYmlpSTf627YN13VRLpfXTCuOY10is9ls4pVXXpn4bWlmCAzrzZvPHskH0vLP522mQW1Q4HSzGaZbpcYTUgbd0JvP5zNL1fpR4xOZperyy2WuUxWUUsFRs7SuCsypoKR6FJU/VtMbtK7y85Evx6rK9+YDsGq6KoicDzCaQd6NPPL7XX5fNKdtzvd23SQX7d8AdBA4CAKsrKxgeXkZrVYLQRCg0+kgiqI1Dcj5bVqk6O8bye590uVS23Rh4e3CD7lz565Q+9CgYMigIKn6jFEPkFarVVQqFfi+D9/3n6jEKo2uvrGbc8fzRhV1cBiHRryi6wIeG4ON03YvKv9Pe8tGOuvt9Oev97dyuQwhhM5E/rEf+zH8z//5P8fmBDIzM6Ov71WgVHWEpGIbObfwfEMK9wWiYiyxS0REO+706ZOyWq3q4JFt25BSIhvLkp4mdeMkhECz2cS5c2fkpUtX9I319es3xfHjR/X2UkHQ8+fPyosXe2ONPn78GFNTU/pGnaV2exkCg7LKTEVBn40EgpS+mxuZjUFa9PeijMeNNFTvRGOtme1pZnMmSQIYQULLshBFUdY4InplTYsCpGYQ0hxf1FwOc7lVkLLdbqPVavUF5cxsTfMz1XullJDApjJ880Fa9bz6fz6wqV6jSuyqz+7fL1TAbfANrtmQtPZGWGBlpaVflw9GSymfMIsUUH0vzfOMScredlhZWdEBYc/zUCqV4Ps+SqVSYbauuQ7MY2y9bV70XpE/WAqst33NjhDrUWOT5oOk+eOvaP7HYQzSWq2mt2mlUgHARplJojpDqI4CO9HpZlQxOLY547a+xmlZxtGw7bPb57FOpwPf9/Gtb30LP/MzP4Mvf/nLY3ViLZfLhddJ2xUg3e3tt1PyQVKiYbifEPVjgJSIiHac53m6sVwIgSiKIOMEvu/j1JnT8tqVq+N5t7LHmOM1djodlEolTE9P48UXPyK//e039Da4efO2OH36pFTlQB3HQbVaxfz8Yakypm7evCleeuklmaapLpE56TZT3sjMMFT/3+xnySzapJ4Y2HO4KAtnNxoIzCxOYfVnFMpucFAFUWWS6ACpKudsnkNUr3I1DbNRoGjZzKw/VeZVldI1A6JmGTxzntU0sMH1pubD6461qQJ/ruvq4OOg7E6V4Wm+Rs1Lb54SrBcgHTRP3Smh0ZjSy2mW81XPbTV7cb3tAEAHpW3b1hmGnuehUqkgSRK4rrsmwKxsJjidf+5JlqPoufWyR5U7d+6Ko0fnZVEQuiij2/xdnVdHWalUguM4OvgNsDFmkqRp2jeO81a3/Ua+V0fFOCwD0aga9ePvW9/6Fv7jf/yP+OxnPzu2987qunTUt9XTwvVERLR1DJASEdGOeuGFC1KNaxdFEYAsUJdCsOTeU2ZZFoIggGVZKJVKWFpaQq1Wg+d5ePXVV+TXv/6a3hhXr14X586dkaqEkxm0UTzPQ5qmcF0Xvu8/9eXZa8zgWpIkuqSsCrLlS43mg2Aq6Jd/rQqEmjfAlmX1elMLgTTtlUU2P8PscW0GZ5I46QvmKmpbx3G8hSzCYtVqFZZlodPpwPN7Y5GmaQrZzTBS82wbAWQVIHMcR2ccmqVoVTafyi4191czw1SVdl1dXdVBfbWtwjCElFJnbuYzF80MUjWf6jPVMZKmKXzfR7VaRalUgud5OhvQ931IKdFqteC6LqSU8DwPlmVhaWkJpVJJP6emq+aj6Gdv04s1gYOizFczg8uyLIRhpF+vAljm/jI1NaXHZg2CQI9PrAKXSSz71w0SPT/ZNlGx+7UdANR6TJIEnU4HKysr8H0f5XIZcRwjiiJd1tucf3N7KoOyR82gdxG1Dc3gu2VZiONYP6emrY4D9fp2u62zQ4dR+4naL9W2B8Sa7ZT//dKlSyP95VipVGDbtt63zO23Hd/75nRUtQMz85p2V75kuNl5ZyPbP39MqGmOk73QcWkvUusi37Fy1G1XFrX6/sp3Hholt27dwje+8Q0EQYCvf/3rqNVqugPZsHWUH8YinzU37Ps5f17ZbOeL/FARRb8Pm//1mNNJkgSLi4u4c+cOLl26hLfffhsXL16ciBOFur5W23e7vt/H8VxrHgf5ig1P2hF33OX3g0laP+Y9ZlGnaqJJxbtIIiLaUSp71CyBKYSAYztYWVnB1ctXeCX2lJhBljAMUalUEIYhLMtCpVLBxz/+qnz48CGuX78pAODSpSvi7NnTsl6vw3VdnQWkdDqdwqDFpCrKBByUgZYv05r/fTsb0vK/78YN4QsvXJD1en1N1qfO1ERvuc3zRD6bUwVBzcCnGQgryuBVGX+qR7rKHFVZk2YwLR98U+8FuqV17ayhX5WBDYIAQRCgXq9j3759cF0Xq6urfWOkqmC2bduo1+t6PMYHDx5AiKzUtQpEmmOVrm9tqTFzn1PBWXObm+M52bbb1/icD15EUQTXdXVpVDVea6vVQqvVguf6fa8XohcAUeMTF82fogJaKptXjUEaBAHCMNTrq2ifLVrmQf9/UuZ0ijJ9h2WPKgsLb4uTJ4/Lzc7XRsp073VqX8uXkGbjy2QwK1CY48gRTSqe/3quXLkirly5stuzQXuQeZ0P9HeYKOqsS5ntuv4lIppUDJASEdGOKpVKCMNQ9+g1b3gePXq0m7M2cfI3lc1mE4uLizqzzfM8zM3NIQxDXUr38uWr4vnnz8lyuYwrV671tez4vt+XTTfpigKj5o29aVDv3qLSm0IIyA2UU80HxAYFXTcSgN3OG+3v/M6Py0ajgampKZ2puV6A1GQGPFWjiVkquii4qQKs6v8qOBpFkQ7AmeV1iwKjg35Xn/n48WNYloWpqSnMz8/rzEIpJWZmZvTxoLLnV1ZWsLy8jFarpTNYbdvG4uIiOp0OKpUKoijSx2F+OwzKcCgK3ilF45yqdee6WQavyngtl8s6i9P3fdTrdURRhE6ngyRJUCqVUKlUUKlUsLKygnYr6N82woYQQIq4LzsaMEtJq9LA2fZWWb1hGKLT6eiHChSrDONBwdb11st2KPqMJzkuzP2z9/71G8mHB8j3PvNYNffF7aDWY9E5jvYGtf3NcuhPalhHiVHDfXVzxmF9bfTai2jSmfdE+fujjYxDOolBwvU657ITMxHRxjBASkREO+bs2dPSzPRSDd5JkmB1ZRULt++wleApUkElAAijCEtLSwCykscqiO04Dg4fPoyFhbf1+956q7jUoxofRgWgaHCQdD1FDWfrNaIVBWnyr81PU+ae38gybIcjR56Ts7OzqNVqKJVKCIIg6wEu+wPrluiVBVbzmc9qNLMgVYkzFeA0g81SyjWlhs3MUTMwaq4v83PU5wshdNnbOI6RIpt2rVbD9PQ0Go2G/oxqtarLgQVBgPv37+P+/ftot9tYWlrSx9fq6ioqlQoePnyIer0OKSUWFxeRJAlmZmbQarX6xl7ayP6U3+9UFmd+uVSgKo5jHTBVQSzbtnW53X379mF2dhb1el0HbFVm6szMDBbFUt/4ggB0ICy/TtejMmZVJq7K7I3jWGfBqs8umt5ONjSb624rx0O+1G82vfXfMw4BUjNjebuDAmzsGw358/J2TnOUMVC2vqLz/qivq/x3MLc/UbGi7wp1bbndQ3+MC/UdO2hoCZ5raKN4fU2TjAFSIiLaMb7v6xKWQBaIU1lJly+O9vhqo8gMGM3NzeH999/H9PQ0Hjx4gAMHDuhgpxACP/iD3y9v3bqly+3mXbhwQXqep8cp7HQ6T3VZRt1Gb1bXaxwcdBOzlYa3fLnSrfJ9Xz/MYKeavn6gF4hS5VXVa/NZkGYgLh9QzTdCqizVfGldsxHBDMaaJbzUe13X1cFct+Sh0WhgbmafHg9JzW+SJLh37x7u3buH+/fvIwgCxHGMSqWig362bWedRWwPrlMCpAXbsvDMwUP4/f/3f+36OfHkiWPS933cu3cPc3NzOgg8MzODcrms1+3U1BTa7TZWVlZ0lqlaD5ZwkMpBHSYsAL3xZlUwV5UVVh01wjBcU9JbMbfzZjoWbMagbO7tmOZGjEOHEzNACvR//7BE3vhbr7F2o+8HxrNhdxyXaSeNQzBxvQoegzoAEU2a559/XjabTZTLZd2pGkDffcEwG+kEOm7M79snrXZCBIzn8UG0UQyQEhHRjvE8D1EUAej1/ux0OgMzEmlnmUGJe/fuoV6vo9Vq4fTp03jzzTfxyiuv4P/5f/770G3zyisvydXVLCuu1Wrh9u3b3J5dRQ15RcGbjWaVqt83UmLX/CxTUbmlovnc7uAoAFQqFViWBc/zsLq6qs8D5g18mqawjIYPs2HczBhVD3Ma+WXMZ2iooP+wcUfzjQvqM1SHASmzcUdrjTpmZ2ch02y6nufBtm188MEHuHXrFj788EMkSaLL2KppSZmVsVbB3SRJcPHy3jsPXr9xS8/T0SMtubCwgFqthvn5eezfvx+qVLLr9GeLttvtvrLCwPoBDtmtkqaC1yqD1HxUKhW9TTeSMb3TnrQR+0mOpXFooDBL65q2Y9lYMm7vU51KVLY+y+z2jEPA72koqvIwyvLXJ4NeQzSJPvnJT8pz587h1KlTmJubg+u6+vq5qKw+FWNwlIjoyTBASkREO8ayLERRpMtdxnGMlZWV3Z6tiaYCQdPT01hZWUGpVMLv/d7/FABw69addd/76quvyCAI8Npr3+IdaoFhZdOGNYrmS8quR9/8SgkMmWb+92GNjhstDbwRKsjlOI4u65plUlp6X8xncJqfLYTQY9mp8exs217TWKKCm+r16jlVGtfMTjSDo/kMVTOwqrJCVSC0Wq2i1shKzkZBjEajgdXVVVy6dAm3b9/WJWhVGV3XdREEAYAsgHj12o2ROm5u37krAGD+uUPy1q1buHv3Lubm5nDy5Ek0G9OwbVtnlQohdJA0TVNA9Gfnmj+zcUkBSOgAtgqSqnFIVRlkoPi4yO8nUko9qud2NQwVBeCfpOEp/97s/SO1KzyR/DG1nQ2bG+kIQrtLnXejKNpSJuk4Y2N/saIOMKO+rgZVPCAi4KMf/aj8nu/5Hpw5cwaHDx/GwYMHdZUWVXmFBjM7X6yHQWYahtdqNMkYICUioh2jLsJV5kAURbh9e4FX5rvEDCaokrgqw3cjvv7117jt1jGofJr6W9HvG5nmk8yHafOBg+0rf6nGr1TBRlW61pIFZXaNQFo+i9Qs16mmkS9RZ46xa2aFqvK2ZnBUTTtfvlcFYIMggOM4ugSs53mo1+so+T6iKEKj0cTCwgJu3LiBDz/8UAdH1edNT09jdXUVb128PPLHzMLdd3Sg9I03L4r79+/LT3znd6NaraJcLqNSqegg53r7WS+4acGyJCR628sMZufLIA8KhqXd9ybY+Hi/G5Xfj8znt1IytLefr//acWjAync8UM/RZDAz91UWaT5Df1IVlVil8TcOgV6inXD69Gm8+OKLeOaZZ/Dss8+iUqn0HSub+c6YxPOpeT/DzmJERE9msu9OiIhoRz1eXsHUzCxqjSaCKMY3v/lttgzskiNHnpNpHMG1LTiWgJQCpVIZUnKTbBfbceC4LhzX1aWh8mV11biWwhaQQgJQwRKJNE2QIAVsgUTK7KGyHQUgbEs/1LSgb4KzzLwkSRFFZpakgBA2hLABWJBptu0tC5BIdNCwl01pA9KCZTmI46332E6SRPf+juMYnU4na+iQErI7/8JYNyrbVAUrhW0jBZACgGUBloUUgCUBkUogSSFSCUtCP5CksCEgUgmRStgQ+vXq/7AFXN+DcCwkSCEtkT2668EteUhkCgkLrufj4DOHICwHcZTCL1WwvLKCb33723j/3j14pRIc14UEEMUxypUKOkGEx0vjlS2vAqVLy6v486/+BT58cF/vj55fQr3ZgF8pI5Fpb/sZ+74ZMIvSBJbjQtgOwjhBGCcIohidMEIYJ0ikRJQkiNO0b7snUkIKgbQgiA4AKSQSmQKWgDq1DSovnQ/GA9DHp/pd7bvq9yfpfZ/v2T8pDeRq3ZlZ3/ms4EGPjVCvU+uWjYJ7y8rKCoIg6Os8sZngaL6UuuqsMg7bOJ/pw8DZWqqKg7LR8Qf3qjiO4bqu7igG9I+h/iRs2x6L8aqJnnnmGdTrdR0cLZfLuoMlgE1VISga7zf/UJ0s1TXKqB9HqnqObdt66A+geIiVSZfvnDQO1xSbk0IICSBFmsbIvo5SxHEIAFhYYCIDTS5mkBIR0Y65+OZbYmVlRUopcfcOL7h2k5lNlyQJSqUyWq2WbrCmrdtoiaPt/LxBzxWVctOPdcp7ynR7s1rMQJT5fxSMqTro88zsUXMM0kGftZFSdjqT1Qhiq9/NgItlWWg2mzpw63keHj58iG984xuI4xjlchmlUkmXhf3Wt14f+/PcnTt3BAC02225uLiIw4cPY2ZmBp7nIQzDNcFAM3iVzwrNZ5CaY8WaGb9FGdhCCKjdVYrNF63dy40iqrTcKFPHz6Bji8abygbPZ5DSpDbKEsAABVHe2bNnZaVSgeu68DwPpVJJdzJVeNysb9C9DxERbdzo330TEdGetnD7Dq/S9wDf93WPUsdxEIYhyuUyXNfd5TkbH2ap1qIb1O0c+2W96RQFRmHeNMv1g0P58T2f1Pz8Yf0hURQ9UYOwWfrWHIM0P79mo4B6fX4Z+v4OWwdIzW1mfp4QAq7rYWpqClEUwfM8xHGMhYUFrKysoF6vY2lpCe12G47jTNyxdO3aNeH7vqzX65ibm+sLjBZlxvSX2Ozft9I0RRRFCMMQQRAgCAI9ZqzZUDYouCal1BHSjey3RSV591LD0jgESNWYw8peWbf0dKgOD6pcNq3FIOlkmbQqAkQbsW/fPlSrVdTrdczOzupsyPz1+3bdmxQZ9XNxvqMnr7c2hh32iMg0+nffRERENFSpVIIQQmeMlstlLC8vs+FyGxWNufcUPrTw6XwAqKh85bAg6VYtLLwt5ufnpRrzVgUkZbes7nqKAp6DskfN16rXmQHS/DhGlmVBSLEmkJ3vfW3bNiqVChzHQRAEEELgnXfewXvvvaeDpY7jQAiBcrmM1dXVLa+zUfPGG2+ISqUiXddFvV6HEAKNRgNLjx/1ldUFevtUtm3WZq4nSYIwDHWgNF/+2ZzGdhxfe7lRZC/P20apbGIzS1hhw934U1nh6prDPBdMOmaQTp6ioAXPg0RAtVoFgG6nxP4OcaadCmb9/9n78yhJrrPMH39ubLln7V29V1VX9b6o1ZJlSS3Zso2XwUasBwM/8MDAzBcGxrPAOYPHgLEHYwYYhsUYg48xGMYYjME2Nl5Bg40sy7KWltTufVWrpd6quqpyje3+/oh8b96MilyqKquyMut++uSp7Fwib0TcWO773Od9e+F8HB5/yg9ZWF4pgbmbURNXFAoFoQRShUKhUCjWAYwxOI4DIBgMzszMwLIsZLPZDresd5AFPfo/sDJiR02gLcIFJw/4w6Joo5o08vvtGCySKFIulyMdnfXWSXZ2NhJH5e+Sy9Q0TVH/MCpYoGkamN94djXnHKZpIpFIwHGcoG6m4+DSpUsolUqwLAulUgnZbBbT09OYnZ3FuXMX1uXo+vHHH2czMzN8y5YtGBkZQV9fH3Rdr6k3KQcgNE0Twie9BlQFFUrNGXae1TueerG+Ui+kPnccR7i95X23mDqUiu5GrhvXK8dmO+iFgLxiadQLxivhQrFeSSQSwjFK94jyOXIljouaUg2LqG+6VmmUYle5JBUKhaI1lECqUCgUCsU6gAZMsVgMtm0jk4kjn89jfn6+003rSaIG9CudYjcq8BZ2kIbbE0WUiLoUKMUuifM1abNYNQVrs0crDlL6jGmaME1TBD3CQYJwLcQo6LuWZUHXdZEqdGZmBjMzMzAMQ9QdLZVKOH78xLqPap4+fZppmsYTiQQymYyo8SqLYbLYGZXalmoky7ULowJXUS7opYikKhi9sti2LY5ZeaKCCtStD8LHqe/7NZNW1jNR5y35vfW+fXqVZqkv1b5XrEcowwxNcgzfK66G67rb70uatV+dV6rUO8+qbaRQKNT0XYVCoVAo1gGGYQihYn5+Hhs2bMDx48fZE088oUYEK0A4CNYoILqSvx+VPrZeO6Kcpsvh8uUrjAQuci83E0PpM/J6yGlzwzVIwyKq7CCNSncsi6NRvyk7WC3Lgud5wvF4+fJlIfjZtg3GGEql0rK2US9B7k/f90UNUTm9ZjjVF0Gv0/dd10W5XK7rIm1HEKPeZIK14uwK19ntRmzbFs+VKLb+kOtGq31fy1o5zyhWl3ZdvxSKXoImUNJEGiIqI047iJp01+3n41bXQZ1/FqK2iUKhIJSDVKFQKBSKdQBjDLZto1Ao4PTps+z06bOdblJP0umBdt20tXUEyDDtbjsJpJ7nCdEnEBmjhduwy4z+L4ujmqYBXm07SfjUNA3QdTDPWyDCRYnF9V4nodUuO4jH4yiXy7h69SoMwxDrkkwmlUAqcebMGabrOi+Xy0gl4yJNsWEYMIzmQw65ZiGJo5SKN9wv2ymSrkVa2V5rHdu2O34+VHSO8Dm7mXtOoehlVL9XKGqZmJjglmVh+/btGB0dRSqVEhPooiZ0Kod1Y+RJfuq+a/GovqVQKLp/9K1QKBQKhaIhY2PbuGEYcF0XqVSq083pWVzXRSwWE4Jg2K0YNWtZk9KLkvOxnihE36HlAwBCAzp6Tw4k+L4P7ldT2nKfB2KlUxViyBlJjj25RuRy0HUdt2/fBmNMOATj8TjA/Jqghy+1z/d9MMMQ24/ESmqr53nQOFuQurMmBW/IrUgOU6ptpOs6bNsWy6fflkVcACKV7tWrV8W2pN+wbRsDAwPL3ka9xMmTJ9nJkydx8MA+PjQ0BMdxkE6nxf6V+yXtC6AqcPu+j1KpBNd1Ydu26I9y4ELTNPhYeHwRYReC/P+o96hereu6ME1TfIbaIx9Huq4v+rhoReANTwrQdR1jY2P80qVLXRuxuX37tth+NOnAcZy2iL+O48A0TfF/zjlc1+0JYblXSCQSiMViIu35YoLb4brF8uu9EPitlzpcCchVGGNwXRe6rsOyrBpHejcip/en58upn0110Xsh24Bi/fGqV72KT05O4tChQxgdHcXExASGhoYi+/NKnBPp+KOxSbfXRafUxOFxlXwPpgigcSZlA7IsC0D3p1kGlnYdAXrn3kqhWC5qFKlQKBQKRY9DwUlKFapQADQYjHaVtmtATSlTSaCs/m7j+qdyIFF+CFHMr34+sp2V5VCAICw++T6PXD49jIpA63vBdwuFgvg+CcmWZeHixYvL3ka9SC6XQzKZRDwer6k7GLWvwnVy5f1G+26xA/d6brXFCDRR6YBXK4hAkwK6GTnNMk1GaJcDhK5pruvWBDiVWLB2oHNpO1PsqiDv+oDOE9R3XNftdJOWjRIpFIqAyclJPj4+jqNHj+L+++9HJpNBLBZDIpHoyESnXhCG6jlH1TknmqgJWGqCnUKhUGcBhUKhUCh6nFgsVjNbUrE24JwvcICK11eBYGDYvCbocsjlcuCcwzAMxGIxkXLRMKNnrYb/36hNUSl0GWMLxFFZIBUiKGoFUXLukpAni7m+72Nubq7mN+W0v4qFXLh4mfX393PLsiJFRqDWNUmvk3uZhHXP8yLdVotlYR9qfXn1xNKVxDRNJJPJVfmtleLixYtsfn6el8vlFQl6kitVPl5VMHDtYBgGLMsS9c+VSFpFnhSiqA/ta9u2a5z/3Ui9tP4KxXojkUhgaGgIW7duxdjYmMi8o+pVLx15vCNn5JH/KqrQNqGsMHQ/qVAo1jfdnUtAoVAoFApFUyzLEilflYN0bdGpAGk9EVJ+rR2cOHGKTU9PY3Z2FoVCAY7j1E0hHCWU1kszHNW+eu7EcMBAdjPKqXfDrxOe52F+fr4mVTJjDI7jdL2ItZLIfSq8b+v1L7kOKT3C9WiX0o5G/69HvT66Wg7SRCKx4r+z0ly7dg1zc3PI5/NwHKdt5xVyphKUklld39YOpmnCsiyYptnQQd6MqGtVt6PS2TVHFkQpBXq3o8RRhQIYHh5GNptFPB6HZVlisqF8r75arPbkt5UifO8cFkkJdd2pbgO5lAagzskKhUI5SBUKhUKh6Hl0XRe1nBRrA3lATs/X0sBVrrW5XM6fv8gA4ODB/dx13ZoaqrKzk/4fTokri5f0GkNj0SsqLWpUoD0qhS9jDD6vDpgpVSil9aTUnkupR7meoG1K+1QW4uXNFhZSwzPh5dp8ywlgdJO4whjrCYH07NmzGBgYwODgIJLJJGKxWFuWS85k2YmnUuyuLUzTrAmAL+bYazQBpluO4UYogbQxjLEaoYQxhlwu18EWLZ+oyVcKxXphz549fHJyEhMTE9i3bx+mpqawcePGmkmHnTg25LFGN0NZVxzHgeu6dTOvdPt6toN6Y+5isdihFikUirWCEkgVCoVCoehxaCBAIoVi7bHaKfeigs1B0BYrNovbtm0xeNf06i2o3JawW1BOgUsPTdMAqQap/Pl6/TsqVW84xa4sknK/utx8Pi+OHTqOdF2HZVnYsGEDnn/+223fVr2EvM1ou8vOUKBWvJRnwkelC2vlGJFF8aUeU1H9abVEVsYY4vH4iv/OSvPMM88gFothamoKqVQKg4OD8DyvLUKmfN4wDAOcc5VCfg1BadVN06yet5dJN01yaMRyz03rBXlyzLVr1zrdnGURlWJXJnw9VCh6iX379uENb3gD7rvvPmzbtg2JRAK2bYtsLKZpdqzfy2n6uxXHcWDbNmzbFuUp1PWlMdTf6N5kenq6k81RKBRrACWQKhQKhULRw0xMjHEaBFCNRcXaoNM1SMO/GQhZgZh14cIFNj4+3taGRNWhCztE5bqejQKKUbN/5c/Q7Gk5wEq/oWkaGGeR4ig94FV+AxDpdWk7kSCjHKSNCbtyWxGvibCDNOwWbEY70+N2KmhnWVZHfredPP300zAMA4ZhYGhoCDt27GjLMSOn62WMwbZtaJrWNoeqYvkYhgHTNBfUIF1OOkMlHK0fwteEF198sYOtWT7tyIKgUHQryWQS2WwWAwMDyGQyoka1pmniXodSaa/0vU/4GtQLzm7HcVAul0E13+U0u4paKK0u7XMaT16/fr3DLVMoFJ1GCaQKhUKxDtm3bx8v2WWcP3tOjdR7HErTSCnLVHBmbcBII1jG7vDpuwxAi5qD3+D3OOfg8OGj/W7WHTvGeSwWEzXpdM0AmA9dM6HrGnTNBNO4EFPIyRoWxaoiV+MNF5lSVwM0nUHjDAbX4DRIsRuk8dUBeCgUCguCKb7vo1Qq4bHHHlcHVB1oX5K7j7ZbVJ3IcOC4KtgvFEijgj6sTleNTK/cQrduJMyvloPUMLp/mHbq1Ck2NDTEx8fHMTc317blmqYpnpOQnkgksHfvXrzpTW/iX/jCF9Rx2WHkySftOGZ6LcVumF5Yr3YRvlflnOPWrVsdbFH7Uftb0et8z/d8D9+9ezfGx8cxOTmJbdu2ob+/X9zb0KRdx3HEtaJTE8O6/XiUU+zK4qicTrbb17GdyGNK2j63b9/ubKMUCkXH6f6Rt0KhUChaZnzHBGeMIVfI4/LFS+pOeR0Qi8XEQKldae4U0dDsZ3JAykFQeTBG6UZNLXAgMjAYugF4HNz1YWrBc+YD4AwMWkUEqCyDAbA0cAZwcDCmAdwHrzwYqwpDXuUf4MOHB858cOaDcQ06Kk7NStt0Xa+Ioz62b9/KL11a/jli3749PB6PwzAMJBIJxOPxYN0rzqJsth8f+MAH2K/8yq/wU6dOYGTrKGbnZqAZJhjTxEOe6atpGoIiljxYZzD4DOAaA9cYGAuEYJ8BHD7AODQGaODQwMG4D00D4pYB37VhGQZihgm3bMMwLHgeB3wGDTqsWAyFXBExyxLinmVZ8H3AMMzGK7/OcT0PsXgctuPAqghaJER7nINpGnzPC+q9cgbOAKYx+ODwweH6HhzPhet7Qb/UKk5gzuGh4jTwgnMbzQbXwOC7HhgHNDDoYNA44HMAPgeThFdZrI1yl8pi6HImC5BATIHAKPdzuCax53nYvHnzkn9zLTE7O4tCoQDP88AYE+mTgWhX+WICebQd4/E4PM9Df38//uEf/gEvv/wyLxaLyOfzyOVymJ2dxcsvv4wrV67gxo0bePHFFzE/P4/p6WnMz8/DcRws5Xy3fft2rmmacMNQzc10Oo2+vj5s2LABw8PD6O/vRzabRTabRTweB50TDcMQDti5uTncvn0b+Xwezz77LM6ePYtHHnmka+/T6DxPdYgXG5yVHR5y9oteuIfRdb1GBHRdtyfSPLYLcvjIKRBfeOGFDrdqeZimKc57UWL/Uo8Px3Ha10iFok3cf//9fP/+/XjwwQdx5MgRZDIZeJ6HZDIpPkMTneQJT6sBXZPoua7ryGazq9qGduO6LjjncBwnsjSFoopc5oExBtd1YRgGbt682eGWLZ96E8lqa+3Kk498MKaBcwag+++tFIrlogRShUKhWEdcPH9B3SmvUzjnSiDtIGFRROPV11v9PiG7QDmL9lKGA2889BrnHIxXxFaJwEXq4fLlK205V5BoINejMwwDtu3C8zi++c1vAgDe8573iN/7mZ/5/2rSQlMtHblGpYaF7sBwSt3aOqNBIMTQAE8H4Acaa/CeV61v6gff15iBUqkkBB0K2Pq+X6nz46pgdotEOUObEa4hWuMgRa3YotMRsIhAUDh9syyGNjsmVQ3SxeF5HhzHETVC2+kQkfeZ7ATYunVrTb9xXRelUgn5fB6lUgme56FcLqNQKMh1SznQmkBLnwlP3KB0sr7vIxaLIZlMIh6Pi/OeZVkiPbfc52gblctlOI6Dhx56CBcvXkQul+NPPPFEV963LbfOZtjhQfTCeVfuqwBqrm2e5626YLAWkfuN4zjK3aNQdAG7d+/mW7ZswZ133omDBw9ifHwcw8PDYIyhVCqJCaJrAXlSWrePjeVJf2EXKaCEUhnqfzQJhxzNMzMznWxWW1HlXxSKpaEEUoVCoVAoehgKwnme1xMpG7uFeoOTRnWoWhFmFuPEYSxw0Pkc8CUhISptqNzudtatIeGAxHnqi5lMFtPT0+jv71/wnZmZGQz0Z4U4Ks+GpvaFBefwc7m+aLjGKA2OdQTuPgrYGIYB3Q++y7Va0bVcLoNzjkwmg3Q6jVyu0PUBldUgShwNRHge+ZkoooRwkV06JGq2mgK3njBazzUa5fpcSRhjyGQyq/Z7K4nsmnIcB/F4fIEwJLPYQF6UsE01SUmIpNTe2WxW7Es5hTedp2ShqhF0jpH7ivydsDM2an3lPk1u0kQiAc45+vr6sGXLFgwNDS1qW6wl6Fwvp8leKvJ264X7GFkgpQlAcop3Ra1TOJfL4dvf/nZXbxi1bxW9zvd///fze++9F7t378amTZswPDyMwcFB0e9pclCnCY+jOOddX/O9WCzCcRzYth0pkCqqRKUddl0Xp06d6nDL2o88aTjqdUJdmxSKgO4fYSgUCoVCoYhk585JLjvf5DR1itUnSphsJHjWC6hpLcSZowY+jQZA1JZ2i0A0YJfFLU3TcPv2nKjjGWZubg6modWkwQoLEJE1RqWHLJKGxVEdle9WUhrKD02r1sc0DEOIO5TOzjAMlMtlfOMb31CjySbUSzEdbPvWaiPVT327cu2W29spfN9HX19fR9vQLs6cOcPe8pa38PAxvJIBmXCwMxwkapbOdLHXSVpePeG3nhDc6PWBgQHEYrFFtWMtQSnnlyqQyvuAMSaW0+2BbADCSUXOZk3ThMu4FwTgdiAfGy+99FIHW9IeoibeKBS9wH333ccnJiZw991348iRIzh69Ki4f6YJIMDaTo/e7Rk7ZmdnUSwWUS6XF2TdkVlKuvtegyaM0z1gkBXIxjPPPNPZhrUBNdFKoVge6g5coVAoFIoexbKsmpm7vu93/SBwLdNK4GsxDrdmrzMePKJy7GpYfDWRRu7WpWLbtnCOkVgaDOCCgem1a9cWfKdYLGJ62hdpBik4TgFyTdPAvaqQucANGBKH5YemBbVXPc+DxliNcGoYBjTHFa4eACLYQMHrubk5PPvs82rkuUgWuDFRO0GgnoszLPRX31tekDkqdW/4bxSrFXjwfT/SXd3N0CQDqrcY5bJs17aNSu/aaKKI/NlW2hDVR8ICSNQEDjqH1RNT6TzXCyKg67pwHEeIpItF3p607RhjSCQS7W7qqnP79m2USiU4joNSqSRq0QK94ZBtB7S/GWM4c+ZMp5uzbFTQWtFr3HPPPfzo0aO49957sW3bNvT392N4eFgIT3R/LZ/L15pAR9fhVCrV4ZYsj+PHj7MDBw5wStMv13CWWUvbvlOEhXrbtlEsFvHcc891/cahjEmNJulF3b+uxARphaIbUXfgCoVCoVD0KJ7niaCMZVkol8vIZrOdblZPU8/xVu//4aB9VBBtKQPaeu69qGXKQQsKaGwf38YvX3xh2YNFEhh1XRciY4CGXC6H8+fPL/iN8+fPY/u2Lejr66tJsVsvyN6qOCqLpPJz+UGine/7oq1yOk6Vsqp1qF/5vg8G1Gxz36+mXaXXosTSZstejfa30p6VoJcE0nw+X+OypONoqen2mgVZo4T1RixWvIhyg0Wdy8OEXZG0DHpO5yFyFpbL5ZbbtNaQBdLFCtBRkLDc7YFsIHD7FAoF5PN55PN5JBIJkQ6a/q5naDIFcezYsQ62pj2s932q6A3Gxsb4tm3bMDk5iX379uHw4cO44447MDAwAAAwTVNMMKTrGUEZjTp9LESNsdLpdIda0z7m5uYwMzODubk5URaEWGvCdCeRsxPR+OPcuXMdblV7kGvcAwvFUDlzD9BcOFUo1htKIFUoFAqFokcpl8s1aelyuVxPuC+6mUbB81aC+M0GubJzNModFxQkrb8MclOapoltY1v5C5euLGtEfenSC2x8fDun36fUT6dOnWHbt2+PHI3F43GUy2VRSyfsQJIFlkZOW1kArfk/r7wfkX6Xgjn0GrXbdV1omqaOn0UQ5dIU+4MvdI2Gn4e/H9X3o9yfi3FyL3AfY/XrjUah63rPpNgFgKtXr8JxHBG0o2MyLJC2GsRr9TPh4I/s4Fzs8qKIqu8kJgU0mewSPo/J7xmGgVgs1tVOUtd14bpuTR3SVgkLZEA1PXsv1Oa9fPkym56e5i+99BLS6TQGBwcxMjIS6fxY77iui2984xudbsayUQ5SRbfzute9jr/yla/E93zP92BiYgKZTAa+74v7YpqUK7vg5ethWDDtNHS/qGkakslkp5uzbK5duwbDMLBhwwbMz8/Ddd1ON2nNQvuexMQvfOELnW5SW6BxbNT9kxxDiBpLdXrco1CsBZRAqlAoFApFj3LhwiU2ODjI6Ya4XC6vqcHpeqcVwXM5gfvw8yhnK0ets0d2WLajr4yPb+eUPlB2aO7YsYOfP3+e7dmzh588ebJmJU+dOsNecfdhTjXayIEkr4/c5hoRjnPhIq3rIGVa5LrKQqppmiLtEmMM8XgcmqaJmfGK1giLm1EO0SiBNEocFfvYj06Ni0UM7usJo2sliK3rek8E7IgXX3wRxWIRc3NzSCaTSCaTKxqMoQkN4f3bzLFKwdxWnK31Ak1AtDM26vwbJRDLk0G62UFKdc+j0h238t16159eKRNw69YtvPDCC7AsC47jIJFIdLUg3k6orziOg7m5OXzta1/r/El5mayF64pCsVi2b9/Oh4aGsHHjRhw6dAgHDhxAMpnE0NAQGGNiAmN4cgdljgn3e3p9LSDfm/bCuffmzZvgnGPXrl0oFAo1Aqk6/1TxPK+mv7qui3/+53/ucKvaQ7NJVvXuw5RAqlAEKIFUoVAoFIoehm6WHccB5xyO43S6ST2N67pIJBJiVjIFeWUhlHNeEzxY4K6Tgu5yOqoooZNzDsY5GAC9IvTJNUg0MOhMA8ABpsFnVAsvCFzrRuAWZYwFwWxw0WbDMDA2NsYvXbq05JG1ZVkwTbNmW3ieB8OwMDk5uUAcJSiwrmkaTNOsCbTI4m1Y6BTiaHg7aBoMw4DHPbiuA13X4VZmu1PaT8uykEgEqXw91xN14TzPqxR7VUGGxcCk/mhIATHf96GBgXu+qP0KAByV2dwciJkWDE2HBgZD02HqRtCPebX6qGEYNY5fVI6lZu7A4PjzhduBgkiapsFxHCHAyAKNruvCzbzYPiDXzl2MwL558+ZF/c5a5umnn2YnTpzgu3fvRiKRQDabRV9fnwhKyo7LdgRQl1rHsRURVf5sO14HFtZMpX5CdZi7kVKpBM/zhIt0MfvEMAw4jiOuHUC1FvWWLVtWqsmryrFjx5BKpVAqlZDP55FKpbBlyxa4rhu5rXotRaK8nrSvKXAtnwO+8pWvdLKZbaOvr0846MNpRpeSfrqX+oJi7XLw4EG87nWvwyte8Qr09/djdHQU2WwWtm0jFovBMAzRp+n4BWonCdV73mls20Y8HhfjtjvuuIMfO3asaw+sy5cvs8uXL+OOO+7gc3Nz4j6C9o/v+119T9Eu6F6TrrsvvPBCT0zCAYBkMinum2iyn3w91cNjsUpJBzHWVSjWOUogVSgUCoWih9F1HeVyGY7jIJPJoFgsdrpJPctSg1v10oauJLKoKDsoOfcbtm2x6Lpek/KHRIBCoVS3ltzOnZM80jkoEeUoDTtI5fWkhy+JpYGgFohqhmGIwWTwuYUBTDW7dvHUc4zKwnX49bBzLKoPRvYNHu0sDaff7fR+DCZFNP9cMplc9gSFtcQ3v/lNpFIpTE1NYffu3TAMA/F4fIEYtJYCqIqlI9cglWtILzaNMn2ezg29UCsOAM6ePcsMw+CFQgGZTAY3b97E3NwcBgcHF1zf5NTQvZAFxPd9lEolJBIJMQkKqO5rEsgNw8DHPvaxDre2PciT5Tp9DVIoWoUyqExMTAiBTZ6cRp9pV9aZ1YTOO7quw7IsxGKxDreoPdi2Dc/zxIMg0azb9lO7oUk4qVQKnHP83//7fzvdpLYwNjbGU6mUmLhQb+ykJtcoFPVZ32dHhUKhUCh6HM450uk0YrEYnnvuOBsdHe10k3qacIrQZp9b6kBF45UHWNVSVyFwlQrTY13qCZDtSjU6NraNG4YBwzBq3Jz02/WEkDNnzjF5xjPVsKMgcZRYukAkrSCLo7K7Vm5PeH3l1JkqqLl0ws5nei0qpXF4H9FnowI5Uc6bVv92C77vI5PJYGBgoNNNaRtf+tKX2Kc+9Sk8/fTTePrpp1EsFhcII+uZ8Dm327cH1Zx1HGdJdUjl84fcP4aGhlakvZ3g5MmT7MSJE7hy5QpyuRyKxSIcx6mpTRtVI7vbIaGbzvWyA41gjOHYsWP4zGc+090HQgXZaQeoewpFd/DZz36Wzc/PI5/PY2hoSGTTIOSU8FQSY61Dx56u68LNbVlW3Umb3YbjOGIcStddOVuOAiKzlud5+NznPtfp5rSFoaEhDAwMIJVK1TiFu/1eUqFYTXrjLluhUCgUCsUC7rjjIHccB4VCAXNzc7jnnrv5n//5X6g75RWmlQFovWD4YoJmUZ9r9ft16zuiViBczsCK3KNhcRSoDk7rQcFSciDJtezo+1FCmfRCTYpX+UGz36Me8jLD6Y5VYGHxRAmUYfeyLJKGHcfh/RLuj/X+L/9tl+C/mpBTY2RkpNNNaSuPP/44O3XqFK5du4Z8Pg+gNq2tSvEV7bruRqiGc7lcrjl/t7pOcmpd+e/g4ODKNLhDHD9+nM3PzwNANYNBSEwOp+vvduRrv5xOnxxc5XIZhmHgAx/4QKea2HZkgTS8D7v5OFf0Ph/84Adx7NgxPPnkk7h27RoKhUJkn6UJkd0E3XMYhtEzE9JyuZwY98/NzaFQKIhzbq9MslkudE396Ec/im5OqyyzYcMG9Pf3I51Oi+tNGHWtUSgao86QCoVC0aXsmJrs/iiJYsXYvXsnp1R0NMNX1R5ZWeoFv2SihJqwcLjoAUxIXF1MADXq82GH31KJco7K7zUSSEkcpYfsQGoUZGeMiVqUUQIpBXCoNio9YrEYYrGYeJ32g+zaUY6PxUHbSz4mwoJlI5G0kbgeFpFovzdbfqeF0sX+/tatW1ewNZ3h/PnzSCaTKJfLQiQFAlGkVCp1sGWdI+qa0O2BLNu2USgURLq/xZ4764nE/f397WrimoImA9E5MOwAInphEgHtU6pZKF9jyVn+5JNP4kMf+lB3HwQSYYG03gQ3hWKtceHCBfY//+f/xD/90z/hySefxI0bN2rqWwK1TtK1TtQES03Teqa+9fnz53Hy5Ek8+eSTeO6553Dx4kXMzMwAUAIZENxr2raN2dlZ/NZv/Vanm9M2RkdH0d/fj0wmA9M0a8ZdCoWiNZRAqlAoFF2KpmnYvXePGk0rIhkZGYHrujBNE8lkEtlsFteuXcPDD79F9ZkVgtLA1guANRukLGYQUy+Q1qqI10igCrv7lsLY2DZOYqMsdNHfVCrVsB4upeoigZTq2IXbH7X+Mq04SEk0lQXTcN07NcBcPFGO37Do2UgcldMzNxTE67we5dDuFuGJxJEdO3Z0uilt5+mnn2bnz5/H7du34XkeisUiCoUCDMPomRR3zah3nm7mkO4myuWycJDKNUhbIfxZWVDr6+trazvXAnNzc6IGtm3bIu2jLJISvVCjl1xmojZ4pSYpYwyJRAK5XA7vfOc7O9zK9kI12IG1Uw9boWiV5557jn3yk5/Eyy+/DHK8y7iu2zV9OnxOpSw1vTIh7dSpU+yxxx7DZz/7WXzlK1/Bs88+i5dfflnso/VOLBZDqVTC+973Ppw8ebJ7b7JCbN68GRs2bEAmkxHZGML1zBUKRWO6KweCQqFQKASlUgnDw8OdboZijTI7OwvOOQzDQLFYFIHFkydPdrppPYtcN1N+rZ6QF047Sn+XExRvVntUQ/TsuKg6kcupC5hIJJBIJBCLxcAYEw4iCvz6PnD58uW6Cw+Lo+EUu+G217xXeR6ua6nrOhjX4bMg0E6iHFB1d5AoR8uSBVLf92vqmyoaU08ANQwD8FxwcGhMgwYW/GUaTE0PHnr1YWgadMagU19kDE6dAHO9mqNroRbpYo4lqvM1OTm5gi3qHP/4j/8IAJifn8fAwACGh4exc+dO6LoOz/N6QgRaKt0i4jcjqgap7/tLSvEnX4uy2Sy2b9/OG10/uo3Tp0/j1KlT2LNnD4rFIizLEqnywnXjeqFvyH2B7g8sywIA5PN5fPCDH8QXv/jF7l9RibBAqlB0G88//zw7duwYP3jw4IIMMJR5pRuQndy6rgvhsFcEUgB45pln2DPPPIPXvva13DAMbNmyBZ7ndV0K5JWgVCrhb/7mb/Dbv/3b3dFhW2Tz5s3YtGkTstksYrGYmtyrUCwBdYZUKBSKLuXK5RfYxo0b1ShbEclzzx0Xd8WHDh3gqVQKuVwOiUSik83qaeSUoo1o5npbzoCGM8BHazVIPe5D41qN45VX3mN8eYHYWCwm3JgAapwwjDHcuhWke9qzZw+PmsHr+2FH7sKZsI3qqBIM5FTUxV+DcfiGAZ9zwGfwNB+m5QGcIW6WYVkWOLwaYU/+XcXiqG5HEpwB7tcKp7Jbt1EN0jA1+z0UfI6qrSv/Xev4vo+NGzd2uhkrwsmTJ9nJkyfx+OOP8wcffBAPPvgghoaGMDo6qmpk9Qg0yYXc/0txF4Un6TDGREaMXuL48eMMAPd9H3v37sX4+Dg8z0M8HodhGNUJOhW6/RiRnaMAxDpOT0/jb/7mb/COd7yjJ6O6ys2j6HY+8pGPsPvvv59v27ZNTIKk2p2GYSx5EsxqQucfur5QlpINGzZ0umlt58qVK5idnYXjOGt+v6wWf/7nf46f/umf7rlrzMDAAAYGBhCPxyMnLKiUuwpFc9RZUqFQKLqYb33zCXWXo2jKs88+zwqlMnTTQjy5PlIYdgLf96uzqqkmoq4BGgM0Bs6qAibXAA8+PPjwGYfPOBzfhee74PCrVtDKg6H2IYJrlYEOB+BxDh8AZww+AgHU4z44q7bP9/3K54LP0neAyuDJry7b0PQl1xXSdR2maQpHpix+GYaBRCKGO++8g9ebzZxMpMGYDtsOguue56FQKAq3CTgDgwbOKg/OxAPQoGmGCMiL39UqbkXNgKnp4L6PuGXA0HQYGoPnOohZJixdg6kx+K6NbF8aTOPwfB+6YYCx9etsWyyWqcN1ytA1gHMPpmlA1xlc1wHTIB5gHJrOoOkMuqHBMKt9hwbylApZ14M+yTUGx/fggcNn1ZScOmPgngedMTHIkQVWTwguPkydAb4PQ9NgaAzwPeHWsgwzcGJzDgbAdwMxXdMMLPaQIJeCbduwLGuB8yIqFTd9p9dn+z/66KPs2LFjuHbtmhDSXNcFAOE6BGpTrtL7wEInfrjGX9SjlXNao+8u5vutLJ+gPkrnLM55V9eblINxNDGmWX+Wz9n0XXliA2MMsVisJ9PsHj9+nL33ve9lf/Inf4KvfvWruHr1as3+l7eNnMp/KTSaVETvNyLqGAsjH7/yb9L3geDcTNkcbt68id/8zd/Ez/zMz/TcuGbPnj3ccRxwzoWjOjz5arGT4+j8QJPQFIrV4t//+3/P/vzP/xx/93d/h+eeew65XK4m60qzc8NaQZ64aRiGcLH3EqdPn2bnz58HUM3q4HkeHMepOT/TeQloXMKlUTYf+XPL2fetXH/Cn5evh5zzmvtESgmdy+XwP/7H/+hJcRQAdu/ejVQqhb6+vpoJSED1/ilq28r3ngrFeqe3R90KhUKhUCgAAM8/+xy7864jdQUpxfJZTIBL/mz4bzvwK4+lQO7R5bhZaXAsC1w0WKNBGgULw+zff5CXCnlobAicc5hmDIVCAbFYDOVyOQjAVPIIcywchFe3pw5ogMaCtK0eM6BpHuD74JoGSzdESk/LssAYg+v6iMcoSFJ1t5DI6vvdK1isNrIYpWnkEGUAWM3s/XAa5PBzub9EiQK9OBuaXBi9KASF+fznP88cx+FUO8n3fZimiXQ6DaAa7JJr08rCUVR6crlPLKW/NKt52yyQ1Oj3gWgHoBx8pOOimzM+yJMa5O3WjuO1V53VAPDVr36VDQwM8H379oExBsdxUCwWhWu2VCohHo+Lz5NYSk4oILrPRYnx4efyZ+t9Hqjff2WRRG4LCYL0mqZpYsKI7/v4xCc+gd/+7d/Gk08+2Xsnc6BmW4TF5eWWM+jF659i7fPe976X/czP/Azv6+vD+Pg4+vv74TjOAsF+rfdPOYvJyMhIp5uzInzmM59hpVKJX7lyBa961auQTqcxNDQk7sPpnguAOC8DqLnflstkhLOxtLt2eqNrUlh8lceq8iQiwzAwPz+PVCqFTCaDT37yk/jlX/5lnDhxYm13yGUQi8UQj8drxk6AEj4VisWgoqQKhUKhUKwTDMNAuVzudDN6lqiUrM0+20mxp15Qrh2DKbnunOwGot8zDEMEfqO+S84SwzBg2zZc162pRerzykxnsAUD5sqPib+6roNXhBXf98GZA83XYJomdN2E53FACwIEjucjmU7VpHaNxWKiLqLjqOOnVcj5K7t4qS80EkgNw6hxH8uD/GaBe/m1cBCnkzRrcxhyNPRiyrcovvKVr7CvfOUrmJqa4q973evwUz/1U9i5cyf6+vrAGFvg7GhUozQqWNeuPlAvINiIemnOwssLOyxt214TfXepmKYpUq23O7Xf2NhYW5e31vj0pz/NPv3pT+Ntb3sb/97v/V4cOnQInuchk8kgHo+jWCyK8ySdP2UanReB5ufEpewv+foui7hyCkvf91Eul5FIJFAoFPC3f/u3eP/734/HHnusZ4PWAIK0/ZWJHq7rwjTNJaedVijWCs8//zzuuOMOmKaJUqkkJjHS9bre+GItiKbhsQkQ1HDcu3cv70UR7Utf+hL70pe+hMOHD/Of+ImfwCtf+UocPnwYsVgM5G4nFy1NuJHHqc0m1bSTqD4SNWFXnnQr/+Wco1QqQdd1fPzjH8cf/uEf4utf/3rP7VOZAwcO8Hg8jmQyKSb1RlHvPmC55X0Uil5BCaQKhUKhUKwjbNvudBN6lqW4Lpfr1Fwq4aBclFC7nHaVy2XYth2ZsolER845Ll++vOAHTp8+yQ4dOMhzuRxc18X09DQ2bBjG/Pw8BgcHhLgGoHm1VRLgTBMGpfyrZF6yrKAmqa7rYJVZ0ZrPkUqlhCtHB5BMJkUws5Ewo6jF87wa55AcyJcDGnKtURJFqX6tLKo2EqdacWF3KhC91N9ljGFgYAAHDhzgzz///LqIXJw9e5adPXsWX/7yl/nrXvc6vOY1r8GhQ4cwOTkpAnmWZcG27ZpZ8mGnA1GvPyw2dXi7J7LIqf2A2jTQBNWg7FZIILUsq2Ed4aUwPj7etmWtZT760Y+yj370o9i7dy//0R/9UTz88MMYGRnByMhITQCUJqLIQuRyMlMsxoEanrjCOUc8HkepVIJt20ilUsJ19Oyzz+K5557DF77wBTz22GO4cOHCujivkUC63Jq8YVRQW9FJvva1r7E3vvGNfHp6GqOjo3BdV/T1lZp82QqtZHjwPK/GQQkAfX192LRpE06cOLEazewIzzzzDPvP//k/4+jRo/wNb3gDXv3qV2PXrl2ijixdP2QnMG0vOm+tdCricAai8CQ3aoeclaBUKmFmZgbz8/N46qmn8JGPfARf+tKX1s3JcWRkRIydwg5SQnbZKhSKaJRAqlAoFArFOiIWi2Hn7l38zKnT62bg0G2stou0XjB0OQOpM2fOsTvuOMhd160Z3Mopdw3DwOTkJD937tyCFR4eHsbc3JxwylANx3w+D8uyEK8M0DUwMF9yZFUejPPARcpZrUhaCYRwn4PzSqooTYOpm4GYp3NkMhl44OC+B40H/6eBuGmamJrawTXNwOnT6hhqhOwepRnNIqBbCW7IrusoB6kskBJ1g8IRM6PXkot0MVDgLhaLYXJyEs8//3ynm7SqnD9/np0/fx4f+tCHAABHjhzhu3fvxv79+3Ho0CEcOnRIiOnkMpaF+EYiaSuO0mYTSJrVBpUF2LBQxRjD7du3xQQCOk7k3/F9H9PT07h9+3bD31nLRAXr2uUkXS8CKXHixAn2zne+E+985zsBAA888ADfunUr9u3bh3379mH79u0YGBhAPB6HpmmIx+M151Y5I0IrmSui+qz8HokKnufBdV0h/FEQPZfL4fr16zh37hy+/e1v49lnn8WpU6dw6dKldXnNJNFIPt7rnYPWisNupdm5cyffv38/du3ahY0bNyIWi4ExtiBFa9S1vF5WCfn8Sf8nMUWeREOOOerD1G/pM/KxQvcl8nmMJiJElQOQJ+wwFqTIJmfek08+iT/6oz/qqZ37S7/0S2zLli18dnYWGzduRDabrUlVu1b7cvj4I3F3x44d+Od//ucOtWr1ePTRR9mjjz4q/r9r1y5+55134tChQzh48KCoZdnX14dkMikmO5mmiVwuV7OsZhkLohyejZCF66hjNJfL4cUXX8SJEyfw7W9/G+fPn8cLL7yAZ555Zm12tlVgdHS05j6rlYmlMt02RlIoVgolkCoUCoVCsY4wDAPZbBYHDhzgnuf1dD2O1WYxomK9z3UymBBO/bjcmaaUZldOG0kBJHIWZjKZyO/+8/97hAHAt54CfuxHf4Rfv96HwcFBAIGjk5NA0ah5ou0aNF2HwTlYxVnDPQYgGHgHYpyFcrkMXddEoNm2bfi+j3Q6Ldwf5H4tFlWq3VaggCelghTBRalWUFgYlR/hFLu0zPBvtEq3zJ6WhaQDBw7g05/+dAdb03meeuop9tRTT3W6GYpFQIE6WahrF1u2bGnbsrqRf/3Xf13VG4Xt27dz+Ty7XoXOpUKOq3rXnuXc961VAaoRd999N7/jjjvwwAMP4O6778b4+DhisZhIeypn6oia5CSL/FEiqfw8XPOVcy7Sl5PgIj/CAqlcAoBep5r14Uk5dI4rl8tIJpNgjKFUKqFYLCKRSODhhx/Gj/3Yj/Gf+ImfwKlTp7pvx9Xh137t1/DmN78ZExMTuOeeezA4OFizTVa7j7bye3I2E13Xhah++PDhlWzamuX06dPs9OnT+Ou//utON0WxBEZHR8V5c7HHW7dOJFUoVgIlkCoUCoVCsU6Ynp6GaZpiNigA7N69m/fSQL2TUPCllfSNco0X+i6xWsGERmmw5Bn4S0WuDyM7RynAND8/L/phI6heKblUcrkcUonK95gGpgGAX3lUEOtFTtJAJIVlAboG7gSz/KHpsFjgcHQ8F5YZw+DAEFKpFBzHge/7SKVSon4pOcfOnz+vjpkmUOCJZp3TvjcMo0YgJWHUsizxaJRit9mxEj6mOj3ov3z5ChsYGFhUI2gdPM/D/v37V6RdCsVKEhYd2snGjRsxNjbGlVC3OkSlwle0DglqAIT7sB3lFbpRHH3d617Hjx49ivHxcezatQvDw8NIJpM1dcnbTfheoN01kcMYhiHGA/F4XKRKN00Tr3jFK/CZz3wGu3fvXtE2rCbnzp1jv//7v4+3v/3tfM+ePTUlKdaqIzrscCTn8pEjRzrVJIViyWzYsAGWZQmRtFF2qKjzX7dMHlUoVpqVvTtQKBQKhUKxZjh35iw7+e0TzDAMuG5QiLGba5ytNRYzwFgr4g0RlVKynTWy5FRlmqYhkUiIPtiM2dlZOI6DXC4H0zRBaXtbDi6yIM0uE6nSDJG+ldLmkhhnmibS6bQQRH3fRyKRQDKZRCKRgGmaiMfjmJycXBs7bg1D+1xOlRtOSye7R+ul162XTm894Pv+unfLKboT2aEVzk6wXIaGhkTNNIVirbOYNNOLPT7WovjUiMnJSbz+9a/HPffcg7GxMaTT6QWToKKcnbITNJwSt9n9a9gNWm/54WWGz2FRLtXwb9Ln5fUgaF9NTU3h8ccf77kbmcuXLyOXy0VuL2Bt9dXwhFZq28TERCebpVAsiWw2K8axzVLo12M9ja0UinoogVShUCgUih5iatdO/uCrX8X3HzxQ9073m994nB07dozlcjkYhoHDhw/zqakpdWe8TKjOYjh4Ek41SMIRzawO1yyKcqCGgy9iwBPhpgsHhsIBat/3hQAVXjbVFKMUZnJ63MVC603p5SiFGjlIDcOom2JXplQqIZfLgXOOXC4n6qzprFpXjYJmtA05rzxAgz4GMA3QdeiWBSsWh2nFYBoWYlYcnGmIJ1OArsFKxLFp0yaUSiUkEgkAwJbNm+FVtott23AcZ8nbZb3geR50XUcymRT9zTRNsY/o/+QYlZ2jsVhM9D0KhspEBR0h9eNwIDUcoIs6PuW6R3L76biQ07AthrGxbdxxHCHuR9VUlQkf57t27Vr0byoUncZxHBSLRZTLZfi+D8dxYNu2SLEuH5thIUMmajKMpmk4ePDgqq6PQrEc4vE4UqmUmGxV795qMe7GdqeuXg1ogpR8LwBApNelz0Q9wmlta+qaSzSbQFdv+XLtvvCyCPm+PepzcrvCNdTleuyveMUr8Ed/9Ec9Ne761Kc+xa5evYq5uTkUi0UYhiHO+QBQKBQ6XkaEHnI6Uvn10dFR7N27t6f2i6L3GRgYEONSOsdEnadonAOg5hjQNE2NaxUKKIFUoVAoFIqe4uzpM+yll17C8eeebzoKPXXqFJudnYXv+xgaGsKOHTvUoHAFaMX11srMzcXWWmxE2NkTDu60AxK7SPiSHYKu62J8fLyl9S6XyyiVSiiXy3AcB6VSCdyLbjvQ4rYM1ZWSn+u6jq1bt8KyLPGbY2NjcBwHsVgMnHOVcrAFSByPxWKIxWLCGUqD96jao5RiNxwABWqDW/RaoxS7UTRzqESlAw27RpYiki4WWnfDMJBMJrFnzx51blZ0FfUEBPl5PSEjXDMw/GCMYefOnau2LgrFcqBrXDKZRCaTWXBtW0/Mz8/jypUruHHjBvL5vMgkspa2RdT5qR0pkWV838cP/uAP4hd/8Rd76tr+gQ98AF/84hdx/PhxPP/885ifn4fjOPA8D8lkstPNa4kDBw50ugkKxaJYr9cThaLdKIFUoVAoupRtY9t7alClaB9nT59p+S757Nmz7Nlnn2U3b97E+fPn2fbtql+1i6iUXMsVSxcboIlaFrlK67l32jXQItErHo/DsixomiZEMs45CoUC5ufnmy6nUCgIF5Ls8uPwQHVHw8ErzoJHcKsr3e4yreIkNcAMUzwMw4JhWDDNGHTdxPj4DqTTWRHY6evrQzabRalYhFGZdatojLy/o0Ro2UEqC6nkHg3XH23l+GnWb8O1cMMPue+HXSOyiLrSyOuYSCSwZ8+eFf9NhaKdhB1e9GjkFm0VzjkOHz7cxtYqFCsHXVtoghBdE4H1l9bw5s2bOHnyJC5fvoyZmRnYtl3z/nrYHo7jQNd1DAwM4Jd/+Zfx1re+tWdW+tSpU+zHf/zH2Uc+8hE88sgjuH37tnDTdotD7ZWvfGWnm6BQtMzu3bt5t2USUCjWKupIUigUii7lhUvKwaRoH+fOnWMAlDNuGYRFusXWTqwnqC6F8HfrueKi2h+1LkuBxDFKpyunJkskErBtG+Pj402XUygURKpGzjlczw611wfntaIVo3/0mfADC9OsyXVJR0ZGsGXLFhHYLBaLmJycRKlU6rqUdp2CAsEkishpdck5KrtH5eAx7Yt6fbBR32zm/Gj1NRk5Ze/FixdX/BwppxU2TROveMUrVvonFYq20qieMFHv+tbMwcU5x+7du1djNRSKZSNPFPB9P7K+9nphZmYGly9fxssvvyzchasx6WgtYZomAKBYLCKZTOIDH/gA7r777p7qBH/6p3/KCoUCpqenYRiGKN3RDdx1112dboJC0TJ9fX3rdsKNQtFuVIRHoVAoFAqFog2QmEbCThThWohR79cTL5dCWHSVl9corWE7BFLaDmGBlHOOfD6PdDqNgYGBpsspFovI5/OYnZ1FPp9HPp+H53k1aXVlZ1+N86/yiIQxME0TfzVdh6brYJqGeCKBAwcOiHZ7nofNmzdj48aNKJfLGBsbU6PQJsjip/yQRVD6f1hIqSeo1Ou3YeoJK3LNWqotSn/D9UqjJhWsVvCBRHvq08rRoOg2LMsS2QPCtfiAhamrFzspaGhoSKWeVnQFnPOa+pPrOR1isVjE/Py8yAxCtePXUmC/WYrvduD7PmKxGABgcHAQn/zkJ9uy3LXE8ePHYZomZmZmRN+nlMprmcnJSUxMTKydDqlQNKC/vx/A6o5RFIpeRQmkCoVCoVAo6rJz506+f/9+rgSh5sipQxs536KoJ2RGfb+VwFqrqUjDy2smoi4G+i65SClAyBhDJpNBf38/XnrppabLsW0bxWIRc3NzKBQKKJVKKBaLNTPSA4G09UBbOM0jiWdCQPN97N69G0NDQ/A8D7FYDL7vY8+ePfA8D7pKs9uQ7ds2cVn4lNMKcs5ratPS3/Bno1LsAtHO7MUGL5s5SDsdZAiLSVNTUx1qiUKxNCzLQiKRQCwWq7kmNrqmNBMm5DrAuq7j0KFDq7hGCsXSqHd/FzVhrtPXnpWm19evFWzbFvdCxMaNG/Gnf/qnPbVx/vIv/5J9/vOfx9mzZ/Hiiy/i6tWrXSGQmqapUrgruoa+vj51XlUo2oQSSBUKhUKhUAAAouqPkoPJsqxVb0+3IddZXIwDMypg1q6ZoItJ6wvUF06XAjnzwrUnSYC6desWrl+/3nQ5Fy+9wHK5HG7evIlbt25hZmYG8/PzIuWu7CKVU5PWUluLlHMm/h/lMHRcF/0DA7jzzjuRyWTg+z4cx8HgYD/GxsaQTCYjjxdFwODgILLZLOLxuNjn8gQCEkXluqP0IMdxVBrCsBDa7DUi/Dl5X0eJ5GsBqrnr+z4GBgbwlre8RfU3RddgmiYSiQTi8biYGENETU6g561Ax+nRo0fb33CFoo3s2rWL9/f3i2ths8k8vR7opvT64Xrfa+W6CzRP8b1cLMvC7OwsDMMAAORyOViWhfvvvx+/9Eu/1FMd4Jd+6ZfYZz/7WXz84x/HlStXuqIOKWNMXVsUXUM6nRbP19J5VKHoRtQRpFAoFAqFAgAwPj6OHTt21AzOOeewLAuZTAZ79+7tqYF7uwmLLlHvy3+XsvxWaSawNnxfa0/qN8/z4LquEKSA6jrYtg3OOfbv39/Ssubmcnj55et48cWruPriy8jn82IZjDHotG14IJAy1KtpVSuURt0Ky/vvnnvuwcaNG1EsFhGLxeC6Lvbv34tUKoFUKtFS29cTk5MT/PDhQ3zz5s0YHh5GKpUSAikJoySYyGl2ZRcppeQMp6quJ4TScwDwOIeP1pxolE6X0jPLjzDtCowuBvn4jMViuOOOO1b19xWK5UD1hkkMARZOwAmLpuH36j0sy1LHhGLNs2fPHr5jxw5s27YNg4ODSCaTQhSLugdbDykS5brv8sQkotfXnyDXl23bSKfTKJfL2L17N97+9rfjF37hF3pqI7znPe9hFy9exNWrV3H79u1ON6cpmUxG1X1XdA00gX2tTTRRKLoRdQQpFAqFQqEAAMzOzmBkZAiTk9XaK65rIxYzxV9FfeSgj65pAOfgng/GAQ0M8Ll47vo+XN+H5/ngvBoU9hGIPCT0MMYB+ODMhw8PnPngdXQaWeyRg89hUQi+Dw0MhqZBZ0zcDDLGAK2aYlQz9DpuzNYol8vQdR2FQgGDg4PgnKNUKlU2lo6Xrl3Hhz/84ZZUpzNnz7MXLr+Ic2cv4caNabz44ksA1wCfV+pYOZW6pACHB6AqknJwUYuUA/D82lm2jOlgTAeJp4zpwumRTKVw9IEH0DeQRb6YQzyZgO06uPPIHdi8ZSP27t3NJ6dU+mkAmNixjU/sGMPdrziCvXv3YtOmTRgY6AdjQCKRgGmaVUHbNGHF4zBjMWiGAabr0AxDPIemiWNBPiY8zsEZA+M+GPcBcDAGMF0DNAYfHK7vic9BM+BDA2caoOnQNEPsX2YEv8N0HZyx4FE5TlzfAzQG1/NQOQLheO4SUysHfYpzBl034XnNu4vjODWikmEYePWrX72E31YoOkMikRATHQzDaOla0mwiAr3vui5isRh2796N8fFxdf5VrEle+9rX4lWvehV27dqFLVu2wDTNBdkUwpMEWr3GaJoGx3FEHctuIiySyqLoegrw02QPIJgExTnH8PAw3v3ud+MHfuAHeuq89sgjjyCXy9WMAxzHQblcFp+Rn68EUZNy6pUZGR8fx6FDh3pqHyh6k1KpJO6zKGsTUH+ySdQ9Vr3JoQrFemP93IEoFAqFQqFoyLFjz7F8Pl+TroWCkeT0UtQnKm1n+P16AeCoNLv1Wf4ghjEGDSyyzYyxuiLsYnAcRwy4SLSl3x4YGFh0YO/CpYvs5Zev49rLN1AoFISDVAML3IAVkRS+X5FEfdC2ok0aiM6t/R61d9euXXjLW96CoaEhXL16FUNDQ+Soxr79exCPxxe1Hr3K4OAgNm/ejLGxMZCDNJ1OwzRN4SaLx+NCLJXrjlKwmPqinH4PiB7Q16shCiBwQWsLjz96r5m7NLzMdqbXawXTNEUqOjoH33XXXavy2wpFO5BTq7f7+NF1HZ7nYWRkBEeOHGnLMhWKdnLo0CG+c+dO3HXXXdizZw9GR0dhmmZwj9IGutVpSYH4em7Zbl2vdpJMJvFrv/Zr2L9/f89sjOPHj7PPfvazKBaL8DxPiKWxWEwIo7FYDLZtd7ilgSNvaGgIDz/8cKebolA0hSbeAFjiJE6FQkEogVShUCgUCoXg+ee/zY4de05EMROJxAJHoiIaWdwh6m2zqO3ZrDZVu2nld5azzx3HEcFAeVYrALz88lVwvvhAIdUhnZubQ6lUAq+4BznncBxHpPStt24aWhNIqa2e58E0Tdx5+E488MADGBoaws2bNwEAhmFgdHQUe/fuxYGDe3omkLUUJnZs4319fchkMkgmkxgcHEA2m1kghlqWJdLsUr1RSq9LKXfDogohO6KjRH36TCOHQNSkhfDn6i2DHC+LdayFHTKtHlPhtIODg4P4vu/7vnXdzxTdA016kGsQtwsSWHRdx+tf//q2LVehaBc7d+7Eli1bMDU1hY0bNyKTyYjJQusZqhXveZ4SSBvQ39+PD3/4wz1V6/4Tn/gE+9CHPoSvfvWreOqpp3D16lUA1Xsi3/dFCupOwjlHIpHA93//93e6KQpFQ8bGxriu6+K8qlAolkfnr0AKhUKhUCjWJLt2TfFsNgvXddXNdwvUE1lkgYTEO4aFgs1qCqRiBr/Pa2bzt/OXSazUNE3Usg3S4DIkk0nouo477zrMn37ymZZV2EsvXGQ7dozz+XwO5XJZBFR834frunAcJ3A7S99h4OBYet1X13Wh6cB9990HXdfxuc99DrlcDrFYDMlkEgMDA9i7dy+2b9/OX7p6DU8/fWxdzCTYtn0Tn5qawsTEBLZu3YpMJhBEk8kkNKnvUw1C2RUai8WEK53EUcuyhEhKn6dlAIgUGWuOM9Q/3sK1SmWXalQ9tIXHcDUt4FID3JRemI6HRpBrn4jH4/B9H29961vxd3/3d0v6fYViNdE0TRzL7Z5c5XmeSE35qle9qq3LVijawaZNmxCLxZBKpSKzIiyHbhcRa+45u3xdVoJSqYTR0VFwzvGZz3wGhw8f7nST2sb//t//m507d44/9NBDMAwDW7duFdmJCoVCTQajTuG6LgBg7969eP3rX8+//OUvr4t7ekX3IbI+Vc6jVB5mqctSKNY7ykGqUCgUCoUiEhKxKE0qpXxU1KeeM1T+2+p7naCdAyQSSElQisfjNeKT53nIZrOLXm6hUECxUEKxbIvfAIKgueOW4bhl+H4g5jN6SKvV6qYuFosiyO/YHmJWAve+8n78wPf/IDZv3oxkMilShA0PD2PHjh2488gdePi7v5Pv3Dm5NnboCjE5NcZ37dqFffv2Yf/+/di1axc2b96MVCIZ1Nqt1FIj12gsFkM8HkcyHkcqkRCuURJHW3GQygHmZqlvW0mhGz7mWql9KAu3rbKUNL0kjpIrmnjNa16zqN9WKDoFnTvDdRbbca2j44Nzjl27duHo0aM9fb5VdB99fX2wLAupVArJZFLUmGw33RbUDqfeDtNt67MSxONxuK6LjRs3Yvfu3fiXf/mXnjq/fepTn2IXLlwQZThobLlW6unSPZ5pmvjJn/zJDrdGoagPTSxtdE5tBXXeVSgClINUoVAoFArFAvbs2cV1XYdt27BtG/RcsTTCjjbQ38rMz9Uem4SFJhJ+OKs68JabVvny5Stsx44dIrATj8cxPz8PAMhkMrh58yYSicSil+s4DsrlMorFIorFonApcc7hui7K5TJMywYswNBNLHU+IDmUgCBQ4rouTNPE3Xffje1jW/Ev//IvePLJJ+H7PlKplHBaDw8P41WvGsWRI0f47du3MT09jevXr+PSpRe6egR68OB+vnXrVmzYsAH79u8R6XPJwatpmkib6/u16fMoKGqRO7QyoCdRNCyORtUgXWxfDLtTZIGU6qDJNXKD5ywyiE2/vRSBNCwQtboevu8LZ8X8/DwymQwGBgbwQz/0Q/zjH/94V/clRe9Dx3fUZIflomkaHMcR55/v+q7vwqOPPtq25SsUy2H//v2cRFES85fj7KlHN5a+MAwDiURCbBu6nrbjnrNX8DwPjuPAcRxYloX77rsPf/iHf8h/9md/tmc2zje/+U0cOXIER48eRalUElkB6F6yk9C1q1wu49/8m3+DHTt28PPnz/fMtlf0DsPDw8hkMrAsa1HHDZ1vFQpFLUogVSgUCoViCUxMjnPTNEUKTqqp88KlKx2949w+vo1TIObi+UtLbksymQRQFQ5KpRLOn7+o7qYbEO1Wi36fBNJwUttOB4ja/fu2bYt1pgGc7/u4desGJibGMD09jf/4c/8f//znvoCxsTH8v//31aY/blqBWJ/P5zE/Pw/DMBBLBkIrOUpNqwwA0OI6NKaJBLvNw5O+eEbHEaU71TRNPN8wsgHf/d3fjVe+8pV4+umncezYMdy+fRvxeFykEk6m4hgc6sfmLRuRz03g0KFDnNyA8/PzIi0wpQam55xznD27+sGY8YmtnAKXlD44m80ik8kgk8lgeHgYo6OjGBgYEIIm7VtD0yvCowfD0AEErmENNDnAh6Yx6DqDYejQTUuIpvSISm1LLHiNe8GkAuYHDwTiJuBXatsGrzGNi/cZ4+KxYMJCC8iTCSYnJ/m5c+da2kemaS6oJ9qMcM1e+p7v+3jb296Gj3/84y21WaHoFOEapO2+rtHkBgD4ju/4DkxMTPALFy6oexRFx+nv7w/uQ0yzRvBp1zFQr+Z2N2CaJrLZLFKpFGKx2LqvyRqFpmlIJBLgnMNxHOi6jocffhinTp3iv//7v99dO7wOjz32GNu0aROne8tsNott27ZhYGCg002rqYmazWbxQz/0Q/j1X//1DrdKoagyNjbGd+7cifHxcWzbtg19fX1iTKZQKJaOEkgVCoVCoVgE+w8e4NlsFsViXgT+aCae53kwTZOfP7t6Qbqt27dwCjbQzTHVeRwZGeHkqCsUCi0Lpq94xV2cagSRMKTE0daoJ7hEOUjDrGagi/YveFV4oZqk7RxgUQotSrVL6zgwMIByuYxsNotLly7hyJEjyOVyeNOb3sC/8IUv1d0Q49vHOKUfK5fLKJVKsG0bZjwmxFfOuXA9e54HZlB1yorgxDjQQk1SCmySi4/EMc/zoOsM5XIZmUwGDzzwAA4cOIArV67g8uXLePnllzE7Oyu2azqdxsiwLtpH24Tq+pJA6jgOPM+D7/s4cuQIl92O9HqUCE/LkvdjVErLmhqeGq8RMAzDEKJoLBbD4OAgstksBgcHxezksHgpp982dKPy+0ych3Rdh1kJfvq+K75DqTcpDa8soESlypWDwdU3GguN4RrAYYs2YwzgC9PfAtV6Po2Wb5pmS4LM+Pg4l8XfVo8t+nyhUEA8Hkcmk4Ft2zAMA/fcc49yNCjWPHR8hx2k7brOUTpG3/exa9cuPPjgg7hw4UJblq1QLAfOeZDJwjRrRMCo6/Ny6TaBVNd1JBIJxONxmKZZM45SBDDGhEOe7j9HR0fxnve8B7Zt8w9+8IPdtdPr8Hd/93fsxRdf5A8++CCOHj2KgYEBZLPZmvrrnUC+x+Wc4wd+4AeUQKpYU2zbtg379+/H9u3bsX37dgwMDCCRSCzqXBq+FnXbtUShWAmUQKpQKBQKRQts3b6Nj4yMQNd1zM3NwTR1OI5TU0/R8zykUincfc9d/FvffHLF7zQnJyf45s2bUS6XwcBQyOVFiiLf98EYg6HrMBJJJOMJ9Gf7OLldT5w4taB9O3dO8kQiAc8L6jdOT0/j4sXL6o65RWSBisQ1xqqpQmnQQn3G93343K9JMUZimBxQlt0HtF/F+02cdvT/8HCJMQbTNOF4VXek53kwLLMm1dlyB0yzs7OYn59HMplELpcTqXU1gwlHZjKZhK5p6B/Iolgs4rWvexXvyw7A8zx85jOfZQDwutc+xC9evIixsTEAgJWIY/r2bczMziORykC3SojFYkLEdJ0yHE2HYQTuPV3TAfhg0MCEOBotJvq+LwL7Uel5dR3wPQemboB7PnzfQyqRxPj2MWwYHkG5XMb8/Dzy+Txu376Nubk5FItFIYBGpXkNC5wUmBFCdgVZaIuqq0nQMUz7WoYxJvY5OUEtyxK1Qg3DQF9f3wLxUO6H4v+V1MaMoRLIC/qPKYQRXvmeUdMWOa0zOUlpvUh0lVPtyq8zxqDVEVtoO8hisu/74NJ2pm1D+5k+EwQlXSH8ep4H0wiWqWm6ELdjsRhc1wZgYnJygp87Fy2STk5O8CD1swfGdDAWOGllB2sU8v6Nx+OirYZhBJMBTBNvf/vb8V/+y3+puwyFotPQZAtN08QEi3alT6TzdrlcFvWNf/qnfxof/ehH29ByhWJ50DWWJjbJNQ3bgXzP0G1BbUoVH4/Ha2pOypNN1yvyuof7iqZpSKVS+IM/+APcuHGDf/KTn+yJDfX444+z4eFhvmXLFmzYsAHbt2+veV8eNwFoyzWkUe1buk5RthjXdTE5OYnf+I3f4L/4i7/YE9tc0f3s2LED+/btw+TkJIaHh9HX11eZvKu35R5LoVivKIFUoVAoFIoW2LBhA8rlsqif47o2isUistksxsfH4XkeLly4gHw+j1gshonJcX7h3Mq6LpPJJG7fvg3f99Hf3490Oi0EmLAri14DggHggQP7uJzSU3YsBeuy9PS865VGglU9wqnSZCFKdv4tdYZ9p2flnz59lm3evJnrui7qU5IAJTuMSPBKJBKBaOVzpFIpfOd3von7vo/BgQHYto1UKgXT1FEul5HL5TA7O4t0Og29IrjS9rRtOxDe7KCuJTPCg0Y/orWo2f6N0RYIfIZhiJR65LwcHBwUwii5RX3fh23bC+pgRqdoru1L1EfK5fKi+1s4+EbrGu578naUXw8L5sG+q/ZL2Q2qi+9HtytcY5SCXrKoSb9PfUUOnnJEbCcsdMqK9Q452Jptr3rCq9x+6sfj49t5eCLJ2FiQ6jx8PC9lX8nrTdv44YcfVgKpYk0j1xhud8CO6kHHYjHMzc0hm83i7rvvxpve9Cb+hS98Qd27KDqOfN0MT45rx7K7VUjUdV2Ioytxbug1ooTjP/mTP8Gzzz7Lz5w5052dIMTnPvc59upXv5qPjo4in88jlUqJsWy47vtq9BeavOl5nph4+aM/+qP4kz/5E5W5Q9Fx7rrrLr5161Zs27YNW7ZsQV9fX3XsvM4nmSgUy0UJpAqFQqFQNGFsYpy7rotUKiVqH544/m1xB/rkE08t+M6hwwdXVJnavn0rj8fjSKfTsG0b165dw+joKEZGRvAP//A5dXfcAWTXGlAVRKIGK+G0p2EhKvz+ghS9i2AxgsxKDKxo4sCGDRswNDQExhhc14GmB+vrcx/cr4pOsVgMnuehUMxheGQQnuehmC9hcHAQnAc1QW3bxtzcHG7cuBHUNtWrgRPDMKouRGZAZxq0OAMzDDBt4a1vdJqhxkEYWRiVBWwK6uTzuYpTkUPXNWgag6YxAByuy5HJZGqEQLkttI7ya+Hn2Wx2geAmf07+ftT68opzuV7qV9ktGhZR6fOmaYj15zxIxx2rpMwjd67sguWcAz4HZwudnrI4ShM56G+UYEsuzLBju972IOT21/tcs2OAhH36bngfAhDCUHh5iz1+w8FREp2Gh4fxsY99jP/Ij/yIOtcr1iSGYYg0muEJEcvFNE3hlkgmk1TeAP/9v/93fOELX2jb7ygUS8EwDDH5iybKRF0nlko3B8Aty0Imk0EymRTOckIF96OR71sAYHBwEO9///vxxje+scMtax9f/vKX8cADD2DTpk1iAqDshpMzI6103VrOOSzLguM44rUtW7bgXe96F/7tv/23K/rbCkUzBgcHMTQ0hJGREYyMjCCZTIrxnDqHKhTLQ03ZUigUCoWiCUNDQwCCeoq2bWN4eLjpd0zTxNSuyRUTSZPJJDjnuHXrFsrlMlKpFB599DGmxNHOUs8lFvX/ZmJOPZF0MW0JE5WCd6W5dOkF9sQTT7Jbt26hVCrBMDXwioBGKejIXUkCYyKRAADk83mUy2VRr5SC7b7voVAoYHp6Gjdv3sT09DTm5+dFTVJKkUV1SsvlciAaRohhi98GGlARDmWXFDlILctCf38/stmsSDNJtaQsy0I8Hm8qSIedqcJpGxIawu5Pek4pcy3LEr8tP+TlhR2w8nfouby88HIty0IikUAikRDrSm0GalNHCxdtxb1O51Tax7Ztw3EckLtdrr1KyMJrVHpi+oz8eXl7ya/Vm3ggb9eo/iJvc13XMT6+XXx5YmKMR+2rxbrLZeg79HuapuEHfuAH8MpXvlIVblOsScjhaVlWpAN9udAkEEqDODs7i4ceeggPP/ywOiYUHWPXrl1869atGBwcrBEAw5lBlku3ukgty0I6nUYikRC1yBWtIWcQOXDgAP7yL/+yZ851X/7yl9mHP/xhfO1rX8OxY8dw/fr1mmOFSkKstDgKQExm0HUdxWIRvu9jdnYWb3vb23D//ff3zDZXdCdUDoXGXZZlifeUI1+hWB7KQapQKBQKRQO2bt/GOQ9qwdGM1lYdeSTytJvxHRO8v78fQc08B5xz9Pf3r8hvKVqnnugSJdBU60dplVnR2pLFk1bbJbehnvizkhQKBZTLZSRTQV1Fz3PAWEWo02uDh7ncPDKZNMplOxj8+dX6jZZlweMeyo6N2fk5WPEYYgkLsXhcBCTJYeQ4Tk0tWE3ToBtRt7+Ln3mraQZ0vbptZeeq6zqixqXneQDzwTQNmm4EYp5XW7M27KgkZEEv7JgMEw4mRb1eFdri4jW5lhlto/D/wy5SoJruVdd1xCqiKOBXtrsnRGrHccB9typocgbGXCDkXjUMQ6xnuA1hBynntQ7VsIO03gSEesJpK0Q5u+U2AUFq3Xa45WRXsvx/oFqLrFQq4X/9r/+Fhx56aFm/pVCsBDThYqVqYtHxQHVITdNEsVjEO97xDnzmM59p++8pFI04dOgQ37NnD3bt2oWxsTHs2rVrwX15K+ndex2aUGVZlhBIletpccRiMYyOjuLNb34z/vRP/5T/u3/373pi4334wx9mJ06c4A899BAefPBBZDIZcQ9NZSFWo6/I5Who7E9ZW973vvfh1a9+9Yr+vkLRDF3XhVAqxpkKhWLZKIFUoVAoFIoGbNu2DfPz8+jr68Ps7CzK5TK+9c0nmo7OfN+vSc/TThKJBHK5HIBgoOw4DmZnZ1fktxSLY7kuMfm7YZfhUoJrYUGnFWF0pYIP+Xwet2/fhmnpyBVyYIzDNKkuqVGz7uVyGZlMBo7jiFnjpVIJrusGwQstcHTn83mYpol4PHBmxmMxaJomaoEy6JX1qW7DGADN0MFQP/Vx003ANYD5C4RICt7oetWVGYvF4HO3xiXr2F6N+1H+bj03sbwvjUiRtzW3sSxAyr9Jqcvk9LphgZTek52npmkiVnH2Ok7gAqU0yKVSCY7jwPccsa6e78P3NbDQb5FA6vu+eC67XOu5MZsdb+HPRTlSw9Rz51QF69rPaZomxFG5/nMUwfau21zRpnBaPRnf92GaJl796lfjjW98I//iF7/YEwFSRe8g1ySWHe7twPf9YKJMJc2u/Jt33nknfuRHfoR/7GMfU8eEYtXYuXMn7r//fhw9ehTDw8Po7++vSQ8qX+vaxUpNqltJgvu1uKhHv9oZTXoF13XR39+Pt73tbXjuuef4//k//6cnNt7Xv/51tnHjRn7gwAFxDXFdV9wTUf3plYZqoQKA53kwDAOFQgH33XcffuVXfoW/5z3v6YntrehOKCtPOF6gUCiWhxJIFQqFQqFogOM4SKfTyOfzePrJp1q+A9U0TYiY7WRsYpybpgkNXDgmKOWlorM0SvMZppU6h+G/smjSbpqlfG0H585dYKZpctez4XEPlmUglQJMU4fva2Kwp2kaMpmMSKHoui40MOEMZYyBs2ot0lwuh+npSnpYM6h/SbVMA3cq4DgMjEEIg1Y8eE/XFqbrankbcICBQdd06IEUCx0Mvm6gVC6CcQ7GPTDO4HkaAB+ezwHPRzIer0kRC0Q7SqPcyPS58D6TvxcOPIYJ3Lu17kxZjKTZyGGBUtQsZdXUwowxMO7BccooFAooFAooFotB2txikN7Y565ot+f7AAxoUupg2fVLaYjp93VdF+tb6x7wwbkHwAeYDw5y6nLxf86rD9/34HMXvu9K26u1XR1FOM1uWNANUkH7yz5mo75L21LTNLz73e/GF7/4xaWviEKxAoT7fTuvLbQs27aRSCSCmtWFAjKZDObm5vBzP/dz+NjHPta231MomjEwMIDNmzdjx44d6Ovrg67rsG1bvN9u59tyJuN1Epo0RgJpu+sTrwdKpZK4R9J1Hb/wC7+A48eP8y996Us9sSEvXbqEq1evYmZmBvHKvTL1kdUQRznnYkxN9+elUgnJZBJzc3P4+Z//eZw4cYJ/4hOf6Intregepqam+JYtW8Q1hvpnt6ZcVyjWGkogVSgUCoUigu3jY3x0dBSO46BQKODbzx9f1J1nLBbDlcsvtv1uta+vL3CP+Z4QYIvFIp566hl1Z9xh6qX2rOdQC6cQrRdQjnSQMg6fPsoANIiRcc7BWfD9ipwUvM7qB63ZCsXcTp48zfbs2cVjSROMJQFoYCwQl2RRjnOOXC6HRCIBTdNRKhSFw1HXdXBWrWtZLpcxP59HLHYb6VQKphlDMpmGpmmwLCv4HIJ0u7peDkQtIxD4NE0TTtJFDS45DzYSYxVzqgbGAJMxuNxHStdhew5c20TZcQDfh+v7iMWCVLPlcnlBSlsAkQJpVL8Kp5yVP0vPiag+FQQmAcZ0MMbBOUPQMzQwxhGLJcA1Dh06oAMGM8Rfv7IcCnB6nge7ZKNYLGJ+fh6FQkG47YvFYlAT1vXgwwN8Bo/7iMUS0Cs1TGVRltahWmvWj9wmtEZa9XCIfGiVh1fZZ5zzyt/Gu7fVCQy078L1XMNu21aWuZi20G9xzrF161a8613v4u9+97vVNUCxZmjkhl9uII/SlycSCZFlIJ1OBynck0mMjY3hr/7qr/gP//APq2NCseKMj49zEv1isVgwqUu6roczTaznQDZl+JAd5orWoP4Uj8dh27YQ8VKpFH7jN34DV69e5c8//3zXb9Ann3ySbdiwgW/atAl79uxBJpPBwMCAEIVWus4i3YNSiQiql1sqlZDNZuG6Ln73d38XTz/9ND979mzXb2/F2ufIkSN8586d2Lx5MyYnJzExMYH+/n4xYUDVHlUo2oMSSBUKhUKhCLFz5yQfGRnB/Pw8OOfoz2YXvYxy0W7+oUVy5Mhh7vs+PMeGacZg24ET6ty5c2qAtgZw3SCtcqlUAmd+RZ1h4AwAAhGFaQwcHL5fEVV0HdAYfAQiJjQGaAyOWw4ccBqrCKE+mBaIcR44mO/DR7B8AGCaDh8uACbS1QIAZzo48+F6LuBzeBzgTIPtluCS21Wr1v9kPJihbRgGDG1lAhEnT55mu3fv5HErjvx8Htl0RgQkhCMQQCxmwq/UrtTNQET1weF7LgxNB+MMGjT4ro/8fB4aGFKJNJKJNGZvzwNcg2XGweDBrxhFy+VykPpaC2phaRqruEjJxckqzjwK7NcRtzRK28vExxjXAMZhAOC6D9MPap/CMOC7HrjrwPN8uL4nBErfJ1E0WAylzqWUYkBU3UsNru+L36ZXmRaVNDgaQ9ehaSxoH4LfYQzQdQO6rkHTdPjwoUEDNEBnOpjOoDMdABcpcF3XRalUwtxcDrdu3cLMzAwKhQJu3boF27aRz+eDdJiGAQ8e4KHifC9DY9UaOpZlwTNcwPfBPQ86Y0AiAcswwD0P0HXohhG8DsCxg9qy8BmYz2BqJnSmw/ZsMJ9B9zUwF/BdDvhMfE7jGnTNBIcH3QvW2Qt2QvDbtI19DviBEKsxBq1SI9ir1D7VtOp+kV2kUbO45YB4dSJE/X1Dgms95LSNjDEMDg7iP/yH/4DLly/zj3zkI+paoFhzLJjgsExRRE6rKzuKYrEYAGBwcBA/+IM/iK997Wv8Ax/4gDomFCvK9u3b0d/fj3g8HmSyqAhX1B9lWun7zVyh8nWm22rP0bUy6hqn0kU2Rt4ulmXBdV1R6/nw4cP4sz/7M9x9990dbGH7+PznP88uX77M3/CGN+Dw4cPYv38/7rrrLnDORfkaymAip7AOp11fCrQsypJCkGuXc47R0VF85CMfwYMPPris31IomnHo0CF++PBh3H333XjFK16B/v5+pNNppNPpmrFFqxlr6Hih8wdNRl3ucaNQ9AJKIFUoFAqFIkQikRC1DsmBtlhmZmba2qYDB/Zxy7JQLpeh6zqKxaISRtcY5Gikh5z6Rv6MLHxVU2XWum2iUo9VU5wGompQz9ETAx3HccSDnHdyWzzPA6885xH1HKuNrHUrbt++nV++fLmtfc1xHHCfXHaBBZZqhS6sv8jBmCa2m7w9aEDHOUepVMb09LTYZjQITKVS1RTUlbqhhYIOx3HgeR5isRh0ndykukiXGkXwMr3HQ+9Jrk1Ng8YN6AYD0wz4ug9NN6FrJkynDO65lbSvfo1LkpDXi/5WnzPodeKnUamSo57Lrke5zijVe2W6VhPIDC/XtW1RY3R2dhY3btzA9evXcePGDeRyOczNzQW1VivbOCxoxAxLpEGmvmtZFjh8Ue+Jzr+GYdS4SBljIvCs6zo0sCAFc+Vv8AikbZ2xQPpmrLbPe37l2Kvv0JW3FXW76raMEj0XpixeqbTY8jb1fR/ZbBZ/9Ed/hNOnT/NHH31UXRcUHYeO2XDfXw3xgxzpv/Vbv4XTp0/zr3zlK+qYULSdo0eP8j179mDPnj2YmprC5s2bV+V3uzW9LtCa+KtoDI1PaVtalgXGGO666y586Utf4m94wxt6YiMeP36cGYbBY7EYdu3ahfn5eWQymQVpdlloPLPSkDD7yle+Eu985zv5e9/73p7Y3oq1iWVZ6O/vx8TEBA4ePIhyuVwzEScqU0ErRE3mVCjWO0ogVSgUCoVC4sCBfZxqF9LsOrmOUKtQ/cR2MDExxjOZjLh5rbrLFGsJEidd1xXpQoFaUYr+ktuMAsiyWOr7PjyXw3V8IXbSDM9geR5iuhWIQpyLh4ZKxldWEYYYg844vMB3GYhP8FF1S/IagU5OiSgH4NotjgLA+fMX2cjICDd0qzLzteKwZVqNI6I6gCORqbItvepMWQqG27aNmZnbKJXK8H0fhUIBtm1jYGAAqVQKiUQCcS8eCEu8BN/1wHiwLMuKBc5ZM3BUBs5KoGL/FVqoSD1MGzoqlS0JZwjchyJ9rmnCsyx4ngXfrRWx66XWlZ/Lr3kNDn85vWtYtJP/huuLUso7TdNE+mVZbCaxnXOOYrGIXC5wjZIweuPGDdy6dQv5fB6FQkH0J7m2LP1WLBbsh7gTh+N7iHtxxHwPHnx4nEM3TTBdh2FZ0IxKvVLfBzStIqcHfdh1XXG8AahOUODBJ6K2Ye12R6RAHRY1F6Ysrh+cbuYADQv9S4FqD5XLZaRSKQBAPp/HP/3TP+GOO+7gp06dUgE7RUcJC6QrNVkgCsuyRM24D3/4w3jjG9/IT548qY4JRdt4zWtewx966CE88MAD2LFjB0zTRDKZXPV2dNtYgK7B3dbutQS5GmVhpFAoIJFI4PWvfz1++7d/m//CL/xCT5zvjh07xu6++25++/ZtGIaBQqFQk61DnigJoMbxuVKQW0/XdbzjHe9APp/nv/u7v9sT21ux9qBMO8lkUmTcofGVfJ/V6r1VlDCqzscKRYASSBUKhUKhqLB3726eTCbheR7m5uZw6tSZJQ94+vv7wTlftvNuamoHj8ViFYdcCUAgAuTz+eUsVrECkFhDAnZtatRagYUG9OH6kZ7nifpV8mdJWKKBUFjUia5fGh2EIqGVBleymCuLSCs9aKJaVBoz4PgOdJ1qYi5sd71xnxwgIcci5xzXrl1DsVhEuVxGoVBANptFNptFJpNBPB6H53mBY1FsAx+u68LyvMrs9AiRS24S86sNq7ONwkIlQGKcCd+xweHXCNTy9pb3UVgcDVINN66bKvevqEc4wBQWUnXTqBFvqe/RJIAbN27g9u3buHbtGq5fv47p6WnMzc0hl8uhVCqJ71B7ZdE7OD6qIp/v+8JF6vtVMZUxJpykcr2ywNladWnS8aHR34pzM0oQdV0Xri9vd0QGa2XXLOccHLVOuPB+od8Ji8ryBAj5e8sVSMlBmkgkUCwWYZomUqkUisUi/uAP/gA/9VM/1XbXt0KxWMLnrdV0h2mahnK5jMHBQXziE5/Az/7sz/KvfvWr6phQtIVYLIa+vj5s27YNY2NjIvPBageau81xSddheWKYYnHQeZUcZJqmIZlMwnEcmKaJn/mZn0E+n+fvete7uqtz1OHDH/4wO3DgAJ+cnER/fz90XY90kq7mNYYxBtd1YZomfvM3fxP9/f38V3/1V3tieyvWDvv27ePj4+PYtGkTUqlUTX1reZKwPG5vlio3KtMQ0H2TbRSKlUAJpAqFQqFQANi9eydPJBLwPA/lchnZJdQdlUmlUktKzUscOnSAZ7NZmKaJQqGAQqGAZ599Xg2+1jA0QIl6nXO/RpiSiRKohJjj+NA1gLGq0EOpYH0/SJkLxsC4B8Y9wHfhu3bgUHQ9eI4Lz7WD73oePNeF53EhDEUFqej/9Hsrha6bot6p7VRnZOs61SIFFtYApZqrWk3gPVheddvOz88LMS+fz6Ovrw/9/f0YGBhAOp1GIhYLhFLHgec4MA0LpmnCraR0jcUSFfHNBJOFUnKOosFAknPxOUbfobUhAQ/R7tBWHaRci06hK/8OvRflIo1ylcq/JQcxqS9QOt1cLoeXr74kBNIbN25gfn4e5XIZtm1X3MC6cOAbhlbZJEFtU87dmskEjuOgXC5L6XbdSupbqokaPEzTgmFwMMbheb5wuvqoHHchNynnHF7lUZNyWhKk/VA6aVnUXrhb+QJdU55k4HlezXYNRaERIQABAABJREFUL1d+LDcOQen1HMdBIpEQqecA4KGHHsLXv/51/NzP/Rz/1Kc+pa4Zio4gTxqQz1+rFcAm8cB1XRw4cACf/vSn8VM/9VP8k5/8pDomFMti586dfNOmTchms8LNI9dAXC0X22o5stsJjbEoO4pi8dD9C/U527ZhWZYQDA3DwK/8yq/gpZde4h/84Ae7q4PU4f3vfz9eeOEFTE5O4sCBA5iamsKmTZtq7rdWs46i7/tie5fLZbzjHe9AOp3uGeeuorPcddddfGpqCnv27MH4+DgmJiYwPDy8oI+Hx3XqnKpQLA8lkCoUCoVi3bNr1xSn4LJt22CM1TieloJt24uuB0EcPLifW5aFQqEAoFKzr1JzT7F2iXJeRgWE5UGMXANSFvjIDek4TjAYYpoQeXRdh2VqdUUvWr7nefC5WyOAcs7BfRfwsUAclQPZqwFjDKZpIh6Pw3HL4pirPW7Cx1C1BmRYNJS3Mwl6nuehVCqJupgkJPVns0gmk8hkMkgmk7DMIG1RPB6HaZpwnCDIaZoxUW+IXIpBXl5eFT4bBSjD79H+IocnEOlA5dIgNywEc84BvXa7RAVJFzqK67hN6DVeTYfrVVydtm3DcRyUSiVMT0/j2rVrmJ6exq0bN5HL5TAzMyPEUblPU181DAOWZaFYLApxn/q57F4mQZZzXpOeXHaPUj1SwABYMGkACPa14zhgjImap1Gu3ChXKefRDtJ6x0H19aowSiKv7Pwmd2xYIKX1d93lBzEYY7Asq0Yc5ZzDNE1s2bIFH/nIR7B3717+vve9TwXsFKsOpT03TXPJ90JLhQSDUqmEVCoF27aRTqfx8Y9/HD/5kz/JP/rRj6pjQrFovvM7v5MfPnwYU1NT2LZtG7Zs2YKBgQEA1UwJqylYdqtAWiqVYNs2XNdVAf0loOs6HMcBAHHPRPcX8r3P7/zO72BmZob/9V//dXd1kgjOnTvHfud3fgdvectbuK7rGBwcxIYNG2qypazWdSaXyyGdTgvHLk2S+G//7b9henqa//qv/3rXb29FZ9m1axfe+MY34qGHHsLAwABisdiCOFBU5ppWjoFwZhui264lCsVKoARShUKhUKxrpqZ28GQyCcMwRGrITCaDF198cVnLpQHrYtm/fy9PJAL3mm3b8DyvJt2jYu1CgqYs0gAkPAafiUqxWXWV1Tr3bNtGsVgE5xyGU03dZpoMZZ6HoQEaAhGKVYQez7XBfTcQRrkL7nqA5wPcC2qV+iRIVdOMhpFT96xkvyMBLZFIwHZKYl2DAZ5WM3gLa1WcMzCmgTESuLya5TKmwfc5yuUyXNdFsVjE/Pw8ZmdnkUql0J/NIpVKIZvNIp1OIx5LIB6PB3VK43EYhiXqvpDLVdd1mKYZuPc0Bk1DNb2rNCitJ9rW/J/JrlR5vSrfM3Txf7bwY6A6snX3T4PUv/LgWH4I17LrwkeQ0jufz2N2dhYzMzOi1ujs7Cxmbk0L5yc5QeQ+Q4HiRCKBdDotasJW9y+v6LKBu9rzqutOxw4FT0motW0bqVQGpqkjnUzANE34ftAXHKdynuSA7wO27cJxPDiOB8/j8DwO1we4z8A5E47P4Pt8wXEZFjdpu/mcnKeumMRg2zYuXXqhZkfs2DHO6bwtH9/tolQqIR6PL5iAQf2wXC6jv78fb3/722FZFn/3u9+tLiCKVYXOD5zzVXHUyZA4Go/HxQQKcli9+93vxo4dO1Q6RMWiuPPOO/nBgwdx33334YEHHqhkRzDEtZMmxayGSBOeHNZNeJ6HYrEoxjfduA5rARojWpZVU/uWSlfQOe+P//iPceHCBf7Nb36zJ853169fRy6Xg6Zp4vgL39Ou9DGYTqdx8+ZNDA8Pi/5L15u3v/3tuPPOO/mv/uqv4vjx4z2xzRWry/79+3lfX5+YxBuLxaBpmogNhIXS8ITrZoTHDd040UahWCmUQKpQKBSKdcvOnZM8m81C0zQR7KbaJpcvX1nW3aJpmmCMYWxsjF+6dKnpsnbtmuJ9fX2irgkNfDVNQ6lUUkGELoDEkiiBlAbvJLyYZrV2UFgctW0b169fBxA4YWKxWhejZVmIWwYKhQJI3De0wJXnuGUhCpKrLZhp7ouZ5q4XXXsxPGAKC3/thsT/WCxwbwbBdH9BmqAg6IGa//seF+JkEJhcWEOSgm+0TSlV9fz8PGZu3UIymUS2IpQm4kkkEongeSKBZDKNWCUNL21/06ymBNatwNUo18iUt1+4xixRT3gOu0TrbfeqWFf7vQWD2xpxOVwPtzY1LP0lQcN1XdyamUaxWMTMzAxu3LiBGzduYHp6GrlcLnCLutXUu4Ts2KQBeDweRzKZFCmPq7VVa4X5QCStLpMmGriui1KpJPZdMpmDYWiImxZMyxCzqkn8MHVDHFPkUIlylFJaXs5rj0subTP5b3hb0nFl23bkteL8+Yts585JLjtH5XqzyyUej4u0xOl0WlwvSBCiAEoqlcJ//a//FQ8//DD/rd/6LXz84x9XURDFqkDHCPX91cYwDOHwoUlnvu9jcHAQ//E//ke89a1v5e973/ug3KSKRuzevZtv3boVe/bswcGDBzE2Nob+/n6RQt62bXG9dxxnVbK9RKXd7xbo+q7co0uHxDiajBZkpQgyb8iTUTzPQyaTwQc/+EH8+I//OH/22We7/lz3zW9+k91xxx18//79QqCMx+PIZDKr5iD1PA/Dw8Nim9O97uzsLEZGRvDd3/3duPfee/Ge97yHf+hDH+r6ba5Yefbu3cvvvPNO7Ny5E3v37sXWrVsxNDSEvr6+mnTOuq7XTJxfjrCpHKQKxUKUQKpQKBSKdUs8HhcCS6FQwPHjJxgAPPvs88te9hNPPMHGxsZ4K86JAwf28XQ6LdJUUrvm5uZw+vRZdcfaJZw5c4ZNTIzxoMakh1QqJQbsNKu5KpR4wknn+15Fz/Lguh6KxRKcsoN8voCrV1+CYRjiu4FrQUcmlUAikag6IK1YILQiEIOCWpA+uEtOObfGJUfuTd/3US6XYVmWCFiF0/yuFPPz8/ArqX7T6XRFDCvWtKHalsBJCK5VhEmI7RqsEwVGgoBbNU0rpR3Sxeu5XA4lTUM+n8ft27cRi8WQiCeRSqWCbVkRRWlmPj0PHKWBczGeigvRVBZJaT/Rg4TThY/GdZLCM4EXpsiNdpDKAdOwGEduTBIN6UECcj6fRy6XQ6FQwMzsbczOzuLGjRuYmZlBoVAQLk7HcWAZZqSgLguRQ0NDQZ1XySVCLhvX95FIJFAul6EbgcCtMQ2O54LpGnyHoVC8jflcAYZhYGBgAJcuX0EikYAVCxzT2WwGvusimUxieHgYuq4LxyqlStdQ7cvUDqrdSU5qcluYpgnH84WoQ8dsUL+3mh6Y0gOeO3eh4bn5zJlzbGxsG2eMiQkOtI8WFDNdAnIgnuotAlVxnYKjVIPxz/7sz/C93/u9/D3veY9yNihWnPn5ebz44ovo7+8Xk0zkfrqScM6FaCW7ixhjyGQyME0TIyMj+IM/+APcf//9/Pd+7/dw4sQJdUwoanjta1/LH3roIdxzzz3YuHEjMpkM+vr6AFSFKLlPt0scbRaopuso/e0m8vk85ubm0NfXh3K5XPMeXXeXG6hvJhp3uxAQj8cB1E6kk8eaNHkKAIrFIvbv34+///u/x+Tk5Oo2dIX40Ic+xMrlMv/Wt76FqakpTExM4PDhw8I9K0P3o+GSJMuB+qlcC5Jzjmw2K5a/detWvPe978XevXv5H/7hH+LcuXPd3ekUK8bU1BTfs2cP7r33Xtx3333YuXOnmIAblYFjuXV2aVxDy6X7o9W6P1Mo1jJKIFUoFArFuuSuu+7knPNQWs/20sw5Oj6+nQ8MDIh6MiSwFAoFZDKZZddBVaw+Fy5cYqZp8kQyJgTv8EzPsJsUgHBAktB0be6aGCDJQSN6vz+bQSIRQyaTQTqdRjIeCHe6Uf0M5xzMJ8HMr/4u0+BzBs584VyVXX9AA2diGykUCpibm0M+P4BEIibENMcJ6tbJtSIZC5yzvheInEaTAWKzdpNISPU1c/NBKllKqUuiKImlVGeIBNJEOiEC/sK9WBFLKUhDgjaJpbIrlzG9pj+EnaXh2pXyOgWv1daNBVAjTtJ+levMynVtSRQld2Y+n8f8/Dzm5+dRKpVw7cZ1lEolkQqPRGvZtSKndQr/fiKRENtAftA2iJuBwFytXRqk452enq4ZpJfLgSN6fn5eLBcAPNdGNptBspJm9sUXX0QmkxE1ZDOZTJASWXKUytvQh1dpc20ArZFrOnCYciE0t8KlSy+wXbumeCdcPuQsMQxDuJ3e8IY3YMuWLfiXf/kX/pnPfAaPP/64CtopVoRLly4hFouJ41bOiEEB/k6RSCRQKpVgmia+93u/F2NjY3jkkUf4V77yFTz11FPqmFjnfOd3ficfHx/HwYMHcccdd2DXrl1C/FhucLodyJOfVru+73KZnZ3FlStXEIvFkEqlMDIygkwm09ayDt0ugC4XOu96nicmom3fvh1PP/00v/POO3ti43z0ox9lBw8e5K9//euRTCZF6lHHcWompIXvs1fzXoxzjocffhhvfOMb8bd/+7f8wx/+MC5fvtwT21+xfF7/+tfzffv2YefOndi6dSu2bt2KLVu2iMnVUef2lTq3rXTGKIWiW1ACqUKhUCjWHVO7dnIKLpBzrhMDagoakjvLsiz4vo9cLteSQ0mxNjl9+izbu28nd113wcxlQk7hGYhNC1PdkBOUHJLVgb2PXG4elmUhnUxW0sIGAqNlWTAMAzHTCgQfHcL5Jgu1rscBLbgNjKoDVa/d7eTChQssnU5zANiwYRjJVFzMenccu+K+DFy2QCUQGEq1uxAa4NWbXFBxlFYcnNwHXM+Dw4NUrjRIDKfVJaFJdoxGCaQkCsZiMfEZetQI3lo1GBgWEOUgTjhoKH9HhtLkyqmd5TTLckpYx3FQKBRQLpeRz+fFg9LY2rYNj/s1Ar7cf4IfZPB9T+wXcmMCDLpuIJXKwLLiFadsIAhrmgHGdHDuSsIoKrU0A6fDp/7+s5Ed7p3vfCf/5je/CcuKY35+FgP9Wdy4cQOMsSDQmkigWCwiHo8jlUohl8vV7h/dqNnO0Hil3awmpXOzPk/Ha7jmaCOqaYVXVySlVFy6roNzDtu20dfXh6NHj+Lo0aP4T//pP+Ef//Ef+R//8R/jkUceUdcaRVs5c+YMbNsWabvp2Oy0OApUJw8wxjA4OIg3velNeNOb3oTz58/jkUce4X/8x3+MJ554Qh0T64yHH36Y33PPPdi1axe2bduGTZs2IZPJIJvNCrfNWnBs0kSDmZkZ5PP5TjdnUdy4cQOXL19GJpPBhg0bauqXK9oDTYiiNOOxWAzz8/PYt28f/uqv/or/8A//cE9s7Oeee4498MADnI6DZDIpJu+Fy1ysZv+i7b5hwwZs2LABjuPgne98Jx5++GF88pOf5H/xF3/RdAK1ojfZt28f37hxI6ampnDkyBHceeedmJqaQiqVAmNMlGeKYiUnLiuBVKEIUAKpQqFQKNYdqVQK5XKpJuVbJxw+J06cUgOkHkZ2A4aDQLIIFlXv0Kykv40Sbjhn8P1A8Mrn8xXBy6hxO8ZMK3DQmSbMUL1MMB2260PTfFHDKJzWrJ4Q126ee+459txzz+Hee+/h27dvR/9AFolEArZdrgQ5DJEKmFLsGoYBHhGkXMwx3KzGZ6FQWLD+spgpC55yLVR6P0pUlYVSrjHRjiiBlH5XFkijXiPCAin9nwRREkepLmcul6t5jVLt0ncNy6ypjwugxtFu6taC36eUTYlEkP6ZAsrhyQBUg5dzX9QY1TS9oXDy3ve+V/zYr//6r/FvPfE4stmMyAAABCk9SQgmgVvUJ9Vra8XqplYJoAXblOqjclRdslH9gt4bG9vGFyOSrjYUIKxu7yDtHNVPpf119OhRHD58GGfPnuWf+9zn8E//9E84ffr0ml0vRfdw48YNmKaJDRs2iPMVZVToNHQuoOODUn1u3rwZ3/d934fv+77vw7lz5/iXvvQlKKd1b3Pffffxqakp7NixA1NTU9iyZQvuuOMO9Pf3Q9O0mjID7Uj/2g6o39L1vZtgjOHmzZuYm5ur1J2v3aY0eUGxdAzDENuRtnEmk4Ft23jDG96An/u5n+Pvf//7O9+R28DJkyfx6le/Gvl8Hn19fWJiWPiemViNY1hOi0r3lqZp4vDhw5iYmMDP//zP4/HHH+ef/vSn1T3XOuH+++/ne/fuxc6dO3HnnXeiv78fg4ODGB4eRn9/P4DacgDESomi4eXRGFahWO8ogVShUCgU64ptY9u5aZowdU0IBeVyeUHdEoViOZAoSu5kqjkqB4drRZjaAXxV7ApeIzETqDoYfJ+jZNsoOw70kg6rZAuBNBmPwzCCtLSmbdYIdZqmweNMCHpRIuxqCaTE9evXMTQ0hJENQ8hms6D0167rBqIW9IpjkVcCILXf5yHHKOe1tTrD2BX348L1rLj9NICDw/PdmrS1hCGli63+Jl8gaNZ78IjtLG//+rVLab1q/0+ClyyO0l9KJxyuPSqvk5z+Vtd1eCFBX07BTAN4+bdJPI3FYkgmk5GpocOCqmHoSKfTAIBSqeoobcb/+B+/xADg//cjP8hN00KxWARjQT+OxxK4PTMrHD+e68Oxq24KQ9ehaQymb1TO+cF6C7d3REBNZinHRSccDLZtIx6PC1FKboNcOy+TyaBUKmHXrl04evQovvGNb+Dpp5/mjz76KK5du4ZvfetbKnCnWBLDw8MYGhqCruvivEDnpVZqs68k5XJZTGQhFzqdq2iixt13340dO3bgu77ru3DlyhV+4sQJPPLII/jsZ6Nd7oruYHJykm/ZsgUjIyPYuXMnDh48iEOHDmF8fFxcj6gONYCascFKleNYLHQNnpmZwfT0dKebsygMw8DIyAiy2Swsy1qQrr8d27fXa5A2Q54glU6nUSqVAATX/FKphB//8R/H9PQ0/9jHPtb1G+KRRx5hb33rW/n169fh+z5SqRT6+/sXiExyiYiVxraDsRiNX0zTDEp55HIYHh4GALzmNa/BoUOH8BM/8RM4deoU/9a3voUnnngCX//617t+nyiCuqITExPYvn07du7ciampKYyNjWHDhg3YuHGjKCMjTyCl/kLnQHmctVLnrHD5HgAYGxvjyuGsWK8ogVShUCgU64p4pWad73lilq3v+zh9+qy6GVS0DcdxUC6XUSqVKgJFbaAtLLoxptUM4JlIwVr/N4KgUrXeJIljjuOAex5M04TruiL1q+M4IgWvD00EUcICqSzYrZaz+vz5i2zjxo28fyArAurkfPQ8D0bF8SO7agnOuawvtwSJdzXLAMC5X+O4o/fCA1nuUQ1Lv+Z9WTSUn4cFxnpSYNjFS3+jBFL5M+E2yusUfk7rT3+jlq+xqlAo1+ekz3iut0Cwj8ViSCQSIk2y/JvhvhWLWSiVgvqmwfvaoiep0HfJlUaphOk9WSSmCQK+YUDXNZB+TrVgm6XYpcC4xjThPmvG9u1bOX124cSIlb3chN24ct1Uy7KQz+dFnSPLsuA4DlKpFF7/+tfjTW96ExzHwcsvv4xz587xM2fO4NSpU/j2t7+N06dP49y5c+paqWjK/v37ceTIEcTjcfT19Ylrz1oQJ8jdDwD5fB66XnWwF4tFccwODg5icHAQ+/btw/33348f+qEfguu6/Ny5czh+/Di++tWv4plnnsGZM2c6v1KKSA4dOsT37NmDffv2YWpqCiMjI5iYmMDQ0BD6+voA1Dq+AIjrCd0jkSvN87yaGtmdgjJB+L6/JlJWL4Z9+/bhTW96EzZt2oShoSGRWlJ2Sq13gXO5uK5b00/j8Tjy+Tw0TcPo6CgGBgbwwQ9+ECMjI/z3fu/3un5jfupTn8JrX/taMRGnEavRd2KxWM1EIKq7HY/HYdt2kMVE17Fp0yZs2rQJR44cwZvf/GZMT09jfn6enz17FsePH8ejjz6KL3/5y12/f9YD27dv51NTU5iamsKDDz6IsbExbNmyBdlsFplMRozHaUwuZ9UAIPpEOGtRVIap5fZheawaVU5HnV8V6xklkCoUCoViXRGPx+H7PsrlMpLJJM6cUcFeRfvxPE8IfEFKVj1SPAsPVOjhOLUOP/BqLU4OD7qpgVXSgVIH5ozB94NakMVyGY7nwfUtmB6H4fqwLF5Jzxv4JA3DqJm9HxbJ5PdWg69//RtseuYmHxgYwObNm5FKpZBKpYJBpVNto+u60KC3KN7WOkMJ3TRqamwKJZozcJ+j7JBwVztw5JwDPoch1TcKC6TyoJdeI4dldXmeKKdaMwDmta+FRVJaJw31nMjVVMBRjuB6AiAtR7QFPHJgTv83NFOsu+/7iMViSKfTSCaTYIzBcZwaF214+aVSCeVyWdRqZUwTLodmvOtd7+LHn38WiURc1DYkwXtmbjZYZkVYJ4HUNM1qGmBTh+t74NyEVqnDWw2GV7dhbZsr4m4liGGaJnbunOTkyr18+UrNRh0f385JKO6UKET9m8RcOWCaSqVEn5SFAcdxUCqVkEgkMDo6iuHhYdx7773iuLNtG77v8+npady+fRsvvvgiLly4gLNnz+Ls2bO4fPkyTp48qa6p65zv//7v53feeSfuv/9+JJNJESCWrzWdhK59rusilUoBqKZCTCQS4v9U11jXdfT19Ynr0ejoKO6++2786I/+KJ3f+fT0NC5duoSXXnoJTzzxBE0wwJNPPqmOhxVmamqK79ixA5OTkxgdHcWhQ4ewadMmbN26Fel0WvQ3uaZ4lFPR8zwUi0XhJK2dxMY67nwmdF1Hf38/du7ciQceeADHjx/n//qv/7rm+9nevXv5q1/9arzmNa9BMpkU9w6N7kuWQqfPL51GngRFKXbpPEfu+Vgsht/93d/F5cuX+d///d939Qa7ceMGksmkSLEbJTTJf1cDuu/XNA3xeFzcY1J2FaA6uVDTNAwMDAjn6+HDh/HmN78ZpVIJrutyx3EwMzODF198ETdu3BDXl/Pnz6ta2avMzp07+b333ovt27djz5492LZtG/r7+5HJZNDX14d0Oi3GJcFY3hH9zrIsWJYlagTTxEXqE1HXpPCEkZVMES2PexSK9crauMtTKBQKhWIVmJjcwS3Lgud54iZWoVgReLh2pV4Z6KDiQojB9z0wtnBAFAhloVmd8vI0Bs93FwycfN+Hy91K4JeLNKokEnHOhawmp3iVRdBw2tbVHiidPBE4uQ8e3M/37tuNkeFRFItFzJRmasQeDi8w4XENYNX216/XUvt/z3HhoypqkqBI3w2nhq2ZyQsO1/ehh8RR+v1wrdAFaWY1DTo4AL+SCjj4y7kn/s+YjkDU1cAYRyD00uuAzvSa3xVrWScQFB5UU40meZvJbZbbLm8b6k+yEMwYg2VZSKfTwo1YnRhgiHbK3yPnomXFhMs6nU7j4YffwgcGBpDP55FMJuG61dqmgWNUw7lz55DNZnHjxjUMDQ3B8zzp866YqU3tC6cXNjwKnvnQdV+If77vQwsJ3WHkttO6eZ6HHTvG+fnzF9nExBgnIYhSaMnbb7WQ67hRkIYEUsdxhFuXPlMsFoXLlt6TBVU69mjmeyaTwdatW7Fnzx6xXW3bRrlchm3bnPZ5uC4uUSgUhNud6uBSPT3P81AoFMR+k5YrRKxmYnq4nm+4xm8zBzClXJb7rny80b1D+L0oJ3cUzSaeyMuPdLBLfVTUmjZNWFZQezqTyYjXY7FYTX1qCh7TvqE6wLOzs7h16xZmZ2dx/fp18f/nnnuubufdtWsX37RpEyYnJ7F3717h0COX0gJn+hpITwpATM6giROUbpf2C02qoHMJOQzlQCbdSwLBPkkmk9iwYQMcx8F3f/d3I5fLYX5+HrZtc/oO1UimbAg0ISGfz2N+fh75fB7lchnXr19HLpfDzMwM5ubmUCgUUCgUxPvknq/X/5rR7HxExzl9NmryVCPC5/rw8Uf9lPpnKpVCMplEIpGAZVno7+8XggdNvEkmk0in00LADqeFJ/cYTbqhZckihLzNZGg/6nqQ9p3OkeG+K9fh7iS6rqNYLCKRSOCHf/iH8R3f8R3I5XL86aefxvnz5/Htb38b09PTuHr1Kp566qkFO3vPnj08m81iZGQEQ0NDGBwcFAJNKpUS6Vmpzv309DSuX7+OGzduIJfL4fLlyzh16lTDTrR3716+b98+7N27F7t27cKePXtgmia2bt2K/v5+6Lpecx6nPqVqkLYHmsxB6cOB6nlPFmj+4i/+Avl8nl+7dg2+7+PMmTM4e/Ysrl27hitXruDll1/GrVu3cOLEiZr9PTk5yTOZjKilODAwgEwmg1gshsHBQXGd4ZyL9LLz8/MoFou4evUq5ubmcOvWrUVNqBofH+cbN27EkSNHsGfPHkxOTmLjxo3o6+tDIpFAOp0W97b1znGrdS8WHleQcNvIuQdUy2XE43FxrnNdF/39/di6dSs8z8PDDz/c8Poi35vato1CoVBzfbl69SqKxSJmZ2cxOzuLXC6HQqEgyppQXWO6tlCb2nV9aYZt2wuuafJ4tNl5+PLly2z79u086hoGQEzKMAwDyWQS/f394jyYTCYxOjqKwcFBjI6OIpPJwDAMkWUikUiIeyz5Xp+c/FTegs5h8qQaOu7ka0pYMI8aM6ykuzN8X6lQrHeUQKpQKBSKdUN/NgPXLlcGHRoymVSnm6ToURhjlUKWGrjP4Lkcnu5DYwYY08A9gIFBZ6FAEAN0fWEAvVZHZTArzjdZEJSDkPTc83xw7sB1PXieD9/nsF2vIt4EdUxd1xNBxkCg0yQxcvUcpLVouD0zh0x6Dul0Gv39g2Jwz7kPBoD5HkhApO1AQQgSGMJOQM45PATBbh2sqptyBKJ05V9NIJ8HD3oPerB9aCjJNBa0R3aa+r54P/wz4KjUuqQBe/Ac0Ct/eZO/lWUswnURdsE2C0DqlGKwMgNeI1HF98A9H74WOD9s24ZlWdi0aRMYY5idnUU6ncbJkycxOTkpgnMUJKAAHbzAB+uUXejMgK4D5aKNdCoFx7ZhmSbcSpBGY4DvOTB0BnAG33NQ8hxkMn0ol53K+phwHA96pba0ruvCb42KGO34LnyXw/GrdWANg6PsOAAYNMMUAQwe/BT8yn7WjGBSAnc5dJ3BcXz09fXh1q1bSCaTME0Tu3ZNcZo1DgDpdBrT09Mi0E7bKgi+V2seyvunXbPD5f1LggQRFkcBiEBcPcLuKRKtqYYctb3+BIWFyIEvElHpNcdxat6XA3VA8wBZPSGuVSdJqwJmPZYbaGpl8pYcZAuLWNTn6Dogu2rI2UKQi0FOaU6BUhK8SZgmaFmmaYrAdDabRSqVEsKjvC3ITb1WkNsj9xV6Lh8bYdegPAFGfk8OknqeJwTTKCd+eMKJXBva933Mz88LAVXeLzTRqVn/X+lAZzOhu1lwVxZNKd2x3D8ty6p5T34/PAFJFmKjhPhwW6MmSIT/TwJD1HqvpINnMSQSCXGOHB4eRjabRX9/P+6//37Mzc0J8b1cLnM6n9K2IhHUMAzEYjHEYrGgXr00gYL6ZniSiuu6mJubg+/7XO6TQHWfkJiQyWREisl4PC7EANrecnpg2qatuHSb9e+1sH/WArQtZWEIqPZ3TdPExITBwUH4vo/du3fj2rVrov9Qtg/Xdbks4FD/sSyrpv+QO1U+x9FEHJqoViwWhXhn27bIxCFfY+T7CXkSkK7r2Lx5MyzLWjDxR163Riy3/yzlvjv8nXoT8KL6P50DCXIoDg4OLhgD0l96Per6ks/nayag0b6h/dvs/qPZ/dFyjz9ZDA1PDmuRhh+U+wjdx1AfNgxDnKvC/Trq+2EapWCnfRj+/ajnq4UsOpMwruqPKtYzSiBVKBQKxbpgz55dPJFIiMDB3FxOpBxSKFYaEkyZxgCfBkGtDYZaHWyGB5LkjAkLEfLyZHdpOMjVSZ577jlWLpd5LldAJpNBJpNBKpVCPB5HqVSAxhjAgxqsFDSVHTnhQaocPDABuLw9wm9YeFzcdmNL/Lt8ooL1MrZtC0ea7/uUWlUEDwANN2/exMaNGzEyMoLbt28jHo8jmUzi/PnzmJmZqUlVSAF9CqwF/19CMIAt3G9RziZyMFVTHfs1x4RhGMIZFnxfq3E9hWech5+nUinMzs5icHAQt2/fhuM4mJycxOc//0X28MNv4VeuXIHv+8hmswvaWi8A3E2zt6MCOc36VL3PRgW/mgWKlhLgDB+ry2Gl91WrAm5Y8KW/C1N61y6TrgPh7RwlcDfbr3Iws9PXjbVCve1KRE2MkF0yg4ODq9PQNUK97VPvc+H+tt76XfhcSdc0SuW5ZcuWBZ+Vt5Gc9rHeOSL8f3miSlSNdnlZlFo0fB4hkUY5RDtLWLiTJxlwzjE2Nlbz+cWe36PuhaP6T9i1FlWOIfweUE0THHZkrpfrEN1DtjJuI3FZvr709/eL97tlmy3m/nK5yCVCwmOLbrpPXwzKQapQBCiBVKFQKBTrAkrnRIHD4eFhzM/Pd7hVil4lPNAID+6WOoO5GsiKFhnk+qbhZdAMUQpUkOAl0u9Ks47p+50cNJ8+fZqdPn0aO3bs4OPj4yLtXixmolQsouy7Ij0rzS43YhYMVAUEnwb/8oOxJvOLWyNq26wVd0kz5HZGtZlS7JGIKKeAKpfLADSMjo6iv79f1BHTNA3Xr1/HpUuXaupchVM4t7KNWh2oh/uoLHLS7wQD/4XrSikTA8eLUalBWq1d1egYzOfz0HUd8/PzyGQy0DQNn//8FxkAfOYznxVfPHBgHyeXBW3TIL1m/fXpVhbT9kbOCqC+ALiU32r2+534/nJp6gCPeL+egzFqX8hpSevtHzmoXu931ivN+mvUdpe3pVzDOvyZVu4f1irUblkIWcx36bHeBbZw0J6g1M/NxARZ4AxTTzBpVCMv6v4h6nNrpYarYiHyfiSBKCwSAQvPXfJxKTs+w9SrR0+/Xa8tMvS9cEaCbhH52k2j7RS1TeWyC/Jf+TNRr681ltu+qFroch8Onx/lMVCvIe/zZs5ghWI9oO5SFAqFQtHz7Nw5ybPZLFzXRTKZBGMM5XIZzz///PoaTSlWnagB2HKXE349PNiNclfQ4IfSKMntqRWSFoquneb8+fNscHCQ53I5UQMmmUjAtkuiLpvjOCItlCxyhdM0tSuQ0uj7a2W7NaORSEqOXHl7Ud083/eRSMSxceNG2LaNYrGI/v5+XLt2DSdPnhR1xSgtGNXLk7/fjkBWPRdWVDCD81rRh9KLVdNSawuOB3k7hfdpNpsFYwzXr19Hf3+/WEeZe++9h8vBRdoWQQrg+gHqbmBBCvBF7s8owSiK8PHb6u81O87bJdCvFK06aOudr+sJFFHPm7l1wr9Lx0cr16T1Svgc16i/RzlUFpPCtpsIC/D1+lHYAS0/wkQdA70azI4i3Ndook8UdPxGTUAJb+NWjuF6/VBOAR3+DUXnaXZ9rCegR90fRe1X+d4xiqiJN1H36PXa1+tuvmbU23/hc2G99+tNPCPWw3EaNbmLkPuhvB17ebtQNimFYr2jBFKFQqFQ9DypVErUL4zH43AcB9lsf6ebpehxwo6PxQzmmw9cmztS5WCYLJJSAC3sppPbKNcDXAt861vfYo7j8A0bNmBwsB/xWAypVEK4SnO5HEqlkqilRzX45PWTWSvr1WnCIikQ9CfahlqlFqnjONA0DalUCslkEn19A8jlctB1HalUCi+99BJOnz6NXC4H0zSFYE2CIOdBHdJmgbPFUi9gLvf54DOs5hgAqiJSWCySU05HuSM45yL7QF9fH7761X9l3/Vdb16wUgMDA7hy5UpNvT1KW+x59R3m3UC7g93hPthpgaPTgbBmgaqovrtU6gW45fflz4WPF/laQfuvkUNtvdBsAko9GGN1JyCEBcZGy+gk4XuSRk6l8LkVaFyHMkqgiXrey8jbM9yXaCISEXX8tnJ+rb1+Lvz9RmJ1PQc7nSvWuwN4LdBsskHU5+XxTCOa9S/XdSMnP0RNTotqQ9Q6NJog14tE7b9Gx6z8nfD9hbxd2zHBqdn3W5mg1uoEo6VQz0krn78aCfi91L/ka7FykCoUSiBVKBQKRY8zOTnBg4C0h1QqhVQqBc45/umfHlkfkRRFR6D6ODRTejmBu1acOvUGkPVSVcqvk3gTDlauNY4dO8YA4ODBg3xifDt0PagFSelLLcsSKYSLxWJkAKZdAdRGQYS1uv1kGgXHKDhBbmMKaCYSCaTTaSSTSdFndF3HzMwMzpw5g9u3byORSIjUzRTop4CCPFGgXfshalmywFANvFWdWRTcICFU/jxNIIjaJvKxFovFYBgGHnvscQYA//APn1uwQvF4vKYGq+M4cBynpl3yb3QTKyVENBKA2iUGAmt/mzcTEFpx04bXsdH5KhyMbDUAyBhrms53PbKUAOpihL5uCNC2sj71Xo9KgQis31SaYdrVP+oJBHK5hahzTbP9KQtgcsr69b7f1grLdQ42+lzUOCLcb6IEqEbLrSecrsfJEe2g2f3FcrflSn+/3eO4qIlfUeOKXryvke+5lYNUoVACqUKhUCh6nIGBAXieB9/3cevWLRHQVihWEl3Xoes6DMOocfs0CzARzQIYvt84zSGwMIWu/H1y9QGBWyNKGF2rAa3nnnuOjQwP8sDhaIj6l+l0FkAgRllWQaQUpuM/2Ga0DZY/0I0aMHfLAJoxPcLhw6QAqV5JWawhFrOQTqeRSCSg6zpc10e5nIOmaXj55Zdx8eJFzM/Pi3q2NMiWa9uSCCk7O9tFVCAuLGxSN5ZnSVP/Dtqk1Yg9jVwNFPS9fv16w3b9/d9/mt1xx0FOTlz6XlD3tHYGe7fRLodBvfNhq+fJRstv9N21eF6TaTaTv1UHWKP/y8jHSzhFZ1TAmzIQyN9t9bcU9R2Vzb7TqsN6pZ0graaAJsLrR+nw6b16zrB6fXi5DqVeRN7n8v6JcujK79e7z2s0iSrcv8Ln83oOYCVwdxeLmVQTft7K9beegzyqREZ4mfJnw/15PdDoHNhsAlX43j+8H5rVCl7uNl7M9alZ+5dCve1Dz4N79IUTa9fqmHgpRE06UA5ShUIJpAqFQqHoYfbs2cV1Xcfc3BwGBga6Yta9ojcggVR2Z9a4SZcwwGs14EB/5XRA4TpVsngVdgssRsjtFP/8yL8wADh0cD/fuHEjNm7ciKGhIcRiMXieh2w2K+pjFotFlMtlsc7tRBb7uk3sCguV8nrYtl3jGA2npDJNE+fPn8f58+dRKpVgWRbK5TJs266IqK5Ia26aphh8M8ZgGEZNgHypbW/0XtR+CYtA1B8CQcgQx2y9YL28bNu2MTo6inPnLjRsZyaTEZMReik1VzOxbbkOgKhA6FK/H8VyBZaVFmiW2k9a3VbhSQLh870ckI4KCkZNumnWjvVOK67f8Gejzj3hZUWxls4zUevaLAAfdU2VA/iNamyuN8LHKlCtS1rv+G20HKCxQCD/bba8eveVirVBPcG6mZBd7/VG35PHJPIEm6g+utiJINS/oib49CKLPY4aTVjpxDG53OvTctscdY8qC4Ty9Ukeu7Tr99ciUZOkFYr1iBJIFQqFQtGzUHrDeDyOUqkk0hsqFCuNXOeQBCM5aMWbpJBs5IqrF7AOLyv8Gd/3hUgbuCwtaJoG27bh+z4sy0I8nhRON3p9x45xfv78xTU5Inz2ueOsWCrxWDyObF8fTMsCR8Ufyhhi8TgSySQAoFwuC9HOdco1M8/DtTHDAT3ZiRsWFul9+Xl4BnyYxb4eRkO0qEEPcgXXW668LuSwZYwJ13M6nUY8Hhd1Qz3PE+7HcrmMp586hlKphLm5HM5dOB/ZN7KZfh6zEhgZGYHncvge4Hk+NAbRD8Pta0VIbCzALRQQgs/WzpD3fR/lchmGYcCyLAAQdVblbUrHCaUMpOPENE3kcrm6bST+9V+/zh588CgPu5lp28vbYa0GJ6LEimY1FNvJSgSjlrvMtRoga7VdzQKUyxWgo2gkEC7FUdmItbh/FtOmldj+q0k7A/iLWWa7tsta374yUW1dbornZgJ2M8JCWbuP927aP6vJYh26ixVAl7o8eq9VkWmp1zG6h1XU0m3Hy0pMZG30f3ptMa93M/JkAnkS9VodgygUq4kSSBUKhULRkxw6dIC7rgtN0+C6rqhRqFCsFiSKhINEvu9DjxjYL3bZjaiXckoWoAqFAjRNQyaTQTqdhu/7KJVKKJVKYCxw+pmmCdu2F9W21ebMmXPszJlzmJgY41u2bMHw8DA2b96MZDKJeDwOz/OEszGbzYJzDtcpw3Ec4Xok0Y1EU1pnWUiUH+SArDegDM84Jug1TY8OYLY6QOVedJ3M8G/VC1SWy05NHdFYLIZYLIZEIiEEwXg8Dl3XhVBu2zYuX76MCxcuoJAvwbZtXHrhct2OOzc3hxs3bghhkc7DhmHAdkoN12/ptXCqExMawRhDMhlMBrBtG57ni/4CoEYoldMG+76PQqEAAC1NuPm3//bH+GOPPYZ4PA7HcaDrOkzThOdFC/CtOidWk6ggd7N+utRAy2IcSitJs/Nrs/XrdJC22fHTqoOxHu2uYRb+fLP2N/v95aaKW65Dc6X7b7P2NVv/5e7/ZjSb9BHef50+3ruNdqTg7uTvL8ZNvRQ6vX06RavbcqVrDTZrR7Ptv9Ln/17d/72Cuh6sPGHhV9UgVSgClECqUCgU64TJnVPcNE0AEC6YQqGA82fP9eSdaCKRQD6fRzweh2VZwlWlUKwGVHtUdorRoD4snAKNZ6lGBRn1OgIbESXMht83jKB+J/2fPmtZlnAMkgO2G7hw4RK7cOESJibG+O3bt7FlyxaMjo6KdF4kzgEAYmaNKEUiKqXjjcViomam/DnZoQlEpyWKEibDz+UATtT+bSpQsGiHaiMBV+5j5IKMx+OIxWIwDKNGFLQsC8ViUbT15ZdfxpkzZ/D0M8+2fL04c+4sy2aznPoaEKTm9X0fpqVHBknrBdZbD9gH31+Ywrf2+7Zti+PSMAxkMnHE43EwxlAqlcS+t2KBE5cm1+i6XuO+Bk41bE2hUIBpmkilUpidnQVjrHJdSta2usWA0GIdIitFtzvcmtHtAdRuOWfXY7nt7/T+W2knRrPjq9v3v6Ixne7f7fz9VlKyL5ZOb5+1zlo/P3T7+V+hWOs4jiPG+aZpKve1QlFBRYoVCoWiR9mzby8nVxAA4V6RxYJUKoXzZ891uKXtZ2rXJPc8TwSzDcPA/Pw85ubmOt00xTohkUggmUwik8mIGowknHmeh5jkZpadifJrwMLUrdXnrTmEotyj9DydToMxBsdxYNs2kskkbNsVzjhKp3ru3IWuUjtIKAWCOsSbNm3C+Pg4NmzYAN/3kc/nYRpVMZBEMsuykMlkAECkQ6WUqOFHPp8XKYpkEbWVQB/nHGbMEs+jaBbgcZ2FNTzllK0k9tG6maYJ0zSFEGoYVk37afYwrUvgcvRw48YNvPjii5idnV1SivLp6WkAQF9fH7LZLDKZDJLJJHzeWMBt5jCpL0AEwvPC7Vf7+UQigXK5DMZYxTUbExMXTNMEZR8wDKNGKKb6qZ7nCbdpI44dO4a+vj54noe+vj5YloVEIgHHcUXt30Zp5xqlSV5N6qVErOdebkaj46RZiuV2sNxt2ehYXw1xuB3tb8RKC+DNJpAs1wG70jTafnTeXUnW+vYJU+/80coEopVguQ7atbZ9F0unBfxmE6DadX6pR7fvv2Z0OlXmch34ne6f653lHj9r/fhb6+1bCovJ8MIYE2NCQn6uUKxnlECqUCgUPca+A/t5KpUSLqKoAKZt23BdF4ODgzh85E5+8+ZNXLn8QvfdEdYhlUoFLiXTxPXr1xGLxXD69NmeWT/F2mZqaoqn02n09/djeHgYlmXVBPx834dpGDVBwkZuz+hgUmsp9KJSeHLOUS6Xa1LJWpYFy7IwPX0b8/PzYrCUz+eXsAXWDidPnmYnT54G8C/YtWsX37JlCzZv3ojhoQHh8gWqgihQHTzqui5ELKB2G/b39wuxOyySAhBpWMPnX/rreG7dQSylN25EOpmq+bzsVKb203v0mlxzNZ/PQ2NGZdKMAU0HPHiV1MMuLl+6gsuXL2N6ehqMMVy4tLQatBcuXWS+73PGmKhrmk6nhTOzVVoPWPh1Pl/7WySE0raha6JpmojFYpienhb7FYAQS4FqTeuFLtWFDA0NgXMuBPVbt27BcRwYhrWgDmuz9e5k0KaTYuBK0K3tJnq9/d3uZFhpga+bt08rE4k67QDr9uOrGZ1ev5X+/U6vX6fp9vXv9vZ3O8vd/mt9/6319i2XZutHWZIA1NQepdcUivWMEkgVCoWiB9g2tp0PDAwglUqBcw7HcSpB2CCF5tzcHJ595tiCO6a7XnE3Hx0dXfM1BhfDxOQ4N00T3Atu+i5d6h3hV9EduK6LcjmocUl1Fz3PEylsNE0TbrywkNlqilHDaL2GT1jgAyDqTBqGgVQqhXK5jJs3b+KFF14I2lYRtCzLwtTUDn727PmuP45Onz7NTp8+DQCYmpzgfX0ZjIyMYHBwEKlUSqTjNk1TCMiUJhlY6Oaj/UnioyygkjAmf0/+a1hmzf6g5TUSymvwFzov5UfYYUS/5TgOOOcY6B8SNVjn5+cxPz+P6elpTE9PY35+XqSGXY44Slx64TKzLIsPDg6K7AVy/4+a+dwo/W5jAmFiocNqoUOFBE9KOZ9IJDA/P48LFy7g1q1bYjnUD0zTxMzMDGKxGFzXFc7SRlBN39HRUfy///dV1tfXx4Pv+wv602JcyMudwb8YovbJcgWaerPdW3WkrnSAa7ECzdJTQq8MnXYwLpbw/mzW/k7XmKvncKfnnXZAr/Txs9T+Xe/4Xmx7On1+WC4r7cBa6f7R7P2l1MBtljViMTS7PvVirb3FOsiW8/5yabb9l+swXe71o9tZ7v1Hp8+f7T6/t9uh3mmajY+abT8SQjnncF0XlmUhHo8rF6lCASWQKhQKRddz6PAdPJlMwvd9lEolEYDWNA1PfevJhneBTz7xLfH+trHt/IVLl7v7rhFAJpMRqSM7HaRUrE8uXrzINm/eyClFLTnR5DqkRiiA02pwiN5rlu5UFuzCywcCFzm9n8/n8dJLL2F6ehovv3wdQBDA4Jzj5MnTbMeO8Z47kM6G0gbvmNjGM5kMstksEokERkZGhGhGTkO5JmupVBLCqCyiAhDnH6JRuuN6Imkzd6KGxjVrZccoBYvodd/3cenSJeRyOdy+fRu5XE4I+uSkPH+xvWmVy+UyisVikN7YNME0XtO+Rilbm6VjrKVeit1aNE0TKXWDbANBG69du4ZLly7B87xg4kDJEUGEcrmMEydOLWq7PP10MDEpnU5zOo4KhQIsK1533boB6t/19kmrAS7qk3LKVc67s154OwXk5bLSAstK759mv7/c7duue8N6x+9yA/Ar7eBZ7v5fqgAclVpZsXia9a+VPv80+/1On787vX26nZUW2Jr1j5VO0av2v2It0+oEl6WOH+TsOCSKyhnnFIr1TPeNPhUKhUIBADh4xyFOKSIpyEsP13Xx/LPPLeqOqZV6at1APB4XLikVhFF0iq9//RtsfHw77+/vF2IlTVygFLuNoAF8YwGicRAoLMDJy6Djw3E8AD5yuYJITW1ZFgwE7jcAOH9+eQ7CbuD8hYVO8/GxLbyvbwDpdBKxWALxuIV4PIlYzEQm0wdNAwzDgq4z6LqJIGajAeBgTAdjHIAGxoL/A774C6aDwwP3mfjLNA4GPRAPmQEwH+Ba5F8NleVAA+CDcwbOPQRjXg+O48H3y3AcD+VyEfl8EbncHAqFEmzbRrFQFusZ9AMHx098e8X28+UrL7DLV17A5MQOblkWYnFTpK2l44JoJI62FhDwK9uhun3qQeIsTWZwXR+AD9fjiGUT4NyGZVk1QvNSeOKJJ9kddxzkH/rQh/C1r30N+XxRiOuySCwL241ScDcLAC9XoJHF+vADWHh+WpT7GY1do5S6uhHLrcHY7P1mM+mj0m6HX2/l+0tt33JTlK60gNqqQyiqH7Tj95sFwJcbgJffjzo3Nft+s/Y1O36bTZBa7vlhuf2v0eQdoNo/6u3/Zqz08dNs+cs9vy7XYbzc/rPS+38xE2SivrPSAl2nhYDl7v/w+Da8vssVKNtx/7Acmm2f5TqAm32/0/2jGYu5PkWx0g705Z4/l7t+Ue1YzD7ttMO42e/XE0hbjXlRTELTNFEmxnVdnDhxYhmtVih6g54PeCkUCkWvsXPnJB8YGIDncZimiXw+j2QyiZmZGZw+fXpdn9cPHtzPY7FYxfETCD1PPPHEut4mirXFjh07OIkiuq6L2p804CPnMw1cXNcV9TGrg6LmAkI2m234fj6fr9RcjCEet+B5HKVSCYZhIJvN4rHHHlPHTQSTk5PcNHWkUhlw7oExHYahIRZLwDA0MKaD///Ze+/oOLLzzPu53V2du5GZiUgEAiDAzBkOOZxRzpbHslYaSZasYOtIXtnetX28Pmufz961fSTZK4ddWbYsS2uNLI9keWWNsmWPRxM45JAESTCAAInESRwGpM5dVff7A/MWbzcaoRrd6Abw/s7BAdgEbt2qunXvrfd5gzTg9frhcjmgaR44nQJCOC1B1eVyQEoBVdgEHJDSsD5X/03fTVO3/t80AdPUYZqArqeg6yZ0PQXDkJDSQDQah2nqMAwJw0hDSgGHA68KuQ54PB7EYjEkEgkkk0mMjpZGBG9oaJBU75WiB2n8L4Tb7V7gf004HC7r/E1zNhpXFSTnr7UzK6gahkQsFkM4HMazzz67otemvr5e0vnnEifViMv5WKqBJV+WK8AtRqn/vtgUO8VpuacgLbYAvBjFTtFbbANrsVO0LldAW4zljt/lslwBvdwjhIt9fYudAne5z2e5z3+Lsdznr9yf/+VSrAj3QlHs9WUtpoheTRQ7RXWxU5jT76iOarquY2xsrLwnRoZZATiClGEYZpXQ3NwoPR6PVf9MSoFkMolYLGZFwKx33G63FT0qpVjUy55hVprh4dVfy3M9c+3atZz3r6WlRXo8HkvoM4ybVjpeAFaUP3n+5/Lgny+CKleEWi4Di/r3oVAIsdhsyqRAIAS32414PI6pqSkMDQ2VzRjkF/K5jI+v/lT3DMMwDMMwDMMwDLMa4BdwhmGYVUBra4sMh8Pwer2IRCIAAK+Xo0ZVKHo0nU6/Kko4kEqlcOHCBb4+DMOUnKampoxoyaWmR8r1+XwexCSSxuNxFuMZhmEYhmEYhmEYhmEWgCNIGYZhypjW1hZZWVlp1QdIp9OIxWLwer04ceIEG78VnE4nTNOEy+V6tb4CR5AyDFM+jIyM8JzNMAzDMAzDMAzDMAxTJrBAyjAMU4Y0NzdKn88HTdOsiCCqQzg0lDvF43qmvb1VptNpeL1eOJ1O3Lx5Ew6Ha950mAzDMAzDMAzDMAzDMAzDMMz6hQVShmGYMqOzs8OqNQoAkUgELpdr0aLs65lwOIx4PG6lrhwZ4bp2DMMwDMMwDMMwDMMwDMMwTG5YIGUYhikTOjraZDAYBDAbLRqLxZBMJnHlyhCLfQvQ1NQgAcDlcsEwDEtYZhiGYRiGYRiGYRiGYRiGYZhcsEDKMAxTYtradshQKAQhBAzDsCJFdV3nqNElUFFRgVQqBafTiUQigVAoVOouMQzDMAzDMAzDMAzDMAzDMGUMC6QMwzAlZM+eXmmaJgzDgNPpREVFBcbGxjhF7BLp6GiTUkq43W7EYjF4PB5MTU2VulsMwzAMwzAMwzAMwzAMwzBMGcMCKcMwTAnYs2+vdArA6XQilUpBCIFUKoXR0VGMjo6zOLoEGhq2S4/HY6XWFUJA13Xoul7qrjEMwzAMwzAMwzAMwzAMwzBlDAukDMMwK0hL6w5ZUVEBKSXS6TQMw4DL5YKUEg6Hg8VRG/h8PgghoGkaotEoDMPAzMwMxsau8zVkGIZhGIZhGIZhGIZhGIZh5oWNyAzDMCvA9oZ6WVtbC6fTaX0mjdlIx2g0ioGBQZ6PbbCjrVXWVFUinU4jnU6jv/8iXz+GYRiGYRiGYRiGYRiGYRhmSXAEKcMwTJHZ0dYqq6urYZom0uk0TNNEMpmE2+WEw+FAKpUqdRdXHRUVFdB1HVJKSClL3R2GYRiGYRiGYRiGYRiGYRhmFcECKcMwTBHp7O6SVVVVmJ6eRiqVQm1tLaLRKAYHrnDEY54072iRmqZhZioKj8cDv99f6i4xDMMwDMMwDMMwDMMwDMMwqwgWSBmGYYrE0WP3y0gkgng8DrfbDdM0MTk5iZmZmVJ3bdWyvaFehkIhpNNpBAIBOBwO6Lpe6m4xDMMwDMMwDMMwDMMwDMMwqwgWSBmGYQrMzq5OWVtbi+npaQgh4Ha7MTU1hYFLlzlqdJkEAgH4fD5Eo1HANOByueBwOErdLYZhGIZhGIZhGIZhGIZhGGYVwQIpwzBMgWhsbJRbtmzB5OQk0skUHBCQpoSeSqOqorLU3Vv1dHS2y0DAh0QiBpfLAa87ACEEbt68WequMQzDMAzDMAzDMAzDMAzDMKsIFkgZhmEKQEdHh6ypqYFpmnA6nUilUujr6+OI0QIihIDD4YDL5UI6ncZLL70EIQRGRkb4OjMMwzAMwzAMwzAMwzAMwzBLhgVShmGYZdDY2Cjr6urgdDohpcSdO3fg9/uRTqdL3bU1xe69vdIwDKTTacRiMQwODLEoyjAMwzAMwzAMwzAMwzAMw+QFC6QMwzB50NjcIGur6+ByuSClRCKRQCKRQDqdRiqVwsWLF1nAKxDbG7ZJj8cD0zRhmiY8Hk+pu8QwDMMwDMMwDMMwDMMwDMOsYlggZRiGsUln905ZVVWFqYlp+Hw+JBIJpFIpXLlyhUXRIrBp0yZEIhFomgbDMOB2u0vdJYZhGIZhGIZhGIZhGIZhGGYVwwIpwzDMEtnZ1SHD4TCklIhGowgGg0gkEkgmk3A4HKXu3pqko7NdapoG0zShaRocDgdSqVSpu8UwDMMwDMMwDMMwDMMwDMOsYlggZRiGWYQdbS0yFArB6XQilUrBNE1LJO3v7+eo0SLR2NwgXS4XkskkQqEQEokEC6QMwzAMwzAMwzAMwzAMwzDMsmGBlGEYZh6aWhql1+uF3++H0+mEruswDAMAIITA+f7zLI4WkZqaGgghoOs6XC4Xzp9lMZphGIZhGIZhGIZhGIZhGIZZPiyQMgzD5KCnp1u6XC44nU5IKZGMJ6BpGot0K8Te/XskAEgpAcwK0gzDMAzDMAzDMAzDMAzDMAxTCFggZRiGUejoaJPhcBi6rgOYFejcbje8Xi+CwWCJe7c+aGxukPSzaZpWFCnDMAzDMAzDMAzDMAzDMAzDFAIWSBmGYQDs3Nku/X4/4vE4EonZaFFN0/Dssyc5dHGFqampsVIZA4DH44FpmiXsEcMwDMMwDMMwDMMwDMMwDLOWYIGUYZh1TWdnh/T5fIjH40ilUti4cSPi8TiSySS2bNlS6u6tOzo626XL5UIqlYKUEqFQCJOTkwiFQqXuGsMwDMMwDMMwDMMwDMMwDLNG4MgohmHWJTvaWmVNVWVGtKjD4UAymcSpU2d4biwBzTuaZEVFhRUtWldXh+npaYTDYfzkx//G94RhGIZhGIZhGIZhGIZhGIYpCBxByjDMuqKto116vV64XC5EIhEEAgHouo6JiQkEAgFs2LCh1F1ct1RWVsLhcCCdTsPr9bIoyjAMwzAMwzAMwzAMwzAMwxQFFkgZhlkXNO9okdXV1fB4PEin00gkEggGg0gmkzh79jwLcSVmZ1eHdDqdMAwDTqcTQvAtYRiGYRiGYRiGYRiGYRiGYYoDC6QMw6xpGpubZHV1NTRNQyqVwuTkJADA5XIhkUhwbcsyoKW1WYbDYSSTSRiGAY/HY6XZZRiGYRiGYRiGYRiGYRiGYZhCwwIpwzBrlu7ubhkOhzExMQF3RQUcEHC7NPT19XF4YpnQ0NAgK8NVSCd1COlAKpFAwBfExMREqbvGMAzDMAzDMAzDMAzDMAzDrFFYJGAYZs3R1dUlKysrIaVEOp2GEAKJRAIulwtnzpzhea9MaG5ulqFQCJqmwTAMGIYBIQSklDh/ntMeMwzDMAzDMAzDMAzDMAzDMMWBI0gZhlkzdHd3S7/fD6fTiTt37sA0Tfh8Pui6jgsXLrDgVkZsb9gmq6qqYBgGpJQsijIMwzAMwzAMwzAMwzAMwzArBgukDMOseurr6+XGjRsBALquIx6P4/Llyyy2lTF1dXXweX2YmpoCMFsTlmEYhmEYhmEYhmEYhmEYhmFWArZIMwyzqtm9e7f0+XwwTRPxeBxer7fUXWIWobN7p/R4PJiamoLT6UQgEEAkEil1txiGYRiGYRiGYRiGYRiGYZh1AgukDMOsSnZ2dciK0Gyd0VQqBSklDMPA9PQ04vF4qbvHzENn907p8/mQTCbh9XoRj8eRTCb5njEMwzAMwzAMwzAMwzAMwzArBgukDMOsKrbVb5XV1dUIhUKITEdhGAYcDgcikQiGh4c5rW4Zs7OrQ/r9fkgpoes6TEiuDcswDMMwDMMwDMMwDMMwDMOsOCyQMgyzatjV2y0DgQAMw5hNzypc8Hq9SCQSLI6WOe0722QwGLQifZ1OJ+LRRKm7xTAMwzAMwzAMwzAMwzAMw6xDWCBlGKbs6ezskG63G16vF9GZCNxuN9wuDWfOnGVRdBXQ0FQvw+EwhBCIxWIAAK/Xi2QyWeKeMQzDMAzDMAzDMAzDMAzDMOsRFkgZhilbduxolpqmwev1wuVyIRKJwOFwwDRNGIZR6u4xS6BrV6cMhUJIJBIwDAMVFRUwDAO3bt3CyMgIC9wMwzAMwzAMwzAMwzAMwzDMisPGaYZhypLu7k7pcrmgaRqcTifS6TROn+7jOWsV0byjyRK4g8EgotEozp45x/eQYRiGYRiGYRiGYRiGYRiGKSkcQcowTFnR1bVT+v1+6LoOTdMQjUbhcrlgmmapu8bYpK6uDqZpQgiB6elpOJ3OUneJYRiGYRiGYRiGYRiGYRiGYVggZRimfOjt3SU9Hg90XYdhGHA6nbh48TJHHK5CenbvkjMzM/B4PPB6vUin06iqqip1txiGYRiGYRiGYRiGYRiGYRiGBVKGYUrPzp3t0uPxAADS6TScTid8Ph9isViJe8bkw86uDul2u+H1ehGPxzExMQGfzwe/31/qrjEMwzAMwzAMwzAMwzAMwzAM1yBlGKZ07NrVJV0uF5xOJ6SUcDgcEEIgmUwiHo9jcPAqz1GrjJ1dHTIQCMA0TRiGASEE1x1lGIZhGIZhGIZhGIZhGIZhygqOIGUYZsVpaGqU4XAYoYAfMzMzME0TgUAAk5OTuHRpgMW0VUpTS6P0+XxwOBwwDAMulwuhUKjU3WIYhmEYhmEYhmEYhmEYhmGYDByl7gDDMOuL3bt75LYtmyENHUI4kUrpcDo1GIaEw8E+G6uVpqYmWRmuguZ0w9QlhHTAKVwI+IKl7hrDMAzDMAzDMAzDMAzDMAzDZMCRWgzDrAj19dvkhg0bkEql4PV6AQCJRArnz5/neWiV09TUJOvq6uBwOKy6sS6XC9PT07h6ldMkMwzDMAzDMAzDMAzDMAzDMOUFR5AyDFN0DhzYJ7ds2QIpJQKBACKRCNLpNOLxeKm7xiyTpqYmGQgEYBgGkskkDMOApmmoqalhcZRhGIZhGIZhGIZhGIZhGIYpSzifJcMwRaG+fpusra2F0+lEPB6Hz+dDOp2GaZpcZ3QN4ff74fV6YZomHA4HTNPE6dOn+f4yDMMwDMMwDMMwDMMwDMMwZQsLpAzDFJzW1hYZDoct0YwENADo6zvH4tka4eDBg9LhmE1EMD09DZ/Ph6qqqhL3imEYhmEYhmEYhmEYhmEYhmEWhgVShmEKRlvbDllRUQEhBHRdt2pS9vdfZFF0jbF7b6+MxWJwOp1wOBxwOBxwOp3w+Xyl7hrDrFpaWlqk3++HaZpIpVIYGhriuZNhGIZZd7S1tcmqqipMT0/j8uXLvBYWkIaGBul0OjE8PLzmr+vOnTtlZWUl4vE4zp49u+bPl2EYhmEYhrEPbxIZhlk29fXbZCgUgsfjAQCYpgnTNOFyuRAIBPDkk0/zXLOG2NnVISsqKpCMp6DrOvr7+/n+MkyBaGhokOFwGEIInD9/np+tEtHU1CQ1TcPg4CDfgzVGU1OTHBkZ4fu6CmlsbJQ+nw+mqePKlfXrQNLS0iKvXbu25s9/x44dUtM0FkiLQGdnpxRCIJFIYD2MpZ6eHqnrOjRNQ2VlJZ544ok1f87M+qC5uVmGw2E4nU7ouo5z59Zvtq7GxkY5Ojq6bs9/PdDW1ib53YxhmGLAEwvDMHnT1NQgvV6vVYdS13XcuXMHQ0Nr/0V7vbKrt1t6vV4kk0kI6YDL5eKaowxTBO677z5569Yt+P1+aJqGaDSKixc5Gn8laW1tlRUVFUgkEkgkEjAMA6tFWGtvb5eGYaCqqgqxWAyGYQAABga4BjjR0tIiTdNcNfeUmXUgqayshMs1m6EkFArh5MlT6/r+7dixQzocjjXrzNHS0iKDwSCcTicAIB6Ps2BaIHbt2iXXizNWY2OjDAQCqKmpsd5XeT0sHxoaGqTf74fT6Zx9xxRizc5pxWDnzp3S5/NBSom+vr51fd1aW1tlOp0GC6VrkwMHDshUKgWXywW3242JiQmeyxXYSYBh8ocfHIZh8mL33l7pdmlIJpPQtNnvPp8PMzMzuHz5Cs8ta5Ce3bskAPj9fszMzMCjzdaW5ZRVDFMcDh8+LKemphAIBDA5OcnGohKwZ88eKaWElBLJZBKJRGLVGF06Ojqkz+dDJBKB2+2Gy+Va15EF89HV1SVN04TP50MsFmNDS5lTX18vN2/eiFQqBYfDgXQ6jfPnL6z7e9bV1SXXcmr2w4cPy3Q6jUQiwZlLCshrXvMa+fLLL+PSpUvr4poePXpU6rqOZDKJM2fOrItzXi20t7dLr9cLl8uFdDoNKSX6+/vFG9/4RjkzM4NoNMp7mAXYtWuXJFtMTU0NXnjhhXXvALZz506ZSCTW/XVYaxw9elRGo1EIIeBwOPDcc8/x/c2ivr5eut1uXL16la8NwywRR6k7wDDM6qJ9Z5s8eM8BqWkaTNOE1+tFKpVCf/9FcfLkKcHi6Nqkd0+PrKyshNvtxtTUFHw+H1KpFKSUpe4aw6xZ7ty5g1AoBF3XS92VdUtfX5+QUlq1lisqKkrdpSUzMDAgTNOE2+2G2+22IrCYTC5evCguX74shBBcR3sVMD4+LpLJJBwOBwzD4PnxVS5evCiGhoZEc3PzmtyYTU9PAwDPYwXmxRdfhNPpRHd3t9y5c+eaHDsq09PT0HUdQvDrarlx5coV4XA4EI/H4fF4EA6H0dTUJKempjAxMYFkMon1MEbzpb+/X+i6Dp/PB13XEQ6HS92lknP58mUxMjIiduzYIdfq2rgeuXnzJrxeLwAgnU6XuDflyfj4uCBxtK2tTba0tPD4Z5hFYIGUYZgl0dbRKnf1dsuamhoAQDQaRTQaRTqdZuPUGmf33l4ZDAZx8+ZNpNNpeL1enDp5Wly4cEGwJy/DFI+BgQFBUVKBQKDU3Vm3xONxGIZhpaldTSSTSZimCV3X2YiwCKdPnxZ9fX1i586dsrOzUzY2NrIxoUyZmJiA2+2Gx+NhwSyL4eFh0dDQIBsaGtbU+L1w4YKQUvL9LjADAwNCCAHTNJFIJErdnaJz7tw5QVFHTPlhmibICTuZTKK2thaJRAIOhwNCCGiaVuouljWU6SSVSsE0zVJ3p2y4evWqcLlc6Onp4b3dGmBgYEBEIhGQEyizMIODg8LhcKC7u1s2NjbK7u5ufgYYJge8M2QYZlF6du+StbW1cLvdVi22Sxcui8uXr4gzZ86KS5c4Hd1a5b6jhyUwa4z0+/0IBoOoq6srdbcYZt3gcDisGs9MaRgcHBSGYUDTtFVnQB4YGBDk0LTa+l4qLl++LEzTRDgc5miVMmVs7LqIRCJWHdKenh65b98+vlevMjY2JsbGxsRaixhgJ4/ikE6noWnauomgpxqXTPkRj8fhcrmQSCQwMzMDAFbGKq/Xi2g0WuIeljfRaBShUAjxeJyFoywGBwfFzMwMNm7ciB07dqyptXE94nQ6UVFRsSqdV0vB0NCQuHDhggiFQjw3MMw8sEDKMMy87OrtlgfvOSArKysRi8VgGAbi8TiCwWCpu8asAIfuPSjV6LVYLIaZmRmOZGOYFeTUqVNicnISPp8P999/v2xra+OX+hJgGAbC4TCklOjs7JRtbW3yHe94x6q4F+Pj42J4eFiwyL50BgYGxPnz54Xb7cZaE5nWCg6HA5qmIR6PIxaLQQiB7u5uefDgQb5fr3Lt2rU1pQLF43GYponOzk7Z1NTE97mAuFwuJJPJUndjRVitGSHWA4ODgyKZTEJKCY/HY6VRT6VS8Hg8a25OKzSjo6PilVdesepzHzp0SDY3N8tDhw5Z8+V63tOMjIyIEydOiFAohGPHjsk9e/as22ux2unr6xMzMzPs/GmT/v5+IYQA2xMYZi68wWAYZg5NLY2SPLKcTidmZmYQDofRd/oszxnrhH0H9kopJdLpNNLpNAYucW1ZhikVhw8flpFIBIFAAOl0GtFoFJcvX+ZncoU5fPiwvHnzJjZs2IB0Oo2ZmRns2rUL3/jGN/herGE6OjqkaZoYHBzk+1xGNDc3ynA4DE3T4HRq8Hg8mJmZga7rOH/+vHjLW94iR0ZGEAgEcOrUqXV97xoaGqTH40EymcTY2NiqvhZ79uyRTqfTijBb7edTDtTX18uamhrEYjFcubL29/stLS2yoqICZ86cWfPnuhrp6emRwKxor4rZoVAIx48f53u2CHv27JHJZBKapiGdTsPlciEUCmFqagrpdBoVFRV4+eWXMT4+vq6v5Z49e6TX68Xt27eh6zqGh4fX9fVYjfT29sqKigrEYjEEAgE88cQTfA+XyN69e2UqlYKu6xgY4GyADAMArlJ3gGGY8mJHW4sMBAJW6gXTNFFTU8OpGNYR3T1dUtM066WU6s4yDFMapJRWinMhBPx+f6m7tC6ZmZmB2+22ajvV1NTgueeew8/93M9JKSU0TcPY2BheeuklNtqvIdhwUJ6YpolAIPBq6YcoJicn4fF44PF4AADf//73xS/8wi/Iv//7v1/394/mo+bm5lUfMUDiKAAEAgHs3btXstC1PMbHx8XGjRtlQ0MDGhsb5Y9+9KM1fT2vXbsm9u/fL3t6eqTD4YDL5Vr3ThTlhMvlsmqQulwuuFwuOBwOrqm5RCjaNpFIoKqqClNTU3C5XHC73XC73RBCrHtxFJiNQOzq6pJbtmxBJBKBy+WSiUSCr80qQtd13Lp1C06nk+uT2+TMmTOiublZbt26FZWVlXJmZgYXL17ksc+sa1ggZRjGYldvt/T7/dB1HbFYzKr10dnZiW998595wVzjbG/YJjdu3GhFDZumiaqqKkxPT5e6awxT9tx///3yU5/6FDweDyKRCJxOJxwOBxKJBDRNg67rcLvdmJycxDe/+U38+Mc/XvKcOjMzA6/XC6fTCU3TuAbTAhw6dEi2trait7cXfr8fgUAALpcLUkqQiOl2u/E7v/M7tiNlEokEamtrEY/HAQCRSARbtmzB6OgoXC4X4vE4NE2D1+styrmtFerr6+ViBqjm5mbp9XoRCoVQVVWFyspKhEIh+P1+y3gaj8dx+/ZtvPjii3jhhRcwOjrK+5Qs2tvbZU1NDWpqalBVVQVygNM0DQ6HA0IImKaJRCKBSCSCmZkZJJNJ3LhxA+l0GslkEvF4HCMjIwte2x07dkifzwev1wtN01BdXW09C/TldrvhcrmsY3q9XkgpkUwmEQgEMDIygkceeWTJ93B0dFzU1NRIwzAQCoWQSCSs55DIVxxtbW2V1P9gMIhQKIRAIACfz2edHwkrNNcLISDlrP6YTqchpYRpmlaKSF3XkU6nYZomYrEYkskkZmZmMDExgdu3b2NoaKjo43d4eFj09vbKycnJojpx9Pb2ys7OTrS1taGiogKhUAi1tbXw+XzQdR0u16wJwuVy4ROf+ISt6Gyn0wmfzwcpJXRdtwTx9cChQ4fkr/7qr1r/FkLk/EqlUvjc5z6H5557bsnX1TRN3Lx5E9HoDF772tfKf/u3fyvpfLpv3z55zz33oLe3F1u3bkUwGITb7YbT6YTH48H09DQCgQCSySRSqRQ+8YlP2DLuSinh9XpB6VxLQWtrq9yyZQu2bNmCuro6VFZWwuPxWHOjlNKaL+PxOCYnJxGJRF69T1HcuXMHt2/fXnNrH0U9GoYBn8+HVCplfV5IGhtnsxDQHEXzu7qHMwzDWguTySR0Xcfk5KSt9XGl8Xg8cLlc1jjy+XygUh2pVApCCDQ0NEg7a8AnP/lJefDgQUSj0UX3uA7H4lXcHA6H9Xu0TqbTaRiGgTt37mB6ehovvfQSrl+/jueff75o6+PFixfFnj17pGma1lo/Pj5ejEMxCvv27ZO1tbXYuHEjgsEgtm/fDrfbjR/84Af4yU9+suR7TU6rDocD09PTaG1tlcXeS/X29kq/34+qqirU1dWhuroa4XAYPp/PckTIXpNprKs/07pDGdtSqRTS6TRu376NiYkJPP/88xgfHy+qaDk8PCxM05Q1NTXQNK1Yh2GYVQMLpAzDoLm5WVZVVcHpdCKd1GGaJjyaF6dOnhYAcPniQKm7yBSZ5uZmWV9fj9u3bwMANKcbutTxzFOcyohhlsKmTZvwcz/3cwBmX/YdDodl3Mr+rK+vDz/+8Y+X3Da1oes6hBCWcbkYNDc3y9bWVuzcuRNtbW0IBoOWATGRSGBgYACf//zny3Je+OAHPyh7enrQ2dmJ7u5ubNiwAU6n07r2wKxxnYww7373u221PzQ0JDZv3izj8Thcrtn2ksk4HA4B09ShaU7oegqalr8Xc3Nzs9y2bRs2b94Mn8+HiooKKypudHQUt27dwg9+8INlXf/67Vvl+PUXinIP77vvPvmGN7wBW7duRU1NDTZt2oTKykpLpAZABl9JBmAV1ZhAAhQJeiSwkRjldDotUepVI6U8ceIEzp07hx/96EcrIjiVC+3t7fKhhx5CZ2cnQqEQ6urq4Pf7LaMveddrmmZdQ8I0TctAST9TrUflS1L0jpRyjrGH7gl97vV6M/6P/p/mMkLXdTgcDrjdbly5cgWPPPKIrfMWQkDTNCSTcTidDsTjUVRUVOR9HZubm+VPfvITmKZp9d/pdM4RQn0+36L9AmCJHKohDJg9b1VAfVWklmQY+8lPfoLnnnsOjz/+eMHH8Llz50Rb246iKkI+nw8f+9jH0NPTA7fbDZ/PZz2zhmFkjJWtW7dicHBwyW3HYjFomvNVoUzD5OSdYp1G2XHo0CH8zM/8jDUXEuoaB8yOr4cfftjW2PF4NMRiMQRDfoyNjeDYsaMyHA7jsce+t+Lz6LFjx2Rvby8efPBB7N27Fxs2bLDOV51LaI+VTqdRX1+PixcvLvkYs+u4yxLiVpquri75jW98Ay6XCx6PB+Fw2BK0aG1T5xHK6kPzMwlKr35J0zSRa03Nh6W0kT2Xq39L9S/T6TT8fj/e+c532qodSob6QCAAANYaYbdGbn19vfyd3/kdhMNhbN68GcFgMEPAoLmd1kV1jqdzoS/1+qZSqezPc+5n5kN9VtW/UdcJatvtduOjH/0ofvrTn9oQ/w04nRoAE7quw+l0wOXywDR1OJ2z48qug8wv//IvY9euXdaarTLfWJi/f4tfJ4oYJnH89u3b8plnnsGTTz6J73//+wWtRdvX1yf27dsjfT4vO4UXiX379smamhps3LgRvb292LFjB3bs2IEtW7ZYe1K3243Lly/bajeVSiEYDCIWiyEWi2Hr1q0YGhoqyjl88YtflHV1ddi4cSPC4TAqKystUZTmD3Vvky9qG+l0GlNTU/Lq1at4/PHH8cwzz+C73/1uQdfk0dFREQwGpc/nQ3d3t5RSciQps25hgZRh1jn19fVy06ZNiMfjEEJA13WEQiHEYrFSd41ZIbq6uqTT6cSdO3dgGAaCwSBOnjzJGyOGyRO7xoJy4HWve518wxvegF27dqG2thahUAiVlZVWyiIyIJ4/fx6f//znS9nVeTly5Ai6u7tRXV1tRQKQsY6iMEjkeeCBB9DY2CjtRl5EIpGMaDG616qYki/t7e2ys7MTe/fuxd69exEIBKyIErfbjZmZGVy9ehU/+MEP8mq/qbFeArBS5jc21svR0cKmEmttbcWb3/xmbNu2zRJGKGpQNYqRswBw11imXksSqFTUf6viKEWRdXd3Y+fOnUgmk/jc5z6H06dPy//3//4f/uVf/mXNp8ltamrCsWPHsH//fsvIrmmaFSlKxlYau/Q8q9dUNQJXV1fPMdaqv0siF7WhGvOz71/2Meg77TlVEdIuPp/PSsOYSCSwbdu2ZXnBezweVFZWIhgMZpybajQHsGiqR9XATt/V8a+OX7pWQghs3boVyWQSx44dg8PhwI0bN+S//uu/4hvf+MayHSNUBgevFvV5mJ6eznBuUK9d9vzZ0tKCxx9/fMltp9NpaNrs9UskEta9Wg80Nzdb65phGNZ6o0akSCkxOjqa3wGECSGcqKquwK3br+DWrVsF6rk93vSmN6G+vh4NDQ0IhUIZawidNz0zhmHA4XBg165dttbGaDSKqqqqgomKdvH7/airq7Oyg1B0rDo3Zz83TqcTUkoEg8GM+Vr9fZpXl8NS9rALCaR0HhStWFtbi2vXrtk+fnYElt29dVtbG97whjegoqLCctoxDMOKgJyvvVwChyqWzucAk93/pTLf39NYqK6uttVeMcgeZ8WE9ioOh8Oa77Zu3Yp3vetdeOc734k//dM/xVNPPSW/+c1v4q/+6q8K0pnTp/vE7t090u/3o729Veq6jmvXyisyeDWzZ88eHDp0CN3d3QgEAqitrUVFRQX8fn/G2LK7D9R1HalUCk6nEzU1Naivr8czzzxTjFNAR0cH2tvbrSj/+cqPLTcVOO2v6brU1taitrYW99xzDwBgampKfu9738M//uM/4rHHHivIGL1w4YLo6uqSFRUVnKWKWdewQMow65jDhw9LqqVG6b98Ph+i0SjOnj3Lm8J1QFdXl6yqqsLMzAw8Ho/lmcwwjD1W2sB26NABmUqlXhXNhpc9X7e1teGNb3wj2tvbAcAyxBmGAY/Hg3Q6Dbfbjebm5mX3vVjs3LkTLS0tcDqdljGMjFkOh8MyNjkcDtTV1eEd73gH/uIv/sLWMW7duoWNGzcCuBtNR8dRj5UPPp8PmzdvRnt7O/bt22cJDB6Px4qUyyc6rrm5UTqEsIyCTqcTXV075XJf4nNRU1OD9vZ2hMNhAHPFz0KRSqWsKBDVSEGe3ACwb98+HDx4EL/927+Nf/mXf5F//dd/jePH12ZWhHA4jPr6etTU1GQIJyqqwWU+Y7P6dwvdu8WMWAs9A6qBWe1HPmNkenoabrcbGzduxGOPfU+cO9dvuw0Vl8uFUCiU8/plC/xLYb5zUkVRNdsA1YhLJBJwOBzYunUrPvShD+EDH/gABgYG5N///d/jM5/5TNmP4UuXLgkppXS73RmCaLZTCTD7nP7t3/7tkttOJpMIhWYjy0gkWi90dnZmZKVQow3Va3HhwoW82qcxaQl1JRAODx06JA8fPoxwOIy6ujp4PJ4MJ6dsqHzB/fffj8985jNLPs7Y2Jiora2VpYgeBWadMSoqKuYY2dW5J1sgzWY+h6xyqMVHGQsMw7C9P1ZFODVi2O4a4fV65zjNZDv75CKXg4+6fq6UEyT1uxycLmm+yXUflisQ54L2MdnPA12TY8eO4ciRI/i93/s9+YUvfAG///u/v+yD3rlzB1u2bIGmaaisrISUUg4Pr6301aVi48aN6OjowO7duwHMOmqqkdL0fNmNEifnOsrKEYlEitD7Waqrq1FXVzdnDlHXJTXDSr6oz716TWgurKiowMMPP4x3v/vdGB4elp/97Gfxt3/7t8sepxcvXhQHDhyQXq8X+/btk3Qt7ZajYZjVzPKeXoZhViXtO9vkPYcPyVgsBiFma9Vomob+/n5x8uRJweLo+mDXrl0yEAiAxkEkEkEkEuH0MgyzCiDhsra2Fj093cu2Ym7cuBGVlZWWwV5NO6Z69dbW1i6778VCTadLRpVsQxcZf03TxMMPP2z7GOPj40KN2gAyI+/oRbm+vt72PVHbJO9k0zSt/uq6bqWcswOlWfV4PFZEoc/nm9f7eTlQvV217mL29VJTt1LaQDV9IKVVoxR99LnaBvWdavdQbUeKWqGoQiEEQqEQPvjBD+KJJ57A1772NXnfffeVpuBcEaF0XwCs60/3QHUMmC/VLZErJWyue6X+nvo72V/Zv6f+PoCM+5qP0JVKpaDresHSgJLglKv/2b+3nC8VNVqIvrxeL1wul1X3TkqJrq4u/PEf/zFu374t/+AP/qDsx3A0Gp1jKMwllHR1ddlqd2RkRKRSKaRSKfh8PhiGgd27e2Rb2w555Mjhsr8uy6GxsTEjKwKhPlMOhwOXLl2y3baaQpvSFlZVVRWm4zY4ePCgFV1EzkHA3HqrqrAppcSePXvyOt5KRMTNd1xivkjQpUL3n+b85c5P883nudaCXJ+R83U+4mghoTFNtULpWtHeRN1b5Jrj1e9A7rlavQ5LvX6LXf9s0b7Q4zOf9uha5vpbu+M1+1nO/qK9WzKZnDMvUcpdKWezZGzatAn/3//3/2FkZES+973vXdZgGx9/XkxMTCAYDCISiWDz5s14xzvetqbXlJWC9gJqiubsvamaWcZOu5SlIh6PL6l+bFNTU173lOZX9VmmaH3qv5rdIN8v4O57EtXcpvc3l8tlve+4XC40NTXhi1/8IgYHB+XDDz+87LH63HPPiXQ6bdV2L8Z7IsOUMyyQMsw6Ylv9Vtm1q1P6/X4kk0k4nU7E43EkEomy8E5kVobtDdvk3v17ZCAQQDweh67rCAaDuHTpkhgYGBB265IwDJOZQnEl5lOqz6MafpZDVVUVQqGQlRKRXvpUgYtqvJQrZDhWo9tyGb6A2Xu0b98+HDlyxPYLJa2Z2UahfCLMVCjNntfrhWEYlohJL8hUH8suHo/HqvGjpmQsxjitqqqyIjtJZCeDAXD3GqnGBPVLTatG0aC5aoLRz5SKSj0/irpViUajVt3Zf/u3f8Nf//VfrymjVyAQsCKEacyotTPt3ms1ujHXvco26Ki/lx3xM9/vEsuZN7dt2wYAeM973l2w+6lGriz1nHIZuIj5roGKen3JSEhR+/T8klHf4/Hg4x//OPr7++WePXvKdhzfvHlzTuRoLtFh69atttsOBAKWeKbrOlwuF9VxLVj/yxESLLPFCrqe9GxeuXLFVrs7djRn1BimMVqK6Nyenh5UVlZajj20nmeLiKogbJom6urq0N7ebut5IGN9Kd6B6Tmfb48CzHXEmA+6XzTnL9dAnz2fZ3/lWrPVz9TI8Vzz3WJkO52p3+1A+1mKZiXUmoG5nIYWciYiJ7vs87dz/Ra7/tkRwMuNSCsES7n+hXQ2oJqUwN05iTIsTE5OZmS5icfjaGxsxD/8wz/gn//5n5e1Jl65MiSo3NTExATGxsYKcj7rHSFmo39pb07jXB0zi81zuaBazACsPcHb3/72BRvJN8Ke3sOy571cjmBLdZCbD1rj6Lmj85ytKTx7TF3XrRTUra2t+NKXvoRHHnlk2XtCVYBlgZRZb5R+tWUYZkXY3rBNVlVVwefzWYuu0+nEhQsXxKVLl8Tp06dZFFsH7GhrkRs2bLA8Mz0eT8bmkmGY1QHVdgoGg6+mfF0efr8foVDIMjbQC1+28atU6eiWAjl8JBIJq5/Z4lC2qPa+973P9nEuXrwogEyjZC4DmF1IGFQFRimldT5CiLzmatUrm/qXj7FxKYRCIcsAoravRpOox58vemU+0Yl+h1JOkaGT9jUUPUrOA2RMIAGRrumHPvQhDA8PywMHDpStwGQH8mRPJpM5IzwBZBhZFopmzDbM2Iksoq+FDKW5InXU+sB2ePnllwEAIyMjuO++e+XBg/uXdT+zUwfPd/6LMZ9YqkL3LPs609/QuAZgiYBU17eurg6tra144okn8L73va8sx/Do6GiGIZKumxpVZpqmFflsBxrDqVTKcsqorq6G3+9HT0+3bGhokPlE8Zczhw4dkmr9UfUaAplG38HBQVttU4kNwuFwWJH5K83WrVsRDocRDAYtoY2eDdXJh+YpEsddLhfuvfdeW8eKRCJ5CXiFQhXUssVu9f9zOWPNF/VYiK/FyPW7udYbqg9odxzNJ47aXSNyzddLibDNnpOzo2Szzzf795bS/kJRper1o+jUUrNcwccONF6ynw3a19GaQU43Pp8PADAzM4N3vvOduH79umxpacm7Y6dOnREzMzNWFHt3d6fctatLHjiwb02tKSuJOo6zo63J+Yv2Q3agNN6ztck1aJq2aL1jIUReWX7U9Tf7+QXu7rGXC0Xgqw6iJCjTZ1JKK8sIPR9CCLzvfe/D9evX5aFDh/Ieq/39/SKRSKC6urqs3/kZphiwQMow64CuXZ1y69at8Pl8Gd5Hr7zySqm7xqwgLa3NsqqqCg6HA/F4HH19feLUqVPi4sWL4sSJEyyQM8wyKIaRYCEooohSY2dj9+UvGAxaL15kLEyn05agRMdUvfCXSnt7u2xubi76Bbp69SrGxsZw69atjJothHpuZCR7xzvekdexsl/ksyMH8hFI6+rqUFlZCZfLZRmA6IWfjpXP9QfuCpREsQxbZLTOFjTp5Z6u/2IRFLkMiOrfqakXVaMERdw5HA4r8g5AhpHW6/UilUqhqakJP/3pT/HWt751WReis7NDdnZ2yJ0722VTU0NJDGiaplkRx6rIpxrW6R6QkSfbGJyN+nd2I4wWMjDnMoDlG3VCAorL5UIikUA6ncb999uPCieyhZJc572Uvi5FgFCdK7LbTCQSc64T/Z1hGJiYmIDH40EoFMJXv/pV/OZv/mbZGW5VkU51fKD5gJ7HYDCIo0eP2uq/KpYZhoFkMolUKoXJyUl4PB5s2bIF1dXVeRlBy5XOzs4MEZTGqjrfGoaBqakpPPvss7YeKIoSoXFODjn5rjf50t3dLUkYpXOl8ZL9nFAEGfVZSonXve51to537do1UcioNzvkEhezP5+PxRww7ESLzueMtNxzyzbo2/37XIKhXWjdUZ3Essf6fOtbrvVOrYm50Fq4GItFlVL7akRuIcnnHs8nINP/zfe7+QiqNO+QwK5Gi5MzB6XbdblclmNcKBSCEALbtm3D0NAQ3vzmN+c9/5P4dOvWLctZgzLrMPahfSFF7atf5OhIX3ag9LpEKpVa9Bmk9yq7SCktQTJXBCk9s8uFot4BWI6PNI8BsNZnYNa5iZxBPR4PZmZmsG3bNjz77LP42Mc+lvf4P3v2rKB71dbWJg8dOiQ7OjrWzH6KYebD3gzEMMyqor5xuwyHwwiHwzBNE7dv38a1oWEWwtYhe/fvkR6PB9FoFOl02krFxzDM6iTb0J7N+Pi4rbmeXsKklNb8QB65wKzBPjtN2VLx+/0AZuseT09Po1hpvIeGhhAKhdDS0oLKysqMF0rg7nUiY5SUElu2bMH73vc++bWvfc329QLuerhnY9cA1draKltbW1FfX2/1fWZmBoFAIMNQnG9ET3aUQrGMwiSi0/UlsqNfFiNXyqpsSORWjZb0u6oQQ57W1GYymUQwGMTU1BQqKirw2GOP4SMf+Yj88pe/nNcFIQOPw+GA1+tFUxPkyEhxxvhCCCGscak+p6o4pRp1F2Kh/6frmuvz7P/P/p7dhtoXu4YxYFZci0ajllGMjJr5oj4n2cx3LrlYbLxTBGD2uKW/pdqaajo1wuVyoaqqClLORlB6PB58+tOfhsPhkJ/+9KcLOu7a21slGYYvX75iq+3x8XHLcSFbqKCxSsb/LVu22OoX3e9AIIDJyUmEQiErLRxd+3Q6bXsdLBTNzc1STXdKmQ2W05+WlhbL+YPGTvZYTSQSmJiYsN226ghFqKLSSrFhwwbLyUMVRdTxr2aHUPsKALt377Z9zJU+x1zQuFVTbAPzzzW5alSqa3qxzylXv9TP6HxcLleGMd8O6jqSr0hKcwztAxba++QS77J/N7sf2fdpqeeZ7fyi/h05iK3k/VwK6vXLdZ3strUQhmFY6zmRPR+o/08CMv0O1VD8x3/8R3zwgx+U3/72t20PwHPn+kVPT7fcvHkzbt++Dbfbbb3LMPahtcrhcGTUsqe5m95v7KbJp2xolL43nU4vSSDNJzKSsnhkzwmLZU2xi7rvyxaN6b08nU5bv+PxeCxH0VAohFQqBbfbjb/8y79ERUWF/JM/+ZO8OjYxMYGKigqEw2GkUik4nU7s3btXnjlzhm3JzJqFBVKGWaN0dLTJcDgMwzAweWcClZWVCPoDpe4WUwKOHDksp6en4XI4kU6mEA6H13ydJoYpBdnGDDJ0qJEPFJ2Rb/t3RQUxRwBUaW5ulsPDS3eIMQzD8pAmQwi9dJmmCa/Xi3Q6nZf4QH2urq4uaqqwS5cuoaqqCsFgEDU1NRnXn+oKqtFLJCJ95CMfwde+9jVbx+rvvyiOHDksqf4rGQPp5dyukLxt2za88Y1vREVFBerq6iCltMRRta/5XD+KMKaXbV3X4VhCdFBHR4ccGBiw9SJM13ohgyF99p//83/GjRs3IITA9PR0RnSv+vdqalEyhGzatAnbt29Hd3c3WltbEQqFLGOLGjVKBhM1AoM+q6iosK7tl770Jdy6dUs+9thjtl/8L1y4JA4dOiDJO766uhrhcFieO9e/YkYEj8cDr9drCSc03smI4nQ6MTMzg1AohD/7sz/DSy+9BCDT0GvHCL2YAWoxQxF5u1PdSL/fj5GRkaWcqkVjY72cnJy0xBTDMCwBKV+yazvOh2EYeOWVV/CLv/iLqKmpsdKR0nyTHV2uGrocDgfq6upQW1uL9vZ2dHZ2Ytu2bfD5fBmiTzKZhN/vt8Y1jVWag71eb0Y9rz/6oz/ClStX8jIG56K5uVEahmFFHPf27rI1pp988kkxMzMj/X6/NffSs00GbPr54MGDePTRR5fct1gshmAwiEQiYRkLad4RYrZWHaVCLwV0b+LxOMLhMGZmZhAMBtHT0yNTqRSklLhyxZ7gfPjwYWtNU59xEjeFEPD7/Th9+rTt/qoCHRlnDcMAVjj1bHd3t5VWVNM0xGIx+P1+697Sc+B2uy2DsOoM09DQYHvdikajCAaDeOihh+Q///M/r9iYefnll/GhD30I27dvh8PhsGqbU1T8Qiw2Rwsh0NbWhg996EMZ+wf6OxpDdN2uXbuGP/mTP0FNTc2S6swvNj/S8xgMBvHiiy/i5MmTtq6rKo4ahmHtrezuq2hc07Oitk1zNYkJTz/9NL797W8jEAgs6fouB3V/k+tnyogQDodx+/Zt2ymz3W53hqAIZDol5SO4Zgu21I46lhwOBy5cuIA/+IM/WDBqlLLTkDOAw+GA3+9HdXU1Kioq0NnZiba2NjQ2NiIcDmc4CaniuzpOSChTBTKHw4Gvf/3rOHr0qDx16pTtm+ZwOBCNRi2nP043mj80JsWr7x5qenTaM2WLj0vDBGDCMHQkEhJCSLjdC88TLpcLgYB9m6ia+pnIFTV64cIF/NZv/Ra2bNmSEQFK10B15AJmnX18Ph/C4TD8fj+amprQ1NSE9vZ2bNy4MeMdluZA2pfRGknt0Ls8vcN/9rOfRTqdln/+539ue/wPDAyIPXv2SHqe1Mw9DLNWYYGUYdYghw4dkOSZpWma5VGfj1cxs3rZs6dXkvHJ7XZbdexOnHiOdzcMs8rJjhgoZzweDyKRCG7fvo1gMIg3v/nN8gc/+EHBOx6NRhGJRBCLxRZN5UVCg8fjwcGDB9HV1SXtGtRnZmYsERmYPxXgUqitrUV9fb0l7haSaDQKh2J4M00T+qtRlAcO7JNCCCSTaZw7dy6j0/lEC+eKuMh1LWKxGE6dOmU7FeR8vOMd75BvectbcOzYMTQ2NgKAFQntdrstw1YikYDX680wMqRSKWiahu985zvo7u62PQ6AWRGaotjIcL+SZBtayWBDhhWHw4FgMIiZmRk8+uijBbvupYaiEsi5Y7l1E+cTibPHsMPhwAsvvIAf/ehHBbmODQ0N8tChQ3jnO9+JY8eOYdOmTfD7/ZYoEIlEEAwGkUqlrHFNY1mNnv3617+Onp4eOTQ0tOx+qXW18l1rbty4gZaWlgwxhgx91J7T6cS2bdtstXv16rDYsGGDpDZonJOxfWZmxnZfC8mlS5dEd3e3BJAhzgCYY0xdKjU1NZaAmX1vVKeSq1ev2m6brptpGiWrxwkAPp8vw8FITV1IP6sZcMjxha6D2+1Gd3c3BgYGlnxMimaamprCrl27ZGVlJcLhML73ve8VdY4cHBwUdoUvOzz88MPyAx/4QIZzFYkShK7riMfjOH78OL7whS+UzZpAohswfx3WpTJfBD856bjdbqTTaTz77LP40z/907K5BoUkVwpcu2RnoFAFS+BuVHMymcQ3v/nNgq2LR48exUMPPYR7770XdXV1GalHXS4XpqenEQwG4fF4rKwg1CfKMPHII4+go6PD9vHPnj0v9uzplZqmweFwLCs7BVN8aH4oVmr4pc5BZ86cQaHecQ8fPizf/va3413vehd27NiR4ShCTlH0M+0J4/G4VZc3Ho/jz/7szzA2NpaX89zk5CQ2bNhgZZhaDTYHhlkOXIOUYdYYXV07pWmaiMVi0HUdN2/exJ07d3D6dJ8YH3+eV7V1QGNjvezu7pRkiIpEIkin0zh//oIYHLzKY4BhSsxKpMvK9yWmGC8/ZLgkA10wGCz4MYBZ0S0ajVr1+3KleFXPj/oUCATwC7/wC7aPd+5cvyAvePKmz7f+aDgcxsaNG1FVVbVgqtF8xs7IyJi4Njwq1DqcLpfLSk0aj8cLWheS+pkrHR19+f3+ghriv/Od74iPf/zj4s1vfjM+/OEP47nnnoOu61adXjKqqSmLo9EogEwB1W4kMXHx4mWhRlXk452+HLINyBTZrIoxQsymbl1KhNBqIBQKWdc5mUxamTEKMb/kqpumfhU6mmRsbEx84xvfEA8//LDYunWr+LVf+zWMjo5aaVTJ0Ot2u0ERiCQOUUrSWCwGr9eLL33pSwXp0+jouKB5NN+5YHBwMOOa0XhU0146HA7s3LnTdtvqfKvOwU6nE5OTk3n1t5CkUikAyIjizpUSfCns2LFDbtq0KUNUVsVD4G5kVV9fn62229vbJbWTbzrUQqFpWkadv+zrpP5fLicGTdPwwAMP2Drm4OCgcDqdeOWVV+ByuRCJRPDiiy/a73wZotaso1TzwN3rqmkawuFw0fZk+aLuH1RBrhCpXNX01BStbJqmtR9YS2SvW9k13e2g1klVf1braGfXul8uY2Nj4pFHHhEPPfSQeN3rXoe//Mu/tJz9aQyHw2ErArGiomLWIdDhwPT0tJWFoa2tDd/4xjfyevGiOoyUAYEpL7LnBCEEYrEYdu5sl93dnfLnfu5nc973fNY51VFnISorK223PR/PPPOM+G//7b+J1tZW8bM/+7P4/ve/b+0t6Dmmucvr9ULXdfh8Puv/fD4fpJT48pe/jLa2NtvPwMjIiFCzVpRDum+GKSYskDLMGqG5uVHu379X0qJcVVX1atqcETE0dI1FsXVCa2uL3LhxI0KhkFU83ufzFc2bjmEY+xTiBWOh9FXlRjwet8SZVCplpfcsNCSIkaE8l0BKZNeqe/jhh/M+Lhnv1OPZvScOhwM+n8+KhFP7XCiDNRkDKbsERb96PB6EQiG84Q1vyOj0ctOwLdRWPB4virFpdHRUfP3rXxf333+/+MQnPoGbN28iGAxmRFiRwKTWk5JSIpFIoLe3F5/73OfyeqBmZmbyqqNZCLLHCnmPA3dFB4q4qK2tLUkfCw0ZZMmLPhgMWsax5ZBrbs3+LBaLLStSdTH+8i//UjQ1NYkvfvGLVuQKicG0n1Ofn3g8Dr/fj8nJSRw9ehQf//jHC7Io5GtMJwYGBqz7RNcy1zNCUd92UB0SgEwnATvp5YsFpaqluUcVcO1e06amJlRWVs6ZR3OJEZcuXbLVtjpXlDJCpLm5WXq93ozItOzaa1TTErh77tkZHA4dOmT72IFAwFoTE4lEWQjsy0VNq6uK8oZhWLX+KCqoHFGFURLh8nGqmm+vTD87nU4rRf1aYr61K993hsVEakr3Xaw90MWLF8Wv//qvi+rqavEP//APVsRcMplEMpnMWCdN00Q4HLYEWyklfv7nfx6f/OQnbZ94JBKBaZpWDUamPFHHpd/vt57pl19+uSjHWAgSMAvNt7/9bfHOd75TvOtd78KTTz5prYGqQ6aaNUZNCezz+fB//+//zeu409PTbEdk1g0skDLMGqChYbvctGkTdF1HLBZDIpFAJBLhOpPrjJ6ebkn1RWdmZuB0OjExMYHbt29zemWGKQPmS99YzGPl8/uFNpBSlCJFMBTLAJud+mshHA4HYrGYZXzbvHkz3vrWt9q+MVQ3TK3NROnb7EK1NqneEVDYe0G1cHRdx8TEhJV+fb7oiXyMQUtNQeXz+YouJn7xi18UW7ZsEY899phV04vOmfpJhgxN0+D1epFMJvFrv/ZreNvb3mZ7LFy6NCBo7BVTPMtFrrmFamICsJ47KaUVXbraoShEwzAQj8eRTCahaRpCoVBBj5PLqOz1ejPq2haLj3/84+I973nPnPprajSmWou6srIS8Xgc//2///eCHJ/EzXwZGhqCGmVPbWYTCoWwa9cuW8+cKg6q9fbKpU4cOSRkCxWq4XKptLe3W/WjacyT0AXcHRe3b99Gf7+92sdqmlr1+0qzefNmK1UmgIxoVnX8qEIfoQrQzc3Nto89MzNjCayVlZWor69f7umUHFWYp+eDvtN6R+t/uTwzANDW1iZVQVSNEi9U1gkS30lwz3fPVo60tbXJ7DlH/Tnf51u99up+V3VQyf69YvG+971P/Oqv/mpGrcVEImHt62lf5/P5Mj7/3d/9XdvHGhkZEzT+VmLNZ+yTvae4desWQqGQVVpK5eDB/TLX36wmHnvsMXH//feLv//7v0ckErE+J0cBNVuFlNKq+b5792784R/+oe0JgOqlF3IOZphyhUc4w6xyent3yaqqKgCwNvr9/RfFuXP94tKlgdW7+jNLpqmpQe7bt0cGg0Fomga3223V/BsZGRMjI2Pi2rURHgsMs4LMZ4QoVMTnYnWZyikqwO12W1EzqlG30FCURK7rkut6kEBHnue/8iu/YvuYk5OTGYZcSttm19hGkVoLRfAsd+yMX39BJBIJpFIpK+2Sz+ezhLSbN29m/L6maWhsbLR9wFz9z/5sJQ2y73jHO8Tf/d3fQdM0q/YsCdnAXaNxIpGwjPP5CkzJZDLvWmmFhGqhqoZMejbWgiG4ublRejwey6lAFQ2XM7YWi7Khzw3DWLF6ZI8++qh473vfaxm/6N7SuQoh4PF4kEqlkEwm4fP5sHXrVvzP//k/l7UINDRsl2odQGD2uttp49q1a5YomC1sZc91dtPsJpNJSxhUo+7LJVXm+Pi4cLlcGelw6TrYHTu7du3KmLPUa0cCucPhwLVr12z3U03Dmk90a6HYsmWLJXgAmSIMMLteX79+3fp3rvqkQghUVFTg2LFjtsbp1NSUNXaoLudqRxWaKXsEjb9UKpUhmq60Q89C+P3+jLSt2ePALvPtlcl5gRyn1orhn/Z2ua7XQu8MSyU7ClcdR+ocVWz+4i/+Qvzar/0a0uk0bty4YdWW93g8cDqdmJmZsdLTRyIRuFwuVFZW4tOf/rTtgWQYBnw+H0eQljk0risrK5FMJuF2uxGJRPC6171GHjy4X+7Z0yuzU9MXg5XKJPPRj35U0LtKIpHIEPDJMYb2jaZpQtM0/Oqv/ir27t2bVyQ1vUPs3LlTAkBTU1P5GBoYpkCsjZ0Aw6xT2tp2SEq5RC875WQUZ4pPe3ur3LRpE5xOJ6ampjA5OWnVlVO9yhiGKQ9Wao4up7WAPPUp3eDt27eLchy32w2v1zsnYgmYGx2j67pljKXUfXZrlwHA8PCoUAUnMkaOjo7afvtWDZnFYmz8eZFOpxGPxxGPxxGLxSynmuwxk4+3cC5jpPqdvtSUtyvBRz7yEfHlL38Z8XjcMhY4nU5LXNN1HV6v18q2cOjQIXzkIx+x/RDR35dahCRnKYqq0nU9I4JttVNbW4t0Oo1YLAZN0yyj+o0bN/DssyeXPbDmMyKrQuxKphz71re+JciBgyLMqbYlpRWm5ziZTCIajebl8EHU12+THo8HmqZlCBV201Beu3YNkUgkQ9BThT3CNE309vbaaptqTatRo1JKTE1N2Wqnvb1dvuc975Hvf//75ac+9Sn5m7/5m/IP/uAP5Oc//3n57W9/Wz7xxBPy+PHj8utf/7rt+YCijmhNoutpd33o6urKeHZpDJKgSZ/bTa8L3L0PhmFkpLBdaXbs2IGKigpL2FOjb4lHHnkEzz//PIDMDAfq7zmdThw5csT28aurq+H1eiGEWDHnh2ISiUSQTCatdU51UFDXgHKKCuro6JD0rFCfVIcVu+L9fHM4jXFyLqH7vhZQ16XszDDLyUwyn1CttrmSAikwm4r+s5/9LCoqKmCaplW6QdM0BAIBS/hXy0l89KMfRVNTQ14OFEz5kX1f1MwwqVQKmqZhYmLCGpsrMdetpMPJn//5n4v/8l/+i7VPI/sfzZVerzdjDnW73fjUpz5l+zgXLlwQFJHq8Xhw6NAhGQ6H8eCDD5aPsYFhCkBpCuUwDLMsOru7ZoVRQ8edO3esAvJ9fed497aOOHhwv0ylUkgkEnC5XEin0xgcvMpjgGHKnEKIl4WKRFXbKxaGYSAWiyEQCMDr9RatPovH44HH48mIKJvvvMgQrAodUkr8yq/8ivzf//t/25pHVYNQPkY86o9aJwwonocziRWUfiwUCmFqagYVFRUZv1cIp6v5ziGXIFtsfvmXf1l4vV753ve+F06nE7FYzDIeUPrKqqoqKw3tb//2b+NLX/qSrWOMjV0XNTU10uFw4PDhe+TExAR27tyJf/7nb9u6mQ0NDXJsbCzvAaBGdZAwo/57NdPc3GylYJRSYnJyEhcvXi763kcdyxTFuZL87d/+rTh48KD82Mc+BsMwMgzhfr8fMzMzCIVCllCaSqXwsY99TH7xi1+0dW1IHCUHElWssBsZMT4+LqanpyXNzcBdYUsdi4ZhoKmpyVbbVEuYjHbU5siIvefmYx/7GD75yU9CSmkJwKqRlaIyent7sXPnTnn58tLHmhACmqZlRPvm8/xt27Yt42/Va0epfF91zLHdNq0H1Ka1fsmVmyd6e3tla2srNm/ejNraWmttBpCxjn/jG9/APffcY10Ptc9kIJZSYs+ePbaOf/XqVbFhwwZJji1qXdbVCgnzQOZ+URVKKbqoHMSfxsZGWVlZOWffQXuqfNeuXOem7vlKFTFdDHbu3Clnn4f5HbTyjSDNFqpzOcKVYhz9j//xP8SDDz4o9+/fj1AoZPXPMAwrmxZFlsZiMYTDYbzjHe/An//5Xy75GCMjY6K6utqKPmxtbZVDQ0Olf2iYnGiahkQiAU3TrD0MCefRaBQuV36pkpc6vlfaQfJzn/uc2LJli/yN3/gNpFIpBINBxONxa19ATpJerxe6ruODH/wg/vAP/9D2GKY1lzIl5XJiYpjVzup+Q2aYdcj+/Xul2+VEPBpBOm3A7w/i8uUrgsXR9UNvb6/ct2+f1HUTum7C6/VD102EQhWL/zHDMEUhuz5PLq9WIp8XCiEEXC5XhiEwnU5DCCcSibmCo91jZNdwU42S9DMZo+3icAButwvpdBKmqWPDhlrbbSwFn8+XIbyRtzDVmFINwQCsVHNEOp3OK+rq7Nnzgu6DrpuIRGK228hOl6h+XmgD1Pj1FwRFUXi9XpimCZ/Pg9u3M1PszszM2K65pEZ9qN8JGkfk2LXSfPCDHxQjIyNIJBLw+/3WWJBSWlkXKDKvpaUF73nPe2w/rOn07LiLRqOorKzE6Ogoenvt1Vf0+Ty2jpkr5SZFmZGQqIpIq5UdO3bI2ehRA0I4IYQTHk/hxAwSy9VxTNcxOz3jSqVRU/mlX/ol8corr8x5vtLpNEKhkDWfkYD78Y9/3PYx/P6gFYlP0aqUno1ETjtcvHjRmkfU5y17Luvq6rLV7ujouDBNQNdNCOFEKqXD4bB/T+6//35LTKKa0vQMqeudpmk4fPiwrbYpgs80dfh8HqTTSRiGveiSxsZGWVdXByAzPTFFp9J1dLlc+Pd//3dbbe/Y0SwB06pPLeCEzxuANAU0zYPp6ZXJRPOmN70JXV1d2L59u5UiE8jcN01MTODs2bPi5MmT1vihtST7+dy9e7ftPgghYRhpeL3uBQWm1UR2GmL1GjmdTus5LweBtLq6EkJImKYOhwNIJGLQtNlnxzR162c7UCQwZS9RIZGPorBX87oIAB0dbTIUCkDXUxkpij0eT4Zzhlrj3g7qWrhQmtJSXMdf/uVfhtvtRiwWs+YF2vtTvXsSTIUQ+KVfsr8u0p6jp6dHBoP+IpzF2oaeN2D59XBVpBQAHHA6NRiGBOCwsiHQmFffq2jflu8zsBRKkYr5N3/zN0VfXx/8/tmxKaWEpmnW2He73dZaqes6PvOZz9g+xsTEBGYzizghpYFUKoEbN14q9KkwTElhgZRhVhFNTQ2SXoiphk0ikSh1t5gVorGxUe7atUt6vV7L8B8MBnH79m1s2LABp0+fLv0bLsMwRUF9mSRDhdvtht/vx4YNG3Do0CF57733yte+9rXy2LFj0u7LXzG9QLOF1mKl4HI6nZZRX9M0GIYxJ5UqAMubGJiNsiDx1O12o66uDvfdd1/e9VnyjbrMdb9yGREKdZ9UL2DyBJ4993vl4cP3yEOHDslNmzYVzXBayqiV3/u937OiCaj+mhACVLJAFW/f//73224/Go1aQhKVP1jtxtdygWpLUSpql8uFQCBQsPbJmEZzrBqxR/OJaugrBX/0R3+UERlG6arVflGK5U2bNqG7u9vWpEEOAmrdTBLZ82FgYMAa/6qDChkwyfGmpqbGdtvqHEm1pO2yefNmSzggITdbFKf2Dxw4YKtttT8kMtvNoNDc3DwnswClBadUfhT1nl1HejG8Xq+1PlJ65lgshmAwjDt37mBwcHBFBvrhw4exfft21NTUwOfzZQjBwOx1HBoaAgD09fVZY5PWfIKegfr6enR0dNgasGr69VKnSF9pShkF1NraIvfv3yvdbjfS6bS1/tIejqLBDMOwbfNQnbCya9uuJVpammQoFLKeh3Q6bV2/27dvw+l0IhAIQAiB6enpZd3vXM5vhc5oY5ehoSHxxS9+0XIizS71kP3V0tKCo0eP2l4Xgbtj6ujR++RrX8vpRZny4Td+4zcAzL6P+v1+az2nzE2q49E999yDzs5OW+N3aGhI0DsTZdwIBoOFPxGGKSEskDLMKqGjo03W1tZaaZ+i0SjC4TBCoVCpu8asAO3t7bK6uhqhUMjapKfTaTz77LNicHBQ/OhHP1p7b3wMwwDIbYhIpVJIp9OYmprAxMRtpFIpJJNJTExM5F0vZzED0nIMS2qaxmIJpBTxQx7yZOwmY2cymbSM09kRvXTu1dXVePjhh20fe2JiwjLO5MNSokMLaYAiAYBSJCUSCUQiEUSjUSQSCei6jlgshng8XrBjZh+/VIbKRx99VPzwhz+E2+22hFoSMpLJJHw+n3Uf3/CGN+DIkSO2Lvzw8LDQdd1qw+fzWV7dTH60t7fLgwcPymQyidraWmiahmQyiZmZmYKOURICSRxV5y36rBTRASp//ud/LsbHx62axeQEQkK/mjaytrYWb3zjG221f+3aNUFpYdXrQPPPbNTh0jlx4oSVBpeeeVoDyNgGzM69dg12qkDqcrmsKPClcv/998sNGzZY14yijkgkV7MRSClt17akiCYShr1eL6LRqK029u3bZ90L6o+aMpvE7Bs3bmBgYMDWpOp0OhGJRJBOp60U9Rs2bMD09DSuXl25kh0HDhxAZWUlgsGg5QSs1qkWQuDkyZMAgDNnzsxb443GlaZp6OzstNWH6elpaJpWkNTy5Y763JTyXFtammRFRUXG/BUKhaxIbnJaC4VCVjp8O+SK8s+1RyvlfmS59PR0y82bN0PTtIz1QNd1JBIJDAwMinQ6jdu3b8PhcFj1Ou2Sy1Ev1zgqlTPY3/zN31hODosJpB6PB29961tttR+PxzPm21gsZnu9YTJZrc9cufLv//7v4itf+UrOWvHknJxOp2GaJjZt2oSf/dmftX0MikYlZ8G1vlYy6w8WSBlmleD3+yGEQDQaxdmz58Xly1fE008/LZ566ineXaxhGhsb5b59+2RFRYVVL4a8Pzl6mGHWD9lGCPLc1DQNDocDPp/PEgSXkzIt198t9yWWjBJkZJ/PuLlc1CivZDKZEQEGwKpPCsDKwgDASqlJ1/Ztb3ub7WOPjY0JNULVLrlS4OWiUC+jVJeGBGUyJKbTaSQSCUSjUSuSrBiU2iD5x3/8x1bELwArDXMqlbKECOrjL/7iL+Z1DK/Xa0UrLyWy7fWvf628//4jcvfuHrY4vEp7e7vcu3evDIfDAGaf1RdffBGpVArnzp0T586dE6dOnSrYQFLvu/oZcNfJQzW0lorHHnsMlCZbFc/oMzXC9nWve53t9kkIpnOmZ0UIYVvs7+/vB5A73bbqLOPz+VBfX2+rbTUy1eFw2Bb17rnnHrjdbivSOzvbAd1jmgebm5tt9W98fFyo7WiahsFBe33cs2dPTiep7PF4/fp1W30DYEW6CyGQSCRw584dRCIR9PX1rdjgpnccinDJTtNO/Txx4gSA2SiWO3fuZIjs2aKMaZq4//77bfXj6tVhQfd5tddpLmeam5vl7t098sCBfXLz5s2grEimacLj8SAcDqOv75w4deqM+OlPnxKPPfY9cfz4CfHkk0+LS5fsOQCQwwNgv35yOdPa2iI7OzvkwYP7rbqtsVgM6XQaLpcL8XgcTqcTdL3OnesX/f0XxfHjJwTVbs6HxUTSUgom/f394tlnnwWAjDU615dhGDh27Jit9kdHRwU50Xg8HkxNTeHWrVvFOJV1zVoQ3Ep5Dn/0R3+EWCxm7f88Hg90XYfH48nIjgLklyFHre263jItMOuDtbNTYJg1zM6d7ZKMyqX2XGdWjo6ODknRPbTZSiQSK+rVzTBM+SGltNJ4kjeoGvmQjyEo+4VOTS1YiP7SF0XiFAOKok2lUtY1ME0z42eHw4FUKgW32z2nH+QdXl9fjw996EPyK1/5iq25NplM5n1uSxFdCh1BStFbZGSniEpgtt5SvrWqlkI+kSv19fVyfHy8IB168sknxfHjx+X+/fuh67olZlIUBnlIx2Ix/MzP/Aw+8pGP2Gp/cnISFRUVltEwEAhg164u6fF44PP58OSTT2ecx733HpIvv/yyFSlTTg5QbW1tkiLWKB2z0+lEPB630pLSvZycnFxSfc5sAUId+0IIaJoGr9drGXrT6TTS6TQqKyshhMCZM2eKMjBpDiBUEY8+X06K3a6uLgncTfN9+fLlvBr65je/iU984hNWv9Qa0qp4pus69u7da7t9mjfVe0LPrN26xBcuXBCTk5MyEAhY/VNTval1ctva2vDDH/5wyW2nUinLgTQfjh49ap1rdt9ovVDHvMvlwhvf+EZpJ2uLrs/WT8y3j+3t7ZZRk/qWTqet+ZrW/cHBQdtt01hWa/SRM8JK0dXVZUWLqoKWml7X4/Hg3Llz1t+MjIygrq4uI/IXyNy3vP71r7fdF3p2ik17++x7vcPhyHBOUh0G6FrQOpzvXLEY+YzLxsZGuWHDBiuTifo8x+Nx+Hw+61misUV7Da/X+6rDkAORSAx37txBodb1bKLRKMbGxlBTU4NQKDTHaU6lnKLZmpubpbonpHGiftG8Pz19NwK8srISoVAIzz77L/OejNvtzUvYyLVfU/dxuVLvrjSPPPIIXvOa11gR9gvR09ODpqYmOTIysuQbT/uDZDKJcDgMp9OJa9dGltXn9Yj6rJXTc7cYSx3bdp+Bhobtsq6uznLUikajGBkZy+vCXLt2TfzTP/2T/OAHP2itjTQ/kE3R6XQiGo2io6MDDz74oHz88ceXfKxoNIpAwAen0zknxT3DrAV4RDPMKiAQCFjRHj6fr9TdYYpMc3OzDAaD8Hg8cDqdmJ6ezvD8YhhmfZHrBVJ9MaFooVkv/OWnTct+eVU9TvNN3UsGfPXnQpNOpy2DXbYRjETlUChkiV+UhoiMs6ow8tGPfhRf+cpXbB2fxCE7Bhci23C3nOu9FNSoM/V+0FjSNA8Mw8C1a9fKxnpRWVmJiooKORshHLdE3mvX7F9vAPjiF7+Iw4cPW0avVCoFr9dr1UCLx+MIBoNwOBx429veJr/73e8u+TiDg1fF4cP3SDJ20H0kh4Zspqam4PP5EIlEyi56ieoTkmHQ7XZbolFFRQUmJyet/9+wYQPcbjdisdiCbS4UqU6Cjc/nw/Hjx1d0/JEzIjmd0LygzoEkbOTzbIbDYSuCxe1249ChWWF8bMyeMeyJJ54QN2/elFVVVZYnPxnTVYOVlBJ1dXW49957pZ1rmUwmM+qcqt/zcdS8du0aent7AWSKrWrbqVQK3d3dttpNJpMg4TUfo39PT48lQlE/slGf13Q6jUOHDuFHP/rRko+RSCTgcvkzUnnbYfv27QDuPhc0Z6tr1qtiu612Gxq2S5rvyWgaCARW3BG3s7MTpmlatcxIcFOjOW/evIkLFy5Y47evrw/33HMPgMy1UxVKW1tbbfclkUjkTE9YaKqqqpBKpawxS2uQ1+uFx+OBpmmYmZnJyPLQ1tYmC1UTNjtC3u5cRllLAFg1bCkC1O/3w+Px4MaNG0gkEqiqqoKu6+jv71/xvcSLL76IEydOoKOjA21tbQgEAhmCOpG/80KrNAwDV68Oz2mgrW2HpFqgtJbEYjF4PB4IkVlTOPvnXE446v/FYrG8HWxu3LiRV3kmmq9zXatysU088sgj4vOf/7xcTCB1Op3w+/3o6OjAyMjSBU6KyiMnOhaHCkep032XEtWZRNd1VFVVobKyUk5MTGB01L7zyP/5P/8H73//+y0nCnI29Pv91jUmR7d3vvOdePzxx5fc9tjYmKiqqpAejwdSyqKVYWGYUsGzOsOUOfX12yRtwAzD4IVojdPW1ib9fj80TbNSHV66dKlsDNQMw5SGbKFS142Mepr0f263Ey6XoygvmoVItZuvsLAUVCeSeDxupZ+kY5MB9vr169iyZUvG36rXS9d17Nu3DwcOHJDPPffckjtrV+RQIXFDTfGo/l/2Z8uFjNAUmaCKpWrEQnNzsxwenmv8KwUU5SWEQDgctISLiooKefv2bYyNXbfVz6985SviM5/5jKyqqrIcDNxutyWWqLUXH3roIXz3u9+11V8yjnq9XquuHtVZO3LksHzqqWes/lZXVyMSiUDTNHg8nrKKICXjLhmwXC4XqEZlZWXliouYxSQej2N8fBwVFRUIh8Pw+XxzIruXIyBROtB0Og0pJTweDzZt2gQA0u78cebMGbz+9a+fE/WqQs/2nj17cPz48SW3nUwmrchMVVDI14h57ty5DIFUrS+p67olQtsVSK9evSoOHjwoAVjr4VLp7OyUmzZtyqidRxGZAOZElNLY37dvn63jzBomvVbUhh32798va2pqLCGNxqIaxUv1Bi9dumSrbb/fbxn4KfIvFotBnZdWgvr6emsM50prrGkaTp8+nfE3Tz/9ND7+8Y/PcSyieyalhKZpeMtb3iK///3vL/l8IpEIyPALzIpcdlMiL4Vnn3123jabmmbrcsbjcaseJwkzhSbfdOFUzkDt1+TkpOV4VcyIVzsMDw+L8+fPy4qKCuzYsQOmaVriFrGcfRVFpe7e3SNpb0JRwH6/H5FIxBrXsVgMw8OjJb8mhXJ6U8dNOQlbQ0NDi2ZNoHlm7969+MEPfrDktmnMUxQ0pxgtPOU0lnKxFIcSu+cwPv682Lhxo1QzLQUCAUqBL+2KpGfOnBEnT56UR44csfqr7nMoS0sqlcIb3vAGW30FZvdsfr8fiUSiaCVzGKZUlJeLMsMwcxgff15MT09bL8TFirxhSkt9fb3s7e2VVVVVcLlcSCQScyIBGIZZn+QSyEi0IKMMGd5N08RyagwVA7WO3VLSb+aLauyjCBS1LikADA4OWvUngbspQ9U+USTHQw89VJR+ztd39XuxoRRtFDEXj8eRTCZhGIaVkimfyJKlkq9RVl0XKVrY7Xajuro6r36cP3/eihiliBjK2EEprIUQePOb32y7bbUWJKVPJCEgEolg3749cteuLllfv03GYjF4vV5rvJYTNEYostLpdOKZZ54Rp06dEj/4wQ9KbvAtJMPDw2JoaAivvPIKpqamrOdAnUOIfJ4NEnD8fj90XUcikYDb7UZFRYXtts6fP58hJtHzrNZmJMGvvb3dVtvqGpIrjWJ9fb2tBebq1avW9VPrH1OfKb11Y2OjrX6q2DVWNzU1ZUQB0XVTnXhovJOg63a70dTUZOs4atS43QjS2tranDU5qW/UrmmatmuQUmYa6p/H47GciFaKxsZGWVlZCWB2zKVSqQyBku6Jml4XAC5cuDBHEFfr0dL6vn//flv9uXp1WJCjVU9Pt9y6dSt6erpXdDM1MjIizp49K4aGhkQ6nUY0GkV/f7+wK64XG13XrfkxGo3iypUr4tKlS+LixYtCjfYtNRMTE1YNvoWyM9idz+vrt8lsRzYSIrxeL2KxGM6fvyD6+s6Js2fPi2II7SuJOv/MF91aDpw5c2bR36Fz6enpsdU2ORHSvrDcsn2sFsppvJQLtMckJ4tIJALDMFBbW4v6+m2216DvfOc7GenqKTMC7UGB2SjStrY27N+/31b75IAMAMFgEK2tLbK9vbV8jA4Mswx4VmeYVcClSwMiEolwit01SENDg9y1a5esra2Fx+Ox0ub09/eLvr4+0dfXx7tIhlmFZBszSWzK98VQCGF5apJhVMIAhAnhyGx71uBsr30SiXI54ajiXT79V424JLQUA0pNTtFtBEVKCiFQX1+Pn/zkJ5YxmYz1qnctMGustVt3cjlkR9CQ8SW7Xl+hhG812pYMyqrYTnXD7B4vW+jNZUij81hKnSgVulc0ftS2XS4X9u7dbfviPPbYY/B4PBniippalK59bW0t3vrWt9pqP5lMWgIuRZ9lt+t2u1FXV2dF1blcrgzhwy7ZRrvsiL98Bdhz586Js2fPigsXLoinnnpqTe9L/vVf/xXnz5/H1NRURnS1KpoBc+uoLgWKaqc6fQAskbSlpcXW+Dp79qw1ltT0h+QYos7ndiMzR0bGhMfjsRwm6Dj0/FZVVdlq7/jx4xnrkzr/qJHVfr8fu3btsnUdyPnm4sWLtsblsWPHrL9PJpNWRIUKPaeU2tYwDOzcuRO7dvUuuY8jIyNC13XEYjFcujRgq4/33HOPNYdQBiHV6Ek/37lzB3ZFKdWZiDIKrHSWos7OTmu9TqVSlhit7hmklDhx4kTG3507d07cvn0bwN05jsYp/e3MzExeji30vFdWVmJ8fHzFRWOV/v5+QZHldiPMc81ZuSL+8tlXjI6OCnJ4I2eicoX6SHtPVSBQsbu3HR9/XqgOKjTfnjvXL5599qQ4e/b8mlsns51lsveR5SB89fX1AUDGPkct8UFzRTwex65du2y1ffnyZUHrNq07zNLJziCkjqdCjp3sfa+a0n+5jp+LzZfqGmQHtU16/zIMA9FolLKM2OI73/mO9U5Bz6ZhGNbP6jPxwAMP2Gr72rURQftOYLZ0hNvtRmdnB4ukzKqHBVKGWSVcvnxFkHjGrA26urpkOBxGZWUlUqkUNE1DIBDgNMoMwyzC3ZevQhh61HaKZeBQX0yL5XXt9/sRCATg8/kyapHR8YHZF3SPx4Mf/vCHAGB51dLv0Quk2+1GTU0N3v3ud5fkhW++F/BCRgbP11Y5GLkWQxVJ1Yg0uzz11FOIx+OWWEsRXqpIDcwaLOxGG0SjUctoqhrmVhv51E1czUxPTyMajVop9LKfk+U8H7kMdGpKazvcvn3beg6yDdfUPkXv5xNhTQ4S2ZGkuWr4LcaLL76IWCxmPQNktAMynzWPx4Nt27bZapui2OzS1dUFYPa8KOUmzQPz3WPqZ0tLi61jTU5OYmJiwnYfW1paMlLNApl1Nun/7EaPZreTPd+tFAcPHkRdXV1GGnKC6gDH43FcvXp1zt+OjIxYjl3q/oX+HQqF0NbWZrtPqVTKqvNYV1cHh8OBrq6dq87wm0sgAJC3843Kjh07ZDKZRCKRQCAQwCuvvLLsNpnVR6H2igtFyDU22stW8PLLLwNYuM45MBtBn28tVoaZT4TNFxJE1WxQ5FiZSCRsR5FeuXJFDAwMWPt3tX+0LwRmnTvuv/9+2/2lrBpqm+wwwKwFVt9bOsOsYyj9GrO6aWpplHv2zUa6+P1+3LlzB7W1tbh+/Tq2b9+OK1eulL91mmGYkqIKf1LKu9GkZShuZYsBxXqJqqqqQkVFBQKBQEbUJXDXqOFyuRAMBvH1r389o64bra1q3xwOBz74wQ8Wpa/ZLCaIFtook6vdfOuRlQISaRwOhyXkCCHQ3d1p60KdPn1avPTSS3M+V68LiVBHjx611cfR0XFB0Xxk7Fjpa1uI48VisQL0ZPUQi8WQSqXmiKOFfAbVWokk6qh18ZbCCy+8gGQyuaRxtWHDBtt9VCPIs6OQ7Yq5g4OD4tatW5agRVFdlFKOmI1C6LTVdr4CPtWpI0Nfdl+yofvkcDhw4MABW8e6dm1EjIzYiwAEZkVcKaVVpxXIjIKi+a+/v99Wu/X122S2QKoaTVeKBx98EDU1NVY/KHqUzgsAxsbG0N/fP+faPfvssxnRtGo0G63n1dXVuO+++2w9uNFoFJqmWdGG0WgUfr9/GWdZOoq5f/B4PHC73RgbG7PtMMCsLZa7bwyFQmhtbZHNzY2yqalBtrQ0ye7uTrlr16wjuR3GxsaszAdq/7JxOByoqKhAY2OjrYej3EogMCtPLsex7J/tkkqlrKwzVJKD1v1kMpmXk9szzzyTIYTSHoLWR3q3ue+++2y3nUwmM7KX5OPkxzDlCAukDLOKSKfT8Hg82L9/r9y9u0c2NzezG9sqY1dvtySP5IqKCkxMTMDn8+E//uM/xPj4uPjmN79Z/pZphmFKTnaaouzPyolc0VLFoKqqCpWVlfD5fHPSt6perqFQCI8//rgYGxsD1fUisVQV3JLJJI4ePYqenp6ir7VLTf9ULO91VRwt13Gkkj2GyLieTzrEM2fOWAZ2NYJMTUEFAHv27Mm7v+q9LUUEwnLuqd3UjqsdMvSQQSnXXJsvamQn/TtfgfTixYuCUsKq82q2U4hhGHkZ1xZy2sjHyWV8fDzjuVUFLfq3w+HAzp07bbWr1vhcKvfee6/ctGlTRlS3naj9e+65x9bx8qWxsTFDSM41hzidzjk1OhfD7/dbIj2lPVzpCNKOjg65c+dOBAIBK3KGREngbjrw5557LuffP/XUU1b68uxUsk6nE4lEAoD9ezU0dE3Qsz89PW2lOVxtmKaZsa+ZL+19Ply7dk1Q5qO6ujqMj48vu01mdVKI8eTxeOD3+xEMBhEIBKxMMB6Px3b65tOnTws1VXr2nlrKu6nJfT6f7T0jByowwOIRynaJxWIYGromEokE4vG49Q5K4y0f8fGnP/2p9U5Dz4E6fl0ulyW+Hjx40NbmNpFIzMnesBreHRlmMVggZZhVRjQatbzbh4eHeSVaJXTt6pT7DuyVXq8XhmEgmUwilUrB5/NhcnKy1N1jGGaVMuflRLxam3QZFPolJ1t8W6z9+np7KbWIqqoqhEIhqw7pfNFflFb3sccesyJDnE6nZTRRDcahUAgf+MAH8umOLZabnilfVusLrcPhgK7rVk1HErbzEW6eeOIJALDEAjUqCbib2nLTpk3o7u62dZMoOq0Uwuh8jhOcIm5hyDCradqcSP3lGoHUOTA7ettuTV5g1kiV6/6qdaZM04TX68WOHTts3fhcqYBz/bxULl++bNVHVdvJjlxsbW211W46nZ5TO3Qx9uzZY4lHas3WbOaLwLMb5ZoP7e3tsqKiwhobqkBK0LW8cuWKrbZ9Pl+GWF9I8WypHDt2DIFAAF6v13JGIPGdaqcBs/Vrc3Hu3DlIebcmNc3dav1S0zStWrN2oHqoQszWxc3n2Sw1iUQC0WjU9rOxFOrr66VpmpiamsLMzAyuXr26OjcSTF5kv3csd/6gOc7tdluRydRWPns6ynqx2L7a4XDYjg5ngZTJLg9QiGdgfPz5V2tNXxejo+MCgBVRqmkaEokEGhq229rDnTx50lof6b1GTUuvpqS3mxVD3XuWYv/AMMWCBVKGWSXs2NEs3W43QqEQ/H4/6urqSt0lZons3b9HejweJBIJq67NhfMXxalTp8S5c+cEv1gyDJMPuTw3Kdok37aKAb2EZUc2zcf4+Hhec2JlZSXC4TC8Xu8cgVQ9Nhlev/71r0MIgXQ6bRlhqAYpMJvuUdd1vOtd78qnO7ZQI0gXolAvoIV4oS8lFAWgChtkILebZvfUqVMwTdMaA6pBLjti6+DBg7b6GY1Gy8azuhz6sBogQYQMR4VOj5z97NH4yscQnB3Zlh2VqR7LriE4e12hz+jfdgXXs2fPZvxbrVmliqT19fW2+jk8PCxmZmZs/c3BgwetCEMAGRGaQG7DujpHV1dXY9eu3qJ6GlAkLY09NeWxOhZTqRTGxsZsta0KfouloiwW999/v+XUQk4uJOrT54ZhzBk3xMjIiBgbG5tzr+jvPR4PHA4Hdu3aZbtvtLa43W6kUinLqWo1MTMzgzt37ljRSBRRWoi9nsPhQCAQgMPhwOXLl8XevXvZ62adkT1XLGfuoPmf1gR17c2n3VwR3+p7kVqCY7Wmz2ZKS6EjSLOh8WoYBlwuF1KpFOymmx4eHha0N8h2OIjH4wBm9wKGYWD//v222h4buy7UvXGpnHwZptCwQMowq4Dm5kZZXV2NVCqFqakpnDp1RjzxxJNs6Spz9u7fI+85fEj6/X7L6B4IBFalJzLDMOVFthHcjgBZClQDeLFq+FRWVqKiogJ+v9+qcUfQtVGPffLkSXHixIkMsUJN70fft2/fjv/0n/5TUS/qfC+XxRIx52t3tbzkktDtcrks8cDtdiOZTNo2eJ04cULcunVrTvtqOioymD/wwAO22r5yZUgAq1+QXk8Eg0FommZFkGVHORby+SDDUr6GYDJyZUdlqkIaReTZTVVIqDV01c/spgQ+f/480um05aAy35q1YcMG7Ny509ZFtptRp6enB5qmWWIa9Sk7rTaQKQrTtXA6nctKub0U9uzZk9Efqqun9s3hcOD555/H0NCQrfN3OBzWPVXFiJVk9+7d1lgl8ZecksggfOvWLTz77LPzntvx48ctA6/6DKlRMhs2bMDu3bttjad0Om2thYlEYlXWHYxEIpiYmEAymZyTgni5jI6OCl3Xoes6Dh06JM+cOcOL2jqlEA5gC0Xh5ZP2m9ZFtS0a+9n/znddZNY3ufaBhdwb0r6E3m9oT2oXSr+vOoCqay191t3dnVcf1TIFDLMW4Eq6DFMmtHW0S6/Xm/HS6vO4rXoslMqGPd3Kn47OdllVVQXDMBCJRKBpGpLJJAYuXeEXSIZhCsLsi5jMaVQoR4ErVzRSofH7/fD7/fB4PNZLYDZk4CZ+8IMf4NChQ5bhXgiBZDIJt9uNRCIBr9eLeDyOd7/73Xj00UeL0u9czGdwKkYUG1HIKLmVwO12W+kxDcOA1+tFNBrNy+B1/fp1KzOHmnqSxgoZ23t6egp3AkWkHOeA1YLf74fb7YbL5bIMP6phShXU7LJQ2uN8nj1Kn5YdkaqmKKV/52Nco76pjhP0s932Tpw4IVKplKTo/mzhka613+/Hpk2bcPny5bz6uxS2bNkCj8cDEnlynUuue0Ln7nI5bddKtUtra2tGpBMJynQf6Jq99NJLebWfKyp4pWhoaJD19fXw+XwwDMMSNEm0drlcmJycxI0bNxZs5/z583j44YctJ1Q6F4/HY7Xr8/mwb9++eSNRcxGNRkElUTwez6pMq5lKpRCPx+cI69kiez60tLTIZDKJq1eviqtXrxaiu8w6pb5+m6Qxme2gl6/4miutdPaYJ0cXu2v5bH94f7WemW/+LOQaOjo6Ljo62iSVE/H7/dZ+zw6Dg4PWXob2hbTGGoZhZWtoaGiw3Tatu7R3W03vjwwzHyz3M0wJ6ejokHv37pX79++XQX8ATuGAAwKa0wWYEum0gXTawOnTfeLUqTNidHRcXLo0wKtPmdLa2irvvfde6XZ5EIvEkU7q8Lp9gCmQStjf1DAMs3rIjjwsJFIakNKA0ylgmncjI9yaF8lEGgJOeNw+6GkT6ZSBinAVlrvFUyOGyGiR77k5HA5I04Sh6zB0HSiSITYcDiMYDMLtdmdEfJEhWUoJTdMyDCJf+9rXEI/HM4RTl8sFXdctD1u3243XvOY1aGpqKppVhIS+7FRF2cbrQoyv5uZGSWkNKUoOuBvlm+3Nb4elGgjyaT9TpBE4ceI58cwzz4pnnz0pzp49LyKRGEKhCui6iebmZlv36rnnnrPOXxVG6N8klm7bts1WnwHA5XJbYzA7Gi87+ps+k3L5ERnZggp9zizOQuIfOV/k4zWvihP0vKuOkfmiRs6pacKJfARYqp+rCpjUNglPdlHFPPUZyDZS792713bbS+Utb3mLrK6utoQjEuWAzNSLakQiRXHQPJ1IxHHwoL2UdHY5ePCgFV0J3N1jqLXDAODChQu22m1ubpZqxGUikchY81aCXbt2IRAIWOfm9/szjKxSSoRCITz++OMLtvPUU08BQEYdaiEEUqmUlVUgEongta99ra3+jY6OC4pMdTgccLlcuO++e+WOHfbWlVLidrut/qt1WrOzI9DYtoNpmqsmG5IqvKl72myjfj5RwoYhYRgSXq8fpgmsQh19yaj7QyAzPTyNqXz2F1R7EcCcOdfhcCCVSqG5udHWc6fey+w9c3b/7e6p6ZkyDCMvwYqZu4YByLlftYv67pK9p1a/CiVkZrdTqPYbGrbLtrYdkrLkeDwe65p1dnbYLiGSnV7X6XRapWUow0htbS327dtnq23aO9C6uxozLTBMNiyQMkwJaG3fIfcf3Ccpl3wikbAiHmij2d/fL/r6+sSFCxfYmrUKuO+++2QgEEAikYDf70cwGERfX584deqU6OvrE3bTfzEMwyyEpmmIRqMIhULQdR3PP/880uk0KisrMTU1VXYRZGQE93g8RavnpWka3G43NE1bsrHm6tWr4oknnrCiRCg1l5TSiiwyDAOBQADvfe97i9JvOxRC4CKveTIOkVFKFUPKkcXu6blz58Tx48dFMpm0nXLz+eefB7CwAK1pGvx+f17pGtVaPfMZcZjygLzqs51EiEIa1+aLKF0qJFJQO2o9RxUyNNtBHaPZjhPZov5SoZqRi9HS0mK77aVCqWsXckKh9Yqi09UIRZfLBa/Xm1fEhR0qKioy6rRmR7bTXDUyMmKrXXIgovlejdpcKdGrp6cn43yA2eufTqchhLBS3J45c2bBdp5++mkxNTU1Z96m9Y2E1o6ODtt9TCQSljNEPB5HMplEIBBAa2tLeW2u5mG1pMtfzYRCITgcDty+fRtSSgQCgVJ3adWTnRUnn4hn1eEmV9peYHlOrfxsrW/m2xcVak+fyxFBPaYdbty4kZFpRM0kkH08uw6glGKXnwdmLcECKcOsIHv27ZZ79++RNTU1EEIgGo1aNbSOHz8u+vr6RF9fn+BaHquDhoYG2dHRIQ8fPiwjkQgCgQDi8TgmJiYQiURK3T2GYdYwFAlJkR/t7e0wDAM3b97MK8otm1wvefm++KniEBl89+7dW/C3KTLwzpcya74Xua985StWij/yDNc0zTLWkhH5Pe95T6G7PC+5hJnlUl+/Tba0NEmXy2UZ3dUoknxFj5WC+rZYvcb+/n7bJ3H58uU5dexy4fP5bKfWTCaTGVEWqlElF2xoKC2UdjVXVGehDUHq3JhPu4FAYE79UfU7ACvyUa3LtlTUOVsdt/nOFX19fUsySBezvuf9999vnUN21DwAK6pSTa+tGiZJnNy+fTtaWoojlu3evVtWV1dnRP6q11s1bvb399tq2+PxIBaLWf9OJpM4fbpPnD7dJ5588ukVWQDuvfdeAJgz9mkdBmbTZB4/fnzRtkZHR60IZ7WWrBod1tLSYjsDRDqdhqZpoBSHqVQKTqcTwWDQTjNlAzvkLIzd+behoUFOT0/D6/WisrISwMKZB5iFmS/qTy11sFSWEglP80M+jkO8R2MWYjnzbFPT7Dtarmw+6hq3VJ5++mmh2iSzBVKKiAaArq4uW23Ts8PPA7OWYIGUYYpMU0uj3Lt/j9y7f4/0er3QdR2RSASJRAJCCGiahrNnz/IbyyqivnG77O7pkpWVlaiqqsLk5CTo54GBAXHlyhVx7tw5vqcMwxSN27dvw+12Y2ZmBh6PBz/84Y/F1q1b4XQ6MTo6WpAXlmwPVvUzu1CqQKoLZhgG3va2txX0rUo12qtCBhm0SYzIvjaPPvqoGB8ft+rqqdGVwKzwKqVEa2srHnzwwVX5JtjQsF263W54PJ4FBWSKkCpHTNO0xKtCp3K6du2aZShQ08bliqhtb2+31TYZ1nM9R9nPExsaSg+Nr+KkS8+cf2hOyscIDCBDrMmur5x9XFUUWwr0HGSPW/XfO3futDVg+/r6liQiFDOCdNeuXRn/znacEUKgv78f4+PjGTW21HkhlUrB7Xajs7OzKH1samqyhDm1j/QzcNfAOTg4aKttqq/r8Xjg8/lKIvi1t7dniL8k4tO11jQNr7zyypIyAZw+fTpjrFLUL0V/xmIx+P1+7Nu3z1YfyaHA4XBYdc1N01w1aTU5sqe4uN1u+P1+JBIJvPjii6iursZPf/pTfvfPg2xhNNsxZGzsuq3rmitLjSps0nfDMGyvi9mOQszqZDnzovpukJ3CO59yBir0Dqq+M6jjLR8njKmpKevn7HcP9TrYFUjpuVxOim2GKTd4VmeYIlFfv012dnbI6soqwJRwCgeiMxG4XRr8Xh9qq2tw4cIF8fTTK+OtyxSGrq6dsra6Bi6HEz6fzxK6Y7EYvvvd7/K9ZBhmRairq0M6nYbH40EikQAARCIR+Hy+sk2RSiIppa797ne/Kz7wgQ/MeUttaGhYlkWPXlrne3nNxeOPP26l06WUXpRikYyubrcbH/nIR5bTtZLQ2FgvKfWwpmkZKRvJ2EOe9BQ5V46QQJpMJjE9PV3Qts+fPy/i8XhGusfssULjqbm52VbbNN7VtoHCOiAwhUMV33MJ2oW4R7kixPMRXnw+37zjRxXSEokERkdHl93xbHHXbrr0K1euLOn3KioqsGPHjoIrO01NTbK2thYArDWCHHfUc/vpT3+KM2fOWGNBnQ8ousMwDNxzzz2F7iIAoK2tDcDc9I/Zc9KNGzdw9epVW/eV5vxoNIqpqSkkk8ll9tYer33ta2VlZSXS6bTlfKQK0fTz+fPnl9TeU089Ba/XC8MwkEqlrPqYanSqw+HAkSNHbPVzeHhUkEBNBmv6Wg3Mt+dhwTQ3dq/L0NCQIBvAvffei5/85Ce8eOdJtgPIcjOoeL3eeWuPAnfnVV3XEY1GbbW93CwKDDMf9fX1kkTQ+VL25iPK37hxI+O9Q32u1Mw8dt9t1L9nhwFmrVCeLuIMs4rZsaNZ+nw+q0aNrutIJBJIJpO4ePEy76Tmoa2jXXq9XstjWtd1DFwqn+vV2toi/X4/vF6v5T0ej8exY8cOfOtb3yqbfjIMsz6IRqOIx+MQQoDqWZMIdvPmTQhRPi8qDocDUonAEUJgYnIKR44ckdevX5/z+xUVFWhoaJBjY2O25lZ6mcyuF7iUWn+PPvooPvzhD8Pj8SCZTELTNEtsprR/pmnizW9+s50u5c18hqF8DDIej8fyPKYoGDW9JDArzqjGYJdr8RRlKw0JlLqu49q1awVfd2/evImKigrr+qgpcYG7Y8lu7cF0Oj0nujk7SgKYP6qUWVnUuaKQqcapbRLmsgVzuwJpb2+vdLvdGenXso9F53Dnzh3bfSWxKhtqk1KR2+H06dNicnJSVlRULPh7TqcTXV1duHr1qq32F2PPnj0Z6XUBzPk3AJw/fx7RaBQPPfQQgLu1tuh33G43UqkU9u/fX9D+ERTJoc5DuViq4KwipYTP54Ou6wumpC8WR44csYR1Oj91jSaHkqeeempJ7fX19Vn3MJ1OWzVW4/E4nE4nPB4PhBDYvXu37b6S8xA5DlGd89UCi6HFo6mpSZIQPzIygmPHjkmPx4Mf//jHvIDbRF0X1Uh9Nf3nUqmvr5c+n8+ar7OFIXVdj8fjtp3tZv82c2/IrC6WOy8utldfTvu59p3Z7wtNTU1yZGRkyfPMyMgIDhw4MK8DHf178+bNefVX/c4wqx2e1RmmgOza1SXr6uoQCoWgaRri8Tji8TiCwSACgUCpu1e2dHTulG63G8lkEnfu3MGF8/2iXMTRhobtcvfuHrlp0yYEAgEkEglQlInf72dxlGGYkkBOOEIIpNNp/Kf/9PPy1q1buHHjhu00ObkopDCQLVA6nU5s3rwZ0WgUN2/enPP7ah0yO+RKGaT+30Jpsf71X/9VnDlzxvp71Xiufq+ursav//qvryqrI0WN0rmr5wfcrWujRjmWM8WKcH3ppZcyBKH5InC2bNliq92xsTGRnap3IQGcDQ2lJ/s+FDJdpToW1DnLbi20bdu2zSvWZPcz1zy7GOpcoF4L9fN8oukGBgYW/R0hBPbu3Wu77cU4cuRIhjgNIEMcpftw6dIlK4Ix28BOYpkQwor0LDSdnZ3QdR0ul8tK95xdE9cwDJw9e9ZWu62trVJKienpaSQSCbhcrhVPGfu6173OSlkL3N0T0LovhFhy/VEAOHv2rHj++ecBzK51Qgjoup4htEg5myLfLmraajVV/2qgUCkfmdw4nU5MTU3BNE0kEgnEYrFVJZ6XG9lpS9Xofjts2rQJbrc7pzCqikK6rmNmZgbj4+O2HTEZZiHy3SeOj48LWl9yzd3UrsfjsdXu4OBgRlppehZoHwPMjuvq6mrbfS7k3phhygGOIGWYZVJfv03W1tbC6/UinU5btQxcLheCwSCeeuoZ3kktQrmIoSrNzY1yy5YtiEaj1svP9PQ0rlwZKru+Mgyz/tB1HX6/H8lkEpFIBFevXkUqlYLP58PExETRXuLzaZcMHcDdl6l4PD6v8UPTtGWl/VO9xLPTT2ZHCqk89thjaG9vt2qypdNpOJ1Oy+hKNc1+8Rd/EZ/73Ofy7p+d8ygE2VFIJJCq9Q8purTQtT0LSbZoXWgmJiYgpbQM96ohXD02RWzbQTVEzMd8KbWYlSXX9V8sAn2pqKkDsw1fdp+9DRs2WNF/qjFN/U5iYD4pqbOjngFVOJ4bAb1URkdHcejQoUWPnU+6t8Xo7u6GpmmWgAZkpoZLJpNIJBI4deqUiEQiUk0JT/MCfeZ2u7Fx40Z0d3fLCxcuFPSh3bx5syWQ0neam6jfyWQS165ds9UuRbz5/X7rGlDmgJViz549loBB86LL5UIqlbKceaampvDUU08t+ZoODg6ipqYGXq8XyWQSpmnC7/dDytkapB6PB5s3b8axY8fkE088seR2E4kEvF6vVbeVBPLVQq49BBuyM8kWz5bK1atXxb333iuj0SgSiQSklLh06RI6OzulaZqIx+OwmwVlvaKKLOpXPg4JGzZssPbqavvZa1U+6XUBiipnQWg9M98+rpBtq1AmA/p/u1kfXnzxRaufan1u0zStnynwo6urS168eHHJJ8XiKLPWKH83cYYpU1pad8i9e3fLjRs3wjRNa5NFaaccDgeLo6uQ5uZGuX//XllTU4N4PI5UKsULP8MwS6YY4oaa0lB98Y/H41a6PCEEKisrrVpcxSDfFyG1pgoJAULM1kCjenAq0Wg0r8gkSh+rGrLVdLIk1M4nkH75y19GMBhEIpHISB9J1z6dTkMIgfb2djzwwAMFXRhUIUKte5odDZPP+FLrupGxnY5JEVOJRALpdHqOF3++659qQMj+PN82hRCWsbq9vb3gC/Po6KgVsUVixGy6YZfVZ9M0UVVVhfb2nbaPr95TYH6joCrS2mmbyPbozh4zqyFKuJSk02krqi57DgHuzsf5CCUkOvp8PitilMShwcFBWw+3GmFJUXc0V6mGL8MwbKdibWlpkrFYzHoeVCGWBEIAeUVMnTp1KkOUoIhM9RnTdb0o6Wu7u7uRSqUynEHoZyEE3G63da0GBgbESy+9ZEU70j2nSHtKnX3gwIGC9rGjo0OS+K3O1wCsOpupVAp+vx/PPPOMrbaprjatg8lkckXngze84Q2ShEsaq/Q8URSpEAKXLl2y1e6JEyfgcrmsdqgGIdXJpdItb3zjG221OzAwKKi0CrW30imJ84XuMT2/2ULgcvaqq2kNyXaWy3ZConPJJzL4+PHj4vz582JwcFD09/eL4eFhcenSJTEwMCDWqjhaDCcuuva071HTf58+3WfrYPv27bOcXrLXFeBujXFN0zAyMmK7r6rDIQVFMIuT7YSgRvMCd+9PPmMr+31jvmc922E2n3PIbr/QqM5y9M6gvjeYpmk7gvSFF16wxi3t2YQQGc5idNz6+npbbVNa++zMHAyzWlkdOzyGKSM6OndKq0YVpJUSyOl0IhqN4tKlgTW5IV4PHDt2VN64ccPakMRiMVy+fIXvJ8MwZUeutDtq5GQ5Q33ctGmTFa2pUlNTg8nJyWUfI/s7GU3muz7j4+Pisccek29961stg6ga1SKEsF4Gf+EXfgH/8R//saw+qhQ7FV62kUD9WT3uXfGuKN1YFiRSkRBeaGZmZqyfs++DanADgKqqKltts6PV6mU+QSGfe0pR85FIBE6nE263G4Zh5BXh2dXVlZFGlIQbEoqojw6Hw7YhmCLmKIqeSktUVlYiEonA6/VhZmYG0WgUu3fvlmfPnl3yxHX16tWM+YZEOxWXy4WamhpbfV6M9vZ2GQ6H4XK55tRgJqHONE0rtS4AjI2NYePGjdazT6Ke2+22xNL29vaC9nMpNY4pTe7t27dttZ0dEbzS0R8HDx7McNRRhTsS44GlpWFWuXjxonVvgLvjnr6TATeftM3ZKf/LfX9FqBkzFnKYyYdr166JmpqaVbuoZWfUYEqLrusZTgiUlS0Sidhua//+/YuKYKlUCi6Xy/Y8A8By6iFnPaawFGJ+Up/pXO/KQHnP4bmEXHW+snuNlvIc0R4yl9PyQpCj32pyHmKYheBRzDBLpLG5SVZXV1uexqZpIhpjQXS109vTLTVNg8PhQGRmBh63G607duAfvv4o31eGYcqWXBFj9PNKHXc5f28YBmKxWE4P7ImJibwiSNWXStWgnf3/C3m5fvWrX8Xb3/52pNNpaJoGj8djGe8dDgfi8Tg0TcPP/MzP2O5fqch1z+YzINz9ufyMhiSO5hJUCsHt27cXrX9L18duHVKK6FsJ4/p8ht/VZgju7u6WsVgMw8PDJd+PqVG5qihpF0rVapqmlbp7ZmYmr3Ps6urKiDZXI8PV40kpM0S/pUBOIU6nExMTEwiFQgiHw3jxxRdfFXVj8Pl80HU9p5PLQly4cAHpdNoSKoHMtN/0WXV1Nfbu3SvPnDlTkPvf3d2NQCCQER1P4hFdO03TcPLkSetv+vv7ceDAAatvJOipYtO+ffsK0T2Lnp6eRX/H4XDg+eefx8jIiK1ro0brLGcc58trXvOajKiu7KwG1D+7kbEUlQzcjZLNdo6SUi6a2jkXFK2bHQFV7rhcLtD7LVBYB7qWlha52o3hq319LFfyXRdpLiYnh2g0isHBq7YHa09Pz6ICKTkOnDp1ylbbLS0tktaBxfaKTHlQSMeQlSbXOM7nPO7cuWPtuYDMaHl1H+B0OrF161ZbbadSKXi93qK9lzHMSrO6dzYMswLs7OqUlZWVcDgcVlojMm6EbBoFmPKhd0+PdAmH9XIeDodx48YN1NbWsjjKMEzZs5CxbiWiEO2Q3U8hBEzjbu2xbILBYF41SHOlU8r+/1yfq3zzm98UL774oqyurrYEDACW4Z76W11djQ9/+MPy7/7u7wpysYttqM6+NrnuCX1eroYE1cCbj4C+GLdv356ThjD7+PS5XSOCKgqUmnK9v9lMT09j69at8Hg80u/34/Tp0yvScYpiyXY6yXZEyed+0rxGWWei0SiuXrVvBL7//vtlZWXlnPS/qvBFz8r09DQuX75sq32PxwMpJSKRCDZu3IhoNIpIJIJQKIRnnz2Z0d/Ozk5bF+LatWvizp07srq62orAyb6WlE6+qakJZ86csdX3+Thy5Ih1rOz5UE1df/bsWevzs2fPWn2klOtqZKLD4UBHR0dB+kfs2rVr0d9xOp0YHh7Oq32K1FIj4lcKEn/peucSadPptG3h4sqVK+L27duyrq5uwfmtsrISDz74oHz88cdt1SG16wRQDlCEuhpRXiiBVNd1xOPxZbdTLqy0o8ByOXr0qJycnLTSvOazhtglO8vJYo53dqDAA+BubdDh4VHb53TkyBG5YcOGjP7kchijue/cuXO22vf7/TBNE263G1NTUwgEAna7yGCug2+h96S5RNHMsVm+e+BcaYizP3M47PV/cnLSipom5nuON27caKttqsfudDrzivhmmHKD3V4YZgH27t8ng8EgJicnraiReDyOM6dOi7Nn+sSJk6fKd4Vl5rCtfqts62iVu/f2Sp/PB7/fj1QqhcnJSbz88stoa2vD0888y/eUYZiyJzsCUE2nVgiv5kIbi7KFBTU1vcrrXvc6GYvF8o4gVY+nfql9WIzHHnvMqltG0M9q7ZePfvSjtvu4EMUy0qn1clRjUS7Bh4zn5YgqXBVD5COD40Jt0/9v3rzZVtvFvqarRfS0w/j4uJiamoLf74fX612x4+q6bhlsFxLK83lWz549K86ePStOnz4tzp07J/I1bL/zne+0RC41ElKNKCWuXLmC8fFxW8ehdgKBAG7evImZmRmkUqk54igAbNu2zXb/x8fHM/o8Hzt37rTd9nzce++91jVTnSzU47/yyis4deruu93Fixczfofqjqp/X1dXh46OjoJN3C0tLYv+jhDCdnrIhoYGSWNjOWO4p6dH5lMD+t5775VVVVUZNcGBzLqDQgjcuXMH586ds/1cXLhwYc5nqrMLOTcfPXrUVruDg1dFLqN1uUMpjLNrJxeqbbfbjQcffHB1XIwFWC33U+XJJ58UVD+4oqKi1N1Zdhabs2fPi76+c6Kv75zo778o8hFHAeDtb387qGbwYl9DQ0O4fPmyreNQqm6aq0KhUD7dZJgFWWzs2p3Hx8fHBTm0qHsgYO66UF1dbavtsbHrgvbL5fruyDB2YIGUYXLQvrND7juwX6pGZq/Xi3g8jsEBrkm5WqmpqUFNTQ1cLhdmZmZw69YtmKaJ0bHrYnDomnjsu9/ne8swzKog27ipiqTFFEuW07ZqQDFNE/F4PCMy413vepe8ceMGQqFQXiLvfFGSua7VQnz1q18FgIwafGQYAWZfMA3DwD333IP9+/cXxLqWS9AtFLkMo7kMWsU6fqEodt/UurfZonK2A4IapbAUaOwUW8icr/1yvaeLcenSJUFzWkdHh2xpaSn6iaTT6YyIlmyngnLgbW97W0bqUJqTaN5Uo8Z++tOf2m6fxnk8Hoff78fly1fE6dN9OQeX6jSyVM6fP2/1UU17TudD/+7u7rbd9nzs2LFjToRtdkTU1atXM/7m+vXrmJ6enpOmVMXlcqG3t7dg/VxKdLphGLbTJvt8PgDLd9ZwOp15RU7dc889VvpMFXq+6bpeunQpr36paXnnS6uYSCTwwAMP2G6b+k2i/mog2ymqkGuPYRhIJBKr5lrMx3IzApQSKWVJIhizr1O280Apr+Ob3vSmjH7M9+V0OnH8+HHb7VPkezqdRigU4gjSIlCIeSrXO0/2fFiuLOTYO9+6thSi0eicv8tORS+ltC2QArPOTalUCpqmoatrp2xpaZKHDh1YXRMqw7wKC6QMo9C8o0X27tktw+EwnE4n4vE4dF3H5YuXxDNPPS36ThemDg6zsuze2ysP3XtQapqGRCIBXdetyCW7Rk6GYZhyYj4DQKHaLjTUJtX3VA2m169fRzQataJ07FKoF9+nn35aUB06SqsIYI5xVwiB973vfQU5JlA8EStXBOl8nxWzH8tFNQ4Uo4+Tk5NLFg9qampstV0qw2Gu45a7gSib27dvI51Oo6KiAl6vF62trbK5ubloFzOdTkPX9QyBdLEU1SvJ+973PllfX59RH5kMXdl9jEaj+P73v2+r/ba2NgnMnrfP55sT5Z/NY489Jo4ePWrrgpw4ccL6mfqdHf2q63rB0tcePXpUVlZWZtTgomOpa012FOLY2JgYGxuz/q0KubQeSJlfbctcdHZ2ytra2kV/L5lM5oyYXAiPx5PRf/X7YlCk4MGDB6XqPGCHY8eOAUCGSKG2Q3178sknbbcNAM8+++ycaNHs89M0bUkpjLNJp9MZwv1qYL6MEIWYu3w+HzweD27cuIHDhw+X54ZhiSwn8rGUDA0NWVGkwGyE9lve8painsRC16jU1+9d73qXbG5uRjqdzvg81x7INE388Ic/tNV+Q0ODpHao3uJLL720vE6vU1bS6Ww1iKIq2U5YuX7OVyBd6G/puHbfbbL7VVVVhYqKCkgp8fa3v3V1TaoMAxZIGQbArDC6e+8eSdGFyWQS0WjUSqnLrE6aWhpld0+XdLlcMAwDUkokEgmcPXNOnD/bL8avvyD+/fEnVs+uiWEYZh6KGYFYKNT+xWIxaJqGmZkZAMDDDz8sb926hS1btuQ0bC6V+dIGzffzfPzTP/0TYrEYgLt1A+m6UnrgdDpdMIF0pe9brgjJckft61KulV0RLRaL5TQm53q27NakK9dncjUwPDwsXC6Xlea62Knt1HRhuTz2S30v/+t//a/WtSBnElW8IQNxLBbDrVu3YKfeIgCrnhSlEqysrFz0b5588klbxxgYGJhjyCbBkZ5xwzDySt+bi9bWViuVa64oUEqrfOXKlTl/e/36det3iOzUrZ2dnQXpZ01NzZKikpLJJPr6ckf0zke+tSjf/va3y5deegk9PT1yZmYGLpcLqVTKzqEBAL29vVZd71wCK/Wnr6/PdtsAMDg4aEV/ZwvYwKwoomkaqqurbYt66thcLcy3JyxEGsRkMgkpJeLx+KoSjeej1HN6viSTSbjdbjzwwANyenoao6OjJe1PKa/jJz7xCfj9/jnlOXLNdXfu3MGjjz5q62Gm1L3ArKPFxMQE5suqwJSW+d5rVsu7DpDZ10JkAkgkEkv6+3A4bLttSuWeTCaRTCYRi8UgpcS1a9fy6ivDlJLVv6NhmGXS1tYmfR4vfB4vpGHCKRw4f/acuNh/QZw90ycGLtmrT8CUB62trXLLpq2QBiCkA+mkjmQ8hVDA/sLPMAyzGNnpGBd6CbFrRDDN2fbJa1mlUCJXrhfJ7L7mW2PENE0YpgkIAeFwQALw+72IxSKoqakCAFy5chkbNtQikYjBNPW8zoXSXzmdTgghoGkaTNPM+Jyu42J89rOfFWQEpihSt9ttnY+u69A0DXV1dXjTm960bKuQem3V+ofZqYHzMUaqBiMyTqvRJaoYVIz0vkCmAHz33to7FyEkDCMNYGn30G7qz6tXr4ps0SbX8yWlREWFPYHu8uUrwu32QkoBKefWyl3u85srCiadTmekFCUnsdVo0I5GZ+DxaHA4ZscBGXuKQSqVypjn6NqRY0R2DcWV5Jd+6Zdkb28vAoEAKHrIMAxL/CIRzzRNBAIB/NVf/ZXtY/j9/lfXHCAeTyKV0hf/I5s89dRTIhqNWqmBKbMLXedkMgmPx4NwOIzOzs5lT0qvec1rkEqlkE6nM+ZRNSLIMAw899xzc/6WUhTT7xDqfFAogXT37t0wDMO6p+l0OuO4NFeQaGsPE6lUAsDdvYrT6Vy03vcrr7wMj0eDlAY0zZnX+rxjxw7Z2NgIXdettVjXdes+kKiZSqXQ39+fx7kB165dE8PDw9b50LpNx1C/261DOjk5Dbfbi2QyDbfbi4MH98uf/dmfKWtVTdd1TE5OWut89vq7nLV+eHhY+Hw+bNy4EePj43jooYfK9lrQ805zN/07FotZ83gqlSrqmlIspqYmYJo6XnrpBWzatAFOZ/FMVoZhWHO0Cs3Z2bWZV5K3ve1t8oEHHkAqlbLWxOy1Wo0q//GPf2z7GB6PBpfLAdPUMT0dydjHMUtDzRZRiH2v2q76fT7HxvlS15YTLtfdZwnChIRhfUHcrYW+Y8cOWydw584dALPnre4nVEevVCpl2/kTuJsm36U5YJhpBII+pPWkldafYVYTq+8NmWEKRENTo9y3b5+sqalBVVUVpqamEIvFMDU1VequMcuks7NT1tTUIBaLwel0wjAMnDt3Tly4cEGcPHmSd7QMwzAlhoxT09PTAGbTmyaTSUxOTlpCpF2W8rJt54X8Jz/5SUa6X4okBWZfCElA/cQnPpFXf1VIKMhVo42Zy1KMG/kYXiiV4mIsRaDNZr527UY3z9e2+pVOp+F2u63oQnreAoHAoilTyxFKeUvn19DQULRjLcVZoBSRCA0NDfKzn/2s1T+32w2XywUpJXRdt8Qnl8uFeDyOqampvAzBQggkk0kkEgls2rSpaONleHg4o9YrnUcymbT27qZpFuRet7a2wu12Q9M0y0BIRnRg9p7PzMxgfHx8zt8ODg5afVSdZOhvHQ4HampqUIj6uK2trRkODPT8ksMPCT0jIyN5H4MM1KlUao4zQC5u3bplCQsulwsej8d2dqXdu3fPOT7db/U+jI+P49q1a3k/WGfOnMmI9F1Kf5bC+Pi4oBq/hmEgHo/jhRdewM6d7XLfvj1laWknR65i1L+ur6+XVHcu34jiUkIOCMDsdfH7/RnlFFYL4+PPC9M0UVFRgVdeeQXNzc1FO5bqVLLQnFGKCL1Pf/rTSCaT0DTNWj+AWWenWCxmOZfSfv6rX/1qXsehdwA1Gp8pX+YTSlfiuEVD5JdhSXX+VPcYaltOp3PZ7zb8XDCrHRZImXVHY3OT3L13j2xqaoKu64hGo5icnMSlS5fEpUuXxJUrV3hmX8X09vZKn8+HeDwOKSV8Pt+qjJZgGIZZSVbam5bSCLpcLrz+9a+VFPUZCoXyekGzw1Jf4L761a9mRHbS35FXOl2zt7zlLWhoaFjWBaQoKkoHX0iWm5qpHCmWQJpKpZbUdr6e0dkpswqVQms+L3mKmFOjae1G1pYDyWQywyir1oUsNCSeLWRQK4VA+t3vftdKf6amISfRjPoFzKYD/N73vmc7DSvVH/V6vdA0DaOjo6iuri7cSSicP38eQGb9VIp2djgcluBrV8zKhVrLVBXA6XqZpokXX3wRY2Njc67X+fPnLUGQxD1yZKHvfr+/IPVSe3t7AdwVRimVO0HXxm4a2vb2VqlG7Pj9fni9XquW5EIIMVuLliJZI5EIhoeHbY2rBx980Oo/QYKvOm+dPXvW1nll8+STTy5p/3DffffZbpuinCkLRSKRQDAYLNtIJBIw1dTkQGH2ejQuU6kUfD5fhvPYaoHGPe29VisulwsnT54S/f0Xxb/8y2NFW5Ro/Cy0Ry3FuvgP//APsr29HZqmQdd1y2mIou/9fr8ljLpcLvz7v/87fvjDH9rqZFNTg/R4PFaE7Pnz58XVq1fXzoZ6jZFr71bukaNErneDQqBGyM/XttPpzMtBWX1HXkvvmcz6pLgWKIYpI7Y31MtwOGzVJ3jhhRdQVVFp1SthVjetra2yoqLCSq04PT2NjRs3YmZmBufOnePVmmEYpoxQX6gcDgfq6uowNTUFTdOQSCTgdnttt5ltZKfPsn9nqU4z3/nOd8QLL7wgt23bBrfbPefF0ePxWMb2d73rXfjTP/1T230myLs9VwqzQrJWXl6LKZAuhXwE0swxuXA0qd0xkJ3CmEQCtWYWCSQ///M/j+7ubqkei0Qfcgi4desW0um0FUlIBmRKpeX3++HxeOD3++Hz+azng6L0lpJ+9gtf+MKSbxAJ1+ScMDBQPGdGei8oRQrd+Xj66adlR0eHJZqpdRXpXtNcROkiP//5z9s+jtfrtc5b13VUVFQULTrs/PnzeP/7358RkUSRkoQQArt27VrWcQ4fPizD4bBlPKcxTNeQPr9w4ULOv7927Zp45ZVXZFNTk9UnuuYUVSiEwMGDB/G9731vWX1ta2uDlBKaplltq2K4YRjQNM22kOj3+zNE4UgkYgnri41zl8uFSCQCTdOs62eXe+65J0OQVsV94O48dfz4cdttq+RKkZyL7du3o729Xdpxik4kEgiHw0ilUvB6vYjFYkgkEmWbmpXmb9XGUai9hRrZHAwGV53AaJpmRkpYl8uFY8eOIRKJSCHEotnEFhPhcz0j2eUqcu1FhRA4e/YsTpw4seRxSVlYis1SMmCsNP/rf/0v+d73vteKlPZ4PJiZmYHb7YbH48nYt0xPTyMcDuN3f/d3bR8nGAxaDiKxWKzQp8GsAKvl3WcpGWXyESJp3C72d/kKpFZaYIZZ5bBAyqwLunt2Sa931tgajUahaRqGrgzyLL5G2LVrl6QXfTKwDw0NiaGhoVJ3jWEYZlWRHVlTLOLxOCorKxGJRHDnzh2EQiFLmKD12i65jFK5zsPOuf3TP/0Tfuu3fgvAXSGJjOtU4yiZTOLDH/7wsgVSXdeRTqdXZaq3ciRfgXQpf5fvGC0Wqne8anglER+YFYLcbjc+9alPzRljqkhK2Tfoc4rkI3FSNY4TqqCoRjTOx8zMDP7t3/5NDg0NLekmjY1dFxs2bJBUM/Hw4cPymWeeKcokRZHcat0+oDSRB3v37pXf/va3sX379ozPs8co3TuPx4NEIoFHH30UTz/9tO3rQ4Ic1ejbtm0brl69uryTmIdLly5ZIiBwV2xUa8gBWHbayO7ubgCYM27pOwmkJ06cmLeNgYEBUOYhVRwhQ6VhGLjnnnuW1c/m5mZZV1eXcT1cLleGGOt0OpFIJDAwMGCrbUo9SefscrksEXZmZmbBv62qqsLNmzehadqciFYb52Ydl1DnKor2OnnypO22Vc6ePStu3rwp6+rqFv3dw4cP48qVK0tuOx6Po6KiAsBsRLuU0nISAa7l2+WiQRGkxRBI6d65XC54vd6yj8rKhsQuVfB/3etehyNHjsDpdC66B7PjJJDr2qgpfinLA6U2/9a3vrXgXJRNPs9jPtB5uN3ueQMLVnIc/PjHP5avec1rrPN3Op1IJpMIhWbrw1M9UtM0EYvFEA6H8dd//dewu2+or98mqWayrusYHOTI0XJnIWERAIo9TJfzHGRGkBauo7kceXL10+VyoaGhQebKpjEftGdkmLUAj2RmTdPW0S4PHDooA4GANXk7nU4rtR+z+unq6pJUUJyMf8VOz8gwDLMWWUnjhs/nQyqVgtvtRjgcxgsvvAAhBKqrqxcVV+Yjl1dtrhdlOy9yf/d3f2elV1S9/qlGHokSHR0dOHz4cN4XMDuCr9AUos5loVlOP5byt/m0v9RUgfmkqV2K40G+z+BC6WDJUOh2u5FOp+HxeDLEThrPmqZZaTfT6bQljGqaZu2t6NkhYyGNVzKWk4hCz8Z8X8FgMK/IxOwI8WKwUATpSs6Rv/d7vyefe+45bN++fY5BmvpGc6Wu69aYfPHFF/G5z33O9vHa29slAEs027BhAyKRCA4cOLCs85iP4eFhqz6c2+2eEz1P93rLli3LOs7evXuh63pG5Gh2qlcAC6atfe655+aksVajdw3DQFdX17L62dramnEN6Hj0jFJ07YsvvojBQXtOvtmpvCm6cDZd7uiibbndbuvvzp+/YOvYDzzwgAwEAhlRz5SqmMaxpmmYmprCU089tezF6dKlS4v+TjqdttL+LpXR0VFB/U2lUgiFQjAMY8UEKruoqcKBuxHmhVj/VXF7JRzqCo26V6R68g6Hw8qMkL0+Zn/lqm2oftHYVtN5q58Bd+tq03pDa+hi0avZrFTKfFVsL2WK3Y997GPy9u3b8vDhwwBmryOl66Y9heqsJaVEKBTC9evX8fGPf9x25yorKwEgw5mHKW8Wcowt57mqoWG7nK/0hko+z9lC+201i4fD4bDtpKtmr1ltzjIMkw3P8syapKmlWe7dv0/6fD5rczozM4NkMgld1xf1lmXKn4amRtnb2ysDgcCr6RjdSCQSS/L8ZBiGYTJZ6ZcaSnml6zqEELhyZUiQALOclHWFFs6uXLkifvSjHwGAFTFKkLGI9hkPP/ywzd7ehUQqMsAVktVgGLBLsQRSGo+Lka+IDyBDpMlVN3S53ucEjU91T2SaJhKJRIZYmW3IJVEme7wTJJqq6Urpc2qLIj3n+3rppZdsX0O1jy+99FI+l2dJqNGjC92LYjxPe/bskb/zO78jr1+/Ln//939/TjQlRa7lqosMAC+88AK+8IUvYGBgwHbnSBiQUmLr1q2YmZnBli1b8K1vfasoE8fQ0JC4fft2RgQQGf5UEaK2thZ79uzJ+6HYt2+fdZ3U66aKzBMTE/jpT38673k+99xzliFeRb3+dXV16O3tzbufVH+U+uVyuTKeLbrH86UCXggSJekaX7x4WZw/f0EsJRrq+PETQp0X7HL48OGMZ4WeKRrT9O98zisXzzzzzKK/43A4sG/fPttt0/pA818ymVyxFKd2yXa2KmR2ELpnhmEgGo0uu72VJjslMDn1ALOpKFUxM9eXGuWV6yuXqEprH9XCJPGaHJDIicludopZsf+Y3Lt3t3z9619btI389PQ0JiYmEI1G52RXIGiOKQaf/OQn5dWrV+Xf/M3fwOfzIRAIwOl0Ws6W0WgUoVAI8Xjcsgul02mk02kkEgl84hOfyOu4dD+klHmlH2VWnlyZP1ZDDdJscTQX/z977x0dyXGeez89PT09EXmxGTktgM05kBKDSYkyFSzRCjQVaF1dyZbsa/k4HN0jf9Z1kHxlHwdJV1akTImkGBRIKlmiRHK53OXmxS6wGRuw3OVGxMndPfX9Ab2FmsEAmG4MgAFQPx4cAlhMdXV3dXXVG57X6RyeTQY927GcOEgz25BIZjMyzUoyp6itr2MlJSV8oavrOmKxGFwul5TUnUPUNzawiooKGIkkN6aXlJSgq6tL3mOJRCKZBSQSCZSVlSESieD69eu4++47WTwex40bN34rWWefXDdodjdyTz/9NN761rdC1/U0IyMZS6LRKPx+Px544AF88pOftN1vANx4JkoqSsZmqhykuZJPJ7Zo8JiMATvT2CE6KhOJBDRNGzPbJNNoREbOTENotjpDmdHsEzkWAaCiosK2IVWUAV6wYAEuXLhg6/O5ks05mu2c8jG+2tra2KJFi7Bp0ya89a1vxZo1a3hmGjA8T9I9o+xfYHQNPHK67927F1/84hdtd6yuro6XqrAsC7/+9a8VAOjo6JjcCU7AzZs3UVpayh0IovOMJGF9Ph8WLVrk+BgNDQ1cBjeVSnHjH11PxhiuXLkybhvnz5/nDlJyWGcaFVVVRVVVleNr1tjYyNsiCXfREUXHtFs+pKamionqB04MxAsWLIBpmo6Cl9ra2tLGq6qqXD2CJOp1XbddV3UsDhw4MOHfqKqKpUuX2m6b7oPH48HNmzcRCoWgKArOnj3npKtTTjZHQb7ei5T9aBgGlzWdLdB4TCQSo7LXc1l/TvQMZQsIyITGEvUlFotB1/Ux5WvHO9alS5dQXl6OgYEBvPnNt7OXXho72MMp5CCltbsYwCGSj7VRY2MjW7hwIdasWYO3ve1tuP322+Hz+fj8QyoX5JCl+ueWZfHyAPF4HIqiwOv14u/+7u/wk5/8xPY1aW5uZJqmwTCMgqpJLhmfzGdu9DNYmObC8aSBJ7v+zDZ+x8pOncwzPBsVBSQSEWmBkcwZVq9ezTRNG5YsYQBLpWCkkmBWCh0dR+VMPUdoW9nOioqKhiM8zeEaHoFAYELjhkQikUwlmqZlrcORzUCVb+eKiNNswcwI+GxtZHOO2GlbbIcMo+QkuXXrFoBh45RTmS7RiSGeQ6YDym507OOPP678y7/8CystLYWu62nZR9TXVCqFBQsW4IEHHmBPP/207c6rqpqWwTWWVJET47Y4LslQPhYj18n2YcYl85ipFGzfB9q4D1+bqVnWZYuyzuwDMHwf6urq2Llz53LuCI2TbNc/8746McaJnyGDL40lTdPSHKe5Pl9jzVWZGYzi97m07cTRImbWTXSfJgONM3JIkfwhXQu6pkuXLsWuXbtYf38/N9KmUikkEglu9M6WUaQoChYsWICioiKUlpZyg7x4X6jmmehYcrvdo2QyRQfToUOH8O53v9vRg1FZWYlIJAK32z2tGRYvvPACWltb05x3ooOQHKVbt27Fz3/+c9vtb9iwgZE8LElaksFb13V+7SbKOuzq6lLeeOMNVlVVxe8JOfnEDN+7774bzz//vP0LAWDjxo1pbQHpdUjpeONJAWejtLQUjDEuEenk3drf349oNOqo9vKmTZv4sQ3D4I5RYPgaUp3knTt32m47Gz/84Q+VSCTCZX0pUISuH70PQ6EQ3va2t7Gf/vSnOV+QwcFBLFiwAIYx3FauigMzgbg2AUbmD7HWLzASSGMHegY0TUNfXx/WrFmT177nE7oGNLfQNaBsTkVR+NgAwOvhZrYxXvsTHT/bnEqZpOLPmb/LhXg8jrKyMiiKgkgkgt7eXlufz5WLFy9i2bJlWLhwIXcsA+AqBy6XC6ZpoqWlBc899xwDRtbkmetyUU2CnJx+vx9FRUUIBAJYvHgx/3txzhKdn16vl79r6X7Se1PM9nz22WfxN3/zN44e0tLSUgwODkLTNPj9fkSjUcfXTzKCqKKSD8Ts0My1WuZxh3+fl8NmtDnSqFNneqYiC4TzEqE1qR2yrSnF/TFdMyp54BRx/17I2boSyVhIB6lk1lNXV8dKS0sBDEcCer1ePuHv37+/MHctEke0trcxktIFRgxD4XAYdoqJSyQSiWRmyZQpA0Y2U8MGTGftjmWMmizf/va38ZnPfIYb18ghbpomfD4fN7T/j//xP/D000876nfm9ZBMDru1vIDcZQgL8R5l9snj8YAxlmb8BUYy1LJ9JlcmCvKYyEBENUydYjfLxg5kpBKdm9nk2RYvXoyFCxfyz9iBDLuZjgnxvqRSqTSjPc01mX+vaRp6enqwfv16RzezsbGRDQ0NIRAIwDAMR04wp3R3dyMWi6VlUgHphnFd11FfX++o/fb2dmialiYbTc5CmsMty8qpbuWJEydQU1MDAPzeZAZFURaoEyiTltqn/hJutxuGYdjOnKbr6NQ5KvYvHA7b+kxDQwNbsGABPw+6ZuSIp/4kEgl0d3c77lsmohIF3XMKyKH7r6oqVq1ahZ/+9Kc5t3v+/HmlvLx8Vlh9x1pf5QMxUIMcr7OJTHnnzGCiidYATgIQRTLr8JK8rtinXPF4PEgmkzz4oKamZkoymklWmq4VOdbpWonX7v777087l8zrmZmFL+4H6N+BsYOuvF4vBgYGUFxcDGDYYRoIBBAOhxEMBnlt4D179uCd73yno0mvrq6GWZbFpXolkulkeMznb84uxD2LRFKIyBqkkllLTV0t2759O6NINlq0DQ0Noa+vb5QRQTK7Wbl6FSMJHzISdXR0KB0dHUp3d7d860skkhlFbj7sIRqkxK98RhXnk+9+97sAkCbRSIZWYNiBkUwmcffdd6OhocH2CYgG7PkwlqbjFKe6XlQh3afMTM5EIsG/F50sAwMDSCaTE44zsUZpti+xHlu2f89Wg038Kioqwvnz521dQHFuyHedXpFsWT2ZgRfJZJI73agvmbVLRUlgcgjTl67ro5yjmfWHxdqwmqZx4zP9bmhoCKZp4uTJk6iurnY8GIuLi3mGMd2/6eLYsWOIRqPcAE1za2aWc1tbm6P2t2/fzjObyBGRTCbTslUNw8C+ffsmbGvXrl38c+TcpwxCGhurVq1y1M81a9awBQsWjHJmiRmALpcLfX19ePXVV23d68zsZSeQY/TEiVO2GlizZg2Ki4vTrhnda8Mw+Ji+evUqDh06lLcJtaurC7FYjP8sOqToZwB405veZLttcd1S6Ij9zMw+nwziHKeq6qTqxk83jDHE4/G0LENRGSTTaZo5n2dzBmf+XeZ8T18kSUzHFhVDnN4Xep+HQiGYpmlbgjtXioqKEAqF+Lsrm3oEgFGBLpn/ni1YQ5xXTdNMq9cqrovFNUAoFOL3IxAIAACCwSAikQhUVcWuXbtwxx13OB7sZWVlMAwjrS9TVV9VIgHGl9id7Dsn189LiVzJfEc6SCWzkg2bNrLKykrcvHmTGxqSySQ6OzuVkydPKmfOnFF27sx//QXJ9FNTV8tWrl7FSAoLAJcwk0gkEsnsZKzN2mQ2gXYkPu1y8uRJ5Te/+c0oGSWv18szYoBhY9WDDz5ou32ZPZp/KLvPDrk66AvtPmWOH6plJhp0GWMoLi4e5Tgmw69owBUl8LJ9iU6XbP8+lmGZvmKxGJqbm20HEohG+alC07RREm2i40pRhusPZxpsRclGMftUrC1MX6JjOdt4sywLXq83zZBMMqvklAuFQvjhD3+ItrY2x4Oxra2NJZNJ7qgKhUI4ePDgtA3ugwcPKn19fdx5liljTNemrq7OUftr167l7YmOD1VVucM0HA5j9+7dE54z1bbMNv6o/aVLl2LFihW2x3VVVRV8Pl/WAABxfFy+fNlu06Oco3YDkN70ptvYsmXLxqxfPB47duwAAD63EFQ/kL7v6uqy3fZ4vPbaa2n9Fa8nPZeWZWHdunW2257OAILJIt7rXNURckEMGnG5XIhEIpNucyrJdNCRQxQYyc4HwOfjsRQ9MjNyxYAV8f2WOd/TFwW6iO2Ic0kikcipBqqI3++HruuIRqPQNA3Nzc3OLtIEVFRUoKysDKFQiNdtJTLHmUi2a0kO0cygJ7pOuUDzN30+mUwCAAKBAL74xS/i7rvvdjzQ6+trGc0RlIRBZbwkkqmgqmoZy3xOxtojOyGXNUDmMymRzEekxK5kVtG2sp0FAgHE43FomsYL0luWZXtBKZkdlJaWwu1288jOoqKiKa8/JZFIJE4oNKdJISMawsXNWCE7Cb/2ta/hzW9+c1qWAQAu16iqKvr6+vDggw/ic5/7nK22p0oKbz7jJJDKTpR1oZFpVDFNk8viiXXVfvGLX+DWrVtZnZh0XhM5AcR/z2YcFZ2y9H/xWX/jjTdw6pS9bDTx+ZhKJ4Xf7+dO0rEMRmKmaKahOLOOZGb/6fvxsmDp82SgpT0OOX1u3LiBj33sY/jxj3/seLJsaWlhFRUVGBgYwODgIEKhEILBoNPmHHPhwgUsXrw4zSlK50/XLBQKYe3atezw4cO2znf58uVwuVzQNI3X7RKDWTRNy1my9ty5c9w5raoqDyQA0rMuVqxYgRMnTtjpJpcQppqjlM1LAQd0jFOnTtlqt7a2etREZXfuGhoaQiwWc/TMbd++nWeUi3UrqRYayXTaras6Ea+99hoP1KBMVXEOpEziBQsWYMOGDezAgQM5j6tkMgm/f0SGuhDfBQDGzHbMB+QYVVWVS3MXKtkcdDR/k+MLAM6fP48jR45AUZS07GMgew1A8feZ/5/IRiFKyIqOVcaY7WchHA7D6/WiqKgIV69exeHDHVOygC4qKkIwGOROzPHWqxM9E3QvxKzmXNZe9O9Ug1Ssi+x2u9Hb24s/+ZM/wWOPPeb4GlRVVbFFixZxm9PQ0BASiQR/F0skU8FYCg/52g/nqiAx1etriaTQkQ5Syaxh/cYNLJVKwTAM+Hw+vknt6uoqTEuqZFIsq1rOFi1aBE3TkEgkkEgk0HWsU95riURSkBSqU69QyZSszPy3QuSpp55SvvSlL7Hi4mKoqsqzrsgBpaoqQqEQAoEA3vKWt7Bf/OIXOZ/IRA6TuYaiTP193rt3r+0D2HGQFpJhXJSgA0ac9pkZeX19ffjCF76Al19+uTAfsnEgqVSSSJwqSkpKuJMUyG7szXxWycBNzjPxXmT+P9sYE2VUyXA8MDCA0tJS+P1+XuP45s2b+L//9//ii1/84qTvn9/vx40bN+D1em07q/PJ8ePHsWXLlrTfic5Scsa0tLTYch68+c1vZoFAIM15SfeI7p+qqjh06FBO7Z07d07p6elh1dXVAIadIGScF6U1N27ciB/+8Ic59xMYdqpSO2NhWZZt54mYleoUTdPg8XgQjUZtf7a+vp4rPVFGlqIo/NmKxWLQdT3vDtIXXnhBGRgYYEVFRfzZooCRTPnmTZs28ezgXIhEIggEfKOynAsNylIXnaT56iu16fF4prVmsROyZWWRtDYRjUbx7W9/G3//938/696LRUVFYIxhcHAQCxYswOrVq9kPf+g8cGYsaD1BY56e5cysZFE2GBi9zhPHYLY171iZzuLPFCjk9/v53z/++OP47Gc/iwsXLkzq3CsqKjA0NIR4PA6/349wOIxz5ybXpkQyEdmzR/Nbg3SivQ09S9JBKpnPzB9LjGTW0ryihW3cvImRRJBhGBgaGoLL5bJdh0UyeygvL0csFkMkEgFjrOA3YBKJRCLJnUxnwWzJoHzqqae4lCaAtHqO5CxNpVL40Ic+ZKtdMjxNpk6cZDS1tbW2BlOuTuqpzM5xQuZzRGM0mUymGTtKS0vR19c3gz11BsmPAeBZYVNFaWkpQqEQl7jNBjkeqL4oZcmJtdlEmd3MOpCZsoyZc57L5UJpaSmXDYxEIvj85z+PDRs25MU5unXrVkbZQKWlpZNtblJ0dnaOepayZSCvXLnSVrtbtmwZVdNUlDSmLKhXX3015zaPHz/OHaGUCZnpgNq4caOtfgJAS0sLgPT5J5sTPldnLkGOhMlIq/b29iISieDoUXtBqvfddx8LBoNp152uF90PRVEwNDSEjo4OR30bD7pXmdncogxqKpXCm9/8ZlvtXrx4cVa8oEk2ncZnvtdXokRsIdcgFddWQLqjja6N3++ftfUl6V0YCoXAGMOlS5em5DiZwQDiGj5Twnq8jFBFUfg7M3Msim2OB61pIpEI9uzZg3e/+9146KGHlMk6R9vb2xllyJJiWU1NzWSalEhyYrwxn2uGdS7tjzf/i4FeEsl8RTpIJQXN2vXrWEVFBVRVRTgchmmaKC8vh6qq2L9336zYoEjs07aynfn9/rQNSyEbzCUSiUREzlcTI0priY4Dcho4YToci1//+tcBjBjWSMaPskkty4LH48H9999vq13ROSodpPkj13pWRK7XvtAySDMhx5ppmlBVFR6PhxtSZ6MheDoDBwKBQJrMLpBuXCKnCz2rJNNIGXLxeHxUzVXREZqZXUOOVcq8yTQcHzhwAOXl5cpnPvMZJR/OmaamJmYYRlpW6kxy/vx5GIbBrxFlJgHp9TNJhjZXWltb4fF40rJF6Xs6RiqVQmdnZ85tXrx4EQB4XTrTNNOMiaqqora21lY/AWDJkiX880B2hyZjjB8/VzLnPyfvl5KSEpSUlNj6DAC0t7dzpQXK/Kbjk4Spx+NBX18fzp07l/eH+/Tp0wDA75MoW20YBn9e29vbbbc9G97RFy9eVDIlTPMFXUuqLesku3g6yZQ3FxUWyLlLwRSzDep3JBJBIpGYsvMQHc30BWR/Fqi+tvjOE6X83W53WjCR+F4c60v8O7fbjTfeeAMf/vCHsX37duVHP/pRXt6LRUVFvA672+1GNBrFokWLJtu0RJIT42VNc5gz942d+V9mkErmM9JB6pCGhobCtYrMAdra2timTZtYPBqDx61haGAQfq8PKdPCyy++pBzrOFr4OxOJIzZv3sw01Y3w4BBSpgWkGBKxOBT5xEkkkgImM9sj20ZnMlGgY2VYim1NxmkzlQa/bOec6RAlQ0pmFpYdcpHszZTXs8uxY8eUV155BQDS6qilUimesQcMO1k+9alP2c5eFCVRyYCrqip3yE7XxnUseTS7nx+LYSeT8/uQ6zg/c+aMrYNQCYeJMM2U7Uwiw0hAURgYGzEciogBA3YR5UgZY/B4PGCM8dqVwLCjgAzDsw2fLwDDsJBKAR6PFx6PZ8qOVVRUxKX7gGFnADktaW4iB5t4r6gMiNfrHdfICwzfr3g8zn9nmiafO8QgEU3TsHDhwrydW1NTA6uoKINhGHyOmenxsG/fPoUkyqlWpSgVTWPariPrjrvuhGGZSJoGXKoKwzLBFED97Zztcrlw8+ZNHDp0KOfneOfOnXC5XPB4PGnPkpgRvHjxYjQ3N+c899fV1TEywovZfnQNaFycP3/e9nwGjLxrRaewHRhTEA7bd4Ddc889/Nji/bUsC4FAANFoFKqqYvfu3bbbzoXf/OY3/HvTNPmzKgY+aJqGqqoqNDU12XpXx2IxuN1ufj6FSFVVFUsmk/y9QvVe8xHYI0qCx2Kxgg66EbOWxexlgt4lhRzwNB6qqsHlckNRVLhcbkyVsAXNxaJyQqZMMQDuXBTX+NlkdWOxGP88lc0isu1laB6kv/P5fDhy5Ehezq2mpooVFQUBpKAojAdVeL1ePP7496XNMQ9klhrIJ9nGTuY+OfNv8nHMzDXdZPaW9Oy6XCNBTSylDDtExS+MlJuwgxjkJMroZipgWJaFs2fP2j4R0zThVj1IWUjrq0Qy25A1SCUFRXNzM6uoqICiKBgcHEQgEEBvby8sy8LBgwflAmWO09raymgj4/f7qd6OvO8SiUQiKRi++93v4o477uAGGzKSkuEcGN4svve978WXvvSlnNokZws5LSbroJQA69evZ729vTh//nzOF5EyGybCqQRVpkxdNmaroXYqycxAmUqnHhl36biZmd3kKL106RL+/d//HZFIhBuF6e8nuodFRUX4//6//w/RaJRL+YrOQFVVEYvF4PP5sGDBAtx///3s+eefn9Rk0NzcyEpLSxGPx1FcXIxIJDJKgnmmuHXrFgzD4Pc1s3YuAFtZjMurq1goFIJbdYP9to6XGIBCwQJ25SjPnDkDwzD4c5zNCaCqKlasWIFTp07l1OaSJUvGPW8aV6+//rqtvlJ7okPISYBQMpl0JKFaV1fHv/f7/dxBB6Q/z11dXbbbzoUTJ04gHo9D0zT+TNNzSpm1pmnC7/ejpaWFZ5zmAu1V4/E4AoEAVqxoZpqm2ZYhnkp6enryW8ROgAz0Xq8X0WgUgUBgKg4jmYDm5mYmBh5MdaCjON9RPV/LsvjzlUql8Prrr+Nv//Zvszo56T1Kzx0F/9HzWFlZiT//8z9HIBBICx4CRhzZlP1dUlKCd73rXfjiF784qfNqaKhj5eXlAEZKZmiaBl3Xcfny5Um1LZHkit11v92/p3dv5t6Snmf6eTLrwal0gksk04V0kE6C1tZWdvz48YJZCM9mamtrWUlJCTweDyKRCEzThMfjgaqqOHDggLzG84DVq1ezoqIihMNhvniWhmGJRCKR5It8bdoeeeQR5R/+4R9YWVkZz/AUMzTIYLRp0yZs376d5VIvnYz3mfKbudZkmm/kci+XLl1qO7jO4/HkdK3JOW6HzGhzSe643W7ous6z0KayRlIwGITX603LthKDFugZvXHjBv7lX/7F8Y1861vfyrZv384dg9nmAADwer344Ac/iOeff97xOTU21rOioiKoqgpVVXHjxg2cOHGiYAZhT08P1q9fz3/OFiBSUVGBjRs3sv3790/Y75UrV8Lr9QIYuV/cwc5ScGHYUG+39uXhw4eV3t5eVl5eniYLKWb9ulwubNu2DT/+8Y9zarOxsZE7F0Qo65PG4bFjx2z1tb6+lpGqgejAsDv3iPLcuXLbbbcxkhoW1RVob0/tmqaJ1157zVbbubJ//37l1q1brLKyktdiJec2kUgkEAgEsH37djz33HM5t33ixCll27YtLJFIIJVKwefzwev1or6+lnV35x6QM5sRa/HalbKfCeai0d7v93NHNa1dpqoerBgoJAaH0NygaRqSySQuX76MRx991PEzsGDBAvbHf/zHo44LjAQskWT2Qw89NCkH6YoVzSwYDKYdizGGSCQCAJgvz7KkMBgJKph42Nmdz8Sgwsy9CO1dxefZDtlUb/It6y6RTBcy99khyWRSFjDOAzU1NWzTpk1s4cKFvFYBRZQNDg5iwYIFM91FyTTQ1tbGFEVBIpFAIpHgm2Z5/yUSiUQyGaYqovX73/8+j6AH0uvbEaqq4sMf/nBO7WXWdpLOtMnT29tr+zO5Src6MULmUl9oLhpxJwvVzDQMA5ZlcXnLqWLhwoUoLi6GrutpQQrivRGz4Zzyta99jUshUwY6kC4hSNH8b33rWx0fZ9WqVay0tJTXR1UUhTuMCoWTJ09yA142OcZUKgWPx4OGhoac2lu9enVancQUS/G5mhw7brfbkXPu1KlT3GlJe1dql/q+adOmnNtraWnhzlBqM1NOX1EUHDp0yFY/g8Egb5faIIfu5s2b2Tvf+c6cJhuXy2Xbmb5582be/2xZZMCwwXZoaAgvvvjilL3gTp06lTZXZF5XGiPbt2+33TYFbdBzFYvFCu65ArJLTk4WyvqLRqNc8alQEc97Lr1fm5qaWCgUgmVZiEajU15Llda4mfVHxRrPqqo6Ch4TeeaZZ/j39F6k55akujVNg2EYWLlyJbZt2+bopra3t7IFCxbwPpNd1+VyobPzuLJ378SBOBJJvrAzNzmZz3Vdz7qWzdxrJhIJW+1mtpHL7yWSQkY6SB3S09OjnD59WmlsbJw7K61pZFnVUrZydTtbsGAB4vE4BgYG4PF44Ha7cfnyZXR0dChnz55VfvKTn8iZdQ7T2trKtm3bxoqLi8EYQ19fH44fP67s379f6ejoUH7+85/L+y+RSCRzhMnUYJ0M+TYOAsMyu2INUoLkv5LJJFwuF373d383p/boumTK68oNpnOuX79u+zO5OkhjsZjttrNlxUkn6cQEAgF4PB5omoZgMMgDKaeK5cuXo6ysDD6fD8BoB0Pmc+qUJ598Urlw4QIPtKB5gzHGjcxk9A4EAvjzP/9z2wOjpaWFeb1enjlIGUZi1kwh0NXVlZaJKdbiBEZkFltaWnJqb926dVBd6ZJyLpcLLiX9vh08eNB2X1977bVRNbSpffpdU1NTzu01NTXxeyPW36a6mXSM48eP2+onOe8I8R2zd+9epbu7Gw888MCEY8rJOH/Tm97Ex5woawyMOKgBoLu723bbdtizZw+fKygbFxg5J8oybmtrs912IpHgNdO9Xi/PcC8kpupdIs6JpJgxW5gL79fm5uHMR8qI9nq9KC8vh67rCIfDU3JMMXiPZL9JDp7QNI0/U0556aWXFJInzwxoEBUd6PsPfOADto/R1raCaZqGcDjMM3CBEYldiSQbUzl32A1ksdsXn8836jnKbINqSttlrOdUPkuS2Yh0kE6SyUZJzUcamupZWVkZl68ieZZgMIhkMomLFy/K2XQe0NTSyHRdx61btxCJRODxeFBaWjrT3ZJIJBLJHELccOZzc3v48GGlo6ODyxMRJA9PtZUWLVqEj370ozkZoWXW6MxSU1PDdF0fJXWZDSdGyPHu71ww2uab5uZm1t7eznw+Hy/BQZkyTpzfuVBXV8cWLVrEM0iBse9NPvaAzz//PFRV5fKImRkzlA1kWRY++MEP2m4/Ho9zWUKxXpwTI9hU0t3dzbMbRWljgq5Hro4s8e8yM/sp++3mzZs4dOiQ7Yn21Vdf5fO82LaqqtxRVFpais2bN+f0UFdXV3MnaGatXervtWvXbPeV9tiifC85cN/5zncywzByqh3pRDFr/fr1/Pmgd6RYZ5fu54EDB2y3bYe9e/emGWtJAl90whuGgZKSEtuZaIODg/x7esaySQ3ONFMRIKaqKgzDgN/vx+DgIIqKivLafr6ZS+/X+vp6tmDBArjdbu6kD4fDCIfDuHr16pRJp2cG72U6Qeh5yoez/Nlnn+XvwlQqxWU/6Z1MigKWZeH++++33X4kEuHBDaZp8uCJXOqHSyRTgV3nqN1xSu968f2bGYgGONvbZNZOlw5SyWym8FZxswzpzMudmrpqtmbdalZWVgZN07icqmVZOHr0qPLSSy8pdmtFSWYnzSuaWFFRETweD7xeL4LBIKLR6JRFPUokEolkfjMVTtJHHnmE1yoi5wNtCFVV5VJFDz/8sKO+AnKDORa5XJdcjP8iPp8vZ4lEu+uV6urlbCKJXWmYG6G9vZ1VVFRA13UYhoF4PI7jx48rR44cUTo7O5ULFy5MmRE4EAjwIE7x92l1LB3UcszG97//fQwNDaXNG5ShA4xkNBuGgba2Nrz1rW+1NUguXLigkFNUdJDafTammkOHDim3bt0CgLQABbrelGWbSwZpbX0dW7Zs2fDnwbI+Z7+VjXXU1xMnTmBoaIhfS7FuLI0Rj8eDdevW5dTekiVLAIy8Qygrj4z3iqI4yrQkhyQF64gO16NHj2LRokX4zne+M+EgFh2BubB+/XpWWVmZ1VFCTmX6t927d9tq2y4nTpwAjaux5lv6/+23326z7VMKMHw9Y7HYqPqmhYB4jvl8v9B9pLqTUyXrmk/mwvt1xYoVrLKyEjdv3uQlidxuNzo6OpRDhw4p3d3dUzYAM4P4sjlaAGcBFZk89dRT6O/vB4BRgQfiWLYsC1VVVfjQhz5k873YozDG+HuQMkezBedIJNOB3T2q3XHq9/tHqdhkC3KYbPCn+LtCex9KJLkgHaSSaWHVmpWssrKSp+4zxkB1E06dOiVnz3nEqjUrWWlpKRhjMAwDpaWlCAaD6OrqUo4fPy7HgkQikUhmBV/96lcVMuJQlgw5IlKpFJftWrduHTZt2pTzblY0PEmcY1fqTdO0NInL8bCbgZfr/Zzvxrn29na2efNm5na7YZomEokEUqkUl7udDsjpBYw2IInG2clKCQLA7t27latXr3Ip3czjAuASgKqq4t3vfrftY8RiMSSTSS5TTFmlhQZdB6qTCmBUbcPFixdP2E5VVRWCgWEJYQUKXMqwtC45vN1uNyzLwrlz5xz1s7u7WyFpRjIC0pxP48PlcqG5uXnCtpqbm1lZWRmfd0T1ATED9PLly476SpmbYkaqZVnYsGEDXnrppXEnpbVr17I3v/nNTmSd05ygouNQdHQ4kQ22y7lz55SrV6+mZaFRP2g8kHNv7dq1ttun66tpGq8nXGhMVZ8og8/tdqdd20KmEO9PLrS1tbH169czr9eLcDiMoqIiMMamNbBcXMNkzss05zWbsDoAAJs1SURBVAHIy1g4ePCgcv36dd5W5hgTFegA4MEHH7R9jKGhIQwNDfGAoVQqhUQiIdfekhnDzvzU09Nja6Dquj5hUCZjLK81SCWS2cjsKRggmZWsW7eGR8wn48MyIG6XikMHDsuZdJ7R1NLI/H4/qOaD2+2Gx61PuEGXSCSS2YAYLUlZJOLvRKeXXRm2zDayGeuzHX8yZHPSiUZWu0y0gRpdu8R+/yeqr2K3xksu/OhHP8If/dEfwTRNpFIpnvGmaRrPKNV1HR/96Eexb9++Cdsjw7jY58lG4orRwaLBXPy30bC8jKPx+uQ002C4TxNfj2vXrtlqd9GiRQCG+0b3EACvNWtZFizLgsfjse2w0HWd16UVMxEzI7rzcZ8LiZqaGkbSdZnjmCRJxXMnp4rL5YKu6+jt7YXX68WBAwem5eRIglPsFzA6q9vlcuUlUwYAvva1r+Gf//mfwRjjtd3EmqH0vkilUnj3u9+Nj370o7baP3HihLJt2xZGTliPx+N4Hp9K/vu//xsLFy7kksPkxBKfx7KyMmzYsIGNNx62bt3Kv0+xdAk5yih1qS7s3bvXcV+PHDmCt7/97Xxc0H2jjM1EIpHWj7FYsWJF2vmJEsuWZfE56KWXXrLVv4aGOjY4OAiPxwPDMPi7iGoGPvXUU+M+Tzt27GDAsCPkzJkztp69bdu2pT07Y2UX3rp1y5HEsV1efPFFtLS08GxssWYsMDw2EokENm7caLttwzDg8XgQCAQwNDRUcJnZNH+I55ovTNNEX18fQqEQnnvuucJ7+fwWsa6xKAGZj7Xg2rVrWSQSSVsraJrGn19RVQQADxxIpVKIx+O8TrBpmvD5fKNk1jVNg6ZpPFjI5XLB5/MhkUggGAxCURRUVFTY7veWLVvYa6+9ZuueidcqM6NTXLPmqx7tl7/8ZXz5y1/mP2uaNmofRdf7zjvvxIoVK5gdeeEzZ7qVTZs2MMuyeFCO1+uV5dOmCVFxQcTuM5nLnDZasn9q5dCdzLMXL15U6urqGM1X4ryduf50krFPMuiifYLaF9Uw7O6bgBHJ61gsxp9Tkq6WSGYbMoNUMiW0tDSx7du3MgB8kx+NRqW2/zylqaWRBQKBtMjqUCjEZY8kEolEIpmNPP/88zzqnDaIorQhYwyxWAzveMc7xm1HZhjmH7ub81AoBCA9G0KUUhXv0cDAgK22xfpd2ZgL97WlpYW1tLSw9vZ21t7ezlauXMmKi4tRVFSEUCiEUCiEYDCIQCDAv6f6omRgJWPxoUOHlJ07dypnzpxRpss5OlO8+OKLGBgY4JH7mUESRCqVQlFRET7xiU/YHiwkIUg13Qpx/X306FGcOnUKN27c4GpDovQwUVtbO247K1euBCA4QxUXlIz/LMtyLLELAB0dHTAMA8lkku9t3G43NE2D2+2GruuoqqpCdXX1uPeqvr4+rUYoZY+K555MJtHT02Orf+RYzgyiypVkMgnTNBGNRm0dFwA2bdo04d+Ypjnl2aPEq6++CmAks42UAoCRd4Su61i8eDHa2tpsPVvxeBymafIgGifZN7MR0zSl0gWGZSuLi4tRXFyMYDDI32uapsHv96OkpAQlJSX83ynL2O12Y8GCBTwQSMwiZ4zB5/PxLEkaY263G6FQCGVlZejo6FB2796tvPrqq8qzzz5r6ybU1NQwu7LZxFj3W1y/5Gst8//+3/9TqBSXYRh8vFGgDwAuva6qKj7wgQ/YPoYoby860SWSmUAcf+MFcDgJcCPFk2zB2uJ+x2kN0mQyyd+ntIdqbW213ZZEMtPIDFJJXmlpaWLFxcVgjGFoaAjAcE0nt9uN06fPzu9V9DylsbmBiVFLiUQCxzo65ViQSCSSKWQ2Ga6myyBBEcv5vDa//OUvlc7OTiZmn5BjlAw3brcblZWV+PCHP8zGqvmWjwzCsShUg89U9+vixYu2LmZlZSWAkc29GMkNpGfivvHGG7b6QlmUZNgYK0N7Nj23wLBEqOjIOXfu3Ow6gSxMd+2kQ4cOKXv27GF33XUXgBFnoDj+xPqhH/7wh/HVr37V1jEGBwdRUlICYNgpVFxcnNdzyAcHDhzAli1b0N7ezstvECRj63K5sGrVKjz99NNjtrNp0yakWAouZXQcOMPwnNPb2zspBZt9+/aBZKApuzztOIxh0aJFaGxsxMWLF8dsZ82aNfz+UiaWKElLMpp2nbmUxSZmqmfWHRsPqivpxFi6evXqCf/G7XanzaG1tbVM13WoqopwODxhhsxE50BZvBcvXlSuXr2KWCzGA2AoWzez1q3X60V7ezu6urom7D9x8uRpZcOGdUwMqpkPUOCQqqpZgzlmI07mfMMwkEqlkEwm+XMrZqeKv/d4PNxBSoFBe/bsmfb35WTrd08k05lPdYKXX34Zb37zm3lteJKbp+O4XC4YhgFd1/He974Xn/3sZ221f+LEKWXDhnWM2pmNazDJ3GHESZ89oHKs0gMT0dbWxqhMhRiAl+1ZvnHjhq22q6qWMcuykEql4Pf7uVx1b28vnnjiSfkwSWYd0kEqyQsNDXXM4/HA6/XyxUoymYTb7cbevfvl5DhPWb9xHdN1HYlEArFYDMFgkC9yJRKJRDK/GcvImS/HmSglRD9PhfPjySefxMaNG5FMJnmtRFEqTdM0DA0N4eMf/zi+853vTOpYhersnAssWrQoTemCjBCZEnmKouD111+31XamZCuRTV6skA101dXVTHQ8nzp1qnA76wA7c0Q+79NTTz2Fe++9lzvcMseFKLe2fv16rF69lnV05F6u5PTps8rWrZsZ1bTMlzxwPjl+/Lhy4cIFtmzZMp7xSsZ2kuT2eDxYsWLFuO3U1taOujcktUvX9ezZs5Pqa2dnJ9/niu1Sfw3DgNfrxcaNG/HCCy+M2Q6dCzlJxYwOckL19/fj/PnztgYbORjHyx697bbbWEVFBX70ox8pANDQ0MAqKyu56oGiKLaf73vuuYflIv8XjUbx+7//+7jvvvtYUVERwuEwkskkAoFATuNzovcgvYtdLhcj5yhJIIvXhu5fLBaD3+/Hm970Jjz55JM5nu0wmqYhkUjANE0EAgHccceb2Isvvjyn5sVMSCrW4/HkTVJ1JshXGQEKcNB1HalUCpqmwTAMR89uITNWkMVUZJACwFe+8hXce++9XJGF3o/ASOkDel/W1dXhd37nd9ivfvUrW9dbnBMA+yVQJJJ80dPTozQ2NjIxyCLz3U17SzssWLAg7fOZkr0i169ft9W2GLxGAU6VlZXYuXPXnJn3JPOL2buikRQEVVXLWHFxMTcIAuALwrNnZ38UucQZVTXLWUlJCa+3Rf+nmhsSiUQimd9kM6JMhfMvW43HfDugnn32WfzlX/4lz8oyTZNnjiYSCei6jkAggM2bN2Pt2rXs8GHnddjzUVu2EJyshdIPkYULF3KnNiHWDAVGsoOdZJACuTlAC+26iNjNyp1tTHf2KPHII48on//851l5eXlaX8RsHE3TEI/H4fV68fDDD+NP//RTto5BNfGGx3Bh3sarV69icHCQOxwsywJjLM2pVV9fP+bnt9+2g2VmjqZYigc9uBQXUiyFAwcOTKqfFy5cUC5cuMCqq6t5trn43NLzvm3btnHbWbRoEXeoUB1rcoYbhgG3241Lly7Z7p+YrQ6Mdmxs3ryZvf7664hGo2hvb2dFRUU864OcXk7moTe96U1pToyx8Pv9AIBgMAjTNNOyhcX77RS/38/l7ouKihCLxchhyt/JIhQQc/vtt9s+FjlzGWMwDINnqs5lqF401a2bbYhz/GTme1FtgiS2h4aGcPTo0cKcYPOIWM9V/F0+ef7555UrV66w8vJyXiOU3mGZ8usulwsPPfQQfvWrX9k6RiwW4wof1I5EMlOIztGxnKR2A9wWLVoEAGlBWBSAJa4TVFW17SDVdZ2raEQiERw71jXn5z7J3Ea+ASSOqK6tYavWrGZLly7lUZmJRALxeBxDQ0PSOTqPqamrZmVlZXC73fD7/YhGozjW0al0Hu1SDuw7qBw9ckyODYlEIpliCjkLLRtT6RSa6gzSs2fPKrt27YLH4xn1b5RRQEa0hx9+OKc28309CtEhWWgsXrwYQHrmsSi1S+MmGo3aNiJkc5DmUmtIMr3MlIMUAF566SXuWBL7IBrD6PcPPPCA7fYHBga4QZ9kCguNmzdvIh6Pc6k5sR4nZWEvXbp0zM+LUuciLtdwHVJiz549k+5rR0cHr9WpqmpaVgc5+Nra2sb8fHNzMysqKuL3hOYd0fgPDNdmdUJm9qg4zzDGUFJSAo/HA8uyMDAwAMuyEAwGEQwGuYPWLlu2bMlJclXMuBXHN9XzFu9/tq9sRmTxCxgu8UP3QZQXJOcoY4wfm343UX3bbEQiEaiqCl3XEYvFbKsLzFboWudTUnU6ycc8T+PRNE0kk8k57RzNnD/E/xNT8e587LHH+DoaGAn+0HWdjz8KTrj77rttt3/y5GnFMAw+B8+2vZNk7jHWnoDGp10HaVVVVdY2M2uROgn+FAMLZHCBZC4gR7HEFjV1tax91UpWUVEBr9eLoaEhhMNh9Pf3o6PjmHL0aKdy6tQZubKYx4RCIbjdbqRSKSQSCUcbbIlEIpHMH6bDOTTVRo/vfe97iEQiAMAN3KLhkEoPvPe97x23nYkMTk6uVSE63wrRKbho0aJRzgkg/fpZloW+vj6cO3fW1oAS6wyS0zWTQrse85WZMpA+9thjo1RWRGcTSTgCwOLFC/HOd/6erQFD+zPGGAYGBrB27Vr2rne9q6AG3a1btwCk118lxyEZ30pKSrBuw/qs/d6yZctwxijSJeTIOcrAEI/HHTsdRV599VUMDQ0hHo+nSXITjDEsXLgQa9asydrXpqamtMxJmntEeXbGGPbv32+rX/X19YzmGmC0g5R+NgwDfX19CAQCCAQCAIalaWOxGIBhZ6VdWlpabMlTu91ueL1e3ldyaJKxdawvMdgp25f4HNE+NJFI8OcplUrB7XbzGpLUJ7fbjXe84x22ngnKVKV+2ZU/nI2QY5Cy0ucCduf9qqoqRpmHJDk8l8kMQCCyPX/55NFHH0V/fz+vK5zZPj3DiqKgoqICDz/8sO13GrVbiOtSyfwjW8CPOO7tzrnLly8f9Wxmy/xOJBK4fPmyrbZVVeXvgkINvJNI7CAdpJKcaW1vY+Xl5Xwjk0wm0dl5XDl6tFM5c6ZbOkXnOcurl7F1G9YyXddBNY5UVU2TX5ZIJBKJRGSqjBFjGWmmKkL8Rz/6kXLt2rVRxyKJeU3ToOs6Kioq8N73vnfUSU+lTJkkN4qLi0dJMmc6FSzLwuDgoONjZMplyXtdmOSSoZZvnn/+eeXGjRtcWpYcNyTrnEwmed9MM4VPfOITto9BjnpyzF27dg2rV68umEE4NDSUFkBATkO6HpZlwe12o6qqKuvnV6xYgUyJXZFEIoFIJIKursnLwB04cAC3bt1COBwGMOw0UlWVGwwVRYHf7x8zK3HZsmWj6huTk43aAoDTp0/b6pfH48laY0wcu4ZhwO/386zceDwOxlhaTUm7WSpr165llZWVOf89SQoD4A5NKssy2eePAglIvtiyLHg8HiiKwh2/YlYynatpmti+fbut8z5//qJiWRbi8Tg0TcPChQttfX42ImaPztd3WE9Pj2KaJnfAezyeeeEgGO9+T8X6urOzU9m3bx/PGk0kEtwxT32hNTZjDA899JDtY9C7lRz/TU0NrKamijU1NczPwS2ZMRRloiGXPcByPCoqKn7b9vCzSeMcGAlGsywLsVgMp0+ftvUABwIBuN1uJJNJmRQjmRNIB6lkQlpaWtiGDRuY16MjHo3BTBpwQYFl2Ns4SeY2ZSXlSJkMbpcGI2Gi71Y/BvuH0HF4bkrNSCQSiUhmLR5RplNE3NTnyrDRdLhunKpqEOvH0TEmG/1MxoVMBxH1GQDPuLALbcCySeRlIp6LE0TpPkLcDIp/k28ef/xxGIbBHaOKonDjLB0zHo/jT/7kT0Z9lozhqqryz+arr5ltKYoCKKnsX5yU8GWfsSTY6L4PZ1zZOzdxzHg8bmzcuJ6tXNnG3va2t+blhi5ZsoQb5+l45Kyg/3s8HnR3d9tumzEFqRSgKCpisUTGv43OyLB737M5RDLnHikdlzsTZahNFY8++ih/PkTZM9FhNpz95sL27VtRXV1ta6AcPHhY0XUfdxLFYrG0LMaZ5vjx44qYSUlfotMRANpbs0vXNtY3gKVSYFYKLJWCAsAFBSnLggLA7VJx5NDhvPR19+7dyoULFxCPx7kDjt41opN3LNnf9vZ2ANnllEn6NhqNYu/evbYGnK5rPEhVlKulMTV8HRkMIwG32wXTTELTVAApMGb99mcNZ87YU4Rav379qIxnYGT9IGa9MMbS6v6JwbTkyJzM80fHonGuqipfu5AcsiirSdfd7XZjw4YNdk4bwPC8Dgyv0xKJwjESU3DFyDvXysv81dPTo6RSJhKJGKLRcB56OvVkSgLTtZiMk9ftdkFVFTBm8WdnriKOo0wpTXFM5SKxbZdvfOMbsCyL1w8WlT40TUuT6t66dSuqqqps3dBz5y4otN5jjMHn88Hr9U7JuUiyZyM73b9mk1zP5fhOpMHFvdRYUu/A5OrYppgJBguqWxHeWRpMM8X3/nbfzfX19Xz9ROoUtKaiZ0lVVdy4ccN+f1NAMmlC0/TfvgclktmNdJBKxmTlypVs7drhjEDDMNJqrBw4cEA5dkzWkpQMs27dOubz+eDxeJBIJNDV1aWcP39esfsCl0gkktlKrhu76XBS5OsYmU6ufDoWx9rIzmaJq2eeeQbAiHwfAL7xJEOcz+fDunXrsGHDhrSTTKVSiMfj3AAkGZ94PA6PxwNd19Hb28t/39TUwO6++05WV1dnaxA1NDQwTdN4RhEwYuQgozwZGC5evGirr3V1dYwcAoqiyDo9kjF5/vnn+TgTjW2UmQiMGKDdbjd+93d/1/YxotEoH+M05xQSiUQizTmczZBaV1c36nObN29mHo+HXy/RSSk6QQ4ePJi3vl66dAmRSIQb6MVj0pzR3Nyc9bNLlizhfydmzNI5AkBPT4/tPuVi1M90NorXWVEUR4FQmzZtSlt7ZAbmqKqKeDwO0zSzOjonyhq180Vtk+PVsixomobBwUGoqgpN07izmPpKGbRO6pCSfO9srcdpl+bmRmaaJrq7zyvZaq/PF6YzeKZQyfYc55tnnnlGeeONN+ByuRCNRif8+w9/+MO2jxEOh/k8QAEvJPktkQBjj+18P/e0TxCDiIYDE9yOnJAVFRWj9jWZgSGMMVy5csVRX+fjvCeZu8gdumQUa9asYevXr2e02fN6vfD7/YhGozh69Kh0jErSWL9+PVNVFeFwGPF4XMorSCSSeYmY3SKS6+/sImaJZTNK2kU0SBOZEfez2XkpMlXncPToUS4FRlJ+ZMyhjBUA8Hq9+MhHPpL2WVVVpWM0B8SsgWQyiUQiAY/Hg9tv38He8pZ72KJFi3Dp0iUUFRXZare6uhrkXMk8VqZD89ixY7ba1nWdR21na286kYaMwmbv3r3KgQMHAIw2YFH0P6GqKh588EHbxxgYGIDL5YLH40EoFCq4MTE4OJhVCk50uLW1jc4gXbNmzSjnIF0v+r2madizZ0/e+trV1YVoNJomiUz9pv+vX78+62cbGxsBjDhSgfS5QdM0nDx50nafcq2FmOkgFa9zLk6ITN70pjfx78Vr4Xa7+drB6/XC7XZPmPUz2QxScsaSs4MyRXVdTxtbdN9I2hcAli5dOmbd2LGIRCL8POcLlmWhsbGeHT3aWVgTyDSSqdZQaHPpdDOV4/+nP/0pNE1Ly4oea+544IEHbLd/8uRpheZft9sNXdfn1fMsyR3xPZS5F5/sHJDZtqhGJQb12GHx4sUA0kt80DtR/N6unL/Yx8koP0kkhYR0kEo4zc3NbNOmTYyyGEpKSgAML/pv3ryJs2fPzu9Vn2QU7e3tTFVVmKbJ66tJORKJRDIfEQ3X2RCzSqbDQWJ3o5K5scsmjZpPsjl2pyIKf6xMmaniq1/9Kt/EisYc0QFqGAbe8573ZO2npmnzJtDIyRil/1M2blFREQYHB3Hr1i1cuXIFiUQCCxYssJ0V19DQMCprLbOPtL45ceKErbbJQVooRtSZPr5kfL75zW8CGJHHFCHjmGEYcLvd2Lx5M9ra2mzKCZ5TUqkUkskkVFUtuAzSmzdvcmNbNvlLxhiqq6tHfW7r1q0ARjuWgREDIGMMHR0deevriRMnuKMtM/uTqKqqQm1t7ah7lFlHNTM4CYAjB2m2gKnMZ56uUbb3osvlQiQSsX1cMavX5XKl1TAVZWy7u7tx9epV/nXt2jVcv34dN2/exK1bt9Db25uXr/7+fgwMDKC3txdXrlzB66+/Dl3XEY/HuTOUMn7pWYvH43C73dixY4etc+/u7ubOlfkwv4ryxPOZsSQ25yNTfQ0effRRXoN6rOPQeKytrcUdd9xhuyMU3CgqwEgkYzFV4yNTCUF0itK7K1c2btzIvF7vqPVFtvnb7noj27pGIpntSAepBOvXr2dbt25lXq+XZ4y63W709fXh2LFjSmdnp5RKlYxixYpmpus6r9cVj8dx48YN9Pf3z3TXJBKJZNoh419mxk82CnHTnZlBOlbWRr6MH9nangsyZY899phy+fJlJJNJuFwuaJrGz4fGiKZpqKysxHve8x5+MQ3D4Nks80HWizFnY4mupWEYiEQiXK4xFArB7XYjHo8jHo/bNvC3tLSkZUpnG++UWXXp0iVbbVPG1ERBFPlmLAPibH6+CoWpNIR/61vfUi5fvgwAPNgic+yI2cgPPfSQ7WOQgSwajRac5PPly5dHZRhmUlxcjKamprR/oEzNbO9gy7JgWRbOnDmDnp6evD0APT09fN6muqHAaHnuFStWpH1u/fr1LBQKpRk+xTmHvrcbjFFbW83o89neq2KmR6YRVvz3ixcv2rpG9957L8vMXKVAlswa5n/7t3+LpUuXKkuWLFGWLFmiLF68WFm0aJFSWVmpLFiwQKmoqFDKy8vH/aqoqBj3q7y8XFm8eLFSWlqqVFZWKsuWLVPe9a53oaenB2QsJqldYETBgZzyYjZsroylIjIXYYzx67Vly6Z5bSQvlMAnO9gtQZDJeO+/qXo37t27Vzl06NCYxxDnL5/Ph49+9KO2jxGPx/m8lUwm4fP58Du/c9e8Ht+S6WN51WImBlGLTkz6fzwet9VmTU0NgJH3E2MsLXhJDMg9c+aMrbap7rpEMpcorB2RZNqoa6hn6zasZxs2bGDxeBxDQ0O8fuS+ffuUjo4O5fjx47NnpSeZVpqbG1lxcTF/cSuKghMnTigXLlxQ7G6qJRKJZC5AG46JNgv5zCDNloGZ+ftcGa9P4r/Nxs1QZmbsVEf5/+AHP+C1zxKJBB8bpmny7y3Lwic/+cm0PjLG5lH2qP3PZBrAqC6jqqo8Q1PTNJSWltp2gqxYsWKUw0D8njh37pxt58Fw7SAXl3qcziyT8bIsJIXLb37zG/69OLeTE4oy3izLcuQg7e/vh8/nA2OMz1WFwoULF2Ca5ihpXYLeoZm1PZcvX84leUWHMj3Hqqpi7969ee3ruXPnlFu3bqU5YsX+0vO+ZcuWtM+Rw1QMSBJRlGGJdruSd3QvszlFxwtAEg2xTgI5duzYkWa4pYxal8vFg38URcHAwAC+973vzcgEdODAAeX8+fNIJBLcCQIMv5epv1QnevXq1bbbp9q582F+pZquixYtmteB0bk8W4VIrjLc45FtbTHV6+snnnhilFJZtnvgcrlw11132W7/+PGTCklu0/xA8qQSSeb4zvdYp7qjbreb7xvonURrPrvy96tWrUr7mYKDgPS1ZTgcth38SUEy8zlzXjL3kA7SeUZjcxPbsGkjq6io4IY4j8cDv9+PRCKBrq6u2bGyk8wY9fW1zO/3w7IsxGIxGIYhZXUlEsm8J1sG6Vjkw4iSmfUx2bYznYjZHKFkkM8H2aTJxKyhqWSq23/00UcRi8UADEfY0juSFDroXLdu3YqGhgYGgMt6zRYDWz5weh/oGiWTyTS5KV3XYRgGent7bbdJEruiU5SM+uS8BpxLXlI5gmyyqVONNGDMPh5//HH09/en1Z4CRs/vpmliyZIladnouXDu3DklHo9DVVXbkm1TzcWLF7mDNJt6AT1PVMMTAO666y7m9/vT5HXF9wldv507d+a9v+fOneMG9bGcJCT/S6xcuRLA6MAk8Tnt7+/HgQMHbL0QfD4fHy/i/zOdB5mZKfR9KpVKyy7JlS1btvAM0cz1g8fj4WPs7NmzttvOJ0eOHOHGZ+qroqTX9lUUBUuXLsWGDRtsPVP0zp8Pc21Pz+tcpvvkydPzZ9GSQWZQ4mxZv02F3WY6ZIafffZZ3LhxY9TvM+c3y7JQVlaGP/mTP7HdGbJraZoGy7IKToJeMvNkK8ORj/FPQTrZ1nsU1PP66/aCP9evXz8q2FNcd1CwxBtvvIGjR4/aatvj8WQtRyKRzGakg3Se0Lyiha3bMCznYxgGf9mnUikcPnxY2bdvn2J3UpTMP+rqalhFRQV0XUcsFuORyvMl40UikUjGYqJNwkw4ReyQadgRN3qigTpfGaSZEoqZTtLZzOHDh5XDhw8jFotxQ5RlWfy8RMPs2972NgDD2SeMsXkhr5sPKLJa0zS43W5YlgWPx+PYyVxZWTnqc2IGG43L119/3XbbZLSjzASJZCJ+8YtfKLdu3UpzVpEThzIkKWM6FovhQx/6kO1jxONxHlRw2223sebmZrZjx44Zn3x7enoUsUZmtudZVdW0zJ6Ghga43W6efUFZFyQnRzK4Bw8ezHt/L1++DEVRuKy6GFABDD//DQ0NaZ+pqalJk2QVnbqKojgO9PB4PFmdNpkZbtkcpzTPOQmCamxsTKvbp2kalzt3uVy8DvP+/fttt51PXnvtNT5GaD6mICZRQcDv96Otrc1W22Rbme3rl1xoaKhjyWQSe/fun9e2I5qnpiu4Lx9UV1ez2eLIzeTcuXPKnj17xszazZzj/uAP/sD2MUzTRDKZ5GvLQgsgkhQe+Xjuq6qWMXKO0hoi067g5N1cX1+fNYjaMIy09eW1a9dst52PTHSJpNCQu/Q5zurVq9nGjRuZprqhMAAphqGBQXQcPqIcPnhIOXbs2OxcIUmmnerq5ay8vJxLJXV2HldSqRRKSkrQ0dEhx5FEIpnXUPYAObjEjYyYaW+aJhYsWGCrbZINFGV2TNOErutwuVx8Ay/KQp0/f97WvFxaWsqN7iTBQ5syyrZwu9225X3o8wRla2TLaBGjZyebXZjZhngudtuuqamx3Zmvfe1r8Hq93DGqqmrafaSsmve+9728f2TEF/ubD8RzzzQoZR5nsjJxY0lGZjuWXcOT6EygMZRMJmFZFnRdRyKRQCqVsi3595a3vIVnn4n9Fp9Z+t5uBlp7eyszjARMMwld12CaSaRSJjweDzRN42ouZJh3GiCQGWhAZMvAk4zNWPcgMzBkOgzM3/jGN9ICJhRFgWmafK6MRCJwuVzw+XyOaiZ2dnYqiqJA13VEIhGoqgqPx4O3v/3tMz5QLl26xJ2cyWQyLZObrsPatWv532/dujXNQZVKpZBIJHgNaJfLhd7eXhw+fDjvN27Xrl18vCSTybRAIkVREIvFUFNTg7a2Nn5dm5ub+d+Q9DYw8q7UNA179uyx3Rcxg1PTNBiGAZfLlZbdaRgGdF3nTkLTNBEKhZBIJOD3+xEOh20dc+3atay6upr3n66F1+tNOz9VVfHqq6/aPqd88tprrwEYfp79fj//nsaaWPP3/vvvt9X2xYsXFY/HU1BBMOI5USa1+K4BwDPu7GBZ1qwyjmfO56LMZLaghlyorl7OTNNMW9/NBnRdd/T+onePqGogBmFMdu2eK1//+tf5mkwc28CI6gftKdauXYstW7bY6tDJk6cVmjMTicS8lpDOJ+I8k238iSoGdtsdKxhorC+nc/REY3syQRI+n48HeNLzRPO03+9HPB535Kyvra0FMCLbT9cqU81m9+7dttumAChxnyuRzHYKZwUnySuNjY1s69atjDaWlZWV3MB57tw56cyS2GbBggVpklcAcPDgQWXXrl1yPEkkknkPOUiz1U3LdBCWlJTYaluUCBSlckQDF22knDpY/H5/msEo8xwm42SZyAmXT4miqcCJIfDRRx9Vent7YVkWj0inWpmapiGZTELXdTQ2NqKmpoaRQQaQjqyJEI1hZFCg509VVZw9a2+du2HDBoiR2wQ9SySLHIvFcObMGVt9FdsUx3Z/fz+SySRSqRTcbjfC4TB3aMwmw/Ncg7LKMg14M+H0+NWvfoVoNMprO4qGKMZGaodalgWfz4dPf/rTtieOSCTC3yNerxf9/f3o6+vL41k4g+qQkqGQ9h5kgHe73aipqeF/v3r1ani93jQHpcfj4RkSyWQS586dm5K+Xrt2DbFYDG63mzsiyRBsmiZ3FIqSwCUlJWnzfbZM0u7ublv9qKurYWLmSTKZhM/n4w7mVCqFYDAIt9uNoaEhUJCraZq4ceMG/H4/PB4PLlywJ+G3bt06/r24FgFGJOYNw4BhGLZrquabCxcuKG+88caoeZn6S+8TVVXR0tJiu32a0yWFxWzNmJwKMgPBZhs//elPlStXriAajY4KBqOMT5qD3G433v/+99s+BmW/kzpJe3urXJTPIrKpExXq/pKgAGFxH05OzFgsBq/Xi6GhIVtt3n///UwMvqK9DKlqUACeaZo4duyYrbarq6sZrWEy3/sSyWxGOkjnGFu3bmW33347Ky0tRX9/P59c+/r6wBiDzBiVOKGlpYn5/X5uaMhXDTqJRCKZK9DGJZuDVDTEZUoD5gIZiql9cjCSsZyMxqKMq12Ki4sBZJ/fyWlANafskln/ZKzswkLdwHo8nrTsn1x58skn4fF44PF4kEqleDaPKB9ZUVGBO++8Ezdv3gQAXvcon2TLCh1LnqyQyRwbYuYPOUacjJ3bb7+dtweMyCFT9hn97ubNm+js7LR1kcTABjqGoiiorKxEMplEKBRCPB5HeXk5gOF74CRLW5IfxCh4yiTLln00HRw6dEjZu3cvNE3jY1PXdd4HUTHA7XbjgQcesH2MaDTKjXAk11sIZTMOHTqEGzduIBwOpwX9iJlelZWV/O+XL1+eJrMNpGesMMZ49mC+OX/+vHLlypW0Z1zMPqd39aZNm/hn6Hmnvonf088dHR22+hEIBPhagNQLyFkQj8e5w7ivrw8lJSU8I6W4uBg+nw8+n8929igA3Hnnnfy8CbpPNP9ZloVbt25NSQavXbq6ugBkV5UQjby1tbVYsWKF7TqkjDHU1tay6urqwlrISADkd51T6GumbORjfs+U/5zu9fsPfvAD+P1+JBIJXt+djkvzPz3H73vf+2y339V1QiFp8KGhIZSXl2P16pXyeZZMGRRgJTo0xXWEy+VCT88FWxPOXXfdlSabT0G6mZme0WgUR44csdVfUrDKVgZCIpnNSAfpHGH16tVsw4YNLJlM4ubNm4hGo6isrISqqjh06JBy+PBh5cCBA7NvFSeZcdrbW1kgEOBGlEAgIAvWSyQSSQaRSARAet2vTLlR+nn58uW22iYjJzDirMx0tJFhwqlEakVFRVpfRURjvN0IVvq82HYu/Sskw5Pb7eZZQHb41re+BWAki4iceKZpcmN1KpXC29/+dsRiMW7Qmcoo3PGkqGYLNNbFjDqv1wvGmCMDP9Wao+ueKT1Fcm5kWLdDZq0/+t21a9cQCARQWlqKEydOKa+9tk+JxWIoLy+XdWhnGDFLE0ifi6Y7eONb3/oWN2pRcIoYcELGrlQqhebmZmzbts1WB8+ePavQc0TZyx6PJ/8nYpODBw+is7MT3d3dGBoaSjtn+r+u69iwYQNbt24do/mVnJGiAZCkg51IyOVKZ2cnTNPkc4UouUnv6S1btgAA7r33XibKu2YLXrEsC6dOnbLVB/EdRRmtuq4jFArh0KEjyoEDh5Rdu3Yr3d3nlYULF6KoqAjRaJQ7F+LxuKP93caNG9MCCUjmUgw4cbvdts9nqjh06BAAcHlhyhgVHT6pVAo+nw+tra222qbglvLycpSVlaG+vl46VQqEfK9xcikjUIhcvHhREZ0ak2Gmzve//uu/AIzsuyhzlIIQgZFgw8rKSjz00EO2O0rzuWVZCIfDKC0tRVubvYAJycwwGzNIyTmaqWJD6xcne+8dO3aklScAkNVOcO3aNdvBn1TvXJTQl0jmAtJBOotZtWY127RpE9u+fTtTVRU+nw+GYSAQCKCzs1N5+eWXlf37988ei5ek4Kivr2WBQIBvImOxGGKxmKOXtEQikcxluru7FarHQZCRMNNBumTJElttnzt3TsmsGyU6RcmgR5sUJw62iooKLl0o9jVTwurq1au22q2rq2Hihmy2OuacRN0fOnRI2bdvHzeaBgIB7oRgjMHj8SASiWDLli3Ytm0bv9ZT5SDLzBadTdcfSM9aIMMBOZ2DwSCSySROnTpj64TuvfdeRsEB2aRtyaGtKApefPFF230WDROidFYgEMDevfuV5577Ce+vz+fDpUuXkC/jpcQ+lCkcDod51D6RzWE61Tz22GPKzZs3+ZwBjNSkNgwjzekeDAYdZcskEgley1rTtIIIgjxw4ICyZ88eHD16FDdv3hyloEASusuWLcOKFSsQCAS4gzCZTPLzIels0zRx9OjRKevvoUOH0t7D5HCmQBi3282dbVQ7NXNeAEbG1o0bN3D27Fnb2erkjPX7/YhEIhgcHMyq+vD00z9Qbty4wQ2cVBPZydxTVVXFndI035HDNJFI8HfeVDqo7UC1XTPXSmKWDT33O3bssNX2hQvDGT7UlpPAKsnUMZVzd6E6XrJBsu2zla6uLuXo0aPw+XzcQUPqCtl46KGHbB/j8OEOxTAMkIJaOBwuiOAhydxElNelvQ6tQXVdt+3AbGhoYM3Nzfx9lqmGQso7wHBAml1IYScftV0lkkJCjuJZRkvrCrZh00a2fuMG5nK5kEgkEIlEYJomXnnlFeXo0aPSKSrJG5WVlTxzNBQK4dSpM8qhQ0eUixcvyTEmkUgkGYiG3EzZPBEnmQWizG6mZC2QXjvPrvGDMnAyM15E4zswvKE6f/68rbbJaJHpOBYpdKcpZW81NjbatoB9+9vf5pk6ZMgh472iKPB4PKisrMSDDz7IHX5Tff65yO0WMmIGqRgkMDg4aLut3/md30lzjIqyi5RZRUbuV155xVbbzc2NTMx6E5+BbDK6r722Tzl79pxy5MjR2XEj5iBnzpxRjh07htdffx3hcDjNMZc5706XQfxnP/sZFEWBrutpstIkySb2495777Xd/sDAAHRdh9vthqqqjp6jqeDQoUM4f/48bt26NcpBSo68iooKXtOb5KnF2qz0zF26dAknTpyYsufq6NGj3GEtysyJ8q3BYBDAiEOR/i7be/3EiRO2+5BIJJBIJHjd666uE0pn53Flz569Wc974cKFqKioQCwW445MquWcK7/3e7/HNE0blX1J41SUrCbH5Ezz4x//WCGlAdE5nVkeQVEUbNu2zXb7NEYpCEoys0zH2mY2OUeBEUf+ZJnJNeO3vvUtiJn4wLDTRgyko3XcXXfdhQ0bNti+SfT80pqd5nBJYTNeQG6h7nNEeV0KAqP3qZOAhttuu40HEAAj+yZN03jwmNvtRiqVwgsvvGC7fVrrUECUWHtdIpnNSAfpLKF91Uq2dv2wAZMmIzKseTweVFdXz3QXJXOMFW0tzDCMUXWOJBKJRJKdK1euABhdK5F+R0Yzv9+P5uZmW22Tg5TaoM2T6HShKFG7Wf4rVqxIqzNKZJPEtesgpWxIcWM6kYO00KJQKYrXiYTQ1772NaW3txeWZSEej/PrQU45XdehKIrturR2yGYUGMtYUKjGA2C0bBZJNyqKgr6+PnR3n7fd+bvvvjutTp7o4BAzic6cOYPXXnvNVvt+v58/rzSuqR4QyfpKCo+9e/fi7Nmz6Ovryyp7PRljeG1tre0Pf//73+fS0RQEI0qxUXZ7KpXC8uXL8Y53vMPWMc6dO6ekUik+JxWKVNrFixcRjUbTggnEoB3GGFpbW1FXVzcq81GU2TVNk8uqThW//OUvlatXr46Sbc3s8wMPPMCqq6tHvQszlSH2799vuw+RSATxeJxLt0/EK6+8qpB8Mb2H7AbA3n333WnOUPHcaZyqqopwODylGbx2OXnyJICRmmxAer1oun6NjY2wW0vUMAz+ni+UZ0kyQr7WOIW8VppqMtfzM+GAeu655xAOh6GqKgzD4NlwdHyaiygo8QMf+IDtY1DpCwp2mM/3XDK1iPt7Wt+43W4YhgEnZfLe8Y53pKlO0V4JGHnXuVwu3Lp1Cy+99JKttpubmxkFedGeKVu5AIlkNlJYFijJKNasW8s2bdnMdF3nWQW6riMcDuPwwUPK/v37lX379inPP/+8nJEkeaOuoZYVFxcjmUwiEAggFovh5s2bM90tiUQiKWh6e3tHOUdFKVz6XlVVNDU12WqbNiKZBlXRUE4blQsXemytCerq6tL6ly3bk87pxo0btvpNhtKJNk6FHOFLEoSWZaG1tdW2g+OHP/whQqEQfD5f2mZVjAom4860ZZwwF/+/oijDP7OR6OXZAN0XRVHQ2XncdqfXr1/PVq1aBSDdKE5ZB3QdLMty5GAhCSog3RGSSqXwgx/8aHZc5HnIqVOn8MYbb2BwcDCro2kyDtLxZADH4uc//7nyxhtvcCegKJUmGqVcLhd8Ph/e85732D5GJBLhzvtCyZI5efKkkimDSudKNee2b9+Obdu2ceeupmmIx+PcSUVSxE4yMu1y5coVUGBppvQ9MCyhfeedd6KxsTEtW0Sce4Dhe3r8+HHbxz9zpltJJpMwTTPnIKmqqiqUlpbyecku69ev59nM9O4m5yAZUC3LwvXr13Hx4sWCmfNOnDjB37/0LNEYEusYlpSUoK6uzlbbVLcwkUhklTeWzF1mWybpbObChQvKK6+8AsuyoOs6VFXlwSHiPoIC0x544AHbxzhy5Kji9Xp50EdfX1++T0MyBczGDFLGGFLWb/f6UMFSCneQOuGuu+6Cy+Xi7zUxK5UyRw3DwPXr19Hd3W3ropBKB73vSc3EyfpWIik0Rhfbkcw4jY2NLBAIDL+MXSqQYkjGE+g6Zk97XCJxQl1dDVtUuRDxeByqqsE0U0ilYLuul0Qikcw3uru7AWSPrs6sn7hp0yZbbXd1dSkbNmxguq5jaGjot5uSYUnWZDIJv98Pj8eLgQH7NaLXrl3LpRpFoy31VTyHs2fP2m6fjAui8djnCyAajcIwDJSWliIajcLlcqG8vBwXL17kGzA7ZNYIFDfCdF5iplTu7VpgzEIoFHAkdfTlL38ZH/3oR2GaJjweD5LJJDweT1p9skzH6FjZtk4yUhhjwG8d4KrLBdXlQtIwUFFRgYGBIUQiURQVlcCyLESjUYwYhNxwIh6RLYN69N9YSKXs1buzLAZV1fj1c7lciERi6OjocLQ++chHPoJ4PA6fz4dYLAa/388zzyg7lQITnnnmGdvt03NJzofdu3PPQLVbu050ypAhhJwz4jMtDbgTc/PmTQwODqbVhBKvI0X3OzG06bqOlStXsmPHjtn68COPPIJ//Md/TMtOA8CdU9SfeDyOd77znbb7dfz4cWXLlk1sONN9tPzzTEF1UUVpYboGuq6Peo9qmsavD2Xsu1wu7Nq1a8r7evz4cTQ2NmLx4sV8vGQqInz84x9P+4wYNGWaJj/XvXv3OuqDqqqIxWI5KxK88cY1DA1FoOs64nH7tWdbW1v5+dF9Eechuv4HDhyw3faGDesYZaaoqsoDxHVdh2UN152OxWLQNA3Xrl3DuXPncn6mXnnlFZ5RRv2nWrEej4efSzKZxF133WWr/nRnZ6eyfv16RnVgZxKSWybEbF9xTWR3XcGY4mhtMBOICg6i04ze9fTvdq9BKjX8ZVkpaJoGv983L4LJM/czmeVEpnON8cQTT+Ctb31rmsoLBXpQP2KxGHw+H5YtW4b77ruP/exnP7P17g2Ho9A0HanU8L5F4gxRejzbGBFlke1A+zNFgXDvx//MZJykuYxxJ4ExsVgCuq7Dpbjh8XihKAZu3LiFM2fs21//7M/+jGXu5emdRvMe/ftPf/pT233VdR2mafJgoiNHjkgbsWTOIDNIC4jW1la2ceNGFgwGuZRDMpnkdUEkkukgGAwimUyCMcYlSzKlqyQSiUQymo6OjjSJTiKb3K4Tic0DBw4o0WgURUVFfHNCcvuWZaGvrw8nT560vVG58847eTQpbRypHqZYQ+2NN95AZ6e9YC1R1odqnrjdbvT19cEwDJSUlGDJkiVoaGjAsmXL4PF4EAgEbDsxpxIyzAPDjquGhjpbFqCOjg7llVde4Vmj012XLFvktKpq6Ol5HYZhwO8fzhizLAslJSVpDph8HHu8PtnBNE0kk0kYhoF9+/Ypr732muLUOQoA73vf++Dz+ZBIJOD3+9MyuXRdRyQSgaqquHbtGp555hlbx2lsrGfkaBWzyHLFbh3I8YxOmYbMQo2gLxRo3Uv3TpQ0n2y5CdHpaoef/exn/L2SmaVIkFPO7XbjYx/7mG0rdSQSSZNkKxQyndG5XD8xq2hoaAiXLl2ayi4CADo7O+H3+3PeM4n1OsWx1dvbi9OnTzt6SF0uF/x+f06ZTvfffz+7du0aFi5cCF3XYddpf8899zBRdjJTTl8MJHCSEStmo5qmiUQiAVVV4fF40mQ1ndQ9O3bs2ChpXTHLn46raRra29ttt0/9TSTsO51nA16vd97LB+u6jlAohGAwCMuyEIlE4PP50N7eztauXcs2b97M7rrrLrZ+/fo5EZVUiMFV3/3ud5XXX38dbrcbiUSCO+HEQDefz8f7/sd//Me2j9HX14dkMol4PO4oQFIy9YwVUDodxxSZbIDA8HoA2LVrl7J3717FiXMUAD784Q/zPQ31RywdIq5Rnn76advti85suZ+QzDUKx/o0T6mpqWGlpaWIxWK8nqhlWTzC4+jRo3LWkUwba9asYj6fD5FIBC6Xy7YhXCKRSOYzVDdMdO6Jzglx49TU1ITm5mZ26tQpW/Ps4cOHlbVr1zLK1KANSiQSQVdXl+05e+vWray4uJjLlYpkGg57enrsNs8N7qLzlTGGsrIy+P1+nDt3Drt27Urr95o1a1ghSfVQloFlWfB6vSgtLbXdxpe//GXcdtttU9C7iREdpDQOvV4fz5jxer38e6pJl08nab44e/Zs3tYkf/VXf8XKy8v5eQPDjiePx8MdmoFAAIwxPPfcc7bbDwaDXKrXSbZhT489mexMxGdNZo7awzRN7iAF8nsNab62m0Xa0dGh7Ny5k91xxx3j9ocyAz74wQ/i61//uq2+hcNhhEKhggpOEWtti5lguUDPwIULFxwFDtll9+7dPAM0F8Q5WTwvJ+/ZxsZ6RuoIokNgPK5cuYKSkhL09PSgpKTE9jHvuusu7oQgZ1m2OmSKouDVV1+11XZb2wpGzmOal6n9gYEB6LqPB4/rug6/32+r/ddee00ZHBxk5eXlaQFc4nWj81q3bp2ttgFw2UHDMFBXV8fsZLfOBuLx+Lx/p5w+fVo5ffr0THdj2hnrvk9GwrStbQWLRCK2y4MAwDPPPIP/9b/+F3w+H1cXoX7QM0zZc/fddx/q6+uZHUnR8+fPK+vXr2eBQGDOBjzMZkTnqFgqZqqPlclYe/1cyVcW5v33389LhySTSS57S/8nid1EIoHTp0/brm/a1NTE6D1vN/BTIpkNyFE9QzQ2NrKtW7eyJUuWwO12IxAIIBgMore3l0c7OSnILJE4paaminm9Xr64kBFBEolEYo8jR44oly9f5j9nk6Cin3Vdxz333OPoOIcPH1auX7+OgwcP8lrkTpyjAHD//fcjmUxmzQYQDeWGYdjOAqmqWsZEGTfCsizEYjFcuXIlq5TokSNHFCdZIcDEUe5O3m204SaniZPMiaeeekrp6enhBuXpJNs7PZFIQNM03LhxA1euXMH169fh9Xrh8XjyXoMwm+FsptcZf/qnf4poNMrHfqYRnozviUQC3/rWt2y3Tw5mu06dfJBtvhH/L9d3E5NZRzqfGQqKojh6xr761a/yz4v3lJyI4v3dtGkTmpubbVnpzp+/qIhZe4VAPB5PcyTm6nwU5+ijR49OVffSOHr0qNLb2+vYwUxO4FOnTtn+rNfrhd/v506+XFQK6DPBYNDRO2379u2jngfKTBGfl3g8jhdeeMHWg0NzMjA8l3o8HmiaBp/Px427iUQClmU5rvN54sQJHvgkPjticAkALFu2zHbt8XA4zOd9v9+P6urlc8qb6ESSVjL7GWtPM1lCoRCKi4vR2Fhvu+HHH3+czxU0JukZpnnQ7Xbzuemhhx6y3b/BwUEoioKBgQHbn5VMPdOZQTpePdNCWGP/7d/+LVKpFBKJRNo6gPpEz4HL5cJjjz1mu30qG0JtyP2EZK4hHaQzxJkzZ5Q9e/You3fvVhhjOHjwoLJr1y7lxIkTym+l2ORsI5k2qqqWsbKyMi7prGmafOFJJBKJA3bv3p3T3yWTSTz44IOOjzPZDDPiHe94B8+aS6VSac47UYZH0zS8/PLLttr2+/1c8pHqAwHDm8jy8nIYhpFVqWD9+vVssk7E8Qw4Tt5vZLwmg+yqVatsG3KefPJJAM7qiOYLui5kcF6wYAGKi4vBGMONGzfgcrlsy7uORbbrXAhriy996Uts0aJF8Hg8XBKTxjpllJKSy5EjR7B//35bna6qWsbI6C5KTU4nYzmgC+H6zybGcpQ6RTSs1dbW2hoUTz/9tHLx4sW0LBZRFlR0kmqahg9+8IO2+xeNFk79UQAYGBhIM8I5eY46Ojry3a0x6ezszMnBLGYnk7FRURSYpunIoUs1OoHhoI5crlM0GsXFixcRCoVw7do128dsbGxMe68Do4NhFEXB66+/brttn88Hn88Hr9eLZDKJffsOKHv37lf27t2vdHQcU7q6upTOzk6lq6tL2bt3r+JE8Wjnzp38e9FJSpCjW1EUbN682VbbJ0+e5MEGc3HOVVVVZtPNM8ZyjmY+707X1yRZbNdJun//fuXs2bO8JAIhBtO4XC5omoZYLIYPfehDtvt35swZJdfAE0nh43Q9nm18T3bs55OHHnqIrVu3DqlUCrquQ1EUJJNJ/ixQJnU8Hkc0GsVTTz1l+xhU8x6Y/prDEsl0IB2kBYDMFJXMNKFQCLqu89pejDG58ZFIJBIHiEa3saJayWmyfv16bNu2bcZ2F295y1tYa2srgGEDocvlQiwW4/9OmRmKoiAWi2HXrl222td1Pc3hSg7GVCoFv9+PseSFE4nEpGpfixu2fGzeSOKR6qlS/+3yzW9+03G2y2TIlORkjCEajcKyLOzfv185dOiQsnnzZlRUVCCZTOYlg2y8iO7JSLFNlttvv5390R/9EZLJJJdAdLlc8Pl8sCwrzeGkqiq+/OUv2z5GKBSC6CAFpr5+11iyqzTXFILxZrZATh/KWhSdJ5O9j3QfTNN0JGv605/+lDvCMjMZxL4lk0lHmTJ9fX1gjOHNb76dbd68ka1fv3ZGrV/Xrl1Ly762m4ltWRa6urry3a0xyTVAiurFimONxoXd/lZVLWPAiJw9AJSVlY37mTvuuINZloXa2lqYpgk7kpPAiDQ/HS8zc4bmIpfLxUsP2IExBsMweN3RqWDXrl24ceMGr2MqOqrF80okEtixY4ft9lVVhdfr5e+XpqaGOWNJNgxjUms0ycxRX1/LVqxY4Wgs5uIQcbLOoAAfXddRXFxs+/OPP/44AoFA2jMMgGeW0j7E5/OhpqYG99xzj+3zTyQSWLBgAe6444458xzPFaYqs3ksMtde4tdMZlV+4Qtf4HsbYLSCDe0/vV4vfvCDH+D8+fO2OtrQkP4Oo/WxRDKXkA5SiWSeU1dXw3y+4VpklOUwMDCArq4T0pImkUgkNiGjG5EZXUo/u91uuN1ufOpTn5qRfgLAn/3ZnwEYkTEE0g3QYrT07t27bW+mNE3jNRhF5ygA/PjHPx6zLU3TEIlE7BwKwNQZb6hdqu9HP1dVVdnajZ8+fVp54YUXpj3iVpThJILBYJpc2BNPPKHs3LlTKSoqmtN1ZR555BFEIhFeKzAej/PzNU0TqqrCNE0YhoH9+/fjsccesz1g/H5/mlFCHPfTRaZkpJTCyh2v1wtN09Iyf8dyRNpFdGQ7CUR47LHHEI/H+c90bzO/GGNYvnw53vGOd9jq7IULPYplWejt7YVpmojFYvi933vnjBmEr169CpfLxY1wovNqPOgeXbt2Db/+9a+nbeDv27ePG+XHI9uzSLJ4dusaer1efl1cLheKiorws5/9YtxzHhoaQjQaxdDQUNp6JVd27NgBqoMOjBhKxfcjPS8vvfSSrbabmxsZZV6applVhj8f/OIXv1BeeuklnD59Gv39/fz39L5MpVIwTROpVApr16613X4kEoFhGFy+3eVyoaGhbk44V8jpu2nTpjlxPnOJ5uZm1trawlpbW1hzc+Oo+xMKhRxlQuby3pvMGoMCKpy8F5988km+jxHfFZmyovS9E/WeAwcOKJFIBLFYDNXV1XLcFwiZztGp3F+NNb4z19kzsdb+3ve+xxYuXMgVoYCRjFEAaWvO/v5+R8GfRUVFaYGfmapTEslcYO5aQCQSSU6UlZXxei60IT179py0okkkEokDjhw5omQaOMeS5AGA3//938fWrVunfbN91113sXvuuQexWIwbWOPxOPx+PxhjPAo1Ho/DNE386le/sn0MMsKIm8dcDCCHDx9Wzp2b3Hson9I/mQ5GMpw6qSP4ta99bdo3z6JThja34XAYS5cuHfW3sVhszkom/eY3v2FLlixBKBRCMpmEy+VCKBTisqKapiGRSMDtdkPXdXzhC19wdBySlBZlmWfqmopjTWaR5obf74emaWmZfZnX0SmZBqXGxtEG7PHYvXu3QrUTqS+Z86uiKNB1HYwx/MEf/IHtPiYSCZ79HwqFbNeezicnTgwHa9pVBSBDuVgTfDrIlEAeC1FilyS+k8kkYrEYzp49a2uAeb1eqKrKs95zyUyOx+OorKxEKpVCUVGRncMBANasWZMmr5tpIBbv0bFjx2y1TYpGHo8HRUVFqKystN2/XHnhhRdw5MgRXLt2Lc2oTOdF0p9VVVW22yZFJpfLhUAgAI/Hw7O/Zzv0fEnJ0cKD3gNutxvFxcVYubKNbd26me3YsY3V19cyCvRzwlStYzIDuLI5dsfjzJkzygsvvMCDKoDhdy1l0onPncvlwv333++4r9FoFJWVlWhra5ubC+VZyHRnkI7HTKyzP/3pT7MHH3yQrwPoOtD4N02Tz9eWZeG5557D4cOHHQV/AuDveAokkkjmEtJBKpHMY1avXsnIeOd2u3Hw4GGls/O4tJ5JJBLJJPjKV74CYETOJpVKpRmyTdNMy9j80pe+NO19/M///E8MDg7C5/PxjQ4ZEWgjRdl1yWQSTz/9tO1jmKaJUCiUJpnrcrkwNDSU13PJhpjBkukstbuJJulVqueiqiosy4LP57OdRfrzn/9c6erq4k45sf4lZR5l23BOZuOfSCRgWRYsy0IgEEB/fz93BmaiqiqXnQWAixcv2loTiGN7or/z+Xx2mp4UzzzzDLvjjju4Y0J00vt8Pn7OtOH/9a9/jR/+8Ie210Pr169lsVgMqqpyecaamhocOnRkStdWoqSlaCARJZ3JMT5XMoQbGxtZTU1N3i1idXV1WLx4MTcuuVyuUZKSuWYyZiJK9yaTyQmlULPxX//1X2kSamKfgJGMZUVRcM8998DuNersPK6IDreZrJkMDGeR0vmKzqvxoIz/V199daq7l8aZM2eU7u7uUXLdQHqdS8qKFZ2JXq/XdvYoAAQCAYTDYSQSCZSWlk6YPQqMGEydZn/s2LEjTY6fsleB9DF59epV7Nmzx9bc5/f74fF40NfXh8rKypzOxymvvfYaLl++jL6+Pv5O9Hg8XIaengO/348HHnjA1nN08uRJRdM07vBVVZWP4+kk29phsgEf9J7v7+/Hli1bZt4rMQGZQWJisJvL5UI4HJ4zhv5IJIJAIMCDL2gtH4vFEAqFuFKGXcQ19Vh1F2kNYhdykNJ6uKKiwnYbVE/R7Xbzd5cIBaFQwMInP/lJ2+N2YGAAiqIgkUgglUqhtbW14Md+oZAZxJUt8GyyjsVscrdi5iTt3QD7cv1A7oG307lm+uhHP8o+85nPAABXBbIsK21PoygKD3geHBzEhz70IdsXeu3atYz2kh6Ph5dlW7VqVX5PSCKZYebGDlkikdimrqGeUW0UeslJJBKJZPI88cQTypkzZ+DxeLhDIh6Pc0ckRd3H43EYhoH169fjO9/5zrRttL/61a+ypUuXjsocETd/AwMD8Hq98Hg8eOyxx2A3o3PNmlUsmUzi1q1bUBQFHo8Hqqri5s2bOHEie+3RyUL9H8/Q5SSzNFM6iTb5ABzVIv3+978Pv9/PjYy0maZNfD4dWNXVy1k0GkV/fz9SqRT6+vpQXFw8puTqnj17lI0bN6K3t9eRwTDX6+tyubiTeKrZtWsXe9vb3gZg2JkAjBgwIpEIFEWBpmnc6Z1MJvGP//iPjo+n6zpM08S+fQeUzs7jynPP/WTKA88yrzkZCHVdh2EYXJY1FApNdVemjUQiAb/fj9bWVlZXV8fq6+vzMoc2NTWhsrISoVCI1x0Ws24y6y06hQy5dvnNb36DwcHBtGeYnLeWZXEnLDA83j/wgQ/YPkY0GoWmaYhGozPuUO/p6eFGPjuGx1QqhZMnT05hz7LT1dWV5gij6ydeR/E9RUZc0zRx9uxZ28fr7++HrutYvHgxn98mguY7CpqwQ3NzMystLU2TvhXPje6VaZq4cOGCrbaB4bl54cKFOHnytPLDH44txZ8POjs7lVgsBsMwuOGcniVS06A5YPXq1bbbp4xxMljPFSecKKVcCFlbdiFlFJon/X7/lEk5Tzc9PT0KnVu2muT5ckLlk0zntZPAje9+97sKSbKLGW4UmOJ2u3nwg8fjwTvf+U7bxzh9+rRCgQ4UBCcpPKhsGO29RanzyQRm5jr2p2ue/8u//Ev2ta99DeXl5YjFYiguLkYymeQlIoaGhridN5lMwuv14q//+q8nfVzLsqBpGoLBIL7//e/LxBrJnEI6SCWSecjy6ipWVlaW9gKfaQOIRCKRzCW++93vwrIsBINBGIYBXdfTaqkxxnitu2g0ive97334m7/5mynfbf/rv/4r+9jHPpa2SRQzXIHh90FxcTGvofX1r3/d9nFIplTXdaRSKezdu1/Zvfs15fx5exmJdpkKyZ9sNf7IWO9EZveJJ54AGWXFjLJc+m/XIHPx4iUlHo+jtLQU0WiUZ5FGIhEsX74862e+/e1vK93d3UoikbCdIQvk5jgi6cKpZMeOHeyNN95g27Ztg9frhWVZSCaT3FgNDDuQRGNcPB7HI488gt/85je2x+maNasY1dydSShaPJlMIh6Pw+12w+v1cmNhX1/fjPYvX/T09ChFRUXcgZEvmpqasHDhQgSDQf5sikERk72/1FcysNrNQjl27Jiye/futL7Q/8WxTM5SJ/XWjh7tVKiG9EzLMh8/fpzPuWJd2PGgbP/Dhw9PQw/TefnllwGMlgUWs45FeVpN07gst93+VlcvZ6WlpbAsC7/+9YvKT3/685xuFjlIXS4XlzHOlfXr1yMUCmXNQszMkD148KCdplFVtYwZhoGpdoyKJBIJfn/IgULXRszA37Ztm+22yTlDThUnAVWFCM1dM/2us4voEKW1KQWJz6VAcVIHEQMxxHVlod03WveKAQWrVrXb7uRPf/rTURmElAVOkMLCbbfdhjVr1tg+Bil0OKmVKpkevF4v+vr6eK1kVVURCAQwMDCQVsPdLrmuhZzI1tvlW9/6Fvs//+f/wLIsHoAdjUbh9XqRSCSgqipCoRDi8Tjf7//yl7/E17/+ddvvVlFOmjGGffv2KYcOHVL2798vnaOSOYf0iEgk85Di4mJomsYjKGlzLpFIJJL88Hd/93dKT08PgBFjJDBcl9MwDCiKAsMwEIvFuLHmc5/7HL7whS9MmeXiqaeeYh/5yEe47A7JWVG9IlE2CxjOhHv22Wdx4MAB25ugaDSKaDSKZDI5bQb2TIP0WEYgu8Yh0TEqZpK53W5omoa6ujpbDXZ3dyu/+tWvRjmpxf+PhVPD1pGOY4qqqli+fDlu3LiBUCiExx9/fNwbE4lE0NPTMyU3Lx6PT9m4WLduHfv1r3/NXnnlFW7IHxoagqqqPJNZ0zR+LVVV5dJrb7zxBv7oj/7IUceojvtMqXKImY0U9ED1hVOpFDeaTGVNv+kmmUzC7/fnLQOoqqqKFRcXIxAIcDk2UbIYmFzmaKYcoaqqjoxpTzzxRNoYE+WA6TjkTGxubsadd95pe+Kg8TLTWW/d3d2OPjcwMIB9+/bZuln5kG3et28fz87MnNfF95IYmErjoqury9axvF4vzxKxi1OpzTvvvJN/Lz4b4nzKGIOu67YljlVVnfYADiolAIxkCIkBS1SGZsWKFbbbJkcrrRvmyl5bfMcUmrNtLMT1LTnhaC2g6/qccV4D4E4gcjqKjlKn9yuX7Dmn2aWig5T66ySA7lvf+lZae5nHAEaCNzwej6Ma3eFwmAcOyeSCwoQxhrKyMpimiaKiIhw4cEjx+/0IBAJppRLskuvYvnXrluNjjMftt9/O/u3f/o3dvHmTffCDH4TH44HL5YLX64Xf74emaYjFYmnPOI37SCSCe++919HildQUZjpYTiKZDuSsLpHMM+oa6hlJyPn9fm78mWkDiEQikcw1/v7v/x7A6LppYj0UMgLEYjEMDQ3hr/7qr/Df//3frKmpKW9Wp3vvvZd1dnay+++/H8XFxQBGMlhExygZURhjvL7YP/3TP9k+XltbGysqKuJ1SiYTsWuHbEagTGOQE+OQGI0uGkXIAeGkjuA3vvENACO14IDhjSxlEonHniw9Pa8rwPC5nz17FpWVlTk58Lq7u20fPNfr6/V6J2WoyGTdunXsj//4j9kvfvELtmfPHmzfvh0DAwPcIBIKhWBZVtox6ft4PM4zed/3vvc5On57eyszTZNnqk53dkGmA0/sA2VB+Xw+JBIJ9Pf3T2vfppLBwUEMDAzg2LFjSldXV16sN7quc2e3OI9MNLfkilib0mmNz0cffVS5ceNGWoYryQiKx6FM94ceesj2Mfr6+uB2u2fcAXLt2jXbe5RUKuXIsVpSUoKioiK0tLQw+rLbxpEjR5QbN24AGD1GyLEkzvk0D4XDYduStLquY2hoyLbjbTJ7vy1btqTNpZnZo8BIPW27GaSWZeHMGfvvncnQ29vLJY7pOScnL8lgu91ulJaWYseOHbbGAwUCUaaiWLd1NkOO/tniIM1WM5P6Ts7SueTsOnv2nELjTqyBPBnGKsuQD8TyGDSeTNNEU1ODrcG1Z88epaOjIy0IFEjvu8vlgqZpMAwD7373u2339ezZswq9b6lciqSwGBgYgGVZiMViuHnzJgDgxRdfVqqrq7Fs2TLH2e+5fmbJkiW22x6Phx9+mO3evZv9+te/xic/+Un4fD643W4YhsGzYqmess/ng67rSCQSME0TbrcbpmniIx/5iKNjt7e3M3HtK52kkrnO3Ahjk0gkORMKhbgmfzgew6lTZ+SbTiKRSKaAb3/728qDDz7IKOOCjDFkdI5Go1yCl7IJDcPAPffcg66uLjzxxBPsm9/8Jnbu3Olonn7LW97CPvvZz2Lbtm0wDIM7TAYGBlBcXDwqm4kyAmiT9c///M+Oske9Xi/C4TAYYzh58vS0vWNEZyMZMLJt5pxmkAIYZVxLpVKOatr85Cc/Uc6ePcsaGhr470QDVr76TrSuaGa0gWaMYaqkkXI1NlqWhTVr1mBwcJBZlgVd13nmZTKZ5PKm2bJ3XS4XVqxYgeXLl2PFihVob29HdXX1KGMVjXd63sjonUwmeVupVIrLzz744IO2M86IYDCIcDjMx8JMZQqRAZjOnTLZ6H6YpoktW7bA4/GwTOcfGScZY1yyS3SkjFXPLFN6mv5tPKhflHFrmiZ0XceZM/bXpE4CFMZC0zSEQiEufy4+95nZcpMJthCzr1KpFNra2phdB++ePXvw7ne/mzt0RMcOtU334b777rPd1wsXepSysjKm6zqqq5czVVVx7tyFad8znD17Vrl+/TorLy/P+blijNl2zgHgcwj9X1VVrFy5kh07dszWeXd2dmLRokX8ZxpHqqqOki2m7y9fvowLF+xdX5fLhaKiItuOTnr+nDjsli5dyo2xY83ziqLg6tWrOH3a3vufgnmmk6tXrwIYdlaTugcAfp/E9/LWrVuxa9eunNs+c+aMsnbtWpZKMf6+mQvQ3OJyuWaNNK3oHKX1AADuaCgtLcWmTZvYrVu3JqzlK0rYZh4DGA5cyJR6FX/OrLmZ+UXZ3UNDQwgEAjAMw/azRAoI2RzZTpwcYm1PsY1s7xu7iA53YCRz20nN9G9/+9v493//d+4YSiaTfD4nhym9J5cuXYr3v//97IknnrDVcXqH67qOqqoq5vf7cfLkSWlPKxBKS0sxODiIhQsXYtGiRejqOgEAePrpHygAHEkrA7nvvxKJBBobG9mZM2eUqqoqRko89fX1LFvgaX19PaMMV6/XizVr1qC1tRXbtm1Dc3MzH8v0TvL7/XxcU6YsKSEkk0le4obqrv7pn/4pnn32WUfjMxQKIRqN8jE/lwJJJJJsSAepRDKPaGldwShqTlXVOSUnI5FIJIXIxz/+cfzyl79ETU0N36yQs5KcKZqm4ebNmwgGg/B6vYjFYlz+6fd///fx+uuvs3379uHVV1/FyZMn0d/fj/7+fm5sc7vdCIVCqKqqwpo1a9DS0oLbbrsNS5YsQTQa5cdIJBLQdR3FxcXcQCoaJmjjk0qlcOTIEfzFX/yF7Q1VVVUVc7lc6Ow8Pu3GgmQyiWQyCU3TpjyDTzR0/1bGkp06dcrWOT/11FP4zGc+w38m49BYDkanztHq6uXs+IlTyrq1q1l1dTV+9OPnpuze0D2YSO5UVVV87nOfQzAY5IZj+pxhGGlGuMzMXToOOUtEwycZaw3DgN/vRzwe5zKzJKlHxjIyPgLAww8/PKHk8FisWNHMyKCZSCSgKAo3oE4n2bKlKUORsqMCgQD+7d/+LS1rMTN7Q/yZHN6ZUrMej4dnJ5Lzmr7PVWYvkUjwNt1uNwYHB/Htb3+b/fVf/3XO98Htduf1WpPDSdO0NGdjZpbcZOvvUfYJrced1DL+7ne/i3vvvRfBYDDNcJXZR5fLhcrKSnzqU59iX/rSl2yN8VgshkAggNLS0hkZ08T58+cRCoVAdVEncpQqioI9e/bYOkZ9fT2jz9KcpKqqowCYPXv24O677077nTiWMqWQAeDMmTO2j0PPpV2jJfXDbjb5/fffz6h2o3hM0bnB2HB9vp07d9pqe6a4fPkygBHpTV3XeekBOk9aL91+++344he/6Og4NGc2NtYzj8eLeDzuSKWhEBDnxJmcF3Ih812kKAqfP0g9QtM0fOADH8ADDzyQVhJjIsZykIo/j/c+FN8j4v/dbjfi8Thf51y6dAn33HMPs1PygN7H4rNJY9qpgzQX8pFhRut4J+/Y//iP/1D+6q/+ii1ZsmTU+kDMqCOZ3fe973144oknbB2jv78fpaWlYIyhtLR0zgQ+zBWuX7+Ou+++G9/4xrfyOr/SenWiMf6ud70Ld911F4LBIFMUBQMDA4wxRioujOYfGt+U1Uz7VsMwuLoTZUKT4hMwvC5zu918ba4oCnw+X1rwXV9fH1RVxT/8wz/gP/7jPxxdh8bGRiYqYJmmKbOmJXMe6SCVSOYJy6qWMzLAGIYxbDBkckEnkUgkU8mZM2eUv/iLv2BPP/00XC4Xr/tMmyzDMJBKpVBRUQHDMGCaJnw+H0zT5Aba5cuXo6amBu9///sBDBsPyKFEG6OxDMY+n2+UY4gccGK0Nhk/LMtCIpHAxz/+cUfnSxK+M4FpmjAMg282s+HEsZGZaTvseHJzh7NpmigpKbHd36effhp//ud/npYZmXm8bP23i6IoWNHSxMLh8JQ6R4Hh9QWtMSZCrL0oZtfk4twWa8GK45k+S0ZOyg4lJx4A7sQk2d2Pfexj+K//+i/H16WoqAjRaBR+vx+RSIQ712Ya0SlM8wMFZ2RmgYhkGoDG+1unJJNJnmVDfVywYEFa1l0uxGIxLFiwAFu2bGF0z3fv3u24o5QVQs5RMaueEJ3IdhGdSG63G9FoFG6321HG8bPPPqv09/ezYDDIHaF0DmRcI0dPIpHAu9/9bnzpS1+ydYwTJ04pq1evZIwxnDhxSmlqamCnT5+ddqfOG2+8gdbWVgAT12kGhq/v8ePHbR2D6mxRYAEZHj0ez5iZH2Nx4sQJHqxBY4mMizRuyNhK2b8XL1601V9geDxR9r0daL60W2N6zZo1AEaeATFDjeY8emb27t1rq08zxcmTJ5VUKsWAdBUHyiykwGKPx4P29nbb7Q+P15GM8UAgAMaUWe1UEefE2ZJBKkJrN3oHkRPC4/HkVJs3m6z0eL8f6+/Gcp7SmqWoqAimaWLRokW2n1VRJSMzg9UJ+V6XjnUMem8FAgFEIhE4eefs3bsXd9xxB0pKSvi9pf6LayFVVbFjxw6IWX65cOHCBaWiooLR/m1oaMiRCoRkaliwYEHenaPA6Ezn8f6uqKgI8XgcHo+H7w9JsSZzDy62DSCt9I24N6SgRY/Hw9co4j6HgkzD4TBKS0vx6U9/Gv/6r//q+DpUVFRgcHAQgUBgTkqRSyTZkCNcIpknLF5YCYWlEA0PoaQohGQ8hn377EsnSiQSicQezzzzjPKJT3yCy3uS85McOlSHVIwYpU2Y2+3mmyEx6tnr9aKoqAjBYJAbYIH0WnkimRkedBwycFKdUMMw8Id/+IeOJViDwSDPWp1uyLkbi8XSIuXJ4AKAG6PtIDpEyDBomkmoqgLTTMI0h6Pd7daNPXLkiPLSSy/xuqN0D6mvorHWNE0kEglHmWZFRUVQXC4gjw6usbh8+bLj2qITqVqIBgTRgEn1QzOhv6GxLv7e7Xbj1q1beO9734tvfOMbji/MunXrWCJhwOPxYmgoAk3TYRgW+vsHnTbpCNEAKl7/TEMvOZDHM5SOlQWTT0SHoJj5aNfYrmkqUikTqZSJZDIOyzKwYcM6tnnzRrZ69Uq2evVKW89kd3e3Ijp86EvM/BMzcu3icrl+O88zGEYCwaAfqZQJw0hg/fq1tht88sknAQzfZ5rDaayLmZaapmHLli1obGy0fQxN02FZDA0NDcwwJlfHzimnT5+GZVlpEqjAaFlychReuXIFHR0dtgZtUVEQlmXA5QJMMwm32wXDSMAwEigttRf409HRkeZoISOn6LwWjY2MMXR0dNg6RlNTA6OgK7sBGSQna5fNmzenOWPpvMhZKmaav/TSS7bbnyn6+vpgmib8fj9/RoGRZ4nWDcuXL8eqVatsPUP9/f3w+/38eSSn+HSoONE8RpmS5Cii8xKlvu1ANWgTiQTOnp3+gAk7iGoIdL5UW5agtTgFrojOxGxf4ucyFQbETPGJPj8WFORHATROSKVSfH9B9dGz1bnPFVGmHwAP+qBrKma62UVUqqAs7sHBQaRSKUdBl1/96ldRUlKCaDTKHUli4CQpiwDDEv3vete7bB+jr68PgUAAyWQSpaWltj8/3yDHII3nzPlIzFK0Q7bauBONb3r/2mU4uGXiuZL64/V60/pCcy2dq7inGWt+ENVd6N1E7yMKkKI2KJjH5XLhIx/5yKSco+3trcyyDIRCARhGAoxZWLSoEpHIkNMmJZJZgXSQSiTzgJUr2xgZeIqLi/HGG2/MmAFbIpFI5iP/+Z//qfzP//k/4ff7uZFMURTu0AOQlvGjaRrPME0mk3yDT8bITElMMSKd6pDQBirTgUQSU6KMIDC84f/EJz6B73//+442Va2trWxwcNCRJGE+oPeaaIwUZYgTiUTO2Y1jIUoi0bWlDbAT6aHHH3+c99s0TS6vTGNBzKr0er3o6+uzfQwxQ6+lxZ4T18mxaGxm1rWcTOYdkO6oi0QiaYYUMtYR5Cyiz5EjgaSp/vu//xsbN27ED37wA8cGhLq6OkYOs1QqhWAwiGQyiWg0mpYdOx1QcAAZQLNdc/GeTPYrW/t2vjLlkoERY74TxLFBRijR0ZEra9euZWNdP/FL13VHhmsa+/QeEI10FChjh6eeegrRaBSMMW4Ap3ko0wlNcoJ2iUQi/Lk+f/78jDhDOjo6uCGQ3l/AsJGVahcDI3OwE7la8RnJFhRjh+PHjyv9/f1cnpUk8+j9HYlEAIwEb7hcLnR3d9s6Br0nnIzDZDLpqP7osmXLUFxczK8VqTaIzidVVXH9+nUcOnSooB1nIpcuXeLja2BggKt5iEE29CXWDc+FixcvKuJzTsb5YDCIe+65Z8rex9XV1czr9ULXdS7nTeOYaodS0J3dAJjJZiNOJx6PBx6PByQNnUwms9bMdno+2dY0Y70rc4Xuid/vRyKRcOTMGc9B62QNRsGd5EgWnTXAiFPKSV8J8VrRfOmkvV/96ldKZ2cn/H4/v/90PwYHB3nbJGP8iU98wvYxuru7Fboe2Zx0knQSiUSayoDoWKfnMhKJ2A4CHWuNNh7kiLcLydZOxHj7HrFvYvAGrROSyWTWjHNxrqJAMZrHaI+XSqVw/PhxbN26Fd/5znccD8jW1hb2WzlgmKaJSCSCgYEBXLlyZUbK50gk04l0kEok84CKigq++BgcHMTZs+eUM2dmZ90TiUQima088sgjyp133olr165xo6au61wGlwzvYq0/TdN4BikZV0laV9wwiTA2XN9OzOwQN1yZUbwDAwNIJpP40Ic+5HhTVVtby8jx6DSDcLJ4vV7upCLHoihbq+s6NE3jxmk7ZG5wxahfADxC3y6PPvqoMjQ0BMuyeF1aume6rsPlcnEHCABHxnCqwWkYBhYuXIi3vGXqjLJDQ0MYHBzkm3enGRQTEQgEuKOEnCN0behe0O8ZY7hx4wbcbjf6+vrw4IMP4r777lMm4+ypra1lFE3OGEMsFkMsFkNXV5dy7tw5ZbqdAx6PB6FQiBvfs11z8X6QYcXp10QZMhN9idk7brebO0md1g7OfB6dOkjD4TDPOiHnd7ZzD4fDGBgYcNxXqtVLASrkJKutrbb1bO7bt085duwYP2+Xy4VYLMavRzwe52PUNE1HDtITJ04oTg2K+aKrq4tnAtH7j6AagvS7eDyOffv22T6G6CAVf8506OfK0aNH+f0Vx7WiKAgEAvx3lEVDtTBzhd4PogRfriQSCZw7d87WHNXc3MwWL17MFRrcbjfPUPN4PDwwwzAMRw7qmaSzsxORSASpVIrfG1H6Wpw377jjDtvti4FodL8Mw8DSpUvzeh4iwWCQZzxRQAc5SgHwn52M7dnkII3H4zzIgwJFMp2XmQ7NiQKERMZ612Z7V9qB1tFOnW+ZQZOTdeL5fL40qc9sx8t8tzuFsuxoPdPa2mJ7zfqjH/0Ipmny9Shl1BYVFfHgUK/XC1VV0dzcjM2bN9s+RiKR4Ovr2fAszBRVVVWsvLycS0YD4OocFMxKwU1icGMuZDogKfB4PCb7LEw0P4y3RhaDnCmgiL5ovy/u18WgBlr/UPuGYSAej0PTNFy9ehX/+3//b6xdu1Y5duzYpAaj1+vla0bLsnD69Fmlu/u8cvz4STnIJXMe6SCVSOY4q1a1M8q0sCwLXV0n5MtNIpFIZogXX3xR+Z3f+R3s3LmTG9gpU4EcLWI9RtqkUJaGy+XimUai04MQI1Lpb8QMUvp3qp3jcrlw7NgxFBcXK88//7zj9wNJ62qaZrseWr4QpYvo+onR9/Q7u4b+zIhf8RrThpgyhOrr6x3JZJLThPpMxyRntuiItQtl0lKmakVFhe02csXj8aCsrGxUhnMmE2XoZfs78edYLMavjaZp/B5Q5gFFVMfjcZ5l9elPfxpLly5VHn/88UmvgxYuXMjls0Qp05mCjN70TI/FdBnyJorgpyx3ALw+smVZCIfDto6TadQWx4n4s51+h0KhUeMt8ysYDKK8vNxW22J75Hyl7BNSCSgrK7Pd5je/+U0AI4ZFCrIARmTbycDW2tqK2267zfYcZZomLly4MGP7h2PHjim9vb1pRnrK/qE5nzIwANiuf9nQUMcyx4+TzC+RV199Ne1ZI6c4zfWiMfXSpUuOpErJwGw389rJvWxra0NpaSmv70zrEspEpH2mZVk4evSo3eZnlK6uLvT19SEWi3H5bLrvdJ707t22bZvt9ul9BIw49BOJhCNFiFzx+/1ob29HaWkpfzZorMRiMT42xXPLlbGchYVIMBjkwQiUBUmBL2M5NCcKEJpMdigwen7JdLDQmmVoaIhnO1ZX2wueITIdtE7nM9qDUJvUFq276PdO2xeha0LX2okc9bPPPsudSdQ3WgPTug0Yfh4TiQQ++clP2j5GPB7ngSGSsenp6VEqKyvTpGUB8PdhpiKEHTKdo7msbykAxi5iBv54XxPNCaKjVHzu6V0jXqfMOt8UME2O0UQigc9//vPYvn07/vmf/3nSa7Q1a1YxOiZlX0sk8wnpIJVI5jDV1ctZMBhEOBzmEaQSiUQimVmOHTumvOlNb1I+//nP82zSSCQCl8sFn8+HSCSSVveOZKzEjNCxyCahRcYxMrxomgZN03Dz5k08/PDDuO222ya1qVq1ahUjeTrRWDLdVFRUwO12o7y8HMFgkMsIU+YRGUecOCGyGbRoYy86pAOBgO22v/KVr6RJlXm93rQaaLRBNQzDkXwxZSjpuo5wOIwTJ07YbiNXDMPAzZs3x7xe9P1EGYbiZzKzpRVF4c5JGmtUV5QirFVVRTQaxYsvvoj3vOc9KCsrUyZTj0dkzZo1LB6PIxqNcie51+vFZKO2J4Ou69w5RNkS4xlvJ3JQ5+rAHo+JPk9Ga9FpaDcLmwxLYrCC+Du7hv+zZ88qZLQbTyYtGo06quEIjARyUB9jsRh3NDnJoP3mN7/J5Vz7+/sBIM2QLdaMS6VS+MM//EPbx5gpaV2R06dPAxgOIKEMRpobSQqOAibsOuhInhhIV2QQx0FDQ52tF9vu3bvTrj8ZGxVF4f9PpYYlsZ3MyTSvU5bwVLNy5UrE4/E0dQPRgEv3w+fzOcrgnUkOHDigdHd348qVK3j99dfTspRprNE7ub29HbW19gKhKLhNfPeRcshUsXjxYjQ2NkLXdSSTSR6EQutMADyAyG4W6fnz5xWacwudeDzO+0mZzvnqd7ZgLvH7sb4ynZbZssyAkXmJMYaLFy/anoPHel87CZLyeDz8/UT9Ex1T4x1vIsT+0DWg+Q3InrE6EQcPHlT27NmDYDDIg+n8fj9E5w+163a78fa3v932MRKJBJ/HZQbp2KxcuZKtXbsWJSUl/D1Ba3WyTQ4ODsLr9TraQ2U+O06VSCYiFArlNGfnMg+I6w1yrNJ7hlSDyClKe3hqNx6P4+jRo/jkJz+JkpIS5TOf+YxiVxEiG01NDYz2VjTvzKRyiEQyE0gHqUQyhykrK4NpmtyA4ERGRyKRSCRTwz/90z8pixYtUv72b/+Wy74ahoFAIMDrZdKXGO1OmxYyUGUaJMipCiCtpqnL5UIkEsHrr7+Oz372s1i4cKHyyCOPTHpT5fV6EYvFUF5ejkQigRMnZkapgCLByXGRKW1ItW2GhoZstZu52SVI/ku8L04CkU6fPq38+Mc/5pkdiUSCS0SR0YUi4Z1mkKqqCsMweEbtAw88MGUW9ZKSkjTZqEypuVzWIpnOUgA8S5SMuoZhpMmJDg0NYf/+/fj+97+PBx54AKFQSLnvvvuUydQZzaS1tZWRVLPP5+NR3zNtKKbxQmNoImm/iRzUE31NxESfFzNdqZ6SkwxS0YE12bpvhJh1OVY2kd/vd2QQpT5RNiEZwEpLSxEIBOByuVBXV2O709/73vfg8XhQUlLCzwFAmnqAruuwLAsf+MAHsGbNmlmX8nLw4EEkk0k+j4vjBxg+v0QigVOnTtmWjxXrgmfeVxpbduf2CxcuoL+/n98Lmq/IyUhBAcCwxKsdamurWTKZ5I5hu8+NE1asWMHl/8hYS+9YcjolEgmEw2EcPHjQVttNTU2srs6eAzrfPPPMM3j11VcxMDCAgYEBruRA44LmEo/Hg3e961222iYjM2WnUu3CqczOobWQGEhE7yuxDIGmaY5qP8+WDFLRGSZKxNIzKL47cnVwjic3D0z8/hPJ9h5JJBI8Q0xV1UnVNBffg2L/7EJ7FFIlE+vXiu80J2NCfM/SO1Hss2maWLmyzfb88I1vfGNUjV3xWaY1NV3jT37yk7aOcf78eUVsS5KdQCCAJUuWIBQKceef6Mh0uVwIBAJIpYZrxNoh27M10T6Jxq5dqN+5zA8TrX+zfY729bS+pHeQ2+3G1atX8dhjj+FTn/oUtmzZgs2bNytf+cpX8ra3aW9vZWVlZbzEBD2TM6UIJZHMFDOrByWRSKaMxuYGRlGjbrfbkYSORCKRSKaez33uc8rnPvc5PPzww+z9738/1q9fj9LS0rS/GStCOTOCnTZeJPlKBribN29i586deOKJJ/DMM8/kbVO1detWRhlQkUjEkUxgvujs7ERDQwP8fj/i8Th30IXDYXi9Xui67jjbJvM6E5SRQVKHmqahtbWVHT9+3NZ1+PrXv47m5mbuzKS2yJgeCATg9/tx8uRJ232numPxeJxL4E5Vjbje3l7cuHEDsVhs1L+N5WjORjYjgmma3Oh7+vRp9Pb24uLFizh37hwuXLiAgwcPTunYa29vZ4FAgDtEGWM4cuRIQaQN3Lx5E93d3aiqqhol2yhed/H/k8GOgTUzs4agqHiXy4WysjJEIhFcv37dUT+yPZ9ioEiutLS0sN27d2P58uW8jWyG7VAohAsXLthqm/pGBjS32w1d1xGNRuFyuRCPx2EYBhYvXoxz5+y1/ZWvfAU1NTWoqalBf38/z5QRJcfpHIqLi9HU1IQjR47Y7v9M8vOf/xyLFy/GkiVLuIGeMnEVZbiuZ19fH44dO2a7bVGqPtO5Tt/bdWZ1d3cr3/nOd9jq1atRUlLCHQelpaUIh8MIBoNQFAW3bt3CSy+9ZKttcviQ8sSpU2dszUNVVVWsp6fH1md6e3v5eyORSHAlCnJmJJNJRKNRRKNRdHZ22mrb6/UikUg46le++OpXv6q89a1vZVeuXMH27dtRWVkJYGQeoXe9Uzl1sXRCKpVy7EzKFVVV0dPTg/LycgwNDfFxTJKtAPi77Pjx446OUehZc1VVVYwkxy9fvsyfYTH4JVv24kTvyEznSubfZ1vjjLWGzHYsn8+H/v5+eL1eJJNJ9Pb2TnCmo8l8N9oNcsrk9OnTWLRoEX9XETSWKBvQroNL7JsoYappWpoj04kT/8knn1Qeeugh1tLSwp9jcsCGw+E0mXtVVbFu3Trbx6AAipkus1DImKaJjo4OLFy4EJZlIZFIpGVL6roOn8+HaDRqO4OUnmHa/yqKMmWKeYcOHeLZr+ORbX7InBMoWIVkdZPJJLfVnjt3DoODg7h06RJOnjyJM2fOTGngcX19LaN61TSeY7EYf09JJPOJwl7VSCQSRyyrWsqWLFkCM2nwLAsyyHR0zJwEnEQikUgmpqqqiq1duxZr167Fjh070NTUhJKSEvh8Pr4JzxbpLkpN3rp1C6dPn8bLL7+MnTt3YufOnXmf+9va2hhJtaVSKRw6dGhOvl9aW1v45pGkushYGo1Gfyv36uYSuIZhTLmzzg5btmxi5DCnzbjH40V9fT2efPLJgulnIdPS0sKotio5f4PBIHbt2iWv3wxSU1PFFixYwLNQRce6ogzLWO7Zs7dg7tHmzZvZsKyqi9ffOnQo3cl+223b2SuvvFowfZ4PbNiwjmXWFiRpO9F4v39/Yczrra0tLJVK4eTJ00pb2wrW1WXPeLpq1Sp29OjRgjgXANiwYQOj7Nru7u6C6Ve+WLmyjRUVFSESiUDXdZjmsGO0rq4OTz/99Kw839WrV7NoNIozZ+w55yXTw/btWxk5NygDfmQ+U7Fnz56CuW+bNm1gpJJCcy8A7pyhvme+KwuBTZs2Merza6+9VnD9m+usWbOKaZqWltFeVVWFZ599fsx7sWHDBuZyubBv3755f79WrGhmJSUliMViPHs1Go3i2LGueX9tJPMTGeoikcxBli1bhmg0iqA/gKtXr8Lr9cIwDJw5M/c2nRKJRDLX6OnpUXp6evDss8/y39XW1rKFCxdiwYIFCIVC8Pl80HUdqqoimUxiaGgIt27dwvXr19HX14cLFy5M6Xzf1NTEgsEgD8CZqpovhQDJllJ0/LJly3ik7/HjJ5W3v/132a1bfWmyg3V1dSwfNWEmS3NzI6PsHmBEXkzTNEdZCfORtrY2VlpaimQyybPWAKRlUUhmhpKSEl6rjCS2KRskFos5qtk7VdTW1jIAPBvaMAzU1tbi0KEjaX93/fp1NDc3MrtZgVNJTU0NY8xZHbxCp76+lpEEOQWRkLRcIBDgWZqpVArV1cvZxYuXZvwaqKoKv98PALDrHAUwKcnOfNPY2MhobtV1Hc3NzezUqVMzfo3zCY0fkiI2TRN+v3/WOkdbWlpYJBKBZVmorq5mc3FemM1UVS1jjDH4/X4+f4lBlamU5UjpZCpoampghmHwoEPGGMLhMMrLy5FMJuH3+3mm3bp1a1ghOUlramqY2+3G4OCgo9qZkvxAYwcYlssfzzkKAH19fSgrK5v3c9eqVe3M6/Xy9ajf74dhGLyMgUQyH5EOUolkjlFdW8VI+mhwcBDnzk2tkVwikUgkU8/58+eV8+fPz3Q3AAw7jCijEnBez2W24PP5kEwm4fV6UVJSgoGBASiKgsbGRuzbdwDPPfcT/p5tb29nfr+/YIwl5MwjOU/LsnitvnxIrc511q9fz3w+H3eMUtkCkiuWzCxDQ0MoLy9HKpXCwMAASkpKsH//QWX79q1M0zQkEgnU19ey7u7zM74W1jSNBygcODCciXj06Ojak2VlZVBVFadOTY0MthOCwSBSqRQaGhrYTMqoTwXkLCSFAKoNOTQ0hKGhIbhcLui6jv7+/oKY12tqahjJxzuFMYa2tjbW1VUYWSLkzMmU75wrUL1qqnlIAW6zFXL0WpY1J4MmZjuLFy+GZVmIRqMAhjMwScZzWGLZPan5I9+Q9DMFDy1YsADXr19HSUkJbt26hWAwCL/fX3AytqQm43a7eR14yfRCwXFUsiGXoLhVq1ahu7sbixYtwsWLF6ehl4XHtm1bGJV1SKVSqKiowK1bt6BpGoaGhma6exLJjFFYbxmJRDIpllcvY5WVlVzbfzZvviQSiURSeDQ1NbGSkhIwxtDb2wu/3w9d16e0ltZMk0qlEI/HeXT7kSPD0oSHD3eM+tt4PM4zfAsBqlUHAIZhoLe3F4wxGTw1AfX19aykpASapsGyLMTjcSQSiSmtAySxD9V2TKVSPJABAF59dY/yu797H7t48SKqqqrQ3T3zwSW0Jp8oMIHm1kLC4/Fw2eK5CNXRprqakUgE5eXlePHFl/kJt7a2sMza4DNBSUkJ3G5nNfkIkrMsBER5RMMwYBjGTHcp71CGMtWyHRwc5M6r2YjP58NknfSSqYNUFdxuN68TTBnMfr8fyaRZMKovpIQDDK+1vV4vBgYGUFpailQqheLiYv789Pf3Y/PmjWzv3v0F8SLSdZ0rskgH6fTT2trCvF4vKIvX4/H8tn78a+N+7kc/+pECALfffvu8jBJdt24NGxgYgKZp0HUdiqLg5ZdfKYhnSiKZaaSDVCKZQ4RCIWiahmQyObz5lWWGJRKJRJInVq5cyYLBIGKxGBhjOHny5Jx/yaxY0cxUVc3ZQRAMBtHb24tQKIR169axma7LSlmOiqIgkUigEDLpCp3W1lbm9XqRSqUQi8WgqipCoRBM05zprkky8Pv9GBgY4EGBotPhJz/5WUGNdY/Hk1Om/Wuv7VN27NjGtmzZxF57beZrZFVXVzOa9+Zi1rTL5YLH44Hb7QbJzSmKgitXrqT93fHjJ5X29tYZN6gOv4smp9igqmrBZGMFAgGejagoSkFk6eaTVatWMXL6koJDUVHRrFXdaGhoYHSvCsXJLkmHMulcLhcMw+B10wEgGo3C5XIXzPjzer1QFAWmaYIxBq/Xi3g8Dq/Xi+vXr8OyLIRCIS4rXkjKJ5T1mkqlCsbhPJ/w+XyIRqMwDAPLly+Hz+fDk0/mLlu+c+fOGV9fTSfNzc2srKwMLtfwtYvH40ilUgiHwzPdNYmkYJCrGolkjtDY3MA8Hg9isRiPZJvN0akSiUQiKRxWr17Na5UwxgomQ3Kq8fv9sCwLXq8XLpdrQoPgkSNHlJUrV+LWrVsznlVLzl0AvC6jZHxWrlzJiouLoes6PB4PAoEAbt68iWvXrqGzs3NeGVMKnXXr1jDKtBzOikmO6fRZsaJ5Rq2qra2tjLJ4cnkOd+3arRQVFWHlyrYZtwaHQqHfyjK6Csapli/Wr1/LgJEMv2QyiYGBASSTSSxbtmzU33d2HlfuvvvOGbsndXV1LJVKgTE2KUfB0qVLwRhDc/PMPhdNTU2MpBGTySR3NswlSktLQTUWPR4Pbty4gVgslua0mk0UFxfzTFiXy4WqqqoZn6MkI6xatYqv1WOxGM9s9Hq98Pv9UFUVpmkilUph1apVM3rvWlqamMvlSnPW9vb2wu1249q1a2htbcWpU2eUAwcOKfv2HVBCoRCSySTa2mb+vbhy5UpG8xXVr5ZML4wxBINBlJaW4r//+1fKj3/8nFyjj8HKlStZIBBALBbD0NAQBgcHYZomBgYGsGbNmpnunkRSMMiZXCKZIwSDQR6BR3WOUgUUZSeRSCSS2UddXR0rLi7mMnhkyJgPEaf19bWMMQbDMODxeLhE3kQ888wzfJPe3NzMTp06Ne2b9urq5SwUCnHpQsaYzPYYh7a2NlZSUgIAXE5XVVVomoaenh5pdClAyAhMhslgMIhLly5l/VuqMzlTeL1e7tTK9Tn85S9fmPFxV1tbyyg4xLKsgsreyQeBQAB9fX383lAtTNM08etfv5j1+r/wwm9m7L6QpDSQmpQj8Qc/+IECAL/3e7/HTp06la/u2YayWEiutdAyxCZLU1MTi8fjUBQFhmEgGAzi1KkzM/5cO6Wmpobpuo5YLAaXy4VYLCbfjwVGMBhEPB7l61bKyOzv74eqqnC5XNA0nWfMzyTFxcUwTROJRIK/I48fH1GmOXEifW5atmzZb2W4Zz77NRAIcEl/er4l08fatauZoigIh8My+HMcWlpaWDAYhNvthmEYME0TgcBwOYrDhzsUADh9+uxMd1MiKRikpUQimQM0tTQykrpRVRU3b97Eia6TyunTZ+WmRSKRSGaQmpqaWWvtW7lyJausrAQwXBvI5XIhlUohmUzOeQdpfX0tKy0thaIoPOjI7XbbjhIPBoNT1MPxKS0tTZP0lA7S7Kxdu5atW7eOqaqKaDSKaDSKRCIBn88HRVEKRoZOks6mTRtYNBqFz+eDpml4/fXXkUwm0dPzetZ1r9vtxubNG2dkLl61ahWjAEYnY2r16pUz9g4pKirifaavuUJrawuLx+MAhjPsy8vLAQxnIzc1Nc1k17LS3NzMKFApX07EH/7whzO2T1yxYgWjTESqaw5gTmVilZaW/lbSdPjdO5ufn9raWlZUVATTNLls69mz0s5QSKxcuZJRtjLVH41EIjhy5Khy8uRppavrhHLsWJfS19eHwcFBXLt2DatXr56R98uKFc2M3i2KooAxNuGz/9hjTyiRSAQ+nw/t7e0z9l5cu3Yto/6mUinoul5wdcPnMitWDEvFJpNJGIYx4wFwhUZ1dTVbsWIFW7duHQsEArAsi8vpkqOUnKMSiSSdubMClUjmMZWVlYhEIrw22rmzssaYRCKRFAIXLlyYdfNxXV0dW7BgAdxuN6LRKJfUjcfjiEajUFUV586dm3XnZYeioiIwxhCLxeDz+bg6g10no8fjQVtbG4vHk+junp7MkXXr1jBg2BhrmiZ0Xf9t1oCskQQMG3rFrGjDMKDrOjRNw61btzATGb+S3NmwYQOjzOi+vj6Ul5fjwoUe5cKFnjE/o2kakskkNmxYxw4cmL66wCtXrmShUAixWAyWZcHlctmuZatpGhob69mZM93TOi7Xrl3LSLqYskfnSpDFqlXtLBQKYXBwkMvrxmIxHDx4OOdr3Nrayo4fPz4t96ShoYFR9ujwfVAmrIdth7q6Ojad7/SVK1cyv9+PcDgMt9sN0zTnnIT5pk2bGAB+n1RVnbWyugBQXl7Os0apRrekcFi1ahULhULo6+uD3z+cGRoOh9HZOXqOOn9+xE60efNmtmbNGnbkyJFpe/7Wrh12ylJ2ta7rfK06ER0dx5T77ruPqaqKdevWsUOHpu99DgzPXT6fD0NDQ/D5fDz7dTbu9WYjjY2NrLi4GNeuXYPf74fL5cIbb7wx090qCKqrq1koFOK14il5xuVyYWhoCCdPnpRjVCKZAOkglUhmORs2bGDRcAwsBaTAoLC5YbyQSCQSyfRSX1/PSkpKoGkaz3ZijEFVVVy9ehVnzsxeabhcaWhoYMN1mjQMDQ1xo2B5eTmeffZZ2+e/Z8+eabtmdP/cbjeSySRSKcDt9mDv3v1z/r5NxMqVKxnJOJqmCZfLxcd2JBKB3+/PuT6kZGaoq6tjFLiQSjF4PF64XG4kkxM7HF96aacCAO985ztZKoUpN6rSs0gOeGDY0ZlIJGxnlQ8MDCEQCGHr1q1sOuaT2tpatnDhQkSjUZCEHfU5n065mWLNmjWMMYZoNA5dH66lPSyVaG//ZJomtmzZwm7cuIHu7qlzXtfX17Pi4mJ+TEVRoCgq4vFk3o5x7tw5paamhk2HkX/t2rVM0zT09vaCzoukKucCDQ0NjOp0plIpxONxBIMVPHtntlFTU8PKysqgqiosy+KKGnPpns1mampqWHFxMTweD5c+NgwLuq7DsiZOshwYGMBwveuVLBwOpzlP801VVRWrrKwEYwyapsEwDCQSCbhcbvj9QVy/fj2ndn72s58pANDe3s62bt3Krl+/PqVzMDDyHFDWKAUcTleQjGRYstzn8wFwwev1wzAsMGaiuLh0prs2Y9TV1TG/34/h6wIkEglYlgWfz4fBwUHous4l7CUSycTMvlWaRCLhNDQ0MLfbjVgsNlxzNI+ySxKJRCKZOmaqNmUmNTU1rLS0lNf/Ifk0v9+PW7duzZvNf21tLQsEAvD5fPB4PAiHwyguLkY4HMbRo0cL+hrU1tayUCiEQCAAxlhaPafZnLEyGZqampjb7U6raWcYBq8ryhiDoiiIxWIyqrrAqaqq4s8mObbFml921r0//vGPFQDYvn076+vry/v8Rk5cANyhQNmuZKAiWddcOXPmjHLXXXexq1evYsuWLWxoaAhdXV1TMmbb2tpYMBjkc0gikYDb7cb+/bM/yKKlpYX5fD643W7E43FuQAwGg9x5bYfTp08rK1euZMFgEK2trcw0TZw+fTpv16mxsZGVlZVxqfR4PA7GGLxeL8LhcN6zeRljqKurY5Zl4eLFi3l/LkgOW+w3ZSfPBeNtVVUVKy4uRlFREZczBIbHyenTp2e4d/apqqpiCxYsQCAQQCKRQDgcBmMMuq7zsSiZWRoaGvicRu9Fj8cDOwFA4vqntbWV3X777Wznzp15ff7p2fB4PHyf4XK5EI1GceLECcfHoqzz2tpa1tjYyJLJZN7nLgAgqdJkMoloNJpWz1EyQlVVFVNVNe9O9pqamrT9WSQSQSKR4Bmk820uWr16NaMsUcMw+BqT9jzRaBRDQ0PzZv8ukeQT+dBIJLOU5dXLWGlxGYLBIJLJJHRdRyQSQTKZlC9EiUQimSU0NjYykvib6ghooqamhgWDQfh8Pr6xoqxRkrkqdIN4fX09I+kgADxTK9cMmPr6eqZpGjweDzfaplIpfg08Hg+8Xi96e3unTHK1qqqK9fT0KMDwPbGTvUMZkXQN3G43VFVFMplELBbjBtrpkE2sqalhmqbBsqxpk15uaGhgw5lUCo+OpmtAWW6macLtdkPXdS4NDQwb5Qvd6T0R1dXVjAyiNHanwjA4UzQ2NjJN0+D1erkUsmmavBYyYwz5kgRsaWlh5HQ6e/as0tDQwHKprVddXc1o/hDHn6qq3CGaSCTy7oRvaGhgoVCIS2hblgXDMGw/e9XV1UzXdW50pEBLqpNIAQS6ruftWtuloaGBeTwefv8Nw8hpnNN7VdM0fn9oXmCMcbnjAwcO5O28mpqaGNVs7evrQ1FREfr7+7Pel+rqakZ9oTmMaly7XC6UlJTANE3uTNc0DYODg9Mm/11fX88YY6Av6muuc0xNTQ1zuVzw+XwIBAKjHAqDg4PTtt4Zi6qqKv4OsXNuwPCcQWOI5NkB8FrliUQChmHYdlZNlurqaqZp2oS1Qen90dPTo1RVVTFxDaGqKkKhEF8b0pzgcrlQVFSE4uJiPP/883PmXTNbqK2t5SoYfr+fz2uWZSGZTPLsclVVJzVft7S0ML/fD2B4rZRIJPhzm6vjq6WlhVFfdF2H6MwZVjcZDugPh8N5X7dUV1czChCkutl2M+Obm5uZx+PhDjiyr5WXl2PJkiV4+umn5fgfB9pfkWJLLBYD7XVyobm5mbndbng8nrS1Ca13XC4XDh/OXQ5/slRXV7OpXl/T+wgAdF3na/vMtSXJnLtcLv43iqIgkUjM+n2NRFIIyIdIIpmlrF67ioUCRUgkEhgYGEBxcTHi8Tgsy5IOUolEIpll1NfXM9HBk0wmbTn8xoOymjRN4/JQVFMzGo3Csix4vV4MDAxMaFgrRKqrqxkZjMjYSbWMMiOLaQP6/7d3b7Fx1nf+xz/PaU72+JjYIXV8jDM4iXOABLIBVaVUTXtFe7XaSlCpN1ysVitE1dWuuKi2q3/3L6R/q5Waq7+2aqFbqaj0Bm2FQFXLQtokhQIJAQKObSCQhIPt2J7Dc/rthXmeDiEnSOzxxO+XFI3n5HxnPM/j8Xye7/cXRZGSD0eT80k4GkXRde0Euhqjo6NmaSRalI57ra+3vtakey4Jh5NRfr7vX9PR+NdDf3+/yeVy6binC+uvX4/tcurXN7vYWM8kxJH0iQ/wk6+TUYDZbFZBEOj5559vutf1Z5GEdZlMJg1aEhc+98k2cjnLPUr1SuvXJV0m9T/POI6VfGh56NChZS2wVCqlT1D981cfyl/4fCZBQvLYkiDX9/1l369u2bLFJNtU0kVUX1/9qfTXfUb95Jn6DyBX2++BoaEhk4SH0tIa0Reqf00n3eLJzyAJ1yWlv19t217Wv5e2bdtm5ufnlc1mVSwW5fu+Wlpa0kC7Xv1aYcn+3RijIAjSfV2jJ04kU4uSUDCpu/5U+uvPIXk/U6lUGhawX63kYIHk+b/w8SWS88m2Ii09Tsuy0vcPxpiGTyUYGhoy9a+p+n/J/qt+e0n2a8ltklAsGUMfRVHD31tci/oD4pLX5eVc6fffSnSs1b+fuXD/kBw4EQSB5ufnV+TgqKSLL/n9krjYPqBSqaQHHyZjpoMgSF97yzUB4VJGRkZM8rO3bTs9oKFeff3J0gvGmHQKyblz526og9BWQjK1IXktu66bviYux/f9dJ+UvFdJDpBK3gc26rPOoaEh09ramr5ekm3ywvfZl3Phvrf+VPrk3zf196mfhrPS2xCwVrBhAU3o5q0l09bWpsX5svL5fLrI/bFjx9imAaDJjY2NpR9EJn/MO46jarWadnjWj2m8WACSjEfs7u7WmTNn0m7RpLPB8zxVKpWGd3EAAAAAAAAAjcAapEATamlpURiGam9v1+zsLB2jAHADudKR+l/72tfM2bNnL9ldKEkLCwtph1CyTg8AAAAAAACAJXxgBjSZ0tgWUywWlzqAnIzK5TIBKQAAAAAAAAAAwFWyG10AgKs3MjpsisViukB5EATpOikAAAAAAAAAAAC4MgJSoIkkC53HcSxjjFpaWlSr1RpdFgAAAAAAAAAAQNMgIAWaxJabR01PT4/K5bLy+byiKNLp06cZrwsAAAAAAAAAAPAZuI0uAMDVaWlp0QcffKBsNqtCoaA/HTpMMAoAAAAAAAAAAPAZEbAATWDb+FZjjFFbW5t839cLf/4L2y4AAAAAAAAAAMDnwIhdoAmsW7dOhUJBi4uLsm02WwAAAAAAAAAAgM+LpAVY5W7bt9dEUaTZ2VnlcrlGlwMAAAAAAAAAANDUGNMJrGKlsS3G8zzFcaxisahyuaxjLx1nuwUAAAAAAAAAAPic6CAFVrFCoaCWlhZls1nNz89rdnZWmwb6TKPrAgAAAAAAAAAAaFYEpMAqVSqNGstIjmVLsVFXR6fenn7Henv6HTpIAQAAAAAAAADAsvubv7nd/Md//Ng88MA/Gkm6+eYtN0QTFwEpsEp1d3fLGKPz588rDEM9++whglEAAAAAAAAAALDshoYGjCR99atf1Ze+9CXdd999uv32vcb3fQ0PDzZ9SEpACqxCO3eOm4WFBWUyGbmuq2Kx2OiSAAAAAAAAAADAGpHL5XTbbXvM/v37ddNNN6lUKmnfvn2SpPb29gZXd+0ISIFVZmhowHR2dsqyLC0sLCiKIlUqlUaXBQAAAAAAAAAA1ohqtarBwUF1dnYqySwOHDggY4xqtVqjy7tmBKTAKtPe3p6O1c3lcnIcR9VqtdFlAQAAAAAAAACANaJQKGjjxo3yfV+O48gYoz179mjTpk0yxqi/v6+px+wSkAKryPDwoHEcR5VKRe3t7apWq5qbm7shjsYAAAAAAAAAAADNwbIsdXV1yXEcBUGgKIq0fv163X333SqXy40u75oRkAKrSCaTkW3b6ujoULFY1CuvvGpNTk5bp05NWY2uDQAAAAAAAAAArA2VSkWe56WTLguFgiTp61//uizLkuM4Da7w2hCQAqvE2FjJ9PT0yLZtzczM6MknnyIUBQAAAAAAAAAAK25oaEi5XE6VSkW2bcu2bfm+r71792rPnj1yXbfRJV4TAlJglWhtbZXv+6pWq/I8r9HlAAAAAAAAAACANaqjo0OSlM/nZczScqOZTEZxHOv+++9XGIbasmWzGR0dacq1SAlIgVVgbKxkFhYWNDc3J9d1lc/nG10SAAAAAAAAAABYY4aHh83dd99thoeHtWHDBrW0tMiyLBljZIyRbdsaHx/Xvn37FASBcrlco0v+XAhIgVUgk8mou7tbHR0dqlarqlarjS4JAAAAAAAAAACsIZs3bza33HKLvvGNb+iOO+5QqVRSsVhMr/d9X5LU29urBx54QMViUblcTps3DzddF2lzDwgGbgA7dmw3rutqdnZWQRCoVquptbW10WUBAAAAAAAAAIA1pK+vT3v27NFXvvIVFYstKhQKsm1bcRyn65BGUaQwDLV3717dc889euyxx/Tmm6esRtf+WRGQAg3meZ48z5PjOKrVanr99TeabkcCAAAAAAAAAACaW29vrwYHB7Vp0yY5jiXP8xQEQRqQep6nOI4lSZVKRd/97nf1wgsvqFqtmqmpt5oq2yAgBRrollt2mTiOFYahHMeRbTP1GgAAAAAAAAAArKz+/n5TLBaVz+dljJFl2XIcR5ZlKY7jjy+zZFmWstmspKUGsIcfflhbt25vqnBUYg1SoGG23Fwytm2rUCjI930dPnzU+stfXmq6nQgAAAAAAAAAAGhuhUJB+XxejuMoiiK5rqswDD8xWjcIAkVRlN4nCAKVSiX9+7//n6Zbg5SAFGiQzs5OBUGgSqXS6FIAAAAAAAAAAMAa1tLSomw2K9d15TiOHMdJA1HbtuW6rjzPk2Ut9XnVajXl83n5vq97771XDz/8f40kDQ8PNkVYyohdoAF27hw3UeDLshwtjevmWAUAAAAAAAAAALDydu3aZUZGRtTT06NisSjLsmSMpVyuIEkydZGnbbsyRooiI8mWbbvq7b1Jf//3/yDbds1PfvKTxjyIz4iAFGgAY4yMMQrDMD0FAAAAAAAAAABYKePj42bXrl3aunWrhoeH1dfXp02bNimXy13xvoXCUngax7GiKFI+n9f9998vSXrwwQeXte7rgYAUWGE7d46bpaMvjOI4Vq1W08TEBGuPAgAAAAAAAACAFXPTTTfptttu05133qmBgQFlMhlJS6GnbV958mUcx8rlcgqCQPPz8yoWi/rWt76lfD5vfvzj/6eTJ99ctdkHcz2BFdTf32fy+Xw6pzubzabzugEAAAAAAAAAAFbC8PCwWbdunTo6OlQsFtXW1pauQ+o4zhXvH8exwjBUFEXyPE/FYlFhGKq9vV333XefHnnkEf3d3/3tql2PlGQGWEHj49tMsshxrVZTEEQql8s6deoU2yIAAAAAAAAAAFg2AwMDZnx8XJs3b9bY2Jj6+vo0ODiojRs3qqOjQ9Jfu0eNuXK2aVmWgiCQbdtpqFqpVD4+v3TdT3/6U/3oRz/Sm2+urhxkVRUD3Mg2bx42HR0d8n1f+Xxe1WpVL710jG0QAAAAAAAAAAAsm7GxMdPX16ft27dr//79Gh8fV6lUuuhtoyi6qg7SesYYlctlZTIZeZ738aWxFhcXValUdPLkST3++ON68skndfz4iVWRi6yKIoC1YNeuHcZ1XYVhKMdxFIYhASkAAAAAAAAAAFgWpVLJ3Hrrrdq7d69GR0fV29urrq4utbe3q7OzM+0AjeNYnuelnaNXs/6oJJXLZRUKhU9dvtRVuhScGmMUx7GMMTp79qx+/etf62c/+5lefvl4Q/MRwhlgBYyOjpjOzk4FQSDP8+T7viTpxRdfZhsEAAAAAAAAAADXRX9/v9mwYYN27Nih9evXa9u2bdq9e7cGBgaUz+clLY3GTU6TUbrJZVEUybbt9PyVRFEkSZ/oOl0KRqM0aA3DUK7rKooinT17Vh999JFefvll/fGPf9QzzzzTkLCUcAZYAXv23GKy2azm5ubkuq7y+bzK5TIdpAAAAAAAAAAA4Lr55je/ab785S/rrrvu0ujoqBzHkTFGlmUpDENls9n0fBzHsizrE2Foct2VXHi7IAjkOE7ahWpZS52jyZqmSVgaRZGiKFIcxzp37pxeeuklPffcczp69KhOnTqlqam3ViQ3cVfiPwHWsu3btxrP87S4uKhsNitJ+vDDD9XV1dXgygAAAAAAAAAAwI1ibGzMdHV1qbu7W8ViMe3aNMYok8mkXZ7VajXtJk06SJOvr3a8bhK4SpLruunao0EQfPw9l8LX5PJKpZLeLggC5XI59fT06Pbbb1d/f7927NihQ4cO6ciRI+bo0eeXPSQlIAWWWSaT0dzcnHK5nNatWyfLsvT883+hcxQAAAAAAAAAAFw3r776qtXX12emp6e1c+fOtHPTGKMoimRZlmzbTsPR+jA06fa88PJLMcbIdd1PdIkaY9IuUilWFEWqVqvKZDLK5/OKokhhGCqXy+nYsWN65pln9PTTT2tiYkKVSkVvvnlqxbITAlJgGW3fvtXk83lls1ktLi7qySefIhgFAAAAAAAAAADL4qmnnrJmZ2dNGIaamprSli1b1N3drdbWVlmWpSiK5DjOJ9YhlZSuO2qMSQPPS0luk4Sh9d2hyfeoVMoqFAqybTudsDk3N6cnnnhCjz/+uE6cOKGJiUlLkgYH+83Vrnl6vRDWAMvottv2mHK5LNu2VSgU9Kc/HWGbAwAAAAAAAAAAy65UKpm9e/fqwIEDOnDggNavX59eF8dxOl436Sytv+5iAWn9ON4k0ExG+LruUk9mEASqVqtqbS3ogw8+0Pr161Wr1XTw4EE9+uijmpmZked56e1rtVoalK4kOkiBZbJ1+5gxxiiXyykIAgVB0OiSAAAAAAAAAADAGvH6669br7/+uj788EMzNTWlsbEx9fX1qVQqqaOjI72dMUbGGF2ui7M+HE34vp92jYZhqCiKlMlkVCwWJcXq7u7WI488ooMHD+rcuXPK5XLpOqgnTrzW0IYyAlJgGQwM9ZtcLpfO0pZEQAoAAAAAAAAAAFbcb3/7W+vVV181W7du1R133KFcLifP85TJZOR5nqSLB6CJi113YahaqVSUyWQkSTMzM/roow/0/e9/X48++l/W6OiIiaKo4aFoPQJSYBm0tLTItm2FYSTf9+W6rmq1WqPLAgAAAAAAAAAAa9DU1JQ1NTWl2dlZc/bsWX3nO99RZ2enurq6lM1m0zG7yVqkiUsFp5ZlpeuNhmEo13WVzWZ1/vx5PfXUU3rooX+R4zgaGhowb7wxsWqC0QQBKXCdjY6OmNZCi6Iokuu6iuNY5XI57SQFAAAAAAAAAABohPfee0+HDx/Wu+++q/379+uuu+7S6OioWlqWco1KpaJ8Pi8pvmg4moSncRwpk3EVBMmYXaNqtaz//M//rwceeNAaGhpalcFogoAUa9bAwICZnp6+7htnoVBQEASK41iZTEaWZWn9+vXpgsMAAAAAAAAAAACNMDk5aU1OTurw4cM6ffq0OXv2rO69916NjIwoCAIVi0XVajVlMpfPNGzbTtcg9X1flmXphz/8of71X//NGhkZMhMTk6s2HJUISLGGTU9PW339XzDvvHX6um2kY2Mlk8vlVKlUlM1m5XmeDh3606reCQAAAAAAAAAAgLXH9329/fbbOnXqlEqlkjzPUxRFsm37kvepX3c0k8nI9335vq+DBw/q5z//uSQpDMMVqf9aXPoRAmuA4zjX7XsNDQ2YYrGoMAwVx7Ecx1GhULhu3x8AAAAAAAAAAOB6qVarqlQqOnTokJ599lnNzc1pZmZGnufJGJP+u1Byme/7ymQy+uUvf6l/+qd/ttrb23XnnfvN9PTbq75xbNUXCDSL3bt3Gtu2FQSBXNeVZVmqVqt65ZVX2c4AAAAAAAAAAMCqs23bNtPW1qYNGzbonnvu0Z133qmNGzemI3Yty0o7RutZlqU4jvXss8/q29/+torFohYXF2VZllb7eF2JEbvAdXHrrbuN67qq1WpyXVeFQkHnz5/XunXrGl0aAAAAAAAAAADARb3yyiuWJA0ODhrXddXb26svfOELaZdoEo4mp8nlcRxrZmZG3/ve91QsFnXs2CtWf3+feeutd1Z9OCoxYhe4Jv39febmm7eYbDarKIpkWZbm5+d17tw5zc7O6g9/+J+m2BEAAAAAAAAAAIC1y7IsnTlzRsePH5fjOGkQerERu5JUq9X00EMPyfd9VSoVSVKzhKMSHaTA5zY0MmjWd69TFEXpIsSO4+iNNyaaZgcAAAAAAAAAAAAwOTlpDQwMmNOnT+vcuXPq6bn8hMwjR47od7/7nbLZ7CVD1NWMDlKseYOD/Z95y92xY7tZ371O5XJZtm2rVqvJsiy1tbUtR4kAAAAAAAAAAADLKo5jRVGk5557TnEcyxiTrjWaCIJAlmXpBz/4gdrb2xXHsVy3+foxCUix5l1sceFL2bJls9m791Zj27Ysy1I+n5dlWWptbZXjOFpYWFjGSgEAAAAAAAAAAJZHpVLR+fPn9dZbb2liYkIzMzOSpDAM05DU8zz96le/0rvvvqswDJXJZBQEQSPL/lyaL9IFrrOrbf3evn2raWlpUa1WUz6f1/z8vGzb1ssvH2ekLgAAAAAAAAAAaGpzc3M6c+aM4jhWsdiiffv2yXVddXR0SJKiKFIYhvrFL36hMAwlSUlDWbMhIMWadzUB6fbtW02hUFC1WpVt21pYWFA2m23KtnEAAAAAAAAAAIALnTx50nJd17z//vuq1Srq7e2V4zjq6upSHMeqVCo6fvy4Tp48qfb2dkVRJN/35ThOo0v/zEh3sOZNT7992UMb+vv7TE9Pj95//31ls1lFUaRisaggCJpy4WEAAAAAAAAAAICLOXHihCVJYeibY8eOadOmTVo6HyqXy+mxxx5THMeyLEvGmHSt0mbDGqTAx0ql0U9twSMjQ2ZwcFDvvfeeMpmMLMuS53nyfV9Hjz5v9fT0NKJUAAAAAAAAAACAZRMEgV588UVlMhnVajUZYxQEgZ5++ml5npdO3PQ8Lx2320zoIAU+5vt++vXAwCbjOI56e3tVqVRUKBQUhqGy2awk6c9/fsGSpCee+O/mG6wNAAAAAAAAAABwGbZt68SJE5qdnZUkWZal3//+9zp//rxaW1tl27bCMFQYhnSQAs1scnLakpbC0XXr1kmS4jiW7/tqa2vT4uKihoeHdeTInwlFAQAAAAAAAADADcv3fb322klrcnJSjuMojmP95je/UVtbmyTJ8zzNz8/rxInXLNsmbgQAAAAAAAAAAABwA3jwwQdNpVIx5XLZfPGLXzSlUsns3r3bbNu2zZRKpeZrHf0YkS4AAAAAAAAAAACAT3nxxRcVBIFOnjypd955R47jyBijMAw/sXRhs2ENUgAAAAAAAAAAAACfMjU1pcnJSR09elTGGGWzWdm2Lctq7tUICUgBAAAAAAAAAAAAfMrExIT16KOPmjfeeEOO42hhYUGO4+i1115r7oQUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA183/AsPYKrs7I2TKAAAAAElFTkSuQmCC" style="height:28px;display:block" alt="idGuru">
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
      <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAB0gAAAJACAYAAAAHGjWAAAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAAEAAElEQVR4nOz9aYxkWV4ffn/P3eLGnlst3VWdta9d09v0DAzMYBjgwcb2C5Bl/MgybyyMjLFAGMuyZMlYlizLaIQsY8sWAntYPPhBfxmZMf4PxgwzwBhm6ZnppfY1q7fqriUrl4i463le3DgnTty8kZFZmVmZEfH9lEKZFRlx4+5x7/md3+8ARERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERERJsidnsGiIiIiGjnnDx5XDabTTSbTdTrdfi+D9u2Yds2ACAIAqyu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" style="height:80px;display:block;margin:0 auto" alt="idGuru">
      <div style="color:#475569;font-size:14px;margin-top:10px">liquidGuru's AI-powered critter finder</div>
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
    <select id="folder-filter" onchange="currentPage=1;load()" style="flex:1;min-width:0"><option>All folders</option></select>
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
      <select id="photo-folder-filter" onchange="photoPage=1;loadPhotos()" style="flex:1;min-width:0"><option>All folders</option></select>
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
  <button class="btn-amber btn-sm" onclick="reanalyseSelectedFrames()">&#128260; Re-analyse</button>
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
  <button class="btn-amber btn-sm" onclick="reanalyseSelectedPhotos()">&#128260; Re-analyse</button>
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
        <button class="btn-amber" onclick="reanalyseFrame()">&#128260; Re-analyse</button>
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
        <button class="btn-amber" onclick="reanalysePhoto()">&#128260; Re-analyse</button>
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
  if (t==='photos') { loadPhotoStat(); loadPhotos(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas(); loadPhotoFolders(); }
  if (t==='home') { loadHomeStat(); }
}

async function load() {
  var q = document.getElementById('q').value;
  var sp = document.getElementById('sp').value;
  var site = document.getElementById('site-filter').value;
  var country = document.getElementById('country-filter').value;
  var region = document.getElementById('region-filter').value;
  var area = document.getElementById('area-filter').value;
  var folder = document.getElementById('folder-filter').value;
  var sort = document.getElementById('sort-filter').value;
  var p = new URLSearchParams();
  if (q) p.set('q', q);
  if (sp && sp !== 'All species') p.set('species', sp);
  if (site && site !== 'All sites') p.set('site', site);
  if (country && country !== 'All countries') p.set('country', country);
  if (region && region !== 'All regions') p.set('region', region);
  if (area && area !== 'All areas') p.set('area', area);
  if (folder && folder !== 'All folders') p.set('folder', folder);
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
    loadPhotoSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas(); loadPhotoFolders();
  } else {
    var ids = Object.keys(selected);
    await fetch('/api/bulk_site_date', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({frame_ids:ids, dive_site:site, dive_date:date, country:country, region:region, area:area})});
    clearSelection();
    loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas(); loadAllFolders();
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
  loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas(); loadAllFolders();
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

async function loadAllFolders() {
  var r = await fetch('/api/folders');
  var data = await r.json();
  var sel = document.getElementById('folder-filter');
  sel.innerHTML = '<option>All folders</option>';
  data.forEach(function(f){ var o=document.createElement('option'); o.value=f.folder; o.textContent=f.folder.split(/[\\/]/).pop()||f.folder; o.title=f.folder; sel.appendChild(o); });
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
      scanES.close(); loadFolderList(); loadAllSpecies(); loadAllSites(); loadAllFolders(); loadStat(); load();
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
    html+='<button class="btn-blue btn-sm" data-idx="'+i+'">🔄 Re-analyse</button>';
    html+='<button class="btn-red btn-sm" data-idx="'+i+'">Remove</button>';
    html+='</div></div>';
  });
  el.innerHTML=html;
  el.querySelectorAll('.btn-amber').forEach(function(btn){
    btn.addEventListener('click', function(){ openExplorer(data[btn.dataset.idx].folder); });
  });
  el.querySelectorAll('.btn-blue').forEach(function(btn){
    btn.addEventListener('click', function(){ reanalyseVideoFolder(data[btn.dataset.idx].folder, data[btn.dataset.idx].frames); });
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

loadAllSpecies(); loadAllSites(); loadAllCountries(); loadAllRegions(); loadAllAreas(); loadAllFolders(); load(); loadStat(); loadHomeStat(); loadSettings();

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
  var folder = document.getElementById('photo-folder-filter').value;
  var sort = document.getElementById('photo-sort-filter').value;
  var p = new URLSearchParams();
  if (q) p.set('q', q);
  if (sp && sp !== 'All species') p.set('species', sp);
  if (country && country !== 'All countries') p.set('country', country);
  if (region && region !== 'All regions') p.set('region', region);
  if (area && area !== 'All areas') p.set('area', area);
  if (site && site !== 'All sites') p.set('site', site);
  if (folder && folder !== 'All folders') p.set('folder', folder);
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
  loadPhotoSites(); loadAllSites(); loadPhotoCountries(); loadPhotoRegions(); loadPhotoAreas(); loadPhotoFolders();
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

async function loadPhotoFolders() {
  var r = await fetch('/api/photo_folders');
  var data = await r.json();
  var sel = document.getElementById('photo-folder-filter');
  sel.innerHTML = '<option>All folders</option>';
  data.forEach(function(f){ var o=document.createElement('option'); o.value=f.folder; o.textContent=f.folder.split(/[\\/]/).pop()||f.folder; o.title=f.folder; sel.appendChild(o); });
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
      photoScanES.close(); loadPhotoFolderList(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoFolders(); loadPhotoStat(); loadPhotos();
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
  loadFolderList(); loadAllSpecies(); loadAllSites(); loadAllFolders(); loadStat(); load();
}

async function removePhotoFolder(folder) {
  if (!confirm('Remove all photos from "'+folder+'" from the index? Files on disk will NOT be deleted.')) return;
  var r = await fetch('/api/remove_photo_folder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({folder:folder})});
  var d = await r.json();
  alert('Removed '+d.photos+' photo(s) from index.');
  loadPhotoFolderList(); loadPhotoSpecies(); loadPhotoSites(); loadPhotoFolders(); loadPhotoStat(); loadPhotos();
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
    html+='<button class="btn-blue btn-sm" data-idx="'+i+'">🔄 Re-analyse</button>';
    html+='<button class="btn-red btn-sm" data-idx="'+i+'">Remove</button>';
    html+='</div></div>';
  });
  el.innerHTML = html;
  el.querySelectorAll('.btn-amber').forEach(function(btn){
    btn.addEventListener('click', function(){ openExplorer(data[btn.dataset.idx].folder); });
  });
  el.querySelectorAll('.btn-blue').forEach(function(btn){
    btn.addEventListener('click', function(){ reanalysePhotoFolder(data[btn.dataset.idx].folder, data[btn.dataset.idx].photos); });
  });
  el.querySelectorAll('.btn-red').forEach(function(btn){
    btn.addEventListener('click', function(){ removePhotoFolder(data[btn.dataset.idx].folder); });
  });
}

// ── Re-analyse functions ──────────────────────────────────────────────

var _reanalyseES = null;

var REANALYSE_CONFIRM = 'This sends the image back through Claude AI for fresh identification. ' +
  'This is usually only needed when the underlying ID logic has changed — for example after a prompt update, model upgrade, or region change.\\n\\n' +
  'Species, habitat, behaviours, notes and visibility will be replaced with new AI results. ' +
  'Your manual edits to location, date, and ID confidence are preserved.\\n\\n' +
  'Each re-analyse uses your Anthropic API credits — approx $0.002 to $0.004 per image.';

async function reanalysePhoto() {
  if (!currentPhoto) return;
  if (!confirm('Re-analyse this photo with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var note = document.getElementById('pm-note');
  note.textContent = 'Re-analysing...';
  try {
    var r = await fetch('/api/reanalyse_photo', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: currentPhoto.id})});
    var d = await r.json();
    if (d.ok) {
      currentPhoto.species = d.species || [];
      currentPhoto.habitat = d.habitat || '';
      currentPhoto.behaviours = d.behaviours || [];
      currentPhoto.notes = d.notes || '';
      currentPhoto.visibility = d.visibility || '';
      renderPhotoSpeciesTags(currentPhoto.species);
      document.getElementById('pm-habitat').value = d.habitat || '';
      document.getElementById('pm-behs').value = (d.behaviours||[]).join(', ');
      document.getElementById('pm-notes').value = d.notes || '';
      document.getElementById('pm-visibility').value = d.visibility || '';
      note.textContent = 'Re-analysed!';
      loadPhotos(); loadPhotoSpecies();
    } else {
      note.textContent = 'Error: ' + (d.error || 'unknown');
    }
  } catch(e) {
    note.textContent = 'Error: ' + e.message;
  }
  setTimeout(function(){ note.textContent = ''; }, 3000);
}

async function reanalyseFrame() {
  if (!current) return;
  if (!confirm('Re-analyse this frame with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var note = document.getElementById('m-note');
  note.textContent = 'Re-analysing...';
  try {
    var r = await fetch('/api/reanalyse_frame', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id: current.id})});
    var d = await r.json();
    if (d.ok) {
      current.species = d.species || [];
      current.habitat = d.habitat || '';
      current.behaviours = d.behaviours || [];
      current.notes = d.notes || '';
      current.visibility = d.visibility || '';
      renderSpeciesTags(current.species);
      document.getElementById('m-habitat').value = d.habitat || '';
      document.getElementById('m-behs').value = (d.behaviours||[]).join(', ');
      document.getElementById('m-notes').value = d.notes || '';
      document.getElementById('m-visibility').value = d.visibility || '';
      note.textContent = 'Re-analysed!';
      load(); loadAllSpecies();
    } else {
      note.textContent = 'Error: ' + (d.error || 'unknown');
    }
  } catch(e) {
    note.textContent = 'Error: ' + e.message;
  }
  setTimeout(function(){ note.textContent = ''; }, 3000);
}

async function reanalyseSelectedPhotos() {
  var ids = Object.keys(selectedPhotos);
  if (!ids.length) return;
  if (!confirm('Re-analyse ' + ids.length + ' selected photo(s) with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var r = await fetch('/api/reanalyse_photos', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: ids})});
  var d = await r.json();
  if (d.started) {
    clearPhotoSelection();
    listenReanalyseProgress(function() { loadPhotos(); loadPhotoSpecies(); });
  } else {
    alert('Error: ' + (d.error || 'unknown'));
  }
}

async function reanalyseSelectedFrames() {
  var ids = Object.keys(selected);
  if (!ids.length) return;
  if (!confirm('Re-analyse ' + ids.length + ' selected frame(s) with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var r = await fetch('/api/reanalyse_frames', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ids: ids})});
  var d = await r.json();
  if (d.started) {
    clearSelection();
    listenReanalyseProgress(function() { load(); loadAllSpecies(); });
  } else {
    alert('Error: ' + (d.error || 'unknown'));
  }
}

async function reanalysePhotoFolder(folder, count) {
  if (!confirm('Re-analyse all ' + count + ' photo(s) in this folder with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var r = await fetch('/api/reanalyse_photos', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder})});
  var d = await r.json();
  if (d.started) {
    listenReanalyseProgress(function() { loadPhotos(); loadPhotoSpecies(); });
  } else {
    alert('Error: ' + (d.error || 'unknown'));
  }
}

async function reanalyseVideoFolder(folder, count) {
  if (!confirm('Re-analyse all ' + count + ' frame(s) in this folder with Claude AI?\\n\\n' + REANALYSE_CONFIRM)) return;
  var r = await fetch('/api/reanalyse_frames', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({folder: folder})});
  var d = await r.json();
  if (d.started) {
    listenReanalyseProgress(function() { load(); loadAllSpecies(); });
  } else {
    alert('Error: ' + (d.error || 'unknown'));
  }
}

function listenReanalyseProgress(onDone) {
  if (_reanalyseES) _reanalyseES.close();
  // Show a small progress toast
  var toast = document.getElementById('reanalyse-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'reanalyse-toast';
    toast.style.cssText = 'position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:#0c1e35;border:1px solid #1e3a5f;color:#7dd4fc;padding:10px 20px;border-radius:10px;font-size:13px;z-index:60;display:flex;align-items:center;gap:10px';
    document.body.appendChild(toast);
  }
  toast.innerHTML = '<span id="reanalyse-toast-msg">Re-analysing...</span><button class="btn-red btn-sm" onclick="stopReanalyse()">Stop</button>';
  toast.style.display = 'flex';
  _reanalyseES = new EventSource('/api/reanalyse_progress');
  _reanalyseES.onmessage = function(e) {
    var d = JSON.parse(e.data);
    if (d.type === 'ping') return;
    if (d.type === 'progress') {
      var msgEl = document.getElementById('reanalyse-toast-msg');
      if (msgEl) msgEl.textContent = d.msg + ' (' + (d.pct||0) + '%)';
    } else if (d.type === 'done') {
      var msgEl = document.getElementById('reanalyse-toast-msg');
      if (msgEl) msgEl.textContent = d.msg;
      _reanalyseES.close(); _reanalyseES = null;
      if (onDone) onDone();
      setTimeout(function(){ toast.style.display = 'none'; }, 3000);
    } else if (d.type === 'error') {
      var msgEl = document.getElementById('reanalyse-toast-msg');
      if (msgEl) { msgEl.textContent = 'Error: ' + d.msg; msgEl.style.color = '#ef4444'; }
      _reanalyseES.close(); _reanalyseES = null;
      setTimeout(function(){ toast.style.display = 'none'; }, 4000);
    }
  };
}

async function stopReanalyse() {
  fetch('/api/reanalyse_stop', {method:'POST'});
  if (_reanalyseES) { _reanalyseES.close(); _reanalyseES = null; }
  var toast = document.getElementById('reanalyse-toast');
  if (toast) toast.style.display = 'none';
}

// ── End re-analyse functions ──────────────────────────────────────────

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
        path = resolve_to_unc(path)
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
        folder = request.args.get("folder","")
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
        if folder: base += " AND v.path LIKE ?"; params.append(folder+"%")

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
        folder = request.args.get("folder","")
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
        if folder: base += " AND path LIKE ?"; params.append(folder+"%")
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
        try:
            if IS_WIN:
                os.startfile(str(p))
            elif IS_MAC:
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return jsonify({"ok":True})
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
        path = resolve_to_unc(path)
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

    # ── Re-analyse routes ──────────────────────────────────────────────

    def _get_default_region():
        """Get default region from settings DB."""
        try:
            _r = conn.execute("SELECT value FROM settings WHERE key='default_region'").fetchone()
            return _r[0] if _r else None
        except:
            return None

    @app.route("/api/reanalyse_photo", methods=["POST"])
    def reanalyse_photo():
        data = request.json
        pid = data.get("id")
        if not pid: return jsonify({"error": "no id"}), 400
        client = get_ai_client()
        if not client: return jsonify({"error": "No API key set. Open Settings (gear icon) and enter your Anthropic API key."}), 400
        db = get_db()
        row = db.execute("SELECT thumb_path, path FROM photos WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify({"error": "Photo not found"}), 404
        thumb_path = row[0]
        # Check thumbnail exists; try to regenerate if missing
        if not thumb_path or not Path(thumb_path).exists():
            orig = Path(row[1])
            if orig.exists():
                out = FRAMES_DIR / f"{pid}.jpg"
                if resize_photo_for_thumb(orig, out):
                    thumb_path = str(out)
                    db.execute("UPDATE photos SET thumb_path=? WHERE id=?", (thumb_path, pid))
                    db.commit()
                else:
                    return jsonify({"error": "Could not regenerate thumbnail"}), 500
            else:
                return jsonify({"error": "Thumbnail and original file not found"}), 404
        region = data.get("region") or _get_default_region()
        try:
            result = analyze_frame(client, thumb_path, region)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        species = json.dumps(normalise_species(result.get("species", [])))
        habitat = result.get("habitat", "")
        visibility = result.get("visibility", "")
        behaviours = json.dumps(result.get("behaviours", []))
        notes = result.get("notes", "")
        db.execute("UPDATE photos SET species=?, habitat=?, visibility=?, behaviours=?, notes=?, indexed_at=? WHERE id=?",
                   (species, habitat, visibility, behaviours, notes, datetime.now().isoformat(), pid))
        db.commit()
        return jsonify({"ok": True, "species": json.loads(species), "habitat": habitat,
                        "visibility": visibility, "behaviours": json.loads(behaviours), "notes": notes})

    @app.route("/api/reanalyse_frame", methods=["POST"])
    def reanalyse_frame():
        data = request.json
        fid = data.get("id")
        if not fid: return jsonify({"error": "no id"}), 400
        client = get_ai_client()
        if not client: return jsonify({"error": "No API key set. Open Settings (gear icon) and enter your Anthropic API key."}), 400
        db = get_db()
        row = db.execute("SELECT thumb_path FROM frames WHERE id=?", (fid,)).fetchone()
        if not row: return jsonify({"error": "Frame not found"}), 404
        thumb_path = row[0]
        if not thumb_path or not Path(thumb_path).exists():
            return jsonify({"error": "Thumbnail not found"}), 404
        region = data.get("region") or _get_default_region()
        try:
            result = analyze_frame(client, thumb_path, region)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        species = json.dumps(normalise_species(result.get("species", [])))
        habitat = result.get("habitat", "")
        visibility = result.get("visibility", "")
        behaviours = json.dumps(result.get("behaviours", []))
        notes = result.get("notes", "")
        db.execute("UPDATE frames SET species=?, habitat=?, visibility=?, behaviours=?, notes=? WHERE id=?",
                   (species, habitat, visibility, behaviours, notes, fid))
        db.commit()
        return jsonify({"ok": True, "species": json.loads(species), "habitat": habitat,
                        "visibility": visibility, "behaviours": json.loads(behaviours), "notes": notes})

    @app.route("/api/reanalyse_photos", methods=["POST"])
    def reanalyse_photos_bulk():
        data = request.json
        ids = data.get("ids", [])
        folder = data.get("folder", "").strip()
        region = data.get("region") or None
        db = get_db()
        if folder:
            rows = db.execute("SELECT id, thumb_path, path FROM photos WHERE path LIKE ?", (folder + "%",)).fetchall()
            ids = [r[0] for r in rows]
            thumb_map = {r[0]: r[1] for r in rows}
            path_map = {r[0]: r[2] for r in rows}
        else:
            if not ids: return jsonify({"error": "no ids"}), 400
            rows = db.execute("SELECT id, thumb_path, path FROM photos WHERE id IN (" + ",".join(["?"]*len(ids)) + ")", ids).fetchall()
            thumb_map = {r[0]: r[1] for r in rows}
            path_map = {r[0]: r[2] for r in rows}
        if not ids: return jsonify({"error": "No photos found"}), 400
        final_ids = list(ids)
        def run():
            reanalyse_stop_flag.clear()
            if not get_ai_client():
                reanalyse_queue.put({"type": "error", "msg": "No API key set."}); return
            default_region = region or _get_default_region()
            total = len(final_ids)
            reanalyse_queue.put({"type": "progress", "msg": "Re-analysing " + str(total) + " photo(s)...", "pct": 0})
            done = 0
            for pid in final_ids:
                if reanalyse_stop_flag.is_set(): break
                tp = thumb_map.get(pid)
                # Try to regenerate thumbnail if missing
                if not tp or not Path(tp).exists():
                    orig = Path(path_map.get(pid, ""))
                    if orig.exists():
                        out = FRAMES_DIR / f"{pid}.jpg"
                        if resize_photo_for_thumb(orig, out):
                            tp = str(out)
                if not tp or not Path(tp).exists():
                    done += 1; continue
                try:
                    result = analyze_frame(get_ai_client(), tp, default_region)
                    species = json.dumps(normalise_species(result.get("species", [])))
                    habitat = result.get("habitat", "")
                    visibility = result.get("visibility", "")
                    behaviours = json.dumps(result.get("behaviours", []))
                    notes = result.get("notes", "")
                    ldb = get_db()
                    ldb.execute("UPDATE photos SET species=?, habitat=?, visibility=?, behaviours=?, notes=?, indexed_at=? WHERE id=?",
                               (species, habitat, visibility, behaviours, notes, datetime.now().isoformat(), pid))
                    ldb.commit()
                except Exception as e:
                    console.print(f"[yellow]  Re-analyse error photo {pid}: {e}[/yellow]")
                done += 1
                reanalyse_queue.put({"type": "progress", "msg": "Re-analysing " + str(done) + "/" + str(total) + " photo(s)", "pct": int(done/total*100)})
            reanalyse_queue.put({"type": "done", "msg": "Re-analysed " + str(done) + " photo(s).", "total": done})
        threading.Thread(target=run, daemon=True).start()
        return jsonify({"started": True, "count": len(final_ids)})

    @app.route("/api/reanalyse_frames", methods=["POST"])
    def reanalyse_frames_bulk():
        data = request.json
        ids = data.get("ids", [])
        folder = data.get("folder", "").strip()
        region = data.get("region") or None
        db = get_db()
        if folder:
            rows = db.execute("SELECT f.id, f.thumb_path FROM frames f JOIN videos v ON f.video_id=v.id WHERE v.path LIKE ?",
                             (folder + "%",)).fetchall()
            ids = [r[0] for r in rows]
            thumb_map = {r[0]: r[1] for r in rows}
        else:
            if not ids: return jsonify({"error": "no ids"}), 400
            rows = db.execute("SELECT id, thumb_path FROM frames WHERE id IN (" + ",".join(["?"]*len(ids)) + ")", ids).fetchall()
            thumb_map = {r[0]: r[1] for r in rows}
        if not ids: return jsonify({"error": "No frames found"}), 400
        final_ids = list(ids)
        def run():
            reanalyse_stop_flag.clear()
            if not get_ai_client():
                reanalyse_queue.put({"type": "error", "msg": "No API key set."}); return
            default_region = region or _get_default_region()
            total = len(final_ids)
            reanalyse_queue.put({"type": "progress", "msg": "Re-analysing " + str(total) + " frame(s)...", "pct": 0})
            done = 0
            for fid in final_ids:
                if reanalyse_stop_flag.is_set(): break
                tp = thumb_map.get(fid)
                if not tp or not Path(tp).exists():
                    done += 1; continue
                try:
                    result = analyze_frame(get_ai_client(), tp, default_region)
                    species = json.dumps(normalise_species(result.get("species", [])))
                    habitat = result.get("habitat", "")
                    visibility = result.get("visibility", "")
                    behaviours = json.dumps(result.get("behaviours", []))
                    notes = result.get("notes", "")
                    ldb = get_db()
                    ldb.execute("UPDATE frames SET species=?, habitat=?, visibility=?, behaviours=?, notes=? WHERE id=?",
                               (species, habitat, visibility, behaviours, notes, fid))
                    ldb.commit()
                except Exception as e:
                    console.print(f"[yellow]  Re-analyse error frame {fid}: {e}[/yellow]")
                done += 1
                reanalyse_queue.put({"type": "progress", "msg": "Re-analysing " + str(done) + "/" + str(total) + " frame(s)", "pct": int(done/total*100)})
            reanalyse_queue.put({"type": "done", "msg": "Re-analysed " + str(done) + " frame(s).", "total": done})
        threading.Thread(target=run, daemon=True).start()
        return jsonify({"started": True, "count": len(final_ids)})

    @app.route("/api/reanalyse_progress")
    def reanalyse_progress():
        def stream():
            while True:
                try:
                    msg = reanalyse_queue.get(timeout=30)
                    yield "data: "+json.dumps(msg)+"\n\n"
                    if msg.get("type") in ("done","error"): break
                except: yield "data: "+json.dumps({"type":"ping"})+"\n\n"
        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    @app.route("/api/reanalyse_stop", methods=["POST"])
    def reanalyse_stop():
        reanalyse_stop_flag.set()
        return jsonify({"stopped": True})

    # ── End re-analyse routes ──────────────────────────────────────────

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
        if IS_WIN:
            if p.is_file(): subprocess.Popen(["explorer","/select,",str(p)])
            else: subprocess.Popen(["explorer",str(p)])
        elif IS_MAC:
            if p.is_file(): subprocess.Popen(["open","-R",str(p)])
            else: subprocess.Popen(["open",str(p)])
        else:
            subprocess.Popen(["xdg-open",str(p.parent if p.is_file() else p)])
        return jsonify({"opened":True})

    @app.route("/api/open")
    def open_file():
        path = request.args.get("path","")
        ts = float(request.args.get("ts",0))
        if not path or not Path(path).exists(): return jsonify({"error":"File not found"}), 404
        vlc_paths = [r"C:\Program Files\VideoLAN\VLC\vlc.exe", r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe", "/Applications/VLC.app/Contents/MacOS/VLC"]
        for vlc in vlc_paths:
            if Path(vlc).exists():
                subprocess.Popen([vlc,f"--start-time={int(ts)}",path])
                return jsonify({"opened":"vlc","ts":ts})
        if IS_WIN: os.startfile(path)
        elif IS_MAC: subprocess.Popen(["open", path])
        else: subprocess.Popen(["xdg-open", path])
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

    if not headless: webbrowser.open("http://localhost:5001")
    app.run(debug=False, port=5001)


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
