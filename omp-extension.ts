/**
 * Tern media bridge for OMP.
 *
 * Records the media a conversation carries into the Tern media store: images
 * from user/assistant turns, media a successful tool result returned or named,
 * and the paths a user imports explicitly. Live inside Tern only — with no
 * `TERN_PANE` the factory registers nothing, so no child process runs and no
 * conversation is touched.
 *
 * Self-contained on purpose: the extension is installed as a symlink, so it
 * imports nothing from its sibling files. Everything it needs (pure media
 * extraction, backend transport, event wiring, commands) is here.
 *
 * Contract with the backend (`media-store.py`, one JSON request on stdin and
 * one JSON response on stdout per invocation):
 *   touch    {session}                        bind this pane to a conversation
 *   register {session, items}                 store media under that conversation
 *   tag      {id, tags}                       replace an item's user tags
 *   watch    {session_id, path}               associate a directory explicitly
 *   backfill {session, transcript_path}       import a session file's media
 *
 * The extension never mutates the session: it reads events, returns no
 * tool_call/tool_result patches, and only reports what the backend answered.
 * Base64 image payloads go to the backend's stdin and are never logged or
 * echoed into a message.
 *
 * Only a main session's `touch` names the pane, so a capture or an import can
 * never move the pane's binding. Binding writes go through one queue and
 * commit in the order their events arrived, however slow the backend is.
 *
 * Self-check: `bun omp-extension.ts` (or `... --self-check`) exercises the media
 * extraction, the workspace identity and the binding write queue, and exits
 * non-zero when a check fails.
 */

import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir, hostname } from "node:os";
import { basename, isAbsolute, join, resolve } from "node:path";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

// ============================================================================
// Media extraction (pure: no I/O, no logging, no side effects)
// ============================================================================

export type MediaSource = "created" | "transferred" | "viewed" | "tagged";
export type MediaKind = "image" | "video" | "audio" | "document";

/** One `register` item, minus the fields the backend derives itself. */
export interface MediaItemRequest {
	path?: string;
	data?: string;
	mime?: string;
	name?: string;
	source: MediaSource;
	tags?: string[];
	recorded_at?: string;
}

/** Extensions the media store is expected to accept, by kind. */
export const MEDIA_EXTENSIONS: Readonly<Record<string, MediaKind>> = {
	png: "image",
	jpg: "image",
	jpeg: "image",
	gif: "image",
	webp: "image",
	bmp: "image",
	tif: "image",
	tiff: "image",
	heic: "image",
	heif: "image",
	avif: "image",
	svg: "image",
	mp4: "video",
	mov: "video",
	m4v: "video",
	mkv: "video",
	webm: "video",
	avi: "video",
	mp3: "audio",
	wav: "audio",
	m4a: "audio",
	aac: "audio",
	flac: "audio",
	ogg: "audio",
	opus: "audio",
	pdf: "document",
};

/** Tools whose successful result means the file was produced, not merely seen. */
const PRODUCING_TOOLS: Readonly<Record<string, true>> = {
	write: true,
	edit: true,
	apply_patch: true,
	multiedit: true,
};

/** Tools that name exactly one file in their input. */
const PATH_INPUT_TOOLS: Readonly<Record<string, true>> = { read: true, write: true, edit: true };

const MARKDOWN_TARGET = /!?\[[^\]]*\]\(\s*(?:<([^>]+)>|([^)]*?))\s*\)/g;
const QUOTED_PATH = /"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`/g;
const TOKEN = /[^\s"'`()\[\]<>|]+/g;
const EDGE_TRIM = /^[,;:!*–—]+|[.,;:!*–—]+$/g;
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;
const WINDOWS_DRIVE = /^[a-z]:[\\/]/i;
const LINE_SUFFIX = /(?::\d+){1,2}$/;
const DATA_URL = /^data:([^;,]+);base64,(.*)$/is;

/** Kind of a path from its extension, or undefined when it is not media. */
export function mediaKindForPath(candidate: unknown): MediaKind | undefined {
	if (typeof candidate !== "string") return undefined;
	const path = candidate.replace(/\\/g, "/").trim();
	const dot = path.lastIndexOf(".");
	if (dot <= path.lastIndexOf("/") || dot === path.length - 1) return undefined;
	const ext = path.slice(dot + 1).toLowerCase();
	return Object.prototype.hasOwnProperty.call(MEDIA_EXTENSIONS, ext) ? MEDIA_EXTENSIONS[ext] : undefined;
}

/** Whether a path looks like media the store can accept. */
export function isMediaPath(candidate: unknown): boolean {
	return mediaKindForPath(candidate) !== undefined;
}

/**
 * Clean one path-like token. Returns undefined for anything that is not a
 * plausible local path with a media extension, so unrelated strings, secrets
 * and text files are never treated as media.
 */
export function normalizeMediaPathCandidate(raw: unknown): string | undefined {
	if (typeof raw !== "string") return undefined;
	let path = raw.trim().replace(EDGE_TRIM, "");
	if (path.length === 0 || path.length > 4096) return undefined;
	if (path.startsWith("//") || path.includes("://")) return undefined;
	if (!WINDOWS_DRIVE.test(path) && SCHEME.test(path)) return undefined;
	path = path.replace(/\\/g, "/").replace(LINE_SUFFIX, "").replace(/[?#].*$/, "");
	if (!isMediaPath(path)) return undefined;
	return path;
}

/**
 * Local media paths clearly referenced by a block of text (markdown targets
 * and bare tokens). Remote URLs, data URLs, and non-media files are skipped;
 * results are de-duplicated and capped at `limit`.
 */
export function extractMediaPaths(text: unknown, limit = 8): string[] {
	if (typeof text !== "string" || text.length === 0 || limit <= 0) return [];
	const found: string[] = [];
	const seen = new Set<string>();
	const consider = (raw: string): boolean => {
		const path = normalizeMediaPathCandidate(raw);
		if (path === undefined) return false;
		const key = path;
		if (seen.has(key)) return false;
		seen.add(key);
		found.push(path);
		return found.length >= limit;
	};

	let remainder = text.replace(MARKDOWN_TARGET, (_match, angle: string, target: string) => {
		if (found.length < limit) consider(angle ?? target ?? "");
		return " ";
	});
	if (found.length >= limit) return found;
	remainder = remainder.replace(QUOTED_PATH, (_match, double: string, single: string, backtick: string) => {
		if (found.length < limit) consider(double ?? single ?? backtick ?? "");
		return " ";
	});
	if (found.length < limit) {
		for (const match of remainder.matchAll(TOKEN)) {
			if (consider(match[0])) break;
		}
	}
	return found;
}

/** Split `data:<mime>;base64,<payload>` and bare base64 payloads. */
function splitImageData(value: string, declaredMime: string | undefined): { mime: string; data: string } | undefined {
	const payload = value.trim();
	if (payload.length === 0) return undefined;
	const match = DATA_URL.exec(payload);
	if (match) return { mime: match[1].toLowerCase(), data: (match[2] ?? "").replace(/\s+/g, "") };
	if (declaredMime === undefined) return undefined;
	const mime = declaredMime.trim().toLowerCase();
	if (mime.length === 0) return undefined;
	return { mime, data: payload.replace(/\s+/g, "") };
}

/**
 * An image content block as a register item. Blocks without an explicit
 * `image/*` mime are refused rather than guessed, so nothing is ever
 * mislabelled as an image.
 */
export function imageItemFromBlock(block: unknown, source: MediaSource, name?: string): MediaItemRequest | undefined {
	if (!block || typeof block !== "object" || !("type" in block) || block.type !== "image") return undefined;
	if (!("data" in block) || typeof block.data !== "string") return undefined;
	const declaredMime = "mimeType" in block && typeof block.mimeType === "string" ? block.mimeType : undefined;
	const split = splitImageData(block.data, declaredMime);
	if (!split || !split.mime.startsWith("image/") || split.data.length === 0) return undefined;
	return { data: split.data, mime: split.mime, name, source };
}

/** Source label for items that came from a tool's successful result. */
export function sourceForTool(toolName: string): MediaSource {
	return PRODUCING_TOOLS[toolName] === true ? "created" : "viewed";
}

/** The single file argument of read/write/edit, verbatim. */
export function filePathFromToolInput(toolName: string, input: unknown): string | undefined {
	if (PATH_INPUT_TOOLS[toolName] !== true) return undefined;
	if (!input || typeof input !== "object") return undefined;
	for (const key of ["file_path", "path", "filePath"] as const) {
		if (!(key in input)) continue;
		const value = input[key];
		if (typeof value === "string" && value.trim().length > 0) return value.trim();
	}
	return undefined;
}

/** The `text` of a text content block, or undefined for any other block. */
function textBlockText(block: unknown): string | undefined {
	if (!block || typeof block !== "object" || !("type" in block) || block.type !== "text") return undefined;
	if (!("text" in block) || typeof block.text !== "string") return undefined;
	return block.text;
}

/**
 * Register items for one successful tool result: structured image blocks plus
 * the local media the result clearly names (its file argument, and media paths
 * in its text output). Failures contribute nothing.
 */
export function itemsFromToolResult(event: unknown, limit = 12): MediaItemRequest[] {
	if (!event || typeof event !== "object" || limit <= 0) return [];
	if ("isError" in event && event.isError === true) return [];
	const toolName = "toolName" in event && typeof event.toolName === "string" ? event.toolName : "";
	const content = "content" in event ? event.content : undefined;
	const input = "input" in event ? event.input : undefined;
	const blocks = Array.isArray(content) ? content : [];
	const source = sourceForTool(toolName);
	const items: MediaItemRequest[] = [];
	const seenPaths = new Set<string>();

	const addPath = (raw: string): void => {
		if (items.length >= limit) return;
		const path = normalizeMediaPathCandidate(raw);
		if (path === undefined) return;
		const key = path;
		if (seenPaths.has(key)) return;
		seenPaths.add(key);
		const slash = path.lastIndexOf("/");
		const name = slash >= 0 ? path.slice(slash + 1) : path;
		items.push({ path, name: name.length > 0 ? name : undefined, source });
	};

	const inputPath = filePathFromToolInput(toolName, input);
	if (inputPath !== undefined) addPath(inputPath);
	// An image read's returned preview is not a second asset. Retain its source
	// file once, with the original bytes rather than a resized tool preview.
	if (items[0]?.path === undefined || mediaKindForPath(items[0].path) !== "image") {
		for (const block of blocks) {
			if (items.length >= limit) return items;
			const item = imageItemFromBlock(block, source);
			if (item) items.push(item);
		}
	}

	let budget = 4000;
	for (const block of blocks) {
		if (items.length >= limit || budget <= 0) break;
		const text = textBlockText(block);
		if (text === undefined) continue;
		const slice = text.slice(0, budget);
		budget -= slice.length;
		for (const path of extractMediaPaths(slice, limit - items.length)) addPath(path);
	}
	return items;
}

/**
 * Register items for one persisted message: images a user or assistant turn
 * carried. Tool results arrive through the `tool_result` hook instead, so the
 * `toolResult` role contributes nothing here.
 */
export function itemsFromMessage(message: unknown, limit = 12): MediaItemRequest[] {
	if (!message || typeof message !== "object" || limit <= 0) return [];
	if (!("role" in message) || (message.role !== "user" && message.role !== "assistant")) return [];
	if (!("content" in message)) return [];
	const content = message.content;
	if (!Array.isArray(content)) return [];
	const items: MediaItemRequest[] = [];
	for (const block of content) {
		const item = imageItemFromBlock(block, "transferred");
		if (item) items.push(item);
		if (items.length >= limit) break;
	}
	return items;
}

// ============================================================================
// Tern bridge: identity, transport, reporting
// ============================================================================

const PANE_ID = (process.env.TERN_PANE || "").trim();
const WINDOW_KEY = (process.env.TERN_WINDOW_KEY || "").trim();
const PYTHON_BIN = (process.env.TERN_MEDIA_PYTHON || "").trim() || "python3";
const PLUGIN_DIR_OVERRIDE = (process.env.TERN_MEDIA_PLUGIN_DIR || "").trim();
const BACKEND_FILE = "media-store.py";
const SHARED_PLUGIN_DIR = join(homedir(), "Sync", "shared", "tern", "media");
const HOST = (process.env.TERN_HOST || "").trim() || hostname();
/** Tern palette action the media window plugin owns. */
const PALETTE_ACTION = "plugin.media.open";

const MAX_IMAGE_CHARS = 16 * 1024 * 1024;
const TOUCH_TIMEOUT_MS = 10_000;
const REGISTER_TIMEOUT_MS = 30_000;
const BACKFILL_TIMEOUT_MS = 60_000;
const LOOKUP_TIMEOUT_MS = 5_000;
const REPORT_COOLDOWN_MS = 60_000;

/** The slice of `ExtensionContext` this extension uses. */
interface HandlerCtx {
	cwd: string;
	agent?: { kind?: string };
	sessionManager: {
		getSessionId(): string;
		getSessionFile(): string | undefined;
		getSessionName(): string | undefined;
		getHeader(): { parentSession?: unknown } | undefined;
	};
	ui: { notify(message: string, type?: "info" | "warning" | "error"): void };
	setTimeout(callback: (...args: unknown[]) => void, ms: number, ...args: unknown[]): unknown;
	clearTimer(timer: unknown): void;
}

interface SessionRef {
	id: string;
	label: string;
	cwd: string;
	isMain: boolean;
}

interface BackendData {
	items: unknown[];
	errors: unknown[];
}

type BackendResponse = { ok: true; data: BackendData } | { ok: false; error: string };

interface SessionState {
	touchedId?: string;
	touchedLabel?: string;
}

/** A native Tern session, as `tern ls --json` reports it. */
export interface TernWorkspace {
	id: string;
	name?: string;
}

/**
 * Serial task queue: a task starts only after every task enqueued before it
 * has settled, so writes commit in the order their events arrived however slow
 * the backend is. A failed task never stops the tasks queued behind it.
 */
export interface SerialQueue {
	enqueue(task: () => Promise<void>): Promise<void>;
	settled(): Promise<void>;
}

export function createSerialQueue(): SerialQueue {
	let tail: Promise<void> = Promise.resolve();
	return {
		enqueue(task: () => Promise<void>): Promise<void> {
			const run = tail.then(task);
			tail = run.then(() => undefined, () => undefined);
			return run;
		},
		settled: (): Promise<void> => tail,
	};
}

const reportedAt = new Map<string, number>();
/** The conversation this pane owns, refreshed by main-session events. */
let owner: { ref: SessionRef; file?: string } | undefined;
/** Native Tern session per conversation, learned when that conversation binds. */
const workspaces = new Map<string, TernWorkspace>();
/** Every binding write for this pane, in event order. */
const bindings = createSerialQueue();
let pluginDirCache: string | undefined;
let pluginDirError: string | undefined;
let logWarning: (message: string) => void = () => undefined;

function describeError(error: unknown): string {
	if (error instanceof Error) return error.message;
	return typeof error === "string" && error.length > 0 ? error : "unknown error";
}

/**
 * Report observable trouble to the user and to OMP's log. Repeated identical
 * messages are suppressed for a minute so one broken backend cannot flood the
 * UI. Data payloads never reach either channel: `logMessage` replaces the
 * user-facing text in the log whenever that text carries a reference, a path
 * or backend detail the trace must not keep.
 */
function report(
	ctx: HandlerCtx,
	message: string,
	level: "info" | "warning" | "error" = "info",
	logMessage?: string,
): void {
	const now = Date.now();
	const last = reportedAt.get(message);
	if (last !== undefined && now - last < REPORT_COOLDOWN_MS) return;
	if (reportedAt.size > 64) reportedAt.clear();
	reportedAt.set(message, now);
	if (level !== "info") logWarning(logMessage ?? message);
	try {
		ctx.ui.notify(message, level);
	} catch {
		// Reporting must never break the session.
	}
}

/** A stderr line worth showing: short, and never an encoded payload. */
function safeStderrDetail(stderr: string): string {
	const firstLine = stderr.trim().split("\n", 1)[0]?.trim() ?? "";
	if (firstLine.length === 0 || firstLine.length > 200) return "";
	if (/[A-Za-z0-9+/=]{64,}/.test(firstLine)) return "";
	return firstLine;
}

function readTrimmedFile(path: string): string | undefined {
	try {
		const text = readFileSync(path, "utf8").trim();
		return text.length > 0 ? text : undefined;
	} catch {
		return undefined;
	}
}

function pluginsDirectory(): string {
	const configDir = (process.env.TERN_CONFIG_DIR || "").trim();
	if (configDir.length > 0) return join(configDir, "plugins");
	if (process.platform === "darwin") return join(homedir(), "Library", "Application Support", "Tern", "plugins");
	const xdg = (process.env.XDG_CONFIG_HOME || "").trim();
	return join(xdg.length > 0 ? xdg : join(homedir(), ".config"), "Tern", "plugins");
}

/** `tern plugin dir` — the CLI's own answer, used only when the known paths miss. */
async function cliPluginDirectory(): Promise<string | undefined> {
	try {
		const child = Bun.spawn(["tern", "plugin", "dir"], {
			stdin: "ignore",
			stdout: "pipe",
			stderr: "ignore",
			signal: AbortSignal.timeout(LOOKUP_TIMEOUT_MS),
		});
		const [code, stdout] = await Promise.all([child.exited, new Response(child.stdout).text()]);
		if (code !== 0) return undefined;
		const dir = stdout.trim();
		return dir.length > 0 ? dir : undefined;
	} catch {
		return undefined;
	}
}

/** A native Tern id as exact text: an id that cannot be rendered exactly is refused, never truncated. */
function nativeIdText(value: unknown): string | undefined {
	if (typeof value === "string") {
		const text = value.trim();
		return text.length > 0 ? text : undefined;
	}
	if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
	return undefined;
}

/**
 * The session holding `paneId`, from a `tern ls --json` document. Sessions are
 * matched by the pane's own block id, so a window that does not hold this pane
 * yields nothing instead of another window's session.
 */
export function ternWorkspaceFromListJson(raw: string, paneId: string): TernWorkspace | undefined {
	if (paneId.length === 0) return undefined;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return undefined;
	}
	if (!parsed || typeof parsed !== "object" || !("sessions" in parsed) || !Array.isArray(parsed.sessions)) {
		return undefined;
	}
	for (const session of parsed.sessions) {
		if (!session || typeof session !== "object" || !("tabs" in session) || !Array.isArray(session.tabs)) continue;
		const holdsPane = session.tabs.some(
			(tab) =>
				tab !== null &&
				typeof tab === "object" &&
				"blocks" in tab &&
				Array.isArray(tab.blocks) &&
				tab.blocks.some(
					(block) =>
						block !== null && typeof block === "object" && "id" in block && nativeIdText(block.id) === paneId,
				),
		);
		if (!holdsPane) continue;
		const id = "id" in session ? nativeIdText(session.id) : undefined;
		if (id === undefined) return undefined;
		const name = "name" in session && typeof session.name === "string" ? session.name.trim() : "";
		return name.length > 0 ? { id, name } : { id };
	}
	return undefined;
}

/**
 * This pane's window as the CLI reports it: `TERN_WINDOW_KEY` scopes the
 * window explicitly, and the pane's own `TERN_PANE` must be found inside it.
 */
async function cliTernWorkspace(): Promise<TernWorkspace | undefined> {
	const args = WINDOW_KEY.length > 0 ? ["ls", "--json", "--window", WINDOW_KEY] : ["ls", "--json"];
	try {
		const child = Bun.spawn(["tern", ...args], {
			stdin: "ignore",
			stdout: "pipe",
			stderr: "ignore",
			signal: AbortSignal.timeout(LOOKUP_TIMEOUT_MS),
		});
		const [code, stdout] = await Promise.all([child.exited, new Response(child.stdout).text()]);
		if (code !== 0) return undefined;
		return ternWorkspaceFromListJson(stdout, PANE_ID);
	} catch {
		return undefined;
	}
}

/**
 * The media plugin root: `TERN_MEDIA_PLUGIN_DIR`, then where the installed
 * plugin is linked, then the shared checkout. Every candidate is verified by
 * the backend script it must contain, so a missing install reports rather than
 * runs something arbitrary.
 */
async function resolvePluginDir(): Promise<string | undefined> {
	if (pluginDirCache !== undefined) return pluginDirCache;
	const candidates: string[] = [];
	if (PLUGIN_DIR_OVERRIDE.length > 0) candidates.push(PLUGIN_DIR_OVERRIDE);
	const linked = readTrimmedFile(join(pluginsDirectory(), "media.path"));
	if (linked !== undefined) candidates.push(linked);
	candidates.push(SHARED_PLUGIN_DIR);

	for (const candidate of candidates) {
		const dir = candidate.startsWith("~") ? join(homedir(), candidate.slice(1)) : candidate;
		if (existsSync(join(dir, BACKEND_FILE))) {
			pluginDirCache = dir;
			return dir;
		}
	}

	const cliDir = await cliPluginDirectory();
	const cliLinked = cliDir === undefined ? undefined : readTrimmedFile(join(cliDir, "media.path"));
	if (cliLinked !== undefined) {
		const dir = cliLinked.startsWith("~") ? join(homedir(), cliLinked.slice(1)) : cliLinked;
		if (existsSync(join(dir, BACKEND_FILE))) {
			pluginDirCache = dir;
			return dir;
		}
	}

	pluginDirError = `no ${BACKEND_FILE} in ${candidates.join(", ")}`;
	return undefined;
}

type BackendRun =
	| { ok: true; code: number; stdout: string; stderr: string; aborted: boolean }
	| { ok: false; error: string; aborted: boolean };

/**
 * Run the backend once: the JSON request goes to its stdin, stdout is drained
 * concurrently so a large request cannot deadlock. The timeout signal kills a
 * hung child. Neither stream's bytes are ever echoed into a message.
 */
async function runBackend(dir: string, request: string, timeoutMs: number): Promise<BackendRun> {
	const signal = AbortSignal.timeout(timeoutMs);
	try {
		const child = Bun.spawn([PYTHON_BIN, join(dir, BACKEND_FILE)], {
			cwd: dir,
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
			signal,
		});
		const writing = (async () => {
			child.stdin.write(request);
			await child.stdin.end();
		})().catch(() => undefined);
		const [code, stdout, stderr] = await Promise.all([
			child.exited,
			new Response(child.stdout).text(),
			new Response(child.stderr).text(),
			writing,
		]);
		return { ok: true, code, stdout, stderr, aborted: signal.aborted };
	} catch (error) {
		return { ok: false, error: describeError(error), aborted: signal.aborted };
	}
}

/** One backend request. Never throws; failures come back as `{ok:false}`. */
async function callBackend(op: string, payload: Record<string, unknown>, timeoutMs: number): Promise<BackendResponse> {
	const dir = await resolvePluginDir();
	if (dir === undefined) return { ok: false, error: pluginDirError ?? `media plugin (${BACKEND_FILE}) not found` };
	const run = await runBackend(dir, JSON.stringify({ op, ...payload }), timeoutMs);
	if (!run.ok) return { ok: false, error: `${op} could not run: ${run.error}` };
	if (run.aborted) return { ok: false, error: `${op} timed out after ${timeoutMs}ms` };
	if (run.code !== 0) {
		const detail = safeStderrDetail(run.stderr);
		return { ok: false, error: `${op} exited ${run.code}${detail.length > 0 ? `: ${detail}` : ""}` };
	}

	let parsed: unknown;
	try {
		parsed = JSON.parse(run.stdout);
	} catch {
		return { ok: false, error: `${op} returned invalid JSON (${run.stdout.length} bytes)` };
	}
	if (!parsed || typeof parsed !== "object") return { ok: false, error: `${op} returned a non-object response` };

	const data: BackendData = { items: [], errors: [] };
	if ("items" in parsed && Array.isArray(parsed.items)) data.items = parsed.items;
	if ("errors" in parsed && Array.isArray(parsed.errors)) data.errors = parsed.errors;
	if ("ok" in parsed && parsed.ok === false) {
		const reason = "error" in parsed && typeof parsed.error === "string" ? parsed.error : "unknown error";
		return { ok: false, error: `${op} failed: ${reason}` };
	}
	return { ok: true, data };
}

/** First per-item rejection in a response, for the user-facing summary. */
function firstErrorSummary(errors: unknown[]): string | undefined {
	for (const entry of errors) {
		if (!entry || typeof entry !== "object") continue;
		const reason = "error" in entry && typeof entry.error === "string" ? entry.error : "rejected";
		return reason;
	}
	return undefined;
}

/**
 * Import diagnostics retain counts and failure reasons, never rejected
 * references: a URL may carry credentials even in a user-facing notification.
 */
export function registerSummary(
	label: string,
	stored: number,
	attempted: number,
	errors: unknown[],
): { message: string; log: string } {
	const rejected = errors.length;
	const reason = firstErrorSummary(errors);
	return {
		message:
			`Tern media: ${label} stored ${stored}/${attempted} item(s), ${rejected} rejected` +
			(reason === undefined ? "" : ` (${reason})`),
		log: `Tern media: ${label} stored ${stored}/${attempted} item(s), ${rejected} rejected`,
	};
}

/** Transcript-import diagnostics exclude reference names from both audiences. */
export function backfillSummary(stored: number, unresolved: number, errors: unknown[]): { message: string; log: string } {
	const reason = firstErrorSummary(errors);
	return {
		message:
			`Tern media: historical import stored ${stored} item(s)` +
			(unresolved > 0 ? `, ${unresolved} unresolved${reason === undefined ? "" : ` (${reason})`}` : ""),
		log: `Tern media: historical import stored ${stored} item(s), ${unresolved} unresolved`,
	};
}

function reportRegister(
	ctx: HandlerCtx,
	label: string,
	result: BackendResponse,
	attempted: number,
	alwaysReport: boolean,
): void {
	if (!result.ok) {
		report(ctx, `Tern media: ${label}: ${result.error}`, "error", `Tern media: ${label} request failed`);
		return;
	}
	const stored = result.data.items.length;
	const rejected = result.data.errors.length;
	if (rejected > 0) {
		const summary = registerSummary(label, stored, attempted, result.data.errors);
		report(ctx, summary.message, "warning", summary.log);
		return;
	}
	if (alwaysReport) report(ctx, `Tern media: ${label} stored ${stored}/${attempted} item(s)`, "info");
}

function sessionFile(ctx: HandlerCtx): string | undefined {
	try {
		const file = ctx.sessionManager.getSessionFile();
		return typeof file === "string" && file.length > 0 ? file : undefined;
	} catch {
		return undefined;
	}
}

function ownSessionId(ctx: HandlerCtx): string {
	try {
		const id = ctx.sessionManager.getSessionId();
		return typeof id === "string" ? id.trim() : "";
	} catch {
		return "";
	}
}

/**
 * The conversation a child session's media belongs to. A child session never
 * owns the pane: its items are filed under the parent conversation instead.
 */
function parentConversation(ctx: HandlerCtx): string | undefined {
	let header: { parentSession?: unknown } | undefined;
	try {
		header = ctx.sessionManager.getHeader();
	} catch {
		return undefined;
	}
	if (!header || typeof header !== "object" || !("parentSession" in header)) return undefined;
	const parent = header.parentSession;
	if (typeof parent !== "string" || parent.length === 0) return undefined;
	// The header records the parent as a session id or as a session file.
	if (parent.includes("/") || parent.endsWith(".jsonl")) {
		return owner !== undefined && owner.file === parent ? owner.ref.id : undefined;
	}
	return parent;
}

function sessionRef(ctx: HandlerCtx): SessionRef | undefined {
	const isMain = ctx.agent === undefined || ctx.agent.kind !== "sub";
	let id: string;
	if (isMain) {
		id = ownSessionId(ctx);
	} else {
		id = parentConversation(ctx) ?? owner?.ref.id ?? "";
		if (id.length === 0) return undefined;
	}
	if (id.length === 0) id = `terminal:${HOST}:${WINDOW_KEY}:${PANE_ID}`;
	const name = ctx.sessionManager.getSessionName();
	const ownLabel = typeof name === "string" && name.trim().length > 0 ? name.trim() : undefined;
	// A child session's items keep the owner conversation's label; only the
	// owning conversation's own events may rename it.
	const inherited = isMain ? undefined : owner?.ref.label;
	const label = inherited ?? ownLabel ?? (basename(ctx.cwd) || id);
	const ref: SessionRef = { id, label, cwd: ctx.cwd, isMain };
	if (isMain) owner = { ref, file: sessionFile(ctx) };
	return ref;
}

/** The workspace fields a descriptor names, always the CLI's own values. */
function workspaceFields(payload: Record<string, unknown>, workspace: TernWorkspace | undefined): void {
	if (workspace === undefined) return;
	payload.tern_session_id = workspace.id;
	if (workspace.name !== undefined && workspace.name.length > 0) payload.tern_session_name = workspace.name;
}

/**
 * The binding descriptor: only the owning conversation's touch may name the
 * pane, so no other write can move what the pane is bound to.
 */
function bindingPayload(ref: SessionRef, workspace: TernWorkspace | undefined): Record<string, unknown> {
	if (!ref.isMain) return passivePayload(ref, workspace);
	const payload: Record<string, unknown> = {
		id: ref.id, label: ref.label, cwd: ref.cwd, host: HOST, pane_id: PANE_ID,
	};
	if (WINDOW_KEY.length > 0) payload.window_key = WINDOW_KEY;
	workspaceFields(payload, workspace);
	return payload;
}

/**
 * The passive descriptor for a capture, a child session or a transcript
 * import. It never names the pane or the window, so storing media cannot
 * restore a stale pane binding; the fields it omits are preserved, not blanked.
 */
function passivePayload(ref: SessionRef, workspace: TernWorkspace | undefined = workspaces.get(ref.id)): Record<string, unknown> {
	const payload: Record<string, unknown> = { id: ref.id, host: HOST };
	workspaceFields(payload, workspace);
	return payload;
}

// ============================================================================
// Extension
// ============================================================================

export default function ternMedia(pi: ExtensionAPI): void {
	if (PANE_ID.length === 0) return;

	logWarning = (message: string): void => {
		try {
			pi.logger.warn(message);
		} catch {
			// Logging must never break the session.
		}
	};

	const state: SessionState = {};

	const touch = async (ctx: HandlerCtx, ref: SessionRef, force: boolean): Promise<void> => {
		if (!force && state.touchedId === ref.id && state.touchedLabel === ref.label) return;
		const workspace = await cliTernWorkspace();
		if (workspace === undefined) {
			// Never guess a workspace: media stays filed under the conversation
			// and no query widens to every session.
			report(
				ctx,
				"Tern media: this window's Tern session could not be resolved (tern ls --json); media stays filed per conversation",
				"warning",
			);
		} else {
			workspaces.set(ref.id, workspace);
		}
		const result = await callBackend("touch", { session: bindingPayload(ref, workspace) }, TOUCH_TIMEOUT_MS);
		if (!result.ok) {
			report(ctx, `Tern media: session binding failed: ${result.error}`, "warning", "Tern media: session binding request failed");
			return;
		}
		state.touchedId = ref.id;
		state.touchedLabel = ref.label;
	};

	/**
	 * Bind this pane to the conversation that owns it. Only a main session may
	 * bind; a child session's items are filed under the parent conversation
	 * without touching the binding. Every bind goes through one queue, so rapid
	 * start/switch/branch events commit in the order they arrived.
	 */
	const bind = (ctx: HandlerCtx): Promise<void> => {
		const ref = sessionRef(ctx);
		if (ref === undefined || !ref.isMain) return Promise.resolve();
		return bindings.enqueue(() => touch(ctx, ref, true));
	};

	const backfill = async (ctx: HandlerCtx, ref: SessionRef, file: string): Promise<void> => {
		// An import files media under the conversation; it must land after the
		// binding writes that came before it, and never name the pane itself.
		await bindings.settled();
		const result = await callBackend(
			"backfill",
			{ session: passivePayload(ref), transcript_path: file },
			BACKFILL_TIMEOUT_MS,
		);
		if (!result.ok) {
			report(ctx, `Tern media: historical import failed: ${result.error}`, "warning", "Tern media: historical import request failed");
			return;
		}
		const stored = result.data.items.length;
		const unresolved = result.data.errors.length;
		if (stored === 0 && unresolved === 0) {
			report(ctx, "Tern media: historical import found no media in that transcript", "info");
			return;
		}
		const summary = backfillSummary(stored, unresolved, result.data.errors);
		report(ctx, summary.message, unresolved > 0 ? "warning" : "info", summary.log);
	};



	/**
	 * Keep only items whose file exists, resolved against the session's cwd.
	 * Incidental paths named by tools simply drop out; explicit imports are
	 * validated in the command so a typo is reported there instead.
	 */
	const localize = (ctx: HandlerCtx, items: MediaItemRequest[]): MediaItemRequest[] => {
		const localized: MediaItemRequest[] = [];
		for (const item of items) {
			if (item.path === undefined) {
				localized.push(item);
				continue;
			}
			const expanded = item.path.startsWith("~") ? join(homedir(), item.path.slice(1)) : item.path;
			const path = isAbsolute(expanded) ? expanded : resolve(ctx.cwd, expanded);
			if (!existsSync(path)) continue;
			localized.push({ ...item, path, name: item.name ?? basename(path) });
		}
		return localized;
	};

	const capture = async (ctx: HandlerCtx, items: MediaItemRequest[]): Promise<void> => {
		if (items.length === 0) return;
		const accepted = items.filter((item) => item.data === undefined || item.data.length <= MAX_IMAGE_CHARS);
		if (accepted.length !== items.length) {
			report(ctx, `Tern media: skipped ${items.length - accepted.length} oversized image(s)`, "warning");
		}
		if (accepted.length === 0) return;
		const ref = sessionRef(ctx);
		if (!ref) {
			report(ctx, "Tern media: no conversation identity; media was not stored", "warning");
			return;
		}
		// The register must land after every binding write that came before it,
		// and must never name the pane: a late capture cannot restore a pane the
		// session has since switched away from. Capture before the next tool can
		// overwrite the same screenshot path.
		await bindings.settled();
		const result = await callBackend("register", {
			session: passivePayload(ref), items: accepted,
		}, REGISTER_TIMEOUT_MS);
		reportRegister(ctx, "capture", result, accepted.length, false);
	};

	/** Register one explicit item now, reporting the backend's answer. */
	const registerNow = async (ctx: HandlerCtx, item: MediaItemRequest, label: string): Promise<void> => {
		const ref = sessionRef(ctx) ?? owner?.ref;
		if (ref === undefined) {
			report(ctx, "Tern media: no conversation identity yet; try again in a session", "warning");
			return;
		}
		await bindings.settled();
		const result = await callBackend(
			"register",
			{ session: passivePayload(ref), items: [item] },
			REGISTER_TIMEOUT_MS,
		);
		reportRegister(ctx, label, result, 1, true);
	};

	const resolveUserPath = (ctx: HandlerCtx, raw: string): string => {
		const path = raw.trim();
		const expanded = path.startsWith("~/") ? join(homedir(), path.slice(2)) : path;
		return isAbsolute(expanded) ? expanded : resolve(ctx.cwd, expanded);
	};

	const commandOpen = (ctx: HandlerCtx): void => {
		// The window plugin owns `plugin.media.open`. Tern's CLI has no verb that
		// dispatches a window action (checked: plugin, ls, run, send, process; the
		// `ctl` endpoint needs a window started with --control), and the action
		// stream is window-plugin-only API, so the honest path is the palette
		// action itself rather than a guessed escape sequence.
		ctx.ui.notify(
			`Tern media: open the gallery with the Tern command palette action ${PALETTE_ACTION} ("Media: Open session gallery") for this conversation.`,
			"info",
		);
	};

	const commandAdd = async (ctx: HandlerCtx, args: string): Promise<void> => {
		const raw = args.trim();
		if (raw.length === 0) {
			report(ctx, "Tern media: usage /media-add PATH", "warning");
			return;
		}
		const path = resolveUserPath(ctx, raw);
		if (!existsSync(path)) {
			report(ctx, `Tern media: no such file: ${path}`, "warning", "Tern media: import path not found");
			return;
		}
		await registerNow(ctx, { path, name: basename(path), source: "transferred" }, "import");
	};

	const commandTag = async (ctx: HandlerCtx, args: string): Promise<void> => {
		const parts = args.trim().split(/\s+/).filter((part) => part.length > 0);
		const id = Number(parts[0]);
		const tags = parts.slice(1).join(" ").split(/[,\s]+/).filter((tag) => tag.length > 0);
		if (!Number.isInteger(id) || id <= 0 || tags.length === 0) {
			report(ctx, "Tern media: usage /media-tag ID TAG[,TAG...]", "warning");
			return;
		}
		const result = await callBackend("tag", { id, tags }, TOUCH_TIMEOUT_MS);
		if (!result.ok) {
			report(ctx, `Tern media: tagging item ${id} failed: ${result.error}`, "error", "Tern media: tag request failed");
			return;
		}
		report(ctx, `Tern media: item ${id} tags set to ${tags.join(", ")}`, "info");
	};

	const commandWatch = async (ctx: HandlerCtx, args: string): Promise<void> => {
		const raw = args.trim();
		if (raw.length === 0) {
			report(ctx, "Tern media: usage /media-watch DIR", "warning");
			return;
		}
		const path = resolveUserPath(ctx, raw);
		let isDirectory = false;
		try {
			isDirectory = existsSync(path) && statSync(path).isDirectory();
		} catch {
			isDirectory = false;
		}
		if (!isDirectory) {
			report(ctx, `Tern media: not a directory: ${path}`, "warning", "Tern media: watch path is not a directory");
			return;
		}
		const ref = sessionRef(ctx) ?? owner?.ref;
		if (ref === undefined) {
			report(ctx, "Tern media: no conversation identity yet; try again in a session", "warning");
			return;
		}
		const result = await callBackend("watch", { session_id: ref.id, path }, TOUCH_TIMEOUT_MS);
		if (!result.ok) {
			report(ctx, `Tern media: watching ${path} failed: ${result.error}`, "error", "Tern media: watch request failed");
			return;
		}
		report(ctx, `Tern media: watching ${path}`, "info");
	};

	const commandBackfill = async (ctx: HandlerCtx, args: string): Promise<void> => {
		const raw = args.trim();
		const ref = sessionRef(ctx) ?? owner?.ref;
		if (ref === undefined) {
			report(ctx, "Tern media: no conversation identity yet; try again in a session", "warning");
			return;
		}
		const file = raw.length > 0 ? resolveUserPath(ctx, raw) : sessionFile(ctx);
		if (file === undefined) {
			report(ctx, "Tern media: this session has no transcript file yet; pass one as /media-backfill FILE", "warning");
			return;
		}
		if (!existsSync(file)) {
			report(ctx, `Tern media: no such transcript: ${file}`, "warning", "Tern media: transcript path not found");
			return;
		}
		await backfill(ctx, ref, file);
	};

	const usage = [
		"Tern media:",
		"/media open                      open the gallery (palette action)",
		"/media-add PATH                  store a local media file here",
		"/media-tag ID TAG[,TAG...]       replace an item's tags",
		"/media-watch DIR                 associate a directory with this conversation",
		"/media-backfill [FILE]           import media referenced by a transcript",
	].join("\n");

	const dispatch = async (ctx: HandlerCtx, args: string): Promise<void> => {
		const trimmed = args.trim();
		const space = trimmed.indexOf(" ");
		const verb = (space < 0 ? trimmed : trimmed.slice(0, space)).toLowerCase();
		const rest = space < 0 ? "" : trimmed.slice(space + 1);
		switch (verb) {
			case "":
			case "open":
				commandOpen(ctx);
				return;
			case "add":
				await commandAdd(ctx, rest);
				return;
			case "tag":
				await commandTag(ctx, rest);
				return;
			case "watch":
				await commandWatch(ctx, rest);
				return;
			case "backfill":
				await commandBackfill(ctx, rest);
				return;
			default:
				report(ctx, usage, "info");
		}
	};

	pi.registerCommand("media", {
		description: "Tern media gallery: open, add, tag, watch, backfill",
		handler: async (args, ctx) => dispatch(ctx, args),
	});
	pi.registerCommand("media-add", {
		description: "Store a local media file in the Tern media gallery for this conversation",
		handler: async (args, ctx) => commandAdd(ctx, args),
	});
	pi.registerCommand("media-tag", {
		description: "Replace the tags of a Tern media item: /media-tag ID TAG[,TAG...]",
		handler: async (args, ctx) => commandTag(ctx, args),
	});
	pi.registerCommand("media-watch", {
		description: "Associate a directory with this conversation in the Tern media gallery",
		handler: async (args, ctx) => commandWatch(ctx, args),
	});
	pi.registerCommand("media-backfill", {
		description: "Import media referenced by a session transcript into the Tern media gallery",
		handler: async (args, ctx) => commandBackfill(ctx, args),
	});

	pi.on("session_start", async (_event, ctx) => {
		await bind(ctx);
	});
	pi.on("session_switch", async (_event, ctx) => {
		await bind(ctx);
	});
	pi.on("session_branch", async (_event, ctx) => {
		await bind(ctx);
	});
	// Re-touch only when the label actually changed (auto-titles settle late).
	pi.on("agent_end", async (_event, ctx) => {
		const ref = sessionRef(ctx);
		if (ref !== undefined && ref.isMain) await bindings.enqueue(() => touch(ctx, ref, false));
	});
	pi.on("tool_result", async (event, ctx) => {
		await capture(ctx, localize(ctx, itemsFromToolResult(event)));
	});
	pi.on("message_end", async (event, ctx) => {
		await capture(ctx, localize(ctx, itemsFromMessage(event.message)));
	});
}

// ============================================================================
// Self-check: `bun omp-extension.ts` (or `... --self-check`)
// ============================================================================

interface Check {
	name: string;
	ok: boolean;
	detail?: string;
}

function sameStrings(actual: readonly string[], expected: readonly string[]): boolean {
	return actual.length === expected.length && actual.every((value, index) => value === expected[index]);
}

export function selfCheck(): Check[] {
	const checks: Check[] = [];
	const text = [
		"Saved chart to /tmp/out/chart.png and clip out/clip.mp4",
		"shot ![shot](file:///Users/x/shot.webp) and (./img/diagram.webp)",
		"remote https://example.com/a.png",
		"inline data:image/png;base64,AAAA",
		"code src/app.ts and package.json and notes.md",
	].join("\n");

	const paths = extractMediaPaths(text, 8);
	const expectedPaths = ["/tmp/out/chart.png", "out/clip.mp4", "./img/diagram.webp"];
	checks.push({
		name: "extractMediaPaths keeps local media and drops remote/data/text",
		ok: sameStrings(paths, expectedPaths),
		detail: JSON.stringify(paths),
	});
	checks.push({ name: "extractMediaPaths respects the limit", ok: extractMediaPaths(text, 2).length === 2 });
	checks.push({
		name: "extractMediaPaths preserves distinct filename case",
		ok: sameStrings(extractMediaPaths("/a/b.png /a/b.png /a/b.PNG", 8), ["/a/b.png", "/a/b.PNG"]),
	});
	checks.push({
		name: "media paths preserve spaces and email directory names",
		ok: sameStrings(extractMediaPaths('Saved `/tmp/name@example.com/Before screenshot.png`', 8),
			["/tmp/name@example.com/Before screenshot.png"]),
	});
	checks.push({
		name: "mediaKindForPath classifies and rejects",
		ok:
			mediaKindForPath("shot.PNG") === "image" &&
			mediaKindForPath("clip.mp4") === "video" &&
			mediaKindForPath("song.flac") === "audio" &&
			mediaKindForPath("paper.pdf") === "document" &&
			mediaKindForPath("src/app.ts") === undefined &&
			mediaKindForPath("/dir.with.dots/file") === undefined &&
			isMediaPath("README") === false,
	});

	const readEvent = {
		toolName: "read",
		input: { path: "/tmp/preview.png" },
		content: [{ type: "image", data: "aGk=", mimeType: "image/png" }],
		isError: false,
	};
	const readItems = itemsFromToolResult(readEvent, 12);
	checks.push({
		name: "an image read retains its source once, not its returned preview",
		ok:
			readItems.length === 1 &&
			readItems[0]?.path === "/tmp/preview.png" &&
			readItems[0]?.data === undefined &&
			readItems[0]?.source === "viewed",
		detail: JSON.stringify(readItems.map((item) => [item.path, item.mime, item.source])),
	});
	checks.push({
		name: "tool result: failure contributes nothing",
		ok: itemsFromToolResult({ ...readEvent, isError: true }).length === 0,
	});

	const writeItems = itemsFromToolResult({
		toolName: "write",
		input: { file_path: "/tmp/out/render.png" },
		content: [{ type: "text", text: "wrote /tmp/out/render.png" }],
		isError: false,
	});
	checks.push({
		name: "tool result: write is labelled created once",
		ok: writeItems.length === 1 && writeItems[0]?.path === "/tmp/out/render.png" && writeItems[0]?.source === "created",
		detail: JSON.stringify(writeItems),
	});
	checks.push({
		name: "tool result: text media of a non-file tool is viewed",
		ok:
			itemsFromToolResult({
				toolName: "bash",
				input: { command: "ls /tmp" },
				content: [{ type: "text", text: "montage.png" }],
				isError: false,
			})
				.map((item) => item.source)
				.join(",") === "viewed",
	});

	const userItems = itemsFromMessage({
		role: "user",
		content: [
			{ type: "text", text: "look" },
			{ type: "image", data: "data:image/png;base64,aGk=", mimeType: "image/png" },
		],
	});
	checks.push({
		name: "message: user image is transferred and unwrapped",
		ok:
			userItems.length === 1 &&
			userItems[0]?.data === "aGk=" &&
			userItems[0]?.mime === "image/png" &&
			userItems[0]?.source === "transferred",
		detail: JSON.stringify(userItems),
	});
	checks.push({
		name: "message: toolResult role and text-only turns contribute nothing",
		ok:
			itemsFromMessage({ role: "toolResult", content: [{ type: "image", data: "aGk=", mimeType: "image/png" }] }).length === 0 &&
			itemsFromMessage({ role: "user", content: "just text" }).length === 0,
	});
	checks.push({
		name: "image blocks without an image mime are refused",
		ok:
			imageItemFromBlock({ type: "image", data: "aGk=" }, "viewed") === undefined &&
			imageItemFromBlock({ type: "image", data: "aGk=", mimeType: "application/pdf" }, "viewed") === undefined &&
			imageItemFromBlock({ type: "text", text: "hi" }, "viewed") === undefined,
	});

	const mainRef: SessionRef = { id: "conv-1", label: "Work", cwd: "/tmp", isMain: true };
	const listJson = JSON.stringify({
		sessions: [
			{ id: 4, name: "Default", shown: false, tabs: [{ id: 12, blocks: [{ id: 11 }] }] },
			{ id: 2972117368835, name: "Work", shown: true, tabs: [{ id: 13, blocks: [{ id: 1679332212743 }] }] },
		],
		detached: [],
	});
	checks.push({
		name: "the workspace is the session holding this pane, named exactly",
		ok:
			ternWorkspaceFromListJson(listJson, "11")?.id === "4" &&
			ternWorkspaceFromListJson(listJson, "1679332212743")?.id === "2972117368835" &&
			ternWorkspaceFromListJson(listJson, "1679332212743")?.name === "Work" &&
			ternWorkspaceFromListJson(listJson, "987654321") === undefined &&
			ternWorkspaceFromListJson(listJson, "") === undefined &&
			ternWorkspaceFromListJson("{ not json", "11") === undefined,
	});
	checks.push({
		name: "a native id that cannot be exact is refused, never truncated",
		ok:
			ternWorkspaceFromListJson('{"sessions":[{"id":1e999,"tabs":[{"blocks":[{"id":7}]}]}]}', "7") === undefined &&
			ternWorkspaceFromListJson('{"sessions":[{"id":4.5,"tabs":[{"blocks":[{"id":7}]}]}]}', "7") === undefined,
	});


	const urlRejection = registerSummary("capture", 1, 2, [
		{ name: "https://files.example/scan.png?private_marker=hidden", error: "unsupported type" },
	]);
	const pathRejection = registerSummary("capture", 1, 2, [{ name: "/tmp/other scan.gif", error: "unsupported type" }]);
	checks.push({
		name: "rejected references stay out of notifications and logs",
		ok:
			urlRejection.message === pathRejection.message &&
			urlRejection.log === pathRejection.log &&
			!/:\/\/|\?|private_marker|scan/.test(urlRejection.message + urlRejection.log),
		detail: urlRejection.log,
	});
	const imported = backfillSummary(2, 1, [{ name: "https://media.example/take.wav?k=1", error: "too large" }]);
	checks.push({
		name: "a transcript import logs counts, never the reference it rejected",
		ok:
			imported.log === backfillSummary(2, 1, []).log &&
			!/:\/\/|\?|\.wav|example/.test(imported.message + imported.log),
		detail: imported.log,
	});
	return checks;
}

/**
 * Checks that need the event loop: a binding write must commit in the order its
 * event arrived even when an earlier write is the slowest one.
 */
async function asyncChecks(): Promise<Check[]> {
	const checks: Check[] = [];
	const queue = createSerialQueue();
	const committed: string[] = [];
	const write = (name: string, delayMs: number): Promise<void> =>
		queue.enqueue(() => new Promise<void>((done) => setTimeout(() => (committed.push(name), done()), delayMs)));
	await Promise.all([write("A", 40), write("B", 0), write("C", 20)]);
	await queue.settled();
	checks.push({
		name: "binding writes commit in event order under an out-of-order backend",
		ok: committed.join(",") === "A,B,C",
		detail: committed.join(","),
	});

	const recovered = createSerialQueue();
	const ran: string[] = [];
	await recovered.enqueue(async () => {
		throw new Error("backend unavailable");
	}).catch(() => undefined);
	await recovered.enqueue(async () => {
		ran.push("after failure");
	});
	checks.push({
		name: "a failed binding write does not stall the writes behind it",
		ok: ran.join(",") === "after failure",
		detail: ran.join(","),
	});
	return checks;
}

/** Every check, including the ones that drive the queue through real delays. */
export async function selfCheckAsync(): Promise<Check[]> {
	return [...selfCheck(), ...(await asyncChecks())];
}

const argv: string[] = process.argv ?? [];
const invokedDirectly =
	argv.includes("--self-check") ||
	(typeof argv[1] === "string" && /(^|[\\/])omp-extension\.(ts|mts|js|mjs)$/.test(argv[1]));

if (invokedDirectly) {
	void selfCheckAsync().then(
		(results) => {
			const failed = results.filter((result) => !result.ok);
			for (const result of results) {
				const detail = result.ok || result.detail === undefined ? "" : ` -> ${result.detail}`;
				console.log(`${result.ok ? "ok  " : "FAIL"} ${result.name}${detail}`);
			}
			console.log(`${results.length - failed.length}/${results.length} checks passed`);
			if (failed.length > 0) process.exitCode = 1;
		},
		(error) => {
			console.log(`FAIL self-check crashed: ${describeError(error)}`);
			process.exitCode = 1;
		},
	);
}
