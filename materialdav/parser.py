import xml.etree.ElementTree as ET

DAV = '{DAV:}'


def inspect(payload):
    root = ET.fromstring(payload)
    if root.tag != DAV + 'multistatus':
        raise ValueError('Expected DAV multistatus')
    resources = []
    for response in root.findall(DAV + 'response'):
        properties = []
        for propstat in response.findall(DAV + 'propstat'):
            prop = propstat.find(DAV + 'prop')
            properties.append({'status': propstat.findtext(DAV + 'status'),
                               'values': {child.tag: ''.join(child.itertext()) for child in prop} if prop is not None else {}})
        resources.append({'href': response.findtext(DAV + 'href'),
                          'status': response.findtext(DAV + 'status'), 'properties': properties})
    return {'resources': resources, 'sync_token': root.findtext(DAV + 'sync-token')}
