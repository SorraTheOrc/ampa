import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import path from 'path';

test('start-work template must escape CONTAINER_PROJECT_ROOT to avoid JS interpolation', () => {
  const file = path.resolve('skill/install-ampa/resources/ampa.mjs');
  const src = fs.readFileSync(file, 'utf8');

  // The source template executed inside the container should use a shell
  // variable named CONTAINER_PROJECT_ROOT. When the template is defined as a
  // JS template literal, any unescaped `${CONTAINER_PROJECT_ROOT}` will cause
  // Node to attempt interpolation and throw a ReferenceError at runtime. We
  // therefore assert the source contains the escaped form and does not contain
  // an unescaped occurrence.
  assert.strictEqual(src.includes('"${CONTAINER_PROJECT_ROOT}"'), false, 'Found unescaped "${CONTAINER_PROJECT_ROOT}" in plugin source; this may cause a ReferenceError at runtime');
  assert.ok(src.includes('\\${CONTAINER_PROJECT_ROOT}'), 'Expected escaped "\\${CONTAINER_PROJECT_ROOT}" in plugin source');
});
