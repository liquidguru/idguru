# idGuru — Development Notes & TODO

## Current version: v1.1.0

## How to continue development
Upload `underwater_indexer.py` from this repo to a new Claude chat and say:
> "This is the current idGuru underwater_indexer.py — please continue from where we left off"

---

## What's been built

### Core
- Video indexing — ffmpeg frame extraction every 10 seconds, Claude AI identification
- Photo indexing — JPG/JPEG and RAW files (CR2, NEF, ARW, DNG, ORF, RAF, RW2, PEF, SRW)
- Batch mode for both video and photos (50% cheaper, async overnight scanning)
- SQLite database stored locally at `~/.underwater_index/`
- Flask web server, opens in browser at localhost:5000
- Cross-platform launcher (Windows: launcher.pyw, macOS: launcher.py)

### UI
- Home tab — what is idGuru, how it works, about liquidGuru, quick start buttons
- Videos tab — frame browse + clips view, filter by species/country/region/area/site
- Photos tab — All Photos + By Species grouped view
- Scan Videos/Photos tab — separate video and photo scan sections with region selector
- Tools tab — find & replace species, fix em dashes
- Sticky tab bar with idGuru logo and ⚙️ settings gear icon
- Settings modal — API key (show/hide) + default region

### Editing
- Editable species tags (add, edit, remove) with autocomplete
- Editable habitat, behaviours, notes text areas
- Water Visibility dropdown (poor/fair/good/excellent)
- ID Confidence dropdown (uncertain/probable/confirmed)
- Confirmed ID triggers automatic species lookup (habitat, behaviours, notes from Claude)
- Bulk Confirm ID & Lookup — works on multi-selected frames/photos and whole clips
- Checkbox species picker when multiple species tagged
- Location/date fields (country, region, area, dive site, dive date)
- Bulk set species, location/date across multi-selected items
- Rename file using species name
- Mark for delete / purge marked files
- Mark reviewed

### Regions (9)
Indo-Pacific / Coral Triangle, Caribbean, Red Sea, UK / North Europe,
West Coast USA, East Coast USA, Australia / New Zealand, Mediterranean, Japan

### Technical
- Case-insensitive file extension matching (.MTS, .MP4, .CR2 etc)
- API key stored in settings DB (no environment variable needed)
- ai_client uses mutable container so key updates take effect immediately
- Per-scan region override
- Folder removal from index (files on disk untouched)

---

## TODO / Future ideas

### High priority
- [ ] Test on macOS — not tested yet, Mac users welcome to report issues

### Features
- [ ] VideoToolbox hardware acceleration for Mac frame extraction
      (currently falls back to CPU on Mac, works but slower)
- [ ] Export to CSV includes new fields (id_confidence, habitat edits etc)
- [ ] Stats page improvements — show confirmed IDs, species count by region
- [ ] Slideshow / fullscreen view for photos

### Nice to have
- [ ] Dark/light mode toggle
- [ ] Keyboard shortcuts (arrow keys to navigate frames)
- [ ] Duplicate detection for photos
- [ ] GPS/EXIF data extraction from photos for auto location tagging

---

## Known issues / watch out for
- Multiple launcher instances running simultaneously causes conflicts — always check only one is running
- The \- escape sequence in JS inside Python triple-quoted strings causes SyntaxWarning — harmless but worth noting for future JS regex edits
- Mac not tested — open_file route uses os.startfile() which is Windows only as fallback

---

## Distribution
- GitHub: https://github.com/liquidguru/idguru
- Licence: AGPL-3.0 for the code; CC BY-NC 4.0 for footage/screenshots/datasets (see LICENSE-MEDIA)
- Contact: kaj@liquidguru.com
- Posted on: Wetpixel (March 2026)
