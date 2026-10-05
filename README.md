# ptouch-label

A command-line tool and macOS print-dialog bridge for Brother P-touch network label printers (PT-P750W, PT-P950NW), built so that scripts, AI agents and people can all print labels that come out the right size and centered.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What it does

`ptouch-label` renders text or an image as a label and sends it to the printer over Brother's raster protocol on TCP port 9100. It asks the printer which tape cassette is loaded before every job, and sizes the label to that tape. It supports the printer features Brother's macOS driver hides: half-cut between labels, several different labels in one tape feed, chain printing, mirroring, and 360 or 720 dpi output.

It also ships an IPP Everywhere front end, so any macOS app's **File > Print** can print to the label printer. You do not pick a tape width. Whatever you lay out is trimmed and scaled to the cassette that is in the printer.

## Why it exists

Brother's label printers are good hardware with awkward software. The PT-P950NW has no AirPrint and no current macOS driver. The PT-P750W's AirPrint queue cannot half-cut. Neither driver knows which tape is loaded, so a label laid out for 24 mm tape prints happily onto 9 mm tape and wastes the cassette.

This project started as a way for an AI agent to print a label from one shell command without wasting tape, and grew into a tool a person can use from Preview too. The low-level protocol work is done by the excellent [`ptouch`](https://github.com/nbuchwitz/ptouch) library by Nicolai Buchwitz. This project adds the tape probing, layout, calibration, the macOS front end and the safety rules around them.

## Install

```bash
pipx install git+https://github.com/johntrandall/ptouch-label
```

`uv tool install git+https://github.com/johntrandall/ptouch-label` works too. Requires Python 3.11 or newer. The print-dialog front end additionally needs macOS and Homebrew's `poppler`:

```bash
brew install poppler
```

## Quick start

Tell the tool where your printer is, once:

```bash
mkdir -p ~/.config/ptouch-label
cat > ~/.config/ptouch-label/config.toml <<'TOML'
default_model = "PT-P950NW"

[printers.PT-P950NW]
host = "printer.example.lan"     # or the printer's IP address
TOML
ptouch-label --probe
```

```text
12
```

That is the loaded tape width in millimeters. Now print a label:

```bash
ptouch-label "Server Room" "Rack A-1"
```

```text
Detected loaded tape: 12mm
Label: 12mm tape, 29.5mm long, 418x150px (150px printable height)
Sent to printer.example.lan:9100 (PT-Direct, 1 copy)
```

Check a layout without wasting tape:

```bash
ptouch-label --preview "Check this first"
```

## How it works

**Every job starts with a probe.** The tool reads the loaded tape width from the printer before rendering. It tries IPP, then the raster status command, then the printer's built-in web page, because different models answer different ones. The PT-P950NW, for example, never answers the raster status request, so its width comes from the web page.

**The probe is the default, and guesses are loud.** With no flags, the label is sized to the probed width. An explicit `--tape` that disagrees with the probe prints a warning. `--no-probe` is refused unless you also pass `--tape`, so a forgotten flag can never fall back to a silent default width.

**Labels are rendered at the head's native resolution.** Text is centered using the font's real ink bounds, so descenders do not clip on narrow tape. Output is 1-bit at 360 dpi, or 720 dpi in high-resolution mode, the default.

**Each printer can be calibrated.** Brother's published pin layout centers every laminated tape about half a millimeter off the print head's center, and each unit's tape path adds its own bias. `--calibrate-vertical` prints a ruler label. Photograph it, read which tick sits on the tape's middle, and store that value. After that, every label is centered, whatever the width. One printer measured +1.45 mm, and the same value held on 9, 12 and 24 mm tape.

## Use cases

### Print from scripts and agents

One command per label, and nothing to click:

```bash
ptouch-label --bold "DANGER"
ptouch-label --copies 12 --half-cut "POWER ONLY"
ptouch-label --label "Router" "UDM Pro" --label "Switch" "USW-24" --label "NAS"
```

Each `--label` is a separate label in one tape feed, half-cut between them. That saves the leading margin Brother feeds before every separate job.

### Print from any macOS app

The `ptouch-ippeve` front end runs Apple's own `ippeveprinter` as a small background service. It presents the printer to macOS as a driverless IPP Everywhere printer with a single paper size, "Label tape (auto-scale to loaded cartridge)". Each job arrives as a PDF. The front end probes the tape, trims the page to its content, scales it to the loaded cassette, and prints it through `ptouch-label`. The page is laid out 36 mm tall, the widest tape, so content is always scaled down and stays sharp.

Setup takes about five minutes. See [`docs/print-dialog.md`](docs/print-dialog.md). It ships local-only: not advertised, web forms off. It still listens on every interface, so the guide shows how to block or share it deliberately.

### Reach a printer the Mac cannot talk to directly

macOS Local Network privacy can block a script from connecting to devices on the Mac's own subnet, unless the app it runs under holds the permission. Routed destinations are exempt, as is a relay on another host. Set `PTOUCH_HOST` to any `host:port` that forwards to the printer's port 9100, for example a one-line `socat` container on a server, and the tool prints through it.

## Configuration

Settings for your printers live in `~/.config/ptouch-label/config.toml`. The file is optional, and environment variables override it.

```toml
default_model = "PT-P950NW"

[printers.PT-P950NW]
host = "label-printer.example.lan"   # hostname or IP address
proxy = "relay.example.lan:9101"     # optional relay to the printer's port 9100
vertical_offset_mm = 1.45            # from --calibrate-vertical
cups_queue = "Office-Labels"         # optional CUPS queue name
queue_aliases = ["Labels"]           # optional extra names --printer accepts

[printers.PT-P750W]
host = "192.0.2.40"            # example address (RFC 5737)
```

| Environment variable | Overrides |
|---|---|
| `PTOUCH_MODEL` | `default_model` |
| `PTOUCH_HOST`, or `PTOUCH_HOST_PT_P950NW` per model | `host` |
| `PTOUCH_PRINTER` | the CUPS queue, and picks the model from it |
| `PTOUCH_LABEL_CONFIG` | the config file's location |

Run `ptouch-label --help` for every flag.

## Status / Roadmap

Beta. The tool is used daily on a PT-P950NW and a PT-P750W, but it has one household of users and has only been tested on those two models, on macOS. The CLI should work on Linux; it has not been tested there.

- **Next:** publish to PyPI, so `pipx install ptouch-label` works.
- **Next:** speed up print-dialog jobs. They currently take about 90 seconds, mostly in PDF inspection and the tape probe.
- **Next:** stop full-height dialog content from being shrunk more than needed by the calibration fit.
- **Wanted:** testing and pin tables for other P-touch network models, such as the PT-P900W and PT-P910BT.

## Contributing

File issues for bugs and for printer models you would like supported. Include the model, the tape width, and the exact command and output.

Pull requests are welcome. For a new printer model, include the pin configuration source and a photo of a `--calibrate-vertical` print.

## License

MIT, see [LICENSE](LICENSE). The [`ptouch`](https://github.com/nbuchwitz/ptouch) library it depends on is licensed separately under the LGPL-2.1-or-later.
