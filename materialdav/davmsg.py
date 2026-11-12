"""DAV:multistatus 结构化解析。

与 :func:`materialdav.parser.inspect` 的原文摘要不同，本模块供 mirror
流程使用，区分两类状态：

- response 层状态（404/410）= 资源级墓碑，表示资源明确删除；
- propstat 层状态（403/500 等）= 属性级未知，只标记不可信，
  绝不产出空值去覆盖旧属性。

属性值只在 propstat 200 时提取；缺失的属性不出现在结果里。
"""
import xml.etree.ElementTree as ET

DAV = '{DAV:}'
KNOWN_PROPERTIES = ('getetag', 'getcontentlength', 'displayname', 'resourcetype')


def _status_code(status_line):
    if not status_line:
        return None
    parts = status_line.split(' ', 2)
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return None


def _is_collection(prop):
    resource_type = prop.find(DAV + 'resourcetype')
    return resource_type is not None and resource_type.find(DAV + 'collection') is not None


def parse_multistatus(payload):
    """解析 207 正文，返回 :class:`Multistatus`。

    重复 href 的 response 保留原始顺序，由调用方按序应用。
    """
    root = ET.fromstring(payload)
    if root.tag != DAV + 'multistatus':
        raise ValueError('Expected DAV multistatus')
    responses = []
    for response in root.findall(DAV + 'response'):
        href = response.findtext(DAV + 'href')
        response_status = _status_code(response.findtext(DAV + 'status'))
        properties = {}
        prop_status = {}
        for propstat in response.findall(DAV + 'propstat'):
            status = _status_code(propstat.findtext(DAV + 'status'))
            prop = propstat.find(DAV + 'prop')
            if prop is None:
                continue
            for child in prop:
                name = child.tag[len(DAV):] if child.tag.startswith(DAV) else child.tag
                prop_status[name] = status
                if status == 200:
                    if name == 'resourcetype':
                        properties[name] = 'collection' if _is_collection(prop) else ''
                    else:
                        properties[name] = ''.join(child.itertext())
        responses.append(Response(href=href, status=response_status,
                                  properties=properties, prop_status=prop_status))
    token_element = root.find(DAV + 'sync-token')
    sync_token = token_element.text if token_element is not None else None
    return Multistatus(responses=responses, sync_token=sync_token)


class Multistatus:
    def __init__(self, responses, sync_token):
        self.responses = responses
        self.sync_token = sync_token


class Response:
    """单个 DAV:response。

    tombstone 仅在 response 层明确 404/410 时成立。
    """

    def __init__(self, href, status, properties, prop_status):
        self.href = href
        self.status = status
        self.properties = properties
        self.prop_status = prop_status

    @property
    def tombstone(self):
        return self.status in (404, 410)

    @property
    def is_collection(self):
        return self.properties.get('resourcetype') == 'collection'

    def trusted(self, name):
        """该属性是否可信（propstat 200 且有值）。"""
        return self.prop_status.get(name) == 200 and name in self.properties
