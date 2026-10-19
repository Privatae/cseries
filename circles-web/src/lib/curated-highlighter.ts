// Curated shiki highlighter (audit phase 5, 2026-07-03).
//
// The markdown renderer's default `@streamdown/code` plugin loads shiki's FULL
// language bundle (~200 grammars + 63 themes), which Vite code-splits into ~280
// lazy chunks (~8.9MB on disk). Only a handful ever load at runtime, so the cost
// is image/bundle SIZE, not user latency — but it's pure dead weight for a chat
// app. This module builds shiki from `core` with a fixed coding-language set and
// exposes:
//   - getCuratedHighlighter(): the shared HighlighterCore (used by CodeBlock too)
//   - curatedCodePlugin: a drop-in replacement for `@streamdown/code`'s `code`
//     (same CodeHighlighterPlugin shape) so streamdown never imports the full bundle.
//
// Any language outside CURATED_LANGS falls back to plaintext ("text") — the same
// graceful path the components already had. To add a language: import its grammar
// and add it to CURATED_LANGS below.
import type { BundledLanguage, TokensResult } from "shiki";
import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createOnigurumaEngine } from "shiki/engine/oniguruma";
import githubLight from "@shikijs/themes/github-light";
import githubDark from "@shikijs/themes/github-dark";
import langTypescript from "@shikijs/langs/typescript";
import langJavascript from "@shikijs/langs/javascript";
import langTsx from "@shikijs/langs/tsx";
import langJsx from "@shikijs/langs/jsx";
import langPython from "@shikijs/langs/python";
import langRust from "@shikijs/langs/rust";
import langGo from "@shikijs/langs/go";
import langC from "@shikijs/langs/c";
import langCpp from "@shikijs/langs/cpp";
import langJava from "@shikijs/langs/java";
import langBash from "@shikijs/langs/bash";
import langJson from "@shikijs/langs/json";
import langYaml from "@shikijs/langs/yaml";
import langToml from "@shikijs/langs/toml";
import langSql from "@shikijs/langs/sql";
import langHtml from "@shikijs/langs/html";
import langCss from "@shikijs/langs/css";
import langMarkdown from "@shikijs/langs/markdown";
import langDiff from "@shikijs/langs/diff";

const CURATED_LANGS = [
  langTypescript, langJavascript, langTsx, langJsx, langPython, langRust,
  langGo, langC, langCpp, langJava, langBash, langJson, langYaml, langToml,
  langSql, langHtml, langCss, langMarkdown, langDiff,
];

// Grammar `name`s (incl. aliases like ts/js/py/sh) the loaded set answers to.
const CURATED_LANG_NAMES: string[] = CURATED_LANGS.flatMap((entry) => {
  const grammars = Array.isArray(entry) ? entry : [entry];
  return grammars.flatMap((g) => {
    const reg = g as { name?: string; aliases?: string[] };
    return [reg.name, ...(reg.aliases ?? [])].filter(Boolean) as string[];
  });
});

const THEMES = [githubLight, githubDark] as [typeof githubLight, typeof githubDark];

let coreHighlighterPromise: Promise<HighlighterCore> | null = null;

export const getCuratedHighlighter = (): Promise<HighlighterCore> => {
  if (!coreHighlighterPromise) {
    coreHighlighterPromise = createHighlighterCore({
      langs: CURATED_LANGS,
      themes: THEMES,
      engine: createOnigurumaEngine(import("shiki/wasm")),
    });
  }
  return coreHighlighterPromise;
};

export const supportsCuratedLanguage = (language: string): boolean =>
  CURATED_LANG_NAMES.includes(language);

// ── Drop-in for @streamdown/code's `code` (CodeHighlighterPlugin shape) ──
// Mirrors that plugin's runtime surface: name/type + getSupportedLanguages /
// getThemes / supportsLanguage / highlight(sync-cache + async callback).
// `language` is whatever the fence says (Streamdown 2.6 passes a plain string);
// anything not loaded falls back to "text" below.
type HighlightOptions = { code: string; language: string; themes?: unknown };
type PluginCallback = (result: TokensResult) => void;

const tokensCache = new Map<string, TokensResult>();
const cacheKey = (code: string, language: string) =>
  `${language}:${code.length}:${code.slice(0, 80)}:${code.slice(-80)}`;

const rawResult = (code: string): TokensResult =>
  ({
    bg: "transparent",
    fg: "inherit",
    themeName: "github",
    tokens: code.split("\n").map((line) =>
      line === "" ? [] : [{ content: line, color: "inherit", offset: 0 }]
    ),
  } as unknown as TokensResult);

const highlight = (
  options: HighlightOptions,
  callback?: PluginCallback
): TokensResult | null => {
  const { code, language } = options;
  const key = cacheKey(code, language);
  const cached = tokensCache.get(key);
  if (cached) {
    return cached;
  }
  getCuratedHighlighter()
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then)
    .then((highlighter) => {
      const loaded = highlighter.getLoadedLanguages();
      const langToUse = loaded.includes(language) ? language : "text";
      const result = highlighter.codeToTokens(code, {
        lang: langToUse,
        themes: { light: "github-light", dark: "github-dark" },
      }) as unknown as TokensResult;
      tokensCache.set(key, result);
      callback?.(result);
    })
    // oxlint-disable-next-line eslint-plugin-promise(prefer-await-to-then), eslint-plugin-promise(prefer-await-to-callbacks)
    .catch(() => {
      callback?.(rawResult(code));
    });
  return null;
};

export const curatedCodePlugin = {
  name: "shiki" as const,
  type: "code-highlighter" as const,
  getSupportedLanguages: () => CURATED_LANG_NAMES as BundledLanguage[],
  getThemes: () => THEMES,
  supportsLanguage: (language: string) => supportsCuratedLanguage(language),
  highlight,
};
