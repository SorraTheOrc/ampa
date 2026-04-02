import test from 'node:test';
import assert from 'node:assert';
import path from 'path';
import { pathToFileURL } from 'url';

test('canonical ampa plugin imports without throwing (no ReferenceError)', async () => {
  const p = path.resolve(process.cwd(), 'skill', 'install-ampa', 'resources', 'ampa.mjs');
  // Ensure the file exists so the test fails meaningfully if path is wrong
  try {
    await import(pathToFileURL(p).href);
  } catch (e) {
    // If import throws, surface the error for debugging
    assert.fail(`Importing canonical ampa.mjs threw: ${String(e)}`);
  }
});
