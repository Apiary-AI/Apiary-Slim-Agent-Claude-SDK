// Pexels API client — pure request/mapping logic with NO MCP-SDK dependency,
// so the query -> request -> response-shape behaviour stays unit-testable with a
// mocked global `fetch`. index.js wires these functions into MCP tools.
//
// API docs: https://www.pexels.com/api/  (free tier: 200 req/hour, 20k/month).
// Using the API requires attribution: a prominent link back to Pexels and, when
// possible, crediting the photographer/videographer (see the API Guidelines).
// Auth is a bare API key in the `Authorization` header (no "Bearer" prefix).
//
// Videos use the versioned `/v1/videos` base — the legacy `/videos` host is
// deprecated (https://www.pexels.com/api/documentation/#videos-search).

const PHOTOS_BASE = 'https://api.pexels.com/v1';
const VIDEOS_BASE = 'https://api.pexels.com/v1/videos';

const ORIENTATIONS = ['portrait', 'landscape', 'square'];
const SIZES = ['large', 'medium', 'small'];

/**
 * Typed error so callers (and the MCP tool layer) can surface a clear,
 * deterministic message per failure mode.
 *
 * `code` is one of: 'missing_key' | 'invalid_argument' | 'unauthorized'
 * | 'rate_limited' | 'http_error'.
 */
export class PexelsError extends Error {
    constructor(message, code, details = {}) {
        super(message);
        this.name = 'PexelsError';
        this.code = code;
        Object.assign(this, details);
    }
}

function apiKey() {
    const key = process.env.PEXELS_API_KEY;
    if (!key) {
        throw new PexelsError('PEXELS_API_KEY not set in env', 'missing_key');
    }
    return key;
}

function buildSearchParams({ query, per_page, orientation, size }) {
    if (typeof query !== 'string' || query.trim() === '') {
        throw new PexelsError('query is required and must be a non-empty string', 'invalid_argument');
    }
    const params = new URLSearchParams({ query });

    if (per_page != null) {
        const n = Number(per_page);
        // Pexels caps per_page at 80.
        if (!Number.isInteger(n) || n < 1 || n > 80) {
            throw new PexelsError('per_page must be an integer between 1 and 80', 'invalid_argument');
        }
        params.set('per_page', String(n));
    }
    if (orientation != null) {
        if (!ORIENTATIONS.includes(orientation)) {
            throw new PexelsError(`orientation must be one of: ${ORIENTATIONS.join(', ')}`, 'invalid_argument');
        }
        params.set('orientation', orientation);
    }
    if (size != null) {
        if (!SIZES.includes(size)) {
            throw new PexelsError(`size must be one of: ${SIZES.join(', ')}`, 'invalid_argument');
        }
        params.set('size', size);
    }
    return params;
}

async function request(url) {
    const res = await fetch(url, { headers: { Authorization: apiKey() } });

    if (res.status === 401) {
        throw new PexelsError('Pexels returned 401 Unauthorized — check PEXELS_API_KEY', 'unauthorized');
    }
    if (res.status === 429) {
        // Surface a retry hint: Pexels exposes rate-limit reset via headers.
        const retryAfter = res.headers.get('Retry-After') ?? res.headers.get('X-Ratelimit-Reset') ?? null;
        throw new PexelsError('Pexels rate limit exceeded (429)', 'rate_limited', { retry_after: retryAfter });
    }
    if (!res.ok) {
        throw new PexelsError(`Pexels request failed with HTTP ${res.status}`, 'http_error', { status: res.status });
    }
    return res.json();
}

/**
 * search_videos — returns a trimmed, deterministic array of video results.
 * Shape: [{ id, url, duration, width, height, photographer, photographer_url }]
 *
 * `url` (Pexels media page) and `photographer` / `photographer_url` are the
 * attribution fields consumers need: the API Guidelines require a prominent
 * link back to Pexels and crediting the creator, linked to their profile.
 */
export async function searchVideos(args = {}) {
    const params = buildSearchParams(args);
    const data = await request(`${VIDEOS_BASE}/search?${params.toString()}`);
    return (data.videos ?? []).map((v) => ({
        id: v.id,
        url: v.url,
        duration: v.duration,
        width: v.width,
        height: v.height,
        photographer: v.user?.name ?? null,
        photographer_url: v.user?.url ?? null,
    }));
}

/**
 * search_photos — returns a trimmed, deterministic array of photo results.
 * Shape: [{ id, url, photographer, photographer_url }]
 *
 * See searchVideos for why the attribution fields are included.
 */
export async function searchPhotos(args = {}) {
    const params = buildSearchParams(args);
    const data = await request(`${PHOTOS_BASE}/search?${params.toString()}`);
    return (data.photos ?? []).map((p) => ({
        id: p.id,
        url: p.url,
        photographer: p.photographer ?? null,
        photographer_url: p.photographer_url ?? null,
    }));
}

/**
 * get_video — resolve a single video's download links by id.
 * Shape: { url, photographer, photographer_url, download_links: [...] }
 *
 * Attribution fields are returned here too: this is the endpoint a consumer
 * hits to actually download media, so it must carry the credit data with it
 * rather than forcing a correlation back to the search result.
 */
export async function getVideo(id) {
    const n = Number(id);
    if (!Number.isInteger(n) || n < 1) {
        throw new PexelsError('id must be a positive integer', 'invalid_argument');
    }
    const data = await request(`${VIDEOS_BASE}/videos/${n}`);
    return {
        url: data.url,
        photographer: data.user?.name ?? null,
        photographer_url: data.user?.url ?? null,
        download_links: (data.video_files ?? []).map((f) => ({
            link: f.link,
            quality: f.quality ?? null,
            width: f.width ?? null,
            height: f.height ?? null,
            file_type: f.file_type ?? null,
        })),
    };
}

export const _internals = { ORIENTATIONS, SIZES, PHOTOS_BASE, VIDEOS_BASE };
