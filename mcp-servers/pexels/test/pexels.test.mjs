import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';

import { searchVideos, searchPhotos, getVideo, PexelsError } from '../src/pexels.js';

const realFetch = globalThis.fetch;
let calls;

function installFetch(handler) {
    globalThis.fetch = async (url, opts) => {
        calls.push({ url: String(url), opts });
        return handler(String(url), opts);
    };
}

function response(body, { status = 200, headers = {} } = {}) {
    const lower = {};
    for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
    return {
        ok: status >= 200 && status < 300,
        status,
        headers: { get: (h) => lower[h.toLowerCase()] ?? null },
        json: async () => body,
    };
}

beforeEach(() => {
    calls = [];
    process.env.PEXELS_API_KEY = 'test-key';
});

afterEach(() => {
    globalThis.fetch = realFetch;
    delete process.env.PEXELS_API_KEY;
});

test('search_videos maps the response to the documented shape', async () => {
    installFetch(() =>
        response({
            videos: [
                {
                    id: 123,
                    url: 'https://www.pexels.com/video/123/',
                    duration: 20,
                    width: 1920,
                    height: 1080,
                    user: { name: 'Jane Doe', url: 'https://www.pexels.com/@jane-doe' },
                    video_files: [{ link: 'https://x/1.mp4' }],
                },
            ],
        }),
    );

    const results = await searchVideos({ query: 'nature' });

    assert.deepEqual(results, [
        {
            id: 123,
            url: 'https://www.pexels.com/video/123/',
            duration: 20,
            width: 1920,
            height: 1080,
            photographer: 'Jane Doe',
            photographer_url: 'https://www.pexels.com/@jane-doe',
        },
    ]);
});

test('search_videos sends the key header, query and optional params', async () => {
    installFetch(() => response({ videos: [] }));

    await searchVideos({ query: 'city lights', per_page: 5, orientation: 'portrait', size: 'medium' });

    const { url, opts } = calls[0];
    // Must use the versioned /v1/videos base — the legacy /videos host is deprecated.
    assert.ok(url.startsWith('https://api.pexels.com/v1/videos/search?'));
    assert.equal(opts.headers.Authorization, 'test-key');
    const params = new URL(url).searchParams;
    assert.equal(params.get('query'), 'city lights');
    assert.equal(params.get('per_page'), '5');
    assert.equal(params.get('orientation'), 'portrait');
    assert.equal(params.get('size'), 'medium');
});

test('search_photos maps the response to the documented shape', async () => {
    installFetch(() =>
        response({
            photos: [
                {
                    id: 7,
                    url: 'https://www.pexels.com/photo/7/',
                    photographer: 'Sam',
                    photographer_url: 'https://www.pexels.com/@sam',
                },
            ],
        }),
    );

    const results = await searchPhotos({ query: 'ocean' });

    assert.deepEqual(results, [
        {
            id: 7,
            url: 'https://www.pexels.com/photo/7/',
            photographer: 'Sam',
            photographer_url: 'https://www.pexels.com/@sam',
        },
    ]);
    assert.ok(calls[0].url.startsWith('https://api.pexels.com/v1/search?'));
});

test('get_video returns url + download_links', async () => {
    installFetch(() =>
        response({
            url: 'https://www.pexels.com/video/99/',
            user: { name: 'Jane Doe', url: 'https://www.pexels.com/@jane-doe' },
            video_files: [
                { link: 'https://x/hd.mp4', quality: 'hd', width: 1920, height: 1080, file_type: 'video/mp4' },
                { link: 'https://x/sd.mp4', quality: 'sd', width: 640, height: 360, file_type: 'video/mp4' },
            ],
        }),
    );

    const result = await getVideo(99);

    assert.equal(result.url, 'https://www.pexels.com/video/99/');
    // get_video carries attribution so a downloader never has to correlate back
    // to the search result to credit the creator.
    assert.equal(result.photographer, 'Jane Doe');
    assert.equal(result.photographer_url, 'https://www.pexels.com/@jane-doe');
    assert.equal(result.download_links.length, 2);
    assert.deepEqual(result.download_links[0], {
        link: 'https://x/hd.mp4',
        quality: 'hd',
        width: 1920,
        height: 1080,
        file_type: 'video/mp4',
    });
    assert.equal(calls[0].url, 'https://api.pexels.com/v1/videos/videos/99');
});

test('every tool exposes the attribution fields consumers need', async () => {
    // The API Guidelines require crediting the creator with a link back to
    // Pexels; the contract must always surface photographer + photographer_url
    // + url (as null when absent) so consumers can render attribution.
    installFetch((url) => {
        if (url.includes('/videos/search')) return response({ videos: [{ id: 1, url: 'p', user: { name: 'A', url: 'u' } }] });
        if (url.includes('/videos/')) return response({ url: 'p', user: { name: 'A', url: 'u' }, video_files: [] });
        return response({ photos: [{ id: 2, url: 'p', photographer: 'B', photographer_url: 'u' }] });
    });

    const [vid] = await searchVideos({ query: 'x' });
    const [pho] = await searchPhotos({ query: 'x' });
    const one = await getVideo(1);

    for (const r of [vid, pho, one]) {
        assert.ok('photographer' in r, 'photographer field present');
        assert.ok('photographer_url' in r, 'photographer_url field present');
        assert.ok('url' in r, 'url field present');
    }
});

test('missing creator data degrades to null attribution, not undefined', async () => {
    installFetch((url) => {
        if (url.includes('/videos/search')) return response({ videos: [{ id: 1, url: 'p' }] });
        if (url.includes('/videos/')) return response({ url: 'p', video_files: [] });
        return response({ photos: [{ id: 2, url: 'p' }] });
    });

    const [vid] = await searchVideos({ query: 'x' });
    const [pho] = await searchPhotos({ query: 'x' });
    const one = await getVideo(1);

    for (const r of [vid, pho, one]) {
        assert.equal(r.photographer, null);
        assert.equal(r.photographer_url, null);
    }
});

test('401 surfaces an unauthorized error that names the key', async () => {
    installFetch(() => response({}, { status: 401 }));

    await assert.rejects(searchVideos({ query: 'nature' }), (err) => {
        assert.ok(err instanceof PexelsError);
        assert.equal(err.code, 'unauthorized');
        assert.match(err.message, /PEXELS_API_KEY/);
        return true;
    });
});

test('429 surfaces a rate_limited error with retry_after', async () => {
    installFetch(() => response({}, { status: 429, headers: { 'Retry-After': '42' } }));

    await assert.rejects(searchVideos({ query: 'nature' }), (err) => {
        assert.ok(err instanceof PexelsError);
        assert.equal(err.code, 'rate_limited');
        assert.equal(err.retry_after, '42');
        return true;
    });
});

test('missing PEXELS_API_KEY surfaces a missing_key error before any fetch', async () => {
    delete process.env.PEXELS_API_KEY;
    let fetched = false;
    installFetch(() => {
        fetched = true;
        return response({ videos: [] });
    });

    await assert.rejects(searchVideos({ query: 'nature' }), (err) => {
        assert.ok(err instanceof PexelsError);
        assert.equal(err.code, 'missing_key');
        return true;
    });
    assert.equal(fetched, false);
});

test('invalid arguments are rejected before any fetch', async () => {
    let fetched = false;
    installFetch(() => {
        fetched = true;
        return response({ videos: [] });
    });

    await assert.rejects(searchVideos({ query: '' }), (e) => e.code === 'invalid_argument');
    await assert.rejects(searchVideos({ query: 'x', per_page: 999 }), (e) => e.code === 'invalid_argument');
    await assert.rejects(searchVideos({ query: 'x', orientation: 'diagonal' }), (e) => e.code === 'invalid_argument');
    await assert.rejects(getVideo(0), (e) => e.code === 'invalid_argument');
    assert.equal(fetched, false);
});
