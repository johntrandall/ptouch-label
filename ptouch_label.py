# ptouch-label — print labels on Brother P-touch network label printers.
#
# Installed as a console script (`ptouch-label`) by pyproject.toml. Install
# with `pipx install .` or `uv tool install .`; both give the command a stable
# interpreter path, which matters on macOS because Local Network privacy is
# granted per executable path (a throwaway per-run interpreter never
# accumulates a grant).
"""
ptouch-label: Print labels to Brother PT-P750W.

Two backends:

  AirPrint/CUPS (opt-in via --no-high-resolution)
    Uses lpr against the local CUPS queue. Simple, works for single labels.
    The PT-P750W's AirPrint queue exposes only the IPP `finishings` enum
    (none / trim / trim-after-pages / trim-after-job), so half-cut, chain
    printing, mirror printing, and special-tape no-cut are UNREACHABLE
    through this path even though the hardware supports them. Also caps
    horizontal raster at 180 DPI — visibly fuzzier on real tape than the
    PT-Direct 360 DPI mode. Use this backend ONLY when print-time speed
    matters more than print quality (large batches, low-stakes prints).

  PT-Direct (default — auto-engaged because --high-resolution defaults ON)
    Bypasses CUPS and sends Brother's raster command protocol over TCP/9100.
    Exposes --half-cut, --chain, --copies, --mirror, --special-tape, and
    360 DPI horizontal raster. This is the default path; the AirPrint
    backend above is the opt-out.

    Implemented on top of the upstream `ptouch` Python library
    (nbuchwitz/ptouch on GitHub, LGPL-2.1) which already speaks the
    PT-E550W/PT-P750W raster protocol correctly. This file's job is the
    same as before — descender-corrected image rendering, fail-closed
    tape probe, ergonomic flags — and it delegates the byte-level
    protocol work to the library. Spec cross-checked against:
      Brother's "Software Developer's Manual: Raster Command Reference"
      for PT-E550W/P750W/P710BT, v1.02 (Brother support site, model
      PT-P750W > Manuals; not redistributed here).

Usage:
    ptouch-label "Hello World"
    ptouch-label "Line 1" "Line 2"
    ptouch-label --tape 12 "Small label"
    ptouch-label --length 80 "Longer label"
    ptouch-label --preview "Test before printing"
    ptouch-label --bold "Important"
    ptouch-label --font-size 32 "Custom size"
    ptouch-label --pt-direct --copies 12 --half-cut "PWR ONLY"
    ptouch-label --pt-direct --chain "First batch"
    ptouch-label --pt-direct --mirror "Iron-on"
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

# ---------------------------------------------------------------------------
# Printer model registry
# ---------------------------------------------------------------------------
# Two printers, two protocol generations:
#
#   PT-P750W — 128 print pins, 180 DPI native, 360 DPI in hi-res.
#     Supports 6/9/12/18/24mm laminated TZe tape. Existing default; all
#     CLI defaults and the original TAPE_SPECS table targeted this model.
#
#   PT-P950NW — 560 print pins, 360 DPI native, 720 DPI in hi-res.
#     Adds 3.5mm and 36mm tape support. Roughly 4x the pixel density of
#     the P750W at the same hi-res mode, so labels rendered for one model
#     are NOT interchangeable — the upstream library re-centers an image
#     whose height ≠ tape_config.print_pins, which would shrink a P750W
#     render to ~40% of the tape height on a P950NW.
#
# Model selection precedence (resolved in `_select_model_from_args`):
#   1. --model / --printer CLI flag (explicit override)
#   2. PTOUCH_MODEL env var (canonical name: 'PT-P750W' or 'PT-P950NW')
#   3. PTOUCH_PRINTER env var (CUPS queue name — auto-maps via QUEUE_TO_MODEL)
#   4. default_model in the config file, else PT-P750W
#
# Why a model dataclass and not just per-model module globals: keeps the
# rendering code (create_label_image, print_label, print_label_pt_direct)
# byte-identical across models — the only thing that changes is which
# dimensions and which upstream printer class are passed in. Do not fork
# `create_label_image` per model; extend the model table instead.

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PrinterModel:
    """A Brother P-touch printer model + its render-side specs.

    `tape_specs` keys: integer tape widths in mm. 3.5mm tape uses key 4 —
    the upstream `ptouch` library reports `Tape3_5mm.width_mm = 4` (likely
    rounding 3.5 → 4 for an int field), and we follow that convention so
    the same key snaps IPP-probed widths cleanly.

    `lib_class_name` is looked up at runtime via `getattr(pt, ...)` so
    this dataclass stays import-clean even when the ptouch library isn't
    installed (e.g. during `--preview` or `--dry-run`).
    """

    name: str
    cups_queue: str
    lib_class_name: str
    dpi: int
    tape_specs: dict[int, tuple[float, int]]
    tape_to_lib: dict[int, str]
    # Per-installation settings. These are EMPTY in the code on purpose and
    # are filled from the config file (see `_load_config`), so the model
    # table holds only facts about the hardware.
    #
    # default_host: printer address used when no CUPS queue, cache or mDNS
    #   record yields one, so a CUPS queue is not needed as an address book.
    # proxy_host: optional `host[:port]` relay in front of the raster port,
    #   tried first. Useful where macOS Local Network privacy blocks a script
    #   from the printer's subnet but a routed relay is reachable. Never cached.
    # vertical_offset_mm: per-unit calibration, applied to every label.
    #   Positive moves content toward higher head pins. Measure it with
    #   `--calibrate-vertical`.
    default_host: str = ""
    proxy_host: str = ""
    vertical_offset_mm: float = 0.0


# Brother PT-P750W — 128 pins, 180 DPI native.
#
# Values from `philpem/printer-driver-ptouch` rastertoptch.c. These are
# DELIBERATELY conservative compared to Brother's Raster Command Reference
# v1.02 page 20 — the upstream driver leaves margin to absorb cassette-
# positioning slop and laminate-edge artifacts. Net deltas vs. Brother's
# stated max:
#     24mm: 128 (matches Brother)
#     18mm: 85 vs Brother's 112  (27 px conservative)
#     12mm: 57 vs Brother's 70   (13 px conservative)
#      9mm: 49 vs Brother's 50   ( 1 px conservative)
#      6mm: 28 vs Brother's 32   ( 4 px conservative)
#
# Do NOT "fix" these to match Brother's spec without verifying tape
# positioning is reliable at the wider dimensions. The conservative
# values work well in practice; the PT-Direct path's library further
# re-centers our image inside its own (Brother-spec) print_pins, so the
# tiny gap on smaller tapes is invisible.
_MODEL_PT_P750W = PrinterModel(
    name="PT-P750W",
    cups_queue="Brother_PT_P750W",
    lib_class_name="PTP750W",
    dpi=180,
    tape_specs={
        24: (18.0, 128),
        18: (12.0, 85),
        12: (8.0, 57),
        9: (6.9, 49),
        6: (3.9, 28),
    },
    tape_to_lib={
        6: "Tape6mm",
        9: "Tape9mm",
        12: "Tape12mm",
        18: "Tape18mm",
        24: "Tape24mm",
    },
)

# Brother PT-P950NW — 560 pins, 360 DPI native.
#
# Values derived from upstream `ptouch` library `PTP900Series.PIN_CONFIGS`
# (printers.py), which sources Brother's PT-P900 Raster Command Reference
# v1.02 pages 23-24 directly. Unlike the PT-P750W table above, these are
# NOT conservative — Brother's spec values are used directly because the
# P900 series' wider 560-pin head positions the tape with more tolerance.
#
# printable_mm derived from print_pins via Brother's vertical 360 DPI:
#   printable_mm ≈ print_pins / (360 / 25.4) ≈ print_pins / 14.173
# printable_px == print_pins (1:1 mapping at the printer's native DPI).
#
# Note: 3.5mm tape uses dict key 4 to match the upstream library's
# Tape3_5mm.width_mm = 4 convention.
_MODEL_PT_P950NW = PrinterModel(
    name="PT-P950NW",
    cups_queue="Brother_PT_P950NW",
    lib_class_name="PTP950NW",
    dpi=360,
    tape_specs={
        36: (32.03, 454),
        24: (22.58, 320),
        18: (16.51, 234),
        12: (10.58, 150),
        9: (7.48, 106),
        6: (4.52, 64),
        4: (3.39, 48),  # canonical key for 3.5mm tape (lib convention)
    },
    tape_to_lib={
        4: "Tape3_5mm",
        6: "Tape6mm",
        9: "Tape9mm",
        12: "Tape12mm",
        18: "Tape18mm",
        24: "Tape24mm",
        36: "Tape36mm",
    },
)

MODELS: dict[str, PrinterModel] = {
    "PT-P750W": _MODEL_PT_P750W,
    "PT-P950NW": _MODEL_PT_P950NW,
}

# CUPS queue name → model. Used when PTOUCH_PRINTER is set but PTOUCH_MODEL
# isn't, so the user can stay in the existing single-env-var pattern.
_QUEUE_TO_MODEL: dict[str, str] = {
    "Brother_PT_P750W": "PT-P750W",
    "Brother_PT_P950NW": "PT-P950NW",
}


def _resolve_model_from_env() -> PrinterModel:
    """Pick a model from env vars at module load. CLI flags can override later."""
    env_model = os.environ.get("PTOUCH_MODEL")
    if env_model:
        if env_model not in MODELS:
            print(
                f"WARNING: PTOUCH_MODEL={env_model!r} not recognized; "
                f"valid: {sorted(MODELS)}. Falling back to the default model.",
                file=sys.stderr,
            )
        else:
            return MODELS[env_model]
    env_queue = os.environ.get("PTOUCH_PRINTER")
    if env_queue and env_queue in _QUEUE_TO_MODEL:
        return MODELS[_QUEUE_TO_MODEL[env_queue]]
    return MODELS[_default_model_name(_CONFIG)]


# CUPS queue names vary per Mac. AirPrint auto-discovery originally created
# queues named after the model string (`Brother_PT_P750W`); the 2026-05-18
# installation may rename them; name yours in the config file (`cups_queue`,
# `queue_aliases`), and every known name is accepted rather than
# forcing every machine to set an env var.
#
# Ordered most- to least-preferred. First one that actually exists in CUPS wins.
_QUEUE_ALIASES: dict[str, list[str]] = {
    "PT-P750W": ["Brother_PT_P750W"],
    "PT-P950NW": ["Brother_PT_P950NW"],
}

_queue_resolution_cache: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Config file
# ---------------------------------------------------------------------------
# Everything specific to one installation lives here, not in the model table:
#
#   # ~/.config/ptouch-label/config.toml
#   default_model = "PT-P950NW"
#
#   [printers.PT-P950NW]
#   host = "label-printer.example.lan"   # or an IP address
#   proxy = "relay.example.lan:9101"     # optional host:port relay to port 9100
#   vertical_offset_mm = 1.45            # from --calibrate-vertical
#   cups_queue = "Office-Labels"         # optional CUPS queue name
#   queue_aliases = ["Labels"]           # optional extra names --printer accepts
#
# Location: $PTOUCH_LABEL_CONFIG, else $XDG_CONFIG_HOME/ptouch-label/config.toml,
# else ~/.config/ptouch-label/config.toml. A missing file is fine; environment
# variables (PTOUCH_MODEL, PTOUCH_HOST, ...) still override what it says.

_CONFIG_KEYS = {"host", "proxy", "vertical_offset_mm", "cups_queue", "queue_aliases"}


def _config_path() -> Path:
    explicit = os.environ.get("PTOUCH_LABEL_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ptouch-label" / "config.toml"


def _load_config() -> dict:
    path = _config_path()
    if not path.is_file():
        return {}
    import tomllib
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"WARNING: ignoring unreadable config {path}: {exc}", file=sys.stderr)
        return {}
    from dataclasses import replace
    for name, cfg in (data.get("printers") or {}).items():
        if name not in MODELS:
            print(f"WARNING: config {path}: unknown printer model {name!r}; "
                  f"valid: {sorted(MODELS)}", file=sys.stderr)
            continue
        unknown = set(cfg) - _CONFIG_KEYS
        if unknown:
            print(f"WARNING: config {path}: [printers.{name}] unknown keys "
                  f"{sorted(unknown)}; valid: {sorted(_CONFIG_KEYS)}", file=sys.stderr)
        updates = {}
        if "host" in cfg:
            updates["default_host"] = str(cfg["host"])
        if "proxy" in cfg:
            updates["proxy_host"] = str(cfg["proxy"])
        if "vertical_offset_mm" in cfg:
            updates["vertical_offset_mm"] = float(cfg["vertical_offset_mm"])
        if "cups_queue" in cfg:
            updates["cups_queue"] = str(cfg["cups_queue"])
        if updates:
            MODELS[name] = replace(MODELS[name], **updates)
        names = [MODELS[name].cups_queue] + [str(a) for a in cfg.get("queue_aliases", [])]
        for q in names:
            _QUEUE_TO_MODEL.setdefault(q, name)
            aliases = _QUEUE_ALIASES.setdefault(name, [])
            if q not in aliases:
                aliases.insert(0, q) if q == MODELS[name].cups_queue else aliases.append(q)
    return data


def _default_model_name(config: dict) -> str:
    name = config.get("default_model")
    if name and name in MODELS:
        return name
    if name:
        print(f"WARNING: config default_model={name!r} not recognized; "
              f"valid: {sorted(MODELS)}. Using PT-P750W.", file=sys.stderr)
    return "PT-P750W"


_CONFIG = _load_config()


def _cups_queue_exists(name: str) -> bool:
    """True if CUPS knows a destination by this name."""
    return subprocess.run(
        ["lpstat", "-p", name],
        capture_output=True, text=True, errors="replace", check=False,
    ).returncode == 0


def _resolve_cups_queue(model_name: str, preferred: str) -> str:
    """Pick a CUPS queue that exists, preferring `preferred`.

    Two tiers of override, deliberately different:

    - `PTOUCH_PRINTER` / `--printer` never reach this function. They are
      absolute: the user named a queue, so they get it and any resulting error,
      rather than silent substitution.
    - The config file's `cups_queue` / `queue_aliases` DO pass through here.
      They are per-model name hints, so they are existence-checked and fall back to a
      known alias if the named queue is absent.

    Returns `preferred` unchanged when nothing matches, so the failure surfaces
    where it is meaningful instead of here.
    """
    cached = _queue_resolution_cache.get(preferred)
    if cached is not None:
        return cached
    candidates = [preferred] + [
        a for a in _QUEUE_ALIASES.get(model_name, []) if a != preferred
    ]
    resolved = next((c for c in candidates if _cups_queue_exists(c)), preferred)
    _queue_resolution_cache[preferred] = resolved
    return resolved


# Active model — initialized from env, mutated by `_select_model()` once
# CLI args are parsed in main(). Globals below shadow the dataclass fields
# so existing function bodies (create_label_image, print_label, etc.) stay
# byte-identical without threading a model parameter through every call.
ACTIVE_MODEL: PrinterModel = _resolve_model_from_env()
_PRINTER_OVERRIDE = os.environ.get("PTOUCH_PRINTER")
PRINTER_NAME = _PRINTER_OVERRIDE or _resolve_cups_queue(
    ACTIVE_MODEL.name, ACTIVE_MODEL.cups_queue
)
DPI = ACTIVE_MODEL.dpi
DOTS_PER_MM = DPI / 25.4  # ~7.087 at 180 DPI, ~14.173 at 360
TAPE_SPECS = ACTIVE_MODEL.tape_specs


def _select_model(model_name: str, cups_queue_override: str | None = None) -> None:
    """Switch the active model and rebind dependent module globals.

    Called from main() AFTER argparse so `--model` / `--printer` flags can
    override the env-var defaults. Bare module globals (DPI, DOTS_PER_MM,
    TAPE_SPECS, PRINTER_NAME) are rebound here so the rendering functions
    don't need explicit model plumbing.
    """
    global ACTIVE_MODEL, PRINTER_NAME, DPI, DOTS_PER_MM, TAPE_SPECS
    if model_name not in MODELS:
        raise ValueError(
            f"Unknown model {model_name!r}; valid: {sorted(MODELS)}"
        )
    ACTIVE_MODEL = MODELS[model_name]
    PRINTER_NAME = cups_queue_override or _resolve_cups_queue(
        ACTIVE_MODEL.name, ACTIVE_MODEL.cups_queue
    )
    DPI = ACTIVE_MODEL.dpi
    DOTS_PER_MM = DPI / 25.4
    TAPE_SPECS = ACTIVE_MODEL.tape_specs

# Font search paths: macOS first, then common Linux locations.
FONT_PATHS_REGULAR = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFCompact.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

FONT_PATHS_BOLD = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


# ---------------------------------------------------------------------------
# Tape probe (IPP query against the printer)
# ---------------------------------------------------------------------------
#
# The PT-P750W reports the currently loaded tape via the IPP `media-ready`
# and `media-col-ready` attributes (per RFC 8011). The local CUPS proxy
# queue does NOT propagate these (it returns letter-paper defaults), so
# we must query the printer directly via its mDNS hostname.
#
# Do NOT confuse `media-ready` (currently loaded) with `media-default`
# (printer's max supported size — the PT-P750W always reports this as 24mm
# regardless of what's actually in the printer).
#
# Steps:
#   1. Read CUPS device-uri to get the dnssd service name
#      (e.g. "dnssd://Brother%20PT-P750W._ipp._tcp.local./?uuid=...")
#   2. Resolve that service via `dns-sd -L` to a BRW{MAC}.local hostname:port
#   3. Run `ipptool` against the resolved IPP endpoint
#   4. Parse `media-col-ready x-dimension` (in 0.01mm units) → tape width mm
#   5. Snap to nearest supported tape width (6, 9, 12, 18, 24)

PROBE_CACHE_DIR = Path.home() / ".cache" / "ptouch-label"


def _probe_cache_path() -> Path:
    """Per-model cache path so PT-P750W and PT-P950NW don't
    collide on the same `printer-host.txt`.

    Pre-2026-06-11 the cache was a single `printer-host.txt`; we keep that
    legacy filename for PT-P750W so existing PT-P750W setups don't lose their
    cached mDNS resolution on first run after upgrade.
    """
    if ACTIVE_MODEL.name == "PT-P750W":
        return PROBE_CACHE_DIR / "printer-host.txt"
    return PROBE_CACHE_DIR / f"printer-host-{ACTIVE_MODEL.name}.txt"


# Kept as a legacy alias for callers that imported this constant directly.
# DO NOT use for new code — call _probe_cache_path() which reflects the
# active model at the time of the call.
PROBE_CACHE = PROBE_CACHE_DIR / "printer-host.txt"


def _get_dnssd_service(printer_name: str) -> str | None:
    out = subprocess.run(
        ["lpoptions", "-p", printer_name],
        capture_output=True, text=True, errors="replace", check=False,
    ).stdout
    m = re.search(r"device-uri=(\S+)", out)
    if not m or not m.group(1).startswith("dnssd://"):
        return None
    rest = m.group(1)[len("dnssd://"):]
    service_full = rest.split("/", 1)[0]
    service_name = service_full.split("._ipp._tcp", 1)[0]
    return urllib.parse.unquote(service_name)


def _resolve_mdns(service_name: str, timeout: float = 2.5) -> tuple[str, int] | None:
    """`dns-sd -L` runs forever; capture stdout for `timeout`, then kill."""
    proc = subprocess.Popen(
        ["dns-sd", "-L", service_name, "_ipp._tcp", "local."],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        errors="replace",
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    m = re.search(r"can be reached at (\S+):(\d+)", out)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _snap_to_supported(width_mm: float) -> int | None:
    """Snap a measured tape width to the nearest supported tape size."""
    supported = sorted(TAPE_SPECS.keys())
    if width_mm < supported[0] - 1.5 or width_mm > supported[-1] + 1.5:
        return None
    return min(supported, key=lambda w: abs(w - width_mm))


def _split_host_port(host: str, default_port: int = 9100) -> tuple[str, int]:
    """`host` or `host:port` -> (host, port). Lets a forwarder sit on any port."""
    if host.count(":") == 1:
        h, p = host.rsplit(":", 1)
        if p.isdigit():
            return h, int(p)
    return host, default_port


def _tcp_accepts(host: str, timeout: float = 4.0) -> bool:
    """True when `host[:port]` accepts a TCP connection. Printing is write-only
    (the ptouch library never waits for a status reply), so an accepting
    forwarder is a usable print path even when the printer stays silent."""
    import socket
    try:
        with socket.create_connection(_split_host_port(host), timeout=timeout):
            return True
    except OSError:
        return False


def _query_raster_tape_mm(host: str, timeout: float = 6.0) -> int | None:
    """Probe loaded tape via Brother's raster status command on TCP/9100.

    Independent of IPP — works on any Brother P-touch that speaks the
    raster protocol, including the PT-P950NW which does NOT expose IPP
    on port 631 when connected over wired Ethernet.

    Wire sequence (per Brother Raster Command Reference v1.02 + P900
    series equivalent):
      Send: 200 × 0x00 (invalidate buffer)
            ESC @ (0x1B 0x40, init)
            ESC i S (0x1B 0x69 0x53, status request)
      Recv: 32-byte status block, with byte index 10 = media (tape) width
            in mm. byte 18 == 0 indicates "status reply" (vs phase change /
            error / etc.).

    Returns None on socket error, short read, or unrecognized status type.
    Caller is responsible for `_snap_to_supported` if it wants strict
    snap-to-key behavior.
    """
    import socket
    try:
        with socket.create_connection(_split_host_port(host), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x00" * 200)        # invalidate
            s.sendall(b"\x1b\x40")          # ESC @
            s.sendall(b"\x1b\x69\x53")      # ESC i S
            # Brother emits 32 bytes. Drain all available within the timeout.
            buf = b""
            while len(buf) < 32:
                chunk = s.recv(32 - len(buf))
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    if len(buf) < 32:
        return None
    # byte 18: status type. 0=phase change reply, 1=printing done, 2=error,
    # 3=notification, 4=phase change request, 5=status reply (varies by
    # firmware — be permissive). byte 10: media width in mm. byte 11:
    # media type (0x00=no media, 0x01=laminated, 0x11=non-laminated, etc.).
    media_width = buf[10]
    if media_width == 0:
        # 0 = no media loaded (or invalid response)
        return None
    return _snap_to_supported(media_width)


def _query_ews_tape_mm(host: str, timeout: float = 6.0) -> int | None:
    """Probe loaded tape by scraping the printer's EWS status page (HTTP/80).

    Works on Brother PT-P9xx where IPP/631 is closed and the raster ESC i S
    status response doesn't fire over TCP/9100 without a full init sequence.
    The EWS page is dead simple to scrape — Brother's HTML format hasn't
    changed across firmware revisions in our experience, and the value is right there
    in the document body without any auth or JS rendering required.

    Example response line (literal, with HTML entity-encoded spaces):
        <dt>Media&#32;Type</dt><dd>12mm(0.47")</dd>

    Returns None if the page doesn't load, the field isn't found, or the
    width doesn't snap to a supported tape size.
    """
    import shutil
    import urllib.request
    url = f"http://{host}/general/status.html"
    html = None
    # Fetch with the platform `curl` first. Apple platform binaries are
    # exempt from macOS Local Network privacy; this Python is not, and under
    # an app with no grant, a raw socket to the printer's subnet fails with
    # EHOSTUNREACH while the same URL through curl succeeds (Verified
    # 2026-10-01). urllib stays as the fallback for hosts without curl.
    if shutil.which("curl"):
        try:
            proc = subprocess.run(
                ["curl", "-s", "--max-filesize", "262144", "-m", str(int(timeout)), url],
                capture_output=True, text=True, errors="replace",
                timeout=timeout + 2, check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                html = proc.stdout[:262144]
        except (OSError, subprocess.TimeoutExpired):
            html = None
    if html is None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                html = resp.read(262144).decode("utf-8", errors="replace")
        except OSError:
            return None
    # Brother encodes spaces as &#32; in attribute-adjacent text
    # ("Media&#32;Type"). Match either form.
    m = re.search(
        r"Media(?:&#32;|\s)?Type</dt>\s*<dd>(\d+(?:\.\d+)?)\s*mm",
        html,
        re.IGNORECASE,
    )
    if not m:
        return None
    return _snap_to_supported(float(m.group(1)))


def _query_ipp_tape_mm(host: str, port: int, timeout: float = 8.0) -> int | None:
    uri = f"ipp://{host}:{port}/ipp/print"
    try:
        proc = subprocess.run(
            ["ipptool", "-tv", uri, "get-printer-attributes.test"],
            capture_output=True, text=True, errors="replace", timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    out = proc.stdout
    # Prefer media-col-ready (collection with x-dimension in 0.01mm units)
    out = out[:262144]
    m = re.search(r"media-col-ready.*?x-dimension=(\d{1,6})\b", out, re.DOTALL)
    if m:
        return _snap_to_supported(int(m.group(1)) / 100)
    # Fallback: media-ready keyword "roll_current_9x0mm" or "roll_9x0mm"
    m = re.search(
        r"media-ready\s+\(keyword\)\s*=\s*roll_(?:current_)?(\d{1,4}(?:\.\d{1,3})?)x",
        out,
    )
    if m:
        return _snap_to_supported(float(m.group(1)))
    return None


def _read_host_cache() -> tuple[str, int] | None:
    try:
        text = _probe_cache_path().read_text().strip()
        host, port = text.rsplit(":", 1)
        return host, int(port)
    except (OSError, ValueError):
        return None


def _write_host_cache(host: str, port: int) -> None:
    try:
        path = _probe_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{host}:{port}\n")
    except OSError:
        pass


def probe_loaded_tape(
    printer_name: str | None = None,
    verbose: bool = False,
    retries: int = 1,
) -> int | None:
    """Query the printer for the currently loaded tape width in mm.

    Returns the tape width (one of 6/9/12/18/24) or None if probe fails.
    Tries cached host first, falls back to dns-sd discovery.

    On timeout, retries once by default. The PT-P750W's WiFi radio
    sleeps deeply; the first probe attempt often wakes it but times
    out, and a second attempt succeeds. Set retries=0 to disable.
    """
    # Late-bind printer_name so --model / --printer changes after module
    # load reach this default. Early-binding via `printer_name=PRINTER_NAME`
    # in the signature would freeze the env-var value.
    if printer_name is None:
        printer_name = PRINTER_NAME

    def _say(msg: str) -> None:
        if verbose:
            print(f"[probe] {msg}", file=sys.stderr)

    def _attempt() -> int | None:
        override = _host_override()
        if override:
            _say(f"host from environment override: {override}")
            for probe, label in (
                (lambda h: _query_ipp_tape_mm(h, 631), "IPP"),
                (_query_raster_tape_mm, "raster ESC i S"),
                (_query_ews_tape_mm, "EWS scrape"),
            ):
                width = probe(override)
                if width:
                    _say(f"detected tape width: {width}mm ({label}, override host)")
                    return width
            _say("override host: IPP + raster + EWS all failed")
            return None

        if ACTIVE_MODEL.proxy_host:
            # Do NOT send the raster status preamble through the proxy. The
            # PT-P950NW never answers ESC i S over TCP/9100 (Verified
            # 2026-10-01), and a 2026-10-01 job that followed two such
            # probe connections came out as a blank feed — the first blank
            # since the EWS-based probe path went in. Whether the preamble
            # caused it is Inferred; keeping the printer's 9100 traffic to
            # exactly one connection per job removes the variable. Tape
            # width comes from the EWS via curl instead.
            _say(f"print proxy {ACTIVE_MODEL.proxy_host} configured; skipping raster probe, asking EWS")
            if ACTIVE_MODEL.default_host:
                width = _query_ews_tape_mm(ACTIVE_MODEL.default_host)
                if width:
                    _say(f"detected tape width: {width}mm (EWS scrape via curl, {ACTIVE_MODEL.default_host})")
                    return width
            _say("proxy gave no status and EWS failed; falling through to direct paths")

        cached = _read_host_cache()
        if cached is None and not _get_direct_host_from_device_uri(
            printer_name or ACTIVE_MODEL.cups_queue
        ) and ACTIVE_MODEL.default_host:
            # No cache, no queue to read an address from. Fall back to the
            # model's canonical DNS name rather than give up — this is what
            # lets the dead placeholder queue be deleted.
            host = ACTIVE_MODEL.default_host
            _say(f"no cache or queue; falling back to default host {host}")
            for probe, label in (
                (lambda h: _query_ipp_tape_mm(h, 631), "IPP"),
                (_query_raster_tape_mm, "raster ESC i S"),
                (_query_ews_tape_mm, "EWS scrape"),
            ):
                width = probe(host)
                if width:
                    _say(f"detected tape width: {width}mm ({label}, default host)")
                    _write_host_cache(host, 80)
                    return width
            _say("default host: IPP + raster + EWS all failed")
            return None
        if cached:
            _say(f"trying cached host {cached[0]}:{cached[1]} (IPP)")
            width = _query_ipp_tape_mm(*cached)
            if width is not None:
                _say(f"detected tape width: {width}mm (IPP)")
                return width
            _say("cached IPP failed, trying raster status on same host")
            width = _query_raster_tape_mm(cached[0])
            if width is not None:
                _say(f"detected tape width: {width}mm (raster ESC i S)")
                return width
            _say("cached host raster also failed, refreshing host")

        # No usable cache. Try direct host from CUPS device-uri first —
        # this is the PT-P950NW path where the queue is socket://, not
        # dnssd://. Cheaper than mDNS and works across VLAN boundaries.
        direct = _get_direct_host_from_device_uri(printer_name)
        if direct:
            _say(f"direct host from CUPS device-uri: {direct}")
            # Try IPP first in case the printer DOES expose 631 (some Brother
            # models do); fall back to raster status on TCP/9100, then EWS
            # scrape on HTTP/80. The PT-P950NW path reliably hits the EWS;
            # its raster status response doesn't fire without full init.
            width = _query_ipp_tape_mm(direct, 631)
            if width is not None:
                _say(f"detected tape width: {width}mm (IPP, direct host)")
                _write_host_cache(direct, 631)
                return width
            width = _query_raster_tape_mm(direct)
            if width is not None:
                _say(f"detected tape width: {width}mm (raster ESC i S, direct host)")
                _write_host_cache(direct, 9100)
                return width
            width = _query_ews_tape_mm(direct)
            if width is not None:
                _say(f"detected tape width: {width}mm (EWS scrape, direct host)")
                _write_host_cache(direct, 80)
                return width
            _say("direct host: IPP + raster + EWS all failed")

        # Fall back to dnssd:// queue — the original PT-P750W path.
        service = _get_dnssd_service(printer_name)
        if not service:
            _say(f"no dnssd device-uri for {printer_name}")
            return None
        _say(f"dnssd service: {service!r}")
        resolved = _resolve_mdns(service)
        if not resolved:
            _say("mDNS resolve failed")
            return None
        _say(f"resolved: {resolved[0]}:{resolved[1]}")
        width = _query_ipp_tape_mm(*resolved)
        if width is not None:
            _write_host_cache(*resolved)
            _say(f"detected tape width: {width}mm (IPP, mDNS)")
            return width
        # mDNS-resolved host but IPP failed — try raster on same host.
        width = _query_raster_tape_mm(resolved[0])
        if width is not None:
            _write_host_cache(resolved[0], 9100)
            _say(f"detected tape width: {width}mm (raster ESC i S, mDNS host)")
            return width
        _say("mDNS host: both IPP and raster failed")
        return None

    for attempt_num in range(retries + 1):
        if attempt_num > 0:
            _say(f"retrying probe (attempt {attempt_num + 1}/{retries + 1})")
        width = _attempt()
        if width is not None:
            return width
    return None


def find_font(bold: bool = False, size: int = 24) -> ImageFont.FreeTypeFont:
    """Find and load a suitable font."""
    paths = FONT_PATHS_BOLD if bold else FONT_PATHS_REGULAR
    # Also try regular paths as fallback for bold
    if bold:
        paths = paths + FONT_PATHS_REGULAR

    for fp in paths:
        try:
            # For .ttc files, index 0 is regular, index 1 is often bold
            if fp.endswith(".ttc"):
                idx = 1 if bold and "Helvetica" in fp else 0
                return ImageFont.truetype(fp, size, index=idx)
            return ImageFont.truetype(fp, size)
        except (OSError, IOError):
            continue

    print("Warning: No TrueType font found, using default bitmap font", file=sys.stderr)
    return ImageFont.load_default()


def auto_font_size(printable_px: int, num_lines: int) -> int:
    """Calculate a font size that fills the tape height well."""
    # Use ~75% of available height per line, leave room for spacing
    available_per_line = printable_px / num_lines
    size = int(available_per_line * 0.70)
    return max(8, min(size, 200))


def measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont):
    """Measure text dimensions and the y-offset of inked pixels.

    Returns (width, height, top_offset). `top_offset` is bbox[1] — the gap
    between the y-coordinate passed to draw.text() (default anchor 'la')
    and the topmost inked pixel. Some fonts (notably HelveticaNeue Bold)
    return a non-trivial top_offset; centering math must compensate or
    descenders will collide with the bottom edge.
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1], bbox[1]


def create_label_image(
    lines: list[str],
    tape_width: int = 24,
    length_mm: float | None = None,
    font_size: int | None = None,
    bold: bool = False,
    padding_mm: float = 4.0,
) -> tuple[Image.Image, float]:
    """
    Create a label image for the given tape width.

    Returns (image_in_landscape_orientation, label_length_mm).
    """
    spec = TAPE_SPECS.get(tape_width)
    if not spec:
        print(
            f"Error: Unsupported tape width {tape_width}mm. "
            f"Supported: {sorted(TAPE_SPECS.keys())}",
            file=sys.stderr,
        )
        sys.exit(1)

    printable_mm, printable_px = spec

    # Font size
    if font_size is None:
        font_size = auto_font_size(printable_px, len(lines))
    font = find_font(bold=bold, size=font_size)

    # Measure all lines to determine label length
    tmp_img = Image.new("L", (1, 1), 255)
    tmp_draw = ImageDraw.Draw(tmp_img)

    line_dims = []
    max_text_width = 0
    for line in lines:
        w, h, top_off = measure_text(tmp_draw, line, font)
        line_dims.append((w, h, top_off))
        max_text_width = max(max_text_width, w)

    line_spacing = max(2, int(font_size * 0.15))
    total_text_height = sum(h for _, h, _ in line_dims) + line_spacing * (len(lines) - 1)

    # Label length in pixels
    padding_px = int(padding_mm * DOTS_PER_MM)
    label_length_px = max_text_width + 2 * padding_px

    if length_mm is not None:
        label_length_px = int(length_mm * DOTS_PER_MM)

    # Enforce minimums
    min_length_px = int(15 * DOTS_PER_MM)  # 15mm minimum
    label_length_px = max(label_length_px, min_length_px)

    # Create landscape image: width=label_length, height=printable_area
    img = Image.new("L", (label_length_px, printable_px), 255)
    draw = ImageDraw.Draw(img)

    # Draw text centered.
    #
    # `draw.text((x, y), ...)` with Pillow's default anchor 'la' places the
    # ascender line at y, but the actual topmost inked pixel is at y + bbox[1]
    # (== top_off). HelveticaNeue Bold reports top_off ≈ 9 at size 34, so
    # ignoring it pushes text 9 px off-center — and on 9mm tape (49 px tall)
    # that puts descenders on the very last row, which the printer clips.
    # Subtracting the first line's top_off cancels that bias.
    y = (printable_px - total_text_height) // 2 - line_dims[0][2]
    for i, line in enumerate(lines):
        tw, th, _ = line_dims[i]
        x = (label_length_px - tw) // 2
        draw.text((x, y), line, fill=0, font=font)
        y += th + line_spacing

    label_length_mm = label_length_px / DOTS_PER_MM
    return img, label_length_mm


def save_label_pdf(img: Image.Image, path: str, tape_width: int) -> tuple[float, float]:
    """Write the label as a PDF page at TRUE tape geometry, and return (w_mm, h_mm).

    Why this exists: `--preview` writes a PNG, which is a picture OF a label
    rather than a label. Handed one, a human cannot reprint it — there is no
    page size, so any print path guesses, and on a normal printer it lands on
    A4. That is the dead end a person hits when an agent finishes: the artifact is
    unusable without going back through an agent.

    A PDF carries the page size, so it reprints at the right dimensions from
    any path that understands paper — including the macOS print dialog once a
    label queue exists.

    The page is the PRINTED area, not the cassette width: `TAPE_SPECS[w][0]` is
    the printable millimetres, which is narrower than the tape (24mm tape prints
    22.58mm on the PT-P950NW). Saving at `resolution=DPI` makes Pillow derive
    the page from pixels, which lands on exactly that figure.
    """
    spec = TAPE_SPECS.get(tape_width)
    if spec is None:
        raise SystemExit(f"ERROR: no tape spec for {tape_width}mm")
    out = img.convert("L")
    out.save(path, "PDF", resolution=float(DPI))
    return img.width / DOTS_PER_MM, spec[0]


def load_label_image(
    path: str,
    tape_width: int = 24,
    trim: bool = True,
    length_mm: float | None = None,
) -> tuple[Image.Image, float]:
    """
    Load a pre-rendered image and fit it to the tape.

    Returns (image_in_landscape_orientation, label_length_mm) — the same shape
    `create_label_image` returns, so every downstream path (preview, AirPrint,
    PT-Direct, multi-label) works on it unchanged.

    `trim` removes surrounding white before scaling. This matters more than it
    sounds: a page arriving from a print dialog is the full media size, so a
    100mm page carrying 20mm of text would otherwise feed — and cut — 100mm of
    mostly-blank tape. Trimming is therefore the default, and --no-trim exists
    for the case where the blank margin is deliberate.
    """
    spec = TAPE_SPECS.get(tape_width)
    if spec is None:
        raise SystemExit(
            f"ERROR: {tape_width}mm is not a supported tape width for "
            f"{ACTIVE_MODEL.name} (have: "
            f"{', '.join(str(w) for w in sorted(TAPE_SPECS))})"
        )
    printable_px = spec[1]

    try:
        src = Image.open(path)
    except Exception as exc:
        raise SystemExit(f"ERROR: cannot open image {path!r}: {exc}")

    # Flatten transparency onto white; a label is printed on opaque tape, and
    # an unflattened alpha channel turns into black smear at 1-bit.
    if src.mode in ("RGBA", "LA", "P"):
        src = src.convert("RGBA")
        flat = Image.new("RGBA", src.size, (255, 255, 255, 255))
        flat.alpha_composite(src)
        src = flat
    img = src.convert("L")

    if trim:
        # getbbox() finds non-zero pixels, so invert first: we want the
        # bounding box of the DARK content, not of the white background.
        #
        # Crop the LENGTH ONLY, never the height. The vertical axis is the tape
        # width, which is fixed by the cassette and already correct; the
        # horizontal axis is how much tape gets fed, which is what should follow
        # the content. Cropping both and then scaling back to full printable
        # height magnifies everything by (tape height / ink height) — a label
        # whose text does not touch the tape edges came back 1.5x too long and
        # too large on a round trip. Caught by re-importing a PDF this tool had
        # just written: 99.7mm out, 149.9mm back.
        bbox = ImageOps.invert(img).getbbox()
        if bbox:
            img = img.crop((bbox[0], 0, bbox[2], img.height))
        else:
            raise SystemExit(
                f"ERROR: {path!r} is entirely blank after trimming — nothing "
                f"to print. Use --no-trim to print it anyway."
            )

    if img.height != printable_px:
        scale = printable_px / img.height
        new_w = max(1, round(img.width * scale))
        img = img.resize((new_w, printable_px), Image.LANCZOS)

    if length_mm is not None:
        target_w = max(1, round(length_mm * DOTS_PER_MM))
        if target_w != img.width:
            canvas = Image.new("L", (target_w, printable_px), 255)
            if target_w < img.width:
                img = img.resize((target_w, printable_px), Image.LANCZOS)
                canvas = img
            else:
                canvas.paste(img, ((target_w - img.width) // 2, 0))
            img = canvas

    return img, img.width / DOTS_PER_MM


def shift_vertical(img: Image.Image, offset_mm: float) -> Image.Image:
    """Move label content by `offset_mm` toward higher image rows (= higher
    head pins). Rows pushed past the printable band are clipped; the caller
    warns when that happens."""
    dy = int(round(offset_mm * DOTS_PER_MM))
    if dy == 0:
        return img
    out = Image.new(img.mode, img.size, 255)
    out.paste(img, (0, dy))
    return out


def create_calibration_image(tape_width: int, length_mm: float = 45.0):
    """A label that measures this printer's vertical bias in one print.

    Layout (image space, rows grow toward higher head pins):
      - a 3 px line across the full length at the image center ("0");
      - a solid full-height block at the left edge, showing where the
        printable band's two edges actually land on the tape;
      - one short tick per column at 0.5 mm steps above and below center,
        thick for whole millimetres, thin for halves;
      - under each whole-millimetre tick, its value: the number to pass as
        `--vertical-offset-mm` (or store in the model) if THAT tick is the one
        sitting on the tape's physical middle. Half-millimetre ticks are read
        by counting from the nearest labelled one.
    """
    printable_mm, printable_px = TAPE_SPECS[tape_width]
    w = int(length_mm * DOTS_PER_MM); h = printable_px
    img = Image.new("L", (w, h), 255); draw = ImageDraw.Draw(img)
    c = (h - 1) / 2
    draw.rectangle([0, int(c) - 1, w - 1, int(c) + 1], fill=0)          # center line
    draw.rectangle([0, 0, 9, h - 1], fill=0)                             # band-edge block
    label_band = 13 if h >= 90 else 0                                    # bottom text band
    half_span = h / 2 - 6 - label_band
    max_k = int(half_span / (0.5 * DOTS_PER_MM))
    ks = [k for k in range(-max_k, max_k + 1) if k != 0]
    font = None
    if label_band:
        for path in ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"):
            try:
                font = ImageFont.truetype(path, 11); break
            except OSError:
                continue
        font = font or ImageFont.load_default()
    x0, x1 = 24, w - 8
    step = (x1 - x0) / max(len(ks), 1)
    for i, k in enumerate(ks):
        d_mm = k * 0.5
        r = int(round(c + d_mm * DOTS_PER_MM))
        x = int(x0 + i * step)
        thick = (k % 2 == 0)
        draw.rectangle([x, r - (1 if thick else 0), x + int(step * 0.7), r + (1 if thick else 0)], fill=0)
        if label_band and thick:
            txt = f"{d_mm:+.0f}" if d_mm == int(d_mm) else f"{d_mm:+.1f}"
            draw.text((x, h - label_band), txt, fill=0, font=font)
    return img, length_mm


def print_label(
    img: Image.Image,
    tape_width: int,
    label_length_mm: float,
    dry_run: bool = False,
) -> None:
    """
    Rotate the image and send to the printer via lpr.

    The AirPrint PPD uses custom page sizes in portrait orientation
    (width=tape, height=label_length). We rotate the landscape image
    90° clockwise so it matches the portrait page layout.
    """
    # Rotate 90° clockwise: landscape → portrait
    # After rotation: width=printable_px, height=label_length_px
    img_rotated = img.transpose(Image.Transpose.ROTATE_270)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img_rotated.save(f.name, dpi=(DPI, DPI))
        tmp_path = f.name

    # Build lpr command with custom page size matching the PRINTABLE area
    # Using tape_width here causes fit-to-page to scale into non-printable
    # margins, truncating both edges. Use printable width instead.
    printable_mm = TAPE_SPECS[tape_width][0]
    cmd = [
        "lpr",
        "-P", PRINTER_NAME,
        "-o", f"PageSize=Custom.{printable_mm}x{label_length_mm:.1f}mm",
        "-o", "fit-to-page",
        "-o", "page-left=0",
        "-o", "page-right=0",
        "-o", "page-top=0",
        "-o", "page-bottom=0",
        tmp_path,
    ]

    try:
        if dry_run:
            print("Would run:", " ".join(cmd))
        else:
            try:
                subprocess.run(
            cmd, check=True, capture_output=True, text=True, errors="replace"
        )
            except subprocess.CalledProcessError as e:
                print(f"Error printing: {e.stderr}", file=sys.stderr)
                sys.exit(1)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PT-Direct backend (TCP/9100, via upstream `ptouch` library)
# ---------------------------------------------------------------------------
#
# The AirPrint queue can't reach half-cut, chain, mirror, or special-tape
# because the IPP `finishings` enum has no vocabulary for them. This path
# bypasses CUPS entirely and talks Brother's raster protocol directly to
# port 9100 on the printer.
#
# We don't reimplement the protocol — the upstream `ptouch` library
# (nbuchwitz/ptouch, LGPL-2.1) does that, and it does it correctly per
# Brother's published PT-E550W/P750W/P710BT Raster Command Reference v1.02
# (Brother support site, PT-P750W > Manuals; not redistributed here).
#
# What this file adds on top of the library:
#   1. Mirror, chain, and special-tape no-cut. The library's INTERNAL methods
#      (`_cmd_mode_settings`, `_cmd_advanced_mode_settings`) already build
#      the correct bytes for mirror and chain — but the public `print()`
#      signature doesn't surface them. Special-tape (ESC i K bit 4) is
#      missing entirely. We subclass PTP750W and override the per-page
#      control sequence to thread these through. Upstream PR is the right
#      long-term fix; this lets us ship today.
#   2. Pre-rendered image injection — we hand the library the same
#      descender-corrected landscape PIL image the AirPrint path produces.

# tape_mm -> upstream ptouch Tape class name. Now read from ACTIVE_MODEL
# (see `_tape_class_for_mm` below) so the right Tape class is used for
# whichever printer model is active — PT-P750W has 6/9/12/18/24, P-P950NW
# adds 3.5mm (key 4) and 36mm. The class names are resolved by name via
# getattr(pt, ...) at runtime to keep import-time clean when the upstream
# library isn't installed.


def _import_ptouch():
    """Lazy import of the ptouch library so missing-install errors stay local.

    Raises a clean ImportError-derived RuntimeError if the lib is missing.
    """
    try:
        import ptouch as _pt  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "PT-Direct requires the `ptouch` Python library. "
            "Install the package (`pipx install ptouch-label` or "
            "`uv tool install ptouch-label`) rather than running the file directly.\n"
            f"Original error: {exc}"
        ) from exc
    return _pt


def make_pt_direct_printer_class(
    mirror: bool = False,
    chain: bool = False,
    special_tape: bool = False,
    base_class_name: str | None = None,
):
    """Subclass the active model's printer class and thread mirror/chain/
    special-tape into the per-page control sequence.

    The upstream library's `_build_page_control_sequence` already accepts
    `chain_printing` (via kwarg) but always passes `chain_printing=False`
    from `print()`. It doesn't accept `mirror_print` at all (calls
    `_cmd_mode_settings(auto_cut=...)` with mirror defaulted to False).
    And there's no special-tape bit anywhere. We override:

      - `_cmd_mode_settings` to plumb mirror_print through
      - `_cmd_advanced_mode_settings` to OR in the special-tape bit (bit 4)
      - `_build_page_control_sequence` to pass our chain flag

    Bit positions verified against Brother Raster Command Reference v1.02
    page 33 (ESC i K) and page 33 (ESC i M).

    `base_class_name` defaults to ACTIVE_MODEL.lib_class_name (PTP750W or
    PTP950NW). Both inherit from LabelPrinter, so the same overrides work
    unchanged — the shape of `_cmd_mode_settings` / `_cmd_advanced_mode_settings`
    is defined on the base class and the P900 series uses the same wire
    format for these commands (Brother shares the protocol across the
    line; the differences are pin count, base DPI, and compression default,
    none of which affect our three overrides). Verified by inspection of
    upstream printers.py + printer.py 2026-06-11.
    """
    pt = _import_ptouch()
    cls_name = base_class_name or ACTIVE_MODEL.lib_class_name
    base_cls = getattr(pt, cls_name)

    class _PatchedPrinter(base_cls):
        # Frozen at class-construction time. The flags don't change
        # mid-print job, so a closure-captured constant is simpler than
        # plumbing through every method signature.
        _MIRROR = mirror
        _CHAIN = chain
        _SPECIAL_TAPE = special_tape

        def _cmd_mode_settings(self, auto_cut=True, mirror_print=False):
            return super()._cmd_mode_settings(
                auto_cut=auto_cut,
                mirror_print=self._MIRROR or mirror_print,
            )

        def _cmd_advanced_mode_settings(
            self, half_cut=False, chain_printing=False, high_resolution=False
        ):
            data = super()._cmd_advanced_mode_settings(
                half_cut=half_cut,
                chain_printing=chain_printing,
                high_resolution=high_resolution,
            )
            # ESC i K data is 4 bytes: 1B 69 4B <mode>. Patch bit 4 of the
            # last byte for special-tape no-cut. Verified bit position
            # against Brother Raster Command Reference v1.02, page 33.
            if self._SPECIAL_TAPE:
                patched = bytearray(data)
                patched[-1] |= (1 << 4)
                data = bytes(patched)
            return data

        def _build_page_control_sequence(self, *args, **kwargs):
            # Force chain printing when user passed --chain. The library's
            # public `print()` hardcodes `chain_printing=False`, so this
            # override is the cleanest place to flip it.
            if self._CHAIN:
                kwargs["chain_printing"] = True
            return super()._build_page_control_sequence(*args, **kwargs)

    return _PatchedPrinter


def _tape_class_for_mm(tape_mm: int):
    pt = _import_ptouch()
    name = ACTIVE_MODEL.tape_to_lib.get(tape_mm)
    if name is None:
        raise ValueError(
            f"PT-Direct: unsupported tape width {tape_mm}mm for "
            f"{ACTIVE_MODEL.name}. Supported: {sorted(ACTIVE_MODEL.tape_to_lib)}"
        )
    return getattr(pt, name)


def print_label_pt_direct(
    imgs: "Image.Image | list[Image.Image]",
    tape_width: int,
    host: str,
    copies: int = 1,
    half_cut: bool = False,
    chain: bool = False,
    mirror: bool = False,
    special_tape: bool = False,
    margin_mm: float = 2.0,
    high_resolution: bool = False,
    dry_run: bool = False,
) -> None:
    """Send a label image to the printer via Brother's raster protocol.

    `img` is the same landscape PIL image the AirPrint path uses: width =
    label length px, height = printable_px for the tape. We hand it to
    the library wrapped in a Label, alongside the matching Tape class.

    `imgs` is one image or a list of DIFFERENT images. The list form is
    what makes half-cuts between distinct labels possible: `print_multi`
    emits one job with N pages, terminating each non-final page with 0x0C
    (print WITHOUT feed). Because the full-cutter sits ~23mm downstream of
    the print head, an unfed boundary can only be half-cut — which is
    exactly the desired behaviour. Chaining separate invocations cannot do
    this: each is its own single-page job on a fresh connection, so there
    is no inter-page boundary for the half-cut bit to act on.

    Multi-copy composes with multi-label: the final page list is
    images x copies, in image order.
    """
    pt = _import_ptouch()
    tape_cls = _tape_class_for_mm(tape_width)
    _img_list = imgs if isinstance(imgs, list) else [imgs]

    # Important: the library's `_prepare_image` will re-center any image
    # whose height != tape_config.print_pins. We render at exactly that
    # height already (the existing `create_label_image` produces
    # `printable_px` rows), so re-centering is a no-op and our
    # descender-corrected baseline is preserved.

    if dry_run:
        print(
            f"Would PT-Direct print: tape={tape_width}mm, "
            f"imgs={[i.size for i in _img_list]}, copies={copies}, half_cut={half_cut}, "
            f"chain={chain}, mirror={mirror}, special_tape={special_tape}, "
            f"host={':'.join(map(str, _split_host_port(host)))}"
        )
        return

    PrinterCls = make_pt_direct_printer_class(
        mirror=mirror, chain=chain, special_tape=special_tape
    )
    _h, _p = _split_host_port(host)
    connection = pt.ConnectionNetwork(_h, port=_p, timeout=15.0)
    try:
        printer = PrinterCls(connection, high_resolution=high_resolution)
        labels = [
            pt.Label(im, tape_cls) for im in _img_list for _ in range(copies)
        ]
        if len(labels) == 1:
            printer.print(
                labels[0],
                margin_mm=margin_mm,
                half_cut=half_cut,
                auto_cut=not (chain or special_tape),
            )
        else:
            printer.print_multi(
                labels,
                margin_mm=margin_mm,
                half_cut=half_cut,
            )
    finally:
        try:
            connection.close()
        except Exception:  # pragma: no cover — best-effort cleanup
            pass


def _host_override() -> str | None:
    """An explicit printer address, bypassing CUPS queue lookup entirely.

    `PTOUCH_HOST_<MODEL>` (dashes as underscores, e.g. PTOUCH_HOST_PT_P950NW)
    wins over the generic `PTOUCH_HOST`, so one shell can address both
    printers. Either may be `host:port` to point at a forwarder.

    Why this exists: without it, a CUPS queue is REQUIRED merely as a place to
    read `device-uri` from. For the PT-P950NW that queue is otherwise useless —
    it carries a generic PostScript description the printer cannot interpret,
    and nothing prints through it, because real jobs go straight to port 9100.
    Keeping it alive purely as an address book forced a second, confusing
    printer to sit in the macOS print dialog next to the real one. An explicit
    host removes that reason, so the dead queue can be deleted.
    """
    suffix = ACTIVE_MODEL.name.replace("-", "_").upper()
    return os.environ.get(f"PTOUCH_HOST_{suffix}") or os.environ.get("PTOUCH_HOST") or None


def _get_direct_host_from_device_uri(printer_name: str) -> str | None:
    """Pull a direct host out of CUPS device-uri schemes that already carry
    one (socket://, ipp://, ipps://, lpd://, http://, https://, hp://).

    Used as a fallback when the queue isn't `dnssd://` — happens for the
    PT-P950NW because Brother's wired Ethernet stack doesn't expose
    IPP on port 631, so the queue is wired as `socket://host:9100` rather
    than via mDNS. Returns None when the URI is missing, is `dnssd://`, or
    is otherwise opaque.
    """
    out = subprocess.run(
        ["lpoptions", "-p", printer_name],
        capture_output=True, text=True, errors="replace", check=False,
    ).stdout
    m = re.search(r"device-uri=(\S+)", out)
    if not m:
        return None
    uri = m.group(1)
    if uri.startswith("dnssd://"):
        return None  # caller should use the dnssd path instead
    parsed = urllib.parse.urlparse(uri)
    host = parsed.hostname
    return host or None


def resolve_printer_host(verbose: bool = False) -> str | None:
    """Return the bare hostname/IP of the printer (no port) for PT-Direct.

    Resolution order:
      1. Per-model host cache (populated by a prior IPP probe).
      2. CUPS device-uri direct-host schemes (`socket://`, `ipp://`, etc.).
         This is the PT-P950NW path: Brother's wired Ethernet stack
         doesn't expose IPP, so the queue is wired as `socket://host:9100`
         and we extract the host directly.
      3. dnssd:// queue — resolve via mDNS Bonjour. This is the historical
         PT-P750W path; AirPrint queue is `dnssd://Brother PT-P750W...`.

    PT-Direct itself ignores the port — it always uses TCP/9100.

    `PTOUCH_HOST` / `PTOUCH_HOST_<MODEL>` short-circuit all of it — see
    `_host_override`.
    """
    override = _host_override()
    if override:
        if verbose:
            print(f"[pt-direct] host from environment override: {override}", file=sys.stderr)
        return override

    if ACTIVE_MODEL.proxy_host and _tcp_accepts(ACTIVE_MODEL.proxy_host):
        if verbose:
            print(f"[pt-direct] using print proxy {ACTIVE_MODEL.proxy_host}", file=sys.stderr)
        return ACTIVE_MODEL.proxy_host

    cached = _read_host_cache()
    if cached:
        return cached[0]

    if not _get_direct_host_from_device_uri(ACTIVE_MODEL.cups_queue) and ACTIVE_MODEL.default_host:
        if verbose:
            print(f"[pt-direct] no cache or queue; using default host {ACTIVE_MODEL.default_host}", file=sys.stderr)
        return ACTIVE_MODEL.default_host
    # Direct-host URI takes priority over mDNS — it's already a resolved
    # hostname, so no Bonjour round-trip needed. Cheaper, more reliable
    # across VLAN boundaries (Bonjour multicast doesn't always cross).
    direct = _get_direct_host_from_device_uri(PRINTER_NAME)
    if direct:
        if verbose:
            print(f"[pt-direct] direct host from CUPS device-uri: {direct}", file=sys.stderr)
        return direct
    service = _get_dnssd_service(PRINTER_NAME)
    if not service:
        if verbose:
            print(
                f"[pt-direct] no dnssd/socket device-uri for {PRINTER_NAME}",
                file=sys.stderr,
            )
        return None
    resolved = _resolve_mdns(service)
    if not resolved:
        if verbose:
            print("[pt-direct] mDNS resolve failed", file=sys.stderr)
        return None
    # Cache the IPP resolution for the IPP probe's benefit. PT-Direct
    # itself ignores the port.
    _write_host_cache(*resolved)
    return resolved[0]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _default_preview_path() -> str:
    """A per-user, private location for previews. Never a fixed name in /tmp,
    which another local account could pre-create as a symlink."""
    base = (Path.home() / "Library" / "Caches" / "ptouch-label") if sys.platform == "darwin" \
        else Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ptouch-label"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(base / "ptouch-preview.png")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Print labels to Brother P-touch printers (PT-P750W default; "
            "PT-P950NW supported via --model)."
        ),
        epilog=(
            "Model selection precedence: --model > --printer > PTOUCH_MODEL "
            "env > PTOUCH_PRINTER env > default PT-P750W. The PT-P750W "
            "default and the AirPrint queue name `Brother_PT_P750W` are "
            "unchanged from the original tool — existing scripts work as-is."
        ),
    )
    # Model selection MUST be parsed before --tape's choices are validated,
    # since the set of valid tape widths depends on the model. We do it in
    # two stages: a pre-parse for model, then a full parse with --tape
    # validation against the chosen model.
    parser.add_argument(
        "--model", choices=sorted(MODELS.keys()), default=None,
        help=(
            "Printer model. Overrides PTOUCH_MODEL env and auto-detection "
            "from PTOUCH_PRINTER. Default (no flag, no env): PT-P750W."
        ),
    )
    parser.add_argument(
        "--printer", default=None,
        help=(
            "CUPS queue name. Overrides PTOUCH_PRINTER env. Used by the "
            "AirPrint backend (lpr -P). For PT-Direct, only the resolved "
            "model and host matter — the queue name is informational. "
            "Default: depends on --model (Brother_PT_P750W for PT-P750W, "
            "Brother_PT_P950NW for PT-P950NW, or the config file's cups_queue)."
        ),
    )
    parser.add_argument(
        "text", nargs="*",
        help="Text lines to print (each argument is a separate line)",
    )
    parser.add_argument(
        "--label", action="append", nargs="+", metavar="LINE",
        help="One label, repeatable. Each --label takes that label's lines: "
             "--label \"Router\" \"Rack A\" --label \"Switch\". "
             "Two or more prints ONE job with half-cuts between labels "
             "(the only way to get them; --chain leaves no cut at all). "
             "Implies --pt-direct. Cannot be combined with positional text.",
    )
    parser.add_argument(
        "--pdf", metavar="FILE", default=None,
        help="Also write the label as a PDF at true tape geometry. Unlike the "
             "PNG from --preview, a PDF carries its page size, so it can be "
             "reprinted later from any path that understands paper. Written "
             "automatically alongside every --preview; use this to choose the "
             "path or to emit one without previewing.",
    )
    parser.add_argument(
        "--image", metavar="FILE", default=None,
        help="Print a pre-rendered image instead of text. Scaled to the tape's "
             "printable height, aspect preserved. Surrounding white is trimmed "
             "by default (see --no-trim) because a page from a print dialog is "
             "the full media size and would otherwise feed mostly-blank tape. "
             "Cannot be combined with positional text or --label.",
    )
    parser.add_argument(
        "--no-trim", action="store_true",
        help="With --image, keep the surrounding white instead of cropping to "
             "the dark content. Use when the blank margin is deliberate.",
    )
    parser.add_argument(
        "--tape", "-t", type=int, default=None,
        # No choices=… here — valid widths depend on the active model.
        # Validated against ACTIVE_MODEL.tape_specs after --model is parsed.
        help="Tape width in mm. PT-P750W supports 6/9/12/18/24. PT-P950NW "
             "adds 4 (3.5mm tape) and 36. If unset, probes the printer "
             "via IPP and refuses to print on probe failure (exit 2). Use "
             "--no-probe to skip the probe.",
    )
    parser.add_argument(
        "--no-probe", action="store_true",
        help="Skip the IPP probe entirely. REQUIRES --tape N (no silent default). "
             "Use only when you genuinely want to queue a job for an offline "
             "printer and have physically confirmed the loaded tape width.",
    )
    parser.add_argument(
        "--probe", action="store_true",
        help="Probe the printer for the loaded tape width and exit",
    )
    parser.add_argument(
        "--length", "-l", type=float, default=None,
        help="Label length in mm (default: auto-fit to text)",
    )
    parser.add_argument(
        "--vertical-offset-mm", type=float, default=None,
        help=("Shift content toward higher head pins by this many mm "
              "(negative = the other way). Default: the model's calibrated "
              "value. Find yours with --calibrate-vertical."),
    )
    parser.add_argument(
        "--calibrate-vertical", action="store_true",
        help=("Print a calibration label instead of text: a center line plus "
              "0.5 mm ticks, each whole-mm tick labelled with the value to use "
              "as --vertical-offset-mm if it is the one on the tape's middle."),
    )
    parser.add_argument(
        "--font-size", "-s", type=int, default=None,
        help="Font size in pixels (default: auto-fit to tape)",
    )
    parser.add_argument(
        "--bold", "-b", action="store_true",
        help="Use bold font",
    )
    parser.add_argument(
        "--padding", type=float, default=4.0,
        help="Horizontal padding in mm (default: 4)",
    )
    parser.add_argument(
        "--preview", "-p", action="store_true",
        help="Save and open preview PNG instead of printing",
    )
    parser.add_argument(
        "--preview-path", default=None,
        help=("Path for preview image (default: ptouch-preview.png in a private "
              "per-user cache directory, ~/Library/Caches/ptouch-label on macOS, "
              "~/.cache/ptouch-label elsewhere)"),
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show lpr command without printing",
    )

    # PT-Direct backend (TCP/9100, via upstream ptouch library) ----------
    parser.add_argument(
        "--pt-direct", action="store_true",
        help="Use the PT-Direct backend (TCP/9100) instead of AirPrint. "
             "Enables --half-cut, --chain, --copies, --mirror, --special-tape. "
             "Auto-enabled when any of those flags are passed.",
    )
    parser.add_argument(
        "--copies", "-c", type=int, default=1,
        help="Number of copies (PT-Direct only). 1..99. Default: 1.",
    )
    parser.add_argument(
        "--half-cut", action="store_true",
        help="Half-cut between copies (slices top layer, leaves backing). "
             "PT-Direct only. Pairs naturally with --copies N.",
    )
    parser.add_argument(
        "--chain", action="store_true",
        help="Chain printing: skip the final feed+cut after the last label "
             "so the next print continues without a leader feed. PT-Direct only.",
    )
    parser.add_argument(
        "--mirror", action="store_true",
        help="Mirror printing (for transparent / iron-on tape). PT-Direct only.",
    )
    parser.add_argument(
        "--special-tape", action="store_true",
        help="No-cut mode for non-laminated decorative tape. PT-Direct only.",
    )
    parser.add_argument(
        "--high-resolution",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="360 DPI horizontal mode (PT-Direct only; ignored on AirPrint). "
             "DEFAULT ON — verified on real tape 2026-05-14 to be visibly "
             "sharper. Pass --no-high-resolution to opt out if you need to "
             "save print time on a large batch.",
    )

    args = parser.parse_args()
    if args.preview_path is None:
        args.preview_path = _default_preview_path()

    # Resolve the active model FIRST. Order: --model > --printer auto-detect
    # > leave whatever env vars selected at module-load. Once selected, the
    # module globals (DPI, DOTS_PER_MM, TAPE_SPECS, PRINTER_NAME) are rebound
    # and the rest of main() sees the right values.
    if args.model:
        _select_model(args.model, cups_queue_override=args.printer)
    elif args.printer:
        # User passed --printer without --model. If the queue name maps to a
        # known model, switch; otherwise just override the cups_queue field
        # and keep whatever model was env-selected.
        if args.printer in _QUEUE_TO_MODEL:
            _select_model(_QUEUE_TO_MODEL[args.printer], cups_queue_override=args.printer)
        else:
            global PRINTER_NAME
            PRINTER_NAME = args.printer

    # Validate --tape against the resolved model's supported widths.
    if args.tape is not None and args.tape not in TAPE_SPECS:
        parser.error(
            f"--tape {args.tape}mm not supported by {ACTIVE_MODEL.name}. "
            f"Supported: {sorted(TAPE_SPECS)}"
        )

    # Auto-enable --pt-direct when any of its capability flags are set.
    # Saves the user one keystroke and avoids cryptic "AirPrint can't half-cut"
    # failures. NOTE: high_resolution IS in this set and defaults to True,
    # which intentionally forces every call onto PT-Direct (360 DPI). The
    # AirPrint backend (180×180) is the explicit opt-out via
    # --no-high-resolution. Reason: hi-res is visibly sharper on real tape
    # (verified 2026-05-14, re-confirmed 2026-05-16 after a fuzzy print
    # was caught — the previous default of AirPrint produced labels that
    # looked acceptable until placed next to a hi-res reprint).
    pt_direct_flag_set = (
        args.high_resolution or
        args.copies > 1 or args.half_cut or args.chain or
        args.mirror or args.special_tape
    )
    # Visible flags (excluding the default-True high_resolution) — used to
    # decide whether to emit the "auto-enabled" stderr note. Hi-res alone is
    # the default and doesn't deserve narration on every call; only narrate
    # when the user passed an explicit PT-Direct-only flag.
    visible_pt_direct_flag = (
        args.copies > 1 or args.half_cut or args.chain or
        args.mirror or args.special_tape
    )
    if pt_direct_flag_set and not args.pt_direct:
        args.pt_direct = True
        if visible_pt_direct_flag and (args.text or args.probe):
            print(
                "Note: --pt-direct auto-enabled (one of --copies/--half-cut/"
                "--chain/--mirror/--special-tape was passed).",
                file=sys.stderr,
            )

    # Validate PT-Direct flag interactions.
    if args.copies < 1 or args.copies > 99:
        parser.error(f"--copies must be 1..99, got {args.copies}")
    if not args.pt_direct and (
        args.half_cut or args.chain or args.mirror or args.special_tape
        or args.copies > 1
    ):
        # Defense in depth — the auto-enable above should have set --pt-direct,
        # but if someone passed e.g. `--copies 1 --half-cut` explicitly,
        # half_cut alone triggered auto-enable. This branch should be
        # unreachable; keep it as a safety net. (high_resolution intentionally
        # omitted — it's default-True and applies only when PT-Direct is
        # otherwise active.)
        parser.error(
            "--half-cut/--chain/--mirror/--special-tape and --copies > 1 "
            "require --pt-direct (the AirPrint queue cannot express these "
            "options)."
        )

    if args.probe:
        width = probe_loaded_tape(verbose=True)
        if width is None:
            print("Could not detect loaded tape width", file=sys.stderr)
            sys.exit(1)
        print(f"{width}")
        return

    if args.text and args.label:
        parser.error(
            "use positional text OR --label, not both. Positional args are the "
            "lines of a single label; --label starts a new label each time."
        )
    if args.image and (args.text or args.label):
        parser.error(
            "--image prints a pre-rendered file; it cannot be combined with "
            "positional text or --label."
        )
    if args.calibrate_vertical and (args.text or args.label or args.image):
        parser.error("--calibrate-vertical prints its own label; drop the text/--label/--image.")
    if not args.text and not args.label and not args.image and not args.calibrate_vertical:
        parser.error(
            "text required (or --image FILE, --calibrate-vertical, or --probe to query the printer)"
        )

    # Label groups: each entry is the list of lines for one physical label.
    # With --image there are no text lines; a single placeholder group keeps
    # the multi-label bookkeeping below working unchanged.
    label_groups = [[]] if (args.image or args.calibrate_vertical) else (args.label if args.label else [args.text])
    multi_label = len(label_groups) > 1

    if multi_label:
        # Half-cuts between distinct labels only exist inside a single
        # multi-page job, which is PT-Direct's print_multi. AirPrint cannot
        # express it, and --chain across separate jobs produces no cut at all.
        args.pt_direct = True
        if not args.half_cut:
            args.half_cut = True
        if args.chain:
            parser.error(
                "--chain is redundant with multiple --label: one job already "
                "keeps the labels on a single strip, and adds half-cuts "
                "between them, which --chain cannot do."
            )

    # Determine effective tape width.
    #
    # Decision tree (fail-closed when printer is unreachable):
    #   --no-probe alone           → ERROR. --no-probe requires --tape N.
    #                                Silent 24mm default has caused wrong-size
    #                                prints (verified 2026-05-05).
    #   --no-probe --tape N        → expert offline mode: skip probe AND skip
    #                                alive-check. Trust user, queue for printer.
    #   default (no --no-probe)    → probe required. Probe doubles as
    #                                alive-check. If probe fails, REFUSE TO
    #                                PRINT regardless of whether --tape is
    #                                set, and tell the user to turn the
    #                                printer on. Even with --tape N, queueing
    #                                a job to an offline printer is a footgun
    #                                — the user usually wants to know NOW
    #                                that the printer is off.
    #   --tape N alone (probe ok)  → use --tape, warn if probe disagrees.
    #   no flags, probe ok         → use probed value.

    # Block the silent-default trap.
    if args.no_probe and args.tape is None:
        print(
            "ERROR: --no-probe requires --tape N.\n"
            "  Silent 24mm default has caused wrong-size prints in the\n"
            "  past. Either drop --no-probe (recommended — wake the printer\n"
            "  and let the probe detect the loaded tape) or pass --tape N\n"
            "  after physically reading the tape cassette label.",
            file=sys.stderr,
        )
        sys.exit(2)

    probed = None
    if not args.no_probe:
        probed = probe_loaded_tape()

    # If probe was attempted and failed, the printer is off / unreachable.
    # Refuse to submit even if --tape is set — fail-closed, ask user to wake
    # the printer. Escape hatch: --no-probe --tape N (skips this check).
    if not args.no_probe and probed is None and not (
        _host_override() or ACTIVE_MODEL.default_host or ACTIVE_MODEL.proxy_host
        or _read_host_cache() or _cups_queue_exists(PRINTER_NAME)
    ):
        print(
            f"ERROR: no address configured for the {ACTIVE_MODEL.name}.\n"
            "  Tell ptouch-label where the printer is, either way:\n"
            "    export PTOUCH_HOST=<hostname-or-ip>\n"
            f"    or host = \"...\" under [printers.{ACTIVE_MODEL.name}] in {_config_path()}",
            file=sys.stderr,
        )
        sys.exit(2)
    if not args.no_probe and probed is None:
        print(
            "ERROR: printer is off or unreachable.\n"
            "  Probe failed — could not confirm loaded tape width.\n"
            "  Please turn the printer on and try again.\n"
            "\n"
            "  Diagnostics:\n"
            f"    lpstat -p {PRINTER_NAME}\n"
            f"    ping -c 1 -t 1 BRW...local   # printer mDNS hostname\n"
            "\n"
            "  Last-resort offline queue (skips this check, requires you\n"
            "  to physically confirm the loaded tape):\n"
            "    ptouch-label --no-probe --tape N \"text\"",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.tape is not None:
        tape_width = args.tape
        if probed is not None and probed != tape_width:
            print(
                f"WARNING: --tape {tape_width}mm does not match loaded "
                f"{probed}mm tape — printer will reject the job at print time",
                file=sys.stderr,
            )
    elif probed is not None:
        tape_width = probed
        print(f"Detected loaded tape: {tape_width}mm", file=sys.stderr)
    else:
        # --no-probe and --tape both set (the only path that reaches here)
        tape_width = args.tape

    if args.calibrate_vertical:
        rendered = [create_calibration_image(tape_width, length_mm=args.length or 45.0)]
    elif args.image:
        rendered = [
            load_label_image(
                args.image,
                tape_width=tape_width,
                trim=not args.no_trim,
                length_mm=args.length,
            )
        ]
    else:
        rendered = [
            create_label_image(
                lines,
                tape_width=tape_width,
                length_mm=args.length,
                font_size=args.font_size,
                bold=args.bold,
                padding_mm=args.padding,
            )
            for lines in label_groups
        ]
    offset_mm = (args.vertical_offset_mm if args.vertical_offset_mm is not None
                 else ACTIVE_MODEL.vertical_offset_mm)
    if offset_mm:
        shifted = []
        dy = int(round(offset_mm * DOTS_PER_MM))
        for img, length in rendered:
            # The calibration moves the printable band; rows pushed past its
            # edge are physically unreachable. Text labels leave margins and
            # never get there, but a full-bleed page from the print dialog
            # does. Rather than clip it, fit the whole label into the rows
            # that remain reachable, keeping aspect, so dialog prints come
            # out centered and complete (slightly shorter, never cut off).
            bbox = ImageOps.invert(img.convert("L")).getbbox()
            h = img.height
            clips = bbox and ((dy > 0 and bbox[3] > h - dy) or (dy < 0 and bbox[1] < -dy))
            if clips:
                # The tape's true middle sits |dy| rows from the band's own
                # center, so the largest region centered on the tape that the
                # band can still reach is h - 2|dy| rows. Fit to that and
                # center it pre-shift; the shift then lands it on the middle.
                usable = max(1, h - 2 * abs(dy))
                scale = usable / h
                fitted = img.resize((max(1, int(round(img.width * scale))), usable), Image.LANCZOS)
                canvas = Image.new(img.mode, (fitted.width, h), 255)
                canvas.paste(fitted, (0, (h - usable) // 2))
                img = canvas
                length = img.width / DOTS_PER_MM
                print(f"Vertical offset would clip this label; fitted it to the reachable band "
                      f"({usable}/{h} rows, {scale:.0%})", file=sys.stderr)
            shifted.append((shift_vertical(img, offset_mm), length))
        rendered = shifted
        print(f"Vertical offset: {offset_mm:+.2f}mm ({int(round(offset_mm * DOTS_PER_MM)):+d}px)", file=sys.stderr)
    imgs = [img for img, _ in rendered]

    tape_spec = TAPE_SPECS[tape_width]
    for idx, (img, label_length_mm) in enumerate(rendered, start=1):
        prefix = f"Label {idx}/{len(rendered)}" if multi_label else "Label"
        print(
            f"{prefix}: {tape_width}mm tape, "
            f"{label_length_mm:.1f}mm long, "
            f"{img.width}x{img.height}px "
            f"({tape_spec[1]}px printable height)"
        )
    if multi_label:
        total = sum(l for _, l in rendered)
        print(
            f"{len(rendered)} labels, one job, half-cut between "
            f"(~{total:.1f}mm content + one leader feed)"
        )

    img, label_length_mm = rendered[0]

    if args.pdf and not args.preview:
        # --pdf on its own: emit the reprintable artifact and stop, without
        # opening anything. This is the shape an unattended agent wants.
        for idx, (im, _) in enumerate(rendered, start=1):
            out = args.pdf if len(rendered) == 1 else str(
                Path(args.pdf).with_name(f"{Path(args.pdf).stem}-{idx}.pdf")
            )
            w_mm, h_mm = save_label_pdf(im, out, tape_width)
            print(f"Reprintable PDF: {out}  ({w_mm:.1f} x {h_mm:.1f} mm)")
        return

    if args.preview:
        # Save landscape (readable) version for preview. Multi-label gets one
        # file per label, numbered, so they can be reviewed independently.
        preview_paths = []
        for idx, (im, _) in enumerate(rendered, start=1):
            if multi_label:
                p = Path(args.preview_path)
                out = str(p.with_name(f"{p.stem}-{idx}{p.suffix}"))
            else:
                out = args.preview_path
            im.save(out, dpi=(DPI, DPI))
            preview_paths.append(out)
            print(f"Preview saved: {out}")

            # Always emit a reprintable sibling. A PNG is a picture of a label;
            # a PDF is a label, because it carries the page size. Without this
            # the artifact is a dead end for anyone who wants another copy
            # later and does not want to go back through an agent.
            pdf_out = args.pdf if (args.pdf and not multi_label) else str(
                Path(out).with_suffix(".pdf")
            )
            try:
                w_mm, h_mm = save_label_pdf(im, pdf_out, tape_width)
                print(f"Reprintable PDF: {pdf_out}  ({w_mm:.1f} x {h_mm:.1f} mm)")
                print(
                    f"  reprint with: ptouch-label --model {ACTIVE_MODEL.name} "
                    f"--image {shlex.quote(out)}"
                )
            except Exception as exc:
                print(f"  (could not write PDF: {exc})", file=sys.stderr)
        subprocess.run(["open", *preview_paths])
    elif args.pt_direct:
        host = resolve_printer_host(verbose=True)
        if host is None:
            print(
                "ERROR: could not resolve printer host for PT-Direct.\n"
                "  Set the printer's address once, either way:\n"
                f"    export PTOUCH_HOST=<hostname-or-ip>\n"
                f"    or host = \"...\" under [printers.{ACTIVE_MODEL.name}] in {_config_path()}\n"
                "  If it is set, check that the printer is on and reachable.",
                file=sys.stderr,
            )
            sys.exit(2)
        print_label_pt_direct(
            imgs,
            tape_width=tape_width,
            host=host,
            copies=args.copies,
            half_cut=args.half_cut,
            chain=args.chain,
            mirror=args.mirror,
            special_tape=args.special_tape,
            high_resolution=args.high_resolution,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            n_pages = len(imgs) * args.copies
            desc = f"{len(imgs)} labels" if multi_label else (
                f"{args.copies} cop{'y' if args.copies == 1 else 'ies'}"
            )
            extra = " with half-cuts between" if n_pages > 1 else ""
            print(f"Sent to {':'.join(map(str, _split_host_port(host)))} (PT-Direct, {desc}{extra})")
    else:
        print_label(img, tape_width, label_length_mm, dry_run=args.dry_run)
        if not args.dry_run:
            print(f"Sent to {PRINTER_NAME}")


if __name__ == "__main__":
    main()
