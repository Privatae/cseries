# Upstream licence sources for the C-Series NOTICE

Fetched 2026-10-15 (sections 5 and 9: 2026-10-19). Every text in this directory was downloaded verbatim from the URL listed next to it; none was written by hand. "Status" means:

- **verified**: the text comes from the exact version or commit that ships, and the version was confirmed against the shipped artifact (hash, `go version -m`, a pinned build file, or the image).
- **closest-tag**: the text is from the nearest upstream tag because the shipped copy has no version.
- **unverified**: no upstream text could be obtained.

Where the repo artifact was compared against upstream, the check is listed under "Evidence".

---

## 1. Oniguruma (inside the shiki WebAssembly bundle)

**Evidence.** I base64-decoded the wasm in `system-apps/circles/assets/wasm-BnjxR4X6.js` (the workbench copy is byte-identical according to inventory.csv). Its sha256 is `fd885c2d12e5951e59d761ebd4a006e06254b1491fd6f530c92b69fb4d8d77d9`, which matches `release/onig.wasm` in the npm package `vscode-oniguruma@1.7.0` byte for byte. `@shikijs/engine-oniguruma@4.2.0` (circles-web/package-lock.json) declares `"devDependencies": {"vscode-oniguruma": "1.7.0"}` in its package.json and inlines that wasm. In the git tree at vscode-oniguruma tag `v1.7.0` (commit 716aeaa2), the submodule `deps/oniguruma` points to kkos/oniguruma commit `08d36110c5670c815ad6d6f969e578049d209080`. The GitHub compare API reports that commit as identical to tag `v6.9.8`, and `src/oniguruma.h:37-39` at that commit defines 6.9.8.

**Surprise.** vscode-oniguruma's own NOTICES.txt says "oniguruma version 6.9.5_rev1". That is stale: the pinned submodule is 6.9.8. The wasm also contains Microsoft's MIT-licensed glue code (`onig.cc`: `createOnigScanner`, `findNextOnigScannerMatch`), so vscode-oniguruma is a second component inside the binary, separate from shiki's own MIT licence.

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| oniguruma-6.9.8.LICENSE | Oniguruma (K.Kosako) | 6.9.8 (commit 08d36110) | BSD-2-Clause | https://raw.githubusercontent.com/kkos/oniguruma/v6.9.8/COPYING | verified |
| vscode-oniguruma-1.7.0.LICENSE | vscode-oniguruma (Microsoft) | 1.7.0 | MIT | https://raw.githubusercontent.com/microsoft/vscode-oniguruma/v1.7.0/LICENSE.txt (byte-identical to the npm tarball's LICENSE.txt) | verified |
| vscode-oniguruma-1.7.0.NOTICE | vscode-oniguruma third-party notices | 1.7.0 | (notice) | https://raw.githubusercontent.com/microsoft/vscode-oniguruma/v1.7.0/NOTICES.txt (byte-identical to npm) | verified |

**Duty.** BSD-2-Clause §2 requires a binary redistribution to reproduce the Oniguruma copyright notice, conditions and disclaimer in its documentation. MIT requires vscode-oniguruma's copyright and permission notice to be included.

## 2. Lucide / Feather (paths copied into dashboard/base/js/icons.js)

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| lucide-0.500.0.LICENSE | Lucide | 0.500.0 (lucide-react version in circles-web/package-lock.json) | ISC | https://raw.githubusercontent.com/lucide-icons/lucide/0.500.0/LICENSE (byte-identical to `package/LICENSE` in https://registry.npmjs.org/lucide-react/-/lucide-react-0.500.0.tgz) | verified |
| feather-4.29.2.LICENSE | Feather (Cole Bemis) | v4.29.2 (latest tag) | MIT | https://raw.githubusercontent.com/feathericons/feather/v4.29.2/LICENSE | closest-tag |

**Note.** Lucide's LICENSE carries only the ISC text plus the line "portions … held by Cole Bemis 2013-2022 as part of Feather (MIT)". It does not include the MIT text. I cannot tell which Feather release Lucide forked from, so I used the latest tag. The v4.28.0 LICENSE is the same apart from its copyright line ("2013-2017" instead of "2013-2023").

**Duty.** ISC: keep Lucide's copyright and permission notice. MIT: keep Feather's copyright and permission notice.

## 3. shadcn/ui and AI Elements (copied component source)

**shadcn/ui origin.** All four `components.json` files use `$schema: https://ui.shadcn.com/schema.json` with `style: new-york`. I normalised whitespace and semicolons and dropped import lines, then compared `app-web-kit/ui/*.tsx` against `apps/v4/registry/new-york-v4/ui/*.tsx` at tag `shadcn@4.21.5`. Twelve of 15 files match exactly; dialog differs by 5 lines, textarea by 2 and command by 2. The workbench-web/circles-web `ui/*` files are re-export shims (`export * from "@kit/ui/input"`). shadcn components carry no per-component version, so I used the latest CLI tag.

**AI Elements origin.** The upstream is **vercel/ai-elements** (https://github.com/vercel/ai-elements, source in `packages/elements/src/`). I rewrote upstream's `@repo/shadcn-ui/` imports to `@/` and diffed every file at tag `ai-elements@1.9.1`. These match exactly: chain-of-thought, shimmer, sources, task and prompt-input. These have small local edits: conversation (2 lines) and reasoning (2). These are substantially modified: code-block (34), message (37/42) and tool (151/144). Tags 1.9.0, 1.9.1 and main give identical results, and every older tag diverges more.

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| shadcn-ui-4.21.5.LICENSE | shadcn/ui | registry at tag shadcn@4.21.5 | MIT | https://raw.githubusercontent.com/shadcn-ui/ui/shadcn@4.21.5/LICENSE.md | closest-tag (components are unversioned) |
| ai-elements-1.9.1.LICENSE | AI Elements (Vercel) | 1.9.x (1.9.0 and 1.9.1 are indistinguishable for these files) | Apache-2.0 | https://raw.githubusercontent.com/vercel/ai-elements/ai-elements@1.9.1/LICENSE | verified (origin and version by content match) |
| Apache-2.0.txt | full Apache License 2.0 text | — | Apache-2.0 | https://www.apache.org/licenses/LICENSE-2.0.txt | verified |

**Surprises.** (a) The repo's LICENSE is only the short Apache boilerplate ("Copyright 2023 Vercel, Inc. Licensed under the Apache License…"). GitHub reports it as `NOASSERTION`, and `packages/elements/package.json` has `license: null`. The published CLI `ai-elements@1.9.1` declares `"license": "Apache-2.0"` on npm. There is no NOTICE file (HTTP 404 at the tag). (b) AI Elements is **Apache-2.0, not MIT** like shadcn.

**Duty.** Apache-2.0 §4(a): include a copy of the licence (use Apache-2.0.txt). §4(b): the modified files (code-block, message, tool, conversation, reasoning) must carry prominent notices stating they were changed. §4(c): keep the Vercel copyright notice. MIT (shadcn): keep the copyright and permission notice.

## 4. curl_cffi 0.16.1: statically linked libcurl, BoringSSL, and more

**Evidence.** In the PyPI sdist (https://files.pythonhosted.org/packages/bb/df/8ef4b9a05139fdb6baf39ab44cc6529b91ef2425aab14d4f4a1dab940a56/curl_cffi-0.16.1.tar.gz), `Makefile:8-9` pins `VERSION := 2.1.1` (libcurl-impersonate) and `CURL_VERSION := curl-8_21_0`. In curl-impersonate v2.1.1 (https://github.com/lexiforest/curl-impersonate/archive/refs/tags/v2.1.1.tar.gz), `CMakeLists.txt:26-53` pins BoringSSL commit `156c7b75ae9b8c3b3f847acf264f17594c3859fb`, brotli 1.2.0, nghttp2 1.63.0, ngtcp2 1.20.0, nghttp3 1.15.0, zlib 1.3.1, zstd 1.5.7 and curl-8_21_0. `Makefile:10` pins libidn2 2.3.7. `CMakeLists.txt:69-74` turns USE_LIBIDN2 on by default on Linux. The image carries the wheel tagged `cp310-abi3-manylinux2014_x86_64`. A string scan of `curl_cffi/_wrapper.abi3.so` from `cseries:dev` shows build paths for every one of these: `build/deps/src/{boringssl,brotli,curl,libidn2,nghttp2,nghttp3,ngtcp2,zlib,zstd}`, plus `libidn2/unistring` and the banner `deflate 1.3.1`. Its only NEEDED libraries are libc, libpthread and ld-linux, so all of the above are linked statically.

**Surprise.** Besides curl and BoringSSL, the .so statically links brotli, nghttp2, ngtcp2, nghttp3, zlib, zstd and **libidn2 2.3.7 with its bundled libunistring subset (LGPL-3.0-or-later OR GPL-2.0-or-later)**. LGPL code is statically linked into a wheel that ships none of these licence texts.

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| curl_cffi-0.16.1.LICENSE | curl_cffi | 0.16.1 | MIT | https://raw.githubusercontent.com/lexiforest/curl_cffi/v0.16.1/LICENSE (byte-identical to sdist LICENSE) | verified |
| curl-impersonate-2.1.1.LICENSE | curl-impersonate (lexiforest) | v2.1.1 | MIT | https://raw.githubusercontent.com/lexiforest/curl-impersonate/v2.1.1/LICENSE (byte-identical to tag tarball) | verified |
| curl-8.21.0.LICENSE | curl / libcurl | 8.21.0 | curl | https://raw.githubusercontent.com/curl/curl/curl-8_21_0/COPYING | verified |
| boringssl-156c7b75ae9b.LICENSE | BoringSSL | commit 156c7b75ae9b8c3b3f847acf264f17594c3859fb (2026-07-08) | Apache-2.0 (the file also has a Go-licence section that, per its own text, covers TLS test-suite code not compiled into libcrypto/libssl) | https://raw.githubusercontent.com/google/boringssl/156c7b75ae9b8c3b3f847acf264f17594c3859fb/LICENSE | verified |
| brotli-1.2.0.LICENSE | brotli | 1.2.0 | MIT | https://raw.githubusercontent.com/google/brotli/v1.2.0/LICENSE | verified |
| nghttp2-1.63.0.LICENSE | nghttp2 | 1.63.0 | MIT | https://raw.githubusercontent.com/nghttp2/nghttp2/v1.63.0/COPYING | verified |
| ngtcp2-1.20.0.LICENSE | ngtcp2 | 1.20.0 | MIT | https://raw.githubusercontent.com/ngtcp2/ngtcp2/v1.20.0/COPYING | verified |
| nghttp3-1.15.0.LICENSE | nghttp3 | 1.15.0 | MIT | https://raw.githubusercontent.com/ngtcp2/nghttp3/v1.15.0/COPYING | verified |
| zlib-1.3.1.LICENSE | zlib | 1.3.1 | Zlib | https://raw.githubusercontent.com/madler/zlib/v1.3.1/LICENSE | verified |
| zstd-1.5.7.LICENSE, zstd-1.5.7.COPYING | zstd | 1.5.7 | BSD-3-Clause OR GPL-2.0-only | https://raw.githubusercontent.com/facebook/zstd/v1.5.7/LICENSE and …/COPYING | verified |
| libidn2-2.3.7.COPYING, .LICENSE-LGPL-3.0, .LICENSE-GPL-2.0, .LICENSE-Unicode | libidn2 (+ bundled unistring subset) | 2.3.7 | LGPL-3.0-or-later OR GPL-2.0-or-later (library); Unicode data under COPYING.unicode | https://ftp.gnu.org/gnu/libidn/libidn2-2.3.7.tar.gz (sha256 4c21a791b610b9519b9d0e12b8097bf2f359b12f8dd92647611a929e6bfd7d64); files extracted verbatim | verified |
| GPL-3.0.txt | GPL v3 text (LGPL-3.0 is written as additional permissions on top of GPL-3.0, so its text must travel with it) | — | GPL-3.0 | https://www.gnu.org/licenses/gpl-3.0.txt | verified |

**Duty.**

- curl, BSD-style: reproduce the curl copyright and permission notice.
- BoringSSL, Apache-2.0: include the licence. BoringSSL has no NOTICE file (404).
- MIT components: keep each copyright and permission notice.
- zstd: the BSD option requires reproducing its notice in documentation.
- libidn2 under LGPL-3.0: provide the LGPL and GPL texts, point to the source, and, because the library is statically linked, let users relink against a modified libidn2. The usual way is to offer the curl_cffi/curl-impersonate build inputs, which are public upstream. Taking the GPL-2.0-or-later option instead would extend GPL terms to the combined work. Chosen: LGPL-3.0-or-later (see NOTICE).

## 5. pytakumi 0.1.5: Takumi, its Rust crates and the font it embeds

Fetched 2026-10-19. pytakumi draws the connector pictures (server/connectors/picture.py); it replaced html2pic, pictex and skia-python, whose section this was.

**Evidence.** The image installs `pytakumi==0.1.5` (requirements.txt) from the cp314 manylinux_2_28 wheel: x86_64 sha256 3cbc197cc469074f33775cb04f7247883f25108450f1285527d25216034ab560, aarch64 sha256 c5049adc2460ed987db8102caac019142d2b2f1dc333d23507081f0b3ce4dbff, both matching the PyPI JSON digests. Its one native module links only glibc and libgcc_s (ldd in the image). The wheel carries a CycloneDX SBOM (`pytakumi-0.1.5.dist-info/sboms/pytakumi.cyclonedx.json`, cargo-cyclonedx 0.5.9), identical for both architectures, listing 171 crates as required (6 of them Takumi's own, vendored in the pytakumi sdist at Takumi 2.14.0) and 14 as build dependencies. The sdist (sha256 eaeb3e17fb0290864a72f49ef5174bc42a20a9cc74553c3c1f56d29c03acfc37) holds the source of the binding and of the vendored Takumi.

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| pytakumi-0.1.5.LICENSE-MIT, pytakumi-0.1.5.LICENSE-APACHE | pytakumi (unofficial Python binding of Takumi) | 0.1.5 | MIT OR Apache-2.0 | the wheel's own `pytakumi-0.1.5.dist-info/licenses/` | verified (from the shipped wheel) |
| takumi-2.14.0.LICENSE-MIT, takumi-2.14.0.LICENSE-APACHE | Takumi: takumi 2.14.0, takumi-core 0.25.0, takumi-html 0.3.4, takumi-raster 0.5.1, takumi-svg 0.3.22, takumi-bindings-common 0.0.0 | 2.14.0 | MIT OR Apache-2.0 | https://raw.githubusercontent.com/kane50613/takumi/takumi%402.14.0/LICENSE-MIT and …/LICENSE-APACHE | verified (byte-identical to `vendor/takumi/` in the pytakumi 0.1.5 sdist) |
| pytakumi-0.1.5-crates.txt | the 165 crates.io crates the SBOM lists as required | per crate | permissive, except cssparser 0.37.0, cssparser-macros 0.7.0, dtoa-short 0.3.5 and selectors 0.40.0 (MPL-2.0) | each `https://static.crates.io/crates/<name>/<name>-<version>.crate`, matching the sha256 in the sdist's Cargo.lock; the licence, copyright, notice, patents and authors files of each, identical texts grouped (the file's header says how) | verified (all 165 archives) |
| geist-1.5.0.OFL.txt | Geist (Latin subset, wght 300-800), embedded in the wheel's native module | 1.500 | OFL-1.1 | https://raw.githubusercontent.com/vercel/geist-font/1.5.0/OFL.txt | verified: the woff2 `vendor/takumi/assets/fonts/geist/geist-latin-wght-300-800.woff2` (sha256 d2f2dda36819f9a1ca3b780fcd324a37d77385b37653420f1599179c1695c355) is in both architectures' `.so` byte for byte; its name table reads "Version 1.500" and the copyright line this OFL.txt opens with |

**Crates with no licence file in the archive.** taffy 0.14.0 and wuff 0.2.8 (MIT): their LICENSE comes from the commit in each archive's `.cargo_vcs_info.json` (taffy 77f385683c1d698c91a23a259f87fdddf26925fb, wuff 0775526a3555e089d2fce140f755cb225e35cdc7). selectors 0.40.0 (MPL-2.0): the MPL-2.0 text. libwebp-sys 0.14.4 declares MIT for its own code and has no licence text in the archive or upstream at the packaged commit (3a40281fd97537b693174acb7375d64d3b471a56) (**unverified**: no upstream text exists); the libwebp C library it compiles in carries its COPYING (BSD-3-Clause) and PATENTS, both in the crate file.

**Note.** The SBOM's required list includes procedural-macro crates and their helpers, which run in the compiler and are not in the binary, and two crates for other systems (windows-link, redox_syscall). Their texts are carried anyway.

**Duty.** MIT, BSD, Zlib, ISC, BSL-1.0, Unicode-3.0, 0BSD: keep each notice and licence text (pytakumi-0.1.5-crates.txt). Apache-2.0: include the licence; no archive carries a NOTICE file. MPL-2.0 (four crates, compiled into an executable): inform recipients where the source is (NOTICE names the crates.io archives) and include the licence. OFL-1.1 (Geist): the copyright notice and licence travel with the font.

## 6. tokenizers 0.23.3

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| tokenizers-0.23.3.LICENSE | tokenizers (Hugging Face) | 0.23.3 | Apache-2.0 | https://raw.githubusercontent.com/huggingface/tokenizers/v0.23.3/LICENSE (no NOTICE at the tag: root and bindings/python both 404) | verified |

**Duty.** tokenizers, Apache-2.0: include the licence.

**The Rust crates in the wheel.** The wheel carries its own CycloneDX SBOM (`tokenizers-0.23.3.dist-info/sboms/tokenizers-python.cyclonedx.json`), which lists 127 crates, all under permissive licences. Where a crate offers Apache-2.0 we take it under Apache-2.0 (Apache-2.0.txt covers it), and where it offers the Unlicense we take that; none of the 127 crate archives from static.crates.io carries a NOTICE file. That leaves 19 crates whose own notice must be kept: 17 MIT-only, numpy (BSD-2-Clause) and unicode-ident (its Unicode-3.0 data licence). Their licence files, extracted from each `.crate` at the SBOM version, are in `tokenizers-0.23.3-crates/`. onig_sys 69.9.3 also builds the Oniguruma C library; its bundled `oniguruma/COPYING` is byte-identical to oniguruma-6.9.8.LICENSE. defmt-parser 1.0.0 ships no licence file and is MIT OR Apache-2.0, taken under Apache-2.0.

## 7. pgvector, age (FiloSottile), Apache AGE

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| pgvector-0.8.5.LICENSE | pgvector | 0.8.5 (pkg 0.8.5-1.pgdg12+1) | PostgreSQL | https://raw.githubusercontent.com/pgvector/pgvector/v0.8.5/LICENSE (byte-identical to the LICENSE in the pgdg orig tarball https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/pgvector/pgvector_0.8.5.orig.tar.gz) | verified |
| pgvector-0.8.5-1.pgdg12+1.debian-copyright | pgvector Debian packaging | 0.8.5-1.pgdg12+1 | PostgreSQL | `debian/copyright` from https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/pgvector/pgvector_0.8.5-1.pgdg12%2B1.debian.tar.xz | verified |
| apache-age-PG17-1.7.0-rc0.LICENSE | Apache AGE | PG17/v1.7.0-rc0 (pkg 1.7.0~rc0-1.pgdg12+1) | Apache-2.0 AND PostgreSQL (the file appends the PostgreSQL licence for PostgreSQL-derived code) | https://raw.githubusercontent.com/apache/age/PG17/v1.7.0-rc0/LICENSE (byte-identical to the pgdg orig tarball https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/postgresql-17-age/postgresql-17-age_1.7.0~rc0.orig.tar.gz) | verified (exact tag exists) |
| apache-age-PG17-1.7.0-rc0.NOTICE | Apache AGE NOTICE | same | (notice) | https://raw.githubusercontent.com/apache/age/PG17/v1.7.0-rc0/NOTICE (byte-identical to orig tarball) | verified |
| apache-age-1.7.0~rc0-1.pgdg12+1.debian-copyright | AGE Debian packaging | 1.7.0~rc0-1.pgdg12+1 | Apache-2.0 and PostgreSQL | `debian/copyright` from https://apt-archive.postgresql.org/pub/repos/apt/pool/main/p/postgresql-17-age/postgresql-17-age_1.7.0~rc0-1.pgdg12%2B1.debian.tar.xz | verified |
| age-1.3.2.LICENSE | age (Filippo Valsorda) **as shipped in the release tarball** | v1.3.2 | BSD-3-Clause (age) + BSD-3-Clause (Go Authors section appended) | `age/LICENSE` from https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz (sha256 cbe24006…ac10). The image builds age from the module filippo.io/age v1.3.2 (h1:r6RSZLFS…C8c=, Dockerfile.linux `AGE_MODULE_SUM`) and writes this file the way age's release workflow does (`LICENSE` + `.github/workflows/LICENSE.suffix.txt`); `/usr/share/doc/age/LICENSE` in the built image is byte-identical to this file | verified |
| age-1.3.2.LICENSE.git-tag | age, git tag copy | v1.3.2 | BSD-3-Clause | https://raw.githubusercontent.com/FiloSottile/age/v1.3.2/LICENSE (a prefix of the release copy, without the Go Authors section) | verified |
| age-1.3.2-go-deps/* | Go toolchain and modules compiled into the age binary | go1.27.2; edwards25519 v1.2.0; hpke v0.4.0; nistec v0.0.4; x/crypto v0.55.0; x/sys v0.47.0; x/term v0.45.0 (from `go version -m` on the binary in the built image; the module versions and hashes are the ones the v1.3.2 release binary records) | BSD-3-Clause (+ Google PATENTS grants for go, x/crypto, x/sys, x/term) | https://raw.githubusercontent.com/golang/go/go1.27.2/{LICENSE,PATENTS} (byte-identical to the go1.27.0 texts); https://raw.githubusercontent.com/FiloSottile/{edwards25519/v1.2.0,hpke/v0.4.0,nistec/v0.0.4}/LICENSE; https://raw.githubusercontent.com/golang/{crypto/v0.55.0,sys/v0.47.0,term/v0.45.0}/{LICENSE,PATENTS} | verified |

**AGE NOTICE text (verbatim, for the NOTICE file):** see apache-age-PG17-1.7.0-rc0.NOTICE. It says "Apache AGE / Copyright 2023 The Apache Software Foundation. / This product includes software developed at The Apache Software Foundation (http://www.apache.org/). / Portions of Apache AGE were originally developed by Bitnine Co., Ltd. … Copyright 2019-2020 Bitnine Co., Ltd."

**Note on the AGE debian/copyright.** It also lists `src/backend/utils/load/libcsv.c` under LGPL-2.1 (Robert Gamble). Searching the 1.7.0~rc0 orig source tree for `libcsv|Robert Gamble|GNU Lesser` returns no hits, so that stanza is stale and does not apply to this version.

**Duty.**

- Apache AGE, Apache-2.0 §4(d): a redistribution must include the readable NOTICE contents in a NOTICE file, plus the licence. The PostgreSQL portion requires keeping its copyright paragraph and the two disclaimer paragraphs.
- pgvector: keep the PostgreSQL copyright and disclaimer paragraphs.
- age and Go: the BSD-3 binary clause requires reproducing each notice in documentation. The release LICENSE already merges age's and Go's notices. The PATENTS files are grants, not conditions.

## 8. tree-sitter grammars baked into the image

**Evidence.** Dockerfile.linux:62-70 calls `tree_sitter_language_pack.prefetch()` on the keys of `CHUNKABLE_NODE_TYPES` (brain/codesearch/chunker.py:46). The image holds exactly `libtree_sitter_{go,javascript,python,rust,sql,tsx,typescript}.so` under `/usr/local/share/tree-sitter-language-pack/tree-sitter-language-pack/v1.22.1/libs/`. Each grammar's repo and rev come from `sources/language_definitions.json` at the pack's tag v1.22.1 (https://raw.githubusercontent.com/xberg-io/tree-sitter-language-pack/v1.22.1/sources/language_definitions.json). Each fetched LICENSE was checked against the sha256 recorded in that tag's `sources/grammar_notices.json`, and all seven match.

| file | grammar(s) | repo @ rev | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| tree-sitter-grammars/tree-sitter-go-2346a3ab.LICENSE | go | tree-sitter/tree-sitter-go @ 2346a3ab1bb3857b48b29d779a1ef9799a248cd7 | MIT | https://raw.githubusercontent.com/tree-sitter/tree-sitter-go/2346a3ab1bb3857b48b29d779a1ef9799a248cd7/LICENSE | verified (sha matches pack) |
| tree-sitter-grammars/tree-sitter-javascript-58404d8c.LICENSE | javascript | tree-sitter/tree-sitter-javascript @ 58404d8cf191d69f2674a8fd507bd5776f46cb11 | MIT | https://raw.githubusercontent.com/tree-sitter/tree-sitter-javascript/58404d8cf191d69f2674a8fd507bd5776f46cb11/LICENSE | verified |
| tree-sitter-grammars/tree-sitter-python-26855eab.LICENSE | python | tree-sitter/tree-sitter-python @ 26855eabccb19c6abf499fbc5b8dc7cc9ab8bc64 | MIT | https://raw.githubusercontent.com/tree-sitter/tree-sitter-python/26855eabccb19c6abf499fbc5b8dc7cc9ab8bc64/LICENSE | verified |
| tree-sitter-grammars/tree-sitter-rust-77a37472.LICENSE | rust | tree-sitter/tree-sitter-rust @ 77a3747266f4d621d0757825e6b11edcbf991ca5 | MIT | https://raw.githubusercontent.com/tree-sitter/tree-sitter-rust/77a3747266f4d621d0757825e6b11edcbf991ca5/LICENSE | verified |
| tree-sitter-grammars/tree-sitter-sql-97614d05.LICENSE | sql | DerekStride/tree-sitter-sql @ 97614d051eebfd3bc5d97c0bdb5a1638719ca811 | MIT | https://raw.githubusercontent.com/DerekStride/tree-sitter-sql/97614d051eebfd3bc5d97c0bdb5a1638719ca811/LICENSE | verified |
| tree-sitter-grammars/tree-sitter-typescript-75b3874e.LICENSE | typescript, tsx | tree-sitter/tree-sitter-typescript @ 75b3874edb2dc714fb1fd77a32013d0f8699989f | MIT | https://raw.githubusercontent.com/tree-sitter/tree-sitter-typescript/75b3874edb2dc714fb1fd77a32013d0f8699989f/LICENSE | verified |
| tree-sitter-grammars/tree-sitter-language-pack-1.22.1.LICENSE | the pack (Kreuzberg, Inc.) | xberg-io/tree-sitter-language-pack @ v1.22.1 | MIT | https://raw.githubusercontent.com/xberg-io/tree-sitter-language-pack/v1.22.1/LICENSE | verified |

**Duty.** MIT: keep each copyright and permission notice. The copyright holders are Max Brunsfeld (go, javascript, python, typescript), Maxim Sokolov (rust) and Derek Stride (sql).

## 9. Inter 4.1 (the picture renderer's font)

Fetched 2026-10-19. Dockerfile.linux installs `InterVariable.ttf` at `/usr/local/share/cseries/fonts/` with its licence beside it, both fetched from the release tag and pinned by sha256. The file is byte-identical to `InterVariable.ttf` in the official release archive (https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip, sha256 9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e) and to the copy pictex 2.1.0 shipped, which the renderer drew in before (sha256 4989b125924991b90d05b2d16e0e388c48f7d5bb8b30539bbf9c755278d0ccaf; name table "Version 4.001").

| file | component | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| inter-4.1.LICENSE.txt | Inter (InterVariable.ttf) | 4.1 (font revision 4.001) | OFL-1.1 | https://raw.githubusercontent.com/rsms/inter/v4.1/LICENSE.txt | verified (byte-identical to LICENSE.txt in the release archive) |

**Duty.** OFL-1.1: the copyright notice and the licence travel with the font (the image keeps the licence beside it, and here).

## 10. npm packages whose published tarball carries no licence file

All three declare MIT in package.json. The texts below come from the upstream repository at the commit npm records for that release (`npm view <pkg>@<version> gitHead`).

| file | package | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| remark-math-6.0.0.LICENSE | remark-math | 6.0.0 | MIT | https://raw.githubusercontent.com/remarkjs/remark-math/d5d0660b150810a535bbb07eac6cc96a4510aa24/license | verified (release commit) |
| rehype-katex-7.0.1.LICENSE | rehype-katex | 7.0.1 | MIT | https://raw.githubusercontent.com/remarkjs/remark-math/88a9497e1ede93b958237c85edbf5651faeca7af/license | verified (release commit; same monorepo licence as remark-math) |
| react-remove-scroll-bar-2.3.8.LICENSE | react-remove-scroll-bar | 2.3.8 | MIT | https://raw.githubusercontent.com/theKashey/react-remove-scroll-bar/master/LICENSE | upstream later: the 2.3.8 release commit (b3b1287a) has no LICENSE file; the author (Anton Korzunov, the package.json author) added this MIT text afterwards |

## 11. webauthn 3.0.1 and the packages it brings (the owner's passkeys)

Fetched 2026-10-19. Each text is the licence file inside the wheel requirements.txt pins, copied byte for byte from its `.dist-info/licenses/` directory; each wheel's sha256 matched the digest PyPI publishes for it. cbor2 is the only one with compiled code; the texts in its x86_64 and arm64 wheels for cp314 are byte-identical (sha256 a6afd126…2fef).

| file | package | version | SPDX | wheel the text came from | status |
|---|---|---|---|---|---|
| webauthn-3.0.1.LICENSE | webauthn (Duo Labs) | 3.0.1 | BSD-3-Clause | https://files.pythonhosted.org/packages/ea/5e/cc651030a1d43010234247ab4361aac17276f62b185975100ba62aa00cf7/webauthn-3.0.1-py3-none-any.whl | verified |
| cbor2-6.1.5.LICENSE | cbor2 (Alex Grönholm) | 6.1.5 | MIT | https://files.pythonhosted.org/packages/e5/fa/5f9baeecf35db1d35ca5415dfa1e8656d656ccbbaca875e65d72df849f4e/cbor2-6.1.5-cp314-cp314-manylinux_2_28_x86_64.whl | verified |
| pyasn1-0.6.4.LICENSE | pyasn1 (Ilya Etingof) | 0.6.4 | BSD-2-Clause | https://files.pythonhosted.org/packages/9a/3b/6163796d69c3977d1e4287bea4a6979161cbbdd170ebb430511e8e1999ce/pyasn1-0.6.4-py3-none-any.whl | verified |
| pyasn1-modules-0.4.2.LICENSE | pyasn1-modules (Ilya Etingof) | 0.4.2 | BSD-2-Clause (the metadata says "BSD"; the text is the two-clause licence, byte-identical to pyasn1's) | https://files.pythonhosted.org/packages/47/8d/d529b5d697919ba8c11ad626e835d4039be708a35b0d22de83a269a6682c/pyasn1_modules-0.4.2-py3-none-any.whl | verified |
| pyOpenSSL-26.4.0.LICENSE | pyOpenSSL (the pyOpenSSL developers) | 26.4.0 | Apache-2.0 | https://files.pythonhosted.org/packages/51/ad/2cf6d3fa2fae5c79e1ed9960c0d42badd0f94d81dd12b50604cdc839e648/pyopenssl-26.4.0-py3-none-any.whl | verified |

**NOTICE.** pyOpenSSL is the one Apache-2.0 package here. Its wheel carries no NOTICE file, and https://raw.githubusercontent.com/pyca/pyopenssl/26.4.0/NOTICE answers 404, so there is no NOTICE text to carry; NOTICE records that.

**Duty.** BSD-2-Clause, BSD-3-Clause and MIT: keep the copyright notice and the licence text. Apache-2.0: include the licence.

## 12. The browser libraries vendored into the system apps (age-encryption, noble, scure, qrcode-generator)

Fetched 2026-10-20. Settings (system-apps/settings/vendor/) and Backups (system-apps/backups/vendor/) each carry the same age-encryption.js, one esbuild bundle of age-encryption and the five packages it uses (sha256 2097a8cf…eb27, pinned in each vendor/VERSIONS). Backups also carries qrcode-generator, byte-identical to dashboard/base/js/qrcode.js (sha256 6326a28e…7c95).

**Evidence.** Each npm text below was read from the package's own tarball (`npm pack <pkg>@<version>`), and each tarball's sha512 matched the integrity the registry publishes and the one vendor/VERSIONS records. Every text is byte-identical to the vendor/LICENSE-* copy beside the bundle. qrcode-generator's npm tarball carries no licence file; its text is the repository's LICENSE at the release tag `js1.4.4`, and that tag's js/qrcode.js is byte-identical to the npm 1.4.4 qrcode.js (sha256 18ae399f…1780). The vendored qrcode.js is that file with a four-line header comment added above it.

| file | package | version | SPDX | URL fetched | status |
|---|---|---|---|---|---|
| age-encryption-0.3.1.LICENSE | age-encryption (typage, the age authors) | 0.3.1 | BSD-3-Clause | https://registry.npmjs.org/age-encryption/-/age-encryption-0.3.1.tgz (package/LICENSE) | verified |
| noble-ciphers-2.4.0.LICENSE | @noble/ciphers (Paul Miller) | 2.4.0 | MIT | https://registry.npmjs.org/@noble/ciphers/-/ciphers-2.4.0.tgz | verified |
| noble-curves-2.4.0.LICENSE | @noble/curves (Paul Miller) | 2.4.0 | MIT | https://registry.npmjs.org/@noble/curves/-/curves-2.4.0.tgz | verified |
| noble-curves-2.0.1.LICENSE | @noble/curves, nested under @noble/post-quantum | 2.0.1 | MIT | https://registry.npmjs.org/@noble/curves/-/curves-2.0.1.tgz | verified |
| noble-hashes-2.4.0.LICENSE | @noble/hashes (Paul Miller) | 2.4.0 | MIT | https://registry.npmjs.org/@noble/hashes/-/hashes-2.4.0.tgz | verified |
| noble-hashes-2.0.1.LICENSE | @noble/hashes, nested under @noble/post-quantum | 2.0.1 | MIT | https://registry.npmjs.org/@noble/hashes/-/hashes-2.0.1.tgz | verified |
| noble-post-quantum-0.5.4.LICENSE | @noble/post-quantum (Paul Miller) | 0.5.4 | MIT | https://registry.npmjs.org/@noble/post-quantum/-/post-quantum-0.5.4.tgz | verified |
| scure-base-2.4.0.LICENSE | @scure/base (Paul Miller) | 2.4.0 | MIT | https://registry.npmjs.org/@scure/base/-/base-2.4.0.tgz | verified |
| qrcode-generator-1.4.4.LICENSE | qrcode-generator (Kazuhiko Arase) | 1.4.4 | MIT | https://raw.githubusercontent.com/kazuhikoarase/qrcode-generator/js1.4.4/LICENSE | verified (release tag) |

**Duty.** BSD-3-Clause and MIT: keep the copyright notice and the licence text with the copies. The texts are here and beside each bundle (vendor/LICENSE-*); the bundle keeps each package's own comments (esbuild --legal-comments=inline).
