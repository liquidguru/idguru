# idGuru — AI-Powered Underwater Species Identifier

> Built by [liquidGuru](https://www.liquidguru.com) · Powered by [Claude AI](https://anthropic.com)

idGuru uses Claude AI to automatically identify marine species in your underwater video footage and still photos, building a fully searchable archive of everything you've shot.

**[⬇️ Download latest release](https://github.com/liquidguru/idguru/releases/latest)**

---

## Requirements

- **Python 3.10 or later** — https://www.python.org/downloads/
- **ffmpeg** (for video indexing) — https://ffmpeg.org/download.html
- **An Anthropic API key** — https://console.anthropic.com
- **VLC** (optional, for opening video at timestamps) — https://www.videolan.org/

---

## Installation

### Windows
1. Install Python from https://www.python.org/downloads/ — tick **"Add Python to PATH"**
2. Install ffmpeg and add it to PATH (see https://ffmpeg.org/download.html)
3. Download the latest release zip and extract it
4. Double-click **`setup.bat`** to install Python dependencies
5. Double-click **`launcher.pyw`** to start idGuru

### macOS
1. Install Python from https://www.python.org/downloads/ or via Homebrew: `brew install python`
2. Install ffmpeg via Homebrew: `brew install ffmpeg`
3. Download the latest release zip and extract it
4. Open Terminal, navigate to the idGuru folder and run:
   ```
   chmod +x setup.sh
   ./setup.sh
   ```
5. Run `python3 launcher.py` or double-click it in Finder

---

## First Run

1. Launch idGuru — it opens in your browser at `http://localhost:5000`
2. Click the **⚙️ gear icon** (top right) to open Settings
3. Enter your **Anthropic API key** (get one at [console.anthropic.com](https://console.anthropic.com)) and set your **default region**
4. Click **Save**

---

## Scanning footage

### Videos
1. Go to **Scan Videos/Photos** tab
2. Under **Scan Videos**, paste or browse to your footage folder
3. Choose your region (overrides the default for this scan)
4. Choose workers (10 is good for most machines)
5. Click **Start Scan**

For large collections, tick **Batch mode** — it costs 50% less and runs asynchronously overnight. Results appear when the batch completes.

### Photos
Same process under the **Scan Photos** section. Supports JPG/JPEG and RAW files (CR2, NEF, ARW, DNG, ORF, RAF, RW2, PEF, SRW).

For RAW files, install rawpy: `pip install rawpy`

---

## Browsing

- **Videos tab** — browse all indexed frames, filter by species/country/region/area/site
- **Photos tab** — browse photos, switch to **By Species** to see grouped results
- Click any card to open the detail modal — edit species, add location/date, rename files
- **Ctrl+click** or **Shift+click** to select multiple items for bulk editing

---

## Regions supported

| Region | Notes |
|--------|-------|
| Indo-Pacific / Coral Triangle | Lembeh, Ambon, Raja Ampat, Philippines |
| Caribbean | Bonaire, Cayman, Roatan, Cozumel |
| Red Sea | Dahab, Sharm, Brothers Islands |
| UK / North Europe | Scotland, Norway, Farne Islands |
| West Coast USA | California kelp forests, Oregon, Washington |
| East Coast USA | North Carolina, Florida, New England |
| Australia / New Zealand | GBR, Coral Sea, Poor Knights |
| Mediterranean | Malta, Croatia, Greece |
| Japan | Izu, Okinawa, Hokkaido |

Each region gives Claude context about local species to improve identification accuracy.

---

## Cost

idGuru uses the Anthropic API — you pay for what you use. Approximate costs:

| Mode | Cost per frame/photo |
|------|---------------------|
| Standard | ~$0.002–0.004 |
| Batch mode | ~$0.001–0.002 |

A 1-hour video at 1 frame/10s = ~360 frames ≈ $0.70–$1.40 standard, $0.35–$0.70 batch.

---

## Data storage

Your index is stored locally at:
- **Windows:** `C:\Users\<you>\.underwater_index\`
- **macOS/Linux:** `~/.underwater_index/`

This contains the SQLite database and thumbnail cache. Your original files are never modified unless you use the Rename or Delete features.

---

## Troubleshooting

**"No API key" / nothing gets indexed**
- Open Settings (⚙️) and enter your Anthropic API key from [console.anthropic.com](https://console.anthropic.com)

**"rawpy not installed" when scanning RAW files**
- Run: `pip install rawpy`

**ffmpeg errors on video scan**
- Make sure ffmpeg is installed and on your PATH

**VLC not opening at timestamp**
- Make sure VLC is installed at the default location

**Blank cmd window appears on Windows**
- Use `launcher.pyw` not `launcher.py` — the `.pyw` extension suppresses the console window

---

## Licence

Copyright (c) 2025 Kaj Maney / liquidGuru

Licensed under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) — free for personal, non-commercial use with attribution. See [LICENSE](LICENSE) for full terms.

---

## Credits

Built by [Kaj Maney / liquidGuru](https://www.liquidguru.com) · Powered by [Claude](https://anthropic.com) from Anthropic
