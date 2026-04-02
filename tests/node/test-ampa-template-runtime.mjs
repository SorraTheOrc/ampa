import test from 'node:test';
import assert from 'node:assert';
import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

// This test demonstrates the root cause of the ReferenceError observed in the
// wild: a JavaScript template literal that contains an unescaped
// `${CONTAINER_PROJECT_ROOT}` will cause Node to evaluate the expression at
// module-evaluation time and throw ReferenceError when the identifier is not
// defined. The safe form escapes the interpolation as `\${...}`.

test('unescaped template interpolation throws ReferenceError at import', async () => {
  const tmp = path.join(process.cwd(), 'tests', 'node', 'tmp_vuln_template.mjs');
  const vulnSrc = `export const s = ` + "`rm -rf \"${CONTAINER_PROJECT_ROOT}\"`" + `;\n`;
  fs.writeFileSync(tmp, vulnSrc, 'utf8');
  try {
    await import(pathToFileURL(tmp).href);
    assert.fail('Expected import to throw ReferenceError for unescaped ${CONTAINER_PROJECT_ROOT}');
  } catch (e) {
    // Accept ReferenceError specifically; other errors would indicate a
    // different problem (syntax, IO, etc.).
    assert.ok(e && (e instanceof ReferenceError || e.name === 'ReferenceError'), `Expected ReferenceError, got ${String(e)}`);
  } finally {
    try { fs.unlinkSync(tmp); } catch (e) {}
  }
});

test('escaped template interpolation imports successfully and preserves literal', async () => {
  const tmp = path.join(process.cwd(), 'tests', 'node', 'tmp_fixed_template.mjs');
  // Note: the file contents must contain a backslash before ${...} so that
  // Node does not attempt interpolation inside the template literal.
  const fixedSrc = `export const s = ` + "`rm -rf \"\\${CONTAINER_PROJECT_ROOT}\"`" + `;\n`;
  fs.writeFileSync(tmp, fixedSrc, 'utf8');
  try {
    const mod = await import(pathToFileURL(tmp).href);
    assert.ok(mod && typeof mod.s === 'string');
    // The exported string should contain the literal ${CONTAINER_PROJECT_ROOT}
    assert.ok(mod.s.includes('${CONTAINER_PROJECT_ROOT}'));
  } finally {
    try { fs.unlinkSync(tmp); } catch (e) {}
  }
});
