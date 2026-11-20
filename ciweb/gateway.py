import asyncio
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

PREFIX = '/buildbot'
HOP_BY_HOP = frozenset(('connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                        'te', 'trailers', 'transfer-encoding', 'upgrade'))
FORWARDED = ('x-forwarded-for', 'x-forwarded-proto', 'x-forwarded-host', 'x-forwarded-prefix', 'x-real-ip')


def origin_of(url):
    parts = urlsplit(url)
    return '%s://%s' % (parts.scheme, parts.netloc)


class Gateway:
    def __init__(self, ui_url, buildbot_url, prefix = PREFIX):
        self.ui        = ui_url.rstrip('/')
        self.buildbot  = buildbot_url.rstrip('/')
        self.prefix    = prefix.rstrip('/')
        self.session   = None

    def application(self):
        app = web.Application(client_max_size = 0)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)
        app.router.add_route('*', '/{tail:.*}', self.handle)
        return app

    async def start(self, app):
        self.session = aiohttp.ClientSession(auto_decompress = False, cookie_jar = aiohttp.DummyCookieJar(),
                                             skip_auto_headers = ('Accept-Encoding',),
                                             timeout = aiohttp.ClientTimeout(total = None, sock_connect = 10))

    async def stop(self, app):
        await self.session.close()

    def route(self, request):
        path = request.raw_path.split('?', 1)[0]
        if path == self.prefix:
            raise web.HTTPPermanentRedirect(self.prefix + '/' + ('?' + request.rel_url.raw_query_string if request.rel_url.raw_query_string else ''))
        if path.startswith(self.prefix + '/'):
            return 'Buildbot', self.buildbot, path[len(self.prefix):]
        return 'The web UI', self.ui, path

    def outgoing_headers(self, request, upstream_name):
        headers = {}
        for key, value in request.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered in FORWARDED or lowered == 'host':
                continue
            headers[key] = value
        headers['Host']              = request.host
        headers['X-Forwarded-Host']  = request.host
        headers['X-Forwarded-Proto'] = request.scheme
        headers['X-Forwarded-For']   = request.remote or ''
        if upstream_name == 'Buildbot':
            headers['X-Forwarded-Prefix'] = self.prefix
        return headers

    def rewrite_location(self, request, upstream_name, upstream_base, location):
        if location.startswith('//'):
            return location
        if location.startswith('/'):
            return (self.prefix + location) if upstream_name == 'Buildbot' else location
        if location.startswith(origin_of(upstream_base)):
            rest = location[len(origin_of(upstream_base)):]
            if upstream_name == 'Buildbot':
                rest = self.prefix + rest
            return '%s://%s%s' % (request.scheme, request.host, rest)
        return location

    async def handle(self, request):
        name, base, path = self.route(request)
        query = '?' + request.rel_url.raw_query_string if request.rel_url.raw_query_string else ''
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self.relay_websocket(request, name, base + path + query)
        try:
            async with self.session.request(request.method, base + path + query,
                                            headers = self.outgoing_headers(request, name),
                                            data = request.content if request.can_read_body else None,
                                            allow_redirects = False) as upstream:
                return await self.relay_response(request, name, base, upstream)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.Response(status = 502, text = '%s is not reachable or answered badly. Is it running?' % name)

    async def relay_response(self, request, name, base, upstream):
        response = web.StreamResponse(status = upstream.status, reason = upstream.reason)
        for key, value in upstream.headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP or lowered == 'content-length':
                continue
            if lowered == 'location':
                value = self.rewrite_location(request, name, base, value)
            response.headers.add(key, value)
        if upstream.headers.get('Content-Length') is not None and request.method != 'HEAD':
            response.content_length = int(upstream.headers['Content-Length'])
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response

    async def relay_websocket(self, request, name, url):
        protocols = tuple(p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip())
        wanted    = {k: v for k, v in self.outgoing_headers(request, name).items()
                     if k.lower() in ('cookie', 'origin', 'user-agent', 'x-forwarded-host', 'x-forwarded-proto', 'x-forwarded-for', 'x-forwarded-prefix')}
        try:
            async with self.session.ws_connect('ws' + url[len('http'):], protocols = protocols, headers = wanted,
                                               max_msg_size = 0, heartbeat = 30) as upstream:
                downstream = web.WebSocketResponse(protocols = (upstream.protocol,) if upstream.protocol else (), max_msg_size = 0, heartbeat = 30)
                await downstream.prepare(request)
                tasks = {asyncio.create_task(self.pump(downstream, upstream)), asyncio.create_task(self.pump(upstream, downstream))}
                done, pending = await asyncio.wait(tasks, return_when = asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await downstream.close()
                return downstream
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.Response(status = 502, text = '%s is not reachable or answered badly. Is it running?' % name)

    async def pump(self, source, target):
        async for message in source:
            if message.type == aiohttp.WSMsgType.TEXT:
                await target.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await target.send_bytes(message.data)
            else:
                break
