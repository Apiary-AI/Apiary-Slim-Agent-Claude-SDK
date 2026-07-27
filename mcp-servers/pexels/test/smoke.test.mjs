import { test } from 'node:test';
import assert from 'node:assert/strict';

import { searchVideos } from '../src/pexels.js';

// Live integration smoke test. Runs ONLY when PEXELS_API_KEY is present (CI and
// local without a key skip it). Hits the real Pexels API — keep it to one cheap
// call to stay well inside the free tier (200 req/hour).
test('live: search_videos("nature") returns >= 1 result', { skip: !process.env.PEXELS_API_KEY }, async () => {
    const results = await searchVideos({ query: 'nature', per_page: 3 });
    assert.ok(Array.isArray(results));
    assert.ok(results.length >= 1, 'expected at least one video result');
    assert.ok(typeof results[0].id === 'number');
    assert.ok(typeof results[0].url === 'string');
});
