# Printing from the macOS print dialog

This guide sets up the `ptouch-ippeve` front end, so that any macOS app can print to a Brother P-touch label printer from **File > Print**. You do not choose a tape width in the dialog. Whatever you lay out is trimmed and scaled to the cassette that is loaded.

It takes about five minutes. It was built and tested with a PT-P950NW on macOS 15.

## How it works

Apple ships `ippeveprinter`, a small IPP Everywhere printer server, with every Mac. This front end runs it in the background with a configuration that offers one paper size, "Label tape (auto-scale to loaded cartridge)", 300 x 36 mm.

When you print, the job arrives as a PDF and `ptouch-ippeve-print` handles it:

1. It probes the printer for the loaded tape width.
2. It rasterises the page at 360 dpi with `pdftoppm`.
3. It prints it with `ptouch-label --image`, which trims the white margins and scales the content to the loaded tape.

The page is laid out 36 mm tall, the widest tape, so content is always scaled down and stays sharp.

## Before you start

- `ptouch-label` is installed and can print. Check with `ptouch-label --probe`, which should print the loaded tape width.
- Your printer's address is in `~/.config/ptouch-label/config.toml` or in `PTOUCH_HOST`. The background service does not see your shell's environment, so prefer the config file.
- Homebrew's `poppler` is installed: `brew install poppler`.
- You have a checkout of this repository. The steps below call its path `REPO`.

## Steps

### 1. Create the spool directory

```bash
mkdir -p -m 700 ~/.local/state/ptouch-ippeve ~/.local/state/ptouch-ippeve/spool
```

The front end refuses every job if this directory is missing, and CUPS then pauses the queue.

### 2. Install the background service

```bash
REPO=~/src/ptouch-label          # wherever you cloned it
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" \
  "$REPO/contrib/print-dialog/ptouch-ippeve.plist.template" \
  > ~/Library/LaunchAgents/local.ptouch-ippeve.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.ptouch-ippeve.plist
```

If your printer is a PT-P750W, change `PTOUCH_IPPEVE_MODEL` in the plist before loading it.

Check that it is listening:

```bash
lsof -nP -iTCP:8631 -sTCP:LISTEN
```

### 3. Add the printer queue

```bash
lpadmin -p Label-Printer -E -v ipp://localhost:8631/ipp/print -m everywhere -D "Label printer"
lpadmin -p Label-Printer -P "$REPO/contrib/print-dialog/label-tape-auto.ppd"
cupsenable Label-Printer && cupsaccept Label-Printer
```

The second command replaces the description CUPS generates with one that names the paper size and passes `cupstestppd`.

### 4. Print a test page

Open any PDF or image in Preview, press Cmd-P, choose **Label-Printer**, and print. The paper size is already selected because it is the only one.

## Security: who can print

`ippeveprinter` accepts jobs without a login unless started with `-A`, and it listens on port 8631 on every network interface. The template ships **local-only**: it is not advertised over Bonjour (`-r off`) and its media and supplies web forms are off (`--no-web-forms`). Other machines can still send it jobs if they know your Mac's address.

A job can use up tape, and its PDF is opened by `pdfinfo` and `pdftoppm` on your Mac. The job script limits the damage:

- It accepts only PDFs.
- It refuses pages that are not label-shaped: at most about 1 m long and 106 mm tall, checked against every page box.
- It caps CPU time at 60 seconds and output files at 200 MB per job.
- It keeps its log and temporary files private to your account.
- It prints only after confirming the loaded tape.

**To share it with other Macs on purpose,** remove `-r off` from `ProgramArguments`. Only do this on a network you trust.

**To block other machines entirely,** use the packet filter. The macOS Application Firewall works per app, not per port, and allows Apple's own `ippeveprinter` by default, so it cannot do this. One way, as root:

```bash
echo 'block in quick on ! lo0 proto tcp to any port 8631' > /etc/pf.anchors/ptouch-ippeve
# add to /etc/pf.conf:  anchor "ptouch-ippeve"  and  load anchor "ptouch-ippeve" from "/etc/pf.anchors/ptouch-ippeve"
pfctl -f /etc/pf.conf -e
```

Or start the service only while you need it, with `launchctl bootstrap` and `bootout`.

## Keeping the dialog in step with the cassette

`contrib/print-dialog/ptouch-sync-ready-media` is for a different setup: OpenPrinting's ghostscript printer application running in a Docker container. It reads the loaded tape from the printer's web page and sets that container's "ready media", so its print dialog offers only the loaded width. The `ippeveprinter` front end above does not need it, because it always scales to the loaded tape.

```bash
contrib/print-dialog/ptouch-sync-ready-media --container <name> --printer <queue> --host <printer-address>
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Queue shows "paused" | A job failed. Read `~/.local/state/ptouch-ippeve/ptouch-ippeve.log`, fix the cause, then run `cupsenable Label-Printer`. |
| "Unable to create print file" in `~/.local/state/ptouch-ippeve/stderr.log` | The spool directory is missing. Repeat step 1. |
| "tape probe failed — refusing to print" in the log | The printer is asleep, off or unreachable. Run `ptouch-label --probe` in a terminal to see why. |
| The label prints but is off-center | Calibrate the printer once with `ptouch-label --calibrate-vertical`, and store `vertical_offset_mm` in the config file. |
| Each job takes over a minute | Expected for now. PDF inspection, the tape probe and rasterising each take several seconds. |

## Removing it

```bash
launchctl bootout gui/$(id -u)/local.ptouch-ippeve
rm ~/Library/LaunchAgents/local.ptouch-ippeve.plist
lpadmin -x Label-Printer
```
