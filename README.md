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
| Escape (outside an edit) | Leave comparison/archive view, or close the gallery when neither is open |

Click the same controls if you prefer the mouse. The gallery inherits Tern's type and colour palette.

## Collecting media

Inside Tern, the OMP extension captures image attachments, successful tool-returned media, and clearly named existing local media paths. Image reads retain the original file once rather than creating a second item from the tool's preview. Child-agent media belongs to its parent conversation. Native Tern media file opens and drops are also registered; opening a retained copy from the gallery does not import it again.

Opening or refreshing the gallery discovers existing media from its owning OMP conversation. New, resumed, switched, and branched main sessions also import their existing transcript through the OMP extension. An unchanged, fully imported transcript is skipped on later session events; unresolved media remains eligible for a later import. A child session never imports its parent's history or changes the pane binding.

Discovery follows the owning pane's live OMP process, using its open transcript or a verified terminal breadcrumb when idle. An older breadcrumb is accepted only when it matches that process's exact absolute resume argument. Ambiguous or unproven pointers never select another conversation.

Explicit OMP commands:

```text
/media open
/media-add /absolute/path/Before screenshot.png
/media-tag 12 baseline, approved
/media-watch /absolute/path/output directory
/media-backfill
/media-backfill /absolute/path/session.jsonl
```

`/media-backfill` explicitly imports the current or named transcript when needed. Automatic and explicit imports retain image blocks and existing referenced local media. Missing references are reported, remote URLs are not downloaded, and transcript bodies are not stored in the media database.

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

Schema creation and migration run once inside a serialized transaction. Normal opens do not rerun schema writes or change the journal mode; existing databases keep their mode, and new databases use SQLite's defaults.

## Runtime troubleshooting

If Tern reports `focus exceeded 50 ms`, `title exceeded 50 ms`, or another plugin hook budget error, it disables the affected handler until **Reload plugins** in the command palette. `tern plugin list` reporting `ready` describes package loading, not whether every window handler is still enabled.

An intermittent hook timeout was observed on Tern 0.7.0 after the initial isolated verification. Its cause remains unresolved: a tiny diagnostic handler also timed out when loaded alongside Media, but succeeded in a separate minimal window. Reloading restores disabled handlers; it is recovery, not a permanent fix. The safety limit is unchanged.

## Checks

```sh
python3 selftest.py
bun omp-extension.ts --self-check
```

The backend check exercises real JSON subprocess requests, retained bytes/versioning, conversation/workspace isolation, imports, watches, transcript/blob backfill, live-process and cold-resume discovery, stale-pointer rejection, and input boundaries. Bun checks automatic historical import, delayed-media availability, and binding order in an isolated store and child-process environment. Check fixtures are retained in temporary directories printed by the commands. Bun is only needed for the standalone extension check, not for the Python store.

Native checks isolate Tern configuration, daemon, media/Flow stores, and OMP state. Generated fixture images exercise gallery opening, existing-session import, Escape closure and persistence, comparison, tagging/search, import/watch, pin persistence, typed saved-file opening, floating/docking, and narrow/light/dark layouts. An actual OMP image read verifies the capture path separately.

## License and acknowledgements

MIT; see `LICENSE`. Window lifecycle patterns follow the separately licensed Flow plugin. Tern belongs to Stencil; this is an independent plugin, not an official Stencil component.
