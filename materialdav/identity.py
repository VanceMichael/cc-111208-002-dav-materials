"""稳定资源身份：href 解析、百分号编码与根边界。

身份规则（见 docs/source-contract.md 与 docs/mirror.md）：
- 相对 href 以实际响应基地址解析，不使用配置以外的基地址。
- 路径按段做 NFC 规范化后重新编码；编码斜杠 ``%2F`` 属于段内容，
  绝不当作路径分隔符。
- 集合身份以斜杠结尾，资源不以斜杠结尾。
- 只允许配置根同源、同根路径下的地址，根外与其他 origin 一律拒绝。
"""
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

# RFC 3986 sub-delims 与段内允许的标点，保持不编码以得到稳定输出。
_SAFE_SEGMENT = "!$&'()*+,;=:@" + "-._~"


def _normalize_segment(segment):
    decoded = unquote(segment)
    normalized = unicodedata.normalize('NFC', decoded)
    return quote(normalized, safe=_SAFE_SEGMENT)


def normalize_path(path):
    """规范化绝对路径：处理点段、NFC、百分号编码、尾斜杠。

    编码斜杠保持为段内字符；规范化点段时不依赖解码后的内容。
    """
    if not path.startswith('/'):
        raise ValueError('collection root must be an absolute path')
    trailing = path.endswith('/') and path != '/'
    parts = path.split('/')
    stack = []
    for part in parts[1:]:
        if part in ('', '.'):
            continue
        if part == '..':
            if not stack:
                raise ValueError('href escapes the configured root')
            stack.pop()
            continue
        stack.append(_normalize_segment(part))
    result = '/' + '/'.join(stack)
    if trailing or path == '/':
        result += '/'
    return result


def resolve_href(base_url, href, is_collection):
    """按响应基地址解析 href，返回规范化后的绝对 URL。

    禁止片段（fragment），保留查询串（本协议子集一般不出现）。
    """
    joined = urlsplit(urljoin(base_url, href))
    if joined.fragment or '#' in href:
        raise ValueError('fragment identifiers are not part of resource identity')
    scheme = joined.scheme or base.scheme
    netloc = joined.netloc or base.netloc
    path = joined.path or '/'
    if is_collection and not path.endswith('/'):
        path += '/'
    normalized_path = normalize_path(path)
    return urlunsplit((scheme, netloc, normalized_path, joined.query, ''))


def _default_port(scheme):
    return '80' if scheme == 'http' else '443'


def _origin_key(split):
    hostname = split.hostname or ''
    port = str(split.port) if split.port is not None else _default_port(split.scheme)
    return split.scheme.lower(), hostname.lower(), port


def ensure_within(root_url, candidate_url):
    """候选地址必须与配置根同 origin，且路径位于根路径之下。"""
    root = urlsplit(root_url)
    candidate = urlsplit(candidate_url)
    if _origin_key(root) != _origin_key(candidate):
        raise ValueError('href origin differs from the configured collection root: ' + candidate_url)
    if candidate.username or candidate.password:
        raise ValueError('credentials in href are not accepted')
    root_path = normalize_path(root.path + ('/' if not root.path.endswith('/') else ''))
    candidate_path = normalize_path(candidate.path + ('/' if not candidate.path.endswith('/') else ''))
    if candidate_path != root_path and not candidate_path.startswith(root_path):
        raise ValueError('href escapes the configured collection root: ' + candidate_url)
    if candidate.query or candidate.fragment:
        raise ValueError('query/fragment addresses are outside the collection subset')
    return candidate_url


def identity_url(root_url, base_url, href, is_collection):
    """解析并校验一个响应条目，返回稳定身份 URL。"""
    candidate = resolve_href(base_url, href, is_collection)
    return ensure_within(root_url, candidate)


def is_collection_identity(url):
    return urlsplit(url).path.endswith('/')


def relative_segments(root_url, url):
    """返回身份相对配置根的路径段（已解码、NFC），用于本地落盘。"""
    root_path = urlsplit(root_url).path
    path = urlsplit(url).path
    root_prefix = root_path if root_path.endswith('/') else root_path + '/'
    if not (path == root_path.rstrip('/') or path.startswith(root_prefix)):
        raise ValueError('url is not below the configured root')
    if path.rstrip('/') == root_path.rstrip('/'):
        return []
    relative = path[len(root_prefix):]
    return [unicodedata.normalize('NFC', unquote(part)) for part in relative.strip('/').split('/') if part]
