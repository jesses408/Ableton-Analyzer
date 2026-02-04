#!/usr/bin/env python3
"""
Ableton Live .als/.xml -> two outputs:
  1) FULL JSON (ChatGPT): detailed but pruned
  2) COMPACT JSON (Claude): token-minimized schema with short keys

This version includes important FIXES for device naming + enabled/disabled accuracy:

Fixes:
  - Prevents bogus plugin/device names like "true"/"false"/"0"/"1" from being used as names/products/vendors.
  - Tightens device enabled/bypass detection:
      * Prefer a "DeviceOn"/"IsOn"/"On"/"Enabled" value that is close to the device root (shallow search)
      * Ignore obviously unrelated deep boolean parameters that can masquerade as enabled flags
      * If not confidently found, returns enabled=None (unknown) rather than False
  - Compact issue "alldis" only triggers when ALL devices are explicitly enabled==False.
    (Previously, False+None could incorrectly trigger "alldis".)

Notes:
  - Ableton XML schema varies across Live versions; extraction is best-effort.
  - Third-party plugins often store state in opaque binary; param extraction will be incomplete for many plugins.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_VERSION = "1.0.25"

# -----------------------------
# IO helpers
# -----------------------------

def is_gzip_file(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def read_xml_bytes(path: str) -> bytes:
    if path.lower().endswith(".als") or is_gzip_file(path):
        with gzip.open(path, "rb") as f:
            return f.read()
    with open(path, "rb") as f:
        return f.read()


def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()



def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


_HEX_CLEAN_RX = re.compile(r"[^0-9a-fA-F]+")


def extract_plugin_state_bytes(device_elem: ET.Element) -> Optional[bytes]:
    """Best-effort: Ableton often stores 3rd-party plugin state in a <ProcessorState> hex blob."""
    # Common tags that may carry the plugin processor state
    for tag in ("ProcessorState", "PluginState", "State", "Chunk", "VstState", "AUState"):
        el = device_elem.find(f".//{tag}")
        if el is None:
            continue
        txt = el.text or ""
        txt = txt.strip()
        if not txt:
            continue
        # Most often: hex-encoded binary with whitespace/newlines
        cleaned = _HEX_CLEAN_RX.sub("", txt)
        if cleaned and all(c in "0123456789abcdefABCDEF" for c in cleaned) and len(cleaned) % 2 == 0 and len(cleaned) >= 32:
            try:
                return bytes.fromhex(cleaned)
            except Exception:
                continue
        # Fallback: treat as raw text
        try:
            return txt.encode("utf-8", errors="ignore")
        except Exception:
            return None
    return None


_ASCII_STR_RX = re.compile(rb"[\x20-\x7E]{4,}")  # printable ASCII, len>=4


def extract_state_hints_from_bytes(b: bytes, max_strings: int = 40, max_len: int = 96) -> List[str]:
    """Extract readable strings from binary plugin state (ASCII + UTF-16LE-ish). Capped for size safety."""
    hints: List[str] = []
    seen = set()

    # ASCII
    for m in _ASCII_STR_RX.finditer(b):
        s = m.group(0).decode("ascii", errors="ignore").strip()
        if not s:
            continue
        if len(s) > max_len:
            s = s[:max_len] + "…"
        if s not in seen:
            seen.add(s)
            hints.append(s)
        if len(hints) >= max_strings:
            return hints

    # UTF-16LE heuristic: look for 0x00-separated ASCII letters; decode chunks
    try:
        u = b.decode("utf-16le", errors="ignore")
        # Keep only reasonable printable runs
        runs = re.findall(r"[\w\s\-\.:/#]{6,}", u)
        for r in runs:
            r = r.strip()
            if not r:
                continue
            if len(r) > max_len:
                r = r[:max_len] + "…"
            if r not in seen and any(ch.isalpha() for ch in r):
                seen.add(r)
                hints.append(r)
            if len(hints) >= max_strings:
                break
    except Exception:
        pass

    return hints


def _find_balanced_json(text: str, start: int, max_len: int = 200000) -> Optional[str]:
    """Find a JSON object starting at or after start by balancing braces. Best-effort, bounded."""
    n = len(text)
    i = text.find("{", start)
    if i < 0:
        return None
    end_limit = min(n, i + max_len)
    depth = 0
    in_str = False
    esc = False
    for j in range(i, end_limit):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        else:
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[i:j+1]
    return None


def _extract_plugin_text_blobs(b: bytes, max_text: int = 400000) -> str:
    """Decode bytes to text best-effort (utf-8 then latin-1) and bound size."""
    try:
        t = b.decode("utf-8", errors="ignore")
    except Exception:
        t = ""
    if not t:
        try:
            t = b.decode("latin-1", errors="ignore")
        except Exception:
            t = ""
    if len(t) > max_text:
        t = t[:max_text]
    return t


def decode_plugin_state_best_effort(identifier: Optional[str], b: Optional[bytes]) -> Optional[Dict[str, Any]]:
    """Attempt to decode common plugin state encodings (embedded JSON/XML). Safe + bounded."""
    if not b:
        return None

    out: Dict[str, Any] = {}

    # Role classification from identifier (helps analysis even without knobs)
    if identifier:
        low = identifier.lower()
        role = None
        if "limiter" in low:
            role = "limiter"
        elif "clip" in low:
            role = "clipper"
        elif "compress" in low:
            role = "compressor"
        elif "transient" in low:
            role = "transient_shaper"
        elif "exciter" in low:
            role = "exciter"
        elif "satur" in low or "distort" in low:
            role = "saturator"
        elif "eq" in low:
            role = "eq"
        if role:
            out["role"] = role

    text = _extract_plugin_text_blobs(b)

    # Known marker: Xfer Serum2 embeds JSON after 'XferJson'
    if "XferJson" in text:
        try:
            k = text.find("XferJson")
            js = _find_balanced_json(text, k)
            if js:
                data = json.loads(js)
                # Keep small, high-value subset
                out["json"] = {
                    k2: data.get(k2)
                    for k2 in ("product", "productVersion", "vendor", "hash", "preset", "presetName", "name", "version")
                    if isinstance(data, dict) and k2 in data
                }
                out["json_keys"] = sorted(list(data.keys()))[:60] if isinstance(data, dict) else None
        except Exception:
            pass

    # Generic embedded JSON object
    if "json" not in out:
        try:
            js = _find_balanced_json(text, 0)
            if js:
                data = json.loads(js)
                if isinstance(data, dict):
                    out["json"] = {k: data.get(k) for k in list(data.keys())[:40]}
                    out["json_keys"] = sorted(list(data.keys()))[:60]
        except Exception:
            pass

    # Embedded XML (some plugins store JUCE ValueTree XML or similar)
    if "<?xml" in text or "<" in text:
        try:
            xi = text.find("<?xml")
            if xi < 0:
                # sometimes starts at '<STATE' or '<root'
                xi = text.find("<")
            if xi >= 0:
                # Heuristic: take a bounded slice and try parse
                xs = text[xi:xi+250000]
                # Trim to last closing angle bracket
                last = xs.rfind(">")
                if last > 0:
                    xs = xs[:last+1]
                root = ET.fromstring(xs)
                # Extract a bounded set of interesting leaf values/attrs
                interesting = []
                keys = ("preset", "name", "mode", "ceiling", "threshold", "ratio", "attack", "release", "drive", "oversample", "true", "gain")
                def walk(node, path, depth=0):
                    if depth > 10 or len(interesting) >= 200:
                        return
                    tag = re.sub(r"\{.*\}", "", node.tag)
                    p2 = f"{path}/{tag}" if path else tag
                    # attributes
                    for ak, av in list(node.attrib.items())[:20]:
                        lk = ak.lower()
                        if any(k in lk for k in keys):
                            interesting.append({"path": f"{p2}@{ak}", "value": av})
                            if len(interesting) >= 200:
                                return
                    txt = (node.text or "").strip()
                    if txt and len(txt) <= 200:
                        ltxt = txt.lower()
                        if any(k in ltxt for k in keys):
                            interesting.append({"path": p2, "value": txt})
                            if len(interesting) >= 200:
                                return
                    for child in list(node)[:200]:
                        walk(child, p2, depth+1)
                walk(root, "")
                out["xml_hints"] = interesting[:200] if interesting else None
                out["xml_root"] = re.sub(r"\{.*\}", "", root.tag)
        except Exception:
            pass

    return out if out else None


def plugin_role_from_identifier(identifier: Optional[str]) -> Optional[str]:
    if not identifier:
        return None
    low = identifier.lower()
    if "limiter" in low:
        return "limiter"
    if "clip" in low:
        return "clipper"
    if "compress" in low or "comp" in low:
        return "compressor"
    if "transient" in low:
        return "transient_shaper"
    if "exciter" in low:
        return "exciter"
    if "satur" in low or "distort" in low or "drive" in low:
        return "saturator"
    if "eq" in low:
        return "eq"
    if "reverb" in low:
        return "reverb"
    if "delay" in low or "echo" in low:
        return "delay"
    return None


def plugin_hint_tags_from_bytes(b: Optional[bytes]) -> List[str]:
    """Return a SMALL set of vendor/tech tags from a plugin state blob. Bounded and stable."""
    if not b:
        return []
    tags: List[str] = []
    # Fast substring checks on bytes
    def has(sub: bytes) -> bool:
        return (sub in b)

    # Common frameworks / vendors
    if has(b"FFBS") or has(b"FFPB") or has(b"FFpr") or has(b"FFQ") or has(b"FabFilter"):
        tags.append("fabfilter")
    if has(b"JUCE") or has(b"juce"):
        tags.append("juce")
    if has(b"iZotope") or has(b"izotope") or has(b"Ozone") or has(b"Neutron"):
        tags.append("izotope")
    if has(b"KClip") or has(b"kazrog") or has(b"Kazrog"):
        tags.append("kazrog")
    if has(b"Xfer") or has(b"XferJson") or has(b"Serum"):
        tags.append("xfer")
    if has(b"Infiltrator") or has(b"devious") or has(b"Devious"):
        tags.append("devious")
    if has(b"VST3") or has(b"VST2") or has(b"VST "):
        tags.append("vst")
    if has(b"AU") and has(b"AudioUnit"):
        tags.append("au")

    return tags[:6]

# -----------------------------
# Generic XML helpers
# -----------------------------

_BOOL_LITERALS = {"true", "false", "0", "1", "yes", "no"}

# -----------------------------
# QC code legends
# -----------------------------
QC_REASON_LEGEND = {
    "d": "track deactivated (gray power button)",
    "o": "device off AND not automated (export risk)",
    "m": "muted (not used in your workflow, but detected when present)",
    "s": "silent track guess (volume very low / -inf)",
    "x": "routing target missing/unknown (could not resolve)",
    "r": "routing impacted by deactivated track (bus/group/return chain break)",
}

QC_WARNING_LEGEND = {
    "a": "device off but On/Off is automated (likely intentional)",
}




def normalize_text(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s2 = s.strip()
    return s2 if s2 else None


def is_boolish_text(s: Optional[str]) -> bool:
    if s is None:
        return False
    return s.strip().lower() in _BOOL_LITERALS


def normalize_non_boolish(s: Optional[str]) -> Optional[str]:
    """
    Normalize and reject values that are clearly boolean-ish ("true"/"false"/"0"/"1").
    """
    s2 = normalize_text(s)
    if s2 is None:
        return None
    if is_boolish_text(s2):
        return None
    return s2


def parse_bool(s: Optional[str]) -> Optional[bool]:
    if s is None:
        return None
    sl = s.strip().lower()
    if sl in ("true", "1", "yes"):
        return True
    if sl in ("false", "0", "no"):
        return False
    return None


def normalize_scalar(v: Any) -> Any:
    """
    Convert simple string-like values to int/float/bool when safe.
    IMPORTANT: treat "0"/"1" as numbers (not booleans) because many device params
    use 0/1 numeric encodings.
    """
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return v
    s = str(v).strip()
    if s == "":
        return None

    # int?
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            pass

    # float?
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?", s):
        try:
            return float(s)
        except Exception:
            pass

    # bool (textual only)
    sl = s.lower()
    if sl in ("true", "yes"):
        return True
    if sl in ("false", "no"):
        return False

    return s



def bool_from_node_manual(n: ET.Element) -> Optional[bool]:
    """
    Best-effort boolean extraction from an Ableton node.

    Order:
      1) node.get("Value") or node.get("Manual")
      2) child <Manual Value="..."> (and one nested level deeper)

    Returns True/False if found, else None.
    """
    v = n.get("Value") or n.get("Manual")
    b = parse_bool(v)
    if b is not None:
        return b

    # Common nested form: <Mute><Manual Value="true"/></Mute>
    for ch in list(n)[:25]:
        if ch.tag == "Manual" or (isinstance(ch.tag, str) and ch.tag.endswith("Manual")):
            b2 = parse_bool(ch.get("Value") or ch.get("Manual"))
            if b2 is not None:
                return b2
        # Occasionally nested further: <Mute><Something><Manual Value="..."/></Something></Mute>
        for gch in list(ch)[:25]:
            if gch.tag == "Manual" or (isinstance(gch.tag, str) and gch.tag.endswith("Manual")):
                b3 = parse_bool(gch.get("Value") or gch.get("Manual"))
                if b3 is not None:
                    return b3
    return None

def parse_float(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except Exception:
        return None


def find_first(elem: ET.Element, tag_name: str) -> Optional[ET.Element]:
    for d in elem.iter():
        if d.tag == tag_name:
            return d
    return None


def first_descendant_attr(elem: ET.Element, tag_regex: str, attr: str = "Value") -> Optional[str]:
    rx = re.compile(tag_regex)
    for d in elem.iter():
        if rx.search(d.tag):
            v = d.get(attr)
            if v is not None and v != "":
                return v
    return None


def first_descendant_attr_any(elem: ET.Element, tag_regex: str, attrs: List[str]) -> Optional[str]:
    rx = re.compile(tag_regex)
    for d in elem.iter():
        if rx.search(d.tag):
            for a in attrs:
                v = d.get(a)
                if v is not None and v != "":
                    return v
    return None


def iter_with_depth(root: ET.Element, max_depth: int) -> List[Tuple[ET.Element, int]]:
    """
    BFS traversal up to max_depth from root.
    Returns list of (element, depth).
    """
    out: List[Tuple[ET.Element, int]] = []
    q: List[Tuple[ET.Element, int]] = [(root, 0)]
    while q:
        node, d = q.pop(0)
        out.append((node, d))
        if d >= max_depth:
            continue
        for ch in list(node):
            q.append((ch, d + 1))
    return out


# -----------------------------
# Ableton-specific extraction
# -----------------------------

def find_liveset_root(xml_bytes: bytes) -> ET.Element:
    root = ET.fromstring(xml_bytes)
    if root.tag == "LiveSet":
        return root
    ls = find_first(root, "LiveSet")
    return ls if ls is not None else root


def extract_track_name(track_elem: ET.Element) -> Optional[str]:
    for pat in (r"EffectiveName$", r"UserName$", r"TrackName$", r"Name$"):
        v = first_descendant_attr(track_elem, pat, "Value")
        v = normalize_non_boolish(v)
        if v:
            return v
    return None



def extract_track_flags(track_elem: ET.Element) -> Dict[str, Optional[bool]]:
    """
    Track mute/solo/arm can be stored either directly on the node as attributes
    or nested as <Mute><Manual Value="true"/></Mute> (and similar).

    IMPORTANT (this project / Live versions):
      - The *track* mute button is stored in the track's Mixer subtree (when present).
      - Many tracks also contain device/internal "Mute" parameters (e.g., inside StereoGain),
        which are NOT the track mute button. We must avoid treating those as track mute.

    Track activation (the gray "Deactivate Track" button) is stored separately.
    In this project's .als, it appears under: DeviceChain/Mixer/Speaker (Manual Value="true/false").

    Keep regex-based tag matching, but once a candidate node is found, extract the
    boolean with a nested Manual fallback.
    """
    def find_flag(tag_regex: str) -> Optional[bool]:
        rx = re.compile(tag_regex)
        for d in track_elem.iter():
            if rx.search(d.tag or ""):
                b = bool_from_node_manual(d)
                if b is not None:
                    return b
        return None

    def find_flag_in(subtree: Optional[ET.Element], tag_regex: str) -> Optional[bool]:
        if subtree is None:
            return None
        rx = re.compile(tag_regex)
        for d in subtree.iter():
            if rx.search(d.tag or ""):
                b = bool_from_node_manual(d)
                if b is not None:
                    return b
        return None

    # Track activator (best-effort): prefer Mixer/Speaker.
    active: Optional[bool] = None
    speaker = track_elem.find("./DeviceChain/Mixer/Speaker")
    if speaker is not None:
        active = bool_from_node_manual(speaker)
    if active is None:
        # Fallback to any Speaker node in the track subtree.
        active = find_flag(r"(Speaker)$")

    deactivated: Optional[bool] = None
    if active is not None:
        deactivated = (not active)

    # TRACK MUTE:
    # Prefer the Mixer subtree and do NOT fall back to searching the whole track,
    # because that frequently finds device/internal "Mute" parameters (e.g., StereoGain/Mute).
    mixer = track_elem.find("./DeviceChain/Mixer")
    muted = (
        find_flag_in(mixer, r"(IsMuted|Mute)$")
        or find_flag_in(mixer, r"(TrackMute|MuteButton|MuteState)$")
    )

    return {
        "muted": muted,
        "solo": find_flag(r"(IsSolo|Solo)$"),
        "arm": find_flag(r"(IsArmed|Arm|RecordArm)$"),
        "active": active,
        "deactivated": deactivated,
    }


def extract_track_routing(track_elem: ET.Element) -> Dict[str, Any]:
    routing: Dict[str, Any] = {}
    ai = find_first(track_elem, "AudioInputRouting")
    ao = find_first(track_elem, "AudioOutputRouting")
    mi = find_first(track_elem, "MidiInputRouting")
    mo = find_first(track_elem, "MidiOutputRouting")

    def routing_target(node: Optional[ET.Element]) -> Optional[str]:
        if node is None:
            return None
        # These strings are useful even if they contain "Track.N" etc.; don't bool-filter them.
        return (
            first_descendant_attr(node, r"(Target|TargetName|DisplayString)$", "Value")
            or first_descendant_attr(node, r"(Enum|Value)$", "Value")
        )

    if ai is not None:
        routing["audio_in"] = routing_target(ai)
    if ao is not None:
        routing["audio_out"] = routing_target(ao)
    if mi is not None:
        routing["midi_in"] = routing_target(mi)
    if mo is not None:
        routing["midi_out"] = routing_target(mo)

    return routing




def extract_track_mixer(track_elem: ET.Element) -> Dict[str, Any]:
    volume = first_descendant_attr_any(
        track_elem,
        r"(Volume|TrackVolume|MixerVolume|MainVolume|OutputVolume|TrackVol)$",
        ["Manual", "Value"],
    )
    pan = first_descendant_attr_any(
        track_elem,
        r"(Pan|TrackPan|MixerPan|MainPan|OutputPan|TrackPanVal)$",
        ["Manual", "Value"],
    )

    # Fallback: explicit <Mixer> subtree
    if volume is None or pan is None:
        mixer_node = find_first(track_elem, "Mixer")
        if mixer_node is not None:
            if volume is None:
                volume = first_descendant_attr_any(
                    mixer_node,
                    r"(Volume|TrackVolume|MixerVolume|MainVolume|OutputVolume)$",
                    ["Manual", "Value"],
                )
            if pan is None:
                pan = first_descendant_attr_any(
                    mixer_node,
                    r"(Pan|TrackPan|MixerPan|MainPan|OutputPan)$",
                    ["Manual", "Value"],
                )

    vol_f = parse_float(volume)
    pan_f = parse_float(pan)

    vol_silent = None
    if vol_f is not None:
        vol_silent = vol_f <= 1e-6  # often linear 0..1

    return {
        "volume_raw": volume,
        "pan_raw": pan,
        "volume": vol_f,
        "pan": pan_f,
        "volume_silent_guess": vol_silent,
    }


def extract_parent_group_id(track_elem: ET.Element) -> Optional[str]:
    candidates = [
        "TrackGroupId",
        "ParentGroupId",
        "ParentGroup",
        "GroupId",
        "GroupTrackId",
        "TrackGroup",
    ]
    for tag in candidates:
        n = find_first(track_elem, tag)
        if n is not None:
            v = n.get("Value") or n.get("Id") or (n.text.strip() if n.text else None)
            v = normalize_text(v)
            if v and not is_boolish_text(v):
                return v

    v = first_descendant_attr(track_elem, r"(TrackGroupId|ParentGroupId|GroupId|GroupTrackId)$", "Value")
    v = normalize_text(v)
    if v and not is_boolish_text(v):
        return v
    return None



# -----------------------------
# Final QA / reference detection
# -----------------------------

_REF_NAME_RX = re.compile(
    r"\b("
    r"ref|reference|mixref|a\s*/\s*b|\bab\b|compare|demo|guide|scratch|temp|test|"
    r"print|bounce|stem|render"
    r")\b",
    re.IGNORECASE,
)

def compute_final_qc_flags(track: Dict[str, Any]) -> Dict[str, Any]:
    """
    Final-project QA flags: intended to catch 'oops, I left something muted/off/at -inf'
    before handing off a project.

    Output uses short codes to keep FULL size down:
      reasons:
        m = muted
        d = deactivated (track activator off)
        s = silent fader (volume_silent_guess)
        x = all devices explicitly disabled (enabled == False)
        o = at least one device explicitly off (enabled == False) without On/Off automation
        r = routing broken/impacted by deactivated track (upstream or downstream)
      warnings:
        a = some device has on/off automation (informational; not necessarily an error)
    """
    flags = track.get("flags") or {}
    mixer = track.get("mixer") or {}
    devs = track.get("devices") or []

    reasons: List[str] = []
    warnings: List[str] = []

    if flags.get("muted") is True:
        reasons.append("m")
    if flags.get("deactivated") is True:
        reasons.append("d")
    if mixer.get("volume_silent_guess") is True:
        reasons.append("s")

    if devs:
        enabled_states = [d.get("enabled") for d in devs]
        if all(s is False for s in enabled_states) and not any(d.get("has_on_automation", False) for d in devs):
            reasons.append("x")
        # Device explicitly off and not power-automated (more likely a mistake)
        if any((d.get("enabled") is False) and (not d.get("has_on_automation", False)) for d in devs):
            reasons.append("o")
        # Any device has on/off automation (informational)
        if any(d.get("has_on_automation", False) for d in devs):
            warnings.append("a")

    # Routing impact (computed in a second pass). Treat as a hard fail.
    if track.get("routing_break") is True:
        reasons.append("r")

    # Fail if any reason present (final QA mode)
    fail = bool(reasons)

    # De-dupe while preserving stable order
    reasons = list(dict.fromkeys(reasons))
    warnings = list(dict.fromkeys(warnings))

    return {"fail": fail, "reasons": reasons, "warnings": warnings}


def classify_plugin_format(device_elem: ET.Element) -> str:
    tag = device_elem.tag.lower()
    if "vst3" in tag:
        return "VST3"
    if "vst" in tag:
        return "VST"
    if "au" in tag:
        return "AU"
    if "plug" in tag:
        return "Plugin"
    return "Device"


def extract_plugin_identity(device_elem: ET.Element) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    More conservative identity extraction; rejects bool-ish 'names' like "true".
    """
    vendor = first_descendant_attr(device_elem, r"(Vendor|Company|Manufacturer)$", "Value")
    product = first_descendant_attr(device_elem, r"(Product|Plug(Name|InName)|PluginName|Name)$", "Value")
    ident = first_descendant_attr(device_elem, r"(Identifier|UniqueId|PluginId|VstId|AUId|Uid)$", "Value")
    path = first_descendant_attr(device_elem, r"(Path|FilePath|FileName)$", "Value")

    vendor = normalize_non_boolish(vendor)
    product = normalize_non_boolish(product)
    identifier = normalize_text(ident) or normalize_text(path)

    # identifier can be path-like; don't bool-filter it
    return vendor, product, identifier


def extract_device_display_name(device_elem: ET.Element) -> Optional[str]:
    for pat in (r"(UserName|EffectiveName)$", r"(Plug(Name|InName)|PluginName)$", r"Name$"):
        v = first_descendant_attr(device_elem, pat, "Value")
        v = normalize_non_boolish(v)
        if v:
            return v
    return None


def extract_named_param_pairs(device_elem: ET.Element, limit: int = 200) -> Dict[str, Any]:
    named: Dict[str, Any] = {}
    count = 0

    # Pattern A: Name + (Value|Manual|Amount)
    for e in device_elem.iter():
        if count >= limit:
            break
        n = e.get("Name")
        if not n:
            continue
        val = e.get("Value") or e.get("Manual") or e.get("Amount")
        if val is None:
            continue
        nn = normalize_text(n)
        vv = normalize_text(val)
        if nn and vv is not None and nn not in named:
            named[nn] = vv
            count += 1

    # Pattern B: ParameterName + ParameterValue in local subtree
    if count < limit:
        for sub in device_elem.iter():
            if count >= limit:
                break
            pname = None
            pval = None
            for d in list(sub)[:50]:
                if d.tag.endswith("ParameterName"):
                    pname = d.get("Value")
                elif d.tag.endswith("ParameterValue") or d.tag.endswith("PluginFloatParameter"):
                    pval = d.get("Value")
            if pname and pval:
                nn = normalize_text(pname)
                vv = normalize_text(pval)
                if nn and vv is not None and nn not in named:
                    named[nn] = vv
                    count += 1

    return named


def prune_param_map(old_params: Dict[str, Any]) -> Dict[str, Any]:
    """
    FULL JSON size reduction:
      - Drop common structural/noisy parameter wrapper nodes that explode output size,
        especially for third-party plugins (ParameterName/ParameterId lists, wrappers, etc.).
      - Keep only entries that look like actual parameter values that could matter for QA.

    Heuristics:
      - Only consider dict-like entries with {tag,name,value_raw}
      - Drop known wrapper/list-ish tags and anything that *contains* those markers
      - Drop ultra-common "constant" sentinel values
      - Keep only if value_raw parses as bool or float, OR is a short (<80) non-empty string
      - Additionally drop tags that are obviously list containers (end with 'List', contain 'Wrapper')
    """
    pruned: Dict[str, Any] = {}

    drop_exact = {
        "ParametersListWrapperLomId",
        "ParameterName",
        "ParameterId",
        "ParameterIdFlankBool",
        "StoredAllParameters",
        "AllParameters",
        "ParameterInfo",
        "ParameterInfoList",
        "ParameterValueList",
        "AutomationLaneList",
        "AutomationLane",
        "SourceContext",
    }
    drop_contains = (
        "StoredAllParameters",
        "AllParameters",
        "ParameterIdFlankBool",
        "ParametersListWrapper",
        "ParameterInfo",
        "AutomationLane",
        "SourceContext",
    )

    def looks_container_tag(tag: str) -> bool:
        t = tag.lower()
        return (
            t.endswith("list")
            or "wrapper" in t
            or "container" in t
            or "bank" in t and "parameter" in t
        )

    for k, v in (old_params or {}).items():
        if not isinstance(v, dict):
            continue

        tag = (v.get("tag") or "").strip()
        if not tag:
            continue

        if tag in drop_exact:
            continue
        if any(x in tag for x in drop_contains):
            continue
        if looks_container_tag(tag):
            continue

        name = v.get("name")
        value_raw = v.get("value_raw")

        # Drop self-evident duplicates like {"name":"Foo","value_raw":"Foo"}
        if isinstance(name, str) and isinstance(value_raw, str) and name == value_raw:
            continue

        # Drop empty / sentinel ParameterId-ish values
        if tag.lower().startswith("parameterid") and isinstance(value_raw, str) and value_raw.strip() in ("-1", ""):
            continue

        # Some Ableton nodes use odd constants; treat as noise.
        if isinstance(value_raw, str) and value_raw.strip() in ("0.1234567687", "0.0.0.0"):
            continue

        keep = False
        if isinstance(value_raw, str):
            s = value_raw.strip()
            if parse_bool(s) is not None:
                keep = True
            elif parse_float(s) is not None:
                keep = True
            else:
                # Keep short strings that might be meaningful, but avoid identifiers/paths explosions
                if s and len(s) <= 80 and not re.search(r"[/\\]{2,}|\.[a-zA-Z0-9]{3,4}$", s):
                    keep = True

        if keep:
            pruned[k] = v

    return pruned


# -----------------------------
# Mix-audit key parameter extraction (opt-in)
# -----------------------------

def _tail_tag(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.split("}")[-1]  # handle namespaces


def extract_key_settings_from_tags(device_elem: ET.Element,
                                   allow_terms: Tuple[str, ...],
                                   max_items: int = 80,
                                   max_depth: int = 10) -> Dict[str, Any]:
    """
    Best-effort extraction of a small set of *useful* device settings without dumping
    every parameter wrapper node.

    Strategy:
      - BFS through the device subtree up to max_depth.
      - Keep nodes whose *tail tag* contains any allow_terms (case-insensitive).
      - Pull a value from common attrs: Value, Manual, Amount
      - Parse bool/float when possible; otherwise keep short strings.

    This is meant for mixing/loudness advice (EQ thresholds, limiter ceilings, etc.),
    and is intentionally bounded to avoid FULL size explosions.
    """
    out: Dict[str, Any] = {}
    allow = tuple(t.lower() for t in allow_terms)

    for node, depth in iter_with_depth(device_elem, max_depth=max_depth):
        if len(out) >= max_items:
            break

        t = _tail_tag(node.tag or "")
        tl = t.lower()
        if not t:
            continue
        if not any(term in tl for term in allow):
            continue

        raw = node.get("Value") or node.get("Manual") or node.get("Amount")
        raw = normalize_text(raw)
        if raw is None:
            continue

        val: Any = parse_bool(raw)
        if val is None:
            fv = parse_float(raw)
            if fv is not None:
                val = fv
            else:
                # Keep short strings only
                if len(raw) <= 80:
                    val = raw
                else:
                    continue

        key = t
        # Avoid collisions: append a counter if needed
        if key in out:
            suffix = 2
            while f"{key}_{suffix}" in out and suffix < 50:
                suffix += 1
            key = f"{key}_{suffix}"

        out[key] = val

    return out



def _manual_value_from_param(param_elem: ET.Element) -> Optional[Any]:
    """
    Many Live device parameters are stored like:
      <Gain><Manual Value="0.0"/></Gain>
    or with nested Timeable/Manual.
    Returns parsed float/int/bool when possible.
    """
    if param_elem is None:
        return None

    # Common cases
    man = param_elem.find(".//Manual")
    if man is not None:
        v = man.get("Value")
        if v is None and man.text:
            v = man.text.strip()
        return normalize_scalar(v)

    # Some params store value directly in attribute
    v = param_elem.get("Value")
    if v is not None:
        return normalize_scalar(v)

    # Sometimes as text
    if param_elem.text and param_elem.text.strip():
        return normalize_scalar(param_elem.text.strip())

    return None


def _get_param(device_elem: ET.Element, tag: str) -> Optional[Any]:
    el = device_elem.find(f".//{tag}")
    return _manual_value_from_param(el) if el is not None else None


def _get_param_attr(device_elem: ET.Element, path: str, attr: str = "Value") -> Optional[Any]:
    el = device_elem.find(path)
    if el is None:
        return None
    v = el.get(attr)
    if v is None and el.text:
        v = el.text.strip()
    return normalize_scalar(v)


def _extract_eq8_bands(eq8_elem: ET.Element) -> Optional[List[Dict[str, Any]]]:
    bands: List[Dict[str, Any]] = []
    for i in range(8):
        b = eq8_elem.find(f".//Bands.{i}")
        if b is None:
            continue

        def band_side(side_tag: str) -> Dict[str, Any]:
            side = b.find(side_tag)
            if side is None:
                return {}
            out: Dict[str, Any] = {}
            for k in ("IsOn", "Mode", "Freq", "Gain", "Q"):
                v = _manual_value_from_param(side.find(k))
                if v is not None:
                    out[k] = v
            return out

        A = band_side("ParameterA")
        B = band_side("ParameterB")
        if not A and not B:
            continue
        bands.append({"i": i, "A": A or None, "B": B or None})

    return bands or None


def _extract_mxd_params(mxd_elem: ET.Element, max_params: int = 64) -> Optional[List[Dict[str, Any]]]:
    """
    Max for Live devices (MxDeviceMidiEffect / MxDeviceAudioEffect) store parameters under:
      ParameterList/ParameterList/(MxD*Parameter)
    We capture Name + current Manual value (Timeable/Manual).
    """
    out: List[Dict[str, Any]] = []
    plist = mxd_elem.find(".//ParameterList/ParameterList")
    if plist is None:
        return None

    for p in list(plist)[:max_params]:
        name = _get_param_attr(p, "Name", "Value")
        if not name:
            continue
        val = _manual_value_from_param(p.find(".//Timeable"))
        out.append({"n": str(name), "v": val})
    return out or None


def _extract_group_device_structure(group_elem: ET.Element) -> Optional[Dict[str, Any]]:
    """
    InstrumentGroupDevice / DrumGroupDevice: capture macros + branch names/ranges.
    This is the 'signal path structure' relevant to analysis.
    """
    d: Dict[str, Any] = {}

    # Chain selector (if present)
    cs = group_elem.find(".//ChainSelector")
    if cs is not None:
        d["chain_selector"] = _manual_value_from_param(cs)

    # Macros
    macros: List[Dict[str, Any]] = []
    for i in range(16):
        name_el = group_elem.find(f".//MacroDisplayNames.{i}")
        val_el = group_elem.find(f".//MacroControls.{i}")
        if name_el is None and val_el is None:
            continue
        name = _get_param_attr(group_elem, f".//MacroDisplayNames.{i}", "Value")
        val = _manual_value_from_param(val_el) if val_el is not None else None
        if name is None and val is None:
            continue
        macros.append({"i": i, "n": name, "v": val})
    if macros:
        d["macros"] = macros

    # Branches / chains
    branches: List[Dict[str, Any]] = []
    br = group_elem.find(".//Branches")
    if br is not None:
        for b in list(br)[:128]:
            bname = _get_param_attr(b, "Name", "Value")
            sel = b.find("BranchSelectorRange")
            lo = _get_param_attr(sel, "Min", "Value") if sel is not None else None
            hi = _get_param_attr(sel, "Max", "Value") if sel is not None else None
            branches.append({
                "n": bname,
                "range": [lo, hi] if lo is not None or hi is not None else None,
                "selected": _get_param_attr(b, "IsSelected", "Value"),
                "solo": _get_param_attr(b, "IsSoloed", "Value"),
            })
    if branches:
        d["branches"] = branches

    return d or None


def extract_device_key_settings(device_elem: ET.Element, device_tag: str) -> Optional[Dict[str, Any]]:
    """
    Device-specific 'high value' settings capture for mix/loudness advice.

    This is intentionally bounded: we capture what you need to reason about the mix
    (EQ points, dynamics, gain staging, time FX parameters, etc.) without dumping
    every wrapper/GUI field.
    """
    tag = (device_tag or "").lower()
    out: Dict[str, Any] = {}

    # ---------- STOCK AUDIO EFFECTS ----------
    if tag == "eq8":
        bands = _extract_eq8_bands(device_elem)
        if bands:
            out["bands"] = bands
        # Also keep a few global toggles if present
        for k in ("AdaptiveQFactor", "ChannelMode", "AnalyzeOn", "SelectedBand"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "stereogain":  # Utility
        for k in ("Gain", "StereoWidth", "Mono", "BassMono", "BassMonoFrequency", "PhaseInvertL", "PhaseInvertR", "ChannelMode"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "gluecompressor":
        for k in ("Threshold", "Ratio", "Attack", "Release", "Makeup", "DryWet", "Range", "PeakClipIn", "Oversample"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        # Sidechain summary (source string is crucial)
        sc_target = _get_param_attr(device_elem, ".//SideChain/RoutedInput/Routable/Target", "Value")
        if sc_target is not None:
            out["sidechain_target"] = sc_target
        sc_on = _get_param_attr(device_elem, ".//SideChain/OnOff", "Value")
        if sc_on is not None:
            out["sidechain_on"] = sc_on
        return out or None

    if tag == "drumbuss":
        for k in ("EnableCompression", "DriveAmount", "DriveType", "CrunchAmount", "DampingFrequency",
                  "TransientShaping", "BoomFrequency", "BoomAmount", "BoomDecay", "InputTrim", "OutputGain", "DryWet"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "autopan2":
        for k in ("Mode", "Modulation_Amount", "Modulation_Waveform", "Modulation_Frequency", "Modulation_Time",
                  "Modulation_SyncedRate", "Modulation_Sixteenth", "Modulation_Phase", "Modulation_PhaseOffset",
                  "Modulation_StereoMode", "Modulation_Spin", "AttackTime", "VintageMode", "HarmonicMode"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "delay":
        for k in ("DelayLine_Link", "DelayLine_PingPong", "DelayLine_SyncL", "DelayLine_SyncR", "DelayLine_TimeL", "DelayLine_TimeR",
                  "DelayLine_SyncedSixteenthL", "DelayLine_SyncedSixteenthR", "Feedback", "Freeze",
                  "Filter_On", "Filter_Frequency", "Filter_Bandwidth", "Modulation_Frequency", "Modulation_AmountTime", "Modulation_AmountFilter",
                  "DryWet", "EcoProcessing"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "echo":
        for k in ("Delay_TimeLink", "Delay_SyncL", "Delay_TimeL", "Delay_SyncR", "Delay_TimeR", "Feedback",
                  "ChannelMode", "InputGain", "OutputGain", "Gate_On", "Gate_Threshold", "Gate_Release",
                  "Ducking_On", "Ducking_Threshold", "Ducking_Release",
                  "Filter_On", "Filter_HighPassFrequency", "Filter_LowPassFrequency",
                  "Modulation_Waveform", "Modulation_Frequency", "Modulation_AmountDelay", "Modulation_AmountFilter",
                  "Reverb_Level", "Reverb_Decay", "StereoWidth", "DryWet"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "saturator":
        for k in ("PreDrive", "Type", "ColorOn", "BaseDrive", "ColorFrequency", "ColorWidth", "ColorDepth",
                  "PostClip", "PostDrive", "DryWet", "Oversampling"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "vocoder":
        for k in ("LowFrequency", "HighFrequency", "FormantShift", "FilterBandWidth", "Retro", "LevelGate",
                  "OutputGain", "EnvelopeRate", "EnvelopeRelease", "CarrierSource", "CarrierFlatten", "MonoStereo",
                  "DryWet", "ModulatorAmount"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    # ---------- RACKS / INSTRUMENTS ----------
    if tag in ("instrumentgroupdevice", "drumgroupdevice"):
        struct = _extract_group_device_structure(device_elem)
        if struct:
            out.update(struct)
        return out or None

    if tag == "drumcell":
        # Capture key sample + voice shaping
        sample_path = _get_param_attr(device_elem, ".//UserSample/Value/SampleRef/FileRef/RelativePath", "Value") \
            or _get_param_attr(device_elem, ".//UserSample/Value/SampleRef/FileRef/Path", "Value")
        if sample_path:
            out["sample"] = sample_path
        for k in ("Voice_Gain", "Voice_Transpose", "Voice_Detune", "Voice_Filter_On", "Voice_Filter_Frequency",
                  "Voice_Filter_Resonance", "Voice_Envelope_Attack", "Voice_Envelope_Decay", "Voice_Envelope_Release",
                  "Volume", "Pan"):
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    if tag == "instrumentvector":
        # Wavetable-esque instrument: capture key oscillator + filter + amp env
        keys = [
            "Voice_Oscillator1_On","Voice_Oscillator1_Pitch_Transpose","Voice_Oscillator1_Pitch_Detune",
            "Voice_Oscillator1_Wavetables_WavePosition","Voice_Oscillator1_Gain",
            "Voice_Oscillator2_On","Voice_Oscillator2_Pitch_Transpose","Voice_Oscillator2_Pitch_Detune",
            "Voice_Oscillator2_Wavetables_WavePosition","Voice_Oscillator2_Gain",
            "Voice_Filter1_On","Voice_Filter1_Type","Voice_Filter1_Slope","Voice_Filter1_Frequency","Voice_Filter1_Resonance","Voice_Filter1_Drive",
            "Voice_Filter2_On","Voice_Filter2_Type","Voice_Filter2_Slope","Voice_Filter2_Frequency","Voice_Filter2_Resonance","Voice_Filter2_Drive",
            "Voice_Modulators_AmpEnvelope_Times_Attack","Voice_Modulators_AmpEnvelope_Times_Decay","Voice_Modulators_AmpEnvelope_Times_Release",
            "Voice_Modulators_AmpEnvelope_Sustain",
        ]
        for k in keys:
            v = _get_param(device_elem, k)
            if v is not None:
                out[k] = v
        return out or None

    # ---------- MAX FOR LIVE ----------
    if tag in ("mxdevicemidieffect", "mxdeviceaudioeffect"):
        params = _extract_mxd_params(device_elem, max_params=64)
        if params:
            out["params"] = params
        return out or None

    # Fallback: bounded key-term scan (keeps size in check)
    terms = ("threshold","ratio","attack","release","gain","ceiling","lookahead","freq","q","width","drive","drywet","mix","feedback","decay","time")
    d = extract_key_settings_from_tags(device_elem, terms, max_items=60, max_depth=12)
    return d or None


def detect_stock_noop(device_tag: str, named_params: Dict[str, Any]) -> Optional[bool]:
    tag = (device_tag or "").lower()

    if tag == "stereogain":  # Utility
        gain = parse_float(named_params.get("Gain") or named_params.get("Gain (dB)"))
        width = parse_float(named_params.get("Width") or named_params.get("Stereo Width"))
        bass_mono = parse_bool(named_params.get("BassMono") or named_params.get("Bass Mono"))
        inv_l = parse_bool(named_params.get("PhaseInvertL") or named_params.get("Invert Left"))
        inv_r = parse_bool(named_params.get("PhaseInvertR") or named_params.get("Invert Right"))

        if gain is None and width is None and bass_mono is None and inv_l is None and inv_r is None:
            return None

        gain_ok = gain is None or abs(gain - 0.0) < 1e-6
        width_ok = width is None or abs(width - 1.0) < 1e-6 or abs(width - 100.0) < 1e-6
        bass_ok = bass_mono is None or bass_mono is False
        inv_ok = (inv_l is None or inv_l is False) and (inv_r is None or inv_r is False)
        return bool(gain_ok and width_ok and bass_ok and inv_ok)

    if tag == "eq8":
        for key in named_params.keys():
            lk = key.lower()
            if "gain" in lk or "freq" in lk or "q" in lk or "band" in lk:
                return False
        return None

    return None

def extract_device_on_state(device_elem: ET.Element) -> Optional[bool]:
    """
    High-confidence device power state.
    - Only trust tags that likely represent the device power button.
    - Allow the value to be stored either as an attribute OR as a child <Manual Value="..."/>.
    - DO NOT use generic 'Enabled' (too ambiguous in Ableton XML).
    """

    trusted_tags = ("DeviceOn", "IsOn", "On")

    def bool_from_node(n: ET.Element) -> Optional[bool]:
        # Common cases: attribute Value/Manual
        v = n.get("Value") or n.get("Manual")
        b = parse_bool(v)
        if b is not None:
            return b

        # Live sometimes nests: <DeviceOn><Manual Value="true"/></DeviceOn>
        for ch in list(n)[:20]:
            if ch.tag == "Manual":
                b2 = parse_bool(ch.get("Value"))
                if b2 is not None:
                    return b2
            # occasionally nested further
            for gch in list(ch)[:10]:
                if gch.tag == "Manual":
                    b3 = parse_bool(gch.get("Value"))
                    if b3 is not None:
                        return b3
        return None

    for node, depth in iter_with_depth(device_elem, max_depth=4):
        t = node.tag or ""
        for cname in trusted_tags:
            if t == cname or t.endswith(cname):
                b = bool_from_node(node)
                if b is not None:
                    return b

    # last resort: device element attributes (still no Enabled)
    for attr in ("IsOn", "On"):
        b = parse_bool(device_elem.get(attr))
        if b is not None:
            return b

    return None


def extract_device_lom_id(device_elem: ET.Element) -> Optional[str]:
    """
    Attempt to extract a stable device identifier used by Ableton's automation/envelope targeting.
    Many sets include a ParametersListWrapperLomId node inside each device.

    Returns the Value/Manual/text if found and non-boolish; else None.
    """
    for n in device_elem.iter():
        t = n.tag or ""
        if t == "ParametersListWrapperLomId" or t.endswith("ParametersListWrapperLomId"):
            v = n.get("Value") or n.get("Manual") or (n.text.strip() if n.text else None)
            v = normalize_text(v)
            if v and not is_boolish_text(v):
                return v
    return None



def collect_track_envelope_pointee_ids_with_events(track_elem: ET.Element) -> set:
    """
    Build a set of EnvelopeTarget PointeeId values for automation envelopes that contain
    actual event/value points. This is used to cheaply answer: "does this track automate
    parameter target X?"

    We do NOT extract curves; we only keep IDs (strings).
    """
    ids = set()

    def has_event_points(env: ET.Element) -> bool:
        for n in env.iter():
            t = (n.tag or "")
            if t.endswith("Event") or t in ("Events", "FloatEvent", "BoolEvent", "IntEvent"):
                return True
            if "Time" in n.attrib and parse_float(n.attrib.get("Time")) is not None:
                return True
            if "Value" in n.attrib and (parse_float(n.attrib.get("Value")) is not None or parse_bool(n.attrib.get("Value")) is not None):
                return True
        return False

    for env in track_elem.iter():
        tg = env.tag or ""
        if tg not in ("AutomationEnvelope", "ClipEnvelope") and "Envelope" not in tg:
            continue
        if not has_event_points(env):
            continue

        # Common: <EnvelopeTarget><PointeeId Value="145237"/></EnvelopeTarget>
        for pid in env.iter("PointeeId"):
            v = pid.get("Value")
            if v and v.isdigit():
                ids.add(v)
                break

    return ids


def device_on_automation_target_ids(device_elem: ET.Element) -> List[str]:
    """
    Extract AutomationTarget Ids associated with the device's On/Off parameter.

    In your .als, devices commonly store:
      <On> ... <AutomationTarget Id="145237"/> </On>

    We collect Ids from AutomationTarget nodes that are contained within an On-ish
    container node (tag name On/DeviceOn/IsOn/Enabled).
    """
    on_container_rx = re.compile(r"^(On|DeviceOn|IsOn|Enabled)$", re.IGNORECASE)
    ids: List[str] = []
    seen = set()

    # Build parent map within this device subtree to check ancestry.
    parent = {}
    for p in device_elem.iter():
        for ch in list(p):
            parent[ch] = p

    for at in device_elem.iter("AutomationTarget"):
        tid = at.get("Id")
        if not tid or not tid.isdigit():
            continue

        # Walk up a few levels to see if this AutomationTarget belongs to an On-ish node.
        cur = at
        ok = False
        for _ in range(4):
            cur = parent.get(cur)
            if cur is None:
                break
            if on_container_rx.match(cur.tag or ""):
                ok = True
                break
        if not ok:
            continue

        if tid not in seen:
            seen.add(tid)
            ids.append(tid)

    return ids


def detect_device_on_automation(device_elem: ET.Element, track_envelope_targets: Optional[set]) -> bool:
    """
    Deterministic, low-cost indicator for "this device's On/Off is automated somewhere".

    Strategy (grounded in your .als):
      1) Collect device On/Off AutomationTarget Id(s) from the device subtree.
      2) If the track has an AutomationEnvelope that targets the same PointeeId and contains
         event/value points, mark True.

    If we can't confidently link a device On target to a real envelope with events, return False.
    """
    if not track_envelope_targets:
        return False

    target_ids = device_on_automation_target_ids(device_elem)
    if not target_ids:
        return False

    for tid in target_ids:
        if tid in track_envelope_targets:
            return True

    return False


    def subtree_has_event_points(ctx: ET.Element, limit: int = 1500) -> bool:
        seen = 0
        for n in ctx.iter():
            seen += 1
            if seen > limit:
                break
            if event_rx.search(n.tag or ""):
                return True
            if "Time" in n.attrib and ("Value" in n.attrib or "Manual" in n.attrib or "Amount" in n.attrib):
                return True
            if "Value" in n.attrib and (parse_float(n.attrib.get("Value")) is not None or parse_bool(n.attrib.get("Value")) is not None):
                return True
        return False

    # (A) Device-subtree scan (rare, but cheap)
    for node, depth in iter_with_depth(device_elem, max_depth=8):
        if not node_mentions(env_rx, node):
            continue
        has_on_ref = any(node_mentions(on_rx, n2) for n2, _ in iter_with_depth(node, max_depth=10))
        if not has_on_ref:
            continue
        if subtree_has_event_points(node):
            return True

    # (B) Track-level scan (common)
    if track_elem is None:
        return False

    lom = extract_device_lom_id(device_elem)
    if not lom:
        # Without a device id anchor, avoid broad track-level scanning (too many false positives).
        return False

    needle = lom

    for node, depth in iter_with_depth(track_elem, max_depth=12):
        if not node_mentions(env_rx, node):
            continue
        # must have points
        if not subtree_has_event_points(node):
            continue

        # must mention On-ish target somewhere
        has_on_ref = any(node_mentions(on_rx, n2) for n2, _ in iter_with_depth(node, max_depth=12))
        if not has_on_ref:
            continue

        # and must reference this device lom id somewhere in attrs/text/tag (anchor)
        found_anchor = False
        seen = 0
        for n in node.iter():
            seen += 1
            if seen > 2000:
                break
            for k, v in (n.attrib or {}).items():
                if isinstance(v, str) and needle in v:
                    found_anchor = True
                    break
            if found_anchor:
                break
            if n.text and needle in n.text:
                found_anchor = True
                break
            if needle in (n.tag or ""):
                found_anchor = True
                break

        if found_anchor:
            return True

    return False


def extract_devices(track_elem: ET.Element, max_params_per_device: int, mix_settings: bool) -> List[Dict[str, Any]]:
    devices: List[Dict[str, Any]] = []

    # Prefer Track -> DeviceChain -> Devices
    devices_container = None
    for e in track_elem.iter():
        if e.tag == "Devices":
            devices_container = e
            break

    if devices_container is None:
        candidates = []
        for e in track_elem.iter():
            if e.tag.endswith("Device") or "Plugin" in e.tag:
                candidates.append(e)
        candidates = candidates[:128]
    else:
        candidates = list(devices_container)

    track_env_targets = collect_track_envelope_pointee_ids_with_events(track_elem)

    for dev in candidates:
        if dev.tag in ("DeviceChain", "Devices"):
            continue

        vendor, product, identifier = extract_plugin_identity(dev)
        fmt = classify_plugin_format(dev)

        # For 3rd-party plugins: capture opaque processor state metadata + readable hints (bounded).
        pstate_bytes: Optional[bytes] = None
        pstate_sha: Optional[str] = None
        pstate_len: Optional[int] = None
        pstate_hints: Optional[List[str]] = None
        plugin_decoded: Optional[Dict[str, Any]] = None
        plugin_meta: Optional[Dict[str, Any]] = None
        if fmt == "Plugin":
            pstate_bytes = extract_plugin_state_bytes(dev)
            if pstate_bytes:
                pstate_len = len(pstate_bytes)
                pstate_sha = sha256_bytes(pstate_bytes)[:16]
                # v23: keep plugin state analysis compact (avoid hint dumps)
                role = plugin_role_from_identifier(identifier)
                hint_tags = plugin_hint_tags_from_bytes(pstate_bytes)
                plugin_meta = {
                    "role": role,
                    "hint_tags": hint_tags or None,
                }
                # Only keep deep decode when it is actually useful and bounded
                if mix_settings and identifier:
                    low = identifier.lower()
                    if ("infiltrator" in low) or ("xferjson" in low) or ("serum" in low):
                        plugin_decoded = decode_plugin_state_best_effort(identifier, pstate_bytes)

        # Name: prefer display name, then product, then tag (never accept bool-ish) prefer display name, then product, then tag (never accept bool-ish)
        dname = extract_device_display_name(dev) or product or dev.tag
        dname = normalize_non_boolish(dname) or dev.tag

        enabled = extract_device_on_state(dev)
        has_on_automation = detect_device_on_automation(dev, track_env_targets)

        named_params = extract_named_param_pairs(dev, limit=200)

        full_params: Optional[Dict[str, Any]] = None
        if max_params_per_device > 0:
            raw_map: Dict[str, Any] = {}
            captured = 0
            for p in dev.iter():
                if captured >= max_params_per_device:
                    break
                if "Parameter" not in p.tag and not p.tag.endswith("Param"):
                    continue

                pid = p.get("Id") or p.get("ParameterId")
                val = p.get("Value") or p.get("Manual") or p.get("Amount")
                pname = p.get("Name")

                item = {
                    "id": normalize_text(pid),
                    "name": normalize_text(pname),
                    "value_raw": normalize_text(val),
                    "tag": p.tag,
                }

                if item["name"]:
                    key = f"name:{item['name']}"
                elif item["id"]:
                    key = f"id:{item['id']}"
                else:
                    key = f"param:{captured}"

                raw_map[key] = item
                captured += 1

            for nk, nv in list(named_params.items())[:50]:
                k = f"named:{nk}"
                if k not in raw_map:
                    raw_map[k] = {"id": None, "name": nk, "value_raw": str(nv), "tag": "NamedParam"}

            pruned = prune_param_map(raw_map)
            full_params = pruned if pruned else None

        full_params_out = None if (fmt == "Plugin") else full_params

        settings_out: Optional[Dict[str, Any]] = None
        if mix_settings and fmt != "Plugin":
            settings_out = extract_device_key_settings(dev, dev.tag)

        devices.append({
            "tag": dev.tag,
            "name": dname,
            "plugin_vendor": vendor,
            "plugin_product": product,
            "plugin_format": fmt,
            "plugin_identifier": identifier,
            "enabled": enabled,
            "plugin_state_len": pstate_len,
            "plugin_state_sha": pstate_sha,
            "plugin_meta": plugin_meta,
            "plugin_decoded": plugin_decoded,
            "has_on_automation": bool(has_on_automation),
            "named_params": named_params if named_params else None,
            "params": full_params_out,
            "settings": settings_out,
        })

    return devices


def extract_tracks(root: ET.Element, max_params_per_device: int, mix_settings: bool) -> List[Dict[str, Any]]:
    tracks: List[Dict[str, Any]] = []
    track_tags = ["AudioTrack", "MidiTrack", "ReturnTrack", "MasterTrack", "GroupTrack"]

    for tag in track_tags:
        for t in root.iter(tag):
            track_id = t.get("Id") or t.get("TrackId")
            name = extract_track_name(t)
            routing = extract_track_routing(t)
            flags = extract_track_flags(t)
            mixer = extract_track_mixer(t)
            parent_group_id = extract_parent_group_id(t)
            devices = extract_devices(t, max_params_per_device=max_params_per_device, mix_settings=mix_settings)

            tr = {
                "track_type": tag,
                "track_id": track_id,
                "name": name,
                "flags": flags,
                "routing": routing,
                "mixer": mixer,
                "parent_group_id": parent_group_id,
                "devices": devices,
            }
            tr["final_qc"] = compute_final_qc_flags(tr)
            tracks.append(tr)

    return tracks


# -----------------------------
# Deactivated routing impact checks (second pass)
# -----------------------------

_TRACK_REF_RX = re.compile(r"(?:Track|GroupTrack)\.(\d+)")
_AOUT_TRACK_RX = re.compile(r"AudioOut/Track\.(\d+)", re.IGNORECASE)
_AIN_TRACK_RX = re.compile(r"AudioIn/Track\.(\d+)", re.IGNORECASE)


def _extract_any_track_id_from_routing(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = _TRACK_REF_RX.search(s)
    return m.group(1) if m else None


def apply_deactivated_routing_impact_checks(tracks: List[Dict[str, Any]]) -> None:
    """
    REQUIRED feature:
      - Detect when a deactivated track breaks routing/bus chains.

    Enhancements in this version:
      - Adds routing_break_depth (shortest hop count from a deactivated source)
      - Adds routing_break_sources (which deactivated tracks cause the break)
      - Adds dead bus / orphan bus detection (bus tracks with zero ACTIVE upstream sources)

    What we flag:
      A) Any track whose audio_in references Track.X where Track.X is deactivated.
         (Common for bus tracks and FX receive chains.)
      B) Any deactivated track that routes audio_out to:
           - a concrete Track.Y (AudioOut/Track.Y/TrackIn)
           - its parent group (AudioOut/GroupTrack + parent_group_id present)
         We mark the destination as impacted, because it's receiving from a deactivated child.
      C) Dead bus / orphan bus:
           - dead bus: track has upstream sources, but ALL are deactivated
           - orphan bus: bus-like track has no upstream sources at all (heuristic)

    Implementation notes:
      - Derived only from extracted routing strings + flags (low risk to existing extraction).
      - Annotates tracks in-place:
          track["routing_break"] = bool
          track["routing_impact"] = [human-readable strings]  (FULL only)
          track["routing_break_depth"] = int (FULL, shortest hop count)
          track["routing_break_sources"] = [ {id,name,type} ... ] (FULL)
          track["routing_dead_bus"] / ["routing_orphan_bus"] = bool (FULL)
        and then recomputes final_qc for all tracks.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for t in tracks:
        tid = t.get("track_id")
        if tid is not None:
            by_id[str(tid)] = t

    def tlabel(tid: str) -> str:
        t = by_id.get(tid)
        if not t:
            return f"Track.{tid}"
        nm = t.get("name") or f"Track.{tid}"
        tt = t.get("track_type") or "Track"
        return f"{nm} ({tt} {tid})"

    def tsimple(tid: str) -> Dict[str, Any]:
        t = by_id.get(tid, {})
        return {"id": str(tid), "name": t.get("name"), "type": t.get("track_type")}

    # Clear old annotations (in case caller reruns)
    for t in tracks:
        t.pop("routing_break", None)
        t.pop("routing_impact", None)
        t.pop("routing_break_depth", None)
        t.pop("routing_break_sources", None)
        t.pop("routing_break_path", None)
        t.pop("routing_break_path_ids", None)
        t.pop("routing_dead_bus", None)
        t.pop("routing_orphan_bus", None)

    # --- Build routing edges ---
    # Edges represent "audio flows from src -> dst"
    edges: Dict[str, List[str]] = {}
    incoming: Dict[str, List[str]] = {}

    def add_edge(src: str, dst: str) -> None:
        if not src or not dst or src == dst:
            return
        if src not in by_id or dst not in by_id:
            return
        edges.setdefault(src, []).append(dst)
        incoming.setdefault(dst, []).append(src)

    # A) downstream from AudioIn references (dst receives from src)
    consumers_of: Dict[str, List[str]] = {}
    for t in tracks:
        tid = str(t.get("track_id")) if t.get("track_id") is not None else None
        if not tid:
            continue
        ai = (t.get("routing") or {}).get("audio_in")
        src = _extract_any_track_id_from_routing(ai)
        if src and src in by_id:
            consumers_of.setdefault(src, []).append(tid)
            add_edge(src, tid)

    # B) downstream from AudioOut track refs (src routes to dst)
    for t in tracks:
        tid = str(t.get("track_id")) if t.get("track_id") is not None else None
        if not tid:
            continue
        ao = (t.get("routing") or {}).get("audio_out")
        dst = _extract_any_track_id_from_routing(ao)
        if dst:
            add_edge(tid, dst)

        # GroupTrack audio out usually means "to parent group"
        if dst is None and isinstance(ao, str) and "AudioOut/GroupTrack" in ao:
            pg = t.get("parent_group_id")
            if pg and str(pg) != "-1":
                # Resolve missing GroupTrack ID using parent_group_id (important for routing analysis)
                (t.setdefault("routing", {}) )["audio_out_resolved_group_id"] = str(pg)
                add_edge(tid, str(pg))

    # --- (A) Tracks that RECEIVE from a deactivated upstream (direct) ---
    for src_id, consumer_ids in consumers_of.items():
        src = by_id.get(src_id)
        if not src:
            continue
        if (src.get("flags") or {}).get("deactivated") is not True:
            continue
        for cid in consumer_ids:
            c = by_id.get(cid)
            if not c:
                continue
            msgs = c.setdefault("routing_impact", [])
            msgs.append(f"audio_in from deactivated upstream: {tlabel(src_id)}")
            c["routing_break"] = True

    # --- (B) Deactivated tracks that SEND into a track or their parent group ---
    for t in tracks:
        tid = str(t.get("track_id")) if t.get("track_id") is not None else None
        if not tid:
            continue
        if (t.get("flags") or {}).get("deactivated") is not True:
            continue

        routing = t.get("routing") or {}
        ao = routing.get("audio_out")
        dest_id: Optional[str] = None

        # Concrete routing to another track
        dest_id = _extract_any_track_id_from_routing(ao)

        # If it routes to GroupTrack without explicit id, treat parent group as destination
        if dest_id is None and isinstance(ao, str) and "AudioOut/GroupTrack" in ao:
            pg = t.get("parent_group_id")
            if pg and str(pg) != "-1":
                dest_id = str(pg)

        if dest_id and dest_id in by_id and dest_id != tid:
            dest = by_id[dest_id]
            msgs = dest.setdefault("routing_impact", [])
            msgs.append(f"receives from deactivated child: {tlabel(tid)}")
            dest["routing_break"] = True

        # Also annotate the deactivated track itself if it's feeding anything.
        if consumers_of.get(tid) or dest_id:
            msgs = t.setdefault("routing_impact", [])
            if consumers_of.get(tid):
                for cid in consumers_of[tid]:
                    msgs.append(f"deactivated track feeds downstream consumer: {tlabel(cid)}")
            if dest_id:
                msgs.append(f"deactivated track routes audio_out into: {tlabel(dest_id)}")
            t["routing_break"] = True

    # --- (C) Dead bus / orphan bus detection ---
    # dead bus: has upstream sources, but ALL are deactivated
    # orphan bus: heuristic bus-like track with zero upstream sources at all
    bus_name_rx = re.compile(r"(bus|return|fx|send|recv|receive)", re.IGNORECASE)
    for tid, t in by_id.items():
        inc = incoming.get(tid, [])
        if inc:
            total = len(set(inc))
            active = 0
            for sid in set(inc):
                s = by_id.get(sid)
                if not s:
                    continue
                if (s.get("flags") or {}).get("deactivated") is not True:
                    active += 1
            if total > 0 and active == 0:
                t["routing_dead_bus"] = True
                msgs = t.setdefault("routing_impact", [])
                msgs.append(f"dead bus: upstream sources exist ({total}) but all are deactivated")
                t["routing_break"] = True
        else:
            # Orphan heuristic: looks like a bus/return but nobody feeds it
            nm = (t.get("name") or "")
            if bus_name_rx.search(nm) and (t.get("track_type") in ("AudioTrack", "GroupTrack")):
                # Only consider if its audio_in isn't explicitly external/track-ref
                ai = (t.get("routing") or {}).get("audio_in")
                aik = parse_routing_kind(ai)
                if aik in ("n", "m", "u"):
                    t["routing_orphan_bus"] = True
                    msgs = t.setdefault("routing_impact", [])
                    msgs.append("orphan bus: bus-like track has no upstream sources")
                    t["routing_break"] = True

    # --- Multi-source BFS from deactivated tracks to compute depth + sources ---
    deact_sources: List[str] = [
        tid for tid, t in by_id.items() if (t.get("flags") or {}).get("deactivated") is True
    ]

    # For each node: best_depth, and list of deactivated sources that achieve that best_depth
    best_depth: Dict[str, int] = {}
    best_sources: Dict[str, List[str]] = {}

    from collections import deque
    # We keep a single exemplar predecessor chain for each node at its best depth.
    # This lets us reconstruct one shortest path for reporting (routing_break_path).
    best_pred: Dict[str, Optional[str]] = {}
    best_pred_src: Dict[str, Optional[str]] = {}

    q = deque()  # (node, depth, src, prev)
    for sid in deact_sources:
        q.append((sid, 0, sid, None))

    while q:
        node, depth, src, prev = q.popleft()

        # record best depth/sources for this node
        if node not in best_depth or depth < best_depth[node]:
            best_depth[node] = depth
            best_sources[node] = [src]
            best_pred[node] = prev
            best_pred_src[node] = src
        elif depth == best_depth[node]:
            if src not in best_sources[node]:
                best_sources[node].append(src)
            # Keep existing predecessor as exemplar; do not overwrite to avoid churn.
        else:
            # worse depth, skip expansion
            continue

        # expand
        for nxt in edges.get(node, []):
            nd = depth + 1
            # prune if we already have a better depth for nxt
            if nxt in best_depth and nd > best_depth[nxt]:
                continue
            q.append((nxt, nd, src, node))
# Attach depth/sources to tracks with routing breaks or deactivated sources
    for tid, t in by_id.items():
        if tid not in best_depth:
            continue

        # Always attach to deactivated sources (depth 0) for traceability
        # Attach to others only if routing_break was triggered
        if best_depth[tid] == 0 or t.get("routing_break") is True:
            t["routing_break_depth"] = int(best_depth[tid])
            srcs = best_sources.get(tid, [])
            # Make sources stable + minimal
            srcs2 = sorted(set(srcs), key=lambda x: int(x) if x.isdigit() else x)
            t["routing_break_sources"] = [tsimple(s) for s in srcs2]

            # Exemplar shortest path from a deactivated source to this track (for actionable debugging)
            if tid in best_pred_src and best_pred_src.get(tid) is not None:
                path_ids: List[str] = []
                cur: Optional[str] = tid
                guard = 0
                while cur is not None and guard < 50:
                    path_ids.append(cur)
                    cur = best_pred.get(cur)
                    guard += 1
                path_ids.reverse()
                # Limit to avoid bloat in pathological graphs
                if len(path_ids) > 25:
                    path_ids = path_ids[:5] + ["..."] + path_ids[-5:]
                t["routing_break_path"] = [tsimple(pid) if pid != "..." else {"id": "...", "name": "...", "type": "..."} for pid in path_ids]
                t["routing_break_path_ids"] = path_ids

            # If this track is not deactivated but reachable from deactivated sources,
            # ensure routing_break is True (graph-derived)
            if (t.get("flags") or {}).get("deactivated") is not True and best_depth[tid] >= 1:
                t["routing_break"] = True
                msgs = t.setdefault("routing_impact", [])
                # avoid spamming; one line that references the closest sources
                if srcs2:
                    msgs.append(
                        f"reachable from deactivated source(s) at depth {best_depth[tid]}: "
                        + ", ".join([tlabel(s) for s in srcs2[:5]])
                        + ("..." if len(srcs2) > 5 else "")
                    )

    # Recompute final_qc now that routing annotations are known
    for t in tracks:
        t["final_qc"] = compute_final_qc_flags(t)

    return tracks


# -----------------------------
# Compact helpers (token-minimized)
# -----------------------------

def parse_routing_kind(s: Optional[str]) -> str:
    if not s:
        return "m"  # missing
    if s.endswith("/None") or "AudioIn/None" in s or "AudioOut/None" in s:
        return "n"  # none
    if "AudioOut/Master" in s or s.endswith("/Master"):
        return "M"  # master
    if "AudioOut/GroupTrack" in s or "GroupTrack" in s:
        return "G"  # group-ish
    if "AudioIn/Track." in s or "AudioOut/Track." in s:
        return "T"  # track ref
    if "Ext" in s or "External" in s:
        return "E"  # external
    return "u"      # unknown


def extract_track_ref_id(routing_str: Optional[str]) -> Optional[str]:
    if not routing_str:
        return None
    m = re.search(r"Track\.(\d+)", routing_str)
    if m:
        return m.group(1)
    m = re.search(r"GroupTrack\.(\d+)", routing_str)
    if m:
        return m.group(1)
    return None


def compact_device(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    SUPER small per-device dict for Claude.
    Keep only: tag, name, enabled, plugin_format, identifier, noop_guess, state hash.
    """
    named = d.get("named_params") or {}
    params = d.get("params") or {}

    fp_payload = json.dumps({
        "id": d.get("plugin_identifier"),
        "name": d.get("name"),
        "fmt": d.get("plugin_format"),
        "named": named,
        "params": params,
    }, sort_keys=True)

    sh = (d.get("plugin_state_sha") or sha256_str(fp_payload))[:12]
    noop = detect_stock_noop(d.get("tag", ""), named)

    return {
        "t": d.get("tag"),               # tag
        "n": d.get("name"),              # name
        "e": d.get("enabled"),           # enabled (True/False/None)
        "a": bool(d.get("has_on_automation") or False),  # on/off automated?
        "f": d.get("plugin_format"),     # format
        "i": d.get("plugin_identifier"), # identifier/path/uid
        "h": sh,                         # state hash short
        "z": noop,                       # noop guess
    }


def detect_compact_issues(tr: Dict[str, Any]) -> List[str]:
    """
    Issues list in compact (short strings).
    Important: "alldis" ONLY when all devices explicitly have e==False.
    """
    issues: List[str] = []
    if tr.get("mu") is True:
        issues.append("mut")
    if tr.get("so") is True:
        issues.append("sol")

    if tr.get("F") is True:
        issues.append("qc")

    # Routing broken/impacted by deactivated track (derived second-pass)
    R = tr.get("R")
    if isinstance(R, str) and "r" in R:
        issues.append("rbrk")

    ai = tr.get("ai")
    ao = tr.get("ao")

    if isinstance(ai, str) and (ai.endswith("/None") or "AudioIn/None" in ai):
        issues.append("ain0")
    if isinstance(ao, str) and (ao.endswith("/None") or "AudioOut/None" in ao):
        issues.append("aout0")

    mx = tr.get("mx") or {}
    if mx.get("vs") is True:
        issues.append("sil")

    if tr.get("dc", 0) == 0:
        issues.append("ndev")

    devs = tr.get("dv") or []
    if devs:
        enabled_states = [d.get("e") for d in devs]
        # Only call it "all disabled" if EVERY device is explicitly False.
        if all(s is False for s in enabled_states):
            issues.append("alldis")
        elif any(s is None for s in enabled_states):
            # Optional: mark presence of unknown device enable states
            issues.append("en?")

    # Group/bus FX candidate: AudioIn from Track.X and AudioOut to GroupTrack/Master
    aik = tr.get("aik")
    aok = tr.get("aok")
    if aik == "T" and aok in ("G", "M"):
        issues.append("fxrcv")

    return issues


def compact_track(t: Dict[str, Any]) -> Dict[str, Any]:
    flags = t.get("flags") or {}
    routing = t.get("routing") or {}
    mixer = t.get("mixer") or {}

    devs_full = t.get("devices") or []
    devs = [compact_device(d) for d in devs_full]

    ai = routing.get("audio_in")
    ao = routing.get("audio_out")
    aik = parse_routing_kind(ai)
    aok = parse_routing_kind(ao)

    air = extract_track_ref_id(ai)
    aor = extract_track_ref_id(ao)

    mx = {
        "v": mixer.get("volume"),
        "p": mixer.get("pan"),
        "vs": mixer.get("volume_silent_guess"),
    }

    tr = {
        "tt": t.get("track_type"),
        "id": str(t.get("track_id")) if t.get("track_id") is not None else None,
        "n": t.get("name"),
        "pg": t.get("parent_group_id"),
        "mu": flags.get("muted"),
        "so": flags.get("solo"),
        "ar": flags.get("arm"),
        "F": (t.get("final_qc") or {}).get("fail"),
        "R": ("".join((t.get("final_qc") or {}).get("reasons") or [])) or None,
        "W": ("".join((t.get("final_qc") or {}).get("warnings") or [])) or None,
        "ai": ai,
        "ao": ao,
        "aik": aik,
        "aok": aok,
        "air": air,
        "aor": aor,
        "aog": (routing.get("audio_out_resolved_group_id") if aok == "G" else None),
        "mx": mx,
        "dc": len(devs),
        "dv": devs,
    }

    # Optional compact routing-break trace (kept tiny)
    if t.get("routing_break") is True and t.get("routing_break_depth") is not None:
        srcs = t.get("routing_break_sources") or []
        src_ids = []
        for s in srcs:
            sid = s.get("id") if isinstance(s, dict) else None
            if sid:
                src_ids.append(str(sid))
        src_ids = sorted(set(src_ids), key=lambda x: int(x) if x.isdigit() else x)
        tr["rb"] = {"d": int(t.get("routing_break_depth")), "s": src_ids or None}

    tr["is"] = detect_compact_issues(tr)

    # Extra issue labels (do not affect packed QC reasons)
    if t.get("routing_dead_bus") is True:
        tr["is"].append("dbus")
    if t.get("routing_orphan_bus") is True:
        tr["is"].append("obus")

    return tr


# -----------------------------
# Build FULL + COMPACT
# -----------------------------


# ---------------------------
# FULL output size controls
# ---------------------------

def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def _stable_hash12(obj: Any) -> str:
    h = hashlib.sha1(_canonical_json(obj).encode("utf-8")).hexdigest()
    return h[:12]

def _strip_none_keys(obj: Any) -> Any:
    """Recursively remove dict keys where value is None. Keeps empty lists/dicts."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None:
                continue
            out[k] = _strip_none_keys(v)
        return out
    if isinstance(obj, list):
        return [_strip_none_keys(v) for v in obj]
    return obj

def _dedupe_full_tracks(tracks: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pool repeated large sub-objects (settings, plugin_decoded) by stable hash.

    Returns (new_tracks, pools) where pools contains:
      - device_settings_pool: {hash12: settings_dict}
      - plugin_decoded_pool: {hash12: decoded_dict}
    """
    settings_pool: Dict[str, Any] = {}
    decoded_pool: Dict[str, Any] = {}

    # Deep-copy-ish via json roundtrip would be expensive; mutate in place on a shallow copy of track list.
    new_tracks = tracks

    for t in new_tracks:
        devs = t.get("devices") or []
        for d in devs:
            # Deduplicate device settings (stock devices)
            s = d.get("settings")
            if isinstance(s, dict) and s:
                key = _stable_hash12(s)
                if key not in settings_pool:
                    settings_pool[key] = s
                d["settings_ref"] = key
                d["settings"] = None  # removed; preserved in pool

            # Deduplicate decoded plugin metadata (only when present)
            pd = d.get("plugin_decoded")
            if isinstance(pd, dict) and pd:
                key = _stable_hash12(pd)
                if key not in decoded_pool:
                    decoded_pool[key] = pd
                d["plugin_decoded_ref"] = key
                d["plugin_decoded"] = None  # removed; preserved in pool

    pools = {
        "device_settings_pool": settings_pool,
        "plugin_decoded_pool": decoded_pool,
    }
    return new_tracks, pools


def build_full_report(in_path: str, root: ET.Element, tracks: List[Dict[str, Any]], *, dedupe_full: bool = True, strip_null_keys: bool = True) -> Dict[str, Any]:
    # Lightweight "final QA" summary block so you can sanity-check the project at a glance.
    fail_tracks = []
    warn_tracks = []
    deactivated_tracks = 0
    muted_tracks = 0
    silent_tracks = 0
    routing_break_tracks = 0
    dead_bus_tracks = 0
    orphan_bus_tracks = 0

    total_devices = 0
    devices_off = 0
    devices_off_no_auto = 0
    devices_on_auto = 0

    for t in tracks:
        tqc = (t.get("final_qc") or {})
        if tqc.get("fail") is True:
            fail_tracks.append(t)
        if (tqc.get("warnings") or []):
            warn_tracks.append(t)

        flags = (t.get("flags") or {})
        if flags.get("deactivated") is True:
            deactivated_tracks += 1
        if flags.get("muted") is True:
            muted_tracks += 1
        if t.get("routing_break") is True:
            routing_break_tracks += 1
        if t.get("routing_dead_bus") is True:
            dead_bus_tracks += 1
        if t.get("routing_orphan_bus") is True:
            orphan_bus_tracks += 1

        mx = (t.get("mixer") or {})
        if mx.get("volume_silent_guess") is True:
            silent_tracks += 1

        for d in (t.get("devices") or []):
            total_devices += 1
            en = d.get("enabled")
            if en is False:
                devices_off += 1
                if d.get("has_on_automation") is not True:
                    devices_off_no_auto += 1
            if d.get("has_on_automation") is True:
                devices_on_auto += 1

    # Include a few failing track names/ids for quick debugging without bloating the file.
    def track_brief(t: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": t.get("track_id"),
            "name": t.get("name"),
            "type": t.get("track_type"),
            "reasons": ((t.get("final_qc") or {}).get("reasons") or []),
        }

    qc_summary = {
        "fail_track_count": len(fail_tracks),
        "warn_track_count": len(warn_tracks),
        "fail_tracks_preview": [track_brief(t) for t in fail_tracks[:12]],
        "deactivated_track_count": deactivated_tracks,
        "muted_track_count": muted_tracks,
        "silent_track_count": silent_tracks,
        "routing_break_track_count": routing_break_tracks,
        "dead_bus_track_count": dead_bus_tracks,
        "orphan_bus_track_count": orphan_bus_tracks,
        "device_count": total_devices,
        "devices_off_count": devices_off,
        "devices_off_no_auto_count": devices_off_no_auto,
        "devices_on_automation_count": devices_on_auto,
    }


    out_tracks = tracks
    pools: Optional[Dict[str, Any]] = None
    if dedupe_full:
        out_tracks, pools = _dedupe_full_tracks(out_tracks)

    out_obj: Dict[str, Any] = {
        "version": SCRIPT_VERSION,
        "input_file": os.path.abspath(in_path),
        "root_tag": root.tag,
        "track_count": len(out_tracks),
        "qc_reason_legend": QC_REASON_LEGEND,
        "qc_warning_legend": QC_WARNING_LEGEND,
        "qc_summary": qc_summary,
        "tracks": out_tracks,
    }
    if pools:
        out_obj["pools"] = pools

    if strip_null_keys:
        out_obj = _strip_none_keys(out_obj)

    return out_obj



def build_compact(in_path: str, root: ET.Element, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
    ctracks = [compact_track(t) for t in tracks]
    total_devices = sum(t.get("dc", 0) for t in ctracks)

    # Keep legend (still small); remove it if you ever need even fewer tokens.
    legend = {
        "tt": "track_type",
        "id": "track_id",
        "n": "name",
        "pg": "parent_group_id",
        "mu": "muted",
        "so": "solo",
        "ar": "arm",
        "F": "final_qc fail (bool)",
        "R": "final_qc reasons packed (m d s x o r)",
        "W": "final_qc warnings packed (a)",
        "ai": "audio_in",
        "ao": "audio_out",
        "aik": "audio_in_kind (n none, m missing, T trackref, G group-ish, M master, E external, u unknown)",
        "aok": "audio_out_kind",
        "air": "audio_in_ref_id (Track.X or GroupTrack.X)",
        "aor": "audio_out_ref_id",
        "aog": "resolved audio_out group id when ao is AudioOut/GroupTrack",
        "mx": "mixer {v volume, p pan, vs volume_silent_guess}",
        "dc": "device_count",
        "dv": "devices",
        "dv.t": "device_tag",
        "dv.n": "device_name",
        "dv.e": "device_enabled (True/False/None)",
        "dv.a": "device_on_automation (On/Off automated?)",
        "dv.f": "plugin_format",
        "dv.i": "plugin_identifier",
        "dv.h": "state_hash",
        "dv.z": "noop_guess",
        "rb": "routing_break trace {d depth, s source_ids}",
        "is": "issues (mut, sol, qc, rbrk, dbus, obus, ain0, aout0, sil, ndev, alldis, en?, fxrcv)",
        "R_codes": QC_REASON_LEGEND,
        "W_codes": QC_WARNING_LEGEND,

    }

    return {
        "version": SCRIPT_VERSION,
        "input_file": os.path.abspath(in_path),
        "root_tag": root.tag,
        "track_count": len(ctracks),
        "total_devices": total_devices,
        "schema": "compact",
        "legend": legend,
        "tracks": ctracks,
    }


# -----------------------------
def print_problem_summary(full: Dict[str, Any], tracks: List[Dict[str, Any]]) -> None:
    """Print a human-readable QA summary to stdout."""
    qc = (full.get("qc_summary") or {})
    fail_n = qc.get("fail_track_count", 0)
    warn_n = qc.get("warn_track_count", 0)
    deact_n = qc.get("deactivated_track_count", 0)
    rbrk_n = qc.get("routing_break_track_count", 0)
    dbus_n = qc.get("dead_bus_track_count", 0)
    obus_n = qc.get("orphan_bus_track_count", 0)
    dev_off = qc.get("devices_off_count", 0)
    dev_off_no_auto = qc.get("devices_off_no_auto_count", 0)
    dev_on_auto = qc.get("devices_on_automation_count", 0)

    print("")
    print("=== Ableton QA Summary ===")
    print(f"FAIL tracks: {fail_n} | WARN tracks: {warn_n}")
    print(f"Deactivated tracks: {deact_n}")
    print(f"Routing breaks: {rbrk_n} (dead bus: {dbus_n}, orphan bus: {obus_n})")
    print(f"Devices OFF: {dev_off} (no-auto: {dev_off_no_auto}, with On/Off auto: {dev_on_auto})")

    # Code legend
    print("")
    print("Reason codes (R): " + ", ".join([f"{k}={v}" for k, v in QC_REASON_LEGEND.items()]))
    print("Warning codes (W): " + ", ".join([f"{k}={v}" for k, v in QC_WARNING_LEGEND.items()]))

    # Print a concise list of failing tracks
    fail_preview = qc.get("fail_tracks_preview") or []
    if fail_preview:
        print("")
        print("Failing tracks (preview):")
        for it in fail_preview[:25]:
            nm = it.get("name") or ""
            tt = it.get("type") or ""
            reasons = "".join(it.get("reasons") or [])
            print(f"  - {tt:<9} | {reasons:<8} | {nm}")

    # Print routing break details (limited)
    if rbrk_n:
        print("")
        print("Routing impact (top):")
        shown = 0
        for t in tracks:
            if t.get("routing_break") is not True:
                continue
            if shown >= 25:
                print("  ...")
                break
            nm = t.get("name") or ""
            depth = t.get("routing_break_depth")
            srcs = t.get("routing_break_sources") or []
            src_names = [s.get("name") for s in srcs if isinstance(s, dict) and s.get("name")] 
            src_names = [str(x) for x in src_names][:5]
            extra = ""
            if t.get("routing_dead_bus") is True:
                extra = " [DEAD BUS]"
            elif t.get("routing_orphan_bus") is True:
                extra = " [ORPHAN BUS]"
            if depth is not None and src_names:
                print(f"  - depth={depth} src={','.join(src_names)}{extra} | {nm}")
            else:
                print(f"  - {extra.strip()} | {nm}" if extra else f"  - {nm}")
            shown += 1

# Main
# -----------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Ableton Live .als/.xml extractor: FULL + COMPACT outputs.")
    ap.add_argument("input", help="Path to .als or extracted .xml")
    ap.add_argument("--out-dir", default=None, help="Output directory (default: alongside input)")
    ap.add_argument("--base-name", default=None, help="Base name for outputs (default: input filename)")
    ap.add_argument(
        "--max-params-per-device",
        type=int,
        default=120,
        help="FULL: cap parameter-ish nodes captured per device (before pruning). Default 120.",
    )
    ap.add_argument("--mix-settings", action="store_true", help="FULL: include small key device settings for mix/loudness analysis (stock devices only).")
    ap.add_argument("--no-full-dedupe", action="store_true", help="FULL: disable pooling repeated settings/decoded blocks (larger but more self-contained).")
    ap.add_argument("--keep-null-keys", action="store_true", help="FULL: keep keys with null values (larger but explicit).")
    ap.add_argument("--minify", action="store_true", help="Minify JSON outputs (single-line, no spaces).")
    args = ap.parse_args()

    in_path = args.input
    if not os.path.exists(in_path):
        print(f"ERROR: input not found: {in_path}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(in_path))
    os.makedirs(out_dir, exist_ok=True)

    base = args.base_name or os.path.splitext(os.path.basename(in_path))[0]

    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    full_path = os.path.join(out_dir, f"{base}.{ts}.full.json")
    compact_path = os.path.join(out_dir, f"{base}.{ts}.compact.json")

    xml_bytes = read_xml_bytes(in_path)
    root = find_liveset_root(xml_bytes)

    tracks = extract_tracks(root, max_params_per_device=args.max_params_per_device, mix_settings=args.mix_settings)

    # Second pass: routing/bus impact checks for deactivated tracks
    apply_deactivated_routing_impact_checks(tracks)

    full = build_full_report(in_path, root, tracks, dedupe_full=(not args.no_full_dedupe), strip_null_keys=(not args.keep_null_keys))
    compact = build_compact(in_path, root, tracks)

    indent = None if args.minify else 2
    dump_kwargs = {"ensure_ascii": False}
    if indent is None:
        dump_kwargs["separators"] = (",", ":")
    else:
        dump_kwargs["indent"] = indent

    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(full, f, **dump_kwargs)
    with open(compact_path, "w", encoding="utf-8") as f:
        json.dump(compact, f, **dump_kwargs)

    print(f"Ableton Dual Extract v{SCRIPT_VERSION}")
    print(f"Wrote FULL:    {full_path}")
    print(f"Wrote COMPACT: {compact_path}")
    print_problem_summary(full, tracks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
