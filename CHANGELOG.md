# Changelog

## [1.0.27] - 2026-02-05
### Refactored
- Extracted magic numbers to named constants (MAX_CANDIDATE_ITEMS, MAX_DISPLAYED_FAILURES, etc.) for better maintainability
- Consolidated plugin role classification logic into single canonical function
- Created device extraction registry with 13 individual extractor functions for cleaner architecture
- Refactored long functions with helper functions (_build_routing_graph, _detect_dead_and_orphan_buses, _find_deactivated_paths)

### Improved
- Pre-compiled regex patterns moved to module level for better performance (BUS_NAME_PATTERN, FLAG_PATTERNS)
- Replaced 11 bare `except Exception` clauses with specific exception types (ValueError, TypeError, JSONDecodeError, etc.)
- Moved all imports to top level following PEP 8 standards
- Code readability and maintainability significantly improved

### Performance
- Regex compilation overhead eliminated by pre-compiling patterns at module level
- find_flag() now accepts pre-compiled patterns for better performance

---

## [1.0.26] - 2026-02-05
### Fixed
- Removed 71 lines of dead/unreachable code after return statement in detect_device_on_automation()
- Fixed BFS performance bug: changed iter_with_depth() to use deque with popleft() instead of list with pop(0) for O(1) operations
- Fixed type annotation mismatch: apply_deactivated_routing_impact_checks() now correctly declares return type as List[Dict[str, Any]]

### Improved
- Queue operations performance in device/track processing (O(n) → O(1))
- Code quality and type safety

---

## [1.0.25] - 2026-02-04
### Changed
- Output filenames now end with .json (full/compact) instead of *.audit.json
- Compact output filename no longer includes a version suffix
- Script prints its version on run and embeds version in generated reports

---

## [1.0.24] - 2026-02-04
### Added
- Device settings deduplication via pools to reduce FULL file size
- Plugin metadata compaction for opaque third-party plugins
- Stable FULL size optimization
- Improved third-party plugin role detection
- Removal of redundant plugin hint dumps

### Improved
- FULL/COMPACT output size stability
- Routing break and dead/orphan bus detection accuracy
- Stock device settings extraction (EQ8, Glue, Drum Buss, Utility, etc.)

### Fixed
- Excessive FULL file growth from v22
- Redundant null-key serialization
- Plugin state duplication

---

## [1.0.23] - 2026-02-04
### Added
- Plugin metadata trimming
- Selective deep decoding for structured plugins
- Removal of large plugin_state_hints

---

## [1.0.22] - 2026-02-03
### Added
- Third-party plugin hint extraction
- Embedded JSON/XML scanning
- Plugin role classification

### Known Issues
- FULL file size regression

---

## [1.0.21] - 2026-02-03
### Added
- Full stock device decoding (EQ8, Utility, Glue, Drum Buss, Delay, Echo)
- Instrument and rack structure parsing

---

## [1.0.20] - 2026-02-02
### Added
- Routing break depth and source tracing
- Dead/orphan bus detection
- Console QA summary

---

## [1.0.19] - 2026-02-02
### Added
- EQ8 band-level parameter extraction

---

## [1.0.18] - 2026-02-01
### Added
- Third-party plugin state hashing
- Plugin state hint extraction

---

## [1.0.17] - 2026-02-01
### Added
- --mix-settings mode
- High-value stock device parameter extraction

---

## [1.0.16] - 2026-02-01
### Added
- Group output resolution
- Routing path tracing

---

## [1.0.15] - 2026-01-31
### Added
- Failure reason legends
- Pretty-printed JSON output

---

## [1.0.14] - 2026-01-31
### Added
- Dead/orphan bus framework
- Routing break propagation

---

## [1.0.13] - 2026-01-30
### Added
- FULL parameter pruning
- Routing impact flags

---

## [1.0.12] - 2026-01-30
### Added
- Deactivated routing chain detection

---

## [1.0.11] - 2026-01-29
### Initial Public Working Release
- Dual FULL / COMPACT extractor
- Core routing and QA detection
