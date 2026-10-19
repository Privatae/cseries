/*
 * fsa-bridge.js — FSA polyfill that routes to the sidecar's Python FS.
 *
 * Injected via Playwright `addInitScript` into every page (including
 * iframes) before any navigation. Overrides window.showDirectoryPicker
 * so claude.ai/design's "local codebase" button returns a directory
 * handle backed by the entity's mounted /var/lib/cseries volume.
 *
 * The bridge uses Playwright `expose_binding` functions on window
 * (prefix __cseries_fs_*) instead of HTTP — same process, no CORS, no
 * mixed-content fights, no PNA blocks. Each call awaits Python.
 *
 * Implements the WICG File System Access spec to the depth Claude
 * Design uses. Methods are added as we observe what gets called.
 *
 * Spec: https://wicg.github.io/file-system-access/
 */
(function (root) {
    "use strict";

    if (root.__cseries_fsa_installed) return;
    root.__cseries_fsa_installed = true;

    /* ── Helpers ─────────────────────────────────────────────────── */

    function _throwFromError(err) {
        // Python side throws PolyfillFSError; Playwright surfaces it as
        // a JS Error with err.message containing JSON-ish text. We try
        // to extract the code if present; otherwise pass through.
        var code = "InvalidStateError";
        var message = String(err && err.message || err);
        var m = message.match(/^([A-Z][A-Za-z]+Error):\s*(.+)$/);
        if (m) { code = m[1]; message = m[2]; }
        throw new DOMException(message, code);
    }

    async function _call(binding, ...args) {
        var fn = root[binding];
        if (typeof fn !== "function") {
            throw new DOMException(
                "cseries FS bridge not exposed: " + binding,
                "InvalidStateError"
            );
        }
        try { return await fn(...args); }
        catch (e) { _throwFromError(e); }
    }

    /* ── FileSystemFileHandle ────────────────────────────────────── */

    function _file(relPath, name) {
        var handle = {
            kind: "file",
            name: name,
            __rel: relPath,

            isSameEntry: async function (other) {
                return other && other.__rel === relPath;
            },

            getFile: async function () {
                var resp = await _call("__cseries_fs_read", relPath);
                var bytes = _b64ToBytes(resp.data_b64);
                var blob = new Blob([bytes]);
                // Construct a real File so MIME-typed code paths work.
                var file = new File([blob], resp.name, {
                    lastModified: resp.last_modified_ms,
                });
                return file;
            },

            createWritable: async function (options) {
                // We buffer the writes in memory; on close() we flush
                // to Python in one call. Simpler than streaming chunks
                // back, and matches what claude.ai/design actually does
                // (small written files in our use case).
                var buffer = [];
                var closed = false;
                var stream = {
                    locked: false,
                    write: async function (chunk) {
                        if (closed) throw new DOMException("stream closed", "InvalidStateError");
                        if (chunk == null) return;
                        if (chunk.type === "write" && chunk.data != null) {
                            chunk = chunk.data;
                        }
                        if (typeof chunk === "string") {
                            buffer.push(new TextEncoder().encode(chunk));
                        } else if (chunk instanceof Uint8Array) {
                            buffer.push(chunk);
                        } else if (chunk instanceof ArrayBuffer) {
                            buffer.push(new Uint8Array(chunk));
                        } else if (chunk instanceof Blob) {
                            buffer.push(new Uint8Array(await chunk.arrayBuffer()));
                        } else if (chunk && chunk.buffer instanceof ArrayBuffer) {
                            buffer.push(new Uint8Array(chunk.buffer));
                        } else {
                            throw new DOMException("unsupported chunk type", "TypeError");
                        }
                    },
                    seek: async function (_pos) {
                        throw new DOMException("seek not supported", "NotSupportedError");
                    },
                    truncate: async function (_size) {
                        // Truncate-to-size before any writes is a common pattern;
                        // since we replace the file wholesale on close, we
                        // approximate by clearing the buffer.
                        buffer = [];
                    },
                    abort: async function () { closed = true; buffer = []; },
                    close: async function () {
                        if (closed) return;
                        closed = true;
                        var total = 0;
                        for (var i = 0; i < buffer.length; i++) total += buffer[i].length;
                        var merged = new Uint8Array(total);
                        var off = 0;
                        for (var j = 0; j < buffer.length; j++) {
                            merged.set(buffer[j], off);
                            off += buffer[j].length;
                        }
                        var b64 = _bytesToB64(merged);
                        await _call("__cseries_fs_write", relPath, b64, true);
                    },
                };
                return stream;
            },

            queryPermission: async function () { return "granted"; },
            requestPermission: async function () { return "granted"; },
        };
        return handle;
    }

    /* ── FileSystemDirectoryHandle ───────────────────────────────── */

    function _dir(relPath, name) {
        function _childRel(childName) {
            return relPath ? (relPath + "/" + childName) : childName;
        }

        var handle = {
            kind: "directory",
            name: name,
            __rel: relPath,

            isSameEntry: async function (other) {
                return other && other.__rel === relPath;
            },

            entries: function () {
                // Async iterator. Each value is [name, handle].
                return (async function* () {
                    var list = await _call("__cseries_fs_list", relPath);
                    for (var i = 0; i < list.length; i++) {
                        var e = list[i];
                        var cRel = _childRel(e.name);
                        if (e.kind === "directory") {
                            yield [e.name, _dir(cRel, e.name)];
                        } else {
                            yield [e.name, _file(cRel, e.name)];
                        }
                    }
                })();
            },

            keys: function () {
                return (async function* () {
                    var list = await _call("__cseries_fs_list", relPath);
                    for (var i = 0; i < list.length; i++) yield list[i].name;
                })();
            },

            values: function () {
                return (async function* () {
                    var list = await _call("__cseries_fs_list", relPath);
                    for (var i = 0; i < list.length; i++) {
                        var e = list[i];
                        var cRel = _childRel(e.name);
                        yield e.kind === "directory" ? _dir(cRel, e.name)
                                                     : _file(cRel, e.name);
                    }
                })();
            },

            getFileHandle: async function (childName, options) {
                var create = !!(options && options.create);
                var cRel = _childRel(childName);
                try {
                    var info = await _call("__cseries_fs_stat", cRel);
                    if (info.kind !== "file") {
                        throw new DOMException(
                            "exists but is not a file: " + childName,
                            "TypeMismatchError"
                        );
                    }
                    return _file(cRel, childName);
                } catch (e) {
                    if (e && e.name === "NotFoundError" && create) {
                        // Create the file empty + return a handle.
                        await _call("__cseries_fs_write", cRel, "", true);
                        return _file(cRel, childName);
                    }
                    throw e;
                }
            },

            getDirectoryHandle: async function (childName, options) {
                var create = !!(options && options.create);
                var cRel = _childRel(childName);
                try {
                    var info = await _call("__cseries_fs_stat", cRel);
                    if (info.kind !== "directory") {
                        throw new DOMException(
                            "exists but is not a directory: " + childName,
                            "TypeMismatchError"
                        );
                    }
                    return _dir(cRel, childName);
                } catch (e) {
                    if (e && e.name === "NotFoundError" && create) {
                        await _call("__cseries_fs_mkdir", cRel, true);
                        return _dir(cRel, childName);
                    }
                    throw e;
                }
            },

            removeEntry: async function (childName, options) {
                var recursive = !!(options && options.recursive);
                await _call("__cseries_fs_remove", _childRel(childName), recursive);
            },

            resolve: async function (descendant) {
                if (!descendant || !descendant.__rel) return null;
                if (!descendant.__rel.startsWith(relPath ? relPath + "/" : "")) {
                    return null;
                }
                var tail = descendant.__rel.slice(relPath ? relPath.length + 1 : 0);
                return tail ? tail.split("/") : [];
            },

            queryPermission: async function () { return "granted"; },
            requestPermission: async function () { return "granted"; },
        };

        // Async iteration on the directory itself yields [name, handle]
        // (matches the WICG spec where the dir handle is itself iterable).
        handle[Symbol.asyncIterator] = handle.entries;
        return handle;
    }

    /* ── showDirectoryPicker override ────────────────────────────── */

    var originalShowDirectoryPicker = root.showDirectoryPicker;

    root.showDirectoryPicker = async function (_options) {
        var grant;
        try { grant = await _call("__cseries_fs_current_grant"); }
        catch (e) { _throwFromError(e); }
        if (!grant || !grant.granted) {
            throw new DOMException(
                "no directory granted (call POST /grant-fs on the sidecar first)",
                "AbortError"
            );
        }
        return _dir("", grant.name);
    };

    // showOpenFilePicker — same dance but returns an array of file handles.
    // claude.ai/design has been reported to use this for "upload one file"
    // flows. Keep parity: rooted at the granted dir, file selection skipped
    // (returns all files in the granted root for now; a richer picker
    // ships when we have a UI to drive it).
    root.showOpenFilePicker = async function (_options) {
        var grant = await _call("__cseries_fs_current_grant");
        if (!grant || !grant.granted) {
            throw new DOMException("no directory granted", "AbortError");
        }
        var list = await _call("__cseries_fs_list", "");
        var files = list.filter(function (e) { return e.kind === "file"; });
        if (!files.length) {
            throw new DOMException("no files in granted directory", "AbortError");
        }
        // Return a single file handle (first file) — matches the
        // single-selection picker shape. Multi-select gets a UI later.
        return [_file(files[0].name, files[0].name)];
    };

    /* ── Base64 helpers (Uint8Array ↔ ascii-safe string) ────────── */

    function _b64ToBytes(b64) {
        var bin = atob(b64 || "");
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return bytes;
    }
    function _bytesToB64(bytes) {
        // Chunk to avoid call-stack blowup on large arrays.
        var CHUNK = 0x8000;
        var pieces = [];
        for (var i = 0; i < bytes.length; i += CHUNK) {
            pieces.push(String.fromCharCode.apply(
                null, bytes.subarray(i, i + CHUNK)
            ));
        }
        return btoa(pieces.join(""));
    }

    /* ── Debug hook ──────────────────────────────────────────────── */
    root.__cseries_fsa_version = "0.2.0";
})(window);
