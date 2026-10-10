# Tern Session Media

A native, session-scoped media gallery for [Stencil Tern](https://stencil.so/tern/), with optional OMP capture. No Tern fork, web server, or browser gallery. Independent of Flow and the Android companion.

## Install

Requires Tern with native plugin blocks and the window-plugin API, Python 3.10+, and OMP for automatic conversation capture. The native interface is verified against Tern 0.7.0.

```sh
git clone https://github.com/GowangInc/tern-media.git
cd tern-media
sh install.sh
```

Keep the checkout in place: installation links the Tern plugin and `~/.omp/agent/extensions/tern-media.ts` to it. Start a new OMP session after installation to load the extension. The installer refuses to replace an unrelated extension or a link to another checkout. For remote Tern panes, install the package on the pane's host too; each host keeps its own media store.

## Gallery

In Tern's command palette, choose **Media: Open session gallery**. It opens beside the focused terminal. Drag the native split boundary to resize it. **Media: Float gallery** and **Media: Dock gallery right** use Tern's native floating and docking controls.

- **Current** follows the owning conversation; **Pin session** freezes that conversation for later comparison.
- **Workspace** shows conversations from the same Tern session. **All archives** is an explicit, host-local view, not an automatic fallback.
- **Archived sessions** lets you reopen an indexed conversation after its terminal closes.
- Filter by media kind, or search filenames and tags. Each item shows its timestamp, provenance, conversation, and retained version.
- Select two images, assign **A** and **B**, then choose **Compare** for side-by-side retained versions. Narrow panes stack the comparison.
- **Open retained original** opens the immutable saved copy, not the mutable source file.
- **Import file** accepts a literal host-local path, including spaces. **Watch directory** explicitly associates a nonrecursive directory with this conversation. Stop a watch from the gallery.

While the gallery is focused:

| Key | Action |
| --- | --- |
| Arrows | Select item |
| A / B / C | Assign comparison slots / toggle comparison |
| T | Edit selected item's comma-separated tags |
| / | Search names and tags |
| I / W / O | Import file / watch directory / open saved original |
| P / H | Pin or follow / show archived sessions |
| S / K | Cycle scope / media kind |
| R | Refresh |
| [ / ] | Previous / next page |
| Enter / Escape | Apply / cancel an edit |

Click the same controls if you prefer the mouse. The gallery inherits Tern's type and colour palette.

## Collecting media

Inside Tern, the OMP extension captures image attachments, successful tool-returned media, and clearly named existing local media paths. Image reads retain the original file once rather than creating a second item from the tool's preview. Child-agent media belongs to its parent conversation. Native Tern media file opens and drops are also registered; opening a retained copy from the gallery does not import it again.

Explicit OMP commands:

```text
/media open
/media-add /absolute/path/Before screenshot.png
/media-tag 12 baseline, approved
/media-watch /absolute/path/output directory
/media-backfill
/media-backfill /absolute/path/session.jsonl
```

Backfill is explicit, never automatic: it imports retained image blocks and existing referenced local media from the selected transcript. Missing references are reported, remote URLs are not downloaded, and transcript bodies are not stored in the media database.

There is no generic Tern event for every possible media transfer. Arbitrary files created by unrelated programs are not indexed unless opened, explicitly imported, or found in a directory you chose to watch.

## Retention and limits

The backend stores original bytes by SHA-256. Repeated observations of identical bytes at the same path do not add duplicate versions; changed bytes at that path produce a new version while the older copy remains available. Different conversation scopes stay separate.

Only observed versions can be retained. Rapid intermediate overwrites between watch scans cannot be recovered. Watched directories are nonrecursive; hidden entries, symlinks, and nonregular files are excluded. Images, video, audio, and PDF files are supported, with a 64 MiB original-file limit and bounded inline payload/backfill processing.

Image/video thumbnails use the installed platform decoder (`sips` on macOS, or `ffmpeg`). Missing codecs or preview tools do not discard the original: the gallery shows a type card and offers the saved file. Saved audio, video, and PDFs use Tern's normal file handling, which may delegate unsupported formats to an external viewer. The gallery has no embedded player.

## Local data

Source code and media data are separate. Default store:

- macOS: `~/Library/Application Support/Tern/media`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/tern-media`
- Override: `TERN_MEDIA_STATE_DIR`

The store contains SQLite metadata, immutable originals, and cached previews. It uses owner-only directories/files. No network service is started. The extension is inert outside Tern (`TERN_PANE` must be present). Publication excludes media, databases, transcripts, credentials, and verification screenshots. Do not put your media store inside the source checkout.

## Checks

```sh
python3 selftest.py
bun omp-extension.ts --self-check
```

The backend check exercises real JSON subprocess requests, retained bytes/versioning, conversation/workspace isolation, imports, watches, transcript/blob backfill, and input boundaries. Bun is only needed for the standalone extension check, not for the Python store.

Native verification uses an isolated Tern configuration/daemon/store and generated fixture images, never live conversation data. It exercises gallery opening, comparison, tagging/search, import/watch, pin persistence, typed saved-file opening, floating/docking, and narrow/light/dark layouts. An actual OMP image read verifies the capture path separately.

## License and acknowledgements

MIT; see `LICENSE`. Window lifecycle patterns follow the separately licensed Flow plugin. Tern belongs to Stencil; this is an independent plugin, not an official Stencil component.
