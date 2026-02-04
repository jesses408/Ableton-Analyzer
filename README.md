# Ableton Dual Extract – Project QA & Mix Analysis Tool

## Overview

This tool analyzes Ableton Live `.als` project files and produces two structured JSON reports:

- **FULL** – Detailed analysis for human review and ChatGPT
- **COMPACT_V2** – Token-optimized summary for Claude and other limited-context LLMs

It functions as a static analyzer for Ableton sessions, helping detect technical risks and enabling automated mix and loudness review.

---

## Typical Workflow

This tool is designed to fit into the following production workflow:

1. Finish arrangement and rough mix in Ableton
2. Organize tracks into groups and internal buses
3. Prepare FX return tracks and stem groups
4. Run the extractor:

   python ableton_dual_extract.py project.als --mix-settings

5. Review console QA output
6. Fix any critical issues in Ableton
7. Upload FULL output to ChatGPT for detailed mix review
8. Upload COMPACT output to Claude for fast QA
9. Re-run before final stem/export

This process ensures technical and routing issues are caught before export.

---

## Track Deactivation Philosophy

This tool assumes that **track deactivation (gray power button)** is the primary method used to silence tracks.

- Muted tracks are treated as exceptional cases
- Deactivated tracks are treated as intentional removals
- Deactivated tracks that feed buses, groups, or FX returns are flagged as export risks

If your workflow relies heavily on mute instead of deactivation, results may be less accurate.

---

## Bus and FX Routing Assumptions

The analyzer is optimized for projects that use:

- Internal bus tracks
- Group-based routing
- FX return tracks
- Bus → FX → group feedback chains
- Stem export from groups

Routing-break detection is tuned for this topology and will flag silent chain failures.

---

## Goals

- Detect export-breaking mistakes before rendering
- Identify disabled tracks and devices
- Detect broken bus/group/return routing
- Analyze device chains and settings
- Support AI-assisted mix and loudness feedback
- Provide stable, diffable project snapshots

---

## Key Features

- Track deactivation detection
- Device bypass and automation detection
- Routing topology extraction
- Dead/orphan bus detection
- Routing break depth tracing
- EQ8 band extraction
- Stock device parameter decoding
- Third-party plugin metadata analysis
- QC summary and console report
- Token-safe COMPACT output

---

## Prerequisites

- Python 3.9+
- Standard library only (no external packages required)
- Ableton Live `.als` files

---

## Installation

No installation required.

Copy the script into a working directory.

---

## Usage

Basic:

python ableton_dual_extract.py "MyProject.als"

With device settings:

python ableton_dual_extract.py "MyProject.als" --mix-settings

Minified output:

python ableton_dual_extract.py "MyProject.als" --minify

---

## Console Output (STDOUT)

The script prints a QA summary including:

- Track/device counts
- Fail/warn counts
- Routing break count
- Dead/orphan buses
- Failing track preview
- Routing impact preview

### Example

```
FAIL: 6 tracks
Routing breaks: 4
Dead buses: 1

[dor] 38 JUP8 LEAD
[r]   32 JUP + Piano
```

Legend:

- d = deactivated
- o = device off
- r = routing break

---

## Output Files

For each run, two files are generated.

### 1. FULL Audit File

Filename:

*.full.audit.json

Contains:

- Complete track graph
- Device chains
- Device settings pools
- Routing traces
- Plugin metadata
- QC summaries

Intended for:

- ChatGPT analysis
- Human inspection
- Archival
- Version diffing

Supports deep mix/loudness review.

---

### 2. COMPACT_V2 Audit File

Filename:

*.compact_v2.audit.json

Contains:

- Minimal track/device summary
- Packed reason codes
- Compact routing info

Designed for:

- Claude
- Other limited-context LLMs
- Fast QA checks

Stays under ~25k tokens.

---

## File Architecture

### FULL Structure

- tracks[] – Per-track records
- devices[] – Device chains
- pools – Deduplicated settings
- qc_summary – Global QC stats
- legend – Reason codes

### COMPACT Structure

- tracks[] – Condensed records
- legend – Field and code mappings

---

## Interpreting Device Settings

### Example: EQ Eight

Example extracted settings:

```json
{
  "bands": [
    { "Freq": 120.0, "Gain": -3.2, "Q": 0.71, "Mode": 1 },
    { "Freq": 4500, "Gain": 1.5, "Q": 1.2, "Mode": 1 }
  ]
}
```

This represents the active EQ curve and can be used for mix analysis.

### Example: Utility

```json
{
  "Gain": 0.8,
  "StereoWidth": 1.2,
  "BassMonoFrequency": 120
}
```

Used for gain staging and stereo management.

---

## Using with AI Models

### With ChatGPT (FULL)

Example prompt:

```
Analyze this Ableton project JSON.
Review my master bus, vocal bus, and drum bus.
Suggest mix and loudness improvements.
```

### With Claude (COMPACT)

Example prompt:

```
Review this compact Ableton audit.
Report export risks and routing failures.
```

---

## Example Analysis Prompts

### Find Technical Mistakes

Check for disabled tracks, broken buses,
and export risks. Summarize problems.

---

### Signal Flow Diagram

Build a signal flow diagram from
"38 JUP8 LEAD" to the Master track.

---

### Mix Review

Analyze EQ curves, compression,
and gain staging. Suggest improvements.

---

### Loudness Strategy

Review limiting and clipping chain.
Estimate loudness risks and headroom.

---

### Bus Processing

Evaluate drum and vocal bus processing
and suggest optimization.

---

## Best Practices

- Run before every stem export
- Commit JSON files with projects
- Diff FULL files between versions
- Use COMPACT for quick checks
- Use FULL for detailed review

---

## Limitations

- Most third-party plugin knobs cannot be decoded offline
- Plugin metadata is best-effort
- Some Ableton internals are opaque

---

## Versioning

Current version: 1.0.24

Versions are tracked in the VERSION file and Git tags.

---

## License

Internal / personal use.
No warranty.

---

## Author Notes

This tool is designed to function as an automated
quality gate for Ableton projects and an AI-assisted
mix review system.
