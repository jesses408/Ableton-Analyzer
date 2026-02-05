# ANALYSIS_GUIDE.md — AI Agent Guide for Analyzing Ableton JSON Outputs

This guide is for AI agents (Claude, ChatGPT, etc.) tasked with analyzing the JSON reports produced by `ableton_dual_extract.py` to provide QA feedback and mix/loudness recommendations.

---

## Quick Start

You will receive one or both of these JSON files:

1. **FULL JSON** (`*.full.json`) — Detailed analysis with complete device settings, routing topology, and QC findings. Use this for comprehensive analysis.
2. **COMPACT JSON** (`*.compact.json`) — Token-minimized version designed to fit within Claude's context limits (~25k tokens). Use this when context is limited.

**Your job:** Analyze the JSON and provide actionable feedback on:
- Export/stem safety issues (CRITICAL PRIORITY)
- Mix balance and processing concerns
- Loudness and dynamics recommendations

---

## User Workflow Context (CRITICAL TO UNDERSTAND)

### Track Deactivation vs Mute
- **The user DOES NOT USE MUTE** for silencing tracks
- **Track deactivation** (gray power button in Ableton) is the primary silence mechanism
- Deactivated tracks DO NOT render in exports/stems
- **This is the #1 source of export problems** - always check for deactivated tracks

### Routing Architecture
The user's typical workflow involves:
- **Groups** containing multiple tracks
- **Internal bus tracks** that receive audio from groups
- **Bus → FX Return → Group** feedback chains
- **Deactivated sources break downstream routing** - if a bus receives from a deactivated track, the entire chain is broken

### Export Workflow
- Stems are rendered by soloing groups/buses
- A deactivated track in the chain will result in silent/missing stems
- **Routing breaks are export-breaking bugs, not just warnings**

---

## QC Code Reference

### Critical Codes (Export-Breaking)

| Code | Meaning | Priority | What It Means |
|------|---------|----------|---------------|
| `d` | **Deactivated** | CRITICAL | Track is disabled (gray power button). Will not render in exports. |
| `r` | **Routing Break** | CRITICAL | Track receives from deactivated upstream source. Chain is broken. |
| `x` | **Missing Route Target** | HIGH | Audio routing points to non-existent track. |
| `o` | **Device Off** | HIGH | Device is disabled without automation to explain it. Likely unintentional. |

### Warning Codes

| Code | Meaning | Priority | What It Means |
|------|---------|----------|---------------|
| `a` | **Automated Device** | MEDIUM | Device is off but has on/off automation. May be intentional. |
| `m` | **Muted** | LOW | Track is muted (user doesn't rely on mute, so this is secondary). |
| `s` | **Silent** | LOW | Track appears to produce no audio (if implemented). |

### Special Findings

- **Dead Bus**: Bus has upstream sources, but ALL sources are deactivated → bus is effectively silent
- **Orphan Bus**: Track name suggests bus/return/fx, but it has NO upstream sources → potentially misconfigured

---

## Analysis Priorities

### 1. CRITICAL: Export Safety (Goal A)

**Always check and report first:**

1. **Deactivated Tracks** (`d` code)
   - List all deactivated tracks by name
   - Identify if they're buses, returns, or regular tracks
   - Flag if they're part of routing chains

2. **Routing Breaks** (`r` code)
   - Identify which tracks are affected
   - Trace the break source (which deactivated track caused it)
   - Calculate impact depth (how many hops from deactivated source)
   - **These are critical export bugs**

3. **Dead Buses**
   - Buses that receive only from deactivated sources
   - Will render silent in exports
   - High priority to fix

4. **Orphan Buses**
   - Named like buses but have no inputs
   - May be misconfigured or leftover from routing changes
   - Medium priority

5. **Disabled Devices** (`o` code)
   - Devices turned off without automation
   - May be unintentional (forgot to re-enable after testing)
   - Could affect mix balance

### 2. IMPORTANT: Mix & Loudness Review (Goal B)

**Only after addressing export safety, analyze:**

1. **Device Chain Analysis**
   - Identify processing order per track
   - Look for problematic chains (e.g., limiter before EQ)
   - Flag excessive processing (too many compressors, etc.)

2. **EQ Analysis** (when `--mix-settings` used)
   - Check for extreme boosts/cuts
   - Identify frequency buildup (multiple tracks boosting same range)
   - Note high-pass filter usage (or lack thereof)

3. **Dynamics Processing**
   - Glue Compressor settings (over-compression risks)
   - Drum Buss drive/crunch levels
   - Limiter/compressor stacking

4. **Stereo Width & Utility**
   - Excessive width manipulation
   - Mono bass management
   - Phase issues from stereo processing

5. **Reverb/Delay Balance**
   - Return levels
   - Dry/wet ratios
   - Potential for muddiness

### 3. INFORMATIONAL

- Track counts and organization
- Plugin usage statistics
- Automation presence

---

## Reading the JSON

### FULL JSON Structure

```json
{
  "meta": {
    "tool": "ableton_dual_extract",
    "schema": "full",
    "version": "1.0.25",
    "timestamp": "2024-02-04T..."
  },
  "qc_reason_legend": {
    "d": "deactivated",
    "o": "device_off_no_automation",
    "r": "routing_break",
    // ... more codes
  },
  "qc_summary": {
    "total_tracks": 42,
    "tracks_with_issues": 8,
    "deactivated_count": 3,
    "routing_breaks_count": 5,
    "dead_bus_count": 1,
    "orphan_bus_count": 2
  },
  "tracks": [
    {
      "track_id": "1234",
      "name": "Kick Bus",
      "type": "AudioTrack",
      "active": false,  // CRITICAL: deactivated!
      "solo": false,
      "arm": false,
      "reasons": ["d", "r"],  // deactivated + routing break
      "routing": {
        "audio_in": ["1230", "1231"],  // receives from these tracks
        "audio_out": "1240",  // sends to this track
        "resolved_group_out": "Master"
      },
      "routing_break": {
        "impacted": true,
        "depth": 1,
        "sources": ["1234"],  // this track is the break source
        "messages": ["reachable from deactivated source(s) at depth 1: Kick Bus"]
      },
      "devices": [
        {
          "name": "Glue Compressor",
          "tag": "gluecompressor",
          "enabled": true,
          "settings": {  // only present if --mix-settings used
            "Threshold": -12.0,
            "Ratio": 2.0,
            // ...
          }
        }
      ]
    }
  ]
}
```

### COMPACT JSON Structure

```json
{
  "schema": "compact",
  "legend": {
    "R_codes": {
      "d": "deactivated",
      "o": "device_off",
      "r": "routing_break",
      // ...
    },
    "fields": {
      "n": "name",
      "t": "type",
      "a": "active",
      "R": "reasons",
      // ...
    }
  },
  "qc": {
    "total": 42,
    "issues": 8,
    "deact": 3,
    "rbreak": 5
  },
  "tracks": [
    {
      "n": "Kick Bus",
      "t": "Audio",
      "a": false,  // not active = deactivated
      "R": "dr",   // packed codes: deactivated + routing break
      "i": ["routing_break"],
      "d": [
        {"n": "Glue", "t": "gluecompressor", "e": true}
      ]
    }
  ]
}
```

---

## Analysis Output Format

Structure your analysis report as follows:

### 1. Executive Summary
- Overall project health (Critical/Warning/OK)
- Count of critical issues
- Quick assessment (safe to export? mix concerns?)

### 2. Critical Issues (Export-Breaking)

**Format:**
```
CRITICAL: [Issue Type]
- Track: [Track Name]
- Problem: [Clear description]
- Impact: [What will happen in export]
- Fix: [Actionable instruction]
```

**Example:**
```
CRITICAL: Deactivated Track in Bus Chain
- Track: "Drum Bus"
- Problem: Track is deactivated but receives audio from 8 drum tracks
- Impact: All drum tracks will be silent in stems/exports
- Fix: Re-activate "Drum Bus" track (click gray power button)

CRITICAL: Routing Break
- Track: "Reverb Return A"
- Problem: Receives from deactivated "FX Send" track (depth 1)
- Impact: Reverb return will be silent
- Fix: Re-activate "FX Send" or re-route reverb return
```

### 3. Important Issues (Mix/Loudness)

**Format:**
```
IMPORTANT: [Issue Type]
- Track(s): [Track Name(s)]
- Observation: [What you noticed]
- Concern: [Potential problem]
- Recommendation: [Suggested improvement]
```

**Example:**
```
IMPORTANT: Excessive Low-End Boost
- Tracks: "Bass", "Sub Bass", "Kick"
- Observation: All three tracks have +6dB boost at 60-80Hz
- Concern: Potential for muddy low-end and mastering issues
- Recommendation: Consider reducing overlapping boosts or use sidechain compression

IMPORTANT: Over-Compression
- Track: "Master"
- Observation: Glue Compressor (ratio 4:1, threshold -18dB) → Limiter
- Concern: May reduce dynamics too aggressively
- Recommendation: Ease compression or adjust threshold
```

### 4. Informational Findings

- Track counts
- Device usage statistics
- Routing topology overview
- Automation presence

### 5. Positive Observations (Optional)

Mention things done well:
- Good high-pass filtering
- Appropriate dynamics processing
- Clean routing structure

---

## Common Issues & What to Look For

### Export-Breaking Patterns

1. **Deactivated Bus with Active Sends**
   - Bus track is deactivated
   - Multiple tracks route into it
   - → Silent stem/export

2. **Cascading Routing Breaks**
   - Track A deactivated
   - Track B receives from A (depth 1 break)
   - Track C receives from B (depth 2 break)
   - → Entire chain is broken

3. **Orphan Returns**
   - Named "Return A" or "Reverb Bus"
   - No tracks route into it
   - → Likely misconfigured

4. **Disabled Devices in Critical Paths**
   - Compressor or EQ turned off
   - No automation to explain it
   - → Unintentional, affects mix

### Mix/Loudness Red Flags

1. **EQ Issues**
   - Extreme boosts (>10dB)
   - Multiple tracks boosting same frequency range
   - No high-pass filters on non-bass tracks
   - Narrow Q with extreme cuts (sounds unnatural)

2. **Dynamics Over-Processing**
   - Multiple compressors in series with high ratios
   - Limiter not at end of chain
   - Drum Buss with Drive >60% + Crunch >60%
   - Glue Compressor ratio >4:1 on mix bus

3. **Stereo Issues**
   - Width >200% on multiple tracks (phase issues)
   - No mono-ing of bass frequencies
   - Conflicting stereo processing

4. **Reverb/Delay Buildup**
   - Multiple long reverbs without ducking
   - Delays with high feedback and no filtering
   - Return levels too high (wet>dry balance)

---

## Device Settings Interpretation

### EQ8
- **bands[]**: Array of EQ bands
- **freq**: Frequency in Hz
- **gain**: Boost/cut in dB (-12 to +12 typical)
- **Q**: Width (0.5 = wide, 5.0 = narrow)
- **isOn**: Whether band is active
- **Look for:** Extreme boosts, narrow cuts, frequency buildup

### Glue Compressor
- **Threshold**: Where compression starts (dB)
- **Ratio**: Compression amount (2:1 gentle, 10:1 aggressive)
- **Attack**: Response speed (ms)
- **Release**: Recovery speed (ms)
- **Makeup**: Gain compensation (dB)
- **DryWet**: Parallel compression amount (%)
- **Look for:** Over-compression (ratio >4:1, threshold <-20dB on mix bus)

### Utility (StereoGain)
- **Gain**: Volume adjustment (dB)
- **StereoWidth**: Stereo field (0% = mono, 100% = normal, 200% = wide)
- **Mono**: Force mono toggle
- **BassMono**: Mono bass frequencies
- **BassMonoFreq**: Cutoff for bass mono (Hz)
- **Look for:** Excessive width, missing bass mono on wide tracks

### Drum Buss
- **Drive**: Saturation amount (0-100%)
- **Crunch**: Compression/transient shaping (0-100%)
- **Boom**: Low-end enhancement (0-100%)
- **TransientsSoften/Trim**: Transient control (0-100%)
- **DryWet**: Parallel processing amount (%)
- **Look for:** Extreme settings (>70% on multiple params)

---

## Analysis Checklist

Before delivering your analysis, ensure you've covered:

- [ ] Identified ALL deactivated tracks by name
- [ ] Traced ALL routing breaks to their sources
- [ ] Flagged all dead buses and orphan buses
- [ ] Listed disabled devices without automation
- [ ] Reviewed device chains for problematic ordering
- [ ] Analyzed EQ settings (if present) for extremes/conflicts
- [ ] Checked dynamics processing for over-compression
- [ ] Evaluated stereo width and bass mono usage
- [ ] Assessed reverb/delay balance
- [ ] Provided actionable fixes for critical issues
- [ ] Prioritized export safety over mix preferences
- [ ] Used track NAMES (not IDs) in all feedback

---

## Example Analysis

```markdown
# Ableton Project Analysis: "Acid Breaks 2026-02-03"

## Executive Summary
**Status: CRITICAL ISSUES FOUND - NOT SAFE TO EXPORT**
- 3 critical export-breaking issues detected
- 2 important mix concerns identified
- Must address deactivation and routing breaks before rendering stems

---

## CRITICAL ISSUES (Must Fix Before Export)

### 1. Deactivated Bus Track
- **Track:** "Drum Bus"
- **Problem:** Track is deactivated but receives from 8 active drum tracks
- **Impact:** All drums will be completely silent in exported stems
- **Fix:** Re-activate "Drum Bus" track (click gray power button in Ableton)

### 2. Routing Break: Reverb Return
- **Track:** "Reverb Return A"
- **Problem:** Receives from deactivated "FX Send A" track (depth 1)
- **Impact:** Reverb return will be silent, affecting 12 tracks that use this return
- **Fix:** Re-activate "FX Send A" or re-route reverb return to active send

### 3. Dead Bus Detected
- **Track:** "Parallel Compression Bus"
- **Problem:** All upstream sources (Kick, Snare, Hats) are deactivated
- **Impact:** Bus will render silent
- **Fix:** Re-activate upstream drum tracks or deactivate this bus if not needed

---

## IMPORTANT: Mix & Loudness Concerns

### 1. Excessive Low-End Boost
- **Tracks:** "Bass", "Sub Bass", "Kick"
- **Observation:** All three have +6-8dB boost at 60-80Hz (EQ8)
- **Concern:** Potential muddiness and mastering headroom issues
- **Recommendation:** Reduce overlapping boosts or use sidechain compression between bass and kick

### 2. Over-Compression on Master
- **Track:** "Master"
- **Observation:** Glue Compressor (ratio 4:1, threshold -18dB, makeup +6dB) before limiter
- **Concern:** May be reducing dynamics too aggressively
- **Recommendation:** Try ratio 2.5:1 or raise threshold to -12dB for more dynamic range

---

## Informational

- **Total Tracks:** 42 (32 audio, 8 return, 2 MIDI)
- **Deactivated Count:** 5 tracks
- **Routing Breaks:** 6 affected tracks
- **Device Count:** 87 total devices (23 stock, 64 plugins)

---

## Positive Observations

- Good use of high-pass filters on non-bass tracks (reduces mud)
- Appropriate bass mono management on stereo bass tracks
- Clean routing structure with clear bus organization
- Drum Buss settings are well-balanced (not over-processed)

---

## Action Items (Priority Order)

1. **CRITICAL:** Re-activate "Drum Bus" track
2. **CRITICAL:** Fix "FX Send A" → "Reverb Return A" routing break
3. **CRITICAL:** Review "Parallel Compression Bus" - re-activate sources or remove bus
4. **IMPORTANT:** Reduce overlapping low-end boosts on bass/kick
5. **IMPORTANT:** Ease master bus compression settings
```

---

## Final Notes

- **Always prioritize export safety over mix aesthetics** - a perfectly mixed project that renders silent is useless
- **Be specific with track names** - use the exact names from the JSON
- **Provide actionable fixes** - don't just identify problems, explain how to solve them
- **Understand the user's workflow** - deactivation is intentional for silence, not an error in itself
- **Context matters** - a deactivated track that feeds into active buses is critical; a deactivated unused track is not

When in doubt, **flag it as critical** - better to over-warn about export issues than miss them.
