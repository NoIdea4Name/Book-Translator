# Editing Book Translator

Start with `main.py`. It creates the Qt application and opens `MainWindow` from
`book_translator/ui.py`. The code uses ordinary Python functions, Qt widgets, and
a small number of dataclasses for page data.

## Where to make changes

All paths below are inside `book_translator/`, except `main.py` and `tests/`.

| File | Responsibility |
| --- | --- |
| `main.py` | Start the app and optionally open files from command-line arguments. |
| `config.py` | Editable defaults, language choices, API URL, and request limits. |
| `layout.py` | Build the header, toolbar, editor, preview, thumbnails, and status bar. |
| `styles.qss` | Qt stylesheet: fonts, colors, padding, and borders. |
| `widgets.py` | Shared label/button helpers, stylesheet loading, and the app icon. |
| `ui.py` | Connect user actions to document state, saving, and provider requests. |
| `settings.py` | API key/model controls, model discovery, and connection testing. |
| `gemini_service.py` | Send API requests and turn responses into validated assessments. |
| `prompts.py` | Shared extraction, translation and confidence prompts. |
| `batch.py` | Sequential batch progress and issue state. |
| `core.py` | Open/render PDFs and images; read/write projects and text exports. |
| `confidence.py` | Validate confidence data and choose which passages to flag. |
| `preview.py` | Pan/zoom, draw review boxes, and load visible thumbnails. |
| `assets/` | Editable SVG icons. |
| `tests/` | Offline regression tests using real widgets and mocked API responses. |

## Common edits

To change the startup language or review threshold, edit `config.py`:

```python
DEFAULT_LANGUAGE = "English"
CONFIDENCE_THRESHOLD = 85
```

Defaults apply to new projects. A saved project retains its own settings. You can
add language names to `LANGUAGES`; the dropdown also accepts typed languages.
Enter your API key through Settings rather than putting it in the source code.

To change the translation tone, edit `TRANSLATION_PROMPT` in `prompts.py`.
`{target_language}` is replaced with the language selected in the app. Keep the
instructions to return complete text and the expected confidence fields, because
`confidence.py` validates that response format.

To move a control, edit the corresponding `_build_...` function in `layout.py`.
For example, `_build_toolbar` creates the extraction and translation controls.
Each button connects directly to a method on the window, such as `window.translate`.
The builders attach their widgets to the window so the action methods can update them.

For appearance changes, edit `styles.qss`. Object names such as `primary` and
`connectionDetails` connect controls to selectors such as `QPushButton#primary`.
`widgets.load_stylesheet` resolves icon paths; the app can start from another folder.
Page-overlay colors are drawing constants named `COLORS` in `preview.py`.

## How a translation runs

1. `MainWindow.translate` reads the current page text and target language.
2. It renders the current page image and gets a `RotatingService` for the configured providers.
3. `_start_job` captures a `PageJob` with the original page number and starts a worker.
4. The service adds the prompt, sends the API request, and validates its JSON response.
5. The worker places the result in a queue. `_poll_results` reads it on the UI thread.
6. `_apply_result` saves it to the original page and refreshes that page if it is visible.

Keep widget updates and PDF rendering on the UI thread. Workers receive text and
image snapshots and return results through a queue. A request token distinguishes
the active request from a cancelled one, so an old response cannot overwrite new work.

## Page state and projects

`BookDocument.pages` contains a `PageState` for each page. A page holds its source,
translation, target language, and optional confidence assessments. `translated_from`
records the source used for the translation and detects later source edits.

Project files use JSON and save atomically. Their source-file paths are relative
to the project where possible. Confidence coordinates use
`[top, left, bottom, right]` on a 0–1000 scale across the original image; they are
independent of the current preview zoom.

## After editing

Restart the app to load your changes and run:

```powershell
py -3 -m unittest discover -s tests -v
```

For layout or stylesheet changes, also open the app at a smaller window size and
check the editor, preview, settings dialog, and scrolling thumbnail strip.

## Combined extraction and translation

Edit `COMBINED_PROMPT` in `prompts.py` to adjust the combined task.
`GeminiService.extract_and_translate()` makes one generation request using a schema
with two assessments. `CombinedAssessment.from_dict()` validates both before the UI
applies either. The existing project fields store the two results and their confidence
regions; no separate project format is needed. Toolbar grouping lives in `layout.py`.

## Batch processing

`batch.py` holds `BatchRun`: a fixed list of page indices, the chosen action,
progress, and issues. `ThumbnailStrip` uses Qt's extended selection mode.
In `ui.py`, `_run_selected()` handles multi-page preflight and overwrite confirmation;
`_advance_batch()` renders one page and starts its worker; `_finish_batch_page()`
records the result and schedules the next page on the UI event loop. Single-page
actions retain their existing paths. No entire-book image buffer is allocated.

The batch's remaining pages are read-only. Each request carries its original page
index, and cancellation invalidates both the request and pending batch callbacks.
All file, PDF and widget access stays on the GUI thread; only AI calls run in
workers. No extra Gemini call is added for batching the combined action.

## Page organization

`BookDocument.refs` maps each visible page to an original file and page/frame number.
`insert()`, `delete()`, and `reorder()` change these references and their corresponding
`PageState` objects together. PDF handles are shared by source path; no source file is
modified. Rendering and offline extraction use the source frame, not the display index.

Project format 3 saves `page_refs` alongside page state; versions 1 and 2 still load.
`restore_project()` validates references against the opened source files before applying
the saved order. Empty projects are supported.

The thumbnail widget emits `move_requested(rows, destination)` for internal drags.
The window owns the actual document change and rebuilds thumbnails afterward, clearing
the old icon cache. Context-menu actions use the clicked thumbnail's index. Structural
edits are blocked while a request or batch holds page indices.

## Generation preferences

`preferences.py` contains immutable `GenerationOptions`, task-scoped prompt assembly,
and legacy QSettings migration. Existing QSettings contain instructions and temperature only. MainWindow loads
preferences at startup and passes them to each provider, including batch requests.
SettingsDialog commits all settings through `secure_settings.py` only on Save. Gemini connection tests use their own
fixed prompt and temperature zero. Keep the confidence schema mandatory when changing
how custom instructions are appended.

## Output preview

`output_preview.py` provides a read-only dialog using the same `core.export_text()`
formatter as file export. It copies exactly the visible preview, including Unicode,
page headings and missing/stale markers. Refresh is explicit so batch results do not
interrupt text selection. MainWindow opens it modally; document switching and page
reordering cannot invalidate its book reference while it is open.

## Find and replace

`find_replace.py` contains literal search helpers and the modal all-page dialog.
Matches keep Python offsets; highlighting converts these to Qt UTF-16 cursor offsets.
Replacements are grouped per field and applied backwards so offsets remain valid.
The most recent mutation retains original text/assessment objects for one-step undo.
`pages_changed` notifies MainWindow to mark the project dirty and refresh text/review
counts. Structural changes and concurrent batches cannot run during the modal dialog.

## Page names and export options

`PageState.name` is optional and defaults to an empty string for older projects.
`BookDocument.page_name()` supplies the default page number. Thumbnail labels show
the custom name; long names are shortened visually with the full name in the tooltip.
Rename changes only metadata, never the original filename or confidence data.

`ExportOptionsDialog` selects the content mode and heading visibility before the save
file dialog. Both preview and export call `export_text(show_page_headers=...)`.

## Phone capture and image editing

- `phone_server.py`: a short-lived `ThreadingHTTPServer`, random token URL, bounded
  uploads, Pillow validation, and an ordered batch queue. No Qt widgets or Gemini
  state are accessed from request threads. The session directory is temporary.
- `assets/phone.html`: self-contained responsive phone page. Native capture/file
  inputs feed a local queue. Uploads are sequential and individually retryable;
  an idempotent commit publishes each photo as it arrives, allowing the desktop
  to process images and free staging space during large sends. No CDN or
  external scripts are needed. Camera streaming is deliberately not used: a LAN
  HTTP page does not have the secure context required by `getUserMedia`.
- `phone_import.py`: owns the receiving dialog, QR link, UI-thread polling,
  save/add decisions, and durable local image copies. Add-only storage uses
  `QStandardPaths.AppLocalDataLocation/images`. The modal dialog prevents a
  simultaneous Gemini batch or structural workspace edit.
- `image_editor.py`: pure Pillow/numpy crop and homography helpers plus a four-handle
  Qt editor. Pillow's perspective coefficients map output pixels back to the input.
  The editor works on a copy and returns an image only when accepted.
- `BookDocument.replace_image`: replaces one file/frame reference, preserves text
  and name, clears now-invalid confidence coordinates, and refreshes PDF handles.
  Project format 3 already supports the new image references; no migration is needed.

Phone uploads run in server threads, unlike ordinary document file/PDF operations,
which remain on the UI thread. Keep those boundaries when extending the feature.
Run `python -m unittest discover -s tests -v`; `test_phone_import.py` exercises real
loopback HTTP requests, retry behavior, transforms, storage, UI integration, and
project reopening without making Gemini calls.

## Save a thumbnail image

`image_export.py` builds a Windows-safe default filename from the page name and
atomically writes a PNG. Image pages retain their full stored resolution and
selected frame; PDF pages use the existing renderer. The thumbnail menu passes
the clicked row to `save_thumbnail_image`, which uses Qt's save dialog for the
filename, default suffix and overwrite confirmation. No page state is changed.

Multi-selection switches the thumbnail menu to `Save selected images` and passes a
snapshot of selected rows. `save_selected_images` owns the folder picker, cancellable
modal progress and per-page failure report. `save_page_copy` reserves unique paths
with exclusive creation and case-insensitive name matching, preserving existing
files and handling duplicate page names. Single-image save remains available.

## Providers and rotation

`openai_service.py` implements OpenAI Responses over the existing httpx dependency.
It uses the shared prompts, options, image snapshots and confidence validators,
and returns the same `Assessment` / `CombinedAssessment` types as Gemini.
It disables response storage (`store: false`) and caps normal output at 16,384
tokens (connection tests: 2,048). Gemini keeps its existing output cap.

`api_errors.py` gives provider errors structured retry/failover metadata and parses
Retry-After. Refusals and page/input failures stay terminal; they never fan out.
`api_rotation.py` owns immutable `ApiProfile` entries, thread-safe active/paused
state and bounded retries. A `RotatingService` captures one cancellation event and
one options snapshot per page. Check cancellation after every HTTP call and before
any retry, switch or state update. Workers report status through `_api_events`;
widgets and PDF operations remain on the UI thread.

`MainWindow._service` retains one router across batch pages; editing settings resets
it. `settings.py` edits a separate profile list so Cancel leaves current credentials
and order intact. Connection tests use only the selected profile, without fallback.
Keys are kept in the OS credential store; profile metadata and preferences are saved
by `secure_settings.py`. Environment variables seed initial entries only when no saved
configuration exists. Projects and exports never contain API keys. Existing Gemini projects,
confidence flags, output preview and offline PDF extraction continue to work.

`tests/test_api_rotation.py` checks actual OpenAI request payloads with mocked HTTP,
retry limits, quota/auth failover, cooldown, cancellation, redaction, profile ordering
and combined batch processing across providers. No live credentials are used.


## NVIDIA NIM

`nvidia_service.py` subclasses `OpenAIService` to reuse the three task methods,
connection-test sample, shared prompt assembly, HTTP error handling and anchor
validation. It overrides the endpoint, model normalization, generation wire format,
completion parser, model discovery and pending-request polling. This shares behavior
without assuming NVIDIA uses OpenAI's Responses API.

- Endpoint: `https://integrate.api.nvidia.com/v1/chat/completions`.
- Default model: `moonshotai/kimi-k3`; retain the owner/model prefix.
- User content contains text and an inline base64 image URL. A system message carries
  instructions and the assessment JSON schema. K3's documented endpoint does not
  expose `response_format`, so confidence JSON is validated locally.
- Use only `message.content` with `finish_reason == "stop"`. Never accept reasoning,
  refusals, truncated output or invalid confidence objects as page text.
- K3 supports reasoning low/high/max and temperature 0–1. These restrictions are
  applied only to that model. Normal requests allow 16,384 output tokens; the sample
  connection test uses low reasoning, temperature zero and 4,096 tokens.
- HTTP 202 is polled at `/v1/status/{requestId}` using a validated UUID, Retry-After,
  a deadline and the request's cancellation event. Poll retrieval errors are terminal
  so rotation does not resubmit an already accepted generation automatically.
- `ApiProfile.reasoning_effort` is saved with profile metadata; `NVIDIA_API_KEY` and `NVIDIA_MODEL`
  optionally seed the profile at startup. Existing project formats are unchanged.

`tests/test_nvidia.py` checks mocked request payloads, response validation, pending
responses, cancellation, settings, environment setup and mixed-provider batches.
It does not make live calls or certify account quota/access.

Reference: [Kimi K3 inference](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3-infer)
and [status polling](https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3-statuspolling).


## Secure settings persistence

`secure_settings.py` owns the settings file and credential lifecycle. It explicitly
selects the keyring package's Windows WinVault, macOS Keychain or Linux SecretService
backend; automatic backend discovery, plaintext and third-party backends are not used.
Credentials use service `BookTranslator.API` and random UUID account names. Profile
metadata contains only that reference; never serialize `asdict(ApiProfile)` to disk.

Saving stages fresh vault entries and verifies readback, then commits non-secret JSON
with QSaveFile (no direct-write fallback). Only after commit are old credentials removed.
On failure, staged entries are removed and the old configuration remains intact. A
QLockFile serializes loading/saving across app instances. Cleanup failures are surfaced
without reporting a committed save as unsuccessful. Process termination between stages
can leave unused vault entries; these contain encrypted credentials, not cleartext files.

Startup restores profiles and settings, preferring them over environment seeding. It
keeps profiles and preferences if credentials are unavailable, blanks the affected key
fields, and shows a warning. Saves are blocked while an existing vault is inaccessible.
Malformed/future settings are preserved rather than overwritten. Old QSettings options
remain a fallback on first launch; the new settings file becomes authoritative on Save.

Tests use an injected fake vault and temporary settings path. They cover restart,
metadata without secrets, key replacement/removal, read/write failures, rollback,
locked/missing credentials and UI Save/Cancel. No real user credential store is touched.
Native Windows Credential Manager must also be checked on Windows; Linux CI cannot
exercise Windows credential encryption or OS prompts.


NVIDIA timeout handling (1.16.1): `_client` uses a 600-second read timeout and a
20-second connect timeout. `NVIDIA_RESPONSE_TIMEOUT_SECONDS` in config.py controls
the response and pending-status wait. A POST read timeout is terminal to prevent
automatic duplicate generation; connect timeouts remain retryable. Cancellation
discards results immediately but an in-flight blocking HTTP call may continue until
it completes or times out. No timeout change is applied to Gemini or OpenAI.

BatchRun.unsuccessful_pages retains failed/skipped page indices separately from
issue messages. Batch completion selects those pages and opens the first; successful
runs retain their selection. Checked thumbnails skip badge painting entirely; the
review signature still persists and Mark as unchecked restores review flags.

ThumbnailDelegate paints vector icons: amber exclamation for AI review and a red-background
flag for PageState.manual_flag. The manual flag is persisted as a boolean, defaults
to false for older projects, and is excluded from review_signature so older checked
status remains valid. Mark as checked clears manual flags as well as AI badges.

ReviewFlag.key identifies a region by task and index (or task:page). PageState keeps
a dismissed_reviews list, excluded from review signatures and persisted in projects.
Original assessment regions remain after text/find-replace edits, with outdated flags
and scores clearly labelled. Individual dismissals filter list items and image boxes
without changing assessment data or creating a fallback whole-page flag. New AI
results clear dismissals for the replaced task; replacing the image clears all.
The review panel has a session-level collapse toggle for the list; confidence totals stay visible.


## Review text ranges

`text_links.py` resolves region quotes against the complete original assessment,
normalizing whitespace while preserving actual offsets. A complete set of duplicate
quotes is allocated by distinct box reading order; established surrounding ranges
can disambiguate partial sets. Unresolved quotes remain unlinked. This is conservative
matching, not a guarantee of understanding every multi-column layout.

`PageState.review_links` stores the report fingerprint, last text and per-region ranges.
The editor records QTextDocument.contentsChange positions and updates ranges through
exact insertions/removals. Corrections inside a passage stay linked; full deletions
retain empty anchors for Undo. Bulk changes use a conservative diff fallback.
Ambiguous duplicate edits do not silently resolve to another occurrence.
New results reset their task's links. Project loading validates offsets, and review
signatures exclude this cache to preserve existing checked status. UI selection converts
Python character offsets to Qt UTF-16 positions. Image clicks emit the same ReviewFlag
as list navigation; dragging continues to pan.

Alt-click in ThumbnailStrip changes only the current row using NoUpdate; its mouse
release is consumed so Qt does not collapse the batch selection. Normal Ctrl/Shift
selection remains native. The review header uses a stretch spacer and a zero-padding
28px icon-only toggle; passage buttons have a borderless hover fill.

Version 1.17.2 also repairs safely recoverable links discarded by older versions.
Both dismiss and collapse controls have borderless hover and pressed feedback.

`export_text(show_placeholders=False)` suppresses generated missing-source and missing-translation
placeholders. Both output dialogs expose the same option; Copy all uses the rendered
preview. Empty bodies are skipped when page headers are hidden. Literal occurrences
in actual text are never removed by search/replace.


## Floating selected-text chat

- `chat.py` owns the answer/replacement JSON contract and prompt. `null` replacement
  means answer only; an empty string explicitly proposes deletion.
- `chat_panel.py` owns the modeless Qt tool window, per-selection snapshot, bounded
  conversation, attachment toggles, Gemini-only router, and cancellable daemon worker.
  Responses pass through a queue; workers do not reference Qt widgets or documents.
- `GeminiService.chat` reuses `_generate` with an explicit schema/parser, preserving
  timeouts, API error redaction, temperature and structured-response validation.
- `PageView` maps a drag through the view transform into image coordinates, clamps
  the rectangle to the image, and exports a crop from the underlying pixmap only.
- Applying checks book/page identity and unchanged editor/page text, then appends
  with a paragraph separator by default, or replaces the captured UTF-16 range
  when requested. Both use one QTextCursor edit block. Existing text-change
  handlers update dirty state, confidence links and translation staleness normally.
- Ctrl+J is a window shortcut (not a system-wide hotkey). No new dependency is needed.

The chat panel is centered within the available screen area by `show_panel`, using
the main window’s screen, not the mouse. Chat-specific appearance uses scoped selectors in `styles.qss`.
`hide_proposal` removes the complete suggestion card after applying or starting
a new request. The conversation renders escaped text with compact role labels.


## Workspace styling

`layout.py` sets compact margins, panel headers and the grouped `pageNavigation`
frame. Main-window utility icons are editable SVGs named `assets/ui-*.svg`.
`styles.qss` owns the shared palette, focus/hover states, checkbox indicators and
scrollbars. `ThumbnailStrip._page_icon` provides an explicit selected icon pixmap
so Qt highlights the thumbnail card without tinting the original scan.

Chat suggestions are dismissible through `dismiss_proposal`. `apply_mode` defaults
to Add to page for each suggestion. Region state is initialized from the preview
and refreshed when a new region is drawn on the chat’s original page.


## Shared reference context and book memory

`book_context.py` builds bounded neighboring-page reference data and returns a
per-request copy of GenerationOptions with that reference appended to common
instructions. The app's saved preferences never contain book content. PDF fallback
runs on the GUI thread. MainWindow._service(page_index) receives the actual batch
page; chat resolves its captured page identity before building request options.
All extraction/translation providers and Gemini chat receive the same reference.
Connection tests use ordinary preferences and never send reference material.

`summary_panel.py` provides a modal editable draft and read-only current-context tab.
`memory.py` adds request-only MemoryOptions (previous_summary and memory_page),
a top-level book_summary schema field and the running-memory instructions.
All three providers parse the primary result first, then attach transient memory_update
metadata; it is excluded from dataclass/project serialization. Chat uses the same
wrapper around its strict reply schema. Invalid memory is a warning, not an API
failure or retry. MainWindow commits the update before scheduling the next batch
page. Chat commits only against the original book and matching previous memory.
Token checks discard cancelled responses; previous-memory checks preserve newer edits.
ContinuityOptions inherits MemoryOptions so the two response features compose.
There is no automatic summarize call. Only the explicitly requested Summary dialog
update uses the standalone summarize method with current-page material.

GenerationOptions persists context_enabled, pages_before, pages_after,
summary_enabled and auto_update_summary. Inclusion and updating are independent.
Offline PDF extraction and connection checks do not request memory updates.
OpenAI/NVIDIA _generate(summary=True)
uses the existing assessment JSON envelope without extraction confidence instructions;
Gemini disables those instructions with review_confidence=False. Only the summary
string is retained, with explicit length validation.


## Previous-page continuity edits

`continuity.py` owns a request-only PreviousPage snapshot, a narrowly extended
translation schema, and exact suffix validation. `ContinuityOptions` is a transient
GenerationOptions subclass constructed in MainWindow._service after reference
context; it is never assigned to persisted settings. Only translate/combined calls
enable the extra schema. For combined responses the optional nullable edit lives
inside the translation assessment, not extraction.

`ContinuityAssessment` subclasses Assessment but keeps edit/snapshot attributes
outside dataclass fields. Thus request metadata is not persisted in assessments.
All providers parse the current result and bounded edit together. UI application
checks the previous source, translation, translated_from and target language against
the snapshot before editing. Cancellation uses the existing job token. The current
page result remains usable if the previous page was edited while waiting.

The latest successful edit has a guarded workspace-local undo record. Batches collect
continuity_notes separately from failures so successful current translations are not
reported as failed. Original confidence reports are preserved and become outdated
when the corresponding translation text changes.

## Interface translation

`i18n.py` loads UTF-8 packs from `locales/*.json`, validates named placeholders and
falls back to English. `main.py` installs the saved language before creating widgets.
Settings persists the interface language separately with QSettings; restart applies it.
Use `tr()` only for interface text, never book content or provider/model identifiers.
The Qt translator covers standard dialog buttons. `TRANSLATING.md` describes adding
a pack. Tests cover discovery, Vietnamese strings and placeholder safety.

Qt translator safety: `QtLanguage.translate()` must return the original source
for unknown strings, never an empty Python string. Empty translations can erase
plugin error messages (notably QTgaFile), interfering with format detection as well
as hiding dialog labels. Restrict translations to control contexts. English installs
no custom Qt translator. `test_qt_language.py` installs the translator and exercises
actual dialog buttons and image/PDF preview decoding, unlike plain `tr()` tests.

## Translation directly from images

Translate remains enabled whenever pages are loaded and no run is busy. Source-empty
pages are processed in batches as well as singly. Gemini and the shared OpenAI/NVIDIA
translate methods select image-authoritative instructions when source.strip() is empty.
The image is already attached; no extraction request or extra summary call is made.
Only translation state changes. translated_from keeps the captured source (even empty),
so adding source later correctly marks the translation stale. Image-only regions use
empty source_text to avoid transcription output; check_text_anchors(None) validates
output quotes without rejecting their image boxes for lack of a source transcript.

## Export, batch monitoring and review navigation

`docx_export.py` builds Word bytes using python-docx and performs atomic output writes.
`ExportOptionsDialog` chooses a file format; existing plain text output is unchanged.
`BatchItem` retains page identity, counters, notes and immutable before/after strings.
`BatchControls` handles the GUI-thread snapshots and pause/retry behavior; `_apply_result`
refreshes snapshots just before applying AI changes so manual edits while a request
was in flight are not misattributed. `BatchWindow` reads these records on a GUI timer,
never starts workers itself and can be closed without cancelling. The last run is
in-memory only. An explicit retry resolves current indices from page objects and
resets the API router; old timer callbacks are guarded by batch identity/job state.
`next_review_passage`/`next_review_page` query live review flags; `_review_cursor` stores
page identity and flag key, not a stale text offset. Existing tracked spans handle
repeated text and editing. `PageView.fit_timer` is parented to the view to prevent
delayed fitting after the native widget has been deleted.

Page-local search is in `page_find.py`; it uses Qt document cursors for Unicode-safe matches. `page_history.py` owns bounded text snapshots, typing coalescing, restore, and the history dialog. PageState.history is saved with projects, validated on load, and excluded from confidence signatures. `_mark_dirty` checkpoints changes; project saving also checkpoints direct model edits.

Thumbnail reordering uses local mouse events in `ThumbnailStrip`: press records the origin, movement past the platform threshold captures selected rows and draws the insertion marker, and release emits `move_requested`. Escape/outside release cancels. Native QDrag/drop events are disabled to avoid the platform drag handoff. Regression tests send press/move/release events without mocking the drag or move handler.

Performance: `_mark_dirty(pages=...)` checkpoints only affected pages (default: visible page). Callers applying results to a background page must pass that page explicitly; continuity edits checkpoint their target separately. Replace-all passes its changed pages. Batch rows retain their QTableWidgetItems and idle polling uses a constant-size event key. Confidence signatures never serialize PageState.history. Full save checkpoints every page intentionally.

AI input rendering: `_page_operation` captures a source reference/password before starting an AI worker. `page_render.py` uses one spawn-process executor, opening its own PDF handle; no GUI document handle crosses threads. Main entry calls freeze_support before launching Qt for packaged Windows builds. Cancellation is checked before rendering and before API submission. Context extraction is cached; batch stage durations separate preparation from provider waiting.

- `local_ocr.py`: cancellable Tesseract subprocess, executable discovery, TSV text and confidence/box parsing. `GenerationOptions.ocr_executable` and `.ocr_languages` are saved in secure settings' non-secret metadata. The UI renders inputs using the existing isolated page renderer and applies results through the normal page job path. `BatchRun.local_ocr` preserves the chosen method on retries.
