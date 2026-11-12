"""mirror 使用的受限 HTTP 客户端。

与 :func:`materialdav.transport.capture` 保持同一安全边界：只接受
loopback HTTP、URL 内不得带凭据、不跟随重定向、不使用代理。新增
超时与响应体大小限制，网络错误以 :class:`DavClientError` 抛出；
HTTP 4xx/5xx 仍然返回状态码与正文，由同步流程按协议语义处理。
"""
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .identity import ensure_within


class DavClientError(OSError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


class DavClient:
    def __init__(self, root_url, timeout=10, maximum=16 * 1024 * 1024):
        parsed = urlsplit(root_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('The mirror transport accepts loopback HTTP only')
        if parsed.username or parsed.password:
            raise ValueError('Credentials in URLs are not accepted')
        self.root_url = root_url
        self.timeout = timeout
        self.maximum = maximum
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, url, method, headers=None, body=None):
        ensure_within(self.root_url, url)
        request = Request(url, data=body, method=method, headers=headers or {})
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        except URLError as error:
            raise DavClientError(str(error)) from error
        with response:
            payload = response.read(self.maximum + 1)
            if len(payload) > self.maximum:
                raise DavClientError('response exceeds byte limit')
            return response.status, dict(response.headers.items()), payload

    def propfind(self, url):
        return self.request(url, 'PROPFIND',
                            {'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8'},
                            b'<?xml version="1.0" encoding="utf-8"?>\n'
                            b'<d:propfind xmlns:d="DAV:"><d:prop>'
                            b'<d:resourcetype/><d:displayname/><d:getetag/><d:getcontentlength/>'
                            b'</d:prop></d:propfind>')

    def sync_report(self, url, token):
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<d:sync-collection xmlns:d="DAV:">'
            '<d:sync-token>{token}</d:sync-token>'
            '<d:sync-level>1</d:sync-level>'
            '<d:prop><d:resourcetype/><d:displayname/><d:getetag/><d:getcontentlength/></d:prop>'
            '</d:sync-collection>').format(token=_xml_escape(token)).encode('utf-8')
        return self.request(url, 'REPORT',
                            {'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8'},
                            body)

    def get(self, url, etag):
        return self.request(url, 'GET', {'If-Match': etag})


def _xml_escape(value):
    return (value.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))
