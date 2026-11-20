import gzip
import json
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from ciweb.gateway import Gateway


async def echo(request):
    body = await request.read()
    return web.json_response({
        'path': request.path_qs, 'method': request.method, 'host': request.headers.get('Host'),
        'body': body.decode('utf8', 'replace'), 'cookie': request.headers.get('Cookie'),
        'forwarded_host': request.headers.get('X-Forwarded-Host'), 'forwarded_proto': request.headers.get('X-Forwarded-Proto'),
        'forwarded_for': request.headers.get('X-Forwarded-For'), 'forwarded_prefix': request.headers.get('X-Forwarded-Prefix'),
        'accept_encoding': request.headers.get('Accept-Encoding'), 'real_ip': request.headers.get('X-Real-IP')})


def fake_ui(port_holder):
    async def absolute_redirect(request):
        raise web.HTTPFound('http://127.0.0.1:%d/next?x=1' % port_holder['ui'])

    async def cookies(request):
        response = web.Response(text = 'cookies')
        response.set_cookie('sessionid', 'abc', httponly = True)
        response.set_cookie('csrftoken', 'def')
        return response

    async def big(request):
        return web.Response(body = b'x' * (5 * 1024 * 1024), content_type = 'application/octet-stream')

    async def strict(request):
        chunked = request.headers.get('Transfer-Encoding', '').lower() == 'chunked'
        body = await request.read()
        return web.json_response({'chunked': chunked, 'length_header': request.headers.get('Content-Length'), 'received': len(body)})

    async def garbage(request):
        request.transport.write(b'this is not http at all\r\n\r\n')
        request.transport.close()
        return web.Response()

    app = web.Application()
    app.router.add_route('*', '/echo', echo)
    app.router.add_post('/strict', strict)
    app.router.add_get('/garbage', garbage)
    app.router.add_get('/redirect', absolute_redirect)
    app.router.add_get('/cookies', cookies)
    app.router.add_get('/big', big)
    app.router.add_get('/', lambda request: web.Response(text = 'web ui home'))
    app.router.add_get('/buildbotx', lambda request: web.Response(text = 'ui owns buildbotx'))
    return app


def fake_buildbot():
    async def relative_redirect(request):
        raise web.HTTPFound('/new/place')

    async def gzipped(request):
        return web.Response(body = gzip.compress(b'compressed hello'), headers = {'Content-Encoding': 'gzip'}, content_type = 'text/plain')

    async def socket(request):
        ws = web.WebSocketResponse(protocols = ('wamp.json',))
        await ws.prepare(request)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(message.data.upper())
            elif message.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(message.data[::-1])
        return ws

    app = web.Application()
    app.router.add_route('*', '/api/v2/echo', echo)
    app.router.add_get('/', lambda request: web.Response(text = 'buildbot home'))
    app.router.add_get('/redirect', relative_redirect)
    app.router.add_get('/gz', gzipped)
    app.router.add_get('/ws', socket)
    return app


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ports = {}
        self.ui = TestServer(fake_ui(self.ports))
        self.bb  = TestServer(fake_buildbot())
        await self.ui.start_server()
        await self.bb.start_server()
        self.ports['ui'] = self.ui.port
        self.gateway = Gateway('http://127.0.0.1:%d' % self.ui.port, 'http://127.0.0.1:%d/' % self.bb.port)
        self.server  = TestServer(self.gateway.application())
        await self.server.start_server()
        self.base    = 'http://127.0.0.1:%d' % self.server.port
        self.http    = aiohttp.ClientSession(auto_decompress = False, cookie_jar = aiohttp.DummyCookieJar(), skip_auto_headers = ('Accept-Encoding',))

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()
        await self.ui.close()
        await self.bb.close()

    async def get_json(self, path, **kwargs):
        async with self.http.request(kwargs.pop('method', 'GET'), self.base + path, **kwargs) as reply:
            return reply.status, await reply.json()

    async def test_everything_but_the_buildbot_prefix_goes_to_the_web_ui(self):
        status, data = await self.get_json('/echo?a=1&b=two%20words')
        self.assertEqual((status, data['path']), (200, '/echo?a=1&b=two%20words'))
        async with self.http.get(self.base + '/') as reply:
            self.assertEqual(await reply.text(), 'web ui home')
        async with self.http.get(self.base + '/buildbotx') as reply:
            self.assertEqual(await reply.text(), 'ui owns buildbotx')

    async def test_the_prefix_is_stripped_before_the_request_reaches_buildbot(self):
        status, data = await self.get_json('/buildbot/api/v2/echo?z=2')
        self.assertEqual((status, data['path']), (200, '/api/v2/echo?z=2'))
        self.assertEqual(data['forwarded_prefix'], '/buildbot')
        async with self.http.get(self.base + '/buildbot/') as reply:
            self.assertEqual(await reply.text(), 'buildbot home')

    async def test_the_bare_prefix_redirects_to_the_slash_form(self):
        async with self.http.get(self.base + '/buildbot', allow_redirects = False) as reply:
            self.assertEqual((reply.status, reply.headers['Location']), (308, '/buildbot/'))
        async with self.http.get(self.base + '/buildbot?x=1', allow_redirects = False) as reply:
            self.assertEqual(reply.headers['Location'], '/buildbot/?x=1')

    async def test_the_app_sees_the_address_people_used_not_its_own(self):
        status, data = await self.get_json('/echo', headers = {'X-Forwarded-Host': 'evil.example', 'X-Forwarded-For': '6.6.6.6', 'X-Real-IP': '6.6.6.6'})
        host = '127.0.0.1:%d' % self.server.port
        self.assertEqual(data['host'], host)
        self.assertEqual(data['forwarded_host'], host)
        self.assertEqual(data['forwarded_proto'], 'http')
        self.assertEqual(data['forwarded_for'], '127.0.0.1')
        self.assertIsNone(data['real_ip'])

    async def test_bodies_methods_and_cookies_pass_through(self):
        status, data = await self.get_json('/echo', method = 'POST', data = 'name=alice&x=1', headers = {'Cookie': 'sessionid=abc; csrftoken=def'})
        self.assertEqual((data['method'], data['body'], data['cookie']), ('POST', 'name=alice&x=1', 'sessionid=abc; csrftoken=def'))
        status, data = await self.get_json('/buildbot/api/v2/echo', method = 'POST', json = {'jsonrpc': '2.0'})
        self.assertEqual(json.loads(data['body']), {'jsonrpc': '2.0'})

    async def test_upload_bodies_keep_their_length_because_django_cannot_read_chunked_ones(self):
        form = 'csrfmiddlewaretoken=abc&username=boss'
        async with self.http.post(self.base + '/strict', data = form) as reply:
            data = await reply.json()
        self.assertFalse(data['chunked'])
        self.assertEqual((data['length_header'], data['received']), (str(len(form)), len(form)))
        big = b'y' * 300000
        async with self.http.post(self.base + '/strict', data = big) as reply:
            data = await reply.json()
        self.assertEqual((data['chunked'], data['received']), (False, 300000))

    async def test_a_program_that_answers_with_nonsense_gets_a_clean_502(self):
        async with self.http.get(self.base + '/garbage') as reply:
            self.assertEqual(reply.status, 502)
            self.assertIn('answered badly', await reply.text())

    async def test_every_set_cookie_header_survives(self):
        async with self.http.get(self.base + '/cookies') as reply:
            cookies = reply.headers.getall('Set-Cookie')
        self.assertEqual(len(cookies), 2)
        self.assertTrue(any(c.startswith('sessionid=abc') and 'HttpOnly' in c for c in cookies))

    async def test_redirects_are_rewritten_to_the_public_address(self):
        host = '127.0.0.1:%d' % self.server.port
        async with self.http.get(self.base + '/redirect', allow_redirects = False) as reply:
            self.assertEqual(reply.headers['Location'], 'http://%s/next?x=1' % host)
        async with self.http.get(self.base + '/buildbot/redirect', allow_redirects = False) as reply:
            self.assertEqual(reply.headers['Location'], '/buildbot/new/place')

    async def test_compressed_answers_are_passed_on_untouched(self):
        async with self.http.get(self.base + '/buildbot/gz', headers = {'Accept-Encoding': 'gzip'}) as reply:
            self.assertEqual(reply.headers['Content-Encoding'], 'gzip')
            self.assertEqual(gzip.decompress(await reply.read()), b'compressed hello')

    async def test_a_large_answer_arrives_complete(self):
        async with self.http.get(self.base + '/big') as reply:
            body = await reply.read()
        self.assertEqual(len(body), 5 * 1024 * 1024)

    async def test_websockets_to_buildbot_are_relayed_both_ways(self):
        async with self.http.ws_connect(self.base + '/buildbot/ws', protocols = ('wamp.json',)) as ws:
            self.assertEqual(ws.protocol, 'wamp.json')
            await ws.send_str('hello builders')
            self.assertEqual((await ws.receive()).data, 'HELLO BUILDERS')
            await ws.send_bytes(b'abc')
            self.assertEqual((await ws.receive()).data, b'cba')
            await ws.close()

    async def test_an_unreachable_program_gets_a_plain_message_naming_it(self):
        broken = Gateway('http://127.0.0.1:1', 'http://127.0.0.1:1/')
        server = TestServer(broken.application())
        await server.start_server()
        try:
            async with self.http.get('http://127.0.0.1:%d/anything' % server.port) as reply:
                self.assertEqual(reply.status, 502)
                self.assertIn('The web UI is not reachable', await reply.text())
            async with self.http.get('http://127.0.0.1:%d/buildbot/' % server.port) as reply:
                self.assertEqual(reply.status, 502)
                self.assertIn('Buildbot is not reachable', await reply.text())
        finally:
            await server.close()

    async def test_no_compression_is_added_for_clients_that_did_not_ask(self):
        status, data = await self.get_json('/echo')
        self.assertIsNone(data['accept_encoding'])


if __name__ == '__main__':
    unittest.main()
