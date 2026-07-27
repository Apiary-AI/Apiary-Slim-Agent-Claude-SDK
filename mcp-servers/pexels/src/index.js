#!/usr/bin/env node
// Pexels stdio MCP server. Exposes three tools — search_videos, search_photos,
// get_video — over the MCP stdio transport so any agent can search Pexels stock
// video/photo. Reads PEXELS_API_KEY from the process env (never baked in).
//
// The request/mapping logic lives in ./pexels.js (SDK-free, unit-tested). This
// file is the thin MCP wrapper around it.

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

import { searchVideos, searchPhotos, getVideo, PexelsError } from './pexels.js';

const orientation = z.enum(['portrait', 'landscape', 'square']).optional();
const size = z.enum(['large', 'medium', 'small']).optional();
const perPage = z.number().int().min(1).max(80).optional();

/**
 * Wrap a tool body so both success and PexelsError produce deterministic JSON
 * text content. PexelsError becomes an isError result carrying { code, message,
 * retry_after? } so the caller can react (e.g. back off on rate_limited).
 */
async function toolResult(fn) {
    try {
        const data = await fn();
        return { content: [{ type: 'text', text: JSON.stringify(data) }] };
    } catch (err) {
        if (err instanceof PexelsError) {
            const payload = { error: { code: err.code, message: err.message } };
            if (err.retry_after != null) payload.error.retry_after = err.retry_after;
            return { isError: true, content: [{ type: 'text', text: JSON.stringify(payload) }] };
        }
        throw err;
    }
}

const server = new McpServer({ name: 'pexels', version: '0.1.0' });

server.registerTool(
    'search_videos',
    {
        title: 'Search Pexels videos',
        description:
            'Search Pexels stock videos. Returns [{id, url, duration, width, height, photographer, photographer_url}]. ' +
            'Attribution is required: credit the photographer (link the name to photographer_url) with a link back to url on Pexels.',
        inputSchema: { query: z.string().min(1), per_page: perPage, orientation, size },
    },
    (args) => toolResult(() => searchVideos(args)),
);

server.registerTool(
    'search_photos',
    {
        title: 'Search Pexels photos',
        description:
            'Search Pexels stock photos. Returns [{id, url, photographer, photographer_url}]. ' +
            'Attribution is required: credit the photographer (link the name to photographer_url) with a link back to url on Pexels.',
        inputSchema: { query: z.string().min(1), per_page: perPage, orientation, size },
    },
    (args) => toolResult(() => searchPhotos(args)),
);

server.registerTool(
    'get_video',
    {
        title: 'Get a Pexels video',
        description:
            'Resolve a single Pexels video by id. Returns {url, photographer, photographer_url, download_links: [...]}. ' +
            'Attribution is required: credit the photographer (link the name to photographer_url) with a link back to url on Pexels.',
        inputSchema: { id: z.number().int().positive() },
    },
    (args) => toolResult(() => getVideo(args.id)),
);

const transport = new StdioServerTransport();
await server.connect(transport);
