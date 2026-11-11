// Loads a lib/*.js file the way QML does, so Deno can test it.
//
// The libraries are QML JavaScript, not modules: they start with
// `.pragma library` and pull each other in with `.import "x.js" as Name`,
// neither of which is JavaScript. The loader drops the pragma, turns each
// import into a parameter of the evaluated function (loading that file first,
// once per file), and returns the file's top-level `function`s and `var`s.
//
// The export list is read from the source rather than written by hand: a
// hand-written one drifts, and a helper missing from it reads as undefined
// here, so its test would fail for the wrong reason or never be written.
// Every test file checks that each name the source declares is reachable.

const LIB = new URL("../../lib/", import.meta.url);

const PRAGMA = /^\.pragma\s+library\s*$/;
const IMPORT = /^\.import\s+"([^"]+\.js)"\s+as\s+([A-Za-z_$][\w$]*)\s*$/;

const cache = new Map();
const loading = new Set();

// The names a QML JS file declares at the top level, in source order.
export function topLevelNames(source) {
  return [
    ...source.matchAll(/^function\s+([A-Za-z_$][\w$]*)/gm),
    ...source.matchAll(/^var\s+([A-Za-z_$][\w$]*)\s*=/gm),
  ].map((match) => match[1]);
}

// The source of a lib file as plain JavaScript, plus what it imports. Lines
// are blanked rather than removed, so an error's line number still points at
// the file.
export function translate(source, file = "<source>") {
  const imports = [];
  const lines = source.split("\n").map((line, index) => {
    const text = line.trim();
    if (!text.startsWith(".")) return line;
    if (PRAGMA.test(text)) return "";
    const found = text.match(IMPORT);
    if (found) {
      imports.push({ file: found[1], as: found[2] });
      return "";
    }
    throw new Error(`${file}:${index + 1}: QML directive this loader does not know: ${text}`);
  });
  return { body: lines.join("\n"), imports };
}

// The library at lib/<file>, evaluated with its imports injected. The same
// file always returns the same object, as one QML engine would share it.
export function load(file, base = LIB) {
  const url = new URL(file, base);
  const key = url.href;
  if (cache.has(key)) return cache.get(key);
  if (loading.has(key)) throw new Error("circular .import of " + key);
  loading.add(key);
  try {
    const source = Deno.readTextFileSync(url);
    const { body, imports } = translate(source, file);
    const deps = imports.map((dep) => load(dep.file, url));
    const names = topLevelNames(source);
    const evaluate = new Function(
      ...imports.map((dep) => dep.as),
      body + "\n;return { " + names.join(", ") + " };",
    );
    const lib = evaluate(...deps);
    cache.set(key, lib);
    return lib;
  } finally {
    loading.delete(key);
  }
}

// The top-level names lib/<file> declares, for the reachability tests.
export function namesOf(file, base = LIB) {
  return topLevelNames(Deno.readTextFileSync(new URL(file, base)));
}
