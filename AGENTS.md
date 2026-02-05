# AGENTS.md — Maintainer Guide for Ableton Dual Extract

This file is written for AI coding agents (Claude Code, Codex, etc.) to quickly understand the project goals, architecture, constraints, and how to safely modify the code.

## Project Summary

**Ableton Dual Extract** is a Python static analyzer for Ableton Live `.als` projects (gzip-compressed XML). It emits two JSON reports:

1. **FULL** (`*.full.json`) — detailed, human + ChatGPT oriented. Includes routing/topology, QC findings, and (when enabled) high-value device settings.
2. **COMPACT** (`*.compact.json`) — token-minimized schema intended to stay under ~25k tokens for Claude and other limited-context LLMs.

The tool is designed as a pre-export QA gate and an AI-assisted mix review aid.

## Primary User Goals

There are two equally important goals:

### Goal A — Export Safety / QA
Detect issues that can break stems/exports, especially in a workflow that uses:
- groups and internal bus tracks
- bus → FX return → group feedback chains
- **track deactivation** (gray power button) instead of mute

QA features include:
- deactivated tracks
- disabled devices
- device on/off automation detection
- routing breaks caused by deactivated sources
- dead/orphan bus detection
- concise QC summary + console report

### Goal B — Mix / Loudness Review
Enable AI analysis of device chains and settings:
- capture stock device parameters (EQ8 bands, Glue, Drum Buss, Utility, delays, etc.)
- represent signal processing chain order per track
- handle third-party plugins realistically (often opaque state blobs)
- provide enough information for mix and loudness recommendations

## Hard Constraints (Do Not Break)

1. **Do not break existing QA logic**:
   - track deactivation flags
   - conservative device-enabled detection
   - on/off automation mapping (AutomationTarget ↔ PointeeId)
   - routing topology fields and derived routing warnings
   - QC fail/warn logic and qc_summary

2. **COMPACT must remain small**:
   - Target under ~25k tokens for Claude.
   - Do not add verbose per-device parameter dumps to COMPACT.
   - Prefer packed codes and small booleans/short strings.

3. **The user does not rely on mute**:
   - mute detection can remain conservative/secondary
   - deactivation is the primary silence mechanism

4. **Human stdout output must show track NAMES, not internal IDs**:
   - internal IDs remain in JSON for graph logic
   - console output should not print IDs

## Repository Files

Typical repo layout (recommended):

- `ableton_dual_extract.py` — main script
- `VERSION` — current version string (e.g., `1.0.24`)
- `CHANGELOG.md` — versioned changes
- `README.md` — end-user documentation
- `AGENTS.md` — this maintainer guide
- `examples/` — optional sample outputs (FULL + COMPACT)

## Running

Basic:
- `python ableton_dual_extract.py project.als`

With device settings:
- `python ableton_dual_extract.py project.als --mix-settings`

Minified JSON (mostly for debugging or storage):
- `python ableton_dual_extract.py project.als --minify`

## High-Level Architecture

### 1) Parse Stage
- Read `.als` as gzip
- Parse XML into an ElementTree
- Identify tracks, devices, automation targets, and routing tags

### 2) Model Build Stage
Construct an internal representation of:
- tracks: name/type/id + mixer + routing in/out
- group topology: parent relationships
- device chains: ordered list of devices per track
- automation mapping: AutomationTarget → PointeeId → device/property

### 3) Analysis Stage (QC)
- Track deactivation checks
- Device enabled/bypass checks
- On/off automation checks
- Routing graph build + break detection
- Dead bus / orphan bus heuristics
- Build per-track reasons (fail/warn) and global qc_summary

### 4) Emit Stage
Write:
- FULL JSON (pretty-printed by default)
- COMPACT JSON (pretty-printed by default, still schema-minimized)
Optionally minify both with `--minify`.

## JSON Outputs

### FULL (`*.full.json`)
Contains:
- `meta` (tool/schema/version/time)
- `qc_reason_legend`, `qc_warning_legend`
- `qc_summary`
- `tracks[]` with:
  - flags (active/deactivated)
  - routing (audio_in/audio_out + resolved group out)
  - routing break annotations (depth, sources, messages)
  - device chains (with settings, possibly pooled)
- `pools` (in v24+) for deduplicated blobs:
  - `device_settings_pool`
  - `plugin_decoded_pool`

**Important:** FULL is the canonical source for analysis. It may be large but must remain useful for troubleshooting and ChatGPT.

### COMPACT (`*.compact.json`)
Contains:
- `schema`: `compact`
- `legend`: includes field meanings + code maps
- minimal `tracks[]`:
  - routing short codes
  - packed QC reasons `R` (string like `dor`)
  - issues tags (short list)
  - minimal device list with small hashes/flags

**Rules for COMPACT:**
- Don’t add large nested structures
- Prefer short codes and stable keys
- Keep legend updated if codes change

## QC Reason Codes

Packed in `R` (order is not semantic):

- `d` = track deactivated
- `o` = device off (without on/off automation that would explain it)
- `r` = routing break (deactivated upstream impacts downstream)
- `x` = missing/unknown routing target
- `m` = muted (secondary)
- `s` = silent track (if implemented)

Warnings in `W` or `warnings[]`:
- `a` = device off but automated (on/off automation present)

If adding new codes:
- update FULL `qc_reason_legend`
- update COMPACT `legend.R_codes` / `legend.W_codes`
- ensure stdout prints a legend line

## Routing Break & Dead/Orphan Bus Logic

### Routing breaks
Goal: detect when deactivated tracks break bus/group/return chains.

Implementation approach:
- Build upstream references from each track’s `audio_in` and `audio_out`
- Identify deactivated sources
- Propagate “break impact” downstream
- Track `routing_break_depth` and `routing_break_sources` (shortest hop)
- Emit per-track `routing_impact` messages in FULL
- Encode `r` in COMPACT `R`

### Dead bus detection
- Track has upstream sources, but all upstream sources are deactivated.

### Orphan bus detection (heuristic)
- Track name indicates bus/return/fx and it has no upstream sources.

If improving heuristics:
- keep false positives low
- prefer adding “warn” vs “fail” where uncertain

## Stock Device Settings Extraction

Enabled via `--mix-settings`.

Guideline:
- Extract **high-value** parameters (mix-relevant)
- Keep structured and bounded output
- Avoid dumping wrapper parameter lists
- Prefer per-device parsers for the top devices:
  - EQ8: per-band freq/gain/Q/mode/isOn
  - Utility: gain/width/mono/bass mono freq
  - Glue: threshold/ratio/attack/release/makeup/drywet
  - Drum Buss: drive/crunch/boom/transients/in-out/drywet
  - Delay/Echo/AutoPan: time/feedback/filters/ducking/etc.

## Third-Party Plugins

Reality:
- Many plugins store state as opaque binary blob; knob-level decoding is often not feasible offline.
- For opaque plugins: store **compact metadata only** (state hash/len + role).
- For plugins that embed XML/JSON (e.g., Serum/Xfer, Infiltrator): extract bounded structured hints when useful.

Never:
- dump huge `plugin_state_hints` arrays (size blowup)
- base64 large state chunks in FULL by default

## File Size Management

The project previously experienced size regressions. Current strategy:
- prune noisy XML wrappers
- do NOT include full parameter wrappers
- dedupe repeated device settings via `pools` and `*_ref` pointers
- strip null keys in FULL
- keep COMPACT minimal

If adding new data:
- measure impact on FULL size
- ensure COMPACT remains under ~25k tokens

## Stdout Report Requirements

- Print actionable summary (fails, routing breaks, dead/orphan buses)
- Print **track names only**
- Print code legend for `R`/`W`
- Keep output stable and easy to scan

## Change Management

### Git Workflow

This project is tracked in Git and changes should be committed and pushed to the `main` branch.

**After every batch of changes:**

1. **Update VERSION** in BOTH locations:
   - `VERSION` file (for repo visibility)
   - `SCRIPT_VERSION` constant in `ableton_dual_extract.py` line 36 (for standalone script)
   - Increment using semantic versioning (e.g., `1.0.25` → `1.0.26`)

2. **Update CHANGELOG.md**:
   - Add entries in descending order (newest at top)
   - Include version number, date, and bullet list of changes
   - Follow existing format

3. **Update README.md** as necessary:
   - Update if user-facing features changed
   - Update if command-line arguments changed
   - Update if output format changed

4. **Commit and push to main**:
   ```bash
   git add .
   git commit -m "v1.0.X: Description of changes"
   git push origin main
   ```

### General Guidelines

- Prefer feature flags for new verbose outputs (`--mix-settings`, future `--trace`, etc.)
- Keep the VERSION file and script version in sync at all times
- Test before committing (see Testing Strategy below)

## Testing Strategy (Lightweight)

### Smoke Tests

Minimal smoke tests recommended:
- Run on a known `.als`
- Validate JSON parses
- Validate schema keys exist
- Validate COMPACT token size stays under budget (approx estimate by chars/4 or use a tokenizer offline)

### Regression Testing (Critical for Refactoring)

When making changes that could affect output (refactoring, bug fixes, new features), use this procedure to ensure zero regressions:

**Test file:** `d:\temp6\Acid Breaks Project 2026-02-03-03.als`

**Before making changes:**
```bash
# Run script on test file and save outputs
python ableton_dual_extract.py "d:\temp6\Acid Breaks Project 2026-02-03-03.als" > before_stdout.txt

# Save the generated JSON files
cp "d:\temp6\Acid Breaks Project 2026-02-03-03.full.json" before.full.json
cp "d:\temp6\Acid Breaks Project 2026-02-03-03.compact.json" before.compact.json
```

**After making changes:**
```bash
# Run script on same test file
python ableton_dual_extract.py "d:\temp6\Acid Breaks Project 2026-02-03-03.als" > after_stdout.txt

# Save the generated JSON files
cp "d:\temp6\Acid Breaks Project 2026-02-03-03.full.json" after.full.json
cp "d:\temp6\Acid Breaks Project 2026-02-03-03.compact.json" after.compact.json
```

**Compare outputs:**
```bash
# Compare stdout
diff before_stdout.txt after_stdout.txt

# Compare JSON files
diff before.full.json after.full.json
diff before.compact.json after.compact.json
```

**Expected result:** All diffs should be EMPTY (no output from diff commands)

If any diffs appear, investigate thoroughly before committing:
- Track deactivation detection must be unchanged
- On/off automation mapping must be unchanged
- Routing break count must be stable
- Device extraction must be identical
- QC summary must match exactly

## Safe Modification Checklist

Before committing/pushing changes:
- [ ] Does COMPACT schema remain stable/minimal?
- [ ] Are legends updated if codes changed?
- [ ] Did FULL size increase significantly? If yes, why?
- [ ] Are track/device IDs still present in JSON but hidden from stdout?
- [ ] Did routing-break detection regress?
- [ ] Did device on/off automation detection regress?
- [ ] Does it run on macOS/Windows/Linux (stdlib only)?
- [ ] VERSION incremented in BOTH `VERSION` file and `ableton_dual_extract.py`?
- [ ] CHANGELOG.md updated with changes?
- [ ] README.md updated if user-facing changes were made?

---

If you are an AI agent making changes: prioritize correctness, stable schemas, and size discipline over completeness dumps.
